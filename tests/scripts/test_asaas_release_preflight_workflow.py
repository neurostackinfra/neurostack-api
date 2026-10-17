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
    assert set(action_refs) == {
        "actions/checkout@08c6903cd8c0fde910a37f88322edcfb5dd907a8",
        "actions/setup-python@e797f83bcb11b83ae66e0230d6156d7c80228e7c",
    }
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref) for ref in action_refs)

    test_step = job["steps"][-1]
    assert (
        "scripts/validate_asaas_producer_release_readbacks.py --check-template"
        in test_step["run"]
    )
    assert "tests/scripts/test_asaas_producer_release_readbacks.py" in test_step["run"]
    assert "test_asaas_build_workflow.py" in test_step["run"]
    assert "test_asaas_release_preflight_workflow.py" in test_step["run"]
    assert "test_package_asaas_build_ui_evidence.py" in test_step["run"]
    assert "test_sign_asaas_build_attestation.py" in test_step["run"]
    assert "tests/api/test_billing_producer_build_trust.py" in test_step["run"]
    assert "test_production_infra.py" in test_step["run"]


_WORKFLOWS_DIR = _WORKFLOW.parent
_YAML_MERGE_TAG = "tag:yaml.org,2002:merge"


def _strict_workflow_errors(source: str) -> list[str]:
    """Report YAML GitHub rejects but PyYAML's default loaders accept silently.

    PyYAML keeps the last value for a duplicate mapping key and expands ``<<``
    merge keys, while GitHub Actions rejects both (PR #82 shipped one of each).
    Walking the composed node graph sees every key exactly as written.
    """
    root = yaml.compose(source, Loader=yaml.SafeLoader)
    errors: list[str] = []
    visited: set[int] = set()

    def walk(node: yaml.Node | None, path: str) -> None:
        # Aliases reuse the anchored node object; visit each node once.
        if node is None or id(node) in visited:
            return
        visited.add(id(node))
        if isinstance(node, yaml.SequenceNode):
            for index, item in enumerate(node.value):
                walk(item, f"{path}[{index}]")
            return
        if not isinstance(node, yaml.MappingNode):
            return
        keys: set[str] = set()
        for key_node, value_node in node.value:
            line = key_node.start_mark.line + 1
            if key_node.tag == _YAML_MERGE_TAG:
                errors.append(f"{path}: unsupported merge key at line {line}")
                continue
            if not isinstance(key_node, yaml.ScalarNode):
                errors.append(f"{path}: non-scalar key at line {line}")
                continue
            key = key_node.value
            if key in keys:
                errors.append(f"{path}: duplicate key {key!r} at line {line}")
            keys.add(key)
            if key == "env" and isinstance(value_node, yaml.MappingNode):
                for env_key, env_value in value_node.value:
                    # Merge keys are reported once, by the mapping walk below.
                    if env_key.tag == _YAML_MERGE_TAG:
                        continue
                    if not isinstance(env_value, yaml.ScalarNode):
                        errors.append(
                            f"{path}.env: non-scalar value for {env_key.value!r} "
                            f"at line {env_key.start_mark.line + 1}"
                        )
            walk(value_node, f"{path}.{key}")

    walk(root, "$")
    return errors


def test_strict_workflow_checker_rejects_the_pr82_parse_defects() -> None:
    duplicate_env_key = (
        "jobs:\n  build:\n    runs-on: ubuntu-24.04\n    steps:\n"
        "      - run: true\n        env:\n          A: one\n          A: two\n"
    )
    merge_key_env = (
        "jobs:\n  deploy:\n    runs-on: ubuntu-24.04\n    steps:\n"
        "      - run: true\n        env: &gate\n          A: one\n"
        "      - run: true\n        env:\n          <<: *gate\n          B: two\n"
    )
    nested_env_value = (
        "jobs:\n  deploy:\n    runs-on: ubuntu-24.04\n    steps:\n"
        "      - run: true\n        env:\n          A:\n            nested: value\n"
    )
    plain_alias = (
        "jobs:\n  deploy:\n    runs-on: ubuntu-24.04\n    steps:\n"
        "      - run: true\n        env: &gate\n          A: one\n"
        "      - run: true\n        env: *gate\n"
    )

    assert _strict_workflow_errors(duplicate_env_key) == [
        "$.jobs.build.steps[0].env: duplicate key 'A' at line 8"
    ]
    assert _strict_workflow_errors(merge_key_env) == [
        "$.jobs.deploy.steps[1].env: unsupported merge key at line 10"
    ]
    assert _strict_workflow_errors(nested_env_value) == [
        "$.jobs.deploy.steps[0].env: non-scalar value for 'A' at line 7"
    ]
    # GitHub accepts plain anchors/aliases; only merge keys are rejected.
    assert _strict_workflow_errors(plain_alias) == []


def test_every_github_workflow_parses_strictly() -> None:
    workflows = sorted([*_WORKFLOWS_DIR.glob("*.yml"), *_WORKFLOWS_DIR.glob("*.yaml")])
    assert {path.name for path in workflows} >= {
        "asaas-build-attestation.yml",
        "asaas-producer-image-deploy.yml",
        "asaas-release-preflight.yml",
    }
    for path in workflows:
        source = path.read_text(encoding="utf-8")
        assert _strict_workflow_errors(source) == [], path.name
        document = yaml.safe_load(source)
        assert isinstance(document, dict), path.name
        assert "on" in document or True in document, path.name
        jobs = document.get("jobs")
        assert isinstance(jobs, dict) and jobs, path.name
        for job_id, job in jobs.items():
            assert isinstance(job, dict), f"{path.name}:{job_id}"
            assert "runs-on" in job or "uses" in job, f"{path.name}:{job_id}"
