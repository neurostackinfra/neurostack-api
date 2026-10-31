"""Credit code imports in a tree without ``tests/`` (neurostack-credit-system).

The production image copies only what its final stage names (``src``,
``config``, ``alembic.ini``, ``alembic``, ``scripts/bootstrap_admin.py``) --
never ``tests/`` (issue #137: ``billing_fact_bundle_digest()`` reads
``tests/fixtures`` and raises there).  Credit code must never read ``tests/``
at import or start-up.  This copies exactly the repository paths the final
stage copies into a scratch tree and, in a subprocess whose ``sys.path``
starts there, imports ``src.models`` and every credit module and builds the
metadata, while an import hook refuses any ``tests`` module and an audit
hook records every file opened under a ``tests`` directory (of any checkout:
the shared virtualenv may put another worktree on ``sys.path``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from tests.scripts.test_production_dockerfile_frontend import (
    _copies,
    _stage,
    docker_ignores,
)

ROOT = Path(__file__).resolve().parents[2]
CREDIT_MODULES = (
    "src.models",
    "src.models.credit",
    "src.models.credit_fact_inbox",
    "src.repositories",
    "src.repositories.credit",
    "src.repositories.credit_fact_ack",
)

_PROBE = r"""
import importlib
import os
import sys

root = os.path.realpath(sys.argv[1])
modules = sys.argv[2:]
sys.path.insert(0, root)
opened = []


def audit(event, args):
    if event != "open" or not args:
        return
    target = args[0]
    if isinstance(target, int):
        return
    try:
        path = os.fsdecode(target)
    except TypeError:
        return
    parts = path.replace("\\", "/").split("/")
    if "tests" in parts:
        opened.append(path)


class RefuseTests:
    def find_spec(self, name, path=None, target=None):
        if name == "tests" or name.startswith("tests."):
            raise ImportError("credit code imported " + name)
        return None


def refuse_reads():
    if opened:
        raise SystemExit("files under tests/ were opened: " + ", ".join(opened))


sys.addaudithook(audit)
sys.meta_path.insert(0, RefuseTests())
for name in modules:
    module = importlib.import_module(name)
    origin = os.path.realpath(module.__file__)
    if not origin.startswith(root + os.sep):
        raise SystemExit(name + " was imported from " + origin)
refuse_reads()

from sqlalchemy import create_engine

from src.models.base import Base
from src.repositories.database import _patch_jsonb_for_sqlite

_patch_jsonb_for_sqlite()
engine = create_engine("sqlite://")
Base.metadata.create_all(engine)
engine.dispose()
refuse_reads()
if any(name == "tests" or name.startswith("tests.") for name in sys.modules):
    raise SystemExit("a tests module was imported")
credit = sorted(t for t in Base.metadata.tables if t.startswith("credit_"))
print("credit tables:", len(credit))
"""


def _image_sources() -> list[str]:
    """Repository paths the production stage copies (no ``--from`` stage)."""

    sources: list[str] = []
    for copy in _copies(_stage("production")):
        if "from" in copy.flags:
            continue
        sources.extend(copy.sources)
    return sources


def _copy_tree(source: Path, destination: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        relative = Path(directory).relative_to(ROOT)
        return {
            name
            for name in names
            if name == "__pycache__" or docker_ignores((relative / name).as_posix())
        }

    if source.is_dir():
        shutil.copytree(source, destination, ignore=ignore)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def test_the_image_never_ships_tests() -> None:
    sources = _image_sources()
    assert {"src", "config", "alembic.ini", "alembic"} <= set(sources)
    assert not any(
        source == "tests" or source.startswith("tests/") for source in sources
    )


def test_credit_modules_import_without_a_tests_tree(tmp_path: Path) -> None:
    image = tmp_path / "app"
    for source in _image_sources():
        _copy_tree(ROOT / source, image / source)
    assert not (image / "tests").exists()
    assert (image / "src" / "models" / "credit.py").is_file()

    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONSTARTUP"}
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE, str(image), *CREDIT_MODULES],
        cwd=image,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    # 15 of migration 038, three 039 inventory, three 040 copy tables and
    # thirteen 041 lifecycle/hold/purge/role tables (Group 5).
    # Migration 042 adds the immutable cutoff-acceptance table.
    assert "credit tables: 35" in completed.stdout


def test_the_probe_detects_a_read_under_tests(tmp_path: Path) -> None:
    """The audit hook is not vacuous: a module that reads tests/ fails."""

    image = tmp_path / "app"
    (image / "src" / "models").mkdir(parents=True)
    (image / "src" / "__init__.py").write_text("", encoding="utf-8")
    (image / "tests").mkdir()
    (image / "tests" / "fixture.txt").write_text("x", encoding="utf-8")
    (image / "src" / "models" / "__init__.py").write_text(
        "from pathlib import Path\n"
        "Path(__file__).resolve().parents[2].joinpath("
        "'tests', 'fixture.txt').read_bytes()\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE, str(image), "src.models"],
        cwd=image,
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert completed.returncode != 0
    assert "files under tests/ were opened" in completed.stdout + completed.stderr
