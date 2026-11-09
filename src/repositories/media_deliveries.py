"""The 24-hour repeat window of the agent's media sends (envio-midia-agente).

Caller-owned, like :mod:`src.repositories.credit_inventory`: the claim runs in
the caller's transaction and never commits, rolls back or nests one.  The
delivery core commits the claim BEFORE any byte reaches the provider, so a
send whose claim did not commit never leaves.

PostgreSQL only.  The claim is one ``INSERT ... ON CONFLICT ... DO UPDATE
... WHERE ... RETURNING`` measured against the database clock (``now()``, the
transaction's start), so every worker and task agrees on the window whatever
its own clock says.  The in-memory SQLite of the unit tests has neither that
clock arithmetic nor the same conflict semantics under concurrency, so on
any other dialect :func:`claim_media_delivery` raises
:class:`MediaDeliveryClaimUnsupported` before it sends any SQL: a caller that
cannot claim must not send.  Unit tests fake the claim; its proofs run on
PostgreSQL (``tests/integration/test_media_deliveries_postgres.py``).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import Interval, func, literal
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.media_delivery import MediaDelivery

# The owner's window: the same media asset reaches the same session at most
# once in it.
MEDIA_DELIVERY_REPEAT_WINDOW: Final = timedelta(hours=24)


class MediaDeliveryClaimUnsupported(RuntimeError):
    """The session's dialect cannot run the claim (anything but PostgreSQL)."""


async def claim_media_delivery(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    session_id: UUID,
    media_id: UUID,
) -> UUID | None:
    """Claim the right to send ``media_id`` to ``session_id`` now.

    Returns the delivery row's id when this call claimed: the first send to
    this session (a new row) or one whose last send is at least
    :data:`MEDIA_DELIVERY_REPEAT_WINDOW` old (``last_sent_at`` moves to
    ``now()``).  Returns ``None`` when the media was sent within the window:
    nothing changes and the caller skips the send in silence.

    Atomic under concurrency: a second claim of the same pair waits on the
    first one's row lock and, once that one commits, sees its fresh
    ``last_sent_at`` and returns ``None``; if the first one rolls back, the
    second one inserts.  The claim locks the pair's row until the caller's
    transaction ends, also when it returns ``None`` (PostgreSQL locks the
    conflicting row even when the ``WHERE`` refuses the update), so claim in a
    fresh transaction and end it right away, before any provider call.  The
    ids are the caller's, already resolved tenant-scoped (the session of the
    turn, the media asset re-read in its tenant); the foreign keys only prove
    they exist.
    """

    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        raise MediaDeliveryClaimUnsupported(
            f"media delivery claim needs PostgreSQL, not {bind.dialect.name}"
        )
    window = literal(MEDIA_DELIVERY_REPEAT_WINDOW, Interval())
    statement = (
        postgresql_insert(MediaDelivery)
        .values(
            id=uuid4(),
            tenant_id=tenant_id,
            session_id=session_id,
            media_id=media_id,
            last_sent_at=func.now(),
        )
        .on_conflict_do_update(
            index_elements=[MediaDelivery.session_id, MediaDelivery.media_id],
            set_={"last_sent_at": func.now()},
            where=MediaDelivery.last_sent_at <= func.now() - window,
        )
        .returning(MediaDelivery.id)
    )
    claimed = (await session.execute(statement)).scalar_one_or_none()
    return claimed
