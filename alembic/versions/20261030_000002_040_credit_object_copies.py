"""Additive credit object-copy registry and durable workers (Group 4b).

Revision ID: 040
Revises: 039

The source of truth remains PostgreSQL ``media_assets.payload`` and
``knowledge_documents.content``.  These tables only record an additional,
rebuildable copy and work items; none has a foreign key to a source object so
that a source CASCADE cannot erase the information required for collection.
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "040"
down_revision: Union[str, None] = "039"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"
CREATED_TABLES = (
    "credit_object_copies",
    "credit_object_copy_jobs",
    "credit_qdrant_jobs",
)


def _execute(statements: tuple[str, ...]) -> None:
    for statement in statements:
        ddl = sa.text(statement)
        if ddl._bindparams:
            raise ValueError("raw migration DDL contains bind parameters")
        op.execute(ddl)


TABLES = (
    """
    CREATE TABLE credit_object_copies (
        id UUID PRIMARY KEY,
        source_kind VARCHAR(16) NOT NULL,
        source_id UUID NOT NULL,
        tenant_id UUID NOT NULL,
        builder_id UUID NULL,
        object_key VARCHAR(1024) NOT NULL,
        version_id VARCHAR(512) NOT NULL,
        etag VARCHAR(512) NOT NULL,
        content_sha256 VARCHAR(64) NOT NULL,
        state VARCHAR(16) NOT NULL DEFAULT 'ready',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_object_copies_source CHECK (source_kind IN ('media', 'knowledge')),
        CONSTRAINT ck_credit_object_copies_state CHECK (state IN ('ready', 'deleted', 'held', 'failed')),
        CONSTRAINT ck_credit_object_copies_sha CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
        CONSTRAINT uq_credit_object_copies_source UNIQUE (source_kind, source_id, content_sha256),
        CONSTRAINT uq_credit_object_copies_version UNIQUE (object_key, version_id)
    )
    """,
    "CREATE INDEX ix_credit_object_copies_tenant_source ON credit_object_copies (tenant_id, source_kind, source_id)",
    """
    CREATE TABLE credit_object_copy_jobs (
        id UUID PRIMARY KEY,
        copy_id UUID NULL REFERENCES credit_object_copies(id) ON DELETE RESTRICT,
        source_kind VARCHAR(16) NOT NULL,
        source_id UUID NOT NULL,
        tenant_id UUID NOT NULL,
        operation VARCHAR(16) NOT NULL,
        state VARCHAR(16) NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error VARCHAR(500) NULL,
        claimed_at TIMESTAMPTZ NULL,
        completed_at TIMESTAMPTZ NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_object_copy_jobs_source CHECK (source_kind IN ('media', 'knowledge')),
        CONSTRAINT ck_credit_object_copy_jobs_operation CHECK (operation IN ('copy', 'gc')),
        CONSTRAINT ck_credit_object_copy_jobs_state CHECK (state IN ('pending', 'running', 'completed', 'held', 'failed')),
        CONSTRAINT ck_credit_object_copy_jobs_attempts CHECK (attempts >= 0),
        CONSTRAINT uq_credit_object_copy_jobs_active UNIQUE (source_kind, source_id, operation, state)
    )
    """,
    "CREATE INDEX ix_credit_object_copy_jobs_claim ON credit_object_copy_jobs (state, created_at)",
    """
    CREATE TABLE credit_qdrant_jobs (
        id UUID PRIMARY KEY,
        source_kind VARCHAR(16) NOT NULL,
        source_id UUID NOT NULL,
        tenant_id UUID NOT NULL,
        operation VARCHAR(16) NOT NULL,
        state VARCHAR(16) NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error VARCHAR(500) NULL,
        claimed_at TIMESTAMPTZ NULL,
        completed_at TIMESTAMPTZ NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_qdrant_jobs_source CHECK (source_kind IN ('media', 'knowledge')),
        CONSTRAINT ck_credit_qdrant_jobs_operation CHECK (operation IN ('rebuild', 'delete')),
        CONSTRAINT ck_credit_qdrant_jobs_state CHECK (state IN ('pending', 'running', 'completed', 'failed')),
        CONSTRAINT ck_credit_qdrant_jobs_attempts CHECK (attempts >= 0),
        CONSTRAINT uq_credit_qdrant_jobs_active UNIQUE (source_kind, source_id, operation, state)
    )
    """,
    "CREATE INDEX ix_credit_qdrant_jobs_claim ON credit_qdrant_jobs (state, created_at)",
)


def downgrade_preflight() -> str:
    occupied = ",\n            ".join(
        f"({index}, '{name}', EXISTS (SELECT 1 FROM {name}))"
        for index, name in enumerate(CREATED_TABLES, start=1)
    )
    return f"""
DO $preflight$
DECLARE problems TEXT[] := ARRAY[]::TEXT[]; listed TEXT;
BEGIN
    LOCK TABLE {", ".join(CREATED_TABLES)} IN ACCESS EXCLUSIVE MODE;
    SELECT string_agg(version_num, ', ' ORDER BY version_num) INTO listed FROM alembic_version;
    IF listed IS DISTINCT FROM '040' THEN
        problems := problems || format('alembic_version is %s, not 040', coalesce(listed, 'empty'));
    END IF;
    SELECT string_agg(name, ', ' ORDER BY position) INTO listed
      FROM (VALUES {occupied}) AS guarded(position, name, has_rows) WHERE has_rows;
    IF listed IS NOT NULL THEN problems := problems || format('tables that have rows: %s', listed); END IF;
    IF cardinality(problems) > 0 THEN
        RAISE EXCEPTION 'downgrade of migration 040 refused (nothing was changed): %', array_to_string(problems, '; ')
          USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
END $preflight$
"""


def upgrade() -> None:
    _execute((LOCK_TIMEOUT, *TABLES))


def downgrade() -> None:
    _execute(
        (
            LOCK_TIMEOUT,
            downgrade_preflight(),
            f"DROP TABLE {', '.join(reversed(CREATED_TABLES))}",
        )
    )
