"""Price-disclosure parity between the prompt and the media selection.

envio-midia-agente Group 4, Decisions 7 and 11.  The prompt and the media
selection decide "may a price be shown this turn?" from ONE adapter
(:func:`clinic_price_variables`) and ONE pure rule (``can_show_price``):

* the full matrix ``accepts_insurance x atendimento_tipo x provide_prices x
  pricing_mode`` runs through the REAL pipeline, and the "may quote"
  pricing block is in the rendered prompt exactly when
  :func:`turn_can_show_price` -- the selection's question -- is true;
* the extracted rule equals, value for value, the template code it was
  extracted from (kept below as the reference);
* the pipeline renders through the adapter and the template through the
  rule (spies), and a ``pedido_preco`` turn is excluded from media exactly
  when the prompt holds the price.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any, Optional
from uuid import uuid4

import pytest

import neurostack_core.prompt.templates.normal as normal_template
from neurostack_core.prompt import CAN_QUOTE_PRIVATE_PAY_CUSTOM_KEY, can_show_price
from neurostack_core.prompt.price_policy import resolve_price_disclosure
from src.core import pipeline as pipeline_module
from src.core.pipeline import CognitivePipeline, PipelineInput, PipelineResult
from src.interaction_context import InteractionOrigin, build_interaction_context
from src.services import price_disclosure_policy
from src.services.media_selection import media_turn_exclusion
from src.services.price_disclosure_policy import (
    can_quote_private_pay,
    clinic_price_variables,
    turn_can_show_price,
    turn_price_variables,
)

_CLINIC_LITERALS = normal_template._CLINIC_LITERALS
# The three pricing blocks of ``_build_clinic_context``; exactly one renders.
_MAY_QUOTE = _CLINIC_LITERALS[8]
_HELD = _CLINIC_LITERALS[5]
_NO_QUOTE = _CLINIC_LITERALS[10]

_ACCEPTS_INSURANCE: tuple[object, ...] = (
    True,
    False,
    "yes",
    "no",
    "false",
    "0",
    "none",
    "",
    None,
    "sim",
)
_PROVIDE_PRICES: tuple[object, ...] = (True, False, "yes", "true", "True", "no", None)
_MISSING = object()
_PRICING_MODES: tuple[object, ...] = (
    "consult_only",
    "CONSULT_ONLY",
    "range_allowed",
    " Range_Allowed ",
    "exact_allowed",
    "EXACT_ALLOWED",
    "",
    _MISSING,
)
_CARE_MODALITIES: tuple[Optional[str], ...] = (None, "particular", "convenio")


def _clinic_config(
    accepts_insurance: object, provide_prices: object, pricing_mode: object
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "identity": {"clinic_name": "Clínica Paridade", "agent_name": "Ana"},
        "business_rules": {
            "accepts_insurance": accepts_insurance,
            "provide_prices": provide_prices,
        },
        "pricing": {},
    }
    if pricing_mode is not _MISSING:
        config["pricing"]["disclosure_mode"] = pricing_mode
    return config


def _reference_template_rule(custom: dict[str, Any]) -> bool:
    """``normal.py:368-391`` before the extraction, verbatim (the oracle)."""

    accepts_ins = custom.get("accepts_insurance")
    insurance_active = bool(
        accepts_ins and str(accepts_ins).lower() not in ("no", "false", "0", "none")
    )
    hold_price = insurance_active and not custom.get(CAN_QUOTE_PRIVATE_PAY_CUSTOM_KEY)
    if hold_price:
        return False
    provide_prices = custom.get("provide_prices")
    pricing_mode = str(custom.get("pricing_mode", "")).lower().strip()
    is_range_allowed = pricing_mode == "range_allowed"
    is_exact_allowed = (
        provide_prices in (True, "yes", "true", "True")
        or pricing_mode == "exact_allowed"
    )
    return is_range_allowed or is_exact_allowed


def _rendered_pricing(system_prompt: str) -> str:
    blocks = [
        name
        for name, literal in (
            ("may_quote", _MAY_QUOTE),
            ("held", _HELD),
            ("no_quote", _NO_QUOTE),
        )
        if literal in system_prompt
    ]
    assert len(blocks) == 1, blocks
    return blocks[0]


def _process(
    pipeline: CognitivePipeline,
    clinic_config: Optional[dict[str, Any]],
    atendimento_tipo: Optional[str],
    message: str,
) -> PipelineResult:
    slots = {"atendimento_tipo": atendimento_tipo} if atendimento_tipo else {}
    return asyncio.run(
        pipeline.process(
            PipelineInput(
                tenant_id=uuid4(),
                session_id=uuid4(),
                message=message,
                channel="whatsapp",
                clinic_config=clinic_config,
                slots=slots,
                turn_count=3,
            )
        )
    )


_MATRIX = list(
    itertools.product(
        _ACCEPTS_INSURANCE, _CARE_MODALITIES, _PROVIDE_PRICES, _PRICING_MODES
    )
)


@pytest.fixture(scope="module")
def pipeline() -> CognitivePipeline:
    return CognitivePipeline()


@pytest.mark.parametrize(
    "message", ["quanto custa a consulta?", "me fala dos serviços"]
)
def test_the_prompt_quotes_a_price_exactly_when_the_selection_may_show_one(
    pipeline: CognitivePipeline, message: str
) -> None:
    """The whole matrix through the real pipeline, on a price turn and on a
    turn of another bound intent: the rendered pricing block and the
    selection's ``turn_can_show_price`` agree at every point."""

    mismatches = []
    rendered_states: set[str] = set()
    for accepts, tipo, provide, mode in _MATRIX:
        config = _clinic_config(accepts, provide, mode)
        result = _process(pipeline, config, tipo, message)
        rendered = _rendered_pricing(result.system_prompt)
        rendered_states.add(rendered)
        selection = turn_can_show_price(config, result.atendimento_tipo)
        if (rendered == "may_quote") is not selection:
            mismatches.append((accepts, tipo, provide, mode, rendered, selection))

    assert mismatches == []
    # Non-vacuous: the matrix reaches all three pricing blocks.
    assert rendered_states == {"may_quote", "held", "no_quote"}


