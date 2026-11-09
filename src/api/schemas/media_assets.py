"""Schemas shared by Media Assets API operations."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from src.services.media_assets import (
    MEDIA_BINDABLE_INTENT_SEQUENCE,
    MEDIA_CANONICAL_MIME_TYPES,
    MEDIA_CATEGORY_MAX_BYTES,
    MEDIA_TRIGGER_INTENTS_MAX_ITEMS,
)

# The published shape of ``trigger_intents``: the 12 bindable intents, at most
# one entry each.  Documentation only on requests: an unknown name is the
# route's stable 422 ``media_trigger_intent_unknown``, never a framework error.
TRIGGER_INTENTS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string", "enum": list(MEDIA_BINDABLE_INTENT_SEQUENCE)},
    "maxItems": MEDIA_TRIGGER_INTENTS_MAX_ITEMS,
}


class MediaAssetResponse(BaseModel):
    """Metadata returned after a media asset is persisted."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    display_name: str = Field(min_length=1, max_length=200)
    original_filename: str = Field(min_length=1, max_length=255)
    category: Literal["video", "audio", "pdf", "image"]
    mime_type: Literal[
        "video/mp4",
        "audio/mpeg",
        "audio/ogg",
        "audio/mp4",
        "application/pdf",
        "image/jpeg",
        "image/png",
    ]
    size_bytes: int = Field(gt=0, le=50_000_000)
    usage_trigger: str | None = Field(default=None, max_length=500)
    auto_suggest: bool
    trigger_intents: list[str] = Field(
        description=(
            "Intents whose turns may send this file, in the canonical order of "
            "GET /v1/media-assets/bindable-intents; empty when none."
        ),
    )
    created_at: datetime
    updated_at: datetime


class MediaAssetListResponse(BaseModel):
    """One keyset page of tenant-scoped media metadata."""

    items: list[MediaAssetResponse]
    next_cursor: str | None
    has_more: bool


class MediaAssetPatchRequest(BaseModel):
    """Author-controlled metadata accepted by the PATCH endpoint."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "additionalProperties": False,
            "minProperties": 1,
            "properties": {
                "display_name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                },
                "original_filename": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 255,
                },
                "usage_trigger": {
                    "anyOf": [
                        {"type": "string", "maxLength": 500},
                        {"type": "null"},
                    ]
                },
                "auto_suggest": {"type": "boolean"},
                "trigger_intents": TRIGGER_INTENTS_SCHEMA,
            },
        },
    )

    display_name: str | None = None
    original_filename: str | None = None
    usage_trigger: str | None = None
    auto_suggest: bool | None = None
    # The names are checked by the route (media_trigger_intent_unknown), which
    # also removes duplicates and stores the canonical order; [] unbinds all.
    trigger_intents: list[str] | None = None

    @model_validator(mode="after")
    def require_at_least_one_field(self) -> "MediaAssetPatchRequest":
        """Reject a no-op body while retaining explicit ``usage_trigger=null``."""
        if not self.model_fields_set:
            raise ValueError("at least one media metadata field is required")
        return self


class MediaCategoryByteLimits(BaseModel):
    """Largest payload of each category, in bytes (upload and agent send)."""

    model_config = ConfigDict(extra="forbid")

    video: int = Field(gt=0)
    audio: int = Field(gt=0)
    pdf: int = Field(gt=0)
    image: int = Field(gt=0)


class MediaBindableIntentsResponse(BaseModel):
    """What the Central de Mídias may bind a file to, and whether it is sent."""

    intents: list[str] = Field(
        description=(
            "Every intent a media asset may be bound to, in canonical order. "
            "Scheduling intents and fora_do_horario are never bindable."
        ),
        json_schema_extra={
            "items": {
                "type": "string",
                "enum": list(MEDIA_BINDABLE_INTENT_SEQUENCE),
            }
        },
    )
    delivery_enabled: bool = Field(
        description=(
            "True only while the agent media send flag is effective and the "
            "clinic's WhatsApp provider authenticates its webhook (its turns "
            "are production turns): Meta with its app secret, or Z-API with "
            "its inbound token."
        ),
    )
    max_bytes_by_category: MediaCategoryByteLimits
    sendable_mime_types: list[str] = Field(
        description=(
            "The canonical MIME types the clinic's WhatsApp provider sends as "
            "agent media, sorted; empty when the clinic has no provider that "
            "sends media. A stored file outside it is never sent by the agent."
        ),
        json_schema_extra={
            "items": {
                "type": "string",
                "enum": [mime for mime in sorted(MEDIA_CANONICAL_MIME_TYPES)],
            }
        },
    )


def media_category_byte_limits() -> MediaCategoryByteLimits:
    """The published limits, read from ``MEDIA_CATEGORY_MAX_BYTES`` only."""

    return MediaCategoryByteLimits(**MEDIA_CATEGORY_MAX_BYTES)


class MediaAssetErrorDetail(BaseModel):
    """Stable machine-readable media error detail."""

    code: str = Field(examples=["media_asset_not_found"])


class MediaAssetErrorResponse(BaseModel):
    """Error envelope returned by Media Assets domain operations."""

    detail: MediaAssetErrorDetail


class MediaAssetAuthErrorResponse(BaseModel):
    """Authentication middleware error envelope."""

    error: Literal["unauthorized", "forbidden"]
    message: str
    documentation_url: str | None = None


class MediaAssetForbiddenResponse(
    RootModel[MediaAssetErrorResponse | MediaAssetAuthErrorResponse]
):
    """Either authorization-layer or tenant-policy rejection."""


class MediaAssetValidationIssue(BaseModel):
    """FastAPI validation issue represented in the public error contract."""

    loc: list[str | int]
    msg: str
    type: str


class MediaAssetRequestValidationError(BaseModel):
    """Framework validation envelope for path, query or JSON inputs."""

    detail: list[MediaAssetValidationIssue]


class MediaAssetValidationErrorResponse(
    RootModel[MediaAssetErrorResponse | MediaAssetRequestValidationError]
):
    """Domain or framework validation error returned with HTTP 422."""
