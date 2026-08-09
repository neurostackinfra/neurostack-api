"""
NeuroStack API - RLHF Rating Endpoint Tests
Tests for POST /v1/rlhf/rating, GET /v1/rlhf/ratings, PATCH /v1/rlhf/ratings/{id}
and GET /v1/admin/rlhf/pending (implemented from stub).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from src.config.settings import settings

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
        matched = [e for e in list_resp.json() if e["id"] == event_id]
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
        matched = [e for e in list_resp.json() if e["id"] == event_id]
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
        entries = response.json()
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
        entries = response.json()
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
        entries = response.json()
        assert isinstance(entries, list)
        assert all(e["tenant_id"] == str(TEST_TENANT_ID) for e in entries)


# ============================================
# PATCH /v1/rlhf/ratings/{event_id}
# ============================================


class TestPatchRating:
    def test_super_admin_can_approve(
        self, client: TestClient, api_key_headers, super_admin_headers
    ):
        # Seed a rating
        # Keep the approval contract independent from the asynchronous
        # low-rating analysis flow, which has dedicated coverage below.
        post = client.post(
            "/v1/rlhf/rating",
            headers=api_key_headers,
            json=_rating_payload(3),
        )
        event_id = post.json()["event_id"]

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
        matched = [e for e in list_resp.json() if e["id"] == event_id]
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


# ============================================
# GET /v1/admin/rlhf/pending
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


class TestAdminRlhfPending:
    def test_returns_real_feedback_events(
        self, client: TestClient, api_key_headers, super_admin_headers
    ):
        # Seed an rlhf.feedback event via the existing endpoint
        payload = {
            "session_id": str(uuid4()),
            "user_message": "Como agendar?",
            "agent_response": "Ok.",
            "suggested_response": "Ficarei feliz em te ajudar a agendar...",
            "status": "aligned",
        }
        client.post("/v1/rlhf/feedback", headers=api_key_headers, json=payload)

        response = client.get("/v1/admin/rlhf/pending", headers=super_admin_headers)
        assert response.status_code == 200
        entries = response.json()
        assert isinstance(entries, list)
        assert any(
            e.get("suggested_response") == payload["suggested_response"]
            for e in entries
        ), "seeded rlhf.feedback event should surface in admin listing"
