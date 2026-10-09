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
from src.api.routes.config import _validate_handoff_policy_patch, patch_config
from src.api.schemas.config import ConfigPatchRequest, ConfigResponse
from src.config.settings import settings
from src.models.clinic_config import ClinicHandoff
from src.services.config_service import (
    ALL_BUCKETS,
    ARRAY_BUCKETS,
    FIELD_TO_BUCKET,
    flatten_config,
    unflatten_patch,
)

PATH = "/v1/config"


def _headers(
    role: str | None = "clinic_admin",
    *,
    tenant_id: Any | None = None,
) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(uuid4()),
        "tenant_id": str(tenant_id or uuid4()),
        "iat": now,
        "exp": now + timedelta(hours=1),
    }
    if role is not None:
        payload["role"] = role
    token = jwt.encode(
        payload,
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


@pytest.mark.parametrize(
    "value",
    [0, 121, True, 1.0, "10", None],
    ids=["zero", "too-high", "boolean", "float", "string", "null"],
)
def test_patch_rejects_invalid_handoff_service_time_without_writing(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    value: object,
) -> None:
    calls: list[str] = []

    async def unexpected_load(*args: Any, **kwargs: Any) -> Any:
        calls.append("load")
        raise AssertionError("invalid SLA target must fail before loading config")

    async def unexpected_cache(*args: Any, **kwargs: Any) -> None:
        calls.append("cache")

    monkeypatch.setattr("src.api.routes.config._load_config", unexpected_load)
    monkeypatch.setattr(
        "src.services.event_service.invalidate_clinic_config_cache",
        unexpected_cache,
    )

    response = client.patch(
        PATH,
        headers=_headers(),
        json={"meta_minutos_atendimento": value},
    )

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    assert calls == []


@pytest.mark.parametrize("value", [1, 47, 120])
def test_patch_persists_valid_handoff_service_time(
    client: TestClient,
    value: int,
) -> None:
    headers = _headers()

    patched = client.patch(
        PATH,
        headers=headers,
        json={"meta_minutos_atendimento": value},
    )
    loaded = client.get(PATH, headers=headers)

    assert patched.status_code == 200, patched.text
    assert patched.json()["meta_minutos_atendimento"] == value
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["meta_minutos_atendimento"] == value


@pytest.mark.parametrize(
    "role",
    ["builder", "super_admin", "investor", None, "future_role"],
    ids=["builder", "super-admin", "investor", "missing", "unknown"],
)
def test_non_clinic_jwt_cannot_read_or_write_handoff_service_time(
    client: TestClient,
    role: str | None,
) -> None:
    tenant_id = uuid4()
    clinic_headers = _headers(tenant_id=tenant_id)
    restricted_headers = _headers(role, tenant_id=tenant_id)

    saved = client.patch(
        PATH,
        headers=clinic_headers,
        json={"meta_minutos_atendimento": 37},
    )
    assert saved.status_code == 200, saved.text

    loaded = client.get(PATH, headers=restricted_headers)
    rejected = client.patch(
        PATH,
        headers=restricted_headers,
        json={"meta_minutos_atendimento": 38},
    )

    assert loaded.status_code == 200, loaded.text
    assert "meta_minutos_atendimento" not in loaded.json()
    assert rejected.status_code == 403
    assert rejected.json() == {
        "detail": {
            "error": "clinic_admin_only_field",
            "field": "meta_minutos_atendimento",
        }
    }


def test_api_key_cannot_read_or_write_handoff_service_time(
    client: TestClient,
) -> None:
    tenant_id = "a0000000-0000-0000-0000-000000000001"
    saved = client.patch(
        PATH,
        headers=_headers(tenant_id=tenant_id),
        json={"meta_minutos_atendimento": 41},
    )
    assert saved.status_code == 200, saved.text

    api_key_headers = {"X-API-Key": "test-api-key"}
    loaded = client.get(PATH, headers=api_key_headers)
    rejected = client.patch(
        PATH,
        headers=api_key_headers,
        json={"meta_minutos_atendimento": 42},
    )

    assert loaded.status_code == 200, loaded.text
    assert "meta_minutos_atendimento" not in loaded.json()
    assert rejected.status_code == 403
    assert rejected.json() == {
        "detail": {
            "error": "clinic_admin_only_field",
            "field": "meta_minutos_atendimento",
        }
    }


def test_valid_restricted_field_rejection_precedes_every_downstream_boundary(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def unexpected_credentials(*args: Any, **kwargs: Any) -> list[str]:
        calls.append("credentials")
        return ["llm_api_key"]

    async def unexpected_resolution(*args: Any, **kwargs: Any) -> Any:
        calls.append("resolve")
        raise AssertionError("restricted field must fail before tenant resolution")

    async def unexpected_load(*args: Any, **kwargs: Any) -> Any:
        calls.append("load")
        raise AssertionError("restricted field must fail before config load")

    async def unexpected_cache(*args: Any, **kwargs: Any) -> None:
        calls.append("cache")

    monkeypatch.setattr(
        "src.api.routes.config.credential_boundary_fields",
        unexpected_credentials,
    )
    monkeypatch.setattr(
        "src.api.routes.config._resolve_config_tenant",
        unexpected_resolution,
    )
    monkeypatch.setattr("src.api.routes.config._load_config", unexpected_load)
    monkeypatch.setattr(
        "src.services.event_service.invalidate_clinic_config_cache",
        unexpected_cache,
    )

    response = client.patch(
        f"{PATH}?tenant_id={uuid4()}",
        headers=_headers("builder"),
        json={"meta_minutos_atendimento": 15, "llm_api_key": "secret"},
    )

    assert response.status_code == 403
    assert response.json() == {
        "detail": {
            "error": "clinic_admin_only_field",
            "field": "meta_minutos_atendimento",
        }
    }
    assert calls == []


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(_headers("builder"), id="builder"),
        pytest.param(_headers("super_admin"), id="super-admin"),
        pytest.param(_headers("investor"), id="investor"),
        pytest.param(_headers(None), id="missing-role"),
        pytest.param(_headers("future_role"), id="unknown-role"),
        pytest.param({"X-API-Key": "test-api-key"}, id="api-key"),
    ],
)
def test_non_clinic_identities_can_save_other_config_fields(
    client: TestClient,
    headers: dict[str, str],
) -> None:
    response = client.patch(PATH, headers=headers, json={"clinic_name": "Allowed"})

    assert response.status_code == 200, response.text
    assert response.json()["clinic_name"] == "Allowed"
    assert "meta_minutos_atendimento" not in response.json()


