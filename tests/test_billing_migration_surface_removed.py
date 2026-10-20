"""Group 10 phase E2: the legacy billing-migration saga surface stays removed.

The AbacatePay -> ASAAS migration saga (service, export, operator client,
admin routes, provider-evidence configuration) was removed without a schema
change: production holds no legacy rows, and the database objects it used
are dropped by later phases with their own migrations (036 dropped the roster
fence on ``billing_subscriptions``, ``migration_id`` and ``roster_revision``;
037 drops ``billing_migrations*`` and ``billing_roster_control``).
These checks fail if any removed entry point comes back, while pinning what
must NOT move in this phase (the alert rules and their digest).
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, get_args
from uuid import uuid4

import pytest
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
# Phase E2 must not rotate the alert rules: the two migration metrics stay
# registered (never incremented) until phase E7 rotates rules and digest.
_RULES_SHA256 = "e37147012adaf5e847f6f6171e15443d268f8e51acde261ae728c1265f447dad"
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
    """Group 10 E4: the Python roster fence is gone; only the legacy tables'
    own triggers (dropped by migration 037; 036 dropped the one on
    billing_subscriptions) still know the roster key."""

    import src.repositories.billing as repository
    from src.repositories.billing import BillingRepository

    assert not hasattr(BillingRepository, "lock_billing_roster")
    assert not hasattr(repository, "billing_roster_advisory_key")
    assert not hasattr(repository, "BillingRosterControl")
    for path in sorted((ROOT / "src").rglob("*.py")):
        if path.is_relative_to(ROOT / "src/models"):
            continue  # the ORM mirror of the legacy tables goes with 037
        source = path.read_text(encoding="utf-8")
        for needle in (
            "lock_billing_roster",
            "billing_roster_advisory_key",
            "BillingRosterControl",
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
# What phase E2 must not move
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["config/prometheus_rules.yml", "src/config/prometheus_rules.yml"]
)
def test_alert_rules_digest_is_unchanged_in_this_phase(path: str) -> None:
    assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == _RULES_SHA256


def test_migration_metrics_stay_registered_until_the_rules_rotate() -> None:
    from src.observability.metrics import (
        BILLING_MIGRATION_BACKLOG,
        BILLING_MIGRATION_OPERATIONS_TOTAL,
    )

    assert BILLING_MIGRATION_BACKLOG._name == "neurostack_billing_migration_backlog"
    assert (
        BILLING_MIGRATION_OPERATIONS_TOTAL._name
        == "neurostack_billing_migration_operations"
    )
