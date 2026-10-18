"""Route and model tests for the builder billing read contract (ASAAS Group 6b).

The PostgreSQL proofs (no financial row changes, builder isolation,
``available_offer_ids``/``POST`` agreement in every rollout state, the
top-up truth table) live in
``tests/integration/test_billing_read_contract_postgres.py``.
"""

from __future__ import annotations

import functools
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, get_args
from uuid import UUID, uuid4

import jwt
import pytest
import structlog
from prometheus_client import REGISTRY
from pydantic import ValidationError

from src.api.routes import billing
from src.api.schemas.billing import (
    BillingCatalogOfferResponse,
    BillingCatalogResponse,
    BillingCheckoutSummaryResponse,
    BillingLiveCheckoutResponse,
    BillingSubscriptionSummaryResponse,
    CheckoutReconciliationResponse,
)
from src.config.settings import settings
from src.models.billing import BillingRolloutControl
from src.models.builder import Builder
from src.models.tenant import Tenant
from src.repositories import get_db_session
from src.repositories.billing import (
    LIVE_CHECKOUT_STATES,
    TERMINAL_SUBSCRIPTION_STATUSES,
    BillingRepository,
)
from src.services import billing_read_contract as read_contract_module
from src.services.billing_catalog import BILLING_CATALOG, CATALOG_VERSION_ID
from src.services.billing_checkout import (
    CheckoutNotEligible,
    CheckoutReconciliationRequired,
    CheckoutView,
)
from src.services.billing_read_contract import (
    BillingReadUnavailable,
    CommercialReadback,
    LiveCheckoutReadback,
    LiveCheckoutState,
    SubscriptionFinancialStatus,
    SubscriptionReadback,
)
from src.services.billing_rollout import BillingCheckoutUnavailable

_FROZEN_NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)

# The exact bytes the pre-Group-6b route produced for the golden fixture
# (captured from origin/main 975bc394 before the route was changed).
_LEGACY_GOLDEN = (
    b'{"plan":"pro","active_clinics":3,"free_tier":2,"extra_clinics":1,'
    b'"base_price":2000,"extra_price":350,"total_monthly":2350,'
    b'"total_annual":23970,"discount_pct":15,"subscription_status":"active",'
    b'"billing_cycle":"annual","usage":{"plan":"pro",'
    b'"as_of":"2026-10-18T12:00:00Z","trace_period":'
    b'{"start":"2026-10-01T00:00:00Z","end":"2026-11-01T00:00:00Z",'
    b'"timezone":"UTC"},"traces":{"used":0,"included":10000,'
    b'"extra_credits":0,"available":10000,"overage":0,"percent_used":0.0},'
    b'"storage":{"rag":{"used_bytes":0,"used_mb":0.0,"limit_bytes":500000000,'
    b'"limit_mb":500,"available_bytes":500000000,"available_mb":500.0,'
    b'"overage_bytes":0,"overage_mb":0.0,"percent_used":0.0},'
    b'"media":{"used_bytes":0,"used_mb":0.0,"limit_bytes":500000000,'
    b'"limit_mb":500,"available_bytes":500000000,"available_mb":500.0,'
    b'"overage_bytes":0,"overage_mb":0.0,"percent_used":0.0}},'
    b'"credits":{"purchase_enabled":false,"coming_soon":true}}}'
)

