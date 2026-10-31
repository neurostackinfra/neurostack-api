"""Group 7 boundary tests for code-only credit rollout readiness."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from src.services.credit_rollout import (
    OBSERVATIONAL_DROP_REASONS,
    BindingCoverage,
    CapabilityReadiness,
    CreditRolloutService,
    cutoff_digest,
)


def test_current_civil_date_uses_sao_paulo_not_utc_midnight() -> None:
    # 00:30 UTC remains the previous civil day in Sao Paulo.
    assert (
        CreditRolloutService.current_civil_date(
            datetime(2026, 1, 2, 0, 30, tzinfo=timezone.utc)
        ).isoformat()
        == "2026-01-01"
    )


def test_cutoff_digest_is_stable_and_order_sensitive_to_report_content() -> None:
    rows = [{"tenant": "a", "reason": "standalone"}]
    assert cutoff_digest(rows) == cutoff_digest([dict(rows[0])])
    assert cutoff_digest(rows) != cutoff_digest([{**rows[0], "reason": "unknown"}])


def test_observational_drop_vocabulary_is_closed() -> None:
    assert OBSERVATIONAL_DROP_REASONS == {
        "route_busy",
        "pool_exhausted",
        "timeout",
        "failed",
        "tenant_gone",
    }


def test_cutoff_coverage_is_narrower_than_the_debt_path() -> None:
    """A normal empty builder is served as debt but blocks rollout cutoff."""

    empty = BindingCoverage()
    assert empty.usable
    assert not empty.covered
    assert BindingCoverage(monthly_binding=True).covered
    assert BindingCoverage(usable_topup=True).covered
    assert not BindingCoverage(usable_topup=True, quarantined=True).covered


def test_current_binding_requires_the_exact_cycle_revision() -> None:
    builder_id = uuid4()
    subscription_id = uuid4()
    cycle_id = uuid4()
    cycle = SimpleNamespace(
        id=cycle_id,
        builder_id=builder_id,
        subscription_id=subscription_id,
        cycle_start=datetime(2026, 10, 1).date(),
        cycle_end=datetime(2026, 11, 1).date(),
        financial_revision=3,
    )
    binding = SimpleNamespace(
        status="active",
        builder_id=builder_id,
        subscription_id=subscription_id,
        subscription_cycle_id=cycle_id,
        cycle_start=cycle.cycle_start,
        cycle_end=cycle.cycle_end,
        financial_revision=4,
    )

    assert not CreditRolloutService._binding_matches_cycle(binding, cycle)


def test_identity_is_split_from_revision_and_status() -> None:
    """Identity ignores revision and status (request coverage: lag serves);
    the strict predicate of readiness/arming requires both."""

    builder_id, subscription_id, cycle_id = uuid4(), uuid4(), uuid4()
    start, end = datetime(2026, 10, 1).date(), datetime(2026, 11, 1).date()
    cycle = SimpleNamespace(
        id=cycle_id,
        builder_id=builder_id,
        subscription_id=subscription_id,
        cycle_start=start,
        cycle_end=end,
        financial_revision=3,
    )

    def binding(**changes: object) -> SimpleNamespace:
        values: dict[str, object] = {
            "status": "active",
            "builder_id": builder_id,
            "subscription_id": subscription_id,
            "subscription_cycle_id": cycle_id,
            "cycle_start": start,
            "cycle_end": end,
            "financial_revision": 3,
        }
        values.update(changes)
        return SimpleNamespace(**values)

    identity = CreditRolloutService._binding_identity_matches
    strict = CreditRolloutService._binding_matches_cycle
    assert identity(binding(), cycle) and strict(binding(), cycle)
    for lagging in (binding(financial_revision=2), binding(status="reversed")):
        assert identity(lagging, cycle) and not strict(lagging, cycle)
    for drift in (
        binding(builder_id=uuid4()),
        binding(subscription_id=uuid4()),
        binding(subscription_cycle_id=uuid4()),
        binding(cycle_start=datetime(2026, 9, 30).date()),
        binding(cycle_end=datetime(2026, 11, 2).date()),
    ):
        assert not identity(drift, cycle) and not strict(drift, cycle)
    assert not identity(None, cycle) and not strict(None, cycle)


@pytest.mark.asyncio
async def test_record_capability_readiness_keeps_all_receipt_fields_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Mock()
    session.add = Mock()
    session.flush = AsyncMock()
    service = CreditRolloutService(session)
    readiness = CapabilityReadiness(
        environment="test",
        billing_fact_schema_digest="a" * 64,
        inbox_high_watermark=4,
        reconciliation_complete=True,
        readback_complete=True,
        has_open_gap=False,
        has_backlog=False,
        has_alert=False,
    )
    monkeypatch.setattr(service, "capability_ready", AsyncMock(return_value=readiness))

    row = await service.record_capability_readiness()

    assert row.operation_id is None
    assert row.deployment_digest is None
    assert row.credit_deployment_digest is None
    assert row.producer_compatibility_digest is None
    assert row.consumer_compatibility_digest is None
    session.add.assert_called_once_with(row)
    session.flush.assert_awaited_once()
