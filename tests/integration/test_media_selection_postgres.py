"""The agent's media selection SQL on real PostgreSQL.

envio-midia-agente Group 4.  One database at ``alembic upgrade head`` with
two tenants, shared by the module (every test reads, or seeds its own
sessions or tenant), plus a clone of it downgraded to 044 -- the schema
before the bridge migration, without ``media_assets.trigger_intents`` and
``media_deliveries``.  Pinned here, through the real
:func:`select_media_candidates`:

* a file is a candidate only when it is the tenant's, ``auto_suggest``,
  bound to the turn's intent and within its category's byte limit (the
  limit itself is a candidate); not ``auto_suggest``, unbound, bound to
  another intent, above the limit, deleted, or another tenant's: never;
* a price file (``pedido_preco`` bound) only when the prompt may show a
  price -- on a ``pedido_preco`` turn and on a turn of another bound intent;
* a file sent to the session within 24 hours is not a candidate there (its
  name is listed as sent), still is in another session, and is again once
  the send is 24 hours old;
* newest ``updated_at`` first, at most five, ``capped`` beyond;
* only formats the turn's provider sends: Meta is offered OGG and M4A
  audio, Z-API, uazapi and Evolution only the MP3 (and every other format
  all four send); an empty format set reads no row and no error;
* with the flag off (or no provider) not one statement is sent: on the 044
  database the selection still reads as "no candidates" without an error,
  and only the flag-on control fails there (closed) on the missing column;
* end to end through ``EventService.finish_response``: the LLM's
  ``[[MIDIA:1]]`` attaches the newest candidate.

Needs ``POSTGRES_SMOKE_DATABASE_URL``; without it the module skips, and only
``scripts/check-credit-integration.sh`` turns that skip into a failure.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Optional
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import src.models  # noqa: F401  (registers every mapper on Base.metadata)
import src.services.event_service as event_service_module
from src.api.schemas.events import CanonicalEventRequest, EventPayload, EventType
from src.core.pipeline import CognitivePipeline, PipelineResult
from src.interaction_context import InteractionOrigin, build_interaction_context
from src.models.media_asset import MediaAsset
from src.models.session import Session
from src.models.tenant import Tenant
from src.services import media_selection
from src.services.event_service import EventService
from src.services.media_assets import MEDIA_IMAGE_MAX_BYTES
from src.services.media_selection import MediaOffer, select_media_candidates
from tests.integration.test_asaas_billing_postgres import _validated_admin_url
from tests.integration.test_migration_036_postgres import _Cluster, _migrate

pytestmark = pytest.mark.integration

_PDF = b"%PDF-1.7\nagent media selection"
_PRICE_SHOWN: dict[str, Any] = {
    "business_rules": {"accepts_insurance": "no", "provide_prices": "yes"},
    "pricing": {"disclosure_mode": "consult_only"},
}
_PRICE_HIDDEN: dict[str, Any] = {
    "business_rules": {"accepts_insurance": "yes", "provide_prices": "yes"},
    "pricing": {"disclosure_mode": "exact_allowed"},
}


@dataclass
class _World:
    head: AsyncEngine
    bridge_less: AsyncEngine
    tenant: UUID
    other_tenant: UUID
    sessions: list[UUID]
    other_session: UUID
    media: dict[str, UUID] = field(default_factory=dict)
    names: dict[UUID, str] = field(default_factory=dict)


def _maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


def _tenant(tenant_id: UUID) -> Tenant:
    return Tenant(
        id=tenant_id,
        name="Media selection",
        slug=f"media-selection-{tenant_id.hex}",
        api_key_hash=f"media-selection-{tenant_id.hex}",
        plan_type="clinic",
        config={},
    )


def _session(tenant_id: UUID, session_id: UUID) -> Session:
    return Session(
        id=session_id,
        tenant_id=tenant_id,
        channel="whatsapp",
        cognitive_state={},
        session_metadata={},
    )


def _asset(
    tenant_id: UUID,
    name: str,
    *,
    intents: list[str],
    auto_suggest: bool = True,
    category: str = "pdf",
    payload: bytes = _PDF,
    mime_type: Optional[str] = None,
) -> MediaAsset:
    mime = mime_type or {"pdf": "application/pdf", "image": "image/png"}[category]
    return MediaAsset(
        id=uuid4(),
        tenant_id=tenant_id,
        display_name=name,
        original_filename=f"{name}.{mime.rsplit('/', 1)[-1]}",
        category=category,
        mime_type=mime,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).digest(),
        payload=payload,
        usage_trigger=f"Quando o paciente pedir {name}",
        auto_suggest=auto_suggest,
        trigger_intents=intents,
    )


async def _seed(world: _World) -> None:
    tenant, other = world.tenant, world.other_tenant
    image_at_limit = b"\x89PNG" + bytes(MEDIA_IMAGE_MAX_BYTES - 4)
    image_over_limit = image_at_limit + b"\x00"
    # Oldest first: ``updated_at`` is set below in this order.
    assets = {
        "bound": _asset(tenant, "Preparo", intents=["detalhes_servicos"]),
        "price": _asset(
            tenant, "Tabela de preços", intents=["pedido_preco", "detalhes_servicos"]
        ),
        "at_limit": _asset(
            tenant,
            "Fachada",
            intents=["detalhes_servicos"],
            category="image",
            payload=image_at_limit,
        ),
        "over_limit": _asset(
            tenant,
            "Fachada grande",
            intents=["detalhes_servicos"],
            category="image",
            payload=image_over_limit,
        ),
        "not_auto": _asset(
            tenant, "Manual", intents=["detalhes_servicos"], auto_suggest=False
        ),
        "unbound": _asset(tenant, "Sem assunto", intents=[]),
        "other_intent": _asset(
            tenant, "Cuidados pós", intents=["pos_atendimento_duvida"]
        ),
        "deleted": _asset(tenant, "Apagado", intents=["detalhes_servicos"]),
        "foreign": _asset(other, "De outra clínica", intents=["detalhes_servicos"]),
    }
    async with _maker(world.head)() as database:
        database.add_all([_tenant(tenant), _tenant(other)])
        await database.flush()
        database.add_all(
            [_session(tenant, session_id) for session_id in world.sessions]
            + [_session(other, world.other_session)]
        )
        database.add_all(list(assets.values()))
        await database.commit()
    async with world.head.begin() as connection:
        for age, asset in enumerate(reversed(list(assets.values()))):
            await connection.execute(
                text(
                    "UPDATE media_assets SET updated_at = now() - "
                    "make_interval(mins => :age) WHERE id = :id"
                ),
                {"age": age + 1, "id": asset.id},
            )
        await connection.execute(
            text("DELETE FROM media_assets WHERE id = :id"),
            {"id": assets["deleted"].id},
        )
    world.media = {key: asset.id for key, asset in assets.items()}
    world.names = {asset.id: asset.display_name for asset in assets.values()}


@pytest.fixture(scope="module")
async def world() -> AsyncIterator[_World]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_admin_url)
    admin = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    cluster = _Cluster(admin_url=admin_url, admin=admin, created=[])
    engines: list[AsyncEngine] = []
    try:
        head_name = await cluster.fresh()
        await _migrate(cluster.url(head_name), "upgrade", "head")
        bridge_less_name = await cluster.clone(head_name)
        await _migrate(cluster.url(bridge_less_name), "downgrade", "044")
        print(f"MEDIA_SELECTION_PG_DATABASES={head_name},{bridge_less_name}")
        head = create_async_engine(cluster.url(head_name), poolclass=NullPool)
        bridge_less = create_async_engine(
            cluster.url(bridge_less_name), poolclass=NullPool
        )
        engines.extend([head, bridge_less])
        built = _World(
            head=head,
            bridge_less=bridge_less,
            tenant=uuid4(),
            other_tenant=uuid4(),
            sessions=[uuid4() for _ in range(4)],
            other_session=uuid4(),
        )
        await _seed(built)
        yield built
    finally:
        for engine in engines:
            await engine.dispose()
        for created in list(reversed(cluster.created)):
            await cluster.drop(created)
        await admin.dispose()


class _CountingFactory:
    """Sessions on one engine, counting every statement they send."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.maker = _maker(engine)
        self.opened = 0
        self.statements: list[str] = []

    def __call__(self) -> AsyncSession:
        self.opened += 1
        session = self.maker()
        real_execute = session.execute

        async def execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
            self.statements.append(str(statement))
            return await real_execute(statement, *args, **kwargs)

        session.execute = execute  # type: ignore[method-assign]
        return session


