"""Pytest plugin: run unchanged suites with the credit switch forced EFFECTIVE.

    pytest -p tests.credit_switch_forced_plugin tests/api/test_events.py ...

Dominance oracle, SQLite half (neurostack-credit-system Group 1b, item 5):
``tests/conftest.py`` only accepts ``sqlite+aiosqlite:///:memory:``, where
``init_database`` runs ``create_all`` without ``alembic_version``, so the real
switch is always off there.  This plugin replaces the verdict itself
(``CreditSwitch.effective``) for the whole session, so every reader --
``credit_switch_effective()``, the per-request capture at entry, the guards
and the gauge -- sees it on, whatever the lifespan refresh reads.  The
existing suites must pass unchanged both with and without it.

Before and after every test the plugin checks that the patch is in place AND
that ``credit_switch_effective()`` really reads ``True`` through it.  Those
self-checks are counted apart, so the terminal summary's read count is only
what the code under test read (non-zero: request code consulted the forced
switch).  ``tests/services/test_credit_switch.py`` runs a probe under this
plugin in a subprocess and asserts that ``True`` reaches
``request.state.credit_switch_effective``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

_PATCHER = pytest.StashKey[pytest.MonkeyPatch]()
_reads = 0
_self_checks = 0


def _forced_effective(_self: Any) -> bool:
    global _reads
    _reads += 1
    return True


def pytest_configure(config: pytest.Config) -> None:
    from src.services.credit_switch import CreditSwitch

    patcher = pytest.MonkeyPatch()
    patcher.setattr(CreditSwitch, "effective", _forced_effective)
    config.stash[_PATCHER] = patcher


def pytest_unconfigure(config: pytest.Config) -> None:
    patcher = config.stash.get(_PATCHER, None)
    if patcher is not None:
        patcher.undo()


def pytest_report_header(config: pytest.Config) -> str:
    return "credit switch: FORCED effective (tests.credit_switch_forced_plugin)"


def pytest_terminal_summary(terminalreporter: Any) -> None:
    terminalreporter.write_line(
        f"credit switch forced effective: {_reads - _self_checks} verdict reads "
        f"by the code under test ({_self_checks} plugin self-checks)"
    )


def _assert_forced() -> None:
    global _self_checks
    from src.services.credit_switch import CreditSwitch, credit_switch_effective

    assert CreditSwitch.effective is _forced_effective, "switch is not forced"
    _self_checks += 1
    assert credit_switch_effective() is True, "the forced switch does not read True"


@pytest.fixture(autouse=True)
def _credit_switch_forced_effective() -> Iterator[None]:
    _assert_forced()
    yield
    _assert_forced()
