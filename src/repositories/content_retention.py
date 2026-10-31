"""Caller-owned persistence for credit lifecycle, legal hold and purge (G5).

Thin, like :mod:`src.repositories.credit`: every method runs in the caller's
transaction, never commits or rolls back, and holds no business rule (those
live in :mod:`src.services.credit_lifecycle` and
:mod:`src.services.content_retention`).

Lock order.  Lifecycle writes happen under the builder's account lock (step
3 of :mod:`src.repositories.credit`), so one builder's episode is never
evaluated by two writers at once; the partial unique index
``uq_credit_lifecycle_episodes_open`` is the second guard.  The lifecycle
worker claims a builder by taking that same account row ``FOR UPDATE SKIP
LOCKED`` (:meth:`claim_account`): a builder a request is evaluating is
skipped, never waited on.  Hold events and purge fences lock, in their own
``SERIALIZABLE`` transaction, the ``credit_hold_generations`` rows of every
scope that overlaps theirs -- the ``builder`` row always, then (for a
builder-wide event or fence) every tenant row -- in the one order of
:func:`hold_scope_order`, and never take the account lock.

The only ASAAS table read here is ``billing_subscriptions.is_active``
(read-only), for the binding grace.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.models.billing import BillingSubscription
from src.models.content_retention import (
    CreditHoldEvent,
    CreditHoldGeneration,
    CreditLifecycleEpisode,
    CreditLifecycleTransition,
    CreditPurgeFence,
    CreditPurgeJob,
    CreditPurgeReceipt,
    CreditPurgeResult,
    CreditRetentionTombstone,
    CreditThresholdAlert,
)
from src.models.credit import (
    CreditAccount,
    CreditEnforcementBinding,
    CreditLedgerEntry,
    CreditLot,
)
from src.models.operational_capability import (
    CreditOperationalAuthorization,
    CreditOperationalAuthorizationReceipt,
    CreditOperationalRoleAssignment,
)

# A lot that expired within this window and has no recorded expiry is looked
# at by the lifecycle worker (an exhaustion by expiry, without traffic).
EXPIRY_LOOKBACK = timedelta(days=7)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite drops the zone of TIMESTAMPTZ; its stored values are UTC."""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def hold_scope_order(scopes: Iterable[str]) -> list[str]:
    """The fixed lock order of hold-generation rows: ``builder`` first, then
    the tenant scopes in lexical order (duplicates dropped)."""

    return sorted(set(scopes), key=lambda scope: (scope != "builder", scope))


