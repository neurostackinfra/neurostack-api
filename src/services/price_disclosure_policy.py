"""The clinic's price-disclosure policy, shared by the prompt and the media.

envio-midia-agente Decisions 7 and 11 (and correcao-link-convenio).  Three
pieces, each with ONE source:

* :func:`clinic_price_variables` -- the adapter from a clinic config to the
  prompt's price variables (``pricing_mode``, ``accepts_insurance``,
  ``provide_prices``).  The pipeline renders the prompt with it
  (``CognitivePipeline._run_prompt_engine``), and the media selection reads
  the same config through it in phase 2;
* :func:`can_show_price` (``neurostack_core.prompt``) -- the pure rule the
  normal template renders its pricing block from.

* :func:`clinic_accepts_insurance` -- whether the clinic's
  ``accepts_insurance`` value means insurance is on.  Its source is the core
  rule (``neurostack_core.prompt.price_policy.resolve_price_disclosure``),
  which it replicates and is parity-tested against.

:func:`turn_can_show_price` joins them with the turn's care modality
(``PipelineResult.atendimento_tipo``, the slot the pipeline turns into
``can_quote_particular``): a media file bound to ``pedido_preco`` is offered
only when it is true, so a price file never reaches a patient the prompt
would not quote a price to.  ``tests/services/test_price_disclosure_policy.py``
pins the parity over the whole matrix.

Pure, and free of metrics and database imports: the pipeline imports it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from neurostack_core.prompt import (
    CAN_QUOTE_PRIVATE_PAY_CUSTOM_KEY,
    PRIVATE_PAY_CARE_MODALITY,
    can_show_price,
)

# The default disclosure mode when the clinic config names none: the
# pipeline's historical default, which never quotes a price.
DEFAULT_PRICING_MODE = "CONSULT_ONLY"

# The spellings the core reads as "the clinic does not accept insurance"
# (``neurostack_core.prompt.price_policy.resolve_price_disclosure``).
_INSURANCE_OFF_SPELLINGS = ("no", "false", "0", "none")


def clinic_accepts_insurance(value: object) -> bool:
    """Whether the clinic's ``accepts_insurance`` value means insurance is on.

    Replicates ``resolve_price_disclosure(custom).insurance_active`` from
    ``neurostack_core.prompt.price_policy`` exactly: truthy and not one of
    the off spellings (case-insensitive, no whitespace stripping).  The UI
    stores the strings ``"yes"``/``"no"``, so ``bool(value)`` is wrong.
    ``tests/services/test_price_disclosure_policy.py`` pins the parity.
    """
    return bool(value and str(value).lower() not in _INSURANCE_OFF_SPELLINGS)


def _json_object(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def clinic_price_variables(clinic_config: Mapping[str, Any] | None) -> dict[str, Any]:
    """The prompt's price variables read from ``clinic_config``.

    The values are passed through exactly as stored (the template normalizes
    them): ``pricing.disclosure_mode`` (default ``CONSULT_ONLY``),
    ``business_rules.accepts_insurance`` and ``business_rules.provide_prices``
    (both default ``False``).  A section that is not a JSON object reads as
    empty, and so does an absent or empty config.
    """

    config = clinic_config if isinstance(clinic_config, Mapping) else {}
    business = _json_object(config.get("business_rules"))
    pricing = _json_object(config.get("pricing"))
    return {
        "pricing_mode": pricing.get("disclosure_mode", DEFAULT_PRICING_MODE),
        "accepts_insurance": business.get("accepts_insurance", False),
        "provide_prices": business.get("provide_prices", False),
    }


def can_quote_private_pay(atendimento_tipo: object) -> bool:
    """Whether the care modality is confirmed as private pay (``particular``).

    The value of the prompt's ``can_quote_particular``: the convenio triage
    holds every price until it is true.
    """

    return atendimento_tipo == PRIVATE_PAY_CARE_MODALITY


def turn_price_variables(
    clinic_config: Mapping[str, Any] | None, atendimento_tipo: object
) -> dict[str, Any]:
    """Every variable the price rule reads, for one turn."""

    return {
        **clinic_price_variables(clinic_config),
        CAN_QUOTE_PRIVATE_PAY_CUSTOM_KEY: can_quote_private_pay(atendimento_tipo),
    }


def turn_can_show_price(
    clinic_config: Mapping[str, Any] | None, atendimento_tipo: object
) -> bool:
    """Whether this turn's prompt may show a price (``can_show_price``)."""

    return can_show_price(turn_price_variables(clinic_config, atendimento_tipo))


__all__ = [
    "DEFAULT_PRICING_MODE",
    "can_quote_private_pay",
    "clinic_accepts_insurance",
    "clinic_price_variables",
    "turn_can_show_price",
    "turn_price_variables",
]
