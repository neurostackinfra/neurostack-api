"""Credit metric families reach ``/metrics`` and move only with the switch on.

neurostack-credit-system Group 7, deliverable 4: every ``neurostack_credit_*``
family is registered in the default registry that ``make_asgi_app()`` serves,
declared in the closed contract, identity-free and emitted from an existing
event point only while the switch is effective.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
import yaml
from prometheus_client import REGISTRY, generate_latest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.observability.metric_dimensions import (
    _FORBIDDEN_LABEL_NAMES,
    METRIC_DIMENSION_CONTRACTS,
)
from src.services import content_retention, credit_fact_consumer
from src.services.credit_fact_consumer import (
    CreditFactConsumer,
    CreditSwitchOff,
    FactHandling,
)
from src.services.credit_ledger import LedgerWrites
from src.services.credit_lifecycle import CreditLifecycle
from src.services.credit_rollout import (
    CreditRolloutService,
    record_observational_write_drop,
)
from src.services.credit_switch import CreditSwitch

ROOT = Path(__file__).resolve().parents[2]
_EMITTING_MODULES = (
    "src.services.credit_switch",
    "src.services.credit_rollout",
    "src.services.credit_fact_consumer",
    "src.services.credit_lifecycle",
    "src.services.content_retention",
)


@pytest.fixture
def switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CreditSwitch, "effective", lambda _self: True)


@pytest.fixture
def switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CreditSwitch, "effective", lambda _self: False)


def _value(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _result(rows: list[Any]) -> Mock:
    result = Mock()
    result.all.return_value = rows
    return result


def _orphan_session() -> Mock:
    """One enforced claim with no effect: exactly one open orphan."""

    claim = (uuid4(), uuid4(), uuid4(), 1)
    session = Mock()
    session.execute = AsyncMock(
        side_effect=[_result([claim])] + [_result([]) for _ in range(6)]
    )
    session.scalar = AsyncMock(return_value=0)
    return session


# ---------------------------------------------------------------------------
# O1: the Group 7 families are in the served registry.
# ---------------------------------------------------------------------------


async def test_group7_families_are_exported_by_the_served_registry(
    switch_on: None,
) -> None:
    before = _value(
        "neurostack_credit_observational_write_dropped_total", reason="timeout"
    )

    record_observational_write_drop("timeout")
    report = await CreditRolloutService(_orphan_session()).orphan_report()

    exposition = generate_latest(REGISTRY).decode()
    assert report.open_count == 1
    assert re.search(
        r'^neurostack_credit_observational_write_dropped_total\{reason="timeout"\} '
        + re.escape(repr(before + 1.0))
        + "$",
        exposition,
        flags=re.MULTILINE,
    )
    assert re.search(
        r"^neurostack_credit_orphan_effects 1\.0$", exposition, flags=re.MULTILINE
    )
    assert re.search(
        r"^neurostack_credit_reconciliation_ready ", exposition, flags=re.MULTILINE
    )


def test_a_drop_with_the_switch_off_moves_nothing(switch_off: None) -> None:
    before = _value(
        "neurostack_credit_observational_write_dropped_total", reason="failed"
    )
    record_observational_write_drop("failed")
    assert (
        _value("neurostack_credit_observational_write_dropped_total", reason="failed")
        == before
    )


# ---------------------------------------------------------------------------
# O2: inbox and rejected acks (fact consumer).
# ---------------------------------------------------------------------------


def _consumer(
    monkeypatch: pytest.MonkeyPatch, handling: FactHandling
) -> CreditFactConsumer:
    monkeypatch.setattr(credit_fact_consumer, "require_presentation_allowed", Mock())
    monkeypatch.setattr(credit_fact_consumer, "consumer_contract_digests", Mock())
    consumer = CreditFactConsumer(session_factory=Mock())
    monkeypatch.setattr(consumer, "_in_own_session", AsyncMock(return_value=handling))
    return consumer


async def test_a_rejected_fact_counts_inbox_and_rejected_ack(
    monkeypatch: pytest.MonkeyPatch, switch_on: None
) -> None:
    inbox = _value("neurostack_credit_fact_inbox_total", outcome="rejected")
    rejected = _value(
        "neurostack_credit_fact_rejected_total", reason="catalog_mismatch"
    )
    handling = FactHandling("rejected", uuid4(), reason="catalog_mismatch")

    assert await _consumer(monkeypatch, handling).handle({}) is handling

    assert _value("neurostack_credit_fact_inbox_total", outcome="rejected") == (
        inbox + 1
    )
    assert _value(
        "neurostack_credit_fact_rejected_total", reason="catalog_mismatch"
    ) == (rejected + 1)


async def test_an_accepted_fact_counts_only_the_inbox(
    monkeypatch: pytest.MonkeyPatch, switch_on: None
) -> None:
    accepted = _value("neurostack_credit_fact_inbox_total", outcome="accepted")
    rejected = sum(
        _value("neurostack_credit_fact_rejected_total", reason=reason)
        for reason in METRIC_DIMENSION_CONTRACTS["neurostack_credit_fact_rejected"]
        .policies["reason"]
        .vocabulary
        or ()
    )
    handling = FactHandling("accepted", uuid4())

    await _consumer(monkeypatch, handling).handle({})

    assert _value("neurostack_credit_fact_inbox_total", outcome="accepted") == (
        accepted + 1
    )
    assert (
        sum(
            _value("neurostack_credit_fact_rejected_total", reason=reason)
            for reason in METRIC_DIMENSION_CONTRACTS["neurostack_credit_fact_rejected"]
            .policies["reason"]
            .vocabulary
            or ()
        )
        == rejected
    )


async def test_the_consumer_counts_nothing_with_the_switch_off(
    monkeypatch: pytest.MonkeyPatch, switch_off: None
) -> None:
    before = _value("neurostack_credit_fact_inbox_total", outcome="rejected")
    consumer = _consumer(
        monkeypatch, FactHandling("rejected", uuid4(), reason="fact_violation")
    )
    with pytest.raises(CreditSwitchOff, match="not effective"):
        await consumer.handle({})
    assert _value("neurostack_credit_fact_inbox_total", outcome="rejected") == before


# ---------------------------------------------------------------------------
# O2: suspension, balance thresholds and debt (lifecycle evaluation).
# ---------------------------------------------------------------------------


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A real SQLAlchemy session with an open DBAPI transaction, so its
    commit and rollback fire the session events the lifecycle relies on."""

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await session.execute(text("SELECT 1"))
            yield session
    finally:
        await engine.dispose()


