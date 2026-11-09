"""Group 7 boundary tests for code-only credit rollout readiness."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from src.services.credit_rollout import (
    OBSERVATIONAL_DROP_REASONS,
    BindingCoverage,
    CapabilityReadiness,
    CreditEnforcementRejected,
    CreditRolloutService,
    cutoff_digest,
)


def test_current_civil_date_uses_sao_paulo_not_utc_midnight() -> None:
    # 00:30 UTC remains the previous civil day in Sao Paulo.
    assert (
        CreditRolloutService.current_civil_date(
            datetime(2026, 1, 2, 0, 30, tzinfo=timezone.utc)
        ).isoformat()
        == "2026-01-01"
    )


def test_cutoff_digest_is_stable_and_order_sensitive_to_report_content() -> None:
    rows = [{"tenant": "a", "reason": "standalone"}]
    assert cutoff_digest(rows) == cutoff_digest([dict(rows[0])])
    assert cutoff_digest(rows) != cutoff_digest([{**rows[0], "reason": "unknown"}])


def test_observational_drop_vocabulary_is_closed() -> None:
    assert OBSERVATIONAL_DROP_REASONS == {
        "route_busy",
        "pool_exhausted",
        "timeout",
        "failed",
        "tenant_gone",
    }


def test_cutoff_coverage_is_narrower_than_the_debt_path() -> None:
    """A normal empty builder is served as debt but blocks rollout cutoff."""

    empty = BindingCoverage()
    assert empty.usable
    assert not empty.covered
    assert BindingCoverage(monthly_binding=True).covered
    assert BindingCoverage(usable_topup=True).covered
    assert not BindingCoverage(usable_topup=True, quarantined=True).covered


def test_current_binding_requires_the_exact_cycle_revision() -> None:
    builder_id = uuid4()
    subscription_id = uuid4()
    cycle_id = uuid4()
    cycle = SimpleNamespace(
        id=cycle_id,
        builder_id=builder_id,
        subscription_id=subscription_id,
        cycle_start=datetime(2026, 10, 1).date(),
        cycle_end=datetime(2026, 11, 1).date(),
        financial_revision=3,
    )
    binding = SimpleNamespace(
        status="active",
        builder_id=builder_id,
        subscription_id=subscription_id,
        subscription_cycle_id=cycle_id,
        cycle_start=cycle.cycle_start,
        cycle_end=cycle.cycle_end,
        financial_revision=4,
    )

    assert not CreditRolloutService._binding_matches_cycle(binding, cycle)


def test_identity_is_split_from_revision_and_status() -> None:
    """Identity ignores revision and status (request coverage: lag serves);
    the strict predicate of readiness/arming requires both."""

    builder_id, subscription_id, cycle_id = uuid4(), uuid4(), uuid4()
    start, end = datetime(2026, 10, 1).date(), datetime(2026, 11, 1).date()
    cycle = SimpleNamespace(
        id=cycle_id,
        builder_id=builder_id,
        subscription_id=subscription_id,
        cycle_start=start,
        cycle_end=end,
        financial_revision=3,
    )

    def binding(**changes: object) -> SimpleNamespace:
        values: dict[str, object] = {
            "status": "active",
            "builder_id": builder_id,
            "subscription_id": subscription_id,
            "subscription_cycle_id": cycle_id,
            "cycle_start": start,
            "cycle_end": end,
            "financial_revision": 3,
        }
        values.update(changes)
        return SimpleNamespace(**values)

    identity = CreditRolloutService._binding_identity_matches
    strict = CreditRolloutService._binding_matches_cycle
    assert identity(binding(), cycle) and strict(binding(), cycle)
    for lagging in (binding(financial_revision=2), binding(status="reversed")):
        assert identity(lagging, cycle) and not strict(lagging, cycle)
    for drift in (
        binding(builder_id=uuid4()),
        binding(subscription_id=uuid4()),
        binding(subscription_cycle_id=uuid4()),
        binding(cycle_start=datetime(2026, 9, 30).date()),
        binding(cycle_end=datetime(2026, 11, 2).date()),
    ):
        assert not identity(drift, cycle) and not strict(drift, cycle)
    assert not identity(None, cycle) and not strict(None, cycle)


@pytest.mark.asyncio
async def test_record_capability_readiness_keeps_all_receipt_fields_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Mock()
    session.add = Mock()
    session.flush = AsyncMock()
    service = CreditRolloutService(session)
    readiness = CapabilityReadiness(
        environment="test",
        billing_fact_schema_digest="a" * 64,
        inbox_high_watermark=4,
        reconciliation_complete=True,
        readback_complete=True,
        has_open_gap=False,
        has_backlog=False,
        has_alert=False,
    )
    monkeypatch.setattr(service, "capability_ready", AsyncMock(return_value=readiness))

    row = await service.record_capability_readiness()

    assert row.operation_id is None
    assert row.deployment_digest is None
    assert row.credit_deployment_digest is None
    assert row.producer_compatibility_digest is None
    assert row.consumer_compatibility_digest is None
    session.add.assert_called_once_with(row)
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_omitting_the_confirmed_digest_is_a_type_error() -> None:
    service = CreditRolloutService(Mock(spec=[]))
    with pytest.raises(TypeError, match="expected_report_digest"):
        await service.arm_enforcement(set_by="task")  # type: ignore[call-arg]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "digest",
    [None, "", "a" * 63, "a" * 65, "A" * 64, "g" * 64, " " + "a" * 63, 7],
    ids=["none", "empty", "short", "long", "uppercase", "non-hex", "space", "int"],
)
async def test_a_malformed_confirmed_digest_is_refused_before_any_database_work(
    digest: object,
) -> None:
    """``spec=[]``: any session attribute (a query, the lock) would raise
    ``AttributeError`` instead of the refusal asserted here."""

    service = CreditRolloutService(Mock(spec=[]))
    with pytest.raises(
        CreditEnforcementRejected,
        match="^expected_report_digest is not a lowercase SHA-256$",
    ):
        await service.arm_enforcement(
            set_by="task", expected_report_digest=digest  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("digest", "reason"), [("A" * 64, "standalone"), ("a" * 64, "  ")]
)
async def test_a_malformed_acceptance_is_refused_before_the_author_is_read(
    digest: str, reason: str
) -> None:
    service = CreditRolloutService(Mock(spec=[]))
    with pytest.raises(ValueError):
        await service.accept_cutoff(
            tenant_id=uuid4(),
            builder_id=None,
            reason=reason,
            report_digest=digest,
            author_user_id=uuid4(),
        )


async def test_the_orphan_worker_keeps_one_sweep_state_across_ticks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full-sweep cadence lives in ONE state for the worker's lifetime: a
    state recreated per tick would sweep the full history every tick."""

    import asyncio

    from src.services import credit_rollout

    seen: list[object] = []
    done = asyncio.Event()

    async def once(_factory: object, *, state: object = None) -> int:
        seen.append(state)
        if len(seen) == 3:
            done.set()
        return 0

    monkeypatch.setattr(credit_rollout, "run_credit_orphan_reconciliation_once", once)
    task = asyncio.create_task(
        credit_rollout.run_credit_orphan_reconciliation_worker(0)
    )
    await asyncio.wait_for(done.wait(), timeout=5)
    await credit_rollout.stop_credit_orphan_reconciliation_worker(task)
    assert len(seen) >= 3
    assert isinstance(seen[0], credit_rollout.OrphanSweepState)
    assert all(state is seen[0] for state in seen)


