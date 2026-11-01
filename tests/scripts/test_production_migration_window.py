"""Four production migration windows, each applying exactly its own revision.

``035`` (migrations 033-035), ``036``, ``037`` and ``042`` (migrations
038-042, asaas-rollout-opening live step L3) are separate manifest steps,
each run only on its own explicit owner instruction, 036 before 037, after a
fresh SELECT showing zero legacy rows, and 042 after 037. The window table of
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
ROLLBACK_RUNBOOK = ROOT / "docs/runbooks/asaas-rollback.md"
# The deployment runbook's state section: operator-recorded live evidence of
# the runs from 2026-10-22 (window 035) to 2026-10-25 (revision 11 serving).
STATE_HEADING = "## State after the live runs of 2026-10-22 to 2026-10-25\n"
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
    "042": (
        "Order (each step stops on any divergence and returns to the owner; the\n"
        "subcommands are `scripts/asaas_migration_window.py --window 042`",
        r"^\*\*Rollback \(window 042\)\.\*\*",
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
    assert list(windows.WINDOWS) == ["035", "036", "037", "042"]
    assert windows.WINDOWS["035"] is windows.WINDOW_035
    assert windows.WINDOWS["036"] is windows.WINDOW_036
    assert windows.WINDOWS["037"] is windows.WINDOW_037
    assert windows.WINDOWS["042"] is windows.WINDOW_042
    assert wiring.MIGRATION_WINDOWS is windows.WINDOWS
    for name, window in windows.WINDOWS.items():
        assert window.command == ("alembic", "upgrade", name)
        assert window.after == name
    assert [(w.before, w.after) for w in windows.WINDOWS.values()] == [
        ("032", "035"),
        ("035", "036"),
        ("036", "037"),
        ("037", "042"),
    ]
    assert [w.snapshot_pattern for w in windows.WINDOWS.values()] == [
        "pre-033-035-<date>",
        "pre-036-<date>",
        "pre-037-<date>",
        "pre-038-042-<date>",
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


@pytest.mark.parametrize("name", ["036", "037", "042"])
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
    elif name == "042":
        assert "`alembic_version` == `037`, read as the application role" in flat
        assert "five upgrade lines `037 -> 038`" in flat
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


@pytest.mark.parametrize("name", ["035", "036", "037", "042"])
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


def test_runbook_records_the_live_read_family_revision() -> None:
    text = " ".join(_text(RUNBOOK).split())
    for phrase in (
        "**`neurostack-staging-consulta:28`**",
        "`neurostack-staging-consulta:26`",
        "`read_container_entry_point_unexpected`, `read_database_secret_mismatch`"
        " and `read_image_not_an_application_image`",
        "no `entryPoint`, `DATABASE_URL` from the secret"
        " `neurostack/staging/database-url` (application role)",
        "`python -c \"raise SystemExit('read revision: a command override is"
        " required')\"`",
        "with stream prefix `consulta`",
        "`ManagedBy=manual`, cpu 512 and memory 1024",
        "The family's numbering skips `:27`, a deregistered revision",
        "a new application image does **not** require re-registering the read"
        " family",
        '--read-task-definition "$READ_TD" # neurostack-staging-consulta:28',
    ):
        assert phrase in text, phrase
    assert windows.READ_FAMILY == "neurostack-staging-consulta"
    assert windows.APP_DATABASE_SECRET == "neurostack/staging/database-url"
    assert "**Not verified live.**" not in text


def test_runbook_separates_what_the_live_run_verified() -> None:
    verified = " ".join(
        _section(
            _text(RUNBOOK),
            "**Verified live on 2026-10-22**",
            r"^\*\*Still not verified live\.\*\*",
        ).split()
    )
    for phrase in (
        "`rds:CreateDBSnapshot` and `rds:DescribeDBSnapshots`",
        "`SnapshotCreateTime` is an aware timestamp string",
        "`creating` -> `available`",
        "`clientToken`, `platformVersion` `LATEST` and `enableECSManagedTags` false",
        "both from this tool and from the deploy hook",
        '`{"containerOverrides": [{"name": ...}], ...}`',
        "(`read_ok`)",
        "`neurostack-staging-consulta:28`",
        "an empty page can carry a different token",
    ):
        assert phrase in verified, phrase
    still = " ".join(
        _section(
            _text(RUNBOOK), "**Still not verified live.**", r"^\*\*Residual risks"
        ).split()
    )
    for phrase in (
        "Whether ECS honours the same `clientToken` for a re-sent RunTask",
        "the enforcement of `--attempt`",
        "the formal `verify-post-read` of this tool and the deploy hook's"
        " `verify-result` on a real run",
        "(`55P03`, `40P01`, `55000`)",
        "PostgreSQL 17",
    ):
        assert phrase in still, phrase
    text = " ".join(_text(RUNBOOK).split())
    assert "not yet verified live" not in text


def test_runbook_state_section_is_the_live_state_of_2026_10_25() -> None:
    """The state section records the operator's live evidence as of
    2026-10-25: 036 and 037 applied, revision 11 serving, rollback target 10,
    worker flag true, rollout still disabled. None of it is repo-derivable;
    the next live change updates this test with the text."""

    raw = _text(RUNBOOK)
    assert "## Current state (2026-10-17)" not in raw
    assert "## State after the live run of 2026-10-22\n" not in raw
    state = " ".join(_section(raw, STATE_HEADING, r"^## ").split())
    for phrase in (
        # Each fact is dated by its read: the database facts by the post-read
        # of window 037 (2026-10-24), not re-read after revisions 10 and 11.
        "Operator-recorded evidence of those runs; none of it can be derived"
        " from this repository, and each item says when it was read.",
        "The database facts (`alembic_version`, the legacy objects, the rollout"
        " rows) are from the post-read of window `037` on 2026-10-24 and have not"
        " been read again since revisions 10 and 11 were deployed; the service,"
        " task definition and hook facts were read back on 2026-10-25.",
        # The formal checks were not run after window 035 either.
        "The same holds for windows `036` and `037` and for the hook's runs on"
        " revisions 10 and 11: the formal `verify-result` and `verify-post-read`"
        " were **not** run on them either; the operator read the raw task logs,"
        " and the owner accepted that raw evidence on 2026-10-25.",
        # Window 035 (2026-10-22).
        "**Migrations 033-035 applied (2026-10-22).** `alembic_version` read"
        " `035` after that run",
        "`4063e8a0555049ada6e4cd0880b436c5`",
        "Pre-read task `98bfa0e6382d4e1590c9751d234475a3`",
        "post-read task `223a1daf9eb14d508c047cb7e0a63e2a`",
        "`verify-result` (`applied`)",
        "`pre-033-035-2026-10-22`",
        # Windows 036 and 037 (2026-10-24).
        "**Migration 036 applied (2026-10-24).** Window `036` migration task"
        " `2f23754d…`",
        "upgrade line `035 -> 036`",
        "`pre-036-2026-10-24`",
        "**Migration 037 applied (2026-10-24).** Window `037` migration task"
        " `4d8b009e…`",
        "upgrade line `036 -> 037`",
        "`alembic_version` read `037` in that window's post-read (2026-10-24),"
        " and the legacy AbacatePay tables and functions are dropped (none left"
        " in that post-read)",
        "`pre-037-2026-10-24`",
        "Windows `036` and `037` both ran with the rollback target at revision 9",
        "**The schema is forward-only.** After 037 the only way back for the"
        " database is a restore from one of the three snapshots above",
        "never an Alembic downgrade",
        "never revision 8, which reads columns 036 dropped",
        # Serving revision and history.
        "**Application revision 11 serving.** `neurostack-staging-app:11`",
        "sha256:f6603a5ee0f8293abfe290d639cfc241a8ac864fe16d0e31393d20d7f0b4b159",
        "deployed 2026-10-25 15:04Z, deployment `COMPLETED` 15:06:57Z,"
        " `BILLING_FACT_WORKER_ENABLED` `true`",
        "Revision 10 (the bridge image, `BILLING_FACT_WORKER_ENABLED` `true`)"
        " served from 2026-10-25 01:51Z to 15:04Z",
        "Revision 9 served from 2026-10-22 18:31Z to 2026-10-25 01:51Z:"
        " `neurostack-staging-app:9`, deployment `COMPLETED`",
        "sha256:18358ce728c465ffd0d9cf6d0e9cf4641c32f6bab9dfa2905729c5495bcf7f24",
        "sha256:7968fb60db6a2390a47ba6a2df029745638486e285c9b2487da1b722d0855a3e",
        "`BILLING_FACT_WORKER_ENABLED` `false`",
        # Rollback target.
        "**Rollback target: revision 10.**",
        "9 to 10 in PR #133 after revision 10's observation (minimum 6 h)"
        " closed at 2026-10-25T12:42Z, after about 11 h clean",
        "Revision 8 is no longer a way back after migration 036",
        # Webhook, read family, what stays off.
        "(task `8593671855574617b5267cbacfdfa081`, on revision 9)",
        "verdict `ready`, action `created`, one provider write",
        "The hook ran again on revisions 10 and 11 and reported `action: none`",
        "A second, foreign webhook exists in the ASAAS account; it was not touched",
        "`neurostack-staging-consulta:28` registered",
        "**Still off:** billing rollout `disabled` (all four"
        " `billing_rollout_control` rows `disabled` in the post-read of window"
        " `037`, 2026-10-24; not read again since revision 11 was deployed);"
        " financial egress closed (the task definition carries no"
        " `APPCONFIG_ENABLED`).",
    ):
        assert phrase in state, phrase
    assert "as read back live on 2026-10-25" not in state
    # Nowhere in the runbook (lowercased) does a sentence still call 036 or
    # 037 unapplied or describe the pre-2026-10-24 state as current.
    flat = " ".join(raw.split()).lower()
    for stale in (
        "**application revision 9 serving.**",
        "**not applied:** migrations 036 and 037.",
        "billing rollout `disabled`; `billing_fact_worker_enabled` `false`",
        "not applied in production",
        "written, not applied",
        "the downgrade is usable today",
        "`035` on `main` today",
        "`036` on `main` today",
    ):
        assert stale not in flat, stale


def test_rollback_runbook_states_every_rollback_target_phase() -> None:
    # Both re-pins are merged (8 -> 9 after revision 9's 24 h observation,
    # 9 -> 10 in PR #133 after revision 10's observation closed at
    # 2026-10-25T12:42Z): the ladder reads as history, revision 10 is R.
    text = " ".join(_text(ROLLBACK_RUNBOOK).split())
    for phrase in (
        "It went through three phases, each move a reviewed manifest change;"
        " both re-pins are merged, and the field reads revision 10",
        "**Revision 8, until the first re-pin merged**",
        "**Revision 9, from the first re-pin to the second**",
        "about **2026-10-23 18:30 UTC**",
        "Windows `036` and `037` ran on 2026-10-24 with this target.",
        "**Revision 10, since the second re-pin**",
        "merged in PR #133",
        "the observation closed at 2026-10-25T12:42Z",
        "The re-pin merged before the image bump (revision 11, a new app image"
        " digest) was deployed, at 2026-10-25 15:04 UTC: a rollback to revision 9"
        " would also turn the worker off",
        "Revision 11 serves now, and the way back is revision 10",
        "**Revision 8 is unsafe after migration 036**: it reads columns 036"
        " drops, and 036 ran on 2026-10-24",
        "--task-definition neurostack-staging-app:R",
        "`R` is the rollback target, `10` since the second re-pin (it was `8`"
        " until the first re-pin merged and `9` until the second); never `8`,"
        " because migration 036 has run",
        "## Current state (2026-10-25)",
        # The current state, each database fact dated by its read.
        "the service `neurostack-staging-app` serves revision 11 (the image bump"
        " of revision 10, deployed 2026-10-25 15:04Z, `COMPLETED` 15:06:57Z),"
        " `BILLING_FACT_WORKER_ENABLED=true`, `alembic_version` `037` (the"
        " post-read of window `037`, 2026-10-24; not read again since revision"
        " 11 was deployed), and the rollback target is revision 10.",
        "Billing rollout is `disabled` (all four rows in the post-read of window"
        " `037`, 2026-10-24) and `topup_commercial_enabled=false`.",
        "deployment completed 2026-10-25 01:51 UTC",
        # The E6 rule after 037 holds for revision 10 (operator evidence).
        "Revision 10, the rollback target, meets this rule (operator evidence,"
        " recorded in the wish): its image, the bridge digest `sha256:18358ce7…`"
        " that revisions 9 and 10 run, was built from commit `67b9b51c`, the"
        " merge of #126, which contains migration 037 and the E6 code.",
        "The schema is forward-only. After 037 the only way back for the"
        " database is a restore from a snapshot",
    ):
        assert phrase in text, phrase
    for retired in (
        "It has two phases",
        "It has three phases:",
        "Today it is `neurostack-staging-app:8`",
        "--task-definition neurostack-staging-app:8",
        "**Until the re-pin is merged: revision 8**",
        "merges only after",
        "Once migration 036 is applied",
        "Once migration 037 is applied",
        "needs the 037 downgrade first",
        "## Current state (2026-10-17)",
    ):
        assert retired not in text, retired


def _alembic_head() -> str:
    numbers = []
    for path in ALEMBIC_VERSIONS.glob("*.py"):
        match = re.match(r"^\d{8}_\d{6}_(\d{3})_", path.name)
        if match:
            numbers.append(match.group(1))
    return max(numbers)


def test_runbook_migration_sections_record_036_and_037_as_applied() -> None:
    """036 and 037 ran in production on 2026-10-24 (operator record): each
    section says so, each downgrade section opens with the forward-only
    warning, and the 037 rollback target note names revision 10."""

    raw = _text(RUNBOOK)
    for number, marker in (
        ("036", "**applied in production on 2026-10-24** (operator record"),
        ("037", "**applied in production on 2026-10-24**, after 036 (operator"),
    ):
        section = " ".join(_section(raw, f"## Migration {number}:", r"^## ").split())
        assert marker in section, number
        downgrade = " ".join(
            _section(raw, f"### Rollback (downgrade {number} -> ", r"^### ").split()
        )
        assert downgrade.index("In production this is not a way back") < (
            downgrade.index("The downgrade is real")
        ), number
        assert (
            "the schema is forward-only, and the only way back is a restore from a"
            " snapshot" in downgrade
        ), number
    section_037 = " ".join(_section(raw, "## Migration 037:", r"^## ").split())
    assert (
        "Revision 10, the production rollback target, meets the rule (operator"
        " evidence, recorded in the wish): its bridge image `sha256:18358ce7…` was"
        " built from commit `67b9b51c`, the merge of #126, which contains"
        " migration 037 and the E6 code." in section_037
    )
    # The image smoke names the head of main, which is the tree's own head.
    flat = " ".join(raw.split())
    head = _alembic_head()
    assert head == "042"
    assert flat.count("on `main` today") == flat.count(f"`{head}` on `main` today")
    assert flat.count(f"`{head}` on `main` today") == 2


def test_runbook_states_the_snapshot_order_and_the_freshness_rule() -> None:
    text = " ".join(_text(RUNBOOK).split())
    for phrase in (
        "**Snapshot order.** `plan-snapshot` runs, and exits 0, **before**"
        " `CreateDBSnapshot` is sent",
        "On 2026-10-22 (window `035`) `CreateDBSnapshot` was sent first, by"
        " mistake, and the `plan-snapshot` run afterwards passed",
        "**before** any `CreateDBSnapshot`; `render-snapshot-requests`; only then"
        " send `CreateDBSnapshot`",
        f"(`pre_read_max_age_seconds` {windows.PRE_READ_MAX_AGE_SECONDS})",
        f"(`snapshot_max_age_seconds` {windows.SNAPSHOT_MAX_AGE_SECONDS})",
        "the run must follow the pre-read within 15 minutes and the snapshot"
        " within 60",
        "Prepare everything that does not age before asking for the owner's go",
        "run the read's `plan-*` or `plan` again; it, not a comparison by eye, is"
        " the judge",
        "ECR `lastRecordedPullTime`, a task's `version`, the ECS service `events`",
        "Any other difference between the old and the new response is"
        " unexplained: stop and report (the safe default), never guess.",
        "Never keep or re-date a readback past its freshness window.",
    ):
        assert phrase in text, phrase
    assert windows.PRE_READ_MAX_AGE_SECONDS == 15 * 60
    assert windows.SNAPSHOT_MAX_AGE_SECONDS == 60 * 60


def test_runbook_states_the_live_log_paging_rule() -> None:
    text = " ".join(_text(RUNBOOK).split())
    for phrase in (
        "**Log pages, as read live (2026-10-22, window `035`).**",
        "until a response returns exactly the token that was sent: only that"
        " repetition is the end of the stream",
        "an **empty page can carry a different `nextForwardToken`**: an empty page"
        " is not the end, send the next one",
    ):
        assert phrase in text, phrase


def test_runbook_states_the_re_pin_merge_condition() -> None:
    text = " ".join(_text(RUNBOOK).split())
    for phrase in (
        "**Merge condition of the re-pin.** The manifest edit above merges only"
        " after revision 9's 24 h observation period has closed, about"
        " **2026-10-23 18:30 UTC**",
        "and before migration 036",
        "**History.** The rollback target was re-pinned from revision 8 to"
        " revision 9 on the date the re-pin merged; revision 8 was the target"
        " until then.",
        "arn:aws:ecs:us-east-1:388460142431:task-definition/neurostack-staging-app:9",
        "neurostack-staging@sha256:"
        "18358ce728c465ffd0d9cf6d0e9cf4641c32f6bab9dfa2905729c5495bcf7f24",
    ):
        assert phrase in text, phrase
    assert "about 2026-10-23 18:30 UTC" in " ".join(_text(MANIFEST).split())
    # Text that lands on main only after the merge never describes the branch
    # or the pre-merge state as current.
    for stale in (
        "prepared on branch",
        "chore/asaas-repin-and-live-chain-docs",
        "Until the merge, `main` reads revision 8",
    ):
        assert stale not in text, stale


def test_runbook_keeps_the_standing_guidance_undated() -> None:
    raw = _text(RUNBOOK)
    assert raw.index(STATE_HEADING) < raw.index("## Standing guidance\n")
    standing = " ".join(_section(raw, "## Standing guidance\n", r"^## ").split())
    for phrase in (
        "The ECS service `neurostack-staging-app` serves production traffic. Any"
        " change to it is production-impacting.",
        "Verify both from the server readback"
        " (`GET /v1/admin/billing/rollout/readback`,",
        "never from UI state or environment overrides.",
        "The commercial credit top-up is deferred and stays unavailable.",
        "`general` opens only after a delivered, deployed credit consumer exists",
        "no transition to `canary_allowlist` or `general` is planned.",
    ):
        assert phrase in standing, phrase
    assert "2026-10-18: D3 step 1" not in raw


def test_the_read_family_contract_is_not_stated_stricter_than_the_code() -> None:
    """The tools accept any application image of the registry and any log
    group matching the name pattern; neither text may claim more."""

    runbook = " ".join(_text(RUNBOOK).split())
    comment = " ".join(
        line.lstrip("# ")
        for line in _text(WINDOW_TOOL).splitlines()
        if line.startswith("#")
    )
    for text in (runbook, comment):
        for stricter in (
            "an image of the `neurostack-staging` repository",
            "image of this registry's neurostack-staging repository",
            "neurostack-staging repository (tag or digest)",
            "awslogs (log group /ecs/neurostack-staging/",
            "log group must be",
        ):
            assert stricter not in text, stricter
    assert "an application image of this registry, by tag or digest" in runbook
    assert "checked only against a name pattern" in runbook
    assert "not a requirement of the tools" in runbook
    assert "an application image of this registry (tag or digest)" in comment
    assert "match the name patterns only" in comment
    assert windows._REGISTRY_IMAGE.fullmatch(
        f"{windows.ECR_REGISTRY}/some-other-repository:anything"
    )
    assert windows.hook._LOG_GROUP.fullmatch("/ecs/unrelated/group")


def test_the_state_section_does_not_claim_formal_post_read_verification() -> None:
    raw = _text(RUNBOOK)
    state = " ".join(_section(raw, STATE_HEADING, r"^## ").split())
    assert (
        "were read from the raw task logs; the formal `verify-post-read` and the"
        " deploy hook's `verify-result` were **not** run on them" in state
    )
    # The second caveat covers windows 036 and 037 and the hook's runs on
    # revisions 10 and 11 (raw logs only, accepted by the owner 2026-10-25).
    second = (
        "the formal `verify-result` and `verify-post-read` were **not** run on"
        " them either"
    )
    assert second in state
    caveat_free = state.replace(
        "the formal `verify-post-read` and the deploy hook's `verify-result` were"
        " **not** run on them",
        "",
    ).replace(second, "")
    for claim in (
        "verify-post-read",
        "verified by the deploy hook's",
        "hook's `verify-result`",
    ):
        assert claim not in caveat_free, claim
