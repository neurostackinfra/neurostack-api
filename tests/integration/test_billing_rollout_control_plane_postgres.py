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
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
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
from src.models.billing import (
    BillingCheckoutSession,
    BillingOperationReceipt,
    BillingRolloutReadinessReceipt,
)
from src.repositories.billing import BillingRepository, billing_advisory_key
from src.repositories.database import get_db_session_dependency
from src.services.billing_catalog import BILLING_CATALOG, CATALOG_VERSION_ID
from src.services.billing_checkout import subscription_pricing_snapshot
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
from src.services.billing_webhook import BillingWebhookService
from tests.integration.test_asaas_billing_postgres import (
    _ROOT,
    _create_child,
    _drop_child,
    _evidence,
    _run_alembic,
    _validated_admin_url,
)
from tests.integration.test_asaas_webhook_postgres import _brl, _envelope, _payload

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
) -> str:
    return _readiness_content_digest(
        environment=material["environment"],
        receipt_kind=kind,
        rollout_revision=rollout_revision,
        operation_id=UUID(material["operation_id"]),
        operation_receipt_id=UUID(deployer_receipt_id),
        provider_receipt_id=UUID(reviewer_receipt_id),
        rule_digest=_rule_digest(),
        bundle_digest=billing_fact_bundle_digest(),
        app_digest=material["deployment_digest"],
    )


def _documented_payload(
    material: dict[str, Any],
    *,
    kind: str,
    rollout_revision: int,
    deployer_receipt_id: str,
    reviewer_receipt_id: str,
) -> dict[str, Any]:
    """The documented field list (``docs/runbooks/asaas-deployment.md``)."""

    return {
        "app_digest": material["deployment_digest"],
        "bundle_digest": billing_fact_bundle_digest(),
        "environment": material["environment"],
        "operation_id": str(UUID(material["operation_id"])),
        "operation_receipt_id": str(UUID(deployer_receipt_id)),
        "provider_receipt_id": str(UUID(reviewer_receipt_id)),
        "receipt_kind": kind,
        "rollout_revision": rollout_revision,
        "rule_digest": _rule_digest(),
    }


