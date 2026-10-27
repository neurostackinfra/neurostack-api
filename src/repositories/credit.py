"""Caller-owned persistence primitives for the credit ledger (thin).

Every method runs in the caller's transaction: it reads, locks, inserts and
flushes, and never commits, rolls back or begins a nested transaction.  It
holds no business rule (grants, FEFO, expiry, reversal, debt): those are the
services of later groups, built on these primitives.

Scope.  The builder is never taken from a client: the caller derives it from
the authenticated tenant with :meth:`CreditRepository.tenant_scope`
(``tenants.builder_id`` re-read in the database) and every builder-owned read
or lock below is filtered by it.  Per-tenant rows (ingress claims,
observational effects) are always read with their ``tenant_id``.

Lock order -- the same for every credit writer (spend, fact inbox,
reconciliation, metering), so two writers never wait on each other crosswise:

0. ``tenant_scope(tenant_id)`` -- a plain read, no lock: a rejected request
   (unknown, deactivated or foreign tenant) stops here without locking or
   writing anything;
1. ``tenant_scope(tenant_id, lock=True)`` -- the tenant row ``FOR SHARE``,
   BEFORE touching any claim, response-outbox row or credit effect of that
   tenant.  Migration 038 takes this lock anyway, later, inside the
   tenant/builder trigger of claims and effects (and the claim/outbox
   foreign keys take ``FOR KEY SHARE``), while ``DELETE FROM tenants`` locks
   the tenant row first and then cascades into its claims and outbox rows.
   Taken late -- after a claim row is already locked -- it deadlocks against
   a concurrent clinic delete; taken first, the delete simply waits for the
   credit transaction (or the credit transaction waits, then finds the
   tenant gone).  It also keeps the builder scope stable: a concurrent
   reparent waits.  A standalone tenant (``builder_id`` NULL) has no account
   and stops after its per-tenant rows;
2. :meth:`~CreditRepository.lock_account` -- the builder's
   ``credit_accounts`` row ``FOR UPDATE``, the serialization point of the
   builder's credits (create it first with
   :meth:`~CreditRepository.ensure_account`);
3. :meth:`~CreditRepository.lock_lots` -- the builder's ``credit_lots``
   ``FOR UPDATE`` in ``id`` order;
4. claim and outbox updates, inserts: effects, ledger entries, debts,
   settlements, outbox.

One transaction locks one tenant and one builder's account; should two ever
be needed, they are locked in ascending id order.  No lock is held across an
LLM, provider or network call.  ``ensure_account`` relies on READ COMMITTED
(the default): after a concurrent insert wins, the re-read sees its commit.

Derived balance.  ``amount_mc`` is always positive and ``entry_type`` gives
its direction (:data:`LOT_INCREASES`, :data:`LOT_DECREASES`,
:data:`DEBT_INCREASES`, :data:`DEBT_DECREASES`); :func:`fold_ledger` is the
reference definition and :meth:`CreditRepository.derived_balance` computes
the same in SQL.  Expiry is an explicit ``expiry`` entry, never inferred from
``expires_at`` here.

Fact consumer (Group 2).  The inbox, revision-gap and enforcement-binding
primitives below serve :mod:`src.services.credit_fact_consumer`; they too run
in the caller's transaction, under the builder's account lock.  The only
ASAAS tables read here are read-only lookups the wish allows: the
``billing_outbox`` row of a presented fact (its aggregate identity) and the
``quarantined`` state of the builder's ``billing_topup_orders`` /
``billing_subscription_cycles`` (the spend hold).  Nothing here writes an
ASAAS table.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import DateTime, and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.billing import (
    BillingOutboxEvent,
    BillingSubscriptionCycle,
    BillingTopupOrder,
)
from src.models.credit import (
    ENFORCEMENT_STATE_ID,
    LEDGER_ENTRY_TYPES,
    CreditAccount,
    CreditDebt,
    CreditDebtOrigin,
    CreditDebtSettlement,
    CreditEffect,
    CreditEnforcementBinding,
    CreditEnforcementState,
    CreditIngressClaim,
    CreditLedgerEntry,
    CreditLot,
    CreditObservationalEffect,
)
from src.models.credit_fact_inbox import CreditFactInbox, CreditRevisionGap
from src.models.tenant import Tenant

# Direction of each entry type on a lot's available balance and on the
# builder's open debt.  ``debt_settlement`` moves both: the settling lot pays
# the debt.  Every type in LEDGER_ENTRY_TYPES appears at least once
# (tests/models/test_credit_models.py).
LOT_INCREASES: Final = frozenset({"grant", "settlement_release", "lot_regrant"})
LOT_DECREASES: Final = frozenset({"debit", "expiry", "reversal", "debt_settlement"})
DEBT_INCREASES: Final = frozenset({"debt"})
DEBT_DECREASES: Final = frozenset({"debt_settlement", "debt_release"})


@dataclass(frozen=True)
class TenantScope:
    """The authenticated tenant and its builder as stored now (``None`` for
    a standalone tenant)."""

    tenant_id: UUID
    builder_id: UUID | None


@dataclass(frozen=True)
class LedgerMovement:
    """The part of a ledger entry the derived balance reads."""

    entry_type: str
    amount_mc: int
    lot_id: UUID | None


@dataclass(frozen=True)
class DerivedBalance:
    """Per-lot ledger balance (lots any lot-moving entry names) and open
    debt, both in millicredits."""

    lots: Mapping[UUID, int] = field(default_factory=dict)
    debt_mc: int = 0

    @property
    def lots_total_mc(self) -> int:
        return sum(self.lots.values())


@dataclass(frozen=True)
class FactOutboxRow:
    """Read-only identity of a presented fact's ``billing_outbox`` row."""

    outbox_event_id: UUID
    builder_id: UUID
    schema_version: str
    aggregate_kind: str
    aggregate_id: UUID
    financial_revision: int
    subscription_id: UUID | None
    topup_order_id: UUID | None
    subscription_cycle_id: UUID | None


