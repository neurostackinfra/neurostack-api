"""Derived Qdrant adapter used only by Group 4b's rebuild/collection jobs."""

from __future__ import annotations

import hashlib
from uuid import UUID, uuid5

from qdrant_client import AsyncQdrantClient

from src.config.settings import Settings, settings


class CreditQdrantError(RuntimeError):
    pass


_CHUNK_CHARACTERS = 1_000
_POINT_NAMESPACE = UUID("9b2ee417-5ac5-4392-9718-f1a93ea2c76f")


def _url(config: Settings) -> str:
    return f"http://{config.qdrant_host}:{config.qdrant_port}"


async def assert_qdrant_ready(config: Settings = settings) -> None:
    client = AsyncQdrantClient(url=_url(config))
    try:
        await client.get_collections()
    except Exception as error:
        raise CreditQdrantError("Qdrant is unavailable") from error
    finally:
        await client.close()


async def delete_derived_chunks(
    *, tenant_id: UUID, source_id: UUID, config: Settings = settings
) -> None:
    """Delete only rebuildable chunks, never an authoritative object."""

    from qdrant_client.models import FieldCondition, Filter, MatchValue

    client = AsyncQdrantClient(url=_url(config))
    try:
        if not await client.collection_exists(config.qdrant_collection_name):
            return
        await client.delete(
            collection_name=config.qdrant_collection_name,
            points_selector=Filter(
                must=[
                    FieldCondition(
                        key="tenant_id", match=MatchValue(value=str(tenant_id))
                    ),
                    FieldCondition(
                        key="source_id", match=MatchValue(value=str(source_id))
                    ),
                ]
            ),
        )
    except Exception as error:
        raise CreditQdrantError("Qdrant derived-chunk delete failed") from error
    finally:
        await client.close()


def _chunks(content: str) -> tuple[str, ...]:
    """A deliberately simple, deterministic, rebuildable chunk boundary."""

    return tuple(
        content[offset : offset + _CHUNK_CHARACTERS]
        for offset in range(0, len(content), _CHUNK_CHARACTERS)
    ) or ("",)


async def replace_derived_chunks(
    *,
    tenant_id: UUID,
    source_id: UUID,
    content: str,
    config: Settings = settings,
) -> int:
    """Replace one document's disposable Qdrant chunks atomically enough.

    The payload stores text and a deterministic placeholder vector only.  It
    is intentionally not an authoritative knowledge path or a search API;
    Group 4b needs a durable/rebuildable external representation which a
    later embedding owner may replace without a migration of source data.
    """

    from qdrant_client.models import (
        Distance,
        FieldCondition,
        Filter,
        MatchValue,
        PointStruct,
        VectorParams,
    )

    client = AsyncQdrantClient(url=_url(config))
    try:
        if not await client.collection_exists(config.qdrant_collection_name):
            await client.create_collection(
                collection_name=config.qdrant_collection_name,
                vectors_config=VectorParams(size=1, distance=Distance.COSINE),
            )
        selector = Filter(
            must=[
                FieldCondition(key="tenant_id", match=MatchValue(value=str(tenant_id))),
                FieldCondition(key="source_id", match=MatchValue(value=str(source_id))),
            ]
        )
        await client.delete(
            collection_name=config.qdrant_collection_name,
            points_selector=selector,
            wait=True,
        )
        chunks = _chunks(content)
        content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        await client.upsert(
            collection_name=config.qdrant_collection_name,
            wait=True,
            points=[
                PointStruct(
                    id=str(uuid5(_POINT_NAMESPACE, f"{source_id}:{index}")),
                    vector=[1.0],
                    payload={
                        "tenant_id": str(tenant_id),
                        "source_id": str(source_id),
                        "chunk_index": index,
                        "content": chunk,
                        "content_sha256": content_sha256,
                    },
                )
                for index, chunk in enumerate(chunks)
            ],
        )
        return len(chunks)
    except Exception as error:
        raise CreditQdrantError("Qdrant derived-chunk rebuild failed") from error
    finally:
        await client.close()


__all__ = [
    "CreditQdrantError",
    "assert_qdrant_ready",
    "delete_derived_chunks",
    "replace_derived_chunks",
]
