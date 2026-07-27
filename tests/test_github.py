import hashlib
import io
import json
import threading
import urllib.error
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from packaging.version import Version

from build_index import github as github_module
from build_index.collection import CollectionError
from build_index.config import RepositoryConfig, UnlabeledChannelRule, load_config
from build_index.github import (
    GitHubClient,
    GitHubError,
    WheelCompatibilityError,
    collect_release_assets,
    infer_channel,
)

ROOT = Path(__file__).parents[1]
CONFIG = load_config(ROOT / "tests" / "fixtures" / "index.toml")


class FakeGitHubClient:
    def __init__(
        self,
        releases: dict[str, list[dict[str, Any]]],
        hashes: dict[str, str] | None = None,
    ) -> None:
        self.token = "test-token"
        self.releases = releases
        self.hashes = hashes or {}
        self.hash_requests: list[str] = []

    def list_releases(
        self,
        repository: str,
        *,
        access: str = "private",
        log: Any = None,
    ) -> list[dict[str, Any]]:
        return self.releases.get(repository, [])

    def asset_sha256(
        self,
        asset_api_url: str,
        *,
        repository: str | None = None,
        access: str = "private",
        log: Any = None,
    ) -> str:
        self.hash_requests.append(asset_api_url)
        return self.hashes[asset_api_url]


def asset(
    filename: str,
    *,
    asset_id: int = 1,
    digest: str | None = "sha256:" + "a" * 64,
) -> dict[str, Any]:
    return {
        "browser_download_url": f"https://github.com/example/releases/{filename}",
        "created_at": "2026-06-02T17:13:12Z",
        "digest": digest,
        "id": asset_id,
        "name": filename,
        "size": 123,
        "url": f"https://api.github.com/releases/assets/{asset_id}",
    }


def release(
    assets: list[dict[str, Any]],
    *,
    tag: str = "0.1.0",
    draft: bool = False,
    prerelease: bool = False,
) -> dict[str, Any]:
    return {
        "assets": assets,
        "draft": draft,
        "prerelease": prerelease,
        "tag_name": tag,
    }


def upstream_vllm_config():
    repository = RepositoryConfig(
        repository="vllm-project/vllm",
        projects=("vllm",),
        access="public",
        tag_regex=r"^v(?P<version>.+)$",
        minimum_release_version=Version("0.9.1"),
        maximum_release_version=Version("0.22.0"),
        ignored_channels=("cpu",),
        unlabeled_channel_rules=(
            UnlabeledChannelRule(Version("0.9.1"), Version("0.12.0"), "cu128"),
            UnlabeledChannelRule(Version("0.12.0"), Version("0.20.0"), "cu129"),
            UnlabeledChannelRule(Version("0.20.0"), Version("0.23.0"), "cu130"),
        ),
        has_version_policy=True,
    )
    return replace(CONFIG, repositories=(repository,))


def test_collect_release_assets_assigns_channels_and_ignores_non_wheels() -> None:
    client = FakeGitHubClient(
        {
            "example/build-index-test-mixed": [
                release(
                    [
                        asset(
                            "index_test_mixed-0.1.0+cpu-py3-none-any.whl",
                            asset_id=1,
                        ),
                        asset(
                            "index_test_mixed-0.1.0+cu128-py3-none-any.whl",
                            asset_id=2,
                        ),
                        asset("checksums.txt", asset_id=3),
                    ]
                )
            ]
        }
    )

    collection = collect_release_assets(CONFIG, client)

    assert [
        (artifact.project, artifact.channel, artifact.filename)
        for artifact in collection.artifacts
    ] == [
        (
            "index-test-mixed",
            "cpu",
            "index_test_mixed-0.1.0+cpu-py3-none-any.whl",
        ),
        (
            "index-test-mixed",
            "cu128",
            "index_test_mixed-0.1.0+cu128-py3-none-any.whl",
        ),
    ]


