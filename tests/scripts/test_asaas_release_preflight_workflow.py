from __future__ import annotations

import re
from pathlib import Path

import yaml

_WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "asaas-release-preflight.yml"
)


def test_release_preflight_emits_required_read_only_check_for_pull_requests() -> None:
    source = _WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.safe_load(source)
    triggers = workflow.get("on", workflow.get(True))

    assert triggers == {
        "pull_request": {
            "branches": ["main"],
            "types": ["opened", "synchronize", "reopened", "ready_for_review"],
        },
        "workflow_dispatch": {},
    }
    assert workflow["permissions"] == {}

    job = workflow["jobs"]["release-preflight"]
    assert job["name"] == "ASAAS Release Preflight"
    assert job["permissions"] == {"contents": "read"}
    assert "environment" not in job
    assert "id-token" not in source
    assert "secrets." not in source

    action_refs = [
        step["uses"]
        for step in job["steps"]
        if isinstance(step, dict) and "uses" in step
    ]
    assert action_refs
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref) for ref in action_refs)

    test_step = job["steps"][-1]
    assert "test_asaas_build_workflow.py" in test_step["run"]
    assert "test_asaas_release_preflight_workflow.py" in test_step["run"]
