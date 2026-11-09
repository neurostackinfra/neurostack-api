"""
NeuroStack API - Logging Configuration
Structured logging using structlog

Every standard-logging line is exactly one physical line: the root stdout
handler and uvicorn's own handlers escape the line-breaking characters of the
fully formatted record (``_SINGLE_LINE_ESCAPES``), so a body value or a
traceback can never start a new CloudWatch event.  With ``LOG_FORMAT=console``
(development only) ESC (``\x1b``) is left as it is, so the console renderer's
ANSI colours keep working; ESC cannot break a line.  JSON mode (production)
escapes the full table.

Every rendered line is also at most ``MAX_LINE_BYTES`` (4095) bytes of UTF-8,
so the line and its newline are one pipe write of at most ``PIPE_BUF`` (4096)
bytes, which the workers sharing stdout cannot tear (see the constant).  A
longer record keeps its first ``HEAD_BYTES`` (2048) and its last
``TAIL_BYTES`` (1900) bytes, cut on character boundaries before escaping (no
escape of this formatter is ever split), joined by ``...[N chars elided]...``
where N is the number of characters of the formatted record, before escaping,
that were dropped.  A line under the cap is unchanged by the cap (structlog
JSON stays byte-identical).  A JSON line over it can no longer be read as the
original record: it may not parse, or it may parse with the fields between
the cuts missing (the cut can fall inside one of JSON's own escapes, such as
``\\"``).  The work per record is bounded by the cap, whatever its size.
"""

import logging
import sys
from typing import Any, Final, Optional

import structlog
from structlog.types import Processor

# What makes a log line break or hide: every C0 control and DEL, plus the
# Unicode line separators (NEL, LINE SEPARATOR, PARAGRAPH SEPARATOR).  The
# awslogs driver starts a new CloudWatch event at every newline, so one raw
# ``\n`` in a body value would forge a whole line.  Backslashes are left as
# they are: a line without these characters renders byte-identical.
_SINGLE_LINE_ESCAPES: Final[dict[int, str]] = {
    **{code: f"\\x{code:02x}" for code in (*range(0x20), 0x7F)},
    ord("\r"): "\\r",
    ord("\n"): "\\n",
    ord("\t"): "\\t",
    **{code: f"\\u{code:04x}" for code in (0x85, 0x2028, 0x2029)},
    # Lone surrogates (the JSON decoder lets them through): UTF-8 cannot
    # encode them, so the stream write would fail and logging would print a
    # multi-line ``--- Logging error ---`` block instead of the record.
    **{code: f"\\u{code:04x}" for code in range(0xD800, 0xE000)},
}


# Console mode (development): the same table but ESC, for ANSI colours.
_CONSOLE_SINGLE_LINE_ESCAPES: Final[dict[int, str]] = {
    code: escaped for code, escaped in _SINGLE_LINE_ESCAPES.items() if code != 0x1B
}
# uvicorn's own loggers, whose handlers do not propagate to the root.
_UVICORN_LOGGERS: Final = ("uvicorn", "uvicorn.error", "uvicorn.access")


# The rendered line's bound: UTF-8 bytes, newline excluded, so a record plus
# its newline is at most 4095 + 1 = 4096 bytes = Linux ``PIPE_BUF``.  The 4
# uvicorn workers (``Dockerfile`` ``--workers 4``) share one stdout pipe, and
# the kernel makes a pipe ``write(2)`` atomic only up to ``PIPE_BUF``: a longer
# record written while the pipe is backed up tears at 4096-byte steps, another
# worker's line lands inside it, and its rest starts a new line and a new
# CloudWatch event that a body value can aim.  This relies on
# ``PYTHONUNBUFFERED=1`` (``Dockerfile`` :21 and :154): each record is then
# one ``write(2)``; a buffered stdout would flush several records at once and
# tear again.  The outer bound, the 16384-byte piece the Fargate log shim
# cuts a longer line into, is therefore never reached.  Pinned by
# ``tests/config/test_logging_single_line.py`` (not asserted here: macOS has
# a 512-byte ``PIPE_BUF``).
MAX_LINE_BYTES: Final = 4095
# What an elided record keeps: at most this many bytes, escaped, of its start
# and of its end (the end keeps a traceback's exception line); with the
# marker they stay under ``MAX_LINE_BYTES``.
HEAD_BYTES: Final = 2048
TAIL_BYTES: Final = 1900
_ELISION_MARKER: Final = "...[{elided} chars elided]..."


