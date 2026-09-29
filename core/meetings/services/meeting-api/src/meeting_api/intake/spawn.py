"""The production ``SpawnPort`` (§1.5): send the bot to exactly one meeting row, and the one table
that turns every spawn failure into its typed code and exact message.

``ExactRowSpawn.spawn_exact(user_id, meeting_id)`` reads the row, then runs the same
``bot_spawn.request_bot`` flow ``POST /bots`` and the auto-join sweep run, with
``claim_meeting_id``: the spawn claims exactly that row (``scheduled`` → ``requested`` through the
status writer, under the link lock) and stamps ``data.auto_join_last_attempt`` with the send time
(Ruling R7), so ``bot_joins_at`` shows it. The answer is always a ``SpawnOutcome``, never an
exception:

  * ``sent`` — the bot was spawned on the row;
  * ``already_live`` — a bot already owns the row's link (``DuplicateMeeting``);
  * ``not_due`` — only with ``due`` (the scheduler's ``DueWindow``): under the lock the row is
    still ``scheduled`` but no longer due, moved or ended since the scheduler read it
    (``ClaimNotDue``); nothing was claimed;
  * ``failed`` — anything else, with ``spawn_failure``'s code and message.

A failure after the claim (the token or invocation, the runtime, the post-spawn writes, a stop
that won the race) would leave a claimed meeting with no bot and no reason, so the port ends it
``not_sent`` itself (Ruling R17), under the link lock: a row still ``requested`` goes ``failed``
through the status writer with the outcome (``meeting.not_sent``). A row the spawn flow already
ended (a runtime spawn failure, the stop fence) is left alone: the flow wrote its ``failed``
through the status writer with that same outcome, in one terminal event (``meeting.not_sent``; or
``bot.failed`` when an outcome recorded before it, such as R5's, stands). A row still
``requested`` whose workload exists (``bot_container_id`` set: the failure came after the workload
was recorded) is left alone too, and logged: its bot is live and the lifecycle ends it. If a raced
stop already tore that workload down, the nonterminal reconcile sweep ends the row. The events
are handed to ``publisher`` after the commit, when one is given; the outbox holds them either way.

While an entry-managed meeting is on, a failure after the claim is retried instead (§6.9 F-K2,
``retry.retry``): the row goes back to ``requested`` with ``bot.retry`` and the retry driver sends
another bot. A row the spawn flow already sent back (``data.bot_retry``) is left alone. The last
failure of an entry-managed meeting that already had a bot session ends ``failed``
(``bot.failed``) without the ``not_sent`` outcome: a bot was sent.

A row whose link changed between the read and the claim (``ClaimTargetMoved``) is read again and
spawned once more. The spawn context (the per-user bot limit and webhook settings) comes from
``fetch_bot_context(user_id)``, as for the auto-join sweep; the limit is never guessed, so a
missing identity edge, an unreachable identity or a context without ``max_concurrent`` fails the
spawn (``internal_error``) rather than spawning uncapped. The one exception is the sweep's
``AUTO_JOIN_ALLOW_UNCAPPED`` self-host opt-in: with ``allow_uncapped`` and no identity edge
configured, the bot is spawned without a limit. The bot's name is the sweep's: the
calendar source's name, else the user's default. Recording and transcription resolve through
``env_flags.resolve_spawn_flag``, the resolver ``POST /bots`` uses.

``spawn_failure(exc)`` is the §1.5 mapping, used by every caller that spawns (intake, and the
scheduler): the code, and a message that is the exception's own text wherever it has one.
"""

from __future__ import annotations

import traceback
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Sequence, cast

