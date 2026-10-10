"""recordings — chunk upload + finalize → master in meeting.data JSONB (recording.v1).

Drives the SHIPPED ``upload_chunk`` / ``finalize_master`` / ``build_router`` over the in-memory
fakes, OFFLINE (no MinIO, no DB): chunks fold into the recording's JSONB payload, the master is
built by the golden-locked codec and the media-file stamped finalized, and the upload-token auth +
session-resolution seams behave.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meeting_api.bot_spawn import mint_meeting_token
from meeting_api.recording_codec import build_recording_master
from meeting_api.recordings import build_router, finalize_master, upload_chunk
from meeting_api.recordings.fakes import InMemoryRecordingRepo, InMemoryStorage
from meeting_api.recordings.jsonb import apply_chunk_to_recording, chunk_storage_key

SECRET = "test-admin-token"
USER = 7
MEETING_ID = 1
SESSION_UID = "conn-abc"

# A minimal valid wav file (44-byte RIFF header + 4 bytes of PCM) so the wav master codec runs.
def _wav(n_data: int = 4) -> bytes:
    import struct

    data = b"\x00" * n_data
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, 16000, 32000, 2, 16)
    chunk = struct.pack("<4sI", b"data", len(data)) + data
    riff_len = 4 + len(fmt) + len(chunk)
    return struct.pack("<4sI4s", b"RIFF", riff_len, b"WAVE") + fmt + chunk


# A deterministic COUNTING-PATTERN wav part (#509): part k's PCM is the byte value k repeated
# n_data times, so the assembled master's PCM is arithmetic — any dropped / duplicated /
# overwritten / reordered part is a byte-count or pattern mismatch, every byte accounted for.
_PART_PCM_LEN = 8


def _counting_wav(byte_val: int, n_data: int = _PART_PCM_LEN) -> bytes:
    import struct

    data = bytes([byte_val % 256]) * n_data
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, 16000, 32000, 2, 16)
    chunk = struct.pack("<4sI", b"data", len(data)) + data
    riff_len = 4 + len(fmt) + len(chunk)
    return struct.pack("<4sI4s", b"RIFF", riff_len, b"WAVE") + fmt + chunk


def _seeded():
    repo = InMemoryRecordingRepo()
    repo.seed(meeting_id=MEETING_ID, user_id=USER, session_uid=SESSION_UID)
    return repo, InMemoryStorage()


def _client_for(repo, storage):
    """A TestClient over the SAME repo+storage a test already uploaded chunks into (so the user read
    path GET /recordings -> /master -> /raw sees what upload_chunk wrote)."""
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(build_router(repo, storage, token_secret=SECRET))
    return TestClient(app)


def test_delete_recording_is_owner_scoped_storage_first_and_removes_metadata():
    repo, storage = _seeded()
    repo._meetings[MEETING_ID]["status"] = "completed"
    import asyncio

    receipt = asyncio.run(upload_chunk(
        repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
        data=_wav(), media_format="wav", chunk_seq=0, is_final=True,
    ))
    rid = receipt["recording_id"]
    client = _client_for(repo, storage)

    assert client.delete(f"/recordings/{rid}", headers={"x-user-id": "999"}).status_code == 404
    assert storage.blobs, "another tenant cannot touch storage"
    deleted = client.delete(f"/recordings/{rid}", headers={"x-user-id": str(USER)})
    assert deleted.status_code == 200
    assert deleted.json()["scope"] == "primary_object_storage"
    assert storage.blobs == {}
    assert asyncio.run(repo.get_recordings(MEETING_ID)) == []


def test_delete_recording_storage_failure_keeps_metadata_retryable():
    class FailsOnceStorage(InMemoryStorage):
        def __init__(self):
            super().__init__()
            self.fail = True

        async def delete(self, key: str) -> None:
            if self.fail:
                self.fail = False
                raise RuntimeError("injected delete failure")
            await super().delete(key)

    import asyncio

    repo = InMemoryRecordingRepo()
    repo.seed(
        meeting_id=MEETING_ID, user_id=USER, session_uid=SESSION_UID, status="completed"
    )
    storage = FailsOnceStorage()
    receipt = asyncio.run(upload_chunk(
        repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
        data=_wav(), media_format="wav", chunk_seq=0, is_final=True,
    ))
    rid = receipt["recording_id"]
    client = TestClient(FastAPI(), raise_server_exceptions=False)
    client.app.include_router(build_router(repo, storage, token_secret=SECRET))

    headers = {"x-user-id": str(USER)}
    assert client.delete(f"/recordings/{rid}", headers=headers).status_code == 500
    assert asyncio.run(repo.get_recordings(MEETING_ID))[0]["id"] == rid
    assert client.delete(f"/recordings/{rid}", headers=headers).status_code == 200
    assert asyncio.run(repo.get_recordings(MEETING_ID)) == []


def test_delete_recording_does_not_become_an_active_bot_stop_operation():
    import asyncio

    repo, storage = _seeded()
    receipt = asyncio.run(upload_chunk(
        repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
        data=_wav(), media_format="wav", chunk_seq=0, is_final=False,
    ))
    response = _client_for(repo, storage).delete(
        f"/recordings/{receipt['recording_id']}", headers={"x-user-id": str(USER)}
    )
    assert response.status_code == 409
    assert storage.blobs
    assert asyncio.run(repo.get_recordings(MEETING_ID))


# ── flow: upload folds chunks into JSONB; finalize builds the master ─────────────────────────────

async def test_upload_chunk_writes_recording_jsonb():
    repo, storage = _seeded()
    receipt = await upload_chunk(
        repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
        data=_wav(), media_type="audio", media_format="wav", chunk_seq=0, is_final=False,
    )
    assert receipt["status"] == "in_progress"
    recs = await repo.get_recordings(MEETING_ID)
    assert len(recs) == 1
    mf = recs[0]["media_files"][0]
    assert mf["type"] == "audio"
    assert mf["chunk_count"] == 1
    # The chunk landed in storage under the parent key scheme.
    assert mf["storage_path"] in storage.blobs


async def test_final_chunk_completes_recording():
    repo, storage = _seeded()
    await upload_chunk(repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
                       data=_wav(), media_format="wav", chunk_seq=0, is_final=False)
    receipt = await upload_chunk(repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
                                 data=_wav(), media_format="wav", chunk_seq=1, is_final=True)
    assert receipt["status"] == "completed"


async def test_finalize_master_builds_and_stamps():
    repo, storage = _seeded()
    rid = None
    for seq in range(3):
        receipt = await upload_chunk(
            repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
            data=_wav(), media_format="wav", chunk_seq=seq, is_final=False,
        )
        rid = receipt["recording_id"]
    master_key = await finalize_master(repo, storage, meeting_id=MEETING_ID, recording_id=rid)
    assert master_key.endswith("/audio/master.wav")
    assert master_key in storage.blobs  # the codec-built master was uploaded
    recs = await repo.get_recordings(MEETING_ID)
    mf = recs[0]["media_files"][0]
    assert mf["is_final"] is True
    assert mf["finalized_by"] == "recording_finalizer.master"
    assert mf["storage_path"] == master_key


async def test_finalize_master_does_not_concatenate_a_sibling_whose_name_starts_alike():
    """`.../audio` is a prefix of `.../audio2/`. The list must not pull the sibling in."""
    repo, storage = _seeded()
    audio = [_counting_wav(1), _counting_wav(2)]
    rid = None
    for seq, part in enumerate(audio):
        receipt = await upload_chunk(
            repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
            data=part, media_type="audio", media_format="wav", chunk_seq=seq, is_final=False,
        )
        rid = receipt["recording_id"]
    await upload_chunk(
        repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
        data=_counting_wav(9), media_type="audio2", media_format="wav",
        chunk_seq=0, is_final=False,
    )
    master_key = await finalize_master(
        repo, storage, meeting_id=MEETING_ID, recording_id=rid, media_type="audio",
    )
    assert storage.blobs[master_key] == build_recording_master(audio, "wav")
    sibling_key = await finalize_master(
        repo, storage, meeting_id=MEETING_ID, recording_id=rid, media_type="audio2",
    )
    assert storage.blobs[sibling_key] == build_recording_master([_counting_wav(9)], "wav")


async def test_upload_before_session_is_pending():
    repo, storage = _seeded()
    receipt = await upload_chunk(
        repo, storage, token_meeting_id=MEETING_ID, session_uid="unknown-session",
        data=_wav(), media_format="wav", chunk_seq=0, is_final=False,
    )
    assert receipt == {"status": "pending"}


# ── route: the upload endpoint authenticates the MeetingToken ────────────────────────────────────

def _client():
    from fastapi import FastAPI

    repo, storage = _seeded()
    app = FastAPI()
    app.include_router(build_router(repo, storage, token_secret=SECRET))
    return TestClient(app)


def test_upload_route_requires_token():
    client = _client()
    r = client.post(
        "/internal/recordings/upload",
        data={"session_uid": SESSION_UID, "media_format": "wav", "chunk_seq": 0, "is_final": "true"},
        files={"file": ("c.wav", _wav(), "audio/wav")},
    )
    assert r.status_code == 401  # missing Authorization


def test_upload_route_accepts_valid_token():
    client = _client()
    token = mint_meeting_token(MEETING_ID, USER, "google_meet", "abc", secret=SECRET)
    r = client.post(
        "/internal/recordings/upload",
        headers={"Authorization": f"Bearer {token}"},
        data={"session_uid": SESSION_UID, "media_format": "wav", "chunk_seq": 0, "is_final": "true"},
        files={"file": ("c.wav", _wav(), "audio/wav")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"


def test_upload_route_keeps_channel_identity_from_the_first_chunk():
    """The metadata form part reaches the fold. A second chunk cannot rename the channel."""
    import asyncio

    repo, storage = _seeded()
    client = _client_for(repo, storage)
    token = mint_meeting_token(MEETING_ID, USER, "google_meet", "abc", secret=SECRET)
    first = {
        "session_uid": SESSION_UID,
        "media_type": "ch0",
        "media_format": "webm",
        "chunk_seq": 0,
        "is_final": False,
        "sample_rate": 16000,
        "recorder_start_epoch_ms": 1_700_000_000_000,
        "channel_kind": "gmeet",
        "stream_id": "remote-1",
        "display_name": "Ada",
        "ignored": "nope",
    }
    second = dict(first, chunk_seq=1, display_name="Bob", sample_rate=None)
    headers = {"Authorization": f"Bearer {token}"}
    for body in (first, second):
        posted = client.post(
            "/internal/recordings/upload",
            headers=headers,
            data={"metadata": json.dumps(body)},
            files={"file": ("c.webm", b"chunk", "video/webm")},
        )
        assert posted.status_code == 200, posted.text
    recs = asyncio.run(repo.get_recordings(MEETING_ID))
    mf = next(m for m in recs[0]["media_files"] if m["type"] == "ch0")
    assert mf["metadata"]["display_name"] == "Ada"
    assert mf["metadata"]["channel_kind"] == "gmeet"
    assert mf["metadata"]["stream_id"] == "remote-1"
    assert mf["metadata"]["recorder_start_epoch_ms"] == 1_700_000_000_000
    assert "ignored" not in mf["metadata"]
    assert "sample_rate" not in mf["metadata"]


# ── G4: object-storage I/O must not block the event loop ─────────────────────────────────────────


class _BlockingS3Client:
    """A stub boto3 client whose put_object BLOCKS (sync) — stands in for a slow S3 round-trip."""

    def __init__(self, block_s: float):
        self._block_s = block_s
        self.calls = 0

    def put_object(self, **kw):
        import time

        time.sleep(self._block_s)  # a real, blocking, synchronous call (what boto3 does)
        self.calls += 1
        return {}


async def test_s3_storage_does_not_block_the_event_loop():
    """G4: a blocking boto3 call must run OFF the loop (asyncio.to_thread), so the control plane keeps
    serving lifecycle/webhook/ws traffic during a slow/large S3 op. We run a ~0.3s blocking upload
    concurrently with a 5ms heartbeat — a non-blocking loop ticks many times; a blocked loop ~never."""
    import asyncio

    from meeting_api.recordings.adapters import S3Storage

    class _StubS3(S3Storage):
        def __init__(self, client):
            super().__init__(bucket="b")
            self._stub = client

        def _c(self):
            return self._stub

        # NB: _run is INHERITED (asyncio.to_thread) — that's exactly what's under test.

    storage = _StubS3(_BlockingS3Client(block_s=0.3))
    ticks = {"n": 0}
    stop = {"v": False}

    async def heartbeat():
        while not stop["v"]:
            ticks["n"] += 1
            await asyncio.sleep(0.005)

    hb = asyncio.create_task(heartbeat())
    try:
        await storage.upload("k", b"x" * 1024, content_type="audio/wav")
    finally:
        stop["v"] = True
        await hb

    assert storage._stub.calls == 1
    assert ticks["n"] >= 20, (
        f"event loop appears BLOCKED during the S3 upload (only {ticks['n']} heartbeats in ~0.3s) — "
        "the boto3 call is not being offloaded to a thread"
    )


# ── G3: concurrent chunk folds must not lose updates (atomic read→modify→write) ──────────────────


class _YieldingStorage(InMemoryStorage):
    """An InMemoryStorage whose upload YIELDS the event loop, so two concurrent uploads genuinely
    interleave (forcing the read→modify→write race the atomic mutate must serialize)."""

    async def upload(self, key, data, *, content_type):
        import asyncio

        await asyncio.sleep(0)
        await super().upload(key, data, content_type=content_type)


async def test_concurrent_chunk_uploads_do_not_lose_updates():
    """G3: two chunk uploads racing on the SAME recording must BOTH be folded. The old
    get_recordings → apply → put_recordings ran in SEPARATE transactions, so the second put clobbered
    the first (lost update → chunk_count stuck at 2). The atomic mutate_recordings re-reads the LIVE
    list under one lock and folds cumulatively → chunk_count 3."""
    import asyncio

    repo = InMemoryRecordingRepo()
    repo.seed(meeting_id=MEETING_ID, user_id=USER, session_uid=SESSION_UID)
    storage = _YieldingStorage()

    # chunk 0 (sequential) establishes the recording.
    await upload_chunk(repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
                       data=_wav(), media_format="wav", chunk_seq=0, is_final=False)
    # chunks 1 + 2 race.
    await asyncio.gather(
        upload_chunk(repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
                     data=_wav(), media_format="wav", chunk_seq=1, is_final=False),
        upload_chunk(repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
                     data=_wav(), media_format="wav", chunk_seq=2, is_final=False),
    )

    recs = await repo.get_recordings(MEETING_ID)
    bot_recs = [r for r in recs if r.get("source") == "bot"]
    assert len(bot_recs) == 1, f"exactly one recording for the session, got {len(bot_recs)}"
    mf = next(m for m in bot_recs[0]["media_files"] if m["type"] == "audio")
    assert mf["chunk_count"] == 3, f"all 3 chunks must be folded (no lost update), got {mf['chunk_count']}"


async def test_concurrent_first_chunks_of_several_media_types_share_one_recording_prefix():
    """A per-channel bot uploads chunk 0 of `audio`, `ch0` and `ch1` at the same moment. Every
    object must land under the ONE recording the JSONB keeps, or the audio master is built without
    chunk 0 (no container header) and cannot be decoded."""
    import asyncio

    repo, _ = _seeded()
    storage = _YieldingStorage()
    await asyncio.gather(*(
        upload_chunk(repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
                     data=_counting_wav(seq), media_type=media_type, media_format="wav",
                     chunk_seq=0, is_final=False)
        for seq, media_type in enumerate(("audio", "ch0", "ch1"))
    ))
    await upload_chunk(repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
                       data=_counting_wav(9), media_type="audio", media_format="wav",
                       chunk_seq=1, is_final=True)

    recs = [r for r in await repo.get_recordings(MEETING_ID) if r.get("source") == "bot"]
    assert len(recs) == 1
    rid = recs[0]["id"]
    keys = await storage.list("recordings/")
    assert keys and all(f"/{rid}/" in k for k in keys), keys

    master_key = await finalize_master(repo, storage, meeting_id=MEETING_ID, recording_id=rid)
    master = await storage.get(master_key)
    assert master == build_recording_master([_counting_wav(0), _counting_wav(9)], "wav")


# ── #509 C2: retrieval serves the assembled master, never the empty final-signal chunk ────────────
# V1/#491 — a confirmed multi-chunk upload downloads BYTE-COMPLETE via /master and /raw.
# V2/#412 — every uploaded part is kept; a crashed (no-final) recording still retrieves its parts.

_HDRS = {"x-user-id": str(USER)}


def _first_audio(rec: dict) -> dict:
    return next(m for m in rec["media_files"] if m["type"] == "audio")


async def test_multichunk_plus_empty_final_raw_serves_master_not_signal_chunk():
    """A2 (V1/#491): N counting-pattern data chunks + an empty is_final signal, all folded, then
    GET .../media/{id}/raw serves the ASSEMBLED master BYTE-COMPLETE — never the zero-byte signal
    chunk. RED at base: /raw trusted the media-file is_final flag and served storage_path (the
    0-byte final chunk) directly."""
    repo, storage = _seeded()
    n = 5
    parts = [_counting_wav(k) for k in range(n)]
    for k, part in enumerate(parts):
        await upload_chunk(
            repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
            data=part, media_type="audio", media_format="wav", chunk_seq=k, is_final=False,
        )
    # The empty is_final "signal" chunk — a zero-byte COMPLETED marker, NOT playable bytes.
    receipt = await upload_chunk(
        repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
        data=b"", media_type="audio", media_format="wav", chunk_seq=n, is_final=True,
    )
    assert receipt["status"] == "completed"

    client = _client_for(repo, storage)
    listed = client.get("/recordings", headers=_HDRS)
    assert listed.status_code == 200, listed.text
    recs = listed.json()["recordings"]
    assert len(recs) == 1
    rec = recs[0]
    rid, mf = rec["id"], _first_audio(rec)
    # A3 defence-in-depth: the stored pointer must never be the zero-byte final-signal chunk.
    # Read off the DETAIL route: `storage_path` is upload bookkeeping and the list row no longer
    # carries it (fr_db203061a7a1d953) — the property is about what is stored, not about which
    # route shows it.
    detail = client.get(f"/recordings/{rid}", headers=_HDRS)
    assert detail.status_code == 200, detail.text
    detail_mf = _first_audio(detail.json())
    assert not detail_mf["storage_path"].endswith(f"/audio/{n:06d}.wav"), detail_mf["storage_path"]

    # Hit /raw DIRECTLY (no prior /master) — finalize-on-read must assemble + serve the master.
    raw = client.get(f"/recordings/{rid}/media/{mf['id']}/raw?type=audio", headers=_HDRS)
    assert raw.status_code == 200, raw.text
    oracle = build_recording_master(parts, "wav")
    assert raw.content == oracle, "raw must byte-equal the codec master oracle"
    # Independent arithmetic oracle: the PCM payload is exactly each part's counting bytes, in order.
    assert raw.content[44:] == b"".join(bytes([k]) * _PART_PCM_LEN for k in range(n))
    assert len(raw.content) > 44, "must not be the zero-byte signal chunk"


async def test_multichunk_full_read_path_master_then_raw_byte_complete():
    """A2 (V1/#491) full player path: GET /recordings -> /recordings/{id} -> /master -> /raw, each
    step succeeds and the bytes are the complete assembled master."""
    repo, storage = _seeded()
    n = 4
    parts = [_counting_wav(k) for k in range(n)]
    for k, part in enumerate(parts):
        await upload_chunk(
            repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
            data=part, media_type="audio", media_format="wav", chunk_seq=k, is_final=False,
        )
    await upload_chunk(
        repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
        data=b"", media_type="audio", media_format="wav", chunk_seq=n, is_final=True,
    )
    client = _client_for(repo, storage)
    rec = client.get("/recordings", headers=_HDRS).json()["recordings"][0]
    rid = rec["id"]
    detail = client.get(f"/recordings/{rid}", headers=_HDRS)
    assert detail.status_code == 200, detail.text

    master = client.get(f"/recordings/{rid}/master?type=audio", headers=_HDRS)
    assert master.status_code == 200, master.text
    body = master.json()
    assert body["storage_path"].endswith("/audio/master.wav"), body["storage_path"]
    assert body["raw_url"], body

    raw = client.get(body["raw_url"], headers=_HDRS)
    assert raw.status_code == 200, raw.text
    assert raw.content == build_recording_master(parts, "wav")


async def test_crash_no_final_master_serves_uploaded_parts():
    """A1 (V2/#412) offline: a bot killed after part 3 (NO is_final) leaves parts 0-2 durable; the
    recording stays in_progress but /raw finalizes-on-read to EXACTLY those 3 parts concatenated —
    nothing lost, no all-or-nothing. Download must NOT require status==completed."""
    repo, storage = _seeded()
    parts = [_counting_wav(k) for k in range(3)]
    rid = None
    for k, part in enumerate(parts):
        r = await upload_chunk(
            repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
            data=part, media_type="audio", media_format="wav", chunk_seq=k, is_final=False,
        )
        rid = r["recording_id"]
    # No final chunk (SIGKILL) — status stays IN_PROGRESS.
    recs = await repo.get_recordings(MEETING_ID)
    rec = next(r for r in recs if r["id"] == rid)
    assert rec["status"] == "in_progress"

    client = _client_for(repo, storage)
    listed = client.get("/recordings", headers=_HDRS).json()["recordings"][0]
    mf = _first_audio(listed)
    raw = client.get(f"/recordings/{rid}/media/{mf['id']}/raw?type=audio", headers=_HDRS)
    assert raw.status_code == 200, raw.text
    assert raw.content == build_recording_master(parts, "wav")
    assert raw.content[44:] == b"".join(bytes([k]) * _PART_PCM_LEN for k in range(3))


def test_empty_final_fold_never_points_storage_at_signal_chunk():
    """A3 (unit): folding an empty is_final chunk keeps storage_path on the prior DATA chunk, never
    the zero-byte signal object, and still flips the recording to completed. Reverting the jsonb hunk
    makes storage_path the empty chunk key -> red."""
    data_key = chunk_storage_key(
        user_id=USER, recording_id=123, session_uid=SESSION_UID,
        media_type="audio", media_format="wav", chunk_seq=0,
    )
    rec, _ = apply_chunk_to_recording(
        None, recording_id=123, meeting_id=MEETING_ID, user_id=USER, session_uid=SESSION_UID,
        media_type="audio", media_format="wav", storage_path=data_key, file_size=100,
        chunk_seq=0, is_final=False, duration_seconds=None, sample_rate=None,
    )
    signal_key = chunk_storage_key(
        user_id=USER, recording_id=123, session_uid=SESSION_UID,
        media_type="audio", media_format="wav", chunk_seq=1,
    )
    rec2, transitioned = apply_chunk_to_recording(
        rec, recording_id=123, meeting_id=MEETING_ID, user_id=USER, session_uid=SESSION_UID,
        media_type="audio", media_format="wav", storage_path=signal_key, file_size=0,
        chunk_seq=1, is_final=True, duration_seconds=None, sample_rate=None,
    )
    mf = next(m for m in rec2["media_files"] if m["type"] == "audio")
    assert mf["storage_path"] == data_key, "kept the data chunk, not the zero-byte signal object"
    assert mf["storage_path"] != signal_key
    assert rec2["status"] == "completed", "empty final still completes the recording"
    assert transitioned is True


def _fold_audio(prior, *, chunk_seq, file_size, sample_rate, chunk_metadata=None, storage_path="k"):
    return apply_chunk_to_recording(
        prior, recording_id=123, meeting_id=MEETING_ID, user_id=USER, session_uid=SESSION_UID,
        media_type="ch0", media_format="webm", storage_path=storage_path, file_size=file_size,
        chunk_seq=chunk_seq, is_final=False, duration_seconds=None, sample_rate=sample_rate,
        chunk_metadata=chunk_metadata,
    )


def test_channel_identity_sticks_to_the_first_chunk():
    """Chunk 0 stores the five channel fields beside sample_rate and drops unknown or invalid ones.
    Chunk 1 may try to rename the speaker; the stored identity stays, and an omitted sample_rate
    is still dropped (that field is rebuilt from the chunk in hand)."""
    rec, _ = _fold_audio(
        None, chunk_seq=0, file_size=100, sample_rate=16000,
        chunk_metadata={
            "recorder_start_epoch_ms": 1_700_000_000_000,
            "channel_kind": "jitsi",
            "stream_id": "remote-audio-0",
            "participant_id": "p-1",
            "display_name": "Ada",
            "ignored": "nope",
        },
    )
    mf = next(m for m in rec["media_files"] if m["type"] == "ch0")
    assert mf["metadata"] == {
        "sample_rate": 16000,
        "recorder_start_epoch_ms": 1_700_000_000_000,
        "channel_kind": "jitsi",
        "stream_id": "remote-audio-0",
        "participant_id": "p-1",
        "display_name": "Ada",
    }

    rejected, _ = _fold_audio(
        None, chunk_seq=0, file_size=100, sample_rate=16000,
        chunk_metadata={
            "recorder_start_epoch_ms": True,
            "channel_kind": "zoom",
            "stream_id": "",
            "participant_id": "x" * 201,
            "display_name": 12,
        },
    )
    rejected_mf = next(m for m in rejected["media_files"] if m["type"] == "ch0")
    assert rejected_mf["metadata"] == {"sample_rate": 16000}

    renamed, _ = _fold_audio(
        rec, chunk_seq=1, file_size=50, sample_rate=None,
        chunk_metadata={"display_name": "Bob", "channel_kind": "gmeet"},
    )
    renamed_mf = next(m for m in renamed["media_files"] if m["type"] == "ch0")
    assert renamed_mf["metadata"] == {
        "recorder_start_epoch_ms": 1_700_000_000_000,
        "channel_kind": "jitsi",
        "stream_id": "remote-audio-0",
        "participant_id": "p-1",
        "display_name": "Ada",
    }


async def test_single_final_chunk_downloads_byte_complete():
    """A4 (no-regression): today's single-master-equivalent writer (ONE is_final chunk carrying
    data) still lists, masters, and /raw-downloads byte-complete."""
    repo, storage = _seeded()
    part = _counting_wav(9, n_data=16)
    r = await upload_chunk(
        repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
        data=part, media_type="audio", media_format="wav", chunk_seq=0, is_final=True,
    )
    assert r["status"] == "completed"
    rid = r["recording_id"]

    client = _client_for(repo, storage)
    rec = client.get("/recordings", headers=_HDRS).json()["recordings"][0]
    mf = _first_audio(rec)
    raw = client.get(f"/recordings/{rid}/media/{mf['id']}/raw?type=audio", headers=_HDRS)
    assert raw.status_code == 200, raw.text
    assert raw.content == build_recording_master([part], "wav")
    assert raw.content[44:] == bytes([9]) * 16


# ── #768: a mid-recording read must NOT freeze the master (finalize is re-assemblable) ────────────


async def test_finalize_after_midread_reassembles_all_chunks():
    """#768 (the exact prod scenario): a GET /master while the meeting is STILL recording must not
    permanently freeze the master. Two chunks land; a mid-recording finalize assembles a 2-chunk
    partial master; three more chunks arrive and the recording completes; the next finalize must
    REBUILD the master to contain ALL five chunks. RED on base: finalize short-circuits on
    ``storage.exists(master_key)`` and never rebuilds → the served master stays the 2-chunk partial
    (in prod: a 4h meeting frozen at 49s, 6.6% of the audio)."""
    repo, storage = _seeded()
    early = [_counting_wav(k) for k in range(2)]
    rid = None
    for k, part in enumerate(early):
        r = await upload_chunk(
            repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
            data=part, media_type="audio", media_format="wav", chunk_seq=k, is_final=False,
        )
        rid = r["recording_id"]
    # Mid-recording read: assemble a PARTIAL master (the prod "check on the recording" gesture).
    mid_key = await finalize_master(repo, storage, meeting_id=MEETING_ID, recording_id=rid)
    assert storage.blobs[mid_key] == build_recording_master(early, "wav"), "partial master = 2 chunks"

    # More chunks arrive AFTER the read, then the meeting ends (empty is_final signal).
    late = [_counting_wav(k) for k in range(2, 5)]
    for k, part in enumerate(late, start=2):
        await upload_chunk(
            repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
            data=part, media_type="audio", media_format="wav", chunk_seq=k, is_final=False,
        )
    await upload_chunk(
        repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
        data=b"", media_type="audio", media_format="wav", chunk_seq=5, is_final=True,
    )

    # The finalize on completion must REASSEMBLE all five chunks — not serve the frozen partial.
    final_key = await finalize_master(repo, storage, meeting_id=MEETING_ID, recording_id=rid)
    all_parts = early + late
    assert storage.blobs[final_key] == build_recording_master(all_parts, "wav"), (
        "the completed master must contain ALL chunks, not the mid-read partial"
    )
    # Independent arithmetic oracle: the PCM is each part's counting bytes, in order, none dropped.
    assert storage.blobs[final_key][44:] == b"".join(bytes([k]) * _PART_PCM_LEN for k in range(5))


async def test_master_route_reflects_late_chunks_after_midread():
    """#768 at the route altitude: GET /master mid-recording, then more chunks + completion, then
    GET .../raw serves the byte-complete master. RED on base: the first /master freezes it."""
    repo, storage = _seeded()
    early = [_counting_wav(k) for k in range(2)]
    rid = None
    for k, part in enumerate(early):
        r = await upload_chunk(
            repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
            data=part, media_type="audio", media_format="wav", chunk_seq=k, is_final=False,
        )
        rid = r["recording_id"]
    client = _client_for(repo, storage)
    # Mid-recording read via the route (this is what froze prod).
    assert client.get(f"/recordings/{rid}/master?type=audio", headers=_HDRS).status_code == 200
    late = [_counting_wav(k) for k in range(2, 5)]
    for k, part in enumerate(late, start=2):
        await upload_chunk(
            repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
            data=part, media_type="audio", media_format="wav", chunk_seq=k, is_final=False,
        )
    await upload_chunk(
        repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
        data=b"", media_type="audio", media_format="wav", chunk_seq=5, is_final=True,
    )
    rec = client.get("/recordings", headers=_HDRS).json()["recordings"][0]
    mf = _first_audio(rec)
    raw = client.get(f"/recordings/{rid}/media/{mf['id']}/raw?type=audio", headers=_HDRS)
    assert raw.status_code == 200, raw.text
    assert raw.content == build_recording_master(early + late, "wav")


# ── #769: chunk listing must paginate past the S3 1000-key cap ────────────────────────────────────


class _PagedS3Client:
    """A stub boto3 S3 client whose ``list_objects_v2`` paginates at ``PAGE`` keys — mirroring the
    real S3/S3-compatible 1000-key response cap — signalling more via ``IsTruncated`` +
    ``NextContinuationToken`` (an opaque offset here)."""

    PAGE = 1000

    def __init__(self, keys):
        self._keys = sorted(keys)

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None, **kw):
        matched = [k for k in self._keys if k.startswith(Prefix)]
        start = int(ContinuationToken) if ContinuationToken else 0
        page = matched[start : start + self.PAGE]
        resp = {"Contents": [{"Key": k} for k in page]}
        nxt = start + self.PAGE
        if nxt < len(matched):
            resp["IsTruncated"] = True
            resp["NextContinuationToken"] = str(nxt)
        else:
            resp["IsTruncated"] = False
        return resp


async def test_s3_storage_list_paginates_past_1000_keys():
    """#769: a single ``list_objects_v2`` caps at 1000 keys and signals more via IsTruncated /
    NextContinuationToken. ``S3Storage.list`` must loop to exhaustion. RED on base: the single
    unpaginated call returns only the first 1000 of 1500 keys, silently dropping 500 chunks."""
    from meeting_api.recordings.adapters import S3Storage

    prefix = "recordings/7/42/sess/audio/"
    keys = [f"{prefix}{i:06d}.wav" for i in range(1500)]

    class _Stub(S3Storage):
        def __init__(self, client):
            super().__init__(bucket="b")
            self._stub = client

        def _c(self):
            return self._stub

    storage = _Stub(_PagedS3Client(keys))
    listed = await storage.list(prefix)
    assert len(listed) == 1500, f"expected all 1500 keys across pages, got {len(listed)}"
    assert listed == sorted(keys)


def test_a_persisted_path_outside_the_owners_namespace_is_never_deleted():
    """A ``storage_path`` is data, not authority: deletion stays inside ``recordings/{user_id}/``.

    Server-derived paths always satisfy this, so the negative control has to forge one — which is
    exactly the precondition worth pinning, because the day a ``storage_path`` becomes writable
    from a request is the day this is the only thing standing between a delete and another tenant.
    """
    import asyncio

    from meeting_api.recordings.deletion import recording_object_keys

    storage = InMemoryStorage()
    mine = f"recordings/{USER}/1/{SESSION_UID}/audio/000000.wav"
    theirs = "recordings/999/1/conn-xyz/audio/000000.wav"
    storage.blobs[mine] = b"mine"
    storage.blobs[theirs] = b"theirs"

    recording = {
        "id": 1, "user_id": USER, "session_uid": SESSION_UID,
        "media_files": [{"storage_path": mine}, {"storage_path": theirs}],
    }

    keys = asyncio.run(recording_object_keys(storage, recording))

    assert mine in keys
    assert theirs not in keys, "a foreign-owner path must never enter the delete set"


# ── streamed master: bounded parallel reads, written in parts ────────────────────────────────────


class _ReadTrackingStorage(InMemoryStorage):
    """Counts chunk reads in flight (each read yields the loop) and how many reads had started when
    the master's first piece reached ``upload_stream``."""

    def __init__(self):
        super().__init__()
        self.in_flight = 0
        self.max_in_flight = 0
        self.reads_started = 0
        self.reads_started_at_first_piece = None

    async def get(self, key: str) -> bytes:
        import asyncio

        if "/master." not in key:
            self.reads_started += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            await asyncio.sleep(0)
            self.in_flight -= 1
        return await super().get(key)

    async def upload_stream(self, key, pieces, *, content_type):
        async def watched():
            async for piece in pieces:
                if self.reads_started_at_first_piece is None:
                    self.reads_started_at_first_piece = self.reads_started
                yield piece

        await super().upload_stream(key, watched(), content_type=content_type)


async def _upload_counting_parts(repo, storage, count):
    rid = None
    for seq in range(count):
        receipt = await upload_chunk(
            repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
            data=_counting_wav(seq), media_format="wav", chunk_seq=seq, is_final=False,
        )
        rid = receipt["recording_id"]
    return rid


async def test_finalize_reads_chunks_in_parallel_up_to_the_window_and_keeps_their_order():
    from meeting_api.recordings.service import MASTER_READ_WINDOW

    repo = InMemoryRecordingRepo()
    repo.seed(meeting_id=MEETING_ID, user_id=USER, session_uid=SESSION_UID)
    storage = _ReadTrackingStorage()
    parts = 5 * MASTER_READ_WINDOW
    rid = await _upload_counting_parts(repo, storage, parts)

    master_key = await finalize_master(repo, storage, meeting_id=MEETING_ID, recording_id=rid)

    assert storage.max_in_flight == MASTER_READ_WINDOW
    assert storage.blobs[master_key] == build_recording_master(
        [_counting_wav(seq) for seq in range(parts)], "wav"
    )


async def test_finalize_streams_the_master_before_every_chunk_is_read():
    from meeting_api.recordings.service import MASTER_READ_WINDOW

    repo = InMemoryRecordingRepo()
    repo.seed(meeting_id=MEETING_ID, user_id=USER, session_uid=SESSION_UID)
    storage = _ReadTrackingStorage()
    parts = 5 * MASTER_READ_WINDOW
    rid = await _upload_counting_parts(repo, storage, parts)

    await finalize_master(repo, storage, meeting_id=MEETING_ID, recording_id=rid)

    assert storage.reads_started_at_first_piece <= MASTER_READ_WINDOW + 1 < parts


class _MultipartS3Client:
    """A stub boto3 S3 client that records single and multipart writes; ``fail_part`` makes that
    part number raise."""

    def __init__(self, fail_part=None):
        self.fail_part = fail_part
        self.calls: list[str] = []
        self.parts: list[tuple[int, int]] = []
        self.completed_parts = None
        self.put_body = None

    def put_object(self, **kw):
        self.calls.append("put_object")
        self.put_body = kw["Body"]
        return {}

    def create_multipart_upload(self, **kw):
        self.calls.append("create_multipart_upload")
        return {"UploadId": "up-1"}

    def upload_part(self, **kw):
        self.calls.append("upload_part")
        if kw["PartNumber"] == self.fail_part:
            raise RuntimeError("part upload failed")
        self.parts.append((kw["PartNumber"], len(kw["Body"])))
        return {"ETag": f"etag-{kw['PartNumber']}"}

    def complete_multipart_upload(self, **kw):
        self.calls.append("complete_multipart_upload")
        self.completed_parts = kw["MultipartUpload"]["Parts"]
        return {}

    def abort_multipart_upload(self, **kw):
        self.calls.append("abort_multipart_upload")
        return {}


def _s3_over(client):
    from meeting_api.recordings.adapters import S3Storage

    class _Stub(S3Storage):
        def _c(self):
            return client

    return _Stub(bucket="b")


async def _mib_pieces(count):
    for _ in range(count):
        yield b"\x01" * (1024 * 1024)


async def test_s3_upload_stream_below_one_part_is_a_single_put():
    client = _MultipartS3Client()
    await _s3_over(client).upload_stream("k", _mib_pieces(3), content_type="video/webm")
    assert client.calls == ["put_object"]
    assert len(client.put_body) == 3 * 1024 * 1024


async def test_s3_upload_stream_writes_ordered_parts_of_the_part_size_and_a_short_last_one():
    from meeting_api.recordings.adapters import MULTIPART_PART_BYTES

    client = _MultipartS3Client()
    await _s3_over(client).upload_stream("k", _mib_pieces(20), content_type="video/webm")
    mib = 1024 * 1024
    assert client.parts == [(1, MULTIPART_PART_BYTES), (2, MULTIPART_PART_BYTES), (3, 20 * mib - 2 * MULTIPART_PART_BYTES)]
    assert client.completed_parts == [
        {"ETag": "etag-1", "PartNumber": 1},
        {"ETag": "etag-2", "PartNumber": 2},
        {"ETag": "etag-3", "PartNumber": 3},
    ]
    assert "put_object" not in client.calls


async def test_s3_upload_stream_aborts_the_multipart_upload_when_a_part_fails():
    client = _MultipartS3Client(fail_part=2)
    with pytest.raises(RuntimeError, match="part upload failed"):
        await _s3_over(client).upload_stream("k", _mib_pieces(20), content_type="video/webm")
    assert client.calls[-1] == "abort_multipart_upload"
    assert "complete_multipart_upload" not in client.calls


def test_a_resent_chunk_0_never_blanks_the_stored_identity():
    """F3: a re-sent (or defaulted) chunk 0 without identity keeps what is stored."""
    identity = {"recorder_start_epoch_ms": 1_700_000_000_000, "channel_kind": "jitsi",
                "stream_id": "remote-audio-2", "display_name": "Ada"}
    rec, _ = _fold_audio(None, chunk_seq=0, file_size=100, sample_rate=16000, chunk_metadata=identity)
    again, _ = _fold_audio(rec, chunk_seq=0, file_size=100, sample_rate=16000, chunk_metadata={})
    mf = next(m for m in again["media_files"] if m["type"] == "ch0")
    assert mf["metadata"] == {"sample_rate": 16000, **identity}


def test_a_name_resolved_after_the_recorder_started_is_stored():
    """F3: chunk 0 had no name; the first chunk that carries one fills it, and it then sticks."""
    start = {"recorder_start_epoch_ms": 1_700_000_000_000, "channel_kind": "jitsi",
             "stream_id": "remote-audio-4"}
    rec, _ = _fold_audio(None, chunk_seq=0, file_size=100, sample_rate=16000, chunk_metadata=start)
    named, _ = _fold_audio(rec, chunk_seq=1, file_size=50, sample_rate=16000,
                           chunk_metadata={**start, "participant_id": "p-9", "display_name": "Grace"})
    later, _ = _fold_audio(named, chunk_seq=2, file_size=50, sample_rate=16000,
                           chunk_metadata={**start, "display_name": "Someone else"})
    mf = next(m for m in later["media_files"] if m["type"] == "ch0")
    assert mf["metadata"]["display_name"] == "Grace"
    assert mf["metadata"]["participant_id"] == "p-9"


def test_a_channel_chunk_without_its_number_is_refused_but_a_whole_upload_is_not():
    repo, storage = _seeded()
    client = _client_for(repo, storage)
    token = mint_meeting_token(MEETING_ID, USER, "google_meet", "abc", secret=SECRET)
    headers = {"Authorization": f"Bearer {token}"}

    def post(meta: dict) -> int:
        return client.post(
            "/internal/recordings/upload", headers=headers,
            data={"metadata": json.dumps({"session_uid": SESSION_UID, **meta})},
            files={"file": ("c.webm", b"chunk", "video/webm")},
        ).status_code

    assert post({"media_type": "ch3", "media_format": "webm", "is_final": False}) == 422
    assert post({"media_type": "audio", "media_format": "webm"}) == 200
