"""§1.2 — the intake/webhook schema shape, at the model level (no DB needed).

Every invariant here is checkable straight off SQLAlchemy metadata: column presence, nullability,
FK targets + `ondelete`, and index/constraint predicates. The REAL-Postgres proofs (convergence on
an empty DB, the live-dedup constraint actually rejecting a second live row, the migration SQL
applied to a dirty DB, RESTRICT vs CASCADE actually firing, the planner using the due index) are in
`meeting-api`'s `tests/test_intake_pg_schema.py` — see that file's docstring for why they live
there.
"""

from __future__ import annotations

from admin_api.schema.models import (
    Base,
    Meeting,
    MeetingAwState,
    MeetingEntry,
    WebhookDelivery,
    WebhookDeliveryAttempt,
    WebhookOutbox,
    WebhookSubscription,
)


def _indexes(table):
    return {i.name: i for i in table.indexes}


def _fk(column):
    """The single FK constraint on `column`, or None."""
    fks = list(column.foreign_keys)
    return fks[0] if fks else None


# ── meetings: the additions only ──────────────────────────────────────────────────────────────


def test_meetings_uuid_column_is_not_null_unique_and_defaulted():
    col = Meeting.__table__.c.uuid
    assert col.nullable is False
    assert col.unique is True
    assert "gen_random_uuid()" in str(col.server_default.arg)


def test_meetings_uuid_has_a_named_index_for_the_concurrently_build():
    # MIGRATION-0008 builds this CONCURRENTLY on a live DB by NAME — `_sync_indexes` must find the
    # same name once the runbook has run, or it will try (and lock) again at deploy time.
    idx = _indexes(Meeting.__table__)["ix_meetings_uuid"]
    assert idx.unique is True
    assert [c.name for c in idx.columns] == ["uuid"]


def test_old_active_dedup_index_is_gone():
    assert "uq_meeting_active_user_platform_native" not in _indexes(Meeting.__table__)


def test_new_live_dedup_index_columns_and_predicate():
    idx = _indexes(Meeting.__table__)["uq_meeting_live_user_platform_native"]
    assert idx.unique is True
    assert [c.name for c in idx.columns] == [
        "user_id",
        "platform",
        "platform_specific_id",
    ]
    where = str(idx.dialect_options["postgresql"]["where"])
    for status in (
        "requested",
        "joining",
        "awaiting_admission",
        "needs_help",
        "active",
        "stopping",
    ):
        assert status in where
    assert "scheduled" not in where
    assert "completed" not in where and "failed" not in where


def test_due_index_expression_and_predicate():
    idx = _indexes(Meeting.__table__)["ix_meeting_scheduled_due"]
    assert idx.unique is False
    expr = str(idx.expressions[0])
    assert "meeting_event_time(data, start_time, created_at)" in expr
    where = str(idx.dialect_options["postgresql"]["where"])
    assert where == "status = 'scheduled'"


# ── meeting_entries ────────────────────────────────────────────────────────────────────────────


def test_meeting_entries_columns_and_nullability():
    t = MeetingEntry.__table__
    assert t.name == "meeting_entries"
    not_null = {
        "id",
        "user_id",
        "source_user",
        "external_id",
        "meeting_id",
        "meeting_url",
        "platform",
        "native_meeting_id",
        "start_at",
        "join_now",
        "content_hash",
        "state",
    }
    nullable = {
        "title",
        "end_at",
        "time_zone",
        "series_id",
        "attendees",
        "metadata",
        "removed_reason",
        "removed_at",
        "closed_at",
    }
    for name in not_null:
        assert t.c[name].nullable is False, name
    for name in nullable:
        assert t.c[name].nullable is True, name
    assert (
        t.c.metadata.name == "metadata"
    )  # Python attr is metadata_, DB column is "metadata"


def test_meeting_entries_fk_meeting_id_restrict():
    fk = _fk(MeetingEntry.__table__.c.meeting_id)
    assert fk is not None
    assert fk.column.table.name == "meetings"
    assert fk.ondelete == "RESTRICT"


def test_meeting_entries_unique_constraint_and_indexes():
    t = MeetingEntry.__table__
    uqs = {
        c.name: c for c in t.constraints if c.__class__.__name__ == "UniqueConstraint"
    }
    uc = uqs["uq_meeting_entries_user_source_external"]
    assert [c.name for c in uc.columns] == ["user_id", "source_user", "external_id"]

    idx = _indexes(t)
    assert [c.name for c in idx["ix_meeting_entries_meeting_id"].columns] == [
        "meeting_id"
    ]
    assert [
        c.name for c in idx["ix_meeting_entries_user_platform_native_state"].columns
    ] == [
        "user_id",
        "platform",
        "native_meeting_id",
        "state",
    ]
    gin = idx["ix_meeting_entries_attendees_gin"]
    assert gin.dialect_options["postgresql"]["using"] == "gin"
    active = idx["ix_meeting_entries_active_user"]
    assert str(active.dialect_options["postgresql"]["where"]) == "state = 'active'"


# ── meeting_aw_state ───────────────────────────────────────────────────────────────────────────


