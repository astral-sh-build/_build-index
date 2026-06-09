import hashlib
import json
import shutil
import subprocess
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

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
    assert messages[0] == f"checking artifact 1/1: {FILENAME}"

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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(arguments: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        if arguments[2] == "head-object":
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout=json.dumps(
                    {
                        "ContentLength": 123,
                        "Metadata": {"SHA256": "a" * 64},
                    }
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(arguments, 0, stdout="{}", stderr="")

    monkeypatch.setattr(mirror_module.subprocess, "run", run)
    store = S3ObjectStore(
        "index",
        "https://example.r2.cloudflarestorage.com",
        aws_cli="aws",
    )

    assert store.head("artifacts/example") == ObjectInfo(
        size=123,
        metadata={"sha256": "a" * 64},
    )
    source = tmp_path / "wheel"
    source.write_bytes(b"wheel")
    store.put(
        "artifacts/example",
        source,
        content_type="application/octet-stream",
        metadata={"sha256": "a" * 64},
    )

    put = calls[1]
    assert put[put.index("--cache-control") + 1] == (
        "public, max-age=31536000, immutable"
    )
    assert json.loads(put[put.index("--metadata") + 1]) == {"sha256": "a" * 64}
