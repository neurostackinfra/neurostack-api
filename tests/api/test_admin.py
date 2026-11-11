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
_NEW_KEYS = {"subscription", "subscription_unavailable", "courtesy", "charging"}


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
            counts.append(len([s for s in statements if "credit_lots" in s]))
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
