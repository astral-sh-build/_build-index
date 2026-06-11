"""Generate deterministic Python Simple API documents."""

from __future__ import annotations

import html
import json
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

from packaging.version import Version

from build_index.collection import CollectedArtifact, CollectionError, ReleaseCollection
from build_index.config import ChannelConfig, IndexConfig


def build_index_tree(
    config: IndexConfig,
    output: Path,
    *,
    collection: ReleaseCollection | None = None,
    public_base_url: str | None = None,
) -> tuple[Path, ...]:
    """Build deterministic index and Python Simple API documents."""
    files = _group_artifacts(config, collection or ReleaseCollection())
    projects_by_channel = {
        channel.name: tuple(
            sorted(
                project
                for channel_name, project in files
                if channel_name == channel.name
            )
        )
        for channel in config.channels
    }
    _reset_output(output)

    written: list[Path] = []
    written.append(
        _write_text(
            output / "index.html",
            _index_landing_html(
                config.channels,
                projects_by_channel,
                files,
                public_base_url=public_base_url,
            ),
        )
    )
    written.append(
        _write_text(
            output / "simple" / "index.html",
            _simple_landing_html(config.channels),
        )
    )

    for channel in config.channels:
        channel_name = channel.name
        projects = projects_by_channel[channel_name]
        html_body = _project_list_html(projects)
        json_body = {
            "meta": {"api-version": "1.4"},
            "projects": [{"name": project} for project in projects],
        }
        written.append(
            _write_json(
                output / "simple" / channel_name / "index.json",
                json_body,
            )
        )
        written.append(
            _write_text(
                output / "simple" / "v1+html" / channel_name / "index.html",
                html_body,
            )
        )
        written.append(
            _write_json(
                output / "simple" / "v1+json" / channel_name / "index.json",
                json_body,
            )
        )
        for project in projects:
            project_files = tuple(files[(channel_name, project)])
            project_json = _project_json(project, project_files)
            written.append(
                _write_json(
                    output / "simple" / channel_name / project / "index.json",
                    project_json,
                )
            )
            written.append(
                _write_text(
                    output
                    / "simple"
                    / "v1+html"
                    / channel_name
                    / project
                    / "index.html",
                    _project_files_html(project, project_files),
                )
            )
            written.append(
                _write_json(
                    output
                    / "simple"
                    / "v1+json"
                    / channel_name
                    / project
                    / "index.json",
                    project_json,
                )
            )

    return tuple(written)


def _group_artifacts(
    config: IndexConfig,
    collection: ReleaseCollection,
) -> dict[tuple[str, str], list[CollectedArtifact]]:
    files: dict[tuple[str, str], list[CollectedArtifact]] = defaultdict(list)
    configured_channels = {channel.name for channel in config.channels}
    for artifact in collection.artifacts:
        if artifact.published_url is None or artifact.metadata_sha256 is None:
            raise CollectionError(
                f"collection contains an unmirrored artifact: {artifact.filename}"
            )
        repository = config.repository(artifact.repository)
        if repository is None:
            raise CollectionError(
                f"collection contains an unconfigured repository: {artifact.repository}"
            )
        if artifact.project not in repository.projects:
            raise CollectionError(
                f"collection contains an unconfigured project for "
                f"{artifact.repository}: {artifact.project}"
            )
        if artifact.channel not in configured_channels:
            raise CollectionError(
                f"collection contains an unconfigured channel: {artifact.channel}"
            )
        if (
            repository.channels is not None
            and artifact.channel not in repository.channels
        ):
            raise CollectionError(
                f"collection contains a restricted channel for "
                f"{artifact.repository}: {artifact.channel}"
            )
        files[(artifact.channel, artifact.project)].append(artifact)

    for project_files in files.values():
        project_files.sort(key=lambda artifact: artifact.filename)
    return files


def _project_json(
    project: str,
    files: tuple[CollectedArtifact, ...],
) -> dict[str, Any]:
    return {
        "files": [_project_json_file(artifact) for artifact in files],
        "meta": {"api-version": "1.4"},
        "name": project,
        "versions": sorted(
            {artifact.version for artifact in files},
            key=lambda value: (Version(value), value),
        ),
    }


def _project_json_file(artifact: CollectedArtifact) -> dict[str, Any]:
    if artifact.published_url is None or artifact.metadata_sha256 is None:
        raise CollectionError(f"artifact is not mirrored: {artifact.filename}")
    result = {
        "core-metadata": {"sha256": artifact.metadata_sha256},
        "filename": artifact.filename,
        "hashes": {"sha256": artifact.sha256},
        "size": artifact.size,
        "upload-time": artifact.upload_time,
        "url": artifact.published_url,
    }
    if artifact.requires_python is not None:
        result["requires-python"] = artifact.requires_python
    return result


