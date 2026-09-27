from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "extract-branch-public-api.py"
CONSUMERS = (
    "src/api/routes/policies.py",
    "src/core/pipeline.py",
    "src/memory/__init__.py",
    "src/memory/models.py",
    "src/services/business_hours.py",
    "src/services/trace_adapter.py",
)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_tree(path: Path, policy_consumer: str | None = None) -> None:
    for consumer in CONSUMERS:
        write(path / consumer, "")
    write(
        path / "src/api/routes/policies.py",
        policy_consumer or "from src.policy import PublicThing as DomainThing\n",
    )
    write(
        path / "src/core/pipeline.py",
        "from src.srm.models import DeepThing as Alias\n"
        "import src.prompt.timeutils as timeutils\n"
        "timezone = timeutils.resolve_clinic_tz\n",
    )
    write(
        path / "src/policy/__init__.py",
        "from src.policy.models import PublicThing\n__all__ = ['PublicThing']\n",
    )
    write(path / "src/policy/models.py", "class PublicThing: pass\n")
    write(path / "src/srm/models.py", "class DeepThing: pass\n")
    write(path / "src/prompt/timeutils.py", "def resolve_clinic_tz(): pass\n")
    write(path / "src/flow/models.py", "class FlowMode: pass\n")
    write(path / "src/ctx/__init__.py", "__all__ = ['Context']\nclass Context: pass\n")
    write(
        path / "README.md",
        "# Project\n\n## Quick Start\n\n```python\n"
        "from src.flow.models import FlowMode as Mode\n"
        "```\n\n## Other\n",
    )


def extract(tree: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--tree",
            str(tree),
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )


def test_captures_minimal_facade_deterministically(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    make_tree(tree)
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    assert extract(tree, first).returncode == 0
    assert extract(tree, second).returncode == 0
    assert first.read_bytes() == second.read_bytes()
    assert json.loads(first.read_text(encoding="utf-8")) == {
        "neurostack_core.flow": ["FlowMode"],
        "neurostack_core.policy": ["PublicThing"],
        "neurostack_core.prompt": ["resolve_clinic_tz"],
        "neurostack_core.srm": ["DeepThing"],
    }


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("from src.policy import *\n", "star import"),
        ("from src.policy import Missing\n", "missing facade export"),
    ],
)
def test_rejects_unproved_direct_imports(
    tmp_path: Path, source: str, message: str
) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    make_tree(tree, source)
    result = extract(tree, tmp_path / "output.json")
    assert result.returncode == 2
    assert message in result.stderr


@pytest.mark.parametrize(
    "source",
    [
        "import importlib as harmless\n",
        "def nested():\n    return client.import_module\n",
        "class builtins:\n    pass\n",
        "def use(__import__):\n    return 1\n",
        "import ordinary as importlib\n",
        "try:\n    action()\nexcept Exception as import_module:\n    pass\n",
        "call(builtins=value)\n",
    ],
)
def test_rejects_reserved_syntax_and_ordinary_name_collisions(
    tmp_path: Path, source: str
) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    make_tree(tree, source)
    result = extract(tree, tmp_path / "output.json")
    assert result.returncode == 2
    assert "reserved import syntax" in result.stderr


@pytest.mark.parametrize(
    "guard",
    ["TYPE_CHECKING", "typing.TYPE_CHECKING", "typing_alias.TYPE_CHECKING"],
)
def test_skips_syntactic_type_checking_body_independent_of_order(
    tmp_path: Path, guard: str
) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    make_tree(
        tree,
        f"if {guard}:\n"
        "    from src.policy import Missing\n"
        "    import importlib\n"
        "    class builtins:\n"
        "        pass\n"
        "from typing import TYPE_CHECKING\n"
        "import typing\n"
        "import typing as typing_alias\n",
    )
    result = extract(tree, tmp_path / "output.json")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            "import src.policy.models as domain\n" "import src.flow.models as domain\n",
            "conflicting facade alias",
        ),
        (
            "import src.policy.models as domain\n"
            "domain = object()\n"
            "value = domain.PublicThing\n",
            "rebound facade alias",
        ),
        (
            "import src.policy.models as domain\n"
            "def nested(domain):\n"
            "    return domain\n",
            "rebound facade alias",
        ),
        (
            "import ordinary as domain\n" "import src.policy.models as domain\n",
            "rebound facade alias",
        ),
        (
            "import src.policy.models as domain\n"
            "from src.policy import PublicThing as domain\n",
            "rebound facade alias",
        ),
        (
            "import src.policy.models as domain\n" "del domain\n",
            "rebound facade alias",
        ),
    ],
)
def test_rejects_conflicting_or_rebound_facade_aliases(
    tmp_path: Path, source: str, message: str
) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    make_tree(tree, source)
    result = extract(tree, tmp_path / "output.json")
    assert result.returncode == 2
    assert message in result.stderr


def test_accepts_runtime_injected_loader(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    make_tree(
        tree,
        "def load(loader, module_name):\n"
        "    return loader(module_name)\n"
        "load(callback, 'src.policy')\n",
    )
    result = extract(tree, tmp_path / "output.json")
    assert result.returncode == 0, result.stderr


def test_ignores_modules_outside_the_four_domain_contract(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    make_tree(tree, "from src.ctx import Context\n")
    output = tmp_path / "output.json"
    result = extract(tree, output)
    assert result.returncode == 0, result.stderr
    assert "neurostack_core.ctx" not in json.loads(output.read_text(encoding="utf-8"))


def test_historical_loop_and_function_input_terminates(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    make_tree(
        tree,
        "def recursive(loader):\n"
        "    return recursive(loader)\n"
        "while True:\n"
        "    callback = lambda value: value\n",
    )
    result = extract(tree, tmp_path / "output.json")
    assert result.returncode == 0, result.stderr
