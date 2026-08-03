"""Group 4 — observability tests for carteirinha PII masking + price-gate guard.

Covers:
- src.observability.pii.mask_carteirinha (LGPD masking of card numbers)
- src.observability.price_gate.check_price_gate + PRICE_GATE_VIOLATIONS counter
"""

from prometheus_client import REGISTRY

from src.observability.pii import (
    MASK_TOKEN,
    mask_carteirinha,
    mask_carteirinha_in_mapping,
)
from src.observability.price_gate import check_price_gate


# ============================================
# mask_carteirinha
# ============================================


class TestMaskCarteirinha:
    def test_masks_bare_number_reply(self):
        # The most common way a patient sends the card number: a bare digit run.
        assert mask_carteirinha("998877665544") == MASK_TOKEN

    def test_masks_number_with_context_words(self):
        out = mask_carteirinha("minha carteirinha é 123456789")
        assert "123456789" not in out
        assert MASK_TOKEN in out

    def test_masks_space_grouped_digits(self):
        out = mask_carteirinha("12 3456 7890")
        assert MASK_TOKEN in out
        assert "3456" not in out

    def test_masks_hyphen_grouped_digits(self):
        out = mask_carteirinha("1234-5678-90")
        assert MASK_TOKEN in out
        assert "5678" not in out

    def test_preserves_price_with_dots_and_comma(self):
        text = "A consulta custa R$ 1.250,00"
        assert mask_carteirinha(text) == text

    def test_preserves_time(self):
        text = "Podemos marcar às 14:30"
        assert mask_carteirinha(text) == text

    def test_preserves_slash_date(self):
        text = "Sua consulta é 15/03/2026"
        assert mask_carteirinha(text) == text

    def test_short_number_not_masked(self):
        # 5 digits is below the 6-digit threshold.
        text = "número 12345"
        assert mask_carteirinha(text) == text

    def test_none_passthrough(self):
        assert mask_carteirinha(None) is None

    def test_empty_string_passthrough(self):
        assert mask_carteirinha("") == ""

    def test_non_string_passthrough(self):
        assert mask_carteirinha(12345678) == 12345678


class TestMaskCarteirinhaInMapping:
    def test_masks_recognised_keys_only(self):
        data = {
            "content": "carteirinha 998877665544",
            "other": "carteirinha 998877665544",
        }
        out = mask_carteirinha_in_mapping(data)
        assert MASK_TOKEN in out["content"]
        # Non-recognised keys are left untouched.
        assert out["other"] == "carteirinha 998877665544"

    def test_recurses_into_list(self):
        data = [{"content": "998877665544"}, {"content": "no digits here"}]
        out = mask_carteirinha_in_mapping(data)
        assert out[0]["content"] == MASK_TOKEN
        assert out[1]["content"] == "no digits here"

    def test_non_container_passthrough(self):
        assert mask_carteirinha_in_mapping("plain") == "plain"


# ============================================
# check_price_gate / PRICE_GATE_VIOLATIONS
# ============================================


def _violation_count(tenant_id: str, atendimento_tipo: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "neurostack_price_gate_violations_total",
            {"tenant_id": tenant_id, "atendimento_tipo": atendimento_tipo},
        )
        or 0.0
    )


class TestCheckPriceGate:
    def test_violation_on_convenio_with_price(self):
        tenant = "t-conv-1"
        before = _violation_count(tenant, "convenio")
        assert check_price_gate("A consulta custa R$ 200", "convenio", tenant) is True
        assert _violation_count(tenant, "convenio") == before + 1

    def test_no_violation_for_particular(self):
        tenant = "t-part-1"
        before = _violation_count(tenant, "particular")
        assert check_price_gate("A consulta custa R$ 200", "particular", tenant) is False
        # particular is never a violation, so nothing is recorded under it.
        assert _violation_count(tenant, "particular") == before

    def test_no_violation_without_price(self):
        tenant = "t-noprice-1"
        assert check_price_gate("Vamos agendar sua consulta", "convenio", tenant) is False
        assert _violation_count(tenant, "convenio") == 0.0

    def test_undefined_tipo_with_price_is_violation(self):
        tenant = "t-undef-1"
        before = _violation_count(tenant, "undefined")
        assert check_price_gate("R$ 350 por sessão", None, tenant) is True
        assert _violation_count(tenant, "undefined") == before + 1

    def test_empty_response_no_violation(self):
        assert check_price_gate("", "convenio", "t-empty") is False
        assert check_price_gate(None, "convenio", "t-empty") is False

    def test_reais_wording_triggers_violation(self):
        tenant = "t-reais-1"
        before = _violation_count(tenant, "convenio")
        assert check_price_gate("são duzentos reais", "convenio", tenant) is True
        assert _violation_count(tenant, "convenio") == before + 1

    def test_plain_integer_does_not_trigger(self):
        # Bare integers must not be read as prices.
        assert check_price_gate("seu protocolo é 200", "convenio", "t-int") is False

    def test_never_raises_on_missing_tenant(self):
        # tenant_id is optional; must fall back to "unknown" without raising.
        assert check_price_gate("R$ 100", "convenio", None) is True
