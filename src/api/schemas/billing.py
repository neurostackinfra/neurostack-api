"""
NeuroStack API - Billing Schemas
Pydantic models for billing endpoints.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class BillingSummaryResponse(BaseModel):
    plan: Optional[str] = None
    active_clinics: int = 0
    free_tier: int = 2
    extra_clinics: int = 0
    base_price: int = 2000
    extra_price: int = 0
    total_monthly: int = 2000
    total_annual: int = 20400
    discount_pct: int = 15
    subscription_status: str = "trial"
    billing_cycle: str = "monthly"


class CheckoutRequest(BaseModel):
    plan: str = Field(..., pattern=r"^(start)$")
    cycle: str = Field(..., pattern=r"^(monthly|annual)$")
    method: str = Field(..., pattern=r"^(pix|card)$")
    coupon_code: Optional[str] = None
    installments: Optional[int] = Field(default=None, ge=1, le=12)


class CheckoutResponse(BaseModel):
    ok: bool = True
    method: str
    qr_code: Optional[str] = None
    qr_code_url: Optional[str] = None
    payment_url: Optional[str] = None
    subscription_id: Optional[str] = None


class CouponValidationResponse(BaseModel):
    valid: bool
    code: Optional[str] = None
    discount_percent: Optional[int] = None
    message: Optional[str] = None


class CreateCouponRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=50)
    discount_percent: int = Field(..., ge=1, le=100)
    max_uses: Optional[int] = Field(default=None, ge=1)
    expires_at: Optional[datetime] = None


class CouponResponse(BaseModel):
    id: str
    code: str
    discount_percent: int
    max_uses: Optional[int] = None
    uses_count: int = 0
    expires_at: Optional[datetime] = None
    is_active: bool = True
    created_at: Optional[datetime] = None
