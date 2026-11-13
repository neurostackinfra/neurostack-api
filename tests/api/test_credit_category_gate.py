"""Group 5: new RAG/media content is refused while capacity is exhausted.

The real application on SQLite.  After ``credit_enforcement_at`` a builder
with no capacity is still served (its first upload's reservation becomes
debt, which opens its exhaustion episode); from then on every new upload --
``POST /v1/knowledge/documents``, the multipart
``POST /v1/knowledge/documents/upload`` and ``POST /v1/media-assets`` --
answers ``402 {"code": "credit_category_exhausted"}`` with nothing stored,
until a payment clears the debt and leaves positive capacity.  Before the
enforcement and with the switch off the same open episode changes nothing.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from src.api.middleware.auth import pwd_context
from src.models.builder import Builder
from src.models.content_retention import CreditLifecycleEpisode
from src.models.credit import CreditAccount, CreditEnforcementState
from src.models.credit_inventory import CreditAllocation
from src.models.knowledge import KnowledgeDocument
from src.models.media_asset import MediaAsset
from src.models.tenant import Tenant
from src.repositories.database import get_db_session
from src.services import credit_inventory, credit_metering
from src.services.credit_ledger import CreditLedger
from src.services.credit_switch import CreditSwitch
from tests.api.test_credit_media_assets import _sqlite_capacity_guard  # noqa: F401
from tests.api.test_events_credit_enforced import _grant

UTC = timezone.utc
EXHAUSTED = {"detail": {"code": "credit_category_exhausted"}}
PDF = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "credit_storage_conformance"
    / "positive"
    / "textual.pdf"
).read_bytes()
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@dataclass(frozen=True)
class World:
    builder: UUID
    tenant: UUID
    key: str

    @property
    def headers(self) -> dict[str, str]:
        return {"X-API-Key": self.key}


async def _seed() -> World:
    world = World(uuid4(), uuid4(), secrets.token_urlsafe(24))
    database = get_db_session()
    try:
        async with database.begin():
            database.add(Builder(id=world.builder, name="Gate builder"))
            await database.flush()
            database.add(
                Tenant(
                    id=world.tenant,
                    name=f"Gate {world.tenant.hex[:6]}",
                    slug=f"gate-{world.tenant.hex}",
                    api_key_hash=pwd_context.hash(world.key),
                    builder_id=world.builder,
                    config={},
                )
            )
    finally:
        await database.close()
    return world


async def _enforce() -> None:
    instant = datetime.now(UTC) - timedelta(days=10)
    database = get_db_session()
    try:
        async with database.begin():
            database.add(
                CreditEnforcementState(
                    id=1, credit_enforcement_at=instant, set_by="test", set_at=instant
                )
            )
    finally:
        await database.close()


async def _open_episode(builder: UUID) -> None:
    exhausted = datetime.now(UTC) - timedelta(hours=1)
    database = get_db_session()
    try:
        async with database.begin():
            account = uuid4()
            database.add(CreditAccount(id=account, builder_id=builder))
            await database.flush()
            database.add(
                CreditLifecycleEpisode(
                    id=uuid4(),
                    builder_id=builder,
                    account_id=account,
                    opened_effect_id=f"lifecycle:{builder}:episode:0:opened",
                    exhausted_at=exhausted,
                    suspension_due_at=exhausted + timedelta(hours=72),
                    deletion_due_at=exhausted + timedelta(days=30),
                )
            )
    finally:
        await database.close()


async def _count(model: Any) -> int:
    database = get_db_session()
    try:
        return int(await database.scalar(select(func.count()).select_from(model)) or 0)
    finally:
        await database.close()


async def _open_episodes(builder: UUID) -> int:
    database = get_db_session()
    try:
        return int(
            await database.scalar(
                select(func.count())
                .select_from(CreditLifecycleEpisode)
                .where(
                    CreditLifecycleEpisode.builder_id == builder,
                    CreditLifecycleEpisode.reactivated_at.is_(None),
                )
            )
            or 0
        )
    finally:
        await database.close()


@pytest.fixture
def world(client: Any) -> World:
    return client._submit(_seed())


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    def force(effective: bool) -> None:
        monkeypatch.setattr(CreditSwitch, "effective", lambda _self: effective)

    force(True)
    return force


@pytest.fixture(autouse=True)
def _fresh_process_state() -> Iterator[None]:
    credit_inventory.forget_enforcement_seen()
    credit_metering.forget_enforcement_seen()
    yield
    credit_inventory.forget_enforcement_seen()
    credit_metering.forget_enforcement_seen()


def _knowledge(client: Any, world: World, text: str) -> Any:
    return client.post(
        "/v1/knowledge/documents",
        json={"content": text, "metadata": {"title": "Gate"}},
        headers=world.headers,
    )


def _knowledge_files(client: Any, world: World) -> Any:
    return client.post(
        "/v1/knowledge/documents/upload",
        files={"files": ("horarios.pdf", PDF, "application/pdf")},
        headers=world.headers,
    )


def _media(client: Any, world: World) -> Any:
    return client.post(
        "/v1/media-assets",
        files={"file": ("foto.png", PNG, "image/png")},
        data={"display_name": "Foto", "original_filename": "foto.png"},
        headers=world.headers,
    )


def test_exhaustion_refuses_new_content_until_paid(
    client: Any, world: World, switch: Any
) -> None:
    client._submit(_enforce())
    # No capacity at all: the first reservation is served as debt, which
    # opens the builder's exhaustion episode.
    first = _knowledge(client, world, "Atendemos de segunda a sexta.")
    assert first.status_code == 201, first.text
    assert client._submit(_open_episodes(world.builder)) == 1
    documents, media = (
        client._submit(_count(KnowledgeDocument)),
        client._submit(_count(MediaAsset)),
    )
    for response in (
        _knowledge(client, world, "Aceitamos Unimed."),
        _knowledge_files(client, world),
        _media(client, world),
    ):
        assert response.status_code == 402, response.text
        assert response.json() == EXHAUSTED
    assert client._submit(_count(KnowledgeDocument)) == documents
    assert client._submit(_count(MediaAsset)) == media
    # Paying the debt with capacity left reactivates: content is accepted.
    client._submit(_grant(world.builder, 100_000))
    assert client._submit(_open_episodes(world.builder)) == 0
    assert _knowledge(client, world, "Aceitamos Unimed.").status_code == 201
    assert _media(client, world).status_code == 201


def test_before_the_enforcement_an_open_episode_changes_nothing(
    client: Any, world: World, switch: Any
) -> None:
    client._submit(_open_episode(world.builder))
    assert _knowledge(client, world, "Sem enforcement.").status_code == 201
    assert _knowledge_files(client, world).status_code == 201
    assert _media(client, world).status_code == 201


def test_with_the_switch_off_an_open_episode_changes_nothing(
    client: Any, world: World, switch: Callable[[bool], None]
) -> None:
    client._submit(_enforce())
    client._submit(_open_episode(world.builder))
    switch(False)
    assert _knowledge(client, world, "Chave desligada.").status_code == 201
    assert _media(client, world).status_code == 201
    # The multipart upload is itself a credit surface: off is 503, as on main.
    off = _knowledge_files(client, world)
    assert (off.status_code, off.json()) == (503, {"code": "credit_unavailable"})


# ---------------------------------------------------------------------------
# Only the upload that needs NEW capacity is refused (Deliverable 3).
# ---------------------------------------------------------------------------


async def _seed_media(world: World, size: int) -> None:
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * (size - 8)
    database = get_db_session()
    try:
        async with database.begin():
            database.add(
                MediaAsset(
                    id=uuid4(),
                    tenant_id=world.tenant,
                    display_name="Antiga",
                    original_filename="antiga.png",
                    category="image",
                    mime_type="image/png",
                    size_bytes=len(payload),
                    sha256=hashlib.sha256(payload).digest(),
                    payload=payload,
                )
            )
    finally:
        await database.close()


async def _trace_debt(world: World) -> None:
    """A charged decision of a builder with no lot: debt (opens its episode;
    the trace also opens the accounting window, with the media baseline)."""

    database = get_db_session()
    try:
        async with database.begin():
            result = await CreditLedger(database).debit(
                world.builder,
                f"trace:{world.tenant}:{uuid4()}",
                1_000,
                at=datetime.now(UTC),
                effect_kind="trace",
                tenant_id=world.tenant,
            )
            assert result.status == "applied", result
    finally:
        await database.close()


async def _reservations(category: str) -> list[CreditAllocation]:
    database = get_db_session()
    try:
        return list(
            (
                await database.execute(
                    select(CreditAllocation).where(
                        CreditAllocation.category == category,
                        CreditAllocation.allocation_kind == "reservation",
                    )
                )
            ).scalars()
        )
    finally:
        await database.close()


def test_a_zero_delta_upload_is_accepted_while_the_builder_is_exhausted(
    client: Any, world: World, switch: Any
) -> None:
    """An episode is open, but the new image fits the media coverage the
    window already has (1,000 + 72 bytes round to the same 1 mc): nothing is
    charged, so it is accepted (201); a RAG upload of the same builder needs
    new capacity and is refused -- only the exhausted need is blocked."""

    client._submit(_seed_media(world, 1_000))
    client._submit(_enforce())
    client._submit(_trace_debt(world))
    assert client._submit(_open_episodes(world.builder)) == 1
    media = client._submit(_count(MediaAsset))
    accepted = _media(client, world)
    assert accepted.status_code == 201, accepted.text
    assert client._submit(_count(MediaAsset)) == media + 1
    assert client._submit(_reservations("media")) == []  # zero delta: no charge
    refused = _knowledge(client, world, "Aceitamos Unimed.")
    assert (refused.status_code, refused.json()) == (402, EXHAUSTED)
    assert client._submit(_count(KnowledgeDocument)) == 0
    assert client._submit(_open_episodes(world.builder)) == 1


def test_a_builder_with_capacity_is_never_category_exhausted(
    client: Any, world: World, switch: Any
) -> None:
    """Capacity in force (no episode): every upload is accepted and charged
    from the lot -- no debt, no refusal, no episode."""

    client._submit(_enforce())
    client._submit(_grant(world.builder, 100_000))
    for response in (
        _knowledge(client, world, "Atendemos de segunda a sexta."),
        _knowledge_files(client, world),
        _media(client, world),
    ):
        assert response.status_code == 201, response.text
    assert client._submit(_open_episodes(world.builder)) == 0
    assert client._submit(_reservations("rag"))


# ---------------------------------------------------------------------------
# debito-cortesia-automatico Group 5a: uploads under the courtesy rule (no
# ``credit_enforcement_at``).  A builder the rule blocks uploads nothing --
# 402 ``credit_suspended`` before any body is read or row written, from an
# API key and from the panel alike; a charged one uploads, paid from its
# courtesy, whatever episode a mark era left open.
# ---------------------------------------------------------------------------

SUSPENDED = {"detail": {"code": "credit_suspended"}}


@pytest.fixture
def rule_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process reading at this image's head (046 and 047 gates open)."""

    from src.services.credit_switch import (
        CreditSwitchReading,
        load_image_migration_chain,
    )

    reading = CreditSwitchReading(
        True, "schema_ready", load_image_migration_chain()[-1]
    )
    monkeypatch.setattr(CreditSwitch, "reading", lambda _self: reading)


