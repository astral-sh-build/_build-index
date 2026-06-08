# GitHub App

`_build-index` uses one GitHub App to read releases from approved private
producer repositories:

| App | Installed on | Permission | Purpose |
| --- | --- | --- | --- |
| `build-index-reader` | Approved producer repositories | `Contents: Read` | Allows `_build-index` to list releases and download release assets |

The reference GitHub App manifest is
[`reader.manifest.json`](reader.manifest.json).

Installing the App on a producer repository is the access grant. A GitHub
Action cannot grant `_build-index` durable access to another private
repository. Producer repositories do not run an authentication action, store
reader credentials, or send their `GITHUB_TOKEN` to `_build-index`.

The App deliberately defines no webhook URL, webhook events, callback URLs,
user authorization flow, or write permissions. `_build-index` polls configured
repositories on a schedule.

## Registration

GitHub App manifests are inputs to a registration handshake, not files that
GitHub automatically discovers in a repository. A registration helper must:

1. Add a temporary `redirect_url` to the selected manifest.
2. POST the JSON-encoded manifest to:
   `https://github.com/organizations/astral-sh-build/settings/apps/new`.
3. Receive GitHub's temporary `code` at the redirect URL.
4. Exchange the code with `POST /app-manifests/{code}/conversions` within one
   hour.
5. Store the returned App credentials securely.
6. Install the App with access limited to the approved producer repositories.

The App name is a suggestion and may need to change because GitHub App names
are globally unique. Because the App is private, it can only be installed on
repositories owned by `astral-sh-build`.

### Registration Helper

Run the local helper from the `_build-index` repository:

```bash
uv run --locked python scripts/register_reader_app.py
```

The helper:

1. Starts a callback server bound only to `127.0.0.1`.
2. Adds a temporary callback URL and opens the GitHub registration page in the
   default browser.
3. Validates the callback state and exchanges GitHub's one-time code.
4. Saves the complete credential response outside the repository with file mode
   `0600`.
5. Prints the App installation URL and the commands needed to configure the
   `_build-index` Actions variable and secret.

The default creates an App owned by `astral-sh-build`.

The script does not install the App or upload credentials automatically. The
browser must be authenticated as a user allowed to create GitHub Apps for the
selected organization. Use `--no-browser` to print the local registration URL
instead of opening it. The output file contains the App private key and other
secrets returned by GitHub; move it into the appropriate secret-management
system or delete it after configuration.

## Credentials

Store the App client ID as an `_build-index` Actions variable named
`BUILD_INDEX_READER_CLIENT_ID` and its private key as an `_build-index`
Actions secret named `BUILD_INDEX_READER_PRIVATE_KEY`.

The composite action in
[`actions/create-reader-token`](../actions/create-reader-token/action.yml)
creates a short-lived installation token. It requires an explicit repository
list and hardcodes `Contents: Read`, so the token cannot silently expand to
every repository in the App installation or request write access.

The Pages branch workflow derives that repository list from configured private
sources only, then uses the action before release collection:

```yaml
- name: Resolve private producer scope
  id: producer-scope
  run: |
    uv run --locked build-index reader-token-scope \
      --github-output "$GITHUB_OUTPUT"

- name: Create producer repository token
  if: steps.producer-scope.outputs.has_private_repositories == 'true'
  id: producer-token
  uses: ./actions/create-reader-token
  with:
    client-id: ${{ vars.BUILD_INDEX_READER_CLIENT_ID }}
    private-key: ${{ secrets.BUILD_INDEX_READER_PRIVATE_KEY }}
    owner: ${{ steps.producer-scope.outputs.owner }}
    repositories: ${{ steps.producer-scope.outputs.repositories }}

- name: Collect producer releases
  env:
    GH_TOKEN: ${{ steps.producer-token.outputs.token || github.token }}
  run: uv run --locked build-index collect
```

The collector still prefers this token for configured public repositories.
When an installation token receives a repository-access rejection for a public
source outside the App installation, that request is retried anonymously.
Rate limits, server errors, malformed responses, and private-source failures
are never retried anonymously. If the configuration has no private sources,
the workflow skips token creation and collection proceeds with public access.
