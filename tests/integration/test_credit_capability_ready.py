"""Group 7 capability readiness on a migrated PostgreSQL schema.

Each calculated field of ``credit_capability_readiness`` is proven one by
one on a fresh database (AC "testado um a um"): ``environment``,
``billing_fact_schema_digest``, ``inbox_high_watermark`` (0 empty, then
growing), ``reconciliation_complete`` (tracks ``credit_reconciliation_ready``),
``readback_complete``, ``has_open_gap``, ``has_backlog`` and ``has_alert``;
the five receipt-derived fields stay NULL and the approval fields do not
exist.  The closure rule for OUTBOX-scoped ASAAS alerts: a direct accepted
ack closes the alert even when it is re-alerted, a rejected ack at revision
N followed by an accepted ack at N+1 closes it, and neither leaves it open.
The evaluation is read-only and repeatable: no ASAAS table changes.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from src.api.routes import admin_credits
from src.config.settings import settings
from src.models.credit import CreditCapabilityReadiness
from src.services import credit_rollout
from src.services.billing_outbox import authoritative_billing_fact_schema_digest
from src.services.content_retention import Actor
from src.services.credit_reconciliation import (
    CreditReconciliation,
    credit_reconciliation_ready,
)
from src.services.credit_rollout import CapabilityReadiness, CreditRolloutService
from tests.integration.credit_rollout_world import (
    civil_today,
    clean,
    execute,
    make_builder,
    make_cycle,
    make_monthly_grant,
    make_subscription,
    make_topup,
    outbox_fact,
    plant,
    scalar,
    set_cycle_revision,
    subscribed_builder,
    template,
)
from tests.integration.test_billing_fact_worker_postgres import _Harness

pytestmark = pytest.mark.integration

__all__ = ["clean", "template"]

NOW = datetime.now(timezone.utc)
TODAY = civil_today(NOW)
CALCULATED = (
    "reconciliation_complete",
    "readback_complete",
    "has_open_gap",
    "has_backlog",
    "has_alert",
)
RECEIPT_FIELDS = (
    "operation_id",
    "deployment_digest",
    "credit_deployment_digest",
    "producer_compatibility_digest",
    "consumer_compatibility_digest",
)
ABSENT_FIELDS = (
    "request_id",
    "reconciliation_receipt_id",
    "approved_by",
    "approved_at",
    "expires_at",
)
CLEAN_FLAGS = {
    "reconciliation_complete": True,
    "readback_complete": True,
    "has_open_gap": False,
    "has_backlog": False,
    "has_alert": False,
}


async def _ready(harness: _Harness) -> CapabilityReadiness:
    async with harness.sessions() as session:
        readiness = await CreditRolloutService(session).capability_ready(now=NOW)
        await session.rollback()
    return readiness


async def _flags(harness: _Harness) -> dict[str, bool]:
    readiness = await _ready(harness)
    return {name: getattr(readiness, name) for name in CALCULATED}


async def _reconciliation_ready(harness: _Harness) -> bool:
    async with harness.sessions() as session:
        ready = await credit_reconciliation_ready(session)
        await session.rollback()
    return ready


async def _open_alerts(harness: _Harness) -> int:
    async with harness.sessions() as session:
        report = await CreditReconciliation(session).report()
        await session.rollback()
    return report.open_alerts


async def _alert(harness: _Harness, outbox_id: UUID | None, detail: str) -> None:
    await plant(
        harness,
        "INSERT INTO billing_fact_reconciliation_alerts (id, outbox_event_id, kind, "
        "detail_sha256, observed_at) VALUES (:id, :outbox, 'backlog', :sha, now())",
        id=uuid4(),
        outbox=outbox_id,
        sha=hashlib.sha256(detail.encode()).hexdigest(),
    )


async def _gap(harness: _Harness) -> None:
    await execute(
        harness,
        "INSERT INTO credit_revision_gaps (aggregate_kind, aggregate_id, "
        "first_missing_revision) VALUES ('topup_order', :id, 2)",
        id=uuid4(),
    )


async def _effect_without_outbox(harness: _Harness, builder_id: UUID) -> None:
    await plant(
        harness,
        """
        INSERT INTO billing_payment_effects (
            id, builder_id, projection_id, delivery_id, provider, payment_id,
            effect_kind, source_revision, amount_cents
        ) VALUES (:id, :builder, :projection, :delivery, 'asaas', 'pay_orphan',
                  'settlement', 'settlement:v1', 10000)
        """,
        id=uuid4(),
        builder=builder_id,
        projection=uuid4(),
        delivery=uuid4(),
    )


async def _asaas_digest(harness: _Harness) -> str:
    tables = [
        "billing_subscriptions",
        "billing_subscription_cycles",
        "billing_topup_orders",
        "billing_outbox",
        "billing_fact_acks",
        "billing_fact_reconciliation_alerts",
        "billing_payment_effects",
        "billing_rollout_control",
        "billing_topup_capability_receipts",
    ]
    parts = []
    for table in tables:
        parts.append(
            str(
                await scalar(
                    harness,
                    f"SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), "
                    f"'')) FROM {table} t",
                )
            )
        )
    return "|".join(parts)


# ---------------------------------------------------------------------------
# The recorded row.
# ---------------------------------------------------------------------------


async def test_code_readiness_records_only_calculated_fields(clean: _Harness) -> None:
    await subscribed_builder(clean)
    async with clean.sessions() as session:
        service = CreditRolloutService(session)
        readiness = await service.capability_ready(now=NOW)
        row = await service.record_capability_readiness()
        await session.commit()
        stored = await session.scalar(
            select(CreditCapabilityReadiness).where(
                CreditCapabilityReadiness.id == row.id
            )
        )
        assert stored is not None
        recorded = {
            name: getattr(stored, name)
            for name in (
                "schema_version",
                "environment",
                "billing_fact_schema_digest",
                "inbox_high_watermark",
                *CALCULATED,
                *RECEIPT_FIELDS,
            )
        }
        await session.rollback()

    assert readiness.ready is True
    assert recorded["schema_version"] == "credit_capability_ready.v1"
    assert recorded["environment"] == readiness.environment == settings.environment
    assert (
        recorded["billing_fact_schema_digest"] == readiness.billing_fact_schema_digest
    )
    assert recorded["inbox_high_watermark"] == 0
    assert {name: recorded[name] for name in CALCULATED} == CLEAN_FLAGS
    assert all(recorded[name] is None for name in RECEIPT_FIELDS)

    columns = {
        str(name) for (name,) in await _columns(clean, "credit_capability_readiness")
    }
    assert set(RECEIPT_FIELDS) <= columns
    assert not set(ABSENT_FIELDS) & columns
    assert not any(hasattr(CreditCapabilityReadiness, name) for name in ABSENT_FIELDS)


async def _columns(harness: _Harness, table: str) -> list[tuple[str]]:
    async with harness.engine.connect() as connection:
        return [
            (row[0],)
            for row in (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = :t"
                    ),
                    {"t": table},
                )
            ).all()
        ]


# ---------------------------------------------------------------------------
# environment and billing_fact_schema_digest.
# ---------------------------------------------------------------------------


# Real production runs ENVIRONMENT=staging; the producer publishes no
# authoritative schema for a literal "production" environment.
@pytest.mark.parametrize("environment", ["test", "staging"])
async def test_environment_and_schema_digest_follow_settings(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    monkeypatch.setattr(settings, "environment", environment)
    readiness = await _ready(clean)
    assert readiness.environment == environment
    assert readiness.billing_fact_schema_digest == (
        authoritative_billing_fact_schema_digest(environment)
    )


async def test_the_schema_digest_is_the_producers_for_that_environment(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both real environments share one digest, so the parametrization above
    cannot tell a constant from the producer's answer: substitute a producer
    that answers per environment and require exactly its answer."""

    asked: list[str] = []

    def per_environment(environment: str) -> str:
        asked.append(environment)
        return hashlib.sha256(f"schema:{environment}".encode()).hexdigest()

    monkeypatch.setattr(
        credit_rollout, "authoritative_billing_fact_schema_digest", per_environment
    )
    for environment in ("test", "staging"):
        monkeypatch.setattr(settings, "environment", environment)
        readiness = await _ready(clean)
        assert readiness.billing_fact_schema_digest == per_environment(environment)
    assert asked == ["test", "test", "staging", "staging"]


