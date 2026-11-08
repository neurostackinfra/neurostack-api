"""media trigger intents and session media deliveries

Revision ID: 045
Revises: 044
Create Date: 2026-11-07 00:00:02

The bridge release of envio-midia-agente (Group 1): expand only, and no ORM
model maps either object yet.  It adds ``media_assets.trigger_intents``
(``TEXT[] NOT NULL DEFAULT '{}'``: every existing row reads the empty list,
and an insert that does not name the column gets it too, so the image that
serves before the window keeps working on this schema) and the table
``media_deliveries``, one row per (session, media asset) the agent sent,
deleted with its tenant, its session or its media asset by the foreign keys'
cascades.  It alters no ASAAS or credit object.

Production applies it in migration window ``045`` (``scripts/
asaas_migration_window.py --window 045``) after the bridge image serves, and
that window also applies ``043`` and ``044`` (the media category/MIME
invariant widened for ``audio/ogg`` and ``audio/mp4``), which have no window
of their own: ``alembic upgrade 045`` from ``042``.  The behaviour release
that maps both objects is merged and deployed only after that window.

Locks.  ``alembic/env.py`` does not set ``transaction_per_migration``, so the
whole run from ``042`` is ONE transaction: every lock below is held until its
commit, and any failure rolls back ``043`` and ``044`` with ``045`` (the
schema stays at ``042``, nothing changed).  ``lock_timeout`` (5 s, ``SET
LOCAL`` by each of ``043``, ``044`` and this one) bounds each lock WAIT, not
a statement or the run, so the waits add up:

1. ``043`` takes ACCESS EXCLUSIVE on ``media_assets`` (wait up to 5 s; while
   it waits, every later reader and writer of ``media_assets`` queues behind
   it) and validates its new CHECK by scanning the table; ``044`` scans it
   again under the same lock.
2. Here the column is a catalog-only change (a constant default: PostgreSQL
   11 and later do not rewrite the table) under that same lock.
3. ``CREATE TABLE media_deliveries`` takes SHARE ROW EXCLUSIVE on
   ``tenants``, then on ``sessions`` (each a wait of up to 5 s behind their
   writers), still holding ACCESS EXCLUSIVE on ``media_assets``.

Worst case, ``media_assets`` is unreadable and unwritable for about 15 s plus
the two scans and the DDL, and writes to ``tenants`` and ``sessions`` wait
from their lock request to the commit; past any single 5 s wait the run gives
up with SQLSTATE 55P03 and nothing changed.
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "045"
down_revision: Union[str, None] = "044"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"
# What the upgrade adds, read by the window's preflight test
# (tests/scripts/test_asaas_migration_window.py keeps
# scripts/migration_043_045_preflight.sql equal to these).
ADDED_COLUMNS = (("media_assets", "trigger_intents"),)
CREATED_TABLES = ("media_deliveries",)


def _execute(statements: tuple[str, ...]) -> None:
    for statement in statements:
        ddl = sa.text(statement)
        if ddl._bindparams:
            raise ValueError("raw migration DDL contains bind parameters")
        op.execute(ddl)


# A constant default: PostgreSQL 11 and later store it in the catalog and do
# not rewrite media_assets.
COLUMNS = (
    "ALTER TABLE media_assets "
    "ADD COLUMN trigger_intents TEXT[] NOT NULL DEFAULT '{}'",
)

TABLES = (
    """
    CREATE TABLE media_deliveries (
        id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        session_id UUID NOT NULL,
        media_id UUID NOT NULL,
        last_sent_at TIMESTAMPTZ NOT NULL,
        CONSTRAINT fk_media_deliveries_tenant
            FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
        CONSTRAINT fk_media_deliveries_session
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
        CONSTRAINT fk_media_deliveries_media
            FOREIGN KEY (media_id) REFERENCES media_assets(id) ON DELETE CASCADE,
        CONSTRAINT uq_media_deliveries_session_media UNIQUE (session_id, media_id)
    )
    """,
    # The unique constraint's index leads with session_id; deleting a media
    # asset finds its deliveries through this one.
    "CREATE INDEX ix_media_deliveries_media ON media_deliveries (media_id)",
    # Deleting a tenant cascades through tenant_id: indexed, as every other
    # tenant-scoped session child (events, handoffs, sessions) indexes it.
    "CREATE INDEX ix_media_deliveries_tenant ON media_deliveries (tenant_id)",
)


def downgrade_preflight() -> str:
    """The downgrade's refusal block (SQLSTATE 55000, nothing changed).

    The shape of 042: both tables are locked ACCESS EXCLUSIVE first (a
    delivery or a binding being written cannot slip in behind the check; a
    conflicting lock gives up after ``lock_timeout``), and every
    ``alembic_version`` row is read, so an empty table or a second row is a
    refusal too.  The downgrade would lose data when ``media_deliveries``
    holds a row or a media asset is bound to an intent, so either refuses.
    """

    return """
DO $preflight$
DECLARE
    problems TEXT[] := ARRAY[]::TEXT[];
    listed TEXT;
BEGIN
    LOCK TABLE media_deliveries, media_assets IN ACCESS EXCLUSIVE MODE;
    SELECT string_agg(version_num, ', ' ORDER BY version_num) INTO listed
    FROM alembic_version;
    IF listed IS DISTINCT FROM '045' THEN
        problems := problems || format(
            'alembic_version is %s, not 045', coalesce(listed, 'empty'));
    END IF;
    IF EXISTS (SELECT 1 FROM media_deliveries) THEN
        problems := problems || format(
            'tables that have rows: %s', 'media_deliveries');
    END IF;
    IF EXISTS (SELECT 1 FROM media_assets WHERE cardinality(trigger_intents) > 0)
    THEN
        problems := problems || format(
            'columns that hold values: %s', 'media_assets.trigger_intents');
    END IF;
    IF cardinality(problems) > 0 THEN
        RAISE EXCEPTION 'downgrade of migration 045 refused (nothing was changed): %',
            array_to_string(problems, '; ')
            USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
END
$preflight$
"""


def upgrade() -> None:
    _execute((LOCK_TIMEOUT, *COLUMNS, *TABLES))


def downgrade() -> None:
    _execute(
        (
            LOCK_TIMEOUT,
            downgrade_preflight(),
            "DROP TABLE media_deliveries",
            "ALTER TABLE media_assets DROP COLUMN trigger_intents",
        )
    )
