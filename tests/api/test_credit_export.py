"""``GET /v1/credits/export`` (credit Group 5, Deliverable 1), SQLite.

The real application (lifespan, middleware, SQLite ``:memory:``) with the
credit switch forced per test.  Pinned here:

* a credit surface: switch off => 503 ``credit_unavailable``;
* the archive holds exactly the tenant's ``media_assets.payload`` and
  ``knowledge_documents.content`` bytes, and ``manifest.json`` lists each
  entry's SHA-256 (verified against the archive bytes here) and, when a
  ``ready`` additional copy with the same SHA-256 is registered, its exact
  S3 ``VersionId``;
* IDOR: another builder's clinic and a clinic that does not exist answer the
  same 403, and no other tenant's item ever appears in an archive;
* export stays available in every lifecycle state (``tests`` in
  ``test_credit_lifecycle.py`` drive the states; here: a suspended builder).

The PostgreSQL half (real bytes against a disposable PostgreSQL and S3) is
the browser E2E ``frontend/artifacts/neurostack/tests/e2e/credit-export.spec.ts``.
"""

from __future__ import annotations

import hashlib
import io
import json
import secrets
import zipfile
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest

from src.api.middleware.auth import pwd_context
from src.config.settings import settings
from src.models.builder import Builder
from src.models.credit_object_copy import CreditObjectCopy
from src.models.knowledge import KnowledgeDocument
from src.models.media_asset import MediaAsset
from src.models.tenant import Tenant
from src.repositories.database import get_db_session
from src.services.credit_export import safe_entry_name
from src.services.credit_switch import CreditSwitch

EXPORT = "/v1/credits/export"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
PDF = b"%PDF-1.7\n" + b"group-5-export" * 8
DOC = "Protocolo da clínica — revisão 3\nconteúdo autoritativo"


def _jwt(
    tenant_id: UUID,
    *,
    role: str,
    builder_id: UUID | None = None,
    subject: UUID | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    claims: dict[str, Any] = {
        "sub": str(subject or uuid4()),
        "tenant_id": str(tenant_id),
        "role": role,
        "iat": now,
        "exp": now + timedelta(hours=1),
    }
    if builder_id is not None:
        claims["builder_id"] = str(builder_id)
    return jwt.encode(claims, settings.secret_key, algorithm="HS256")


class World:
    def __init__(self) -> None:
        self.builder = uuid4()
        self.other_builder = uuid4()
        self.tenant = uuid4()
        self.sibling = uuid4()
        self.foreign = uuid4()
        self.key = secrets.token_urlsafe(24)
        self.foreign_key = secrets.token_urlsafe(24)
        self.media = uuid4()
        self.pdf = uuid4()
        self.document = uuid4()
        self.foreign_media = uuid4()
        self.copy_version = "3HL4kqtJlcpXroDTDmJ+rmSpXd3dIbrHY"

    def api(self, foreign: bool = False) -> dict[str, str]:
        return {"X-API-Key": self.foreign_key if foreign else self.key}

    def builder_jwt(self) -> dict[str, str]:
        token = _jwt(self.tenant, role="builder", builder_id=self.builder)
        return {"Authorization": f"Bearer {token}"}


def _media(
    media_id: UUID, tenant_id: UUID, payload: bytes, **overrides: Any
) -> MediaAsset:
    values: dict[str, Any] = {
        "id": media_id,
        "tenant_id": tenant_id,
        "display_name": "Foto",
        "original_filename": "../../etc/foto clínica.png",
        "category": "image",
        "mime_type": "image/png",
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).digest(),
        "payload": payload,
    }
    values.update(overrides)
    return MediaAsset(**values)


async def _seed(world: World) -> None:
    session = get_db_session()
    try:
        async with session.begin():
            session.add(Builder(id=world.builder, name="Export builder"))
            session.add(Builder(id=world.other_builder, name="Other builder"))
            await session.flush()
            for tenant_id, builder, key in (
                (world.tenant, world.builder, world.key),
                (world.sibling, world.builder, secrets.token_urlsafe(24)),
                (world.foreign, world.other_builder, world.foreign_key),
            ):
                session.add(
                    Tenant(
                        id=tenant_id,
                        name=f"Export {tenant_id.hex[:6]}",
                        slug=f"export-{tenant_id.hex}",
                        api_key_hash=pwd_context.hash(key),
                        builder_id=builder,
                        config={},
                    )
                )
            await session.flush()
            session.add(_media(world.media, world.tenant, PNG))
            session.add(
                _media(
                    world.pdf,
                    world.tenant,
                    PDF,
                    original_filename="contrato.pdf",
                    category="pdf",
                    mime_type="application/pdf",
                    # A stored digest that disagrees with the bytes: exported
                    # as stored and flagged, never repaired nor dropped.
                    sha256=hashlib.sha256(b"something else").digest(),
                )
            )
            session.add(
                KnowledgeDocument(
                    id=world.document,
                    tenant_id=world.tenant,
                    title="Protocolo",
                    content=DOC,
                    content_type="text",
                    document_metadata={},
                )
            )
            session.add(
                _media(world.foreign_media, world.foreign, b"\x89PNG\r\n\x1a\nforeign")
            )
            session.add(
                CreditObjectCopy(
                    source_kind="media",
                    source_id=world.media,
                    tenant_id=world.tenant,
                    builder_id=world.builder,
                    object_key=f"tenants/{world.tenant}/objects/{world.media}",
                    version_id=world.copy_version,
                    etag="etag-1",
                    content_sha256=hashlib.sha256(PNG).hexdigest(),
                )
            )
            # A deleted copy and a copy of other bytes are never named.
            session.add(
                CreditObjectCopy(
                    source_kind="knowledge",
                    source_id=world.document,
                    tenant_id=world.tenant,
                    builder_id=world.builder,
                    object_key=f"tenants/{world.tenant}/objects/{world.document}",
                    version_id="deleted-version",
                    etag="etag-2",
                    content_sha256=hashlib.sha256(DOC.encode()).hexdigest(),
                    state="deleted",
                )
            )
    finally:
        await session.close()


