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
        {"business_hours_open": "25:00"},
        {"business_hours_close": "9h"},
        {"business_hours_days": ["funday"]},
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
        "business_hours_enabled": True,
        "business_hours_open": "09:00",
        "business_hours_close": "18:00",
        "business_hours_days": ["MON", "tue", "fri"],
    }

    patched = client.patch(PATH, headers=headers, json=payload)
    assert patched.status_code == 200, patched.text
    assert patched.json()["business_hours_days"] == ["mon", "tue", "fri"]

    loaded = client.get(PATH, headers=headers)
    assert loaded.status_code == 200, loaded.text
    for field, expected in {
        **payload,
        "business_hours_days": ["mon", "tue", "fri"],
    }.items():
        assert loaded.json()[field] == expected


def test_operations_revalidation_preserves_existing_free_text_hours(
    client: TestClient,
) -> None:
    headers = _headers()
    initial = client.patch(
        PATH,
        headers=headers,
        json={
            "working_hours": "Seg-Sex 08h-18h",
            "scheduling_hours": "Seg-Sex 09h-17h",
        },
    )
    assert initial.status_code == 200, initial.text

    updated = client.patch(
        PATH,
        headers=headers,
        json={
            "business_hours_enabled": True,
            "business_hours_open": "08:00",
            "business_hours_close": "18:00",
            "business_hours_days": ["mon", "tue", "wed", "thu", "fri"],
        },
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["working_hours"] == "Seg-Sex 08h-18h"
    assert updated.json()["scheduling_hours"] == "Seg-Sex 09h-17h"


def test_business_hour_fields_route_to_operations_and_flatten() -> None:
    payload = {
        "business_hours_enabled": True,
        "business_hours_open": "09:00",
        "business_hours_close": "17:00",
        "business_hours_days": ["mon"],
    }
    assert all(FIELD_TO_BUCKET[field] == "operations" for field in payload)
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
    assert all(flat[field] == value for field, value in payload.items())


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
            body=ConfigPatchRequest(business_hours_open="09:00"),
            tenant=TenantContext(
                tenant_id=tenant_id, role="clinic_admin", user_id=None
            ),
            session=Session(),  # type: ignore[arg-type]
        )

    assert "legacy_operation_key" not in row.operations
    assert "legacy_operation_key" in caplog.text
    assert "operations" in caplog.text
