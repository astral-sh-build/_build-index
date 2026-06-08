from dataclasses import replace
from pathlib import Path

import pytest

from build_index.config import ConfigError, load_config, private_repository_scope

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "config" / "index.toml"


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
    assert len(config.repositories) == 24
    assert all(repository.channels is None for repository in config.repositories)


def test_active_config_upstream_vllm_policy() -> None:
    config = load_config(CONFIG)

    assert {channel.name for channel in config.channels} >= {"cpu", "cu128"}
    assert all(channel.name != "pypi" for channel in config.channels)
    assert all(
        repository.repository != "astral-sh-build/build-rdkit"
        for repository in config.repositories
    )
    upstream = next(
        repository
        for repository in config.repositories
        if repository.repository == "vllm-project/vllm"
    )
    assert upstream.access == "public"
    assert upstream.tag_regex == "^v(?P<version>.+)$"
    assert str(upstream.minimum_release_version) == "0.9.1"
    assert upstream.ignored_channels == ("cpu",)
    assert [
        (str(rule.from_version), str(rule.before_version), rule.channel)
        for rule in upstream.unlabeled_channel_rules
    ] == [
        ("0.9.1", "0.12.0", "cu128"),
        ("0.12.0", "0.20.0", "cu129"),
        ("0.20.0", "0.23.0", "cu130"),
    ]
    assert len(config.repositories) == 24


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
    private = tuple(
        repository
        for repository in config.repositories
        if repository.access == "private"
    )

    assert len(private) == 23
    assert all(repository.tag_regex == "^(?P<version>.+)$" for repository in private)
    assert all(repository.has_version_policy is False for repository in private)
    assert all(repository.allow_prereleases is False for repository in private)


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
    assert "build-vllm" in repositories
    assert "vllm" not in repositories


def test_private_repository_scope_allows_public_only_config() -> None:
    config = load_config(CONFIG)
    public = tuple(
        repository
        for repository in config.repositories
        if repository.access == "public"
    )

    owner, repositories = private_repository_scope(replace(config, repositories=public))

    assert owner == ""
    assert repositories == ()
