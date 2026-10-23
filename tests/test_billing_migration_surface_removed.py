"""Group 10 phase E2: the legacy billing-migration saga surface stays removed.

The AbacatePay -> ASAAS migration saga (service, export, operator client,
admin routes, provider-evidence configuration) was removed without a schema
change: production holds no legacy rows, and the database objects it used
are dropped by later phases with their own migrations (036 dropped the roster
fence on ``billing_subscriptions``, ``migration_id`` and ``roster_revision``;
037 dropped ``billing_migrations*`` and ``billing_roster_control`` with the
ORM models that mirrored them).
These checks fail if any removed entry point comes back.  Phase E7 retired
the observability half of the saga (the dead manual-intervention alert, the
two never-emitted migration metrics and their label dimensions) and renamed
the live readback alert, in one rotation of the alert rules digest pinned
here.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, get_args
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]

_REMOVED_MODULES = (
    "src.services.billing_migration",
    "src.services.billing_export",
)
_REMOVED_FILES = (
    "src/services/billing_migration.py",
    "src/services/billing_export.py",
    "scripts/asaas-billing-migration.py",
    "docs/runbooks/asaas-billing-migration.md",
    "tests/services/test_billing_migration.py",
    "tests/scripts/test_asaas_billing_migration.py",
    "tests/services/test_billing_export.py",
)
_SCANNED_ROOTS = ("src", "scripts", "tests")
# Shipped text that could still link the removed runbook, client or service.
_SHIPPED_TEXT_ROOTS = (
    "src",
    "scripts",
    "config",
    "docs",
    "infra",
    ".github",
    "README.md",
    "SETUP.md",
    "AGENTS.md",
    "Makefile",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.production.yml",
)
_RETIRED_REFERENCES = (
    "asaas-billing-migration",
    "billing_migration.py",
    "billing_export.py",
    "services.billing_migration",
    "services.billing_export",
    "services/billing_migration",
    "services/billing_export",
)
# Any /billing/migration(s) path, including variants such as "-legacy".
_REMOVED_ROUTE = re.compile(r"/billing/migrations?\b")
# The admin-router paths removed in phase E2 (prefix /v1/admin in the app).
_RETIRED_ROUTE_PATHS = (
    "/billing/migrations",
    "/billing/migrations/roster",
    "/billing/migrations/cancel-begin",
    "/billing/migrations/cancel-reconcile",
    "/billing/migrations/recreate-pending",
    "/billing/migrations/recreate-reopen",
    "/billing/migrations/recreate-reserve",
    "/billing/migrations/recover-checkout",
    "/billing/migrations/manual-intervention",
    "/billing/migrations/absence-reads",
    "/billing/migrations/provider-flip",
)
_REMOVED_SCHEMAS = frozenset(
    {
        "BillingCheckoutReservationResponse",
        "BillingMigrationAbsenceReadRequest",
        "BillingMigrationAbsenceReadResponse",
        "BillingMigrationCancelBeginRequest",
        "BillingMigrationCancelReconcileRequest",
        "BillingMigrationProviderFlipRequest",
        "BillingMigrationRecoverCheckoutRequest",
        "BillingMigrationReserveAttemptRequest",
        "BillingMigrationResponse",
        "BillingMigrationRosterRequest",
        "BillingMigrationTargetRequest",
        "BillingMigrationTransitionRequest",
        "BillingProviderEvidenceRequest",
    }
)
# SHA-256 of the raw bytes of both rules copies after the E7 rotation.  A
# rules change must update this pin and docs/configuration.md together.
_RULES_SHA256 = "7464018bfa2d496dddbef2184ffd1a842804c33f614ce90d878fc1eaeeb267f0"
# The pre-E7 digest, split so this guard is not itself a pin of it.  Only
# docs/configuration.md may still name it, as release history.
_RETIRED_RULES_SHA256 = (
    "e37147012adaf5e847f6f6171e15443d" "268f8e51acde261ae728c1265f447dad"
)
_RETIRED_DIGEST_HISTORY = frozenset({"docs/configuration.md"})
_RULES_COPIES = ("config/prometheus_rules.yml", "src/config/prometheus_rules.yml")
# Names of the retired observability surface: the dead alert, the readback
# alert's old name, the two migration families, their Python symbols and
# their label-dimension policies.
_RETIRED_OBSERVABILITY_NAMES = (
    "BillingMigrationManualIntervention",
    "BillingMigrationReadbackBacklog",
    "neurostack_billing_migration_operations",
    "neurostack_billing_migration_backlog",
    "BILLING_MIGRATION_OPERATIONS_TOTAL",
    "BILLING_MIGRATION_BACKLOG",
    "_BILLING_MIGRATION_OPERATION",
    "_BILLING_MIGRATION_STATE",
)
_READBACK_ALERT = {
    "alert": "BillingReadbackBacklog",
    "expr": "sum(neurostack_billing_readback_backlog) > 0",
    "for": "10m",
    "labels": {"severity": "warning", "team": "backend"},
    "annotations": {
        "summary": "Billing readback backlog is not converging",
        "description": "Exact-ID billing readback is still required",
        "runbook_url": "https://docs.neurostack.io/runbooks/asaas-billing-reconciliation",
    },
}
_LIVE_SCOPES = frozenset({"rollout", "topup_capability", "incident"})


# ---------------------------------------------------------------------------
# Modules and files
# ---------------------------------------------------------------------------


def _python_files() -> Iterator[Path]:
    for root in _SCANNED_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path


def _imported_names(path: Path, *, root: Path = ROOT) -> Iterator[str]:
    """Every module an AST imports, including lazy and dynamic imports."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = list(path.relative_to(root).with_suffix("").parts[:-1])
                base = package[: len(package) - node.level + 1]
                module = ".".join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            yield module
            for alias in node.names:
                yield f"{module}.{alias.name}"
        elif isinstance(node, ast.Call) and node.args:
            function = node.func
            name = (
                function.attr
                if isinstance(function, ast.Attribute)
                else getattr(function, "id", "")
            )
            first = node.args[0]
            if (
                name in {"import_module", "__import__"}
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
            ):
                yield first.value


