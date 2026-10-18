"""Periodic, multi-process-safe delivery and escalation of ``billing.fact.v1``.

One loop runs per API worker process when ``BILLING_FACT_WORKER_ENABLED`` is
true (2 ECS tasks x 4 uvicorn workers in production), so every guarantee here
is a database guarantee, never a process-local one:

* **Publish phase** (only when a consumer transport is wired): each fact is
  claimed in its own short transaction ``FOR UPDATE OF billing_fact_deliveries
  SKIP LOCKED``; the row stays locked until its attempt is committed, so no
  two loops ever present the same fact concurrently and the persisted
  backoff stops a re-presentation until it is due (not after a crash
  between a send and its commit; see the presentation guard below).  At most ``batch_size``
  facts per tick and no new claim after the tick budget.
* **Maintenance phase** (reconcile + overdue escalation): serialized across
  all processes by ``pg_try_advisory_xact_lock``; a loop that does not get
  the lock skips the phase instead of waiting.  Both steps are bounded pages.
* **Observation**: every loop recomputes the global backlog gauges from the
  database, so any process a scrape reaches reports the global state.

Presentation is not crash-safe yet (``PRESENTATION_PRECONDITIONS_OPEN`` in
``billing_outbox``): constructing a worker with a transport raises, so the
only production wiring is ``transport=None``.

Liveness: :meth:`BillingFactWorker.start` stamps the last-tick gauge, so a
loop that never completes a tick still exports a non-zero timestamp that
ages into ``BillingFactWorkerStalled``; a disabled process exports ``0``.

Without a consumer (``transport=None``, today) nothing is presented and no
attempt is counted: facts stay ``pending`` and retriable, and the stale gauge
and ``ack overdue`` alert rows fire after the threshold.  This module never
imports or calls credit or ledger code, and never deletes or acknowledges a
fact; only the reconciler marks ``acknowledged``, from a consumer-owned ack.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config.logging import get_logger
from src.config.settings import Settings
from src.observability.metrics import (
    BILLING_FACT_BACKLOG,
    BILLING_FACT_PUBLISH_ATTEMPTS_TOTAL,
    BILLING_FACT_WORKER_LAST_TICK,
    BILLING_FACT_WORKER_TICKS_TOTAL,
)
from src.services.billing_outbox import (
    BillingFactPublisher,
    BillingFactReconciler,
    FactBacklog,
    FactRetryPolicy,
    PublishAttempt,
    require_presentation_allowed,
)

logger = get_logger(__name__)

FactTransport = Callable[[dict[str, object]], Awaitable[None]]
TickOutcome = Literal["completed", "deadline", "stopped"]

# A presentation that does not return within this window is a failed attempt.
TRANSPORT_TIMEOUT_SECONDS: Final = 10.0
# Lifespan shutdown waits this long for the in-flight fact before cancelling
# it (its transaction then rolls back and the fact stays retriable).
SHUTDOWN_GRACE_SECONDS: Final = 15.0


def billing_fact_maintenance_advisory_key() -> int:
    """Stable signed bigint key serializing reconcile/escalation globally."""

    digest = hashlib.sha256(b"neurostack:billing-fact-maintenance:global").digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


@dataclass(frozen=True, slots=True)
class BillingFactWorkerConfig:
    """Bounded per-tick work; validated again here for direct construction."""

    interval_seconds: float
    batch_size: int
    max_attempts: int
    stale_after_seconds: float
    transport_timeout_seconds: float = TRANSPORT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if (
            self.interval_seconds <= 0
            or self.batch_size <= 0
            or self.stale_after_seconds <= 0
            or self.transport_timeout_seconds <= 0
        ):
            raise ValueError("billing fact worker bounds must be positive")
        # Validates max_attempts with the same rule the claim uses.
        FactRetryPolicy(max_attempts=self.max_attempts)

    @classmethod
    def from_settings(cls, source: Settings) -> BillingFactWorkerConfig:
        return cls(
            interval_seconds=float(source.billing_fact_worker_interval_seconds),
            batch_size=source.billing_fact_worker_batch_size,
            max_attempts=source.billing_fact_worker_max_attempts,
            stale_after_seconds=float(source.billing_fact_worker_stale_after_seconds),
        )

    @property
    def retry_policy(self) -> FactRetryPolicy:
        return FactRetryPolicy(max_attempts=self.max_attempts)


@dataclass(frozen=True, slots=True)
class TickReport:
    """What one tick did in this process (sanitized: counts and states only)."""

    outcome: TickOutcome
    maintained: bool
    presented: int
    failed: int
    exhausted: int
    escalated: int
    backlog: FactBacklog


def _set_metric(metric: Any, value: float, **labels: str) -> None:
    try:
        (metric.labels(**labels) if labels else metric).set(value)
    except Exception:  # pragma: no cover - telemetry is best effort
        logger.warning("billing fact metric emission failed")


def _count(metric: Any, **labels: str) -> None:
    try:
        metric.labels(**labels).inc()
    except Exception:  # pragma: no cover - telemetry is best effort
        logger.warning("billing fact metric emission failed")


async def _try_maintenance_lock(session: AsyncSession) -> bool:
    """Take the global maintenance lock for this transaction, never waiting."""

    if session.get_bind().dialect.name != "postgresql":
        return True  # single-process SQLite development/test databases
    acquired = await session.scalar(
        select(func.pg_try_advisory_xact_lock(billing_fact_maintenance_advisory_key()))
    )
    return bool(acquired)


class BillingFactWorker:
    """Run publisher + reconciler ticks until stopped; one per process."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        transport: FactTransport | None,
        config: BillingFactWorkerConfig,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if transport is not None:
            require_presentation_allowed()
        self._session_factory = session_factory
        self._transport = transport
        self._config = config
        self._policy = config.retry_policy
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._cursor: UUID | None = None

    @property
    def consumer_ready(self) -> bool:
        return self._transport is not None

    async def run_tick(self) -> TickReport:
        """One bounded pass: maintenance, publication, then observation."""

        deadline = self._monotonic() + self._config.interval_seconds
        maintained, escalated = await self._maintain()
        outcome: TickOutcome = "completed"
        presented = failed = exhausted = 0
        if self._transport is not None:
            for _ in range(self._config.batch_size):
                if self._stop.is_set():
                    outcome = "stopped"
                    break
                if self._monotonic() >= deadline:
                    outcome = "deadline"
                    break
                attempt = await self._publish_one(self._transport)
                if attempt is None:
                    break
                if attempt.outcome == "presented":
                    presented += 1
                elif attempt.outcome == "failed":
                    failed += 1
                    if attempt.failure == "CancelledError":
                        logger.error(
                            "billing fact transport raised a stray cancellation",
                            failure_class=attempt.failure,
                            attempt_count=attempt.attempt_count,
                        )
                    logger.warning(
                        "billing fact presentation failed",
                        failure_class=attempt.failure,
                        attempt_count=attempt.attempt_count,
                    )
                elif attempt.outcome == "exhausted":
                    exhausted += 1
                    logger.warning(
                        "billing fact retry cap exhausted",
                        attempt_count=attempt.attempt_count,
                    )
                if attempt.outcome != "skipped":
                    _count(BILLING_FACT_PUBLISH_ATTEMPTS_TOTAL, outcome=attempt.outcome)
        backlog = await self._observe()
        _count(BILLING_FACT_WORKER_TICKS_TOTAL, outcome=outcome)
        _set_metric(BILLING_FACT_WORKER_LAST_TICK, self._now().timestamp())
        if escalated:
            logger.warning("billing fact acknowledgement overdue", facts=escalated)
        return TickReport(
            outcome=outcome,
            maintained=maintained,
            presented=presented,
            failed=failed,
            exhausted=exhausted,
            escalated=escalated,
            backlog=backlog,
        )

    async def run_forever(self) -> None:
        """Tick every ``interval_seconds`` until :meth:`stop` (lifespan task)."""

        while not self._stop.is_set():
            try:
                await self.run_tick()
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise  # shutdown or a deliberate cancel of this loop
                # A stray CancelledError (not aimed at this task) must not
                # silently end delivery: count it and keep the loop alive.
                _count(BILLING_FACT_WORKER_TICKS_TOTAL, outcome="failed")
                logger.error(
                    "billing fact worker tick failed",
                    failure_class="CancelledError",
                )
            except Exception as exc:
                # Sanitized: the class only, never SQL parameters or payloads.
                _count(BILLING_FACT_WORKER_TICKS_TOTAL, outcome="failed")
                logger.error(
                    "billing fact worker tick failed",
                    failure_class=type(exc).__name__,
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._config.interval_seconds
                )

    def start(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._stop.clear()
            # Liveness baseline: a loop that fails every tick from boot is
            # still visible to the stall rule once this stamp ages out.
            _set_metric(BILLING_FACT_WORKER_LAST_TICK, self._now().timestamp())
            self._task = asyncio.create_task(
                self.run_forever(), name="billing-fact-worker"
            )
        return self._task

    async def stop(self, *, grace: float = SHUTDOWN_GRACE_SECONDS) -> None:
        """Finish the in-flight fact, then exit; cancel it after ``grace``.

        The loop checks the stop request before every claim, so a graceful
        stop never starts new work.  A cancelled claim's transaction rolls
        back: the fact keeps its previous state and is presented again later.
        """

        self._stop.set()
        task = self._task
        if task is None:
            return
        if task.done():
            if task.cancelled() or task.exception() is not None:
                # The loop ended before anyone asked it to: delivery stopped.
                logger.error(
                    "billing fact worker loop was not running",
                    failure_class=(
                        "CancelledError"
                        if task.cancelled()
                        else type(task.exception()).__name__
                    ),
                )
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=grace)
        except TimeoutError:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                task.cancel()  # our caller is being cancelled: stop the loop too
                raise
            # Otherwise the loop task itself was cancelled elsewhere: stopped.
        except Exception as exc:
            # Shutdown must continue to the pool close whatever the loop did.
            logger.error(
                "billing fact worker stop failed", failure_class=type(exc).__name__
            )

    async def _maintain(self) -> tuple[bool, int]:
        async with self._session_factory() as session:
            async with session.begin():
                if not await _try_maintenance_lock(session):
                    return False, 0
                reconciler = BillingFactReconciler(session, now=self._now)
                page = await reconciler.reconcile_open(
                    limit=self._config.batch_size, after=self._cursor
                )
                escalated = await reconciler.escalate_overdue(
                    stale_before=self._stale_before(),
                    limit=self._config.batch_size,
                )
        self._cursor = page.next_after
        return True, escalated

    async def _publish_one(self, transport: FactTransport) -> PublishAttempt | None:
        async with self._session_factory() as session:
            async with session.begin():
                return await BillingFactPublisher(session, now=self._now).publish_next(
                    transport,
                    policy=self._policy,
                    timeout=self._config.transport_timeout_seconds,
                )

    async def _observe(self) -> FactBacklog:
        async with self._session_factory() as session:
            backlog = await BillingFactReconciler(session, now=self._now).backlog(
                stale_before=self._stale_before()
            )
        _set_metric(BILLING_FACT_BACKLOG, backlog.unacked, condition="unacked")
        _set_metric(BILLING_FACT_BACKLOG, backlog.stale, condition="stale")
        _set_metric(BILLING_FACT_BACKLOG, backlog.blocked, condition="blocked")
        return backlog

    def _stale_before(self) -> datetime:
        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc) - timedelta(
            seconds=self._config.stale_after_seconds
        )


