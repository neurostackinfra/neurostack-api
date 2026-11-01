"""``python -m src.ops.credit_enforcement`` on real PostgreSQL (Group 6).

The one-off task module is the only caller of ``arm_enforcement`` and
``accept_cutoff``.  Every proof here runs the module's own entry point
(``amain``: argument parsing, switch refresh, transaction, readback and the
one JSON document) against a fresh migrated database:

* the credit switch is refreshed IN THIS PROCESS: a stale effective verdict
  published earlier does not let any subcommand through once the
  configuration is off or ``alembic_version`` is below the credit minimum;
* ``arm`` confirms ``--report-digest`` INSIDE the arming transaction (a
  change committed while the arm waits on the rollout lock is seen), arms at
  the DATABASE clock (``clock_timestamp()`` is shifted a day here, so the
  process clock cannot pass) with the ECS task ARN in ``set_by``, reads the
  row back in a fresh session, and a second call exits ``already_armed``;
* ``arm`` refuses a different digest and a report whose uncovered tenant
  lacks an acceptance for THAT finding under THAT digest;
* ``accept`` takes a finding ``(tenant, builder, reason code)`` of the
  current report, refuses an author who is not an active ``super_admin``,
  records the database clock and reads the row back in a fresh session;
* ``status`` and ``report`` write nothing (a digest of every public table is
  unchanged, in a world where ``reconcile_orphans`` would write).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.config.settings import settings
from src.ops import credit_enforcement as ops
from src.repositories.credit import CreditRepository
from src.services.credit_rollout import ORPHAN_EFFECT_KINDS, CreditRolloutService
from src.services.credit_switch import (
    credit_switch_effective,
    refresh_credit_switch,
    stop_credit_switch,
)
from tests.integration.credit_rollout_world import (
    PLANT,
    SAO_PAULO,
    clean,
    execute,
    make_builder,
    make_tenant,
    message_in,
    observation,
    plant,
    scalar,
    subscribed_builder,
    template,
)
from tests.integration.test_billing_fact_worker_postgres import _Harness

pytestmark = pytest.mark.integration

__all__ = ["clean", "template"]

TASK_ARN = (
    "arn:aws:ecs:us-east-1:123456789012:task/neurostack-staging/"
    "0123456789abcdef0123456789abcdef"
)
OTHER_DIGEST = "0" * 64
# The digest of a report with no finding (published in the runbook).
EMPTY_DIGEST = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
HOUR_AGO = timedelta(hours=1)
NO_ORPHANS = {
    "open": 0,
    "decision_without_effect": 0,
    "effect_without_decision": 0,
    "trace_partial_debit": 0,
    "orphan_outbox": 0,
    "handoff_without_effect": 0,
}


# ---------------------------------------------------------------------------
# Harness: the module's real entry point, the switch and the database clock.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    code: int
    document: dict[str, Any]
    log: dict[str, Any]


@pytest.fixture
async def world(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_Harness]:
    """A fresh database with the credit configuration on (the module still
    refreshes the switch itself); the process verdict is reset afterwards."""

    monkeypatch.setattr(settings, "credit_hot_path_enabled", True)
    try:
        yield clean
    finally:
        await stop_credit_switch(None)


async def _identity() -> str:
    return TASK_ARN


async def _no_identity() -> None:
    return None


async def run(
    harness: _Harness,
    *argv: str,
    identity: Any = _identity,
    sessions: Any = None,
) -> Outcome:
    out, err = io.StringIO(), io.StringIO()
    code = await ops.amain(
        list(argv),
        session_factory=sessions or harness.sessions,
        task_identity=identity,
        stdout=out,
        stderr=err,
    )
    (line,) = out.getvalue().splitlines()
    document = json.loads(line)
    logs = [
        record
        for record in (
            json.loads(raw) for raw in err.getvalue().splitlines() if raw[:1] == "{"
        )
        if record.get("event") == ops.LOG_EVENT
    ]
    (log,) = logs
    assert document["exit_code"] == code
    assert {key: log[key] for key in document} == document
    return Outcome(code, document, log)


async def shift_database_clock(harness: _Harness, interval: str) -> None:
    """Make ``clock_timestamp()`` -- unqualified, as the code calls it -- run
    ``interval`` ahead for every new connection to this database."""

    await execute(
        harness,
        "CREATE FUNCTION public.clock_timestamp() RETURNS timestamptz "
        "LANGUAGE sql VOLATILE AS "
        f"$$ SELECT pg_catalog.clock_timestamp() + interval '{interval}' $$",
    )
    database = harness.url.database
    await execute(
        harness, f'ALTER DATABASE "{database}" SET search_path = public, pg_catalog'
    )
    skew = await scalar(
        harness,
        "SELECT extract(epoch FROM clock_timestamp() - pg_catalog.clock_timestamp())",
    )
    assert float(skew) > 3600


async def make_user(
    harness: _Harness, *, role: str = "super_admin", active: bool = True
) -> UUID:
    user_id = uuid4()
    home = await make_tenant(harness, None)
    await execute(
        harness,
        "INSERT INTO users (id, tenant_id, email, password_hash, role, is_active) "
        "VALUES (:id, :tenant, :email, 'x', CAST(:role AS userrole), :active)",
        id=user_id,
        tenant=home,
        email=f"{user_id.hex}@ops.invalid",
        role=role,
        active=active,
    )
    return user_id


async def traffic(
    harness: _Harness,
    tenant_id: UUID,
    builder_id: UUID | None,
    **observation_kwargs: object,
) -> None:
    at = datetime.now(timezone.utc) - HOUR_AGO
    await message_in(harness, tenant_id, at)
    await observation(harness, tenant_id, builder_id, at, **observation_kwargs)


async def state_rows(harness: _Harness) -> tuple[int, Any, Any]:
    count = await scalar(harness, "SELECT count(*) FROM credit_enforcement_state")
    at = await scalar(
        harness, "SELECT max(credit_enforcement_at) FROM credit_enforcement_state"
    )
    set_by = await scalar(harness, "SELECT max(set_by) FROM credit_enforcement_state")
    return int(count), at, set_by


async def acceptance_count(harness: _Harness) -> int:
    return int(await scalar(harness, "SELECT count(*) FROM credit_cutoff_acceptances"))


async def database_digest(harness: _Harness) -> str:
    """Every row of every public table, hashed (a write anywhere changes it)."""

    async with harness.engine.connect() as connection:
        tables = (
            (
                await connection.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                        "ORDER BY tablename"
                    )
                )
            )
            .scalars()
            .all()
        )
        parts = []
        for table in tables:
            value = await connection.scalar(
                text(
                    "SELECT md5(coalesce(string_agg(r::text, '|' ORDER BY r::text), ''))"
                    f' FROM "{table}" r'
                )
            )
            parts.append(f"{table}:{value}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


async def service_digest(harness: _Harness) -> str:
    async with harness.sessions() as session:
        now = await CreditRepository(session).database_now()
        report = await CreditRolloutService(session).cutoff_report(now=now)
        await session.rollback()
    return report.digest


async def report(harness: _Harness) -> dict[str, Any]:
    outcome = await run(harness, "report")
    assert outcome.code == 0, outcome.document
    cutoff: dict[str, Any] = outcome.document["cutoff"]
    return cutoff


def accept_argv(
    digest: str,
    tenant_id: UUID,
    builder_id: UUID | None,
    reason: str,
    author: UUID,
    rationale: str = "Owner: tenant reviewed, cutoff accepted (runbook row 1).",
) -> list[str]:
    return [
        "accept",
        "--report-digest",
        digest,
        "--tenant-id",
        str(tenant_id),
        "--builder-id",
        "none" if builder_id is None else str(builder_id),
        "--reason-code",
        reason,
        "--author-user-id",
        str(author),
        "--rationale",
        rationale,
    ]


async def plant_acceptance(
    harness: _Harness,
    *,
    tenant_id: UUID,
    builder_id: UUID | None,
    reason: str,
    digest: str,
) -> None:
    """An acceptance row written around the module (only to prove what
    ``arm`` does with a near-miss)."""

    await execute(
        harness,
        "INSERT INTO credit_cutoff_acceptances (id, tenant_id, builder_id, reason, "
        "report_digest, author_user_id, accepted_at, accepted_on) VALUES "
        "(:id, :tenant, :builder, :reason, :digest, :author, now(), current_date)",
        id=uuid4(),
        tenant=tenant_id,
        builder=builder_id,
        reason=reason,
        digest=digest,
        author=uuid4(),
    )


async def deleted_tenant_orphan(
    harness: _Harness, builder_id: UUID, kind: str = "trace_no_response"
) -> None:
    """An effect of *kind* of a since-deleted tenant, with no decision or
    allocation: ``reconcile_orphans`` would write its deletion closure (for
    an orphan kind); ``orphan_report`` only counts it."""

    tenant_id = await make_tenant(harness, builder_id)
    await execute(
        harness,
        "INSERT INTO credit_effects (id, builder_id, tenant_id, effect_id, "
        "effect_kind, decision_digest) VALUES (:id, :b, :t, :e, :kind, :d)",
        id=uuid4(),
        b=builder_id,
        t=tenant_id,
        e=f"{kind}:{tenant_id}:{uuid4()}:decision:1",
        kind=kind,
        d="c" * 64,
    )
    await execute(harness, "DELETE FROM tenants WHERE id = :id", id=tenant_id)


# ---------------------------------------------------------------------------
# status / report: read-only.
# ---------------------------------------------------------------------------


async def test_status_reports_the_gate_and_writes_nothing(world: _Harness) -> None:
    covered = await subscribed_builder(world)
    standalone = await make_tenant(world, None)
    await traffic(world, standalone, None)
    await deleted_tenant_orphan(world, covered.builder_id)
    before = await database_digest(world)

    outcome = await run(world, "status")

    assert outcome.code == 0 and outcome.document["verdict"] == "ok"
    assert outcome.document["switch"]["effective"] is True
    assert outcome.document["enforcement"] == {
        "armed": False,
        "credit_enforcement_at": None,
        "set_by": None,
        "set_at": None,
    }
    readiness = outcome.document["readiness"]
    assert readiness["has_alert"] is True and readiness["ready"] is False
    assert readiness["open_orphans"] == 1
    # ... but that one orphan is a deleted tenant's effect, which ``arm``
    # closes itself in its own transaction: after that, nothing is open.
    assert readiness["orphans"] == {
        **NO_ORPHANS,
        "open": 1,
        "effect_without_decision": 1,
    }
    assert readiness["deletion_closures_arm_writes"] == 1
    assert readiness["orphans_after_arm_closures"] == NO_ORPHANS
    assert readiness["ready_after_arm_closures"] is True
    cutoff = outcome.document["cutoff"]
    assert cutoff["digest"] == await service_digest(world)
    assert (cutoff["finding_count"], cutoff["accepted_count"], cutoff["ready"]) == (
        1,
        0,
        False,
    )
    assert cutoff["findings"] is None
    assert await database_digest(world) == before
    assert (
        await scalar(
            world,
            "SELECT count(*) FROM credit_effects WHERE effect_kind = 'deletion_closure'",
        )
        == 0
    )


async def test_report_lists_each_finding_with_its_acceptance_and_writes_nothing(
    world: _Harness,
) -> None:
    await subscribed_builder(world)
    standalone = await make_tenant(world, None)
    await traffic(world, standalone, None)
    uncovered = await make_builder(world)
    tenant = await make_tenant(world, uncovered)
    await traffic(world, tenant, uncovered)
    first = await report(world)
    await plant_acceptance(
        world,
        tenant_id=standalone,
        builder_id=None,
        reason="standalone",
        digest=first["digest"],
    )
    before = await database_digest(world)

    outcome = await run(world, "report")

    assert outcome.code == 0 and outcome.document["verdict"] == "ok"
    cutoff = outcome.document["cutoff"]
    assert cutoff["digest"] == first["digest"] == await service_digest(world)
    assert sorted(
        (f["tenant_id"], f["builder_id"], f["reason"], f["accepted"])
        for f in cutoff["findings"]
    ) == sorted(
        [
            (str(standalone), None, "standalone", True),
            (str(tenant), str(uncovered), "builder_without_coverage", False),
        ]
    )
    assert (cutoff["finding_count"], cutoff["accepted_count"], cutoff["ready"]) == (
        2,
        1,
        False,
    )
    assert await database_digest(world) == before


# ---------------------------------------------------------------------------
# The switch is refreshed in this process: a stale "on" never passes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("degrade", ["config_off", "schema_below_minimum"])
@pytest.mark.parametrize("command", ["status", "report", "accept", "arm"])
async def test_every_subcommand_refuses_once_the_switch_is_off(
    world: _Harness, monkeypatch: pytest.MonkeyPatch, degrade: str, command: str
) -> None:
    await subscribed_builder(world)
    standalone = await make_tenant(world, None)
    await traffic(world, standalone, None)
    author = await make_user(world)
    digest = await service_digest(world)
    argv = {
        "status": ["status"],
        "report": ["report"],
        "accept": accept_argv(digest, standalone, None, "standalone", author),
        "arm": ["arm", "--report-digest", digest],
    }[command]
    # A verdict published earlier in this process was effective ...
    assert (await refresh_credit_switch(session_factory=world.sessions)).effective
    assert credit_switch_effective()
    # ... and the world changed since.
    if degrade == "config_off":
        monkeypatch.setattr(settings, "credit_hot_path_enabled", False)
    else:
        await execute(world, "UPDATE alembic_version SET version_num = '041'")
    before = await database_digest(world)

    outcome = await run(world, *argv)

    assert outcome.code == ops.EXIT_CODES["switch_off"]
    assert outcome.document["verdict"] == "switch_off"
    assert outcome.document["reasons"] == ["credit_switch_off"]
    assert outcome.document["switch"]["effective"] is False
    assert outcome.document["switch"]["reason"] == (
        "config_disabled" if degrade == "config_off" else "schema_below_minimum"
    )
    assert not credit_switch_effective()
    assert await database_digest(world) == before


# ---------------------------------------------------------------------------
# accept: a finding of the current report, an active super_admin, the
# database clock.
# ---------------------------------------------------------------------------


async def test_accept_records_the_finding_at_the_database_clock(
    world: _Harness,
) -> None:
    await subscribed_builder(world)
    standalone = await make_tenant(world, None)
    await traffic(world, standalone, None)
    author = await make_user(world)
    await shift_database_clock(world, "1 day")
    digest = (await report(world))["digest"]
    rationale = "Clinica piloto sem builder; owner aceita credit_pending (L10)."

    outcome = await run(
        world, *accept_argv(digest, standalone, None, "standalone", author, rationale)
    )

    assert outcome.code == 0 and outcome.document["verdict"] == "accepted"
    assert outcome.document["rationale"] == rationale
    assert outcome.log["rationale"] == rationale
    assert outcome.document["task_arn"] == TASK_ARN
    acceptance = outcome.document["acceptance"]
    async with world.engine.connect() as connection:
        (row,) = (
            await connection.execute(
                text(
                    "SELECT id, tenant_id, builder_id, reason, report_digest, "
                    "author_user_id, accepted_at, accepted_on "
                    "FROM credit_cutoff_acceptances"
                )
            )
        ).all()
    assert str(row.id) == acceptance["id"]
    assert (row.tenant_id, row.builder_id, row.reason, row.report_digest) == (
        standalone,
        None,
        "standalone",
        digest,
    )
    assert row.author_user_id == author
    # The database clock runs a day ahead here: the process clock cannot be
    # the instant that was written.
    assert row.accepted_at - datetime.now(timezone.utc) > timedelta(hours=23)
    assert row.accepted_on == row.accepted_at.astimezone(SAO_PAULO).date()
    assert acceptance["accepted_at"] == row.accepted_at.isoformat()
    assert (await report(world))["ready"] is True


@pytest.mark.parametrize(
    ("role", "active"),
    [
        ("super_admin", False),
        ("clinic_admin", True),
        ("builder", True),
        (None, True),
    ],
    ids=["inactive-super-admin", "clinic-admin", "builder", "unknown-user"],
)
async def test_accept_refuses_an_author_who_is_not_an_active_super_admin(
    world: _Harness, role: str | None, active: bool
) -> None:
    await subscribed_builder(world)
    standalone = await make_tenant(world, None)
    await traffic(world, standalone, None)
    author = (
        uuid4() if role is None else await make_user(world, role=role, active=active)
    )
    digest = (await report(world))["digest"]

    outcome = await run(
        world, *accept_argv(digest, standalone, None, "standalone", author)
    )

    assert outcome.code == ops.EXIT_CODES["refused"], outcome.document
    assert outcome.document["reasons"] == ["author_not_active_super_admin"]
    assert await acceptance_count(world) == 0


async def test_accept_refuses_a_digest_that_is_not_the_current_report(
    world: _Harness,
) -> None:
    await subscribed_builder(world)
    standalone = await make_tenant(world, None)
    await traffic(world, standalone, None)
    author = await make_user(world)
    stale = (await report(world))["digest"]
    other = await make_tenant(world, None)
    await traffic(world, other, None)  # new traffic: a new report

    outcome = await run(
        world, *accept_argv(stale, standalone, None, "standalone", author)
    )

    assert outcome.code == ops.EXIT_CODES["refused"]
    assert outcome.document["reasons"] == ["report_digest_mismatch"]
    assert await acceptance_count(world) == 0


@pytest.mark.parametrize("miss", ["builder", "reason", "tenant"])
async def test_accept_refuses_a_triple_that_is_not_a_finding(
    world: _Harness, miss: str
) -> None:
    await subscribed_builder(world)
    uncovered = await make_builder(world)
    tenant = await make_tenant(world, uncovered)
    await traffic(world, tenant, uncovered)
    author = await make_user(world)
    digest = (await report(world))["digest"]
    finding = {
        "builder": (tenant, None, "builder_without_coverage"),
        "reason": (tenant, uncovered, "unknown_unauthenticated_traffic"),
        "tenant": (
            await make_tenant(world, uncovered),
            uncovered,
            "builder_without_coverage",
        ),
    }[miss]

    outcome = await run(world, *accept_argv(digest, *finding, author))

    assert outcome.code == ops.EXIT_CODES["refused"]
    assert outcome.document["reasons"] == ["finding_not_in_report"]
    assert await acceptance_count(world) == 0


@pytest.mark.parametrize("command", ["accept", "arm"])
async def test_a_write_needs_the_task_identity(world: _Harness, command: str) -> None:
    await subscribed_builder(world)
    standalone = await make_tenant(world, None)
    await traffic(world, standalone, None)
    author = await make_user(world)
    digest = (await report(world))["digest"]
    argv = (
        accept_argv(digest, standalone, None, "standalone", author)
        if command == "accept"
        else ["arm", "--report-digest", digest]
    )
    before = await database_digest(world)

    outcome = await run(world, *argv, identity=_no_identity)

    assert outcome.code == ops.EXIT_CODES["unconfigured"]
    assert outcome.document["reasons"] == ["task_identity_unavailable"]
    assert await database_digest(world) == before


async def test_accept_reads_the_row_back_in_a_fresh_session(world: _Harness) -> None:
    """A trigger rewrites the stored digest: only a fresh read can see it."""

    await subscribed_builder(world)
    standalone = await make_tenant(world, None)
    await traffic(world, standalone, None)
    author = await make_user(world)
    digest = (await report(world))["digest"]
    await execute(
        world,
        "CREATE FUNCTION probe_tamper_acceptance() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN NEW.report_digest := repeat('f', 64); RETURN NEW; END $$",
    )
    await execute(
        world,
        "CREATE TRIGGER probe_tamper BEFORE INSERT ON credit_cutoff_acceptances "
        "FOR EACH ROW EXECUTE FUNCTION probe_tamper_acceptance()",
    )

    outcome = await run(
        world, *accept_argv(digest, standalone, None, "standalone", author)
    )

    assert outcome.code == ops.EXIT_CODES["readback_mismatch"], outcome.document
    assert outcome.document["reasons"] == ["readback_mismatch"]


# ---------------------------------------------------------------------------
# arm: the confirmed digest, inside the arming transaction.
# ---------------------------------------------------------------------------


async def test_arm_arms_once_at_the_database_clock_with_the_task_arn(
    world: _Harness,
) -> None:
    await subscribed_builder(world)
    standalone = await make_tenant(world, None)
    await traffic(world, standalone, None)
    author = await make_user(world)
    await shift_database_clock(world, "1 day")
    digest = (await report(world))["digest"]
    accepted = await run(
        world, *accept_argv(digest, standalone, None, "standalone", author)
    )
    assert accepted.code == 0, accepted.document

    outcome = await run(world, "arm", "--report-digest", digest)

    assert outcome.code == 0 and outcome.document["verdict"] == "armed"
    count, armed_at, set_by = await state_rows(world)
    assert count == 1 and set_by == TASK_ARN
    assert armed_at - datetime.now(timezone.utc) > timedelta(hours=23)
    assert outcome.document["enforcement"] == {
        "armed": True,
        "credit_enforcement_at": armed_at.isoformat(),
        "set_by": TASK_ARN,
        "set_at": armed_at.isoformat(),
    }
    assert outcome.document["report_digest"] == digest

    again = await run(world, "arm", "--report-digest", digest)
    assert again.code == ops.EXIT_CODES["already_armed"] == 10
    assert again.document["verdict"] == "already_armed"
    assert again.document["reasons"] == ["already_armed"]
    assert await state_rows(world) == (count, armed_at, set_by)

    late = await run(
        world, *accept_argv(digest, standalone, None, "standalone", author)
    )
    assert late.code == ops.EXIT_CODES["already_armed"]
    assert await acceptance_count(world) == 1

    status = await run(world, "status")
    assert status.code == 0
    assert status.document["enforcement"]["armed"] is True
    assert status.document["enforcement"]["set_by"] == TASK_ARN


async def test_arm_refuses_a_different_digest(world: _Harness) -> None:
    await subscribed_builder(world)
    current = (await report(world))["digest"]
    assert current != OTHER_DIGEST and (await report(world))["ready"] is True

    outcome = await run(world, "arm", "--report-digest", OTHER_DIGEST)

    assert outcome.code == ops.EXIT_CODES["refused"]
    assert outcome.document["reasons"] == ["report_digest_mismatch"]
    assert await state_rows(world) == (0, None, None)


async def test_arm_refuses_an_uncovered_tenant_until_that_finding_is_accepted(
    world: _Harness,
) -> None:
    """Each near-miss acceptance -- another digest, another reason of the
    same tenant, another builder -- leaves the builder finding unaccepted."""

    await subscribed_builder(world)
    uncovered = await make_builder(world)
    tenant = await make_tenant(world, uncovered)
    await traffic(world, tenant, uncovered, origin="unknown", authenticated=False)
    author = await make_user(world)
    cutoff = await report(world)
    digest = cutoff["digest"]
    assert sorted(f["reason"] for f in cutoff["findings"]) == [
        "builder_without_coverage",
        "unknown_unauthenticated_traffic",
    ]

    async def refused() -> None:
        outcome = await run(world, "arm", "--report-digest", digest)
        assert outcome.code == ops.EXIT_CODES["refused"], outcome.document
        assert outcome.document["reasons"] == ["cutoff_unaccepted"]
        assert await state_rows(world) == (0, None, None)

    await refused()
    await plant_acceptance(
        world,
        tenant_id=tenant,
        builder_id=uncovered,
        reason="builder_without_coverage",
        digest=OTHER_DIGEST,
    )
    await refused()
    other_reason = await run(
        world,
        *accept_argv(
            digest, tenant, uncovered, "unknown_unauthenticated_traffic", author
        ),
    )
    assert other_reason.code == 0, other_reason.document
    await refused()
    await plant_acceptance(
        world,
        tenant_id=tenant,
        builder_id=None,
        reason="builder_without_coverage",
        digest=digest,
    )
    await refused()

    finding = await run(
        world,
        *accept_argv(digest, tenant, uncovered, "builder_without_coverage", author),
    )
    assert finding.code == 0, finding.document
    outcome = await run(world, "arm", "--report-digest", digest)
    assert outcome.code == 0, outcome.document
    assert (await state_rows(world))[0] == 1


async def test_arm_maps_the_gate_refusals(world: _Harness) -> None:
    """Orphans first, then readiness: each with its own reason."""

    covered = await subscribed_builder(world)
    digest = (await report(world))["digest"]
    await execute(
        world,
        "INSERT INTO credit_revision_gaps (aggregate_kind, aggregate_id, "
        "first_missing_revision) VALUES ('topup_order', :id, 2)",
        id=uuid4(),
    )
    readiness = await run(world, "arm", "--report-digest", digest)
    assert readiness.code == ops.EXIT_CODES["refused"]
    assert readiness.document["reasons"] == ["readiness_incomplete"]

    await execute(
        world,
        """
        INSERT INTO credit_ingress_claims (
            id, tenant_id, builder_id, source, principal_id, idempotency_key,
            interaction_id, payload_sha256, decision_version, mode
        ) VALUES (:id, :tenant, :builder, 'events', 'api_key:probe', 'k1',
                  :interaction, :sha, 1, 'enforced')
        """,
        id=uuid4(),
        tenant=covered.tenant_id,
        builder=covered.builder_id,
        interaction=uuid4(),
        sha="b" * 64,
    )
    orphans = await run(world, "arm", "--report-digest", digest)
    assert orphans.code == ops.EXIT_CODES["refused"]
    assert orphans.document["reasons"] == ["orphans_open"]
    assert await state_rows(world) == (0, None, None)


async def _rollout_lock_waiters(harness: _Harness) -> int:
    return int(
        await scalar(
            harness,
            "SELECT count(*) FROM pg_stat_activity WHERE datname = "
            "current_database() AND wait_event_type = 'Lock' "
            "AND pid <> pg_backend_pid()",
        )
    )


async def test_the_digest_is_confirmed_inside_the_arming_transaction(
    world: _Harness,
) -> None:
    """The confirmed report loses its only (accepted) finding while the arm
    waits on the rollout lock.  Checked before the lock, the digest would
    still match and the emptied report would arm; checked inside the
    transaction it no longer matches."""

    covered = await subscribed_builder(world)
    standalone = await make_tenant(world, None)
    await traffic(world, standalone, None)
    author = await make_user(world)
    digest = (await report(world))["digest"]
    accepted = await run(
        world, *accept_argv(digest, standalone, None, "standalone", author)
    )
    assert accepted.code == 0 and (await report(world))["ready"] is True

    acquired, release = asyncio.Event(), asyncio.Event()
    writer_engine, writer_sessions = world.factory()

    async def writer() -> None:
        async with writer_sessions() as session:
            await CreditRepository(session).lock_rollout_control(
                environment=settings.environment
            )
            acquired.set()
            await session.execute(text(PLANT))
            await session.execute(
                text("UPDATE tenants SET builder_id = :b WHERE id = :t"),
                {"b": covered.builder_id, "t": standalone},
            )
            await release.wait()
            await session.commit()

    writing = asyncio.create_task(writer())
    arming: asyncio.Task[Outcome] | None = None
    try:
        await asyncio.wait_for(acquired.wait(), timeout=20)
        arming = asyncio.create_task(run(world, "arm", "--report-digest", digest))
        deadline = asyncio.get_running_loop().time() + 20
        while await _rollout_lock_waiters(world) == 0:
            assert not arming.done(), "the arm did not wait for the rollout lock"
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.05)
        release.set()
        await asyncio.wait_for(writing, timeout=20)
        outcome = await asyncio.wait_for(arming, timeout=60)
    finally:
        release.set()
        for task in (writing, *([arming] if arming is not None else [])):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            writing, *([arming] if arming is not None else []), return_exceptions=True
        )
        await writer_engine.dispose()

    assert outcome.code == ops.EXIT_CODES["refused"], outcome.document
    assert outcome.document["reasons"] == ["report_digest_mismatch"]
    assert await state_rows(world) == (0, None, None)
    # The world itself would arm now, under its new digest.
    assert (await report(world))["findings"] == []


async def test_two_concurrent_arms_arm_once(world: _Harness) -> None:
    await subscribed_builder(world)
    digest = (await report(world))["digest"]
    engines = [world.factory() for _ in range(2)]
    try:
        outcomes = await asyncio.gather(
            *(
                run(world, "arm", "--report-digest", digest, sessions=sessions)
                for _engine, sessions in engines
            )
        )
    finally:
        for engine, _sessions in engines:
            await engine.dispose()
    assert sorted(outcome.document["verdict"] for outcome in outcomes) == [
        "already_armed",
        "armed",
    ]
    assert (await state_rows(world))[0] == 1


async def test_arm_reads_the_row_back_in_a_fresh_session(world: _Harness) -> None:
    """A trigger rewrites ``set_by`` on the way in: the transaction's own
    object still says the task ARN, only a fresh session sees the row."""

    await subscribed_builder(world)
    digest = (await report(world))["digest"]
    await execute(
        world,
        "CREATE FUNCTION probe_tamper_state() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN IF NEW.set_by IS NOT NULL THEN NEW.set_by := 'tampered'; "
        "END IF; RETURN NEW; END $$",
    )
    await execute(
        world,
        "CREATE TRIGGER probe_tamper BEFORE INSERT OR UPDATE ON "
        "credit_enforcement_state FOR EACH ROW EXECUTE FUNCTION probe_tamper_state()",
    )

    outcome = await run(world, "arm", "--report-digest", digest)

    assert outcome.code == ops.EXIT_CODES["readback_mismatch"], outcome.document
    assert outcome.document["reasons"] == ["readback_mismatch"]
    assert (await state_rows(world))[2] == "tampered"


# ---------------------------------------------------------------------------
# status predicts arm's own orphan step (MEDIUM-2 of the Group 6 review).
# ---------------------------------------------------------------------------


async def _enforced_claim_without_effect(harness: _Harness, covered: Any) -> None:
    """A ``decision_without_effect`` orphan: no ``arm`` closes it."""

    await execute(
        harness,
        """
        INSERT INTO credit_ingress_claims (
            id, tenant_id, builder_id, source, principal_id, idempotency_key,
            interaction_id, payload_sha256, decision_version, mode
        ) VALUES (:id, :tenant, :builder, 'events', 'api_key:probe', 'k1',
                  :interaction, :sha, 1, 'enforced')
        """,
        id=uuid4(),
        tenant=covered.tenant_id,
        builder=covered.builder_id,
        interaction=uuid4(),
        sha="b" * 64,
    )


@pytest.mark.parametrize("other_orphan", [False, True], ids=["closable", "blocking"])
async def test_status_tells_which_orphans_arm_closes_and_arm_agrees(
    world: _Harness, other_orphan: bool
) -> None:
    """A deleted tenant's effect makes ``ready`` false in ``status``; ``arm``
    closes it in its own transaction and arms.  Any other orphan class still
    blocks with ``orphans_open`` -- and ``status`` says so beforehand."""

    covered = await subscribed_builder(world)
    await deleted_tenant_orphan(world, covered.builder_id)
    if other_orphan:
        await _enforced_claim_without_effect(world, covered)
    before = await database_digest(world)

    status = await run(world, "status")

    assert await database_digest(world) == before
    readiness = status.document["readiness"]
    assert readiness["ready"] is False
    assert readiness["deletion_closures_arm_writes"] == 1
    assert readiness["orphans"]["effect_without_decision"] == 1
    after = readiness["orphans_after_arm_closures"]
    if other_orphan:
        assert after == {**NO_ORPHANS, "open": 1, "decision_without_effect": 1}
        assert readiness["ready_after_arm_closures"] is False
    else:
        assert after == NO_ORPHANS
        assert readiness["ready_after_arm_closures"] is True
    digest = status.document["cutoff"]["digest"]

    outcome = await run(world, "arm", "--report-digest", digest)

    if other_orphan:
        assert outcome.code == ops.EXIT_CODES["refused"], outcome.document
        assert outcome.document["reasons"] == ["orphans_open"]
        assert await state_rows(world) == (0, None, None)
    else:
        assert outcome.code == 0, outcome.document
        assert (await state_rows(world))[0] == 1


# ---------------------------------------------------------------------------
# The cutoff report is evaluated at the DATABASE clock (MEDIUM-3).
# ---------------------------------------------------------------------------


async def test_report_accept_and_arm_evaluate_the_cutoff_at_the_database_clock(
    world: _Harness,
) -> None:
    """The database clock runs 75 days ahead: the standalone traffic of an
    hour ago (process clock) is outside its two-month window, so the report
    is empty there.  Evaluated at the process clock it would hold the
    standalone finding -- a digest ``accept`` must refuse and ``arm`` must
    not compute."""

    await subscribed_builder(world)
    standalone = await make_tenant(world, None)
    await traffic(world, standalone, None)
    author = await make_user(world)
    async with world.sessions() as session:
        process_clock = await CreditRolloutService(session).cutoff_report(
            now=datetime.now(timezone.utc)
        )
        await session.rollback()
    assert [f.reason for f in process_clock.findings] == ["standalone"]
    await shift_database_clock(world, "75 days")

    cutoff = await report(world)
    assert cutoff["digest"] == EMPTY_DIGEST and cutoff["findings"] == []

    accept = await run(
        world,
        *accept_argv(process_clock.digest, standalone, None, "standalone", author),
    )
    assert accept.code == ops.EXIT_CODES["refused"], accept.document
    assert accept.document["reasons"] == ["report_digest_mismatch"]
    assert await acceptance_count(world) == 0

    outcome = await run(world, "arm", "--report-digest", EMPTY_DIGEST)
    assert outcome.code == 0, outcome.document
    assert (await state_rows(world))[0] == 1


# ---------------------------------------------------------------------------
# accept never writes once armed, even when the arm lands after its check.
# ---------------------------------------------------------------------------


async def test_an_arm_committed_while_accept_waits_is_seen_inside_its_transaction(
    world: _Harness,
) -> None:
    """Another backend holds the rollout lock and arms.  ``accept`` passes
    its pre-check (not armed yet), then waits on the rollout lock; once the
    arm commits it re-reads ``credit_enforcement_at`` under the lock and
    stops at ``already_armed`` without writing."""

    await subscribed_builder(world)
    standalone = await make_tenant(world, None)
    await traffic(world, standalone, None)
    author = await make_user(world)
    digest = (await report(world))["digest"]

    acquired, release = asyncio.Event(), asyncio.Event()
    arm_engine, arm_sessions = world.factory()

    async def arm_elsewhere() -> None:
        async with arm_sessions() as session:
            await CreditRepository(session).lock_rollout_control(
                environment=settings.environment
            )
            await session.execute(text(PLANT))
            await session.execute(
                text(
                    "INSERT INTO credit_enforcement_state (id, credit_enforcement_at,"
                    " set_by, set_at) VALUES (1, clock_timestamp(), 'other-arm', "
                    "clock_timestamp())"
                )
            )
            acquired.set()
            await release.wait()
            await session.commit()

    arming = asyncio.create_task(arm_elsewhere())
    accepting: asyncio.Task[Outcome] | None = None
    try:
        await asyncio.wait_for(acquired.wait(), timeout=20)
        accepting = asyncio.create_task(
            run(world, *accept_argv(digest, standalone, None, "standalone", author))
        )
        deadline = asyncio.get_running_loop().time() + 20
        while await _rollout_lock_waiters(world) == 0:
            assert not accepting.done(), "accept did not wait for the rollout lock"
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.05)
        release.set()
        await asyncio.wait_for(arming, timeout=20)
        outcome = await asyncio.wait_for(accepting, timeout=60)
    finally:
        release.set()
        for task in (arming, *([accepting] if accepting is not None else [])):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            arming,
            *([accepting] if accepting is not None else []),
            return_exceptions=True,
        )
        await arm_engine.dispose()

    assert outcome.code == ops.EXIT_CODES["already_armed"], outcome.document
    assert outcome.document["reasons"] == ["already_armed"]
    assert outcome.document["enforcement"]["set_by"] == "other-arm"
    assert await acceptance_count(world) == 0


# ---------------------------------------------------------------------------
# The digest does not depend on the query plan (L2).
# ---------------------------------------------------------------------------


async def _unknown_ingress(
    harness: _Harness, tenant_id: UUID, builder_id: UUID, at: datetime
) -> None:
    await execute(
        harness,
        """
        INSERT INTO credit_ingress_claims (
            id, tenant_id, builder_id, source, principal_id, provider_message_id,
            interaction_id, payload_sha256, decision_version, mode, created_at
        ) VALUES (:id, :tenant, :builder, 'uazapi', 'uazapi:probe', :message,
                  :interaction, :sha, 1, 'observational', :at)
        """,
        id=uuid4(),
        tenant=tenant_id,
        builder=builder_id,
        message=f"m-{uuid4()}",
        interaction=uuid4(),
        sha="d" * 64,
        at=at,
    )


@pytest.mark.parametrize("evidence", ["observation", "ingress"])
async def test_a_deleted_tenant_with_two_recorded_builders_has_one_digest(
    world: _Harness, evidence: str
) -> None:
    """The deleted tenant's evidence names two builders; the one recorded
    first (``recorded_at`` / ``created_at``) is kept, whatever order the scan
    returns rows in.  Physical order (uncovered first) and recorded order
    (covered first) disagree here.

    ``ingress``: only unknown-source ingress claims, which the tenant
    foreign key deletes with the tenant (``ON DELETE CASCADE``).  The tenant
    is therefore deleted with its triggers off (``PLANT``) to build the one
    state where the ingress order could matter -- a state the schema
    normally prevents."""

    covered = await subscribed_builder(world)
    uncovered = await make_builder(world)
    tenant = await make_tenant(world, uncovered)
    now = datetime.now(timezone.utc)
    record = observation if evidence == "observation" else _unknown_ingress
    await record(world, tenant, uncovered, now - timedelta(hours=1))
    await execute(
        world,
        "UPDATE tenants SET builder_id = :b WHERE id = :t",
        b=covered.builder_id,
        t=tenant,
    )
    await record(world, tenant, covered.builder_id, now - timedelta(hours=2))
    if evidence == "observation":
        await execute(world, "DELETE FROM tenants WHERE id = :id", id=tenant)
        expected: list[dict[str, Any]] = []
    else:
        await plant(world, "DELETE FROM tenants WHERE id = :id", id=tenant)
        assert (
            await scalar(
                world,
                "SELECT count(*) FROM credit_ingress_claims WHERE tenant_id = :t",
                t=tenant,
            )
            == 2
        )
        expected = [
            {
                "tenant_id": str(tenant),
                "builder_id": str(covered.builder_id),
                "reason": "unknown_unauthenticated_traffic",
                "accepted": False,
            }
        ]
    database = world.url.database
    digests = {}
    for plan, switches in {
        "seqscan": (
            "enable_indexscan = off",
            "enable_bitmapscan = off",
            "enable_seqscan = on",
        ),
        "indexscan": (
            "enable_indexscan = on",
            "enable_bitmapscan = off",
            "enable_seqscan = off",
        ),
    }.items():
        for switch in switches:
            await execute(world, f'ALTER DATABASE "{database}" SET {switch}')
        engine, sessions = world.factory()
        try:
            outcome = await run(world, "report", sessions=sessions)
        finally:
            await engine.dispose()
        digests[plan] = (
            outcome.document["cutoff"]["digest"],
            outcome.document["cutoff"]["findings"],
        )
    assert digests["seqscan"] == digests["indexscan"]
    assert digests["seqscan"][1] == expected
    if not expected:
        assert digests["seqscan"][0] == EMPTY_DIGEST


# ---------------------------------------------------------------------------
# The author: held FOR SHARE until commit, and part of every comparison.
# ---------------------------------------------------------------------------


async def test_the_author_cannot_be_deactivated_before_the_acceptance_commits(
    world: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await subscribed_builder(world)
    standalone = await make_tenant(world, None)
    await traffic(world, standalone, None)
    author = await make_user(world)
    digest = (await report(world))["digest"]
    original = CreditRolloutService.accept_cutoff
    attempt: dict[str, str] = {}

    async def then_deactivate(self: CreditRolloutService, **kwargs: Any) -> Any:
        row = await original(self, **kwargs)
        engine, _sessions = world.factory()
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SET lock_timeout = '700ms'"))
                try:
                    await connection.execute(
                        text("UPDATE users SET is_active = false WHERE id = :id"),
                        {"id": author},
                    )
                    await connection.commit()
                    attempt["outcome"] = "updated"
                except DBAPIError as error:
                    attempt["outcome"] = str(error.orig)
                    await connection.rollback()
        finally:
            await engine.dispose()
        return row

    monkeypatch.setattr(CreditRolloutService, "accept_cutoff", then_deactivate)

    outcome = await run(
        world, *accept_argv(digest, standalone, None, "standalone", author)
    )

    assert "lock timeout" in attempt["outcome"], attempt
    assert outcome.code == 0, outcome.document
    assert await scalar(world, "SELECT is_active FROM users WHERE id = :id", id=author)


async def test_a_second_author_records_an_acceptance_of_its_own(
    world: _Harness,
) -> None:
    """The same finding, the same digest, the same day, two authors: two
    rows, each read back with its own author; the first author again is the
    same row (idempotent)."""

    await subscribed_builder(world)
    standalone = await make_tenant(world, None)
    await traffic(world, standalone, None)
    first, second = await make_user(world), await make_user(world)
    digest = (await report(world))["digest"]

    a = await run(world, *accept_argv(digest, standalone, None, "standalone", first))
    b = await run(world, *accept_argv(digest, standalone, None, "standalone", second))
    again = await run(
        world, *accept_argv(digest, standalone, None, "standalone", first)
    )

    for outcome in (a, b, again):
        assert outcome.code == 0, outcome.document
    assert a.document["acceptance"]["author_user_id"] == str(first)
    assert b.document["acceptance"]["author_user_id"] == str(second)
    assert b.document["acceptance"]["id"] != a.document["acceptance"]["id"]
    assert again.document["acceptance"] == a.document["acceptance"]
    assert await acceptance_count(world) == 2


async def test_accept_compares_the_author_it_reads_back(world: _Harness) -> None:
    """A trigger rewrites the stored author to another active super_admin:
    every other field matches, only the author comparison can see it."""

    await subscribed_builder(world)
    standalone = await make_tenant(world, None)
    await traffic(world, standalone, None)
    author, other = await make_user(world), await make_user(world)
    digest = (await report(world))["digest"]
    await execute(
        world,
        "CREATE FUNCTION probe_tamper_author() RETURNS trigger LANGUAGE plpgsql "
        f"AS $$ BEGIN NEW.author_user_id := '{other}'; RETURN NEW; END $$",
    )
    await execute(
        world,
        "CREATE TRIGGER probe_tamper BEFORE INSERT ON credit_cutoff_acceptances "
        "FOR EACH ROW EXECUTE FUNCTION probe_tamper_author()",
    )

    outcome = await run(
        world, *accept_argv(digest, standalone, None, "standalone", author)
    )

    assert outcome.code == ops.EXIT_CODES["readback_mismatch"], outcome.document
    assert outcome.document["reasons"] == ["readback_mismatch"]
    assert outcome.document["acceptance"]["author_user_id"] == str(other)


# ---------------------------------------------------------------------------
# status / report cannot write, even if the code they run tried to.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["status", "report"])
async def test_a_write_inside_a_read_only_command_fails(
    world: _Harness, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """A write slipped into the code ``status``/``report`` run (here: before
    the clock read) is refused by the READ ONLY transaction itself: SQLSTATE
    25006 (``read_only_sql_transaction``), on a table that is otherwise
    writable (no append-only trigger could refuse it instead)."""

    await subscribed_builder(world)
    standalone = await make_tenant(world, None)
    await traffic(world, standalone, None)
    write = "UPDATE tenants SET builder_id = builder_id WHERE id = :id"
    async with world.engine.connect() as connection:  # writable outside
        assert (await connection.execute(text(write), {"id": standalone})).rowcount == 1
        await connection.rollback()
    original = CreditRepository.database_now
    refused: list[str | None] = []

    async def write_then_read(self: CreditRepository) -> datetime:
        try:
            await self.session.execute(text(write), {"id": standalone})
        except DBAPIError as error:
            refused.append(getattr(error.orig, "sqlstate", None))
            raise
        return await original(self)

    monkeypatch.setattr(CreditRepository, "database_now", write_then_read)
    before = await database_digest(world)

    outcome = await run(world, command)

    assert refused == ["25006"], refused
    assert outcome.code == ops.EXIT_CODES["error"], outcome.document
    assert outcome.document["reasons"] == ["unexpected"]
    assert await database_digest(world) == before


# ---------------------------------------------------------------------------
# status and arm agree on every deleted-tenant effect kind (L-a, L-c).
# ---------------------------------------------------------------------------

# What ``arm`` must do with ONE effect of a deleted tenant, by kind: every
# orphan kind is closed; a ``trace`` effect also lacks its debit, which the
# closure does not supply (``trace_partial_debit`` stays open).
_CLOSABLE_KINDS = {
    "trace_no_response": "armed",
    "trace_credit_pending": "armed",
    "rag": "armed",
    "media": "armed",
    "egress": "armed",
    "maintenance": "armed",
    "trace": "orphans_open",
}


def test_the_kind_table_is_every_orphan_kind() -> None:
    assert set(_CLOSABLE_KINDS) == set(ORPHAN_EFFECT_KINDS)


async def _closures(harness: _Harness) -> int:
    return int(
        await scalar(
            harness,
            "SELECT count(*) FROM credit_effects WHERE effect_kind = 'deletion_closure'",
        )
    )


@pytest.mark.parametrize("kind", sorted(_CLOSABLE_KINDS))
async def test_status_and_arm_agree_for_each_deleted_tenant_effect_kind(
    world: _Harness, kind: str
) -> None:
    covered = await subscribed_builder(world)
    await deleted_tenant_orphan(world, covered.builder_id, kind)
    expected = _CLOSABLE_KINDS[kind]

    status = await run(world, "status")
    readiness = status.document["readiness"]
    assert readiness["open_orphans"] >= 1 and readiness["ready"] is False
    assert readiness["deletion_closures_arm_writes"] == 1
    after = readiness["orphans_after_arm_closures"]
    if expected == "armed":
        assert after == NO_ORPHANS
        assert readiness["ready_after_arm_closures"] is True
    else:
        assert after == {**NO_ORPHANS, "open": 1, "trace_partial_debit": 1}
        assert readiness["ready_after_arm_closures"] is False

    outcome = await run(
        world, "arm", "--report-digest", status.document["cutoff"]["digest"]
    )

    if expected == "armed":
        assert outcome.code == 0, outcome.document
        assert await _closures(world) == 1
    else:
        assert outcome.code == ops.EXIT_CODES["refused"], outcome.document
        assert outcome.document["reasons"] == ["orphans_open"]
        assert await _closures(world) == 0  # rolled back with the refusal
        assert await state_rows(world) == (0, None, None)


async def test_a_deleted_tenant_effect_of_another_kind_is_not_closed(
    world: _Harness,
) -> None:
    """Not an orphan kind: no orphan, no closure from ``arm``, and ``status``
    says so (the kind filter of the closing selection)."""

    covered = await subscribed_builder(world)
    kind = "debt_settlement"
    assert kind not in ORPHAN_EFFECT_KINDS
    await deleted_tenant_orphan(world, covered.builder_id, kind)

    status = await run(world, "status")
    readiness = status.document["readiness"]
    assert readiness["orphans"] == NO_ORPHANS
    assert readiness["deletion_closures_arm_writes"] == 0
    assert readiness["orphans_after_arm_closures"] == NO_ORPHANS

    outcome = await run(
        world, "arm", "--report-digest", status.document["cutoff"]["digest"]
    )

    assert outcome.code == 0, outcome.document
    assert await _closures(world) == 0
