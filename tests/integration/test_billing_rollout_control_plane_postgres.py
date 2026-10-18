"""Real PostgreSQL measurement of the billing rollout control plane.

Every step goes through the public admin routes (``/v1/admin/billing/...``)
with the real services, repository SQL, triggers and constraints.  Only
authentication is replaced: ``require_super_admin_jwt`` is overridden by a
header-selected super-admin principal, exactly one per operator duty.  The
few probes that cannot be expressed through a route (an injected clock, a
row written past the service, a raw constraint error) say so in their name.

What these tests pin (measured, not assumed):

* ``disabled -> canary_allowlist`` is reachable with one operation, three
  self-assignments and three backend-signed receipts from three distinct
  actors; the quorum signs its builder, so a front-run with another builder
  is refused.
* ``canary_allowlist -> general`` is reachable with the documented
  procedure: one ``canary_allowlist -> general`` operation, its three-actor
  quorum, four readiness rows (readiness, backlog, provisioning, smoke) that
  name that quorum's deployer and reviewer receipts, then the transition.
  Recording readiness does not spend the quorum; the ``general`` transition
  spends exactly the receipts its readiness rows name, once.
* Readiness the transition could not use is refused before insert: a
  material other than ``canary_allowlist -> general``, a stored state other
  than ``canary_allowlist``, an incomplete/extra quorum, receipts outside the
  quorum, a second receipt pair at one revision, a pair already used at
  another revision, and a duplicate kind.  A revision CAN be left with no
  completable readiness set (fail-closed): a stray receipt on the quorum
  whose pair already backs readiness, or that readiness expiring, refuses
  every other pair at that revision.  Recovery is rollback to ``disabled``,
  a fresh canary (new revision) and a fresh quorum; the ``characterization``
  tests pin this.
* A quorum is refused as ``billing_rollout_quorum_stale`` unless every one of
  its receipts was created strictly after the rollout receipt of the current
  state (readiness and every transition except to ``disabled``), including
  mixed quorums; an opening receipt is also signed for the rollout revision.
* Readiness rows are recorded only by the bound duty (deployer: readiness;
  reviewer: backlog, provisioning, smoke), never by a fourth super-admin.
* Assignments and receipts live at most the configured cap (24 h default).
* A signed ``from_state`` that differs from the stored state is refused
  before anything is consumed.
* Database constraint violations surface as ``billing_operation_conflict``,
  never as a SQLAlchemy/driver code.
* ``general -> disabled`` and incident-disable work from any non-disabled
  state with one fresh quorum each.
* The backend signs every receipt over its own role, actor, assignment and
  nonce; the client cannot submit a signature (422) and one role's stored
  signature does not authenticate another receipt.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Iterator
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.api.middleware.auth import TenantContext, require_super_admin_jwt
from src.api.routes import admin as admin_route
from src.config.settings import settings
from src.models.billing import BillingOperationReceipt, BillingRolloutReadinessReceipt
from src.repositories.billing import BillingRepository
from src.repositories.database import get_db_session_dependency
from src.services.billing_operations import (
    BillingOperationAuthority,
    BillingOperationRejected,
    OperationMaterial,
    canary_budget_digest,
    operation_receipt_signature,
)
from src.services.billing_outbox import (
    billing_fact_bundle_digest,
    billing_fact_schema_digest,
)
from src.services.billing_rollout import (
    BillingRolloutOperationService,
    _readiness_content_digest,
)
from tests.integration.test_asaas_billing_postgres import (
    _ROOT,
    _create_child,
    _drop_child,
    _evidence,
    _run_alembic,
    _validated_admin_url,
)

pytestmark = pytest.mark.integration

_ENVIRONMENT = "staging"
_SIGNING_KEY = "rollout-control-plane-measurement-key-0123456789"
_BUDGET_DIGEST = "b" * 64
_DEPLOYMENT_DIGEST = "d" * 64
_PRINCIPAL_HEADER = "X-Test-Principal"
_KINDS = ("readiness", "backlog", "provisioning", "smoke")
# Who may record each readiness kind: the deployer attests the deployed
# application (``operation_receipt_id``), the reviewer the provider-side
# checks (``provider_receipt_id``).
_RECORDER = {
    "readiness": "deployer",
    "backlog": "reviewer",
    "provisioning": "reviewer",
    "smoke": "reviewer",
}
_ROLES = ("deployer", "approver", "reviewer")
_CONFLICT = "billing_operation_conflict"


# --------------------------------------------------------------------------
# Disposable database: one migrated template per module, one clone per test.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Template:
    admin_engine: AsyncEngine
    admin_url: URL
    name: str


@pytest.fixture(scope="module")
async def template_database() -> AsyncIterator[_Template]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_admin_url)
    admin_engine = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    name = f"neurostack_asaas_smoke_{uuid4().hex[:12]}"
    created = False
    try:
        await _create_child(admin_engine, name)
        created = True
        await _run_alembic(admin_url.set(database=name), "upgrade", "head")
        yield _Template(admin_engine=admin_engine, admin_url=admin_url, name=name)
    finally:
        if created:
            await _drop_child(admin_engine, name)
        await admin_engine.dispose()


@pytest.fixture
async def sessions(
    template_database: _Template,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    name = f"neurostack_asaas_smoke_{uuid4().hex[:12]}"
    async with template_database.admin_engine.connect() as connection:
        quoted = connection.dialect.identifier_preparer.quote
        await connection.exec_driver_sql(
            f"CREATE DATABASE {quoted(name)} "
            f"TEMPLATE {quoted(template_database.name)}"
        )
    engine = create_async_engine(
        template_database.admin_url.set(database=name), poolclass=NullPool
    )
    try:
        # The application session semantics (src/repositories/database.py).
        yield async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
    finally:
        await engine.dispose()
        await _drop_child(template_database.admin_engine, name)


# --------------------------------------------------------------------------
# Public-route operator client.
# --------------------------------------------------------------------------


def _rule_digest() -> str:
    return hashlib.sha256(
        (_ROOT / "config/prometheus_rules.yml").read_bytes()
    ).hexdigest()


def _content_digest(
    material: dict[str, Any],
    *,
    kind: str,
    rollout_revision: int,
    deployer_receipt_id: str,
    reviewer_receipt_id: str,
    roster_revision: int = 0,
) -> str:
    return _readiness_content_digest(
        environment=material["environment"],
        receipt_kind=kind,
        rollout_revision=rollout_revision,
        roster_revision=roster_revision,
        operation_id=UUID(material["operation_id"]),
        operation_receipt_id=UUID(deployer_receipt_id),
        provider_receipt_id=UUID(reviewer_receipt_id),
        rule_digest=_rule_digest(),
        bundle_digest=billing_fact_bundle_digest(),
        app_digest=material["deployment_digest"],
    )


@dataclass
class _ControlPlane:
    """Drive the admin billing routes as named super-admin principals."""

    client: httpx.AsyncClient
    sessions: async_sessionmaker[AsyncSession]
    actors: dict[str, UUID] = field(default_factory=dict)

    def actor(self, principal: str) -> UUID:
        return self.actors.setdefault(principal, uuid4())

    async def post(self, principal: str, path: str, body: dict[str, Any]) -> Any:
        self.actor(principal)
        return await self.client.post(
            f"{settings.api_prefix}/admin/billing{path}",
            json=body,
            headers={_PRINCIPAL_HEADER: principal},
        )

    async def assign(
        self, principal: str, material: dict[str, Any], role: str
    ) -> httpx.Response:
        return await self.post(
            principal,
            "/operations/assignments",
            {**material, "role": role, "actor_user_id": str(self.actor(principal))},
        )

    async def receipt(
        self,
        principal: str,
        material: dict[str, Any],
        assignment_id: str,
        *,
        evidence_type: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Ask the backend to sign one receipt; the client sends no signature."""

        body: dict[str, Any] = {
            **material,
            "assignment_id": assignment_id,
            "nonce": str(uuid4()),
            **(extra or {}),
        }
        if evidence_type is not None:
            body.update(
                evidence_type=evidence_type,
                request_id=str(uuid4()),
                inbox_high_watermark=0,
            )
        return await self.post(principal, "/operations/receipts", body)

    async def signed(
        self,
        principal: str,
        material: dict[str, Any],
        role: str,
        *,
        evidence_type: str | None = None,
    ) -> str:
        """Self-assign *role* and append its signed receipt; return receipt id."""

        assignment = await self.assign(principal, material, role)
        assert assignment.status_code == 201, assignment.text
        receipt = await self.receipt(
            principal,
            material,
            assignment.json()["assignment_id"],
            evidence_type=evidence_type,
        )
        assert receipt.status_code == 201, receipt.text
        return str(receipt.json()["receipt_id"])

    async def quorum(
        self,
        material: dict[str, Any],
        principals: tuple[str, str, str] = ("deployer", "approver", "reviewer"),
    ) -> dict[str, str]:
        """One assignment + one signed receipt per duty; role -> receipt id."""

        return {
            role: await self.signed(principal, material, role)
            for role, principal in zip(_ROLES, principals, strict=True)
        }

    async def transition(
        self,
        material: dict[str, Any],
        *,
        expected_revision: int,
        canary_builder_id: UUID | None = None,
        incident: bool = False,
        principal: str = "deployer",
    ) -> httpx.Response:
        body = {**material, "expected_revision": expected_revision}
        if canary_builder_id is not None:
            body["canary_builder_id"] = str(canary_builder_id)
        path = "/rollout/incident-disable" if incident else "/rollout/transitions"
        return await self.post(principal, path, body)

    async def readiness(
        self,
        material: dict[str, Any],
        *,
        kind: str,
        rollout_revision: int,
        deployer_receipt_id: str,
        reviewer_receipt_id: str,
        roster_revision: int = 0,
        principal: str | None = None,
    ) -> httpx.Response:
        """Record one readiness row; by default as the duty that owns *kind*."""

        body = {
            # ``BillingRolloutReadinessReceiptRequest`` inherits the material
            # fields; its own ``expires_at`` is the material's expiry too.
            **material,
            "receipt_kind": kind,
            "rollout_revision": rollout_revision,
            "roster_revision": roster_revision,
            "status": "passed",
            "content_sha256": _content_digest(
                material,
                kind=kind,
                rollout_revision=rollout_revision,
                deployer_receipt_id=deployer_receipt_id,
                reviewer_receipt_id=reviewer_receipt_id,
                roster_revision=roster_revision,
            ),
            "operation_receipt_id": deployer_receipt_id,
            "provider_receipt_id": reviewer_receipt_id,
            "rule_digest": _rule_digest(),
            "bundle_digest": billing_fact_bundle_digest(),
            "app_digest": material["deployment_digest"],
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        return await self.post(principal or _RECORDER[kind], "/rollout/receipts", body)

    async def record_kinds(
        self,
        material: dict[str, Any],
        quorum: dict[str, str],
        *,
        rollout_revision: int,
        kinds: tuple[str, ...] = _KINDS,
    ) -> None:
        for kind in kinds:
            response = await self.readiness(
                material,
                kind=kind,
                rollout_revision=rollout_revision,
                deployer_receipt_id=quorum["deployer"],
                reviewer_receipt_id=quorum["reviewer"],
            )
            assert response.status_code == 201, (kind, response.text)

    async def readback(self) -> dict[str, Any]:
        self.actor("deployer")
        response = await self.client.get(
            f"{settings.api_prefix}/admin/billing/rollout/readback",
            headers={_PRINCIPAL_HEADER: "deployer"},
        )
        assert response.status_code == 200, response.text
        return dict(response.json())

    async def scalar(self, sql: str, **params: Any) -> Any:
        async with self.sessions() as session:
            return await session.scalar(text(sql), params)

    async def applied(self, material: dict[str, Any]) -> int:
        """Spent receipts of exactly this signed material."""

        return int(
            await self.scalar(
                "SELECT count(*) FROM billing_operation_receipts "
                "WHERE operation_id = :op AND payload_digest = :payload "
                "AND applied_at IS NOT NULL",
                op=material["operation_id"],
                payload=_payload_digest(material),
            )
        )

    async def readiness_rows(
        self, rollout_revision: int, *, environment: str = _ENVIRONMENT
    ) -> int:
        return int(
            await self.scalar(
                "SELECT count(*) FROM billing_rollout_readiness_receipts "
                "WHERE environment = :env AND rollout_revision = :rev",
                env=environment,
                rev=rollout_revision,
            )
        )


def _material(
    *,
    from_state: str,
    to_state: str,
    scope: str = "rollout",
    operation_id: UUID | None = None,
    expires_at: datetime | None = None,
    environment: str = _ENVIRONMENT,
    canary_builder_id: UUID | None = None,
) -> dict[str, Any]:
    """Operation material; a canary opening signs its builder's budget."""

    expiry = expires_at or (
        datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=30)
    )
    return {
        "scope": scope,
        "operation_id": str(operation_id or uuid4()),
        "environment": environment,
        "from_state": from_state,
        "to_state": to_state,
        "budget_digest": (
            _BUDGET_DIGEST
            if canary_builder_id is None
            else canary_budget_digest(
                environment=environment, canary_builder_id=canary_builder_id
            )
        ),
        "deployment_digest": _DEPLOYMENT_DIGEST,
        "expires_at": expiry.isoformat(),
    }


