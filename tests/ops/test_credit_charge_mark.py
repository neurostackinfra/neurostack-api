"""Proofs of ``python -m src.ops.credit_charge_mark`` (cobranca-credito-por-
builder: ``status`` from Group 1, the bridge release; ``mark`` and ``unmark``
from Group 4).

Without a database: the argument contract (every malformed invocation is
``unconfigured``/``usage`` before any session is opened, the ``--reason``
rules among them), the closed result vocabulary and exit codes, the
switch-off path doing no database work, the printable provider vocabulary and
the channel allowlist, the module's boundary (its name, what it never names
or imports), the AST fence of the marks table (one writer in ``src``, the
repository's ``insert_enforcement_mark``, whose one caller is this module,
which nothing in ``src`` imports) with probes proving each of its analyses
sees what it must, and the real ``python -m`` entry point printing exactly
one stdout line.

On SQLite (``create_all`` plus an ``alembic_version`` row, so the real switch
reads the revision): ``status`` with the marks table absent (046: the gate is
closed and the table is never touched), empty (047), and with mark and unmark
events inserted by SQL; the effective instant and its origin (global first,
then a latest ``mark``, else none); the linked tenants with and without a
provider; an unknown builder; and that no credential key or value other than
``whatsapp_provider`` reaches the output or the log, every statement being a
SELECT and every mention of ``api_credentials`` the one ``->>`` key read.
``tests/integration/test_credit_enforcement_marks_migration_postgres.py``
repeats ``status`` on PostgreSQL at 046 and at 047.

``mark`` and ``unmark`` on SQLite: the sequence ``mark -> unmark -> mark``
(one event each, strictly increasing ``occurred_at``, author and reason
stored, the request path's reader following it), every refusal writing
nothing (author, builder, the 047 gate, the global L10, already marked / not
marked, coverage, channel, window, open episode, open debt), and no
credential reaching the output.  ``tests/integration/
test_credit_charge_mark_postgres.py`` repeats them on PostgreSQL with the
locks.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import DateTime, String, bindparam, event, select, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import src.models  # noqa: F401  (registers every mapper for create_all)
from src.config.settings import settings
from src.models.base import Base
from src.models.builder import Builder
from src.models.clinic_config_table import ClinicConfigTable
from src.models.content_retention import CreditLifecycleEpisode
from src.models.credit import (
    ENFORCEMENT_STATE_ID,
    CreditAccount,
    CreditDebt,
    CreditEffect,
    CreditEnforcementState,
    CreditLedgerEntry,
    CreditLot,
)
from src.models.tenant import Tenant
from src.ops import credit_charge_mark as ops
from src.repositories.credit import BUILDER_TENANT_PROVIDERS_SQL
from src.repositories.database import _patch_jsonb_for_sqlite
from src.services import credit_switch

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "src" / "ops" / "credit_charge_mark.py"
BUILDER = UUID("4a2e1d5f-6b7c-4d8e-9f0a-1b2c3d4e5f60")
ACTOR = UUID("5b3f2e60-7c8d-4e9f-8a1b-2c3d4e5f6071")
T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
# Credential values a tenant may hold; none may ever be printed or logged.
SECRETS = (
    "zapi-token-NEVER-PRINT-71c9",
    "zapi-client-token-NEVER-PRINT-55aa",
    "meta-permanent-token-NEVER-PRINT-0b1e",
    "sk-llm-key-NEVER-PRINT-9f3d",
    "provider-value-that-is-a-secret-NEVER-PRINT",
)
CREDENTIAL_KEYS = (
    "zapi_instance_id",
    "zapi_token",
    "zapi_client_token",
    "meta_permanent_token",
    "llm_api_key",
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


def _status(builder: UUID = BUILDER) -> list[str]:
    return ["status", "--builder-id", str(builder)]


# ---------------------------------------------------------------------------
# Argument contract and result vocabulary.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--help"],
        ["status"],
        ["status", "--builder-id"],
        ["status", "--builder-id", str(BUILDER).upper()],
        ["status", "--builder-id", "{" + str(BUILDER) + "}"],
        ["status", "--builder-id", "none"],
        ["status", "--builder-id", str(BUILDER), "extra"],
        ["status", "--builder", str(BUILDER)],
        ["mark", "--builder-id", str(BUILDER)],
        ["unmark", "--builder-id", str(BUILDER)],
        ["mark", "--builder-id", str(BUILDER), "--author-user-id", str(ACTOR)],
        ["mark", "--builder-id", str(BUILDER), "--reason", "pilot"],
        ["mark", "--author-user-id", str(ACTOR), "--reason", "pilot"],
        [
            "unmark",
            "--builder-id",
            str(BUILDER),
            "--author-user-id",
            str(ACTOR).upper(),
            "--reason",
            "pilot",
        ],
        [
            "mark",
            "--builder-id",
            str(BUILDER),
            "--author-user-id",
            str(ACTOR),
            "--reason",
            "pilot",
            "extra",
        ],
        [
            "status",
            "--builder-id",
            str(BUILDER),
            "--author-user-id",
            str(ACTOR),
        ],
        ["arm", "--builder-id", str(BUILDER)],
    ],
    ids=lambda argv: " ".join(argv)[:60] or "empty",
)
async def test_a_malformed_invocation_is_usage_before_any_work(
    argv: list[str],
) -> None:
    code, document, _ = await _invoke(argv)
    assert code == ops.EXIT_CODES["unconfigured"] == 30
    assert document["verdict"] == "unconfigured"
    assert document["reasons"] == ["usage"]
    assert document["command"] is None


def _change_argv(
    command: str,
    builder: UUID = BUILDER,
    author: UUID = ACTOR,
    reason: str = "piloto de cobrança",
) -> list[str]:
    return [
        command,
        "--builder-id",
        str(builder),
        "--author-user-id",
        str(author),
        "--reason",
        reason,
    ]


def test_the_commands_are_status_mark_and_unmark() -> None:
    assert ops.COMMANDS == ("status", "mark", "unmark")
    assert dict(ops.SUCCESS) == {"status": "ok", "mark": "marked", "unmark": "unmarked"}
    args = ops.parse_arguments(_status())
    assert (args.command, args.builder_id) == ("status", BUILDER)
    for command in ("mark", "unmark"):
        args = ops.parse_arguments(_change_argv(command, reason="  piloto  "))
        assert (args.command, args.builder_id, args.author_user_id, args.reason) == (
            command,
            BUILDER,
            ACTOR,
            "piloto",
        )


@pytest.mark.parametrize(
    "reason",
    [
        "",
        "   ",
        "\t",
        "x" * 501,
        " " + "x" * 501 + " ",
        "linha um\nlinha dois",
        "with a tab\tinside",
        "carriage\rreturn",
        "escape \x1b[31m red",
        "nul \x00 byte",
        "zero\u200bwidth",
        "bidi \u202e override",
        "line\u2028separator",
        "del \x7f char",
        "no\xa0break",
        # LOW-1: no e-mail-like personal data, at least one letter or digit,
        # no printable invisible filler.
        "pedido de joao.silva@clinica.com.br",
        "contato: a@b.co",
        "x@y.z",
        "\u3164",
        "\u3164\u3164\u3164",
        "\u2800\u2800",
        "\u115f",
        "\uffa0 \uffa0",
        "piloto\u3164",
        "--- !!! ...",
        "-",
        "(  )",
    ],
    ids=repr,
)
async def test_a_bad_reason_is_usage_before_any_work(reason: str) -> None:
    for command in ("mark", "unmark"):
        code, document, _ = await _invoke(_change_argv(command, reason=reason))
        assert code == ops.EXIT_CODES["unconfigured"]
        assert (document["command"], document["reasons"]) == (None, ["usage"])


@pytest.mark.parametrize(
    "reason",
    [
        "x",
        "7",
        "x" * 500,
        "  " + "y" * 500 + "  ",
        "piloto de cobrança: ação nº 1 (çãé)",
        "ticket #42 @ suporte",
        "R$ 5,00 de cortesia -- teste",
    ],
    ids=lambda value: f"{len(value)} chars",
)
def test_a_good_reason_is_kept_trimmed(reason: str) -> None:
    args = ops.parse_arguments(_change_argv("mark", reason=reason))
    assert args.reason == reason.strip()
    assert len(args.reason) <= ops.REASON_MAX_LENGTH


def test_the_reason_bound_is_the_column_bound() -> None:
    from src.models.credit import ENFORCEMENT_MARK_REASON_MAX_LENGTH

    assert ops.REASON_MAX_LENGTH == ENFORCEMENT_MARK_REASON_MAX_LENGTH == 500


def test_the_exit_codes_are_the_published_literals() -> None:
    assert dict(ops.EXIT_CODES) == {
        "ok": 0,
        "marked": 0,
        "unmarked": 0,
        "refused": 20,
        "switch_off": 21,
        "unconfigured": 30,
        "error": 50,
    }


def test_the_result_vocabulary_is_closed() -> None:
    with pytest.raises(ValueError, match="verdict"):
        ops.RunResult(command="status", verdict="armed")
    with pytest.raises(ValueError, match="command"):
        ops.RunResult(command="arm", verdict="refused")
    with pytest.raises(ValueError, match="success"):
        ops.RunResult(command="mark", verdict="ok")
    with pytest.raises(ValueError, match="success"):
        ops.RunResult(command="unmark", verdict="marked")
    with pytest.raises(ValueError, match="success"):
        ops.RunResult(command="status", verdict="unmarked")
    with pytest.raises(ValueError, match="reason"):
        ops.RunResult(command="status", verdict="refused", reasons=("other",))
    with pytest.raises(ValueError, match="success"):
        ops.RunResult(command=None, verdict="ok")


@pytest.mark.parametrize(
    "argv",
    [_status(), _change_argv("mark"), _change_argv("unmark")],
    ids=lambda argv: argv[0],
)
async def test_with_the_configuration_off_nothing_reaches_the_database(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    monkeypatch.setattr(
        credit_switch._SWITCH, "_reading", credit_switch._SWITCH._reading
    )
    monkeypatch.setattr(settings, "credit_hot_path_enabled", False)
    code, document, _ = await _invoke(argv)
    assert code == ops.EXIT_CODES["switch_off"] == 21
    assert document["verdict"] == "switch_off"
    assert document["reasons"] == ["credit_switch_off"]
    assert document["switch"] == {
        "effective": False,
        "reason": "config_disabled",
        "revision": None,
    }
    assert document["marks"] is None and document["tenants"] is None


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


@pytest.mark.parametrize(
    ("raw", "allowed"),
    [
        (None, True),
        ("zapi", True),
        ("meta_cloud", True),
        ("uazapi", False),
        ("evolution", False),
        ("dialog360", False),
        ("twilio", False),
        ("meta", False),
        ("ZAPI", False),
        (" zapi", False),
        ("", False),
        (SECRETS[-1], False),
    ],
)
def test_the_channel_allowlist_is_zapi_meta_cloud_or_none(
    raw: str | None, allowed: bool
) -> None:
    assert ops.ALLOWED_WHATSAPP_PROVIDERS == frozenset({"zapi", "meta_cloud"})
    assert ops.channel_allowed(raw) is allowed


def test_the_enforcement_origin_is_global_then_mark_then_none() -> None:
    """PR-B: the rule (global, then a latest ``mark``, else none) is
    ``CreditRepository.effective_enforcement``'s -- the request path's own
    reader, proven against the table by the status runs below and by
    tests/integration/test_credit_effective_enforcement_postgres.py --; the
    command only prints its verdict."""

    from src.repositories.credit import EffectiveEnforcement

    later = T0 + timedelta(hours=1)
    none = EffectiveEnforcement(None, None, builder_id=BUILDER)
    assert ops.enforcement_document(none)["origin"] == "none"
    assert ops.enforcement_document(none) == {
        "global_at": None,
        "effective_at": None,
        "origin": "none",
    }
    assert ops.enforcement_document(EffectiveEnforcement(later, "mark")) == {
        "global_at": None,
        "effective_at": later.isoformat(),
        "origin": "mark",
    }
    assert ops.enforcement_document(EffectiveEnforcement(T0, "global")) == {
        "global_at": T0.isoformat(),
        "effective_at": T0.isoformat(),
        "origin": "global",
    }


@pytest.mark.parametrize("state", ["charged", "blocked"])
def test_the_courtesy_origin_prints_its_state(state: str) -> None:
    """debito-cortesia-automatico (DESIGN item 13): under the courtesy rule
    the origin is ``courtesy``, at the first grant, with the rule's state;
    no other origin prints a state.  The vocabularies are closed."""

    from src.repositories.credit import EffectiveEnforcement

    assert ops.ORIGINS == ("global", "mark", "courtesy", "none")
    assert ops.COURTESY_STATES == ("charged", "blocked")
    rule = EffectiveEnforcement(T0, "courtesy", builder_id=BUILDER, state=state)  # type: ignore[arg-type]
    assert ops.enforcement_document(rule) == {
        "global_at": None,
        "effective_at": T0.isoformat(),
        "origin": "courtesy",
        "state": state,
    }
    # A state never leaks into another origin's document, which keeps the
    # three keys it had before the rule (dominance of the printed output).
    marked = EffectiveEnforcement(T0, "mark", state=state)  # type: ignore[arg-type]
    assert set(ops.enforcement_document(marked)) == {
        "global_at",
        "effective_at",
        "origin",
    }
    with pytest.raises(ValueError, match="courtesy state"):
        ops.enforcement_document(EffectiveEnforcement(T0, "courtesy"))
    with pytest.raises(ValueError, match="origin"):
        ops.enforcement_document(EffectiveEnforcement(T0, "other"))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The module's boundary (no database).
# ---------------------------------------------------------------------------


def test_the_module_never_names_the_arming_task_or_its_writers() -> None:
    """The L10 fence (tests/integration/test_credit_enforcement_rollout_fence.py)
    stays unchanged: this module is not named like the arming task, never
    names its writers or its module, and builds no route."""

    assert "credit_enforcement" not in MODULE.name
    source = MODULE.read_text(encoding="utf-8")
    for forbidden in ("arm_enforcement", "accept_cutoff", "src.ops.credit_enforcement"):
        assert forbidden not in source, forbidden
    tree = ast.parse(source)
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


# ---------------------------------------------------------------------------
# The AST fence of the marks table (Group 4; replaces the bridge's text fence).
#
# * only three files name the table in code: the model's
#   (``src/models/credit.py``), the package export (``src/models/__init__.py``)
#   and the repository (``src/repositories/credit.py``).  "Naming" is the model
#   name ``CreditEnforcementMark`` (a name, an attribute or an import) or a
#   string that contains ``credit_enforcement_marks``.  The repository's
#   reader methods (``effective_enforcement``, ``marked_builders``,
#   ``latest_enforcement_mark(s)``...) and the 047 gate are what everyone
#   else calls, and they are not on this list;
# * exactly ONE writer of ``credit_enforcement_marks`` exists in ``src`` (and
#   none in ``scripts``): the repository's
#   ``CreditRepository.insert_enforcement_mark``.  A write is an ORM
#   construction of the model (also through an import alias or a local name
#   bound to it), a DML call on it -- any callee named like ``insert``,
#   ``_insert``, ``pg_insert``, ``update``, ``delete``, ``merge``, ``upsert``
#   or ``bulk_*`` -- on the model, its ``__table__``, a ``table(...)`` of
#   that name or ``metadata.tables[...]`` of it, or a SQL string that
#   inserts into, updates, deletes from, merges into or copies into the
#   table: a literal, or one built with ``+``, an f-string, ``.format`` or
#   ``%`` from literals, local names bound to literals or ``__tablename__``;
# * that writer's ONLY caller is this module: any reference to the name (an
#   attribute, a bare name, a ``getattr`` string) elsewhere in ``src`` is a
#   finding, and the module references it exactly once;
# * nothing in ``src`` imports this module, in any form (so no route, worker
#   or script reaches it: it runs only as ``python -m``);
# * the module stays outside the L10 fence: it never names the arming task's
#   writers or module, and its name has no ``credit_enforcement`` prefix.
#
# Docstrings (the owners documenting their one caller) are prose, not code.
# ---------------------------------------------------------------------------

_REPOSITORY = "src/repositories/credit.py"
_COMMAND = "src/ops/credit_charge_mark.py"
_COMMAND_MODULE = "src.ops.credit_charge_mark"
_MARK_MODEL = "CreditEnforcementMark"
_WRITER = "insert_enforcement_mark"
_WRITER_SCOPE = "CreditRepository.insert_enforcement_mark"
_TABLE = "credit_enforcement_marks"
_WRITE_SQL = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO|COPY)\s+"
    r'(?:"?\w+"?\.)?"?credit_enforcement_marks\b',
    re.IGNORECASE,
)
_DML = re.compile(r"(?i)(^|_)(insert|update|delete|merge|upsert)$|^bulk_")
# The only files that may name the model or the table in code.
_NAMING_FILES = frozenset(
    {"src/models/credit.py", "src/models/__init__.py", "src/repositories/credit.py"}
)


def _sources(root: Path = ROOT) -> list[Path]:
    return sorted((root / "src").rglob("*.py"))


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _docstring_ids(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                found.add(id(body[0].value))
    return found


def _folded(node: ast.AST) -> str | None:
    """A string literal, an f-string's literal parts, or a ``+`` of them."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            (
                part.value
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
                else "{}"
            )
            for part in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _folded(node.left), _folded(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _scoped(tree: ast.AST) -> list[tuple[str, ast.AST]]:
    """Every node with the qualified name of its enclosing class/function."""

    out: list[tuple[str, ast.AST]] = []

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        out.append((".".join(scope), node))
        inner = scope
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            inner = (*scope, node.name)
        for child in ast.iter_child_nodes(node):
            visit(child, inner)

    visit(tree, ())
    return out


@dataclass
class _Bindings:
    """What a module binds by name: every simple assignment's value (the
    last one wins) and the names that stand for the model or its table."""

    values: dict[str, ast.AST]
    models: set[str]


def _bindings(tree: ast.AST) -> _Bindings:
    values: dict[str, ast.AST] = {}
    models: set[str] = {_MARK_MODEL}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name):
                values[node.target.id] = node.value
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name.split(".")[-1] == _MARK_MODEL and alias.asname:
                    models.add(alias.asname)
    bound = _Bindings(values=values, models=models)
    changed = True
    while changed:  # ``A = CreditEnforcementMark``, ``B = A.__table__`` ...
        changed = False
        for name, value in values.items():
            if name not in bound.models and _names_model(value, bound):
                bound.models.add(name)
                changed = True
    return bound