def _is_removed(module: str) -> bool:
    return any(
        module == removed or module.startswith(f"{removed}.")
        for removed in _REMOVED_MODULES
    )


def test_import_scan_sees_lazy_relative_and_dynamic_imports(tmp_path: Path) -> None:
    """The scanner itself: a scan that sees nothing would pass vacuously."""

    package = tmp_path / "src" / "services"
    package.mkdir(parents=True)
    probe = package / "probe.py"
    probe.write_text(
        "import importlib\n"
        "def lazy():\n"
        "    from src.services.billing_migration import BillingMigrationService\n"
        "    from . import billing_export\n"
        "    from ..services import billing_migration as relative_parent\n"
        "    importlib.import_module('src.services.billing_export')\n"
        "    __import__('src.services.billing_migration')\n",
        encoding="utf-8",
    )
    lines = probe.read_text(encoding="utf-8").splitlines()

    for index in range(2, len(lines)):
        single = tmp_path / "src" / "services" / f"single_{index}.py"
        single.write_text(f"def lazy():\n{lines[index]}\n", encoding="utf-8")
        found = [
            name for name in _imported_names(single, root=tmp_path) if _is_removed(name)
        ]
        assert found, lines[index]
    assert not [
        name
        for name in _imported_names(probe, root=tmp_path)
        if "billing_" in name and not _is_removed(name)
    ]
    assert not _is_removed("src.services.billing_migration_extra")
    assert not _is_removed("src.services.billing_operations")


def test_no_module_imports_the_removed_saga_or_export() -> None:
    scanned = 0
    offenders: list[str] = []
    for path in _python_files():
        scanned += 1
        for module in _imported_names(path):
            if _is_removed(module):
                offenders.append(f"{path.relative_to(ROOT)}: {module}")

    assert scanned > 100, "the import scan found no source files"
    assert offenders == []


def test_removed_saga_files_are_gone() -> None:
    assert [path for path in _REMOVED_FILES if (ROOT / path).exists()] == []
    for module in _REMOVED_MODULES:
        with pytest.raises(ModuleNotFoundError):
            __import__(module)