def test_meeting_aw_state_columns_and_pk_fk_cascade():
    t = MeetingAwState.__table__
    assert t.name == "meeting_aw_state"
    assert [c.name for c in t.primary_key.columns] == ["meeting_id"]
    fk = _fk(t.c.meeting_id)
    assert (
        fk is not None
        and fk.column.table.name == "meetings"
        and fk.ondelete == "CASCADE"
    )
    for name in (
        "scheduled_end_at",
        "time_zone",
        "outcome_kind",
        "outcome_detail",
        "outcome_message",
        "outcome_at",
        "last_error_code",
        "last_error_message",
        "waiting_for_room_sent_at",
        "export_state",
        "export_s3_path",
        "export_error",
        "export_at",
    ):
        assert t.c[name].nullable is True, name
    assert t.c.event_seq.nullable is False
    assert t.c.updated_at.nullable is True


# ── webhook_subscriptions ──────────────────────────────────────────────────────────────────────


def test_webhook_subscriptions_columns():
    t = WebhookSubscription.__table__
    assert t.name == "webhook_subscriptions"
    assert [c.name for c in t.primary_key.columns] == ["id"]
    not_null = {
        "id",
        "user_id",
        "url",
        "secret_enc",
        "enc_key_id",
        "secret_last4",
        "events",
        "active",
    }
    nullable = {
        "previous_secret_enc",
        "previous_enc_key_id",
        "previous_secret_expires_at",
        "description",
    }
    for name in not_null:
        assert t.c[name].nullable is False, name
    for name in nullable:
        assert t.c[name].nullable is True, name
    assert "ix_webhook_subscriptions_user_id" in _indexes(t)


# ── webhook_outbox ─────────────────────────────────────────────────────────────────────────────


def test_webhook_outbox_columns_and_nullable_meeting_fk_cascade():
    t = WebhookOutbox.__table__
    assert t.name == "webhook_outbox"
    assert [c.name for c in t.primary_key.columns] == ["event_id"]
    assert (
        t.c.meeting_id.nullable is True
    )  # null only for the synthetic webhook.test event
    fk = _fk(t.c.meeting_id)
    assert (
        fk is not None
        and fk.column.table.name == "meetings"
        and fk.ondelete == "CASCADE"
    )
    for name in ("event_type", "sequence", "payload_text"):
        assert t.c[name].nullable is False, name


def test_webhook_outbox_unpublished_index_predicate():
    idx = _indexes(WebhookOutbox.__table__)["ix_webhook_outbox_unpublished"]
    assert [c.name for c in idx.columns] == ["created_at"]
    assert str(idx.dialect_options["postgresql"]["where"]) == "published_at IS NULL"


# ── webhook_deliveries ─────────────────────────────────────────────────────────────────────────


def test_webhook_deliveries_columns_and_fk_cascade():
    t = WebhookDelivery.__table__
    assert t.name == "webhook_deliveries"
    fk = _fk(t.c.event_id)
    assert fk is not None
    assert fk.column.table.name == "webhook_outbox" and fk.column.name == "event_id"
    assert fk.ondelete == "CASCADE"
    assert t.c.event_id.nullable is False
    assert t.c.subscription_id.nullable is False
    assert (
        not t.c.subscription_id.foreign_keys
    )  # no FK to webhook_subscriptions — by design (§1.2)
    for name in ("state", "next_attempt_at"):
        assert t.c[name].nullable is False, name
    assert t.c.lease_until.nullable is True


def test_webhook_deliveries_unique_constraint_and_indexes():
    t = WebhookDelivery.__table__
    uqs = {
        c.name: c for c in t.constraints if c.__class__.__name__ == "UniqueConstraint"
    }
    uc = uqs["uq_webhook_deliveries_event_subscription"]
    assert [c.name for c in uc.columns] == ["event_id", "subscription_id"]

    idx = _indexes(t)
    due = idx["ix_webhook_deliveries_due"]
    assert [c.name for c in due.columns] == ["next_attempt_at"]
    assert (
        str(due.dialect_options["postgresql"]["where"])
        == "state IN ('pending','sending')"
    )
    assert [
        c.name for c in idx["ix_webhook_deliveries_subscription_created_at"].columns
    ] == [
        "subscription_id",
        "created_at",
    ]


# ── webhook_delivery_attempts ──────────────────────────────────────────────────────────────────


def test_webhook_delivery_attempts_columns_and_fk_cascade():
    t = WebhookDeliveryAttempt.__table__
    assert t.name == "webhook_delivery_attempts"
    fk = _fk(t.c.delivery_id)
    assert fk is not None
    assert fk.column.table.name == "webhook_deliveries" and fk.ondelete == "CASCADE"
    assert t.c.delivery_id.nullable is False
    for name in ("attempt", "outcome"):
        assert t.c[name].nullable is False, name
    for name in ("status_code", "error", "duration_ms"):
        assert t.c[name].nullable is True, name


def test_webhook_delivery_attempts_indexes():
    idx = _indexes(WebhookDeliveryAttempt.__table__)
    assert [
        c.name for c in idx["ix_webhook_delivery_attempts_delivery_id"].columns
    ] == [
        "delivery_id",
    ]
    assert [c.name for c in idx["ix_webhook_delivery_attempts_created_at"].columns] == [
        "created_at",
    ]


def test_all_six_tables_registered_on_base_metadata():
    for name in (
        "meeting_entries",
        "meeting_aw_state",
        "webhook_subscriptions",
        "webhook_outbox",
        "webhook_deliveries",
        "webhook_delivery_attempts",
    ):
        assert name in Base.metadata.tables
