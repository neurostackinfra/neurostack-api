from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-package-boundary.py"


def run_check(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path)],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )


def test_allows_package_imports_and_runtime_injected_loader(tmp_path: Path) -> None:
    source = tmp_path / "safe.py"
    source.write_text(
        "from .metrics import enable_metrics\n"
        "import pydantic\n"
        "from .src import local_name\n"
        "def load(loader, module_name):\n"
        "    return loader(module_name)\n",
        encoding="utf-8",
    )
    result = run_check(source)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "source",
    [
        "import src\n",
        "import src.policy\n",
        "import os, src.srm.models as models\n",
        "from src import policy\n",
        "from src.policy import PolicyRule\n",
        "def nested():\n    import src.prompt\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import src.flow\n",
    ],
)
def test_rejects_all_static_src_import_forms(tmp_path: Path, source: str) -> None:
    path = tmp_path / "bad.py"
    path.write_text(source, encoding="utf-8")
    result = run_check(path)
    assert result.returncode == 1
    assert "static src import" in result.stderr


@pytest.mark.parametrize(
    "source",
    [
        "import importlib as harmless\n",
        "from builtins import object as harmless\n",
        "from ordinary import import_module as harmless\n",
        "import ordinary as __builtins__\n",
        "client.__import__\n",
        "client.import_module\n",
        "importlib = object()\n",
        "def builtins():\n    pass\n",
        "class import_module:\n    pass\n",
        "def use(__import__):\n    return 1\n",
        "import ordinary as importlib\n",
        "try:\n    action()\nexcept Exception as import_module:\n    pass\n",
        "call(builtins=value)\n",
        "def nested():\n    return __import__\n",
        "if TYPE_CHECKING:\n    import ordinary as builtins\n",
    ],
)
def test_rejects_reserved_spelling_in_every_syntax_position(
    tmp_path: Path, source: str
) -> None:
    path = tmp_path / "bad.py"
    path.write_text(source, encoding="utf-8")
    result = run_check(path)
    assert result.returncode == 1
    assert "reserved dynamic-import syntax" in result.stderr


def test_scans_every_python_file_below_a_package_path(tmp_path: Path) -> None:
    package = tmp_path / "package"
    (package / "nested").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "nested" / "safe.py").write_text("value = 1\n", encoding="utf-8")
    (package / "nested" / "bad.py").write_text(
        "from src.policy import PolicyRule\n", encoding="utf-8"
    )
    result = run_check(package)
    assert result.returncode == 1
    assert "nested/bad.py" in result.stderr
    assert "static src import" in result.stderr


def test_historical_loop_and_function_input_terminates(tmp_path: Path) -> None:
    path = tmp_path / "finite.py"
    path.write_text(
        "def recursive(loader):\n"
        "    return recursive(loader)\n"
        "while True:\n"
        "    callback = lambda value: value\n",
        encoding="utf-8",
    )
    result = run_check(path)
    assert result.returncode == 0, result.stderr
