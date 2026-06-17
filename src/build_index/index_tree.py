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
    example_data = channel_examples[example_channel.name]
    channel_rows = []
    for channel in channels:
        projects = projects_by_channel[channel.name]
        package_label = "package" if len(projects) == 1 else "packages"
        channel_rows.append(
            f'<a class="catalog-name" href="./simple/{_escape(channel.name)}/">'
            f"{_escape(channel.name)}</a>"
            f'<span class="catalog-description">'
            f"{_escape(channel.description)}</span>"
            f'<span class="catalog-count">{len(projects)} {package_label}</span>'
        )
    lines = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '  <meta charset="UTF-8">',
        '  <meta name="viewport" content="width=device-width, initial-scale=1.0">',
        "  <title>Astral build indexes</title>",
        '  <meta name="description" content="Pre-built Python wheels organized by compute platform.">',
        _UVX_STYLE,
        "</head>",
        "<body>",
        "  <h1>Astral build indexes</h1>",
        '  <p class="tagline">Pre-built Python wheels organized by compute platform. Powered by <a href="https://docs.astral.sh/uv/" target="_blank" rel="noopener noreferrer">uv</a>.</p>',
        "",
        "  <p>Choose an index matching your target compute platform. Each index supplements PyPI with selected wheel builds.</p>",
        "",
        '  <h2 id="available-indexes">Available indexes</h2>',
        _terminal("\n".join(channel_rows), copy=False),
    ]
    lines.extend(
        [
            '  <h2 id="using-indexes">Using an index with uv</h2>',
            "  <p>Choose a channel to update the examples and copy buttons below.</p>",
            _channel_chooser(
                channels, projects_by_channel, selected=example_channel.name
            ),
            f'  <p class="example-note" data-example-note>{example_data["note"]}</p>',
            "  <h3>Add the index</h3>",
            '  <p class="example-note">Use <code>explicit = true</code> so this index is only used for packages listed in <code>tool.uv.sources</code>.</p>',
            _terminal(example_data["snippets"]["index_config"], snippet="index-config"),
            "  <h3>Point the package to the index</h3>",
            _terminal(
                example_data["snippets"]["source_config"], snippet="source-config"
            ),
            "  <h3>Add the dependency</h3>",
            _terminal(example_data["snippets"]["uv_add"], snippet="uv-add"),
            "",
            "  <h2>The uv pip interface</h2>",
            _terminal(example_data["snippets"]["uv_pip"], snippet="uv-pip"),
            "",
            "  <h2>pip</h2>",
            '  <p class="example-note">Because these indexes contain selected wheels, keep PyPI available by adding the selected index as an extra index.</p>',
            _terminal(example_data["snippets"]["pip"], snippet="pip"),
            "",
            '  <p class="note">These indexes contain selected builds, not a complete PyPI mirror. Pin package versions in automated environments.</p>',
            "",
            '  <h2 id="package-inventory">Packages and versions</h2>',
            "  <p>Expand an index to inspect its available packages and versions.</p>",
        ]
    )
    for channel in channels:
        projects = projects_by_channel[channel.name]
        package_label = "package" if len(projects) == 1 else "packages"
        lines.extend(
            [
                f'  <details id="index-{_escape(channel.name)}">',
                "    <summary>"
                f"{_escape(channel.name)} - {_escape(channel.description)} "
                f"({len(projects)} {package_label})</summary>",
            ]
        )
        if not projects:
            lines.extend(
                [
                    '    <p class="empty-state">No packages currently published for this channel.</p>',
                    "  </details>",
                ]
            )
            continue
        project_rows = []
        for project in projects:
            versions = sorted(
                {artifact.version for artifact in files[(channel.name, project)]},
                key=lambda value: (Version(value), value),
                reverse=True,
            )
            rendered_versions = ", ".join(
                f'<span class="cmd">{_escape(version)}</span>' for version in versions
            )
            project_rows.append(
                f'<a class="catalog-name project-name" '
                f'href="./simple/{_escape(channel.name)}/{_escape(project)}/">'
                f"{_escape(project)}</a>  "
                f'<span class="catalog-description">{rendered_versions}</span>'
            )
        lines.extend(
            [
                _terminal("\n".join(project_rows), copy=False),
                "  </details>",
            ]
        )
    lines.extend(
        [
            _ASTRAL_FOOTER,
            (
                '  <script id="channel-example-data" type="application/json">'
                f"{_json_script(channel_examples)}</script>"
            ),
            _UVX_SCRIPT,
            "</body>",
            "</html>",
            "",
        ]
    )
    return "\n".join(lines)