# ---------------------------------------------------------------------------
# inbox_high_watermark.
# ---------------------------------------------------------------------------


async def test_inbox_high_watermark_is_zero_then_grows(clean: _Harness) -> None:
    assert (await _ready(clean)).inbox_high_watermark == 0
    # A fresh identity starts at 1, where max(inbox_seq) == count(*): start
    # it at 100 so only the maximum matches.
    await execute(
        clean, "ALTER TABLE credit_fact_inbox ALTER COLUMN inbox_seq RESTART WITH 100"
    )
    builder_id = await make_builder(clean)
    order = uuid4()
    await outbox_fact(clean, builder_id=builder_id, order_id=order, revision=1)
    first = (await _ready(clean)).inbox_high_watermark
    assert first == await scalar(clean, "SELECT max(inbox_seq) FROM credit_fact_inbox")
    assert first == 100
    await outbox_fact(clean, builder_id=builder_id, order_id=order, revision=2)
    second = (await _ready(clean)).inbox_high_watermark
    assert second == 101 > first
    assert second == await scalar(clean, "SELECT max(inbox_seq) FROM credit_fact_inbox")


# ---------------------------------------------------------------------------
# reconciliation_complete tracks credit_reconciliation_ready.
# ---------------------------------------------------------------------------


async def test_reconciliation_complete_starts_true(clean: _Harness) -> None:
    assert await _reconciliation_ready(clean) is True
    assert await _flags(clean) == CLEAN_FLAGS


