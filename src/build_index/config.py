"""Index source and channel configuration."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_CHANNEL_PATTERN = re.compile(r"^(?:cpu|xpu|cu[0-9]+|rocm[0-9]+\.[0-9]+)$")
_DEFAULT_TAG_REGEX = r"^(?P<version>.+)$"
_REPOSITORY_ACCESS = frozenset({"private", "public"})


class ConfigError(ValueError):
    """Raised when index configuration is invalid."""


@dataclass(frozen=True)
class ChannelConfig:
    name: str
    description: str


@dataclass(frozen=True)
class UnlabeledChannelRule:
    from_version: Version
    before_version: Version
    channel: str

    def contains(self, version: Version) -> bool:
        return self.from_version <= version < self.before_version


@dataclass(frozen=True)
class RepositoryConfig:
    repository: str
    projects: tuple[str, ...]
    channels: tuple[str, ...] | None = None
    access: str = "private"
    tag_regex: str = _DEFAULT_TAG_REGEX
    minimum_release_version: Version | None = None
    maximum_release_version: Version | None = None
    allow_prereleases: bool = False
    ignored_channels: tuple[str, ...] = ()
    allowed_metadata_version_mismatch_tags: tuple[str, ...] = ()
    unlabeled_channel_rules: tuple[UnlabeledChannelRule, ...] = ()
    has_version_policy: bool = False


@dataclass(frozen=True)
class IndexConfig:
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

    _expect_keys(data, {"schema_version", "channel", "repository"}, "root")
    if _required(data, "schema_version", int, "root") != 1:
        raise ConfigError("root.schema_version must be exactly 1")

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
        _validate_repository_channels(repository, channel_names)

    return IndexConfig(
        channels=channels,
        repositories=repositories,
    )


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
        {
            "repository",
            "projects",
            "channels",
            "access",
            "tag_regex",
            "minimum_release_version",
            "maximum_release_version",
            "allow_prereleases",
            "ignored_channels",
            "allowed_metadata_version_mismatch_tags",
            "unlabeled_channel_rules",
        },
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

    access = data.get("access", "private")
    if not isinstance(access, str) or access not in _REPOSITORY_ACCESS:
        raise ConfigError(
            f"{context}.access must be one of: {', '.join(sorted(_REPOSITORY_ACCESS))}"
        )

    tag_regex = data.get("tag_regex", _DEFAULT_TAG_REGEX)
    if not isinstance(tag_regex, str):
        raise ConfigError(f"{context}.tag_regex must be a string")
    _validate_tag_regex(tag_regex, context)

    minimum_release_version = (
        _version(data["minimum_release_version"], f"{context}.minimum_release_version")
        if "minimum_release_version" in data
        else None
    )
    maximum_release_version = (
        _version(data["maximum_release_version"], f"{context}.maximum_release_version")
        if "maximum_release_version" in data
        else None
    )
    if (
        minimum_release_version is not None
        and maximum_release_version is not None
        and minimum_release_version > maximum_release_version
    ):
        raise ConfigError(
            f"{context}.minimum_release_version must not be greater than "
            f"{context}.maximum_release_version"
        )

    allow_prereleases = data.get("allow_prereleases", False)
    if not isinstance(allow_prereleases, bool):
        raise ConfigError(f"{context}.allow_prereleases must be a boolean")

    ignored_channels = (
        _string_tuple(data, "ignored_channels", context)
        if "ignored_channels" in data
        else ()
    )
    _require_unique(ignored_channels, f"{context} ignored channel")

    allowed_metadata_version_mismatch_tags = (
        _string_tuple(data, "allowed_metadata_version_mismatch_tags", context)
        if "allowed_metadata_version_mismatch_tags" in data
        else ()
    )
    if any(not tag for tag in allowed_metadata_version_mismatch_tags):
        raise ConfigError(
            f"{context}.allowed_metadata_version_mismatch_tags must not contain "
            "empty tags"
        )
    _require_unique(
        allowed_metadata_version_mismatch_tags,
        f"{context} allowed metadata version mismatch tag",
    )

    unlabeled_channel_rules = _load_unlabeled_channel_rules(
        data.get("unlabeled_channel_rules", []),
        context,
    )
    has_version_policy = any(
        key in data
        for key in (
            "tag_regex",
            "minimum_release_version",
            "maximum_release_version",
            "unlabeled_channel_rules",
        )
    )
    return RepositoryConfig(
        repository=repository,
        projects=projects,
        channels=channels,
        access=access,
        tag_regex=tag_regex,
        minimum_release_version=minimum_release_version,
        maximum_release_version=maximum_release_version,
        allow_prereleases=allow_prereleases,
        ignored_channels=ignored_channels,
        allowed_metadata_version_mismatch_tags=(allowed_metadata_version_mismatch_tags),
        unlabeled_channel_rules=unlabeled_channel_rules,
        has_version_policy=has_version_policy,
    )


def private_repository_scope(config: IndexConfig) -> tuple[str, tuple[str, ...]]:
    """Return one GitHub App owner and private scope, or an empty public-only scope."""
    private = sorted(
        (
            repository.repository.split("/", maxsplit=1)
            for repository in config.repositories
            if repository.access == "private"
        ),
        key=lambda item: (item[0], item[1]),
    )
    if not private:
        return "", ()
    owners = {owner for owner, _name in private}
    if len(owners) != 1:
        raise ConfigError(
            "private repositories must share one GitHub App installation owner: "
            f"{', '.join(sorted(owners))}"
        )
    return private[0][0], tuple(name for _owner, name in private)


def _load_unlabeled_channel_rules(
    value: Any,
    context: str,
) -> tuple[UnlabeledChannelRule, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{context}.unlabeled_channel_rules must be an array")

    rules = []
    for index, data in enumerate(value):
        rule_context = f"{context}.unlabeled_channel_rules[{index}]"
        if not isinstance(data, dict):
            raise ConfigError(f"{rule_context} must be a table")
        _expect_keys(data, {"from", "before", "channel"}, rule_context)
        from_version = _version(
            _required(data, "from", str, rule_context),
            f"{rule_context}.from",
        )
        before_version = _version(
            _required(data, "before", str, rule_context),
            f"{rule_context}.before",
        )
        if from_version >= before_version:
            raise ConfigError(
                f"{rule_context} must define a nonempty range with from < before"
            )
        rules.append(
            UnlabeledChannelRule(
                from_version=from_version,
                before_version=before_version,
                channel=_required(data, "channel", str, rule_context),
            )
        )

    sorted_rules = sorted(
        rules,
        key=lambda rule: (rule.from_version, rule.before_version, rule.channel),
    )
    for previous, current in zip(sorted_rules, sorted_rules[1:], strict=False):
        if current.from_version < previous.before_version:
            raise ConfigError(
                f"{context}.unlabeled_channel_rules contains overlapping ranges: "
                f"{previous.from_version}..<"
                f"{previous.before_version} and "
                f"{current.from_version}..<"
                f"{current.before_version}"
            )
    return tuple(sorted_rules)


def _validate_repository_channels(
    repository: RepositoryConfig,
    configured: set[str],
) -> None:
    for channel in repository.ignored_channels:
        if not _CHANNEL_PATTERN.fullmatch(channel):
            raise ConfigError(
                f"repository {repository.repository!r} ignored channel is not "
                f"canonical: {channel!r}"
            )

    references = {rule.channel for rule in repository.unlabeled_channel_rules}
    if repository.channels is not None:
        references.update(repository.channels)
    unknown = sorted(references - configured)
    if unknown:
        raise ConfigError(
            f"repository {repository.repository!r} references unknown channels: "
            f"{', '.join(unknown)}"
        )


def _validate_tag_regex(value: str, context: str) -> None:
    try:
        pattern = re.compile(value)
    except re.error as error:
        raise ConfigError(f"{context}.tag_regex is invalid: {error}") from error
    if pattern.groups != 1 or pattern.groupindex != {"version": 1}:
        raise ConfigError(
            f"{context}.tag_regex must contain exactly one named 'version' capture"
        )


def _version(value: Any, context: str) -> Version:
    if not isinstance(value, str):
        raise ConfigError(f"{context} must be a string")
    try:
        return Version(value)
    except InvalidVersion as error:
        raise ConfigError(f"{context} is not a valid version: {value!r}") from error


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
