"""
NeuroStack API - Billing Endpoints
Plan summary, hosted ASAAS checkout, ASAAS webhook receiver, and coupon validation.
"""

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
)
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import require_builder
from src.api.schemas.billing import (
    BillingCatalogOfferResponse,
    BillingCatalogResponse,
    BillingCheckoutSummaryResponse,
    BillingLiveCheckoutResponse,
    BillingSubscriptionSummaryResponse,
    BillingSummaryResponse,
    BillingUsageResponse,
    CheckoutReconciliationResponse,
    CheckoutRequest,
    CheckoutResponse,
    CouponValidationResponse,
)
from src.config.logging import get_logger
from src.config.settings import settings
from src.models.builder import Builder
from src.models.coupon import Coupon
from src.models.tenant import Tenant
from src.repositories import get_db_session_dependency
from src.repositories.database import get_db_session
from src.services.asaas import AsaasClient
from src.services.billing_catalog import catalog_snapshot
from src.services.billing_checkout import (
    BillingCheckoutService,
    CheckoutCouponRejected,
    CheckoutCreationRejected,
    CheckoutInputRejected,
    CheckoutNotEligible,
    CheckoutNotFound,
    CheckoutReconciliationRequired,
    CheckoutUnavailable,
)
from src.services.billing_read_contract import (
    BillingReadUnavailable,
    read_commercial_availability,
    read_current_subscription,
    read_live_checkout,
)
from src.services.billing_usage_service import (
    PlanUsageLimits,
    get_builder_billing_usage,
)
from src.services.billing_webhook import (
    ASAAS_INGRESS_BODY_STATE,
    ASAAS_INGRESS_PAYLOAD_STATE,
    ASAAS_WEBHOOK_READINESS,
    AsaasWebhookProvisioner,
    BillingWebhookService,
    WebhookEnvelopeRejected,
    WebhookProcessingUnavailable,
    WebhookReadiness,
    desired_webhook_settings,
    parse_envelope,
    record_rejection,
    run_delivery_readback,
)

logger = get_logger(__name__)

router = APIRouter()

BASE_PRICE = 2000
EXTRA_PER_CLINIC = 350
FREE_TIER = 2
ANNUAL_DISCOUNT_PCT = 15


def _asaas_client() -> AsaasClient:
    """Build the only client permitted on the new hosted-checkout path."""

    return AsaasClient(
        api_key=settings.asaas_api_key,
        user_agent=settings.asaas_user_agent,
    )


async def _read_back_webhook_readiness() -> WebhookReadiness:
    """Read-only provider readback of this environment's webhook and token."""

    desired = desired_webhook_settings(
        environment=settings.environment,
        url=settings.asaas_webhook_url,
        email=settings.asaas_webhook_notification_email,
    )
    return await AsaasWebhookProvisioner(
        _asaas_client(), desired, auth_token=settings.asaas_webhook_token
    ).readback()


def _asaas_checkout_client() -> AsaasClient:
    """Creation client: only built while the webhook reads back ready.

    ``BillingCheckoutService`` calls this among its side-effect-free creation
    preconditions (after the rollout pre-check and the egress flag, before any
    reservation), so a missing, drifted or unverified provider webhook/token
    blocks hosted checkout with zero rows and zero POSTs.  Reconciliation and
    readback use ``_asaas_client`` and are never gated.
    """

    ASAAS_WEBHOOK_READINESS.require_ready(_read_back_webhook_readiness)
    return _asaas_client()


# Longest a checkout request waits for a stale verdict's readback.
WEBHOOK_READBACK_WAIT_SECONDS = 3.0


async def _ensure_webhook_ready() -> None:
    """Creation precondition: refresh a stale verdict, then require ready.

    Runs after the rollout pre-check and the egress flag, before any lock,
    reservation or POST, with no open transaction.  A failed or slow readback
    keeps the gate closed (503, zero rows, zero POSTs).
    """

    await ASAAS_WEBHOOK_READINESS.ensure_fresh(
        _read_back_webhook_readiness, timeout=WEBHOOK_READBACK_WAIT_SECONDS
    )
    ASAAS_WEBHOOK_READINESS.require_ready()


async def _count_active_clinics(builder_id: UUID, db: AsyncSession) -> int:
    result = await db.execute(
        select(func.count(Tenant.id)).where(
            Tenant.builder_id == builder_id,
            Tenant.is_active.is_(True),
            Tenant.plan_type != "admin",
        )
    )
    return result.scalar() or 0


