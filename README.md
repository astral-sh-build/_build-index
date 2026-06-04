# _build-index

`_build-index` turns wheels from configured GitHub Releases into static Python
Simple API indexes.

It does four things:

1. Polls configured repositories.
2. Selects the highest trailing `-rN` revision for each release family.
3. Validates wheel projects and assigns each wheel to a configured channel.
4. Generates deterministic PEP 691 JSON and HTML index documents.

It does not build wheels, mirror dependencies, or retain publication history.
Invalid legacy wheel names with repeated local-version `+` separators are
normalized in the index; their source URLs and bytes are unchanged.

## Configuration

Each run reads one TOML configuration containing:

- The public base URL.
- Available channels.
- GitHub repositories and their admitted projects.

Channels are inferred per wheel from version markers such as `+cpu`, `+cu128`,
`+rocm6.3`, and `+xpu`. The global channel list defines what the index may
publish. A repository may provide an optional `channels` list to restrict its
wheels further, but repositories do not need to enumerate their normal output
channels.

[`config/index.toml`](config/index.toml) is the active production configuration.
Its initial producer set covers CPU-only and GPU-only release layouts.
Additional producers will be added incrementally.
[`config/astral-sh-build.toml`](config/astral-sh-build.toml) is an evaluation
inventory and is not used by the Pages workflow.

## Commands

```bash
export GH_TOKEN="$(gh auth token)"

uv run --locked build-index collect
uv run --locked build-index build
```

`collect` writes `build/releases.json`. `build` reads that file and replaces
`dist/` with:

```text
index.html
.nojekyll

simple/index.html
simple/<channel>/index.json
simple/<channel>/<project>/index.json

simple/v1+json/<channel>/index.json
simple/v1+json/<channel>/<project>/index.json

simple/v1+html/<channel>/index.html
simple/v1+html/<channel>/<project>/index.html
```

The JSON-default and explicit JSON documents contain the same data. Explicit
HTML documents support clients that request the HTML representation.

Every collection is a fresh projection of currently published GitHub
Releases. An unsuffixed tag is revision zero; `1.0-r2` replaces `1.0` and
`1.0-r1`. An empty highest revision removes that release family's files from
the generated index. When one repository republishes the same filename across
release families, the later-uploaded asset is used.

## Development

```bash
export UV_DEFAULT_INDEX=https://pypi.org/simple

uv sync --locked
uv run --locked build-index validate-config
uv run --locked pytest
uv run --locked ruff check
uv run --locked ruff format --check
uv run --locked zizmor .
```

Private producer repositories grant read access through the GitHub App
described in [`github-apps/README.md`](github-apps/README.md). The workflow
creates a short-lived token using
[`actions/create-reader-token`](actions/create-reader-token/action.yml).

The Pages workflow derives its explicit GitHub App repository scope from the
active configuration, polls those repositories, builds the complete static
tree, and replaces the `pages` branch. With no configured repositories it
publishes an empty index. The branch is for review; wheel URLs still point to
private GitHub Release assets. Public artifact storage and CDN publication
remain separate future work.

## Deferred Artifact Publication

The production publisher will copy selected GitHub Release wheels to public
artifact storage before publishing index documents. During that single wheel
transfer, it should also:

1. Extract the wheel's exact `.dist-info/METADATA` bytes.
2. Publish those bytes unchanged at `<public wheel URL>.metadata`.
3. Compute the metadata file's SHA-256.
4. Parse and normalize its `Requires-Python` value when present.
5. Add `core-metadata` and `requires-python` to PEP 691 JSON project pages.
6. Add `data-core-metadata` and `data-requires-python` to HTML project pages.

This belongs to artifact ingestion, not GitHub Release collection or producer
build repositories. Existing configured releases do not publish
`.whl.metadata` assets, and advertising PEP 658 metadata is only valid when the
corresponding `<wheel URL>.metadata` resource is publicly available. Historical
wheels will therefore gain metadata naturally when they are first copied into
public storage; no separate producer-side backfill is required.

See [PEP 658](https://peps.python.org/pep-0658/) and
[PEP 714](https://peps.python.org/pep-0714/).
