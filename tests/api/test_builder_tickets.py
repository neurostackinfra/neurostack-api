"""
NeuroStack API - Builder Support Tickets Endpoints Tests (Group 3)

Covers `GET /v1/builder/tickets` and `PATCH /v1/builder/tickets/{id}/status`:
scope isolation by JWT `builder_id`, tenant join for `clinic_name` /
`contact_phone`, status filter, ordering, validation, and role guards.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
import pytest

from src.config.settings import settings
from tests.api.test_builder import _make_builder_token, _make_clinic_admin_token

# ============================================
# Local helpers
# ============================================


def _make_builder_token_without_builder_id(tenant_id=None, user_id=None) -> str:
    """Forge a JWT with role=builder but WITHOUT the `builder_id` claim.

    `require_builder` must reject this with 403 even though the role is right.
    The public `_make_builder_token` helper always fills in a `builder_id`
    (falling back to `uuid4()`), so we can't reuse it for this case.
    """
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(user_id or uuid4()),
            "tenant_id": str(tenant_id or uuid4()),
            "role": "builder",
            "email": "builder-nobid@test.com",
            # NOTE: no "builder_id" claim on purpose
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": str(uuid4()),
        },
        settings.secret_key,
        algorithm="HS256",
    )


def _headers_for_builder(builder_id) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_make_builder_token(builder_id=builder_id)}",
        "Content-Type": "application/json",
    }


# ============================================
# Seed helpers (mirrors TestBuilderClinicContactPhone pattern)
# ============================================


async def _seed_builder():
    from src.models.builder import Builder
    from src.repositories import get_db_session

    session = get_db_session()
    try:
        builder = Builder(
            id=uuid4(),
            name=f"Builder {uuid4().hex[:6]}",
            status="active",
            is_active=True,
            billing_cycle="monthly",
            subscription_status="trial",
        )
        session.add(builder)
        await session.commit()
        await session.refresh(builder)
        return builder
    finally:
        await session.close()


async def _seed_tenant(builder_id, contact_phone=None, name=None):
    from src.models.tenant import Tenant
    from src.repositories import get_db_session

    session = get_db_session()
    try:
        tenant = Tenant(
            id=uuid4(),
            name=name or f"Clinic {uuid4().hex[:6]}",
            slug=f"clinic-{uuid4().hex[:8]}",
            api_key_hash=f"test-hash-{uuid4().hex}",
            is_active=True,
            plan_type="clinic",
            builder_id=builder_id,
            contact_phone=contact_phone,
            config={},
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant
    finally:
        await session.close()


async def _seed_ticket(
    tenant_id,
    routed_to_builder_id,
    status="open",
    created_at=None,
    tipo="bug",
    mensagem="Preciso de ajuda",
    email="user@clinic.example",
    clinic_name=None,
):
    """Insert a SupportTicket row and return the persisted instance.

    `created_at` is set explicitly so ordering assertions are deterministic
    (SQLite `func.now()` is second-resolution and ties collapse otherwise).
    """
    from src.models.support_ticket import SupportTicket
    from src.repositories import get_db_session

    session = get_db_session()
    try:
        ticket = SupportTicket(
            tenant_id=tenant_id,
            clinic_name=clinic_name,
            email=email,
            tipo=tipo,
            mensagem=mensagem,
            status=status,
            routed_to="builder" if routed_to_builder_id is not None else "central",
            routed_to_builder_id=routed_to_builder_id,
            created_at=created_at or datetime.now(timezone.utc),
        )
        session.add(ticket)
        await session.commit()
        await session.refresh(ticket)
        return ticket
    finally:
        await session.close()


async def _fetch_ticket(ticket_id: int):
    """Re-read a ticket in a FRESH session (bypasses any identity-map caching)."""
    from sqlalchemy import select

    from src.models.support_ticket import SupportTicket
    from src.repositories import get_db_session

    session = get_db_session()
    try:
        result = await session.execute(
            select(SupportTicket).where(SupportTicket.id == ticket_id)
        )
        return result.scalar_one_or_none()
    finally:
        await session.close()


# ============================================
# GET /v1/builder/tickets — positive & isolation
# ============================================


class TestListBuilderTickets:
    @pytest.mark.asyncio
    async def test_lists_only_own_tickets_with_joined_clinic_fields(self, async_client):
        """Two tickets routed to A + one to B: A sees exactly its two, with
        clinic_name and contact_phone joined from Tenant."""
        builder_a = await _seed_builder()
        builder_b = await _seed_builder()

        tenant_a = await _seed_tenant(
            builder_a.id, contact_phone="11988887777", name="Clinic A"
        )
        tenant_b = await _seed_tenant(
            builder_b.id, contact_phone="11977776666", name="Clinic B"
        )

        base = datetime.now(timezone.utc)
        ticket_a1 = await _seed_ticket(
            tenant_a.id,
            routed_to_builder_id=builder_a.id,
            created_at=base - timedelta(minutes=10),
        )
        ticket_a2 = await _seed_ticket(
            tenant_a.id,
            routed_to_builder_id=builder_a.id,
            created_at=base - timedelta(minutes=5),
        )
        await _seed_ticket(
            tenant_b.id,
            routed_to_builder_id=builder_b.id,
            created_at=base - timedelta(minutes=1),
        )

        resp = await async_client.get(
            "/v1/builder/tickets",
            headers=_headers_for_builder(builder_a.id),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert isinstance(body, list)
        returned_ids = {row["id"] for row in body}
        assert returned_ids == {ticket_a1.id, ticket_a2.id}

        for row in body:
            assert row["clinic_name"] == "Clinic A"
            assert row["contact_phone"] == "11988887777"
            assert row["email"] == "user@clinic.example"
            assert row["status"] == "open"
            assert row["tipo"] == "bug"
            assert row["mensagem"] == "Preciso de ajuda"

    @pytest.mark.asyncio
    async def test_builder_with_no_tickets_returns_200_empty_list(self, async_client):
        """Builder A with zero routed tickets gets 200 + `[]`, never 403."""
        builder_a = await _seed_builder()
        # Seed a ticket for someone else so the table isn't empty
        builder_b = await _seed_builder()
        tenant_b = await _seed_tenant(builder_b.id, contact_phone="11900000000")
        await _seed_ticket(tenant_b.id, routed_to_builder_id=builder_b.id)

        resp = await async_client.get(
            "/v1/builder/tickets",
            headers=_headers_for_builder(builder_a.id),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_excludes_other_builder_and_central_routed_tickets(
        self, async_client
    ):
        """A must never see B's tickets nor central-routed (NULL) tickets."""
        builder_a = await _seed_builder()
        builder_b = await _seed_builder()

        tenant_a = await _seed_tenant(builder_a.id, contact_phone="11988887777")
        tenant_b = await _seed_tenant(builder_b.id, contact_phone="11977776666")
        tenant_central = await _seed_tenant(builder_a.id, contact_phone="11966665555")

        own = await _seed_ticket(tenant_a.id, routed_to_builder_id=builder_a.id)
        other = await _seed_ticket(tenant_b.id, routed_to_builder_id=builder_b.id)
        central = await _seed_ticket(tenant_central.id, routed_to_builder_id=None)

        resp = await async_client.get(
            "/v1/builder/tickets",
            headers=_headers_for_builder(builder_a.id),
        )
        assert resp.status_code == 200, resp.text
        returned_ids = {row["id"] for row in resp.json()}
        assert own.id in returned_ids
        assert other.id not in returned_ids
        assert central.id not in returned_ids

    @pytest.mark.asyncio
    async def test_status_filter_open_and_closed(self, async_client):
        """`?status=open` and `?status=closed` isolate exactly one; no filter
        returns both."""
        builder_a = await _seed_builder()
        tenant_a = await _seed_tenant(builder_a.id, contact_phone="11988887777")

        base = datetime.now(timezone.utc)
        open_ticket = await _seed_ticket(
            tenant_a.id,
            routed_to_builder_id=builder_a.id,
            status="open",
            created_at=base - timedelta(minutes=2),
        )
        closed_ticket = await _seed_ticket(
            tenant_a.id,
            routed_to_builder_id=builder_a.id,
            status="closed",
            created_at=base - timedelta(minutes=1),
        )

        headers = _headers_for_builder(builder_a.id)

        resp_open = await async_client.get(
            "/v1/builder/tickets?status=open", headers=headers
        )
        assert resp_open.status_code == 200, resp_open.text
        ids_open = {row["id"] for row in resp_open.json()}
        assert ids_open == {open_ticket.id}

        resp_closed = await async_client.get(
            "/v1/builder/tickets?status=closed", headers=headers
        )
        assert resp_closed.status_code == 200, resp_closed.text
        ids_closed = {row["id"] for row in resp_closed.json()}
        assert ids_closed == {closed_ticket.id}

        resp_all = await async_client.get("/v1/builder/tickets", headers=headers)
        assert resp_all.status_code == 200, resp_all.text
        ids_all = {row["id"] for row in resp_all.json()}
        assert ids_all == {open_ticket.id, closed_ticket.id}

    @pytest.mark.asyncio
    async def test_list_ordered_by_created_at_desc(self, async_client):
        """Newest-first ordering, using distinct explicit timestamps."""
        builder_a = await _seed_builder()
        tenant_a = await _seed_tenant(builder_a.id, contact_phone="11988887777")

        base = datetime.now(timezone.utc)
        older = await _seed_ticket(
            tenant_a.id,
            routed_to_builder_id=builder_a.id,
            created_at=base - timedelta(hours=2),
        )
        middle = await _seed_ticket(
            tenant_a.id,
            routed_to_builder_id=builder_a.id,
            created_at=base - timedelta(hours=1),
        )
        newest = await _seed_ticket(
            tenant_a.id,
            routed_to_builder_id=builder_a.id,
            created_at=base,
        )

        resp = await async_client.get(
            "/v1/builder/tickets",
            headers=_headers_for_builder(builder_a.id),
        )
        assert resp.status_code == 200, resp.text
        ordered_ids = [row["id"] for row in resp.json()]
        assert ordered_ids == [newest.id, middle.id, older.id]

    @pytest.mark.asyncio
    async def test_list_invalid_status_filter_returns_400(self, async_client):
        builder_a = await _seed_builder()
        resp = await async_client.get(
            "/v1/builder/tickets?status=pending",
            headers=_headers_for_builder(builder_a.id),
        )
        assert resp.status_code == 400, resp.text


