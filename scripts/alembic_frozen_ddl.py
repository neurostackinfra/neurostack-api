"""Freeze, from the catalog, the DDL that the downgrades of 036 and 037 restore.

Migration 036 (Group 10 E5, "desacoplar") drops three columns and the
constraints that depend on them, drops the roster statement trigger on
``billing_subscriptions`` and rewrites four canary trigger functions.
Migration 037 (Group 10 E6, "remover objetos legados") drops the four legacy
tables of the AbacatePay migration saga and the ten functions only they use.
Both downgrades are real (owner decision 2026-10-17): they restore the
objects of the previous revision, and the DDL they execute is never typed by
hand.  This module reads it from a database at the previous revision --
``pg_get_functiondef``, ``pg_get_triggerdef``, ``pg_get_constraintdef``,
``pg_get_indexdef`` and the column catalog -- and renders the frozen files
``alembic/frozen/036_downgrade_restores_035.json`` and
``alembic/frozen/037_downgrade_restores_036.json`` that the downgrades execute
verbatim (after checking the SHA-256 pinned in each migration).

Regenerate only from a database freshly upgraded to exactly the source
revision (035 for ``--migration 036``, 036 for ``--migration 037``)::

    python -m scripts.alembic_frozen_ddl --migration 037 \\
        --database-url postgresql://USER@127.0.0.1:PORT/DB --output FILE

The PostgreSQL fidelity tests re-extract from a fresh database at the source
revision and require byte equality with the frozen file, so a hand edit of
the file (or a change to the migrations before it) fails there and at the
migration's digest check.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

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


# ---------------------------------------------------------------------------
# Migration 037: whole tables and functions, dropped and restored.
# ---------------------------------------------------------------------------

FROZEN_037_PATH = ROOT / "alembic" / "frozen" / "037_downgrade_restores_036.json"
SOURCE_REVISION_037 = "036"
# Scope names are interpolated into SQL text: plain lowercase identifiers only.
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")


@dataclass(frozen=True)
class DropScope:
    """The 036 objects migration 037 drops whole.

    ``tables`` and ``functions`` are in creation order (the order the
    downgrade restores them in).  Functions are validated when they are
    created, so the tables come first (``%ROWTYPE`` declarations of the
    PL/pgSQL validators name them) and ``billing_evidence_timestamp``
    precedes ``billing_provider_evidence_digest``, the SQL function that
    calls it; the CHECK that calls ``billing_migration_snapshot_is_valid``
    comes after both.  ``rows`` names the
    seed rows a brand-new 036 database holds (table, columns copied); every
    other scope table must be empty in the source database.
    """

    tables: tuple[str, ...]
    functions: tuple[str, ...]
    rows: tuple[tuple[str, tuple[str, ...]], ...]


SCOPE_037 = DropScope(
    tables=(
        "billing_roster_control",
        "billing_migrations",
        "billing_migration_receipts",
        "billing_migration_absence_reads",
    ),
    functions=(
        "billing_migration_snapshot_is_valid",
        "lock_billing_roster_statement",
        "validate_billing_migration_receipt",
        "validate_billing_migration_receipt_commit",
        "protect_bound_billing_migration_target",
        "protect_billing_roster_control",
        "validate_billing_provider_evidence",
        "billing_evidence_timestamp",
        "billing_provider_evidence_digest",
        "validate_billing_migration_absence_read",
    ),
    rows=(("billing_roster_control", ("singleton", "billing_roster_revision")),),
)

_HEADER_037 = (
    "Frozen by scripts/alembic_frozen_ddl.py from a database at revision 036 "
    "(pg_get_functiondef, pg_get_constraintdef, pg_get_indexdef, "
    "pg_get_triggerdef, the column catalog and the seed row). Migration 037's "
    "downgrade executes these statements verbatim, in the order tables, "
    "functions, constraints, indexes, rows, triggers; its upgrade derives the "
    "allowlist of the objects it may drop from this file. Never edit by hand: "
    "regenerate from a fresh 036 database and update the SHA-256 pinned in "
    "the migration."
)

_TABLE_SQL = """
SELECT c.oid AS oid,
       c.relkind::text AS kind,
       c.relpersistence::text AS persistence,
       c.relrowsecurity OR c.relforcerowsecurity AS row_security,
       c.relreplident::text AS replica_identity,
       c.reloptions IS NOT NULL AS options,
       c.reltablespace <> 0 AS tablespace,
       c.relispartition OR c.relhassubclass AS inheritance,
       am.amname AS access_method,
       c.relacl IS NULL AS default_acl,
       obj_description(c.oid, 'pg_class') AS comment
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
LEFT JOIN pg_am AS am ON am.oid = c.relam
WHERE n.nspname = 'public' AND c.relname = :table
"""

_TABLE_COLUMNS_SQL = """
SELECT a.attname AS name,
       a.attidentity::text AS identity,
       a.attgenerated::text AS generated,
       a.attcollation = t.typcollation AS default_collation,
       a.attacl IS NULL AS default_acl,
       col_description(a.attrelid, a.attnum) AS comment,
       d.adbin IS NOT NULL AS has_default,
       format(
           '%I %s%s%s',
           a.attname,
           format_type(a.atttypid, a.atttypmod),
           CASE WHEN d.adbin IS NULL THEN ''
                ELSE ' DEFAULT ' || pg_get_expr(d.adbin, d.adrelid) END,
           CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE '' END
       ) AS definition