def _suspending_lifecycle(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession
) -> CreditLifecycle:
    at = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    episode = SimpleNamespace(
        id=uuid4(),
        builder_id=uuid4(),
        exhausted_at=at - timedelta(hours=80),
        suspension_due_at=at - timedelta(hours=8),
        deletion_due_at=at + timedelta(days=20),
    )
    lifecycle = CreditLifecycle(session, grace_days=0)
    repository = Mock()
    repository.open_episode = AsyncMock(return_value=episode)
    repository.closed_episode_count = AsyncMock(return_value=0)
    repository.transitions_of = AsyncMock(return_value={})
    repository.insert_transition = AsyncMock(return_value=True)
    lifecycle.repository = repository
    monkeypatch.setattr(lifecycle, "_observe_grace", AsyncMock())

    async def thresholds(*args: Any) -> None:
        args[-1].thresholds.append(100)

    monkeypatch.setattr(lifecycle, "_thresholds", thresholds)
    return lifecycle


def _book() -> SimpleNamespace:
    return SimpleNamespace(
        clock=datetime(2026, 11, 1, 12, tzinfo=timezone.utc),
        builder_id=uuid4(),
        open_debt_mc=400,
        available_trace_mc=lambda _at: 0,
    )


def _lifecycle_samples() -> tuple[float, float, float, float]:
    return (
        _value("neurostack_credit_lifecycle_transitions_total", transition="suspended"),
        _value("neurostack_credit_balance_thresholds_total", threshold="100"),
        _value("neurostack_credit_debt_recorded_millicredits_total"),
        _value("neurostack_credit_debt_settled_millicredits_total"),
    )


