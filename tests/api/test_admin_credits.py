"""Credit admin API (Group 5): role assignments and signed authorizations.

* the first (and every) assignment of each role is created only by an
  authenticated JWT ``super_admin``, never for itself; an API key, a
  ``clinic_admin`` or a ``builder`` JWT is refused before anything is
  written;
* issuance fails closed (503, nothing written) with the signing key absent,
  shorter than 32 bytes or equal to the ASAAS receipt key; an authorization
  is never issued to its own issuer;
* the signature is HMAC-SHA256 of the canonical payload with the server key.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from src.config.settings import settings
from src.models.operational_capability import (
    CreditOperationalAuthorization,
    CreditOperationalRoleAssignment,
)
from src.repositories.database import get_db_session
from src.services.content_retention import canonical
from tests.api.test_credit_operational_authorization import (
    ADMIN,
    KEY,
    World,
    _authorize,
    _grant,
)

pytest_plugins = ["tests.api.test_credit_operational_authorization"]


async def _count(model: Any) -> int:
    session = get_db_session()
    try:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)
    finally:
        await session.close()


def test_super_admin_grants_and_lists_roles(client: Any, world: World) -> None:
    for role, user in (
        ("production_data_lifecycle_owner", "owner"),
        ("legal_hold_officer", "officer_1"),
        ("legal_hold_release_officer", "releaser"),
    ):
        _grant(client, world, role, user)
    listed = client.get(f"{ADMIN}/role-assignments", headers=world.bearer("admin_a"))
    assert listed.status_code == 200
    rows = listed.json()
    assert {row["role"] for row in rows} == {
        "production_data_lifecycle_owner",
        "legal_hold_officer",
        "legal_hold_release_officer",
    }
    assert {row["actor_user_id"] for row in rows} == {str(world.users["admin_a"])}


def test_never_for_oneself(client: Any, world: World) -> None:
    response = client.post(
        f"{ADMIN}/role-assignments",
        json={
            "role": "legal_hold_officer",
            "user_id": str(world.users["admin_a"]),
            "reason": "me",
        },
        headers=world.bearer("admin_a"),
    )
    assert response.status_code == 403
    assert response.json() == {"code": "self_assignment_forbidden"}
    assert client._submit(_count(CreditOperationalRoleAssignment)) == 0


@pytest.mark.parametrize("caller", ["clinic_admin_jwt", "api_key"])
def test_only_a_super_admin_jwt_grants(client: Any, world: World, caller: str) -> None:
    headers = (
        world.bearer("officer_1")
        if caller == "clinic_admin_jwt"
        else {"X-API-Key": world.api_key}
    )
    response = client.post(
        f"{ADMIN}/role-assignments",
        json={
            "role": "legal_hold_officer",
            "user_id": str(world.users["officer_2"]),
            "reason": "x",
        },
        headers=headers,
    )
    assert response.status_code == 403
    assert client._submit(_count(CreditOperationalRoleAssignment)) == 0


def test_unknown_user_and_double_revocation(client: Any, world: World) -> None:
    unknown = client.post(
        f"{ADMIN}/role-assignments",
        json={"role": "legal_hold_officer", "user_id": str(uuid4()), "reason": "x"},
        headers=world.bearer("admin_a"),
    )
    assert unknown.status_code == 404
    assignment = _grant(client, world, "legal_hold_officer", "officer_1")
    path = f"{ADMIN}/role-assignments/{assignment}/revocations"
    first = client.post(path, json={"reason": "x"}, headers=world.bearer("admin_b"))
    second = client.post(path, json={"reason": "x"}, headers=world.bearer("admin_b"))
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json() == {"code": "assignment_already_revoked"}


@pytest.mark.parametrize("key", ["", "short-key", "x" * 31])
def test_issuance_fails_closed_without_a_32_byte_key(
    client: Any, world: World, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    monkeypatch.setattr(settings, "credit_authorization_signing_key", SecretStr(key))
    response = client.post(
        f"{ADMIN}/authorizations",
        json={
            "purpose": "hold_request",
            "builder_id": str(world.builder),
            "scope": "builder",
            "subject_user_id": str(world.users["officer_1"]),
        },
        headers=world.bearer("admin_a"),
    )
    assert response.status_code == 503
    assert response.json() == {"code": "credit_authorization_key_unavailable"}
    assert client._submit(_count(CreditOperationalAuthorization)) == 0


def test_issuance_fails_closed_with_the_asaas_receipt_key(
    client: Any, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not hasattr(settings, "billing_operation_receipt_signing_key"):
        pytest.fail("settings must define billing_operation_receipt_signing_key")
    current = settings.billing_operation_receipt_signing_key
    shared = SecretStr(KEY) if hasattr(current, "get_secret_value") else KEY
    monkeypatch.setattr(settings, "billing_operation_receipt_signing_key", shared)
    response = client.post(
        f"{ADMIN}/authorizations",
        json={
            "purpose": "hold_request",
            "builder_id": str(world.builder),
            "scope": "builder",
            "subject_user_id": str(world.users["officer_1"]),
        },
        headers=world.bearer("admin_a"),
    )
    assert response.status_code == 503
    assert client._submit(_count(CreditOperationalAuthorization)) == 0


def test_signature_is_hmac_of_the_canonical_payload(client: Any, world: World) -> None:
    issued = _authorize(client, world, "hold_request", "officer_1")
    expected = hmac.new(
        KEY.encode(), canonical(issued["payload"]), hashlib.sha256
    ).hexdigest()
    assert issued["signature"] == expected
    assert issued["payload"]["environment"] == settings.environment
    assert issued["payload"]["issued_by"] == str(world.users["admin_b"])


def test_no_authorization_for_its_own_issuer_and_purge_needs_a_generation(
    client: Any, world: World
) -> None:
    own = client.post(
        f"{ADMIN}/authorizations",
        json={
            "purpose": "hold_request",
            "builder_id": str(world.builder),
            "scope": "builder",
            "subject_user_id": str(world.users["admin_a"]),
        },
        headers=world.bearer("admin_a"),
    )
    assert own.json() == {"code": "self_authorization_forbidden"}
    missing = client.post(
        f"{ADMIN}/authorizations",
        json={
            "purpose": "purge",
            "builder_id": str(world.builder),
            "scope": "builder",
            "subject_user_id": str(world.users["owner"]),
        },
        headers=world.bearer("admin_a"),
    )
    assert missing.status_code == 422
    assert client._submit(_count(CreditOperationalAuthorization)) == 0
