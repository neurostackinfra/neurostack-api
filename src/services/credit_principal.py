"""Credit principal derived from the current auth (neurostack-credit-system 1b).

Owner option B (2026-10-25): no credential table.  ``derive_credit_principal``
is a pure function over the identity that ``main``'s auth has ALREADY
authenticated, plus the tenant's builder as re-read from ``tenants`` --
never a builder or clinic the caller asserts (the JWT ``builder_id`` claim is
not an input).  It is used only with the switch effective.

=================  ============================  ===============  ==========
Ingress            Proof that ``main`` checks    ``principal_id``  authen-
                                                                   ticated
=================  ============================  ===============  ==========
JWT                HS256 signature (``sub``)     ``user:{sub}``      yes
``X-API-Key``      ``tenants.api_key_hash``      ``api_key:{t}``     yes
Z-API              capability token digest       ``zapi:{t}``        yes
                   (``zapi_inbound_token_digest``)
Meta               ``X-Hub-Signature-256`` HMAC  ``meta:{t}``        yes
Uazapi             envelope ``token`` equal to   ``uazapi:{t}``      yes
                   the stored instance token
                   (``inbound_verified``)
Uazapi             instance name only            ``uazapi:{t}``      no
Evolution          instance name only            ``evolution:{t}``   no
=================  ============================  ===============  ==========

``{t}`` is the authenticated tenant id and ``{sub}`` the JWT subject, both in
canonical UUID form.  An Evolution webhook is routed by instance name with no
inbound secret (its stored key is an OUTBOUND credential), so it is
``authenticated=False``: zero debit, still served (Decision 25).  A Uazapi
webhook is the same unless the route verified the callback envelope's
``token`` against the instance token stored for that tenant: only then does
the route build the identity with ``inbound_verified=True``, which is valid
for kind ``uazapi`` alone and makes the principal authenticated.  The
``principal_id`` (and therefore the claim key) is ``uazapi:{t}`` either way.
A tenant whose ``builder_id`` is NULL is standalone.

``TenantIdentityMiddleware`` publishes the identity it authenticated on
``request.state.credit_identity``; a rejected request publishes nothing.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Any, Final, Literal, get_args
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection

from src.config.logging import get_logger
from src.models.tenant import Tenant

logger = get_logger(__name__)

CreditIngressKind = Literal["jwt", "api_key", "zapi", "meta", "uazapi", "evolution"]
CREDIT_INGRESS_KINDS: Final[frozenset[str]] = frozenset(get_args(CreditIngressKind))
# Key of the published identity in the ASGI scope state.
CREDIT_IDENTITY_STATE_KEY: Final = "credit_identity"
_PRINCIPAL_PREFIX: Final[dict[str, str]] = {
    "jwt": "user",
    "api_key": "api_key",
    "zapi": "zapi",
    "meta": "meta",
    "uazapi": "uazapi",
    "evolution": "evolution",
}
# Routed by instance name with no inbound secret by default.  ``uazapi`` stays
# here on purpose: only an identity the route built with
# ``inbound_verified=True`` (an enforced envelope-token match) authenticates
# it; every other Uazapi identity, and every Evolution one, is unauthenticated.
_UNAUTHENTICATED_KINDS: Final = frozenset({"uazapi", "evolution"})
# The kinds whose route may prove the caller per request (``inbound_verified``):
# a Uazapi callback whose envelope token equals the stored instance token.
_INBOUND_VERIFIABLE_KINDS: Final = frozenset({"uazapi"})


class CreditPrincipalError(ValueError):
    """The identity and the tenant read do not describe the same tenant."""


@dataclass(frozen=True)
class AuthenticatedIdentity:
    """What ``main``'s auth proved for one request (and nothing else).

    ``subject`` is the JWT ``sub`` and exists only for ``kind == "jwt"``.
    ``inbound_verified`` says the route proved this request's caller itself
    (a Uazapi envelope token equal to the stored instance token); it is valid
    only for kind ``uazapi`` and never changes the ``principal_id``.
    """

    kind: CreditIngressKind
    tenant_id: UUID
    subject: UUID | None = None
    inbound_verified: bool = False

    def __post_init__(self) -> None:
        if self.kind not in CREDIT_INGRESS_KINDS:
            raise ValueError(f"unknown credit ingress kind {self.kind!r}")
        if not isinstance(self.tenant_id, UUID):
            raise TypeError("tenant_id must be a UUID")
        if self.kind == "jwt":
            if not isinstance(self.subject, UUID):
                raise TypeError("a JWT identity needs its UUID subject")
        elif self.subject is not None:
            raise ValueError("only a JWT identity carries a subject")
        if type(self.inbound_verified) is not bool:
            raise TypeError("inbound_verified must be a bool")
        if self.inbound_verified and self.kind not in _INBOUND_VERIFIABLE_KINDS:
            raise ValueError(f"a {self.kind!r} identity cannot be inbound-verified")


@dataclass(frozen=True)
class TenantBuilder:
    """``tenants.builder_id`` (and ``is_active``) as re-read from the database."""

    tenant_id: UUID
    builder_id: UUID | None
    is_active: bool


@dataclass(frozen=True)
class CreditPrincipal:
    principal_id: str
    kind: CreditIngressKind
    tenant_id: UUID
    builder_id: UUID | None
    authenticated: bool

    @property
    def standalone(self) -> bool:
        """A tenant without a builder has no credit account (observational)."""

        return self.builder_id is None


def derive_credit_principal(
    identity: AuthenticatedIdentity,
    tenant: TenantBuilder,
) -> CreditPrincipal:
    """Pure derivation; ``tenant`` must be the re-read row of the same tenant."""

    if tenant.tenant_id != identity.tenant_id:
        raise CreditPrincipalError("tenant read does not match the identity")
    subject = identity.subject if identity.kind == "jwt" else identity.tenant_id
    # A verified Uazapi request is authenticated; the kind check repeats the
    # one of ``__post_init__`` so no other kind is lifted by the field.
    verified = (
        identity.kind in _INBOUND_VERIFIABLE_KINDS and identity.inbound_verified is True
    )
    return CreditPrincipal(
        principal_id=f"{_PRINCIPAL_PREFIX[identity.kind]}:{subject}",
        kind=identity.kind,
        tenant_id=identity.tenant_id,
        builder_id=tenant.builder_id,
        authenticated=identity.kind not in _UNAUTHENTICATED_KINDS or verified,
    )


async def read_tenant_builder(
    session: AsyncSession, tenant_id: UUID
) -> TenantBuilder | None:
    """Re-read the tenant's builder; ``None`` when the tenant row is gone."""

    row = (
        await session.execute(
            select(Tenant.builder_id, Tenant.is_active).where(Tenant.id == tenant_id)
        )
    ).one_or_none()
    if row is None:
        return None
    builder_id, is_active = row
    return TenantBuilder(
        tenant_id=tenant_id, builder_id=builder_id, is_active=bool(is_active)
    )


