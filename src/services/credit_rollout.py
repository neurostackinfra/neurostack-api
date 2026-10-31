"""Readiness and server-disabled enforcement preflight (Group 7).

The credit consumer owns only the *technical* preconditions.  This module is
intentionally incapable of changing billing rollout/top-up state; its one
write to ``credit_enforcement_state`` has no route and is reserved for the
future operational opening.  All ASAAS rows are read-only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Final, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.logging import get_logger
from src.config.settings import settings
from src.models.billing import (
    BillingFactAck,
    BillingOutboxEvent,
    BillingSubscription,
    BillingSubscriptionCycle,
    BillingTopupOrder,
)
from src.models.credit import (
    CreditCapabilityReadiness,
    CreditDebt,
    CreditEffect,
    CreditEnforcementBinding,
    CreditEnforcementState,
    CreditIngressClaim,
    CreditLedgerEntry,
    CreditLot,
    CreditObservationalEffect,
    CreditResponseOutbox,
)
from src.models.credit_enforcement import CreditCutoffAcceptance
from src.models.credit_fact_inbox import CreditFactInbox
from src.models.credit_inventory import CreditAllocation
from src.models.event import Event
from src.models.handoff import HandoffRecord
from src.models.tenant import Tenant
from src.observability.metric_dimensions import ContractCounter as Counter
from src.observability.metric_dimensions import ContractGauge as Gauge
from src.repositories.credit import LOT_DECREASES, LOT_INCREASES, CreditRepository
from src.services.billing_outbox import authoritative_billing_fact_schema_digest
from src.services.credit_ledger import start_of_civil_day
from src.services.credit_lifecycle import binding_grace_end
from src.services.credit_reconciliation import (
    CreditReconciliation,
    CreditReconciliationReport,
)
from src.services.credit_switch import credit_switch_effective

logger = get_logger(__name__)

READINESS_SCHEMA_VERSION: Final = "credit_capability_ready.v1"
SAO_PAULO: Final = ZoneInfo("America/Sao_Paulo")
TRACE_COST_MC: Final = 1_000
ORPHAN_EFFECT_KINDS: Final = frozenset(
    {
        "trace",
        "trace_credit_pending",
        "trace_no_response",
        "rag",
        "media",
        "egress",
        "maintenance",
    }
)
UNKNOWN_INGRESS_SOURCES: Final = frozenset({"uazapi", "evolution"})
# Non-terminal ASAAS subscription statuses (``ck_billing_subscriptions_status``
# of ``src/models/billing.py``; terminal are ``inactive`` and ``cancelled``).
# Named here rather than imported: the readback must not depend on producer
# code.
OPEN_SUBSCRIPTION_STATUSES: Final = (
    "active",
    "active_linked",
    "readback_required",
    "non_billable_pending_readback",
)
# Open statuses that themselves await a provider readback: with a current
# cycle they are ambiguous and block the enforcement gate.
AMBIGUOUS_SUBSCRIPTION_STATUSES: Final = frozenset(
    {"readback_required", "non_billable_pending_readback"}
)
PRODUCTION_SERVING: Final = "production-serving"
OBSERVATIONAL_DROP_REASONS: Final = frozenset(
    {"route_busy", "pool_exhausted", "timeout", "failed", "tenant_gone"}
)

CREDIT_RECONCILIATION_READY = Gauge(
    "neurostack_credit_reconciliation_ready",
    "Whether credit reconciliation is currently ready (1=true).",
    registry=None,
)
CREDIT_ORPHAN_EFFECTS = Gauge(
    "neurostack_credit_orphan_effects",
    "Open credit effects whose durable decision/outbox boundary is incomplete.",
    registry=None,
)
CREDIT_OBSERVATIONAL_WRITE_DROPPED_TOTAL = Counter(
    "neurostack_credit_observational_write_dropped_total",
    "Best-effort observational writes discarded by bounded reason.",
    ["reason"],
    registry=None,
)


@dataclass(frozen=True)
class ReadbackReport:
    """Subscription-cycle coverage, evaluated without mutating anything."""

    missing_or_stale_bindings: int = 0
    ambiguous_current_cycles: int = 0

    @property
    def complete(self) -> bool:
        return (
            self.missing_or_stale_bindings == 0 and self.ambiguous_current_cycles == 0
        )


@dataclass(frozen=True)
class BindingCoverage:
    """Capacity source permitted for an enforced request.

    A valid monthly binding says the subscription grant may participate in a
    debit; it deliberately does *not* say that it has a positive balance.
    An empty, valid account therefore becomes debt and enters the existing
    72-hour lifecycle rather than being reclassified as ``credit_pending``.
    """

    monthly_binding: bool = False
    usable_topup: bool = False
    quarantined: bool = False
    identity_conflict: bool = False
    reconciliation_failure: bool = False

    @property
    def usable(self) -> bool:
        return (
            not self.quarantined
            and not self.identity_conflict
            and not self.reconciliation_failure
        )

    @property
    def covered(self) -> bool:
        """Whether a builder has capacity coverage for the rollout cutoff.

        This is intentionally narrower than :attr:`usable`: a normal builder
        with no financial capacity is usable for the 72-hour debt path, but
        is not covered for the one-time enforcement cutoff report.
        """

        return (
            not self.quarantined
            and not self.identity_conflict
            and (self.monthly_binding or self.usable_topup)
        )


@dataclass(frozen=True)
class CutoffFinding:
    tenant_id: UUID
    builder_id: UUID | None
    reason: str

    def canonical(self) -> dict[str, object]:
        return {
            "tenant_id": str(self.tenant_id),
            "builder_id": None if self.builder_id is None else str(self.builder_id),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CutoffReport:
    findings: tuple[CutoffFinding, ...]
    digest: str
    accepted: bool

    @property
    def ready(self) -> bool:
        return not self.findings or self.accepted


@dataclass(frozen=True)
class OrphanReport:
    decision_without_effect: int = 0
    effect_without_decision: int = 0
    trace_partial_debit: int = 0
    orphan_outbox: int = 0
    handoff_without_effect: int = 0

    @property
    def open_count(self) -> int:
        return sum(
            (
                self.decision_without_effect,
                self.effect_without_decision,
                self.trace_partial_debit,
                self.orphan_outbox,
                self.handoff_without_effect,
            )
        )


@dataclass(frozen=True)
class CapabilityReadiness:
    environment: str
    billing_fact_schema_digest: str
    inbox_high_watermark: int
    reconciliation_complete: bool
    readback_complete: bool
    has_open_gap: bool
    has_backlog: bool
    has_alert: bool

    @property
    def ready(self) -> bool:
        return (
            self.reconciliation_complete
            and self.readback_complete
            and not self.has_open_gap
            and not self.has_backlog
            and not self.has_alert
        )


class CreditEnforcementRejected(RuntimeError):
    """The server-side preflight did not establish a safe enforcement point."""


class CreditRolloutService:
    """Single-owner credit readiness computations in one caller transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def current_civil_date(now: datetime | None = None) -> date:
        stamp = now or datetime.now(timezone.utc)
        if stamp.tzinfo is None:
            raise ValueError("rollout time must be timezone-aware")
        return stamp.astimezone(SAO_PAULO).date()

    async def reconciliation_report(self) -> CreditReconciliationReport:
        report = await CreditReconciliation(self.session).report()
        if credit_switch_effective():
            CREDIT_RECONCILIATION_READY.set(1 if report.ready else 0)
        return report

    async def readback_report(self, *, now: datetime | None = None) -> ReadbackReport:
        """Require an exact active binding for every OPEN subscription's
        current candidate cycle.

        Open is ``is_active`` or any non-terminal ASAAS status
        (:data:`OPEN_SUBSCRIPTION_STATUSES`), so an inactivated subscription
        still awaiting its readback stays inside the gate.  A subscription
        whose status itself awaits a provider readback
        (:data:`AMBIGUOUS_SUBSCRIPTION_STATUSES`) and that has a current
        cycle is ambiguous, as are two candidate cycles: both block until
        reconciled.  A subscription with no candidate current cycle is
        deliberately not a global failure; it is handled by the tenant cutoff
        report/grace rule.
        """

        today = self.current_civil_date(now)
        rows = (
            await self.session.execute(
                select(BillingSubscription, BillingSubscriptionCycle)
                .join(
                    BillingSubscriptionCycle,
                    BillingSubscriptionCycle.subscription_id == BillingSubscription.id,
                )
                .where(
                    BillingSubscription.provider == "asaas",
                    or_(
                        BillingSubscription.is_active.is_(True),
                        BillingSubscription.status.in_(OPEN_SUBSCRIPTION_STATUSES),
                    ),
                    BillingSubscriptionCycle.cycle_start <= today,
                    BillingSubscriptionCycle.cycle_end > today,
                )
                .order_by(BillingSubscription.id, BillingSubscriptionCycle.id)
            )
        ).all()
        by_subscription: dict[UUID, tuple[str, list[BillingSubscriptionCycle]]] = {}
        for subscription, cycle in rows:
            by_subscription.setdefault(subscription.id, (subscription.status, []))[
                1
            ].append(cycle)

        missing = 0
        ambiguous = 0
        for status, cycles in by_subscription.values():
            if len(cycles) != 1 or status in AMBIGUOUS_SUBSCRIPTION_STATUSES:
                ambiguous += 1
                continue
            cycle = cycles[0]
            binding = (
                await self.session.execute(
                    select(CreditEnforcementBinding).where(
                        CreditEnforcementBinding.subscription_cycle_id == cycle.id
                    )
                )
            ).scalar_one_or_none()
            if (
                binding is None
                or not self._binding_matches_cycle(binding, cycle)
                or not await self._grant_exists(binding)
            ):
                missing += 1
        return ReadbackReport(missing, ambiguous)

    async def _effect_exists(self, builder_id: UUID, effect_id: str) -> bool:
        return bool(
            await self.session.scalar(
                select(
                    exists().where(
                        CreditEffect.builder_id == builder_id,
                        CreditEffect.effect_id == effect_id,
                    )
                )
            )
        )

    async def _monthly_lot_exists(self, binding: CreditEnforcementBinding) -> bool:
        return bool(
            await self.session.scalar(
                select(CreditLot.id)
                .where(
                    CreditLot.builder_id == binding.builder_id,
                    CreditLot.lot_kind == "monthly",
                    CreditLot.subscription_id == binding.subscription_id,
                    CreditLot.cycle_start == binding.cycle_start,
                    CreditLot.cycle_end == binding.cycle_end,
                    CreditLot.grant_effect_id == binding.grant_effect_id,
                )
                .limit(1)
            )
        )

    @staticmethod
    def _binding_identity_matches(
        binding: CreditEnforcementBinding | None,
        cycle: BillingSubscriptionCycle,
    ) -> bool:
        """The binding names this cycle: builder, subscription, id and period.

        Identity only -- neither ``status`` nor ``financial_revision``.  The
        grant effect half of the identity is checked against the ledger by
        the callers (:meth:`_grant_exists`).
        """

        return bool(
            binding is not None
            and binding.builder_id == cycle.builder_id
            and binding.subscription_id == cycle.subscription_id
            and binding.subscription_cycle_id == cycle.id
            and binding.cycle_start == cycle.cycle_start
            and binding.cycle_end == cycle.cycle_end
        )

    @classmethod
    def _binding_matches_cycle(
        cls,
        binding: CreditEnforcementBinding | None,
        cycle: BillingSubscriptionCycle,
    ) -> bool:
        """Strict readback: identity, ``active`` and the EXACT revision.

        This is the predicate of readiness and of :meth:`arm_enforcement`.
        A request uses the looser rule of :meth:`binding_coverage`, where an
        identity-matched active binding behind its cycle (consumer lag) still
        serves.
        """

        return bool(
            cls._binding_identity_matches(binding, cycle)
            and binding is not None
            and binding.status == "active"
            and binding.financial_revision == cycle.financial_revision
        )

    async def _grant_exists(self, binding: CreditEnforcementBinding) -> bool:
        """The binding's grant effect and monthly lot are in the ledger."""

        return await self._effect_exists(
            binding.builder_id, binding.grant_effect_id
        ) and await self._monthly_lot_exists(binding)

    async def binding_coverage(
        self,
        builder_id: UUID,
        *,
        at: datetime | None = None,
        quarantine_checked: bool = False,
    ) -> BindingCoverage:
        """Read the only capacity sources an enforced request may use.

        This is deliberately a *read-only* gate.  For each current cycle of
        an active subscription the binding is classified by IDENTITY first
        (builder, subscription, cycle id, period, grant effect and lot):

        * identity matched, ``active``, revision equal to the cycle's or
          BEHIND it (consumer lag): monthly coverage.  Lag keeps readiness
          and readback false and alerts (``binding_lag``) but never denies
          the request;
        * identity matched, ``reversed`` (a full chargeback of the plan
          line): no monthly coverage and NOT a reconciliation failure, so an
          uncovered request follows the 72-hour debt lifecycle;
        * identity matched, revision AHEAD of the cycle: not usable (the
          binding names a fact the producer does not have) -- treated like a
          missing binding;
        * identity divergent: a conflict that blocks both source classes.

        ``reconciliation_failure`` (``credit_pending``) is therefore only an
        identity conflict, a current cycle with no usable binding outside the
        previous binding's grace, or an unreconciled top-up source.  An ASAAS
        quarantine also blocks both source classes.
        """

        instant = at or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise ValueError("coverage time must be timezone-aware")
        instant = instant.astimezone(timezone.utc)
        today = self.current_civil_date(instant)
        repository = CreditRepository(self.session)
        if not quarantine_checked and await repository.quarantine_hold(builder_id):
            return BindingCoverage(quarantined=True)

        current_rows = (
            await self.session.execute(
                select(
                    BillingSubscription,
                    BillingSubscriptionCycle,
                    CreditEnforcementBinding,
                )
                .join(
                    BillingSubscriptionCycle,
                    and_(
                        BillingSubscriptionCycle.subscription_id
                        == BillingSubscription.id,
                        BillingSubscriptionCycle.builder_id
                        == BillingSubscription.builder_id,
                    ),
                )
                .outerjoin(
                    CreditEnforcementBinding,
                    CreditEnforcementBinding.subscription_cycle_id
                    == BillingSubscriptionCycle.id,
                )
                .where(
                    BillingSubscription.builder_id == builder_id,
                    BillingSubscription.provider == "asaas",
                    BillingSubscription.is_active.is_(True),
                    BillingSubscriptionCycle.cycle_start <= today,
                    BillingSubscriptionCycle.cycle_end > today,
                )
            )
        ).all()
        identity_conflict = False
        unbound_current = False
        monthly = False
        for subscription, cycle, binding in current_rows:
            if binding is None:
                unbound_current = True
                continue
            if not (
                self._binding_identity_matches(binding, cycle)
                and binding.subscription_id == subscription.id
                and await self._grant_exists(binding)
            ):
                identity_conflict = True
                continue
            if binding.financial_revision > cycle.financial_revision:
                # Ahead of the producer's row (whatever its status): it names
                # a fact the producer does not have -- unusable, like no
                # binding.
                unbound_current = True
                continue
            if binding.status != "active":
                # Reversed: no monthly capacity, but a reconciled state.
                continue
            # Exact, or behind the cycle (consumer lag): the request is
            # served; readiness stays false through the strict readback.
            monthly = True

        # A previous active binding remains usable only for its subscription's
        # bounded civil-time grace.  A following binding commit cuts it off;
        # inactive/cancelled subscriptions have no grace at all.
        if not monthly and not identity_conflict:
            bindings = (
                await self.session.execute(
                    select(CreditEnforcementBinding, BillingSubscription)
                    .join(
                        BillingSubscription,
                        and_(
                            BillingSubscription.id
                            == CreditEnforcementBinding.subscription_id,
                            BillingSubscription.builder_id
                            == CreditEnforcementBinding.builder_id,
                        ),
                    )
                    .where(
                        CreditEnforcementBinding.builder_id == builder_id,
                        CreditEnforcementBinding.status == "active",
                        BillingSubscription.provider == "asaas",
                        BillingSubscription.is_active.is_(True),
                        CreditEnforcementBinding.cycle_end <= today,
                    )
                    .order_by(
                        CreditEnforcementBinding.cycle_end.desc(),
                        CreditEnforcementBinding.id,
                    )
                )
            ).all()
            for binding, subscription in bindings:
                next_binding_at = await self.session.scalar(
                    select(func.min(CreditEnforcementBinding.created_at)).where(
                        CreditEnforcementBinding.subscription_id
                        == binding.subscription_id,
                        CreditEnforcementBinding.cycle_start >= binding.cycle_end,
                    )
                )
                grace_end = binding_grace_end(
                    start_of_civil_day(binding.cycle_end),
                    grace_days=settings.credit_binding_grace_days,
                    is_active=bool(subscription.is_active),
                    next_binding_at=next_binding_at,
                )
                if (
                    instant < grace_end
                    and await self._effect_exists(builder_id, binding.grant_effect_id)
                    and await self._monthly_lot_exists(binding)
                ):
                    monthly = True
                    break

        # Top-ups are subscription-independent: cancellation never removes a
        # reconciled lot.  The aggregate must not be quarantined, must retain
        # a positive swept ledger balance, and must have an accepted fact.
        lot_balance = func.coalesce(
            func.sum(
                case(
                    (
                        CreditLedgerEntry.entry_type.in_(LOT_INCREASES),
                        CreditLedgerEntry.amount_mc,
                    ),
                    (
                        CreditLedgerEntry.entry_type.in_(LOT_DECREASES),
                        -CreditLedgerEntry.amount_mc,
                    ),
                    else_=0,
                )
            ),
            0,
        )
        accepted_topup_fact = (
            select(BillingFactAck.id)
            .join(
                BillingOutboxEvent,
                BillingOutboxEvent.id == BillingFactAck.outbox_event_id,
            )
            .where(
                BillingOutboxEvent.topup_order_id == CreditLot.order_id,
                BillingFactAck.outcome == "accepted",
            )
            .exists()
        )
        usable_topup = bool(
            (
                await self.session.execute(
                    select(CreditLot.id)
                    .join(
                        BillingTopupOrder,
                        and_(
                            BillingTopupOrder.id == CreditLot.order_id,
                            BillingTopupOrder.builder_id == CreditLot.builder_id,
                        ),
                    )
                    .outerjoin(
                        CreditLedgerEntry,
                        and_(
                            CreditLedgerEntry.builder_id == CreditLot.builder_id,
                            CreditLedgerEntry.lot_id == CreditLot.id,
                        ),
                    )
                    .where(
                        CreditLot.builder_id == builder_id,
                        CreditLot.lot_kind == "topup",
                        CreditLot.expires_at > instant,
                        BillingTopupOrder.state != "quarantined",
                        BillingTopupOrder.settled_cents > 0,
                        accepted_topup_fact,
                    )
                    .group_by(CreditLot.id)
                    .having(lot_balance > 0)
                    .limit(1)
                )
            ).scalar_one_or_none()
            is not None
        )
        # A builder with no financial capacity at all is intentionally served
        # as debt.  ``credit_pending`` is reserved for a conflicting or
        # unreconciled *financial* source.  A ledger lot alone is not proof
        # that a current ASAAS source exists: test/bootstrap grants and a
        # builder that has never paid must retain the normal debt path.  Once
        # a top-up order exists, however, its missing accepted fact is a
        # reconciliation failure and cannot be spent optimistically.
        has_unreconciled_topup_source = bool(
            await self.session.scalar(
                select(CreditLot.id)
                .join(
                    BillingTopupOrder,
                    and_(
                        BillingTopupOrder.id == CreditLot.order_id,
                        BillingTopupOrder.builder_id == CreditLot.builder_id,
                    ),
                )
                .where(
                    CreditLot.builder_id == builder_id,
                    CreditLot.lot_kind == "topup",
                    CreditLot.expires_at > instant,
                    BillingTopupOrder.state != "quarantined",
                    BillingTopupOrder.settled_cents > 0,
                    ~accepted_topup_fact,
                )
                .limit(1)
            )
        )
        reconciliation_failure = bool(
            identity_conflict
            or (unbound_current and not monthly and not usable_topup)
            or (has_unreconciled_topup_source and not monthly and not usable_topup)
        )
        return BindingCoverage(
            monthly_binding=monthly,
            usable_topup=usable_topup,
            identity_conflict=identity_conflict,
            reconciliation_failure=reconciliation_failure,
        )

    async def capability_ready(
        self, *, now: datetime | None = None
    ) -> CapabilityReadiness:
        """Calculate readiness only; this function has no persistence side effect.

        ``has_alert`` is broader than an open ASAAS reconciliation alert: it
        is also true while an ASAAS line is ``quarantined`` (quarantine), while
        a settled cycle's binding is missing or behind its revision (consumer
        binding lag -- requests are still served), for an open ``rejected``
        ack or digest mismatch, and while an orphan effect is open.
        """

        reconciliation = await self.reconciliation_report()
        readback = await self.readback_report(now=now)
        orphan = await self.orphan_report()
        watermark = int(
            await self.session.scalar(
                select(func.coalesce(func.max(CreditFactInbox.inbox_seq), 0))
            )
            or 0
        )
        return CapabilityReadiness(
            environment=settings.environment,
            billing_fact_schema_digest=authoritative_billing_fact_schema_digest(
                settings.environment
            ),
            inbox_high_watermark=watermark,
            reconciliation_complete=reconciliation.ready,
            readback_complete=readback.complete,
            has_open_gap=reconciliation.has_open_gap,
            has_backlog=reconciliation.has_backlog,
            # Also true for quarantine and binding lag (see the docstring).
            has_alert=bool(
                reconciliation.rejected_open
                or reconciliation.digest_mismatches
                or reconciliation.quarantined_lines
                or reconciliation.binding_lag
                or reconciliation.open_alerts
                or orphan.open_count
            ),
        )

    async def record_capability_readiness(self) -> CreditCapabilityReadiness:
        """Append a code-only readiness row; callers must own commit/rollback."""

        state = await self.capability_ready()
        row = CreditCapabilityReadiness(
            id=uuid4(),
            schema_version=READINESS_SCHEMA_VERSION,
            environment=state.environment,
            billing_fact_schema_digest=state.billing_fact_schema_digest,
            inbox_high_watermark=state.inbox_high_watermark,
            reconciliation_complete=state.reconciliation_complete,
            readback_complete=state.readback_complete,
            has_open_gap=state.has_open_gap,
            has_backlog=state.has_backlog,
            has_alert=state.has_alert,
            # Group 8/ASAAS exclusively own these receipt-derived values.
            operation_id=None,
            deployment_digest=None,
            credit_deployment_digest=None,
            producer_compatibility_digest=None,
            consumer_compatibility_digest=None,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    @staticmethod
    def _month_window(today: date) -> tuple[datetime, datetime]:
        """Current + preceding São Paulo civil months as UTC instants."""

        current = today.replace(day=1)
        previous = (
            current.replace(year=current.year - 1, month=12)
            if current.month == 1
            else current.replace(month=current.month - 1)
        )
        return (
            datetime.combine(previous, time.min, tzinfo=SAO_PAULO).astimezone(
                timezone.utc
            ),
            datetime.combine(today, time.max, tzinfo=SAO_PAULO).astimezone(
                timezone.utc
            ),
        )

    async def cutoff_report(self, *, now: datetime | None = None) -> CutoffReport:
        """Build the tenant cutoff evidence without carrying a legacy exemption.

        The report intentionally uses current traffic evidence only: a
        ``message.in`` event, an observational effect, or a Uazapi/Evolution
        observational ingress.  It never consults historical
        ``events.interaction_origin``.  A report finding is not a credit
        permission: it needs a separate immutable acceptance for this exact
        report digest before the dormant arming transaction can proceed.
        """

        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise ValueError("cutoff report time must be timezone-aware")
        lower, upper = self._month_window(self.current_civil_date(instant))
        traffic: dict[UUID, dict[str, UUID | None | bool]] = {}

        def seen(tenant_id: UUID, builder_id: UUID | None, field: str) -> None:
            # ``builder_id`` here is only the builder RECORDED with the
            # evidence; it is resolved against the current tenant row below.
            row = traffic.setdefault(
                tenant_id,
                {
                    "builder_id": builder_id,
                    "message": False,
                    "observational": False,
                    "unknown": False,
                },
            )
            if row["builder_id"] is None and builder_id is not None:
                row["builder_id"] = builder_id
            row[field] = True

        message_rows = (
            await self.session.execute(
                select(Event.tenant_id, Tenant.builder_id)
                .outerjoin(Tenant, Tenant.id == Event.tenant_id)
                .where(
                    Event.event_type == "message.in",
                    Event.created_at >= lower,
                    Event.created_at <= upper,
                )
            )
        ).all()
        for tenant_id, builder_id in message_rows:
            seen(tenant_id, builder_id, "message")

        observational_rows = (
            await self.session.execute(
                select(
                    CreditObservationalEffect.tenant_id,
                    CreditObservationalEffect.builder_id,
                    CreditObservationalEffect.origin,
                    CreditObservationalEffect.authenticated,
                ).where(
                    CreditObservationalEffect.recorded_at >= lower,
                    CreditObservationalEffect.recorded_at <= upper,
                )
            )
        ).all()
        for tenant_id, builder_id, origin, authenticated in observational_rows:
            seen(tenant_id, builder_id, "observational")
            if origin == "unknown" and authenticated is False:
                seen(tenant_id, builder_id, "unknown")

        unknown_ingress_rows = (
            await self.session.execute(
                select(
                    CreditIngressClaim.tenant_id, CreditIngressClaim.builder_id
                ).where(
                    CreditIngressClaim.mode == "observational",
                    CreditIngressClaim.source.in_(UNKNOWN_INGRESS_SOURCES),
                    CreditIngressClaim.created_at >= lower,
                    CreditIngressClaim.created_at <= upper,
                )
            )
        ).all()
        for tenant_id, builder_id in unknown_ingress_rows:
            seen(tenant_id, builder_id, "unknown")

        # A tenant can carry old observations/claims recorded under a builder
        # it no longer belongs to (re-parent, or detached to standalone).  The
        # CURRENT tenants.builder_id wins whenever the tenant row exists, even
        # when it is NULL; only a deleted tenant keeps the recorded builder.
        if traffic:
            current = {
                tenant_id: builder_id
                for tenant_id, builder_id in (
                    await self.session.execute(
                        select(Tenant.id, Tenant.builder_id).where(
                            Tenant.id.in_(list(traffic))
                        )
                    )
                ).all()
            }
            for tenant_id, evidence in traffic.items():
                if tenant_id in current:
                    evidence["builder_id"] = current[tenant_id]

        findings: list[CutoffFinding] = []
        for tenant_id in sorted(traffic, key=str):
            row = traffic[tenant_id]
            builder_id = row["builder_id"]
            assert builder_id is None or isinstance(builder_id, UUID)
            if builder_id is None:
                findings.append(CutoffFinding(tenant_id, None, "standalone"))
            elif not (await self.binding_coverage(builder_id, at=instant)).covered:
                findings.append(
                    CutoffFinding(tenant_id, builder_id, "builder_without_coverage")
                )
            if row["message"] and not row["observational"]:
                findings.append(
                    CutoffFinding(tenant_id, builder_id, "message_without_observation")
                )
            if row["unknown"]:
                findings.append(
                    CutoffFinding(
                        tenant_id, builder_id, "unknown_unauthenticated_traffic"
                    )
                )

        findings.sort(key=lambda finding: (str(finding.tenant_id), finding.reason))
        digest = cutoff_digest([finding.canonical() for finding in findings])
        if not findings:
            return CutoffReport((), digest, True)
        accepted_rows = (
            await self.session.execute(
                select(
                    CreditCutoffAcceptance.tenant_id,
                    CreditCutoffAcceptance.builder_id,
                    CreditCutoffAcceptance.reason,
                ).where(CreditCutoffAcceptance.report_digest == digest)
            )
        ).all()
        accepted = {
            (tenant_id, builder_id, reason)
            for tenant_id, builder_id, reason in accepted_rows
        }
        complete = all(
            (finding.tenant_id, finding.builder_id, finding.reason) in accepted
            for finding in findings
        )
        return CutoffReport(tuple(findings), digest, complete)

    @staticmethod
    def _trace_effect_id(
        tenant_id: UUID, interaction_id: UUID, decision_version: int
    ) -> str:
        return f"trace:{tenant_id}:{interaction_id}:decision:{decision_version}"

    async def orphan_report(self) -> OrphanReport:
        """Reconcile durable decision/effect/outbox boundaries by reading only.

        Zero-cost effects are complete effects, not partial debits.  The one
        mutation is intentionally elsewhere in :meth:`reconcile_orphans` and
        is limited to a deletion closure for an already-deleted tenant.
        """

        claims = (
            await self.session.execute(
                select(
                    CreditIngressClaim.tenant_id,
                    CreditIngressClaim.builder_id,
                    CreditIngressClaim.interaction_id,
                    CreditIngressClaim.decision_version,
                ).where(
                    CreditIngressClaim.mode == "enforced",
                    CreditIngressClaim.builder_id.is_not(None),
                )
            )
        ).all()
        expected: set[tuple[UUID, str]] = {
            (
                builder_id,
                self._trace_effect_id(tenant_id, interaction_id, int(version)),
            )
            for tenant_id, builder_id, interaction_id, version in claims
            if isinstance(builder_id, UUID)
        }
        closure_effect_ids = {
            effect_id
            for (effect_id,) in (
                await self.session.execute(
                    select(CreditEffect.effect_id).where(
                        CreditEffect.effect_kind == "deletion_closure"
                    )
                )
            ).all()
        }
        live_tenants = {
            tenant_id
            for (tenant_id,) in (await self.session.execute(select(Tenant.id))).all()
        }
        allocation_effects = {
            (builder_id, effect_id)
            for builder_id, effect_id in (
                await self.session.execute(
                    select(
                        CreditAllocation.builder_id, CreditAllocation.effect_id
                    ).where(CreditAllocation.effect_id.is_not(None))
                )
            ).all()
            if isinstance(effect_id, str)
        }
        effects = (
            await self.session.execute(
                select(
                    CreditEffect.builder_id,
                    CreditEffect.tenant_id,
                    CreditEffect.effect_id,
                    CreditEffect.effect_kind,
                ).where(CreditEffect.effect_kind.in_(ORPHAN_EFFECT_KINDS))
            )
        ).all()
        actual = {
            (builder_id, effect_id) for builder_id, _tenant, effect_id, _kind in effects
        }
        decision_without_effect = len(expected - actual)

        # Only trace-shaped effects are decisions with an ingress boundary.
        # RAG/media/maintenance use their own durable allocation/source key
        # and are checked through their effect/outbox states below.
        effect_without_decision = 0
        for builder_id, tenant_id, effect_id, kind in effects:
            closed_by_deletion = (
                tenant_id is not None
                and tenant_id not in live_tenants
                and "deletion_closure:" + hashlib.sha256(effect_id.encode()).hexdigest()
                in closure_effect_ids
            )
            if (
                kind in {"trace", "trace_credit_pending", "trace_no_response"}
                and (builder_id, effect_id) not in expected
                and not closed_by_deletion
            ):
                effect_without_decision += 1
            elif (
                kind in {"rag", "media", "egress", "maintenance"}
                and (builder_id, effect_id) not in allocation_effects
                and not closed_by_deletion
            ):
                effect_without_decision += 1

        partial = 0
        for builder_id, _tenant_id, effect_id, kind in effects:
            if kind != "trace":
                continue
            debit = int(
                await self.session.scalar(
                    select(
                        func.coalesce(func.sum(CreditLedgerEntry.amount_mc), 0)
                    ).where(
                        CreditLedgerEntry.builder_id == builder_id,
                        CreditLedgerEntry.effect_id == effect_id,
                        CreditLedgerEntry.entry_type == "debit",
                    )
                )
                or 0
            )
            debt = int(
                await self.session.scalar(
                    select(func.coalesce(func.sum(CreditDebt.amount_mc), 0)).where(
                        CreditDebt.builder_id == builder_id,
                        CreditDebt.debt_source_effect_id == effect_id,
                    )
                )
                or 0
            )
            if debit + debt != TRACE_COST_MC:
                partial += 1

        orphan_outbox = 0
        for tenant_id, effect_id in (
            await self.session.execute(
                select(CreditResponseOutbox.tenant_id, CreditResponseOutbox.effect_id)
            )
        ).all():
            effect = await self.session.scalar(
                select(CreditEffect.id).where(
                    CreditEffect.tenant_id == tenant_id,
                    CreditEffect.effect_id == effect_id,
                )
            )
            if effect is None:
                orphan_outbox += 1

        handoff_without_effect = 0
        for tenant_id, context in (
            await self.session.execute(
                select(HandoffRecord.tenant_id, HandoffRecord.context)
            )
        ).all():
            effect_id = (
                context.get("credit_effect_id") if isinstance(context, dict) else None
            )
            if not isinstance(effect_id, str):
                continue
            effect = await self.session.scalar(
                select(CreditEffect.id).where(
                    CreditEffect.tenant_id == tenant_id,
                    CreditEffect.effect_id == effect_id,
                )
            )
            if effect is None:
                handoff_without_effect += 1

        report = OrphanReport(
            decision_without_effect=decision_without_effect,
            effect_without_decision=effect_without_decision,
            trace_partial_debit=partial,
            orphan_outbox=orphan_outbox,
            handoff_without_effect=handoff_without_effect,
        )
        if credit_switch_effective():
            CREDIT_ORPHAN_EFFECTS.set(report.open_count)
        return report

    async def reconcile_orphans(self) -> OrphanReport:
        """Write only idempotent deleted-tenant closure effects, then report.

        A closure preserves the original decision digest under the original
        builder account; it never changes a balance, a fact, or any ASAAS
        table.  Normal orphan classes remain blockers for a human repair.
        """

        deleted_effects = (
            await self.session.execute(
                select(CreditEffect).where(
                    CreditEffect.tenant_id.is_not(None),
                    CreditEffect.effect_kind.in_(ORPHAN_EFFECT_KINDS),
                    ~exists().where(Tenant.id == CreditEffect.tenant_id),
                )
            )
        ).scalars()
        repository = CreditRepository(self.session)
        for effect in deleted_effects:
            closure_id = (
                "deletion_closure:"
                + hashlib.sha256(effect.effect_id.encode()).hexdigest()
            )
            await repository.insert_effect(
                builder_id=effect.builder_id,
                tenant_id=None,
                effect_id=closure_id,
                effect_kind="deletion_closure",
                decision_digest=effect.decision_digest,
            )
        await self.session.flush()
        return await self.orphan_report()

    async def accept_cutoff(
        self,
        *,
        tenant_id: UUID,
        builder_id: UUID | None,
        reason: str,
        report_digest: str,
        author_user_id: UUID,
        accepted_at: datetime,
    ) -> CreditCutoffAcceptance:
        """Persist one explicit owner acknowledgement; there is intentionally no API."""

        if len(report_digest) != 64 or any(
            c not in "0123456789abcdef" for c in report_digest
        ):
            raise ValueError("report_digest must be a lowercase SHA-256")
        if not reason.strip() or accepted_at.tzinfo is None:
            raise ValueError("cutoff acceptance requires reason and aware timestamp")
        builder_clause = (
            CreditCutoffAcceptance.builder_id.is_(None)
            if builder_id is None
            else CreditCutoffAcceptance.builder_id == builder_id
        )
        accepted_on = accepted_at.astimezone(SAO_PAULO).date()
        existing = (
            await self.session.execute(
                select(CreditCutoffAcceptance).where(
                    CreditCutoffAcceptance.tenant_id == tenant_id,
                    builder_clause,
                    CreditCutoffAcceptance.reason == reason.strip(),
                    CreditCutoffAcceptance.report_digest == report_digest,
                    CreditCutoffAcceptance.author_user_id == author_user_id,
                    CreditCutoffAcceptance.accepted_on == accepted_on,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        acceptance = CreditCutoffAcceptance(
            id=uuid4(),
            tenant_id=tenant_id,
            builder_id=builder_id,
            reason=reason.strip(),
            report_digest=report_digest,
            author_user_id=author_user_id,
            accepted_at=accepted_at,
            accepted_on=accepted_on,
        )
        self.session.add(acceptance)
        await self.session.flush()
        return acceptance

    async def arm_enforcement(
        self, *, set_by: str, at: datetime | None = None
    ) -> CreditEnforcementState:
        """Internal Group-7 transaction, intentionally unreachable by a route.

        The Group 8 operational process may call this only after its own
        independent owner decision.  A second call is rejected rather than
        moving the boundary and charging history.

        ``credit_enforcement_at`` is the database clock (``clock_timestamp``,
        read through :meth:`CreditRepository.database_now` once the rollout
        lock is held) -- the instant of this transaction, never another one:

        * earlier would retroact over events already stored as observational;
        * later would not delay anything (every hot-path reader treats any
          non-NULL ``credit_enforcement_at`` as enforced from the commit), so
          events before the published boundary would stop being
          observational, and the gate would be evaluated at a future date
          with no current cycle and no traffic -- vacuously.

        An explicit ``at`` is therefore accepted only when it IS that clock.
        Readiness and the cutoff report are evaluated at the database clock,
        never at ``at``.  Tests control the instant by patching
        ``CreditRepository.database_now``.
        """

        if (at is not None and at.tzinfo is None) or not set_by:
            raise ValueError("enforcement needs an aware timestamp and actor")
        repository = CreditRepository(self.session)
        # Same lock as ASAAS subscription writers.  No Billing control fields
        # are read or written as authority by this service.
        await repository.lock_rollout_control(environment=settings.environment)
        database_now = await repository.database_now()
        when = database_now if at is None else at.astimezone(timezone.utc)
        if when < database_now:
            raise CreditEnforcementRejected(
                "credit_enforcement_at cannot be backdated before the database clock"
            )
        if when > database_now:
            raise CreditEnforcementRejected(
                "credit_enforcement_at cannot be future-dated past the database clock"
            )
        if (await self.reconcile_orphans()).open_count:
            raise CreditEnforcementRejected(
                "credit orphan reconciliation is incomplete"
            )
        ready = await self.capability_ready(now=database_now)
        if not ready.ready:
            raise CreditEnforcementRejected("credit readiness is incomplete")
        cutoff = await self.cutoff_report(now=database_now)
        if not cutoff.ready:
            raise CreditEnforcementRejected("tenant cutoff report is unaccepted")
        state = await self.session.get(CreditEnforcementState, 1, with_for_update=True)
        if state is None:
            state = CreditEnforcementState(
                id=1, credit_enforcement_at=None, set_by=None, set_at=None
            )
            self.session.add(state)
            await self.session.flush()
        if state.credit_enforcement_at is not None:
            raise CreditEnforcementRejected("credit enforcement is already armed")
        state.credit_enforcement_at = when
        state.set_by = set_by
        state.set_at = database_now
        await self.session.flush()
        return state


async def run_credit_orphan_reconciliation_once(
    session_factory: object | None = None,
) -> int:
    """Run the post-enforcement orphan pass once.

    Before the effective switch and the immutable enforcement timestamp the
    worker performs no query beyond the timestamp read and emits no metric.
    Afterward it is read-only except for idempotent deleted-tenant closure
    effects; every other orphan remains visible to the operator as a bounded
    gauge and sanitized log count.
    """

    if not credit_switch_effective():
        return 0
    if session_factory is None:
        from src.repositories.database import get_db_session_factory

        session_factory = get_db_session_factory()
    factory = session_factory
    async with factory() as session:  # type: ignore[operator]
        try:
            if await CreditRepository(session).enforcement_at() is None:
                await session.rollback()
                return 0
            report = await CreditRolloutService(session).reconcile_orphans()
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    if report.open_count:
        logger.warning(
            "credit orphan reconciliation is open",
            alert="credit_orphan_effects",
            count=report.open_count,
        )
    return report.open_count


async def run_credit_orphan_reconciliation_worker(
    interval: float = 60.0,
    *,
    session_factory: object | None = None,
) -> None:
    """Cancellable low-frequency lifecycle task; never owns rollout authority."""

    while True:
        # Match the other non-critical background maintenance: do not let a
        # just-started process contend with request/bootstrap work.  A direct
        # ``run_credit_orphan_reconciliation_once`` remains available to the
        # scheduler/tests for an immediate explicit sweep.
        await asyncio.sleep(interval)
        try:
            await run_credit_orphan_reconciliation_once(session_factory)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "credit orphan reconciliation worker failed",
                alert="credit_orphan_worker",
                error_type=type(error).__name__,
            )


def start_credit_orphan_reconciliation_worker() -> asyncio.Task[None] | None:
    """Create the dormant worker only with the existing credit hot-path config."""

    if settings.credit_hot_path_enabled is not True:
        return None
    return asyncio.create_task(
        run_credit_orphan_reconciliation_worker(), name="credit-orphan-worker"
    )


async def stop_credit_orphan_reconciliation_worker(
    task: asyncio.Task[None] | None,
) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def record_observational_write_drop(reason: str) -> None:
    """Metric-only best-effort hook used after the Group 3a log boundary."""

    if reason not in OBSERVATIONAL_DROP_REASONS or not credit_switch_effective():
        return
    CREDIT_OBSERVATIONAL_WRITE_DROPPED_TOTAL.labels(reason=reason).inc()


def derive_operational_role(
    environment: str,
    cluster_arn: str | None,
    service_arn: str | None,
    *,
    allowlist: tuple[tuple[str, str, str], ...],
) -> Literal["production-serving"] | None:
    """``production-serving`` only for an EXACT allowlisted triple.

    Deliverable 9: the role is derived from the deployment's environment and
    its ECS cluster/service ARNs against the allowlist injected by
    configuration (``settings.credit_production_serving_allowlist``, passed in
    by the caller so the function stays pure).  It never reads
    ``settings.is_production`` (false in real production, which runs
    ``ENVIRONMENT=staging``) nor AppConfig, and there is no parameter through
    which a caller could assert a role or target: anything else derives
    ``None``.
    """

    if not (environment and cluster_arn and service_arn):
        return None
    if (environment, cluster_arn, service_arn) in allowlist:
        return PRODUCTION_SERVING
    return None


def cutoff_digest(rows: list[dict[str, object]]) -> str:
    """Stable digest for a tenant-scoped cutoff report, never including PII."""

    encoded = json.dumps(
        rows, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "CapabilityReadiness",
    "BindingCoverage",
    "CutoffFinding",
    "CutoffReport",
    "CreditEnforcementRejected",
    "CreditRolloutService",
    "CREDIT_OBSERVATIONAL_WRITE_DROPPED_TOTAL",
    "CREDIT_ORPHAN_EFFECTS",
    "CREDIT_RECONCILIATION_READY",
    "AMBIGUOUS_SUBSCRIPTION_STATUSES",
    "OBSERVATIONAL_DROP_REASONS",
    "OPEN_SUBSCRIPTION_STATUSES",
    "ORPHAN_EFFECT_KINDS",
    "OrphanReport",
    "PRODUCTION_SERVING",
    "ReadbackReport",
    "cutoff_digest",
    "derive_operational_role",
    "record_observational_write_drop",
    "run_credit_orphan_reconciliation_once",
    "run_credit_orphan_reconciliation_worker",
    "start_credit_orphan_reconciliation_worker",
    "stop_credit_orphan_reconciliation_worker",
]
