"""Auto-join sweep — "scheduled" MEANS the bot joins.

One tick reads only the PLANNED rows that are due (§1.5): ``list_due_meetings(now, lead_s)`` asks
the database for the ``scheduled`` rows whose meeting time is within ``lead_s`` or already past,
through the partial index ``ix_meeting_scheduled_due``. An entry-less row is due inside
[start - ``lead_s``, start + ``grace_s``] (never join hours late); a meeting entries manage is due
from start - ``lead_s`` until its ``scheduled_end_at`` (R6: a late bot is better than none). Unless
the per-meeting ``data.auto_join`` toggle is off, the tick spawns the bot on that exact row through
``intake.ExactRowSpawn`` — the SAME ``request_bot`` flow POST /bots runs, claiming the row by its id
under the link lock, where the due window (``DueWindow``) is checked again — so the sweep is
idempotent by construction and never sends a row a writer moved after the read: a claimed row leaves
``scheduled`` and drops out of the read, and a concurrent manual "Send bot now" surfaces here as
``already_live`` (someone already joined), counted, never error-stamped.

A meeting entries manage has its link checked again under the link lock first
(``intake.sweeps.check_room``, R2). A link held by an open-ended ``join_now`` meeting whose bot is
staying takes the due meeting's entries (``IntakeService.merge_into_live``); a link held by any
other bot, a leaving one included, makes the meeting wait: ``meeting.waiting_for_room`` goes out once, no retry pause is stamped, and the bot goes
on the first tick after the link is free. A real spawn failure stores its typed code
(``meeting_aw_state.last_error_code`` / ``last_error_message``) and backs off like any other row;
the not-sent sweep (``intake.sweeps``) ends the meeting at its end if no bot ever went.

Defense in depth behind that dedup for an entry-less row: before spawning, the tick asks the repo
which (user, platform, native) tuples a bot ALREADY owns (``list_live_meetings`` over
``LIVE_STATUSES``) and refuses a due row whose room is already covered by a DIFFERENT row — stamping
``data.auto_join_error`` with the holding meeting id. Two Vexa bots in one meeting is never
correct however the two rows came to exist (live 2026-08-17: a manual "Send bot now" row plus a
calendar import of the same Meet that failed to adopt it).

Failures are LOUD, never silent (P18/P10): a cap/quota rejection or spawn failure stamps
``data.auto_join_error`` (+ ``data.auto_join_next_retry`` backoff so one bad row doesn't re-fire
every tick) — the terminal surfaces it on the meeting row.

Every dispatch also stamps ``data.auto_join_last_attempt`` BEFORE it is made. That records the
attempt rather than its outcome, so it outlives outcomes this row never gets to write: a spawn
that succeeds and a bot that then fails to JOIN takes the row terminal, calendar sync creates a
sibling row for the same occurrence, and without the stamp the sibling was due the instant it
existed — one bot every sync interval for the whole grace window (live 2026-08-17, user 13820).
``calendar_sync`` carries the stamp onto the sibling, and ``due_rows`` honours it, so the ceiling
is ONE dispatch per backoff interval per occurrence however many rows that occurrence acquires.

``auto_join`` defaults ON when the key is absent — planning a meeting with a time means the bot
comes, opting out is the explicit act.

The bot that comes is the SAME bot POST /bots sends: ``ExactRowSpawn`` resolves recording and
transcription through ``env_flags.resolve_spawn_flag``, the one resolver the route uses, so a
calendar bot records like a manual one (#1216). Passing nothing meant inheriting ``request_bot``'s
``recording_enabled=False`` default, and every calendar-joined meeting on stage rev 194 came back
unrecorded while manual ones recorded — a split default nobody chose.

Each bot sent moves ``aw_autojoin_lag_seconds`` (§1.13) by the tick's time minus
(``scheduled_at`` − lead): how long after the bot became due it went.

The tick is a pure-ish function over injected ports (repo, runtime, context fetcher, clock) — the
entrypoint (``__main__``) wraps it in the standard poll loop; tests drive single ticks offline.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from ..metrics import autojoin_lag
from ..obs import log_event

# Sweep cadence/window env vocabulary (config.v1: all optional, sane defaults).
# 120s (#1208): the bot must be STANDING IN THE LOBBY when the meeting starts, not setting out then
# — spawn, browser boot and the join flow all happen inside the lead. Two minutes early plus the
# 15-minute lobby budget (``bot_spawn.service.lobby_budget_ms``) is the pair that makes "the bot is
# already there" true for a host who joins late.
DEFAULT_LEAD_S = 120         # AUTO_JOIN_LEAD_S — join this many seconds BEFORE scheduled_at
DEFAULT_GRACE_S = 600        # AUTO_JOIN_GRACE_S — never join more than this AFTER scheduled_at
DEFAULT_RETRY_BACKOFF_S = 300  # AUTO_JOIN_RETRY_BACKOFF_S — error-stamped rows wait this long


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _instant(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return _parse_iso(value)


@dataclass(frozen=True)
class DueWindow:
    """The time part of the due rule at one tick (§1.5): a row is due from ``scheduled_at - lead_s``
    until its ``scheduled_end_at`` when entries manage it (none: open-ended), else until
    ``scheduled_at + grace_s``. ``due_rows`` filters with it, and the exact-row claim applies it
    again under the link lock, so a row moved or ended after the tick's read is never sent."""

    now: datetime
    lead_s: float = DEFAULT_LEAD_S
    grace_s: float = DEFAULT_GRACE_S

    def holds(self, data: Any, *, managed: bool, scheduled_end_at: Any) -> bool:
        at = _parse_iso(data.get("scheduled_at")) if isinstance(data, dict) else None
        if at is None or self.now < at - timedelta(seconds=self.lead_s):
            return False
        if managed:
            end = _instant(scheduled_end_at)
            return end is None or self.now < end
        return self.now <= at + timedelta(seconds=self.grace_s)