@pytest.mark.parametrize(
    "role", ["builder", "super_admin", "investor", None, "future_role"]
)
def test_invalid_restricted_field_keeps_fastapi_422_for_non_clinic_jwt(
    client: TestClient,
    role: str | None,
) -> None:
    response = client.patch(
        PATH,
        headers=_headers(role),
        json={"meta_minutos_atendimento": 0},
    )

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


@pytest.mark.asyncio
async def test_patch_omitting_service_time_preserves_legacy_handoff_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    legacy_handoff = {
        "meta_minutos_atendimento": "legacy-invalid",
        "legacy_handoff_key": {"nested": True},
    }
    row = SimpleNamespace(
        clinic_id=None,
        version=1,
        handoff=legacy_handoff,
        **{
            bucket: ([] if bucket in ARRAY_BUCKETS else {})
            for bucket in ALL_BUCKETS
            if bucket != "handoff"
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
        "src.services.event_service.invalidate_clinic_config_cache",
        no_cache,
    )

    await patch_config(
        request=SimpleNamespace(query_params={}),
        body=ConfigPatchRequest(clinic_name="Nova clínica"),
        tenant=TenantContext(tenant_id=tenant_id, role="clinic_admin", user_id=None),
        session=Session(),  # type: ignore[arg-type]
    )

    assert row.handoff is legacy_handoff
    assert row.handoff == {
        "meta_minutos_atendimento": "legacy-invalid",
        "legacy_handoff_key": {"nested": True},
    }


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


def test_handoff_policy_round_trip_canonicalizes_legacy_days_and_preserves_keys(
    client: TestClient,
) -> None:
    headers = _headers()
    initial = client.patch(
        PATH,
        headers=headers,
        json={
            "trigger_message": "Mensagem preservada",
            "reactivation_delay_hours": 3,
            "clinic_name": "Clínica preservada",
        },
    )
    assert initial.status_code == 200, initial.text

    patched = client.patch(
        PATH,
        headers=headers,
        json={
            "handoff_active": "yes",
            "standby_mode": "active",
            "handoff_period": "cycle",
            "handoff_cycle_hours": None,
            "handoff_cycle_days": "12",
        },
    )

    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["handoff_cycle_hours"] is None
    assert body["handoff_cycle_days"] == 12
    assert body["trigger_message"] == "Mensagem preservada"
    assert body["reactivation_delay_hours"] == 3
    assert body["effective_trigger_message"] == "Mensagem preservada"
    assert body["effective_reactivation_delay_hours"] == 3
    assert body["clinic_name"] == "Clínica preservada"
    loaded = client.get(PATH, headers=headers)
    assert loaded.json()["handoff_cycle_days"] == 12
    assert loaded.json()["effective_trigger_message"] == "Mensagem preservada"
    assert loaded.json()["effective_reactivation_delay_hours"] == 3


def test_config_response_exposes_safe_effective_reactivation_fallbacks(
    client: TestClient,
) -> None:
    response = client.patch(
        PATH,
        headers=_headers(),
        json={
            "trigger_message": {"invalid": "legacy shape"},
            "reactivation_delay_hours": 3.0,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["trigger_message"] == {"invalid": "legacy shape"}
    assert body["reactivation_delay_hours"] == 3.0
    assert body["effective_trigger_message"] == (
        "Precisando de algo mais, estamos a disposição."
    )
    assert body["effective_reactivation_delay_hours"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {
            "handoff_active": "yes",
            "standby_mode": "inactive",
            "handoff_period": "cycle",
            "handoff_cycle_hours": None,
            "handoff_cycle_days": None,
        },
        {
            "handoff_active": "yes",
            "standby_mode": "active",
            "handoff_period": "permanent",
            "handoff_cycle_hours": 2,
            "handoff_cycle_days": None,
        },
        {
            "handoff_active": "yes",
            "standby_mode": "active",
            "handoff_period": "cycle",
            "handoff_cycle_hours": None,
            "handoff_cycle_days": None,
        },
        {
            "handoff_active": "yes",
            "standby_mode": "active",
            "handoff_period": "cycle",
            "handoff_cycle_hours": 2,
            "handoff_cycle_days": 3,
        },
        {"handoff_cycle_hours": "2"},
        {"handoff_cycle_hours": True},
        {"handoff_cycle_hours": 1.5},
        {"handoff_cycle_hours": 721},
        {"handoff_cycle_days": " 2"},
        {"handoff_cycle_days": "+2"},
        {"handoff_cycle_days": "2.0"},
        {"handoff_cycle_days": "１２"},
        {"handoff_cycle_days": True},
        {"handoff_cycle_days": 1.5},
        {"handoff_cycle_days": 366},
        {"handoff_active": None},
        {"standby_mode": "unknown"},
    ],
)
def test_handoff_policy_rejects_invalid_type_range_and_combinations(
    client: TestClient,
    payload: dict[str, Any],
) -> None:
    response = client.patch(PATH, headers=_headers(), json=payload)

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("handoff_active", None, "null_not_allowed"),
        ("standby_mode", "invalid", "literal_error"),
        ("handoff_cycle_hours", "2", "int_type"),
        ("handoff_cycle_hours", 721, "less_than_equal"),
    ],
)
def test_policy_request_validation_uses_secret_safe_stable_envelope(
    client: TestClient,
    field: str,
    value: Any,
    expected_code: str,
) -> None:
    secret = "SECRET-MUST-NOT-ECHO"
    response = client.patch(
        PATH,
        headers=_headers(),
        json={field: value, "llm_api_key": secret},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "error": "invalid_handoff_policy",
            "violations": [{"field": field, "code": expected_code}],
        }
    }
    assert secret not in response.text
    for forbidden_key in ("input", "msg", "ctx", "body"):
        assert forbidden_key not in response.text


