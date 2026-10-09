"""Unit contracts for media validation, storage-owner keys and the capacity guard."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from src.models.media_asset import MediaAsset
from src.models.tenant import Tenant
from src.services import media_assets
from src.services.media_assets import (
    MEDIA_STORAGE_CAP_BYTES,
    MediaAssetDomainError,
    MediaStorageCapacityExceeded,
    MediaStorageCapacityGuard,
    MediaStorageGuardError,
    MediaStorageProjection,
    classify_media_payload,
    media_payload_sha256,
    media_storage_advisory_key,
    parse_auto_suggest_wire,
    read_media_payload_limited,
    validate_media_classification,
    validate_media_metadata,
)


class _ByteStream:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.offset = 0

    async def read(self, size: int = -1) -> bytes:
        if self.offset >= len(self.payload):
            return b""
        end = len(self.payload) if size < 0 else self.offset + size
        chunk = self.payload[self.offset : end]
        self.offset += len(chunk)
        return chunk


@pytest.mark.parametrize(
    ("owner_id", "expected"),
    [
        ("00000000-0000-0000-0000-000000000000", -4516021718240404535),
        ("ffffffff-ffff-ffff-ffff-ffffffffffff", -5828949329638390497),
        ("12345678-1234-5678-9abc-def012345678", 5896964795730298223),
    ],
)
def test_advisory_key_has_fixed_sha256_bigint_vectors(
    owner_id: str,
    expected: int,
) -> None:
    assert media_storage_advisory_key(UUID(owner_id)) == expected


def test_metadata_preserves_exact_values_and_defaults_auto_suggest() -> None:
    metadata = validate_media_metadata(
        display_name="  Nome autoral  ",
        original_filename=" mídia final.png ",
        usage_trigger="quando a pessoa pedir",
    )
    assert metadata.display_name == "  Nome autoral  "
    assert metadata.original_filename == " mídia final.png "
    assert metadata.usage_trigger == "quando a pessoa pedir"
    assert metadata.auto_suggest is False


@pytest.mark.parametrize(
    ("wire", "expected"),
    [(b"true", True), ("true", True), (b"false", False), ("false", False)],
)
def test_auto_suggest_accepts_only_lowercase_ascii(
    wire: bytes | str,
    expected: bool,
) -> None:
    assert parse_auto_suggest_wire(wire) is expected


@pytest.mark.parametrize(
    "wire",
    [b"TRUE", "False", b"1", "0", b"", "", True, False, b" true"],
)
def test_auto_suggest_rejects_every_other_wire_value(wire: object) -> None:
    with pytest.raises(MediaAssetDomainError, match="media_metadata_invalid"):
        parse_auto_suggest_wire(wire)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"display_name": ""},
        {"display_name": "x" * 201},
        {"original_filename": ""},
        {"original_filename": "x" * 256},
        {"usage_trigger": "x" * 501},
        {"display_name": "nul\x00name"},
        {"original_filename": "line\rbreak"},
        {"usage_trigger": "line\nbreak"},
    ],
)
def test_metadata_rejects_lengths_and_control_characters(
    overrides: dict[str, str],
) -> None:
    values = {
        "display_name": "asset",
        "original_filename": "asset.pdf",
        "usage_trigger": None,
    }
    values.update(overrides)
    with pytest.raises(MediaAssetDomainError, match="media_metadata_invalid"):
        validate_media_metadata(**values)  # type: ignore[arg-type]


def test_metadata_accepts_exact_length_boundaries() -> None:
    metadata = validate_media_metadata(
        display_name="d" * 200,
        original_filename="f" * 255,
        usage_trigger="u" * 500,
        auto_suggest=b"true",
    )
    assert len(metadata.display_name) == 200
    assert len(metadata.original_filename) == 255
    assert len(metadata.usage_trigger or "") == 500
    assert metadata.auto_suggest is True


@pytest.mark.parametrize(
    ("payload", "category", "mime_type"),
    [
        (b"%PDF-1.7\nbody", "pdf", "application/pdf"),
        (b"\x89PNG\r\n\x1a\nbody", "image", "image/png"),
        (b"\xff\xd8\xff\xe0jpeg", "image", "image/jpeg"),
        (b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64", "audio", "audio/mpeg"),
        (b"\xff\xfb\x90\x64mp3", "audio", "audio/mpeg"),
        (b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isommp42", "video", "video/mp4"),
    ],
)
def test_classification_derives_each_canonical_pair(
    payload: bytes,
    category: str,
    mime_type: str,
) -> None:
    classification = classify_media_payload(payload)
    assert (classification.category, classification.mime_type) == (
        category,
        mime_type,
    )
    assert validate_media_classification(payload, mime_type) == classification
    assert len(media_payload_sha256(payload)) == 32


def test_classification_rejects_unknown_signature_unsupported_and_mismatch() -> None:
    with pytest.raises(MediaAssetDomainError, match="media_signature_invalid"):
        classify_media_payload(b"not-media")
    with pytest.raises(MediaAssetDomainError, match="media_type_unsupported"):
        validate_media_classification(b"%PDF-1.7", "text/plain")
    with pytest.raises(MediaAssetDomainError, match="media_type_mismatch"):
        validate_media_classification(b"%PDF-1.7", "image/png")


@pytest.mark.parametrize(
    "payload",
    [
        b"ID3",
        b"ID3\x04\x00\x00\x00\x00\x00\x00",
        b"ID3\x04\x00\x00\x00\x00\x00\x05short",
        b"ID3\x04\x00\x00\x80\x00\x00\x00\xff\xfb\x90\x64",
        b"\xff\xff\x90\x64layer-i",
        b"\xff\xfd\x90\x64layer-ii",
        b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xff\x90\x64",
        b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfd\x90\x64",
        b"\x00\x00\x00\x0cftypisom",
        b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heicmif1",
        b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00avifmif1",
        b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00M4A isom",
        b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  ",
        b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomheic",
    ],
)
def test_classification_rejects_truncated_or_non_mp4_container_signatures(
    payload: bytes,
) -> None:
    with pytest.raises(MediaAssetDomainError, match="media_signature_invalid"):
        classify_media_payload(payload)


@pytest.mark.asyncio
async def test_bounded_reader_stops_at_limit_plus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media_assets, "MEDIA_FILE_MAX_BYTES", 4)
    monkeypatch.setattr(media_assets, "MEDIA_READ_CHUNK_BYTES", 2)
    assert await read_media_payload_limited(_ByteStream(b"1234")) == b"1234"
    with pytest.raises(MediaAssetDomainError, match="media_size_exceeded"):
        await read_media_payload_limited(_ByteStream(b"12345"))


_GUARD_BUILDER_ID = UUID("00000000-0000-4000-8000-0000000000b1")
_GUARD_TENANT_ID = UUID("00000000-0000-4000-8000-0000000000c1")
_GUARD_ACTOR_ID = UUID("00000000-0000-4000-8000-0000000000a1")


class _GuardResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value

    def scalar_one(self) -> object:
        return self.value

    def one_or_none(self) -> object:
        return self.value


class _GuardSession:
    """Statement-recording session that answers the guard's four lock queries."""

    def __init__(
        self,
        *,
        actor_exists: bool = True,
        builder_exists: bool = True,
        tenant_exists: bool = True,
        current_builder_id: UUID | None = _GUARD_BUILDER_ID,
        used_bytes: int = 0,
    ) -> None:
        self.actor_exists = actor_exists
        self.builder_exists = builder_exists
        self.tenant_exists = tenant_exists
        self.current_builder_id = current_builder_id
        self.used_bytes = used_bytes
        self.statements: list[str] = []
        self.parameters: list[dict[str, Any]] = []
        self.rollbacks = 0
        self.new: list[object] = []
        self.no_autoflush = nullcontext()

    async def execute(self, statement: Any) -> _GuardResult:
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = " ".join(str(compiled).lower().split())
        self.statements.append(sql)
        self.parameters.append(dict(compiled.params))
        if "pg_advisory_xact_lock" in sql:
            return _GuardResult(None)
        if "sum(media_assets.size_bytes)" in sql:
            return _GuardResult(self.used_bytes)
        if "from users" in sql:
            return _GuardResult(_GUARD_ACTOR_ID if self.actor_exists else None)
        if "from builders" in sql:
            return _GuardResult(_GUARD_BUILDER_ID if self.builder_exists else None)
        if "from tenants" in sql:
            if not self.tenant_exists:
                return _GuardResult(None)
            return _GuardResult(
                SimpleNamespace(
                    id=_GUARD_TENANT_ID,
                    builder_id=self.current_builder_id,
                )
            )
        raise AssertionError(f"unexpected statement: {sql}")

    async def rollback(self) -> None:
        self.rollbacks += 1

    def index(self, fragment: str) -> int:
        return next(
            position for position, sql in enumerate(self.statements) if fragment in sql
        )


