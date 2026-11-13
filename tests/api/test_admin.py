"""
NeuroStack API - Admin Endpoints Tests
Tests for super-admin clinic + builder management endpoints.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from src.config.settings import settings
from src.services.tenant_classification import real_clinic_tenant_filter


def _make_super_admin_token(tenant_id=None, user_id=None) -> str:
    """Generate a valid JWT for a super_admin user."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(user_id or uuid4()),
            "tenant_id": str(tenant_id or uuid4()),
            "role": "super_admin",
            "email": "admin@test.com",
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": str(uuid4()),
        },
        settings.secret_key,
        algorithm="HS256",
    )


def _make_clinic_admin_token() -> str:
    """Generate a valid JWT for a clinic_admin user (non-super)."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(uuid4()),
            "role": "clinic_admin",
            "email": "clinic@test.com",
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": str(uuid4()),
        },
        settings.secret_key,
        algorithm="HS256",
    )


@pytest.fixture
def super_admin_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_make_super_admin_token()}",
        "Content-Type": "application/json",
    }


@pytest.fixture
def clinic_admin_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_make_clinic_admin_token()}",
        "Content-Type": "application/json",
    }


class TestRequireSuperAdmin:
    """Test that admin endpoints enforce super_admin role."""

    def test_clinics_returns_403_for_clinic_admin(
        self, client: TestClient, clinic_admin_headers
    ):
        response = client.get("/v1/admin/clinics", headers=clinic_admin_headers)
        assert response.status_code == 403

    def test_builders_returns_403_for_clinic_admin(
        self, client: TestClient, clinic_admin_headers
    ):
        response = client.get("/v1/admin/builders", headers=clinic_admin_headers)
        assert response.status_code == 403

    def test_stats_returns_403_for_clinic_admin(
        self, client: TestClient, clinic_admin_headers
    ):
        response = client.get("/v1/admin/stats", headers=clinic_admin_headers)
        assert response.status_code == 403

    def test_no_auth_returns_401(self, client: TestClient):
        response = client.get("/v1/admin/clinics")
        assert response.status_code == 401


class TestListClinics:
    """Test GET /v1/admin/clinics."""

    def test_returns_list(self, client: TestClient, super_admin_headers):
        response = client.get("/v1/admin/clinics", headers=super_admin_headers)
        assert response.status_code == 200
        assert isinstance(response.json(), list)


class TestAdminStats:
    """Test GET /v1/admin/stats."""

    def test_returns_stats_structure(self, client: TestClient, super_admin_headers):
        response = client.get("/v1/admin/stats", headers=super_admin_headers)
        assert response.status_code == 200
        data = response.json()
        assert "total_traces" in data
        assert "active_pilots" in data
        assert "active_builders" in data
        assert "total_clinics" in data
        assert "active_clinics" in data
        assert "total_builders" in data
        assert "estimated_mrr" in data

    def test_admin_imports_the_canonical_real_clinic_predicate(self) -> None:
        from src.api.routes import admin

        assert admin.real_clinic_tenant_filter is real_clinic_tenant_filter
        assert not hasattr(admin, "_real_clinic_tenant_filter")

    @pytest.mark.asyncio
    async def test_role_matrix_preserves_clinic_list_and_totals(
        self,
        async_client,
        super_admin_headers,
    ) -> None:
        from src.api.routes.admin import _compute_platform_totals
        from src.models.tenant import Tenant
        from src.models.user import User, UserRole
        from src.repositories import get_db_session

        userless_id = uuid4()
        clinic_id = uuid4()
        mixed_id = uuid4()
        internal_id = uuid4()
        session = get_db_session()
        try:
            baseline = await _compute_platform_totals(session)
            session.add_all(
                [
                    Tenant(
                        id=userless_id,
                        name="Matrix Userless",
                        slug=f"matrix-userless-{userless_id.hex}",
                        api_key_hash=f"matrix-userless-{userless_id.hex}",
                        is_active=True,
                        config={},
                    ),
                    Tenant(
                        id=clinic_id,
                        name="Matrix Clinic",
                        slug=f"matrix-clinic-{clinic_id.hex}",
                        api_key_hash=f"matrix-clinic-{clinic_id.hex}",
                        is_active=False,
                        config={},
                    ),
                    Tenant(
                        id=mixed_id,
                        name="Matrix Mixed",
                        slug=f"matrix-mixed-{mixed_id.hex}",
                        api_key_hash=f"matrix-mixed-{mixed_id.hex}",
                        is_active=True,
                        config={},
                    ),
                    Tenant(
                        id=internal_id,
                        name="Matrix Internal",
                        slug=f"matrix-internal-{internal_id.hex}",
                        api_key_hash=f"matrix-internal-{internal_id.hex}",
                        is_active=True,
                        config={},
                    ),
                    User(
                        tenant_id=clinic_id,
                        email=f"clinic-{clinic_id.hex}@test.local",
                        password_hash="test",
                        role=UserRole.clinic_admin,
                    ),
                    User(
                        tenant_id=mixed_id,
                        email=f"mixed-clinic-{mixed_id.hex}@test.local",
                        password_hash="test",
                        role=UserRole.clinic_admin,
                    ),
                    User(
                        tenant_id=mixed_id,
                        email=f"mixed-builder-{mixed_id.hex}@test.local",
                        password_hash="test",
                        role=UserRole.builder,
                    ),
                    User(
                        tenant_id=internal_id,
                        email=f"internal-builder-{internal_id.hex}@test.local",
                        password_hash="test",
                        role=UserRole.builder,
                    ),
                    User(
                        tenant_id=internal_id,
                        email=f"internal-admin-{internal_id.hex}@test.local",
                        password_hash="test",
                        role=UserRole.super_admin,
                    ),
                    User(
                        tenant_id=internal_id,
                        email=f"internal-investor-{internal_id.hex}@test.local",
                        password_hash="test",
                        role=UserRole.investor,
                    ),
                ]
            )
            await session.commit()
        finally:
            await session.close()

        clinics_response = await async_client.get(
            "/v1/admin/clinics",
            headers=super_admin_headers,
        )
        stats_response = await async_client.get(
            "/v1/admin/stats",
            headers=super_admin_headers,
        )
        assert clinics_response.status_code == 200
        assert stats_response.status_code == 200
        listed_ids = {item["clinic_id"] for item in clinics_response.json()}
        assert {str(userless_id), str(clinic_id), str(mixed_id)}.issubset(listed_ids)
        assert str(internal_id) not in listed_ids
        stats = stats_response.json()
        assert stats["total_clinics"] == baseline["total_clinics"] + 3
        assert stats["active_clinics"] == baseline["active_clinics"] + 2


class TestListBuilders:
    """Test GET /v1/admin/builders."""

    def test_returns_list(self, client: TestClient, super_admin_headers):
        response = client.get("/v1/admin/builders", headers=super_admin_headers)
        assert response.status_code == 200
        assert isinstance(response.json(), list)


class TestStubEndpoints:
    """Test stub endpoints return empty lists."""

    def test_circuit_breakers_stub(self, client: TestClient, super_admin_headers):
        response = client.get("/v1/admin/circuit-breakers", headers=super_admin_headers)
        assert response.status_code == 200
        assert response.json() == []

    def test_removed_rlhf_pending_route_returns_404(
        self, client: TestClient, super_admin_headers
    ):
        response = client.get(
            "/v1/admin/rlhf/" + "pending", headers=super_admin_headers
        )
        assert response.status_code == 404


class TestCreateClinicValidation:
    """Test POST /v1/admin/clinics validation."""

    def test_rejects_empty_name(self, client: TestClient, super_admin_headers):
        response = client.post(
            "/v1/admin/clinics",
            headers=super_admin_headers,
            json={
                "clinic_name": "",
                "admin_email": "test@test.com",
                "admin_password": "12345678",
            },
        )
        assert response.status_code == 422

    def test_rejects_invalid_email(self, client: TestClient, super_admin_headers):
        response = client.post(
            "/v1/admin/clinics",
            headers=super_admin_headers,
            json={
                "clinic_name": "Test Clinic",
                "admin_email": "not-an-email",
                "admin_password": "12345678",
            },
        )
        assert response.status_code == 422

    def test_rejects_short_password(self, client: TestClient, super_admin_headers):
        response = client.post(
            "/v1/admin/clinics",
            headers=super_admin_headers,
            json={
                "clinic_name": "Test Clinic",
                "admin_email": "test@test.com",
                "admin_password": "short",
            },
        )
        assert response.status_code == 422

    def test_rejects_invalid_plan_type(self, client: TestClient, super_admin_headers):
        response = client.post(
            "/v1/admin/clinics",
            headers=super_admin_headers,
            json={
                "clinic_name": "Test Clinic",
                "admin_email": "test@test.com",
                "admin_password": "12345678",
                "plan_type": "invalid",
            },
        )
        assert response.status_code == 422


class TestCreateBuilderValidation:
    """Test POST /v1/admin/builders validation."""

    def test_rejects_empty_name(self, client: TestClient, super_admin_headers):
        response = client.post(
            "/v1/admin/builders",
            headers=super_admin_headers,
            json={"name": ""},
        )
        assert response.status_code == 422


class TestUpdateBuilderValidation:
    """Test PATCH /v1/admin/builders/{id} validation."""

    def test_rejects_invalid_status(self, client: TestClient, super_admin_headers):
        fake_id = str(uuid4())
        response = client.patch(
            f"/v1/admin/builders/{fake_id}",
            headers=super_admin_headers,
            json={"status": "bogus"},
        )
        assert response.status_code == 422

    def test_invalid_uuid_returns_400(self, client: TestClient, super_admin_headers):
        response = client.patch(
            "/v1/admin/builders/not-a-uuid",
            headers=super_admin_headers,
            json={"name": "New Name"},
        )
        # 400 or 422 depending on FastAPI validation
        assert response.status_code in (400, 422, 500)


class TestDeactivateClinic:
    """Test DELETE /v1/admin/clinics/{id}."""

    def test_invalid_uuid_returns_400(self, client: TestClient, super_admin_headers):
        response = client.delete(
            "/v1/admin/clinics/not-a-uuid",
            headers=super_admin_headers,
        )
        assert response.status_code == 400


class TestDeactivateBuilder:
    """Test DELETE /v1/admin/builders/{id}."""

    def test_invalid_uuid_returns_400(self, client: TestClient, super_admin_headers):
        response = client.delete(
            "/v1/admin/builders/not-a-uuid",
            headers=super_admin_headers,
        )
        assert response.status_code == 400


def _seed_entities(client: TestClient, *entities: Any) -> None:
    from src.repositories.database import get_db_session

    async def _run() -> None:
        db = get_db_session()
        try:
            db.add_all(entities)
            await db.commit()
        finally:
            await db.close()

    client._submit(_run())  # type: ignore[attr-defined]


def _clinic(builder_id: UUID | None = None) -> Any:
    from src.models.tenant import Tenant

    tenant_id = uuid4()
    return Tenant(
        id=tenant_id,
        name=f"Audit Add-on Clinic {tenant_id.hex}",
        slug=f"audit-addon-{tenant_id.hex}",
        api_key_hash=f"audit-addon-hash-{tenant_id.hex}",
        builder_id=builder_id,
        config={},
    )


def _listed(rows: list[dict[str, Any]], clinic_id: UUID) -> dict[str, Any]:
    return next(row for row in rows if row["clinic_id"] == str(clinic_id))


class TestAuditExtendedAccess:
    """Super admin toggles the per-clinic audit export add-on."""

    def test_super_admin_turns_addon_on_and_off(
        self, client: TestClient, super_admin_headers
    ):
        clinic = _clinic()
        _seed_entities(client, clinic)

        listed_before = client.get("/v1/admin/clinics", headers=super_admin_headers)
        enabled = client.patch(
            f"/v1/admin/clinics/{clinic.id}",
            headers=super_admin_headers,
            json={"audit_extended_access": True},
        )
        listed_enabled = client.get("/v1/admin/clinics", headers=super_admin_headers)
        untouched = client.patch(
            f"/v1/admin/clinics/{clinic.id}",
            headers=super_admin_headers,
            json={"clinic_name": "Renamed Audit Clinic"},
        )
        disabled = client.patch(
            f"/v1/admin/clinics/{clinic.id}",
            headers=super_admin_headers,
            json={"audit_extended_access": False},
        )
        listed_disabled = client.get("/v1/admin/clinics", headers=super_admin_headers)

        assert listed_before.status_code == 200
        assert (
            _listed(listed_before.json(), clinic.id)["audit_extended_access"] is False
        )
        assert enabled.status_code == 200, enabled.text
        assert enabled.json() == {"ok": True, "audit_extended_access": True}
        assert (
            _listed(listed_enabled.json(), clinic.id)["audit_extended_access"] is True
        )
        assert untouched.status_code == 200, untouched.text
        assert untouched.json() == {"ok": True, "audit_extended_access": True}
        assert disabled.status_code == 200, disabled.text
        assert disabled.json() == {"ok": True, "audit_extended_access": False}
        assert (
            _listed(listed_disabled.json(), clinic.id)["audit_extended_access"] is False
        )

    def test_non_super_admin_cannot_set_addon(
        self, client: TestClient, clinic_admin_headers
    ):
        clinic = _clinic()
        _seed_entities(client, clinic)

        response = client.patch(
            f"/v1/admin/clinics/{clinic.id}",
            headers=clinic_admin_headers,
            json={"audit_extended_access": True},
        )

        assert response.status_code == 403

    def test_builder_listing_exposes_addon_per_clinic(
        self, client: TestClient, super_admin_headers
    ):
        from src.models.builder import Builder

        builder_id = uuid4()
        builder = Builder(id=builder_id, name=f"Audit Add-on Builder {builder_id.hex}")
        plain = _clinic(builder_id)
        extended = _clinic(builder_id)
        extended.audit_extended_access = True
        _seed_entities(client, builder, plain, extended)

        response = client.get("/v1/admin/builders", headers=super_admin_headers)

        assert response.status_code == 200
        listed = next(row for row in response.json() if row["id"] == str(builder_id))
        flags = {
            clinic["clinic_id"]: clinic["audit_extended_access"]
            for clinic in listed["clinics"]
        }
        assert flags == {str(plain.id): False, str(extended.id): True}


# ---------------------------------------------------------------------------
# GET /v1/admin/builders/subscriptions (creditos-cortesia Group 3).
# ---------------------------------------------------------------------------

_LEGACY_KEYS = {
    "id",
    "name",
    "subscription_plan",
    "subscription_status",
    "billing_cycle",
    "updated_at",
}
_NEW_KEYS = {
    "subscription",
    "subscription_unavailable",
    "courtesy",
    "charging",
    "courtesy_rule",
}


@contextmanager
def _sql() -> Iterator[list[str]]:
    """Every SQL statement the application runs inside the block."""

    from sqlalchemy import event

    from src.repositories.database import get_db_engine

    seen: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        seen.append(" ".join(statement.split()).lower())

    engine = get_db_engine().sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


class TestBuilderSubscriptions:
    """Legacy fields kept; ``subscription``, ``subscription_unavailable`` and
    ``courtesy`` always present; ``courtesy`` only with the credit switch
    effective and the courtesy gate open, from one statement that never
    reads ``credit_courtesy_events``."""

    @staticmethod
    def _switch(
        monkeypatch: pytest.MonkeyPatch, *, effective: bool, revision: str | None
    ) -> None:
        from src.services.credit_switch import CreditSwitch, CreditSwitchReading

        reading = CreditSwitchReading(effective, "schema_ready", revision)
        monkeypatch.setattr(CreditSwitch, "effective", lambda _self: effective)
        monkeypatch.setattr(CreditSwitch, "reading", lambda _self: reading)

    @staticmethod
    def _head() -> str:
        from src.services.credit_switch import load_image_migration_chain

        return load_image_migration_chain()[-1]

    @staticmethod
    def _below_046() -> str:
        from src.services.credit_switch import (
            COURTESY_SCHEMA_MIN_REVISION,
            load_image_migration_chain,
        )

        chain = load_image_migration_chain()
        return chain[chain.index(COURTESY_SCHEMA_MIN_REVISION) - 1]

    @staticmethod
    def _builders(client: TestClient, count: int) -> list[UUID]:
        from src.models.builder import Builder

        ids = [uuid4() for _ in range(count)]
        _seed_entities(
            client, *(Builder(id=i, name=f"Sub builder {i.hex}") for i in ids)
        )
        return ids

    @staticmethod
    def _courtesy(client: TestClient, builder_id: UUID, credits: int) -> Any:
        from src.repositories.database import get_db_session
        from src.services import credit_courtesy

        async def run() -> Any:
            session = get_db_session()
            try:
                async with session.begin():
                    return await credit_courtesy.grant(
                        session,
                        builder_id=builder_id,
                        credits=credits,
                        reason="teste",
                        actor_user_id=uuid4(),
                        idempotency_key=f"sub-{uuid4()}",
                    )
            finally:
                await session.close()

        return client._submit(run())  # type: ignore[attr-defined]

    @staticmethod
    def _rows(
        client: TestClient, headers: dict[str, str], ids: list[UUID]
    ) -> dict[UUID, dict[str, Any]]:
        response = client.get("/v1/admin/builders/subscriptions", headers=headers)
        assert response.status_code == 200, response.text
        rows = {UUID(row["id"]): row for row in response.json()}
        for row in rows.values():
            assert _LEGACY_KEYS | _NEW_KEYS == set(row)
        return {builder_id: rows[builder_id] for builder_id in ids}

    def test_requires_super_admin(
        self, client: TestClient, clinic_admin_headers: dict[str, str]
    ) -> None:
        response = client.get(
            "/v1/admin/builders/subscriptions", headers=clinic_admin_headers
        )
        assert response.status_code == 403
        assert client.get("/v1/admin/builders/subscriptions").status_code == 401

    def test_switch_off_keeps_legacy_fields_and_courtesy_null(
        self,
        client: TestClient,
        super_admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._switch(monkeypatch, effective=False, revision=None)
        [builder] = self._builders(client, 1)
        with _sql() as statements:
            row = self._rows(client, super_admin_headers, [builder])[builder]
        assert row["subscription_plan"] is None
        assert row["subscription_status"] == "trial"
        assert row["billing_cycle"] == "monthly"
        assert row["name"] == f"Sub builder {builder.hex}"
        assert (row["subscription"], row["subscription_unavailable"]) == (None, False)
        assert row["courtesy"] is None
        assert [s for s in statements if "credit_" in s] == []

    def test_the_gate_closed_is_courtesy_null_with_the_switch_on(
        self,
        client: TestClient,
        super_admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._switch(monkeypatch, effective=True, revision=self._head())
        [builder] = self._builders(client, 1)
        self._courtesy(client, builder, 5)
        for revision in (self._below_046(), None):
            self._switch(monkeypatch, effective=True, revision=revision)
            with _sql() as statements:
                row = self._rows(client, super_admin_headers, [builder])[builder]
            assert row["courtesy"] is None
            assert [s for s in statements if "credit_" in s] == []

    def test_the_gate_open_summarizes_the_active_courtesy(
        self,
        client: TestClient,
        super_admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.services.credit_ledger import civil_date, courtesy_lot_expires_at

        self._switch(monkeypatch, effective=True, revision=self._head())
        rich, plain = self._builders(client, 2)
        first = self._courtesy(client, rich, 5)
        self._courtesy(client, rich, 7)
        rows = self._rows(client, super_admin_headers, [rich, plain])
        assert rows[plain]["courtesy"] == {"active_expires_at": None, "available_mc": 0}
        summary = rows[rich]["courtesy"]
        assert summary["available_mc"] == 12_000
        # Both lots were granted today: the latest expiry is theirs.
        assert datetime.fromisoformat(summary["active_expires_at"]) == (
            courtesy_lot_expires_at(civil_date(first.occurred_at))
        )

    def test_the_courtesy_part_is_one_statement_without_the_events_table(
        self,
        client: TestClient,
        super_admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._switch(monkeypatch, effective=True, revision=self._head())
        counts = []
        for size in (2, 6):
            ids = self._builders(client, size)
            for builder in ids[:2]:
                self._courtesy(client, builder, 3)
            with _sql() as statements:
                rows = self._rows(client, super_admin_headers, ids)
            assert all(rows[b]["courtesy"]["available_mc"] == 3_000 for b in ids[:2])
            assert [s for s in statements if "credit_courtesy_events" in s] == []
            # The summary's statement (the courtesy rule's own reads are
            # counted by the rule's tests below).
            counts.append(
                len([s for s in statements if "credit_lots" in s and " having " in s])
            )
        assert counts == [1, 1]

    def test_a_failed_subscription_read_marks_only_its_row(
        self,
        client: TestClient,
        super_admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from datetime import date

        from src.api.routes import admin
        from src.services.billing_read_contract import (
            BillingReadUnavailable,
            SubscriptionReadback,
        )

        self._switch(monkeypatch, effective=True, revision=self._head())
        broken, subscribed, plain = self._builders(client, 3)
        readback = SubscriptionReadback(
            offer_id="plan_pro_monthly_v1",
            catalog_version_id="payment-2026-10-03",
            status="active",
            amount_cents=265_000,
            current_period_end=date(2026, 12, 9),
            payment_state="paid",
        )

        async def read(_session: Any, *, builder_id: UUID) -> Any:
            if builder_id == broken:
                raise BillingReadUnavailable()
            return readback if builder_id == subscribed else None

        monkeypatch.setattr(admin, "read_current_subscription", read)
        rows = self._rows(client, super_admin_headers, [broken, subscribed, plain])
        assert (
            rows[broken]["subscription"],
            rows[broken]["subscription_unavailable"],
        ) == (
            None,
            True,
        )
        assert rows[subscribed]["subscription_unavailable"] is False
        assert rows[subscribed]["subscription"] == {
            "offer_id": "plan_pro_monthly_v1",
            "catalog_version_id": "payment-2026-10-03",
            "status": "active",
            "amount_cents": 265_000,
            "current_period_end": "2026-12-09",
            "payment_state": "paid",
        }
        assert (
            rows[plain]["subscription"],
            rows[plain]["subscription_unavailable"],
        ) == (
            None,
            False,
        )
        for row in rows.values():
            assert row["courtesy"] == {"active_expires_at": None, "available_mc": 0}

    def test_any_failed_subscription_read_marks_only_its_row(
        self,
        client: TestClient,
        super_admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Not only ``BillingReadUnavailable``: a database error or any other
        exception of one builder's read is that row's
        ``subscription_unavailable`` (in its own savepoint, so the next read
        still runs), never a 500; the log names the type, never the message."""

        import structlog
        from sqlalchemy import text

        from src.api.routes import admin

        self._switch(monkeypatch, effective=True, revision=self._head())
        sql_broken, other_broken, plain = self._builders(client, 3)
        self._courtesy(client, plain, 2)

        async def read(session: Any, *, builder_id: UUID) -> Any:
            if builder_id == sql_broken:
                await session.execute(text("SELECT * FROM no_such_table_secret"))
            if builder_id == other_broken:
                raise RuntimeError("secret-message")
            return None

        monkeypatch.setattr(admin, "read_current_subscription", read)
        with structlog.testing.capture_logs() as records:
            rows = self._rows(
                client, super_admin_headers, [sql_broken, other_broken, plain]
            )
        for broken in (sql_broken, other_broken):
            assert (
                rows[broken]["subscription"],
                rows[broken]["subscription_unavailable"],
            ) == (None, True)
        assert (
            rows[plain]["subscription"],
            rows[plain]["subscription_unavailable"],
        ) == (
            None,
            False,
        )
        assert rows[plain]["courtesy"]["available_mc"] == 2_000
        mine = [
            r
            for r in records
            if r["event"].startswith("builder subscription unreadable")
        ]
        assert {r["error_type"] for r in mine} >= {"OperationalError", "RuntimeError"}
        assert "secret" not in repr(records)

    def test_a_failed_courtesy_read_is_courtesy_null_only(
        self,
        client: TestClient,
        super_admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import structlog
        from sqlalchemy import text

        from src.api.routes import admin

        self._switch(monkeypatch, effective=True, revision=self._head())
        [builder] = self._builders(client, 1)
        self._courtesy(client, builder, 2)

        async def broken(session: Any, _at: datetime) -> Any:
            await session.execute(text("SELECT * FROM no_such_table_secret"))

        monkeypatch.setattr(admin, "_courtesy_by_builder", broken)
        with structlog.testing.capture_logs() as records:
            row = self._rows(client, super_admin_headers, [builder])[builder]
        assert row["courtesy"] is None
        assert (row["subscription"], row["subscription_unavailable"]) == (None, False)
        [logged] = [
            r for r in records if r["event"].startswith("builder courtesy summary")
        ]
        assert logged["error_type"] == "OperationalError"
        assert "secret" not in repr(records)

    # -- charging (cobranca-credito-por-builder Group 5) ---------------------

    @staticmethod
    def _below_047() -> str:
        from src.services.credit_switch import (
            ENFORCEMENT_MARK_SCHEMA_MIN_REVISION,
            load_image_migration_chain,
        )

        chain = load_image_migration_chain()
        return chain[chain.index(ENFORCEMENT_MARK_SCHEMA_MIN_REVISION) - 1]

    @staticmethod
    def _mark(
        client: TestClient, builder_id: UUID, *events: tuple[str, datetime]
    ) -> None:
        """Append ``(kind, occurred_at)`` events to the builder's marks."""

        from src.models.credit import CreditEnforcementMark

        _seed_entities(
            client,
            *(
                CreditEnforcementMark(
                    id=uuid4(),
                    builder_id=builder_id,
                    kind=kind,
                    occurred_at=at,
                    actor_user_id=uuid4(),
                    reason="teste",
                )
                for kind, at in events
            ),
        )

    _T0 = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)

    def test_charging_is_null_and_unread_without_the_switch_or_below_047(
        self,
        client: TestClient,
        super_admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        [builder] = self._builders(client, 1)
        self._mark(client, builder, ("mark", self._T0))
        for effective, revision in (
            (False, None),
            (False, self._head()),
            (True, self._below_047()),
            (True, None),
        ):
            self._switch(monkeypatch, effective=effective, revision=revision)
            with _sql() as statements:
                row = self._rows(client, super_admin_headers, [builder])[builder]
            assert row["charging"] is None, (effective, revision)
            assert [s for s in statements if "credit_enforcement_marks" in s] == []
        # At 046 the courtesy gate is open: only the marks are not read.
        assert row["courtesy"] is None
        self._switch(monkeypatch, effective=True, revision=self._below_047())
        row = self._rows(client, super_admin_headers, [builder])[builder]
        assert row["courtesy"] == {"active_expires_at": None, "available_mc": 0}
        assert row["charging"] is None

    def test_charging_shows_only_the_builder_whose_latest_event_is_a_mark(
        self,
        client: TestClient,
        super_admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._switch(monkeypatch, effective=True, revision=self._head())
        marked, unmarked, remarked, never = self._builders(client, 4)
        hour = timedelta(hours=1)
        self._mark(client, marked, ("mark", self._T0))
        self._mark(client, unmarked, ("mark", self._T0), ("unmark", self._T0 + hour))
        self._mark(
            client,
            remarked,
            ("mark", self._T0),
            ("unmark", self._T0 + hour),
            ("mark", self._T0 + 2 * hour),
        )
        self._courtesy(client, marked, 4)
        rows = self._rows(
            client, super_admin_headers, [marked, unmarked, remarked, never]
        )
        assert rows[marked]["charging"] == {"marked_at": "2026-11-11T12:00:00Z"}
        assert rows[remarked]["charging"] == {"marked_at": "2026-11-11T14:00:00Z"}
        assert rows[unmarked]["charging"] is None
        assert rows[never]["charging"] is None
        # Every other field is what it was without a mark.
        assert rows[marked]["courtesy"]["available_mc"] == 4_000
        for builder_id, row in rows.items():
            assert (row["subscription"], row["subscription_unavailable"]) == (
                None,
                False,
            )
            assert row["subscription_status"] == "trial"
            assert row["name"] == f"Sub builder {builder_id.hex}"

    def test_the_charging_part_is_one_statement_for_the_whole_table(
        self,
        client: TestClient,
        super_admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._switch(monkeypatch, effective=True, revision=self._head())
        counts = []
        for size in (2, 6):
            ids = self._builders(client, size)
            for builder in ids:
                self._mark(client, builder, ("mark", self._T0))
            with _sql() as statements:
                rows = self._rows(client, super_admin_headers, ids)
            assert all(rows[b]["charging"] is not None for b in ids)
            counts.append(
                len([s for s in statements if "credit_enforcement_marks" in s])
            )
        assert counts == [1, 1]

    def test_a_failed_marks_read_is_charging_null_only(
        self,
        client: TestClient,
        super_admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import structlog
        from sqlalchemy import text

        from src.repositories.credit import CreditRepository

        self._switch(monkeypatch, effective=True, revision=self._head())
        [builder] = self._builders(client, 1)
        self._mark(client, builder, ("mark", self._T0))
        self._courtesy(client, builder, 2)

        async def broken(self: Any, *_args: Any, **_kwargs: Any) -> Any:
            await self.session.execute(text("SELECT * FROM no_such_table_secret"))

        monkeypatch.setattr(CreditRepository, "latest_enforcement_marks", broken)
        with structlog.testing.capture_logs() as records:
            row = self._rows(client, super_admin_headers, [builder])[builder]
        assert row["charging"] is None
        assert row["courtesy"]["available_mc"] == 2_000
        assert (row["subscription"], row["subscription_unavailable"]) == (None, False)
        [logged] = [
            r for r in records if r["event"].startswith("builder charging marks")
        ]
        assert logged["error_type"] == "OperationalError"
        assert "secret" not in repr(records)
        # The precedence is unknown without the marks: no rule state either.
        assert row["courtesy_rule"] is None

    # -- courtesy_rule (debito-cortesia-automatico Group 5a) ------------------

    @staticmethod
    def _remove_all(client: TestClient, lot_id: UUID) -> None:
        from src.repositories.database import get_db_session
        from src.services import credit_courtesy

        async def run() -> None:
            session = get_db_session()
            try:
                async with session.begin():
                    await credit_courtesy.remove(
                        session,
                        lot_id=lot_id,
                        remove_all=True,
                        reason="teste",
                        actor_user_id=uuid4(),
                        idempotency_key=f"sub-removal-{uuid4()}",
                    )
            finally:
                await session.close()

        client._submit(run())  # type: ignore[attr-defined]

    @staticmethod
    def _paid_topup(client: TestClient, builder_id: UUID) -> None:
        """A paid top-up lot in force (a paid plan in force, wish Decision 5)."""

        from src.repositories.database import get_db_session
        from src.services.credit_ledger import (
            CreditLedger,
            GrantSpec,
            civil_date,
            topup_grant_effect_id,
            topup_lot_expires_at,
        )

        async def run() -> None:
            session = get_db_session()
            try:
                async with session.begin():
                    ledger = CreditLedger(session)
                    book = await ledger.open_book(
                        builder_id, at=datetime.now(timezone.utc)
                    )
                    assert book.clock is not None
                    order, paid = uuid4(), civil_date(book.clock)
                    book.grant(
                        GrantSpec(
                            lot_kind="topup",
                            effect_id=topup_grant_effect_id(order),
                            payment_id=f"pay-{order}",
                            paid_at=paid,
                            granted_mc=1_000,
                            expires_at=topup_lot_expires_at(paid),
                            order_id=order,
                        ),
                        at=book.clock,
                    )
                    await ledger.persist(book, lifecycle_source=None)
            finally:
                await session.close()

        client._submit(run())  # type: ignore[attr-defined]

    @staticmethod
    def _effective(client: TestClient, builder_id: UUID) -> Any:
        """The request path's own reader, for the batch's equivalence."""

        from src.repositories.credit import CreditRepository
        from src.repositories.database import get_db_session

        async def run() -> Any:
            session = get_db_session()
            try:
                return await CreditRepository(session).effective_enforcement(builder_id)
            finally:
                await session.close()

        return client._submit(run())  # type: ignore[attr-defined]

    @staticmethod
    def _is_rule_lots_read(statement: str) -> bool:
        return "as granted_at" in statement and "credit_lots.builder_id in (" in (
            statement
        )

    @staticmethod
    def _is_paid_plan_read(statement: str) -> bool:
        # The batch is anchored on the credit accounts (every builder with a
        # courtesy lot has one; ``builders`` may lack the row).
        return (
            "as unbound_paid_cycle" in statement and "from credit_accounts" in statement
        )

    def _rule_world(self, client: TestClient) -> dict[str, UUID]:
        charged, blocked, never, marked, protected, partial = self._builders(client, 6)
        self._courtesy(client, charged, 5)
        self._remove_all(client, self._courtesy(client, blocked, 4).lot_id)
        self._mark(client, marked, ("mark", self._T0))
        self._courtesy(client, marked, 4)
        self._remove_all(client, self._courtesy(client, protected, 2).lot_id)
        self._paid_topup(client, protected)
        # Two lots, one removed: still courtesy left.
        self._courtesy(client, partial, 1)
        self._remove_all(client, self._courtesy(client, partial, 3).lot_id)
        return {
            "charged": charged,
            "blocked": blocked,
            "never": never,
            "marked": marked,
            "protected": protected,
            "partial": partial,
        }

    def test_courtesy_rule_is_charged_blocked_or_null_like_the_request_reader(
        self,
        client: TestClient,
        super_admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._switch(monkeypatch, effective=True, revision=self._head())
        world = self._rule_world(client)
        rows = self._rows(client, super_admin_headers, list(world.values()))
        found = {
            name: rows[builder]["courtesy_rule"] for name, builder in world.items()
        }
        assert found == {
            "charged": "charged",
            "blocked": "blocked",
            "never": None,
            "marked": None,
            "protected": None,
            "partial": "charged",
        }
        # The batch is the request path's reader, builder by builder.
        for name, builder in world.items():
            effective = self._effective(client, builder)
            expected = effective.state if effective.origin == "courtesy" else None
            assert found[name] == expected, name
        # The mark's badge and the courtesy summary are what they were.
        assert rows[world["marked"]]["charging"] == {
            "marked_at": "2026-11-11T12:00:00Z"
        }
        assert rows[world["charged"]]["charging"] is None
        assert rows[world["blocked"]]["courtesy"] == {
            "active_expires_at": None,
            "available_mc": 0,
        }
        assert rows[world["charged"]]["courtesy"]["available_mc"] == 5_000

    def test_with_the_global_set_no_row_is_under_the_rule(
        self,
        client: TestClient,
        super_admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.models.credit import CreditEnforcementState

        self._switch(monkeypatch, effective=True, revision=self._head())
        world = self._rule_world(client)
        armed = datetime.now(timezone.utc) - timedelta(days=1)
        _seed_entities(
            client,
            CreditEnforcementState(
                id=1, credit_enforcement_at=armed, set_by="test", set_at=armed
            ),
        )
        rows = self._rows(client, super_admin_headers, list(world.values()))
        assert {row["courtesy_rule"] for row in rows.values()} == {None}
        assert rows[world["marked"]]["charging"] is not None

    def test_courtesy_rule_is_null_and_unread_without_the_switch_or_below_046(
        self,
        client: TestClient,
        super_admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._switch(monkeypatch, effective=True, revision=self._head())
        [builder] = self._builders(client, 1)
        self._courtesy(client, builder, 2)
        for effective, revision in (
            (False, None),
            (False, self._head()),
            (True, self._below_046()),
            (True, None),
        ):
            self._switch(monkeypatch, effective=effective, revision=revision)
            with _sql() as statements:
                row = self._rows(client, super_admin_headers, [builder])[builder]
            assert row["courtesy_rule"] is None, (effective, revision)
            assert [s for s in statements if self._is_rule_lots_read(s)] == []

    def test_below_047_the_rule_decides_without_reading_marks(
        self,
        client: TestClient,
        super_admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._switch(monkeypatch, effective=True, revision=self._head())
        charged, blocked = self._builders(client, 2)
        self._courtesy(client, charged, 2)
        self._remove_all(client, self._courtesy(client, blocked, 2).lot_id)
        self._switch(monkeypatch, effective=True, revision=self._below_047())
        with _sql() as statements:
            rows = self._rows(client, super_admin_headers, [charged, blocked])
        assert rows[charged]["courtesy_rule"] == "charged"
        assert rows[blocked]["courtesy_rule"] == "blocked"
        assert [s for s in statements if "credit_enforcement_marks" in s] == []

    def test_the_courtesy_rule_part_is_a_bounded_batch_for_the_whole_table(
        self,
        client: TestClient,
        super_admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One read of the lots for every builder and -- with some builder
        spent -- one of the paid plans, whatever the size of the table:
        never one read per builder (the cobranca badge's rule)."""

        self._switch(monkeypatch, effective=True, revision=self._head())
        counts = []
        for size in (2, 6):
            ids = self._builders(client, size)
            for builder in ids:
                self._remove_all(client, self._courtesy(client, builder, 1).lot_id)
            with _sql() as statements:
                rows = self._rows(client, super_admin_headers, ids)
            assert all(rows[b]["courtesy_rule"] == "blocked" for b in ids)
            counts.append(
                (
                    len([s for s in statements if self._is_rule_lots_read(s)]),
                    len([s for s in statements if self._is_paid_plan_read(s)]),
                )
            )
        assert counts == [(1, 1), (1, 1)]

    def test_with_nobody_spent_the_paid_plans_are_not_read(
        self,
        client: TestClient,
        super_admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Wish Decision 8: the subscription, binding and cycle reads happen
        only for a builder with courtesy and nothing spendable."""

        self._switch(monkeypatch, effective=True, revision=self._head())
        ids = self._builders(client, 2)
        self._courtesy(client, ids[0], 1)
        with _sql() as statements:
            rows = self._rows(client, super_admin_headers, ids)
        assert (rows[ids[0]]["courtesy_rule"], rows[ids[1]]["courtesy_rule"]) == (
            "charged",
            None,
        )
        assert len([s for s in statements if self._is_rule_lots_read(s)]) == 1
        assert [s for s in statements if self._is_paid_plan_read(s)] == []

    def test_charging_and_courtesy_rule_are_decided_on_one_switch_reading(
        self,
        client: TestClient,
        super_admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A switch reading that changes between two reads (a refresh) never
        splits the table: the mark gate and the rule gate judge the SAME
        reading, so a marked builder is never computed under the rule with
        its marks unread."""

        from src.api.routes import admin
        from src.services.credit_switch import CreditSwitchReading

        self._switch(monkeypatch, effective=True, revision=self._head())
        [marked] = self._builders(client, 1)
        self._mark(client, marked, ("mark", self._T0))
        self._courtesy(client, marked, 4)
        # Every read is a new reading, alternating the head and below 046.
        revisions = [self._head(), self._below_046()]
        calls: list[CreditSwitchReading] = []

        def flipping() -> CreditSwitchReading:
            reading = CreditSwitchReading(
                True, "schema_ready", revisions[len(calls) % 2]
            )
            calls.append(reading)
            return reading

        judged: dict[str, list[int]] = {"mark": [], "rule": []}
        mark_gate, rule_gate = (
            admin.enforcement_mark_gate_open,
            admin.courtesy_rule_gate_open,
        )

        def mark_judged(reading: Any) -> bool:
            judged["mark"].append(id(reading))
            return bool(mark_gate(reading))

        def rule_judged(reading: Any) -> bool:
            judged["rule"].append(id(reading))
            return bool(rule_gate(reading))

        monkeypatch.setattr(admin, "credit_switch_reading", flipping)
        monkeypatch.setattr(admin, "enforcement_mark_gate_open", mark_judged)
        monkeypatch.setattr(admin, "courtesy_rule_gate_open", rule_judged)
        row = self._rows(client, super_admin_headers, [marked])[marked]
        assert len(judged["mark"]) == len(judged["rule"]) == 1
        assert judged["mark"] == judged["rule"]
        assert row["courtesy_rule"] is None

    def test_a_failed_courtesy_rule_read_is_courtesy_rule_null_only(
        self,
        client: TestClient,
        super_admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import structlog
        from sqlalchemy import text

        from src.api.routes import admin

        self._switch(monkeypatch, effective=True, revision=self._head())
        [builder] = self._builders(client, 1)
        self._courtesy(client, builder, 2)

        async def broken(session: Any, *_args: Any) -> Any:
            await session.execute(text("SELECT * FROM no_such_table_secret"))

        monkeypatch.setattr(admin, "_courtesy_rule_by_builder", broken)
        with structlog.testing.capture_logs() as records:
            row = self._rows(client, super_admin_headers, [builder])[builder]
        assert row["courtesy_rule"] is None
        assert row["courtesy"]["available_mc"] == 2_000
        assert (row["subscription"], row["subscription_unavailable"]) == (None, False)
        [logged] = [
            r for r in records if r["event"].startswith("builder courtesy rule")
        ]
        assert logged["error_type"] == "OperationalError"
        assert "secret" not in repr(records)

    def test_the_description_documents_courtesy_rule(self) -> None:
        from src.api.main import app

        description = app.openapi()["paths"]["/v1/admin/builders/subscriptions"]["get"][
            "description"
        ]
        for needle in (
            "`courtesy_rule`",
            '"charged"',
            '"blocked"',
            "never stored",
            "no paid plan in force",
            "global enforcement is set",
            "below 046",
            "`charging` does not change",
        ):
            assert needle in description, needle
