from __future__ import annotations

import json
import runpy
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BUNDLED = (
    ROOT
    / "packages"
    / "neurostack-core"
    / "neurostack_core"
    / "presets"
    / "pt_br_clinic"
)


def test_every_flow_prompt_addition_comes_from_selected_preset(
    tmp_path: Path,
) -> None:
    preset = tmp_path / "substituted"
    shutil.copytree(BUNDLED, preset)
    flow_path = preset / "flow.toml"
    original = tomllib.loads(flow_path.read_text(encoding="utf-8"))
    imperative = original["imperative_literals"]

    expected: dict[str, list[str]] = {}
    substitutions: dict[str, str] = {}
    handler_names = {
        "flow.handlers.handoff": "HandoffModeHandler",
        "flow.handlers.safety": "SafetyModeHandler",
        "flow.handlers.normal": "NormalModeHandler",
        "flow.handlers.rag": "RagModeHandler",
    }
    for module_name, handler_name in handler_names.items():
        method_name = f"{handler_name}.get_mode_prompt_additions"
        values = imperative[module_name][method_name]
        markers = [
            f"PRESET-MARKER-{handler_name}-{index}" for index in range(len(values))
        ]
        expected[handler_name] = markers
        substitutions.update(zip(values, markers, strict=True))

    source = flow_path.read_text(encoding="utf-8")
    for value, marker in substitutions.items():
        old = f"  '''{value}''',"
        assert source.count(old) == 1
        source = source.replace(old, f"  '''{marker}''',", 1)
    flow_path.write_text(source, encoding="utf-8")

    script = """
import json
import pathlib
import sys

import neurostack_core as nc

nc.use_preset(pathlib.Path(sys.argv[1]))
from neurostack_core.flow.handlers.handoff import HandoffModeHandler
from neurostack_core.flow.handlers.normal import NormalModeHandler
from neurostack_core.flow.handlers.rag import RagModeHandler
from neurostack_core.flow.handlers.safety import SafetyModeHandler

handlers = (HandoffModeHandler, SafetyModeHandler, NormalModeHandler, RagModeHandler)
print(json.dumps({handler.__name__: handler().get_mode_prompt_additions() for handler in handlers}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(preset)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    actual: dict[str, Any] = json.loads(completed.stdout)
    assert actual == expected


def test_inventory_detects_unaccented_flow_tone_instruction() -> None:
    inventory = runpy.run_path(str(ROOT / "scripts" / "pt-br-inventory.py"))
    is_pt_br_literal = inventory["is_pt_br_literal"]
    assert is_pt_br_literal("Mantenha tom calmo e acolhedor.", frozenset())
