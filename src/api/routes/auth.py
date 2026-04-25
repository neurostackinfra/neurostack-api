"""
NeuroStack API - Auth Endpoint
User login with JWT token generation.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from fastapi import APIRouter, HTTPException, status
from passlib.context import CryptContext
from sqlalchemy import select

from src.api.schemas.auth import LoginRequest, LoginResponse
from src.config.settings import settings
from src.models.clinic_config_table import ClinicConfigTable
from src.models.user import User
from src.repositories import get_db_session

router = APIRouter()

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
                "application/json": {
                    "example": {"detail": "Invalid email or password"}
                }
            },
        },
        403: {
            "description": "User account inactive",
            "content": {
                "application/json": {
                    "example": {"detail": "User account is inactive"}
                }
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
        clinic_id: str | None = None
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

        token = jwt.encode(claims, settings.secret_key, algorithm=JWT_ALGORITHM)

        return LoginResponse(
            token=token,
            email=user.email,
            tenant_id=str(user.tenant_id),
            clinic_id=clinic_id,
            role=user.role.value,
        )
