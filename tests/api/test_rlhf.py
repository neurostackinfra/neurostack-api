"""
NeuroStack API - RLHF Rating Endpoint Tests
Tests for POST /v1/rlhf/rating, GET /v1/rlhf/ratings, and PATCH
/v1/rlhf/ratings/{id}.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.config.settings import settings
from src.models.event import Event
from src.models.tenant import Tenant
from src.repositories import get_db_session

TEST_TENANT_ID = UUID("a0000000-0000-0000-0000-000000000001")


# ============================================
# Helpers
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
def api_key_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-API-Key": "test-api-key",
    }


def _rating_payload(rating: int, reason: str | None = None) -> dict:
    return {
        "session_id": str(uuid4()),
        "message_id": f"msg-{uuid4().hex[:8]}",
        "rating": rating,
        "reason": reason,
        "agent_response": "Ok.",
        "user_message": "Tenho dúvidas sobre o tratamento",
    }


# ============================================
# POST /v1/rlhf/rating
# ============================================


class TestPostRating:
    def test_neutral_rating_saves_event_without_analysis(
        self, client: TestClient, api_key_headers
    ):
        response = client.post(
            "/v1/rlhf/rating",
            headers=api_key_headers,
            json=_rating_payload(3),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert UUID(data["event_id"])

    def test_low_rating_with_reason_schedules_analysis(
        self, client: TestClient, api_key_headers
    ):
        with patch(
            "src.api.routes.rlhf._analyze_rating_async",
            new=AsyncMock(return_value=None),
        ) as mock_analyze:
            response = client.post(
                "/v1/rlhf/rating",
                headers=api_key_headers,
                json=_rating_payload(1, "Resposta fria demais"),
            )
        assert response.status_code == 200
        event_id = response.json()["event_id"]

        # The event body was created; verify status was set to pending by
        # re-fetching via the listing endpoint
        list_resp = client.get("/v1/rlhf/ratings", headers=api_key_headers)
        assert list_resp.status_code == 200
        matched = [e for e in list_resp.json()["entries"] if e["id"] == event_id]
        assert matched, "created event should appear in ratings list"
        assert matched[0]["ai_analysis_status"] == "pending"

        # Analysis coroutine was scheduled
        assert mock_analyze.called

    def test_low_rating_without_reason_does_not_trigger_analysis(
        self, client: TestClient, api_key_headers
    ):
        with patch(
            "src.api.routes.rlhf._analyze_rating_async",
            new=AsyncMock(return_value=None),
        ) as mock_analyze:
            response = client.post(
                "/v1/rlhf/rating",
                headers=api_key_headers,
                json=_rating_payload(1, reason=None),
            )
        assert response.status_code == 200
        event_id = response.json()["event_id"]

        list_resp = client.get("/v1/rlhf/ratings", headers=api_key_headers)
        matched = [e for e in list_resp.json()["entries"] if e["id"] == event_id]
        assert matched
        assert matched[0].get("ai_analysis_status") is None
        assert not mock_analyze.called

    def test_rating_three_with_substantive_reason_schedules_analysis(
        self, client: TestClient, api_key_headers
    ):
        """Rating 3 with an >=8-char written critique now triggers analysis.

        The threshold moved from `rating <= 2` to `rating <= 3 AND
        len(reason.strip()) >= 8` so mid-scale critiques with substance are
        audited too.
        """
        with patch(
            "src.api.routes.rlhf._analyze_rating_async",
            new=AsyncMock(return_value=None),
        ) as mock_analyze:
            response = client.post(
                "/v1/rlhf/rating",
                headers=api_key_headers,
                # 20 chars trimmed — well above the 8-char floor
                json=_rating_payload(3, "Faltou empatia aqui."),
            )
        assert response.status_code == 200
        event_id = response.json()["event_id"]

        list_resp = client.get("/v1/rlhf/ratings", headers=api_key_headers)
        matched = [e for e in list_resp.json()["entries"] if e["id"] == event_id]
        assert matched, "created event should appear in ratings list"
        assert matched[0]["ai_analysis_status"] == "pending"
        assert mock_analyze.called

    def test_rating_three_with_short_reason_does_not_trigger_analysis(
        self, client: TestClient, api_key_headers
    ):
        """Rating 3 with a reason below the 8-char floor is stored without analysis."""
        with patch(
            "src.api.routes.rlhf._analyze_rating_async",
            new=AsyncMock(return_value=None),
        ) as mock_analyze:
            response = client.post(
                "/v1/rlhf/rating",
                headers=api_key_headers,
                # 7 chars after trim — one below the floor
                json=_rating_payload(3, "abcdefg"),
            )
        assert response.status_code == 200
        event_id = response.json()["event_id"]

        list_resp = client.get("/v1/rlhf/ratings", headers=api_key_headers)
        matched = [e for e in list_resp.json()["entries"] if e["id"] == event_id]
        assert matched
        assert matched[0].get("ai_analysis_status") is None
        assert not mock_analyze.called

    def test_rating_three_with_whitespace_only_reason_does_not_trigger_analysis(
        self, client: TestClient, api_key_headers
    ):
        """Reason of whitespace only trims to empty — no analysis, no reason stored."""
        with patch(
            "src.api.routes.rlhf._analyze_rating_async",
            new=AsyncMock(return_value=None),
        ) as mock_analyze:
            response = client.post(
                "/v1/rlhf/rating",
                headers=api_key_headers,
                json=_rating_payload(3, "          "),
            )
        assert response.status_code == 200
        assert not mock_analyze.called

    def test_rating_four_with_long_reason_does_not_trigger_analysis(
        self, client: TestClient, api_key_headers
    ):
        """Ratings 4 and 5 never trigger analysis, even with a long reason."""
        with patch(
            "src.api.routes.rlhf._analyze_rating_async",
            new=AsyncMock(return_value=None),
        ) as mock_analyze:
            response = client.post(
                "/v1/rlhf/rating",
                headers=api_key_headers,
                json=_rating_payload(4, "Elogio detalhado e substantivo."),
            )
        assert response.status_code == 200
        event_id = response.json()["event_id"]

        list_resp = client.get("/v1/rlhf/ratings", headers=api_key_headers)
        matched = [e for e in list_resp.json()["entries"] if e["id"] == event_id]
        assert matched
        assert matched[0].get("ai_analysis_status") is None
        assert not mock_analyze.called

    def test_rejects_out_of_range_rating(self, client: TestClient, api_key_headers):
        response = client.post(
            "/v1/rlhf/rating",
            headers=api_key_headers,
            json=_rating_payload(0),
        )
        assert response.status_code == 422

        response = client.post(
            "/v1/rlhf/rating",
            headers=api_key_headers,
            json=_rating_payload(6),
        )
        assert response.status_code == 422


# ============================================
# subject_tenant_id derivation on POST /v1/rlhf/rating
# ============================================


async def _seed_tenant(tenant_id: UUID) -> None:
    """Insert a Tenant row so subject_tenant_id lookup can succeed."""
    session = get_db_session()
    try:
        tenant = Tenant(
            id=tenant_id,
            name=f"Subject Clinic {tenant_id.hex[:6]}",
            slug=f"subject-{tenant_id.hex}",
            api_key_hash=f"hash-{tenant_id.hex}",
            config={},
        )
        session.add(tenant)
        await session.commit()
    finally:
        await session.close()


async def _load_event(event_id: UUID) -> Event:
    session = get_db_session()
    try:
        result = await session.execute(select(Event).where(Event.id == event_id))
        return result.scalar_one()
    finally:
        await session.close()


class TestSubjectTenantIdDerivation:
    """Group 3 wish/rating-audit-report: submit_rating must record which
    clinic the rating is *about* (subject_tenant_id) so the per-clinic
    filter on the audit report (Group 4) resolves to a real tenant.

    Event.tenant_id (record owner = rater's tenant) is NEVER mutated by
    this feature — only a new key inside event_data is added.
    """

    def test_valid_uuid_existing_tenant_is_persisted_as_subject(
        self, client: TestClient, api_key_headers
    ) -> None:
        subject_id = uuid4()
        client._submit(_seed_tenant(subject_id))

        payload = _rating_payload(4)
        payload["clinic_tenant_id"] = str(subject_id)

        response = client.post(
            "/v1/rlhf/rating",
            headers=api_key_headers,
            json=payload,
        )
        assert response.status_code == 200
        event_id = UUID(response.json()["event_id"])

        event = client._submit(_load_event(event_id))
        # Event.tenant_id stays the rater's tenant (the record owner).
        assert event.tenant_id == TEST_TENANT_ID
        # subject_tenant_id captures the clinic the rating describes.
        assert event.event_data["subject_tenant_id"] == str(subject_id)

    def test_missing_clinic_tenant_id_falls_back_to_event_tenant(
        self, client: TestClient, api_key_headers
    ) -> None:
        response = client.post(
            "/v1/rlhf/rating",
            headers=api_key_headers,
            json=_rating_payload(5),
        )
        assert response.status_code == 200
        event_id = UUID(response.json()["event_id"])

        event = client._submit(_load_event(event_id))
        assert event.tenant_id == TEST_TENANT_ID
        # No clinic_tenant_id provided → subject equals record owner.
        assert event.event_data["subject_tenant_id"] == str(event.tenant_id)

    def test_non_uuid_clinic_tenant_id_falls_back_and_is_not_persisted(
        self, client: TestClient, api_key_headers
    ) -> None:
        payload = _rating_payload(4)
        # clinic_admin path sends the ClinicConfigTable business slug here,
        # which is NOT a UUID that resolves to a Tenant row.
        payload["clinic_tenant_id"] = "acme-clinic"

        response = client.post(
            "/v1/rlhf/rating",
            headers=api_key_headers,
            json=payload,
        )
        assert response.status_code == 200
        event_id = UUID(response.json()["event_id"])

        event = client._submit(_load_event(event_id))
        assert event.tenant_id == TEST_TENANT_ID
        # Fallback to rater's tenant …
        assert event.event_data["subject_tenant_id"] == str(event.tenant_id)
        # … and the raw slug is not persisted anywhere in the event body.
        assert "acme-clinic" not in str(event.event_data.values())

    def test_valid_uuid_non_existing_tenant_falls_back(
        self, client: TestClient, api_key_headers
    ) -> None:
        # Syntactically valid UUID, but nothing in Tenant matches it.
        orphan_id = uuid4()

        payload = _rating_payload(4)
        payload["clinic_tenant_id"] = str(orphan_id)

        response = client.post(
            "/v1/rlhf/rating",
            headers=api_key_headers,
            json=payload,
        )
        assert response.status_code == 200
        event_id = UUID(response.json()["event_id"])

        event = client._submit(_load_event(event_id))
        assert event.tenant_id == TEST_TENANT_ID
        # Existence check must reject dangling UUIDs and fall back.
        assert event.event_data["subject_tenant_id"] == str(event.tenant_id)
        assert event.event_data["subject_tenant_id"] != str(orphan_id)


# ============================================
# GET /v1/rlhf/ratings
# ============================================


class TestListRatings:
    def test_returns_own_tenant_ratings(self, client: TestClient, api_key_headers):
        # Create one rating first
        client.post(
            "/v1/rlhf/rating",
            headers=api_key_headers,
            json=_rating_payload(4),
        )
        response = client.get("/v1/rlhf/ratings", headers=api_key_headers)
        assert response.status_code == 200
        body = response.json()
        entries = body["entries"]
        assert isinstance(entries, list)
        # All entries belong to the caller's tenant
        assert all(e["tenant_id"] == str(TEST_TENANT_ID) for e in entries)

    def test_non_super_admin_ignores_builder_id_filter(
        self, client: TestClient, api_key_headers
    ):
        # Seed one entry
        client.post(
            "/v1/rlhf/rating",
            headers=api_key_headers,
            json=_rating_payload(5),
        )
        # Attempt to filter by a foreign builder — should be ignored silently
        response = client.get(
            f"/v1/rlhf/ratings?builder_id={uuid4()}",
            headers=api_key_headers,
        )
        assert response.status_code == 200
        entries = response.json()["entries"]
        # All returned entries must still belong to caller's own tenant
        assert all(e["tenant_id"] == str(TEST_TENANT_ID) for e in entries)

    def test_super_admin_can_filter_by_tenant_id(
        self, client: TestClient, super_admin_headers
    ):
        response = client.get(
            f"/v1/rlhf/ratings?tenant_id={TEST_TENANT_ID}",
            headers=super_admin_headers,
        )
        assert response.status_code == 200
        body = response.json()
        entries = body["entries"]
        assert isinstance(entries, list)
        # Group 4: `tenant_id` filter now matches subject_tenant_id, not
        # Event.tenant_id. Rows submitted without an explicit clinic_tenant_id
        # fall back to Event.tenant_id, and TEST_TENANT_ID is the api-key
        # tenant most fixtures use — so the record-owner check remains a
        # meaningful assertion for the shape returned here.
        assert all(e["subject_tenant_id"] == str(TEST_TENANT_ID) for e in entries)


# ============================================
# PATCH /v1/rlhf/ratings/{event_id}
# ============================================


class TestPatchRating:
    def test_super_admin_can_approve(
        self, client: TestClient, api_key_headers, super_admin_headers
    ):
        # Seed a rating with a qualifying reason so the Group 5 has_reason
        # guard on the accept action lets the PATCH through — we're covering
        # the happy path here, not the guard (that has its own test below).
        event_id = uuid4()
        client._submit(
            _seed_rating_event(
                event_id=event_id,
                tenant_id=TEST_TENANT_ID,
                rating=3,
                reason="motivo suficientemente longo",
                subject_tenant_id=TEST_TENANT_ID,
            )
        )
        # Ensure the tenant exists so serialization/filtering works.
        client._submit(_seed_tenant_full(TEST_TENANT_ID, "Approve Tenant"))

        response = client.patch(
            f"/v1/rlhf/ratings/{event_id}",
            headers=super_admin_headers,
            json={"action": "approve"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["action"] == "approve"

        # Confirm approved_for_training flag reached the event
        list_resp = client.get(
            f"/v1/rlhf/ratings?tenant_id={TEST_TENANT_ID}",
            headers=super_admin_headers,
        )
        matched = [e for e in list_resp.json()["entries"] if e["id"] == str(event_id)]
        assert matched
        assert matched[0]["approved_for_training"] is True

    def test_non_super_admin_receives_403(
        self, client: TestClient, api_key_headers, clinic_admin_headers
    ):
        post = client.post(
            "/v1/rlhf/rating",
            headers=api_key_headers,
            json=_rating_payload(2, "Motivo"),
        )
        event_id = post.json()["event_id"]

        response = client.patch(
            f"/v1/rlhf/ratings/{event_id}",
            headers=clinic_admin_headers,
            json={"action": "approve"},
        )
        assert response.status_code == 403


class TestPatchRatingAcceptAudit:
    """Group 5: `PATCH ... action=approve` stamps `accepted_by` / `accepted_at`,
    refuses entries without a qualifying reason, and leaves `ignore` unchanged.

    All coverage goes through the HTTP surface — no direct handler-function
    calls — so the LifespanTestClient/asyncio deadlock warned about in the
    wish's Group 3 fix section does not apply here.
    """

    def _super_admin_headers_for(self, user_id: UUID) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {_make_jwt('super_admin', user_id=user_id)}",
            "Content-Type": "application/json",
        }

    def test_approve_stamps_accepted_by_and_accepted_at(
        self, client: TestClient, super_admin_headers
    ) -> None:
        """AC: `PATCH ... action=approve` persists `accepted_by` and
        `accepted_at` in addition to `approved_for_training`."""
        # Extract the same user_id from the fixture's JWT so the stamp value
        # matches what we can assert.
        import jwt as _jwt

        token = super_admin_headers["Authorization"].removeprefix("Bearer ")
        acting_user_id = UUID(
            _jwt.decode(token, settings.secret_key, algorithms=["HS256"])["sub"]
        )
        subject_id = uuid4()
        event_id = uuid4()
        client._submit(_seed_tenant_full(subject_id, "Audit Clinic"))
        client._submit(
            _seed_rating_event(
                event_id=event_id,
                tenant_id=TEST_TENANT_ID,
                rating=2,
                reason="motivo escrito válido",
                subject_tenant_id=subject_id,
            )
        )

        before_iso = datetime.now(timezone.utc).isoformat()
        response = client.patch(
            f"/v1/rlhf/ratings/{event_id}",
            headers=super_admin_headers,
            json={"action": "approve"},
        )
        assert response.status_code == 200

        # Read back via GET to prove the stamp landed on event_data through
        # the serializer contract (not via ORM introspection).
        list_resp = client.get(
            f"/v1/rlhf/ratings?tenant_id={subject_id}",
            headers=super_admin_headers,
        )
        matched = [e for e in list_resp.json()["entries"] if e["id"] == str(event_id)]
        assert matched
        entry = matched[0]
        assert entry["approved_for_training"] is True
        assert entry["accepted_by"] == str(acting_user_id)
        assert entry["accepted_at"] is not None
        # The stamp is ISO-8601 UTC and lands AFTER we snapshotted `before_iso`.
        assert entry["accepted_at"] >= before_iso

    def test_approve_rejects_entry_without_qualifying_reason(
        self, client: TestClient, super_admin_headers
    ) -> None:
        """AC: `PATCH ... action=approve` on an entry with no qualifying
        reason (or a reason under 8 chars) returns 422."""
        subject_id = uuid4()
        client._submit(_seed_tenant_full(subject_id, "No-Reason Clinic"))
        # 7-char reason — just under the floor, proves the boundary.
        short_id = uuid4()
        client._submit(
            _seed_rating_event(
                event_id=short_id,
                tenant_id=TEST_TENANT_ID,
                rating=3,
                reason="abcdefg",
                subject_tenant_id=subject_id,
            )
        )
        # Whitespace-only reason (strip drops it below the floor).
        blank_id = uuid4()
        client._submit(
            _seed_rating_event(
                event_id=blank_id,
                tenant_id=TEST_TENANT_ID,
                rating=3,
                reason="   ",
                subject_tenant_id=subject_id,
            )
        )
        # Missing reason altogether.
        none_id = uuid4()
        client._submit(
            _seed_rating_event(
                event_id=none_id,
                tenant_id=TEST_TENANT_ID,
                rating=3,
                reason=None,
                subject_tenant_id=subject_id,
            )
        )

        for eid in (short_id, blank_id, none_id):
            resp = client.patch(
                f"/v1/rlhf/ratings/{eid}",
                headers=super_admin_headers,
                json={"action": "approve"},
            )
            assert (
                resp.status_code == 422
            ), f"expected 422 for {eid}, got {resp.status_code}: {resp.text}"

        # Confirm none of them were flipped to approved despite the failed PATCH.
        list_resp = client.get(
            f"/v1/rlhf/ratings?tenant_id={subject_id}",
            headers=super_admin_headers,
        )
        by_id = {e["id"]: e for e in list_resp.json()["entries"]}
        for eid in (short_id, blank_id, none_id):
            assert by_id[str(eid)]["approved_for_training"] is False
            assert by_id[str(eid)]["accepted_by"] is None

    def test_ignore_action_unchanged_no_stamp_no_guard(
        self, client: TestClient, super_admin_headers
    ) -> None:
        """AC (deliverable 6): the `ignore` action does not stamp or guard —
        entries without a reason still ignore successfully.
        """
        subject_id = uuid4()
        client._submit(_seed_tenant_full(subject_id, "Ignore Clinic"))
        event_id = uuid4()
        client._submit(
            _seed_rating_event(
                event_id=event_id,
                tenant_id=TEST_TENANT_ID,
                rating=1,
                reason=None,  # No reason, would 422 under `approve`
                subject_tenant_id=subject_id,
            )
        )
        resp = client.patch(
            f"/v1/rlhf/ratings/{event_id}",
            headers=super_admin_headers,
            json={"action": "ignore"},
        )
        assert resp.status_code == 200

        list_resp = client.get(
            f"/v1/rlhf/ratings?tenant_id={subject_id}",
            headers=super_admin_headers,
        )
        matched = [e for e in list_resp.json()["entries"] if e["id"] == str(event_id)]
        assert matched
        entry = matched[0]
        assert entry["ignored"] is True
        assert entry["accepted_by"] is None
        assert entry["accepted_at"] is None


class TestListRatingsAcceptedByEmail:
    """Group 5: GET response carries `accepted_by`, `accepted_at` and
    `accepted_by_email`; email resolution is one batched query."""

    def test_accepted_entries_return_email_via_batched_lookup(
        self, client: TestClient, super_admin_headers
    ) -> None:
        """AC: `GET /v1/rlhf/ratings` returns `accepted_by`, `accepted_at`
        and `accepted_by_email` on accepted entries.
        """
        subject_id = uuid4()
        acting_user_id = uuid4()
        client._submit(_seed_tenant_full(subject_id, "Email Clinic"))
        client._submit(_seed_user(acting_user_id, "auditor@example.com", subject_id))
        event_id = uuid4()
        stamp_iso = datetime.now(timezone.utc).isoformat()
        client._submit(
            _seed_rating_event(
                event_id=event_id,
                tenant_id=TEST_TENANT_ID,
                rating=3,
                reason="crítica escrita",
                subject_tenant_id=subject_id,
                approved_for_training=True,
                accepted_by=acting_user_id,
                accepted_at=stamp_iso,
            )
        )
        response = client.get(
            f"/v1/rlhf/ratings?tenant_id={subject_id}",
            headers=super_admin_headers,
        )
        assert response.status_code == 200
        matched = [e for e in response.json()["entries"] if e["id"] == str(event_id)]
        assert matched
        entry = matched[0]
        assert entry["accepted_by"] == str(acting_user_id)
        assert entry["accepted_at"] == stamp_iso
        assert entry["accepted_by_email"] == "auditor@example.com"

    def test_legacy_approved_row_without_stamp_shows_none_authorship(
        self, client: TestClient, super_admin_headers
    ) -> None:
        """AC: entries approved before this change remain accepted and
        surface `accepted_by`/`accepted_by_email` as `None` — the panel
        renders that as "—" with no migration.
        """
        subject_id = uuid4()
        client._submit(_seed_tenant_full(subject_id, "Legacy Clinic"))
        event_id = uuid4()
        client._submit(
            _seed_rating_event(
                event_id=event_id,
                tenant_id=TEST_TENANT_ID,
                rating=2,
                reason="crítica escrita",
                subject_tenant_id=subject_id,
                approved_for_training=True,
                # No accepted_by / accepted_at — this is a pre-Group-5 row.
            )
        )
        response = client.get(
            f"/v1/rlhf/ratings?tenant_id={subject_id}",
            headers=super_admin_headers,
        )
        assert response.status_code == 200
        matched = [e for e in response.json()["entries"] if e["id"] == str(event_id)]
        assert matched
        entry = matched[0]
        assert entry["approved_for_training"] is True
        assert entry["accepted_by"] is None
        assert entry["accepted_at"] is None
        assert entry["accepted_by_email"] is None

    def test_email_resolution_is_batched_not_per_row(
        self, client: TestClient, super_admin_headers
    ) -> None:
        """AC: email resolution is ONE batched query, never one per entry.

        Seed 20 accepted entries each authored by a different user (worst
        case for N+1), then count SQL execute calls during the handler. A
        batched `.in_(...)` lookup adds exactly one query beyond the tenant
        + builder batches; a per-row implementation would add 20.
        """
        from sqlalchemy import event as sa_event

        subject_id = uuid4()
        client._submit(_seed_tenant_full(subject_id, "Batch Email Clinic"))
        for i in range(20):
            user_id = uuid4()
            client._submit(_seed_user(user_id, f"user{i}@example.com", subject_id))
            client._submit(
                _seed_rating_event(
                    event_id=uuid4(),
                    tenant_id=TEST_TENANT_ID,
                    rating=3,
                    reason="crítica escrita",
                    subject_tenant_id=subject_id,
                    approved_for_training=True,
                    accepted_by=user_id,
                    accepted_at=datetime.now(timezone.utc).isoformat(),
                )
            )

        from src.repositories import get_db_engine

        query_count = {"n": 0}

        def _on_execute(conn, clauseelement, multiparams, params, execution_options):
            query_count["n"] += 1

        engine = get_db_engine().sync_engine
        sa_event.listen(engine, "before_execute", _on_execute)
        try:
            response = client.get(
                f"/v1/rlhf/ratings?tenant_id={subject_id}&limit=500",
                headers=super_admin_headers,
            )
        finally:
            sa_event.remove(engine, "before_execute", _on_execute)

        assert response.status_code == 200
        # Main SELECT + Tenant batch + (no builder batch — no builder set) +
        # 1 User batch = 3 core queries. Allow small connection-setup headroom.
        # A per-row regression would push this to ~23.
        assert (
            query_count["n"] <= 6
        ), f"expected batched user lookup, got {query_count['n']} queries"

        # And prove all 20 emails actually resolved, so we're counting a
        # real single-query batch — not a silently empty one.
        entries = response.json()["entries"]
        resolved = [e for e in entries if e["accepted_by_email"] is not None]
        assert len(resolved) == 20


class TestListRatingsAcceptedCount:
    """Group 5: `summary.accepted_count` counts only entries that are BOTH
    approved AND satisfy `_has_reason` — excluding legacy no-reason approvals.
    """

    def test_accepted_count_excludes_legacy_no_reason_approvals(
        self, client: TestClient, super_admin_headers
    ) -> None:
        """AC: for a cut with N entries and `limit >= N`,
        `summary.accepted_count` equals exactly the number returned by
        `only_with_reason=true&only_accepted=true`; both exclude legacy
        no-reason approvals.
        """
        subject_id = uuid4()
        client._submit(_seed_tenant_full(subject_id, "Count Clinic"))

        # 3 approved WITH reason — should be counted.
        for _ in range(3):
            client._submit(
                _seed_rating_event(
                    event_id=uuid4(),
                    tenant_id=TEST_TENANT_ID,
                    rating=3,
                    reason="crítica escrita",
                    subject_tenant_id=subject_id,
                    approved_for_training=True,
                )
            )
        # 2 legacy approved WITHOUT reason — must NOT be counted.
        for _ in range(2):
            client._submit(
                _seed_rating_event(
                    event_id=uuid4(),
                    tenant_id=TEST_TENANT_ID,
                    rating=4,
                    reason=None,
                    subject_tenant_id=subject_id,
                    approved_for_training=True,
                )
            )
        # 1 approved with a 7-char reason (below floor) — must NOT be counted.
        client._submit(
            _seed_rating_event(
                event_id=uuid4(),
                tenant_id=TEST_TENANT_ID,
                rating=2,
                reason="abcdefg",
                subject_tenant_id=subject_id,
                approved_for_training=True,
            )
        )
        # 1 unapproved with a long reason — irrelevant to the count.
        client._submit(
            _seed_rating_event(
                event_id=uuid4(),
                tenant_id=TEST_TENANT_ID,
                rating=5,
                reason="motivo longo mas não aprovado",
                subject_tenant_id=subject_id,
                approved_for_training=False,
            )
        )

        base = client.get(
            f"/v1/rlhf/ratings?tenant_id={subject_id}&limit=500",
            headers=super_admin_headers,
        ).json()
        assert base["summary"]["accepted_count"] == 3

        filtered = client.get(
            f"/v1/rlhf/ratings?tenant_id={subject_id}"
            "&only_with_reason=true&only_accepted=true&limit=500",
            headers=super_admin_headers,
        ).json()
        # The archive-view count MUST equal the summary count.
        assert len(filtered["entries"]) == base["summary"]["accepted_count"]


# ============================================
class TestRatingAnalysisRuntimeCredentials:
    """Rating analysis uses the DB-only runtime resolver and never logs secret prefixes."""

    @pytest.mark.asyncio
    async def test_analyze_uses_db_only_loader_and_no_secret_logged(
        self, caplog
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from unittest.mock import patch as ptch

        from src.api.routes import rlhf

        raw_key = "SECRET-LLM-KEY-XYZ"

        async def _fake_runtime_creds(_tenant_id):
            return {
                "llm_provider": "openai",
                "llm_api_url": "https://api.openai.com/v1",
                "llm_api_key": raw_key,
                "llm_model_name": "gpt-model",
            }

        fake_llm = MagicMock()
        fake_llm.generate = AsyncMock(
            return_value={
                "content": '{"status": "agreed", "justification": "ok"}',
                "usage": {},
                "model": "gpt-model",
            }
        )

        with (
            ptch(
                "src.services.event_service._load_runtime_llm_credentials",
                new=_fake_runtime_creds,
            ),
            ptch(
                "src.api.routes.rlhf.get_tenant_llm_client",
                return_value=fake_llm,
            ),
            ptch(
                "src.api.routes.rlhf._update_rating_status",
                new=AsyncMock(return_value=None),
            ),
        ):
            with caplog.at_level("INFO"):
                await rlhf._analyze_rating_async(
                    event_id=uuid4(),
                    rating=1,
                    reason="motivo",
                    agent_response="ok",
                    user_message="por que?",
                    tenant_id=uuid4(),
                )

        # The raw key MUST NOT appear in any log record — no prefix, no substring.
        for rec in caplog.records:
            text = rec.getMessage()
            assert raw_key not in text
            # Also confirm no 8-char prefix leaked (the previous impl exposed 8 chars).
            assert raw_key[:8] not in text

        # LLM was invoked with the DB-only resolved credentials.
        assert fake_llm.generate.await_count == 1

    @pytest.mark.asyncio
    async def test_analyze_marks_failed_when_no_credentials(self) -> None:
        from unittest.mock import AsyncMock
        from unittest.mock import patch as ptch

        from src.api.routes import rlhf

        async def _empty_creds(_tenant_id):
            return {}

        update_status = AsyncMock(return_value=None)
        with (
            ptch(
                "src.services.event_service._load_runtime_llm_credentials",
                new=_empty_creds,
            ),
            ptch(
                "src.api.routes.rlhf.get_tenant_llm_client",
                return_value=None,
            ),
            ptch(
                "src.api.routes.rlhf._update_rating_status",
                new=update_status,
            ),
        ):
            await rlhf._analyze_rating_async(
                event_id=uuid4(),
                rating=2,
                reason="motivo",
                agent_response="ok",
                user_message="por que?",
                tenant_id=uuid4(),
            )
        # When no LLM client resolvable, status flips to failed — no crash and
        # no secret ever handled.
        assert update_status.await_count == 1
        args, kwargs = update_status.await_args
        assert kwargs.get("status_") == "failed" or (
            len(args) >= 2 and args[1] == "failed"
        )


class TestRatingAnalysisClinicTenantIdValidation:
    """`_analyze_rating_async` must validate `clinic_tenant_id` against the
    Tenant table (matching submit_rating's hardening) before trusting it to
    pick which tenant's LLM credentials are loaded. A syntactically valid
    UUID that has no Tenant row must fall through to the message_id lookup
    (or the submitter tenant), never silently select attacker-supplied
    credentials.
    """

    @pytest.fixture
    async def db_ready(self):
        """Initialize the DB directly on this test's own event loop.

        Deliberately avoids the `client` fixture here: `LifespanTestClient`
        runs its lifespan on a dedicated background thread/loop, and this
        class awaits `_analyze_rating_async` (and DB seeding) directly on
        the test's own loop. Routing either through `client._submit` while
        the other runs on the test's loop mixes two live event loops over
        the same StaticPool sqlite connection and deadlocks.
        """
        from src.repositories.database import (
            close_database,
            get_db_engine,
            init_database,
        )

        created = False
        try:
            get_db_engine()
        except RuntimeError:
            await init_database()
            created = True
        yield
        if created:
            await close_database()

    @staticmethod
    def _patches(fake_llm, seen: dict):
        from unittest.mock import AsyncMock
        from unittest.mock import patch as ptch

        async def _fake_runtime_creds(tenant_id):
            seen["resolve_tenant_id"] = tenant_id
            return {
                "llm_provider": "openai",
                "llm_api_url": "https://api.openai.com/v1",
                "llm_api_key": "SECRET",
                "llm_model_name": "gpt-model",
            }

        return (
            ptch(
                "src.services.event_service._load_runtime_llm_credentials",
                new=_fake_runtime_creds,
            ),
            ptch(
                "src.api.routes.rlhf.get_tenant_llm_client",
                return_value=fake_llm,
            ),
            ptch(
                "src.api.routes.rlhf._update_rating_status",
                new=AsyncMock(return_value=None),
            ),
        )

    @pytest.mark.asyncio
    async def test_existing_clinic_tenant_id_is_used_for_credentials(
        self, db_ready
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from src.api.routes import rlhf

        clinic_id = uuid4()
        submitter_id = uuid4()
        await _seed_tenant(clinic_id)

        fake_llm = MagicMock()
        fake_llm.generate = AsyncMock(
            return_value={
                "content": '{"status": "agreed", "justification": "ok"}',
                "usage": {},
                "model": "gpt-model",
            }
        )
        seen: dict = {}
        p1, p2, p3 = self._patches(fake_llm, seen)
        with p1, p2, p3:
            await rlhf._analyze_rating_async(
                event_id=uuid4(),
                rating=1,
                reason="motivo",
                agent_response="ok",
                user_message="por que?",
                tenant_id=submitter_id,
                message_id=None,
                clinic_tenant_id=str(clinic_id),
            )

        assert seen["resolve_tenant_id"] == clinic_id

    @pytest.mark.asyncio
    async def test_orphan_clinic_tenant_id_falls_through_to_submitter(
        self, db_ready
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from src.api.routes import rlhf

        orphan_id = uuid4()  # syntactically valid, no Tenant row
        submitter_id = uuid4()

        fake_llm = MagicMock()
        fake_llm.generate = AsyncMock(
            return_value={
                "content": '{"status": "agreed", "justification": "ok"}',
                "usage": {},
                "model": "gpt-model",
            }
        )
        seen: dict = {}
        p1, p2, p3 = self._patches(fake_llm, seen)
        with p1, p2, p3:
            await rlhf._analyze_rating_async(
                event_id=uuid4(),
                rating=1,
                reason="motivo",
                agent_response="ok",
                user_message="por que?",
                tenant_id=submitter_id,
                message_id=None,
                clinic_tenant_id=str(orphan_id),
            )

        # Attacker-supplied UUID must NOT be used to load credentials.
        assert seen["resolve_tenant_id"] != orphan_id
        # No message_id, so we fall all the way back to the submitter's tenant.
        assert seen["resolve_tenant_id"] == submitter_id

    @pytest.mark.asyncio
    async def test_orphan_clinic_tenant_id_falls_through_to_message_id_lookup(
        self, db_ready
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from src.api.routes import rlhf

        orphan_id = uuid4()  # bogus clinic_tenant_id
        submitter_id = uuid4()
        real_clinic_id = uuid4()
        await _seed_tenant(real_clinic_id)

        # Seed an Event owned by real_clinic_id so the message_id branch
        # resolves to it — proving we actually FELL THROUGH to that branch
        # rather than silently staying on the submitter tenant.
        original_event_id = uuid4()

        async def _seed_original() -> None:
            session = get_db_session()
            try:
                session.add(
                    Event(
                        id=original_event_id,
                        tenant_id=real_clinic_id,
                        session_id=None,
                        event_type="chat.response",
                        event_data={},
                        event_metadata={},
                    )
                )
                await session.commit()
            finally:
                await session.close()

        await _seed_original()

        fake_llm = MagicMock()
        fake_llm.generate = AsyncMock(
            return_value={
                "content": '{"status": "agreed", "justification": "ok"}',
                "usage": {},
                "model": "gpt-model",
            }
        )
        seen: dict = {}
        p1, p2, p3 = self._patches(fake_llm, seen)
        with p1, p2, p3:
            await rlhf._analyze_rating_async(
                event_id=uuid4(),
                rating=1,
                reason="motivo",
                agent_response="ok",
                user_message="por que?",
                tenant_id=submitter_id,
                message_id=str(original_event_id),
                clinic_tenant_id=str(orphan_id),
            )

        # Priority order preserved: bogus clinic_tenant_id falls through to
        # the message_id branch, which resolves the real owning tenant.
        assert seen["resolve_tenant_id"] == real_clinic_id

    @pytest.mark.asyncio
    async def test_non_uuid_clinic_tenant_id_falls_through(
        self,
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from src.api.routes import rlhf

        submitter_id = uuid4()

        fake_llm = MagicMock()
        fake_llm.generate = AsyncMock(
            return_value={
                "content": '{"status": "agreed", "justification": "ok"}',
                "usage": {},
                "model": "gpt-model",
            }
        )
        seen: dict = {}
        p1, p2, p3 = self._patches(fake_llm, seen)
        with p1, p2, p3:
            await rlhf._analyze_rating_async(
                event_id=uuid4(),
                rating=1,
                reason="motivo",
                agent_response="ok",
                user_message="por que?",
                tenant_id=submitter_id,
                message_id=None,
                clinic_tenant_id="acme-clinic",  # not a UUID
            )

        # Non-UUID clinic_tenant_id must not affect resolution.
        assert seen["resolve_tenant_id"] == submitter_id


class TestRemovedSuggestionFeedbackRoutes:
    @pytest.mark.parametrize("route", ["validate", "feedback"])
    def test_removed_suggestion_routes_return_404(
        self, client: TestClient, api_key_headers, route: str
    ):
        response = client.post(f"/v1/rlhf/{route}", headers=api_key_headers, json={})
        assert response.status_code == 404


# ============================================
# Group 4: GET /v1/rlhf/ratings — filters, facets, summary
# ============================================


async def _seed_builder(builder_id: UUID, name: str) -> None:
    """Insert a Builder row so subject_tenant_id facets can attach a builder."""
    from src.models.builder import Builder

    session = get_db_session()
    try:
        session.add(Builder(id=builder_id, name=name))
        await session.commit()
    finally:
        await session.close()


async def _seed_tenant_full(
    tenant_id: UUID, name: str, builder_id: UUID | None = None
) -> None:
    """Insert a Tenant row with an optional builder attached."""
    session = get_db_session()
    try:
        session.add(
            Tenant(
                id=tenant_id,
                name=name,
                slug=f"t-{tenant_id.hex}",
                api_key_hash=f"hash-{tenant_id.hex}",
                config={},
                builder_id=builder_id,
            )
        )
        await session.commit()
    finally:
        await session.close()


async def _seed_user(user_id: UUID, email: str, tenant_id: UUID) -> None:
    """Insert a User row so accepted_by_email resolution can find a match.

    The audit trail (Group 5) stores the acting user's UUID on the event and
    the report resolves it to an email via a batched User lookup — this helper
    lets tests populate that lookup deterministically.
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
    """Insert a rlhf.rating Event row directly, bypassing the endpoint.

    This lets us plant events with an explicit created_at (for date-window
    tests) and with or without a subject_tenant_id (for fallback tests) —
    control the endpoint's submit_rating pipeline can't give us.
    """
    session = get_db_session()
    try:
        event_data: dict[str, Any] = {
            "rating": rating,
            "reason": reason,
            "agent_response": "resp",
            "user_message": "msg",
        }
        if subject_tenant_id is not None:
            event_data["subject_tenant_id"] = str(subject_tenant_id)
        if approved_for_training:
            event_data["approved_for_training"] = True
        if accepted_by is not None:
            event_data["accepted_by"] = str(accepted_by)
        if accepted_at is not None:
            event_data["accepted_at"] = accepted_at
        event = Event(
            id=event_id,
            tenant_id=tenant_id,
            session_id=None,
            event_type="rlhf.rating",
            event_data=event_data,
            event_metadata={},
        )
        if created_at is not None:
            event.created_at = created_at
        session.add(event)
        await session.commit()
    finally:
        await session.close()


class TestListRatingsResponseShape:
    """`GET /v1/rlhf/ratings` returns `{ summary, facets, entries }` and its
    server-side aggregation stays honest under pagination.
    """

    def test_response_carries_summary_facets_entries_keys(
        self, client: TestClient, api_key_headers
    ) -> None:
        response = client.get("/v1/rlhf/ratings", headers=api_key_headers)
        assert response.status_code == 200
        body = response.json()
        assert set(body.keys()) == {"summary", "facets", "entries"}
        assert set(body["summary"].keys()) == {
            "total",
            "average",
            "accepted_count",
            "distribution",
        }
        assert set(body["facets"].keys()) == {"builders", "clinics"}

    def test_summary_total_exceeds_entries_when_paginated(
        self, client: TestClient, api_key_headers
    ) -> None:
        # Seed 5 fresh ratings under the api-key tenant, then request page
        # size 2 — summary.total must count all 5 while entries returns 2.
        for _ in range(5):
            client.post(
                "/v1/rlhf/rating",
                headers=api_key_headers,
                json=_rating_payload(4),
            )
        response = client.get(
            "/v1/rlhf/ratings?limit=2",
            headers=api_key_headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["summary"]["total"] >= 5
        assert len(body["entries"]) == 2
        assert body["summary"]["total"] > len(body["entries"])

    def test_summary_average_matches_arithmetic_mean(
        self, client: TestClient, api_key_headers, super_admin_headers
    ) -> None:
        # Seed a private subject tenant so this test is isolated from
        # ratings other tests dumped under TEST_TENANT_ID.
        subject_id = uuid4()
        client._submit(_seed_tenant_full(subject_id, "Avg Clinic"))

        for value in (1, 2, 3, 4, 5):
            payload = _rating_payload(value, reason=None if value >= 4 else "motivo x")
            payload["clinic_tenant_id"] = str(subject_id)
            client.post("/v1/rlhf/rating", headers=api_key_headers, json=payload)

        response = client.get(
            f"/v1/rlhf/ratings?tenant_id={subject_id}",
            headers=super_admin_headers,
        )
        body = response.json()
        assert body["summary"]["total"] == 5
        # Arithmetic mean of 1..5 is 3.0
        assert body["summary"]["average"] == 3.0

    def test_summary_average_is_zero_for_empty_cut(
        self, client: TestClient, super_admin_headers
    ) -> None:
        # A tenant_id nobody has rated under => empty cut, average = 0.0.
        empty_subject = uuid4()
        response = client.get(
            f"/v1/rlhf/ratings?tenant_id={empty_subject}",
            headers=super_admin_headers,
        )
        body = response.json()
        assert body["summary"]["total"] == 0
        assert body["summary"]["average"] == 0.0

    def test_summary_distribution_sums_to_total(
        self, client: TestClient, api_key_headers
    ) -> None:
        client.post(
            "/v1/rlhf/rating",
            headers=api_key_headers,
            json=_rating_payload(2, "motivo x"),
        )
        response = client.get("/v1/rlhf/ratings", headers=api_key_headers)
        body = response.json()
        dist = body["summary"]["distribution"]
        assert sum(dist.values()) == body["summary"]["total"]

    def test_entries_preserve_desc_by_date(
        self, client: TestClient, api_key_headers
    ) -> None:
        for _ in range(3):
            client.post(
                "/v1/rlhf/rating",
                headers=api_key_headers,
                json=_rating_payload(5),
            )
        response = client.get("/v1/rlhf/ratings", headers=api_key_headers)
        entries = response.json()["entries"]
        assert len(entries) >= 3
        timestamps = [e["ts"] for e in entries]
        assert timestamps == sorted(timestamps, reverse=True)


class TestListRatingsDateWindow:
    """Date-window handling must copy `_apply_filters` in trace_service.py:
    end_date at midnight widens to 23:59:59, and both ends pass through
    `_to_utc` (naive datetimes read as BRT / UTC-3).
    """

    def test_rating_created_today_included_when_end_date_is_today(
        self, client: TestClient, super_admin_headers, api_key_headers
    ) -> None:
        subject_id = uuid4()
        client._submit(_seed_tenant_full(subject_id, "Today Clinic"))
        event_id = uuid4()
        # Rating created "now" (UTC-aware).
        now_utc = datetime.now(timezone.utc)
        client._submit(
            _seed_rating_event(
                event_id=event_id,
                tenant_id=TEST_TENANT_ID,
                rating=4,
                subject_tenant_id=subject_id,
                created_at=now_utc,
            )
        )
        # Panel default: end_date = today at midnight BRT.
        today_brt = now_utc.astimezone(timezone(timedelta(hours=-3))).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )
        response = client.get(
            "/v1/rlhf/ratings",
            headers=super_admin_headers,
            params={
                "tenant_id": str(subject_id),
                "end_date": today_brt.isoformat(),
            },
        )
        assert response.status_code == 200
        ids = [e["id"] for e in response.json()["entries"]]
        assert str(event_id) in ids

    def test_rating_at_22h_brt_included_end_date_D_excluded_end_date_D_minus_1(
        self, client: TestClient, super_admin_headers
    ) -> None:
        """A rating created at 22h BRT on day D is returned with end_date=D
        and NOT with end_date=D-1 — proves the BRT->UTC conversion."""
        subject_id = uuid4()
        client._submit(_seed_tenant_full(subject_id, "BRT Clinic"))

        # Day D = 2025-06-15. 22:00 BRT (UTC-3) = 01:00 UTC of D+1 = 2025-06-16 01:00 UTC.
        brt = timezone(timedelta(hours=-3))
        created_at_brt = datetime(2025, 6, 15, 22, 0, 0, tzinfo=brt)
        created_at_utc = created_at_brt.astimezone(timezone.utc)
        event_id = uuid4()
        client._submit(
            _seed_rating_event(
                event_id=event_id,
                tenant_id=TEST_TENANT_ID,
                rating=4,
                subject_tenant_id=subject_id,
                created_at=created_at_utc,
            )
        )

        # Panel sends naive dates (interpreted as BRT).
        response_D = client.get(
            "/v1/rlhf/ratings",
            headers=super_admin_headers,
            params={
                "tenant_id": str(subject_id),
                "start_date": "2025-06-15T00:00:00",
                "end_date": "2025-06-15T00:00:00",  # midnight => widens to 23:59:59 BRT
            },
        )
        assert response_D.status_code == 200
        ids_D = [e["id"] for e in response_D.json()["entries"]]
        assert (
            str(event_id) in ids_D
        ), "22h BRT rating on day D must be visible when end_date=D"

        response_prev = client.get(
            "/v1/rlhf/ratings",
            headers=super_admin_headers,
            params={
                "tenant_id": str(subject_id),
                "start_date": "2025-06-14T00:00:00",
                "end_date": "2025-06-14T00:00:00",
            },
        )
        assert response_prev.status_code == 200
        ids_prev = [e["id"] for e in response_prev.json()["entries"]]
        assert (
            str(event_id) not in ids_prev
        ), "22h BRT rating on day D must NOT appear when end_date=D-1"

    def test_out_of_window_ratings_excluded_from_summary_facets_entries(
        self, client: TestClient, super_admin_headers
    ) -> None:
        subject_id = uuid4()
        client._submit(_seed_tenant_full(subject_id, "Window Clinic"))

        # Seed one rating in-window and one far outside it.
        in_id = uuid4()
        out_id = uuid4()
        client._submit(
            _seed_rating_event(
                event_id=in_id,
                tenant_id=TEST_TENANT_ID,
                rating=5,
                subject_tenant_id=subject_id,
                created_at=datetime(2025, 3, 10, 12, 0, tzinfo=timezone.utc),
            )
        )
        client._submit(
            _seed_rating_event(
                event_id=out_id,
                tenant_id=TEST_TENANT_ID,
                rating=1,
                reason="motivo forte",
                subject_tenant_id=subject_id,
                created_at=datetime(2020, 1, 1, 0, 0, tzinfo=timezone.utc),
            )
        )
        response = client.get(
            "/v1/rlhf/ratings",
            headers=super_admin_headers,
            params={
                "tenant_id": str(subject_id),
                "start_date": "2025-03-01T00:00:00",
                "end_date": "2025-03-31T00:00:00",
            },
        )
        body = response.json()
        ids = [e["id"] for e in body["entries"]]
        assert str(in_id) in ids
        assert str(out_id) not in ids
        # Summary only counts the in-window row.
        assert body["summary"]["total"] == 1
        # Facets are also cut before entries; the subject appears exactly once.
        clinic_ids = [c["id"] for c in body["facets"]["clinics"]]
        assert str(subject_id) in clinic_ids


class TestListRatingsSubjectFiltering:
    """Filters match the SUBJECT of the rating (`subject_tenant_id`), not the
    record owner (`Event.tenant_id`). Rows lacking `subject_tenant_id` fall
    back to `Event.tenant_id` so pre-Group-3 data isn't silently excluded.
    """

    def test_rating_by_builder_on_clinic_agent_returns_under_clinic_and_builder(
        self, client: TestClient, super_admin_headers, api_key_headers
    ) -> None:
        # Setup: builder B has clinic X; a rating owned by the api-key tenant
        # (a stand-in for the builder's HQ tenant) points at clinic X as
        # subject. It must appear under tenant_id=X AND under builder_id=B.
        builder_id = uuid4()
        clinic_x_id = uuid4()
        client._submit(_seed_builder(builder_id, "Builder B"))
        client._submit(
            _seed_tenant_full(clinic_x_id, "Clinic X", builder_id=builder_id)
        )

        event_id = uuid4()
        client._submit(
            _seed_rating_event(
                event_id=event_id,
                tenant_id=TEST_TENANT_ID,  # builder's HQ / record owner
                rating=2,
                reason="Precisou de mais empatia.",
                subject_tenant_id=clinic_x_id,
            )
        )

        # Filter by the subject clinic:
        r_clinic = client.get(
            f"/v1/rlhf/ratings?tenant_id={clinic_x_id}",
            headers=super_admin_headers,
        )
        ids_clinic = [e["id"] for e in r_clinic.json()["entries"]]
        assert str(event_id) in ids_clinic

        # Filter by builder — must also surface the same row.
        r_builder = client.get(
            f"/v1/rlhf/ratings?builder_id={builder_id}",
            headers=super_admin_headers,
        )
        ids_builder = [e["id"] for e in r_builder.json()["entries"]]
        assert str(event_id) in ids_builder

    def test_rating_without_subject_tenant_id_falls_back_to_event_tenant(
        self, client: TestClient, super_admin_headers
    ) -> None:
        # Older rating: Event.tenant_id set, no subject_tenant_id in event_data.
        record_owner = uuid4()
        client._submit(_seed_tenant_full(record_owner, "Legacy Clinic"))

        event_id = uuid4()
        client._submit(
            _seed_rating_event(
                event_id=event_id,
                tenant_id=record_owner,
                rating=3,
                reason=None,
                subject_tenant_id=None,  # pre-Group-3 shape
            )
        )
        # Filter by record_owner as tenant_id (super admin path) — should
        # find the row via fallback and face it as record_owner clinic.
        response = client.get(
            f"/v1/rlhf/ratings?tenant_id={record_owner}",
            headers=super_admin_headers,
        )
        body = response.json()
        ids = [e["id"] for e in body["entries"]]
        assert str(event_id) in ids
        # Faceted under the record-owner clinic, not vanished.
        clinic_ids = [c["id"] for c in body["facets"]["clinics"]]
        assert str(record_owner) in clinic_ids


class TestListRatingsOnlyFilters:
    """`only_accepted` and `only_with_reason` filter entries without moving
    the summary or facets — those describe the population being audited.
    """

    def test_only_accepted_returns_only_approved_entries(
        self, client: TestClient, super_admin_headers, api_key_headers
    ) -> None:
        subject_id = uuid4()
        client._submit(_seed_tenant_full(subject_id, "Acc Clinic"))

        approved_id = uuid4()
        not_approved_id = uuid4()
        client._submit(
            _seed_rating_event(
                event_id=approved_id,
                tenant_id=TEST_TENANT_ID,
                rating=4,
                reason="motivo forte",
                subject_tenant_id=subject_id,
                approved_for_training=True,
            )
        )
        client._submit(
            _seed_rating_event(
                event_id=not_approved_id,
                tenant_id=TEST_TENANT_ID,
                rating=5,
                subject_tenant_id=subject_id,
                approved_for_training=False,
            )
        )

        response = client.get(
            "/v1/rlhf/ratings",
            headers=super_admin_headers,
            params={"tenant_id": str(subject_id), "only_accepted": "true"},
        )
        body = response.json()
        entries = body["entries"]
        assert all(e["approved_for_training"] for e in entries)
        ids = [e["id"] for e in entries]
        assert str(approved_id) in ids
        assert str(not_approved_id) not in ids

    def test_only_with_reason_excludes_short_reasons(
        self, client: TestClient, super_admin_headers
    ) -> None:
        subject_id = uuid4()
        client._submit(_seed_tenant_full(subject_id, "Reason Clinic"))
        seven_id = uuid4()
        eight_id = uuid4()
        client._submit(
            _seed_rating_event(
                event_id=seven_id,
                tenant_id=TEST_TENANT_ID,
                rating=3,
                reason="abcdefg",  # 7 chars
                subject_tenant_id=subject_id,
            )
        )
        client._submit(
            _seed_rating_event(
                event_id=eight_id,
                tenant_id=TEST_TENANT_ID,
                rating=3,
                reason="abcdefgh",  # 8 chars
                subject_tenant_id=subject_id,
            )
        )
        response = client.get(
            "/v1/rlhf/ratings",
            headers=super_admin_headers,
            params={"tenant_id": str(subject_id), "only_with_reason": "true"},
        )
        body = response.json()
        ids = [e["id"] for e in body["entries"]]
        assert str(eight_id) in ids
        assert str(seven_id) not in ids

    def test_only_with_reason_pagination_returns_full_page(
        self, client: TestClient, super_admin_headers
    ) -> None:
        """With `only_with_reason=true`, `limit=10` and more than 10 qualifying
        entries in the cut, the first page returns 10 — pagination happens
        AFTER the in-memory filter, so no silent short pages.
        """
        subject_id = uuid4()
        client._submit(_seed_tenant_full(subject_id, "Page Clinic"))
        # 12 qualifying + 3 disqualifying (short reason).
        for i in range(12):
            client._submit(
                _seed_rating_event(
                    event_id=uuid4(),
                    tenant_id=TEST_TENANT_ID,
                    rating=3,
                    reason=f"crítica número {i:02d}",
                    subject_tenant_id=subject_id,
                )
            )
        for _ in range(3):
            client._submit(
                _seed_rating_event(
                    event_id=uuid4(),
                    tenant_id=TEST_TENANT_ID,
                    rating=3,
                    reason="abc",  # 3 chars — below floor
                    subject_tenant_id=subject_id,
                )
            )
        response = client.get(
            "/v1/rlhf/ratings",
            headers=super_admin_headers,
            params={
                "tenant_id": str(subject_id),
                "only_with_reason": "true",
                "limit": 10,
            },
        )
        body = response.json()
        assert len(body["entries"]) == 10

    def test_only_accepted_pagination_returns_full_page(
        self, client: TestClient, super_admin_headers
    ) -> None:
        subject_id = uuid4()
        client._submit(_seed_tenant_full(subject_id, "Acc Page"))
        for _ in range(12):
            client._submit(
                _seed_rating_event(
                    event_id=uuid4(),
                    tenant_id=TEST_TENANT_ID,
                    rating=4,
                    subject_tenant_id=subject_id,
                    approved_for_training=True,
                )
            )
        for _ in range(3):
            client._submit(
                _seed_rating_event(
                    event_id=uuid4(),
                    tenant_id=TEST_TENANT_ID,
                    rating=4,
                    subject_tenant_id=subject_id,
                    approved_for_training=False,
                )
            )
        response = client.get(
            "/v1/rlhf/ratings",
            headers=super_admin_headers,
            params={
                "tenant_id": str(subject_id),
                "only_accepted": "true",
                "limit": 10,
            },
        )
        body = response.json()
        assert len(body["entries"]) == 10

    def test_summary_unchanged_by_only_filters(
        self, client: TestClient, super_admin_headers
    ) -> None:
        subject_id = uuid4()
        client._submit(_seed_tenant_full(subject_id, "Summary Stable"))
        # Mix: some approved, some with reason, some neither.
        client._submit(
            _seed_rating_event(
                event_id=uuid4(),
                tenant_id=TEST_TENANT_ID,
                rating=1,
                reason="motivo longo",
                subject_tenant_id=subject_id,
                approved_for_training=True,
            )
        )
        client._submit(
            _seed_rating_event(
                event_id=uuid4(),
                tenant_id=TEST_TENANT_ID,
                rating=3,
                reason="curto",
                subject_tenant_id=subject_id,
            )
        )
        client._submit(
            _seed_rating_event(
                event_id=uuid4(),
                tenant_id=TEST_TENANT_ID,
                rating=5,
                subject_tenant_id=subject_id,
            )
        )
        base = client.get(
            f"/v1/rlhf/ratings?tenant_id={subject_id}",
            headers=super_admin_headers,
        ).json()["summary"]
        accepted = client.get(
            f"/v1/rlhf/ratings?tenant_id={subject_id}&only_accepted=true",
            headers=super_admin_headers,
        ).json()["summary"]
        reason = client.get(
            f"/v1/rlhf/ratings?tenant_id={subject_id}&only_with_reason=true",
            headers=super_admin_headers,
        ).json()["summary"]
        both = client.get(
            "/v1/rlhf/ratings"
            f"?tenant_id={subject_id}&only_accepted=true&only_with_reason=true",
            headers=super_admin_headers,
        ).json()["summary"]
        assert base == accepted == reason == both


class TestListRatingsFacets:
    """`facets` is computed BEFORE builder/clinic narrowing so selecting a
    builder cannot shrink its own dropdown. Each `facets.clinics` item
    carries `builder_id` so the panel can narrow client-side.
    """

    def test_facets_do_not_shrink_when_builder_selected(
        self, client: TestClient, super_admin_headers
    ) -> None:
        builder_id = uuid4()
        clinic_a = uuid4()
        clinic_b = uuid4()
        client._submit(_seed_builder(builder_id, "Builder Alpha"))
        client._submit(_seed_tenant_full(clinic_a, "A", builder_id=builder_id))
        client._submit(_seed_tenant_full(clinic_b, "B", builder_id=builder_id))
        # One rating on each subject clinic — same builder.
        for sub in (clinic_a, clinic_b):
            client._submit(
                _seed_rating_event(
                    event_id=uuid4(),
                    tenant_id=TEST_TENANT_ID,
                    rating=4,
                    subject_tenant_id=sub,
                )
            )

        # Unfiltered call: facets contain both clinics.
        base = client.get("/v1/rlhf/ratings", headers=super_admin_headers).json()[
            "facets"
        ]
        base_clinic_ids = {c["id"] for c in base["clinics"]}
        assert {str(clinic_a), str(clinic_b)} <= base_clinic_ids

        # Filter by builder — facets must still list BOTH clinics (facets are
        # pre-narrowing) so the dropdown stays navigable.
        with_builder = client.get(
            f"/v1/rlhf/ratings?builder_id={builder_id}",
            headers=super_admin_headers,
        ).json()["facets"]
        with_clinic_ids = {c["id"] for c in with_builder["clinics"]}
        assert {str(clinic_a), str(clinic_b)} <= with_clinic_ids

    def test_facets_clinics_include_builder_id_or_null(
        self, client: TestClient, super_admin_headers
    ) -> None:
        builder_id = uuid4()
        with_builder = uuid4()
        without_builder = uuid4()
        client._submit(_seed_builder(builder_id, "B1"))
        client._submit(_seed_tenant_full(with_builder, "WB", builder_id=builder_id))
        client._submit(_seed_tenant_full(without_builder, "NB"))
        for sub in (with_builder, without_builder):
            client._submit(
                _seed_rating_event(
                    event_id=uuid4(),
                    tenant_id=TEST_TENANT_ID,
                    rating=5,
                    subject_tenant_id=sub,
                )
            )
        response = client.get("/v1/rlhf/ratings", headers=super_admin_headers)
        clinics = response.json()["facets"]["clinics"]
        by_id = {c["id"]: c for c in clinics}
        assert by_id[str(with_builder)]["builder_id"] == str(builder_id)
        assert by_id[str(without_builder)]["builder_id"] is None


class TestListRatingsTenantIsolation:
    """Called by a non-super-admin, the SQL filter isolates by
    `Event.tenant_id` — no filter parameter can leak rows the caller doesn't
    own. `facets` may still list several subject clinics from those rows.
    """

    def test_non_super_admin_never_sees_foreign_rows_even_with_filters(
        self, client: TestClient, api_key_headers, clinic_admin_headers
    ) -> None:
        # Seed a foreign event (different owner) with a subject the caller
        # might try to filter to.
        foreign_owner = uuid4()
        subject_id = uuid4()
        client._submit(_seed_tenant_full(foreign_owner, "Foreign"))
        client._submit(_seed_tenant_full(subject_id, "Foreign Subject"))
        foreign_event = uuid4()
        client._submit(
            _seed_rating_event(
                event_id=foreign_event,
                tenant_id=foreign_owner,
                rating=1,
                reason="segredo",
                subject_tenant_id=subject_id,
            )
        )
        # clinic_admin (TEST_TENANT_ID) attempts to filter by the foreign
        # subject. The SQL clause forces Event.tenant_id == TEST_TENANT_ID,
        # so the foreign row cannot appear.
        response = client.get(
            f"/v1/rlhf/ratings?tenant_id={subject_id}",
            headers=clinic_admin_headers,
        )
        assert response.status_code == 200
        entries = response.json()["entries"]
        assert all(e["tenant_id"] == str(TEST_TENANT_ID) for e in entries)
        assert str(foreign_event) not in [e["id"] for e in entries]


class TestListRatingsQueryCount:
    """A request with 50 entries across 10 distinct clinics triggers no more
    than 3 queries beyond the main one (batched tenant + batched builder).
    """

    def test_batched_tenant_and_builder_lookups(
        self, client: TestClient, super_admin_headers
    ) -> None:
        from sqlalchemy import event as sa_event

        # 10 clinics, each with its own builder (worst-case for batching).
        clinic_ids = []
        for i in range(10):
            builder_id = uuid4()
            clinic_id = uuid4()
            client._submit(_seed_builder(builder_id, f"B{i}"))
            client._submit(_seed_tenant_full(clinic_id, f"C{i}", builder_id=builder_id))
            clinic_ids.append(clinic_id)
        # 50 events distributed round-robin across the 10 clinics.
        for i in range(50):
            client._submit(
                _seed_rating_event(
                    event_id=uuid4(),
                    tenant_id=TEST_TENANT_ID,
                    rating=(i % 5) + 1,
                    subject_tenant_id=clinic_ids[i % 10],
                )
            )

        # Instrument SQL query count during the handler run.
        from src.repositories import get_db_engine

        query_count = {"n": 0}

        def _on_execute(conn, clauseelement, multiparams, params, execution_options):
            query_count["n"] += 1

        engine = get_db_engine().sync_engine
        sa_event.listen(engine, "before_execute", _on_execute)
        try:
            response = client.get(
                "/v1/rlhf/ratings?limit=500", headers=super_admin_headers
            )
        finally:
            sa_event.remove(engine, "before_execute", _on_execute)

        assert response.status_code == 200
        # Main SELECT + batched Tenant lookup + batched Builder lookup = 3.
        # Anything more indicates an N+1 regression per row/subject.
        # Allow a small headroom (up to 4) for connection setup queries.
        assert (
            query_count["n"] <= 5
        ), f"expected batched queries (main + tenants + builders), got {query_count['n']}"


# ============================================
# Loader isolation — RLHF must never see the multimodal bundle
# ============================================
#
# The RLHF consumer imports `_load_runtime_llm_credentials` directly (see
# `src/api/routes/rlhf.py`), and Group 1 adds a SEPARATE private loader for
# the multimodal-text fallback. This test proves the split: the RLHF loader
# still returns exactly the four LLM fields even when the DB row is loaded
# with a complete multimodal bundle AND the opt-in gate set. If it ever
# started leaking multimodal fields or the gate, the fallback capability
# would silently reach the RLHF LLM call path.


class TestRlhfLoaderNeverSeesMultimodalBundle:
    @pytest.mark.asyncio
    async def test_rlhf_loader_returns_exactly_four_llm_fields_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.services import event_service

        async def _fake_load(_tenant_id: UUID) -> dict[str, Any]:
            return {
                "identity": {},
                # Full multimodal bundle + opt-in gate present alongside the
                # LLM credentials. The RLHF loader MUST drop every one of
                # them and return only the four llm_* fields.
                "api_credentials": {
                    "llm_provider": "openai",
                    "llm_api_url": "https://api.openai.com/v1",
                    "llm_api_key": "RLHF-LLM-KEY",
                    "llm_model_name": "gpt-model",
                    "multimodal_provider": "openrouter",
                    "multimodal_api_url": "https://openrouter.ai/api/v1",
                    "multimodal_api_key": "MULTIMODAL-KEY-MUST-NOT-LEAK",
                    "multimodal_model_name": "google/gemini-2.5-flash",
                    "multimodal_text_fallback_enabled": True,
                    "tts_api_key": "TTS-KEY-MUST-NOT-LEAK",
                },
            }

        monkeypatch.setattr(event_service, "_load_config_from_db", _fake_load)

        result = await event_service._load_runtime_llm_credentials(uuid4())

        # Exactly four fields, all llm_*.
        assert set(result.keys()) == {
            "llm_provider",
            "llm_api_url",
            "llm_api_key",
            "llm_model_name",
        }
        # Multimodal bundle and gate must be entirely absent — not even the
        # non-secret gate leaks.
        assert "multimodal_provider" not in result
        assert "multimodal_api_url" not in result
        assert "multimodal_api_key" not in result
        assert "multimodal_model_name" not in result
        assert "multimodal_text_fallback_enabled" not in result
        # Zero secret bleed from any adjacent capability.
        import json

        serialized = json.dumps(result)
        assert "MULTIMODAL-KEY-MUST-NOT-LEAK" not in serialized
        assert "TTS-KEY-MUST-NOT-LEAK" not in serialized