def _result(
    tenant_id: UUID, session_id: UUID, intent: str = "detalhes_servicos"
) -> PipelineResult:
    return PipelineResult(
        tenant_id=tenant_id,
        session_id=session_id,
        intent=intent,
        intent_category="information",
        funnel_stage="em_qualificacao",
        action="provide_info",
        confidence=0.9,
        safety_concern=False,
        safety_category="none",
        needs_rag=False,
        handoff_required=False,
        system_prompt="sys",
        instructions="inst",
        mode="normal",
    )


async def _select(
    engine: AsyncEngine,
    tenant_id: UUID,
    session_id: UUID,
    *,
    intent: str = "detalhes_servicos",
    clinic_config: Optional[dict[str, Any]] = None,
    flag: bool = True,
    provider: Optional[str] = "zapi",
    factory: Optional[_CountingFactory] = None,
) -> Optional[MediaOffer]:
    async def read_flag() -> bool:
        return flag

    context = build_interaction_context(
        effective_tenant_id=tenant_id,
        session_id=session_id,
        origin=InteractionOrigin.PRODUCTION,
        event_type="message.in",
    )
    return await select_media_candidates(
        result=_result(tenant_id, session_id, intent),
        clinic_config=_PRICE_SHOWN if clinic_config is None else clinic_config,
        context=context,
        delivery_provider=provider,
        flag_reader=read_flag,
        session_factory=factory or _maker(engine),
    )


