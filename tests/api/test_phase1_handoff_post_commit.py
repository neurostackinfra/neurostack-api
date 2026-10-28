"""The phase 1 transaction owners announce a handoff only after their commit.

A synchronous handoff joins the locked phase 1 transaction and queues its
creation webhook on that session (#145). Both owners of that transaction, the
``/v1/events`` route and the channel adapters' ``_run_pipeline_transactionally``,
must commit, then run ``dispatch_committed_handoff_webhooks``, then start the
response I/O; a rollback never reaches the dispatch. The announcement is best
effort: its failure is logged and counted, and the reply is still produced.

The real composition is proved on PostgreSQL in
``tests/integration/test_handoff_phase1_lock_postgres.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import TenantContext, get_tenant_context
from src.api.middleware.tenant_identity import TenantIdentityMiddleware
from src.api.routes import events
from src.api.routes import webhook as webhook_module
from src.api.schemas.events import CanonicalEventRequest, EventPayload, EventType
from src.interaction_context import unknown_interaction_context
from src.repositories import get_db_session_dependency
from src.services import handoff_service as handoff_service_module
from src.services.handoff_service import HANDOFF_OPERATIONS, HandoffPostCommitError


class _RecordingDb:
    """The request's database session, reduced to its transaction edges."""

    def __init__(self, order: list[str], *, fail_commit: bool = False) -> None:
        self.order = order
        self.fail_commit = fail_commit
        self.info: dict[str, Any] = {}

    async def commit(self) -> None:
        self.order.append("commit")
        if self.fail_commit:
            raise RuntimeError("commit failed")

    async def rollback(self) -> None:
        self.order.append("rollback")

    async def close(self) -> None:
        return None


def _service(order: list[str], *, fail_pipeline: bool = False) -> SimpleNamespace:
    async def run_pipeline(*_args: Any, **_kwargs: Any) -> Any:
        order.append("run_pipeline")
        if fail_pipeline:
            raise RuntimeError("pipeline failed after the handoff write")
        return SimpleNamespace(), {}, []

    async def finish_response(*_args: Any, **_kwargs: Any) -> Any:
        order.append("finish_response")
        return JSONResponse(status_code=200, content={"ok": True})

    return SimpleNamespace(
        run_pipeline=AsyncMock(side_effect=run_pipeline),
        finish_response=AsyncMock(side_effect=finish_response),
    )


def _dispatch_spy(
    order: list[str], db: object, *, error: Exception | None = None
) -> AsyncMock:
    async def dispatch(session: object) -> None:
        assert session is db
        order.append("dispatch")
        if error is not None:
            raise error

    return AsyncMock(side_effect=dispatch)


def _post_commit_dispatch_errors() -> float:
    value: float = HANDOFF_OPERATIONS.labels(
        operation="post_commit_dispatch", status="error"
    )._value.get()
    return value


def _spy_logger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    log = MagicMock()
    monkeypatch.setattr(handoff_service_module, "logger", log)
    return log


def _assert_failure_logged(log: MagicMock, *, owner: str, error: Exception) -> None:
    log.error.assert_called_once()
    args, kwargs = log.error.call_args
    assert args == ("handoff_post_commit_dispatch_failed",)
    assert kwargs["owner"] == owner
    assert kwargs["error_type"] == type(error).__name__
    assert kwargs["error"] == str(error)
    assert kwargs["exc_info"] is True


# ============================================
# /v1/events
# ============================================


async def _post_event(
    monkeypatch: pytest.MonkeyPatch,
    order: list[str],
    db: _RecordingDb,
    service: SimpleNamespace,
    dispatch: AsyncMock,
) -> Any:
    tenant_id = uuid4()
    tenant = TenantContext(tenant_id=tenant_id, api_key="phase1-handoff-key")

    class AuthenticatedTenantMiddleware:
        def __init__(self, application: Any) -> None:
            self.application = application

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            scope.setdefault("state", {})["tenant"] = tenant
            scope["state"]["request_id"] = "phase1-handoff-request"
            await self.application(scope, receive, send)

    async def database() -> Any:
        yield db

    app = FastAPI()
    app.include_router(events.router, prefix="/v1/events")
    app.dependency_overrides[get_tenant_context] = lambda: tenant
    app.dependency_overrides[get_db_session_dependency] = database
    app.add_middleware(TenantIdentityMiddleware)
    app.add_middleware(AuthenticatedTenantMiddleware)
    monkeypatch.setattr(events, "get_event_service", lambda: service)
    monkeypatch.setattr(events, "dispatch_committed_handoff_webhooks", dispatch)

    async with AsyncClient(
        transport=ASGITransport(app=cast(Any, app)), base_url="http://testserver"
    ) as client:
        return await client.post(
            "/v1/events",
            headers={"X-API-Key": "phase1-handoff-key"},
            json={
                "tenant_id": str(tenant_id),
                "session_id": str(uuid4()),
                "event_type": "message.in",
                "payload": {"content": "Quero falar com uma pessoa"},
            },
        )


