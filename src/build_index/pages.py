"""Generate a deterministic static tree for the review branch."""

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


def build_pages(
    config: IndexConfig,
    output: Path,
    *,
    collection: ReleaseCollection | None = None,
    base_url: str | None = None,
) -> tuple[Path, ...]:
    """Build deterministic JSON and HTML Simple API documents."""
    publication_base_url = (base_url or config.site.base_url).rstrip("/")
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
    written.append(_write_text(output / ".nojekyll", ""))
    written.append(
        _write_text(
            output / "index.html",
            _landing_html(
                config.channels,
                projects_by_channel,
                publication_base_url,
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
        "files": [
            {
                "filename": artifact.filename,
                "hashes": {"sha256": artifact.sha256},
                "size": artifact.size,
                "upload-time": artifact.upload_time,
                "url": artifact.url,
            }
            for artifact in files
        ],
        "meta": {"api-version": "1.4"},
        "name": project,
        "versions": sorted(
            {artifact.version for artifact in files},
            key=lambda value: (Version(value), value),
        ),
    }


def _reset_output(output: Path) -> None:
    resolved = output.resolve()
    forbidden = {Path("/").resolve(), Path.home().resolve(), Path.cwd().resolve()}
    if resolved in forbidden:
        raise ValueError(f"refusing to replace unsafe output directory: {resolved}")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)


def _landing_html(
    channels: tuple[ChannelConfig, ...],
    projects_by_channel: dict[str, tuple[str, ...]],
    base_url: str,
) -> str:
    channel_rows = "\n".join(
        _channel_row(channel, len(projects_by_channel[channel.name]), base_url)
        for channel in channels
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>Package indexes</title>
  <style>
    :root {{ color-scheme: dark; --bg:#0d0e0d; --panel:#171718; --line:#2a292a; --text:#dfdfdf; --muted:#9f9f9f; --accent:#d7ff64; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,sans-serif; }}
    main {{ width:min(780px,calc(100% - 2rem)); margin:3rem auto; }}
    h2 {{ margin:0 0 1rem; font-size:1.15rem; }}
    p {{ color:var(--muted); }}
    button {{ font:inherit; }}
    .channel {{ width:100%; display:grid; grid-template-columns:8rem 1fr auto; gap:1rem; padding:.85rem; border:0; border-top:1px solid var(--line); background:transparent; color:inherit; text-align:left; cursor:pointer; }}
    .channel:last-child {{ border-bottom:1px solid var(--line); }}
    .channel:hover,.channel[aria-pressed="true"] {{ background:var(--panel); }}
    .channel-name {{ color:var(--accent); font-family:monospace; }}
    .channel-description,.channel-projects {{ color:var(--muted); }}
    #helper {{ margin-top:2.5rem; }}
    #helper[hidden] {{ display:none; }}
    pre {{ overflow:auto; padding:1rem; border:1px solid var(--line); background:var(--panel); }}
    .copy {{ float:right; padding:.25rem .5rem; border:1px solid var(--line); background:var(--bg); color:var(--text); cursor:pointer; }}
    code {{ font-family:ui-monospace,monospace; }}
    @media(max-width:600px) {{ .channel {{ grid-template-columns:1fr; gap:.2rem; }} }}
  </style>
</head>
  <body>
    <main>
      <h2>Available channels</h2>
      <p>Click a channel to select your environment.</p>
{channel_rows}
      <section id="helper" hidden>
        <h2 id="selected-channel"></h2>
        <button class="copy" data-copy="uv-snippet">Copy</button>
        <h3>uv</h3>
        <pre><code id="uv-snippet"></code></pre>
        <button class="copy" data-copy="pip-snippet">Copy</button>
        <h3>pip</h3>
        <pre><code id="pip-snippet"></code></pre>
      </section>
    </main>
    <script>
      const helper = document.getElementById("helper");
      const selectedChannel = document.getElementById("selected-channel");
      const uvSnippet = document.getElementById("uv-snippet");
      const pipSnippet = document.getElementById("pip-snippet");

      for (const channel of document.querySelectorAll(".channel")) {{
        channel.addEventListener("click", () => {{
          for (const candidate of document.querySelectorAll(".channel")) {{
            candidate.setAttribute("aria-pressed", String(candidate === channel));
          }}
          const name = channel.dataset.channel;
          const url = channel.dataset.indexUrl;
          const indexName = `astral-${{name}}`;
          selectedChannel.textContent = name;
          uvSnippet.textContent = `[[tool.uv.index]]\\nname = "${{indexName}}"\\nurl = "${{url}}"\\nexplicit = true\\n\\n[tool.uv.sources]\\nyour-package = {{ index = "${{indexName}}" }}`;
          pipSnippet.textContent = `python -m pip install --extra-index-url ${{url}} <package>`;
          helper.hidden = false;
        }});
      }}

      for (const button of document.querySelectorAll(".copy")) {{
        button.addEventListener("click", async () => {{
          const snippet = document.getElementById(button.dataset.copy).textContent;
          await navigator.clipboard.writeText(snippet);
          button.textContent = "Copied";
          window.setTimeout(() => {{ button.textContent = "Copy"; }}, 1200);
        }});
      }}
    </script>
  </body>
</html>
"""


def _channel_row(channel: ChannelConfig, project_count: int, base_url: str) -> str:
    name = _escape(channel.name)
    description = _escape(channel.description)
    projects = _count_label(project_count, "project")
    index_url = _escape(f"{base_url.rstrip('/')}/simple/{channel.name}/")
    return f"""      <button aria-pressed="false" class="channel" data-channel="{name}" data-index-url="{index_url}" type="button">
        <span class="channel-name">{name}</span>
        <span class="channel-description">{description}</span>
        <span class="channel-projects">{projects}</span>
      </button>"""


def _count_label(count: int, singular: str) -> str:
    suffix = "" if count == 1 else "s"
    return f"{count} {singular}{suffix}"


def _simple_landing_html(channels: tuple[ChannelConfig, ...]) -> str:
    links = [
        f'<a href="./{_escape(channel.name)}/">{_escape(channel.name)}</a>'
        for channel in channels
    ]
    return _html_document("Simple API channels", links)


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
    links = [
        f'<a href="{_escape(artifact.url)}#sha256={artifact.sha256}">'
        f"{_escape(artifact.filename)}</a>"
        for artifact in files
    ]
    return _html_document(
        f"Links for {project}",
        links,
        repository_version="1.4",
    )


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