FROM pg_attribute AS a
JOIN pg_type AS t ON t.oid = a.atttypid
LEFT JOIN pg_attrdef AS d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attrelid = :oid AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""

# Every constraint of the scope tables except a constraint trigger's own
# pg_constraint row (pg_get_triggerdef recreates it).  Foreign keys last: the
# ones between scope tables need the referenced unique constraint first.
_TABLE_CONSTRAINTS_SQL = """
SELECT c.relname AS table_name,
       con.conname AS name,
       con.contype = 'f' AS foreign_key,
       con.contype = 'c' AS check_constraint,
       con.contype IN ('p', 'u', 'x') AS owns_index,
       obj_description(con.oid, 'pg_constraint') AS comment,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint AS con
JOIN pg_class AS c ON c.oid = con.conrelid
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relname = ANY(CAST(:tables AS text[]))
  AND con.contype <> 't'
"""

# Indexes that no constraint owns (a constraint's index comes back with it).
_TABLE_INDEXES_SQL = """
SELECT c.relname AS table_name,
       i.relname AS name,
       obj_description(i.oid, 'pg_class') AS comment,
       pg_get_indexdef(i.oid) AS statement
FROM pg_index AS x
JOIN pg_class AS c ON c.oid = x.indrelid
JOIN pg_class AS i ON i.oid = x.indexrelid
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relname = ANY(CAST(:tables AS text[]))
  AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conindid = x.indexrelid
                  AND conrelid = x.indrelid)
"""

_TABLE_TRIGGERS_SQL = """
SELECT c.relname AS table_name,
       t.tgname AS name,
       t.tgconstraint <> 0 AS constraint_trigger,
       t.tgenabled::text AS enabled,
       obj_description(t.oid, 'pg_trigger') AS comment,
       pg_get_triggerdef(t.oid) AS statement
FROM pg_trigger AS t
JOIN pg_class AS c ON c.oid = t.tgrelid
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relname = ANY(CAST(:tables AS text[]))
  AND NOT t.tgisinternal
"""

_FUNCTION_037_SQL = """
SELECT p.proname AS name,
       p.oid::regprocedure::text AS signature,
       p.prokind::text AS kind,
       p.proacl IS NULL AS default_acl,
       p.prosecdef AS security_definer,
       p.proconfig IS NOT NULL AS config,
       obj_description(p.oid, 'pg_proc') AS comment,
       pg_get_functiondef(p.oid) AS statement
FROM pg_proc AS p
JOIN pg_namespace AS n ON n.oid = p.pronamespace
WHERE n.nspname = 'public' AND p.proname = :name
"""


def _ordered(
    rows: list[Any], tables: tuple[str, ...], *, foreign_keys_last: bool = False
) -> list[Any]:
    """Catalog rows in scope-table order, then by name (FKs last if asked)."""

    def key(row: Any) -> tuple[bool, int, str]:
        late = bool(row["foreign_key"]) if foreign_keys_last else False
        return (late, tables.index(row["table_name"]), row["name"])

    return sorted(rows, key=key)


