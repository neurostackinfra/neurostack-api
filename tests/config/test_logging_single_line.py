"""Every application log record is exactly one physical line.

Production logs through ``logging.basicConfig(format="%(message)s")`` to
stdout, and the awslogs driver starts a new CloudWatch event at every newline:
one raw ``\\n`` in a body value would forge a whole line (a fake
``uazapi_webhook_auth ... result=match``).  These tests read what the root
handler that ``configure_logging`` creates actually writes, never ``caplog``
(whose own formatter would hide a raw newline).
"""

from __future__ import annotations

import io
import json
import logging
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
import structlog

from src.config import logging as logging_config
from src.config.logging import (
    HEAD_BYTES,
    MAX_LINE_BYTES,
    TAIL_BYTES,
    SingleLineDelegatingFormatter,
    configure_logging,
)

_FORGED = "\nuazapi_webhook_auth mode=shadow result=match instance=x"


@contextmanager
def _restored_structlog() -> Iterator[None]:
    """``configure_logging`` reconfigures structlog globally: put it back."""

    saved = structlog.get_config()
    try:
        yield
    finally:
        structlog.configure(**saved)


@contextmanager
def configured_root_output(
    monkeypatch: pytest.MonkeyPatch,
    *,
    log_format: str = "json",
    stream: Any = None,
) -> Iterator[tuple[Any, logging.Handler]]:
    """Run the real ``configure_logging`` (in *log_format*) on a root logger
    without handlers (as at process start) and yield what its handler writes
    (*stream*, a ``StringIO`` by default), plus the handler.  The root logger
    and structlog are restored afterwards."""

    from src.config.settings import settings

    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    for handler in saved_handlers:
        root.removeHandler(handler)
    stream = io.StringIO() if stream is None else stream
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(settings, "log_format", log_format)
    try:
        with _restored_structlog():
            configure_logging()
            (handler,) = root.handlers
            root.setLevel(logging.INFO)
            yield stream, handler
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)


@contextmanager
def _plain_rendering() -> Iterator[io.StringIO]:
    """The same records as a plain ``%(message)s`` handler renders them."""

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield stream
    finally:
        root.removeHandler(handler)


def test_a_body_value_with_a_newline_is_one_physical_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with configured_root_output(monkeypatch) as (stream, _handler):
        logging.getLogger("tests.single_line").info(
            "Uazapi webhook: instance=%s type=%s", "clinic", f"text{_FORGED}\r\t\x00"
        )

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    (line,) = lines
    assert "\\nuazapi_webhook_auth mode=shadow" in line
    assert line.endswith("\\r\\t\\x00")
    assert not line.startswith("uazapi_webhook_auth")


