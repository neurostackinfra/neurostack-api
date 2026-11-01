"""The Group 7 enforcement transaction on real PostgreSQL, and its fence.

``CreditRolloutService.arm_enforcement`` (item 7) is the only writer of
``credit_enforcement_at``.  Proven here, each on a fresh migrated database:

* it refuses with an open orphan, with readiness false, with an unaccepted
  tenant cutoff and with an ambiguous open ASAAS subscription -- every
  non-terminal status counts (``active``, ``active_linked``,
  ``readback_required``, ``non_billable_pending_readback``), and a
  ``readback_required``/``non_billable_pending_readback`` subscription with
  a current cycle, or two candidate cycles, block;
* on success it writes ``credit_enforcement_at`` exactly once; a second call
  is refused and the boundary never moves; ``at`` is the database clock
  (neither earlier nor later);
* ``expected_report_digest`` is required (keyword-only, no default) and must
  be the digest of the report the transaction itself evaluates (the arms
  below pass the one the operator would have reviewed); a malformed value
  is refused before the rollout lock, a well-formed wrong one inside the
  transaction;
* a concurrent subscription change serializes under ``lock_rollout_control``:
  committed before the gate it is seen by the reread, after the gate it waits
  for the gate's commit and is then captured by the by-id reread;
* ``accept_cutoff`` is bound to the tenant AND the report digest, and its
  author must be an active ``super_admin``;
* the cutoff report names each tenant by its CURRENT ``tenants.builder_id``
  (a tenant detached to standalone inside the window is standalone);
* by AST over ``src``, ``scripts``, ``alembic``, ``config`` and ``packages``
  (every Python tree but ``tests``): ``record_capability_readiness`` and
  ``PurgeExecutor`` have no reference outside their defining modules;
  ``arm_enforcement`` and ``accept_cutoff`` have exactly one caller, the
  one-off task ``src/ops/credit_enforcement.py`` (asaas-rollout-opening
  Group 6), which arms at ``at=None`` with the confirmed digest and which no
  other module imports; no route arms.  A name, attribute, import alias,
  string literal, or string built from literals (``+``, f-strings) counts,
  and so does ``getattr(<the service, an alias or an instance variable of
  it>, <computed name>)``;
* by text over every non-Python file of the repository outside ``docs``,
  ``tests`` and ``.genie`` (``git ls-files -co --exclude-standard``, so
  ignored test artifacts are skipped and a new untracked file is not; a
  filesystem walk that skips the ignored artifact directories when git is
  unavailable): no shell script, Makefile, workflow, compose or
  infrastructure file names the task module or a writer.  A future
  legitimate mention (for example an alarm filter on the logger
  ``src.ops.credit_enforcement``) is declared in ``_TEXT_ALLOWED`` by path
  and exact line.

NOT covered (deliberate evasion; review catches these, the fence does not):
``getattr`` on any other object with a computed name (for example
``getattr(self.rollout, name)``), ``obj.__getattribute__(...)``,
``operator.methodcaller``, names assembled across statements or by
``"".join``/``%``/``.format`` (for example a ``".".join([...])`` module name
passed to a Python subprocess), shell variable splitting, an ignored and
untracked file (git does not list it; it cannot be pushed without ``-f``),
and anything outside this repository.
"""

from __future__ import annotations

import ast
import asyncio
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text

from src.config.settings import settings
from src.models.credit import CreditEnforcementState
from src.repositories.credit import CreditRepository
from src.services.credit_ledger import start_of_civil_day
from src.services.credit_metering import EventsEnforcement, precheck_events
from src.services.credit_principal import (
    TenantBuilder,
    credit_identity,
    derive_credit_principal,
)
from src.services.credit_rollout import (
    CreditEnforcementRejected,
    CreditRolloutService,
    CutoffReport,
)
from tests.integration.credit_rollout_world import (
    PLANT,
    civil_today,
    clean,
    execute,
    make_builder,
    make_cycle,
    make_monthly_grant,
    make_subscription,
    make_tenant,
    make_topup,
    message_in,
    observation,
    scalar,
    set_subscription_status,
    subscribed_builder,
    template,
)
from tests.integration.test_billing_fact_worker_postgres import _Harness

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.integration

__all__ = ["clean", "template"]

NOW = datetime.now(timezone.utc)
TODAY = civil_today(NOW)
# ``_arm`` default: confirm the report as it stands at the database clock.
REVIEWED = "reviewed"


async def _arm(
    harness: _Harness, *, at: datetime | None = None, digest: str | None = REVIEWED
) -> datetime:
    async with harness.sessions() as session:
        try:
            service = CreditRolloutService(session)
            if digest == REVIEWED:
                now = await CreditRepository(session).database_now()
                digest = (await service.cutoff_report(now=now)).digest
            state = await service.arm_enforcement(
                set_by="owner-decision", at=at, expected_report_digest=digest
            )
            armed = state.credit_enforcement_at
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
    assert armed is not None
    return armed


async def _refused(harness: _Harness, match: str) -> None:
    with pytest.raises(CreditEnforcementRejected, match=match):
        await _arm(harness)
    assert await _state_rows(harness) == (0, None)


async def _state_rows(harness: _Harness) -> tuple[int, datetime | None]:
    count = await scalar(harness, "SELECT count(*) FROM credit_enforcement_state")
    at = await scalar(
        harness, "SELECT max(credit_enforcement_at) FROM credit_enforcement_state"
    )
    return int(count), at


async def _cutoff(harness: _Harness) -> CutoffReport:
    async with harness.sessions() as session:
        report = await CreditRolloutService(session).cutoff_report(now=NOW)
        await session.rollback()
    return report


async def _owner(
    harness: _Harness, *, role: str = "super_admin", active: bool = True
) -> UUID:
    """A user (in a tenant of its own, with no traffic: not in any report)."""

    user_id = uuid4()
    home = await make_tenant(harness, None)
    await execute(
        harness,
        "INSERT INTO users (id, tenant_id, email, password_hash, role, is_active) "
        "VALUES (:id, :tenant, :email, 'x', CAST(:role AS userrole), :active)",
        id=user_id,
        tenant=home,
        email=f"{user_id.hex}@fence.invalid",
        role=role,
        active=active,
    )
    return user_id


async def _accept(
    harness: _Harness,
    *,
    tenant_id: UUID,
    builder_id: UUID | None,
    reason: str,
    digest: str,
    author: UUID | None = None,
) -> None:
    author_user_id = author or await _owner(harness)
    async with harness.sessions() as session:
        await CreditRolloutService(session).accept_cutoff(
            tenant_id=tenant_id,
            builder_id=builder_id,
            reason=reason,
            report_digest=digest,
            author_user_id=author_user_id,
        )
        await session.commit()


async def _two_bound_candidates(harness: _Harness) -> None:
    """A readback-only gate failure today: two candidate current cycles of
    one subscription, both exactly bound (no reconciliation lag)."""

    world = await subscribed_builder(harness)
    start, end = TODAY - timedelta(days=1), TODAY + timedelta(days=29)
    second = await make_cycle(
        harness, world.builder_id, world.subscription_id, start=start, end=end
    )
    await make_monthly_grant(
        harness,
        builder_id=world.builder_id,
        subscription_id=world.subscription_id,
        cycle_id=second,
        start=start,
        end=end,
    )


# ---------------------------------------------------------------------------
# Success, once, never backdated.
# ---------------------------------------------------------------------------


async def test_a_clean_world_arms_exactly_once(clean: _Harness) -> None:
    await subscribed_builder(clean)
    async with clean.sessions() as session:
        before = await CreditRepository(session).database_now()
        await session.rollback()
    armed = await _arm(clean)
    async with clean.sessions() as session:
        after = await CreditRepository(session).database_now()
        stored = await session.scalar(select(CreditEnforcementState))
        assert stored is not None
        set_by = stored.set_by
        await session.rollback()
    # ``at`` omitted is the database clock of the transaction.
    assert before <= armed <= after
    assert set_by == "owner-decision"
    assert await _state_rows(clean) == (1, armed)

    with pytest.raises(CreditEnforcementRejected, match="already armed"):
        await _arm(clean)
    assert await _state_rows(clean) == (1, armed)


