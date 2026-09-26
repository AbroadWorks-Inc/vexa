"""The v0.12 backing-stack schema — the SQLAlchemy source-of-truth.

Derived from the parent (re-read, not blind-copied):
  - identity tables    ← `libs/admin-models/admin_models/models.py`   (User, APIToken)
  - meeting tables     ← `services/meeting-api/meeting_api/models.py` (Meeting, Transcription, MeetingSession)

ONE `Base` here (the parent split identity vs meeting bases and bridged the FK via
`ensure_schema(prerequisites=...)`). Co-locating them in one metadata is the same shape —
`create_all` emits tables in FK order, so `users` lands before `api_tokens` and `meetings`
before its children. The cross-domain FK (api_tokens.user_id → users.id, meetings has a
logical user_id) is preserved.

DROPPED vs the parent: the `recordings` + `media_files` tables. See O-STACK-1 migration note
(`schema/MIGRATION-0001-drop-recordings.md`) — they are write-never dead columns; recordings
live in `meetings.data['recordings'][]` JSONB (the `internal_upload_recording` writer). The
parent keeps the ORM classes only as a legacy READ fallback guarded by
`to_regclass('public.recordings') IS NOT NULL`, so omitting the tables is safe.
"""
from sqlalchemy import (
    BigInteger, Boolean, Column, String, Text, Integer, DateTime, Float, LargeBinary,
    ForeignKey, Index, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, ARRAY, UUID
from sqlalchemy.sql import func, text
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime

Base = declarative_base()


# --------------------------------------------------------------------------- #
# identity tables (parent: libs/admin-models/admin_models/models.py)
# --------------------------------------------------------------------------- #
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    name = Column(String(100))
    image_url = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), default=func.now())
    max_concurrent_bots = Column(Integer, nullable=False, server_default="3", default=3)
    # webhook_url / webhook_secret / webhook_events live here (surfaced by /internal/validate)
    data = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=lambda: {})

    api_tokens = relationship("APIToken", back_populates="user")

    __table_args__ = (
        # MIGRATION-0007 — every email lookup in this service folds case
        # (`func.lower(User.email) == …`, `create_user` + `GET /admin/users/email/{email}`), and the
        # plain `email` index above cannot serve that predicate: both lookups are sequential scans
        # on `users`, on the sign-in path.
        #
        # NON-UNIQUE, DELIBERATELY, AND THIS IS THE HONEST PART. The invariant we want is
        # one-address-one-account, which is a UNIQUE index on `lower(email)`. It cannot ship as one
        # here: the instances that need it are exactly the instances that already hold case-variant
        # duplicate rows (that is the defect), a UNIQUE build against those rows raises
        # UniqueViolation, and `_sync_indexes` FAILS CLOSED on a unique-index failure by design
        # (#1186) — so shipping it unique would turn "this instance has a few ghost accounts" into
        # "admin-api will not start". Trading a data defect for an outage is not a fix.
        #
        # What closes the hole instead, today: `create_user` stores new addresses folded, so a new
        # collision is caught by the `email` UNIQUE index that already exists, and both lookups
        # ORDER BY id so an instance holding duplicates resolves the same row every time. The
        # UNIQUE upgrade is an operator step AFTER reconciling the duplicates — the SQL to find
        # them, and the CONCURRENTLY build, are in schema/MIGRATION-0007-users-email-lower.md.
        Index("ix_users_email_lower", text("lower(email)")),
    )