@pytest.mark.parametrize("cause", ["gap", "backlog", "quarantine", "rejected"])
async def test_reconciliation_complete_tracks_credit_reconciliation_ready(
    clean: _Harness, cause: str
) -> None:
    builder_id = await make_builder(clean)
    if cause == "gap":
        await _gap(clean)
    elif cause == "backlog":
        await outbox_fact(
            clean, builder_id=builder_id, order_id=uuid4(), ack=None, inbox=False
        )
    elif cause == "quarantine":
        subscription_id = await make_subscription(clean, builder_id)
        await make_cycle(
            clean,
            builder_id,
            subscription_id,
            start=TODAY + timedelta(days=40),
            end=TODAY + timedelta(days=70),
            state="quarantined",
        )
    else:
        await outbox_fact(
            clean, builder_id=builder_id, order_id=uuid4(), ack="rejected"
        )
    assert await _reconciliation_ready(clean) is False
    assert (await _ready(clean)).reconciliation_complete is False


# ---------------------------------------------------------------------------
# readback_complete.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["unbound", "lag", "identity"])
async def test_readback_complete_falls_for_an_open_current_cycle(
    clean: _Harness, case: str
) -> None:
    builder_id = await make_builder(clean)
    subscription_id = await make_subscription(clean, builder_id)
    start, end = TODAY - timedelta(days=5), TODAY + timedelta(days=25)
    cycle_id = await make_cycle(
        clean, builder_id, subscription_id, start=start, end=end
    )
    if case == "lag":
        await make_monthly_grant(
            clean,
            builder_id=builder_id,
            subscription_id=subscription_id,
            cycle_id=cycle_id,
            start=start,
            end=end,
        )
        await set_cycle_revision(clean, cycle_id, 2)
    elif case == "identity":
        await make_monthly_grant(
            clean,
            builder_id=builder_id,
            subscription_id=subscription_id,
            cycle_id=cycle_id,
            start=start,
            end=end,
            binding_start=start + timedelta(days=1),
        )
    assert (await _ready(clean)).readback_complete is False


@pytest.mark.parametrize("offset", [-40, 40])
async def test_readback_complete_holds_without_a_candidate_cycle(
    clean: _Harness, offset: int
) -> None:
    """No current cycle row: not a gate failure (the cutoff report owns it)."""

    builder_id = await make_builder(clean)
    subscription_id = await make_subscription(clean, builder_id)
    start = TODAY + timedelta(days=offset)
    cycle_id = await make_cycle(
        clean, builder_id, subscription_id, start=start, end=start + timedelta(days=30)
    )
    await make_monthly_grant(
        clean,
        builder_id=builder_id,
        subscription_id=subscription_id,
        cycle_id=cycle_id,
        start=start,
        end=start + timedelta(days=30),
    )
    assert (await _ready(clean)).readback_complete is True


