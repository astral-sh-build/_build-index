import hashlib
import shutil
import threading
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from build_index.collection import CollectedArtifact, collection_from_artifacts
from build_index.config import load_config
from build_index import mirror as mirror_module
from build_index.mirror import (
    MirrorError,
    ObjectInfo,
    S3ObjectStore,
    artifact_key,
    extract_core_metadata,
    mirror_artifacts,
)

ROOT = Path(__file__).parents[1]
CONFIG = load_config(ROOT / "config" / "index.toml")
FILENAME = "grouped_gemm-0.1.0+cu128-py3-none-any.whl"


class FakeDownloader:
    def __init__(self, source: Path) -> None:
        self.source = source
        self.calls = 0

    def download_asset(
        self,
        asset_api_url: str,
        destination: Path,
        *,
        repository: str | None = None,
        access: str = "private",
        log=None,
    ) -> tuple[str, int]:
        self.calls += 1
        shutil.copyfile(self.source, destination)
        contents = destination.read_bytes()
        return hashlib.sha256(contents).hexdigest(), len(contents)


class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str], str]] = {}
        self.puts: list[str] = []

    def head(self, key: str) -> ObjectInfo | None:
        value = self.objects.get(key)
        if value is None:
            return None
        contents, metadata, _content_type = value
        return ObjectInfo(size=len(contents), metadata=metadata)

    def put(
        self,
        key: str,
        source: Path,
        *,
        content_type: str,
        metadata: dict[str, str],
    ) -> None:
        self.puts.append(key)
        self.objects[key] = (source.read_bytes(), metadata, content_type)


class ConcurrentHeadStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.maximum_active = 0
        self.overlap = threading.Event()
        self.lock = threading.Lock()

    def head(self, key: str) -> ObjectInfo | None:
        with self.lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            if self.active > 1:
                self.overlap.set()
        self.overlap.wait(timeout=1)
        try:
            return super().head(key)
        finally:
            with self.lock:
                self.active -= 1


class FailingHeadStore(FakeStore):
    def __init__(self, failure_key: str) -> None:
        super().__init__()
        self.failure_key = failure_key

    def head(self, key: str) -> ObjectInfo | None:
        if key == self.failure_key:
            raise MirrorError(f"failed to check {key}")
        return super().head(key)


class FakeS3Client:
    def __init__(self) -> None:
        self.head_response: dict[str, object] | Exception = {
            "ContentLength": 123,
            "Metadata": {"SHA256": "a" * 64},
        }
        self.head_calls: list[dict[str, object]] = []
        self.put_calls: list[dict[str, object]] = []

    def head_object(self, **kwargs: object) -> dict[str, object]:
        self.head_calls.append(kwargs)
        if isinstance(self.head_response, Exception):
            raise self.head_response
        return self.head_response

    def put_object(self, **kwargs: object) -> dict[str, object]:
        body = kwargs["Body"]
        assert hasattr(body, "read")
        self.put_calls.append({**kwargs, "Body": body.read()})
        return {}


def make_wheel(path: Path, metadata: bytes | None = None) -> CollectedArtifact:
    core_metadata = metadata or (
        b"Metadata-Version: 2.4\n"
        b"Name: grouped-gemm\n"
        b"Version: 0.1.0+cu128\n"
        b"Requires-Python: >=3.10, <3.14\n"
        b"\n"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "grouped_gemm-0.1.0+cu128.dist-info/METADATA",
            core_metadata,
        )
    contents = path.read_bytes()
    return CollectedArtifact(
        repository="astral-sh-build/build-grouped-gemm",
        release="0.1.0",
        filename=FILENAME,
        project="grouped-gemm",
        version="0.1.0+cu128",
        channel="cu128",
        source_url=f"https://github.com/example/releases/{FILENAME}",
        download_url="https://api.github.com/releases/assets/1",
        sha256=hashlib.sha256(contents).hexdigest(),
        size=len(contents),
        upload_time="2026-06-02T17:13:12Z",
    )


