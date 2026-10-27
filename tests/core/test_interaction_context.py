"""Contracts for the trusted interaction context."""

import ast
import json
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

import src.interaction_context as interaction_context_module
from src.api.middleware import auth as auth_module
from src.api.middleware.auth import TenantContext
from src.api.schemas.events import EventType
from src.core.interaction_context import InteractionOrigin, ResolvedInteractionContext
from src.interaction_context import (
    InteractionContextError,
    build_interaction_context,
    classify_interaction_origin,
    require_interaction_context,
    unknown_interaction_context,
)
from src.services.credit_principal import identity_from_tenant_context


def test_interaction_origin_is_a_closed_string_contract() -> None:
    assert {origin.value for origin in InteractionOrigin} == {
        "production",
        "simulator",
        "unknown",
    }
    with pytest.raises(ValueError):
        InteractionOrigin("client-supplied")


def test_resolved_interaction_context_is_frozen_and_auth_free() -> None:
    context = ResolvedInteractionContext(
        effective_tenant_id=uuid4(),
        session_id=uuid4(),
        origin=InteractionOrigin.PRODUCTION,
        interaction_event_id=uuid4(),
    )

    with pytest.raises(FrozenInstanceError):
        context.origin = InteractionOrigin.UNKNOWN  # type: ignore[misc]

    assert {field.name for field in fields(context)} == {
        "effective_tenant_id",
        "session_id",
        "origin",
        "interaction_event_id",
    }


def test_interaction_event_id_is_explicitly_nullable() -> None:
    context = ResolvedInteractionContext(
        effective_tenant_id=uuid4(),
        session_id=uuid4(),
        origin=InteractionOrigin.UNKNOWN,
    )
    assert context.interaction_event_id is None


# ============================================
# Group 3: constructor, classifier, unknown factory and seam check
# ============================================

_OPERATIONAL_ROLES = ("clinic_admin", "builder", "super_admin")


@pytest.mark.parametrize("event_type", list(EventType))
def test_single_constructor_allocates_interaction_id_iff_message_in(
    event_type: EventType,
) -> None:
    tenant_id, session_id = uuid4(), uuid4()

    for given in (event_type, event_type.value):
        context = build_interaction_context(
            effective_tenant_id=tenant_id,
            session_id=session_id,
            origin=InteractionOrigin.SIMULATOR,
            event_type=given,
        )

        assert type(context) is ResolvedInteractionContext
        assert context.effective_tenant_id == tenant_id
        assert context.session_id == session_id
        assert context.origin is InteractionOrigin.SIMULATOR
        if event_type is EventType.MESSAGE_IN:
            assert isinstance(context.interaction_event_id, UUID)
        else:
            assert context.interaction_event_id is None


def test_constructor_preallocates_a_fresh_id_per_interaction() -> None:
    tenant_id, session_id = uuid4(), uuid4()
    ids = {
        build_interaction_context(
            effective_tenant_id=tenant_id,
            session_id=session_id,
            origin=InteractionOrigin.PRODUCTION,
            event_type="message.in",
        ).interaction_event_id
        for _ in range(5)
    }
    assert len(ids) == 5 and None not in ids


@pytest.mark.parametrize(
    "overrides",
    [
        {"effective_tenant_id": "not-a-uuid"},
        {"session_id": None},
        {"origin": "production"},
        {"event_type": None},
    ],
)
def test_constructor_rejects_untyped_inputs(overrides: dict[str, Any]) -> None:
    arguments: dict[str, Any] = {
        "effective_tenant_id": uuid4(),
        "session_id": uuid4(),
        "origin": InteractionOrigin.PRODUCTION,
        "event_type": "message.in",
    }
    arguments.update(overrides)
    with pytest.raises(TypeError):
        build_interaction_context(**arguments)


@pytest.mark.parametrize("event_type", ["message.in", "session.start"])
def test_unknown_factory_delegates_to_the_single_constructor(
    monkeypatch: pytest.MonkeyPatch, event_type: str
) -> None:
    calls: list[dict[str, Any]] = []
    real_constructor = interaction_context_module.build_interaction_context

    def spy(**kwargs: Any) -> ResolvedInteractionContext:
        calls.append(kwargs)
        return real_constructor(**kwargs)

    monkeypatch.setattr(interaction_context_module, "build_interaction_context", spy)
    tenant_id, session_id = uuid4(), uuid4()

    context = unknown_interaction_context(
        effective_tenant_id=tenant_id, session_id=session_id, event_type=event_type
    )

    assert calls == [
        {
            "effective_tenant_id": tenant_id,
            "session_id": session_id,
            "origin": InteractionOrigin.UNKNOWN,
            "event_type": event_type,
        }
    ]
    assert context.origin is InteractionOrigin.UNKNOWN
    with pytest.raises(FrozenInstanceError):
        context.origin = InteractionOrigin.PRODUCTION  # type: ignore[misc]
    assert (context.interaction_event_id is not None) == (event_type == "message.in")


