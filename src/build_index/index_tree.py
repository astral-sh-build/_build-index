"""Generate deterministic Python Simple API documents."""

from __future__ import annotations

import html
import json
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader
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
                config,
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
    config: IndexConfig,
    projects_by_channel: dict[str, tuple[str, ...]],
    files: dict[tuple[str, str], list[CollectedArtifact]],
    *,
    public_base_url: str | None,
) -> str:
    channels = config.channels
    populated_channels = tuple(
        channel for channel in channels if projects_by_channel[channel.name]
    )
    example_channel = next(
        (channel for channel in channels if channel.name == "cu126"),
        populated_channels[0] if populated_channels else channels[0],
    )
    base_url = (
        public_base_url.rstrip("/") if public_base_url else "https://<index-host>"
    )
    channel_examples = {
        channel.name: _channel_example(
            channel,
            projects_by_channel,
            files,
            base_url=base_url,
        )
        for channel in channels
    }
    return _landing_template().render(
        build_packages=_build_package_catalog(config),
        channel_examples_json=_json_script(channel_examples),
        channels=_landing_channels(
            channels,
            projects_by_channel,
            selected=example_channel.name,
        ),
        example=channel_examples[example_channel.name],
    )


def _build_package_catalog(config: IndexConfig) -> list[dict[str, str]]:
    packages = {}
    for repository in config.repositories:
        _owner, name = repository.repository.split("/", maxsplit=1)
        if not name.startswith("build-"):
            continue
        for project in repository.projects:
            packages[project] = repository.repository
    return [
        {
            "name": project,
            "url": f"https://github.com/{packages[project]}",
        }
        for project in sorted(packages)
    ]


def _landing_template():
    return Environment(
        loader=PackageLoader("build_index", "templates"),
        autoescape=True,
        keep_trailing_newline=True,
        lstrip_blocks=True,
        trim_blocks=True,
    ).get_template("landing.html")


def _landing_channels(
    channels: tuple[ChannelConfig, ...],
    projects_by_channel: dict[str, tuple[str, ...]],
    *,
    selected: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for channel in channels:
        projects = projects_by_channel[channel.name]
        package_label = "package" if len(projects) == 1 else "packages"
        result.append(
            {
                "count": len(projects),
                "description": channel.description,
                "display_name": _channel_display_name(channel),
                "name": channel.name,
                "package_label": package_label,
                "selected": channel.name == selected,
            }
        )
    return result


def _example_project(projects: tuple[str, ...]) -> str:
    for project in ("flash-attn", "vllm"):
        if project in projects:
            return project
    return projects[0] if projects else "PACKAGE"


def _channel_example(
    channel: ChannelConfig,
    projects_by_channel: dict[str, tuple[str, ...]],
    files: dict[tuple[str, str], list[CollectedArtifact]],
    *,
    base_url: str,
) -> dict[str, Any]:
    projects = projects_by_channel[channel.name]
    project = _example_project(projects)
    versions = sorted(
        {artifact.version for artifact in files.get((channel.name, project), ())},
        key=lambda value: (Version(value), value),
        reverse=True,
    )
    version = versions[0] if versions else "VERSION"
    requirement = f"{project}=={version}"
    index_name = f"astral-{channel.name}"
    index_url = f"{base_url}/simple/{channel.name}/"
    uv_add_command = f"uv add {requirement} --index {index_name}={index_url}"
    uv_pip_command = (
        f"uv pip install \\\n    {requirement} \\\n    --index {index_name}={index_url}"
    )
    pip_command = (
        "python -m pip install \\\n"
        f"    {requirement} \\\n"
        f"    --extra-index-url {index_url}"
    )
    return {
        "channel": channel.name,
        "has_packages": bool(projects),
        "inventory_count": (
            f"{len(projects)} {'package' if len(projects) == 1 else 'packages'}"
        ),
        "inventory_html": _package_inventory_html(channel, projects, files),
        "inventory_title": _channel_display_name(channel),
        "project": project,
        "uv_add_note": (
            f"Use <code>uv add</code> to pin a package to the Astral "
            f"{'CPU' if channel.name == 'cpu' else 'GPU'} index."
        ),
        "uv_pip_note": (
            f"Add the Astral {'CPU' if channel.name == 'cpu' else 'GPU'} index as "
            "an extra <code>--index</code> at install time."
        ),
        "pip_note": (
            f"Add the Astral {'CPU' if channel.name == 'cpu' else 'GPU'} index as "
            "an extra <code>--extra-index-url</code> at install time."
        ),
        "snippets": {
            "uv_add": _highlight_shell(uv_add_command),
            "uv_pip": _highlight_shell(uv_pip_command),
            "pip": _highlight_shell(pip_command),
        },
        "version": version,
    }


def _package_inventory_html(
    channel: ChannelConfig,
    projects: tuple[str, ...],
    files: dict[tuple[str, str], list[CollectedArtifact]],
) -> str:
    if not projects:
        return '<p class="empty-state">No packages currently published.</p>'

    rows = []
    for project in projects:
        versions = sorted(
            {artifact.version for artifact in files[(channel.name, project)]},
            key=lambda value: (Version(value), value),
            reverse=True,
        )
        rendered_versions = ", ".join(
            f'<span class="cmd">{_escape(version)}</span>' for version in versions
        )
        rows.append(
            '<div class="package-row">'
            f'<a class="package-name" href="./simple/{_escape(channel.name)}/'
            f'{_escape(project)}/">{_escape(project)}</a>'
            f'<span class="package-versions">{rendered_versions}</span>'
            "</div>"
        )
    return "\n".join(rows)


def _channel_display_name(channel: ChannelConfig) -> str:
    suffix = " builds"
    if channel.description.endswith(suffix):
        return channel.description[: -len(suffix)]
    return channel.description


def _json_script(value: Any) -> str:
    return (
        json.dumps(value, sort_keys=True)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _highlight_shell(value: str) -> str:
    lines = []
    command_highlighted = False
    for line in value.splitlines():
        stripped = line.lstrip(" ")
        indent = line[: len(line) - len(stripped)]
        highlighted = []
        for part in stripped.split(" "):
            if not part:
                highlighted.append("")
                continue
            if not command_highlighted:
                css_class = "cmd"
                command_highlighted = True
            elif part == "|":
                css_class = "pipe"
            elif part.startswith("-"):
                css_class = "flag"
            elif "://" in part:
                css_class = "url"
            else:
                css_class = ""
            if css_class:
                highlighted.append(f'<span class="{css_class}">{_escape(part)}</span>')
            else:
                highlighted.append(_escape(part))
        lines.append(_escape(indent) + " ".join(highlighted))
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
