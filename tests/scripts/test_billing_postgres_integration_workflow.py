from __future__ import annotations

import re
from pathlib import Path

import yaml
from sqlalchemy.engine import make_url

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github" / "workflows" / "billing-postgres-integration.yml"
_GATES = _ROOT / "scripts" / "wish-gates.sh"
_SUITES_FUNCTION = "asaas_release_candidate_postgres_suites"
_TRIGGER_CHARACTERIZATION = (
    "tests/integration/test_asaas_subscription_triggers_postgres.py"
)
# The release-candidate PostgreSQL suites, pinned in order here too: this
# module is the one the CI job runs, so dropping, adding or reordering a
# suite fails CI itself (tests/scripts/test_wish_gates.py pins the call).
_RELEASE_CANDIDATE_POSTGRES_SUITES = (
    "tests/integration/test_asaas_billing_postgres.py",
    "tests/integration/test_asaas_checkout_service_postgres.py",
    "tests/integration/test_asaas_webhook_postgres.py",
    _TRIGGER_CHARACTERIZATION,
    "tests/integration/test_billing_producer_handoff_postgres.py",
    "tests/integration/test_migration_036_postgres.py",
    "tests/integration/test_migration_037_postgres.py",
    "tests/integration/test_migration_window_read_postgres.py",
    "tests/integration/test_rollout_readback_postgres.py",
)
_PRODUCTION_COMPOSE = _ROOT / "docker-compose.production.yml"
_LOG_STEP = "Enable the server log the trigger characterization audits"
_SUITES_STEP = "Run the release-candidate PostgreSQL suites"
# Every step and the exact env keys it may see; nothing else reaches a script.
_STEP_ENV = {
    "Check out the reviewed commit": set(),
    "Set up Python": set(),
    "Install the locked project dependencies": set(),
    "Validate this workflow's contract": set(),
    _LOG_STEP: {"POSTGRES_CONTAINER", "PGPASSWORD"},
    "Migrate the service database to the Alembic head": {"DATABASE_URL"},
    _SUITES_STEP: {"POSTGRES_SMOKE_DATABASE_URL"},
}
_FORBIDDEN_TEXT = (
    "GITHUB_ENV",
    "GITHUB_OUTPUT",
    "GITHUB_PATH",
    "curl",
    "wget",
    "github.token",
    "GH_TOKEN",
    "secrets.",
    "vars.",
    "id-token",
    "PYTHONPATH",
    "exit 0",
    "|| true",
)


def _load() -> tuple[str, dict]:
    source = _WORKFLOW.read_text(encoding="utf-8")
    return source, yaml.safe_load(source)


def _step(job: dict, name: str) -> tuple[int, dict]:
    matches = [
        (index, step)
        for index, step in enumerate(job["steps"])
        if step.get("name") == name
    ]
    assert len(matches) == 1, name
    return matches[0]


def _keys(node: object) -> list[str]:
    if isinstance(node, dict):
        return [str(key) for key in node] + [
            key for value in node.values() for key in _keys(value)
        ]
    if isinstance(node, list):
        return [key for item in node for key in _keys(item)]
    return []


def _gate_function_body(source: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) \(\n(?P<body>.*?)^\)$", source, re.M | re.S)
    assert match is not None, name
    return match.group("body")


def test_runs_read_only_on_pull_requests_and_pushes_to_main() -> None:
    source, workflow = _load()
    triggers = workflow.get("on", workflow.get(True))

    assert triggers == {
        "pull_request": {
            "branches": ["main"],
            "types": ["opened", "synchronize", "reopened", "ready_for_review"],
        },
        "push": {"branches": ["main"]},
    }
    # PRs share a `pr-<number>` group (newer pushes cancel older runs); each
    # push to main gets its own SHA group, so no main run is ever dropped and
    # no PR can join a main group.
    assert workflow["concurrency"] == {
        "group": (
            "billing-postgres-integration-${{ github.event_name == 'pull_request'"
            " && format('pr-{0}', github.event.pull_request.number)"
            " || github.sha }}"
        ),
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }
    assert workflow["permissions"] == {}
    # No workflow-level env/defaults: steps see only their own `env:`.
    assert set(workflow) - {"on", True} == {
        "name",
        "concurrency",
        "permissions",
        "jobs",
    }
    assert list(workflow["jobs"]) == ["billing-postgres"]
    job = workflow["jobs"]["billing-postgres"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["permissions"] == {"contents": "read"}
    assert isinstance(job["timeout-minutes"], int)
    assert 0 < job["timeout-minutes"] <= 60
    assert set(job) == {
        "name",
        "runs-on",
        "timeout-minutes",
        "permissions",
        "services",
        "steps",
    }
    for text in _FORBIDDEN_TEXT:
        assert text not in source, text
    # No step or job can be skipped or allowed to fail.
    assert "if" not in _keys(job)
    assert "continue-on-error" not in _keys(job)

    action_refs = [step["uses"] for step in job["steps"] if "uses" in step]
    # The same pins as asaas-release-preflight.yml: no new third-party code.
    assert action_refs == [
        "actions/checkout@08c6903cd8c0fde910a37f88322edcfb5dd907a8",
        "actions/setup-python@e797f83bcb11b83ae66e0230d6156d7c80228e7c",
    ]
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref) for ref in action_refs)
    assert job["steps"][0]["with"] == {"persist-credentials": False}


def test_postgres_16_service_is_disposable_loopback_and_health_checked() -> None:
    _, workflow = _load()
    job = workflow["jobs"]["billing-postgres"]

    assert list(job["services"]) == ["postgres"]
    service = job["services"]["postgres"]
    assert re.fullmatch(r"postgres@sha256:[0-9a-f]{64}", service["image"])
    assert service["ports"] == ["127.0.0.1:5432:5432"]
    assert service["env"] == {
        "POSTGRES_USER": "postgres",
        "POSTGRES_PASSWORD": "ci-disposable-password",
        "POSTGRES_DB": "neurostack_ci",
    }
    assert "--health-cmd" in service["options"]
    assert "pg_isready" in service["options"]


