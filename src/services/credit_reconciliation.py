"""Read-only reconciliation of ASAAS facts against the credit consumer.

neurostack-credit-system Group 2, Deliverable 6: compare the financial outbox
with the credit inbox, acks and effects BY READING ONLY.  It never presents a
fact again (that is the ASAAS publisher's job), never ingests by reading
``billing_outbox`` (the only ingestion is ``handle_billing_fact``), never
creates an inbox row, effect or ack and never writes an ASAAS table.  It
alerts (a log line with counts, no identities) and feeds
``credit_reconciliation_ready``, which is false while any of these holds:

* a fact in ``billing_outbox`` has no credit ack or no credit inbox row
  (backlog: not yet presented, refused by the consumer, or ``blocked``);
* a ``rejected`` ack is not closed by an ``accepted`` ack of a later
  ``financial_revision`` of the same aggregate (closure rule (a) of the
  wish's Group 7);
* an ``accepted`` ack disagrees with its outbox row or handoff digests and
  is not closed the same way;
* a ``financial_revision`` gap of the inbox is still open;
* an ASAAS payment effect (``provider='asaas'``) has no outbox row, or an
  ASAAS order/cycle is ``quarantined`` -- the quarantine signal ``main``
  already produces, accepted as permanent until an ASAAS repair wish;
* a settled, non-quarantined cycle is ahead of its enforcement binding
  (consumer lag): missing binding or a lower ``financial_revision``.

Group 7 adds, on top of this report, closure rule (b) for ASAAS alert rows
and the subscription readback of the enforcement gate.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.config.logging import get_logger
from src.models.billing import (
    BillingFactAck,
    BillingFactDelivery,
    BillingFactHandoff,
    BillingOutboxEvent,
    BillingPaymentEffect,
    BillingSubscriptionCycle,
    BillingTopupOrder,
)
from src.models.credit import CreditEnforcementBinding
from src.models.credit_fact_inbox import CreditFactInbox, CreditRevisionGap
from src.services.billing_outbox import PRODUCER

logger = get_logger(__name__)


@dataclass(frozen=True)
class CreditReconciliationReport:
    """Counts only; every field must be zero for the readiness to hold."""

    facts_without_ack: int = 0
    facts_without_inbox: int = 0
    rejected_open: int = 0
    digest_mismatches: int = 0
    open_revision_gaps: int = 0
    effects_without_outbox: int = 0
    quarantined_lines: int = 0
    binding_lag: int = 0

    @property
    def ready(self) -> bool:
        return not any(getattr(self, item.name) for item in fields(self))

    @property
    def has_backlog(self) -> bool:
        """A fact of the outbox has no credit ack (or inbox row)."""

        return bool(self.facts_without_ack or self.facts_without_inbox)

    @property
    def has_open_gap(self) -> bool:
        """An inbox revision gap is open or an ASAAS effect has no outbox."""

        return bool(self.open_revision_gaps or self.effects_without_outbox)

    def counts(self) -> dict[str, int]:
        return {item.name: int(getattr(self, item.name)) for item in fields(self)}


class CreditReconciliation:
    """Compute the report in the caller's session; reads only."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _count(self, statement: Any) -> int:
        return int(await self.session.scalar(statement) or 0)

    async def report(self) -> CreditReconciliationReport:
        outbox = BillingOutboxEvent
        ack = BillingFactAck
        later_outbox = aliased(BillingOutboxEvent)
        later_ack = aliased(BillingFactAck)
        closed_by_later_revision = (
            select(later_ack.id)
            .join(later_outbox, later_outbox.id == later_ack.outbox_event_id)
            .where(
                later_ack.outcome == "accepted",
                later_outbox.aggregate_kind == outbox.aggregate_kind,
                later_outbox.aggregate_id == outbox.aggregate_id,
                later_outbox.financial_revision > outbox.financial_revision,
            )
            .exists()
        )
        delivered_event = func.coalesce(BillingFactDelivery.event_id, outbox.id)
        report = CreditReconciliationReport(
            facts_without_ack=await self._count(
                select(func.count())
                .select_from(outbox)
                .where(~exists().where(ack.outbox_event_id == outbox.id))
            ),
            facts_without_inbox=await self._count(
                select(func.count())
                .select_from(outbox)
                .outerjoin(
                    BillingFactDelivery,
                    BillingFactDelivery.outbox_event_id == outbox.id,
                )
                .where(
                    ~exists().where(
                        CreditFactInbox.producer == PRODUCER,
                        CreditFactInbox.event_id == delivered_event,
                        CreditFactInbox.schema_version == outbox.schema_version,
                    )
                )
            ),
            rejected_open=await self._count(
                select(func.count())
                .select_from(ack)
                .join(outbox, outbox.id == ack.outbox_event_id)
                .where(ack.outcome == "rejected", ~closed_by_later_revision)
            ),
            digest_mismatches=await self._count(
                select(func.count())
                .select_from(ack)
                .join(outbox, outbox.id == ack.outbox_event_id)
                .join(
                    BillingFactHandoff,
                    BillingFactHandoff.outbox_event_id == ack.outbox_event_id,
                )
                .where(
                    ack.outcome == "accepted",
                    or_(
                        ack.fact_sha256 != outbox.payload_sha256,
                        ack.bundle_digest != BillingFactHandoff.bundle_digest,
                        ack.deployment_digest != BillingFactHandoff.deployment_digest,
                    ),
                    ~closed_by_later_revision,
                )
            ),
            open_revision_gaps=await self._count(
                select(func.count())
                .select_from(CreditRevisionGap)
                .where(CreditRevisionGap.closed_at.is_(None))
            ),
            effects_without_outbox=await self._count(
                select(func.count())
                .select_from(BillingPaymentEffect)
                .where(
                    BillingPaymentEffect.provider == "asaas",
                    ~exists().where(
                        outbox.payment_effect_id == BillingPaymentEffect.id
                    ),
                )
            ),
            quarantined_lines=await self._count(
                select(func.count())
                .select_from(BillingTopupOrder)
                .where(BillingTopupOrder.state == "quarantined")
            )
            + await self._count(
                select(func.count())
                .select_from(BillingSubscriptionCycle)
                .where(BillingSubscriptionCycle.state == "quarantined")
            ),
            binding_lag=await self._count(
                select(func.count())
                .select_from(BillingSubscriptionCycle)
                .where(
                    BillingSubscriptionCycle.state != "quarantined",
                    BillingSubscriptionCycle.settled_cents > 0,
                    ~exists().where(
                        and_(
                            CreditEnforcementBinding.subscription_cycle_id
                            == BillingSubscriptionCycle.id,
                            CreditEnforcementBinding.financial_revision
                            >= BillingSubscriptionCycle.financial_revision,
                        )
                    ),
                )
            ),
        )
        if not report.ready:
            logger.warning("credit reconciliation not ready", **report.counts())
        return report


async def credit_reconciliation_ready(session: AsyncSession) -> bool:
    """Read-only readiness precondition fed by :class:`CreditReconciliation`."""

    return (await CreditReconciliation(session).report()).ready


__all__ = [
    "CreditReconciliation",
    "CreditReconciliationReport",
    "credit_reconciliation_ready",
]