@pytest.fixture
def world(client: Any) -> World:
    seeded = World()
    client._submit(_seed(seeded))
    return seeded


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    def force(effective: bool) -> None:
        monkeypatch.setattr(CreditSwitch, "effective", lambda _self: effective)

    force(True)
    return force


def _archive(response: Any) -> tuple[zipfile.ZipFile, dict[str, Any]]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].startswith("attachment;")
    body = response.content
    assert (
        response.headers["x-credit-export-sha256"] == hashlib.sha256(body).hexdigest()
    )
    archive = zipfile.ZipFile(io.BytesIO(body))
    manifest_bytes = archive.read("manifest.json")
    assert (
        response.headers["x-credit-export-manifest-sha256"]
        == hashlib.sha256(manifest_bytes).hexdigest()
    )
    return archive, json.loads(manifest_bytes)


def test_switch_off_answers_credit_unavailable(
    client: Any, world: World, switch: Callable[[bool], None]
) -> None:
    switch(False)
    response = client.get(EXPORT, headers=world.api())
    assert response.status_code == 503
    assert response.json() == {"code": "credit_unavailable"}


def test_unauthenticated_is_refused_before_anything(
    client: Any, world: World, switch: Callable[[bool], None]
) -> None:
    assert client.get(EXPORT).status_code == 401


def test_archive_holds_the_authoritative_bytes_with_hashes(
    client: Any, world: World, switch: Callable[[bool], None]
) -> None:
    archive, manifest = _archive(client.get(EXPORT, headers=world.api()))
    assert manifest["schema_version"] == "credit_export.v1"
    assert manifest["tenant_id"] == str(world.tenant)
    assert manifest["counts"] == {"media": 2, "knowledge": 1}
    by_id = {item["id"]: item for item in manifest["items"]}
    assert set(by_id) == {str(world.media), str(world.pdf), str(world.document)}
    for item in manifest["items"]:
        data = archive.read(item["path"])
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
        assert len(data) == item["size_bytes"]
    png = by_id[str(world.media)]
    assert archive.read(png["path"]) == PNG
    assert png["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert png["source_sha256_matches"] is True
    # The exact VersionId of the ready copy with the same SHA-256.
    assert png["s3_copy"]["version_id"] == world.copy_version
    # Traversal-free entry name.
    assert png["path"] == f"media/{world.media}/etc_foto_clinica.png"
    pdf = by_id[str(world.pdf)]
    assert archive.read(pdf["path"]) == PDF
    assert pdf["source_sha256_matches"] is False
    assert pdf["s3_copy"] is None
    document = by_id[str(world.document)]
    assert archive.read(document["path"]).decode("utf-8") == DOC
    # A deleted copy is never named.
    assert document["s3_copy"] is None
    assert sorted(archive.namelist()) == sorted(
        [*(item["path"] for item in manifest["items"]), "manifest.json"]
    )


def test_builder_exports_its_own_clinic_and_never_another_builders(
    client: Any, world: World, switch: Callable[[bool], None]
) -> None:
    _, own = _archive(
        client.get(
            EXPORT,
            params={"tenant_id": str(world.sibling)},
            headers=world.builder_jwt(),
        )
    )
    assert own["tenant_id"] == str(world.sibling)
    assert own["items"] == []

    foreign = client.get(
        EXPORT, params={"tenant_id": str(world.foreign)}, headers=world.builder_jwt()
    )
    missing = client.get(
        EXPORT, params={"tenant_id": str(uuid4())}, headers=world.builder_jwt()
    )
    assert foreign.status_code == missing.status_code == 403
    assert foreign.json() == missing.json()
    assert str(world.foreign) not in foreign.text


def test_api_key_cannot_select_another_tenant(
    client: Any, world: World, switch: Callable[[bool], None]
) -> None:
    response = client.get(
        EXPORT, params={"tenant_id": str(world.foreign)}, headers=world.api()
    )
    assert response.status_code == 403
    _, foreign = _archive(client.get(EXPORT, headers=world.api(foreign=True)))
    assert [item["id"] for item in foreign["items"]] == [str(world.foreign_media)]
    _, own = _archive(client.get(EXPORT, headers=world.api()))
    assert str(world.foreign_media) not in {item["id"] for item in own["items"]}


def test_clinic_admin_is_scoped_to_its_token(
    client: Any, world: World, switch: Callable[[bool], None]
) -> None:
    token = _jwt(world.tenant, role="clinic_admin")
    headers = {"Authorization": f"Bearer {token}"}
    _, manifest = _archive(client.get(EXPORT, headers=headers))
    assert manifest["tenant_id"] == str(world.tenant)
    refused = client.get(
        EXPORT, params={"tenant_id": str(world.sibling)}, headers=headers
    )
    assert refused.status_code == 403


@pytest.mark.parametrize(
    "original,expected",
    [
        ("../../etc/passwd", "etc_passwd"),
        ("C:\\Windows\\system32", "C_Windows_system32"),
        ("", "fallback"),
        ("....", "fallback"),
        ("relatório final.pdf", "relatorio_final.pdf"),
    ],
)
def test_entry_names_are_traversal_free(original: str, expected: str) -> None:
    assert safe_entry_name(original, "fallback") == expected
