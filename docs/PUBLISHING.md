# Publishing

The publication workflow collects release assets, mirrors immutable wheel data,
builds deterministic index documents, and publishes those documents to
Cloudflare R2.

The workflow is defined in `.github/workflows/publish.yml` and runs:

- On pushes to `main`.
- Hourly at minute 17.
- On manual dispatch.

One concurrency group serializes publication runs without cancelling an
in-progress run.

## Pipeline

Publication executes these stages in order:

1. Validate `config/index.toml`.
2. Resolve the private producer repository scope and create a read-only GitHub
   App installation token when needed.
3. Collect eligible wheels from configured GitHub Releases.
4. Mirror wheel bytes and exact core metadata to R2.
5. Build the landing page and Python Simple API tree.
6. Sync generated index documents to R2 and remove stale Simple API documents.

Collection and mirroring finish before any index document changes. A malformed
release, unknown channel, missing implicit-channel rule, digest mismatch, or
failed upload leaves the previously published index intact.

## GitHub authentication

Private producer repositories grant read access through the GitHub App
described in [`github-apps/README.md`](../github-apps/README.md). The workflow
derives an explicit repository list from private configuration entries and
creates a short-lived token with only `Contents: Read`.

The token is preferred for GitHub API reads. A public repository retries
anonymously only when GitHub rejects the token because that repository is
outside the App installation. Private sources never fall back anonymously.
Rate limits, server errors, and malformed responses are not anonymous-fallback
conditions.

When all producer repositories are public, the App action and its credentials
can be removed after every source is explicitly configured with
`access = "public"`.

## R2 settings

The GitHub repository requires these Actions settings:

| Setting | GitHub type | Purpose |
| --- | --- | --- |
| `R2_ENDPOINT` | Variable | Private S3 endpoint, `https://<account-id>.r2.cloudflarestorage.com` |
| `R2_BUCKET` | Variable | Destination bucket name |
| `R2_PUBLIC_URL` | Variable | Public base URL embedded in package and metadata links |
| `R2_ACCESS_KEY_ID` | Variable | Bucket-scoped S3 access key ID |
| `R2_SECRET_ACCESS_KEY` | Secret | Bucket-scoped S3 secret key |

Use Cloudflare's Object Read & Write permission, scoped only to the target
bucket. Publication uses object HEAD, PUT, LIST, and DELETE operations.
Cloudflare DNS, custom-domain, cache, and rewrite configuration are managed
separately.

## Artifact mirroring

Selected wheels use immutable, content-addressed keys:

```text
artifacts/<wheel-sha256>/<filename>
artifacts/<wheel-sha256>/<filename>.metadata
```

The mirror downloads a wheel through GitHub's authenticated release-asset API
and verifies its expected size and SHA-256 before upload. It extracts and
validates exact core metadata without rewriting wheel bytes. Artifact filenames
are retained except for the documented legacy repeated-`+` normalization. R2
object metadata records the wheel hash, metadata hash, and normalized
`Requires-Python` value. See [Configuration](CONFIGURATION.md#wheel-metadata-policy)
for the admission rules and filename exception.

Each run checks existing objects with bounded parallel `head-object` requests.
Missing artifacts upload serially and deterministically. This makes an
interrupted run resumable without a separate database.

Metadata policy is an ingestion decision. A complete existing wheel and
metadata sidecar are reused by hash and are not re-extracted when policy code
changes later. Tightening metadata validation therefore affects newly mirrored
artifacts; revalidating existing objects requires a deliberate audit.

Artifact objects are never deleted automatically when a release leaves the
index.

## Generated documents

`build-index build` replaces `dist/` with the landing page and three Simple API
endpoint families. Each family includes channel and project documents:

| Endpoint | Generated file | Content type |
| --- | --- | --- |
| `simple/<channel>/` | `simple/<channel>/index.json` | `application/vnd.pypi.simple.v1+json` |
| `simple/v1+json/<channel>/` | `simple/v1+json/<channel>/index.json` | `application/vnd.pypi.simple.v1+json` |
| `simple/v1+html/<channel>/` | `simple/v1+html/<channel>/index.html` | `application/vnd.pypi.simple.v1+html` |

The unversioned and explicit JSON endpoints contain the same data. These paths
implement PEP 691's [endpoint-configuration mechanism][pep-691-endpoint]: each
endpoint fixes its representation instead of negotiating through `Accept`.

Project documents advertise exact core metadata according to PEP 658 and PEP
714. Repeated builds from the same mirrored collection produce byte-identical
documents. With no configured repositories, the build still produces the
landing page and empty channel indexes.

R2 stores each Simple API document at its canonical trailing-slash object key;
for example, `dist/simple/cu128/vllm/index.json` becomes
`simple/cu128/vllm/`.

Package-level documents upload before channel and index roots. Stale objects
under the complete `simple/` prefix are deleted only after all new uploads
succeed. Document sync never deletes `artifacts/` objects.

The landing page uploads as `index.html`. R2 does not provide static-website
index resolution, so a public custom domain must internally rewrite `/` to
`/index.html`.

## Running locally

Set the required GitHub and R2 environment variables, then run each stage:

```bash
export GH_TOKEN="$(gh auth token)"

uv run --locked build-index collect
uv run --locked build-index mirror
uv run --locked build-index build
uv run --locked build-index sync-r2
```

`collect` writes `build/releases.json`. `mirror` updates that collection with
R2 URLs and core metadata. `build` reads the mirrored collection and writes
`dist/`. `sync-r2` publishes only generated documents.

See [PEP 658](https://peps.python.org/pep-0658/),
[PEP 691](https://peps.python.org/pep-0691/), and
[PEP 714](https://peps.python.org/pep-0714/).

[pep-691-endpoint]: https://peps.python.org/pep-0691/#endpoint-configuration
