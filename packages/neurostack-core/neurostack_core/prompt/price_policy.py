"""The price-disclosure rule of the normal prompt, as one pure function.

Extracted from ``NormalTemplate._build_clinic_context`` so that every
consumer that must agree with the prompt about showing a price this turn
reads the same rule: the template renders its pricing block from
:func:`resolve_price_disclosure`, and the application's media selection
(envio-midia-agente) offers a price file only when :func:`can_show_price`
is true.  The inputs are the template's custom variables, by the names the
pipeline gives them: ``accepts_insurance``, ``provide_prices``,
``pricing_mode`` and :data:`CAN_QUOTE_PRIVATE_PAY_CUSTOM_KEY`.

The rule, unchanged from the template:

* with insurance active (``accepts_insurance`` truthy and not one of the
  "no" spellings) and the care modality not confirmed as private pay, the
  price is held: no monetary value at all;
* otherwise the price may be shared when ``pricing_mode`` is
  ``range_allowed`` or ``exact_allowed`` (case and outer blanks ignored), or
  when ``provide_prices`` says yes -- which counts as ``exact_allowed`` even
  with ``pricing_mode = consult_only``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from neurostack_core.protocol import CAN_QUOTE_PRIVATE_PAY_CUSTOM_KEY

# ``accepts_insurance`` spellings that keep the convenio triage off.
_INSURANCE_OFF_VALUES = ("no", "false", "0", "none")
# ``provide_prices`` values that mean "yes, quote the exact price".
_PROVIDE_PRICES_YES = (True, "yes", "true", "True")


@dataclass(frozen=True)
class PriceDisclosure:
    """What the normal prompt may say about prices this turn."""

    insurance_active: bool
    hold_price: bool
    range_allowed: bool
    exact_allowed: bool

    @property
    def can_share_price(self) -> bool:
        """The clinic's disclosure mode lets the agent quote a price."""

        return self.range_allowed or self.exact_allowed

    @property
    def can_show_price(self) -> bool:
        """A price may be shown this turn: not held, and the mode allows it."""

        return not self.hold_price and self.can_share_price


def resolve_price_disclosure(custom: Mapping[str, object]) -> PriceDisclosure:
    """Resolve the price rule from the prompt's custom variables. Pure."""

    accepts_insurance = custom.get("accepts_insurance")
    insurance_active = bool(
        accepts_insurance
        and str(accepts_insurance).lower() not in _INSURANCE_OFF_VALUES
    )
    hold_price = insurance_active and not custom.get(CAN_QUOTE_PRIVATE_PAY_CUSTOM_KEY)
    provide_prices = custom.get("provide_prices")
    pricing_mode = str(custom.get("pricing_mode", "")).lower().strip()
    return PriceDisclosure(
        insurance_active=insurance_active,
        hold_price=hold_price,
        range_allowed=pricing_mode == "range_allowed",
        exact_allowed=(
            provide_prices in _PROVIDE_PRICES_YES or pricing_mode == "exact_allowed"
        ),
    )


def can_show_price(custom: Mapping[str, object]) -> bool:
    """Whether the normal prompt lets the agent show a price this turn.

    ``not hold_price and can_share_price`` -- exactly the condition under
    which the template renders its "may quote" pricing policy.
    """

    return resolve_price_disclosure(custom).can_show_price


__all__ = ["PriceDisclosure", "can_show_price", "resolve_price_disclosure"]
