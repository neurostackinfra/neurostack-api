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

Group 3b-2: every Z-API and Meta message reaching the pause check runs its
adapter's pre-check (``credit_metering.precheck_webhook``: switch on,
authenticated ``production`` and ``credit_enforcement_at`` set).  The
plugin wraps it the same way and counts its verdicts (``enforced``,
``credit_pending`` or ``main``): every existing webhook suite must show only
``main`` ones -- no enforced claim, debit, hold or outbox ever ran there.

Group 4a: knowledge and media routes read their credit mode once
(``credit_inventory.read_credit_mode``: ``off`` -- a suite's fake session or
request --, ``observational`` or ``enforced``) and, observationally, run the
inventory writers (``credit_inventory.record_observational_upload`` and
``record_observational_egress``).  The plugin wraps the three the same way
and counts their verdicts and outcomes: every existing media and knowledge
suite must show no ``enforced`` mode, so no reservation, maintenance or
egress debit ever ran there.

Group 6: every credit read route (``/v1/credits/*``) loads its one ledger
snapshot through ``credits.CreditSnapshot._load_book`` (assembled from the
stored rows and swept in memory, never persisted).  The plugin wraps that
method the same way and counts its outcomes (``builder``, ``standalone``):
the existing suites never call a credit read, so their forced runs must
show none, while the Group 6 suites exercise it under the forced switch.

Group 7: the "must show only ``main``" rules above are enforced, not only
printed.  Every enforced capture, enforced or ``credit_pending`` webhook
pre-check and enforced inventory mode is attributed to the test file that
was running; a file outside :data:`ENFORCED_SUITES` (the suites that set
``credit_enforcement_at`` themselves) fails the session, and the terminal
summary names each offending file and verdict.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
# The only suites allowed to record an enforced verdict under this plugin:
# each sets ``credit_enforcement_at`` (or an enforced mode) itself.
ENFORCED_SUITES = frozenset(
    {
        "tests/api/test_admin_credit_rollout.py",
        "tests/api/test_credit_category_gate.py",
        "tests/api/test_credit_media_assets.py",
        "tests/api/test_credit_principal_authorization.py",
        "tests/api/test_credit_rag.py",
        "tests/api/test_credit_suspension_events.py",
        "tests/api/test_credit_suspension_rag_media.py",
        "tests/api/test_credit_suspension_webhooks.py",
        "tests/api/test_credits.py",
        "tests/api/test_events_credit_enforced.py",
        "tests/integration/test_credit_enforcement_rollout_fence.py",
        "tests/integration/test_credit_ledger_postgres.py",
        "tests/integration/test_credit_maintenance_postgres.py",
        "tests/integration/test_credit_metering_postgres.py",
        "tests/integration/test_credit_retention_postgres.py",
        "tests/integration/test_credit_schema_dominance_postgres.py",
        "tests/services/test_credit_inventory.py",
        "tests/services/test_credit_ledger.py",
        "tests/services/test_credit_lifecycle.py",
        "tests/services/test_credit_metering.py",
        "tests/services/test_credit_response_publisher.py",
    }
)
_current_suite = "<outside a test>"
_unlisted_enforced: Counter[tuple[str, str]] = Counter()

_PATCHER = pytest.StashKey[pytest.MonkeyPatch]()
_reads = 0
_self_checks = 0
_writer_outcomes: Counter[str] = Counter()
_ORIGINAL_WRITER: Any = None
_captures: Counter[str] = Counter()
_ORIGINAL_CAPTURE: Any = None
_webhook_prechecks: Counter[str] = Counter()
_ORIGINAL_WEBHOOK_PRECHECK: Any = None
_inventory_modes: Counter[str] = Counter()
_inventory_writes: Counter[str] = Counter()
_ORIGINAL_MODE: Any = None
_ORIGINAL_UPLOAD_WRITER: Any = None
_ORIGINAL_EGRESS_WRITER: Any = None
_credit_reads: Counter[str] = Counter()
_ORIGINAL_SNAPSHOT_BOOK: Any = None


def _suite_of(item: pytest.Item) -> str:
    path = Path(str(item.path)).resolve()
    try:
        return path.relative_to(_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _enforced_verdict(kind: str) -> None:
    if _current_suite not in ENFORCED_SUITES:
        _unlisted_enforced[(_current_suite, kind)] += 1


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: Any) -> Iterator[None]:
    global _current_suite
    _current_suite = _suite_of(item)
    try:
        yield
    finally:
        _current_suite = "<outside a test>"


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if _unlisted_enforced and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


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
    if enforcement is not None:
        _enforced_verdict("enforced-path capture")
    return enforcement


async def _counted_webhook_precheck(*args: Any, **kwargs: Any) -> Any:
    try:
        verdict = await _ORIGINAL_WEBHOOK_PRECHECK(*args, **kwargs)
    except BaseException as error:
        _webhook_prechecks[f"raised {type(error).__name__}"] += 1
        raise
    if verdict is None:
        _webhook_prechecks["main"] += 1
    elif verdict == "credit_pending":
        _webhook_prechecks["credit_pending"] += 1
        _enforced_verdict("webhook pre-check credit_pending")
    else:
        _webhook_prechecks["enforced"] += 1
        _enforced_verdict("webhook pre-check enforced")
    return verdict


async def _counted_mode(*args: Any, **kwargs: Any) -> Any:
    try:
        mode = await _ORIGINAL_MODE(*args, **kwargs)
    except BaseException as error:
        _inventory_modes[f"raised {type(error).__name__}"] += 1
        raise
    if mode.enforced:
        _inventory_modes["enforced"] += 1
        _enforced_verdict("inventory mode enforced")
    elif mode.observational:
        _inventory_modes["observational"] += 1
    else:
        _inventory_modes["off"] += 1
    return mode


async def _counted_upload_writer(*args: Any, **kwargs: Any) -> Any:
    try:
        outcome = await _ORIGINAL_UPLOAD_WRITER(*args, **kwargs)
    except BaseException as error:
        _inventory_writes[f"upload raised {type(error).__name__}"] += 1
        raise
    _inventory_writes[f"upload {outcome}"] += 1
    return outcome


async def _counted_egress_writer(*args: Any, **kwargs: Any) -> Any:
    try:
        outcome = await _ORIGINAL_EGRESS_WRITER(*args, **kwargs)
    except BaseException as error:
        _inventory_writes[f"egress raised {type(error).__name__}"] += 1
        raise
    _inventory_writes[f"egress {outcome}"] += 1
    return outcome


async def _counted_snapshot_book(self: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        book = await _ORIGINAL_SNAPSHOT_BOOK(self, *args, **kwargs)
    except BaseException as error:
        _credit_reads[f"raised {type(error).__name__}"] += 1
        raise
    _credit_reads["standalone" if book is None else "builder"] += 1
    return book


def pytest_configure(config: pytest.Config) -> None:
    global _ORIGINAL_WRITER, _ORIGINAL_CAPTURE, _ORIGINAL_WEBHOOK_PRECHECK
    global _ORIGINAL_MODE, _ORIGINAL_UPLOAD_WRITER, _ORIGINAL_EGRESS_WRITER
    global _ORIGINAL_SNAPSHOT_BOOK
    from src.api.routes import credits
    from src.services import credit_inventory, credit_metering
    from src.services.credit_switch import CreditSwitch

    patcher = pytest.MonkeyPatch()
    patcher.setattr(CreditSwitch, "effective", _forced_effective)
    _ORIGINAL_WRITER = credit_metering.record_observational_trace
    patcher.setattr(credit_metering, "record_observational_trace", _counted_writer)
    _ORIGINAL_CAPTURE = credit_metering.capture_events_enforcement
    patcher.setattr(credit_metering, "capture_events_enforcement", _counted_capture)
    _ORIGINAL_WEBHOOK_PRECHECK = credit_metering.precheck_webhook
    patcher.setattr(credit_metering, "precheck_webhook", _counted_webhook_precheck)
    _ORIGINAL_MODE = credit_inventory.read_credit_mode
    patcher.setattr(credit_inventory, "read_credit_mode", _counted_mode)
    _ORIGINAL_UPLOAD_WRITER = credit_inventory.record_observational_upload
    patcher.setattr(
        credit_inventory, "record_observational_upload", _counted_upload_writer
    )
    _ORIGINAL_EGRESS_WRITER = credit_inventory.record_observational_egress
    patcher.setattr(
        credit_inventory, "record_observational_egress", _counted_egress_writer
    )
    _ORIGINAL_SNAPSHOT_BOOK = credits.CreditSnapshot._load_book
    patcher.setattr(credits.CreditSnapshot, "_load_book", _counted_snapshot_book)
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
    prechecks = ", ".join(
        f"{name}={count}" for name, count in sorted(_webhook_prechecks.items())
    )
    terminalreporter.write_line(
        f"credit webhook pre-checks: {sum(_webhook_prechecks.values())}"
        f" ({prechecks or 'none'})"
    )
    modes = ", ".join(
        f"{name}={count}" for name, count in sorted(_inventory_modes.items())
    )
    terminalreporter.write_line(
        f"credit inventory modes: {sum(_inventory_modes.values())}"
        f" ({modes or 'none'})"
    )
    writes = ", ".join(
        f"{name}={count}" for name, count in sorted(_inventory_writes.items())
    )
    terminalreporter.write_line(
        f"credit inventory writer: {sum(_inventory_writes.values())} calls"
        f" ({writes or 'none'})"
    )
    reads = ", ".join(
        f"{name}={count}" for name, count in sorted(_credit_reads.items())
    )
    terminalreporter.write_line(
        f"credit read snapshots: {sum(_credit_reads.values())}" f" ({reads or 'none'})"
    )
    if _unlisted_enforced:
        terminalreporter.write_line(
            "credit enforced verdicts in unlisted suites (session FAILED):", red=True
        )
        for (suite, kind), count in sorted(_unlisted_enforced.items()):
            terminalreporter.write_line(f"  {suite}: {kind}={count}", red=True)
    else:
        terminalreporter.write_line("credit enforced verdicts in unlisted suites: none")


def _assert_forced() -> None:
    global _self_checks
    from src.api.routes import credits
    from src.services import credit_inventory, credit_metering
    from src.services.credit_switch import CreditSwitch, credit_switch_effective

    assert CreditSwitch.effective is _forced_effective, "switch is not forced"
    assert (
        credit_metering.record_observational_trace is _counted_writer
    ), "the observational writer counter was swapped and not restored"
    assert (
        credit_metering.capture_events_enforcement is _counted_capture
    ), "the enforced-path capture counter was swapped and not restored"
    assert (
        credit_metering.precheck_webhook is _counted_webhook_precheck
    ), "the webhook pre-check counter was swapped and not restored"
    assert (
        credit_inventory.read_credit_mode is _counted_mode
        and credit_inventory.record_observational_upload is _counted_upload_writer
        and credit_inventory.record_observational_egress is _counted_egress_writer
    ), "a credit inventory counter was swapped and not restored"
    assert (
        credits.CreditSnapshot._load_book is _counted_snapshot_book
    ), "the credit read snapshot counter was swapped and not restored"
    _self_checks += 1
    assert credit_switch_effective() is True, "the forced switch does not read True"


@pytest.fixture(autouse=True)
def _credit_switch_forced_effective() -> Iterator[None]:
    _assert_forced()
    yield
    _assert_forced()
