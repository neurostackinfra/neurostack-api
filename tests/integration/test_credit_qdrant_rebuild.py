"""Real Qdrant proof for Group 4b's disposable derived chunks."""

from __future__ import annotations

import os
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from src.config.settings import Settings
from src.storage.credit_qdrant import (
    assert_qdrant_ready,
    delete_derived_chunks,
    replace_derived_chunks,
)

pytestmark = pytest.mark.integration


def _config() -> Settings:
    value = os.getenv("CREDIT_TEST_QDRANT_URL")
    if not value:
        pytest.skip("CREDIT_TEST_QDRANT_URL is not configured")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        pytest.fail("CREDIT_TEST_QDRANT_URL must be an absolute HTTP(S) URL")
    return Settings(
        qdrant_host=parsed.hostname,
        qdrant_port=parsed.port or (443 if parsed.scheme == "https" else 80),
        qdrant_collection_name=f"credit_g4b_{uuid4().hex}",
    )


async def test_derived_chunks_are_rebuilt_and_deleted() -> None:
    config = _config()
    tenant_id, source_id = uuid4(), uuid4()
    content = "A" * 1000 + "B"

    await assert_qdrant_ready(config)
    assert (
        await replace_derived_chunks(
            tenant_id=tenant_id,
            source_id=source_id,
            content=content,
            config=config,
        )
        == 2
    )
    client = AsyncQdrantClient(url=f"http://{config.qdrant_host}:{config.qdrant_port}")
    selector = Filter(
        must=[
            FieldCondition(key="tenant_id", match=MatchValue(value=str(tenant_id))),
            FieldCondition(key="source_id", match=MatchValue(value=str(source_id))),
        ]
    )
    try:
        points, _ = await client.scroll(
            collection_name=config.qdrant_collection_name,
            scroll_filter=selector,
            limit=10,
            with_payload=True,
        )
        ordered = sorted(points, key=lambda point: int(point.payload["chunk_index"]))
        assert [point.payload["content"] for point in ordered] == ["A" * 1000, "B"]
        await delete_derived_chunks(
            tenant_id=tenant_id, source_id=source_id, config=config
        )
        absent, _ = await client.scroll(
            collection_name=config.qdrant_collection_name,
            scroll_filter=selector,
            limit=10,
        )
        assert absent == []
    finally:
        await client.delete_collection(config.qdrant_collection_name)
        await client.close()
