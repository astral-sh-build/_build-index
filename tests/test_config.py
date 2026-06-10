from dataclasses import replace
from pathlib import Path

import pytest

from build_index.config import ConfigError, load_config, private_repository_scope

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "config" / "index.toml"
EXPECTED_REPOSITORIES = (
    ("astral-sh-build/build-adan", ("adan",)),
    ("astral-sh-build/build-apex", ("apex",)),
    ("astral-sh-build/build-causal-conv1d", ("causal-conv1d",)),
    ("astral-sh-build/build-deepep", ("deep-ep",)),
    ("astral-sh-build/build-deepgemm", ("deep-gemm",)),
    ("astral-sh-build/build-detectron2", ("detectron2",)),
    ("astral-sh-build/build-ffmpeg", ("ffmpeg",)),
    ("astral-sh-build/build-flash-attention", ("flash-attn",)),
    ("astral-sh-build/build-flash-attention-3", ("flash-attn-3",)),
    ("astral-sh-build/build-grouped-gemm", ("grouped-gemm",)),
    ("astral-sh-build/build-megablocks", ("megablocks",)),
    ("astral-sh-build/build-mmcv", ("mmcv",)),
    ("astral-sh-build/build-nvmolkit", ("nvmolkit",)),
    (
        "astral-sh-build/build-opencv",
        ("opencv-contrib-python-headless", "opencv-python-headless"),
    ),
    ("astral-sh-build/build-pycuda", ("pycuda",)),
    ("astral-sh-build/build-pytorch-scatter", ("torch-scatter",)),
    ("astral-sh-build/build-pytorch3d", ("pytorch3d",)),
    ("astral-sh-build/build-sageattention", ("sageattention",)),
    ("astral-sh-build/build-sageattention3", ("sageattn3",)),
    (
        "astral-sh-build/build-transformer-engine-torch",
        ("transformer-engine-torch",),
    ),
    ("astral-sh-build/build-vllm", ("vllm",)),
    ("vllm-project/vllm", ("vllm",)),
)


def test_active_config_matches_validated_producer_inventory() -> None:
    config = load_config(CONFIG)

    assert config.site.base_url == "https://build-index.invalid"
    assert {channel.name for channel in config.channels} == {
        "cpu",
        "cu121",
        "cu124",
        "cu126",
        "cu118",
        "cu128",
        "cu129",
        "cu130",
    }
    assert [
        (repository.repository, repository.projects)
        for repository in config.repositories
    ] == list(EXPECTED_REPOSITORIES)
    assert all(repository.channels is None for repository in config.repositories)
    upstream = config.repositories[-1]
    assert upstream.access == "public"
    assert str(upstream.minimum_release_version) == "0.9.1"
    assert upstream.ignored_channels == ("cpu",)
    assert tuple(rule.channel for rule in upstream.unlabeled_channel_rules) == (
        "cu128",
        "cu129",
        "cu130",
    )


def test_active_config_is_limited_to_r2_mirroring_trial() -> None:
    config = load_config(CONFIG)

    assert {channel.name for channel in config.channels} >= {"cpu", "cu128"}
    assert all(channel.name != "pypi" for channel in config.channels)
    assert len(config.repositories) == len(EXPECTED_REPOSITORIES)
    assert {repository.repository for repository in config.repositories} == {
        repository for repository, _projects in EXPECTED_REPOSITORIES
    }


