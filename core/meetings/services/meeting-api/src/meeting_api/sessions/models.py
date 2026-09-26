"""The meeting-api SQLAlchemy models — the per-service mirror of the backing-stack
``meetings`` / ``transcriptions`` / ``meeting_sessions`` tables.

SELF-CONTAINED per-service mirror (the SSOT is ``identity/services/admin-api/.../schema/
models.py``). Co-located here — NOT imported across the lane seam — for the same reason
``obs.py`` is duplicated per service: it keeps the cross-domain import-boundary gates
(``gate:isolation-py`` / ``gate:graph-py``) clean while binding the SAME physical Postgres
schema (identical table names + columns). ``gate:isolation-py`` PRE-ALLOWS a ``meeting_api →
admin_api`` edge for these models, but we DO NOT take it: mirroring keeps the monolith
import-free of the identity domain (no real edge is created), exactly as the folded collector
already did.

SQLAlchemy is imported at MODULE load, so this module is only imported lazily by the production
``bot_spawn`` / ``recordings`` / ``collector.adapters`` paths at runtime — never during the gate
venv's test run (the in-memory fakes never touch it). That is why ``pyproject.toml`` carries no
``greenlet`` pin.

Recordings + notes live in ``meetings.data`` JSONB (there is NO separate recordings table — see
``schema/MIGRATION-0001-drop-recordings.md``). ``MeetingSession`` keys N sessions per meeting by
``session_uid`` (one per bot connection), the linkage ``bot_spawn`` eager-creates on spawn and
``recordings`` looks up on chunk upload.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func, text

Base = declarative_base()


class Meeting(Base):
    __tablename__ = "meetings"

    id = Column(Integer, primary_key=True, index=True)
    # §1.2 — every meeting's stable external identity (intake/webhook payloads key off this, not
    # the internal `id`). `index=True` gives it a named index (`ix_meetings_uuid`) so the
    # CONCURRENTLY build on a live DB (MIGRATION-0008) converges by name with `ensure_schema`.
    uuid = Column(UUID(as_uuid=True), nullable=False, unique=True, index=True,
                  server_default=text("gen_random_uuid()"))
    user_id = Column(Integer, nullable=False, index=True)
    platform = Column(String(100), nullable=False)
    platform_specific_id = Column(String(255), index=True, nullable=True)
    status = Column(String(50), nullable=False, default="requested", index=True)
    bot_container_id = Column(String(255), nullable=True)
    start_time = Column(DateTime, nullable=True)
    end_time = Column(DateTime, nullable=True)
    # recordings[] are stored in this JSONB blob — NOT in a `recordings` table.
    data = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=lambda: {})
    created_at = Column(DateTime, server_default=func.now(), index=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    transcriptions = relationship("Transcription", back_populates="meeting")
    sessions = relationship(
        "MeetingSession", back_populates="meeting", cascade="all, delete-orphan"
    )

    @property
    def native_meeting_id(self):
        return self.platform_specific_id

    __table_args__ = (
        Index(
            "ix_meeting_user_platform_native_id_created_at",
            "user_id", "platform", "platform_specific_id", "created_at",
        ),
        Index("ix_meeting_data_gin", "data", postgresql_using="gin"),
        # #800: the collector's list_meetings UNIONs three access branches, one scan path each —
        # owner top-N, transcript-share containment, workspace top-N. The whole-column GIN above
        # cannot serve a containment probe on the `transcript_viewers` key alone, and the
        # single-column created_at index invites the catastrophic backward-walk plan the UNION
        # exists to avoid.
        # ⚠ PROD ROLLOUT: build these CONCURRENTLY out-of-band before deploying (vexa-platform
        # O-book O4); in-band CREATE INDEX locks `meetings` under live traffic.
        Index("ix_meeting_user_created_at", "user_id", "created_at"),
        Index("ix_meeting_transcript_viewers_gin",
              text("(data -> 'transcript_viewers') jsonb_path_ops"), postgresql_using="gin"),
        Index("ix_meeting_workspace_created_at", text("(data ->> 'workspace_id')"), "created_at"),
        # #1222: the list_view sort is (non-terminal pin, MEETING EVENT time, id) — see
        # collector/adapters.py list_meetings — so the owner/workspace union branches need THAT
        # key index-walkable, not created_at. `meeting_event_time()` is the IMMUTABLE SQL wrapper
        # for COALESCE(data.scheduled_at, start_time, created_at); the admin-api schema sync
        # creates it (_sync_functions) before any index DDL, so a metadata.create_all from THIS
        # mirror alone (no function in the DB) cannot build these two — run the admin-api sync or
        # MIGRATION-0005 first. The expressions must stay verbatim-equal to the query's ORDER BY
        # or the planner won't substitute the index.
        Index("ix_meeting_user_event_order", "user_id",
              text("(status IN ('active', 'awaiting_admission', 'joining', 'requested', "
                   "'scheduled', 'stopping'))"),
              text("meeting_event_time(data, start_time, created_at)"),
              "id"),
        Index("ix_meeting_workspace_event_order", text("(data ->> 'workspace_id')"),
              text("(status IN ('active', 'awaiting_admission', 'joining', 'requested', "
                   "'scheduled', 'stopping'))"),
              text("meeting_event_time(data, start_time, created_at)"),
              "id"),
        # ROB1/ROB2 DB-level backstop (§1.2): at most ONE LIVE meeting per (user, platform,
        # native_meeting_id) — a partial unique index over the bot-lifecycle statuses only. A
        # user's meeting_entries can hold many `scheduled` occurrences for one link (recurring
        # series), so the dedup key can no longer cover 'scheduled' the way
        # `uq_meeting_active_user_platform_native` used to — only a row the bot has actually spawned
        # for is live. Concurrent duplicate spawns that slip past the in-txn advisory-lock dedup
        # (e.g. across meeting-api processes) hit this index → IntegrityError → mapped to
        # DuplicateMeeting in create_meeting_guarded.
        #
        # `ensure_schema` (admin-api) matches indexes BY NAME and never alters one in place, so
        # swapping the old dedup index for this one is a manual runbook (CONCURRENTLY, before the
        # deploy) — see admin-api's schema/MIGRATION-0008-meeting-live-dedup-index.md.
        Index(
            "uq_meeting_live_user_platform_native",
            "user_id", "platform", "platform_specific_id",
            unique=True,
            postgresql_where=text(
                "status IN ('requested', 'joining', 'awaiting_admission', 'needs_help', "
                "'active', 'stopping')"
            ),
        ),
        # §1.2 — the scheduler's due query: only meetings still `scheduled`, ordered by the same
        # `meeting_event_time()` wrapper the event-order indexes use (MIGRATION-0005), restricted to
        # rows the scheduler still cares about so it never walks history.
        Index(
            "ix_meeting_scheduled_due",
            text("meeting_event_time(data, start_time, created_at)"),
            postgresql_where=text("status = 'scheduled'"),
        ),
    )


class Transcription(Base):
    __tablename__ = "transcriptions"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=False, index=True)
    start_time = Column(Float, nullable=False)
    end_time = Column(Float, nullable=False)
    text = Column(Text, nullable=False)
    speaker = Column(String(255), nullable=True)
    language = Column(String(10), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    session_uid = Column(String, nullable=True, index=True)
    segment_id = Column(String, nullable=True)

    meeting = relationship("Meeting", back_populates="transcriptions")

    __table_args__ = (
        Index("ix_transcription_meeting_start", "meeting_id", "start_time"),
        # The segment identity the db-writer upserts on (ON CONFLICT (meeting_id, segment_id)
        # WHERE segment_id IS NOT NULL) — mirrors the AUTHORITATIVE admin-api schema
        # (admin_api.schema.models), which owns the table; kept in sync here so a
        # metadata.create_all from this mirror builds the same shape.
        Index("ix_transcription_meeting_segment", "meeting_id", "segment_id",
              unique=True, postgresql_where=segment_id.isnot(None)),
    )


class MeetingSession(Base):
    """N sessions per meeting, keyed by ``session_uid`` (one per bot connection/reconnect).

    ``bot_spawn`` eager-creates a row on spawn (``session_uid`` == the ``connectionId`` minted into
    the bot's invocation); ``recordings`` looks the row up by ``session_uid`` when the bot uploads
    a chunk, so the upload finds its meeting even before the bot reports ``active``.
    """

    __tablename__ = "meeting_sessions"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=False, index=True)
    session_uid = Column(String, nullable=False, index=True)
    session_start_time = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    meeting = relationship("Meeting", back_populates="sessions")

    __table_args__ = (
        UniqueConstraint("meeting_id", "session_uid", name="_meeting_session_uc"),
    )


# --------------------------------------------------------------------------- #
# meeting intake + webhooks (§1.2) — mirror of admin_api.schema.models; SSOT there.
# --------------------------------------------------------------------------- #
class MeetingEntry(Base):  # type: ignore[valid-type,misc]
    """One calendar/manual occurrence bound to a `meetings` row (§1.2). Many entries can point at
    the same `meeting_id` (a recurring series); `content_hash` lets intake detect a no-op re-push."""
    __tablename__ = "meeting_entries"

    id = Column(BigInteger, primary_key=True)
    user_id = Column(Integer, nullable=False)
    source_user = Column(Text, nullable=False)
    external_id = Column(String(255), nullable=False)
    meeting_id = Column(Integer, ForeignKey("meetings.id", ondelete="RESTRICT"),
                         nullable=False, index=True)
    meeting_url = Column(Text, nullable=False)
    platform = Column(String(100), nullable=False)
    native_meeting_id = Column(String(255), nullable=False)
    title = Column(String(512), nullable=True)
    start_at = Column(DateTime(timezone=True), nullable=False)
    end_at = Column(DateTime(timezone=True), nullable=True)
    time_zone = Column(Text, nullable=True)
    series_id = Column(String(255), nullable=True)
    attendees = Column(ARRAY(Text), nullable=True)
    join_now = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    metadata_ = Column("metadata", JSONB, nullable=True)
    content_hash = Column(String(64), nullable=False)
    state = Column(String(16), nullable=False)  # 'active' | 'removed' | 'closed'
    removed_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    removed_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "source_user", "external_id",
                          name="uq_meeting_entries_user_source_external"),
        Index("ix_meeting_entries_user_platform_native_state",
              "user_id", "platform", "native_meeting_id", "state"),
        Index("ix_meeting_entries_attendees_gin", "attendees", postgresql_using="gin"),
        Index("ix_meeting_entries_active_user", "user_id",
              postgresql_where=text("state = 'active'")),
    )


class MeetingAwState(Base):  # type: ignore[valid-type,misc]
    """AW-owned per-meeting state that doesn't belong in upstream's `meetings.data` blob (§1.2):
    the scheduled window, outcome/error reporting, and export tracking — one row per meeting."""
    __tablename__ = "meeting_aw_state"

    meeting_id = Column(Integer, ForeignKey("meetings.id", ondelete="CASCADE"), primary_key=True)
    scheduled_end_at = Column(DateTime(timezone=True), nullable=True)
    time_zone = Column(Text, nullable=True)
    event_seq = Column(BigInteger, nullable=False, server_default="0")
    outcome_kind = Column(Text, nullable=True)
    outcome_detail = Column(Text, nullable=True)
    outcome_message = Column(Text, nullable=True)
    outcome_at = Column(DateTime(timezone=True), nullable=True)
    last_error_code = Column(Text, nullable=True)
    last_error_message = Column(Text, nullable=True)
    waiting_for_room_sent_at = Column(DateTime(timezone=True), nullable=True)
    export_state = Column(Text, nullable=True)
    export_s3_path = Column(Text, nullable=True)
    export_error = Column(Text, nullable=True)
    export_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class WebhookSubscription(Base):  # type: ignore[valid-type,misc]
    """A user's webhook target (§1.2). `secret_enc`/`enc_key_id` are the encrypted-at-rest signing
    secret; `previous_*` carries the prior secret through a rotation window so both signatures
    validate until `previous_secret_expires_at`."""
    __tablename__ = "webhook_subscriptions"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(Integer, nullable=False, index=True)
    url = Column(Text, nullable=False)
    secret_enc = Column(LargeBinary, nullable=False)
    enc_key_id = Column(String(64), nullable=False)
    secret_last4 = Column(String(4), nullable=False)
    previous_secret_enc = Column(LargeBinary, nullable=True)
    previous_enc_key_id = Column(String(64), nullable=True)
    previous_secret_expires_at = Column(DateTime(timezone=True), nullable=True)
    events = Column(ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"))
    active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class WebhookOutbox(Base):  # type: ignore[valid-type,misc]
    """The durable record of every webhook-worthy event (§1.2), written once in the same
    transaction as the state change it describes. `payload_text` is the exact bytes later sent —
    deliveries never re-serialize. `meeting_id` is null only for the synthetic `webhook.test` event."""
    __tablename__ = "webhook_outbox"

    event_id = Column(String(80), primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id", ondelete="CASCADE"), nullable=True)
    event_type = Column(String(64), nullable=False)
    sequence = Column(BigInteger, nullable=False)
    payload_text = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    published_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_webhook_outbox_unpublished", "created_at",
              postgresql_where=text("published_at IS NULL")),
    )


class WebhookDelivery(Base):  # type: ignore[valid-type,misc]
    """One row per (event, subscription) — the delivery state machine (§1.2). `next_attempt_at` +
    `lease_until` drive the retry worker's claim; `state` is the current position in that machine."""
    __tablename__ = "webhook_deliveries"

    id = Column(BigInteger, primary_key=True)
    event_id = Column(String(80), ForeignKey("webhook_outbox.event_id", ondelete="CASCADE"),
                       nullable=False)
    subscription_id = Column(UUID(as_uuid=True), nullable=False)
    user_id = Column(Integer, nullable=False)
    state = Column(String(16), nullable=False)  # 'pending'|'sending'|'delivered'|'failed'|'dead'|'cancelled'
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at = Column(DateTime(timezone=True), nullable=False)
    lease_until = Column(DateTime(timezone=True), nullable=True)
    last_status_code = Column(Integer, nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("event_id", "subscription_id",
                          name="uq_webhook_deliveries_event_subscription"),
        Index("ix_webhook_deliveries_due", "next_attempt_at",
              postgresql_where=text("state IN ('pending','sending')")),
        Index("ix_webhook_deliveries_subscription_created_at", "subscription_id", "created_at"),
    )


class WebhookDeliveryAttempt(Base):  # type: ignore[valid-type,misc]
    """The delivery log (§1.2) — one row per attempt, kept even after the delivery reaches a
    terminal state, so a subscriber dispute has the full HTTP history to point to."""
    __tablename__ = "webhook_delivery_attempts"

    id = Column(BigInteger, primary_key=True)
    delivery_id = Column(BigInteger, ForeignKey("webhook_deliveries.id", ondelete="CASCADE"),
                          nullable=False, index=True)
    attempt = Column(Integer, nullable=False)
    outcome = Column(String(16), nullable=False)
    status_code = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