def _translate(text: str, table: dict[int, str]) -> str:
    """The one place a record's text is escaped (bounded by the callers)."""

    return text.translate(table)


def _plain(text: str) -> bool:
    """Printable ASCII: nothing to escape, one byte per character."""

    return text.isascii() and text.isprintable()


def _escape(text: str, table: dict[int, str]) -> str:
    return text if _plain(text) else _translate(text, table)


def _escaped_bytes(char: str, table: dict[int, str]) -> int:
    escaped = table.get(ord(char))
    if escaped is not None:
        return len(escaped)
    code = ord(char)
    return 1 if code < 0x80 else 2 if code < 0x800 else 3 if code < 0x10000 else 4


def _fitting_chars(text: str, budget: int, table: dict[int, str]) -> int:
    """How many leading characters of *text* fit, escaped, in *budget* bytes.

    *text* is a slice of at most *budget* characters (every character takes
    at least one byte), so this is bounded by the cap.
    """

    if _plain(text):
        return min(len(text), budget)
    used = 0
    for count, char in enumerate(text):
        used += _escaped_bytes(char, table)
        if used > budget:
            return count
    return len(text)


def _single_line(text: str, *, keep_ansi: bool = False) -> str:
    """*text* escaped onto one line of at most ``MAX_LINE_BYTES`` bytes."""

    table = _CONSOLE_SINGLE_LINE_ESCAPES if keep_ansi else _SINGLE_LINE_ESCAPES
    if len(text) <= MAX_LINE_BYTES:
        if _plain(text):
            return text  # the fast path: nothing to escape, under the cap
        escaped = _translate(text, table)
        if len(escaped.encode("utf-8", "surrogatepass")) <= MAX_LINE_BYTES:
            return escaped
    # Over the cap: keep a head and a tail, cut on raw characters (never
    # inside a multi-byte character or an escape), then escape only those.
    head = _fitting_chars(text[:HEAD_BYTES], HEAD_BYTES, table)
    rest = len(text) - head
    tail_window = text[len(text) - min(rest, TAIL_BYTES) :][::-1]
    tail = _fitting_chars(tail_window, TAIL_BYTES, table)
    elided = len(text) - head - tail
    return (
        _escape(text[:head], table)
        + _ELISION_MARKER.format(elided=elided)
        + _escape(text[len(text) - tail :], table)
    )


def _unformattable(record: logging.LogRecord) -> str:
    try:
        fallback = (
            f"unformattable log record: logger={record.name!r} "
            f"msg={record.msg!r} args={record.args!r}"
        )
        return _single_line(fallback)
    except Exception:
        return "unformattable log record"


class SingleLineFormatter(logging.Formatter):
    """``logging.Formatter`` whose every record is exactly one physical line.

    The fully formatted record (message, ``exc_text`` and ``stack_info``) has
    its line-breaking characters escaped (``_SINGLE_LINE_ESCAPES``): a
    traceback becomes one line with ``\\n`` escapes, and a body value cannot
    start a forged line.  ``keep_ansi`` (console mode) leaves ESC.  Never
    raises.
    """

    def __init__(self, fmt: Optional[str] = None, *, keep_ansi: bool = False):
        super().__init__(fmt)
        self.keep_ansi = keep_ansi

    def format(self, record: logging.LogRecord) -> str:
        try:
            return _single_line(super().format(record), keep_ansi=self.keep_ansi)
        except Exception:
            return _unformattable(record)