def _keys(world: _World, offer: Optional[MediaOffer]) -> list[str]:
    assert offer is not None
    by_id = {media_id: key for key, media_id in world.media.items()}
    return [by_id[candidate.media_id] for candidate in offer.candidates]


async def _deliver(
    engine: AsyncEngine,
    tenant_id: UUID,
    session_id: UUID,
    media_id: UUID,
    age: timedelta,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO media_deliveries (id, tenant_id, session_id, media_id, "
                "last_sent_at) VALUES (:id, :tenant, :session, :media, "
                "now() - CAST(:age AS interval))"
            ),
            {
                "id": uuid4(),
                "tenant": tenant_id,
                "session": session_id,
                "media": media_id,
                "age": age,
            },
        )


# ---------------------------------------------------------------------------
# Which files are candidates.
# ---------------------------------------------------------------------------


async def test_only_the_tenants_bound_auto_files_within_the_limit_are_candidates(
    world: _World,
) -> None:
    offer = await _select(world.head, world.tenant, world.sessions[0])

    # Newest first; the image at exactly its limit is in, one byte over out.
    assert _keys(world, offer) == ["at_limit", "price", "bound"]
    assert offer is not None and offer.capped is False
    assert offer.recently_sent_names == ()
    first = offer.candidates[0]
    assert (first.index, first.category, first.mime_type, first.size_bytes) == (
        1,
        "image",
        "image/png",
        MEDIA_IMAGE_MAX_BYTES,
    )
    assert first.display_name == "Fachada"
    assert first.usage_trigger == "Quando o paciente pedir Fachada"


async def test_another_intent_and_another_tenant_see_only_their_own_files(
    world: _World,
) -> None:
    post = await _select(
        world.head, world.tenant, world.sessions[0], intent="pos_atendimento_duvida"
    )
    foreign = await _select(world.head, world.other_tenant, world.other_session)
    nothing = await _select(
        world.head, world.tenant, world.sessions[0], intent="agendar"
    )

    assert _keys(world, post) == ["other_intent"]
    assert _keys(world, foreign) == ["foreign"]
    assert nothing is None


