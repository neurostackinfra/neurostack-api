"""Credit operational admin API (Group 5): roles, signed authorizations,
legal hold events and the (disabled) purge.

Mounted at ``/v1/admin/credits``.  A credit surface (``credit_router``): with
the credit switch not effective every route answers ``503 {"code":
"credit_unavailable"}`` before any dependency or database work.

* ``POST /role-assignments`` / ``POST /role-assignments/{id}/revocations`` /
  ``GET /role-assignments`` -- a JWT ``super_admin`` only; a grant is never
  for the caller itself (moved here from Group 1a, with the issuance below).
* ``POST /authorizations`` -- a JWT ``super_admin`` issues one signed,
  single-use authorization to ANOTHER user; an absent or short
  ``credit_authorization_signing_key`` fails closed (503).
* ``POST /holds/{request|activate|release}`` -- the authenticated JWT user
  (the actor; never a body field) with the persisted role and a valid
  authorization; one event per ``SERIALIZABLE`` transaction.
* ``POST /purges`` -- ``409 {"code": "purge_disabled"}`` for every call of a
  JWT ``super_admin``, before reading the body or touching anything.
* ``POST /courtesy-grants``, ``POST /courtesy-grants/{lot_id}/removals`` and
  ``GET /courtesy-grants?builder_id=`` (creditos-cortesia) -- a JWT
  ``super_admin`` gives a builder courtesy credits, takes them back and reads
  them (:mod:`src.services.credit_courtesy`).  Besides the switch, these
  three need the schema at ``046``: their route class captures
  ``credit_switch_reading()`` ONCE, before the body is read or any
  dependency or database work, and answers ``503 credit_unavailable`` unless
  ``courtesy_gate_open`` says yes (wish Decision 6; a database at ``045``
  keeps every other credit surface on).

Refusals answer ``{"code": ...}`` with nothing written.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta, timezone
from typing import Any, Final, TypeVar
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import TenantContext, get_tenant_context
from src.api.routes.credits import CreditSnapshot, lot_view, read_builder_snapshot
from src.api.schemas.admin_credits import (
    AuthorizationCreate,
    AuthorizationResponse,
    CourtesyEventItem,
    CourtesyGrantCreate,
    CourtesyGrantResponse,
    CourtesyGrantsResponse,
    CourtesyLotItem,
    CourtesyRemovalCreate,
    CourtesyRemovalResponse,
    CreditBindingStatus,
    CreditCapabilityStatus,
    CreditReconciliationStatus,
    CreditRolloutStatus,
    HoldEventCreate,
    HoldEventResponse,
    RefusalResponse,
    RoleAssignmentCreate,
    RoleAssignmentItem,
    RoleAssignmentResponse,
    RoleAssignmentRevoke,
)
from src.config.logging import get_logger
from src.config.settings import settings
from src.models.credit import COURTESY_IDEMPOTENCY_KEY_MAX_LENGTH, CreditCourtesyEvent
from src.repositories.content_retention import ContentRetentionRepository
from src.repositories.credit import CreditRepository
from src.repositories.database import get_db_session_dependency
from src.services import content_retention, credit_courtesy
from src.services.content_retention import (
    Actor,
    HoldEventType,
    PresentedAuthorization,
    RetentionRefused,
)
from src.services.credit_courtesy import CourtesyRefused, LedgerClockAhead
from src.services.credit_rollout import CreditRolloutService
from src.services.credit_switch import (
    CreditSwitchRoute,
    courtesy_gate_open,
    credit_router,
    credit_switch_reading,
    credit_unavailable_response,
)

logger = get_logger(__name__)

router = credit_router()

_Endpoint = TypeVar("_Endpoint", bound=Callable[..., Any])

_REFUSALS: dict[int | str, dict[str, Any]] = {
    403: {"model": RefusalResponse, "description": "Refused (nothing written)."},
    409: {"model": RefusalResponse, "description": "Conflict (nothing written)."},
    503: {"model": RefusalResponse, "description": "credit_unavailable or key."},
}


def _actor(tenant: TenantContext) -> Actor:
    """The caller from the verified JWT; an API key is never an actor."""

    if tenant.api_key or tenant.authentication_mode != "jwt" or tenant.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail={"code": "jwt_required"}
        )
    return Actor(user_id=tenant.user_id, role=tenant.role, jwt=True)


def _super_admin(tenant: TenantContext = Depends(get_tenant_context)) -> Actor:
    actor = _actor(tenant)
    if not actor.super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "super_admin_jwt_required"},
        )
    return actor


def _jwt_actor(tenant: TenantContext = Depends(get_tenant_context)) -> Actor:
    return _actor(tenant)


def _refusal(error: RetentionRefused) -> JSONResponse:
    return JSONResponse(status_code=error.status, content={"code": error.code})


@router.get(
    "/rollout/reconciliation",
    response_model=CreditReconciliationStatus,
    responses=_REFUSALS,
)
async def credit_reconciliation_status(
    actor: Actor = Depends(_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> CreditReconciliationStatus:
    """Technical evidence only; never presents facts or changes ASAAS state."""

    del actor
    report = await CreditRolloutService(session).reconciliation_report()
    await session.rollback()
    return CreditReconciliationStatus(**report.counts(), ready=report.ready)


@router.get(
    "/rollout/bindings/{builder_id}",
    response_model=CreditBindingStatus,
    responses=_REFUSALS,
)
async def credit_binding_status(
    builder_id: UUID,
    actor: Actor = Depends(_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> CreditBindingStatus:
    """Read a builder's non-authoritative credit source coverage only."""

    del actor
    coverage = await CreditRolloutService(session).binding_coverage(
        builder_id, include_courtesy=True
    )
    await session.rollback()
    return CreditBindingStatus(
        builder_id=builder_id, **coverage.__dict__, usable=coverage.usable
    )


