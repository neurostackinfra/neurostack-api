"""The ASAAS producer never depends on credit code (credit wish, Group 2).

By import analysis (AST, nothing is imported):

* no module of the producer set pinned in the wish's "Contract and
  dependency pins" imports credit code, directly, relatively, through a
  package attribute or by ``importlib``/``__import__`` with a literal;
* outside the credit modules, only ``src/api/main.py`` may import
  ``handle_billing_fact`` -- and no group before Group 8 wires it there;
* credit modules write no ASAAS table except ``billing_fact_acks``: the
  only ``Billing*`` model they construct is ``BillingFactAck``, no
  ``insert``/``update``/``delete`` names a ``Billing*`` model and no SQL
  literal mutates a ``billing_*`` table.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]

PRODUCER_SET = (
    *(
        f"src/services/billing_{name}.py"
        for name in (
            "outbox",
            "fact_worker",
            "webhook",
            "checkout",
            "capability",
            "catalog",
            "operations",
            "rollout",
            "read_contract",
            "producer_grants",
            "producer_handoff",
            "signed_envelope",
        )
    ),
    "src/repositories/billing.py",
    "src/repositories/billing_producer.py",
    "src/models/billing.py",
    "src/models/billing_producer.py",
    "src/models/billing_producer_signed.py",
    "src/api/routes/billing.py",
    "src/api/routes/billing_producer.py",
    "src/api/routes/billing_producer_setup.py",
    "src/api/schemas/billing.py",
    "src/ops/asaas_webhook.py",
    "src/services/asaas.py",
    "src/services/asaas_secret_rotation.py",
)

# Credit code, as the wish's Group 2 boundary criterion declares it.
_CREDIT_MODULE = re.compile(
    r"^src\.("
    r"services\.credit_\w+|services\.content_retention"
    r"|models\.credit\w*|models\.content_retention"
    r"|repositories\.credit\w*|storage\.credit_\w+"
    r")(\.|$)"
)
_CREDIT_GLOBS = (
    "src/services/credit_*.py",
    "src/services/content_retention.py",
    "src/models/credit*.py",
    "src/models/content_retention.py",
    "src/repositories/credit*.py",
    "src/storage/credit_*.py",
)
_ASAAS_MUTATION_SQL = re.compile(
    r"\b(insert\s+into|update|delete\s+from)\s+billing_(?!fact_acks\b)\w+",
    re.IGNORECASE,
)


def _module_name(path: Path) -> str:
    relative = path.relative_to(_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imported_modules(path: Path, *, module: str | None = None) -> Iterator[str]:
    """Every module name an AST import, attribute import or literal dynamic
    import of ``path`` (module ``module``, default: its own) can reach."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = (module or _module_name(path)).rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                module = ".".join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            yield module
            for alias in node.names:
                yield f"{module}.{alias.name}"
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else ""
            )
            if name in ("import_module", "__import__") and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    yield first.value


def _credit_files() -> list[Path]:
    return sorted({path for pattern in _CREDIT_GLOBS for path in _ROOT.glob(pattern)})


def test_the_pinned_producer_set_exists() -> None:
    missing = [name for name in PRODUCER_SET if not (_ROOT / name).is_file()]
    assert missing == []


@pytest.mark.parametrize("relative", PRODUCER_SET)
def test_no_producer_module_imports_credit_code(relative: str) -> None:
    reached = sorted(
        name
        for name in _imported_modules(_ROOT / relative)
        if _CREDIT_MODULE.match(name)
    )
    assert reached == [], f"{relative} imports credit code: {reached}"


def test_the_analysis_sees_every_import_form(tmp_path: Path) -> None:
    probe = tmp_path / "billing_probe.py"
    probe.write_text(
        "import src.services.credit_ledger\n"
        "from src.services import credit_fact_consumer\n"
        "from .credit_reconciliation import x\n"
        "import importlib\n"
        "importlib.import_module('src.repositories.credit')\n",
        encoding="utf-8",
    )
    reached = sorted(
        name
        for name in _imported_modules(probe, module="src.services.billing_probe")
        if _CREDIT_MODULE.match(name)
    )
    assert reached == [
        "src.repositories.credit",
        "src.services.credit_fact_consumer",
        "src.services.credit_ledger",
        "src.services.credit_reconciliation",
        "src.services.credit_reconciliation.x",
    ]


def test_only_credit_code_and_main_may_import_the_fact_handler() -> None:
    credit = {path.relative_to(_ROOT).as_posix() for path in _credit_files()}
    importers = []
    for path in sorted((_ROOT / "src").rglob("*.py")):
        relative = path.relative_to(_ROOT).as_posix()
        names = set(_imported_modules(path))
        if (
            "src.services.credit_fact_consumer.handle_billing_fact" in names
            or "handle_billing_fact"
            in {
                node.attr
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                if isinstance(node, ast.Attribute)
            }
        ):
            importers.append(relative)
    assert set(importers) <= credit | {"src/api/main.py"}
    # Wiring the consumer into the worker is Group 8's (rollout opening).
    assert (
        "src/api/main.py" not in importers
    ), "handle_billing_fact is wired into main.py before Group 8"


@pytest.mark.parametrize(
    "path", _credit_files(), ids=lambda path: path.relative_to(_ROOT).as_posix()
)
def test_credit_code_writes_no_asaas_table_but_the_ack(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else ""
            if name.startswith("Billing") and name != "BillingFactAck":
                problems.append(f"constructs {name}")
            if name in ("insert", "update", "delete") and node.args:
                target = node.args[0]
                if isinstance(target, ast.Name) and target.id.startswith("Billing"):
                    problems.append(f"{name}({target.id})")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _ASAAS_MUTATION_SQL.search(node.value):
                problems.append(f"SQL literal: {node.value[:60]!r}")
    assert problems == []
