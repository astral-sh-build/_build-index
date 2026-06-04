from pathlib import Path

import pytest

from build_index.config import ConfigError, load_config

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "config" / "index.toml"
ASTRAL_SH_BUILD_CONFIG = ROOT / "config" / "astral-sh-build.toml"


def test_active_config_starts_without_repositories() -> None:
    config = load_config(CONFIG)

    assert config.site.base_url == "https://build-index.invalid"
    assert {channel.name for channel in config.channels} == {
        "cpu",
        "cu121",
        "cu124",
        "cu126",
        "cu128",
        "cu129",
        "cu130",
    }
    assert config.repositories == ()


def test_real_producer_evaluation_config() -> None:
    config = load_config(ASTRAL_SH_BUILD_CONFIG)

    assert {channel.name for channel in config.channels} >= {"cpu", "cu128"}
    assert all(channel.name != "pypi" for channel in config.channels)
    assert all(
        repository.repository != "astral-sh-build/build-rdkit"
        for repository in config.repositories
    )
    assert len(config.repositories) == 23


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
