"""Credit dominance switch (neurostack-credit-system, Group 1b).

Every credit surface is inert unless this switch is EFFECTIVE, which takes
both of (Decision 20):

* ``credit_hot_path_enabled`` on in the server configuration; and
* ``alembic_version`` holding exactly one row whose revision belongs to this
  image's own migration chain (the ``alembic/versions`` shipped next to
  ``src``, parsed without importing it) at or after
  ``CREDIT_SCHEMA_MIN_REVISION``, the highest credit revision of the wish.

The schema is read at start-up (``src/api/main.py`` lifespan) and again every
``credit_switch_refresh_seconds`` by a background task, so request code never
queries the database: it reads the cached verdict synchronously.  The read
fails closed: a missing table, zero or several rows, a revision outside the
image chain, an unreadable chain, a timeout or any other error is OFF, with a
log line.  No request, header, frontend or AppConfig value reaches it.

A request captures the value ONCE, at entry (``capture_credit_switch``, called
by ``TenantIdentityMiddleware``), and uses that captured value until it ends;
ECS tasks may disagree for up to one refresh interval, each request stays
internally consistent.  On is observational only (Decision 24): anything
that changes visible behaviour waits for ``credit_enforcement_at``.

Reusable guards for the credit surfaces of Groups 2-7:

* ``require_credit_switch`` (FastAPI dependency; the app's handler for
  ``CreditUnavailableError``, registered in ``create_app``, answers the stable
  ``503 {"code": "credit_unavailable"}``) and ``credit_router()`` (router whose
  routes give that answer before any body parsing, dependency or database
  work -- the dependency alone runs only after FastAPI parsed the body, so a
  malformed body still gets its 422 first);
* ``run_credit_worker_tick`` (a worker tick that claims nothing when off);
* ``load_credit_panel`` (the UI degrades: a panel is ``None`` when off).

This module touches no ``credit_*`` table and never reads ``tests/``.
"""

from __future__ import annotations

import ast
import asyncio
import functools
from collections.abc import Awaitable, Callable, Coroutine, MutableMapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from starlette.requests import HTTPConnection

from src.config.logging import get_logger
from src.config.settings import Settings, settings
from src.observability.metric_dimensions import ContractGauge

logger = get_logger(__name__)

_T = TypeVar("_T")

# Highest credit migration the wish defines; every group that adds a credit
# migration raises it (with the head pins of Group 1a's convention).
CREDIT_SCHEMA_MIN_REVISION: Final = "039"
# Key of the per-request captured value in the ASGI scope state
# (``request.state.credit_switch_effective``).
CREDIT_SWITCH_STATE_KEY: Final = "credit_switch_effective"
CREDIT_UNAVAILABLE_CODE: Final = "credit_unavailable"
# Whole budget of one schema read: pool checkout, statement and rollback.
VERSION_READ_TIMEOUT_SECONDS: Final = 5.0
# The image layout: /app/src/services/credit_switch.py -> /app/alembic/versions.
IMAGE_VERSIONS_DIR: Final = Path(__file__).resolve().parents[2] / "alembic" / "versions"
_VERSION_SQL: Final = "SELECT version_num FROM alembic_version"

CreditSwitchReason = Literal[
    "not_read",
    "stopped",
    "config_disabled",
    "schema_ready",
    "schema_below_minimum",
    "revision_outside_image_chain",
    "version_row_count",
    "image_chain_unavailable",
    "read_error",
]


class ImageMigrationChainError(ValueError):
    """The image's ``alembic/versions`` is not one readable linear chain."""


@dataclass(frozen=True)
class CreditSwitchReading:
    """One verdict of the switch and why (the reason never leaves the logs)."""

    effective: bool
    reason: CreditSwitchReason
    revision: str | None = None


_NOT_READ: Final = CreditSwitchReading(False, "not_read")
_STOPPED: Final = CreditSwitchReading(False, "stopped")
_CONFIG_DISABLED: Final = CreditSwitchReading(False, "config_disabled")


# ---------------------------------------------------------------------------
# The image's own migration chain, parsed without importing the scripts.
# ---------------------------------------------------------------------------


def _module_string(path: Path, tree: ast.Module, name: str) -> str | None:
    """The literal module-level ``name`` (``revision``/``down_revision``)."""

    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        if not isinstance(value, ast.Constant) or not (
            value.value is None or isinstance(value.value, str)
        ):
            raise ImageMigrationChainError(
                f"{path.name}: `{name}` is not a string literal"
            )
        return value.value
    raise ImageMigrationChainError(f"{path.name}: module-level `{name}` not found")


