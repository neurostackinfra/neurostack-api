"""Tenant authorization contract for the ``/v1/policies`` routes.

Every tenant-scoped policy route resolves exactly one authorized tenant through
the canonical ``resolve_effective_tenant`` policy before it touches the policy
store: API keys and clinic administrators stay inside their own tenant,
builders may only select clinics they own, super administrators may select a
clinic explicitly, and every other JWT role is denied.  The shared global
policy stays visible to every authorized caller, and no policy response is
ever replayed from the shared response cache, so a cached 200 for one tenant
can never answer another tenant's request.
"""

import logging
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from httpx import Response

from neurostack_core.policy import PolicyDefinition, get_global_policy, get_policy_store
from src.api.middleware.performance import response_cache
from src.config.settings import settings

API_KEY_TENANT = UUID("a0000000-0000-0000-0000-000000000001")
MARKER = "segredo-exclusivo-da-clinica"
OWN_POLICY_NAME = "Own clinic policy"
OWN_RULE_NAME = "own_marker_rule"
RENAMED = "Renamed via route matrix"
CREATED = "Created via route matrix"

ROUTES = ("create", "get", "list", "patch", "delete", "effective", "evaluate")
SUCCESS_STATUS = {
    "create": 201,
    "get": 200,
    "list": 200,
    "patch": 200,
    "delete": 204,
    "effective": 200,
    "evaluate": 200,
}
TENANT_OVERRIDE_FORBIDDEN = {"code": "tenant_override_forbidden"}
CALLER_ROLE_FORBIDDEN = {"code": "caller_role_forbidden"}


# ============================================
# Helpers
# ============================================


@pytest.fixture(autouse=True)
def _isolated_response_cache() -> Iterator[None]:
    """Start and finish every test with an empty process-wide response cache."""
    response_cache.invalidate()
    yield
    response_cache.invalidate()


