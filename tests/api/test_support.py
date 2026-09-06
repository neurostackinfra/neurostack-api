"""
NeuroStack API - Support Endpoint Tests

Covers conditional routing of the support ticket email between:
  - Central NeuroStack inbox (settings.support_email_to)
  - The builder's user email (when the clinic's tenant is linked to an active
    builder with a matching role=builder user)

Also verifies that `SupportTicket.routed_to` / `routed_to_builder_id` are
persisted with the same decision, and provides regression smoke tests that
the GET/PATCH admin endpoints still enforce the super_admin role.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from src.config.settings import settings
from src.models.builder import Builder
from src.models.support_ticket import SupportTicket
from src.models.tenant import Tenant
from src.models.user import User, UserRole


# ============================================
# Token Helpers (mirrors tests/api/test_builder.py)
# ============================================


def _make_clinic_admin_token(tenant_id: UUID, user_id: UUID | None = None) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(user_id or uuid4()),
            "tenant_id": str(tenant_id),
            "role": "clinic_admin",
            "email": "clinic-admin@test.com",
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": str(uuid4()),
        },
        settings.secret_key,
        algorithm="HS256",
    )


def _make_builder_token(builder_id: UUID | None = None, tenant_id: UUID | None = None) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(tenant_id or uuid4()),
            "role": "builder",
            "email": "builder@test.com",
            "builder_id": str(builder_id or uuid4()),
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": str(uuid4()),
        },
        settings.secret_key,
        algorithm="HS256",
    )


def _make_super_admin_token(tenant_id: UUID | None = None) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(tenant_id or uuid4()),
            "role": "super_admin",
            "email": "super-admin@test.com",
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": str(uuid4()),
        },
        settings.secret_key,
        algorithm="HS256",
    )


# ============================================
# Helpers
# ============================================


async def _open_session():
    """Return a fresh AsyncSession bound to the initialized test engine."""
    from src.repositories import get_db_session

    return get_db_session()


async def _seed_tenant(
    *,
    builder_id: UUID | None = None,
    name: str = "Test Clinic",
) -> Tenant:
    session = await _open_session()
    try:
        tenant = Tenant(
            id=uuid4(),
            name=name,
            slug=f"clinic-{uuid4().hex[:8]}",
            api_key_hash=f"hash-{uuid4().hex}",
            is_active=True,
            bot_active=True,
            builder_id=builder_id,
            plan_type="pilot",
            config={},
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant
    finally:
        await session.close()


async def _seed_builder(
    *, status_: str = "active", support_email: str | None = None
) -> Builder:
    session = await _open_session()
    try:
        builder = Builder(
            id=uuid4(),
            name=f"Builder {uuid4().hex[:6]}",
            status=status_,
            is_active=(status_ == "active"),
            billing_cycle="monthly",
            subscription_status="trial",
            support_email=support_email,
        )
        session.add(builder)
        await session.commit()
        await session.refresh(builder)
        return builder
    finally:
        await session.close()


async def _seed_builder_user(builder_id: UUID, tenant_id: UUID, email: str) -> User:
    session = await _open_session()
    try:
        user = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email=email,
            password_hash="not-a-real-hash",
            role=UserRole.builder,
            builder_id=builder_id,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user
    finally:
        await session.close()


async def _fetch_ticket(ticket_id: int) -> SupportTicket | None:
    session = await _open_session()
    try:
        result = await session.execute(
            select(SupportTicket).where(SupportTicket.id == ticket_id)
        )
        return result.scalar_one_or_none()
    finally:
        await session.close()


class _SmtpCapture:
    """Async mock replacement for aiosmtplib.send that records the MIME message."""

    def __init__(self) -> None:
        self.messages: list = []
        self.kwargs: list[dict] = []

    async def __call__(self, message, **kwargs):
        self.messages.append(message)
        self.kwargs.append(kwargs)
        return {}


@pytest.fixture
def smtp_capture(monkeypatch: pytest.MonkeyPatch) -> _SmtpCapture:
    """Patch aiosmtplib.send (module-level import in support route) + set smtp_host."""
    capture = _SmtpCapture()
    monkeypatch.setattr("src.api.routes.support.aiosmtplib.send", capture)
    monkeypatch.setattr(settings, "smtp_host", "smtp.test.local")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_user", "noreply@test.local")
    monkeypatch.setattr(settings, "smtp_password", "secret")
    return capture


# ============================================
# Routing Tests
# ============================================


class TestSupportTicketRouting:
    """Verify create_support_ticket routes to the right recipient and persists routed_to."""

    @pytest.mark.asyncio
    async def test_active_builder_with_user_routes_to_builder_email(
        self, async_client: AsyncClient, smtp_capture: _SmtpCapture
    ):
        builder = await _seed_builder(status_="active")
        tenant = await _seed_tenant(builder_id=builder.id)
        builder_user_email = f"builder-user-{uuid4().hex[:6]}@builder.test"
        await _seed_builder_user(
            builder_id=builder.id, tenant_id=tenant.id, email=builder_user_email
        )

        headers = {
            "Authorization": f"Bearer {_make_clinic_admin_token(tenant_id=tenant.id)}",
            "Content-Type": "application/json",
        }
        resp = await async_client.post(
            "/v1/support/tickets",
            headers=headers,
            json={"tipo": "solicitacao", "mensagem": "please help"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["email_sent"] is True
        assert "ticket_id" in data
        # POST response contract unchanged — no routed_to leaked here
        assert "routed_to" not in data

        # SMTP recipient == builder's user email
        assert len(smtp_capture.messages) == 1
        assert smtp_capture.messages[0]["To"] == builder_user_email

        # Persisted routing decision matches
        row = await _fetch_ticket(data["ticket_id"])
        assert row is not None
        assert row.routed_to == "builder"
        assert row.routed_to_builder_id == builder.id

    @pytest.mark.asyncio
    async def test_no_builder_on_tenant_routes_to_central(
        self, async_client: AsyncClient, smtp_capture: _SmtpCapture
    ):
        tenant = await _seed_tenant(builder_id=None)

        headers = {
            "Authorization": f"Bearer {_make_clinic_admin_token(tenant_id=tenant.id)}",
            "Content-Type": "application/json",
        }
        resp = await async_client.post(
            "/v1/support/tickets",
            headers=headers,
            json={"tipo": "solicitacao", "mensagem": "unbranded clinic"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert len(smtp_capture.messages) == 1
        assert smtp_capture.messages[0]["To"] == settings.support_email_to

        row = await _fetch_ticket(data["ticket_id"])
        assert row is not None
        assert row.routed_to == "central"
        assert row.routed_to_builder_id is None

    @pytest.mark.asyncio
    async def test_builder_without_user_falls_back_to_central(
        self, async_client: AsyncClient, smtp_capture: _SmtpCapture
    ):
        builder = await _seed_builder(status_="active")
        tenant = await _seed_tenant(builder_id=builder.id)
        # No builder-role user seeded for this builder

        headers = {
            "Authorization": f"Bearer {_make_clinic_admin_token(tenant_id=tenant.id)}",
            "Content-Type": "application/json",
        }
        resp = await async_client.post(
            "/v1/support/tickets",
            headers=headers,
            json={"tipo": "feedback", "mensagem": "no builder user"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert len(smtp_capture.messages) == 1
        assert smtp_capture.messages[0]["To"] == settings.support_email_to

        row = await _fetch_ticket(data["ticket_id"])
        assert row is not None
        assert row.routed_to == "central"
        assert row.routed_to_builder_id is None

    @pytest.mark.asyncio
    async def test_suspended_builder_falls_back_to_central_even_with_user(
        self, async_client: AsyncClient, smtp_capture: _SmtpCapture
    ):
        builder = await _seed_builder(status_="suspended")
        tenant = await _seed_tenant(builder_id=builder.id)
        await _seed_builder_user(
            builder_id=builder.id,
            tenant_id=tenant.id,
            email=f"suspended-{uuid4().hex[:6]}@builder.test",
        )

        headers = {
            "Authorization": f"Bearer {_make_clinic_admin_token(tenant_id=tenant.id)}",
            "Content-Type": "application/json",
        }
        resp = await async_client.post(
            "/v1/support/tickets",
            headers=headers,
            json={"tipo": "solicitacao", "mensagem": "builder is suspended"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert len(smtp_capture.messages) == 1
        assert smtp_capture.messages[0]["To"] == settings.support_email_to

        row = await _fetch_ticket(data["ticket_id"])
        assert row is not None
        assert row.routed_to == "central"
        assert row.routed_to_builder_id is None

    @pytest.mark.asyncio
    async def test_active_builder_with_support_email_routes_to_support_email(
        self, async_client: AsyncClient, smtp_capture: _SmtpCapture
    ):
        """Group 2: when Builder.support_email is set and a role=builder user exists,
        the ticket is delivered to support_email rather than the user's login email."""
        support_email = f"support-{uuid4().hex[:6]}@builder.test"
        builder = await _seed_builder(status_="active", support_email=support_email)
        tenant = await _seed_tenant(builder_id=builder.id)
        # role=builder user still exists — that gate is preserved
        builder_user_email = f"user-{uuid4().hex[:6]}@builder.test"
        await _seed_builder_user(
            builder_id=builder.id, tenant_id=tenant.id, email=builder_user_email
        )

        headers = {
            "Authorization": f"Bearer {_make_clinic_admin_token(tenant_id=tenant.id)}",
            "Content-Type": "application/json",
        }
        resp = await async_client.post(
            "/v1/support/tickets",
            headers=headers,
            json={"tipo": "solicitacao", "mensagem": "route via support_email"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert len(smtp_capture.messages) == 1
        # Delivered to support_email, NOT the login user's email
        assert smtp_capture.messages[0]["To"] == support_email
        assert smtp_capture.messages[0]["To"] != builder_user_email

        row = await _fetch_ticket(data["ticket_id"])
        assert row is not None
        assert row.routed_to == "builder"
        assert row.routed_to_builder_id == builder.id

    @pytest.mark.asyncio
    async def test_builder_without_user_falls_back_to_central_even_with_support_email(
        self, async_client: AsyncClient, smtp_capture: _SmtpCapture
    ):
        """Group 2 guardrail: support_email is IGNORED when no role=builder user exists
        for the builder. This precondition was deliberately preserved."""
        support_email = f"orphan-{uuid4().hex[:6]}@builder.test"
        builder = await _seed_builder(status_="active", support_email=support_email)
        tenant = await _seed_tenant(builder_id=builder.id)
        # Deliberately no builder-role user

        headers = {
            "Authorization": f"Bearer {_make_clinic_admin_token(tenant_id=tenant.id)}",
            "Content-Type": "application/json",
        }
        resp = await async_client.post(
            "/v1/support/tickets",
            headers=headers,
            json={"tipo": "solicitacao", "mensagem": "support_email set but no user"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert len(smtp_capture.messages) == 1
        assert smtp_capture.messages[0]["To"] == settings.support_email_to
        assert smtp_capture.messages[0]["To"] != support_email

        row = await _fetch_ticket(data["ticket_id"])
        assert row is not None
        assert row.routed_to == "central"
        assert row.routed_to_builder_id is None

    @pytest.mark.asyncio
    async def test_suspended_builder_with_support_email_still_central(
        self, async_client: AsyncClient, smtp_capture: _SmtpCapture
    ):
        """Group 2 guardrail: inactive builder → central regardless of support_email."""
        support_email = f"suspended-support-{uuid4().hex[:6]}@builder.test"
        builder = await _seed_builder(status_="suspended", support_email=support_email)
        tenant = await _seed_tenant(builder_id=builder.id)
        await _seed_builder_user(
            builder_id=builder.id,
            tenant_id=tenant.id,
            email=f"suspended-user-{uuid4().hex[:6]}@builder.test",
        )

        headers = {
            "Authorization": f"Bearer {_make_clinic_admin_token(tenant_id=tenant.id)}",
            "Content-Type": "application/json",
        }
        resp = await async_client.post(
            "/v1/support/tickets",
            headers=headers,
            json={"tipo": "solicitacao", "mensagem": "suspended + support_email"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert len(smtp_capture.messages) == 1
        assert smtp_capture.messages[0]["To"] == settings.support_email_to
        assert smtp_capture.messages[0]["To"] != support_email

        row = await _fetch_ticket(data["ticket_id"])
        assert row is not None
        assert row.routed_to == "central"
        assert row.routed_to_builder_id is None

    @pytest.mark.asyncio
    async def test_financeiro_with_support_email_still_central(
        self, async_client: AsyncClient, smtp_capture: _SmtpCapture
    ):
        """Group 2 guardrail: financeiro tickets → central even if support_email set."""
        support_email = f"fin-support-{uuid4().hex[:6]}@builder.test"
        builder = await _seed_builder(status_="active", support_email=support_email)
        tenant = await _seed_tenant(builder_id=builder.id)
        await _seed_builder_user(
            builder_id=builder.id,
            tenant_id=tenant.id,
            email=f"fin-user-{uuid4().hex[:6]}@builder.test",
        )

        headers = {
            "Authorization": f"Bearer {_make_clinic_admin_token(tenant_id=tenant.id)}",
            "Content-Type": "application/json",
        }
        resp = await async_client.post(
            "/v1/support/tickets",
            headers=headers,
            json={"tipo": "financeiro", "mensagem": "invoice + support_email"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert len(smtp_capture.messages) == 1
        assert smtp_capture.messages[0]["To"] == settings.support_email_to
        assert smtp_capture.messages[0]["To"] != support_email

        row = await _fetch_ticket(data["ticket_id"])
        assert row is not None
        assert row.routed_to == "central"
        assert row.routed_to_builder_id is None

    @pytest.mark.asyncio
    async def test_financeiro_ticket_always_routes_to_central(
        self, async_client: AsyncClient, smtp_capture: _SmtpCapture
    ):
        builder = await _seed_builder(status_="active")
        tenant = await _seed_tenant(builder_id=builder.id)
        await _seed_builder_user(
            builder_id=builder.id,
            tenant_id=tenant.id,
            email=f"active-{uuid4().hex[:6]}@builder.test",
        )

        headers = {
            "Authorization": f"Bearer {_make_clinic_admin_token(tenant_id=tenant.id)}",
            "Content-Type": "application/json",
        }
        resp = await async_client.post(
            "/v1/support/tickets",
            headers=headers,
            json={"tipo": "financeiro", "mensagem": "invoice question"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert len(smtp_capture.messages) == 1
        assert smtp_capture.messages[0]["To"] == settings.support_email_to

        row = await _fetch_ticket(data["ticket_id"])
        assert row is not None
        assert row.routed_to == "central"
        assert row.routed_to_builder_id is None


class TestSupportTicketListExposesRoutedTo:
    """GET /v1/support/tickets (super_admin) exposes routed_to on each row."""

    @pytest.mark.asyncio
    async def test_list_returns_routed_to_field(
        self, async_client: AsyncClient, smtp_capture: _SmtpCapture
    ):
        builder = await _seed_builder(status_="active")
        tenant = await _seed_tenant(builder_id=builder.id)
        builder_user_email = f"list-{uuid4().hex[:6]}@builder.test"
        await _seed_builder_user(
            builder_id=builder.id, tenant_id=tenant.id, email=builder_user_email
        )

        create_headers = {
            "Authorization": f"Bearer {_make_clinic_admin_token(tenant_id=tenant.id)}",
            "Content-Type": "application/json",
        }
        create_resp = await async_client.post(
            "/v1/support/tickets",
            headers=create_headers,
            json={"tipo": "solicitacao", "mensagem": "listable"},
        )
        assert create_resp.status_code == 200
        ticket_id = create_resp.json()["ticket_id"]

        admin_headers = {
            "Authorization": f"Bearer {_make_super_admin_token()}",
            "Content-Type": "application/json",
        }
        list_resp = await async_client.get("/v1/support/tickets", headers=admin_headers)
        assert list_resp.status_code == 200, list_resp.text
        rows = list_resp.json()
        assert isinstance(rows, list)
        matched = [r for r in rows if r["id"] == ticket_id]
        assert len(matched) == 1
        assert matched[0]["routed_to"] == "builder"


# ============================================
# Admin-endpoint role smoke tests (regression)
# ============================================


class TestSupportAdminAuthEnforcement:
    """GET /v1/support/tickets and PATCH .../status are super_admin-only."""

    @pytest.mark.asyncio
    async def test_list_returns_403_for_clinic_admin(self, async_client: AsyncClient):
        headers = {
            "Authorization": f"Bearer {_make_clinic_admin_token(tenant_id=uuid4())}",
            "Content-Type": "application/json",
        }
        resp = await async_client.get("/v1/support/tickets", headers=headers)
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_list_returns_403_for_builder(self, async_client: AsyncClient):
        headers = {
            "Authorization": f"Bearer {_make_builder_token()}",
            "Content-Type": "application/json",
        }
        resp = await async_client.get("/v1/support/tickets", headers=headers)
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_list_returns_200_for_super_admin(self, async_client: AsyncClient):
        headers = {
            "Authorization": f"Bearer {_make_super_admin_token()}",
            "Content-Type": "application/json",
        }
        resp = await async_client.get("/v1/support/tickets", headers=headers)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_patch_status_returns_403_for_clinic_admin(
        self, async_client: AsyncClient
    ):
        headers = {
            "Authorization": f"Bearer {_make_clinic_admin_token(tenant_id=uuid4())}",
            "Content-Type": "application/json",
        }
        resp = await async_client.patch(
            "/v1/support/tickets/1/status", headers=headers, json={"status": "closed"}
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_patch_status_returns_403_for_builder(self, async_client: AsyncClient):
        headers = {
            "Authorization": f"Bearer {_make_builder_token()}",
            "Content-Type": "application/json",
        }
        resp = await async_client.patch(
            "/v1/support/tickets/1/status", headers=headers, json={"status": "closed"}
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_patch_status_returns_200_for_super_admin(
        self, async_client: AsyncClient, smtp_capture: _SmtpCapture
    ):
        # Seed a ticket to patch (route auth passes for super_admin → 200 on update)
        tenant = await _seed_tenant(builder_id=None)
        create_headers = {
            "Authorization": f"Bearer {_make_clinic_admin_token(tenant_id=tenant.id)}",
            "Content-Type": "application/json",
        }
        create_resp = await async_client.post(
            "/v1/support/tickets",
            headers=create_headers,
            json={"tipo": "solicitacao", "mensagem": "to-close"},
        )
        assert create_resp.status_code == 200
        ticket_id = create_resp.json()["ticket_id"]

        admin_headers = {
            "Authorization": f"Bearer {_make_super_admin_token()}",
            "Content-Type": "application/json",
        }
        resp = await async_client.patch(
            f"/v1/support/tickets/{ticket_id}/status",
            headers=admin_headers,
            json={"status": "closed"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["status"] == "closed"
