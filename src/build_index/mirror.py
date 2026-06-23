"""Mirror collected wheels and their core metadata to R2."""

from __future__ import annotations

import hashlib
import tempfile
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlparse

from botocore.exceptions import BotoCoreError, ClientError
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from build_index.collection import (
    CollectedArtifact,
    CollectionError,
    ReleaseCollection,
    collection_from_artifacts,
    parse_collected_wheel_filename,
)
from build_index.config import IndexConfig, RepositoryConfig
from build_index.r2 import S3Client, create_s3_client

_ARTIFACT_CACHE_CONTROL = "public, max-age=31536000, immutable"
_DEFAULT_MIRROR_WORKERS = 4
_MAX_HEAD_WORKERS = 16
_MAX_METADATA_SIZE = 10 * 1024 * 1024
_OBJECT_FORMAT_VERSION = "1"


class MirrorError(CollectionError):
    """Raised when an artifact cannot be mirrored safely."""


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    metadata: dict[str, str]


@dataclass(frozen=True)
class CoreMetadata:
    contents: bytes
    sha256: str
    requires_python: str | None
    version_mismatch: bool


class ArtifactDownloader(Protocol):
    def download_asset(
        self,
        asset_api_url: str,
        destination: Path,
        *,
        repository: str | None = None,
        access: str = "private",
        log: Callable[[str], None] | None = None,
    ) -> tuple[str, int]: ...


class ObjectStore(Protocol):
    def head(self, key: str) -> ObjectInfo | None: ...

    def put(
        self,
        key: str,
        source: Path,
        *,
        content_type: str,
        metadata: dict[str, str],
    ) -> None: ...


class S3ObjectStore:
    """Small pooled S3 client for an R2 bucket."""

    def __init__(
        self,
        bucket: str,
        endpoint: str,
        *,
        client: S3Client | None = None,
    ) -> None:
        if not bucket:
            raise MirrorError("R2 bucket must not be empty")
        self.bucket = bucket
        self.endpoint = endpoint.rstrip("/")
        try:
            self.client = client or create_s3_client(
                self.endpoint,
                max_pool_connections=_MAX_HEAD_WORKERS,
            )
        except ValueError as error:
            raise MirrorError(str(error)) from error

    def head(self, key: str) -> ObjectInfo | None:
        try:
            document = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            response = error.response
            code = str(response.get("Error", {}).get("Code", ""))
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
                return None
            raise MirrorError(
                f"R2 head-object failed for {key}: {_client_error_message(error)}"
            ) from error
        except BotoCoreError as error:
            raise MirrorError(f"R2 head-object failed for {key}: {error}") from error
        size = document.get("ContentLength")
        metadata = document.get("Metadata", {})
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or not isinstance(metadata, dict)
            or not all(
                isinstance(name, str) and isinstance(value, str)
                for name, value in metadata.items()
            )
        ):
            raise MirrorError(f"R2 head-object returned invalid data for {key}")
        return ObjectInfo(
            size=size,
            metadata={name.lower(): value for name, value in metadata.items()},
        )

    def put(
        self,
        key: str,
        source: Path,
        *,
        content_type: str,
        metadata: dict[str, str],
    ) -> None:
        try:
            with source.open("rb") as body:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=body,
                    ContentType=content_type,
                    CacheControl=_ARTIFACT_CACHE_CONTROL,
                    Metadata=metadata,
                )
        except ClientError as error:
            raise MirrorError(
                f"R2 put-object failed for {key}: {_client_error_message(error)}"
            ) from error
        except (BotoCoreError, OSError) as error:
            raise MirrorError(f"R2 put-object failed for {key}: {error}") from error