def _start_plan_usage_limits() -> PlanUsageLimits:
    """Resolve Start-plan limits from deployment configuration."""
    return PlanUsageLimits(
        monthly_traces=settings.start_plan_monthly_trace_limit,
        rag_storage_mb=settings.start_plan_rag_storage_limit_mb,
        media_storage_mb=settings.start_plan_media_storage_limit_mb,
    )


async def _get_builder(builder_id: UUID, db: AsyncSession) -> Builder:
    result = await db.execute(select(Builder).where(Builder.id == builder_id))
    builder = result.scalar_one_or_none()
    if not builder:
        raise HTTPException(status_code=404, detail="Builder not found")
    return builder


async def _get_usage(
    builder: Builder,
    db: AsyncSession,
) -> BillingUsageResponse:
    usage = await get_builder_billing_usage(
        db,
        builder.id,
        builder.subscription_plan,
        _start_plan_usage_limits(),
    )
    return BillingUsageResponse.model_validate(usage)


def _calc_pricing(active_clinics: int) -> dict[str, int]:
    extra = max(0, active_clinics - FREE_TIER)
    extra_price = extra * EXTRA_PER_CLINIC
    total_monthly = BASE_PRICE + extra_price
    total_annual = int(total_monthly * 12 * (1 - ANNUAL_DISCOUNT_PCT / 100))
    return {
        "active_clinics": active_clinics,
        "free_tier": FREE_TIER,
        "extra_clinics": extra,
        "base_price": BASE_PRICE,
        "extra_price": extra_price,
        "total_monthly": total_monthly,
        "total_annual": total_annual,
        "discount_pct": ANNUAL_DISCOUNT_PCT,
    }


def _no_store(response: Response) -> None:
    """Forbid browsers and shared caches from storing the builder's summary.

    It is per builder and time-dependent (``payment_state`` and the cancelled
    paid window move with the São Paulo date), so no copy may be reused.
    """

    response.headers["Cache-Control"] = "no-store"


