import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_sync_r2_uses_canonical_keys_and_deletes_stale_objects(
    tmp_path: Path,
) -> None:
    output = tmp_path / "dist"
    documents = {
        "simple/index.html": "<html></html>",
        "simple/cu128/index.json": "{}",
        "simple/cu128/vllm/index.json": "{}",
        "simple/v1+html/cu128/vllm/index.html": "<html></html>",
        "simple/v1+json/cu128/vllm/index.json": "{}",
    }
    for relative, body in documents.items():
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    log = tmp_path / "aws.jsonl"
    fake_aws = tmp_path / "aws"
    fake_aws.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
import time

arguments = sys.argv[1:]
if arguments[:2] == ["s3api", "put-object"]:
    time.sleep(0.02)
with open(os.environ["AWS_LOG"], "a", encoding="utf-8") as output:
    output.write(json.dumps(arguments) + "\\n")
if arguments[:2] == ["s3api", "list-objects-v2"]:
    print(json.dumps(["simple/cu128/", "simple/stale/"]))
else:
    print("{}")
"""
    )
    fake_aws.chmod(0o755)

    environment = {
        **os.environ,
        "AWS_CLI": str(fake_aws),
        "AWS_LOG": str(log),
        "R2_BUCKET": "index",
        "R2_ENDPOINT": "https://example.r2.cloudflarestorage.com",
        "R2_UPLOAD_CONCURRENCY": "2",
    }
    subprocess.run(
        [ROOT / "scripts" / "sync_r2.sh", output],
        check=True,
        env=environment,
        text=True,
        capture_output=True,
    )

    calls = [json.loads(line) for line in log.read_text().splitlines()]
    puts = [call for call in calls if call[:2] == ["s3api", "put-object"]]
    deletes = [call for call in calls if call[:2] == ["s3api", "delete-object"]]
    listing = next(call for call in calls if call[:2] == ["s3api", "list-objects-v2"])

    assert {_argument(call, "--key") for call in puts} == {
        "simple/v1+html/cu128/vllm/",
        "simple/v1+json/cu128/vllm/",
        "simple/cu128/vllm/",
        "simple/cu128/",
        "simple/",
    }
    call_indexes = {id(call): index for index, call in enumerate(calls)}
    project_indexes = [
        call_indexes[id(call)]
        for call in puts
        if _argument(call, "--key")
        in {
            "simple/v1+html/cu128/vllm/",
            "simple/v1+json/cu128/vllm/",
            "simple/cu128/vllm/",
        }
    ]
    root_indexes = [
        call_indexes[id(call)]
        for call in puts
        if _argument(call, "--key") in {"simple/cu128/", "simple/"}
    ]
    assert max(project_indexes) < min(root_indexes)
    assert {_argument(call, "--content-type") for call in puts} == {
        "application/vnd.pypi.simple.v1+html",
        "application/vnd.pypi.simple.v1+json",
    }
    assert {_argument(call, "--cache-control") for call in puts} == {
        "public, max-age=60, stale-while-revalidate=300"
    }
    assert calls[: len(puts)] == puts
    assert _argument(listing, "--prefix") == "simple/"
    assert [_argument(call, "--key") for call in deletes] == ["simple/stale/"]


def _argument(arguments: list[str], name: str) -> str:
    return arguments[arguments.index(name) + 1]
