"""Pre-parser request caps and media concurrency budgets.

The Central de Mídias routes fail fast (:class:`ImmediateLimiter`); only the
agent's media sends wait, bounded and in arrival order, for their permit
(:class:`QueuedLimiter`).
"""

from __future__ import annotations

import asyncio
import math
from collections import deque
from threading import Lock, RLock
from typing import Final

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.api.routing_paths import decision_path
from src.services.media_assets import MEDIA_FILE_MAX_BYTES

# Headroom for the boundary, the part headers and the four text fields; the
# cap exists to stop a body no category could accept before it is parsed.
MEDIA_MULTIPART_OVERHEAD_BYTES: Final = 1_000_000
MEDIA_MULTIPART_MAX_BYTES: Final = MEDIA_FILE_MAX_BYTES + MEDIA_MULTIPART_OVERHEAD_BYTES
# envio-midia-agente Decision 9: ONE agent media send at a time per process
# (``agent_sends``).  A 20 MB PDF send peaks below 80 MB, measured with the
# real senders; four workers share a 2048 MB task that already peaked near
# 69 %, so one permit each tops out around 85 % and two would leave no room.
# A constant, not a setting: raise it only after a new memory measurement.
# Its callers wait for it, bounded and in arrival order (a
# :class:`QueuedLimiter`, ``reply_media.AGENT_MEDIA_PERMIT_WAIT_SECONDS``):
# a waiter holds no file bytes, so the wait costs no memory.
AGENT_MEDIA_SENDS_PER_PROCESS: Final = 1


class MediaRequestTooLarge(Exception):
    """Raised before a multipart parser can receive an oversized body."""


class MediaConcurrencyExceeded(Exception):
    """Raised before receive when no media permit is immediately available."""


class ImmediatePermit:
    """An idempotently releasable permit from an :class:`ImmediateLimiter`
    (or a :class:`QueuedLimiter`)."""

    def __init__(self, limiter: ImmediateLimiter | QueuedLimiter) -> None:
        self._limiter = limiter
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._limiter._release()


