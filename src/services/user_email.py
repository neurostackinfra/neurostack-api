"""Case-insensitive uniqueness of user account emails.

``users.email`` is unique ignoring case (index ``uq_users_email_lower`` on
``lower(email)``, migration 025). Routes check here before writing so a
duplicate is a 409 rather than an ``IntegrityError``; the index still decides
concurrent writes.
"""

from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.user import User


async def email_in_use(
    session: AsyncSession, email: str, *, exclude_user_id: Optional[UUID] = None
) -> bool:
    """Return whether another account already uses ``email`` ignoring case."""
    stmt = select(User.id).where(func.lower(User.email) == func.lower(email))
    if exclude_user_id is not None:
        stmt = stmt.where(User.id != exclude_user_id)
    return (await session.execute(stmt.limit(1))).first() is not None