@functools.lru_cache(maxsize=4)
def load_image_migration_chain(
    versions_dir: Path = IMAGE_VERSIONS_DIR,
) -> tuple[str, ...]:
    """Revisions of the image's migration chain, root first.

    Raises ``ImageMigrationChainError`` unless the scripts form exactly one
    linear chain (one root, no duplicate, branch, merge or dangling parent).
    A successful parse is cached: the chain is part of the image.
    """

    parents: dict[str, str | None] = {}
    for path in sorted(versions_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError) as error:
            raise ImageMigrationChainError(f"{path.name}: unreadable") from error
        revision = _module_string(path, tree, "revision")
        if not revision:
            raise ImageMigrationChainError(f"{path.name}: empty revision")
        if revision in parents:
            raise ImageMigrationChainError(f"duplicate revision {revision}")
        parents[revision] = _module_string(path, tree, "down_revision")
    if not parents:
        raise ImageMigrationChainError(f"no migration script in {versions_dir}")

    roots = [revision for revision, parent in parents.items() if parent is None]
    children: dict[str, str] = {}
    for revision, parent in parents.items():
        if parent is None:
            continue
        if parent not in parents:
            raise ImageMigrationChainError(f"{revision} revises unknown {parent}")
        if parent in children:
            raise ImageMigrationChainError(f"more than one migration revises {parent}")
        children[parent] = revision
    if len(roots) != 1:
        raise ImageMigrationChainError(f"expected one root, found {sorted(roots)}")

    chain = [roots[0]]
    while chain[-1] in children:
        chain.append(children[chain[-1]])
    if len(chain) != len(parents):
        raise ImageMigrationChainError("migration scripts outside the linear chain")
    return tuple(chain)


def evaluate_schema_revision(
    version_rows: tuple[str, ...],
    chain: tuple[str, ...],
    minimum: str = CREDIT_SCHEMA_MIN_REVISION,
) -> CreditSwitchReading:
    """Pure verdict for ``alembic_version`` rows against the image chain."""

    if len(version_rows) != 1:
        return CreditSwitchReading(False, "version_row_count")
    revision = version_rows[0]
    if revision not in chain or minimum not in chain:
        return CreditSwitchReading(False, "revision_outside_image_chain", revision)
    if chain.index(revision) < chain.index(minimum):
        return CreditSwitchReading(False, "schema_below_minimum", revision)
    return CreditSwitchReading(True, "schema_ready", revision)


# ---------------------------------------------------------------------------
# The process verdict: read in the background, consulted synchronously.
# ---------------------------------------------------------------------------


class CreditSwitch:
    """This process's cached verdict; request code only ever reads it."""

    def __init__(self) -> None:
        self._reading: CreditSwitchReading = _NOT_READ

    def effective(self) -> bool:
        return self._reading.effective

    def reading(self) -> CreditSwitchReading:
        return self._reading

    def publish(self, reading: CreditSwitchReading) -> None:
        previous = self._reading
        self._reading = reading
        if previous.effective != reading.effective or previous.reason != reading.reason:
            logger.info(
                "credit switch verdict changed",
                effective=reading.effective,
                reason=reading.reason,
                revision=reading.revision,
                previous_reason=previous.reason,
            )


_SWITCH: Final = CreditSwitch()


def credit_switch_effective() -> bool:
    """Synchronous, never raises, never touches the database; error => off."""

    try:
        return _SWITCH.effective() is True
    except Exception as error:
        logger.error(
            "credit switch unreadable; treating it as off",
            error_type=type(error).__name__,
        )
        return False


def credit_switch_reading() -> CreditSwitchReading:
    """The last published verdict (for logs, health and tests)."""

    return _SWITCH.reading()


# Label-free on purpose: no builder, tenant or revision enters Prometheus.  The
# value is computed at scrape time from the same verdict request code reads.
CREDIT_SWITCH_EFFECTIVE = ContractGauge(
    "neurostack_credit_switch_effective",
    "1 when this process's credit dominance switch is effective, else 0",
)
CREDIT_SWITCH_EFFECTIVE.set_function(lambda: 1.0 if credit_switch_effective() else 0.0)


async def _invalidate(connection: AsyncConnection) -> None:
    with suppress(Exception):
        await connection.invalidate()


