"""
NeuroStack API - Billing Endpoints
Plan summary, checkout (AbacatePay), webhook handler, and coupon validation.
"""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import require_builder
from src.api.schemas.billing import (
    BillingSummaryResponse,
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
from src.services.abacatepay import AbacatePayClient

logger = get_logger(__name__)

router = APIRouter()

BASE_PRICE = 2000
EXTRA_PER_CLINIC = 350
FREE_TIER = 2
ANNUAL_DISCOUNT_PCT = 15


def _get_client() -> AbacatePayClient:
    return AbacatePayClient(
        api_key=settings.abacatepay_api_key,
        env=settings.environment,
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


def _calc_pricing(active_clinics: int) -> dict:
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
):
    result = await db.execute(select(Builder).where(Builder.id == builder_id))
    builder = result.scalar_one_or_none()
    if not builder:
        raise HTTPException(status_code=404, detail="Builder not found")

    active_clinics = await _count_active_clinics(builder_id, db)
    pricing = _calc_pricing(active_clinics)

    return BillingSummaryResponse(
        plan=builder.subscription_plan,
        subscription_status=builder.subscription_status,
        billing_cycle=builder.billing_cycle,
        **pricing,
    )


async def _validate_coupon(code: str, db: AsyncSession) -> Coupon:
    result = await db.execute(
        select(Coupon).where(Coupon.code == code.upper())
    )
    coupon = result.scalar_one_or_none()
    if not coupon:
        raise HTTPException(status_code=404, detail="Cupom não encontrado")
    if not coupon.is_active:
        raise HTTPException(status_code=400, detail="Cupom inativo")
    if coupon.expires_at and coupon.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Cupom expirado")
    if coupon.max_uses is not None and coupon.uses_count >= coupon.max_uses:
        raise HTTPException(status_code=400, detail="Cupom esgotado")
    return coupon


@router.get(
    "/coupon/{code}",
    summary="Validate Coupon",
    response_model=CouponValidationResponse,
)
async def validate_coupon(
    code: str,
    builder_id: UUID = Depends(require_builder),
    db: AsyncSession = Depends(get_db_session_dependency),
):
    result = await db.execute(
        select(Coupon).where(Coupon.code == code.upper())
    )
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
):
    result = await db.execute(select(Builder).where(Builder.id == builder_id))
    builder = result.scalar_one_or_none()
    if not builder:
        raise HTTPException(status_code=404, detail="Builder not found")

    active_clinics = await _count_active_clinics(builder_id, db)
    pricing = _calc_pricing(active_clinics)

    if body.cycle == "monthly":
        amount = pricing["total_monthly"]
    else:
        amount = pricing["total_annual"]

    coupon_code_used: str | None = None
    if body.coupon_code:
        coupon = await _validate_coupon(body.coupon_code, db)
        discount_factor = 1 - coupon.discount_percent / 100
        amount = int(amount * discount_factor)
        coupon_code_used = coupon.code

    amount_cents = amount * 100
    client = _get_client()

    metadata = {
        "builder_id": str(builder_id),
        "plan": body.plan,
        "cycle": body.cycle,
    }
    if coupon_code_used:
        metadata["coupon_code"] = coupon_code_used

    frequency = "MONTHLY" if body.cycle == "monthly" else "YEARLY"

    if body.method == "pix" and body.cycle == "monthly":
        result_pay = client.create_subscription(
            amount_cents=amount_cents,
            frequency="MONTHLY",
            method="PIX",
            description=f"NeuroStack Start - Mensal (PIX)",
            customer={"name": builder.name},
            metadata=metadata,
        )
        return CheckoutResponse(
            method="pix",
            payment_url=result_pay["subscription_url"],
            subscription_id=result_pay["subscription_id"],
        )

    if body.method == "pix" and body.cycle == "annual":
        result_pay = client.create_pix_charge(
            amount_cents=amount_cents,
            description=f"NeuroStack Start - Anual (PIX)",
            metadata=metadata,
        )
        return CheckoutResponse(
            method="pix",
            qr_code=result_pay["qr_code"],
            qr_code_url=result_pay["qr_code_url"],
            subscription_id=result_pay.get("charge_id"),
        )

    max_installments = None
    if body.cycle == "annual":
        max_installments = body.installments or 12

    result_pay = client.create_subscription(
        amount_cents=amount_cents,
        frequency=frequency,
        method="CREDIT_CARD",
        description=f"NeuroStack Start - {'Mensal' if body.cycle == 'monthly' else 'Anual'} (Cartão)",
        customer={"name": builder.name},
        metadata=metadata,
        max_installments=max_installments,
    )
    return CheckoutResponse(
        method="card",
        payment_url=result_pay["subscription_url"],
        subscription_id=result_pay["subscription_id"],
    )


@router.post(
    "/webhook",
    summary="AbacatePay Webhook",
    status_code=200,
)
async def abacatepay_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db_session_dependency),
):
    body = await request.body()

    signature = request.headers.get("X-Webhook-Signature", "")
    if settings.abacatepay_webhook_secret:
        if not AbacatePayClient.verify_webhook_signature(
            body, signature, settings.abacatepay_webhook_secret
        ):
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

    builder_id_str = metadata.get("builder_id")
    if not builder_id_str:
        logger.warning("Webhook missing builder_id in metadata")
        return {"ok": True, "ignored": True}

    from uuid import UUID as _UUID
    try:
        bid = _UUID(builder_id_str)
    except ValueError:
        logger.warning("Webhook invalid builder_id", builder_id=builder_id_str)
        return {"ok": True, "ignored": True}

    result = await db.execute(select(Builder).where(Builder.id == bid))
    builder = result.scalar_one_or_none()
    if not builder:
        logger.warning("Webhook builder not found", builder_id=builder_id_str)
        return {"ok": True, "ignored": True}

    plan = metadata.get("plan", "start")
    cycle = metadata.get("cycle", "monthly")
    builder.subscription_plan = plan
    builder.subscription_status = "active"
    builder.billing_cycle = cycle

    coupon_code = metadata.get("coupon_code")
    if coupon_code:
        coupon_result = await db.execute(
            select(Coupon).where(Coupon.code == coupon_code.upper())
        )
        coupon = coupon_result.scalar_one_or_none()
        if coupon:
            coupon.uses_count += 1
            if coupon.max_uses is not None and coupon.uses_count >= coupon.max_uses:
                coupon.is_active = False

    await db.commit()
    logger.info("Webhook processed", builder_id=builder_id_str, event=event_type)
    return {"ok": True}
