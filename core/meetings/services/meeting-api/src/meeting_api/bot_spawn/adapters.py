"""Production adapters — the real ``MeetingRepo`` (SQLAlchemy) + ``RuntimeClient`` (runtime.v1 HTTP).

Thin translations of the ports to the concrete clients, exactly as the parent's
``meetings.request_bot`` did (SQLAlchemy INSERTs for the meeting + session; an httpx POST to the
runtime kernel's ``POST /workloads``). They carry NO test logic.

Heavy imports (SQLAlchemy, httpx) are LAZY (inside the methods / ``build_production_router``) so the
package can be imported and unit-tested with the in-memory fakes without those runtime deps in the
gate venv — which is why ``pyproject.toml`` needs no ``greenlet`` pin.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from ..lifecycle.machine import dominant_completion_reason
from ..sessions import new_session
from .ports import (
    DuplicateMeeting,
    MaxBotsExceeded,
    MeetingStopped,
    QuotaExceeded,
    SpawnFailed,
    WorkloadUnknown,
    _archive_completion,
    _stopped_reopen_detail,
    reconcile_grace_for_status,
)


def _reason(resp) -> str:
    """The kernel's error reason from a non-201 runtime.v1 response — its ``{detail}`` (the sealed
    contract defines no error shape, so the API uses FastAPI's default), falling back to the raw body
    text. Lets the meeting-api 502 name WHY the spawn failed (e.g. the absent image) instead of a bare
    status code (#718)."""
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("detail"):
            return str(body["detail"])
    except Exception:  # noqa: BLE001 — a non-JSON error body falls back to text
        pass
    return (getattr(resp, "text", "") or "").strip() or f"HTTP {resp.status_code}"


def _iso_utc(dt) -> Optional[str]:
    """Serialize a datetime as an unambiguous UTC ISO-8601 string (``…Z``).

    The meeting time columns are naive but hold UTC (the DB session is UTC). Emitting a bare
    ``isoformat()`` yields a zone-less string that a browser's ``new Date()`` parses as LOCAL —
    so the value renders offset by the viewer's UTC offset. Stamping UTC makes clients localize it.
    """
    if dt is None:
        return None
    aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return aware.isoformat().replace("+00:00", "Z")


#: The unique partial index that allows one live meeting per (user, platform, native id).
LIVE_LINK_INDEX = "uq_meeting_live_user_platform_native"


def _violated_constraint(error) -> Optional[str]:
    """The constraint an ``IntegrityError`` names: asyncpg's error (the DBAPI error's cause)
    carries ``constraint_name``."""
    orig = getattr(error, "orig", None)
    for candidate in (orig, getattr(orig, "__cause__", None)):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return name
    return None


def _row_to_dict(m) -> dict:
    return {
        "id": m.id,
        "user_id": m.user_id,
        "platform": m.platform,
        "native_meeting_id": m.platform_specific_id,
        "platform_specific_id": m.platform_specific_id,
        "status": m.status,
        "bot_container_id": m.bot_container_id,
        "start_time": _iso_utc(m.start_time),
        "end_time": _iso_utc(m.end_time),
        "data": m.data if isinstance(m.data, dict) else {},
        "created_at": _iso_utc(m.created_at),
        "updated_at": _iso_utc(m.updated_at),
    }


class SqlAlchemyMeetingRepo:
    """``MeetingRepo`` over a SQLAlchemy-async ``session_factory`` (``meetings`` /
    ``meeting_sessions`` tables). Carve of the parent ``meetings.request_bot`` DB ops."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def find_active(self, user_id, platform, native_meeting_id) -> Optional[dict]:
        from sqlalchemy import select

        from ..sessions.models import Meeting

        async with self._session_factory() as db:
            stmt = (
                select(Meeting)
                .where(
                    Meeting.user_id == user_id,
                    Meeting.platform == platform,
                    Meeting.platform_specific_id == native_meeting_id,
                    Meeting.status.in_(["requested", "joining", "awaiting_admission", "active"]),
                )
                .order_by(Meeting.created_at.desc())
            )
            m = (await db.execute(stmt)).scalars().first()
            return _row_to_dict(m) if m else None

    async def find_active_rows(self, user_id, platform, native_meeting_id) -> list:
        """Every non-terminal row for (user, platform, native), newest first — the user-stop's
        eviction set. ``stopping`` is included: it is still non-terminal, and the caller
        de-duplicates on ``data.stop_requested``, not on status."""
        from sqlalchemy import select

        from ..sessions.models import Meeting

        async with self._session_factory() as db:
            stmt = (
                select(Meeting)
                .where(
                    Meeting.user_id == user_id,
                    Meeting.platform == platform,
                    Meeting.platform_specific_id == native_meeting_id,
                    Meeting.status.notin_(("completed", "failed")),
                )
                .order_by(Meeting.created_at.desc(), Meeting.id.desc())
            )
            return [_row_to_dict(m) for m in (await db.execute(stmt)).scalars().all()]

    async def find_active_by_userdata(self, userdata_s3_path) -> Optional[dict]:
        from sqlalchemy import select

        from ..sessions.models import Meeting

        async with self._session_factory() as db:
            stmt = (
                select(Meeting)
                .where(
                    Meeting.status.in_(["requested", "joining", "awaiting_admission", "active", "stopping"]),
                    Meeting.data["auth_userdata_path"].astext == userdata_s3_path,
                )
                .order_by(Meeting.created_at.desc())
            )
            m = (await db.execute(stmt)).scalars().first()
            return _row_to_dict(m) if m else None

    async def find_latest(self, user_id, platform, native_meeting_id) -> Optional[dict]:
        from sqlalchemy import select

        from ..intake.adapters import link_rows
        from ..intake.ports import Room
        from ..intake.resolver import LinkKind, resolve
        from ..sessions.models import Meeting

        async with self._session_factory() as db:
            rows = await link_rows(db, user_id, Room(platform, native_meeting_id))
            picked = resolve(rows, LinkKind.READ, now=datetime.now(timezone.utc))
            if picked is None:
                return None
            m = (
                await db.execute(select(Meeting).where(Meeting.id == picked.id))
            ).scalars().first()
            return _row_to_dict(m) if m else None

    async def get_meeting(self, meeting_id) -> Optional[dict]:
        from sqlalchemy import select

        from ..sessions.models import Meeting

        async with self._session_factory() as db:
            m = (
                await db.execute(select(Meeting).where(Meeting.id == meeting_id))
            ).scalars().first()
            return _row_to_dict(m) if m else None

    async def reopen_meeting(self, *, meeting_id, data_patch=None) -> dict:
        from sqlalchemy import select
        from sqlalchemy.orm.attributes import flag_modified

        from ..sessions.models import Meeting

        async with self._session_factory() as db:
            m = (
                await db.execute(
                    select(Meeting).where(Meeting.id == meeting_id).with_for_update()
                )
            ).scalars().first()
            data = dict(m.data) if isinstance(m.data, dict) else {}
            # Last line of defense behind the service-level 409: a row the USER stopped is never
            # reopened in place, by any caller. Under the row lock, so a stop committing concurrently
            # is either visible here or lands on a row this txn has already moved.
            if data.get("stop_requested"):
                raise MeetingStopped(_stopped_reopen_detail(meeting_id))
            # The same last line of defense for deletion: a row whose artifacts are erased, or being
            # erased, is not reusable. Under the same lock, so a deletion committing concurrently is
            # either visible here or lands on a row this txn has already moved.
            deletion_pending = data.get("artifact_deletion") or any(
                r.get("deletion_pending") for r in (data.get("recordings") or [])
                if isinstance(r, dict)
            )
            if m.status not in ("completed", "failed") or deletion_pending:
                raise DuplicateMeeting("Terminal meeting is no longer reusable")
            m.status = "requested"
            m.end_time = None
            m.bot_container_id = None
            _archive_completion(data)
            for key, value in (data_patch or {}).items():
                if value is None:
                    data.pop(key, None)
                else:
                    data[key] = value
            m.data = data
            flag_modified(m, "data")
            # updated_at is set server-side by the column's onupdate=func.now() (main's pattern);
            # never write a tz-aware Python datetime into the naive column (asyncpg DataError).
            await db.commit()
            await db.refresh(m)
            return _row_to_dict(m)

    async def get_status_by_session(self, *, session_uid) -> Optional[str]:
        from sqlalchemy import select

        from ..sessions.models import Meeting, MeetingSession

        async with self._session_factory() as db:
            sess = (
                await db.execute(select(MeetingSession).where(MeetingSession.session_uid == session_uid))
            ).scalars().first()
            if sess is None:
                return None
            status = (
                await db.execute(select(Meeting.status).where(Meeting.id == sess.meeting_id))
            ).scalars().first()
            return status

    async def get_lifecycle_state_by_session(self, *, session_uid) -> Optional[dict]:
        from sqlalchemy import select

        from ..sessions.models import Meeting, MeetingSession

        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(Meeting.status, Meeting.data)
                    .join(MeetingSession, MeetingSession.meeting_id == Meeting.id)
                    .where(MeetingSession.session_uid == session_uid)
                )
            ).first()
            if row is None:
                return None
            return {
                "status": row.status,
                "data": dict(row.data) if isinstance(row.data, dict) else {},
            }

    async def find_by_container(self, *, bot_container_id) -> Optional[dict]:
        """The meeting + latest session for a workload id — used by the runtime callback (CC5) to drive a
        synthetic ``failed`` for a workload that died before the bot reported. ``{meeting_id, status,
        session_uid, stop_requested}`` or ``None``.

        ``stop_requested`` carries the user's intent so the synthetic terminal can tell a bot the USER
        abandoned from one that timed out on its own — the two earn different completion reasons, and
        only the latter may be retried."""
        from sqlalchemy import select

        from ..sessions.models import Meeting, MeetingSession

        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(Meeting.id, Meeting.status, Meeting.data).where(
                        Meeting.bot_container_id == bot_container_id
                    )
                )
            ).first()
            if row is None:
                return None
            mid, status, data = row
            sid = (
                await db.execute(
                    select(MeetingSession.session_uid)
                    .where(MeetingSession.meeting_id == mid)
                    .order_by(MeetingSession.id.desc())
                )
            ).scalars().first()
            return {
                "meeting_id": mid,
                "status": status,
                "session_uid": sid,
                "stop_requested": bool((data or {}).get("stop_requested")),
            }

    async def update_meeting_status(
        self, *, session_uid, status, completion_reason=None, failure_stage=None, data=None
    ) -> None:
        from sqlalchemy import select
        from sqlalchemy.orm.attributes import flag_modified

        from ..sessions.models import Meeting, MeetingSession

        async with self._session_factory() as db:
            sess = (
                await db.execute(select(MeetingSession).where(MeetingSession.session_uid == session_uid))
            ).scalars().first()
            if sess is None:
                return  # unknown session (e.g. a self-host bot) — nothing to persist
            m = (
                await db.execute(select(Meeting).where(Meeting.id == sess.meeting_id).with_for_update())
                # FOR UPDATE: db-writer/recordings/docs all lock before read-modify-write of data
                # JSONB; without it a concurrent db-writer merge commit is clobbered (#53 review).
            ).scalars().first()
            if m is None:
                return
            m.status = status
            merged = dict(m.data) if isinstance(m.data, dict) else {}
            if completion_reason is not None:
                merged["completion_reason"] = completion_reason
            if failure_stage is not None:
                merged["failure_stage"] = failure_stage
            for k, v in (data or {}).items():
                merged[k] = v
            if status in ("completed", "failed"):
                # Delivery marker (#807): `completed` alone means "the bot exited cleanly" — it says
                # nothing about whether a transcript exists. Persisting the segment count at the
                # terminal transition makes completed-but-empty meetings (roughly half of hosted
                # completions) queryable and alertable instead of indistinguishable from successes.
                from sqlalchemy import func as _func

                from ..sessions.models import Transcription

                merged["segments_captured"] = (
                    await db.execute(
                        select(_func.count()).select_from(Transcription).where(Transcription.meeting_id == m.id)
                    )
                ).scalar() or 0
            m.data = merged
            flag_modified(m, "data")
            # Naive UTC into the naive time columns (tz-aware → asyncpg DataError, per set_bot_container).
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if status == "active" and m.start_time is None:
                m.start_time = now
            if status in ("completed", "failed") and m.end_time is None:
                m.end_time = now
            await db.commit()
            # Refresh BEFORE _row_to_dict: `updated_at` has a server-side onupdate, so it is expired
            # post-commit; reading it in _row_to_dict would trigger implicit async IO (MissingGreenlet).
            # The other write adapters (create_meeting/set_bot_container/reopen) follow the same pattern.
            await db.refresh(m)
            # Return the updated row so the lifecycle callback can deliver the per-user webhook from
            # meeting.data (and the stop route gets a clean dict) without a second query.
            return _row_to_dict(m)

    async def count_active_bots(self, *, user_id, exclude_meeting_id=None) -> int:
        from sqlalchemy import func, select

        from ..sessions.models import Meeting

        async with self._session_factory() as db:
            stmt = (
                select(func.count())
                .select_from(Meeting)
                .where(
                    Meeting.user_id == user_id,
                    Meeting.status.in_(["requested", "joining", "awaiting_admission", "active"]),
                    Meeting.platform != "browser_session",  # infra excluded (parent meetings.py:1091)
                )
            )
            if exclude_meeting_id is not None:
                stmt = stmt.where(Meeting.id != exclude_meeting_id)
            return int((await db.execute(stmt)).scalar() or 0)

    async def list_stale_stopping(
        self, *, older_than_seconds: float
    ) -> list[tuple[int, str, Optional[str]]]:
        """Meetings stuck in ``stopping`` longer than ``older_than_seconds`` — with their latest
        session_uid AND ``bot_container_id``. The stop-reconcile backstop completes these (the bot was
        told to leave but never sent its own terminal callback) AND kills the workload (CC6), since an
        ACTIVE bot that missed the fire-and-forget leave is an orphan until torn down. Returns
        ``[(meeting_id, session_uid, bot_container_id), …]`` (bot_container_id may be ``None``)."""
        from datetime import datetime, timezone

        from sqlalchemy import select

        from ..sessions.models import Meeting, MeetingSession

        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    select(Meeting.id, Meeting.updated_at, MeetingSession.session_uid,
                           Meeting.bot_container_id)
                    .join(MeetingSession, MeetingSession.meeting_id == Meeting.id)
                    .where(Meeting.status == "stopping")
                    .order_by(MeetingSession.id.desc())
                )
            ).all()
        now = datetime.now(timezone.utc)
        out: dict[int, tuple[str, Optional[str]]] = {}
        for mid, upd, sid, bcid in rows:
            if mid in out or upd is None or not sid:
                continue
            u = upd if upd.tzinfo else upd.replace(tzinfo=timezone.utc)
            if (now - u).total_seconds() >= older_than_seconds:
                out[mid] = (sid, bcid)
        return [(mid, sid, bcid) for mid, (sid, bcid) in out.items()]

    async def list_stale_nonterminal(
        self, *, stop_grace: float, active_grace: float, preactive_grace: Optional[float] = None
    ) -> list[tuple[int, str, str, Optional[str], bool]]:
        """Meetings stuck in ANY non-terminal status whose row has gone quiet past its grace window —
        a bot that exited (or vanished) without ever sending its terminal lifecycle callback leaves the
        row hung here forever. ``updated_at`` is bumped on every status change AND on segment/heartbeat
        persistence. NOTE: for a LIVE status (`active`/`needs_help`) ``updated_at`` staleness is a
        CANDIDATE signal only — the sweep additionally gates the active-reap on runtime workload
        liveness (see ``reconcile.py``), because a silent-but-live bot stops bumping ``updated_at``.

        Per-row window: ``stopping`` uses ``stop_grace`` (a stop was requested — clear it fast), a
        PRE-ACTIVE row (`requested`/`joining`/`awaiting_admission` — the bot has not reached the
        meeting yet, and holds the lobby budget the control plane handed it) uses ``preactive_grace``,
        everything else ``active_grace`` (a longer idle so a momentarily-quiet live bot is not
        reaped). Returns ``[(meeting_id, status, session_uid, bot_container_id, stop_requested), …]`` with
        the LATEST session_uid per meeting (mirrors ``list_stale_stopping``)."""
        from datetime import datetime, timezone

        from sqlalchemy import select

        from ..sessions.models import Meeting, MeetingSession

        non_terminal = [
            "requested", "joining", "awaiting_admission", "needs_help", "active", "stopping",
        ]
        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    select(Meeting.id, Meeting.status, Meeting.updated_at,
                           MeetingSession.session_uid, Meeting.bot_container_id, Meeting.data)
                    .join(MeetingSession, MeetingSession.meeting_id == Meeting.id)
                    .where(Meeting.status.in_(non_terminal))
                    .order_by(MeetingSession.id.desc())
                )
            ).all()
        now = datetime.now(timezone.utc)
        out: dict[int, tuple[str, str, Optional[str], bool]] = {}
        for mid, status, upd, sid, bcid, data in rows:
            if mid in out or upd is None or not sid:
                continue
            u = upd if upd.tzinfo else upd.replace(tzinfo=timezone.utc)
            grace = reconcile_grace_for_status(status, stop_grace, active_grace, preactive_grace)
            if (now - u).total_seconds() >= grace:
                stop_req = bool(isinstance(data, dict) and data.get("stop_requested"))
                out[mid] = (status, sid, bcid, stop_req)
        return [(mid, st, sid, bcid, sr) for mid, (st, sid, bcid, sr) in out.items()]

    async def create_meeting(self, *, user_id, platform, native_meeting_id, data) -> dict:
        from ..sessions.models import Meeting

        async with self._session_factory() as db:
            m = Meeting(
                user_id=user_id, platform=platform, platform_specific_id=native_meeting_id,
                status="requested", data=dict(data or {}),
            )
            db.add(m)
            await db.commit()
            await db.refresh(m)
            return _row_to_dict(m)

    async def create_meeting_guarded(
        self, *, user_id, platform, native_meeting_id, data, max_concurrent=None,
        exclude_meeting_id=None, claim_meeting_id=None,
    ) -> dict:
        """ATOMIC dedup + cap + claim-or-insert in ONE transaction (ROB1/ROB2, §1.5).

        The TOCTOU-safe spawn primitive. Its locks, in the one lock order (§1.4):

          * the link's advisory lock (``intake.adapters.take_link_lock`` — the key intake's writes
            take), so a claim and an intake write on the same link queue instead of interleaving;
          * the per-user ``pg_advisory_xact_lock(:user_id)``, so concurrent spawns for the SAME user
            SERIALIZE through this txn and the dedup query + cap COUNT + write see a stable
            snapshot (both locks auto-release at commit/rollback);
          * the claimed meeting row (``FOR UPDATE``), then its ``meeting_aw_state`` row inside the
            status writer.

        Nothing takes the user lock and then a link lock, and intake takes every link lock before
        any row, so the order cannot cycle. The unique partial index on live rows
        (``uq_meeting_live_user_platform_native`` — see sessions/models.py) is the DB-level
        backstop: a duplicate live row that slipped past the advisory locks (e.g. a different
        meeting-api process) raises ``IntegrityError`` at commit → mapped to ``DuplicateMeeting``.
        """
        from sqlalchemy import bindparam, func, select, text
        from sqlalchemy.exc import IntegrityError

        from ..intake.adapters import take_link_lock
        from ..intake.ports import Room
        from ..sessions.models import Meeting, MeetingAwState, MeetingEntry
        from .auto_join import LIVE_STATUSES
        from .ports import ClaimTargetMoved, PlannedRow, planned_claim

        # 0. depleted — a cap <= 0 means NO bots allowed (0 is "depleted", never "unlimited");
        #    reject before touching the DB. Only ``None`` (no cap provided) skips the gate.
        if max_concurrent is not None and max_concurrent <= 0:
            raise MaxBotsExceeded(user_id, max_concurrent)

        active = ["requested", "joining", "awaiting_admission", "active"]
        async with self._session_factory() as db:
            await take_link_lock(db, user_id, Room(platform, native_meeting_id))
            # Per-user serialization: hold the advisory lock for the whole transaction. asyncpg needs a
            # bound int param (not a literal-format string), so bind it explicitly.
            await db.execute(
                text("SELECT pg_advisory_xact_lock(:uid)").bindparams(bindparam("uid", user_id))
            )
            # 0b. the exact row — locked and re-read under the link lock, so the link it is on now
            #     is the link this spawn was built for and locked.
            target = None
            if claim_meeting_id is not None:
                from ..intake.status import lock_meeting

                target = await lock_meeting(db, claim_meeting_id)
                if target is None or target.user_id != user_id:
                    raise LookupError(f"meeting {claim_meeting_id} not found for user {user_id}")
                if (target.platform, target.platform_specific_id) != (platform, native_meeting_id):
                    raise ClaimTargetMoved(claim_meeting_id)
            # 1. dedup — under the locks, a LIVE row for (user, platform, native) blocks the spawn:
            #    whatever its status, a bot already owns the room.
            dup = (
                await db.execute(
                    select(Meeting.id).where(
                        Meeting.user_id == user_id,
                        Meeting.platform == platform,
                        Meeting.platform_specific_id == native_meeting_id,
                        Meeting.status.in_(LIVE_STATUSES),
                    )
                )
            ).scalars().first()
            if dup is not None:
                raise DuplicateMeeting(
                    f"An active meeting already exists for {platform}/{native_meeting_id}"
                )
            # 2. cap — count the user's active bots (browser_session excluded); reject the N+1th.
            #    (cap <= 0 was already rejected as depleted above.)
            if max_concurrent is not None:
                count_stmt = (
                    select(func.count())
                    .select_from(Meeting)
                    .where(
                        Meeting.user_id == user_id,
                        Meeting.status.in_(active),
                        Meeting.platform != "browser_session",
                    )
                )
                if exclude_meeting_id is not None:
                    count_stmt = count_stmt.where(Meeting.id != exclude_meeting_id)
                n_active = int((await db.execute(count_stmt)).scalar() or 0)
                if n_active >= max_concurrent:
                    raise MaxBotsExceeded(user_id, max_concurrent, active=n_active)
            if target is not None:
                return await self._claim_exact(db, target, data)
            # 2b. claim — the PLANNED row (intent status `idle`/`scheduled`, created by POST /meetings,
            #     calendar sync or intake) `planned_claim` picks is UPGRADED in place, so the plan, its
            #     workspace bind and the transcript live on ONE row: an entry-managed row by the R1
            #     join_now rule, else the newest entry-less row (upstream's rule). A future occurrence
            #     is never claimed: the spawn inserts a new row instead. Spawn keys merge OVER the
            #     planned data; the plan's `title` / `scheduled_at` / `workspace_id` / `auto_join` /
            #     `calendar_uid` survive.
            from sqlalchemy import exists
            from sqlalchemy.orm.attributes import flag_modified

            managed = exists().where(MeetingEntry.meeting_id == Meeting.id)
            planned = (await db.execute(
                select(Meeting, MeetingAwState.scheduled_end_at, managed.label("managed"))
                .outerjoin(MeetingAwState, MeetingAwState.meeting_id == Meeting.id)
                .where(
                    Meeting.user_id == user_id,
                    Meeting.platform == platform,
                    Meeting.platform_specific_id == native_meeting_id,
                    Meeting.status.in_(("idle", "scheduled")),
                ).with_for_update(of=Meeting)
            )).all()
            picked = planned_claim(
                [PlannedRow.of(m.id, m.status, m.data, m.start_time, m.created_at, end, entries)
                 for m, end, entries in planned],
                now=datetime.now(timezone.utc),
            )
            claimable = next((m for m, _, _ in planned if m.id == picked), None)
            if claimable is not None:
                planned_data = dict(claimable.data) if isinstance(claimable.data, dict) else {}
                # A THIS-REQUEST dispatch supersedes an earlier stop ON THE PLAN. Legacy zombie rows
                # exist (a scheduled row a rev-193 DELETE flagged but never terminalized), and
                # claiming one with the flag still on it would make the spawn fence abort the very
                # bot the user just asked for. A stop never touches a planned row (§1.6: it stops the
                # live meeting only), so the flag on a plan only ever comes from an older build.
                planned_data.pop("stop_requested", None)
                claimable.status = "requested"
                claimable.end_time = None
                claimable.bot_container_id = None
                claimable.data = {**planned_data, **dict(data or {})}
                flag_modified(claimable, "data")
                await db.commit()
                await db.refresh(claimable)
                return _row_to_dict(claimable)
            # 3. insert — still inside the same txn/lock, so check+insert is atomic.
            m = Meeting(
                user_id=user_id, platform=platform, platform_specific_id=native_meeting_id,
                status="requested", data=dict(data or {}),
            )
            db.add(m)
            try:
                await db.commit()
            except IntegrityError as e:
                # The unique partial index backstop fired — a concurrent duplicate active row won the
                # race (e.g. a spawn in another process the advisory lock didn't cover). Treat as dedup.
                await db.rollback()
                raise DuplicateMeeting(
                    f"An active meeting already exists for {platform}/{native_meeting_id}"
                ) from e
            await db.refresh(m)
            return _row_to_dict(m)

    async def _claim_exact(self, db, target, data) -> dict:
        """§1.5: the exact row ``scheduled`` → ``requested`` through the one status writer
        (conditional, so a row that is no longer scheduled is never claimed), the spawn keys merged
        over its data and the send time stamped as ``data.auto_join_last_attempt`` (Ruling R7:
        ``bot_joins_at`` reads it). Commits the caller's transaction."""
        from sqlalchemy.exc import IntegrityError

        from ..intake.status import StatusConflict, write_status

        # The row is locked and the live-row dedup has passed, so a status other than `scheduled`
        # here is a finished or intent-only row, never a live one.
        meeting_id, room = target.id, f"{target.platform}/{target.platform_specific_id}"
        patch = {**dict(data or {}),
                 "auto_join_last_attempt": datetime.now(timezone.utc).isoformat()}
        try:
            await write_status(db, meeting_id, "requested", expected_from={"scheduled"},
                               data_patch=patch)
            await db.commit()
        except StatusConflict as e:
            raise MeetingStopped(
                f"meeting {meeting_id} is {e.actual}; only a scheduled meeting can be sent a bot"
            ) from e
        except IntegrityError as e:
            # The live-row index backstop: a writer outside these locks made another row live. Any
            # other constraint is a fault, not a duplicate, and propagates.
            if _violated_constraint(e) != LIVE_LINK_INDEX:
                raise
            await db.rollback()
            raise DuplicateMeeting(f"An active meeting already exists for {room}") from e
        await db.refresh(target)
        return _row_to_dict(target)

    async def list_due_meetings(self, now, lead_s) -> list[dict]:
        """The ``scheduled`` rows with a joinable link whose meeting time
        (``meeting_event_time``) is at or before ``now + lead_s`` — the auto-join sweep's
        candidates (§1.5), read through the partial index ``ix_meeting_scheduled_due``, so a
        future row is never read. ``status = 'scheduled'`` is a literal: a bound value would let a
        cached generic plan miss the index's predicate. Each row carries its
        ``meeting_aw_state.scheduled_end_at`` / ``waiting_for_room_sent_at`` and ``has_entries``
        (a ``meeting_entries`` row points at it); the toggle/window/backoff filtering is the
        sweep's pure ``due_rows``."""
        from datetime import timedelta

        from sqlalchemy import exists, func, select, text

        from ..sessions.models import Meeting, MeetingAwState, MeetingEntry

        event_time = func.meeting_event_time(Meeting.data, Meeting.start_time, Meeting.created_at)
        due_by = (now + timedelta(seconds=lead_s)).astimezone(timezone.utc).replace(tzinfo=None)
        async with self._session_factory() as db:
            rows = (await db.execute(
                select(
                    Meeting,
                    MeetingAwState.scheduled_end_at,
                    MeetingAwState.waiting_for_room_sent_at,
                    exists().where(MeetingEntry.meeting_id == Meeting.id).label("has_entries"),
                )
                .outerjoin(MeetingAwState, MeetingAwState.meeting_id == Meeting.id)
                .where(
                    text("meetings.status = 'scheduled'"),
                    event_time <= due_by,
                    Meeting.platform_specific_id.isnot(None),
                    Meeting.platform != "unknown",
                )
                .order_by(event_time, Meeting.id)
            )).all()
            return [
                {**_row_to_dict(m), "scheduled_end_at": _iso_utc(end),
                 "waiting_for_room_sent_at": _iso_utc(waiting), "has_entries": bool(managed)}
                for m, end, waiting, managed in rows
            ]

    async def list_live_meetings(self) -> list[dict]:
        """Every row a bot currently OWNS (``auto_join.LIVE_STATUSES``) with a joinable link — the
        auto-join sweep's duplicate-dispatch guard reads it to answer "is someone already in this
        room?" for a due row that a sibling row (manual send, un-adopted calendar import) covers."""
        from sqlalchemy import select

        from ..sessions.models import Meeting
        from .auto_join import LIVE_STATUSES

        async with self._session_factory() as db:
            rows = (await db.execute(
                select(Meeting).where(
                    Meeting.status.in_(LIVE_STATUSES),
                    Meeting.platform_specific_id.isnot(None),
                    Meeting.platform != "unknown",
                )
            )).scalars().all()
            return [_row_to_dict(m) for m in rows]

    async def merge_meeting_data(self, meeting_id, patch: dict) -> None:
        """Merge ``patch`` into ``meeting.data`` (a ``None`` value REMOVES the key) — the sweep's
        error/backoff stamping primitive. Row-locked; a missing row is a no-op."""
        from sqlalchemy import select
        from sqlalchemy.orm.attributes import flag_modified

        from ..sessions.models import Meeting

        async with self._session_factory() as db:
            meeting = (await db.execute(
                select(Meeting).where(Meeting.id == meeting_id).with_for_update()
            )).scalars().first()
            if meeting is None:
                return
            data = dict(meeting.data) if isinstance(meeting.data, dict) else {}
            for k, v in patch.items():
                if v is None:
                    data.pop(k, None)
                else:
                    data[k] = v
            meeting.data = data
            flag_modified(meeting, "data")
            await db.commit()

    async def list_service_authority_sessions(self) -> list[dict]:
        """Active admitted sessions carrying the frozen generic authority identity."""
        from sqlalchemy import select

        from ..sessions.models import Meeting

        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    select(Meeting).where(
                        Meeting.status.in_(("active", "needs_help")),
                    )
                )
            ).scalars().all()
            return [
                _row_to_dict(row)
                for row in rows
                if isinstance(row.data, dict)
                and isinstance(row.data.get("service_authority"), dict)
                and row.data["service_authority"].get("mode")
                in ("enforce", "observe")
            ]

    async def record_service_authority_decision(
        self,
        *,
        meeting_id,
        request,
        decision,
    ) -> bool:
        """Persist one request-bound boundary decision under a row lock."""
        from sqlalchemy import select
        from sqlalchemy.orm.attributes import flag_modified

        from ..sessions.models import Meeting

        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(Meeting)
                    .where(Meeting.id == meeting_id)
                    .with_for_update()
                )
            ).scalars().first()
            if row is None:
                return False
            data = dict(row.data) if isinstance(row.data, dict) else {}
            metadata = data.get("service_authority")
            if (
                not isinstance(metadata, dict)
                or metadata.get("service_identity")
                != request.service_identity
            ):
                return False
            metadata = dict(metadata)
            boundary = request.boundary_at.isoformat()
            prior_boundary = metadata.get("last_boundary_at")
            if prior_boundary == boundary:
                if metadata.get("last_decision_id") != decision.decision_id:
                    raise ValueError(
                        "service-authority boundary decision conflicts"
                    )
                return False
            if prior_boundary:
                prior = datetime.fromisoformat(
                    prior_boundary.replace("Z", "+00:00")
                )
                if prior >= request.boundary_at:
                    return False
            metadata.update(decision.to_record())
            metadata["last_boundary_at"] = boundary
            metadata["last_decision_id"] = decision.decision_id
            if (
                decision.enforced
                and not decision.allow
                and decision.stop_scope == "billable_service"
            ):
                metadata["teardown_confirmed"] = False
                data["stop_requested"] = True
                row.status = "stopping"
            data["service_authority"] = metadata
            row.data = data
            flag_modified(row, "data")
            await db.commit()
            return True

    async def list_service_authority_teardowns(self) -> list[dict]:
        """Durable stop intents that still lack a confirmed runtime teardown."""
        from sqlalchemy import select

        from ..sessions.models import Meeting

        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    select(Meeting).where(
                        Meeting.status.in_((
                            "active",
                            "needs_help",
                            "stopping",
                        )),
                    )
                )
            ).scalars().all()
            out = []
            for row in rows:
                data = row.data if isinstance(row.data, dict) else {}
                metadata = data.get("service_authority")
                if (
                    isinstance(metadata, dict)
                    and metadata.get("enforced") is True
                    and metadata.get("allow") is False
                    and metadata.get("stop_scope")
                    == "billable_service"
                    and metadata.get("teardown_confirmed") is not True
                ):
                    out.append({
                        "id": row.id,
                        "bot_container_id": row.bot_container_id,
                        "decision_id": metadata.get("decision_id"),
                    })
            return out

    async def claim_service_authority_teardown(
        self,
        *,
        meeting_id,
        claim_id,
        claimed_at,
        lease_seconds,
    ) -> Optional[dict]:
        """Lease one stop intent under a row lock so replicas cannot race it."""
        from datetime import timezone

        from sqlalchemy import select
        from sqlalchemy.orm.attributes import flag_modified

        from ..sessions.models import Meeting

        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(Meeting)
                    .where(Meeting.id == meeting_id)
                    .with_for_update()
                )
            ).scalars().first()
            if row is None:
                return None
            data = dict(row.data) if isinstance(row.data, dict) else {}
            metadata = data.get("service_authority")
            if (
                not isinstance(metadata, dict)
                or metadata.get("enforced") is not True
                or metadata.get("allow") is not False
                or metadata.get("stop_scope") != "billable_service"
                or metadata.get("teardown_confirmed") is True
            ):
                return None
            metadata = dict(metadata)
            prior_claim = metadata.get("teardown_claim_id")
            prior_at = metadata.get("teardown_claimed_at")
            if prior_claim and prior_at:
                try:
                    prior_time = datetime.fromisoformat(
                        prior_at.replace("Z", "+00:00"),
                    ).astimezone(timezone.utc)
                except (TypeError, ValueError):
                    return None
                if (
                    claimed_at.astimezone(timezone.utc) - prior_time
                ).total_seconds() < lease_seconds:
                    return None
            metadata["teardown_claim_id"] = claim_id
            metadata["teardown_claimed_at"] = claimed_at.isoformat()
            data["service_authority"] = metadata
            row.data = data
            flag_modified(row, "data")
            await db.commit()
            return {
                "id": row.id,
                "bot_container_id": row.bot_container_id,
                "decision_id": metadata.get("decision_id"),
                "claim_id": claim_id,
            }

    async def confirm_service_authority_teardown(
        self,
        *,
        meeting_id,
        decision_id,
        claim_id,
    ) -> bool:
        from sqlalchemy import select
        from sqlalchemy.orm.attributes import flag_modified

        from ..sessions.models import Meeting

        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(Meeting)
                    .where(Meeting.id == meeting_id)
                    .with_for_update()
                )
            ).scalars().first()
            if row is None:
                return False
            data = dict(row.data) if isinstance(row.data, dict) else {}
            metadata = data.get("service_authority")
            if (
                not isinstance(metadata, dict)
                or metadata.get("decision_id") != decision_id
                or metadata.get("teardown_claim_id") != claim_id
                or metadata.get("teardown_confirmed") is True
            ):
                return False
            metadata = dict(metadata)
            metadata["teardown_confirmed"] = True
            metadata["teardown_claim_id"] = None
            metadata["teardown_claimed_at"] = None
            data["service_authority"] = metadata
            row.data = data
            flag_modified(row, "data")
            await db.commit()
            return True

    async def create_session(self, *, meeting_id, session_uid) -> None:
        async with self._session_factory() as db:
            db.add(new_session(meeting_id, session_uid))
            await db.commit()

    async def list_sessions(self, *, meeting_id) -> list:
        from sqlalchemy import select

        from ..sessions.models import MeetingSession

        async with self._session_factory() as db:
            stmt = (
                select(MeetingSession.session_uid)
                .where(MeetingSession.meeting_id == meeting_id)
                .order_by(MeetingSession.session_start_time.asc(), MeetingSession.id.asc())
            )
            return [r for (r,) in (await db.execute(stmt)).all()]

    async def set_bot_container(self, *, meeting_id, bot_container_id) -> dict:
        from sqlalchemy import select

        from ..sessions.models import Meeting

        async with self._session_factory() as db:
            m = (
                await db.execute(select(Meeting).where(Meeting.id == meeting_id))
            ).scalars().first()
            m.bot_container_id = bot_container_id
            # updated_at is set server-side by the column's onupdate=func.now() (main's pattern);
            # never write a tz-aware Python datetime into the naive column (asyncpg DataError).
            await db.commit()
            await db.refresh(m)
            return _row_to_dict(m)

    async def fail_meeting(
        self, *, meeting_id, reason, failure_stage="requested",
        completion_reason="start_failed", data=None,
    ) -> Optional[dict]:
        """Mark a meeting ``failed`` BY ID (no session needed) — the spawn-time failure path (#718).

        A workload dead on arrival (kernel ``start_failed``) is refused BEFORE the ``MeetingSession``
        exists, so the session-keyed ``update_meeting_status`` cannot reach the row; this fails it
        directly, stamping the reason into ``data`` so ``GET /meetings`` and the terminal show WHY
        instead of leaving a ``requested`` row for the 5-minute reaper to flip reason-less. Row-locked;
        a missing row is a no-op."""
        from sqlalchemy import select
        from sqlalchemy.orm.attributes import flag_modified

        from ..sessions.models import Meeting

        async with self._session_factory() as db:
            m = (
                await db.execute(select(Meeting).where(Meeting.id == meeting_id).with_for_update())
            ).scalars().first()
            if m is None:
                return None
            m.status = "failed"
            merged = dict(m.data) if isinstance(m.data, dict) else {}
            merged.update(dict(data or {}))
            # A PLANNED row cancelled before any bot existed has NO stage — inventing one would
            # claim a spawn that never happened.
            if failure_stage is not None:
                merged["failure_stage"] = failure_stage
            merged["failure_reason"] = reason
            merged["completion_reason"] = dominant_completion_reason(
                completion_reason, stop_requested=bool(merged.get("stop_requested"))
            )
            m.data = merged
            flag_modified(m, "data")
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if m.end_time is None:
                m.end_time = now
            await db.commit()
            await db.refresh(m)
            return _row_to_dict(m)


