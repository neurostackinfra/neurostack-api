"""Structured business-hours persistence through the generic config API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import jwt
import pytest
from fastapi import HTTPException
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


def test_services_modalities_round_trip_with_extra_fields(client: TestClient) -> None:
    headers = _headers()
    services = [
        {
            "name": "Consulta por vídeo",
            "service_format": "online",
            "frontend_metadata": {"color": "blue"},
        },
        "Retorno",
    ]

    patched = client.patch(
        PATH,
        headers=headers,
        json={"service_format": "both", "services": services},
    )

    assert patched.status_code == 200, patched.text
    assert patched.json()["services"] == services
    loaded = client.get(PATH, headers=headers)
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["services"] == services


def test_services_null_flattens_to_empty_list_over_http(client: TestClient) -> None:
    headers = _headers()
    catalog = [{"name": "Consulta", "service_format": "online"}, "Retorno"]

    initial = client.patch(PATH, headers=headers, json={"services": catalog})
    assert initial.status_code == 200, initial.text
    assert initial.json()["services"] == catalog

    cleared = client.patch(PATH, headers=headers, json={"services": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["services"] == []

    loaded = client.get(PATH, headers=headers)
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["services"] == []


def test_services_empty_list_replaces_populated_catalog_over_http(
    client: TestClient,
) -> None:
    headers = _headers()
    catalog = [{"name": "Consulta", "service_format": "online"}, "Retorno"]

    initial = client.patch(PATH, headers=headers, json={"services": catalog})
    assert initial.status_code == 200, initial.text
    assert initial.json()["services"] == catalog

    replaced = client.patch(PATH, headers=headers, json={"services": []})
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["services"] == []

    loaded = client.get(PATH, headers=headers)
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["services"] == []


@pytest.mark.parametrize(
    ("clinic_service_format", "service_format", "status_code"),
    [
        ("online", "both", 422),
        ("presential", "both", 422),
        ("both", "both", 200),
        ("presencial e online", "both", 200),
    ],
)
def test_service_both_compatibility_matrix(
    client: TestClient,
    clinic_service_format: str,
    service_format: str,
    status_code: int,
) -> None:
    response = client.patch(
        PATH,
        headers=_headers(),
        json={
            "service_format": clinic_service_format,
            "services": [{"name": "Atendimento", "service_format": service_format}],
        },
    )

    assert response.status_code == status_code, response.text
    if status_code == 422:
        assert response.json()["detail"]["conflicts"] == [
            {"index": 0, "service_format": "both"}
        ]


def test_service_format_null_round_trips_over_http(client: TestClient) -> None:
    headers = _headers()
    patched = client.patch(PATH, headers=headers, json={"service_format": None})

    assert patched.status_code == 200, patched.text
    assert patched.json()["service_format"] is None
    loaded = client.get(PATH, headers=headers)
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["service_format"] is None


def test_services_patch_model_preserves_omitted_null_and_legacy_items() -> None:
    omitted = ConfigPatchRequest(
        services=[{"name": "Consulta", "frontend_key": "kept"}, "Retorno"]
    ).model_dump(exclude_unset=True)
    explicit_null = ConfigPatchRequest(
        services=[{"name": "Consulta", "service_format": None}]
    ).model_dump(exclude_unset=True)

    assert omitted == {
        "services": [{"name": "Consulta", "frontend_key": "kept"}, "Retorno"]
    }
    assert explicit_null == {"services": [{"name": "Consulta", "service_format": None}]}
    assert ConfigPatchRequest(services=None).model_dump(exclude_unset=True) == {
        "services": None
    }


def test_invalid_token_does_not_create_or_invalidate_config_cache_on_first_write(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def new_config(*args: Any, **kwargs: Any) -> Any:
        calls.append("create")
        raise AssertionError("an invalid token must not create config")

    async def invalidate(*args: Any, **kwargs: Any) -> None:
        calls.append("cache")

    monkeypatch.setattr("src.api.routes.config._new_config", new_config)
    monkeypatch.setattr(
        "src.services.event_service.invalidate_clinic_config_cache", invalidate
    )

    response = client.patch(
        PATH,
        headers={"Authorization": "Bearer invalid-token"},
        json={"service_format": "online", "services": []},
    )

    assert response.status_code == 401
    assert calls == []


def test_invalid_service_modality_uses_standard_pydantic_422(
    client: TestClient,
) -> None:
    response = client.patch(
        PATH,
        headers=_headers(),
        json={"services": [{"name": "Consulta", "service_format": "hybrid"}]},
    )

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


@pytest.mark.parametrize(
    ("clinic_service_format", "service_format", "allowed"),
    [
        ("online", "presential", ["online"]),
        ("presential", "online", ["presential"]),
    ],
)
def test_service_modality_conflict_has_stable_contract(
    client: TestClient,
    clinic_service_format: str,
    service_format: str,
    allowed: list[str],
) -> None:
    response = client.patch(
        PATH,
        headers=_headers(),
        json={
            "service_format": clinic_service_format,
            "services": [
                {"name": "Compatível", "service_format": clinic_service_format},
                {"name": "Incompatível", "service_format": service_format},
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error": "service_format_conflict",
        "message": "One or more service modalities are incompatible with the clinic service format.",
        "clinic_service_format": clinic_service_format,
        "allowed_service_formats": allowed,
        "conflicts": [{"index": 1, "service_format": service_format}],
        "action": "Change the listed service modalities or broaden the clinic service format.",
    }


def test_single_field_modality_patches_validate_against_persisted_counterpart(
    client: TestClient,
) -> None:
    headers = _headers()
    initial = client.patch(
        PATH,
        headers=headers,
        json={
            "service_format": "online",
            "services": [{"name": "Vídeo", "service_format": "online"}],
        },
    )
    assert initial.status_code == 200, initial.text

    services_only = client.patch(
        PATH,
        headers=headers,
        json={"services": [{"name": "Consultório", "service_format": "presential"}]},
    )
    assert services_only.status_code == 422
    assert services_only.json()["detail"]["conflicts"] == [
        {"index": 0, "service_format": "presential"}
    ]

    global_only = client.patch(
        PATH,
        headers=headers,
        json={"service_format": "presential"},
    )
    assert global_only.status_code == 422
    assert global_only.json()["detail"]["conflicts"] == [
        {"index": 0, "service_format": "online"}
    ]


@pytest.mark.asyncio
async def test_conflicting_first_write_is_atomic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    calls: list[str] = []

    class Session:
        def add(self, row: Any) -> None:
            calls.append("add")

        async def flush(self) -> None:
            calls.append("flush")

        async def commit(self) -> None:
            calls.append("commit")

    async def no_row(*args: Any, **kwargs: Any) -> None:
        return None

    async def keep_tenant(*args: Any, **kwargs: Any) -> TenantContext:
        return TenantContext(tenant_id=tenant_id, role="clinic_admin", user_id=None)

    async def no_cache(*args: Any, **kwargs: Any) -> None:
        calls.append("cache")

    monkeypatch.setattr("src.api.routes.config._load_config", no_row)
    monkeypatch.setattr("src.api.routes.config._resolve_config_tenant", keep_tenant)
    monkeypatch.setattr(
        "src.services.event_service.invalidate_clinic_config_cache", no_cache
    )

    with pytest.raises(HTTPException) as exc_info:
        await patch_config(
            request=SimpleNamespace(query_params={}),
            body=ConfigPatchRequest(
                service_format="online",
                services=[{"name": "Consultório", "service_format": "presential"}],
            ),
            tenant=TenantContext(
                tenant_id=tenant_id, role="clinic_admin", user_id=None
            ),
            session=Session(),  # type: ignore[arg-type]
        )

    assert getattr(exc_info.value, "status_code", None) == 422
    assert calls == []


@pytest.mark.asyncio
async def test_services_null_stays_nullable_while_response_flattens_to_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    row = SimpleNamespace(
        clinic_id=None,
        version=1,
        services=[{"name": "Vídeo", "service_format": "online"}],
        **{
            bucket: ({"service_format": "online"} if bucket == "operations" else {})
            for bucket in ALL_BUCKETS
            if bucket not in ARRAY_BUCKETS and bucket != "operations"
        },
    )
    row.operations = {"service_format": "online"}

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

    monkeypatch.setattr("src.api.routes.config._load_config", get_row)
    monkeypatch.setattr("src.api.routes.config._resolve_config_tenant", keep_tenant)
    monkeypatch.setattr(
        "src.services.event_service.invalidate_clinic_config_cache", no_cache
    )

    cleared = await patch_config(
        request=SimpleNamespace(query_params={}),
        body=ConfigPatchRequest(services=None),
        tenant=TenantContext(tenant_id=tenant_id, role="clinic_admin", user_id=None),
        session=Session(),  # type: ignore[arg-type]
    )
    assert row.services is None
    assert cleared.services == []

    emptied = await patch_config(
        request=SimpleNamespace(query_params={}),
        body=ConfigPatchRequest(services=[]),
        tenant=TenantContext(tenant_id=tenant_id, role="clinic_admin", user_id=None),
        session=Session(),  # type: ignore[arg-type]
    )
    assert row.services == []
    assert emptied.services == []


@pytest.mark.asyncio
async def test_unrelated_patch_does_not_validate_or_normalize_historical_modalities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    services = [{"name": "Legado", "service_format": "presential"}]
    row = SimpleNamespace(
        clinic_id=None,
        version=1,
        services=services,
        operations={"service_format": "online"},
        **{
            bucket: ([] if bucket in ARRAY_BUCKETS else {})
            for bucket in ALL_BUCKETS
            if bucket not in {"services", "operations"}
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

    monkeypatch.setattr("src.api.routes.config._load_config", get_row)
    monkeypatch.setattr("src.api.routes.config._resolve_config_tenant", keep_tenant)
    monkeypatch.setattr(
        "src.services.event_service.invalidate_clinic_config_cache", no_cache
    )

    result = await patch_config(
        request=SimpleNamespace(query_params={}),
        body=ConfigPatchRequest(clinic_name="Nome novo"),
        tenant=TenantContext(tenant_id=tenant_id, role="clinic_admin", user_id=None),
        session=Session(),  # type: ignore[arg-type]
    )

    assert result.services == services
    assert row.services == services


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

    monkeypatch.setattr("src.api.routes.config._load_config", get_row)
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
