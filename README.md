# Astral GPU indexes

The Astral GPU indexes provide pre-built wheels for popular GPU-enabled
packages in the PyTorch ecosystem across supported Python versions, CPU
architectures, CUDA versions, and PyTorch versions. Following the PyTorch
convention, each supported CUDA version has a separate index URL and each
supported CUDA and PyTorch combination has a separate wheel version.

The indexes implement the standard Python Simple API and work with uv, pip,
Poetry, and other Python package installers.

[Browse the available indexes, packages, and versions][index]

These indexes supplement PyPI. They contain selected wheels, not a complete
mirror of package dependencies.

## Using the indexes

Choose the index matching the target compute platform. Index URLs have this
form:

```text
https://wheels.astral.sh/simple/<channel>/
```

The [index landing page][index] lists the enabled channels and the current
packages and versions in each one. Common channel names include `cpu`, `cu126`,
`cu128`, `cu129`, and `cu130`.

### Response formats

The default root for each channel serves the PEP 691 v1 JSON representation:

```text
https://wheels.astral.sh/simple/cu128/
Content-Type: application/vnd.pypi.simple.v1+json
```

Configure an explicit version and format when a client should not use the
default:

| Format | Index path |
| --- | --- |
| v1 JSON | `/simple/v1+json/<channel>/` |
| v1 HTML | `/simple/v1+html/<channel>/` |

The default and explicit JSON endpoints contain the same repository data. Each
explicit endpoint always returns its configured representation rather than
selecting a format from the request's `Accept` header. This follows PEP 691's
[endpoint configuration][pep-691-endpoint] mechanism. Any of these channel
roots can be used as the index URL in the examples below.

### uv projects

Use `uv add` to pin a package to the selected Astral GPU index. For example, to
install vLLM from the CUDA 12.8 index:

```bash
uv add vllm --index astral-cu128=https://wheels.astral.sh/simple/cu128/
```

Replace `vllm` and `cu128` with a package and channel shown on the landing
page. Add an exact package version when reproducibility requires it.

### uv pip

For environment-oriented workflows, add the selected Astral GPU index at
install time:

```bash
uv pip install vllm \
    --index astral-cu128=https://wheels.astral.sh/simple/cu128/
```

### pip

Keep PyPI available for dependencies by adding the selected index:

```bash
INDEX_URL=https://wheels.astral.sh/simple/cu128/
python -m pip install --extra-index-url "$INDEX_URL" vllm
```

Pin package versions in automated pip workflows. A small number of preserved
legacy producer wheels contain filename and embedded-metadata versions that do
not agree; pip rejects those files, while uv can resolve them. The index does
not modify producer wheel bytes to make them installable. Also note that pip
does not prioritize `--extra-index-url` over PyPI; it considers candidates from
both indexes.

### Poetry

Add the selected index as an explicit source, then assign packages to it:

```bash
INDEX_URL=https://wheels.astral.sh/simple/cu128/
poetry source add --priority=explicit astral-cu128 "$INDEX_URL"
poetry add --source astral-cu128 vllm
```

## Published artifacts

Every package link points to an immutable wheel mirrored into Cloudflare R2.
Publication preserves producer bytes and records the SHA-256 digest, size,
upload time, `Requires-Python`, and exact core metadata.

The index does not build wheels, mirror dependencies, or rewrite wheel
metadata. The publication workflow does not delete artifact history when a
release leaves an index; stale artifact pruning is an explicit maintainer
workflow.

## Maintaining the indexes

[`config/index.toml`](config/index.toml) is the production inventory. Changes
to repositories, projects, release policies, or channel mappings should be
reviewed there before publication.

Detailed maintainer documentation:

| Document | Purpose |
| --- | --- |
| [Configuration](docs/CONFIGURATION.md) | Repository admission, channel assignment, and release policy |
| [Publishing](docs/PUBLISHING.md) | GitHub Actions, authentication, R2 mirroring, and failure behavior |
| [GitHub App](github-apps/README.md) | Temporary private-producer read access |

The publication workflow runs on pushes to `main`, hourly, and on manual
dispatch. It completes collection and artifact mirroring before replacing any
Simple API documents, so an invalid or unknown release cannot publish a partial
index update.

### Development

```bash
export UV_DEFAULT_INDEX=https://pypi.org/simple

uv sync --locked
uv run --locked build-index validate-config
uv run --locked pytest
uv run --locked ruff check
uv run --locked ruff format --check
uv run --locked zizmor .
```

## License

Licensed under the [Apache License, Version 2.0](LICENSE).

[index]: https://wheels.astral.sh/
[pep-691-endpoint]: https://peps.python.org/pep-0691/#endpoint-configuration