class HttpRuntimeClient:
    """``RuntimeClient`` over the runtime.v1 HTTP kernel (``POST /workloads``). 429 → QuotaExceeded;
    non-201 → SpawnFailed (parent ``_spawn_via_runtime_api``)."""

    def __init__(self, client, runtime_api_url: str):
        self._client = client
        self._url = runtime_api_url.rstrip("/")

    async def create_workload(self, spec: dict) -> dict:
        resp = await self._client.post(f"{self._url}/workloads", json=spec, timeout=30.0)
        if resp.status_code == 429:
            raise QuotaExceeded("runtime kernel: owner quota exceeded")
        if resp.status_code != 201:
            # Carry the kernel's own reason (its {detail}) so the 502 the user sees NAMES the cause
            # — e.g. "No such image: …" for an absent bot image (#718 C1 → C2).
            raise SpawnFailed(f"runtime kernel returned {resp.status_code}: {_reason(resp)}")
        body = resp.json()
        # Belt-and-suspenders (#718 C2): even a 201 must be a workload that actually STARTED. A kernel
        # that answers 201 with a dead body (state=stopped/destroyed, e.g. start_failed) is dead on
        # arrival — refuse it here too, so the adapter never trusts any kernel version's optimism.
        state = body.get("state")
        if state in ("stopped", "destroyed"):
            raise SpawnFailed(
                f"workload dead on spawn: {body.get('stopReason') or state}"
            )
        return body

    async def delete_workload(self, workload_id: str) -> None:
        """Tear down a workload (``DELETE /workloads/{id}``) — teardown must be CONFIRMED.

        A 2xx means the kernel destroyed the workload (with kernel re-adoption that reaches the
        real container even across a runtime restart). A 404 raises ``WorkloadUnknown``: the kernel
        does not know the workload, so termination is UNCONFIRMED — a container may still be live
        (the orphaned-live-bot incident treated exactly this 404 as success). Any other error
        raises ``SpawnFailed``. Callers log loud and retry/backstop; they must never report a stop
        as done on these."""
        resp = await self._client.delete(
            f"{self._url}/workloads/{workload_id}",
            timeout=60.0,  # the kernel's graceful teardown can hold the request for its stop grace
        )
        if resp.status_code == 404:
            raise WorkloadUnknown(workload_id)
        if resp.status_code >= 400:
            raise SpawnFailed(f"runtime kernel delete_workload returned {resp.status_code}")

    async def get_workload(self, workload_id: str) -> Optional[dict]:
        """Liveness probe (``GET /workloads/{id}``). 404 → the kernel does not TRACK the workload →
        ``None`` — which is NOT evidence the bot is gone (a recreated runtime forgets live bots);
        the reconcile sweep treats it as 'untracked: fail loud, do not reap'. Any other non-200
        raises (caller treats it as 'unknown, do not reap' — fail safe toward NOT killing a
        possibly-live meeting)."""
        resp = await self._client.get(f"{self._url}/workloads/{workload_id}", timeout=10.0)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise SpawnFailed(f"runtime kernel get_workload returned {resp.status_code}")
        return resp.json()


def build_production_router(*, database_url: Optional[str] = None, runtime_api_url: Optional[str] = None):
    """Construct the bot-spawn router with real SQLAlchemy + httpx runtime adapters from env."""
    import httpx
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from ..db import build_engine
    from .router import build_router

    database_url = database_url or os.getenv(
        "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@postgres:5432/vexa"
    )
    runtime_api_url = runtime_api_url or os.getenv("RUNTIME_API_URL", "http://runtime:8090")

    engine = build_engine(database_url)  # #635: env-steered pool
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    http = httpx.AsyncClient(timeout=30.0)
    return build_router(SqlAlchemyMeetingRepo(session_factory), HttpRuntimeClient(http, runtime_api_url))
