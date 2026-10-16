"""Fail-closed authentication and roster tests for the legacy webhook."""

from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from pydantic import SecretStr
from starlette.requests import Request

from src.api.routes import billing, health


def _request(
    body: bytes,
    signature: str = "",
    body_reads: list[bool] | None = None,
) -> Request:
    headers = []
    if signature:
        headers.append((b"x-webhook-signature", signature.encode("ascii")))

    async def receive() -> dict[str, object]:
        if body_reads is not None:
            body_reads.append(True)
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/billing/webhook",
            "headers": headers,
        },
        receive,
    )


def _payload(
    builder_id: str | None = None,
    *,
    plan: str = "plus",
    cycle: str = "monthly",
    coupon_code: str | None = None,
) -> bytes:
    metadata = {
        "builder_id": builder_id or str(uuid4()),
        "plan": plan,
        "cycle": cycle,
    }
    if coupon_code is not None:
        metadata["coupon_code"] = coupon_code
    return json.dumps(
        {
            "event": "billing.paid",
            "data": {
                "id": "pay_fixture_1",
                "subscription_id": "sub_legacy_fixture_1",
                "amount": 99_900,
                "method": "CREDIT_CARD",
                "metadata": metadata,
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _signature(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_missing_secret_rejects_before_reading_body_or_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        billing.settings,
        "abacatepay_webhook_secret",
        SecretStr(""),
    )
    body_reads: list[bool] = []
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), add=MagicMock())

    with pytest.raises(HTTPException) as exc_info:
        await billing.abacatepay_webhook(
            _request(_payload(), body_reads=body_reads),
            db,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "billing_webhook_unavailable"
    assert body_reads == []
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_unsigned_delivery_never_mutates_financial_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        billing.settings,
        "abacatepay_webhook_secret",
        SecretStr("legacy-fixture-secret"),
    )
    body_reads: list[bool] = []
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), add=MagicMock())

    with pytest.raises(HTTPException) as exc_info:
        await billing.abacatepay_webhook(
            _request(_payload(), body_reads=body_reads), db
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid signature"
    assert body_reads == []
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_valid_signature_with_no_roster_row_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "legacy-fixture-secret"
    body = _payload()
    monkeypatch.setattr(
        billing.settings,
        "abacatepay_webhook_secret",
        SecretStr(secret),
    )
    mappings = SimpleNamespace(
        one_or_none=MagicMock(
            return_value={
                "billing_roster_revision": 1,
                "builder_id": None,
                "plan": None,
                "billing_cycle": None,
                "coupon_code": None,
                "subscription_roster_revision": None,
            }
        )
    )
    query_result = SimpleNamespace(mappings=MagicMock(return_value=mappings))
    db = SimpleNamespace(
        execute=AsyncMock(return_value=query_result),
        commit=AsyncMock(),
        add=MagicMock(),
    )

    result = await billing.abacatepay_webhook(
        _request(body, _signature(body, secret)),
        db,
    )

    assert result == {"ok": True, "ignored": True}
    assert db.execute.await_count == 2
    db.commit.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_uninitialized_roster_is_retryable_without_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "legacy-fixture-secret"
    body = _payload()
    monkeypatch.setattr(
        billing.settings,
        "abacatepay_webhook_secret",
        SecretStr(secret),
    )
    mappings = SimpleNamespace(
        one_or_none=MagicMock(
            return_value={
                "billing_roster_revision": 0,
                "builder_id": None,
                "plan": None,
                "billing_cycle": None,
                "coupon_code": None,
                "subscription_roster_revision": None,
            }
        )
    )
    query_result = SimpleNamespace(mappings=MagicMock(return_value=mappings))
    db = SimpleNamespace(
        execute=AsyncMock(return_value=query_result),
        commit=AsyncMock(),
        add=MagicMock(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await billing.abacatepay_webhook(
            _request(body, _signature(body, secret)),
            db,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "billing_webhook_unavailable"
    db.commit.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_roster_query_failure_is_retryable_without_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "legacy-fixture-secret"
    body = _payload()
    monkeypatch.setattr(
        billing.settings,
        "abacatepay_webhook_secret",
        SecretStr(secret),
    )
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=RuntimeError("sensitive database failure")),
        commit=AsyncMock(),
        add=MagicMock(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await billing.abacatepay_webhook(
            _request(body, _signature(body, secret)),
            db,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "billing_webhook_unavailable"
    assert "sensitive" not in str(exc_info.value)
    db.execute.assert_awaited_once()
    db.commit.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_roster_row_is_retryable_without_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "legacy-fixture-secret"
    body = _payload()
    monkeypatch.setattr(
        billing.settings,
        "abacatepay_webhook_secret",
        SecretStr(secret),
    )
    mappings = SimpleNamespace(
        one_or_none=MagicMock(
            return_value={
                "billing_roster_revision": 1,
                "builder_id": "not-a-uuid",
                "plan": "plus",
                "billing_cycle": "monthly",
                "coupon_code": None,
                "subscription_roster_revision": 1,
            }
        )
    )
    query_result = SimpleNamespace(mappings=MagicMock(return_value=mappings))
    db = SimpleNamespace(
        execute=AsyncMock(return_value=query_result),
        commit=AsyncMock(),
        add=MagicMock(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await billing.abacatepay_webhook(
            _request(body, _signature(body, secret)),
            db,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "billing_webhook_unavailable"
    assert "not-a-uuid" not in str(exc_info.value)
    assert db.execute.await_count == 2
    db.commit.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_json_rejects_before_roster_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "legacy-fixture-secret"
    body = b"not-json"
    monkeypatch.setattr(
        billing.settings,
        "abacatepay_webhook_secret",
        SecretStr(secret),
    )
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), add=MagicMock())

    with pytest.raises(HTTPException) as exc_info:
        await billing.abacatepay_webhook(
            _request(body, _signature(body, secret)),
            db,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid JSON"
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_rostered_signed_delivery_preserves_legacy_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "legacy-fixture-secret"
    builder_id = uuid4()
    body = _payload(str(builder_id))
    builder = SimpleNamespace(
        name="Legacy Builder",
        subscription_plan="start",
        subscription_status="inactive",
        billing_cycle="monthly",
    )

    class _BuilderResult:
        @staticmethod
        def scalar_one_or_none() -> SimpleNamespace:
            return builder

    monkeypatch.setattr(
        billing.settings,
        "abacatepay_webhook_secret",
        SecretStr(secret),
    )
    monkeypatch.setattr(
        billing,
        "_get_abacatepay_roster_binding",
        AsyncMock(
            return_value=billing.AbacatePayRosterBinding(
                builder_id=builder_id,
                plan="plus",
                billing_cycle="monthly",
                coupon_code=None,
            )
        ),
    )
    db = SimpleNamespace(
        execute=AsyncMock(return_value=_BuilderResult()),
        commit=AsyncMock(),
        add=MagicMock(),
    )

    result = await billing.abacatepay_webhook(
        _request(body, _signature(body, secret)),
        db,
    )

    assert result == {"ok": True}
    assert builder.subscription_plan == "plus"
    assert builder.subscription_status == "active"
    db.add.assert_called_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata_builder", "plan", "cycle", "coupon_code"),
    [
        (str(uuid4()), "pro", "annual", "PROVIDER_COUPON"),
        (None, "pro", "monthly", None),
        (None, "plus", "annual", None),
        (None, "plus", "monthly", "PROVIDER_COUPON"),
    ],
)
async def test_rostered_delivery_with_divergent_metadata_has_no_effects(
    monkeypatch: pytest.MonkeyPatch,
    metadata_builder: str | None,
    plan: str,
    cycle: str,
    coupon_code: str | None,
) -> None:
    secret = "legacy-fixture-secret"
    canonical_builder_id = uuid4()
    provider_builder_id = metadata_builder or str(canonical_builder_id)
    body = _payload(
        provider_builder_id,
        plan=plan,
        cycle=cycle,
        coupon_code=coupon_code,
    )
    monkeypatch.setattr(
        billing.settings,
        "abacatepay_webhook_secret",
        SecretStr(secret),
    )
    monkeypatch.setattr(
        billing,
        "_get_abacatepay_roster_binding",
        AsyncMock(
            return_value=billing.AbacatePayRosterBinding(
                builder_id=canonical_builder_id,
                plan="plus",
                billing_cycle="monthly",
                coupon_code=None,
            )
        ),
    )
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), add=MagicMock())

    result = await billing.abacatepay_webhook(
        _request(body, _signature(body, secret)),
        db,
    )

    assert result == {"ok": True, "ignored": True}
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_roster_binding_uses_group_1_column_contract() -> None:
    builder_id = uuid4()

    class _Mappings:
        @staticmethod
        def one_or_none() -> dict[str, object]:
            return {
                "billing_roster_revision": 7,
                "builder_id": builder_id,
                "plan": "plus",
                "billing_cycle": "monthly",
                "coupon_code": "LEGACY10",
                "subscription_roster_revision": 6,
            }

    result = SimpleNamespace(mappings=lambda: _Mappings())
    db = SimpleNamespace(execute=AsyncMock(side_effect=[SimpleNamespace(), result]))

    binding = await billing._get_abacatepay_roster_binding("sub_legacy_fixture_1", db)

    assert binding == billing.AbacatePayRosterBinding(
        builder_id=builder_id,
        plan="plus",
        billing_cycle="monthly",
        coupon_code="LEGACY10",
    )
    assert db.execute.await_count == 2
    lock_call, select_call = db.execute.await_args_list
    lock_statement = str(lock_call.args[0])
    statement = str(select_call.args[0])
    assert "billing_roster_revision" in statement
    assert "LEFT JOIN billing_subscriptions" in statement
    assert "pg_advisory_xact_lock" in lock_statement
    assert "roster.environment" not in statement
    assert lock_call.args[1] == {
        "roster_lock_key": billing.billing_roster_advisory_key(),
    }
    assert select_call.args[1] == {
        "external_subscription_id": "sub_legacy_fixture_1",
    }


@pytest.mark.asyncio
async def test_production_readiness_fails_when_legacy_secret_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(health.settings, "environment", "production")
    monkeypatch.setattr(
        health.settings,
        "abacatepay_webhook_secret",
        SecretStr(""),
    )
    response = Response()

    result = await health.readiness_check(response)

    assert response.status_code == 503
    assert result.ready is False
    assert result.checks["abacatepay_webhook"] is False


@pytest.mark.asyncio
async def test_production_readiness_fails_when_legacy_secret_is_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(health.settings, "environment", "production")
    monkeypatch.setattr(
        health.settings,
        "abacatepay_webhook_secret",
        SecretStr(" \t "),
    )
    response = Response()

    result = await health.readiness_check(response)

    assert response.status_code == 503
    assert result.ready is False
    assert result.checks["abacatepay_webhook"] is False


def _live_production_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reachable(*_args: object) -> bool:
        return True

    for probe in ("_database_ready", "_redis_ready", "_qdrant_ready"):
        monkeypatch.setattr(health, probe, reachable)


@pytest.mark.asyncio
async def test_production_readiness_accepts_injected_legacy_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A production readiness pass now also needs the Group 5 billing
    # observability evidence and reachable dependencies; provide a complete,
    # validated production configuration instead of flipping one field.
    from tests.config.test_asaas_settings import _production_settings

    monkeypatch.setattr(
        health,
        "settings",
        _production_settings(abacatepay_webhook_secret="legacy-fixture-secret"),
    )
    _live_production_dependencies(monkeypatch)
    response = Response()

    result = await health.readiness_check(response)

    assert response.status_code == 200
    assert result.ready is True
    assert result.checks["abacatepay_webhook"] is True
    assert "legacy-fixture-secret" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", ["_database_ready", "_redis_ready", "_qdrant_ready"])
async def test_production_readiness_still_requires_live_dependencies(
    monkeypatch: pytest.MonkeyPatch, probe: str
) -> None:
    from tests.config.test_asaas_settings import _production_settings

    monkeypatch.setattr(
        health,
        "settings",
        _production_settings(abacatepay_webhook_secret="legacy-fixture-secret"),
    )
    _live_production_dependencies(monkeypatch)

    async def unreachable(*_args: object) -> bool:
        return False

    monkeypatch.setattr(health, probe, unreachable)
    response = Response()

    result = await health.readiness_check(response)

    assert response.status_code == 503
    assert result.ready is False
    failed = {name for name, ok in result.checks.items() if not ok}
    assert failed == {probe.removeprefix("_").removesuffix("_ready")}


@pytest.mark.asyncio
async def test_production_readiness_rejects_missing_runtime_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.config.test_asaas_settings import _production_settings

    configured = _production_settings(abacatepay_webhook_secret="legacy-fixture-secret")
    monkeypatch.setattr(
        health,
        "settings",
        configured.model_copy(update={"billing_observability_acknowledgement": ""}),
    )
    _live_production_dependencies(monkeypatch)
    response = Response()

    result = await health.readiness_check(response)

    assert response.status_code == 503
    assert {name for name, ok in result.checks.items() if not ok} == {
        "billing_observability"
    }
