"""The media concurrency pools of ``src/api/middleware/media_limits.py``.

``uploads`` and ``downloads`` (the Central de Mídias routes) never queue: an
exhausted pool refuses at once, as before the agent's pool learned to wait
(their middleware and route tests are ``tests/api/test_media_assets*.py``).
``agent_sends`` is a :class:`QueuedLimiter` (envio-midia-agente, owner
2026-11-08: the bounded wait of the delivery's step 4).  Pinned here:

* a free permit is taken at once; a released one goes to the oldest waiter,
  never back to the pool while one waits, so a newcomer -- waiting or not
  -- never overtakes the queue; with no waiter it returns to the pool;
* three waiters are served in arrival order;
* a waiter that times out or is cancelled leaves the queue, holds nothing
  and strands no other waiter;
* the classic race: a permit handed to a waiter cancelled before it resumed
  passes on to the next one, never leaks; one handed over at the deadline
  is kept (the slot did open);
* a waiter on another event-loop thread is woken on its own loop; one whose
  loop closed passes its permit on, and its coroutine, once collected,
  releases nothing;
* a stranded waiter's coroutine closed INSIDE a critical section of the
  same thread -- where CPython 3.12 runs a collection that falls due as the
  lock is taken -- deadlocks nothing (the lock is re-entrant), whether its
  permit was taken back or it still holds one, which it then passes on;
* the wait's bound and the limit are validated.
"""

from __future__ import annotations

import asyncio
import gc
import math
import threading
from collections import deque
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from src.api.middleware.media_limits import (
    AGENT_MEDIA_SENDS_PER_PROCESS,
    ImmediateLimiter,
    ImmediatePermit,
    MediaConcurrencyLimiters,
    QueuedLimiter,
)

_WAIT = 5.0


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(_WAIT):
        while not condition():
            await asyncio.sleep(0)


def _held(limiter: QueuedLimiter) -> ImmediatePermit:
    permit = limiter.try_acquire()
    assert permit is not None
    return permit


# ---------------------------------------------------------------------------
# The three pools.
# ---------------------------------------------------------------------------


async def test_uploads_and_downloads_still_refuse_at_once_while_an_agent_send_waits() -> (
    None
):
    limiters = MediaConcurrencyLimiters(uploads=1, downloads=1)
    assert type(limiters.uploads) is ImmediateLimiter
    assert type(limiters.downloads) is ImmediateLimiter
    assert type(limiters.agent_sends) is QueuedLimiter
    assert limiters.agent_sends.limit == AGENT_MEDIA_SENDS_PER_PROCESS
    # The panel's pools have no way to wait.
    assert not hasattr(limiters.uploads, "acquire")
    assert not hasattr(limiters.downloads, "acquire")

    agent = _held(limiters.agent_sends)
    waiter = asyncio.create_task(limiters.agent_sends.acquire(_WAIT))
    await _until(lambda: limiters.agent_sends.waiting == 1)
    upload, download = limiters.uploads.try_acquire(), limiters.downloads.try_acquire()
    assert upload is not None and download is not None
    # Exhausted: refused at once, with an agent send queued meanwhile.
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert limiters.uploads.try_acquire() is None
    assert limiters.downloads.try_acquire() is None
    assert loop.time() - started < 0.05
    upload.release()
    download.release()
    assert (limiters.uploads.active, limiters.downloads.active) == (0, 0)
    assert not waiter.done()

    agent.release()
    permit = await asyncio.wait_for(waiter, _WAIT)
    assert permit is not None
    permit.release()
    assert limiters.agent_sends.active == 0


# ---------------------------------------------------------------------------
# The queue.
# ---------------------------------------------------------------------------


async def test_a_free_permit_is_taken_at_once_and_returns_to_the_pool() -> None:
    limiter = QueuedLimiter(1)
    permit = await asyncio.wait_for(limiter.acquire(60), 0.5)
    assert permit is not None
    assert (limiter.active, limiter.waiting) == (1, 0)
    permit.release()
    permit.release()  # idempotent
    assert (limiter.active, limiter.waiting) == (0, 0)
    again = limiter.try_acquire()
    assert again is not None
    again.release()


