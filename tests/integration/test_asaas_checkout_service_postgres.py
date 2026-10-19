"""Real PostgreSQL proofs of the Group 2 checkout *service* path.

Every checkout here goes through ``BillingCheckoutService.create_checkout``
with the real repository SQL, triggers and CHECKs, the real rollout guards and
the real ``AsaasClient`` on an in-process ``httpx.MockTransport`` (no network).
The harness reuses the helpers of ``test_asaas_billing_postgres.py`` and owns
its own disposable child database.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.config.settings import settings
from src.models.billing import BillingCheckoutSession, BillingRolloutControl
from src.repositories.billing import BillingCapabilityRejected, BillingRepository
from src.services import billing_checkout as checkout_module
from src.services.asaas import AsaasClient
from src.services.billing_checkout import (
    BillingCheckoutService,
    CheckoutNotEligible,
    CheckoutUnavailable,
    CheckoutView,
)
from src.services.billing_outbox import billing_fact_schema_digest
from src.services.billing_read_contract import read_commercial_availability
from src.services.egress_policy import EgressPolicy
from tests.integration.test_asaas_billing_postgres import (
    _create_child,
    _drop_child,
    _evidence,
    _run_alembic,
    _seed_capability_binding,
    _validated_admin_url,
)

pytestmark = pytest.mark.integration

_API_KEY = "$aact_prod_service-path-proof"
# Provider checkout ids are globally unique (uq_..._provider_external).
_PROVIDER_IDS = itertools.count(1)
# 23:59:59 on 2026-10-11 in São Paulo.
_CLOCK = datetime(2026, 10, 12, 2, 59, 59, tzinfo=timezone.utc)


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
            # The application session semantics (src/repositories/database.py).
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


class _Flag:
    """Financial-egress flag reader: open, closed or unreadable."""

    def __init__(self) -> None:
        self.value: object = True
        self.error: Exception | None = None
        self.calls = 0

    async def is_enabled(self, flag_name: str) -> bool:
        assert flag_name == "financial_egress_enabled"
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.value  # type: ignore[return-value]


@dataclass
class _Provider:
    """In-process ASAAS: records every POST and answers like ``/v3/checkouts``."""

    harness: _Harness
    posts: list[dict[str, Any]] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    states_at_post: list[tuple[str, ...]] = field(default_factory=list)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://api.asaas.com/v3/checkouts"
        body = json.loads(request.content)
        self.posts.append(body)
        reference = UUID(body["externalReference"])
        # The reservation must already be committed (visible to another
        # connection) and still ``creating`` when the request leaves.
        async with self.harness.session() as observer:
            row = (
                await observer.execute(
                    text("SELECT state FROM billing_checkout_sessions WHERE id = :id"),
                    {"id": reference},
                )
            ).one_or_none()
        self.states_at_post.append(tuple(row) if row is not None else ())
        self.ids.append(f"chk_{next(_PROVIDER_IDS):015d}")
        return httpx.Response(
            200,
            json={
                "id": self.ids[-1],
                "externalReference": str(reference),
                "status": "ACTIVE",
            },
        )

    def client(self) -> AsaasClient:
        return AsaasClient(
            api_key=SecretStr(_API_KEY),
            user_agent="NeuroStack/test",
            transport=httpx.MockTransport(self.handler),
        )


def _configure(monkeypatch: pytest.MonkeyPatch, environment: str, flag: _Flag) -> None:
    monkeypatch.setattr(settings, "environment", environment)
    monkeypatch.setattr(settings, "asaas_api_key", SecretStr(_API_KEY))
    monkeypatch.setattr(settings, "asaas_webhook_token", SecretStr("whk-test-token"))
    monkeypatch.setattr(settings, "asaas_user_agent", "NeuroStack/test")
    monkeypatch.setattr(
        settings, "asaas_checkout_return_url", "https://app.example.test/billing"
    )
    monkeypatch.setattr(
        checkout_module,
        "require_financial_egress",
        EgressPolicy(flag).require_financial_egress,
    )


def _service(
    session: AsyncSession, factory: Callable[[], AsaasClient]
) -> BillingCheckoutService:
    return BillingCheckoutService(session, client_factory=factory, clock=lambda: _CLOCK)


async def _builder(harness: _Harness, label: str) -> UUID:
    builder_id = uuid4()
    async with harness.session() as session:
        async with session.begin():
            await session.execute(
                text("INSERT INTO builders (id, name) VALUES (:id, :name)"),
                {"id": builder_id, "name": label},
            )
    return builder_id


async def _transition(
    harness: _Harness,
    environment: str,
    to_state: str,
    canary_builder_id: UUID | None = None,
) -> None:
    async with harness.session() as session:
        async with session.begin():
            repository = BillingRepository(session)
            control = await repository.lock_rollout_control(environment=environment)
            await repository.transition_rollout(
                environment=environment,
                expected_revision=control.rollout_revision,
                to_state=to_state,
                canary_builder_id=canary_builder_id,
                evidence=_evidence(),
            )


async def _insert_topup_capability(
    session: AsyncSession,
    environment: str,
    *,
    schema_digest: str | None = None,
    bind: bool = True,
) -> UUID:
    """A ``credit_capability_ready.v1`` receipt in the Group 5 production shape.

    The receipt carries the operation, deployment digest (the one the
    transition evidence signs), request, watermark and the authoritative
    ``billing.fact.v1`` schema digest; ``bind`` adds the typed
    producer/credit/reconciliation/verification operation receipts and the
    binding row exactly as the repository gate re-reads them.
    """

    capability_id = uuid4()
    now = datetime.now(timezone.utc)
    await session.execute(
        text("""
            INSERT INTO billing_topup_capability_receipts (
                id, environment, schema_version, request_id,
                operation_id, deployment_digest,
                credit_deployment_digest, billing_fact_schema_digest,
                producer_compatibility_digest,
                consumer_compatibility_digest, inbox_high_watermark,
                reconciliation_receipt_id, approved_by, approved_at,
                expires_at, reconciliation_complete, readback_complete,
                has_open_gap, has_backlog, has_alert
            ) VALUES (
                :id, :environment, 'credit_capability_ready.v1',
                :request_id, :operation_id, :deployment_digest,
                'credit-image-sha256', :schema_digest, :digest,
                :digest, 0, :reconciliation_id, :approved_by,
                :approved_at, :expires_at, true, true, false, false,
                false
            )
        """),
        {
            "id": capability_id,
            "environment": environment,
            "request_id": uuid4(),
            "operation_id": uuid4(),
            "deployment_digest": _evidence().deployment_digest,
            "schema_digest": schema_digest or billing_fact_schema_digest(),
            "digest": "f" * 64,
            "reconciliation_id": uuid4(),
            "approved_by": uuid4(),
            "approved_at": now - timedelta(minutes=1),
            "expires_at": now + timedelta(hours=2),
        },
    )
    if bind:
        await _seed_capability_binding(
            session, environment=environment, capability_receipt_id=capability_id
        )
    return capability_id


async def _enable_topup_gate(
    harness: _Harness, environment: str, capability_id: UUID
) -> None:
    async with harness.session() as session:
        async with session.begin():
            repository = BillingRepository(session)
            control = await repository.lock_rollout_control(environment=environment)
            await repository.set_topup_commercial_enabled(
                environment=environment,
                expected_revision=control.topup_revision,
                enabled=True,
                evidence=_evidence(),
                capability_receipt_id=capability_id,
            )


async def _open_topup_gate(harness: _Harness, environment: str) -> None:
    async with harness.session() as session:
        async with session.begin():
            capability_id = await _insert_topup_capability(session, environment)
    await _enable_topup_gate(harness, environment, capability_id)


_general_ready: set[str] = set()


async def _general(harness: _Harness, environment: str) -> None:
    """``disabled → canary_allowlist → general`` with the top-up gate open."""

    if environment in _general_ready:
        return
    placeholder = await _builder(harness, f"{environment} placeholder canary")
    await _transition(harness, environment, "canary_allowlist", placeholder)
    await _transition(harness, environment, "general")
    await _open_topup_gate(harness, environment)
    _general_ready.add(environment)


async def _canary_counters(harness: _Harness, environment: str) -> tuple[Any, ...]:
    async with harness.session() as session:
        return tuple(
            (
                await session.execute(
                    text("""
                        SELECT canary_checkout_count, canary_amount_reserved_cents,
                               canary_subscription_checkout_id,
                               canary_topup_checkout_id, rollout_revision
                        FROM billing_rollout_control
                        WHERE environment = :environment
                    """),
                    {"environment": environment},
                )
            ).one()
        )


async def _rows(harness: _Harness, builder_id: UUID) -> tuple[list[Any], list[Any]]:
    async with harness.session() as session:
        sessions = (
            await session.execute(
                text("""
                    SELECT id, intent_kind, state, offer_id, amount_cents,
                           external_checkout_id, canary_slot, topup_order_id,
                           pricing_snapshot
                    FROM billing_checkout_sessions
                    WHERE builder_id = :builder_id ORDER BY created_at
                """),
                {"builder_id": builder_id},
            )
        ).all()
        orders = (
            await session.execute(
                text("""
                    SELECT id, state, offer_id, amount_cents, credit_grant_mc,
                           payment_method, pricing_snapshot
                    FROM billing_topup_orders
                    WHERE builder_id = :builder_id ORDER BY created_at
                """),
                {"builder_id": builder_id},
            )
        ).all()
    return list(sessions), list(orders)


async def _create(
    harness: _Harness,
    factory: Callable[[], AsaasClient],
    builder_id: UUID,
    offer_id: str,
    payment_method: str,
) -> CheckoutView:
    async with harness.session() as session:
        return await _service(session, factory).create_checkout(
            builder_id=builder_id, offer_id=offer_id, payment_method=payment_method
        )


def _broken_client() -> AsaasClient:
    # A real configuration failure: the key is not an ASAAS production key.
    return AsaasClient(api_key=SecretStr("not-a-key"), user_agent="NeuroStack/test")


@pytest.mark.parametrize(
    ("environment", "offer_id", "payment_method", "slot"),
    [
        ("test", "plan_plus_monthly_v1", "credit_card", "subscription"),
        ("staging", "package_credits_500_v1", "pix", "topup"),
    ],
)
async def test_canary_service_path_survives_pre_egress_failures_then_goes_active(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    offer_id: str,
    payment_method: str,
    slot: str,
) -> None:
    builder_id = await _builder(harness, f"G2 canary {slot}")
    await _transition(harness, environment, "canary_allowlist", builder_id)
    flag = _Flag()
    _configure(monkeypatch, environment, flag)
    provider = _Provider(harness)
    before = await _canary_counters(harness, environment)

    # HIGH C: every side-effect-free precondition fails before the reservation.
    failures: list[tuple[str, Callable[[], AsaasClient]]] = [
        ("flag_false", provider.client),
        ("flag_error", provider.client),
        ("empty_return_url", provider.client),
        ("client_build_error", _broken_client),
    ]
    for failure, factory in failures:
        flag.value, flag.error = True, None
        if failure == "flag_false":
            flag.value = False
        elif failure == "flag_error":
            flag.error = RuntimeError("appconfig agent unreachable")
        url = "" if failure == "empty_return_url" else "https://app.example.test/b"
        monkeypatch.setattr(settings, "asaas_checkout_return_url", url)
        with pytest.raises(CheckoutUnavailable, match="^billing_checkout_unavailable$"):
            await _create(harness, factory, builder_id, offer_id, payment_method)
        assert await _rows(harness, builder_id) == ([], []), failure
        assert await _canary_counters(harness, environment) == before, failure
    assert provider.posts == []

    # MEDIUM E: the valid canary request for the same builder still succeeds:
    # reserve (committed) -> fresh re-check -> one POST -> active.
    flag.value, flag.error, flag.calls = True, None, 0
    monkeypatch.setattr(
        settings,
        "asaas_checkout_return_url",
        "https://app.example.test/billing",
    )
    view = await _create(harness, provider.client, builder_id, offer_id, payment_method)

    assert view.state == "active"
    assert view.checkout_url == (
        f"https://asaas.com/checkoutSession/show?id={provider.ids[0]}"
    )
    assert len(provider.posts) == 1
    assert provider.posts[0]["externalReference"] == str(view.checkout_session_id)
    assert provider.states_at_post == [("creating",)]
    assert flag.calls == 2  # pre-reservation gate + permit read before the POST
    sessions, orders = await _rows(harness, builder_id)
    assert len(sessions) == 1
    row = sessions[0]
    assert row.id == view.checkout_session_id
    assert (row.intent_kind, row.state, row.offer_id, row.canary_slot) == (
        slot,
        "active",
        offer_id,
        slot,
    )
    assert row.external_checkout_id == provider.ids[0]
    counters = await _canary_counters(harness, environment)
    assert counters[0] == before[0] + 1
    if slot == "subscription":
        assert row.amount_cents == 99_900
        assert provider.posts[0]["chargeTypes"] == ["RECURRENT"]
        assert counters[2] == row.id
        assert orders == []
    else:
        assert row.amount_cents == 10_000
        assert provider.posts[0]["chargeTypes"] == ["DETACHED"]
        assert counters[3] == row.id
        assert len(orders) == 1
        order = orders[0]
        assert row.topup_order_id == order.id
        assert (order.state, order.amount_cents, order.credit_grant_mc) == (
            "pending",
            10_000,
            500_000,
        )
        assert [line["kind"] for line in order.pricing_snapshot["lines"]] == ["topup"]
        assert row.pricing_snapshot == order.pricing_snapshot

    # A retry returns the same live session without a second POST.
    retry = await _create(
        harness, provider.client, builder_id, offer_id, payment_method
    )
    assert retry.checkout_session_id == view.checkout_session_id
    assert len(provider.posts) == 1


@pytest.mark.parametrize(
    ("offer_id", "payment_method", "amount_cents", "credits"),
    [
        ("package_credits_1000_v1", "pix", 20_000, 1_000_000),
        ("package_credits_4000_v1", "credit_card", 80_000, 4_000_000),
    ],
)
async def test_general_topup_is_persisted_through_the_service(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    offer_id: str,
    payment_method: str,
    amount_cents: int,
    credits: int,
) -> None:
    await _general(harness, "development")
    builder_id = await _builder(harness, "G2 general topup")
    _configure(monkeypatch, "development", _Flag())
    provider = _Provider(harness)

    view = await _create(harness, provider.client, builder_id, offer_id, payment_method)

    assert view.state == "active"
    assert len(provider.posts) == 1
    assert provider.posts[0]["externalReference"] == str(view.checkout_session_id)
    # The hosted top-up page offers both detached methods (client contract);
    # the chosen method is frozen into the order and its snapshot.
    assert provider.posts[0]["billingTypes"] == ["CREDIT_CARD", "PIX"]
    assert provider.posts[0]["chargeTypes"] == ["DETACHED"]
    sessions, orders = await _rows(harness, builder_id)
    assert len(sessions) == 1 and len(orders) == 1
    row, order = sessions[0], orders[0]
    assert (row.intent_kind, row.state, row.canary_slot) == ("topup", "active", None)
    assert row.topup_order_id == order.id
    assert (
        order.state,
        order.offer_id,
        order.amount_cents,
        order.credit_grant_mc,
        order.payment_method,
    ) == ("pending", offer_id, amount_cents, credits, payment_method)
    assert order.pricing_snapshot["lines"] == [
        {
            "line_id": "topup",
            "offer_id": offer_id,
            "kind": "topup",
            "quantity": 1,
            "unit_amount_cents": amount_cents,
            "line_amount_cents": amount_cents,
            "credit_grant_mc": credits,
            "settled_cents": 0,
            "reversed_cents": 0,
            "restored_cents": 0,
            "line_financial_state": "pending",
        }
    ]


class _LockContention:
    """Hold the rollout lock until another backend is waiting on a lock."""

    def __init__(self, harness: _Harness) -> None:
        self.harness = harness
        self.blocker_calls = 0
        self.observed_waiters: list[int] = []

    async def wait_for_waiter(self) -> None:
        async with self.harness.session() as observer:
            for _ in range(200):
                waiting = await observer.scalar(text("""
                        SELECT count(*) FROM pg_stat_activity
                        WHERE datname = current_database()
                          AND wait_event_type = 'Lock'
                    """))
                if waiting:
                    self.observed_waiters.append(int(waiting))
                    return
                await asyncio.sleep(0.05)
        raise AssertionError("the second creation never waited on the lock")


def _instrument_concurrency(
    monkeypatch: pytest.MonkeyPatch, contention: _LockContention
) -> asyncio.Barrier:
    barrier = asyncio.Barrier(2)
    original_prepare = BillingCheckoutService._prepare_creation
    original_blocker = BillingRepository.subscription_checkout_blocker

    async def prepare(self: BillingCheckoutService) -> Any:
        prepared = await original_prepare(self)
        # Both creations have done their lock-free reads (no live session).
        await barrier.wait()
        return prepared

    async def blocker(self: BillingRepository, *, builder_id: UUID) -> str | None:
        contention.blocker_calls += 1
        if contention.blocker_calls == 1:
            # The first lock holder decides while the other one is blocked.
            await contention.wait_for_waiter()
        return await original_blocker(self, builder_id=builder_id)

    monkeypatch.setattr(BillingCheckoutService, "_prepare_creation", prepare)
    monkeypatch.setattr(BillingRepository, "subscription_checkout_blocker", blocker)
    return barrier


async def test_concurrent_same_offer_creations_share_one_reservation_and_post(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _general(harness, "development")
    builder_id = await _builder(harness, "G2 concurrent same offer")
    _configure(monkeypatch, "development", _Flag())
    provider = _Provider(harness)
    contention = _LockContention(harness)
    _instrument_concurrency(monkeypatch, contention)

    results = await asyncio.gather(
        _create(
            harness, provider.client, builder_id, "plan_pro_monthly_v1", "credit_card"
        ),
        _create(
            harness, provider.client, builder_id, "plan_pro_monthly_v1", "credit_card"
        ),
        return_exceptions=True,
    )

    assert all(isinstance(result, CheckoutView) for result in results), results
    views = [result for result in results if isinstance(result, CheckoutView)]
    assert views[0].checkout_session_id == views[1].checkout_session_id
    assert contention.observed_waiters, "creations did not overlap"
    assert contention.blocker_calls == 1  # the second reused the live session
    assert len(provider.posts) == 1
    sessions, _ = await _rows(harness, builder_id)
    assert [(row.state, row.offer_id) for row in sessions] == [
        ("active", "plan_pro_monthly_v1")
    ]


async def test_concurrent_different_plan_creation_is_rejected_with_stable_code(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _general(harness, "development")
    builder_id = await _builder(harness, "G2 concurrent different plans")
    _configure(monkeypatch, "development", _Flag())
    provider = _Provider(harness)
    contention = _LockContention(harness)
    _instrument_concurrency(monkeypatch, contention)

    results = await asyncio.gather(
        _create(
            harness, provider.client, builder_id, "plan_plus_monthly_v1", "credit_card"
        ),
        _create(
            harness, provider.client, builder_id, "plan_max_monthly_v1", "credit_card"
        ),
        return_exceptions=True,
    )

    views = [result for result in results if isinstance(result, CheckoutView)]
    errors = [result for result in results if isinstance(result, BaseException)]
    assert len(views) == 1 and len(errors) == 1, results
    with pytest.raises(CheckoutNotEligible, match="^billing_checkout_not_eligible$"):
        raise errors[0]
    assert contention.observed_waiters, "creations did not overlap"
    assert contention.blocker_calls == 2  # both decided under the lock
    assert len(provider.posts) == 1
    sessions, _ = await _rows(harness, builder_id)
    assert [(row.id, row.state) for row in sessions] == [
        (views[0].checkout_session_id, "active")
    ]


async def _seed_blocker(harness: _Harness, builder_id: UUID, condition: str) -> None:
    subscription_id = uuid4()
    provider, status, active = {
        "legacy_active": ("abacatepay", "active", True),
        "asaas_non_terminal": ("asaas", "readback_required", True),
    }.get(condition, (None, None, None))
    async with harness.session() as session:
        async with session.begin():
            if provider is not None:
                await session.execute(
                    text("""
                        INSERT INTO billing_subscriptions (
                            id, builder_id, provider, external_subscription_id,
                            is_active, plan, billing_cycle, status,
                            roster_revision
                        ) VALUES (
                            :id, :builder_id, :provider, :external_id, :active,
                            'plus', 'monthly', :status, 1
                        )
                    """),
                    {
                        "id": subscription_id,
                        "builder_id": builder_id,
                        "provider": provider,
                        "external_id": f"sub-{subscription_id.hex}",
                        "active": active,
                        "status": status,
                    },
                )


@pytest.mark.parametrize(
    "condition",
    ["legacy_active", "asaas_non_terminal", "live_other_plan"],
)
async def test_ineligible_builder_gets_stable_rejection_without_rows_or_post(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    await _general(harness, "development")
    builder_id = await _builder(harness, f"G2 ineligible {condition}")
    _configure(monkeypatch, "development", _Flag())
    provider = _Provider(harness)
    if condition == "live_other_plan":
        await _create(
            harness, provider.client, builder_id, "plan_plus_monthly_v1", "credit_card"
        )
        assert len(provider.posts) == 1
    else:
        await _seed_blocker(harness, builder_id, condition)
    before = await _rows(harness, builder_id)

    with pytest.raises(CheckoutNotEligible, match="^billing_checkout_not_eligible$"):
        await _create(
            harness, provider.client, builder_id, "plan_pro_monthly_v1", "credit_card"
        )

    assert await _rows(harness, builder_id) == before
    assert len(provider.posts) == (1 if condition == "live_other_plan" else 0)
    # A top-up is not a subscription and stays available to the builder.
    topup = await _create(
        harness, provider.client, builder_id, "package_credits_1000_v1", "pix"
    )
    assert topup.state == "active"


# ---------------------------------------------------------------------------
# Group 10 phase E2: the migration saga is removed and a billing_migrations row
# no longer blocks by itself.  Every live saga state still fails closed through
# the subscription row it leaves behind; a row left over after its
# subscription ended does not.  The advisory read contract (6b) and the POST
# must agree in both directions.
# ---------------------------------------------------------------------------

_SAGA_SNAPSHOT = """
    jsonb_build_object(
      'catalog_version_id','payment-2026-10-03',
      'offer_id','plan_plus_monthly_v1','plan','plus',
      'billing_cycle','monthly','coupon_code',NULL,
      'amount_cents',99900,'credit_grant_mc',5000,
      'included_clinics',1,'additional_clinics',0,
      'lines',jsonb_build_array(jsonb_build_object(
        'line_id','plan','offer_id','plan_plus_monthly_v1',
        'kind','plan','quantity',1,'unit_amount_cents',99900,
        'line_amount_cents',99900,'credit_grant_mc',5000,
        'settled_cents',0,'reversed_cents',0,'restored_cents',0,
        'line_financial_state','pending')))