def test_mirror_publishes_wheel_and_exact_metadata_then_resumes(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / FILENAME
    artifact = make_wheel(wheel)
    collection = collection_from_artifacts([artifact])
    downloader = FakeDownloader(wheel)
    store = FakeStore()
    messages: list[str] = []

    mirrored = mirror_artifacts(
        CONFIG,
        collection,
        downloader,
        store,
        public_base_url="https://packages.example",
        log=messages.append,
    )

    result = mirrored.artifacts[0]
    key = artifact_key(artifact)
    assert downloader.calls == 1
    assert store.puts == [key, f"{key}.metadata"]
    assert store.objects[key][0] == wheel.read_bytes()
    assert store.objects[f"{key}.metadata"][0] == (
        b"Metadata-Version: 2.4\n"
        b"Name: grouped-gemm\n"
        b"Version: 0.1.0+cu128\n"
        b"Requires-Python: >=3.10, <3.14\n"
        b"\n"
    )
    assert result.published_url == f"https://packages.example/{key}"
    assert (
        result.metadata_sha256
        == hashlib.sha256(store.objects[f"{key}.metadata"][0]).hexdigest()
    )
    assert result.requires_python == "<3.14,>=3.10"
    assert messages[:2] == [
        "checking existing mirror state: artifacts=1, workers=1",
        f"checking artifact 1/1: {FILENAME}",
    ]

    repeated = mirror_artifacts(
        CONFIG,
        collection,
        downloader,
        store,
        public_base_url="https://packages.example",
    )

    assert repeated == mirrored
    assert downloader.calls == 1
    assert store.puts == [key, f"{key}.metadata"]


def test_mirror_checks_existing_artifacts_concurrently_and_preserves_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = tmp_path / FILENAME
    base = make_wheel(wheel)
    artifacts = tuple(
        replace(
            base,
            release=f"0.1.{index}",
            filename=f"grouped_gemm-0.1.{index}+cu128-py3-none-any.whl",
            version=f"0.1.{index}+cu128",
            download_url=f"https://api.github.com/releases/assets/{index}",
            sha256=f"{index + 1:064x}",
            size=1,
        )
        for index in range(4)
    )
    collection = collection_from_artifacts(artifacts)
    store = ConcurrentHeadStore()
    metadata = b"metadata"
    metadata_sha256 = hashlib.sha256(metadata).hexdigest()
    for artifact in artifacts:
        key = artifact_key(artifact)
        store.objects[key] = (
            b"x",
            {
                "format-version": "1",
                "sha256": artifact.sha256,
            },
            "application/octet-stream",
        )
        store.objects[f"{key}.metadata"] = (
            metadata,
            {
                "format-version": "1",
                "sha256": metadata_sha256,
                "wheel-sha256": artifact.sha256,
            },
            "application/octet-stream",
        )
    monkeypatch.setattr(mirror_module, "_MAX_HEAD_WORKERS", 2)
    messages: list[str] = []

    mirrored = mirror_artifacts(
        CONFIG,
        collection,
        FakeDownloader(wheel),
        store,
        public_base_url="https://packages.example",
        log=messages.append,
    )

    assert store.overlap.is_set()
    assert store.maximum_active == 2
    assert store.puts == []
    assert [artifact.filename for artifact in mirrored.artifacts] == [
        artifact.filename for artifact in collection.artifacts
    ]
    assert messages[0] == "checking existing mirror state: artifacts=4, workers=2"
    assert messages[1::2] == [
        f"checking artifact {index + 1}/4: {artifact.filename}"
        for index, artifact in enumerate(mirrored.artifacts)
    ]
    assert messages[2::2] == [
        f"already mirrored: {artifact.filename}" for artifact in mirrored.artifacts
    ]


def test_mirror_finishes_parallel_preflight_before_writes(tmp_path: Path) -> None:
    wheel = tmp_path / FILENAME
    base = make_wheel(wheel)
    artifacts = tuple(
        replace(
            base,
            release=f"0.1.{index}",
            filename=f"grouped_gemm-0.1.{index}+cu128-py3-none-any.whl",
            version=f"0.1.{index}+cu128",
            download_url=f"https://api.github.com/releases/assets/{index}",
            sha256=f"{index + 1:064x}",
        )
        for index in range(2)
    )
    downloader = FakeDownloader(wheel)
    store = FailingHeadStore(artifact_key(artifacts[1]))

    with pytest.raises(MirrorError, match="failed to check"):
        mirror_artifacts(
            CONFIG,
            collection_from_artifacts(artifacts),
            downloader,
            store,
            public_base_url="https://packages.example",
        )

    assert downloader.calls == 0
    assert store.puts == []


def test_mirror_rejects_wheel_with_wrong_digest(tmp_path: Path) -> None:
    wheel = tmp_path / FILENAME
    artifact = make_wheel(wheel)
    artifact = CollectedArtifact(**{**artifact.__dict__, "sha256": "a" * 64})

    with pytest.raises(MirrorError, match="SHA-256 does not match"):
        mirror_artifacts(
            CONFIG,
            collection_from_artifacts([artifact]),
            FakeDownloader(wheel),
            FakeStore(),
            public_base_url="https://packages.example",
        )


def test_extract_core_metadata_validates_name_and_version(tmp_path: Path) -> None:
    wheel = tmp_path / FILENAME
    artifact = make_wheel(
        wheel,
        b"Metadata-Version: 2.4\nName: other-project\nVersion: 0.1.0+cu128\n\n",
    )

    with pytest.raises(MirrorError, match="Name does not match"):
        extract_core_metadata(wheel, artifact)


def test_extract_core_metadata_allows_omitted_local_version(tmp_path: Path) -> None:
    wheel = tmp_path / FILENAME
    artifact = make_wheel(
        wheel,
        b"Metadata-Version: 2.4\nName: grouped-gemm\nVersion: 0.1.0\n\n",
    )

    assert extract_core_metadata(wheel, artifact).requires_python is None


def test_extract_core_metadata_allows_local_version_prefix(tmp_path: Path) -> None:
    wheel = tmp_path / FILENAME
    artifact = make_wheel(
        wheel,
        b"Metadata-Version: 2.4\nName: grouped-gemm\nVersion: 0.1.0+build1\n\n",
    )
    artifact = replace(
        artifact,
        filename="grouped_gemm-0.1.0+build1.cu128-py3-none-any.whl",
        version="0.1.0+build1.cu128",
    )

    assert extract_core_metadata(wheel, artifact).requires_python is None


def test_extract_core_metadata_rejects_unrelated_local_version(tmp_path: Path) -> None:
    wheel = tmp_path / FILENAME
    artifact = make_wheel(
        wheel,
        b"Metadata-Version: 2.4\nName: grouped-gemm\nVersion: 0.1.0+cu129\n\n",
    )

    with pytest.raises(MirrorError, match="Version does not match"):
        extract_core_metadata(wheel, artifact)


def test_s3_object_store_reads_resume_metadata_and_sets_immutable_headers(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    store = S3ObjectStore(
        "index",
        "https://example.r2.cloudflarestorage.com",
        client=client,
    )

    assert store.head("artifacts/example") == ObjectInfo(
        size=123,
        metadata={"sha256": "a" * 64},
    )
    assert client.head_calls == [
        {
            "Bucket": "index",
            "Key": "artifacts/example",
        }
    ]
    source = tmp_path / "wheel"
    source.write_bytes(b"wheel")
    store.put(
        "artifacts/example",
        source,
        content_type="application/octet-stream",
        metadata={"sha256": "a" * 64},
    )

    assert client.put_calls == [
        {
            "Bucket": "index",
            "Key": "artifacts/example",
            "Body": b"wheel",
            "ContentType": "application/octet-stream",
            "CacheControl": "public, max-age=31536000, immutable",
            "Metadata": {"sha256": "a" * 64},
        }
    ]


def test_s3_object_store_handles_missing_and_failed_heads() -> None:
    client = FakeS3Client()
    store = S3ObjectStore(
        "index",
        "https://example.r2.cloudflarestorage.com",
        client=client,
    )
    client.head_response = ClientError(
        {
            "Error": {"Code": "404", "Message": "Not Found"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        },
        "HeadObject",
    )

    assert store.head("artifacts/missing") is None

    client.head_response = ClientError(
        {
            "Error": {"Code": "AccessDenied", "Message": "Access denied"},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        },
        "HeadObject",
    )
    with pytest.raises(MirrorError, match="AccessDenied: Access denied"):
        store.head("artifacts/private")
