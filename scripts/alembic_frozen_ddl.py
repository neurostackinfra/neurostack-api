"""Freeze, from the catalog, the revision-035 DDL that migration 036 removes.

Migration 036 (Group 10 E5, "desacoplar") drops three columns and the
constraints that depend on them, drops the roster statement trigger on
``billing_subscriptions`` and rewrites four canary trigger functions.  Its
downgrade is real (owner decision 2026-10-17): it restores the 035 objects,
and the DDL it executes is never typed by hand.  This module reads it from a
database at revision 035 -- ``pg_get_functiondef``, ``pg_get_triggerdef``,
``pg_get_constraintdef`` and the column catalog -- and renders the frozen
file ``alembic/frozen/036_downgrade_restores_035.json`` that the downgrade
executes verbatim (after checking the SHA-256 pinned in the migration).

Regenerate only from a database freshly upgraded to exactly 035::

    python -m scripts.alembic_frozen_ddl \\
        --database-url postgresql://USER@127.0.0.1:PORT/DB --output FILE

The PostgreSQL fidelity test re-extracts from a fresh 035 database and
requires byte equality with the frozen file, so a hand edit of the file (or a
change to 026-035) fails there and at the migration's digest check.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]
FROZEN_036_PATH = ROOT / "alembic" / "frozen" / "036_downgrade_restores_035.json"
SOURCE_REVISION = "035"


@dataclass(frozen=True)
class Scope:
    """The 035 objects one migration removes or rewrites."""

    columns: tuple[tuple[str, str], ...]
    functions: tuple[str, ...]
    triggers: tuple[tuple[str, str], ...]


# Constraints are not listed: every constraint whose key includes one of the
# columns is frozen, as the catalog reports it (pg_constraint.conkey).
SCOPE_036 = Scope(
    columns=(
        ("billing_checkout_sessions", "migration_id"),
        ("billing_rollout_readiness_receipts", "roster_revision"),
        ("billing_subscriptions", "roster_revision"),
    ),
    functions=(
        "protect_billing_canary_subscription",
        "validate_billing_canary_checkout",
        "validate_billing_canary_effect",
        "validate_billing_canary_projection",
    ),
    triggers=(("billing_subscriptions", "trg_billing_subscriptions_roster_fence"),),
)

_HEADER = (
    "Frozen by scripts/alembic_frozen_ddl.py from a database at revision 035 "
    "(pg_get_functiondef, pg_get_triggerdef, pg_get_constraintdef and the "
    "column catalog). Migration 036's downgrade executes these statements "
    "verbatim, in the order columns, constraints, functions, triggers. Never "
    "edit by hand: regenerate from a fresh 035 database and update the "
    "SHA-256 pinned in the migration."
)

_COLUMN_SQL = """
SELECT format_type(a.atttypid, a.atttypmod) AS column_type,
       a.attidentity::text AS identity,
       a.attgenerated::text AS generated,
       a.attcollation = t.typcollation AS default_collation,
       col_description(c.oid, a.attnum) AS comment,
       format(
           'ALTER TABLE %I.%I ADD COLUMN %I %s%s%s',
           n.nspname, c.relname, a.attname,
           format_type(a.atttypid, a.atttypmod),
           CASE WHEN d.adbin IS NULL THEN ''
                ELSE ' DEFAULT ' || pg_get_expr(d.adbin, d.adrelid) END,
           CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE '' END
       ) AS statement
FROM pg_attribute AS a
JOIN pg_class AS c ON c.oid = a.attrelid
JOIN pg_namespace AS n ON n.oid = c.relnamespace
JOIN pg_type AS t ON t.oid = a.atttypid
LEFT JOIN pg_attrdef AS d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE n.nspname = 'public'
  AND c.relname = :table
  AND a.attname = :column
  AND a.attnum > 0
  AND NOT a.attisdropped
"""

_CONSTRAINT_SQL = """
SELECT c.relname AS table_name,
       con.conname AS name,
       obj_description(con.oid, 'pg_constraint') AS comment,
       format(
           'ALTER TABLE %I.%I ADD CONSTRAINT %I %s',
           n.nspname, c.relname, con.conname, pg_get_constraintdef(con.oid)
       ) AS statement