async def _rule(builder: UUID, credits: int, *, keep: bool) -> None:
    from src.services import credit_courtesy

    database = get_db_session()
    try:
        async with database.begin():
            granted = await credit_courtesy.grant(
                database,
                builder_id=builder,
                credits=credits,
                reason="cortesia",
                actor_user_id=uuid4(),
                idempotency_key=f"gate-rule-{uuid4()}",
            )
        if not keep:
            async with database.begin():
                await credit_courtesy.remove(
                    database,
                    lot_id=granted.lot_id,
                    remove_all=True,
                    reason="tirar tudo",
                    actor_user_id=uuid4(),
                    idempotency_key=f"gate-rule-removal-{uuid4()}",
                )
    finally:
        await database.close()


async def _stale_episode(builder: UUID, *, overdue: bool) -> None:
    """An exhaustion episode left open from a mark era, on the builder's
    existing account (the courtesy grant created it)."""

    exhausted = datetime.now(UTC) - timedelta(hours=73 if overdue else 1)
    database = get_db_session()
    try:
        async with database.begin():
            account = await database.scalar(
                select(CreditAccount.id).where(CreditAccount.builder_id == builder)
            )
            database.add(
                CreditLifecycleEpisode(
                    id=uuid4(),
                    builder_id=builder,
                    account_id=account,
                    opened_effect_id=f"lifecycle:{builder}:episode:0:opened",
                    exhausted_at=exhausted,
                    suspension_due_at=exhausted + timedelta(hours=72),
                    deletion_due_at=exhausted + timedelta(days=30),
                )
            )
    finally:
        await database.close()