def _sha256_canonical(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _legacy_content_digest(
    material: dict[str, Any],
    *,
    kind: str,
    rollout_revision: int,
    deployer_receipt_id: str,
    reviewer_receipt_id: str,
    roster_revision: int,
) -> str:
    """The pre-E3 digest: the documented fields plus ``roster_revision``."""

    payload = _documented_payload(
        material,
        kind=kind,
        rollout_revision=rollout_revision,
        deployer_receipt_id=deployer_receipt_id,
        reviewer_receipt_id=reviewer_receipt_id,
    )
    return _sha256_canonical({**payload, "roster_revision": roster_revision})


# Clock seam.  Operation and rollout receipts are stamped by the database
# (``created_at`` is the inserting transaction's ``now()``), readiness
# ``observed_at`` by this process, and the service judges both against its own
# wall clock: a quorum must be newer than the rollout receipt of the current
# state, and ``created_at <= observed_at <= now``.  CLOCK_REALTIME is not
# monotonic: on the WSL2 hosts these suites run on it steps BACK by ~70-90 ms
# every 30 s (time sync; measured on 2026-10-19).  Evidence produced within
# that window after the row it must follow then predates it and is refused
# (``billing_rollout_readiness_receipt_invalid``,
# ``billing_rollout_readiness_expired``, ``billing_rollout_quorum_stale``):
# an intermittent 409 that passes alone.  The harness therefore waits until
# its wall clock is a margin past the database stamp before it signs a
# receipt or observes readiness, and observes 1 us after the receipts it
# names, so a later step cannot reorder them.
_CLOCK_STEP_MARGIN = timedelta(milliseconds=250)


@dataclass
class _ControlPlane:
    """Drive the admin billing routes as named super-admin principals."""

    client: httpx.AsyncClient
    sessions: async_sessionmaker[AsyncSession]
    actors: dict[str, UUID] = field(default_factory=dict)

    def actor(self, principal: str) -> UUID:
        return self.actors.setdefault(principal, uuid4())

    async def clock_past(self, sql: str, **params: Any) -> datetime | None:
        """Wait until this process's wall clock is ``_CLOCK_STEP_MARGIN`` past
        the database stamp *sql* selects (if any); return that stamp."""

        stamp = await self.scalar(sql, **params)
        if stamp is not None:
            while datetime.now(timezone.utc) <= stamp + _CLOCK_STEP_MARGIN:
                await asyncio.sleep(0.01)
        return stamp

    async def observed_after(self, *receipt_ids: str) -> datetime:
        """A readiness ``observed_at`` 1 us after the named receipts."""

        stamp = await self.clock_past(
            "SELECT max(created_at) FROM billing_operation_receipts "
            "WHERE id::text = ANY(CAST(:ids AS text[]))",
            ids=list(receipt_ids),
        )
        if stamp is None:  # receipts that do not exist: nothing to follow
            return datetime.now(timezone.utc)
        return stamp + timedelta(microseconds=1)

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

        # Newer than the rollout receipt of the current state (clock seam).
        await self.clock_past(
            "SELECT max(created_at) FROM billing_rollout_receipts "
            "WHERE environment = :env",
            env=material.get("environment", _ENVIRONMENT),
        )
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
        principal: str | None = None,
        content_sha256: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Record one readiness row; by default as the duty that owns *kind*."""

        body = {
            # ``BillingRolloutReadinessReceiptRequest`` inherits the material
            # fields; its own ``expires_at`` is the material's expiry too.
            **material,
            "receipt_kind": kind,
            "rollout_revision": rollout_revision,
            "status": "passed",
            "content_sha256": content_sha256
            or _content_digest(
                material,
                kind=kind,
                rollout_revision=rollout_revision,
                deployer_receipt_id=deployer_receipt_id,
                reviewer_receipt_id=reviewer_receipt_id,
            ),
            "operation_receipt_id": deployer_receipt_id,
            "provider_receipt_id": reviewer_receipt_id,
            "rule_digest": _rule_digest(),
            "bundle_digest": billing_fact_bundle_digest(),
            "app_digest": material["deployment_digest"],
            "observed_at": (
                await self.observed_after(deployer_receipt_id, reviewer_receipt_id)
            ).isoformat(),
            **(extra or {}),
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
                observed_at=await plane.observed_after(
                    quorum["deployer"], quorum["reviewer"]
                ),
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
    legacy_roster_revision: int | None = None,
) -> None:
    """Write all four readiness rows directly (no service checks).

    *legacy_roster_revision* computes the pre-E3 digest instead (the column
    itself left with migration 036).
    """

    observed_at = await plane.observed_after(quorum["deployer"], quorum["reviewer"])
    async with plane.sessions() as session:
        async with session.begin():
            for kind in _KINDS:
                content = (
                    _content_digest(
                        material,
                        kind=kind,
                        rollout_revision=rollout_revision,
                        deployer_receipt_id=quorum["deployer"],
                        reviewer_receipt_id=quorum["reviewer"],
                    )
                    if legacy_roster_revision is None
                    else _legacy_content_digest(
                        material,
                        kind=kind,
                        rollout_revision=rollout_revision,
                        deployer_receipt_id=quorum["deployer"],
                        reviewer_receipt_id=quorum["reviewer"],
                        roster_revision=legacy_roster_revision,
                    )
                )
                session.add(
                    BillingRolloutReadinessReceipt(
                        environment=material["environment"],
                        receipt_kind=kind,
                        rollout_revision=rollout_revision,
                        status="passed",
                        content_sha256=content,
                        operation_id=UUID(material["operation_id"]),
                        operation_receipt_id=UUID(quorum["deployer"]),
                        provider_receipt_id=UUID(quorum["reviewer"]),
                        rule_digest=_rule_digest(),
                        bundle_digest=billing_fact_bundle_digest(),
                        app_digest=material["deployment_digest"],
                        observed_at=observed_at,
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


# --------------------------------------------------------------------------
# E3 (Group 10): readiness and ``general`` no longer bind the legacy roster
# --------------------------------------------------------------------------

# Inventory for Group 10 E5/E6, same convention as the characterization
# module (tests/integration/test_asaas_subscription_triggers_postgres.py):
# pytest node names, checked by test_break_inventory_names_only_collected_tests
# below.  A test may appear in both lists when it breaks for both reasons.
#
# 036 is applied: E5 removed billing_rollout_readiness_receipts.roster_revision
# (and its model field) from the twelve tests it listed, directly or through
# _insert_readiness_past_the_service, and moved the "stored value is inert"
# test to "the column is gone".
TESTS_THAT_BREAK_AFTER_036: tuple[str, ...] = ()
# 037 is applied: it dropped billing_roster_control.  E6 removed
# _advance_roster_revision (it moved the global revision 0 -> 1) and turned
# test_e3_readiness_ignores_the_roster_and_promotes into
# test_e3_readiness_promotes_without_any_roster (no table to move);
# _assert_roster_fence_free became _assert_roster_key_free (the advisory key
# is still never taken; the row it also locked no longer exists), so the two
# lock-order tests keep their parametrizations.
TESTS_THAT_BREAK_AFTER_037: tuple[str, ...] = ()


async def test_break_inventory_names_only_collected_tests(
    request: pytest.FixtureRequest,
) -> None:
    """Every inventory entry is a real node name of this module (not stale)."""

    module = request.node.parent
    assert isinstance(module, pytest.Module)
    collected = {item.name for item in module.collect()}
    for listed in (TESTS_THAT_BREAK_AFTER_036, TESTS_THAT_BREAK_AFTER_037):
        assert len(set(listed)) == len(listed)
        assert sorted(set(listed) - collected) == []


# The global roster key the migration 026 triggers took (the Python mirror
# was removed in Group 10 E4; migration 036 removed the trigger on
# billing_subscriptions, 037 the legacy tables, their triggers and
# lock_billing_roster_statement()).
_ROSTER_KEY = 3486705124568449331
_HELD_ADVISORY = (
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
    "AND pid = pg_backend_pid() AND objsubid = 1 "
    "AND classid::bigint = ((CAST(:key AS bigint) >> 32) & 4294967295) "
    "AND objid::bigint = (CAST(:key AS bigint) & 4294967295)"
)


async def _held_advisory(session: AsyncSession, key: int) -> int:
    """Advisory locks on *key* that *session*'s own backend holds now."""

    return int(await session.scalar(text(_HELD_ADVISORY), {"key": key}))


def _signed_key(high: int, low: int) -> int:
    """The bigint advisory key pg_locks splits into ``classid``/``objid``."""

    key = (high << 32) | low
    return key - 2**64 if key >= 2**63 else key


async def _wait_until_waiting(
    plane: _ControlPlane, pid: int, task: asyncio.Task[Any]
) -> list[tuple[str, int | None]]:
    """Wait until backend *pid* blocks; return its ungranted locks as
    ``(locktype, advisory key or None)``."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 20
    while True:
        async with plane.sessions() as probe:
            rows = (
                await probe.execute(
                    text(
                        "SELECT locktype, classid::bigint AS high, "
                        "objid::bigint AS low, objsubid "
                        "FROM pg_locks WHERE pid = :pid AND NOT granted"
                    ),
                    {"pid": pid},
                )
            ).all()
        if rows:
            return [
                (
                    row.locktype,
                    (
                        _signed_key(int(row.high), int(row.low))
                        if row.locktype == "advisory" and row.objsubid == 1
                        else None
                    ),
                )
                for row in rows
            ]
        if task.done():
            task.result()
            raise AssertionError("finished without waiting on the rollout lock")
        assert loop.time() < deadline, "never waited on the held rollout lock"
        await asyncio.sleep(0.05)


async def _assert_lockable(
    plane: _ControlPlane, sql: str, expected: int, **params: Any
) -> None:
    """Nobody holds a row lock on *sql*'s rows (``NOWAIT`` would raise)."""

    async with plane.sessions() as probe:
        rows = (await probe.execute(text(f"{sql} FOR UPDATE NOWAIT"), params)).all()
        assert len(rows) == expected
        await probe.rollback()


async def _assert_roster_key_free(plane: _ControlPlane) -> None:
    """Nobody holds the global roster advisory key (since migration 037 the
    roster row it used to guard does not exist)."""

    async with plane.sessions() as probe:
        assert (
            await probe.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _ROSTER_KEY}
            )
            is True
        )
        assert (
            await probe.scalar(text("SELECT to_regclass('billing_roster_control')"))
            is None
        )
        await probe.rollback()


