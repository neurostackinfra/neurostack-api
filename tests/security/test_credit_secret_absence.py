"""Group 7: credit code cannot open the ASAAS rollout nor reach a secret path.

Deliverable 10: readiness rows, acks, metrics and credit routes never write
``billing_rollout_control``, ``topup_commercial_enabled`` or
``billing_topup_capability_receipts``.  By analysis (AST and SQL literals,
nothing imported) over every credit module:

* ``BillingTopupCapabilityReceipt`` is never named (only an import would be
  harmless, and there is none);
* ``BillingRolloutControl`` is named only inside
  ``CreditRepository.lock_rollout_control``, whose body may only READ (no
  write-shaped call, no assignment);
* any call whose callee chain ends in ``insert``/``update``/``delete``/
  ``merge`` (``postgresql_insert``, ``sa.update``, ``Table.update()``,
  ``query(...).update({})``) or starts with ``on_conflict_`` and names a
  target model or table anywhere in its callee chain or arguments;
* a string -- constant, concatenation or f-string fragments folded -- that
  IS one of the table names, or that is SQL writing them
  (``INSERT``/``UPDATE``/``DELETE``/``TRUNCATE``/``MERGE``) or setting the
  column;
* ``topup_commercial_enabled`` as an assigned attribute, a ``setattr``
  name, a keyword argument or a dict key (``set_={...}``, ``values({...})``).

A PostgreSQL before/after digest of the three surfaces around the real
writers lives in ``tests/integration/test_credit_capability_ready.py`` and
``tests/integration/test_credit_fact_replay_postgres.py``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TABLES = ("billing_rollout_control", "billing_topup_capability_receipts")
RECEIPT_MODEL = "BillingTopupCapabilityReceipt"
CONTROL_MODEL = "BillingRolloutControl"
MODELS = (CONTROL_MODEL, RECEIPT_MODEL)
COLUMN = "topup_commercial_enabled"
LOCKING_READ = "lock_rollout_control"
WRITE_SUFFIXES = ("insert", "update", "delete", "merge")
WRITE_CALLS = ("add", "add_all", "delete", "merge")
# Read-only callees that merely end like a write verb.
READ_CALLEES = frozenset({"with_for_update"})
_SQL_WRITE = re.compile(
    r"\b(insert\s+into|update|delete\s+from|truncate(\s+table)?|merge\s+into)"
    r"\s+(only\s+)?(public\.)?(" + "|".join(TABLES) + r")\b",
    re.IGNORECASE,
)
_SQL_SET_COLUMN = re.compile(r"\bset\b[^;]*\b" + COLUMN + r"\b\s*=", re.IGNORECASE)


def _credit_modules() -> list[Path]:
    paths = [
        *sorted((ROOT / "src/services").glob("credit_*.py")),
        ROOT / "src/services/content_retention.py",
        *sorted((ROOT / "src/models").glob("credit*.py")),
        ROOT / "src/models/content_retention.py",
        ROOT / "src/models/operational_capability.py",
        *sorted((ROOT / "src/repositories").glob("credit*.py")),
        ROOT / "src/repositories/content_retention.py",
        *sorted((ROOT / "src/storage").glob("credit_*.py")),
        ROOT / "src/api/routes/credits.py",
        ROOT / "src/api/routes/credit_export.py",
        ROOT / "src/api/routes/admin_credits.py",
        ROOT / "src/api/schemas/credits.py",
        ROOT / "src/api/schemas/admin_credits.py",
        ROOT / "src/observability/metric_dimensions.py",
    ]
    return [path for path in paths if path.exists()]


def _terminal(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Call):
        return _terminal(func.func)
    return None


def _folded(node: ast.AST) -> str | None:
    """The static text of a constant, a ``+`` of strings or an f-string
    (formatted values become a placeholder)."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _folded(node.left), _folded(node.right)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr):
        parts = []
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                inner = _folded(part.value)
                parts.append("{}" if inner is None else inner)
            else:
                parts.append(_folded(part) or "")
        return "".join(parts)
    return None


def _sql_hit(text: str) -> bool:
    return bool(
        text in TABLES
        or text == COLUMN
        or _SQL_WRITE.search(text)
        or _SQL_SET_COLUMN.search(text)
    )


def _names_target(node: ast.AST) -> bool:
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name) and inner.id in MODELS:
            return True
        if isinstance(inner, ast.Attribute) and inner.attr in MODELS:
            return True
        text = _folded(inner)
        if text is not None and any(table in text for table in TABLES):
            return True
    return False


def _docstrings(tree: ast.Module) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
            ):
                found.add(id(node.body[0].value))
    return found


