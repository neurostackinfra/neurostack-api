"""State-boundary tests for hosted checkout orchestration."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Callable, Iterator
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.models.billing import BillingCheckoutSession
from src.models.coupon import Coupon
from src.repositories.billing import BillingRepository
from src.services import billing_checkout as checkout_module
from src.services.asaas import AsaasOutcomeUnknown, CheckoutCreated
from src.services.billing_checkout import (
    COUPON_NOT_ELIGIBLE,
    BillingCheckoutService,
    CheckoutCouponRejected,
    CheckoutNotFound,
    CheckoutReconciliationRequired,
    CheckoutUnavailable,
    billing_local_date,
    normalize_coupon_code,
)
from src.services.billing_outbox import billing_fact_schema_digest

# 23:59:59 on 2026-10-11 in São Paulo (UTC-03:00, no DST since 2019).
_LATE_EVENING_UTC = datetime(2026, 10, 12, 2, 59, 59, tzinfo=timezone.utc)


class _Result:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value

    def scalar_one(self) -> object:
        return self.value


class _Session:
    def __init__(
        self,
        results: list[object],
        *,
        scalars: list[object] | None = None,
        on_commit: dict[int, Callable[[], None]] | None = None,
    ) -> None:
        self.results = results
        self.scalars = scalars or []
        self.on_commit = on_commit or {}
        self.commits = 0
        self.rollbacks = 0
        self.added: list[object] = []

    async def execute(self, _statement: object) -> _Result:
        return _Result(self.results.pop(0))

    async def scalar(self, _statement: object) -> object:
        return self.scalars.pop(0)

    async def commit(self) -> None:
        self.commits += 1
        hook = self.on_commit.get(self.commits)
        if hook is not None:
            # Simulates a concurrent transition committed right after ours.
            hook()

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def flush(self) -> None:
        return None

    def add(self, value: object) -> None:
        self.added.append(value)


def _checkout(
    *,
    builder_id: UUID,
    state: str = "creating",
    checkout_id: UUID | None = None,
) -> BillingCheckoutSession:
    return BillingCheckoutSession(
        id=checkout_id or uuid4(),
        builder_id=builder_id,
        intent_kind="subscription",
        intent_id=uuid4(),
        migration_id=None,
        topup_order_id=None,
        attempt_no=1,
        provider="asaas",
        state=state,
        catalog_version_id="payment-2026-10-03",
        offer_id="plan_plus_monthly_v1",
        amount_cents=99_900,
        pricing_snapshot={
            "catalog_version_id": "payment-2026-10-03",
            "offer_id": "plan_plus_monthly_v1",
            "amount_cents": 99_900,
            "credit_grant_mc": 5_000_000,
            "included_clinics": 1,
            "additional_clinics": 0,
            "billing_cycle": "monthly",
            "payment_method": "credit_card",
            "lines": [],
        },
        next_due_date=date(2026, 10, 12),
        reconciliation_attempts=0,
    )


def _canary_checkout(builder_id: UUID) -> BillingCheckoutSession:
    checkout = _checkout(builder_id=builder_id)
    checkout.canary_environment = "test"
    checkout.canary_slot = "subscription"
    return checkout


def _topup_checkout(
    builder_id: UUID, *, canary: bool = False
) -> BillingCheckoutSession:
    order_id = uuid4()
    return BillingCheckoutSession(
        id=uuid4(),
        builder_id=builder_id,
        intent_kind="topup",
        intent_id=order_id,
        migration_id=None,
        topup_order_id=order_id,
        attempt_no=1,
        provider="asaas",
        state="creating",
        catalog_version_id="payment-2026-10-03",
        offer_id="package_credits_500_v1",
        amount_cents=10_000,
        pricing_snapshot={
            "catalog_version_id": "payment-2026-10-03",
            "offer_id": "package_credits_500_v1",
            "amount_cents": 10_000,
            "credit_grant_mc": 500_000,
            "payment_method": "pix",
            "lines": [],
        },
        canary_environment="test" if canary else None,
        canary_slot="topup" if canary else None,
        next_due_date=None,
        reconciliation_attempts=0,
    )


@dataclass
class _Repository:
    db: _Session

    checkout: BillingCheckoutSession | None = None

    async def lock_rollout_control(self, *, environment: str) -> object:
        assert environment == "test"
        return SimpleNamespace(state="general")

    async def reserve_general_subscription_checkout(
        self, **kwargs: object
    ) -> BillingCheckoutSession:
        assert isinstance(kwargs["amount_cents"], int)
        assert kwargs["amount_cents"] > 0
        assert kwargs["pricing_snapshot"]
        assert self.checkout is not None
        # Mirror the repository: the persisted attempt is the planned one.
        self.checkout.id = kwargs["checkout_session_id"]  # type: ignore[assignment]
        self.checkout.next_due_date = kwargs["next_due_date"]  # type: ignore[assignment]
        self.checkout.amount_cents = kwargs["amount_cents"]  # type: ignore[assignment]
        return self.checkout

    async def subscription_checkout_blocker(self, *, builder_id: UUID) -> str | None:
        return None

    async def authorize_reserved_checkout_egress(
        self, *, environment: str, builder_id: UUID, checkout_session_id: UUID
    ) -> BillingCheckoutSession:
        assert environment == "test"
        assert self.checkout is not None
        assert builder_id == self.checkout.builder_id
        assert checkout_session_id == self.checkout.id
        return self.checkout

    async def lock_checkout_session(
        self, *, builder_id: UUID, checkout_session_id: UUID
    ) -> BillingCheckoutSession | None:
        if self.checkout is None:
            return None
        assert builder_id == self.checkout.builder_id
        assert checkout_session_id == self.checkout.id
        return self.checkout


class _Client:
    def __init__(self, db: _Session, *, unknown: bool = False) -> None:
        self.db = db
        self.unknown = unknown
        self.requests: list[object] = []
        self.commits_at_post: list[int] = []

    async def create_checkout(
        self, request: object, *, permit: object
    ) -> CheckoutCreated:
        # The reservation and the pre-POST re-authorization must both have
        # committed (releasing every lock) before this outbound call.
        assert self.db.commits == 2
        self.commits_at_post.append(self.db.commits)
        self.requests.append((request, permit))
        if self.unknown:
            raise AsaasOutcomeUnknown("asaas_transport_unknown")
        return CheckoutCreated("chk_000000000000001")


def _prepare_creation(
    monkeypatch: pytest.MonkeyPatch,
    session: _Session,
    checkout: BillingCheckoutSession,
) -> None:
    class Repository(_Repository):
        def __init__(self, _db: _Session) -> None:
            super().__init__(_db, checkout)

    monkeypatch.setattr(checkout_module, "BillingRepository", Repository)
    _prepare_settings(monkeypatch)


def _prepare_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        checkout_module,
        "require_billing_checkout_rollout",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        checkout_module,
        "authorize_creation_egress",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(
        checkout_module.settings,
        "environment",
        "test",
    )
    monkeypatch.setattr(
        checkout_module.settings,
        "asaas_checkout_return_url",
        "https://app.example.test/billing",
    )


@pytest.mark.asyncio
async def test_creating_snapshot_commits_before_post_and_retry_returns_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder_id = uuid4()
    persisted = _checkout(builder_id=builder_id)
    db = _Session([None, None, 2])
    _prepare_creation(monkeypatch, db, persisted)
    client = _Client(db)

    service = BillingCheckoutService(
        db, client_factory=lambda: client, clock=lambda: _LATE_EVENING_UTC
    )
    created = await service.create_checkout(
        builder_id=builder_id,
        offer_id="plan_plus_monthly_v1",
        payment_method="credit_card",
    )

    assert persisted.state == "active"
    assert persisted.external_checkout_id == "chk_000000000000001"
    assert db.commits == 3
    assert created.checkout_url == (
        "https://asaas.com/checkoutSession/show?id=chk_000000000000001"
    )
    assert len(client.requests) == 1

    retry_db = _Session([persisted])
    _prepare_creation(monkeypatch, retry_db, persisted)
    retry_client = _Client(retry_db)
    retried = await BillingCheckoutService(
        retry_db, client_factory=lambda: retry_client
    ).create_checkout(
        builder_id=builder_id,
        offer_id="plan_plus_monthly_v1",
        payment_method="credit_card",
    )
    assert retried.checkout_session_id == persisted.id
    assert retry_client.requests == []


@pytest.mark.asyncio
async def test_ambiguous_timeout_marks_reconciliation_and_never_blind_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder_id = uuid4()
    persisted = _checkout(builder_id=builder_id)
    db = _Session([None, None, 1])
    _prepare_creation(monkeypatch, db, persisted)
    client = _Client(db, unknown=True)
    service = BillingCheckoutService(db, client_factory=lambda: client)

    with pytest.raises(CheckoutReconciliationRequired):
        await service.create_checkout(
            builder_id=builder_id,
            offer_id="plan_plus_monthly_v1",
            payment_method="credit_card",
        )

    assert persisted.state == "reconciliation_required"
    assert persisted.reconciliation_attempts == 1
    assert len(client.requests) == 1

    retry_db = _Session([persisted])
    _prepare_creation(monkeypatch, retry_db, persisted)
    retry_client = _Client(retry_db)
    retried = await BillingCheckoutService(
        retry_db, client_factory=lambda: retry_client
    ).create_checkout(
        builder_id=builder_id,
        offer_id="plan_plus_monthly_v1",
        payment_method="credit_card",
    )
    assert retried.state == "reconciliation_required"
    assert retry_client.requests == []


@pytest.mark.asyncio
async def test_foreign_uuid_is_unlocked_indistinguishable_not_found_without_network() -> (
    None
):
    builder_id = uuid4()
    db = _Session([None])
    client_factory = AsyncMock(side_effect=AssertionError("provider reached"))
    service = BillingCheckoutService(db, client_factory=client_factory)

    with pytest.raises(CheckoutNotFound) as exc_info:
        await service.get_checkout(
            builder_id=builder_id,
            checkout_session_id=uuid4(),
        )

    assert str(exc_info.value) == "billing_checkout_not_found"
    client_factory.assert_not_called()
    assert db.commits == 0


@pytest.mark.asyncio
async def test_reconciliation_uses_only_the_exact_session_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder_id = uuid4()
    persisted = _checkout(builder_id=builder_id, state="reconciliation_required")
    db = _Session([persisted])
    references: list[UUID] = []

    class Repository(_Repository):
        def __init__(self, _db: _Session) -> None:
            super().__init__(_db, persisted)

    class ReconciliationClient:
        async def list_subscriptions_by_external_reference(
            self, reference: UUID
        ) -> tuple[object, ...]:
            references.append(reference)
            return ()

        async def list_payments_by_external_reference(
            self, reference: UUID
        ) -> tuple[object, ...]:
            references.append(reference)
            return ()

    monkeypatch.setattr(checkout_module, "BillingRepository", Repository)
    result = await BillingCheckoutService(
        db, client_factory=ReconciliationClient
    ).reconcile_checkout(builder_id=builder_id, checkout_session_id=persisted.id)

    assert references == [persisted.id, persisted.id]
    assert result.state == "reconciliation_required"
    assert db.commits == 1


# ---------------------------------------------------------------------------
# Pre-POST re-authorization (HIGH 1).  These tests run the REAL
# ``BillingRepository.authorize_reserved_checkout_egress`` decision logic; only
# the SQL lock primitives are replaced by an in-memory control row that a
# "concurrent" writer mutates right after the reservation commit.
# ---------------------------------------------------------------------------


@dataclass
class _World:
    control: SimpleNamespace
    checkout: BillingCheckoutSession
    lock_calls: int = 0
    fail_lock_call: int | None = None
    reservations: list[dict[str, object]] = field(default_factory=list)
    blocker: str | None = None
    blocker_checks: int = 0


def _general_control(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "state": "general",
        "canary_builder_id": None,
        "canary_subscription_checkout_id": None,
        "canary_topup_checkout_id": None,
        "topup_commercial_enabled": False,
        "active_topup_capability_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _install_world(monkeypatch: pytest.MonkeyPatch, world: _World) -> None:
    class Repository(BillingRepository):
        async def _lock_rollout(self, environment: str) -> SimpleNamespace:  # type: ignore[override]
            assert environment == "test"
            world.lock_calls += 1
            if world.fail_lock_call == world.lock_calls:
                raise RuntimeError("connection reset during rollout read")
            return world.control

        async def reserve_general_subscription_checkout(  # type: ignore[override]
            self, **kwargs: object
        ) -> BillingCheckoutSession:
            world.reservations.append(kwargs)
            world.checkout.id = kwargs["checkout_session_id"]  # type: ignore[assignment]
            world.checkout.next_due_date = kwargs["next_due_date"]  # type: ignore[assignment]
            world.checkout.pricing_snapshot = kwargs["pricing_snapshot"]  # type: ignore[assignment]
            world.checkout.amount_cents = kwargs["amount_cents"]  # type: ignore[assignment]
            return world.checkout

        async def reserve_canary_subscription_checkout(  # type: ignore[override]
            self, **kwargs: object
        ) -> BillingCheckoutSession:
            world.reservations.append(kwargs)
            world.checkout.id = kwargs["checkout_session_id"]  # type: ignore[assignment]
            world.checkout.next_due_date = kwargs["next_due_date"]  # type: ignore[assignment]
            world.control.canary_subscription_checkout_id = world.checkout.id
            return world.checkout

        async def subscription_checkout_blocker(
            self, *, builder_id: UUID
        ) -> str | None:
            world.blocker_checks += 1
            return world.blocker

        async def lock_checkout_session(
            self, *, builder_id: UUID, checkout_session_id: UUID
        ) -> BillingCheckoutSession | None:
            if (
                builder_id == world.checkout.builder_id
                and checkout_session_id == world.checkout.id
            ):
                return world.checkout
            return None

    monkeypatch.setattr(checkout_module, "BillingRepository", Repository)
    _prepare_settings(monkeypatch)

    async def reserve_topup(_db: object, **kwargs: object) -> BillingCheckoutSession:
        world.reservations.append(kwargs)
        world.checkout.id = kwargs["checkout_session_id"]  # type: ignore[assignment]
        if world.checkout.canary_slot == "topup":
            world.control.canary_topup_checkout_id = world.checkout.id
        return world.checkout

    monkeypatch.setattr(checkout_module, "require_billing_topup_rollout", reserve_topup)


class _RecordingClient:
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def create_checkout(
        self, request: object, *, permit: object
    ) -> CheckoutCreated:
        self.requests.append((request, permit))
        return CheckoutCreated("chk_000000000000002")


def _valid_capability() -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        environment="test",
        schema_version="credit_capability_ready.v1",
        approved_at=now - timedelta(minutes=5),
        expires_at=now + timedelta(hours=1),
        # Group 5 binding: operation, deployment, request, watermark and the
        # authoritative billing.fact.v1 schema digest.
        operation_id=uuid4(),
        deployment_digest="d" * 64,
        request_id=uuid4(),
        inbox_high_watermark=0,
        billing_fact_schema_digest=billing_fact_schema_digest(),
        reconciliation_complete=True,
        readback_complete=True,
        has_open_gap=False,
        has_backlog=False,
        has_alert=False,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (None, None),
        ("operation_id", None),
        ("deployment_digest", None),
        ("deployment_digest", "d" * 63),
        ("request_id", None),
        ("inbox_high_watermark", -1),
        ("billing_fact_schema_digest", "0" * 64),
        ("has_alert", True),
    ],
)
def test_capability_stub_exercises_every_group5_binding(
    field: str | None, value: object
) -> None:
    from src.repositories.billing import _capability_is_valid

    capability = _valid_capability()
    if field is not None:
        setattr(capability, field, value)
    assert _capability_is_valid(
        capability,  # type: ignore[arg-type]
        environment="test",
        now=datetime.now(timezone.utc),
    ) is (field is None)


@pytest.mark.asyncio
async def test_rollout_disabled_after_reservation_commit_sends_nothing_and_allows_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder_id = uuid4()
    world = _World(
        control=_general_control(), checkout=_checkout(builder_id=builder_id)
    )
    _install_world(monkeypatch, world)

    def disable() -> None:
        world.control.state = "disabled"

    db = _Session([None, None, 1], on_commit={1: disable})
    client = _RecordingClient()
    factory = MagicMock(return_value=client)

    with pytest.raises(CheckoutUnavailable) as exc_info:
        await BillingCheckoutService(db, client_factory=factory).create_checkout(
            builder_id=builder_id,
            offer_id="plan_plus_monthly_v1",
            payment_method="credit_card",
        )

    assert exc_info.value.code == "billing_checkout_unavailable"
    assert client.requests == []
    # Conclusively not sent: non-live and NOT marked as possibly sent.
    assert world.checkout.state == "failed"
    assert world.checkout.state not in checkout_module._LIVE_STATES
    assert world.checkout.reconciliation_attempts == 0
    assert world.checkout.external_checkout_id is None
    assert db.rollbacks >= 1
    # pre-check lock, reservation lock, pre-POST re-check lock
    assert world.lock_calls == 3

    # Once rollout is valid again the same offer can be retried and egresses.
    retry = _World(
        control=_general_control(), checkout=_checkout(builder_id=builder_id)
    )
    _install_world(monkeypatch, retry)
    retry_db = _Session([None, None, 1])
    retry_client = _RecordingClient()
    view = await BillingCheckoutService(
        retry_db, client_factory=lambda: retry_client
    ).create_checkout(
        builder_id=builder_id,
        offer_id="plan_plus_monthly_v1",
        payment_method="credit_card",
    )
    assert len(retry_client.requests) == 1
    assert view.state == "active"
    assert view.checkout_session_id == retry.checkout.id


@pytest.mark.asyncio
async def test_canary_builder_changed_after_commit_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder_id = uuid4()
    world = _World(
        control=_general_control(
            state="canary_allowlist", canary_builder_id=builder_id
        ),
        checkout=_canary_checkout(builder_id),
    )
    _install_world(monkeypatch, world)

    def change_allowlist() -> None:
        world.control.canary_builder_id = uuid4()

    db = _Session([None, None], on_commit={1: change_allowlist})
    client = _RecordingClient()

    with pytest.raises(CheckoutUnavailable):
        await BillingCheckoutService(db, client_factory=lambda: client).create_checkout(
            builder_id=builder_id,
            offer_id="plan_plus_monthly_v1",
            payment_method="credit_card",
        )

    assert client.requests == []
    assert world.checkout.state == "failed"
    assert len(world.reservations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["gate_closed", "capability_invalidated", "canary_slot_lost", "canary_promoted"],
)
async def test_topup_gate_or_canary_slot_invalid_after_commit_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    builder_id = uuid4()
    canary = case.startswith("canary")
    checkout = _topup_checkout(builder_id, canary=canary)
    if canary:
        control = _general_control(
            state="canary_allowlist",
            canary_builder_id=builder_id,
            canary_topup_checkout_id=checkout.id,
        )
    else:
        control = _general_control(
            topup_commercial_enabled=True, active_topup_capability_id=uuid4()
        )
    world = _World(control=control, checkout=checkout)
    _install_world(monkeypatch, world)
    scalars: list[object] = []

    def invalidate() -> None:
        if case == "gate_closed":
            world.control.topup_commercial_enabled = False
            world.control.active_topup_capability_id = None
        elif case == "capability_invalidated":
            stale = _valid_capability()
            stale.has_alert = True
            scalars.append(stale)
        elif case == "canary_slot_lost":
            world.control.canary_topup_checkout_id = uuid4()
        else:
            world.control.state = "general"

    db = _Session([None, None], scalars=scalars, on_commit={1: invalidate})
    client = _RecordingClient()

    with pytest.raises(CheckoutUnavailable):
        await BillingCheckoutService(db, client_factory=lambda: client).create_checkout(
            builder_id=builder_id,
            offer_id="package_credits_500_v1",
            payment_method="pix",
        )

    assert client.requests == []
    assert world.checkout.state == "failed"


@pytest.mark.asyncio
async def test_rollout_read_error_in_pre_post_recheck_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder_id = uuid4()
    world = _World(
        control=_general_control(),
        checkout=_checkout(builder_id=builder_id),
        fail_lock_call=3,  # the pre-POST re-check's rollout read
    )
    _install_world(monkeypatch, world)
    db = _Session([None, None, 1])
    client = _RecordingClient()

    with pytest.raises(CheckoutUnavailable):
        await BillingCheckoutService(db, client_factory=lambda: client).create_checkout(
            builder_id=builder_id,
            offer_id="plan_plus_monthly_v1",
            payment_method="credit_card",
        )

    assert client.requests == []
    assert world.checkout.state == "failed"
    assert db.rollbacks >= 1


@pytest.mark.asyncio
async def test_rollout_recheck_helper_error_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder_id = uuid4()
    world = _World(
        control=_general_control(), checkout=_checkout(builder_id=builder_id)
    )
    _install_world(monkeypatch, world)
    calls = {"count": 0}

    async def flaky_rollout(_db: object, _builder_id: UUID) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("rollout row unreadable")

    monkeypatch.setattr(
        checkout_module, "require_billing_checkout_rollout", flaky_rollout
    )
    db = _Session([None, None, 1])
    client = _RecordingClient()

    with pytest.raises(CheckoutUnavailable):
        await BillingCheckoutService(db, client_factory=lambda: client).create_checkout(
            builder_id=builder_id,
            offer_id="plan_plus_monthly_v1",
            payment_method="credit_card",
        )

    assert calls["count"] == 2
    assert client.requests == []
    assert world.checkout.state == "failed"


@pytest.mark.asyncio
async def test_valid_general_topup_reauthorizes_and_posts_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder_id = uuid4()
    checkout = _topup_checkout(builder_id)
    world = _World(
        control=_general_control(
            topup_commercial_enabled=True, active_topup_capability_id=uuid4()
        ),
        checkout=checkout,
    )
    _install_world(monkeypatch, world)
    db = _Session([None, None], scalars=[_valid_capability(), "pending"])
    client = _RecordingClient()

    view = await BillingCheckoutService(
        db, client_factory=lambda: client
    ).create_checkout(
        builder_id=builder_id,
        offer_id="package_credits_500_v1",
        payment_method="pix",
    )

    assert len(client.requests) == 1
    assert view.state == "active"
    assert world.lock_calls == 2
    assert db.scalars == []


# ---------------------------------------------------------------------------
# São Paulo commercial calendar (HIGH 2).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        (datetime(2026, 10, 12, 2, 59, 59, tzinfo=timezone.utc), date(2026, 10, 11)),
        (datetime(2026, 10, 12, 3, 0, 0, tzinfo=timezone.utc), date(2026, 10, 12)),
        (
            datetime(2026, 10, 12, 16, 59, 59, tzinfo=timezone(timedelta(hours=14))),
            date(2026, 10, 11),
        ),
    ],
)
def test_billing_local_date_follows_sao_paulo_midnight(
    instant: datetime, expected: date
) -> None:
    assert billing_local_date(instant) == expected


def test_billing_local_date_rejects_naive_instants() -> None:
    with pytest.raises(CheckoutUnavailable):
        billing_local_date(datetime(2026, 10, 12, 2, 59, 59))


@pytest.fixture
def _restore_host_timezone() -> Iterator[None]:
    original = os.environ.get("TZ")
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


@pytest.mark.asyncio
@pytest.mark.parametrize("host_tz", ["Etc/GMT-14", "Etc/GMT+12", "UTC"])
@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        (datetime(2026, 10, 12, 2, 59, 59, tzinfo=timezone.utc), date(2026, 10, 11)),
        (datetime(2026, 10, 12, 3, 0, 0, tzinfo=timezone.utc), date(2026, 10, 12)),
    ],
)
async def test_next_due_date_is_sao_paulo_today_whatever_the_host_timezone(
    monkeypatch: pytest.MonkeyPatch,
    _restore_host_timezone: None,
    host_tz: str,
    instant: datetime,
    expected: date,
) -> None:
    os.environ["TZ"] = host_tz
    time.tzset()
    builder_id = uuid4()
    world = _World(
        control=_general_control(), checkout=_checkout(builder_id=builder_id)
    )
    _install_world(monkeypatch, world)
    db = _Session([None, None, 1])
    client = _RecordingClient()

    await BillingCheckoutService(
        db, client_factory=lambda: client, clock=lambda: instant
    ).create_checkout(
        builder_id=builder_id,
        offer_id="plan_plus_monthly_v1",
        payment_method="credit_card",
    )

    assert world.reservations[0]["next_due_date"] == expected
    request = client.requests[0][0]
    assert request.next_due_date == expected  # type: ignore[attr-defined]


def test_default_clock_is_utc_aware_not_host_local(
    _restore_host_timezone: None,
) -> None:
    os.environ["TZ"] = "Etc/GMT-14"
    time.tzset()
    instant = checkout_module._utc_now()
    assert instant.utcoffset() == timedelta(0)
    assert checkout_module.BILLING_TIMEZONE.key == "America/Sao_Paulo"


# ---------------------------------------------------------------------------
# Coupon selector (HIGH 3).
# ---------------------------------------------------------------------------


def _coupon(**overrides: object) -> Coupon:
    values: dict[str, object] = {
        "id": uuid4(),
        "code": "PROMO10",
        "discount_percent": 10,
        "max_uses": 5,
        "uses_count": 1,
        "expires_at": datetime(2027, 1, 1, tzinfo=timezone.utc),
        "is_active": True,
    }
    values.update(overrides)
    return Coupon(**values)


@pytest.mark.asyncio
async def test_coupon_selector_is_resolved_server_side_and_frozen_as_integers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder_id = uuid4()
    world = _World(
        control=_general_control(), checkout=_checkout(builder_id=builder_id)
    )
    _install_world(monkeypatch, world)
    # pre-lock live, pre-lock coupon, locked live, locked coupon, clinic count
    db = _Session([None, _coupon(), None, _coupon(), 2])
    client = _RecordingClient()

    await BillingCheckoutService(
        db, client_factory=lambda: client, clock=lambda: _LATE_EVENING_UTC
    ).create_checkout(
        builder_id=builder_id,
        offer_id="plan_plus_monthly_v1",
        payment_method="credit_card",
        coupon_code="  promo10 ",
    )

    reservation = world.reservations[0]
    snapshot = reservation["pricing_snapshot"]
    assert isinstance(snapshot, dict)
    gross = 99_900 + 20_000  # one additional clinic above the included one
    assert snapshot["coupon_code"] == "PROMO10"
    assert snapshot["amount_cents"] == gross - gross * 10 // 100 == 107_910
    assert reservation["amount_cents"] == 107_910
    assert type(snapshot["amount_cents"]) is int
    assert len(client.requests) == 1
    item = client.requests[0][0].items[0]  # type: ignore[attr-defined]
    assert item.unit_amount_cents == 107_910
    assert db.results == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "coupon",
    [
        None,
        _coupon(is_active=False),
        _coupon(expires_at=datetime(2026, 10, 1, tzinfo=timezone.utc)),
        _coupon(max_uses=3, uses_count=3),
        _coupon(expires_at=datetime(2027, 1, 1)),  # naive: undefined instant
        _coupon(discount_percent=0),
    ],
    ids=["absent", "inactive", "expired", "exhausted", "naive-expiry", "zero"],
)
async def test_invalid_coupon_is_one_fixed_code_before_any_lock_row_or_network(
    monkeypatch: pytest.MonkeyPatch,
    coupon: Coupon | None,
) -> None:
    builder_id = uuid4()
    world = _World(
        control=_general_control(), checkout=_checkout(builder_id=builder_id)
    )
    _install_world(monkeypatch, world)
    db = _Session([None, coupon])
    factory = MagicMock(side_effect=AssertionError("provider reached"))

    with pytest.raises(CheckoutCouponRejected) as exc_info:
        await BillingCheckoutService(
            db, client_factory=factory, clock=lambda: _LATE_EVENING_UTC
        ).create_checkout(
            builder_id=builder_id,
            offer_id="plan_plus_monthly_v1",
            payment_method="credit_card",
            coupon_code="PROMO10",
        )

    assert exc_info.value.code == "billing_checkout_coupon_invalid"
    assert world.lock_calls == 0
    assert world.reservations == []
    assert db.commits == 0
    assert db.added == []
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_live_session_is_returned_without_resolving_the_coupon_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder_id = uuid4()
    live = _checkout(builder_id=builder_id, state="active")
    live.external_checkout_id = "chk_000000000000003"
    world = _World(control=_general_control(), checkout=live)
    _install_world(monkeypatch, world)
    # Only the lock-free live read is available: any coupon resolution would
    # pop from an empty queue and fail this test.
    db = _Session([live])
    factory = MagicMock(side_effect=AssertionError("provider reached"))

    view = await BillingCheckoutService(db, client_factory=factory).create_checkout(
        builder_id=builder_id,
        offer_id="plan_plus_monthly_v1",
        payment_method="credit_card",
        coupon_code="EXHAUSTED-NOW",
    )

    assert view.checkout_session_id == live.id
    assert world.lock_calls == 0
    assert db.results == []
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_coupon_on_canary_subscription_fails_closed_before_egress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder_id = uuid4()
    world = _World(
        control=_general_control(
            state="canary_allowlist", canary_builder_id=builder_id
        ),
        checkout=_canary_checkout(builder_id),
    )
    _install_world(monkeypatch, world)
    db = _Session([None, _coupon(), None])
    client = _RecordingClient()
    factory = MagicMock(return_value=client)

    with pytest.raises(CheckoutCouponRejected) as exc_info:
        await BillingCheckoutService(db, client_factory=factory).create_checkout(
            builder_id=builder_id,
            offer_id="plan_plus_monthly_v1",
            payment_method="credit_card",
            coupon_code="PROMO10",
        )

    assert exc_info.value.code == COUPON_NOT_ELIGIBLE
    assert world.reservations == []
    assert db.commits == 0
    assert db.rollbacks >= 1
    # Building the client is a pre-reservation precondition; no POST.
    assert client.requests == []


@pytest.mark.asyncio
async def test_full_discount_coupon_is_not_eligible_for_hosted_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder_id = uuid4()
    world = _World(
        control=_general_control(), checkout=_checkout(builder_id=builder_id)
    )
    _install_world(monkeypatch, world)
    db = _Session([None, _coupon(discount_percent=100)])
    factory = MagicMock(side_effect=AssertionError("provider reached"))

    with pytest.raises(CheckoutCouponRejected) as exc_info:
        await BillingCheckoutService(
            db, client_factory=factory, clock=lambda: _LATE_EVENING_UTC
        ).create_checkout(
            builder_id=builder_id,
            offer_id="plan_plus_monthly_v1",
            payment_method="credit_card",
            coupon_code="FREE",
        )

    assert exc_info.value.code == COUPON_NOT_ELIGIBLE
    assert world.lock_calls == 0
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_coupon_invalidated_between_precheck_and_lock_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder_id = uuid4()
    world = _World(
        control=_general_control(), checkout=_checkout(builder_id=builder_id)
    )
    _install_world(monkeypatch, world)
    db = _Session([None, _coupon(), None, _coupon(max_uses=1, uses_count=1)])
    client = _RecordingClient()
    factory = MagicMock(return_value=client)

    with pytest.raises(CheckoutCouponRejected):
        await BillingCheckoutService(
            db, client_factory=factory, clock=lambda: _LATE_EVENING_UTC
        ).create_checkout(
            builder_id=builder_id,
            offer_id="plan_plus_monthly_v1",
            payment_method="credit_card",
            coupon_code="PROMO10",
        )

    assert world.reservations == []
    assert db.commits == 0
    assert db.rollbacks >= 1
    # Building the client is a pre-reservation precondition; no POST.
    assert client.requests == []


@pytest.mark.asyncio
async def test_topup_never_accepts_a_coupon_selector() -> None:
    db = _Session([])
    factory = MagicMock(side_effect=AssertionError("provider reached"))
    with pytest.raises(checkout_module.CheckoutInputRejected):
        await BillingCheckoutService(db, client_factory=factory).create_checkout(
            builder_id=uuid4(),
            offer_id="package_credits_500_v1",
            payment_method="pix",
            coupon_code="PROMO10",
        )
    factory.assert_not_called()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("promo10", "PROMO10"),
        ("  Promo10\t", "PROMO10"),
        ("A" * 50, "A" * 50),
    ],
)
def test_coupon_selector_normalization_matches_stored_codes(
    raw: str | None, expected: str | None
) -> None:
    assert normalize_coupon_code(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "A" * 51])
def test_coupon_selector_outside_stored_length_is_rejected(raw: str) -> None:
    with pytest.raises(CheckoutCouponRejected):
        normalize_coupon_code(raw)


# ---------------------------------------------------------------------------
# Fix loop 2 — HIGH B (eligibility) and HIGH C (pre-reservation preconditions).
# ---------------------------------------------------------------------------


def _canary_world(builder_id: UUID) -> _World:
    return _World(
        control=_general_control(
            state="canary_allowlist", canary_builder_id=builder_id
        ),
        checkout=_canary_checkout(builder_id),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocker",
    [
        "abacatepay_subscription",
        "asaas_subscription",
        "unresolved_subscription",
        "migration",
        "live_subscription_checkout",
    ],
)
async def test_ineligible_builder_gets_one_stable_code_with_no_row_or_post(
    monkeypatch: pytest.MonkeyPatch,
    blocker: str,
) -> None:
    builder_id = uuid4()
    world = _World(
        control=_general_control(),
        checkout=_checkout(builder_id=builder_id),
        blocker=blocker,
    )
    _install_world(monkeypatch, world)
    db = _Session([None, None])
    client = _RecordingClient()

    with pytest.raises(checkout_module.CheckoutNotEligible) as exc_info:
        await BillingCheckoutService(db, client_factory=lambda: client).create_checkout(
            builder_id=builder_id,
            offer_id="plan_pro_monthly_v1",
            payment_method="credit_card",
        )

    assert exc_info.value.code == "billing_checkout_not_eligible"
    assert blocker not in repr(exc_info.value)  # the reason never leaves
    assert world.blocker_checks == 1
    assert world.lock_calls == 2  # checked while the rollout lock is held
    assert world.reservations == []
    assert client.requests == []
    assert db.commits == 0
    assert db.rollbacks >= 1


@pytest.mark.asyncio
async def test_canary_subscription_is_also_eligibility_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder_id = uuid4()
    world = _canary_world(builder_id)
    world.blocker = "abacatepay_subscription"
    _install_world(monkeypatch, world)
    client = _RecordingClient()

    with pytest.raises(checkout_module.CheckoutNotEligible):
        await BillingCheckoutService(
            _Session([None, None]), client_factory=lambda: client
        ).create_checkout(
            builder_id=builder_id,
            offer_id="plan_plus_monthly_v1",
            payment_method="credit_card",
        )

    assert world.reservations == []
    assert world.control.canary_subscription_checkout_id is None
    assert client.requests == []


@pytest.mark.asyncio
async def test_eligible_builder_and_topups_are_not_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder_id = uuid4()
    world = _World(
        control=_general_control(), checkout=_checkout(builder_id=builder_id)
    )
    _install_world(monkeypatch, world)
    client = _RecordingClient()
    await BillingCheckoutService(
        _Session([None, None, 1]), client_factory=lambda: client
    ).create_checkout(
        builder_id=builder_id,
        offer_id="plan_plus_monthly_v1",
        payment_method="credit_card",
    )
    assert world.blocker_checks == 1
    assert len(client.requests) == 1

    # A live subscription does not block a detached top-up (no eligibility).
    topup = _World(
        control=_general_control(
            topup_commercial_enabled=True, active_topup_capability_id=uuid4()
        ),
        checkout=_topup_checkout(builder_id),
        blocker="live_subscription_checkout",
    )
    _install_world(monkeypatch, topup)
    topup_client = _RecordingClient()
    await BillingCheckoutService(
        _Session([None, None], scalars=[_valid_capability(), "pending"]),
        client_factory=lambda: topup_client,
    ).create_checkout(
        builder_id=builder_id,
        offer_id="package_credits_500_v1",
        payment_method="pix",
    )
    assert topup.blocker_checks == 0
    assert len(topup_client.requests) == 1


@pytest.mark.asyncio
async def test_same_offer_retry_returns_the_live_session_before_eligibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder_id = uuid4()
    live = _checkout(builder_id=builder_id, state="active")
    live.external_checkout_id = "chk_000000000000004"
    world = _World(
        control=_general_control(),
        checkout=live,
        blocker="live_subscription_checkout",
    )
    _install_world(monkeypatch, world)
    # The race variant: absent at the lock-free read, present under the lock.
    db = _Session([None, live])
    client = _RecordingClient()

    view = await BillingCheckoutService(
        db, client_factory=lambda: client
    ).create_checkout(
        builder_id=builder_id,
        offer_id="plan_plus_monthly_v1",
        payment_method="credit_card",
    )

    assert view.checkout_session_id == live.id
    assert world.blocker_checks == 0
    assert world.reservations == []
    assert client.requests == []


class _Reader:
    def __init__(self, result: object = True, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    async def is_enabled(self, flag_name: str) -> bool:
        assert flag_name == "financial_egress_enabled"
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result  # type: ignore[return-value]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["flag_false", "flag_error", "empty_return_url", "client_build_error"],
)
@pytest.mark.parametrize("kind", ["subscription", "topup"])
async def test_side_effect_free_precondition_failure_never_reserves_a_canary_slot(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    kind: str,
) -> None:
    from src.services.egress_policy import EgressPolicy

    builder_id = uuid4()
    if kind == "subscription":
        world = _canary_world(builder_id)
        offer_id, method = "plan_plus_monthly_v1", "credit_card"
    else:
        world = _World(
            control=_general_control(
                state="canary_allowlist", canary_builder_id=builder_id
            ),
            checkout=_topup_checkout(builder_id, canary=True),
        )
        offer_id, method = "package_credits_500_v1", "pix"
    _install_world(monkeypatch, world)
    reader = _Reader(
        result=False if failure == "flag_false" else True,
        error=RuntimeError("agent down") if failure == "flag_error" else None,
    )
    from src.services import asaas as asaas_module

    monkeypatch.setattr(
        checkout_module,
        "authorize_creation_egress",
        asaas_module.authorize_creation_egress,
    )
    monkeypatch.setattr(
        checkout_module,
        "require_financial_egress",
        EgressPolicy(reader).require_financial_egress,
    )
    if failure == "empty_return_url":
        monkeypatch.setattr(checkout_module.settings, "asaas_checkout_return_url", "")
    client = _RecordingClient()

    def factory() -> _RecordingClient:
        if failure == "client_build_error":
            raise checkout_module.CheckoutInputRejected()
        return client

    db = _Session([None])
    with pytest.raises(CheckoutUnavailable):
        await BillingCheckoutService(db, client_factory=factory).create_checkout(
            builder_id=builder_id, offer_id=offer_id, payment_method=method
        )

    assert world.lock_calls == 0
    assert world.reservations == []
    assert db.added == []  # no top-up order either
    assert db.commits == 0
    assert world.control.canary_subscription_checkout_id is None
    assert world.control.canary_topup_checkout_id is None
    assert client.requests == []
    if failure == "empty_return_url":
        assert reader.calls == 0  # URL is checked before the flag is read
    else:
        assert reader.calls == 1  # gate read only; no reservation followed


@pytest.mark.asyncio
async def test_permit_reaching_the_post_is_minted_after_the_rollout_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services import asaas as asaas_module
    from src.services.egress_policy import EgressPolicy

    builder_id = uuid4()
    world = _canary_world(builder_id)
    _install_world(monkeypatch, world)
    reader = _Reader(True)
    monkeypatch.setattr(
        checkout_module,
        "authorize_creation_egress",
        asaas_module.authorize_creation_egress,
    )
    monkeypatch.setattr(
        checkout_module,
        "require_financial_egress",
        EgressPolicy(reader).require_financial_egress,
    )
    client = _RecordingClient()

    view = await BillingCheckoutService(
        _Session([None, None]), client_factory=lambda: client
    ).create_checkout(
        builder_id=builder_id,
        offer_id="plan_plus_monthly_v1",
        payment_method="credit_card",
    )

    assert view.state == "active"
    # One gate read before the reservation, one permit-minting read after the
    # fresh rollout re-check.
    assert reader.calls == 2
    request, permit = client.requests[0]  # type: ignore[misc]
    assert isinstance(permit, asaas_module.CreationEgressPermit)
    assert request.external_reference == world.checkout.id  # type: ignore[attr-defined]
    assert world.control.canary_subscription_checkout_id == world.checkout.id


class _FlippingReader(_Reader):
    """Open for the pre-reservation gate, closed (or broken) afterwards."""

    def __init__(self, *, then_error: bool) -> None:
        super().__init__(True)
        self.then_error = then_error

    async def is_enabled(self, flag_name: str) -> bool:
        assert flag_name == "financial_egress_enabled"
        self.calls += 1
        if self.calls == 1:
            return True
        if self.then_error:
            raise RuntimeError("agent down")
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("then_error", [False, True])
async def test_kill_switch_thrown_after_the_reservation_still_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
    then_error: bool,
) -> None:
    from src.services import asaas as asaas_module
    from src.services.egress_policy import EgressPolicy

    builder_id = uuid4()
    world = _World(
        control=_general_control(), checkout=_checkout(builder_id=builder_id)
    )
    _install_world(monkeypatch, world)
    reader = _FlippingReader(then_error=then_error)
    monkeypatch.setattr(
        checkout_module,
        "authorize_creation_egress",
        asaas_module.authorize_creation_egress,
    )
    monkeypatch.setattr(
        checkout_module,
        "require_financial_egress",
        EgressPolicy(reader).require_financial_egress,
    )
    client = _RecordingClient()

    with pytest.raises(CheckoutUnavailable):
        # Results: lock-free live read, locked live re-check, clinic count.
        await BillingCheckoutService(
            _Session([None, None, 1]), client_factory=lambda: client
        ).create_checkout(
            builder_id=builder_id,
            offer_id="plan_plus_monthly_v1",
            payment_method="credit_card",
        )

    assert reader.calls == 2
    assert len(world.reservations) == 1  # the race reached the reservation
    assert client.requests == []
    assert world.checkout.state == "failed"  # conclusive, never sent