def _shipped_text_files() -> Iterator[Path]:
    for root in _SHIPPED_TEXT_ROOTS:
        base = ROOT / root
        assert base.exists(), f"scan root {root} is missing"
        paths = [base] if base.is_file() else sorted(base.rglob("*"))
        for path in paths:
            if path.is_file() and "__pycache__" not in path.parts:
                yield path


def test_no_shipped_file_points_at_the_removed_runbook_client_or_service() -> None:
    scanned = 0
    offenders = []
    for path in _shipped_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        scanned += 1
        offenders.extend(
            f"{path.relative_to(ROOT)}: {reference}"
            for reference in _RETIRED_REFERENCES
            if reference in text
        )

    assert scanned > 100, "the shipped-text scan found no files"
    assert offenders == []


@pytest.mark.parametrize(
    "path",
    [
        "env.example",
        ".env.example",
        "docker-compose.yml",
        "docker-compose.production.yml",
        "tests/fixtures/compose/production-synthetic.env",
    ],
)
def test_deployment_inputs_no_longer_name_the_provider_evidence_key(
    path: str,
) -> None:
    # The key was read only by the removed saga; Settings ignores it if a
    # stale deployment still injects it.
    text = (ROOT / path).read_text(encoding="utf-8")

    assert "ASAAS_PROVIDER_EVIDENCE" not in text.upper()


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def test_removed_route_pattern_matches_every_retired_path() -> None:
    """The route checks filter with this pattern; it must not match nothing."""

    for path in _RETIRED_ROUTE_PATHS:
        assert _REMOVED_ROUTE.search(path), path
        assert _REMOVED_ROUTE.search(f"/v1/admin{path}"), path
    assert _REMOVED_ROUTE.search("/v1/admin/billing/migrations-legacy")
    assert _REMOVED_ROUTE.search("/v1/admin/billing/migration/roster")
    for kept in (
        "/billing/rollout/transitions",
        "/billing/operations/receipts",
        "/billing/topup-capability/enable",
        "/v1/billing/checkout",
        "/v1/admin/billing/producer-receipts",
    ):
        assert not _REMOVED_ROUTE.search(kept), kept


def test_admin_router_has_no_billing_migration_route() -> None:
    from src.api.routes.admin import router

    paths = sorted(getattr(route, "path", "") for route in router.routes)

    # The billing control plane itself stays mounted on the same router.
    assert "/billing/rollout/transitions" in paths
    assert "/billing/operations/receipts" in paths
    assert [path for path in paths if _REMOVED_ROUTE.search(path)] == []


def test_application_and_openapi_document_no_billing_migration_path() -> None:
    from src.api.main import create_app
    from src.config.settings import settings
    from tests.api._route_path_support import effective_routes

    app = create_app()
    # Included routers resolved to the prefixed paths the app dispatches.
    app_paths = {
        route.path for route in effective_routes(app) if isinstance(route.path, str)
    }
    schema = app.openapi()
    documented = set(schema["paths"])
    components = set(schema.get("components", {}).get("schemas", {}))

    rollout = f"{settings.api_prefix}/admin/billing/rollout/transitions"
    assert rollout in app_paths
    assert rollout in documented
    assert [path for path in app_paths if _REMOVED_ROUTE.search(path)] == []
    assert [path for path in documented if _REMOVED_ROUTE.search(path)] == []
    assert components & _REMOVED_SCHEMAS == set()
    assert [name for name in components if name.startswith("BillingMigration")] == []


def test_admin_schemas_no_longer_define_the_saga_models() -> None:
    import src.api.routes.admin as routes
    import src.api.schemas.admin as schemas

    defined = set(vars(schemas))
    assert defined & _REMOVED_SCHEMAS == set()
    assert [name for name in defined if name.startswith("BillingMigration")] == []
    for helper in ("_migration_response", "_consume_migration_quorum"):
        assert not hasattr(routes, helper), helper
    assert not hasattr(routes, "_provider_evidence")


# ---------------------------------------------------------------------------
# Repository, rollout facade and operation scope
# ---------------------------------------------------------------------------


