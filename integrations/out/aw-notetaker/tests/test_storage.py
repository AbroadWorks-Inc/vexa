"""exporter.storage.Storage against moto's mocked S3 (spec §4.2)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from exporter.storage import Storage


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


def test_size_returns_content_length_or_none(storage: Storage) -> None:
    assert storage.size("src-bucket", "missing.bin") is None
    storage.put_bytes("src-bucket", "five.bin", b"12345", "application/octet-stream")
    assert storage.size("src-bucket", "five.bin") == 5


def test_size_reraises_non_404_errors(storage: Storage) -> None:
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError):
        storage.size("no-such-bucket-xyz", "k")


def test_download_file_writes_object_to_path(storage: Storage, tmp_path: Path) -> None:
    storage.put_bytes("src-bucket", "master.webm", b"webm-bytes", "video/webm")
    dst = tmp_path / "master.webm"
    storage.download_file("src-bucket", "master.webm", dst)
    assert dst.read_bytes() == b"webm-bytes"


def test_upload_file_puts_object_with_content_type(
    storage: Storage, tmp_path: Path
) -> None:
    src = tmp_path / "audio.wav"
    src.write_bytes(b"RIFF-wav")
    storage.upload_file(src, "dst-bucket", "recordings/x/audio.wav", "audio/wav")
    assert storage.get_bytes("dst-bucket", "recordings/x/audio.wav") == b"RIFF-wav"
    head = storage._client.head_object(
        Bucket="dst-bucket", Key="recordings/x/audio.wav"
    )
    assert head["ContentType"] == "audio/wav"
