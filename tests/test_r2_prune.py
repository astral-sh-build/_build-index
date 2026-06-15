from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from build_index.collection import CollectedArtifact, collection_from_artifacts
from build_index.mirror import artifact_key
from build_index.r2_prune import (
    ArtifactPruneError,
    RetainedArtifact,
    load_retained_artifacts,
    prune_stale_artifacts,
)


class FakeS3Client:
    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing = existing or set()
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.pages: list[dict[str, object]] | None = None
        self.delete_response: dict[str, object] = {}
        self.fail_delete = False

    def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("list", kwargs))
        if self.pages is not None:
            return self.pages.pop(0)
        prefix = kwargs["Prefix"]
        assert isinstance(prefix, str)
        return {
            "Contents": [
                {"Key": key} for key in sorted(self.existing) if key.startswith(prefix)
            ],
            "IsTruncated": False,
        }

    def delete_objects(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("delete", kwargs))
        if self.fail_delete:
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}},
                "DeleteObjects",
            )
        return self.delete_response


def test_prune_r2_deletes_stale_artifacts_and_metadata() -> None:
    artifact = make_artifact()
    desired_key = artifact_key(artifact)
    client = FakeS3Client(
        {
            desired_key,
            f"{desired_key}.metadata",
            "artifacts/ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff/stale.whl",
            "artifacts/ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff/stale.whl.metadata",
            "simple/cu128/sample-project/",
        }
    )
    messages: list[str] = []

    result = prune_stale_artifacts(
        collection_from_artifacts([artifact]),
        "index",
        "https://example.r2.cloudflarestorage.com",
        delete=True,
        client=client,
        log=messages.append,
    )

    assert result.desired == 2
    assert result.retained == 0
    assert result.existing == 4
    assert result.stale == 2
    assert result.deleted == 2
    assert result.dry_run is False
    assert client.calls[0] == ("list", {"Bucket": "index", "Prefix": "artifacts/"})
    assert client.calls[1] == (
        "delete",
        {
            "Bucket": "index",
            "Delete": {
                "Objects": [
                    {
                        "Key": (
                            "artifacts/ffffffffffffffffffffffffffffffffffffffff"
                            "ffffffffffffffffffffffff/stale.whl"
                        )
                    },
                    {
                        "Key": (
                            "artifacts/ffffffffffffffffffffffffffffffffffffffff"
                            "ffffffffffffffffffffffff/stale.whl.metadata"
                        )
                    },
                ],
                "Quiet": True,
            },
        },
    )
    assert messages == [
        (
            "deleted stale s3://index/artifacts/ffffffffffffffffffffffffffffffffffffffff"
            "ffffffffffffffffffffffff/stale.whl"
        ),
        (
            "deleted stale s3://index/artifacts/ffffffffffffffffffffffffffffffffffffffff"
            "ffffffffffffffffffffffff/stale.whl.metadata"
        ),
    ]


def test_prune_r2_dry_run_reports_stale_objects_without_deleting() -> None:
    artifact = make_artifact()
    client = FakeS3Client(
        {
            artifact_key(artifact),
            "artifacts/ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff/stale.whl",
        }
    )
    messages: list[str] = []

    result = prune_stale_artifacts(
        collection_from_artifacts([artifact]),
        "index",
        "https://example.r2.cloudflarestorage.com",
        client=client,
        log=messages.append,
    )

    assert result.stale == 1
    assert result.retained == 0
    assert result.deleted == 0
    assert result.dry_run is True
    assert [operation for operation, _call in client.calls] == ["list"]
    assert messages == [
        (
            "would delete stale s3://index/artifacts/ffffffffffffffffffffffffffffffff"
            "ffffffffffffffffffffffffffffffff/stale.whl"
        )
    ]


def test_prune_r2_keeps_retained_artifacts_and_metadata() -> None:
    artifact = make_artifact()
    retained = RetainedArtifact(
        sha256="1" * 64,
        filename="retained-1.0.0-py3-none-any.whl",
        reason="keep for migration",
    )
    retained_key = f"artifacts/{retained.sha256}/{retained.filename}"
    stale_key = (
        "artifacts/ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff/"
        "stale.whl"
    )
    client = FakeS3Client(
        {
            artifact_key(artifact),
            f"{artifact_key(artifact)}.metadata",
            retained_key,
            f"{retained_key}.metadata",
            stale_key,
        }
    )

    result = prune_stale_artifacts(
        collection_from_artifacts([artifact]),
        "index",
        "https://example.r2.cloudflarestorage.com",
        delete=True,
        retained_artifacts=[retained],
        client=client,
    )

    assert result.desired == 4
    assert result.retained == 2
    assert result.stale == 1
    deletion = next(call for operation, call in client.calls if operation == "delete")
    assert deletion["Delete"] == {
        "Objects": [{"Key": stale_key}],
        "Quiet": True,
    }


