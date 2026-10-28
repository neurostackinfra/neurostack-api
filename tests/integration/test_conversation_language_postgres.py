"""PostgreSQL row-lock proof for per-turn conversation language state."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.api.middleware.auth import TenantContext
from src.api.routes import webhook as webhook_module
from src.api.schemas.events import EventPayload, EventRequest, EventType
from src.config.settings import settings
from src.core.pipeline import PipelineInput, PipelineResult
from src.models.session import CognitiveStateData, Session
from src.models.tenant import Tenant
from src.repositories.session import SessionRepository
from src.services.conversation_language import ENGLISH_PROMPT
from src.services.event_service import EventService

_ROOT = Path(__file__).resolve().parents[2]
_CHILD_PATTERN = re.compile(r"^neurostack_conversation_language_[0-9a-f]{12}$")
_LOCAL_SOCKET_DIRECTORIES = {"/run/postgresql", "/var/run/postgresql"}
SessionMaker = async_sessionmaker[AsyncSession]


def _database_identity(url: URL) -> tuple[str, int, str | None]:
    query_host = url.query.get("host")
    query_port = url.query.get("port")
    raw_port = query_port or url.port or 5432
    if isinstance(raw_port, tuple):
        raw_port = raw_port[0]
    return (
        str(query_host or url.host or "").lower(),
        int(raw_port),
        url.database,
    )


def _validated_admin_url(raw_url: str) -> URL:
    url = make_url(raw_url)
    if not url.drivername.startswith("postgresql"):
        raise ValueError("smoke URL must use PostgreSQL")
    if url.database != "postgres":
        raise ValueError("smoke URL must use the postgres administrative database")
    if url.query:
        if url.host is not None or set(url.query) - {"host", "port"}:
            raise ValueError("smoke URL has unsafe connection parameters")
        if str(url.query.get("host", "")) not in _LOCAL_SOCKET_DIRECTORIES:
            raise ValueError("smoke URL must use an allowlisted local socket")
        if str(url.query.get("port", "5432")) != "5432":
            raise ValueError("smoke URL must use the local PostgreSQL port")
    else:
        try:
            is_loopback = ipaddress.ip_address(url.host or "").is_loopback
        except ValueError:
            is_loopback = (url.host or "").lower() == "localhost"
        port = url.port or 5432
        if not is_loopback or not 1 <= port <= 65535:
            raise ValueError("smoke URL must use local PostgreSQL")
    if _database_identity(url) == _database_identity(make_url(settings.database_url)):
        raise ValueError("smoke URL must not be the application database")
    return url.set(drivername="postgresql+asyncpg")


async def _run_alembic(database_url: URL) -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url.render_as_string(
        hide_password=False
    ).replace("%", "%%")
    executable = Path(sys.executable).with_name("alembic")
    if not executable.is_file():
        raise RuntimeError("Alembic executable is unavailable beside the test Python")
    process = await asyncio.create_subprocess_exec(
        str(executable),
        "upgrade",
        "head",
        cwd=_ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    assert process.returncode == 0, output.decode()


@pytest.fixture(scope="module")
async def postgres_language_database(
    alembic_head: str,
) -> AsyncIterator[SessionMaker]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")

    admin_url = _validated_admin_url(raw_admin_url)
    child_name = f"neurostack_conversation_language_{uuid4().hex[:12]}"
    if _CHILD_PATTERN.fullmatch(child_name) is None:
        raise ValueError("unsafe conversation-language child database name")
    child_url = admin_url.set(database=child_name)
    admin_engine = create_async_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    child_engine: AsyncEngine | None = None
    created_database = False
    try:
        async with admin_engine.connect() as connection:
            can_create = bool(
                (
                    await connection.execute(
                        sa.text(
                            "SELECT rolcreatedb OR rolsuper "
                            "FROM pg_roles WHERE rolname = current_user"
                        )
                    )
                ).scalar_one()
            )
            if not can_create:
                raise PermissionError("smoke database user must have CREATEDB")
            quoted_name = connection.dialect.identifier_preparer.quote(child_name)
            await connection.exec_driver_sql(f"CREATE DATABASE {quoted_name}")
            created_database = True

        await _run_alembic(child_url)
        child_engine = create_async_engine(child_url, poolclass=NullPool)
        factory = async_sessionmaker(child_engine, expire_on_commit=False)
        async with child_engine.connect() as connection:
            assert (
                await connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                )
                == alembic_head
            )
        yield factory
    finally:
        if child_engine is not None:
            await child_engine.dispose()
        try:
            if created_database:
                async with admin_engine.connect() as connection:
                    active = (
                        (
                            await connection.execute(
                                sa.text(
                                    "SELECT pid FROM pg_stat_activity "
                                    "WHERE datname = :database "
                                    "AND pid <> pg_backend_pid() ORDER BY pid"
                                ),
                                {"database": child_name},
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if active:
                        raise AssertionError(
                            "child database still has active sessions; "
                            f"refusing teardown: {active}"
                        )
                    quoted_name = connection.dialect.identifier_preparer.quote(
                        child_name
                    )
                    await connection.exec_driver_sql(f"DROP DATABASE {quoted_name}")
        finally:
            await admin_engine.dispose()


async def _seed_session(
    factory: SessionMaker,
    *,
    tenant_id: UUID | None = None,
    session_id: UUID | None = None,
    active_language: str = "pt-BR",
) -> tuple[UUID, UUID]:
    tenant_id = tenant_id or uuid4()
    session_id = session_id or uuid4()
    async with factory() as db:
        if await db.get(Tenant, tenant_id) is None:
            db.add(
                Tenant(
                    id=tenant_id,
                    name=f"Language {tenant_id}",
                    slug=f"language-{tenant_id}",
                    api_key_hash=f"language-{tenant_id}",
                    config={},
                )
            )
        db.add(
            Session(
                id=session_id,
                tenant_id=tenant_id,
                user_id=f"user-{session_id}",
                channel="test",
                cognitive_state=CognitiveStateData(
                    active_language=active_language
                ).to_dict(),
                session_metadata={},
            )
        )
        await db.commit()
    return tenant_id, session_id


@pytest.mark.asyncio
async def test_same_session_lock_serializes_and_observes_committed_language(
    postgres_language_database: SessionMaker,
) -> None:
    factory = postgres_language_database
    tenant_id, session_id = await _seed_session(factory)

    first_pipeline_entered = asyncio.Event()
    release_first_pipeline = asyncio.Event()

    class SerialPipeline:
        calls = 0

        async def process(
            self,
            input_data: PipelineInput,
            *,
            context: Any = None,
            session: Any = None,
        ) -> PipelineResult:
            self.calls += 1
            if self.calls == 1:
                first_pipeline_entered.set()
                await release_first_pipeline.wait()
            return PipelineResult(
                tenant_id=input_data.tenant_id,
                session_id=input_data.session_id,
                intent="duvida_generica",
                intent_category="question",
                funnel_stage="lead_novo",
                action="respond",
                confidence=0.95,
                safety_concern=False,
                safety_category="none",
                needs_rag=False,
                handoff_required=False,
                system_prompt="system",
                instructions="instructions",
                mode="normal",
                state_patch={"slots": {}, "flags": {}},
            )

    service = EventService(pipeline=cast(Any, SerialPipeline()))
    setattr(
        service,
        "_load_clinic_config",
        AsyncMock(return_value={"business_rules": {"agent_language": "pt-BR"}}),
    )
    setattr(service, "_load_message_history", AsyncMock(return_value=[]))

    def request(text: str) -> EventRequest:
        return EventRequest(
            tenant_id=tenant_id,
            session_id=session_id,
            event_type=EventType.MESSAGE_IN,
            payload=EventPayload(
                content=text,
                channel="whatsapp",
                user_id="5511999999999",
            ),
        )

    async with factory() as first, factory() as second:
        first_turn = asyncio.create_task(
            service.run_pipeline(
                request(
                    "Hello, I would like to schedule an appointment tomorrow please."
                ),
                session=first,
            )
        )
        await asyncio.wait_for(first_pipeline_entered.wait(), timeout=2)

        second_turn = asyncio.create_task(
            service.run_pipeline(request("yes"), session=second)
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(second_turn), timeout=0.15)

        release_first_pipeline.set()
        first_result, _, _ = await asyncio.wait_for(first_turn, timeout=2)
        await first.commit()
        second_result, _, _ = await asyncio.wait_for(second_turn, timeout=2)
        await second.commit()

    assert first_result.deterministic_response == ENGLISH_PROMPT
    assert first_result.response_language == "en"
    assert second_result.response_language == "en"
    async with factory() as observer:
        persisted = await observer.get(Session, session_id)
        assert persisted is not None
        state = persisted.get_cognitive_state()
        assert state.active_language == "en"
        assert state.pending_language is None


@pytest.mark.asyncio
async def test_distinct_sessions_do_not_block(
    postgres_language_database: SessionMaker,
) -> None:
    factory = postgres_language_database
    tenant_id, first_id = await _seed_session(factory)
    _, second_id = await _seed_session(factory, tenant_id=tenant_id)

    async with factory() as first, factory() as second:
        assert (
            await SessionRepository(first).get_by_tenant_for_update(first_id, tenant_id)
            is not None
        )
        second_row = await asyncio.wait_for(
            SessionRepository(second).get_by_tenant_for_update(second_id, tenant_id),
            timeout=1,
        )
        assert second_row is not None
        await first.rollback()
        await second.rollback()


@pytest.mark.asyncio
async def test_tenant_scope_does_not_block_or_leak(
    postgres_language_database: SessionMaker,
) -> None:
    factory = postgres_language_database
    first_tenant, first_id = await _seed_session(factory, active_language="en")
    second_tenant, second_id = await _seed_session(factory, active_language="pt-BR")

    async with factory() as first, factory() as second:
        assert (
            await SessionRepository(first).get_by_tenant_for_update(
                first_id, first_tenant
            )
            is not None
        )
        wrong_tenant = await asyncio.wait_for(
            SessionRepository(second).get_by_tenant_for_update(first_id, second_tenant),
            timeout=1,
        )
        assert wrong_tenant is None
        own_row = await asyncio.wait_for(
            SessionRepository(second).get_by_tenant_for_update(
                second_id, second_tenant
            ),
            timeout=1,
        )
        assert own_row is not None
        assert own_row.get_cognitive_state().active_language == "pt-BR"
        await first.rollback()
        await second.rollback()


@pytest.mark.parametrize("provider", ["meta", "zapi", "evolution", "uazapi"])
@pytest.mark.asyncio
async def test_webhook_commit_releases_lock_before_finish_response(
    postgres_language_database: SessionMaker,
    provider: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = postgres_language_database
    tenant_id, session_id = await _seed_session(factory)
    finished_providers: list[str] = []

    class LockingService:
        async def _load_clinic_config(self, _tenant_id: UUID) -> dict[str, object]:
            return {"business_rules": {"agent_language": "pt-BR"}}

        async def run_pipeline(
            self,
            event_request: EventRequest,
            session: AsyncSession,
            *,
            context: object = None,
        ) -> tuple[object, dict[str, object], list[dict[str, str]]]:
            expected_source = "meta_cloud" if provider == "meta" else provider
            assert event_request.payload.metadata["source"] == expected_source
            row = await SessionRepository(session).get_by_tenant_for_update(
                session_id, tenant_id
            )
            assert row is not None
            return object(), {}, []

        async def finish_response(
            self, *_args: object, context: object = None
        ) -> SimpleNamespace:
            async with factory() as observer:
                row = await asyncio.wait_for(
                    SessionRepository(observer).get_by_tenant_for_update(
                        session_id, tenant_id
                    ),
                    timeout=1,
                )
                assert row is not None
                await observer.rollback()
            finished_providers.append(provider)
            return SimpleNamespace(
                provider=provider,
                prompt=None,
                handoff_summary=None,
                decision=None,
                response_language="pt-BR",
            )

    class RequestStub:
        def __init__(
            self,
            body: dict[str, object],
            headers: dict[str, str] | None = None,
            state: SimpleNamespace | None = None,
        ) -> None:
            self._body = body
            self._raw = json.dumps(body).encode()
            self.headers = headers or {}
            self.state = state or SimpleNamespace()

        async def body(self) -> bytes:
            return self._raw

        async def json(self) -> dict[str, object]:
            return self._body

    service = LockingService()
    monkeypatch.setattr(webhook_module, "get_event_service", lambda: service)
    monkeypatch.setattr(
        webhook_module,
        "_check_bot_active",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        webhook_module,
        "_check_pause_state",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        webhook_module,
        "_get_or_create_session",
        AsyncMock(return_value=(session_id, False)),
    )
    monkeypatch.setattr(
        webhook_module,
        "_load_tenant_credentials",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        webhook_module,
        "_claim_incoming_message",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        webhook_module,
        "check_webhook_rate_limit",
        lambda *_args: SimpleNamespace(allowed=True),
    )
    monkeypatch.setattr(
        webhook_module,
        "_is_ai_sent_message",
        lambda *_args: False,
    )

    message_id = f"{provider}-{uuid4()}"
    phone = "5511999999999"
    if provider == "zapi":
        token = "A" * 43
        body = {
            "instanceId": "instance",
            "phone": phone,
            "messageId": message_id,
            "fromMe": False,
            "text": {"message": "short input"},
        }
        monkeypatch.setattr(
            webhook_module,
            "_resolve_tenant_by_zapi_digest",
            AsyncMock(return_value=(tenant_id, {"zapi_instance_id": "instance"})),
        )
        async with factory() as db:
            response = await webhook_module.zapi_webhook(
                token,
                cast(Any, RequestStub(body)),
                db=db,
            )
    else:
        async with factory() as db:
            if provider == "evolution":
                body = {
                    "event": "messages.upsert",
                    "instance": "instance",
                    "data": {
                        "key": {
                            "remoteJid": f"{phone}@s.whatsapp.net",
                            "fromMe": False,
                            "id": message_id,
                        },
                        "message": {"conversation": "short input"},
                    },
                }
                monkeypatch.setattr(
                    webhook_module,
                    "_resolve_tenant_by_credential",
                    AsyncMock(return_value=tenant_id),
                )
                response = await webhook_module.evolution_webhook(
                    cast(Any, RequestStub(body)), db=db
                )
            elif provider == "uazapi":
                body = {
                    "instanceName": "instance",
                    "chat": {"wa_chatid": f"{phone}@s.whatsapp.net"},
                    "message": {
                        "sender_pn": phone,
                        "messageid": message_id,
                        "type": "text",
                        "text": "short input",
                    },
                }
                monkeypatch.setattr(
                    webhook_module,
                    "_resolve_tenant_by_credential",
                    AsyncMock(return_value=tenant_id),
                )
                response = await webhook_module.uazapi_webhook(
                    cast(Any, RequestStub(body)), db=db
                )
            else:
                body = {
                    "object": "whatsapp_business_account",
                    "entry": [
                        {
                            "changes": [
                                {
                                    "field": "messages",
                                    "value": {
                                        "metadata": {"phone_number_id": "pnid"},
                                        "messages": [
                                            {
                                                "from": phone,
                                                "id": message_id,
                                                "type": "text",
                                                "text": {"body": "short input"},
                                            }
                                        ],
                                    },
                                }
                            ]
                        }
                    ],
                }
                monkeypatch.setattr(
                    webhook_module,
                    "_resolve_tenant_by_meta_phone_number_id",
                    AsyncMock(return_value=(tenant_id, {"meta_app_secret": "secret"})),
                )
                monkeypatch.setattr(
                    webhook_module,
                    "_verify_meta_signature",
                    lambda *_args: True,
                )
                monkeypatch.setattr(
                    webhook_module,
                    "_meta_get_pending_file_review",
                    AsyncMock(return_value=False),
                )
                response = await webhook_module.meta_webhook(
                    cast(
                        Any,
                        RequestStub(
                            body,
                            state=SimpleNamespace(
                                tenant=TenantContext(
                                    tenant_id=tenant_id,
                                    is_active=True,
                                    authentication_mode="meta_hmac",
                                ),
                                canonical_tenant_id=tenant_id,
                                meta_credentials={"meta_app_secret": "secret"},
                                meta_phone_number_id="pnid",
                            ),
                        ),
                    ),
                    db=db,
                )

    assert isinstance(response, dict)
    assert response["ok"] is True
    assert response["replied"] is False
    assert finished_providers == [provider]
