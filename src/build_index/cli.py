"""Command line interface for collecting releases and building indexes."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from build_index.collection import CollectionError, load_collection, write_collection
from build_index.config import ConfigError, load_config, private_repository_scope
from build_index.github import GitHubClient, collect_release_assets
from build_index.pages import build_pages

DEFAULT_CONFIG = Path("config/index.toml")
DEFAULT_COLLECTION = Path("build/releases.json")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect GitHub release wheels and build static package indexes."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate-config", help="Validate index configuration."
    )
    validate_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)

    collect_parser = subparsers.add_parser(
        "collect", help="Collect wheels from configured GitHub Releases."
    )
    collect_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    collect_parser.add_argument("--output", type=Path, default=DEFAULT_COLLECTION)
    collect_parser.add_argument(
        "--token-env",
        default="GH_TOKEN",
        help="Environment variable containing a GitHub token.",
    )
    collect_parser.add_argument(
        "--api-url",
        default="https://api.github.com",
        help="GitHub REST API base URL.",
    )

    scope_parser = subparsers.add_parser(
        "reader-token-scope",
        help="Write the private repository scope for the reader GitHub App token.",
    )
    scope_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    scope_parser.add_argument(
        "--github-output",
        type=Path,
        help="Write owner and repositories as GitHub Actions step outputs.",
    )

    build_parser = subparsers.add_parser(
        "build", help="Build static JSON and HTML Simple API documents."
    )
    build_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    build_parser.add_argument("--collection", type=Path, default=DEFAULT_COLLECTION)
    build_parser.add_argument("--output", type=Path, default=Path("dist"))
    build_parser.add_argument(
        "--base-url",
        help="Override the configured public base URL.",
    )

    args = parser.parse_args()
    try:
        if args.command == "validate-config":
            config = load_config(args.config)
            print(
                f"valid configuration: {len(config.channels)} channels, "
                f"{len(config.repositories)} repositories"
            )
        elif args.command == "collect":
            config = load_config(args.config)
            token = os.environ.get(args.token_env)
            if not token and any(
                repository.access == "private" for repository in config.repositories
            ):
                raise CollectionError(
                    f"GitHub token environment variable is not set: {args.token_env}"
                )
            collection = collect_release_assets(
                config,
                GitHubClient(token, api_url=args.api_url),
                log=print,
            )
            write_collection(args.output, collection)
            print(
                f"wrote release collection: {args.output}, "
                f"{len(collection.artifacts)} wheel assets"
            )
        elif args.command == "reader-token-scope":
            config = load_config(args.config)
            owner, repositories = private_repository_scope(config)
            if args.github_output is None:
                print(owner)
                print("\n".join(repositories))
            else:
                with args.github_output.open("a", encoding="utf-8") as output:
                    output.write(
                        f"has_private_repositories={str(bool(repositories)).lower()}\n"
                    )
                    output.write(f"owner={owner}\n")
                    if repositories:
                        output.write("repositories<<__BUILD_INDEX_REPOSITORIES__\n")
                        output.write("\n".join(repositories) + "\n")
                        output.write("__BUILD_INDEX_REPOSITORIES__\n")
                    else:
                        output.write("repositories=\n")
        elif args.command == "build":
            config = load_config(args.config)
            collection = load_collection(args.collection)
            written = build_pages(
                config,
                args.output,
                collection=collection,
                base_url=args.base_url,
            )
            print(
                f"built index tree: {len(written)} files, "
                f"{len(config.channels)} channels, "
                f"{len(collection.artifacts)} wheel assets"
            )
    except (CollectionError, ConfigError, ValueError) as error:
        parser.error(str(error))