async def test_a_released_permit_goes_to_the_waiter_and_no_newcomer_overtakes() -> None:
    limiter = QueuedLimiter(1)
    held = _held(limiter)
    first = asyncio.create_task(limiter.acquire(_WAIT))
    await _until(lambda: limiter.waiting == 1)
    assert limiter.try_acquire() is None

    held.release()
    # Handed over, not returned: the pool stays full for the waiter even
    # before it resumes, so neither a try_acquire nor a new waiter takes it.
    assert (limiter.active, limiter.waiting) == (1, 0)
    assert limiter.try_acquire() is None
    late = asyncio.create_task(limiter.acquire(_WAIT))
    permit = await asyncio.wait_for(first, _WAIT)
    assert permit is not None
    await _until(lambda: limiter.waiting == 1)
    assert not late.done()

    permit.release()
    late_permit = await asyncio.wait_for(late, _WAIT)
    assert late_permit is not None
    late_permit.release()
    assert (limiter.active, limiter.waiting) == (0, 0)


async def test_three_waiters_are_served_in_arrival_order() -> None:
    limiter = QueuedLimiter(1)
    held = _held(limiter)
    served: list[int] = []

    async def turn(number: int) -> None:
        permit = await limiter.acquire(_WAIT)
        assert permit is not None
        served.append(number)
        await asyncio.sleep(0.01)  # hold it a little: the others keep waiting
        permit.release()

    turns = []
    for number in (1, 2, 3):
        turns.append(asyncio.create_task(turn(number)))
        await _until(lambda: limiter.waiting == len(turns))
    held.release()
    await asyncio.wait_for(asyncio.gather(*turns), _WAIT)

    assert served == [1, 2, 3]
    assert (limiter.active, limiter.waiting) == (0, 0)


async def test_a_waiter_that_times_out_leaves_the_queue_and_strands_nobody() -> None:
    limiter = QueuedLimiter(1)
    held = _held(limiter)
    loop = asyncio.get_running_loop()
    started = loop.time()
    short = asyncio.create_task(limiter.acquire(0.05))
    await _until(lambda: limiter.waiting == 1)
    patient = asyncio.create_task(limiter.acquire(_WAIT))
    await _until(lambda: limiter.waiting == 2)

    assert await asyncio.wait_for(short, _WAIT) is None
    assert loop.time() - started >= 0.05
    assert (limiter.active, limiter.waiting) == (1, 1)

    held.release()
    permit = await asyncio.wait_for(patient, _WAIT)
    assert permit is not None
    permit.release()
    assert (limiter.active, limiter.waiting) == (0, 0)


async def test_a_cancelled_waiter_leaves_the_queue_and_the_next_one_gets_it() -> None:
    limiter = QueuedLimiter(1)
    held = _held(limiter)
    cancelled = asyncio.create_task(limiter.acquire(_WAIT))
    await _until(lambda: limiter.waiting == 1)
    after = asyncio.create_task(limiter.acquire(_WAIT))
    await _until(lambda: limiter.waiting == 2)

    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert (limiter.active, limiter.waiting) == (1, 1)

    held.release()
    permit = await asyncio.wait_for(after, _WAIT)
    assert permit is not None
    permit.release()
    assert (limiter.active, limiter.waiting) == (0, 0)


async def test_a_permit_handed_to_a_waiter_cancelled_before_it_resumed_passes_on() -> (
    None
):
    """The classic race: the release hands the permit over (the waiter's
    future resolved) and the waiter is cancelled before it runs again.  The
    permit must reach the next waiter, never leak with the leaving one."""

    limiter = QueuedLimiter(1)
    held = _held(limiter)
    leaving = asyncio.create_task(limiter.acquire(_WAIT))
    await _until(lambda: limiter.waiting == 1)
    after = asyncio.create_task(limiter.acquire(_WAIT))
    await _until(lambda: limiter.waiting == 2)

    held.release()  # handed to ``leaving`` ...
    leaving.cancel()  # ... which is cancelled before it resumes
    with pytest.raises(asyncio.CancelledError):
        await leaving

    permit = await asyncio.wait_for(after, _WAIT)
    assert permit is not None
    assert (limiter.active, limiter.waiting) == (1, 0)
    permit.release()
    assert (limiter.active, limiter.waiting) == (0, 0)


