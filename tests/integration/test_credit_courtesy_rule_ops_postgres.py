"""``python -m src.ops.credit_courtesy_rule status`` on real PostgreSQL
(debito-cortesia-automatico, Group 6: the read-only pre-deploy list).

Every proof runs the module's own entry point (``amain``: the switch
refreshed in this process at the database's ``alembic_version``, one
``REPEATABLE READ READ ONLY`` transaction, the one JSON document) against a
fresh database copied from one migrated template (head):

* the states at the deploy instant, each next to what the request path's
  reader (``CreditRepository.effective_enforcement``) returns: ``charged``;
  ``blocked`` (courtesy removed); ``protected`` by a top-up with nothing
  left but a future expiry, by an active subscription inside its binding
  grace, and by the binding lag (a paid current cycle without a binding,
  with how long it has lasted); ``mark`` and ``global`` winning over the
  rule; a builder that never had courtesy is not listed;
* the spendable courtesy and its nearest expiry, the open debt (an old debt
  a later grant settled in part), the active subscription and
  ``coverage.usable`` (equal to ``binding_coverage`` read directly);
* the storage reservation of the first window after the deploy: the next
  window for a first grant inside the current one, the current window for
  a first grant before it, only what the courtesy pays being payable;
* the clinics per channel (Uazapi, Evolution, Z-API, Meta, none) with no
  credential, instance name, token digest or phone-number id printed; the
  channels with incomplete send credentials per tenant and their totals,
  each case's verdict pinned against ``CreditReplySender.prepare`` on the
  same JSONB credentials (owner decision 20);
* the ``/v1/events`` traffic without ``Idempotency-Key`` (wish Decision 9)
  with non-vacuous controls: a claimed request (an ``events`` claim, a
  provider claim) is not counted, nor an unauthenticated principal, a
  simulator or unknown origin, a non-trace effect, one older than 7 days,
  nor another builder's tenant; a claim of ANOTHER tenant does not hide one;
* read-only: the command's transaction starts with ``SET TRANSACTION
  ISOLATION LEVEL REPEATABLE READ READ ONLY``, runs SELECTs only and is
  rolled back, and every table and sequence of the database is identical
  (row counts and an md5 of every row's text) before and after it.

Needs ``POSTGRES_SMOKE_DATABASE_URL``; without it the module skips, and only
``scripts/check-credit-integration.sh`` turns that skip into a failure.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text

import src.models  # noqa: F401  (registers every mapper)
from src.config.settings import settings
from src.interaction_context import InteractionOrigin
from src.models.knowledge import KnowledgeDocument
from src.models.media_asset import MediaAsset
from src.models.session import Session
from src.ops import credit_courtesy_rule as ops
from src.repositories.credit import CreditRepository
from src.services.credit_allocations import accounting_window
from src.services.credit_ledger import CreditLedger, civil_date
from src.services.credit_metering import (
    ObservationalIngress,
    record_observational_trace,
)
from src.services.credit_principal import credit_identity
from src.services.credit_rollout import CreditRolloutService
from src.services.credit_switch import stop_credit_switch
from src.services.media_assets import media_payload_sha256
from tests.integration.credit_rollout_world import (
    PLANT,
    civil_today,
    clean,
    effective_of,
    execute,
    force_credit_switch,
    grant_courtesy,
    make_builder,
    make_cycle,
    make_monthly_grant,
    make_subscription,
    make_tenant,
    make_topup,
    record_mark,
    relink_tenant,
    remove_courtesy,
    scalar,
    template,
)
from tests.integration.test_billing_fact_worker_postgres import _Harness
from tests.ops.test_credit_courtesy_rule import (
    READ_KEYS,
    compare_send_verdicts,
    send_cases,
    sender_ready,
)

pytestmark = pytest.mark.integration

__all__ = ["clean", "template"]

MC = 1_000
SHA = "b" * 64
SECRETS = (
    "uazapi-token-NEVER-PRINT-55aa",
    "https://user:pass-NEVER-PRINT@uazapi.example",
    "evolution-api-key-NEVER-PRINT-77ce",
    "zapi-token-NEVER-PRINT-71c9",
    "meta-permanent-token-NEVER-PRINT-0b1e",
    "sk-llm-key-NEVER-PRINT-9f3d",
    "provider-value-that-is-a-secret-NEVER-PRINT",
)
ROUTING_VALUES = (
    "instance-uazapi-NEVER-PRINT",
    "instance-evolution-NEVER-PRINT",
    "e" * 64,
    "5511988887777",
)
# What PostgreSQL and asyncpg may run besides the command's SELECTs: the
# dialect's first-connect probes and the command's own read-only opening.
_READ_ONLY_SQL = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
_WRITE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|COPY|GRANT|"
    r"REVOKE|LOCK|VACUUM|COMMENT|REFRESH|CLUSTER|REINDEX|NEXTVAL|SETVAL)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Harness: the module's real entry point on a fresh database.
# ---------------------------------------------------------------------------


@pytest.fixture
async def world(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_Harness]:
    """A fresh database with the credit configuration on and this process's
    switch effective on a head reading (the helpers' reads see the rule);
    the command still refreshes the switch itself."""

    monkeypatch.setattr(settings, "credit_hot_path_enabled", True)
    force_credit_switch(monkeypatch)
    try:
        yield clean
    finally:
        await stop_credit_switch(None)


@dataclass(frozen=True)
class Outcome:
    code: int
    document: dict[str, Any]
    printed: str
    statements: list[str]
    rollbacks: int
    commits: int

    def builder(self, builder_id: UUID) -> dict[str, Any]:
        (entry,) = [
            row
            for row in self.document["builders"]
            if row["builder_id"] == str(builder_id)
        ]
        return entry


@contextmanager
def _captured(harness: _Harness) -> Iterator[tuple[list[str], list[str]]]:
    statements: list[str] = []
    ends: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    def rolled_back(_conn: Any) -> None:
        ends.append("rollback")

    def committed(_conn: Any) -> None:
        ends.append("commit")

    sync = harness.engine.sync_engine
    event.listen(sync, "before_cursor_execute", record)
    event.listen(sync, "rollback", rolled_back)
    event.listen(sync, "commit", committed)
    try:
        yield statements, ends
    finally:
        event.remove(sync, "before_cursor_execute", record)
        event.remove(sync, "rollback", rolled_back)
        event.remove(sync, "commit", committed)


async def run(harness: _Harness) -> Outcome:
    out, err = io.StringIO(), io.StringIO()
    with _captured(harness) as (statements, ends):
        code = await ops.amain(
            ["status"], session_factory=harness.sessions, stdout=out, stderr=err
        )
    (line,) = out.getvalue().splitlines()
    document = json.loads(line)
    assert document["exit_code"] == code
    return Outcome(
        code,
        document,
        out.getvalue() + err.getvalue(),
        list(statements),
        ends.count("rollback"),
        ends.count("commit"),
    )


async def _spent_builder(harness: _Harness) -> tuple[UUID, Any]:
    """A builder whose only courtesy was removed ("Tirar tudo")."""

    builder_id = await make_builder(harness)
    granted = await grant_courtesy(harness, builder_id, 5)
    await remove_courtesy(harness, granted.lot_id)
    return builder_id, granted


async def _backdated_grant(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    builder_id: UUID,
    credits: int,
    at: datetime,
) -> Any:
    """A courtesy granted at a past instant: the ledger's database clock is
    simulated for that one call (its tolerance check)."""

    async def reference_now(_self: CreditLedger) -> datetime:
        return at

    with monkeypatch.context() as patch:
        patch.setattr(CreditLedger, "reference_now", reference_now)
        return await grant_courtesy(harness, builder_id, credits, at=at)


async def _config(
    harness: _Harness, tenant_id: UUID, credentials: dict[str, Any]
) -> None:
    await execute(
        harness,
        "INSERT INTO clinic_configs (id, tenant_id, api_credentials) "
        "VALUES (:id, :tenant, CAST(:credentials AS JSONB))",
        id=uuid4(),
        tenant=tenant_id,
        credentials=json.dumps(credentials),
    )


async def _plain_read(harness: _Harness, builder_id: UUID) -> tuple[Any, Any]:
    """``paid_plan_in_force`` and ``binding_coverage`` read directly."""

    async with harness.sessions() as session:
        try:
            plan = await CreditRepository(session).paid_plan_in_force(builder_id)
            coverage = await CreditRolloutService(session).binding_coverage(builder_id)
        finally:
            await session.rollback()
    return plan, coverage


def _at(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _knowledge(tenant: UUID, characters: int) -> KnowledgeDocument:
    return KnowledgeDocument(
        id=uuid4(),
        tenant_id=tenant,
        title="Doc",
        content="x" * characters,
        content_type="text",
        document_metadata={},
        is_indexed=False,
        chunk_count=0,
        size_bytes=characters,
    )


def _media(tenant: UUID, size: int) -> MediaAsset:
    payload = b"%PDF-" + b"0" * (size - 5)
    return MediaAsset(
        id=uuid4(),
        tenant_id=tenant,
        display_name="Media",
        original_filename="media.pdf",
        category="pdf",
        mime_type="application/pdf",
        size_bytes=size,
        sha256=media_payload_sha256(payload),
        payload=payload,
        usage_trigger=None,
        auto_suggest=False,
    )


# ---------------------------------------------------------------------------
# The list: every state, the balance, the debt, the plan, the coverage.
# ---------------------------------------------------------------------------


async def test_the_list_shows_who_is_charged_blocked_or_protected(
    world: _Harness,
) -> None:
    harness = world
    today = civil_today()

    charged = await make_builder(harness)
    first = await grant_courtesy(harness, charged, 5)
    second = await grant_courtesy(harness, charged, 2)

    blocked, blocked_grant = await _spent_builder(harness)
    # A top-up that has ALREADY expired protects nothing and has no future
    # expiry to print (``_paid_lot_expiry``'s ``expires_at > now``).
    await make_topup(
        harness,
        blocked,
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )

    by_topup, _ = await _spent_builder(harness)
    topup_until = datetime.now(timezone.utc) + timedelta(days=40)
    await make_topup(harness, by_topup, reversed_mc=500_000, expires_at=topup_until)
    await make_topup(
        harness,
        by_topup,
        reversed_mc=500_000,
        expires_at=datetime.now(timezone.utc) + timedelta(days=10),
    )

    by_grace, _ = await _spent_builder(harness)
    grace_subscription = await make_subscription(harness, by_grace)
    start, end = today - timedelta(days=31), today - timedelta(days=1)
    grace_cycle = await make_cycle(
        harness, by_grace, grace_subscription, start=start, end=end
    )
    await make_monthly_grant(
        harness,
        builder_id=by_grace,
        subscription_id=grace_subscription,
        cycle_id=grace_cycle,
        start=start,
        end=end,
    )

    by_lag, _ = await _spent_builder(harness)
    lag_subscription = await make_subscription(harness, by_lag)
    lag_cycle = await make_cycle(
        harness,
        by_lag,
        lag_subscription,
        start=today - timedelta(days=2),
        end=today + timedelta(days=28),
        state="settled",
    )
    async with harness.engine.begin() as connection:
        await connection.execute(text(PLANT))
        await connection.execute(
            text(
                "UPDATE billing_subscription_cycles SET updated_at = now() - "
                "interval '2 hours' WHERE id = :id"
            ),
            {"id": lag_cycle},
        )

    marked = await make_builder(harness)
    await grant_courtesy(harness, marked, 1)
    marked_at = await record_mark(harness.engine, marked)

    debtor = await make_builder(harness)
    async with harness.sessions() as session, session.begin():
        debt = await CreditLedger(session).debit(
            debtor,
            f"trace:{uuid4()}:decision:1",
            5 * MC,
            at=datetime.now(timezone.utc),
            effect_kind="trace",
        )
    assert debt.debt_mc == 5 * MC
    debtor_grant = await grant_courtesy(harness, debtor, 1)  # settles 1 credit

    never = await make_builder(harness)  # no courtesy: never listed
    await make_topup(harness, never)

    outcome = await run(harness)
    assert outcome.code == 0, outcome.document
    document = outcome.document
    listed = [row["builder_id"] for row in document["builders"]]
    expected = sorted(
        str(b) for b in (charged, blocked, by_topup, by_grace, by_lag, marked, debtor)
    )
    assert listed == expected
    assert document["global_enforcement_at"] is None
    assert document["gates"] == {"courtesy_rule": True, "marks": True}

    # Every state is the request path's own verdict at the same instant.
    now = datetime.fromisoformat(document["database_now"])
    for builder_id in (charged, blocked, by_topup, by_grace, by_lag, marked, debtor):
        effective = await effective_of(harness, builder_id, at=now)
        printed = outcome.builder(builder_id)["enforcement"]
        assert printed["origin"] == (effective.origin or "none")
        assert printed["rule_state"] == effective.state
        assert _at(printed["effective_at"]) == effective.at

    entry = outcome.builder(charged)
    assert entry["state"] == "charged"
    assert _at(entry["enforcement"]["effective_at"]) == first.occurred_at
    assert entry["courtesy"]["lots"] == 2
    assert _at(entry["courtesy"]["first_grant_at"]) == first.occurred_at
    assert entry["courtesy"]["spendable_mc"] == 7 * MC
    assert _at(entry["courtesy"]["nearest_expiry_at"]) == min(
        first.expires_at, second.expires_at
    )
    assert _at(entry["courtesy"]["last_expiry_at"]) == max(
        first.expires_at, second.expires_at
    )
    assert entry["paid_plan"]["in_force"] is False
    assert entry["paid_plan"]["paid_lot_expires_at"] is None
    assert entry["open_debt"] == {"open_debt_mc": 0, "open_debts": 0}
    assert entry["subscription"]["active"] is False
    assert entry["binding_lag"]["lagging"] is False

    entry = outcome.builder(blocked)
    assert entry["state"] == "blocked"
    assert entry["courtesy"]["spendable_mc"] == 0
    assert entry["courtesy"]["nearest_expiry_at"] is None
    assert entry["courtesy"]["last_expiry_at"] is None
    assert _at(entry["courtesy"]["first_grant_at"]) == blocked_grant.occurred_at
    assert entry["paid_plan"]["in_force"] is False
    assert entry["paid_plan"]["paid_lot"] is False
    assert entry["paid_plan"]["paid_lot_expires_at"] is None

    entry = outcome.builder(by_topup)
    assert entry["state"] == "protected"
    assert entry["enforcement"]["origin"] == "none"
    assert entry["paid_plan"]["paid_lot"] is True
    assert entry["paid_plan"]["in_force"] is True
    # The latest future expiry of its two top-ups: protected until then.
    assert _at(entry["paid_plan"]["paid_lot_expires_at"]) == topup_until

    entry = outcome.builder(by_grace)
    assert entry["state"] == "protected"
    plan, coverage = await _plain_read(harness, by_grace)
    assert entry["paid_plan"]["in_grace"] is True and plan.in_grace
    assert entry["paid_plan"]["paid_lot"] is False
    assert entry["paid_plan"]["last_binding_cycle_end"] == end.isoformat()
    assert _at(entry["paid_plan"]["grace_ends_at"]) == plan.grace_ends_at
    assert entry["subscription"]["active"] is True
    assert entry["subscription"]["status"] == "active"
    assert [row["id"] for row in entry["subscription"]["subscriptions"]] == [
        str(grace_subscription)
    ]
    assert entry["binding_lag"]["lagging"] is False
    assert entry["coverage"] == {
        "usable": coverage.usable,
        "reconciliation_failure": coverage.reconciliation_failure,
    }

    entry = outcome.builder(by_lag)
    assert entry["state"] == "protected"
    plan, coverage = await _plain_read(harness, by_lag)
    assert plan.unbound_paid_cycle and entry["paid_plan"]["unbound_paid_cycle"]
    lag = entry["binding_lag"]
    assert lag["lagging"] is True
    assert [row["cycle_id"] for row in lag["cycles"]] == [str(lag_cycle)]
    assert lag["cycles"][0]["state"] == "settled"
    assert lag["since_column"] == "cycle_updated_at"
    assert 2 * 3600 - 60 <= lag["seconds"] <= 2 * 3600 + 600
    assert entry["subscription"] == {
        "active": True,
        "status": "active",
        "subscriptions": [
            {
                "id": str(lag_subscription),
                "provider": "asaas",
                "status": "active",
                "is_active": True,
            }
        ],
    }
    assert entry["coverage"] == {
        "usable": coverage.usable,
        "reconciliation_failure": coverage.reconciliation_failure,
    }

    entry = outcome.builder(marked)
    assert entry["state"] == "mark"
    assert _at(entry["enforcement"]["effective_at"]) == marked_at
    assert entry["courtesy"]["spendable_mc"] == MC  # the rule would charge it

    entry = outcome.builder(debtor)
    assert entry["state"] == "blocked"
    assert _at(entry["courtesy"]["first_grant_at"]) == debtor_grant.occurred_at
    assert entry["open_debt"] == {"open_debt_mc": 4 * MC, "open_debts": 1}

    summary = document["summary"]
    assert summary["builders"] == 7
    assert summary["states"] == {
        "charged": 1,
        "blocked": 2,
        "protected": 3,
        "global": 0,
        "mark": 1,
        "not_under_rule": 0,
        "rule_unread": 0,
    }
    assert summary["open_debt_mc"] == 4 * MC
    assert outcome.commits == 0 and outcome.rollbacks >= 1


async def test_no_binding_lag_without_an_unbound_paid_cycle_of_an_active_plan(
    world: _Harness,
) -> None:
    """Each condition of the lag, alone false, ends it: a binding written for
    the paid current cycle, a cycle not paid (``pending``, ``reversed``),
    a paid cycle of a subscription no longer active.  Each reading equals
    ``paid_plan_in_force``'s ``unbound_paid_cycle``."""

    harness = world
    today = civil_today()
    start, end = today - timedelta(days=2), today + timedelta(days=28)

    bound, _ = await _spent_builder(harness)
    bound_subscription = await make_subscription(harness, bound)
    bound_cycle = await make_cycle(
        harness, bound, bound_subscription, start=start, end=end, state="settled"
    )
    grant = await make_monthly_grant(
        harness,
        builder_id=bound,
        subscription_id=bound_subscription,
        cycle_id=bound_cycle,
        start=start,
        end=end,
    )
    assert grant.binding_id is not None

    unpaid: list[UUID] = []
    for state in ("pending", "reversed"):
        builder_id, _ = await _spent_builder(harness)
        subscription = await make_subscription(harness, builder_id)
        await make_cycle(
            harness, builder_id, subscription, start=start, end=end, state=state
        )
        unpaid.append(builder_id)

    inactive, _ = await _spent_builder(harness)
    inactive_subscription = await make_subscription(
        harness, inactive, status="cancelled"
    )
    await make_cycle(
        harness,
        inactive,
        inactive_subscription,
        start=start,
        end=end,
        state="settled",
    )

    outcome = await run(harness)
    assert outcome.code == 0, outcome.document
    for builder_id in (bound, *unpaid, inactive):
        entry = outcome.builder(builder_id)
        plan, _ = await _plain_read(harness, builder_id)
        lag = entry["binding_lag"]
        assert lag["lagging"] is False, (builder_id, lag)
        assert lag["lagging"] is plan.unbound_paid_cycle
        assert entry["paid_plan"]["unbound_paid_cycle"] is False
        assert lag["cycles"] == [] and lag["since"] is None
    assert outcome.builder(bound)["state"] == "protected"  # by its binding
    assert outcome.builder(inactive)["subscription"]["active"] is False
    for builder_id in (*unpaid, inactive):
        assert outcome.builder(builder_id)["state"] == "blocked"


async def test_the_open_debt_is_the_swept_one(
    world: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The open debt is read from the book swept at the database clock: a
    monthly allowance whose cycle started after the debt pays it at its
    start (the catch-up), as ``/v1/credits/debt`` reads it -- the stored
    rows alone still show the debt open."""

    harness = world
    today = civil_today()
    builder_id = await make_builder(harness)
    incurred = datetime.now(timezone.utc) - timedelta(days=3)

    async def reference_now(_self: CreditLedger) -> datetime:
        return incurred

    with monkeypatch.context() as patch:
        patch.setattr(CreditLedger, "reference_now", reference_now)
        async with harness.sessions() as session, session.begin():
            debt = await CreditLedger(session).debit(
                builder_id,
                f"trace:{uuid4()}:decision:1",
                5 * MC,
                at=incurred,
                effect_kind="trace",
            )
    assert debt.debt_mc == 5 * MC
    await grant_courtesy(harness, builder_id, 1)  # settles 1 credit now
    subscription = await make_subscription(harness, builder_id)
    start, end = today - timedelta(days=1), today + timedelta(days=29)
    cycle = await make_cycle(harness, builder_id, subscription, start=start, end=end)
    await make_monthly_grant(  # stored rows only: nothing settled yet
        harness,
        builder_id=builder_id,
        subscription_id=subscription,
        cycle_id=cycle,
        start=start,
        end=end,
    )
    stored_open = await scalar(
        harness,
        "SELECT coalesce(sum(d.amount_mc), 0) - coalesce((SELECT sum(s.amount_mc) "
        "FROM credit_debt_settlements AS s WHERE s.builder_id = :b), 0) "
        "FROM credit_debts AS d WHERE d.builder_id = :b",
        b=builder_id,
    )
    assert stored_open == 4 * MC  # unswept: still open

    outcome = await run(harness)
    assert outcome.code == 0, outcome.document
    entry = outcome.builder(builder_id)
    assert entry["state"] == "protected"
    assert entry["open_debt"] == {"open_debt_mc": 0, "open_debts": 0}
    assert outcome.document["summary"]["open_debt_mc"] == 0


async def test_the_global_wins_over_the_rule(world: _Harness) -> None:
    harness = world
    builder_id = await make_builder(harness)
    await grant_courtesy(harness, builder_id, 2)
    armed = datetime.now(timezone.utc) - timedelta(days=3)
    await execute(
        harness,
        "INSERT INTO credit_enforcement_state (id, credit_enforcement_at, set_by, "
        "set_at) VALUES (1, :at, 'courtesy-rule-ops-test', :at)",
        at=armed,
    )
    outcome = await run(harness)
    assert outcome.code == 0, outcome.document
    assert _at(outcome.document["global_enforcement_at"]) == armed
    entry = outcome.builder(builder_id)
    assert entry["state"] == "global"
    assert entry["enforcement"]["origin"] == "global"
    assert _at(entry["enforcement"]["effective_at"]) == armed
    assert entry["enforcement"]["rule_state"] is None
    # Outside the rule the whole estimate is charged (a shortfall is debt).
    assert entry["reservation"]["payable_by_courtesy_mc"] is None


# ---------------------------------------------------------------------------
# The storage reservation of the first window after the deploy.
# ---------------------------------------------------------------------------


async def test_the_reservation_of_the_first_window_after_the_deploy(
    world: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = world
    current = accounting_window([], datetime.now(timezone.utc))

    # First grant inside the current window: it is the free baseline, the
    # next one charges maintenance.
    inside = await make_builder(harness)
    inside_tenant = await make_tenant(harness, inside)
    await grant_courtesy(harness, inside, 3)

    # First grant BEFORE the current window: the current one charges.
    before = await make_builder(harness)
    before_tenant = await make_tenant(harness, before)
    await _backdated_grant(
        harness, monkeypatch, before, 1, current.starts_at - timedelta(hours=6)
    )
    before_now = await grant_courtesy(harness, before, 4)  # charged now

    # Courtesy spent, a top-up in force: protected, nothing payable.
    protected, _ = await _spent_builder(harness)
    protected_tenant = await make_tenant(harness, protected)
    await make_topup(harness, protected)

    async with harness.sessions() as session, session.begin():
        session.add_all(
            [
                _knowledge(inside_tenant, 50_000),  # 10 credits
                _media(inside_tenant, 30_000_000),  # 1.5 credits
                _knowledge(before_tenant, 50_000),
                _media(before_tenant, 30_000_000),
                _knowledge(protected_tenant, 50_000),
                _media(protected_tenant, 30_000_000),
            ]
        )

    outcome = await run(harness)
    assert outcome.code == 0, outcome.document

    entry = outcome.builder(inside)
    assert entry["state"] == "charged"
    reservation = entry["reservation"]
    assert reservation["applies"] is True
    assert reservation["window"]["which"] == "next"
    assert reservation["window"]["start"] == current.end.isoformat()
    assert reservation["categories"]["rag"]["target_mc"] == 10_000
    assert reservation["categories"]["media"]["target_mc"] == 1_500
    assert reservation["estimated_charge_mc"] == 11_500
    assert reservation["payable_by_courtesy_mc"] == 3 * MC

    entry = outcome.builder(before)
    assert entry["state"] == "charged"
    first_at = datetime.fromisoformat(entry["courtesy"]["first_grant_at"])
    assert first_at < current.starts_at
    assert civil_date(first_at) < current.start
    reservation = entry["reservation"]
    assert reservation["window"] == {
        "which": "current",
        "start": current.start.isoformat(),
        "end": current.end.isoformat(),
        "kind": "calendar",
    }
    assert reservation["estimated_charge_mc"] == 11_500
    # Both lots may still spend (the first expires 30 days after its grant).
    spendable = entry["courtesy"]["spendable_mc"]
    assert spendable in (4 * MC, 5 * MC)
    assert reservation["payable_by_courtesy_mc"] == spendable
    # Blocked by expiry only once the LAST lot that can spend expires.
    assert _at(entry["courtesy"]["last_expiry_at"]) == before_now.expires_at
    if spendable == 5 * MC:  # the backdated lot spends and expires first
        assert _at(entry["courtesy"]["nearest_expiry_at"]) < before_now.expires_at
    else:
        assert entry["courtesy"]["nearest_expiry_at"] == (
            entry["courtesy"]["last_expiry_at"]
        )

    entry = outcome.builder(protected)
    assert entry["state"] == "protected"
    reservation = entry["reservation"]
    assert reservation["applies"] is False  # observational: nothing charged
    assert reservation["estimated_charge_mc"] == 11_500  # what the rule would
    assert reservation["payable_by_courtesy_mc"] == 0  # its courtesy is spent


# ---------------------------------------------------------------------------
# Clinics per channel, and no credential printed.
# ---------------------------------------------------------------------------


async def test_the_clinics_per_channel_print_no_credential(world: _Harness) -> None:
    harness = world
    builder_id = await make_builder(harness)
    await grant_courtesy(harness, builder_id, 1)
    uazapi = await make_tenant(harness, builder_id)
    await _config(
        harness,
        uazapi,
        {
            "whatsapp_provider": "uazapi",
            "uazapi_instance_name": ROUTING_VALUES[0],
            "uazapi_token": SECRETS[0],
            "uazapi_api_url": SECRETS[1],
        },
    )
    evolution = await make_tenant(harness, builder_id)
    await _config(
        harness,
        evolution,
        {
            "whatsapp_provider": "evolution",
            "evolution_instance_name": ROUTING_VALUES[1],
            "evolution_api_key": SECRETS[2],
        },
    )
    zapi = await make_tenant(harness, builder_id)
    await _config(
        harness,
        zapi,
        {
            "whatsapp_provider": "zapi",
            "zapi_inbound_token_digest": ROUTING_VALUES[2],
            "zapi_token": SECRETS[3],
            "llm_api_key": SECRETS[5],
        },
    )
    meta = await make_tenant(harness, builder_id)
    await _config(
        harness,
        meta,
        {
            "whatsapp_provider": "meta_cloud",
            "meta_phone_number_id": ROUTING_VALUES[3],
            "meta_permanent_token": SECRETS[4],
        },
    )
    # Uazapi keys without a provider: a Uazapi webhook still reaches it.
    routed_only = await make_tenant(harness, builder_id)
    await _config(
        harness,
        routed_only,
        {"uazapi_instance_name": f"{ROUTING_VALUES[0]}-2", "uazapi_token": SECRETS[0]},
    )
    blank = await make_tenant(harness, builder_id)
    await _config(
        harness, blank, {"whatsapp_provider": None, "uazapi_instance_name": "  "}
    )
    unknown = await make_tenant(harness, builder_id)
    await _config(harness, unknown, {"whatsapp_provider": SECRETS[6]})
    bare = await make_tenant(harness, builder_id)  # no config row
    other = await make_builder(harness)
    await make_tenant(harness, other)  # another builder's clinic

    outcome = await run(harness)
    assert outcome.code == 0, outcome.document
    clinics = outcome.builder(builder_id)["clinics"]
    assert clinics["total"] == 8

    def ids(*tenants: UUID) -> list[str]:
        return sorted(str(t) for t in tenants)

    by_channel = clinics["by_channel"]
    assert by_channel == {
        "uazapi": {"count": 2, "tenant_ids": ids(uazapi, routed_only)},
        "evolution": {"count": 1, "tenant_ids": ids(evolution)},
        "zapi": {"count": 1, "tenant_ids": ids(zapi)},
        "meta": {"count": 1, "tenant_ids": ids(meta)},
        "none": {"count": 3, "tenant_ids": ids(blank, unknown, bare)},
    }
    providers = {
        row["tenant_id"]: row["whatsapp_provider"] for row in clinics["tenants"]
    }
    assert providers[str(unknown)] == "unrecognized"
    assert providers[str(bare)] is None and providers[str(meta)] == "meta_cloud"
    routes = {row["tenant_id"]: row["inbound_routes"] for row in clinics["tenants"]}
    assert routes[str(routed_only)] == {
        "uazapi": True,
        "evolution": False,
        "zapi": False,
        "meta": False,
    }
    assert routes[str(blank)] == {c: False for c in ops.CHANNELS}
    expected_channels = {"uazapi": 2, "evolution": 1, "zapi": 1, "meta": 1, "none": 3}
    summary = outcome.document["summary"]
    assert summary["under_rule"]["clinics_per_channel"] == expected_channels
    assert summary["all_states"]["clinics_per_channel"] == expected_channels
    # Send credentials (owner decision 20): Uazapi and Meta complete;
    # Evolution without its URL, Z-API without its instance id and the
    # routed-only Uazapi tenant without its URL are incomplete; a tenant on
    # no channel lists nothing.
    incomplete = {
        row["tenant_id"]: row["credentials_incomplete"] for row in clinics["tenants"]
    }
    assert incomplete == {
        str(uazapi): [],
        str(evolution): ["evolution"],
        str(zapi): ["zapi"],
        str(meta): [],
        str(routed_only): ["uazapi"],
        str(blank): [],
        str(unknown): [],
        str(bare): [],
    }
    assert clinics["credentials_incomplete"] == {
        "count": 3,
        "tenant_ids": ids(evolution, zapi, routed_only),
        "by_channel": {
            "uazapi": {"count": 1, "tenant_ids": ids(routed_only)},
            "evolution": {"count": 1, "tenant_ids": ids(evolution)},
            "zapi": {"count": 1, "tenant_ids": ids(zapi)},
            "meta": {"count": 0, "tenant_ids": []},
        },
    }
    for scope in ("under_rule", "all_states"):
        assert summary[scope]["clinics_with_incomplete_credentials"] == 3
        assert summary[scope]["incomplete_credentials_per_channel"] == {
            "uazapi": 1,
            "evolution": 1,
            "zapi": 1,
            "meta": 0,
        }
    for value in (*SECRETS, *ROUTING_VALUES):
        assert value not in outcome.printed
    # The values are reduced inside the database: only the provider name and
    # booleans come back.
    named = {
        key
        for statement in outcome.statements
        for key in re.findall(r"api_credentials ->> '(\w+)'", statement)
    }
    assert named == READ_KEYS


async def test_the_list_judges_send_credentials_as_the_charged_sender(
    world: _Harness,
) -> None:
    """Owner decision 20 on real JSONB: every case of ``send_cases`` (each
    send key absent, JSON null, blank by ``str.strip()`` -- Unicode
    whitespace included -- or padded) gets from the list's SQL the verdict
    ``CreditReplySender.prepare`` gives on the same stored credentials; the
    entry point prints the same, prints no value and writes nothing."""

    harness = world
    builder_id = await make_builder(harness)
    await grant_courtesy(harness, builder_id, 1)
    cases = []
    for channel, label, credentials in send_cases():
        tenant_id = await make_tenant(harness, builder_id)
        await _config(harness, tenant_id, credentials)
        cases.append((tenant_id, channel, label))
    bare = await make_tenant(harness, builder_id)  # no config row
    async with harness.sessions() as session:
        try:
            listed = {
                row.tenant_id: row
                for row in await ops._tenant_channels(session, builder_id)
            }
            sender = {
                tenant_id: await sender_ready(session, tenant_id, channel)
                for tenant_id, channel, _ in cases
            }
            for channel in ops.CHANNELS:  # no row: the sender sends nowhere
                assert not await sender_ready(session, bare, channel)
        finally:
            await session.rollback()
    assert listed[bare].send_ready == {c: False for c in ops.CHANNELS}
    verdicts = compare_send_verdicts(cases, listed, sender)

    before = await _fingerprint(harness)
    outcome = await run(harness)
    assert await _fingerprint(harness) == before
    assert outcome.code == 0, outcome.document
    assert outcome.commits == 0 and outcome.rollbacks >= 1
    entry = outcome.builder(builder_id)
    tenants = {row["tenant_id"]: row for row in entry["clinics"]["tenants"]}
    for tenant_id, channel, label in cases:
        expected = [] if verdicts[(channel, label)] else [channel]
        assert tenants[str(tenant_id)]["credentials_incomplete"] == expected, label
    assert tenants[str(bare)]["credentials_incomplete"] == []
    incomplete = [case for case, ready in verdicts.items() if not ready]
    summary = outcome.document["summary"]
    for scope in ("under_rule", "all_states"):
        assert summary[scope]["clinics_with_incomplete_credentials"] == len(incomplete)
        assert summary[scope]["incomplete_credentials_per_channel"] == {
            channel: sum(1 for case in incomplete if case[0] == channel)
            for channel in ops.CHANNELS
        }
    assert "NEVER-PRINT" not in outcome.printed
    named = {
        key
        for statement in outcome.statements
        for key in re.findall(r"api_credentials ->> '(\w+)'", statement)
    }
    assert named == READ_KEYS


# ---------------------------------------------------------------------------
# ``/v1/events`` traffic without ``Idempotency-Key`` (wish Decision 9).
# ---------------------------------------------------------------------------


async def _effect(
    harness: _Harness,
    tenant_id: UUID,
    builder_id: UUID | None,
    *,
    age: timedelta = timedelta(hours=1),
    origin: str | None = "production",
    authenticated: bool | None = True,
    kind: str = "trace",
    trace_id: UUID | None = None,
) -> UUID:
    interaction = trace_id or uuid4()
    await execute(
        harness,
        """
        INSERT INTO credit_observational_effects (
            id, tenant_id, builder_id, effect_id, effect_kind, trace_id, origin,
            authenticated, would_debit_mc, recorded_at
        ) VALUES (:id, :tenant, :builder, :effect, :kind, :trace, :origin, :auth,
                  1000, now() - make_interval(secs => :age))
        """,
        id=uuid4(),
        tenant=tenant_id,
        builder=builder_id,
        effect=f"trace:{tenant_id}:{interaction}:decision:1",
        kind=kind,
        trace=interaction,
        origin=origin,
        auth=authenticated,
        age=age.total_seconds(),
    )
    return interaction


async def _claim(
    harness: _Harness,
    tenant_id: UUID,
    builder_id: UUID | None,
    interaction: UUID,
    *,
    source: str = "events",
) -> None:
    events = source == "events"
    await execute(
        harness,
        "INSERT INTO credit_ingress_claims (id, tenant_id, builder_id, source, "
        "principal_id, idempotency_key, provider_message_id, interaction_id, "
        "payload_sha256, decision_version, mode) VALUES (:id, :tenant, :builder, "
        ":source, :principal, :key, :message, :interaction, :sha, 1, "
        "'observational')",
        id=uuid4(),
        tenant=tenant_id,
        builder=builder_id,
        source=source,
        principal=f"{'api_key' if events else source}:{tenant_id}",
        key=f"idem-{uuid4()}" if events else None,
        message=None if events else f"msg-{uuid4()}",
        interaction=interaction,
        sha=SHA,
    )


async def test_events_without_an_idempotency_key_are_counted_per_tenant(
    world: _Harness,
) -> None:
    harness = world
    builder_id = await make_builder(harness)
    await grant_courtesy(harness, builder_id, 1)
    tenant = await make_tenant(harness, builder_id)
    quiet = await make_tenant(harness, builder_id)  # no traffic at all
    other_builder = await make_builder(harness)
    await grant_courtesy(harness, other_builder, 1)
    other = await make_tenant(harness, other_builder)
    plain_builder = await make_builder(harness)  # no courtesy: never listed
    plain = await make_tenant(harness, plain_builder)
    # Listed but not under the rule (protected by a top-up): its traffic and
    # clinic are in the all-states totals only.
    protected, _ = await _spent_builder(harness)
    await make_topup(harness, protected)
    protected_tenant = await make_tenant(harness, protected)
    await _config(harness, protected_tenant, {"whatsapp_provider": "uazapi"})
    await _effect(harness, protected_tenant, protected)
    await _effect(harness, protected_tenant, protected)

    # Counted: production, an API key, no claim, inside the 7 days.
    await _effect(harness, tenant, builder_id)
    await _effect(harness, tenant, builder_id, age=timedelta(days=6, hours=23))
    # A claim of ANOTHER tenant with the same interaction id hides nothing.
    shared = await _effect(harness, tenant, builder_id)
    await _claim(harness, other, other_builder, shared)
    # Not counted, each a control of one condition of the query:
    claimed = await _effect(harness, tenant, builder_id)
    await _claim(harness, tenant, builder_id, claimed)  # a keyed /v1/events
    webhook = await _effect(harness, tenant, builder_id)
    await _claim(harness, tenant, builder_id, webhook, source="zapi")
    await _effect(harness, tenant, builder_id, authenticated=False)
    await _effect(harness, tenant, builder_id, authenticated=None)
    await _effect(harness, tenant, builder_id, origin="simulator")
    await _effect(harness, tenant, builder_id, origin="unknown")
    await _effect(harness, tenant, builder_id, origin=None)
    await _effect(harness, tenant, builder_id, kind="media")
    await _effect(harness, tenant, builder_id, age=timedelta(days=8))
    await _effect(harness, tenant, builder_id, age=timedelta(days=7, minutes=1))
    # The other builder's own traffic, and a builder without courtesy.
    await _effect(harness, other, other_builder)
    await _effect(harness, plain, plain_builder)
    # The tenant's builder NOW decides, not the one stored with the effect:
    # a clinic moved from the other builder brings its traffic along.
    moved = await make_tenant(harness, other_builder)
    await _effect(harness, moved, other_builder)
    await relink_tenant(harness, moved, builder_id)

    outcome = await run(harness)
    assert outcome.code == 0, outcome.document
    events = outcome.builder(builder_id)["events_without_idempotency_key"]
    assert events["total"] == 4
    rows = {row["tenant_id"]: row for row in events["by_tenant"]}
    assert set(rows) == {str(tenant), str(moved)}
    assert rows[str(tenant)]["requests"] == 3
    assert rows[str(moved)]["requests"] == 1
    assert all(row["latest_at"] is not None for row in rows.values())
    assert str(quiet) not in json.dumps(events)
    other_events = outcome.builder(other_builder)["events_without_idempotency_key"]
    assert other_events == {
        "total": 1,
        "by_tenant": [
            {
                "tenant_id": str(other),
                "requests": 1,
                "latest_at": other_events["by_tenant"][0]["latest_at"],
            }
        ],
    }
    window = outcome.document["events_window"]
    assert window["days"] == 7
    assert datetime.fromisoformat(window["until"]) - datetime.fromisoformat(
        window["since"]
    ) == timedelta(days=7)
    assert window["approximation"] == ops.EVENTS_APPROXIMATION
    assert outcome.builder(protected)["state"] == "protected"
    assert outcome.builder(protected)["events_without_idempotency_key"]["total"] == 2
    summary = outcome.document["summary"]
    # Exposed to the new 400: the charged and blocked builders only.
    assert summary["under_rule"]["builders"] == 2
    assert summary["under_rule"]["events_without_idempotency_key"] == 5
    assert summary["under_rule"]["clinics_per_channel"]["uazapi"] == 0
    assert summary["all_states"]["builders"] == 3
    assert summary["all_states"]["events_without_idempotency_key"] == 7
    assert summary["all_states"]["clinics_per_channel"]["uazapi"] == 1


async def _session_row(harness: _Harness, tenant_id: UUID) -> UUID:
    session_id = uuid4()
    async with harness.sessions() as session, session.begin():
        session.add(
            Session(
                id=session_id,
                tenant_id=tenant_id,
                channel="test",
                cognitive_state={},
                session_metadata={},
                message_count=2,
            )
        )
    return session_id


async def test_the_count_matches_the_real_observational_writer(
    world: _Harness,
) -> None:
    """The drift guard of the count against ``credit_metering``: claims and
    effects written by ``record_observational_trace`` itself (not seeded),
    one per ingress shape.  Every claimed decision -- Z-API, Meta and
    verified Uazapi with a provider message id, ``/v1/events`` with an API
    key and an ``Idempotency-Key`` (and its retry) -- is left out; the two
    keyless ``/v1/events`` requests are counted.  The known approximation
    (``EVENTS_APPROXIMATION``): a Z-API decision without a message id takes
    no claim and IS counted."""

    harness = world
    builder_id = await make_builder(harness)
    await grant_courtesy(harness, builder_id, 1)
    tenant = await make_tenant(harness, builder_id)
    session_id = await _session_row(harness, tenant)
    approximated = await make_tenant(harness, builder_id)
    approximated_session = await _session_row(harness, approximated)

    def ingress(
        source: Any,
        kind: str,
        *,
        key: str | None = None,
        message: str | None = None,
        verified: bool = False,
        on: UUID = tenant,
        session: UUID = session_id,
    ) -> ObservationalIngress:
        identity = credit_identity(kind, on, inbound_verified=verified)
        assert identity is not None
        return ObservationalIngress(
            source=source,
            tenant_id=on,
            session_id=session,
            origin=InteractionOrigin.PRODUCTION,
            identity=identity,
            payload_sha256=SHA,
            idempotency_key=key,
            provider_message_id=message,
        )

    async def write(item: ObservationalIngress) -> str:
        return str(
            await record_observational_trace(
                item,
                anchor_id=uuid4(),
                decision={"kind": "pipeline"},
                deliverable=True,
                session_factory=harness.sessions,
            )
        )

    keyed = ingress("events", "api_key", key=f"idem-{uuid4()}")
    outcomes = [
        await write(ingress("zapi", "zapi", message=f"zapi-{uuid4()}")),
        await write(ingress("meta", "meta", message=f"wamid.{uuid4()}")),
        await write(
            ingress("uazapi", "uazapi", message=f"uaz-{uuid4()}", verified=True)
        ),
        await write(keyed),
        await write(keyed),  # the same key: a retry of the same request
        await write(ingress("events", "api_key")),
        await write(ingress("events", "api_key")),
        await write(
            ingress("zapi", "zapi", on=approximated, session=approximated_session)
        ),
    ]
    assert outcomes == [
        "recorded",
        "recorded",
        "recorded",
        "recorded",
        "replayed",
        "recorded",
        "recorded",
        "recorded",
    ]
    claims = await scalar(
        harness,
        "SELECT count(*) FROM credit_ingress_claims WHERE tenant_id = :t",
        t=tenant,
    )
    effects = await scalar(
        harness,
        "SELECT count(*) FROM credit_observational_effects WHERE tenant_id = :t "
        "AND effect_kind = 'trace' AND origin = 'production' "
        "AND authenticated IS TRUE",
        t=tenant,
    )
    assert (claims, effects) == (4, 6)  # four claimed, two keyless

    outcome = await run(harness)
    assert outcome.code == 0, outcome.document
    events = outcome.builder(builder_id)["events_without_idempotency_key"]
    rows = {row["tenant_id"]: row["requests"] for row in events["by_tenant"]}
    assert rows == {str(tenant): 2, str(approximated): 1}
    assert events["total"] == 3


# ---------------------------------------------------------------------------
# Read-only: SQL capture and a byte-identical database.
# ---------------------------------------------------------------------------


async def _fingerprint(harness: _Harness) -> dict[str, tuple[int, str]]:
    """Every table's row count and the md5 of its rows' text, and every
    sequence's last value."""

    async with harness.engine.connect() as connection:
        tables = [
            str(name)
            for (name,) in await connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                    "ORDER BY table_name"
                )
            )
        ]
        found: dict[str, tuple[int, str]] = {}
        for name in tables:
            quoted = connection.dialect.identifier_preparer.quote(name)
            count, digest = (
                await connection.execute(
                    text(
                        f"SELECT count(*), md5(coalesce(string_agg(t::text, '|' "
                        f"ORDER BY t::text), '')) FROM {quoted} AS t"
                    )
                )
            ).one()
            found[name] = (int(count), str(digest))
        for name, value in await connection.execute(
            text("SELECT sequencename, last_value FROM pg_sequences")
        ):
            found[f"sequence:{name}"] = (-1, str(value))
        await connection.rollback()
    return found