async def _read_version_rows(
    session_factory: Callable[[], AsyncSession],
) -> tuple[str, ...]:
    """``alembic_version`` rows; an interrupted read never returns to the pool."""

    session = session_factory()
    try:
        connection = await session.connection()
        try:
            result = await connection.execute(text(_VERSION_SQL))
            return tuple(str(row[0]) for row in result.all())
        except asyncio.CancelledError:
            await _invalidate(connection)
            raise
    finally:
        await session.close()


async def _schema_reading(
    session_factory: Callable[[], AsyncSession] | None,
    versions_dir: Path,
    timeout_seconds: float,
) -> CreditSwitchReading:
    try:
        chain = load_image_migration_chain(versions_dir)
    except Exception as error:
        logger.error(
            "credit switch off: image migration chain unavailable",
            error_type=type(error).__name__,
        )
        return CreditSwitchReading(False, "image_chain_unavailable")
    try:
        if session_factory is None:
            from src.repositories.database import get_db_session_factory

            session_factory = get_db_session_factory()
        async with asyncio.timeout(timeout_seconds):
            rows = await _read_version_rows(session_factory)
    except Exception as error:
        logger.warning(
            "credit switch off: alembic_version unreadable",
            error_type=type(error).__name__,
        )
        return CreditSwitchReading(False, "read_error")
    reading = evaluate_schema_revision(rows, chain)
    # A schema still below the minimum is expected before the migration: the
    # transition log of ``publish`` says it once.  These are faults, every time.
    if reading.reason in ("version_row_count", "revision_outside_image_chain"):
        logger.warning(
            "credit switch off: alembic_version is not one revision of this image",
            reason=reading.reason,
            revision=reading.revision,
            rows=len(rows),
            minimum=CREDIT_SCHEMA_MIN_REVISION,
        )
    return reading


async def refresh_credit_switch(
    *,
    enabled: bool | None = None,
    session_factory: Callable[[], AsyncSession] | None = None,
    versions_dir: Path = IMAGE_VERSIONS_DIR,
    timeout_seconds: float = VERSION_READ_TIMEOUT_SECONDS,
) -> CreditSwitchReading:
    """Re-read the verdict and publish it; never raises (except cancellation).

    With the configuration off it answers off without any database work.
    """

    config_enabled = settings.credit_hot_path_enabled if enabled is None else enabled
    if config_enabled is not True:
        reading = _CONFIG_DISABLED
    else:
        reading = await _schema_reading(session_factory, versions_dir, timeout_seconds)
    _SWITCH.publish(reading)
    return reading


