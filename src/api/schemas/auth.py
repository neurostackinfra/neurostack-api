"""
NeuroStack API - Auth Schemas
Contracts for authentication endpoints (/v1/auth).
"""

from typing import Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    ValidationInfo,
    field_validator,
)

from src.api.helpers.password_validation import password_mismatch_error


class LoginRequest(BaseModel):
    """Request body for POST /v1/auth/login."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "email": "admin@clinic.com",
                "password": "s3cret",
            }
        }
    )

    email: str = Field(
        ...,
        description="User email address",
        json_schema_extra={"example": "admin@clinic.com"},
    )
    password: str = Field(
        ...,
        min_length=1,
        description="User password",
    )


class LoginResponse(BaseModel):
    """Response body for a successful login."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "token": "eyJhbGciOiJIUzI1NiIs...",
                "email": "admin@clinic.com",
                "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "clinic_id": "c3d4e5f6-a7b8-9012-cdef-123456789012",
                "role": "clinic_admin",
            }
        }
    )

    token: str = Field(..., description="JWT access token")
    email: str = Field(..., description="Authenticated user email")
    tenant_id: str = Field(..., description="Tenant UUID")
    clinic_id: Optional[str] = Field(
        default=None, description="Clinic UUID if configured"
    )
    role: str = Field(..., description="User role")


class StagingRehearsalTokenRequest(BaseModel):
    """Strict credentials accepted only by the staging rehearsal issuer."""

    model_config = ConfigDict(extra="forbid", strict=True)

    email: EmailStr = Field(..., max_length=254)
    password: str = Field(..., min_length=1, max_length=1024, repr=False)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().casefold()
        return value


class StagingRehearsalTokenResponse(BaseModel):
    """Short-lived JWT response for a staging producer rehearsal."""

    token: str = Field(..., description="Short-lived staging rehearsal JWT")
    token_type: str = Field(default="Bearer", pattern="^Bearer$")
    expires_in: int = Field(default=600, ge=1, le=600)
    token_use: str = Field(default="asaas_staging_rehearsal")


class PasswordChangeRequest(BaseModel):
    """Strict payload shared by the self-service and delegated password routes."""

    model_config = ConfigDict(extra="forbid")

    new_password: str = Field(..., min_length=6)
    confirm_password: str = Field(..., min_length=6)

    @field_validator("confirm_password")
    @classmethod
    def passwords_match(cls, confirm_password: str, info: ValidationInfo) -> str:
        """Reject a confirmation that does not equal the supplied new password."""
        if (
            "new_password" in info.data
            and confirm_password != info.data["new_password"]
        ):
            raise password_mismatch_error()
        return confirm_password
