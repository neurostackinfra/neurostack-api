"""Secret-safe validation handling for password-only endpoints."""

from __future__ import annotations

from typing import Any

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic_core import PydanticCustomError
from starlette.requests import Request


def password_mismatch_error() -> PydanticCustomError:
    """Build the stable validation error used when password fields differ."""
    return PydanticCustomError("password_mismatch", "passwords do not match")


def _field_name(error: dict[str, Any]) -> str:
    """Return a client-safe field name without ever returning invalid input."""
    location = error.get("loc", ())
    fields = [str(item) for item in location if item not in {"body", "query"}]
    return ".".join(fields) if fields else "body"


def password_validation_response(error: RequestValidationError) -> JSONResponse:
    """Serialize Pydantic errors without their secret-bearing ``input`` values."""
    fields = [
        {"field": _field_name(item), "code": str(item.get("type", "invalid"))}
        for item in error.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "error": "invalid_password_payload",
                "fields": fields,
            }
        },
    )


class PasswordValidationRoute(APIRoute):
    """Route class that restricts secret-safe validation to password endpoints."""

    def get_route_handler(self) -> Any:
        original_handler = super().get_route_handler()

        async def custom_route_handler(request: Request) -> Any:
            try:
                return await original_handler(request)
            except RequestValidationError as error:
                return password_validation_response(error)

        return custom_route_handler
