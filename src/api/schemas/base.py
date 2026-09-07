"""
NeuroStack API - Base Schemas
Foundation classes for all API schemas.
"""

from datetime import datetime
from enum import Enum
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class TimestampMixin(BaseModel):
    """Mixin for timestamp fields."""

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="Timestamp of creation (ISO 8601)",
        json_schema_extra={"example": "2025-12-31T10:30:00Z"},
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="Timestamp of last update (ISO 8601)",
        json_schema_extra={"example": "2025-12-31T10:30:00Z"},
    )


class BaseResponse(BaseModel):
    """Base response model with common fields."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "success": True,
                "message": "Operation completed successfully",
            }
        }
    )

    success: bool = Field(
        default=True, description="Whether the operation was successful"
    )
    message: Optional[str] = Field(
        default=None, description="Optional message about the operation"
    )


class ErrorCode(str, Enum):
    """Standardized error codes for the API."""

    # Client Errors (4xx)
    BAD_REQUEST = "bad_request"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    CONFLICT = "conflict"
    UNPROCESSABLE_ENTITY = "unprocessable_entity"
    RATE_LIMITED = "rate_limited"
    PAYLOAD_TOO_LARGE = "payload_too_large"

    # Server Errors (5xx)
    INTERNAL_ERROR = "internal_server_error"
    NOT_IMPLEMENTED = "not_implemented"
    SERVICE_UNAVAILABLE = "service_unavailable"
    GATEWAY_TIMEOUT = "gateway_timeout"

    # Domain Errors
    TENANT_NOT_FOUND = "tenant_not_found"
    SESSION_NOT_FOUND = "session_not_found"
    SESSION_EXPIRED = "session_expired"
    POLICY_NOT_FOUND = "policy_not_found"
    POLICY_CONFLICT = "policy_conflict"
    INVALID_EVENT_TYPE = "invalid_event_type"
    SAFETY_VIOLATION = "safety_violation"
    CONTEXT_OVERFLOW = "context_overflow"


class ErrorDetail(BaseModel):
    """Detailed error information for a specific field or issue."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "field": "payload.content",
                "message": "Content cannot be empty",
                "code": "value_error.missing",
            }
        }
    )

    field: Optional[str] = Field(
        default=None, description="Field that caused the error"
    )
    message: str = Field(..., description="Human-readable error message")
    code: Optional[str] = Field(
        default=None, description="Error code for programmatic handling"
    )


class ErrorResponse(BaseModel):
    """
    Standard error response format.

    All API errors follow this format for consistent error handling.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error": "validation_error",
                "message": "Request validation failed",
                "details": [
                    {
                        "field": "payload.content",
                        "message": "Content is required",
                        "code": "value_error.missing",
                    }
                ],
                "request_id": "req_abc123xyz",
                "documentation_url": "https://docs.neurostack.ai/errors/validation_error",
            }
        }
    )

    error: str = Field(
        ...,
        description="Error code for programmatic handling",
        json_schema_extra={"example": "validation_error"},
    )
    message: str = Field(
        ...,
        description="Human-readable error message",
        json_schema_extra={"example": "Request validation failed"},
    )
    details: Optional[list[ErrorDetail]] = Field(
        default=None,
        description="Detailed error information for each field/issue",
    )
    request_id: Optional[str] = Field(
        default=None,
        description="Unique request identifier for debugging",
        json_schema_extra={"example": "req_abc123xyz"},
    )
    documentation_url: Optional[str] = Field(
        default=None,
        description="URL to documentation about this error",
    )


class PaginationParams(BaseModel):
    """Pagination parameters for list endpoints."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "page": 1,
                "page_size": 20,
            }
        }
    )

    page: int = Field(default=1, ge=1, description="Page number (1-indexed)")
    page_size: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Number of items per page (max 100)",
    )


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated response wrapper."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "items": [],
                "total": 100,
                "page": 1,
                "page_size": 20,
                "total_pages": 5,
                "has_next": True,
                "has_previous": False,
            }
        }
    )

    items: list[T] = Field(..., description="List of items for the current page")
    total: int = Field(..., ge=0, description="Total number of items")
    page: int = Field(..., ge=1, description="Current page number")
    page_size: int = Field(..., ge=1, description="Number of items per page")
    total_pages: int = Field(..., ge=0, description="Total number of pages")
    has_next: bool = Field(..., description="Whether there is a next page")
    has_previous: bool = Field(..., description="Whether there is a previous page")

    @classmethod
    def create(
        cls,
        items: list[T],
        total: int,
        page: int,
        page_size: int,
    ) -> "PaginatedResponse[T]":
        """Create a paginated response from items and pagination info."""
        total_pages = (total + page_size - 1) // page_size if total > 0 else 0
        return cls(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            has_next=page < total_pages,
            has_previous=page > 1,
        )
