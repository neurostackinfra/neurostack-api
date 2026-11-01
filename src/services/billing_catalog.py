"""Immutable, backend-owned commercial catalog for ASAAS billing.

The values in this module are a release contract.  Existing catalog versions
must never be edited in place; publish a new ``catalog_version_id`` instead.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Final, Literal, Mapping

CATALOG_VERSION_ID: Final = "payment-2026-10-03"
CATALOG_SOURCE_DOCUMENT: Final = "NeuroStack_Estrutura_de_Pagamento.pdf"
CATALOG_SOURCE_UPDATED_AT: Final = "2026-10-03"
CATALOG_SOURCE_PAGES: Final = 6
CATALOG_SOURCE_SHA256: Final = (
    "58e71858ae25d12b76db4c72aa7f6838cb760187ecdb841fdf56d729aca86aaf"
)

BillingCycle = Literal["MONTHLY", "DETACHED"]


@dataclass(frozen=True, slots=True)
class BillingOffer:
    """One immutable offer snapshot, represented only in integer units."""

    offer_id: str
    amount_cents: int
    credits: int
    included_clinics: int
    additional_clinic_amount_cents: int
    cycle: BillingCycle
    additional_clinic_credit_grant: int = 0

    def snapshot(self) -> dict[str, int | str]:
        """Return a detached snapshot suitable for persistence."""

        return asdict(self)


_OFFERS = (
    BillingOffer("plan_plus_monthly_v1", 99_900, 5_000, 1, 20_000, "MONTHLY"),
    BillingOffer("plan_pro_monthly_v1", 250_000, 15_000, 3, 15_000, "MONTHLY"),
    BillingOffer("plan_max_monthly_v1", 500_000, 35_000, 10, 10_000, "MONTHLY"),
    BillingOffer("package_credits_500_v1", 10_000, 500, 0, 0, "DETACHED"),
    BillingOffer("package_credits_1000_v1", 20_000, 1_000, 0, 0, "DETACHED"),
    BillingOffer("package_credits_2000_v1", 40_000, 2_000, 0, 0, "DETACHED"),
    BillingOffer("package_credits_4000_v1", 80_000, 4_000, 0, 0, "DETACHED"),
)

# Customer-facing plan names. Deliberately NOT a field of ``BillingOffer``:
# ``snapshot()`` is persisted with each checkout and ``catalog_snapshot_bytes``
# is compared byte for byte by the release contract, so a label edit must
# never change either.  Only subscription offers carry a name; a detached
# package is described by its credits.
OFFER_DISPLAY_NAMES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "plan_plus_monthly_v1": "Plus",
        "plan_pro_monthly_v1": "Pro",
        "plan_max_monthly_v1": "Max",
    }
)

BILLING_CATALOG: Final[Mapping[str, BillingOffer]] = MappingProxyType(
    {offer.offer_id: offer for offer in _OFFERS}
)


def catalog_snapshot() -> dict[str, object]:
    """Return the complete backend-owned catalog snapshot."""

    return {
        "catalog_version_id": CATALOG_VERSION_ID,
        "offers": [offer.snapshot() for offer in _OFFERS],
    }


def catalog_snapshot_bytes() -> bytes:
    """Return deterministic UTF-8 bytes for release-contract comparisons."""

    return (
        json.dumps(
            catalog_snapshot(),
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def validate_billing_catalog() -> None:
    """Fail startup when the frozen catalog contract is internally inconsistent."""

    expected_ids = (
        "plan_plus_monthly_v1",
        "plan_pro_monthly_v1",
        "plan_max_monthly_v1",
        "package_credits_500_v1",
        "package_credits_1000_v1",
        "package_credits_2000_v1",
        "package_credits_4000_v1",
    )
    if len(_OFFERS) != 7 or tuple(BILLING_CATALOG) != expected_ids:
        raise RuntimeError("invalid_billing_catalog")
    if any(offer.additional_clinic_credit_grant != 0 for offer in _OFFERS):
        raise RuntimeError("invalid_billing_catalog")
    if {o.offer_id for o in _OFFERS if o.cycle != "DETACHED"} != set(
        OFFER_DISPLAY_NAMES
    ) or any(not name.strip() for name in OFFER_DISPLAY_NAMES.values()):
        raise RuntimeError("invalid_billing_catalog")
    if any(
        offer.amount_cents <= 0
        or offer.credits <= 0
        or offer.included_clinics < 0
        or offer.additional_clinic_amount_cents < 0
        for offer in _OFFERS
    ):
        raise RuntimeError("invalid_billing_catalog")
    if any(
        offer.cycle == "DETACHED"
        and (offer.included_clinics or offer.additional_clinic_amount_cents)
        for offer in _OFFERS
    ):
        raise RuntimeError("invalid_billing_catalog")


validate_billing_catalog()