# pg_get_constraintdef prints a varchar IN-list as
# ``(col)::text = ANY ((ARRAY['a'::character varying, ...])::text[])``.  Read
# back, the cast of an ARRAY[...] literal is pushed into its elements, so the
# restored CHECK would print ``ARRAY[('a'::character varying)::text, ...]``:
# the same predicate, a different catalog.  Typing the literal as
# ``character varying[]`` first keeps the original expression; every CHECK
# is verified to read back exactly (``_round_trip_check``).
_VARCHAR_ARRAY_AS_TEXT = re.compile(
    r"\(ARRAY\[((?:'(?:[^']|'')*'::character varying(?:, )?)+)\]\)::text\[\]"
)


def _round_trip_check(
    connection: Connection, table: str, name: str, definition: str
) -> str:
    """A CHECK definition that, executed, reads back as *definition*.

    Proven on a temporary copy of the table's columns inside a savepoint
    that is rolled back, so the source database is left as it was.
    """

    candidate = _VARCHAR_ARRAY_AS_TEXT.sub(
        r"(ARRAY[\1]::character varying[])::text[]", definition
    )
    savepoint = connection.begin_nested()
    try:
        # Raw driver SQL: a definition is not a template for bind parameters.
        connection.exec_driver_sql(
            f"CREATE TEMPORARY TABLE frozen_ddl_probe (LIKE public.{table})"
        )
        connection.exec_driver_sql(
            f"ALTER TABLE frozen_ddl_probe ADD CONSTRAINT {name} {candidate}"
        )
        read_back = connection.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'frozen_ddl_probe'::regclass "
                "AND conname = :name"
            ),
            {"name": name},
        ).scalar_one()
    finally:
        savepoint.rollback()
    if read_back != definition:
        raise FreezeError(f"constraint {name}: its definition does not read back")
    return candidate


def _require_no_comment(row: Any, what: str) -> None:
    if row["comment"] is not None:
        raise FreezeError(f"{what}: comment is unsupported")


