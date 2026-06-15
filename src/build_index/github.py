"""Collect admitted wheel assets from published GitHub Releases."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

from packaging.utils import (
    InvalidWheelFilename,
    canonicalize_name,
)
from packaging.version import InvalidVersion, Version

from build_index.collection import (
    CollectedArtifact,
    CollectionError,
    ReleaseCollection,
    collection_from_artifacts,
    normalize_wheel_filename,
    parse_collected_wheel_filename,
    release_revision,
)
from build_index.config import IndexConfig, RepositoryConfig

_DIGEST_PATTERN = re.compile(r"^sha256:([0-9a-f]{64})$")
_CANONICAL_CUDA_LOCAL_PATTERN = re.compile(
    r"(?:^|\.)cu\.(?P<major>[0-9]+)\.(?P<minor>[0-9]+)(?:\.|$)"
)
_LEGACY_CUDA_LOCAL_PATTERN = re.compile(
    r"(?:^|\.)cu(?P<major>[0-9]+)\.(?P<minor>[0-9]+)(?:\.|torch|$)"
)
_CONDENSED_CUDA_LOCAL_PATTERN = re.compile(
    r"(?:^|\.)cu(?P<version>[0-9]+)(?:\.|torch|$)"
)
_CPU_LOCAL_PATTERN = re.compile(r"(?:^|\.)cpu(?:\.|$)")
_ROCM_LOCAL_PATTERN = re.compile(
    r"(?:^|\.)rocm(?P<major>[0-9]+)\.(?P<minor>[0-9]+)(?:\.|torch|$)"
)
_XPU_LOCAL_PATTERN = re.compile(r"(?:^|\.)xpu(?:\.|$)")


class GitHubError(CollectionError):
    """Raised when GitHub release collection fails."""


class WheelCompatibilityError(CollectionError):
    """Raised when a release asset cannot be represented in the index."""


@dataclass(frozen=True)
class SelectedRelease:
    document: dict[str, Any]
    captured_version: str | None = None
    version: Version | None = None


class GitHubClient:
    """Small GitHub REST API client for release enumeration and asset hashing."""

    def __init__(
        self,
        token: str | None,
        *,
        api_url: str = "https://api.github.com",
    ) -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")

    def list_releases(
        self,
        repository: str,
        *,
        access: str = "private",
        log: Callable[[str], None] | None = None,
    ) -> Iterable[dict[str, Any]]:
        owner, name = repository.split("/", maxsplit=1)
        page = 1
        while True:
            path = (
                f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/releases"
                f"?per_page=100&page={page}"
            )
            document = self.get_json(
                path,
                repository=repository,
                allow_anonymous_fallback=access == "public",
                log=log,
            )
            if not isinstance(document, list):
                raise GitHubError(
                    f"GitHub releases response is not a list: {repository}"
                )
            for release in document:
                if not isinstance(release, dict):
                    raise GitHubError(
                        f"GitHub releases response contains a non-object: {repository}"
                    )
                yield release
            if len(document) < 100:
                return
            page += 1

    def get_json(
        self,
        path: str,
        *,
        repository: str | None = None,
        allow_anonymous_fallback: bool = False,
        log: Callable[[str], None] | None = None,
    ) -> Any:
        try:
            return self._get_json(path, token=self.token)
        except urllib.error.HTTPError as error:
            if not (
                self.token
                and allow_anonymous_fallback
                and _is_repository_access_rejection(error)
            ):
                raise GitHubError(
                    f"GitHub API request failed for {path}: {error}"
                ) from error
            error.close()
            if log is not None:
                log(
                    f"  authentication mode for {repository or path}: "
                    "anonymous fallback"
                )
            try:
                return self._get_json(path, token=None)
            except (
                OSError,
                urllib.error.HTTPError,
                urllib.error.URLError,
                json.JSONDecodeError,
                UnicodeDecodeError,
            ) as anonymous_error:
                raise GitHubError(
                    f"GitHub API request failed for {path}: {anonymous_error}"
                ) from anonymous_error
        except (
            OSError,
            urllib.error.URLError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as error:
            raise GitHubError(
                f"GitHub API request failed for {path}: {error}"
            ) from error

    def _get_json(self, path: str, *, token: str | None) -> Any:
        request = urllib.request.Request(
            f"{self.api_url}{path}",
            headers=self._headers("application/vnd.github+json", token=token),
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def asset_sha256(
        self,
        asset_api_url: str,
        *,
        repository: str | None = None,
        access: str = "private",
        log: Callable[[str], None] | None = None,
    ) -> str:
        request = urllib.request.Request(
            asset_api_url,
            headers=self._headers("application/octet-stream", token=self.token),
        )
        try:
            return _sha256_github_asset(request)
        except urllib.error.HTTPError as error:
            if not (
                self.token
                and access == "public"
                and _is_repository_access_rejection(error)
            ):
                raise GitHubError(
                    f"GitHub release asset download failed for {asset_api_url}: {error}"
                ) from error
            error.close()
            if log is not None:
                log(
                    f"  authentication mode for {repository or asset_api_url}: "
                    "anonymous fallback"
                )
            anonymous_request = urllib.request.Request(
                asset_api_url,
                headers=self._headers("application/octet-stream", token=None),
            )
            try:
                return _sha256_github_asset(anonymous_request)
            except (
                OSError,
                urllib.error.HTTPError,
                urllib.error.URLError,
            ) as anonymous_error:
                raise GitHubError(
                    f"GitHub release asset download failed for "
                    f"{asset_api_url}: {anonymous_error}"
                ) from anonymous_error
        except (OSError, urllib.error.URLError) as error:
            raise GitHubError(
                f"GitHub release asset download failed for {asset_api_url}: {error}"
            ) from error

    def download_asset(
        self,
        asset_api_url: str,
        destination: Path,
        *,
        repository: str | None = None,
        access: str = "private",
        log: Callable[[str], None] | None = None,
    ) -> tuple[str, int]:
        request = urllib.request.Request(
            asset_api_url,
            headers=self._headers("application/octet-stream", token=self.token),
        )
        try:
            return _download_github_asset(request, destination)
        except urllib.error.HTTPError as error:
            if not (
                self.token
                and access == "public"
                and _is_repository_access_rejection(error)
            ):
                raise GitHubError(
                    f"GitHub release asset download failed for {asset_api_url}: {error}"
                ) from error
            error.close()
            if log is not None:
                log(
                    f"  authentication mode for {repository or asset_api_url}: "
                    "anonymous fallback"
                )
            anonymous_request = urllib.request.Request(
                asset_api_url,
                headers=self._headers("application/octet-stream", token=None),
            )
            try:
                return _download_github_asset(anonymous_request, destination)
            except (
                OSError,
                urllib.error.HTTPError,
                urllib.error.URLError,
            ) as anonymous_error:
                raise GitHubError(
                    f"GitHub release asset download failed for "
                    f"{asset_api_url}: {anonymous_error}"
                ) from anonymous_error
        except (OSError, urllib.error.URLError) as error:
            raise GitHubError(
                f"GitHub release asset download failed for {asset_api_url}: {error}"
            ) from error

    def _headers(self, accept: str, *, token: str | None) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": "astral-build-index",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers


def _sha256_github_asset(request: urllib.request.Request) -> str:
    digest = hashlib.sha256()
    with _open_github_asset(request) as response:
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_github_asset(
    request: urllib.request.Request,
    destination: Path,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with _open_github_asset(request) as response:
        with destination.open("wb") as output:
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    return digest.hexdigest(), size


def _is_repository_access_rejection(error: urllib.error.HTTPError) -> bool:
    if error.code == 404:
        return True
    rate_limit_remaining = (
        error.headers.get("X-RateLimit-Remaining")
        if error.headers is not None
        else None
    )
    if error.code != 403 or rate_limit_remaining == "0":
        return False
    try:
        document = json.loads(error.read())
    except json.JSONDecodeError, OSError, TypeError:
        return False
    return (
        isinstance(document, dict)
        and document.get("message") == "Resource not accessible by integration"
    )


def _open_github_asset(request: urllib.request.Request) -> Any:
    """Open a GitHub asset without forwarding API credentials across redirects."""
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        response = opener.open(request, timeout=30)
    except urllib.error.HTTPError as error:
        if error.code not in {301, 302, 303, 307, 308}:
            raise
        location = error.headers.get("Location")
        error.close()
        if not location:
            raise urllib.error.URLError(
                "GitHub release asset route did not return a redirect location"
            )
        download_request = urllib.request.Request(
            urljoin(request.full_url, location),
            headers={
                "Accept": "application/octet-stream",
                "User-Agent": "astral-build-index",
            },
        )
        return urllib.request.urlopen(download_request, timeout=30)
    return response


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def collect_release_assets(
    config: IndexConfig,
    client: GitHubClient,
    *,
    log: Callable[[str], None] | None = None,
) -> ReleaseCollection:
    """Collect admitted wheels from published releases using release assets."""
    logger = log or (lambda _message: None)
    artifacts: list[CollectedArtifact] = []
    channels = tuple(channel.name for channel in config.channels)

    for repository in sorted(config.repositories, key=lambda item: item.repository):
        authentication = "app token" if getattr(client, "token", None) else "anonymous"
        logger(
            f"collecting published releases: {repository.repository}, "
            f"access={repository.access}, authentication={authentication}"
        )
        release_count = 0
        wheel_count = 0
        releases = _select_latest_release_revisions(
            repository,
            client.list_releases(
                repository.repository,
                access=repository.access,
                log=logger,
            ),
            logger,
        )
        for selected_release in releases:
            release_count += 1
            release_artifacts = _release_artifacts(
                repository,
                selected_release,
                client,
                logger,
                channels=channels,
            )
            wheel_count += len(release_artifacts)
            artifacts.extend(release_artifacts)
            for artifact in release_artifacts:
                logger(
                    f"  collected {artifact.release}: {artifact.filename} "
                    f"({artifact.channel})"
                )
        logger(
            f"collected repository: {repository.repository}, "
            f"{release_count} releases, {wheel_count} wheel assets"
        )

    collection = collection_from_artifacts(artifacts)
    collapsed = len(artifacts) - len(collection.artifacts)
    if collapsed:
        logger(f"collapsed {collapsed} republished wheel filenames")
    return collection


def _select_latest_release_revisions(
    repository: RepositoryConfig,
    releases: Iterable[dict[str, Any]],
    logger: Callable[[str], None],
) -> tuple[SelectedRelease, ...]:
    """Select the highest published ``-rN`` revision for each base release tag."""
    candidates: list[tuple[str, int, str, SelectedRelease]] = []
    selected: dict[str, tuple[int, str, SelectedRelease]] = {}

    for release in releases:
        context = repository.repository
        tag = _string(release, "tag_name", context)
        if _boolean(release, "draft", context):
            logger(f"  excluded draft release: {tag}")
            continue
        if (
            _boolean(release, "prerelease", context)
            and not repository.allow_prereleases
        ):
            logger(f"  excluded GitHub prerelease: {tag}")
            continue

        policy_release = _apply_release_version_policy(repository, tag, release, logger)
        if policy_release is None:
            continue
        base, revision = release_revision(tag)
        candidates.append((base, revision, tag, policy_release))
        current = selected.get(base)
        if current is None or revision > current[0]:
            selected[base] = (revision, tag, policy_release)

    result = []
    for base, (revision, selected_tag, selected_release) in sorted(selected.items()):
        logger(f"  selected release: {selected_tag}")
        result.append(selected_release)
        for candidate_base, candidate_revision, candidate_tag, _candidate in candidates:
            if candidate_base == base and candidate_revision < revision:
                logger(f"  superseded release {candidate_tag} by {selected_tag}")
    return tuple(result)


def _apply_release_version_policy(
    repository: RepositoryConfig,
    tag: str,
    release: dict[str, Any],
    logger: Callable[[str], None],
) -> SelectedRelease | None:
    if not repository.has_version_policy:
        return SelectedRelease(document=release)

    match = re.fullmatch(repository.tag_regex, tag)
    if match is None:
        logger(f"  excluded release with nonmatching tag: {tag}")
        return None
    captured_version = match.group("version")
    try:
        version = Version(captured_version)
    except InvalidVersion:
        logger(
            f"  excluded release with invalid captured version: "
            f"{tag} -> {captured_version}"
        )
        return None
    if (
        repository.minimum_release_version is not None
        and version < repository.minimum_release_version
    ):
        logger(f"  excluded release below minimum version: {tag} -> {captured_version}")
        return None
    if (
        repository.maximum_release_version is not None
        and version > repository.maximum_release_version
    ):
        logger(f"  excluded release above maximum version: {tag} -> {captured_version}")
        return None
    if (
        version.is_prerelease or version.is_devrelease
    ) and not repository.allow_prereleases:
        logger(f"  excluded parsed prerelease: {tag} -> {captured_version}")
        return None
    return SelectedRelease(
        document=release,
        captured_version=captured_version,
        version=version,
    )


def infer_channel(version: Version, channels: tuple[str, ...]) -> str:
    """Infer one configured channel from a wheel's local version label."""
    configured = set(channels)
    channel = _infer_channel_label(version, channels)
    return _require_configured_channel(channel, version, configured)


