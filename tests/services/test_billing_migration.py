"""Pure boundary tests for the legacy subscription migration state machine."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.services.billing_migration import (
    BillingMigrationRejected,
    MigrationTarget,
    migration_window,
    provider_evidence_digest,
    provider_evidence_signature,
    validate_provider_evidence_envelope,
    validate_target,
)

_PROVIDER_PRIVATE_KEY = bytes(range(1, 33))
_PROVIDER_PUBLIC_KEY = (
    Ed25519PrivateKey.from_private_bytes(_PROVIDER_PRIVATE_KEY)
    .public_key()
    .public_bytes_raw()
)


def _target(**overrides: object) -> MigrationTarget:
    snapshot: dict[str, object] = {
        "catalog_version_id": "catalog-v1",
        "offer_id": "plan-v1",
        "plan": "plus",
        "billing_cycle": "monthly",
        "coupon_code": None,
        "amount_cents": 99900,
        "credit_grant_mc": 5000,
        "included_clinics": 1,
        "additional_clinics": 0,
        "lines": [],
    }
    values: dict[str, object] = {
        "catalog_version_id": "catalog-v1",
        "offer_id": "plan-v1",
        "plan": "plus",
        "coupon_code": None,
        "amount_cents": 99900,
        "snapshot": snapshot,
    }
    values.update(overrides)
    return MigrationTarget(**values)  # type: ignore[arg-type]


def test_sao_paulo_window_is_t_t_minus_7_t_minus_2() -> None:
    window = migration_window(date(2026, 10, 12))

    assert window.renewal_date == date(2026, 10, 12)
    assert window.open_by_date == date(2026, 10, 5)
    assert window.cutover_by_date == date(2026, 10, 10)


def test_timestamp_window_uses_sao_paulo_not_host_timezone() -> None:
    # 02:30 UTC is still the previous commercial date in São Paulo.
    window = migration_window(datetime(2026, 10, 12, 2, 30, tzinfo=timezone.utc))

    assert window.renewal_date == date(2026, 10, 11)
    assert window.open_by_date == date(2026, 10, 4)
    assert window.cutover_by_date == date(2026, 10, 9)


def test_naive_renewal_is_rejected() -> None:
    with pytest.raises(BillingMigrationRejected, match="renewal_invalid"):
        migration_window(datetime(2026, 10, 12, 12, 0))


@pytest.mark.parametrize(
    "snapshot",
    [
        {"email": "not-allowed@example.test"},
        {"catalog_version_id": "catalog-v1"},
        {
            "catalog_version_id": "catalog-v1",
            "offer_id": "plan-v1",
            "plan": "plus",
            "billing_cycle": "monthly",
            "coupon_code": None,
            "amount_cents": 99900,
            "credit_grant_mc": 5000,
            "included_clinics": 1,
            "additional_clinics": 0,
            "lines": [],
            "extra": "not-allowed",
        },
    ],
)
def test_target_snapshot_is_closed_and_pii_free(snapshot: dict[str, object]) -> None:
    with pytest.raises(BillingMigrationRejected, match="snapshot_invalid"):
        validate_target(_target(snapshot=snapshot))


def test_target_snapshot_is_copied_before_retry() -> None:
    target = _target()
    copied = validate_target(target)
    target.snapshot["plan"] = "mutated"  # type: ignore[index]

    assert copied["plan"] == "plus"


def _provider_fixture(**overrides: object) -> dict[str, object]:
    migration_id = UUID("00000000-0000-4000-8000-000000000001")
    operation_id = UUID("00000000-0000-4000-8000-000000000002")
    observed = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
    expires = observed + timedelta(minutes=15)
    values: dict[str, object] = {
        "migration_id": migration_id,
        "operation_id": operation_id,
        "expected_external_reference": "asaas-subscription-1",
        "expected_snapshot_digest": "e" * 64,
        "migration_created_at": observed - timedelta(days=1),
        "provider": "asaas",
        "external_reference": "asaas-subscription-1",
        "provider_event_id": "evt-asaas-1",
        "provider_authority": "asaas.api.v3",
        "provider_key_id": "fixture-v1",
        "provider_operation": "subscription.migration.provider_flip",
        "provider_observed_at": observed,
        "provider_expires_at": expires,
        "provider_snapshot_digest": "e" * 64,
        "observed_at": observed,
        "now": observed + timedelta(minutes=1),
    }
    values["provider_evidence_digest_value"] = provider_evidence_digest(
        migration_id=migration_id,
        operation_id=operation_id,
        provider="asaas",
        external_reference="asaas-subscription-1",
        provider_event_id="evt-asaas-1",
        provider_authority="asaas.api.v3",
        provider_snapshot_digest="e" * 64,
        provider_observed_at=observed,
        provider_expires_at=expires,
        provider_operation="subscription.migration.provider_flip",
    )
    values["provider_signature"] = provider_evidence_signature(
        provider_authority="asaas.api.v3",
        provider_digest=values["provider_evidence_digest_value"],  # type: ignore[arg-type]
        signing_key=_PROVIDER_PRIVATE_KEY,
    )
    values.update(overrides)
    return values


def test_provider_evidence_fixture_binds_every_boundary() -> None:
    validate_provider_evidence_envelope(
        **_provider_fixture(),
        provider_public_key=_PROVIDER_PUBLIC_KEY,
        expected_provider_key_id="fixture-v1",
    )

    for field, value in (
        ("provider_event_id", None),
        ("provider_expires_at", datetime(2026, 10, 12, 12, tzinfo=timezone.utc)),
        ("provider_signature", "a" * 64),
        ("provider_authority", "wrong.authority"),
        ("provider_key_id", "wrong-key"),
        ("provider_operation", "wrong-operation"),
        ("operation_id", UUID("00000000-0000-4000-8000-000000000003")),
        ("external_reference", "asaas-other-subscription"),
        ("provider", "abacatepay"),
        ("provider_evidence_digest_value", "f" * 64),
        ("provider_snapshot_digest", "d" * 64),
    ):
        with pytest.raises(BillingMigrationRejected, match="readback_unverified"):
            validate_provider_evidence_envelope(
                **_provider_fixture(**{field: value}),
                provider_public_key=_PROVIDER_PUBLIC_KEY,
                expected_provider_key_id="fixture-v1",
            )


# ---------------------------------------------------------------------------
# Fix cycle 7: São Paulo deadlines (CRITICAL-1) and two-read absence (HIGH-1).
# ---------------------------------------------------------------------------

from collections.abc import AsyncIterator  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402
from uuid import uuid4  # noqa: E402

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import src.api.main  # noqa: E402,F401  (model registry import order, as in conftest)
from src.config.settings import settings  # noqa: E402
from src.models.base import Base  # noqa: E402
from src.models.billing import (  # noqa: E402
    BillingMigration,
    BillingMigrationAbsenceRead,
    BillingMigrationReceipt,
    BillingRosterControl,
    BillingSubscription,
)
from src.models.builder import Builder  # noqa: E402
from src.observability.metrics import BILLING_MIGRATION_OPERATIONS_TOTAL  # noqa: E402
from src.repositories.database import _patch_jsonb_for_sqlite  # noqa: E402
from src.services import billing_checkout  # noqa: E402
from src.services.billing_migration import (  # noqa: E402
    ABSENCE_CONVERGENCE_LIMIT,
    ABSENCE_READ_SPACING,
    MIGRATION_CUTOVER_LATE,
    MIGRATION_OPEN_LATE,
    PROVIDER_ABSENCE_OPERATION,
    BillingMigrationService,
    ProviderEvidenceEnvelope,
    migration_deadline_passed,
)

# T = 2026-12-10 -> T-7 = 2026-12-03, T-2 = 2026-12-08.  São Paulo is UTC-03:00
# all year (no DST since 2019), so local midnight is 03:00Z.
_T = date(2026, 12, 10)
_T2_LAST_SECOND = datetime(2026, 12, 9, 2, 59, 59, tzinfo=timezone.utc)
_T2_FIRST_LATE = datetime(2026, 12, 9, 3, 0, 0, tzinfo=timezone.utc)
_T7_LAST_SECOND = datetime(2026, 12, 4, 2, 59, 59, tzinfo=timezone.utc)
_T7_FIRST_LATE = datetime(2026, 12, 4, 3, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("now", "late"),
    [
        (_T2_LAST_SECOND, False),
        (datetime(2026, 12, 9, 2, 59, 59, 999999, tzinfo=timezone.utc), False),
        (_T2_FIRST_LATE, True),
        # Already 2026-12-09 in UTC, still 2026-12-08 in São Paulo.
        (datetime(2026, 12, 9, 1, 0, tzinfo=timezone.utc), False),
        # The same instants carried in other offsets are judged in São Paulo.
        (datetime(2026, 12, 9, 11, 59, 59, tzinfo=timezone(timedelta(hours=9))), False),
        (datetime(2026, 12, 9, 12, 0, 0, tzinfo=timezone(timedelta(hours=9))), True),
        (
            datetime(2026, 12, 8, 23, 59, 59, tzinfo=timezone(timedelta(hours=-3))),
            False,
        ),
        (datetime(2026, 12, 8, 0, 0, tzinfo=timezone.utc), False),
    ],
)
def test_cutover_deadline_is_inclusive_through_the_sao_paulo_day(
    now: datetime, late: bool
) -> None:
    assert migration_deadline_passed(now, date(2026, 12, 8)) is late


def test_deadline_rejects_a_naive_clock() -> None:
    with pytest.raises(
        BillingMigrationRejected, match="^billing_migration_clock_invalid$"
    ):
        migration_deadline_passed(datetime(2026, 12, 9, 12, 0), date(2026, 12, 8))


def test_absence_policy_mirrors_checkout_convergence_constants() -> None:
    # Migration 032 hard-codes the same 90 min / 5 min database floors.
    assert ABSENCE_CONVERGENCE_LIMIT == billing_checkout.CHECKOUT_CONVERGENCE_LIMIT
    assert ABSENCE_CONVERGENCE_LIMIT == timedelta(minutes=90)
    assert ABSENCE_READ_SPACING == billing_checkout.CONVERGENCE_READ_SPACING
    assert ABSENCE_READ_SPACING == timedelta(minutes=5)


@dataclass
class _World:
    sessions: async_sessionmaker[AsyncSession]
    builder_id: UUID


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[_World]:
    _patch_jsonb_for_sqlite()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    builder_id = uuid4()
    async with sessions() as session:
        session.add_all(
            [
                Builder(id=builder_id, name="Migration Builder"),
                BillingRosterControl(singleton=True, billing_roster_revision=1),
            ]
        )
        await session.commit()
    try:
        yield _World(sessions=sessions, builder_id=builder_id)
    finally:
        await engine.dispose()


def _legacy_target() -> MigrationTarget:
    return _target(catalog_version_id="catalog-v1")


async def _seed_subscription(world: _World) -> UUID:
    subscription_id = uuid4()
    async with world.sessions() as session:
        session.add(
            BillingSubscription(
                id=subscription_id,
                builder_id=world.builder_id,
                provider="abacatepay",
                external_subscription_id=f"legacy-{subscription_id}",
                is_active=True,
                plan="start",
                billing_cycle="monthly",
                status="active",
                roster_revision=1,
            )
        )
        await session.commit()
    return subscription_id


async def _import(world: _World, *, at: datetime) -> UUID:
    subscription_id = await _seed_subscription(world)
    async with world.sessions() as session:
        view = await BillingMigrationService(
            session, clock=lambda: at
        ).import_roster_entry(
            builder_id=world.builder_id,
            subscription_id=subscription_id,
            renewal=_T,
            target=_legacy_target(),
        )
        await session.commit()
    return view.migration_id


async def _row(world: _World, migration_id: UUID) -> tuple[object, ...]:
    async with world.sessions() as session:
        migration = await session.get(BillingMigration, migration_id)
        assert migration is not None
        subscription = await session.get(BillingSubscription, migration.subscription_id)
        assert subscription is not None
        revision = await session.scalar(
            select(BillingRosterControl.billing_roster_revision)
        )
        receipts = await session.scalar(
            select(func.count()).select_from(BillingMigrationReceipt)
        )
        return (
            migration.state,
            migration.operation_id,
            migration.attempt_count,
            subscription.provider,
            subscription.is_active,
            subscription.status,
            subscription.roster_revision,
            revision,
            receipts,
        )


def _rejected_count(operation: str) -> float:
    return BILLING_MIGRATION_OPERATIONS_TOTAL.labels(
        operation=operation, outcome="rejected"
    )._value.get()


async def test_late_cancel_begin_changes_nothing_and_emits_rejected_metric(
    world: _World,
) -> None:
    migration_id = await _import(world, at=datetime(2026, 12, 1, tzinfo=timezone.utc))
    before = await _row(world, migration_id)
    rejected_before = _rejected_count("cancel_begin")

    async with world.sessions() as session:
        with pytest.raises(
            BillingMigrationRejected, match=f"^{MIGRATION_CUTOVER_LATE}$"
        ):
            await BillingMigrationService(
                session, clock=lambda: _T2_FIRST_LATE
            ).begin_legacy_cancel(migration_id=migration_id, operation_id=uuid4())
        await session.rollback()

    assert await _row(world, migration_id) == before
    assert before[:2] == ("pending", None)
    assert before[3:6] == ("abacatepay", True, "active")
    assert _rejected_count("cancel_begin") == rejected_before + 1

    async with world.sessions() as session:
        await BillingMigrationService(
            session, clock=lambda: _T2_LAST_SECOND
        ).begin_legacy_cancel(migration_id=migration_id, operation_id=uuid4())
        await session.commit()
    accepted = await _row(world, migration_id)
    assert accepted[3:6] == (None, False, "legacy_cancel_pending")


async def test_late_roster_row_is_not_adopted(world: _World) -> None:
    subscription_id = await _seed_subscription(world)
    rejected_before = _rejected_count("roster_import")
    async with world.sessions() as session:
        with pytest.raises(BillingMigrationRejected, match=f"^{MIGRATION_OPEN_LATE}$"):
            await BillingMigrationService(
                session, clock=lambda: _T7_FIRST_LATE
            ).import_roster_entry(
                builder_id=world.builder_id,
                subscription_id=subscription_id,
                renewal=_T,
                target=_legacy_target(),
            )
        await session.rollback()
    async with world.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(BillingMigration))
            == 0
        )
    assert _rejected_count("roster_import") == rejected_before + 1

    async with world.sessions() as session:
        view = await BillingMigrationService(
            session, clock=lambda: _T7_LAST_SECOND
        ).import_roster_entry(
            builder_id=world.builder_id,
            subscription_id=subscription_id,
            renewal=_T,
            target=_legacy_target(),
        )
        await session.commit()
    assert (view.open_by_date, view.cutover_by_date) == (
        date(2026, 12, 3),
        date(2026, 12, 8),
    )


async def _force_state(
    world: _World,
    migration_id: UUID,
    *,
    state: str,
    legacy_cancelled_at: datetime | None = None,
) -> UUID:
    """SQLite has no saga triggers: place the row where the guard is tested."""

    operation_id = uuid4()
    async with world.sessions() as session:
        migration = await session.get(BillingMigration, migration_id)
        assert migration is not None
        subscription = await session.get(BillingSubscription, migration.subscription_id)
        assert subscription is not None
        migration.state = state
        migration.operation_id = operation_id
        migration.legacy_cancelled_at = legacy_cancelled_at
        if legacy_cancelled_at is not None:
            # Imported before it was cancelled.
            migration.created_at = legacy_cancelled_at - timedelta(minutes=30)
        subscription.provider = None
        subscription.is_active = False
        subscription.status = "legacy_cancelled"
        await session.commit()
    return operation_id


@pytest.mark.parametrize(
    ("state", "operation"),
    [("recreate_pending", "recreate_reopen"), ("legacy_cancelled", "recreate_reserve")],
)
async def test_late_recreate_is_refused_before_any_evidence_check(
    world: _World, state: str, operation: str
) -> None:
    migration_id = await _import(world, at=datetime(2026, 12, 1, tzinfo=timezone.utc))
    await _force_state(world, migration_id, state=state)
    before = await _row(world, migration_id)
    async with world.sessions() as session:
        service = BillingMigrationService(session, clock=lambda: _T2_FIRST_LATE)
        with pytest.raises(
            BillingMigrationRejected, match=f"^{MIGRATION_CUTOVER_LATE}$"
        ):
            if operation == "recreate_reopen":
                await service.reopen_recreate_after_absent_readback(
                    migration_id=migration_id, actor_user_id=uuid4()
                )
            else:
                await service.reserve_recreate_attempt(
                    migration_id=migration_id, environment="test"
                )
        await session.rollback()
    assert await _row(world, migration_id) == before


def _absence_envelope(
    *,
    migration_id: UUID,
    operation_id: UUID,
    snapshot_digest: str,
    observed_at: datetime,
    event_id: str,
) -> ProviderEvidenceEnvelope:
    digest = provider_evidence_digest(
        migration_id=migration_id,
        operation_id=operation_id,
        provider="asaas",
        external_reference=f"migration:{migration_id}",
        provider_event_id=event_id,
        provider_authority="asaas.api.v3",
        provider_snapshot_digest=snapshot_digest,
        provider_observed_at=observed_at,
        provider_expires_at=observed_at + timedelta(hours=2),
        provider_operation=PROVIDER_ABSENCE_OPERATION,
    )
    return ProviderEvidenceEnvelope(
        external_reference=f"migration:{migration_id}",
        provider_event_id=event_id,
        provider_key_id="fixture-v1",
        provider_operation=PROVIDER_ABSENCE_OPERATION,
        provider_signature=provider_evidence_signature(
            provider_authority="asaas.api.v3",
            provider_digest=digest,
            signing_key=_PROVIDER_PRIVATE_KEY,
        ),
        provider_observed_at=observed_at,
        provider_expires_at=observed_at + timedelta(hours=2),
        provider_snapshot_digest=snapshot_digest,
        provider_evidence_digest=digest,
    )


@pytest.fixture
def provider_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "asaas_provider_evidence_key_id", "fixture-v1")
    monkeypatch.setattr(
        settings, "asaas_provider_evidence_public_key", _PROVIDER_PUBLIC_KEY.hex()
    )


async def test_authorization_without_both_reads_never_reserves(
    world: _World, provider_authority: None
) -> None:
    now = datetime.now(timezone.utc)
    migration_id = await _import(world, at=now)
    operation_id = await _force_state(
        world,
        migration_id,
        state="legacy_cancelled",
        legacy_cancelled_at=now - timedelta(hours=3),
    )
    async with world.sessions() as session:
        service = BillingMigrationService(session, clock=lambda: now)
        snapshot = await service._provider_digest(
            migration_id=migration_id, receipt_kind="provider_flip", observed_at=now
        )
    # SQLite has no 032 trigger: an authorization receipt can exist alone here,
    # which isolates the service's own two-read guard.
    envelope = _absence_envelope(
        migration_id=migration_id,
        operation_id=operation_id,
        snapshot_digest=snapshot,
        observed_at=now - timedelta(minutes=1),
        event_id="evt-authorization-only",
    )
    async with world.sessions() as session:
        migration = await session.get(BillingMigration, migration_id)
        assert migration is not None
        receipt = BillingMigrationReceipt(
            builder_id=world.builder_id,
            migration_id=migration_id,
            operation_id=operation_id,
            receipt_kind="recreate_authorized",
            provider="asaas",
            external_reference=envelope.external_reference,
            provider_event_id=envelope.provider_event_id,
            provider_authority=envelope.provider_authority,
            provider_key_id=envelope.provider_key_id,
            provider_operation=envelope.provider_operation,
            provider_signature=envelope.provider_signature,
            provider_observed_at=envelope.provider_observed_at,
            provider_expires_at=envelope.provider_expires_at,
            provider_snapshot_digest=envelope.provider_snapshot_digest,
            provider_evidence_digest=envelope.provider_evidence_digest,
            status="absent",
            content_sha256=envelope.provider_evidence_digest,
            actor_user_id=uuid4(),
            observed_at=now,
        )
        session.add(receipt)
        await session.commit()
        receipt_id = receipt.id

    async def reserve() -> None:
        async with world.sessions() as session:
            try:
                await BillingMigrationService(
                    session, clock=lambda: now
                ).reserve_recreate_attempt(
                    migration_id=migration_id,
                    environment="test",
                    absence_receipt_id=receipt_id,
                )
            finally:
                await session.rollback()

    with pytest.raises(
        BillingMigrationRejected, match="^billing_migration_absence_reads_incomplete$"
    ):
        await reserve()

    # One read recorded through the service is still not enough.
    async with world.sessions() as session:
        first = await BillingMigrationService(
            session, clock=lambda: now - timedelta(minutes=19)
        ).record_absence_read(
            migration_id=migration_id,
            actor_user_id=uuid4(),
            evidence=_absence_envelope(
                migration_id=migration_id,
                operation_id=operation_id,
                snapshot_digest=snapshot,
                observed_at=now - timedelta(minutes=20),
                event_id="evt-read-one",
            ),
        )
        await session.commit()
    assert (first.read_no, first.authorization_receipt_id) == (1, None)
    with pytest.raises(
        BillingMigrationRejected, match="^billing_migration_absence_reads_incomplete$"
    ):
        await reserve()
    async with world.sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(BillingMigrationAbsenceRead)
            )
            == 1
        )
