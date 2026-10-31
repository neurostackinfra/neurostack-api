"""PostgreSQL selection logic of Group 4b's copy collection and legal hold.

The S3 side is a recording in-memory fake (an S3-compatible server with
Object Lock is proven separately, env-gated, by
``tests/integration/test_credit_object_copy_gc.py`` and
``tests/integration/test_credit_storage_s3.py``).  What runs here for real is
the PostgreSQL schema at ``head``, the orphan scan's SQL, the worker's job
claims and the real legal-hold service writing ``credit_hold_events``:

* an orphan left by a CASCADE (clinic delete) is collected in one run even
  behind more than one batch of older live copies;
* a credit legal hold in PostgreSQL protects the S3 copy: the collection
  marks the copy and the job ``held`` and never deletes the version, and the
  hold sets ``PutObjectLegalHold=ON`` on the registered version;
* on release the S3 legal hold goes ``OFF`` and a ``held`` copy whose source
  is gone is collected.

Needs ``POSTGRES_SMOKE_DATABASE_URL``; without it the module skips, and only
``scripts/check-credit-integration.sh`` turns that skip into a failure.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from botocore.exceptions import EndpointConnectionError
from pydantic import SecretStr
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.config.settings import settings
from src.models.builder import Builder
from src.models.credit_object_copy import CreditObjectCopy, CreditObjectCopyJob
from src.models.knowledge import KnowledgeDocument
from src.models.media_asset import MediaAsset
from src.models.tenant import Tenant
from src.models.user import User, UserRole
from src.services import content_retention, credit_inventory
from src.services.content_retention import Actor, PresentedAuthorization
from src.storage import credit_object_store
from src.storage.credit_object_store import (
    ObjectCopyError,
    ObjectCopyHeld,
    StoredCopy,
    copy_object_key,
)
from tests.integration.test_asaas_billing_postgres import _validated_admin_url
from tests.integration.test_migration_036_postgres import _Cluster, _migrate

pytestmark = pytest.mark.integration

UTC = timezone.utc
KEY = "credit-authorization-signing-key-for-copy-gc-0123456789"


@dataclass(frozen=True)
class _Database:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]


@pytest.fixture(scope="module")
async def database() -> AsyncIterator[_Database]:
    raw = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
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
        yield _Database(engine, async_sessionmaker(engine, expire_on_commit=False))
    finally:
        if engine is not None:
            await engine.dispose()
        for created in list(reversed(cluster.created)):
            await cluster.drop(created)
        await admin.dispose()


@dataclass
class FakeObjectStore:
    """Exact-version store: ``versions[(key, version)]`` is the legal hold."""

    versions: dict[tuple[str, str], bool] = field(default_factory=dict)
    calls: list[tuple[str, str, Any]] = field(default_factory=list)
    # Injected faults: raised by an ON (``hold_on_error``; after storing ON
    # when ``hold_on_after`` is set), and by delete of a given version id.
    hold_on_error: BaseException | None = None
    hold_on_after: bool = False
    delete_errors: dict[str, BaseException] = field(default_factory=dict)
    after_delete: Any = None

    def put(
        self,
        *,
        tenant_id: UUID,
        object_id: UUID,
        payload: bytes,
        expected_sha256: bytes,
        **_: Any,
    ) -> StoredCopy:
        digest = hashlib.sha256(payload).digest()
        assert digest == expected_sha256
        key = copy_object_key(tenant_id, object_id, digest.hex())
        version = uuid4().hex
        self.versions[(key, version)] = False
        self.calls.append(("put", key, version))
        return StoredCopy(key, version, "etag", digest.hex())

    def delete(self, *, copy: StoredCopy, **_: Any) -> None:
        self.calls.append(("delete", copy.object_key, copy.version_id))
        if copy.version_id in self.delete_errors:
            raise self.delete_errors[copy.version_id]
        if self.versions.get((copy.object_key, copy.version_id)):
            raise ObjectCopyHeld("S3 object version is under legal hold")
        self.versions.pop((copy.object_key, copy.version_id), None)
        if self.after_delete is not None:
            self.after_delete()

    def set_hold(self, *, copy: StoredCopy, enabled: bool, **_: Any) -> None:
        self.calls.append(("hold", copy.version_id, enabled))
        if (copy.object_key, copy.version_id) not in self.versions:
            raise ObjectCopyError("no such version")
        if enabled and self.hold_on_error is not None and not self.hold_on_after:
            raise self.hold_on_error
        self.versions[(copy.object_key, copy.version_id)] = enabled
        if enabled and self.hold_on_error is not None and self.hold_on_after:
            raise self.hold_on_error

    def holds_set(self, version_id: str, enabled: bool) -> int:
        return self.calls.count(("hold", version_id, enabled))


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeObjectStore:
    fake = FakeObjectStore()
    monkeypatch.setattr(credit_object_store, "put_immutable_copy", fake.put)
    monkeypatch.setattr(credit_object_store, "delete_exact_copy", fake.delete)
    monkeypatch.setattr(credit_object_store, "set_legal_hold", fake.set_hold)
    return fake


@pytest.fixture(autouse=True)
def _effective_copy_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """The REAL gate, made effective: switch on, ``ENVIRONMENT=test`` and the
    four S3 settings filled (the store itself is the fake above)."""

    monkeypatch.setattr(settings, "environment", "test")
    monkeypatch.setattr(settings, "credit_object_store_endpoint", "http://fake")
    monkeypatch.setattr(settings, "credit_object_store_bucket", "fake-bucket")
    monkeypatch.setattr(settings, "credit_object_store_access_key_id", "fake")
    monkeypatch.setattr(
        settings, "credit_object_store_secret_access_key", SecretStr("fake")
    )
    monkeypatch.setattr(settings, "credit_authorization_signing_key", SecretStr(KEY))
    monkeypatch.setattr(credit_inventory, "credit_switch_effective", lambda: True)
    # Qdrant has its own env-gated integration (test_credit_qdrant_rebuild).
    monkeypatch.setattr(credit_inventory, "qdrant_runtime_enabled", lambda *_: False)


@dataclass(frozen=True)
class _World:
    builder: UUID
    tenant: UUID
    issuer: Actor
    officer: Actor
    releaser: Actor


async def _world(database: _Database) -> _World:
    world = _World(
        uuid4(),
        uuid4(),
        Actor(uuid4(), "super_admin", True),
        Actor(uuid4(), "clinic_admin", True),
        Actor(uuid4(), "clinic_admin", True),
    )
    async with database.sessions() as session, session.begin():
        session.add(Builder(id=world.builder, name="Copy GC builder"))
        await session.flush()
        session.add(
            Tenant(
                id=world.tenant,
                name="Copy GC clinic",
                slug=f"copy-gc-{world.tenant.hex}",
                api_key_hash=f"hash-{world.tenant.hex}",
                builder_id=world.builder,
                config={},
            )
        )
        await session.flush()
        for actor in (world.issuer, world.officer, world.releaser):
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
    async with database.sessions() as session, session.begin():
        for role, actor in (
            ("legal_hold_officer", world.officer),
            ("legal_hold_release_officer", world.releaser),
        ):
            await content_retention.grant_assignment(
                session, world.issuer, role=role, user_id=actor.user_id, reason="t"
            )
    return world


async def _media(database: _Database, tenant_id: UUID, label: str) -> UUID:
    media_id = uuid4()
    payload = f"copy-gc-{label}-{media_id}".encode()
    async with database.sessions() as session, session.begin():
        session.add(
            MediaAsset(
                id=media_id,
                tenant_id=tenant_id,
                display_name=label,
                original_filename=f"{label}.pdf",
                category="pdf",
                mime_type="application/pdf",
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).digest(),
                payload=payload,
            )
        )
    return media_id


async def _copied(database: _Database, world: _World, label: str) -> UUID:
    """A media source plus its registered copy, through the real worker."""

    media_id = await _media(database, world.tenant, label)
    async with database.sessions() as session:
        await credit_inventory.enqueue_object_copy(
            session, source_kind="media", source_id=media_id, tenant_id=world.tenant
        )
    await credit_inventory.run_credit_object_copy_once(database.sessions)
    return media_id


async def _copy_of(database: _Database, source_id: UUID) -> CreditObjectCopy:
    async with database.sessions() as session:
        copy = await session.scalar(
            select(CreditObjectCopy).where(CreditObjectCopy.source_id == source_id)
        )
    assert copy is not None
    return copy


async def _gc_states(database: _Database, source_id: UUID) -> list[str]:
    async with database.sessions() as session:
        return list(
            (
                await session.execute(
                    select(CreditObjectCopyJob.state).where(
                        CreditObjectCopyJob.source_id == source_id,
                        CreditObjectCopyJob.operation == "gc",
                    )
                )
            ).scalars()
        )


async def _hold_event(
    database: _Database,
    world: _World,
    event_type: Any,
    actor: Actor,
    *,
    intent_id: UUID | None = None,
) -> UUID:
    purpose = content_retention._EVENT_PURPOSE[event_type]
    async with database.sessions() as session, session.begin():
        issued = await content_retention.issue_authorization(
            session,
            world.issuer,
            purpose=purpose,
            builder_id=world.builder,
            scope="builder",
            subject_user_id=actor.user_id,
            resource=None if intent_id is None else str(intent_id),
        )
    async with database.sessions() as session:
        result = await content_retention.record_hold_event(
            session,
            actor,
            event_type=event_type,
            builder_id=world.builder,
            scope="builder",
            presented=PresentedAuthorization(issued.authorization_id, issued.signature),
            reason_code="litigation",
            intent_id=intent_id,
            justification="released by counsel" if intent_id else None,
        )
    return result.intent_id


async def _delete_media(database: _Database, world: _World, media_id: UUID) -> None:
    """``DELETE /v1/media-assets/{id}``: authoritative delete, then the queue."""

    async with database.sessions() as session:
        await session.execute(delete(MediaAsset).where(MediaAsset.id == media_id))
        await session.commit()
        await credit_inventory.enqueue_source_collection(
            session, source_kind="media", source_id=media_id, tenant_id=world.tenant
        )


# ---------------------------------------------------------------------------
# S1: the orphan scan selects in SQL.
# ---------------------------------------------------------------------------


async def test_a_cascade_orphan_behind_more_than_a_batch_of_live_copies_is_collected(
    database: _Database, store: FakeObjectStore
) -> None:
    live_world = await _world(database)
    doomed_world = await _world(database)
    batch = credit_inventory._COPY_BATCH_SIZE
    live = [await _copied(database, live_world, f"live-{i}") for i in range(batch + 1)]
    orphan = await _copied(database, doomed_world, "orphan")
    # The live copies are strictly OLDER than the orphan: an oldest-first
    # scan of `ready` rows never reaches it.
    base = datetime.now(UTC) - timedelta(days=1)
    async with database.sessions() as session, session.begin():
        for index, source_id in enumerate(live):
            copy = await session.scalar(
                select(CreditObjectCopy).where(CreditObjectCopy.source_id == source_id)
            )
            assert copy is not None and copy.state == "ready"
            copy.created_at = base + timedelta(seconds=index)
    orphan_copy = await _copy_of(database, orphan)
    assert orphan_copy.state == "ready"

    # DELETE /clinics/{id}: the CASCADE removes media_assets, no job queued.
    async with database.sessions() as session, session.begin():
        await session.execute(delete(Tenant).where(Tenant.id == doomed_world.tenant))
    assert await _gc_states(database, orphan) == []

    await credit_inventory.run_credit_object_copy_once(database.sessions)

    collected = await _copy_of(database, orphan)
    assert collected.state == "deleted"
    assert (collected.object_key, collected.version_id) not in store.versions
    assert await _gc_states(database, orphan) == ["completed"]
    for source_id in live:
        copy = await _copy_of(database, source_id)
        assert copy.state == "ready"
        assert (copy.object_key, copy.version_id) in store.versions
        assert await _gc_states(database, source_id) == []


# ---------------------------------------------------------------------------
# S2/S3: the credit legal hold protects and releases the S3 copy.
# ---------------------------------------------------------------------------


async def test_a_postgres_hold_keeps_the_version_and_release_collects_it(
    database: _Database, store: FakeObjectStore
) -> None:
    world = await _world(database)
    media_id = await _copied(database, world, "held")
    copy = await _copy_of(database, media_id)
    version = (copy.object_key, copy.version_id)
    assert store.versions[version] is False

    intent = await _hold_event(database, world, "hold_requested", world.officer)
    await _hold_event(
        database, world, "hold_activated", world.officer, intent_id=intent
    )
    await _delete_media(database, world, media_id)

    await credit_inventory.run_credit_object_copy_once(database.sessions)

    # The version survives, PutObjectLegalHold=ON is set on it, and both
    # the copy and its collection job record `held`.
    assert version in store.versions
    assert store.versions[version] is True
    assert ("delete", *version) not in store.calls
    assert (await _copy_of(database, media_id)).state == "held"
    assert await _gc_states(database, media_id) == ["held"]

    # Still held: another run deletes nothing and queues nothing new.
    await credit_inventory.run_credit_object_copy_once(database.sessions)
    assert store.versions[version] is True
    assert await _gc_states(database, media_id) == ["held"]

    await _hold_event(
        database, world, "hold_released", world.releaser, intent_id=intent
    )
    await credit_inventory.run_credit_object_copy_once(database.sessions)

    assert ("hold", copy.version_id, False) in store.calls
    assert version not in store.versions
    assert (await _copy_of(database, media_id)).state == "deleted"
    assert await _gc_states(database, media_id) == ["completed"]


async def test_a_hold_sets_the_s3_legal_hold_on_a_live_copy_and_release_clears_it(
    database: _Database, store: FakeObjectStore
) -> None:
    world = await _world(database)
    media_id = await _copied(database, world, "live-held")
    copy = await _copy_of(database, media_id)
    version = (copy.object_key, copy.version_id)

    intent = await _hold_event(database, world, "hold_requested", world.officer)
    await _hold_event(
        database, world, "hold_activated", world.officer, intent_id=intent
    )
    await credit_inventory.run_credit_object_copy_once(database.sessions)
    assert store.versions[version] is True
    assert (await _copy_of(database, media_id)).state == "held"

    await _hold_event(
        database, world, "hold_released", world.releaser, intent_id=intent
    )
    await credit_inventory.run_credit_object_copy_once(database.sessions)
    # The source still exists: the copy is ready again, never collected.
    assert store.versions[version] is False
    assert (await _copy_of(database, media_id)).state == "ready"
    assert await _gc_states(database, media_id) == []


async def test_an_s3_hold_in_a_scope_no_credit_hold_covered_is_left_alone(
    database: _Database, store: FakeObjectStore
) -> None:
    """No credit hold ever covered this builder or tenant: the worker sends
    no OFF and the copy stays ``held``.  (The S3 flag has no owner: once a
    released credit hold covers the scope, the release OFF lifts it.)"""

    world = await _world(database)
    media_id = await _copied(database, world, "s3-only")
    copy = await _copy_of(database, media_id)
    store.versions[(copy.object_key, copy.version_id)] = True
    await _delete_media(database, world, media_id)

    await credit_inventory.run_credit_object_copy_once(database.sessions)
    await credit_inventory.run_credit_object_copy_once(database.sessions)

    assert store.versions[(copy.object_key, copy.version_id)] is True
    assert ("hold", copy.version_id, False) not in store.calls
    assert (await _copy_of(database, media_id)).state == "held"
    assert await _gc_states(database, media_id) == ["held"]


async def test_a_held_copy_orphaned_by_a_cascade_is_collected_after_release(
    database: _Database, store: FakeObjectStore
) -> None:
    """Held while live, then its clinic is deleted (no job is queued by a
    CASCADE): the release makes it ``ready`` and the orphan scan collects."""

    world = await _world(database)
    clinic = uuid4()
    async with database.sessions() as session, session.begin():
        session.add(
            Tenant(
                id=clinic,
                name="Second clinic",
                slug=f"copy-gc-{clinic.hex}",
                api_key_hash=f"hash-{clinic.hex}",
                builder_id=world.builder,
                config={},
            )
        )
    media_id = await _media(database, clinic, "cascade-held")
    async with database.sessions() as session:
        await credit_inventory.enqueue_object_copy(
            session, source_kind="media", source_id=media_id, tenant_id=clinic
        )
    await credit_inventory.run_credit_object_copy_once(database.sessions)
    copy = await _copy_of(database, media_id)
    version = (copy.object_key, copy.version_id)

    intent = await _hold_event(database, world, "hold_requested", world.officer)
    await credit_inventory.run_credit_object_copy_once(database.sessions)
    assert store.versions[version] is True
    assert (await _copy_of(database, media_id)).state == "held"

    async with database.sessions() as session, session.begin():
        await session.execute(delete(Tenant).where(Tenant.id == clinic))
    await credit_inventory.run_credit_object_copy_once(database.sessions)
    assert version in store.versions
    assert await _gc_states(database, media_id) == []

    await _hold_event(
        database, world, "hold_activated", world.officer, intent_id=intent
    )
    await _hold_event(
        database, world, "hold_released", world.releaser, intent_id=intent
    )
    await credit_inventory.run_credit_object_copy_once(database.sessions)

    assert version not in store.versions
    assert (await _copy_of(database, media_id)).state == "deleted"
    assert await _gc_states(database, media_id) == ["completed"]


# ---------------------------------------------------------------------------
# Review loop 1: failure paths, races and the other source/delete paths.
# ---------------------------------------------------------------------------


async def _hold_builder(database: _Database, world: _World) -> UUID:
    intent = await _hold_event(database, world, "hold_requested", world.officer)
    await _hold_event(
        database, world, "hold_activated", world.officer, intent_id=intent
    )
    return intent


async def _release(database: _Database, world: _World, intent: UUID) -> None:
    await _hold_event(
        database, world, "hold_released", world.releaser, intent_id=intent
    )


async def _job_rows(database: _Database, source_id: UUID) -> list[tuple[str, str]]:
    async with database.sessions() as session:
        return sorted(
            (str(row.operation), str(row.state))
            for row in (
                await session.execute(
                    select(CreditObjectCopyJob).where(
                        CreditObjectCopyJob.source_id == source_id
                    )
                )
            ).scalars()
        )


async def test_g3_a_failing_hold_on_until_release_is_still_collected(
    database: _Database, store: FakeObjectStore
) -> None:
    world = await _world(database)
    media_id = await _copied(database, world, "on-fails")
    copy = await _copy_of(database, media_id)
    store.hold_on_error = ObjectCopyError("S3 down")
    intent = await _hold_builder(database, world)
    await _delete_media(database, world, media_id)
    for _ in range(2):
        await credit_inventory.run_credit_object_copy_once(database.sessions)
    # The ON keeps failing: the copy stays `ready` (S3 unprotected, but the
    # PostgreSQL hold stops the collector) and its job `held`.
    assert (copy.object_key, copy.version_id) in store.versions
    assert (await _copy_of(database, media_id)).state == "ready"
    assert await _gc_states(database, media_id) == ["held"]

    await _release(database, world, intent)
    await credit_inventory.run_credit_object_copy_once(database.sessions)
    assert (copy.object_key, copy.version_id) not in store.versions
    assert (await _copy_of(database, media_id)).state == "deleted"
    assert await _gc_states(database, media_id) == ["completed"]


async def test_g4_the_requeue_never_resets_a_job_another_worker_claimed(
    database: _Database, store: FakeObjectStore
) -> None:
    source_id = uuid4()
    job_id = uuid4()
    async with database.sessions() as session, session.begin():
        session.add(
            CreditObjectCopyJob(
                id=job_id,
                source_kind="media",
                source_id=source_id,
                tenant_id=uuid4(),
                operation="gc",
                state="held",
            )
        )
    async with database.sessions() as session:
        scalar = session.scalar
        calls = 0

        async def racing_scalar(statement: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            result = await scalar(statement, *args, **kwargs)
            if calls == 2:
                # After this pass read "held, nothing active" and before its
                # write, another worker claims the row.
                async with database.sessions() as other, other.begin():
                    await other.execute(
                        update(CreditObjectCopyJob)
                        .where(CreditObjectCopyJob.id == job_id)
                        .values(state="running", claimed_at=datetime.now(UTC))
                    )
            return result

        session.scalar = racing_scalar  # type: ignore[method-assign]
        await credit_inventory._requeue_held_collection(session, "media", source_id)
        await session.commit()
    async with database.sessions() as session:
        assert (
            await session.scalar(
                select(CreditObjectCopyJob.state).where(
                    CreditObjectCopyJob.id == job_id
                )
            )
            == "running"
        )


async def test_g5_a_failing_reconcile_does_not_stop_the_pass(
    database: _Database, store: FakeObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(database)
    media_id = await _copied(database, world, "reconcile-fails")

    async def broken(_factory: Any) -> None:
        raise EndpointConnectionError(endpoint_url="http://fake")

    monkeypatch.setattr(credit_inventory, "_reconcile_copy_legal_holds", broken)
    await _delete_media(database, world, media_id)
    await credit_inventory.run_credit_object_copy_once(database.sessions)
    assert (await _copy_of(database, media_id)).state == "deleted"
    assert await _gc_states(database, media_id) == ["completed"]


async def test_g5_a_botocore_error_on_hold_is_an_alert_not_an_abort(
    database: _Database, store: FakeObjectStore
) -> None:
    world = await _world(database)
    live = await _copied(database, world, "boto-live")
    other = await _copied(database, await _world(database), "boto-other")
    store.hold_on_error = EndpointConnectionError(endpoint_url="http://fake")
    await _hold_builder(database, world)
    async with database.sessions() as session:
        await session.execute(delete(MediaAsset).where(MediaAsset.id == other))
        await session.commit()
    await credit_inventory.run_credit_object_copy_once(database.sessions)
    assert (await _copy_of(database, live)).state == "ready"  # retried later
    assert (await _copy_of(database, other)).state == "deleted"


async def test_g6_a_crash_after_the_s3_hold_on_is_still_released(
    database: _Database, store: FakeObjectStore
) -> None:
    world = await _world(database)
    media_id = await _copied(database, world, "crash")
    copy = await _copy_of(database, media_id)
    store.hold_on_error = RuntimeError("process died")
    store.hold_on_after = True  # ON is stored, then the "crash"
    intent = await _hold_builder(database, world)
    await credit_inventory.run_credit_object_copy_once(database.sessions)
    assert store.versions[(copy.object_key, copy.version_id)] is True
    assert (await _copy_of(database, media_id)).state == "held"

    store.hold_on_error = None
    await _release(database, world, intent)
    await credit_inventory.run_credit_object_copy_once(database.sessions)
    assert store.versions[(copy.object_key, copy.version_id)] is False
    assert (await _copy_of(database, media_id)).state == "ready"


async def _extra_copy(
    database: _Database, source_id: UUID, tenant_id: UUID, builder_id: UUID | None
) -> CreditObjectCopy:
    """A second registry row (other bytes) for the same source."""

    digest = hashlib.sha256(uuid4().bytes).hexdigest()
    key = copy_object_key(tenant_id, source_id, digest)
    version = uuid4().hex
    async with database.sessions() as session, session.begin():
        row = CreditObjectCopy(
            source_kind="media",
            source_id=source_id,
            tenant_id=tenant_id,
            builder_id=builder_id,
            object_key=key,
            version_id=version,
            etag="etag",
            content_sha256=digest,
        )
        session.add(row)
    return row


async def test_g7_a_hold_requested_mid_batch_stops_the_next_delete(
    database: _Database, store: FakeObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(database)
    media_id = await _copied(database, world, "mid-batch")
    first = await _copy_of(database, media_id)
    second = await _extra_copy(database, media_id, world.tenant, world.builder)
    store.versions[(second.object_key, second.version_id)] = False
    hold_now = False

    def hold_arrives() -> None:
        nonlocal hold_now
        hold_now = True

    async def under_hold(_session: Any, _copy: Any) -> bool:
        return hold_now

    store.after_delete = hold_arrives
    monkeypatch.setattr(credit_inventory, "_copy_under_credit_hold", under_hold)
    await _delete_media(database, world, media_id)
    await credit_inventory.run_credit_object_copy_once(database.sessions)

    deleted = [c for c in store.calls if c[0] == "delete"]
    assert len(deleted) == 1  # the second copy was re-checked and held
    survivors = {
        c.id: c.state
        for c in [
            await _copy_by_id(database, first.id),
            await _copy_by_id(database, second.id),
        ]
    }
    assert sorted(survivors.values()) == ["deleted", "held"]
    assert await _gc_states(database, media_id) == ["held"]


async def _copy_by_id(database: _Database, copy_id: UUID) -> CreditObjectCopy:
    async with database.sessions() as session:
        copy = await session.get(CreditObjectCopy, copy_id)
    assert copy is not None
    return copy


async def test_g8_the_collector_only_touches_the_jobs_tenant(
    database: _Database, store: FakeObjectStore
) -> None:
    world = await _world(database)
    stranger = await _world(database)
    media_id = await _copied(database, world, "tenant-scope")
    foreign = await _extra_copy(database, media_id, stranger.tenant, stranger.builder)
    store.versions[(foreign.object_key, foreign.version_id)] = False
    await _delete_media(database, world, media_id)
    await credit_inventory.run_credit_object_copy_once(database.sessions)

    assert (await _copy_by_id(database, foreign.id)).state == "ready"
    assert ("delete", foreign.object_key, foreign.version_id) not in store.calls


async def test_g9_a_knowledge_source_delete_collects_its_copy(
    database: _Database, store: FakeObjectStore
) -> None:
    world = await _world(database)
    document_id = uuid4()
    async with database.sessions() as session, session.begin():
        session.add(
            KnowledgeDocument(
                id=document_id,
                tenant_id=world.tenant,
                title="Protocolo",
                content="conteudo de conhecimento",
                content_type="text",
                document_metadata={},
                is_indexed=False,
                chunk_count=0,
            )
        )
    async with database.sessions() as session:
        await credit_inventory.enqueue_object_copy(
            session,
            source_kind="knowledge",
            source_id=document_id,
            tenant_id=world.tenant,
        )
    await credit_inventory.run_credit_object_copy_once(database.sessions)
    copy = await _copy_of(database, document_id)
    assert copy.source_kind == "knowledge" and copy.state == "ready"

    async with database.sessions() as session:
        await session.execute(
            delete(KnowledgeDocument).where(KnowledgeDocument.id == document_id)
        )
        await session.commit()
        await credit_inventory.enqueue_source_collection(
            session,
            source_kind="knowledge",
            source_id=document_id,
            tenant_id=world.tenant,
        )
    await credit_inventory.run_credit_object_copy_once(database.sessions)
    assert (copy.object_key, copy.version_id) not in store.versions
    assert (await _copy_of(database, document_id)).state == "deleted"
    assert await _gc_states(database, document_id) == ["completed"]


async def test_g9_a_builder_delete_cascade_collects_its_copies(
    database: _Database, store: FakeObjectStore
) -> None:
    """``DELETE /builders/{id}``: its own tenant, then the builder record."""

    builder_id, tenant_id = uuid4(), uuid4()
    async with database.sessions() as session, session.begin():
        session.add(Builder(id=builder_id, name="Doomed builder"))
        await session.flush()
        session.add(
            Tenant(
                id=tenant_id,
                name="Doomed clinic",
                slug=f"doomed-{tenant_id.hex}",
                api_key_hash=f"hash-{tenant_id.hex}",
                builder_id=builder_id,
                config={},
            )
        )
    media_id = await _media(database, tenant_id, "builder-cascade")
    async with database.sessions() as session:
        await credit_inventory.enqueue_object_copy(
            session, source_kind="media", source_id=media_id, tenant_id=tenant_id
        )
    await credit_inventory.run_credit_object_copy_once(database.sessions)
    copy = await _copy_of(database, media_id)

    async with database.sessions() as session, session.begin():
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await session.execute(delete(Builder).where(Builder.id == builder_id))
    await credit_inventory.run_credit_object_copy_once(database.sessions)
    assert (copy.object_key, copy.version_id) not in store.versions
    assert (await _copy_of(database, media_id)).state == "deleted"


async def test_g9_an_unreadable_hold_state_deletes_nothing(
    database: _Database, store: FakeObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(database)
    media_id = await _copied(database, world, "fail-closed")
    copy = await _copy_of(database, media_id)

    async def unreadable(_session: Any, _copy: Any) -> bool:
        raise RuntimeError("hold events unreadable")

    monkeypatch.setattr(credit_inventory, "_copy_under_credit_hold", unreadable)
    await _delete_media(database, world, media_id)
    await credit_inventory.run_credit_object_copy_once(database.sessions)
    assert (copy.object_key, copy.version_id) in store.versions
    assert not [c for c in store.calls if c[0] == "delete"]
    assert (await _copy_of(database, media_id)).state == "ready"
    assert await _gc_states(database, media_id) == ["failed"]


async def test_g9_two_concurrent_reconcile_passes_change_a_copy_once(
    database: _Database, store: FakeObjectStore
) -> None:
    world = await _world(database)
    media_id = await _copied(database, world, "concurrent")
    copy = await _copy_of(database, media_id)
    await _hold_builder(database, world)

    results = await asyncio.gather(
        credit_inventory._reconcile_copy_legal_holds(database.sessions),
        credit_inventory._reconcile_copy_legal_holds(database.sessions),
        return_exceptions=True,
    )
    assert results == [None, None]
    assert store.holds_set(copy.version_id, True) == 1
    assert (await _copy_of(database, media_id)).state == "held"


# ---------------------------------------------------------------------------
# S7: one outcome row per (source, operation, state).
# ---------------------------------------------------------------------------


async def _seed_job(
    database: _Database, source_id: UUID, tenant_id: UUID, state: str
) -> UUID:
    job_id = uuid4()
    async with database.sessions() as session, session.begin():
        session.add(
            CreditObjectCopyJob(
                id=job_id,
                source_kind="media",
                source_id=source_id,
                tenant_id=tenant_id,
                operation="gc",
                state=state,
                last_error="earlier",
            )
        )
    return job_id


async def test_s7_a_second_completed_gc_of_one_source_reuses_the_outcome_row(
    database: _Database, store: FakeObjectStore
) -> None:
    world = await _world(database)
    media_id = await _copied(database, world, "twice-completed")
    earlier = await _seed_job(database, media_id, world.tenant, "completed")
    await _delete_media(database, world, media_id)
    await credit_inventory.run_credit_object_copy_once(database.sessions)

    assert (await _copy_of(database, media_id)).state == "deleted"
    assert await _job_rows(database, media_id) == [
        ("copy", "completed"),
        ("gc", "completed"),
    ]
    async with database.sessions() as session:
        row = await session.get(CreditObjectCopyJob, earlier)
    assert row is not None and row.last_error is None


async def test_s7_a_second_failed_gc_of_one_source_reuses_the_outcome_row(
    database: _Database, store: FakeObjectStore
) -> None:
    world = await _world(database)
    media_id = await _copied(database, world, "twice-failed")
    copy = await _copy_of(database, media_id)
    earlier = await _seed_job(database, media_id, world.tenant, "failed")
    store.delete_errors[copy.version_id] = ObjectCopyError("S3 delete failed")
    await _delete_media(database, world, media_id)
    await credit_inventory.run_credit_object_copy_once(database.sessions)

    assert await _job_rows(database, media_id) == [
        ("copy", "completed"),
        ("gc", "failed"),
    ]
    async with database.sessions() as session:
        row = await session.get(CreditObjectCopyJob, earlier)
    assert row is not None and row.last_error == "S3 delete failed"


async def test_s7_the_batch_continues_past_a_job_that_raises(
    database: _Database, store: FakeObjectStore
) -> None:
    world = await _world(database)
    broken = await _copied(database, world, "raises")
    healthy = await _copied(database, world, "healthy")
    broken_copy = await _copy_of(database, broken)
    store.delete_errors[broken_copy.version_id] = RuntimeError("unexpected")
    await _delete_media(database, world, broken)
    await _delete_media(database, world, healthy)
    await credit_inventory.run_credit_object_copy_once(database.sessions)

    assert (await _copy_of(database, healthy)).state == "deleted"
    assert await _gc_states(database, healthy) == ["completed"]
    # The raising job is left `running` for the lease, never lost.
    assert await _gc_states(database, broken) == ["running"]
    assert (await _copy_of(database, broken)).state == "ready"