@pytest.mark.asyncio
async def test_events_route_commits_then_announces_then_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    db = _RecordingDb(order)
    dispatch = _dispatch_spy(order, db)

    response = await _post_event(monkeypatch, order, db, _service(order), dispatch)

    assert response.status_code == 200
    assert order == ["run_pipeline", "commit", "dispatch", "finish_response"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail_pipeline", "fail_commit", "expected"),
    [
        (True, False, ["run_pipeline", "rollback"]),
        (False, True, ["run_pipeline", "commit", "rollback"]),
    ],
    ids=["phase1-failure", "commit-failure"],
)
async def test_events_route_rollback_never_announces(
    monkeypatch: pytest.MonkeyPatch,
    fail_pipeline: bool,
    fail_commit: bool,
    expected: list[str],
) -> None:
    order: list[str] = []
    db = _RecordingDb(order, fail_commit=fail_commit)
    dispatch = _dispatch_spy(order, db)

    response = await _post_event(
        monkeypatch,
        order,
        db,
        _service(order, fail_pipeline=fail_pipeline),
        dispatch,
    )

    assert response.status_code == 500
    assert order == expected
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [HandoffPostCommitError("bookkeeping failed"), RuntimeError("dispatch crashed")],
    ids=["post-commit-error", "unexpected-error"],
)
async def test_events_route_announcement_failure_still_answers(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    order: list[str] = []
    db = _RecordingDb(order)
    dispatch = _dispatch_spy(order, db, error=error)
    before = _post_commit_dispatch_errors()
    log = _spy_logger(monkeypatch)

    response = await _post_event(monkeypatch, order, db, _service(order), dispatch)

    # Phase 1 is durable: no rollback, the reply is still produced, the
    # failure is logged and counted, and the announcement is not retried.
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert order == ["run_pipeline", "commit", "dispatch", "finish_response"]
    dispatch.assert_awaited_once()
    assert _post_commit_dispatch_errors() == before + 1
    _assert_failure_logged(log, owner="events", error=error)


# ============================================
# Channel adapters: _run_pipeline_transactionally
# ============================================


def _adapter_request() -> tuple[CanonicalEventRequest, Any]:
    request = CanonicalEventRequest(
        tenant_id=uuid4(),
        session_id=uuid4(),
        event_type=EventType.MESSAGE_IN,
        payload=EventPayload(content="Quero falar com uma pessoa", channel="whatsapp"),
    )
    context = unknown_interaction_context(
        effective_tenant_id=request.tenant_id,
        session_id=request.session_id,
        event_type=request.event_type,
    )
    return request, context


@pytest.mark.asyncio
async def test_adapter_commits_then_announces_then_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    db = _RecordingDb(order)
    dispatch = _dispatch_spy(order, db)
    monkeypatch.setattr(webhook_module, "dispatch_committed_handoff_webhooks", dispatch)
    request, context = _adapter_request()

    await webhook_module._run_pipeline_transactionally(
        service=_service(order),
        event_request=request,
        db=cast(AsyncSession, db),
        context=context,
    )

    assert order == ["run_pipeline", "commit", "dispatch", "finish_response"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail_pipeline", "fail_commit", "expected"),
    [
        (True, False, ["run_pipeline", "rollback"]),
        (False, True, ["run_pipeline", "commit", "rollback"]),
    ],
    ids=["phase1-failure", "commit-failure"],
)
async def test_adapter_rollback_never_announces(
    monkeypatch: pytest.MonkeyPatch,
    fail_pipeline: bool,
    fail_commit: bool,
    expected: list[str],
) -> None:
    order: list[str] = []
    db = _RecordingDb(order, fail_commit=fail_commit)
    dispatch = _dispatch_spy(order, db)
    monkeypatch.setattr(webhook_module, "dispatch_committed_handoff_webhooks", dispatch)
    request, context = _adapter_request()

    with pytest.raises(RuntimeError):
        await webhook_module._run_pipeline_transactionally(
            service=_service(order, fail_pipeline=fail_pipeline),
            event_request=request,
            db=cast(AsyncSession, db),
            context=context,
        )

    assert order == expected
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [HandoffPostCommitError("bookkeeping failed"), RuntimeError("dispatch crashed")],
    ids=["post-commit-error", "unexpected-error"],
)
async def test_adapter_announcement_failure_still_answers(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    order: list[str] = []
    db = _RecordingDb(order)
    dispatch = _dispatch_spy(order, db, error=error)
    monkeypatch.setattr(webhook_module, "dispatch_committed_handoff_webhooks", dispatch)
    request, context = _adapter_request()
    before = _post_commit_dispatch_errors()
    log = _spy_logger(monkeypatch)

    response = await webhook_module._run_pipeline_transactionally(
        service=_service(order),
        event_request=request,
        db=cast(AsyncSession, db),
        context=context,
    )

    assert cast(Any, response).status_code == 200
    assert order == ["run_pipeline", "commit", "dispatch", "finish_response"]
    dispatch.assert_awaited_once()
    assert _post_commit_dispatch_errors() == before + 1
    _assert_failure_logged(log, owner="adapter", error=error)
