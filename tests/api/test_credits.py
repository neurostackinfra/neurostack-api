"""``/v1/credits/*`` read API (credit Group 6), SQLite.

The real application (lifespan, middleware, SQLite ``:memory:``) with the
credit switch forced per test.  The ledger states are written through the
real :class:`~src.services.credit_ledger.CreditLedger` (grants, debits,
debt), never as hand-made balances.  Pinned here:

* a credit surface: switch off => ``503 {"code": "credit_unavailable"}`` on
  every route, before anything else;
* the balance is discriminated (monthly / not-started allowance / top-ups /
  debt / net / reserved) and comes from the swept ledger, never from a
  pending purchase: a lot shows only once its grant is in the ledger;
* lots, expiries (an expiry due is applied to the VIEW, never persisted),
  debt, reservations, thresholds and consumption (charged vs observed);
* the states ``active``, ``pending`` (quarantine, standalone, an enforced
  binding coverage that is not usable: reconciliation), ``grace``,
  ``suspended``, ``deletion_due`` with their deadlines and the export
  deadline, and the mode ``observational`` until ``credit_enforcement_at``;
* the routes are read-only: no row, no effect, nothing persisted by a read;
  no body, no other method.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from sqlalchemy import func, select

from src.api.middleware.auth import pwd_context
from src.config.settings import settings
from src.models.billing import BillingSubscription, BillingSubscriptionCycle
from src.models.builder import Builder
from src.models.content_retention import (
    CreditLifecycleEpisode,
    CreditLifecycleTransition,
    CreditThresholdAlert,
)
from src.models.credit import (
    CreditAccount,
    CreditEffect,
    CreditEnforcementBinding,
    CreditEnforcementState,
    CreditLedgerEntry,
    CreditObservationalEffect,
)
from src.models.credit_inventory import CreditAllocation
from src.models.tenant import Tenant
from src.repositories.credit import CreditRepository
from src.repositories.database import get_db_session
from src.services.credit_allocations import accounting_window
from src.services.credit_ledger import (
    CreditLedger,
    GrantSpec,
    LedgerBook,
    add_calendar_months,
    civil_date,
    monthly_grant_effect_id,
    monthly_lot_expires_at,
    topup_grant_effect_id,
    topup_lot_expires_at,
)
from src.services.credit_lifecycle import arm_key
from src.services.credit_switch import CreditSwitch

UTC = timezone.utc
CREDITS = "/v1/credits"
READ_ROUTES = (
    "catalog",
    "summary",
    "balance",
    "lots",
    "expiries",
    "debt",
    "reservations",
    "thresholds",
    "state",
    "consumption",
)
UNAVAILABLE = {"code": "credit_unavailable"}


def _jwt(
    tenant_id: UUID,
    *,
    role: str,
    builder_id: UUID | None = None,
    subject: UUID | None = None,
) -> dict[str, str]:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": str(subject or uuid4()),
        "tenant_id": str(tenant_id),
        "role": role,
        "iat": now,
        "exp": now + timedelta(hours=1),
    }
    if builder_id is not None:
        claims["builder_id"] = str(builder_id)
    token = jwt.encode(claims, settings.secret_key, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


@dataclass
class Builders:
    """A world of builders, each with an own tenant and a clinic."""

    rich: UUID = field(default_factory=uuid4)
    rich_tenant: UUID = field(default_factory=uuid4)
    rich_clinic: UUID = field(default_factory=uuid4)
    indebted: UUID = field(default_factory=uuid4)
    indebted_tenant: UUID = field(default_factory=uuid4)
    expired: UUID = field(default_factory=uuid4)
    expired_tenant: UUID = field(default_factory=uuid4)
    empty: UUID = field(default_factory=uuid4)
    empty_tenant: UUID = field(default_factory=uuid4)
    standalone: UUID = field(default_factory=uuid4)
    admin_tenant: UUID = field(default_factory=uuid4)
    clinic_key: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    subscription: UUID = field(default_factory=uuid4)
    order: UUID = field(default_factory=uuid4)
    renewal_paid: date | None = None
    cycle_start: date | None = None
    cycle_end: date | None = None
    rag_effect: str = ""

    def builder(self, builder_id: UUID, tenant_id: UUID) -> dict[str, str]:
        return _jwt(tenant_id, role="builder", builder_id=builder_id)

    @property
    def rich_jwt(self) -> dict[str, str]:
        return self.builder(self.rich, self.rich_tenant)

    @property
    def indebted_jwt(self) -> dict[str, str]:
        return self.builder(self.indebted, self.indebted_tenant)

    @property
    def super_admin(self) -> dict[str, str]:
        return _jwt(self.admin_tenant, role="super_admin")


async def _plain(work: Callable[[Any], Awaitable[None]]) -> None:
    database = get_db_session()
    try:
        async with database.begin():
            await work(database)
    finally:
        await database.close()


async def _ledger(
    builder: UUID,
    operate: Callable[[LedgerBook, datetime], None],
    *,
    at: datetime | None = None,
) -> None:
    """One ledger transaction through the real service; ``at`` replaces the
    database clock (to write a history in the past)."""

    async def clock() -> datetime:
        assert at is not None
        return at

    async def work(database: Any) -> None:
        ledger = CreditLedger(database, reference_clock=clock if at else None)
        book = await ledger.open_book(builder, at=at or datetime.now(UTC))
        assert book.clock is not None
        operate(book, book.clock)
        await ledger.persist(book, lifecycle_source=None)

    await _plain(work)


def _monthly(world: Builders, start: date, end: date, paid: date, mc: int) -> GrantSpec:
    return GrantSpec(
        lot_kind="monthly",
        effect_id=monthly_grant_effect_id(world.subscription, start, end),
        payment_id=f"pay-{start.isoformat()}",
        paid_at=paid,
        granted_mc=mc,
        expires_at=monthly_lot_expires_at(end),
        subscription_id=world.subscription,
        cycle_start=start,
        cycle_end=end,
    )


def _topup(order: UUID, paid: date, mc: int) -> GrantSpec:
    return GrantSpec(
        lot_kind="topup",
        effect_id=topup_grant_effect_id(order),
        payment_id=f"pay-{order}",
        paid_at=paid,
        granted_mc=mc,
        expires_at=topup_lot_expires_at(paid),
        order_id=order,
    )


async def _seed(world: Builders) -> None:
    async def tenants(database: Any) -> None:
        for builder in (world.rich, world.indebted, world.expired, world.empty):
            database.add(Builder(id=builder, name=f"Credit {builder.hex[:6]}"))
        await database.flush()
        rows = (
            (world.rich_tenant, world.rich, ""),
            (world.rich_clinic, world.rich, world.clinic_key),
            (world.indebted_tenant, world.indebted, ""),
            (world.expired_tenant, world.expired, ""),
            (world.empty_tenant, world.empty, ""),
            (world.standalone, None, ""),
            (world.admin_tenant, None, ""),
        )
        for tenant_id, builder, key in rows:
            database.add(
                Tenant(
                    id=tenant_id,
                    name=f"Credit tenant {tenant_id.hex[:6]}",
                    slug=f"credit-{tenant_id.hex}",
                    api_key_hash=pwd_context.hash(key or secrets.token_urlsafe(16)),
                    builder_id=builder,
                    config={},
                )
            )

    await _plain(tenants)

    now = datetime.now(UTC)
    today = civil_date(now)
    world.cycle_start = today - timedelta(days=5)
    world.cycle_end = add_calendar_months(world.cycle_start, 1)
    world.renewal_paid = today
    world.rag_effect = f"rag:{world.rich_clinic}:{uuid4()}:reserve"
    cycle_start, cycle_end = world.cycle_start, world.cycle_end
    renewal_end = add_calendar_months(cycle_end, 1)

    def rich(book: LedgerBook, at: datetime) -> None:
        book.grant(_monthly(world, cycle_start, cycle_end, cycle_start, 10_000), at=at)
        # An early renewal: granted, not usable before its cycle starts.
        book.grant(_monthly(world, cycle_end, renewal_end, today, 10_000), at=at)
        book.grant(_topup(world.order, today - timedelta(days=10), 500_000), at=at)
        # Monthly first (10 000), then the top-up (2 000).
        book.debit("trace:rich:1", 12_000, at=at, effect_kind="trace")
        book.debit(
            world.rag_effect,
            2_000,
            at=at,
            effect_kind="rag",
            tenant_id=world.rich_clinic,
        )

    await _ledger(world.rich, rich)

    def indebted(book: LedgerBook, at: datetime) -> None:
        book.debit("trace:indebted:1", 3_000, at=at, effect_kind="trace")

    await _ledger(world.indebted, indebted)

    # A top-up granted eight months ago: past its six-month validity now,
    # with no expiry recorded yet (no writer since).
    past = now - timedelta(days=240)
    old_order = uuid4()

    def expired(book: LedgerBook, at: datetime) -> None:
        book.grant(_topup(old_order, civil_date(past), 1_000), at=at)

    await _ledger(world.expired, expired, at=past)

    async def window_rows(database: Any) -> None:
        account = await database.scalar(
            select(CreditAccount.id).where(CreditAccount.builder_id == world.rich)
        )
        common = {
            "builder_id": world.rich,
            "account_id": account,
            "window_start": cycle_start,
            "window_end": cycle_end,
        }
        database.add_all(
            [
                CreditAllocation(
                    **common,
                    category="rag",
                    allocation_kind="baseline",
                    source_key=f"window:{cycle_start.isoformat()}:rag:opening",
                    units=5_000,
                    target_mc=1_000,
                    charged_mc=0,
                ),
                CreditAllocation(
                    **common,
                    category="rag",
                    allocation_kind="reservation",
                    source_key=world.rag_effect,
                    units=15_000,
                    target_mc=3_000,
                    charged_mc=2_000,
                    effect_id=world.rag_effect,
                ),
                CreditAllocation(
                    **common,
                    category="media",
                    allocation_kind="baseline",
                    source_key=f"window:{cycle_start.isoformat()}:media:opening",
                    units=40_000_000,
                    target_mc=2_000,
                    charged_mc=0,
                ),
                CreditObservationalEffect(
                    tenant_id=world.rich_clinic,
                    builder_id=world.rich,
                    effect_id=f"trace:{world.rich_clinic}:{uuid4()}:decision:1",
                    effect_kind="trace",
                    origin="production",
                    authenticated=True,
                    would_debit_mc=1_000,
                ),
                CreditObservationalEffect(
                    tenant_id=world.rich_clinic,
                    builder_id=world.rich,
                    effect_id=f"rag:{world.rich_clinic}:{uuid4()}:reserve",
                    effect_kind="rag",
                    would_debit_mc=500,
                ),
            ]
        )

    await _plain(window_rows)


@pytest.fixture
def world(client: Any) -> Builders:
    seeded = Builders()
    client._submit(_seed(seeded))
    return seeded


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    def force(effective: bool) -> None:
        monkeypatch.setattr(CreditSwitch, "effective", lambda _self: effective)

    force(True)
    return force


def _get(client: Any, route: str, headers: dict[str, str], **params: str) -> Any:
    response = client.get(f"{CREDITS}/{route}", headers=headers, params=params)
    return response


def _ok(client: Any, route: str, headers: dict[str, str], **params: str) -> Any:
    response = _get(client, route, headers, **params)
    assert response.status_code == 200, (route, response.text)
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def _without_as_of(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_as_of(v) for k, v in value.items() if k != "as_of"}
    if isinstance(value, list):
        return [_without_as_of(item) for item in value]
    return value


async def _count(model: Any, *conditions: Any) -> int:
    database = get_db_session()
    try:
        return int(
            await database.scalar(
                select(func.count()).select_from(model).where(*conditions)
            )
            or 0
        )
    finally:
        await database.close()


async def _enforce(at: datetime | None = None) -> None:
    instant = at or datetime.now(UTC) - timedelta(days=10)

    async def work(database: Any) -> None:
        database.add(
            CreditEnforcementState(
                id=1, credit_enforcement_at=instant, set_by="test", set_at=instant
            )
        )

    await _plain(work)


async def _episode(
    builder: UUID, *, transitions: tuple[str, ...] = (), reactivated: bool = False
) -> CreditLifecycleEpisode:
    exhausted = datetime.now(UTC) - timedelta(hours=100)
    episode = CreditLifecycleEpisode(
        id=uuid4(),
        builder_id=builder,
        account_id=uuid4(),
        opened_effect_id=f"lifecycle:{builder}:episode:0:opened",
        exhausted_at=exhausted,
        suspension_due_at=exhausted + timedelta(hours=72),
        deletion_due_at=exhausted + timedelta(days=30),
        reactivated_at=datetime.now(UTC) if reactivated else None,
    )

    async def work(database: Any) -> None:
        account = await database.scalar(
            select(CreditAccount.id).where(CreditAccount.builder_id == builder)
        )
        episode.account_id = account
        database.add(episode)
        await database.flush()
        for name in transitions:
            database.add(
                CreditLifecycleTransition(
                    builder_id=builder,
                    episode_id=episode.id,
                    transition=name,
                    effect_id=f"lifecycle:{episode.id}:{name}",
                    occurred_at=datetime.now(UTC),
                    source="worker",
                    detail={},
                )
            )

    await _plain(work)
    return episode


# ---------------------------------------------------------------------------
# The switch.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", READ_ROUTES)
def test_switch_off_answers_credit_unavailable_on_every_route(
    client: Any, world: Builders, switch: Callable[[bool], None], route: str
) -> None:
    switch(False)
    response = _get(client, route, world.rich_jwt)
    assert (response.status_code, response.json()) == (503, UNAVAILABLE)
    # Not even a refusal is computed: an API key gets the same 503.
    response = _get(client, route, {"X-API-Key": world.clinic_key})
    assert (response.status_code, response.json()) == (503, UNAVAILABLE)


def test_switch_off_runs_no_credit_sql(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    from sqlalchemy import event

    from src.repositories.database import get_db_engine

    switch(False)
    statements: list[str] = []

    def record(_c: Any, _cur: Any, statement: str, *_a: Any) -> None:
        statements.append(statement)

    engine = get_db_engine().sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        for route in READ_ROUTES:
            assert _get(client, route, world.rich_jwt).status_code == 503
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert [s for s in statements if "credit_" in s or "billing_" in s] == []


def test_unauthenticated_is_refused(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    assert client.get(f"{CREDITS}/balance").status_code == 401


# ---------------------------------------------------------------------------
# Balance, lots, expiries, debt.
# ---------------------------------------------------------------------------


def test_the_balance_is_discriminated_by_origin(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    balance = _ok(client, "balance", world.rich_jwt)
    assert {key: value for key, value in balance.items() if key.endswith("_mc")} == {
        # The current allowance (10 000) was consumed first.
        "monthly_available_mc": 0,
        # The early renewal is granted but not usable before its cycle.
        "monthly_not_started_mc": 10_000,
        # 500 000 - 2 000 (trace) - 2 000 (RAG reservation).
        "topup_available_mc": 496_000,
        # creditos-cortesia: no courtesy lot here.
        "courtesy_available_mc": 0,
        "available_mc": 496_000,
        "open_debt_mc": 0,
        "net_available_mc": 496_000,
        # What the window's reservation debited (already left the lots).
        "reserved_mc": 2_000,
    }
    assert balance["next_expiry"]["kind"] == "monthly"
    assert balance["next_expiry"]["available_mc"] == 10_000
    assert balance["next_expiry"]["removed_mc"] == 0


def test_lots_carry_their_status_and_expiry(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    lots = _ok(client, "lots", world.rich_jwt)["lots"]
    by_status = {(lot["kind"], lot["status"]): lot for lot in lots}
    assert set(by_status) == {
        ("monthly", "exhausted"),
        ("monthly", "not_started"),
        ("topup", "active"),
    }
    current = by_status[("monthly", "exhausted")]
    assert (current["granted_mc"], current["available_mc"], current["consumed_mc"]) == (
        10_000,
        0,
        10_000,
    )
    assert current["cycle_start"] == world.cycle_start.isoformat()  # type: ignore[union-attr]
    topup = by_status[("topup", "active")]
    assert (topup["available_mc"], topup["consumed_mc"], topup["expired_mc"]) == (
        496_000,
        4_000,
        0,
    )
    assert topup["cycle_start"] is None
    expiries = _ok(client, "expiries", world.rich_jwt)["expiries"]
    # Holding capacity only, the earliest expiry first.
    assert [(e["kind"], e["available_mc"]) for e in expiries] == [
        ("monthly", 10_000),
        ("topup", 496_000),
    ]
    assert expiries[0]["expires_at"] < expiries[1]["expires_at"]


def test_an_expiry_due_is_applied_to_the_view_and_never_persisted(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    headers = world.builder(world.expired, world.expired_tenant)
    [lot] = _ok(client, "lots", headers)["lots"]
    assert (lot["status"], lot["available_mc"], lot["expired_mc"]) == (
        "expired",
        0,
        1_000,
    )
    assert _ok(client, "balance", headers)["available_mc"] == 0
    assert _ok(client, "expiries", headers)["expiries"] == []
    # The read wrote nothing: no expiry effect, no entry.
    assert (
        client._submit(_count(CreditEffect, CreditEffect.effect_id.like("expiry:%")))
        == 0
    )
    assert (
        client._submit(
            _count(CreditLedgerEntry, CreditLedgerEntry.entry_type == "expiry")
        )
        == 0
    )


def test_debt_is_open_consumption_beyond_capacity(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    debt = _ok(client, "debt", world.indebted_jwt)
    assert debt["open_debt_mc"] == 3_000
    [item] = debt["debts"]
    assert (item["source_kind"], item["amount_mc"], item["settled_mc"]) == (
        "consumption",
        3_000,
        0,
    )
    assert item["open_mc"] == 3_000
    balance = _ok(client, "balance", world.indebted_jwt)
    assert (balance["available_mc"], balance["open_debt_mc"]) == (0, 3_000)
    assert balance["net_available_mc"] == 0
    thresholds = _ok(client, "thresholds", world.indebted_jwt)
    assert thresholds["reached"] == [50, 90, 100]
    assert thresholds["used_percent"] == 100


def test_a_builder_without_any_credit_row_reads_zeros(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    headers = world.builder(world.empty, world.empty_tenant)
    balance = _ok(client, "balance", headers)
    assert {k: v for k, v in balance.items() if k.endswith("_mc")} == dict.fromkeys(
        (
            "monthly_available_mc",
            "monthly_not_started_mc",
            "topup_available_mc",
            "courtesy_available_mc",
            "available_mc",
            "open_debt_mc",
            "net_available_mc",
            "reserved_mc",
        ),
        0,
    )
    assert balance["next_expiry"] is None
    assert _ok(client, "lots", headers)["lots"] == []
    assert _ok(client, "state", headers)["state"] == "active"
    # A read never creates the account.
    assert (
        client._submit(_count(CreditAccount, CreditAccount.builder_id == world.empty))
        == 0
    )


# ---------------------------------------------------------------------------
# Reservations, thresholds, consumption.
# ---------------------------------------------------------------------------


def test_reservations_show_the_window_coverage(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    body = _ok(client, "reservations", world.rich_jwt)
    assert body["window"] == {
        "start": world.cycle_start.isoformat(),  # type: ignore[union-attr]
        "end": world.cycle_end.isoformat(),  # type: ignore[union-attr]
        "kind": "cycle",
    }
    assert body["reservations"] == [
        {"category": "rag", "units": 15_000, "coverage_mc": 3_000, "charged_mc": 2_000},
        {
            "category": "media",
            "units": 40_000_000,
            "coverage_mc": 2_000,
            "charged_mc": 0,
        },
        {"category": "egress", "units": 0, "coverage_mc": 0, "charged_mc": 0},
    ]
    # 200 MB of downloads per media credit covered.
    assert body["egress_allowance_bytes"] == 400_000_000
    assert body["egress_delivered_bytes"] == 0


def test_thresholds_count_the_capacity_in_force(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    body = _ok(client, "thresholds", world.rich_jwt)
    # Started, unexpired lots: the current allowance and the top-up.
    assert (body["capacity_mc"], body["available_mc"], body["used_mc"]) == (
        510_000,
        496_000,
        14_000,
    )
    assert (body["used_percent"], body["reached"], body["alerts"]) == (2, [], [])


def test_thresholds_list_the_alerts_of_the_current_arm(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    window = accounting_window([], datetime.now(UTC))
    key = arm_key(0, window.start.isoformat())

    async def work(database: Any) -> None:
        for threshold, arm in ((50, key), (90, key), (100, "g9:w1999-01-01")):
            database.add(
                CreditThresholdAlert(
                    builder_id=world.indebted,
                    arm_key=arm,
                    threshold=threshold,
                    capacity_mc=0,
                    available_mc=0,
                    effect_id=f"threshold:{world.indebted}:{arm}:{threshold}",
                    occurred_at=datetime.now(UTC),
                )
            )

    client._submit(_plain(work))
    body = _ok(client, "thresholds", world.indebted_jwt)
    # Only this arm's alerts (closed-episode count and window).
    assert [alert["threshold"] for alert in body["alerts"]] == [50, 90]


def test_consumption_separates_charged_from_observed(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    body = _ok(client, "consumption", world.rich_jwt)
    assert body["mode"] == "observational"
    assert body["window"]["kind"] == "cycle"
    assert (body["charged_mc"], body["charged"]) == (
        14_000,
        [{"kind": "rag", "amount_mc": 2_000}, {"kind": "trace", "amount_mc": 12_000}],
    )
    assert (body["observed_mc"], body["observed"]) == (
        1_500,
        [{"kind": "rag", "amount_mc": 500}, {"kind": "trace", "amount_mc": 1_000}],
    )
    # Consumption beyond capacity is consumption too (its debt entry).
    indebted = _ok(client, "consumption", world.indebted_jwt)
    assert indebted["charged"] == [{"kind": "trace", "amount_mc": 3_000}]


# ---------------------------------------------------------------------------
# State: active, pending, grace, suspended, deletion_due; mode; export deadline.
# ---------------------------------------------------------------------------


def test_before_the_enforcement_the_mode_is_observational(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    state = _ok(client, "state", world.rich_jwt)
    assert (state["mode"], state["enforcement_at"], state["state"]) == (
        "observational",
        None,
        "active",
    )
    assert state["pending_reasons"] == []
    assert state["export_deadline_at"] is None
    # An enforcement instant still in the future is not enforced yet.
    client._submit(_enforce(datetime.now(UTC) + timedelta(days=1)))
    assert _ok(client, "state", world.rich_jwt)["mode"] == "observational"


def test_after_the_enforcement_the_mode_is_enforced(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    client._submit(_enforce())
    state = _ok(client, "state", world.rich_jwt)
    assert state["mode"] == "enforced"
    assert state["enforcement_at"] is not None
    assert _ok(client, "consumption", world.rich_jwt)["mode"] == "enforced"


@pytest.mark.parametrize(
    ("transitions", "expected"),
    [
        ((), "grace"),
        (("suspended",), "suspended"),
        (("suspended", "deletion_due"), "deletion_due"),
    ],
)
def test_an_open_episode_gives_its_state_and_deadlines(
    client: Any,
    world: Builders,
    switch: Callable[[bool], None],
    transitions: tuple[str, ...],
    expected: str,
) -> None:
    client._submit(_enforce())
    episode = client._submit(_episode(world.indebted, transitions=transitions))
    state = _ok(client, "state", world.indebted_jwt)
    assert state["state"] == expected
    assert state["mode"] == "enforced"

    def instant(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    assert instant(state["exhausted_at"]) == episode.exhausted_at
    assert instant(state["suspension_due_at"]) == episode.suspension_due_at
    assert instant(state["deletion_due_at"]) == episode.deletion_due_at
    # The export deadline is the retention deadline of the open episode.
    assert state["export_deadline_at"] == state["deletion_due_at"]
    summary = _ok(client, "summary", world.indebted_jwt)
    assert summary["state"]["state"] == expected


def test_a_reactivated_episode_is_active_again(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    client._submit(_enforce())
    client._submit(
        _episode(world.indebted, transitions=("suspended",), reactivated=True)
    )
    state = _ok(client, "state", world.indebted_jwt)
    assert (state["state"], state["export_deadline_at"]) == ("active", None)


def test_a_quarantine_hold_is_pending(
    client: Any,
    world: Builders,
    switch: Callable[[bool], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[UUID] = []

    async def quarantined(_self: Any, builder_id: UUID) -> bool:
        seen.append(builder_id)
        return True

    monkeypatch.setattr(CreditRepository, "quarantine_hold", quarantined)
    state = _ok(client, "state", world.rich_jwt)
    assert (state["state"], state["pending_reasons"]) == ("pending", ["quarantine"])
    assert seen == [world.rich]
    # A lifecycle state outranks pending.
    client._submit(_enforce())
    client._submit(_episode(world.rich, transitions=("suspended",)))
    state = _ok(client, "state", world.rich_jwt)
    assert (state["state"], state["pending_reasons"]) == ("suspended", ["quarantine"])


def test_a_standalone_tenant_is_pending_without_an_account(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    state = _ok(client, "state", world.super_admin, tenant_id=str(world.standalone))
    assert (state["state"], state["pending_reasons"]) == ("pending", ["standalone"])
    balance = _ok(client, "balance", world.super_admin, tenant_id=str(world.standalone))
    assert balance["available_mc"] == 0


async def _current_cycle(world: Builders, binding: str) -> None:
    """An active ASAAS subscription of the rich builder with its current
    cycle, bound ``exact``-ly, bound with a conflicting identity
    (``conflict``), or not bound at all (``missing``)."""

    assert world.cycle_start is not None and world.cycle_end is not None
    start, end = world.cycle_start, world.cycle_end
    cycle_id = uuid4()

    async def work(database: Any) -> None:
        database.add(
            BillingSubscription(
                id=world.subscription,
                builder_id=world.rich,
                provider="asaas",
                external_subscription_id=f"sub_{world.subscription.hex}",
                is_active=True,
                plan="credits",
                billing_cycle="monthly",
                status="active",
            )
        )
        await database.flush()
        database.add(
            BillingSubscriptionCycle(
                id=cycle_id,
                builder_id=world.rich,
                subscription_id=world.subscription,
                payment_id=f"pay_{cycle_id.hex}",
                catalog_version_id="test",
                offer_id="test",
                cycle_start=start,
                cycle_end=end,
                amount_cents=10_000,
                credit_grant_mc=10_000,
                pricing_snapshot={},
                financial_snapshot={},
                state="settled",
                financial_revision=1,
                settled_cents=10_000,
            )
        )
        await database.flush()
        if binding == "missing":
            return
        database.add(
            CreditEnforcementBinding(
                builder_id=world.rich,
                subscription_id=world.subscription,
                subscription_cycle_id=cycle_id,
                cycle_start=start,
                # A conflict: the binding of this cycle names other dates.
                cycle_end=end + timedelta(days=1) if binding == "conflict" else end,
                financial_revision=1,
                grant_effect_id=monthly_grant_effect_id(world.subscription, start, end),
                status="active",
            )
        )

    await _plain(work)


@pytest.mark.parametrize("binding", ["conflict", "missing"])
def test_an_unusable_binding_coverage_is_pending_once_enforced(
    client: Any, world: Builders, switch: Callable[[bool], None], binding: str
) -> None:
    """The enforced path answers ``credit_pending`` when the binding coverage
    is not usable; the state must say so instead of an optimistic ``active``."""

    client._submit(_current_cycle(world, binding))
    # Observational: no enforced request is gated, nothing is pending.
    state = _ok(client, "state", world.rich_jwt)
    assert (state["state"], state["pending_reasons"]) == ("active", [])
    client._submit(_enforce())
    state = _ok(client, "state", world.rich_jwt)
    assert (state["mode"], state["state"], state["pending_reasons"]) == (
        "enforced",
        "pending",
        ["reconciliation"],
    )
    assert _without_as_of(_ok(client, "summary", world.rich_jwt)["state"]) == (
        _without_as_of(state)
    )


def test_an_exact_binding_stays_active_once_enforced(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    client._submit(_current_cycle(world, "exact"))
    client._submit(_enforce())
    state = _ok(client, "state", world.rich_jwt)
    assert (state["mode"], state["state"], state["pending_reasons"]) == (
        "enforced",
        "active",
        [],
    )


def test_a_quarantine_is_checked_before_the_binding_coverage(
    client: Any,
    world: Builders,
    switch: Callable[[bool], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As on the enforced request path, a quarantine excludes the coverage
    check: the reason is ``quarantine`` alone, never also ``reconciliation``."""

    async def quarantined(_self: Any, builder_id: UUID) -> bool:
        return True

    client._submit(_current_cycle(world, "conflict"))
    client._submit(_enforce())
    monkeypatch.setattr(CreditRepository, "quarantine_hold", quarantined)
    state = _ok(client, "state", world.rich_jwt)
    assert (state["mode"], state["state"], state["pending_reasons"]) == (
        "enforced",
        "pending",
        ["quarantine"],
    )


