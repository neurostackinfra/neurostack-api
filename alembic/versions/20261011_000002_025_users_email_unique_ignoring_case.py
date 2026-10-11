"""make users.email unique ignoring case

Revision ID: 025
Revises: 024
Create Date: 2026-10-11 00:00:02

``uq_users_email`` compares case-sensitively, so the admin and builder routes
could create ``Ops@example.com`` next to ``ops@example.com``: two accounts for
what people read as one email. This adds a unique index on ``lower(email)``.
The upgrade refuses, without changes, while case-only duplicates exist; they
must be renamed or merged first. Emails are not printed (they are personal
data); the error carries the query that lists them.
"""

from typing import Union

import sqlalchemy as sa

from alembic import context, op

revision: str = "025"
down_revision: Union[str, None] = "024"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_users_email_lower"
DUPLICATES_QUERY = (
    "SELECT lower(email) AS email, count(*) FROM users "
    "GROUP BY lower(email) HAVING count(*) > 1"
)


def upgrade() -> None:
    if not context.is_offline_mode():
        duplicates = op.get_bind().execute(sa.text(DUPLICATES_QUERY)).all()
        if duplicates:
            raise RuntimeError(
                f"{len(duplicates)} email(s) belong to more than one user when "
                f"case is ignored; rename or merge them before upgrading. "
                f"List them with: {DUPLICATES_QUERY}"
            )
    op.create_index(INDEX_NAME, "users", [sa.text("lower(email)")], unique=True)


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="users")