async def test_a_permit_handed_to_a_cancelled_last_waiter_returns_to_the_pool() -> None:
    limiter = QueuedLimiter(1)
    held = _held(limiter)
    leaving = asyncio.create_task(limiter.acquire(_WAIT))
    await _until(lambda: limiter.waiting == 1)

    held.release()
    leaving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leaving

    assert (limiter.active, limiter.waiting) == (0, 0)
    again = limiter.try_acquire()
    assert again is not None
    again.release()


async def test_a_permit_handed_over_at_the_deadline_is_kept() -> None:
    """The release lands in the loop iteration whose timeout fires: the
    waiter ends by ``TimeoutError`` holding a handed-over permit, which it
    keeps (the slot did open) rather than leak or drop."""

    limiter = QueuedLimiter(1)
    held = _held(limiter)
    waiter = asyncio.create_task(limiter.acquire(0))
    await asyncio.sleep(0)  # queued; its zero timeout is now due
    assert limiter.waiting == 1
    held.release()

    permit = await asyncio.wait_for(waiter, _WAIT)
    assert permit is not None
    assert (limiter.active, limiter.waiting) == (1, 0)
    permit.release()
    assert limiter.active == 0


# ---------------------------------------------------------------------------
# Event-loop threads.
# ---------------------------------------------------------------------------


def test_a_waiter_on_another_event_loop_thread_is_woken_on_its_own_loop() -> None:
    limiter = QueuedLimiter(1)
    held = _held(limiter)
    queued = threading.Event()
    got: list[bool] = []

    def other_thread() -> None:
        async def wait() -> None:
            task = asyncio.ensure_future(limiter.acquire(_WAIT))
            await _until(lambda: limiter.waiting == 1)
            queued.set()
            permit = await task
            got.append(permit is not None)
            if permit is not None:
                permit.release()

        asyncio.run(wait())

    thread = threading.Thread(target=other_thread)
    thread.start()
    assert queued.wait(_WAIT)
    held.release()  # this thread runs no loop: woken through its own loop
    thread.join(_WAIT)

    assert not thread.is_alive()
    assert got == [True]
    assert (limiter.active, limiter.waiting) == (0, 0)


def test_a_waiter_whose_loop_closed_passes_its_permit_on_and_releases_nothing_later() -> (
    None
):
    limiter = QueuedLimiter(1)
    held = _held(limiter)
    dead = asyncio.new_event_loop()
    stranded = dead.create_task(limiter.acquire(_WAIT))
    dead.run_until_complete(asyncio.sleep(0.01))  # queued, then never run again
    dead.close()
    assert limiter.waiting == 1

    held.release()

    assert (limiter.active, limiter.waiting) == (0, 0)
    again = limiter.try_acquire()
    assert again is not None
    # Collecting the stranded coroutine closes it (``GeneratorExit`` at its
    # wait): the permit was taken back from it, so it releases nothing --
    # ``again`` is still the only permit held.
    stranded._log_destroy_pending = False  # type: ignore[attr-defined]
    del stranded
    gc.collect()
    assert (limiter.active, limiter.waiting) == (1, 0)
    assert limiter.try_acquire() is None
    again.release()
    assert limiter.active == 0


