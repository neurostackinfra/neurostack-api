from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-package-boundary.py"
HELPER = ROOT / "scripts" / "_forbidden_imports.py"

spec = importlib.util.spec_from_file_location("_forbidden_imports_test", HELPER)
assert spec is not None and spec.loader is not None
helper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = helper
spec.loader.exec_module(helper)

try:
    ast.parse("def generic[T]():\n    pass\n")
except SyntaxError:
    TYPE_PARAMETER_FALSIFIERS: tuple[str, ...] = ()
else:
    TYPE_PARAMETER_FALSIFIERS = (
        'from importlib.resources import files\nfiles("neurostack_core.presets")\ndef generic[files]():\n    pass\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets")\ndef generic[**files]():\n    pass\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets")\ndef generic[*files]():\n    pass\n',
    )


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


@pytest.mark.parametrize(
    "argument",
    [
        '"neurostack_core.presets"',
        '"neurostack_core." "presets"',
    ],
)
def test_allows_exact_packaged_resource_grammar(tmp_path: Path, argument: str) -> None:
    path = tmp_path / "resources.py"
    path.write_text(
        "from importlib.resources import files\n" f"value = files({argument})\n",
        encoding="utf-8",
    )
    result = run_check(path)
    assert result.returncode == 0, result.stderr


def test_allows_multiple_exact_packaged_resource_calls(tmp_path: Path) -> None:
    path = tmp_path / "resources.py"
    path.write_text(
        "from importlib.resources import files\n"
        'first = files("neurostack_core.presets")\n'
        'second = files("neurostack_core.presets")\n',
        encoding="utf-8",
    )
    result = run_check(path)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "resource_import",
    [
        "from importlib.resources import files as resource_files\n",
        "from importlib.resources import as_file\n",
    ],
)
def test_valid_call_does_not_activate_invalid_resource_import_shape(
    tmp_path: Path, resource_import: str
) -> None:
    path = tmp_path / "invalid_import_shape.py"
    path.write_text(
        resource_import + 'files("neurostack_core.presets")\n',
        encoding="utf-8",
    )
    result = run_check(path)
    assert result.returncode == 1
    assert "reserved dynamic-import syntax" in result.stderr
    assert "static src import" not in result.stderr


def test_rejects_approved_anchor_called_through_other_name(tmp_path: Path) -> None:
    path = tmp_path / "other_call_target.py"
    path.write_text(
        "from importlib.resources import files\n" 'other("neurostack_core.presets")\n',
        encoding="utf-8",
    )
    result = run_check(path)
    assert result.returncode == 1
    assert "reserved dynamic-import syntax" in result.stderr
    assert "static src import" not in result.stderr


def test_rejects_files_imported_from_resource_submodule(tmp_path: Path) -> None:
    path = tmp_path / "resource_submodule.py"
    path.write_text(
        "from importlib.resources.extra import files\n"
        'files("neurostack_core.presets")\n',
        encoding="utf-8",
    )
    result = run_check(path)
    assert result.returncode == 1
    assert "reserved dynamic-import syntax" in result.stderr
    assert "static src import" not in result.stderr


def test_rejects_standalone_files_attribute_with_valid_resource_grammar(
    tmp_path: Path,
) -> None:
    path = tmp_path / "standalone_attribute.py"
    path.write_text(
        "from importlib.resources import files\n"
        'files("neurostack_core.presets")\n'
        "client.files\n",
        encoding="utf-8",
    )
    result = run_check(path)
    assert result.returncode == 1
    assert "reserved dynamic-import syntax" in result.stderr
    assert "static src import" not in result.stderr


def test_resource_policy_is_an_explicit_helper_opt_in() -> None:
    tree = ast.parse(
        'from importlib.resources import files\nfiles("neurostack_core.presets")\n'
    )
    assert helper.find_forbidden_imports(tree)
    assert not helper.find_forbidden_imports(tree, allow_packaged_resources=True)