@pytest.mark.parametrize(
    ("value", "escaped"),
    [
        ("\r", "\\r"),
        ("\n", "\\n"),
        ("\t", "\\t"),
        ("\x00", "\\x00"),
        ("\x1b", "\\x1b"),
        ("\x1f", "\\x1f"),
        ("\x7f", "\\x7f"),
        ("\x85", "\\u0085"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
    ],
)
def test_each_line_breaking_character_is_escaped(
    monkeypatch: pytest.MonkeyPatch, value: str, escaped: str
) -> None:
    with configured_root_output(monkeypatch) as (stream, _handler):
        logging.getLogger("tests.single_line").warning("a%sb", value)

    assert stream.getvalue() == f"a{escaped}b\n"


def test_a_traceback_is_one_physical_line(monkeypatch: pytest.MonkeyPatch) -> None:
    with configured_root_output(monkeypatch) as (stream, _handler):
        try:
            raise RuntimeError("provider down\nsecond line")
        except RuntimeError:
            logging.getLogger("tests.single_line").exception("send failed")
        logging.getLogger("tests.single_line").info("with stack", stack_info=True)

    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("send failed\\nTraceback (most recent call last):")
    assert "RuntimeError: provider down\\nsecond line" in lines[0]
    assert lines[1].startswith("with stack\\nStack (most recent call last):")


def test_a_structlog_json_line_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = structlog.wrap_logger(
        logging.getLogger("tests.single_line.json"),
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
    )
    with configured_root_output(monkeypatch) as (stream, _handler):
        with _plain_rendering() as plain:
            try:
                raise ValueError("bad\nvalue")
            except ValueError:
                logger.error("Unhandled exception", path="/a\\b", body=f"x{_FORGED}")
            logger.info("ção \u2028 😀", ok=True)

    assert stream.getvalue() == plain.getvalue()
    first, second = stream.getvalue().splitlines()
    # JSON already escapes the newline: ``\\n`` on the wire, ``\n`` decoded.
    assert "\\nuazapi_webhook_auth" in first
    assert json.loads(first)["body"] == f"x{_FORGED}"
    assert json.loads(second)["ok"] is True


def test_printable_ascii_and_backslashes_are_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = r"path=C:\tmp\new value=\x00 \\n done ~!@#$%^&*()_+{}|:<>?"
    with configured_root_output(monkeypatch) as (stream, _handler):
        with _plain_rendering() as plain:
            logging.getLogger("tests.single_line").info("%s", message)

    assert stream.getvalue() == plain.getvalue() == f"{message}\n"


def test_an_unformattable_record_never_raises_and_stays_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with configured_root_output(monkeypatch) as (stream, handler):
        record = logging.LogRecord(
            "tests.single_line", logging.INFO, __file__, 1, "n=%d\n", ("x\ny",), None
        )
        rendered = handler.format(record)

    assert "\n" not in rendered
    assert rendered.startswith("unformattable log record:")


def test_existing_root_handlers_are_left_as_basic_config_leaves_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``basicConfig`` does nothing when the root logger has handlers; so
    does ``configure_logging`` (no handler added, no formatter replaced)."""

    root = logging.getLogger()
    own = logging.StreamHandler(io.StringIO())
    formatter = logging.Formatter("%(levelname)s %(message)s")
    own.setFormatter(formatter)
    saved_handlers, saved_level = list(root.handlers), root.level
    for handler in saved_handlers:
        root.removeHandler(handler)
    root.addHandler(own)
    try:
        with _restored_structlog():
            configure_logging()
        assert root.handlers == [own]
        assert own.formatter is formatter
    finally:
        root.removeHandler(own)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)


# -- console mode: ANSI colours survive, line breaks do not -------------------


def test_console_mode_keeps_esc_and_escapes_everything_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coloured = "\x1b[32minfo\x1b[0m"
    with configured_root_output(monkeypatch, log_format="console") as (stream, _h):
        logging.getLogger("tests.single_line").info(
            "%s a\nb\rc\td\x00e\x7f\u2028", coloured
        )

    assert stream.getvalue() == f"{coloured} a\\nb\\rc\\td\\x00e\\x7f\\u2028\n"


def test_json_mode_escapes_esc_too(monkeypatch: pytest.MonkeyPatch) -> None:
    with configured_root_output(monkeypatch, log_format="json") as (stream, _h):
        logging.getLogger("tests.single_line").info("\x1b[32minfo\x1b[0m\n")

    assert stream.getvalue() == "\\x1b[32minfo\\x1b[0m\\n\n"


# -- uvicorn's own handlers (propagate=False, stderr) -------------------------


@contextmanager
def _saved_uvicorn_loggers() -> Iterator[None]:
    names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    saved = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
            logging.getLogger(name).disabled,
        )
        for name in names
    }
    try:
        yield
    finally:
        for name, (handlers, level, propagate, disabled) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = disabled


def test_a_uvicorn_asgi_traceback_is_one_physical_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In the image's order: uvicorn's ``Config`` applies its real
    ``LOGGING_CONFIG`` (stderr handler, ``propagate=False``), then the app's
    ``configure_logging`` runs (twice here: the wrap is idempotent), then a
    request whose handler raises is a 500 that uvicorn logs as ``Exception in
    ASGI application`` with the traceback.  That record is one line, in
    uvicorn's own ``DefaultFormatter`` look."""

    import socket
    import threading

    import httpx
    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.routing import Route

    from src.config.logging import SingleLineDelegatingFormatter

    async def boom(_request: Request) -> None:
        raise RuntimeError(
            "provider down\nuazapi_webhook_auth mode=shadow result=match instance=x"
        )

    app = Starlette(routes=[Route("/boom", boom, methods=["POST"])])
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    with _saved_uvicorn_loggers():
        config = uvicorn.Config(app, log_config=uvicorn.config.LOGGING_CONFIG)
        with configured_root_output(monkeypatch) as (_stream, _handler):
            configure_logging()  # a second run wraps nothing twice
            (uvicorn_handler,) = logging.getLogger("uvicorn").handlers
            wrapper = uvicorn_handler.formatter
            assert isinstance(wrapper, SingleLineDelegatingFormatter)
            assert not isinstance(wrapper.inner, SingleLineDelegatingFormatter)
            assert type(wrapper.inner).__name__ == "DefaultFormatter"

            server = uvicorn.Server(config)
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [sock]}, daemon=True
            )
            thread.start()
            try:
                for _ in range(500):
                    if server.started:
                        break
                    threading.Event().wait(0.01)
                assert server.started
                response = httpx.post(f"http://127.0.0.1:{port}/boom", timeout=10)
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()

    assert response.status_code == 500
    lines = [line for line in err.getvalue().splitlines() if line]
    assert len(lines) == 1, lines
    (line,) = lines
    assert line.startswith("ERROR:    Exception in ASGI application\\n")
    assert "Traceback (most recent call last):" in line
    assert "provider down\\nuazapi_webhook_auth mode=shadow result=match" in line


