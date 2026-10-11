"""
NeuroStack API - Billing Endpoints
Plan summary, hosted ASAAS checkout, legacy webhook handler, and coupon validation.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import require_builder
from src.api.schemas.billing import (
    BillingSummaryResponse,
    BillingUsageResponse,
    CheckoutRequest,
    CheckoutResponse,
    CouponValidationResponse,
)
from src.config.logging import get_logger
from src.config.settings import settings
from src.models.builder import Builder
from src.models.coupon import Coupon
from src.models.tenant import Tenant
from src.models.transaction import Transaction
from src.repositories import get_db_session_dependency
from src.repositories.billing import billing_roster_advisory_key
from src.services.abacatepay import AbacatePayClient
from src.services.asaas import AsaasClient
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
from src.services.billing_usage_service import (
    PlanUsageLimits,
    get_builder_billing_usage,
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


@dataclass(frozen=True)
class AbacatePayRosterBinding:
    """Canonical legacy entitlement snapshot owned by billing_subscriptions."""

    builder_id: UUID
    plan: str
    billing_cycle: str
    coupon_code: str | None


class AbacatePayRosterUnavailable(RuntimeError):
    """The canonical legacy roster could not be read safely."""


async def _get_abacatepay_roster_binding(
    external_subscription_id: str,
    db: AsyncSession,
) -> AbacatePayRosterBinding | None:
    """Load the future canonical roster snapshot and deny on read/schema drift."""

    if not external_subscription_id:
        raise AbacatePayRosterUnavailable
    try:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:roster_lock_key)"),
            {"roster_lock_key": billing_roster_advisory_key()},
        )
        result = await db.execute(
            text("""
                SELECT
                    roster.billing_roster_revision,
                    subscription.builder_id,
                    subscription.plan,
                    subscription.billing_cycle,
                    subscription.coupon_code,
                    subscription.roster_revision AS subscription_roster_revision
                FROM billing_roster_control AS roster
                LEFT JOIN billing_subscriptions AS subscription
                  ON subscription.provider = 'abacatepay'
                 AND subscription.external_subscription_id = :external_subscription_id
                 AND subscription.is_active = TRUE
                LIMIT 1
            """),
            {"external_subscription_id": external_subscription_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise ValueError("billing roster control is unavailable")
        roster_revision = row["billing_roster_revision"]
        if not isinstance(roster_revision, int) or roster_revision <= 0:
            raise ValueError("billing roster is not initialized")
        builder_id = row["builder_id"]
        if builder_id is None:
            return None
        subscription_roster_revision = row["subscription_roster_revision"]
        if (
            not isinstance(subscription_roster_revision, int)
            or subscription_roster_revision <= 0
            or subscription_roster_revision > roster_revision
        ):
            raise ValueError("invalid canonical roster revision")
        if not isinstance(builder_id, UUID):
            builder_id = UUID(str(builder_id))
        plan = row["plan"]
        billing_cycle = row["billing_cycle"]
        coupon_code = row["coupon_code"]
        if (
            not isinstance(plan, str)
            or not plan
            or not isinstance(billing_cycle, str)
            or not billing_cycle
            or (coupon_code is not None and not isinstance(coupon_code, str))
        ):
            raise ValueError("invalid canonical roster snapshot")
        return AbacatePayRosterBinding(
            builder_id=builder_id,
            plan=plan,
            billing_cycle=billing_cycle,
            coupon_code=coupon_code,
        )
    except Exception as exc:
        logger.warning("AbacatePay roster lookup unavailable")
        raise AbacatePayRosterUnavailable from exc


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


@router.get(
    "/summary",
    summary="Billing Summary",
    response_model=BillingSummaryResponse,
)
async def billing_summary(
    builder_id: UUID = Depends(require_builder),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> BillingSummaryResponse:
    builder = await _get_builder(builder_id, db)

    active_clinics = await _count_active_clinics(builder_id, db)
    pricing = _calc_pricing(active_clinics)
    usage = await _get_usage(builder, db)

    return BillingSummaryResponse(
        plan=builder.subscription_plan,
        subscription_status=builder.subscription_status,
        billing_cycle=builder.billing_cycle,
        usage=usage,
        **pricing,
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
)
async def create_checkout(
    body: CheckoutRequest,
    builder_id: UUID = Depends(require_builder),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> CheckoutResponse:
    """Start a provider-hosted checkout from server-owned commercial state."""

    service = BillingCheckoutService(db, client_factory=_asaas_client)
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
        # One fixed 409 for every subscription eligibility blocker (legacy or
        # non-terminal ASAAS subscription, migration, other live plan attempt).
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
        raise HTTPException(status_code=202, detail=exc.code) from exc
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
    "/webhook",
    summary="AbacatePay Webhook",
    response_model=None,
    status_code=200,
)
async def abacatepay_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, bool]:
    webhook_secret = settings.abacatepay_webhook_secret.get_secret_value()
    if not webhook_secret:
        raise HTTPException(status_code=503, detail="billing_webhook_unavailable")

    signature = request.headers.get("X-Webhook-Signature", "")
    if not signature:
        logger.warning("Webhook signature verification failed")
        raise HTTPException(status_code=400, detail="Invalid signature")

    body = await request.body()

    if not AbacatePayClient.verify_webhook_signature(body, signature, webhook_secret):
        logger.warning("Webhook signature verification failed")
        raise HTTPException(status_code=400, detail="Invalid signature")

    import json

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = payload.get("event", "")
    data = payload.get("data", {})
    metadata = data.get("metadata", {})

    if event_type not in ("billing.paid", "checkout.completed"):
        return {"ok": True, "ignored": True}

    subscription = data.get("subscription", {})
    nested_subscription_id = (
        subscription.get("id") if isinstance(subscription, dict) else None
    )
    external_subscription_id = data.get("subscription_id") or nested_subscription_id
    if not isinstance(external_subscription_id, str) or not external_subscription_id:
        logger.warning("AbacatePay webhook subscription is not in canonical roster")
        return {"ok": True, "ignored": True}
    try:
        binding = await _get_abacatepay_roster_binding(external_subscription_id, db)
    except AbacatePayRosterUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="billing_webhook_unavailable",
        ) from exc
    if binding is None:
        logger.warning("AbacatePay webhook subscription is not in canonical roster")
        return {"ok": True, "ignored": True}

    builder_id_str = metadata.get("builder_id")
    if not isinstance(builder_id_str, str):
        logger.warning("Webhook metadata does not match canonical roster")
        return {"ok": True, "ignored": True}

    try:
        metadata_builder_id = UUID(builder_id_str)
    except (ValueError, AttributeError):
        logger.warning("Webhook metadata does not match canonical roster")
        return {"ok": True, "ignored": True}

    metadata_coupon = metadata.get("coupon_code")
    if (
        metadata_builder_id != binding.builder_id
        or metadata.get("plan") != binding.plan
        or metadata.get("cycle") != binding.billing_cycle
        or metadata_coupon != binding.coupon_code
    ):
        logger.warning("Webhook metadata does not match canonical roster")
        return {"ok": True, "ignored": True}

    result = await db.execute(select(Builder).where(Builder.id == binding.builder_id))
    builder = result.scalar_one_or_none()
    if not builder:
        logger.warning("Canonical webhook builder not found")
        return {"ok": True, "ignored": True}

    builder.subscription_plan = binding.plan
    builder.subscription_status = "active"
    builder.billing_cycle = binding.billing_cycle

    coupon_code = binding.coupon_code
    if coupon_code:
        coupon_result = await db.execute(
            select(Coupon).where(Coupon.code == coupon_code.upper())
        )
        coupon = coupon_result.scalar_one_or_none()
        if coupon:
            coupon.uses_count += 1
            if coupon.max_uses is not None and coupon.uses_count >= coupon.max_uses:
                coupon.is_active = False

    # Log transaction record
    tx = Transaction(
        builder_id=binding.builder_id,
        builder_name=builder.name,
        amount_cents=data.get("amount", 0),
        method=data.get("method", "unknown").lower(),
        cycle=binding.billing_cycle,
        plan=binding.plan,
        coupon_code=coupon_code if coupon_code else None,
        external_id=data.get("id")
        or data.get("charge_id")
        or data.get("subscription_id"),
        event_type=event_type,
    )
    db.add(tx)

    await db.commit()
    logger.info(
        "Webhook processed",
        builder_id=str(binding.builder_id),
        event_type=event_type,
    )
    return {"ok": True}