def _reset_output(output: Path) -> None:
    resolved = output.resolve()
    forbidden = {Path("/").resolve(), Path.home().resolve(), Path.cwd().resolve()}
    if resolved in forbidden:
        raise ValueError(f"refusing to replace unsafe output directory: {resolved}")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)


def _simple_landing_html(channels: tuple[ChannelConfig, ...]) -> str:
    links = [
        f'<a href="./{_escape(channel.name)}/">{_escape(channel.name)}</a>'
        for channel in channels
    ]
    return _html_document("Simple API channels", links)


def _index_landing_html(
    channels: tuple[ChannelConfig, ...],
    projects_by_channel: dict[str, tuple[str, ...]],
    files: dict[tuple[str, str], list[CollectedArtifact]],
    *,
    public_base_url: str | None,
) -> str:
    populated_channels = tuple(
        channel for channel in channels if projects_by_channel[channel.name]
    )
    example_channel = next(
        (channel for channel in populated_channels if channel.name == "cu128"),
        populated_channels[0] if populated_channels else channels[0],
    )
    example_projects = projects_by_channel[example_channel.name]
    example_project = (
        "vllm"
        if "vllm" in example_projects
        else example_projects[0]
        if example_projects
        else "PACKAGE"
    )
    example_versions = sorted(
        {
            artifact.version
            for artifact in files.get((example_channel.name, example_project), ())
        },
        key=lambda value: (Version(value), value),
        reverse=True,
    )
    example_version = example_versions[0] if example_versions else "VERSION"
    example_requirement = f"{example_project}=={example_version}"
    index_name = f"astral-{example_channel.name}"
    base_url = (
        public_base_url.rstrip("/") if public_base_url else "https://<index-host>"
    )
    index_url = f"{base_url}/simple/{example_channel.name}/"
    lines = [
        "<!doctype html>",
        '<html lang="en">',
        "  <head>",
        '    <meta charset="utf-8">',
        '    <meta name="viewport" content="width=device-width, initial-scale=1">',
        '    <link rel="icon" href="data:,">',
        "    <title>Astral package indexes</title>",
        "  </head>",
        "  <body>",
        "    <header>",
        "      <h1>Astral package indexes</h1>",
        "      <p>Pre-built Python packages organized by compute platform.</p>",
        "    </header>",
        "    <main>",
        '      <section aria-labelledby="available-indexes">',
        '        <h2 id="available-indexes">Available indexes</h2>',
        "        <table>",
        "          <thead>",
        "            <tr>",
        '              <th scope="col">Index</th>',
        '              <th scope="col">Description</th>',
        '              <th scope="col">Packages</th>',
        "            </tr>",
        "          </thead>",
        "          <tbody>",
    ]
    for channel in channels:
        projects = projects_by_channel[channel.name]
        lines.extend(
            [
                "            <tr>",
                "              <td>"
                f'<a href="./simple/{_escape(channel.name)}/">'
                f"{_escape(channel.name)}</a></td>",
                f"              <td>{_escape(channel.description)}</td>",
                f"              <td>{len(projects)}</td>",
                "            </tr>",
            ]
        )
    lines.extend(
        [
            "          </tbody>",
            "        </table>",
            "      </section>",
            '      <section aria-labelledby="using-indexes">',
            '        <h2 id="using-indexes">Using an index with uv</h2>',
            "        <p>Each index provides builds for one compute platform. "
            f"The following example uses <code>{_escape(example_channel.name)}</code>; "
            "choose the index that matches your target.</p>",
            "        <h3>Configure the index</h3>",
            "        <p>Add the index to <code>pyproject.toml</code> and mark it "
            "explicit so packages use it only when selected through "
            "<code>tool.uv.sources</code>.</p>",
            "        <pre><code>[[tool.uv.index]]\n"
            f'name = "{_escape(index_name)}"\n'
            f'url = "{_escape(index_url)}"\n'
            "explicit = true</code></pre>",
            "        <h3>Select packages from the index</h3>",
            "        <p>Route each package that needs a specialized build to the "
            "configured index:</p>",
            "        <pre><code>[tool.uv.sources]\n"
            f'"{_escape(example_project)}" = '
            f'{{ index = "{_escape(index_name)}" }}</code></pre>',
            "        <p>Then add or declare the dependency normally. Other "
            "available packages and versions are listed below.</p>",
            f"        <pre><code>uv add {_escape(example_requirement)}</code></pre>",
            "        <h3>The uv pip interface</h3>",
            "        <p>For environment-oriented workflows, pass the selected "
            "index directly:</p>",
            f"        <pre><code>uv pip install --index {_escape(index_url)} "
            f"{_escape(example_requirement)}</code></pre>",
            "        <h3>pip</h3>",
            f"        <pre><code>python -m pip install --extra-index-url "
            f"{_escape(index_url)} {_escape(example_requirement)}</code></pre>",
            "        <p>These indexes contain selected builds, not a complete "
            "PyPI mirror. Pin package versions in automated environments.</p>",
            "      </section>",
            '      <section aria-labelledby="package-inventory">',
            '        <h2 id="package-inventory">Packages and versions</h2>',
            "        <p>Expand an index to inspect its available packages and "
            "versions.</p>",
        ]
    )
    for channel in channels:
        projects = projects_by_channel[channel.name]
        package_label = "package" if len(projects) == 1 else "packages"
        lines.extend(
            [
                f'        <details id="index-{_escape(channel.name)}">',
                "          <summary>"
                f"{_escape(channel.name)} - {_escape(channel.description)} "
                f"({len(projects)} {package_label})</summary>",
                "          <table>",
                "            <thead>",
                "              <tr>",
                '                <th scope="col">Package</th>',
                '                <th scope="col">Versions</th>',
                "              </tr>",
                "            </thead>",
                "            <tbody>",
            ]
        )
        for project in projects:
            versions = sorted(
                {artifact.version for artifact in files[(channel.name, project)]},
                key=lambda value: (Version(value), value),
                reverse=True,
            )
            rendered_versions = ", ".join(
                f"<code>{_escape(version)}</code>" for version in versions
            )
            lines.extend(
                [
                    "              <tr>",
                    "                <td>"
                    f'<a href="./simple/{_escape(channel.name)}/'
                    f'{_escape(project)}/">{_escape(project)}</a></td>',
                    f"                <td>{rendered_versions}</td>",
                    "              </tr>",
                ]
            )
        lines.extend(
            [
                "            </tbody>",
                "          </table>",
                "        </details>",
            ]
        )
    lines.extend(
        [
            "      </section>",
            "    </main>",
            "  </body>",
            "</html>",
            "",
        ]
    )
    return "\n".join(lines)


