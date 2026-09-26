"""README content and executable extraction contracts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from conftest import PACKAGE, ROOT, external_environment

README = PACKAGE / "README.md"
EXTRACTOR = ROOT / "scripts" / "extract-readme-quickstart.py"


def test_readme_states_source_status_state_and_domain_limits() -> None:
    text = " ".join(README.read_text(encoding="utf-8").split())
    required = (
        "unpublished",
        "source install",
        "not available from PyPI",
        "FlowManager",
        "reset_session(session_id)",
        "PolicyStore",
        "PolicyStore.clear()",
        "process-local state",
        "pt-BR",
        "aesthetic-clinic",
        "neurostack-core-dedomainification",
    )
    for phrase in required:
        assert phrase in text


@pytest.mark.parametrize(
    ("contents", "error"),
    (
        ("# Package\n", "no '## Quickstart' heading"),
        (
            "## Quickstart\n\n```bash\necho nope\n```\n\n## API\n```python\nprint('late')\n```\n",
            "no following Python code fence",
        ),
        (
            "## Quickstart\n\n```python trailing\nprint('bad')\n```\n",
            "malformed Python code fence",
        ),
        (
            "## Quickstart\n\n```python\nprint('open')\n",
            "unclosed code fence",
        ),
    ),
)
def test_quickstart_extractor_rejects_invalid_sections(
    tmp_path: Path,
    contents: str,
    error: str,
) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(contents, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(EXTRACTOR), str(readme)],
        cwd=tmp_path,
        env=external_environment(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert error in result.stderr
