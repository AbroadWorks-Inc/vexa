"""exporter.storage.Storage against moto's mocked S3 (spec §4.2)."""

from __future__ import annotations

import ast
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


# ---------------------------------------------------------------------------
# retention-class tagging (design doc §3/§7) — the export bucket expires
# objects by the `retention-class` tag; an object written with no `retention`
# argument stays untagged (the Vexa bucket's pending/failed queue objects).
# ---------------------------------------------------------------------------


def _tags(storage: Storage, bucket: str, key: str) -> dict[str, str]:
    tag_set = storage._client.get_object_tagging(Bucket=bucket, Key=key)["TagSet"]
    return {t["Key"]: t["Value"] for t in tag_set}


def test_put_bytes_with_retention_tags_the_object(storage: Storage) -> None:
    storage.put_bytes(
        "src-bucket", "a.bin", b"hi", "application/octet-stream", retention="audio"
    )
    assert _tags(storage, "src-bucket", "a.bin") == {"retention-class": "audio"}


def test_put_bytes_without_retention_is_untagged(storage: Storage) -> None:
    storage.put_bytes("src-bucket", "b.bin", b"hi", "application/octet-stream")
    assert _tags(storage, "src-bucket", "b.bin") == {}


def test_put_json_with_retention_tags_the_object(storage: Storage) -> None:
    storage.put_json("src-bucket", "m.json", {"a": 1}, retention="metadata")
    assert _tags(storage, "src-bucket", "m.json") == {"retention-class": "metadata"}


def test_put_json_without_retention_is_untagged(storage: Storage) -> None:
    storage.put_json("src-bucket", "n.json", {"a": 1})
    assert _tags(storage, "src-bucket", "n.json") == {}


def test_upload_file_with_retention_tags_the_object(
    storage: Storage, tmp_path: Path
) -> None:
    src = tmp_path / "audio.wav"
    src.write_bytes(b"RIFF-wav")
    storage.upload_file(
        src, "dst-bucket", "recordings/x/audio.wav", "audio/wav", retention="audio"
    )
    assert _tags(storage, "dst-bucket", "recordings/x/audio.wav") == {
        "retention-class": "audio"
    }


def test_copy_with_retention_replaces_tags(storage: Storage) -> None:
    storage.put_bytes(
        "src-bucket", "master.webm", b"webm-bytes", "video/webm", retention="audio"
    )
    storage.copy(
        "src-bucket",
        "master.webm",
        "dst-bucket",
        "x/master.webm",
        retention="recording-mp4",
    )
    # REPLACE, not merge: only the new tag lands, the source's "audio" tag does not
    # leak across the copy.
    assert _tags(storage, "dst-bucket", "x/master.webm") == {
        "retention-class": "recording-mp4"
    }


def test_copy_without_retention_is_untagged(storage: Storage) -> None:
    storage.put_bytes("src-bucket", "p.json", b"{}", "application/json")
    storage.copy("src-bucket", "p.json", "dst-bucket", "p.json")
    assert _tags(storage, "dst-bucket", "p.json") == {}


# ---------------------------------------------------------------------------
# The exporter deletes nothing but its own queue markers (design §1.9)
# ---------------------------------------------------------------------------

_EXPORTER_DIR = Path(__file__).resolve().parent.parent / "exporter"
_DELETE_CALLS = {"delete", "delete_object", "delete_objects"}
#: Every delete call the exporter may make, by (module, enclosing function):
#: the pending-queue marker's removal on success (``done``) and on quarantine
#: (``fail``), and ``Storage.delete`` itself, the one S3 delete they go through.
_ALLOWED = {
    ("queue.py", "PendingQueue.done", "delete"),
    ("queue.py", "PendingQueue.fail", "delete"),
    ("storage.py", "Storage.delete", "delete_object"),
}


def _delete_calls(path: Path) -> list[tuple[str, str, str]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: list[tuple[str, str, str]] = []

    def visit(node: ast.AST, scope: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, [*scope, child.name])
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in _DELETE_CALLS
            ):
                found.append((path.name, ".".join(scope), child.func.attr))
            if (
                isinstance(child, ast.Attribute)
                and child.attr in _DELETE_CALLS
                and not any(
                    isinstance(parent, ast.Call) and parent.func is child
                    for parent in ast.walk(node)
                )
            ):
                found.append((path.name, ".".join(scope), child.attr + " (ref)"))
            visit(child, scope)

    visit(tree, [])
    return found


def test_the_only_deletes_are_the_two_queue_marker_sites() -> None:
    calls = [
        call
        for path in sorted(_EXPORTER_DIR.glob("*.py"))
        for call in _delete_calls(path)
    ]
    assert sorted(calls) == sorted(_ALLOWED)


def test_the_delete_guard_sees_a_new_delete_call(tmp_path: Path) -> None:
    """Negative control: a delete added anywhere else is named by the guard."""
    stray = tmp_path / "job.py"
    stray.write_text(
        "def export_meeting(storage):\n"
        "    storage.delete('aw-chatworks-transcribe', 'recordings/x/audio.wav')\n"
        "    remove = storage.delete\n"
    )
    assert _delete_calls(stray) == [
        ("job.py", "export_meeting", "delete"),
        ("job.py", "export_meeting", "delete (ref)"),
    ]
