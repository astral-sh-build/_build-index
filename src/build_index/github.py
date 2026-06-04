"""Collect admitted wheel assets from published GitHub Releases."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import quote, urljoin

from packaging.utils import (
    InvalidWheelFilename,
    canonicalize_name,
)
from packaging.version import Version

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

    def list_releases(self, repository: str) -> Iterable[dict[str, Any]]:
        owner, name = repository.split("/", maxsplit=1)
        page = 1
        while True:
            path = (
                f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/releases"
                f"?per_page=100&page={page}"
            )
            document = self.get_json(path)
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

    def get_json(self, path: str) -> Any:
        request = urllib.request.Request(
            f"{self.api_url}{path}",
            headers=self._headers("application/vnd.github+json"),
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except (OSError, urllib.error.HTTPError, urllib.error.URLError) as error:
            raise GitHubError(
                f"GitHub API request failed for {path}: {error}"
            ) from error

    def asset_sha256(self, asset_api_url: str) -> str:
        request = urllib.request.Request(
            asset_api_url,
            headers=self._headers("application/octet-stream"),
        )
        digest = hashlib.sha256()
        try:
            with _open_github_asset(request) as response:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    digest.update(chunk)
        except (OSError, urllib.error.HTTPError, urllib.error.URLError) as error:
            raise GitHubError(
                f"GitHub release asset download failed for {asset_api_url}: {error}"
            ) from error
        return digest.hexdigest()

    def _headers(self, accept: str) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": "astral-build-index",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers


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
        logger(f"collecting published releases: {repository.repository}")
        release_count = 0
        wheel_count = 0
        releases = _select_latest_release_revisions(
            repository,
            client.list_releases(repository.repository),
            logger,
        )
        for release in releases:
            release_count += 1
            release_artifacts = _release_artifacts(
                repository,
                release,
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
) -> tuple[dict[str, Any], ...]:
    """Select the highest published ``-rN`` revision for each base release tag."""
    candidates: list[tuple[str, int, str, dict[str, Any]]] = []
    selected: dict[str, tuple[int, str, dict[str, Any]]] = {}

    for release in releases:
        context = repository.repository
        if _boolean(release, "draft", context):
            continue
        if (
            _boolean(release, "prerelease", context)
            and not repository.allow_prereleases
        ):
            continue

        tag = _string(release, "tag_name", context)
        base, revision = release_revision(tag)
        candidates.append((base, revision, tag, release))
        current = selected.get(base)
        if current is None or revision > current[0]:
            selected[base] = (revision, tag, release)

    result = []
    for base, (revision, selected_tag, release) in sorted(selected.items()):
        result.append(release)
        for candidate_base, candidate_revision, candidate_tag, _candidate in candidates:
            if candidate_base == base and candidate_revision < revision:
                logger(f"  superseded release {candidate_tag} by {selected_tag}")
    return tuple(result)


def infer_channel(version: Version, channels: tuple[str, ...]) -> str:
    """Infer one configured channel from a wheel's local version label."""
    configured = set(channels)
    local = version.local
    if local is None:
        if "pypi" in configured:
            return "pypi"
        raise WheelCompatibilityError(
            f"wheel version has no local channel label: {version}"
        )

    if _CPU_LOCAL_PATTERN.search(local):
        return _require_configured_channel("cpu", version, configured)

    for pattern in (_CANONICAL_CUDA_LOCAL_PATTERN, _LEGACY_CUDA_LOCAL_PATTERN):
        if match := pattern.search(local):
            return _require_configured_channel(
                f"cu{match.group('major')}{match.group('minor')}",
                version,
                configured,
            )

    if match := _CONDENSED_CUDA_LOCAL_PATTERN.search(local):
        return _require_configured_channel(
            f"cu{match.group('version')}",
            version,
            configured,
        )

    if match := _ROCM_LOCAL_PATTERN.search(local):
        return _require_configured_channel(
            f"rocm{match.group('major')}.{match.group('minor')}",
            version,
            configured,
        )

    if _XPU_LOCAL_PATTERN.search(local):
        return _require_configured_channel("xpu", version, configured)

    matches = []
    for channel in channels:
        if local == channel:
            matches.append(channel)
            continue
        if local.startswith(channel):
            remainder = local[len(channel) :]
            if remainder and not remainder[0].isdigit():
                matches.append(channel)

    if not matches:
        if "pypi" in configured:
            return "pypi"
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
    release: dict[str, Any],
    client: GitHubClient,
    logger: Callable[[str], None],
    *,
    channels: tuple[str, ...],
) -> list[CollectedArtifact]:
    context = repository.repository
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
            continue
        artifact = _collected_artifact(
            repository,
            release_name,
            asset,
            client,
            channels=channels,
        )
        if artifact.filename != filename:
            logger(f"  normalized wheel filename: {filename} -> {artifact.filename}")
        result.append(artifact)
    return result


def _collected_artifact(
    repository: RepositoryConfig,
    release: str,
    asset: dict[str, Any],
    client: GitHubClient,
    *,
    channels: tuple[str, ...],
) -> CollectedArtifact:
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
    channel = infer_channel(version, channels)
    if repository.channels is not None and channel not in repository.channels:
        raise CollectionError(
            f"wheel channel is restricted for {repository.repository}: "
            f"{channel} ({filename})"
        )

    digest = asset.get("digest")
    if digest is None:
        sha256 = client.asset_sha256(_string(asset, "url", context))
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
        url=_string(asset, "browser_download_url", context),
        sha256=sha256,
        size=_integer(asset, "size", context),
        upload_time=_string(asset, "created_at", context),
    )


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
