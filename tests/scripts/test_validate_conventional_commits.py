from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "scripts" / "validate-conventional-commits.py"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env={**os.environ, **GIT_ENV},
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(repo: Path, subject: str) -> None:
    git(repo, "commit", "--allow-empty", "-q", "-m", subject)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "chore: base")
    git(tmp_path, "tag", "base")
    return tmp_path


def validate(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), *args],
        cwd=repo,
        env={**os.environ, **GIT_ENV},
        check=False,
        capture_output=True,
        text=True,
    )


def test_accepts_conventional_subjects(repo: Path) -> None:
    for subject in (
        "feat(billing): wire hosted checkout",
        "fix!: drop legacy shape",
        "docs: add runbooks",
        "revert: feat(billing): wire hosted checkout",
    ):
        commit(repo, subject)

    result = validate(repo, "--range", "base..HEAD")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 checked" in result.stdout


@pytest.mark.parametrize(
    "subject",
    [
        "Update billing page",
        "feat:missing space",
        "feature(billing): unknown type",
        "feat(): empty scope",
        "feat(two words): spaced scope",
        "feat: ",
        'Revert "feat(billing): wire hosted checkout"',
    ],
)
def test_rejects_non_conventional_subject(repo: Path, subject: str) -> None:
    commit(repo, "feat: fine")
    commit(repo, subject)

    result = validate(repo, "--range", "base..HEAD")

    assert result.returncode == 1
    assert "non-conventional commit subjects" in result.stdout
    assert subject.strip() in result.stdout


def test_skips_merge_commits(repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "topic")
    commit(repo, "feat: topic work")
    git(repo, "checkout", "-q", "main")
    commit(repo, "fix: main work")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'topic'", "topic")

    result = validate(repo, "--range", "base..HEAD")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 checked" in result.stdout


def test_empty_range_fails_unless_allowed(repo: Path) -> None:
    empty = validate(repo, "--range", "base..HEAD")
    assert empty.returncode == 1
    assert "no commits in range" in empty.stderr

    allowed = validate(repo, "--range", "base..HEAD", "--allow-empty")
    assert allowed.returncode == 0
    assert "0 checked" in allowed.stdout


def test_invalid_range_propagates_git_failure(repo: Path) -> None:
    result = validate(repo, "--range", "missing-ref..HEAD")

    assert result.returncode != 0
    assert result.stderr


def test_rejects_option_like_range(repo: Path) -> None:
    result = validate(repo, "--range=--all")

    assert result.returncode == 2
