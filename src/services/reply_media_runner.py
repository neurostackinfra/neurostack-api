"""Run the agent's media delivery after a reply (envio-midia-agente Group 6).

The four inline webhook replies and the credit response outbox call
:func:`start_reply_media` once the provider accepted the reply's text (its
2xx).  The delivery (:func:`~src.services.reply_media.deliver_reply_media`)
then runs as a task this process owns, never awaited by its caller:

* a webhook request answers as before and closes its session -- nothing is
  held while the delivery waits up to
  :data:`~src.services.reply_media.AGENT_MEDIA_PERMIT_WAIT_SECONDS` for the
  process's only ``agent_sends`` permit (the pool is 5 connections with no
  overflow: a few queued turns holding the request's would exhaust it);
* the outbox publisher's serial tick goes on to its next row at once: a
  media step of minutes inside the tick could push that row past
  ``REPLY_MAX_AGE_SECONDS`` and its text would never go.

Every database step of the delivery is a short session of its own from the
caller's factory, made cut-safe (:class:`CutSafeSession`), and the whole
delivery is under ONE outer bound, :data:`REPLY_MEDIA_OUTER_SECONDS`: the
delivery's own worst case (:data:`~src.services.reply_media.REPLY_MEDIA_MAX_SECONDS`)
plus :data:`REPLY_MEDIA_DB_SECONDS` for its database steps, which have no
bound of their own.  It is a backstop against a hung statement (no
``statement_timeout`` is set), not a latency budget: a delivery it cuts ends
with no notice (an alert is logged, ``reason="outer_bound"``), so it is
generous.  A statement it cuts
leaves its connection invalidated, never pooled -- the bounded writers' rule
(``credit_metering._record_bounded``).  On a single-connection pool (SQLite
``:memory:``) nothing is ever cut nor invalidated: there that would destroy
the database, so the delivery runs unbounded, as ``_record_bounded`` does.

The lifespan cancels the tasks still running before the database pool closes
(:func:`shutdown`): a delivery waiting for the permit leaves with no write; one
cut mid-send has consumed its 24-hour window with no notice -- the accepted
"crash between the claim and the send" (DESIGN, risk 7).  A record already
running finishes first (it is shielded and bounded).  The cancellation is
logged (``reason="shutdown"``, the session and media ids, never the phone)
and re-raised.

Both cuts are deliveries the patient never got after a text that announced
the file, so each one counts once in ``neurostack_agent_media_delivery_total``
as ``failed`` -- its log ``reason`` (``outer_bound`` / ``shutdown``) tells
them apart from a provider failure, as ``reason`` already does inside
``failed``.  A delivery already past its outcome (the delivery core counted
it, then the cut came during the file's record or the notice) is logged but
never counted twice: the runner sees the delivery reach that phase through
the two callables only it reaches -- the notice's sender and the record.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Final
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, SessionTransaction
from sqlalchemy.pool import SingletonThreadPool, StaticPool

from src.config.logging import get_logger
from src.observability.metrics import AGENT_MEDIA_DELIVERY_TOTAL
from src.services.reply_media import (
    DELIVERY_LOG,
    REPLY_MEDIA_MAX_SECONDS,
    PermitPool,
    RecordSentId,
    ReplyMediaChannel,
    deliver_reply_media,
)

logger = get_logger(__name__)

# The delivery's database steps with no bound of their own: the asset
# re-read, the credit mode (and the suspension gate), the 24-hour claim, the
# egress charge and the payload read (the observational egress and the two
# records are bounded by the delivery itself).
REPLY_MEDIA_DB_STEPS: Final = 5
# Their allowance: each step may first wait for a pooled connection up to
# ``database_pool_timeout`` (30 s by default, ``settings.py``), then runs a
# short statement.  5 x 30 s = 150 s (pinned against the setting's default).
REPLY_MEDIA_DB_SECONDS: Final = 150
# ONE outer bound per delivery and provider: Meta 375 + 150 = 525 s; Z-API,
# uazapi and Evolution 250 + 150 = 400 s.
REPLY_MEDIA_OUTER_SECONDS: Final[Mapping[str, float]] = MappingProxyType(
    {
        provider: seconds + REPLY_MEDIA_DB_SECONDS
        for provider, seconds in REPLY_MEDIA_MAX_SECONDS.items()
    }
)
REPLY_MEDIA_TASK_NAME: Final = "agent-media-delivery"


@dataclass(frozen=True)
class ReplyMediaJob:
    """One turn's file to deliver after its text: the bound channel (the
    provider the selection was given, its senders, the patient's phone), the
    attachment and the echo bookkeeping of the caller's path."""

    channel: ReplyMediaChannel
    media_id: UUID
    tenant_id: UUID
    session_id: UUID
    response_language: str
    record_sent_id: RecordSentId
    # The switch the webhook request captured at entry; ``None`` (the outbox)
    # reads the process-wide verdict when the delivery runs.
    credit_switch_on: bool | None = None