_EXPECTED_CATALOG = {
    "catalog_version_id": "payment-2026-10-03",
    "offers": [
        {
            "offer_id": "plan_plus_monthly_v1",
            "kind": "subscription",
            "currency": "BRL",
            "amount_cents": 99_900,
            "credits": 5_000,
            "included_clinics": 1,
            "additional_clinic_amount_cents": 20_000,
        },
        {
            "offer_id": "plan_pro_monthly_v1",
            "kind": "subscription",
            "currency": "BRL",
            "amount_cents": 250_000,
            "credits": 15_000,
            "included_clinics": 3,
            "additional_clinic_amount_cents": 15_000,
        },
        {
            "offer_id": "plan_max_monthly_v1",
            "kind": "subscription",
            "currency": "BRL",
            "amount_cents": 500_000,
            "credits": 35_000,
            "included_clinics": 10,
            "additional_clinic_amount_cents": 10_000,
        },
        {
            "offer_id": "package_credits_500_v1",
            "kind": "topup",
            "currency": "BRL",
            "amount_cents": 10_000,
            "credits": 500,
            "included_clinics": 0,
            "additional_clinic_amount_cents": 0,
        },
        {
            "offer_id": "package_credits_1000_v1",
            "kind": "topup",
            "currency": "BRL",
            "amount_cents": 20_000,
            "credits": 1_000,
            "included_clinics": 0,
            "additional_clinic_amount_cents": 0,
        },
        {
            "offer_id": "package_credits_2000_v1",
            "kind": "topup",
            "currency": "BRL",
            "amount_cents": 40_000,
            "credits": 2_000,
            "included_clinics": 0,
            "additional_clinic_amount_cents": 0,
        },
        {
            "offer_id": "package_credits_4000_v1",
            "kind": "topup",
            "currency": "BRL",
            "amount_cents": 80_000,
            "credits": 4_000,
            "included_clinics": 0,
            "additional_clinic_amount_cents": 0,
        },
    ],
}


def _token(role: str, *, builder_id: UUID | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(uuid4()),
        "tenant_id": str(uuid4()),
        "role": role,
        "email": f"{role}@test.com",
        "iat": now,
        "exp": now + timedelta(hours=1),
        "jti": str(uuid4()),
    }
    if builder_id is not None:
        payload["builder_id"] = str(builder_id)
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def _headers(builder_id: UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token('builder', builder_id=builder_id)}"}


async def _seed_golden_builder(builder_id: UUID) -> None:
    session = get_db_session()
    try:
        session.add(
            Builder(
                id=builder_id,
                name="Golden summary builder",
                subscription_plan="pro",
                subscription_status="active",
                billing_cycle="annual",
            )
        )
        await session.flush()
        clinics = [(True, "start")] * 3 + [(False, "start"), (True, "admin")]
        for index, (active, plan_type) in enumerate(clinics):
            session.add(
                Tenant(
                    id=uuid4(),
                    name=f"Golden clinic {index}",
                    slug=f"golden-{index}-{uuid4().hex}",
                    api_key_hash=f"hash-{uuid4().hex}",
                    builder_id=builder_id,
                    is_active=active,
                    plan_type=plan_type,
                    config={},
                )
            )
        await session.commit()
    finally:
        await session.close()


def _freeze_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        billing,
        "get_builder_billing_usage",
        functools.partial(billing.get_builder_billing_usage, now=_FROZEN_NOW),
    )


# -- legacy summary contract -------------------------------------------------


