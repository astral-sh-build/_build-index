"""Prune stale mirrored artifact objects from Cloudflare R2."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
import re
import tomllib
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from build_index.collection import CollectionError, ReleaseCollection
from build_index.mirror import artifact_key
from build_index.r2 import S3Client, create_s3_client

_ARTIFACT_PREFIX = "artifacts/"
_DELETE_BATCH_SIZE = 1000
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ArtifactPruneError(CollectionError):
    """Raised when stale artifacts cannot be pruned safely."""


@dataclass(frozen=True)
class RetainedArtifact:
    sha256: str
    filename: str
    reason: str | None = None


@dataclass(frozen=True)
class ArtifactPruneResult:
    desired: int
    retained: int
    existing: int
    stale: int
    deleted: int
    dry_run: bool


def prune_stale_artifacts(
    collection: ReleaseCollection,
    bucket: str,
    endpoint: str,
    *,
    delete: bool = False,
    retained_artifacts: Iterable[RetainedArtifact] = (),
    client: S3Client | None = None,
    log: Callable[[str], None] | None = None,
) -> ArtifactPruneResult:
    """Delete artifact objects that are not part of the current collection."""
    if not bucket:
        raise ArtifactPruneError("R2 bucket must not be empty")
    if not collection.artifacts:
        raise ArtifactPruneError("refusing to prune artifacts from an empty collection")
    try:
        s3 = client or create_s3_client(endpoint, max_pool_connections=1)
    except ValueError as error:
        raise ArtifactPruneError(str(error)) from error

    logger = log or (lambda _message: None)
    desired = _desired_artifact_keys(collection)
    retained = _retained_artifact_keys(retained_artifacts)
    desired |= retained
    existing = _list_artifact_keys(s3, bucket)
    stale = sorted(existing - desired)

    if delete:
        deleted = 0
        for batch in _batches(stale, _DELETE_BATCH_SIZE):
            _delete_artifacts(s3, bucket, batch)
            deleted += len(batch)
            for key in batch:
                logger(f"deleted stale s3://{bucket}/{key}")
    else:
        deleted = 0
        for key in stale:
            logger(f"would delete stale s3://{bucket}/{key}")

    return ArtifactPruneResult(
        desired=len(desired),
        retained=len(retained),
        existing=len(existing),
        stale=len(stale),
        deleted=deleted,
        dry_run=not delete,
    )


def load_retained_artifacts(path: Path) -> tuple[RetainedArtifact, ...]:
    """Load artifacts explicitly protected from R2 pruning."""
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ArtifactPruneError(f"could not load {path}: {error}") from error

    if not isinstance(document, dict):
        raise ArtifactPruneError("retained artifact config must be a TOML table")
    _expect_keys(document, {"schema_version", "artifact"}, "retained artifact config")
    if document.get("schema_version") != 1:
        raise ArtifactPruneError("retained artifact schema_version must be exactly 1")
    values = document.get("artifact", [])
    if not isinstance(values, list):
        raise ArtifactPruneError("retained artifact config.artifact must be a list")
    return tuple(
        _retained_artifact_from_dict(value, index) for index, value in enumerate(values)
    )


def _desired_artifact_keys(collection: ReleaseCollection) -> set[str]:
    keys = set()
    for artifact in collection.artifacts:
        key = artifact_key(artifact)
        keys.add(key)
        keys.add(f"{key}.metadata")
    return keys


def _retained_artifact_keys(artifacts: Iterable[RetainedArtifact]) -> set[str]:
    keys = set()
    for artifact in artifacts:
        key = f"artifacts/{artifact.sha256}/{artifact.filename}"
        keys.add(key)
        keys.add(f"{key}.metadata")
    return keys


def _list_artifact_keys(client: S3Client, bucket: str) -> set[str]:
    keys = set()
    continuation_token = None
    while True:
        arguments: dict[str, object] = {
            "Bucket": bucket,
            "Prefix": _ARTIFACT_PREFIX,
        }
        if continuation_token is not None:
            arguments["ContinuationToken"] = continuation_token
        try:
            response = client.list_objects_v2(**arguments)
        except ClientError as error:
            raise ArtifactPruneError(
                f"R2 list-objects-v2 failed: {_client_error_message(error)}"
            ) from error
        except BotoCoreError as error:
            raise ArtifactPruneError(f"R2 list-objects-v2 failed: {error}") from error

        contents = response.get("Contents", [])
        if not isinstance(contents, list):
            raise ArtifactPruneError("R2 list-objects-v2 returned invalid contents")
        for entry in contents:
            if not isinstance(entry, dict) or not isinstance(entry.get("Key"), str):
                raise ArtifactPruneError(
                    "R2 list-objects-v2 returned an invalid object"
                )
            keys.add(entry["Key"])

        truncated = response.get("IsTruncated", False)
        if truncated is False:
            return keys
        continuation_token = response.get("NextContinuationToken")
        if truncated is not True or not isinstance(continuation_token, str):
            raise ArtifactPruneError(
                "R2 list-objects-v2 returned invalid pagination data"
            )


def _delete_artifacts(
    client: S3Client,
    bucket: str,
    keys: tuple[str, ...],
) -> None:
    try:
        response = client.delete_objects(
            Bucket=bucket,
            Delete={
                "Objects": [{"Key": key} for key in keys],
                "Quiet": True,
            },
        )
    except ClientError as error:
        raise ArtifactPruneError(
            f"R2 delete-objects failed: {_client_error_message(error)}"
        ) from error
    except BotoCoreError as error:
        raise ArtifactPruneError(f"R2 delete-objects failed: {error}") from error
    errors = response.get("Errors", [])
    if errors:
        raise ArtifactPruneError(f"R2 delete-objects returned errors: {errors!r}")


def _batches(values: list[str], size: int) -> Iterable[tuple[str, ...]]:
    for index in range(0, len(values), size):
        yield tuple(values[index : index + size])


def _client_error_message(error: ClientError) -> str:
    details = error.response.get("Error", {})
    code = str(details.get("Code", "unknown"))
    message = str(details.get("Message", "unknown error"))
    return f"{code}: {message}"


def _retained_artifact_from_dict(value: Any, index: int) -> RetainedArtifact:
    context = f"retained artifact config.artifact[{index}]"
    if not isinstance(value, dict):
        raise ArtifactPruneError(f"{context} must be a table")
    _expect_keys(value, {"sha256", "filename", "reason"}, context)
    missing = sorted({"sha256", "filename"} - set(value))
    if missing:
        raise ArtifactPruneError(f"{context} is missing keys: {', '.join(missing)}")

    sha256 = value["sha256"]
    if not isinstance(sha256, str) or not _SHA256_PATTERN.fullmatch(sha256):
        raise ArtifactPruneError(f"{context}.sha256 must be a lowercase SHA-256")

    filename = value["filename"]
    if (
        not isinstance(filename, str)
        or not filename.endswith(".whl")
        or "/" in filename
        or "\\" in filename
        or filename == ".whl"
    ):
        raise ArtifactPruneError(f"{context}.filename must be a wheel filename")

    reason = value.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ArtifactPruneError(f"{context}.reason must be a string")
    return RetainedArtifact(sha256=sha256, filename=filename, reason=reason)


def _expect_keys(value: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ArtifactPruneError(
            f"{context} contains unknown keys: {', '.join(unknown)}"
        )