def due_rows(rows: list[dict], *, now: datetime,
             lead_s: float = DEFAULT_LEAD_S, grace_s: float = DEFAULT_GRACE_S,
             retry_backoff_s: float = DEFAULT_RETRY_BACKOFF_S) -> list[dict]:
    """The PURE due-filter over ``scheduled`` rows: auto_join on (absent = on), a joinable link,
    ``scheduled_at`` inside [start - lead, start + grace], past any error backoff, and past the
    backoff owed to the LAST DISPATCH ATTEMPT for this occurrence (``auto_join_last_attempt``).

    A row entries manage (``has_entries``) is due from start - lead until its ``scheduled_end_at``
    (none: open-ended) instead of the grace window (§1.5, R6), and only ``auto_join_next_retry``
    holds it: waiting for a busy link carries no retry pause (R2), and the dispatch stamp guards a
    storm that only an entry-less row can have (calendar sync recreating it, below).

    The last-attempt rule is what bounds the retry storm to one dispatch per backoff interval per
    occurrence. ``auto_join_next_retry`` only ever covered failures the sweep itself saw (a cap
    rejection, a spawn refusal): when the spawn SUCCEEDED and the bot then failed to join, the row
    went terminal carrying no backoff at all, calendar sync recreated a sibling for the same
    occurrence, and the sibling was immediately due — a fresh bot every sync interval until the
    grace window closed (measured live 2026-08-17: meeting 26265 failed 20:06:25, sibling 26267
    created 20:06:35, dispatched 20:06:42). ``auto_join_last_attempt`` records the ATTEMPT rather
    than its outcome, so it survives the outcome — and calendar sync carries it onto the sibling it
    creates for the same occurrence, which is how the backoff outlives the row it was earned on.
    """
    window = DueWindow(now, lead_s, grace_s)
    due: list[dict] = []
    for row in rows:
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        # THE USER SAID NO — checked first, before every rate and window rule below, because none of
        # them can express "never" (F1, stage rev 193 row 26306: DELETE on a still-`scheduled`
        # occurrence answered 200 and set `stop_requested`, and this sweep dispatched it anyway at
        # 02:52:16 — the bot joined a meeting the user had already cancelled). `occurrence.
        # disposition` enforces the same rule for TERMINAL rows; a scheduled row never reached it,
        # because a scheduled row is not terminal. Founder ruling 2026-08-17: "explicit stop must be
        # stop, evict."
        #
        # The stop route never touches a planned row (§1.6: it stops the live meeting only), so no
        # current writer flags a `scheduled` row. This stays as the standing guarantee: no row
        # carrying the user's stop is EVER due, whichever writer left it in this state.
        if data.get("stop_requested"):
            continue
        if data.get("auto_join") is False:
            continue
        if not row.get("native_meeting_id") or row.get("platform") in (None, "", "unknown"):
            continue
        managed = bool(row.get("has_entries"))
        if not window.holds(data, managed=managed, scheduled_end_at=row.get("scheduled_end_at")):
            continue
        retry_at = _parse_iso(data.get("auto_join_next_retry"))
        if retry_at is not None and now < retry_at:
            continue
        attempted_at = _parse_iso(data.get("auto_join_last_attempt"))
        if (not managed and attempted_at is not None
                and now < attempted_at + timedelta(seconds=retry_backoff_s)):
            continue
        due.append(row)
    return due