def _project_list_html(projects: tuple[str, ...]) -> str:
    links = [
        f'<a href="./{_escape(project)}/">{_escape(project)}</a>'
        for project in projects
    ]
    return _html_document(
        "Simple index",
        links,
        repository_version="1.4",
    )


def _project_files_html(
    project: str,
    files: tuple[CollectedArtifact, ...],
) -> str:
    links = [_artifact_link(artifact) for artifact in files]
    return _html_document(
        f"Links for {project}",
        links,
        repository_version="1.4",
    )


def _artifact_link(artifact: CollectedArtifact) -> str:
    if artifact.published_url is None or artifact.metadata_sha256 is None:
        raise CollectionError(f"artifact is not mirrored: {artifact.filename}")
    attributes = [
        f'href="{_escape(artifact.published_url)}#sha256={artifact.sha256}"',
        f'data-core-metadata="sha256={artifact.metadata_sha256}"',
    ]
    if artifact.requires_python is not None:
        attributes.append(f'data-requires-python="{_escape(artifact.requires_python)}"')
    return f"<a {' '.join(attributes)}>{_escape(artifact.filename)}</a>"


def _html_document(
    title: str,
    links: list[str],
    *,
    introduction: str | None = None,
    repository_version: str | None = None,
) -> str:
    head = [
        "<!doctype html>",
        "<html>",
        "  <head>",
        '    <meta charset="utf-8">',
        f"    <title>{_escape(title)}</title>",
    ]
    if repository_version is not None:
        head.append(
            '    <meta name="pypi:repository-version" '
            f'content="{_escape(repository_version)}">'
        )
    body = ["  </head>", "  <body>", f"    <h1>{_escape(title)}</h1>"]
    if introduction is not None:
        body.append(f"    <p>{_escape(introduction)}</p>")
    body.extend(f"    {link}<br>" for link in links)
    return "\n".join([*head, *body, "  </body>", "</html>", ""])


def _write_json(path: Path, value: Any) -> Path:
    return _write_text(
        path,
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
    )


def _write_text(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def _escape(value: str) -> str:
    return html.escape(value, quote=True)
