import json
from dataclasses import replace
from pathlib import Path

import pytest

from build_index.collection import (
    CollectedArtifact,
    CollectionError,
    collection_from_artifacts,
)
from build_index.config import load_config
from build_index.index_tree import build_index_tree

ROOT = Path(__file__).parents[1]
CONFIG = load_config(ROOT / "tests" / "fixtures" / "index.toml")


def artifact(
    repository: str,
    filename: str,
    project: str,
    version: str,
    channel: str,
    *,
    sha256: str,
    size: int,
    requires_python: str | None = None,
) -> CollectedArtifact:
    return CollectedArtifact(
        repository=repository,
        release="0.1.0",
        filename=filename,
        project=project,
        version=version,
        channel=channel,
        source_url=(
            f"https://github.com/{repository}/releases/download/0.1.0/"
            f"{filename.replace('+', '%2B')}"
        ),
        download_url="https://api.github.com/releases/assets/1",
        sha256=sha256,
        size=size,
        upload_time="2026-06-02T17:13:12Z",
        published_url=f"https://packages.example/artifacts/{sha256}/{filename}",
        metadata_sha256="e" * 64,
        requires_python=requires_python,
    )


def example_collection():
    return collection_from_artifacts(
        [
            artifact(
                "example/build-index-test-cpu",
                "index_test_cpu-0.1.0+cpu-py3-none-any.whl",
                "index-test-cpu",
                "0.1.0+cpu",
                "cpu",
                sha256="a" * 64,
                size=5001,
            ),
            artifact(
                "example/build-index-test-gpu",
                "index_test_gpu-0.1.0+cu128-py3-none-any.whl",
                "index-test-gpu",
                "0.1.0+cu128",
                "cu128",
                sha256="b" * 64,
                size=5002,
                requires_python=">=3.10",
            ),
            artifact(
                "example/build-index-test-mixed",
                "index_test_mixed-0.1.0+cpu-py3-none-any.whl",
                "index-test-mixed",
                "0.1.0+cpu",
                "cpu",
                sha256="c" * 64,
                size=5003,
            ),
            artifact(
                "example/build-index-test-mixed",
                "index_test_mixed-0.1.0+cu128-py3-none-any.whl",
                "index-test-mixed",
                "0.1.0+cu128",
                "cu128",
                sha256="d" * 64,
                size=5004,
            ),
        ]
    )


def test_build_index_tree_generates_simple_api_documents(tmp_path: Path) -> None:
    output = tmp_path / "dist"

    build_index_tree(
        CONFIG,
        output,
        collection=example_collection(),
    )

    root = json.loads((output / "simple" / "cu128" / "index.json").read_text())
    project = json.loads(
        (output / "simple" / "cu128" / "index-test-gpu" / "index.json").read_text()
    )
    explicit_project = json.loads(
        (
            output / "simple" / "v1+json" / "cu128" / "index-test-gpu" / "index.json"
        ).read_text()
    )
    project_html = (
        output / "simple" / "v1+html" / "cu128" / "index-test-gpu" / "index.html"
    ).read_text()
    root_html = (output / "simple" / "v1+html" / "cu128" / "index.html").read_text()

    assert root == {
        "meta": {"api-version": "1.4"},
        "projects": [
            {"name": "index-test-gpu"},
            {"name": "index-test-mixed"},
        ],
    }
    assert project == explicit_project
    assert project["meta"] == {"api-version": "1.4"}
    assert project["name"] == "index-test-gpu"
    assert project["versions"] == ["0.1.0+cu128"]
    assert project["files"] == [
        {
            "core-metadata": {"sha256": "e" * 64},
            "filename": "index_test_gpu-0.1.0+cu128-py3-none-any.whl",
            "hashes": {"sha256": "b" * 64},
            "requires-python": ">=3.10",
            "size": 5002,
            "upload-time": "2026-06-02T17:13:12Z",
            "url": (
                "https://packages.example/artifacts/"
                + "b" * 64
                + "/index_test_gpu-0.1.0+cu128-py3-none-any.whl"
            ),
        }
    ]
    assert 'content="1.4"' in root_html
    assert 'content="1.4"' in project_html
    assert "#sha256=" + "b" * 64 in project_html
    assert 'data-core-metadata="sha256=' + "e" * 64 + '"' in project_html
    assert 'data-requires-python="&gt;=3.10"' in project_html
    assert not (output / "catalog").exists()
    assert not (output / "artifacts").exists()
    assert not (output / "index.html").exists()


def test_build_index_tree_generates_empty_channel_documents(tmp_path: Path) -> None:
    output = tmp_path / "dist"

    build_index_tree(CONFIG, output)

    root = json.loads((output / "simple" / "cu128" / "index.json").read_text())
    assert root == {"meta": {"api-version": "1.4"}, "projects": []}


def test_build_index_tree_rejects_unconfigured_repository(tmp_path: Path) -> None:
    collection = collection_from_artifacts(
        [
            artifact(
                "example/unconfigured",
                "index_test_gpu-0.1.0+cu128-py3-none-any.whl",
                "index-test-gpu",
                "0.1.0+cu128",
                "cu128",
                sha256="a" * 64,
                size=1,
            )
        ]
    )

    with pytest.raises(CollectionError, match="unconfigured repository"):
        build_index_tree(CONFIG, tmp_path / "dist", collection=collection)


def test_build_index_tree_rejects_unmirrored_artifact(tmp_path: Path) -> None:
    collection = example_collection()
    unmirrored = collection_from_artifacts(
        [
            replace(
                artifact,
                published_url=None,
                metadata_sha256=None,
                requires_python=None,
            )
            if index == 0
            else artifact
            for index, artifact in enumerate(collection.artifacts)
        ]
    )

    with pytest.raises(CollectionError, match="unmirrored artifact"):
        build_index_tree(CONFIG, tmp_path / "dist", collection=unmirrored)


def test_build_index_tree_replaces_output_tree(tmp_path: Path) -> None:
    output = tmp_path / "dist"
    stale = output / "stale.txt"
    stale.parent.mkdir()
    stale.write_text("stale")

    build_index_tree(CONFIG, output, collection=example_collection())

    assert not stale.exists()


def test_repeated_index_tree_builds_are_byte_identical(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    build_index_tree(CONFIG, first, collection=example_collection())
    build_index_tree(CONFIG, second, collection=example_collection())

    first_files = {
        path.relative_to(first): path.read_bytes()
        for path in first.rglob("*")
        if path.is_file()
    }
    second_files = {
        path.relative_to(second): path.read_bytes()
        for path in second.rglob("*")
        if path.is_file()
    }
    assert first_files == second_files