"""


async def _seed_saga(
    harness: _Harness,
    builder_id: UUID,
    *,
    state: str,
    provider: str | None,
    status: str,
    is_active: bool,
) -> None:
    subscription_id = uuid4()
    async with harness.session() as session:
        async with session.begin():
            await session.execute(
                text("""
                    INSERT INTO billing_subscriptions (
                        id, builder_id, provider, external_subscription_id,
                        is_active, plan, billing_cycle, status, roster_revision
                    ) VALUES (
                        :id, :builder_id, :provider, :external_id, :active,
                        'plus', 'monthly', :status, 1
                    )
                """),
                {
                    "id": subscription_id,
                    "builder_id": builder_id,
                    "provider": provider,
                    "external_id": f"sub-{subscription_id.hex}",
                    "active": is_active,
                    "status": status,
                },
            )
            await session.execute(
                text(f"""
                    INSERT INTO billing_migrations (
                        id, builder_id, subscription_id, state,
                        renewal_date, open_by_date, cutover_by_date,
                        target_catalog_version_id, target_offer_id,
                        target_plan, target_billing_cycle,
                        target_amount_cents, target_snapshot,
                        source_roster_revision
                    ) VALUES (
                        :id, :builder_id, :subscription_id, :state,
                        DATE '2026-12-10', DATE '2026-12-03',
                        DATE '2026-12-08', 'payment-2026-10-03',
                        'plan_plus_monthly_v1', 'plus', 'monthly', 99900,
                        {_SAGA_SNAPSHOT}, 1
                    )
                """),
                {
                    "id": uuid4(),
                    "builder_id": builder_id,
                    "subscription_id": subscription_id,
                    "state": state,
                },
            )


async def _listed_offers(harness: _Harness, builder_id: UUID) -> tuple[str, ...]:
    async with harness.session() as session:
        readback = await read_commercial_availability(
            session, builder_id=builder_id, clock=lambda: _CLOCK
        )
    assert readback.available is True
    return readback.available_offer_ids


@pytest.mark.parametrize(
    ("state", "provider", "status", "is_active"),
    [
        ("pending", "abacatepay", "active", True),
        ("in_progress", None, "legacy_cancel_pending", False),
        ("legacy_cancel_pending", None, "legacy_cancel_pending", False),
        ("legacy_cancelled", None, "legacy_cancelled", False),
        ("recreate_pending", None, "legacy_cancelled", False),
        ("migrated", "asaas", "active", True),
        ("manual_intervention", None, "manual_intervention", False),
    ],
)
async def test_live_saga_pairings_still_reject_without_rows_or_post(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    provider: str | None,
    status: str,
    is_active: bool,
) -> None:
    await _general(harness, "development")
    builder_id = await _builder(harness, f"E2 live saga {state}")
    _configure(monkeypatch, "development", _Flag())
    fake = _Provider(harness)
    await _seed_saga(
        harness,
        builder_id,
        state=state,
        provider=provider,
        status=status,
        is_active=is_active,
    )
    before = await _rows(harness, builder_id)

    # The read contract does not advertise what the POST refuses.
    listed = await _listed_offers(harness, builder_id)
    assert "plan_pro_monthly_v1" not in listed
    assert "plan_plus_monthly_v1" not in listed
    with pytest.raises(CheckoutNotEligible, match="^billing_checkout_not_eligible$"):
        await _create(
            harness, fake.client, builder_id, "plan_pro_monthly_v1", "credit_card"
        )

    assert await _rows(harness, builder_id) == before
    assert fake.posts == []


@pytest.mark.parametrize(
    ("state", "provider", "status"),
    [
        ("pending", "abacatepay", "inactive"),
        ("in_progress", "asaas", "cancelled"),
        ("legacy_cancel_pending", "abacatepay", "cancelled"),
        ("legacy_cancelled", "asaas", "inactive"),
        ("recreate_pending", "abacatepay", "inactive"),
        ("migrated", "asaas", "cancelled"),
        ("manual_intervention", "abacatepay", "cancelled"),
    ],
)
async def test_leftover_saga_row_with_an_ended_subscription_checks_out(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    provider: str,
    status: str,
) -> None:
    await _general(harness, "development")
    builder_id = await _builder(harness, f"E2 leftover saga {state}")
    _configure(monkeypatch, "development", _Flag())
    fake = _Provider(harness)
    await _seed_saga(
        harness,
        builder_id,
        state=state,
        provider=provider,
        status=status,
        is_active=False,
    )

    # Read contract and POST agree: listed, then exactly one provider POST.
    assert "plan_pro_monthly_v1" in await _listed_offers(harness, builder_id)
    view = await _create(
        harness, fake.client, builder_id, "plan_pro_monthly_v1", "credit_card"
    )

    assert view.state == "active"
    assert len(fake.posts) == 1
    sessions, orders = await _rows(harness, builder_id)
    assert [(row.intent_kind, row.state, row.offer_id) for row in sessions] == [
        ("subscription", "active", "plan_pro_monthly_v1")
    ]
    assert orders == []
    # The live attempt stays listed (the POST would return it unchanged).
    assert "plan_pro_monthly_v1" in await _listed_offers(harness, builder_id)


async def test_locked_reads_refresh_references_held_across_a_foreign_commit(
    harness: _Harness,
) -> None:
    builder_id = await _builder(harness, "G2 identity map")
    checkout_id = uuid4()
    async with harness.session() as session:
        async with session.begin():
            await session.execute(
                text("""
                    INSERT INTO billing_checkout_sessions (
                        id, builder_id, intent_kind, intent_id, attempt_no,
                        provider, state, catalog_version_id, offer_id,
                        amount_cents, pricing_snapshot, next_due_date
                    ) VALUES (
                        :id, :builder_id, 'subscription', :intent_id, 1,
                        'asaas', 'creating', 'payment-2026-10-03',
                        'plan_pro_monthly_v1', 250000,
                        CAST(:snapshot AS jsonb),
                        DATE '2026-10-11'
                    )
                """),
                {
                    "id": checkout_id,
                    "builder_id": builder_id,
                    "intent_id": uuid4(),
                    "snapshot": '{"amount_cents":250000}',
                },
            )

    async with harness.session() as service:
        repository = BillingRepository(service)
        held_control = await repository.lock_rollout_control(environment="production")
        held_checkout = await repository.lock_checkout_session(
            builder_id=builder_id, checkout_session_id=checkout_id
        )
        assert isinstance(held_control, BillingRolloutControl)
        assert isinstance(held_checkout, BillingCheckoutSession)
        initial_state = held_control.state
        initial_revision = held_control.rollout_revision
        await service.commit()  # expire_on_commit=False: references stay loaded

        # Another connection commits a legal rollout transition and ends the
        # attempt while this session keeps both references.
        canary = await _builder(harness, "G2 identity map canary")
        assert initial_state == "disabled"
        await _transition(harness, "production", "canary_allowlist", canary)
        async with harness.session() as operator:
            async with operator.begin():
                await operator.execute(
                    text(
                        "UPDATE billing_checkout_sessions SET state = 'failed' "
                        "WHERE id = :id"
                    ),
                    {"id": checkout_id},
                )

        control = await repository.lock_rollout_control(environment="production")
        locked = await repository.lock_checkout_session(
            builder_id=builder_id, checkout_session_id=checkout_id
        )
        assert control is held_control and locked is held_checkout
        assert control.state == "canary_allowlist"
        assert control.canary_builder_id == canary
        assert control.rollout_revision == initial_revision + 1
        assert locked.state == "failed"
        await service.rollback()


@pytest.mark.parametrize(
    ("bind", "schema_digest", "reason"),
    [
        (False, None, "topup capability binding is required"),
        (True, "0" * 64, "topup capability is not valid"),
    ],
)
async def test_topup_gate_helper_still_refuses_unbound_or_stale_capability(
    harness: _Harness, bind: bool, schema_digest: str | None, reason: str
) -> None:
    """The fixture's production shape is what opens the gate, nothing less."""

    environment = "staging"
    async with harness.session() as session:
        async with session.begin():
            capability_id = await _insert_topup_capability(
                session, environment, schema_digest=schema_digest, bind=bind
            )
    with pytest.raises(BillingCapabilityRejected, match=f"^{reason}$"):
        await _enable_topup_gate(harness, environment, capability_id)
    async with harness.session() as session:
        control = await BillingRepository(session).lock_rollout_control(
            environment=environment
        )
        assert control.topup_commercial_enabled is False
        assert control.active_topup_capability_id is None
        await session.rollback()