def _guard_tenant(builder_id: UUID | None = _GUARD_BUILDER_ID) -> Tenant:
    return Tenant(id=_GUARD_TENANT_ID, name="Guard Clinic", builder_id=builder_id)


def _guard(
    session: _GuardSession,
    *,
    actor: UUID | None = _GUARD_ACTOR_ID,
    tenant: Tenant | None = None,
) -> MediaStorageCapacityGuard:
    return MediaStorageCapacityGuard(
        session,  # type: ignore[arg-type]
        actor,
        tenant if tenant is not None else _guard_tenant(),
    )


@pytest.mark.asyncio
async def test_guard_locks_user_builder_advisory_tenant_before_any_media_query() -> (
    None
):
    session = _GuardSession(used_bytes=40)
    guard = _guard(session)

    async with guard:
        entered = list(session.statements)
        projection = await guard.validate_projection(2)

    assert len(entered) == 4
    assert "from users" in entered[0] and "for key share" in entered[0]
    assert "from builders" in entered[1] and "for update" in entered[1]
    assert "pg_advisory_xact_lock" in entered[2]
    assert "from tenants" in entered[3] and "for update" in entered[3]
    assert not any("media_assets" in sql for sql in entered)
    expected_key = media_storage_advisory_key(_GUARD_BUILDER_ID)
    assert guard.owner_id == _GUARD_BUILDER_ID
    assert guard.advisory_key == expected_key
    assert expected_key in session.parameters[2].values()
    assert session.statements[4:] == [
        sql for sql in session.statements if "sum(media_assets.size_bytes)" in sql
    ]
    assert projection == MediaStorageProjection(
        owner_id=_GUARD_BUILDER_ID,
        used_bytes=40,
        additional_bytes=2,
        projected_bytes=42,
        cap_bytes=500_000_000,
    )
    assert session.rollbacks == 0