def _as_utc(value: datetime) -> datetime:
    """SQLite drops the zone of TIMESTAMPTZ; its stored values are UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def fold_ledger(movements: Iterable[LedgerMovement]) -> DerivedBalance:
    """Reference definition of the derived balance (no I/O)."""

    lots: dict[UUID, int] = {}
    debt = 0
    for movement in movements:
        if movement.entry_type not in LEDGER_ENTRY_TYPES:
            raise ValueError(f"unknown ledger entry type {movement.entry_type!r}")
        if movement.amount_mc <= 0:
            raise ValueError("ledger amounts are positive millicredits")
        if movement.entry_type in LOT_INCREASES | LOT_DECREASES:
            if movement.lot_id is None:
                raise ValueError(f"{movement.entry_type} entries name a lot")
            sign = 1 if movement.entry_type in LOT_INCREASES else -1
            lots[movement.lot_id] = (
                lots.get(movement.lot_id, 0) + sign * movement.amount_mc
            )
        if movement.entry_type in DEBT_INCREASES:
            debt += movement.amount_mc
        elif movement.entry_type in DEBT_DECREASES:
            debt -= movement.amount_mc
    return DerivedBalance(lots=lots, debt_mc=debt)


class CreditRepository:
    """Builder- and tenant-scoped reads, locks and idempotent inserts."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _insert(self, model: type[object]) -> Any:
        if self.session.get_bind().dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as postgresql_insert

            return postgresql_insert(model)
        # SQLite is the unit-test dialect; both support ON CONFLICT DO NOTHING.
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        return sqlite_insert(model)

    # -- scope -----------------------------------------------------------

    async def tenant_scope(
        self, tenant_id: UUID, *, lock: bool = False
    ) -> TenantScope | None:
        """Re-read the tenant's builder; ``None`` if the tenant is gone.

        ``lock=False`` is lock-order step 0 (no lock).  ``lock=True`` is step
        1: the tenant row ``FOR SHARE``, held to the end of the transaction,
        taken before any claim, outbox row or effect of the tenant (see the
        module docstring); re-check the returned builder, it may have changed
        since step 0.
        """

        statement = select(Tenant.id, Tenant.builder_id).where(Tenant.id == tenant_id)
        if lock:
            statement = statement.with_for_update(read=True)
        row = (await self.session.execute(statement)).one_or_none()
        if row is None:
            return None
        return TenantScope(tenant_id=row.id, builder_id=row.builder_id)

    # -- account and lots (lock order steps 2 and 3) ----------------------

    async def ensure_account(self, builder_id: UUID) -> UUID:
        """Return the builder's account id, creating the account once.

        A concurrent creator waits on ``uq_credit_accounts_builder`` and then
        inserts nothing; the re-read returns the winner's row.
        """

        inserted = (
            await self.session.execute(
                self._insert(CreditAccount)
                .values(id=uuid4(), builder_id=builder_id)
                .on_conflict_do_nothing(index_elements=["builder_id"])
                .returning(CreditAccount.id)
            )
        ).scalar_one_or_none()
        if inserted is not None:
            return UUID(str(inserted))
        existing = (
            await self.session.execute(
                select(CreditAccount.id).where(CreditAccount.builder_id == builder_id)
            )
        ).scalar_one()
        return UUID(str(existing))

    async def lock_account(self, builder_id: UUID) -> CreditAccount | None:
        """Row-lock the builder's account (``FOR UPDATE``)."""

        return (
            await self.session.execute(
                select(CreditAccount)
                .where(CreditAccount.builder_id == builder_id)
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def lock_lots(self, builder_id: UUID) -> list[CreditLot]:
        """Row-lock every lot of the builder, in ``id`` order."""

        return list(
            (
                await self.session.execute(
                    select(CreditLot)
                    .where(CreditLot.builder_id == builder_id)
                    .order_by(CreditLot.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )

    async def derived_balance(self, builder_id: UUID) -> DerivedBalance:
        """:func:`fold_ledger` of the builder's ledger, computed in SQL."""

        entry = CreditLedgerEntry
        lot_delta = case(
            (entry.entry_type.in_(sorted(LOT_INCREASES)), entry.amount_mc),
            else_=-entry.amount_mc,
        )
        lot_rows = (
            await self.session.execute(
                select(entry.lot_id, func.sum(lot_delta))
                .where(
                    entry.builder_id == builder_id,
                    entry.entry_type.in_(sorted(LOT_INCREASES | LOT_DECREASES)),
                )
                .group_by(entry.lot_id)
            )
        ).all()
        debt_delta = case(
            (entry.entry_type.in_(sorted(DEBT_INCREASES)), entry.amount_mc),
            else_=-entry.amount_mc,
        )
        debt = (
            await self.session.execute(
                select(func.coalesce(func.sum(debt_delta), 0)).where(
                    entry.builder_id == builder_id,
                    entry.entry_type.in_(sorted(DEBT_INCREASES | DEBT_DECREASES)),
                )
            )
        ).scalar_one()
        return DerivedBalance(
            lots={UUID(str(lot_id)): int(total) for lot_id, total in lot_rows},
            debt_mc=int(debt),
        )

    # -- builder-scoped reads --------------------------------------------

    async def get_effect(self, builder_id: UUID, effect_id: str) -> CreditEffect | None:
        return (
            await self.session.execute(
                select(CreditEffect).where(
                    CreditEffect.builder_id == builder_id,
                    CreditEffect.effect_id == effect_id,
                )
            )
        ).scalar_one_or_none()

    # -- tenant-scoped reads ---------------------------------------------

    async def get_idempotency_claim(
        self, tenant_id: UUID, *, principal_id: str, idempotency_key: str
    ) -> CreditIngressClaim | None:
        """The ``POST /v1/events`` claim of this tenant, principal and key."""

        return (
            await self.session.execute(
                select(CreditIngressClaim).where(
                    CreditIngressClaim.tenant_id == tenant_id,
                    CreditIngressClaim.principal_id == principal_id,
                    CreditIngressClaim.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()

    async def get_provider_claim(
        self, tenant_id: UUID, *, source: str, provider_message_id: str
    ) -> CreditIngressClaim | None:
        """The provider-webhook claim of this tenant, source and message."""

        return (
            await self.session.execute(
                select(CreditIngressClaim).where(
                    CreditIngressClaim.tenant_id == tenant_id,
                    CreditIngressClaim.source == source,
                    CreditIngressClaim.provider_message_id == provider_message_id,
                )
            )
        ).scalar_one_or_none()

    async def get_observational_effect(
        self, tenant_id: UUID, effect_id: str
    ) -> CreditObservationalEffect | None:
        return (
            await self.session.execute(
                select(CreditObservationalEffect).where(
                    CreditObservationalEffect.tenant_id == tenant_id,
                    CreditObservationalEffect.effect_id == effect_id,
                )
            )
        ).scalar_one_or_none()

    # -- global reads ----------------------------------------------------

    async def enforcement_at(self) -> datetime | None:
        """``credit_enforcement_at``; ``None`` while no row or no instant."""

        return (
            await self.session.execute(
                select(CreditEnforcementState.credit_enforcement_at).where(
                    CreditEnforcementState.id == ENFORCEMENT_STATE_ID
                )
            )
        ).scalar_one_or_none()

    async def inbox_high_watermark(self) -> int:
        """The largest ``inbox_seq`` of the fact inbox, ``0`` when empty."""

        value = (
            await self.session.execute(
                select(func.coalesce(func.max(CreditFactInbox.inbox_seq), 0))
            )
        ).scalar_one()
        return int(value)

    # -- ledger state (Group 2; under the account lock) ---------------------

    async def lot_entry_sums(self, builder_id: UUID) -> dict[UUID, dict[str, int]]:
        """Per lot, the sum of its ledger entries by ``entry_type``."""

        entry = CreditLedgerEntry
        rows = (
            await self.session.execute(
                select(entry.lot_id, entry.entry_type, func.sum(entry.amount_mc))
                .where(entry.builder_id == builder_id, entry.lot_id.is_not(None))
                .group_by(entry.lot_id, entry.entry_type)
            )
        ).all()
        sums: dict[UUID, dict[str, int]] = {}
        for lot_id, entry_type, total in rows:
            sums.setdefault(UUID(str(lot_id)), {})[str(entry_type)] = int(total)
        return sums

    async def database_now(self) -> datetime:
        """The database's current instant (PostgreSQL ``clock_timestamp()``:
        the real time, not the transaction start; SQLite: UTC now)."""

        clock = (
            func.clock_timestamp(type_=DateTime(timezone=True))
            if self.session.get_bind().dialect.name == "postgresql"
            else func.current_timestamp()
        )
        value = (await self.session.execute(select(clock))).scalar_one()
        if isinstance(value, str):  # SQLite returns its text form
            value = datetime.fromisoformat(value)
        return _as_utc(value)

    async def latest_occurred_at(self, builder_id: UUID) -> datetime | None:
        """The latest ``occurred_at`` of the builder's ledger (its clock)."""

        value = (
            await self.session.execute(
                select(func.max(CreditLedgerEntry.occurred_at)).where(
                    CreditLedgerEntry.builder_id == builder_id
                )
            )
        ).scalar_one_or_none()
        return None if value is None else _as_utc(value)

    async def debts(self, builder_id: UUID) -> list[CreditDebt]:
        """The builder's debts in settlement order ``(created_at, id)``."""

        return list(
            (
                await self.session.execute(
                    select(CreditDebt)
                    .where(CreditDebt.builder_id == builder_id)
                    .order_by(CreditDebt.created_at, CreditDebt.id)
                )
            )
            .scalars()
            .all()
        )

    async def settlement_sums(self, builder_id: UUID) -> dict[UUID, dict[UUID, int]]:
        """Per debt, how much each settling lot paid of it."""

        settlement = CreditDebtSettlement
        rows = (
            await self.session.execute(
                select(
                    settlement.debt_id,
                    settlement.settling_grant_lot_id,
                    func.sum(settlement.amount_mc),
                )
                .where(settlement.builder_id == builder_id)
                .group_by(settlement.debt_id, settlement.settling_grant_lot_id)
            )
        ).all()
        sums: dict[UUID, dict[UUID, int]] = {}
        for debt_id, lot_id, total in rows:
            sums.setdefault(UUID(str(debt_id)), {})[UUID(str(lot_id))] = int(total)
        return sums

    async def effect_ids_of_kinds(
        self, builder_id: UUID, kinds: Iterable[str]
    ) -> list[str]:
        return [
            str(effect_id)
            for effect_id in (
                await self.session.execute(
                    select(CreditEffect.effect_id).where(
                        CreditEffect.builder_id == builder_id,
                        CreditEffect.effect_kind.in_(list(kinds)),
                    )
                )
            ).scalars()
        ]

    async def existing_effect_ids(
        self, builder_id: UUID, effect_ids: Iterable[str]
    ) -> set[str]:
        wanted = sorted(set(effect_ids))
        if not wanted:
            return set()
        return {
            str(effect_id)
            for effect_id in (
                await self.session.execute(
                    select(CreditEffect.effect_id).where(
                        CreditEffect.builder_id == builder_id,
                        CreditEffect.effect_id.in_(wanted),
                    )
                )
            ).scalars()
        }

    async def release_sums(
        self, builder_id: UUID
    ) -> list[tuple[str, str, UUID | None, int]]:
        """``debt_release``/``settlement_release`` totals per effect and lot."""

        entry = CreditLedgerEntry
        rows = (
            await self.session.execute(
                select(
                    entry.effect_id,
                    entry.entry_type,
                    entry.lot_id,
                    func.sum(entry.amount_mc),
                )
                .where(
                    entry.builder_id == builder_id,
                    entry.entry_type.in_(("debt_release", "settlement_release")),
                )
                .group_by(entry.effect_id, entry.entry_type, entry.lot_id)
            )
        ).all()
        return [
            (
                str(effect_id),
                str(entry_type),
                None if lot_id is None else UUID(str(lot_id)),
                int(total),
            )
            for effect_id, entry_type, lot_id, total in rows
        ]

    async def unlinked_consumption_entries(
        self, builder_id: UUID, lot_id: UUID
    ) -> list[tuple[UUID, int]]:
        """Consumption entries of a lot, latest first, not yet linked to a
        debt that names the lot as its original grant."""

        entry = CreditLedgerEntry
        linked = (
            select(CreditDebtOrigin.consumption_entry_id)
            .join(
                CreditDebt,
                and_(
                    CreditDebt.builder_id == CreditDebtOrigin.builder_id,
                    CreditDebt.id == CreditDebtOrigin.debt_id,
                ),
            )
            .where(
                CreditDebt.builder_id == builder_id,
                CreditDebt.original_grant_lot_id == lot_id,
            )
        )
        rows = (
            await self.session.execute(
                select(entry.id, entry.amount_mc)
                .where(
                    entry.builder_id == builder_id,
                    entry.lot_id == lot_id,
                    entry.entry_type.in_(("debit", "debt_settlement")),
                    entry.id.not_in(linked),
                )
                .order_by(
                    entry.occurred_at.desc(), entry.created_at.desc(), entry.id.desc()
                )
            )
        ).all()
        return [(UUID(str(entry_id)), int(amount)) for entry_id, amount in rows]

    async def quarantine_hold(self, builder_id: UUID) -> bool:
        """Read-only: the builder has a ``quarantined`` ASAAS order or cycle."""

        orders = (
            select(BillingTopupOrder.id)
            .where(
                BillingTopupOrder.builder_id == builder_id,
                BillingTopupOrder.state == "quarantined",
            )
            .exists()
        )
        cycles = (
            select(BillingSubscriptionCycle.id)
            .where(
                BillingSubscriptionCycle.builder_id == builder_id,
                BillingSubscriptionCycle.state == "quarantined",
            )
            .exists()
        )
        return bool(await self.session.scalar(select(or_(orders, cycles))))

    # -- fact inbox, revision gaps and the enforcement binding (Group 2) ----

    async def fact_outbox_row(self, outbox_event_id: UUID) -> FactOutboxRow | None:
        """Read-only lookup of a presented fact's producer row."""

        outbox = BillingOutboxEvent
        row = (
            await self.session.execute(
                select(
                    outbox.id,
                    outbox.builder_id,
                    outbox.schema_version,
                    outbox.aggregate_kind,
                    outbox.aggregate_id,
                    outbox.financial_revision,
                    outbox.subscription_id,
                    outbox.topup_order_id,
                    outbox.subscription_cycle_id,
                ).where(outbox.id == outbox_event_id)
            )
        ).one_or_none()
        if row is None:
            return None
        return FactOutboxRow(
            outbox_event_id=row.id,
            builder_id=row.builder_id,
            schema_version=row.schema_version,
            aggregate_kind=row.aggregate_kind,
            aggregate_id=row.aggregate_id,
            financial_revision=int(row.financial_revision),
            subscription_id=row.subscription_id,
            topup_order_id=row.topup_order_id,
            subscription_cycle_id=row.subscription_cycle_id,
        )

    async def inbox_by_event(
        self, producer: str, event_id: UUID, schema_version: str
    ) -> CreditFactInbox | None:
        return (
            await self.session.execute(
                select(CreditFactInbox).where(
                    CreditFactInbox.producer == producer,
                    CreditFactInbox.event_id == event_id,
                    CreditFactInbox.schema_version == schema_version,
                )
            )
        ).scalar_one_or_none()

    async def inbox_by_revision(
        self,
        producer: str,
        aggregate_kind: str,
        aggregate_id: UUID,
        financial_revision: int,
    ) -> CreditFactInbox | None:
        return (
            await self.session.execute(
                select(CreditFactInbox).where(
                    CreditFactInbox.producer == producer,
                    CreditFactInbox.aggregate_kind == aggregate_kind,
                    CreditFactInbox.aggregate_id == aggregate_id,
                    CreditFactInbox.financial_revision == financial_revision,
                )
            )
        ).scalar_one_or_none()

    async def inbox_revisions(
        self, producer: str, aggregate_kind: str, aggregate_id: UUID
    ) -> dict[int, str]:
        """``financial_revision -> outcome`` of every inbox row of an aggregate."""

        rows = (
            await self.session.execute(
                select(
                    CreditFactInbox.financial_revision, CreditFactInbox.outcome
                ).where(
                    CreditFactInbox.producer == producer,
                    CreditFactInbox.aggregate_kind == aggregate_kind,
                    CreditFactInbox.aggregate_id == aggregate_id,
                )
            )
        ).all()
        return {int(revision): str(outcome) for revision, outcome in rows}

    async def insert_inbox(
        self,
        *,
        producer: str,
        event_id: UUID,
        schema_version: str,
        aggregate_kind: str,
        aggregate_id: UUID,
        financial_revision: int,
        fact_sha256: str,
        bundle_digest: str,
        schema_digest: str,
        outcome: str,
        received_at: datetime,
    ) -> CreditFactInbox:
        """Append one inbox row (``inbox_seq`` is generated by PostgreSQL)."""

        row = CreditFactInbox(
            id=uuid4(),
            producer=producer,
            event_id=event_id,
            schema_version=schema_version,
            aggregate_kind=aggregate_kind,
            aggregate_id=aggregate_id,
            financial_revision=financial_revision,
            fact_sha256=fact_sha256,
            bundle_digest=bundle_digest,
            schema_digest=schema_digest,
            outcome=outcome,
            received_at=received_at,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def open_revision_gaps(
        self, aggregate_kind: str, aggregate_id: UUID
    ) -> list[CreditRevisionGap]:
        gap = CreditRevisionGap
        return list(
            (
                await self.session.execute(
                    select(gap)
                    .where(
                        gap.aggregate_kind == aggregate_kind,
                        gap.aggregate_id == aggregate_id,
                        gap.closed_at.is_(None),
                    )
                    .order_by(gap.first_missing_revision)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )

    async def open_revision_gap(
        self,
        aggregate_kind: str,
        aggregate_id: UUID,
        first_missing_revision: int,
        opened_at: datetime,
    ) -> bool:
        """Open a gap once (``True`` when this call inserted it)."""

        inserted = (
            await self.session.execute(
                self._insert(CreditRevisionGap)
                .values(
                    aggregate_kind=aggregate_kind,
                    aggregate_id=aggregate_id,
                    first_missing_revision=first_missing_revision,
                    opened_at=opened_at,
                )
                .on_conflict_do_nothing()
                .returning(CreditRevisionGap.first_missing_revision)
            )
        ).scalar_one_or_none()
        return inserted is not None

    async def close_revision_gap(
        self, gap: CreditRevisionGap, closed_at: datetime
    ) -> None:
        """Close a gap once; ``closed_at`` never precedes ``opened_at``."""

        gap.closed_at = max(_as_utc(gap.opened_at), _as_utc(closed_at))
        await self.session.flush()

    async def binding_for_cycle(
        self, subscription_cycle_id: UUID
    ) -> CreditEnforcementBinding | None:
        """The binding of one ASAAS cycle, row-locked for its update."""

        return (
            await self.session.execute(
                select(CreditEnforcementBinding)
                .where(
                    CreditEnforcementBinding.subscription_cycle_id
                    == subscription_cycle_id
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def binding_for_period(
        self, subscription_id: UUID, cycle_start: date, cycle_end: date
    ) -> CreditEnforcementBinding | None:
        return (
            await self.session.execute(
                select(CreditEnforcementBinding).where(
                    CreditEnforcementBinding.subscription_id == subscription_id,
                    CreditEnforcementBinding.cycle_start == cycle_start,
                    CreditEnforcementBinding.cycle_end == cycle_end,
                )
            )
        ).scalar_one_or_none()

    async def insert_binding(
        self,
        *,
        builder_id: UUID,
        subscription_id: UUID,
        subscription_cycle_id: UUID,
        cycle_start: date,
        cycle_end: date,
        financial_revision: int,
        grant_effect_id: str,
        status: str,
        at: datetime,
    ) -> CreditEnforcementBinding:
        binding = CreditEnforcementBinding(
            id=uuid4(),
            builder_id=builder_id,
            subscription_id=subscription_id,
            subscription_cycle_id=subscription_cycle_id,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
            financial_revision=financial_revision,
            grant_effect_id=grant_effect_id,
            status=status,
            created_at=at,
            updated_at=at,
        )
        self.session.add(binding)
        await self.session.flush()
        return binding

    async def update_binding(
        self,
        binding: CreditEnforcementBinding,
        *,
        financial_revision: int,
        status: str,
        at: datetime,
    ) -> None:
        """Advance the binding (its cycle identity never changes)."""

        binding.financial_revision = financial_revision
        binding.status = status
        binding.updated_at = at
        await self.session.flush()