def _channel_example(
    channel: ChannelConfig,
    projects_by_channel: dict[str, tuple[str, ...]],
    files: dict[tuple[str, str], list[CollectedArtifact]],
    *,
    base_url: str,
) -> dict[str, Any]:
    projects = projects_by_channel[channel.name]
    project = "vllm" if "vllm" in projects else projects[0] if projects else "PACKAGE"
    versions = sorted(
        {artifact.version for artifact in files.get((channel.name, project), ())},
        key=lambda value: (Version(value), value),
        reverse=True,
    )
    version = versions[0] if versions else "VERSION"
    requirement = f"{project}=={version}"
    index_name = f"astral-{channel.name}"
    index_url = f"{base_url}/simple/{channel.name}/"
    uv_project_config = (
        "[[tool.uv.index]]\n"
        f'name = "{index_name}"\n'
        f'url = "{index_url}"\n'
        "explicit = true"
    )
    uv_source_config = (
        f'[tool.uv.sources]\n"{project}" = [\n  {{ index = "{index_name}" }},\n]'
    )
    uv_add_command = f"uv add {requirement}"
    uv_pip_command = (
        f"uv pip install \\\n    {requirement} \\\n    --index-url {index_url}"
    )
    pip_command = (
        "python -m pip install \\\n"
        f"    {requirement} \\\n"
        f"    --extra-index-url {index_url}"
    )
    if projects:
        note = (
            f"Examples use <code>{_escape(channel.name)}</code> with "
            f"<code>{_escape(project)}</code> "
            f"<code>{_escape(version)}</code>."
        )
    else:
        note = (
            f"No packages are currently published for <code>{_escape(channel.name)}</code>; "
            "package-specific examples use placeholders."
        )
    return {
        "channel": channel.name,
        "has_packages": bool(projects),
        "note": note,
        "project": project,
        "snippets": {
            "index_config": _highlight_toml(uv_project_config),
            "source_config": _highlight_toml(uv_source_config),
            "uv_add": _highlight_shell(uv_add_command),
            "uv_pip": _highlight_shell(uv_pip_command),
            "pip": _highlight_shell(pip_command),
        },
        "version": version,
    }


def _channel_chooser(
    channels: tuple[ChannelConfig, ...],
    projects_by_channel: dict[str, tuple[str, ...]],
    *,
    selected: str,
) -> str:
    lines = ['  <div class="channel-chooser" role="group" aria-label="Select channel">']
    for channel in channels:
        projects = projects_by_channel[channel.name]
        package_label = "package" if len(projects) == 1 else "packages"
        aria_pressed = "true" if channel.name == selected else "false"
        display_name = _channel_display_name(channel)
        lines.extend(
            [
                "    <button "
                'type="button" '
                'class="channel-option" '
                f'data-channel="{_escape(channel.name)}" '
                f'aria-pressed="{aria_pressed}">',
                f'      <span class="channel-label">{_escape(display_name)}</span>',
                (
                    '      <span class="channel-meta">'
                    f"{_escape(channel.name)} / {len(projects)} {package_label}</span>"
                ),
                "    </button>",
            ]
        )
    lines.append("  </div>")
    return "\n".join(lines)


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