# ---------------------------------------------------------------------------
# Group 5 deliverable 8: the ASAAS-owned top-up capability endpoint's service,
# on a session built like production (autoflush=False, expire_on_commit=False).
# ---------------------------------------------------------------------------

_CAPABILITY_SIGNING_KEY = b"capability-proof-signing-key-0123456789"


def _capability_material(
    environment: str,
    operation_id: UUID,
    *,
    from_state: str,
    to_state: str,
    budget_digest: str,
    expires_at: datetime,
) -> Any:
    from src.services.billing_operations import OperationMaterial

    return OperationMaterial(
        scope="topup_capability",
        operation_id=operation_id,
        environment=environment,
        from_state=from_state,
        to_state=to_state,
        budget_digest=budget_digest,
        deployment_digest=_evidence().deployment_digest,
        expires_at=expires_at,
    )


async def _signed_receipt(
    session: AsyncSession,
    material: Any,
    *,
    role: str,
    evidence_type: str | None = None,
    request_id: UUID | None = None,
    inbox_high_watermark: int | None = None,
) -> Any:
    """Assignment + backend-signed receipt through the operation authority."""

    from src.services.billing_operations import BillingOperationAuthority

    authority = BillingOperationAuthority(session, signing_key=_CAPABILITY_SIGNING_KEY)
    actor_id = uuid4()
    assignment = await authority.assign(
        material=material, role=role, actor_user_id=actor_id  # type: ignore[arg-type]
    )
    return await authority.record_receipt(
        assignment_id=assignment.id,
        actor_user_id=actor_id,
        nonce=uuid4(),
        material=material,
        evidence_type=evidence_type,
        request_id=request_id,
        inbox_high_watermark=inbox_high_watermark,
    )


