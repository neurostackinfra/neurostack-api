"""
NeuroStack API - Billing Schemas
Pydantic models for billing endpoints.
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.services.billing_catalog import BILLING_CATALOG


class UsagePeriodResponse(BaseModel):
    start: datetime
    end: datetime
    timezone: str = "UTC"


class TraceUsageResponse(BaseModel):
    used: int = 0
    included: int = 0
    extra_credits: int = 0
    available: int = 0
    overage: int = 0
    percent_used: float = 0.0


class StorageUsageResponse(BaseModel):
    used_bytes: int = 0
    used_mb: float = 0.0
    limit_bytes: int = 0
    limit_mb: int = 0
    available_bytes: int = 0
    available_mb: float = 0.0
    overage_bytes: int = 0
    overage_mb: float = 0.0
    percent_used: float = 0.0


class StorageBreakdownResponse(BaseModel):
    rag: StorageUsageResponse
    media: StorageUsageResponse


class CreditAvailabilityResponse(BaseModel):
    purchase_enabled: bool = False
    coming_soon: bool = True


class BillingUsageResponse(BaseModel):
    plan: Optional[str] = None
    as_of: datetime
    trace_period: UsagePeriodResponse
    traces: TraceUsageResponse
    storage: StorageBreakdownResponse
    credits: CreditAvailabilityResponse


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
    usage: BillingUsageResponse


class CheckoutRequest(BaseModel):
    """Browser-selectable checkout inputs.

    Price, discount, quantity, tenant identity, cycle, due date, coupon state
    and every payment credential are backend-owned.  Subscriptions accept only
    ``credit_card``; detached top-ups may also use ``pix``.  A subscription
    may name an optional ``coupon_code``, which is only a selector resolved by
    the server; top-ups never accept one.
    """

    offer_id: str = Field(..., min_length=1, max_length=100)
    method: Literal["credit_card", "pix"]
    coupon_code: Optional[str] = Field(default=None, min_length=1, max_length=50)
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_offer_method(self) -> "CheckoutRequest":
        """Reject known invalid combinations at the public contract boundary."""

        offer = BILLING_CATALOG.get(self.offer_id)
        if (
            offer is not None
            and offer.cycle == "MONTHLY"
            and self.method != "credit_card"
        ):
            raise ValueError("subscription checkout requires credit_card")
        if self.coupon_code is not None and (offer is None or offer.cycle != "MONTHLY"):
            raise ValueError("coupon_code is accepted only for a subscription")
        return self


class CheckoutResponse(BaseModel):
    ok: bool = True
    checkout_session_id: str
    kind: Literal["subscription", "topup"]
    state: str
    checkout_url: Optional[str] = None


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