_UVX_STYLE = """  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      background: #0c0c0c;
      color: #e0e0e0;
      font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
      font-size: 14px;
      line-height: 1.6;
      padding: 1.5rem;
      padding-bottom: 1rem;
      max-width: 720px;
      margin: 0 auto;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }
    h1 {
      color: #fff;
      margin-bottom: 0.5rem;
      font-size: 2rem;
      letter-spacing: -0.02em;
    }
    p.tagline { color: #888; margin-bottom: 1.5rem; }
    p.tagline a { color: #D7FF64; }
    p.tagline a:hover { text-decoration: underline; }
    h2 {
      color: #888;
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin: 1.25rem 0 0.5rem;
    }
    h3 {
      color: #e0e0e0;
      font-size: 0.875rem;
      font-weight: 600;
      margin: 1rem 0 0.35rem;
    }
    p { color: #e0e0e0; }
    .note { margin-top: 1rem; }
    .example-note { color: #888; margin: 0.25rem 0 0.5rem; }
    .channel-chooser {
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
      margin: 0.75rem 0 0.35rem;
    }
    .channel-option {
      align-items: flex-start;
      background: #161616;
      border: 1px solid #2a2a2a;
      border-radius: 4px;
      color: #e0e0e0;
      cursor: pointer;
      display: inline-flex;
      flex-direction: column;
      font-family: inherit;
      gap: 0.05rem;
      min-width: 7rem;
      padding: 0.35rem 0.55rem;
      transition: all 0.2s;
    }
    .channel-option:hover {
      background: #1e1e1e;
      border-color: #4a4a4a;
    }
    .channel-option[aria-pressed="true"] {
      background: #D7FF64;
      border-color: #D7FF64;
      color: #0c0c0c;
    }
    .channel-label {
      font-weight: 600;
      line-height: 1.2;
    }
    .channel-meta {
      color: #888;
      font-size: 0.75rem;
      line-height: 1.2;
    }
    .channel-option[aria-pressed="true"] .channel-meta { color: #3a3a3a; }
    .terminal {
      background: #161616;
      border: 1px solid #2a2a2a;
      border-radius: 8px;
      margin: 0.5rem 0;
      overflow: hidden;
    }
    .terminal-header {
      background: #1e1e1e;
      padding: 0.5rem 0.75rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      border-bottom: 1px solid #2a2a2a;
    }
    .terminal-dots {
      display: flex;
      gap: 6px;
    }
    .terminal-dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
    }
    .terminal-dot.red { background: #ff5f56; }
    .terminal-dot.yellow { background: #ffbd2e; }
    .terminal-dot.green { background: #27ca40; }
    .copy-btn {
      background: transparent;
      border: 1px solid #3a3a3a;
      border-radius: 4px;
      color: #888;
      cursor: pointer;
      font-size: 12px;
      padding: 0.25rem 0.5rem;
      transition: all 0.2s;
      font-family: inherit;
    }
    .copy-btn:hover { background: #2a2a2a; color: #e0e0e0; border-color: #4a4a4a; }
    pre {
      padding: 1rem;
      overflow-x: auto;
      margin: 0;
    }
    code { color: #D7FF64; }
    .terminal code { color: #e0e0e0; }
    .cmd { color: #D7FF64; }
    .flag { color: #ff79c6; }
    .url { color: #8be9fd; }
    .pipe { color: #f8f8f2; font-weight: bold; }
    .string { color: #f1fa8c; }
    .comment { color: #6272a4; font-style: italic; }
    .catalog-name {
      display: inline-block;
      min-width: 5.25rem;
    }
    .catalog-description {
      display: inline-block;
      min-width: 13rem;
      color: #e0e0e0;
    }
    .catalog-count { color: #888; }
    .project-name { min-width: 13rem; }
    a { color: #D7FF64; text-decoration: none; }
    a:hover { text-decoration: underline; }
    details { margin: 0.5rem 0; }
    summary {
      color: #e0e0e0;
      cursor: pointer;
      list-style-position: inside;
    }
    summary:hover { color: #D7FF64; }
    .empty-state {
      color: #888;
      margin: 0.35rem 0 0.75rem;
      padding-left: 1.1rem;
    }
    .footer { margin-top: 3rem; color: #666; font-size: 0.875rem; }
    .footer a { color: #888; }
    .footer a:hover { color: #D7FF64; }
    .astral { margin-top: auto; padding-top: 1rem; text-align: center; color: #888; }
    .astral a { transition: opacity 0.2s; }
    .astral a:hover { opacity: 0.7; }
    .astral svg { height: 1.5rem; }
    @media (prefers-color-scheme: light) {
      body { background: #fff; color: #222; }
      h1 { color: #000; }
      p.tagline { color: #666; }
      p.tagline a { color: #6340AC; }
      h2 { color: #666; }
      h3 { color: #222; }
      p { color: #222; }
      .example-note { color: #666; }
      .channel-option {
        background: #f8f8f8;
        border-color: #e0e0e0;
        color: #222;
      }
      .channel-option:hover {
        background: #ececec;
        border-color: #aaa;
      }
      .channel-option[aria-pressed="true"] {
        background: #6340AC;
        border-color: #6340AC;
        color: #fff;
      }
      .channel-meta { color: #666; }
      .channel-option[aria-pressed="true"] .channel-meta { color: #e9ddff; }
      .terminal { background: #f8f8f8; border-color: #e0e0e0; }
      .terminal-header { background: #ececec; border-color: #e0e0e0; }
      .terminal-dot.red { background: #ff5f56; }
      .terminal-dot.yellow { background: #ffbd2e; }
      .terminal-dot.green { background: #27ca40; }
      .copy-btn { border-color: #ccc; color: #666; }
      .copy-btn:hover { background: #e0e0e0; color: #222; border-color: #aaa; }
      code { color: #6340AC; }
      .terminal code { color: #222; }
      .cmd { color: #6340AC; }
      .flag { color: #d33682; }
      .url { color: #2aa198; }
      .pipe { color: #222; }
      .string { color: #b58900; }
      .comment { color: #93a1a1; }
      .catalog-description { color: #222; }
      .catalog-count { color: #666; }
      a { color: #6340AC; }
      summary { color: #222; }
      summary:hover { color: #6340AC; }
      .footer { color: #888; }
      .footer a { color: #666; }
      .footer a:hover { color: #6340AC; }
      .astral a { color: #30173D; }
    }
  </style>"""