def _operation(material: dict[str, Any]) -> OperationMaterial:
    return OperationMaterial(
        scope=material["scope"],
        operation_id=UUID(material["operation_id"]),
        environment=material["environment"],
        from_state=material["from_state"],
        to_state=material["to_state"],
        budget_digest=material["budget_digest"],
        deployment_digest=material["deployment_digest"],
        expires_at=datetime.fromisoformat(material["expires_at"]),
    )


def _payload_digest(material: dict[str, Any]) -> str:
    return _operation(material).payload_digest


def _assert_rejected(response: httpx.Response, detail: str) -> None:
    assert (response.status_code, response.json()) == (409, {"detail": detail})


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "environment", _ENVIRONMENT)
    monkeypatch.setattr(
        settings, "billing_operation_receipt_signing_key", SecretStr(_SIGNING_KEY)
    )
    # Typed (evidence_type) receipts derive the deployed fact-schema digest.
    monkeypatch.setenv("BILLING_FACT_SCHEMA_DIGEST", billing_fact_schema_digest())
    yield


@pytest.fixture
async def plane(
    sessions: async_sessionmaker[AsyncSession], configured: None
) -> AsyncIterator[_ControlPlane]:
    app = FastAPI()
    app.include_router(admin_route.router, prefix=f"{settings.api_prefix}/admin")
    actors: dict[str, UUID] = {}
    tenant_id = uuid4()

    def principal(request: Request) -> TenantContext:
        # One super-admin JWT principal per duty, selected per request.
        return TenantContext(
            tenant_id=tenant_id,
            user_id=actors[request.headers[_PRINCIPAL_HEADER]],
            role="super_admin",
            authentication_mode="jwt",
        )

    async def session() -> AsyncIterator[AsyncSession]:
        async with sessions() as db_session:
            yield db_session

    app.dependency_overrides[require_super_admin_jwt] = principal
    app.dependency_overrides[get_db_session_dependency] = session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control-plane.test"
    ) as client:
        yield _ControlPlane(client=client, sessions=sessions, actors=actors)


async def _canary_builder(plane: _ControlPlane) -> UUID:
    builder_id = uuid4()
    async with plane.sessions() as session:
        async with session.begin():
            await session.execute(
                text("INSERT INTO builders (id, name) VALUES (:id, 'Canary')"),
                {"id": builder_id},
            )
    return builder_id


async def _open_canary(plane: _ControlPlane, *, environment: str = _ENVIRONMENT) -> int:
    """S1 procedure; returns the resulting rollout revision."""

    builder_id = await _canary_builder(plane)
    material = _material(
        from_state="disabled",
        to_state="canary_allowlist",
        environment=environment,
        canary_builder_id=builder_id,
    )
    await plane.quorum(material)
    response = await plane.transition(
        material, expected_revision=0, canary_builder_id=builder_id
    )
    assert response.status_code == 200, response.text
    return int(response.json()["rollout_revision"])


async def _promote_general(plane: _ControlPlane, revision: int) -> int:
    """The documented ``general`` procedure; returns the resulting revision."""

    material = _material(from_state="canary_allowlist", to_state="general")
    quorum = await plane.quorum(material)
    await plane.record_kinds(material, quorum, rollout_revision=revision)
    response = await plane.transition(material, expected_revision=revision)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "general"
    return int(response.json()["rollout_revision"])


# --------------------------------------------------------------------------
# S1: disabled -> canary_allowlist
# --------------------------------------------------------------------------


async def test_s1_canary_allowlist_needs_one_operation_three_actors_three_receipts(
    plane: _ControlPlane,
) -> None:
    builder_id = await _canary_builder(plane)
    assert (await plane.readback())["state"] == "disabled"
    material = _material(
        from_state="disabled", to_state="canary_allowlist", canary_builder_id=builder_id
    )
    operation_id = material["operation_id"]

    # Two receipts are not a quorum; nothing is consumed by the refusal.
    receipts = {
        "deployer": await plane.signed("deployer", material, "deployer"),
        "approver": await plane.signed("approver", material, "approver"),
    }
    _assert_rejected(
        await plane.transition(
            material, expected_revision=0, canary_builder_id=builder_id
        ),
        "billing_operation_quorum_incomplete",
    )
    receipts["reviewer"] = await plane.signed("reviewer", material, "reviewer")

    response = await plane.transition(
        material, expected_revision=0, canary_builder_id=builder_id
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "environment": _ENVIRONMENT,
        "state": "canary_allowlist",
        "rollout_revision": 1,
        "topup_commercial_enabled": False,
        "topup_revision": 0,
    }
    assert (await plane.readback())["state"] == "canary_allowlist"

    # Exactly: 1 operation, 3 assignments (consumed), 3 receipts (applied),
    # 3 distinct actors and nonces; the deployer's nonce is the rollout nonce.
    assert len(set(plane.actors.values())) == 3
    assert (
        await plane.scalar(
            "SELECT count(*) FROM billing_operation_assignments "
            "WHERE operation_id = :op AND consumed_at IS NOT NULL AND NOT active",
            op=operation_id,
        )
        == 3
    )
    assert (
        await plane.scalar(
            "SELECT count(DISTINCT nonce) FROM billing_operation_receipts "
            "WHERE operation_id = :op AND applied_at IS NOT NULL",
            op=operation_id,
        )
        == 3
    )
    assert await plane.scalar(
        "SELECT r.nonce = o.nonce AND r.expires_at = o.expires_at "
        "AND c.canary_builder_id = :builder "
        "FROM billing_rollout_control c "
        "JOIN billing_rollout_receipts r ON r.id = c.rollout_receipt_id "
        "JOIN billing_operation_receipts o ON o.id = :deployer "
        "WHERE c.environment = :env",
        builder=builder_id,
        deployer=receipts["deployer"],
        env=_ENVIRONMENT,
    )
    # Replayed against ``canary_allowlist`` the signed ``disabled`` origin is
    # refused first; once the state is ``disabled`` again the consumed
    # quorum is refused as stale (signed before the rollback that produced
    # the current state) before its spent receipts are even looked at.
    _assert_rejected(
        await plane.transition(
            material, expected_revision=1, canary_builder_id=builder_id
        ),
        "billing_rollout_from_state_mismatch",
    )
    rollback = _material(from_state="canary_allowlist", to_state="disabled")
    await plane.quorum(rollback)
    response = await plane.transition(rollback, expected_revision=1)
    assert response.status_code == 200, response.text
    _assert_rejected(
        await plane.transition(
            material, expected_revision=2, canary_builder_id=builder_id
        ),
        "billing_rollout_quorum_stale",
    )
    assert len(set(plane.actors.values())) == 3


