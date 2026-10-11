"""Account emails are unique ignoring case across every route that writes them."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from src.models.user import User, UserRole
from src.repositories import get_db_session
from tests.api.test_password_management import (
    _create_builder,
    _create_builder_clinic,
    _jwt_headers,
    _login,
    _run_db,
)


def _variant(email: str) -> str:
    """Same address with a different-case local part (EmailStr keeps it)."""
    local, _, domain = email.partition("@")
    return f"{local.upper()}@{domain}"


def _account(client: TestClient) -> tuple[str, dict[str, str]]:
    _, email, _, builder_headers = _create_builder(client)
    return email, builder_headers


@pytest.mark.parametrize(
    "route",
    ["admin_clinic", "admin_builder", "admin_builder_clinic", "builder_clinic"],
)
def test_create_routes_reject_a_case_variant_of_an_existing_email(
    client: TestClient, route: str
) -> None:
    builder_id, existing_email, _, builder_headers = _create_builder(client)
    master = _jwt_headers("super_admin")
    payload = {
        "clinic_name": f"Clinic {uuid4().hex[:8]}",
        "admin_email": _variant(existing_email),
        "admin_password": "variant-password",
    }
    if route == "admin_clinic":
        response = client.post("/v1/admin/clinics", headers=master, json=payload)
    elif route == "admin_builder":
        response = client.post(
            "/v1/admin/builders",
            headers=master,
            json={
                "name": f"Builder {uuid4().hex[:8]}",
                "admin_email": payload["admin_email"],
                "admin_password": "variant-password",
            },
        )
    elif route == "admin_builder_clinic":
        response = client.post(
            f"/v1/admin/builders/{builder_id}/clinics",
            headers=master,
            json=payload,
        )
    else:
        response = client.post(
            "/v1/builder/clinics", headers=builder_headers, json=payload
        )

    assert response.status_code == 409, response.text
    login = client.post(
        "/v1/auth/login",
        json={"email": _variant(existing_email), "password": "variant-password"},
    )
    assert login.status_code == 401


def test_admin_email_updates_reject_another_accounts_variant(
    client: TestClient,
) -> None:
    other_email, _ = _account(client)
    builder_id, builder_email, _, builder_headers = _create_builder(client)
    clinic_id, clinic_email, _ = _create_builder_clinic(client, builder_headers)
    master = _jwt_headers("super_admin")

    for url in (f"/v1/admin/builders/{builder_id}", f"/v1/admin/clinics/{clinic_id}"):
        response = client.patch(
            url, headers=master, json={"email": _variant(other_email)}
        )
        assert response.status_code == 409, (url, response.text)

    assert _login(client, builder_email, "builder-old-password")
    assert _login(client, clinic_email, "clinic-old-password")


def test_admin_email_updates_accept_a_case_change_of_the_own_email(
    client: TestClient,
) -> None:
    builder_id, builder_email, _, builder_headers = _create_builder(client)
    clinic_id, clinic_email, _ = _create_builder_clinic(client, builder_headers)
    master = _jwt_headers("super_admin")

    builder_changed = client.patch(
        f"/v1/admin/builders/{builder_id}",
        headers=master,
        json={"email": _variant(builder_email)},
    )
    clinic_changed = client.patch(
        f"/v1/admin/clinics/{clinic_id}",
        headers=master,
        json={"email": _variant(clinic_email)},
    )

    assert builder_changed.status_code == 200, builder_changed.text
    assert clinic_changed.status_code == 200, clinic_changed.text
    assert _login(client, _variant(builder_email), "builder-old-password")
    assert _login(client, _variant(clinic_email), "clinic-old-password")


def test_builder_clinic_email_update_follows_the_same_rule(
    client: TestClient,
) -> None:
    other_email, _ = _account(client)
    _, _, _, builder_headers = _create_builder(client)
    clinic_id, clinic_email, _ = _create_builder_clinic(client, builder_headers)
    url = f"/v1/builder/clinics/{clinic_id}/email"

    duplicate = client.patch(
        url, headers=builder_headers, json={"email": _variant(other_email)}
    )
    own_case = client.patch(
        url, headers=builder_headers, json={"email": _variant(clinic_email)}
    )

    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": {"error": "email_already_in_use"}}
    assert own_case.status_code == 200, own_case.text
    assert _login(client, _variant(clinic_email), "clinic-old-password")


async def _insert_user(email: str, tenant_id: object) -> None:
    async with get_db_session() as session:
        async with session.begin():
            session.add(
                User(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    email=email,
                    password_hash="x",
                    role=UserRole.clinic_admin,
                    is_active=True,
                )
            )


def test_database_index_rejects_case_variants(client: TestClient) -> None:
    _, _, _, builder_headers = _create_builder(client)
    clinic_id, clinic_email, _ = _create_builder_clinic(client, builder_headers)

    with pytest.raises(IntegrityError):
        _run_db(client, _insert_user(_variant(clinic_email), clinic_id))