# The webhook side of the lock-order proofs below is a REAL delivery through
# ``BillingWebhookService.receive`` (Group 10 E4), paused right after its
# ``lock_rollout_control`` and then let run to its commit.  A handoff
# (SUBSCRIPTION_CREATED) locks the checkout row and inserts the subscription;
# a deletion locks the subscription row and UPDATEs it, which took the
# database's roster statement fence until migration 036 dropped that trigger.
# Neither the webhook nor (since 036) the database takes the roster fence.
_E4_DUE = date(2026, 10, 11)
_E4_SUBSCRIPTION = "sub_e4_lock_order"
_FENCE_TRIGGER = (
    "SELECT count(*) FROM pg_trigger "
    "WHERE tgname = 'trg_billing_subscriptions_roster_fence' AND NOT tgisinternal"
)


def _subscription_event(event: str, checkout_id: UUID, amount_cents: int) -> Any:
    return _payload(
        event,
        {
            "object": "subscription",
            "id": _E4_SUBSCRIPTION,
            "value": Decimal(_brl(amount_cents)),
            "cycle": "MONTHLY",
            "nextDueDate": _E4_DUE.isoformat(),
            "status": "ACTIVE" if event == "SUBSCRIPTION_CREATED" else "INACTIVE",
            "deleted": event == "SUBSCRIPTION_DELETED",
            "externalReference": str(checkout_id),
        },
    )