async def _evaluate(monkeypatch: pytest.MonkeyPatch, session: AsyncSession) -> Any:
    return await _suspending_lifecycle(monkeypatch, session).evaluate(
        _book(),  # type: ignore[arg-type]
        source="worker",
        writes=LedgerWrites(
            debts=[{"amount_mc": 400}], settlements=[{"amount_mc": 250}]
        ),
        enforcement_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )


async def test_a_committed_suspension_counts_once_after_the_commit(
    monkeypatch: pytest.MonkeyPatch, switch_on: None, session: AsyncSession
) -> None:
    before = _lifecycle_samples()

    outcome = await _evaluate(monkeypatch, session)
    assert outcome.transitions == ["suspended"]
    assert _lifecycle_samples() == before  # nothing before the commit

    await session.commit()
    after = (before[0] + 1, before[1] + 1, before[2] + 400, before[3] + 250)
    assert _lifecycle_samples() == after

    await session.execute(text("SELECT 1"))
    await session.commit()  # a later, empty transaction re-emits nothing
    assert _lifecycle_samples() == after


async def test_a_rolled_back_evaluation_counts_nothing(
    monkeypatch: pytest.MonkeyPatch, switch_on: None, session: AsyncSession
) -> None:
    """The 402 of an exhausted builder's upload: evaluated, then rolled back."""

    before = _lifecycle_samples()

    await _evaluate(monkeypatch, session)
    await session.rollback()
    assert _lifecycle_samples() == before

    await session.execute(text("SELECT 1"))
    await session.commit()  # the discarded increments never come back
    assert _lifecycle_samples() == before


async def test_two_evaluations_in_one_transaction_count_each_once(
    monkeypatch: pytest.MonkeyPatch, switch_on: None, session: AsyncSession
) -> None:
    before = _lifecycle_samples()

    await _evaluate(monkeypatch, session)
    await _evaluate(monkeypatch, session)
    assert _lifecycle_samples() == before
    await session.commit()

    assert _lifecycle_samples() == (
        before[0] + 2,
        before[1] + 2,
        before[2] + 800,
        before[3] + 500,
    )


async def test_the_lifecycle_counts_nothing_with_the_switch_off(
    monkeypatch: pytest.MonkeyPatch, switch_off: None, session: AsyncSession
) -> None:
    before = _lifecycle_samples()
    await _evaluate(monkeypatch, session)
    await session.commit()
    assert _lifecycle_samples() == before


# ---------------------------------------------------------------------------
# O2: hold and purge (content retention).
# ---------------------------------------------------------------------------


def _retention_sample(operation: str, outcome: str) -> float:
    return _value(
        "neurostack_credit_retention_operations_total",
        operation=operation,
        outcome=outcome,
    )


async def _hold(monkeypatch: pytest.MonkeyPatch, steps: AsyncMock) -> Any:
    monkeypatch.setattr(content_retention, "_serializable", AsyncMock())
    monkeypatch.setattr(content_retention, "_hold_event_steps", steps)
    session = Mock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return await content_retention.record_hold_event(
        session,
        content_retention.Actor(uuid4(), None, True),
        event_type="hold_requested",
        builder_id=uuid4(),
        scope="builder",
        presented=content_retention.PresentedAuthorization(uuid4(), "sig"),
        reason_code="dispute",
    )


async def test_hold_events_and_purge_refusals_are_counted(
    monkeypatch: pytest.MonkeyPatch, switch_on: None
) -> None:
    recorded = _retention_sample("hold_requested", "recorded")
    refused = _retention_sample("hold_requested", "refused")
    disabled = _retention_sample("purge", "disabled")

    await _hold(monkeypatch, AsyncMock(return_value=Mock()))
    with pytest.raises(content_retention.RetentionRefused, match="^scope_closed$"):
        await _hold(
            monkeypatch,
            AsyncMock(
                side_effect=content_retention.RetentionRefused("scope_closed", 409)
            ),
        )
    outcome = await content_retention.request_purge()

    assert outcome.code == content_retention.PURGE_DISABLED
    assert _retention_sample("hold_requested", "recorded") == recorded + 1
    assert _retention_sample("hold_requested", "refused") == refused + 1
    assert _retention_sample("purge", "disabled") == disabled + 1


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("reason_required", 422),
        ("assignment_required", 403),
        ("authorization_invalid", 403),
        ("scope_not_found", 404),
    ],
)
async def test_bad_or_unauthorized_hold_requests_are_not_policy_refusals(
    monkeypatch: pytest.MonkeyPatch, switch_on: None, code: str, status: int
) -> None:
    refused = _retention_sample("hold_requested", "refused")
    with pytest.raises(content_retention.RetentionRefused, match=f"^{code}$"):
        await _hold(
            monkeypatch,
            AsyncMock(side_effect=content_retention.RetentionRefused(code, status)),
        )
    assert _retention_sample("hold_requested", "refused") == refused


