"""Real PostgreSQL proofs of the builder billing read contract (Group 6b).

Every summary here is read through the real route function inside a
``READ ONLY`` transaction (PostgreSQL then rejects any write and any
``SELECT ... FOR UPDATE``), and every ``POST /checkout`` goes through the real
route, service, repository SQL, triggers and CHECKs with an in-process
``httpx.MockTransport`` ASAAS (no network).  The harness owns its own
disposable child database, like ``test_asaas_checkout_service_postgres.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.api.routes import billing
from src.api.schemas.billing import (
    BillingSummaryResponse,
    CheckoutRequest,
    CheckoutResponse,
)
from src.config.settings import settings
from src.repositories import billing as billing_repository_module
from src.repositories.billing import BillingRepository
from src.services import billing_read_contract as read_contract_module
from src.services.asaas import AsaasClient
from src.services.billing_catalog import BILLING_CATALOG
from src.services.billing_read_contract import (
    read_commercial_availability,
    read_current_subscription,
)
from tests.api.test_billing_read_contract import (
    DATE_RELATIONS,
    LATEST_CYCLES,
    MATRIX_STATUSES,
    PAYMENT_STATE_MATRIX,
    UNREACHABLE,
)
from tests.integration.test_asaas_billing_postgres import (
    _create_child,
    _drop_child,
    _evidence,
    _run_alembic,
    _validated_admin_url,
)
from tests.integration.test_asaas_checkout_service_postgres import (
    _API_KEY,
    _builder,
    _configure,
    _enable_topup_gate,
    _Flag,
    _insert_topup_capability,
    _Provider,
    _transition,
)

pytestmark = pytest.mark.integration

_ALL_OFFERS = list(BILLING_CATALOG)
_SUBSCRIPTIONS = [o for o, offer in BILLING_CATALOG.items() if offer.cycle == "MONTHLY"]
_TOPUPS = [o for o, offer in BILLING_CATALOG.items() if offer.cycle == "DETACHED"]
_OUTCOME_ACCEPTED = (200, 202)

# Injected instants (never the host clock).  São Paulo is UTC-3.
_T = date(2026, 11, 11)
_MID_CYCLE = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)  # 10-20 12:00
_T_LATE = datetime(2026, 11, 12, 2, 30, tzinfo=timezone.utc)  # 11-11 23:30
_T_NEXT_DAY = datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc)  # 11-12 00:00


def _at(monkeypatch: pytest.MonkeyPatch, instant: datetime) -> None:
    """The route's ``today``: the summary reads ``_utc_now`` at call time."""

    monkeypatch.setattr(read_contract_module, "_utc_now", lambda: instant)


@dataclass(frozen=True)
class _Harness:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]

    def session(self) -> AsyncSession:
        return self.sessions()


@pytest.fixture(scope="module")
async def harness() -> AsyncIterator[_Harness]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_admin_url)
    admin_engine = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    name = f"neurostack_asaas_smoke_{uuid4().hex[:12]}"
    engine: AsyncEngine | None = None
    created = False
    try:
        await _create_child(admin_engine, name)
        created = True
        url = admin_url.set(database=name)
        await _run_alembic(url, "upgrade", "head")
        engine = create_async_engine(url, poolclass=NullPool)
        yield _Harness(
            engine=engine,
            sessions=async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
            ),
        )
    finally:
        if engine is not None:
            await engine.dispose()
        if created:
            await _drop_child(admin_engine, name)
        await admin_engine.dispose()


# -- route drivers ---------------------------------------------------------------


async def _summary(harness: _Harness, builder_id: UUID) -> BillingSummaryResponse:
    """The real route, in a transaction PostgreSQL enforces as read-only."""

    async with harness.session() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        response = await billing.billing_summary(builder_id, session)
        # Still the same read-only transaction: nothing committed in between.
        assert (await session.scalar(text("SHOW transaction_read_only"))) == "on"
        await session.rollback()
    return response


def _method(offer_id: str) -> str:
    return "credit_card" if BILLING_CATALOG[offer_id].cycle == "MONTHLY" else "pix"


async def _post(harness: _Harness, builder_id: UUID, offer_id: str) -> int:
    async with harness.session() as session:
        try:
            result = await billing.create_checkout(
                CheckoutRequest(offer_id=offer_id, method=_method(offer_id)),
                builder_id,
                session,
            )
        except HTTPException as exc:
            return exc.status_code
    if isinstance(result, JSONResponse):
        return result.status_code
    assert isinstance(result, CheckoutResponse)
    return 200


async def _agrees(harness: _Harness, builder_id: UUID, offer_id: str) -> int:
    """Summary immediately before a POST must predict whether it is accepted."""

    available = (await _summary(harness, builder_id)).checkout.available_offer_ids
    status = await _post(harness, builder_id, offer_id)
    assert (offer_id in available) == (status in _OUTCOME_ACCEPTED), (
        offer_id,
        status,
        available,
    )
    return status


def _wire(
    monkeypatch: pytest.MonkeyPatch, harness: _Harness, environment: str
) -> _Provider:
    """Wire the route's own creation path to the in-process ASAAS."""

    _configure(monkeypatch, environment, _Flag())
    provider = _Provider(harness)

    async def ready() -> None:
        return None

    monkeypatch.setattr(billing, "_asaas_checkout_client", provider.client)
    monkeypatch.setattr(billing, "_ensure_webhook_ready", ready)
    return provider


# -- financial fingerprint -------------------------------------------------------


async def _fingerprint(harness: _Harness) -> dict[str, tuple[int, str]]:
    """Row count and content hash of every billing/financial table."""

    async with harness.session() as session:
        tables = list((await session.execute(text(r"""
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                      AND (table_name LIKE 'billing\_%'
                           OR table_name IN ('builders', 'coupons',
                                             'transactions', 'tenants'))
                    ORDER BY table_name
                """))).scalars())
        assert "billing_checkout_sessions" in tables
        assert "billing_rollout_control" in tables
        result: dict[str, tuple[int, str]] = {}
        for table in tables:
            quoted = session.bind.dialect.identifier_preparer.quote(table)
            row = (await session.execute(text(f"""
                    SELECT count(*), md5(coalesce(string_agg(
                        md5(t::text), '' ORDER BY md5(t::text)), ''))
                    FROM {quoted} AS t
                """))).one()
            result[table] = (int(row[0]), str(row[1]))
    return result


# -- seeds ------------------------------------------------------------------------


async def _seed_subscription(
    harness: _Harness,
    builder_id: UUID,
    *,
    status: str,
    provider: str = "asaas",
    offer_id: str | None = "plan_pro_monthly_v1",
    amount_cents: int | None = 265_000,
    created_at: datetime | None = None,
) -> UUID:
    subscription_id = uuid4()
    active = status in ("active", "active_linked", "readback_required")
    async with harness.session() as session:
        async with session.begin():
            await session.execute(
                text("""
                    INSERT INTO billing_subscriptions (
                        id, builder_id, provider, external_subscription_id,
                        is_active, plan, billing_cycle, status, roster_revision,
                        catalog_version_id, offer_id, amount_cents, created_at
                    ) VALUES (
                        :id, :builder_id, :provider, :external_id, :active,
                        'pro', 'monthly', :status, 1, :catalog, :offer_id,
                        :amount_cents, :created_at
                    )
                """),
                {
                    "id": subscription_id,
                    "builder_id": builder_id,
                    "provider": provider,
                    "external_id": f"sub_{subscription_id.hex}",
                    "active": active,
                    "status": status,
                    "catalog": "payment-2026-10-03" if offer_id else None,
                    "offer_id": offer_id,
                    "amount_cents": amount_cents,
                    "created_at": created_at or datetime.now(timezone.utc),
                },
            )
    return subscription_id


