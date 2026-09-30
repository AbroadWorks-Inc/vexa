"""§1.2 — the mirror proof: `meeting_api.sessions.models` must build the SAME physical schema as
the SSOT (`admin_api.schema.models`) for every intake/webhook table plus the `meetings` additions.

The test-isolation gate (`node scripts/gates.mjs test-isolation`) lets a meeting-api test import
`admin_api`: `scripts/check-isolation-py.mjs`'s `ALLOWED_EDGES` allows `meeting_api → admin_api`
verbatim for "shared SQLAlchemy models — admin_api.schema.models is the DB source-of-truth", so this
file takes that edge and compares the two live model modules directly — a stronger proof than a
frozen seal snapshot, and it fails immediately (not at the next `pnpm seal:schema`) if a future edit
touches one mirror and not the other.

Both modules import SQLAlchemy at class-definition time. That library is pinned in this
package's `pyproject.toml` at the image's version (`sqlalchemy[asyncio]==2.0.36`), so the
python gate can import them. `admin_api`'s `src` is reached via the `pythonpath` entry in
that same file (the same wiring style `gateway/services/conformance` already uses to reach
`meeting_api`'s `src`).
"""

from __future__ import annotations

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy", reason="see this module's docstring")

from admin_api.schema import models as admin_models  # noqa: E402
from meeting_api.sessions import models as mirror_models  # noqa: E402

MIRRORED_TABLES = (
    "meetings",
    "meeting_entries",
    "meeting_aw_state",
    "webhook_subscriptions",
    "webhook_outbox",
    "webhook_deliveries",
    "webhook_delivery_attempts",
    "sweep_item_failures",
)


def _fk_shape(column):
    fks = list(column.foreign_keys)
    if not fks:
        return None
    fk = fks[0]
    return (fk.column.table.name, fk.column.name, fk.ondelete)


def _index_shape(index):
    return (
        tuple(c.name if hasattr(c, "name") else str(c) for c in index.columns)
        or tuple(str(e) for e in index.expressions),
        index.unique,
        str(index.dialect_options["postgresql"].get("where")),
        index.dialect_options["postgresql"].get("using"),
    )


def _unique_constraint_shape(table):
    return {
        tuple(c.name for c in uc.columns)
        for uc in table.constraints
        if uc.__class__.__name__ == "UniqueConstraint"
    }


@pytest.mark.parametrize("table_name", MIRRORED_TABLES)
def test_table_columns_match(table_name):
    admin_t = admin_models.Base.metadata.tables[table_name]
    mirror_t = mirror_models.Base.metadata.tables[table_name]

    admin_cols = {c.name: (type(c.type).__name__, c.nullable) for c in admin_t.columns}
    mirror_cols = {
        c.name: (type(c.type).__name__, c.nullable) for c in mirror_t.columns
    }
    assert mirror_cols == admin_cols


@pytest.mark.parametrize("table_name", MIRRORED_TABLES)
def test_table_foreign_keys_match(table_name):
    admin_t = admin_models.Base.metadata.tables[table_name]
    mirror_t = mirror_models.Base.metadata.tables[table_name]

    for col in admin_t.columns:
        assert _fk_shape(mirror_t.c[col.name]) == _fk_shape(col), col.name


@pytest.mark.parametrize("table_name", MIRRORED_TABLES)
def test_table_unique_constraints_match(table_name):
    admin_t = admin_models.Base.metadata.tables[table_name]
    mirror_t = mirror_models.Base.metadata.tables[table_name]
    assert _unique_constraint_shape(mirror_t) == _unique_constraint_shape(admin_t)


@pytest.mark.parametrize("table_name", MIRRORED_TABLES)
def test_table_indexes_match(table_name):
    admin_t = admin_models.Base.metadata.tables[table_name]
    mirror_t = mirror_models.Base.metadata.tables[table_name]

    admin_idx = {i.name: _index_shape(i) for i in admin_t.indexes}
    mirror_idx = {i.name: _index_shape(i) for i in mirror_t.indexes}
    assert mirror_idx == admin_idx


def test_no_extra_or_missing_mirrored_tables():
    admin_names = {t for t in admin_models.Base.metadata.tables if t in MIRRORED_TABLES}
    mirror_names = set(mirror_models.Base.metadata.tables)
    # meeting-api's mirror is self-contained (§1.2's "mirror them all") — every intake/webhook
    # table plus the pre-existing meetings/transcriptions/meeting_sessions trio, nothing from the
    # identity-only tables (users/api_tokens/platform_settings).
    assert mirror_names == {
        "meetings",
        "transcriptions",
        "meeting_sessions",
        "meeting_entries",
        "meeting_aw_state",
        "webhook_subscriptions",
        "webhook_outbox",
        "webhook_deliveries",
        "webhook_delivery_attempts",
        "sweep_item_failures",
    }
    assert admin_names == set(MIRRORED_TABLES)
