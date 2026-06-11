# GitHub App

`_build-index` uses the private `build-index-reader` GitHub App while producer
repositories remain private. A normal workflow `GITHUB_TOKEN` cannot read
sibling private repositories.

The App has one permission:

| Installed on | Permission | Purpose |
| --- | --- | --- |
| Approved producer repositories | `Contents: Read` | List releases and download release assets |

It has no webhooks, callback URLs, user authorization flow, or write
permissions. Installing it on a producer repository is the access grant; the
producer does not store credentials or run an authentication workflow.

## Registration

The reference manifest is [`reader.manifest.json`](reader.manifest.json). To
register or replace the App, run:

```bash
uv run --locked python scripts/register_reader_app.py
```

The helper opens GitHub's App registration flow, receives the one-time callback
on `127.0.0.1`, validates its state, and exchanges the registration code. It
writes the returned credentials outside the repository with file mode `0600`
and prints the installation and Actions-configuration commands.

The browser must be authenticated as a user allowed to create GitHub Apps for
the organization. Use `--no-browser` to print the registration URL instead.
The helper does not install the App or upload credentials; install it only on
approved producer repositories, then move the private key to secret storage or
delete the local credential file.

## Actions configuration

Configure `_build-index` with:

| Name | GitHub type | Value |
| --- | --- | --- |
| `BUILD_INDEX_READER_CLIENT_ID` | Variable | App client ID |
| `BUILD_INDEX_READER_PRIVATE_KEY` | Secret | App private key |

At runtime, the publication workflow derives an explicit list of configured
private repositories. [`actions/create-reader-token`](../actions/create-reader-token/action.yml)
wraps the pinned `actions/create-github-app-token` action and hardcodes
`Contents: Read`, producing a short-lived installation token for only that
list.

The token is preferred for GitHub API reads. A public repository outside the
installation retries anonymously only when GitHub rejects repository access.
Private sources, rate limits, server errors, and malformed responses never use
anonymous fallback. If configuration has no private sources, token creation is
skipped.

When every producer repository is public and configured with
`access = "public"`, the App, local wrapper action, credentials, manifest, and
registration helper can be removed.