async def test_a_retryable_hold_conflict_is_not_a_policy_refusal(
    monkeypatch: pytest.MonkeyPatch, switch_on: None
) -> None:
    refused = _retention_sample("hold_requested", "refused")
    monkeypatch.setattr(content_retention, "_serialization_failure", lambda _e: True)
    error = DBAPIError("COMMIT", {}, Exception("serialization failure"))
    with pytest.raises(content_retention.RetentionRefused, match="^hold_conflict$"):
        await _hold(monkeypatch, AsyncMock(side_effect=error))
    assert _retention_sample("hold_requested", "refused") == refused


async def test_retention_counts_nothing_with_the_switch_off(
    monkeypatch: pytest.MonkeyPatch, switch_off: None
) -> None:
    disabled = _retention_sample("purge", "disabled")
    recorded = _retention_sample("hold_requested", "recorded")
    await _hold(monkeypatch, AsyncMock(return_value=Mock()))
    await content_retention.request_purge()
    assert _retention_sample("purge", "disabled") == disabled
    assert _retention_sample("hold_requested", "recorded") == recorded


# ---------------------------------------------------------------------------
# The served registry and the rules.
# ---------------------------------------------------------------------------


def _family(sample_or_series: str) -> str:
    for suffix in ("_total", "_created"):
        if sample_or_series.endswith(suffix):
            return sample_or_series[: -len(suffix)]
    return sample_or_series


def _emittable_credit_families() -> set[str]:
    import importlib

    families: set[str] = set()
    for module_name in _EMITTING_MODULES:
        for candidate in vars(importlib.import_module(module_name)).values():
            name = getattr(candidate, "_name", "")
            if isinstance(name, str) and name.startswith("neurostack_credit_"):
                families.add(name)
    return families


def test_every_credit_family_is_served_declared_and_identity_free() -> None:
    declared = {
        name
        for name in METRIC_DIMENSION_CONTRACTS
        if name.startswith("neurostack_credit_")
    }
    served = {
        family.name
        for family in REGISTRY.collect()
        if family.name.startswith("neurostack_credit_")
    }
    assert _emittable_credit_families() == declared
    assert served == declared
    for name in declared:
        labels = set(METRIC_DIMENSION_CONTRACTS[name].labels)
        assert not labels & _FORBIDDEN_LABEL_NAMES, name
        assert not labels & {"tenant", "tenant_id"}, name


_SELECTOR = re.compile(r"\b(neurostack_[a-z0-9_]+)\{([^}]*)\}")
_MATCHER = re.compile(r'\s*(\w+)\s*(=~|!~|!=|=)\s*"([^"]*)"\s*(?:,|$)')


def _check_matchers(expression: str) -> None:
    """Every label matcher names a declared label and only values in its
    closed vocabulary: ``=``/``!=`` a literal, ``=~``/``!~`` a ``|``
    alternation of literals (no wildcard can reach an undeclared value)."""

    for series, body in _SELECTOR.findall(expression):
        policies = METRIC_DIMENSION_CONTRACTS[_family(series)].policies
        consumed = 0
        for match in _MATCHER.finditer(body):
            assert match.start() == consumed, (series, body)
            consumed = match.end()
            label, operator, value = match.groups()
            assert label in policies, (series, label)
            values = value.split("|") if operator in ("=~", "!~") else [value]
            for literal in values:
                assert policies[label].accepts(literal), (series, label, literal)
        assert consumed == len(body), (series, body)