@pytest.mark.parametrize("intent", ["saudacao", "faq"])
async def test_an_outras_turn_sees_every_automatic_file_of_its_tenant(
    world: _World, intent: str
) -> None:
    # "Outras" is implied: bound or not, every file with the switch on is a
    # candidate, under the same tenant, limit and price filters.
    shown = await _select(world.head, world.tenant, world.sessions[0], intent=intent)
    hidden = await _select(
        world.head,
        world.tenant,
        world.sessions[0],
        intent=intent,
        clinic_config=_PRICE_HIDDEN,
    )

    assert _keys(world, shown) == [
        "other_intent",
        "unbound",
        "at_limit",
        "price",
        "bound",
    ]
    assert shown is not None and shown.capped is False
    assert _keys(world, hidden) == ["other_intent", "unbound", "at_limit", "bound"]


async def test_a_price_file_follows_the_prompt_on_any_bound_intent(
    world: _World,
) -> None:
    hidden_other_intent = await _select(
        world.head, world.tenant, world.sessions[0], clinic_config=_PRICE_HIDDEN
    )
    shown_price_turn = await _select(
        world.head, world.tenant, world.sessions[0], intent="pedido_preco"
    )
    hidden_price_turn = await _select(
        world.head,
        world.tenant,
        world.sessions[0],
        intent="pedido_preco",
        clinic_config=_PRICE_HIDDEN,
    )

    assert _keys(world, hidden_other_intent) == ["at_limit", "bound"]
    assert _keys(world, shown_price_turn) == ["price"]
    assert hidden_price_turn is None


# ---------------------------------------------------------------------------
# The 24-hour window.
# ---------------------------------------------------------------------------


async def test_a_file_sent_within_24_hours_is_not_offered_to_that_session(
    world: _World,
) -> None:
    session, other_session = world.sessions[1], world.sessions[2]
    await _deliver(
        world.head, world.tenant, session, world.media["bound"], timedelta(hours=23)
    )

    here = await _select(world.head, world.tenant, session)
    there = await _select(world.head, world.tenant, other_session)

    assert _keys(world, here) == ["at_limit", "price"]
    assert here is not None and here.recently_sent_names == ("Preparo",)
    assert _keys(world, there) == ["at_limit", "price", "bound"]
    assert there is not None and there.recently_sent_names == ()


async def test_a_send_24_hours_old_is_offered_again(world: _World) -> None:
    session = world.sessions[3]
    await _deliver(
        world.head,
        world.tenant,
        session,
        world.media["bound"],
        timedelta(hours=24, seconds=1),
    )

    offer = await _select(world.head, world.tenant, session)

    assert _keys(world, offer) == ["at_limit", "price", "bound"]
    assert offer is not None and offer.recently_sent_names == ()


async def test_more_than_five_candidates_are_capped_newest_first(
    world: _World,
) -> None:
    tenant_id, session_id = uuid4(), uuid4()
    assets = [
        _asset(tenant_id, f"Arquivo {index}", intents=["detalhes_servicos"])
        for index in range(7)
    ]
    async with _maker(world.head)() as database:
        database.add(_tenant(tenant_id))
        await database.flush()
        database.add(_session(tenant_id, session_id))
        database.add_all(assets)
        await database.commit()
    async with world.head.begin() as connection:
        for age, asset in enumerate(assets):
            await connection.execute(
                text(
                    "UPDATE media_assets SET updated_at = now() - "
                    "make_interval(mins => :age) WHERE id = :id"
                ),
                {"age": age + 1, "id": asset.id},
            )

    offer = await _select(world.head, tenant_id, session_id, intent="detalhes_servicos")

    assert offer is not None and offer.capped is True
    assert [candidate.media_id for candidate in offer.candidates] == [
        asset.id for asset in assets[:5]
    ]
    assert [candidate.index for candidate in offer.candidates] == [1, 2, 3, 4, 5]