FROM pg_constraint AS con
JOIN pg_class AS c ON c.oid = con.conrelid
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND EXISTS (
      SELECT 1
      FROM unnest(con.conkey) AS key(attnum)
      JOIN pg_attribute AS a
        ON a.attrelid = con.conrelid AND a.attnum = key.attnum
      WHERE c.relname || '.' || a.attname = ANY(CAST(:columns AS text[]))
  )
ORDER BY c.relname, con.conname
"""

_FUNCTION_SQL = """
SELECT p.proname AS name,
       p.prokind::text AS kind,
       p.proacl IS NULL AS default_acl,
       obj_description(p.oid, 'pg_proc') AS comment,
       pg_get_functiondef(p.oid) AS statement
FROM pg_proc AS p
JOIN pg_namespace AS n ON n.oid = p.pronamespace
WHERE n.nspname = 'public' AND p.proname = :name
"""

_TRIGGER_SQL = """
SELECT t.tgenabled::text AS enabled,
       obj_description(t.oid, 'pg_trigger') AS comment,
       pg_get_triggerdef(t.oid) AS statement
FROM pg_trigger AS t
JOIN pg_class AS c ON c.oid = t.tgrelid
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relname = :table
  AND t.tgname = :name
  AND NOT t.tgisinternal
"""


class FreezeError(RuntimeError):
    """The catalog holds something this freezer cannot restore faithfully."""


def _one(connection: Connection, sql: str, what: str, **params: Any) -> Any:
    rows = connection.execute(text(sql), params).mappings().all()
    if len(rows) != 1:
        raise FreezeError(f"{what}: expected exactly one catalog row, got {len(rows)}")
    return rows[0]


def extract(connection: Connection, scope: Scope = SCOPE_036) -> dict[str, Any]:
    """Read the frozen document for *scope* from a database at exactly 035."""

    revision = connection.execute(
        text("SELECT version_num FROM alembic_version")
    ).scalar_one()
    if revision != SOURCE_REVISION:
        raise FreezeError(
            f"the source database is at {revision}, not {SOURCE_REVISION}"
        )

    columns = []
    for table, column in scope.columns:
        row = _one(
            connection,
            _COLUMN_SQL,
            f"column {table}.{column}",
            table=table,
            column=column,
        )
        if row["identity"] or row["generated"]:
            raise FreezeError(f"{table}.{column}: identity/generated is unsupported")
        if not row["default_collation"] or row["comment"] is not None:
            raise FreezeError(f"{table}.{column}: collation/comment is unsupported")
        columns.append(
            {"table": table, "column": column, "statement": row["statement"]}
        )

    keys = [f"{table}.{column}" for table, column in scope.columns]
    constraints = []
    for row in connection.execute(text(_CONSTRAINT_SQL), {"columns": keys}).mappings():
        if row["comment"] is not None:
            raise FreezeError(f"constraint {row['name']}: comment is unsupported")
        constraints.append(
            {
                "table": row["table_name"],
                "name": row["name"],
                "statement": row["statement"],
            }
        )

    functions = []
    for name in scope.functions:
        row = _one(connection, _FUNCTION_SQL, f"function {name}", name=name)
        if row["kind"] != "f" or not row["default_acl"] or row["comment"] is not None:
            raise FreezeError(f"function {name}: kind/ACL/comment is unsupported")
        functions.append({"name": name, "statement": row["statement"]})

    triggers = []
    for table, name in scope.triggers:
        row = _one(connection, _TRIGGER_SQL, f"trigger {name}", table=table, name=name)
        if row["enabled"] != "O" or row["comment"] is not None:
            raise FreezeError(f"trigger {name}: enabled state/comment is unsupported")
        triggers.append({"table": table, "name": name, "statement": row["statement"]})

    return {
        "comment": _HEADER,
        "revision": SOURCE_REVISION,
        "columns": columns,
        "constraints": constraints,
        "functions": functions,
        "triggers": triggers,
    }


def render(document: dict[str, Any]) -> str:
    """The exact bytes of the frozen file (stable key order, ASCII, newline)."""

    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


async def _extract_from(url: str) -> dict[str, Any]:
    parsed = make_url(url).set(drivername="postgresql+asyncpg")
    engine = create_async_engine(parsed, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(extract)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--database-url",
        required=True,
        help="a database freshly upgraded to exactly 035",
    )
    parser.add_argument("--output", type=Path, default=FROZEN_036_PATH)
    arguments = parser.parse_args(argv)
    document = asyncio.run(_extract_from(arguments.database_url))
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(render(document), encoding="ascii")
    print(f"wrote {arguments.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
