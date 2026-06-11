# Index configuration

`config/index.toml` is the production input to collection and publication. It
defines which channels may be published, which GitHub repositories are trusted
as producers, and any release-version policy needed to classify their wheels.

Validate changes before collection:

```bash
uv run --locked build-index validate-config
```

## Channels

Channels are assigned per wheel, and one producer release may contain multiple
channels. Every published channel must be declared globally:

```toml
[[channel]]
name = "cu128"
description = "CUDA 12.8 builds"
```

The global channel list is a publication allowlist. Canonical names are:

| Kind | Name | Example |
| --- | --- | --- |
| CPU | `cpu` | `cpu` |
| CUDA | `cu<major><minor>` | `cu128` |
| ROCm | `rocm<major>.<minor>` | `rocm6.3` |
| Intel XPU | `xpu` | `xpu` |

Wheels assigned to any other channel fail collection unless that channel is
explicitly ignored by the repository. An optional repository `channels` list
adds a further restriction; repositories do not need to repeat the global
allowlist.

## Repositories

A repository entry admits one GitHub repository and an explicit project list:

```toml
[[repository]]
repository = "astral-sh-build/build-vllm"
projects = ["vllm"]
```

Project names are normalized according to Python package naming rules. A wheel
whose project is not admitted by its repository is excluded.

Repository settings:

| Setting | Default | Purpose |
| --- | --- | --- |
| `repository` | Required | GitHub repository in `owner/name` form |
| `projects` | Required | Projects admitted from release assets |
| `access` | `"private"` | Whether public anonymous fallback is permitted |
| `channels` | All configured channels | Optional additional channel restriction |
| `tag_regex` | `^(?P<version>.+)$` | Extract a policy version from a complete tag |
| `minimum_release_version` | None | Inclusive lower release-version bound |
| `allow_prereleases` | `false` | Admit parsed prerelease versions |
| `ignored_channels` | Empty | Exclude matching wheel channels |
| `unlabeled_channel_rules` | Empty | Assign bare wheels in bounded version ranges |
| `allowed_metadata_version_mismatch_tags` | Empty | Admit reviewed metadata mismatches by exact tag |

`access = "public"` controls authentication fallback, not visibility in the
published index. Repositories default to private so a newly added source cannot
silently fall back to anonymous GitHub access.

## Release tags and revisions

Repositories without a version policy treat release tags as opaque. For each
release family, collection selects the greatest trailing `-rN` revision:

```text
v1.0      revision 0
v1.0-r1   revision 1
v1.0-r2   revision 2
```

Only the selected revision contributes assets. An empty highest revision
removes that release family's files from the generated index. If the same
filename appears in multiple release families, the later-uploaded asset wins.

A repository opts into parsed version policy by configuring `tag_regex`,
`minimum_release_version`, or `unlabeled_channel_rules`. The regex uses a full
match and must contain exactly one named `version` capture. The captured value
is parsed as a Python packaging version.

Policy-enabled repositories skip:

- Tags that do not match the configured regex.
- Invalid captured versions.
- Versions below `minimum_release_version`.
- GitHub releases marked as prereleases.
- Parsed alpha, beta, release-candidate, and development versions unless
  `allow_prereleases` is enabled.

Stable post releases remain eligible.

## Artifact channels

An explicit wheel local-version label such as `+cpu` or `+cu128` is
authoritative. Compound local versions use their leading channel label.

`ignored_channels` excludes matching wheels before the global publication
allowlist is enforced. An ignored channel therefore does not need a global
`[[channel]]` declaration.

Bare wheels require a bounded unlabeled-channel rule:

```toml
unlabeled_channel_rules = [
  { from = "0.9.1", before = "0.12.0", channel = "cu128" },
  { from = "0.12.0", before = "0.20.0", channel = "cu129" },
]
```

Each rule requires both `from` and `before`. Ranges are half-open, nonempty,
nonoverlapping, and must reference a configured channel. Gaps are allowed, but
collection fails if an unlabeled wheel falls into one. This forces a deliberate
review when a producer changes its implicit hardware default. Rules use the
version captured from the original release tag, not the wheel version.

## Wheel metadata policy

Mirroring extracts the exact `.dist-info/METADATA` bytes without modifying the
wheel. The normalized metadata project name must match the filename project.
The public metadata version must match the filename version; its local version
may be omitted or may be a dot-delimited prefix of the filename's local
version.

Known producer defects can be admitted only by exact release tag:

```toml
allowed_metadata_version_mismatch_tags = [
  "v0.18.4-r1",
]
```

Every use is logged. This setting preserves the original bytes; it does not
make a mismatched wheel acceptable to installers that enforce metadata and
filename agreement.

Some legacy producers published invalid wheel filenames containing repeated
local-version `+` separators. Those filenames are normalized only for the
index-facing package filename. Their source URLs and mirrored wheel bytes remain
unchanged.

## Upstream vLLM

The production configuration keeps Astral and upstream vLLM entries adjacent.
Astral builds remain authoritative for CPU wheels. Stable upstream releases are
included from `v0.9.1`, upstream CPU wheels are ignored, and bare upstream
wheels use reviewed CUDA mappings with a deliberately bounded final range.

Extending that final range requires confirming the new upstream default CUDA
version. An unknown bare wheel outside the reviewed ranges fails collection
before publication.