# ---------------------------------------------------------------------------
# has_open_gap, has_backlog.
# ---------------------------------------------------------------------------


async def test_has_open_gap_rises_with_a_revision_gap(clean: _Harness) -> None:
    await _gap(clean)
    flags = await _flags(clean)
    assert flags["has_open_gap"] is True
    assert flags["has_backlog"] is False


async def test_has_open_gap_rises_with_an_effect_without_outbox(
    clean: _Harness,
) -> None:
    await _effect_without_outbox(clean, await make_builder(clean))
    flags = await _flags(clean)
    assert flags["has_open_gap"] is True
    assert flags["has_backlog"] is False


async def test_has_backlog_rises_with_a_fact_without_ack(clean: _Harness) -> None:
    await outbox_fact(
        clean, builder_id=await make_builder(clean), order_id=uuid4(), ack=None
    )
    flags = await _flags(clean)
    assert flags["has_backlog"] is True
    assert flags["has_open_gap"] is False


# ---------------------------------------------------------------------------
# has_alert and the closure rule of outbox-scoped alerts.
# ---------------------------------------------------------------------------


async def test_has_alert_rises_with_a_counted_alert(clean: _Harness) -> None:
    builder_id = await make_builder(clean)
    order = uuid4()
    outbox = await outbox_fact(clean, builder_id=builder_id, order_id=order)
    await _alert(clean, outbox, "probe:counted")
    # The fact itself was accepted directly: this alert is closed.
    assert await _open_alerts(clean) == 0
    assert (await _ready(clean)).has_alert is False

    # An alert on a fact with no ack at all (no rejected ack either, so only
    # the alert itself can raise has_alert).
    unacked = await outbox_fact(
        clean, builder_id=builder_id, order_id=uuid4(), ack=None, inbox=False
    )
    await _alert(clean, unacked, "probe:open")
    async with clean.sessions() as session:
        report = await CreditReconciliation(session).report()
        orphan = await CreditRolloutService(session).orphan_report()
        await session.rollback()
    assert report.rejected_open == 0 and report.digest_mismatches == 0
    assert report.quarantined_lines == 0 and report.binding_lag == 0
    assert orphan.open_count == 0
    assert report.open_alerts == 1
    assert (await _ready(clean)).has_alert is True


async def test_has_alert_rises_with_binding_lag(clean: _Harness) -> None:
    """has_alert is broader than ASAAS alerts: consumer lag raises it too."""

    world = await subscribed_builder(clean)
    assert (await _ready(clean)).has_alert is False
    await set_cycle_revision(clean, world.cycle_id, 2)
    assert (await _ready(clean)).has_alert is True


async def test_a_directly_accepted_fact_closes_its_repeated_alerts(
    clean: _Harness,
) -> None:
    outbox = await outbox_fact(
        clean, builder_id=await make_builder(clean), order_id=uuid4()
    )
    # The ASAAS reconciler re-alerts the same gap on each page (deduplicated
    # by (outbox_event_id, kind, detail_sha256)): different details.
    for page in range(3):
        await _alert(clean, outbox, f"probe:page:{page}")
    assert await _open_alerts(clean) == 0


async def test_rejected_at_n_then_accepted_at_n_plus_1_closes(
    clean: _Harness,
) -> None:
    builder_id, order = await make_builder(clean), uuid4()
    rejected = await outbox_fact(
        clean, builder_id=builder_id, order_id=order, revision=1, ack="rejected"
    )
    await _alert(clean, rejected, "probe:rejected")
    assert await _open_alerts(clean) == 1
    await outbox_fact(clean, builder_id=builder_id, order_id=order, revision=2)
    assert await _open_alerts(clean) == 0
    # A later revision of ANOTHER aggregate never closes it.
    other = await outbox_fact(
        clean, builder_id=builder_id, order_id=uuid4(), revision=1, ack="rejected"
    )
    await _alert(clean, other, "probe:other")
    await outbox_fact(clean, builder_id=builder_id, order_id=uuid4(), revision=2)
    assert await _open_alerts(clean) == 1


async def test_an_alert_with_neither_closure_stays_open(clean: _Harness) -> None:
    outbox = await outbox_fact(
        clean, builder_id=await make_builder(clean), order_id=uuid4(), ack=None
    )
    await _alert(clean, outbox, "probe:neither")
    assert await _open_alerts(clean) == 1


