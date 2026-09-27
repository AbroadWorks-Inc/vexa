"""§1.13 erasure — upstream's completed-artifact deletion as one callable, and ``DELETE /v2/meetings/{id}``.

``collector.app.delete_completed_artifacts(store, deleter, user_id, meeting_id)`` is the terminal
branch of upstream's ``DELETE /meetings/{id}``: storage first, then the transcript rows, the
meeting row kept. Both routes call it.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from meeting_api.collector.app import delete_completed_artifacts
from meeting_api.collector.fakes import InMemoryTranscriptStore
from meeting_api.recordings.deletion import delete_recording_objects
from meeting_api.recordings.fakes import InMemoryStorage

OWNER = 7
OTHER = 8
MEETING_ID = 41
RECORDING_ID = 9001
PREFIX = f"recordings/{OWNER}/{RECORDING_ID}/sess-41/audio/"


def _recording() -> dict:
    return {
        "id": RECORDING_ID,
        "meeting_id": MEETING_ID,
        "user_id": OWNER,
        "session_uid": "sess-41",
        "status": "completed",
        "media_files": [
            {
                "id": 22,
                "type": "audio",
                "format": "wav",
                "storage_path": f"{PREFIX}master.wav",
            }
        ],
    }


def _artifact_store(status: str = "completed") -> InMemoryTranscriptStore:
    store = InMemoryTranscriptStore()
    store.seed_meeting(
        meeting_id=MEETING_ID,
        user_id=OWNER,
        platform="google_meet",
        native_meeting_id="private-room",
        status=status,
        data={"recordings": [_recording()], "notes": "derived summary"},
        segments=[
            {
                "segment_id": "s1",
                "start": 0,
                "end": 1,
                "text": "confidential",
                "language": "en",
            }
        ],
    )
    return store


def _storage() -> InMemoryStorage:
    storage = InMemoryStorage()
    storage.blobs[f"{PREFIX}000000.wav"] = b"chunk"
    storage.blobs[f"{PREFIX}master.wav"] = b"master"
    return storage


def _deleter(storage: InMemoryStorage):
    async def delete(recording: dict) -> list[str]:
        return await delete_recording_objects(storage, recording)

    return delete


# ── the callable ─────────────────────────────────────────────────────────────────────────────


async def test_the_callable_deletes_objects_then_transcripts_and_keeps_the_row():
    store, storage = _artifact_store(), _storage()

    result = await delete_completed_artifacts(
        store, _deleter(storage), OWNER, MEETING_ID
    )

    assert result == {
        "kind": "artifacts",
        "objects_deleted": 2,
        "already_deleted": False,
    }
    assert storage.blobs == {}
    meeting = store._meetings[MEETING_ID]
    assert meeting["status"] == "completed"
    assert meeting["segments"] == {}
    assert "recordings" not in meeting["data"]


async def test_the_callable_refuses_a_live_meeting_and_another_account():
    live, storage = _artifact_store("active"), _storage()
    with pytest.raises(HTTPException) as conflict:
        await delete_completed_artifacts(live, _deleter(storage), OWNER, MEETING_ID)
    assert conflict.value.status_code == 409

    with pytest.raises(HTTPException) as missing:
        await delete_completed_artifacts(
            _artifact_store(), _deleter(storage), OTHER, MEETING_ID
        )
    assert missing.value.status_code == 404
    assert len(storage.blobs) == 2


async def test_the_callable_needs_a_deleter_when_there_are_recordings():
    store = _artifact_store()
    with pytest.raises(HTTPException) as unavailable:
        await delete_completed_artifacts(store, None, OWNER, MEETING_ID)
    assert unavailable.value.status_code == 503
    assert store._meetings[MEETING_ID]["segments"]["s1"]["text"] == "confidential"