def test_a_lifecycle_state_outranks_a_reconciliation_pending(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    client._submit(_current_cycle(world, "conflict"))
    client._submit(_enforce())
    client._submit(_episode(world.rich, transitions=("suspended",)))
    state = _ok(client, "state", world.rich_jwt)
    assert (state["state"], state["pending_reasons"]) == (
        "suspended",
        ["reconciliation"],
    )


# ---------------------------------------------------------------------------
# Summary, catalog, read-only.
# ---------------------------------------------------------------------------


def test_the_summary_is_every_panel_of_one_snapshot(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    summary = _ok(client, "summary", world.rich_jwt)
    as_of = summary["as_of"]
    for part in ("state", "balance", "debt", "reservations", "thresholds"):
        assert summary[part]["as_of"] == as_of, part
    assert summary["consumption"]["as_of"] == as_of

    def without_as_of(body: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in body.items() if key != "as_of"}

    for part in ("balance", "debt", "reservations", "thresholds", "state"):
        assert without_as_of(summary[part]) == without_as_of(
            _ok(client, part, world.rich_jwt)
        ), part
    assert summary["expiries"] == _ok(client, "expiries", world.rich_jwt)["expiries"]


def test_the_catalog_sells_packages_by_card_or_pix_and_the_subscription_by_card(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    catalog = _ok(client, "catalog", world.rich_jwt)
    assert [
        (p["offer_id"], p["credits"], p["amount_cents"]) for p in catalog["packages"]
    ] == [
        ("package_credits_500_v1", 500, 10_000),
        ("package_credits_1000_v1", 1_000, 20_000),
        ("package_credits_2000_v1", 2_000, 40_000),
        ("package_credits_4000_v1", 4_000, 80_000),
    ]
    assert {tuple(p["payment_methods"]) for p in catalog["packages"]} == {
        ("credit_card", "pix")
    }
    assert {p["validity_months"] for p in catalog["packages"]} == {6}
    assert catalog["subscription_payment_methods"] == ["credit_card"]
    assert catalog["subscription_cycle"] == "monthly"
    assert catalog["checkout_endpoint"] == "/v1/billing/checkout"
    assert catalog["millicredits_per_credit"] == 1_000
    assert catalog["units"] == {
        "trace_mc": 1_000,
        "rag_characters_per_credit": 5_000,
        "media_bytes_per_credit": 20_000_000,
        "egress_included_bytes_per_credit": 200_000_000,
        "egress_overage_bytes_per_credit": 100_000_000,
    }


def test_reads_write_nothing(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    from src.models import content_retention, credit, credit_inventory

    models = [
        getattr(module, name)
        for module in (credit, content_retention, credit_inventory)
        for name in dir(module)
        if isinstance(getattr(module, name), type)
        and getattr(getattr(module, name), "__tablename__", "").startswith("credit_")
    ]

    def counts() -> dict[str, int]:
        return {model.__tablename__: client._submit(_count(model)) for model in models}

    before = counts()
    for headers in (world.rich_jwt, world.indebted_jwt):
        for route in READ_ROUTES:
            _ok(client, route, headers)
    _ok(
        client,
        "summary",
        world.builder(world.expired, world.expired_tenant),
    )
    assert counts() == before


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("route", ["balance", "summary", "catalog", "lots"])
def test_no_route_accepts_a_body_or_another_method(
    client: Any,
    world: Builders,
    switch: Callable[[bool], None],
    method: str,
    route: str,
) -> None:
    response = client.request(
        method,
        f"{CREDITS}/{route}",
        headers=world.rich_jwt,
        json={"credits": 4_000, "builder_id": str(world.indebted), "amount_mc": 1},
    )
    assert response.status_code == 405


def test_client_values_never_reach_the_figures(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    """Query values other than ``tenant_id`` are ignored: a builder id, an
    amount, a validity or a checkout-return marker change nothing."""

    clean = _ok(client, "summary", world.rich_jwt)
    tampered = _ok(
        client,
        "summary",
        world.rich_jwt,
        builder_id=str(world.indebted),
        credits="4000",
        available_mc="999999999",
        expires_at="2099-01-01T00:00:00Z",
        checkout="success",
    )

    assert _without_as_of(tampered) == _without_as_of(clean)


def test_a_credit_scope_needs_a_tenant_or_a_builder() -> None:
    """creditos-cortesia: ``tenant_id`` may be ``None`` only for the super
    admin's read of one builder by id; a scope with neither is refused at
    construction, never served as an empty snapshot."""

    from src.api.routes.credits import CreditScope

    with pytest.raises(ValueError, match="needs a tenant or a builder"):
        CreditScope(tenant_id=None, builder_id=None)
    builder = uuid4()
    assert CreditScope(None, builder).builder_id == builder
    assert CreditScope(uuid4(), None).builder_id is None


# ---------------------------------------------------------------------------
# debito-cortesia-automatico Group 5a: the courtesy rule on ``/v1/credits/*``
# (DESIGN items 4, 5, 7 and 13; wish Decision 4).  The rule needs the 046
# gate open on the process reading (``rule_reading``); no
# ``credit_enforcement_at`` is set: the rule enforces a builder with
# courtesy, no mark and no global, from its first grant.
# ---------------------------------------------------------------------------


@pytest.fixture
def rule_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process reading at this image's head (046 and 047 gates open)."""

    from src.services.credit_switch import (
        CreditSwitchReading,
        load_image_migration_chain,
    )

    reading = CreditSwitchReading(
        True, "schema_ready", load_image_migration_chain()[-1]
    )
    monkeypatch.setattr(CreditSwitch, "reading", lambda _self: reading)


async def _courtesy(builder: UUID, credits: int) -> Any:
    from src.services import credit_courtesy

    result: list[Any] = []

    async def work(database: Any) -> None:
        result.append(
            await credit_courtesy.grant(
                database,
                builder_id=builder,
                credits=credits,
                reason="cortesia",
                actor_user_id=uuid4(),
                idempotency_key=f"rule-{uuid4()}",
            )
        )

    await _plain(work)
    return result[0]


async def _remove_all(lot_id: UUID) -> None:
    from src.services import credit_courtesy

    async def work(database: Any) -> None:
        await credit_courtesy.remove(
            database,
            lot_id=lot_id,
            remove_all=True,
            reason="tirar tudo",
            actor_user_id=uuid4(),
            idempotency_key=f"rule-removal-{uuid4()}",
        )

    await _plain(work)


async def _rule_debit(builder: UUID, amount: int, first_grant_at: datetime) -> Any:
    """A trace debit captured under the rule (``charged``), as a request's."""

    from src.repositories.credit import EffectiveEnforcement

    found: list[Any] = []

    async def work(database: Any) -> None:
        found.append(
            await CreditLedger(database).debit(
                builder,
                f"trace:{uuid4()}:decision:1",
                amount,
                at=datetime.now(UTC),
                effect_kind="trace",
                captured=EffectiveEnforcement(
                    first_grant_at, "courtesy", state="charged"
                ),
            )
        )

    await _plain(work)
    return found[0]


async def _mark(builder: UUID) -> None:
    from src.models.credit import CreditEnforcementMark

    async def work(database: Any) -> None:
        database.add(
            CreditEnforcementMark(
                id=uuid4(),
                builder_id=builder,
                kind="mark",
                occurred_at=datetime.now(UTC) - timedelta(days=1),
                actor_user_id=uuid4(),
                reason="piloto",
            )
        )

    await _plain(work)


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


_DEADLINES = (
    "exhausted_at",
    "suspension_due_at",
    "deletion_due_at",
    "export_deadline_at",
)


def test_a_charged_rule_builder_is_active_and_enforced_with_no_deadline(
    client: Any, world: Builders, switch: Any, rule_reading: None
) -> None:
    headers = world.builder(world.empty, world.empty_tenant)
    first = client._submit(_courtesy(world.empty, 5))
    client._submit(_courtesy(world.empty, 2))  # never moves the rule's instant
    state = _ok(client, "state", headers)
    assert (state["mode"], state["state"], state["pending_reasons"]) == (
        "enforced",
        "active",
        [],
    )
    assert _instant(state["enforcement_at"]) == first.occurred_at.astimezone(UTC)
    assert all(state[name] is None for name in _DEADLINES)
    # An episode a mark era left open (suspended, deletion due) shows
    # neither its state nor its deadlines under the rule.
    client._submit(_episode(world.empty, transitions=("suspended", "deletion_due")))
    stale = _ok(client, "state", headers)
    assert _without_as_of(stale) == _without_as_of(state)
    assert _ok(client, "consumption", headers)["mode"] == "enforced"


def test_a_blocked_rule_builder_is_suspended_with_every_deadline_null(
    client: Any, world: Builders, switch: Any, rule_reading: None
) -> None:
    headers = world.builder(world.empty, world.empty_tenant)
    granted = client._submit(_courtesy(world.empty, 3))
    client._submit(_remove_all(granted.lot_id))  # "Tirar tudo", no paid plan
    state = _ok(client, "state", headers)
    assert (state["mode"], state["state"], state["pending_reasons"]) == (
        "enforced",
        "suspended",
        [],
    )
    assert _instant(state["enforcement_at"]) == granted.occurred_at.astimezone(UTC)
    assert all(state[name] is None for name in _DEADLINES)
    client._submit(_episode(world.empty, transitions=("suspended",)))
    assert _without_as_of(_ok(client, "state", headers)) == _without_as_of(state)
    # Zero courtesy capacity in force reads as 100 % used, nothing reached.
    assert _without_as_of(_ok(client, "thresholds", headers)) == {
        "capacity_mc": 0,
        "available_mc": 0,
        "used_mc": 0,
        "used_percent": 100,
        "reached": [],
        "alerts": [],
    }
    # A new courtesy unblocks at the next read (nothing is stored).
    client._submit(_courtesy(world.empty, 1))
    assert _ok(client, "state", headers)["state"] == "active"
    # DESIGN Decision 7: the courtesy expires with its balance left and no
    # paid plan in force -- blocked again, never spending the expired lot.
    client._submit(_expire_courtesy(world.empty))
    expired = _ok(client, "state", headers)
    assert (expired["mode"], expired["state"], expired["pending_reasons"]) == (
        "enforced",
        "suspended",
        [],
    )
    assert all(expired[name] is None for name in _DEADLINES)
    assert _ok(client, "balance", headers)["net_available_mc"] == 0


async def _expire_courtesy(builder: UUID) -> None:
    """Age every courtesy lot of ``builder`` past its expiry (balance kept)."""

    from sqlalchemy import update

    from src.models.credit import CreditLot as CreditLotRow

    async def work(database: Any) -> None:
        await database.execute(
            update(CreditLotRow)
            .where(
                CreditLotRow.builder_id == builder,
                CreditLotRow.lot_kind == "courtesy",
            )
            .values(expires_at=datetime.now(UTC) - timedelta(days=1))
        )

    await _plain(work)


@pytest.mark.parametrize("blocked", [False, True])
def test_under_the_rule_quarantine_stays_a_reason_and_never_pends(
    client: Any,
    world: Builders,
    switch: Any,
    rule_reading: None,
    monkeypatch: pytest.MonkeyPatch,
    blocked: bool,
) -> None:
    """Wish Decision 4: ``quarantine`` stays in ``pending_reasons`` (the
    top-up stays refused), the state is the rule's (never ``pending``) and
    the binding coverage is never read (no ``reconciliation``)."""

    from src.services.credit_rollout import CreditRolloutService

    granted = client._submit(_courtesy(world.empty, 3))
    if blocked:
        client._submit(_remove_all(granted.lot_id))

    async def quarantined(_self: Any, builder_id: UUID) -> bool:
        return True

    async def never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the binding coverage is read under the rule")

    monkeypatch.setattr(CreditRepository, "quarantine_hold", quarantined)
    monkeypatch.setattr(CreditRolloutService, "binding_coverage", never)
    headers = world.builder(world.empty, world.empty_tenant)
    for state in (
        _ok(client, "state", headers),
        _ok(client, "summary", headers)["state"],
    ):
        assert (state["mode"], state["state"], state["pending_reasons"]) == (
            "enforced",
            "suspended" if blocked else "active",
            ["quarantine"],
        )


def test_an_unusable_coverage_never_adds_reconciliation_under_the_rule(
    client: Any, world: Builders, switch: Any, rule_reading: None
) -> None:
    """The rich builder's current cycle has no binding: enforced by the
    global it is ``pending``/``reconciliation`` (above); under the rule its
    debits touch only courtesy, so it stays ``active``."""

    client._submit(_current_cycle(world, "missing"))
    client._submit(_courtesy(world.rich, 4))
    state = _ok(client, "state", world.rich_jwt)
    assert (state["mode"], state["state"], state["pending_reasons"]) == (
        "enforced",
        "active",
        [],
    )


def test_the_balance_by_kind_stays_and_net_is_the_spendable_courtesy(
    client: Any, world: Builders, switch: Any, rule_reading: None
) -> None:
    before = _ok(client, "balance", world.rich_jwt)
    client._submit(_courtesy(world.rich, 7))
    after = _ok(client, "balance", world.rich_jwt)
    for line in (
        "monthly_available_mc",
        "monthly_not_started_mc",
        "topup_available_mc",
        "open_debt_mc",
        "reserved_mc",
    ):
        assert after[line] == before[line], line
    assert after["courtesy_available_mc"] == 7_000
    assert after["available_mc"] == before["available_mc"] + 7_000
    # Under the rule: all it spends is the courtesy (never the paid lots).
    assert after["net_available_mc"] == 7_000
    # A debt a mark era left stays shown, and is neither netted nor settled.
    headers = world.builder(world.empty, world.empty_tenant)
    client._submit(_courtesy(world.empty, 5))
    client._submit(_old_debt(world.empty, 3_000))
    empty = _ok(client, "balance", headers)
    assert (empty["open_debt_mc"], empty["available_mc"]) == (3_000, 5_000)
    assert empty["net_available_mc"] == empty["courtesy_available_mc"] == 5_000
    summary = _ok(client, "summary", headers)
    assert _without_as_of(summary["balance"]) == _without_as_of(empty)
    assert _ok(client, "debt", headers)["open_debt_mc"] == 3_000


async def _old_debt(builder: UUID, amount: int) -> None:
    """An open debt left from a mark era (the rule makes none)."""

    from src.models.credit import CreditDebt

    effect = f"trace:{uuid4()}:decision:1"
    at = datetime.now(UTC) - timedelta(days=2)

    async def work(database: Any) -> None:
        account = await database.scalar(
            select(CreditAccount.id).where(CreditAccount.builder_id == builder)
        )
        database.add(
            CreditEffect(builder_id=builder, effect_id=effect, effect_kind="trace")
        )
        await database.flush()
        database.add(
            CreditLedgerEntry(
                builder_id=builder,
                account_id=account,
                effect_id=effect,
                lot_id=None,
                entry_type="debt",
                amount_mc=amount,
                occurred_at=at,
            )
        )
        database.add(
            CreditDebt(
                builder_id=builder,
                account_id=account,
                debt_source_kind="debit",
                debt_source_effect_id=effect,
                original_grant_lot_id=None,
                amount_mc=amount,
                created_at=at,
            )
        )

    await _plain(work)


def test_the_thresholds_are_on_the_courtesy_basis_with_its_arm(
    client: Any, world: Builders, switch: Any, rule_reading: None
) -> None:
    """DESIGN items 4 and 13: capacity the courtesy in force, available the
    spendable courtesy, the alerts of ``c:{grant_effect_id}`` of the latest
    grant -- recorded by the lifecycle on the rule's own debit."""

    from src.services.credit_lifecycle import courtesy_arm_key

    headers = world.builder(world.empty, world.empty_tenant)
    granted = client._submit(_courtesy(world.empty, 10))
    debit = client._submit(_rule_debit(world.empty, 6_000, granted.occurred_at))
    assert debit.status == "applied" and debit.debited_mc == 6_000

    async def plant(database: Any) -> None:
        # An alert on an episode-and-window arm is never this basis'.
        window = accounting_window([], datetime.now(UTC))
        key = arm_key(0, window.start.isoformat())
        database.add(
            CreditThresholdAlert(
                builder_id=world.empty,
                arm_key=key,
                threshold=90,
                capacity_mc=0,
                available_mc=0,
                effect_id=f"threshold:{world.empty}:{key}:90",
                occurred_at=datetime.now(UTC),
            )
        )

    client._submit(_plain(plant))
    body = _ok(client, "thresholds", headers)
    assert (body["capacity_mc"], body["available_mc"], body["used_mc"]) == (
        10_000,
        4_000,
        6_000,
    )
    assert (body["used_percent"], body["reached"]) == (60, [50])
    assert [alert["threshold"] for alert in body["alerts"]] == [50]

    async def arms(database: Any) -> list[str]:
        return [
            str(key)
            for key in (
                await database.execute(
                    select(CreditThresholdAlert.arm_key).where(
                        CreditThresholdAlert.builder_id == world.empty,
                        CreditThresholdAlert.threshold == 50,
                    )
                )
            ).scalars()
        ]

    found: list[list[str]] = []

    async def read(database: Any) -> None:
        found.append(await arms(database))

    client._submit(_plain(read))
    lot_effect = client._submit(_grant_effect(granted.lot_id))
    assert found == [[courtesy_arm_key(lot_effect)]]
    # A new grant re-arms on its own key: the lifecycle records that arm's
    # 50 % at the grant (6 000 of 12 000), and only that arm is listed.
    second = client._submit(_courtesy(world.empty, 2))
    body = _ok(client, "thresholds", headers)
    assert (body["capacity_mc"], body["available_mc"], body["used_mc"]) == (
        12_000,
        6_000,
        6_000,
    )
    assert body["reached"] == [50]
    found.clear()
    client._submit(_plain(read))
    second_key = courtesy_arm_key(client._submit(_grant_effect(second.lot_id)))
    assert sorted(found[0]) == sorted([courtesy_arm_key(lot_effect), second_key])

    async def occurred(database: Any) -> None:
        found.append(
            [
                _utc_iso(at)
                for at in (
                    await database.execute(
                        select(CreditThresholdAlert.occurred_at).where(
                            CreditThresholdAlert.builder_id == world.empty,
                            CreditThresholdAlert.arm_key == second_key,
                        )
                    )
                ).scalars()
            ]
        )

    client._submit(_plain(occurred))
    assert [(a["threshold"], a["occurred_at"]) for a in body["alerts"]] == [
        (50, found[-1][0])
    ]
    summary = _ok(client, "summary", headers)
    assert _without_as_of(summary["thresholds"]) == _without_as_of(body)


def _utc_iso(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def _grant_effect(lot_id: UUID) -> str:
    from src.models.credit import CreditLot as CreditLotRow

    database = get_db_session()
    try:
        return str(
            await database.scalar(
                select(CreditLotRow.grant_effect_id).where(CreditLotRow.id == lot_id)
            )
        )
    finally:
        await database.close()


def test_a_protected_builder_stays_observational(
    client: Any, world: Builders, switch: Any, rule_reading: None
) -> None:
    """Courtesy spent with a paid lot in force (the rich builder's top-up):
    no origin -- every route answers as without the rule."""

    before = {route: _ok(client, route, world.rich_jwt) for route in ("state",)}
    granted = client._submit(_courtesy(world.rich, 2))
    client._submit(_remove_all(granted.lot_id))
    state = _ok(client, "state", world.rich_jwt)
    assert _without_as_of(state) == _without_as_of(before["state"])
    assert state["mode"] == "observational"
    thresholds = _ok(client, "thresholds", world.rich_jwt)
    assert thresholds["capacity_mc"] == 510_000  # the episode basis, as before


@contextmanager
def _sql() -> Iterator[list[str]]:
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


def _is_rule_read(statement: str) -> bool:
    """``CreditRepository.courtesy_rule``'s one statement (Group 1)."""

    return "as granted_at" in statement and "from credit_lots" in statement


def test_dominance_without_courtesy_and_for_a_marked_builder(
    client: Any,
    world: Builders,
    switch: Any,
    rule_reading: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For a builder that never had courtesy and for a marked one (with
    courtesy), every ``/v1/credits/*`` answer is the same with the rule's
    046 read open and closed; with no courtesy the balance and thresholds
    run exactly the same statements and the state only the rule's one read
    (Group 1's); nothing is written."""

    client._submit(_enforce_mark_world(world))
    marked = world.builder(world.expired, world.expired_tenant)
    plain = world.rich_jwt

    def answers(headers: dict[str, str]) -> dict[str, Any]:
        return {
            route: _without_as_of(_ok(client, route, headers)) for route in READ_ROUTES
        }

    def statements(route: str) -> list[str]:
        with _sql() as seen:
            _ok(client, route, plain)
        return seen

    opened = {"marked": answers(marked), "plain": answers(plain)}
    opened_sql = {
        route: statements(route) for route in ("balance", "thresholds", "state")
    }
    monkeypatch.setattr(
        CreditRepository, "courtesy_rule_readable", staticmethod(lambda *_: False)
    )
    closed = {"marked": answers(marked), "plain": answers(plain)}
    closed_sql = {
        route: statements(route) for route in ("balance", "thresholds", "state")
    }
    assert opened == closed
    assert opened["marked"]["state"]["mode"] == "enforced"  # the mark's
    for route in ("balance", "thresholds"):
        assert opened_sql[route] == closed_sql[route], route
        assert not any(_is_rule_read(s) for s in opened_sql[route]), route
    rule_reads = [s for s in opened_sql["state"] if _is_rule_read(s)]
    assert len(rule_reads) == 1
    assert [s for s in opened_sql["state"] if s not in rule_reads] == closed_sql[
        "state"
    ]


async def _enforce_mark_world(world: Builders) -> None:
    """The expired builder gets courtesy and a mark (the mark wins)."""

    await _courtesy(world.expired, 3)
    await _mark(world.expired)


def test_rule_reads_write_nothing(
    client: Any, world: Builders, switch: Any, rule_reading: None
) -> None:
    from src.models import content_retention, credit, credit_inventory

    granted = client._submit(_courtesy(world.empty, 2))
    client._submit(_courtesy(world.indebted, 2))
    client._submit(_remove_all(granted.lot_id))
    models = [
        getattr(module, name)
        for module in (credit, content_retention, credit_inventory)
        for name in dir(module)
        if isinstance(getattr(module, name), type)
        and getattr(getattr(module, name), "__tablename__", "").startswith("credit_")
    ]

    def counts() -> dict[str, int]:
        return {model.__tablename__: client._submit(_count(model)) for model in models}

    before = counts()
    for headers in (
        world.builder(world.empty, world.empty_tenant),
        world.indebted_jwt,
    ):
        for route in READ_ROUTES:
            _ok(client, route, headers)
    assert counts() == before
