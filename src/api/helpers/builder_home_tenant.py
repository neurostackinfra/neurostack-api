"""A builder's own (home) tenant linked to no builder.

The one fallback shared by the credit reads (``resolve_credit_scope`` in
``src/api/routes/credits.py``) and the content export (``_export_tenant`` in
``src/api/routes/credit_export.py``).  Both normally require the tenant's
STORED builder (``tenants.builder_id``) to equal the builder JWT's
``builder_id`` claim.  The usual home tenant of a builder user is linked to
no builder (``tenants.builder_id`` NULL), so that rule refused a builder its
own tenant.  The fallback accepts exactly that case and nothing else:

* a JWT ``builder`` (never an API key) carrying a ``builder_id`` claim and a
  user id (``sub``);
* the effective tenant is the token's OWN tenant (no clinic override);
* that tenant is linked to no builder (an own tenant linked to ANOTHER
  builder is a conflict, refused by the callers);
* the token is not stale: the ``users`` row of ``sub`` is re-read and must
  exist, be active, have role ``builder``, the token's ``builder_id`` and the
  token's tenant as its tenant.  A user since moved to another builder or
  tenant, deactivated or deleted is refused, not served the old builder's
  account until the token expires;
* no OTHER builder's user shares that tenant: when a ``users`` row of role
  ``builder`` on the tenant names a different builder (or none), the tenant
  is not one builder's home and nobody gets the fallback.  A second user of
  the same builder, or a user of another role, does not block it.

Both ``users`` checks are ONE statement (the token's row plus an ``EXISTS``
over the tenant's other builder users).  The re-read touches ``users`` only (no ``credit_*`` or ``billing_*`` table),
so the callers' guarantee "refused before any financial read" holds.  The
callers map ``None`` to the same refusal as an absent tenant.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.api.middleware.auth import TenantContext
from src.models.user import User

_BUILDER_ROLE = "builder"


async def builder_home_tenant_account(
    session: AsyncSession,
    tenant: TenantContext,
    effective: UUID,
    stored_builder_id: UUID | None,
) -> UUID | None:
    """The builder whose account ``tenant`` reads through ``effective``
    under the home-tenant fallback, or ``None`` when the fallback does not
    apply (the caller refuses).

    ``stored_builder_id`` is ``tenants.builder_id`` as the caller re-read it
    for ``effective``.  The returned builder is the token's claim, confirmed
    against the current ``users`` row, on a tenant no other builder's user
    shares.  Every check on the token comes before the one ``users``
    statement: a refusal by those checks runs no SQL."""

    if (
        tenant.api_key
        or tenant.authentication_mode != "jwt"
        or tenant.role != _BUILDER_ROLE
        or tenant.builder_id is None
        or tenant.user_id is None
        or effective != tenant.tenant_id
        or stored_builder_id is not None
    ):
        return None
    # ``users.role``: VARCHAR before migration 024, the enum since: compare
    # its text so either works.
    other = aliased(User)
    shared = (
        select(other.id)
        .where(
            other.tenant_id == effective,
            cast(other.role, String) == _BUILDER_ROLE,
            other.builder_id.is_distinct_from(tenant.builder_id),
        )
        .exists()
    )
    user = (
        await session.execute(
            select(
                User.is_active,
                cast(User.role, String).label("role"),
                User.builder_id,
                User.tenant_id,
                shared.label("shared"),
            ).where(User.id == tenant.user_id)
        )
    ).one_or_none()
    if (
        user is None
        or user.is_active is not True
        or user.role != _BUILDER_ROLE
        or user.builder_id != tenant.builder_id
        or user.tenant_id != effective
        or bool(user.shared)
    ):
        return None
    return tenant.builder_id


__all__ = ["builder_home_tenant_account"]