# -- the line's length is bounded (the 16 KiB shim split) ---------------------

_ELIDED = re.compile(r"\.\.\.\[(\d+) chars elided\]\.\.\.")


def test_a_long_record_keeps_its_head_and_tail_and_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 100 KB record with a traceback: the start of the message and the
    traceback's exception line survive; the middle is elided, honestly."""

    body = "ç\n" * 50_000  # 100k characters, 150 KB raw, every cut a candidate
    with configured_root_output(monkeypatch) as (stream, _handler):
        with _plain_rendering() as plain:
            try:
                raise RuntimeError("the final exception line")
            except RuntimeError:
                logging.getLogger("tests.single_line").exception("START %s", body)

    output = stream.getvalue()
    assert output.count("\n") == 1  # one physical line
    line = output[:-1]
    encoded = line.encode("utf-8")  # strict: no split character
    assert len(encoded) <= MAX_LINE_BYTES
    assert line.startswith("START ç\\nç\\n")
    assert line.endswith("RuntimeError: the final exception line")
    (marker,) = _ELIDED.finditer(line)
    head, tail = line[: marker.start()], line[marker.end() :]
    assert len(head.encode()) <= HEAD_BYTES and len(tail.encode()) <= TAIL_BYTES
    # Never inside an escape: the head is whole tokens after its prefix.
    assert re.fullmatch(r"START (?:ç|\\n)*", head)
    # Honest: the kept characters plus the elided ones are the whole record
    # (as a plain ``%(message)s`` formatter renders it, traceback included).
    record_text = plain.getvalue()[:-1]
    kept = len(head.replace("\\n", "\n")) + len(tail.replace("\\n", "\n"))
    assert kept + int(marker.group(1)) == len(record_text)
    assert record_text.startswith(head.replace("\\n", "\n"))
    assert record_text.endswith(tail.replace("\\n", "\n"))