@pytest.mark.parametrize(
    "source",
    [
        # Import count, placement, and shape.
        'from importlib.resources import files\nfrom importlib.resources import files\nfiles("neurostack_core.presets")\n',
        'if enabled:\n    from importlib.resources import files\nfiles("neurostack_core.presets")\n',
        'from importlib.resources import files as files\nfiles("neurostack_core.presets")\n',
        'from importlib.resources import files as resource_files\nresource_files("neurostack_core.presets")\n',
        'from importlib.resources import files, as_file\nfiles("neurostack_core.presets")\n',
        'from importlib.resources import *\nfiles("neurostack_core.presets")\n',
        'from .importlib.resources import files\nfiles("neurostack_core.presets")\n',
        "from importlib.resources import as_file\n",
        'import importlib.resources\nfiles("neurostack_core.presets")\n',
        'import importlib.resources\nimportlib.resources.files("neurostack_core.presets")\n',
        'from importlib import resources\nfiles("neurostack_core.presets")\n',
        "from importlib.resources import files\n",
        # Call target, anchor, and arguments.
        'from importlib.resources import files\nfiles.__call__("neurostack_core.presets")\n',
        'from importlib.resources import files\nfiles("src")\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets.extra")\n',
        'from importlib.resources import files\nanchor = "neurostack_core.presets"\nfiles(anchor)\n',
        'from importlib.resources import files\nfiles("neurostack_core." + "presets")\n',
        'from importlib.resources import files\nfiles(f"neurostack_core.presets")\n',
        "from importlib.resources import files\nfiles()\n",
        'from importlib.resources import files\nfiles("neurostack_core.presets", 1)\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets", package="extra")\n',
        'from importlib.resources import files\nfiles(package="neurostack_core.presets")\n',
        'from importlib.resources import files\nfiles(*["neurostack_core.presets"])\n',
        'from importlib.resources import files\nfiles(**{"package": "neurostack_core.presets"})\n',
        # Attribute, non-call, storage, passing, returning, and rebinding uses.
        'from importlib.resources import files\nfiles("neurostack_core.presets")\nfiles.name\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets")\nfiles\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets")\nsaved = files\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets")\nsink(files)\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets")\ndef get():\n    return files\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets")\nfiles = sink\n',
        # Other direct identifier-bearing fields.
        'from importlib.resources import files\nfiles("neurostack_core.presets")\ndef files():\n    pass\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets")\nasync def files():\n    pass\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets")\nclass files:\n    pass\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets")\ndef f(files):\n    pass\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets")\ntry:\n    act()\nexcept Exception as files:\n    pass\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets")\nglobal files\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets")\ndef outer():\n    files = 1\n    def inner():\n        nonlocal files\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets")\nmatch value:\n    case files:\n        pass\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets")\nmatch value:\n    case [*files]:\n        pass\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets")\nmatch value:\n    case {"key": _, **files}:\n        pass\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets")\nmatch value:\n    case Point(files=value):\n        pass\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets")\nimport ordinary as files\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets")\nimport files\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets")\nfrom ordinary.files import value\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets")\nfrom ordinary import files\n',
        'from importlib.resources import files\nfiles("neurostack_core.presets")\nsink(files=value)\n',
        *TYPE_PARAMETER_FALSIFIERS,
    ],
)
def test_rejects_resource_grammar_falsifiers(tmp_path: Path, source: str) -> None:
    path = tmp_path / "bad_resource.py"
    path.write_text(source, encoding="utf-8")
    result = run_check(path)
    assert result.returncode == 1
    assert "reserved dynamic-import syntax" in result.stderr


@pytest.mark.parametrize(
    "invalid_use",
    [
        pytest.param('files("src")\n', id="other-literal"),
        pytest.param(
            'anchor = "neurostack_core.presets"\nfiles(anchor)\n',
            id="variable-anchor",
        ),
        pytest.param('files("neurostack_core." + "presets")\n', id="runtime-concat"),
        pytest.param('files(f"neurostack_core.presets")\n', id="f-string"),
        pytest.param("files()\n", id="zero-arguments"),
        pytest.param('files("neurostack_core.presets", 1)\n', id="extra-positional"),
        pytest.param(
            'files("neurostack_core.presets", package="extra")\n',
            id="extra-keyword",
        ),
        pytest.param(
            'args = ["neurostack_core.presets"]\nfiles(*args)\n', id="star-args"
        ),
        pytest.param(
            'kwargs = {"package": "neurostack_core.presets"}\nfiles(**kwargs)\n',
            id="star-star-kwargs",
        ),
        pytest.param('files.__call__("neurostack_core.presets")\n', id="dunder-call"),
        pytest.param("files.name\n", id="attribute"),
        pytest.param("files\n", id="non-call"),
        pytest.param("sink(files)\n", id="passed"),
        pytest.param("def get():\n    return files\n", id="returned"),
        pytest.param("saved = files\n", id="stored"),
        pytest.param("files = sink\n", id="rebound"),
    ],
)
def test_valid_resource_call_does_not_hide_invalid_direct_files_use(
    tmp_path: Path, invalid_use: str
) -> None:
    path = tmp_path / "mixed_files_use.py"
    path.write_text(
        "from importlib.resources import files\n"
        'files("neurostack_core.presets")\n' + invalid_use,
        encoding="utf-8",
    )
    result = run_check(path)
    assert result.returncode == 1
    assert "reserved dynamic-import syntax" in result.stderr
    assert "static src import" not in result.stderr


def test_valid_resource_syntax_does_not_hide_static_src(tmp_path: Path) -> None:
    path = tmp_path / "mixed.py"
    path.write_text(
        "from importlib.resources import files\n"
        'files("neurostack_core.presets")\n'
        "import src.policy\n",
        encoding="utf-8",
    )
    result = run_check(path)
    assert result.returncode == 1
    assert "static src import" in result.stderr
    assert "reserved dynamic-import syntax" not in result.stderr


def test_valid_resource_syntax_does_not_hide_independent_reserved_import(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mixed_reserved.py"
    path.write_text(
        "from importlib.resources import files\n"
        'files("neurostack_core.presets")\n'
        "import importlib\n",
        encoding="utf-8",
    )
    result = run_check(path)
    assert result.returncode == 1
    assert "reserved dynamic-import syntax" in result.stderr
    assert "static src import" not in result.stderr


def test_reflective_and_runtime_injected_cases_remain_out_of_scope(
    tmp_path: Path,
) -> None:
    """This trusted-source lexical gate is not a Python sandbox.

    Reflective namespaces, synthesized names, and runtime-injected callables are
    accepted inherited residual risks because no scope or data-flow model exists.
    """
    path = tmp_path / "residual.py"
    path.write_text(
        'name = "fi" + "les"\n'
        'globals()[name]("neurostack_core.presets")\n'
        'locals()["files"]("neurostack_core.presets")\n'
        'def invoke(injected):\n    return injected("neurostack_core.presets")\n',
        encoding="utf-8",
    )
    result = run_check(path)
    assert result.returncode == 0, result.stderr
