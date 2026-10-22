"""Three production migration windows, each applying exactly its own revision.

``035`` (migrations 033-035), ``036`` and ``037`` are separate manifest
steps, each run only on its own explicit owner instruction, 036 before 037,
after a fresh SELECT showing zero legacy rows. The window table of
``scripts/asaas_migration_window.py`` is the single source: one constant per
window, each baking ``alembic upgrade <window>``. ``alembic upgrade head`` in
any artifact that describes a production window would apply every later
revision in the same run, so every such artifact names the exact revision,
and no tool defaults a window.

The VPS deploy pin (``scripts/deploy-vps.sh``) and ``docs/configuration.md``
name the first window, ``035``, and stay there.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from scripts import asaas_migration_window as windows
from scripts import asaas_production_wiring as wiring

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "infra/production/asaas-production-wiring.yaml"
PLANNER = ROOT / "scripts/asaas_production_wiring.py"
WINDOW_TOOL = ROOT / "scripts/asaas_migration_window.py"
RUNBOOK = ROOT / "docs/runbooks/asaas-deployment.md"
CONFIGURATION = ROOT / "docs/configuration.md"
README = ROOT / "README.md"
VPS_RUNBOOK = ROOT / "docs/api-provider-settings-rollout.md"
DEPLOY_VPS = ROOT / "scripts/deploy-vps.sh"
ALEMBIC_VERSIONS = ROOT / "alembic/versions"
DEPLOY_VPS_PIN = re.compile(r'^readonly APPROVED_ALEMBIC_REVISION="([^"\n]*)"$', re.M)

UPGRADE_HEAD = re.compile(r"upgrade\s+heads?\b", re.IGNORECASE)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, start: str, end_pattern: str) -> str:
    begin = text.index(start)
    match = re.compile(end_pattern, re.M).search(text, begin + len(start))
    return text[begin : match.start() if match else len(text)]


def _configuration_section() -> str:
    return _section(
        _text(CONFIGURATION), "## Primeiro acesso: bootstrap do super_admin", r"^## "
    )


def _manifest_step(step_id: str) -> str:
    return _section(_text(MANIFEST), f"  - id: {step_id}\n", r"^  - id: ")


# The runbook text of each window: gate 5 for 035, the deploy order of the
# 036 and 037 sections for the later windows.
RUNBOOK_WINDOW_SECTIONS = {
    "035": ("5. **Gate: migrations 033-035", r"^6\. \*\*Gate:"),
    "036": (
        "Order (each step stops on any divergence and returns to the owner; the\n"
        "subcommands are `scripts/asaas_migration_window.py --window 036`",
        r"^Never run 036 under an earlier image\.",
    ),
    "037": (
        "Order (each step stops on any divergence and returns to the owner; the\n"
        "subcommands are `scripts/asaas_migration_window.py --window 037`",
        r"^### Rollback \(downgrade 037 -> 036\)",
    ),
}


def _runbook_window(name: str) -> str:
    start, end = RUNBOOK_WINDOW_SECTIONS[name]
    return _section(_text(RUNBOOK), start, end)


def test_no_production_migration_artifact_says_upgrade_head() -> None:
    artifacts = {
        "manifest": _text(MANIFEST),
        "planner": _text(PLANNER),
        "window tool": _text(WINDOW_TOOL),
        "runbook": _text(RUNBOOK),
        "configuration bootstrap section": _configuration_section(),
        "README production deploy": _section(_text(README), "### Produção", r"^---$"),
        "VPS runbook": _text(VPS_RUNBOOK),
        "VPS deploy script": _text(DEPLOY_VPS),
        **{
            f"read {spec.sql_file}": _text(ROOT / str(spec.sql_file))
            for window in windows.WINDOWS.values()
            for spec in (window.pre_read, window.post_read)
            if spec.sql_file is not None
        },
    }
    offenders = [name for name, text in artifacts.items() if UPGRADE_HEAD.search(text)]
    assert offenders == []


def test_one_constant_per_window_and_the_planner_uses_that_table() -> None:
    assert list(windows.WINDOWS) == ["035", "036", "037"]
    assert windows.WINDOWS["035"] is windows.WINDOW_035
    assert windows.WINDOWS["036"] is windows.WINDOW_036
    assert windows.WINDOWS["037"] is windows.WINDOW_037
    assert wiring.MIGRATION_WINDOWS is windows.WINDOWS
    for name, window in windows.WINDOWS.items():
        assert window.command == ("alembic", "upgrade", name)
        assert window.after == name
    assert [(w.before, w.after) for w in windows.WINDOWS.values()] == [
        ("032", "035"),
        ("035", "036"),
        ("036", "037"),
    ]
    assert [w.snapshot_pattern for w in windows.WINDOWS.values()] == [
        "pre-033-035-<date>",
        "pre-036-<date>",
        "pre-037-<date>",
    ]


def test_runbook_migrations_gate_pins_035_and_reads_back_alembic_version() -> None:
    gate = _runbook_window("035")
    flat = " ".join(gate.split())
    assert "`alembic upgrade 035`" in flat
    assert "`alembic_version`" in flat
    assert "`032`" in flat
    assert "**Verify:** read back `alembic_version` == `035`" in flat
    assert "--window 035" in flat
    assert "pre-033-035-<date>" in flat
    # The exclusion of the later migrations and the owner conditions.
    for phrase in ("`036`", "`037`", "036 before 037", "zero legacy rows"):
        assert phrase in flat, phrase


@pytest.mark.parametrize("name", ["036", "037"])
def test_runbook_gate_of_the_later_windows_pins_their_own_revision(name: str) -> None:
    window = windows.WINDOWS[name]
    flat = " ".join(_runbook_window(name).split())
    assert f"`alembic upgrade {name}`" in flat
    assert f"render-migrations-taskdef --window {name}" in flat
    assert f"named `{window.snapshot_pattern}`" in flat
    assert f"**Verify** `alembic_version` == `{name}`" in flat
    for subcommand in (
        "render-snapshot-requests",
        "render-pre-read",
        "verify-pre-read",
        "`plan`",
        "render-run-task",
        "verify-result",
        "render-post-read",
        "verify-post-read",
    ):
        assert subcommand in flat, subcommand
    if name == "036":
        assert "`alembic_version` == `035`" in flat
    else:
        assert "never the application role" in flat and "eleven `ok`" in flat


def test_configuration_migrations_task_points_production_at_the_window() -> None:
    section = " ".join(_configuration_section().split())
    assert "`alembic upgrade 035`" in section
    assert "nunca `upgrade` até o head" in section
    assert "docs/runbooks/asaas-deployment.md" in section


def test_deploy_vps_migration_carries_the_production_guard_comment() -> None:
    lines = _text(DEPLOY_VPS).splitlines()
    index = next(
        i
        for i, line in enumerate(lines)
        if line.strip().startswith("/usr/bin/docker compose")
        and "alembic upgrade" in line
    )
    guard = " ".join(
        line.strip().lstrip("#").strip()
        for line in lines[max(0, index - 6) : index]
        if line.strip().startswith("#")
    )
    assert "compose-local Postgres" in guard
    assert "never the AWS production database" in guard
    assert "036" in guard and "037" in guard
    assert "docs/runbooks/asaas-deployment.md" in guard
    assert (
        lines[index]
        .strip()
        .endswith('exec -T api alembic upgrade "$APPROVED_ALEMBIC_REVISION"')
    )


def _deploy_vps_pin() -> str:
    pins = DEPLOY_VPS_PIN.findall(_text(DEPLOY_VPS))
    assert len(pins) == 1, pins
    return pins[0]


def test_deploy_vps_pin_is_the_first_window() -> None:
    """The VPS pin and docs/configuration.md name window 035 exactly.

    Matching uses the planner's own broad ``alembic upgrade <target>`` pattern,
    so a relative or range target (``+1``, ``035@head``) is a mismatch too.
    """

    pin = _deploy_vps_pin()
    assert pin == windows.PINNED_WINDOW == "035"
    assert windows.WINDOWS[pin].command == ("alembic", "upgrade", pin)
    for name, text in {
        "configuration bootstrap section": _configuration_section(),
    }.items():
        targets = wiring._ALEMBIC_UPGRADE.findall(text)
        assert targets and set(targets) == {pin}, (name, targets)
        assert wiring._UNPINNED_UPGRADE.search(text) is None, name


@pytest.mark.parametrize("name", ["035", "036", "037"])
def test_each_window_text_names_only_its_own_revision(name: str) -> None:
    window = windows.WINDOWS[name]
    texts = {
        "manifest step": _manifest_step(window.manifest_step),
        "runbook gate": _runbook_window(name),
    }
    for label, text in texts.items():
        targets = wiring._ALEMBIC_UPGRADE.findall(text)
        assert targets and set(targets) == {name}, (label, targets)
        assert wiring._UNPINNED_UPGRADE.search(text) is None, label
    declared = [
        path.name
        for path in ALEMBIC_VERSIONS.glob("*.py")
        if f'revision: str = "{name}"' in path.read_text(encoding="utf-8")
    ]
    assert len(declared) == 1, declared


def test_no_manifest_text_outside_the_window_steps_names_an_upgrade() -> None:
    text = _text(MANIFEST)
    for window in windows.WINDOWS.values():
        text = text.replace(_manifest_step(window.manifest_step), "")
    assert wiring._ALEMBIC_UPGRADE.findall(text) == []


def _window_arguments(path: Path) -> list[ast.Call]:
    def is_add(func: ast.expr) -> bool:
        return (isinstance(func, ast.Attribute) and func.attr == "add_argument") or (
            isinstance(func, ast.Name) and func.id == "add"
        )

    return [
        node
        for node in ast.walk(ast.parse(_text(path)))
        if isinstance(node, ast.Call)
        and is_add(node.func)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "--window"
    ]


def test_no_tool_defaults_a_window() -> None:
    with pytest.raises(SystemExit):
        wiring.main(["render-migrations-taskdef", "--params", str(MANIFEST)])
    assert windows.main(["render-snapshot-requests"]) == 2
    assert windows.main(["plan"]) == 2
    planner, tool = _window_arguments(PLANNER), _window_arguments(WINDOW_TOOL)
    # One --window per planner subcommand that renders, one loop for every
    # subcommand of the window tool (which also refuses it twice).
    assert len(planner) == 1 and len(tool) == 1
    for call in planner + tool:
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        assert "default" not in keywords
        required = keywords["required"]
        assert isinstance(required, ast.Constant) and required.value is True
    action = {keyword.arg: keyword.value for keyword in tool[0].keywords}["action"]
    assert isinstance(action, ast.Name) and action.id == "_Once"


def test_deploy_vps_pin_comment_names_the_owner_approval_and_other_pins() -> None:
    lines = _text(DEPLOY_VPS).splitlines()
    index = next(
        i
        for i, line in enumerate(lines)
        if line.startswith("readonly APPROVED_ALEMBIC_REVISION=")
    )
    start = index
    while start > 0 and lines[start - 1].startswith("#"):
        start -= 1
    comment = " ".join(line.lstrip("#").strip() for line in lines[start:index])
    assert "owner" in comment
    for other_pin in (
        "infra/production/asaas-production-wiring.yaml",
        "scripts/asaas_production_wiring.py",
        "docs/runbooks/asaas-deployment.md",
        "tests/scripts/test_production_migration_window.py",
    ):
        assert other_pin in comment, other_pin


def test_runbook_states_the_label_source_the_same_day_limit_and_the_read_revision() -> (
    None
):
    text = " ".join(_text(RUNBOOK).split())
    assert "The S labels in this table are the ones this tooling uses" in text
    assert "Same-day snapshot retry: a known limitation." in text
    assert "ask the owner to approve a suffixed name for that retry" in text
    assert "The tools do not extend the pattern." in text
    assert "render-migrations-taskdef --window 037 --purpose read" in text
    assert "carries **no override**" in text
    assert "pending owner confirmation" not in text.lower()


def test_runbook_states_the_review_fixes_and_the_accepted_residual_risks() -> None:
    text = " ".join(_text(RUNBOOK).split())
    for phrase in (
        "It never invokes the migration tool: it reads the `alembic_version` table.",
        "default_transaction_read_only=on",
        "SQLSTATE `25006`",
        "`42601`",
        "`read_family_names_the_migration_tool`",
        "`window_task_stopped_after_pre_read`",
        "**Retry after a stopped run.**",
        "**A re-run after a clean run is not supported.**",
        "`window_already_ran_cleanly`",
        "a retry is a new explicit owner instruction",
        "`migration_log_inconsistent`",
        "`task_overrides_present`",
        "There is no `--now`",
        "`plan-snapshot`",
        "**Residual risks, accepted.**",
        "Nothing cross-checks that the read tasks and the migration task reach the same database",
    ):
        assert phrase in text, phrase
    assert "never names the migration tool" not in text
