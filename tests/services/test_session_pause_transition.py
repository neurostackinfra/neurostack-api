"""Session pause/reactivation CAS contract tests (wish Group 2, chunk 4).

Covers 7 of the Group 2 acceptance criteria that focus on the CAS primitives'
correctness under concurrency, retries, and cross-variant equivalence:

* ``AC2``  — barrier: AI handoff vs human takeover over the same active session
* ``AC3``  — concurrent/repeated reactivations keep a single UUID winner
* ``AC4``  — manual vs auto variants produce identical cognitive state and
             preserve the full classification table (temporary, superseded,
             ended, missing, conflict)
* ``AC5``  — scheduling-link takeover without a handoff record leaves
             ``handoff_records`` byte-for-byte unchanged
* ``AC7``  — resume failure never reopens with stale state; the typed neutral
             object is a valid substitute
* ``AC10`` — UUID generated once outside the retry loop stays stable across
             forced CAS misses, in BOTH reactivate variants
* ``AC11`` — the barrier test also runs against a real Postgres instance when
             ``POSTGRES_SMOKE_DATABASE_URL`` is exported in the environment

Sibling chunks own the test files that exercise event emission for the
context_episode (``tests/services/test_event_service_context_episode.py``) and
the D2 patient-message retention test in ``tests/api/test_webhook_uazapi.py``;
nothing here duplicates them.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.handoff.episode_context import (
    NEUTRAL_SUMMARY_TEXT,
    build_prior_episode_context,
    project_resume_state,
)
from src.models.base import Base
from src.models.handoff import HandoffRecord
from src.models.session import CognitiveStateData, Session
from src.repositories.session import SessionRepository

POSTGRES_SMOKE_DATABASE_URL = os.environ.get("POSTGRES_SMOKE_DATABASE_URL")


# ============================================================
# Shared fixtures / helpers
# ============================================================


def _session(
    tenant_id: UUID,
    *,
    session_id: UUID | None = None,
    user_id: str | None,
    activity: datetime,
    paused: bool = True,
    paused_until: datetime | None = None,
    active: bool = True,
    channel: str = "whatsapp",
    cognitive_state: dict[str, Any] | None = None,
) -> Session:
    """Mirror ``tests/api/test_sessions.py:_session`` but allow an explicit state."""
    state = (
        cognitive_state
        if cognitive_state is not None
        else {
            "last_intent": "scheduling",
            "funnel_stage": "em_qualificacao",
            "private": {"must_not_leak": True},
        }
    )
    return Session(
        id=session_id or uuid4(),
        tenant_id=tenant_id,
        user_id=user_id,
        channel=channel,
        cognitive_state=state,
        session_metadata={},
        last_activity_at=activity,
        is_active=active,
        is_paused=paused,
        paused_until=paused_until,
    )


def _seed(
    client: TestClient,
    *,
    tenants: list[tuple[UUID, UUID | None]],
    sessions: list[Any] | None = None,
    builder_ids: list[UUID] | None = None,
    handoffs: list[Any] | None = None,
    events: list[Any] | None = None,
) -> None:
    from src.models.builder import Builder
    from src.models.tenant import Tenant
    from src.repositories.database import get_db_session

    async def _run() -> None:
        db = get_db_session()
        try:
            for seeded_builder_id in builder_ids or []:
                db.add(
                    Builder(
                        id=seeded_builder_id,
                        name=f"Builder {seeded_builder_id}",
                    )
                )
            for seeded_tenant_id, seeded_tenant_builder_id in tenants:
                db.add(
                    Tenant(
                        id=seeded_tenant_id,
                        name=f"Clinic {seeded_tenant_id}",
                        slug=f"clinic-{seeded_tenant_id}",
                        api_key_hash=f"hash-{seeded_tenant_id}",
                        builder_id=seeded_tenant_builder_id,
                        config={},
                    )
                )
            db.add_all(sessions or [])
            db.add_all(handoffs or [])
            db.add_all(events or [])
            await db.commit()
        finally:
            await db.close()

    client._submit(_run())  # type: ignore[attr-defined]


def _read_session_row(client: TestClient, session_id: UUID) -> dict[str, Any]:
    """Fetch a session row by id from a fresh ``AsyncSession``."""
    from src.repositories.database import get_db_session

    async def _run() -> dict[str, Any]:
        db = get_db_session()
        try:
            row = await db.get(Session, session_id)
            assert row is not None, "session row vanished unexpectedly"
            return {
                "cognitive_state": dict(row.cognitive_state or {}),
                "is_paused": bool(row.is_paused),
                "paused_until": row.paused_until,
            }
        finally:
            await db.close()

    return client._submit(_run())  # type: ignore[attr-defined,no-any-return]


def _handoff_rows(client: TestClient) -> list[dict[str, Any]]:
    """Return every handoff row as serializable dicts, ordered by id."""
    from src.repositories.database import get_db_session

    async def _run() -> list[dict[str, Any]]:
        db = get_db_session()
        try:
            result = await db.execute(select(HandoffRecord))
            rows = list(result.scalars().all())
            rows.sort(key=lambda r: str(r.id))
            return [row.to_dict() for row in rows]
        finally:
            await db.close()

    return client._submit(_run())  # type: ignore[attr-defined,no-any-return]


def _candidate_state(marker: str, *, episode_id: UUID | None = None) -> dict[str, Any]:
    """Build a serialized cognitive state carrying a unique marker.

    The marker is embedded inside ``prior_episode_context.summary`` so that
    both the serialized form (what the CAS writes) and the round-tripped form
    (what ``to_dict`` round-trips) preserve the signal — a merge or a
    last-writer-wins corruption would scramble this.
    """
    prior_ctx = {
        "schema_version": 1,
        "lead_name": None,
        "summary": f"candidate-marker::{marker}",
    }
    state = CognitiveStateData(
        funnel_stage="em_qualificacao",
        intent_history=["scheduling"],
        slots={"candidate_marker": marker},
        flags={},
        turn_count=1,
        safety_triggers=0,
        last_intent="scheduling",
        context_episode_id=str(episode_id) if episode_id is not None else None,
        prior_episode_context=prior_ctx,
    )
    return state.to_dict()


# ============================================================
# AC2 — barrier on pause_effective (AI handoff vs human takeover)
# ============================================================


def test_ac2_pause_barrier_ai_handoff_vs_human_takeover_no_last_writer_wins(
    client: TestClient,
) -> None:
    """Two concurrent pause_effective calls over the same active session.

    AC2 contract: exactly one ``"paused"`` winner, the other is
    ``"already_paused"`` (idempotent loser), the row's cognitive_state is the
    WINNER's candidate byte-for-byte (never merged, never the loser's), and
    ``is_paused=True`` with the WINNER's ``paused_until``.
    """
    tenant_id = uuid4()
    now = datetime.now(timezone.utc)
    session = _session(
        tenant_id,
        user_id="race-user",
        activity=now,
        paused=False,
        paused_until=None,
    )
    _seed(client, tenants=[(tenant_id, None)], sessions=[session])

    ai_state = _candidate_state("ai_handoff")
    human_state = _candidate_state("human_takeover")
    ai_deadline = now + timedelta(hours=1)
    human_deadline = now + timedelta(hours=2)

    async def _race() -> tuple[str, str]:
        from src.repositories.database import get_db_session

        db_a = get_db_session()
        db_b = get_db_session()
        try:
            repo_a = SessionRepository(db_a)
            repo_b = SessionRepository(db_b)
            start = asyncio.Event()

            async def _ai() -> str:
                await start.wait()
                outcome = await repo_a.pause_effective(
                    tenant_id,
                    session.id,
                    cognitive_state=ai_state,
                    paused_until=ai_deadline,
                )
                await db_a.commit()
                return outcome

            async def _human() -> str:
                await start.wait()
                outcome = await repo_b.pause_effective(
                    tenant_id,
                    session.id,
                    cognitive_state=human_state,
                    paused_until=human_deadline,
                )
                await db_b.commit()
                return outcome

            task_ai = asyncio.create_task(_ai())
            task_human = asyncio.create_task(_human())
            # Release both coroutines at the same instant so the ordering is
            # driven by the event loop's scheduling, not by any pre-wait skew.
            start.set()
            return await asyncio.gather(task_ai, task_human)
        finally:
            await db_a.close()
            await db_b.close()

    outcomes = client._submit(_race())  # type: ignore[attr-defined]
    ai_outcome, human_outcome = outcomes

    results = [ai_outcome, human_outcome]
    assert sorted(results) == [
        "already_paused",
        "paused",
    ], f"expected exactly one 'paused' and one 'already_paused', got {results}"

    # Identify the winner by candidate marker, then prove the DB row matches
    # byte-for-byte with the winner's cognitive_state and paused_until.
    if ai_outcome == "paused":
        winner_state = ai_state
        winner_deadline = ai_deadline
        loser_state = human_state
    else:
        winner_state = human_state
        winner_deadline = human_deadline
        loser_state = ai_state

    row = _read_session_row(client, session.id)
    assert row["is_paused"] is True
    # SQLite loses tzinfo; compare naive UTC epochs for determinism.
    stored_deadline = row["paused_until"]
    if stored_deadline is not None and stored_deadline.tzinfo is None:
        stored_deadline = stored_deadline.replace(tzinfo=timezone.utc)
    assert stored_deadline == winner_deadline
    assert (
        row["cognitive_state"] == winner_state
    ), "winning candidate must be persisted byte-for-byte (no merge)"
    assert (
        row["cognitive_state"] != loser_state
    ), "loser's candidate must never land in the row (no last-writer-wins)"


def test_ac2_pause_barrier_ai_versus_ai_complementary_variant(
    client: TestClient,
) -> None:
    """Complementary (not substitute) AI-vs-AI barrier.

    Same CAS contract as the AI-vs-human case: single winner, idempotent
    loser, DB row equals the winner byte-for-byte.
    """
    tenant_id = uuid4()
    now = datetime.now(timezone.utc)
    session = _session(
        tenant_id,
        user_id="race-user-ai-ai",
        activity=now,
        paused=False,
        paused_until=None,
    )
    _seed(client, tenants=[(tenant_id, None)], sessions=[session])

    state_one = _candidate_state("ai_handoff_v1")
    state_two = _candidate_state("ai_handoff_v2")
    deadline_one = now + timedelta(hours=3)
    deadline_two = now + timedelta(hours=4)

    async def _race() -> tuple[str, str]:
        from src.repositories.database import get_db_session

        db_a = get_db_session()
        db_b = get_db_session()
        try:
            repo_a = SessionRepository(db_a)
            repo_b = SessionRepository(db_b)
            start = asyncio.Event()

            async def _first() -> str:
                await start.wait()
                outcome = await repo_a.pause_effective(
                    tenant_id,
                    session.id,
                    cognitive_state=state_one,
                    paused_until=deadline_one,
                )
                await db_a.commit()
                return outcome

            async def _second() -> str:
                await start.wait()
                outcome = await repo_b.pause_effective(
                    tenant_id,
                    session.id,
                    cognitive_state=state_two,
                    paused_until=deadline_two,
                )
                await db_b.commit()
                return outcome

            task_one = asyncio.create_task(_first())
            task_two = asyncio.create_task(_second())
            start.set()
            return await asyncio.gather(task_one, task_two)
        finally:
            await db_a.close()
            await db_b.close()

    outcomes = client._submit(_race())  # type: ignore[attr-defined]
    assert sorted(outcomes) == ["already_paused", "paused"]
    row = _read_session_row(client, session.id)
    assert row["is_paused"] is True
    assert row["cognitive_state"] in (state_one, state_two)


# ============================================================
# AC3 — concurrent/repeated reactivations with UUID-once contract
# ============================================================


def test_ac3_reactivate_manual_barrier_single_uuid_winner(
    client: TestClient,
) -> None:
    """Two concurrent reactivations: exactly one wins, UUID is NOT a hybrid.

    The final row's ``cognitive_state["context_episode_id"]`` must equal the
    winner's episode id (and only that one). A partial write would show a
    merged dict / foreign episode id.
    """
    tenant_id = uuid4()
    now = datetime.now(timezone.utc)
    session = _session(
        tenant_id,
        user_id="reactivate-race-user",
        activity=now,
        paused=True,
        paused_until=None,  # permanent pause (precondition for manual CAS)
    )
    _seed(client, tenants=[(tenant_id, None)], sessions=[session])

    episode_a = uuid4()
    episode_b = uuid4()
    state_a = _candidate_state("resume_a", episode_id=episode_a)
    state_b = _candidate_state("resume_b", episode_id=episode_b)

    async def _race() -> tuple[str, str]:
        from src.repositories.database import get_db_session

        db_a = get_db_session()
        db_b = get_db_session()
        try:
            repo_a = SessionRepository(db_a)
            repo_b = SessionRepository(db_b)
            start = asyncio.Event()

            async def _a() -> str:
                await start.wait()
                outcome = await repo_a.reactivate_effective_manual(
                    tenant_id=tenant_id,
                    session_id=session.id,
                    new_cognitive_state=state_a,
                    new_episode_id=episode_a,
                )
                await db_a.commit()
                return outcome

            async def _b() -> str:
                await start.wait()
                outcome = await repo_b.reactivate_effective_manual(
                    tenant_id=tenant_id,
                    session_id=session.id,
                    new_cognitive_state=state_b,
                    new_episode_id=episode_b,
                )
                await db_b.commit()
                return outcome

            task_a = asyncio.create_task(_a())
            task_b = asyncio.create_task(_b())
            start.set()
            return await asyncio.gather(task_a, task_b)
        finally:
            await db_a.close()
            await db_b.close()

    outcomes = client._submit(_race())  # type: ignore[attr-defined]
    outcome_a, outcome_b = outcomes
    assert sorted(outcomes) == [
        "already_active",
        "reactivated",
    ], f"expected one reactivated + one already_active, got {outcomes}"

    winner_episode_id = str(episode_a if outcome_a == "reactivated" else episode_b)
    row = _read_session_row(client, session.id)
    assert row["is_paused"] is False
    assert row["paused_until"] is None
    assert row["cognitive_state"]["context_episode_id"] == winner_episode_id
    # And never a hybrid: the OTHER episode id must not appear anywhere in the
    # serialized state.
    other_id = str(episode_b if outcome_a == "reactivated" else episode_a)
    assert other_id not in str(row["cognitive_state"])


def test_ac3_repeated_sequential_reactivations_idempotent(
    client: TestClient,
) -> None:
    """A second, repeat reactivation after a successful one is already_active."""
    tenant_id = uuid4()
    now = datetime.now(timezone.utc)
    session = _session(
        tenant_id,
        user_id="repeat-reactivate",
        activity=now,
        paused=True,
        paused_until=None,
    )
    _seed(client, tenants=[(tenant_id, None)], sessions=[session])

    episode_id = uuid4()
    new_state = _candidate_state("resume_once", episode_id=episode_id)

    async def _run() -> tuple[str, str]:
        from src.repositories.database import get_db_session

        db = get_db_session()
        try:
            repo = SessionRepository(db)
            first = await repo.reactivate_effective_manual(
                tenant_id=tenant_id,
                session_id=session.id,
                new_cognitive_state=new_state,
                new_episode_id=episode_id,
            )
            await db.commit()
            # Second call with a brand-new episode id must NOT be allowed to
            # overwrite the first winner's state.
            second_episode = uuid4()
            second_state = _candidate_state("resume_twice", episode_id=second_episode)
            second = await repo.reactivate_effective_manual(
                tenant_id=tenant_id,
                session_id=session.id,
                new_cognitive_state=second_state,
                new_episode_id=second_episode,
            )
            await db.commit()
            return first, second
        finally:
            await db.close()

    first_outcome, second_outcome = client._submit(_run())  # type: ignore[attr-defined]
    assert first_outcome == "reactivated"
    assert second_outcome == "already_active"
    row = _read_session_row(client, session.id)
    assert row["cognitive_state"]["context_episode_id"] == str(episode_id)


# ============================================================
# AC4 — manual vs auto equivalence + full classification preservation
# ============================================================


def test_ac4_manual_and_auto_write_identical_cognitive_state(
    client: TestClient,
) -> None:
    """Given the same projected resume state, manual and auto UPDATE
    identically — the two repository methods are pure pass-throughs of the
    serialized dict.
    """
    tenant_id = uuid4()
    now = datetime.now(timezone.utc)
    manual_session = _session(
        tenant_id,
        user_id="equiv-manual",
        activity=now,
        paused=True,
        paused_until=None,
    )
    auto_session = _session(
        tenant_id,
        user_id="equiv-auto",
        activity=now,
        paused=True,
        paused_until=now - timedelta(minutes=1),  # already expired
    )
    _seed(
        client,
        tenants=[(tenant_id, None)],
        sessions=[manual_session, auto_session],
    )

    prior_dict = {
        "funnel_stage": "em_qualificacao",
        "slots": {"lead_name": "Ana", "already_patient": True},
        "intent_history": ["scheduling"],
        "last_intent": "scheduling",
    }
    prior_state = CognitiveStateData.from_dict(prior_dict)
    episode_id = uuid4()
    prior_ctx = {
        "schema_version": 1,
        "lead_name": "Ana",
        "summary": "contexto do episódio anterior",
    }
    projected = project_resume_state(
        prior_state,
        context_episode_id=str(episode_id),
        prior_episode_context=prior_ctx,
    )
    new_state = projected.to_dict()

    async def _run() -> tuple[str, str]:
        from src.repositories.database import get_db_session

        db = get_db_session()
        try:
            repo = SessionRepository(db)
            manual_outcome = await repo.reactivate_effective_manual(
                tenant_id=tenant_id,
                session_id=manual_session.id,
                new_cognitive_state=new_state,
                new_episode_id=episode_id,
            )
            auto_outcome = await repo.reactivate_effective_auto(
                tenant_id=tenant_id,
                session_id=auto_session.id,
                new_cognitive_state=new_state,
                new_episode_id=episode_id,
                now=now,
            )
            await db.commit()
            return manual_outcome, auto_outcome
        finally:
            await db.close()

    manual_result, auto_result = client._submit(_run())  # type: ignore[attr-defined]
    assert manual_result == "reactivated"
    assert auto_result == "reactivated"

    manual_row = _read_session_row(client, manual_session.id)
    auto_row = _read_session_row(client, auto_session.id)
    assert (
        manual_row["cognitive_state"] == auto_row["cognitive_state"]
    ), "manual and auto reactivate paths must persist the same cognitive_state"
    assert manual_row["cognitive_state"] == new_state
    assert manual_row["is_paused"] is False
    assert auto_row["is_paused"] is False


def test_ac4_manual_classification_temporary_pause(client: TestClient) -> None:
    """Manual CAS over a temporary (paused_until set) row returns temporary_pause."""
    tenant_id = uuid4()
    now = datetime.now(timezone.utc)
    temp_session = _session(
        tenant_id,
        user_id="manual-temp",
        activity=now,
        paused=True,
        paused_until=now + timedelta(hours=1),
    )
    _seed(client, tenants=[(tenant_id, None)], sessions=[temp_session])

    episode_id = uuid4()
    state = _candidate_state("resume_temp", episode_id=episode_id)

    async def _run() -> str:
        from src.repositories.database import get_db_session

        db = get_db_session()
        try:
            return await SessionRepository(db).reactivate_effective_manual(
                tenant_id=tenant_id,
                session_id=temp_session.id,
                new_cognitive_state=state,
                new_episode_id=episode_id,
            )
        finally:
            await db.close()

    assert client._submit(_run()) == "temporary_pause"  # type: ignore[attr-defined]


def test_ac4_manual_classification_superseded_session(client: TestClient) -> None:
    """Manual CAS over an older row for the same (tenant, user) is superseded."""
    tenant_id = uuid4()
    now = datetime.now(timezone.utc)
    older = _session(
        tenant_id,
        user_id="manual-super",
        activity=now - timedelta(minutes=5),
        paused=True,
        paused_until=None,
    )
    newer = _session(
        tenant_id,
        user_id="manual-super",
        activity=now,
        paused=True,
        paused_until=None,
    )
    _seed(client, tenants=[(tenant_id, None)], sessions=[older, newer])

    episode_id = uuid4()
    state = _candidate_state("resume_super", episode_id=episode_id)

    async def _run() -> str:
        from src.repositories.database import get_db_session

        db = get_db_session()
        try:
            return await SessionRepository(db).reactivate_effective_manual(
                tenant_id=tenant_id,
                session_id=older.id,
                new_cognitive_state=state,
                new_episode_id=episode_id,
            )
        finally:
            await db.close()

    assert client._submit(_run()) == "superseded_session"  # type: ignore[attr-defined]


def test_ac4_manual_classification_not_found_covers_missing_and_ended(
    client: TestClient,
) -> None:
    """Manual CAS over a missing or ended row yields not_found.

    The classifier's branch ``if snapshot is None or not snapshot.is_active``
    maps both "no row at all" and "row exists but is_active=False" to
    ``not_found`` — this test locks that mapping in.
    """
    tenant_id = uuid4()
    now = datetime.now(timezone.utc)
    ended = _session(
        tenant_id,
        user_id="manual-ended",
        activity=now,
        paused=True,
        paused_until=None,
        active=False,
    )
    _seed(client, tenants=[(tenant_id, None)], sessions=[ended])

    missing_id = uuid4()
    episode_id = uuid4()
    state = _candidate_state("resume_nf", episode_id=episode_id)

    async def _run() -> tuple[str, str]:
        from src.repositories.database import get_db_session

        db = get_db_session()
        try:
            repo = SessionRepository(db)
            missing = await repo.reactivate_effective_manual(
                tenant_id=tenant_id,
                session_id=missing_id,
                new_cognitive_state=state,
                new_episode_id=episode_id,
            )
            ended_outcome = await repo.reactivate_effective_manual(
                tenant_id=tenant_id,
                session_id=ended.id,
                new_cognitive_state=state,
                new_episode_id=episode_id,
            )
            return missing, ended_outcome
        finally:
            await db.close()

    missing_outcome, ended_outcome = client._submit(_run())  # type: ignore[attr-defined]
    assert missing_outcome == "not_found"
    assert ended_outcome == "not_found"


async def test_ac4_manual_classification_reactivation_conflict() -> None:
    """Manual CAS: forced miss on every attempt returns reactivation_conflict."""
    tenant_id = uuid4()
    session_id = uuid4()
    episode_id = uuid4()
    db = AsyncMock()
    permanently_paused = SimpleNamespace(
        id=session_id,
        user_id="conflict-user",
        is_active=True,
        is_paused=True,
        paused_until=None,
        effective_id=session_id,
    )
    db.execute.side_effect = [
        item
        for _ in range(SessionRepository._REACTIVATION_MAX_ATTEMPTS)
        for item in (
            SimpleNamespace(rowcount=0),
            SimpleNamespace(one_or_none=lambda: permanently_paused),
        )
    ]

    result = await SessionRepository(db).reactivate_effective_manual(
        tenant_id=tenant_id,
        session_id=session_id,
        new_cognitive_state={"placeholder": True},
        new_episode_id=episode_id,
    )

    assert result == "reactivation_conflict"
    db.flush.assert_not_awaited()


def test_ac4_auto_classification_already_active(client: TestClient) -> None:
    """Auto CAS over an already-unpaused row returns already_active."""
    tenant_id = uuid4()
    now = datetime.now(timezone.utc)
    active = _session(
        tenant_id,
        user_id="auto-already-active",
        activity=now,
        paused=False,
        paused_until=None,
    )
    _seed(client, tenants=[(tenant_id, None)], sessions=[active])

    episode_id = uuid4()
    state = _candidate_state("resume_auto_aa", episode_id=episode_id)

    async def _run() -> str:
        from src.repositories.database import get_db_session

        db = get_db_session()
        try:
            return await SessionRepository(db).reactivate_effective_auto(
                tenant_id=tenant_id,
                session_id=active.id,
                new_cognitive_state=state,
                new_episode_id=episode_id,
                now=now,
            )
        finally:
            await db.close()

    assert client._submit(_run()) == "already_active"  # type: ignore[attr-defined]


def test_ac4_auto_classification_superseded_session(client: TestClient) -> None:
    """Auto CAS over an older row for the same (tenant, user) is superseded."""
    tenant_id = uuid4()
    now = datetime.now(timezone.utc)
    older = _session(
        tenant_id,
        user_id="auto-super",
        activity=now - timedelta(minutes=5),
        paused=True,
        paused_until=now - timedelta(minutes=10),
    )
    newer = _session(
        tenant_id,
        user_id="auto-super",
        activity=now,
        paused=True,
        paused_until=now - timedelta(minutes=10),
    )
    _seed(client, tenants=[(tenant_id, None)], sessions=[older, newer])

    episode_id = uuid4()
    state = _candidate_state("resume_auto_super", episode_id=episode_id)

    async def _run() -> str:
        from src.repositories.database import get_db_session

        db = get_db_session()
        try:
            return await SessionRepository(db).reactivate_effective_auto(
                tenant_id=tenant_id,
                session_id=older.id,
                new_cognitive_state=state,
                new_episode_id=episode_id,
                now=now,
            )
        finally:
            await db.close()

    assert client._submit(_run()) == "superseded_session"  # type: ignore[attr-defined]


def test_ac4_auto_classification_not_found_covers_missing_and_ended(
    client: TestClient,
) -> None:
    """Auto CAS over a missing or ended row yields not_found."""
    tenant_id = uuid4()
    now = datetime.now(timezone.utc)
    ended = _session(
        tenant_id,
        user_id="auto-ended",
        activity=now,
        paused=True,
        paused_until=now - timedelta(minutes=10),
        active=False,
    )
    _seed(client, tenants=[(tenant_id, None)], sessions=[ended])

    missing_id = uuid4()
    episode_id = uuid4()
    state = _candidate_state("resume_auto_nf", episode_id=episode_id)

    async def _run() -> tuple[str, str]:
        from src.repositories.database import get_db_session

        db = get_db_session()
        try:
            repo = SessionRepository(db)
            missing = await repo.reactivate_effective_auto(
                tenant_id=tenant_id,
                session_id=missing_id,
                new_cognitive_state=state,
                new_episode_id=episode_id,
                now=now,
            )
            ended_outcome = await repo.reactivate_effective_auto(
                tenant_id=tenant_id,
                session_id=ended.id,
                new_cognitive_state=state,
                new_episode_id=episode_id,
                now=now,
            )
            return missing, ended_outcome
        finally:
            await db.close()

    missing_outcome, ended_outcome = client._submit(_run())  # type: ignore[attr-defined]
    assert missing_outcome == "not_found"
    assert ended_outcome == "not_found"


def test_ac4_auto_classification_not_yet_expired(client: TestClient) -> None:
    """Auto CAS over a row whose paused_until is still in the future.

    The precondition ``paused_until <= now`` fails, the UPDATE misses, and the
    classifier returns ``not_yet_expired`` — a literal that does NOT exist
    for the manual variant.
    """
    tenant_id = uuid4()
    # SQLite stores DateTime(timezone=True) as naive text; the classifier
    # branch for not_yet_expired compares ``snapshot.paused_until > now``
    # directly, so both sides must share tz awareness. Mirror the aware/naive
    # convention the real webhook uses when reading the row back.
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    future_pause = _session(
        tenant_id,
        user_id="auto-not-yet",
        activity=now_naive,
        paused=True,
        paused_until=now_naive + timedelta(hours=1),
    )
    _seed(client, tenants=[(tenant_id, None)], sessions=[future_pause])

    episode_id = uuid4()
    state = _candidate_state("resume_auto_ny", episode_id=episode_id)

    async def _run() -> str:
        from src.repositories.database import get_db_session

        db = get_db_session()
        try:
            return await SessionRepository(db).reactivate_effective_auto(
                tenant_id=tenant_id,
                session_id=future_pause.id,
                new_cognitive_state=state,
                new_episode_id=episode_id,
                now=now_naive,
            )
        finally:
            await db.close()

    assert client._submit(_run()) == "not_yet_expired"  # type: ignore[attr-defined]


async def test_ac4_auto_classification_reactivation_conflict() -> None:
    """Auto CAS: forced miss on every attempt returns reactivation_conflict."""
    tenant_id = uuid4()
    session_id = uuid4()
    episode_id = uuid4()
    now = datetime.now(timezone.utc)
    db = AsyncMock()
    expired_pause = SimpleNamespace(
        id=session_id,
        user_id="auto-conflict",
        is_active=True,
        is_paused=True,
        paused_until=now - timedelta(minutes=1),
        effective_id=session_id,
    )
    db.execute.side_effect = [
        item
        for _ in range(SessionRepository._REACTIVATION_MAX_ATTEMPTS)
        for item in (
            SimpleNamespace(rowcount=0),
            SimpleNamespace(one_or_none=lambda: expired_pause),
        )
    ]

    result = await SessionRepository(db).reactivate_effective_auto(
        tenant_id=tenant_id,
        session_id=session_id,
        new_cognitive_state={"placeholder": True},
        new_episode_id=episode_id,
        now=now,
    )

    assert result == "reactivation_conflict"
    db.flush.assert_not_awaited()


# ============================================================
# AC5 — scheduling-link takeover without a record leaves handoff_records
#        byte-for-byte unchanged
# ============================================================


def test_ac5_scheduling_link_takeover_without_record_leaves_handoffs_untouched(
    client: TestClient,
) -> None:
    """Human-takeover pause path never writes to ``handoff_records``.

    Seed one pre-existing unrelated handoff row to make the "byte-for-byte
    unchanged" assertion meaningful, plus a handful of patient/AI events, then
    run the exact composition + pause_effective sequence that
    ``_handle_clinic_message`` uses when there is no record to produce.
    Assert the full handoff_records table is identical before and after.
    """
    from src.api.routes.webhook import _load_ai_person_history
    from src.models.event import Event, EventType

    tenant_id = uuid4()
    now = datetime.now(timezone.utc)
    session = _session(
        tenant_id,
        user_id="scheduling-link-user",
        activity=now,
        paused=False,
        paused_until=None,
    )
    # Pre-existing unrelated handoff row that must NOT be mutated by the pause.
    unrelated_handoff = HandoffRecord(
        tenant_id=tenant_id,
        session_id=session.id,
        reason="user_request",
        summary="pre-existing untouched handoff",
        context={"nested": {"keep": True}},
        status="pending",
        urgency="normal",
    )
    user_msg = Event(
        tenant_id=tenant_id,
        session_id=session.id,
        event_type=EventType.MESSAGE_IN.value,
        event_data={"content": "ola, meu nome é Ana"},
        event_metadata={},
    )
    bot_msg = Event(
        tenant_id=tenant_id,
        session_id=session.id,
        event_type=EventType.PROMPT_GENERATED.value,
        event_data={"suggested_response": "Oi Ana, como posso ajudar?"},
        event_metadata={},
    )
    _seed(
        client,
        tenants=[(tenant_id, None)],
        sessions=[session],
        handoffs=[unrelated_handoff],
        events=[user_msg, bot_msg],
    )

    before = _handoff_rows(client)
    assert len(before) == 1

    async def _takeover() -> str:
        from src.repositories.database import get_db_session

        db = get_db_session()
        try:
            ai_person_events = await _load_ai_person_history(
                tenant_id, session.id, db, limit=20
            )
            prior_context = build_prior_episode_context(
                tenant_id=tenant_id,
                session_id=session.id,
                events=ai_person_events,
                current_message=None,
            )
            # Prove the composer sees ONLY the AI/person events we seeded (no
            # handoff records consulted, no scheduling-link side effects).
            assert prior_context["schema_version"] == 1
            # The composer's lead_name extraction uses the same source of truth
            # as the normal flow — the self-identifying user message is here.
            assert prior_context["lead_name"] == "Ana"
            cognitive = session.get_cognitive_state()
            cognitive.prior_episode_context = prior_context
            outcome = await SessionRepository(db).pause_effective(
                tenant_id,
                session.id,
                cognitive_state=cognitive.to_dict(),
                paused_until=None,  # permanent pause ("sem record" branch)
            )
            await db.commit()
            return outcome
        finally:
            await db.close()

    outcome = client._submit(_takeover())  # type: ignore[attr-defined]
    assert outcome == "paused"

    after = _handoff_rows(client)
    assert after == before, (
        "handoff_records table must remain byte-for-byte unchanged after a "
        "scheduling-link human takeover (no record created, no record mutated)"
    )
    row = _read_session_row(client, session.id)
    assert row["is_paused"] is True
    assert row["cognitive_state"]["prior_episode_context"]["lead_name"] == "Ana"


# ============================================================
# AC7 — resume failure never reopens with stale state; neutral object wins
# ============================================================


def test_ac7_build_prior_episode_context_never_raises_on_summarizer_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_prior_episode_context`` must swallow summarizer failures and
    return the neutral typed object — this is the contract AC7 relies on.
    """
    from src.handoff.summarizer import ConversationSummarizer

    def _raise_boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("summarizer explodes on purpose")

    monkeypatch.setattr(ConversationSummarizer, "summarize", _raise_boom)

    result = build_prior_episode_context(
        tenant_id=uuid4(),
        session_id=uuid4(),
        events=[{"role": "user", "content": "qualquer coisa"}],
        current_message=None,
    )

    assert result == {
        "schema_version": 1,
        "lead_name": None,
        "summary": NEUTRAL_SUMMARY_TEXT,
    }