class SingleLineDelegatingFormatter(logging.Formatter):
    """Another handler's formatter, kept as it is, then made one line.

    Wraps uvicorn's own formatters (``DefaultFormatter`` keeps its look):
    ``format`` calls the wrapped one and escapes its result like
    ``SingleLineFormatter``.  Never raises.
    """

    def __init__(self, inner: logging.Formatter, *, keep_ansi: bool = False):
        super().__init__()
        self.inner = inner
        self.keep_ansi = keep_ansi

    def format(self, record: logging.LogRecord) -> str:
        try:
            return _single_line(self.inner.format(record), keep_ansi=self.keep_ansi)
        except Exception:
            return _unformattable(record)


def _single_line_uvicorn_handlers(*, keep_ansi: bool) -> None:
    """Wrap the formatter of every handler uvicorn's loggers have now; once.

    The uvicorn CLI configures its logging (``Config.configure_logging``,
    again in each worker process) before it imports the application, which
    runs ``configure_logging``: these handlers exist by then.
    """

    for name in _UVICORN_LOGGERS:
        for handler in logging.getLogger(name).handlers:
            current = handler.formatter
            if isinstance(
                current, (SingleLineDelegatingFormatter, SingleLineFormatter)
            ):
                continue
            handler.setFormatter(
                SingleLineDelegatingFormatter(
                    current or logging.Formatter(), keep_ansi=keep_ansi
                )
            )


def configure_logging() -> None:
    """Configure structured logging for the application."""
    # Import settings here to avoid circular import
    from src.config.settings import settings

    # Shared processors for all log outputs
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    # Processors for structlog
    if settings.log_format == "json":
        # JSON format for production
        processors: list[Processor] = [
            *shared_processors,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        # Console format for development
        processors = [
            *shared_processors,
            structlog.dev.ConsoleRenderer(colors=True),
        ]

    # Configure structlog
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Configure standard logging.  ``basicConfig`` does nothing when the root
    # logger already has handlers; when it creates the stdout handler, that
    # handler renders every record as one physical line.
    root = logging.getLogger()
    adds_handler = not root.handlers
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.log_level),
    )
    keep_ansi = settings.log_format != "json"
    if adds_handler:
        for handler in root.handlers:
            handler.setFormatter(
                SingleLineFormatter("%(message)s", keep_ansi=keep_ansi)
            )

    # Reduce noise from third-party libraries.
    #
    # ``uvicorn.access`` is fully silenced (not merely raised to WARNING)
    # because its default formatter logs the raw request path. The
    # Z-API webhook path
    # ``/v1/webhook/zapi/{capability_token}`` embeds the capability token
    # in the segment, so any raw-path record — even at DEBUG in a rescued
    # production incident — is a secret leak. The application emits its
    # own post-routing access log through
    # :class:`src.api.middleware.tracing.RequestIdMiddleware`, which uses
    # the route template instead of the raw path.
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.setLevel(logging.CRITICAL + 1)
    access_logger.disabled = True
    access_logger.propagate = False
    for handler in list(access_logger.handlers):
        access_logger.removeHandler(handler)
    _single_line_uvicorn_handlers(keep_ansi=keep_ansi)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: Optional[str] = None) -> structlog.stdlib.BoundLogger:
    """Get a structured logger instance."""
    return structlog.stdlib.get_logger(name)


class LogContext:
    """Context manager for adding temporary log context."""

    def __init__(self, **kwargs: Any) -> None:
        self.context = kwargs
        self.token: Any = None

    def __enter__(self) -> "LogContext":
        self.token = structlog.contextvars.bind_contextvars(**self.context)
        return self

    def __exit__(self, *args: Any) -> None:
        structlog.contextvars.unbind_contextvars(*self.context.keys())


def bind_context(**kwargs: Any) -> None:
    """Bind context variables for logging."""
    structlog.contextvars.bind_contextvars(**kwargs)


def unbind_context(*keys: str) -> None:
    """Unbind context variables from logging."""
    structlog.contextvars.unbind_contextvars(*keys)


def clear_context() -> None:
    """Clear all context variables."""
    structlog.contextvars.clear_contextvars()