@router.get(
    "/rollout/status",
    response_model=CreditRolloutStatus,
    responses=_REFUSALS,
)
async def credit_capability_status(
    actor: Actor = Depends(_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> CreditRolloutStatus:
    """Read code readiness; neither records a receipt nor arms enforcement.

    Orphans are the FULL-history scan (set-based, a constant number of
    statements, human-triggered), so an orphan older than the periodic window
    stays visible here until it closes.
    """

    del actor
    rollout = CreditRolloutService(session)
    readiness = await rollout.capability_ready()
    orphan = await rollout.orphan_report()
    enforcement_at = await CreditRepository(session).enforcement_at()
    await session.rollback()
    return CreditRolloutStatus(
        enforcement_at=enforcement_at,
        capability_ready=readiness.ready,
        reconciliation_ready=readiness.reconciliation_complete,
        orphan_effects=orphan.open_count,
    )


@router.get(
    "/rollout/readiness",
    response_model=CreditCapabilityStatus,
    responses=_REFUSALS,
)
async def credit_capability_readiness(
    actor: Actor = Depends(_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> CreditCapabilityStatus:
    """Detailed code-only capability calculation, intentionally not recorded."""

    del actor
    readiness = await CreditRolloutService(session).capability_ready()
    await session.rollback()
    return CreditCapabilityStatus(
        environment=readiness.environment,
        billing_fact_schema_digest=readiness.billing_fact_schema_digest,
        inbox_high_watermark=readiness.inbox_high_watermark,
        reconciliation_complete=readiness.reconciliation_complete,
        readback_complete=readiness.readback_complete,
        has_open_gap=readiness.has_open_gap,
        has_backlog=readiness.has_backlog,
        has_alert=readiness.has_alert,
        ready=readiness.ready,
    )


@router.post(
    "/role-assignments",
    status_code=status.HTTP_201_CREATED,
    response_model=RoleAssignmentResponse,
    responses=_REFUSALS,
)
async def grant_role(
    body: RoleAssignmentCreate,
    actor: Actor = Depends(_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> Any:
    try:
        assignment_id = await content_retention.grant_assignment(
            session,
            actor,
            role=body.role,
            user_id=body.user_id,
            reason=body.reason,
            expires_at=body.expires_at,
        )
        await session.commit()
    except RetentionRefused as error:
        await session.rollback()
        return _refusal(error)
    return RoleAssignmentResponse(assignment_id=assignment_id)


@router.post(
    "/role-assignments/{assignment_id}/revocations",
    status_code=status.HTTP_201_CREATED,
    response_model=RoleAssignmentResponse,
    responses=_REFUSALS,
)
async def revoke_role(
    assignment_id: UUID,
    body: RoleAssignmentRevoke,
    actor: Actor = Depends(_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> Any:
    try:
        revocation = await content_retention.revoke_assignment(
            session, actor, assignment_id=assignment_id, reason=body.reason
        )
        await session.commit()
    except RetentionRefused as error:
        await session.rollback()
        return _refusal(error)
    return RoleAssignmentResponse(assignment_id=revocation)


@router.get(
    "/role-assignments",
    response_model=list[RoleAssignmentItem],
    responses=_REFUSALS,
)
async def list_roles(
    actor: Actor = Depends(_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> Any:
    del actor
    rows = await ContentRetentionRepository(session).list_assignments(
        settings.environment
    )
    items = [
        RoleAssignmentItem(
            id=row.id,
            role=row.role,
            user_id=row.user_id,
            environment=row.environment,
            action=row.action,
            revokes_assignment_id=row.revokes_assignment_id,
            actor_user_id=row.actor_user_id,
            expires_at=row.expires_at,
            created_at=row.created_at,
        )
        for row in rows
    ]
    # Read-only: end the transaction only after the rows were read (a
    # rollback expires them).
    await session.rollback()
    return items


@router.post(
    "/authorizations",
    status_code=status.HTTP_201_CREATED,
    response_model=AuthorizationResponse,
    responses=_REFUSALS,
)
async def issue_authorization(
    body: AuthorizationCreate,
    actor: Actor = Depends(_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> Any:
    try:
        issued = await content_retention.issue_authorization(
            session,
            actor,
            purpose=body.purpose,
            builder_id=body.builder_id,
            scope=body.scope,
            subject_user_id=body.subject_user_id,
            resource=body.resource,
            fence_generation=body.fence_generation,
            ttl=timedelta(seconds=body.ttl_seconds),
        )
        await session.commit()
    except RetentionRefused as error:
        await session.rollback()
        return _refusal(error)
    return AuthorizationResponse(
        authorization_id=issued.authorization_id,
        signature=issued.signature,
        payload=issued.payload,
    )


async def _hold(
    event_type: HoldEventType,
    body: HoldEventCreate,
    actor: Actor,
    session: AsyncSession,
) -> Any:
    try:
        result = await content_retention.record_hold_event(
            session,
            actor,
            event_type=event_type,
            builder_id=body.builder_id,
            scope=body.scope,
            presented=PresentedAuthorization(
                body.authorization_id, body.authorization_signature
            ),
            reason_code=body.reason_code,
            intent_id=body.intent_id,
            justification=body.justification,
        )
    except RetentionRefused as error:
        return _refusal(error)
    return HoldEventResponse(
        event_id=result.event_id,
        intent_id=result.intent_id,
        event_type=result.event_type,
        generation=result.generation,
    )


@router.post(
    "/holds/request",
    status_code=status.HTTP_201_CREATED,
    response_model=HoldEventResponse,
    responses=_REFUSALS,
)
async def request_hold(
    body: HoldEventCreate,
    actor: Actor = Depends(_jwt_actor),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> Any:
    return await _hold("hold_requested", body, actor, session)


@router.post(
    "/holds/activate",
    status_code=status.HTTP_201_CREATED,
    response_model=HoldEventResponse,
    responses=_REFUSALS,
)
async def activate_hold(
    body: HoldEventCreate,
    actor: Actor = Depends(_jwt_actor),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> Any:
    return await _hold("hold_activated", body, actor, session)


@router.post(
    "/holds/release",
    status_code=status.HTTP_201_CREATED,
    response_model=HoldEventResponse,
    responses=_REFUSALS,
)
async def release_hold(
    body: HoldEventCreate,
    actor: Actor = Depends(_jwt_actor),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> Any:
    return await _hold("hold_released", body, actor, session)


# ---------------------------------------------------------------------------
# Courtesy credits (creditos-cortesia).
# ---------------------------------------------------------------------------

CREDIT_CLOCK_AHEAD_CODE: Final = "credit_clock_ahead"
# DESIGN Decisions 5, 6, 10 and 14: the service's refusal codes (nothing is
# written for any of them).  A removal's builder is the lot's own, so a lot of
# another builder is ``courtesy_lot_not_found`` like an absent one.
_COURTESY_STATUS: Final[dict[str, int]] = {
    "builder_not_found": status.HTTP_404_NOT_FOUND,
    "courtesy_lot_not_found": status.HTTP_404_NOT_FOUND,
    "builder_inactive": status.HTTP_409_CONFLICT,
    "idempotency_key_reused": status.HTTP_409_CONFLICT,
    "courtesy_removal_exceeds_balance": status.HTTP_409_CONFLICT,
}
_COURTESY_SWITCH_DESCRIPTION = (
    "credit_unavailable: the credit switch is not effective, or the schema is "
    "below 046 (checked before the body is read, with no SQL); "
    "credit_clock_ahead (POST only): the ledger clock is ahead, nothing was "
    "written, retry with the same Idempotency-Key."
)
_COURTESY_AUTH: dict[int | str, dict[str, Any]] = {
    401: {"description": "Not authenticated."},
    403: {
        "model": RefusalResponse,
        "description": (
            "jwt_required or super_admin_jwt_required: only a super admin's "
            "JWT, before any credit SQL."
        ),
    },
}
_COURTESY_GRANT_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_COURTESY_AUTH,
    404: {
        "model": RefusalResponse,
        "description": "builder_not_found (nothing written).",
    },
    409: {
        "model": RefusalResponse,
        "description": (
            "builder_inactive, or idempotency_key_reused: the key was used for "
            "another request (nothing written)."
        ),
    },
    503: {"model": RefusalResponse, "description": _COURTESY_SWITCH_DESCRIPTION},
}
_COURTESY_REMOVAL_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_COURTESY_AUTH,
    404: {
        "model": RefusalResponse,
        "description": (
            "courtesy_lot_not_found: no such lot, or a monthly or top-up lot "
            "-- the same answer for all (nothing written)."
        ),
    },
    409: {
        "model": RefusalResponse,
        "description": (
            "courtesy_removal_exceeds_balance: more than the lot can still "
            "spend, or remove_all of an expired or zeroed lot; or "
            "idempotency_key_reused (nothing written)."
        ),
    },
    503: {"model": RefusalResponse, "description": _COURTESY_SWITCH_DESCRIPTION},
}
_COURTESY_READ_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_COURTESY_AUTH,
    404: {"model": RefusalResponse, "description": "builder_not_found."},
    503: {
        "model": RefusalResponse,
        "description": (
            "credit_unavailable: the credit switch is not effective, or the "
            "schema is below 046 (no SQL)."
        ),
    },
}
_IDEMPOTENCY_KEY = Header(
    ...,
    alias="Idempotency-Key",
    min_length=1,
    max_length=COURTESY_IDEMPOTENCY_KEY_MAX_LENGTH,
    pattern=r"^[^\x00]+$",
    description=(
        "1 to 200 characters, unique across every builder: the same key with "
        "the same request answers the recorded result again (nothing "
        "written); with another request it is 409 idempotency_key_reused."
    ),
)


def courtesy_gate_captured() -> bool:
    """The courtesy gate of THIS request: ``credit_switch_reading()`` read
    once and judged by ``courtesy_gate_open`` (no database).  Anything that
    fails is closed, never a 500."""

    try:
        return courtesy_gate_open(credit_switch_reading()) is True
    except Exception as error:
        logger.error(
            "credit courtesy gate unreadable; treating it as closed",
            error_type=type(error).__name__,
        )
        return False


class CourtesyGateRoute(CreditSwitchRoute):
    """A courtesy route: ``503 {"code": "credit_unavailable"}`` unless the
    switch is effective AND the schema is at ``046`` or later.

    The gate is judged once per request, before FastAPI reads or parses the
    body, before any dependency (authentication included) and so before any
    SQL; then the switch check of :class:`CreditSwitchRoute` runs as for
    every credit surface."""

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        guarded = super().get_route_handler()

        async def courtesy_route_handler(request: Request) -> Response:
            if not courtesy_gate_captured():
                return credit_unavailable_response()
            return await guarded(request)

        return courtesy_route_handler


def _courtesy_route(
    method: str, path: str, **options: Any
) -> Callable[[_Endpoint], _Endpoint]:
    def register(endpoint: _Endpoint) -> _Endpoint:
        router.add_api_route(
            path,
            endpoint,
            methods=[method],
            route_class_override=CourtesyGateRoute,
            **options,
        )
        return endpoint

    return register


def _courtesy_refusal(error: CourtesyRefused) -> JSONResponse:
    return JSONResponse(
        status_code=_COURTESY_STATUS.get(error.code, status.HTTP_409_CONFLICT),
        content={"code": error.code},
    )


def _clock_ahead() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"code": CREDIT_CLOCK_AHEAD_CODE},
    )


def _utc(value: datetime) -> datetime:
    """SQLite drops the zone of TIMESTAMPTZ; its stored values are UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@_courtesy_route(
    "POST",
    "/courtesy-grants",
    status_code=status.HTTP_201_CREATED,
    response_model=CourtesyGrantResponse,
    responses=_COURTESY_GRANT_RESPONSES,
    summary="Grant courtesy credits",
    description=(
        "A super admin gives an active builder 1 to 100,000 courtesy credits, "
        "valid until the start of the civil day (America/Sao_Paulo) of the "
        "grant + 30 days. Open debt is settled first. No subscription, cycle, "
        "order, payment or ASAAS row is created. Requires Idempotency-Key."
    ),
)
async def grant_courtesy(
    body: CourtesyGrantCreate,
    idempotency_key: str = _IDEMPOTENCY_KEY,
    actor: Actor = Depends(_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> Any:
    try:
        result = await credit_courtesy.grant(
            session,
            builder_id=body.builder_id,
            credits=body.credits,
            reason=body.reason,
            actor_user_id=actor.user_id,
            idempotency_key=idempotency_key,
        )
        if result.replayed:
            await session.rollback()
        else:
            await session.commit()
    except CourtesyRefused as error:
        await session.rollback()
        return _courtesy_refusal(error)
    except LedgerClockAhead:
        await session.rollback()
        return _clock_ahead()
    except BaseException:
        await session.rollback()
        raise
    return CourtesyGrantResponse(
        event_id=result.event_id,
        builder_id=result.builder_id,
        lot_id=result.lot_id,
        granted_mc=result.granted_mc,
        expires_at=_utc(result.expires_at),
        occurred_at=_utc(result.occurred_at),
        actor_user_id=result.actor_user_id,
    )


@_courtesy_route(
    "POST",
    "/courtesy-grants/{lot_id}/removals",
    status_code=status.HTTP_201_CREATED,
    response_model=CourtesyRemovalResponse,
    responses=_COURTESY_REMOVAL_RESPONSES,
    summary="Remove courtesy credits",
    description=(
        "A super admin takes back N whole credits ({credits: N}, at most the "
        "lot's whole spendable credits) or the whole spendable balance "
        "({remove_all: true}) of a courtesy lot. Never what was consumed, "
        "never debt; allowed for an inactive builder. Requires "
        "Idempotency-Key."
    ),
)
async def remove_courtesy(
    lot_id: UUID,
    body: CourtesyRemovalCreate,
    idempotency_key: str = _IDEMPOTENCY_KEY,
    actor: Actor = Depends(_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> Any:
    try:
        result = await credit_courtesy.remove(
            session,
            lot_id=lot_id,
            reason=body.reason,
            actor_user_id=actor.user_id,
            idempotency_key=idempotency_key,
            credits=body.credits,
            remove_all=body.remove_all is True,
        )
        if result.replayed:
            await session.rollback()
        else:
            await session.commit()
    except CourtesyRefused as error:
        await session.rollback()
        return _courtesy_refusal(error)
    except LedgerClockAhead:
        await session.rollback()
        return _clock_ahead()
    except BaseException:
        await session.rollback()
        raise
    return CourtesyRemovalResponse(
        event_id=result.event_id,
        builder_id=result.builder_id,
        lot_id=result.lot_id,
        removed_mc=result.removed_mc,
        occurred_at=_utc(result.occurred_at),
        actor_user_id=result.actor_user_id,
    )


@_courtesy_route(
    "GET",
    "/courtesy-grants",
    response_model=CourtesyGrantsResponse,
    responses=_COURTESY_READ_RESPONSES,
    summary="Courtesy credits of a builder",
    description=(
        "The builder's courtesy lots (granted, available, consumed, removed, "
        "expired, expiry), swept at the database clock in one read-only "
        "snapshot, and every grant and removal with its actor and reason."
    ),
)
async def list_courtesy_grants(
    response: Response,
    builder_id: UUID = Query(..., description="The builder whose courtesy to read."),
    actor: Actor = Depends(_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> Any:
    del actor
    response.headers["Cache-Control"] = "no-store"

    async def build(snapshot: CreditSnapshot) -> CourtesyGrantsResponse | None:
        if await snapshot.repository.builder_is_active(builder_id) is None:
            return None
        book = await snapshot.book()
        as_of = await snapshot.as_of()
        lots = [
            CourtesyLotItem(
                **lot_view(lot, as_of).model_dump(
                    include={
                        "lot_id",
                        "kind",
                        "status",
                        "granted_mc",
                        "available_mc",
                        "consumed_mc",
                        "removed_mc",
                        "expired_mc",
                        "paid_at",
                        "expires_at",
                    }
                )
            )
            for lot in ([] if book is None else book.lots)
            if lot.lot_kind == "courtesy"
        ]
        event = CreditCourtesyEvent
        rows = await snapshot.session.execute(
            select(
                event.id,
                event.lot_id,
                event.event_kind,
                event.amount_mc,
                event.reason,
                event.actor_user_id,
                event.occurred_at,
            )
            .where(event.builder_id == builder_id)
            .order_by(event.occurred_at, event.id)
        )
        events = [
            CourtesyEventItem(
                event_id=row.id,
                lot_id=row.lot_id,
                kind=row.event_kind,
                amount_mc=int(row.amount_mc),
                reason=row.reason,
                actor_user_id=row.actor_user_id,
                occurred_at=_utc(row.occurred_at),
            )
            for row in rows
        ]
        return CourtesyGrantsResponse(as_of=as_of, lots=lots, events=events)

    found = await read_builder_snapshot(session, builder_id, build)
    if found is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"code": "builder_not_found"},
            headers={"Cache-Control": "no-store"},
        )
    return found


@router.post(
    "/purges",
    status_code=status.HTTP_409_CONFLICT,
    response_model=RefusalResponse,
    responses=_REFUSALS,
)
async def purge(request: Request, actor: Actor = Depends(_super_admin)) -> Any:
    """Always ``purge_disabled`` (Groups 1a-7): the body is never read; no
    hold, fence, storage, authorization or receipt is touched."""

    del request, actor
    outcome = await content_retention.request_purge()
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT, content={"code": outcome.code}
    )


__all__ = ["CREDIT_CLOCK_AHEAD_CODE", "CourtesyGateRoute", "router"]