def mirror_artifacts(
    config: IndexConfig,
    collection: ReleaseCollection,
    downloader: ArtifactDownloader,
    store: ObjectStore,
    *,
    public_base_url: str,
    workers: int = _DEFAULT_MIRROR_WORKERS,
    log: Callable[[str], None] | None = None,
) -> ReleaseCollection:
    """Mirror every collected wheel and return publication-ready records."""
    if workers < 1:
        raise MirrorError("mirror workers must be a positive integer")
    logger = log or (lambda _message: None)
    base_url = _validate_public_base_url(public_base_url)
    mirrored: list[CollectedArtifact] = []
    repositories = []

    for artifact in collection.artifacts:
        repository = config.repository(artifact.repository)
        if repository is None:
            raise MirrorError(
                f"collection contains an unconfigured repository: {artifact.repository}"
            )
        repositories.append(repository)

    if collection.artifacts:
        head_workers = min(_MAX_HEAD_WORKERS, len(collection.artifacts))
        logger(
            f"checking existing mirror state: artifacts={len(collection.artifacts)}, "
            f"workers={head_workers}"
        )
    existing_metadata = _existing_metadata_for_artifacts(
        store,
        collection.artifacts,
    )

    total = len(collection.artifacts)
    for index, (artifact, existing) in enumerate(
        zip(collection.artifacts, existing_metadata, strict=True)
    ):
        logger(f"checking artifact {index + 1}/{total}: {artifact.filename}")
        if existing is not None:
            logger(f"already mirrored: {artifact.filename}")

    missing_indexes = [
        index for index, existing in enumerate(existing_metadata) if existing is None
    ]
    mirrored_metadata: dict[int, tuple[str, str | None]] = {}
    if missing_indexes:
        mirror_workers = min(workers, len(missing_indexes))
        logger(
            f"mirroring missing artifacts: artifacts={len(missing_indexes)}, "
            f"workers={mirror_workers}"
        )
        with ThreadPoolExecutor(
            max_workers=mirror_workers,
            thread_name_prefix="artifact-mirror",
        ) as executor:
            futures = {
                index: executor.submit(
                    _mirror_artifact,
                    collection.artifacts[index],
                    repositories[index],
                    downloader,
                    store,
                    logger,
                )
                for index in missing_indexes
            }
            mirrored_metadata = {
                index: future.result() for index, future in futures.items()
            }

    for index, (artifact, existing) in enumerate(
        zip(collection.artifacts, existing_metadata, strict=True)
    ):
        metadata_sha256, requires_python = (
            existing if existing is not None else mirrored_metadata[index]
        )
        mirrored.append(
            replace(
                artifact,
                published_url=artifact_url(base_url, artifact_key(artifact)),
                metadata_sha256=metadata_sha256,
                requires_python=requires_python,
            )
        )

    return collection_from_artifacts(mirrored)


def _mirror_artifact(
    artifact: CollectedArtifact,
    repository: RepositoryConfig,
    downloader: ArtifactDownloader,
    store: ObjectStore,
    logger: Callable[[str], None],
) -> tuple[str, str | None]:
    key = artifact_key(artifact)
    with tempfile.TemporaryDirectory(prefix="build-index-mirror-") as temporary:
        directory = Path(temporary)
        wheel = directory / "artifact.whl"
        logger(f"downloading source artifact: {artifact.filename}")
        sha256, size = downloader.download_asset(
            artifact.download_url,
            wheel,
            repository=artifact.repository,
            access=repository.access,
            log=logger,
        )
        if sha256 != artifact.sha256:
            raise MirrorError(
                f"downloaded wheel SHA-256 does not match collection: "
                f"{artifact.filename}"
            )
        if size != artifact.size:
            raise MirrorError(
                f"downloaded wheel size does not match collection: {artifact.filename}"
            )

        core_metadata = extract_core_metadata(
            wheel,
            artifact,
            allow_version_mismatch=(
                artifact.release in repository.allowed_metadata_version_mismatch_tags
            ),
        )
        if core_metadata.version_mismatch:
            logger(
                "tolerated configured wheel metadata Version mismatch: "
                f"{artifact.repository}@{artifact.release} ({artifact.filename})"
            )
        metadata_path = directory / "artifact.metadata"
        metadata_path.write_bytes(core_metadata.contents)

        store.put(
            key,
            wheel,
            content_type="application/octet-stream",
            metadata={
                "format-version": _OBJECT_FORMAT_VERSION,
                "sha256": artifact.sha256,
            },
        )
        sidecar_metadata = {
            "format-version": _OBJECT_FORMAT_VERSION,
            "sha256": core_metadata.sha256,
            "wheel-sha256": artifact.sha256,
        }
        if core_metadata.requires_python is not None:
            sidecar_metadata["requires-python"] = core_metadata.requires_python
        store.put(
            f"{key}.metadata",
            metadata_path,
            content_type="application/octet-stream",
            metadata=sidecar_metadata,
        )
    logger(f"mirrored artifact and metadata: {artifact.filename}")
    return core_metadata.sha256, core_metadata.requires_python


def artifact_key(artifact: CollectedArtifact) -> str:
    """Return the immutable object key for one wheel."""
    return f"artifacts/{artifact.sha256}/{artifact.filename}"


def artifact_url(public_base_url: str, key: str) -> str:
    """Return the public URL for one R2 object key."""
    return f"{public_base_url.rstrip('/')}/{quote(key, safe='/+-._~')}"