async def _clinic_admin(world: World) -> UUID:
    """A clinic admin user of the world's clinic (the panel's caller)."""

    from src.models.user import User, UserRole

    user_id = uuid4()
    database = get_db_session()
    try:
        async with database.begin():
            database.add(
                User(
                    id=user_id,
                    tenant_id=world.tenant,
                    email=f"{user_id.hex}@panel.invalid",
                    password_hash="x",
                    role=UserRole.clinic_admin,
                    is_active=True,
                )
            )
    finally:
        await database.close()
    return user_id


def _panel(world: World, user_id: UUID | None = None) -> dict[str, str]:
    """A clinic admin's JWT on the world's clinic (the panel)."""

    import jwt

    from src.config.settings import settings

    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": str(user_id or uuid4()),
            "tenant_id": str(world.tenant),
            "role": "clinic_admin",
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


async def _written() -> dict[str, int]:
    from src.models.credit import CreditEffect, CreditLedgerEntry

    return {
        model.__tablename__: await _count(model)
        for model in (
            KnowledgeDocument,
            MediaAsset,
            CreditAllocation,
            CreditEffect,
            CreditLedgerEntry,
        )
    }


def _uploads(client: Any, world: World, headers: dict[str, str]) -> list[Any]:
    return [
        client.post(
            "/v1/knowledge/documents",
            json={"content": "Aceitamos Unimed.", "metadata": {"title": "Gate"}},
            headers=headers,
        ),
        client.post(
            "/v1/knowledge/documents/upload",
            files={"files": ("horarios.pdf", PDF, "application/pdf")},
            headers=headers,
        ),
        client.post(
            "/v1/media-assets",
            files={"file": ("foto.png", PNG, "image/png")},
            data={"display_name": "Foto", "original_filename": "foto.png"},
            headers=headers,
        ),
    ]


