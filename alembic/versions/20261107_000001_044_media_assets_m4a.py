"""allow audio/mp4 (M4A) in the media asset category/MIME invariant

Revision ID: 044
Revises: 043
Create Date: 2026-11-07 00:00:01

Widening only: every row accepted by the old constraint is accepted by the new
one, so the rewrite needs no backfill. The downgrade fails by design while any
``audio/mp4`` row exists.
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "044"
down_revision: Union[str, None] = "043"
branch_labels = None
depends_on = None

CONSTRAINT = "ck_media_assets_category_mime"
_AUDIO_WITH_M4A = (
    "(category = 'audio' AND mime_type IN ('audio/mpeg', 'audio/ogg', 'audio/mp4'))"
)
_AUDIO_WITH_OGG = "(category = 'audio' AND mime_type IN ('audio/mpeg', 'audio/ogg'))"


def _invariant(audio_clause: str) -> str:
    return (
        "(category = 'video' AND mime_type = 'video/mp4') OR "
        f"{audio_clause} OR "
        "(category = 'pdf' AND mime_type = 'application/pdf') OR "
        "(category = 'image' AND mime_type IN ('image/jpeg', 'image/png'))"
    )


def _replace(invariant: str) -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.drop_constraint(CONSTRAINT, "media_assets", type_="check")
    op.create_check_constraint(CONSTRAINT, "media_assets", invariant)


def upgrade() -> None:
    _replace(_invariant(_AUDIO_WITH_M4A))


def downgrade() -> None:
    _replace(_invariant(_AUDIO_WITH_OGG))
