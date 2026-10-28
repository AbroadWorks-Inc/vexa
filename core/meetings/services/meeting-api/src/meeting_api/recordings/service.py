"""The recordings flow — chunk upload + finalize → master in ``meeting.data`` JSONB.

Port of the parent ``recordings.internal_upload_recording`` + ``recording_finalizer`` CORE:

  * ``upload_chunk(...)`` — verify the MeetingToken, resolve the bot's ``MeetingSession`` by
    ``session_uid``, upload the chunk to object storage, fold it into the recording's JSONB payload
    (``jsonb.apply_chunk_to_recording``) under a read-modify-write on ``meeting.data['recordings']``,
    and return the upload receipt.
  * ``finalize_master(...)`` — stream a recording media-file's chunks through the golden-locked
    codec (``MasterWriter``) into the master object, and stamp the JSONB media-file
    (``storage_path`` → master key, ``finalized_by``, ``is_final``, ``playback_url``). Chunks are
    read ``MASTER_READ_WINDOW`` at a time and the master is written in parts, so time and memory
    stay flat however long the meeting ran.

The codec itself (``meeting_api.build_recording_master``, recording.v1) is already ported +
golden-locked — this module only orchestrates the IO + the JSONB bookkeeping around it.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import aclosing
from datetime import datetime, timezone
from itertools import islice
from typing import Any, AsyncGenerator, Optional, Sequence

from ..obs import log_event
from ..recording_codec import MasterWriter
from .jsonb import (
    SIGNAL_TAPE_PARTS,
    SIGNAL_TAPE_PART_FORMATS,
    apply_chunk_to_recording,
    chunk_storage_key,
    master_storage_key,
    session_recording_numeric_id,
    signal_tape_key,
)
from .ports import RecordingRepo, Storage

# Media content types (parent ``recording_codec._media_content_type``, reduced to the core set).
_CONTENT_TYPES = {"webm": "video/webm", "wav": "audio/wav", "jsonl": "application/x-ndjson",
                  "txt": "text/plain; charset=utf-8"}

# Chunk reads in flight while a master is assembled. A 2-hour track is ~480 chunks; read one at a
# time it took 38 s (past the gateway's 30 s), 8 at a time 2.4 s, 16 at a time 1.7 s. 8 leaves the
# storage thread pool free for the live chunk uploads.
MASTER_READ_WINDOW = 8

# The ``media_type`` that means "a captured-signal tape, not playable media" (O-TEL-1).
SIGNAL_MEDIA_TYPE = "signal"
SIGNAL_MEDIA_FORMAT = "jsonl"


def _content_type(media_format: str) -> str:
    return _CONTENT_TYPES.get(media_format, "application/octet-stream")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SessionNotFound(Exception):
    """The upload's ``session_uid`` matches no MeetingSession AND it is the final chunk → 404."""


class InvalidSignalTape(Exception):
    """A tape upload named a part or format we do not accept → 422 (never a silent store)."""