from ..bot_spawn.auto_join import DueWindow, _calendar_bot_name
from ..bot_spawn.env_flags import resolve_spawn_flag
from ..bot_spawn.ports import (
    AuthSessionBusy,
    AuthSessionNotConfigured,
    ClaimNotDue,
    ClaimTargetMoved,
    DuplicateMeeting,
    MaxBotsExceeded,
    MeetingRepo,
    MeetingStopped,
    QuotaExceeded,
    RuntimeClient,
    SpawnFailed,
    TranscriptionNotConfigured,
)
from ..bot_spawn.service import request_bot
from ..obs import log_event
from ..service_authority import ServiceAuthorityDenied, ServiceAuthorityUnavailable
from . import retry
from .ports import EventPublisher, IntakeStore, Room, SpawnOutcome
from .settings import IntakeSettings
from .status import Outcome

__all__ = [
    "ExactRowSpawn",
    "IDENTITY_UNAVAILABLE",
    "NO_BOT_LIMIT",
    "NO_IDENTITY_EDGE",
    "spawn_failure",
]

#: The exact refusals when the per-user bot limit can't be read (code ``internal_error``).
NO_IDENTITY_EDGE = "the bot limit could not be read: no identity edge is configured"
IDENTITY_UNAVAILABLE = "the bot limit could not be read: identity is unavailable"
NO_BOT_LIMIT = "the bot limit could not be read: identity returned no max_concurrent"

BotContextFetcher = Callable[[int], Awaitable[Optional[dict[str, Any]]]]


def _bot_limit(exc: MaxBotsExceeded) -> str:
    if exc.active is None:
        return f"bot limit reached (limit {exc.cap})"
    return f"bot limit reached ({exc.active} of {exc.cap})"


def _quota(exc: QuotaExceeded) -> str:
    return f"bot limit reached ({exc})" if str(exc) else "bot limit reached"


def _authority_denied(exc: ServiceAuthorityDenied) -> str:
    return f"service not allowed ({exc.reason}; decision {exc.decision_id})"


def _own_text(default: str) -> Callable[[BaseException], str]:
    return lambda exc: str(exc) or default


#: §1.5: each spawn exception, its typed code (§1.13) and its exact message.
_FAILURES: tuple[tuple[type[BaseException], str, Callable[[Any], str]], ...] = (
    (MaxBotsExceeded, "account_limit", _bot_limit),
    (QuotaExceeded, "account_limit", _quota),
    (DuplicateMeeting, "already_live", _own_text("a bot is already in this meeting")),
    (MeetingStopped, "meeting_stopped", _own_text("the meeting was stopped")),
    (SpawnFailed, "spawn_error", _own_text("bot workload failed to start")),
    (ServiceAuthorityDenied, "authority_denied", _authority_denied),
    (
        ServiceAuthorityUnavailable,
        "authority_unavailable",
        _own_text("service authority unavailable"),
    ),
    (
        AuthSessionNotConfigured,
        "auth_session",
        _own_text("bot sign-in is not configured"),
    ),
    (AuthSessionBusy, "auth_session", _own_text("the bot's signed-in session is busy")),
    (
        TranscriptionNotConfigured,
        "transcription_config",
        _own_text("transcription is not configured"),
    ),
)


def spawn_failure(
    exc: BaseException,
    *,
    user_id: Optional[int] = None,
    meeting_id: Optional[int] = None,
) -> tuple[str, str]:
    """``(code, message)`` for a spawn exception (§1.5). Anything not in the table is
    ``internal_error``, logged here with its stack."""
    for kind, code, message in _FAILURES:
        if isinstance(exc, kind):
            return code, message(exc)
    log_event(
        "spawn_internal_error",
        audience="operator",
        level="error",
        span="meetings.spawn",
        user_id=user_id,
        meeting_id=None if meeting_id is None else str(meeting_id),
        fields={
            "error": type(exc).__name__,
            "traceback": "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ),
        },
    )
    return "internal_error", f"internal error ({type(exc).__name__})"


class _Refused(Exception):
    """The spawn context couldn't be read; the message is the outcome's."""


