"""Structured business-hours persistence through the generic config API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from src.api.middleware.auth import TenantContext
from src.api.routes.config import patch_config
from src.api.schemas.config import ConfigPatchRequest
from src.config.settings import settings
from src.services.config_service import (
    ALL_BUCKETS,
    ARRAY_BUCKETS,
    FIELD_TO_BUCKET,
    flatten_config,
    unflatten_patch,
)

PATH = "/v1/config"


def _headers() -> dict[str, str]:
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(uuid4()),
            "role": "clinic_admin",
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.mark.parametrize(
    "payload",
    [
        # close <= open
        {"business_hours": {"mon": {"open": "19:00", "close": "09:00"}}},
        # bad hour (25:00)
        {"business_hours": {"mon": {"open": "25:00", "close": "18:00"}}},
        # bad day key
        {"business_hours": {"funday": {"open": "09:00", "close": "18:00"}}},
    ],
)
def test_patch_rejects_invalid_business_hours(
    client: TestClient, payload: dict[str, Any]
) -> None:
    response = client.patch(PATH, headers=_headers(), json=payload)

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


def test_valid_business_hours_persist_and_round_trip(client: TestClient) -> None:
    headers = _headers()
    payload = {
        "business_hours": {
            "MON": {"open": "09:00", "close": "18:00"},
            "tue": {"open": "09:00", "close": "18:00"},
            "wed": {"open": "09:00", "close": "18:00"},
            "thu": {"open": "09:00", "close": "18:00"},
            "fri": {"open": "09:00", "close": "18:00"},
            "sat": {"open": "09:00", "close": "13:00"},
        }
    }
    # Day keys are normalized to lowercase by the ClinicOperations validator.
    expected = {
        "mon": {"open": "09:00", "close": "18:00"},
        "tue": {"open": "09:00", "close": "18:00"},
        "wed": {"open": "09:00", "close": "18:00"},
        "thu": {"open": "09:00", "close": "18:00"},
        "fri": {"open": "09:00", "close": "18:00"},
        "sat": {"open": "09:00", "close": "13:00"},
    }

    patched = client.patch(PATH, headers=headers, json=payload)
    assert patched.status_code == 200, patched.text
    assert patched.json()["business_hours"] == expected

    loaded = client.get(PATH, headers=headers)
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["business_hours"] == expected


def test_operations_revalidation_preserves_existing_free_text_hours(
    client: TestClient,
) -> None:
    """Setting ``business_hours`` must leave the sibling operations fields
    that this wish deliberately does not touch (``working_hours``,
    ``scheduling_hours``, ``service_format``) exactly where they were."""
    headers = _headers()
    initial = client.patch(
        PATH,
        headers=headers,
        json={
            "working_hours": "Seg-Sex 08h-18h",
            "scheduling_hours": "Seg-Sex 09h-17h",
            "service_format": "presencial e online",
        },
    )
    assert initial.status_code == 200, initial.text

    updated = client.patch(
        PATH,
        headers=headers,
        json={
            "business_hours": {
                "mon": {"open": "08:00", "close": "18:00"},
                "tue": {"open": "08:00", "close": "18:00"},
                "wed": {"open": "08:00", "close": "18:00"},
                "thu": {"open": "08:00", "close": "18:00"},
                "fri": {"open": "08:00", "close": "18:00"},
            }
        },
    )

    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["working_hours"] == "Seg-Sex 08h-18h"
    assert body["scheduling_hours"] == "Seg-Sex 09h-17h"
    assert body["service_format"] == "presencial e online"


def test_business_hours_routes_to_operations_and_round_trips_through_flatten() -> None:
    payload = {
        "business_hours": {
            "mon": {"open": "09:00", "close": "17:00"},
        }
    }
    assert FIELD_TO_BUCKET["business_hours"] == "operations"
    assert unflatten_patch(payload) == {"operations": payload}

    row = SimpleNamespace(
        clinic_id=None,
        version=1,
        **{
            bucket: (
                []
                if bucket in ARRAY_BUCKETS
                else payload if bucket == "operations" else {}
            )
            for bucket in ALL_BUCKETS
        },
    )
    flat = flatten_config(row)
    assert flat["business_hours"] == payload["business_hours"]


def test_patch_with_only_legacy_business_hours_keys_is_silent_no_op(
    client: TestClient,
) -> None:
    """Legacy flat keys are no longer in FIELD_TO_BUCKET; they must be
    silently dropped by ``unflatten_patch`` and never revalidate the bucket."""
    headers = _headers()

    initial = client.patch(
        PATH,
        headers=headers,
        json={"working_hours": "Seg-Sex 08h-18h"},
    )
    assert initial.status_code == 200, initial.text

    legacy_only = client.patch(
        PATH,
        headers=headers,
        json={
            "business_hours_enabled": True,
            "business_hours_open": "09:00",
            "business_hours_close": "17:00",
            "business_hours_days": ["mon", "tue"],
        },
    )
    assert legacy_only.status_code == 200, legacy_only.text
    body = legacy_only.json()
    # None of the legacy keys land in any bucket.
    for key in (
        "business_hours_enabled",
        "business_hours_open",
        "business_hours_close",
        "business_hours_days",
    ):
        assert key not in body or body.get(key) in (None, False, [], "")
    # Working hours are untouched.
    assert body["working_hours"] == "Seg-Sex 08h-18h"
    # No structured schedule was created either.
    assert body.get("business_hours") in (None, {}, "")


def test_patch_business_hours_replaces_whole_schedule(client: TestClient) -> None:
    """The bucket merge is a shallow ``{**existing, **new}``; ``business_hours``
    is a single field, so a subsequent PATCH replaces the entire map instead
    of per-day merging (config.py:369 semantics)."""
    headers = _headers()

    first = client.patch(
        PATH,
        headers=headers,
        json={
            "business_hours": {
                "mon": {"open": "09:00", "close": "18:00"},
                "tue": {"open": "09:00", "close": "18:00"},
            }
        },
    )
    assert first.status_code == 200, first.text
    assert set(first.json()["business_hours"]) == {"mon", "tue"}

    second = client.patch(
        PATH,
        headers=headers,
        json={
            "business_hours": {
                "wed": {"open": "10:00", "close": "16:00"},
            }
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["business_hours"] == {
        "wed": {"open": "10:00", "close": "16:00"},
    }


@pytest.mark.asyncio
async def test_operations_validation_warns_when_legacy_key_is_dropped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    tenant_id = uuid4()
    row = SimpleNamespace(
        clinic_id=None,
        version=1,
        operations={"legacy_operation_key": "discard me"},
        **{
            bucket: ([] if bucket in ARRAY_BUCKETS else {})
            for bucket in ALL_BUCKETS
            if bucket != "operations"
        },
    )

    class Session:
        async def flush(self) -> None:
            pass

        async def commit(self) -> None:
            pass

    async def get_row(*args: Any, **kwargs: Any) -> Any:
        return row

    async def keep_tenant(*args: Any, **kwargs: Any) -> TenantContext:
        return TenantContext(tenant_id=tenant_id, role="clinic_admin", user_id=None)

    async def no_cache(*args: Any, **kwargs: Any) -> None:
        pass

    monkeypatch.setattr("src.api.routes.config._get_or_create_config", get_row)
    monkeypatch.setattr("src.api.routes.config._resolve_config_tenant", keep_tenant)
    monkeypatch.setattr(
        "src.services.event_service.invalidate_clinic_config_cache", no_cache
    )

    with caplog.at_level("WARNING", logger="src.api.routes.config"):
        await patch_config(
            request=SimpleNamespace(query_params={}),
            body=ConfigPatchRequest(timezone="America/Sao_Paulo"),
            tenant=TenantContext(
                tenant_id=tenant_id, role="clinic_admin", user_id=None
            ),
            session=Session(),  # type: ignore[arg-type]
        )

    assert "legacy_operation_key" not in row.operations
    assert "legacy_operation_key" in caplog.text
    assert "operations" in caplog.text