async def _webhook_payload(plane: _ControlPlane, event: str) -> dict[str, Any]:
    """Seed what *event* needs and return its payload.

    A live non-canary subscription checkout of a fresh builder, as Group 2
    reserves it; for ``SUBSCRIPTION_DELETED`` its subscription is first
    handed off by a committed SUBSCRIPTION_CREATED delivery.
    """

    builder_id = uuid4()
    snapshot = subscription_pricing_snapshot(
        BILLING_CATALOG["plan_plus_monthly_v1"], active_clinics=1
    )
    amount = int(snapshot["amount_cents"])  # type: ignore[call-overload]
    checkout = BillingCheckoutSession(
        id=uuid4(),
        builder_id=builder_id,
        intent_kind="subscription",
        intent_id=uuid4(),
        topup_order_id=None,
        attempt_no=1,
        provider="asaas",
        external_checkout_id=f"chk_e4_{uuid4().hex[:16]}",
        state="active",
        catalog_version_id=CATALOG_VERSION_ID,
        offer_id="plan_plus_monthly_v1",
        amount_cents=amount,
        pricing_snapshot=snapshot,
        next_due_date=_E4_DUE,
    )
    async with plane.sessions() as session:
        async with session.begin():
            await session.execute(
                text("INSERT INTO builders (id, name) VALUES (:id, 'E4 webhook')"),
                {"id": builder_id},
            )
            session.add(checkout)
    if event == "SUBSCRIPTION_DELETED":
        async with plane.sessions() as session:
            created = await BillingWebhookService(
                session, environment=_ENVIRONMENT
            ).receive(
                _envelope(
                    _subscription_event("SUBSCRIPTION_CREATED", checkout.id, amount)
                )
            )
        assert created.outcome == "processed"
    return _subscription_event(event, checkout.id, amount)


@dataclass
class _PausedWebhook:
    pid: int
    task: asyncio.Task[Any]
    resume: asyncio.Event
    # Advisory keys the delivery's own backend held right before its commit.
    held_at_commit: list[set[int]]