def test_collect_release_assets_preserves_historical_cuda_names() -> None:
    filenames = [
        "index_test_gpu-0.1.0+cu.12.8.torch.2.8-py3-none-any.whl",
        "index_test_gpu-0.2.0+cu12.8.0torch2.8.0cxx11abiTRUE-py3-none-any.whl",
    ]
    client = FakeGitHubClient(
        {
            "example/build-index-test-gpu": [
                release(
                    [
                        asset(filenames[0], asset_id=10),
                        asset(filenames[1], asset_id=11),
                    ]
                )
            ]
        }
    )

    collection = collect_release_assets(CONFIG, client)

    assert [
        (artifact.filename, artifact.channel) for artifact in collection.artifacts
    ] == [(filename, "cu128") for filename in filenames]


def test_collect_release_assets_infers_globally_configured_channel() -> None:
    filename = "index_test_gpu-0.1.0+cu129-py3-none-any.whl"
    client = FakeGitHubClient(
        {"example/build-index-test-gpu": [release([asset(filename)])]}
    )

    collection = collect_release_assets(CONFIG, client)

    assert collection.artifacts[0].channel == "cu129"


def test_collect_release_assets_enforces_channel_restriction() -> None:
    repositories = tuple(
        replace(repository, channels=("cpu",))
        if repository.repository == "example/build-index-test-gpu"
        else repository
        for repository in CONFIG.repositories
    )
    config = replace(CONFIG, repositories=repositories)
    client = FakeGitHubClient(
        {
            "example/build-index-test-gpu": [
                release([asset("index_test_gpu-0.1.0+cu128-py3-none-any.whl")])
            ]
        }
    )

    with pytest.raises(CollectionError, match="wheel channel is restricted"):
        collect_release_assets(config, client)


def test_collect_release_assets_rejects_incompatible_wheel() -> None:
    invalid = "index_test_gpu-1..0+hash+cu12.8torch2.8.0-py3-none-any.whl"
    client = FakeGitHubClient(
        {"example/build-index-test-gpu": [release([asset(invalid)])]}
    )

    with pytest.raises(WheelCompatibilityError, match="invalid wheel filename"):
        collect_release_assets(CONFIG, client)


def test_collect_release_assets_normalizes_legacy_wheel_filename() -> None:
    source = (
        "index_test_gpu-1.2.1+1300811+cu12.8torch2.10.0cxx11abiTRUE-py3-none-any.whl"
    )
    client = FakeGitHubClient(
        {"example/build-index-test-gpu": [release([asset(source)])]}
    )
    messages: list[str] = []

    collection = collect_release_assets(CONFIG, client, log=messages.append)

    assert collection.artifacts[0].filename == (
        "index_test_gpu-1.2.1+1300811.cu12.8torch2.10.0cxx11abiTRUE-py3-none-any.whl"
    )
    assert collection.artifacts[0].source_url.endswith(source)
    assert any("normalized wheel filename" in message for message in messages)


def test_collect_release_assets_hashes_asset_when_digest_is_absent() -> None:
    api_url = "https://api.github.com/releases/assets/1"
    client = FakeGitHubClient(
        {
            "example/build-index-test-gpu": [
                release(
                    [
                        asset(
                            "index_test_gpu-0.1.0+cu128-py3-none-any.whl",
                            digest=None,
                        )
                    ]
                )
            ]
        },
        hashes={api_url: "b" * 64},
    )

    collection = collect_release_assets(CONFIG, client)

    assert collection.artifacts[0].sha256 == "b" * 64
    assert client.hash_requests == [api_url]