@router.get(
    "/summary",
    summary="Billing Summary",
    response_model=BillingSummaryResponse,
    dependencies=[Depends(_no_store)],
)
async def billing_summary(
    builder_id: UUID = Depends(require_builder),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> BillingSummaryResponse:
    builder = await _get_builder(builder_id, db)

    active_clinics = await _count_active_clinics(builder_id, db)
    pricing = _calc_pricing(active_clinics)
    usage = await _get_usage(builder, db)
    # Legacy values are captured before the Group 6b reads run.
    plan = builder.subscription_plan
    subscription_status = builder.subscription_status
    billing_cycle = builder.billing_cycle
    subscription, checkout, topup_enabled = await _summary_read_contract(builder_id, db)

    return BillingSummaryResponse(
        plan=plan,
        subscription_status=subscription_status,
        billing_cycle=billing_cycle,
        usage=usage,
        **pricing,
        subscription=subscription,
        checkout=checkout,
        topup_commercial_enabled=topup_enabled,
    )


async def _summary_read_contract(builder_id: UUID, db: AsyncSession) -> tuple[
    BillingSubscriptionSummaryResponse | None,
    BillingCheckoutSummaryResponse,
    bool,
]:
    """Group 6b summary fields: read-only, lock-free, scoped to the JWT builder.

    The financial reads (subscription record, live attempt) fail the request
    closed (503) rather than reporting "no subscription"; the advisory
    availability read answers closed by itself (``checkout.available`` false,
    no offer, no live session, top-up false) and runs last, so a failed
    statement there cannot poison the reads above.
    """

    try:
        subscription = await read_current_subscription(db, builder_id=builder_id)
        live = await read_live_checkout(db, builder_id=builder_id)
    except BillingReadUnavailable as exc:
        raise HTTPException(status_code=503, detail=exc.code) from exc
    except Exception as exc:
        logger.warning("Billing summary financial read unavailable")
        raise HTTPException(
            status_code=503, detail=BillingReadUnavailable.code
        ) from exc
    commercial = await read_commercial_availability(db, builder_id=builder_id)
    return (
        (
            None
            if subscription is None
            else BillingSubscriptionSummaryResponse(
                offer_id=subscription.offer_id,
                catalog_version_id=subscription.catalog_version_id,
                status=subscription.status,
                amount_cents=subscription.amount_cents,
                current_period_end=subscription.current_period_end,
                payment_state=subscription.payment_state,
            )
        ),
        BillingCheckoutSummaryResponse(
            available_offer_ids=list(commercial.available_offer_ids),
            # A failed advisory read closes the whole block for buying.
            live_session=(
                None
                if live is None or not commercial.available
                else BillingLiveCheckoutResponse(
                    checkout_session_id=str(live.checkout_session_id),
                    kind=live.kind,
                    state=live.state,
                    offer_id=live.offer_id,
                )
            ),
            available=commercial.available,
        ),
        commercial.topup_commercial_enabled,
    )


_CATALOG_KINDS: dict[str, Literal["subscription", "topup"]] = {
    "MONTHLY": "subscription",
    "DETACHED": "topup",
}


@router.get(
    "/catalog",
    summary="Billing Catalog",
    response_model=BillingCatalogResponse,
)
async def billing_catalog(
    builder_id: UUID = Depends(require_builder),
) -> BillingCatalogResponse:
    """Return the immutable backend-owned catalog; prices are never computed
    by the client.  Builder login is required; the response is identical for
    every builder of a deployment."""

    del builder_id
    snapshot = catalog_snapshot()
    offers = snapshot["offers"]
    assert isinstance(offers, list)
    return BillingCatalogResponse(
        catalog_version_id=str(snapshot["catalog_version_id"]),
        offers=[
            BillingCatalogOfferResponse(
                offer_id=offer["offer_id"],
                kind=_CATALOG_KINDS[offer["cycle"]],
                currency="BRL",
                amount_cents=offer["amount_cents"],
                credits=offer["credits"],
                included_clinics=offer["included_clinics"],
                additional_clinic_amount_cents=offer["additional_clinic_amount_cents"],
            )
            for offer in offers
        ],
    )


@router.get(
    "/usage",
    summary="Builder Plan Usage",
    response_model=BillingUsageResponse,
)
async def billing_usage(
    builder_id: UUID = Depends(require_builder),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> BillingUsageResponse:
    """Return monthly trace and current storage usage for the builder."""
    builder = await _get_builder(builder_id, db)
    return await _get_usage(builder, db)


@router.get(
    "/coupon/{code}",
    summary="Validate Coupon",
    response_model=CouponValidationResponse,
)
async def validate_coupon(
    code: str,
    builder_id: UUID = Depends(require_builder),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> CouponValidationResponse:
    result = await db.execute(select(Coupon).where(Coupon.code == code.upper()))
    coupon = result.scalar_one_or_none()
    if not coupon or not coupon.is_active:
        return CouponValidationResponse(valid=False, message="Cupom inválido")
    if coupon.expires_at and coupon.expires_at < datetime.now(timezone.utc):
        return CouponValidationResponse(valid=False, message="Cupom expirado")
    if coupon.max_uses is not None and coupon.uses_count >= coupon.max_uses:
        return CouponValidationResponse(valid=False, message="Cupom esgotado")
    return CouponValidationResponse(
        valid=True,
        code=coupon.code,
        discount_percent=coupon.discount_percent,
    )


@router.post(
    "/checkout",
    summary="Create Checkout",
    response_model=CheckoutResponse,
    responses={
        202: {
            "model": CheckoutReconciliationResponse,
            "description": "Outcome uncertain: reconcile by checkout_session_id.",
        }
    },
)
async def create_checkout(
    body: CheckoutRequest,
    builder_id: UUID = Depends(require_builder),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> CheckoutResponse | JSONResponse:
    """Start a provider-hosted checkout from server-owned commercial state."""

    service = BillingCheckoutService(
        db,
        client_factory=_asaas_checkout_client,
        creation_precondition=_ensure_webhook_ready,
    )
    try:
        checkout = await service.create_checkout(
            builder_id=builder_id,
            offer_id=body.offer_id,
            payment_method=body.method,
            coupon_code=body.coupon_code,
        )
    except CheckoutInputRejected as exc:
        raise HTTPException(status_code=422, detail=exc.code) from exc
    except CheckoutNotEligible as exc:
        # One fixed 409 for every subscription eligibility blocker (unresolved,
        # legacy or non-terminal ASAAS subscription, other live plan attempt).
        raise HTTPException(status_code=409, detail=exc.code) from exc
    except CheckoutCouponRejected as exc:
        # One fixed 400 for absent/inactive/expired/exhausted (no enumeration),
        # matching the legacy coupon route's 400 family.
        raise HTTPException(status_code=400, detail=exc.code) from exc
    except CheckoutUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail=exc.code,
        ) from exc
    except CheckoutCreationRejected as exc:
        raise HTTPException(status_code=502, detail=exc.code) from exc
    except CheckoutReconciliationRequired as exc:
        if exc.checkout_session_id is None:
            raise HTTPException(status_code=202, detail=exc.code) from exc
        # Group 6b: the legacy ``{"detail": code}`` body plus the stored
        # reservation id (the builder's own session UUID).
        return JSONResponse(
            status_code=202,
            content=CheckoutReconciliationResponse.model_validate(
                {
                    "detail": exc.code,
                    "checkout_session_id": str(exc.checkout_session_id),
                }
            ).model_dump(),
        )
    return CheckoutResponse(
        checkout_session_id=str(checkout.checkout_session_id),
        kind=checkout.kind,
        state=checkout.state,
        checkout_url=checkout.checkout_url,
    )


@router.get(
    "/checkout/{checkout_session_id}",
    summary="Get Hosted Checkout",
    response_model=CheckoutResponse,
)
async def get_checkout(
    checkout_session_id: UUID,
    builder_id: UUID = Depends(require_builder),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> CheckoutResponse:
    """Return only the caller's session; foreign IDs are indistinguishable 404s."""

    try:
        checkout = await BillingCheckoutService(
            db, client_factory=_asaas_client
        ).get_checkout(
            builder_id=builder_id,
            checkout_session_id=checkout_session_id,
        )
    except CheckoutNotFound as exc:
        raise HTTPException(status_code=404, detail="Checkout not found") from exc
    return CheckoutResponse(
        checkout_session_id=str(checkout.checkout_session_id),
        kind=checkout.kind,
        state=checkout.state,
        checkout_url=checkout.checkout_url,
    )


@router.post(
    "/checkout/{checkout_session_id}/reconcile",
    summary="Reconcile Hosted Checkout",
    response_model=CheckoutResponse,
)
async def reconcile_checkout(
    checkout_session_id: UUID,
    builder_id: UUID = Depends(require_builder),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> CheckoutResponse:
    """Read provider evidence by the session UUID; this endpoint never creates."""

    try:
        checkout = await BillingCheckoutService(
            db, client_factory=_asaas_client
        ).reconcile_checkout(
            builder_id=builder_id,
            checkout_session_id=checkout_session_id,
        )
    except CheckoutNotFound as exc:
        raise HTTPException(status_code=404, detail="Checkout not found") from exc
    except CheckoutReconciliationRequired as exc:
        raise HTTPException(status_code=202, detail=exc.code) from exc
    return CheckoutResponse(
        checkout_session_id=str(checkout.checkout_session_id),
        kind=checkout.kind,
        state=checkout.state,
        checkout_url=checkout.checkout_url,
    )


@router.post(
    "/webhook/asaas",
    summary="ASAAS Webhook",
    response_model=None,
    status_code=200,
)
async def asaas_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, bool]:
    """Persist one authenticated ASAAS delivery; 200 only after commit.

    The ingress boundary has already authenticated ``asaas-access-token`` and
    bounded/parsed the body; without its state this handler fails closed.
    Known events apply their deterministic transition in the same commit;
    readbacks run after the response with their own session.
    """

    state = request.scope.get("state") or {}
    body = state.get(ASAAS_INGRESS_BODY_STATE)
    payload = state.get(ASAAS_INGRESS_PAYLOAD_STATE)
    if not isinstance(body, bytes) or payload is None:
        record_rejection("unavailable")
        raise HTTPException(status_code=503, detail="billing_webhook_unavailable")
    try:
        envelope = parse_envelope(body, payload)
    except WebhookEnvelopeRejected as exc:
        record_rejection("invalid_event")
        raise HTTPException(status_code=400, detail="invalid_event") from exc
    try:
        outcome = await BillingWebhookService(
            db, environment=settings.environment
        ).receive(envelope)
    except WebhookProcessingUnavailable as exc:
        record_rejection("processing_unavailable")
        raise HTTPException(
            status_code=503, detail="billing_webhook_unavailable"
        ) from exc
    if outcome.readback and outcome.delivery_id is not None:
        background_tasks.add_task(
            run_delivery_readback,
            outcome.delivery_id,
            # Resolved inside the task: a fresh session after the response.
            session_factory=get_db_session,
            client_factory=_asaas_client,
            environment=settings.environment,
        )
    return {"ok": True}