def credit_identity(
    kind: str,
    tenant_id: object,
    subject: object = None,
    *,
    inbound_verified: object = False,
) -> AuthenticatedIdentity | None:
    """Build an identity, or ``None`` when the inputs are not sound (no raise).

    ``inbound_verified=True`` for any kind but ``uazapi`` is not sound.
    """

    try:
        return AuthenticatedIdentity(
            kind=kind,  # type: ignore[arg-type]  # validated in __post_init__
            tenant_id=tenant_id,  # type: ignore[arg-type]
            subject=subject,  # type: ignore[arg-type]
            inbound_verified=inbound_verified,  # type: ignore[arg-type]
        )
    except (TypeError, ValueError):
        return None


def identity_from_tenant_context(tenant: Any) -> AuthenticatedIdentity | None:
    """Map ``main``'s authenticated ``TenantContext``; ``None`` if inconsistent.

    ``api_key`` needs the validated key and no user; ``jwt`` needs the UUID
    subject and no key; ``meta_hmac`` is the Meta HMAC context.  Never raises.
    """

    try:
        mode = getattr(tenant, "authentication_mode", None)
        tenant_id = getattr(tenant, "tenant_id", None)
        api_key = getattr(tenant, "api_key", "")
        user_id = getattr(tenant, "user_id", None)
        if mode == "api_key" and api_key and user_id is None:
            return credit_identity("api_key", tenant_id)
        if mode == "jwt" and not api_key:
            return credit_identity("jwt", tenant_id, user_id)
        if mode == "meta_hmac" and not api_key and user_id is None:
            return credit_identity("meta", tenant_id)
    except Exception as error:
        logger.error(
            "credit identity unavailable for this request",
            error_type=type(error).__name__,
        )
    return None


def publish_credit_identity(
    state: MutableMapping[str, Any], identity: AuthenticatedIdentity | None
) -> None:
    """Expose an authenticated identity on ``request.state`` (no-op on ``None``)."""

    if isinstance(identity, AuthenticatedIdentity):
        state[CREDIT_IDENTITY_STATE_KEY] = identity


def request_credit_identity(request: HTTPConnection) -> AuthenticatedIdentity | None:
    """The identity this request's auth published, if any."""

    state = request.scope.get("state")
    value = state.get(CREDIT_IDENTITY_STATE_KEY) if isinstance(state, dict) else None
    return value if isinstance(value, AuthenticatedIdentity) else None


__all__ = [
    "CREDIT_IDENTITY_STATE_KEY",
    "CREDIT_INGRESS_KINDS",
    "AuthenticatedIdentity",
    "CreditIngressKind",
    "CreditPrincipal",
    "CreditPrincipalError",
    "TenantBuilder",
    "credit_identity",
    "derive_credit_principal",
    "identity_from_tenant_context",
    "publish_credit_identity",
    "read_tenant_builder",
    "request_credit_identity",
]