def test_prune_r2_paginates_before_deleting_stale_objects() -> None:
    artifact = make_artifact()
    client = FakeS3Client()
    client.pages = [
        {
            "Contents": [{"Key": artifact_key(artifact)}],
            "IsTruncated": True,
            "NextContinuationToken": "next",
        },
        {
            "Contents": [
                {
                    "Key": (
                        "artifacts/ffffffffffffffffffffffffffffffffffffffff"
                        "ffffffffffffffffffffffff/stale.whl"
                    )
                }
            ],
            "IsTruncated": False,
        },
    ]

    prune_stale_artifacts(
        collection_from_artifacts([artifact]),
        "index",
        "https://example.r2.cloudflarestorage.com",
        delete=True,
        client=client,
    )

    assert client.calls[:2] == [
        ("list", {"Bucket": "index", "Prefix": "artifacts/"}),
        (
            "list",
            {
                "Bucket": "index",
                "Prefix": "artifacts/",
                "ContinuationToken": "next",
            },
        ),
    ]
    assert client.calls[2][0] == "delete"


def test_prune_r2_refuses_empty_collection() -> None:
    with pytest.raises(ArtifactPruneError, match="empty collection"):
        prune_stale_artifacts(
            collection_from_artifacts([]),
            "index",
            "https://example.r2.cloudflarestorage.com",
            client=FakeS3Client({"artifacts/stale.whl"}),
        )


def test_prune_r2_reports_delete_errors() -> None:
    artifact = make_artifact()
    client = FakeS3Client(
        {
            artifact_key(artifact),
            "artifacts/ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff/stale.whl",
        }
    )
    client.fail_delete = True

    with pytest.raises(ArtifactPruneError, match="AccessDenied: denied"):
        prune_stale_artifacts(
            collection_from_artifacts([artifact]),
            "index",
            "https://example.r2.cloudflarestorage.com",
            delete=True,
            client=client,
        )


def test_prune_r2_reports_delete_result_errors() -> None:
    artifact = make_artifact()
    client = FakeS3Client(
        {
            artifact_key(artifact),
            "artifacts/ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff/stale.whl",
        }
    )
    client.delete_response = {"Errors": [{"Key": "stale.whl", "Code": "denied"}]}

    with pytest.raises(ArtifactPruneError, match="delete-objects returned errors"):
        prune_stale_artifacts(
            collection_from_artifacts([artifact]),
            "index",
            "https://example.r2.cloudflarestorage.com",
            delete=True,
            client=client,
        )


def test_load_retained_artifacts(tmp_path: Path) -> None:
    path = tmp_path / "retain-artifacts.toml"
    path.write_text(
        """
schema_version = 1

[[artifact]]
sha256 = "1111111111111111111111111111111111111111111111111111111111111111"
filename = "retained-1.0.0-py3-none-any.whl"
reason = "manual hold"
""",
        encoding="utf-8",
    )

    assert load_retained_artifacts(path) == (
        RetainedArtifact(
            sha256="1" * 64,
            filename="retained-1.0.0-py3-none-any.whl",
            reason="manual hold",
        ),
    )


def test_load_retained_artifacts_rejects_invalid_keys(tmp_path: Path) -> None:
    path = tmp_path / "retain-artifacts.toml"
    path.write_text(
        """
schema_version = 1

[[artifact]]
sha256 = "not-a-sha"
filename = "retained-1.0.0-py3-none-any.whl"
""",
        encoding="utf-8",
    )

    with pytest.raises(ArtifactPruneError, match="lowercase SHA-256"):
        load_retained_artifacts(path)


def make_artifact() -> CollectedArtifact:
    return CollectedArtifact(
        repository="example/build-sample",
        release="v1.0.0",
        filename="sample_project-1.0.0-py3-none-any.whl",
        project="sample-project",
        version="1.0.0",
        channel="cu128",
        source_url="https://github.com/example/build-sample/releases/tag/v1.0.0",
        download_url="https://api.github.com/repos/example/build-sample/releases/assets/1",
        sha256="0" * 64,
        size=100,
        upload_time="2026-01-01T00:00:00Z",
    )