def test_summary_keeps_every_legacy_byte_and_only_appends_new_fields(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id = uuid4()
    client._submit(_seed_golden_builder(builder_id))
    _freeze_usage(monkeypatch)

    response = client.get("/v1/billing/summary", headers=_headers(builder_id))

    assert response.status_code == 200
    # Byte-for-byte: the legacy object up to its last field, then the
    # Group 6b fields appended after ``usage``.
    assert response.content.startswith(_LEGACY_GOLDEN[:-1] + b',"subscription":')
    body = response.json()
    legacy = json.loads(_LEGACY_GOLDEN)
    assert {key: body[key] for key in legacy} == legacy
    assert list(body)[: len(legacy)] == list(legacy)
    # No billing rows and no rollout row: everything answers closed.
    assert {key: body[key] for key in body if key not in legacy} == {
        "subscription": None,
        "checkout": {
            "available_offer_ids": [],
            "live_session": None,
            "available": True,
        },
        "topup_commercial_enabled": False,
    }


def test_summary_and_catalog_require_builder_login(client) -> None:
    for path in ("/v1/billing/summary", "/v1/billing/catalog"):
        assert client.get(path).status_code == 401
        denied = client.get(
            path, headers={"Authorization": f"Bearer {_token('clinic_admin')}"}
        )
        assert denied.status_code == 403
        assert "available_offer_ids" not in denied.text
        assert "catalog_version_id" not in denied.text


# -- catalog -----------------------------------------------------------------


def test_catalog_route_returns_the_closed_versioned_snapshot(client) -> None:
    builder_id = uuid4()
    response = client.get("/v1/billing/catalog", headers=_headers(builder_id))

    assert response.status_code == 200
    assert response.json() == _EXPECTED_CATALOG
    assert response.json()["catalog_version_id"] == CATALOG_VERSION_ID
    assert [offer["offer_id"] for offer in response.json()["offers"]] == list(
        BILLING_CATALOG
    )
    # Internal snapshot fields never leave the backend.
    assert "cycle" not in response.text
    assert "additional_clinic_credit_grant" not in response.text


def test_catalog_route_ignores_client_supplied_identity(client) -> None:
    builder_id = uuid4()
    plain = client.get("/v1/billing/catalog", headers=_headers(builder_id))
    spoofed = client.get(
        f"/v1/billing/catalog?builder_id={uuid4()}&tenant_id={uuid4()}",
        headers=_headers(uuid4()),
    )
    assert plain.status_code == spoofed.status_code == 200
    assert plain.content == spoofed.content


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("currency", "USD", "Input should be 'BRL'"),
        ("kind", "annual", "Input should be 'subscription' or 'topup'"),
        ("amount_cents", 0, "Input should be greater than 0"),
        ("credits", 0, "Input should be greater than 0"),
        ("included_clinics", -1, "Input should be greater than or equal to 0"),
        (
            "additional_clinic_amount_cents",
            -1,
            "Input should be greater than or equal to 0",
        ),
        ("cycle", "MONTHLY", "Extra inputs are not permitted"),
    ],
)
def test_catalog_offer_model_is_closed(field: str, value: object, match: str) -> None:
    payload = dict(_EXPECTED_CATALOG["offers"][0])  # type: ignore[index]
    payload[field] = value
    with pytest.raises(ValidationError, match=match):
        BillingCatalogOfferResponse.model_validate(payload)


def test_catalog_response_model_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        BillingCatalogResponse.model_validate(
            {**_EXPECTED_CATALOG, "price_override": 1}
        )


# -- summary read-contract models ---------------------------------------------


def test_subscription_status_literal_is_the_non_terminal_asaas_status_set() -> None:
    check_admitted = {
        "active",
        "inactive",
        "cancelled",
        "active_linked",
        "readback_required",
        "non_billable_pending_readback",
    }  # ck_billing_subscriptions_status minus the provider-NULL legacy states
    assert set(get_args(SubscriptionFinancialStatus)) == check_admitted - set(
        TERMINAL_SUBSCRIPTION_STATUSES
    )
    assert set(get_args(LiveCheckoutState)) == set(LIVE_CHECKOUT_STATES)


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"status": "inactive"}, "Input should be 'active', 'active_linked'"),
        ({"status": "trial"}, "Input should be 'active', 'active_linked'"),
        ({"amount_cents": -1}, "Input should be greater than or equal to 0"),
        ({"offer_id": ""}, "String should have at least 1 character"),
        ({"builder_id": str(uuid4())}, "Extra inputs are not permitted"),
        ({"external_subscription_id": "sub_1"}, "Extra inputs are not permitted"),
    ],
)
def test_subscription_summary_model_is_closed(
    override: dict[str, object], match: str
) -> None:
    payload: dict[str, object] = {
        "offer_id": "plan_plus_monthly_v1",
        "catalog_version_id": "payment-2026-10-03",
        "status": "active",
        "amount_cents": 99_900,
        "current_period_end": "2026-11-11",
    }
    payload.update(override)
    with pytest.raises(ValidationError, match=match):
        BillingSubscriptionSummaryResponse.model_validate(payload)


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"state": "completed"}, "Input should be 'creating', 'active'"),
        ({"kind": "annual"}, "Input should be 'subscription' or 'topup'"),
        ({"checkout_url": "https://x"}, "Extra inputs are not permitted"),
    ],
)
def test_live_session_model_is_closed(override: dict[str, object], match: str) -> None:
    payload: dict[str, object] = {
        "checkout_session_id": str(uuid4()),
        "kind": "subscription",
        "state": "active",
    }
    payload.update(override)
    with pytest.raises(ValidationError, match=match):
        BillingLiveCheckoutResponse.model_validate(payload)