class _ClaimWatch:
    """The repo as ``request_bot`` sees it, noting whether the guarded claim committed: a failure
    after that point belongs to a claimed row."""

    def __init__(self, repo: MeetingRepo) -> None:
        self._repo = repo
        self.claimed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._repo, name)

    async def create_meeting_guarded(self, **kwargs: Any) -> dict:
        row = await self._repo.create_meeting_guarded(**kwargs)
        self.claimed = True
        return row


class ExactRowSpawn:
    """``SpawnPort`` over ``bot_spawn``'s repo and runtime (§1.5)."""

    def __init__(
        self,
        repo: MeetingRepo,
        runtime: RuntimeClient,
        *,
        store: IntakeStore,
        fetch_bot_context: Optional[BotContextFetcher],
        publisher: Optional[EventPublisher] = None,
        authority: Any = None,
        token_secret: Optional[str] = None,
        redis_url: Optional[str] = None,
        allow_uncapped: bool = False,
    ) -> None:
        self._repo = repo
        self._runtime = runtime
        self._store = store
        self._publisher = publisher
        self._fetch_bot_context = fetch_bot_context
        self._authority = authority
        self._token_secret = token_secret
        self._redis_url = redis_url
        self._allow_uncapped = allow_uncapped

    async def spawn_exact(
        self, user_id: int, meeting_id: int, *, due: Optional[DueWindow] = None
    ) -> SpawnOutcome:
        watch = _ClaimWatch(self._repo)
        try:
            await self._spawn(watch, user_id, meeting_id, due)
        except _Refused as exc:
            return self._failed(user_id, meeting_id, "internal_error", str(exc))
        except DuplicateMeeting:
            # Raised only by the guarded claim itself, so nothing was claimed.
            return SpawnOutcome("already_live")
        except ClaimNotDue:
            # Raised by the claim under the lock, before it writes anything.
            return SpawnOutcome("not_due")
        except Exception as exc:
            code, message = spawn_failure(exc, user_id=user_id, meeting_id=meeting_id)
            if watch.claimed:
                await self._end_not_sent(user_id, meeting_id, code, message)
            return self._failed(user_id, meeting_id, code, message)
        return SpawnOutcome("sent")

    async def _spawn(
        self,
        watch: _ClaimWatch,
        user_id: int,
        meeting_id: int,
        due: Optional[DueWindow],
    ) -> None:
        ctx = await self._context(user_id)
        for attempt in range(2):
            row = await self._repo.get_meeting(meeting_id)
            if row is None or row.get("user_id") != user_id:
                raise LookupError(f"meeting {meeting_id} not found for user {user_id}")
            stored = row.get("data")
            data: dict[str, Any] = stored if isinstance(stored, dict) else {}
            try:
                await request_bot(
                    cast(MeetingRepo, watch),
                    self._runtime,
                    authority=self._authority,
                    user_id=user_id,
                    platform=row["platform"],
                    native_meeting_id=row["native_meeting_id"],
                    meeting_url=data.get("constructed_meeting_url"),
                    bot_name=_calendar_bot_name(data) or ctx.get("bot_name"),
                    recording_enabled=resolve_spawn_flag(
                        "RECORDING_ENABLED", default=True
                    ),
                    transcribe_enabled=resolve_spawn_flag(
                        "TRANSCRIBE_ENABLED", default=True
                    ),
                    max_concurrent=ctx.get("max_concurrent"),
                    webhook_url=ctx.get("webhook_url"),
                    webhook_secret=ctx.get("webhook_secret"),
                    webhook_events=ctx.get("webhook_events"),
                    token_secret=self._token_secret,
                    redis_url=self._redis_url,
                    claim_meeting_id=meeting_id,
                    claim_due=due,
                )
                return
            except ClaimTargetMoved:
                if attempt:
                    raise

    async def _context(self, user_id: int) -> dict[str, Any]:
        if self._fetch_bot_context is None and self._allow_uncapped:
            return {}
        if self._fetch_bot_context is None:
            raise _Refused(NO_IDENTITY_EDGE)
        ctx = await self._fetch_bot_context(user_id)
        if ctx is None:
            raise _Refused(IDENTITY_UNAVAILABLE)
        if ctx.get("max_concurrent") is None:
            raise _Refused(NO_BOT_LIMIT)
        return ctx

    async def _end_not_sent(
        self, user_id: int, meeting_id: int, code: str, message: str
    ) -> None:
        """Ruling R17: the claimed row ends ``not_sent`` with the code and message, or, while the
        meeting is on, goes back to ``requested`` for another bot (§6.9 F-K2, ``retry.retry``). The
        spawn flow fails the row itself wherever a workload may exist, so a row that reaches here
        names no workload, and it is never recorded as gone. The last failure of a meeting
        entries manage that already had a bot session ends ``failed`` without the ``not_sent``
        outcome. A row the spawn flow already sent back (``data.bot_retry``) is left alone. Best
        effort: a failure here is logged with its stack, and the spawn's answer stands."""
        events: list[str] = []
        try:
            row = await self._repo.get_meeting(meeting_id)
            if row is None:
                raise LookupError(f"meeting {meeting_id} not found")
            room = Room(row["platform"], row["native_meeting_id"])
            had_bot = bool(await self._repo.list_sessions(meeting_id=meeting_id))
            async with self._store.room_lock(user_id, [room]) as tx:
                current = await tx.meeting(meeting_id)
                if retry.marker(current.data) is not None:
                    pass
                elif current.status == "requested" and current.row.get(
                    "bot_container_id"
                ):
                    log_event(
                        "spawn_not_sent_skipped_live_workload",
                        audience="operator",
                        level="warning",
                        span="meetings.spawn",
                        user_id=user_id,
                        meeting_id=str(meeting_id),
                        fields={"code": code},
                    )
                elif current.status == "requested":
                    # Nothing says whether a workload was started: never recorded gone.
                    failure = retry.Failure(
                        "failed", None, message, stage="requested", code=code,
                        proven_gone=False,
                    )
                    written = await retry.retry(
                        tx, meeting_id, failure, now=datetime.now(timezone.utc),
                        settings=IntakeSettings.from_env(),
                    )
                    if written is None:
                        managed = bool(current.active_entries())
                        outcome = (
                            None if had_bot and managed else Outcome("not_sent", code, message)
                        )
                        written = await tx.status(
                            meeting_id,
                            "failed",
                            expected_from={"requested"},
                            outcome=outcome,
                            change_reason=code,
                            event_type=None if outcome is None else "meeting.not_sent",
                        )
                    events.append(written.event_id)
        except Exception as exc:
            log_event(
                "spawn_not_sent_record_failed",
                audience="operator",
                level="error",
                span="meetings.spawn",
                user_id=user_id,
                meeting_id=str(meeting_id),
                fields={
                    "error": type(exc).__name__,
                    "traceback": "".join(
                        traceback.format_exception(type(exc), exc, exc.__traceback__)
                    ),
                },
            )
            return
        await self._publish(events)

    async def _publish(self, event_ids: Sequence[str]) -> None:
        if not event_ids or self._publisher is None:
            return
        try:
            await self._publisher.publish(event_ids)
        except Exception as exc:
            log_event(
                "spawn_publish_failed",
                audience="operator",
                level="warning",
                span="meetings.spawn",
                fields={"events": len(event_ids), "error": type(exc).__name__},
            )

    @staticmethod
    def _failed(user_id: int, meeting_id: int, code: str, message: str) -> SpawnOutcome:
        log_event(
            "spawn_exact_failed",
            audience="user",
            level="warning",
            span="meetings.spawn",
            user_id=user_id,
            meeting_id=str(meeting_id),
            fields={"code": code, "message": message},
        )
        return SpawnOutcome("failed", code, message)
