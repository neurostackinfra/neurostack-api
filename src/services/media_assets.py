"""Canonical validation and capacity primitives for tenant media assets."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import AsyncContextManager, Protocol
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from src.models.builder import Builder
from src.models.media_asset import MediaAsset
from src.models.tenant import Tenant
from src.models.user import User

MEDIA_FILE_MAX_BYTES = 50_000_000
MEDIA_STORAGE_CAP_BYTES = 500_000_000
MEDIA_READ_CHUNK_BYTES = 1024 * 1024
_ADVISORY_KEY_PREFIX = b"media-storage-owner:v1\0"
_MP4_BRANDS = frozenset(
    {
        b"avc1",
        b"dash",
        b"iso2",
        b"iso3",
        b"iso4",
        b"iso5",
        b"iso6",
        b"isom",
        b"mp41",
        b"mp42",
    }
)


class AsyncReadable(Protocol):
    """Smallest stream interface needed by the bounded media reader."""

    async def read(self, size: int = -1) -> bytes: ...


class MediaAssetDomainError(ValueError):
    """Stable domain failure that an API layer can map without leaking data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class MediaStorageGuardError(RuntimeError):
    """Capacity guard lifecycle or ownership failure."""


class MediaStorageCapacityExceeded(MediaStorageGuardError):
    """The protected owner would exceed the operational storage cap."""

    code = "media_storage_cap_exceeded"


@dataclass(frozen=True)
class MediaOwnerTransition:
    """One expected Tenant.builder_id change protected by media-owner locks."""

    tenant_id: UUID
    expected_builder_id: UUID | None
    destination_builder_id: UUID | None


@dataclass(frozen=True)
class MediaClassification:
    """Server-derived category and canonical MIME pair."""

    category: str
    mime_type: str


@dataclass(frozen=True)
class MediaMetadata:
    """Validated author-controlled metadata, preserved byte-for-byte as text."""

    display_name: str
    original_filename: str
    usage_trigger: str | None
    auto_suggest: bool


@dataclass(frozen=True)
class MediaStorageProjection:
    """Result of the single protected usage/projection calculation."""

    owner_id: UUID
    used_bytes: int
    additional_bytes: int
    projected_bytes: int
    cap_bytes: int = MEDIA_STORAGE_CAP_BYTES


def media_storage_advisory_key(owner_id: UUID) -> int:
    """Derive the exact signed PostgreSQL bigint key for a storage owner."""
    digest = hashlib.sha256(_ADVISORY_KEY_PREFIX + owner_id.bytes).digest()
    unsigned = int.from_bytes(digest[:8], byteorder="big", signed=False)
    return unsigned - (1 << 64) if unsigned >= (1 << 63) else unsigned


def _media_storage_owner_id(tenant_id: UUID, builder_id: UUID | None) -> UUID:
    return builder_id if builder_id is not None else tenant_id


def _media_usage_statement(
    *,
    builder_id: UUID | None,
    tenant_id: UUID,
) -> Select[tuple[int]]:
    if builder_id is None:
        return select(func.coalesce(func.sum(MediaAsset.size_bytes), 0)).where(
            MediaAsset.tenant_id == tenant_id
        )
    return (
        select(func.coalesce(func.sum(MediaAsset.size_bytes), 0))
        .select_from(MediaAsset)
        .join(Tenant, Tenant.id == MediaAsset.tenant_id)
        .where(Tenant.builder_id == builder_id)
    )


async def _media_storage_used_bytes(
    session: AsyncSession,
    *,
    builder_id: UUID | None,
    tenant_id: UUID,
) -> int:
    with session.no_autoflush:
        return int(
            (
                await session.execute(
                    _media_usage_statement(
                        builder_id=builder_id,
                        tenant_id=tenant_id,
                    )
                )
            ).scalar_one()
        )


async def _lock_transition_users(
    session: AsyncSession,
    *,
    actor_user_id: UUID | None,
    builder_ids: frozenset[UUID],
    tenant_ids: frozenset[UUID] = frozenset(),
) -> None:
    predicates = []
    if actor_user_id is not None:
        predicates.append(User.id == actor_user_id)
    if builder_ids:
        predicates.append(User.builder_id.in_(builder_ids))
    if tenant_ids:
        predicates.append(User.tenant_id.in_(tenant_ids))
    if not predicates:
        return
    await session.execute(
        select(User.id).where(or_(*predicates)).order_by(User.id).with_for_update()
    )