class ContentRetentionRepository:
    """Lifecycle, hold, authorization and purge rows (caller's transaction)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @property
    def postgresql(self) -> bool:
        return self.session.get_bind().dialect.name == "postgresql"

    def _insert(self, model: type[object]) -> Any:
        if self.postgresql:
            from sqlalchemy.dialects.postgresql import insert as postgresql_insert

            return postgresql_insert(model)
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        return sqlite_insert(model)

    # -- lifecycle: episodes ---------------------------------------------

    async def open_episode(self, builder_id: UUID) -> CreditLifecycleEpisode | None:
        """The builder's open episode (at most one, by the partial index)."""

        return (
            await self.session.execute(
                select(CreditLifecycleEpisode).where(
                    CreditLifecycleEpisode.builder_id == builder_id,
                    CreditLifecycleEpisode.reactivated_at.is_(None),
                )
            )
        ).scalar_one_or_none()

    async def latest_episode(self, builder_id: UUID) -> CreditLifecycleEpisode | None:
        return (
            await self.session.execute(
                select(CreditLifecycleEpisode)
                .where(CreditLifecycleEpisode.builder_id == builder_id)
                .order_by(
                    CreditLifecycleEpisode.exhausted_at.desc(),
                    CreditLifecycleEpisode.id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()

    async def closed_episode_count(self, builder_id: UUID) -> int:
        value = await self.session.scalar(
            select(func.count())
            .select_from(CreditLifecycleEpisode)
            .where(
                CreditLifecycleEpisode.builder_id == builder_id,
                CreditLifecycleEpisode.reactivated_at.is_not(None),
            )
        )
        return int(value or 0)

    async def insert_episode(self, **values: Any) -> CreditLifecycleEpisode:
        episode = CreditLifecycleEpisode(id=values.pop("id", uuid4()), **values)
        self.session.add(episode)
        await self.session.flush()
        return episode

    async def update_episode(
        self, episode: CreditLifecycleEpisode, **changes: Any
    ) -> None:
        """Apply ``changes`` to the open episode (the guard trigger refuses
        anything but shortening, first writes and the reactivation)."""

        await self.session.execute(
            update(CreditLifecycleEpisode)
            .where(
                CreditLifecycleEpisode.id == episode.id,
                CreditLifecycleEpisode.reactivated_at.is_(None),
            )
            .values(**changes)
        )
        for name, value in changes.items():
            setattr(episode, name, value)

    async def transitions_of(self, episode_id: UUID) -> dict[str, datetime]:
        rows = await self.session.execute(
            select(
                CreditLifecycleTransition.transition,
                CreditLifecycleTransition.occurred_at,
            ).where(CreditLifecycleTransition.episode_id == episode_id)
        )
        found: dict[str, datetime] = {}
        for transition, occurred_at in rows:
            stamp = as_utc(occurred_at)
            assert stamp is not None
            found.setdefault(str(transition), stamp)
        return found

    async def insert_transition(
        self,
        *,
        builder_id: UUID,
        episode_id: UUID,
        transition: str,
        effect_id: str,
        occurred_at: datetime,
        source: str,
        detail: Mapping[str, Any] | None = None,
    ) -> bool:
        """Record one transition once (``ON CONFLICT DO NOTHING`` on the
        effect id): ``True`` when this call wrote it."""

        inserted = await self.session.execute(
            self._insert(CreditLifecycleTransition)
            .values(
                id=uuid4(),
                builder_id=builder_id,
                episode_id=episode_id,
                transition=transition,
                effect_id=effect_id,
                occurred_at=occurred_at,
                source=source,
                detail=dict(detail or {}),
            )
            .on_conflict_do_nothing(index_elements=["builder_id", "effect_id"])
            .returning(CreditLifecycleTransition.id)
        )
        return inserted.scalar_one_or_none() is not None

    # -- lifecycle: thresholds -------------------------------------------

    async def threshold_alerts(self, builder_id: UUID, arm_key: str) -> set[int]:
        rows = await self.session.execute(
            select(CreditThresholdAlert.threshold).where(
                CreditThresholdAlert.builder_id == builder_id,
                CreditThresholdAlert.arm_key == arm_key,
            )
        )
        return {int(value) for (value,) in rows}

    async def insert_threshold_alert(
        self,
        *,
        builder_id: UUID,
        arm_key: str,
        threshold: int,
        capacity_mc: int,
        available_mc: int,
        effect_id: str,
        occurred_at: datetime,
    ) -> bool:
        inserted = await self.session.execute(
            self._insert(CreditThresholdAlert)
            .values(
                id=uuid4(),
                builder_id=builder_id,
                arm_key=arm_key,
                threshold=threshold,
                capacity_mc=capacity_mc,
                available_mc=available_mc,
                effect_id=effect_id,
                occurred_at=occurred_at,
            )
            .on_conflict_do_nothing(
                index_elements=["builder_id", "arm_key", "threshold"]
            )
            .returning(CreditThresholdAlert.id)
        )
        return inserted.scalar_one_or_none() is not None

    # -- lifecycle: binding grace inputs (read-only) -----------------------

    async def active_bindings(self, builder_id: UUID) -> list[CreditEnforcementBinding]:
        return list(
            (
                await self.session.execute(
                    select(CreditEnforcementBinding)
                    .where(
                        CreditEnforcementBinding.builder_id == builder_id,
                        CreditEnforcementBinding.status == "active",
                    )
                    .order_by(
                        CreditEnforcementBinding.cycle_end.desc(),
                        CreditEnforcementBinding.id,
                    )
                )
            ).scalars()
        )

    async def subscription_is_active(self, subscription_id: UUID) -> bool:
        """``billing_subscriptions.is_active`` (an absent row is inactive)."""

        value = await self.session.scalar(
            select(BillingSubscription.is_active).where(
                BillingSubscription.id == subscription_id
            )
        )
        return bool(value)

    async def next_binding_at(
        self, subscription_id: UUID, after_cycle_end: date
    ) -> datetime | None:
        """Commit instant of the earliest binding of a following cycle."""

        value = await self.session.scalar(
            select(func.min(CreditEnforcementBinding.created_at)).where(
                CreditEnforcementBinding.subscription_id == subscription_id,
                CreditEnforcementBinding.cycle_start >= after_cycle_end,
            )
        )
        return as_utc(value)

    # -- lifecycle worker ------------------------------------------------

    async def lifecycle_candidates(
        self,
        *,
        now: datetime,
        enforcement_at: datetime,
        limit: int,
        today: date | None = None,
    ) -> list[UUID]:
        """Builders the worker must look at -- only those with work pending,
        so a backlog of quiet episodes (already suspended, nothing due) never
        fills the batch and starves a later builder.  Each source has its
        own ``limit`` budget (the expired-lot query never competes with the
        open episodes):

        * an open episode whose ``suspended`` or ``deletion_due`` deadline
          passed with that transition not recorded yet (oldest first);
        * an open episode still inside its binding grace (the first observed
          ``is_active = false`` or next binding cuts it);
        * an open episode whose builder has a monthly allowance that started
          (``cycle_start`` reached, civil day of ``today``) within
          :data:`EXPIRY_LOOKBACK` -- the reactivation an early-paid allowance
          brings at its start, with no traffic;
        * a builder with a lot that expired after the enforcement (in the
          last :data:`EXPIRY_LOOKBACK`) and no ``expiry`` entry recorded yet
          (an exhaustion without traffic)."""

        episode = CreditLifecycleEpisode
        open_ = episode.reactivated_at.is_(None)

        def recorded(name: str) -> Any:
            return exists().where(
                CreditLifecycleTransition.episode_id == episode.id,
                CreditLifecycleTransition.transition == name,
            )

        due_rows = await self.session.execute(
            select(episode.builder_id)
            .where(
                open_,
                or_(
                    and_(episode.suspension_due_at <= now, ~recorded("suspended")),
                    and_(episode.deletion_due_at <= now, ~recorded("deletion_due")),
                ),
            )
            .order_by(episode.suspension_due_at, episode.builder_id)
            .limit(limit)
        )
        grace_rows = await self.session.execute(
            select(episode.builder_id)
            .where(
                open_,
                episode.cycle_end_at.is_not(None),
                episode.grace_ends_at > now,
            )
            .order_by(episode.grace_ends_at, episode.builder_id)
            .limit(limit)
        )
        local_today = today or now.date()
        starting_rows = await self.session.execute(
            select(episode.builder_id)
            .where(
                open_,
                exists().where(
                    CreditLot.builder_id == episode.builder_id,
                    CreditLot.lot_kind == "monthly",
                    CreditLot.cycle_start.is_not(None),
                    CreditLot.cycle_start <= local_today,
                    CreditLot.cycle_start
                    >= local_today - timedelta(days=EXPIRY_LOOKBACK.days),
                ),
            )
            .order_by(episode.suspension_due_at, episode.builder_id)
            .limit(limit)
        )
        expired_rows = await self.session.execute(
            select(CreditLot.builder_id)
            .where(
                CreditLot.expires_at > max(enforcement_at, now - EXPIRY_LOOKBACK),
                CreditLot.expires_at <= now,
                ~exists().where(
                    CreditLedgerEntry.builder_id == CreditLot.builder_id,
                    CreditLedgerEntry.lot_id == CreditLot.id,
                    CreditLedgerEntry.entry_type == "expiry",
                ),
            )
            .group_by(CreditLot.builder_id)
            .order_by(CreditLot.builder_id)
            .limit(limit)
        )
        builders: list[UUID] = []
        for rows in (due_rows, grace_rows, starting_rows, expired_rows):
            for (builder_id,) in rows:
                if builder_id not in builders:
                    builders.append(builder_id)
        return builders

    async def claim_account(self, builder_id: UUID) -> bool:
        """The builder's account row ``FOR UPDATE SKIP LOCKED`` (lock-order
        step 3 without waiting): ``False`` when another writer holds it."""

        statement = select(CreditAccount.id).where(
            CreditAccount.builder_id == builder_id
        )
        if self.postgresql:
            statement = statement.with_for_update(skip_locked=True)
        return (await self.session.execute(statement)).first() is not None

    # -- operational roles and authorizations ------------------------------

    async def insert_assignment(self, **values: Any) -> CreditOperationalRoleAssignment:
        row = CreditOperationalRoleAssignment(id=values.pop("id", uuid4()), **values)
        self.session.add(row)
        await self.session.flush()
        return row

    async def assignment(
        self, assignment_id: UUID
    ) -> CreditOperationalRoleAssignment | None:
        return await self.session.get(CreditOperationalRoleAssignment, assignment_id)

    async def revocation_of(self, assignment_id: UUID) -> UUID | None:
        value = await self.session.scalar(
            select(CreditOperationalRoleAssignment.id).where(
                CreditOperationalRoleAssignment.revokes_assignment_id == assignment_id
            )
        )
        return None if value is None else UUID(str(value))

    async def active_assignment(
        self, *, user_id: UUID, role: str, environment: str, at: datetime
    ) -> CreditOperationalRoleAssignment | None:
        """A granted, unrevoked, unexpired assignment of ``role``."""

        revoked = (
            select(CreditOperationalRoleAssignment.revokes_assignment_id)
            .where(CreditOperationalRoleAssignment.action == "revoked")
            .scalar_subquery()
        )
        return (
            await self.session.execute(
                select(CreditOperationalRoleAssignment)
                .where(
                    CreditOperationalRoleAssignment.user_id == user_id,
                    CreditOperationalRoleAssignment.role == role,
                    CreditOperationalRoleAssignment.environment == environment,
                    CreditOperationalRoleAssignment.action == "granted",
                    or_(
                        CreditOperationalRoleAssignment.expires_at.is_(None),
                        CreditOperationalRoleAssignment.expires_at > at,
                    ),
                    CreditOperationalRoleAssignment.id.not_in(revoked),
                )
                .order_by(CreditOperationalRoleAssignment.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def list_assignments(
        self, environment: str
    ) -> list[CreditOperationalRoleAssignment]:
        return list(
            (
                await self.session.execute(
                    select(CreditOperationalRoleAssignment)
                    .where(CreditOperationalRoleAssignment.environment == environment)
                    .order_by(
                        CreditOperationalRoleAssignment.created_at,
                        CreditOperationalRoleAssignment.id,
                    )
                )
            ).scalars()
        )

    async def insert_authorization(
        self, **values: Any
    ) -> CreditOperationalAuthorization:
        row = CreditOperationalAuthorization(**values)
        self.session.add(row)
        await self.session.flush()
        return row

    async def authorization(
        self, authorization_id: UUID
    ) -> CreditOperationalAuthorization | None:
        return await self.session.get(CreditOperationalAuthorization, authorization_id)

    async def authorization_consumed(self, authorization_id: UUID) -> bool:
        return (
            await self.session.scalar(
                select(CreditOperationalAuthorizationReceipt.authorization_id).where(
                    CreditOperationalAuthorizationReceipt.authorization_id
                    == authorization_id
                )
            )
        ) is not None

    async def consume_authorization(
        self,
        *,
        authorization_id: UUID,
        consumed_by: UUID,
        consumed_for: str,
        result_ref: str,
        at: datetime,
    ) -> bool:
        """The single use: ``False`` when the authorization was already used."""

        inserted = await self.session.execute(
            self._insert(CreditOperationalAuthorizationReceipt)
            .values(
                authorization_id=authorization_id,
                consumed_by=consumed_by,
                consumed_for=consumed_for,
                result_ref=result_ref,
                consumed_at=at,
            )
            .on_conflict_do_nothing(index_elements=["authorization_id"])
            .returning(CreditOperationalAuthorizationReceipt.authorization_id)
        )
        return inserted.scalar_one_or_none() is not None

    # -- legal hold ----------------------------------------------------------

    async def lock_hold_generation(self, builder_id: UUID, scope: str) -> int:
        """Create the ``(builder_id, scope)`` row once, then lock it ``FOR
        UPDATE``; returns the current generation."""

        await self.session.execute(
            self._insert(CreditHoldGeneration)
            .values(builder_id=builder_id, scope=scope, generation=0)
            .on_conflict_do_nothing(index_elements=["builder_id", "scope"])
        )
        statement = select(CreditHoldGeneration.generation).where(
            CreditHoldGeneration.builder_id == builder_id,
            CreditHoldGeneration.scope == scope,
        )
        if self.postgresql:
            statement = statement.with_for_update()
        return int((await self.session.execute(statement)).scalar_one())

    async def lock_hold_generations(
        self, builder_id: UUID, scopes: Iterable[str]
    ) -> dict[str, int]:
        """:meth:`lock_hold_generation` of every scope in the ONE fixed
        order (``builder`` first, then the tenant scopes sorted), so two
        writers of overlapping scopes never wait on each other crosswise."""

        return {
            scope: await self.lock_hold_generation(builder_id, scope)
            for scope in hold_scope_order(scopes)
        }

    async def hold_generations(self, builder_id: UUID) -> dict[str, int]:
        """Every ``(scope, generation)`` row of the builder (a plain read)."""

        rows = await self.session.execute(
            select(CreditHoldGeneration.scope, CreditHoldGeneration.generation).where(
                CreditHoldGeneration.builder_id == builder_id
            )
        )
        return {str(scope): int(generation) for scope, generation in rows}

    async def bump_hold_generation(
        self, builder_id: UUID, scope: str, current: int
    ) -> int:
        await self.session.execute(
            update(CreditHoldGeneration)
            .where(
                CreditHoldGeneration.builder_id == builder_id,
                CreditHoldGeneration.scope == scope,
                CreditHoldGeneration.generation == current,
            )
            .values(generation=current + 1)
        )
        return current + 1

    async def hold_events(
        self, builder_id: UUID, scopes: Iterable[str] | None
    ) -> list[CreditHoldEvent]:
        """The hold events of ``scopes`` (``None``: every scope of the
        builder -- what a builder-wide purge must fold)."""

        statement = select(CreditHoldEvent).where(
            CreditHoldEvent.builder_id == builder_id
        )
        if scopes is not None:
            statement = statement.where(CreditHoldEvent.scope.in_(tuple(scopes)))
        return list(
            (
                await self.session.execute(
                    statement.order_by(CreditHoldEvent.generation, CreditHoldEvent.id)
                )
            ).scalars()
        )

    async def insert_hold_event(self, **values: Any) -> CreditHoldEvent:
        row = CreditHoldEvent(**values)
        self.session.add(row)
        await self.session.flush()
        return row

    async def fences(
        self, builder_id: UUID, scopes: Iterable[str]
    ) -> list[CreditPurgeFence]:
        return list(
            (
                await self.session.execute(
                    select(CreditPurgeFence).where(
                        CreditPurgeFence.builder_id == builder_id,
                        CreditPurgeFence.scope.in_(tuple(scopes)),
                    )
                )
            ).scalars()
        )

    # -- purge -----------------------------------------------------------------

    async def insert_fence(
        self,
        *,
        builder_id: UUID,
        scope: str,
        generation: int,
        operation_id: UUID,
    ) -> UUID | None:
        """The CAS insert of a purge fence: ``None`` when the generation
        already has one (never a second fence for one generation)."""

        inserted = await self.session.execute(
            self._insert(CreditPurgeFence)
            .values(
                id=uuid4(),
                builder_id=builder_id,
                scope=scope,
                generation=generation,
                operation_id=operation_id,
            )
            .on_conflict_do_nothing()
            .returning(CreditPurgeFence.id)
        )
        value = inserted.scalar_one_or_none()
        return None if value is None else UUID(str(value))

    async def fence_of(self, operation_id: UUID) -> CreditPurgeFence | None:
        return (
            await self.session.execute(
                select(CreditPurgeFence).where(
                    CreditPurgeFence.operation_id == operation_id
                )
            )
        ).scalar_one_or_none()

    async def insert_tombstone(self, **values: Any) -> CreditRetentionTombstone:
        row = CreditRetentionTombstone(id=values.pop("id", uuid4()), **values)
        self.session.add(row)
        await self.session.flush()
        return row

    async def tombstone(self, token: UUID) -> CreditRetentionTombstone | None:
        return (
            await self.session.execute(
                select(CreditRetentionTombstone).where(
                    CreditRetentionTombstone.token == token
                )
            )
        ).scalar_one_or_none()

    async def tombstones_of(self, operation_id: UUID) -> list[CreditRetentionTombstone]:
        return list(
            (
                await self.session.execute(
                    select(CreditRetentionTombstone)
                    .where(CreditRetentionTombstone.operation_id == operation_id)
                    .order_by(
                        CreditRetentionTombstone.source_kind,
                        CreditRetentionTombstone.source_id,
                    )
                )
            ).scalars()
        )

    async def insert_job_state(
        self,
        *,
        token: UUID,
        operation_id: UUID,
        state: str,
        fence_id: UUID | None = None,
    ) -> bool:
        inserted = await self.session.execute(
            self._insert(CreditPurgeJob)
            .values(
                id=uuid4(),
                token=token,
                operation_id=operation_id,
                state=state,
                fence_id=fence_id,
            )
            .on_conflict_do_nothing(index_elements=["token", "state"])
            .returning(CreditPurgeJob.id)
        )
        return inserted.scalar_one_or_none() is not None

    async def job_states(self, token: UUID) -> dict[str, UUID | None]:
        rows = await self.session.execute(
            select(CreditPurgeJob.state, CreditPurgeJob.fence_id).where(
                CreditPurgeJob.token == token
            )
        )
        return {str(state): fence_id for state, fence_id in rows}

    async def claim_unfinished_tokens(self, *, limit: int) -> list[UUID]:
        """Tokens with a job but no final result, ``FOR UPDATE SKIP LOCKED``
        on their ``prepared`` row (append-only rows can still be locked)."""

        final = exists().where(
            CreditPurgeResult.token == CreditPurgeJob.token,
            CreditPurgeResult.outcome.in_(("deleted", "held", "blocked")),
        )
        statement = (
            select(CreditPurgeJob.token)
            .where(CreditPurgeJob.state == "prepared", ~final)
            .order_by(CreditPurgeJob.created_at, CreditPurgeJob.id)
            .limit(limit)
        )
        if self.postgresql:
            statement = statement.with_for_update(skip_locked=True)
        return [row[0] for row in await self.session.execute(statement)]

    async def operations_without_receipt(self, *, limit: int) -> list[UUID]:
        """Operations whose every token already has a final result but that
        have no receipt yet -- a crash between the last result and
        :meth:`PurgeExecutor.finish`; :meth:`claim_unfinished_tokens` never
        sees them (nothing is left unfinished)."""

        other = aliased(CreditRetentionTombstone)
        unfinished = exists().where(
            other.operation_id == CreditRetentionTombstone.operation_id,
            ~exists().where(
                CreditPurgeResult.token == other.token,
                CreditPurgeResult.outcome.in_(("deleted", "held", "blocked")),
            ),
        )
        receipted = exists().where(
            CreditPurgeReceipt.operation_id == CreditRetentionTombstone.operation_id
        )
        rows = await self.session.execute(
            select(CreditRetentionTombstone.operation_id)
            .where(~receipted, ~unfinished)
            .group_by(CreditRetentionTombstone.operation_id)
            .order_by(CreditRetentionTombstone.operation_id)
            .limit(limit)
        )
        return [row[0] for row in rows]

    async def results_of(self, token: UUID) -> list[CreditPurgeResult]:
        return list(
            (
                await self.session.execute(
                    select(CreditPurgeResult)
                    .where(CreditPurgeResult.token == token)
                    .order_by(CreditPurgeResult.attempt)
                )
            ).scalars()
        )

    async def insert_result(
        self,
        *,
        token: UUID,
        operation_id: UUID,
        outcome: str,
        detail: str,
    ) -> int:
        attempt = 1 + int(
            await self.session.scalar(
                select(func.coalesce(func.max(CreditPurgeResult.attempt), 0)).where(
                    CreditPurgeResult.token == token
                )
            )
            or 0
        )
        self.session.add(
            CreditPurgeResult(
                id=uuid4(),
                token=token,
                operation_id=operation_id,
                attempt=attempt,
                outcome=outcome,
                detail=detail[:500],
            )
        )
        await self.session.flush()
        return attempt

    async def receipt(self, operation_id: UUID) -> CreditPurgeReceipt | None:
        return await self.session.get(CreditPurgeReceipt, operation_id)

    async def insert_receipt(self, **values: Any) -> CreditPurgeReceipt:
        row = CreditPurgeReceipt(**values)
        self.session.add(row)
        await self.session.flush()
        return row

    async def active_tombstone_for(
        self, source_kind: str, source_id: UUID
    ) -> CreditRetentionTombstone | None:
        """A tombstone of this object whose job has no final result yet (the
        object is hidden while it exists)."""

        final = exists().where(
            CreditPurgeResult.token == CreditRetentionTombstone.token,
            CreditPurgeResult.outcome.in_(("deleted", "held", "blocked")),
        )
        return (
            await self.session.execute(
                select(CreditRetentionTombstone)
                .where(
                    and_(
                        CreditRetentionTombstone.source_kind == source_kind,
                        CreditRetentionTombstone.source_id == source_id,
                        ~final,
                    )
                )
                .limit(1)
            )
        ).scalar_one_or_none()


__all__ = ["ContentRetentionRepository", "as_utc", "hold_scope_order"]
