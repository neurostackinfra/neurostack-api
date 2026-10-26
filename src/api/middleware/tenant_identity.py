"""Canonical tenant identity guard for authenticated JSON ingress."""

import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from typing import Any, cast
from uuid import UUID

from fastapi.responses import JSONResponse
from sqlalchemy import select
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.api.middleware.auth import TenantContext
from src.api.routing_paths import decision_path
from src.config.logging import get_logger
from src.models.clinic_config_table import ClinicConfigTable
from src.models.tenant import Tenant
from src.repositories import get_db_session
from src.services.credit_principal import (
    credit_identity,
    identity_from_tenant_context,
    publish_credit_identity,
)
from src.services.credit_switch import capture_credit_switch

logger = get_logger(__name__)
_audit_logger = get_logger(f"{__name__}.audit")

META_SIGNATURE_HEADER = "x-hub-signature-256"
TENANT_IDENTITY_ERROR = {
    "error": "tenant_identity_mismatch",
    "message": "Tenant identity does not match the authenticated request",
}


def audit_tenant_identity_mismatch(
    *,
    request_id: str | None,
    route: str,
    method: str,
    authentication_mode: str,
    authenticated_tenant: UUID | None,
    claimed_tenant: UUID | None,
) -> None:
    """Emit one allowlisted audit record without caller credentials or payload."""

    _audit_logger.warning(
        "tenant_identity_mismatch",
        request_id=request_id or "unavailable",
        route=route,
        method=method,
        authentication_mode=authentication_mode,
        authenticated_tenant=(
            str(authenticated_tenant)
            if authenticated_tenant is not None
            else "unresolved"
        ),
        claimed_tenant=(
            str(claimed_tenant) if claimed_tenant is not None else "unresolved"
        ),
    )


def tenant_identity_mismatch_response() -> JSONResponse:
    """Build the shared mismatch response used by middleware and the route."""

    return JSONResponse(status_code=403, content=TENANT_IDENTITY_ERROR)


