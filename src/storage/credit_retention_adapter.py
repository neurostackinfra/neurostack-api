"""``tombstone | restore | lookup | purge`` over the two stores (Group 5).

Dual-store additive (owner decision, 2026-10-25): PostgreSQL holds the
authoritative bytes (``media_assets.payload``, ``knowledge_documents.content``)
and the additional S3 copy is one exact ``VersionId`` per object; Qdrant
holds only derived, rebuildable chunks.

* :meth:`CreditRetentionAdapter.tombstone` hides the object -- its token row
  already exists in PostgreSQL (``credit_retention_tombstones``) -- and
  removes ONLY the derived Qdrant points.  It never deletes the PostgreSQL row
  nor the S3 version.
* :meth:`CreditRetentionAdapter.restore` re-exposes exactly the same bytes:
  the PostgreSQL row must still hash to the tombstone's SHA-256, the S3
  ``VersionId`` must still exist with the same bytes, and the Qdrant points
  are rebuilt deterministically (the same point ids).
* :meth:`CreditRetentionAdapter.lookup` folds what each store says now.
* :meth:`CreditRetentionAdapter.purge` (exercised only by tests in this
  wish; never reachable from a request) requires the operation's fence at
  the generation the tombstone observed; it refuses an S3 version under
  legal hold (``held``) or retention (``blocked``), deletes the exact
  ``VersionId``, the PostgreSQL row with its bytes (no schema change) and
  the Qdrant points, and answers ``deleted`` only after confirming absence in
  both stores (row gone; ``head_object`` 404 and no such version in
  ``list_object_versions``).  An unknown result is ``failed``, never a
  presumed success.

Synchronous boto3 calls run in a worker thread.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID, uuid5

from botocore.exceptions import ClientError
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import Settings, settings
from src.models.content_retention import CreditPurgeFence, CreditRetentionTombstone
from src.models.credit_object_copy import CreditObjectCopy
from src.models.knowledge import KnowledgeDocument
from src.models.media_asset import MediaAsset
from src.storage.credit_object_store import (
    ObjectCopyError,
    ObjectCopyHeld,
    StoredCopy,
    delete_exact_copy,
)

# The same deterministic chunking and point ids as Group 4b's rebuild
# (``src/storage/credit_qdrant.py``): a restore rebuilds identical points.
_CHUNK_CHARACTERS: Final = 1_000
_POINT_NAMESPACE: Final = UUID("9b2ee417-5ac5-4392-9718-f1a93ea2c76f")


def derived_point_ids(source_id: UUID, content: str) -> list[str]:
    chunks = max(1, -(-len(content) // _CHUNK_CHARACTERS))
    return [
        str(uuid5(_POINT_NAMESPACE, f"{source_id}:{index}")) for index in range(chunks)
    ]


class RetentionStoreError(RuntimeError):
    """A store disagreed with the tombstone (nothing is assumed)."""


@dataclass(frozen=True)
class Lookup:
    """What each store says about one tombstoned object now."""

    postgresql_present: bool
    postgresql_sha256: str | None
    s3_version_present: bool | None  # None: no S3 copy registered
    qdrant_points: int


class CreditRetentionAdapter:
    """The adapter over PostgreSQL, one S3-compatible bucket and Qdrant."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        s3_config: Settings = settings,
        qdrant_config: Settings = settings,
    ) -> None:
        self.sessions = session_factory
        self.s3_config = s3_config
        self.qdrant_config = qdrant_config

    # -- PostgreSQL ------------------------------------------------------

    async def _source_bytes(self, item: CreditRetentionTombstone) -> bytes | None:
        async with self.sessions() as session:
            if item.source_kind == "media":
                payload = await session.scalar(
                    select(MediaAsset.payload).where(
                        MediaAsset.id == item.source_id,
                        MediaAsset.tenant_id == item.tenant_id,
                    )
                )
                return None if payload is None else bytes(payload)
            content = await session.scalar(
                select(KnowledgeDocument.content).where(
                    KnowledgeDocument.id == item.source_id,
                    KnowledgeDocument.tenant_id == item.tenant_id,
                )
            )
            return None if content is None else str(content).encode("utf-8")

    # -- S3 -----------------------------------------------------------------

    def _client(self) -> Any:
        from src.storage.credit_object_store import _client

        return _client(self.s3_config)

    def _copy(self, item: CreditRetentionTombstone) -> StoredCopy | None:
        if item.s3_object_key is None or item.s3_version_id is None:
            return None
        return StoredCopy(
            item.s3_object_key,
            item.s3_version_id,
            item.s3_etag or "",
            item.content_sha256,
        )

    def _s3_version_bytes(self, copy: StoredCopy) -> bytes | None:
        try:
            response = self._client().get_object(
                Bucket=self.s3_config.credit_object_store_bucket,
                Key=copy.object_key,
                VersionId=copy.version_id,
            )
            return bytes(response["Body"].read())
        except ClientError as error:
            if _missing(error):
                return None
            raise RetentionStoreError("S3 version read failed") from error

    def _s3_version_present(self, copy: StoredCopy) -> bool:
        client = self._client()
        bucket = self.s3_config.credit_object_store_bucket
        try:
            client.head_object(
                Bucket=bucket, Key=copy.object_key, VersionId=copy.version_id
            )
            head = True
        except ClientError as error:
            if not _missing(error):
                raise RetentionStoreError("S3 head failed") from error
            head = False
        listed = False
        pages = client.get_paginator("list_object_versions").paginate(
            Bucket=bucket, Prefix=copy.object_key
        )
        for page in pages:
            for entry in (*page.get("Versions", ()), *page.get("DeleteMarkers", ())):
                if (
                    entry.get("Key") == copy.object_key
                    and entry.get("VersionId") == copy.version_id
                ):
                    listed = True
        return head or listed

    def _legal_hold_on(self, copy: StoredCopy) -> bool:
        try:
            response = self._client().get_object_legal_hold(
                Bucket=self.s3_config.credit_object_store_bucket,
                Key=copy.object_key,
                VersionId=copy.version_id,
            )
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            if code in {"NoSuchObjectLockConfiguration", "InvalidRequest"} or _missing(
                error
            ):
                return False
            raise RetentionStoreError("S3 legal-hold read failed") from error
        return bool(response.get("LegalHold", {}).get("Status") == "ON")

    # -- Qdrant -------------------------------------------------------------

    async def _qdrant_points(self, item: CreditRetentionTombstone) -> int:
        if item.source_kind != "knowledge":
            return 0
        from qdrant_client import AsyncQdrantClient
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        config = self.qdrant_config
        client = AsyncQdrantClient(
            url=f"http://{config.qdrant_host}:{config.qdrant_port}"
        )
        try:
            if not await client.collection_exists(config.qdrant_collection_name):
                return 0
            result = await client.count(
                collection_name=config.qdrant_collection_name,
                count_filter=Filter(
                    must=[
                        FieldCondition(
                            key="tenant_id", match=MatchValue(value=str(item.tenant_id))
                        ),
                        FieldCondition(
                            key="source_id", match=MatchValue(value=str(item.source_id))
                        ),
                    ]
                ),
                exact=True,
            )
            return int(result.count)
        finally:
            await client.close()

    async def _drop_points(self, item: CreditRetentionTombstone) -> None:
        if item.source_kind != "knowledge":
            return
        from src.storage.credit_qdrant import delete_derived_chunks

        await delete_derived_chunks(
            tenant_id=item.tenant_id,
            source_id=item.source_id,
            config=self.qdrant_config,
        )

    # -- the four operations -------------------------------------------------

    async def tombstone(self, item: CreditRetentionTombstone) -> None:
        """Hide: only the derived Qdrant points go; PostgreSQL and S3 stay."""

        await self._drop_points(item)

    async def restore(self, item: CreditRetentionTombstone) -> None:
        """The same bytes, the same ``VersionId``, the same Qdrant points."""

        data = await self._source_bytes(item)
        if data is None or hashlib.sha256(data).hexdigest() != item.content_sha256:
            raise RetentionStoreError("PostgreSQL bytes differ from the tombstone")
        copy = self._copy(item)
        if copy is not None:
            remote = await asyncio.to_thread(self._s3_version_bytes, copy)
            if (
                remote is None
                or hashlib.sha256(remote).hexdigest() != item.content_sha256
            ):
                raise RetentionStoreError("S3 VersionId differs from the tombstone")
        if item.source_kind == "knowledge":
            from src.storage.credit_qdrant import replace_derived_chunks

            await replace_derived_chunks(
                tenant_id=item.tenant_id,
                source_id=item.source_id,
                content=data.decode("utf-8"),
                config=self.qdrant_config,
            )

    async def lookup(self, item: CreditRetentionTombstone) -> Lookup:
        data = await self._source_bytes(item)
        copy = self._copy(item)
        present = (
            None
            if copy is None
            else await asyncio.to_thread(self._s3_version_present, copy)
        )
        return Lookup(
            postgresql_present=data is not None,
            postgresql_sha256=(
                None if data is None else hashlib.sha256(data).hexdigest()
            ),
            s3_version_present=present,
            qdrant_points=await self._qdrant_points(item),
        )

    async def _fence_valid(self, item: CreditRetentionTombstone) -> bool:
        async with self.sessions() as session:
            fence = (
                await session.execute(
                    select(CreditPurgeFence).where(
                        CreditPurgeFence.operation_id == item.operation_id,
                        CreditPurgeFence.builder_id == item.builder_id,
                        CreditPurgeFence.scope == item.scope,
                        CreditPurgeFence.generation == item.observed_generation,
                    )
                )
            ).scalar_one_or_none()
        return fence is not None

    async def _source_unchanged(self, item: CreditRetentionTombstone) -> bool:
        """The PostgreSQL bytes still hash to the tombstone's (an absent row
        -- a crash after its delete -- is unchanged)."""

        data = await self._source_bytes(item)
        return data is None or hashlib.sha256(data).hexdigest() == item.content_sha256

    async def purge(self, item: CreditRetentionTombstone) -> str:
        """``deleted`` only after confirmed absence in both stores."""

        if not await self._fence_valid(item):
            return "blocked"
        # The authoritative bytes first: an object that changed since its
        # tombstone is blocked BEFORE anything irreversible (the S3 version
        # included) is deleted.
        if not await self._source_unchanged(item):
            return "blocked"
        copy = self._copy(item)
        if copy is not None:
            # A version already confirmed absent (a crash after its delete)
            # is not deleted again; its absence is what we record.
            if await asyncio.to_thread(self._s3_version_present, copy):
                if await asyncio.to_thread(self._legal_hold_on, copy):
                    return "held"
                try:
                    await asyncio.to_thread(
                        delete_exact_copy, copy=copy, config=self.s3_config
                    )
                except ObjectCopyHeld:
                    return "blocked"  # retention / Object Lock refused the delete
                except ObjectCopyError:
                    return "failed"
            async with self.sessions() as session, session.begin():
                await session.execute(
                    update(CreditObjectCopy)
                    .where(
                        CreditObjectCopy.object_key == copy.object_key,
                        CreditObjectCopy.version_id == copy.version_id,
                    )
                    .values(state="deleted")
                )
        data = await self._source_bytes(item)
        if data is not None:
            if hashlib.sha256(data).hexdigest() != item.content_sha256:
                return "blocked"  # the object changed since the tombstone
            model: Any = (
                MediaAsset if item.source_kind == "media" else KnowledgeDocument
            )
            async with self.sessions() as session, session.begin():
                await session.execute(
                    delete(model).where(
                        model.id == item.source_id, model.tenant_id == item.tenant_id
                    )
                )
        await self._drop_points(item)
        observed = await self.lookup(item)
        if (
            observed.postgresql_present
            or observed.s3_version_present
            or observed.qdrant_points
        ):
            return "failed"
        return "deleted"


def _missing(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code", ""))
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"404", "NoSuchKey", "NoSuchVersion", "NotFound"} or status == 404


__all__ = [
    "CreditRetentionAdapter",
    "Lookup",
    "RetentionStoreError",
    "derived_point_ids",
]
