"""Render the read-only readback of migration 037's preflight.

The query (``scripts/migration_037_readback.sql``) is generated from the
migration itself (``readback_sql``), after its pinned frozen-file check, so
the operator's readback and the migration run the same checks.  Regenerate
only after a reviewed change to the migration::

    python -m scripts.migration_037_readback --output scripts/migration_037_readback.sql

``tests/test_migration_037_drop_legacy.py`` fails while the committed file
differs from what this renders.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "alembic"
    / "versions"
    / "20261020_000001_037_drop_legacy_billing_migration_objects.py"
)
READBACK_PATH = ROOT / "scripts" / "migration_037_readback.sql"


def load_migration() -> ModuleType:
    """The migration module, loaded by path.

    From the repository root the ``alembic/`` directory shadows the installed
    package; the module only needs the name ``op`` to import.
    """

    import alembic as alembic_package

    if not hasattr(alembic_package, "op"):
        setattr(alembic_package, "op", SimpleNamespace())
    spec = importlib.util.spec_from_file_location("migration_037", MIGRATION)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {MIGRATION}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def render() -> str:
    migration = load_migration()
    return str(migration.readback_sql(migration.frozen_036()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=None)
    arguments = parser.parse_args(argv)
    sql = render()
    if arguments.output is None:
        sys.stdout.write(sql)
    else:
        arguments.output.write_text(sql, encoding="ascii")
        print(f"wrote {arguments.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