@pytest.mark.parametrize(
    "ahead", [timedelta(seconds=30), timedelta(hours=6), timedelta(days=60)]
)
async def test_a_future_timestamp_is_refused_and_writes_nothing(
    clean: _Harness, ahead: timedelta
) -> None:
    """Every hot-path reader treats a non-NULL boundary as enforced from the
    commit: a future boundary would not delay enforcement, it would only
    make earlier events non-observational."""

    await subscribed_builder(clean)
    with pytest.raises(CreditEnforcementRejected, match="cannot be future-dated"):
        await _arm(clean, at=datetime.now(timezone.utc) + ahead)
    assert await _state_rows(clean) == (0, None)


async def test_a_far_future_timestamp_does_not_pass_vacuously(
    clean: _Harness,
) -> None:
    """Today this world must be refused (two exactly bound candidate cycles
    -- a readback-only failure -- and unaccepted standalone traffic).  Evaluated 120 days ahead there would be
    no current cycle and no traffic in the window: the boundary must not be
    accepted that way."""

    await _two_bound_candidates(clean)
    standalone = await make_tenant(clean, None)
    await message_in(clean, standalone, NOW - timedelta(hours=1))
    with pytest.raises(CreditEnforcementRejected):
        await _arm(clean, at=NOW + timedelta(days=120))
    with pytest.raises(CreditEnforcementRejected, match="readiness is incomplete"):
        await _arm(clean)
    assert await _state_rows(clean) == (0, None)


async def test_the_gate_is_evaluated_at_the_database_clock(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clock seam is ``CreditRepository.database_now``: with it moved 40
    days ahead today's current cycle is no longer current (this world then
    arms) and the boundary is exactly that instant; an explicit ``at`` equal
    to it is the same boundary, one before it is refused."""

    await _two_bound_candidates(clean)
    with pytest.raises(CreditEnforcementRejected, match="readiness is incomplete"):
        await _arm(clean)

    shifted = NOW + timedelta(days=40)

    async def database_now(self: CreditRepository) -> datetime:
        return shifted

    monkeypatch.setattr(CreditRepository, "database_now", database_now)
    with pytest.raises(CreditEnforcementRejected, match="cannot be backdated"):
        await _arm(clean, at=NOW)
    assert await _arm(clean, at=shifted) == shifted
    assert await _state_rows(clean) == (1, shifted)


@pytest.mark.parametrize(
    "backdate", [timedelta(seconds=30), timedelta(days=1), timedelta(days=60)]
)
async def test_a_past_timestamp_is_refused_and_writes_nothing(
    clean: _Harness, backdate: timedelta
) -> None:
    await subscribed_builder(clean)
    with pytest.raises(CreditEnforcementRejected, match="cannot be backdated"):
        await _arm(clean, at=datetime.now(timezone.utc) - backdate)
    assert await _state_rows(clean) == (0, None)


# ---------------------------------------------------------------------------
# Refusals.
# ---------------------------------------------------------------------------


async def test_an_open_orphan_refuses(clean: _Harness) -> None:
    world = await subscribed_builder(clean)
    # An enforced decision whose trace effect was never written.
    await execute(
        clean,
        """
        INSERT INTO credit_ingress_claims (
            id, tenant_id, builder_id, source, principal_id, idempotency_key,
            interaction_id, payload_sha256, decision_version, mode
        ) VALUES (:id, :tenant, :builder, 'events', 'api_key:probe', 'k1',
                  :interaction, :sha, 1, 'enforced')
        """,
        id=uuid4(),
        tenant=world.tenant_id,
        builder=world.builder_id,
        interaction=uuid4(),
        sha="b" * 64,
    )
    await _refused(clean, "orphan reconciliation is incomplete")


async def test_readiness_false_refuses(clean: _Harness) -> None:
    await subscribed_builder(clean)
    await execute(
        clean,
        "INSERT INTO credit_revision_gaps (aggregate_kind, aggregate_id, "
        "first_missing_revision) VALUES ('topup_order', :id, 2)",
        id=uuid4(),
    )
    await _refused(clean, "readiness is incomplete")


async def test_consumer_lag_refuses_even_though_requests_are_served(
    clean: _Harness,
) -> None:
    await subscribed_builder(clean, revision=2, binding_revision=1)
    await _refused(clean, "readiness is incomplete")


@pytest.mark.parametrize(
    "status", ["readback_required", "non_billable_pending_readback"]
)
async def test_an_ambiguous_open_subscription_with_a_current_cycle_refuses(
    clean: _Harness, status: str
) -> None:
    """Even with an exactly matching binding: the row awaits a readback."""

    world = await subscribed_builder(clean)
    await set_subscription_status(clean, world.subscription_id, status)
    async with clean.sessions() as session:
        readback = await CreditRolloutService(session).readback_report(now=NOW)
        await session.rollback()
    assert readback.ambiguous_current_cycles == 1
    await _refused(clean, "readiness is incomplete")


@pytest.mark.parametrize(
    "status",
    ["active", "active_linked", "readback_required", "non_billable_pending_readback"],
)
async def test_every_open_status_with_an_unbound_current_cycle_refuses(
    clean: _Harness, status: str
) -> None:
    builder_id = await make_builder(clean)
    subscription_id = await make_subscription(clean, builder_id, status=status)
    await make_cycle(
        clean,
        builder_id,
        subscription_id,
        start=TODAY - timedelta(days=3),
        end=TODAY + timedelta(days=27),
    )
    async with clean.sessions() as session:
        readback = await CreditRolloutService(session).readback_report(now=NOW)
        await session.rollback()
    # The subscription is inside the gate (counted by the readback itself,
    # not only by the reconciliation's binding_lag).
    assert not readback.complete
    assert readback.missing_or_stale_bindings + readback.ambiguous_current_cycles == 1
    await _refused(clean, "readiness is incomplete")


@pytest.mark.parametrize("status", ["inactive", "cancelled"])
async def test_a_terminal_subscription_is_outside_the_readback(
    clean: _Harness, status: str
) -> None:
    builder_id = await make_builder(clean)
    subscription_id = await make_subscription(clean, builder_id, status=status)
    await make_cycle(
        clean,
        builder_id,
        subscription_id,
        start=TODAY - timedelta(days=3),
        end=TODAY + timedelta(days=27),
    )
    async with clean.sessions() as session:
        readback = await CreditRolloutService(session).readback_report(now=NOW)
        await session.rollback()
    assert readback.complete


async def test_two_candidate_cycles_refuse(clean: _Harness) -> None:
    world = await subscribed_builder(clean)
    await make_cycle(
        clean,
        world.builder_id,
        world.subscription_id,
        start=TODAY - timedelta(days=1),
        end=TODAY + timedelta(days=29),
    )
    await _refused(clean, "readiness is incomplete")


async def test_an_unaccepted_cutoff_refuses(clean: _Harness) -> None:
    await subscribed_builder(clean)
    standalone = await make_tenant(clean, None)
    await message_in(clean, standalone, NOW - timedelta(hours=1))
    await observation(clean, standalone, None, NOW - timedelta(hours=1))
    await _refused(clean, "cutoff report is unaccepted")


# ---------------------------------------------------------------------------
# expected_report_digest: the confirmation is inside the transaction.
# ---------------------------------------------------------------------------


async def test_a_digest_other_than_the_evaluated_report_refuses(
    clean: _Harness,
) -> None:
    """A world that would arm (no finding at all) still refuses a digest
    that is not the one its transaction computes."""

    await subscribed_builder(clean)
    assert (await _cutoff(clean)).findings == ()
    with pytest.raises(
        CreditEnforcementRejected, match="differs from the confirmed digest"
    ):
        await _arm(clean, digest="0" * 64)
    assert await _state_rows(clean) == (0, None)


async def test_an_accepted_report_that_changed_since_review_refuses(
    clean: _Harness,
) -> None:
    """The reviewed report is accepted; one more finding appears and is
    accepted too, so the gate alone would pass -- the digest refuses."""

    await subscribed_builder(clean)
    first = await make_tenant(clean, None)
    await message_in(clean, first, NOW - timedelta(hours=1))
    await observation(clean, first, None, NOW - timedelta(hours=1))
    reviewed = await _cutoff(clean)
    await _accept(
        clean,
        tenant_id=first,
        builder_id=None,
        reason="standalone",
        digest=reviewed.digest,
    )
    second = await make_tenant(clean, None)
    await message_in(clean, second, NOW - timedelta(hours=1))
    await observation(clean, second, None, NOW - timedelta(hours=1))
    changed = await _cutoff(clean)
    for tenant_id in (first, second):
        await _accept(
            clean,
            tenant_id=tenant_id,
            builder_id=None,
            reason="standalone",
            digest=changed.digest,
        )
    assert (await _cutoff(clean)).ready
    with pytest.raises(
        CreditEnforcementRejected, match="differs from the confirmed digest"
    ):
        await _arm(clean, digest=reviewed.digest)
    assert await _state_rows(clean) == (0, None)
    await _arm(clean, digest=changed.digest)
    assert (await _state_rows(clean))[0] == 1


async def test_omitting_the_confirmed_digest_is_a_type_error(
    clean: _Harness,
) -> None:
    """Required, keyword-only, no default: there is no call that arms
    without a confirmed report.  This world would otherwise arm."""

    await subscribed_builder(clean)
    async with clean.sessions() as session:
        with pytest.raises(TypeError, match="expected_report_digest"):
            await CreditRolloutService(session).arm_enforcement(  # type: ignore[call-arg]
                set_by="owner-decision"
            )
        await session.rollback()
    assert await _state_rows(clean) == (0, None)


async def _holding_the_rollout_lock(
    harness: _Harness,
) -> tuple[asyncio.Task[None], asyncio.Event]:
    """Another backend holding ``lock_rollout_control`` until released."""

    acquired, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(
        _locked_change(harness, (), acquired=acquired, release=release)
    )
    await asyncio.wait_for(acquired.wait(), timeout=20)
    return holder, release


@pytest.mark.parametrize(
    "digest",
    [None, "", "a" * 63, "a" * 65, "A" * 64, "g" * 64, " " + "a" * 63],
    ids=["none", "empty", "short", "long", "uppercase", "non-hex", "space"],
)
async def test_a_malformed_confirmed_digest_is_refused_before_the_lock(
    clean: _Harness, digest: str | None
) -> None:
    """While another backend holds the rollout lock, a malformed digest is
    refused at once: validated after the lock, the call would wait on it
    (the ``wait_for`` would time out instead of raising the refusal)."""

    await subscribed_builder(clean)
    holder, release = await _holding_the_rollout_lock(clean)
    try:
        with pytest.raises(
            CreditEnforcementRejected,
            match="^expected_report_digest is not a lowercase SHA-256$",
        ):
            await asyncio.wait_for(_arm(clean, digest=digest), timeout=10)
        assert await _lock_waiters(clean) == 0
    finally:
        release.set()
        await asyncio.wait_for(holder, timeout=20)
    assert await _state_rows(clean) == (0, None)


async def test_a_well_formed_wrong_digest_is_refused_inside_the_transaction(
    clean: _Harness,
) -> None:
    """The contrast: a well-formed digest passes the boundary check, waits
    for the rollout lock, and only the transaction's own report refuses it."""

    await subscribed_builder(clean)
    holder, release = await _holding_the_rollout_lock(clean)
    gate: asyncio.Task[datetime] | None = None
    try:
        gate = asyncio.create_task(_arm(clean, digest="0" * 64))
        await _until_blocked(clean, gate)
        release.set()
        await asyncio.wait_for(holder, timeout=20)
        with pytest.raises(
            CreditEnforcementRejected, match="differs from the confirmed digest"
        ):
            await asyncio.wait_for(gate, timeout=60)
    finally:
        release.set()
        await _settle(holder, *([gate] if gate is not None else []))
    assert await _state_rows(clean) == (0, None)