@pytest.mark.asyncio
async def test_userless_standalone_guard_skips_user_and_builder_locks() -> None:
    session = _GuardSession(current_builder_id=None)
    guard = _guard(session, actor=None, tenant=_guard_tenant(builder_id=None))

    async with guard:
        await guard.validate_projection(0)

    assert guard.owner_id == _GUARD_TENANT_ID
    assert "pg_advisory_xact_lock" in session.statements[0]
    assert "from tenants" in session.statements[1]
    assert media_storage_advisory_key(_GUARD_TENANT_ID) in (
        session.parameters[0].values()
    )
    assert not any(
        "from users" in sql or "from builders" in sql for sql in session.statements
    )


@pytest.mark.asyncio
async def test_guard_enforces_exact_500mb_cap_boundary() -> None:
    assert MEDIA_STORAGE_CAP_BYTES == 500_000_000
    at_cap = _GuardSession(used_bytes=499_999_990)
    guard = _guard(at_cap)
    async with guard:
        projection = await guard.validate_projection(10)
    assert projection.projected_bytes == MEDIA_STORAGE_CAP_BYTES

    above_cap = _GuardSession(used_bytes=499_999_990)
    with pytest.raises(MediaStorageCapacityExceeded) as exceeded:
        async with _guard(above_cap) as guard:
            await guard.validate_projection(11)
    assert exceeded.value.code == "media_storage_cap_exceeded"
    assert above_cap.rollbacks == 1