async def test_ha_canary_quorum_signs_its_builder_so_a_front_run_is_refused(
    plane: _ControlPlane,
) -> None:
    """Whoever learns a fresh ``disabled -> canary_allowlist`` quorum cannot
    choose the canary builder: every signer signed
    ``budget_digest = canary_budget_digest(environment, builder)``."""

    builder_id = await _canary_builder(plane)
    attacker_builder = await _canary_builder(plane)
    material = _material(
        from_state="disabled", to_state="canary_allowlist", canary_builder_id=builder_id
    )
    await plane.quorum(material)

    # A fourth super-admin front-runs with another builder, or with none.
    for front_run in (attacker_builder, None):
        _assert_rejected(
            await plane.transition(
                material,
                expected_revision=0,
                canary_builder_id=front_run,
                principal="outsider",
            ),
            "billing_rollout_canary_budget_unbound",
        )
    # A canary quorum whose budget digest names no builder is refused too.
    unbound = _material(from_state="disabled", to_state="canary_allowlist")
    await plane.quorum(unbound, ("deployer-2", "approver-2", "reviewer-2"))
    _assert_rejected(
        await plane.transition(
            unbound, expected_revision=0, canary_builder_id=builder_id
        ),
        "billing_rollout_canary_budget_unbound",
    )
    assert (await plane.applied(material), await plane.applied(unbound)) == (0, 0)
    readback = await plane.readback()
    assert (readback["state"], readback["rollout_revision"]) == ("disabled", 0)

    # The legitimate call is unaffected and opens the signed builder's canary.
    response = await plane.transition(
        material, expected_revision=0, canary_builder_id=builder_id
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "canary_allowlist"
    assert await plane.applied(material) == 3
    assert (
        await plane.scalar(
            "SELECT canary_builder_id FROM billing_rollout_control "
            "WHERE environment = :env",
            env=_ENVIRONMENT,
        )
        == builder_id
    )


# --------------------------------------------------------------------------
# HB: receipt lifetime cap (BILLING_OPERATION_RECEIPT_MAX_TTL_SECONDS)
# --------------------------------------------------------------------------

_TOO_LONG = "billing_operation_expiry_too_long"


async def _operation_rows(plane: _ControlPlane, material: dict[str, Any]) -> tuple:
    return (
        await plane.scalar(
            "SELECT count(*) FROM billing_operation_assignments "
            "WHERE operation_id = :op",
            op=material["operation_id"],
        ),
        await plane.scalar(
            "SELECT count(*) FROM billing_operation_receipts "
            "WHERE operation_id = :op",
            op=material["operation_id"],
        ),
    )


async def test_hb_ten_year_receipt_is_refused_with_a_typed_error(
    plane: _ControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quorum of ten-year receipts used to promote; now neither its
    assignments nor its receipts can be created (default cap: 24 h)."""

    ten_years = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=3650)
    material = _material(
        from_state="disabled", to_state="canary_allowlist", expires_at=ten_years
    )
    _assert_rejected(await plane.assign("deployer", material, "deployer"), _TOO_LONG)
    assert await _operation_rows(plane, material) == (0, 0)

    # An assignment created under a looser cap cannot yield a receipt once
    # the cap is tightened: the receipt is judged at its own creation.
    two_days = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=2)
    material = _material(
        from_state="disabled", to_state="canary_allowlist", expires_at=two_days
    )
    monkeypatch.setattr(settings, "billing_operation_receipt_max_ttl_seconds", 604800)
    assignment = await plane.assign("deployer", material, "deployer")
    assert assignment.status_code == 201, assignment.text
    monkeypatch.setattr(settings, "billing_operation_receipt_max_ttl_seconds", 86400)
    _assert_rejected(
        await plane.receipt("deployer", material, assignment.json()["assignment_id"]),
        _TOO_LONG,
    )
    assert await _operation_rows(plane, material) == (1, 0)


async def test_hb_receipt_lifetime_boundary_service_clock(
    plane: _ControlPlane,
) -> None:
    """Exactly the cap after creation is accepted; one microsecond more is
    refused (operation authority called directly with an injected clock)."""

    created = datetime.now(timezone.utc).replace(microsecond=0)
    cap = timedelta(seconds=settings.billing_operation_receipt_max_ttl_seconds)
    assert cap == timedelta(hours=24)
    for expires_at, accepted in ((created + cap, True), (created + cap, False)):
        if not accepted:
            expires_at += timedelta(microseconds=1)
        operation = _operation(
            _material(
                from_state="disabled",
                to_state="canary_allowlist",
                expires_at=expires_at,
            )
        )
        actor = uuid4()
        async with plane.sessions() as session:
            authority = BillingOperationAuthority(
                session, signing_key=_SIGNING_KEY.encode(), clock=lambda: created
            )
            if not accepted:
                with pytest.raises(BillingOperationRejected, match=f"^{_TOO_LONG}$"):
                    await authority.assign(
                        material=operation, role="deployer", actor_user_id=actor
                    )
                continue
            assignment = await authority.assign(
                material=operation, role="deployer", actor_user_id=actor
            )
            receipt = await authority.record_receipt(
                assignment_id=assignment.id,
                actor_user_id=actor,
                nonce=uuid4(),
                material=operation,
            )
            await session.commit()
            assert receipt.expires_at - created == cap


# --------------------------------------------------------------------------
# S2: canary_allowlist -> general spends one quorum exactly once
# --------------------------------------------------------------------------


async def test_s2_readiness_does_not_spend_the_quorum_and_general_spends_it_once(
    plane: _ControlPlane,
) -> None:
    revision = await _open_canary(plane)
    material = _material(from_state="canary_allowlist", to_state="general")
    quorum = await plane.quorum(material)

    # Without readiness, ``general`` is refused before any consumption.
    _assert_rejected(
        await plane.transition(material, expected_revision=revision),
        "billing_rollout_readiness_incomplete",
    )
    await plane.record_kinds(material, quorum, rollout_revision=revision)
    # Four readiness rows, all naming the same unspent pair.
    assert await plane.applied(material) == 0
    assert (
        await plane.scalar(
            "SELECT count(*) FROM billing_rollout_readiness_receipts "
            "WHERE rollout_revision = :rev AND operation_receipt_id = :deployer "
            "AND provider_receipt_id = :reviewer",
            rev=revision,
            deployer=quorum["deployer"],
            reviewer=quorum["reviewer"],
        )
        == 4
    )

    response = await plane.transition(material, expected_revision=revision)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "general"
    assert response.json()["rollout_revision"] == revision + 1
    # The transition spent the three receipts once; its nonce is the deployer's.
    assert await plane.applied(material) == 3
    assert await plane.scalar(
        "SELECT r.nonce = o.nonce FROM billing_rollout_control c "
        "JOIN billing_rollout_receipts r ON r.id = c.rollout_receipt_id "
        "JOIN billing_operation_receipts o ON o.id = :deployer "
        "WHERE c.environment = :env",
        deployer=quorum["deployer"],
        env=_ENVIRONMENT,
    )
    # Replaying the spent quorum against its own readiness rows: the rows now
    # name spent evidence and are unbound.
    _assert_rejected(
        await plane.transition(material, expected_revision=revision),
        "billing_rollout_readiness_unbound",
    )
    assert (await plane.readback())["state"] == "general"
    assert len(set(plane.actors.values())) == 3


# --------------------------------------------------------------------------
# S3: strategies that previously could not reach ``general``
# --------------------------------------------------------------------------


async def test_s3a_readiness_under_another_operation_is_refused_before_insert(
    plane: _ControlPlane,
) -> None:
    """D4: a second operation at one revision fails early, poisoning nothing."""

    revision = await _open_canary(plane)
    first = _material(from_state="canary_allowlist", to_state="general")
    first_quorum = await plane.quorum(first)
    await plane.record_kinds(
        first, first_quorum, rollout_revision=revision, kinds=("readiness",)
    )

    # The same three actors sign another general operation (allowed), but
    # its receipts cannot back readiness at this revision.
    other = _material(from_state="canary_allowlist", to_state="general")
    other_quorum = await plane.quorum(other)
    _assert_rejected(
        await plane.readiness(
            other,
            kind="backlog",
            rollout_revision=revision,
            deployer_receipt_id=other_quorum["deployer"],
            reviewer_receipt_id=other_quorum["reviewer"],
        ),
        "billing_rollout_readiness_operation_conflict",
    )
    assert await plane.readiness_rows(revision) == 1

    # The revision stays usable: finish with the first operation.
    await plane.record_kinds(
        first, first_quorum, rollout_revision=revision, kinds=_KINDS[1:]
    )
    _assert_rejected(
        await plane.transition(other, expected_revision=revision),
        "billing_rollout_readiness_incomplete",
    )
    response = await plane.transition(first, expected_revision=revision)
    assert response.status_code == 200, response.text
    assert (await plane.readback())["state"] == "general"
    assert (await plane.applied(first), await plane.applied(other)) == (3, 0)
    assert len(set(plane.actors.values())) == 3


async def test_s3b_readiness_refuses_receipts_outside_the_quorum_general_spends(
    plane: _ControlPlane,
) -> None:
    revision = await _open_canary(plane)
    material = _material(from_state="canary_allowlist", to_state="general")
    quorum = await plane.quorum(material)
    await plane.record_kinds(
        material, quorum, rollout_revision=revision, kinds=("readiness",)
    )

    # Typed evidence receipts share the operation but are never spent by
    # ``consume_quorum``; they cannot back readiness.
    typed_deployer = await plane.signed(
        "deployer-2", material, "deployer", evidence_type="verification"
    )
    typed_reviewer = await plane.signed(
        "reviewer-2", material, "reviewer", evidence_type="verification"
    )
    _assert_rejected(
        await plane.readiness(
            material,
            kind="backlog",
            rollout_revision=revision,
            deployer_receipt_id=typed_deployer,
            reviewer_receipt_id=typed_reviewer,
        ),
        "billing_rollout_readiness_receipt_invalid",
    )

    # Ordinary receipts from new actors: the operation now has five quorum
    # rows, never three, so no readiness (and no transition) can use it.
    deployer = await plane.signed("deployer-3", material, "deployer")
    reviewer = await plane.signed("reviewer-3", material, "reviewer")
    _assert_rejected(
        await plane.readiness(
            material,
            kind="backlog",
            rollout_revision=revision,
            deployer_receipt_id=deployer,
            reviewer_receipt_id=reviewer,
        ),
        "billing_operation_quorum_incomplete",
    )
    _assert_rejected(
        await plane.transition(material, expected_revision=revision),
        "billing_rollout_readiness_incomplete",
    )
    assert await plane.readiness_rows(revision) == 1
    assert len(set(plane.actors.values())) == 7
    assert (await plane.readback())["state"] == "canary_allowlist"


async def test_s3c_readiness_is_bound_to_one_payload_digest(
    plane: _ControlPlane,
) -> None:
    revision = await _open_canary(plane)
    operation_id = uuid4()
    base = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=30)
    signed = _material(
        from_state="canary_allowlist",
        to_state="general",
        operation_id=operation_id,
        expires_at=base,
    )
    quorum = await plane.quorum(signed)
    await plane.record_kinds(
        signed, quorum, rollout_revision=revision, kinds=("readiness",)
    )

    # Same operation, another expiry: another payload digest, hence another
    # quorum (three actors never used on this operation).
    drifted = _material(
        from_state="canary_allowlist",
        to_state="general",
        operation_id=operation_id,
        expires_at=base + timedelta(seconds=1),
    )
    drifted_quorum = await plane.quorum(
        drifted, ("deployer-2", "approver-2", "reviewer-2")
    )
    _assert_rejected(
        await plane.readiness(
            drifted,
            kind="backlog",
            rollout_revision=revision,
            deployer_receipt_id=drifted_quorum["deployer"],
            reviewer_receipt_id=drifted_quorum["reviewer"],
            principal="reviewer-2",
        ),
        "billing_rollout_readiness_operation_conflict",
    )
    await plane.record_kinds(
        signed, quorum, rollout_revision=revision, kinds=_KINDS[1:]
    )

    # Four rows of this operation exist, but they name the first payload's
    # receipts; the drifted payload's transition is refused, unspent.
    _assert_rejected(
        await plane.transition(drifted, expected_revision=revision),
        "billing_rollout_readiness_unbound",
    )
    assert (await plane.applied(signed), await plane.applied(drifted)) == (0, 0)
    response = await plane.transition(signed, expected_revision=revision)
    assert response.status_code == 200, response.text
    assert (await plane.readback())["state"] == "general"
    assert len(set(plane.actors.values())) == 6


async def test_s3_documented_general_rollout_procedure_reaches_general(
    plane: _ControlPlane,
) -> None:
    """The procedure the design describes: one operation, one 3-actor quorum,
    four readiness rows bound to it, then the ``general`` transition."""

    revision = await _open_canary(plane)
    material = _material(from_state="canary_allowlist", to_state="general")
    quorum = await plane.quorum(material)
    for kind in _KINDS:
        response = await plane.readiness(
            material,
            kind=kind,
            rollout_revision=revision,
            deployer_receipt_id=quorum["deployer"],
            reviewer_receipt_id=quorum["reviewer"],
        )
        assert response.status_code == 201, (kind, response.text)
    response = await plane.transition(material, expected_revision=revision)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "general"
    assert (await plane.readback())["state"] == "general"
    assert len(set(plane.actors.values())) == 3


# --------------------------------------------------------------------------
# D1: attacks on readiness that names unspent receipts
# --------------------------------------------------------------------------


async def test_d1_readiness_pair_cannot_be_replayed_at_a_later_rollout_revision(
    plane: _ControlPlane,
) -> None:
    revision = await _open_canary(plane)
    material = _material(from_state="canary_allowlist", to_state="general")
    quorum = await plane.quorum(material)
    await plane.record_kinds(material, quorum, rollout_revision=revision)

    # An incident-style rollback and a fresh canary bump the revision by two
    # while the general quorum stays unspent and unexpired.
    rollback = _material(from_state="canary_allowlist", to_state="disabled")
    await plane.quorum(rollback)
    response = await plane.transition(rollback, expected_revision=revision)
    assert response.status_code == 200, response.text
    builder_id = await _canary_builder(plane)
    reopen = _material(
        from_state="disabled", to_state="canary_allowlist", canary_builder_id=builder_id
    )
    await plane.quorum(reopen)
    response = await plane.transition(
        reopen, expected_revision=revision + 1, canary_builder_id=builder_id
    )
    assert response.status_code == 200, response.text
    current = revision + 2

    # The old readiness rows are bound to the old revision, whose quorum
    # predates the current state (refused before the revision check)...
    _assert_rejected(
        await plane.transition(material, expected_revision=revision),
        "billing_rollout_quorum_stale",
    )
    _assert_rejected(
        await plane.transition(material, expected_revision=current),
        "billing_rollout_readiness_incomplete",
    )
    # ...and their unspent pair cannot be re-recorded at the new revision:
    # the quorum predates the current state.
    _assert_rejected(
        await plane.readiness(
            material,
            kind="readiness",
            rollout_revision=current,
            deployer_receipt_id=quorum["deployer"],
            reviewer_receipt_id=quorum["reviewer"],
        ),
        "billing_rollout_quorum_stale",
    )
    # Defence in depth: a quorum that only looks fresh (created_at rewritten
    # past the append-only trigger) is still signed for the old revision...
    await _shift_quorum_created_at(plane, material, timedelta(microseconds=1))
    _assert_rejected(
        await plane.readiness(
            material,
            kind="readiness",
            rollout_revision=current,
            deployer_receipt_id=quorum["deployer"],
            reviewer_receipt_id=quorum["reviewer"],
        ),
        "billing_operation_signature_invalid",
    )
    # ...and even re-signed for the current revision with the server key
    # (past the trigger), its pair was used at another revision.
    await _resign_quorum(plane, material, rollout_revision=current)
    _assert_rejected(
        await plane.readiness(
            material,
            kind="readiness",
            rollout_revision=current,
            deployer_receipt_id=quorum["deployer"],
            reviewer_receipt_id=quorum["reviewer"],
        ),
        "billing_rollout_readiness_receipt_reused",
    )
    assert await plane.readiness_rows(current) == 0
    assert await plane.applied(material) == 0
    # A fresh general approval at the new revision still promotes.
    assert await _promote_general(plane, current) == current + 1


async def test_d1_concurrent_general_transitions_spend_the_quorum_exactly_once(
    plane: _ControlPlane,
) -> None:
    revision = await _open_canary(plane)
    material = _material(from_state="canary_allowlist", to_state="general")
    quorum = await plane.quorum(material)
    await plane.record_kinds(material, quorum, rollout_revision=revision)

    responses = await asyncio.gather(
        plane.transition(material, expected_revision=revision),
        plane.transition(material, expected_revision=revision),
    )
    outcomes = sorted(
        (response.status_code, response.json().get("detail")) for response in responses
    )
    assert outcomes == [(200, None), (409, "billing_rollout_readiness_unbound")]
    assert await plane.applied(material) == 3
    assert (await plane.readback())["rollout_revision"] == revision + 1
    assert (
        await plane.scalar(
            "SELECT count(*) FROM billing_rollout_receipts "
            "WHERE environment = :env AND to_state = 'general'",
            env=_ENVIRONMENT,
        )
        == 1
    )


async def test_d1_general_spends_exactly_the_receipts_its_rows_name_bypass_row(
    plane: _ControlPlane,
) -> None:
    """A readiness row written past the service (direct SQL) that names typed
    receipts of the same material passes every per-row check; the transition
    still refuses, because it spends a different pair than the row names."""

    revision = await _open_canary(plane)
    material = _material(from_state="canary_allowlist", to_state="general")
    quorum = await plane.quorum(material)
    await plane.record_kinds(
        material, quorum, rollout_revision=revision, kinds=_KINDS[:3]
    )
    typed_deployer = await plane.signed(
        "deployer-2", material, "deployer", evidence_type="verification"
    )
    typed_reviewer = await plane.signed(
        "reviewer-2", material, "reviewer", evidence_type="verification"
    )
    async with plane.sessions() as session:
        async with session.begin():
            session.add(
                BillingRolloutReadinessReceipt(
                    environment=_ENVIRONMENT,
                    receipt_kind="smoke",
                    rollout_revision=revision,
                    status="passed",
                    content_sha256=_content_digest(
                        material,
                        kind="smoke",
                        rollout_revision=revision,
                        deployer_receipt_id=typed_deployer,
                        reviewer_receipt_id=typed_reviewer,
                    ),
                    operation_id=UUID(material["operation_id"]),
                    operation_receipt_id=UUID(typed_deployer),
                    provider_receipt_id=UUID(typed_reviewer),
                    roster_revision=0,
                    rule_digest=_rule_digest(),
                    bundle_digest=billing_fact_bundle_digest(),
                    app_digest=material["deployment_digest"],
                    observed_at=datetime.now(timezone.utc),
                    expires_at=_operation(material).expires_at,
                )
            )

    _assert_rejected(
        await plane.transition(material, expected_revision=revision),
        "billing_rollout_readiness_unbound",
    )
    assert await plane.applied(material) == 0
    assert (await plane.readback())["state"] == "canary_allowlist"


async def test_d1_receipt_expiry_bounds_readiness_service_clock(
    plane: _ControlPlane,
) -> None:
    """Readiness can never outlive the receipts it names, and expired evidence
    cannot promote (service called directly with an injected clock)."""

    revision = await _open_canary(plane)
    material = _material(from_state="canary_allowlist", to_state="general")
    operation = _operation(material)
    quorum = await plane.quorum(material)

    async with plane.sessions() as session:
        service = BillingRolloutOperationService(
            session, signing_key=_SIGNING_KEY.encode()
        )
        with pytest.raises(
            BillingOperationRejected,
            match="^billing_rollout_readiness_receipt_invalid$",
        ):
            await service.record_readiness_receipt(
                environment=_ENVIRONMENT,
                receipt_kind="readiness",
                rollout_revision=revision,
                roster_revision=0,
                status="passed",
                content_sha256=_content_digest(
                    material,
                    kind="readiness",
                    rollout_revision=revision,
                    deployer_receipt_id=quorum["deployer"],
                    reviewer_receipt_id=quorum["reviewer"],
                ),
                material=operation,
                operation_receipt_id=UUID(quorum["deployer"]),
                provider_receipt_id=UUID(quorum["reviewer"]),
                rule_digest=_rule_digest(),
                bundle_digest=billing_fact_bundle_digest(),
                app_digest=material["deployment_digest"],
                observed_at=datetime.now(timezone.utc),
                expires_at=operation.expires_at + timedelta(minutes=1),
                recorder_user_id=plane.actor("deployer"),
            )
        await session.rollback()
    assert await plane.readiness_rows(revision) == 0

    await plane.record_kinds(material, quorum, rollout_revision=revision)
    late = operation.expires_at + timedelta(seconds=1)
    async with plane.sessions() as session:
        service = BillingRolloutOperationService(
            session, signing_key=_SIGNING_KEY.encode(), clock=lambda: late
        )
        with pytest.raises(
            BillingOperationRejected, match="^billing_rollout_readiness_incomplete$"
        ):
            await service.transition(expected_revision=revision, material=operation)
        await session.rollback()
    assert await plane.applied(material) == 0
    assert (await plane.readback())["state"] == "canary_allowlist"


@pytest.mark.parametrize("drift", ["rule", "bundle"])
async def test_d1_rule_and_bundle_digest_drift_unbinds_readiness(
    plane: _ControlPlane, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    from src.services import billing_rollout

    revision = await _open_canary(plane)
    material = _material(from_state="canary_allowlist", to_state="general")
    quorum = await plane.quorum(material)
    await plane.record_kinds(material, quorum, rollout_revision=revision)

    # A redeploy between readiness and the transition changes what is on disk.
    if drift == "rule":
        monkeypatch.setattr(billing_rollout, "_digest_file", lambda _path: "e" * 64)
    else:
        monkeypatch.setattr(
            billing_rollout, "billing_fact_bundle_digest", lambda: "e" * 64
        )
    _assert_rejected(
        await plane.transition(material, expected_revision=revision),
        "billing_rollout_readiness_unbound",
    )
    assert await plane.applied(material) == 0
    assert (await plane.readback())["state"] == "canary_allowlist"


# --------------------------------------------------------------------------
# D2: the signed from_state is the persisted state
# --------------------------------------------------------------------------


async def test_d2_signed_from_state_must_match_the_persisted_state(
    plane: _ControlPlane,
) -> None:
    revision = await _open_canary(plane)
    stale = _material(from_state="general", to_state="disabled")
    await plane.quorum(stale)
    _assert_rejected(
        await plane.transition(stale, expected_revision=revision),
        "billing_rollout_from_state_mismatch",
    )
    # Typed refusal before consumption: nothing changed, nothing was spent.
    assert (await plane.readback())["state"] == "canary_allowlist"
    assert (await plane.readback())["rollout_revision"] == revision
    assert await plane.applied(stale) == 0

    # Once the persisted state is what was signed, the same quorum applies.
    general_revision = await _promote_general(plane, revision)
    response = await plane.transition(stale, expected_revision=general_revision)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "disabled"
    assert await plane.applied(stale) == 3


# --------------------------------------------------------------------------
# D3: constraint violations map to the domain conflict code
# --------------------------------------------------------------------------


async def test_d3_integrity_violations_surface_as_billing_operation_conflict(
    plane: _ControlPlane,
) -> None:
    revision = await _open_canary(plane)
    material = _material(from_state="canary_allowlist", to_state="general")
    quorum = await plane.quorum(material)

    # uq_billing_operation_assignments_actor (operation_id, actor_user_id).
    # Intentional server-log ERROR: duplicate key ... _assignments_actor.
    _assert_rejected(await plane.assign("deployer", material, "approver"), _CONFLICT)

    # uq_billing_rollout_readiness_revision, raised by PostgreSQL for a row
    # written past the service's duplicate-kind check.  Intentional
    # server-log ERROR: duplicate key ... _readiness_revision.
    await plane.record_kinds(
        material, quorum, rollout_revision=revision, kinds=("readiness",)
    )
    async with plane.sessions() as session:
        session.add(
            BillingRolloutReadinessReceipt(
                environment=_ENVIRONMENT,
                receipt_kind="readiness",
                rollout_revision=revision,
                status="passed",
                content_sha256="c" * 64,
                operation_id=UUID(material["operation_id"]),
                operation_receipt_id=UUID(quorum["deployer"]),
                provider_receipt_id=UUID(quorum["reviewer"]),
                roster_revision=0,
                rule_digest=_rule_digest(),
                bundle_digest=billing_fact_bundle_digest(),
                app_digest=material["deployment_digest"],
                observed_at=datetime.now(timezone.utc),
                expires_at=_operation(material).expires_at,
            )
        )
        with pytest.raises(
            IntegrityError, match="uq_billing_rollout_readiness_revision"
        ) as captured:
            await session.flush()
        await session.rollback()
    error = admin_route._billing_http_error(captured.value)
    assert (error.status_code, error.detail) == (409, _CONFLICT)
    assert error.detail != captured.value.code


# --------------------------------------------------------------------------
# D4: readiness the transition could not use is refused before insert
# --------------------------------------------------------------------------


async def test_d4_unusable_readiness_is_refused_before_insert(
    plane: _ControlPlane,
) -> None:
    # Stored ``disabled``: readiness for a canary -> general quorum is moot.
    early = _material(from_state="canary_allowlist", to_state="general")
    early_quorum = await plane.quorum(early)
    _assert_rejected(
        await plane.readiness(
            early,
            kind="readiness",
            rollout_revision=0,
            deployer_receipt_id=early_quorum["deployer"],
            reviewer_receipt_id=early_quorum["reviewer"],
        ),
        "billing_rollout_from_state_mismatch",
    )
    assert await plane.readiness_rows(0) == 0

    revision = await _open_canary(plane)
    # A valid quorum for another transition can never back ``general``.
    rollback = _material(from_state="canary_allowlist", to_state="disabled")
    rollback_quorum = await plane.quorum(rollback)
    _assert_rejected(
        await plane.readiness(
            rollback,
            kind="readiness",
            rollout_revision=revision,
            deployer_receipt_id=rollback_quorum["deployer"],
            reviewer_receipt_id=rollback_quorum["reviewer"],
        ),
        "billing_rollout_readiness_transition_invalid",
    )
    # A quorum the transition could not consume yet (no approver).
    material = _material(from_state="canary_allowlist", to_state="general")
    quorum = {
        "deployer": await plane.signed("deployer", material, "deployer"),
        "reviewer": await plane.signed("reviewer", material, "reviewer"),
    }
    _assert_rejected(
        await plane.readiness(
            material,
            kind="readiness",
            rollout_revision=revision,
            deployer_receipt_id=quorum["deployer"],
            reviewer_receipt_id=quorum["reviewer"],
        ),
        "billing_operation_quorum_incomplete",
    )
    assert await plane.readiness_rows(revision) == 0

    # None of the refusals occupied a kind: the procedure still completes.
    await plane.signed("approver", material, "approver")
    await plane.record_kinds(
        material, quorum, rollout_revision=revision, kinds=("readiness",)
    )
    _assert_rejected(
        await plane.readiness(
            material,
            kind="readiness",
            rollout_revision=revision,
            deployer_receipt_id=quorum["deployer"],
            reviewer_receipt_id=quorum["reviewer"],
        ),
        "billing_rollout_readiness_duplicate",
    )
    await plane.record_kinds(
        material, quorum, rollout_revision=revision, kinds=_KINDS[1:]
    )
    response = await plane.transition(material, expected_revision=revision)
    assert response.status_code == 200, response.text
    assert (await plane.readback())["state"] == "general"
    assert await plane.applied(rollback) == 0
    assert len(set(plane.actors.values())) == 3


# --------------------------------------------------------------------------
# HC: readiness rows are recorded by the duty they bind
# --------------------------------------------------------------------------

_RECORDER_INVALID = "billing_rollout_readiness_recorder_invalid"


async def test_hc_a_fourth_super_admin_cannot_record_readiness_rows(
    plane: _ControlPlane,
) -> None:
    """Reviewer probe: a fourth super-admin in no quorum recorded all four
    rows and triggered ``general``.  Each kind is now recorded only by the
    actor of the receipt it is bound to: ``readiness`` by the deployer
    (``operation_receipt_id``); ``backlog``, ``provisioning`` and ``smoke``
    by the reviewer (``provider_receipt_id``)."""

    revision = await _open_canary(plane)
    material = _material(from_state="canary_allowlist", to_state="general")
    quorum = await plane.quorum(material)
    for kind in _KINDS:
        other_duty = "reviewer" if _RECORDER[kind] == "deployer" else "deployer"
        for principal in ("outsider", "approver", other_duty):
            _assert_rejected(
                await plane.readiness(
                    material,
                    kind=kind,
                    rollout_revision=revision,
                    deployer_receipt_id=quorum["deployer"],
                    reviewer_receipt_id=quorum["reviewer"],
                    principal=principal,
                ),
                _RECORDER_INVALID,
            )
    assert await plane.readiness_rows(revision) == 0
    _assert_rejected(
        await plane.transition(
            material, expected_revision=revision, principal="outsider"
        ),
        "billing_rollout_readiness_incomplete",
    )
    assert await plane.applied(material) == 0

    # The bound duties record their kinds; then anyone may trigger the
    # transition the quorum authorizes.
    await plane.record_kinds(material, quorum, rollout_revision=revision)
    response = await plane.transition(
        material, expected_revision=revision, principal="outsider"
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "general"


# --------------------------------------------------------------------------
# S4: rollback paths
# --------------------------------------------------------------------------


async def _seed_general_outside_public_routes(plane: _ControlPlane) -> int:
    """Seed ``general`` via the repository to measure rollbacks in isolation.

    ``general`` is publicly reachable (see the documented procedure test);
    ``BillingRepository.transition_rollout`` is internal and takes unsigned
    evidence, and is used here only so the rollback measurements below do
    not depend on the readiness procedure.
    """

    async with plane.sessions() as session:
        async with session.begin():
            repository = BillingRepository(session)
            control = await repository.lock_rollout_control(environment=_ENVIRONMENT)
            await repository.transition_rollout(
                environment=_ENVIRONMENT,
                expected_revision=control.rollout_revision,
                to_state="general",
                evidence=_evidence(),
            )
            return int(control.rollout_revision)


async def test_s4_general_to_disabled_needs_one_fresh_rollout_quorum(
    plane: _ControlPlane,
) -> None:
    await _open_canary(plane)
    revision = await _seed_general_outside_public_routes(plane)
    assert (await plane.readback())["state"] == "general"

    material = _material(from_state="general", to_state="disabled")
    await plane.quorum(material)
    response = await plane.transition(material, expected_revision=revision)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "disabled"
    assert response.json()["rollout_revision"] == revision + 1
    assert len(set(plane.actors.values())) == 3


async def test_s4_canary_to_disabled_needs_one_fresh_rollout_quorum(
    plane: _ControlPlane,
) -> None:
    revision = await _open_canary(plane)
    material = _material(from_state="canary_allowlist", to_state="disabled")
    await plane.quorum(material)
    response = await plane.transition(material, expected_revision=revision)
    assert response.status_code == 200, response.text
    assert (await plane.readback())["state"] == "disabled"


async def test_s4_incident_disable_needs_its_own_incident_scoped_quorum(
    plane: _ControlPlane,
) -> None:
    await _open_canary(plane)
    revision = await _seed_general_outside_public_routes(plane)

    # A rollout-scoped quorum cannot be spent on the incident route.
    rollout = _material(from_state="general", to_state="disabled")
    await plane.quorum(rollout)
    _assert_rejected(
        await plane.transition(rollout, expected_revision=revision, incident=True),
        "billing_incident_transition_invalid",
    )
    # The incident route only ever moves to ``disabled``.
    _assert_rejected(
        await plane.transition(
            _material(scope="incident", from_state="general", to_state="canary"),
            expected_revision=revision,
            incident=True,
        ),
        "billing_incident_transition_invalid",
    )

    incident = _material(scope="incident", from_state="general", to_state="disabled")
    await plane.signed("deployer", incident, "deployer")
    await plane.signed("approver", incident, "approver")
    _assert_rejected(
        await plane.transition(incident, expected_revision=revision, incident=True),
        "billing_operation_quorum_incomplete",
    )
    await plane.signed("reviewer", incident, "reviewer")
    response = await plane.transition(
        incident, expected_revision=revision, incident=True
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "disabled"
    assert (await plane.readback())["state"] == "disabled"

    # From ``disabled`` another incident quorum is consumed then rolled back
    # by the repository's allowed-transition check (no code attribute ->
    # generic conflict detail).
    again = _material(scope="incident", from_state="disabled", to_state="disabled")
    await plane.quorum(again)
    _assert_rejected(
        await plane.transition(again, expected_revision=revision + 1, incident=True),
        "billing_operation_conflict",
    )
    assert len(set(plane.actors.values())) == 3


# --------------------------------------------------------------------------
# S5: signature binding probe
# --------------------------------------------------------------------------


async def _overwrite_receipt_column(
    plane: _ControlPlane, receipt_id: str, column: str, value: Any
) -> None:
    """Rewrite one stored receipt column past the append-only trigger."""

    assert column in {"signature_sha256", "nonce"}
    async with plane.sessions() as session:
        async with session.begin():
            await session.execute(text("SET LOCAL session_replication_role = replica"))
            result = await session.execute(
                text(
                    f"UPDATE billing_operation_receipts SET {column} = :value "
                    "WHERE id = :id"
                ),
                {"value": value, "id": receipt_id},
            )
            assert result.rowcount == 1


async def test_s5_old_receipt_request_shape_with_a_client_signature_is_refused(
    plane: _ControlPlane,
) -> None:
    """The client cannot even submit a signature: the closed schema is 422."""

    material = _material(from_state="disabled", to_state="canary_allowlist")
    assignment = await plane.assign("deployer", material, "deployer")
    assert assignment.status_code == 201, assignment.text
    response = await plane.receipt(
        "deployer",
        material,
        assignment.json()["assignment_id"],
        extra={"signature": "a" * 64},
    )
    assert response.status_code == 422, response.text
    assert [error["loc"] for error in response.json()["detail"]] == [
        ["body", "signature"]
    ]
    assert [error["type"] for error in response.json()["detail"]] == ["extra_forbidden"]
    assert (
        await plane.scalar(
            "SELECT count(*) FROM billing_operation_receipts WHERE operation_id = :op",
            op=material["operation_id"],
        )
        == 0
    )


async def test_s5_a_used_nonce_is_refused_before_anything_is_signed(
    plane: _ControlPlane,
) -> None:
    material = _material(from_state="disabled", to_state="canary_allowlist")
    nonce = str(uuid4())
    receipts = []
    for role in ("deployer", "approver"):
        assignment = await plane.assign(role, material, role)
        assert assignment.status_code == 201, assignment.text
        receipts.append(
            await plane.receipt(
                role,
                material,
                assignment.json()["assignment_id"],
                extra={"nonce": nonce},
            )
        )
    assert receipts[0].status_code == 201, receipts[0].text
    _assert_rejected(receipts[1], "billing_operation_nonce_conflict")
    assert await _operation_rows(plane, material) == (2, 1)


async def test_s5_backend_signature_of_one_role_cannot_be_replayed_as_another(
    plane: _ControlPlane,
) -> None:
    """The backend signs each receipt over its own role, actor, assignment and
    nonce, so the deployer's stored signature is not a valid approver or
    reviewer signature, nor valid for another nonce.  (Rows are rewritten past
    the append-only trigger: the client has no way to submit a signature.)"""

    builder_id = await _canary_builder(plane)
    material = _material(
        from_state="disabled", to_state="canary_allowlist", canary_builder_id=builder_id
    )
    quorum = await plane.quorum(material)
    signatures = {
        role: await plane.scalar(
            "SELECT signature_sha256 FROM billing_operation_receipts WHERE id = :id",
            id=receipt_id,
        )
        for role, receipt_id in quorum.items()
    }
    # Three receipts of one material, three distinct backend signatures.
    assert len(set(signatures.values())) == 3

    async def refused() -> None:
        _assert_rejected(
            await plane.transition(
                material, expected_revision=0, canary_builder_id=builder_id
            ),
            "billing_operation_signature_invalid",
        )
        assert await plane.applied(material) == 0

    for role in ("approver", "reviewer"):
        await _overwrite_receipt_column(
            plane, quorum[role], "signature_sha256", signatures["deployer"]
        )
        await refused()
        await _overwrite_receipt_column(
            plane, quorum[role], "signature_sha256", signatures[role]
        )
    deployer_nonce = await plane.scalar(
        "SELECT nonce FROM billing_operation_receipts WHERE id = :id",
        id=quorum["deployer"],
    )
    await _overwrite_receipt_column(plane, quorum["deployer"], "nonce", uuid4())
    await refused()
    await _overwrite_receipt_column(plane, quorum["deployer"], "nonce", deployer_nonce)

    # The untouched quorum still applies, exactly once.
    response = await plane.transition(
        material, expected_revision=0, canary_builder_id=builder_id
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "canary_allowlist"
    assert await plane.applied(material) == 3


# --------------------------------------------------------------------------
# H1: a quorum must be signed after the state it transitions from exists
# --------------------------------------------------------------------------

_STALE = "billing_rollout_quorum_stale"


async def _incident_and_recanary(plane: _ControlPlane, revision: int) -> int:
    """Incident-disable a canary, then reopen it; returns the new revision."""

    incident = _material(
        scope="incident", from_state="canary_allowlist", to_state="disabled"
    )
    await plane.quorum(incident)
    response = await plane.transition(
        incident, expected_revision=revision, incident=True
    )
    assert response.status_code == 200, response.text
    builder_id = await _canary_builder(plane)
    reopen = _material(
        from_state="disabled", to_state="canary_allowlist", canary_builder_id=builder_id
    )
    await plane.quorum(reopen)
    response = await plane.transition(
        reopen, expected_revision=revision + 1, canary_builder_id=builder_id
    )
    assert response.status_code == 200, response.text
    return revision + 2


async def _insert_readiness_past_the_service(
    plane: _ControlPlane,
    material: dict[str, Any],
    quorum: dict[str, str],
    *,
    rollout_revision: int,
) -> None:
    """Write all four readiness rows directly (no service checks)."""

    async with plane.sessions() as session:
        async with session.begin():
            for kind in _KINDS:
                session.add(
                    BillingRolloutReadinessReceipt(
                        environment=material["environment"],
                        receipt_kind=kind,
                        rollout_revision=rollout_revision,
                        status="passed",
                        content_sha256=_content_digest(
                            material,
                            kind=kind,
                            rollout_revision=rollout_revision,
                            deployer_receipt_id=quorum["deployer"],
                            reviewer_receipt_id=quorum["reviewer"],
                        ),
                        operation_id=UUID(material["operation_id"]),
                        operation_receipt_id=UUID(quorum["deployer"]),
                        provider_receipt_id=UUID(quorum["reviewer"]),
                        roster_revision=0,
                        rule_digest=_rule_digest(),
                        bundle_digest=billing_fact_bundle_digest(),
                        app_digest=material["deployment_digest"],
                        observed_at=datetime.now(timezone.utc),
                        expires_at=_operation(material).expires_at,
                    )
                )


async def test_h1_pre_incident_general_quorum_is_stale_after_recanary(
    plane: _ControlPlane,
) -> None:
    """Reviewer probe 5(a): a general quorum signed while ``disabled`` cannot
    be used after canary, incident-disable and a fresh re-canary, not even by
    a fourth super-admin who is in no quorum."""

    stale = _material(from_state="canary_allowlist", to_state="general")
    stale_quorum = await plane.quorum(stale)
    revision = await _open_canary(plane)
    current = await _incident_and_recanary(plane, revision)

    for kind in _KINDS:
        _assert_rejected(
            await plane.readiness(
                stale,
                kind=kind,
                rollout_revision=current,
                deployer_receipt_id=stale_quorum["deployer"],
                reviewer_receipt_id=stale_quorum["reviewer"],
                principal="outsider",
            ),
            _STALE,
        )
    assert await plane.readiness_rows(current) == 0
    # The transition refuses it too, even with readiness written past the
    # service, before anything is spent.
    await _insert_readiness_past_the_service(
        plane, stale, stale_quorum, rollout_revision=current
    )
    _assert_rejected(
        await plane.transition(stale, expected_revision=current, principal="outsider"),
        _STALE,
    )
    assert await plane.applied(stale) == 0
    readback = await plane.readback()
    assert (readback["state"], readback["rollout_revision"]) == (
        "canary_allowlist",
        current,
    )


async def test_h1_chain_single_principal_cannot_reuse_pre_incident_quorums(
    plane: _ControlPlane,
) -> None:
    """Reviewer probe 5(a) chain: pre-incident canary and general quorums are
    both stale once the incident-disable is the current state."""

    stale_builder = await _canary_builder(plane)
    stale_canary = _material(
        from_state="disabled",
        to_state="canary_allowlist",
        canary_builder_id=stale_builder,
    )
    await plane.quorum(stale_canary)
    stale_general = _material(from_state="canary_allowlist", to_state="general")
    stale_general_quorum = await plane.quorum(stale_general)
    revision = await _open_canary(plane)
    incident = _material(
        scope="incident", from_state="canary_allowlist", to_state="disabled"
    )
    await plane.quorum(incident)
    response = await plane.transition(
        incident, expected_revision=revision, incident=True
    )
    assert response.status_code == 200, response.text

    _assert_rejected(
        await plane.transition(
            stale_canary,
            expected_revision=revision + 1,
            canary_builder_id=stale_builder,
            principal="outsider",
        ),
        _STALE,
    )
    assert await plane.applied(stale_canary) == 0
    readback = await plane.readback()
    assert (readback["state"], readback["rollout_revision"]) == (
        "disabled",
        revision + 1,
    )

    # Even after a legitimate re-canary the stale general quorum is refused.
    reopen_builder = await _canary_builder(plane)
    reopen = _material(
        from_state="disabled",
        to_state="canary_allowlist",
        canary_builder_id=reopen_builder,
    )
    await plane.quorum(reopen)
    response = await plane.transition(
        reopen, expected_revision=revision + 1, canary_builder_id=reopen_builder
    )
    assert response.status_code == 200, response.text
    current = revision + 2
    _assert_rejected(
        await plane.readiness(
            stale_general,
            kind="readiness",
            rollout_revision=current,
            deployer_receipt_id=stale_general_quorum["deployer"],
            reviewer_receipt_id=stale_general_quorum["reviewer"],
            principal="outsider",
        ),
        _STALE,
    )
    assert await plane.readiness_rows(current) == 0
    assert await plane.applied(stale_general) == 0
    assert (await plane.readback())["rollout_revision"] == current


async def test_h1_quorum_signed_after_the_current_state_promotes(
    plane: _ControlPlane,
) -> None:
    """The legitimate flow: sign the next transition after the state exists."""

    revision = await _open_canary(plane)
    current = await _incident_and_recanary(plane, revision)
    assert await _promote_general(plane, current) == current + 1
    assert await plane.scalar(
        "SELECT bool_and(o.created_at > r.created_at) "
        "FROM billing_operation_receipts o, billing_rollout_receipts r "
        "WHERE r.resulting_revision = :current AND r.environment = :env "
        "AND o.to_state = 'general' AND o.applied_at IS NOT NULL",
        current=current,
        env=_ENVIRONMENT,
    )


async def _shift_quorum_created_at(
    plane: _ControlPlane, material: dict[str, Any], offset: timedelta
) -> None:
    """Set the quorum's ``created_at`` to the current rollout receipt's plus
    *offset*, bypassing the append-only trigger (superuser replica role)."""

    async with plane.sessions() as session:
        async with session.begin():
            await session.execute(text("SET LOCAL session_replication_role = replica"))
            result = await session.execute(
                text(
                    "UPDATE billing_operation_receipts o "
                    "SET created_at = r.created_at + :offset "
                    "FROM billing_rollout_control c "
                    "JOIN billing_rollout_receipts r ON r.id = c.rollout_receipt_id "
                    "WHERE c.environment = :env AND o.operation_id = :op"
                ),
                {
                    "offset": offset,
                    "env": material["environment"],
                    "op": material["operation_id"],
                },
            )
            assert result.rowcount == 3


async def _resign_quorum(
    plane: _ControlPlane, material: dict[str, Any], *, rollout_revision: int
) -> None:
    """Recompute the quorum's backend signatures for *rollout_revision* with
    the server key and store them past the append-only trigger: a probe of
    the layers behind the signature, never something a client can do."""

    async with plane.sessions() as session:
        async with session.begin():
            rows = (
                (
                    await session.execute(
                        select(BillingOperationReceipt).where(
                            BillingOperationReceipt.operation_id
                            == UUID(material["operation_id"]),
                            BillingOperationReceipt.evidence_type.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 3
            await session.execute(text("SET LOCAL session_replication_role = replica"))
            for row in rows:
                row.signature_sha256 = operation_receipt_signature(
                    row,
                    rollout_revision=rollout_revision,
                    signing_key=_SIGNING_KEY.encode(),
                )


async def test_h1_quorum_created_at_equal_to_the_rollout_receipt_is_stale(
    plane: _ControlPlane,
) -> None:
    """Both timestamps are the database ``now()``; equal is not strictly
    later, so it is stale.  One microsecond later is accepted.  (Rows are
    rewritten past the append-only trigger.)"""

    revision = await _open_canary(plane)
    material = _material(from_state="canary_allowlist", to_state="general")
    quorum = await plane.quorum(material)

    await _shift_quorum_created_at(plane, material, timedelta(0))
    _assert_rejected(
        await plane.readiness(
            material,
            kind="readiness",
            rollout_revision=revision,
            deployer_receipt_id=quorum["deployer"],
            reviewer_receipt_id=quorum["reviewer"],
        ),
        _STALE,
    )
    await _insert_readiness_past_the_service(
        plane, material, quorum, rollout_revision=revision
    )
    _assert_rejected(
        await plane.transition(material, expected_revision=revision), _STALE
    )
    assert await plane.applied(material) == 0
    assert (await plane.readback())["rollout_revision"] == revision

    await _shift_quorum_created_at(plane, material, timedelta(microseconds=1))
    response = await plane.transition(material, expected_revision=revision)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "general"


async def test_h1_readiness_service_boundary_one_microsecond_later_is_accepted(
    plane: _ControlPlane,
) -> None:
    revision = await _open_canary(plane)
    material = _material(from_state="canary_allowlist", to_state="general")
    quorum = await plane.quorum(material)
    await _shift_quorum_created_at(plane, material, timedelta(microseconds=1))
    await plane.record_kinds(material, quorum, rollout_revision=revision)
    assert await plane.readiness_rows(revision) == 4


async def test_h1_an_older_quorum_to_disabled_stays_allowed(
    plane: _ControlPlane,
) -> None:
    """Moving to ``disabled`` only reduces exposure: a rollback quorum signed
    before the current state still applies (see also D2)."""

    revision = await _open_canary(plane)
    rollback = _material(from_state="canary_allowlist", to_state="disabled")
    await plane.quorum(rollback)
    current = await _incident_and_recanary(plane, revision)
    response = await plane.transition(rollback, expected_revision=current)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "disabled"
    assert await plane.applied(rollback) == 3


# --------------------------------------------------------------------------
# HD: mixed quorums; freshness judges every spent row against the CURRENT
# state's rollout receipt (one rule for readiness and transition)
# --------------------------------------------------------------------------


async def _cycle_canary(plane: _ControlPlane, revision: int) -> int:
    """From ``disabled`` at *revision*: open a canary, roll it back."""

    builder_id = await _canary_builder(plane)
    opening = _material(
        from_state="disabled", to_state="canary_allowlist", canary_builder_id=builder_id
    )
    await plane.quorum(opening)
    response = await plane.transition(
        opening, expected_revision=revision, canary_builder_id=builder_id
    )
    assert response.status_code == 200, response.text
    rollback = _material(from_state="canary_allowlist", to_state="disabled")
    await plane.quorum(rollback)
    response = await plane.transition(rollback, expected_revision=revision + 1)
    assert response.status_code == 200, response.text
    return revision + 2


async def _assert_general_quorum_stale(
    plane: _ControlPlane,
    material: dict[str, Any],
    quorum: dict[str, str],
    revision: int,
) -> None:
    """Stale in readiness (every kind) and in the transition, nothing spent."""

    for kind in _KINDS:
        _assert_rejected(
            await plane.readiness(
                material,
                kind=kind,
                rollout_revision=revision,
                deployer_receipt_id=quorum["deployer"],
                reviewer_receipt_id=quorum["reviewer"],
            ),
            _STALE,
        )
    assert await plane.readiness_rows(revision) == 0
    await _insert_readiness_past_the_service(
        plane, material, quorum, rollout_revision=revision
    )
    _assert_rejected(
        await plane.transition(material, expected_revision=revision), _STALE
    )
    assert await plane.applied(material) == 0
    readback = await plane.readback()
    assert (readback["state"], readback["rollout_revision"]) == (
        "canary_allowlist",
        revision,
    )


async def test_hd_general_quorum_with_two_stale_and_one_fresh_receipt_is_stale(
    plane: _ControlPlane,
) -> None:
    revision = await _open_canary(plane)
    material = _material(from_state="canary_allowlist", to_state="general")
    quorum = {
        "deployer": await plane.signed("deployer", material, "deployer"),
        "approver": await plane.signed("approver", material, "approver"),
    }
    current = await _incident_and_recanary(plane, revision)
    quorum["reviewer"] = await plane.signed("reviewer", material, "reviewer")

    await _assert_general_quorum_stale(plane, material, quorum, current)


async def test_hd_general_quorum_with_only_the_approver_stale_is_stale(
    plane: _ControlPlane,
) -> None:
    """Readiness names only fresh deployer/reviewer receipts, but the quorum
    it binds (and the transition would spend) includes a stale approver."""

    revision = await _open_canary(plane)
    material = _material(from_state="canary_allowlist", to_state="general")
    approver = await plane.signed("approver", material, "approver")
    current = await _incident_and_recanary(plane, revision)
    quorum = {
        "deployer": await plane.signed("deployer", material, "deployer"),
        "approver": approver,
        "reviewer": await plane.signed("reviewer", material, "reviewer"),
    }

    await _assert_general_quorum_stale(plane, material, quorum, current)


async def test_hd_canary_quorum_with_one_fresh_and_two_stale_receipts_is_stale(
    plane: _ControlPlane,
) -> None:
    revision = await _open_canary(plane)
    rollback = _material(from_state="canary_allowlist", to_state="disabled")
    await plane.quorum(rollback)
    response = await plane.transition(rollback, expected_revision=revision)
    assert response.status_code == 200, response.text
    builder_id = await _canary_builder(plane)
    reopen = _material(
        from_state="disabled", to_state="canary_allowlist", canary_builder_id=builder_id
    )
    await plane.signed("deployer", reopen, "deployer")
    await plane.signed("reviewer", reopen, "reviewer")
    current = await _cycle_canary(plane, revision + 1)
    await plane.signed("approver", reopen, "approver")

    _assert_rejected(
        await plane.transition(
            reopen, expected_revision=current, canary_builder_id=builder_id
        ),
        _STALE,
    )
    assert await plane.applied(reopen) == 0
    readback = await plane.readback()
    assert (readback["state"], readback["rollout_revision"]) == ("disabled", current)


async def test_hd_freshness_is_judged_against_the_current_rollout_receipt(
    plane: _ControlPlane,
) -> None:
    """A quorum signed after the FIRST rollout receipt but before the current
    one is stale: the anchor is the receipt of the state that exists now."""

    revision = await _open_canary(plane)
    material = _material(from_state="canary_allowlist", to_state="general")
    quorum = await plane.quorum(material)
    current = await _incident_and_recanary(plane, revision)
    # Precondition: newer than the first rollout receipt, not the current one.
    assert await plane.scalar(
        "SELECT bool_and(o.created_at > f.created_at AND o.created_at <= c.created_at)"
        " FROM billing_operation_receipts o,"
        " (SELECT min(created_at) AS created_at FROM billing_rollout_receipts"
        "  WHERE environment = :env) f,"
        " billing_rollout_control k"
        " JOIN billing_rollout_receipts c ON c.id = k.rollout_receipt_id"
        " WHERE k.environment = :env AND o.operation_id = :op",
        env=_ENVIRONMENT,
        op=material["operation_id"],
    )

    await _assert_general_quorum_stale(plane, material, quorum, current)


# --------------------------------------------------------------------------
# M2: CURRENT fail-closed dead ends of one revision (characterization)
# --------------------------------------------------------------------------


async def test_characterization_rogue_fourth_receipt_strands_the_revision(
    plane: _ControlPlane,
) -> None:
    """CURRENT behaviour, pinned so it fails loudly once it is fixed.

    One extra self-assigned receipt on the operation whose pair already backs
    readiness makes that quorum unusable, and the revision refuses every other
    pair.  Recovery: rollback to ``disabled``, re-canary, fresh quorum.
    """

    revision = await _open_canary(plane)
    first = _material(from_state="canary_allowlist", to_state="general")
    first_quorum = await plane.quorum(first)
    await plane.record_kinds(
        first, first_quorum, rollout_revision=revision, kinds=("readiness",)
    )
    await plane.signed("rogue", first, "approver")

    _assert_rejected(
        await plane.readiness(
            first,
            kind="backlog",
            rollout_revision=revision,
            deployer_receipt_id=first_quorum["deployer"],
            reviewer_receipt_id=first_quorum["reviewer"],
        ),
        "billing_operation_quorum_incomplete",
    )
    second = _material(from_state="canary_allowlist", to_state="general")
    second_quorum = await plane.quorum(second)
    for kind in _KINDS:
        _assert_rejected(
            await plane.readiness(
                second,
                kind=kind,
                rollout_revision=revision,
                deployer_receipt_id=second_quorum["deployer"],
                reviewer_receipt_id=second_quorum["reviewer"],
            ),
            "billing_rollout_readiness_operation_conflict",
        )
    _assert_rejected(
        await plane.transition(second, expected_revision=revision),
        "billing_rollout_readiness_incomplete",
    )
    assert (await plane.readback())["state"] == "canary_allowlist"

    # Recovery.
    rollback = _material(from_state="canary_allowlist", to_state="disabled")
    await plane.quorum(rollback)
    response = await plane.transition(rollback, expected_revision=revision)
    assert response.status_code == 200, response.text
    reopen_builder = await _canary_builder(plane)
    reopen = _material(
        from_state="disabled",
        to_state="canary_allowlist",
        canary_builder_id=reopen_builder,
    )
    await plane.quorum(reopen)
    response = await plane.transition(
        reopen, expected_revision=revision + 1, canary_builder_id=reopen_builder
    )
    assert response.status_code == 200, response.text
    assert await _promote_general(plane, revision + 2) == revision + 3


async def test_characterization_expired_readiness_strands_the_revision(
    plane: _ControlPlane,
) -> None:
    """CURRENT behaviour, pinned so it fails loudly once it is fixed.

    After the first pair's readiness expires, a second, valid quorum is still
    refused at that revision (injected clock, service called directly).
    """

    revision = await _open_canary(plane)
    first = _material(from_state="canary_allowlist", to_state="general")
    first_quorum = await plane.quorum(first)
    await plane.record_kinds(first, first_quorum, rollout_revision=revision)
    base = datetime.now(timezone.utc).replace(microsecond=0)
    second = _material(
        from_state="canary_allowlist",
        to_state="general",
        expires_at=base + timedelta(hours=3),
    )
    second_quorum = await plane.quorum(second)
    late = _operation(first).expires_at + timedelta(seconds=1)
    operation = _operation(second)
    async with plane.sessions() as session:
        service = BillingRolloutOperationService(
            session, signing_key=_SIGNING_KEY.encode(), clock=lambda: late
        )
        with pytest.raises(
            BillingOperationRejected,
            match="^billing_rollout_readiness_operation_conflict$",
        ):
            await service.record_readiness_receipt(
                environment=_ENVIRONMENT,
                receipt_kind="readiness",
                rollout_revision=revision,
                roster_revision=0,
                status="passed",
                content_sha256=_content_digest(
                    second,
                    kind="readiness",
                    rollout_revision=revision,
                    deployer_receipt_id=second_quorum["deployer"],
                    reviewer_receipt_id=second_quorum["reviewer"],
                ),
                material=operation,
                operation_receipt_id=UUID(second_quorum["deployer"]),
                provider_receipt_id=UUID(second_quorum["reviewer"]),
                rule_digest=_rule_digest(),
                bundle_digest=billing_fact_bundle_digest(),
                app_digest=second["deployment_digest"],
                observed_at=late,
                expires_at=operation.expires_at,
                recorder_user_id=plane.actor("deployer"),
            )
        await session.rollback()
    assert await plane.readiness_rows(revision) == 4


# --------------------------------------------------------------------------
# L5: readiness conflicts are scoped to one environment
# --------------------------------------------------------------------------


async def test_l5_readiness_conflicts_do_not_cross_environments(
    plane: _ControlPlane,
) -> None:
    other_environment = "production"
    staging_revision = await _open_canary(plane)
    production_revision = await _open_canary(plane, environment=other_environment)
    assert staging_revision == production_revision

    staging = _material(from_state="canary_allowlist", to_state="general")
    staging_quorum = await plane.quorum(staging)
    await plane.record_kinds(staging, staging_quorum, rollout_revision=staging_revision)
    # Same revision number, another environment, another pair: not a conflict.
    production = _material(
        from_state="canary_allowlist",
        to_state="general",
        environment=other_environment,
    )
    production_quorum = await plane.quorum(production)
    await plane.record_kinds(
        production, production_quorum, rollout_revision=production_revision
    )
    assert await plane.readiness_rows(staging_revision) == 4
    assert (
        await plane.readiness_rows(production_revision, environment=other_environment)
        == 4
    )
    response = await plane.transition(production, expected_revision=production_revision)
    assert response.status_code == 200, response.text
    assert response.json()["environment"] == other_environment
    assert response.json()["state"] == "general"
    response = await plane.transition(staging, expected_revision=staging_revision)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "general"