def test_github_client_asset_download_strips_auth_on_redirect(
    tmp_path: Path,
) -> None:
    content = b"release asset bytes"

    class FileHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            assert self.headers.get("Authorization") is None
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    files = ThreadingHTTPServer(("127.0.0.1", 0), FileHandler)
    files_thread = threading.Thread(target=files.serve_forever, daemon=True)
    files_thread.start()
    files_url = f"http://127.0.0.1:{files.server_address[1]}/wheel.whl"

    class ApiHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            assert self.headers["Authorization"] == "Bearer test-token"
            self.send_response(307)
            self.send_header("Location", files_url)
            self.end_headers()

    api = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
    api_thread = threading.Thread(target=api.serve_forever, daemon=True)
    api_thread.start()
    api_url = f"http://127.0.0.1:{api.server_address[1]}/asset"
    try:
        client = GitHubClient("test-token")
        assert client.asset_sha256(api_url) == hashlib.sha256(content).hexdigest()
        destination = tmp_path / "wheel.whl"
        assert client.download_asset(api_url, destination) == (
            hashlib.sha256(content).hexdigest(),
            len(content),
        )
        assert destination.read_bytes() == content
    finally:
        api.shutdown()
        api.server_close()
        api_thread.join()
        files.shutdown()
        files.server_close()
        files_thread.join()


def test_collect_release_assets_selects_highest_release_revision() -> None:
    filename = "index_test_gpu-0.1.0+cu128-py3-none-any.whl"
    client = FakeGitHubClient(
        {
            "example/build-index-test-gpu": [
                release(
                    [asset(filename, asset_id=3, digest="sha256:" + "c" * 64)],
                    tag="0.1.0-r2",
                ),
                release(
                    [asset(filename, asset_id=1, digest="sha256:" + "a" * 64)],
                    tag="0.1.0",
                ),
                release(
                    [asset(filename, asset_id=2, digest="sha256:" + "b" * 64)],
                    tag="0.1.0-r1",
                ),
            ]
        }
    )

    collection = collect_release_assets(CONFIG, client)

    assert len(collection.artifacts) == 1
    assert collection.artifacts[0].release == "0.1.0-r2"
    assert collection.artifacts[0].sha256 == "c" * 64


def test_empty_higher_revision_removes_release_family_files() -> None:
    filename = "index_test_gpu-0.1.0+cu128-py3-none-any.whl"
    client = FakeGitHubClient(
        {
            "example/build-index-test-gpu": [
                release([], tag="0.1.0-r1"),
                release([asset(filename)], tag="0.1.0"),
            ]
        }
    )

    assert collect_release_assets(CONFIG, client).artifacts == ()


def test_collect_release_assets_ignores_drafts_and_prereleases() -> None:
    client = FakeGitHubClient(
        {
            "example/build-index-test-gpu": [
                release(
                    [asset("index_test_gpu-0.1.0+cu128-py3-none-any.whl")],
                    draft=True,
                ),
                release(
                    [asset("index_test_gpu-0.2.0+cu128-py3-none-any.whl")],
                    prerelease=True,
                ),
            ]
        }
    )

    assert collect_release_assets(CONFIG, client).artifacts == ()


def test_collect_release_assets_collapses_republished_filename() -> None:
    filename = "index_test_gpu-0.1.0+cu128-py3-none-any.whl"
    client = FakeGitHubClient(
        {
            "example/build-index-test-gpu": [
                release([asset(filename, asset_id=1)], tag="0.1.0"),
                release([asset(filename, asset_id=2)], tag="replacement"),
            ]
        }
    )
    messages: list[str] = []

    collection = collect_release_assets(CONFIG, client, log=messages.append)

    assert [artifact.filename for artifact in collection.artifacts] == [filename]
    assert "collapsed 1 republished wheel filenames" in messages


def test_collect_release_assets_rejects_unconfigured_project() -> None:
    client = FakeGitHubClient(
        {
            "example/build-index-test-gpu": [
                release([asset("other-0.1.0+cu128-py3-none-any.whl")])
            ]
        }
    )

    with pytest.raises(CollectionError, match="wheel project is not admitted"):
        collect_release_assets(CONFIG, client)


def test_collect_release_assets_rejects_malformed_digest() -> None:
    client = FakeGitHubClient(
        {
            "example/build-index-test-gpu": [
                release(
                    [
                        asset(
                            "index_test_gpu-0.1.0+cu128-py3-none-any.whl",
                            digest="sha256:not-a-digest",
                        )
                    ]
                )
            ]
        }
    )

    with pytest.raises(GitHubError, match="invalid 'digest'"):
        collect_release_assets(CONFIG, client)


