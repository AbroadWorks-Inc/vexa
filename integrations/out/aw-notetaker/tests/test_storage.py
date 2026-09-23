"""aw_exporter.storage.Storage against moto's mocked S3 (spec §4.2)."""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

from aw_exporter.storage import Storage


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> Iterator[Storage]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="src-bucket")
        client.create_bucket(Bucket="dst-bucket")
        yield Storage(client)


def test_put_get_json_roundtrip(storage: Storage) -> None:
    storage.put_json("src-bucket", "meeting.json", {"meeting_id": "vexa-1", "n": 2})
    assert storage.get_json("src-bucket", "meeting.json") == {
        "meeting_id": "vexa-1",
        "n": 2,
    }


def test_get_json_returns_none_on_missing_key(storage: Storage) -> None:
    assert storage.get_json("src-bucket", "does/not/exist.json") is None


def test_exists(storage: Storage) -> None:
    assert storage.exists("src-bucket", "a.bin") is False
    storage.put_bytes("src-bucket", "a.bin", b"hi", "application/octet-stream")
    assert storage.exists("src-bucket", "a.bin") is True


def test_copy_across_buckets(storage: Storage) -> None:
    storage.put_bytes("src-bucket", "master.webm", b"webm-bytes", "video/webm")
    storage.copy("src-bucket", "master.webm", "dst-bucket", "recordings/x/master.webm")
    assert storage.get_bytes("dst-bucket", "recordings/x/master.webm") == b"webm-bytes"


def test_delete(storage: Storage) -> None:
    storage.put_bytes("src-bucket", "gone.bin", b"x", "application/octet-stream")
    storage.delete("src-bucket", "gone.bin")
    assert storage.exists("src-bucket", "gone.bin") is False


def test_list_keys_paginates_over_1005_keys(storage: Storage) -> None:
    for i in range(1005):
        storage.put_bytes("src-bucket", f"p/{i:04d}.txt", b"x", "text/plain")
    keys = storage.list_keys("src-bucket", "p/")
    assert len(keys) == 1005
    assert keys[0] == "p/0000.txt"
    assert keys[-1] == "p/1004.txt"


def test_iter_lines_yields_lines_of_a_three_line_object(storage: Storage) -> None:
    storage.put_bytes(
        "src-bucket", "tape.jsonl", b"one\ntwo\nthree\n", "application/x-ndjson"
    )
    assert list(storage.iter_lines("src-bucket", "tape.jsonl")) == [
        "one",
        "two",
        "three",
    ]