@pytest.mark.asyncio
async def test_validation_before_or_after_the_context_is_rejected() -> None:
    session = _GuardSession()
    guard = _guard(session)

    with pytest.raises(MediaStorageGuardError, match="not active"):
        await guard.validate_projection(1)
    assert session.statements == []

    async with guard:
        await guard.validate_projection(1)
    measured = len(session.statements)
    with pytest.raises(MediaStorageGuardError, match="not active"):
        await guard.validate_projection(1)
    assert len(session.statements) == measured


@pytest.mark.asyncio
@pytest.mark.parametrize("second_size", [1, 2])
async def test_second_validation_is_rejected_under_the_same_lock(
    second_size: int,
) -> None:
    session = _GuardSession()
    async with _guard(session) as guard:
        await guard.validate_projection(1)
        with pytest.raises(MediaStorageGuardError, match="already validated"):
            await guard.validate_projection(second_size)
    assert sum("sum(media_assets.size_bytes)" in sql for sql in session.statements) == 1


@pytest.mark.asyncio
async def test_validation_after_cap_failure_cannot_be_retried() -> None:
    session = _GuardSession(used_bytes=MEDIA_STORAGE_CAP_BYTES)
    with pytest.raises(MediaStorageGuardError, match="not approved"):
        async with _guard(session) as guard:
            with pytest.raises(MediaStorageCapacityExceeded):
                await guard.validate_projection(1)
            with pytest.raises(MediaStorageGuardError, match="already validated"):
                await guard.validate_projection(0)
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_guard_cannot_be_reentered() -> None:
    session = _GuardSession()
    guard = _guard(session)
    async with guard:
        await guard.validate_projection(0)
    with pytest.raises(MediaStorageGuardError, match="re-entered"):
        await guard.__aenter__()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("size", "error"),
    [(True, TypeError), (1.5, TypeError), ("1", TypeError), (-1, ValueError)],
)
async def test_invalid_additional_bytes_never_measure_usage(
    size: object,
    error: type[Exception],
) -> None:
    session = _GuardSession()
    async with _guard(session) as guard:
        with pytest.raises(error):
            await guard.validate_projection(size)  # type: ignore[arg-type]
        assert not any("media_assets" in sql for sql in session.statements)
        await guard.validate_projection(0)


@pytest.mark.asyncio
async def test_pending_media_insert_before_validation_is_rejected() -> None:
    session = _GuardSession()
    with pytest.raises(MediaStorageGuardError, match="cannot precede"):
        async with _guard(session) as guard:
            session.new.append(MediaAsset(tenant_id=_GUARD_TENANT_ID))
            await guard.validate_projection(1)
    assert not any("media_assets" in sql for sql in session.statements)
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_exit_without_approved_projection_rolls_back() -> None:
    session = _GuardSession()
    with pytest.raises(MediaStorageGuardError, match="not approved"):
        async with _guard(session):
            pass
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_caller_failure_inside_guard_rolls_back_and_propagates() -> None:
    session = _GuardSession()
    with pytest.raises(RuntimeError, match="planted"):
        async with _guard(session) as guard:
            await guard.validate_projection(1)
            raise RuntimeError("planted failure after projection")
    assert session.rollbacks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_kwargs", "message"),
    [
        ({"actor_exists": False}, "actor user no longer exists"),
        ({"builder_exists": False}, "storage owner no longer exists"),
        ({"tenant_exists": False}, "subject tenant no longer exists"),
        ({"current_builder_id": None}, "owner changed"),
        ({"current_builder_id": UUID(int=7)}, "owner changed"),
    ],
)
async def test_lock_phase_failure_or_divergent_owner_rolls_back_before_media(
    session_kwargs: dict[str, Any],
    message: str,
) -> None:
    session = _GuardSession(**session_kwargs)
    guard = _guard(session)
    with pytest.raises(MediaStorageGuardError, match=message):
        async with guard:
            raise AssertionError("unreachable")
    assert session.rollbacks == 1
    assert not any("media_assets" in sql for sql in session.statements)
    with pytest.raises(MediaStorageGuardError, match="not active"):
        await guard.validate_projection(0)
