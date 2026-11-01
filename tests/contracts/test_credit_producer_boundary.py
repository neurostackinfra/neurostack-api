"""The ASAAS producer never depends on credit code (credit wish, Group 2).

By import analysis (AST, nothing is imported):

* no module of the producer set pinned in the wish's "Contract and
  dependency pins" (plus ``src/ops/billing_fact_requeue.py``,
  asaas-rollout-opening Group 2b) imports credit code, directly,
  relatively, through a package attribute or by
  ``importlib``/``__import__`` with a literal;
* outside the credit modules, only ``src/api/main.py`` may import
  ``handle_billing_fact`` or anything else of the consumer module -- and it
  does: since asaas-rollout-opening Group 2c it is the one wiring point of
  the consumer into the worker's transport;
* outside the worker itself, only ``src/api/main.py`` imports
  ``BillingFactWorker`` or ``start_billing_fact_worker`` -- credit modules
  included -- so no other module can wire the consumer into a worker;
* credit modules write no ASAAS table except ``billing_fact_acks``: the
  only ``Billing*`` model they construct is ``BillingFactAck``, no
  ``insert``/``update``/``delete`` names a ``Billing*`` model and no SQL
  literal mutates a ``billing_*`` table.

Accepted limits of the analysis: only ``src/`` is scanned (``scripts/`` and
other trees outside the API package are one-off operator tools, e.g. the
production image smoke names the consumer inside a subprocess string, which
no AST import analysis can see); a dynamic import is seen only with a
literal module name (and a literal ``package`` for a relative one), and an
attribute only by name or a literal ``getattr`` string.
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
    "src/ops/billing_fact_requeue.py",
    "src/services/asaas.py",
    "src/services/asaas_secret_rotation.py",
)
_CONSUMER_MODULE = "src.services.credit_fact_consumer"
_WIRING_POINT = "src/api/main.py"

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
                    yield _resolved_dynamic(first.value, node, package)


def _resolved_dynamic(name: str, call: ast.Call, package: str) -> str:
    """``import_module(".x", "pkg")`` names ``pkg.x``: a relative literal is
    resolved against its literal ``package`` argument (or, without one, the
    module's own package, the conservative reading)."""

    if not name.startswith("."):
        return name
    anchor: ast.expr | None = call.args[1] if len(call.args) > 1 else None
    for keyword in call.keywords:
        if keyword.arg == "package":
            anchor = keyword.value
    if isinstance(anchor, ast.Constant) and isinstance(anchor.value, str):
        package = anchor.value
    level = len(name) - len(name.lstrip("."))
    base = package.split(".")
    base = base[: len(base) - (level - 1)]
    return ".".join([*base, *([name[level:]] if name[level:] else [])])


def _credit_files() -> list[Path]:
    return sorted({path for pattern in _CREDIT_GLOBS for path in _ROOT.glob(pattern)})


def test_the_pinned_producer_set_exists() -> None:
    missing = [name for name in PRODUCER_SET if not (_ROOT / name).is_file()]
    assert missing == []
    assert "src/ops/billing_fact_requeue.py" in PRODUCER_SET
    # The credit model suite pins the same set: the two lists never drift.
    from tests.models.test_credit_models import _PRODUCER_FILES

    assert sorted(_PRODUCER_FILES) == sorted(PRODUCER_SET)


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
        "importlib.import_module('src.repositories.credit')\n"
        "__import__('src.models.credit')\n"
        "import src.storage.credit_objects as objects\n"
        "importlib.import_module('.credit_switch', 'src.services')\n"
        "importlib.import_module('..models.credit_inventory', package='src.api')\n"
        "importlib.import_module('.credit_ledger', package='src.repositories')\n",
        encoding="utf-8",
    )
    reached = sorted(
        name
        for name in _imported_modules(probe, module="src.services.billing_probe")
        if _CREDIT_MODULE.match(name)
    )
    assert reached == [
        "src.models.credit",
        "src.models.credit_inventory",
        "src.repositories.credit",
        "src.repositories.credit_ledger",
        "src.services.credit_fact_consumer",
        "src.services.credit_ledger",
        "src.services.credit_reconciliation",
        "src.services.credit_reconciliation.x",
        "src.services.credit_switch",
        "src.storage.credit_objects",
    ]


def _reaches_the_consumer(path: Path, *, module: str | None = None) -> bool:
    """Whether ``path`` imports the consumer module or any of its names, or
    reads ``handle_billing_fact`` as an attribute."""

    names = set(_imported_modules(path, module=module))
    if any(
        name == _CONSUMER_MODULE or name.startswith(f"{_CONSUMER_MODULE}.")
        for name in names
    ):
        return True
    return "handle_billing_fact" in {
        node.attr
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Attribute)
    }


def _consumer_importers() -> set[str]:
    return {
        path.relative_to(_ROOT).as_posix()
        for path in sorted((_ROOT / "src").rglob("*.py"))
        if _reaches_the_consumer(path)
    }


def test_main_is_the_only_wiring_point_of_the_fact_consumer() -> None:
    credit = {path.relative_to(_ROOT).as_posix() for path in _credit_files()}
    importers = _consumer_importers()
    assert importers - credit == {_WIRING_POINT}
    # Inverted on purpose (asaas-rollout-opening Group 2c, which took the
    # wiring over from the credit wish's Group 8): main.py wires the
    # consumer into the worker's transport, and nothing else outside credit.
    assert _WIRING_POINT in importers, "the consumer is no longer wired in main.py"


