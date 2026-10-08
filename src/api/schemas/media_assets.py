"""Schemas shared by Media Assets API operations."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator


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
        "application/pdf",
        "image/jpeg",
        "image/png",
    ]
    size_bytes: int = Field(gt=0, le=50_000_000)
    usage_trigger: str | None = Field(default=None, max_length=500)
    auto_suggest: bool
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
            },
        },
    )

    display_name: str | None = None
    original_filename: str | None = None
    usage_trigger: str | None = None
    auto_suggest: bool | None = None

    @model_validator(mode="after")
    def require_at_least_one_field(self) -> "MediaAssetPatchRequest":
        """Reject a no-op body while retaining explicit ``usage_trigger=null``."""
        if not self.model_fields_set:
            raise ValueError("at least one media metadata field is required")
        return self


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
