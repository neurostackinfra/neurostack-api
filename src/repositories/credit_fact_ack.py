"""The credit consumer's only writer of ``billing_fact_acks`` (thin).

``billing_fact_acks`` is ASAAS DDL (migration 027), consumer-owned and
append-only (``trg_billing_fact_acks_immutable``); its model is the existing
``BillingFactAck``, so the ack has no credit model of its own.  This
repository only INSERTs into ``billing_fact_acks`` and only READS
``billing_fact_deliveries``, ``billing_outbox`` and ``billing_fact_handoffs``
-- to resolve the ``outbox_event_id`` of an ``event_id`` and to copy the
handoff's ``deployment_digest``.  It never writes another ASAAS table and
never imports the producer's services.

Values (``billing.fact.ack.v1``, pinned by the credit wish):

* ``fact_sha256``: recomputed by the caller as
  ``src.services.billing_outbox.payload_digest(payload)``;
* ``bundle_digest``: the caller's own ``billing_fact_bundle_digest()``
  (computed before ``validate_fact``, outside the code that maps a fact
  violation to ``rejected``);
* ``deployment_digest``: the ONLY value copied, from the handoff row;
* ``effects_digest``: :func:`effects_digest` of the effect ids the fact
  produced -- :data:`EMPTY_EFFECTS_DIGEST` for none;
* ``rejected`` is only for a problem in the fact itself, and carries zero
  effects.

Every method runs in the caller's transaction (with the inbox row the ack
names) and never commits.  Nothing here reads ``tests/``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.billing import (
    BillingFactAck,
    BillingFactDelivery,
    BillingFactHandoff,
    BillingOutboxEvent,
)

ACK_SCHEMA_VERSION: Final = "billing.fact.ack.v1"
ACK_OUTCOMES: Final = ("accepted", "rejected")
# sha256(b"[]"): the effects digest of a fact that produced no effect.
EMPTY_EFFECTS_DIGEST: Final = (
    "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
)
_SHA256_HEX: Final = re.compile(r"[0-9a-f]{64}")


def effects_digest(effect_ids: Iterable[str]) -> str:
    """SHA-256 of ``json.dumps(sorted(effect_ids), separators=(",", ":"))``."""

    encoded = json.dumps(sorted(effect_ids), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_hex(value: str, name: str) -> str:
    if not isinstance(value, str) or _SHA256_HEX.fullmatch(value) is None:
        raise ValueError(f"{name} must be 64 lowercase hex characters")
    return value


@dataclass(frozen=True)
class FactHandoff:
    """The producer rows an ack answers (read-only)."""

    outbox_event_id: UUID
    event_id: UUID
    payload_sha256: str
    bundle_digest: str
    deployment_digest: str


class CreditFactAckRepository:
    """Insert-only access to ``billing_fact_acks``; read-only lookups."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def resolve_handoff(self, event_id: UUID) -> FactHandoff | None:
        """Resolve a fact's ``event_id`` to its outbox row and handoff, the
        way the ASAAS reconciler pairs them (delivery ``event_id``)."""

        row = (
            await self.session.execute(
                select(
                    BillingFactDelivery.outbox_event_id,
                    BillingFactDelivery.event_id,
                    BillingOutboxEvent.payload_sha256,
                    BillingFactHandoff.bundle_digest,
                    BillingFactHandoff.deployment_digest,
                )
                .join(
                    BillingOutboxEvent,
                    BillingOutboxEvent.id == BillingFactDelivery.outbox_event_id,
                )
                .join(
                    BillingFactHandoff,
                    BillingFactHandoff.outbox_event_id
                    == BillingFactDelivery.outbox_event_id,
                )
                .where(BillingFactDelivery.event_id == event_id)
            )
        ).one_or_none()
        if row is None:
            return None
        return FactHandoff(
            outbox_event_id=row.outbox_event_id,
            event_id=row.event_id,
            payload_sha256=row.payload_sha256,
            bundle_digest=row.bundle_digest,
            deployment_digest=row.deployment_digest,
        )

    async def get_ack(self, outbox_event_id: UUID) -> BillingFactAck | None:
        return (
            await self.session.execute(
                select(BillingFactAck).where(
                    BillingFactAck.outbox_event_id == outbox_event_id
                )
            )
        ).scalar_one_or_none()

    async def insert_ack(
        self,
        *,
        handoff: FactHandoff,
        outcome: str,
        inbox_event_id: UUID,
        fact_sha256: str,
        bundle_digest: str,
        effect_ids: Iterable[str],
        acknowledged_at: datetime,
    ) -> BillingFactAck:
        """Append the ack of one fact; ``deployment_digest`` is the handoff's."""

        if outcome not in ACK_OUTCOMES:
            raise ValueError(f"ack outcome must be one of {ACK_OUTCOMES}")
        effects = list(effect_ids)
        if outcome == "rejected" and effects:
            raise ValueError("a rejected fact acknowledges zero effects")
        if acknowledged_at.tzinfo is None or acknowledged_at.utcoffset() is None:
            raise ValueError("acknowledged_at must be timezone-aware")
        ack = BillingFactAck(
            id=uuid4(),
            outbox_event_id=handoff.outbox_event_id,
            event_id=handoff.event_id,
            schema_version=ACK_SCHEMA_VERSION,
            outcome=outcome,
            inbox_event_id=inbox_event_id,
            fact_sha256=_sha256_hex(fact_sha256, "fact_sha256"),
            bundle_digest=_sha256_hex(bundle_digest, "bundle_digest"),
            deployment_digest=_sha256_hex(
                handoff.deployment_digest, "deployment_digest"
            ),
            effects_digest=effects_digest(effects),
            acknowledged_at=acknowledged_at,
        )
        self.session.add(ack)
        await self.session.flush()
        return ack