async def test_the_command_writes_nothing(world: _Harness) -> None:
    harness = world
    builder_id = await make_builder(harness)
    await grant_courtesy(harness, builder_id, 2)
    tenant = await make_tenant(harness, builder_id)
    await _config(harness, tenant, {"whatsapp_provider": "uazapi"})
    await _effect(harness, tenant, builder_id)
    spent, _ = await _spent_builder(harness)
    subscription = await make_subscription(harness, spent)
    today = civil_today()
    await make_cycle(
        harness,
        spent,
        subscription,
        start=today - timedelta(days=1),
        end=today + timedelta(days=29),
    )
    async with harness.sessions() as session, session.begin():
        session.add_all([_knowledge(tenant, 10_000), _media(tenant, 1_000_000)])

    before = await _fingerprint(harness)
    outcome = await run(harness)
    after = await _fingerprint(harness)
    assert outcome.code == 0, outcome.document
    assert len(outcome.document["builders"]) == 2
    assert after == before

    statements = [s.strip() for s in outcome.statements]
    assert statements
    opening = [i for i, s in enumerate(statements) if s == _READ_ONLY_SQL]
    assert len(opening) == 1  # the command's one transaction
    # Before it only the switch's own read ran; from it on, SELECTs only.
    for statement in statements[opening[0] + 1 :]:
        assert statement.upper().startswith("SELECT"), statement[:160]
    for statement in statements:
        if statement == _READ_ONLY_SQL:
            continue
        assert statement.upper().startswith(("SELECT", "SHOW")), statement[:160]
        assert not _WRITE.search(statement), statement[:160]
        assert "FOR UPDATE" not in statement.upper()
        assert "FOR SHARE" not in statement.upper()
    assert outcome.commits == 0 and outcome.rollbacks >= 1
