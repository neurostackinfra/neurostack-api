"""Regression coverage for model modules with package-level runtime imports."""

import os
import subprocess
import sys
from pathlib import Path


def test_clinic_config_and_event_import_in_fresh_process() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import src.models.clinic_config; "
                "import src.models.event; "
                "from src.core.interaction_context import InteractionOrigin as legacy; "
                "from src.interaction_context import InteractionOrigin as pure; "
                "from src.handoff.pause_policy import parse_legacy_cycle_days as public; "
                "from src.pause_policy import parse_legacy_cycle_days as canonical; "
                "assert legacy is pure and public is canonical"
            ),
        ],
        check=False,
        capture_output=True,
        cwd=repository_root,
        env=environment,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_knowledge_service_and_context_orchestrator_import_in_fresh_process() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from src.services import KnowledgeService; "
                "from src.ctx import ContextOrchestrator; "
                "service = KnowledgeService(); "
                "orchestrator = ContextOrchestrator(knowledge_service=service); "
                "assert orchestrator._knowledge is service"
            ),
        ],
        check=False,
        capture_output=True,
        cwd=repository_root,
        env=environment,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
