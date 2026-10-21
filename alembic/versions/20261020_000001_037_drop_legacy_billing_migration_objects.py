"""drop the legacy AbacatePay migration saga and roster objects

Revision ID: 037
Revises: 036
Create Date: 2026-10-20 00:00:01

Group 10 phase E6 ("remover objetos legados"; owner decisions of 2026-10-17
and 2026-10-18 in the asaas-payment-gateway wish).  036 decoupled every ASAAS
table from the legacy saga; what it left behind is dropped here, whole:

* the four legacy tables ``billing_migrations``,
  ``billing_migration_receipts``, ``billing_migration_absence_reads`` and
  ``billing_roster_control`` -- with them go their columns (the saga's
  ``source_roster_revision`` and the roster's ``billing_roster_revision``),
  their 38 constraints (the CHECK that lists the legacy receipt kinds
  ``roster_import``/``roster_cutover``/``roster_disposal`` included), two
  plain indexes, twelve triggers, the roster's seed row and the RI triggers
  their foreign keys hold on ``builders``, ``billing_subscriptions`` and
  ``billing_checkout_sessions``;
* the ten functions only those tables use (``DROPPED_FUNCTIONS``): their
  trigger functions -- ``validate_billing_migration_receipt`` and
  ``validate_billing_migration_absence_read`` read columns 036 dropped and
  already fail closed --, the CHECK helper
  ``billing_migration_snapshot_is_valid`` and the legacy provider-evidence
  digest (``billing_provider_evidence_digest`` and
  ``billing_evidence_timestamp``), which only those validators call.

Not touched: no live table, column, constraint or index (``abacatepay`` and
NULL providers, the legacy subscription statuses and the operation scope
``migration`` stay in their CHECKs: owner decision "sem os endurecimentos
opcionais"); ``billing_snapshot_has_no_pii`` and
``reject_immutable_billing_row`` stay (live CHECKs and triggers use them).

Preflight (upgrade): one ``DO`` block, under ``ACCESS EXCLUSIVE`` locks on
the four tables and on the three live tables their foreign keys reference
(dropping those keys locks them anyway), that refuses with SQLSTATE 55000
and one message listing every failing check, changing nothing, when:

* the revision is not exactly 036;
* any row exists in the three saga tables, or the roster revision is not 0;
* one of the ten functions does not exist;
* any object other than the 64 the frozen file restores depends on a
  dropped table, row type or function (``pg_depend``);
* any function outside the drop set, in any non-system schema, names a
  dropped table or function in its source (case-insensitive; PL/pgSQL bodies
  are invisible to ``pg_depend``);
* a dropped table has a column the frozen file does not restore;
* a dropped table or function is not owned by the role running the
  migration, or its access list (column ACLs included) is not, as a set of
  privileges, what a table or function that role creates now gets (read from
  a probe table and function created and dropped inside the block);
* a dropped object or its row type carries a comment.

What it does NOT compare -- each restored as the frozen 036 definition,
harmless for a database built only from migrations 026-036, but not
detected: SECURITY DEFINER, a ``SET`` option or any other change of a dropped
function's definition, a disabled trigger, a changed column type or default,
storage options (fillfactor), row-level security flags, and a constraint,
index, trigger or function redefined under the same name.  Function sources
are matched as text, so a reference built by concatenation in dynamic SQL
(``EXECUTE 'SELECT ... FROM billing_' || 'migrations'``) cannot be detected
by this or any text scan.  The access-list order is not compared: the
downgrade gives the restored objects the canonical order PostgreSQL stores
for new objects.

``readback_sql()`` renders the same checks as one read-only query
(``scripts/migration_037_readback.sql``) for the operator's readback before
the window; only the probe differs, replaced there by the role's default
privileges read from ``pg_default_acl`` (the rule PostgreSQL applies to a new
object), and it takes only the read locks of its row counts.

No readiness-receipt check: unlike 036, 037 changes no live column, so no
content digest or receipt changes meaning.  Whether 036 was verified in
production first is a procedural rule (the production window is pinned by
exact revision), not a check: a fresh database in development and CI runs
the whole chain, 036 and 037 included, in one transaction.

Lock wait (both directions): the first statement is ``LOCK_TIMEOUT``
(``SET LOCAL lock_timeout = '5s'``, as 024, 025 and 036).  A timeout raises
SQLSTATE 55P03, and a lock cycle with another session SQLSTATE 40P01, inside
the migration transaction, so nothing changed: wait for the blocking
transaction to end and run the migration again.

Downgrade (real, owner decision): restores the 036 objects -- the four
tables (empty), the ten functions, their constraints, indexes and triggers,
and the roster's seed row at revision 0, the value the upgrade required --
from ``alembic/frozen/037_downgrade_restores_036.json``, which
``scripts/alembic_frozen_ddl.py`` extracted from a fresh 036 database and
whose SHA-256 is pinned below; a changed byte is refused before any SQL.  It
needs no data it would have to invent, so it refuses only structural states:
a revision other than 037, or an existing relation, type or function whose
name the restore needs.  The restored objects belong to the role running the
downgrade and get its default privileges, which is what the upgrade's
privilege check required of the objects it dropped.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Union

import sqlalchemy as sa

from alembic import op

revision: str = "037"
down_revision: Union[str, None] = "036"
branch_labels = None
depends_on = None

FROZEN_036 = (
    Path(__file__).resolve().parents[1] / "frozen" / "037_downgrade_restores_036.json"
)
FROZEN_036_SHA256 = "58861488e43be0e7d26247f86eb968cf7192377ad994de051c77958ea2fdd7ae"
# First statement of both directions; see "Lock wait" in the module docstring.
LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"

DROPPED_TABLES = (
    "billing_roster_control",
    "billing_migrations",
    "billing_migration_receipts",
    "billing_migration_absence_reads",
)
# Exact signatures, in creation order (the downgrade restores them so).
DROPPED_FUNCTIONS = (
    "billing_migration_snapshot_is_valid(jsonb,text,text,text,text,text,bigint)",
    "lock_billing_roster_statement()",
    "validate_billing_migration_receipt()",
    "validate_billing_migration_receipt_commit()",
    "protect_bound_billing_migration_target()",
    "protect_billing_roster_control()",
    "validate_billing_provider_evidence()",
    "billing_evidence_timestamp(timestamp with time zone)",
    "billing_provider_evidence_digest(text,text,timestamp with time zone,text,"
    "uuid,timestamp with time zone,text,uuid,text,text)",
    "validate_billing_migration_absence_read()",
)
# Live tables the legacy foreign keys reference: dropping those keys (and
# re-adding them in the downgrade) changes their RI triggers, which locks
# them; both preflights take the lock first, bounded by LOCK_TIMEOUT.
REFERENCED_TABLES = ("builders", "billing_subscriptions", "billing_checkout_sessions")
UPGRADE_LOCKED = (*DROPPED_TABLES, *REFERENCED_TABLES)
DOWNGRADE_LOCKED = REFERENCED_TABLES

# Restore order.  Functions are validated when created: the PL/pgSQL ones
# declare ``billing_migrations%ROWTYPE``, so the tables come first; the CHECK
# calling billing_migration_snapshot_is_valid() follows the functions.
_KINDS = ("tables", "functions", "constraints", "indexes", "rows", "triggers")


def _execute(statements: tuple[str, ...]) -> None:
    for statement in statements:
        ddl = sa.text(statement)
        if ddl._bindparams:
            bind_names = ", ".join(sorted(ddl._bindparams))
            raise ValueError(
                f"raw migration DDL contains bind parameters: {bind_names}"
            )
        op.execute(ddl)


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _text_array(values: list[str] | tuple[str, ...]) -> str:
    return "ARRAY[" + ", ".join(_literal(value) for value in values) + "]::text[]"


def frozen_036(raw: bytes | None = None) -> dict[str, Any]:
    """The frozen 036 document, after the pinned digest check."""

    content = FROZEN_036.read_bytes() if raw is None else raw
    digest = hashlib.sha256(content).hexdigest()
    if digest != FROZEN_036_SHA256:
        raise RuntimeError(
            f"{FROZEN_036.name} has SHA-256 {digest}, migration 037 pins "
            f"{FROZEN_036_SHA256}; the frozen 036 DDL was changed"
        )
    document: dict[str, Any] = json.loads(content)
    if document.get("revision") != "036":
        raise RuntimeError(f"{FROZEN_036.name} does not freeze revision 036")
    if [entry["table"] for entry in document["tables"]] != list(DROPPED_TABLES) or [
        entry["signature"] for entry in document["functions"]
    ] != list(DROPPED_FUNCTIONS):
        raise RuntimeError(f"{FROZEN_036.name} does not match migration 037's scope")
    return document


def restore_statements(document: dict[str, Any]) -> tuple[str, ...]:
    """The frozen statements in restore order: tables, functions,
    constraints (foreign keys last), indexes, rows, triggers."""

    return tuple(entry["statement"] for kind in _KINDS for entry in document[kind])


def frozen_036_statements(raw: bytes | None = None) -> tuple[str, ...]:
    """The frozen 036 DDL, in restore order, after the pinned digest check."""

    return restore_statements(frozen_036(raw))


def expected_dependents(document: dict[str, Any]) -> list[str]:
    """``pg_identify_object`` type and identity of every object that may
    depend on the drop set: exactly the ones the downgrade restores."""

    identities = [
        f"table constraint {entry['name']} on public.{entry['table']}"
        for entry in document["constraints"]
    ]
    for entry in document["triggers"]:
        identities.append(f"trigger {entry['name']} on public.{entry['table']}")
        if entry["constraint"]:
            # A constraint trigger's own pg_constraint row.
            identities.append(
                f"table constraint {entry['name']} on public.{entry['table']}"
            )
    identities += [f"index public.{entry['name']}" for entry in document["indexes"]]
    identities += [
        f"default value for public.{entry['table']}.{column}"
        for entry in document["tables"]
        for column in entry["defaults"]
    ]
    return sorted(identities)


def restored_columns(document: dict[str, Any]) -> list[str]:
    return [
        f"{entry['table']}.{column}"
        for entry in document["tables"]
        for column in entry["columns"]
    ]


def restored_relations(document: dict[str, Any]) -> list[str]:
    """Relation names the downgrade creates in ``public`` (tables and the
    indexes of their keys and plain indexes)."""

    return [
        *(entry["table"] for entry in document["tables"]),
        *(entry["name"] for entry in document["constraints"] if entry["owns_index"]),
        *(entry["name"] for entry in document["indexes"]),
    ]


# PL/pgSQL bodies reference tables and functions by name only; pg_depend
# cannot see them.  A word-bounded, case-insensitive match of any dropped name
# in the source of a function outside the drop set refuses the upgrade.
_DROPPED_NAMES_PATTERN = (
    r"\m("
    + "|".join(
        [*DROPPED_TABLES, *(signature.split("(")[0] for signature in DROPPED_FUNCTIONS)]
    )
    + r")\M"
)
_CURRENT_ROLE = "(SELECT oid FROM pg_roles WHERE rolname = current_user)"


def _acl_set(acl: str) -> str:
    """An access list as a sorted text array of its privileges (order and
    grouping of the aclitems do not matter, only who got what from whom)."""

    return (
        "ARRAY(SELECT format('%s>%s:%s:%s', grantor, grantee, privilege_type, "
        f"bool_or(is_grantable)) FROM aclexplode({_explodable(acl)}) "
        "GROUP BY grantor, grantee, privilege_type ORDER BY 1)"
    )


def _explodable(acl: str) -> str:
    """aclexplode() rejects an empty (zero-dimensional) array; NULL yields no
    row, which is what an empty access list means."""

    return f"nullif({acl}, '{{}}'::aclitem[])"


def _default_acl_set(kind: str) -> str:
    """What a new table (``r``) or function (``f``) in ``public`` created by
    the current role gets, as PostgreSQL computes it (get_user_default_acl):
    the role's global default-privilege entry, or the built-in default when
    it has none, merged (privileges and grant options OR-ed) with its entry
    for schema ``public``; a merged list that is empty -- a global entry
    ``{}`` and no schema entry -- is stored as NULL, which is the built-in
    default again."""

    def entry(namespace: str) -> str:
        return (
            "(SELECT defaclacl FROM pg_default_acl "
            f"WHERE defaclrole = {_CURRENT_ROLE} AND defaclnamespace = {namespace} "
            f"AND defaclobjtype = '{kind}')"
        )

    global_entry = entry("0")
    schema_entry = entry("'public'::regnamespace")
    built_in = f"acldefault({kind!r}, {_CURRENT_ROLE})"
    merged = (
        "ARRAY(SELECT format('%s>%s:%s:%s', grantor, grantee, privilege_type, "
        "bool_or(is_grantable)) FROM ("
        "SELECT * FROM aclexplode("
        f"{_explodable(f'coalesce({global_entry}, {built_in})')}"
        ") UNION ALL "
        f"SELECT * FROM aclexplode({_explodable(schema_entry)})"
        ") AS granted GROUP BY grantor, grantee, privilege_type ORDER BY 1)"
    )
    return (
        "(SELECT CASE WHEN cardinality(merged.privileges) = 0 "
        f"THEN {_acl_set(built_in)} ELSE merged.privileges END "
        f"FROM (SELECT {merged} AS privileges) AS merged)"
    )


def _scope_ctes() -> str:
    """``scope``: the OIDs of the drop set that exist (NULL-free arrays, so no
    ``<> ALL`` below turns NULL) and the missing functions; ``scope_types``:
    the four row types and their array types."""

    tables = _text_array([f"public.{table}" for table in DROPPED_TABLES])
    functions = _text_array([f"public.{function}" for function in DROPPED_FUNCTIONS])
    return f"""scope AS (
    SELECT ARRAY(SELECT to_regclass(name)::oid FROM unnest({tables}) AS name
                 WHERE to_regclass(name) IS NOT NULL) AS tables,
           ARRAY(SELECT to_regprocedure(name)::oid FROM unnest({functions}) AS name
                 WHERE to_regprocedure(name) IS NOT NULL) AS functions,
           (SELECT string_agg(name, ', ' ORDER BY position)
            FROM unnest({functions}) WITH ORDINALITY AS listed(name, position)
            WHERE to_regprocedure(name) IS NULL) AS missing_functions
),
scope_types AS (
    SELECT ARRAY(
        SELECT reltype FROM pg_class WHERE oid = ANY(scope.tables)
        UNION ALL
        SELECT typarray FROM pg_type WHERE oid IN (
            SELECT reltype FROM pg_class WHERE oid = ANY(scope.tables))
    ) AS types
    FROM scope
)"""


def _checks(
    document: dict[str, Any], *, role: str, table_acl: str, function_acl: str
) -> list[tuple[str, str]]:
    """``(name, expression)`` of every upgrade check, in refusal order.  Each
    expression yields the refusal line or NULL; it reads ``scope`` and
    ``scope_types``.  *role* is the OID of the role that would recreate the
    objects, *table_acl*/*function_acl* the privilege sets it would give."""

    def rows(table: str, condition: str = "") -> str:
        where = f" WHERE {condition}" if condition else ""
        return (
            f"(SELECT format('{table} has %s row(s)', count(*)) FROM {table}{where} "
            "HAVING count(*) > 0)"
        )

    dependents = f"""(SELECT 'unexpected objects depend on the dropped objects: '
            || string_agg(DISTINCT dependent, ', ' ORDER BY dependent)
        FROM (
            SELECT (pg_identify_object(dep.classid, dep.objid, 0)).type || ' '
                   || (pg_identify_object(dep.classid, dep.objid, 0)).identity
                   AS dependent
            FROM pg_depend AS dep
            WHERE dep.deptype IN ('n', 'a')
              AND ((dep.refclassid = 'pg_class'::regclass
                    AND dep.refobjid = ANY(scope.tables))
                   OR (dep.refclassid = 'pg_proc'::regclass
                       AND dep.refobjid = ANY(scope.functions))
                   OR (dep.refclassid = 'pg_type'::regclass
                       AND dep.refobjid = ANY(scope_types.types)))
              AND NOT (dep.classid = 'pg_class'::regclass
                       AND dep.objid = ANY(scope.tables))
              AND NOT (dep.classid = 'pg_proc'::regclass
                       AND dep.objid = ANY(scope.functions))
              AND NOT (dep.classid = 'pg_type'::regclass
                       AND dep.objid = ANY(scope_types.types))
        ) AS dependents
        WHERE dependent <> ALL ({_text_array(expected_dependents(document))}))"""
    bodies = f"""(SELECT 'functions outside 037''s scope name a dropped object: '
            || string_agg(p.oid::regprocedure::text, ', '
                          ORDER BY p.oid::regprocedure::text)
        FROM pg_proc AS p
        JOIN pg_namespace AS n ON n.oid = p.pronamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND p.oid <> ALL (scope.functions)
          AND p.prosrc ~* {_literal(_DROPPED_NAMES_PATTERN)})"""
    columns = f"""(SELECT 'columns the downgrade would not restore: '
            || string_agg(column_name, ', ' ORDER BY column_name)
        FROM (
            SELECT c.relname || '.' || a.attname AS column_name
            FROM pg_attribute AS a
            JOIN pg_class AS c ON c.oid = a.attrelid
            WHERE a.attrelid = ANY(scope.tables)
              AND a.attnum > 0 AND NOT a.attisdropped
        ) AS columns
        WHERE column_name <> ALL ({_text_array(restored_columns(document))}))"""
    legacy_table_acl = _acl_set("coalesce(c.relacl, acldefault('r', c.relowner))")
    legacy_function_acl = _acl_set("coalesce(p.proacl, acldefault('f', p.proowner))")
    privileges = f"""(SELECT format(
                'owners or privileges the downgrade would not recreate as %s: %s',
                current_user, string_agg(item, ', ' ORDER BY item))
        FROM (
            SELECT format('table %s (owner %s, acl %s)', c.relname,
                          c.relowner::regrole, coalesce(c.relacl::text, 'default'))
                   AS item
            FROM pg_class AS c
            WHERE c.oid = ANY(scope.tables)
              AND (c.relowner <> {role}
                   OR {legacy_table_acl} <> {table_acl}
                   OR EXISTS (SELECT 1 FROM pg_attribute AS a
                              WHERE a.attrelid = c.oid AND a.attnum > 0
                                AND a.attacl IS NOT NULL))
            UNION ALL
            SELECT format('function %s (owner %s, acl %s)', p.oid::regprocedure,
                          p.proowner::regrole, coalesce(p.proacl::text, 'default'))
            FROM pg_proc AS p
            WHERE p.oid = ANY(scope.functions)
              AND (p.proowner <> {role}
                   OR {legacy_function_acl} <> {function_acl})
        ) AS differing
        HAVING count(*) > 0)"""
    comments = """(SELECT 'comments the downgrade would not restore: '
            || string_agg(described, ', ' ORDER BY described)
        FROM (
            SELECT (pg_identify_object(d.classoid, d.objoid, d.objsubid)).type
                   || ' '
                   || (pg_identify_object(d.classoid, d.objoid, d.objsubid)).identity
                   AS described
            FROM pg_description AS d
            WHERE (d.classoid = 'pg_class'::regclass
                   AND (d.objoid = ANY(scope.tables)
                        OR d.objoid IN (SELECT indexrelid FROM pg_index
                                        WHERE indrelid = ANY(scope.tables))))
               OR (d.classoid = 'pg_proc'::regclass
                   AND d.objoid = ANY(scope.functions))
               OR (d.classoid = 'pg_type'::regclass
                   AND d.objoid = ANY(scope_types.types))
               OR (d.classoid = 'pg_constraint'::regclass
                   AND d.objoid IN (SELECT oid FROM pg_constraint
                                    WHERE conrelid = ANY(scope.tables)))
               OR (d.classoid = 'pg_trigger'::regclass
                   AND d.objoid IN (SELECT oid FROM pg_trigger
                                    WHERE tgrelid = ANY(scope.tables)))
        ) AS comments)"""
    return [
        (
            "revision",
            "(SELECT format('alembic_version is %s, not 036', "
            "coalesce(string_agg(version_num, ', ' ORDER BY version_num), 'empty')) "
            "FROM alembic_version HAVING string_agg(version_num, ', ' "
            "ORDER BY version_num) IS DISTINCT FROM '036')",
        ),
        ("billing_migrations rows", rows("billing_migrations")),
        ("billing_migration_receipts rows", rows("billing_migration_receipts")),
        (
            "billing_migration_absence_reads rows",
            rows("billing_migration_absence_reads"),
        ),
        (
            "roster revision",
            "(SELECT format('billing_roster_control has %s row(s) whose revision "
            "is not 0', count(*)) FROM billing_roster_control "
            "WHERE billing_roster_revision <> 0 HAVING count(*) > 0)",
        ),
        (
            "functions to drop exist",
            "'functions 037 drops do not exist: ' || scope.missing_functions",
        ),
        ("catalog dependents", dependents),
        ("function sources", bodies),
        ("columns", columns),
        ("owners and privileges", privileges),
        ("comments", comments),
    ]


def upgrade_preflight(document: dict[str, Any]) -> str:
    checks = _checks(
        document,
        role="probe_owner",
        table_acl=_acl_set("coalesce(probe_table_acl, acldefault('r', probe_owner))"),
        function_acl=_acl_set(
            "coalesce(probe_function_acl, acldefault('f', probe_owner))"
        ),
    )
    listed = ",\n        ".join(expression for _, expression in checks)
    return f"""
DO $preflight$
DECLARE
    problems TEXT[];
    probe TEXT := format('migration_037_privilege_probe_%s', txid_current());
    probe_owner OID;
    probe_table_acl ACLITEM[];
    probe_function_acl ACLITEM[];
BEGIN
    LOCK TABLE {", ".join(UPGRADE_LOCKED)}
        IN ACCESS EXCLUSIVE MODE;
    -- What a table and a function the running role creates now get.
    EXECUTE format('CREATE TABLE public.%I ()', probe);
    SELECT relowner, relacl INTO probe_owner, probe_table_acl
    FROM pg_class WHERE oid = to_regclass(format('public.%I', probe));
    EXECUTE format('DROP TABLE public.%I', probe);
    EXECUTE format(
        'CREATE FUNCTION public.%I() RETURNS void LANGUAGE sql AS %L', probe, '');
    SELECT proacl INTO probe_function_acl
    FROM pg_proc WHERE oid = to_regprocedure(format('public.%I()', probe));
    EXECUTE format('DROP FUNCTION public.%I()', probe);

    WITH {_scope_ctes()}
    SELECT array_remove(ARRAY[
        {listed}
    ], NULL)
    INTO problems
    FROM scope, scope_types;

    IF cardinality(problems) > 0 THEN
        RAISE EXCEPTION 'migration 037 refused (nothing was changed): %',
            array_to_string(problems, '; ')
            USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
END
$preflight$
"""


READBACK_HEADER = """\
-- Read-only readback before migration 037 (Group 10 E6), generated from the
-- migration by scripts/migration_037_readback.py; never edit by hand.
--
-- Run it connected as the role that will run the migration, inside a READ
-- ONLY transaction, immediately before the window:
--   BEGIN READ ONLY; <this query>; ROLLBACK;
-- One row per check of the migration's preflight, in its order: verdict
-- 'ok', or the exact line the migration's refusal would carry.  Every
-- verdict 'ok' = the preflight passes for this state of the database.  It
-- reads alembic_version and the four legacy tables, so it must run as the
-- migration role (as another role the privilege row answers for that role,
-- and a role without SELECT on those tables gets "permission denied").  It
-- holds ordinary read locks on them while its transaction is open: never
-- leave that transaction open (the migration's ACCESS EXCLUSIVE lock would
-- wait on it and give up with 55P03).  The state can change before the run.
-- Where the migration creates and drops a probe table and function to read
-- what the role's new objects get, this computes that from pg_default_acl.
-- At revision 035 three rows are red (revision, catalog dependents, function
-- sources); at 037 it stops with: relation "billing_migrations" does not
-- exist."""


def readback_sql(document: dict[str, Any]) -> str:
    """The upgrade checks as one read-only query (see ``READBACK_HEADER``)."""

    checks = _checks(
        document,
        role=_CURRENT_ROLE,
        table_acl=_default_acl_set("r"),
        function_acl=_default_acl_set("f"),
    )
    rows = ",\n    ".join(
        f"({number}, {_literal(name)}, {expression})"
        for number, (name, expression) in enumerate(checks, start=1)
    )
    return f"""{READBACK_HEADER}
WITH {_scope_ctes()}
SELECT check_no, check_name, coalesce(problem, 'ok') AS verdict
FROM scope, scope_types, LATERAL (VALUES
    {rows}
) AS checks(check_no, check_name, problem)
ORDER BY check_no;
"""


# No CASCADE: an object the preflight did not foresee makes the statement
# fail (nothing changed) instead of vanishing.
UPGRADE_DROPS = (
    f"DROP TABLE {', '.join(reversed(DROPPED_TABLES))}",
    f"DROP FUNCTION {', '.join(reversed(DROPPED_FUNCTIONS))}",
)


def downgrade_preflight(document: dict[str, Any]) -> str:
    signatures = _text_array(
        [f"public.{entry['signature']}" for entry in document["functions"]]
    )
    return f"""
DO $preflight$
DECLARE
    problems TEXT[] := ARRAY[]::TEXT[];
    listed TEXT;
BEGIN
    LOCK TABLE {", ".join(DOWNGRADE_LOCKED)}
        IN ACCESS EXCLUSIVE MODE;
    SELECT string_agg(version_num, ', ' ORDER BY version_num) INTO listed
    FROM alembic_version;
    IF listed IS DISTINCT FROM '037' THEN
        problems := problems || format(
            'alembic_version is %s, not 037', coalesce(listed, 'empty'));
    END IF;
    SELECT string_agg(existing, ', ' ORDER BY existing) INTO listed
    FROM (
        SELECT 'relation ' || relname AS existing FROM pg_class
        WHERE relnamespace = 'public'::regnamespace
          AND relname = ANY({_text_array(restored_relations(document))})
        UNION ALL
        SELECT 'type ' || typname FROM pg_type
        WHERE typnamespace = 'public'::regnamespace
          AND typname = ANY({_text_array(list(DROPPED_TABLES))})
          AND typrelid = 0
        UNION ALL
        SELECT 'function ' || to_regprocedure(name)::text
        FROM unnest({signatures}) AS name
        WHERE to_regprocedure(name) IS NOT NULL
    ) AS collisions;
    IF listed IS NOT NULL THEN
        problems := problems || format(
            'objects the restore would create already exist: %s', listed);
    END IF;
    IF cardinality(problems) > 0 THEN
        RAISE EXCEPTION 'downgrade of migration 037 refused (nothing was changed): %',
            array_to_string(problems, '; ')
            USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
END
$preflight$
"""


def upgrade() -> None:
    document = frozen_036()
    _execute((LOCK_TIMEOUT, upgrade_preflight(document), *UPGRADE_DROPS))


def downgrade() -> None:
    document = frozen_036()
    _execute(
        (LOCK_TIMEOUT, downgrade_preflight(document), *restore_statements(document))
    )
