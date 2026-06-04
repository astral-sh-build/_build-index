import json
from pathlib import Path

import pytest

from build_index.collection import (
    CollectedArtifact,
    CollectionError,
    collection_from_artifacts,
)
from build_index.config import load_config
from build_index.pages import build_pages

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
) -> CollectedArtifact:
    return CollectedArtifact(
        repository=repository,
        release="0.1.0",
        filename=filename,
        project=project,
        version=version,
        channel=channel,
        url=(
            f"https://github.com/{repository}/releases/download/0.1.0/"
            f"{filename.replace('+', '%2B')}"
        ),
        sha256=sha256,
        size=size,
        upload_time="2026-06-02T17:13:12Z",
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


def test_build_pages_generates_only_landing_and_simple_api(tmp_path: Path) -> None:
    output = tmp_path / "dist"

    build_pages(
        CONFIG,
        output,
        collection=example_collection(),
        base_url="https://example.invalid/_build-index",
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
    landing_html = (output / "index.html").read_text()

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
            "filename": "index_test_gpu-0.1.0+cu128-py3-none-any.whl",
            "hashes": {"sha256": "b" * 64},
            "size": 5002,
            "upload-time": "2026-06-02T17:13:12Z",
            "url": (
                "https://github.com/example/build-index-test-gpu/releases/"
                "download/0.1.0/"
                "index_test_gpu-0.1.0%2Bcu128-py3-none-any.whl"
            ),
        }
    ]
    assert 'content="1.4"' in root_html
    assert 'content="1.4"' in project_html
    assert "#sha256=" + "b" * 64 in project_html
    assert "2 projects" in landing_html
    assert "Catalog" not in landing_html
    assert not (output / "catalog").exists()
    assert not (output / "artifacts").exists()
    assert (output / ".nojekyll").exists()


def test_build_pages_generates_empty_channel_documents(tmp_path: Path) -> None:
    output = tmp_path / "dist"

    build_pages(CONFIG, output, base_url="https://example.invalid/_build-index")

    root = json.loads((output / "simple" / "cu128" / "index.json").read_text())
    assert root == {"meta": {"api-version": "1.4"}, "projects": []}


def test_build_pages_rejects_unconfigured_repository(tmp_path: Path) -> None:
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
        build_pages(CONFIG, tmp_path / "dist", collection=collection)


def test_build_pages_replaces_output_tree(tmp_path: Path) -> None:
    output = tmp_path / "dist"
    stale = output / "stale.txt"
    stale.parent.mkdir()
    stale.write_text("stale")

    build_pages(CONFIG, output, collection=example_collection())

    assert not stale.exists()