def _ogg_opus(channels: int = 1) -> bytes:
    """One RFC 3533 page whose only packet is an RFC 7845 OpusHead."""

    head = b"OpusHead" + bytes([1, channels]) + bytes(9)
    page = b"OggS" + bytes([0, 2]) + bytes(20) + bytes([1, len(head)])
    return page + head


async def test_only_the_formats_the_turns_provider_sends_are_candidates(
    world: _World,
) -> None:
    tenant_id, session_id = uuid4(), uuid4()
    assets = {
        "mp3": _asset(
            tenant_id,
            "Áudio MP3",
            intents=["detalhes_servicos"],
            category="audio",
            mime_type="audio/mpeg",
            payload=b"ID3\x04" + bytes(64),
        ),
        "ogg": _asset(
            tenant_id,
            "Áudio OGG",
            intents=["detalhes_servicos"],
            category="audio",
            mime_type="audio/ogg",
            payload=_ogg_opus(),
        ),
        "m4a": _asset(
            tenant_id,
            "Áudio M4A",
            intents=["detalhes_servicos"],
            category="audio",
            mime_type="audio/mp4",
            payload=bytes(4) + b"ftypM4A " + bytes(52),
        ),
        "pdf": _asset(tenant_id, "Preparo", intents=["detalhes_servicos"]),
    }
    async with _maker(world.head)() as database:
        database.add(_tenant(tenant_id))
        await database.flush()
        database.add(_session(tenant_id, session_id))
        database.add_all(list(assets.values()))
        await database.commit()
    async with world.head.begin() as connection:
        for age, asset in enumerate(assets.values()):
            await connection.execute(
                text(
                    "UPDATE media_assets SET updated_at = now() - "
                    "make_interval(mins => :age) WHERE id = :id"
                ),
                {"age": age + 1, "id": asset.id},
            )
    by_id = {asset.id: key for key, asset in assets.items()}

    offered: dict[str, list[str]] = {}
    for provider in ("meta", "zapi", "uazapi", "evolution"):
        offer = await _select(
            world.head,
            tenant_id,
            session_id,
            intent="detalhes_servicos",
            provider=provider,
        )
        assert offer is not None, provider
        offered[provider] = [by_id[item.media_id] for item in offer.candidates]

    assert offered == {
        "meta": ["mp3", "ogg", "m4a", "pdf"],
        "zapi": ["mp3", "pdf"],
        "uazapi": ["mp3", "pdf"],
        "evolution": ["mp3", "pdf"],
    }


async def test_an_empty_format_set_reads_no_row_on_postgresql(world: _World) -> None:
    """``_read_candidates`` keeps no guard for an empty ``sendable`` (the
    selection excludes it purely before): SQLAlchemy sends the empty ``IN``
    as ``IN (NULL) AND (1 != 1)``, which PostgreSQL answers with no row and
    no error.  The same read with the PDF's format finds the file."""

    tenant_id, session_id = uuid4(), uuid4()
    async with _maker(world.head)() as database:
        database.add(_tenant(tenant_id))
        await database.flush()
        database.add(_session(tenant_id, session_id))
        database.add(_asset(tenant_id, "Preparo", intents=["detalhes_servicos"]))
        await database.commit()

    found: dict[str, int] = {}
    for label, sendable in (
        ("empty", frozenset[str]()),
        ("pdf", frozenset({"application/pdf"})),
    ):
        async with _maker(world.head)() as database:
            rows = await media_selection._read_candidates(
                database,
                tenant_id=tenant_id,
                session_id=session_id,
                intent="detalhes_servicos",
                price_allowed=True,
                sendable=sendable,
            )
        found[label] = len(rows)

    assert found == {"empty": 0, "pdf": 1}


# ---------------------------------------------------------------------------
# No media SQL before the flag and the provider.
# ---------------------------------------------------------------------------


