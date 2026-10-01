"""Preset ownership contracts for SRM action details and inventory coverage."""

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


def _srm_document(path: Path = BUNDLED) -> dict[str, Any]:
    with (path / "srm.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_inventory_recognizes_every_default_action_description() -> None:
    inventory = runpy.run_path(str(ROOT / "scripts" / "pt-br-inventory.py"))
    is_pt_br_literal = inventory["is_pt_br_literal"]
    allowlist = inventory["_load_allowlist"]()
    imperative = _srm_document()["imperative_literals"]
    details = imperative["srm.engine"]["SRMEngine._get_action_details"]
    residual_outputs = [
        *details.values(),
        imperative["srm.rag_decider"]["RagDecider._generate_query"][1],
    ]

    missed = [
        value for value in residual_outputs if not is_pt_br_literal(value, allowlist)
    ]

    assert missed == []


def test_substituted_preset_replaces_every_action_detail_without_pt_leakage(
    tmp_path: Path,
) -> None:
    preset = tmp_path / "foreign"
    shutil.copytree(BUNDLED, preset)
    document = _srm_document(preset)
    details = document["imperative_literals"]["srm.engine"][
        "SRMEngine._get_action_details"
    ]
    expected = {
        action_id: f"foreign-action-detail-{index:02d}"
        for index, action_id in enumerate(details, start=1)
    }
    path = preset / "srm.toml"
    source = path.read_text(encoding="utf-8")
    for action_id, default in details.items():
        source = source.replace(
            f"\"{action_id}\" = '''{default}'''",
            f"\"{action_id}\" = '''{expected[action_id]}'''",
            1,
        )
    path.write_text(source, encoding="utf-8")

    script = """
import json
import pathlib
import sys

from neurostack_core import use_preset

use_preset(pathlib.Path(sys.argv[1]))
from neurostack_core.protocol import ActionType
from neurostack_core.srm.engine import SRMEngine

engine = object.__new__(SRMEngine)
print(json.dumps({
    action.value: engine._get_action_details(action, "unused")
    for action in ActionType
}, sort_keys=True))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(preset)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert json.loads(completed.stdout) == expected
    assert not any(default in completed.stdout for default in details.values())