def _names_model(node: ast.AST, bound: _Bindings | None = None) -> bool:
    """The model or its table: ``CreditEnforcementMark`` (or a name bound to
    it), ``models.CreditEnforcementMark``, its ``__table__``, a
    ``table("credit_enforcement_marks", ...)`` and
    ``metadata.tables["credit_enforcement_marks"]``."""

    models = {_MARK_MODEL} if bound is None else bound.models
    if isinstance(node, ast.Name):
        return node.id in models
    if isinstance(node, ast.Attribute):
        return node.attr == _MARK_MODEL or (
            node.attr == "__table__" and _names_model(node.value, bound)
        )
    if isinstance(node, ast.Call) and _callee(node) in {"table", "Table"}:
        return bool(node.args) and _fold_sql(node.args[0], bound) == _TABLE
    if isinstance(node, ast.Subscript):
        return (
            isinstance(node.value, ast.Attribute)
            and node.value.attr == "tables"
            and _TABLE in (_fold_sql(node.slice, bound) or "")
        )
    return False


def _callee(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _fold_sql(node: ast.AST, bound: _Bindings | None, depth: int = 0) -> str | None:
    """The text a string expression can produce, as far as the module shows
    it: literals, names bound to them, ``__tablename__`` (the table), and
    ``+``, f-strings, ``.format`` and ``%`` of those; an unknown part is
    ``{}``.  ``None``: not a string expression at all."""

    if depth > 8:
        return None
    again = depth + 1
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        if bound is not None and node.id in bound.values:
            return _fold_sql(bound.values[node.id], bound, again)
        return None
    if isinstance(node, ast.Attribute):
        if node.attr == "__tablename__" or (
            node.attr == "name" and _names_model(node.value, bound)
        ):
            return _TABLE
        return None
    if isinstance(node, ast.JoinedStr):
        parts = []
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                parts.append(_fold_sql(part.value, bound, again) or "{}")
            else:
                parts.append(_fold_sql(part, bound, again) or "")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _fold_sql(node.left, bound, again)
        right = _fold_sql(node.right, bound, again)
        if left is None and right is None:
            return None
        return (left or "{}") + (right or "{}")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        template_ = _fold_sql(node.left, bound, again)
        if template_ is None:
            return None
        values = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
        if isinstance(node.right, ast.Dict):
            values = [value for value in node.right.values if value is not None]
        filled = iter([_fold_sql(value, bound, again) or "{}" for value in values])
        return re.sub(r"%(?:\([^)]*\))?s", lambda _m: next(filled, "{}"), template_)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        template_ = _fold_sql(node.func.value, bound, again)
        if template_ is None:
            return None
        positional = [_fold_sql(arg, bound, again) or "{}" for arg in node.args]
        named = {
            keyword.arg: _fold_sql(keyword.value, bound, again) or "{}"
            for keyword in node.keywords
            if keyword.arg is not None
        }
        counter = iter(range(len(positional)))

        def fill(match: re.Match[str]) -> str:
            field_ = match.group(1).split(":")[0].split("!")[0]
            if field_ in named:
                return named[field_]
            index = int(field_) if field_.isdigit() else next(counter, -1)
            return positional[index] if 0 <= index < len(positional) else "{}"

        return re.sub(r"\{([^{}]*)\}", fill, template_)
    return None


def _mark_writes(path: Path, root: Path) -> list[tuple[str, str, int]]:
    """Every write of the marks table in ``path``: ``(file, scope, line)``."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = _docstring_ids(tree)
    bound = _bindings(tree)
    relative = _relative(path, root)
    found: set[tuple[str, str, int]] = set()
    for scope, node in _scoped(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Call):
            callee = _callee(node) or ""
            arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
            if (
                isinstance(node.func, (ast.Name, ast.Attribute))
                and _names_model(node.func, bound)
                and not (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "__table__"
                )
            ):
                found.add((relative, scope, line))  # an ORM construction
            elif _DML.search(callee) and any(
                _names_model(argument, bound) for argument in arguments
            ):
                found.add((relative, scope, line))  # insert(model), _insert ...
            elif (
                isinstance(node.func, ast.Attribute)
                and _DML.search(node.func.attr)
                and _names_model(node.func.value, bound)
            ):
                found.add((relative, scope, line))  # table.insert()
        if id(node) not in docstrings and isinstance(
            node, (ast.Constant, ast.JoinedStr, ast.BinOp, ast.Call)
        ):
            folded = _fold_sql(node, bound)
            if folded is not None and _WRITE_SQL.search(folded):
                found.add((relative, scope, line))
    return sorted(found)


def _naming_sites(path: Path, root: Path) -> list[tuple[str, int]]:
    """Every place ``path`` names the model or the table in CODE (docstrings
    and comments are prose): a name, an attribute or an import of
    ``CreditEnforcementMark``, or a string containing the table name or the
    model name (``getattr(models, "CreditEnforcementMark")``)."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = _docstring_ids(tree)
    bound = _bindings(tree)
    relative = _relative(path, root)
    found: set[tuple[str, int]] = set()
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if (
            (isinstance(node, ast.Name) and node.id == _MARK_MODEL)
            or (isinstance(node, ast.Attribute) and node.attr == _MARK_MODEL)
            or (
                isinstance(node, ast.alias)
                and _MARK_MODEL in (node.name.split(".")[-1], node.asname)
            )
        ):
            found.add((relative, line))
        elif id(node) not in docstrings and isinstance(
            node, (ast.Constant, ast.JoinedStr, ast.BinOp)
        ):
            folded = _fold_sql(node, bound)
            if folded is not None and (_TABLE in folded or _MARK_MODEL in folded):
                found.add((relative, line))
    return sorted(found)


def _writer_references(path: Path, root: Path) -> list[tuple[str, int]]:
    """Every reference to the writer in ``path`` other than its definition:
    an attribute, a bare name (an imported alias) or a string (``getattr``)."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = _docstring_ids(tree)
    relative = _relative(path, root)
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if (
            (isinstance(node, ast.Attribute) and node.attr == _WRITER)
            or (isinstance(node, ast.Name) and node.id == _WRITER)
            or (isinstance(node, ast.alias) and _WRITER in (node.name, node.asname))
        ):
            found.append((relative, getattr(node, "lineno", 0)))
        elif id(node) not in docstrings and isinstance(
            node, (ast.Constant, ast.JoinedStr, ast.BinOp)
        ):
            folded = _folded(node)
            if folded is not None and _WRITER in folded and node is not None:
                # A ``+`` is seen once, not again through its literal parts.
                found.append((relative, getattr(node, "lineno", 0)))
    return sorted(set(found))


def _imports_command(path: Path, root: Path) -> bool:
    """An import of this module in any form; prose in docstrings is not."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = ".".join(Path(_relative(path, root)).with_suffix("").parts[:-1])
    docstrings = _docstring_ids(tree)
    for node in ast.walk(tree):
        if id(node) in docstrings:
            continue
        if isinstance(node, ast.Import) and any(
            alias.name.startswith(_COMMAND_MODULE) for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                parts = package.split(".")
                base = parts[: len(parts) - (node.level - 1)]
                module = ".".join([*base, *([module] if module else [])])
            if module.startswith(_COMMAND_MODULE) or (
                module == "src.ops"
                and any(alias.name == "credit_charge_mark" for alias in node.names)
            ):
                return True
        if isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp)):
            folded = _folded(node)
            if folded is not None and (
                _COMMAND_MODULE in folded or "src/ops/credit_charge_mark" in folded
            ):
                return True
    return False