async def upload_chunk(
    repo: RecordingRepo,
    storage: Storage,
    *,
    token_meeting_id: Optional[int],
    session_uid: str,
    data: bytes,
    media_type: str = "audio",
    media_format: str = "wav",
    chunk_seq: int = 0,
    is_final: bool = True,
    duration_seconds: Optional[float] = None,
    sample_rate: Optional[int] = None,
    chunk_metadata: Optional[dict] = None,
) -> dict:
    """Process ONE recording chunk upload. ``token_meeting_id`` is the verified MeetingToken's
    meeting_id (the route verifies the token before calling this).

    Returns ``{recording_id, media_file_id, storage_path, status, chunk_seq}``. When the session is
    not yet known and the chunk is non-final, returns ``{"status": "pending"}`` (the bot retries).
    """
    session = await repo.find_session(session_uid)
    if session is None:
        if not is_final:
            return {"status": "pending"}
        raise SessionNotFound(f"no MeetingSession for session_uid {session_uid}")

    meeting_id = session["meeting_id"]
    if token_meeting_id is not None and meeting_id != token_meeting_id:
        # A MeetingToken was used and was minted for a different meeting — fail closed.
        # (token_meeting_id is None for internal-secret auth, which is already meeting-scoped by session.)
        raise SessionNotFound("MeetingToken meeting_id does not match the session's meeting")

    owner = await repo.owner_of(meeting_id)

    # Find / start the bot recording for this session.
    recordings = await repo.get_recordings(meeting_id)
    existing_rec = next(
        (r for r in recordings if r.get("session_uid") == session_uid and r.get("source") == "bot"),
        None,
    )
    recording_id = existing_rec["id"] if existing_rec else session_recording_numeric_id(session_uid)

    # Upload the chunk to object storage (idempotent by key; OUTSIDE the row lock).
    key = chunk_storage_key(
        user_id=owner or 0, recording_id=recording_id, session_uid=session_uid,
        media_type=media_type, media_format=media_format, chunk_seq=chunk_seq,
    )
    await storage.upload(key, data, content_type=_content_type(media_format))

    # G3 — fold the chunk into the JSONB ATOMICALLY: the mutator reads the LIVE recordings under one
    # row lock and folds cumulatively, so a concurrent chunk/finalize can't clobber it (the old
    # get_recordings → apply → put_recordings were SEPARATE transactions → lost update). The mutator
    # re-resolves the recording for this session, so it reuses an id created concurrently.
    def _fold(recs):
        ex = next(
            (r for r in recs if r.get("session_uid") == session_uid and r.get("source") == "bot"), None
        )
        rid = ex["id"] if ex else recording_id
        payload, transitioned_ = apply_chunk_to_recording(
            ex,
            recording_id=rid, meeting_id=meeting_id, user_id=owner or 0,
            session_uid=session_uid, media_type=media_type, media_format=media_format,
            storage_path=key, file_size=len(data), chunk_seq=chunk_seq, is_final=is_final,
            duration_seconds=duration_seconds, sample_rate=sample_rate,
            chunk_metadata=chunk_metadata,
        )
        others = [r for r in recs if r.get("id") != rid]
        return others + [payload], (payload, transitioned_)

    rec_payload, transitioned = await repo.mutate_recordings(meeting_id, _fold)
    recording_id = rec_payload["id"]

    media_file = next((mf for mf in rec_payload["media_files"] if mf["type"] == media_type), {})
    if transitioned:
        log_event(
            "recording_completed", audience="user", span="recordings.upload",
            user_id=owner, meeting_id=str(meeting_id),
            fields={"recording_id": recording_id, "media_type": media_type},
        )
    return {
        "recording_id": recording_id,
        "media_file_id": media_file.get("id"),
        "storage_path": key,
        "status": rec_payload["status"],
        "chunk_seq": chunk_seq,
    }


async def upload_signal_tape(
    repo: RecordingRepo,
    storage: Storage,
    *,
    token_meeting_id: Optional[int],
    session_uid: str,
    data: bytes,
    part: str,
    media_format: str = SIGNAL_MEDIA_FORMAT,
) -> dict:
    """Store ONE captured-signal tape part for a bot session (O-TEL-1 fixture collection).

    Deliberately NOT ``upload_chunk`` with a third media_type. Three properties differ, and each of
    them is the reason:

      * **No JSONB fold.** A tape is an internal fixture, not the user's recording. Folding it into
        ``meeting.data['recordings']`` would surface a phantom recording in ``GET /recordings`` for
        every meeting the user never asked to record.
      * **No master, no chunk sequence.** The bot uploads a whole flushed file once at teardown;
        there is nothing to assemble and nothing to finalize-on-read.
      * **Its own keyspace** (``signal/…``), so the budget janitor can list every tape in the
        deployment without paging through the recordings prefix.

    Returns ``{"status": "stored", "storage_path", "bytes"}``. Raises ``SessionNotFound`` (404) when
    the session is unknown — unlike a chunk there is no "pending" case, because a tape is only ever
    uploaded at teardown, long after the session row exists — and ``InvalidSignalTape`` (422) for an
    unknown part/format, since the part name lands in an object key.
    """
    if part not in SIGNAL_TAPE_PARTS:
        raise InvalidSignalTape(
            f"unknown signal tape part {part!r}; known: {list(SIGNAL_TAPE_PARTS)}"
        )
    # Most parts are JSONL; the bot log is plain text. The expected format is a property of the
    # PART, not of the caller, so an uploader cannot choose an extension — the key would otherwise
    # be caller-shaped, and a curator downloading `botlog.jsonl` would get a text file their tools
    # try to parse a line at a time as JSON.
    expected = SIGNAL_TAPE_PART_FORMATS.get(part, SIGNAL_MEDIA_FORMAT)
    if media_format != expected:
        raise InvalidSignalTape(
            f"signal tape part {part!r} is {expected}, got {media_format!r}"
        )

    session = await repo.find_session(session_uid)
    if session is None:
        raise SessionNotFound(f"no MeetingSession for session_uid {session_uid}")
    meeting_id = session["meeting_id"]
    if token_meeting_id is not None and meeting_id != token_meeting_id:
        # Same fail-closed rule as a chunk upload: a MeetingToken minted for another meeting must
        # not be able to write into this one's prefix.
        raise SessionNotFound("MeetingToken meeting_id does not match the session's meeting")

    owner = await repo.owner_of(meeting_id)
    key = signal_tape_key(user_id=owner or 0, meeting_id=meeting_id, session_uid=session_uid,
                          part=part, media_format=media_format)
    await storage.upload(key, data, content_type=_content_type(media_format))
    log_event(
        "signal_tape_stored", audience="operator", span="recordings.signal",
        user_id=owner, meeting_id=str(meeting_id),
        fields={"session_uid": session_uid, "part": part, "bytes": len(data),
                "storage_path": key},
    )
    return {"status": "stored", "storage_path": key, "bytes": len(data),
            "meeting_id": meeting_id, "part": part}


