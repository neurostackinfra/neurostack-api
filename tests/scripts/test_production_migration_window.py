"""The production migration window 033-035 applies exactly revision 035.

Migrations 036 and 037 (decouple and drop the legacy objects) land on main in
later PRs. Each one needs its own explicit owner instruction, 036 before 037,
and a fresh SELECT showing zero legacy rows before it runs. ``alembic upgrade
head`` in any artifact that describes the 033-035 window would apply them in
the same run, so every such artifact must name the exact revision instead.
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts import asaas_production_wiring as wiring

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "infra/production/asaas-production-wiring.yaml"
PLANNER = ROOT / "scripts/asaas_production_wiring.py"
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


def test_no_production_migration_artifact_says_upgrade_head() -> None:
    artifacts = {
        "manifest": _text(MANIFEST),
        "planner": _text(PLANNER),
        "runbook": _text(RUNBOOK),
        "configuration bootstrap section": _section(
            _text(CONFIGURATION),
            "## Primeiro acesso: bootstrap do super_admin",
            r"^## ",
        ),
        "README production deploy": _section(_text(README), "### Produção", r"^---$"),
        "VPS runbook": _text(VPS_RUNBOOK),
        "VPS deploy script": _text(DEPLOY_VPS),
    }
    offenders = [name for name, text in artifacts.items() if UPGRADE_HEAD.search(text)]
    assert offenders == []


def test_runbook_migrations_gate_pins_035_and_reads_back_alembic_version() -> None:
    gate = _section(_text(RUNBOOK), "5. **Gate: migrations 033-035", r"^6\. \*\*Gate:")
    flat = " ".join(gate.split())
    assert "`alembic upgrade 035`" in flat
    assert "`alembic_version`" in flat
    assert "`032`" in flat
    assert "**Verify:** read back `alembic_version` == `035`" in flat
    # The exclusion of the later migrations and the owner conditions.
    for phrase in ("`036`", "`037`", "036 before 037", "zero legacy rows"):
        assert phrase in flat, phrase


def test_configuration_migrations_task_points_production_at_the_window() -> None:
    section = " ".join(
        _section(
            _text(CONFIGURATION),
            "## Primeiro acesso: bootstrap do super_admin",
            r"^## ",
        ).split()
    )
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


def test_deploy_vps_pin_is_the_single_window_revision() -> None:
    """The VPS pin, planner, manifest, runbook and configuration agree.

    Matching uses the planner's own broad ``alembic upgrade <target>`` pattern,
    so a relative or range target (``+1``, ``035@head``) is a mismatch too.
    """
    pin = _deploy_vps_pin()
    assert pin == wiring.MIGRATIONS_TARGET_REVISION
    assert wiring.MIGRATIONS_COMMAND == ("alembic", "upgrade", pin)
    window_texts = {
        "manifest step": _section(
            _text(MANIFEST), f"  - id: {wiring.MIGRATIONS_STEP}", r"^  - id: "
        ),
        "runbook gate": _section(
            _text(RUNBOOK), "5. **Gate: migrations 033-035", r"^6\. \*\*Gate:"
        ),
        "configuration bootstrap section": _section(
            _text(CONFIGURATION),
            "## Primeiro acesso: bootstrap do super_admin",
            r"^## ",
        ),
    }
    for name, text in window_texts.items():
        targets = wiring._ALEMBIC_UPGRADE.findall(text)
        assert targets and set(targets) == {pin}, (name, targets)
        assert wiring._UNPINNED_UPGRADE.search(text) is None, name
    declared = [
        path.name
        for path in ALEMBIC_VERSIONS.glob("*.py")
        if f'revision: str = "{pin}"' in path.read_text(encoding="utf-8")
    ]
    assert len(declared) == 1, declared


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
