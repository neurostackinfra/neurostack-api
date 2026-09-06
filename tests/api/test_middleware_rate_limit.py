"""
NeuroStack API - Rate Limiting Middleware Tests
"""

import time
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import src.api.middleware.rate_limit as rate_limit_module
from src.api.middleware.rate_limit import rate_limit_store


class TestRateLimitMiddleware:
    """Test suite for rate limiting middleware."""

    def setup_method(self) -> None:
        """Clear rate limit store before each test."""
        # Clear all tenant buckets
        rate_limit_store._buckets.clear()
        rate_limit_store._tenant_limits.clear()

    def test_rate_limit_headers_present(
        self,
        client: TestClient,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test that rate limit headers are present in response."""
        response = client.post(
            "/v1/events",
            json=sample_event_payload,
            headers=api_headers,
        )

        assert response.status_code == 200
        assert "X-RateLimit-Limit" in response.headers
        assert "X-RateLimit-Remaining" in response.headers
        assert "X-RateLimit-Reset" in response.headers

    def test_rate_limit_remaining_decreases(
        self,
        client: TestClient,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test that remaining rate limit decreases with each request."""
        # First request
        response1 = client.post(
            "/v1/events",
            json=sample_event_payload,
            headers=api_headers,
        )
        remaining1 = int(response1.headers["X-RateLimit-Remaining"])

        bucket = rate_limit_store._buckets[sample_event_payload["tenant_id"]]
        frozen_time = bucket.last_refill
        monkeypatch.setattr(
            rate_limit_module,
            "time",
            SimpleNamespace(time=lambda: frozen_time),
        )

        # Second request
        response2 = client.post(
            "/v1/events",
            json=sample_event_payload,
            headers=api_headers,
        )
        remaining2 = int(response2.headers["X-RateLimit-Remaining"])

        assert remaining2 == remaining1 - 1

    def test_health_endpoint_not_rate_limited(
        self,
        client: TestClient,
    ) -> None:
        """Test that health endpoints are not rate limited."""
        # Make many requests to health endpoint
        for _ in range(20):
            response = client.get("/health")
            assert response.status_code == 200
            # Health endpoint should not have rate limit headers
            # (or they should not be tracked)

    def test_ready_endpoint_not_rate_limited(
        self,
        client: TestClient,
    ) -> None:
        """Test that ready endpoint is not rate limited."""
        for _ in range(20):
            response = client.get("/ready")
            assert response.status_code == 200


class TestTokenBucket:
    """Test suite for token bucket implementation."""

    def test_token_bucket_consumes_tokens(self) -> None:
        """Test that token bucket consumes tokens correctly."""
        from src.api.middleware.rate_limit import TokenBucket

        bucket = TokenBucket(capacity=10, refill_rate=1.0)

        # Should have full capacity
        assert bucket.get_remaining() == 10

        # Consume a token
        assert bucket.consume() is True
        assert bucket.get_remaining() == 9

    def test_token_bucket_refills(self) -> None:
        """Test that token bucket refills over time."""
        from src.api.middleware.rate_limit import TokenBucket

        bucket = TokenBucket(capacity=10, refill_rate=10.0)  # 10 per second

        # Consume all tokens
        for _ in range(10):
            bucket.consume()

        assert bucket.get_remaining() == 0

        # Wait a bit for refill
        time.sleep(0.2)

        # Should have some tokens back
        remaining = bucket.get_remaining()
        assert remaining > 0

    def test_token_bucket_rejects_when_empty(self) -> None:
        """Test that token bucket rejects when empty."""
        from src.api.middleware.rate_limit import TokenBucket

        bucket = TokenBucket(capacity=2, refill_rate=0.1)

        # Consume all tokens
        assert bucket.consume() is True
        assert bucket.consume() is True

        # Should reject now
        assert bucket.consume() is False

    def test_token_bucket_capacity_limit(self) -> None:
        """Test that token bucket doesn't exceed capacity."""
        from src.api.middleware.rate_limit import TokenBucket

        bucket = TokenBucket(capacity=10, refill_rate=100.0)

        # Wait for potential over-refill
        time.sleep(0.2)

        # Should not exceed capacity
        assert bucket.get_remaining() <= 10


class TestRateLimitStore:
    """Test suite for rate limit store."""

    def setup_method(self) -> None:
        """Clear store before each test."""
        rate_limit_store._buckets.clear()
        rate_limit_store._tenant_limits.clear()

    def test_get_or_create_bucket_creates_new(self) -> None:
        """Test that get_or_create creates new bucket."""
        bucket = rate_limit_store.get_or_create_bucket("tenant-1", rate_limit=100)

        assert bucket is not None
        assert bucket.capacity > 0

    def test_get_or_create_bucket_returns_existing(self) -> None:
        """Test that get_or_create returns existing bucket."""
        bucket1 = rate_limit_store.get_or_create_bucket("tenant-1", rate_limit=100)
        bucket2 = rate_limit_store.get_or_create_bucket("tenant-1", rate_limit=100)

        assert bucket1 is bucket2

    def test_different_tenants_have_different_buckets(self) -> None:
        """Test that different tenants have separate buckets."""
        bucket1 = rate_limit_store.get_or_create_bucket("tenant-1", rate_limit=100)
        bucket2 = rate_limit_store.get_or_create_bucket("tenant-2", rate_limit=100)

        assert bucket1 is not bucket2

    def test_clear_tenant_removes_bucket(self) -> None:
        """Test that clear_tenant removes tenant bucket."""
        rate_limit_store.get_or_create_bucket("tenant-1", rate_limit=100)
        rate_limit_store.clear_tenant("tenant-1")

        # Should create new bucket
        bucket = rate_limit_store.get_or_create_bucket("tenant-1", rate_limit=100)
        assert bucket.get_remaining() > 0  # Full bucket


class TestRateLimitMiddlewareAsync:
    """Async test suite for rate limiting middleware."""

    @pytest.mark.asyncio
    async def test_async_rate_limit_headers(
        self,
        async_client,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test rate limit headers in async requests."""
        response = await async_client.post(
            "/v1/events",
            json=sample_event_payload,
            headers=api_headers,
        )

        assert response.status_code == 200
        assert "X-RateLimit-Limit" in response.headers