# ============================================
# PATCH /v1/builder/tickets/{id}/status — positive, isolation, validation
# ============================================


class TestUpdateBuilderTicketStatus:
    @pytest.mark.asyncio
    async def test_patch_own_ticket_open_to_closed_and_back(self, async_client):
        """Round-trip status update; re-read via fresh session both times."""
        builder_a = await _seed_builder()
        tenant_a = await _seed_tenant(builder_a.id, contact_phone="11988887777")
        ticket = await _seed_ticket(
            tenant_a.id, routed_to_builder_id=builder_a.id, status="open"
        )

        headers = _headers_for_builder(builder_a.id)

        # open -> closed
        resp = await async_client.patch(
            f"/v1/builder/tickets/{ticket.id}/status",
            headers=headers,
            json={"status": "closed"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "closed"

        row = await _fetch_ticket(ticket.id)
        assert row is not None
        assert row.status == "closed"

        # closed -> open
        resp = await async_client.patch(
            f"/v1/builder/tickets/{ticket.id}/status",
            headers=headers,
            json={"status": "open"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "open"

        row = await _fetch_ticket(ticket.id)
        assert row is not None
        assert row.status == "open"

    @pytest.mark.asyncio
    async def test_patch_other_builders_ticket_returns_404_and_leaves_row(
        self, async_client
    ):
        """A patching B's ticket: 404, and B's ticket status untouched in DB."""
        builder_a = await _seed_builder()
        builder_b = await _seed_builder()
        tenant_b = await _seed_tenant(builder_b.id, contact_phone="11977776666")
        ticket_b = await _seed_ticket(
            tenant_b.id, routed_to_builder_id=builder_b.id, status="open"
        )

        resp = await async_client.patch(
            f"/v1/builder/tickets/{ticket_b.id}/status",
            headers=_headers_for_builder(builder_a.id),
            json={"status": "closed"},
        )
        assert resp.status_code == 404, resp.text

        row = await _fetch_ticket(ticket_b.id)
        assert row is not None
        assert row.status == "open"

    @pytest.mark.asyncio
    async def test_patch_central_routed_ticket_returns_404(self, async_client):
        """Ticket with NULL routed_to_builder_id must be untouchable by any builder."""
        builder_a = await _seed_builder()
        tenant_a = await _seed_tenant(builder_a.id, contact_phone="11988887777")
        ticket = await _seed_ticket(
            tenant_a.id, routed_to_builder_id=None, status="open"
        )

        resp = await async_client.patch(
            f"/v1/builder/tickets/{ticket.id}/status",
            headers=_headers_for_builder(builder_a.id),
            json={"status": "closed"},
        )
        assert resp.status_code == 404, resp.text

        row = await _fetch_ticket(ticket.id)
        assert row is not None
        assert row.status == "open"

    @pytest.mark.asyncio
    async def test_patch_invalid_status_returns_400(self, async_client):
        builder_a = await _seed_builder()
        tenant_a = await _seed_tenant(builder_a.id, contact_phone="11988887777")
        ticket = await _seed_ticket(
            tenant_a.id, routed_to_builder_id=builder_a.id, status="open"
        )

        resp = await async_client.patch(
            f"/v1/builder/tickets/{ticket.id}/status",
            headers=_headers_for_builder(builder_a.id),
            json={"status": "pending"},
        )
        assert resp.status_code == 400, resp.text

        # DB unchanged
        row = await _fetch_ticket(ticket.id)
        assert row is not None
        assert row.status == "open"


# ============================================
# Role / claim guards (403 on both endpoints)
# ============================================


class TestBuilderTicketsRoleGuards:
    @pytest.mark.asyncio
    async def test_clinic_admin_forbidden_on_list(self, async_client):
        resp = await async_client.get(
            "/v1/builder/tickets",
            headers={
                "Authorization": f"Bearer {_make_clinic_admin_token()}",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 403, resp.text

    @pytest.mark.asyncio
    async def test_clinic_admin_forbidden_on_patch(self, async_client):
        # Ticket id is arbitrary — auth must fail before the lookup runs.
        resp = await async_client.patch(
            "/v1/builder/tickets/1/status",
            headers={
                "Authorization": f"Bearer {_make_clinic_admin_token()}",
                "Content-Type": "application/json",
            },
            json={"status": "closed"},
        )
        assert resp.status_code == 403, resp.text

    @pytest.mark.asyncio
    async def test_builder_role_missing_builder_id_forbidden_on_list(
        self, async_client
    ):
        resp = await async_client.get(
            "/v1/builder/tickets",
            headers={
                "Authorization": (f"Bearer {_make_builder_token_without_builder_id()}"),
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 403, resp.text

    @pytest.mark.asyncio
    async def test_builder_role_missing_builder_id_forbidden_on_patch(
        self, async_client
    ):
        resp = await async_client.patch(
            "/v1/builder/tickets/1/status",
            headers={
                "Authorization": (f"Bearer {_make_builder_token_without_builder_id()}"),
                "Content-Type": "application/json",
            },
            json={"status": "closed"},
        )
        assert resp.status_code == 403, resp.text