@asynccontextmanager
async def _webhook_paused_after_rollout(
    plane: _ControlPlane, payload: dict[str, Any]
) -> AsyncIterator[_PausedWebhook]:
    """Run one real delivery; pause it once it holds the rollout lock."""

    async with plane.sessions() as session:
        # One transaction from here to the delivery's commit: the backend pid
        # stays the delivery's, and a lock wait fails instead of hanging.
        pid = int(await session.scalar(text("SELECT pg_backend_pid()")))
        await session.execute(text("SET LOCAL lock_timeout = '10s'"))
        service = BillingWebhookService(session, environment=_ENVIRONMENT)
        repository = service._repo
        lock_rollout = repository.lock_rollout_control
        commit = session.commit
        holding = asyncio.Event()
        resume = asyncio.Event()
        held_at_commit: list[set[int]] = []

        async def paused_lock_rollout(*, environment: str) -> Any:
            control = await lock_rollout(environment=environment)
            holding.set()
            await resume.wait()
            return control

        async def recording_commit() -> None:
            rows = await session.execute(
                text(
                    "SELECT classid::bigint, objid::bigint FROM pg_locks "
                    "WHERE pid = pg_backend_pid() AND locktype = 'advisory' "
                    "AND granted AND objsubid = 1"
                )
            )
            held_at_commit.append({_signed_key(high, low) for high, low in rows})
            await commit()

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(repository, "lock_rollout_control", paused_lock_rollout)
            patch.setattr(session, "commit", recording_commit)
            task = asyncio.create_task(service.receive(_envelope(payload)))
            try:
                waiter = asyncio.ensure_future(holding.wait())
                await asyncio.wait(
                    {task, waiter}, timeout=20, return_when=asyncio.FIRST_COMPLETED
                )
                waiter.cancel()
                if task.done():
                    task.result()
                    raise AssertionError("the delivery never paused on its lock")
                assert holding.is_set(), "the delivery never took the rollout lock"
                yield _PausedWebhook(
                    pid=pid, task=task, resume=resume, held_at_commit=held_at_commit
                )
            finally:
                resume.set()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 30)


async def _finish_webhook(
    plane: _ControlPlane, webhook: _PausedWebhook, event: str
) -> None:
    """Let the paused delivery run its whole path; it must not wait on the
    worker queued behind its rollout lock."""

    webhook.resume.set()
    outcome = await asyncio.wait_for(webhook.task, 30)
    assert outcome.outcome == (
        "processed" if event == "SUBSCRIPTION_CREATED" else "readback_required"
    ), outcome
    (held,) = webhook.held_at_commit
    assert billing_advisory_key(_ENVIRONMENT) in held
    # 035's statement trigger took the roster key on the deletion's UPDATE;
    # migration 036 dropped it, so no event holds the key any more.
    assert int(await plane.scalar(_FENCE_TRIGGER)) == 0
    assert _ROSTER_KEY not in held


async def test_e3_readiness_request_with_roster_revision_is_422_before_anything(
    plane: _ControlPlane,
) -> None:
    revision = await _open_canary(plane)
    material = _material(from_state="canary_allowlist", to_state="general")
    quorum = await plane.quorum(material)

    for value in (0, 1):
        response = await plane.readiness(
            material,
            kind="readiness",
            rollout_revision=revision,
            deployer_receipt_id=quorum["deployer"],
            reviewer_receipt_id=quorum["reviewer"],
            extra={"roster_revision": value},
        )
        assert response.status_code == 422, response.text
        assert [
            (error["loc"], error["type"]) for error in response.json()["detail"]
        ] == [(["body", "roster_revision"], "extra_forbidden")]
    assert await plane.readiness_rows(revision) == 0

    response = await plane.readiness(
        material,
        kind="readiness",
        rollout_revision=revision,
        deployer_receipt_id=quorum["deployer"],
        reviewer_receipt_id=quorum["reviewer"],
    )
    assert response.status_code == 201, response.text
    assert "roster_revision" not in response.json()
    assert await plane.readiness_rows(revision) == 1


