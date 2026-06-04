"""Command line interface for collecting releases and building indexes."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from build_index.collection import CollectionError, load_collection, write_collection
from build_index.config import ConfigError, load_config
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
            token = os.environ.get(args.token_env)
            if not token:
                raise CollectionError(
                    f"GitHub token environment variable is not set: {args.token_env}"
                )
            config = load_config(args.config)
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