def _jwt_headers(
    role: str,
    tenant_id: UUID,
    *,
    builder_id: UUID | None = None,
    expired: bool = False,
) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(uuid4()),
        "tenant_id": str(tenant_id),
        "role": role,
        "exp": now - timedelta(minutes=5) if expired else now + timedelta(hours=1),
    }
    if builder_id is not None:
        payload["builder_id"] = str(builder_id)
    token = jwt.encode(payload, settings.secret_key, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _clinic_admin(tenant_id: UUID) -> dict[str, str]:
    return _jwt_headers("clinic_admin", tenant_id)


def _create_policy(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    """Create a policy in the caller's own tenant (tenant_id omitted)."""
    response = client.post(
        "/v1/policies",
        headers=headers,
        json={
            "name": OWN_POLICY_NAME,
            "policy_type": "tenant",
            "priority": 10,
            "rules": [
                {
                    "rule": OWN_RULE_NAME,
                    "message": "Clinic-only rule",
                    "severity": "warning",
                    "action": "warn",
                    "patterns": [MARKER],
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def _stored_policy(client: TestClient, policy_id: str) -> PolicyDefinition | None:
    """Read the policy store directly so the response cache cannot mask state."""
    store = get_policy_store()
    return client._submit(  # type: ignore[attr-defined, no-any-return]
        store.get_policy_or_none(UUID(policy_id))
    )


def _stored_tenant_policies(client: TestClient, tenant_id: UUID) -> list[str]:
    store = get_policy_store()
    policies = client._submit(  # type: ignore[attr-defined]
        store.list_policies(tenant_id, active_only=False)
    )
    return sorted(policy.name for policy in policies)


def _assert_untouched(client: TestClient, tenant_id: UUID, policy_id: str) -> None:
    stored = _stored_policy(client, policy_id)
    assert stored is not None, "the victim policy was deleted"
    assert stored.tenant_id == tenant_id
    assert stored.name == OWN_POLICY_NAME
    assert stored.version == 1
    assert _stored_tenant_policies(client, tenant_id) == [OWN_POLICY_NAME]


def _tenant_policy_ids(response: Response) -> set[str]:
    return {
        policy["id"]
        for policy in response.json()["policies"]
        if policy["tenant_id"] is not None
    }


def _all_policy_ids(response: Response) -> set[str]:
    return {policy["id"] for policy in response.json()["policies"]}


def _seed_tenants(
    client: TestClient,
    *,
    tenants: list[tuple[UUID, UUID | None]],
    builder_ids: list[UUID],
) -> None:
    from src.models.builder import Builder
    from src.models.tenant import Tenant
    from src.repositories.database import get_db_session

    async def _run() -> None:
        db = get_db_session()
        try:
            for builder_id in builder_ids:
                db.add(Builder(id=builder_id, name=f"Builder {builder_id}"))
            for tenant_id, builder_id in tenants:
                db.add(
                    Tenant(
                        id=tenant_id,
                        name=f"Clinic {tenant_id}",
                        slug=f"clinic-{tenant_id}",
                        api_key_hash=f"hash-{tenant_id}",
                        builder_id=builder_id,
                        config={},
                    )
                )
            await db.commit()
        finally:
            await db.close()

    client._submit(_run())  # type: ignore[attr-defined]


def _call(
    client: TestClient,
    route: str,
    headers: dict[str, str],
    *,
    tenant_id: UUID,
    policy_id: str,
) -> Response:
    """Issue one policy route call that explicitly targets ``tenant_id``."""
    target = {"tenant_id": str(tenant_id)}
    if route == "create":
        return client.post(
            "/v1/policies",
            headers=headers,
            json={**target, "name": CREATED, "policy_type": "tenant", "rules": []},
        )
    if route == "get":
        return client.get(f"/v1/policies/{policy_id}", headers=headers, params=target)
    if route == "list":
        return client.get("/v1/policies", headers=headers, params=target)
    if route == "patch":
        return client.patch(
            f"/v1/policies/{policy_id}",
            headers=headers,
            params=target,
            json={"name": RENAMED},
        )
    if route == "delete":
        return client.delete(
            f"/v1/policies/{policy_id}", headers=headers, params=target
        )
    if route == "effective":
        return client.get(f"/v1/policies/tenant/{tenant_id}/effective", headers=headers)
    if route == "evaluate":
        return client.post(
            "/v1/policies/evaluate",
            headers=headers,
            params={**target, "session_id": str(uuid4()), "message": MARKER},
        )
    raise AssertionError(f"unknown route {route}")


def _assert_success_effect(
    client: TestClient,
    route: str,
    response: Response,
    *,
    tenant_id: UUID,
    policy_id: str,
) -> None:
    """Prove a successful call acted on ``tenant_id`` with the legacy shape."""
    assert response.status_code == SUCCESS_STATUS[route], response.text
    if route == "create":
        assert response.json()["tenant_id"] == str(tenant_id)
        assert response.json()["name"] == CREATED
        assert _stored_tenant_policies(client, tenant_id) == sorted(
            [CREATED, OWN_POLICY_NAME]
        )
    elif route == "get":
        assert response.json()["id"] == policy_id
        assert response.json()["tenant_id"] == str(tenant_id)
    elif route == "list":
        body = response.json()
        assert _tenant_policy_ids(response) == {policy_id}
        assert str(get_global_policy().id) in _all_policy_ids(response)
        assert (body["total"], body["global_count"], body["tenant_count"]) == (2, 1, 1)
    elif route == "patch":
        assert response.json()["name"] == RENAMED
        stored = _stored_policy(client, policy_id)
        assert stored is not None
        assert (stored.name, stored.version) == (RENAMED, 2)
    elif route == "delete":
        assert response.content == b""
        assert _stored_policy(client, policy_id) is None
    elif route == "effective":
        body = response.json()
        assert body["tenant_id"] == str(tenant_id)
        tenant_rules = [r for r in body["effective_rules"] if r["source"] == "tenant"]
        assert [r["rule"] for r in tenant_rules] == [OWN_RULE_NAME]
        assert policy_id in {p["id"] for p in body["source_policies"]}
    elif route == "evaluate":
        body = response.json()
        assert [v["rule_name"] for v in body["violations"]] == [OWN_RULE_NAME]
        assert body["passed"] is False


# ============================================
# Cross-tenant access (failing first)
# ============================================


class TestCrossTenantPolicyAccess:
    """Tenant B must never read, mutate or plant data in tenant A."""

    @pytest.mark.parametrize("route", ROUTES)
    def test_other_clinic_admin_is_forbidden(
        self, client: TestClient, route: str
    ) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        policy = _create_policy(client, _clinic_admin(tenant_a))

        response = _call(
            client,
            route,
            _clinic_admin(tenant_b),
            tenant_id=tenant_a,
            policy_id=policy["id"],
        )

        assert response.status_code == 403, response.text
        assert response.json()["detail"] == TENANT_OVERRIDE_FORBIDDEN
        assert MARKER not in response.text
        assert policy["id"] not in response.text
        _assert_untouched(client, tenant_a, policy["id"])

    @pytest.mark.parametrize("route", ["get", "patch", "delete"])
    def test_foreign_policy_id_inside_own_scope_is_not_found(
        self, client: TestClient, route: str
    ) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        policy = _create_policy(client, _clinic_admin(tenant_a))

        response = _call(
            client,
            route,
            _clinic_admin(tenant_b),
            tenant_id=tenant_b,
            policy_id=policy["id"],
        )

        assert response.status_code == 404, response.text
        assert response.json() == {"detail": f"Policy {policy['id']} not found"}
        _assert_untouched(client, tenant_a, policy["id"])

    def test_foreign_policy_id_without_tenant_id_is_not_found(
        self, client: TestClient
    ) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        policy = _create_policy(client, _clinic_admin(tenant_a))

        response = client.get(
            f"/v1/policies/{policy['id']}", headers=_clinic_admin(tenant_b)
        )

        assert response.status_code == 404, response.text
        assert MARKER not in response.text

    def test_api_key_cannot_select_another_tenant(
        self, client: TestClient, api_headers: dict[str, str]
    ) -> None:
        tenant_a = uuid4()
        policy = _create_policy(client, _clinic_admin(tenant_a))

        for route in ROUTES:
            response = _call(
                client, route, api_headers, tenant_id=tenant_a, policy_id=policy["id"]
            )
            assert response.status_code == 403, (route, response.text)
            assert response.json()["detail"] == TENANT_OVERRIDE_FORBIDDEN, route
        _assert_untouched(client, tenant_a, policy["id"])


# ============================================
# Own-tenant access
# ============================================


class TestOwnTenantPolicyAccess:
    @pytest.mark.parametrize("route", ROUTES)
    def test_clinic_admin_acts_on_own_tenant(
        self, client: TestClient, route: str
    ) -> None:
        tenant = uuid4()
        headers = _clinic_admin(tenant)
        policy = _create_policy(client, headers)

        response = _call(
            client, route, headers, tenant_id=tenant, policy_id=policy["id"]
        )

        _assert_success_effect(
            client, route, response, tenant_id=tenant, policy_id=policy["id"]
        )

    def test_list_without_tenant_id_defaults_to_own_tenant_and_global(
        self, client: TestClient
    ) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        policy_a = _create_policy(client, _clinic_admin(tenant_a))
        _create_policy(client, _clinic_admin(tenant_b))

        response = client.get("/v1/policies", headers=_clinic_admin(tenant_a))

        assert response.status_code == 200, response.text
        body = response.json()
        assert _tenant_policy_ids(response) == {policy_a["id"]}
        assert str(get_global_policy().id) in _all_policy_ids(response)
        assert (body["total"], body["global_count"], body["tenant_count"]) == (2, 1, 1)

    def test_list_without_global_returns_only_own_tenant(
        self, client: TestClient
    ) -> None:
        tenant = uuid4()
        policy = _create_policy(client, _clinic_admin(tenant))

        response = client.get(
            "/v1/policies",
            params={"include_global": "false"},
            headers=_clinic_admin(tenant),
        )

        body = response.json()
        assert _all_policy_ids(response) == {policy["id"]}
        assert (body["total"], body["global_count"], body["tenant_count"]) == (1, 0, 1)

    def test_get_and_create_without_tenant_id_use_own_tenant(
        self, client: TestClient
    ) -> None:
        tenant = uuid4()
        headers = _clinic_admin(tenant)
        policy = _create_policy(client, headers)

        detail = client.get(f"/v1/policies/{policy['id']}", headers=headers)

        assert policy["tenant_id"] == str(tenant)
        assert detail.status_code == 200, detail.text
        assert detail.json() == policy

    def test_api_key_acts_on_own_tenant(
        self, client: TestClient, api_headers: dict[str, str]
    ) -> None:
        created = client.post(
            "/v1/policies",
            headers=api_headers,
            json={"name": f"api-key-{uuid4()}", "policy_type": "tenant", "rules": []},
        )
        defaulted = client.get("/v1/policies", headers=api_headers)
        explicit = client.get(
            "/v1/policies",
            params={"tenant_id": str(API_KEY_TENANT)},
            headers=api_headers,
        )

        assert created.status_code == 201, created.text
        assert created.json()["tenant_id"] == str(API_KEY_TENANT)
        assert created.json()["id"] in _tenant_policy_ids(defaulted)
        assert created.json()["id"] in _tenant_policy_ids(explicit)


# ============================================
# Shared global policies
# ============================================


class TestGlobalPolicyVisibility:
    def test_global_policy_is_listed_for_every_tenant(self, client: TestClient) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        policy_a = _create_policy(client, _clinic_admin(tenant_a))
        global_id = str(get_global_policy().id)

        listed_a = client.get("/v1/policies", headers=_clinic_admin(tenant_a))
        listed_b = client.get("/v1/policies", headers=_clinic_admin(tenant_b))

        assert global_id in _all_policy_ids(listed_a)
        assert global_id in _all_policy_ids(listed_b)
        assert policy_a["id"] not in _all_policy_ids(listed_b)
        assert listed_b.json()["global_count"] == 1

    def test_global_policy_is_readable_by_id_for_every_tenant(
        self, client: TestClient, api_headers: dict[str, str]
    ) -> None:
        global_id = str(get_global_policy().id)

        for headers in (_clinic_admin(uuid4()), _clinic_admin(uuid4()), api_headers):
            response = client.get(f"/v1/policies/{global_id}", headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["id"] == global_id
            assert response.json()["tenant_id"] is None

    def test_effective_and_evaluate_apply_global_rules_to_every_tenant(
        self, client: TestClient
    ) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        _create_policy(client, _clinic_admin(tenant_a))
        global_rule = get_global_policy().rules[0]
        global_rule_ids = {rule.rule_id for rule in get_global_policy().rules}

        effective_b = client.get(
            f"/v1/policies/tenant/{tenant_b}/effective",
            headers=_clinic_admin(tenant_b),
        )
        evaluated_b = client.post(
            "/v1/policies/evaluate",
            params={
                "tenant_id": str(tenant_b),
                "session_id": str(uuid4()),
                "message": f"{global_rule.patterns[0]} {MARKER}",
            },
            headers=_clinic_admin(tenant_b),
        )

        rules = effective_b.json()["effective_rules"]
        assert {r["rule_id"] for r in rules} == global_rule_ids
        assert all(r["source"] == "global" for r in rules)
        violations = [v["rule_id"] for v in evaluated_b.json()["violations"]]
        assert global_rule.rule_id in violations
        assert OWN_RULE_NAME not in {
            v["rule_name"] for v in evaluated_b.json()["violations"]
        }

    @pytest.mark.parametrize("method", ["patch", "delete"])
    def test_global_policy_remains_immutable(
        self, client: TestClient, method: str
    ) -> None:
        tenant = uuid4()
        global_id = str(get_global_policy().id)
        kwargs: dict[str, Any] = {
            "headers": _clinic_admin(tenant),
            "params": {"tenant_id": str(tenant)},
        }
        if method == "patch":
            kwargs["json"] = {"name": "global hijack"}

        response = client.request(method.upper(), f"/v1/policies/{global_id}", **kwargs)

        assert response.status_code == 403, response.text
        assert get_global_policy().name != "global hijack"


# ============================================
# Privileged tenant selection (canonical convention)
# ============================================


class TestPrivilegedTenantSelection:
    @pytest.mark.parametrize("route", ROUTES)
    def test_super_admin_may_select_any_tenant(
        self, client: TestClient, route: str
    ) -> None:
        clinic, admin_tenant = uuid4(), uuid4()
        policy = _create_policy(client, _clinic_admin(clinic))

        response = _call(
            client,
            route,
            _jwt_headers("super_admin", admin_tenant),
            tenant_id=clinic,
            policy_id=policy["id"],
        )

        _assert_success_effect(
            client, route, response, tenant_id=clinic, policy_id=policy["id"]
        )

    def test_super_admin_without_tenant_id_stays_in_own_tenant(
        self, client: TestClient
    ) -> None:
        clinic, admin_tenant = uuid4(), uuid4()
        policy = _create_policy(client, _clinic_admin(clinic))
        headers = _jwt_headers("super_admin", admin_tenant)

        listed = client.get("/v1/policies", headers=headers)
        detail = client.get(f"/v1/policies/{policy['id']}", headers=headers)

        assert listed.status_code == 200, listed.text
        assert _tenant_policy_ids(listed) == set()
        assert detail.status_code == 404, detail.text

    @pytest.mark.parametrize("route", ROUTES)
    def test_linked_builder_may_act_on_owned_clinic(
        self, client: TestClient, route: str
    ) -> None:
        builder_id, builder_tenant, clinic = uuid4(), uuid4(), uuid4()
        _seed_tenants(client, tenants=[(clinic, builder_id)], builder_ids=[builder_id])
        policy = _create_policy(client, _clinic_admin(clinic))

        response = _call(
            client,
            route,
            _jwt_headers("builder", builder_tenant, builder_id=builder_id),
            tenant_id=clinic,
            policy_id=policy["id"],
        )

        _assert_success_effect(
            client, route, response, tenant_id=clinic, policy_id=policy["id"]
        )

    def test_builder_cannot_act_on_unlinked_clinic(self, client: TestClient) -> None:
        builder_id, other_builder = uuid4(), uuid4()
        foreign_clinic, orphan_clinic = uuid4(), uuid4()
        _seed_tenants(
            client,
            tenants=[(foreign_clinic, other_builder), (orphan_clinic, None)],
            builder_ids=[builder_id, other_builder],
        )
        headers = _jwt_headers("builder", uuid4(), builder_id=builder_id)

        for clinic in (foreign_clinic, orphan_clinic, uuid4()):
            policy = _create_policy(client, _clinic_admin(clinic))
            for route in ROUTES:
                response = _call(
                    client, route, headers, tenant_id=clinic, policy_id=policy["id"]
                )
                assert response.status_code == 403, (route, response.text)
                assert response.json()["detail"] == TENANT_OVERRIDE_FORBIDDEN
            _assert_untouched(client, clinic, policy["id"])

    def test_builder_without_builder_claim_cannot_select_a_clinic(
        self, client: TestClient
    ) -> None:
        builder_id, clinic = uuid4(), uuid4()
        _seed_tenants(client, tenants=[(clinic, builder_id)], builder_ids=[builder_id])
        policy = _create_policy(client, _clinic_admin(clinic))

        response = _call(
            client,
            "list",
            _jwt_headers("builder", uuid4()),
            tenant_id=clinic,
            policy_id=policy["id"],
        )

        assert response.status_code == 403, response.text
        assert response.json()["detail"] == TENANT_OVERRIDE_FORBIDDEN

    @pytest.mark.parametrize("role", ["investor", "unrecognized"])
    def test_non_operational_roles_are_denied_even_for_own_tenant(
        self, client: TestClient, role: str
    ) -> None:
        tenant = uuid4()
        policy = _create_policy(client, _clinic_admin(tenant))
        headers = _jwt_headers(role, tenant)

        for route in ROUTES:
            response = _call(
                client, route, headers, tenant_id=tenant, policy_id=policy["id"]
            )
            assert response.status_code == 403, (route, response.text)
            assert response.json()["detail"] == CALLER_ROLE_FORBIDDEN, route
        defaulted = client.get("/v1/policies", headers=headers)
        global_detail = client.get(
            f"/v1/policies/{get_global_policy().id}", headers=headers
        )
        assert defaulted.status_code == 403, defaulted.text
        assert global_detail.status_code == 403, global_detail.text
        _assert_untouched(client, tenant, policy["id"])


# ============================================
# Authentication boundary
# ============================================


class TestPolicyAuthentication:
    @pytest.mark.parametrize("credential", ["missing", "invalid", "expired"])
    def test_unauthenticated_calls_are_rejected(
        self, client: TestClient, credential: str
    ) -> None:
        tenant = uuid4()
        policy = _create_policy(client, _clinic_admin(tenant))
        headers = {
            "missing": {},
            "invalid": {"Authorization": "Bearer not-a-jwt"},
            "expired": _jwt_headers("super_admin", tenant, expired=True),
        }[credential]

        for route in ROUTES:
            response = _call(
                client, route, headers, tenant_id=tenant, policy_id=policy["id"]
            )
            assert response.status_code == 401, (route, response.text)
            assert MARKER not in response.text
        _assert_untouched(client, tenant, policy["id"])


# ============================================
# Response cache interaction
# ============================================


class TestPolicyResponseCacheIsolation:
    """A cache HIT runs before the route, so policy reads must never be cached."""

    def test_cached_tenant_list_is_never_replayed_to_another_tenant(
        self, client: TestClient
    ) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        policy = _create_policy(client, _clinic_admin(tenant_a))
        params = {"tenant_id": str(tenant_a)}

        warmed = client.get(
            "/v1/policies", params=params, headers=_clinic_admin(tenant_a)
        )
        replayed = client.get(
            "/v1/policies", params=params, headers=_clinic_admin(tenant_b)
        )

        assert policy["id"] in _tenant_policy_ids(warmed)
        assert replayed.status_code == 403, replayed.text
        assert policy["id"] not in replayed.text

    def test_default_list_is_never_replayed_between_tenants(
        self, client: TestClient
    ) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        policy_a = _create_policy(client, _clinic_admin(tenant_a))
        policy_b = _create_policy(client, _clinic_admin(tenant_b))

        listed_a = client.get("/v1/policies", headers=_clinic_admin(tenant_a))
        listed_b = client.get("/v1/policies", headers=_clinic_admin(tenant_b))

        assert _tenant_policy_ids(listed_a) == {policy_a["id"]}
        assert _tenant_policy_ids(listed_b) == {policy_b["id"]}

    def test_spoofed_tenant_header_cannot_select_another_tenants_entry(
        self, client: TestClient
    ) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        policy = _create_policy(client, _clinic_admin(tenant_a))
        spoof = {"X-Tenant-ID": str(tenant_a)}

        client.get("/v1/policies", headers={**_clinic_admin(tenant_a), **spoof})
        client.get(
            "/v1/policies",
            params={"tenant_id": str(tenant_a)},
            headers={**_clinic_admin(tenant_a), **spoof},
        )
        defaulted = client.get(
            "/v1/policies", headers={**_clinic_admin(tenant_b), **spoof}
        )
        explicit = client.get(
            "/v1/policies",
            params={"tenant_id": str(tenant_a)},
            headers={**_clinic_admin(tenant_b), **spoof},
        )

        assert policy["id"] not in defaulted.text
        assert explicit.status_code == 403, explicit.text

    def test_cached_detail_and_effective_are_never_replayed(
        self, client: TestClient
    ) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        policy = _create_policy(client, _clinic_admin(tenant_a))
        detail_path = f"/v1/policies/{policy['id']}"
        effective_path = f"/v1/policies/tenant/{tenant_a}/effective"

        assert (
            client.get(detail_path, headers=_clinic_admin(tenant_a)).status_code == 200
        )
        assert (
            client.get(effective_path, headers=_clinic_admin(tenant_a)).status_code
            == 200
        )
        detail_b = client.get(detail_path, headers=_clinic_admin(tenant_b))
        effective_b = client.get(effective_path, headers=_clinic_admin(tenant_b))

        assert detail_b.status_code == 404, detail_b.text
        assert effective_b.status_code == 403, effective_b.text
        assert MARKER not in detail_b.text + effective_b.text

    def test_reads_reflect_writes_immediately(self, client: TestClient) -> None:
        tenant = uuid4()
        headers = _clinic_admin(tenant)
        policy = _create_policy(client, headers)
        detail_path = f"/v1/policies/{policy['id']}"

        assert client.get(detail_path, headers=headers).json()["version"] == 1
        assert client.get("/v1/policies", headers=headers).json()["tenant_count"] == 1
        client.patch(
            detail_path,
            params={"tenant_id": str(tenant)},
            json={"name": RENAMED},
            headers=headers,
        )
        renamed = client.get(detail_path, headers=headers)
        client.delete(detail_path, params={"tenant_id": str(tenant)}, headers=headers)
        deleted = client.get(detail_path, headers=headers)
        listed = client.get("/v1/policies", headers=headers)

        assert (renamed.json()["name"], renamed.json()["version"]) == (RENAMED, 2)
        assert deleted.status_code == 404, deleted.text
        assert listed.json()["tenant_count"] == 0

    def test_policy_responses_never_enter_the_response_cache(
        self, client: TestClient
    ) -> None:
        tenant = uuid4()
        headers = _clinic_admin(tenant)
        policy = _create_policy(client, headers)
        response_cache.invalidate()

        responses = [
            client.get("/v1/policies", headers=headers),
            client.get(f"/v1/policies/{policy['id']}", headers=headers),
            client.get(f"/v1/policies/tenant/{tenant}/effective", headers=headers),
        ]

        assert [r.status_code for r in responses] == [200, 200, 200]
        assert all("x-cache" not in r.headers for r in responses)
        assert response_cache.size == 0

    @pytest.mark.parametrize("route", ROUTES)
    def test_policy_responses_forbid_downstream_storage(
        self, client: TestClient, route: str
    ) -> None:
        tenant = uuid4()
        headers = _clinic_admin(tenant)
        policy = _create_policy(client, headers)

        response = _call(
            client, route, headers, tenant_id=tenant, policy_id=policy["id"]
        )

        assert response.status_code == SUCCESS_STATUS[route], response.text
        assert response.headers.get("cache-control") == "no-store"


class TestNonPolicyResponseCacheRules:
    """Pin the remaining cacheable entries so the policy exclusion stays local."""

    def test_health_probes_keep_their_cache_rules(self, client: TestClient) -> None:
        observed = [
            (path, client.get(path).headers.get("x-cache"))
            for path in ("/health", "/health", "/live", "/live", "/ready")
        ]

        assert observed == [
            ("/health", "MISS"),
            ("/health", "HIT"),
            ("/live", "MISS"),
            ("/live", "HIT"),
            ("/ready", "MISS"),
        ]


# ============================================
# List filters and counts
# ============================================


def _create_typed_policy(
    client: TestClient, headers: dict[str, str], *, policy_type: str, name: str
) -> dict[str, Any]:
    response = client.post(
        "/v1/policies",
        headers=headers,
        json={"name": name, "policy_type": policy_type, "rules": []},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


class TestPolicyListFiltersAndCounts:
    @pytest.mark.parametrize("tenant_state", ["populated", "empty"])
    @pytest.mark.parametrize("include_global", [True, False])
    @pytest.mark.parametrize("policy_type", [None, "global", "tenant", "safety"])
    def test_counts_follow_the_filtered_scope(
        self,
        client: TestClient,
        tenant_state: str,
        include_global: bool,
        policy_type: str | None,
    ) -> None:
        tenant = uuid4()
        headers = _clinic_admin(tenant)
        policies = (
            [
                _create_typed_policy(client, headers, policy_type=kind, name=kind)
                for kind in ("tenant", "safety")
            ]
            if tenant_state == "populated"
            else []
        )
        params: dict[str, str] = {"include_global": str(include_global).lower()}
        if policy_type is not None:
            params["policy_type"] = policy_type

        response = client.get("/v1/policies", params=params, headers=headers)

        expected_global = (
            {str(get_global_policy().id)}
            if include_global and policy_type in (None, "global")
            else set()
        )
        expected_tenant = {
            p["id"]
            for p in policies
            if policy_type is None or p["policy_type"] == policy_type
        }
        assert response.status_code == 200, response.text
        body = response.json()
        assert _all_policy_ids(response) == expected_global | expected_tenant
        assert body["total"] == len(expected_global) + len(expected_tenant)
        assert body["global_count"] == len(expected_global)
        assert body["tenant_count"] == len(expected_tenant)

    def test_inactive_policies_are_listed_only_on_request_and_only_own(
        self, client: TestClient
    ) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        headers_a = _clinic_admin(tenant_a)
        active = _create_typed_policy(
            client, headers_a, policy_type="tenant", name="active"
        )
        inactive = _create_typed_policy(
            client, headers_a, policy_type="tenant", name="inactive"
        )
        foreign = _create_typed_policy(
            client, _clinic_admin(tenant_b), policy_type="tenant", name="foreign"
        )
        for owner, policy in ((tenant_a, inactive), (tenant_b, foreign)):
            deactivated = client.patch(
                f"/v1/policies/{policy['id']}",
                params={"tenant_id": str(owner)},
                json={"is_active": False},
                headers=_clinic_admin(owner),
            )
            assert deactivated.status_code == 200, deactivated.text

        default = client.get("/v1/policies", headers=headers_a)
        everything = client.get(
            "/v1/policies", params={"active_only": "false"}, headers=headers_a
        )
        explicit = client.get(
            "/v1/policies",
            params={"active_only": "false", "tenant_id": str(tenant_a)},
            headers=headers_a,
        )

        assert _tenant_policy_ids(default) == {active["id"]}
        assert _tenant_policy_ids(everything) == {active["id"], inactive["id"]}
        assert _tenant_policy_ids(explicit) == {active["id"], inactive["id"]}
        assert everything.json()["tenant_count"] == 2


# ============================================
# Update error hygiene
# ============================================


class TestPolicyUpdateErrorHygiene:
    SENTINEL = "store-internal-detail-7f3a do not echo"

    @pytest.mark.parametrize(
        ("error", "status_code"),
        [(ValueError, 404), (RuntimeError, 500)],
    )
    def test_update_failures_never_echo_exception_text(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        error: type[Exception],
        status_code: int,
    ) -> None:
        tenant = uuid4()
        headers = _clinic_admin(tenant)
        policy = _create_policy(client, headers)

        async def _failing_update(*_args: Any, **_kwargs: Any) -> PolicyDefinition:
            raise error(f"{self.SENTINEL} {tenant}")

        monkeypatch.setattr(get_policy_store(), "update_policy", _failing_update)
        caplog.set_level(logging.DEBUG)

        response = client.patch(
            f"/v1/policies/{policy['id']}",
            params={"tenant_id": str(tenant)},
            json={"name": RENAMED},
            headers=headers,
        )

        assert response.status_code == status_code, response.text
        expected = {
            404: f"Policy {policy['id']} not found",
            500: "Failed to update policy",
        }[status_code]
        assert response.json() == {"detail": expected}
        assert self.SENTINEL not in caplog.text
        if status_code == 500:
            errors = [
                record.getMessage()
                for record in caplog.records
                if record.levelno == logging.ERROR
                and "Unexpected error updating policy" in record.getMessage()
            ]
            assert len(errors) == 1, caplog.text
            assert error.__name__ in errors[0]