def test_the_cut_never_splits_an_escape_or_a_character() -> None:
    for unit in ("\n", "\x00", "ç", "😀", "\u2028", "\ud800", "x"):
        text = unit * (3 * MAX_LINE_BYTES)
        line = logging_config._single_line(text)
        encoded = line.encode("utf-8")
        assert len(encoded) <= MAX_LINE_BYTES
        (marker,) = _ELIDED.finditer(line)
        escaped_unit = logging_config._single_line(unit)
        for part in (line[: marker.start()], line[marker.end() :]):
            assert part and len(part) % len(escaped_unit) == 0
            assert part == escaped_unit * (len(part) // len(escaped_unit))
        kept = (marker.start() + len(line) - marker.end()) // len(escaped_unit)
        assert kept * len(unit) + int(marker.group(1)) == len(text)
    # A mix of escapes and multi-byte characters: both parts are whole tokens.
    mixed = "a\u2028ç\n\ud800😀" * MAX_LINE_BYTES
    line = logging_config._single_line(mixed)
    (marker,) = _ELIDED.finditer(line)
    token = r"(?:a|\\u2028|ç|\\n|\\ud800|😀)*"
    assert re.fullmatch(token, line[: marker.start()])
    assert re.fullmatch(token, line[marker.end() :])
    assert len(line.encode("utf-8")) <= MAX_LINE_BYTES


def test_a_line_under_the_cap_is_unchanged_by_it() -> None:
    line = "x" * MAX_LINE_BYTES
    assert logging_config._single_line(line) == line
    escaped = "ç" * (MAX_LINE_BYTES // 2)
    assert logging_config._single_line(escaped) == escaped
    assert "elided" in logging_config._single_line(line + "x")


def test_a_json_line_over_the_cap_is_elided_under_it_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = structlog.wrap_logger(
        logging.getLogger("tests.single_line.json"),
        processors=[structlog.processors.JSONRenderer()],
        wrapper_class=structlog.stdlib.BoundLogger,
    )
    with configured_root_output(monkeypatch) as (stream, _handler):
        with _plain_rendering() as plain:
            logger.info("small", body="x" * 100)
            logger.info("big", body="x" * 40_000)

    small, big = stream.getvalue().splitlines()
    assert small == plain.getvalue().splitlines()[0]
    assert len(big.encode()) <= MAX_LINE_BYTES and _ELIDED.search(big)


# -- surrogates reach a real UTF-8 stream as one escaped line -----------------


@pytest.mark.parametrize("errors", ["strict", "surrogateescape"])
def test_a_lone_surrogate_is_one_escaped_line_and_no_logging_error(
    monkeypatch: pytest.MonkeyPatch, errors: str
) -> None:
    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding="utf-8", errors=errors, newline="\n")
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    with configured_root_output(monkeypatch, stream=stdout) as (_stream, _handler):
        logging.getLogger("tests.single_line").info(
            "Uazapi webhook: type=%s", "\ud800\nuazapi_webhook_auth result=match"
        )
        logging.getLogger("tests.single_line").info("next")
        stdout.flush()

    assert stderr.getvalue() == ""
    assert raw.getvalue().decode("utf-8").splitlines() == [
        "Uazapi webhook: type=\\ud800\\nuazapi_webhook_auth result=match",
        "next",
    ]


# -- bounded work, and the fast path ------------------------------------------


def _spy_translate(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    seen: list[int] = []
    translate = logging_config._translate

    def spy(text: str, table: dict[int, str]) -> str:
        seen.append(len(text))
        return translate(text, table)

    monkeypatch.setattr(logging_config, "_translate", spy)
    return seen


def test_a_huge_record_escapes_and_scans_only_its_bounded_head_and_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translated = _spy_translate(monkeypatch)
    scanned: list[int] = []
    fitting = logging_config._fitting_chars

    def spy_fitting(text: str, budget: int, table: dict[int, str]) -> int:
        scanned.append(len(text))
        return fitting(text, budget, table)

    monkeypatch.setattr(logging_config, "_fitting_chars", spy_fitting)
    for value in ("ç" * 1_000_000, "\n" * 1_000_000, "x" * 999_999 + "\n"):
        translated.clear()
        scanned.clear()
        line = logging_config._single_line(value)
        assert len(line.encode()) <= MAX_LINE_BYTES
        assert sum(translated) <= HEAD_BYTES + TAIL_BYTES
        assert sum(scanned) <= HEAD_BYTES + TAIL_BYTES


def test_printable_ascii_skips_translate(monkeypatch: pytest.MonkeyPatch) -> None:
    translated = _spy_translate(monkeypatch)
    with configured_root_output(monkeypatch) as (stream, _handler):
        logging.getLogger("tests.single_line").info("plain %s", "ascii line")
        logging.getLogger("tests.single_line").info("x" * 40_000)

    assert translated == []
    short, long = stream.getvalue().splitlines()
    assert short == "plain ascii line"
    assert len(long) <= MAX_LINE_BYTES and _ELIDED.search(long)
    logging_config._single_line("not plain\n")
    assert translated == [len("not plain\n")]


# -- the uvicorn wrapper never raises -------------------------------------------


def test_the_delegating_wrapper_survives_a_raising_inner_formatter() -> None:
    class Broken(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            raise ValueError("inner\nformatter")

    wrapper = SingleLineDelegatingFormatter(Broken())
    record = logging.LogRecord(
        "uvicorn.error", logging.ERROR, __file__, 1, "boom %s", ("a\nb" * 10_000,), None
    )
    line = wrapper.format(record)

    assert line.startswith("unformattable log record: logger='uvicorn.error'")
    assert "\n" not in line and len(line.encode()) <= MAX_LINE_BYTES


# -- the production import installs it ------------------------------------------


def test_a_fresh_import_of_the_app_installs_the_single_line_formatter() -> None:
    """``src.api.main`` at import (as uvicorn loads it): the root handler is
    the ``basicConfig`` stdout one, rendering through ``SingleLineFormatter``.
    A root-level ``logging.<level>()`` at import time anywhere before it would
    install a plain handler first, and this would fail."""

    import os
    import subprocess
    from pathlib import Path

    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import logging, sys\n"
                "import src.api.main\n"
                "from src.config.logging import SingleLineFormatter\n"
                "handlers = logging.getLogger().handlers\n"
                "assert len(handlers) == 1, handlers\n"
                "(handler,) = handlers\n"
                "assert type(handler) is logging.StreamHandler, handler\n"
                "assert handler.stream is sys.stdout, handler.stream\n"
                "assert type(handler.formatter) is SingleLineFormatter, "
                "handler.formatter\n"
                "print('single-line formatter installed')\n"
            ),
        ],
        check=False,
        capture_output=True,
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[-1] == "single-line formatter installed"