@pytest.mark.parametrize(
    "expression",
    [
        'neurostack_credit_retention_operations_total{outcome!="nope"}',
        'neurostack_credit_retention_operations_total{outcome=~"refused|.*"}',
        'neurostack_credit_retention_operations_total{outcome!~"bogus"}',
        'neurostack_credit_retention_operations_total{tenant_id="x"}',
        "neurostack_credit_retention_operations_total{outcome=refused}",
    ],
)
def test_the_matcher_check_rejects_out_of_vocabulary_matchers(
    expression: str,
) -> None:
    with pytest.raises(AssertionError):
        _check_matchers(expression)


def test_the_matcher_check_accepts_closed_matchers() -> None:
    _check_matchers(
        'neurostack_credit_retention_operations_total{outcome!="disabled",'
        ' operation=~"hold_requested|hold_activated"}'
    )


def test_every_rule_references_a_declared_family_this_code_emits() -> None:
    rules = yaml.safe_load((ROOT / "config/prometheus_rules_credit.yml").read_text())
    emittable = _emittable_credit_families()
    referenced: set[str] = set()
    for group in rules["groups"]:
        for rule in group["rules"]:
            names = set(re.findall(r"\bneurostack_[a-z0-9_]+", rule["expr"]))
            assert names, rule["alert"]
            referenced |= {_family(name) for name in names}
            _check_matchers(rule["expr"])
            assert not re.search(r"\b(?:by|without)\s*\(", rule["expr"]), rule["alert"]
    assert referenced <= set(METRIC_DIMENSION_CONTRACTS)
    assert referenced <= emittable
    # Every condition deliverable 4 names has a rule.
    assert {
        "neurostack_credit_fact_rejected",
        "neurostack_credit_orphan_effects",
        "neurostack_credit_reconciliation_ready",
        "neurostack_credit_lifecycle_transitions",
        "neurostack_credit_retention_operations",
        "neurostack_credit_observational_write_dropped",
    } <= referenced


# ---------------------------------------------------------------------------
# Every closed child exists at zero right after import, before any emit.
# ---------------------------------------------------------------------------

_PRECREATED_PROBE = """
import itertools, json
from prometheus_client import REGISTRY
import src.services.content_retention  # noqa: F401
import src.services.credit_fact_consumer  # noqa: F401
import src.services.credit_lifecycle  # noqa: F401
import src.services.credit_rollout  # noqa: F401
from src.observability.metric_dimensions import METRIC_DIMENSION_CONTRACTS

counters = {
    f.name for f in REGISTRY.collect()
    if f.name.startswith("neurostack_credit_") and f.type == "counter"
}
samples = {}
for name in sorted(counters):
    contract = METRIC_DIMENSION_CONTRACTS[name]
    vocabularies = [sorted(contract.policies[l].vocabulary) for l in contract.labels]
    for values in itertools.product(*vocabularies):
        labels = dict(zip(contract.labels, values))
        key = name + json.dumps(labels, sort_keys=True)
        samples[key] = REGISTRY.get_sample_value(name + "_total", labels)
print(json.dumps({"counters": sorted(counters), "samples": samples}))
"""


def test_every_closed_credit_counter_child_is_zero_right_after_import() -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONSTARTUP"}
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", _PRECREATED_PROBE],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    exported = json.loads(result.stdout.strip().splitlines()[-1])

    assert exported["counters"] == [
        "neurostack_credit_balance_thresholds",
        "neurostack_credit_debt_recorded_millicredits",
        "neurostack_credit_debt_settled_millicredits",
        "neurostack_credit_fact_inbox",
        "neurostack_credit_fact_rejected",
        "neurostack_credit_lifecycle_transitions",
        "neurostack_credit_observational_write_dropped",
        "neurostack_credit_retention_operations",
    ]
    # 3 + 1 + 1 + 4 + 6 + 5 + 5 + 4x3 closed children.
    assert len(exported["samples"]) == 37
    missing = [key for key, value in exported["samples"].items() if value is None]
    assert not missing
    assert set(exported["samples"].values()) == {0.0}