# Every status in which a bot OWNS the room. A due row whose (user, platform, native) is held by
# one of these on ANOTHER row must never spawn: two Vexa bots in one meeting is customer-visible
# and never correct, however the two rows came to exist.
LIVE_STATUSES = (
    "requested", "joining", "awaiting_admission", "active",
    "needs_help", "needs_human_help", "stopping",
)


def live_keys(rows: Optional[list]) -> dict[tuple, Any]:
    """``{(user_id, platform, native_meeting_id): meeting_id}`` over the repo's live rows."""
    out: dict[tuple, Any] = {}
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        key = (row.get("user_id"), row.get("platform"), row.get("native_meeting_id"))
        if key[0] is None or not key[1] or not key[2]:
            continue
        out.setdefault(key, row.get("id"))
    return out


def _calendar_bot_name(data: dict) -> Optional[str]:
    """Resolve the bot name from the calendar source that armed this meeting.

    Multi-calendar meetings use the first auto-joining source in stable source order. Legacy rows
    without per-source names fall back to the user-wide bot context below.
    """
    raw = data.get("calendar_sources")
    sources = [source for source in raw if isinstance(source, dict)] if isinstance(raw, list) else []
    source = next((item for item in sources if item.get("auto_join", True)), None)
    if source is None and sources:
        source = sources[0]
    value = source.get("bot_name") if source else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _production_transcribe_gate() -> Optional[str]:
    """Mirror POST /bots' CC4 fail-loud STT gate: when transcription resolves ON (env default) but
    the ``stt`` capability is not configured, refuse the auto-spawn with the reason string.

    Read through ``env_flag`` for the same reason as router.py: with a bare ``os.getenv`` a
    set-but-empty ``TRANSCRIBE_ENABLED=`` made ``"" != "true"`` true, so this gate returned None and
    refused nothing — the empty value both disabled transcription AND disarmed the alarm meant to
    catch it. That double failure is why the v0.12.5 witness saw silence with no error."""
    from ..config_preflight import CONFIGURED, capability_state, missing_capability_keys
    from .env_flags import env_flag

    if not env_flag("TRANSCRIBE_ENABLED", True):
        return None
    state = capability_state("stt")
    if state != CONFIGURED:
        unset = ", ".join(missing_capability_keys("stt"))
        return f"STT not configured (capability 'stt' is {state}: {unset} unset)"
    return None