def extract_037(connection: Connection, scope: DropScope = SCOPE_037) -> dict[str, Any]:
    """Read the frozen 037 document for *scope* from a database at exactly 036."""

    revision = connection.execute(
        text("SELECT version_num FROM alembic_version")
    ).scalar_one()
    if revision != SOURCE_REVISION_037:
        raise FreezeError(
            f"the source database is at {revision}, not {SOURCE_REVISION_037}"
        )

    functions = []
    for name in scope.functions:
        row = _one(connection, _FUNCTION_037_SQL, f"function {name}", name=name)
        if (
            row["kind"] != "f"
            or not row["default_acl"]
            or row["security_definer"]
            or row["config"]
        ):
            raise FreezeError(
                f"function {name}: kind/ACL/definer/config is unsupported"
            )
        _require_no_comment(row, f"function {name}")
        functions.append(
            {"name": name, "signature": row["signature"], "statement": row["statement"]}
        )

    tables = []
    for table in scope.tables:
        row = _one(connection, _TABLE_SQL, f"table {table}", table=table)
        if (
            row["kind"] != "r"
            or row["persistence"] != "p"
            or row["row_security"]
            or row["replica_identity"] != "d"
            or row["options"]
            or row["tablespace"]
            or row["inheritance"]
            or row["access_method"] != "heap"
            or not row["default_acl"]
        ):
            raise FreezeError(f"table {table}: storage/security option is unsupported")
        _require_no_comment(row, f"table {table}")
        columns = connection.execute(
            text(_TABLE_COLUMNS_SQL), {"oid": row["oid"]}
        ).mappings()
        definitions, names, defaults = [], [], []
        for column in columns:
            what = f"column {table}.{column['name']}"
            if column["identity"] or column["generated"]:
                raise FreezeError(f"{what}: identity/generated is unsupported")
            if not column["default_collation"] or not column["default_acl"]:
                raise FreezeError(f"{what}: collation/ACL is unsupported")
            _require_no_comment(column, what)
            definitions.append(column["definition"])
            names.append(column["name"])
            if column["has_default"]:
                defaults.append(column["name"])
        if not names:
            raise FreezeError(f"table {table}: no columns")
        body = ",\n    ".join(definitions)
        tables.append(
            {
                "table": table,
                "columns": names,
                "defaults": defaults,
                "statement": f"CREATE TABLE public.{table} (\n    {body}\n)",
            }
        )

    keys = {"tables": list(scope.tables)}
    constraints = []
    for row in _ordered(
        list(connection.execute(text(_TABLE_CONSTRAINTS_SQL), keys).mappings()),
        scope.tables,
        foreign_keys_last=True,
    ):
        _require_no_comment(row, f"constraint {row['name']}")
        definition = row["definition"]
        if row["check_constraint"]:
            definition = _round_trip_check(
                connection, row["table_name"], row["name"], definition
            )
        constraints.append(
            {
                "table": row["table_name"],
                "name": row["name"],
                "owns_index": bool(row["owns_index"]),
                "statement": (
                    f"ALTER TABLE public.{row['table_name']} "
                    f"ADD CONSTRAINT {row['name']} {definition}"
                ),
            }
        )

    indexes = []
    for row in _ordered(
        list(connection.execute(text(_TABLE_INDEXES_SQL), keys).mappings()),
        scope.tables,
    ):
        _require_no_comment(row, f"index {row['name']}")
        indexes.append(
            {
                "table": row["table_name"],
                "name": row["name"],
                "statement": row["statement"],
            }
        )

    seeded = {table for table, _ in scope.rows}
    identifiers = [*scope.tables, *(c for _, columns in scope.rows for c in columns)]
    for identifier in identifiers:
        if not _IDENTIFIER.fullmatch(identifier):
            raise FreezeError(f"{identifier!r} is not a plain lowercase identifier")
    rows = []
    for table in scope.tables:
        count = connection.execute(
            text(f"SELECT count(*) FROM public.{table}")
        ).scalar_one()
        if table not in seeded:
            if count != 0:
                raise FreezeError(f"table {table}: expected no rows, got {count}")
            continue
        if count != 1:
            raise FreezeError(
                f"table {table}: expected exactly one seed row, got {count}"
            )
        (copied,) = (columns for name, columns in scope.rows if name == table)
        # The values come back as SQL literals of their text form, quoted by
        # the server (quote_nullable), so nothing is formatted by hand.
        literals = ", ".join(f"quote_nullable({column}::text)" for column in copied)
        values = connection.execute(
            text(f"SELECT concat_ws(', ', {literals}) FROM public.{table}")
        ).scalar_one()
        rows.append(
            {
                "table": table,
                "statement": (
                    f"INSERT INTO public.{table} ({', '.join(copied)}) "
                    f"VALUES ({values})"
                ),
            }
        )

    triggers = []
    for row in _ordered(
        list(connection.execute(text(_TABLE_TRIGGERS_SQL), keys).mappings()),
        scope.tables,
    ):
        if row["enabled"] != "O":
            raise FreezeError(f"trigger {row['name']}: enabled state is unsupported")
        _require_no_comment(row, f"trigger {row['name']}")
        triggers.append(
            {
                "table": row["table_name"],
                "name": row["name"],
                "constraint": bool(row["constraint_trigger"]),
                "statement": row["statement"],
            }
        )

    return {
        "comment": _HEADER_037,
        "revision": SOURCE_REVISION_037,
        "functions": functions,
        "tables": tables,
        "constraints": constraints,
        "indexes": indexes,
        "rows": rows,
        "triggers": triggers,
    }


_EXTRACTORS: dict[str, tuple[Callable[[Connection], dict[str, Any]], Path]] = {
    "036": (extract, FROZEN_036_PATH),
    "037": (extract_037, FROZEN_037_PATH),
}


async def _extract_from(url: str, migration: str) -> dict[str, Any]:
    extractor, _ = _EXTRACTORS[migration]
    parsed = make_url(url).set(drivername="postgresql+asyncpg")
    engine = create_async_engine(parsed, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(extractor)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--migration",
        required=True,
        choices=sorted(_EXTRACTORS),
        help="the migration whose downgrade DDL to freeze",
    )
    parser.add_argument(
        "--database-url",
        required=True,
        help="a database freshly upgraded to exactly the migration's down revision",
    )
    parser.add_argument("--output", type=Path, default=None)
    arguments = parser.parse_args(argv)
    document = asyncio.run(_extract_from(arguments.database_url, arguments.migration))
    output = arguments.output or _EXTRACTORS[arguments.migration][1]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(document), encoding="ascii")
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
