"""SQLite checks for checkout eligibility and fresh locked reads (Group 2).

PostgreSQL-only behaviour (advisory locks, triggers, real concurrency) is
proven in ``tests/integration/test_asaas_checkout_service_postgres.py``; here the real
repository SQL runs on SQLite so every blocker condition and the
``populate_existing`` guarantee are covered without a database server.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.base import Base
from src.models.billing import (
    BillingCheckoutSession,
    BillingRolloutControl,
    BillingSubscription,
)
from src.repositories.billing import BillingRepository
from src.repositories.database import _patch_jsonb_for_sqlite


@pytest.fixture
async def sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    _patch_jsonb_for_sqlite()

    async def no_advisory_lock(self: BillingRepository, environment: str) -> None:
        del self, environment  # PostgreSQL-only primitive

    monkeypatch.setattr(BillingRepository, "_lock_environment", no_advisory_lock)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'billing.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    # Same session semantics as the application engine.
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _subscription(
    session: AsyncSession,
    builder_id: UUID,
    *,
    provider: str | None,
    status: str,
    is_active: bool,
) -> UUID:
    subscription = BillingSubscription(
        id=uuid4(),
        builder_id=builder_id,
        provider=provider,
        external_subscription_id=f"sub-{uuid4().hex}",
        is_active=is_active,
        plan="plus",
        billing_cycle="monthly",
        status=status,
    )
    session.add(subscription)
    await session.flush()
    return subscription.id


def _checkout(
    builder_id: UUID, *, state: str, intent_kind: str = "subscription"
) -> BillingCheckoutSession:
    order_id = uuid4()
    return BillingCheckoutSession(
        id=uuid4(),
        builder_id=builder_id,
        intent_kind=intent_kind,
        intent_id=order_id,
        topup_order_id=order_id if intent_kind == "topup" else None,
        attempt_no=1,
        provider="asaas",
        state=state,
        catalog_version_id="payment-2026-10-03",
        offer_id=(
            "plan_pro_monthly_v1"
            if intent_kind == "subscription"
            else "package_credits_500_v1"
        ),
        amount_cents=250_000 if intent_kind == "subscription" else 10_000,
        pricing_snapshot={"amount_cents": 250_000},
        next_due_date=date(2026, 10, 11) if intent_kind == "subscription" else None,
        reconciliation_attempts=0,
    )


async def _blocker(
    sessions: async_sessionmaker[AsyncSession], builder_id: UUID
) -> str | None:
    async with sessions() as session:
        return await BillingRepository(session).subscription_checkout_blocker(
            builder_id=builder_id
        )


async def test_a_builder_without_billing_rows_is_eligible(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    assert await _blocker(sessions, uuid4()) is None


SUBSCRIPTION_CASES: list[tuple[str | None, str, bool, str | None]] = [
    ("abacatepay", "active", True, "abacatepay_subscription"),
    ("abacatepay", "inactive", False, None),
    ("abacatepay", "cancelled", False, None),
    # Non-terminal legacy rows fail closed even when not billable.
    (
        "abacatepay",
        "non_billable_pending_readback",
        False,
        "abacatepay_subscription",
    ),
    ("abacatepay", "readback_required", True, "abacatepay_subscription"),
    ("asaas", "active", True, "asaas_subscription"),
    ("asaas", "active_linked", True, "asaas_subscription"),
    ("asaas", "readback_required", True, "asaas_subscription"),
    ("asaas", "non_billable_pending_readback", False, "asaas_subscription"),
    ("asaas", "inactive", False, None),
    ("asaas", "cancelled", False, None),
    (None, "legacy_cancel_pending", False, "unresolved_subscription"),
    (None, "legacy_cancelled", False, "unresolved_subscription"),
    (None, "provider_unknown", False, "unresolved_subscription"),
    (None, "manual_intervention", False, "unresolved_subscription"),
]


@pytest.mark.parametrize(
    ("provider", "status", "is_active", "expected"), SUBSCRIPTION_CASES
)
async def test_subscription_rows_block_until_terminal(
    sessions: async_sessionmaker[AsyncSession],
    provider: str | None,
    status: str,
    is_active: bool,
    expected: str | None,
) -> None:
    builder_id = uuid4()
    async with sessions() as session:
        await _subscription(
            session, builder_id, provider=provider, status=status, is_active=is_active
        )
        await session.commit()

    assert await _blocker(sessions, builder_id) == expected
    # Another builder's rows never block (tenant scope).
    assert await _blocker(sessions, uuid4()) is None


async def test_legacy_row_flipped_to_asaas_blocks_while_asaas_is_billable(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    builder_id = uuid4()
    async with sessions() as session:
        subscription_id = await _subscription(
            session, builder_id, provider="abacatepay", status="active", is_active=True
        )
        await session.commit()
    assert await _blocker(sessions, builder_id) == "abacatepay_subscription"

    async with sessions() as session:
        await session.execute(
            update(BillingSubscription)
            .where(BillingSubscription.id == subscription_id)
            .values(provider="asaas")
        )
        await session.commit()
    assert await _blocker(sessions, builder_id) == "asaas_subscription"

    async with sessions() as session:
        await session.execute(
            update(BillingSubscription)
            .where(BillingSubscription.id == subscription_id)
            .values(status="cancelled", is_active=False)
        )
        await session.commit()
    assert await _blocker(sessions, builder_id) is None


# The migration saga was removed (Group 10 phase E2) and migration 037 (E6)
# dropped billing_migrations: no saga row can exist any more, so eligibility
# reads subscription rows only.  Each saga state (migration 026) is paired
# with the subscription row that state left behind; every such shape is a
# case of test_subscription_rows_block_until_terminal above, and the shapes
# that still block are exactly the non-terminal ones.  E6 deleted the two
# tests that inserted a BillingMigration row next to each shape (the ORM no
# longer maps the table): the "saga row + live subscription" half is the
# subscription case alone, and the "leftover saga row + ended subscription"
# shape (owner-acknowledged E2 behaviour change) cannot be built at 037.
SAGA_LIVE_SUBSCRIPTION: dict[str, tuple[str | None, str, bool, str]] = {
    "pending": ("abacatepay", "active", True, "abacatepay_subscription"),
    "in_progress": (None, "legacy_cancel_pending", False, "unresolved_subscription"),
    "legacy_cancel_pending": (
        None,
        "legacy_cancel_pending",
        False,
        "unresolved_subscription",
    ),
    "legacy_cancelled": (None, "legacy_cancelled", False, "unresolved_subscription"),
    "recreate_pending": (None, "legacy_cancelled", False, "unresolved_subscription"),
    "migrated": ("asaas", "active", True, "asaas_subscription"),
    "manual_intervention": (
        None,
        "manual_intervention",
        False,
        "unresolved_subscription",
    ),
}
ENDED_SUBSCRIPTIONS = (("abacatepay", "inactive"), ("asaas", "cancelled"))
_FROZEN_036 = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "frozen"
    / "037_downgrade_restores_036.json"
)


def test_every_shape_the_retired_saga_left_behind_is_a_blocking_case() -> None:
    """The saga states come from the 036 CHECK that 037's downgrade restores
    (the only place they still exist); each state's subscription shape is a
    case above that blocks, and both ended shapes are cases that do not."""

    (check,) = [
        entry["statement"]
        for entry in json.loads(_FROZEN_036.read_text(encoding="ascii"))["constraints"]
        if entry["name"] == "ck_billing_migrations_state"
    ]
    persisted = set(re.findall(r"'([a-z_]+)'::character varying", check))

    assert persisted == set(SAGA_LIVE_SUBSCRIPTION)
    for shape in SAGA_LIVE_SUBSCRIPTION.values():
        assert shape in SUBSCRIPTION_CASES, shape
        assert shape[3] is not None
    for provider, status in ENDED_SUBSCRIPTIONS:
        assert (provider, status, False, None) in SUBSCRIPTION_CASES


@pytest.mark.parametrize(
    ("state", "intent_kind", "expected"),
    [
        ("creating", "subscription", "live_subscription_checkout"),
        ("active", "subscription", "live_subscription_checkout"),
        ("paid_pending_subscription", "subscription", "live_subscription_checkout"),
        ("reconciliation_required", "subscription", "live_subscription_checkout"),
        ("terminal_pending_readback", "subscription", "live_subscription_checkout"),
        ("failed", "subscription", None),
        ("completed", "subscription", None),
        ("abandoned", "subscription", None),
        ("active", "topup", None),
    ],
)
async def test_any_live_subscription_attempt_blocks_a_new_plan(
    sessions: async_sessionmaker[AsyncSession],
    state: str,
    intent_kind: str,
    expected: str | None,
) -> None:
    builder_id = uuid4()
    async with sessions() as session:
        session.add(_checkout(builder_id, state=state, intent_kind=intent_kind))
        await session.commit()

    assert await _blocker(sessions, builder_id) == expected


async def test_locked_reads_return_fresh_rows_for_held_references(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    builder_id = uuid4()
    async with sessions() as session:
        session.add(BillingRolloutControl(environment="test", state="general"))
        checkout = _checkout(builder_id, state="creating")
        session.add(checkout)
        await session.commit()

    async with sessions() as service:
        repository = BillingRepository(service)
        held_control = await repository.lock_rollout_control(environment="test")
        held_checkout = await repository.lock_checkout_session(
            builder_id=builder_id, checkout_session_id=checkout.id
        )
        assert held_control.state == "general"
        assert held_checkout is not None and held_checkout.state == "creating"
        await service.commit()  # expire_on_commit=False keeps both references

        async with sessions() as operator:
            await operator.execute(
                update(BillingRolloutControl)
                .where(BillingRolloutControl.environment == "test")
                .values(state="disabled")
            )
            await operator.execute(
                update(BillingCheckoutSession)
                .where(BillingCheckoutSession.id == checkout.id)
                .values(state="failed")
            )
            await operator.commit()

        control = await repository.lock_rollout_control(environment="test")
        locked = await repository.lock_checkout_session(
            builder_id=builder_id, checkout_session_id=checkout.id
        )
        assert control is held_control  # same identity, refreshed in place
        assert control.state == "disabled"
        assert locked is not None and locked.state == "failed"
