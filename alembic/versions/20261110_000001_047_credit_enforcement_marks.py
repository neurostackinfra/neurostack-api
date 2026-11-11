"""credit enforcement marks

Revision ID: 047
Revises: 046
Create Date: 2026-11-10 00:00:01

The bridge release of cobranca-credito-por-builder (Group 1, PR-A): the
table of the per-builder enforcement marks, with no code that writes it and
no request path that reads it yet.  It only adds:

* the table ``credit_enforcement_marks`` (``CREATED_TABLES``), one row per
  operator event: ``builder_id``, ``kind`` ``mark`` or ``unmark``,
  ``occurred_at``, ``actor_user_id`` and a non-blank ``reason`` of at most
  500 characters; a builder's state is its latest event, so
  ``mark -> unmark -> mark`` is three rows.  Like every credit history
  table (038), it keeps ``builder_id`` without a foreign key to
  ``builders`` (nor ``actor_user_id`` to users): a builder with marks can
  still be deleted and its marks stay, and the writer of a later release
  checks that the builder exists in its own transaction;
* the index ``ix_credit_enforcement_marks_builder_occurred`` on
  ``(builder_id, occurred_at)``, the latest event of one builder;
* the append-only trigger every credit history table uses
  (``reject_credit_history_mutation()``, migration 038): UPDATE and DELETE
  are refused with SQLSTATE 23000.

No existing table, column, constraint, trigger or row changes, and no
existing table is referenced.  The ORM of the
bridge image describes this table (the drift test needs it) but nothing
writes a mark, and the one reader is the read-only operator command
``python -m src.ops.credit_charge_mark status``, behind the revision gate
``ENFORCEMENT_MARK_SCHEMA_MIN_REVISION`` (047); ``CREDIT_SCHEMA_MIN_REVISION``
stays 042, so the image serves on ``046`` and on ``047`` alike.

No migration window of its own (cobranca-credito-por-builder Decision 14):
it creates one table and rewrites nothing.  The real production applies it
with its own one-off migrations task once the bridge image serves, with the
SELECT-only reads ``scripts/migration_047_preflight.sql`` (at 046) and
``scripts/migration_047_post_read.sql`` (at 047), and a snapshot
``pre-047-<date>`` first (``docs/runbooks/asaas-deployment.md``, "Migration
047").

Locks (``pg_locks`` read inside the upgrade's transaction on PostgreSQL 16).
``lock_timeout`` (5 s, the first statement) bounds each lock WAIT, not a
statement or the run.  The migration's own statements lock no table or
index of the schema that existed before them: every such lock (ACCESS
EXCLUSIVE included) is on an object the upgrade itself creates (the table,
its primary key, its index and its TOAST table), besides the ACCESS SHARE
every DDL takes on system catalogs (``pg_class``); the only lock on an
existing relation of the schema is the ROW EXCLUSIVE of Alembic's
``alembic_version`` update.  No existing table is locked, scanned or
rewritten, so no reader or writer of one waits for it (an open writer of
``builders``, or a session holding ACCESS EXCLUSIVE on it, does not delay
the run).

Downgrade (``downgrade_preflight()``): ``LOCK_TIMEOUT``, then one ``DO``
block that locks ``credit_enforcement_marks`` in ACCESS EXCLUSIVE mode and
refuses with SQLSTATE 55000, changing nothing, when the revision is not
exactly 047 or while any mark row exists: credit history is never discarded
by a downgrade.  It is a dev/test guard; production never runs an Alembic
downgrade (the way back is a forward fix or, by owner decision, the
snapshot).
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "047"
down_revision: Union[str, None] = "046"
branch_labels = None
depends_on = None

# First statement of both directions; see "Locks" in the module docstring.
LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"
# What the upgrade adds, read by the preflight test
# (tests/integration/test_credit_enforcement_marks_migration_postgres.py
# keeps scripts/migration_047_preflight.sql equal to these).
CREATED_TABLES = ("credit_enforcement_marks",)
# The existing tables the upgrade references: none (no foreign key, see the
# module docstring), so it locks no existing table.
REFERENCED_TABLES: tuple[str, ...] = ()
DOWNGRADE_LOCKED = CREATED_TABLES
MARK_KINDS = ("mark", "unmark")
REASON_MAX_LENGTH = 500


def _execute(statements: tuple[str, ...]) -> None:
    for statement in statements:
        ddl = sa.text(statement)
        if ddl._bindparams:
            raise ValueError("raw migration DDL contains bind parameters")
        op.execute(ddl)


TABLES = (
    f"""
    CREATE TABLE credit_enforcement_marks (
        id UUID PRIMARY KEY,
        builder_id UUID NOT NULL,
        kind VARCHAR(20) NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL,
        actor_user_id UUID NOT NULL,
        reason VARCHAR({REASON_MAX_LENGTH}) NOT NULL,
        CONSTRAINT ck_credit_enforcement_marks_kind CHECK (
            kind IN ('mark', 'unmark')
        ),
        CONSTRAINT ck_credit_enforcement_marks_reason CHECK (
            length(trim(reason)) > 0
        )
    )
    """,
    "CREATE INDEX ix_credit_enforcement_marks_builder_occurred "
    "ON credit_enforcement_marks (builder_id, occurred_at)",
)

TRIGGERS = (
    """
    CREATE TRIGGER trg_credit_enforcement_marks_immutable
    BEFORE UPDATE OR DELETE ON credit_enforcement_marks
    FOR EACH ROW EXECUTE FUNCTION reject_credit_history_mutation()
    """,
)


def downgrade_preflight() -> str:
    """The downgrade's refusal block (see "Downgrade" in the docstring).

    The shape of 046: the table is locked ACCESS EXCLUSIVE first (a mark
    being written cannot slip in behind the check; a conflicting lock gives
    up after ``lock_timeout``), and every ``alembic_version`` row is read,
    so an empty table or a second row is a refusal too.
    """

    return f"""
DO $preflight$
DECLARE
    problems TEXT[] := ARRAY[]::TEXT[];
    listed TEXT;
BEGIN
    LOCK TABLE {", ".join(DOWNGRADE_LOCKED)} IN ACCESS EXCLUSIVE MODE;
    SELECT string_agg(version_num, ', ' ORDER BY version_num) INTO listed
    FROM alembic_version;
    IF listed IS DISTINCT FROM '047' THEN
        problems := problems || format(
            'alembic_version is %s, not 047', coalesce(listed, 'empty'));
    END IF;
    IF EXISTS (SELECT 1 FROM credit_enforcement_marks) THEN
        problems := problems || 'credit enforcement marks exist'::TEXT;
    END IF;
    IF cardinality(problems) > 0 THEN
        RAISE EXCEPTION 'downgrade of migration 047 refused (nothing was changed): %',
            array_to_string(problems, '; ')
            USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
END
$preflight$
"""


# No CASCADE: an object the preflight did not foresee makes the statement
# fail (nothing changed) instead of vanishing.  The table takes its index
# and trigger with it.
DOWNGRADE_STATEMENTS = ("DROP TABLE credit_enforcement_marks",)


def upgrade() -> None:
    _execute((LOCK_TIMEOUT, *TABLES, *TRIGGERS))


def downgrade() -> None:
    _execute((LOCK_TIMEOUT, downgrade_preflight(), *DOWNGRADE_STATEMENTS))