async def _lock_transition_builders(
    session: AsyncSession,
    builder_ids: frozenset[UUID],
) -> None:
    if not builder_ids:
        return
    locked_ids = frozenset(
        (
            await session.execute(
                select(Builder.id)
                .where(Builder.id.in_(builder_ids))
                .order_by(Builder.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if locked_ids != builder_ids:
        raise MediaStorageGuardError("storage owner no longer exists")


async def _lock_transition_advisories(
    session: AsyncSession,
    owner_ids: frozenset[UUID],
) -> None:
    connection = await session.connection()
    if connection.dialect.name != "postgresql":
        return
    keys = sorted(media_storage_advisory_key(owner_id) for owner_id in owner_ids)
    for key in keys:
        await session.execute(select(func.pg_advisory_xact_lock(key)))


async def _apply_media_owner_transitions(
    session: AsyncSession,
    transitions: tuple[MediaOwnerTransition, ...],
    *,
    actor_user_id: UUID | None,
    resources_prelocked: bool = False,
) -> None:
    if not transitions:
        return
    by_tenant = {transition.tenant_id: transition for transition in transitions}
    if len(by_tenant) != len(transitions):
        raise MediaStorageGuardError("duplicate tenant owner transition")
    if any(
        transition.expected_builder_id == transition.destination_builder_id
        for transition in transitions
    ):
        raise MediaStorageGuardError("storage owner transition must change owner")

    builder_ids = frozenset(
        builder_id
        for transition in transitions
        for builder_id in (
            transition.expected_builder_id,
            transition.destination_builder_id,
        )
        if builder_id is not None
    )
    owner_ids = frozenset(
        owner_id
        for transition in transitions
        for owner_id in (
            _media_storage_owner_id(
                transition.tenant_id,
                transition.expected_builder_id,
            ),
            _media_storage_owner_id(
                transition.tenant_id,
                transition.destination_builder_id,
            ),
        )
    )
    if not resources_prelocked:
        await _lock_transition_users(
            session,
            actor_user_id=actor_user_id,
            builder_ids=builder_ids,
        )
        await _lock_transition_builders(session, builder_ids)
    await _lock_transition_advisories(session, owner_ids)

    rows = (
        await session.execute(
            select(Tenant.id, Tenant.builder_id)
            .where(Tenant.id.in_(by_tenant))
            .order_by(Tenant.id)
            .with_for_update()
        )
    ).all()
    if len(rows) != len(transitions):
        raise MediaStorageGuardError("subject tenant no longer exists")
    for row in rows:
        if row.builder_id != by_tenant[row.id].expected_builder_id:
            raise MediaStorageGuardError("storage owner changed during locking")

    for transition in sorted(transitions, key=lambda item: item.tenant_id.int):
        if transition.destination_builder_id is None:
            projected_bytes = await _media_storage_used_bytes(
                session,
                builder_id=None,
                tenant_id=transition.tenant_id,
            )
        else:
            destination_used = await _media_storage_used_bytes(
                session,
                builder_id=transition.destination_builder_id,
                tenant_id=transition.tenant_id,
            )
            moving_bytes = await _media_storage_used_bytes(
                session,
                builder_id=None,
                tenant_id=transition.tenant_id,
            )
            projected_bytes = destination_used + moving_bytes
        if projected_bytes > MEDIA_STORAGE_CAP_BYTES:
            raise MediaStorageCapacityExceeded(
                "media storage projection exceeds the operational cap"
            )

    for row in rows:
        tenant = await session.get(Tenant, row.id)
        assert tenant is not None
        tenant.builder_id = by_tenant[row.id].destination_builder_id


async def transition_tenant_media_owner(
    session: AsyncSession,
    transition: MediaOwnerTransition,
    *,
    actor_user_id: UUID | None = None,
) -> None:
    """Move one tenant between media owners under the canonical lock order."""
    await _apply_media_owner_transitions(
        session,
        (transition,),
        actor_user_id=actor_user_id,
    )


async def detach_builder_media_owners_for_delete(
    session: AsyncSession,
    builder_id: UUID,
    *,
    actor_user_id: UUID | None = None,
) -> tuple[UUID, ...]:
    """Detach every tenant after freezing a builder's complete ownership set."""
    builder_ids = frozenset((builder_id,))
    await _lock_transition_users(
        session,
        actor_user_id=actor_user_id,
        builder_ids=builder_ids,
    )
    await _lock_transition_builders(session, builder_ids)
    tenant_ids = tuple(
        (
            await session.execute(
                select(Tenant.id)
                .where(Tenant.builder_id == builder_id)
                .order_by(Tenant.id)
            )
        )
        .scalars()
        .all()
    )
    await _apply_media_owner_transitions(
        session,
        tuple(
            MediaOwnerTransition(
                tenant_id=tenant_id,
                expected_builder_id=builder_id,
                destination_builder_id=None,
            )
            for tenant_id in tenant_ids
        ),
        actor_user_id=actor_user_id,
        resources_prelocked=True,
    )
    return tenant_ids


async def lock_clinic_for_delete(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    actor_user_id: UUID | None = None,
) -> Tenant | None:
    """Lock affected users and the media owner before a tenant delete."""
    snapshot = (
        await session.execute(
            select(Tenant.id, Tenant.builder_id).where(Tenant.id == tenant_id)
        )
    ).one_or_none()
    if snapshot is None:
        return None
    builder_ids = (
        frozenset((snapshot.builder_id,))
        if snapshot.builder_id is not None
        else frozenset()
    )
    await _lock_transition_users(
        session,
        actor_user_id=actor_user_id,
        builder_ids=builder_ids,
        tenant_ids=frozenset((tenant_id,)),
    )
    await _lock_transition_builders(session, builder_ids)
    await _lock_transition_advisories(
        session,
        frozenset((_media_storage_owner_id(tenant_id, snapshot.builder_id),)),
    )
    tenant = (
        await session.execute(
            select(Tenant).where(Tenant.id == tenant_id).with_for_update()
        )
    ).scalar_one_or_none()
    if tenant is None:
        return None
    if tenant.builder_id != snapshot.builder_id:
        raise MediaStorageGuardError("storage owner changed during locking")
    return tenant


async def lock_tenant_users_before_parent(
    session: AsyncSession,
    tenant_id: UUID,
) -> Tenant | None:
    """Lock all tenant users in UUID order before its Tenant parent row."""
    await _lock_transition_users(
        session,
        actor_user_id=None,
        builder_ids=frozenset(),
        tenant_ids=frozenset((tenant_id,)),
    )
    return (
        await session.execute(
            select(Tenant).where(Tenant.id == tenant_id).with_for_update()
        )
    ).scalar_one_or_none()


async def lock_builder_users_before_parent(
    session: AsyncSession,
    builder_id: UUID,
) -> Builder | None:
    """Lock all builder users in UUID order before its Builder parent row."""
    builder_ids = frozenset((builder_id,))
    await _lock_transition_users(
        session,
        actor_user_id=None,
        builder_ids=builder_ids,
    )
    locked_ids = frozenset(
        (
            await session.execute(
                select(Builder.id).where(Builder.id == builder_id).with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if not locked_ids:
        return None
    return await session.get(Builder, builder_id)


def _validate_text(
    value: str | None,
    *,
    maximum: int,
    optional: bool,
) -> str | None:
    if value is None:
        if optional:
            return None
        raise MediaAssetDomainError("media_metadata_invalid")
    if not isinstance(value, str):
        raise MediaAssetDomainError("media_metadata_invalid")
    if (not optional and len(value) < 1) or len(value) > maximum:
        raise MediaAssetDomainError("media_metadata_invalid")
    if any(control in value for control in ("\x00", "\r", "\n")):
        raise MediaAssetDomainError("media_metadata_invalid")
    return value


def parse_auto_suggest_wire(value: bytes | str | None) -> bool:
    """Parse only the approved lowercase ASCII multipart wire values."""
    if value is None:
        return False
    if value == b"true" or value == "true":
        return True
    if value == b"false" or value == "false":
        return False
    raise MediaAssetDomainError("media_metadata_invalid")


def validate_media_metadata(
    *,
    display_name: str,
    original_filename: str,
    usage_trigger: str | None = None,
    auto_suggest: bytes | str | None = None,
) -> MediaMetadata:
    """Validate all four catalog metadata fields without trimming or execution."""
    validated_display_name = _validate_text(
        display_name,
        maximum=200,
        optional=False,
    )
    validated_original_filename = _validate_text(
        original_filename,
        maximum=255,
        optional=False,
    )
    validated_usage_trigger = _validate_text(
        usage_trigger,
        maximum=500,
        optional=True,
    )
    assert validated_display_name is not None
    assert validated_original_filename is not None
    return MediaMetadata(
        display_name=validated_display_name,
        original_filename=validated_original_filename,
        usage_trigger=validated_usage_trigger,
        auto_suggest=parse_auto_suggest_wire(auto_suggest),
    )


async def read_media_payload_limited(stream: AsyncReadable) -> bytes:
    """Read at most the approved file limit plus one sentinel byte."""
    chunks: list[bytes] = []
    total = 0
    while True:
        remaining_with_sentinel = MEDIA_FILE_MAX_BYTES + 1 - total
        if remaining_with_sentinel <= 0:
            raise MediaAssetDomainError("media_size_exceeded")
        chunk = await stream.read(min(MEDIA_READ_CHUNK_BYTES, remaining_with_sentinel))
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise TypeError("media stream must return bytes")
        chunks.append(chunk)
        total += len(chunk)
        if total > MEDIA_FILE_MAX_BYTES:
            raise MediaAssetDomainError("media_size_exceeded")
    return b"".join(chunks)


def media_payload_sha256(payload: bytes) -> bytes:
    """Return the binary SHA-256 stored by the relational model."""
    return hashlib.sha256(payload).digest()


def _looks_like_mpeg_audio(payload: bytes) -> bool:
    if payload.startswith(b"ID3"):
        if len(payload) < 14 or payload[3] == 0xFF or payload[4] == 0xFF:
            return False
        size_bytes = payload[6:10]
        if any(value & 0x80 for value in size_bytes):
            return False
        tag_size = sum(
            value << shift for value, shift in zip(size_bytes, (21, 14, 7, 0))
        )
        frame_offset = 10 + tag_size
        if payload[5] & 0x10:
            frame_offset += 10
        if frame_offset + 4 > len(payload):
            return False
        payload = payload[frame_offset:]
    if len(payload) < 4 or payload[0] != 0xFF or payload[1] & 0xE0 != 0xE0:
        return False
    version = (payload[1] >> 3) & 0x03
    layer = (payload[1] >> 1) & 0x03
    bitrate = (payload[2] >> 4) & 0x0F
    sample_rate = (payload[2] >> 2) & 0x03
    return (
        version != 0x01
        and layer == 0x01
        and bitrate not in (0, 0x0F)
        and sample_rate != 0x03
    )


def _looks_like_mp4(payload: bytes) -> bool:
    if len(payload) < 16 or payload[4:8] != b"ftyp":
        return False
    box_size = int.from_bytes(payload[:4], byteorder="big", signed=False)
    if box_size < 16 or box_size > len(payload) or box_size % 4:
        return False
    brands = (payload[8:12],) + tuple(
        payload[offset : offset + 4] for offset in range(16, box_size, 4)
    )
    return bool(brands) and all(brand in _MP4_BRANDS for brand in brands)


def classify_media_payload(payload: bytes) -> MediaClassification:
    """Classify one of the five approved formats from content signatures."""
    if payload.startswith(b"%PDF-"):
        return MediaClassification("pdf", "application/pdf")
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return MediaClassification("image", "image/png")
    if payload.startswith(b"\xff\xd8\xff"):
        return MediaClassification("image", "image/jpeg")
    if _looks_like_mpeg_audio(payload):
        return MediaClassification("audio", "audio/mpeg")
    if _looks_like_mp4(payload):
        return MediaClassification("video", "video/mp4")
    raise MediaAssetDomainError("media_signature_invalid")


def validate_media_classification(
    payload: bytes,
    declared_mime_type: str,
) -> MediaClassification:
    """Require a supported declared MIME that matches the derived signature."""
    canonical_mimes = {
        "video/mp4",
        "audio/mpeg",
        "application/pdf",
        "image/jpeg",
        "image/png",
    }
    if declared_mime_type not in canonical_mimes:
        raise MediaAssetDomainError("media_type_unsupported")
    classification = classify_media_payload(payload)
    if classification.mime_type != declared_mime_type:
        raise MediaAssetDomainError("media_type_mismatch")
    return classification


def _actor_user_id(actor_user: User | UUID | None) -> UUID | None:
    if actor_user is None:
        return None
    if isinstance(actor_user, UUID):
        return actor_user
    if actor_user.id is None:
        raise MediaStorageGuardError("actor user must be persisted")
    return actor_user.id


class MediaStorageCapacityGuard:
    """Two-phase capacity guard with one canonical cross-replica lock order."""

    def __init__(
        self,
        session: AsyncSession,
        actor_user: User | UUID | None,
        subject_tenant: Tenant,
    ) -> None:
        self._session = session
        self._actor_user_id = _actor_user_id(actor_user)
        self._subject_tenant_id = subject_tenant.id
        self._initial_builder_id = subject_tenant.builder_id
        self._owner_id = _media_storage_owner_id(
            subject_tenant.id,
            subject_tenant.builder_id,
        )
        self._entered = False
        self._exited = False
        self._validation_attempted = False
        self._projection_approved = False

    @property
    def owner_id(self) -> UUID:
        """Resolved owner UUID; available without querying media usage."""
        return self._owner_id

    @property
    def advisory_key(self) -> int:
        """Canonical PostgreSQL advisory key for the resolved owner."""
        return media_storage_advisory_key(self._owner_id)

    async def _rollback(self) -> None:
        await asyncio.shield(self._session.rollback())

    async def __aenter__(self) -> MediaStorageCapacityGuard:
        if self._entered or self._exited:
            raise MediaStorageGuardError("capacity guard cannot be re-entered")
        try:
            if self._actor_user_id is not None:
                actor_result = await self._session.execute(
                    select(User.id)
                    .where(User.id == self._actor_user_id)
                    .with_for_update(read=True, key_share=True)
                )
                if actor_result.scalar_one_or_none() is None:
                    raise MediaStorageGuardError("actor user no longer exists")

            if self._initial_builder_id is not None:
                builder_result = await self._session.execute(
                    select(Builder.id)
                    .where(Builder.id == self._initial_builder_id)
                    .with_for_update()
                )
                if builder_result.scalar_one_or_none() is None:
                    raise MediaStorageGuardError("storage owner no longer exists")

            await self._session.execute(
                select(func.pg_advisory_xact_lock(self.advisory_key))
            )

            tenant_result = await self._session.execute(
                select(Tenant.id, Tenant.builder_id)
                .where(Tenant.id == self._subject_tenant_id)
                .with_for_update()
            )
            tenant_row = tenant_result.one_or_none()
            if tenant_row is None:
                raise MediaStorageGuardError("subject tenant no longer exists")
            current_owner = _media_storage_owner_id(
                tenant_row.id,
                tenant_row.builder_id,
            )
            if (
                tenant_row.builder_id != self._initial_builder_id
                or current_owner != self._owner_id
            ):
                raise MediaStorageGuardError("storage owner changed during locking")

            self._entered = True
            return self
        except BaseException:
            self._exited = True
            await self._rollback()
            raise

    async def validate_projection(
        self,
        additional_bytes: int,
    ) -> MediaStorageProjection:
        """Measure protected use once and reject a projection above the cap."""
        if not self._entered or self._exited:
            raise MediaStorageGuardError("capacity guard is not active")
        if self._validation_attempted:
            raise MediaStorageGuardError("capacity projection was already validated")
        if isinstance(additional_bytes, bool) or not isinstance(additional_bytes, int):
            raise TypeError("additional_bytes must be a non-negative integer")
        if additional_bytes < 0:
            raise ValueError("additional_bytes must be a non-negative integer")
        if any(isinstance(instance, MediaAsset) for instance in self._session.new):
            raise MediaStorageGuardError(
                "media insert cannot precede capacity validation"
            )
        self._validation_attempted = True

        used_bytes = await _media_storage_used_bytes(
            self._session,
            builder_id=self._initial_builder_id,
            tenant_id=self._subject_tenant_id,
        )
        projected_bytes = used_bytes + additional_bytes
        if projected_bytes > MEDIA_STORAGE_CAP_BYTES:
            raise MediaStorageCapacityExceeded(
                "media storage projection exceeds the operational cap"
            )
        self._projection_approved = True
        return MediaStorageProjection(
            owner_id=self._owner_id,
            used_bytes=used_bytes,
            additional_bytes=additional_bytes,
            projected_bytes=projected_bytes,
            cap_bytes=MEDIA_STORAGE_CAP_BYTES,
        )

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> bool:
        self._exited = True
        if exc_type is not None:
            await self._rollback()
            return False
        if not self._projection_approved:
            await self._rollback()
            raise MediaStorageGuardError("capacity projection was not approved")
        return False


def media_storage_capacity_guard(
    session: AsyncSession,
    actor_user: User | UUID | None,
    subject_tenant: Tenant,
) -> AsyncContextManager[MediaStorageCapacityGuard]:
    """Build the only supported owner/lock/usage/cap guard."""
    return MediaStorageCapacityGuard(session, actor_user, subject_tenant)