class TenantIdentityMiddleware:
    """Publish only authenticated or HMAC-authorized tenant identity."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            # Credit dominance switch, captured once per request at entry
            # (neurostack-credit-system Group 1b).  Only request.state changes.
            capture_credit_switch(scope)
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        # The router's path (routing_paths): "/v1/webhook/meta%0a" still
        # routes to the Meta receiver and must not skip its HMAC guard.
        path = decision_path(scope)
        if path == "/v1/events":
            await self._guard_event(scope, receive, send)
            return
        if path == "/v1/webhook/meta":
            await self._guard_meta(scope, receive, send)
            return

        await self.app(scope, receive, send)

    async def _guard_event(self, scope: Scope, receive: Receive, send: Send) -> None:
        tenant = _scope_tenant(scope)
        if tenant is None:
            await self.app(scope, receive, send)
            return

        raw_body, guarded_receive = await _bounded_body(scope, receive)
        body = _json_object(raw_body)
        if body is None:
            await _invalid_json(scope, receive, send)
            return
        asserted = body.get("tenant_id") if body is not None else None
        asserted_uuid = _optional_uuid(asserted)
        if asserted is not None and asserted_uuid is not None:
            if asserted_uuid != tenant.tenant_id:
                await self._reject(
                    scope,
                    receive,
                    send,
                    authentication_mode=tenant.authentication_mode,
                    authenticated_tenant=tenant.tenant_id,
                    claimed_tenant=asserted_uuid,
                )
                return

        state = _state(scope)
        state["canonical_tenant_id"] = tenant.tenant_id
        publish_credit_identity(state, identity_from_tenant_context(tenant))
        await self.app(scope, guarded_receive, send)

    async def _guard_meta(self, scope: Scope, receive: Receive, send: Send) -> None:
        raw_body, guarded_receive = await _bounded_body(scope, receive)

        # Receiver values are lookup hints, never tenant authorization.  Read
        # the complete safe subset first so a malformed sibling cannot skip
        # authentication of the exact body or select an arbitrary tenant.
        body = _json_object(raw_body)
        receiver_claims, malformed_receivers = _extract_meta_receiver_claims(body)
        receiver_ids = {phone_id for phone_id, _waba_id in receiver_claims}
        candidates = [
            _normalise_meta_candidate(row)
            for row in await _load_meta_candidates(receiver_ids)
        ]
        candidate_secrets = {
            secret
            for _phone_id, _tenant_id, _active, credentials in candidates
            for secret in _meta_candidate_secrets(credentials)
        }
        signature = _header(scope, META_SIGNATURE_HEADER)
        valid_secrets = [
            secret
            for secret in candidate_secrets
            if _verify_meta_signature(signature, raw_body, secret)
        ]
        if len(valid_secrets) != 1:
            await _invalid_meta_auth(scope, receive, send)
            return
        valid_secret = valid_secrets[0]
        signed_candidates = [
            candidate
            for candidate in candidates
            if valid_secret in _meta_candidate_secrets(candidate[3])
        ]

        # Semantic envelope decisions remain after HMAC over the exact bytes.
        if body is None:
            await _invalid_meta_envelope(scope, receive, send)
            return
        if body.get("object") != "whatsapp_business_account":
            await self._reject(
                scope,
                receive,
                send,
                authentication_mode="meta_hmac",
                authenticated_tenant=None,
                claimed_tenant=None,
            )
            return

        signed_receiver_ids = {phone_id for phone_id, *_rest in signed_candidates}
        resolved_tenants = {
            tenant_id for _phone, tenant_id, _active, _creds in signed_candidates
        }
        authenticated_tenant = (
            next(iter(resolved_tenants)) if len(resolved_tenants) == 1 else None
        )
        if (
            malformed_receivers
            or not receiver_claims
            or receiver_ids != signed_receiver_ids
            or len(receiver_ids) != 1
            or len(signed_candidates) != 1
            or len(resolved_tenants) != 1
            or any(not active for _phone, _tenant, active, _creds in signed_candidates)
        ):
            await self._reject(
                scope,
                receive,
                send,
                authentication_mode="meta_hmac",
                authenticated_tenant=authenticated_tenant,
                claimed_tenant=None,
            )
            return

        phone_id, tenant_id, is_active, credentials = signed_candidates[0]
        asserted = body.get("tenant_id")
        if asserted is not None and _optional_uuid(asserted) != tenant_id:
            await self._reject(
                scope,
                receive,
                send,
                authentication_mode="meta_hmac",
                authenticated_tenant=tenant_id,
                claimed_tenant=_optional_uuid(asserted),
            )
            return

        state = _state(scope)
        state["tenant"] = TenantContext(
            tenant_id=tenant_id,
            is_active=is_active,
            authentication_mode="meta_hmac",
        )
        state["canonical_tenant_id"] = tenant_id
        state["meta_phone_number_id"] = phone_id
        state["meta_credentials"] = credentials
        publish_credit_identity(state, credit_identity("meta", tenant_id))
        await self.app(scope, guarded_receive, send)

    async def _reject(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        authentication_mode: str,
        authenticated_tenant: UUID | None,
        claimed_tenant: UUID | None,
    ) -> None:
        audit_tenant_identity_mismatch(
            request_id=cast(str | None, _state(scope).get("request_id")),
            route=cast(str, scope.get("path", "unresolved")),
            method=cast(str, scope.get("method", "unresolved")),
            authentication_mode=authentication_mode,
            authenticated_tenant=authenticated_tenant,
            claimed_tenant=claimed_tenant,
        )
        await tenant_identity_mismatch_response()(scope, receive, send)


MetaCandidate = (
    tuple[str, UUID, dict[str, Any]] | tuple[str, UUID, bool, dict[str, Any]]
)


async def _load_meta_candidates(
    receiver_ids: set[str],
) -> list[MetaCandidate]:
    """Load candidate keys for receiver hints without authorizing a tenant."""

    session = get_db_session()
    try:
        stmt = select(
            ClinicConfigTable.tenant_id,
            ClinicConfigTable.api_credentials,
            Tenant.is_active,
        ).join(Tenant, Tenant.id == ClinicConfigTable.tenant_id)
        if receiver_ids:
            stmt = stmt.where(
                ClinicConfigTable.api_credentials["meta_phone_number_id"].astext.in_(
                    receiver_ids
                )
            )
        result = await session.execute(stmt)
        candidates: list[MetaCandidate] = []
        for tenant_id, raw_credentials, is_active in result.all():
            credentials = dict(raw_credentials or {})
            phone_id = _strict_nonempty_string(credentials.get("meta_phone_number_id"))
            if phone_id is not None:
                candidates.append((phone_id, tenant_id, bool(is_active), credentials))
        return candidates
    finally:
        await session.close()


def _normalise_meta_candidate(
    candidate: MetaCandidate,
) -> tuple[str, UUID, bool, dict[str, Any]]:
    """Accept the original three-field test seam and active-aware DB rows."""

    if len(candidate) == 3:
        phone_id, tenant_id, credentials = candidate
        return phone_id, tenant_id, True, credentials
    phone_id, tenant_id, is_active, credentials = candidate
    return phone_id, tenant_id, is_active, credentials


def _meta_candidate_secrets(credentials: dict[str, Any]) -> tuple[str, ...]:
    """Return distinct secrets explicitly associated with one candidate."""

    current = _strict_nonempty_string(credentials.get("meta_app_secret"))
    if current is None:
        return ()
    return (current,)


def _extract_meta_receiver_claims(
    body: dict[str, Any] | None,
) -> tuple[list[tuple[str, str | None]], bool]:
    """Collect every safe receiver hint while retaining malformed evidence."""

    receivers: list[tuple[str, str | None]] = []
    malformed = False
    if body is None:
        return receivers, True
    entries = body.get("entry")
    if not isinstance(entries, list):
        return receivers, True
    for entry in entries:
        if not isinstance(entry, dict):
            malformed = True
            continue
        raw_waba_id = entry.get("id")
        waba_id = None if raw_waba_id is None else _strict_nonempty_string(raw_waba_id)
        if raw_waba_id is not None and waba_id is None:
            malformed = True
        changes = entry.get("changes")
        if not isinstance(changes, list) or not changes:
            malformed = True
            continue
        for change in changes:
            if not isinstance(change, dict):
                malformed = True
                continue
            if change.get("field") != "messages":
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                malformed = True
                continue
            metadata = value.get("metadata")
            if not isinstance(metadata, dict):
                malformed = True
                continue
            phone_id = _strict_nonempty_string(metadata.get("phone_number_id"))
            if phone_id is None:
                malformed = True
                continue
            messages = value.get("messages")
            statuses = value.get("statuses")
            if messages is None and statuses is None:
                continue
            if messages is not None and not isinstance(messages, list):
                malformed = True
                continue
            if statuses is not None and not isinstance(statuses, list):
                malformed = True
                continue
            receivers.append((phone_id, waba_id))
    return receivers, malformed


def _strict_nonempty_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _verify_meta_signature(
    signature_header: str | None, body_bytes: bytes, app_secret: str
) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    provided = signature_header.removeprefix("sha256=").strip().lower()
    if len(provided) != 64:
        return False
    try:
        bytes.fromhex(provided)
    except ValueError:
        return False
    expected = hmac.new(
        app_secret.encode("utf-8"), body_bytes, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, provided)


async def _capture_body(receive: Receive) -> tuple[bytes, Message | None]:
    body = bytearray()
    terminal: Message | None = None
    while True:
        message = await receive()
        if message["type"] != "http.request":
            terminal = message
            break
        body.extend(cast(bytes, message.get("body", b"")))
        if not message.get("more_body", False):
            break
    return bytes(body), terminal


async def _bounded_body(scope: Scope, receive: Receive) -> tuple[bytes, Receive]:
    """Reuse the outer cap's exact bytes; capture only in isolated middleware use."""

    bounded = _state(scope).get("bounded_ingress_body")
    if isinstance(bounded, bytes):
        return bounded, receive
    raw_body, terminal = await _capture_body(receive)
    return raw_body, _replay_messages(raw_body, terminal, receive)