def test_config_rejects_noncanonical_channel_name(tmp_path: Path) -> None:
    path = tmp_path / "index.toml"
    path.write_text(
        CONFIG.read_text(encoding="utf-8").replace(
            'name = "cu128"', 'name = "cuda128"', 1
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="not a canonical channel name"):
        load_config(path)


def test_config_rejects_repository_unknown_channel(tmp_path: Path) -> None:
    path = tmp_path / "index.toml"
    path.write_text(
        CONFIG.read_text(encoding="utf-8")
        + """

[[repository]]
repository = "astral-sh-build/build-example"
projects = ["example"]
channels = ["cu999"]
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="unknown channels: cu999"):
        load_config(path)


def test_config_accepts_optional_repository_channel_restriction(
    tmp_path: Path,
) -> None:
    path = tmp_path / "index.toml"
    path.write_text(
        CONFIG.read_text(encoding="utf-8")
        + """

[[repository]]
repository = "astral-sh-build/build-example"
projects = ["example"]
channels = ["cpu"]
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.repositories[-1].channels == ("cpu",)


def test_config_rejects_non_normalized_project_name(tmp_path: Path) -> None:
    path = tmp_path / "index.toml"
    path.write_text(
        CONFIG.read_text(encoding="utf-8")
        + """

[[repository]]
repository = "astral-sh-build/build-example"
projects = ["Example_Package"]
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="non-normalized name"):
        load_config(path)


def test_repository_policy_defaults_to_private_opaque_tags() -> None:
    config = load_config(CONFIG)
    opaque_private = tuple(
        repository
        for repository in config.repositories
        if repository.access == "private" and not repository.has_version_policy
    )

    assert all(
        repository.tag_regex == "^(?P<version>.+)$" for repository in opaque_private
    )
    assert all(repository.allow_prereleases is False for repository in opaque_private)

    versioned_private = tuple(
        repository
        for repository in config.repositories
        if repository.access == "private" and repository.has_version_policy
    )
    assert tuple(repository.repository for repository in versioned_private) == (
        "astral-sh-build/build-causal-conv1d",
    )


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        ('tag_regex = "("\n', "tag_regex is invalid"),
        ('tag_regex = "^v(.+)$"\n', "named 'version' capture"),
        (
            'tag_regex = "^(?P<version>.+)-(?P<other>.+)$"\n',
            "named 'version' capture",
        ),
    ],
)
def test_config_rejects_invalid_tag_regex(
    tmp_path: Path,
    policy: str,
    message: str,
) -> None:
    path = tmp_path / "index.toml"
    path.write_text(
        CONFIG.read_text(encoding="utf-8")
        + f"""

[[repository]]
repository = "example/project"
projects = ["example"]
{policy}
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=message):
        load_config(path)


@pytest.mark.parametrize(
    ("rules", "message"),
    [
        (
            '[{ from = "1.0", channel = "cu128" }]',
            "before is required",
        ),
        (
            '[{ from = "1.0", before = "1.0", channel = "cu128" }]',
            "nonempty range",
        ),
        (
            (
                '[{ from = "1.0", before = "2.0", channel = "cu128" }, '
                '{ from = "1.5", before = "3.0", channel = "cu129" }]'
            ),
            "overlapping ranges",
        ),
        (
            '[{ from = "1.0", before = "2.0", channel = "cu999" }]',
            "unknown channels: cu999",
        ),
    ],
)
def test_config_rejects_invalid_unlabeled_channel_rules(
    tmp_path: Path,
    rules: str,
    message: str,
) -> None:
    path = tmp_path / "index.toml"
    path.write_text(
        CONFIG.read_text(encoding="utf-8")
        + f"""

[[repository]]
repository = "example/project"
projects = ["example"]
unlabeled_channel_rules = {rules}
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_config_allows_gaps_between_unlabeled_channel_rules(
    tmp_path: Path,
) -> None:
    path = tmp_path / "index.toml"
    path.write_text(
        CONFIG.read_text(encoding="utf-8")
        + """

[[repository]]
repository = "example/project"
projects = ["example"]
unlabeled_channel_rules = [
  { from = "1.0", before = "2.0", channel = "cu128" },
  { from = "3.0", before = "4.0", channel = "cu129" },
]
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert len(config.repositories[-1].unlabeled_channel_rules) == 2


def test_config_allows_ignored_channel_outside_public_channel_list(
    tmp_path: Path,
) -> None:
    path = tmp_path / "index.toml"
    path.write_text(
        CONFIG.read_text(encoding="utf-8")
        + """

[[repository]]
repository = "example/project"
projects = ["example"]
ignored_channels = ["rocm6.3"]
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.repositories[-1].ignored_channels == ("rocm6.3",)


def test_config_rejects_noncanonical_ignored_channel(tmp_path: Path) -> None:
    path = tmp_path / "index.toml"
    path.write_text(
        CONFIG.read_text(encoding="utf-8")
        + """

[[repository]]
repository = "example/project"
projects = ["example"]
ignored_channels = ["cuda12.8"]
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="ignored channel is not canonical"):
        load_config(path)


def test_private_repository_scope_excludes_public_sources() -> None:
    config = load_config(CONFIG)
    owner, repositories = private_repository_scope(config)

    assert owner == "astral-sh-build"
    assert repositories == tuple(
        repository.removeprefix("astral-sh-build/")
        for repository, _projects in EXPECTED_REPOSITORIES
        if repository.startswith("astral-sh-build/")
    )


def test_private_repository_scope_allows_public_only_config() -> None:
    config = load_config(CONFIG)
    public = replace(
        config.repositories[0],
        repository="vllm-project/vllm",
        access="public",
    )

    owner, repositories = private_repository_scope(
        replace(config, repositories=(public,))
    )

    assert owner == ""
    assert repositories == ()
