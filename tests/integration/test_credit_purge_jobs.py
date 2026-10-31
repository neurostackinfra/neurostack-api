"""Purge jobs against disposable PostgreSQL + S3 (Object Lock) + Qdrant (G5).

The real :class:`CreditRetentionAdapter` over the three stores, driven by
:class:`PurgeExecutor` exactly as Group 8 will (the public entry stays
``purge_disabled``).  The payloads are the real conformance fixture bytes.

* tombstone hides and drops only the Qdrant points; a hold that wins before
  the fence restores the SAME PostgreSQL bytes/SHA-256, the SAME S3
  ``VersionId`` and the SAME Qdrant point ids;
* a fenced purge deletes the PostgreSQL row with its bytes and the exact S3
  ``VersionId``, then confirms absence (row gone, ``head_object`` 404, no such
  version in ``list_object_versions``, no point); a replay deletes nothing
  twice;
* crash between the S3 delete and the recorded result converges on
  recovery; a tombstone without a valid fence is ``blocked`` with nothing
  deleted; a legal hold on the S3 version is ``held`` and keeps the row.

Guarded by ``POSTGRES_SMOKE_DATABASE_URL``, ``CREDIT_TEST_S3_*`` and
``CREDIT_TEST_QDRANT_URL``; it skips without them, and only
``scripts/check-credit-integration.sh`` turns that skip into a failure.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
from botocore.exceptions import ClientError
from pydantic import SecretStr
from qdrant_client import AsyncQdrantClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.config.settings import Settings, settings
from src.models.builder import Builder
from src.models.content_retention import (
    CreditLifecycleEpisode,
    CreditLifecycleTransition,
    CreditRetentionTombstone,
)
from src.models.credit import CreditAccount
from src.models.credit_object_copy import CreditObjectCopy
from src.models.knowledge import KnowledgeDocument
from src.models.media_asset import MediaAsset
from src.models.tenant import Tenant
from src.models.user import User, UserRole
from src.services import content_retention
from src.services.content_retention import Actor, PresentedAuthorization, PurgeExecutor
from src.services.knowledge_service import extract_document_text
from src.storage.credit_object_store import (
    StoredCopy,
    _client,
    delete_exact_copy,
    put_immutable_copy,
    set_legal_hold,
)
from src.storage.credit_qdrant import replace_derived_chunks
from src.storage.credit_retention_adapter import (
    CreditRetentionAdapter,
    derived_point_ids,
)
from tests.integration.test_asaas_billing_postgres import _validated_admin_url
from tests.integration.test_migration_036_postgres import _Cluster, _migrate

pytestmark = pytest.mark.integration

UTC = timezone.utc
KEY = "credit-authorization-signing-key-for-purge-jobs-0123456789"
_FIXTURES = (
    Path(__file__).resolve().parents[1] / "fixtures" / "credit_storage_conformance"
)
PDF = (_FIXTURES / "positive" / "textual.pdf").read_bytes()
DOCX_TEXT = extract_document_text(
    (_FIXTURES / "positive" / "textual.docx").read_bytes()
).text


def _s3() -> Settings:
    names = (
        "CREDIT_TEST_S3_ENDPOINT",
        "CREDIT_TEST_S3_BUCKET",
        "CREDIT_TEST_S3_ACCESS_KEY_ID",
        "CREDIT_TEST_S3_SECRET_ACCESS_KEY",
        "CREDIT_TEST_QDRANT_URL",
    )
    missing = [name for name in names if not os.getenv(name)]
    if missing:
        pytest.skip("purge job stores are absent: " + ", ".join(missing))
    qdrant = urlsplit(os.environ["CREDIT_TEST_QDRANT_URL"])
    return Settings(
        credit_object_store_endpoint=os.environ["CREDIT_TEST_S3_ENDPOINT"],
        credit_object_store_bucket=os.environ["CREDIT_TEST_S3_BUCKET"],
        credit_object_store_access_key_id=os.environ["CREDIT_TEST_S3_ACCESS_KEY_ID"],
        credit_object_store_secret_access_key=os.environ[
            "CREDIT_TEST_S3_SECRET_ACCESS_KEY"
        ],
        qdrant_host=qdrant.hostname or "",
        qdrant_port=qdrant.port or 80,
        qdrant_collection_name=f"credit_g5_{uuid4().hex}",
    )


@dataclass(frozen=True)
class _Harness:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    config: Settings


@pytest.fixture(scope="module")
async def harness() -> AsyncIterator[_Harness]:
    raw = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    config = _s3()
    admin_url = _validated_admin_url(raw)
    admin = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    cluster = _Cluster(admin_url=admin_url, admin=admin, created=[])
    engine: AsyncEngine | None = None
    try:
        name = await cluster.fresh()
        await _migrate(cluster.url(name), "upgrade", "head")
        engine = create_async_engine(cluster.url(name), poolclass=NullPool)
        yield _Harness(
            engine,
            async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False),
            config,
        )
    finally:
        if engine is not None:
            await engine.dispose()
        for created in list(reversed(cluster.created)):
            await cluster.drop(created)
        await admin.dispose()
        qdrant = AsyncQdrantClient(
            url=f"http://{config.qdrant_host}:{config.qdrant_port}"
        )
        try:
            if await qdrant.collection_exists(config.qdrant_collection_name):
                await qdrant.delete_collection(config.qdrant_collection_name)
        finally:
            await qdrant.close()


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "credit_authorization_signing_key", SecretStr(KEY))


@dataclass(frozen=True)
class _World:
    builder: UUID
    tenant: UUID
    issuer: Actor
    owner: Actor
    officer: Actor
    media: UUID
    document: UUID
    copies: dict[str, StoredCopy]


async def _world(harness: _Harness) -> _World:
    config = harness.config
    world = _World(
        uuid4(),
        uuid4(),
        Actor(uuid4(), "super_admin", True),
        Actor(uuid4(), "super_admin", True),
        Actor(uuid4(), "clinic_admin", True),
        uuid4(),
        uuid4(),
        {},
    )
    document_bytes = DOCX_TEXT.encode("utf-8")
    for kind, source, data in (
        ("media", world.media, PDF),
        ("knowledge", world.document, document_bytes),
    ):
        world.copies[kind] = await asyncio.to_thread(
            put_immutable_copy,
            tenant_id=world.tenant,
            object_id=source,
            payload=data,
            expected_sha256=hashlib.sha256(data).digest(),
            config=config,
        )
    await replace_derived_chunks(
        tenant_id=world.tenant,
        source_id=world.document,
        content=DOCX_TEXT,
        config=config,
    )
    exhausted = datetime.now(UTC) - timedelta(days=40)
    async with harness.sessions() as session, session.begin():
        session.add(Builder(id=world.builder, name="Purge job builder"))
        await session.flush()
        session.add(
            Tenant(
                id=world.tenant,
                name="Purge job clinic",
                slug=f"purge-job-{world.tenant.hex}",
                api_key_hash=f"hash-{world.tenant.hex}",
                plan_type="clinic",
                builder_id=world.builder,
                config={},
            )
        )
        await session.flush()
        for actor in (world.issuer, world.owner, world.officer):
            session.add(
                User(
                    id=actor.user_id,
                    tenant_id=world.tenant,
                    email=f"{actor.user_id.hex[:12]}@example.invalid",
                    password_hash="x",
                    role=(
                        UserRole.super_admin
                        if actor.role == "super_admin"
                        else UserRole.clinic_admin
                    ),
                )
            )
        session.add(
            MediaAsset(
                id=world.media,
                tenant_id=world.tenant,
                display_name="contrato",
                original_filename="contrato.pdf",
                category="pdf",
                mime_type="application/pdf",
                size_bytes=len(PDF),
                sha256=hashlib.sha256(PDF).digest(),
                payload=PDF,
            )
        )
        session.add(
            KnowledgeDocument(
                id=world.document,
                tenant_id=world.tenant,
                title="protocolo",
                content=DOCX_TEXT,
                content_type="text",
                document_metadata={},
            )
        )
        for kind, source in (("media", world.media), ("knowledge", world.document)):
            copy = world.copies[kind]
            session.add(
                CreditObjectCopy(
                    source_kind=kind,
                    source_id=source,
                    tenant_id=world.tenant,
                    builder_id=world.builder,
                    object_key=copy.object_key,
                    version_id=copy.version_id,
                    etag=copy.etag,
                    content_sha256=copy.content_sha256,
                )
            )
        account = uuid4()
        session.add(CreditAccount(id=account, builder_id=world.builder))
        await session.flush()
        episode = uuid4()
        session.add(
            CreditLifecycleEpisode(
                id=episode,
                builder_id=world.builder,
                account_id=account,
                opened_effect_id="e",
                exhausted_at=exhausted,
                suspension_due_at=exhausted + timedelta(hours=72),
                deletion_due_at=exhausted + timedelta(days=30),
            )
        )
        await session.flush()
        for name in ("suspended", "deletion_due"):
            session.add(
                CreditLifecycleTransition(
                    id=uuid4(),
                    builder_id=world.builder,
                    episode_id=episode,
                    transition=name,
                    effect_id=f"lifecycle:{episode}:{name}",
                    occurred_at=exhausted,
                    source="worker",
                    detail={},
                )
            )
    async with harness.sessions() as session, session.begin():
        for role, actor in (
            ("production_data_lifecycle_owner", world.owner),
            ("legal_hold_officer", world.officer),
        ):
            await content_retention.grant_assignment(
                session, world.issuer, role=role, user_id=actor.user_id, reason="t"
            )
    return world


async def _authorize(
    harness: _Harness, world: _World, purpose: Any, subject: Actor, **extra: Any
) -> PresentedAuthorization:
    async with harness.sessions() as session, session.begin():
        issued = await content_retention.issue_authorization(
            session,
            world.issuer,
            purpose=purpose,
            builder_id=world.builder,
            scope="builder",
            subject_user_id=subject.user_id,
            **extra,
        )
    return PresentedAuthorization(issued.authorization_id, issued.signature)


async def _prepared(harness: _Harness, world: _World) -> tuple[PurgeExecutor, UUID]:
    operation = uuid4()
    presented = await _authorize(
        harness,
        world,
        "purge",
        world.owner,
        resource=str(operation),
        fence_generation=0,
    )
    adapter = CreditRetentionAdapter(
        harness.sessions, s3_config=harness.config, qdrant_config=harness.config
    )
    executor = PurgeExecutor(harness.sessions, adapter, config=settings)
    tokens = await executor.prepare(
        world.owner,
        operation_id=operation,
        builder_id=world.builder,
        scope="builder",
        presented=presented,
    )
    assert len(tokens) == 2
    return executor, operation


async def _items(harness: _Harness, operation: UUID) -> list[CreditRetentionTombstone]:
    async with harness.sessions() as session:
        return list(
            (
                await session.execute(
                    select(CreditRetentionTombstone).where(
                        CreditRetentionTombstone.operation_id == operation
                    )
                )
            ).scalars()
        )


def _head(config: Settings, copy: StoredCopy) -> int:
    try:
        _client(config).head_object(
            Bucket=config.credit_object_store_bucket,
            Key=copy.object_key,
            VersionId=copy.version_id,
        )
    except ClientError as error:
        return int(error.response["ResponseMetadata"]["HTTPStatusCode"])
    return 200


def _listed(config: Settings, copy: StoredCopy) -> bool:
    pages = (
        _client(config)
        .get_paginator("list_object_versions")
        .paginate(Bucket=config.credit_object_store_bucket, Prefix=copy.object_key)
    )
    return any(
        entry.get("VersionId") == copy.version_id
        for page in pages
        for entry in (*page.get("Versions", ()), *page.get("DeleteMarkers", ()))
    )


def _version_bytes(config: Settings, copy: StoredCopy) -> bytes:
    body = _client(config).get_object(
        Bucket=config.credit_object_store_bucket,
        Key=copy.object_key,
        VersionId=copy.version_id,
    )["Body"]
    return bytes(body.read())


async def _source_rows(harness: _Harness, world: _World) -> tuple[Any, Any]:
    async with harness.sessions() as session:
        media = await session.scalar(
            select(MediaAsset.payload).where(MediaAsset.id == world.media)
        )
        document = await session.scalar(
            select(KnowledgeDocument.content).where(
                KnowledgeDocument.id == world.document
            )
        )
    return media, document


async def test_tombstone_then_hold_restores_the_same_bytes_version_and_points(
    harness: _Harness,
) -> None:
    world = await _world(harness)
    executor, operation = await _prepared(harness, world)
    adapter = executor.store
    await executor.apply_tombstones(operation)
    items = {item.source_kind: item for item in await _items(harness, operation)}
    hidden = await adapter.lookup(items["knowledge"])
    assert hidden.postgresql_present and hidden.s3_version_present
    assert hidden.qdrant_points == 0  # tombstone dropped only derived points
    assert (await adapter.lookup(items["media"])).s3_version_present

    async with harness.sessions() as session:
        await content_retention.record_hold_event(
            session,
            world.officer,
            event_type="hold_requested",
            builder_id=world.builder,
            scope="builder",
            presented=await _authorize(harness, world, "hold_request", world.officer),
            reason_code="litigation",
        )
    assert await executor.fence(operation) is None
    media, document = await _source_rows(harness, world)
    assert hashlib.sha256(bytes(media)).hexdigest() == items["media"].content_sha256
    assert bytes(media) == PDF and document == DOCX_TEXT
    for kind in ("media", "knowledge"):
        copy = world.copies[kind]
        assert items[kind].s3_version_id == copy.version_id
        restored = await adapter.lookup(items[kind])
        assert restored.s3_version_present
        assert hashlib.sha256(_version_bytes(harness.config, copy)).hexdigest() == (
            items[kind].content_sha256
        )
    rebuilt = await adapter.lookup(items["knowledge"])
    assert rebuilt.qdrant_points == len(derived_point_ids(world.document, DOCX_TEXT))
    assert items["knowledge"].qdrant_point_ids == derived_point_ids(
        world.document, DOCX_TEXT
    )
    report = await executor.finish(operation)
    assert report.status == "blocked" and set(report.outcomes.values()) == {"held"}


async def test_fenced_purge_confirms_absence_in_both_stores_and_replays_safely(
    harness: _Harness,
) -> None:
    world = await _world(harness)
    executor, operation = await _prepared(harness, world)
    await executor.apply_tombstones(operation)
    assert await executor.fence(operation) is not None
    report = await executor.purge(operation)
    assert report.status == "completed"
    assert set(report.outcomes.values()) == {"deleted"}
    assert await _source_rows(harness, world) == (None, None)
    for copy in world.copies.values():
        assert _head(harness.config, copy) == 404
        assert not _listed(harness.config, copy)
    async with harness.sessions() as session:
        states = set(
            (
                await session.execute(
                    select(CreditObjectCopy.state).where(
                        CreditObjectCopy.tenant_id == world.tenant
                    )
                )
            ).scalars()
        )
    assert states == {"deleted"}
    replay = await executor.purge(operation)
    assert replay.status == "completed"


async def test_crash_between_s3_delete_and_result_converges_on_recovery(
    harness: _Harness,
) -> None:
    world = await _world(harness)
    executor, operation = await _prepared(harness, world)
    await executor.apply_tombstones(operation)
    assert await executor.fence(operation) is not None
    # The process deleted the exact S3 version, then died before the row
    # delete and before recording any result.
    await asyncio.to_thread(
        delete_exact_copy, copy=world.copies["media"], config=harness.config
    )
    assert (await executor.finish(operation)).status == "incomplete"
    assert await executor.reconcile() == 2
    report = await executor.finish(operation)
    assert report.status == "completed"
    assert await _source_rows(harness, world) == (None, None)


async def test_without_a_valid_fence_nothing_is_deleted(harness: _Harness) -> None:
    world = await _world(harness)
    executor, operation = await _prepared(harness, world)
    await executor.apply_tombstones(operation)
    adapter = executor.store
    # Stale: no fence for the generation this tombstone observed.
    for item in await _items(harness, operation):
        assert await adapter.purge(item) == "blocked"
    media, document = await _source_rows(harness, world)
    assert bytes(media) == PDF and document == DOCX_TEXT
    for copy in world.copies.values():
        assert _head(harness.config, copy) == 200
    # New fence: the same items now purge.
    assert await executor.fence(operation) is not None
    assert (await executor.purge(operation)).status == "completed"


async def test_legal_hold_on_the_s3_version_is_held_and_keeps_the_row(
    harness: _Harness,
) -> None:
    world = await _world(harness)
    executor, operation = await _prepared(harness, world)
    copy = world.copies["media"]
    set_legal_hold(copy=copy, enabled=True, config=harness.config)
    try:
        await executor.apply_tombstones(operation)
        assert await executor.fence(operation) is not None
        report = await executor.purge(operation)
        assert report.status == "partial"
        outcomes = {
            item.source_kind: report.outcomes[item.token]
            for item in await _items(harness, operation)
        }
        assert outcomes == {"media": "held", "knowledge": "deleted"}
        media, document = await _source_rows(harness, world)
        assert bytes(media) == PDF and document is None
        assert _version_bytes(harness.config, copy) == PDF
    finally:
        set_legal_hold(copy=copy, enabled=False, config=harness.config)
        await asyncio.to_thread(delete_exact_copy, copy=copy, config=harness.config)


async def test_changed_source_bytes_block_before_the_s3_version_is_deleted(
    harness: _Harness,
) -> None:
    """The PostgreSQL bytes are re-verified BEFORE the exact S3 version is
    deleted: an object rewritten after its tombstone is ``blocked`` with its
    row AND its S3 version intact (nothing irreversible happened)."""

    world = await _world(harness)
    executor, operation = await _prepared(harness, world)
    await executor.apply_tombstones(operation)
    assert await executor.fence(operation) is not None
    changed = PDF + b"\n% rewritten after the tombstone"
    async with harness.sessions() as session, session.begin():
        await session.execute(
            update(MediaAsset)
            .where(MediaAsset.id == world.media)
            .values(
                payload=changed,
                size_bytes=len(changed),
                sha256=hashlib.sha256(changed).digest(),
            )
        )
    [media] = [i for i in await _items(harness, operation) if i.source_kind == "media"]
    assert await executor.store.purge(media) == "blocked"
    copy = world.copies["media"]
    assert _head(harness.config, copy) == 200
    assert _version_bytes(harness.config, copy) == PDF
    stored, _document = await _source_rows(harness, world)
    assert bytes(stored) == changed
    await asyncio.to_thread(delete_exact_copy, copy=copy, config=harness.config)
