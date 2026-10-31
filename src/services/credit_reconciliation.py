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
* an append-only ASAAS reconciliation alert has neither a direct accepted
  acknowledgement nor an accepted later revision of its aggregate.

Group 7 adds, on top of this report, closure rule (b) for ASAAS alert rows
and the subscription readback of the enforcement gate.
"""

from __future__ import annotations

import hashlib
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
    BillingFactReconciliationAlert,
    BillingOutboxEvent,
    BillingPaymentEffect,
    BillingPaymentProjection,
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
    open_alerts: int = 0

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

    async def _open_effect_alert_count(self) -> int:
        """Count effect-gap alerts whose repaired effect still lacks an ack.

        Effect alerts deliberately have no outbox foreign key: they are
        emitted precisely when the producer finds an ASAAS effect without an
        outbox row.  Their only durable correlation is the SHA-256 of the
        sanitized ``effect:<uuid>`` detail.  Once that effect acquires an
        accepted consumer fact, the alert is closed just like an accepted
        outbox-scoped alert.  Any non-effect or unmatched digest remains an
        open alert.
        """

        alert = BillingFactReconciliationAlert
        digests = list(
            await self.session.scalars(
                select(alert.detail_sha256).where(alert.outbox_event_id.is_(None))
            )
        )
        if not digests:
            return 0

        digest_to_effect = {
            hashlib.sha256(f"effect:{effect_id}".encode("utf-8")).hexdigest(): effect_id
            for effect_id in await self.session.scalars(select(BillingPaymentEffect.id))
        }
        effect_ids = {
            digest_to_effect[digest] for digest in digests if digest in digest_to_effect
        }
        if not effect_ids:
            return len(digests)

        accepted_effect_ids = set(
            await self.session.scalars(
                select(BillingOutboxEvent.payment_effect_id)
                .join(
                    BillingFactAck,
                    BillingFactAck.outbox_event_id == BillingOutboxEvent.id,
                )
                .where(
                    BillingOutboxEvent.payment_effect_id.in_(effect_ids),
                    BillingFactAck.outcome == "accepted",
                )
            )
        )
        # The alert means this effect has *no* outbox, so the aggregate and
        # its missing revision must come from the effect's payment projection,
        # not an impossible join back to that absent outbox.  The next
        # revision after the latest fact that predates the effect is the
        # missing revision; only an accepted fact strictly after it closes the
        # alert.  This keeps same/lower revisions and another aggregate open.
        effects = list(
            await self.session.scalars(
                select(BillingPaymentEffect).where(
                    BillingPaymentEffect.id.in_(effect_ids)
                )
            )
        )
        for effect in effects:
            projection = await self.session.scalar(
                select(BillingPaymentProjection).where(
                    BillingPaymentProjection.id == effect.projection_id,
                    BillingPaymentProjection.builder_id == effect.builder_id,
                    BillingPaymentProjection.provider == effect.provider,
                    BillingPaymentProjection.payment_id == effect.payment_id,
                )
            )
            if projection is None:
                continue
            if projection.aggregate_kind == "topup" and projection.topup_order_id:
                aggregate_kind, aggregate_id = "topup_order", projection.topup_order_id
            elif (
                projection.aggregate_kind == "subscription"
                and projection.subscription_cycle_id
            ):
                aggregate_kind, aggregate_id = (
                    "subscription_cycle",
                    projection.subscription_cycle_id,
                )
            else:
                continue
            prior_revision = await self.session.scalar(
                select(
                    func.coalesce(func.max(BillingOutboxEvent.financial_revision), 0)
                ).where(
                    BillingOutboxEvent.aggregate_kind == aggregate_kind,
                    BillingOutboxEvent.aggregate_id == aggregate_id,
                    BillingOutboxEvent.created_at < effect.created_at,
                )
            )
            missing_revision = int(prior_revision or 0) + 1
            has_accepted_successor = await self.session.scalar(
                select(
                    exists().where(
                        BillingOutboxEvent.aggregate_kind == aggregate_kind,
                        BillingOutboxEvent.aggregate_id == aggregate_id,
                        BillingOutboxEvent.financial_revision > missing_revision,
                        exists().where(
                            BillingFactAck.outbox_event_id == BillingOutboxEvent.id,
                            BillingFactAck.outcome == "accepted",
                        ),
                    )
                )
            )
            if has_accepted_successor:
                accepted_effect_ids.add(effect.id)
        return sum(
            1
            for digest in digests
            if digest_to_effect.get(digest) not in accepted_effect_ids
        )

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
        alert = BillingFactReconciliationAlert
        direct_accepted = (
            select(BillingFactAck.id)
            .where(
                BillingFactAck.outbox_event_id == alert.outbox_event_id,
                BillingFactAck.outcome == "accepted",
            )
            .exists()
        )
        alert_outbox = aliased(BillingOutboxEvent)
        alert_later_outbox = aliased(BillingOutboxEvent)
        alert_later_ack = aliased(BillingFactAck)
        alert_closed_by_later = (
            select(alert_later_ack.id)
            .select_from(alert_later_ack)
            .join(
                alert_later_outbox,
                alert_later_outbox.id == alert_later_ack.outbox_event_id,
            )
            .join(alert_outbox, alert_outbox.id == alert.outbox_event_id)
            .where(
                alert_later_ack.outcome == "accepted",
                alert_later_outbox.aggregate_kind == alert_outbox.aggregate_kind,
                alert_later_outbox.aggregate_id == alert_outbox.aggregate_id,
                alert_later_outbox.financial_revision > alert_outbox.financial_revision,
            )
            .exists()
        )
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
            open_alerts=(
                await self._count(
                    select(func.count())
                    .select_from(alert)
                    .where(
                        alert.outbox_event_id.is_not(None),
                        ~direct_accepted,
                        ~alert_closed_by_later,
                    )
                )
                + await self._open_effect_alert_count()
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
