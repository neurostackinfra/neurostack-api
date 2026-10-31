"""Tenant-scoped export of the authoritative content bytes (credit Group 5).

The export is the first promise of the retention lifecycle (Deliverable 1):
before any suspension or retention rule may act, a builder can download the
exact bytes PostgreSQL holds for one tenant -- ``media_assets.payload`` and
``knowledge_documents.content`` -- with a manifest of hashes.  It reads only
PostgreSQL, the authority of the bytes in this wish; the additional S3 copy of
Group 4b is never read, only named: when a ``ready`` copy with the same
SHA-256 is registered, the manifest records its exact ``VersionId``.

Archive layout (``application/zip``, entries stored uncompressed so a client
can verify them with nothing but a SHA-256)::

    manifest.json
    media/{media_id}/{sanitised original filename}
    knowledge/{document_id}.txt          (UTF-8 of knowledge_documents.content)

``manifest.json`` (``schema_version = credit_export.v1``) lists every entry in
archive order with its ``sha256`` -- of the bytes written in the archive --
and, for media, ``source_sha256`` (the stored ``media_assets.sha256``) and
whether the two agree.  A divergent row is exported as stored and flagged,
never silently repaired or dropped: the export is a portability promise, not
an integrity gate.  The response also carries the manifest's own SHA-256.

Scope.  The tenant comes from the route's authorization (never from a body);
every read here filters by that ``tenant_id``.  Payloads are loaded one row
at a time so a tenant's whole media inventory never sits in memory at once;
the archive is spooled to a temporary file (memory up to ``SPOOL_MAX_BYTES``).
Nothing here writes to the database.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unicodedata
import zipfile
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import IO, Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.credit_object_copy import CreditObjectCopy
from src.models.knowledge import KnowledgeDocument
from src.models.media_asset import MediaAsset

EXPORT_SCHEMA_VERSION: Final = "credit_export.v1"
MANIFEST_NAME: Final = "manifest.json"
SPOOL_MAX_BYTES: Final = 16 * 1024 * 1024
STREAM_CHUNK_BYTES: Final = 256 * 1024
# A ZIP entry timestamp cannot precede 1980; the archive is deterministic
# apart from the manifest's ``generated_at``.
_ZIP_EPOCH: Final = (1980, 1, 1, 0, 0, 0)
_UNSAFE_NAME: Final = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class ExportItem:
    """One archive entry, as the manifest lists it."""

    kind: str
    source_id: UUID
    path: str
    size_bytes: int
    sha256: str
    content_type: str
    title: str
    created_at: datetime | None
    source_sha256: str | None = None
    s3_copy: dict[str, str] | None = None

    @property
    def source_sha256_matches(self) -> bool | None:
        if self.source_sha256 is None:
            return None
        return self.source_sha256 == self.sha256

    def manifest_entry(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "kind": self.kind,
            "id": str(self.source_id),
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "content_type": self.content_type,
            "title": self.title,
            "created_at": _iso(self.created_at),
            "s3_copy": self.s3_copy,
        }
        if self.kind == "media":
            entry["source_sha256"] = self.source_sha256
            entry["source_sha256_matches"] = self.source_sha256_matches
        return entry


@dataclass
class ExportArchive:
    """A finished archive: a rewound spooled file plus its manifest."""

    tenant_id: UUID
    file: IO[bytes]
    size_bytes: int
    manifest: dict[str, Any]
    manifest_sha256: str
    archive_sha256: str

    @property
    def items(self) -> Sequence[dict[str, Any]]:
        return tuple(self.manifest["items"])

    def filename(self) -> str:
        stamp = str(self.manifest["generated_at"]).replace(":", "").replace("-", "")
        return f"neurostack-export-{self.tenant_id}-{stamp[:15]}.zip"

    async def chunks(self) -> AsyncIterator[bytes]:
        """Stream the archive and close the spooled file at the end."""

        try:
            self.file.seek(0)
            while True:
                chunk = self.file.read(STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                yield chunk
        finally:
            self.file.close()

    def close(self) -> None:
        self.file.close()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def safe_entry_name(original: str, fallback: str) -> str:
    """A portable, traversal-free file name for one archive entry."""

    normalized = unicodedata.normalize("NFKD", original or "")
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    # Separators are outside the safe set: every run of unsafe characters
    # (``/``, ``\``, ``:`` ...) becomes one ``_``, so no entry leaves its folder.
    cleaned = _UNSAFE_NAME.sub("_", ascii_name).strip("._")
    return (cleaned or fallback)[:120]


def _zip_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=_ZIP_EPOCH)
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = 0o644 << 16
    return info


async def _copy_versions(
    session: AsyncSession, tenant_id: UUID
) -> dict[tuple[str, UUID, str], dict[str, str]]:
    """The live additional copies of this tenant, by exact content: ``ready``
    and ``held`` (a copy under a credit legal hold is still the same exact
    version); never a ``deleted`` or ``failed`` one."""

    rows = await session.execute(
        select(
            CreditObjectCopy.source_kind,
            CreditObjectCopy.source_id,
            CreditObjectCopy.content_sha256,
            CreditObjectCopy.object_key,
            CreditObjectCopy.version_id,
            CreditObjectCopy.etag,
        )
        .where(
            CreditObjectCopy.tenant_id == tenant_id,
            CreditObjectCopy.state.in_(("ready", "held")),
        )
        .order_by(CreditObjectCopy.created_at, CreditObjectCopy.id)
    )
    copies: dict[tuple[str, UUID, str], dict[str, str]] = {}
    for row in rows:
        copies.setdefault(
            (str(row.source_kind), row.source_id, str(row.content_sha256)),
            {
                "object_key": str(row.object_key),
                "version_id": str(row.version_id),
                "etag": str(row.etag),
            },
        )
    return copies


async def build_tenant_export(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    now: datetime | None = None,
) -> ExportArchive:
    """Write the tenant's archive (caller authorized ``tenant_id``).

    Read-only; the caller ends its transaction.  The returned archive owns a
    spooled file the caller streams with :meth:`ExportArchive.chunks` (or
    closes with :meth:`ExportArchive.close`).
    """

    generated_at = now or datetime.now(timezone.utc)
    copies = await _copy_versions(session, tenant_id)
    media_rows = (
        await session.execute(
            select(
                MediaAsset.id,
                MediaAsset.display_name,
                MediaAsset.original_filename,
                MediaAsset.mime_type,
                MediaAsset.sha256,
                MediaAsset.created_at,
            )
            .where(MediaAsset.tenant_id == tenant_id)
            .order_by(MediaAsset.created_at, MediaAsset.id)
        )
    ).all()
    knowledge_rows = (
        await session.execute(
            select(
                KnowledgeDocument.id,
                KnowledgeDocument.title,
                KnowledgeDocument.created_at,
            )
            .where(KnowledgeDocument.tenant_id == tenant_id)
            .order_by(KnowledgeDocument.created_at, KnowledgeDocument.id)
        )
    ).all()

    spool: IO[bytes] = tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES)
    items: list[ExportItem] = []
    try:
        with zipfile.ZipFile(spool, mode="w", compression=zipfile.ZIP_STORED) as zf:
            for row in media_rows:
                payload = await session.scalar(
                    select(MediaAsset.payload).where(
                        MediaAsset.id == row.id, MediaAsset.tenant_id == tenant_id
                    )
                )
                if payload is None:
                    # Deleted between the listing and this read: not exported.
                    continue
                data = bytes(payload)
                digest = hashlib.sha256(data).hexdigest()
                name = safe_entry_name(row.original_filename, f"media-{row.id}")
                path = f"media/{row.id}/{name}"
                zf.writestr(_zip_info(path), data)
                items.append(
                    ExportItem(
                        kind="media",
                        source_id=row.id,
                        path=path,
                        size_bytes=len(data),
                        sha256=digest,
                        content_type=str(row.mime_type),
                        title=str(row.display_name),
                        created_at=row.created_at,
                        source_sha256=bytes(row.sha256).hex(),
                        s3_copy=copies.get(("media", row.id, digest)),
                    )
                )
                del data, payload
            for document in knowledge_rows:
                content = await session.scalar(
                    select(KnowledgeDocument.content).where(
                        KnowledgeDocument.id == document.id,
                        KnowledgeDocument.tenant_id == tenant_id,
                    )
                )
                if content is None:
                    continue
                data = str(content).encode("utf-8")
                digest = hashlib.sha256(data).hexdigest()
                path = f"knowledge/{document.id}.txt"
                zf.writestr(_zip_info(path), data)
                items.append(
                    ExportItem(
                        kind="knowledge",
                        source_id=document.id,
                        path=path,
                        size_bytes=len(data),
                        sha256=digest,
                        content_type="text/plain; charset=utf-8",
                        title=str(document.title),
                        created_at=document.created_at,
                        s3_copy=copies.get(("knowledge", document.id, digest)),
                    )
                )
            manifest: dict[str, Any] = {
                "schema_version": EXPORT_SCHEMA_VERSION,
                "tenant_id": str(tenant_id),
                "generated_at": _iso(generated_at),
                "hash_algorithm": "sha256",
                "authority": "postgresql",
                "counts": {
                    "media": sum(1 for item in items if item.kind == "media"),
                    "knowledge": sum(1 for item in items if item.kind == "knowledge"),
                },
                "items": [item.manifest_entry() for item in items],
            }
            manifest_bytes = json.dumps(
                manifest, ensure_ascii=False, sort_keys=True, indent=2
            ).encode("utf-8")
            zf.writestr(_zip_info(MANIFEST_NAME), manifest_bytes)
        size = spool.tell()
        spool.seek(0)
        archive_digest = hashlib.sha256()
        while True:
            chunk = spool.read(STREAM_CHUNK_BYTES)
            if not chunk:
                break
            archive_digest.update(chunk)
        spool.seek(0)
    except BaseException:
        spool.close()
        raise
    return ExportArchive(
        tenant_id=tenant_id,
        file=spool,
        size_bytes=size,
        manifest=manifest,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        archive_sha256=archive_digest.hexdigest(),
    )


__all__ = [
    "EXPORT_SCHEMA_VERSION",
    "MANIFEST_NAME",
    "ExportArchive",
    "ExportItem",
    "build_tenant_export",
    "safe_entry_name",
]