class PlatformSetting(Base):
    """Deployment-wide runtime config, one JSONB value per key (`models`, `transcription`).
    The DB layer between per-user prefs (users.data) and the process env: services resolve
    user > platform_settings > env. Written only over the internal tier (the terminal's
    admin-gated settings editor fronts it); read over the same edge by agent-api/meeting-api."""
    __tablename__ = "platform_settings"

    key = Column(String(64), primary_key=True)
    value = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=lambda: {})
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class APIToken(Base):
    __tablename__ = "api_tokens"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String(255), unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    scopes = Column(ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"))
    name = Column(String(255), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    last_used_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="api_tokens")


# --------------------------------------------------------------------------- #
# meeting tables (parent: services/meeting-api/meeting_api/models.py)
# --------------------------------------------------------------------------- #
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
    sessions = relationship("MeetingSession", back_populates="meeting", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_meeting_user_platform_native_id_created_at",
              "user_id", "platform", "platform_specific_id", "created_at"),
        Index("ix_meeting_data_gin", "data", postgresql_using="gin"),
        # #800 (mirror of meeting-api's sessions/models.py): the collector's list_meetings UNIONs
        # three access branches, one scan path each — owner top-N, transcript-share containment,
        # workspace top-N. The whole-column GIN above cannot serve a containment probe on the
        # `transcript_viewers` key alone.
        # ⚠ PROD ROLLOUT: build CONCURRENTLY out-of-band before deploying (vexa-platform O-book
        # O4); _sync_indexes' in-band CREATE INDEX locks `meetings` under live traffic.
        Index("ix_meeting_user_created_at", "user_id", "created_at"),
        Index("ix_meeting_transcript_viewers_gin",
              text("(data -> 'transcript_viewers') jsonb_path_ops"), postgresql_using="gin"),
        Index("ix_meeting_workspace_created_at", text("(data ->> 'workspace_id')"), "created_at"),
        # #1222 (mirror of meeting-api's sessions/models.py): the list_view sort is
        # (non-terminal pin, MEETING EVENT time, id), so the owner/workspace union branches need
        # THAT key index-walkable, not created_at. `meeting_event_time()` is the IMMUTABLE wrapper
        # for COALESCE(data.scheduled_at, start_time, created_at), created by _sync_functions
        # (sync.py) BEFORE create_all/_sync_indexes — these expressions must stay verbatim-equal
        # to the ORDER BY in the collector's list_meetings or the planner won't substitute them.
        # ⚠ PROD ROLLOUT: create the function + both indexes CONCURRENTLY out-of-band BEFORE this
        # change deploys — see schema/MIGRATION-0005-meeting-event-order.md.
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
        # ROB1/ROB2 DB-level backstop (mirror of meeting-api's sessions/models.py; §1.2): at most
        # ONE LIVE meeting per (user, platform, native_meeting_id) — a partial unique index over the
        # bot-lifecycle statuses only. A user's meeting_entries can hold many `scheduled` occurrences
        # for one link (recurring series), so the dedup key can no longer cover 'scheduled' the way
        # `uq_meeting_active_user_platform_native` used to — only a row the bot has actually spawned
        # for is live. The in-txn pg_advisory_xact_lock in create_meeting_guarded serializes
        # same-process spawns; this index backstops the cross-process race → IntegrityError →
        # DuplicateMeeting.
        #
        # `ensure_schema` matches indexes BY NAME and never alters one in place, so swapping the old
        # dedup index for this one is a manual runbook (CONCURRENTLY, before the deploy) —
        # see schema/MIGRATION-0008-meeting-live-dedup-index.md.
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
        # ⚠ PROD ROLLOUT: build CONCURRENTLY out-of-band before deploying — see
        # schema/MIGRATION-0008-meeting-live-dedup-index.md.
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
        Index("ix_transcription_meeting_segment", "meeting_id", "segment_id",
              unique=True, postgresql_where=segment_id.isnot(None)),
    )


class MeetingSession(Base):
    __tablename__ = "meeting_sessions"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=False, index=True)
    session_uid = Column(String, nullable=False, index=True)
    session_start_time = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    meeting = relationship("Meeting", back_populates="sessions")

    __table_args__ = (
        UniqueConstraint("meeting_id", "session_uid", name="_meeting_session_uc"),
    )


# --------------------------------------------------------------------------- #
# meeting intake + webhooks (§1.2) — ours, no upstream parent
# --------------------------------------------------------------------------- #
class MeetingEntry(Base):
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


class MeetingAwState(Base):
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


class WebhookSubscription(Base):
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


class WebhookOutbox(Base):
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


class WebhookDelivery(Base):
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


class WebhookDeliveryAttempt(Base):
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