def test_credential_boundary_remains_authoritative_for_valid_policy_payload(
    client: TestClient,
) -> None:
    secret = "SECRET-MUST-NOT-ECHO"
    response = client.patch(
        PATH,
        headers=_headers(),
        json={
            "handoff_active": "yes",
            "standby_mode": "active",
            "handoff_period": "permanent",
            "handoff_cycle_hours": None,
            "handoff_cycle_days": None,
            "llm_api_key": secret,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error": "credential_endpoint_required",
        "fields": ["llm_api_key"],
    }
    assert secret not in response.text


def test_handoff_policy_partial_patch_validates_post_merge(client: TestClient) -> None:
    headers = _headers()
    seeded = client.patch(
        PATH,
        headers=headers,
        json={
            "handoff_active": "yes",
            "standby_mode": "active",
            "handoff_period": "cycle",
            "handoff_cycle_hours": None,
            "handoff_cycle_days": 4,
        },
    )
    assert seeded.status_code == 200, seeded.text

    conflict = client.patch(PATH, headers=headers, json={"handoff_cycle_hours": 6})
    assert conflict.status_code == 422

    repaired = client.patch(
        PATH,
        headers=headers,
        json={"handoff_cycle_hours": 6, "handoff_cycle_days": None},
    )
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["handoff_cycle_hours"] == 6
    assert repaired.json()["handoff_cycle_days"] is None


def test_legacy_days_with_leading_zeroes_persist_as_bounded_integer(
    client: TestClient,
) -> None:
    response = client.patch(
        PATH,
        headers=_headers(),
        json={
            "handoff_active": "yes",
            "standby_mode": "active",
            "handoff_period": "cycle",
            "handoff_cycle_hours": None,
            "handoff_cycle_days": "000365",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["handoff_cycle_days"] == 365


def test_legacy_days_with_5000_digits_is_rejected_without_echo(
    client: TestClient,
) -> None:
    raw = "9" * 5000
    response = client.patch(
        PATH,
        headers=_headers(),
        json={
            "handoff_active": "yes",
            "standby_mode": "active",
            "handoff_period": "cycle",
            "handoff_cycle_hours": None,
            "handoff_cycle_days": raw,
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "error": "invalid_handoff_policy",
            "violations": [{"field": "handoff_cycle_days", "code": "int_type"}],
        }
    }
    assert raw not in response.text


@pytest.mark.asyncio
async def test_rejected_handoff_policy_is_atomic_before_row_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    calls: list[str] = []
    original_handoff = {
        "handoff_active": "yes",
        "standby_mode": "active",
        "handoff_period": "permanent",
        "handoff_cycle_hours": None,
        "handoff_cycle_days": None,
        "trigger_message": "keep",
    }
    row = SimpleNamespace(
        clinic_id=None,
        version=9,
        handoff=dict(original_handoff),
        **{
            bucket: ([] if bucket in ARRAY_BUCKETS else {})
            for bucket in ALL_BUCKETS
            if bucket != "handoff"
        },
    )

    class Session:
        def add(self, value: Any) -> None:
            calls.append("add")

        async def flush(self) -> None:
            calls.append("flush")

        async def commit(self) -> None:
            calls.append("commit")

    async def get_row(*args: Any, **kwargs: Any) -> Any:
        return row

    async def keep_tenant(*args: Any, **kwargs: Any) -> TenantContext:
        return TenantContext(tenant_id=tenant_id, role="clinic_admin", user_id=None)

    async def no_cache(*args: Any, **kwargs: Any) -> None:
        calls.append("cache")

    monkeypatch.setattr("src.api.routes.config._load_config", get_row)
    monkeypatch.setattr("src.api.routes.config._resolve_config_tenant", keep_tenant)
    monkeypatch.setattr(
        "src.services.event_service.invalidate_clinic_config_cache", no_cache
    )

    with pytest.raises(HTTPException) as exc_info:
        await patch_config(
            request=SimpleNamespace(query_params={}),
            body=ConfigPatchRequest(
                handoff_period="cycle",
                handoff_cycle_hours=2,
                handoff_cycle_days=3,
            ),
            tenant=TenantContext(
                tenant_id=tenant_id, role="clinic_admin", user_id=None
            ),
            session=Session(),  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 422
    assert row.handoff == original_handoff
    assert row.version == 9
    assert calls == []


@pytest.mark.asyncio
async def test_unrelated_patch_preserves_malformed_handoff_policy_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    malformed = {
        "handoff_active": "YES-ish",
        "standby_mode": ["bad"],
        "handoff_period": "cycle",
        "handoff_cycle_hours": "6",
        "handoff_cycle_days": True,
        "trigger_message": "old",
    }
    row = SimpleNamespace(
        clinic_id=None,
        version=4,
        handoff=dict(malformed),
        **{
            bucket: ([] if bucket in ARRAY_BUCKETS else {})
            for bucket in ALL_BUCKETS
            if bucket != "handoff"
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

    await patch_config(
        request=SimpleNamespace(query_params={}),
        body=ConfigPatchRequest(trigger_message="new"),
        tenant=TenantContext(tenant_id=tenant_id, role="clinic_admin", user_id=None),
        session=Session(),  # type: ignore[arg-type]
    )

    assert row.handoff == {**malformed, "trigger_message": "new"}


def test_handoff_openapi_contract_documents_canonical_fields_without_nullable_enums() -> (
    None
):
    patch_schema = ConfigPatchRequest.model_json_schema()["properties"]
    response_schema = ConfigResponse.model_json_schema()["properties"]

    for field in ("handoff_active", "standby_mode", "handoff_period"):
        assert patch_schema[field]["type"] == "string"
        assert "anyOf" not in patch_schema[field]
        assert "default" not in patch_schema[field]
        assert field in response_schema
    assert patch_schema["handoff_cycle_hours"]["anyOf"][0] == {
        "maximum": 720,
        "minimum": 1,
        "type": "integer",
    }
    assert patch_schema["handoff_cycle_days"]["anyOf"][0] == {
        "maximum": 365,
        "minimum": 1,
        "type": "integer",
    }
    assert ConfigPatchRequest().model_dump(exclude_unset=True) == {}


def test_openapi_documents_clinic_admin_only_sla_without_tenant_examples(
    client: TestClient,
) -> None:
    document = client.get("/openapi.json").json()
    patch_operation = document["paths"][PATH]["patch"]
    description = patch_operation["description"]
    schema = ConfigPatchRequest.model_json_schema()["properties"][
        "meta_minutos_atendimento"
    ]

    assert "human JWT" in description
    assert "exactly `clinic_admin`" in description
    assert "API keys remain accepted for other allowed" in description
    assert schema["type"] == "integer"
    assert schema["minimum"] == 1
    assert schema["maximum"] == 120
    assert "examples" not in schema
    assert "example" not in schema


@pytest.mark.asyncio
async def test_unrelated_handoff_patch_handles_non_object_historical_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    row = SimpleNamespace(
        clinic_id=None,
        version=4,
        handoff="historical-non-object",
        **{
            bucket: ([] if bucket in ARRAY_BUCKETS else {})
            for bucket in ALL_BUCKETS
            if bucket != "handoff"
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
        body=ConfigPatchRequest(trigger_message="new"),
        tenant=TenantContext(tenant_id=tenant_id, role="clinic_admin", user_id=None),
        session=Session(),  # type: ignore[arg-type]
    )

    assert row.handoff == {"trigger_message": "new"}
    assert result.trigger_message == "new"  # type: ignore[attr-defined]


def test_complete_policy_patch_repairs_non_object_historical_bucket() -> None:
    row = SimpleNamespace(handoff=["historical", "shape"])
    patch = {
        "handoff_active": "yes",
        "standby_mode": "active",
        "handoff_period": "cycle",
        "handoff_cycle_hours": 8,
        "handoff_cycle_days": None,
    }

    assert _validate_handoff_policy_patch(patch, row) == patch

    with pytest.raises(HTTPException) as exc_info:
        _validate_handoff_policy_patch({"handoff_period": "permanent"}, row)
    assert exc_info.value.detail["error"] == "invalid_handoff_policy"
    assert all(
        violation["code"] == "repair_required"
        for violation in exc_info.value.detail["violations"]
    )


def test_typed_clinic_handoff_round_trips_policy_and_unknown_keys() -> None:
    raw = {
        "handoff_active": "yes",
        "standby_mode": "active",
        "handoff_period": "cycle",
        "handoff_cycle_hours": 10,
        "handoff_cycle_days": None,
        "trigger_message": "preserve me",
    }

    dumped = ClinicHandoff.model_validate(raw).model_dump()
    for key, value in raw.items():
        assert dumped[key] == value


@pytest.mark.asyncio
async def test_invalid_first_handoff_write_does_not_add_flush_commit_or_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    calls: list[str] = []

    class Session:
        def add(self, value: Any) -> None:
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
                handoff_period="cycle",
                handoff_cycle_hours=None,
                handoff_cycle_days=None,
            ),
            tenant=TenantContext(
                tenant_id=tenant_id, role="clinic_admin", user_id=None
            ),
            session=Session(),  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["violations"] == [
        {
            "field": "handoff_policy",
            "code": "cycle_requires_exactly_one_duration",
        }
    ]
    assert calls == []