def _infer_channel_label(
    version: Version,
    candidate_channels: tuple[str, ...],
) -> str:
    """Recognize a channel label without applying the publication allowlist."""
    local = version.local
    if local is None:
        raise WheelCompatibilityError(
            f"wheel version has no local channel label: {version}"
        )

    if _CPU_LOCAL_PATTERN.search(local):
        return "cpu"

    for pattern in (_CANONICAL_CUDA_LOCAL_PATTERN, _LEGACY_CUDA_LOCAL_PATTERN):
        if match := pattern.search(local):
            return f"cu{match.group('major')}{match.group('minor')}"

    if match := _CONDENSED_CUDA_LOCAL_PATTERN.search(local):
        return f"cu{match.group('version')}"

    if match := _ROCM_LOCAL_PATTERN.search(local):
        return f"rocm{match.group('major')}.{match.group('minor')}"

    if _XPU_LOCAL_PATTERN.search(local):
        return "xpu"

    matches = []
    for channel in candidate_channels:
        if local == channel:
            matches.append(channel)
            continue
        if local.startswith(channel):
            remainder = local[len(channel) :]
            if remainder and not remainder[0].isdigit():
                matches.append(channel)

    if not matches:
        raise WheelCompatibilityError(
            f"wheel version local label does not match a configured channel: {version}"
        )
    return max(matches, key=len)