async def test_the_bridge_less_database_really_lacks_the_media_schema(
    world: _World,
) -> None:
    async with world.bridge_less.connect() as connection:
        version = (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
        column = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns WHERE "
                    "table_name = 'media_assets' AND column_name = 'trigger_intents'"
                )
            )
        ).scalar_one()
        table = (
            await connection.execute(
                text("SELECT to_regclass('public.media_deliveries') IS NULL")
            )
        ).scalar_one()

    assert (version, column, table) == ("044", 0, True)


@pytest.mark.parametrize("database", ["head", "bridge_less"])
@pytest.mark.parametrize("gate", ["flag_off", "no_provider"])
async def test_a_closed_gate_sends_no_statement_on_either_schema(
    world: _World, database: str, gate: str
) -> None:
    engine = world.head if database == "head" else world.bridge_less
    factory = _CountingFactory(engine)

    offer = await _select(
        engine,
        world.tenant,
        world.sessions[0],
        flag=gate != "flag_off",
        provider=None if gate == "no_provider" else "zapi",
        factory=factory,
    )

    assert offer is None
    assert (factory.opened, factory.statements) == (0, [])


async def test_the_flag_on_control_fails_closed_on_the_bridge_less_schema(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-vacuity of the test above: with the flag on, the selection does
    send its SQL, which the 044 schema refuses -- and still reads as "no
    candidates" (the one expected server-log ERROR of this module)."""

    warnings: list[dict[str, Any]] = []

    def warning(event: str, **fields: Any) -> None:
        warnings.append({"event": event, **fields})

    monkeypatch.setattr(media_selection.logger, "warning", warning)
    factory = _CountingFactory(world.bridge_less)

    offer = await _select(
        world.bridge_less, world.tenant, world.sessions[0], factory=factory
    )

    assert offer is None
    assert factory.opened == 1
    assert len(factory.statements) == 1
    assert "trigger_intents" in factory.statements[0]
    assert [entry["event"] for entry in warnings] == ["agent_media_selection_failed"]
    assert warnings[0]["error_type"] == "ProgrammingError"


# ---------------------------------------------------------------------------
# End to end through phase 2.
# ---------------------------------------------------------------------------


class _LLM:
    def __init__(self) -> None:
        self.messages: list[list[dict[str, str]]] = []

    async def generate(self, messages: list[dict[str, str]], **_kwargs: Any) -> Any:
        self.messages.append(list(messages))
        return {"content": "Claro! Segue a foto. [[MIDIA:1]]", "usage": {}}


async def test_finish_response_attaches_the_chosen_candidate(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = _LLM()
    monkeypatch.setattr("src.llm.client.get_tenant_llm_client", lambda *_a: llm)
    monkeypatch.setattr(
        event_service_module,
        "_load_runtime_llm_credentials",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        media_selection, "agent_media_send_enabled", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(media_selection, "get_db_session", _maker(world.head))
    service = EventService(pipeline=CognitivePipeline())
    monkeypatch.setattr(service, "_save_events_to_store", AsyncMock(return_value=None))
    request = CanonicalEventRequest(
        tenant_id=world.tenant,
        session_id=world.sessions[0],
        event_type=EventType.MESSAGE_IN,
        payload=EventPayload(content="tem foto da clínica?", channel="whatsapp"),
    )
    context = build_interaction_context(
        effective_tenant_id=world.tenant,
        session_id=world.sessions[0],
        origin=InteractionOrigin.PRODUCTION,
        event_type=request.event_type,
    )

    response = await service.finish_response(
        request,
        _result(world.tenant, world.sessions[0]),
        _PRICE_SHOWN,
        [],
        context=context,
        media_delivery_provider="meta",
    )

    assert response.attachment is not None
    assert response.attachment.media_id == world.media["at_limit"]
    assert response.attachment.category == "image"
    assert response.attachment.size_bytes == MEDIA_IMAGE_MAX_BYTES
    assert response.prompt.suggested_response == "Claro! Segue a foto."
    (messages,) = llm.messages
    assert '"nome": "Fachada"' in messages[-2]["content"]
