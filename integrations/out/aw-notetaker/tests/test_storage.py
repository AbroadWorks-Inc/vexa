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
#: Every delete call the exporter may make, by (module, enclosing function):
#: the pending-queue marker's removal on success (``done``) and on quarantine
#: (``fail``), and ``Storage.delete`` itself, the one S3 delete they go through.
_ALLOWED = {
    ("queue.py", "PendingQueue.done", "delete"),
    ("queue.py", "PendingQueue.fail", "delete"),
    ("storage.py", "Storage.delete", "delete_object"),
}


def _is_delete(name: str) -> bool:
    return name.startswith("delete")


def _delete_calls(path: Path, root: Path | None = None) -> list[tuple[str, str, str]]:
    """Every use of a `delete…` function in `path`: a call through an
    attribute (`s.delete(...)`) or a bare name (`delete_object(...)`), a
    `getattr(obj, "delete…")`, and an attribute taken without calling it
    (`remove = s.delete`), each by (module, enclosing function, name)."""
    module = path.relative_to(root).as_posix() if root else path.name
    tree = ast.parse(path.read_text(), filename=str(path))
    found: list[tuple[str, str, str]] = []
    called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}

    def visit(node: ast.AST, scope: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, [*scope, child.name])
                continue
            where = (module, ".".join(scope))
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Attribute) and _is_delete(func.attr):
                    found.append((*where, func.attr))
                elif isinstance(func, ast.Name) and _is_delete(func.id):
                    found.append((*where, func.id))
                elif (
                    isinstance(func, ast.Name)
                    and func.id == "getattr"
                    and len(child.args) >= 2
                    and isinstance(child.args[1], ast.Constant)
                    and isinstance(child.args[1].value, str)
                    and _is_delete(child.args[1].value)
                ):
                    found.append((*where, f"getattr {child.args[1].value}"))
            elif (
                isinstance(child, ast.Attribute)
                and _is_delete(child.attr)
                and id(child) not in called
            ):
                found.append((*where, child.attr + " (ref)"))
            visit(child, scope)

    visit(tree, [])
    return found


def _scan(root: Path) -> list[tuple[str, str, str]]:
    return [
        call
        for path in sorted(root.rglob("*.py"))
        for call in _delete_calls(path, root)
    ]


def test_the_only_deletes_are_the_two_queue_marker_sites() -> None:
    assert sorted(_scan(_EXPORTER_DIR)) == sorted(_ALLOWED)


def _stray(tmp_path: Path, source: str) -> list[tuple[str, str, str]]:
    (tmp_path / "job.py").write_text(source)
    return _scan(tmp_path)


def test_the_delete_guard_sees_an_attribute_call(tmp_path: Path) -> None:
    """Negative control: a delete added anywhere else is named by the guard."""
    assert _stray(
        tmp_path,
        "def export_meeting(storage):\n"
        "    storage.delete('aw-chatworks-transcribe', 'recordings/x/audio.wav')\n",
    ) == [("job.py", "export_meeting", "delete")]


def test_the_delete_guard_sees_an_attribute_reference(tmp_path: Path) -> None:
    assert _stray(
        tmp_path, "def export_meeting(storage):\n    remove = storage.delete\n"
    ) == [("job.py", "export_meeting", "delete (ref)")]


def test_the_delete_guard_sees_a_bare_name_call(tmp_path: Path) -> None:
    assert _stray(
        tmp_path,
        "from somewhere import delete_object\n"
        "def export_meeting():\n"
        "    delete_object(Bucket='aw-chatworks-transcribe', Key='k')\n",
    ) == [("job.py", "export_meeting", "delete_object")]


def test_the_delete_guard_sees_getattr(tmp_path: Path) -> None:
    assert _stray(
        tmp_path,
        "def export_meeting(client):\n"
        "    getattr(client, 'delete_objects')(Bucket='b', Delete={})\n",
    ) == [("job.py", "export_meeting", "getattr delete_objects")]


def test_the_delete_guard_scans_subpackages(tmp_path: Path) -> None:
    sub = tmp_path / "adapters"
    sub.mkdir()
    (sub / "__init__.py").write_text("")
    (sub / "s3.py").write_text("def purge(s):\n    s.delete_object(Bucket='b')\n")
    assert _scan(tmp_path) == [("adapters/s3.py", "purge", "delete_object")]
