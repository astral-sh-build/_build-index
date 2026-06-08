# Channel Naming

Channels are assigned per wheel. A producer release may contain wheels for
multiple channels.

The global channel list is the publication allowlist. Repository entries do
not need to repeat it: `_build-index` infers a wheel's channel from its version
marker. An optional repository `channels` list acts only as an additional
restriction.

## Canonical Names

| Kind | Name | Example |
| --- | --- | --- |
| CPU | `cpu` | `cpu` |
| CUDA | `cu<major><minor>` | `cu128` |
| ROCm | `rocm<major>.<minor>` | `rocm6.3` |
| Intel XPU | `xpu` | `xpu` |

Only channels listed in `config/index.toml` are public. The initial
configuration includes CPU and observed first-party CUDA channels. ROCm and XPU
naming is reserved but those channels are not enabled until a producer needs
them.

Explicit local-version labels always select their corresponding configured
channel. A repository may define bounded `unlabeled_channel_rules` for wheels
whose versions have no local channel label. Those rules are evaluated against
the version captured from the original GitHub release tag, not the wheel
version. Gaps are allowed deliberately, but collection fails if an unlabeled
wheel lands in one.