def _require_configured_channel(
    channel: str,
    version: Version,
    configured: set[str],
) -> str:
    if channel not in configured:
        raise CollectionError(
            f"wheel version local label selects unconfigured channel "
            f"{channel!r}: {version}"
        )
    return channel


def _release_artifacts(
    repository: RepositoryConfig,
    selected_release: SelectedRelease,
    client: GitHubClient,
    logger: Callable[[str], None],
    *,
    channels: tuple[str, ...],
) -> list[CollectedArtifact]:
    context = repository.repository
    release = selected_release.document
    release_name = _string(release, "tag_name", context)
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise GitHubError(f"GitHub release assets response is not a list: {context}")

    result = []
    for asset in assets:
        if not isinstance(asset, dict):
            raise GitHubError(f"GitHub release contains a non-object asset: {context}")
        filename = _string(asset, "name", f"{context}@{release_name}")
        if not filename.endswith(".whl"):
            logger(f"  excluded non-wheel asset: {release_name}/{filename}")
            continue
        artifact = _collected_artifact(
            repository,
            selected_release,
            asset,
            client,
            logger,
            channels=channels,
        )
        if artifact is None:
            continue
        if artifact.filename != filename:
            logger(f"  normalized wheel filename: {filename} -> {artifact.filename}")
        result.append(artifact)
    return result