def test_a_rule_blocked_builder_uploads_nothing_from_a_key_or_the_panel(
    client: Any,
    world: World,
    switch: Any,
    rule_reading: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.routes import knowledge, media_assets

    client._submit(_rule(world.builder, 3, keep=False))
    panel = _panel(world, client._submit(_clinic_admin(world)))
    before = client._submit(_written())

    async def unread(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a blocked builder's body was read")

    async def unwritten(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a blocked builder's upload reached a write")

    from src.repositories import KnowledgeDocumentRepository

    monkeypatch.setattr(knowledge, "_read_upload_files", unread)
    monkeypatch.setattr(media_assets, "_parse_upload", unread)
    # Before ANY write, not a write rolled back: neither the row nor the
    # reservation is ever attempted.
    monkeypatch.setattr(KnowledgeDocumentRepository, "create", unwritten)
    monkeypatch.setattr(credit_inventory, "reserve_upload", unwritten)
    for headers in (world.headers, panel):
        for response in _uploads(client, world, headers):
            assert (response.status_code, response.json()) == (402, SUSPENDED), (
                response.request.url,
                response.text,
            )
    assert client._submit(_written()) == before
    assert client._submit(_open_episodes(world.builder)) == 0


def test_the_reservation_lock_refuses_a_blocked_capture_too(
    client: Any,
    world: World,
    switch: Any,
    rule_reading: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second layer: with the entry refusal out of the way, the
    reservation's verdict on the captured mode is ``suspended`` -- answered
    402 ``credit_suspended`` and rolled back, never served as ``ok``."""

    from src.api.routes import knowledge, media_assets

    client._submit(_rule(world.builder, 3, keep=False))
    before = client._submit(_written())

    async def let_through(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(knowledge, "_refuse_blocked", let_through)
    monkeypatch.setattr(media_assets, "_refuse_blocked", lambda _mode: None)
    for response in _uploads(client, world, world.headers):
        assert (response.status_code, response.json()) == (402, SUSPENDED), (
            response.request.url,
            response.text,
        )
    assert client._submit(_written()) == before


@pytest.mark.parametrize("module_name", ["knowledge", "media_assets"])
def test_every_category_verdict_has_its_own_answer(module_name: str) -> None:
    import importlib

    module = importlib.import_module(f"src.api.routes.{module_name}")
    assert module._verdict_refusal("ok") is None
    for verdict, code in (
        ("suspended", "credit_suspended"),
        ("category_exhausted", "credit_category_exhausted"),
    ):
        refusal = module._verdict_refusal(verdict)
        assert (refusal.status_code, refusal.detail) == (402, {"code": code})


def test_a_charged_rule_builder_uploads_from_courtesy_despite_a_stale_episode(
    client: Any, world: World, switch: Any, rule_reading: None
) -> None:
    """A charged builder's uploads are served and their reservations paid
    by the courtesy; an exhaustion episode left from a mark era is not the
    rule's and refuses nothing (no ``credit_category_exhausted``)."""

    client._submit(_rule(world.builder, 100, keep=True))
    client._submit(_stale_episode(world.builder, overdue=False))
    panel = _panel(world, client._submit(_clinic_admin(world)))
    for headers in (world.headers, panel):
        for response in _uploads(client, world, headers):
            assert response.status_code == 201, (response.request.url, response.text)
    reserved = client._submit(_reservations("rag")) + client._submit(
        _reservations("media")
    )
    assert reserved and all(row.charged_mc >= 0 for row in reserved)
    assert sum(row.charged_mc for row in client._submit(_reservations("rag"))) > 0


def test_without_courtesy_the_rule_reading_changes_no_upload(
    client: Any, world: World, switch: Any, rule_reading: None
) -> None:
    """Dominance: a builder that never had courtesy (no global, no mark) is
    observational with the rule's gate open -- every upload is accepted as
    on ``prod-fe800aec`` and nothing is reserved."""

    for response in _uploads(client, world, world.headers):
        assert response.status_code == 201, response.text
    assert client._submit(_reservations("rag")) == []
    assert client._submit(_reservations("media")) == []
