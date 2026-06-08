import json
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from build_index import register_reader_app
from build_index.register_reader_app import (
    RegistrationError,
    load_reader_manifest,
    prepare_manifest,
    reserve_credentials_file,
    validate_output_path,
    write_credentials,
)


ROOT = Path(__file__).parents[1]
APP_MANIFEST = ROOT / "github-apps" / "reader.manifest.json"
TOKEN_ACTION = ROOT / "actions" / "create-reader-token" / "action.yml"
PAGES_WORKFLOW = ROOT / ".github" / "workflows" / "pages.yml"
CREATE_TOKEN_SHA = "bcd2ba49218906704ab6c1aa796996da409d3eb1"


def test_reader_app_is_private_read_only_and_eventless() -> None:
    manifest = json.loads(APP_MANIFEST.read_text(encoding="utf-8"))

    assert set(manifest) == {
        "name",
        "url",
        "description",
        "public",
        "request_oauth_on_install",
        "setup_on_update",
        "default_permissions",
        "default_events",
    }
    assert manifest["public"] is False
    assert manifest["request_oauth_on_install"] is False
    assert manifest["setup_on_update"] is False
    assert manifest["default_permissions"] == {"contents": "read"}
    assert manifest["default_events"] == []


def test_reader_token_action_requires_explicit_read_scope() -> None:
    action = TOKEN_ACTION.read_text(encoding="utf-8")

    for input_name in ("client-id", "private-key", "owner", "repositories"):
        assert f"  {input_name}:" in action

    assert (
        f"uses: actions/create-github-app-token@{CREATE_TOKEN_SHA} # v3.2.0" in action
    )
    assert "owner: ${{ inputs.owner }}" in action
    assert "repositories: ${{ inputs.repositories }}" in action
    assert "permission-contents: read" in action
    assert action.count("permission-") == 1


def test_pages_workflow_uses_reader_token_for_admitted_repositories() -> None:
    workflow = PAGES_WORKFLOW.read_text(encoding="utf-8")

    assert "Resolve private producer scope" in workflow
    assert "uses: ./actions/create-reader-token" in workflow
    assert "BUILD_INDEX_READER_CLIENT_ID" in workflow
    assert "BUILD_INDEX_READER_PRIVATE_KEY" in workflow
    assert "build-index reader-token-scope" in workflow
    assert "steps.producer-scope.outputs.owner" in workflow
    assert "steps.producer-scope.outputs.repositories" in workflow
    assert "vllm-project/vllm" not in workflow
    assert "steps.producer-token.outputs.token || github.token" in workflow
    assert "build-index collect" in workflow
    assert "build-index build" in workflow
    assert "Prepare pages branch worktree" in workflow
    assert "previous-collection" not in workflow
    assert "report-new-files" not in workflow


def test_registration_helper_adds_only_runtime_urls() -> None:
    manifest = load_reader_manifest(APP_MANIFEST)

    prepared = prepare_manifest(
        manifest,
        owner="astral-sh-build",
        repository="_build-index",
        redirect_url="http://127.0.0.1:1234/callback",
    )

    assert "redirect_url" not in manifest
    assert prepared["url"] == "https://github.com/astral-sh-build/_build-index"
    assert prepared["redirect_url"] == "http://127.0.0.1:1234/callback"
    assert prepared["default_permissions"] == {"contents": "read"}


def test_registration_helper_rejects_broadened_manifest(tmp_path: Path) -> None:
    manifest = json.loads(APP_MANIFEST.read_text(encoding="utf-8"))
    manifest["default_permissions"]["issues"] = "read"
    path = tmp_path / "reader.manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RegistrationError, match="default_permissions"):
        load_reader_manifest(path)


def test_registration_helper_writes_private_credentials(tmp_path: Path) -> None:
    output = tmp_path / "credentials" / "reader.json"
    credentials = {"client_id": "Iv1.example", "pem": "private"}

    descriptor = reserve_credentials_file(output)
    try:
        write_credentials(descriptor, credentials)
    finally:
        os.close(descriptor)

    assert json.loads(output.read_text(encoding="utf-8")) == credentials
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pytest.raises(RegistrationError, match="reserve credential output"):
        reserve_credentials_file(output)


def test_registration_helper_refuses_repository_output() -> None:
    with pytest.raises(RegistrationError, match="inside the repository"):
        validate_output_path(ROOT / "reader-app-credentials.json")


def test_registration_helper_waits_for_matching_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = load_reader_manifest(APP_MANIFEST)
    monkeypatch.setattr(register_reader_app.secrets, "token_urlsafe", lambda _: "state")

    def open_browser(local_url: str) -> bool:
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(
                f"{local_url}callback?state=wrong&code=ignored", timeout=1
            )
        with urllib.request.urlopen(
            f"{local_url}callback?state=state&code=registration-code", timeout=1
        ) as response:
            assert b"Registration complete" in response.read()
        return True

    monkeypatch.setattr(register_reader_app.webbrowser, "open", open_browser)

    code = register_reader_app.wait_for_registration(
        manifest,
        owner="astral-sh-build",
        repository="_build-index",
        port=0,
        timeout=1,
        open_browser=True,
    )

    assert code == "registration-code"
