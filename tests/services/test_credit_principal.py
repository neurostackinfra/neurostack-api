"""``derive_credit_principal`` (neurostack-credit-system Group 1b, option B).

The principal comes from what ``main``'s auth already authenticated, the
builder from ``tenants.builder_id`` as re-read from the database:

* exact strings -- ``user:{sub}``, ``api_key:{tenant}``, ``zapi:{tenant}``,
  ``meta:{tenant}``, ``uazapi:{tenant}``, ``evolution:{tenant}``;
* Uazapi and Evolution are ``authenticated=False`` (routed by instance name);
* a tenant with ``builder_id`` NULL is standalone; a JWT ``builder_id`` claim
  is never an input; a tenant read of another tenant is refused;
* the mapping from ``main``'s ``TenantContext`` refuses inconsistent contexts
  instead of raising.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.api.middleware.auth import TenantContext
from src.models.base import Base
from src.models.builder import Builder
from src.models.tenant import Tenant
from src.repositories.database import _patch_jsonb_for_sqlite
from src.services.credit_principal import (
    CREDIT_IDENTITY_STATE_KEY,
    CREDIT_INGRESS_KINDS,
    AuthenticatedIdentity,
    CreditPrincipal,
    CreditPrincipalError,
    TenantBuilder,
    credit_identity,
    derive_credit_principal,
    identity_from_tenant_context,
    publish_credit_identity,
    read_tenant_builder,
    request_credit_identity,
)

TENANT = UUID("7a0c1b2e-0000-4000-8000-00000000c1b1")
SUBJECT = UUID("5b1d2c3f-0000-4000-8000-00000000c1b2")
BUILDER = UUID("9e2f3a4b-0000-4000-8000-00000000c1b3")


def _identity(kind: str) -> AuthenticatedIdentity:
    identity = credit_identity(kind, TENANT, SUBJECT if kind == "jwt" else None)
    assert identity is not None
    return identity


@pytest.mark.parametrize(
    ("kind", "principal_id", "authenticated"),
    [
        ("jwt", f"user:{SUBJECT}", True),
        ("api_key", f"api_key:{TENANT}", True),
        ("zapi", f"zapi:{TENANT}", True),
        ("meta", f"meta:{TENANT}", True),
        ("uazapi", f"uazapi:{TENANT}", False),
        ("evolution", f"evolution:{TENANT}", False),
    ],
)
def test_exact_principal_strings(
    kind: str, principal_id: str, authenticated: bool
) -> None:
    principal = derive_credit_principal(
        _identity(kind), TenantBuilder(TENANT, BUILDER, True)
    )

    assert principal == CreditPrincipal(
        principal_id=principal_id,
        kind=kind,  # type: ignore[arg-type]
        tenant_id=TENANT,
        builder_id=BUILDER,
        authenticated=authenticated,
    )
    assert principal.standalone is False


def test_every_ingress_kind_is_covered() -> None:
    assert CREDIT_INGRESS_KINDS == {
        "jwt",
        "api_key",
        "zapi",
        "meta",
        "uazapi",
        "evolution",
    }


def test_principal_strings_use_the_canonical_uuid_form() -> None:
    identity = credit_identity(
        "jwt", UUID(str(TENANT).upper()), UUID(SUBJECT.hex.upper())
    )
    assert identity is not None
    principal = derive_credit_principal(identity, TenantBuilder(TENANT, None, True))
    assert principal.principal_id == f"user:{SUBJECT}"


@pytest.mark.parametrize("kind", sorted(CREDIT_INGRESS_KINDS))
def test_a_tenant_without_builder_is_standalone(kind: str) -> None:
    principal = derive_credit_principal(
        _identity(kind), TenantBuilder(TENANT, None, True)
    )
    assert principal.builder_id is None
    assert principal.standalone is True


def test_a_tenant_read_of_another_tenant_is_refused() -> None:
    with pytest.raises(CreditPrincipalError):
        derive_credit_principal(
            _identity("api_key"), TenantBuilder(uuid4(), BUILDER, True)
        )


@pytest.mark.parametrize(
    ("kind", "tenant_id", "subject"),
    [
        ("password", TENANT, None),
        ("jwt", TENANT, None),
        ("jwt", TENANT, str(SUBJECT)),
        ("api_key", str(TENANT), None),
        ("api_key", None, None),
        ("meta", TENANT, SUBJECT),
        ("uazapi", TENANT, SUBJECT),
    ],
)
def test_unsound_identities_are_refused(
    kind: str, tenant_id: Any, subject: Any
) -> None:
    with pytest.raises((TypeError, ValueError)):
        AuthenticatedIdentity(kind=kind, tenant_id=tenant_id, subject=subject)
    assert credit_identity(kind, tenant_id, subject) is None


# ---------------------------------------------------------------------------
# main's TenantContext -> identity.
# ---------------------------------------------------------------------------


def test_an_api_key_context_maps_to_its_tenant() -> None:
    context = TenantContext(tenant_id=TENANT, api_key="validated-key")
    assert identity_from_tenant_context(context) == AuthenticatedIdentity(
        "api_key", TENANT
    )


def test_a_jwt_context_maps_to_its_subject_and_ignores_the_builder_claim() -> None:
    context = TenantContext(
        tenant_id=TENANT,
        user_id=SUBJECT,
        role="builder",
        builder_id=BUILDER,
        authentication_mode="jwt",
    )
    identity = identity_from_tenant_context(context)

    assert identity == AuthenticatedIdentity("jwt", TENANT, SUBJECT)
    # The builder is re-read from tenants, never taken from the token.
    principal = derive_credit_principal(identity, TenantBuilder(TENANT, None, True))
    assert principal.builder_id is None and principal.standalone


def test_a_meta_hmac_context_maps_to_meta() -> None:
    context = TenantContext(tenant_id=TENANT, authentication_mode="meta_hmac")
    assert identity_from_tenant_context(context) == AuthenticatedIdentity(
        "meta", TENANT
    )


@pytest.mark.parametrize(
    "context",
    [
        TenantContext(tenant_id=TENANT),  # jwt mode without a subject
        TenantContext(tenant_id=TENANT, authentication_mode="api_key"),  # no key
        TenantContext(
            tenant_id=TENANT, api_key="k", user_id=SUBJECT, authentication_mode="jwt"
        ),
        TenantContext(tenant_id=TENANT, api_key="k", authentication_mode="meta_hmac"),
        object(),
        None,
    ],
)
def test_inconsistent_contexts_map_to_no_identity(context: Any) -> None:
    assert identity_from_tenant_context(context) is None


def test_a_context_whose_attributes_raise_maps_to_no_identity() -> None:
    class Hostile:
        @property
        def authentication_mode(self) -> str:
            raise RuntimeError("boom")

    assert identity_from_tenant_context(Hostile()) is None


# ---------------------------------------------------------------------------
# request.state publication.
# ---------------------------------------------------------------------------


class _Request:
    def __init__(self, state: Any) -> None:
        self.scope = {"type": "http", "state": state}


def test_publication_round_trip_and_garbage() -> None:
    state: dict[str, Any] = {}
    publish_credit_identity(state, None)
    assert state == {}
    assert request_credit_identity(_Request(state)) is None  # type: ignore[arg-type]

    identity = _identity("meta")
    publish_credit_identity(state, identity)
    assert state == {CREDIT_IDENTITY_STATE_KEY: identity}
    assert request_credit_identity(_Request(state)) is identity  # type: ignore[arg-type]

    garbage = {CREDIT_IDENTITY_STATE_KEY: "meta:" + str(TENANT)}
    assert request_credit_identity(_Request(garbage)) is None  # type: ignore[arg-type]
    assert request_credit_identity(_Request(None)) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The builder re-read from tenants.
# ---------------------------------------------------------------------------


@pytest.fixture
async def tenants() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    _patch_jsonb_for_sqlite()
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[Builder.__table__, Tenant.__table__],
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    builder_id: UUID | None,
    *,
    is_active: bool = True,
) -> None:
    async with factory() as session:
        if builder_id is not None:
            session.add(Builder(id=builder_id, name=f"Builder {builder_id.hex[:8]}"))
            await session.flush()
        session.add(
            Tenant(
                id=tenant_id,
                name=f"Clinic {tenant_id.hex[:8]}",
                slug=f"principal-{tenant_id.hex}",
                api_key_hash=f"hash-{tenant_id.hex}",
                builder_id=builder_id,
                is_active=is_active,
                config={},
            )
        )
        await session.commit()


async def test_the_builder_is_re_read_from_tenants(
    tenants: async_sessionmaker[AsyncSession],
) -> None:
    standalone, owned = uuid4(), uuid4()
    await _seed(tenants, standalone, None)
    await _seed(tenants, owned, BUILDER, is_active=False)

    async with tenants() as session:
        standalone_row = await read_tenant_builder(session, standalone)
        owned_row = await read_tenant_builder(session, owned)
        missing_row = await read_tenant_builder(session, uuid4())

    assert standalone_row == TenantBuilder(standalone, None, True)
    assert owned_row == TenantBuilder(owned, BUILDER, False)
    assert missing_row is None

    assert standalone_row is not None and owned_row is not None
    as_standalone = derive_credit_principal(
        AuthenticatedIdentity("uazapi", standalone), standalone_row
    )
    as_owned = derive_credit_principal(AuthenticatedIdentity("zapi", owned), owned_row)
    assert (as_standalone.standalone, as_standalone.authenticated) == (True, False)
    assert (as_owned.builder_id, as_owned.principal_id) == (BUILDER, f"zapi:{owned}")