def _writes(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    docstrings = _docstrings(tree)
    found: list[str] = []

    def hit(node: ast.AST, why: str) -> None:
        found.append(f"{path.name}:{getattr(node, 'lineno', 0)}:{why}")

    def visit(node: ast.AST, function: str | None, folded_parent: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        in_read = function == LOCKING_READ
        # Strings (outside docstrings): fold whole expressions once.
        is_text = isinstance(node, (ast.Constant, ast.BinOp, ast.JoinedStr))
        text = _folded(node) if is_text and not folded_parent else None
        if text is not None and id(node) not in docstrings and _sql_hit(text):
            hit(node, "sql")
        # Model names.
        if isinstance(node, ast.Name) and node.id == RECEIPT_MODEL:
            hit(node, "receipt model")
        if isinstance(node, ast.Attribute) and node.attr == RECEIPT_MODEL:
            hit(node, "receipt model")
        if (
            isinstance(node, (ast.Name, ast.Attribute))
            and (getattr(node, "id", None) or getattr(node, "attr", None))
            == CONTROL_MODEL
            and not in_read
        ):
            hit(node, "control model outside the locking read")
        if isinstance(node, ast.Call):
            callee = _terminal(node.func) or ""
            if callee in READ_CALLEES:
                callee = ""
            if (
                callee.lower().endswith(WRITE_SUFFIXES)
                or callee.startswith("on_conflict_")
            ) and (
                _names_target(node.func)
                or any(_names_target(arg) for arg in node.args)
                or any(_names_target(k.value) for k in node.keywords)
            ):
                hit(node, f"orm {callee}")
            if callee == "setattr" and any(_folded(arg) == COLUMN for arg in node.args):
                hit(node, "setattr")
            if any(keyword.arg == COLUMN for keyword in node.keywords):
                hit(node, "keyword")
            if in_read and (
                callee in WRITE_CALLS or callee.lower().endswith(WRITE_SUFFIXES)
            ):
                hit(node, "write in the locking read")
        if isinstance(node, ast.Dict) and any(
            key is not None and _folded(key) == COLUMN for key in node.keys
        ):
            hit(node, "dict key")
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for inner in ast.walk(target):
                    if isinstance(inner, ast.Attribute) and (
                        inner.attr == COLUMN or in_read
                    ):
                        hit(node, "assign")
        child_folded = is_text and _folded(node) is not None
        for child in ast.iter_child_nodes(node):
            visit(child, function, child_folded or (folded_parent and is_text))

    visit(tree, None, False)
    return found


def test_the_swept_set_includes_rollout_routes_and_repositories() -> None:
    swept = {path.relative_to(ROOT).as_posix() for path in _credit_modules()}
    assert {
        "src/services/credit_rollout.py",
        "src/repositories/credit.py",
        "src/api/routes/admin_credits.py",
        "src/services/credit_fact_consumer.py",
    } <= swept


def test_no_credit_module_writes_the_asaas_rollout_surfaces() -> None:
    offenders = [hit for path in _credit_modules() for hit in _writes(path)]
    assert offenders == []


@pytest.mark.parametrize(
    "source",
    [
        "SQL = \"UPDATE billing_rollout_control SET state = 'general'\"\n",
        'SQL = "insert into public.billing_topup_capability_receipts (id) values (1)"\n',
        'SQL = "DELETE FROM billing_rollout_control"\n',
        'SQL = "TRUNCATE TABLE billing_topup_capability_receipts"\n',
        'SQL = "UPDATE x SET topup_commercial_enabled = true"\n',
        # Concatenated and f-string table names.
        'SQL = "UPDATE " + "billing_rollout_" + "control SET state = 1"\n',
        'T = "billing_rollout_" + "control"\n',
        'SQL = f"UPDATE billing_rollout_control SET state = {s}"\n',
        "SQL = f\"UPDATE billing_{'rollout'}_control SET a = 1\"\n",
        # ORM statements, the repository's own idioms included.
        "stmt = update(BillingRolloutControl).values(state='general')\n",
        "stmt = insert(BillingTopupCapabilityReceipt)\n",
        "stmt = sa.delete(billing.BillingRolloutControl)\n",
        "stmt = insert(table('billing_rollout_control'))\n",
        "stmt = postgresql_insert(billing_models.BillingRolloutControl)\n",
        "stmt = billing_models.BillingRolloutControl.__table__.update()\n",
        "await s.execute(q(c.BillingRolloutControl).update({'state': 'g'}))\n",
        "stmt = pg_insert(t).on_conflict_do_update(set_={'topup_commercial_enabled': 1})\n",
        # Model construction and references.
        "session.add(BillingTopupCapabilityReceipt(id=1))\n",
        "from src.models.billing import BillingTopupCapabilityReceipt\nx = 1\n"
        "y = BillingTopupCapabilityReceipt\n",
        "async def load(s):\n    row = await s.get(BillingRolloutControl, 'test')\n"
        "    await s.delete(row)\n",
        "async def load(s):\n    row = await s.get(BillingRolloutControl, 'test')\n"
        "    row.state = 'general'\n",
        # Writes inside the locking read.
        "async def lock_rollout_control(self, environment):\n"
        "    row = await self.session.get(BillingRolloutControl, environment)\n"
        "    row.state = 'general'\n",
        "async def lock_rollout_control(self, environment):\n"
        "    await self.session.delete(BillingRolloutControl)\n",
        # The column.
        "row.topup_commercial_enabled = True\n",
        "setattr(row, 'topup_commercial_enabled', True)\n",
        "stmt = update(x).values(topup_commercial_enabled=True)\n",
        "stmt = update(x).values({'topup_commercial_enabled': True})\n",
    ],
)
def test_the_analysis_sees_every_write_form(tmp_path: Path, source: str) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(source, encoding="utf-8")
    assert _writes(probe)


def test_the_locking_read_of_the_control_row_is_allowed(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from src.models.billing import BillingRolloutControl\n"
        "async def lock_rollout_control(self, *, environment):\n"
        '    """Reads billing_rollout_control FOR UPDATE; never writes."""\n'
        "    await self.session.execute(select(func.pg_advisory_xact_lock(1)))\n"
        "    row = await self.session.scalar(\n"
        "        select(BillingRolloutControl.environment)\n"
        "        .where(BillingRolloutControl.environment == environment)\n"
        "        .with_for_update()\n"
        "    )\n"
        "description = 'refuses top-ups while topup_commercial_enabled is off'\n"
        "seen = control.topup_commercial_enabled\n",
        encoding="utf-8",
    )
    assert _writes(probe) == []


def test_rollout_has_no_secret_or_asaas_writer_dependency() -> None:
    path = ROOT / "src/services/credit_rollout.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "billing_rollout_control" not in names
    assert "topup_commercial_enabled" not in names
    assert "billing_topup_capability_receipts" not in names
    assert "secret_key" not in names
    assert not set(MODELS) & names