async def run_credit_switch_refresher(
    interval_seconds: float,
    *,
    refresh: Callable[[], Awaitable[CreditSwitchReading]] = refresh_credit_switch,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Lifespan task: re-read every ``interval_seconds`` until cancelled."""

    while True:
        await sleep(interval_seconds)
        try:
            await refresh()
        except Exception as error:
            logger.error(
                "credit switch refresh failed; treating it as off",
                error_type=type(error).__name__,
            )
            _SWITCH.publish(CreditSwitchReading(False, "read_error"))


def start_credit_switch_refresher(
    source: Settings | None = None,
) -> asyncio.Task[None] | None:
    """Start the refresher only when the configuration is on (no task if off)."""

    configured = settings if source is None else source
    if configured.credit_hot_path_enabled is not True:
        return None
    return asyncio.create_task(
        run_credit_switch_refresher(float(configured.credit_switch_refresh_seconds)),
        name="credit-switch-refresher",
    )


async def stop_credit_switch(task: asyncio.Task[None] | None) -> None:
    """Cancel the refresher and turn the verdict off before the pool closes.

    Never raises an error of the refresher, so the rest of the shutdown runs.
    """

    try:
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as error:
                logger.error(
                    "credit switch refresher had stopped with an error",
                    error_type=type(error).__name__,
                )
    finally:
        _SWITCH.publish(_STOPPED)


# ---------------------------------------------------------------------------
# Per-request capture and the reusable guards.
# ---------------------------------------------------------------------------


def capture_credit_switch(scope: MutableMapping[str, Any]) -> bool:
    """Capture the verdict once per request; later calls return that value.

    ``scope`` is the request's ASGI scope; the value lands on
    ``request.state.credit_switch_effective``.  Never raises.
    """

    try:
        state = scope.setdefault("state", {})
        captured = state.get(CREDIT_SWITCH_STATE_KEY)
        if isinstance(captured, bool):
            return captured
        value = credit_switch_effective()
        state[CREDIT_SWITCH_STATE_KEY] = value
        return value
    except Exception as error:
        logger.error(
            "credit switch capture failed; treating it as off",
            error_type=type(error).__name__,
        )
        return False


def request_credit_switch(request: HTTPConnection) -> bool:
    """The value this request captured at entry (captured now if it was not)."""

    return capture_credit_switch(request.scope)


def credit_unavailable_response() -> JSONResponse:
    """The stable answer of every credit surface while the switch is off."""

    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"code": CREDIT_UNAVAILABLE_CODE},
    )


class CreditUnavailableError(HTTPException):
    """Raised by ``require_credit_switch``.

    ``CreditSwitchRoute`` and the application handler registered by
    ``create_app`` (``credit_unavailable_exception_handler``) answer it with
    ``credit_unavailable_response()``.  An app without that handler still
    answers 503 (never a 500), with FastAPI's ``{"detail": ...}`` body.
    """

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=CREDIT_UNAVAILABLE_CODE,
        )


async def credit_unavailable_exception_handler(
    _request: Request, _error: Exception
) -> JSONResponse:
    """Application handler for ``CreditUnavailableError`` (``create_app``)."""

    return credit_unavailable_response()


async def require_credit_switch(request: Request) -> None:
    """FastAPI dependency of every credit surface: off => 503 credit_unavailable.

    The exact body comes from the application handler.  FastAPI resolves
    dependencies only after it has read and parsed the request body, so with
    this dependency alone a malformed body is still answered 422 first and
    the body is still read: use ``credit_router()`` (or ``add_api_route(...,
    route_class_override=CreditSwitchRoute)``) for the guarantee "503 before
    the body is parsed, before any dependency or database work".
    """

    if not request_credit_switch(request):
        raise CreditUnavailableError()


class CreditSwitchRoute(APIRoute):
    """Answer ``503 {"code": "credit_unavailable"}`` while the switch is off.

    The check runs before FastAPI reads or parses the body and before any
    dependency (so no database session is opened); a refusal raised later by
    ``require_credit_switch`` gets the same body.
    """

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def credit_route_handler(request: Request) -> Response:
            if not request_credit_switch(request):
                return credit_unavailable_response()
            try:
                return await original(request)
            except CreditUnavailableError:
                return credit_unavailable_response()

        return credit_route_handler


def credit_router(**options: Any) -> APIRouter:
    """``APIRouter`` for a credit surface: guarded route class and dependency."""

    dependencies = [Depends(require_credit_switch), *options.pop("dependencies", ())]
    return APIRouter(
        route_class=CreditSwitchRoute, dependencies=dependencies, **options
    )


async def run_credit_worker_tick(
    tick: Callable[[], Awaitable[_T]],
) -> _T | None:
    """Worker guard: one tick claims nothing (``tick`` is not called) when off.

    Workers read the switch once per tick, like a request at entry.
    """

    if not credit_switch_effective():
        return None
    return await tick()


async def load_credit_panel(
    request: HTTPConnection,
    load: Callable[[], Awaitable[_T]],
) -> _T | None:
    """UI degradation: ``None`` (panel hidden) without calling ``load`` when off."""

    if not request_credit_switch(request):
        return None
    return await load()


__all__ = [
    "CREDIT_SCHEMA_MIN_REVISION",
    "CREDIT_SWITCH_EFFECTIVE",
    "CREDIT_SWITCH_STATE_KEY",
    "CREDIT_UNAVAILABLE_CODE",
    "IMAGE_VERSIONS_DIR",
    "VERSION_READ_TIMEOUT_SECONDS",
    "CreditSwitch",
    "CreditSwitchReading",
    "CreditSwitchRoute",
    "CreditUnavailableError",
    "ImageMigrationChainError",
    "capture_credit_switch",
    "credit_router",
    "credit_switch_effective",
    "credit_switch_reading",
    "credit_unavailable_exception_handler",
    "credit_unavailable_response",
    "evaluate_schema_revision",
    "load_credit_panel",
    "load_image_migration_chain",
    "refresh_credit_switch",
    "request_credit_switch",
    "require_credit_switch",
    "run_credit_switch_refresher",
    "run_credit_worker_tick",
    "start_credit_switch_refresher",
    "stop_credit_switch",
]