def _fence_findings(paths: list[Path], root: Path) -> list[str]:
    """Everything the fence refuses among ``paths`` (empty: the fence holds)."""

    findings: list[str] = []
    for path in paths:
        if _relative(path, root) not in _NAMING_FILES:
            findings.extend(f"naming {site}" for site in _naming_sites(path, root))
    writes = [site for path in paths for site in _mark_writes(path, root)]
    allowed = [
        site for site in writes if (site[0], site[1]) == (_REPOSITORY, _WRITER_SCOPE)
    ]
    findings.extend(f"writer {site}" for site in writes if site not in allowed)
    if len(allowed) != 1:
        findings.append(f"the repository writer has {len(allowed)} write sites")
    for path in paths:
        relative = _relative(path, root)
        references = _writer_references(path, root)
        if relative == _COMMAND:
            if len(references) != 1:
                findings.append(f"the command references the writer {references}")
        else:
            findings.extend(f"caller {site}" for site in references)
        if relative != _COMMAND and _imports_command(path, root):
            findings.append(f"importer {relative}")
    return findings


def test_the_fence_holds_over_src() -> None:
    """Success Criterion 8: three files name the table, one writer, one
    caller, no importer."""

    assert _fence_findings(_sources(), ROOT) == []


def test_only_the_three_owner_files_name_the_table_in_code() -> None:
    """Not vacuous: each of the three really names it, nothing else does."""

    named = {_relative(path, ROOT) for path in _sources() if _naming_sites(path, ROOT)}
    assert named == _NAMING_FILES


def test_no_script_writes_the_marks() -> None:
    """``scripts``: no Python writer and no SQL file that writes the table
    (the 047 pre- and post-reads only read it)."""

    scripts = ROOT / "scripts"
    python = sorted(scripts.rglob("*.py"))
    sql = sorted(scripts.rglob("*.sql"))
    assert python and sql
    assert [site for path in python for site in _mark_writes(path, ROOT)] == []
    for path in sql:
        assert _WRITE_SQL.search(path.read_text(encoding="utf-8")) is None, path
    assert any(_TABLE in path.read_text(encoding="utf-8") for path in sql)


