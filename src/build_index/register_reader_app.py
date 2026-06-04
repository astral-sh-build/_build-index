"""Register the build-index reader GitHub App from its checked-in manifest."""

from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import shlex
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "github-apps" / "reader.manifest.json"
DEFAULT_OWNER = "ee-test-builds"
DEFAULT_REPOSITORY = "_build-index"
GITHUB_API_VERSION = "2026-03-10"
GITHUB_API_URL = "https://api.github.com"
GITHUB_WEB_URL = "https://github.com"


class RegistrationError(RuntimeError):
    """Raised when the GitHub App manifest registration flow fails."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Register the read-only build-index reader GitHub App and save the "
            "returned credentials outside the repository."
        )
    )
    parser.add_argument(
        "--owner",
        default=DEFAULT_OWNER,
        help=f"GitHub organization that will own the App (default: {DEFAULT_OWNER})",
    )
    parser.add_argument(
        "--repository",
        default=DEFAULT_REPOSITORY,
        help=(
            "build-index repository name used as the App homepage "
            f"(default: {DEFAULT_REPOSITORY})"
        ),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"path to the reader App manifest (default: {DEFAULT_MANIFEST})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "credential output path; defaults to "
            "~/.config/build-index/github-apps/<owner>-reader-app.json"
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="localhost callback port; 0 selects an available port (default: 0)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=900,
        help="seconds to wait for the browser callback (default: 900)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="print the local registration URL instead of opening a browser",
    )
    return parser.parse_args(argv)


def validate_name(value: str, label: str) -> str:
    if not value or "/" in value or value in {".", ".."}:
        raise RegistrationError(f"{label} must be a single non-empty GitHub name")
    return value


def load_reader_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RegistrationError(f"failed to load manifest {path}: {error}") from error

    if not isinstance(manifest, dict):
        raise RegistrationError(f"manifest {path} must contain a JSON object")

    expected = {
        "public": False,
        "request_oauth_on_install": False,
        "setup_on_update": False,
        "default_permissions": {"contents": "read"},
        "default_events": [],
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise RegistrationError(f"manifest {path} must set {key!r} to {value!r}")

    forbidden = {
        "callback_urls",
        "hook_attributes",
        "redirect_url",
        "setup_url",
    }
    present = sorted(forbidden.intersection(manifest))
    if present:
        raise RegistrationError(
            f"manifest {path} must not contain temporary fields: {', '.join(present)}"
        )

    return manifest


def prepare_manifest(
    manifest: dict[str, Any],
    *,
    owner: str,
    repository: str,
    redirect_url: str,
) -> dict[str, Any]:
    prepared = dict(manifest)
    prepared["url"] = f"{GITHUB_WEB_URL}/{owner}/{repository}"
    prepared["redirect_url"] = redirect_url
    return prepared


def registration_url(owner: str, state: str) -> str:
    encoded_owner = urllib.parse.quote(owner, safe="")
    encoded_state = urllib.parse.quote(state, safe="")
    return (
        f"{GITHUB_WEB_URL}/organizations/{encoded_owner}/settings/apps/new"
        f"?state={encoded_state}"
    )


def registration_page(action_url: str, manifest: dict[str, Any]) -> bytes:
    manifest_json = json.dumps(manifest, separators=(",", ":"))
    escaped_action = html.escape(action_url, quote=True)
    escaped_manifest = html.escape(manifest_json, quote=True)
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Register build-index reader App</title></head>
<body>
<p>Redirecting to GitHub to register the build-index reader App...</p>
<form id="register" action="{escaped_action}" method="post">
  <input type="hidden" name="manifest" value="{escaped_manifest}">
  <noscript><button type="submit">Continue to GitHub</button></noscript>
</form>
<script>document.getElementById("register").submit();</script>
</body>
</html>
""".encode()