# ---------------------------------------------------------------------------
# accept_cutoff: an active super_admin, at the database clock.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "active"),
    [("super_admin", False), ("clinic_admin", True), ("investor", True), (None, True)],
    ids=["inactive-super-admin", "clinic-admin", "investor", "no-such-user"],
)
async def test_accept_cutoff_refuses_an_author_who_is_not_an_active_super_admin(
    clean: _Harness, role: str | None, active: bool
) -> None:
    standalone = await make_tenant(clean, None)
    author = uuid4() if role is None else await _owner(clean, role=role, active=active)
    with pytest.raises(CreditEnforcementRejected, match="not an active super_admin"):
        await _accept(
            clean,
            tenant_id=standalone,
            builder_id=None,
            reason="standalone",
            digest="a" * 64,
            author=author,
        )
    assert await scalar(clean, "SELECT count(*) FROM credit_cutoff_acceptances") == 0


async def test_accept_cutoff_stamps_the_database_clock(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seam is ``CreditRepository.database_now`` (the real clock seam of
    the ops suite is a shifted ``clock_timestamp()``)."""

    standalone = await make_tenant(clean, None)
    author = await _owner(clean)
    shifted = NOW + timedelta(days=3, hours=5)

    async def database_now(self: CreditRepository) -> datetime:
        return shifted

    monkeypatch.setattr(CreditRepository, "database_now", database_now)
    await _accept(
        clean,
        tenant_id=standalone,
        builder_id=None,
        reason="standalone",
        digest="a" * 64,
        author=author,
    )
    async with clean.engine.connect() as connection:
        (row,) = (
            await connection.execute(
                text(
                    "SELECT accepted_at, accepted_on, author_user_id "
                    "FROM credit_cutoff_acceptances"
                )
            )
        ).all()
    assert row.accepted_at == shifted
    assert row.accepted_on == civil_today(shifted)
    assert row.author_user_id == author


# ---------------------------------------------------------------------------
# accept_cutoff: bound to the tenant and to the report digest.
# ---------------------------------------------------------------------------


async def test_acceptance_counts_only_for_its_tenant_and_digest(
    clean: _Harness,
) -> None:
    world = await subscribed_builder(clean)
    standalone = await make_tenant(clean, None)
    await message_in(clean, standalone, NOW - timedelta(hours=1))
    await observation(clean, standalone, None, NOW - timedelta(hours=1))
    report = await _cutoff(clean)
    assert [(f.tenant_id, f.builder_id, f.reason) for f in report.findings] == [
        (standalone, None, "standalone")
    ]

    # Another tenant (with the same reason and digest) does not count.
    await _accept(
        clean,
        tenant_id=world.tenant_id,
        builder_id=None,
        reason="standalone",
        digest=report.digest,
    )
    # Nor does this tenant under another report digest.
    await _accept(
        clean,
        tenant_id=standalone,
        builder_id=None,
        reason="standalone",
        digest="b" * 64,
    )
    assert not (await _cutoff(clean)).ready
    await _refused(clean, "cutoff report is unaccepted")

    await _accept(
        clean,
        tenant_id=standalone,
        builder_id=None,
        reason="standalone",
        digest=report.digest,
    )
    assert (await _cutoff(clean)).ready
    await _arm(clean)
    assert (await _state_rows(clean))[0] == 1


async def test_new_traffic_changes_the_digest_and_voids_the_acceptance(
    clean: _Harness,
) -> None:
    await subscribed_builder(clean)
    standalone = await make_tenant(clean, None)
    await message_in(clean, standalone, NOW - timedelta(hours=1))
    await observation(clean, standalone, None, NOW - timedelta(hours=1))
    report = await _cutoff(clean)
    await _accept(
        clean,
        tenant_id=standalone,
        builder_id=None,
        reason="standalone",
        digest=report.digest,
    )
    other = await make_tenant(clean, None)
    await message_in(clean, other, NOW - timedelta(hours=1))
    await observation(clean, other, None, NOW - timedelta(hours=1))
    changed = await _cutoff(clean)
    assert changed.digest != report.digest and not changed.ready


# ---------------------------------------------------------------------------
# Cutoff builder: the current tenant row wins.
# ---------------------------------------------------------------------------


async def test_a_tenant_detached_inside_the_window_is_standalone(
    clean: _Harness,
) -> None:
    """Its observation recorded the old (covered) builder; the tenant row
    now has none, so the report must name it standalone."""

    world = await subscribed_builder(clean)
    await message_in(clean, world.tenant_id, NOW - timedelta(days=2))
    await observation(clean, world.tenant_id, world.builder_id, NOW - timedelta(days=2))
    assert (await _cutoff(clean)).findings == ()

    await execute(
        clean,
        "UPDATE tenants SET builder_id = NULL WHERE id = :id",
        id=world.tenant_id,
    )
    findings = (await _cutoff(clean)).findings
    assert [(f.tenant_id, f.builder_id, f.reason) for f in findings] == [
        (world.tenant_id, None, "standalone")
    ]


async def test_a_tenant_reparented_inside_the_window_uses_its_new_builder(
    clean: _Harness,
) -> None:
    covered = await subscribed_builder(clean)
    uncovered = await make_builder(clean)
    await message_in(clean, covered.tenant_id, NOW - timedelta(days=2))
    await observation(
        clean, covered.tenant_id, covered.builder_id, NOW - timedelta(days=2)
    )
    await execute(
        clean,
        "UPDATE tenants SET builder_id = :b WHERE id = :id",
        b=uncovered,
        id=covered.tenant_id,
    )
    findings = (await _cutoff(clean)).findings
    assert [(f.tenant_id, f.builder_id, f.reason) for f in findings] == [
        (covered.tenant_id, uncovered, "builder_without_coverage")
    ]


async def test_a_deleted_tenant_falls_back_to_its_recorded_builder(
    clean: _Harness,
) -> None:
    uncovered = await make_builder(clean)
    tenant_id = await make_tenant(clean, uncovered)
    await observation(clean, tenant_id, uncovered, NOW - timedelta(days=2))
    await execute(clean, "DELETE FROM tenants WHERE id = :id", id=tenant_id)
    findings = (await _cutoff(clean)).findings
    assert [(f.tenant_id, f.builder_id, f.reason) for f in findings] == [
        (tenant_id, uncovered, "builder_without_coverage")
    ]


# ---------------------------------------------------------------------------
# The cutoff report, one AC clause per case.
# ---------------------------------------------------------------------------

_THIS_MONTH = TODAY.replace(day=1)
_PREVIOUS_MONTH = (_THIS_MONTH - timedelta(days=1)).replace(day=1)
# Noon (São Paulo) of the first day of the previous month: inside the window.
PREVIOUS_MONTH_NOON = start_of_civil_day(_PREVIOUS_MONTH) + timedelta(hours=12)
# Noon of the last day before the previous month: outside the window.
BEFORE_WINDOW_NOON = start_of_civil_day(
    _PREVIOUS_MONTH - timedelta(days=1)
) + timedelta(hours=12)


async def _traffic(
    harness: _Harness,
    tenant_id: UUID,
    builder_id: UUID | None,
    at: datetime,
    **observation_kwargs: object,
) -> None:
    await message_in(harness, tenant_id, at)
    await observation(harness, tenant_id, builder_id, at, **observation_kwargs)


async def _unknown_claim(
    harness: _Harness, tenant_id: UUID, builder_id: UUID, source: str
) -> None:
    await execute(
        harness,
        """
        INSERT INTO credit_ingress_claims (
            id, tenant_id, builder_id, source, principal_id, provider_message_id,
            interaction_id, payload_sha256, decision_version, mode, created_at
        ) VALUES (:id, :tenant, :builder, :source, :principal, :message,
                  :interaction, :sha, 1, 'observational', :at)
        """,
        id=uuid4(),
        tenant=tenant_id,
        builder=builder_id,
        source=source,
        principal=f"{source}:{tenant_id}",
        message=f"msg-{uuid4().hex}",
        interaction=uuid4(),
        sha="d" * 64,
        at=NOW - timedelta(hours=2),
    )


def _findings(report: CutoffReport) -> list[tuple[UUID, UUID | None, str]]:
    return [(f.tenant_id, f.builder_id, f.reason) for f in report.findings]


async def test_previous_month_standalone_traffic_is_listed(clean: _Harness) -> None:
    standalone = await make_tenant(clean, None)
    await _traffic(clean, standalone, None, PREVIOUS_MONTH_NOON)
    assert _findings(await _cutoff(clean)) == [(standalone, None, "standalone")]


async def test_traffic_older_than_the_two_months_is_not_listed(
    clean: _Harness,
) -> None:
    standalone = await make_tenant(clean, None)
    await _traffic(clean, standalone, None, BEFORE_WINDOW_NOON)
    assert _findings(await _cutoff(clean)) == []


async def test_current_month_builder_without_coverage_is_listed(
    clean: _Harness,
) -> None:
    builder_id = await make_builder(clean)
    tenant_id = await make_tenant(clean, builder_id)
    await _traffic(clean, tenant_id, builder_id, NOW - timedelta(hours=1))
    assert _findings(await _cutoff(clean)) == [
        (tenant_id, builder_id, "builder_without_coverage")
    ]


@pytest.mark.parametrize("source", ["uazapi", "evolution"])
async def test_uazapi_and_evolution_traffic_is_listed(
    clean: _Harness, source: str
) -> None:
    world = await subscribed_builder(clean)
    await _traffic(clean, world.tenant_id, world.builder_id, NOW - timedelta(hours=1))
    await _unknown_claim(clean, world.tenant_id, world.builder_id, source)
    assert _findings(await _cutoff(clean)) == [
        (world.tenant_id, world.builder_id, "unknown_unauthenticated_traffic")
    ]


async def test_unknown_unauthenticated_observation_is_listed(
    clean: _Harness,
) -> None:
    world = await subscribed_builder(clean)
    await _traffic(
        clean,
        world.tenant_id,
        world.builder_id,
        NOW - timedelta(hours=1),
        origin="unknown",
        authenticated=False,
    )
    assert _findings(await _cutoff(clean)) == [
        (world.tenant_id, world.builder_id, "unknown_unauthenticated_traffic")
    ]


async def test_old_unknown_message_rows_do_not_list_a_covered_tenant(
    clean: _Harness,
) -> None:
    """``events.interaction_origin`` is 'unknown' for every row written before
    the origin deploy (and immutable); it is never read as unknown traffic."""

    world = await subscribed_builder(clean)
    await _traffic(clean, world.tenant_id, world.builder_id, NOW - timedelta(days=1))
    assert (
        await scalar(
            clean,
            "SELECT count(*) FROM events WHERE tenant_id = :t "
            "AND interaction_origin = 'unknown'",
            t=world.tenant_id,
        )
        == 1
    )
    assert _findings(await _cutoff(clean)) == []


@pytest.mark.parametrize(
    ("topup", "covered"),
    [
        ({}, True),
        ({"reversed_mc": 500_000}, False),
        ({"expires_at": NOW - timedelta(seconds=1)}, False),
        ({"accepted": False}, False),
    ],
    ids=["reconciled", "fully-reversed", "expired", "unreconciled"],
)
async def test_a_topup_only_tenant_is_covered_only_by_a_live_reconciled_lot(
    clean: _Harness, topup: dict[str, object], covered: bool
) -> None:
    builder_id = await make_builder(clean)
    tenant_id = await make_tenant(clean, builder_id)
    await make_topup(clean, builder_id, **topup)  # type: ignore[arg-type]
    await _traffic(clean, tenant_id, builder_id, NOW - timedelta(hours=1))
    expected = [] if covered else [(tenant_id, builder_id, "builder_without_coverage")]
    assert _findings(await _cutoff(clean)) == expected


async def test_after_an_accepted_cutoff_each_tenant_follows_its_normal_rule(
    clean: _Harness,
) -> None:
    """Accepted is not exempt: the uncovered builder reaches the normal
    B+C path (served, debt), the standalone tenant is ``credit_pending``,
    and Uazapi/Evolution traffic stays unauthenticated (never enforced, so
    never debited)."""

    covered = await subscribed_builder(clean)
    uncovered = await make_builder(clean)
    uncovered_tenant = await make_tenant(clean, uncovered)
    standalone = await make_tenant(clean, None)
    hour_ago = NOW - timedelta(hours=1)
    await _traffic(clean, uncovered_tenant, uncovered, hour_ago)
    await _traffic(clean, standalone, None, hour_ago)
    await _traffic(clean, covered.tenant_id, covered.builder_id, hour_ago)
    await _unknown_claim(clean, covered.tenant_id, covered.builder_id, "uazapi")
    report = await _cutoff(clean)
    assert _findings(report) == sorted(
        [
            (uncovered_tenant, uncovered, "builder_without_coverage"),
            (standalone, None, "standalone"),
            (covered.tenant_id, covered.builder_id, "unknown_unauthenticated_traffic"),
        ],
        key=lambda row: (str(row[0]), row[2]),
    )
    for tenant_id, builder_id, reason in _findings(report):
        await _accept(
            clean,
            tenant_id=tenant_id,
            builder_id=builder_id,
            reason=reason,
            digest=report.digest,
        )
    await _arm(clean)

    assert await _precheck(clean, uncovered_tenant) == "ok"
    assert await _precheck(clean, standalone) == "credit_pending"
    assert await _precheck(clean, covered.tenant_id) == "ok"
    for source in ("uazapi", "evolution"):
        identity = credit_identity(source, covered.tenant_id)
        assert identity is not None
        principal = derive_credit_principal(
            identity,
            TenantBuilder(
                tenant_id=covered.tenant_id,
                builder_id=covered.builder_id,
                is_active=True,
            ),
        )
        assert principal.authenticated is False


async def _precheck(harness: _Harness, tenant_id: UUID) -> str:
    async with harness.sessions() as session:
        return await precheck_events(
            session,
            EventsEnforcement(
                tenant_id=tenant_id,
                session_id=uuid4(),
                principal_id=f"api_key:{tenant_id}",
                payload_sha256="d" * 64,
                idempotency_key="probe",
                key_problem=None,
            ),
        )


# ---------------------------------------------------------------------------
# A concurrent subscription change serializes under lock_rollout_control.
# ---------------------------------------------------------------------------


async def _locked_change(
    harness: _Harness,
    statements: tuple[tuple[str, dict[str, object]], ...],
    *,
    acquired: asyncio.Event | None = None,
    release: asyncio.Event | None = None,
) -> None:
    """An ASAAS-webhook-shaped writer: take the rollout lock, mutate, commit."""

    engine, sessions = harness.factory()
    try:
        async with sessions() as session:
            await CreditRepository(session).lock_rollout_control(
                environment=settings.environment
            )
            if acquired is not None:
                acquired.set()
            await session.execute(text(PLANT))
            for sql, params in statements:
                await session.execute(text(sql), params)
            if release is not None:
                await release.wait()
            await session.commit()
    finally:
        await engine.dispose()


async def _lock_waiters(harness: _Harness) -> int:
    """Backends of this database blocked on a heavyweight lock right now."""

    return int(
        await scalar(
            harness,
            "SELECT count(*) FROM pg_stat_activity WHERE datname = "
            "current_database() AND wait_event_type = 'Lock' "
            "AND pid <> pg_backend_pid()",
        )
    )


async def _until_blocked(harness: _Harness, task: asyncio.Task[object]) -> None:
    """Poll until a backend waits on a lock (the task's own); never a fixed
    negative sleep.  Fails if the task finished first (it did not wait)."""

    deadline = asyncio.get_running_loop().time() + 20
    while await _lock_waiters(harness) == 0:
        assert not task.done(), "it finished without waiting for the lock"
        assert asyncio.get_running_loop().time() < deadline, "never blocked"
        await asyncio.sleep(0.05)
    assert not task.done()


async def _settle(*tasks: asyncio.Task[object]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_a_change_committed_before_the_gate_is_seen(clean: _Harness) -> None:
    """The writer holds the lock with the change uncommitted; the gate blocks
    on it (seen in pg_stat_activity) and its reread sees the committed
    ``readback_required``."""

    world = await subscribed_builder(clean)
    acquired, release = asyncio.Event(), asyncio.Event()
    writer = asyncio.create_task(
        _locked_change(
            clean,
            (
                (
                    "UPDATE billing_subscriptions SET status = 'readback_required' "
                    "WHERE id = :id",
                    {"id": world.subscription_id},
                ),
            ),
            acquired=acquired,
            release=release,
        )
    )
    gate: asyncio.Task[datetime] | None = None
    try:
        await asyncio.wait_for(acquired.wait(), timeout=20)
        gate = asyncio.create_task(_arm(clean))
        await _until_blocked(clean, gate)
        release.set()
        await asyncio.wait_for(writer, timeout=20)
        with pytest.raises(CreditEnforcementRejected, match="readiness is incomplete"):
            await asyncio.wait_for(gate, timeout=60)
    finally:
        release.set()
        await _settle(writer, *([gate] if gate is not None else []))
    assert await _state_rows(clean) == (0, None)


async def test_a_change_after_the_gate_waits_and_is_captured_by_id(
    clean: _Harness,
) -> None:
    """The gate holds the lock until its commit; the writer blocks on it
    (seen in pg_stat_activity), lands after it, and the by-id reread of the
    current cycle sees it."""

    world = await subscribed_builder(clean)
    writer: asyncio.Task[None] | None = None
    try:
        async with clean.sessions() as gate:
            service = CreditRolloutService(gate)
            reviewed = await service.cutoff_report(
                now=await CreditRepository(gate).database_now()
            )
            await service.arm_enforcement(
                set_by="owner-decision", expected_report_digest=reviewed.digest
            )
            writer = asyncio.create_task(
                _locked_change(
                    clean,
                    (
                        (
                            "UPDATE billing_subscription_cycles "
                            "SET financial_revision = 2 WHERE id = :id",
                            {"id": world.cycle_id},
                        ),
                    ),
                )
            )
            await _until_blocked(clean, writer)
            await gate.commit()
        await asyncio.wait_for(writer, timeout=20)
    finally:
        if writer is not None:
            await _settle(writer)

    count, armed = await _state_rows(clean)
    assert count == 1 and armed is not None
    async with clean.sessions() as session:
        rollout = CreditRolloutService(session)
        readback = await rollout.readback_report(now=NOW)
        coverage = await rollout.binding_coverage(world.builder_id, at=NOW)
        await session.rollback()
    # Captured: the binding is now behind its cycle -- readback false, the
    # request still served (lag).
    assert readback.missing_or_stale_bindings == 1
    assert coverage.monthly_binding and not coverage.reconciliation_failure


# ---------------------------------------------------------------------------
# Fence: no route, no worker, no script reaches the single writers.
# ---------------------------------------------------------------------------

_FENCED = {
    "arm_enforcement": "src/services/credit_rollout.py",
    "record_capability_readiness": "src/services/credit_rollout.py",
    "accept_cutoff": "src/services/credit_rollout.py",
    "PurgeExecutor": "src/services/content_retention.py",
}
# The one-off ECS task of asaas-rollout-opening Group 6 is the ONLY module
# allowed to reach these two writers; nothing else, and nothing else for it.
_OPS_MODULE = "src/ops/credit_enforcement.py"
_OPS_CALLERS = {"arm_enforcement": _OPS_MODULE, "accept_cutoff": _OPS_MODULE}
# A source text for the AST probes below: parsed, never executed (the digest
# is only there so the probe has the shape of the one real call).
_ARM_PROBE = (
    "async def f(s):\n"
    "    await s.arm_enforcement(set_by='x', expected_report_digest='a' * 64)\n"
)
# Only this function may construct the enforcement row or set its boundary.
_STATE_WRITER = ("src/services/credit_rollout.py", "arm_enforcement")


def _exempt(tree: ast.Module) -> set[int]:
    """Docstrings and the ``__all__`` strings: the owner's declarations."""

    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                exempt.add(id(body[0].value))
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        ):
            exempt.update(id(inner) for inner in ast.walk(node.value))
    return exempt


def _folded(node: ast.AST) -> str | None:
    """The value of a string built only from literals (``+`` and f-strings
    without placeholders), else ``None``."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _folded(node.left), _folded(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.FormattedValue) and node.format_spec is None:
        return _folded(node.value)
    if isinstance(node, ast.JoinedStr):
        parts = [_folded(value) for value in node.values]
        return None if any(part is None for part in parts) else "".join(parts)  # type: ignore[arg-type]
    return None


def _references(path: Path, names: set[str], root: Path = ROOT) -> list[str]:
    """Every name, attribute, import alias or string literal naming one of
    *names* in *path* (a string covers ``getattr`` and ``import_module``).

    Applied to the owner module too: only the ``def``/``class`` statement
    itself (a str field, not a reference), its docstrings and ``__all__``
    are allowed there, so a worker in the same module calling the writer is
    caught."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    exempt = _exempt(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if id(node) in exempt:
            continue
        name: str | None = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.alias):
            name = node.name.rsplit(".", 1)[-1]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # getattr(service, "arm_enforcement") and import_module literals.
            name = node.value if node.value in names else None
        elif isinstance(node, (ast.BinOp, ast.JoinedStr)):
            # A name built from literals: "arm_" + "enforcement".
            folded = _folded(node)
            name = folded if folded in names else None
        if name in names:
            found.append(f"{path.relative_to(root)}:{getattr(node, 'lineno', 0)}")
    return found