async def test_e3_readiness_promotes_without_any_roster(
    plane: _ControlPlane,
) -> None:
    """Readiness never checked nor copied the global roster revision (E3),
    since migration 036 its row has no roster column and since 037 there is
    no roster at all: the receipts are recorded with the documented digest,
    re-derived from the field list, and ``general`` is reached."""

    assert await plane.scalar("SELECT to_regclass('billing_roster_control')") is None
    revision = await _open_canary(plane)
    material = _material(from_state="canary_allowlist", to_state="general")
    quorum = await plane.quorum(material)
    await plane.record_kinds(material, quorum, rollout_revision=revision)

    async with plane.sessions() as session:
        rows = (
            (
                await session.execute(
                    select(BillingRolloutReadinessReceipt).where(
                        BillingRolloutReadinessReceipt.environment == _ENVIRONMENT,
                        BillingRolloutReadinessReceipt.rollout_revision == revision,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert sorted(row.receipt_kind for row in rows) == sorted(_KINDS)
    for row in rows:
        assert row.content_sha256 == _sha256_canonical(
            _documented_payload(
                material,
                kind=row.receipt_kind,
                rollout_revision=revision,
                deployer_receipt_id=quorum["deployer"],
                reviewer_receipt_id=quorum["reviewer"],
            )
        )

    # Replaying any recorded row is still refused and changes nothing.
    for kind in _KINDS:
        _assert_rejected(
            await plane.readiness(
                material,
                kind=kind,
                rollout_revision=revision,
                deployer_receipt_id=quorum["deployer"],
                reviewer_receipt_id=quorum["reviewer"],
            ),
            "billing_rollout_readiness_duplicate",
        )
    assert await plane.readiness_rows(revision) == 4

    response = await plane.transition(material, expected_revision=revision)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "general"
    assert await plane.applied(material) == 3


async def test_e3_a_digest_that_hashes_roster_revision_never_verifies(
    plane: _ControlPlane,
) -> None:
    """Receipts issued before E3 hashed ``roster_revision``; none verifies."""

    revision = await _open_canary(plane)
    material = _material(from_state="canary_allowlist", to_state="general")
    quorum = await plane.quorum(material)

    for roster_revision in (0, 1):
        _assert_rejected(
            await plane.readiness(
                material,
                kind="readiness",
                rollout_revision=revision,
                deployer_receipt_id=quorum["deployer"],
                reviewer_receipt_id=quorum["reviewer"],
                content_sha256=_legacy_content_digest(
                    material,
                    kind="readiness",
                    rollout_revision=revision,
                    deployer_receipt_id=quorum["deployer"],
                    reviewer_receipt_id=quorum["reviewer"],
                    roster_revision=roster_revision,
                ),
            ),
            "billing_rollout_readiness_digest_invalid",
        )
    assert await plane.readiness_rows(revision) == 0

    # A full pre-E3 set, stored with the old (legacy) digest, is refused by
    # the transition before anything is spent.
    await _insert_readiness_past_the_service(
        plane, material, quorum, rollout_revision=revision, legacy_roster_revision=0
    )
    _assert_rejected(
        await plane.transition(material, expected_revision=revision),
        "billing_rollout_readiness_unbound",
    )
    assert await plane.applied(material) == 0
    assert (await plane.readback())["state"] == "canary_allowlist"

    # The revision cannot be completed again: every kind is taken by that
    # pair (unique environment, kind, revision; one pair per revision).
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
    assert await plane.readiness_rows(revision) == 4

    # The recovery the runbook documents: disable, re-open the canary (a new
    # revision), then a fresh general quorum and all four kinds again.
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
    assert await _promote_general(plane, revision + 2) == revision + 3
    assert await plane.applied(material) == 0


async def test_e3_readiness_rows_carry_no_roster_revision_since_036(
    plane: _ControlPlane,
) -> None:
    """Decided behaviour (E3): the roster revision is not readiness evidence.
    E3 kept the column (constant 0, never read) until migration 036, which
    dropped it with its CHECK (``>= 0``); rows written past the service bind
    on the documented fields alone."""

    assert (
        await plane.scalar(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'billing_rollout_readiness_receipts' "
            "AND column_name = 'roster_revision'"
        )
        == 0
    )
    assert (
        await plane.scalar(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conname = 'ck_billing_rollout_readiness_roster_revision'"
        )
        == 0
    )
    assert "roster_revision" not in BillingRolloutReadinessReceipt.__table__.columns

    revision = await _open_canary(plane)
    material = _material(from_state="canary_allowlist", to_state="general")
    quorum = await plane.quorum(material)
    await _insert_readiness_past_the_service(
        plane, material, quorum, rollout_revision=revision
    )
    response = await plane.transition(material, expected_revision=revision)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "general"
    assert await plane.applied(material) == 3


_WEBHOOK_EVENTS = pytest.mark.parametrize(
    "event", ["SUBSCRIPTION_CREATED", "SUBSCRIPTION_DELETED"]
)


@_WEBHOOK_EVENTS
async def test_e3_readiness_locks_rollout_first_and_never_the_roster_fence(
    plane: _ControlPlane, event: str
) -> None:
    """Readiness takes ``lock_rollout_control`` before anything else and the
    roster fence never.  A real webhook delivery that holds the rollout lock
    completes its whole path (row locks; until migration 036, for a deletion,
    also the database's roster statement fence) while readiness waits behind
    it; the old readiness order (roster fence, then rollout) deadlocked
    against it."""

    revision = await _open_canary(plane)
    material = _material(from_state="canary_allowlist", to_state="general")
    quorum = await plane.quorum(material)
    await plane.record_kinds(
        material, quorum, rollout_revision=revision, kinds=_KINDS[:3]
    )
    operation = _operation(material)
    payload = await _webhook_payload(plane, event)

    async with (
        _webhook_paused_after_rollout(plane, payload) as webhook,
        plane.sessions() as worker,
    ):
        pid = int(await worker.scalar(text("SELECT pg_backend_pid()")))
        service = BillingRolloutOperationService(
            worker, signing_key=_SIGNING_KEY.encode()
        )
        task = asyncio.create_task(
            service.record_readiness_receipt(
                environment=_ENVIRONMENT,
                receipt_kind="smoke",
                rollout_revision=revision,
                status="passed",
                content_sha256=_content_digest(
                    material,
                    kind="smoke",
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
                observed_at=await plane.observed_after(
                    quorum["deployer"], quorum["reviewer"]
                ),
                expires_at=operation.expires_at,
                recorder_user_id=plane.actor("reviewer"),
            )
        )
        try:
            # It waits on the per-environment advisory key, the first step of
            # ``lock_rollout_control``: not even the control row came first.
            assert await _wait_until_waiting(plane, pid, task) == [
                ("advisory", billing_advisory_key(_ENVIRONMENT))
            ]
            # Waiting on the rollout lock, it holds nothing it locks later.
            await _assert_lockable(
                plane,
                "SELECT id FROM billing_operation_receipts WHERE operation_id = :op",
                3,
                op=material["operation_id"],
            )
            await _assert_roster_key_free(plane)
            await _finish_webhook(plane, webhook, event)
        finally:
            webhook.resume.set()
            (outcome,) = await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True), 30
            )
        if isinstance(outcome, BaseException):
            raise outcome
        assert outcome.receipt_kind == "smoke"
        assert await _held_advisory(worker, billing_advisory_key(_ENVIRONMENT)) == 1
        assert await _held_advisory(worker, _ROSTER_KEY) == 0
        await _assert_roster_key_free(plane)
        await worker.commit()

    assert await plane.readiness_rows(revision) == 4
    response = await plane.transition(material, expected_revision=revision)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "general"


@_WEBHOOK_EVENTS
async def test_e3_general_locks_rollout_first_and_never_the_roster_fence(
    plane: _ControlPlane, event: str
) -> None:
    """Lock order of ``general``: rollout control (advisory + row), then the
    readiness rows, then the quorum receipts; the roster fence never.  A real
    webhook delivery that holds the rollout lock completes while ``general``
    waits; the old order (roster fence, readiness rows, rollout) deadlocked
    against it."""

    revision = await _open_canary(plane)
    material = _material(from_state="canary_allowlist", to_state="general")
    quorum = await plane.quorum(material)
    await plane.record_kinds(material, quorum, rollout_revision=revision)
    operation = _operation(material)
    payload = await _webhook_payload(plane, event)

    async with (
        _webhook_paused_after_rollout(plane, payload) as webhook,
        plane.sessions() as worker,
    ):
        pid = int(await worker.scalar(text("SELECT pg_backend_pid()")))
        service = BillingRolloutOperationService(
            worker, signing_key=_SIGNING_KEY.encode()
        )
        task = asyncio.create_task(
            service.transition(expected_revision=revision, material=operation)
        )
        try:
            # The advisory key first, as the webhook takes it: a control-row
            # lock taken before it deadlocks against a webhook that holds the
            # advisory key and then asks for the row.
            assert await _wait_until_waiting(plane, pid, task) == [
                ("advisory", billing_advisory_key(_ENVIRONMENT))
            ]
            await _assert_lockable(
                plane,
                "SELECT id FROM billing_rollout_readiness_receipts "
                "WHERE environment = :env AND rollout_revision = :rev",
                4,
                env=_ENVIRONMENT,
                rev=revision,
            )
            await _assert_lockable(
                plane,
                "SELECT id FROM billing_operation_receipts WHERE operation_id = :op",
                3,
                op=material["operation_id"],
            )
            await _assert_roster_key_free(plane)
            await _finish_webhook(plane, webhook, event)
        finally:
            webhook.resume.set()
            (outcome,) = await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True), 30
            )
        if isinstance(outcome, BaseException):
            raise outcome
        assert outcome.state == "general"
        assert await _held_advisory(worker, billing_advisory_key(_ENVIRONMENT)) == 1
        assert await _held_advisory(worker, _ROSTER_KEY) == 0
        await _assert_roster_key_free(plane)
        await worker.commit()

    assert (await plane.readback())["state"] == "general"
    assert await plane.applied(material) == 3


async def test_e3_readiness_without_a_control_row_keeps_its_typed_refusal(
    plane: _ControlPlane,
) -> None:
    """Readiness now locks through ``lock_rollout_control``, which raises
    ``BillingStateUnavailable`` for a missing row; the route keeps the typed
    409 readiness answered before E3, never a 500.  (Row deleted past the
    history trigger.)"""

    async with plane.sessions() as session:
        async with session.begin():
            await session.execute(text("SET LOCAL session_replication_role = replica"))
            result = await session.execute(
                text("DELETE FROM billing_rollout_control WHERE environment = 'test'")
            )
            assert result.rowcount == 1
    material = _material(
        from_state="canary_allowlist", to_state="general", environment="test"
    )
    _assert_rejected(
        await plane.readiness(
            material,
            kind="readiness",
            rollout_revision=0,
            deployer_receipt_id=str(uuid4()),
            reviewer_receipt_id=str(uuid4()),
        ),
        "billing_rollout_readiness_revision_invalid",
    )
    assert await plane.readiness_rows(0, environment="test") == 0


async def test_e3_general_without_a_control_row_is_a_conflict_before_any_read(
    plane: _ControlPlane,
) -> None:
    """The rollout lock is the first step of every transition, so ``general``
    with a missing control row stops there with the route's generic 409 for
    ``BillingStateUnavailable`` (before E3 the readiness read came first and
    answered ``billing_rollout_readiness_incomplete``).  (Row deleted past the
    history trigger.)"""

    async with plane.sessions() as session:
        async with session.begin():
            await session.execute(text("SET LOCAL session_replication_role = replica"))
            result = await session.execute(
                text("DELETE FROM billing_rollout_control WHERE environment = 'test'")
            )
            assert result.rowcount == 1
    material = _material(
        from_state="canary_allowlist", to_state="general", environment="test"
    )
    _assert_rejected(await plane.transition(material, expected_revision=0), _CONFLICT)
    assert await plane.applied(material) == 0


async def test_e3_general_reads_its_clock_only_after_the_rollout_lock(
    plane: _ControlPlane,
) -> None:
    """``now`` (readiness freshness) is read once the rollout lock is held, so
    a transition that waited judges readiness at the time it can act.  The
    injected clock records whether the holder had already let go."""

    revision = await _open_canary(plane)
    material = _material(from_state="canary_allowlist", to_state="general")
    quorum = await plane.quorum(material)
    await plane.record_kinds(material, quorum, rollout_revision=revision)
    released = {"value": False}
    reads: list[bool] = []

    def clock() -> datetime:
        reads.append(released["value"])
        return datetime.now(timezone.utc)

    async with plane.sessions() as holder, plane.sessions() as worker:
        await BillingRepository(holder).lock_rollout_control(environment=_ENVIRONMENT)
        pid = int(await worker.scalar(text("SELECT pg_backend_pid()")))
        service = BillingRolloutOperationService(
            worker, signing_key=_SIGNING_KEY.encode(), clock=clock
        )
        task = asyncio.create_task(
            service.transition(
                expected_revision=revision, material=_operation(material)
            )
        )
        try:
            assert await _wait_until_waiting(plane, pid, task) == [
                ("advisory", billing_advisory_key(_ENVIRONMENT))
            ]
            assert reads == []
        finally:
            released["value"] = True
            await holder.rollback()
            (outcome,) = await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True), 30
            )
        if isinstance(outcome, BaseException):
            raise outcome
        assert outcome.state == "general"
        assert reads and all(reads)
        await worker.commit()