@pytest.mark.parametrize("role", _OPERATIONAL_ROLES)
def test_operational_jwt_is_simulator(role: str) -> None:
    tenant = TenantContext(tenant_id=uuid4(), user_id=uuid4(), role=role)
    assert classify_interaction_origin(tenant) is InteractionOrigin.SIMULATOR


def test_api_key_is_production() -> None:
    tenant = TenantContext(tenant_id=uuid4(), api_key="ns_live_key")
    assert classify_interaction_origin(tenant) is InteractionOrigin.PRODUCTION


def test_consistent_meta_hmac_is_production() -> None:
    tenant = TenantContext(tenant_id=uuid4(), authentication_mode="meta_hmac")
    assert classify_interaction_origin(tenant) is InteractionOrigin.PRODUCTION


@pytest.mark.parametrize("role", ["investor", "unrecognized", "", None, "CLINIC_ADMIN"])
def test_jwt_outside_the_allowlist_is_unknown_in_the_pure_classifier(
    role: str | None,
) -> None:
    tenant = TenantContext(tenant_id=uuid4(), user_id=uuid4(), role=role)
    # The identity itself is consistent: only the role keeps it out.
    assert identity_from_tenant_context(tenant) is not None
    assert classify_interaction_origin(tenant) is InteractionOrigin.UNKNOWN


def test_classifier_allowlist_mirrors_the_shared_resolver() -> None:
    assert set(interaction_context_module.SIMULATOR_JWT_ROLES) == set(
        auth_module._OPERATIONAL_JWT_ROLES
    )


class _ExplodingTenant:
    tenant_id = uuid4()

    @property
    def authentication_mode(self) -> str:
        raise RuntimeError("broken auth state")


def _tenant_cases() -> list[tuple[str, object]]:
    tenant_id = uuid4()
    user_id = uuid4()
    cases: list[tuple[str, object]] = []
    for role in _OPERATIONAL_ROLES:
        cases += [
            (
                f"jwt-{role}",
                TenantContext(tenant_id=tenant_id, user_id=user_id, role=role),
            ),
            (
                f"jwt-{role}-with-key",
                SimpleNamespace(
                    authentication_mode="jwt",
                    tenant_id=tenant_id,
                    api_key="leaked",
                    user_id=user_id,
                    role=role,
                ),
            ),
            (
                f"jwt-{role}-without-subject",
                SimpleNamespace(
                    authentication_mode="jwt",
                    tenant_id=tenant_id,
                    api_key="",
                    user_id=None,
                    role=role,
                ),
            ),
            (
                f"jwt-{role}-string-subject",
                SimpleNamespace(
                    authentication_mode="jwt",
                    tenant_id=tenant_id,
                    api_key="",
                    user_id=str(user_id),
                    role=role,
                ),
            ),
            (
                f"jwt-{role}-string-tenant",
                SimpleNamespace(
                    authentication_mode="jwt",
                    tenant_id=str(tenant_id),
                    api_key="",
                    user_id=user_id,
                    role=role,
                ),
            ),
        ]
    cases += [
        ("api-key", TenantContext(tenant_id=tenant_id, api_key="k")),
        (
            "api-key-with-user",
            TenantContext(tenant_id=tenant_id, api_key="k", user_id=user_id),
        ),
        (
            "api-key-mode-without-key",
            SimpleNamespace(
                authentication_mode="api_key",
                tenant_id=tenant_id,
                api_key="",
                user_id=None,
                role=None,
            ),
        ),
        (
            "api-key-string-tenant",
            SimpleNamespace(
                authentication_mode="api_key",
                tenant_id=str(tenant_id),
                api_key="k",
                user_id=None,
                role=None,
            ),
        ),
        ("meta", TenantContext(tenant_id=tenant_id, authentication_mode="meta_hmac")),
        (
            "meta-with-key",
            TenantContext(
                tenant_id=tenant_id, api_key="k", authentication_mode="meta_hmac"
            ),
        ),
        (
            "meta-with-user",
            TenantContext(
                tenant_id=tenant_id, user_id=user_id, authentication_mode="meta_hmac"
            ),
        ),
        (
            "unknown-mode",
            SimpleNamespace(
                authentication_mode="zapi",
                tenant_id=tenant_id,
                api_key="",
                user_id=None,
                role=None,
            ),
        ),
        ("missing-attributes", object()),
        ("none", None),
        ("exploding", _ExplodingTenant()),
    ]
    return cases