async def _quorum(session: AsyncSession, material: Any) -> None:
    for role in ("deployer", "approver", "reviewer"):
        await _signed_receipt(session, material, role=role)


async def test_capability_service_enables_and_disables_the_gate_on_a_production_session(
    harness: _Harness,
) -> None:
    from src.services.billing_capability import (
        BillingCapabilityRejected as CapabilityServiceRejected,
    )
    from src.services.billing_capability import (
        BillingTopupCapabilityService,
        TopupCapabilityEvidence,
    )

    environment = "test"
    # The harness sessions are the application's: autoflush=False,
    # expire_on_commit=False (src/repositories/database.py).
    assert harness.sessions.kw["autoflush"] is False
    assert harness.sessions.kw["expire_on_commit"] is False
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(
        microsecond=0
    )
    operation_id, request_id, capability_id = uuid4(), uuid4(), uuid4()
    enable_material = _capability_material(
        environment,
        operation_id,
        from_state="topup_disabled",
        to_state="topup_enabled",
        budget_digest="b" * 64,
        expires_at=expires_at,
    )
    typed: dict[str, Any] = {}
    async with harness.session() as session:
        async with session.begin():
            await _quorum(session, enable_material)
            for evidence_type, budget in zip(
                ("producer", "credit", "reconciliation", "verification"),
                ("1", "2", "3", "4"),
                strict=True,
            ):
                typed[evidence_type] = await _signed_receipt(
                    session,
                    _capability_material(
                        environment,
                        operation_id,
                        from_state="topup_disabled",
                        to_state="topup_enabled",
                        budget_digest=budget * 64,
                        expires_at=expires_at,
                    ),
                    role="reviewer",
                    evidence_type=evidence_type,
                    request_id=request_id,
                    inbox_high_watermark=0,
                )
            # The credit system's readiness receipt (input to this service).
            now = datetime.now(timezone.utc)
            await session.execute(
                text("""
                    INSERT INTO billing_topup_capability_receipts (
                        id, environment, schema_version, request_id,
                        operation_id, deployment_digest,
                        credit_deployment_digest, billing_fact_schema_digest,
                        producer_compatibility_digest,
                        consumer_compatibility_digest, inbox_high_watermark,
                        reconciliation_receipt_id, approved_by, approved_at,
                        expires_at, reconciliation_complete, readback_complete,
                        has_open_gap, has_backlog, has_alert
                    ) VALUES (
                        :id, :environment, 'credit_capability_ready.v1',
                        :request_id, :operation_id, :deployment_digest,
                        :credit_digest, :schema_digest, :producer_digest,
                        :reconciliation_digest, 0, :reconciliation_receipt_id,
                        :approved_by, :approved_at, :expires_at, true, true,
                        false, false, false
                    )
                """),
                {
                    "id": capability_id,
                    "environment": environment,
                    "request_id": request_id,
                    "operation_id": operation_id,
                    "deployment_digest": _evidence().deployment_digest,
                    "credit_digest": typed["credit"].payload_digest,
                    "schema_digest": billing_fact_schema_digest(),
                    "producer_digest": typed["producer"].payload_digest,
                    "reconciliation_digest": typed["reconciliation"].payload_digest,
                    "reconciliation_receipt_id": typed["reconciliation"].id,
                    "approved_by": typed["verification"].actor_user_id,
                    "approved_at": now - timedelta(minutes=1),
                    "expires_at": now + timedelta(hours=2),
                },
            )
    evidence = TopupCapabilityEvidence(
        environment=environment,
        capability_receipt_id=capability_id,
        producer_receipt_id=typed["producer"].id,
        credit_receipt_id=typed["credit"].id,
        reconciliation_receipt_id=typed["reconciliation"].id,
        verification_receipt_id=typed["verification"].id,
        producer_digest=typed["producer"].payload_digest,
        credit_digest=typed["credit"].payload_digest,
        reconciliation_digest=typed["reconciliation"].payload_digest,
        verification_digest=typed["verification"].payload_digest,
        billing_fact_schema_digest=billing_fact_schema_digest(),
        producer_actor_id=typed["producer"].actor_user_id,
        credit_actor_id=typed["credit"].actor_user_id,
        reconciliation_actor_id=typed["reconciliation"].actor_user_id,
        verification_actor_id=typed["verification"].actor_user_id,
        verified_at=datetime.now(timezone.utc),
        operation_id=operation_id,
        deployment_digest=_evidence().deployment_digest,
        request_id=request_id,
        inbox_high_watermark=0,
    )

    async def gate() -> tuple[Any, ...]:
        async with harness.session() as session:
            row = (
                await session.execute(
                    text("""
                        SELECT topup_commercial_enabled, topup_revision,
                               active_topup_capability_id
                        FROM billing_rollout_control WHERE environment = :env
                    """),
                    {"env": environment},
                )
            ).one()
            return tuple(row)

    before = await gate()
    assert before[0] is False
    async with harness.session() as session:
        async with session.begin():
            control = await BillingTopupCapabilityService(
                session, signing_key=_CAPABILITY_SIGNING_KEY
            ).enable(
                expected_revision=before[1],
                material=enable_material,
                evidence=evidence,
            )
            assert control.topup_commercial_enabled is True
    enabled = await gate()
    assert enabled == (True, before[1] + 1, capability_id)
    async with harness.session() as session:
        counts = (
            await session.execute(
                text("""
                    SELECT
                      (SELECT count(*) FROM billing_topup_capability_bindings
                       WHERE capability_receipt_id = :capability_id),
                      (SELECT count(*) FROM billing_rollout_receipts
                       WHERE capability_receipt_id = :capability_id),
                      (SELECT count(*) FROM billing_operation_receipts
                       WHERE operation_id = :operation_id
                         AND evidence_type IS NULL AND applied_at IS NOT NULL)
                """),
                {"capability_id": capability_id, "operation_id": operation_id},
            )
        ).one()
    assert tuple(counts) == (1, 1, 3)

    # Single use: replaying the consumed quorum is refused, nothing changes.
    async with harness.session() as session:
        with pytest.raises(
            CapabilityServiceRejected, match="^billing_operation_receipt_invalid$"
        ):
            async with session.begin():
                await BillingTopupCapabilityService(
                    session, signing_key=_CAPABILITY_SIGNING_KEY
                ).enable(
                    expected_revision=enabled[1],
                    material=enable_material,
                    evidence=evidence,
                )
    assert await gate() == enabled

    # The reverse transition: health disable with its own quorum.
    disable_material = _capability_material(
        environment,
        uuid4(),
        from_state="topup_enabled",
        to_state="topup_disabled",
        budget_digest="c" * 64,
        expires_at=expires_at,
    )
    async with harness.session() as session:
        async with session.begin():
            await _quorum(session, disable_material)
    async with harness.session() as session:
        async with session.begin():
            disabled_control = await BillingTopupCapabilityService(
                session, signing_key=_CAPABILITY_SIGNING_KEY
            ).disable(
                expected_revision=enabled[1],
                material=disable_material,
                reason="health",
            )
            assert disabled_control.topup_commercial_enabled is False
    assert await gate() == (False, enabled[1] + 1, None)