@pytest.mark.parametrize("clinic_config", [None, {}])
def test_a_clinic_without_config_never_shows_a_price_on_either_side(
    pipeline: CognitivePipeline, clinic_config: Optional[dict[str, Any]]
) -> None:
    result = _process(pipeline, clinic_config, "particular", "quanto custa?")

    assert _rendered_pricing(result.system_prompt) == "no_quote"
    assert turn_can_show_price(clinic_config, result.atendimento_tipo) is False


@pytest.mark.parametrize(
    ("accepts", "tipo", "provide", "mode", "expected"),
    [
        # A clinic without insurance that provides prices: shown.
        ("no", None, "yes", "consult_only", True),
        (False, None, True, _MISSING, True),
        # consult_only + provide_prices yes: shown (Decision 7).
        ("no", None, "yes", "consult_only", True),
        ("yes", "particular", "yes", "consult_only", True),
        # consult_only + provide_prices not yes: never.
        ("no", None, "no", "consult_only", False),
        ("no", "particular", None, "consult_only", False),
        ("no", None, False, "CONSULT_ONLY", False),
        # insurance accepted + modality undefined: never, whatever else.
        ("yes", None, "yes", "exact_allowed", False),
        (True, None, True, "range_allowed", False),
        # insurance accepted + convenio: never.
        ("yes", "convenio", "yes", "exact_allowed", False),
    ],
)
def test_the_success_criteria_cases(
    pipeline: CognitivePipeline,
    accepts: object,
    tipo: Optional[str],
    provide: object,
    mode: object,
    expected: bool,
) -> None:
    config = _clinic_config(accepts, provide, mode)
    result = _process(pipeline, config, tipo, "quanto custa a consulta?")

    assert turn_can_show_price(config, result.atendimento_tipo) is expected
    assert (_rendered_pricing(result.system_prompt) == "may_quote") is expected


