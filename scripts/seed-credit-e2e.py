#!/usr/bin/env python3
"""Seed the credit export browser E2E (neurostack-credit-system Group 5).

    ENVIRONMENT=test DATABASE_URL=postgresql+asyncpg://...@127.0.0.1:.../db \\
    SECRET_KEY=... CREDIT_TEST_S3_ENDPOINT=... CREDIT_TEST_S3_BUCKET=... \\
    CREDIT_TEST_S3_ACCESS_KEY_ID=... CREDIT_TEST_S3_SECRET_ACCESS_KEY=... \\
        poetry run python scripts/seed-credit-e2e.py

Against a DISPOSABLE PostgreSQL already at ``alembic upgrade head`` and a
disposable S3-compatible bucket created with Object Lock, it writes (fixed
ids, so a rerun changes nothing):

* a builder, one of its clinics (tenant) and a ``builder`` user;
* one PNG in ``media_assets`` and one knowledge document, with deterministic
  bytes the E2E spec rebuilds to check the downloaded SHA-256;
* the additional S3 copy of the PNG (``put_immutable_copy``, exact
  ``VersionId``) registered in ``credit_object_copies`` as ``ready``, so the
  export manifest must name that ``VersionId``.

It prints, on stdout only, shell ``export`` lines to ``eval``:
``CREDIT_E2E_JWT`` (a builder JWT signed with the API's ``SECRET_KEY``) and
``CREDIT_E2E_S3_VERSION_ID``.  Diagnostics go to stderr.  It refuses to run
outside ``ENVIRONMENT`` development/test or against a non-loopback database.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shlex
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import jwt  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config.settings import Settings, settings  # noqa: E402

BUILDER_ID: Final = UUID("5e000000-0000-4000-8000-000000000001")
TENANT_ID: Final = UUID("5e000000-0000-4000-8000-000000000002")
USER_ID: Final = UUID("5e000000-0000-4000-8000-000000000003")
MEDIA_ID: Final = UUID("5e000000-0000-4000-8000-000000000004")
DOCUMENT_ID: Final = UUID("5e000000-0000-4000-8000-000000000005")
# The E2E spec rebuilds these exact bytes (keep both in sync).
PNG_PAYLOAD: Final = (
    b"\x89PNG\r\n\x1a\n" + b"neurostack-credit-e2e-export" + bytes(range(256))
)
DOCUMENT_TEXT: Final = "NeuroStack credit export E2E: conteúdo autoritativo\n"
_S3_ENV: Final = (
    "CREDIT_TEST_S3_ENDPOINT",
    "CREDIT_TEST_S3_BUCKET",
    "CREDIT_TEST_S3_ACCESS_KEY_ID",
    "CREDIT_TEST_S3_SECRET_ACCESS_KEY",
)


def _refuse(message: str) -> None:
    print(f"seed-credit-e2e: {message}", file=sys.stderr)
    raise SystemExit(2)


def _preflight() -> Settings:
    if settings.environment not in ("development", "test"):
        _refuse("ENVIRONMENT must be development or test")
    host = urlsplit(settings.database_url.replace("+asyncpg", "")).hostname
    if host not in ("127.0.0.1", "localhost", "::1"):
        _refuse("DATABASE_URL must point at a disposable loopback PostgreSQL")
    if not settings.database_url.startswith("postgresql"):
        _refuse("DATABASE_URL must be PostgreSQL")
    missing = [name for name in _S3_ENV if not os.getenv(name)]
    if missing:
        _refuse("missing disposable S3 configuration: " + ", ".join(missing))
    return Settings(
        credit_object_store_endpoint=os.environ["CREDIT_TEST_S3_ENDPOINT"],
        credit_object_store_bucket=os.environ["CREDIT_TEST_S3_BUCKET"],
        credit_object_store_access_key_id=os.environ["CREDIT_TEST_S3_ACCESS_KEY_ID"],
        credit_object_store_secret_access_key=os.environ[
            "CREDIT_TEST_S3_SECRET_ACCESS_KEY"
        ],
    )


async def _seed(s3: Settings) -> str:
    from src.api.middleware.auth import pwd_context
    from src.models.builder import Builder
    from src.models.credit_object_copy import CreditObjectCopy
    from src.models.knowledge import KnowledgeDocument
    from src.models.media_asset import MediaAsset
    from src.models.tenant import Tenant
    from src.models.user import User, UserRole
    from src.storage.credit_object_store import put_immutable_copy

    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    digest = hashlib.sha256(PNG_PAYLOAD).digest()
    try:
        async with factory() as session, session.begin():
            if await session.get(Builder, BUILDER_ID) is None:
                session.add(Builder(id=BUILDER_ID, name="Credit E2E builder"))
                await session.flush()
            if await session.get(Tenant, TENANT_ID) is None:
                session.add(
                    Tenant(
                        id=TENANT_ID,
                        name="Credit E2E clinic",
                        slug="credit-e2e-clinic",
                        api_key_hash=pwd_context.hash(os.urandom(16).hex()),
                        builder_id=BUILDER_ID,
                        config={},
                    )
                )
                await session.flush()
            if await session.get(User, USER_ID) is None:
                session.add(
                    User(
                        id=USER_ID,
                        tenant_id=TENANT_ID,
                        email="credit-e2e-builder@example.invalid",
                        password_hash=pwd_context.hash(os.urandom(16).hex()),
                        role=UserRole("builder"),
                        builder_id=BUILDER_ID,
                    )
                )
            if await session.get(MediaAsset, MEDIA_ID) is None:
                session.add(
                    MediaAsset(
                        id=MEDIA_ID,
                        tenant_id=TENANT_ID,
                        display_name="E2E export image",
                        original_filename="credit-e2e.png",
                        category="image",
                        mime_type="image/png",
                        size_bytes=len(PNG_PAYLOAD),
                        sha256=digest,
                        payload=PNG_PAYLOAD,
                    )
                )
            if await session.get(KnowledgeDocument, DOCUMENT_ID) is None:
                session.add(
                    KnowledgeDocument(
                        id=DOCUMENT_ID,
                        tenant_id=TENANT_ID,
                        title="E2E export document",
                        content=DOCUMENT_TEXT,
                        content_type="text",
                        document_metadata={},
                        size_bytes=len(DOCUMENT_TEXT.encode("utf-8")),
                    )
                )
        async with factory() as session:
            version = await session.scalar(
                select(CreditObjectCopy.version_id).where(
                    CreditObjectCopy.source_kind == "media",
                    CreditObjectCopy.source_id == MEDIA_ID,
                    CreditObjectCopy.content_sha256 == digest.hex(),
                    CreditObjectCopy.state == "ready",
                )
            )
        if version is None:
            stored = await asyncio.to_thread(
                put_immutable_copy,
                tenant_id=TENANT_ID,
                object_id=MEDIA_ID,
                payload=PNG_PAYLOAD,
                expected_sha256=digest,
                config=s3,
            )
            async with factory() as session, session.begin():
                session.add(
                    CreditObjectCopy(
                        source_kind="media",
                        source_id=MEDIA_ID,
                        tenant_id=TENANT_ID,
                        builder_id=BUILDER_ID,
                        object_key=stored.object_key,
                        version_id=stored.version_id,
                        etag=stored.etag,
                        content_sha256=stored.content_sha256,
                    )
                )
            version = stored.version_id
        return str(version)
    finally:
        await engine.dispose()


def _token() -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(USER_ID),
            "tenant_id": str(TENANT_ID),
            "role": "builder",
            "builder_id": str(BUILDER_ID),
            "email": "credit-e2e-builder@example.invalid",
            "iat": now,
            "exp": now + timedelta(hours=2),
        },
        settings.secret_key,
        algorithm="HS256",
    )


def main() -> int:
    s3 = _preflight()
    version = asyncio.run(_seed(s3))
    print(
        f"seeded tenant {TENANT_ID}: media {MEDIA_ID} "
        f"(sha256 {hashlib.sha256(PNG_PAYLOAD).hexdigest()}), document {DOCUMENT_ID}",
        file=sys.stderr,
    )
    print(f"export CREDIT_E2E_JWT={shlex.quote(_token())}")
    print(f"export CREDIT_E2E_S3_VERSION_ID={shlex.quote(version)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