async def _read_in_order(
    storage: Storage, keys: Sequence[str], window: int
) -> AsyncGenerator[bytes, None]:
    """``storage.get`` of every key, yielded in key order, with at most ``window`` reads in flight."""
    upcoming = iter(keys)
    reads = deque(asyncio.create_task(storage.get(k)) for k in islice(upcoming, window))
    try:
        while reads:
            data = await reads.popleft()
            reads.extend(asyncio.create_task(storage.get(k)) for k in islice(upcoming, 1))
            yield data
    finally:
        for read in reads:
            read.cancel()
        await asyncio.gather(*reads, return_exceptions=True)


async def _master_pieces(
    storage: Storage, chunk_objects: Sequence[dict], media_format: str
) -> AsyncGenerator[bytes, None]:
    """The master's bytes, chunk by chunk: memory holds the reads in flight, never the master."""
    writer = MasterWriter(media_format, [o["size"] for o in chunk_objects])
    keys = [o["key"] for o in chunk_objects]
    async for chunk in _read_in_order(storage, keys, MASTER_READ_WINDOW):
        yield writer.add(chunk)
    writer.finish()


async def finalize_master(
    repo: RecordingRepo,
    storage: Storage,
    *,
    meeting_id: int,
    recording_id: int,
    media_type: str = "audio",
) -> Optional[str]:
    """Build + upload the master for a recording media-file and stamp the JSONB. Returns the master
    storage key, or ``None`` when there is nothing to finalize.

    RE-ASSEMBLABLE, not write-once (#768). Existence is the WRONG freshness signal: a read while the
    meeting is still recording must not permanently freeze the master. The master is (re)built when
    it is absent OR when the number of chunk objects under the recording's prefix differs from the
    count the current master already represents (``assembled_chunk_count``). So a mid-recording read
    assembles a partial, and every later read that finds new chunks rebuilds — which also repairs a
    master frozen by a pre-fix stack on its next read. The freeze is impossible to reintroduce
    silently: the assembled-chunk-count is recorded and compared, and a rebuild-on-growth is logged.
    """
    recordings = await repo.get_recordings(meeting_id)
    rec = next((r for r in recordings if r.get("id") == recording_id), None)
    if rec is None:
        return None
    mf = next((m for m in rec.get("media_files", []) if m.get("type") == media_type), None)
    if mf is None:
        return None

    media_format = mf.get("format", "wav")
    master_key = master_storage_key(mf["storage_path"], media_format)

    # Gather the chunk objects under this media type (excluding any prior master).
    # S3 Prefix and the in-memory list are startswith with no delimiter, so
    # `.../audio` also matches `.../audio<anything>/`. The trailing slash keeps
    # a sibling media type out of this master.
    prefix = mf["storage_path"].rsplit("/", 1)[0] + "/"
    chunk_objects = sorted(
        (o for o in await storage.list_detailed(prefix)
         if not o["key"].rsplit("/", 1)[-1].startswith("master.")),
        key=lambda o: o["key"],
    )
    listed_count = len(chunk_objects)
    assembled_count = mf.get("assembled_chunk_count")

    # Loud guard (#769): the number of chunks we're about to assemble vs what the JSONB fold counted.
    # A mismatch means chunks were dropped from the listing (truncation) or the fold — surface it.
    jsonb_count = mf.get("chunk_count")
    if jsonb_count is not None and listed_count != jsonb_count:
        log_event(
            "recording_chunk_count_mismatch", audience="operator", span="recordings.finalize",
            meeting_id=str(meeting_id),
            fields={"recording_id": recording_id, "media_type": media_type,
                    "listed_count": listed_count, "jsonb_chunk_count": jsonb_count},
        )

    master_exists = await storage.exists(master_key)
    # Rebuild only when there ARE chunks to assemble (listed_count > 0) and either no master exists yet
    # or the chunk count changed since the master was last assembled. With zero chunk objects we never
    # rebuild — an existing master is served as-is rather than assembled from nothing.
    rebuild = listed_count > 0 and ((not master_exists) or assembled_count != listed_count)
    if rebuild:
        if master_exists and assembled_count is not None and listed_count > assembled_count:
            # A prior (partial) master is being superseded by chunks that arrived after it — the exact
            # #768 unfreeze. Log it so a re-freeze regression is noisy rather than silent.
            log_event(
                "recording_master_reassembled", audience="operator", span="recordings.finalize",
                meeting_id=str(meeting_id),
                fields={"recording_id": recording_id, "media_type": media_type,
                        "prior_assembled_count": assembled_count, "new_count": listed_count},
            )
        async with aclosing(_master_pieces(storage, chunk_objects, media_format)) as pieces:
            await storage.upload_stream(
                master_key, pieces, content_type=_content_type(media_format)
            )

    # G3 — stamp the media-file finalized ATOMICALLY (read→modify→write under one row lock), so a late
    # concurrent chunk upload can't clobber the finalized master pointer (the master bytes are already
    # uploaded above, idempotently by key). The mutator re-reads the LIVE recording.
    def _stamp(recs):
        r = next((x for x in recs if x.get("id") == recording_id), None)
        if r is None:
            return recs, None
        m = next((x for x in r.get("media_files", []) if x.get("type") == media_type), None)
        if m is None:
            return recs, None
        m["storage_path"] = master_key
        m["is_final"] = True
        m["assembled_chunk_count"] = listed_count
        m["finalized_at"] = _now_iso()
        m["finalized_by"] = "recording_finalizer.master"
        existing_pb = r.get("playback_url") or {}
        r["playback_url"] = {
            "audio": existing_pb.get("audio")
            or (f"/recordings/{recording_id}/master?type=audio" if media_type == "audio" else None),
            "video": existing_pb.get("video")
            or (f"/recordings/{recording_id}/master?type=video" if media_type == "video" else None),
        }
        others = [x for x in recs if x.get("id") != recording_id]
        return others + [r], master_key

    return await repo.mutate_recordings(meeting_id, _stamp)


def _verify_meeting_token(token: str, *, secret: Optional[str] = None) -> dict[str, Any]:
    """Verify a MeetingToken (HS256, ``ADMIN_TOKEN``-signed) and return its claims. Raises
    ``ValueError`` on a bad signature / expiry (the parent ``verify_meeting_token``)."""
    import base64
    import hmac
    import json
    import os

    secret = secret if secret is not None else os.environ.get("ADMIN_TOKEN")
    if not secret:
        raise ValueError("ADMIN_TOKEN not configured; cannot verify MeetingToken")
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except ValueError:
        raise ValueError("malformed MeetingToken")
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected = hmac.new(secret.encode(), signing_input, digestmod="sha256").digest()
    got = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
    if not hmac.compare_digest(expected, got):
        raise ValueError("MeetingToken signature mismatch")
    claims = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
    exp = claims.get("exp")
    if exp is not None and int(datetime.now(timezone.utc).timestamp()) > int(exp):
        raise ValueError("MeetingToken expired")
    return claims
