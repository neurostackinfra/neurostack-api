"""add immutable interaction provenance to events

Revision ID: 018
Revises: 017
Create Date: 2026-09-04 00:00:01
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "018"
down_revision: Union[str, None] = "017"
branch_labels = None
depends_on = None

_CHECK_NAME = "ck_events_interaction_origin"
_INDEX_NAME = "idx_events_tenant_origin_type_created"
_TRIGGER_NAME = "trg_events_interaction_origin_immutable"
_FUNCTION_NAME = "prevent_events_interaction_origin_change"


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column(
            "interaction_origin",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
    )
    op.create_check_constraint(
        _CHECK_NAME,
        "events",
        "interaction_origin IN ('production', 'simulator', 'unknown')",
    )
    op.execute(
        f"""
        CREATE INDEX {_INDEX_NAME}
        ON events (tenant_id, interaction_origin, event_type, created_at DESC)
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION {_FUNCTION_NAME}()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            IF NEW.interaction_origin IS DISTINCT FROM OLD.interaction_origin THEN
                RAISE EXCEPTION 'events.interaction_origin is immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_TRIGGER_NAME}
        BEFORE UPDATE OF interaction_origin ON events
        FOR EACH ROW
        EXECUTE FUNCTION {_FUNCTION_NAME}()
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME} ON events")
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION_NAME}()")
    op.drop_index(_INDEX_NAME, table_name="events")
    op.drop_constraint(_CHECK_NAME, "events", type_="check")
    op.drop_column("events", "interaction_origin")
