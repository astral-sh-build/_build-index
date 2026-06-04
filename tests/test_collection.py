from dataclasses import replace
from pathlib import Path

import pytest

from build_index.collection import (
    CollectedArtifact,
    CollectionError,
    collection_from_artifacts,
    load_collection,
    normalize_wheel_filename,
    release_revision,
    write_collection,
)


def artifact(**overrides: object) -> CollectedArtifact:
    values = {
        "repository": "ee-test-builds/build-index-test-gpu",
        "release": "0.1.0",
        "filename": "index_test_gpu-0.1.0+cu128-py3-none-any.whl",
        "project": "index-test-gpu",
        "version": "0.1.0+cu128",
        "channel": "cu128",
        "url": (
            "https://github.com/ee-test-builds/build-index-test-gpu/releases/"
            "download/0.1.0/index_test_gpu-0.1.0%2Bcu128-py3-none-any.whl"
        ),
        "sha256": "a" * 64,
        "size": 123,
        "upload_time": "2026-06-02T17:13:12Z",
    }
    values.update(overrides)
    return CollectedArtifact(**values)


def test_collection_round_trip_is_deterministic(tmp_path: Path) -> None:
    second = artifact(
        repository="ee-test-builds/build-index-test-cpu",
        filename="index_test_cpu-0.1.0+cpu-py3-none-any.whl",
        project="index-test-cpu",
        version="0.1.0+cpu",
        channel="cpu",
        url=(
            "https://github.com/ee-test-builds/build-index-test-cpu/releases/"
            "download/0.1.0/index_test_cpu-0.1.0%2Bcpu-py3-none-any.whl"
        ),
        sha256="b" * 64,
    )
    collection = collection_from_artifacts([artifact(), second])
    path = write_collection(tmp_path / "releases.json", collection)

    assert load_collection(path) == collection
    assert [item.channel for item in collection.artifacts] == ["cpu", "cu128"]


def test_collection_rejects_duplicate_index_filename() -> None:
    original = artifact()
    duplicate = replace(
        original,
        repository="ee-test-builds/build-index-test-mixed",
        release="replacement",
    )

    with pytest.raises(CollectionError, match="duplicate wheel filename"):
        collection_from_artifacts([original, duplicate])


def test_collection_uses_later_republication_from_same_repository() -> None:
    original = artifact(upload_time="2026-06-02T17:13:12Z")
    replacement = replace(
        original,
        release="replacement",
        sha256="b" * 64,
        upload_time="2026-06-03T17:13:12Z",
    )

    collection = collection_from_artifacts([replacement, original])

    assert collection.artifacts == (replacement,)


def test_collection_rejects_ambiguous_same_time_republication() -> None:
    original = artifact()
    replacement = replace(original, release="replacement", sha256="b" * 64)

    with pytest.raises(CollectionError, match="same upload time"):
        collection_from_artifacts([original, replacement])


def test_collection_rejects_project_that_disagrees_with_wheel() -> None:
    with pytest.raises(CollectionError, match="project does not match"):
        collection_from_artifacts([artifact(project="other")])


def test_collection_rejects_invalid_hash() -> None:
    with pytest.raises(CollectionError, match="invalid SHA-256"):
        collection_from_artifacts([artifact(sha256="not-a-hash")])


def test_collection_rejects_non_https_url() -> None:
    with pytest.raises(CollectionError, match="invalid URL"):
        collection_from_artifacts([artifact(url="http://example.com/wheel.whl")])


@pytest.mark.parametrize(
    "upload_time",
    [
        "2026-06-02T17:13:12+00:00",
        "2026-06-02T17:13:12.1234567Z",
        "2026-06-31T17:13:12Z",
    ],
)
def test_collection_rejects_invalid_upload_time(upload_time: str) -> None:
    with pytest.raises(CollectionError, match="invalid upload time"):
        collection_from_artifacts([artifact(upload_time=upload_time)])


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("1.0.0", ("1.0.0", 0)),
        ("1.0.0-r1", ("1.0.0", 1)),
        ("release-r12", ("release", 12)),
        ("release-r0", ("release-r0", 0)),
    ],
)
def test_release_revision(tag: str, expected: tuple[str, int]) -> None:
    assert release_revision(tag) == expected


def test_normalize_wheel_filename_repairs_repeated_local_separators() -> None:
    source = (
        "deep_ep-1.2.1+1300811+cu12.8torch2.10.0cxx11abiTRUE-"
        "cp314-cp314-manylinux_2_27_x86_64.whl"
    )

    assert normalize_wheel_filename(source) == (
        "deep_ep-1.2.1+1300811.cu12.8torch2.10.0cxx11abiTRUE-"
        "cp314-cp314-manylinux_2_27_x86_64.whl"
    )