def start_billing_fact_worker(
    source: Settings,
    session_factory: Callable[[], async_sessionmaker[AsyncSession]],
    *,
    transport: FactTransport | None = None,
) -> BillingFactWorker | None:
    """Start the lifespan loop only when the operator flag is on.

    ``session_factory`` is a provider so the flag-off path never touches the
    database pool.  ``transport`` is the consumer's in-process inbox handoff;
    no consumer exists yet, so production passes ``None`` and facts stay
    pending (and alert) instead of being presented anywhere.  Passing a
    transport raises while the presentation preconditions are open.
    """

    if not source.billing_fact_worker_enabled:
        return None
    worker = BillingFactWorker(
        session_factory(),
        transport=transport,
        config=BillingFactWorkerConfig.from_settings(source),
    )
    worker.start()
    logger.info(
        "billing fact worker started",
        consumer="present" if worker.consumer_ready else "absent",
        interval_seconds=source.billing_fact_worker_interval_seconds,
        batch_size=source.billing_fact_worker_batch_size,
        max_attempts=source.billing_fact_worker_max_attempts,
    )
    return worker


__all__ = [
    "SHUTDOWN_GRACE_SECONDS",
    "TRANSPORT_TIMEOUT_SECONDS",
    "BillingFactWorker",
    "BillingFactWorkerConfig",
    "FactTransport",
    "TickReport",
    "billing_fact_maintenance_advisory_key",
    "start_billing_fact_worker",
]