_WORKER_MODULE = "src.services.billing_fact_worker"
_WORKER_ENTRY_POINTS = ("BillingFactWorker", "start_billing_fact_worker")


def _attribute_names(tree: ast.AST) -> set[str]:
    """Every attribute ``path`` reads: ``x.name`` and a literal
    ``getattr(x, "name")``."""

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            names.add(node.args[1].value)
    return names


def _reaches_the_worker(path: Path, *, module: str | None = None) -> bool:
    """Whether ``path`` imports the worker's class or starter (a star import
    of the worker module counts), or reads one of them as an attribute (a
    credit module is NOT exempt here)."""

    names = set(_imported_modules(path, module=module))
    if any(
        f"{_WORKER_MODULE}.{entry}" in names for entry in (*_WORKER_ENTRY_POINTS, "*")
    ):
        return True
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return bool(set(_WORKER_ENTRY_POINTS) & _attribute_names(tree))


def _imports_main(path: Path, *, module: str | None = None) -> bool:
    """Whether ``path`` imports ``src.api.main`` or anything it re-exports
    (the worker entry points, ``_credit_fact_transport``)."""

    return any(
        name == "src.api.main" or name.startswith("src.api.main.")
        for name in _imported_modules(path, module=module)
    )


def test_main_is_the_only_module_that_builds_the_fact_worker() -> None:
    """Credit modules may import the consumer, but none may wire it into a
    worker of its own: ``src/api/main.py`` is the only builder."""

    worker = (_ROOT / "src/services/billing_fact_worker.py").as_posix()
    builders = {
        path.relative_to(_ROOT).as_posix()
        for path in sorted((_ROOT / "src").rglob("*.py"))
        if path.as_posix() != worker and _reaches_the_worker(path)
    }
    assert builders == {_WIRING_POINT}
    # main.py re-exports the worker entry points and its own transport
    # (``_credit_fact_transport`` calls the consumer with no worker at all):
    # no other module may reach them through it.
    main_importers = {
        path.relative_to(_ROOT).as_posix()
        for path in sorted((_ROOT / "src").rglob("*.py"))
        if path.relative_to(_ROOT).as_posix() != _WIRING_POINT and _imports_main(path)
    }
    assert main_importers == set()


@pytest.mark.parametrize(
    "source",
    [
        "from src.services.billing_fact_worker import BillingFactWorker\n",
        "from src.services.billing_fact_worker import start_billing_fact_worker\n",
        "from .billing_fact_worker import BillingFactWorker\n",
        "from src.services import billing_fact_worker\n"
        "billing_fact_worker.start_billing_fact_worker\n",
        "from src.services.billing_fact_worker import *\n"
        "worker = BillingFactWorker(None, transport=None)\n",
        "from .billing_fact_worker import *\n",
        "import importlib\n"
        "worker = importlib.import_module('src.services.billing_fact_worker')\n"
        "getattr(worker, 'BillingFactWorker')\n",
    ],
)
def test_the_worker_analysis_sees_a_credit_module_building_it(
    tmp_path: Path, source: str
) -> None:
    probe = tmp_path / "credit_probe.py"
    probe.write_text(source, encoding="utf-8")
    assert _reaches_the_worker(probe, module="src.services.credit_probe")
    probe.write_text(
        "from src.services.billing_fact_worker import TRANSPORT_TIMEOUT_SECONDS\n",
        encoding="utf-8",
    )
    assert not _reaches_the_worker(probe, module="src.services.credit_probe")


@pytest.mark.parametrize(
    "source",
    [
        "from src.api.main import start_billing_fact_worker\n",
        "from src.api.main import _credit_fact_transport\n",
        "from src.api import main\n",
        "import src.api.main\n",
        "from .main import BillingFactWorker\n",
        "import importlib\nimportlib.import_module('src.api.main')\n",
    ],
)
def test_the_main_analysis_sees_a_module_reaching_through_main(
    tmp_path: Path, source: str
) -> None:
    probe = tmp_path / "credit_probe.py"
    probe.write_text(source, encoding="utf-8")
    assert _imports_main(probe, module="src.api.credit_probe")
    probe.write_text("from src.api import routes\n", encoding="utf-8")
    assert not _imports_main(probe, module="src.api.credit_probe")


@pytest.mark.parametrize(
    "source",
    [
        "from src.services.credit_fact_consumer import handle_billing_fact\n",
        "from src.services.credit_fact_consumer import CreditSwitchOff\n",
        "import src.services.credit_fact_consumer\n",
        "from src.services import credit_fact_consumer\n",
        "from .credit_fact_consumer import FactHandling\n",
        "import src.services.credit_fact_consumer as consumer\n",
        "__import__('src.services.credit_fact_consumer')\n",
        "import importlib\n"
        "importlib.import_module('src.services.credit_fact_consumer')\n",
        "import importlib\n"
        "importlib.import_module('.credit_fact_consumer', 'src.services')\n",
        "def wire(module):\n    return module.handle_billing_fact\n",
        "def wire():\n"
        "    from src.services.credit_fact_consumer import handle_billing_fact\n"
        "    return handle_billing_fact\n",
    ],
)
def test_the_wiring_analysis_sees_every_way_to_reach_the_consumer(
    tmp_path: Path, source: str
) -> None:
    probe = tmp_path / "billing_probe.py"
    probe.write_text(source, encoding="utf-8")
    assert _reaches_the_consumer(probe, module="src.services.billing_probe")
    probe.write_text("from src.services import billing_outbox\n", encoding="utf-8")
    assert not _reaches_the_consumer(probe, module="src.services.billing_probe")


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