def _state_writes(path: Path, root: Path, allowed: str | None) -> list[str]:
    """``CreditEnforcementState(...)`` or ``.credit_enforcement_at =`` outside
    the function named *allowed*."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []

    def visit(node: ast.AST, function: str | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        inside = allowed is not None and function == allowed
        if not inside:
            if isinstance(node, ast.Call) and (
                (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "CreditEnforcementState"
                )
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "CreditEnforcementState"
                )
            ):
                found.append(f"{path.relative_to(root)}:{node.lineno}:construct")
            targets: list[ast.AST] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for target in targets:
                for inner in ast.walk(target):
                    if (
                        isinstance(inner, ast.Attribute)
                        and inner.attr == "credit_enforcement_at"
                    ):
                        found.append(f"{path.relative_to(root)}:{node.lineno}:assign")
            if isinstance(node, ast.Call) and any(
                keyword.arg == "credit_enforcement_at" for keyword in node.keywords
            ):
                found.append(f"{path.relative_to(root)}:{node.lineno}:keyword")
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, None)
    return found


# Every Python tree that ships or runs in a task: the API, the scripts, the
# migrations (``alembic/env.py`` runs as a production one-off task), the
# config package and the vendored packages.  ``tests`` is the only exclusion.
_PYTHON_TREES = ("src", "scripts", "alembic", "config", "packages")


def _sources() -> list[Path]:
    return sorted(
        path
        for tree in _PYTHON_TREES
        for path in ROOT.glob(f"{tree}/**/*.py")
        if "node_modules" not in path.parts
    )


# Outside Python: a shell script, the Makefile, a workflow, a compose file or
# infrastructure code could start the task (``python -m
# src.ops.credit_enforcement``) or name a writer.  Only ``docs`` (the runbook),
# ``tests`` and the planning notes in ``.genie`` may mention them.
_TEXT_SCOPE_EXCLUDED = frozenset({"docs", "tests", ".genie"})
# The files are the repository's own: ``git ls-files -co --exclude-standard``
# (tracked, plus untracked-but-not-ignored, so a NEW script is still seen and
# the artifacts a test run writes -- ``.pytest_cache``, ``htmlcov`` -- are
# not).  Without git, the walk below skips every artifact directory the
# ``.gitignore`` names (it never skips the sweep itself).
_ARTIFACT_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        "htmlcov",
        ".mypy_cache",
        ".dmypy",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".hypothesis",
        ".eggs",
        "dist",
        "build",
        "sdist",
        "develop-eggs",
        ".venv",
        "venv",
        "venv.bak",
        ".pybuilder",
    }
)
_ARTIFACT_FILES = re.compile(
    r"\.coverage(\..*)?|coverage\.xml|\.dmypy\.json|dmypy\.json"
)
_TEXT_FENCE = re.compile(
    r"(?<![A-Za-z0-9_])(?:arm_enforcement|accept_cutoff)(?![A-Za-z0-9_])"
    r"|credit_enforcement(?![A-Za-z0-9_])"
)
# A legitimate future mention (for example an alarm filter on the task's
# logger ``src.ops.credit_enforcement`` in ``infra/``) is declared here, by
# path and EXACT stripped line, with its reason in a comment; any other line
# of that file is still a finding.  Empty today.
_TEXT_ALLOWED: dict[str, frozenset[str]] = {}


def _git_files(root: Path, git: str) -> list[str] | None:
    """The repository's files under *root* per git, or ``None`` when git is
    not installed or *root* is not inside a work tree."""

    try:
        listed = subprocess.run(
            [git, "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (FileNotFoundError, PermissionError):
        return None
    if listed.returncode != 0:
        return None
    return [name for name in listed.stdout.decode("utf-8").split("\0") if name]


def _walked_files(root: Path) -> list[str]:
    found: list[str] = []
    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = [
            d
            for d in subdirectories
            if d not in _ARTIFACT_DIRS and not d.endswith(".egg-info")
        ]
        base = Path(directory).relative_to(root)
        found.extend(
            (base / name).as_posix()
            for name in files
            if not _ARTIFACT_FILES.fullmatch(name)
        )
    return found


def _text_sources(root: Path = ROOT, *, git: str = "git") -> list[Path]:
    names = _git_files(root, git)
    if names is None:
        names = _walked_files(root)
    return sorted(
        root / name
        for name in names
        if not name.endswith(".py")
        and name.split("/", 1)[0] not in _TEXT_SCOPE_EXCLUDED
        and not set(name.split("/")) & {"node_modules", "__pycache__"}
        and (root / name).is_file()
    )


def _text_mentions(
    paths: list[Path],
    root: Path,
    allowed: dict[str, frozenset[str]] | None = None,
) -> list[str]:
    allowances = _TEXT_ALLOWED if allowed is None else allowed
    found: list[str] = []
    for path in paths:
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary assets
        relative = path.relative_to(root).as_posix()
        for number, line in enumerate(content.splitlines(), start=1):
            if _TEXT_FENCE.search(line) and line.strip() not in allowances.get(
                relative, frozenset()
            ):
                found.append(f"{relative}:{number}")
    return found


def _service_names(tree: ast.Module) -> set[str]:
    """Local names bound to ``CreditRolloutService`` or to an instance of it
    (import alias, or ``x = CreditRolloutService(...)`` / ``x = Alias(...)``)."""

    names = {"CreditRolloutService"}
    for node in ast.walk(tree):
        if isinstance(node, ast.alias) and node.name.endswith("CreditRolloutService"):
            names.add(node.asname or node.name.rsplit(".", 1)[-1])
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id in names
        ):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _dynamic_service_getattrs(path: Path, root: Path = ROOT) -> list[str]:
    """``getattr(<the service or an instance>, <not a literal>)``: a writer
    reached by a computed name the reference analysis cannot read."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    service = _service_names(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and _folded(node.args[1]) is None
            and any(
                (isinstance(inner, ast.Name) and inner.id in service)
                or (
                    isinstance(inner, ast.Attribute)
                    and inner.attr == "CreditRolloutService"
                )
                for inner in ast.walk(node.args[0])
            )
        ):
            found.append(f"{path.relative_to(root).as_posix()}:{node.lineno}")
    return found