class _ClosingDeque(deque[Any]):
    """The limiter's queue, closing the given coroutines right after an
    ``append``, i.e. inside :meth:`QueuedLimiter.acquire`'s critical section:
    where CPython 3.12 runs a collection that fell due as the lock was taken
    (at the eval-breaker check after that C call), and the collector closes
    a garbage coroutine exactly so (``GeneratorExit`` at its ``await``)."""

    def __init__(self, closing: list[Coroutine[Any, Any, Any]]) -> None:
        super().__init__()
        self.closing = closing

    def append(self, item: Any) -> None:
        super().append(item)
        while self.closing:
            self.closing.pop().close()


def _in_a_daemon_thread(work: Callable[[], None]) -> None:
    """Run ``work`` in a daemon thread: a deadlock fails the test instead of
    hanging the suite."""

    errors: list[BaseException] = []

    def run() -> None:
        try:
            work()
        except BaseException as error:  # re-raised below
            errors.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(_WAIT)
    assert not thread.is_alive(), "deadlock: a finalizer re-entered the lock"
    if errors:
        raise errors[0]


def _stranded(limiter: QueuedLimiter, *, granted: bool) -> asyncio.Task[Any]:
    """A waiter queued on a loop that then closes, with its permit taken back
    (``granted=False``) or handed over before the close (``granted=True``:
    its wake was scheduled, and the close drops it)."""

    held = _held(limiter)
    dead = asyncio.new_event_loop()
    task = dead.create_task(limiter.acquire(_WAIT))
    dead.run_until_complete(asyncio.sleep(0.01))
    if granted:
        held.release()
        dead.close()
    else:
        dead.close()
        held.release()
    task._log_destroy_pending = False  # type: ignore[attr-defined]
    return task


def test_a_stranded_waiter_closed_inside_a_critical_section_does_not_deadlock() -> None:
    limiter = QueuedLimiter(1)
    stranded = _stranded(limiter, granted=False)
    assert (limiter.active, limiter.waiting) == (0, 0)  # taken back
    blocker = _held(limiter)
    limiter._waiters = _ClosingDeque([stranded.get_coro()])

    def queue_then_time_out() -> None:
        assert asyncio.run(limiter.acquire(0.01)) is None

    _in_a_daemon_thread(queue_then_time_out)

    assert stranded.get_coro().cr_frame is None  # closed, released nothing
    assert (limiter.active, limiter.waiting) == (1, 0)
    blocker.release()
    assert (limiter.active, limiter.waiting) == (0, 0)


def test_a_stranded_waiter_holding_a_permit_passes_it_on_from_inside_a_critical_section() -> (
    None
):
    """Re-entered from inside ``acquire``'s critical section, the stranded
    waiter's ``GeneratorExit`` path passes its permit on -- to the very
    waiter just queued there, woken at once on its own loop."""

    limiter = QueuedLimiter(1)
    stranded = _stranded(limiter, granted=True)
    assert (limiter.active, limiter.waiting) == (1, 0)  # held by the stranded
    limiter._waiters = _ClosingDeque([stranded.get_coro()])

    def queue_and_get_it() -> None:
        async def queue() -> None:
            permit = await asyncio.wait_for(limiter.acquire(_WAIT), _WAIT)
            assert permit is not None
            assert (limiter.active, limiter.waiting) == (1, 0)
            permit.release()

        asyncio.run(queue())

    _in_a_daemon_thread(queue_and_get_it)

    assert stranded.get_coro().cr_frame is None
    assert (limiter.active, limiter.waiting) == (0, 0)


# ---------------------------------------------------------------------------
# Validation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("timeout", [-0.001, math.inf, math.nan])
async def test_the_wait_must_be_finite_and_non_negative(timeout: float) -> None:
    limiter = QueuedLimiter(1)
    with pytest.raises(ValueError):
        await limiter.acquire(timeout)
    assert (limiter.active, limiter.waiting) == (0, 0)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_the_limit_must_be_a_positive_integer(limit: object) -> None:
    with pytest.raises(ValueError):
        QueuedLimiter(limit)  # type: ignore[arg-type]


def test_a_release_below_zero_is_refused() -> None:
    limiter = QueuedLimiter(1)
    with pytest.raises(RuntimeError):
        limiter._release()