_UVX_SCRIPT = """  <script>
    const channelExamples = JSON.parse(
      document.getElementById('channel-example-data').textContent
    );

    function copyCode(btn) {
      const pre = btn.closest('.terminal').querySelector('pre');
      const text = pre.textContent;
      navigator.clipboard.writeText(text).then(() => {
        btn.textContent = 'Copied!';
        setTimeout(() => {
          btn.textContent = 'Copy';
        }, 2000);
      });
    }

    function setChannel(channel) {
      const example = channelExamples[channel];
      if (!example) return;

      document.querySelector('[data-snippet="index-config"]').innerHTML =
        example.snippets.index_config;
      document.querySelector('[data-snippet="source-config"]').innerHTML =
        example.snippets.source_config;
      document.querySelector('[data-snippet="uv-add"]').innerHTML =
        example.snippets.uv_add;
      document.querySelector('[data-snippet="uv-pip"]').innerHTML =
        example.snippets.uv_pip;
      document.querySelector('[data-snippet="pip"]').innerHTML =
        example.snippets.pip;
      document.querySelector('[data-example-note]').innerHTML = example.note;

      document.querySelectorAll('[data-channel]').forEach((btn) => {
        btn.setAttribute('aria-pressed', String(btn.dataset.channel === channel));
      });
    }

    document.querySelectorAll('[data-channel]').forEach((btn) => {
      btn.addEventListener('click', () => setChannel(btn.dataset.channel));
    });
  </script>"""


