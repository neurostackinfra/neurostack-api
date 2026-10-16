"""
NeuroStack API - Auth Endpoint
User login with JWT token generation.
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from passlib.context import CryptContext
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.helpers.password_validation import PasswordValidationRoute
from src.api.middleware.auth import (
    STAGING_REHEARSAL_TOKEN_ENVIRONMENT,
    STAGING_REHEARSAL_TOKEN_SCOPE,
    STAGING_REHEARSAL_TOKEN_TTL_SECONDS,
    STAGING_REHEARSAL_TOKEN_USE,
    TenantContext,
    get_tenant_context,
)
from src.api.schemas.auth import (
    LoginRequest,
    LoginResponse,
    PasswordChangeRequest,
    StagingRehearsalTokenRequest,
    StagingRehearsalTokenResponse,
)
from src.config.settings import settings
from src.models.clinic_config_table import ClinicConfigTable
from src.models.user import User, UserRole
from src.repositories import get_db_session, get_db_session_dependency

router = APIRouter()
password_router = APIRouter(route_class=PasswordValidationRoute)

# Bcrypt context with 12 rounds (passlib default for bcrypt)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)

# JWT settings
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24


@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    summary="User Login",
    description="Authenticate with email and password to obtain a JWT token.",
    responses={
        200: {"description": "Login successful", "model": LoginResponse},
        401: {
            "description": "Invalid credentials",
            "content": {
                "application/json": {"example": {"detail": "Invalid email or password"}}
            },
        },
        403: {
            "description": "User account inactive",
            "content": {
                "application/json": {"example": {"detail": "User account is inactive"}}
            },
        },
    },
    tags=["Auth"],
)
async def login(body: LoginRequest) -> LoginResponse:
    """
    Authenticate a user and return a JWT token.

    - Verifies email exists and password matches bcrypt hash.
    - Returns 401 for invalid credentials.
    - Returns 403 for inactive accounts.
    - JWT contains sub, tenant_id, role, email, iat, exp, jti.
    """
    async with get_db_session() as session:
        # Look up user by email
        stmt = select(User).where(User.email == body.email)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if user is None or not pwd_context.verify(body.password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
            )

        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User account is inactive",
            )

        # Look up clinic_id for the tenant (if exists)
        clinic_id: Optional[str] = None
        clinic_stmt = select(ClinicConfigTable.clinic_id).where(
            ClinicConfigTable.tenant_id == user.tenant_id
        )
        clinic_result = await session.execute(clinic_stmt)
        clinic_row = clinic_result.scalar_one_or_none()
        if clinic_row is not None:
            clinic_id = str(clinic_row)

        # Build JWT claims
        now = datetime.now(timezone.utc)
        claims = {
            "sub": str(user.id),
            "tenant_id": str(user.tenant_id),
            "role": user.role.value,
            "email": user.email,
            "iat": now,
            "exp": now + timedelta(hours=JWT_EXPIRATION_HOURS),
            "jti": str(uuid4()),
        }

        # Include builder_id claim if user has one
        if user.builder_id is not None:
            claims["builder_id"] = str(user.builder_id)

        token = jwt.encode(claims, settings.secret_key, algorithm=JWT_ALGORITHM)

        return LoginResponse(
            token=token,
            email=user.email,
            tenant_id=str(user.tenant_id),
            clinic_id=clinic_id,
            role=user.role.value,
        )


@router.post(
    "/staging-rehearsal-token",
    response_model=StagingRehearsalTokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Issue a short-lived staging rehearsal token",
    description=(
        "Authenticate an active super administrator and issue a ten-minute JWT "
        "limited to the staging billing rehearsal authorization and readback routes."
    ),
    responses={
        200: {"description": "Staging rehearsal token issued"},
        400: {"description": "Malformed credentials payload"},
        401: {"description": "Invalid email or password"},
        403: {"description": "User is inactive or is not a super administrator"},
        404: {"description": "Issuer is available only in staging"},
        429: {"description": "Staging token issuance rate limit exceeded"},
        503: {"description": "Staging token issuer is unavailable"},
    },
    tags=["Auth"],
)
async def issue_staging_rehearsal_token(
    body: StagingRehearsalTokenRequest,
    request: Request,
) -> StagingRehearsalTokenResponse:
    """Issue a staging-only JWT after the always-on ingress throttle admits it."""
    if settings.environment != STAGING_REHEARSAL_TOKEN_ENVIRONMENT:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not found",
            headers={"Cache-Control": "no-store"},
        )
    if not getattr(request.state, "staging_rehearsal_issuer_admitted", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Staging token issuer is unavailable",
            headers={"Cache-Control": "no-store"},
        )

    normalized_email = str(body.email)
    async with get_db_session() as session:
        result = await session.execute(
            select(User).where(func.lower(User.email) == normalized_email)
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
                headers={"Cache-Control": "no-store"},
            )

        try:
            password_matches = pwd_context.verify(body.password, user.password_hash)
        except Exception:
            password_matches = False
        if not password_matches:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
                headers={"Cache-Control": "no-store"},
            )

        if not user.is_active or user.role != UserRole.super_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User is not authorized for staging rehearsal",
                headers={"Cache-Control": "no-store"},
            )

        issued_at = int(time.time())
        claims = {
            "sub": str(user.id),
            "tenant_id": str(user.tenant_id),
            "role": user.role.value,
            "email": user.email,
            "iat": issued_at,
            "exp": issued_at + STAGING_REHEARSAL_TOKEN_TTL_SECONDS,
            "jti": str(uuid4()),
            "token_use": STAGING_REHEARSAL_TOKEN_USE,
            "token_environment": STAGING_REHEARSAL_TOKEN_ENVIRONMENT,
            "scope": STAGING_REHEARSAL_TOKEN_SCOPE,
        }
        token = jwt.encode(claims, settings.secret_key, algorithm=JWT_ALGORITHM)

    return StagingRehearsalTokenResponse(
        token=token,
        expires_in=STAGING_REHEARSAL_TOKEN_TTL_SECONDS,
        token_use=STAGING_REHEARSAL_TOKEN_USE,
    )


def _password_change_forbidden() -> HTTPException:
    """Keep all password-authority failures on one stable public contract."""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"error": "password_change_forbidden"},
    )


@password_router.patch(
    "/me/password",
    status_code=status.HTTP_200_OK,
    summary="Change My Password",
    description="Change the authenticated builder or clinic administrator password.",
    responses={
        200: {"description": "Password changed"},
        401: {"description": "Missing, invalid, or expired Bearer JWT"},
        403: {
            "description": "Password change is not authorized",
            "content": {
                "application/json": {
                    "example": {"detail": {"error": "password_change_forbidden"}}
                }
            },
        },
        422: {
            "description": "Invalid password payload without echoed input values",
        },
    },
)
async def change_my_password(
    body: PasswordChangeRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, bool]:
    """Update only the current JWT subject after revalidating current DB authority."""
    if tenant.api_key or tenant.user_id is None:
        raise _password_change_forbidden()

    result = await session.execute(select(User).where(User.id == tenant.user_id))
    user = result.scalar_one_or_none()
    if (
        user is None
        or not user.is_active
        or user.tenant_id != tenant.tenant_id
        or user.role.value != tenant.role
        or user.role not in {UserRole.builder, UserRole.clinic_admin}
        or (
            user.role == UserRole.builder
            and (user.builder_id is None or user.builder_id != tenant.builder_id)
        )
    ):
        raise _password_change_forbidden()

    user.password_hash = pwd_context.hash(body.new_password)
    await session.commit()
    return {"ok": True}


# Keep password validation local so only the password contract replaces FastAPI's
# default (which includes the rejected input value in its error details).
router.include_router(password_router)
