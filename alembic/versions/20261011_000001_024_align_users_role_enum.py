"""align users.role with the userrole enum

Revision ID: 024
Revises: 023
Create Date: 2026-10-11 00:00:01

Migration 004 created ``users.role`` as VARCHAR(50) while the model maps the
PG enum ``userrole``. On databases built by Alembic, ORM comparisons render
``users.role = $1::userrole`` and Postgres rejects them (``operator does not
exist: character varying = userrole``). This converts the column to the enum.
Databases whose column is already the enum (created from the models) are left
untouched. Downgrade restores migration 004's VARCHAR(50).
"""

from typing import Union

import sqlalchemy as sa

from alembic import context, op

revision: str = "024"
down_revision: Union[str, None] = "023"
branch_labels = None
depends_on = None

TO_ENUM = "ALTER TABLE users ALTER COLUMN role TYPE userrole USING role::userrole"
TO_VARCHAR = "ALTER TABLE users ALTER COLUMN role TYPE VARCHAR(50) USING role::text"


def _role_column_type() -> str:
    return str(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT udt_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'users' AND column_name = 'role'"
            )
        )
        .scalar_one()
    )


def upgrade() -> None:
    if context.is_offline_mode():
        # Offline SQL targets a database built by this migration chain (VARCHAR).
        op.execute(TO_ENUM)
        return
    if _role_column_type() == "userrole":
        return

    bind = op.get_bind()
    labels = set(
        bind.execute(
            sa.text(
                "SELECT e.enumlabel FROM pg_enum e "
                "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = 'userrole'"
            )
        ).scalars()
    )
    values = set(bind.execute(sa.text("SELECT DISTINCT role FROM users")).scalars())
    unknown = sorted(values - labels)
    if unknown:
        raise RuntimeError(
            f"users.role has values outside the userrole enum {sorted(labels)}: "
            f"{unknown}. Correct those rows before upgrading."
        )
    op.execute(TO_ENUM)


def downgrade() -> None:
    if context.is_offline_mode():
        op.execute(TO_VARCHAR)
        return
    if _role_column_type() != "userrole":
        return
    op.execute(TO_VARCHAR)