@pytest.mark.parametrize(
    ("tag", "expected_channel"),
    [
        ("v0.9.1", "cu128"),
        ("v0.11.2", "cu128"),
        ("v0.12.0", "cu129"),
        ("v0.19.1", "cu129"),
        ("v0.20.0", "cu130"),
        ("v0.22.0", "cu130"),
    ],
)
def test_upstream_vllm_unlabeled_wheels_use_bounded_release_mapping(
    tag: str,
    expected_channel: str,
) -> None:
    version = tag.removeprefix("v")
    filename = f"vllm-{version}-cp312-cp312-manylinux_2_28_x86_64.whl"
    client = FakeGitHubClient(
        {"vllm-project/vllm": [release([asset(filename)], tag=tag)]}
    )

    collection = collect_release_assets(upstream_vllm_config(), client)

    assert len(collection.artifacts) == 1
    artifact = collection.artifacts[0]
    assert artifact.channel == expected_channel
    assert artifact.filename == filename
    assert artifact.release == tag


@pytest.mark.parametrize("version", ["0.22.1", "0.23.0"])
def test_upstream_vllm_skips_versions_above_owned_release_ceiling(
    version: str,
) -> None:
    filename = f"vllm-{version}-cp312-cp312-manylinux_2_28_x86_64.whl"
    client = FakeGitHubClient(
        {"vllm-project/vllm": [release([asset(filename)], tag=f"v{version}")]}
    )
    messages: list[str] = []

    collection = collect_release_assets(
        upstream_vllm_config(), client, log=messages.append
    )

    assert collection.artifacts == ()
    assert any(
        f"excluded release above maximum version: v{version} -> {version}" in message
        for message in messages
    )


def test_upstream_vllm_maximum_applies_to_explicit_channels() -> None:
    filename = "vllm-0.23.0+cu.13.0.torch.2.11-cp312-cp312-manylinux_2_28_x86_64.whl"
    client = FakeGitHubClient(
        {"vllm-project/vllm": [release([asset(filename)], tag="v0.23.0")]}
    )

    collection = collect_release_assets(upstream_vllm_config(), client)

    assert collection.artifacts == ()


def test_upstream_vllm_explicit_cu118_is_authoritative() -> None:
    filename = "vllm-0.20.0+cu.11.8.torch.2.7-cp312-cp312-manylinux_2_28_x86_64.whl"
    client = FakeGitHubClient(
        {"vllm-project/vllm": [release([asset(filename)], tag="v0.20.0")]}
    )

    collection = collect_release_assets(upstream_vllm_config(), client)

    assert collection.artifacts[0].channel == "cu118"


def test_upstream_vllm_ignores_cpu_wheels_without_affecting_astral_cpu() -> None:
    upstream_cpu = "vllm-0.20.0+cpu-cp312-cp312-manylinux_2_28_x86_64.whl"
    upstream_gpu = "vllm-0.20.0-cp312-cp312-manylinux_2_28_x86_64.whl"
    config = upstream_vllm_config()
    upstream = config.repositories[0]
    astral = replace(
        upstream,
        repository="astral-sh-build/build-vllm",
        access="private",
        ignored_channels=(),
        unlabeled_channel_rules=(),
        has_version_policy=False,
    )
    combined = replace(config, repositories=(astral, upstream))
    client = FakeGitHubClient(
        {
            "vllm-project/vllm": [
                release(
                    [asset(upstream_cpu, asset_id=1), asset(upstream_gpu, asset_id=2)],
                    tag="v0.20.0",
                )
            ],
            "astral-sh-build/build-vllm": [
                release([asset(upstream_cpu, asset_id=3)], tag="v0.20.0")
            ],
        }
    )

    collection = collect_release_assets(combined, client)

    assert [(item.repository, item.channel) for item in collection.artifacts] == [
        ("astral-sh-build/build-vllm", "cpu"),
        ("vllm-project/vllm", "cu130"),
    ]