def test_postgres_service_uses_the_production_postgres_digest() -> None:
    _, workflow = _load()
    production = yaml.safe_load(_PRODUCTION_COMPOSE.read_text(encoding="utf-8"))

    image = workflow["jobs"]["billing-postgres"]["services"]["postgres"]["image"]
    assert image == production["services"]["postgres"]["image"]


def test_steps_receive_only_their_own_env_and_no_inline_expressions() -> None:
    _, workflow = _load()
    steps = workflow["jobs"]["billing-postgres"]["steps"]

    assert [step["name"] for step in steps] == list(_STEP_ENV)
    for step in steps:
        assert set(step.get("env", {})) == _STEP_ENV[step["name"]], step["name"]
        assert set(step) <= {"name", "uses", "with", "shell", "env", "run"}
        # Expressions reach scripts through `env:` only, never spliced in.
        assert "${{" not in step.get("run", ""), step["name"]
        assert step.get("shell", "bash") == "bash", step["name"]


def test_steps_migrate_then_run_the_shared_release_candidate_suites() -> None:
    _, workflow = _load()
    job = workflow["jobs"]["billing-postgres"]
    service_env = job["services"]["postgres"]["env"]

    install_index, install = _step(job, "Install the locked project dependencies")
    assert 'pip install --disable-pip-version-check "poetry==2.4.1"' in install["run"]
    assert "poetry install --with dev --no-root" in install["run"]

    contract_index, contract = _step(job, "Validate this workflow's contract")
    assert Path(__file__).relative_to(_ROOT).as_posix() in contract["run"]

    log_index, log = _step(job, _LOG_STEP)
    assert log["env"]["POSTGRES_CONTAINER"] == "${{ job.services.postgres.id }}"
    assert log["env"]["PGPASSWORD"] == service_env["POSTGRES_PASSWORD"]
    assert "ALTER SYSTEM SET logging_collector = on" in log["run"]
    assert "ALTER SYSTEM SET log_line_prefix = '%m [%p] %d '" in log["run"]
    # The audit reads only the current file: rotation must be off.
    assert "ALTER SYSTEM SET log_rotation_age = 0" in log["run"]
    assert "ALTER SYSTEM SET log_rotation_size = 0" in log["run"]
    assert 'docker restart "$POSTGRES_CONTAINER"' in log["run"]
    assert "current_setting('log_rotation_age')" in log["run"]
    assert "current_setting('log_rotation_size')" in log["run"]
    assert log["run"].rstrip().endswith('test "$settings" = "on|%m [%p] %d |0|0|true"')

    migrate_index, migrate = _step(
        job, "Migrate the service database to the Alembic head"
    )
    assert set(migrate["env"]) == {"DATABASE_URL"}
    application = make_url(migrate["env"]["DATABASE_URL"])
    assert application.drivername == "postgresql+asyncpg"
    assert (application.host, application.port) == ("127.0.0.1", 5432)
    assert application.username == service_env["POSTGRES_USER"]
    assert application.password == service_env["POSTGRES_PASSWORD"]
    assert application.database == service_env["POSTGRES_DB"]
    assert "poetry run alembic upgrade head" in migrate["run"]
    assert "(head)" in migrate["run"]

    suites_index, suites = _step(job, _SUITES_STEP)
    assert set(suites["env"]) == {"POSTGRES_SMOKE_DATABASE_URL"}
    smoke = make_url(suites["env"]["POSTGRES_SMOKE_DATABASE_URL"])
    assert smoke.drivername == "postgresql"
    assert (smoke.host, smoke.port, smoke.database) == ("127.0.0.1", 5432, "postgres")
    assert smoke.username == service_env["POSTGRES_USER"]
    assert smoke.password == service_env["POSTGRES_PASSWORD"]
    assert suites["shell"] == "bash"
    assert suites["run"] == (
        "set -euo pipefail\n" "source scripts/wish-gates.sh\n" f"{_SUITES_FUNCTION}\n"
    )
    # The job names no suite itself: the list lives once, in wish-gates.sh.
    assert "tests/integration" not in suites["run"]

    assert install_index < contract_index < log_index < migrate_index < suites_index
    assert suites_index == len(job["steps"]) - 1


def test_ci_and_release_gate_share_one_postgres_suite_list() -> None:
    source = _GATES.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    # Every test path the function names, whatever its directory or suffix,
    # so an extra module cannot hide from the exact pin below.
    suites = re.findall(r"tests/\S+", _gate_function_body(source, _SUITES_FUNCTION))
    gate = _gate_function_body(source, "asaas_release_candidate_gate")

    assert suites, "the shared function lists no PostgreSQL suite"
    assert tuple(suites) == _RELEASE_CANDIDATE_POSTGRES_SUITES
    assert f"    {_SUITES_FUNCTION} || return 1\n" in gate
    assert "tests/integration/" not in gate
    for suite in suites:
        # Named once, by the shared function: no second list to drift.
        assert code.count(suite) == 1, suite
        assert (_ROOT / suite).is_file(), suite


def test_workflow_is_allowlisted_by_the_workflow_ignore_rule() -> None:
    # .gitignore ignores .github/workflows/* and re-includes files one by one;
    # without this line a plain `git add` silently drops the workflow.
    lines = (_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    rule = lines.index(".github/workflows/*")
    allow = lines.index("!.github/workflows/billing-postgres-integration.yml")
    assert allow > rule