def test_the_extracted_rule_equals_the_template_code_it_came_from() -> None:
    for accepts, tipo, provide, mode in _MATRIX:
        custom = turn_price_variables(_clinic_config(accepts, provide, mode), tipo)
        assert can_show_price(custom) is _reference_template_rule(custom), custom
        disclosure = resolve_price_disclosure(custom)
        assert disclosure.can_show_price is can_show_price(custom)
        assert disclosure.can_show_price is (
            not disclosure.hold_price and disclosure.can_share_price
        )


def test_the_adapter_reads_the_pipelines_keys_and_defaults() -> None:
    assert clinic_price_variables(
        {
            "business_rules": {"accepts_insurance": "yes", "provide_prices": "no"},
            "pricing": {"disclosure_mode": "range_allowed"},
        }
    ) == {
        "pricing_mode": "range_allowed",
        "accepts_insurance": "yes",
        "provide_prices": "no",
    }
    defaults = {
        "pricing_mode": "CONSULT_ONLY",
        "accepts_insurance": False,
        "provide_prices": False,
    }
    assert clinic_price_variables(None) == defaults
    assert clinic_price_variables({}) == defaults
    assert (
        clinic_price_variables({"business_rules": ["no"], "pricing": "exact"})
        == defaults
    )
    assert can_quote_private_pay("particular") is True
    for other in (None, "", "convenio", "Particular", ["particular"]):
        assert can_quote_private_pay(other) is False


def test_the_pipeline_renders_through_the_adapter_and_the_template_through_the_rule(
    pipeline: CognitivePipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter_calls: list[Any] = []
    rule_calls: list[Any] = []
    real_adapter = price_disclosure_policy.clinic_price_variables
    real_rule = normal_template.resolve_price_disclosure

    def adapter_spy(config: Any) -> dict[str, Any]:
        adapter_calls.append(config)
        return real_adapter(config)

    def rule_spy(custom: Any) -> Any:
        rule_calls.append(dict(custom))
        return real_rule(custom)

    monkeypatch.setattr(pipeline_module, "clinic_price_variables", adapter_spy)
    monkeypatch.setattr(normal_template, "resolve_price_disclosure", rule_spy)
    config = _clinic_config("no", "yes", "consult_only")

    result = _process(pipeline, config, None, "quanto custa a consulta?")

    assert adapter_calls == [config]
    assert len(rule_calls) == 1
    assert rule_calls[0]["provide_prices"] == "yes"
    assert rule_calls[0][CAN_QUOTE_PRIVATE_PAY_CUSTOM_KEY] is False
    assert _rendered_pricing(result.system_prompt) == "may_quote"


def test_a_price_turn_offers_media_exactly_when_the_prompt_quotes_a_price(
    pipeline: CognitivePipeline,
) -> None:
    for accepts, tipo, provide, mode in _MATRIX:
        config = _clinic_config(accepts, provide, mode)
        result = _process(pipeline, config, tipo, "quanto custa a consulta?")
        assert result.intent == "pedido_preco"
        # The fixture turn is otherwise eligible: only the price can exclude.
        result.mode = "normal"
        context = build_interaction_context(
            effective_tenant_id=result.tenant_id,
            session_id=result.session_id,
            origin=InteractionOrigin.PRODUCTION,
            event_type="message.in",
        )
        excluded = media_turn_exclusion(result, context, "zapi", config)
        may_quote = _rendered_pricing(result.system_prompt) == "may_quote"
        assert (excluded == "price_hidden") is (not may_quote)
        assert excluded in (None, "price_hidden")
