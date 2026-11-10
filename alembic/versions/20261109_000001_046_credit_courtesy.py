"""credit courtesy lots, removals and events

Revision ID: 046
Revises: 045
Create Date: 2026-11-09 00:00:01

The bridge release of creditos-cortesia (Group 1, PR-A): the schema of a
super admin's courtesy credits, with no code that writes or reads it yet.
It only widens rules and adds:

* ``credit_lots``: ``ck_credit_lots_kind`` accepts ``courtesy``, and
  ``ck_credit_lots_source`` a third branch for it: no ``subscription_id``,
  ``order_id``, ``cycle_start`` or ``cycle_end``, no ``payment_id``, and a
  ``grant_effect_id`` of the form ``courtesy:{event_id}:grant``.  The column
  ``payment_id`` loses its ``NOT NULL``, which becomes part of that CHECK:
  required for ``monthly`` and ``topup``, absent for ``courtesy``.
  ``paid_at`` stays ``NOT NULL`` (for a courtesy lot it is the civil date of
  the grant).
* ``credit_ledger_entries``: ``ck_credit_ledger_entries_type`` accepts
  ``courtesy_removal``, whose effect id is ``courtesy-removal:{event_id}``
  (it names a lot, by ``ck_credit_ledger_entries_lot``, unchanged).
* the table ``credit_courtesy_events`` (``CREATED_TABLES``), one row per
  grant or removal: builder, lot, kind, amount in millicredits (> 0), reason,
  actor, a globally unique idempotency key and the request digest.  It is
  append-only by the trigger every credit history table uses
  (``reject_credit_history_mutation()``, migration 038), and its lot is
  referenced inside one builder (``(builder_id, lot_id)``), like every
  reference between credit tables; no foreign key to ``builders`` or users.

Every row the 045 schema admits is admitted here, so no existing row can
stop the upgrade, and the only side effect on an existing object besides the
three rewritten CHECKs and the column's nullability is the pair of internal RI
triggers PostgreSQL adds on ``credit_lots`` for ``fk_credit_courtesy_events_
lot``.  The ORM of the bridge image describes this schema (the drift test
needs it) but nothing writes a courtesy lot, a ``courtesy_removal`` entry or
an event: the image serves on ``045`` and on ``046`` alike.

Production applies it in migration window ``046`` (``scripts/
asaas_migration_window.py --window 046``) after the bridge image serves; the
release that writes courtesy is deployed only after that window.

Locks.  ``lock_timeout`` (5 s, the first statement) bounds each lock WAIT,
not a statement or the run.  The ``ALTER TABLE credit_lots`` takes ACCESS
EXCLUSIVE on ``credit_lots`` (every reader and writer of lots queues behind
the request) and validates both CHECKs in one scan; the ``ALTER TABLE
credit_ledger_entries`` then takes ACCESS EXCLUSIVE on the ledger and scans
it once; ``CREATE TABLE credit_courtesy_events`` needs ``credit_lots``
again, already held.  Lots before ledger entries is the order the ledger
writers insert in, and both locks are held until the commit.  Past any
single 5 s wait the run gives up with SQLSTATE 55P03 and nothing changed.

Downgrade (``downgrade_preflight()``): ``LOCK_TIMEOUT``, then one ``DO``
block that locks ``credit_lots``, ``credit_ledger_entries`` and
``credit_courtesy_events`` in ACCESS EXCLUSIVE mode, in that order, and
refuses with SQLSTATE 55000, changing nothing, when the revision is not
exactly 046 or while any courtesy lot, ``courtesy_removal`` entry or event
exists: credit history is never discarded by a downgrade.  It is a dev/test
guard; production never runs an Alembic downgrade (the way back is a
forward fix or, by owner decision, the window's snapshot).
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "046"
down_revision: Union[str, None] = "045"
branch_labels = None
depends_on = None

# First statement of both directions; see "Locks" in the module docstring.
LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"
# What the upgrade adds and rewrites, read by the window's preflight test
# (tests/scripts/test_asaas_migration_window.py keeps
# scripts/migration_046_preflight.sql equal to these).
CREATED_TABLES = ("credit_courtesy_events",)
REWRITTEN_CHECKS = (
    ("credit_lots", "ck_credit_lots_kind"),
    ("credit_lots", "ck_credit_lots_source"),
    ("credit_ledger_entries", "ck_credit_ledger_entries_type"),
)
RELAXED_COLUMNS = (("credit_lots", "payment_id"),)
# Upgrade and downgrade lock order (lots before ledger entries, as the
# ledger writers insert).
DOWNGRADE_LOCKED = ("credit_lots", "credit_ledger_entries", *CREATED_TABLES)

# The LIKE patterns of the courtesy effect ids.  In the DDL text the colon
# before ``grant`` is written ``\:``: ``sa.text`` (and SQLAlchemy's
# ``CheckConstraint``) would read ``:grant`` as a bind parameter even inside
# a quoted literal; the escape renders as a plain colon.
COURTESY_GRANT_EFFECT_PATTERN = "courtesy:%:grant"
COURTESY_REMOVAL_EFFECT_PATTERN = "courtesy-removal:%"
_GRANT_EFFECT_LIKE = COURTESY_GRANT_EFFECT_PATTERN.replace(":grant", r"\:grant")

_SHA256 = "'^[0-9a-f]{64}$'"


def _execute(statements: tuple[str, ...]) -> None:
    for statement in statements:
        ddl = sa.text(statement)
        if ddl._bindparams:
            raise ValueError("raw migration DDL contains bind parameters")
        op.execute(ddl)


# The 045 definitions (written by migration 038), restored by the downgrade.
LOTS_KIND_045 = "lot_kind IN ('monthly', 'topup')"
LOTS_SOURCE_045 = """
            (lot_kind = 'monthly' AND subscription_id IS NOT NULL
                AND cycle_start IS NOT NULL AND cycle_end IS NOT NULL
                AND cycle_start < cycle_end AND order_id IS NULL)
            OR (lot_kind = 'topup' AND order_id IS NOT NULL
                AND subscription_id IS NULL AND cycle_start IS NULL
                AND cycle_end IS NULL)
        """
LEDGER_TYPE_045 = """
            entry_type IN ('grant', 'debit', 'expiry', 'reversal', 'debt',
                           'debt_settlement', 'debt_release',
                           'settlement_release', 'lot_regrant')
        """

# The 046 definitions (src/models/credit.py mirrors them).
LOTS_KIND_046 = "lot_kind IN ('monthly', 'topup', 'courtesy')"
LOTS_SOURCE_046 = f"""
            (lot_kind = 'monthly' AND subscription_id IS NOT NULL
                AND cycle_start IS NOT NULL AND cycle_end IS NOT NULL
                AND cycle_start < cycle_end AND order_id IS NULL
                AND payment_id IS NOT NULL)
            OR (lot_kind = 'topup' AND order_id IS NOT NULL
                AND subscription_id IS NULL AND cycle_start IS NULL
                AND cycle_end IS NULL AND payment_id IS NOT NULL)
            OR (lot_kind = 'courtesy' AND subscription_id IS NULL
                AND cycle_start IS NULL AND cycle_end IS NULL
                AND order_id IS NULL AND payment_id IS NULL
                AND grant_effect_id LIKE '{_GRANT_EFFECT_LIKE}')
        """
LEDGER_TYPE_046 = f"""
            entry_type IN ('grant', 'debit', 'expiry', 'reversal', 'debt',
                           'debt_settlement', 'debt_release',
                           'settlement_release', 'lot_regrant',
                           'courtesy_removal')
            AND (entry_type <> 'courtesy_removal'
                 OR effect_id LIKE '{COURTESY_REMOVAL_EFFECT_PATTERN}')
        """

ALTERS = (
    f"""
    ALTER TABLE credit_lots
        DROP CONSTRAINT ck_credit_lots_kind,
        DROP CONSTRAINT ck_credit_lots_source,
        ALTER COLUMN payment_id DROP NOT NULL,
        ADD CONSTRAINT ck_credit_lots_kind CHECK ({LOTS_KIND_046}),
        ADD CONSTRAINT ck_credit_lots_source CHECK ({LOTS_SOURCE_046})
    """,
    f"""
    ALTER TABLE credit_ledger_entries
        DROP CONSTRAINT ck_credit_ledger_entries_type,
        ADD CONSTRAINT ck_credit_ledger_entries_type CHECK ({LEDGER_TYPE_046})
    """,
)

TABLES = (
    f"""
    CREATE TABLE credit_courtesy_events (
        id UUID PRIMARY KEY,
        builder_id UUID NOT NULL,
        lot_id UUID NOT NULL,
        event_kind VARCHAR(20) NOT NULL,
        amount_mc BIGINT NOT NULL,
        reason VARCHAR(500) NOT NULL,
        actor_user_id UUID NOT NULL,
        idempotency_key VARCHAR(200) NOT NULL,
        request_digest VARCHAR(64) NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_courtesy_events_kind CHECK (
            event_kind IN ('grant', 'removal')
        ),
        CONSTRAINT ck_credit_courtesy_events_amount CHECK (amount_mc > 0),
        CONSTRAINT ck_credit_courtesy_events_reason CHECK (length(reason) > 0),
        CONSTRAINT ck_credit_courtesy_events_idempotency_key CHECK (
            length(idempotency_key) > 0
        ),
        CONSTRAINT ck_credit_courtesy_events_request_digest CHECK (
            request_digest ~ {_SHA256}
        ),
        CONSTRAINT fk_credit_courtesy_events_lot
            FOREIGN KEY (builder_id, lot_id)
            REFERENCES credit_lots(builder_id, id) ON DELETE RESTRICT,
        CONSTRAINT uq_credit_courtesy_events_idempotency_key
            UNIQUE (idempotency_key)
    )
    """,
    "CREATE INDEX ix_credit_courtesy_events_builder_lot "
    "ON credit_courtesy_events (builder_id, lot_id)",
)

TRIGGERS = (
    """
    CREATE TRIGGER trg_credit_courtesy_events_immutable
    BEFORE UPDATE OR DELETE ON credit_courtesy_events
    FOR EACH ROW EXECUTE FUNCTION reject_credit_history_mutation()
    """,
)


def downgrade_preflight() -> str:
    """The downgrade's refusal block (see "Downgrade" in the docstring).

    The shape of 042 and 045: the tables are locked ACCESS EXCLUSIVE first
    (a courtesy row being written cannot slip in behind the check; a
    conflicting lock gives up after ``lock_timeout``), and every
    ``alembic_version`` row is read, so an empty table or a second row is a
    refusal too.
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
    IF listed IS DISTINCT FROM '046' THEN
        problems := problems || format(
            'alembic_version is %s, not 046', coalesce(listed, 'empty'));
    END IF;
    SELECT string_agg(name, ', ' ORDER BY position) INTO listed
    FROM (VALUES
            (1, 'courtesy lots',
             EXISTS (SELECT 1 FROM credit_lots WHERE lot_kind = 'courtesy')),
            (2, 'courtesy_removal ledger entries',
             EXISTS (SELECT 1 FROM credit_ledger_entries
                     WHERE entry_type = 'courtesy_removal')),
            (3, 'credit_courtesy_events rows',
             EXISTS (SELECT 1 FROM credit_courtesy_events))
    ) AS guarded(position, name, has_rows)
    WHERE has_rows;
    IF listed IS NOT NULL THEN
        problems := problems || format('courtesy credit exists: %s', listed);
    END IF;
    IF cardinality(problems) > 0 THEN
        RAISE EXCEPTION 'downgrade of migration 046 refused (nothing was changed): %',
            array_to_string(problems, '; ')
            USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