def _collected_artifact(
    repository: RepositoryConfig,
    selected_release: SelectedRelease,
    asset: dict[str, Any],
    client: GitHubClient,
    logger: Callable[[str], None],
    *,
    channels: tuple[str, ...],
) -> CollectedArtifact | None:
    release = _string(
        selected_release.document,
        "tag_name",
        repository.repository,
    )
    context = f"{repository.repository}@{release}"
    source_filename = _string(asset, "name", context)
    try:
        filename = normalize_wheel_filename(source_filename)
        distribution, version = parse_collected_wheel_filename(filename)
    except InvalidWheelFilename as error:
        raise WheelCompatibilityError(
            f"invalid wheel filename in {context}: {source_filename}"
        ) from error

    project = canonicalize_name(distribution)
    if project not in repository.projects:
        raise CollectionError(
            f"wheel project is not admitted for {repository.repository}: "
            f"{project} ({filename})"
        )
    channel = _artifact_channel(
        repository,
        selected_release,
        filename,
        version,
        channels=channels,
    )
    if channel in repository.ignored_channels:
        logger(f"  excluded ignored channel {channel}: {release}/{source_filename}")
        return None
    if repository.channels is not None and channel not in repository.channels:
        raise CollectionError(
            f"wheel channel is restricted for {repository.repository}: "
            f"{channel} ({filename})"
        )

    digest = asset.get("digest")
    if digest is None:
        sha256 = client.asset_sha256(
            _string(asset, "url", context),
            repository=repository.repository,
            access=repository.access,
            log=logger,
        )
    elif (
        not isinstance(digest, str)
        or (match := _DIGEST_PATTERN.fullmatch(digest)) is None
    ):
        raise GitHubError(f"GitHub response has invalid 'digest': {context}")
    else:
        sha256 = match.group(1)

    return CollectedArtifact(
        repository=repository.repository,
        release=release,
        filename=filename,
        project=project,
        version=str(version),
        channel=channel,
        source_url=_string(asset, "browser_download_url", context),
        download_url=_string(asset, "url", context),
        sha256=sha256,
        size=_integer(asset, "size", context),
        upload_time=_string(asset, "created_at", context),
    )


