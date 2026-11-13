"""The courtesy rule's backend surfaces over HTTP on real PostgreSQL
(debito-cortesia-automatico Group 5a).

The real application (lifespan, middleware, the process pool) on a fresh
database copied from one migrated template (head), with
``credit_hot_path_enabled`` on and the REAL credit switch reading (the 046
and 047 gates open on the ``alembic_version`` the lifespan reads).  No
``credit_enforcement_at``: the rule enforces a builder that received
courtesy, has no mark, from its first grant.

* A builder under the rule, ``charged``: ``/v1/credits/state`` is ``active``
  and ``enforced`` with every deadline null; its uploads (RAG JSON and
  multipart, media; by API key and from the panel), search and download are
  served and paid from courtesy only -- also under an ASAAS quarantine
  (``quarantine`` stays a reason, the state stays ``active``, a paid lot is
  never touched, no debt is created) and with a binding coverage that is not
  usable (never ``reconciliation``).
* ``blocked`` ("Tirar tudo", no paid plan): ``suspended`` with every
  deadline null; every upload is ``402 credit_suspended`` before anything is
  written (no row of any kind); API key search and download are ``402
  credit_suspended``; the panel's reads, its download and the export stay
  open; a new courtesy unblocks.
* An episode a mark era left open shows neither its state nor its
  deadlines, and gates nothing, under the rule.
* ``/v1/credits/thresholds`` on the courtesy basis after real consumption,
  with the alert the lifecycle recorded on ``c:{grant_effect_id}``.
* master-admin ``courtesy_rule``: the request path's reader, builder by
  builder, over a matrix of plan-in-force cases, from one batch read of the
  lots and one of the paid plans; null for everyone with the global set.
* A builder enforced by a mark, or by the global, that also holds courtesy
  keeps that enforcement's ``/v1/credits/*`` figures (``grace`` with its
  deadlines, the debt, the episode arm's alerts) and is not under the rule.
* Dominance: a builder that never had courtesy and a marked one answer the
  same with the rule's read open and closed; the marked one's upload is
  charged as a mark (a paid lot pays), the plain one's stays observational.

Needs ``POSTGRES_SMOKE_DATABASE_URL``; without it the module skips, and only
``scripts/check-credit-integration.sh`` turns that skip into a failure.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from sqlalchemy import text

from src.api.middleware.auth import pwd_context
from src.api.routes import admin as admin_routes
from src.repositories.credit import CreditRepository
from src.services import credit_inventory, credit_metering
from src.services.credit_ledger import CreditLedger
from src.services.credit_lifecycle import courtesy_arm_key
from tests.integration.credit_rollout_world import (
    civil_today,
    clean,
    effective_of,
    execute,
    force_credit_switch,
    forget_enforcement_latches,
    grant_courtesy,
    make_builder,
    make_cycle,
    make_monthly_grant,
    make_subscription,
    make_topup,
    paid_lot_moves,
    plant,
    record_mark,
    remove_courtesy,
    reverse_binding,
    scalar,
    set_subscription_status,
    template,
)
from tests.integration.test_billing_fact_worker_postgres import _Harness
from tests.integration.test_credit_courtesy_http_postgres import (
    _running_app,
    _statements,
)

pytestmark = pytest.mark.integration

__all__ = ["clean", "template"]

MC = 1_000
SUSPENDED = {"code": "credit_suspended"}
SUBSCRIPTIONS = "/v1/admin/builders/subscriptions"
PDF = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "credit_storage_conformance"
    / "positive"
    / "textual.pdf"
).read_bytes()
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
DEADLINES = (
    "exhausted_at",
    "suspension_due_at",
    "deletion_due_at",
    "export_deadline_at",
)
# What an upload writes, per builder or tenant (none may move for a refusal).
_WRITTEN = (
    ("knowledge_documents", "tenant_id = :t"),
    ("media_assets", "tenant_id = :t"),
    ("credit_effects", "builder_id = :b"),
    ("credit_ledger_entries", "builder_id = :b"),
    ("credit_allocations", "builder_id = :b"),
    ("credit_inventory_versions", "builder_id = :b"),
    ("credit_debts", "builder_id = :b"),
    ("credit_lifecycle_episodes", "builder_id = :b"),
    ("credit_observational_effects", "tenant_id = :t"),
)
# The master-admin batch (``admin._courtesy_rule_by_builder``), as asyncpg
# runs it and ``_statements`` records it (lower case, spaces collapsed): the
# lots of a builder list, and the paid plans of a builder list.
_RULE_LOTS_BATCH = re.compile(
    r"as granted_at from credit_lots left outer join credit_ledger_entries .*"
    r"where credit_lots\.builder_id in \("
)
_PAID_PLAN_BATCH = re.compile(
    r"as unbound_paid_cycle from credit_accounts "
    r"where credit_accounts\.builder_id in \("
)


@pytest.fixture(autouse=True)
def _fresh_latches() -> Iterator[None]:
    forget_enforcement_latches()
    yield
    forget_enforcement_latches()


@dataclass(frozen=True)
class _Clinic:
    builder: UUID
    tenant: UUID
    key: str
    user: UUID

    @property
    def api_key(self) -> dict[str, str]:
        return {"X-API-Key": self.key}

    @property
    def panel(self) -> dict[str, str]:
        return _bearer(self.tenant, "clinic_admin", subject=self.user)

    @property
    def owner(self) -> dict[str, str]:
        return _bearer(self.tenant, "builder", builder_id=str(self.builder))


def _bearer(
    tenant: UUID, role: str, *, subject: UUID | None = None, **claims: Any
) -> dict[str, str]:
    from src.config.settings import settings

    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": str(subject or uuid4()),
            "tenant_id": str(tenant),
            "role": role,
            "iat": now,
            "exp": now + timedelta(hours=1),
            **claims,
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def _super_admin() -> dict[str, str]:
    return _bearer(uuid4(), "super_admin")


async def _clinic(harness: _Harness, builder: UUID | None = None) -> _Clinic:
    """A builder (made unless given), its clinic with a real API key and the
    clinic's admin user (the panel's caller)."""

    builder_id = builder or await make_builder(harness)
    tenant, key, user = uuid4(), f"key-{uuid4().hex}", uuid4()
    await execute(
        harness,
        "INSERT INTO tenants (id, name, slug, api_key_hash, is_active, builder_id, "
        "config) VALUES (:id, 'Rule clinic', :slug, :hash, true, :builder, '{}')",
        id=tenant,
        slug=f"rule-{tenant.hex}",
        hash=pwd_context.hash(key),
        builder=builder_id,
    )
    await execute(
        harness,
        "INSERT INTO users (id, tenant_id, email, password_hash, role, is_active) "
        "VALUES (:id, :tenant, :email, 'x', CAST('clinic_admin' AS userrole), true)",
        id=user,
        tenant=tenant,
        email=f"{user.hex}@rule.invalid",
    )
    return _Clinic(builder_id, tenant, key, user)


async def _written(harness: _Harness, clinic: _Clinic) -> dict[str, int]:
    found: dict[str, int] = {}
    for table, where in _WRITTEN:
        found[table] = int(
            await scalar(
                harness,
                f"SELECT count(*) FROM {table} WHERE {where}",
                t=clinic.tenant,
                b=clinic.builder,
            )
        )
    return found


async def _media_row(harness: _Harness, clinic: _Clinic) -> UUID:
    """A stored image of the clinic (for the downloads)."""

    import hashlib

    media_id = uuid4()
    await execute(
        harness,
        "INSERT INTO media_assets (id, tenant_id, display_name, original_filename, "
        "category, mime_type, size_bytes, sha256, payload) VALUES (:id, :t, "
        "'Antiga', 'antiga.png', 'image', 'image/png', :size, :sha, :payload)",
        id=media_id,
        t=clinic.tenant,
        size=len(PNG),
        sha=hashlib.sha256(PNG).digest(),
        payload=PNG,
    )
    return media_id


async def _remove_everything(harness: _Harness, builder: UUID) -> None:
    """ "Tirar tudo" on every courtesy lot still holding something."""

    async with harness.engine.connect() as connection:
        lots = (
            await connection.execute(
                text(
                    "SELECT id FROM credit_lots WHERE builder_id = :b "
                    "AND lot_kind = 'courtesy' ORDER BY id"
                ),
                {"b": builder},
            )
        ).scalars()
        lot_ids = [UUID(str(lot)) for lot in lots]
    for lot_id in lot_ids:
        await remove_courtesy(harness, lot_id)


def _uploads(client: Any, clinic: _Clinic, headers: dict[str, str]) -> list[Any]:
    return [
        client.post(
            "/v1/knowledge/documents",
            json={"content": "Aceitamos Unimed.", "metadata": {"title": "Regra"}},
            headers=headers,
        ),
        client.post(
            "/v1/knowledge/documents/upload",
            files={"files": ("horarios.pdf", PDF, "application/pdf")},
            headers=headers,
        ),
        client.post(
            "/v1/media-assets",
            files={"file": ("foto.png", PNG, "image/png")},
            data={"display_name": "Foto", "original_filename": "foto.png"},
            headers=headers,
        ),
    ]


async def _all(responses: Any) -> list[Any]:
    return [await response for response in responses]


def _search(client: Any, headers: dict[str, str]) -> Any:
    return client.post(
        "/v1/knowledge/search", json={"query": "horários"}, headers=headers
    )


async def _state(client: Any, clinic: _Clinic) -> dict[str, Any]:
    response = await client.get("/v1/credits/state", headers=clinic.owner)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _rule_row(client: Any, builder: UUID) -> Any:
    response = await client.get(SUBSCRIPTIONS, headers=_super_admin())
    assert response.status_code == 200, response.text
    return {UUID(row["id"]): row for row in response.json()}[builder]["courtesy_rule"]


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Charged, then blocked, then unblocked.
# ---------------------------------------------------------------------------


async def test_charged_then_blocked_then_unblocked_over_http(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    clinic = await _clinic(clean)
    media = await _media_row(clean, clinic)
    first = await grant_courtesy(clean, clinic.builder, 3)
    async with _running_app(clean.url, monkeypatch) as client:
        state = await _state(client, clinic)
        assert (state["mode"], state["state"], state["pending_reasons"]) == (
            "enforced",
            "active",
            [],
        )
        assert _instant(state["enforcement_at"]) == first.occurred_at
        assert all(state[name] is None for name in DEADLINES)
        assert await _rule_row(client, clinic.builder) == "charged"
        for headers in (clinic.api_key, clinic.panel):
            for response in await _all(_uploads(client, clinic, headers)):
                assert response.status_code == 201, (response.url, response.text)
        assert (await _search(client, clinic.api_key)).status_code == 200
        download = await client.get(
            f"/v1/media-assets/{media}/content", headers=clinic.api_key
        )
        assert download.status_code == 200 and download.content == PNG

        # "Tirar tudo" with no paid plan: blocked now.
        await _remove_everything(clean, clinic.builder)
        state = await _state(client, clinic)
        assert (state["mode"], state["state"], state["pending_reasons"]) == (
            "enforced",
            "suspended",
            [],
        )
        assert all(state[name] is None for name in DEADLINES)
        assert await _rule_row(client, clinic.builder) == "blocked"
        before = await _written(clean, clinic)
        for headers in (clinic.api_key, clinic.panel):
            for response in await _all(_uploads(client, clinic, headers)):
                assert (response.status_code, response.json()) == (
                    402,
                    {"detail": SUSPENDED},
                ), (response.url, response.text)
        for response in (
            await _search(client, clinic.api_key),
            await client.get(
                f"/v1/media-assets/{media}/content", headers=clinic.api_key
            ),
        ):
            assert (response.status_code, response.json()) == (
                402,
                {"detail": SUSPENDED},
            )
        assert await _written(clean, clinic) == before
        # The panel's reads, its download and the export stay open.
        assert (await _search(client, clinic.panel)).status_code == 200
        for path in (
            f"/v1/media-assets/{media}/content",
            "/v1/media-assets",
            "/v1/knowledge/documents",
        ):
            response = await client.get(path, headers=clinic.panel)
            assert response.status_code == 200, (path, response.text)
        export = await client.get("/v1/credits/export", headers=clinic.api_key)
        assert export.status_code == 200, export.text
        assert await _written(clean, clinic) == before

        # A new courtesy unblocks at once (nothing was stored).
        await grant_courtesy(clean, clinic.builder, 2)
        assert (await _state(client, clinic))["state"] == "active"
        assert await _rule_row(client, clinic.builder) == "charged"
        response = (await _all(_uploads(client, clinic, clinic.api_key)))[0]
        assert response.status_code == 201, response.text
    # Under the rule nothing ever opened an episode or created a debt.
    written = await _written(clean, clinic)
    assert (written["credit_lifecycle_episodes"], written["credit_debts"]) == (0, 0)


# ---------------------------------------------------------------------------
# ASAAS gates out of the rule (DESIGN item 7, wish Decision 4).
# ---------------------------------------------------------------------------


async def test_a_charged_builder_in_quarantine_is_served_from_courtesy_only(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    clinic = await _clinic(clean)
    await make_topup(clean, clinic.builder)  # a paid lot the rule never touches
    subscription = await make_subscription(clean, clinic.builder)
    today = civil_today()
    await make_cycle(
        clean,
        clinic.builder,
        subscription,
        start=today - timedelta(days=40),
        end=today - timedelta(days=10),
        state="quarantined",
    )
    await grant_courtesy(clean, clinic.builder, 50)
    async with _running_app(clean.url, monkeypatch) as client:
        state = await _state(client, clinic)
        assert (state["mode"], state["state"], state["pending_reasons"]) == (
            "enforced",
            "active",
            ["quarantine"],
        )
        summary = (await client.get("/v1/credits/summary", headers=clinic.owner)).json()
        assert summary["state"]["pending_reasons"] == ["quarantine"]
        balance = summary["balance"]
        assert balance["topup_available_mc"] == 500_000
        assert balance["net_available_mc"] == balance["courtesy_available_mc"]
        response = (await _all(_uploads(client, clinic, clinic.api_key)))[0]
        assert response.status_code == 201, response.text
        after = (await client.get("/v1/credits/balance", headers=clinic.owner)).json()
    assert after["courtesy_available_mc"] < balance["courtesy_available_mc"]
    assert after["topup_available_mc"] == 500_000
    assert await paid_lot_moves(clean, clinic.builder) == []
    assert (await _written(clean, clinic))["credit_debts"] == 0


async def test_an_unusable_coverage_is_never_reconciliation_under_the_rule(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A current cycle of an active subscription with no binding: enforced
    by a mark this is ``pending``/``reconciliation`` and ``credit_pending``;
    under the rule the builder is served from its courtesy."""

    clinic = await _clinic(clean)
    subscription = await make_subscription(clean, clinic.builder)
    today = civil_today()
    await make_cycle(
        clean,
        clinic.builder,
        subscription,
        start=today - timedelta(days=3),
        end=today + timedelta(days=27),
        state="pending",
    )
    await grant_courtesy(clean, clinic.builder, 20)
    async with _running_app(clean.url, monkeypatch) as client:
        state = await _state(client, clinic)
        assert (state["state"], state["pending_reasons"]) == ("active", [])
        response = (await _all(_uploads(client, clinic, clinic.api_key)))[0]
        assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# A mark era's episode under the rule.
