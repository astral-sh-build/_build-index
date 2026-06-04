"""Index source and channel configuration."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from packaging.utils import canonicalize_name

_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_CHANNEL_PATTERN = re.compile(r"^(?:cpu|xpu|cu[0-9]+|rocm[0-9]+\.[0-9]+)$")


class ConfigError(ValueError):
    """Raised when index configuration is invalid."""


@dataclass(frozen=True)
class SiteConfig:
    base_url: str


@dataclass(frozen=True)
class ChannelConfig:
    name: str
    description: str


@dataclass(frozen=True)
class RepositoryConfig:
    repository: str
    projects: tuple[str, ...]
    channels: tuple[str, ...] | None = None
    allow_prereleases: bool = False


@dataclass(frozen=True)
class IndexConfig:
    site: SiteConfig
    channels: tuple[ChannelConfig, ...]
    repositories: tuple[RepositoryConfig, ...]

    def repository(self, name: str) -> RepositoryConfig | None:
        return next(
            (
                repository
                for repository in self.repositories
                if repository.repository == name
            ),
            None,
        )


def load_config(path: Path) -> IndexConfig:
    """Load and validate one index configuration file."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"could not load {path}: {error}") from error

    _expect_keys(data, {"schema_version", "site", "channel", "repository"}, "root")
    if _required(data, "schema_version", int, "root") != 1:
        raise ConfigError("root.schema_version must be exactly 1")

    site = _load_site(_required(data, "site", dict, "root"))
    channels = tuple(
        _load_channel(entry, index)
        for index, entry in enumerate(data.get("channel", []))
    )
    repositories = tuple(
        _load_repository(entry, index)
        for index, entry in enumerate(data.get("repository", []))
    )

    _require_unique((channel.name for channel in channels), "channel name")
    _require_unique(
        (repository.repository for repository in repositories), "repository name"
    )
    channel_names = {channel.name for channel in channels}
    for repository in repositories:
        if repository.channels is None:
            continue
        unknown = sorted(set(repository.channels) - channel_names)
        if unknown:
            raise ConfigError(
                f"repository {repository.repository!r} references unknown channels: "
                f"{', '.join(unknown)}"
            )

    return IndexConfig(
        site=site,
        channels=channels,
        repositories=repositories,
    )


def _load_site(data: dict[str, Any]) -> SiteConfig:
    _expect_keys(data, {"base_url"}, "site")
    base_url = _required(data, "base_url", str, "site").rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ConfigError(
            "site.base_url must be an HTTPS URL without query or fragment"
        )
    return SiteConfig(base_url=base_url)


def _load_channel(data: Any, index: int) -> ChannelConfig:
    context = f"channel[{index}]"
    if not isinstance(data, dict):
        raise ConfigError(f"{context} must be a table")
    _expect_keys(data, {"name", "description"}, context)
    name = _required(data, "name", str, context)
    if not _CHANNEL_PATTERN.fullmatch(name):
        raise ConfigError(f"{context}.name is not a canonical channel name: {name!r}")
    return ChannelConfig(
        name=name,
        description=_required(data, "description", str, context),
    )


def _load_repository(data: Any, index: int) -> RepositoryConfig:
    context = f"repository[{index}]"
    if not isinstance(data, dict):
        raise ConfigError(f"{context} must be a table")
    _expect_keys(
        data,
        {"repository", "projects", "channels", "allow_prereleases"},
        context,
    )
    repository = _required(data, "repository", str, context)
    if not _REPOSITORY_PATTERN.fullmatch(repository):
        raise ConfigError(f"{context}.repository must use 'owner/name'")

    projects = _string_tuple(data, "projects", context)
    channels = _string_tuple(data, "channels", context) if "channels" in data else None
    if not projects:
        raise ConfigError(f"{context}.projects must not be empty")
    _require_unique(projects, f"{context} project")
    if channels is not None:
        if not channels:
            raise ConfigError(f"{context}.channels must not be empty when specified")
        _require_unique(channels, f"{context} channel")
    for project in projects:
        if canonicalize_name(project) != project:
            raise ConfigError(
                f"{context}.projects contains non-normalized name {project!r}"
            )

    allow_prereleases = data.get("allow_prereleases", False)
    if not isinstance(allow_prereleases, bool):
        raise ConfigError(f"{context}.allow_prereleases must be a boolean")
    return RepositoryConfig(
        repository=repository,
        projects=projects,
        channels=channels,
        allow_prereleases=allow_prereleases,
    )


def _required(data: dict[str, Any], key: str, expected_type: type, context: str) -> Any:
    if key not in data:
        raise ConfigError(f"{context}.{key} is required")
    value = data[key]
    if not isinstance(value, expected_type):
        raise ConfigError(f"{context}.{key} must be a {expected_type.__name__}")
    return value


def _string_tuple(data: dict[str, Any], key: str, context: str) -> tuple[str, ...]:
    value = _required(data, key, list, context)
    if not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{context}.{key} must contain only strings")
    return tuple(value)


def _expect_keys(data: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"{context} contains unknown keys: {', '.join(unknown)}")


def _require_unique(values: Any, label: str) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        raise ConfigError(f"duplicate {label}: {', '.join(sorted(duplicates))}")