def test_checkout_summary_model_rejects_a_blocking_reason() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        BillingCheckoutSummaryResponse.model_validate(
            {
                "available_offer_ids": [],
                "live_session": None,
                "available": True,
                "blocked_reason": "migration",
            }
        )


def _stub_reads(
    monkeypatch: pytest.MonkeyPatch,
    *,
    subscription: SubscriptionReadback | None,
    live: LiveCheckoutReadback | None,
    commercial: CommercialReadback,
    seen: list[UUID],
) -> None:
    async def current(db: object, *, builder_id: UUID) -> object:
        seen.append(builder_id)
        return subscription

    async def live_read(db: object, *, builder_id: UUID) -> object:
        seen.append(builder_id)
        return live

    async def availability(db: object, *, builder_id: UUID) -> object:
        seen.append(builder_id)
        return commercial

    monkeypatch.setattr(billing, "read_current_subscription", current)
    monkeypatch.setattr(billing, "read_live_checkout", live_read)
    monkeypatch.setattr(billing, "read_commercial_availability", availability)


def test_summary_serializes_the_read_contract_from_the_jwt_builder_only(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id = uuid4()
    client._submit(_seed_golden_builder(builder_id))
    _freeze_usage(monkeypatch)
    session_id = uuid4()
    seen: list[UUID] = []
    _stub_reads(
        monkeypatch,
        subscription=SubscriptionReadback(
            offer_id="plan_pro_monthly_v1",
            catalog_version_id="payment-2026-10-03",
            status="active",
            amount_cents=265_000,
            current_period_end=date(2026, 11, 11),
        ),
        live=LiveCheckoutReadback(
            checkout_session_id=session_id, kind="topup", state="active"
        ),
        commercial=CommercialReadback(
            available_offer_ids=("package_credits_500_v1", "package_credits_1000_v1"),
            topup_commercial_enabled=True,
            available=True,
        ),
        seen=seen,
    )

    response = client.get(
        f"/v1/billing/summary?builder_id={uuid4()}&tenant_id={uuid4()}",
        headers=_headers(builder_id),
    )

    assert response.status_code == 200
    assert seen == [builder_id, builder_id, builder_id]
    body = response.json()
    assert body["subscription"] == {
        "offer_id": "plan_pro_monthly_v1",
        "catalog_version_id": "payment-2026-10-03",
        "status": "active",
        "amount_cents": 265_000,
        "current_period_end": "2026-11-11",
    }
    assert body["checkout"] == {
        "available_offer_ids": ["package_credits_500_v1", "package_credits_1000_v1"],
        "live_session": {
            "checkout_session_id": str(session_id),
            "kind": "topup",
            "state": "active",
        },
        "available": True,
    }
    assert body["topup_commercial_enabled"] is True
    # The legacy fields stay the builder's own, untouched by the new reads.
    legacy = json.loads(_LEGACY_GOLDEN)
    assert {key: body[key] for key in legacy} == legacy


@pytest.mark.parametrize("failure", ["subscription", "live"])
def test_summary_fails_closed_when_the_financial_state_is_unreadable(
    client, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    builder_id = uuid4()
    client._submit(_seed_golden_builder(builder_id))

    async def unavailable(db: object, *, builder_id: UUID) -> object:
        raise BillingReadUnavailable()

    async def broken(db: object, *, builder_id: UUID) -> object:
        raise RuntimeError("relation does not exist")

    target = (
        "read_current_subscription"
        if failure == "subscription"
        else ("read_live_checkout")
    )
    for replacement in (unavailable, broken):
        monkeypatch.setattr(billing, target, replacement)
        response = client.get("/v1/billing/summary", headers=_headers(builder_id))
        assert response.status_code == 503
        assert response.json() == {"detail": "billing_summary_unavailable"}
        # Never a fabricated "no subscription" body.
        assert "subscription_status" not in response.text


@pytest.mark.parametrize("failure", ["subscription", "live"])
def test_financial_read_failure_log_carries_no_exception_text(
    client, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    builder_id = uuid4()
    client._submit(_seed_golden_builder(builder_id))
    # No quote characters: json.dumps would escape them and make the
    # absence check below vacuous.
    leaked = "relation billing_secret_table does not exist at 10.0.0.7"

    async def broken(db: object, *, builder_id: UUID) -> object:
        raise RuntimeError(leaked)

    target = (
        "read_current_subscription"
        if failure == "subscription"
        else "read_live_checkout"
    )
    monkeypatch.setattr(billing, target, broken)
    with structlog.testing.capture_logs() as records:
        response = client.get("/v1/billing/summary", headers=_headers(builder_id))

    assert response.status_code == 503
    # Pre-registered guard: the warning was emitted, so the absence check
    # below is not vacuous.
    events = [
        record
        for record in records
        if record["event"] == "Billing summary financial read unavailable"
    ]
    assert len(events) == 1
    assert events[0]["log_level"] == "warning"
    serialized = json.dumps(records, default=str)
    assert leaked not in serialized
    assert "billing_secret_table" not in serialized


# -- advisory availability failure: explicit marker, fail-closed for buying ------


def _unavailable_count() -> float:
    return (
        REGISTRY.get_sample_value(
            "neurostack_billing_summary_checkout_unavailable_total"
        )
        or 0.0
    )


def _live_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    async def live_read(db: object, *, builder_id: UUID) -> object:
        return LiveCheckoutReadback(
            checkout_session_id=uuid4(), kind="subscription", state="active"
        )

    monkeypatch.setattr(billing, "read_live_checkout", live_read)


async def _open_precheck(db: object, builder_id: UUID) -> None:
    return None


def _assert_marked_unavailable(response: Any) -> None:
    assert response.status_code == 200
    body = response.json()
    assert body["checkout"] == {
        "available_offer_ids": [],
        "live_session": None,
        "available": False,
    }
    assert body["topup_commercial_enabled"] is False
    legacy = json.loads(_LEGACY_GOLDEN)
    assert {key: body[key] for key in legacy} == legacy
    assert response.content.startswith(_LEGACY_GOLDEN[:-1] + b',"subscription":')


def test_summary_marks_a_failed_advisory_read_unavailable_and_counts_it(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id = uuid4()
    client._submit(_seed_golden_builder(builder_id))
    _freeze_usage(monkeypatch)
    _live_stub(monkeypatch)

    async def broken(db: object, *, builder_id: UUID, now: datetime) -> object:
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr(
        read_contract_module, "require_billing_checkout_rollout", _open_precheck
    )
    monkeypatch.setattr(read_contract_module, "_commercial_availability", broken)
    before = _unavailable_count()

    with structlog.testing.capture_logs() as records:
        response = client.get("/v1/billing/summary", headers=_headers(builder_id))

    _assert_marked_unavailable(response)
    assert _unavailable_count() == before + 1
    events = [
        record
        for record in records
        if record["event"] == "billing summary checkout availability unavailable"
    ]
    assert events == [
        {
            "event": "billing summary checkout availability unavailable",
            "error_type": "RuntimeError",
            "log_level": "warning",
        }
    ]
    assert "relation does not exist" not in json.dumps(records, default=str)


@pytest.mark.parametrize("failure", ["chained", "unexpected"])
def test_summary_marks_a_failed_rollout_precheck_unavailable(
    client, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    builder_id = uuid4()
    client._submit(_seed_golden_builder(builder_id))
    _freeze_usage(monkeypatch)
    _live_stub(monkeypatch)

    async def precheck(db: object, builder_id: UUID) -> None:
        if failure == "chained":
            # How the precheck reports a failed query: the cause is chained.
            raise BillingCheckoutUnavailable(
                "billing_checkout_unavailable"
            ) from ConnectionResetError("connection reset by peer 10.0.0.7")
        raise RuntimeError("unexpected precheck detail")

    monkeypatch.setattr(
        read_contract_module, "require_billing_checkout_rollout", precheck
    )
    before = _unavailable_count()
    with structlog.testing.capture_logs() as records:
        response = client.get("/v1/billing/summary", headers=_headers(builder_id))
    _assert_marked_unavailable(response)
    assert _unavailable_count() == before + 1
    # The chained database error is reported by its own class, never as the
    # pre-check's wrapper, and never with its message.
    events = [
        record
        for record in records
        if record["event"] == "billing summary checkout availability unavailable"
    ]
    assert events == [
        {
            "event": "billing summary checkout availability unavailable",
            "error_type": (
                "ConnectionResetError" if failure == "chained" else "RuntimeError"
            ),
            "log_level": "warning",
        }
    ]
    serialized = json.dumps(records, default=str)
    assert "connection reset by peer" not in serialized
    assert "unexpected precheck detail" not in serialized


def test_summary_closed_rollout_is_available_and_empty_not_a_failure(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id = uuid4()
    client._submit(_seed_golden_builder(builder_id))
    session_id = uuid4()

    async def live_read(db: object, *, builder_id: UUID) -> object:
        return LiveCheckoutReadback(
            checkout_session_id=session_id, kind="topup", state="active"
        )

    async def closed(db: object, builder_id: UUID) -> None:
        raise BillingCheckoutUnavailable("billing_checkout_unavailable")

    monkeypatch.setattr(billing, "read_live_checkout", live_read)
    monkeypatch.setattr(
        read_contract_module, "require_billing_checkout_rollout", closed
    )
    before = _unavailable_count()
    response = client.get("/v1/billing/summary", headers=_headers(builder_id))
    assert response.status_code == 200
    assert response.json()["checkout"] == {
        "available_offer_ids": [],
        "live_session": {
            "checkout_session_id": str(session_id),
            "kind": "topup",
            "state": "active",
        },
        "available": True,
    }
    assert _unavailable_count() == before


def test_checkout_summary_model_requires_the_availability_marker() -> None:
    with pytest.raises(ValidationError, match="available\n  Field required"):
        BillingCheckoutSummaryResponse.model_validate(
            {"available_offer_ids": [], "live_session": None}
        )


# -- POST /checkout 202 --------------------------------------------------------


def _service_raising(error: Exception) -> type:
    class Service:
        def __init__(self, db: object, **_kwargs: object) -> None:
            del db

        async def create_checkout(self, **kwargs: object) -> CheckoutView:
            del kwargs
            raise error

    return Service


def test_post_checkout_202_adds_only_the_stored_session_id(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id = uuid4()
    session_id = uuid4()
    monkeypatch.setattr(
        billing,
        "BillingCheckoutService",
        _service_raising(
            CheckoutReconciliationRequired(checkout_session_id=session_id)
        ),
    )

    response = client.post(
        "/v1/billing/checkout",
        headers=_headers(builder_id),
        json={"offer_id": "plan_plus_monthly_v1", "method": "credit_card"},
    )

    assert response.status_code == 202
    assert response.content == (
        b'{"detail":"billing_checkout_reconciliation_required",'
        b'"checkout_session_id":"' + str(session_id).encode() + b'"}'
    )


def test_post_checkout_202_without_a_reservation_keeps_the_legacy_body(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        billing,
        "BillingCheckoutService",
        _service_raising(CheckoutReconciliationRequired()),
    )
    response = client.post(
        "/v1/billing/checkout",
        headers=_headers(uuid4()),
        json={"offer_id": "plan_plus_monthly_v1", "method": "credit_card"},
    )
    assert response.status_code == 202
    assert response.content == b'{"detail":"billing_checkout_reconciliation_required"}'


def test_post_checkout_other_statuses_are_unchanged(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        billing, "BillingCheckoutService", _service_raising(CheckoutNotEligible())
    )
    response = client.post(
        "/v1/billing/checkout",
        headers=_headers(uuid4()),
        json={"offer_id": "plan_plus_monthly_v1", "method": "credit_card"},
    )
    assert response.status_code == 409
    assert response.content == b'{"detail":"billing_checkout_not_eligible"}'


def test_reconciliation_required_repr_never_carries_the_session_id() -> None:
    session_id = uuid4()
    error = CheckoutReconciliationRequired(checkout_session_id=session_id)
    assert error.checkout_session_id == session_id
    assert str(session_id) not in repr(error)
    assert str(error) == "billing_checkout_reconciliation_required"


def test_reconciliation_response_model_is_closed() -> None:
    with pytest.raises(
        ValidationError, match="Input should be 'billing_checkout_reconciliation_req"
    ):
        CheckoutReconciliationResponse.model_validate(
            {"detail": "billing_checkout_unavailable", "checkout_session_id": "x"}
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CheckoutReconciliationResponse.model_validate(
            {
                "detail": "billing_checkout_reconciliation_required",
                "checkout_session_id": "x",
                "checkout_url": "https://x",
            }
        )


# -- OpenAPI ---------------------------------------------------------------------


def test_openapi_documents_the_read_contract(client) -> None:
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    components = schema["components"]["schemas"]

    catalog = paths["/v1/billing/catalog"]["get"]
    assert catalog["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/BillingCatalogResponse"
    }
    assert set(components["BillingCatalogOfferResponse"]["properties"]) == set(
        _EXPECTED_CATALOG["offers"][0]  # type: ignore[index]
    )
    assert components["BillingCatalogOfferResponse"]["additionalProperties"] is False

    summary = components["BillingSummaryResponse"]["properties"]
    assert list(summary)[-3:] == [
        "subscription",
        "checkout",
        "topup_commercial_enabled",
    ]
    assert set(components["BillingSubscriptionSummaryResponse"]["properties"]) == {
        "offer_id",
        "catalog_version_id",
        "status",
        "amount_cents",
        "current_period_end",
    }
    assert set(components["BillingCheckoutSummaryResponse"]["properties"]) == {
        "available_offer_ids",
        "live_session",
        "available",
    }

    post = paths["/v1/billing/checkout"]["post"]["responses"]
    assert post["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CheckoutResponse"
    }
    assert post["202"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CheckoutReconciliationResponse"
    }
    assert set(components["CheckoutReconciliationResponse"]["properties"]) == {
        "detail",
        "checkout_session_id",
    }


# -- canary predicate (pure) -------------------------------------------------------


def _control(**values: object) -> BillingRolloutControl:
    control = BillingRolloutControl(environment="test")
    for key, value in {
        "state": "canary_allowlist",
        "canary_builder_id": None,
        "canary_subscription_checkout_id": None,
        "canary_topup_checkout_id": None,
        **values,
    }.items():
        setattr(control, key, value)
    return control


def test_canary_predicate_admits_only_the_canary_builder_offers_and_free_slots() -> (
    None
):
    canary = uuid4()
    admits = BillingRepository.canary_reservation_admits
    open_control = _control(canary_builder_id=canary)

    assert [
        offer_id
        for offer_id in BILLING_CATALOG
        if admits(open_control, builder_id=canary, offer_id=offer_id)
    ] == ["plan_plus_monthly_v1", "package_credits_500_v1"]
    assert not any(
        admits(open_control, builder_id=uuid4(), offer_id=offer_id)
        for offer_id in BILLING_CATALOG
    )
    consumed = _control(
        canary_builder_id=canary,
        canary_subscription_checkout_id=uuid4(),
        canary_topup_checkout_id=uuid4(),
    )
    assert not any(
        admits(consumed, builder_id=canary, offer_id=offer_id)
        for offer_id in BILLING_CATALOG
    )
    for state in ("disabled", "general"):
        assert not any(
            admits(
                _control(state=state, canary_builder_id=canary),
                builder_id=canary,
                offer_id=offer_id,
            )
            for offer_id in BILLING_CATALOG
        )