async def test_the_evaluation_is_read_only_and_repeatable(clean: _Harness) -> None:
    world = await subscribed_builder(clean)
    await set_cycle_revision(clean, world.cycle_id, 2)
    outbox = await outbox_fact(
        clean, builder_id=world.builder_id, order_id=uuid4(), ack="rejected"
    )
    await _alert(clean, outbox, "probe:repeat")
    before = await _asaas_digest(clean)
    first, second = await _ready(clean), await _ready(clean)
    assert first == second
    assert await _asaas_digest(clean) == before


# ---------------------------------------------------------------------------
# Digest mismatch and its closure, on PostgreSQL.
# ---------------------------------------------------------------------------


async def _handoff(harness: _Harness, outbox_id: UUID, bundle: str) -> None:
    await plant(
        harness,
        "INSERT INTO billing_fact_handoffs (id, outbox_event_id, delivery_id, "
        "schema_version, bundle_digest, deployment_digest, handed_off_at) VALUES "
        "(:id, :outbox, :delivery, 'billing.fact.v1', :bundle, :deployment, now())",
        id=uuid4(),
        outbox=outbox_id,
        delivery=uuid4(),
        bundle=bundle,
        deployment="a" * 64,
    )


async def test_a_digest_mismatch_closes_only_by_a_later_accepted_revision(
    clean: _Harness,
) -> None:
    builder_id, order = await make_builder(clean), uuid4()
    first = await outbox_fact(clean, builder_id=builder_id, order_id=order, revision=1)
    await _handoff(clean, first, "a" * 64)
    assert await _reconciliation_ready(clean) is True

    divergent = await outbox_fact(
        clean, builder_id=builder_id, order_id=order, revision=2
    )
    # The accepted ack names bundle "a"*64; the handoff says otherwise.
    await _handoff(clean, divergent, "e" * 64)
    async with clean.sessions() as session:
        report = await CreditReconciliation(session).report()
        await session.rollback()
    assert report.digest_mismatches == 1
    readiness = await _ready(clean)
    assert readiness.reconciliation_complete is False
    assert readiness.has_alert is True

    # An accepted revision N+1 of the same aggregate closes it.
    await outbox_fact(clean, builder_id=builder_id, order_id=order, revision=3)
    async with clean.sessions() as session:
        report = await CreditReconciliation(session).report()
        await session.rollback()
    assert report.digest_mismatches == 0
    assert (await _ready(clean)).reconciliation_complete is True


# ---------------------------------------------------------------------------
# The rollout surfaces are untouched by readiness and the rollout routes.
# ---------------------------------------------------------------------------


async def _surfaces(harness: _Harness) -> tuple[str, str, object]:
    control = await scalar(
        harness,
        "SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) "
        "FROM billing_rollout_control t",
    )
    receipts = await scalar(
        harness,
        "SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) "
        "FROM billing_topup_capability_receipts t",
    )
    commercial = await scalar(
        harness,
        "SELECT string_agg(environment || '=' || topup_commercial_enabled::text, "
        "',' ORDER BY environment) FROM billing_rollout_control",
    )
    return str(control), str(receipts), commercial


async def test_readiness_and_rollout_routes_never_touch_the_rollout_surfaces(
    clean: _Harness,
) -> None:
    world = await subscribed_builder(clean)
    await make_topup(clean, world.builder_id)
    before = await _surfaces(clean)
    actor = Actor(user_id=uuid4(), role="super_admin", jwt=True)

    async with clean.sessions() as session:
        await CreditRolloutService(session).record_capability_readiness()
        await session.commit()
    async with clean.sessions() as session:
        await admin_credits.credit_reconciliation_status(actor=actor, session=session)
    async with clean.sessions() as session:
        await admin_credits.credit_binding_status(
            world.builder_id, actor=actor, session=session
        )
    async with clean.sessions() as session:
        await admin_credits.credit_capability_status(actor=actor, session=session)
    async with clean.sessions() as session:
        await admin_credits.credit_capability_readiness(actor=actor, session=session)

    assert await _surfaces(clean) == before
    assert await scalar(clean, "SELECT count(*) FROM credit_capability_readiness") == 1
    assert await scalar(clean, "SELECT count(*) FROM credit_enforcement_state") == 0