def _fence_offenders(paths: list[Path], root: Path) -> list[str]:
    """References to a fenced name outside its one allowed caller."""

    offenders: list[str] = []
    for path in paths:
        relative = path.relative_to(root).as_posix()
        for name in _FENCED:
            if _OPS_CALLERS.get(name) == relative:
                continue
            offenders.extend(_references(path, {name}, root))
    return offenders


def test_the_single_writers_have_no_caller_but_the_ops_task() -> None:
    assert _fence_offenders(_sources(), ROOT) == []


def test_the_ops_task_calls_each_writer_exactly_once() -> None:
    """The allowance is not vacuous: the task really is the caller, and it
    reaches neither of the other fenced names."""

    path = ROOT / _OPS_MODULE
    assert len(_references(path, {"arm_enforcement"})) == 1
    assert len(_references(path, {"accept_cutoff"})) == 1
    assert _references(path, {"record_capability_readiness", "PurgeExecutor"}) == []


def _calls(path: Path, name: str) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == name
    ]


def test_the_ops_task_arms_at_the_database_clock_with_the_confirmed_digest() -> None:
    (call,) = _calls(ROOT / _OPS_MODULE, "arm_enforcement")
    assert call.args == []
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    assert set(keywords) == {"set_by", "at", "expected_report_digest"}
    at = keywords["at"]
    assert isinstance(at, ast.Constant) and at.value is None
    # Neither omitted nor a literal: the operator's --report-digest.
    digest = keywords["expected_report_digest"]
    assert not isinstance(digest, ast.Constant)
    assert ast.unparse(digest) == "args.report_digest"
    assert not isinstance(keywords["set_by"], ast.Constant)