def test_ac7_reactivation_with_neutral_context_still_succeeds(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even when ``build_prior_episode_context`` degrades to the neutral
    object (summarizer failure), the manual CAS still succeeds and the row's
    ``prior_episode_context`` is the neutral object — never the old,
    pre-pause state, never a block on the reactivation.
    """
    from src.handoff.summarizer import ConversationSummarizer

    tenant_id = uuid4()
    now = datetime.now(timezone.utc)
    old_prior_ctx = {
        "schema_version": 1,
        "lead_name": "Stale",
        "summary": "OLD prior-pause summary that must NOT survive",
    }
    stale_prior_state = CognitiveStateData(
        funnel_stage="em_qualificacao",
        intent_history=["scheduling"],
        slots={"lead_name": "Stale", "already_patient": True},
        prior_episode_context=old_prior_ctx,
    )
    session = _session(
        tenant_id,
        user_id="stale-resume-user",
        activity=now,
        paused=True,
        paused_until=None,
        cognitive_state=stale_prior_state.to_dict(),
    )
    _seed(client, tenants=[(tenant_id, None)], sessions=[session])

    def _raise_boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("summarizer explodes on purpose")

    monkeypatch.setattr(ConversationSummarizer, "summarize", _raise_boom)

    # Compose exactly the way reactivate_session does (with the composer failing
    # under the hood). The caller still gets a typed neutral object back.
    neutral_ctx = build_prior_episode_context(
        tenant_id=tenant_id,
        session_id=session.id,
        events=[{"role": "user", "content": "mensagem"}],
        current_message=None,
    )
    assert neutral_ctx["summary"] == NEUTRAL_SUMMARY_TEXT

    new_episode_id = uuid4()
    projected = project_resume_state(
        stale_prior_state,
        context_episode_id=str(new_episode_id),
        prior_episode_context=neutral_ctx,
    )
    new_state = projected.to_dict()

    async def _run() -> str:
        from src.repositories.database import get_db_session

        db = get_db_session()
        try:
            outcome = await SessionRepository(db).reactivate_effective_manual(
                tenant_id=tenant_id,
                session_id=session.id,
                new_cognitive_state=new_state,
                new_episode_id=new_episode_id,
            )
            await db.commit()
            return outcome
        finally:
            await db.close()

    assert client._submit(_run()) == "reactivated"  # type: ignore[attr-defined]
    row = _read_session_row(client, session.id)
    assert row["is_paused"] is False
    assert row["cognitive_state"]["prior_episode_context"] == neutral_ctx
    assert row["cognitive_state"]["prior_episode_context"] != old_prior_ctx


# ============================================================
# AC10 / A3 — UUID generated once stays stable across retry CAS misses
# ============================================================


async def test_ac10_manual_retry_preserves_single_uuid_and_state() -> None:
    """Forced CAS misses in the manual variant: the same cognitive_state /
    episode_id pair passed in at call time is what reaches the successful
    UPDATE — the repository never rebuilds state or regenerates a UUID
    inside the retry loop.
    """
    tenant_id = uuid4()
    session_id = uuid4()
    episode_id = uuid4()
    new_state = {
        "context_episode_id": str(episode_id),
        "candidate_marker": "ac10_manual_once",
    }
    permanently_paused = SimpleNamespace(
        id=session_id,
        user_id="retry-manual",
        is_active=True,
        is_paused=True,
        paused_until=None,
        effective_id=session_id,
    )
    db = AsyncMock()
    db.execute.side_effect = [
        SimpleNamespace(rowcount=0),  # first UPDATE misses
        SimpleNamespace(one_or_none=lambda: permanently_paused),  # classify
        SimpleNamespace(rowcount=0),  # second UPDATE misses
        SimpleNamespace(one_or_none=lambda: permanently_paused),  # classify
        SimpleNamespace(rowcount=1),  # third UPDATE wins
    ]

    result = await SessionRepository(db).reactivate_effective_manual(
        tenant_id=tenant_id,
        session_id=session_id,
        new_cognitive_state=new_state,
        new_episode_id=episode_id,
    )

    assert result == "reactivated"
    assert db.execute.await_count == 5
    db.flush.assert_awaited_once()
    # Every UPDATE call carries the SAME statement object (built once outside
    # the loop). Inspecting the compiled parameters would be brittle; instead
    # inspect call_args_list and confirm the first positional argument (the
    # stmt) is identity-equal across all UPDATE invocations — a UUID
    # regeneration inside the loop would have forced a new stmt binding.
    update_calls = [
        call for i, call in enumerate(db.execute.call_args_list) if i % 2 == 0
    ]
    assert len(update_calls) == 3
    first_stmt = update_calls[0].args[0]
    for call in update_calls[1:]:
        assert call.args[0] is first_stmt, (
            "retry loop must reuse the same UPDATE stmt; a per-iteration "
            "rebuild would permit a per-iteration UUID regeneration"
        )
    # And the caller-provided dict object is still referentially the one we
    # passed in — the repository must not have copied / rewritten it.
    assert new_state["context_episode_id"] == str(episode_id)
    assert new_state["candidate_marker"] == "ac10_manual_once"


async def test_ac10_auto_retry_preserves_single_uuid_and_state() -> None:
    """Same contract as the manual variant, verified for the auto variant."""
    tenant_id = uuid4()
    session_id = uuid4()
    episode_id = uuid4()
    now = datetime.now(timezone.utc)
    new_state = {
        "context_episode_id": str(episode_id),
        "candidate_marker": "ac10_auto_once",
    }
    expired = SimpleNamespace(
        id=session_id,
        user_id="retry-auto",
        is_active=True,
        is_paused=True,
        paused_until=now - timedelta(minutes=5),
        effective_id=session_id,
    )
    db = AsyncMock()
    db.execute.side_effect = [
        SimpleNamespace(rowcount=0),
        SimpleNamespace(one_or_none=lambda: expired),
        SimpleNamespace(rowcount=0),
        SimpleNamespace(one_or_none=lambda: expired),
        SimpleNamespace(rowcount=1),
    ]

    result = await SessionRepository(db).reactivate_effective_auto(
        tenant_id=tenant_id,
        session_id=session_id,
        new_cognitive_state=new_state,
        new_episode_id=episode_id,
        now=now,
    )

    assert result == "reactivated"
    assert db.execute.await_count == 5
    db.flush.assert_awaited_once()
    update_calls = [
        call for i, call in enumerate(db.execute.call_args_list) if i % 2 == 0
    ]
    assert len(update_calls) == 3
    first_stmt = update_calls[0].args[0]
    for call in update_calls[1:]:
        assert (
            call.args[0] is first_stmt
        ), "auto retry loop must reuse the same UPDATE stmt object"
    assert new_state["context_episode_id"] == str(episode_id)
    assert new_state["candidate_marker"] == "ac10_auto_once"


# ============================================================
# AC11 / A8a — Postgres smoke test (skipped unless env var provided)
# ============================================================


@pytest.mark.skipif(
    not POSTGRES_SMOKE_DATABASE_URL,
    reason=(
        "POSTGRES_SMOKE_DATABASE_URL not set — Postgres barrier smoke test "
        "skipped (aiosqlite barrier coverage still runs)"
    ),
)
async def test_ac11_postgres_barrier_matches_sqlite_semantics() -> None:
    """Run the AC2 barrier against a real Postgres instance.

    Builds its own engine/sessionmaker pointed at ``POSTGRES_SMOKE_DATABASE_URL``
    so that two coroutines each hold a different physical connection and
    truly race. Asserts the same single-winner, idempotent-loser contract.
    """
    assert POSTGRES_SMOKE_DATABASE_URL is not None
    engine = create_async_engine(POSTGRES_SMOKE_DATABASE_URL, future=True)
    sessionmaker = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        tenant_id = uuid4()
        now = datetime.now(timezone.utc)
        session_id = uuid4()

        async with sessionmaker() as setup_db:
            from src.models.tenant import Tenant

            setup_db.add(
                Tenant(
                    id=tenant_id,
                    name=f"Clinic {tenant_id}",
                    slug=f"clinic-{tenant_id}",
                    api_key_hash=f"hash-{tenant_id}",
                    config={},
                )
            )
            setup_db.add(
                _session(
                    tenant_id,
                    session_id=session_id,
                    user_id="pg-race-user",
                    activity=now,
                    paused=False,
                    paused_until=None,
                )
            )
            await setup_db.commit()

        ai_state = _candidate_state("ai_handoff_pg")
        human_state = _candidate_state("human_takeover_pg")
        ai_deadline = now + timedelta(hours=1)
        human_deadline = now + timedelta(hours=2)
        start = asyncio.Event()

        async def _pause(state: dict[str, Any], deadline: datetime) -> str:
            async with sessionmaker() as db:
                await start.wait()
                outcome = await SessionRepository(db).pause_effective(
                    tenant_id,
                    session_id,
                    cognitive_state=state,
                    paused_until=deadline,
                )
                await db.commit()
                return outcome

        task_ai = asyncio.create_task(_pause(ai_state, ai_deadline))
        task_human = asyncio.create_task(_pause(human_state, human_deadline))
        start.set()
        outcomes = await asyncio.gather(task_ai, task_human)
        assert sorted(outcomes) == ["already_paused", "paused"]

        if outcomes[0] == "paused":
            winner_state = ai_state
            winner_deadline = ai_deadline
        else:
            winner_state = human_state
            winner_deadline = human_deadline

        async with sessionmaker() as verify_db:
            row = await verify_db.get(Session, session_id)
            assert row is not None
            assert row.is_paused is True
            stored = row.paused_until
            if stored is not None and stored.tzinfo is None:
                stored = stored.replace(tzinfo=timezone.utc)
            assert stored == winner_deadline
            assert dict(row.cognitive_state or {}) == winner_state
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