class CutSafeSession(AsyncSession):
    """A delivery session whose connection is INVALIDATED, never pooled,
    when a cancellation (the outer bound, a shutdown) interrupts it inside a
    transaction -- a statement may have been cut mid-flight, leaving the
    connection in an unknown state.  The connections its transactions
    engaged are tracked through the public ``after_begin`` session event;
    invalidating one closes the driver connection at once (no round trip),
    then the ordinary close discards it.  An ``Exception`` leaves the close
    as it is (the statement finished, with an error)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._engaged: list[Connection] = []
        event.listen(self.sync_session, "after_begin", self._engage)

    def _engage(
        self,
        _session: Session,
        _transaction: SessionTransaction,
        connection: Connection,
    ) -> None:
        self._engaged.append(connection)

    def invalidate_engaged(self) -> None:
        """Invalidate every engaged connection still open; never raises."""

        for connection in self._engaged:
            if connection.closed or connection.invalidated:
                continue
            with suppress(Exception):
                connection.invalidate()

    async def __aexit__(self, type_: Any, value: Any, traceback: Any) -> None:
        if (
            isinstance(type_, type)
            and issubclass(type_, BaseException)
            and not issubclass(type_, Exception)
            and self.in_transaction()
        ):
            self.invalidate_engaged()
        await super().__aexit__(type_, value, traceback)


def _single_connection_pool(factory: Callable[[], AsyncSession]) -> bool:
    """``True`` for a factory bound to a pool of ONE shared connection
    (``StaticPool``, ``SingletonThreadPool``: SQLite ``:memory:``)."""

    if not isinstance(factory, async_sessionmaker):
        return False
    pool = getattr(factory.kw.get("bind"), "pool", None)
    return isinstance(pool, (StaticPool, SingletonThreadPool))


def cut_safe_sessions(
    factory: Callable[[], AsyncSession],
) -> Callable[[], AsyncSession]:
    """``factory``'s sessions as :class:`CutSafeSession` (same bind and
    options).  A single-connection pool's factory, or one that is not an
    ``async_sessionmaker`` (a suite's fake), is returned as it is."""

    if not isinstance(factory, async_sessionmaker) or _single_connection_pool(factory):
        return factory
    return async_sessionmaker(class_=CutSafeSession, **factory.kw)


# The deliveries this process owns (one task each), for the drain and the
# lifespan's shutdown.
_TASKS: Final[set[asyncio.Task[None]]] = set()


def start_reply_media(
    job: ReplyMediaJob,
    *,
    session_factory: Callable[[], AsyncSession],
    agent_sends: PermitPool,
) -> asyncio.Task[None] | None:
    """Start the delivery of ``job`` as a task of its own and return at once;
    never raises (``None`` when it could not start, logged).  Call it only
    after the provider accepted the reply's text, holding no pooled
    connection: the delivery never uses the caller's session."""

    try:
        single = _single_connection_pool(session_factory)
        sessions = cut_safe_sessions(session_factory)
        task = asyncio.get_running_loop().create_task(
            _run(job, sessions, agent_sends, bounded=not single),
            name=REPLY_MEDIA_TASK_NAME,
        )
    except Exception as error:
        logger.error(
            DELIVERY_LOG,
            alert="agent_media_task",
            reason="not_started",
            error_type=type(error).__name__,
            **_fields(job),
        )
        return None
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


def _fields(job: ReplyMediaJob) -> dict[str, str]:
    return {
        "provider": job.channel.provider,
        "tenant_id": str(job.tenant_id),
        "session_id": str(job.session_id),
        "media_id": str(job.media_id),
    }


class _Outcome:
    """Whether the delivery core already counted this delivery's outcome.

    ``deliver_reply_media`` increments the delivery counter, then records
    the file's answer (``sent``: shielded, so the record runs even when the
    cut lands first), or enters the notice's sender before its first
    suspension (a notice outcome), or returns at once.  So a cut that finds
    neither the record nor the notice's sender entered came before the
    count; one that finds either came after it."""

    def __init__(self) -> None:
        self.counted = False

    def watch(
        self, channel: ReplyMediaChannel, record_sent_id: RecordSentId
    ) -> tuple[ReplyMediaChannel, RecordSentId]:
        send_text = channel.send_text

        async def notice(text: str) -> Mapping[str, object]:
            self.counted = True
            return await send_text(text)

        async def record(sent: Mapping[str, object]) -> None:
            self.counted = True
            await record_sent_id(sent)

        return replace(channel, send_text=notice), record


def _count_cut(job: ReplyMediaJob, outcome: _Outcome) -> bool:
    """Count a cut delivery once, as ``failed``; ``True`` if it counted."""

    if outcome.counted:
        return False
    AGENT_MEDIA_DELIVERY_TOTAL.labels(
        provider=job.channel.provider, outcome="failed"
    ).inc()
    return True


async def _run(
    job: ReplyMediaJob,
    sessions: Callable[[], AsyncSession],
    agent_sends: PermitPool,
    *,
    bounded: bool,
) -> None:
    bound = REPLY_MEDIA_OUTER_SECONDS[job.channel.provider] if bounded else None
    outcome = _Outcome()
    channel, record_sent_id = outcome.watch(job.channel, job.record_sent_id)
    try:
        async with asyncio.timeout(bound):
            await deliver_reply_media(
                channel=channel,
                media_id=job.media_id,
                tenant_id=job.tenant_id,
                session_id=job.session_id,
                response_language=job.response_language,
                session_factory=sessions,
                agent_sends=agent_sends,
                record_sent_id=record_sent_id,
                credit_switch_on=job.credit_switch_on,
            )
    except TimeoutError:
        logger.error(
            DELIVERY_LOG,
            alert="agent_media_outer_bound",
            reason="outer_bound",
            outcome="failed",
            counted=_count_cut(job, outcome),
            bound_seconds=bound,
            **_fields(job),
        )
    except asyncio.CancelledError:
        # Only the lifespan's shutdown (or the loop's close) cancels a
        # delivery: the outer bound's own cancellation is a TimeoutError.
        logger.error(
            DELIVERY_LOG,
            alert="agent_media_task",
            reason="shutdown",
            outcome="failed",
            counted=_count_cut(job, outcome),
            **_fields(job),
        )
        raise
    except Exception as error:  # the delivery never raises; defensive
        logger.error(
            DELIVERY_LOG,
            alert="agent_media_task",
            reason="task_failed",
            error_type=type(error).__name__,
            **_fields(job),
        )


def _own_tasks() -> list[asyncio.Task[None]]:
    """This loop's deliveries still running (a closed loop's are dropped)."""

    loop = asyncio.get_running_loop()
    mine = []
    for task in list(_TASKS):
        if task.get_loop() is loop:
            if not task.done():
                mine.append(task)
        elif task.get_loop().is_closed():
            _TASKS.discard(task)
    return mine


async def wait_idle() -> None:
    """Wait until every delivery of this loop has ended (tests, drains)."""

    while pending := _own_tasks():
        await asyncio.wait(pending)


async def shutdown() -> None:
    """Cancel and await every delivery of this loop; never raises.  Called by
    the lifespan before the database pool closes."""

    pending = _own_tasks()
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


__all__ = [
    "REPLY_MEDIA_DB_SECONDS",
    "REPLY_MEDIA_DB_STEPS",
    "REPLY_MEDIA_OUTER_SECONDS",
    "REPLY_MEDIA_TASK_NAME",
    "CutSafeSession",
    "ReplyMediaJob",
    "cut_safe_sessions",
    "shutdown",
    "start_reply_media",
    "wait_idle",
]
