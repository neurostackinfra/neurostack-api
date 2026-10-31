"""Response bodies of the credit read API (neurostack-credit-system Group 6).

Every amount is an integer of millicredits (``*_mc``; 1 credit = 1,000 mc):
no float anywhere.  Every figure is derived by the server from the credit
ledger as of ``as_of`` (the database clock): nothing here is an input, and
no route of this API accepts a body.  The builder these figures belong to is
never named by the client: it is re-read from ``tenants.builder_id`` of the
authenticated (or, for a builder or a super admin, the selected) tenant.

Purchasing is not part of this API: top-ups and the subscription are sold by
the ASAAS checkout (``POST /v1/billing/checkout``, which accepts only
``offer_id`` and ``method``), and a purchase appears here only once its
settled fact was applied to the ledger -- a checkout return, a QR code or a
pending payment never changes these figures.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

CreditMode = Literal["observational", "enforced"]
CreditState = Literal["active", "pending", "grace", "suspended", "deletion_due"]
PendingReason = Literal["quarantine", "standalone", "reconciliation"]
LotKind = Literal["monthly", "topup"]
LotStatus = Literal["active", "not_started", "exhausted", "expired"]
ReservationCategory = Literal["rag", "media", "egress"]
WindowKind = Literal["cycle", "continuation", "calendar"]
PaymentMethod = Literal["credit_card", "pix"]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CreditUnits(_Frozen):
    """What one credit buys (the PDF's conversions, fixed by the server)."""

    trace_mc: int = Field(..., description="Cost of one governed decision.")
    rag_characters_per_credit: int
    media_bytes_per_credit: int
    egress_included_bytes_per_credit: int
    egress_overage_bytes_per_credit: int


class CreditPackage(_Frozen):
    """One detached top-up of the ASAAS catalog (card or PIX)."""

    offer_id: str
    credits: int = Field(..., gt=0)
    amount_cents: int = Field(..., gt=0)
    currency: Literal["BRL"] = "BRL"
    payment_methods: list[PaymentMethod]
    validity_months: int = Field(..., gt=0)


class CreditCatalogResponse(_Frozen):
    """Units and packages; prices come from the immutable ASAAS catalog."""

    catalog_version_id: str
    millicredits_per_credit: int
    units: CreditUnits
    packages: list[CreditPackage]
    subscription_payment_methods: list[Literal["credit_card"]] = Field(
        ...,
        description=(
            "The subscription is a monthly card subscription: no PIX, no "
            "Pix Automático, no annual cycle, no installments."
        ),
    )
    subscription_cycle: Literal["monthly"] = "monthly"
    checkout_endpoint: Literal["/v1/billing/checkout"] = Field(
        default="/v1/billing/checkout",
        description=(
            "The ASAAS-owned endpoint that sells a package: it accepts only "
            "offer_id and method, never an amount, a quantity, a builder or "
            "a validity, and refuses top-ups while topup_commercial_enabled "
            "is false on the server."
        ),
    )


class CreditStateResponse(_Frozen):
    """The builder's credit state.

    ``state``: ``deletion_due`` > ``suspended`` > ``grace`` (an exhaustion
    episode, recorded only after ``credit_enforcement_at``) > ``pending``
    (``credit_pending``: a quarantined ASAAS order or cycle, a standalone
    tenant without an account, or -- once enforced -- a binding coverage
    that is not usable, ``reconciliation``: an identity conflict, a current
    cycle without a binding outside grace, an unreconciled top-up) >
    ``active``.  ``export_deadline_at`` is the
    retention deadline of the open episode (export before it); ``mode`` is
    ``observational`` until ``credit_enforcement_at``: nothing is charged.
    """

    as_of: datetime
    mode: CreditMode
    enforcement_at: Optional[datetime]
    state: CreditState
    pending_reasons: list[PendingReason]
    exhausted_at: Optional[datetime] = None
    suspension_due_at: Optional[datetime] = None
    deletion_due_at: Optional[datetime] = None
    export_deadline_at: Optional[datetime] = None


class CreditNextExpiry(_Frozen):
    lot_id: UUID
    kind: LotKind
    expires_at: datetime
    available_mc: int


class CreditBalanceResponse(_Frozen):
    """Usable capacity by origin, from the ledger swept at ``as_of``.

    ``available_mc = monthly_available_mc + topup_available_mc``; a monthly
    allowance granted before its cycle starts counts only in
    ``monthly_not_started_mc``; ``net_available_mc`` is the available
    capacity net of ``open_debt_mc``.  ``reserved_mc`` is what the current
    window's RAG/media/egress reservations already debited (a reservation is
    a debit: it is not subtracted again).
    """

    as_of: datetime
    monthly_available_mc: int
    monthly_not_started_mc: int
    topup_available_mc: int
    available_mc: int
    open_debt_mc: int
    net_available_mc: int
    reserved_mc: int
    next_expiry: Optional[CreditNextExpiry] = None


class CreditLot(_Frozen):
    """One grant: a monthly allowance or a top-up, with its own expiry."""

    lot_id: UUID
    kind: LotKind
    status: LotStatus
    granted_mc: int
    available_mc: int
    consumed_mc: int
    reversed_mc: int
    expired_mc: int
    paid_at: date
    expires_at: datetime
    cycle_start: Optional[date] = None
    cycle_end: Optional[date] = None


class CreditLotsResponse(_Frozen):
    as_of: datetime
    lots: list[CreditLot]


class CreditExpiry(_Frozen):
    lot_id: UUID
    kind: LotKind
    expires_at: datetime
    available_mc: int


class CreditExpiriesResponse(_Frozen):
    """Lots still holding usable capacity, the earliest expiry first."""

    as_of: datetime
    expiries: list[CreditExpiry]


class CreditDebtItem(_Frozen):
    debt_id: UUID
    source_kind: str
    amount_mc: int
    settled_mc: int
    open_mc: int
    created_at: datetime


class CreditDebtResponse(_Frozen):
    """Open debt (consumption beyond capacity, or a refunded payment's
    consumption), settled oldest first by the next capacity."""

    as_of: datetime
    open_debt_mc: int
    debts: list[CreditDebtItem]


class CreditAccountingWindow(_Frozen):
    """``[start, end)`` in civil dates of America/Sao_Paulo."""

    start: date
    end: date
    kind: WindowKind


class CreditReservation(_Frozen):
    """One category of the current window: ``coverage_mc`` is the highest
    target recorded (deleting content refunds nothing), ``charged_mc`` what
    the window's reservations debited."""

    category: ReservationCategory
    units: int
    coverage_mc: int
    charged_mc: int


class CreditReservationsResponse(_Frozen):
    as_of: datetime
    window: CreditAccountingWindow
    reservations: list[CreditReservation]
    egress_allowance_bytes: int
    egress_delivered_bytes: int


class CreditThresholdAlertItem(_Frozen):
    threshold: int
    occurred_at: datetime


class CreditThresholdsResponse(_Frozen):
    """50/90/100 % of the capacity in force consumed, counting open debt."""

    as_of: datetime
    capacity_mc: int
    available_mc: int
    used_mc: int
    used_percent: int = Field(..., ge=0, le=100)
    reached: list[int]
    alerts: list[CreditThresholdAlertItem]


class CreditConsumptionItem(_Frozen):
    kind: str
    amount_mc: int


class CreditConsumptionResponse(_Frozen):
    """Consumption in the current window by effect kind.

    ``charged`` comes from the ledger (debits and the consumption debt they
    created, after ``credit_enforcement_at``); ``observed`` is what the
    observational metering WOULD have debited (never charged).
    """

    as_of: datetime
    mode: CreditMode
    window: CreditAccountingWindow
    charged_mc: int
    charged: list[CreditConsumptionItem]
    observed_mc: int
    observed: list[CreditConsumptionItem]


class CreditSummaryResponse(_Frozen):
    """Every panel of the billing page from ONE snapshot of the ledger."""

    as_of: datetime
    state: CreditStateResponse
    balance: CreditBalanceResponse
    expiries: list[CreditExpiry]
    debt: CreditDebtResponse
    reservations: CreditReservationsResponse
    thresholds: CreditThresholdsResponse
    consumption: CreditConsumptionResponse


class CreditRefusal(BaseModel):
    """``{"detail": {"code": ...}}``: the same body for a foreign and an
    absent tenant (nothing reveals existence)."""

    detail: dict[str, str]


class CreditUnavailable(BaseModel):
    """``{"code": "credit_unavailable"}`` while the credit switch is off."""

    code: Literal["credit_unavailable"]


__all__ = [
    "CreditAccountingWindow",
    "CreditBalanceResponse",
    "CreditCatalogResponse",
    "CreditConsumptionItem",
    "CreditConsumptionResponse",
    "CreditDebtItem",
    "CreditDebtResponse",
    "CreditExpiriesResponse",
    "CreditExpiry",
    "CreditLot",
    "CreditLotsResponse",
    "CreditMode",
    "CreditNextExpiry",
    "CreditPackage",
    "CreditRefusal",
    "CreditReservation",
    "CreditReservationsResponse",
    "CreditState",
    "CreditStateResponse",
    "CreditSummaryResponse",
    "CreditThresholdAlertItem",
    "CreditThresholdsResponse",
    "CreditUnavailable",
    "CreditUnits",
]
