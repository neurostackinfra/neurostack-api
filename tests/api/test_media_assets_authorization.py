"""Authorization ordering and caller/target matrix for Media Assets."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from starlette.requests import Request

from src.api.middleware.auth import TenantContext
from src.api.middleware.media_limits import MediaConcurrencyLimiters
from src.api.routes import media_assets as route
from src.api.schemas.media_assets import MediaAssetPatchRequest
from src.models.tenant import Tenant


def _request() -> Request:
    app = SimpleNamespace(
        state=SimpleNamespace(
            media_concurrency=MediaConcurrencyLimiters(uploads=4, downloads=8)
        )
    )
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/v1/media-assets",
            "raw_path": b"/v1/media-assets",
            "query_string": b"",
            "headers": [],
            "client": ("test", 1),
            "server": ("test", 80),
            "app": app,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["list", "download", "patch", "delete"])
async def test_each_crud_endpoint_resolves_then_classifies_before_media_access(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    tenant_id = uuid4()
    media_id = uuid4()
    order: list[str] = []
    target = Tenant(
        id=tenant_id,
        name="Clinic",
        api_key_hash="h",
        is_active=True,
        plan_type="clinic",
        config={},
    )

    async def resolve(*args: Any, **kwargs: Any) -> UUID:
        order.append("resolve")
        return tenant_id

    class Result:
        def __init__(self, classification: bool) -> None:
            self.classification = classification

        def scalar_one_or_none(self) -> Tenant | None:
            return target if self.classification else None

        def all(self) -> list[Any]:
            return []

        def one_or_none(self) -> None:
            return None

    class Session:
        async def execute(self, statement: Any) -> Result:
            rendered = str(statement).lower()
            if "media_assets" in rendered:
                order.append("media")
                assert order == ["resolve", "classify", "media"]
                return Result(False)
            order.append("classify")
            assert order == ["resolve", "classify"]
            assert "tenants.is_active is true" in rendered
            assert "tenants.plan_type !=" in rendered
            return Result(True)

        async def rollback(self) -> None:
            return None

    monkeypatch.setattr(route, "resolve_effective_tenant", resolve)
    session = Session()
    tenant = TenantContext(tenant_id, api_key="key")
    try:
        if operation == "list":
            await route.list_media_assets(
                request=_request(),
                tenant_id=None,
                cursor=None,
                page_size=25,
                tenant=tenant,
                session=session,  # type: ignore[arg-type]
            )
        elif operation == "download":
            await route.download_media_asset(
                media_id=media_id,
                request=_request(),
                tenant_id=None,
                tenant=tenant,
                session=session,  # type: ignore[arg-type]
            )
        elif operation == "patch":
            await route.patch_media_asset(
                media_id=media_id,
                patch=MediaAssetPatchRequest(display_name="updated"),
                request=_request(),
                tenant_id=None,
                tenant=tenant,
                session=session,  # type: ignore[arg-type]
            )
        else:
            await route.delete_media_asset(
                media_id=media_id,
                request=_request(),
                tenant_id=None,
                tenant=tenant,
                session=session,  # type: ignore[arg-type]
            )
    except HTTPException as error:
        assert error.status_code == 404

    assert order == ["resolve", "classify", "media"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["list", "download", "patch", "delete"])
async def test_invalid_target_is_forbidden_before_any_media_query(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    tenant_id = uuid4()
    media_id = uuid4()
    statements: list[str] = []

    async def resolve(*args: Any, **kwargs: Any) -> UUID:
        return tenant_id

    class Result:
        def scalar_one_or_none(self) -> None:
            return None

    class Session:
        async def execute(self, statement: Any) -> Result:
            rendered = str(statement).lower()
            statements.append(rendered)
            assert "media_assets" not in rendered
            return Result()

        async def rollback(self) -> None:
            return None

    monkeypatch.setattr(route, "resolve_effective_tenant", resolve)
    session = Session()
    tenant = TenantContext(tenant_id)

    with pytest.raises(HTTPException) as caught:
        if operation == "list":
            await route.list_media_assets(
                request=_request(),
                tenant_id=None,
                cursor=None,
                page_size=25,
                tenant=tenant,
                session=session,  # type: ignore[arg-type]
            )
        elif operation == "download":
            await route.download_media_asset(
                media_id=media_id,
                request=_request(),
                tenant_id=None,
                tenant=tenant,
                session=session,  # type: ignore[arg-type]
            )
        elif operation == "patch":
            await route.patch_media_asset(
                media_id=media_id,
                patch=MediaAssetPatchRequest(display_name="updated"),
                request=_request(),
                tenant_id=None,
                tenant=tenant,
                session=session,  # type: ignore[arg-type]
            )
        else:
            await route.delete_media_asset(
                media_id=media_id,
                request=_request(),
                tenant_id=None,
                tenant=tenant,
                session=session,  # type: ignore[arg-type]
            )

    assert caught.value.status_code == 403
    assert getattr(caught.value, "detail") == {"code": "media_tenant_forbidden"}
    assert len(statements) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "allowed"),
    [
        ("userless_clone", True),
        ("clinic_role_user", True),
        ("admin_only", False),
        ("builder_only", False),
        ("investor_only", False),
        ("inactive", False),
        ("admin_plan", False),
    ],
)
async def test_target_classification_matrix_is_applied_after_resolution(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    allowed: bool,
) -> None:
    tenant_id = uuid4()
    order: list[str] = []
    target = Tenant(
        id=tenant_id,
        name=case,
        api_key_hash="h",
        is_active=case != "inactive",
        plan_type="admin" if case == "admin_plan" else "clinic",
        config={},
    )

    async def resolve(*args: Any, **kwargs: Any) -> UUID:
        order.append("resolve")
        return tenant_id

    class Result:
        def scalar_one_or_none(self) -> Tenant | None:
            return target if allowed else None

    class Session:
        async def execute(self, statement: Any) -> Result:
            order.append("classify")
            rendered = str(statement).lower()
            assert "media_assets" not in rendered
            assert "tenants.is_active is true" in rendered
            assert "tenants.plan_type !=" in rendered
            assert "cast(users.role as varchar) not in" in rendered
            return Result()

    monkeypatch.setattr(route, "resolve_effective_tenant", resolve)
    if allowed:
        result = await route._authorized_target(
            _request(),
            TenantContext(tenant_id, api_key="key"),
            None,
            Session(),  # type: ignore[arg-type]
        )
        assert result.id == tenant_id
    else:
        with pytest.raises(HTTPException) as caught:
            await route._authorized_target(
                _request(),
                TenantContext(tenant_id, api_key="key"),
                None,
                Session(),  # type: ignore[arg-type]
            )
        assert caught.value.status_code == 403
    assert order == ["resolve", "classify"]


@pytest.mark.asyncio
async def test_unowned_builder_override_is_denied_by_resolver_before_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller_tenant_id = uuid4()
    requested_tenant_id = uuid4()
    builder_id = uuid4()
    statements: list[str] = []

    class Result:
        def scalar_one_or_none(self) -> None:
            return None

    class Session:
        async def execute(self, statement: Any) -> Result:
            rendered = str(statement).lower()
            statements.append(rendered)
            assert "media_assets" not in rendered
            return Result()

        async def rollback(self) -> None:
            return None

    with pytest.raises(HTTPException) as caught:
        await route.list_media_assets(
            request=_request(),
            tenant_id=requested_tenant_id,
            cursor=None,
            page_size=25,
            tenant=TenantContext(
                caller_tenant_id,
                role="builder",
                builder_id=builder_id,
            ),
            session=Session(),  # type: ignore[arg-type]
        )

    assert caught.value.status_code == 403
    assert getattr(caught.value, "detail") == {"code": "tenant_override_forbidden"}
    assert len(statements) == 1


@pytest.mark.asyncio
async def test_builder_own_tenant_requires_claim_and_ownership_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    builder_id = uuid4()
    statements: list[str] = []
    target = Tenant(
        id=tenant_id,
        name="Mixed own clinic",
        api_key_hash="h",
        is_active=True,
        plan_type="clinic",
        builder_id=builder_id,
        config={},
    )

    async def resolve(*args: Any, **kwargs: Any) -> UUID:
        return tenant_id

    class Result:
        def scalar_one_or_none(self) -> Tenant:
            return target

    class Session:
        async def execute(self, statement: Any) -> Result:
            rendered = str(statement).lower()
            statements.append(rendered)
            return Result()

    monkeypatch.setattr(route, "resolve_effective_tenant", resolve)

    with pytest.raises(HTTPException) as missing_claim:
        await route._authorized_target(
            _request(),
            TenantContext(tenant_id, role="builder", builder_id=None),
            None,
            Session(),  # type: ignore[arg-type]
        )
    assert missing_claim.value.status_code == 403
    assert statements == []

    authorized = await route._authorized_target(
        _request(),
        TenantContext(tenant_id, role="builder", builder_id=builder_id),
        None,
        Session(),  # type: ignore[arg-type]
    )
    assert authorized.id == tenant_id
    assert "tenants.builder_id =" in statements[0]


def test_all_five_registered_routes_accept_optional_tenant_id() -> None:
    by_path_method: set[tuple[str, str]] = set()
    for registered in route.router.routes:
        if not isinstance(registered, APIRoute):
            continue
        for method in registered.methods or set():
            by_path_method.add((registered.path, method))

    assert by_path_method >= {
        ("", "POST"),
        ("", "GET"),
        ("/{media_id}/content", "GET"),
        ("/{media_id}", "PATCH"),
        ("/{media_id}", "DELETE"),
    }