_ASTRAL_FOOTER = """  <div class="astral">
    <a href="https://astral.sh" target="_blank" rel="noopener noreferrer" aria-label="Made by Astral">
      <svg width="139" height="24" viewBox="0 0 139 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M8.72798 15.2726H9.91316V11.8697L9.6887 10.4062L9.8952 10.3343L12.1309 15.1649L14.3486 10.3343L14.5461 10.4062L14.3486 11.8607V15.2726H15.5248V8.72714H13.9535L12.2117 12.7137H12.0142L10.2723 8.72714H8.72798V15.2726Z" fill="currentColor"/>
        <path d="M22.3432 15.2726H23.6631L21.3017 8.72714H19.7574L17.4589 15.2726H18.7069L19.1558 13.9797H21.9033L22.3432 15.2726ZM19.497 13.0279L19.901 11.8607L20.4308 10.0021H20.6463L21.176 11.8607L21.5711 13.0279H19.497Z" fill="currentColor"/>
        <path d="M25.4209 15.2726H28.1234C30.1077 15.2726 30.9876 14.1413 30.9876 12.0044C30.9876 9.92131 30.1706 8.72714 28.1234 8.72714H25.4209V15.2726ZM26.624 14.2131V9.77765H28.0965C29.147 9.77765 29.7306 10.1907 29.7306 11.4477V12.5521C29.7306 13.6923 29.2817 14.2131 28.0965 14.2131H26.624Z" fill="currentColor"/>
        <path d="M33.079 15.2726H37.6491V14.2131H34.2822V12.3815H37.2002V11.3938H34.2822V9.77765H37.6491V8.72714H33.079V15.2726Z" fill="currentColor"/>
        <path d="M42.923 15.2726H46.2451C47.4572 15.2726 48.2025 14.5812 48.2025 13.5487C48.2025 12.7675 47.8343 12.175 47.0532 11.9954V11.7799C47.6637 11.5734 48.0319 11.0436 48.0319 10.3433C48.0319 9.38259 47.4572 8.72714 46.281 8.72714H42.923V15.2726ZM44.0992 11.4746V9.65195H45.9578C46.4875 9.65195 46.7928 9.92131 46.7928 10.3523V10.7653C46.7928 11.1873 46.4965 11.4746 45.9758 11.4746H44.0992ZM44.0992 14.3388V12.3904H46.0296C46.5863 12.3904 46.9365 12.6418 46.9365 13.1806V13.5666C46.9365 14.0425 46.5684 14.3388 45.9309 14.3388H44.0992Z" fill="currentColor"/>
        <path d="M49.6959 8.72714L52.174 12.579V14.1952H50.1898V15.2726H53.3772V12.579L55.8553 8.72714H54.4456L53.5119 10.2535L52.8744 11.3759H52.6679L52.0483 10.2715L51.1056 8.72714H49.6959Z" fill="currentColor"/>
        <path fill-rule="evenodd" clip-rule="evenodd" d="M74.1824 7.63626C74.1824 7.03377 74.6708 6.54535 75.2733 6.54535H84.0006C84.6031 6.54535 85.0915 7.03377 85.0915 7.63626V9.81808H80.0733V8.94535H79.2006V10.6908H84.0006C84.6031 10.6908 85.0915 11.1792 85.0915 11.7817V16.3635C85.0915 16.966 84.6031 17.4544 84.0006 17.4544H75.2733C74.6708 17.4544 74.1824 16.966 74.1824 16.3635V14.1817L79.2006 14.1817V15.0544H80.0733V13.309L75.2733 13.309C74.6708 13.309 74.1824 12.8206 74.1824 12.2181V7.63626ZM63.4912 6.54545C62.8887 6.54545 62.4003 7.03387 62.4003 7.63636V17.4545H67.4185V14.1818H68.2912V17.4545H73.3094V7.63636C73.3094 7.03387 72.821 6.54545 72.2185 6.54545H63.4912ZM69.164 10.6909V11.5636H66.5458V10.6909H69.164ZM110.619 6.54545C110.016 6.54545 109.528 7.03387 109.528 7.63636V17.4545H114.546V14.1818H115.419V17.4545H120.437V7.63636C120.437 7.03387 119.948 6.54545 119.346 6.54545H110.619ZM116.291 10.6909V11.5636H113.673V10.6909H116.291ZM91.8549 8.29091H96.8731V11.3455C96.8731 11.9479 96.3847 12.4364 95.7822 12.4364H91.8549V13.3091H96.8731V17.4545H87.9276C87.3251 17.4545 86.8367 16.9661 86.8367 16.3636V12.4364H85.964V8.29091H86.8367V6.54545H91.8549V8.29091ZM108.655 7.63636C108.655 7.03387 108.166 6.54545 107.564 6.54545H97.7458V17.4545H102.764V14.1818H103.637V17.4545H108.655V13.3091H106.473V12.4364H107.564C108.166 12.4364 108.655 11.9479 108.655 11.3455V7.63636ZM104.509 10.6909V11.5636H101.891V10.6909H104.509ZM132.218 13.3091L126.327 13.3091V6.54547L121.309 6.54547V17.4546H132.218V13.3091Z" fill="currentColor"/>
      </svg>
    </a>
  </div>"""


def _terminal(markup: str, *, copy: bool = True, snippet: str | None = None) -> str:
    header = [
        '    <div class="terminal-header">',
        '      <div class="terminal-dots">',
        '        <span class="terminal-dot red"></span>',
        '        <span class="terminal-dot yellow"></span>',
        '        <span class="terminal-dot green"></span>',
        "      </div>",
    ]
    if copy:
        header.append(
            '      <button class="copy-btn" onclick="copyCode(this)">Copy</button>'
        )
    header.append("    </div>")
    code_attributes = f' data-snippet="{_escape(snippet)}"' if snippet else ""
    return "\n".join(
        [
            '  <div class="terminal">',
            *header,
            f"    <pre><code{code_attributes}>{markup}</code></pre>",
            "  </div>",
        ]
    )


def _highlight_toml(value: str) -> str:
    lines = []
    for line in value.splitlines():
        if not line or line.startswith("[") or line[:1].isspace():
            lines.append(_escape(line))
            continue
        key, separator, rest = line.partition("=")
        if separator:
            lines.append(
                f'<span class="cmd">{_escape(key.rstrip())}</span> '
                f"{separator} {_highlight_toml_value(rest.strip())}"
            )
        else:
            lines.append(_escape(line))
    return "\n".join(lines)


def _highlight_toml_value(value: str) -> str:
    if value.startswith('"') and value.endswith('"'):
        return f'<span class="string">{_escape(value)}</span>'
    return _escape(value)


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
