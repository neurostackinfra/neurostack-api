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

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "infra/production/asaas-production-wiring.yaml"
PLANNER = ROOT / "scripts/asaas_production_wiring.py"
RUNBOOK = ROOT / "docs/runbooks/asaas-deployment.md"
CONFIGURATION = ROOT / "docs/configuration.md"
README = ROOT / "README.md"
VPS_RUNBOOK = ROOT / "docs/api-provider-settings-rollout.md"
DEPLOY_VPS = ROOT / "scripts/deploy-vps.sh"

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
    index = next(i for i, line in enumerate(lines) if "alembic upgrade" in line)
    guard = " ".join(
        line.strip().lstrip("#").strip()
        for line in lines[max(0, index - 6) : index]
        if line.strip().startswith("#")
    )
    assert "compose-local Postgres" in guard
    assert "never the AWS production database" in guard
    assert "036" in guard and "037" in guard
    assert "docs/runbooks/asaas-deployment.md" in guard