def _artifact_channel(
    repository: RepositoryConfig,
    selected_release: SelectedRelease,
    filename: str,
    version: Version,
    *,
    channels: tuple[str, ...],
) -> str:
    if version.local is not None:
        candidates = tuple(dict.fromkeys((*channels, *repository.ignored_channels)))
        channel = _infer_channel_label(version, candidates)
        if channel in repository.ignored_channels:
            return channel
        return _require_configured_channel(channel, version, set(channels))
    if not repository.unlabeled_channel_rules:
        return infer_channel(version, channels)

    release_version = selected_release.version
    captured_version = selected_release.captured_version
    if release_version is None or captured_version is None:
        raise CollectionError(
            f"repository {repository.repository} has unlabeled channel rules "
            "without release version policy"
        )
    matches = [
        rule
        for rule in repository.unlabeled_channel_rules
        if rule.contains(release_version)
    ]
    if len(matches) != 1:
        release = _string(
            selected_release.document,
            "tag_name",
            repository.repository,
        )
        raise WheelCompatibilityError(
            f"unlabeled wheel has no reviewed channel mapping: "
            f"repository={repository.repository}, tag={release}, "
            f"captured_version={captured_version}, filename={filename}"
        )
    return matches[0].channel


def _string(value: dict[str, Any], key: str, context: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise GitHubError(f"GitHub response has invalid {key!r}: {context}")
    return result


def _integer(value: dict[str, Any], key: str, context: str) -> int:
    result = value.get(key)
    if not isinstance(result, int) or isinstance(result, bool):
        raise GitHubError(f"GitHub response has invalid {key!r}: {context}")
    return result


def _boolean(value: dict[str, Any], key: str, context: str) -> bool:
    result = value.get(key)
    if not isinstance(result, bool):
        raise GitHubError(f"GitHub response has invalid {key!r}: {context}")
    return result