# ---------------------------------------------------------------------------
# uazapi-webhook-auth: the L10 guard.  ``arm_enforcement`` refuses
# unconditionally until ``uazapi-credit-enforcement`` removes the guard.
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parents[2]
_GUARD = "uazapi_enforced_credit_adapter_available"
_GUARD_MESSAGE = "uazapi has no enforced credit adapter"


def _armable(monkeypatch: pytest.MonkeyPatch) -> tuple[CreditRolloutService, Mock]:
    """A service whose lock and clock pass; every later step is a spy."""

    from src.repositories.credit import CreditRepository

    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(CreditRepository, "lock_rollout_control", AsyncMock())
    monkeypatch.setattr(CreditRepository, "database_now", AsyncMock(return_value=now))
    session = Mock()
    session.get = AsyncMock()
    session.flush = AsyncMock()
    service = CreditRolloutService(session)
    later = Mock(
        reconcile_orphans=AsyncMock(),
        capability_ready=AsyncMock(),
        cutoff_report=AsyncMock(),
    )
    for name in ("reconcile_orphans", "capability_ready", "cutoff_report"):
        monkeypatch.setattr(service, name, getattr(later, name))
    return service, later


@pytest.mark.asyncio
async def test_arming_is_refused_without_the_swap_before_anything_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, later = _armable(monkeypatch)

    with pytest.raises(CreditEnforcementRejected, match=f"^{_GUARD_MESSAGE}$"):
        await service.arm_enforcement(set_by="task", expected_report_digest="a" * 64)

    # Refused before the orphan closures (the first write) and every check.
    later.reconcile_orphans.assert_not_awaited()
    later.capability_ready.assert_not_awaited()
    later.cutoff_report.assert_not_awaited()
    service.session.get.assert_not_awaited()  # type: ignore[attr-defined]
    service.session.flush.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_with_the_swap_arming_reaches_the_orphan_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control case: only the predicate stands in the way."""

    from src.services import credit_rollout

    service, later = _armable(monkeypatch)
    later.reconcile_orphans.return_value = Mock(open_count=1)
    monkeypatch.setattr(credit_rollout, _GUARD, lambda: True)

    with pytest.raises(CreditEnforcementRejected, match="orphan reconciliation"):
        await service.arm_enforcement(set_by="task", expected_report_digest="a" * 64)
    later.reconcile_orphans.assert_awaited_once()


def test_the_guard_predicate_is_a_constant_false() -> None:
    """It reads no environment variable, configuration, flag or row: its
    body (after the docstring) is ``return False`` and nothing else."""

    import ast
    import inspect

    from src.services import credit_rollout

    assert credit_rollout.uazapi_enforced_credit_adapter_available() is False
    (function,) = ast.parse(
        inspect.getsource(credit_rollout.uazapi_enforced_credit_adapter_available)
    ).body
    assert isinstance(function, ast.FunctionDef) and not function.args.args
    docstring, statement = function.body
    assert isinstance(docstring, ast.Expr) and isinstance(docstring.value, ast.Constant)
    assert "uazapi-credit-enforcement" in docstring.value.value
    assert isinstance(statement, ast.Return)
    assert isinstance(statement.value, ast.Constant)
    assert statement.value.value is False


def _guard_references(path: Path) -> list[tuple[int, str]]:
    """Every reference to the predicate in *path*: how it is used."""

    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    # Docstrings and the ``__all__`` strings are declarations, not usages.
    declarations = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        ):
            declarations.update(id(inner) for inner in ast.walk(node.value))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.FunctionDef) and node.name == _GUARD:
            found.append((line, "def"))
        elif isinstance(node, ast.Global | ast.Nonlocal) and _GUARD in node.names:
            found.append((line, "rebind"))
        elif isinstance(node, ast.alias) and _GUARD in (node.name, node.asname):
            found.append((line, "import"))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _GUARD in node.value
            and id(node) not in declarations
        ):
            found.append((line, "string"))
        elif (isinstance(node, ast.Name) and node.id == _GUARD) or (
            isinstance(node, ast.Attribute) and node.attr == _GUARD
        ):
            parent = parents.get(id(node))
            called = isinstance(parent, ast.Call) and parent.func is node
            found.append((line, "call" if called else "other"))
    return found


def test_no_production_code_path_swaps_the_guard() -> None:
    """Under ``src/``: the ``def`` in ``credit_rollout``, its one call in
    ``arm_enforcement`` and its one call in the ops ``status`` (plus the
    ``__all__`` entry and docstrings) -- nothing that assigns, rebinds,
    imports by name or names it in a string (``setattr`` targets)."""

    usages: dict[str, list[str]] = {}
    for path in sorted((_ROOT / "src").rglob("*.py")):
        for _line, kind in _guard_references(path):
            usages.setdefault(str(path.relative_to(_ROOT)), []).append(kind)
    assert usages == {
        "src/services/credit_rollout.py": ["def", "call"],
        "src/ops/credit_enforcement.py": ["call"],
    }


def test_the_reference_scan_sees_a_swap() -> None:
    """The scan above is not vacuous: each way to swap the predicate is a
    usage it reports as something other than a call."""

    import tempfile

    probes = {
        "assign": f"from src.services import credit_rollout\n"
        f"credit_rollout.{_GUARD} = lambda: True\n",
        "setattr": "import src.services.credit_rollout as r\n"
        f"setattr(r, '{_GUARD}', lambda: True)\n",
        "import": f"from src.services.credit_rollout import {_GUARD}\n",
        "global": f"def f():\n    global {_GUARD}\n",
    }
    with tempfile.TemporaryDirectory() as directory:
        for name, source in probes.items():
            path = Path(directory) / f"{name}.py"
            path.write_text(source, encoding="utf-8")
            kinds = {kind for _line, kind in _guard_references(path)}
            assert kinds - {"call"}, (name, kinds)