def test_ignored_channel_does_not_need_to_be_publicly_configured() -> None:
    rocm = "vllm-0.20.0+rocm6.3-cp312-cp312-manylinux_2_28_x86_64.whl"
    cuda = "vllm-0.20.0-cp312-cp312-manylinux_2_28_x86_64.whl"
    config = upstream_vllm_config()
    repository = replace(config.repositories[0], ignored_channels=("cpu", "rocm6.3"))
    config = replace(config, repositories=(repository,))
    client = FakeGitHubClient(
        {
            "vllm-project/vllm": [
                release(
                    [asset(rocm, asset_id=1), asset(cuda, asset_id=2)],
                    tag="v0.20.0",
                )
            ]
        }
    )

    collection = collect_release_assets(config, client)

    assert [(item.filename, item.channel) for item in collection.artifacts] == [
        (cuda, "cu130")
    ]


def test_version_policy_excludes_external_tags_and_prereleases() -> None:
    stable = "vllm-0.20.0-cp312-cp312-manylinux_2_28_x86_64.whl"
    post = "vllm-0.20.0.post1-cp312-cp312-manylinux_2_28_x86_64.whl"
    client = FakeGitHubClient(
        {
            "vllm-project/vllm": [
                release([], tag="external-release"),
                release([], tag="vnot-a-version"),
                release([], tag="v0.9.0"),
                release([], tag="v0.20.0a1"),
                release([], tag="v0.20.0b1"),
                release([], tag="v0.20.0rc1"),
                release([], tag="v0.20.0.dev1"),
                release([asset(stable, asset_id=1)], tag="v0.20.0"),
                release([asset(post, asset_id=2)], tag="v0.20.0.post1"),
            ]
        }
    )
    messages: list[str] = []

    collection = collect_release_assets(
        upstream_vllm_config(),
        client,
        log=messages.append,
    )

    assert [item.version for item in collection.artifacts] == [
        "0.20.0",
        "0.20.0.post1",
    ]
    assert any("nonmatching tag" in message for message in messages)
    assert any("invalid captured version" in message for message in messages)
    assert any("below minimum version" in message for message in messages)
    assert sum("parsed prerelease" in message for message in messages) == 4


def test_default_tag_regex_extracts_the_complete_tag() -> None:
    config = upstream_vllm_config()
    repository = replace(
        config.repositories[0],
        tag_regex="^(?P<version>.+)$",
    )
    config = replace(config, repositories=(repository,))
    filename = "vllm-0.20.0-cp312-cp312-manylinux_2_28_x86_64.whl"
    client = FakeGitHubClient(
        {"vllm-project/vllm": [release([asset(filename)], tag="0.20.0")]}
    )

    collection = collect_release_assets(config, client)

    assert collection.artifacts[0].channel == "cu130"
    assert collection.artifacts[0].release == "0.20.0"


def _http_error(
    code: int,
    *,
    message: str = "Not Found",
    headers: dict[str, str] | None = None,
) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.github.com/repos/example/project/releases",
        code,
        message,
        headers or {},
        io.BytesIO(json.dumps({"message": message}).encode()),
    )