_EXPECTED_ORIGIN_BY_IDENTITY_KIND = {
    "api_key": InteractionOrigin.PRODUCTION,
    "jwt": InteractionOrigin.SIMULATOR,
    "meta": InteractionOrigin.PRODUCTION,
}


@pytest.mark.parametrize(
    "tenant", [case for _, case in _tenant_cases()], ids=[n for n, _ in _tenant_cases()]
)
def test_classifier_agrees_with_credit_identity_on_consistency(tenant: object) -> None:
    """Allowlisted roles: a known origin iff the credit identity is consistent."""

    identity = identity_from_tenant_context(tenant)
    origin = classify_interaction_origin(tenant)

    if identity is None:
        assert origin is InteractionOrigin.UNKNOWN
    else:
        assert origin is _EXPECTED_ORIGIN_BY_IDENTITY_KIND[identity.kind]


@pytest.mark.parametrize("channel", ["whatsapp", "web", "simulator", "api", "unknown"])
def test_classifier_ignores_channel_body_metadata_and_headers(channel: str) -> None:
    tenant = TenantContext(tenant_id=uuid4(), user_id=uuid4(), role="clinic_admin")
    # Request-shaped attributes on the tenant object never feed the decision.
    tenant.channel = channel  # type: ignore[attr-defined]
    tenant.metadata = {"interaction_origin": "production"}  # type: ignore[attr-defined]
    tenant.headers = {"X-Origin": "production"}  # type: ignore[attr-defined]
    assert classify_interaction_origin(tenant) is InteractionOrigin.SIMULATOR


class _ContextSubclass(ResolvedInteractionContext):
    pass


@pytest.mark.parametrize(
    "forged",
    [
        "dict",
        "json",
        "namespace",
        "mock",
        "subclass",
        "string-origin",
    ],
)
def test_seam_accepts_only_real_contexts(forged: str) -> None:
    tenant_id, session_id = uuid4(), uuid4()
    fields_ = {
        "effective_tenant_id": tenant_id,
        "session_id": session_id,
        "origin": InteractionOrigin.PRODUCTION,
        "interaction_event_id": None,
    }
    candidate: object
    if forged == "dict":
        candidate = dict(fields_)
    elif forged == "json":
        candidate = json.dumps({k: str(v) for k, v in fields_.items()})
    elif forged == "namespace":
        candidate = SimpleNamespace(**fields_)
    elif forged == "mock":
        candidate = MagicMock(spec=ResolvedInteractionContext, **fields_)
    elif forged == "subclass":
        candidate = _ContextSubclass(**fields_)
    else:
        candidate = ResolvedInteractionContext(
            effective_tenant_id=tenant_id,
            session_id=session_id,
            origin="production",  # type: ignore[arg-type]
        )

    with pytest.raises(TypeError):
        require_interaction_context(
            candidate, tenant_id=tenant_id, session_id=session_id
        )


@pytest.mark.parametrize("diverges", ["tenant", "session", "event_type"])
def test_seam_fails_closed_on_divergence_without_leaking_values(
    diverges: str,
) -> None:
    context = build_interaction_context(
        effective_tenant_id=uuid4(),
        session_id=uuid4(),
        origin=InteractionOrigin.PRODUCTION,
        event_type="message.in",
    )
    tenant_id = uuid4() if diverges == "tenant" else context.effective_tenant_id
    session_id = uuid4() if diverges == "session" else context.session_id
    event_type = "session.start" if diverges == "event_type" else "message.in"

    with pytest.raises(InteractionContextError) as error:
        require_interaction_context(
            context, tenant_id=tenant_id, session_id=session_id, event_type=event_type
        )

    rendered = str(error.value)
    for value in (
        context.effective_tenant_id,
        context.session_id,
        context.interaction_event_id,
    ):
        assert str(value) not in rendered
    assert "production" not in rendered


def test_seam_returns_the_same_object_when_consistent() -> None:
    context = build_interaction_context(
        effective_tenant_id=uuid4(),
        session_id=uuid4(),
        origin=InteractionOrigin.SIMULATOR,
        event_type="session.start",
    )
    assert (
        require_interaction_context(
            context,
            tenant_id=context.effective_tenant_id,
            session_id=context.session_id,
            event_type="session.start",
        )
        is context
    )


def test_context_docstring_requires_proof_before_consuming() -> None:
    doc = " ".join((ResolvedInteractionContext.__doc__ or "").split())
    assert "before consuming the context" in doc
    assert "before constructing" not in doc


def test_context_module_stays_pure() -> None:
    source = Path(interaction_context_module.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert imported == {"dataclasses", "enum", "typing", "uuid"}


def test_core_path_remains_a_re_export() -> None:
    from src.core import interaction_context as core_module

    assert core_module.InteractionOrigin is InteractionOrigin
    assert core_module.ResolvedInteractionContext is ResolvedInteractionContext
