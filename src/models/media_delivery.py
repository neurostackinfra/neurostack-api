"""The agent's sends of a media asset to a session (migration 045).

One row per (session, media asset) the agent sent, with the instant of the
last send: the 24-hour repeat window of envio-midia-agente is claimed on it
(:func:`src.repositories.media_deliveries.claim_media_delivery`).

No relationship on purpose: the three foreign keys are ``ON DELETE CASCADE``
in the DDL, so deleting a tenant, a session (LGPD) or a media asset deletes
its rows in PostgreSQL.  A relationship without ``passive_deletes`` would make
the ORM try to null a ``NOT NULL`` key instead; none means the ORM never
touches this table when a parent goes.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class MediaDelivery(Base):
    """Last time the agent sent one media asset to one session."""

    __tablename__ = "media_deliveries"
    __table_args__ = (
        # The claim's ON CONFLICT arbiter.
        UniqueConstraint(
            "session_id",
            "media_id",
            name="uq_media_deliveries_session_media",
        ),
        # The unique index leads with session_id; deleting a media asset
        # finds its deliveries through this one.
        Index("ix_media_deliveries_media", "media_id"),
        # Deleting a tenant cascades through tenant_id.
        Index("ix_media_deliveries_tenant", "tenant_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "tenants.id",
            ondelete="CASCADE",
            name="fk_media_deliveries_tenant",
        ),
        nullable=False,
    )
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "sessions.id",
            ondelete="CASCADE",
            name="fk_media_deliveries_session",
        ),
        nullable=False,
    )
    media_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "media_assets.id",
            ondelete="CASCADE",
            name="fk_media_deliveries_media",
        ),
        nullable=False,
    )
    last_sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