def test_public_github_read_prefers_token_then_falls_back_anonymously(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GitHubClient("app-token")
    tokens: list[str | None] = []

    def get_json(path: str, *, token: str | None) -> Any:
        tokens.append(token)
        if token is not None:
            raise _http_error(404)
        return []

    monkeypatch.setattr(client, "_get_json", get_json)
    messages: list[str] = []

    assert (
        list(
            client.list_releases(
                "example/project",
                access="public",
                log=messages.append,
            )
        )
        == []
    )
    assert tokens == ["app-token", None]
    assert any("anonymous fallback" in message for message in messages)


def test_public_github_read_falls_back_for_integration_access_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GitHubClient("app-token")
    tokens: list[str | None] = []

    def get_json(path: str, *, token: str | None) -> Any:
        tokens.append(token)
        if token is not None:
            raise _http_error(
                403,
                message="Resource not accessible by integration",
            )
        return []

    monkeypatch.setattr(client, "_get_json", get_json)

    assert list(client.list_releases("example/project", access="public")) == []
    assert tokens == ["app-token", None]


@pytest.mark.parametrize(
    "error",
    [
        _http_error(404),
        _http_error(
            403,
            message="API rate limit exceeded",
            headers={"X-RateLimit-Remaining": "0"},
        ),
        _http_error(500, message="Internal Server Error"),
    ],
)
def test_github_read_does_not_fall_back_for_private_or_unrelated_failures(
    monkeypatch: pytest.MonkeyPatch,
    error: urllib.error.HTTPError,
) -> None:
    client = GitHubClient("app-token")
    tokens: list[str | None] = []

    def get_json(path: str, *, token: str | None) -> Any:
        tokens.append(token)
        raise error

    monkeypatch.setattr(client, "_get_json", get_json)
    access = "private" if error.code == 404 else "public"

    with pytest.raises(GitHubError):
        list(client.list_releases("example/project", access=access))
    assert tokens == ["app-token"]


def test_public_github_read_does_not_fall_back_for_malformed_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GitHubClient("app-token")
    tokens: list[str | None] = []

    def get_json(path: str, *, token: str | None) -> Any:
        tokens.append(token)
        raise json.JSONDecodeError("invalid", "not-json", 0)

    monkeypatch.setattr(client, "_get_json", get_json)

    with pytest.raises(GitHubError):
        list(client.list_releases("example/project", access="public"))
    assert tokens == ["app-token"]


def test_public_asset_hash_prefers_token_then_falls_back_anonymously(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = []

    def asset_hash(request: Any) -> str:
        requests.append(request)
        if request.headers.get("Authorization") is not None:
            raise _http_error(404)
        return "a" * 64

    monkeypatch.setattr(github_module, "_sha256_github_asset", asset_hash)
    client = GitHubClient("app-token")

    digest = client.asset_sha256(
        "https://api.github.com/releases/assets/1",
        repository="example/project",
        access="public",
    )

    assert digest == "a" * 64
    assert [request.headers.get("Authorization") for request in requests] == [
        "Bearer app-token",
        None,
    ]


@pytest.mark.parametrize(
    ("version", "channels", "expected"),
    [
        ("1.0+cpu", ("cpu",), "cpu"),
        ("1.0+cpu.torch.2.8.0", ("cpu",), "cpu"),
        ("1.0+cu128torch2.8.0", ("cu128",), "cu128"),
        ("1.0+cu128.torch.2.8.0", ("cu12", "cu128"), "cu128"),
        ("1.0+cu.12.8.torch.2.8", ("cu128",), "cu128"),
        ("1.0+cu12.8torch2.8.0cxx11abiTRUE", ("cu128",), "cu128"),
        ("1.0+g2d5be1dd5.d20260219.cpu", ("cpu", "cu128"), "cpu"),
        ("1.0+rocm6.3", ("rocm6.3",), "rocm6.3"),
        ("1.0+xpu", ("xpu",), "xpu"),
    ],
)
def test_infer_channel(version: str, channels: tuple[str, ...], expected: str) -> None:
    assert infer_channel(Version(version), channels) == expected


def test_infer_channel_rejects_unlabeled_mixed_channel_wheel() -> None:
    with pytest.raises(WheelCompatibilityError, match="no local channel label"):
        infer_channel(Version("1.0"), ("cpu", "cu128"))


def test_infer_channel_rejects_ambiguous_numeric_prefix() -> None:
    with pytest.raises(CollectionError, match="unconfigured channel 'cu1281'"):
        infer_channel(Version("1.0+cu1281"), ("cu128",))


def test_infer_channel_rejects_unconfigured_canonical_cuda_channel() -> None:
    with pytest.raises(CollectionError, match="unconfigured channel 'cu129'"):
        infer_channel(Version("1.0+cu.12.9.torch.2.8"), ("cu128",))