async def _seed_cycle(
    harness: _Harness,
    builder_id: UUID,
    subscription_id: UUID,
    *,
    start: date,
    end: date,
    state: str,
    reversed_cents: int = 0,
    restored_cents: int = 0,
    created_at: datetime | None = None,
) -> None:
    async with harness.session() as session:
        async with session.begin():
            await session.execute(
                text("""
                    INSERT INTO billing_subscription_cycles (
                        id, builder_id, subscription_id, provider, payment_id,
                        catalog_version_id, offer_id, cycle_start, cycle_end,
                        amount_cents, credit_grant_mc, pricing_snapshot,
                        financial_snapshot, state, financial_revision,
                        settled_cents, reversed_cents, restored_cents,
                        created_at
                    ) VALUES (
                        :id, :builder_id, :subscription_id, 'asaas', :payment_id,
                        'payment-2026-10-03', 'plan_pro_monthly_v1', :start,
                        :end, 265000, 15000000, '{}'::jsonb, '{}'::jsonb,
                        :state, 0, :settled, :reversed, :restored,
                        COALESCE(CAST(:created_at AS timestamptz), now())
                    )
                """),
                {
                    "id": uuid4(),
                    "builder_id": builder_id,
                    "subscription_id": subscription_id,
                    "payment_id": f"pay_{uuid4().hex}",
                    "start": start,
                    "end": end,
                    "state": state,
                    "settled": 0 if state in ("pending", "quarantined") else 265_000,
                    "reversed": reversed_cents,
                    "restored": restored_cents,
                    "created_at": created_at,
                },
            )


async def _seed_legacy_builder_flag(
    harness: _Harness, builder_id: UUID, status: str
) -> None:
    async with harness.session() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE builders SET subscription_status = :status, "
                    "subscription_plan = 'plus' WHERE id = :id"
                ),
                {"status": status, "id": builder_id},
            )


async def _enable_topup_flag(harness: _Harness, environment: str) -> None:
    async with harness.session() as session:
        async with session.begin():
            capability_id = await _insert_topup_capability(session, environment)
    await _enable_topup_gate(harness, environment, capability_id)


async def _control_state(harness: _Harness, environment: str) -> tuple[str, bool]:
    async with harness.session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT state, topup_commercial_enabled FROM "
                    "billing_rollout_control WHERE environment = :env"
                ),
                {"env": environment},
            )
        ).one()
    return str(row[0]), bool(row[1])


async def _disable_topup_flag(harness: _Harness, environment: str) -> None:
    async with harness.session() as session:
        async with session.begin():
            repository = BillingRepository(session)
            control = await repository.lock_rollout_control(environment=environment)
            await repository.set_topup_commercial_enabled(
                environment=environment,
                expected_revision=control.topup_revision,
                enabled=False,
                evidence=_evidence(),
                disable_reason="health",
            )


async def _ensure_general(harness: _Harness, environment: str, *, topup: bool) -> None:
    """Order-independent: ``general`` with the top-up gate as requested."""

    state, enabled = await _control_state(harness, environment)
    if state == "disabled":
        placeholder = await _builder(harness, f"{environment} placeholder canary")
        await _transition(harness, environment, "canary_allowlist", placeholder)
        state = "canary_allowlist"
    if state == "canary_allowlist":
        await _transition(harness, environment, "general")
    if enabled and not topup:
        await _disable_topup_flag(harness, environment)
    elif topup and not enabled:
        await _enable_topup_flag(harness, environment)


def _invalidate_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    """Schema drift: the stored capability no longer matches the deployed
    ``billing.fact.v1`` digest, so ``_capability_is_valid`` is false."""

    monkeypatch.setattr(
        billing_repository_module,
        "authoritative_billing_fact_schema_digest",
        lambda environment: "0" * 64,
    )


# -- rollout states: available_offer_ids vs POST, top-up truth table ----------------