def _replay_messages(
    body: bytes, terminal: Message | None, fallback_receive: Receive
) -> Callable[[], Awaitable[Message]]:
    index = 0

    async def replay() -> Message:
        nonlocal index
        if index == 0:
            index = 1
            return {"type": "http.request", "body": body, "more_body": False}
        if index == 1 and terminal is not None:
            index = 2
            return terminal
        return await fallback_receive()

    return replay


def _json_object(raw_body: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(raw_body)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _optional_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _scope_tenant(scope: Scope) -> TenantContext | None:
    value = _state(scope).get("tenant")
    return value if isinstance(value, TenantContext) else None


def _state(scope: Scope) -> dict[str, Any]:
    state = scope.setdefault("state", {})
    return cast(dict[str, Any], state)


def _header(scope: Scope, name: str) -> str | None:
    target = name.lower().encode("latin-1")
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() == target:
            return cast(str, raw_value.decode("latin-1"))
    return None


async def _invalid_meta_auth(scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse(
        status_code=401,
        content={"error": "invalid_webhook_auth"},
    )
    await response(scope, receive, send)


async def _invalid_meta_envelope(scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse(
        status_code=400,
        content={"error": "malformed_webhook_envelope"},
    )
    await response(scope, receive, send)


async def _invalid_json(scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse(
        status_code=400,
        content={
            "error": "invalid_json",
            "message": "Request body must be a JSON object",
        },
    )
    await response(scope, receive, send)
