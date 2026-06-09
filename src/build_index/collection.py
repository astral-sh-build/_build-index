"""Deterministic release-asset collection models."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from packaging.utils import (
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_REVISION_PATTERN = re.compile(r"^(?P<base>.+)-r(?P<revision>[1-9][0-9]*)$")
_UPLOAD_TIME_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)


class CollectionError(ValueError):
    """Raised when collected release-asset data is invalid."""


@dataclass(frozen=True)
class CollectedArtifact:
    repository: str
    release: str
    filename: str
    project: str
    version: str
    channel: str
    source_url: str
    download_url: str
    sha256: str
    size: int
    upload_time: str
    published_url: str | None = None
    metadata_sha256: str | None = None
    requires_python: str | None = None

    @property
    def file_key(self) -> tuple[str, str, str]:
        return (self.channel, self.project, self.filename)

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "download_url": self.download_url,
            "filename": self.filename,
            "metadata_sha256": self.metadata_sha256,
            "project": self.project,
            "published_url": self.published_url,
            "release": self.release,
            "repository": self.repository,
            "requires_python": self.requires_python,
            "sha256": self.sha256,
            "size": self.size,
            "source_url": self.source_url,
            "upload_time": self.upload_time,
            "version": self.version,
        }


@dataclass(frozen=True)
class ReleaseCollection:
    artifacts: tuple[CollectedArtifact, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifacts": [artifact.as_dict() for artifact in self.artifacts],
            "schema_version": 2,
        }


def collection_from_artifacts(
    artifacts: Iterable[CollectedArtifact],
) -> ReleaseCollection:
    """Validate and sort collected wheel assets."""
    by_key: dict[tuple[str, str, str], CollectedArtifact] = {}
    for artifact in artifacts:
        _validate_artifact(artifact)
        existing = by_key.get(artifact.file_key)
        if existing is not None:
            if existing.repository != artifact.repository:
                raise CollectionError(
                    "duplicate wheel filename in index: "
                    f"{artifact.channel}/{artifact.project}/{artifact.filename} from "
                    f"{existing.repository}@{existing.release} and "
                    f"{artifact.repository}@{artifact.release}"
                )
            existing_time = _upload_datetime(existing)
            artifact_time = _upload_datetime(artifact)
            if existing_time == artifact_time and existing.sha256 != artifact.sha256:
                raise CollectionError(
                    "conflicting wheel filename has the same upload time: "
                    f"{artifact.channel}/{artifact.project}/{artifact.filename} from "
                    f"{existing.repository}@{existing.release} and "
                    f"{artifact.repository}@{artifact.release}"
                )
            if artifact_time <= existing_time:
                continue
        by_key[artifact.file_key] = artifact

    return ReleaseCollection(
        artifacts=tuple(
            sorted(
                by_key.values(),
                key=lambda artifact: (
                    artifact.channel,
                    artifact.project,
                    artifact.filename,
                ),
            )
        )
    )


def load_collection(path: Path) -> ReleaseCollection:
    """Load one deterministic release-asset collection file."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CollectionError(f"could not load {path}: {error}") from error

    if not isinstance(document, dict):
        raise CollectionError("collection must be a JSON object")
    _expect_keys(document, {"schema_version", "artifacts"}, "collection")
    if document.get("schema_version") != 2:
        raise CollectionError("collection.schema_version must be exactly 2")
    values = document.get("artifacts")
    if not isinstance(values, list):
        raise CollectionError("collection.artifacts must be a list")

    artifacts = tuple(
        _artifact_from_dict(value, index) for index, value in enumerate(values)
    )
    collection = collection_from_artifacts(artifacts)
    if collection.artifacts != artifacts:
        raise CollectionError(
            "collection.artifacts must be sorted and contain unique wheel filenames"
        )
    return collection


def write_collection(path: Path, collection: ReleaseCollection) -> Path:
    """Write one deterministic release-asset collection file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(collection.as_dict(), ensure_ascii=True, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return path


def release_revision(tag: str) -> tuple[str, int]:
    """Return a release tag's base family and trailing revision number."""
    match = _RELEASE_REVISION_PATTERN.fullmatch(tag)
    if match is None:
        return tag, 0
    return match.group("base"), int(match.group("revision"))


def normalize_wheel_filename(filename: str) -> str:
    """Normalize repeated local-version separators in legacy wheel names."""
    try:
        parse_wheel_filename(filename)
    except InvalidWheelFilename as original_error:
        if not filename.endswith(".whl"):
            raise original_error
        try:
            name_and_version, python_tag, abi_tag, platform_tag = filename[
                : -len(".whl")
            ].rsplit("-", 3)
            distribution, version = name_and_version.rsplit("-", 1)
            public, local = version.split("+", 1)
        except ValueError:
            raise original_error from None
        if "+" not in local:
            raise original_error
        normalized = (
            f"{distribution}-{public}+{local.replace('+', '.')}-"
            f"{python_tag}-{abi_tag}-{platform_tag}.whl"
        )
        try:
            parse_wheel_filename(normalized)
        except InvalidWheelFilename:
            raise original_error from None
        return normalized
    return filename


def parse_collected_wheel_filename(filename: str) -> tuple[str, Version]:
    """Return the distribution and version from an indexed wheel filename."""
    distribution, version, _build, _tags = parse_wheel_filename(
        normalize_wheel_filename(filename)
    )
    return distribution, version


