"""NeuroStack API - RLHF Human Review Endpoint Tests

Tests for ``POST /v1/rlhf/ratings/{id}/review``: super-admin-only,
append-only history, and reviewer email captured at write time so
authorship survives the ``users`` row being deleted.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from src.config.settings import settings
from src.models.rlhf_audit_review import RlhfAuditReview
from src.models.rlhf_rating import RlhfRating
from src.repositories import get_db_session

TEST_TENANT_ID = UUID("a0000000-0000-0000-0000-000000000001")


# ============================================
# Helpers (locally replicated — no shared conftest for these)
# ============================================


def _make_jwt(
    role: str,
    tenant_id: UUID | None = None,
    user_id: UUID | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(user_id or uuid4()),
            "tenant_id": str(tenant_id or uuid4()),
            "role": role,
            "email": f"{role}@test.com",
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": str(uuid4()),
        },
        settings.secret_key,
        algorithm="HS256",
    )


@pytest.fixture
def super_admin_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_make_jwt('super_admin')}",
        "Content-Type": "application/json",
    }


@pytest.fixture
def clinic_admin_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_make_jwt('clinic_admin', tenant_id=TEST_TENANT_ID)}",
        "Content-Type": "application/json",
    }


@pytest.fixture
def builder_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_make_jwt('builder')}",
        "Content-Type": "application/json",
    }


@pytest.fixture
def investor_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_make_jwt('investor')}",
        "Content-Type": "application/json",
    }


async def _seed_user(user_id: UUID, email: str, tenant_id: UUID) -> None:
    """Insert a User row so ``revisado_por_email`` resolution can find a match.

    The endpoint resolves the reviewer's email from ``users`` at write time,
    so this helper populates that lookup deterministically.
    """
    from src.models.user import User, UserRole

    session = get_db_session()
    try:
        session.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                email=email,
                password_hash="x",
                role=UserRole.super_admin,
            )
        )
        await session.commit()
    finally:
        await session.close()


async def _seed_rating_event(
    *,
    event_id: UUID,
    tenant_id: UUID,
    rating: int,
    reason: str | None = None,
    subject_tenant_id: UUID | None = None,
    approved_for_training: bool = False,
    accepted_by: UUID | None = None,
    accepted_at: str | None = None,
    created_at: datetime | None = None,
) -> None:
    """Insert an ``rlhf_ratings`` row directly, bypassing the endpoint.

    Mirrors the helper in ``tests/api/test_rlhf.py`` (no shared module).
    ``event_id`` maps to ``RlhfRating.id`` — the name is preserved so
    call-site style matches the sibling test file.
    """
    resolved_subject = subject_tenant_id if subject_tenant_id is not None else tenant_id
    parsed_accepted_at: datetime | None = None
    if accepted_at is not None:
        parsed_accepted_at = datetime.fromisoformat(accepted_at)

    session = get_db_session()
    try:
        row = RlhfRating(
            id=event_id,
            tenant_id=tenant_id,
            subject_tenant_id=resolved_subject,
            session_id=None,
            message_id=f"seed-{event_id.hex[:8]}",
            trace_event_id=None,
            trace_tenant_id=None,
            channel="unknown",
            rating=rating,
            reason=reason,
            agent_response="resp",
            user_message="msg",
            submitted_by=None,
            role=None,
            approved_for_training=approved_for_training,
            ignored=False,
            accepted_by=accepted_by,
            accepted_at=parsed_accepted_at,
        )
        if created_at is not None:
            row.created_at = created_at
        session.add(row)
        await session.commit()
    finally:
        await session.close()


async def _load_reviews(rating_id: UUID) -> list[RlhfAuditReview]:
    """Read back every ``rlhf_audit_reviews`` row for a rating.

    The wish's append-only invariant is verified by asserting both rows
    remain after a flip — the test queries the table directly since the
    review-history GET endpoint is Group 4/6's scope, not this group's.
    """
    session = get_db_session()
    try:
        result = await session.execute(
            select(RlhfAuditReview)
            .where(RlhfAuditReview.rating_id == rating_id)
            .order_by(RlhfAuditReview.revisado_em.desc(), RlhfAuditReview.id.desc())
        )
        return list(result.scalars().all())
    finally:
        await session.close()


async def _null_revisado_por(review_id: UUID) -> None:
    """Simulate the ``users`` row being deleted: SET NULL on ``revisado_por``.

    The endpoint's contract says the email — captured at write time — must
    remain readable even when the FK degrades to NULL.
    """
    session = get_db_session()
    try:
        await session.execute(
            update(RlhfAuditReview)
            .where(RlhfAuditReview.id == review_id)
            .values(revisado_por=None)
        )
        await session.commit()
    finally:
        await session.close()


# ============================================
# Tests
# ============================================


class TestReviewEndpointAuth:
    """Only super_admin may register a human verdict."""

    @pytest.mark.parametrize(
        "role_headers_fixture",
        ["builder_headers", "clinic_admin_headers", "investor_headers"],
    )
    def test_non_super_admin_receives_403(
        self,
        client: TestClient,
        role_headers_fixture: str,
        request: pytest.FixtureRequest,
    ) -> None:
        headers = request.getfixturevalue(role_headers_fixture)
        # A missing rating still 403s: the auth gate fires before the
        # 404 lookup, so a made-up UUID is enough to prove the gate.
        response = client.post(
            f"/v1/rlhf/ratings/{uuid4()}/review",
            headers=headers,
            json={"revisado": True},
        )
        assert response.status_code == 403


class TestReviewEndpointNotFound:
    def test_review_on_unknown_rating_returns_404(
        self, client: TestClient, super_admin_headers
    ) -> None:
        response = client.post(
            f"/v1/rlhf/ratings/{uuid4()}/review",
            headers=super_admin_headers,
            json={"revisado": True},
        )
        assert response.status_code == 404


class TestReviewEndpointHappyPath:
    """Append-only writes; observation is optional; flipping revisado
    keeps both rows in ``rlhf_audit_reviews``."""

    def test_empty_observation_still_marks_reviewed(self, client: TestClient) -> None:
        # Seed a rating and the acting super admin so email resolution
        # returns a real address rather than the "unknown" fallback.
        rating_id = uuid4()
        user_id = uuid4()
        client._submit(
            _seed_rating_event(
                event_id=rating_id,
                tenant_id=TEST_TENANT_ID,
                rating=2,
                reason="motivo suficientemente longo",
                subject_tenant_id=TEST_TENANT_ID,
            )
        )
        client._submit(_seed_user(user_id, "reviewer@test.com", TEST_TENANT_ID))

        headers = {
            "Authorization": f"Bearer {_make_jwt('super_admin', user_id=user_id)}",
            "Content-Type": "application/json",
        }

        # Case 1: observacao absent from the body.
        response = client.post(
            f"/v1/rlhf/ratings/{rating_id}/review",
            headers=headers,
            json={"revisado": True},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["rating_id"] == str(rating_id)
        assert body["revisado"] is True

        rows = client._submit(_load_reviews(rating_id))
        assert len(rows) == 1
        assert rows[0].observacao is None
        assert rows[0].revisado is True
        assert rows[0].revisado_por == user_id
        assert rows[0].revisado_por_email == "reviewer@test.com"

    def test_none_observation_also_marks_reviewed(self, client: TestClient) -> None:
        rating_id = uuid4()
        user_id = uuid4()
        client._submit(
            _seed_rating_event(
                event_id=rating_id,
                tenant_id=TEST_TENANT_ID,
                rating=2,
                reason="motivo suficientemente longo",
                subject_tenant_id=TEST_TENANT_ID,
            )
        )
        client._submit(_seed_user(user_id, "reviewer2@test.com", TEST_TENANT_ID))

        headers = {
            "Authorization": f"Bearer {_make_jwt('super_admin', user_id=user_id)}",
            "Content-Type": "application/json",
        }

        # Case 2: observacao explicitly null in the body.
        response = client.post(
            f"/v1/rlhf/ratings/{rating_id}/review",
            headers=headers,
            json={"revisado": True, "observacao": None},
        )
        assert response.status_code == 200

        rows = client._submit(_load_reviews(rating_id))
        assert len(rows) == 1
        assert rows[0].observacao is None
        assert rows[0].revisado is True

    def test_flip_keeps_both_rows_and_flips_current_state(
        self, client: TestClient
    ) -> None:
        """Append-only: a second review with ``revisado=false`` inserts a
        NEW row (it does not update the first), and both rows remain."""
        rating_id = uuid4()
        user_id = uuid4()
        client._submit(
            _seed_rating_event(
                event_id=rating_id,
                tenant_id=TEST_TENANT_ID,
                rating=2,
                reason="motivo suficientemente longo",
                subject_tenant_id=TEST_TENANT_ID,
            )
        )
        client._submit(_seed_user(user_id, "flipper@test.com", TEST_TENANT_ID))
        headers = {
            "Authorization": f"Bearer {_make_jwt('super_admin', user_id=user_id)}",
            "Content-Type": "application/json",
        }

        # First: mark reviewed with observation.
        first = client.post(
            f"/v1/rlhf/ratings/{rating_id}/review",
            headers=headers,
            json={"revisado": True, "observacao": "aprovado"},
        )
        assert first.status_code == 200

        # Second: flip to not-reviewed.
        second = client.post(
            f"/v1/rlhf/ratings/{rating_id}/review",
            headers=headers,
            json={"revisado": False, "observacao": "revertido"},
        )
        assert second.status_code == 200

        rows = client._submit(_load_reviews(rating_id))
        assert len(rows) == 2, "second review must INSERT, not UPDATE"
        # Newest-first ordering (revisado_em DESC, id DESC): current state
        # is the flipped-back value.
        assert rows[0].revisado is False
        assert rows[0].observacao == "revertido"
        assert rows[1].revisado is True
        assert rows[1].observacao == "aprovado"
        # And the two rows are truly distinct entities.
        assert rows[0].id != rows[1].id


class TestReviewCurrentStateTieBreak:
    """Group 4 will read the current state via
    ``ORDER BY rating_id, revisado_em DESC, id DESC``. When two reviews
    share the exact same ``revisado_em``, the tie-break by ``id DESC``
    must give a deterministic — not flaky — current state."""

    def test_identical_timestamp_current_state_is_deterministic(
        self, client: TestClient
    ) -> None:
        rating_id = uuid4()
        client._submit(
            _seed_rating_event(
                event_id=rating_id,
                tenant_id=TEST_TENANT_ID,
                rating=2,
                reason="motivo suficientemente longo",
                subject_tenant_id=TEST_TENANT_ID,
            )
        )

        # Two audit rows sharing the exact same revisado_em, with a
        # deliberate id ordering so we can predict the tie-break.
        shared_ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        # Force a lexicographic id ordering so the tie-break assertion is
        # deterministic: ``high_id`` sorts strictly after ``low_id``.
        raw_a, raw_b = uuid4(), uuid4()
        low_id, high_id = sorted([raw_a, raw_b], key=lambda u: u.hex)

        async def _seed_two() -> None:
            session = get_db_session()
            try:
                session.add(
                    RlhfAuditReview(
                        id=low_id,
                        rating_id=rating_id,
                        observacao="older-by-id",
                        revisado=False,
                        revisado_por=None,
                        revisado_por_email="a@test.com",
                        revisado_em=shared_ts,
                    )
                )
                session.add(
                    RlhfAuditReview(
                        id=high_id,
                        rating_id=rating_id,
                        observacao="newer-by-id",
                        revisado=True,
                        revisado_por=None,
                        revisado_por_email="b@test.com",
                        revisado_em=shared_ts,
                    )
                )
                await session.commit()
            finally:
                await session.close()

        client._submit(_seed_two())

        # Repeated reads produce the identical current state; higher id
        # wins the tie-break for the exact same timestamp.
        first_read = client._submit(_load_reviews(rating_id))
        second_read = client._submit(_load_reviews(rating_id))

        assert [r.id for r in first_read] == [r.id for r in second_read]
        assert first_read[0].id == high_id
        assert first_read[0].revisado is True
        assert first_read[0].observacao == "newer-by-id"


class TestReviewEmailSurvivesUserDeletion:
    """``revisado_por_email`` is denormalized (captured at write time),
    so nulling ``revisado_por`` — which is what happens when the ``users``
    row is deleted (FK is SET NULL) — must not disturb the email."""

    def test_email_readable_after_revisado_por_is_nulled(
        self, client: TestClient
    ) -> None:
        rating_id = uuid4()
        user_id = uuid4()
        client._submit(
            _seed_rating_event(
                event_id=rating_id,
                tenant_id=TEST_TENANT_ID,
                rating=2,
                reason="motivo suficientemente longo",
                subject_tenant_id=TEST_TENANT_ID,
            )
        )
        client._submit(_seed_user(user_id, "author@test.com", TEST_TENANT_ID))
        headers = {
            "Authorization": f"Bearer {_make_jwt('super_admin', user_id=user_id)}",
            "Content-Type": "application/json",
        }

        response = client.post(
            f"/v1/rlhf/ratings/{rating_id}/review",
            headers=headers,
            json={"revisado": True, "observacao": "com autor"},
        )
        assert response.status_code == 200

        rows_before = client._submit(_load_reviews(rating_id))
        assert len(rows_before) == 1
        assert rows_before[0].revisado_por == user_id
        assert rows_before[0].revisado_por_email == "author@test.com"

        # Simulate the users row being deleted: SET NULL on the FK column.
        client._submit(_null_revisado_por(rows_before[0].id))

        rows_after = client._submit(_load_reviews(rating_id))
        assert len(rows_after) == 1
        assert (
            rows_after[0].revisado_por is None
        ), "precondition: the SET NULL simulation actually nulled the FK"
        # The frozen email survives — proving the column is denormalized,
        # not a live join through ``users``.
        assert rows_after[0].revisado_por_email == "author@test.com"