END
$preflight$
"""


# No CASCADE: an object the preflight did not foresee makes the statement
# fail (nothing changed) instead of vanishing.  The table goes first (its
# foreign key holds RI triggers on credit_lots), then each CHECK back to its
# 045 definition; payment_id NOT NULL holds because no courtesy lot exists.
DOWNGRADE_STATEMENTS = (
    "DROP TABLE credit_courtesy_events",
    f"""
    ALTER TABLE credit_ledger_entries
        DROP CONSTRAINT ck_credit_ledger_entries_type,
        ADD CONSTRAINT ck_credit_ledger_entries_type CHECK ({LEDGER_TYPE_045})
    """,
    f"""
    ALTER TABLE credit_lots
        DROP CONSTRAINT ck_credit_lots_source,
        DROP CONSTRAINT ck_credit_lots_kind,
        ALTER COLUMN payment_id SET NOT NULL,
        ADD CONSTRAINT ck_credit_lots_kind CHECK ({LOTS_KIND_045}),
        ADD CONSTRAINT ck_credit_lots_source CHECK ({LOTS_SOURCE_045})
    """,
)


def upgrade() -> None:
    _execute((LOCK_TIMEOUT, *ALTERS, *TABLES, *TRIGGERS))


def downgrade() -> None:
    _execute((LOCK_TIMEOUT, downgrade_preflight(), *DOWNGRADE_STATEMENTS))