def _artifact_from_dict(value: Any, index: int) -> CollectedArtifact:
    context = f"collection.artifacts[{index}]"
    if not isinstance(value, dict):
        raise CollectionError(f"{context} must be an object")
    keys = {
        "repository",
        "release",
        "filename",
        "project",
        "version",
        "channel",
        "source_url",
        "download_url",
        "sha256",
        "size",
        "upload_time",
        "published_url",
        "metadata_sha256",
        "requires_python",
    }
    _expect_keys(value, keys, context)
    missing = sorted(keys - set(value))
    if missing:
        raise CollectionError(f"{context} is missing keys: {', '.join(missing)}")
    return CollectedArtifact(
        repository=_string(value, "repository", context),
        release=_string(value, "release", context),
        filename=_string(value, "filename", context),
        project=_string(value, "project", context),
        version=_string(value, "version", context),
        channel=_string(value, "channel", context),
        source_url=_string(value, "source_url", context),
        download_url=_string(value, "download_url", context),
        sha256=_string(value, "sha256", context),
        size=_integer(value, "size", context),
        upload_time=_string(value, "upload_time", context),
        published_url=_optional_string(value, "published_url", context),
        metadata_sha256=_optional_string(value, "metadata_sha256", context),
        requires_python=_optional_string(value, "requires_python", context),
    )


def _validate_artifact(artifact: CollectedArtifact) -> None:
    try:
        distribution, version = parse_collected_wheel_filename(artifact.filename)
    except InvalidWheelFilename as error:
        raise CollectionError(
            f"artifact has invalid wheel filename: {artifact.filename}"
        ) from error
    if canonicalize_name(distribution) != artifact.project:
        raise CollectionError(
            f"artifact project does not match wheel filename: {artifact.filename}"
        )
    if str(version) != artifact.version:
        raise CollectionError(
            f"artifact version does not match wheel filename: {artifact.filename}"
        )
    if canonicalize_name(artifact.project) != artifact.project:
        raise CollectionError(
            f"artifact has non-normalized project name: {artifact.filename}"
        )
    if not _SHA256_PATTERN.fullmatch(artifact.sha256):
        raise CollectionError(f"artifact has invalid SHA-256: {artifact.filename}")
    if artifact.size < 0:
        raise CollectionError(f"artifact has invalid size: {artifact.filename}")
    for label, value in (
        ("repository", artifact.repository),
        ("release", artifact.release),
        ("project", artifact.project),
        ("version", artifact.version),
        ("channel", artifact.channel),
        ("source URL", artifact.source_url),
        ("download URL", artifact.download_url),
        ("upload time", artifact.upload_time),
    ):
        if not value:
            raise CollectionError(f"artifact has empty {label}: {artifact.filename}")
    for label, value in (
        ("source URL", artifact.source_url),
        ("download URL", artifact.download_url),
        ("published URL", artifact.published_url),
    ):
        if value is None:
            continue
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.fragment:
            raise CollectionError(f"artifact has invalid {label}: {artifact.filename}")
    if (artifact.published_url is None) != (artifact.metadata_sha256 is None):
        raise CollectionError(
            f"artifact has incomplete publication metadata: {artifact.filename}"
        )
    if artifact.metadata_sha256 is not None and not _SHA256_PATTERN.fullmatch(
        artifact.metadata_sha256
    ):
        raise CollectionError(
            f"artifact has invalid metadata SHA-256: {artifact.filename}"
        )
    if artifact.requires_python is not None:
        if artifact.published_url is None:
            raise CollectionError(
                f"artifact has Requires-Python without publication metadata: "
                f"{artifact.filename}"
            )
        try:
            normalized_requires_python = str(SpecifierSet(artifact.requires_python))
        except InvalidSpecifier as error:
            raise CollectionError(
                f"artifact has invalid Requires-Python: {artifact.filename}"
            ) from error
        if normalized_requires_python != artifact.requires_python:
            raise CollectionError(
                f"artifact has non-normalized Requires-Python: {artifact.filename}"
            )
    if not _UPLOAD_TIME_PATTERN.fullmatch(artifact.upload_time):
        raise CollectionError(f"artifact has invalid upload time: {artifact.filename}")
    try:
        timestamp = _upload_datetime(artifact)
    except ValueError as error:
        raise CollectionError(
            f"artifact has invalid upload time: {artifact.filename}"
        ) from error
    if timestamp.utcoffset() is None:
        raise CollectionError(f"artifact has invalid upload time: {artifact.filename}")


def _upload_datetime(artifact: CollectedArtifact) -> datetime:
    return datetime.fromisoformat(artifact.upload_time.removesuffix("Z") + "+00:00")


def _string(value: dict[str, Any], key: str, context: str) -> str:
    result = value[key]
    if not isinstance(result, str):
        raise CollectionError(f"{context}.{key} must be a string")
    return result


def _integer(value: dict[str, Any], key: str, context: str) -> int:
    result = value[key]
    if not isinstance(result, int) or isinstance(result, bool):
        raise CollectionError(f"{context}.{key} must be an integer")
    return result


def _optional_string(
    value: dict[str, Any],
    key: str,
    context: str,
) -> str | None:
    result = value[key]
    if result is not None and not isinstance(result, str):
        raise CollectionError(f"{context}.{key} must be a string or null")
    return result


def _expect_keys(value: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise CollectionError(f"{context} contains unknown keys: {', '.join(unknown)}")
