# _build-index

`_build-index` turns wheels from configured GitHub Releases into static Python
Simple API indexes.

It does six things:

1. Polls configured repositories.
2. Selects the highest trailing `-rN` revision for each release family.
3. Validates wheel projects and assigns each wheel to a configured channel.
4. Mirrors selected wheel bytes and exact core metadata to Cloudflare R2.
5. Generates a human-readable package inventory and deterministic PEP 691 JSON
   and HTML index documents.
6. Syncs the generated index documents to Cloudflare R2.

It does not build wheels, mirror dependencies, or delete immutable artifact
history.
Invalid legacy wheel names with repeated local-version `+` separators are
normalized in the index; their source URLs and bytes are unchanged.

## Configuration

Each run reads one TOML configuration containing:

- Available channels.
- GitHub repositories and their admitted projects.
- Optional release-version and unlabeled-wheel channel policies.

Channels are inferred per wheel from version markers such as `+cpu`, `+cu128`,
`+rocm6.3`, and `+xpu`. The global channel list defines what the index may
publish. A repository may provide an optional `channels` list to restrict its
wheels further, but repositories do not need to enumerate their normal output
channels.

[`config/index.toml`](config/index.toml) is the active production configuration.
It admits the build repositories confirmed against the live hardware registries
in the Astral Pyx workspace, plus stable upstream `vllm-project/vllm` releases
from `v0.9.1`. Upstream CPU wheels are excluded, and unlabeled wheels are
assigned only in reviewed, half-open CUDA-version ranges. Explicit wheel labels
remain authoritative.

Repositories default to private access and opaque release tags. A repository
opts into version policy by configuring `tag_regex`,
`minimum_release_version`, or `unlabeled_channel_rules`. Version regexes use
`fullmatch` and must contain one named `version` capture. Policy-enabled
repositories skip nonmatching, invalid, below-minimum, and prerelease versions
unless prereleases are explicitly allowed. Unlabeled channel rules require
bounded, nonoverlapping `from` and `before` versions; an unlabeled wheel in a
gap fails collection. Ignored channels are exclusions rather than publication
channels, so they do not need to appear in the global channel list.
Wheel metadata versions must match filename versions by default. A repository
may explicitly allow mismatches for exact, reviewed release tags with
`allowed_metadata_version_mismatch_tags`; the mirror logs every use and still
preserves the original wheel and metadata bytes.

## Commands

```bash
export GH_TOKEN="$(gh auth token)"

uv run --locked build-index collect
uv run --locked build-index mirror
uv run --locked build-index build
uv run --locked build-index sync-r2
```

`collect` writes `build/releases.json`. `build` reads that file and replaces
`dist/` with:

```text
index.html

simple/index.html
simple/<channel>/index.json
simple/<channel>/<project>/index.json

simple/v1+json/<channel>/index.json
simple/v1+json/<channel>/<project>/index.json

simple/v1+html/<channel>/index.html
simple/v1+html/<channel>/<project>/index.html
```

`index.html` lists each channel, explains how to use it, and provides an
expandable package and version inventory. It uses only semantic HTML and the
browser's default stylesheet. The JSON-default and explicit JSON documents
contain the same data. Explicit HTML documents support clients that request the
HTML representation.

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
That token is preferred for every GitHub API read. Public repositories retry
anonymously only when GitHub rejects the installation token because the
repository is outside the App installation; private repositories never use
anonymous fallback.

The publication workflow derives its explicit GitHub App repository scope from
the active configuration, polls those repositories, mirrors admitted artifacts,
builds the complete index tree, and syncs it to R2. With no configured
repositories it publishes the landing page and empty channel indexes.

R2 publication is enabled when these repository settings are configured:

| Setting | GitHub type | Purpose |
| --- | --- | --- |
| `R2_ENDPOINT` | Variable | `https://<account-id>.r2.cloudflarestorage.com` |
| `R2_BUCKET` | Variable | Destination bucket name |
| `R2_PUBLIC_URL` | Variable | Public URL serving the bucket |
| `R2_ACCESS_KEY_ID` | Variable | Bucket-scoped R2 S3 access key ID |
| `R2_SECRET_ACCESS_KEY` | Secret | Bucket-scoped R2 S3 secret key |

The R2 credentials should have object read and write access only to the target
bucket. Cloudflare resources are provisioned separately; this repository needs
only the bucket's S3 endpoint and credentials.

The workflow first mirrors every selected wheel to an immutable,
content-addressed key:

```text
artifacts/<wheel-sha256>/<filename>
artifacts/<wheel-sha256>/<filename>.metadata
```

The wheel is downloaded through GitHub's authenticated release-asset API, and
its size and SHA-256 are verified before publication. The exact
`.dist-info/METADATA` bytes are extracted without rewriting the wheel or the
metadata. The metadata name and public version must agree with the wheel
filename. Its local version may be omitted or may be a dot-delimited prefix of
the filename's local version, allowing producer-added build dimensions while
rejecting unrelated versions. `Requires-Python` is validated and normalized
when present.

R2 object metadata records the wheel and core-metadata hashes plus normalized
`Requires-Python`. Each fresh runner uses a pooled in-process S3 client to
check complete artifacts with bounded, parallel `head-object` requests, then
preserves deterministic serial uploads for anything missing. An interrupted
run resumes without a separate database. Artifact objects are immutable and
are not deleted when a release leaves the index.

Only after every selected wheel and metadata sidecar is present does the
workflow generate index documents. Published project pages contain only R2
artifact URLs and advertise PEP 658 metadata using `core-metadata` in JSON and
`data-core-metadata` in HTML. An incomplete mirror fails before the existing R2
index is changed.

The workflow then publishes `dist/index.html` and every generated
`dist/simple/**/index.json` or `index.html` document to R2. Simple API documents
use the object key for their canonical trailing-slash URL:

```text
dist/simple/cu128/vllm/index.json
  -> simple/cu128/vllm/

dist/simple/v1+html/cu128/vllm/index.html
  -> simple/v1+html/cu128/vllm/
```

This makes the default JSON and explicit JSON/HTML endpoints work directly
through an R2 custom domain without Cloudflare URL rewrites. Project documents
are uploaded before channel roots, and stale objects are deleted only after all
new objects succeed. The landing page is published as `index.html`; because R2
does not provide static-website index resolution, the public custom domain must
rewrite `/` to `/index.html` for the page to render at the root URL. The
document sync owns the complete `simple/` object prefix but never deletes
`artifacts/` objects. It uses one pooled boto3 client with bounded concurrent
uploads and batched stale-object deletion, avoiding a new CLI process and TLS
connection for every document.

See [PEP 658](https://peps.python.org/pep-0658/) and
[PEP 714](https://peps.python.org/pep-0714/).
