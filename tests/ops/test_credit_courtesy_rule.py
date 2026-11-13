"""Proofs of ``python -m src.ops.credit_courtesy_rule status``
(debito-cortesia-automatico, Group 6: the read-only pre-deploy list).

Without a database: the argument contract (anything but a bare ``status`` is
``unconfigured``/``usage`` before any session is opened), the closed result
vocabulary and exit codes, the switch-off path doing no database work, the
printable provider vocabulary, the channels per clinic and the channels
with incomplete send credentials (and their totals), the state each
``EffectiveEnforcement`` prints as, the first window after the deploy and
the reservation arithmetic (the inventory code's own functions), the
``READ ONLY`` first statement, the module's boundary (the charge-mark AST
fence still holds over ``src`` with this module in it, and this module
never names the marks table nor imports the charge-mark command) and the
real ``python -m`` entry point printing exactly one stdout line.

On SQLite (``create_all`` plus an ``alembic_version`` row, so the real switch
reads the revision): ``status`` over builders granted courtesy through the
real service, with SQL capture -- every statement is a SELECT, the
transaction is rolled back, every table keeps its rows, no credential key
other than :data:`READ_KEYS` (the provider, reduced in SQL with the routing
and send keys) is ever named and no credential value, instance name or
phone-number id reaches the output or the log; and the send-credential
verdict of every case of :func:`send_cases` pinned against
``CreditReplySender.prepare`` on the same stored credentials (owner
decision 20).
``tests/integration/test_credit_courtesy_rule_ops_postgres.py`` proves the
states, the traffic count and the byte-identical database on PostgreSQL,
where the transaction is ``REPEATABLE READ READ ONLY``.
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import src.models  # noqa: F401  (registers every mapper for create_all)
from src.api.routes.webhook import CreditReplySender
from src.config.settings import settings
from src.interaction_context import InteractionOrigin
from src.models.base import Base
from src.models.builder import Builder
from src.models.clinic_config_table import ClinicConfigTable
from src.models.knowledge import KnowledgeDocument
from src.models.tenant import Tenant
from src.ops import credit_courtesy_rule as ops
from src.repositories.credit import (
    CourtesyRuleReading,
    EffectiveEnforcement,
    PaidPlanInForce,
)
from src.repositories.credit_inventory import SourceRow
from src.repositories.database import _patch_jsonb_for_sqlite
from src.services import credit_courtesy, credit_metering, credit_switch
from src.services.credit_allocations import AccountingWindow, accounting_window
from src.services.credit_inventory import InventorySnapshot
from src.services.credit_metering import REPLY_PAYLOAD_KIND, CreditReplyUndeliverable
from tests.ops.test_credit_charge_mark import (
    _fence_findings,
    _imports_command,
    _naming_sites,
    _sources,
)

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "src" / "ops" / "credit_courtesy_rule.py"
ACTOR = UUID("6c4f3e71-8d9e-4fa0-9b1c-3d4e5f607182")
T0 = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)
# Credential values a tenant may hold; none may ever be printed or logged.
SECRETS = (
    "uazapi-token-NEVER-PRINT-55aa",
    "https://user:pass-NEVER-PRINT@uazapi.example",
    "evolution-api-key-NEVER-PRINT-77ce",
    "zapi-token-NEVER-PRINT-71c9",
    "meta-permanent-token-NEVER-PRINT-0b1e",
    "sk-llm-key-NEVER-PRINT-9f3d",
    "provider-value-that-is-a-secret-NEVER-PRINT",
)
# Routing values: identifiers of the provider, never printed either.
ROUTING_VALUES = (
    "instance-uazapi-NEVER-PRINT",
    "instance-evolution-NEVER-PRINT",
    "f" * 64,
    "5511999990000",
)
# The only keys of ``api_credentials`` the command names in SQL: the
# provider, the four routing keys and the send keys of the charged reply
# sender (owner decision 20).
READ_KEYS = frozenset(
    {
        "whatsapp_provider",
        "uazapi_instance_name",
        "evolution_instance_name",
        "zapi_inbound_token_digest",
        "meta_phone_number_id",
        "uazapi_api_url",
        "uazapi_token",
        "evolution_api_url",
        "evolution_api_key",
        "zapi_token",
        "zapi_instance_id",
        "meta_permanent_token",
    }
)


class _Untouchable:
    """A session factory that fails the test when used."""

    def __call__(self, *args: object, **kwargs: object) -> Any:
        raise AssertionError("no database work was allowed here")


async def _invoke(
    argv: list[str], session_factory: Any = None
) -> tuple[int, dict[str, Any], str]:
    out, err = io.StringIO(), io.StringIO()
    code = await ops.amain(
        argv,
        session_factory=_Untouchable() if session_factory is None else session_factory,
        stdout=out,
        stderr=err,
    )
    (line,) = out.getvalue().splitlines()
    document = json.loads(line)
    assert document["exit_code"] == code
    return code, document, out.getvalue() + err.getvalue()


# ---------------------------------------------------------------------------
# Argument contract and result vocabulary.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--help"],
        ["status", "--help"],
        ["stat"],
        ["Status"],
        ["status", "extra"],
        ["status", "--builder-id", str(uuid4())],
        ["mark"],
        ["status", "--at", "2026-11-12"],
    ],
)
async def test_anything_but_a_bare_status_is_usage_before_any_work(
    argv: list[str],
) -> None:
    code, document, _ = await _invoke(argv)
    assert code == ops.EXIT_CODES["unconfigured"] == 30
    assert document["verdict"] == "unconfigured"
    assert document["reasons"] == ["usage"]
    assert document["command"] is None and document["builders"] is None


def test_the_one_command_is_status() -> None:
    assert ops.COMMANDS == ("status",)
    assert ops.parse_arguments(["status"]).command == "status"


def test_the_exit_codes_are_the_published_literals() -> None:
    assert dict(ops.EXIT_CODES) == {
        "ok": 0,
        "refused": 20,
        "switch_off": 21,
        "unconfigured": 30,
        "error": 50,
    }


def test_the_result_vocabulary_is_closed() -> None:
    with pytest.raises(ValueError):
        ops.RunResult(command="status", verdict="maybe")
    with pytest.raises(ValueError):
        ops.RunResult(command="mark", verdict="refused")
    with pytest.raises(ValueError):
        ops.RunResult(command=None, verdict="ok")
    with pytest.raises(ValueError):
        ops.RunResult(command="status", verdict="refused", reasons=("whatever",))
    with pytest.raises(ValueError):
        ops.RunResult(command="status", verdict="ok", builders=[{"state": "free"}])
    document = ops.RunResult(command="status", verdict="ok", builders=[]).document()
    assert set(document) == {
        "schema",
        "command",
        "verdict",
        "reasons",
        "exit_code",
        "switch",
        "database_now",
        "gates",
        "global_enforcement_at",
        "events_window",
        "summary",
        "builders",
    }
    assert document["schema"] == ops.SCHEMA and document["exit_code"] == 0


async def test_with_the_configuration_off_nothing_reaches_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        credit_switch._SWITCH, "_reading", credit_switch._SWITCH._reading
    )
    monkeypatch.setattr(settings, "credit_hot_path_enabled", False)
    code, document, _ = await _invoke(["status"])
    assert code == ops.EXIT_CODES["switch_off"] == 21
    assert document["verdict"] == "switch_off"
    assert document["reasons"] == ["credit_switch_off"]
    assert document["switch"] == {
        "effective": False,
        "reason": "config_disabled",
        "revision": None,
    }
    assert document["builders"] is None and document["summary"] is None


def test_the_trace_kind_is_the_metering_one() -> None:
    assert ops.TRACE_EFFECT_KIND == credit_metering.TRACE_EFFECT_KIND
    assert ops.EVENTS_WITHOUT_KEY_DAYS == 7


def test_the_production_origin_is_the_interaction_one() -> None:
    """The literal the count filters on is the value the writer stores
    (``ingress.origin.value``)."""

    assert ops.PRODUCTION_ORIGIN == InteractionOrigin.PRODUCTION.value


def test_the_count_says_it_is_a_floor_with_one_known_excess() -> None:
    approximation = ops.EVENTS_APPROXIMATION
    assert "floor" in approximation
    assert "dropped" in approximation and "errored" in approximation
    assert "without a provider message id" in approximation


# ---------------------------------------------------------------------------
# Pure formatting.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "printed"),
    [
        (None, None),
        ("zapi", "zapi"),
        ("meta_cloud", "meta_cloud"),
        ("uazapi", "uazapi"),
        ("evolution", "evolution"),
        ("meta", "unrecognized"),
        ("ZAPI", "unrecognized"),
        ("", "unrecognized"),
        (SECRETS[-1], "unrecognized"),
        ('{"zapi_token": "x"}', "unrecognized"),
    ],
)
def test_only_a_known_provider_name_is_printed(
    raw: str | None, printed: str | None
) -> None:
    assert ops.provider_label(raw) == printed


def _row(
    provider: str | None = None,
    active: bool = True,
    ready: tuple[str, ...] = ops.CHANNELS,
    **routes: bool,
) -> ops.TenantChannels:
    """A tenant whose send credentials are complete on ``ready``."""

    return ops.TenantChannels(
        tenant_id=uuid4(),
        is_active=active,
        whatsapp_provider=provider,
        routes=routes,
        send_ready={channel: channel in ready for channel in ops.CHANNELS},
    )


def test_a_tenant_is_on_its_provider_and_on_every_routed_channel() -> None:
    assert _row("uazapi").channels == ("uazapi",)
    assert _row("meta_cloud").channels == ("meta",)
    assert _row("zapi", uazapi=True).channels == ("uazapi", "zapi")
    assert _row(None, evolution=True).channels == ("evolution",)
    assert _row("dialog360").channels == ()
    assert _row(SECRETS[-1]).channels == ()
    assert _row(None).channels == ()


def test_the_clinics_are_counted_per_channel() -> None:
    rows = [
        _row("uazapi"),
        _row(None, uazapi=True),
        _row("evolution"),
        _row("zapi", meta=True),
        _row(None),
        _row(SECRETS[-1], active=False),
    ]
    document = ops.clinics_document(rows)
    by_channel: Any = document["by_channel"]
    assert document["total"] == 6
    assert {name: value["count"] for name, value in by_channel.items()} == {
        "uazapi": 2,
        "evolution": 1,
        "zapi": 1,
        "meta": 1,
        "none": 2,
    }
    assert by_channel["uazapi"]["tenant_ids"] == [
        str(rows[0].tenant_id),
        str(rows[1].tenant_id),
    ]
    assert by_channel["none"]["tenant_ids"] == [
        str(rows[4].tenant_id),
        str(rows[5].tenant_id),
    ]
    tenants: Any = document["tenants"]
    assert tenants[5] == {
        "tenant_id": str(rows[5].tenant_id),
        "active": False,
        "whatsapp_provider": "unrecognized",
        "inbound_routes": {c: False for c in ops.CHANNELS},
        "channels": [],
        "credentials_incomplete": [],
    }
    assert document["credentials_incomplete"] == {
        "count": 0,
        "tenant_ids": [],
        "by_channel": {c: {"count": 0, "tenant_ids": []} for c in ops.CHANNELS},
    }
    assert SECRETS[-1] not in json.dumps(document)


def test_incomplete_credentials_are_listed_only_on_the_tenants_channels() -> None:
    # Incomplete everywhere, but on no channel: nothing to report.
    assert _row(None, ready=()).credentials_incomplete == ()
    assert _row("dialog360", ready=()).credentials_incomplete == ()
    # On Uazapi only: Meta's missing credentials are not its problem.
    assert _row("uazapi", ready=("uazapi",)).credentials_incomplete == ()
    assert _row("uazapi", ready=("meta",)).credentials_incomplete == ("uazapi",)
    # On two channels (its provider and a routed one).
    two = _row("zapi", ready=("zapi",), meta=True)
    assert two.channels == ("zapi", "meta")
    assert two.credentials_incomplete == ("meta",)
    both = _row("evolution", ready=(), uazapi=True)
    assert both.credentials_incomplete == ("uazapi", "evolution")
    # A missing verdict is incomplete (fail closed: nothing says it can send).
    unread = ops.TenantChannels(
        tenant_id=uuid4(),
        is_active=True,
        whatsapp_provider="meta_cloud",
        routes={},
        send_ready={},
    )
    assert unread.credentials_incomplete == ("meta",)


def test_the_incomplete_credentials_are_counted_per_channel() -> None:
    rows = [
        _row("uazapi"),  # complete
        _row("uazapi", ready=()),
        _row("zapi", ready=("zapi",), meta=True),  # incomplete on Meta only
        _row("evolution", ready=(), uazapi=True),  # incomplete on both
        _row(None, ready=()),  # on no channel
    ]
    document = ops.clinics_document(rows)
    incomplete: Any = document["credentials_incomplete"]
    ids = [str(row.tenant_id) for row in rows]
    assert incomplete == {
        "count": 3,
        "tenant_ids": [ids[1], ids[2], ids[3]],
        "by_channel": {
            "uazapi": {"count": 2, "tenant_ids": [ids[1], ids[3]]},
            "evolution": {"count": 1, "tenant_ids": [ids[3]]},
            "zapi": {"count": 0, "tenant_ids": []},
            "meta": {"count": 1, "tenant_ids": [ids[2]]},
        },
    }
    tenants: Any = document["tenants"]
    assert [row["credentials_incomplete"] for row in tenants] == [
        [],
        ["uazapi"],
        ["meta"],
        ["uazapi", "evolution"],
        [],
    ]


def test_the_whitespace_stripped_in_sql_is_pythons_strip() -> None:
    """``TRIM(value, PYTHON_WHITESPACE)`` removes what ``str.strip()``
    removes: exactly the characters for which ``str.isspace()`` holds."""

    every = "".join(
        character
        for character in map(chr, range(sys.maxunicode + 1))
        if character.isspace()
    )
    assert ops.PYTHON_WHITESPACE == every
    assert all((c + "x" + c).strip() == "x" for c in ops.PYTHON_WHITESPACE)


def test_the_tenant_read_returns_only_the_declared_columns() -> None:
    """Pin the select list of the tenant read: only the tenant id, its active
    flag, the provider (printed as a known label only) and booleans computed
    inside PostgreSQL leave the database -- never a credential value, even
    one that is not printed.  The send keys are named in SQL (``READ_KEYS``),
    so the key-name check cannot see a raw value selected beside them; a new
    column must be added here deliberately."""

    marker = " FROM tenants AS t"
    assert ops.TENANT_CHANNELS_SQL.count(marker) == 1  # never vacuous
    select_list = ops.TENANT_CHANNELS_SQL.split(marker)[0]
    declared = [
        "tenant_id",
        "is_active",
        "whatsapp_provider",
        *(f"{c}_route" for c in ops.CHANNELS),
        *(f"{c}_send" for c in ops.CHANNELS),
    ]
    assert re.findall(r"\bAS (\w+)", select_list) == declared
    # Every top-level item is aliased, so an unaliased value cannot hide.
    items, depth, current = [], 0, ""
    for character in select_list.removeprefix("SELECT "):
        depth += {"(": 1, ")": -1}.get(character, 0)
        if character == "," and depth == 0:
            items.append(current.strip())
            current = ""
        else:
            current += character
    items.append(current.strip())
    assert [
        found.group(1) if (found := re.fullmatch(r".+ AS (\w+)", item, re.S)) else item
        for item in items
    ] == declared


def test_the_send_keys_are_the_ones_the_sql_names() -> None:
    named = set(re.findall(r"api_credentials ->> '(\w+)'", ops.TENANT_CHANNELS_SQL))
    assert named == READ_KEYS
    assert set(ops.SEND_CREDENTIALS) == set(ops.CHANNELS)
    for channel in ops.CHANNELS:
        assert f"AS {channel}_send" in ops.TENANT_CHANNELS_SQL
        assert f"AS {channel}_route" in ops.TENANT_CHANNELS_SQL


def _reading(lots: int = 1, spendable: int = 0) -> CourtesyRuleReading:
    return CourtesyRuleReading(
        at=T0,
        courtesy_lots=lots,
        first_grant_at=T0 - timedelta(days=3) if lots else None,
        spendable_mc=spendable,
    )


def test_the_state_is_the_request_paths_verdict() -> None:
    at = T0 - timedelta(days=3)
    reading = _reading()
    cases = [
        (EffectiveEnforcement(at, "global"), "global"),
        (EffectiveEnforcement(at, "mark"), "mark"),
        (EffectiveEnforcement(at, "courtesy", state="charged"), "charged"),
        (EffectiveEnforcement(at, "courtesy", state="blocked"), "blocked"),
        (EffectiveEnforcement(None, None), "protected"),
        (EffectiveEnforcement(None, None, rule_unread=True), "rule_unread"),
    ]
    for effective, state in cases:
        assert ops.builder_state(effective, reading) == state
        assert state in ops.STATES
    no_grant = CourtesyRuleReading(at=T0, courtesy_lots=1, first_grant_at=None)
    assert ops.builder_state(EffectiveEnforcement(None, None), no_grant) == (
        "not_under_rule"
    )
    assert ops.enforcement_document(
        EffectiveEnforcement(at, "courtesy", state="blocked")
    ) == {
        "origin": "courtesy",
        "effective_at": at.isoformat(),
        "rule_state": "blocked",
        "mark_unread": False,
    }


def test_the_paid_plan_is_printed_as_read() -> None:
    plan = PaidPlanInForce(
        at=T0,
        last_binding_cycle_end=date(2026, 11, 10),
        grace_ends_at=T0 + timedelta(days=1),
    )
    assert ops.paid_plan_document(plan) == {
        "in_force": True,
        "paid_lot": False,
        "paid_lot_expires_at": None,
        "last_binding_cycle_end": "2026-11-10",
        "grace_ends_at": (T0 + timedelta(days=1)).isoformat(),
        "in_grace": True,
        "unbound_paid_cycle": False,
    }


def test_the_expiries_are_printed_as_read() -> None:
    paid_until = T0 + timedelta(days=40)
    plan = PaidPlanInForce(at=T0, paid_lot=True)
    assert ops.paid_plan_document(plan, paid_until)["paid_lot_expires_at"] == (
        paid_until.isoformat()
    )
    reading = CourtesyRuleReading(
        at=T0, courtesy_lots=2, first_grant_at=T0, spendable_mc=3_000
    )
    nearest, last = T0 + timedelta(days=3), T0 + timedelta(days=30)
    document = ops.courtesy_document(reading, nearest, last)
    assert document["nearest_expiry_at"] == nearest.isoformat()
    assert document["last_expiry_at"] == last.isoformat()


def _listed(
    state: str, events: int, uazapi: int, incomplete: int = 0, meta: int = 0
) -> dict[str, Any]:
    """A listed builder: ``uazapi`` clinics on Uazapi; ``incomplete`` clinics
    with incomplete send credentials, all of them on Uazapi and ``meta`` of
    them on Meta too."""

    by_channel = {
        channel: {"count": uazapi if channel == "uazapi" else 0, "tenant_ids": []}
        for channel in (*ops.CHANNELS, "none")
    }
    per_channel = {"uazapi": incomplete, "evolution": 0, "zapi": 0, "meta": meta}
    return {
        "state": state,
        "open_debt": {"open_debt_mc": 1_000, "open_debts": 1},
        "events_without_idempotency_key": {"total": events, "by_tenant": []},
        "clinics": {
            "by_channel": by_channel,
            "credentials_incomplete": {
                "count": incomplete,
                "tenant_ids": [],
                "by_channel": {
                    channel: {"count": count, "tenant_ids": []}
                    for channel, count in per_channel.items()
                },
            },
        },
    }


def test_the_summary_totals_the_rule_apart_from_every_state() -> None:
    builders = [
        _listed("charged", 2, 1, incomplete=1, meta=1),
        _listed("blocked", 3, 2, incomplete=2),
        _listed("protected", 100, 10, incomplete=10, meta=3),
        _listed("mark", 1_000, 100, incomplete=100),
        _listed("global", 10_000, 1_000, incomplete=1_000, meta=30),
    ]
    summary: Any = ops.summary_document(builders)
    assert summary["builders"] == 5
    assert summary["open_debt_mc"] == 5_000
    assert summary["under_rule"] == {
        "states": ["charged", "blocked"],
        "builders": 2,
        "events_without_idempotency_key": 5,
        "clinics_per_channel": {
            "uazapi": 3,
            "evolution": 0,
            "zapi": 0,
            "meta": 0,
            "none": 0,
        },
        "clinics_with_incomplete_credentials": 3,
        "incomplete_credentials_per_channel": {
            "uazapi": 3,
            "evolution": 0,
            "zapi": 0,
            "meta": 1,
        },
    }
    assert summary["all_states"]["builders"] == 5
    assert summary["all_states"]["events_without_idempotency_key"] == 11_105
    assert summary["all_states"]["clinics_per_channel"]["uazapi"] == 1_113
    assert summary["all_states"]["clinics_with_incomplete_credentials"] == 1_113
    assert summary["all_states"]["incomplete_credentials_per_channel"] == {
        "uazapi": 1_113,
        "evolution": 0,
        "zapi": 0,
        "meta": 34,
    }


def test_the_first_window_after_the_deploy() -> None:
    now = T0  # civil 2026-11-12: the calendar window [11-01, 12-01)
    current = accounting_window([], now)
    assert (current.start, current.end) == (date(2026, 11, 1), date(2026, 12, 1))
    # The first grant came before the current window: it charges maintenance.
    before = current.starts_at - timedelta(seconds=1)
    assert ops.first_window_after_deploy([], now, before) == (current, "current")
    # Inside it (its start included): a free baseline, the next one charges.
    for inside in (current.starts_at, now - timedelta(days=2)):
        window, which = ops.first_window_after_deploy([], now, inside)
        assert which == "next"
        assert (window.start, window.end) == (date(2026, 12, 1), date(2027, 1, 1))
    # With a monthly cycle the windows are the cycles (owner decision C).
    cycles = [(date(2026, 10, 20), date(2026, 11, 20))]
    window, which = ops.first_window_after_deploy(cycles, now, now)
    assert which == "next"
    assert (window.start, window.end, window.kind) == (
        date(2026, 11, 20),
        date(2026, 12, 20),
        "continuation",
    )


def _snapshot(media_bytes: int, characters: int) -> InventorySnapshot:
    tenant = uuid4()
    return InventorySnapshot.of(
        [
            SourceRow("media", tenant, uuid4(), media_bytes),
            SourceRow("rag", tenant, uuid4(), characters),
        ]
    )


def test_the_reservation_uses_the_inventory_rules() -> None:
    window = AccountingWindow(date(2026, 12, 1), date(2027, 1, 1), "calendar")
    snapshot = _snapshot(30_000_000, 50_000)  # 1.5 and 10 credits
    unopened = ops.reservation_document(
        window=window,
        which="next",
        snapshot=snapshot,
        coverage={},
        opened=frozenset(),
        applies=True,
        courtesy_rule=True,
        spendable_mc=4_000,
    )
    categories: Any = unopened["categories"]
    assert categories["media"] == {
        "units": 30_000_000,
        "target_mc": 1_500,
        "coverage_mc": 0,
        "opened": False,
        "estimated_charge_mc": 1_500,
    }
    assert categories["rag"]["target_mc"] == 10_000
    assert unopened["estimated_charge_mc"] == 11_500
    assert unopened["payable_by_courtesy_mc"] == 4_000  # only what courtesy pays
    assert unopened["window"] == {
        "which": "next",
        "start": "2026-12-01",
        "end": "2027-01-01",
        "kind": "calendar",
    }
    assert unopened["approximation"] == ops.RESERVATION_APPROXIMATION
    # An opened category bills only what its coverage lacks.
    opened = ops.reservation_document(
        window=window,
        which="current",
        snapshot=snapshot,
        coverage={"media": 1_000, "rag": 12_000},
        opened=frozenset({"media", "rag"}),
        applies=True,
        courtesy_rule=False,
        spendable_mc=0,
    )
    assert opened["estimated_charge_mc"] == 500
    assert opened["payable_by_courtesy_mc"] is None  # outside the rule: debt


class _PostgresBind:
    class dialect:  # noqa: N801  (mimics ``Engine.dialect``)
        name = "postgresql"


class _RecordingSession:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def get_bind(self) -> Any:
        return _PostgresBind()

    async def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))


async def test_on_postgresql_the_first_statement_is_read_only() -> None:
    session = _RecordingSession()
    await ops._read_only(session)  # type: ignore[arg-type]
    assert session.statements == [
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    ]


# ---------------------------------------------------------------------------
# The module's boundary (the charge-mark AST fence, still green).
# ---------------------------------------------------------------------------


def test_the_charge_mark_fence_holds_with_this_module_in_src() -> None:
    assert MODULE in _sources()
    assert _fence_findings(_sources(), ROOT) == []
    assert _naming_sites(MODULE, ROOT) == []
    assert not _imports_command(MODULE, ROOT)


def test_the_fence_would_see_this_module_name_the_table(tmp_path: Path) -> None:
    """Not vacuous: the same analysis flags this file once it names it."""

    probe = tmp_path / "src" / "ops" / "credit_courtesy_rule.py"
    probe.parent.mkdir(parents=True)
    probe.write_text(
        MODULE.read_text(encoding="utf-8")
        + "\nQ = 'SELECT kind FROM credit_enforcement_marks'\n",
        encoding="utf-8",
    )
    assert _naming_sites(probe, tmp_path)


def test_the_module_imports_no_http_layer_and_no_other_task() -> None:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    } | {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not any(name.startswith(("fastapi", "starlette")) for name in imported)
    assert not any("route" in name.lower() for name in imported)
    assert "src.services.credit_metering" not in imported
    assert not any(
        name.startswith(("src.ops.credit_charge_mark", "src.ops.credit_enforcement"))
        for name in imported
    )
    assert "credit_charge_mark" not in imported and "credit_enforcement" not in imported


def test_nothing_in_src_imports_this_module() -> None:
    """It runs only as ``python -m``: no route, worker or script reaches it."""

    for path in _sources():
        if path == MODULE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [alias.name for alias in node.names]
            else:
                continue
            assert not any("credit_courtesy_rule" in name for name in names), path


# ---------------------------------------------------------------------------
# The real entry point.
# ---------------------------------------------------------------------------


def _subprocess(tmp_path: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
    """``python -m src.ops.credit_courtesy_rule`` from a directory with no
    ``.env`` (``src`` linked in) and a database URL nothing listens on."""

    workdir = tmp_path / "app"
    workdir.mkdir()
    (workdir / "src").symlink_to(ROOT / "src", target_is_directory=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "ENVIRONMENT": "staging",
        "SECRET_KEY": "s" * 48,
        "DATABASE_URL": "postgresql+asyncpg://nobody@127.0.0.1:9/none",
        "CREDIT_HOT_PATH_ENABLED": "false",
    }
    if "PYTHONPATH" in os.environ:
        env["PYTHONPATH"] = os.environ["PYTHONPATH"]
    return subprocess.run(
        [sys.executable, "-m", "src.ops.credit_courtesy_rule", *argv],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.mark.parametrize(
    ("argv", "verdict"),
    [(["status", "--x"], "unconfigured"), (["status"], "switch_off")],
)
def test_the_entry_point_prints_one_document(
    tmp_path: Path, argv: list[str], verdict: str
) -> None:
    result = _subprocess(tmp_path, argv)
    assert result.returncode == ops.EXIT_CODES[verdict], result.stderr[-2000:]
    (line,) = result.stdout.splitlines()
    document = json.loads(line)
    assert document["schema"] == ops.SCHEMA
    assert document["verdict"] == verdict
    logs = [
        json.loads(raw)
        for raw in result.stderr.splitlines()
        if raw.startswith("{") and json.loads(raw).get("event") == ops.LOG_EVENT
    ]
    assert len(logs) == 1 and logs[0]["verdict"] == verdict


# ---------------------------------------------------------------------------
# ``status`` on SQLite: the real switch reads alembic_version.
# ---------------------------------------------------------------------------


@dataclass
class _World:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]


@pytest.fixture
async def world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_World]:
    _patch_jsonb_for_sqlite()
    monkeypatch.setattr(
        credit_switch._SWITCH, "_reading", credit_switch._SWITCH._reading
    )
    monkeypatch.setattr(settings, "credit_hot_path_enabled", True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rule.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        )
        await connection.execute(
            text("INSERT INTO alembic_version VALUES (:revision)"),
            {"revision": credit_switch.load_image_migration_chain()[-1]},
        )
    sessions = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    try:
        yield _World(engine=engine, sessions=sessions)
    finally:
        await engine.dispose()


async def _builder(world: _World, name: str, courtesy: int | None) -> UUID:
    builder_id = uuid4()
    async with world.sessions() as session, session.begin():
        session.add(Builder(id=builder_id, name=name, is_active=True, status="active"))
    if courtesy is not None:
        async with world.sessions() as session, session.begin():
            await credit_courtesy.grant(
                session,
                builder_id=builder_id,
                credits=courtesy,
                reason="pre-deploy list test",
                actor_user_id=ACTOR,
                idempotency_key=f"rule-ops-{uuid4()}",
            )
    return builder_id


async def _tenant(
    world: _World, builder_id: UUID, credentials: dict[str, Any] | None
) -> UUID:
    tenant_id = uuid4()
    async with world.sessions() as session, session.begin():
        session.add(
            Tenant(
                id=tenant_id,
                name=f"Clinic {tenant_id.hex[:6]}",
                api_key_hash=uuid4().hex,
                builder_id=builder_id,
            )
        )
        await session.flush()
        if credentials is not None:
            session.add(
                ClinicConfigTable(tenant_id=tenant_id, api_credentials=credentials)
            )
    return tenant_id


async def _table_rows(world: _World) -> dict[str, list[tuple[Any, ...]]]:
    async with world.engine.connect() as connection:
        names = [
            row[0]
            for row in await connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            )
        ]
        return {
            name: sorted(
                (
                    tuple(map(repr, row))
                    for row in await connection.execute(text(f'SELECT * FROM "{name}"'))
                ),
            )
            for name in sorted(names)
        }


async def test_status_lists_the_courtesy_builders_and_writes_nothing(
    world: _World,
) -> None:
    charged = await _builder(world, "Charged builder", 3)
    await _builder(world, "No courtesy", None)  # never listed
    await _tenant(
        world,
        charged,
        {
            "whatsapp_provider": "uazapi",
            "uazapi_instance_name": ROUTING_VALUES[0],
            "uazapi_token": SECRETS[0],
            "uazapi_api_url": SECRETS[1],
        },
    )
    await _tenant(
        world,
        charged,
        {
            "evolution_instance_name": ROUTING_VALUES[1],
            "evolution_api_key": SECRETS[2],
        },
    )
    await _tenant(
        world,
        charged,
        {
            "whatsapp_provider": "zapi",
            "zapi_inbound_token_digest": ROUTING_VALUES[2],
            "zapi_token": SECRETS[3],
            "meta_phone_number_id": ROUTING_VALUES[3],
            "meta_permanent_token": SECRETS[4],
            "llm_api_key": SECRETS[5],
        },
    )
    await _tenant(world, charged, {"whatsapp_provider": SECRETS[6]})
    await _tenant(world, charged, None)
    before = await _table_rows(world)

    statements: list[str] = []
    rollbacks: list[str] = []
    commits: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    def rolled_back(_conn: Any) -> None:
        rollbacks.append("rollback")

    def committed(_conn: Any) -> None:
        commits.append("commit")

    sync = world.engine.sync_engine
    event.listen(sync, "before_cursor_execute", record)
    event.listen(sync, "rollback", rolled_back)
    event.listen(sync, "commit", committed)
    try:
        code, document, printed = await _invoke(["status"], world.sessions)
    finally:
        event.remove(sync, "before_cursor_execute", record)
        event.remove(sync, "rollback", rolled_back)
        event.remove(sync, "commit", committed)

    assert code == 0, document
    assert document["verdict"] == "ok" and document["reasons"] == []
    assert document["gates"] == {"courtesy_rule": True, "marks": True}
    (entry,) = document["builders"]
    assert entry["builder_id"] == str(charged)
    assert entry["name"] == "Charged builder" and entry["active"] is True
    assert entry["state"] == "charged"
    assert entry["courtesy"]["lots"] == 1
    assert entry["courtesy"]["spendable_mc"] == 3_000
    assert entry["courtesy"]["nearest_expiry_at"] is not None
    assert entry["courtesy"]["last_expiry_at"] == entry["courtesy"]["nearest_expiry_at"]
    assert entry["paid_plan"]["paid_lot_expires_at"] is None
    assert entry["open_debt"] == {"open_debt_mc": 0, "open_debts": 0}
    assert entry["reservation"]["window"]["which"] == "next"
    assert entry["subscription"] == {
        "active": False,
        "status": None,
        "subscriptions": [],
    }
    assert entry["binding_lag"]["lagging"] is False
    assert set(entry["coverage"]) == {"usable", "reconciliation_failure"}
    assert entry["events_without_idempotency_key"] == {"total": 0, "by_tenant": []}
    counts = {
        name: value["count"] for name, value in entry["clinics"]["by_channel"].items()
    }
    assert counts == {"uazapi": 1, "evolution": 1, "zapi": 1, "meta": 1, "none": 2}
    # Uazapi complete; Evolution without its URL; Z-API without its instance
    # id (Meta complete on the same tenant); the last two on no channel.
    incomplete = {
        tuple(row["channels"]): row["credentials_incomplete"]
        for row in entry["clinics"]["tenants"]
    }
    assert incomplete == {
        ("uazapi",): [],
        ("evolution",): ["evolution"],
        ("zapi", "meta"): ["zapi"],
        (): [],
    }
    for scope in ("under_rule", "all_states"):
        exposure = document["summary"][scope]
        assert exposure["clinics_with_incomplete_credentials"] == 2
        assert exposure["incomplete_credentials_per_channel"] == {
            "uazapi": 0,
            "evolution": 1,
            "zapi": 1,
            "meta": 0,
        }
    assert document["summary"]["builders"] == 1
    assert document["summary"]["states"]["charged"] == 1
    assert document["summary"]["under_rule"]["builders"] == 1
    assert document["events_window"]["approximation"] == ops.EVENTS_APPROXIMATION

    # Read-only: SELECT only, rolled back, never committed, every row kept.
    assert statements
    for statement in statements:
        assert statement.lstrip().upper().startswith("SELECT"), statement[:120]
        assert not re.search(
            r"\b(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE|REPLACE)\b",
            statement,
            re.IGNORECASE,
        ), statement[:120]
    assert rollbacks and not commits
    assert await _table_rows(world) == before

    # No credential or routing value is printed or logged, and the only
    # keys of ``api_credentials`` named are :data:`READ_KEYS`.
    for value in (*SECRETS, *ROUTING_VALUES):
        assert value not in printed
    named = {
        key
        for statement in statements
        for key in re.findall(r"api_credentials ->> '(\w+)'", statement)
    }
    assert named == READ_KEYS
    for statement in statements:
        for found in re.finditer(r"api_credentials", statement):
            assert statement.startswith("api_credentials ->> '", found.start())


async def test_the_reservation_of_a_builder_with_content(world: _World) -> None:
    builder_id = await _builder(world, "With content", 2)
    tenant = await _tenant(world, builder_id, None)
    async with world.sessions() as session, session.begin():
        session.add(
            KnowledgeDocument(
                id=uuid4(),
                tenant_id=tenant,
                title="Doc",
                content="x" * 25_000,
                content_type="text",
                document_metadata={},
                is_indexed=False,
                chunk_count=0,
                size_bytes=25_000,
            )
        )
    code, document, _ = await _invoke(["status"], world.sessions)
    assert code == 0, document
    (entry,) = document["builders"]
    reservation = entry["reservation"]
    assert reservation["applies"] is True
    assert reservation["categories"]["rag"]["units"] == 25_000
    assert reservation["categories"]["rag"]["target_mc"] == 5_000
    assert reservation["categories"]["media"]["target_mc"] == 0
    assert reservation["estimated_charge_mc"] == 5_000
    # Only what the courtesy holds is payable under the rule (2 credits).
    assert reservation["payable_by_courtesy_mc"] == 2_000
    # Granted now: the current window is the free baseline, the next charges.
    assert reservation["window"]["which"] == "next"
    current = accounting_window([], datetime.now(timezone.utc))
    assert reservation["window"]["start"] == current.end.isoformat()


# ---------------------------------------------------------------------------
# Send credentials, pinned against the charged reply sender (owner
# decision 20).  The oracle is ``CreditReplySender.prepare`` itself, run on
# the same stored credentials: a sender change the list does not mirror
# fails here (and in the PostgreSQL suite, which imports these cases).
# ---------------------------------------------------------------------------

# Every key the sender reads per channel, required or not (written by hand,
# not taken from ``ops.SEND_CREDENTIALS``: the sender is the oracle).
SENDER_KEYS: dict[str, tuple[str, ...]] = {
    "uazapi": ("uazapi_api_url", "uazapi_token"),
    "evolution": ("evolution_api_url", "evolution_instance_name", "evolution_api_key"),
    "zapi": ("zapi_instance_id", "zapi_token", "zapi_client_token"),
    "meta": ("meta_phone_number_id", "meta_permanent_token"),
}
SENDER_PROVIDER = {
    "uazapi": "uazapi",
    "evolution": "evolution",
    "zapi": "zapi",
    "meta": "meta_cloud",
}
_ABSENT = object()
# A value replacing one key: absent, JSON null, blank by ``str.strip()``
# (ASCII and Unicode whitespace that PostgreSQL's plain ``TRIM`` keeps) and a
# padded real value.
SEND_BLANKS: tuple[object, ...] = (
    _ABSENT,
    None,
    "",
    " ",
    "\t",
    "\n\r",
    "\u00a0",
    "\u2028",
    "\u3000",
    " \t\u205f ",
    "  kept\u3000",
)


def send_cases() -> list[tuple[str, str, dict[str, Any]]]:
    """``(channel, label, credentials)``: per channel the complete set
    (every key the sender reads, planted values that must never be
    printed) and, for each key, the set with that key replaced by each of
    :data:`SEND_BLANKS`."""

    def planted(channel: str, keys: tuple[str, ...], case: int) -> dict[str, Any]:
        # Unique per case: PostgreSQL keeps some keys unique across tenants.
        return {
            "whatsapp_provider": SENDER_PROVIDER[channel],
            **{key: f"https://{key}-{case}.NEVER-PRINT.example/t" for key in keys},
        }

    cases: list[tuple[str, str, dict[str, Any]]] = []
    for channel, keys in SENDER_KEYS.items():
        cases.append((channel, "complete", planted(channel, keys, len(cases))))
        for key in keys:
            for blank in SEND_BLANKS:
                credentials = planted(channel, keys, len(cases))
                if blank is _ABSENT:
                    del credentials[key]
                    label = f"{key} absent"
                else:
                    credentials[key] = blank
                    label = f"{key}={blank!r}"
                cases.append((channel, label, credentials))
    return cases


async def sender_ready(session: AsyncSession, tenant_id: UUID, channel: str) -> bool:
    """Whether ``CreditReplySender.prepare`` would send a charged reply of
    ``channel`` for the tenant (``False``: it refused ``no_credentials``)."""

    payload = {
        "kind": REPLY_PAYLOAD_KIND,
        "channel": channel,
        "tenant_id": str(tenant_id),
        "session_id": str(uuid4()),
        "phone": "5500000000000",
        "text": "oi",
    }
    try:
        await CreditReplySender().prepare(payload, session)
    except CreditReplyUndeliverable as refused:
        assert refused.reason == "no_credentials"
        return False
    return True


# What the cases must show, so the pin is not vacuous: Evolution keeps
# main's unstripped check, the others strip Unicode whitespace, the Z-API
# client token is optional and a padded value is a value.
SEND_SPOT_CHECKS: dict[tuple[str, str], bool] = {
    ("uazapi", "complete"): True,
    ("uazapi", "uazapi_token='\\u3000'"): False,
    ("uazapi", "uazapi_api_url='\\xa0'"): False,
    ("evolution", "evolution_api_key=' '"): True,
    ("evolution", "evolution_api_url=''"): False,
    ("evolution", "evolution_instance_name absent"): False,
    ("zapi", "zapi_client_token absent"): True,
    ("zapi", "zapi_instance_id='\\t'"): False,
    ("meta", "meta_permanent_token='  kept\\u3000'"): True,
    ("meta", "meta_phone_number_id=None"): False,
}


def compare_send_verdicts(
    cases: list[tuple[UUID, str, str]],
    listed: dict[UUID, ops.TenantChannels],
    sender: dict[UUID, bool],
) -> dict[tuple[str, str], bool]:
    """The sender's verdict per case, after asserting the list agrees on
    every one (each case tenant is on exactly its channel)."""

    mismatches = []
    verdicts: dict[tuple[str, str], bool] = {}
    for tenant_id, channel, label in cases:
        row = listed[tenant_id]
        assert row.channels == (channel,), label
        ready = sender[tenant_id]
        verdicts[(channel, label)] = ready
        if (channel not in row.credentials_incomplete) != ready:
            mismatches.append((channel, label, ready))
    assert mismatches == []
    for case, expected in SEND_SPOT_CHECKS.items():
        assert verdicts[case] is expected, case
    assert sum(verdicts.values()) and not all(verdicts.values())
    return verdicts


async def test_the_list_judges_send_credentials_as_the_charged_sender(
    world: _World,
) -> None:
    builder_id = await _builder(world, "Pinned", 1)
    cases = [
        (await _tenant(world, builder_id, credentials), channel, label)
        for channel, label, credentials in send_cases()
    ]
    bare = await _tenant(world, builder_id, None)  # no config row
    async with world.sessions() as session:
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
        await session.rollback()
    assert listed[bare].send_ready == {c: False for c in ops.CHANNELS}
    verdicts = compare_send_verdicts(cases, listed, sender)

    # The same verdicts through the entry point, and no value printed.
    code, document, printed = await _invoke(["status"], world.sessions)
    assert code == 0, document
    (entry,) = document["builders"]
    tenants = {row["tenant_id"]: row for row in entry["clinics"]["tenants"]}
    for tenant_id, channel, label in cases:
        expected = [] if verdicts[(channel, label)] else [channel]
        assert tenants[str(tenant_id)]["credentials_incomplete"] == expected, label
    incomplete = sum(1 for ready in verdicts.values() if not ready)
    assert entry["clinics"]["credentials_incomplete"]["count"] == incomplete
    for scope in ("under_rule", "all_states"):
        assert document["summary"][scope]["clinics_with_incomplete_credentials"] == (
            incomplete
        )
        assert document["summary"][scope]["incomplete_credentials_per_channel"] == {
            channel: sum(
                1
                for (case_channel, _), ready in verdicts.items()
                if case_channel == channel and not ready
            )
            for channel in ops.CHANNELS
        }
    assert "NEVER-PRINT" not in printed


async def test_with_the_courtesy_gate_closed_no_lot_is_read(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _builder(world, "Granted", 1)
    monkeypatch.setattr(ops, "courtesy_rule_gate_open", lambda _reading: False)
    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    event.listen(world.engine.sync_engine, "before_cursor_execute", record)
    try:
        code, document, _ = await _invoke(["status"], world.sessions)
    finally:
        event.remove(world.engine.sync_engine, "before_cursor_execute", record)
    assert code == ops.EXIT_CODES["refused"] == 20, document
    assert document["reasons"] == ["courtesy_rule_gate_closed"]
    assert document["builders"] is None
    assert statements  # the switch was read
    assert not any("credit_lots" in statement for statement in statements)