async def test_disabled_rollout_lists_nothing_and_post_refuses_every_offer(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = "test"
    _wire(monkeypatch, harness, environment)
    builder_id = await _builder(harness, "G6b disabled")
    before = await _fingerprint(harness)

    summary = await _summary(harness, builder_id)
    assert summary.checkout.available_offer_ids == []
    assert summary.topup_commercial_enabled is False
    for offer_id in _ALL_OFFERS:
        assert await _agrees(harness, builder_id, offer_id) == 503
    assert await _fingerprint(harness) == before

    # flag=true x disabled x valid capability -> false.
    await _enable_topup_flag(harness, environment)
    summary = await _summary(harness, builder_id)
    assert summary.topup_commercial_enabled is False
    assert summary.checkout.available_offer_ids == []
    for offer_id in _TOPUPS:
        assert await _agrees(harness, builder_id, offer_id) == 503


async def test_canary_lists_only_the_canary_builder_offers_and_agrees_with_post(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = "staging"
    provider = _wire(monkeypatch, harness, environment)
    canary = await _builder(harness, "G6b canary")
    other = await _builder(harness, "G6b canary outsider")
    await _transition(harness, environment, "canary_allowlist", canary)
    # flag=true x canary x valid capability -> false.
    await _enable_topup_flag(harness, environment)

    outsider = await _summary(harness, other)
    assert outsider.checkout.available_offer_ids == []
    assert outsider.topup_commercial_enabled is False
    for offer_id in _ALL_OFFERS:
        assert await _agrees(harness, other, offer_id) == 503

    summary = await _summary(harness, canary)
    assert summary.checkout.available_offer_ids == [
        "plan_plus_monthly_v1",
        "package_credits_500_v1",
    ]
    assert summary.topup_commercial_enabled is False
    # Outside the Plus-only / 500-credit canary: refused, and not listed.
    for offer_id in _ALL_OFFERS:
        if offer_id not in ("plan_plus_monthly_v1", "package_credits_500_v1"):
            assert await _agrees(harness, canary, offer_id) == 503
    assert provider.posts == []

    assert await _agrees(harness, canary, "plan_plus_monthly_v1") == 200
    assert await _agrees(harness, canary, "package_credits_500_v1") == 200
    assert len(provider.posts) == 2
    # Both slots are consumed: only the live attempts are still returned.
    after = await _summary(harness, canary)
    assert after.checkout.available_offer_ids == [
        "plan_plus_monthly_v1",
        "package_credits_500_v1",
    ]
    for offer_id in _ALL_OFFERS:
        await _agrees(harness, canary, offer_id)
    assert len(provider.posts) == 2  # reuse, never a second POST
    assert (await _summary(harness, other)).checkout.available_offer_ids == []


async def test_canary_builder_with_an_eligibility_blocker_is_not_offered_plus(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A separate environment: the canary builder holds a legacy subscription.
    environment = "production"
    provider = _wire(monkeypatch, harness, environment)
    canary = await _builder(harness, "G6b blocked canary")
    await _seed_subscription(
        harness,
        canary,
        status="active",
        provider="abacatepay",
        offer_id=None,
        amount_cents=None,
    )
    await _transition(harness, environment, "canary_allowlist", canary)

    summary = await _summary(harness, canary)
    # The subscription blocker hides Plus; the canary top-up is not blocked.
    assert summary.checkout.available_offer_ids == ["package_credits_500_v1"]
    assert summary.subscription is None  # legacy provider: not an ASAAS record
    assert await _agrees(harness, canary, "plan_plus_monthly_v1") == 409
    # Eligibility is decided before the canary's Plus-only check: 409 for
    # every subscription, 503 for the top-ups outside the canary offer.
    for offer_id in _SUBSCRIPTIONS:
        assert await _agrees(harness, canary, offer_id) == 409
    for offer_id in _TOPUPS[1:]:
        assert await _agrees(harness, canary, offer_id) == 503
    assert provider.posts == []


async def test_general_availability_tracks_eligibility_and_the_topup_gate(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = "development"
    provider = _wire(monkeypatch, harness, environment)
    await _ensure_general(harness, environment, topup=False)
    builder_id = await _builder(harness, "G6b general")

    # flag=false x general -> false; no top-up listed, POST refuses top-ups.
    summary = await _summary(harness, builder_id)
    assert summary.topup_commercial_enabled is False
    assert summary.checkout.available_offer_ids == _SUBSCRIPTIONS
    for offer_id in _TOPUPS:
        assert await _agrees(harness, builder_id, offer_id) == 503

    # flag=true x general x valid capability -> true.
    await _enable_topup_flag(harness, environment)
    summary = await _summary(harness, builder_id)
    assert summary.topup_commercial_enabled is True
    assert summary.checkout.available_offer_ids == _ALL_OFFERS

    # One subscription goes live: the others are blocked (409), the live one is
    # returned as-is, top-ups are unaffected.
    assert await _agrees(harness, builder_id, "plan_pro_monthly_v1") == 200
    summary = await _summary(harness, builder_id)
    assert summary.checkout.available_offer_ids == ["plan_pro_monthly_v1", *_TOPUPS]
    assert await _agrees(harness, builder_id, "plan_plus_monthly_v1") == 409
    assert await _agrees(harness, builder_id, "plan_max_monthly_v1") == 409
    assert await _agrees(harness, builder_id, "plan_pro_monthly_v1") == 200
    assert await _agrees(harness, builder_id, "package_credits_1000_v1") == 200
    posts = len(provider.posts)
    assert posts == 2

    # flag=true x general x INVALID capability -> false, and POST agrees.
    authoritative = billing_repository_module.authoritative_billing_fact_schema_digest
    _invalidate_capability(monkeypatch)
    summary = await _summary(harness, builder_id)
    assert summary.topup_commercial_enabled is False
    for offer_id in _TOPUPS:
        await _agrees(harness, builder_id, offer_id)
    # The live 1000-credit attempt is still returned; new top-ups are refused.
    assert summary.checkout.available_offer_ids == [
        "plan_pro_monthly_v1",
        "package_credits_1000_v1",
    ]
    monkeypatch.setattr(
        billing_repository_module,
        "authoritative_billing_fact_schema_digest",
        authoritative,
    )

    # An expired capability is invalid as well (clock past its expiry).
    async with harness.session() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        expired = await read_commercial_availability(
            session,
            builder_id=builder_id,
            clock=lambda: datetime.now(timezone.utc) + timedelta(hours=3),
        )
        valid = await read_commercial_availability(session, builder_id=builder_id)
        await session.rollback()
    assert expired.topup_commercial_enabled is False
    assert valid.topup_commercial_enabled is True

    # An incompatible ASAAS configuration closes everything, as the POST does.
    monkeypatch.setattr(settings, "asaas_api_key", SecretStr("not-a-key"))
    summary = await _summary(harness, builder_id)
    assert summary.checkout.available_offer_ids == []
    assert summary.topup_commercial_enabled is False
    for offer_id in ("plan_plus_monthly_v1", "package_credits_2000_v1"):
        assert await _agrees(harness, builder_id, offer_id) == 503
    monkeypatch.setattr(settings, "asaas_api_key", SecretStr(_API_KEY))

    # Blockers other than a live attempt: a non-terminal ASAAS subscription.
    blocked = await _builder(harness, "G6b general subscribed")
    await _seed_subscription(harness, blocked, status="active")
    summary = await _summary(harness, blocked)
    assert summary.checkout.available_offer_ids == _TOPUPS
    for offer_id in _SUBSCRIPTIONS:
        assert await _agrees(harness, blocked, offer_id) == 409
    assert len(provider.posts) == posts  # refused before any POST


async def test_completed_attempt_is_not_relisted_for_a_subscribed_builder(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Normal subscriber state: the Plus checkout completed and the ASAAS
    subscription is active.  Only *live* attempts are returned as-is, so the
    completed one must not bring Plus back into the list, and the POST
    agrees (409)."""

    environment = "development"
    provider = _wire(monkeypatch, harness, environment)
    await _ensure_general(harness, environment, topup=True)
    builder_id = await _builder(harness, "G6b completed subscriber")

    assert await _agrees(harness, builder_id, "plan_plus_monthly_v1") == 200
    async with harness.session() as session:
        async with session.begin():
            updated = await session.execute(
                text("""
                    UPDATE billing_checkout_sessions SET state = 'completed'
                    WHERE builder_id = :builder_id
                      AND offer_id = 'plan_plus_monthly_v1'
                      AND state = 'active'
                """),
                {"builder_id": builder_id},
            )
            assert updated.rowcount == 1
    await _seed_subscription(
        harness,
        builder_id,
        status="active",
        offer_id="plan_plus_monthly_v1",
        amount_cents=BILLING_CATALOG["plan_plus_monthly_v1"].amount_cents,
    )
    posts = len(provider.posts)

    summary = await _summary(harness, builder_id)
    assert summary.subscription is not None
    assert summary.subscription.offer_id == "plan_plus_monthly_v1"
    assert summary.checkout.live_session is None
    assert "plan_plus_monthly_v1" not in summary.checkout.available_offer_ids
    assert summary.checkout.available_offer_ids == _TOPUPS
    assert await _agrees(harness, builder_id, "plan_plus_monthly_v1") == 409
    assert len(provider.posts) == posts


# -- read-only, isolation, status source, 202 ---------------------------------------


async def test_summary_reads_change_no_financial_row_and_take_no_blocking_lock(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = "development"
    _wire(monkeypatch, harness, environment)
    await _ensure_general(harness, environment, topup=True)
    builder_id = await _builder(harness, "G6b read-only")
    await _seed_legacy_builder_flag(harness, builder_id, "active")
    assert await _post(harness, builder_id, "package_credits_500_v1") == 200
    subscription_id = await _seed_subscription(harness, builder_id, status="active")
    await _seed_cycle(
        harness,
        builder_id,
        subscription_id,
        start=date(2026, 10, 11),
        end=date(2026, 11, 11),
        state="settled",
    )
    before = await _fingerprint(harness)

    def stable(summary: BillingSummaryResponse) -> dict[str, object]:
        # ``usage.as_of`` is the read instant; everything else must repeat.
        return summary.model_dump(exclude={"usage"})

    first = await _summary(harness, builder_id)
    for _ in range(3):
        assert stable(await _summary(harness, builder_id)) == stable(first)
    assert first.topup_commercial_enabled is True
    assert await _fingerprint(harness) == before

    # Writers' locks held elsewhere do not block the summary: the rollout
    # advisory + row lock, the session, subscription and capability rows.
    async with harness.session() as holder:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": billing_repository_module.billing_advisory_key(environment)},
        )
        for statement in (
            "SELECT 1 FROM billing_rollout_control WHERE environment = :env "
            "FOR UPDATE",
            "SELECT 1 FROM billing_topup_capability_receipts "
            "WHERE environment = :env FOR UPDATE",
            "SELECT 1 FROM billing_topup_capability_bindings "
            "WHERE environment = :env FOR UPDATE",
        ):
            await holder.execute(text(statement), {"env": environment})
        await holder.execute(
            text(
                "SELECT 1 FROM billing_checkout_sessions WHERE builder_id = :b "
                "FOR UPDATE"
            ),
            {"b": builder_id},
        )
        await holder.execute(
            text(
                "SELECT 1 FROM billing_subscriptions WHERE builder_id = :b "
                "FOR UPDATE"
            ),
            {"b": builder_id},
        )
        locked = await asyncio.wait_for(_summary(harness, builder_id), timeout=10)
        await holder.rollback()
    assert stable(locked) == stable(first)
    assert await _fingerprint(harness) == before


async def test_builder_never_sees_another_builders_session_or_subscription(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = "development"
    _wire(monkeypatch, harness, environment)
    await _ensure_general(harness, environment, topup=True)
    owner = await _builder(harness, "G6b owner")
    stranger = await _builder(harness, "G6b stranger")
    assert await _post(harness, owner, "plan_max_monthly_v1") == 200
    subscription_id = await _seed_subscription(harness, owner, status="active")
    await _seed_cycle(
        harness,
        owner,
        subscription_id,
        start=date(2026, 9, 1),
        end=date(2026, 10, 1),
        state="settled",
    )

    mine = await _summary(harness, owner)
    theirs = await _summary(harness, stranger)

    assert mine.subscription is not None
    assert mine.checkout.live_session is not None
    assert mine.checkout.available_offer_ids == ["plan_max_monthly_v1", *_TOPUPS]
    assert theirs.subscription is None
    assert theirs.checkout.live_session is None
    assert theirs.checkout.available_offer_ids == _ALL_OFFERS
    serialized = theirs.model_dump_json()
    assert mine.checkout.live_session.checkout_session_id not in serialized
    # The stranger's POST is not blocked by the owner's state either.
    assert await _agrees(harness, stranger, "plan_plus_monthly_v1") == 200


async def test_subscription_status_comes_from_the_financial_record(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(monkeypatch, harness, "development")
    await _ensure_general(harness, "development", topup=True)

    # Legacy flag says active; the financial record is linked, never settled.
    linked = await _builder(harness, "G6b linked")
    await _seed_legacy_builder_flag(harness, linked, "active")
    await _seed_subscription(harness, linked, status="active_linked")
    summary = await _summary(harness, linked)
    assert summary.subscription_status == "active"  # legacy, untouched
    assert summary.subscription is not None
    assert summary.subscription.model_dump(mode="json") == {
        "offer_id": "plan_pro_monthly_v1",
        "catalog_version_id": "payment-2026-10-03",
        "status": "active_linked",
        "amount_cents": 265_000,
        "current_period_end": None,
        # Never paid: pending whatever the day.
        "payment_state": "pending",
    }

    # Legacy flag still trial; settled cycles define the period end.
    settled = await _builder(harness, "G6b settled")
    old = await _seed_subscription(
        harness,
        settled,
        status="cancelled",
        created_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    current = await _seed_subscription(harness, settled, status="active")
    for start, end, state in (
        (date(2026, 9, 11), date(2026, 10, 11), "settled"),
        (date(2026, 10, 11), date(2026, 11, 11), "settled"),
        (date(2026, 11, 11), date(2026, 12, 11), "pending"),
        (date(2026, 12, 11), date(2027, 1, 11), "reversed"),
    ):
        await _seed_cycle(harness, settled, current, start=start, end=end, state=state)
    await _seed_cycle(
        harness,
        settled,
        old,
        start=date(2027, 1, 11),
        end=date(2027, 2, 11),
        state="settled",
    )
    summary = await _summary(harness, settled)
    assert summary.subscription_status == "trial"
    assert summary.subscription is not None
    assert summary.subscription.status == "active"
    assert summary.subscription.current_period_end == date(2026, 11, 11)
    # The newest cycle (2026-12-11) is reversed: reversed whatever the day,
    # and the cancelled row's later paid cycle never leaks into the current one.
    assert summary.subscription.payment_state == "reversed"

    # Only terminal or legacy-provider rows: no current ASAAS subscription.
    ended = await _builder(harness, "G6b ended")
    await _seed_subscription(harness, ended, status="cancelled")
    await _seed_subscription(
        harness,
        ended,
        status="active",
        provider="abacatepay",
        offer_id=None,
        amount_cents=None,
    )
    assert (await _summary(harness, ended)).subscription is None


async def _period_end(
    harness: _Harness, cycles: list[tuple[date, date, str, int, int]]
) -> date | None:
    """A fresh builder's ``current_period_end`` over the seeded cycles."""

    builder_id = await _builder(harness, "G6b period end")
    subscription_id = await _seed_subscription(harness, builder_id, status="active")
    for start, end, state, reversed_cents, restored_cents in cycles:
        await _seed_cycle(
            harness,
            builder_id,
            subscription_id,
            start=start,
            end=end,
            state=state,
            reversed_cents=reversed_cents,
            restored_cents=restored_cents,
        )
    summary = await _summary(harness, builder_id)
    assert summary.subscription is not None
    return summary.subscription.current_period_end


async def test_period_end_counts_only_cycles_fully_paid_net_of_reversals(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(monkeypatch, harness, "development")
    october = (date(2026, 10, 11), date(2026, 11, 11))
    november = (date(2026, 11, 11), date(2026, 12, 11))
    full = 265_000

    # A fully restored cycle is paid again: its end counts.
    assert await _period_end(harness, [(*october, "restored", full, full)]) == date(
        2026, 11, 11
    )
    # A partially restored cycle is still short of money: it does not count.
    assert await _period_end(harness, [(*october, "restored", full, 1)]) is None
    # A reversal of only part of a settled cycle is not "fully paid" either
    # (the conservative owner-policy default).
    assert await _period_end(harness, [(*october, "reversed", 1, 0)]) is None
    # Previous cycle fully paid, current one reversed: the previous end.
    assert await _period_end(
        harness,
        [(*october, "settled", 0, 0), (*november, "reversed", full, 0)],
    ) == date(2026, 11, 11)
    # ...and once the current one is fully restored, its own end.
    assert await _period_end(
        harness,
        [(*october, "settled", 0, 0), (*november, "restored", full, full)],
    ) == date(2026, 12, 11)
    # Pending and quarantined cycles never count.
    assert (
        await _period_end(
            harness,
            [(*october, "pending", 0, 0), (*november, "quarantined", 0, 0)],
        )
        is None
    )


async def test_two_current_subscriptions_fail_the_summary_closed(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(monkeypatch, harness, "development")
    builder_id = await _builder(harness, "G6b two current subscriptions")
    await _seed_subscription(harness, builder_id, status="active")
    await _seed_subscription(harness, builder_id, status="active_linked")
    with pytest.raises(HTTPException) as raised:
        await _summary(harness, builder_id)
    assert (raised.value.status_code, raised.value.detail) == (
        503,
        "billing_summary_unavailable",
    )


async def test_live_session_is_the_newest_live_attempt_only(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = "development"
    _wire(monkeypatch, harness, environment)
    await _ensure_general(harness, environment, topup=True)
    builder_id = await _builder(harness, "G6b live ordering")
    assert await _post(harness, builder_id, "plan_plus_monthly_v1") == 200
    assert await _post(harness, builder_id, "package_credits_500_v1") == 200

    async def session_id(kind: str) -> str:
        async with harness.session() as session:
            return str(
                await session.scalar(
                    text(
                        "SELECT id FROM billing_checkout_sessions "
                        "WHERE builder_id = :b AND intent_kind = :k"
                    ),
                    {"b": builder_id, "k": kind},
                )
            )

    subscription_id, topup_id = await session_id("subscription"), await session_id(
        "topup"
    )
    live = (await _summary(harness, builder_id)).checkout.live_session
    assert live is not None
    assert (live.checkout_session_id, live.kind) == (topup_id, "topup")
    # Owner A3: the offer of that very attempt, from its own session row.
    assert live.offer_id == await _session_offer(harness, topup_id)
    assert live.offer_id == "package_credits_500_v1"

    # The newest attempt ends: it is no longer a live session, the older live
    # one is returned instead.
    async with harness.session() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE billing_checkout_sessions SET state = 'expired' "
                    "WHERE id = :id"
                ),
                {"id": UUID(topup_id)},
            )
    live = (await _summary(harness, builder_id)).checkout.live_session
    assert live is not None
    assert (live.checkout_session_id, live.kind, live.state, live.offer_id) == (
        subscription_id,
        "subscription",
        "active",
        "plan_plus_monthly_v1",
    )
    assert live.offer_id == await _session_offer(harness, subscription_id)


async def _session_offer(harness: _Harness, session_id: str) -> str:
    async with harness.session() as session:
        value = await session.scalar(
            text("SELECT offer_id FROM billing_checkout_sessions WHERE id = :id"),
            {"id": UUID(session_id)},
        )
    assert isinstance(value, str)
    return value


async def _consume_bound_receipt(harness: _Harness, environment: str) -> None:
    """Keep the active capability valid but make its typed binding stale."""

    async with harness.session() as session:
        async with session.begin():
            await session.execute(
                text("""
                    UPDATE billing_operation_receipts SET applied_at = now()
                    WHERE id = (
                        SELECT b.verification_receipt_id
                        FROM billing_topup_capability_bindings b
                        JOIN billing_rollout_control c
                          ON c.active_topup_capability_id = b.capability_receipt_id
                        WHERE c.environment = :env AND b.environment = :env
                    )
                """),
                {"env": environment},
            )


async def test_read_path_flag_requires_the_current_capability_binding(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = "development"
    _wire(monkeypatch, harness, environment)
    await _ensure_general(harness, environment, topup=True)
    builder_id = await _builder(harness, "G6b stale binding")
    assert (await _summary(harness, builder_id)).topup_commercial_enabled is True

    await _consume_bound_receipt(harness, environment)
    try:
        summary = await _summary(harness, builder_id)
        assert await _control_state(harness, environment) == ("general", True)
        assert summary.topup_commercial_enabled is False
        assert summary.checkout.available_offer_ids == _SUBSCRIPTIONS
        for offer_id in _TOPUPS:
            assert await _agrees(harness, builder_id, offer_id) == 503
    finally:
        # Leave a valid gate behind for the tests that share the environment.
        await _disable_topup_flag(harness, environment)
        await _enable_topup_flag(harness, environment)


async def _summary_after_failure(
    harness: _Harness, builder_id: UUID
) -> BillingSummaryResponse:
    """The real route; the transaction may be aborted by the injected error."""

    async with harness.session() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        response = await billing.billing_summary(builder_id, session)
        await session.rollback()
    return response


async def test_advisory_read_failure_is_marked_and_confined_to_that_request(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = "development"
    _wire(monkeypatch, harness, environment)
    await _ensure_general(harness, environment, topup=True)
    failing = await _builder(harness, "G6b advisory failure")
    neighbour = await _builder(harness, "G6b advisory neighbour")
    assert await _post(harness, failing, "plan_plus_monthly_v1") == 200
    baseline = await _summary(harness, failing)
    assert baseline.checkout.available is True
    assert baseline.checkout.live_session is not None
    before = await _fingerprint(harness)

    original = BillingRepository.subscription_checkout_blocker

    async def blocker(self: BillingRepository, *, builder_id: UUID) -> str | None:
        if builder_id == failing:
            # A real server-side error (intentional probe): it aborts the
            # request's transaction after the financial reads.
            await self.session.execute(text("SELECT 1 FROM g6b_missing_relation"))
        return await original(self, builder_id=builder_id)

    monkeypatch.setattr(BillingRepository, "subscription_checkout_blocker", blocker)
    failed = await _summary_after_failure(harness, failing)
    # Fail-closed for buying, and explicitly marked as unavailable.
    assert failed.checkout.model_dump() == {
        "available_offer_ids": [],
        "live_session": None,
        "available": False,
    }
    assert failed.topup_commercial_enabled is False
    # The legacy fields and the financial reads are untouched.
    unaffected = {"usage", "checkout", "topup_commercial_enabled"}
    assert failed.model_dump(exclude=unaffected) == baseline.model_dump(
        exclude=unaffected
    )
    # Another builder's request, at the same time, is unaffected.
    other = await _summary(harness, neighbour)
    assert other.checkout.available is True
    assert other.checkout.available_offer_ids == _ALL_OFFERS
    assert other.topup_commercial_enabled is True

    monkeypatch.setattr(BillingRepository, "subscription_checkout_blocker", original)
    recovered = await _summary(harness, failing)
    assert recovered.model_dump(exclude={"usage"}) == baseline.model_dump(
        exclude={"usage"}
    )
    assert await _fingerprint(harness) == before


async def test_post_202_carries_the_stored_reservation_id(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = "development"
    _wire(monkeypatch, harness, environment)
    await _ensure_general(harness, environment, topup=True)
    builder_id = await _builder(harness, "G6b uncertain outcome")
    posts: list[str] = []

    async def timeout(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content)["externalReference"])
        raise httpx.ReadTimeout("read timeout", request=request)

    def client() -> AsaasClient:
        return AsaasClient(
            api_key=SecretStr(_API_KEY),
            user_agent="NeuroStack/test",
            transport=httpx.MockTransport(timeout),
        )

    monkeypatch.setattr(billing, "_asaas_checkout_client", client)
    async with harness.session() as session:
        result = await billing.create_checkout(
            CheckoutRequest(offer_id="plan_plus_monthly_v1", method="credit_card"),
            builder_id,
            session,
        )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 202
    body = json.loads(result.body)
    assert list(body) == ["detail", "checkout_session_id"]
    assert body["detail"] == "billing_checkout_reconciliation_required"
    assert posts == [body["checkout_session_id"]]
    async with harness.session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT builder_id, state, intent_kind FROM "
                    "billing_checkout_sessions WHERE id = :id"
                ),
                {"id": UUID(body["checkout_session_id"])},
            )
        ).one()
    assert (row.builder_id, row.state, row.intent_kind) == (
        builder_id,
        "reconciliation_required",
        "subscription",
    )
    live = (await _summary(harness, builder_id)).checkout.live_session
    assert live is not None
    assert live.model_dump() == {
        "checkout_session_id": body["checkout_session_id"],
        "kind": "subscription",
        "state": "reconciliation_required",
        "offer_id": "plan_plus_monthly_v1",
    }


async def _post_202(
    harness: _Harness, builder_id: UUID, offer_id: str
) -> dict[str, str]:
    async with harness.session() as session:
        result = await billing.create_checkout(
            CheckoutRequest(offer_id=offer_id, method=_method(offer_id)),
            builder_id,
            session,
        )
    assert isinstance(result, JSONResponse)
    assert result.status_code == 202
    body = json.loads(result.body)
    assert list(body) == ["detail", "checkout_session_id"]
    assert body["detail"] == "billing_checkout_reconciliation_required"
    return body


async def _session_row(harness: _Harness, checkout_session_id: str) -> Any:
    async with harness.session() as session:
        return (
            await session.execute(
                text(
                    "SELECT builder_id, state, offer_id FROM "
                    "billing_checkout_sessions WHERE id = :id"
                ),
                {"id": UUID(checkout_session_id)},
            )
        ).one()


async def test_post_202_after_a_generic_client_failure_carries_the_reservation_id(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = "development"
    _wire(monkeypatch, harness, environment)
    await _ensure_general(harness, environment, topup=True)
    builder_id = await _builder(harness, "G6b generic client failure")
    posts: list[str] = []

    async def exploding(request: httpx.Request) -> httpx.Response:
        # Not an httpx error and not an ASAAS classification: the service's
        # generic ``except Exception`` branch after the permit was handed over.
        posts.append(json.loads(request.content)["externalReference"])
        raise RuntimeError("custom transport failure")

    def client() -> AsaasClient:
        return AsaasClient(
            api_key=SecretStr(_API_KEY),
            user_agent="NeuroStack/test",
            transport=httpx.MockTransport(exploding),
        )

    monkeypatch.setattr(billing, "_asaas_checkout_client", client)
    body = await _post_202(harness, builder_id, "package_credits_500_v1")

    assert posts == [body["checkout_session_id"]]
    row = await _session_row(harness, body["checkout_session_id"])
    assert (row.builder_id, row.state, row.offer_id) == (
        builder_id,
        "reconciliation_required",
        "package_credits_500_v1",
    )


async def test_post_202_when_saving_the_provider_result_fails_carries_the_id(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = "development"
    provider = _wire(monkeypatch, harness, environment)
    await _ensure_general(harness, environment, topup=True)
    builder_id = await _builder(harness, "G6b save failure")
    original = BillingRepository.lock_checkout_session

    async def failing_after_post(self: Any, **kwargs: Any) -> Any:
        # Only the post-provider persistence step fails.
        if provider.posts:
            raise RuntimeError("could not persist the provider identity")
        return await original(self, **kwargs)

    monkeypatch.setattr(BillingRepository, "lock_checkout_session", failing_after_post)
    body = await _post_202(harness, builder_id, "plan_plus_monthly_v1")

    assert len(provider.posts) == 1
    assert provider.posts[0]["externalReference"] == body["checkout_session_id"]
    # The provider identity was never saved: the stored reservation is the one
    # the 202 names, still awaiting exact-ID reconciliation.
    row = await _session_row(harness, body["checkout_session_id"])
    assert (row.builder_id, row.state, row.offer_id) == (
        builder_id,
        "creating",
        "plan_plus_monthly_v1",
    )


async def test_legacy_summary_fields_are_unchanged_on_postgres(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(monkeypatch, harness, "development")
    builder_id = await _builder(harness, "G6b legacy")
    async with harness.session() as session:
        async with session.begin():
            for index in range(4):
                await session.execute(
                    text("""
                        INSERT INTO tenants (id, name, slug, api_key_hash,
                                             builder_id, is_active, config)
                        VALUES (:id, :name, :slug, :hash, :builder_id, true,
                                '{}'::jsonb)
                    """),
                    {
                        "id": uuid4(),
                        "name": f"G6b clinic {index}",
                        "slug": f"g6b-{index}-{uuid4().hex}",
                        "hash": f"hash-{uuid4().hex}",
                        "builder_id": builder_id,
                    },
                )
    summary = (await _summary(harness, builder_id)).model_dump(mode="json")
    legacy = {
        key: summary[key]
        for key in (
            "plan",
            "active_clinics",
            "free_tier",
            "extra_clinics",
            "base_price",
            "extra_price",
            "total_monthly",
            "total_annual",
            "discount_pct",
            "subscription_status",
            "billing_cycle",
        )
    }
    assert legacy == {
        "plan": None,
        "active_clinics": 4,
        "free_tier": 2,
        "extra_clinics": 2,
        "base_price": 2000,
        "extra_price": 700,
        "total_monthly": 2700,
        "total_annual": 27540,
        "discount_pct": 15,
        "subscription_status": "trial",
        "billing_cycle": "monthly",
    }
    assert list(summary) == [
        *legacy,
        "usage",
        "subscription",
        "checkout",
        "topup_commercial_enabled",
    ]


# -- owner A1/A2/A4/A5: payment_state and the cancelled paid window ----------------

_RELATION_END: dict[str, date] = {
    "before": date(2026, 11, 10),
    "today": _T,
    "after": date(2026, 11, 12),
}


async def _seed_matrix_cell(
    harness: _Harness, status: str, latest: str, relation: str
) -> tuple[UUID, date | None]:
    """One fresh builder: the newest cycle ``latest`` and, unless that cycle is
    itself fully paid, an older settled cycle ending on the ``relation`` day."""

    builder_id = await _builder(harness, f"G6b-2 matrix {status} {latest}")
    subscription_id = await _seed_subscription(harness, builder_id, status=status)
    cycle = LATEST_CYCLES[latest]
    if cycle is None:
        return builder_id, None
    state, _settled, reversed_cents, restored_cents = cycle
    fully_paid = state in ("settled", "restored") and reversed_cents == restored_cents
    paid_through = None if relation == "none" else _RELATION_END[relation]
    if fully_paid:
        assert paid_through is not None
        start, end = paid_through - timedelta(days=31), paid_through
    else:
        if paid_through is None:
            start = date(2026, 10, 11)
        else:
            await _seed_paid_cycles(harness, builder_id, subscription_id, paid_through)
            start = paid_through  # the newest cycle starts where that one ends
        end = start + timedelta(days=30)
    await _seed_cycle(
        harness,
        builder_id,
        subscription_id,
        start=start,
        end=end,
        state=state,
        reversed_cents=reversed_cents,
        restored_cents=restored_cents,
    )
    return builder_id, paid_through


# Every possible cell (the impossible ones are pinned by the unit matrix).
_POSSIBLE_CELLS = [
    (status, latest, relation, expected)
    for status in MATRIX_STATUSES
    for latest in LATEST_CYCLES
    for relation, expected in zip(
        DATE_RELATIONS, PAYMENT_STATE_MATRIX[(status, latest)], strict=True
    )
    if expected != UNREACHABLE
]


@pytest.mark.parametrize(("status", "latest", "relation", "expected"), _POSSIBLE_CELLS)
async def test_payment_state_matrix_on_postgres(
    harness: _Harness, status: str, latest: str, relation: str, expected: str
) -> None:
    """The literal matrix through the real SQL, at 23:30 in São Paulo on the
    boundary day (already the next day in UTC)."""

    builder_id, paid_through = await _seed_matrix_cell(
        harness, status, latest, relation
    )
    async with harness.session() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        readback = await read_current_subscription(
            session, builder_id=builder_id, clock=lambda: _T_LATE
        )
        await session.rollback()
    if expected == "null":
        assert readback is None
        return
    assert readback is not None
    assert (
        readback.status,
        readback.payment_state,
        readback.current_period_end,
        readback.offer_id,
        readback.amount_cents,
    ) == (status, expected, paid_through, "plan_pro_monthly_v1", 265_000)


async def _seed_paid_cycles(
    harness: _Harness, builder_id: UUID, subscription_id: UUID, *ends: date
) -> None:
    for end in ends:
        await _seed_cycle(
            harness,
            builder_id,
            subscription_id,
            start=end - timedelta(days=31),
            end=end,
            state="settled",
        )


async def test_cancelled_subscription_keeps_its_paid_period_then_turns_null(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner A2: cancelled mid-cycle keeps the paid period through its last
    calendar day in São Paulo, then the whole object is ``null``."""

    _wire(monkeypatch, harness, "development")
    builder_id = await _builder(harness, "G6b-2 cancelled mid-cycle")
    await _seed_legacy_builder_flag(harness, builder_id, "active")
    subscription_id = await _seed_subscription(
        harness,
        builder_id,
        status="cancelled",
        offer_id="plan_plus_monthly_v1",
        amount_cents=99_900,
    )
    await _seed_paid_cycles(
        harness, builder_id, subscription_id, date(2026, 10, 11), date(2026, 11, 11)
    )
    expected = {
        "offer_id": "plan_plus_monthly_v1",
        "catalog_version_id": "payment-2026-10-03",
        "status": "cancelled",
        "amount_cents": 99_900,
        "current_period_end": "2026-11-11",
        "payment_state": "paid",
    }
    for instant in (_MID_CYCLE, _T_LATE):
        _at(monkeypatch, instant)
        summary = await _summary(harness, builder_id)
        assert summary.subscription is not None
        assert summary.subscription.model_dump(mode="json") == expected
        # Owner A4: the exact JSON string, a date without a time zone.
        assert '"current_period_end":"2026-11-11"' in summary.model_dump_json()

    # 00:00 on the next São Paulo day: the paid period is over.
    _at(monkeypatch, _T_NEXT_DAY)
    summary = await _summary(harness, builder_id)
    assert summary.subscription is None
    # Owner A5: the legacy fields still say active; they are never the proof.
    assert (summary.plan, summary.subscription_status) == ("plus", "active")


async def test_ended_legacy_and_inactive_subscriptions_are_null_with_legacy_active(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner A5 and the A2 mapping: only a provider-deleted (``cancelled``)
    ASAAS subscription inside its paid period is shown."""

    _wire(monkeypatch, harness, "development")
    _at(monkeypatch, _T_LATE)

    async def flagged(label: str) -> UUID:
        builder_id = await _builder(harness, label)
        await _seed_legacy_builder_flag(harness, builder_id, "active")
        return builder_id

    # Cancelled after its paid period ended (yesterday in São Paulo).
    expired = await flagged("G6b-2 cancelled and expired")
    expired_sub = await _seed_subscription(harness, expired, status="cancelled")
    await _seed_paid_cycles(harness, expired, expired_sub, date(2026, 11, 10))
    # Ended as INACTIVE/EXPIRED at the provider, even if paid through tomorrow.
    inactive = await flagged("G6b-2 inactive but paid")
    inactive_sub = await _seed_subscription(harness, inactive, status="inactive")
    await _seed_paid_cycles(harness, inactive, inactive_sub, date(2026, 11, 12))
    # Legacy (non-ASAAS) rows, live or cancelled, even with paid cycles.
    legacy_live = await flagged("G6b-2 legacy live")
    live_sub = await _seed_subscription(
        harness,
        legacy_live,
        status="active",
        provider="abacatepay",
        offer_id=None,
        amount_cents=None,
    )
    await _seed_paid_cycles(harness, legacy_live, live_sub, date(2026, 11, 12))
    legacy_cancelled = await flagged("G6b-2 legacy cancelled")
    cancelled_sub = await _seed_subscription(
        harness, legacy_cancelled, status="cancelled", provider="abacatepay"
    )
    await _seed_paid_cycles(
        harness, legacy_cancelled, cancelled_sub, date(2026, 11, 12)
    )

    for builder_id in (expired, inactive, legacy_live, legacy_cancelled):
        summary = await _summary(harness, builder_id)
        assert summary.subscription is None, builder_id
        assert (summary.plan, summary.subscription_status) == ("plus", "active")


async def test_route_payment_states_for_reversal_restoration_and_pending_evidence(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(monkeypatch, harness, "development")
    _at(monkeypatch, _T_LATE)
    october = (date(2026, 10, 11), date(2026, 11, 11))
    november = (date(2026, 11, 11), date(2026, 12, 11))
    full = 265_000

    async def state_of(
        status: str, cycles: list[tuple[date, date, str, int, int]]
    ) -> tuple[str, date | None]:
        builder_id = await _builder(harness, f"G6b-2 route {status}")
        subscription_id = await _seed_subscription(harness, builder_id, status=status)
        for start, end, state, reversed_cents, restored_cents in cycles:
            await _seed_cycle(
                harness,
                builder_id,
                subscription_id,
                start=start,
                end=end,
                state=state,
                reversed_cents=reversed_cents,
                restored_cents=restored_cents,
            )
        summary = await _summary(harness, builder_id)
        assert summary.subscription is not None
        return summary.subscription.payment_state, (
            summary.subscription.current_period_end
        )

    paid = (*october, "settled", 0, 0)
    # Paid through today: paid; the renewal then reversed or partially
    # restored: reversed, with the previous paid end still reported.
    assert await state_of("active", [paid]) == ("paid", date(2026, 11, 11))
    assert await state_of("active", [paid, (*november, "reversed", full, 0)]) == (
        "reversed",
        date(2026, 11, 11),
    )
    assert await state_of("active", [paid, (*november, "restored", full, 1)]) == (
        "reversed",
        date(2026, 11, 11),
    )
    assert await state_of("active", [paid, (*november, "restored", full, full)]) == (
        "paid",
        date(2026, 12, 11),
    )
    # Only a partially restored cycle, never fully paid: reversed, no end.
    assert await state_of("active", [(*october, "restored", full, 1)]) == (
        "reversed",
        None,
    )
    # Pending provider evidence and quarantine never claim paid.
    assert await state_of("readback_required", [paid]) == (
        "pending",
        date(2026, 11, 11),
    )
    assert await state_of("non_billable_pending_readback", [paid]) == (
        "pending",
        date(2026, 11, 11),
    )
    assert await state_of("active", [paid, (*november, "quarantined", 0, 0)]) == (
        "pending",
        date(2026, 11, 11),
    )
    assert await state_of("active_linked", []) == ("pending", None)
    # The renewal never came: overdue from the next São Paulo day on.
    _at(monkeypatch, _T_NEXT_DAY)
    assert await state_of("active", [paid]) == ("overdue", date(2026, 11, 11))


async def test_cancelled_selection_prefers_the_longest_paid_period_and_live_rows(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(monkeypatch, harness, "development")
    _at(monkeypatch, _T_LATE)
    now = datetime.now(timezone.utc)
    builder_id = await _builder(harness, "G6b-2 two cancelled")
    # Older cancelled Plus paid through 11-20; newer cancelled Pro through
    # 11-15; the newest cancelled Max never paid.
    plus = await _seed_subscription(
        harness,
        builder_id,
        status="cancelled",
        offer_id="plan_plus_monthly_v1",
        created_at=now - timedelta(days=60),
    )
    await _seed_paid_cycles(harness, builder_id, plus, date(2026, 11, 20))
    pro = await _seed_subscription(
        harness,
        builder_id,
        status="cancelled",
        offer_id="plan_pro_monthly_v1",
        created_at=now - timedelta(days=30),
    )
    await _seed_paid_cycles(harness, builder_id, pro, date(2026, 11, 15))
    await _seed_subscription(
        harness,
        builder_id,
        status="cancelled",
        offer_id="plan_max_monthly_v1",
        created_at=now - timedelta(days=1),
    )
    summary = await _summary(harness, builder_id)
    assert summary.subscription is not None
    assert (
        summary.subscription.offer_id,
        summary.subscription.current_period_end,
    ) == ("plan_plus_monthly_v1", date(2026, 11, 20))

    # Same paid-through day: the newer subscription.
    await _seed_paid_cycles(harness, builder_id, pro, date(2026, 11, 20))
    summary = await _summary(harness, builder_id)
    assert summary.subscription is not None
    assert summary.subscription.offer_id == "plan_pro_monthly_v1"

    # A current (non-terminal) subscription is always the one returned.
    await _seed_subscription(
        harness, builder_id, status="active_linked", offer_id="plan_max_monthly_v1"
    )
    summary = await _summary(harness, builder_id)
    assert summary.subscription is not None
    assert summary.subscription.model_dump(mode="json") == {
        "offer_id": "plan_max_monthly_v1",
        "catalog_version_id": "payment-2026-10-03",
        "status": "active_linked",
        "amount_cents": 265_000,
        "current_period_end": None,
        "payment_state": "pending",
    }
    # ...and a second current one still fails closed, cancelled rows or not.
    await _seed_subscription(harness, builder_id, status="active")
    with pytest.raises(HTTPException) as raised:
        await _summary(harness, builder_id)
    assert (raised.value.status_code, raised.value.detail) == (
        503,
        "billing_summary_unavailable",
    )


async def test_cancelled_but_paid_builder_keeps_checkout_eligibility_unchanged(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The A2 window is display only: ``available_offer_ids``, the blocker and
    ``POST /checkout`` behave exactly as for a builder with no subscription,
    and the summary agrees with the POST."""

    environment = "development"
    provider = _wire(monkeypatch, harness, environment)
    await _ensure_general(harness, environment, topup=True)
    _at(monkeypatch, _MID_CYCLE)
    cancelled = await _builder(harness, "G6b-2 cancelled still paid")
    fresh = await _builder(harness, "G6b-2 never subscribed")
    subscription_id = await _seed_subscription(
        harness, cancelled, status="cancelled", offer_id="plan_plus_monthly_v1"
    )
    await _seed_paid_cycles(harness, cancelled, subscription_id, date(2026, 11, 11))

    async def blocker(builder_id: UUID) -> str | None:
        async with harness.session() as session:
            return await BillingRepository(session).subscription_checkout_blocker(
                builder_id=builder_id
            )

    mine = await _summary(harness, cancelled)
    theirs = await _summary(harness, fresh)
    assert mine.subscription is not None
    assert (mine.subscription.status, mine.subscription.payment_state) == (
        "cancelled",
        "paid",
    )
    assert theirs.subscription is None
    assert mine.checkout == theirs.checkout
    assert mine.checkout.available_offer_ids == _ALL_OFFERS
    assert mine.topup_commercial_enabled is theirs.topup_commercial_enabled is True
    assert await blocker(cancelled) is None and await blocker(fresh) is None

    posts = len(provider.posts)
    assert await _agrees(harness, cancelled, "plan_pro_monthly_v1") == 200
    assert len(provider.posts) == posts + 1
    after = await _summary(harness, cancelled)
    # Still the cancelled record inside its period; the new attempt is the
    # live session, with its own offer.
    assert after.subscription is not None
    assert after.subscription.status == "cancelled"
    assert after.checkout.live_session is not None
    assert (after.checkout.live_session.kind, after.checkout.live_session.offer_id) == (
        "subscription",
        "plan_pro_monthly_v1",
    )
    assert after.checkout.available_offer_ids == ["plan_pro_monthly_v1", *_TOPUPS]
    assert await _agrees(harness, cancelled, "plan_plus_monthly_v1") == 409


async def test_newest_cycle_tie_on_cycle_start_is_the_last_created(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two cycles with the same ``cycle_start`` (same due date): the newest is
    the one created last, in both orders (explicit timestamps, never the
    host clock)."""

    _wire(monkeypatch, harness, "development")
    _at(monkeypatch, _MID_CYCLE)
    first = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    later = first + timedelta(hours=1)

    async def payment_state(order: tuple[str, str]) -> tuple[str, date | None]:
        builder_id = await _builder(harness, f"G6b-2 tie {order}")
        subscription_id = await _seed_subscription(harness, builder_id, status="active")
        for state, created_at in zip(order, (first, later), strict=True):
            await _seed_cycle(
                harness,
                builder_id,
                subscription_id,
                start=date(2026, 10, 11),
                end=date(2026, 11, 11),
                state=state,
                reversed_cents=265_000 if state == "reversed" else 0,
                created_at=created_at,
            )
        summary = await _summary(harness, builder_id)
        assert summary.subscription is not None
        return (
            summary.subscription.payment_state,
            summary.subscription.current_period_end,
        )

    # The settled one still bounds the paid period either way.
    assert await payment_state(("settled", "reversed")) == (
        "reversed",
        date(2026, 11, 11),
    )
    assert await payment_state(("reversed", "settled")) == ("paid", date(2026, 11, 11))