def extract_core_metadata(
    wheel: Path,
    artifact: CollectedArtifact,
    *,
    allow_version_mismatch: bool = False,
) -> CoreMetadata:
    """Extract and validate exact core metadata bytes from one wheel."""
    try:
        with zipfile.ZipFile(wheel) as archive:
            matches = [
                entry
                for entry in archive.infolist()
                if entry.filename.endswith(".dist-info/METADATA") and not entry.is_dir()
            ]
            if len(matches) != 1:
                raise MirrorError(
                    f"wheel must contain exactly one .dist-info/METADATA file: "
                    f"{artifact.filename}"
                )
            entry = matches[0]
            if entry.file_size > _MAX_METADATA_SIZE:
                raise MirrorError(
                    f"wheel core metadata exceeds {_MAX_METADATA_SIZE} bytes: "
                    f"{artifact.filename}"
                )
            with archive.open(entry) as metadata_file:
                contents = metadata_file.read(_MAX_METADATA_SIZE + 1)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise MirrorError(
            f"could not read wheel metadata: {artifact.filename}"
        ) from error
    if len(contents) > _MAX_METADATA_SIZE:
        raise MirrorError(
            f"wheel core metadata exceeds {_MAX_METADATA_SIZE} bytes: "
            f"{artifact.filename}"
        )

    message = BytesParser(policy=policy.compat32).parsebytes(contents)
    metadata_name = _single_header(message, "Name", artifact.filename)
    metadata_version = _single_header(message, "Version", artifact.filename)
    if canonicalize_name(metadata_name) != artifact.project:
        raise MirrorError(
            f"wheel metadata Name does not match filename: {artifact.filename}"
        )
    try:
        parsed_metadata_version = Version(metadata_version)
    except InvalidVersion as error:
        raise MirrorError(
            f"wheel metadata has invalid Version: {artifact.filename}"
        ) from error
    _distribution, filename_version = parse_collected_wheel_filename(artifact.filename)
    version_mismatch = not _metadata_version_matches_filename(
        parsed_metadata_version,
        filename_version,
    )
    if version_mismatch and not allow_version_mismatch:
        raise MirrorError(
            f"wheel metadata Version does not match filename: {artifact.filename}"
        )

    requires_python_values = message.get_all("Requires-Python", [])
    if len(requires_python_values) > 1:
        raise MirrorError(
            f"wheel metadata contains multiple Requires-Python fields: "
            f"{artifact.filename}"
        )
    requires_python = None
    if requires_python_values:
        try:
            requires_python = str(SpecifierSet(requires_python_values[0].strip()))
        except InvalidSpecifier as error:
            raise MirrorError(
                f"wheel metadata has invalid Requires-Python: {artifact.filename}"
            ) from error

    return CoreMetadata(
        contents=contents,
        sha256=hashlib.sha256(contents).hexdigest(),
        requires_python=requires_python,
        version_mismatch=version_mismatch,
    )


def _existing_metadata_for_artifacts(
    store: ObjectStore,
    artifacts: tuple[CollectedArtifact, ...],
) -> tuple[tuple[str, str | None] | None, ...]:
    if not artifacts:
        return ()
    workers = min(_MAX_HEAD_WORKERS, len(artifacts))
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="r2-head",
    ) as executor:
        futures = [
            executor.submit(
                _existing_metadata,
                store,
                artifact,
                artifact_key(artifact),
            )
            for artifact in artifacts
        ]
        return tuple(future.result() for future in futures)


def _existing_metadata(
    store: ObjectStore,
    artifact: CollectedArtifact,
    key: str,
) -> tuple[str, str | None] | None:
    wheel = store.head(key)
    if (
        wheel is None
        or wheel.size != artifact.size
        or wheel.metadata.get("format-version") != _OBJECT_FORMAT_VERSION
        or wheel.metadata.get("sha256") != artifact.sha256
    ):
        return None
    sidecar = store.head(f"{key}.metadata")
    if (
        sidecar is None
        or sidecar.metadata.get("format-version") != _OBJECT_FORMAT_VERSION
        or sidecar.metadata.get("wheel-sha256") != artifact.sha256
    ):
        return None
    metadata_sha256 = sidecar.metadata.get("sha256")
    if metadata_sha256 is None or not _is_sha256(metadata_sha256):
        return None
    requires_python = sidecar.metadata.get("requires-python")
    if requires_python is not None:
        try:
            normalized = str(SpecifierSet(requires_python))
        except InvalidSpecifier:
            return None
        if normalized != requires_python:
            return None
    return metadata_sha256, requires_python


def _single_header(message: Message, name: str, filename: str) -> str:
    values = message.get_all(name, [])
    if len(values) != 1 or not values[0].strip():
        raise MirrorError(
            f"wheel metadata must contain exactly one {name} field: {filename}"
        )
    return values[0].strip()


def _client_error_message(error: ClientError) -> str:
    details = error.response.get("Error", {})
    code = str(details.get("Code", "unknown"))
    message = str(details.get("Message", "unknown error"))
    return f"{code}: {message}"


def _metadata_version_matches_filename(
    metadata_version: Version,
    filename_version: Version,
) -> bool:
    if metadata_version.public != filename_version.public:
        return False
    if metadata_version.local is None:
        return True
    if filename_version.local is None:
        return False
    return (
        filename_version.local == metadata_version.local
        or filename_version.local.startswith(f"{metadata_version.local}.")
    )


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _validate_public_base_url(value: str) -> str:
    base_url = value.rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise MirrorError(
            "public artifact base URL must be an HTTPS URL without query or fragment"
        )
    return base_url
