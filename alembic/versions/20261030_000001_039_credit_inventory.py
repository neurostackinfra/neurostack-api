"""credit inventory, allocations and media egress deliveries

Revision ID: 039
Revises: 038
Create Date: 2026-10-30 00:00:01

neurostack-credit-system Group 4a: the durable inventory and allocation of
the RAG and media reservations, PostgreSQL only.  Nothing that existed at 038
is altered: no column, constraint, index, trigger, type, function or row of
an existing table changes.  The only side effect on an existing object is
the internal RI triggers PostgreSQL itself adds on ``credit_accounts`` and
``credit_effects`` (both 038 credit tables) for the two foreign keys of
``credit_allocations``.  No foreign key references a table outside the
credit schema.

Tables (``CREATED_TABLES``), all append-only by the 038 trigger function
``reject_credit_history_mutation()``:

* ``credit_inventory_versions`` -- one row per change of one stored object
  (a knowledge document's characters, a media asset's bytes), versioned per
  ``(tenant_id, category, object_id)`` and carrying the builder the object
  was counted under (``units = 0``: the object left that builder).  Written
  by the readback under the builder's account lock; no foreign key, so it
  survives ``DELETE /clinics`` and a reparent.  No tenant/builder trigger:
  the readback writes rows of every tenant of the builder, and locking a
  sibling tenant after the account would invert the lock order.
* ``credit_allocations`` -- what each accounting window of a builder covers
  per category (``rag``, ``media``, ``egress``): the opening (a free
  ``baseline`` in the window open at ``credit_enforcement_at``, else the
  ``maintenance``), the ``reservation`` of new content, RAG ``reprocessing``
  and the media ``egress`` overage.  ``target_mc`` is the aggregate target
  once rounded, ``charged_mc`` the expected value of the row's effect (the
  ledger debit plus debt of ``effect_id``, zero and no effect when the
  coverage already covered it); ``UNIQUE(builder_id, source_key)`` makes a
  retry idempotent and one partial unique index allows one opening per
  window and category.
* ``credit_egress_deliveries`` -- one row per served
  ``GET /v1/media-assets/{media_id}/content`` (server-generated
  ``delivery_id``), observational or enforced: the window's egress usage.
  Checked against the tenant's builder by the 038 function
  ``check_credit_tenant_builder()`` (the tenant row is locked first anyway).

Downgrade (``downgrade_preflight()``): ``LOCK_TIMEOUT``, then one ``DO``
block that locks the 039 tables in ``ACCESS EXCLUSIVE`` mode and refuses
with SQLSTATE 55000, changing nothing, when the revision is not exactly 039
or when ANY 039 table has a row.  Only with all of them empty does it drop
exactly the 039 tables (no ``CASCADE``).

Lock wait (both directions): the first statement is ``LOCK_TIMEOUT`` (``SET
LOCAL lock_timeout = '5s'``, as 036, 037 and 038).
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "039"
down_revision: Union[str, None] = "038"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"

CREATED_TABLES = (
    "credit_inventory_versions",
    "credit_allocations",
    "credit_egress_deliveries",
)
APPEND_ONLY_TABLES = CREATED_TABLES
TENANT_BUILDER_CHECKED_TABLES = ("credit_egress_deliveries",)

_SHA256 = "'^[0-9a-f]{64}$'"


def _execute(statements: tuple[str, ...]) -> None:
    for statement in statements:
        ddl = sa.text(statement)
        if ddl._bindparams:
            bind_names = ", ".join(sorted(ddl._bindparams))
            raise ValueError(
                f"raw migration DDL contains bind parameters: {bind_names}"
            )
        op.execute(ddl)


TABLES = (
    """
    CREATE TABLE credit_inventory_versions (
        id UUID PRIMARY KEY,
        builder_id UUID NULL,
        tenant_id UUID NOT NULL,
        category VARCHAR(10) NOT NULL,
        object_id UUID NOT NULL,
        version INTEGER NOT NULL,
        units BIGINT NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_inventory_versions_category CHECK (
            category IN ('rag', 'media')
        ),
        CONSTRAINT ck_credit_inventory_versions_version CHECK (version > 0),
        CONSTRAINT ck_credit_inventory_versions_units CHECK (units >= 0),
        CONSTRAINT uq_credit_inventory_versions_object_version
            UNIQUE (tenant_id, category, object_id, version)
    )
    """,
    "CREATE INDEX ix_credit_inventory_versions_builder "
    "ON credit_inventory_versions (builder_id, category)",
    f"""
    CREATE TABLE credit_allocations (
        id UUID PRIMARY KEY,
        builder_id UUID NOT NULL,
        account_id UUID NOT NULL,
        window_start DATE NOT NULL,
        window_end DATE NOT NULL,
        category VARCHAR(10) NOT NULL,
        allocation_kind VARCHAR(20) NOT NULL,
        source_key VARCHAR(512) NOT NULL,
        units BIGINT NOT NULL,
        target_mc BIGINT NOT NULL,
        charged_mc BIGINT NOT NULL,
        effect_id VARCHAR(512) NULL,
        snapshot_digest VARCHAR(64) NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_allocations_category CHECK (
            category IN ('rag', 'media', 'egress')
        ),
        CONSTRAINT ck_credit_allocations_kind CHECK (
            (category = 'rag' AND allocation_kind IN
                ('baseline', 'maintenance', 'reservation', 'reprocessing'))
            OR (category = 'media' AND allocation_kind IN
                ('baseline', 'maintenance', 'reservation'))
            OR (category = 'egress' AND allocation_kind = 'egress')
        ),
        CONSTRAINT ck_credit_allocations_window CHECK (window_start < window_end),
        CONSTRAINT ck_credit_allocations_amounts CHECK (
            units >= 0 AND target_mc >= 0 AND charged_mc >= 0
            AND charged_mc <= target_mc
        ),
        CONSTRAINT ck_credit_allocations_effect CHECK (
            (charged_mc = 0 AND effect_id IS NULL)
            OR (charged_mc > 0 AND effect_id IS NOT NULL)
        ),
        CONSTRAINT ck_credit_allocations_baseline_free CHECK (
            allocation_kind <> 'baseline' OR charged_mc = 0
        ),
        CONSTRAINT ck_credit_allocations_source_key CHECK (length(source_key) > 0),
        CONSTRAINT ck_credit_allocations_snapshot_digest CHECK (
            snapshot_digest IS NULL OR snapshot_digest ~ {_SHA256}
        ),
        CONSTRAINT fk_credit_allocations_account
            FOREIGN KEY (builder_id, account_id)
            REFERENCES credit_accounts(builder_id, id) ON DELETE RESTRICT,
        CONSTRAINT fk_credit_allocations_effect
            FOREIGN KEY (builder_id, effect_id)
            REFERENCES credit_effects(builder_id, effect_id) ON DELETE RESTRICT,
        CONSTRAINT uq_credit_allocations_source UNIQUE (builder_id, source_key),
        CONSTRAINT uq_credit_allocations_effect UNIQUE (builder_id, effect_id)
    )
    """,
    "CREATE INDEX ix_credit_allocations_builder_window "
    "ON credit_allocations (builder_id, window_start, category)",
    "CREATE UNIQUE INDEX uq_credit_allocations_window_opening "
    "ON credit_allocations (builder_id, window_start, category) "
    "WHERE allocation_kind IN ('baseline', 'maintenance')",
    """
    CREATE TABLE credit_egress_deliveries (
        id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        builder_id UUID NULL,
        media_id UUID NOT NULL,
        delivery_id UUID NOT NULL,
        window_start DATE NOT NULL,
        bytes BIGINT NOT NULL,
        mode VARCHAR(20) NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_egress_deliveries_bytes CHECK (bytes > 0),
        CONSTRAINT ck_credit_egress_deliveries_mode CHECK (
            mode IN ('observational', 'enforced')
        ),
        CONSTRAINT uq_credit_egress_deliveries_delivery UNIQUE (delivery_id)
    )
    """,
    "CREATE INDEX ix_credit_egress_deliveries_builder_window "
    "ON credit_egress_deliveries (builder_id, window_start, mode)",
    "CREATE INDEX ix_credit_egress_deliveries_tenant_window "
    "ON credit_egress_deliveries (tenant_id, window_start, mode)",
)

TRIGGERS = (
    *(f"""
        CREATE TRIGGER trg_{table}_immutable
        BEFORE UPDATE OR DELETE ON {table}
        FOR EACH ROW EXECUTE FUNCTION reject_credit_history_mutation()
        """ for table in APPEND_ONLY_TABLES),
    """
    CREATE TRIGGER trg_credit_egress_deliveries_tenant_builder
    BEFORE INSERT ON credit_egress_deliveries
    FOR EACH ROW EXECUTE FUNCTION check_credit_tenant_builder()
    """,
)


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def downgrade_preflight() -> str:
    """The downgrade's refusal block (see "Downgrade" in the docstring)."""

    occupied = ",\n            ".join(
        f"({position}, {_literal(table)}, EXISTS (SELECT 1 FROM {table}))"
        for position, table in enumerate(CREATED_TABLES, start=1)
    )
    return f"""
DO $preflight$
DECLARE
    problems TEXT[] := ARRAY[]::TEXT[];
    listed TEXT;
BEGIN
    LOCK TABLE {", ".join(CREATED_TABLES)} IN ACCESS EXCLUSIVE MODE;
    SELECT string_agg(version_num, ', ' ORDER BY version_num) INTO listed
    FROM alembic_version;
    IF listed IS DISTINCT FROM '039' THEN
        problems := problems || format(
            'alembic_version is %s, not 039', coalesce(listed, 'empty'));
    END IF;
    SELECT string_agg(name, ', ' ORDER BY position) INTO listed
    FROM (VALUES
            {occupied}
    ) AS guarded(position, name, has_rows)
    WHERE has_rows;
    IF listed IS NOT NULL THEN
        problems := problems || format('tables that have rows: %s', listed);
    END IF;
    IF cardinality(problems) > 0 THEN
        RAISE EXCEPTION 'downgrade of migration 039 refused (nothing was changed): %',
            array_to_string(problems, '; ')
            USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
END
$preflight$
"""


# No CASCADE: an object the preflight did not foresee makes the statement
# fail (nothing changed) instead of vanishing.
DOWNGRADE_DROPS = (f"DROP TABLE {', '.join(reversed(CREATED_TABLES))}",)


def upgrade() -> None:
    _execute((LOCK_TIMEOUT, *TABLES, *TRIGGERS))


def downgrade() -> None:
    _execute((LOCK_TIMEOUT, downgrade_preflight(), *DOWNGRADE_DROPS))