def test_repository_and_rollout_drop_the_legacy_saga_entry_points() -> None:
    from src.repositories.billing import BillingRepository
    from src.services import billing_rollout

    assert not hasattr(BillingRepository, "rebase_legacy_migration")
    assert not hasattr(billing_rollout, "rebase_legacy_billing_migration")
    reserve = inspect.signature(BillingRepository.reserve_general_subscription_checkout)
    assert "migration_id" not in reserve.parameters


def test_application_code_no_longer_takes_the_roster_fence() -> None:
    """Group 10 E4: the Python roster fence is gone; migration 036 dropped
    the database fence on billing_subscriptions and 037 the legacy tables,
    their triggers and the ORM models that mirrored them, so no file under
    src/ (models included) knows the roster or its key."""

    import src.repositories.billing as repository
    from src.repositories.billing import BillingRepository

    assert not hasattr(BillingRepository, "lock_billing_roster")
    assert not hasattr(repository, "billing_roster_advisory_key")
    assert not hasattr(repository, "BillingRosterControl")
    for path in sorted((ROOT / "src").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for needle in (
            "lock_billing_roster",
            "billing_roster_advisory_key",
            "BillingRosterControl",
            "billing_roster_control",
            "BillingMigration",
            "billing_migrations",
            "billing-roster-control",
            "3486705124568449331",
        ):
            assert needle not in source, (path.relative_to(ROOT), needle)


def test_operation_scope_migration_is_retired_in_code() -> None:
    from src.api.schemas.admin import BillingOperationMaterialRequest
    from src.services.billing_operations import (
        OPERATION_SCOPES,
        BillingOperationRejected,
        OperationScope,
        operation_payload_digest,
    )

    assert OPERATION_SCOPES == _LIVE_SCOPES
    assert set(get_args(OperationScope)) == _LIVE_SCOPES
    scope_annotation = BillingOperationMaterialRequest.model_fields["scope"].annotation
    assert set(get_args(scope_annotation)) == _LIVE_SCOPES

    material = {
        "operation_id": uuid4(),
        "environment": "test",
        "from_state": "pending",
        "to_state": "in_progress",
        "budget_digest": "a" * 64,
        "deployment_digest": "b" * 64,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    with pytest.raises(ValidationError, match="scope"):
        BillingOperationMaterialRequest.model_validate(
            {**material, "scope": "migration"}
        )
    with pytest.raises(
        BillingOperationRejected, match="^billing_operation_scope_invalid$"
    ):
        operation_payload_digest(scope="migration", **material)  # type: ignore[arg-type]
    # A live scope still produces a digest from the same material.
    assert len(operation_payload_digest(scope="rollout", **material)) == 64


def test_code_scopes_stay_within_the_database_check() -> None:
    """The DB CHECK (migration 028) keeps 'migration'; code only narrows it."""

    from sqlalchemy import CheckConstraint

    from src.models.billing import BillingOperationAssignment, BillingOperationReceipt
    from src.services.billing_operations import OPERATION_SCOPES

    for model in (BillingOperationAssignment, BillingOperationReceipt):
        checks = [
            str(constraint.sqltext)
            for constraint in model.__table__.constraints
            if isinstance(constraint, CheckConstraint)
            and str(constraint.name).endswith("_scope")
        ]
        assert len(checks) == 1, model.__name__
        assert all(f"'{scope}'" in checks[0] for scope in OPERATION_SCOPES)


# ---------------------------------------------------------------------------
# Phase E7: the rules rotation and the retired observability surface
# ---------------------------------------------------------------------------


def _alert_rules(path: str) -> list[dict[str, Any]]:
    parsed = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
    return [
        {**rule, "group": group["name"]}
        for group in parsed["groups"]
        for rule in group["rules"]
        if "alert" in rule
    ]


@pytest.mark.parametrize("path", _RULES_COPIES)
def test_alert_rules_digest_is_the_rotated_one(path: str) -> None:
    assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == _RULES_SHA256


def test_both_rules_copies_are_byte_identical() -> None:
    primary, mirror = ((ROOT / path).read_bytes() for path in _RULES_COPIES)

    assert primary == mirror


@pytest.mark.parametrize("path", _RULES_COPIES)
def test_readback_alert_exists_once_under_its_new_name(path: str) -> None:
    rules = _alert_rules(path)
    matches = [rule for rule in rules if rule["alert"] == _READBACK_ALERT["alert"]]

    assert len(matches) == 1
    # Only the name moved: expression, window, labels and annotations did not.
    assert matches[0] == {**_READBACK_ALERT, "group": "neurostack_billing_operations"}
    assert [
        rule["alert"]
        for rule in rules
        if rule["group"] == "neurostack_billing_operations"
    ] == [
        "BillingReadbackBacklog",
        "BillingOperationReceiptRejected",
        "BillingTopupCapabilityRevoked",
    ]
    assert not [rule for rule in rules if "migration" in str(rule).lower()]


# prometheus_client methods that give a sample a value.  A mention of the
# metric object (an import, a re-export, a comment) is not a writer.
_WRITER_METHODS = frozenset({"inc", "dec", "set", "observe", "set_to_current_time"})


def _metric_receivers(node: ast.expr) -> set[str]:
    """Names a writer call lands on: ``M``, ``mod.M``, ``M.labels(...)``."""

    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "labels"
    ):
        return _metric_receivers(node.func.value)
    if isinstance(node, ast.IfExp):  # (m.labels(**labels) if labels else m)
        return _metric_receivers(node.body) | _metric_receivers(node.orelse)
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    return set()


def _written_names(node: ast.AST) -> set[str]:
    return {
        name
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr in _WRITER_METHODS
        for name in _metric_receivers(call.func.value)
    }


def _written_metric_symbols(source: str) -> set[str]:
    """Metric objects a module gives a value, read from its AST.

    Direct writers (``M.labels(...).inc()``, ``M.set(v)``) and one level of
    module-level helpers whose parameter is written (``_set_metric(M, v)``
    where ``_set_metric(metric, ...)`` calls ``metric.set``).
    """

    tree = ast.parse(source)
    written = _written_names(tree)
    helpers: dict[str, tuple[list[str], set[str]]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            params = [arg.arg for arg in (*node.args.posonlyargs, *node.args.args)]
            keyword_only = [arg.arg for arg in node.args.kwonlyargs]
            targets = _written_names(node) & {*params, *keyword_only}
            if targets:
                helpers[node.name] = (params, targets)
    for call in ast.walk(tree):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id in helpers
        ):
            continue
        params, targets = helpers[call.func.id]
        passed = [
            argument
            for index, argument in enumerate(call.args)
            if index < len(params) and params[index] in targets
        ] + [keyword.value for keyword in call.keywords if keyword.arg in targets]
        for argument in passed:
            written |= _metric_receivers(argument)
    return written


def _application_metric_writers() -> set[str]:
    return {
        symbol
        for path in sorted((ROOT / "src").rglob("*.py"))
        if path != ROOT / "src/observability/metrics.py"
        for symbol in _written_metric_symbols(path.read_text(encoding="utf-8"))
    }


@pytest.mark.parametrize(
    ("source", "written"),
    [
        pytest.param(
            "from m import X\n__all__ = ['X']\n# X.set(1)\n", False, id="import-only"
        ),
        pytest.param(
            "from m import X, Y\nY.labels(resource='a').set(1)\nX\n",
            False,
            id="writer-on-a-different-metric",
        ),
        pytest.param(
            "from m import X\nfor r in ():\n    pass\n", False, id="writer-is-pass"
        ),
        pytest.param(
            "def f(metric):\n    return metric\nf(X)\n",
            False,
            id="helper-that-does-not-write",
        ),
        pytest.param("X.labels(resource='a').set(1)\n", True, id="labels-set"),
        pytest.param("metrics.X.inc()\n", True, id="module-attribute-inc"),
        pytest.param(
            "def _set(metric, value, **labels):\n"
            "    (metric.labels(**labels) if labels else metric).set(value)\n"
            "_set(X, 1, condition='stale')\n",
            True,
            id="module-helper",
        ),
    ],
)
def test_the_writer_scan_counts_only_a_value_given_to_that_metric(
    source: str, written: bool
) -> None:
    assert ("X" in _written_metric_symbols(source)) is written


def test_every_billing_alert_reads_a_metric_the_application_writes() -> None:
    """A billing alert on a family nothing sets can never fire (the E7 bug).

    The symbol must be the receiver of an actual ``inc``/``set``/``observe``
    call in ``src`` (read from the AST): an import or any other mention of
    the metric object does not count. This proves that a write EXISTS in the
    code, not that production code CALLS it: ``reconcile_backlog`` (the writer
    of the readback backlog gauge) has no production caller today (MEDIUM-D).
    """

    tree = ast.parse((ROOT / "src/observability/metrics.py").read_text("utf-8"))
    symbols = {
        call.args[0].value: node.targets[0].id
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and isinstance(call := node.value, ast.Call)
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
    }
    writers = _application_metric_writers()
    billing = [
        rule
        for rule in _alert_rules(_RULES_COPIES[0])
        if rule["group"].startswith("neurostack_billing")
    ]

    assert len(billing) == 6
    for rule in billing:
        families = re.findall(r"\bneurostack_[a-z0-9_]+", rule["expr"])
        assert families, rule["alert"]
        for family in families:
            symbol = symbols.get(family)
            assert symbol, (rule["alert"], family)
            assert symbol in writers, (rule["alert"], symbol)


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> list[Any]:
        return list(self._rows)

    def all(self) -> list[Any]:
        return list(self._rows)


class _BacklogSession:
    """The four reads of ``reconcile_backlog``, in order; no database."""

    def __init__(self, *results: list[Any]) -> None:
        self._results = list(results)
        self.executed = 0

    async def execute(self, _statement: Any) -> _Rows:
        self.executed += 1
        return _Rows(self._results.pop(0))

    async def rollback(self) -> None:
        return None


async def test_reconcile_backlog_sets_the_readback_backlog_gauge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The only writer of the family ``BillingReadbackBacklog`` reads."""

    from prometheus_client import REGISTRY

    from src.observability.metrics import BILLING_READBACK_BACKLOG
    from src.services.billing_webhook import BillingReadbackService

    family = "neurostack_billing_readback_backlog"
    resources = ("delivery", "subscription", "checkout_session")
    for resource in resources:
        BILLING_READBACK_BACKLOG.labels(resource=resource).set(77)
    session = _BacklogSession(
        [uuid4(), uuid4(), uuid4()],  # deliveries
        [uuid4()],  # subscriptions
        [(uuid4(),), (uuid4(),)],  # checkout sessions awaiting readback
        [],  # ambiguous attempts
    )
    service = BillingReadbackService(
        session,  # type: ignore[arg-type]
        client_factory=lambda: pytest.fail("no provider call expected"),
        environment="test",
    )
    delivery_outcomes = iter(("converged", "pending", "failed"))
    checkout_outcomes = iter(("drift", "skipped"))

    async def process_delivery(_delivery_id: Any) -> str:
        return next(delivery_outcomes)

    async def readback_subscription(_subscription_id: Any) -> str:
        return "failed"

    async def readback_checkout(_session_id: Any) -> str:
        return next(checkout_outcomes)

    monkeypatch.setattr(service, "process_delivery", process_delivery)
    monkeypatch.setattr(service, "readback_subscription", readback_subscription)
    monkeypatch.setattr(service, "readback_checkout", readback_checkout)

    counts = await service.reconcile_backlog()

    assert session.executed == 4
    assert counts == {"delivery": 2, "subscription": 1, "checkout_session": 1}
    assert {
        resource: REGISTRY.get_sample_value(family, {"resource": resource})
        for resource in resources
    } == {"delivery": 2.0, "subscription": 1.0, "checkout_session": 1.0}


def test_retired_observability_names_are_gone_from_every_shipped_file() -> None:
    """Rules, metrics, dimensions, verifier, runbooks and the rest of docs."""

    scanned = 0
    offenders = []
    for path in _shipped_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        scanned += 1
        offenders.extend(
            f"{path.relative_to(ROOT)}: {name}"
            for name in _RETIRED_OBSERVABILITY_NAMES
            if name in text
        )

    assert scanned > 100, "the shipped-text scan found no files"
    for required in (
        *_RULES_COPIES,
        "src/observability/metrics.py",
        "src/observability/metric_dimensions.py",
        "scripts/verify-asaas-observability.py",
        "docs/runbooks/asaas-billing-incident.md",
        "docs/runbooks/asaas-billing-reconciliation.md",
        "docs/runbooks/asaas-deployment.md",
    ):
        assert (ROOT / required).is_file(), required
    assert offenders == []


def test_the_retired_rules_digest_survives_only_as_documented_history() -> None:
    offenders = []
    for root in (*_SHIPPED_TEXT_ROOTS, "tests"):
        base = ROOT / root
        for path in [base] if base.is_file() else sorted(base.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            relative = path.relative_to(ROOT).as_posix()
            if (
                _RETIRED_RULES_SHA256 in text
                and relative not in _RETIRED_DIGEST_HISTORY
            ):
                offenders.append(relative)

    assert offenders == []
    configuration = (ROOT / "docs/configuration.md").read_text(encoding="utf-8")
    # The release note names the current digest, after the retired one.
    assert _RULES_SHA256 in configuration
    assert configuration.index(_RETIRED_RULES_SHA256) < configuration.index(
        _RULES_SHA256
    )


def test_migration_metrics_are_retired_with_the_rules_rotation() -> None:
    from prometheus_client import REGISTRY

    import src.observability.metric_dimensions as dimensions
    import src.observability.metrics as metrics
    from src.observability.metric_dimensions import (
        METRIC_DIMENSION_CONTRACTS,
        MetricDimensionContractError,
        metric_dimension_contract,
    )

    for symbol in ("BILLING_MIGRATION_OPERATIONS_TOTAL", "BILLING_MIGRATION_BACKLOG"):
        assert not hasattr(metrics, symbol), symbol
    for policy in ("_BILLING_MIGRATION_OPERATION", "_BILLING_MIGRATION_STATE"):
        assert not hasattr(dimensions, policy), policy
    for family in (
        "neurostack_billing_migration_operations",
        "neurostack_billing_migration_backlog",
    ):
        with pytest.raises(MetricDimensionContractError, match="unknown"):
            metric_dimension_contract(family)
    assert not [
        name for name in METRIC_DIMENSION_CONTRACTS if "billing_migration" in name
    ]
    assert not [
        name
        for contract in METRIC_DIMENSION_CONTRACTS.values()
        for policy in contract.policies.values()
        for name in policy.vocabulary or ()
        if name == "roster_import"
    ]
    assert not [
        family.name
        for family in REGISTRY.collect()
        if family.name.startswith("neurostack_billing_migration")
    ]
    # The live readback family the renamed alert reads is still registered.
    assert metrics.BILLING_READBACK_BACKLOG._name == (
        "neurostack_billing_readback_backlog"
    )


# ---------------------------------------------------------------------------
# E7 live order: the worker flag ships first, the E7 image after
# ---------------------------------------------------------------------------

# doc -> (start of the E7 order text, its end, the step headings in order).
_E7_ORDER_DOCS = {
    "docs/runbooks/asaas-deployment.md": (
        "**E7-A (alerts and digest)",
        "- **E8 / A8 (CI)",
        (
            "1. **Worker flag first, its own change.**",
            "2. **Observe the worker.**",
            "3. **E7 image after, never in the same revision.**",
        ),
    ),
    "docs/configuration.md": (
        "Ordem do E7 na AWS",
        "## Primeiro acesso",
        (
            "1. **Primeiro a flag, numa mudança própria.**",
            "2. **Observar.**",
            "3. **Depois, a imagem do E7, nunca na mesma revisão.**",
        ),
    ),
}
_RETIRED_E7_ORDER = "after the image, never bundled"
_MENTIONS_FLAG = re.compile(r"BILLING_FACT_WORKER_ENABLED|\bflag\b|\bworker\b", re.I)
_MENTIONS_IMAGE = re.compile(r"\bimage\b|\bimagem\b", re.I)
_FLAG_AFTER_IMAGE = re.compile(
    r"\bafter the (?:\S+ ){0,2}image\b|\bdepois da (?:\S+ ){0,2}imagem\b", re.I
)
_FLAG_BUNDLED_WITH_IMAGE = re.compile(
    r"bundled with|together with|alongside|in the same (?:deploy|revision)"
    r"|in (?:one|a single) (?:deploy|revision)|junto com"
    r"|n[oa] mesm[oa] (?:deploy|revisão)|num único deploy",
    re.I,
)
_NEGATED = re.compile(r"\b(?:never|not|nunca|não)\b[^,;]{0,12}$", re.I)


def _e7_order_violations(text: str) -> list[str]:
    """Sentences that put the worker flag after, or in, the E7 image deploy."""

    flat = " ".join(text.split())
    violations = [_RETIRED_E7_ORDER] if _RETIRED_E7_ORDER in flat else []
    for sentence in re.split(r"(?<=[.;])\s+", flat):
        if not (_MENTIONS_FLAG.search(sentence) and _MENTIONS_IMAGE.search(sentence)):
            continue
        violations.extend(m.group(0) for m in _FLAG_AFTER_IMAGE.finditer(sentence))
        violations.extend(
            m.group(0)
            for m in _FLAG_BUNDLED_WITH_IMAGE.finditer(sentence)
            if not _NEGATED.search(sentence[: m.start()])
        )
    return violations


@pytest.mark.parametrize(
    ("wording", "violates"),
    [
        pytest.param(
            "2. **Worker flag.** `BILLING_FACT_WORKER_ENABLED=true` ships as its"
            " own reviewed change, after the image, never bundled with it.",
            True,
            id="retired-flag-after-image",
        ),
        pytest.param(
            "Ship `BILLING_FACT_WORKER_ENABLED=true` together with the E7 image.",
            True,
            id="bundled-together",
        ),
        pytest.param(
            "Turn the worker flag on in the same revision as the E7 image.",
            True,
            id="bundled-same-revision",
        ),
        pytest.param(
            "Turn the worker flag on after the E7 image is live.",
            True,
            id="flag-after-e7-image",
        ),
        pytest.param(
            "A flag `BILLING_FACT_WORKER_ENABLED=true` sai junto com a imagem.",
            True,
            id="pt-bundled",
        ),
        pytest.param(
            "The flag change and the E7 image never share a revision or a deploy.",
            False,
            id="never-share",
        ),
        pytest.param(
            "3. **Depois, a imagem do E7, nunca na mesma revisão.** Só então a"
            " imagem do E7 é implantada, depois da flag.",
            False,
            id="pt-image-after-flag",
        ),
    ],
)
def test_the_e7_order_scan_flags_a_flag_after_or_with_the_image(
    wording: str, violates: bool
) -> None:
    assert bool(_e7_order_violations(wording)) is violates


@pytest.mark.parametrize("path", sorted(_E7_ORDER_DOCS))
def test_e7_ships_the_worker_flag_first_and_the_e7_image_after(path: str) -> None:
    """Wish Group 10, E7: the worker is deployed (code and flag) before E7.

    Owner decision 2026-10-22: "worker deployed" means code deployed and
    ``BILLING_FACT_WORKER_ENABLED=true``; the flag goes on first, on the
    bridge image, as its own reviewed change, is observed, and only then is
    the E7 image deployed, never in the same revision.
    """

    text = (ROOT / path).read_text(encoding="utf-8")
    start_marker, end_marker, steps = _E7_ORDER_DOCS[path]
    start = text.index(start_marker)
    block = " ".join(text[start : text.index(end_marker, start)].split())
    positions = [block.index(step) for step in steps]

    assert positions == sorted(positions)
    flag_step = block[positions[0] : positions[1]]
    assert "BILLING_FACT_WORKER_ENABLED=true" in block[: positions[1]]
    assert "ManagedBy=manual" in flag_step
    if path.endswith("asaas-deployment.md"):
        assert "DescribeTaskDefinition" in flag_step
        observe_step = block[positions[1] : positions[2]]
        assert "neurostack_billing_fact_worker_last_tick_timestamp_seconds" in (
            observe_step
        )
        assert "neurostack_billing_fact_backlog" in observe_step
    assert _e7_order_violations(text) == []
