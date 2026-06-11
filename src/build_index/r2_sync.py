"""Publish generated package index documents to Cloudflare R2."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from build_index.collection import CollectionError
from build_index.r2 import S3Client, create_s3_client

_CACHE_CONTROL = "public, max-age=60, stale-while-revalidate=300"
_DEFAULT_UPLOAD_WORKERS = 16
_DELETE_BATCH_SIZE = 1000
_SIMPLE_PREFIX = "simple/"


class DocumentSyncError(CollectionError):
    """Raised when generated Simple API documents cannot be published safely."""


@dataclass(frozen=True)
class IndexDocument:
    stage: int
    key: str
    source: Path
    content_type: str


def sync_index_documents(
    output: Path,
    bucket: str,
    endpoint: str,
    *,
    upload_workers: int = _DEFAULT_UPLOAD_WORKERS,
    client: S3Client | None = None,
    log: Callable[[str], None] | None = None,
) -> None:
    """Publish generated index documents and delete stale Simple API entries."""
    if not bucket:
        raise DocumentSyncError("R2 bucket must not be empty")
    if upload_workers < 1:
        raise DocumentSyncError("R2 upload workers must be a positive integer")
    try:
        s3 = client or create_s3_client(
            endpoint,
            max_pool_connections=upload_workers,
        )
    except ValueError as error:
        raise DocumentSyncError(str(error)) from error

    logger = log or (lambda _message: None)
    documents = _index_documents(output)
    desired_simple = {
        document.key
        for document in documents
        if document.key.startswith(_SIMPLE_PREFIX)
    }

    for stage in (3, 2, 1):
        staged = tuple(document for document in documents if document.stage == stage)
        if not staged:
            continue
        workers = min(upload_workers, len(staged))
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="r2-document-upload",
        ) as executor:
            results = executor.map(
                lambda document: _upload_document(s3, bucket, document),
                staged,
            )
            for key in results:
                logger(f"uploaded s3://{bucket}/{key}")

    existing = _list_simple_keys(s3, bucket)
    stale = sorted(existing - desired_simple)
    for batch in _batches(stale, _DELETE_BATCH_SIZE):
        _delete_documents(s3, bucket, batch)
        for key in batch:
            logger(f"deleted stale s3://{bucket}/{key}")


def _index_documents(output: Path) -> tuple[IndexDocument, ...]:
    root = output / "index.html"
    if root.is_symlink() or not root.is_file():
        raise DocumentSyncError(f"missing generated index page: {root}")
    simple_root = output / "simple"
    if simple_root.is_symlink():
        raise DocumentSyncError(
            f"refusing to publish symlinked Simple API tree: {simple_root}"
        )
    if not simple_root.is_dir():
        raise DocumentSyncError(f"missing generated Simple API tree: {simple_root}")

    documents = [
        IndexDocument(
            stage=1,
            key="index.html",
            source=root,
            content_type="text/html; charset=utf-8",
        )
    ]
    keys = set()
    for path in sorted(simple_root.rglob("*")):
        if path.is_symlink():
            raise DocumentSyncError(
                f"refusing to publish symlink from {simple_root}: {path}"
            )
        if not path.is_file():
            continue
        if path.name == "index.json":
            content_type = "application/vnd.pypi.simple.v1+json"
        elif path.name == "index.html":
            content_type = "application/vnd.pypi.simple.v1+html"
        else:
            raise DocumentSyncError(f"unexpected generated Simple API file: {path}")
        relative = path.relative_to(output)
        key = relative.parent.as_posix() + "/"
        if key in keys:
            raise DocumentSyncError(
                f"multiple Simple API files map to the same R2 key: {key}"
            )
        keys.add(key)
        documents.append(
            IndexDocument(
                stage=_document_stage(key),
                key=key,
                source=path,
                content_type=content_type,
            )
        )

    if len(documents) == 1:
        raise DocumentSyncError(
            f"refusing to publish an empty Simple API tree: {simple_root}"
        )
    return tuple(sorted(documents, key=lambda document: document.key))


def _document_stage(key: str) -> int:
    parts = key.rstrip("/").split("/")
    if parts == ["simple"]:
        return 1
    if len(parts) in {2, 3} and parts[1] in {"v1+json", "v1+html"}:
        return 2
    if len(parts) == 2:
        return 2
    return 3


def _upload_document(
    client: S3Client,
    bucket: str,
    document: IndexDocument,
) -> str:
    try:
        with document.source.open("rb") as body:
            client.put_object(
                Bucket=bucket,
                Key=document.key,
                Body=body,
                ContentType=document.content_type,
                CacheControl=_CACHE_CONTROL,
            )
    except ClientError as error:
        raise DocumentSyncError(
            f"R2 put-object failed for {document.key}: {_client_error_message(error)}"
        ) from error
    except (BotoCoreError, OSError) as error:
        raise DocumentSyncError(
            f"R2 put-object failed for {document.key}: {error}"
        ) from error
    return document.key


def _list_simple_keys(client: S3Client, bucket: str) -> set[str]:
    keys = set()
    continuation_token = None
    while True:
        arguments: dict[str, object] = {
            "Bucket": bucket,
            "Prefix": _SIMPLE_PREFIX,
        }
        if continuation_token is not None:
            arguments["ContinuationToken"] = continuation_token
        try:
            response = client.list_objects_v2(**arguments)
        except ClientError as error:
            raise DocumentSyncError(
                f"R2 list-objects-v2 failed: {_client_error_message(error)}"
            ) from error
        except BotoCoreError as error:
            raise DocumentSyncError(f"R2 list-objects-v2 failed: {error}") from error

        contents = response.get("Contents", [])
        if not isinstance(contents, list):
            raise DocumentSyncError("R2 list-objects-v2 returned invalid contents")
        for entry in contents:
            if not isinstance(entry, dict) or not isinstance(entry.get("Key"), str):
                raise DocumentSyncError("R2 list-objects-v2 returned an invalid object")
            keys.add(entry["Key"])

        truncated = response.get("IsTruncated", False)
        if truncated is False:
            return keys
        continuation_token = response.get("NextContinuationToken")
        if truncated is not True or not isinstance(continuation_token, str):
            raise DocumentSyncError(
                "R2 list-objects-v2 returned invalid pagination data"
            )


def _delete_documents(
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
        raise DocumentSyncError(
            f"R2 delete-objects failed: {_client_error_message(error)}"
        ) from error
    except BotoCoreError as error:
        raise DocumentSyncError(f"R2 delete-objects failed: {error}") from error
    errors = response.get("Errors", [])
    if errors:
        raise DocumentSyncError(f"R2 delete-objects returned errors: {errors!r}")


def _batches(values: list[str], size: int) -> Iterable[tuple[str, ...]]:
    for index in range(0, len(values), size):
        yield tuple(values[index : index + size])


def _client_error_message(error: ClientError) -> str:
    details = error.response.get("Error", {})
    code = str(details.get("Code", "unknown"))
    message = str(details.get("Message", "unknown error"))
    return f"{code}: {message}"
