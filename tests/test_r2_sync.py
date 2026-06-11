import threading
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from build_index.r2_sync import DocumentSyncError, sync_index_documents


class FakeS3Client:
    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing = existing or set()
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.lock = threading.Lock()
        self.pages: list[dict[str, object]] | None = None
        self.failed_key: str | None = None

    def put_object(self, **kwargs: object) -> dict[str, object]:
        key = kwargs["Key"]
        assert isinstance(key, str)
        if key == self.failed_key:
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}},
                "PutObject",
            )
        body = kwargs["Body"]
        assert hasattr(body, "read")
        call = {**kwargs, "Body": body.read()}
        with self.lock:
            self.calls.append(("put", call))
        return {}

    def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
        with self.lock:
            self.calls.append(("list", kwargs))
        if self.pages is not None:
            return self.pages.pop(0)
        return {
            "Contents": [{"Key": key} for key in sorted(self.existing)],
            "IsTruncated": False,
        }

    def delete_objects(self, **kwargs: object) -> dict[str, object]:
        with self.lock:
            self.calls.append(("delete", kwargs))
        return {}


def test_sync_r2_uses_canonical_keys_and_deletes_stale_objects(
    tmp_path: Path,
) -> None:
    output = tmp_path / "dist"
    documents = {
        "index.html": "<main></main>",
        "simple/index.html": "<html></html>",
        "simple/cu128/index.json": "{}",
        "simple/cu128/vllm/index.json": "{}",
        "simple/v1+html/cu128/vllm/index.html": "<html></html>",
        "simple/v1+json/cu128/vllm/index.json": "{}",
    }
    for relative, contents in documents.items():
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)

    client = FakeS3Client({"simple/cu128/", "simple/stale/"})
    messages: list[str] = []
    sync_index_documents(
        output,
        "index",
        "https://example.r2.cloudflarestorage.com",
        upload_workers=2,
        client=client,
        log=messages.append,
    )

    puts = [call for operation, call in client.calls if operation == "put"]
    assert {call["Key"] for call in puts} == {
        "index.html",
        "simple/v1+html/cu128/vllm/",
        "simple/v1+json/cu128/vllm/",
        "simple/cu128/vllm/",
        "simple/cu128/",
        "simple/",
    }
    call_indexes = {
        call["Key"]: index
        for index, (operation, call) in enumerate(client.calls)
        if operation == "put"
    }
    project_keys = {
        "simple/v1+html/cu128/vllm/",
        "simple/v1+json/cu128/vllm/",
        "simple/cu128/vllm/",
    }
    root_keys = {"index.html", "simple/cu128/", "simple/"}
    assert max(call_indexes[key] for key in project_keys) < min(
        call_indexes[key] for key in root_keys
    )
    assert {call["ContentType"] for call in puts} == {
        "application/vnd.pypi.simple.v1+html",
        "application/vnd.pypi.simple.v1+json",
        "text/html; charset=utf-8",
    }
    assert {call["CacheControl"] for call in puts} == {
        "public, max-age=60, stale-while-revalidate=300"
    }
    assert all(call["Bucket"] == "index" for call in puts)
    assert {call["Body"] for call in puts} == {
        b"{}",
        b"<html></html>",
        b"<main></main>",
    }

    listing = next(call for operation, call in client.calls if operation == "list")
    assert listing == {"Bucket": "index", "Prefix": "simple/"}
    deletion = next(call for operation, call in client.calls if operation == "delete")
    assert deletion == {
        "Bucket": "index",
        "Delete": {
            "Objects": [{"Key": "simple/stale/"}],
            "Quiet": True,
        },
    }
    assert messages[-1] == "deleted stale s3://index/simple/stale/"


def test_sync_r2_paginates_before_deleting_stale_objects(tmp_path: Path) -> None:
    output = tmp_path / "dist"
    root = output / "index.html"
    root.parent.mkdir(parents=True)
    root.write_text("<main></main>")
    document = output / "simple" / "index.json"
    document.parent.mkdir()
    document.write_text("{}")
    client = FakeS3Client()
    client.pages = [
        {
            "Contents": [{"Key": "simple/"}],
            "IsTruncated": True,
            "NextContinuationToken": "next",
        },
        {
            "Contents": [{"Key": "simple/stale/"}],
            "IsTruncated": False,
        },
    ]

    sync_index_documents(
        output,
        "index",
        "https://example.r2.cloudflarestorage.com",
        client=client,
    )

    listings = [call for operation, call in client.calls if operation == "list"]
    assert listings == [
        {"Bucket": "index", "Prefix": "simple/"},
        {
            "Bucket": "index",
            "Prefix": "simple/",
            "ContinuationToken": "next",
        },
    ]
    deletion = next(call for operation, call in client.calls if operation == "delete")
    assert deletion["Delete"] == {
        "Objects": [{"Key": "simple/stale/"}],
        "Quiet": True,
    }


def test_sync_r2_does_not_publish_roots_or_delete_after_upload_failure(
    tmp_path: Path,
) -> None:
    output = tmp_path / "dist"
    root = output / "index.html"
    root.parent.mkdir(parents=True)
    root.write_text("<main></main>")
    for relative in (
        "simple/index.json",
        "simple/cu128/index.json",
        "simple/cu128/vllm/index.json",
    ):
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    client = FakeS3Client({"simple/stale/"})
    client.failed_key = "simple/cu128/vllm/"

    with pytest.raises(DocumentSyncError, match="AccessDenied: denied"):
        sync_index_documents(
            output,
            "index",
            "https://example.r2.cloudflarestorage.com",
            client=client,
        )

    assert client.calls == []


def test_sync_r2_rejects_unexpected_files_and_duplicate_keys(
    tmp_path: Path,
) -> None:
    output = tmp_path / "dist"
    root = output / "index.html"
    root.parent.mkdir(parents=True)
    root.write_text("<main></main>")
    simple = output / "simple"
    simple.mkdir()
    (simple / "README").write_text("unexpected")

    with pytest.raises(DocumentSyncError, match="unexpected generated"):
        sync_index_documents(
            output,
            "index",
            "https://example.r2.cloudflarestorage.com",
            client=FakeS3Client(),
        )

    (simple / "README").unlink()
    (simple / "index.json").write_text("{}")
    (simple / "index.html").write_text("<html></html>")
    with pytest.raises(DocumentSyncError, match="same R2 key"):
        sync_index_documents(
            output,
            "index",
            "https://example.r2.cloudflarestorage.com",
            client=FakeS3Client(),
        )


def test_sync_r2_rejects_a_symlinked_simple_tree(tmp_path: Path) -> None:
    output = tmp_path / "dist"
    root = output / "index.html"
    root.parent.mkdir(parents=True)
    root.write_text("<main></main>")
    target = tmp_path / "simple"
    target.mkdir()
    (target / "index.json").write_text("{}")
    (output / "simple").symlink_to(target, target_is_directory=True)

    with pytest.raises(DocumentSyncError, match="symlinked Simple API tree"):
        sync_index_documents(
            output,
            "index",
            "https://example.r2.cloudflarestorage.com",
            client=FakeS3Client(),
        )
