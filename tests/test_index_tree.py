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
            artifact(
                "example/build-flash-attention",
                "flash_attn-2.8.3+cu128-py3-none-any.whl",
                "flash-attn",
                "2.8.3+cu128",
                "cu128",
                sha256="f" * 64,
                size=5005,
            ),
            artifact(
                "example/build-vllm",
                "vllm-0.22.0+cpu-py3-none-any.whl",
                "vllm",
                "0.22.0+cpu",
                "cpu",
                sha256="0" * 64,
                size=5006,
            ),
            artifact(
                "example/build-vllm",
                "vllm-0.22.0+cu128-py3-none-any.whl",
                "vllm",
                "0.22.0+cu128",
                "cu128",
                sha256="1" * 64,
                size=5007,
            ),
        ]
    )


def test_build_index_tree_generates_index_documents(tmp_path: Path) -> None:
    output = tmp_path / "dist"

    build_index_tree(
        CONFIG,
        output,
        collection=example_collection(),
        public_base_url="https://packages.example",
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
            {"name": "flash-attn"},
            {"name": "index-test-gpu"},
            {"name": "index-test-mixed"},
            {"name": "vllm"},
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
    assert "<h1>Astral build indexes</h1>" in landing_html
    assert '<a class="catalog-name" href="./simple/cu128/">cu128</a>' in landing_html
    assert "CUDA 12.8 builds" in landing_html
    assert "Using an index with uv" in landing_html
    assert "Add the index" in landing_html
    assert "Point the package to the index" in landing_html
    assert "Add the dependency" in landing_html
    assert "Use <code>explicit = true</code>" in landing_html
    assert "packages listed in <code>tool.uv.sources</code>" in landing_html
    assert "[[tool.uv.index]]" in landing_html
    assert '<span class="cmd">name</span> = ' in landing_html
    assert '<span class="string">&quot;astral-cu128&quot;</span>' in landing_html
    assert '<span class="cmd">url</span> = ' in landing_html
    assert (
        '<span class="string">&quot;https://packages.example/simple/cu128/&quot;</span>'
        in landing_html
    )
    assert '<span class="cmd">explicit</span> = true' in landing_html
    assert "[tool.uv.sources]" in landing_html
    assert '<span class="cmd">&quot;flash-attn&quot;</span> = ' in landing_html
    assert "  { index = &quot;astral-cu128&quot; }," in landing_html
    assert "]</code></pre>" in landing_html
    assert '<span class="cmd">uv</span> add flash-attn==2.8.3+cu128' in landing_html
    assert '<span class="cmd">uv</span> pip install ' in landing_html
    assert '<span class="flag">--index-url</span>' in landing_html
    assert (
        '<span class="cmd">python</span> '
        '<span class="flag">-m</span> pip install ' in landing_html
    )
    assert '<span class="flag">--extra-index-url</span>' in landing_html
    assert "keep PyPI available" in landing_html
    assert (
        '<span class="url">https://packages.example/simple/cu128/</span>'
        in landing_html
    )
    assert "flash-attn==2.8.3+cu128" in landing_html
    assert "\\\n    flash-attn==2.8.3+cu128" in landing_html
    assert '<details id="index-cu128">' in landing_html
    assert (
        '<a class="catalog-name project-name" '
        'href="./simple/cu128/index-test-gpu/">index-test-gpu</a>' in landing_html
    )
    assert '<span class="cmd">0.1.0+cu128</span>' in landing_html
    assert "No packages currently published for this channel." in landing_html
    assert "<style" in landing_html
    assert "stylesheet" not in landing_html
    assert "<script" in landing_html
    assert 'aria-label="Made by Astral"' in landing_html
    assert '<svg width="139" height="24"' in landing_html
    assert ".astral svg { height: 1.5rem; }" in landing_html
    assert "copyCode" in landing_html
    assert landing_html.count('onclick="copyCode(this)"') == 5
    assert (
        '<div class="channel-chooser" role="group" aria-label="Select channel">'
        in landing_html
    )
    assert 'data-channel="cpu" aria-pressed="false"' in landing_html
    assert 'data-channel="cu128" aria-pressed="true"' in landing_html
    assert '<span class="channel-label">CPU</span>' in landing_html
    assert '<span class="channel-meta">cpu / 3 packages</span>' in landing_html
    assert '<span class="channel-label">CUDA 12.8</span>' in landing_html
    assert '<span class="channel-meta">cu128 / 4 packages</span>' in landing_html
    assert 'data-snippet="index-config"' in landing_html
    assert 'data-snippet="source-config"' in landing_html
    assert 'data-snippet="uv-add"' in landing_html
    assert 'data-snippet="uv-pip"' in landing_html
    assert 'data-snippet="pip"' in landing_html
    assert "setChannel(btn.dataset.channel)" in landing_html
    channel_data_prefix = '<script id="channel-example-data" type="application/json">'
    channel_data_start = landing_html.index(channel_data_prefix) + len(
        channel_data_prefix
    )
    channel_data_end = landing_html.index("</script>", channel_data_start)
    channel_examples = json.loads(landing_html[channel_data_start:channel_data_end])
    assert channel_examples["cpu"]["has_packages"] is True
    assert "astral-cpu" in channel_examples["cpu"]["snippets"]["index_config"]
    assert (
        "https://packages.example/simple/cpu/"
        in channel_examples["cpu"]["snippets"]["index_config"]
    )
    assert "vllm==0.22.0+cpu" in channel_examples["cpu"]["snippets"]["uv_add"]
    assert "flash-attn==2.8.3+cu128" in channel_examples["cu128"]["snippets"]["uv_add"]
    assert "--index-url" in channel_examples["cpu"]["snippets"]["uv_pip"]
    assert "--extra-index-url" in channel_examples["cpu"]["snippets"]["pip"]
    assert (
        "  { index = &quot;astral-cpu&quot; },"
        in channel_examples["cpu"]["snippets"]["source_config"]
    )
    assert channel_examples["cu118"]["has_packages"] is False
    assert "PACKAGE==VERSION" in channel_examples["cu118"]["snippets"]["uv_add"]
    assert (
        "package-specific examples use placeholders"
        in channel_examples["cu118"]["note"]
    )
    assert not (output / "catalog").exists()
    assert not (output / "artifacts").exists()


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