async def auto_join_tick(
    repo,
    runtime,
    *,
    store,
    intake,
    publisher=None,
    authority=None,
    fetch_bot_context: Optional[Callable[[int], Awaitable[Optional[dict]]]] = None,
    publish_status: Optional[Callable[..., Awaitable[None]]] = None,
    transcribe_gate: Optional[Callable[[], Optional[str]]] = None,
    now: Optional[datetime] = None,
    lead_s: float = DEFAULT_LEAD_S,
    grace_s: float = DEFAULT_GRACE_S,
    retry_backoff_s: float = DEFAULT_RETRY_BACKOFF_S,
    token_secret: Optional[str] = None,
    redis_url: Optional[str] = None,
    allow_uncapped: bool = False,
) -> dict:
    """One sweep: spawn every due scheduled meeting. Returns counters for observability:
    ``{"due": n, "spawned": n, "already": n, "errors": n, "skipped_uncapped": n,
    "skipped_live": n, "stopped": n}``. ``skipped_live`` counts rows whose link another bot holds
    (an entry-less row is error-stamped, an entry-managed one waits); ``already`` counts rows a bot
    already covers (a racing spawn, or a merge into the live meeting on the link).

    ``store`` is the intake store (``intake.IntakeStore``: the link lock, the link check, the typed
    failure code) and ``intake`` the entry service whose ``merge_into_live`` takes R2's exception;
    ``publisher`` receives the events they write once committed (``None``: they stay in the
    outbox).

    ``fetch_bot_context(user_id)`` supplies the per-user spawn context the gateway would have
    injected as headers (including the Calendar default ``bot_name``), fetched once per user per
    tick and shared with the spawn. An entry-managed row skipped because its bot limit can't be
    read records ``internal_error`` with the refusal's text, the cause its not-sent outcome carries.
    Three states: the callable is ``None`` (no admin edge configured — the per-user cap is
    UNRESOLVABLE); it returns a dict (use it); it returns ``None`` (identity is configured but
    UNAVAILABLE right now — SKIP the row this tick).

    Fail-closed by default: an unresolvable cap SKIPS the row (never spawns past a cap we cannot
    read), both when no admin edge is configured (``fetch_bot_context is None``) and when identity
    is unreachable (the fetch returns ``None``). Set ``allow_uncapped=True`` (the deliberate
    self-host opt-in, env ``AUTO_JOIN_ALLOW_UNCAPPED=1``) to spawn uncapped when no admin edge is
    configured — the unsafe mode is then chosen, never defaulted.

    ``publish_status(user_id=…, meeting_id=…, native_id=…, status=…, when=…)`` optionally fans the
    row's frame to ``u:{user}:meetings`` after an error stamp so the terminal refreshes."""
    from ..intake.ports import Room
    from ..intake.spawn import (
        IDENTITY_UNAVAILABLE,
        NO_IDENTITY_EDGE,
        ExactRowSpawn,
        spawn_failure,
    )
    from ..intake.sweeps import check_room
    from .ports import TranscriptionNotConfigured

    now = now or datetime.now(timezone.utc)
    gate = transcribe_gate if transcribe_gate is not None else _production_transcribe_gate

    rows = await repo.list_due_meetings(now, lead_s)
    due = due_rows(rows, now=now, lead_s=lead_s, grace_s=grace_s,
                   retry_backoff_s=retry_backoff_s)
    counters = {"due": len(due), "spawned": 0, "already": 0, "errors": 0,
                "skipped_uncapped": 0, "skipped_live": 0, "stopped": 0}
    # Duplicate-dispatch guard for entry-less rows (defense in depth behind the spawn's dedup): a
    # bot already owning this (user, platform, native) on a DIFFERENT row means the meeting is
    # covered. Entry-managed rows are checked under the link lock instead (``check_room``).
    live = live_keys(
        await repo.list_live_meetings() if hasattr(repo, "list_live_meetings") else None
    )
    ctx_cache: dict[int, Optional[dict]] = {}
    uncapped_warned = False

    async def cached_context(user_id: int) -> Optional[dict]:
        if user_id not in ctx_cache:
            assert fetch_bot_context is not None
            ctx_cache[user_id] = await fetch_bot_context(user_id)
        return ctx_cache[user_id]

    # Every spawn claims the exact row by its id (§1.5), with the context read once per user.
    spawn = ExactRowSpawn(
        repo, runtime,
        store=store,
        fetch_bot_context=cached_context if fetch_bot_context is not None else None,
        publisher=publisher,
        authority=authority,
        token_secret=token_secret,
        redis_url=redis_url,
        allow_uncapped=allow_uncapped,
    )

    async def _stamp_error(row: dict, message: str, *, counter: str = "errors",
                           event: str = "auto_join_failed") -> None:
        counters[counter] += 1
        next_retry = (now + timedelta(seconds=retry_backoff_s)).isoformat()
        await repo.merge_meeting_data(row["id"], {
            "auto_join_error": message,
            "auto_join_next_retry": next_retry,
        })
        log_event(event, audience="user", level="warning", span="meetings.auto_join",
                  user_id=row["user_id"], meeting_id=str(row["id"]),
                  fields={"error": message, "next_retry": next_retry})
        if publish_status is not None:
            data = row.get("data") if isinstance(row.get("data"), dict) else {}
            await publish_status(
                user_id=row["user_id"], meeting_id=row["id"],
                native_id=row.get("native_meeting_id"), status=row.get("status"),
                when=data.get("scheduled_at"),
            )

    async def _record_cause(row: dict, code: str, message: str) -> None:
        """Why an entry-managed row got no bot this tick, as its typed code (under the link lock,
        while it is still scheduled): the cause its not-sent outcome carries if it never gets one."""
        if not row.get("has_entries"):
            return
        room = Room(row["platform"], row["native_meeting_id"])
        async with store.room_lock(row["user_id"], [room]) as tx:
            if (await tx.meeting(row["id"])).status == "scheduled":
                await tx.record_spawn_error(row["id"], code, message)

    async def _failed(row: dict, code: str, message: str) -> None:
        """A spawn that failed before claiming the row: its typed code, and the loud stamp +
        backoff."""
        await _record_cause(row, code, message)
        await _stamp_error(row, message)

    for row in due:
        user_id = row["user_id"]
        if row.get("has_entries"):
            room = Room(row["platform"], row["native_meeting_id"])
            check = await check_room(store, user_id, row["id"], room, publisher=publisher)
            if check.kind == "gone":
                continue
            if check.kind == "waiting":
                counters["skipped_live"] += 1
                continue
            if check.kind == "merge":
                assert check.live_id is not None
                if await intake.merge_into_live(user_id, row["id"], check.live_id):
                    counters["already"] += 1
                continue
        else:
            holder = live.get((user_id, row.get("platform"), row.get("native_meeting_id")))
            if holder is not None and holder != row.get("id"):
                # A bot is already in this room on another row — the classic shape is a manual
                # "Send bot now" plus a calendar import of the same link that failed to adopt it
                # (live 2026-08-17: rows 26237 live + 26251 imported, native mjm-dycn-qdp). Refuse
                # LOUDLY (P18): the row carries the reason the terminal renders, never a silent skip.
                await _stamp_error(
                    row,
                    f"a bot is already in this meeting (meeting {holder}) — auto-join skipped so a "
                    f"second bot never joins",
                    counter="skipped_live", event="auto_join_skipped_live",
                )
                continue
        gate_error = gate()
        if gate_error:
            await _failed(row, *spawn_failure(TranscriptionNotConfigured(gate_error)))
            continue

        if fetch_bot_context is None:
            # No admin edge configured → the per-user cap is unresolvable. Fail closed: refuse to
            # spawn rather than spawn uncapped, unless the operator explicitly opted in.
            if not allow_uncapped:
                counters["skipped_uncapped"] += 1
                await _record_cause(row, "internal_error", NO_IDENTITY_EDGE)
                if not uncapped_warned:
                    uncapped_warned = True
                    log_event(
                        "auto_join_skipped_uncapped", audience="operator", level="warning",
                        span="meetings.auto_join", user_id=user_id, meeting_id=str(row["id"]),
                        fields={"reason": "no ADMIN_API_URL/INTERNAL_API_SECRET — per-user cap "
                                "unresolvable; refusing uncapped spawn. Set AUTO_JOIN_ALLOW_UNCAPPED=1 "
                                "to opt into uncapped self-host spawns."})
                continue
        elif await cached_context(user_id) is None:
            # identity configured but unreachable — skip this tick rather than spawn uncapped
            await _record_cause(row, "internal_error", IDENTITY_UNAVAILABLE)
            continue

        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        # Record the ATTEMPT before making it. Written first so it survives everything the attempt
        # can do next — including the two outcomes that leave no trace on this row: a spawn that
        # succeeds and a bot that then fails to join (the row goes terminal, and calendar sync
        # recreates it), or a process death mid-spawn. The stamp is what ``due_rows`` and calendar
        # sync both read to hold the next dispatch for one backoff interval; the claim restamps it
        # with the send time (Ruling R7).
        await repo.merge_meeting_data(row["id"], {"auto_join_last_attempt": now.isoformat()})
        outcome = await spawn.spawn_exact(
            user_id, row["id"], due=DueWindow(now, lead_s, grace_s)
        )
        if outcome.result == "not_due":
            # A writer moved or ended the row after this tick read it; the claim saw it under the
            # lock and sent nothing. The next tick decides it again.
            log_event("auto_join_not_due", audience="system", span="meetings.auto_join",
                      user_id=user_id, meeting_id=str(row["id"]))
            continue
        if outcome.result == "already_live":
            # a manual "Send bot now" (or a racing sweep) already claimed it — success, not an error
            counters["already"] += 1
            continue
        if outcome.result == "failed" and outcome.code == "meeting_stopped":
            # The user stopped it between this tick's read and the spawn fence. Not an error and not
            # a backoff-worthy failure: the row is already terminalized as stopped by the fence, and
            # `due_rows` will never offer it again. Counted so the sweep's numbers stay honest.
            counters["stopped"] += 1
            log_event("auto_join_stopped", audience="user", span="meetings.auto_join",
                      user_id=user_id, meeting_id=str(row["id"]),
                      fields={"reason": "the user stopped this meeting while the bot was starting"})
            continue
        if outcome.result == "failed":
            current = await repo.get_meeting(row["id"])
            if current is None or current.get("status") != "scheduled":
                # The claim went through and the spawn failed after it: the spawn port already
                # ended the meeting ``not_sent`` with the code and message.
                counters["errors"] += 1
                continue
            await _failed(row, outcome.code or "internal_error",
                          outcome.message or "bot workload failed to start")
            continue
        counters["spawned"] += 1
        scheduled_at = _parse_iso(data.get("scheduled_at")) if isinstance(data, dict) else None
        if scheduled_at is not None:
            autojoin_lag((now - (scheduled_at - timedelta(seconds=lead_s))).total_seconds())
        if data.get("auto_join_error"):
            # a prior failure resolved — clear the stamp so the row reads clean
            await repo.merge_meeting_data(row["id"], {
                "auto_join_error": None, "auto_join_next_retry": None,
            })
        log_event("auto_join_spawned", audience="user", span="meetings.auto_join",
                  user_id=user_id, meeting_id=str(row["id"]),
                  fields={"platform": row["platform"], "native": row["native_meeting_id"]})

    return counters
