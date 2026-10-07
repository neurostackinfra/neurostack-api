"""Contract tests for self-service and builder-delegated password changes."""

from datetime import datetime, timedelta, timezone
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID, uuid4

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update

from src.config.settings import settings
from src.models.builder import Builder
from src.models.user import User, UserRole
from src.repositories import get_db_session


def _jwt_headers(
    role: str,
    tenant_id: UUID | None = None,
    user_id: UUID | None = None,
    builder_id: UUID | None = None,
) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id or uuid4()),
        "tenant_id": str(tenant_id or uuid4()),
        "role": role,
        "iat": now,
        "exp": now + timedelta(hours=1),
        "jti": str(uuid4()),
    }
    if builder_id is not None:
        payload["builder_id"] = str(builder_id)
    token = jwt.encode(payload, settings.secret_key, algorithm="HS256")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _login(client: TestClient, email: str, password: str) -> dict[str, str]:
    response = client.post(
        "/v1/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _create_builder(client: TestClient) -> tuple[UUID, str, str, dict[str, str]]:
    email = f"builder-{uuid4().hex}@test.com"
    password = "builder-old-password"
    created = client.post(
        "/v1/admin/builders",
        headers=_jwt_headers("super_admin"),
        json={
            "name": f"Builder {uuid4().hex[:8]}",
            "admin_email": email,
            "admin_password": password,
        },
    )
    assert created.status_code == 201, created.text
    return UUID(created.json()["id"]), email, password, _login(client, email, password)


def _create_builder_clinic(
    client: TestClient, builder_headers: dict[str, str]
) -> tuple[UUID, str, str]:
    email = f"clinic-{uuid4().hex}@test.com"
    password = "clinic-old-password"
    created = client.post(
        f"/v1/admin/builders/{_token_claims(builder_headers)['builder_id']}/clinics",
        headers=_jwt_headers("super_admin"),
        json={
            "clinic_name": f"Clinic {uuid4().hex[:8]}",
            "admin_email": email,
            "admin_password": password,
        },
    )
    assert created.status_code == 201, created.text
    return UUID(created.json()["id"]), email, password


def _token_claims(headers: dict[str, str]) -> dict[str, Any]:
    """Decode a test JWT only to construct an intentional stale-claim case."""
    return jwt.decode(
        headers["Authorization"].removeprefix("Bearer "),
        settings.secret_key,
        algorithms=["HS256"],
    )


def _run_db(client: TestClient, operation: Any) -> Any:
    """Run a deterministic test-only DB mutation on the app event loop."""
    return client._submit(operation)  # type: ignore[attr-defined]


async def _user_by_email(email: str) -> User | None:
    async with get_db_session() as session:
        return cast(
            User | None,
            await session.scalar(select(User).where(User.email == email)),
        )


async def _set_user(email: str, **changes: Any) -> None:
    async with get_db_session() as session:
        user = await session.scalar(select(User).where(User.email == email))
        assert user is not None
        for field, value in changes.items():
            setattr(user, field, value)
        await session.commit()


async def _set_builder(builder_id: UUID, **changes: Any) -> None:
    async with get_db_session() as session:
        builder = await session.scalar(select(Builder).where(Builder.id == builder_id))
        assert builder is not None
        for field, value in changes.items():
            setattr(builder, field, value)
        await session.commit()


async def _add_clinic_admin(clinic_id: UUID) -> None:
    async with get_db_session() as session:
        session.add(
            User(
                id=uuid4(),
                tenant_id=clinic_id,
                email=f"extra-admin-{uuid4().hex}@test.com",
                password_hash="not-used-in-this-test",
                role=UserRole.clinic_admin,
                is_active=True,
            )
        )
        await session.commit()


async def _delete_user(email: str) -> None:
    async with get_db_session() as session:
        user = await session.scalar(select(User).where(User.email == email))
        assert user is not None
        await session.delete(user)
        await session.commit()


async def _clinic_admin_hashes(clinic_id: UUID) -> list[str]:
    async with get_db_session() as session:
        result = await session.execute(
            select(User.password_hash).where(
                User.tenant_id == clinic_id,
                User.role == UserRole.clinic_admin,
            )
        )
        return list(result.scalars())


async def _clinic_admin_emails(clinic_id: UUID) -> list[str]:
    async with get_db_session() as session:
        result = await session.execute(
            select(User.email).where(
                User.tenant_id == clinic_id,
                User.role == UserRole.clinic_admin,
            )
        )
        return list(result.scalars())


def _password_payload(password: str = "replacement-password") -> dict[str, str]:
    return {"new_password": password, "confirm_password": password}


def _assert_forbidden(response: Any) -> None:
    assert response.status_code == 403
    assert response.json() == {"detail": {"error": "password_change_forbidden"}}


def _assert_unchanged_password(
    client: TestClient, email: str, password_hash: str
) -> None:
    user = _run_db(client, _user_by_email(email))
    assert user is not None
    assert user.password_hash == password_hash


def test_clinic_self_password_change_replaces_login_and_preserves_old_jwt(
    client: TestClient,
) -> None:
    _, _, _, builder_headers = _create_builder(client)
    _, email, old_password = _create_builder_clinic(client, builder_headers)
    original_headers = _login(client, email, old_password)

    changed = client.patch(
        "/v1/auth/me/password",
        headers=original_headers,
        json={
            "new_password": "clinic-new-password",
            "confirm_password": "clinic-new-password",
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json() == {"ok": True}
    assert (
        client.post(
            "/v1/auth/login", json={"email": email, "password": old_password}
        ).status_code
        == 401
    )
    assert _login(client, email, "clinic-new-password")

    # Stateless JWTs remain valid while their current database identity is valid.
    second_change = client.patch(
        "/v1/auth/me/password",
        headers=original_headers,
        json={
            "new_password": "clinic-later-password",
            "confirm_password": "clinic-later-password",
        },
    )
    assert second_change.status_code == 200, second_change.text


def test_builder_self_password_change_replaces_login(client: TestClient) -> None:
    _, email, old_password, headers = _create_builder(client)

    changed = client.patch(
        "/v1/auth/me/password",
        headers=headers,
        json=_password_payload("builder-new-password"),
    )
    assert changed.status_code == 200, changed.text
    assert changed.json() == {"ok": True}
    assert (
        client.post(
            "/v1/auth/login", json={"email": email, "password": old_password}
        ).status_code
        == 401
    )
    assert _login(client, email, "builder-new-password")


def test_master_can_still_change_builder_and_clinic_email_and_password(
    client: TestClient,
) -> None:
    builder_id, builder_email, builder_password, builder_headers = _create_builder(
        client
    )
    clinic_id, clinic_email, clinic_password = _create_builder_clinic(
        client, builder_headers
    )
    master_headers = _jwt_headers("super_admin")
    new_builder_email = f"updated-builder-{uuid4().hex}@test.com"
    new_clinic_email = f"updated-clinic-{uuid4().hex}@test.com"

    builder_changed = client.patch(
        f"/v1/admin/builders/{builder_id}",
        headers=master_headers,
        json={"email": new_builder_email, "password": "builder-master-password"},
    )
    assert builder_changed.status_code == 200, builder_changed.text
    assert builder_changed.json() == {"ok": True}
    assert (
        client.post(
            "/v1/auth/login",
            json={"email": builder_email, "password": builder_password},
        ).status_code
        == 401
    )
    assert _login(client, new_builder_email, "builder-master-password")

    clinic_changed = client.patch(
        f"/v1/admin/clinics/{clinic_id}",
        headers=master_headers,
        json={"email": new_clinic_email, "password": "clinic-master-password"},
    )
    assert clinic_changed.status_code == 200, clinic_changed.text
    assert clinic_changed.json() == {"ok": True}
    assert (
        client.post(
            "/v1/auth/login",
            json={"email": clinic_email, "password": clinic_password},
        ).status_code
        == 401
    )
    assert _login(client, new_clinic_email, "clinic-master-password")


def test_old_clinic_jwt_can_self_change_after_builder_reset(client: TestClient) -> None:
    _, _, _, builder_headers = _create_builder(client)
    clinic_id, email, old_password = _create_builder_clinic(client, builder_headers)
    old_clinic_headers = _login(client, email, old_password)

    reset = client.patch(
        f"/v1/builder/clinics/{clinic_id}/password",
        headers=builder_headers,
        json=_password_payload("master-reset-password"),
    )
    assert reset.status_code == 200, reset.text

    # Password changes do not revoke otherwise authorized stateless JWTs.
    changed = client.patch(
        "/v1/auth/me/password",
        headers=old_clinic_headers,
        json=_password_payload("old-jwt-self-change"),
    )
    assert changed.status_code == 200, changed.text
    assert _login(client, email, "old-jwt-self-change")


def test_self_change_rejects_deleted_inactive_and_changed_role_without_writes(
    client: TestClient,
) -> None:
    _, _, _, builder_headers = _create_builder(client)
    _, email, _ = _create_builder_clinic(client, builder_headers)
    headers = _login(client, email, "clinic-old-password")
    original = _run_db(client, _user_by_email(email))
    assert original is not None

    _run_db(client, _set_user(email, is_active=False))
    _assert_forbidden(
        client.patch("/v1/auth/me/password", headers=headers, json=_password_payload())
    )
    _assert_unchanged_password(client, email, original.password_hash)

    _run_db(client, _set_user(email, is_active=True, role=UserRole.investor))
    _assert_forbidden(
        client.patch("/v1/auth/me/password", headers=headers, json=_password_payload())
    )
    _assert_unchanged_password(client, email, original.password_hash)

    _run_db(client, _delete_user(email))
    _assert_forbidden(
        client.patch("/v1/auth/me/password", headers=headers, json=_password_payload())
    )
    assert _run_db(client, _user_by_email(email)) is None


def test_self_change_rejects_tenant_and_builder_reallocation_without_writes(
    client: TestClient,
) -> None:
    builder_id, builder_email, builder_password, builder_headers = _create_builder(
        client
    )
    builder = _run_db(client, _user_by_email(builder_email))
    assert builder is not None

    stale_tenant = _jwt_headers(
        "builder",
        tenant_id=uuid4(),
        user_id=builder.id,
        builder_id=builder_id,
    )
    _assert_forbidden(
        client.patch(
            "/v1/auth/me/password", headers=stale_tenant, json=_password_payload()
        )
    )
    _assert_unchanged_password(client, builder_email, builder.password_hash)

    reallocated_builder_id, _, _, _ = _create_builder(client)
    _run_db(client, _set_user(builder_email, builder_id=reallocated_builder_id))
    _assert_forbidden(
        client.patch(
            "/v1/auth/me/password", headers=builder_headers, json=_password_payload()
        )
    )
    _assert_unchanged_password(client, builder_email, builder.password_hash)
    assert _login(client, builder_email, builder_password)


def test_delegated_change_rejects_stale_authority_and_unlinked_clinic_without_writes(
    client: TestClient,
) -> None:
    builder_id, builder_email, _, builder_headers = _create_builder(client)
    clinic_id, clinic_email, clinic_password = _create_builder_clinic(
        client, builder_headers
    )
    clinic = _run_db(client, _user_by_email(clinic_email))
    assert clinic is not None
    builder_user = _run_db(client, _user_by_email(builder_email))
    assert builder_user is not None

    _run_db(client, _set_user(builder_email, role=UserRole.investor))
    _assert_forbidden(
        client.patch(
            f"/v1/builder/clinics/{clinic_id}/password",
            headers=builder_headers,
            json=_password_payload(),
        )
    )
    _assert_forbidden(
        client.patch(
            f"/v1/builder/clinics/{clinic_id}/email",
            headers=builder_headers,
            json={"email": f"unused-{uuid4().hex}@test.com"},
        )
    )
    _assert_unchanged_password(client, clinic_email, clinic.password_hash)
    assert _run_db(client, _clinic_admin_emails(clinic_id)) == [clinic_email]

    _run_db(client, _set_user(builder_email, role=UserRole.builder))
    for stale_headers in (
        _jwt_headers(
            "builder",
            tenant_id=uuid4(),
            user_id=builder_user.id,
            builder_id=builder_id,
        ),
        _jwt_headers(
            "builder",
            tenant_id=builder_user.tenant_id,
            user_id=builder_user.id,
            builder_id=uuid4(),
        ),
    ):
        _assert_forbidden(
            client.patch(
                f"/v1/builder/clinics/{clinic_id}/email",
                headers=stale_headers,
                json={"email": f"unused-{uuid4().hex}@test.com"},
            )
        )
        assert _run_db(client, _clinic_admin_emails(clinic_id)) == [clinic_email]

    unlinked = client.post(
        f"/v1/admin/builders/{builder_id}/unlink-clinic",
        headers=_jwt_headers("super_admin"),
        json={"clinic_id": str(clinic_id)},
    )
    assert unlinked.status_code == 200, unlinked.text
    missing = client.patch(
        f"/v1/builder/clinics/{clinic_id}/password",
        headers=builder_headers,
        json=_password_payload(),
    )
    assert missing.status_code == 404
    assert missing.json() == {"detail": {"error": "clinic_not_available"}}
    missing_email = client.patch(
        f"/v1/builder/clinics/{clinic_id}/email",
        headers=builder_headers,
        json={"email": f"unused-{uuid4().hex}@test.com"},
    )
    assert missing_email.status_code == 404
    assert missing_email.json() == {"detail": {"error": "clinic_not_available"}}
    _assert_unchanged_password(client, clinic_email, clinic.password_hash)
    assert _run_db(client, _clinic_admin_emails(clinic_id)) == [clinic_email]
    assert _login(client, clinic_email, clinic_password)


def test_delegated_change_requires_exactly_one_clinic_admin_without_writes(
    client: TestClient,
) -> None:
    _, _, _, builder_headers = _create_builder(client)
    clinic_id, clinic_email, clinic_password = _create_builder_clinic(
        client, builder_headers
    )
    clinic = _run_db(client, _user_by_email(clinic_email))
    assert clinic is not None

    _run_db(client, _set_user(clinic_email, role=UserRole.investor))
    for setup in ("zero", "multiple"):
        if setup == "multiple":
            _run_db(client, _set_user(clinic_email, role=UserRole.clinic_admin))
            _run_db(client, _add_clinic_admin(clinic_id))
        hashes_before = _run_db(client, _clinic_admin_hashes(clinic_id))
        conflict = client.patch(
            f"/v1/builder/clinics/{clinic_id}/password",
            headers=builder_headers,
            json=_password_payload(),
        )
        assert conflict.status_code == 409
        assert conflict.json() == {"detail": {"error": "clinic_admin_unavailable"}}
        _assert_unchanged_password(client, clinic_email, clinic.password_hash)
        assert _run_db(client, _clinic_admin_hashes(clinic_id)) == hashes_before
    assert _login(client, clinic_email, clinic_password)


def test_builder_can_reset_only_a_linked_clinic_password(client: TestClient) -> None:
    _, _, _, builder_headers = _create_builder(client)
    clinic_id, clinic_email, clinic_old_password = _create_builder_clinic(
        client, builder_headers
    )

    changed = client.patch(
        f"/v1/builder/clinics/{clinic_id}/password",
        headers=builder_headers,
        json={
            "new_password": "clinic-reset-password",
            "confirm_password": "clinic-reset-password",
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json() == {"ok": True}
    assert (
        client.post(
            "/v1/auth/login",
            json={"email": clinic_email, "password": clinic_old_password},
        ).status_code
        == 401
    )
    assert _login(client, clinic_email, "clinic-reset-password")


def test_builder_can_change_linked_clinic_email_and_listing_stays_in_sync(
    client: TestClient,
) -> None:
    _, _, _, builder_headers = _create_builder(client)
    clinic_id, old_email, password = _create_builder_clinic(client, builder_headers)
    new_email = f"updated-clinic-{uuid4().hex}@test.com"

    listed_before = client.get("/v1/builder/clinics", headers=builder_headers)
    assert listed_before.status_code == 200, listed_before.text
    listed_clinic = next(
        item for item in listed_before.json() if item["id"] == str(clinic_id)
    )
    assert listed_clinic["admin_email"] == old_email

    changed = client.patch(
        f"/v1/builder/clinics/{clinic_id}/email",
        headers=builder_headers,
        json={"email": new_email},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json() == {"ok": True, "admin_email": new_email}
    assert _run_db(client, _clinic_admin_emails(clinic_id)) == [new_email]
    assert (
        client.post(
            "/v1/auth/login", json={"email": old_email, "password": password}
        ).status_code
        == 401
    )
    assert _login(client, new_email, password)

    idempotent = client.patch(
        f"/v1/builder/clinics/{clinic_id}/email",
        headers=builder_headers,
        json={"email": new_email},
    )
    assert idempotent.status_code == 200, idempotent.text
    assert idempotent.json() == {"ok": True, "admin_email": new_email}

    listed_after = client.get("/v1/builder/clinics", headers=builder_headers)
    assert listed_after.status_code == 200, listed_after.text
    listed_clinic = next(
        item for item in listed_after.json() if item["id"] == str(clinic_id)
    )
    assert listed_clinic["admin_email"] == new_email


def test_builder_clinic_email_rejects_duplicates_and_admin_cardinality(
    client: TestClient,
) -> None:
    _, _, _, builder_headers = _create_builder(client)
    clinic_id, clinic_email, clinic_password = _create_builder_clinic(
        client, builder_headers
    )
    _, conflicting_email, _ = _create_builder_clinic(client, builder_headers)

    original_execute = AsyncSession.execute
    update_attempts = 0

    async def reject_duplicate_update(
        session: AsyncSession, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        nonlocal update_attempts
        if isinstance(statement, Update):
            update_attempts += 1
            raise AssertionError("duplicate email reached UPDATE instead of precheck")
        return await original_execute(session, statement, *args, **kwargs)

    with patch(
        "src.api.routes.builder.AsyncSession.execute", new=reject_duplicate_update
    ):
        duplicate = client.patch(
            f"/v1/builder/clinics/{clinic_id}/email",
            headers=builder_headers,
            json={"email": conflicting_email},
        )
    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": {"error": "email_already_in_use"}}
    assert update_attempts == 0
    assert _run_db(client, _clinic_admin_emails(clinic_id)) == [clinic_email]

    _run_db(client, _set_user(clinic_email, role=UserRole.investor))
    for setup in ("zero", "multiple"):
        if setup == "multiple":
            _run_db(client, _set_user(clinic_email, role=UserRole.clinic_admin))
            _run_db(client, _add_clinic_admin(clinic_id))
        emails_before = _run_db(client, _clinic_admin_emails(clinic_id))
        unavailable = client.patch(
            f"/v1/builder/clinics/{clinic_id}/email",
            headers=builder_headers,
            json={"email": f"unused-{uuid4().hex}@test.com"},
        )
        assert unavailable.status_code == 409
        assert unavailable.json() == {"detail": {"error": "clinic_admin_unavailable"}}
        assert _run_db(client, _clinic_admin_emails(clinic_id)) == emails_before
    listed = client.get("/v1/builder/clinics", headers=builder_headers)
    listed_clinic = next(item for item in listed.json() if item["id"] == str(clinic_id))
    assert listed_clinic["admin_email"] is None
    assert _login(client, clinic_email, clinic_password)


def test_builder_clinic_email_translates_post_precheck_integrity_race(
    client: TestClient,
) -> None:
    _, _, _, builder_headers = _create_builder(client)
    clinic_id, clinic_email, _ = _create_builder_clinic(client, builder_headers)
    unused_email = f"race-{uuid4().hex}@test.com"

    with patch(
        "src.api.routes.builder.AsyncSession.commit",
        side_effect=IntegrityError(
            statement="UPDATE users SET email = :email",
            params={"email": unused_email},
            orig=Exception(
                "duplicate key value violates unique constraint users_email"
            ),
        ),
    ):
        raced = client.patch(
            f"/v1/builder/clinics/{clinic_id}/email",
            headers=builder_headers,
            json={"email": unused_email},
        )

    assert raced.status_code == 409
    assert raced.json() == {"detail": {"error": "email_already_in_use"}}
    assert _run_db(client, _clinic_admin_emails(clinic_id)) == [clinic_email]


def test_builder_clinic_list_rejects_stale_database_authority(
    client: TestClient,
) -> None:
    builder_id, builder_email, _, builder_headers = _create_builder(client)
    _, clinic_email, _ = _create_builder_clinic(client, builder_headers)
    builder_user = _run_db(client, _user_by_email(builder_email))
    assert builder_user is not None

    def assert_forbidden(headers: dict[str, str]) -> None:
        response = client.get("/v1/builder/clinics", headers=headers)
        assert response.status_code == 403
        assert clinic_email not in response.text

    assert_forbidden(
        _jwt_headers(
            "builder",
            tenant_id=uuid4(),
            user_id=builder_user.id,
            builder_id=builder_id,
        )
    )
    assert_forbidden(
        _jwt_headers(
            "builder",
            tenant_id=builder_user.tenant_id,
            user_id=builder_user.id,
            builder_id=uuid4(),
        )
    )

    _run_db(client, _set_user(builder_email, is_active=False))
    assert_forbidden(builder_headers)
    _run_db(client, _set_user(builder_email, is_active=True, role=UserRole.investor))
    assert_forbidden(builder_headers)
    _run_db(
        client,
        _set_user(
            builder_email,
            role=UserRole.builder,
            tenant_id=uuid4(),
        ),
    )
    assert_forbidden(builder_headers)
    _run_db(client, _set_user(builder_email, tenant_id=builder_user.tenant_id))
    _run_db(client, _set_builder(builder_id, is_active=False))
    assert_forbidden(builder_headers)
    _run_db(client, _set_builder(builder_id, is_active=True, status="inactive"))
    assert_forbidden(builder_headers)
    _run_db(client, _set_builder(builder_id, status="active"))
    reassigned_builder_id, _, _, _ = _create_builder(client)
    _run_db(client, _set_user(builder_email, builder_id=reassigned_builder_id))
    assert_forbidden(builder_headers)
    _run_db(client, _delete_user(builder_email))
    assert_forbidden(builder_headers)


def test_password_routes_reject_api_keys_roles_and_stale_tenant_claims(
    client: TestClient,
) -> None:
    _, _, _, builder_headers = _create_builder(client)
    clinic_id, clinic_email, clinic_password = _create_builder_clinic(
        client, builder_headers
    )
    clinic_headers = _login(client, clinic_email, clinic_password)
    payload = {"new_password": "still-secret", "confirm_password": "still-secret"}

    api_key = client.patch(
        "/v1/auth/me/password", headers={"X-API-Key": "test-api-key"}, json=payload
    )
    assert api_key.status_code == 403
    assert api_key.json() == {"detail": {"error": "password_change_forbidden"}}
    email_api_key = client.patch(
        f"/v1/builder/clinics/{uuid4()}/email",
        headers={"X-API-Key": "test-api-key"},
        json={"email": "unused@test.com"},
    )
    assert email_api_key.status_code == 403
    assert email_api_key.json() == {"detail": {"error": "password_change_forbidden"}}
    for role, role_headers in (
        ("clinic_admin", clinic_headers),
        ("super_admin", _jwt_headers("super_admin")),
        ("investor", _jwt_headers("investor")),
    ):
        if role != "clinic_admin":
            denied = client.patch(
                "/v1/auth/me/password", headers=role_headers, json=payload
            )
            assert denied.status_code == 403
            assert denied.json() == {"detail": {"error": "password_change_forbidden"}}
        email_denied = client.patch(
            f"/v1/builder/clinics/{clinic_id}/email",
            headers=role_headers,
            json={"email": f"forbidden-{uuid4().hex}@test.com"},
        )
        assert email_denied.status_code == 403
        assert email_denied.json() == {"detail": {"error": "password_change_forbidden"}}
        assert _run_db(client, _clinic_admin_emails(clinic_id)) == [clinic_email]

    token = clinic_headers["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    stale = _jwt_headers("clinic_admin", tenant_id=uuid4(), user_id=UUID(claims["sub"]))
    denied = client.patch("/v1/auth/me/password", headers=stale, json=payload)
    assert denied.status_code == 403
    assert denied.json() == {"detail": {"error": "password_change_forbidden"}}
    assert _login(client, clinic_email, clinic_password)

    missing_bearer = client.patch("/v1/auth/me/password", json=payload)
    assert missing_bearer.status_code == 401
    missing_builder_bearer = client.patch(
        f"/v1/builder/clinics/{uuid4()}/password", json=payload
    )
    assert missing_builder_bearer.status_code == 401
    missing_email_bearer = client.patch(
        f"/v1/builder/clinics/{uuid4()}/email",
        json={"email": "unused@test.com"},
    )
    assert missing_email_bearer.status_code == 401


def test_delegated_route_hides_foreign_and_unknown_clinics(client: TestClient) -> None:
    _, _, _, builder_a_headers = _create_builder(client)
    _, _, _, builder_b_headers = _create_builder(client)
    foreign_clinic_id, foreign_email, foreign_password = _create_builder_clinic(
        client, builder_b_headers
    )
    payload = {"new_password": "foreign-secret", "confirm_password": "foreign-secret"}

    for clinic_id in (foreign_clinic_id, uuid4(), "not-a-uuid"):
        denied = client.patch(
            f"/v1/builder/clinics/{clinic_id}/password",
            headers=builder_a_headers,
            json=payload,
        )
        assert denied.status_code == 404
        assert denied.json() == {"detail": {"error": "clinic_not_available"}}
        email_denied = client.patch(
            f"/v1/builder/clinics/{clinic_id}/email",
            headers=builder_a_headers,
            json={"email": f"unused-{uuid4().hex}@test.com"},
        )
        assert email_denied.status_code == 404
        assert email_denied.json() == {"detail": {"error": "clinic_not_available"}}
    assert _login(client, foreign_email, foreign_password)


def test_password_validation_is_input_free_and_openapi_is_bearer_only(
    client: TestClient, caplog: Any
) -> None:
    _, _, _, builder_headers = _create_builder(client)
    clinic_id, email, password = _create_builder_clinic(client, builder_headers)
    headers = _login(client, email, password)
    sentinel = "response-and-log-sentinel"

    for payload in (
        {"new_password": sentinel, "confirm_password": "different-sentinel"},
        {"new_password": sentinel, "confirm_password": sentinel, "email": sentinel},
        {"new_password": "short", "confirm_password": "short"},
        {"new_password": 123456, "confirm_password": 123456},
    ):
        invalid = client.patch("/v1/auth/me/password", headers=headers, json=payload)
        assert invalid.status_code == 422
        detail = invalid.json()["detail"]
        assert detail["error"] == "invalid_password_payload"
        assert all(set(field) == {"field", "code"} for field in detail["fields"])
        assert sentinel not in invalid.text

    delegated_invalid = client.patch(
        f"/v1/builder/clinics/{clinic_id}/password",
        headers=builder_headers,
        json={
            "new_password": sentinel,
            "confirm_password": sentinel,
            "email": sentinel,
        },
    )
    assert delegated_invalid.status_code == 422
    assert sentinel not in delegated_invalid.text

    for payload in (
        {"email": "not-an-email"},
        {"email": f"valid-{uuid4().hex}@test.com", "extra": "forbidden"},
    ):
        invalid_email = client.patch(
            f"/v1/builder/clinics/{clinic_id}/email",
            headers=builder_headers,
            json=payload,
        )
        assert invalid_email.status_code == 422

    valid_sentinel = "valid-response-and-log-sentinel"
    valid = client.patch(
        "/v1/auth/me/password",
        headers=headers,
        json=_password_payload(valid_sentinel),
    )
    assert valid.status_code == 200, valid.text
    assert valid.json() == {"ok": True}
    assert valid_sentinel not in valid.text
    assert sentinel not in caplog.text
    assert valid_sentinel not in caplog.text

    schema = client.get("/v1/openapi.json").json()
    for path in (
        "/v1/auth/me/password",
        "/v1/builder/clinics/{clinic_id}/password",
        "/v1/builder/clinics/{clinic_id}/email",
    ):
        operation = schema["paths"][path]["patch"]
        assert operation["security"] == [{"BearerAuth": []}]
        assert all(
            parameter["name"] != "X-API-Key"
            for parameter in operation.get("parameters", [])
        )
    assert {"200", "401", "403", "422"} <= set(
        schema["paths"]["/v1/auth/me/password"]["patch"]["responses"]
    )
    assert {"200", "401", "403", "404", "409", "422"} <= set(
        schema["paths"]["/v1/builder/clinics/{clinic_id}/password"]["patch"][
            "responses"
        ]
    )
    assert {"200", "401", "403", "404", "409", "422"} <= set(
        schema["paths"]["/v1/builder/clinics/{clinic_id}/email"]["patch"]["responses"]
    )
    assert any(
        parameter["name"] == "X-API-Key"
        for parameter in schema["paths"]["/v1/builder/clinics"]["get"]["parameters"]
    )