def test_the_fence_is_not_vacuous() -> None:
    """The allowances are real: the writer is the one in the repository
    method, the command calls it exactly once, and the sweep covers every
    package of ``src`` that could reach the table."""

    writes = [site for path in _sources() for site in _mark_writes(path, ROOT)]
    assert [(file, scope) for file, scope, _ in writes] == [
        (_REPOSITORY, _WRITER_SCOPE)
    ]
    assert len(_writer_references(MODULE, ROOT)) == 1
    (call,) = [
        node
        for node in ast.walk(ast.parse(MODULE.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == _WRITER
    ]
    assert {keyword.arg for keyword in call.keywords} == {
        "builder_id",
        "kind",
        "actor_user_id",
        "reason",
        "marks_readable",
    }
    swept = {_relative(path, ROOT) for path in _sources()}
    for package in (
        "src/api/routes/",
        "src/services/",
        "src/ops/",
        "src/repositories/",
        "src/models/",
    ):
        assert any(name.startswith(package) for name in swept), package


_OTHER = "src/services/other.py"


@pytest.mark.parametrize(
    ("relative", "source"),
    [
        (_OTHER, "def f(s):\n    s.add(CreditEnforcementMark(kind='mark'))\n"),
        (_OTHER, "def f(m):\n    return m.CreditEnforcementMark(kind='mark')\n"),
        (_OTHER, "def f():\n    return insert(CreditEnforcementMark)\n"),
        (_OTHER, "def f():\n    return sa.update(CreditEnforcementMark)\n"),
        (_OTHER, "def f():\n    return delete(CreditEnforcementMark)\n"),
        (_OTHER, "def f():\n    return CreditEnforcementMark.__table__.insert()\n"),
        (_OTHER, "def f():\n    return insert(CreditEnforcementMark.__table__)\n"),
        (_OTHER, "Q = 'INSERT INTO credit_enforcement_marks (id) VALUES (1)'\n"),
        (_OTHER, "Q = 'insert  into public.credit_enforcement_marks VALUES (1)'\n"),
        (_OTHER, "Q = 'UPDATE credit_enforcement_marks SET reason = 1'\n"),
        (_OTHER, "Q = 'DELETE FROM credit_enforcement_marks'\n"),
        (_OTHER, "Q = 'INSERT INTO ' + 'credit_enforcement_marks VALUES (1)'\n"),
        (_OTHER, "T = 'x'\nQ = f'INSERT INTO credit_enforcement_marks {T}'\n"),
        # The forms the Group 4 execution review showed escaping.
        (
            _OTHER,
            "from src.models.credit import CreditEnforcementMark as M\n"
            "def f(s):\n    s.add(M(kind='mark'))\n",
        ),
        (
            _OTHER,
            "import src.models.credit as c\n"
            "def f(s):\n    s.add(c.CreditEnforcementMark(kind='mark'))\n",
        ),
        (_OTHER, "Mark = CreditEnforcementMark\ndef f():\n    return insert(Mark)\n"),
        (
            _OTHER,
            "def f():\n    Mark = CreditEnforcementMark\n    return insert(Mark)\n",
        ),
        (
            _OTHER,
            "T = CreditEnforcementMark.__table__\ndef f():\n    return T.insert()\n",
        ),
        (
            _REPOSITORY,
            "class CreditRepository:\n"
            "    async def insert_enforcement_mark(self):\n"
            "        return insert(CreditEnforcementMark)\n"
            "    async def other(self):\n"
            "        return self._insert(CreditEnforcementMark)\n",
        ),
        (
            _REPOSITORY,
            "class CreditRepository:\n"
            "    async def insert_enforcement_mark(self):\n"
            "        return insert(CreditEnforcementMark)\n"
            "    async def other(self):\n"
            "        model = CreditEnforcementMark\n"
            "        return self._insert(model).values(kind='mark')\n",
        ),
        (_OTHER, "def f():\n    return pg_insert(CreditEnforcementMark)\n"),
        (_OTHER, "def f():\n    return postgresql_insert(CreditEnforcementMark)\n"),
        (_OTHER, "def f():\n    return sqlite_insert(CreditEnforcementMark)\n"),
        (
            _OTHER,
            "def f():\n    return sa.dialects.postgresql.insert(CreditEnforcementMark)\n",
        ),
        (
            _OTHER,
            "def f(s, rows):\n    s.bulk_insert_mappings(CreditEnforcementMark, rows)\n",
        ),
        (
            _OTHER,
            "def f(s, rows):\n    s.bulk_save_objects(rows, mapper=CreditEnforcementMark)\n",
        ),
        (_OTHER, "def f(s, m):\n    return s.merge(CreditEnforcementMark(**m))\n"),
        (_OTHER, "def f():\n    return upsert(CreditEnforcementMark)\n"),
        (
            _OTHER,
            "VERB = 'INSERT INTO '\nQ = VERB + 'credit_enforcement_marks VALUES (1)'\n",
        ),
        (_OTHER, "T = 'credit_enforcement_marks'\nQ = 'INSERT INTO ' + T\n"),
        (_OTHER, "T = 'credit_enforcement_marks'\nQ = f'INSERT INTO {T} VALUES (1)'\n"),
        (
            _OTHER,
            "Q = 'INSERT INTO {} VALUES (1)'.format('credit_enforcement_marks')\n",
        ),
        (
            _OTHER,
            "Q = 'INSERT INTO {t} (id)'.format(t=CreditEnforcementMark.__tablename__)\n",
        ),
        (_OTHER, "Q = 'DELETE FROM %s' % CreditEnforcementMark.__tablename__\n"),
        (_OTHER, "Q = 'DELETE FROM %s WHERE %s' % ('credit_enforcement_marks', 'x')\n"),
        (
            _OTHER,
            "Q = f'UPDATE {CreditEnforcementMark.__tablename__} SET reason = 1'\n",
        ),
        (_OTHER, "Q = f'UPDATE {Model.__tablename__} SET reason = 1'\n"),
        (_OTHER, "def f():\n    return insert(sa.table('credit_enforcement_marks'))\n"),
        (
            _OTHER,
            "def f():\n    t = sa.table('credit_enforcement_marks', sa.column('id'))\n"
            "    return t.insert()\n",
        ),
        (
            _OTHER,
            "def f(md):\n    return md.tables['credit_enforcement_marks'].insert()\n",
        ),
        (
            _OTHER,
            "def f(md):\n    return delete(md.tables['public.credit_enforcement_marks'])\n",
        ),
        (_OTHER, "Q = 'MERGE INTO credit_enforcement_marks USING x ON true'\n"),
        (_OTHER, "Q = 'COPY credit_enforcement_marks FROM STDIN'\n"),
        (
            _OTHER,
            'Q = \'INSERT INTO "public"."credit_enforcement_marks" VALUES (1)\'\n',
        ),
        (
            _REPOSITORY,
            "class CreditRepository:\n"
            "    async def insert_enforcement_mark(self):\n"
            "        return insert(CreditEnforcementMark)\n"
            "    async def other(self):\n"
            "        return insert(CreditEnforcementMark)\n",
        ),
        (
            _REPOSITORY,
            "class CreditRepository:\n"
            "    async def insert_enforcement_mark(self):\n"
            "        return insert(CreditEnforcementMark)\n"
            "def insert_enforcement_mark():\n"
            "    return insert(CreditEnforcementMark)\n",
        ),
        (_COMMAND, "def f():\n    return insert(CreditEnforcementMark)\n"),
    ],
)
def test_the_writer_analysis_sees_every_write_form(
    tmp_path: Path, relative: str, source: str
) -> None:
    probe = tmp_path / relative
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(source, encoding="utf-8")
    assert _mark_writes(probe, tmp_path)
    assert [
        finding
        for finding in _fence_findings([probe], tmp_path)
        if finding.startswith(f"writer ('{relative}'")
    ], source


@pytest.mark.parametrize(
    "source",
    [
        "def f():\n    return select(CreditEnforcementMark.kind)\n",
        "def f(m):\n    return m.kind == CreditEnforcementMark.kind\n",
        "class CreditEnforcementMark(Base):\n    pass\n",
        "Q = 'SELECT kind FROM credit_enforcement_marks'\n",
        'def f():\n    """INSERT INTO credit_enforcement_marks, in prose."""\n',
        "Q = 'INSERT INTO credit_enforcement_marks_archive VALUES (1)'\n",
    ],
)
def test_a_read_or_prose_is_not_a_write(tmp_path: Path, source: str) -> None:
    probe = tmp_path / _OTHER
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(source, encoding="utf-8")
    assert _mark_writes(probe, tmp_path) == []


@pytest.mark.parametrize(
    "source",
    [
        "def f():\n    return select(CreditEnforcementMark.kind)\n",
        "from src.models import CreditEnforcementMark\n",
        "from src.models.credit import CreditEnforcementMark as M\n",
        "import src.models.credit as c\nX = c.CreditEnforcementMark\n",
        "Q = 'SELECT kind FROM credit_enforcement_marks'\n",
        "Q = 'SELECT kind FROM credit_' + 'enforcement_marks'\n",
        "T = 'credit_enforcement_marks'\n",
        "import src.models as models\n"
        "def f(s):\n    s.add(getattr(models, 'CreditEnforcementMark')(kind='mark'))\n",
        "def f(m):\n    return getattr(m, 'CreditEnforcement' + 'Mark')\n",
        "NAME = 'CreditEnforcementMark'\n",
    ],
)
def test_naming_the_model_or_the_table_elsewhere_is_a_finding(
    tmp_path: Path, source: str
) -> None:
    """Reads included: outside the three owner files nothing names them."""

    for relative in (_OTHER, _COMMAND, "src/api/routes/admin.py"):
        root = tmp_path / relative.replace("/", "_")
        probe = root / relative
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text(source, encoding="utf-8")
        assert any(
            finding.startswith(f"naming ('{relative}'")
            for finding in _fence_findings([probe], root)
        ), (relative, source)
    for relative in sorted(_NAMING_FILES):
        root = tmp_path / ("owner_" + relative.replace("/", "_"))
        probe = root / relative
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text(source, encoding="utf-8")
        assert not any(
            finding.startswith("naming") for finding in _fence_findings([probe], root)
        ), (relative, source)


@pytest.mark.parametrize(
    "source",
    [
        "async def f(repo):\n    return await repo.marked_builders()\n",
        "async def f(repo, ids):\n    return await repo.latest_enforcement_marks(ids)\n",
        "async def f(repo, b):\n    return await repo.latest_enforcement_mark(b)\n",
        "async def f(repo, b):\n    return await repo.effective_enforcement(b)\n",
        "async def f(repo):\n    return await repo.any_enforcement_mark()\n",
        "async def f(repo, b, at):\n    return await repo.mark_window_state(b, at=at)\n",
        "from src.services.credit_switch import enforcement_mark_gate_open\n"
        "def f(r):\n    return enforcement_mark_gate_open(r)\n",
        "from src.services.credit_switch import ENFORCEMENT_MARK_SCHEMA_MIN_REVISION\n",
        'def f():\n    """Reads ``credit_enforcement_marks`` (CreditEnforcementMark)."""\n',
        "# CreditEnforcementMark, credit_enforcement_marks: a comment\nX = 1\n",
        # Neighbouring names are not the model or the table.
        "import src.models as models\n"
        "def f():\n    return getattr(models, 'CreditEnforcementState')\n",
        "X = 'EnforcementMarkRow'\nY = 'credit_enforcement_state'\n",
    ],
)
def test_a_reader_of_the_repository_is_not_a_finding(
    tmp_path: Path, source: str
) -> None:
    """Groups 3 and 5 call the repository's readers and the 047 gate:
    those calls, prose and comments are not findings."""

    for relative in (_OTHER, "src/api/routes/admin.py", "src/services/credit_x.py"):
        root = tmp_path / relative.replace("/", "_")
        probe = root / relative
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text(source, encoding="utf-8")
        findings = [
            finding
            for finding in _fence_findings([probe], root)
            if not finding.startswith("the repository writer has")
        ]
        assert findings == [], (relative, source)


@pytest.mark.parametrize(
    "source",
    [
        "async def f(r):\n    await r.insert_enforcement_mark(kind='mark')\n",
        "def f(r):\n    return getattr(r, 'insert_enforcement_mark')\n",
        "def f(r):\n    return getattr(r, 'insert_' + 'enforcement_mark')\n",
        "from src.repositories.credit import insert_enforcement_mark\n",
        "def f(r):\n    w = r.insert_enforcement_mark\n    return w\n",
    ],
)
def test_the_caller_analysis_sees_every_reference_form(
    tmp_path: Path, source: str
) -> None:
    for relative in (_OTHER, "src/api/routes/credit_marks.py", "src/ops/other.py"):
        root = tmp_path / relative.replace("/", "_")
        probe = root / relative
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text(source, encoding="utf-8")
        assert any(
            finding.startswith("caller") for finding in _fence_findings([probe], root)
        ), (relative, source)


def test_the_command_may_reference_the_writer_only_once(tmp_path: Path) -> None:
    probe = tmp_path / _COMMAND
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(
        "async def f(r):\n    await r.insert_enforcement_mark()\n", encoding="utf-8"
    )
    assert not [f for f in _fence_findings([probe], tmp_path) if "references" in f]
    probe.write_text(
        "async def f(r):\n    await r.insert_enforcement_mark()\n"
        "async def g(r):\n    await r.insert_enforcement_mark()\n",
        encoding="utf-8",
    )
    assert [f for f in _fence_findings([probe], tmp_path) if "references" in f]


@pytest.mark.parametrize(
    ("relative", "source"),
    [
        ("src/api/routes/marks.py", "import src.ops.credit_charge_mark\n"),
        ("src/api/routes/marks.py", "import src.ops.credit_charge_mark as ops\n"),
        ("src/api/routes/marks.py", "from src.ops import credit_charge_mark\n"),
        ("src/api/routes/marks.py", "from src.ops.credit_charge_mark import run\n"),
        ("src/workers/marks.py", "from ..ops import credit_charge_mark\n"),
        ("src/workers/marks.py", "from ..ops.credit_charge_mark import amain\n"),
        ("src/ops/other.py", "from . import credit_charge_mark\n"),
        ("src/ops/other.py", "from .credit_charge_mark import main\n"),
        (
            "src/main.py",
            "import importlib\n"
            "m = importlib.import_module('src.ops.' + 'credit_charge_mark')\n",
        ),
        ("src/main.py", "CMD = ['python', '-m', 'src.ops.credit_charge_mark']\n"),
    ],
)
def test_the_import_analysis_sees_every_form(
    tmp_path: Path, relative: str, source: str
) -> None:
    probe = tmp_path / relative
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(source, encoding="utf-8")
    assert _fence_findings([probe], tmp_path)[-1] == f"importer {relative}"


def test_a_docstring_naming_the_command_is_not_an_import(tmp_path: Path) -> None:
    probe = tmp_path / _OTHER
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(
        '"""Written only by ``src.ops.credit_charge_mark``."""\n'
        "def f():\n"
        '    """Its one caller is ``src.ops.credit_charge_mark``'
        ' (``insert_enforcement_mark``)."""\n',
        encoding="utf-8",
    )
    assert not _imports_command(probe, tmp_path)
    assert _writer_references(probe, tmp_path) == []


def test_the_provider_read_names_only_the_whatsapp_provider_key() -> None:
    assert BUILDER_TENANT_PROVIDERS_SQL == (
        "SELECT t.id AS tenant_id, "
        "c.api_credentials ->> 'whatsapp_provider' AS whatsapp_provider "
        "FROM tenants AS t "
        "LEFT JOIN clinic_configs AS c ON c.tenant_id = t.id "
        "WHERE t.builder_id = :builder_id "
        "ORDER BY t.id"
    )
    for path in (ROOT / "src/repositories/credit.py", MODULE):
        source = path.read_text(encoding="utf-8")
        # No ORM access to the credentials, and every SQL use is the one key.
        assert "ClinicConfigTable" not in source, path.name
        assert ".api_credentials[" not in source, path.name
        uses = [
            m.start() for m in re.finditer(r"(?<![\w.])c\.api_credentials\b", source)
        ]
        assert len(uses) == (1 if path.name == "credit.py" else 0), path.name
        for start in uses:
            assert source.startswith(
                "c.api_credentials ->> 'whatsapp_provider' AS whatsapp_provider", start
            ), path.name


# ---------------------------------------------------------------------------
# The real entry point.
# ---------------------------------------------------------------------------


def _subprocess(tmp_path: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
    """``python -m src.ops.credit_charge_mark`` from a directory with no
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
        [sys.executable, "-m", "src.ops.credit_charge_mark", *argv],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.mark.parametrize(
    ("argv", "verdict"),
    [(["status"], "unconfigured"), (_status(), "switch_off")],
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
    statements: list[str]


@pytest.fixture
async def world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_World]:
    _patch_jsonb_for_sqlite()
    monkeypatch.setattr(
        credit_switch._SWITCH, "_reading", credit_switch._SWITCH._reading
    )
    monkeypatch.setattr(settings, "credit_hot_path_enabled", True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'marks.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        )
    sessions = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    async with sessions() as session, session.begin():
        session.add(Builder(id=BUILDER, name="Marked", is_active=True, status="active"))
    statements: list[str] = []
    try:
        yield _World(engine=engine, sessions=sessions, statements=statements)
    finally:
        await engine.dispose()


async def _at(world: _World, revision: str) -> None:
    """``alembic_version`` at *revision*; below 047 the marks table is
    dropped, so any read of it would fail the command."""

    async with world.engine.begin() as connection:
        await connection.execute(text("DELETE FROM alembic_version"))
        await connection.execute(
            text("INSERT INTO alembic_version VALUES (:revision)"),
            {"revision": revision},
        )
        if revision < "047":
            await connection.execute(
                text("DROP TABLE IF EXISTS credit_enforcement_marks")
            )


_INSERT_MARK = text(
    "INSERT INTO credit_enforcement_marks "
    "(id, builder_id, kind, occurred_at, actor_user_id, reason) "
    "VALUES (:id, :builder_id, :kind, :occurred_at, :actor_user_id, :reason)"
).bindparams(
    bindparam("id", type_=PG_UUID(as_uuid=True)),
    bindparam("builder_id", type_=PG_UUID(as_uuid=True)),
    bindparam("kind", type_=String()),
    bindparam("occurred_at", type_=DateTime(timezone=True)),
    bindparam("actor_user_id", type_=PG_UUID(as_uuid=True)),
    bindparam("reason", type_=String()),
)


async def _mark(world: _World, kind: str, at: datetime, reason: str) -> UUID:
    mark_id = uuid4()
    async with world.engine.begin() as connection:
        await connection.execute(
            _INSERT_MARK,
            {
                "id": mark_id,
                "builder_id": BUILDER,
                "kind": kind,
                "occurred_at": at,
                "actor_user_id": ACTOR,
                "reason": reason,
            },
        )
    return mark_id


async def _tenant(
    world: _World,
    *,
    credentials: dict[str, Any] | None,
    builder: UUID | None = BUILDER,
) -> UUID:
    tenant_id = uuid4()
    async with world.sessions() as session, session.begin():
        session.add(
            Tenant(
                id=tenant_id,
                name=f"Clinic {tenant_id.hex[:6]}",
                api_key_hash=uuid4().hex,
                builder_id=builder,
            )
        )
        await session.flush()
        if credentials is not None:
            session.add(
                ClinicConfigTable(tenant_id=tenant_id, api_credentials=credentials)
            )
    return tenant_id


async def _run(
    world: _World, builder: UUID = BUILDER
) -> tuple[int, dict[str, Any], str]:
    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        world.statements.append(statement)

    event.listen(world.engine.sync_engine, "before_cursor_execute", record)
    try:
        return await _invoke(_status(builder), session_factory=world.sessions)
    finally:
        event.remove(world.engine.sync_engine, "before_cursor_execute", record)


def _assert_read_only(world: _World) -> None:
    assert world.statements
    for statement in world.statements:
        assert statement.lstrip().upper().startswith("SELECT"), statement[:80]
        for found in re.finditer(r"api_credentials", statement):
            assert statement.startswith(
                "api_credentials ->> 'whatsapp_provider' AS whatsapp_provider",
                found.start(),
            ), statement


async def test_status_with_the_marks_table_absent(world: _World) -> None:
    await _at(world, "046")
    code, document, _ = await _run(world)
    assert code == 0, document
    assert document["verdict"] == "ok" and document["reasons"] == []
    assert document["switch"] == {
        "effective": True,
        "reason": "schema_ready",
        "revision": "046",
    }
    assert document["marks"] == {
        "gate_open": False,
        "summary": "marks table absent",
        "latest": None,
    }
    # The rest is still read.
    assert document["builder"] == {"exists": True, "active": True}
    assert document["enforcement"] == {
        "global_at": None,
        "effective_at": None,
        "origin": "none",
    }
    assert document["lifecycle"] == {
        "open_episode": None,
        "open_debt_mc": 0,
        "open_debts": 0,
    }
    assert document["tenants"] == []
    assert set(document["coverage"]) == {"usable", "reconciliation_failure"}
    assert not any("credit_enforcement_marks" in s for s in world.statements)
    _assert_read_only(world)


async def test_status_with_the_marks_table_empty(world: _World) -> None:
    await _at(world, "047")
    code, document, _ = await _run(world)
    assert code == 0, document
    assert document["switch"]["revision"] == "047"
    assert document["marks"] == {
        "gate_open": True,
        "summary": "no marks",
        "latest": None,
    }
    assert document["enforcement"]["origin"] == "none"
    assert any("credit_enforcement_marks" in s for s in world.statements)
    _assert_read_only(world)


async def test_status_reads_the_latest_of_a_mark_and_an_unmark(world: _World) -> None:
    await _at(world, "047")
    marked = T0 - timedelta(days=2)
    await _mark(world, "mark", marked, "piloto de cobrança")
    code, document, _ = await _run(world)
    assert code == 0, document
    latest = document["marks"]["latest"]
    assert document["marks"]["summary"] == "latest mark"
    assert latest["kind"] == "mark"
    assert latest["actor_user_id"] == str(ACTOR)
    assert latest["reason"] == "piloto de cobrança"
    assert datetime.fromisoformat(latest["occurred_at"]) == marked
    assert document["enforcement"]["origin"] == "mark"
    assert datetime.fromisoformat(document["enforcement"]["effective_at"]) == marked

    unmarked_id = await _mark(world, "unmark", T0 - timedelta(days=1), "fim do piloto")
    code, document, _ = await _run(world)
    assert code == 0, document
    assert document["marks"]["summary"] == "latest unmark"
    assert document["marks"]["latest"]["id"] == str(unmarked_id)
    assert document["marks"]["latest"]["reason"] == "fim do piloto"
    assert document["enforcement"] == {
        "global_at": None,
        "effective_at": None,
        "origin": "none",
    }

    # mark -> unmark -> mark: the latest event wins, at its own instant.
    remarked = T0 - timedelta(hours=1)
    await _mark(world, "mark", remarked, "de novo")
    code, document, _ = await _run(world)
    assert document["marks"]["summary"] == "latest mark"
    assert datetime.fromisoformat(document["enforcement"]["effective_at"]) == remarked
    _assert_read_only(world)


async def test_the_global_instant_wins_over_a_mark(world: _World) -> None:
    await _at(world, "047")
    await _mark(world, "mark", T0, "piloto")
    armed = T0 - timedelta(days=10)
    async with world.sessions() as session, session.begin():
        session.add(
            CreditEnforcementState(
                id=ENFORCEMENT_STATE_ID,
                credit_enforcement_at=armed,
                set_by="test",
                set_at=armed,
            )
        )
    code, document, _ = await _run(world)
    assert code == 0, document
    assert document["enforcement"]["origin"] == "global"
    assert datetime.fromisoformat(document["enforcement"]["global_at"]) == armed
    assert datetime.fromisoformat(document["enforcement"]["effective_at"]) == armed
    assert document["marks"]["summary"] == "latest mark"


async def test_status_shows_the_courtesy_rule_and_its_state(world: _World) -> None:
    """debito-cortesia-automatico (DESIGN item 13): a builder with courtesy,
    no mark and no global prints origin ``courtesy`` at its first grant,
    ``charged`` while courtesy is spendable and ``blocked`` once it is all
    removed ("Tirar tudo", no paid plan); a later mark wins over the rule;
    below 046 the rule reads nothing (``none``).  Read-only throughout."""

    from src.services import credit_courtesy

    async def grant() -> Any:
        async with world.sessions() as session, session.begin():
            return await credit_courtesy.grant(
                session,
                builder_id=BUILDER,
                credits=3,
                reason="cortesia",
                actor_user_id=ACTOR,
                idempotency_key=f"status-{uuid4()}",
            )

    await _at(world, "047")
    first = await grant()
    await grant()  # a second grant never moves the rule's instant
    code, document, _ = await _run(world)
    assert code == 0, document
    assert document["enforcement"] == {
        "global_at": None,
        "effective_at": document["enforcement"]["effective_at"],
        "origin": "courtesy",
        "state": "charged",
    }
    assert datetime.fromisoformat(document["enforcement"]["effective_at"]) == (
        first.occurred_at.astimezone(timezone.utc)
    )
    _assert_read_only(world)

    async with world.sessions() as session:
        lots = [
            row.id
            for row in (
                await session.execute(
                    select(CreditLot).where(CreditLot.builder_id == BUILDER)
                )
            ).scalars()
        ]
    for lot_id in lots:
        async with world.sessions() as session, session.begin():
            await credit_courtesy.remove(
                session,
                lot_id=lot_id,
                remove_all=True,
                reason="tirar tudo",
                actor_user_id=ACTOR,
                idempotency_key=f"status-removal-{uuid4()}",
            )
    code, document, _ = await _run(world)
    assert (document["enforcement"]["origin"], document["enforcement"]["state"]) == (
        "courtesy",
        "blocked",
    )

    # A mark wins over the rule (precedence global > mark > courtesy).
    await _mark(world, "mark", T0, "piloto")
    code, document, _ = await _run(world)
    assert document["enforcement"]["origin"] == "mark"
    assert "state" not in document["enforcement"]

    # Below 046 the rule is not read: no origin at all.
    await _at(world, "045")
    code, document, _ = await _run(world)
    assert code == 0, document
    assert document["enforcement"] == {
        "global_at": None,
        "effective_at": None,
        "origin": "none",
    }


async def test_the_linked_tenants_with_and_without_a_provider(world: _World) -> None:
    await _at(world, "047")
    zapi = await _tenant(world, credentials={"whatsapp_provider": "zapi"})
    meta = await _tenant(world, credentials={"whatsapp_provider": "meta_cloud"})
    no_key = await _tenant(world, credentials={"llm_provider": "openai"})
    no_config = await _tenant(world, credentials=None)
    await _tenant(world, credentials={"whatsapp_provider": "zapi"}, builder=None)
    code, document, _ = await _run(world)
    assert code == 0, document
    expected = {
        str(zapi): "zapi",
        str(meta): "meta_cloud",
        str(no_key): None,
        str(no_config): None,
    }
    assert document["tenants"] == [
        {"tenant_id": tenant_id, "whatsapp_provider": expected[tenant_id]}
        for tenant_id in sorted(expected, key=UUID)
    ]
    _assert_read_only(world)


async def test_no_credential_but_the_provider_reaches_the_output_or_the_log(
    world: _World,
) -> None:
    await _at(world, "047")
    await _tenant(
        world,
        credentials={
            "whatsapp_provider": "zapi",
            "zapi_instance_id": "instance-77",
            "zapi_token": SECRETS[0],
            "zapi_client_token": SECRETS[1],
            "llm_api_key": SECRETS[3],
        },
    )
    await _tenant(
        world,
        credentials={
            "whatsapp_provider": "meta_cloud",
            "meta_permanent_token": SECRETS[2],
        },
    )
    # A credential stored under the provider key itself is not a provider.
    await _tenant(world, credentials={"whatsapp_provider": SECRETS[4]})
    code, document, printed = await _run(world)
    assert code == 0, document
    assert sorted(str(row["whatsapp_provider"]) for row in document["tenants"]) == [
        "meta_cloud",
        "unrecognized",
        "zapi",
    ]
    for secret in (*SECRETS, "instance-77"):
        assert secret not in printed, secret
    for key in CREDENTIAL_KEYS:
        assert key not in printed, key
    assert "api_credentials" not in printed
    _assert_read_only(world)


async def test_status_reports_the_open_episode_and_the_open_debt(
    world: _World,
) -> None:
    """The lifecycle part reads the stored rows as the credit read routes do:
    an open exhaustion episode and a 5-credit debt (its row and its ``debt``
    ledger entry) of the builder."""

    await _at(world, "047")
    account, effect = uuid4(), f"debit:test:{uuid4()}"
    exhausted = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=5)
    async with world.sessions() as session, session.begin():
        session.add(CreditAccount(id=account, builder_id=BUILDER))
        await session.flush()
        session.add(
            CreditEffect(builder_id=BUILDER, effect_id=effect, effect_kind="debit")
        )
        await session.flush()
        session.add_all(
            [
                CreditLedgerEntry(
                    builder_id=BUILDER,
                    account_id=account,
                    effect_id=effect,
                    lot_id=None,
                    entry_type="debt",
                    amount_mc=5_000,
                    occurred_at=exhausted,
                ),
                CreditDebt(
                    builder_id=BUILDER,
                    account_id=account,
                    debt_source_kind="debit",
                    debt_source_effect_id=effect,
                    original_grant_lot_id=None,
                    amount_mc=5_000,
                    created_at=exhausted,
                ),
                CreditLifecycleEpisode(
                    builder_id=BUILDER,
                    account_id=account,
                    opened_effect_id=effect,
                    exhausted_at=exhausted,
                    suspension_due_at=exhausted + timedelta(hours=72),
                    deletion_due_at=exhausted + timedelta(days=30),
                ),
            ]
        )
    code, document, _ = await _run(world)
    assert code == 0, document
    lifecycle = document["lifecycle"]
    assert (lifecycle["open_debt_mc"], lifecycle["open_debts"]) == (5_000, 1)
    episode = lifecycle["open_episode"]
    assert datetime.fromisoformat(episode["exhausted_at"]) == exhausted
    assert datetime.fromisoformat(episode["suspension_due_at"]) == exhausted + (
        timedelta(hours=72)
    )
    _assert_read_only(world)


async def test_an_unknown_builder_is_refused(world: _World) -> None:
    await _at(world, "047")
    code, document, _ = await _run(world, builder=uuid4())
    assert code == ops.EXIT_CODES["refused"] == 20
    assert document["verdict"] == "refused"
    assert document["reasons"] == ["builder_not_found"]
    assert document["marks"] is None and document["tenants"] is None
    _assert_read_only(world)


# ---------------------------------------------------------------------------
# ``mark`` and ``unmark`` on SQLite (the locks are PostgreSQL's: see
# tests/integration/test_credit_charge_mark_postgres.py).
# ---------------------------------------------------------------------------


async def _user(
    world: _World, *, role: str = "super_admin", active: bool = True
) -> UUID:
    from src.models.user import User, UserRole

    user_id = uuid4()
    home = await _tenant(world, credentials=None, builder=None)
    async with world.sessions() as session, session.begin():
        session.add(
            User(
                id=user_id,
                tenant_id=home,
                email=f"{user_id.hex}@ops.invalid",
                password_hash="x",
                role=UserRole(role),
                is_active=active,
            )
        )
    return user_id


async def _change(
    world: _World,
    command: str,
    author: UUID,
    *,
    builder: UUID = BUILDER,
    reason: str = "piloto de cobrança",
) -> tuple[int, dict[str, Any], str]:
    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        world.statements.append(statement)

    world.statements.clear()
    event.listen(world.engine.sync_engine, "before_cursor_execute", record)
    try:
        return await _invoke(
            _change_argv(command, builder=builder, author=author, reason=reason),
            session_factory=world.sessions,
        )
    finally:
        event.remove(world.engine.sync_engine, "before_cursor_execute", record)


async def _events(world: _World, builder: UUID = BUILDER) -> list[dict[str, Any]]:
    from src.models.credit import CreditEnforcementMark

    async with world.sessions() as session:
        rows = (
            await session.execute(
                select(CreditEnforcementMark)
                .where(CreditEnforcementMark.builder_id == builder)
                .order_by(CreditEnforcementMark.occurred_at, CreditEnforcementMark.id)
            )
        ).scalars()
        return [
            {
                "id": row.id,
                "kind": row.kind,
                "occurred_at": (
                    row.occurred_at
                    if row.occurred_at.tzinfo
                    else row.occurred_at.replace(tzinfo=timezone.utc)
                ),
                "actor_user_id": row.actor_user_id,
                "reason": row.reason,
            }
            for row in rows
        ]


async def _accounts(world: _World, builder: UUID = BUILDER) -> int:
    async with world.engine.connect() as connection:
        return int(
            await connection.scalar(
                text("SELECT count(*) FROM credit_accounts WHERE builder_id = :b"),
                {"b": builder.hex},
            )
        )


async def _effective(world: _World, builder: UUID = BUILDER) -> Any:
    from src.repositories.credit import CreditRepository

    async with world.sessions() as session:
        found = await CreditRepository(session).effective_enforcement(
            builder, marks_readable=True
        )
        await session.rollback()
    return found


def _writes(world: _World) -> list[str]:
    return [
        statement
        for statement in world.statements
        if not statement.lstrip().upper().startswith(("SELECT", "PRAGMA"))
    ]


def _assert_refused(
    code: int, document: dict[str, Any], command: str, *reasons: str
) -> None:
    assert code == ops.EXIT_CODES["refused"] == 20, document
    assert (document["command"], document["verdict"]) == (command, "refused")
    assert document["reasons"] == list(reasons)
    assert document["change"]["event"] is None


async def test_mark_unmark_mark_writes_one_event_each(world: _World) -> None:
    await _at(world, "047")
    author = await _user(world)
    assert await _accounts(world) == 0

    code, marked, printed = await _change(world, "mark", author)
    assert code == 0, marked
    assert (marked["command"], marked["verdict"], marked["reasons"]) == (
        "mark",
        "marked",
        [],
    )
    event_ = marked["change"]["event"]
    assert marked["change"]["kind"] == "mark"
    assert marked["change"]["author_user_id"] == str(author)
    assert marked["change"]["reason"] == "piloto de cobrança"
    assert (event_["kind"], event_["actor_user_id"], event_["reason"]) == (
        "mark",
        str(author),
        "piloto de cobrança",
    )
    assert marked["enforcement"] == {
        "global_at": None,
        "effective_at": event_["occurred_at"],
        "origin": "mark",
    }
    assert marked["change"]["window"]["window_opened"] is False
    # Printed (stdout and the log line) as JSON, as ``status`` prints it.
    assert printed.count(json.dumps("piloto de cobrança")) == 4
    # One event and the builder's empty account (the request path's lock).
    inserts = [s for s in _writes(world) if s.lstrip().upper().startswith("INSERT")]
    assert len(inserts) == 2, inserts
    assert "credit_accounts" in inserts[0]
    assert "credit_enforcement_marks" in inserts[1]
    assert all(s.lstrip().upper().startswith("INSERT") for s in _writes(world))
    assert await _accounts(world) == 1
    first = datetime.fromisoformat(event_["occurred_at"])
    effective = await _effective(world)
    assert (effective.at, effective.origin) == (first, "mark")

    code, unmarked, _ = await _change(world, "unmark", author, reason="fim")
    assert code == 0, unmarked
    assert unmarked["verdict"] == "unmarked"
    assert unmarked["lifecycle"] == {
        "open_episode": None,
        "open_debt_mc": 0,
        "open_debts": 0,
    }
    assert unmarked["enforcement"]["origin"] == "none"
    inserts = [s for s in _writes(world) if s.lstrip().upper().startswith("INSERT")]
    assert [("credit_enforcement_marks" in s) for s in inserts] == [False, True]
    assert (await _effective(world)).origin is None

    code, again, _ = await _change(world, "mark", author, reason="de novo")
    assert code == 0, again
    second = datetime.fromisoformat(again["change"]["event"]["occurred_at"])
    effective = await _effective(world)
    assert (effective.at, effective.origin) == (second, "mark")

    events = await _events(world)
    assert [row["kind"] for row in events] == ["mark", "unmark", "mark"]
    assert [row["reason"] for row in events] == ["piloto de cobrança", "fim", "de novo"]
    assert {row["actor_user_id"] for row in events} == {author}
    instants = [row["occurred_at"] for row in events]
    assert instants == sorted(instants) and len(set(instants)) == 3
    assert instants[0] == first and instants[2] == second
    assert await _accounts(world) == 1

    code, status, _ = await _run(world)
    assert status["marks"]["latest"]["id"] == again["change"]["event"]["id"]


@pytest.mark.parametrize(
    ("role", "active", "exists"),
    [
        ("super_admin", False, True),
        ("clinic_admin", True, True),
        ("builder", True, True),
        ("investor", True, True),
        ("super_admin", True, False),
    ],
)
async def test_the_author_must_be_an_active_super_admin(
    world: _World, role: str, active: bool, exists: bool
) -> None:
    await _at(world, "047")
    author = await _user(world, role=role, active=active) if exists else uuid4()
    for command in ("mark", "unmark"):
        code, document, _ = await _change(world, command, author)
        _assert_refused(code, document, command, "author_not_active_super_admin")
        assert _writes(world) == []
    assert await _events(world) == []
    assert await _accounts(world) == 0


async def test_an_unknown_builder_is_refused_for_both_commands(world: _World) -> None:
    await _at(world, "047")
    author = await _user(world)
    ghost = uuid4()
    for command in ("mark", "unmark"):
        code, document, _ = await _change(world, command, author, builder=ghost)
        _assert_refused(code, document, command, "builder_not_found")
    assert await _events(world, ghost) == []
    assert await _accounts(world, ghost) == 0


async def test_below_047_both_commands_refuse_without_touching_the_table(
    world: _World,
) -> None:
    await _at(world, "046")
    author = await _user(world)
    for command in ("mark", "unmark"):
        code, document, _ = await _change(world, command, author)
        _assert_refused(code, document, command, "marks_table_absent")
        assert document["switch"]["revision"] == "046"
        assert not any("credit_enforcement_marks" in s for s in world.statements)
    assert await _accounts(world) == 0


async def test_the_armed_global_refuses_both_commands(world: _World) -> None:
    await _at(world, "047")
    author = await _user(world)
    code, _, _ = await _change(world, "mark", author)
    assert code == 0
    armed = T0 - timedelta(days=1)
    async with world.sessions() as session, session.begin():
        session.add(
            CreditEnforcementState(
                id=ENFORCEMENT_STATE_ID,
                credit_enforcement_at=armed,
                set_by="test",
                set_at=armed,
            )
        )
    for command in ("unmark", "mark"):
        code, document, _ = await _change(world, command, author)
        _assert_refused(code, document, command, "global_enforcement_armed")
        assert document["enforcement"]["origin"] == "global"
    assert [row["kind"] for row in await _events(world)] == ["mark"]


async def test_already_marked_and_not_marked_are_refused(world: _World) -> None:
    await _at(world, "047")
    author = await _user(world)
    code, document, _ = await _change(world, "unmark", author)
    _assert_refused(code, document, "unmark", "not_marked")
    assert document["marks"]["summary"] == "no marks"
    assert await _accounts(world) == 0
    assert (await _change(world, "mark", author))[0] == 0
    code, document, _ = await _change(world, "mark", author)
    _assert_refused(code, document, "mark", "already_marked")
    assert document["marks"]["summary"] == "latest mark"
    assert (await _change(world, "unmark", author))[0] == 0
    code, document, _ = await _change(world, "unmark", author)
    _assert_refused(code, document, "unmark", "not_marked")
    assert document["marks"]["summary"] == "latest unmark"
    assert [row["kind"] for row in await _events(world)] == ["mark", "unmark"]


@pytest.mark.parametrize(
    ("credentials", "allowed"),
    [
        (None, True),
        ({"llm_provider": "openai"}, True),
        ({"whatsapp_provider": None}, True),
        ({"whatsapp_provider": "zapi", "zapi_token": SECRETS[0]}, True),
        ({"whatsapp_provider": "meta_cloud", "meta_permanent_token": SECRETS[2]}, True),
        ({"whatsapp_provider": "uazapi", "uazapi_token": SECRETS[1]}, False),
        ({"whatsapp_provider": "evolution"}, False),
        ({"whatsapp_provider": "dialog360"}, False),
        ({"whatsapp_provider": "twilio"}, False),
        ({"whatsapp_provider": ""}, False),
        ({"whatsapp_provider": "ZAPI"}, False),
        ({"whatsapp_provider": SECRETS[4], "llm_api_key": SECRETS[3]}, False),
    ],
    ids=lambda value: repr(value)[:40],
)
async def test_mark_refuses_a_channel_outside_the_allowlist(
    world: _World, credentials: dict[str, Any] | None, allowed: bool
) -> None:
    await _at(world, "047")
    author = await _user(world)
    await _tenant(world, credentials={"whatsapp_provider": "zapi"})
    tenant = await _tenant(world, credentials=credentials)
    code, document, printed = await _change(world, "mark", author)
    if allowed:
        assert code == 0, document
        assert [row["kind"] for row in await _events(world)] == ["mark"]
    else:
        _assert_refused(code, document, "mark", "channel_not_allowed")
        assert await _events(world) == []
        assert await _accounts(world) == 0
    listed = {row["tenant_id"]: row["whatsapp_provider"] for row in document["tenants"]}
    assert listed[str(tenant)] == ops.provider_label(
        None if credentials is None else credentials.get("whatsapp_provider")
    )
    for secret in (*SECRETS, *CREDENTIAL_KEYS, "api_credentials"):
        assert secret not in printed, secret
    for statement in world.statements:
        for found in re.finditer(r"api_credentials", statement):
            assert statement.startswith(
                "api_credentials ->> 'whatsapp_provider' AS whatsapp_provider",
                found.start(),
            ), statement


@pytest.mark.parametrize(
    "coverage",
    [
        {"quarantined": True},
        {"identity_conflict": True},
        {"reconciliation_failure": True},
    ],
    ids=lambda value: next(iter(value)),
)
async def test_mark_refuses_an_unusable_coverage(
    world: _World, monkeypatch: pytest.MonkeyPatch, coverage: dict[str, bool]
) -> None:
    """The real ASAAS states that make it unusable are planted on
    PostgreSQL; here the reader's verdict is injected."""

    from src.services.credit_rollout import BindingCoverage, CreditRolloutService

    seen: list[tuple[UUID, datetime | None]] = []

    async def unusable(
        _self: Any, builder_id: UUID, *, at: datetime | None = None, **_: Any
    ) -> BindingCoverage:
        seen.append((builder_id, at))
        return BindingCoverage(**coverage)

    monkeypatch.setattr(CreditRolloutService, "binding_coverage", unusable)
    await _at(world, "047")
    author = await _user(world)
    code, document, _ = await _change(world, "mark", author)
    _assert_refused(code, document, "mark", "coverage_unusable")
    assert document["coverage"]["usable"] is False
    assert seen and seen[0][0] == BUILDER and seen[0][1] is not None
    assert datetime.fromisoformat(document["database_now"]) == seen[0][1]
    assert await _events(world) == []


async def _opening(world: _World, *, at: datetime) -> None:
    """A ``baseline`` opening of the window containing ``at``, as
    ``WindowWork`` writes it under an enforcement."""

    from src.models.credit_inventory import CreditAllocation
    from src.services.credit_allocations import accounting_window

    window = accounting_window((), at)
    async with world.sessions() as session, session.begin():
        account = await session.scalar(
            select(CreditAccount.id).where(CreditAccount.builder_id == BUILDER)
        )
        if account is None:
            account = uuid4()
            session.add(CreditAccount(id=account, builder_id=BUILDER))
            await session.flush()
        session.add(
            CreditAllocation(
                id=uuid4(),
                builder_id=BUILDER,
                account_id=account,
                window_start=window.start,
                window_end=window.end,
                category="rag",
                allocation_kind="baseline",
                source_key=f"opening:test:{uuid4()}",
                units=0,
                target_mc=0,
                charged_mc=0,
            )
        )


async def test_mark_refuses_a_window_opened_by_an_earlier_mark(world: _World) -> None:
    await _at(world, "047")
    author = await _user(world)
    code, document, _ = await _change(world, "mark", author)
    assert code == 0, document
    await _opening(world, at=datetime.now(timezone.utc))
    assert (await _change(world, "unmark", author))[0] == 0
    code, document, _ = await _change(world, "mark", author)
    _assert_refused(code, document, "mark", "window_already_opened")
    window = document["change"]["window"]
    assert window["window_opened"] is True
    assert window["latest_mark_at"] is not None
    assert [row["kind"] for row in await _events(world)] == ["mark", "unmark"]


async def test_mark_reports_every_refusal_that_applies(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services.credit_rollout import BindingCoverage, CreditRolloutService

    async def quarantined(_self: Any, *_args: Any, **_kwargs: Any) -> BindingCoverage:
        return BindingCoverage(quarantined=True)

    await _at(world, "047")
    author = await _user(world)
    assert (await _change(world, "mark", author))[0] == 0
    await _opening(world, at=datetime.now(timezone.utc))
    assert (await _change(world, "unmark", author))[0] == 0
    await _tenant(world, credentials={"whatsapp_provider": "uazapi"})
    monkeypatch.setattr(CreditRolloutService, "binding_coverage", quarantined)
    code, document, _ = await _change(world, "mark", author)
    _assert_refused(
        code,
        document,
        "mark",
        "coverage_unusable",
        "channel_not_allowed",
        "window_already_opened",
    )


async def _lifecycle(world: _World, *, episode: bool, debt: bool) -> None:
    """An open exhaustion episode and/or a 5-credit open debt of BUILDER
    (the rows ``status`` reads; see its own test above)."""

    async with world.sessions() as session, session.begin():
        account = await session.scalar(
            select(CreditAccount.id).where(CreditAccount.builder_id == BUILDER)
        )
        if account is None:
            account = uuid4()
            session.add(CreditAccount(id=account, builder_id=BUILDER))
            await session.flush()
        effect = f"debit:test:{uuid4()}"
        exhausted = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(
            hours=5
        )
        session.add(
            CreditEffect(builder_id=BUILDER, effect_id=effect, effect_kind="debit")
        )
        await session.flush()
        if debt:
            session.add_all(
                [
                    CreditLedgerEntry(
                        builder_id=BUILDER,
                        account_id=account,
                        effect_id=effect,
                        lot_id=None,
                        entry_type="debt",
                        amount_mc=5_000,
                        occurred_at=exhausted,
                    ),
                    CreditDebt(
                        builder_id=BUILDER,
                        account_id=account,
                        debt_source_kind="debit",
                        debt_source_effect_id=effect,
                        original_grant_lot_id=None,
                        amount_mc=5_000,
                        created_at=exhausted,
                    ),
                ]
            )
        if episode:
            session.add(
                CreditLifecycleEpisode(
                    builder_id=BUILDER,
                    account_id=account,
                    opened_effect_id=effect,
                    exhausted_at=exhausted,
                    suspension_due_at=exhausted + timedelta(hours=72),
                    deletion_due_at=exhausted + timedelta(days=30),
                )
            )


@pytest.mark.parametrize(
    ("episode", "debt", "reasons"),
    [
        (True, False, ("open_episode",)),
        (False, True, ("open_debt",)),
        (True, True, ("open_episode", "open_debt")),
    ],
)
async def test_unmark_refuses_an_open_episode_or_debt(
    world: _World, episode: bool, debt: bool, reasons: tuple[str, ...]
) -> None:
    await _at(world, "047")
    author = await _user(world)
    assert (await _change(world, "mark", author))[0] == 0
    await _lifecycle(world, episode=episode, debt=debt)
    code, document, _ = await _change(world, "unmark", author)
    _assert_refused(code, document, "unmark", *reasons)
    lifecycle = document["lifecycle"]
    assert (lifecycle["open_episode"] is not None) is episode
    assert (lifecycle["open_debt_mc"], lifecycle["open_debts"]) == (
        (5_000, 1) if debt else (0, 0)
    )
    assert [row["kind"] for row in await _events(world)] == ["mark"]
    assert (await _effective(world)).origin == "mark"


async def test_the_reason_is_stored_trimmed_and_printed_as_stored(
    world: _World,
) -> None:
    await _at(world, "047")
    author = await _user(world)
    reason = "  " + "ç" * 500 + "  "
    code, document, printed = await _change(world, "mark", author, reason=reason)
    assert code == 0, document
    (row,) = await _events(world)
    assert row["reason"] == "ç" * 500
    assert document["change"]["reason"] == document["change"]["event"]["reason"]
    assert document["change"]["event"]["reason"] == "ç" * 500
