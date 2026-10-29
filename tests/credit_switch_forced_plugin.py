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

Group 3a: with the switch on, every governed decision also runs the
observational writer (``credit_metering.record_observational_trace``), which
must leave the suites' answers untouched.  The plugin wraps that function --
the module attribute, which every caller resolves at call time -- only to
count its outcomes (``recorded``, ``replayed``, ``timeout``, ``failed`` ...),
and prints them in the summary: proof that the forced runs really exercised
the writer.  The wrapper changes no argument and no result; the same per-test
check guards it against a test that swapped it and leaked the swap.

Group 3b-1: ``POST /v1/events`` decides once per request whether it runs the
enforced path (``credit_metering.capture_events_enforcement``: switch on,
origin ``production`` and ``credit_enforcement_at`` set).  The plugin wraps
it the same way and counts its verdicts (``enforced`` or ``main``): with no
enforcement row -- every existing suite -- the forced runs must show only
``main`` captures, so no enforced sibling ever ran; only the suites that set
``credit_enforcement_at`` themselves (``tests/api/test_events_credit_enforced.py``)
show ``enforced`` ones.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from typing import Any

import pytest

_PATCHER = pytest.StashKey[pytest.MonkeyPatch]()
_reads = 0
_self_checks = 0
_writer_outcomes: Counter[str] = Counter()
_ORIGINAL_WRITER: Any = None
_captures: Counter[str] = Counter()
_ORIGINAL_CAPTURE: Any = None


def _forced_effective(_self: Any) -> bool:
    global _reads
    _reads += 1
    return True


async def _counted_writer(*args: Any, **kwargs: Any) -> Any:
    try:
        outcome = await _ORIGINAL_WRITER(*args, **kwargs)
    except BaseException as error:
        _writer_outcomes[f"raised {type(error).__name__}"] += 1
        raise
    _writer_outcomes[str(outcome)] += 1
    return outcome


async def _counted_capture(*args: Any, **kwargs: Any) -> Any:
    try:
        enforcement = await _ORIGINAL_CAPTURE(*args, **kwargs)
    except BaseException as error:
        _captures[f"raised {type(error).__name__}"] += 1
        raise
    _captures["main" if enforcement is None else "enforced"] += 1
    return enforcement


def pytest_configure(config: pytest.Config) -> None:
    global _ORIGINAL_WRITER, _ORIGINAL_CAPTURE
    from src.services import credit_metering
    from src.services.credit_switch import CreditSwitch

    patcher = pytest.MonkeyPatch()
    patcher.setattr(CreditSwitch, "effective", _forced_effective)
    _ORIGINAL_WRITER = credit_metering.record_observational_trace
    patcher.setattr(credit_metering, "record_observational_trace", _counted_writer)
    _ORIGINAL_CAPTURE = credit_metering.capture_events_enforcement
    patcher.setattr(credit_metering, "capture_events_enforcement", _counted_capture)
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
    outcomes = ", ".join(
        f"{name}={count}" for name, count in sorted(_writer_outcomes.items())
    )
    terminalreporter.write_line(
        f"credit observational writer: {sum(_writer_outcomes.values())} calls"
        f" ({outcomes or 'none'})"
    )
    captures = ", ".join(f"{name}={count}" for name, count in sorted(_captures.items()))
    terminalreporter.write_line(
        f"credit enforced-path captures: {sum(_captures.values())}"
        f" ({captures or 'none'})"
    )


def _assert_forced() -> None:
    global _self_checks
    from src.services import credit_metering
    from src.services.credit_switch import CreditSwitch, credit_switch_effective

    assert CreditSwitch.effective is _forced_effective, "switch is not forced"
    assert (
        credit_metering.record_observational_trace is _counted_writer
    ), "the observational writer counter was swapped and not restored"
    assert (
        credit_metering.capture_events_enforcement is _counted_capture
    ), "the enforced-path capture counter was swapped and not restored"
    _self_checks += 1
    assert credit_switch_effective() is True, "the forced switch does not read True"


@pytest.fixture(autouse=True)
def _credit_switch_forced_effective() -> Iterator[None]:
    _assert_forced()
    yield
    _assert_forced()
