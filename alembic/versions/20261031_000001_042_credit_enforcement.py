"""credit enforcement cutoff acceptances

Revision ID: 042
Revises: 041

Group 7 adds only the immutable, tenant-scoped acknowledgement used by the
server-disabled enforcement preflight.  It does not alter ASAAS objects,
rollout control, top-up capability or the existing enforcement-state row.
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "042"
down_revision: Union[str, None] = "041"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"
CREATED_TABLES = ("credit_cutoff_acceptances",)
CREATED_FUNCTIONS = ("reject_credit_cutoff_acceptance_mutation()",)


def _execute(statements: tuple[str, ...]) -> None:
    for statement in statements:
        ddl = sa.text(statement)
        if ddl._bindparams:
            raise ValueError("raw migration DDL contains bind parameters")
        op.execute(ddl)


FUNCTIONS = (
    """
    CREATE FUNCTION reject_credit_cutoff_acceptance_mutation()
    RETURNS trigger LANGUAGE plpgsql AS $function$
    BEGIN
        RAISE EXCEPTION 'credit_cutoff_acceptances is append-only'
            USING ERRCODE = 'integrity_constraint_violation';
    END;
    $function$
    """,
)

TABLES = (
    """
    CREATE TABLE credit_cutoff_acceptances (
        id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        builder_id UUID NULL,
        reason VARCHAR(500) NOT NULL,
        report_digest VARCHAR(64) NOT NULL,
        author_user_id UUID NOT NULL,
        accepted_at TIMESTAMPTZ NOT NULL,
        accepted_on DATE NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_cutoff_acceptances_reason CHECK (length(reason) > 0),
        CONSTRAINT ck_credit_cutoff_acceptances_report_digest
            CHECK (report_digest ~ '^[0-9a-f]{64}$'),
        CONSTRAINT uq_credit_cutoff_acceptances_cutoff
            UNIQUE (tenant_id, builder_id, reason, report_digest, author_user_id,
                    accepted_on)
    )
    """,
    "CREATE INDEX ix_credit_cutoff_acceptances_report "
    "ON credit_cutoff_acceptances (report_digest, tenant_id)",
)

TRIGGERS = (
    """
    CREATE TRIGGER trg_credit_cutoff_acceptances_append_only
    BEFORE UPDATE OR DELETE ON credit_cutoff_acceptances
    FOR EACH ROW EXECUTE FUNCTION reject_credit_cutoff_acceptance_mutation()
    """,
)


def downgrade_preflight() -> str:
    return """
DO $preflight$
DECLARE
    problems TEXT[] := ARRAY[]::TEXT[];
BEGIN
    IF (SELECT version_num FROM alembic_version) <> '042' THEN
        problems := problems || format('alembic_version is %s, not 042',
            (SELECT version_num FROM alembic_version));
    END IF;
    IF EXISTS (SELECT 1 FROM credit_cutoff_acceptances) THEN
        problems := problems || 'tables that have rows: credit_cutoff_acceptances';
    END IF;
    IF cardinality(problems) > 0 THEN
        RAISE EXCEPTION 'downgrade of migration 042 refused (nothing was changed): %',
            array_to_string(problems, '; ')
            USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
END
$preflight$
"""


def upgrade() -> None:
    _execute((LOCK_TIMEOUT, *FUNCTIONS, *TABLES, *TRIGGERS))


def downgrade() -> None:
    _execute(
        (
            LOCK_TIMEOUT,
            downgrade_preflight(),
            "DROP TABLE credit_cutoff_acceptances",
            "DROP FUNCTION reject_credit_cutoff_acceptance_mutation()",
        )
    )
