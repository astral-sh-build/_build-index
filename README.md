# _build-index

`_build-index` turns wheels from configured GitHub Releases into static Python
Simple API indexes.

It does five things:

1. Polls configured repositories.
2. Selects the highest trailing `-rN` revision for each release family.
3. Validates wheel projects and assigns each wheel to a configured channel.
4. Generates deterministic PEP 691 JSON and HTML index documents.
5. Syncs the explicit JSON and HTML Simple API trees to Cloudflare R2.

It does not build wheels, mirror dependencies, or retain publication history.
Invalid legacy wheel names with repeated local-version `+` separators are
normalized in the index; their source URLs and bytes are unchanged.

## Configuration

Each run reads one TOML configuration containing:

- The public base URL.
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
That token is preferred for every GitHub API read. Public repositories retry
anonymously only when GitHub rejects the installation token because the
repository is outside the App installation; private repositories never use
anonymous fallback.

The Pages workflow derives its explicit GitHub App repository scope from the
active configuration, polls those repositories, builds the complete static
tree, optionally syncs its explicit Simple API documents to R2, and replaces
the `pages` branch. With no configured repositories it publishes an empty
index. The branch remains available for review.

R2 publication is enabled when these repository settings are configured:

| Setting | GitHub type | Purpose |
| --- | --- | --- |
| `R2_ENDPOINT` | Variable | `https://<account-id>.r2.cloudflarestorage.com` |
| `R2_BUCKET` | Variable | Destination bucket name |
| `R2_ACCESS_KEY_ID` | Secret | Bucket-scoped R2 S3 access key |
| `R2_SECRET_ACCESS_KEY` | Secret | Bucket-scoped R2 S3 secret key |

The bucket, custom domain, URL rewrites, and cache behavior are managed by the
[`terraform/`](terraform/README.md) stack. Its outputs provide the two
non-secret GitHub variables. R2 access keys remain separately managed secrets
so they are not stored in Terraform state.

The R2 credentials should have object read and write access only to the target
bucket. The sync owns only `simple/v1+json/` and `simple/v1+html/`; it does not
delete or upload any future `artifacts/` objects. The conventional
trailing-slash URLs require Cloudflare routing to append `index.json` or
`index.html`; direct R2 object URLs include those filenames. The default
`/simple/<channel>/` JSON alias is also a CDN rewrite concern.

The workflow uses the AWS CLI included in GitHub's Ubuntu runner. Each endpoint
tree is mirrored with `aws s3 sync --delete`, its PEP 691 media type, and a
short cache lifetime. Keeping R2 publication in the workflow avoids adding an
S3 SDK and publisher abstraction to this small package.

Wheel URLs still point to GitHub Release assets. Copying wheel bytes belongs in
the later artifact-ingestion change because it also requires public URL
rewriting, immutable object naming, metadata extraction, and ordering artifact
publication before index publication. None of those concerns are needed to
establish R2 index publication.

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