class ImmediateLimiter:
    """Small process-local limiter which never queues callers.

    The lock makes the counter safe even when ASGI applications are driven by
    more than one event-loop thread. A rejected caller never touches receive.
    """

    def __init__(self, limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("media concurrency limit must be a positive integer")
        self.limit = limit
        self._active = 0
        self._lock = Lock()

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def try_acquire(self) -> ImmediatePermit | None:
        with self._lock:
            if self._active >= self.limit:
                return None
            self._active += 1
        return ImmediatePermit(self)

    def _release(self) -> None:
        with self._lock:
            if self._active < 1:
                raise RuntimeError("media concurrency permit released below zero")
            self._active -= 1


class _Waiter:
    """One caller queued in a :class:`QueuedLimiter`.  ``granted`` is set
    under the limiter's lock when a released permit is handed to it; the
    future only wakes it, on its own loop."""

    __slots__ = ("granted", "loop", "woken")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.woken: asyncio.Future[None] = loop.create_future()
        self.granted = False


def _wake(woken: asyncio.Future[None]) -> None:
    if not woken.done():  # a cancelled waiter's future is already done
        woken.set_result(None)


def _woken(waiter: _Waiter) -> bool:
    """Wake a waiter handed a permit, on its own loop; ``False`` when that
    loop is closed (the waiter can never run again)."""

    try:
        running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is waiter.loop:
        _wake(waiter.woken)
        return True
    try:
        waiter.loop.call_soon_threadsafe(_wake, waiter.woken)
    except RuntimeError:
        return False
    return True


class QueuedLimiter:
    """Process-local limiter whose callers wait for a permit, bounded and in
    arrival order (``MediaConcurrencyLimiters.agent_sends`` only).

    :meth:`acquire` takes a free permit at once when nobody is waiting, else
    joins the queue for at most ``timeout`` seconds.  A released permit goes
    to the oldest waiter (it never returns to the pool while one waits, so a
    newcomer never overtakes the queue) and back to the pool when none does.
    The permit's ownership moves under the lock, the waiter's future only
    wakes it: a waiter that times out or is cancelled leaves the queue, and
    if the permit had already been handed to it, a timed-out one keeps it
    (the slot did open) while a cancelled one passes it on -- a permit is
    never stranded with a caller that is leaving, nor skipped past the
    others.  :meth:`try_acquire` never queues and never overtakes a waiter.

    The lock keeps the count and the queue whole across event-loop threads;
    each waiter is woken on its own loop (from another thread through
    ``call_soon_threadsafe``; a closed loop's waiter can never run again, so
    its permit passes on).

    The lock is RE-ENTRANT on purpose.  A stranded waiter's coroutine (its
    loop closed) is closed by the garbage collector, and its
    ``GeneratorExit`` path takes the lock (:meth:`_leave`, maybe
    :meth:`_release`).  CPython 3.12 runs a due collection at the next
    eval-breaker check -- after a C call such as ``len()`` or
    ``deque.append`` -- so it can run inside a critical section of the same
    thread, where a plain ``Lock`` deadlocks.  Re-entering is safe: every
    call inside a critical section comes after the state it leaves is
    consistent (a waiter appended, removed or popped and not yet granted),
    and a finalizer only leaves the queue or passes its own permit on.
    No object is built inside the critical sections either (the waiter is
    built before the lock, the permit after it).
    """

    def __init__(self, limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("media concurrency limit must be a positive integer")
        self.limit = limit
        self._active = 0
        self._waiters: deque[_Waiter] = deque()
        self._lock = RLock()  # re-entrant: see the class docstring

    @property
    def active(self) -> int:
        """Permits held, including one handed to a waiter not yet resumed."""

        with self._lock:
            return self._active

    @property
    def waiting(self) -> int:
        with self._lock:
            return len(self._waiters)

    def try_acquire(self) -> ImmediatePermit | None:
        with self._lock:
            # ``or self._waiters`` decides nothing today: a non-empty queue
            # implies ``_active == limit``, since a handover keeps the count.
            # It stays so that "never overtake a waiter" is stated where it
            # is enforced, not left to that invariant alone.
            if self._active >= self.limit or self._waiters:
                return None
            self._active += 1
        return ImmediatePermit(self)

    async def acquire(self, timeout: float) -> ImmediatePermit | None:
        """A permit, waited for at most ``timeout`` seconds in arrival order;
        ``None`` when none was handed over in time.  A cancellation leaves the
        queue (passing on a permit handed over meanwhile) and propagates."""

        if timeout < 0 or not math.isfinite(timeout):
            raise ValueError("media permit wait must be finite and non-negative")
        # Built before the lock (and the permit after it): no object is built
        # inside the critical section (LOW-1 of the review).
        waiter = _Waiter(asyncio.get_running_loop())
        with self._lock:
            free = self._active < self.limit and not self._waiters
            if free:
                self._active += 1
            else:
                self._waiters.append(waiter)
        if free:
            return ImmediatePermit(self)
        try:
            async with asyncio.timeout(timeout):
                await waiter.woken
        except TimeoutError:
            if self._leave(waiter):
                return ImmediatePermit(self)  # handed over at the deadline
            return None
        except BaseException:
            if self._leave(waiter):
                self._release()  # handed over, but it is leaving: pass it on
            raise
        return ImmediatePermit(self)

    def _leave(self, waiter: _Waiter) -> bool:
        """Take a timed-out or cancelled waiter off the queue; ``True`` when
        a permit had already been handed to it, which it then holds."""

        with self._lock:
            if waiter.granted:
                return True
            if waiter in self._waiters:  # else taken back: its loop closed
                self._waiters.remove(waiter)
            return False

    def _release(self) -> None:
        while True:  # once per waiter whose loop turned out to be closed
            waiter: _Waiter | None = None
            with self._lock:
                below_zero = self._active < 1
                if below_zero:
                    pass
                elif not self._waiters:
                    self._active -= 1
                else:
                    waiter = self._waiters.popleft()
                    waiter.granted = True  # the permit moves on: ``_active`` stays
            if below_zero:
                raise RuntimeError("media concurrency permit released below zero")
            if waiter is None or _woken(waiter):
                return
            # Its loop is closed: that waiter never runs again.  The permit
            # is taken back from it (so its coroutine, closed when collected,
            # releases nothing) and passes on, or back to the pool.
            with self._lock:
                waiter.granted = False


class MediaConcurrencyLimiters:
    """Shared media limiters attached to one application instance.

    Three independent pools: ``uploads`` and ``downloads`` for the Central de
    Mídias routes, which never queue (:class:`ImmediateLimiter`), and
    ``agent_sends`` for the agent's media sends
    (:func:`src.services.reply_media.deliver_reply_media`), whose callers wait
    for it, bounded and in arrival order (:class:`QueuedLimiter`), so an agent
    send never takes a permit a panel download needs, nor the reverse.
    """

    def __init__(
        self,
        *,
        uploads: int,
        downloads: int,
        agent_sends: int = AGENT_MEDIA_SENDS_PER_PROCESS,
    ) -> None:
        self.uploads = ImmediateLimiter(uploads)
        self.downloads = ImmediateLimiter(downloads)
        self.agent_sends = QueuedLimiter(agent_sends)


def _error_response(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": {"code": code}})


def _declared_content_length(scope: Scope) -> int | None:
    values = [
        value
        for name, value in scope.get("headers", [])
        if name.lower() == b"content-length"
    ]
    if not values:
        return None
    if len(values) != 1:
        raise ValueError("ambiguous content-length")
    raw = values[0]
    if not raw or not raw.isdigit():
        raise ValueError("invalid content-length")
    return int(raw)


class MediaUploadLimitsMiddleware:
    """Protect the media upload path before multipart parsing starts."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        limiters: MediaConcurrencyLimiters,
        upload_path: str,
        request_max_bytes: int = MEDIA_MULTIPART_MAX_BYTES,
    ) -> None:
        self.app = app
        self.limiters = limiters
        self.upload_path = upload_path
        self.request_max_bytes = request_max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            # the router's path: "%0a" still routes to the upload endpoint
            or decision_path(scope) != self.upload_path
        ):
            await self.app(scope, receive, send)
            return

        try:
            declared_length = _declared_content_length(scope)
        except ValueError:
            await _error_response(400, "media_content_length_invalid")(
                scope, receive, send
            )
            return
        if declared_length is not None and declared_length > self.request_max_bytes:
            await _error_response(413, "media_request_size_exceeded")(
                scope, receive, send
            )
            return

        received = 0
        permit: ImmediatePermit | None = None

        async def capped_receive() -> Message:
            nonlocal permit, received
            if permit is None:
                permit = self.limiters.uploads.try_acquire()
                if permit is None:
                    raise MediaConcurrencyExceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.request_max_bytes:
                    raise MediaRequestTooLarge
            return message

        try:
            await self.app(scope, capped_receive, send)
        except MediaConcurrencyExceeded:
            await _error_response(503, "media_concurrency_exceeded")(
                scope, receive, send
            )
        except MediaRequestTooLarge:
            await _error_response(413, "media_request_size_exceeded")(
                scope, receive, send
            )
        finally:
            if permit is not None:
                permit.release()