def callback_page(message: str, *, success: bool) -> bytes:
    title = "Registration complete" if success else "Registration failed"
    escaped_title = html.escape(title)
    escaped_message = html.escape(message)
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>{escaped_title}</title></head>
<body>
<h1>{escaped_title}</h1>
<p>{escaped_message}</p>
</body>
</html>
""".encode()


def make_handler(context: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    class RegistrationHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self._respond(200, context["registration_page"])
                return

            if parsed.path != "/callback":
                self._respond(
                    404, callback_page("Unknown callback path.", success=False)
                )
                return

            query = urllib.parse.parse_qs(parsed.query)
            state = query.get("state", [None])[0]
            code = query.get("code", [None])[0]
            if state != context["state"]:
                self._respond(
                    400,
                    callback_page(
                        "GitHub callback state did not match.", success=False
                    ),
                )
                return
            elif not code:
                context["error"] = (
                    "GitHub callback did not include a registration code."
                )
                self._respond(400, callback_page(context["error"], success=False))
            else:
                context["code"] = code
                self._respond(
                    200,
                    callback_page(
                        "The one-time code was received. You can close this tab.",
                        success=True,
                    ),
                )
            context["complete"].set()

        def log_message(self, format: str, *args: object) -> None:
            return

        def _respond(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return RegistrationHandler


def wait_for_registration(
    manifest: dict[str, Any],
    *,
    owner: str,
    repository: str,
    port: int,
    timeout: float,
    open_browser: bool,
) -> str:
    state = secrets.token_urlsafe(32)
    context: dict[str, Any] = {
        "complete": threading.Event(),
        "state": state,
    }
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(context))
    callback_url = f"http://127.0.0.1:{server.server_port}/callback"
    prepared = prepare_manifest(
        manifest,
        owner=owner,
        repository=repository,
        redirect_url=callback_url,
    )
    action_url = registration_url(owner, state)
    context["registration_page"] = registration_page(action_url, prepared)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    local_url = f"http://127.0.0.1:{server.server_port}/"

    print(f"Waiting for GitHub App registration callback at {callback_url}")
    if open_browser:
        print(f"Opening {local_url}")
        if not webbrowser.open(local_url):
            print(f"Browser launch failed; open this URL manually:\n{local_url}")
    else:
        print(f"Open this URL in a browser authenticated to GitHub:\n{local_url}")

    try:
        if not context["complete"].wait(timeout):
            raise RegistrationError(
                f"timed out after {timeout:g} seconds waiting for GitHub callback"
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    if error := context.get("error"):
        raise RegistrationError(error)
    if code := context.get("code"):
        return str(code)
    raise RegistrationError("GitHub callback completed without a registration code")


def exchange_code(code: str) -> dict[str, Any]:
    encoded_code = urllib.parse.quote(code, safe="")
    request = urllib.request.Request(
        f"{GITHUB_API_URL}/app-manifests/{encoded_code}/conversions",
        data=b"",
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "build-index-reader-app-registration",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            credentials = json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")
        raise RegistrationError(
            f"GitHub manifest conversion failed with HTTP {error.code}: {body}"
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise RegistrationError(
            f"GitHub manifest conversion failed: {error}"
        ) from error

    if not isinstance(credentials, dict):
        raise RegistrationError(
            "GitHub manifest conversion returned a non-object response"
        )

    required = {"id", "client_id", "pem", "slug", "html_url"}
    missing = sorted(required.difference(credentials))
    if missing:
        raise RegistrationError(
            "GitHub manifest conversion response is missing: " + ", ".join(missing)
        )
    return credentials


def default_output_path(owner: str) -> Path:
    return (
        Path.home()
        / ".config"
        / "build-index"
        / "github-apps"
        / f"{owner}-reader-app.json"
    )


def validate_output_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.is_relative_to(ROOT):
        raise RegistrationError(
            f"refusing to write GitHub App credentials inside the repository: {resolved}"
        )
    if resolved.exists():
        raise RegistrationError(
            f"refusing to overwrite existing credential file: {resolved}"
        )
    return resolved


def reserve_credentials_file(path: Path) -> int:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
    except OSError as error:
        raise RegistrationError(
            f"failed to reserve credential output file {path}: {error}"
        ) from error
    return descriptor


def write_credentials(descriptor: int, credentials: dict[str, Any]) -> None:
    payload = (json.dumps(credentials, indent=2, sort_keys=True) + "\n").encode()
    remaining = memoryview(payload)
    try:
        while remaining:
            written = os.write(descriptor, remaining)
            if written == 0:
                raise OSError("zero-byte write")
            remaining = remaining[written:]
        os.fsync(descriptor)
    except OSError as error:
        raise RegistrationError(
            f"failed to write GitHub App credentials: {error}"
        ) from error


def discard_credentials_file(path: Path, descriptor: int) -> None:
    os.close(descriptor)
    try:
        path.unlink()
    except OSError:
        pass


def print_next_steps(
    *,
    owner: str,
    repository: str,
    output: Path,
    credentials: dict[str, Any],
) -> None:
    quoted_output = shlex.quote(str(output))
    full_repository = f"{owner}/{repository}"
    install_url = f"{str(credentials['html_url']).rstrip('/')}/installations/new"

    print()
    print(f"Created GitHub App: {credentials['slug']}")
    print(f"Saved credentials with mode 0600: {output}")
    print()
    print("Install the App on selected producer repositories:")
    print(install_url)
    print()
    print("Configure _build-index Actions credentials:")
    print(
        "gh variable set BUILD_INDEX_READER_CLIENT_ID "
        f"--repo {shlex.quote(full_repository)} "
        f'--body "$(jq -r .client_id {quoted_output})"'
    )
    print(
        f"jq -r .pem {quoted_output} | "
        "gh secret set BUILD_INDEX_READER_PRIVATE_KEY "
        f"--repo {shlex.quote(full_repository)}"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        owner = validate_name(args.owner, "owner")
        repository = validate_name(args.repository, "repository")
        if args.port < 0 or args.port > 65535:
            raise RegistrationError("port must be between 0 and 65535")
        if args.timeout <= 0:
            raise RegistrationError("timeout must be greater than zero")

        manifest = load_reader_manifest(args.manifest.expanduser().resolve())
        output = validate_output_path(args.output or default_output_path(owner))
        descriptor = reserve_credentials_file(output)
        try:
            code = wait_for_registration(
                manifest,
                owner=owner,
                repository=repository,
                port=args.port,
                timeout=args.timeout,
                open_browser=not args.no_browser,
            )
            credentials = exchange_code(code)
            write_credentials(descriptor, credentials)
        except BaseException:
            discard_credentials_file(output, descriptor)
            raise
        else:
            os.close(descriptor)
        print_next_steps(
            owner=owner,
            repository=repository,
            output=output,
            credentials=credentials,
        )
    except RegistrationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