def _imports_ops_module(path: Path, root: Path = ROOT) -> bool:
    """An import of the task module in any form; prose in docstrings (the
    owners documenting their one caller) is not an import."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = ".".join(path.relative_to(root).with_suffix("").parts[:-1])
    docstrings = _exempt(tree)
    for node in ast.walk(tree):
        if id(node) in docstrings:
            continue
        if isinstance(node, ast.Import) and any(
            alias.name.startswith("src.ops.credit_enforcement") for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                base = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                module = ".".join([*base, *([module] if module else [])])
            if module.startswith("src.ops.credit_enforcement") or (
                module == "src.ops"
                and any(alias.name == "credit_enforcement" for alias in node.names)
            ):
                return True
        if isinstance(node, (ast.Constant, ast.BinOp, ast.JoinedStr)):
            # A literal, or a string built from literals ("src.ops." + ...).
            folded = _folded(node)
            if folded is not None and "src.ops.credit_enforcement" in folded:
                return True
    return False


def test_no_route_worker_or_script_imports_the_ops_task() -> None:
    """Deliverable 5: the task is reachable only as ``python -m``.  The
    module itself builds no router either."""

    importers = [
        path.relative_to(ROOT).as_posix()
        for path in _sources()
        if path.relative_to(ROOT).as_posix() != _OPS_MODULE
        and _imports_ops_module(path)
    ]
    assert importers == []
    tree = ast.parse((ROOT / _OPS_MODULE).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    } | {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not any(name.startswith(("fastapi", "starlette")) for name in imported)
    assert not any(
        "router" in name.lower() or "route" in name.lower() for name in imported
    )


@pytest.mark.parametrize(
    ("relative", "source", "caught"),
    [
        (
            _OPS_MODULE,
            _ARM_PROBE,
            False,
        ),
        (
            _OPS_MODULE,
            "async def f(s):\n    await s.accept_cutoff(reason='x')\n",
            False,
        ),
        (
            _OPS_MODULE,
            "async def f(s):\n    await s.record_capability_readiness()\n",
            True,
        ),
        (
            _OPS_MODULE,
            "from src.services.content_retention import PurgeExecutor\n",
            True,
        ),
        (
            "src/ops/other.py",
            _ARM_PROBE,
            True,
        ),
        (
            "src/api/routes/credit_ops.py",
            "def f(s):\n    return getattr(s, 'accept_cutoff')\n",
            True,
        ),
        (
            "scripts/credit_enforcement.py",
            _ARM_PROBE,
            True,
        ),
    ],
)
def test_the_allowance_is_by_exact_path_and_name(
    tmp_path: Path, relative: str, source: str, caught: bool
) -> None:
    probe = tmp_path / relative
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(source, encoding="utf-8")
    assert bool(_fence_offenders([probe], tmp_path)) is caught


@pytest.mark.parametrize(
    "source",
    [
        "import src.ops.credit_enforcement\n",
        "from src.ops import credit_enforcement\n",
        "from src.ops.credit_enforcement import amain\n",
        "import importlib\nm = importlib.import_module('src.ops.credit_enforcement')\n",
    ],
)
def test_the_import_analysis_sees_every_form(tmp_path: Path, source: str) -> None:
    probe = tmp_path / "src" / "api" / "probe.py"
    probe.parent.mkdir(parents=True)
    probe.write_text(source, encoding="utf-8")
    assert _imports_ops_module(probe, tmp_path)


def test_the_import_analysis_sees_a_relative_import(tmp_path: Path) -> None:
    probe = tmp_path / "src" / "ops" / "probe.py"
    probe.parent.mkdir(parents=True)
    probe.write_text("from . import credit_enforcement\n", encoding="utf-8")
    assert _imports_ops_module(probe, tmp_path)
    # The credit model module of the same name is not the task.
    probe.write_text(
        "from src.models.credit_enforcement import CreditCutoffAcceptance\n",
        encoding="utf-8",
    )
    assert not _imports_ops_module(probe, tmp_path)


def test_only_arm_enforcement_writes_the_enforcement_row() -> None:
    offenders: list[str] = []
    for path in _sources():
        relative = path.relative_to(ROOT).as_posix()
        allowed = _STATE_WRITER[1] if relative == _STATE_WRITER[0] else None
        offenders.extend(_state_writes(path, ROOT, allowed))
    assert offenders == []
    # The allowed writer really is where the sweep says it is.
    owner = ROOT / _STATE_WRITER[0]
    assert _state_writes(owner, ROOT, None)


@pytest.mark.parametrize(
    "source",
    [
        _ARM_PROBE,
        "def f(s):\n    return getattr(s, 'record_capability_readiness')\n",
        "from src.services.content_retention import PurgeExecutor\n",
        "import src.services.content_retention.PurgeExecutor\n",
        "from src.services.credit_rollout import accept_cutoff as innocuous\n",
        # The owner module itself: a worker beside the writer calling it.
        '"""Module."""\n__all__ = ["CreditRolloutService"]\n'
        "class CreditRolloutService:\n"
        "    async def record_capability_readiness(self):\n"
        '        """Writer."""\n'
        "async def run_worker_once(session):\n"
        "    await CreditRolloutService(session).record_capability_readiness()\n",
        "class PurgeExecutor:\n    pass\n\n\ndef build():\n"
        "    return PurgeExecutor()\n",
    ],
)
def test_the_fence_analysis_sees_every_reference_form(
    tmp_path: Path, source: str
) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(source, encoding="utf-8")
    assert _references(probe, set(_FENCED), root=tmp_path)


def test_the_owner_declarations_alone_are_allowed(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        '"""Mentions arm_enforcement in prose."""\n'
        '__all__ = ["PurgeExecutor"]\n'
        "class PurgeExecutor:\n"
        '    """PurgeExecutor."""\n'
        "    async def arm_enforcement(self):\n"
        '        """accept_cutoff"""\n',
        encoding="utf-8",
    )
    assert _references(probe, set(_FENCED), root=tmp_path) == []


@pytest.mark.parametrize(
    "source",
    [
        "def f(s):\n    s.add(CreditEnforcementState(id=1))\n",
        "def f(m):\n    return m.CreditEnforcementState(id=1)\n",
        "def f(row, at):\n    row.credit_enforcement_at = at\n",
        "def f(t, at):\n    return update(t).values(credit_enforcement_at=at)\n",
    ],
)
def test_the_state_writer_analysis_sees_every_write_form(
    tmp_path: Path, source: str
) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(source, encoding="utf-8")
    assert _state_writes(probe, tmp_path, "arm_enforcement")


def test_the_fence_sweep_covers_routes_workers_and_scripts() -> None:
    swept = {path.relative_to(ROOT).as_posix() for path in _sources()}
    assert {
        "src/api/main.py",
        "src/api/routes/admin_credits.py",
        "src/services/credit_rollout.py",
        "src/services/content_retention.py",
        "alembic/env.py",
    } <= swept
    assert any(name.startswith("scripts/") for name in swept)
    assert any(name.startswith("alembic/versions/") for name in swept)
    assert not any(name.startswith("tests/") for name in swept)


def test_no_writer_is_reached_through_a_computed_getattr() -> None:
    offenders: list[str] = []
    for path in _sources():
        offenders.extend(_dynamic_service_getattrs(path))
    assert offenders == []


@pytest.mark.parametrize(
    "source",
    [
        "from src.services.credit_rollout import CreditRolloutService as _S\n"
        "async def f(session, name):\n"
        "    return await getattr(_S(session), name)()\n",
        "from src.services import credit_rollout\n"
        "def f(session, name):\n"
        "    return getattr(credit_rollout.CreditRolloutService(session), name)\n",
        "from src.services.credit_rollout import CreditRolloutService\n"
        "def f(session, verb):\n"
        "    service = CreditRolloutService(session)\n"
        "    return getattr(service, verb + '_enforcement')\n",
    ],
    ids=["alias-call", "module-attribute", "instance-variable"],
)
def test_the_computed_getattr_analysis_sees_every_form(
    tmp_path: Path, source: str
) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(source, encoding="utf-8")
    assert _dynamic_service_getattrs(probe, tmp_path)


def test_a_computed_getattr_on_anything_else_is_not_a_finding(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        "def f(decision, name):\n    return getattr(decision, name, None)\n",
        encoding="utf-8",
    )
    assert _dynamic_service_getattrs(probe, tmp_path) == []


@pytest.mark.parametrize(
    "source",
    [
        "def f(s):\n    return getattr(s, 'arm_' + 'enforcement')\n",
        "def f(s):\n    return getattr(s, f'accept_{\"cutoff\"}')\n",
    ],
    ids=["concatenation", "f-string"],
)
def test_a_writer_name_built_from_literals_is_a_reference(
    tmp_path: Path, source: str
) -> None:
    probe = tmp_path / "src" / "workers" / "probe.py"
    probe.parent.mkdir(parents=True)
    probe.write_text(source, encoding="utf-8")
    assert _fence_offenders([probe], tmp_path)


def test_a_task_module_name_built_from_literals_is_an_import(tmp_path: Path) -> None:
    probe = tmp_path / "scripts" / "probe.py"
    probe.parent.mkdir(parents=True)
    probe.write_text(
        "import subprocess\n"
        "def main(digest):\n"
        "    subprocess.run(['python', '-m', 'src.ops.' + 'credit_enforcement',"
        " 'arm', '--report-digest', digest])\n",
        encoding="utf-8",
    )
    assert _imports_ops_module(probe, tmp_path)


def test_no_shell_makefile_workflow_or_infra_file_names_the_task() -> None:
    """Text, not AST: the files that could start the one-off task."""

    swept = _text_sources()
    relative = {path.relative_to(ROOT).as_posix() for path in swept}
    assert {"Makefile", "Dockerfile", "scripts/backup.sh"} <= relative
    assert any(name.startswith(".github/workflows/") for name in relative)
    assert any(name.startswith("infra/") for name in relative)
    assert not any(name.startswith(("docs/", "tests/")) for name in relative)
    assert _text_mentions(swept, ROOT) == []


@pytest.mark.parametrize(
    ("relative", "line"),
    [
        (
            "scripts/arm.sh",
            "python -m src.ops.credit_enforcement arm --report-digest $D",
        ),
        ("Makefile", "arm:\n\tpoetry run python -m src.ops.credit_enforcement arm"),
        (".github/workflows/arm.yml", "run: python -m src/ops/credit_enforcement"),
        ("infra/production/task.json", '{"command": ["credit_enforcement"]}'),
        ("scripts/call.sql", "-- arm_enforcement"),
        ("scripts/note.txt", "accept_cutoff"),
    ],
)
def test_the_text_sweep_sees_each_form(
    tmp_path: Path, relative: str, line: str
) -> None:
    probe = tmp_path / relative
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(line + "\n", encoding="utf-8")
    assert _text_mentions(_text_sources(tmp_path), tmp_path)


def _artifacts_and_a_new_script(root: Path) -> None:
    """What a cached or coverage test run leaves behind, beside a new script."""

    for relative, content in {
        ".pytest_cache/v/cache/nodeids": (
            '[\n  "tests/ops/test_credit_enforcement.py::test_x"\n]\n'
        ),
        "htmlcov/x.html": "<p>src.ops.credit_enforcement arm_enforcement</p>\n",
        ".mypy_cache/3.12/src/ops/credit_enforcement.meta.json": (
            '{"id": "src.ops.credit_enforcement"}\n'
        ),
        ".coverage": "src/ops/credit_enforcement.py\n",
        "scripts/zz_x.sh": "python -m src.ops.credit_enforcement arm\n",
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_the_text_sweep_skips_test_artifacts_but_sees_a_new_script(
    tmp_path: Path,
) -> None:
    """A repository whose ``.gitignore`` names the artifacts, as this one's
    does: the second ``make test`` (``.pytest_cache``) or a ``make test-cov``
    (``htmlcov``) must not trip the sweep; an untracked script must."""

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(
        ".pytest_cache/\nhtmlcov/\n.mypy_cache/\n.coverage\n", encoding="utf-8"
    )
    _artifacts_and_a_new_script(tmp_path)

    assert _git_files(tmp_path, "git") is not None  # the git path, not the walk
    assert _text_mentions(_text_sources(tmp_path), tmp_path) == ["scripts/zz_x.sh:1"]


def test_without_git_the_walk_skips_the_artifacts_but_sees_a_new_script(
    tmp_path: Path,
) -> None:
    _artifacts_and_a_new_script(tmp_path)
    missing = str(tmp_path / "no-such-git")

    assert _git_files(tmp_path, missing) is None
    sources = _text_sources(tmp_path, git=missing)
    assert _text_mentions(sources, tmp_path) == ["scripts/zz_x.sh:1"]


def test_the_real_sweep_reads_the_git_listing() -> None:
    """In this repository the git path is the one used (the walk is only the
    fallback); a skip here would hide which one ran."""

    if shutil.which("git") is None or _git_files(ROOT, "git") is None:
        pytest.fail("the fence needs git or must be run from a work tree")


def test_an_allowlisted_line_is_not_a_finding_but_its_neighbours_are(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "infra" / "alarm.yaml"
    probe.parent.mkdir(parents=True)
    probe.write_text(
        "filter: '{ $.logger = \"src.ops.credit_enforcement\" }'\n"
        "command: python -m src.ops.credit_enforcement arm\n",
        encoding="utf-8",
    )
    allowed = {
        "infra/alarm.yaml": frozenset(
            {"filter: '{ $.logger = \"src.ops.credit_enforcement\" }'"}
        )
    }
    sources = _text_sources(tmp_path, git=str(tmp_path / "no-git"))
    assert _text_mentions(sources, tmp_path, allowed) == ["infra/alarm.yaml:2"]


def test_the_text_sweep_ignores_the_tables_of_the_same_prefix(tmp_path: Path) -> None:
    probe = tmp_path / "scripts" / "readback.sql"
    probe.parent.mkdir(parents=True)
    probe.write_text(
        "SELECT credit_enforcement_at FROM credit_enforcement_state;\n",
        encoding="utf-8",
    )
    assert _text_mentions(_text_sources(tmp_path), tmp_path) == []


async def test_the_fence_starts_unset_after_migration(clean: _Harness) -> None:
    assert await _state_rows(clean) == (0, None)
    async with clean.sessions() as session:
        assert await CreditRepository(session).enforcement_at() is None
        assert (
            await session.scalar(
                select(func.count()).select_from(CreditEnforcementState)
            )
        ) == 0
        await session.rollback()


async def test_the_cutoff_reads_one_message_row_per_tenant(clean: _Harness) -> None:
    """The ``message.in`` evidence is DISTINCT per (tenant, builder): many
    events of one tenant are one row shipped, not one per event."""

    from sqlalchemy import event

    standalone = await make_tenant(clean, None)
    for minutes in range(1, 26):
        await message_in(clean, standalone, NOW - timedelta(minutes=minutes))
    captured: list[tuple[str, object]] = []

    def record(*args: object) -> None:
        statement, parameters = str(args[2]), args[3]
        if "message.in" in statement or (
            "FROM events" in statement and "event_type" in statement
        ):
            captured.append((statement, parameters))

    engine = clean.engine.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        async with clean.sessions() as session:
            report = await CreditRolloutService(session).cutoff_report(
                now=await CreditRepository(session).database_now()
            )
            await session.rollback()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert [f.reason for f in report.findings].count("standalone") == 1
    [(statement, parameters)] = captured
    async with clean.engine.connect() as connection:
        rows = (
            await connection.exec_driver_sql(statement, parameters)  # type: ignore[arg-type]
        ).all()
    assert len(rows) == 1