# ---------------------------------------------------------------------------
# Group 6b review: the general top-up reservation keeps its capability row
# locks and its binding check (the summary read path takes neither lock).
# ---------------------------------------------------------------------------

_CAPABILITY_TABLES = (
    "billing_topup_capability_receipts",
    "billing_topup_capability_bindings",
    "billing_operation_receipts",
)


async def test_general_topup_reservation_locks_and_rebinds_the_capability(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = "development"
    await _general(harness, environment)
    _configure(monkeypatch, environment, _Flag())
    provider = _Provider(harness)

    reserving = False
    locked: list[str] = []
    refusals: list[BaseException] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if reserving and "FOR UPDATE" in statement:
            locked.append(statement)

    original = BillingRepository.authorize_and_reserve_topup_checkout

    async def reservation(self: BillingRepository, **kwargs: Any) -> Any:
        nonlocal reserving
        reserving = True
        try:
            return await original(self, **kwargs)
        except BaseException as exc:
            refusals.append(exc)
            raise
        finally:
            reserving = False

    monkeypatch.setattr(
        BillingRepository, "authorize_and_reserve_topup_checkout", reservation
    )
    event.listen(harness.engine.sync_engine, "before_cursor_execute", record)
    try:
        builder_id = await _builder(harness, "G6b reservation locks")
        view = await _create(
            harness, provider.client, builder_id, "package_credits_500_v1", "pix"
        )
        assert view.state == "active"
        # Every capability table is read FOR UPDATE inside the reservation.
        for table in _CAPABILITY_TABLES:
            assert any(f"FROM {table}" in sql for sql in locked), table

        # The capability itself stays valid; one bound operation receipt is
        # consumed, so the typed binding is no longer current.
        async with harness.session() as session:
            async with session.begin():
                await session.execute(
                    text("""
                        UPDATE billing_operation_receipts SET applied_at = now()
                        WHERE id = (
                            SELECT b.verification_receipt_id
                            FROM billing_topup_capability_bindings b
                            JOIN billing_rollout_control c
                              ON c.active_topup_capability_id
                                 = b.capability_receipt_id
                            WHERE c.environment = :env AND b.environment = :env
                        )
                    """),
                    {"env": environment},
                )
        posts = len(provider.posts)
        refused = await _builder(harness, "G6b reservation unbound")
        with pytest.raises(CheckoutUnavailable):
            await _create(
                harness, provider.client, refused, "package_credits_500_v1", "pix"
            )
        assert len(provider.posts) == posts
        assert [str(exc) for exc in refusals] == ["topup capability is not valid"]
        assert isinstance(refusals[0], BillingCapabilityRejected)
    finally:
        event.remove(harness.engine.sync_engine, "before_cursor_execute", record)
        # Leave the shared environment's gate open for the other tests.
        async with harness.session() as session:
            async with session.begin():
                repository = BillingRepository(session)
                control = await repository.lock_rollout_control(environment=environment)
                if control.topup_commercial_enabled:
                    await repository.set_topup_commercial_enabled(
                        environment=environment,
                        expected_revision=control.topup_revision,
                        enabled=False,
                        evidence=_evidence(),
                        disable_reason="health",
                    )
        await _open_topup_gate(harness, environment)