# ---------------------------------------------------------------------------

_EPISODE = (
    "INSERT INTO credit_lifecycle_episodes (id, builder_id, account_id, "
    "opened_effect_id, exhausted_at, suspension_due_at, deletion_due_at) "
    "SELECT :id, :b, id, :effect, :at, :due, :deletion FROM credit_accounts "
    "WHERE builder_id = :b"
)
_TRANSITION = (
    "INSERT INTO credit_lifecycle_transitions (id, builder_id, episode_id, "
    "transition, effect_id, occurred_at, source, detail) VALUES (:id, :b, "
    ":episode, :name, :effect, :at, 'worker', '{}'::jsonb)"
)


async def test_a_mark_era_episode_never_shows_nor_gates_under_the_rule(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    clinic = await _clinic(clean)
    media = await _media_row(clean, clinic)
    marked = datetime.now(timezone.utc) - timedelta(days=9)
    await record_mark(clean, clinic.builder, occurred_at=marked)
    episode, exhausted = uuid4(), marked + timedelta(days=1)
    await execute(
        clean,
        _EPISODE,
        id=episode,
        b=clinic.builder,
        effect=f"lifecycle:{clinic.builder}:episode:0:opened",
        at=exhausted,
        due=exhausted + timedelta(hours=72),
        deletion=exhausted + timedelta(days=30),
    )
    for name in ("suspended", "deletion_due"):
        await execute(
            clean,
            _TRANSITION,
            id=uuid4(),
            b=clinic.builder,
            episode=episode,
            name=name,
            effect=f"lifecycle:{episode}:{name}",
            at=exhausted + timedelta(days=4),
        )
    await record_mark(clean, clinic.builder, "unmark")
    await grant_courtesy(clean, clinic.builder, 30)
    async with _running_app(clean.url, monkeypatch) as client:
        state = await _state(client, clinic)
        assert (state["state"], state["mode"]) == ("active", "enforced")
        assert all(state[name] is None for name in DEADLINES)
        # The episode gates nothing for the charged builder.
        assert (await _search(client, clinic.api_key)).status_code == 200
        download = await client.get(
            f"/v1/media-assets/{media}/content", headers=clinic.api_key
        )
        assert download.status_code == 200
        for response in await _all(_uploads(client, clinic, clinic.api_key)):
            assert response.status_code == 201, (response.url, response.text)
        await _remove_everything(clean, clinic.builder)
        state = await _state(client, clinic)
        assert state["state"] == "suspended"
        assert all(state[name] is None for name in DEADLINES)
    transitions = await scalar(
        clean,
        "SELECT count(*) FROM credit_lifecycle_transitions WHERE builder_id = :b",
        b=clinic.builder,
    )
    assert int(transitions) == 2  # nothing recorded under the rule


# ---------------------------------------------------------------------------
# Thresholds on the courtesy basis (DESIGN items 4 and 13).
# ---------------------------------------------------------------------------


async def test_the_thresholds_follow_real_consumption_on_the_courtesy_basis(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    clinic = await _clinic(clean)
    granted = await grant_courtesy(clean, clinic.builder, 2)
    grant_effect = await scalar(
        clean,
        "SELECT grant_effect_id FROM credit_lots WHERE id = :id",
        id=granted.lot_id,
    )
    async with _running_app(clean.url, monkeypatch) as client:
        # 5 000 characters: one credit of RAG, half the courtesy.
        response = await client.post(
            "/v1/knowledge/documents",
            json={"content": "x" * 5_000, "metadata": {"title": "Metade"}},
            headers=clinic.api_key,
        )
        assert response.status_code == 201, response.text
        body = (await client.get("/v1/credits/thresholds", headers=clinic.owner)).json()
        summary = (await client.get("/v1/credits/summary", headers=clinic.owner)).json()
    assert (body["capacity_mc"], body["available_mc"], body["used_mc"]) == (
        2 * MC,
        MC,
        MC,
    )
    assert (body["used_percent"], body["reached"]) == (50, [50])
    assert [alert["threshold"] for alert in body["alerts"]] == [50]
    arms = await scalar(
        clean,
        "SELECT string_agg(arm_key, ',') FROM credit_threshold_alerts "
        "WHERE builder_id = :b",
        b=clinic.builder,
    )
    assert arms == courtesy_arm_key(str(grant_effect))
    assert summary["thresholds"] == {**body, "as_of": summary["thresholds"]["as_of"]}
    assert summary["balance"]["net_available_mc"] == MC
    assert summary["consumption"]["charged"] == [{"kind": "rag", "amount_mc": MC}]


# ---------------------------------------------------------------------------
# master-admin ``courtesy_rule`` (DESIGN item 13).
# ---------------------------------------------------------------------------


async def _matrix(harness: _Harness) -> dict[str, UUID]:
    """One builder per case of the rule and of the paid plan in force."""

    today = civil_today()
    cases: dict[str, UUID] = {}

    async def builder(
        name: str, credits: int | None = 3, *, remove: bool = False
    ) -> UUID:
        builder_id = await make_builder(harness)
        if credits is not None:
            granted = await grant_courtesy(harness, builder_id, credits)
            if remove:
                await remove_courtesy(harness, granted.lot_id)
        cases[name] = builder_id
        return builder_id

    await builder("never", None)
    await builder("charged")
    await builder("blocked", remove=True)
    # A courtesy past its expiry with its balance, nothing else: blocked
    # like an empty one (DESIGN Decision 7).  The ledger dates a grant at
    # its own clock, so the lot is aged in place (triggers off).
    expired = await builder("expired")
    await plant(
        harness,
        "UPDATE credit_lots SET expires_at = now() - interval '1 day' "
        "WHERE builder_id = :b AND lot_kind = 'courtesy'",
        b=expired,
    )
    topped = await builder("topup_protected", remove=True)
    await make_topup(harness, topped)
    spent_topup = await builder("spent_topup_protected", remove=True)
    await make_topup(harness, spent_topup, reversed_mc=500_000)
    graced = await builder("grace_protected", remove=True)
    subscription = await make_subscription(harness, graced)
    start, end = today - timedelta(days=32), today - timedelta(days=2)
    cycle = await make_cycle(harness, graced, subscription, start=start, end=end)
    await make_monthly_grant(
        harness,
        builder_id=graced,
        subscription_id=subscription,
        cycle_id=cycle,
        start=start,
        end=end,
    )
    # The same grace window, but the cycle's only binding was reversed (a
    # full chargeback): an active subscription grants no grace without an
    # ``active`` binding, so the spent courtesy blocks.
    chargedback = await builder("reversed_binding_blocked", remove=True)
    subscription = await make_subscription(harness, chargedback)
    cycle = await make_cycle(harness, chargedback, subscription, start=start, end=end)
    grant = await make_monthly_grant(
        harness,
        builder_id=chargedback,
        subscription_id=subscription,
        cycle_id=cycle,
        start=start,
        end=end,
    )
    assert grant.binding_id is not None
    await reverse_binding(harness, grant.binding_id, 2)
    lapsed = await builder("inactive_no_grace", remove=True)
    subscription = await make_subscription(harness, lapsed)
    cycle = await make_cycle(harness, lapsed, subscription, start=start, end=end)
    await make_monthly_grant(
        harness,
        builder_id=lapsed,
        subscription_id=subscription,
        cycle_id=cycle,
        start=start,
        end=end,
    )
    await set_subscription_status(harness, subscription, "cancelled")
    unbound = await builder("unbound_paid_cycle_protected", remove=True)
    subscription = await make_subscription(harness, unbound)
    await make_cycle(
        harness,
        unbound,
        subscription,
        start=today - timedelta(days=2),
        end=today + timedelta(days=28),
    )
    reversed_cycle = await builder("reversed_cycle_blocked", remove=True)
    subscription = await make_subscription(harness, reversed_cycle)
    await make_cycle(
        harness,
        reversed_cycle,
        subscription,
        start=today - timedelta(days=2),
        end=today + timedelta(days=28),
        state="reversed",
    )
    marked = await builder("marked")
    await record_mark(harness, marked)
    unmarked = await builder("unmarked")
    await record_mark(harness, unmarked)
    await record_mark(harness, unmarked, "unmark")
    return cases


async def test_the_master_admin_batch_is_the_request_reader_builder_by_builder(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = await _matrix(clean)
    async with _running_app(clean.url, monkeypatch) as client:
        with _statements() as seen:
            response = await client.get(SUBSCRIPTIONS, headers=_super_admin())
        assert response.status_code == 200, response.text
        rows = {UUID(row["id"]): row for row in response.json()}
    found = {name: rows[builder]["courtesy_rule"] for name, builder in cases.items()}
    assert found == {
        "never": None,
        "charged": "charged",
        "blocked": "blocked",
        "expired": "blocked",
        "topup_protected": None,
        "spent_topup_protected": None,
        "grace_protected": None,
        "reversed_binding_blocked": "blocked",
        "inactive_no_grace": "blocked",
        "unbound_paid_cycle_protected": None,
        "reversed_cycle_blocked": "blocked",
        "marked": None,
        "unmarked": "charged",
    }
    for name, builder in cases.items():
        effective = await effective_of(
            clean, builder, marks_readable=True, rule_readable=True
        )
        expected = effective.state if effective.origin == "courtesy" else None
        assert found[name] == expected, name
    assert rows[cases["marked"]]["charging"] is not None
    assert rows[cases["charged"]]["charging"] is None
    # One batch read of the lots and one of the paid plans, for the table.
    assert len([s for s in seen if _RULE_LOTS_BATCH.search(s)]) == 1
    assert len([s for s in seen if _PAID_PLAN_BATCH.search(s)]) == 1


async def _enforced_with_courtesy_keeps_its_figures(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    """A builder enforced by ``origin`` (a MARK or the GLOBAL) that also
    holds a courtesy lot keeps that enforcement's ``/v1/credits/*``
    figures: its exhaustion episode (``grace`` with every deadline set), the
    debt-net balance and the episode arm's alerts; never the courtesy
    rule's (the rule would forgive the debt and null the deadlines), and
    the master-admin row is not under the rule.  Precedence global > mark >
    courtesy (DESIGN Decision 13) on the read side."""

    # The setup below runs in this process, outside the app's lifespan: its
    # switch must read the head revision, as the served app's does; without
    # it, the mark-era debit below runs with the marks gate closed and the
    # mark case reads ``active``.
    force_credit_switch(monkeypatch)
    clinic = await _clinic(clean)
    await grant_courtesy(clean, clinic.builder, 2)
    if origin == "mark":
        await record_mark(clean, clinic.builder)
    else:
        armed = datetime.now(timezone.utc) - timedelta(minutes=1)
        await plant(
            clean,
            "INSERT INTO credit_enforcement_state (id, credit_enforcement_at, "
            "set_by, set_at) VALUES (1, :at, 'test', :at) ON CONFLICT (id) DO "
            "UPDATE SET credit_enforcement_at = :at",
            at=armed,
        )
    effective = await effective_of(
        clean, clinic.builder, marks_readable=True, rule_readable=True
    )
    assert effective.origin == origin
    # A debit past the balance: 2 credits from the courtesy lot, 1 of debt --
    # the enforcement's exhaustion episode opens (the rule would forgive it).
    async with clean.sessions() as session, session.begin():
        result = await CreditLedger(session).debit(
            clinic.builder,
            f"trace:{uuid4()}:decision:1",
            3 * MC,
            at=datetime.now(timezone.utc),
            effect_kind="trace",
        )
    assert (result.debited_mc, result.debt_mc) == (2 * MC, 1 * MC)
    async with _running_app(clean.url, monkeypatch) as client:
        state = await _state(client, clinic)
        balance = (await client.get("/v1/credits/balance", headers=clinic.owner)).json()
        thresholds = (
            await client.get("/v1/credits/thresholds", headers=clinic.owner)
        ).json()
        row = await _rule_row(client, clinic.builder)
    assert (state["mode"], state["state"]) == ("enforced", "grace"), state
    assert all(state[name] is not None for name in DEADLINES), state
    assert balance["open_debt_mc"] == 1 * MC
    assert balance["net_available_mc"] == 0, balance
    assert [alert["threshold"] for alert in thresholds["alerts"]] == [50, 90, 100]
    assert row is None  # never under the rule


async def test_a_marked_builder_with_courtesy_keeps_the_marks_figures(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enforced_with_courtesy_keeps_its_figures(clean, monkeypatch, "mark")


async def test_a_global_builder_with_courtesy_keeps_the_globals_figures(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enforced_with_courtesy_keeps_its_figures(clean, monkeypatch, "global")


async def test_with_the_global_set_no_builder_is_under_the_rule(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    charged = await make_builder(clean)
    await grant_courtesy(clean, charged, 3)
    blocked = await make_builder(clean)
    granted = await grant_courtesy(clean, blocked, 3)
    await remove_courtesy(clean, granted.lot_id)
    armed = datetime.now(timezone.utc) - timedelta(days=1)
    await plant(
        clean,
        "INSERT INTO credit_enforcement_state (id, credit_enforcement_at, set_by, "
        "set_at) VALUES (1, :at, 'test', :at) ON CONFLICT (id) DO UPDATE SET "
        "credit_enforcement_at = :at",
        at=armed,
    )
    async with _running_app(clean.url, monkeypatch) as client:
        response = await client.get(SUBSCRIPTIONS, headers=_super_admin())
    rows = {UUID(row["id"]): row for row in response.json()}
    assert (rows[charged]["courtesy_rule"], rows[blocked]["courtesy_rule"]) == (
        None,
        None,
    )


# ---------------------------------------------------------------------------
# Dominance: no courtesy, and a marked builder.
# ---------------------------------------------------------------------------

_READS = ("state", "balance", "thresholds", "summary", "debt", "consumption", "lots")


def _without_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_volatile(v) for k, v in value.items() if k != "as_of"}
    if isinstance(value, list):
        return [_without_volatile(item) for item in value]
    return value


@pytest.fixture
def patient_observational_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The house pattern of the suites that COUNT observational rows
    (``_patient_observational_writer`` of
    tests/integration/test_credit_effective_enforcement_postgres.py): the
    best-effort writers get a wide budget.  Their 250 ms budget and server
    backstops are main's and pinned by
    tests/integration/test_credit_metering_postgres.py
    (``test_a_write_takes_one_connection_and_leaves_server_defaults``,
    ``test_lock_contention_is_dropped_inside_the_budget``); on a loaded
    machine they drop a row by design (``timeout``), which is not what the
    proof using this fixture is about.  The routes resolve the writers and
    the timeout through the module attributes, so the app sees the patch."""

    def patient(module: Any, name: str) -> None:
        original = getattr(module, name)

        async def wider(*args: Any, **kwargs: Any) -> Any:
            kwargs["budget_seconds"] = 10.0
            return await original(*args, **kwargs)

        monkeypatch.setattr(module, name, wider)

    patient(credit_metering, "record_observational_trace")
    patient(credit_inventory, "record_observational_upload")
    patient(credit_inventory, "record_observational_egress")
    monkeypatch.setattr(credit_metering, "OBSERVATIONAL_SERVER_TIMEOUT", "10s")
    monkeypatch.setattr(credit_inventory, "OBSERVATIONAL_SERVER_TIMEOUT", "10s")


async def test_dominance_without_courtesy_and_for_a_marked_builder(
    clean: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    patient_observational_writer: None,
) -> None:
    # ``patient_observational_writer``: the plain builder's upload must leave
    # its observational row (asserted below); under a loaded PostgreSQL the
    # writer's real 250 ms budget dropped it once (``timeout``).
    plain = await _clinic(clean)
    await make_topup(clean, plain.builder)
    marked = await _clinic(clean)
    await make_topup(clean, marked.builder)
    await grant_courtesy(clean, marked.builder, 5)
    await record_mark(clean, marked.builder)

    async def answers(client: Any) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for clinic in (plain, marked):
            for route in _READS:
                response = await client.get(
                    f"/v1/credits/{route}", headers=clinic.owner
                )
                assert response.status_code == 200, (route, response.text)
                found[f"{clinic.builder}:{route}"] = _without_volatile(response.json())
            found[f"{clinic.builder}:search"] = (
                await _search(client, clinic.api_key)
            ).status_code
        rows = (await client.get(SUBSCRIPTIONS, headers=_super_admin())).json()
        for row in rows:
            if UUID(row["id"]) in (plain.builder, marked.builder):
                found[f"{row['id']}:row"] = {
                    k: v for k, v in row.items() if k != "updated_at"
                }
        return found

    async with _running_app(clean.url, monkeypatch) as client:
        opened = await answers(client)
        with pytest.MonkeyPatch.context() as rule_closed:
            # Both sides closed: the request path's gate and the master
            # admin table's own predicate.
            rule_closed.setattr(
                CreditRepository,
                "courtesy_rule_readable",
                staticmethod(lambda *_: False),
            )
            admin_closed: list[Any] = []

            def admin_rule_closed(reading: Any) -> bool:
                admin_closed.append(reading)
                return False

            rule_closed.setattr(
                admin_routes, "_courtesy_rule_readable", admin_rule_closed
            )
            closed = await answers(client)
            assert len(admin_closed) == 1  # the table's predicate was consulted
    assert opened == closed
    assert opened[f"{marked.builder}:state"]["mode"] == "enforced"  # the mark's
    assert opened[f"{plain.builder}:state"]["mode"] == "observational"
    for clinic in (plain, marked):
        assert opened[f"{clinic.builder}:row"]["courtesy_rule"] is None

    # The writes: the plain builder's upload stays observational, the marked
    # one's is charged as a mark -- its paid lot pays (the rule never would).
    async with _running_app(clean.url, monkeypatch) as client:
        for clinic in (plain, marked):
            response = await client.post(
                "/v1/knowledge/documents",
                json={"content": "x" * 60_000, "metadata": {"title": "Dominio"}},
                headers=clinic.api_key,
            )
            assert response.status_code == 201, response.text
    plain_written = await _written(clean, plain)
    assert plain_written["credit_ledger_entries"] == 1  # the top-up's grant only
    assert plain_written["credit_observational_effects"] >= 1
    assert await paid_lot_moves(clean, marked.builder) != []
