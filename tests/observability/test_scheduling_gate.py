"""Group 4 — scheduling-gate guard tests.

Covers:
- src.observability.scheduling_gate.detect_scheduling_violation
- src.observability.scheduling_gate.check_scheduling_gate + counter/log
- src.observability.scheduling_gate.get_safe_replacement
- src.observability.scheduling_gate.resolve_gate_mode
- Anti-false-positive corpus against real production literals

Style mirrors tests/observability/test_pii_and_price_gate.py::TestCheckPriceGate.
"""

from __future__ import annotations

from typing import Optional

import pytest
from prometheus_client import REGISTRY

from neurostack_core.flow.handlers.handoff import HANDOFF_RESPONSES
from neurostack_core.prompt.models import PromptVariables
from neurostack_core.prompt.templates.normal import NormalTemplate
from src.handoff.models import HandoffServiceConfig
from src.observability.scheduling_gate import (
    CATEGORY_AVAILABILITY,
    CATEGORY_CONFIRMED,
    CATEGORY_PROCESSED_CHANGE,
    GATE_MODE_ENFORCE,
    GATE_MODE_OBSERVE,
    check_scheduling_gate,
    detect_scheduling_violation,
    get_safe_replacement,
    resolve_gate_mode,
)
from src.services.event_service import EN_HANDOFF_MESSAGES


def _render_handoff_confirmation_pending(language: str) -> str:
    """Render the actual production block instead of maintaining a snapshot."""
    content = (
        NormalTemplate()
        .build_system_message(
            PromptVariables(
                user_message="",
                funnel_stage="agendamento_solicitado",
                last_intent="agendar",
                language=language,
                custom={"handoff_confirmation_pending": True},
            )
        )
        .content
    )
    header = "## ⚠️ MANDATORY ACTION" if language == "en" else "## ⚠️ AÇÃO OBRIGATÓRIA"
    return content[content.index(header) :]


def _violation_count(category: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "neurostack_scheduling_gate_violations_total",
            {"category": category},
        )
        or 0.0
    )


# ============================================
# detect_scheduling_violation — pure detector
# ============================================


class TestDetectSchedulingViolation:
    @pytest.mark.parametrize(
        "text, expected_category",
        [
            # Category (a) — confirmed / booked
            ("seu agendamento está confirmado para amanhã", CATEGORY_CONFIRMED),
            ("Sua consulta foi marcada com sucesso", CATEGORY_CONFIRMED),
            ("Agendei sua consulta para segunda às 10h", CATEGORY_CONFIRMED),
            ("A consulta foi marcada para segunda", CATEGORY_CONFIRMED),
            ("Agendamento confirmado", CATEGORY_CONFIRMED),
            ("Your appointment is confirmed for tomorrow", CATEGORY_CONFIRMED),
            ("Your booking has been scheduled", CATEGORY_CONFIRMED),
            ("I've booked your appointment", CATEGORY_CONFIRMED),
            ("The appointment was booked", CATEGORY_CONFIRMED),
            ("We've scheduled you for Tuesday", CATEGORY_CONFIRMED),
            # Category (b) — availability
            ("esse horário está disponível", CATEGORY_AVAILABILITY),
            ("este dia está livre", CATEGORY_AVAILABILITY),
            ("Temos disponibilidade no dia 12", CATEGORY_AVAILABILITY),
            ("Temos horários disponíveis.", CATEGORY_AVAILABILITY),
            ("Temos vagas livres.", CATEGORY_AVAILABILITY),
            ("Há disponibilidade para amanhã", CATEGORY_AVAILABILITY),
            ("That time is available", CATEGORY_AVAILABILITY),
            ("We have appointments available.", CATEGORY_AVAILABILITY),
            ("We have an appointment free.", CATEGORY_AVAILABILITY),
            ("We have availability on Monday", CATEGORY_AVAILABILITY),
            ("The 10am slot is open", CATEGORY_AVAILABILITY),
            ("We have a slot available", CATEGORY_AVAILABILITY),
            # Category (c) — cancellation / reschedule processed
            ("Seu cancelamento foi processado", CATEGORY_PROCESSED_CHANGE),
            ("Sua remarcação foi realizada", CATEGORY_PROCESSED_CHANGE),
            ("Cancelei sua consulta", CATEGORY_PROCESSED_CHANGE),
            ("Sua consulta foi cancelada", CATEGORY_PROCESSED_CHANGE),
            ("Your reschedule has been done", CATEGORY_PROCESSED_CHANGE),
            ("I've cancelled your appointment", CATEGORY_PROCESSED_CHANGE),
            ("Your appointment has been cancelled", CATEGORY_PROCESSED_CHANGE),
        ],
    )
    def test_positive_matches(self, text: str, expected_category: str) -> None:
        result = detect_scheduling_violation(text)
        assert result is not None, f"Expected match for {text!r}"
        category, offset = result
        assert category == expected_category
        # Offset must land inside the text.
        assert 0 <= offset < len(text)

    def test_none_input_returns_none(self) -> None:
        assert detect_scheduling_violation(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert detect_scheduling_violation("") is None

    def test_non_string_returns_none(self) -> None:
        # detect_scheduling_violation must never raise — even on garbage.
        assert detect_scheduling_violation(12345) is None  # type: ignore[arg-type]
        assert detect_scheduling_violation({"a": 1}) is None  # type: ignore[arg-type]
        assert detect_scheduling_violation([1, 2]) is None  # type: ignore[arg-type]

    def test_clean_conversational_reply(self) -> None:
        # Happy path: normal reply from the collection flow.
        text = (
            "Obrigado! Pode me passar seu nome completo e qual dia e "
            "horário ficam melhores para você?"
        )
        assert detect_scheduling_violation(text) is None

    def test_clean_en_conversational_reply(self) -> None:
        text = (
            "Thanks! Could you share your full name and which day and time "
            "work best for you?"
        )
        assert detect_scheduling_violation(text) is None

    @pytest.mark.parametrize(
        "text",
        [
            "NÃO diga que o horário está confirmado.",
            "Não posso confirmar que sua consulta foi marcada.",
            "Do NOT say the time is confirmed.",
            "I can't confirm that your appointment is scheduled.",
        ],
    )
    def test_negated_or_prohibited_claim_is_not_a_violation(self, text: str) -> None:
        assert detect_scheduling_violation(text) is None

    @pytest.mark.parametrize(
        "text",
        [
            # A sentence boundary ends the scope of the negation.
            "Não posso confirmar que sua consulta foi marcada. Sua consulta foi marcada.",
            "I can't confirm that your appointment is scheduled. Your appointment is scheduled.",
            # A same-sentence adversative clause is an independent assertion.
            "Não posso confirmar que sua consulta foi marcada, mas sua consulta foi marcada.",
            "I can't confirm that your appointment is scheduled, but your appointment is scheduled.",
            "Não posso confirmar que sua consulta foi marcada mas sua consulta foi marcada.",
            "I can't confirm that your appointment is scheduled but your appointment is scheduled.",
        ],
    )
    def test_later_independent_assertion_after_negated_claim_is_a_violation(
        self, text: str
    ) -> None:
        result = detect_scheduling_violation(text)
        assert result is not None
        assert result[0] == CATEGORY_CONFIRMED


# ============================================
# check_scheduling_gate — detector + counter + log
# ============================================


class TestCheckSchedulingGate:
    def test_confirmed_pt_bumps_counter(self) -> None:
        tenant = "t-sched-conf-pt"
        before = _violation_count(CATEGORY_CONFIRMED)
        got = check_scheduling_gate(
            "seu agendamento está confirmado para amanhã", tenant
        )
        assert got == CATEGORY_CONFIRMED
        assert _violation_count(CATEGORY_CONFIRMED) == before + 1

    def test_confirmed_en_bumps_counter(self) -> None:
        tenant = "t-sched-conf-en"
        before = _violation_count(CATEGORY_CONFIRMED)
        got = check_scheduling_gate("your appointment is confirmed", tenant)
        assert got == CATEGORY_CONFIRMED
        assert _violation_count(CATEGORY_CONFIRMED) == before + 1

    def test_later_assertion_after_negated_claim_is_counted_and_logged(
        self, caplog
    ) -> None:
        import logging

        tenant = "t-sched-negated-then-confirmed"
        before = _violation_count(CATEGORY_CONFIRMED)
        caplog.set_level(logging.WARNING)

        got = check_scheduling_gate(
            "Não posso confirmar que sua consulta foi marcada. Sua consulta foi marcada.",
            tenant,
        )

        assert got == CATEGORY_CONFIRMED
        assert _violation_count(CATEGORY_CONFIRMED) == before + 1
        assert any(
            "scheduling_gate_violation" in record.getMessage()
            for record in caplog.records
        )

    def test_availability_pt_bumps_counter(self) -> None:
        tenant = "t-sched-avail-pt"
        before = _violation_count(CATEGORY_AVAILABILITY)
        got = check_scheduling_gate("esse horário está disponível", tenant)
        assert got == CATEGORY_AVAILABILITY
        assert _violation_count(CATEGORY_AVAILABILITY) == before + 1

    def test_availability_en_bumps_counter(self) -> None:
        tenant = "t-sched-avail-en"
        before = _violation_count(CATEGORY_AVAILABILITY)
        got = check_scheduling_gate("that time is available", tenant)
        assert got == CATEGORY_AVAILABILITY
        assert _violation_count(CATEGORY_AVAILABILITY) == before + 1

    def test_processed_change_pt_bumps_counter(self) -> None:
        tenant = "t-sched-proc-pt"
        before = _violation_count(CATEGORY_PROCESSED_CHANGE)
        got = check_scheduling_gate("seu cancelamento foi processado", tenant)
        assert got == CATEGORY_PROCESSED_CHANGE
        assert _violation_count(CATEGORY_PROCESSED_CHANGE) == before + 1

    def test_processed_change_en_bumps_counter(self) -> None:
        tenant = "t-sched-proc-en"
        before = _violation_count(CATEGORY_PROCESSED_CHANGE)
        got = check_scheduling_gate("I've cancelled your appointment", tenant)
        assert got == CATEGORY_PROCESSED_CHANGE
        assert _violation_count(CATEGORY_PROCESSED_CHANGE) == before + 1

    def test_clean_response_returns_none(self) -> None:
        tenant = "t-sched-clean"
        assert (
            check_scheduling_gate(
                "Perfeito! Qual dia e horário ficam melhores para você?",
                tenant,
            )
            is None
        )

    def test_empty_and_none_are_safe(self) -> None:
        assert check_scheduling_gate("", "t-x") is None
        assert check_scheduling_gate(None, "t-x") is None

    def test_missing_tenant_falls_back_to_unknown(self) -> None:
        # No tenant_id is safe because identity never becomes a metric label.
        before = _violation_count(CATEGORY_CONFIRMED)
        got = check_scheduling_gate("your appointment is confirmed", None)
        assert got == CATEGORY_CONFIRMED
        assert _violation_count(CATEGORY_CONFIRMED) == before + 1

    def test_log_sink_failure_does_not_break_the_gate(self, monkeypatch) -> None:
        """The guard is an observability backstop, never a new failure mode."""

        def _broken_warning(*_args, **_kwargs) -> None:
            raise RuntimeError("log sink unavailable")

        monkeypatch.setattr(
            "src.observability.scheduling_gate.logger.warning",
            _broken_warning,
        )
        assert (
            check_scheduling_gate("seu agendamento está confirmado", "t-log-failure")
            == CATEGORY_CONFIRMED
        )

    def test_metric_and_logging_failures_do_not_break_the_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every observability side effect remains best-effort."""

        class _BrokenCounter:
            def labels(self, **_kwargs):
                raise RuntimeError("metrics unavailable")

        def _broken_log(*_args, **_kwargs) -> None:
            raise RuntimeError("log sink unavailable")

        monkeypatch.setattr(
            "src.observability.scheduling_gate.SCHEDULING_GATE_VIOLATIONS",
            _BrokenCounter(),
        )
        monkeypatch.setattr(
            "src.observability.scheduling_gate.logger.error",
            _broken_log,
        )
        monkeypatch.setattr(
            "src.observability.scheduling_gate.logger.warning",
            _broken_log,
        )

        assert (
            check_scheduling_gate(
                "seu agendamento está confirmado", "t-observability-failure"
            )
            == CATEGORY_CONFIRMED
        )

    def test_log_emitted_with_offset_and_no_snippet(self, caplog) -> None:
        # Structured log must carry tenant_id, category, match_offset.
        # It must NOT carry any snippet/excerpt of the response (PII).
        # The PT sample has the match at offset 0.
        import logging

        caplog.set_level(logging.WARNING)
        check_scheduling_gate("seu agendamento está confirmado", "t-log-1")
        matching = [
            rec
            for rec in caplog.records
            if "scheduling_gate_violation" in rec.getMessage()
        ]
        assert matching, "expected a scheduling_gate_violation log record"
        # Structured logger renders key=value pairs into the message.
        joined = " ".join(rec.getMessage() for rec in matching)
        assert "tenant_id" in joined
        assert "category" in joined
        assert "match_offset" in joined
        # No response snippet keys.
        assert "response_snippet" not in joined
        assert "response_text" not in joined
        # And no fragment of the actual response text — the response
        # begins with "seu agendamento", so that literal must not appear.
        assert "seu agendamento" not in joined


# ============================================
# get_safe_replacement — category × language × funnel matrix
# ============================================


class TestGetSafeReplacement:
    @pytest.mark.parametrize("language", ["pt-BR", "pt", None, "en", "en-US", "EN"])
    @pytest.mark.parametrize(
        "category",
        [CATEGORY_CONFIRMED, CATEGORY_AVAILABILITY, CATEGORY_PROCESSED_CHANGE],
    )
    def test_replacement_never_promises_team_follow_up(
        self, category: str, language: Optional[str]
    ) -> None:
        """The gate only fires on turns with no handoff, so promising
        team follow-up would be an unfulfillable promise — the exact
        failure class this wish exists to prevent."""
        text = get_safe_replacement(category, language=language)
        low = text.lower()
        forbidden = [
            "entrará em contato",
            "entrara em contato",
            "will be in touch",
            "will contact you",
            "will get back to you",
            "someone will follow up",
            "our team will follow up",
        ]
        for phrase in forbidden:
            assert (
                phrase not in low
            ), f"[{category}/{language}] replacement promises follow-up: {phrase!r}"

    @pytest.mark.parametrize("language", ["pt-BR", None, "en"])
    @pytest.mark.parametrize(
        "category",
        [CATEGORY_CONFIRMED, CATEGORY_AVAILABILITY, CATEGORY_PROCESSED_CHANGE],
    )
    def test_replacement_never_mentions_humano_or_transfer(
        self, category: str, language: Optional[str]
    ) -> None:
        text = get_safe_replacement(category, language=language)
        low = text.lower()
        for phrase in [
            "humano",
            "atendente humano",
            "transferir",
            "human agent",
            "transfer you",
            "transfer to a human",
        ]:
            assert (
                phrase not in low
            ), f"[{category}/{language}] replacement uses forbidden phrase: {phrase!r}"

    @pytest.mark.parametrize("language", ["pt-BR", "en"])
    def test_availability_does_not_ask_for_full_name(self, language: str) -> None:
        text = get_safe_replacement(
            CATEGORY_AVAILABILITY, language=language, funnel_stage="agendado"
        )
        low = text.lower()
        assert "nome completo" not in low
        assert "full name" not in low

    @pytest.mark.parametrize("language", ["pt-BR", "en"])
    def test_processed_change_does_not_ask_for_full_name(self, language: str) -> None:
        text = get_safe_replacement(
            CATEGORY_PROCESSED_CHANGE, language=language, funnel_stage="agendado"
        )
        low = text.lower()
        assert "nome completo" not in low
        assert "full name" not in low

    @pytest.mark.parametrize("language", ["pt-BR", "en"])
    @pytest.mark.parametrize("funnel_stage", ["agendado", "cancelar", "reagendar"])
    @pytest.mark.parametrize(
        "category",
        [CATEGORY_CONFIRMED, CATEGORY_AVAILABILITY, CATEGORY_PROCESSED_CHANGE],
    )
    def test_non_collection_stages_never_restart_full_name_collection(
        self, category: str, language: str, funnel_stage: str
    ) -> None:
        text = get_safe_replacement(
            category, language=language, funnel_stage=funnel_stage
        ).lower()
        assert "nome completo" not in text
        assert "full name" not in text

    def test_availability_never_cites_a_time_pt(self) -> None:
        text = get_safe_replacement(CATEGORY_AVAILABILITY, language="pt-BR")
        # The reply may not include a specific time or date.
        import re

        assert not re.search(r"\d{1,2}\s*(?:h|:\d{2})", text)
        assert "10h" not in text
        assert "às" not in text or "às " not in text  # "às 14h" style forbidden

    def test_availability_never_cites_a_time_en(self) -> None:
        text = get_safe_replacement(CATEGORY_AVAILABILITY, language="en")
        import re

        assert not re.search(r"\d{1,2}\s*(?:am|pm|:\d{2})", text.lower())

    def test_confirmed_agendado_neither_denies_nor_reaffirms_pt(self) -> None:
        # Funnel = agendado (patient already has an appointment).
        # The reply must not deny ("você não tem agendamento") nor
        # reaffirm ("seu agendamento está confirmado") the existing one.
        text = get_safe_replacement(
            CATEGORY_CONFIRMED, language="pt-BR", funnel_stage="agendado"
        )
        # No affirmative reaffirmation.
        assert (
            detect_scheduling_violation(text) is None
        ), "reply itself must not trip the gate"
        low = text.lower()
        # No explicit denial framings.
        assert "você não tem agendamento" not in low
        assert "voce nao tem agendamento" not in low
        assert "não há agendamento" not in low
        assert "nao ha agendamento" not in low
        assert "not booked" not in low
        assert "no appointment" not in low
        # Also must not restart name/day-time collection in this branch.
        assert "nome completo" not in low
        assert "preferência de dia" not in low
        assert "preferencia de dia" not in low

    def test_confirmed_agendado_neither_denies_nor_reaffirms_en(self) -> None:
        text = get_safe_replacement(
            CATEGORY_CONFIRMED, language="en", funnel_stage="agendado"
        )
        assert detect_scheduling_violation(text) is None
        low = text.lower()
        assert "no appointment" not in low
        assert "not booked" not in low
        assert "you don't have" not in low
        assert "full name" not in low

    def test_confirmed_agendamento_solicitado_may_ask_for_missing_data_pt(
        self,
    ) -> None:
        """Only category (a) with funnel = agendamento_solicitado may
        resume collection — the patient has requested an appointment but
        collection is not yet complete."""
        text = get_safe_replacement(
            CATEGORY_CONFIRMED,
            language="pt-BR",
            funnel_stage="agendamento_solicitado",
        )
        # And it still must not itself trigger the gate.
        assert detect_scheduling_violation(text) is None

    def test_unknown_category_falls_back_safely(self) -> None:
        # Defensive: any unknown category returns the conservative
        # "team validates, I can't confirm" text — never raises.
        text = get_safe_replacement("something_new", language="pt-BR")
        assert isinstance(text, str) and text
        assert detect_scheduling_violation(text) is None


# ============================================
# resolve_gate_mode — env-var rollback lever
# ============================================


class TestResolveGateMode:
    def test_default_is_enforce(self, monkeypatch) -> None:
        monkeypatch.delenv("SCHEDULING_GATE_MODE", raising=False)
        assert resolve_gate_mode() == GATE_MODE_ENFORCE

    def test_observe_activates_observe_mode(self, monkeypatch) -> None:
        monkeypatch.setenv("SCHEDULING_GATE_MODE", "observe")
        assert resolve_gate_mode() == GATE_MODE_OBSERVE

    def test_enforce_is_enforce(self, monkeypatch) -> None:
        monkeypatch.setenv("SCHEDULING_GATE_MODE", "enforce")
        assert resolve_gate_mode() == GATE_MODE_ENFORCE

    @pytest.mark.parametrize(
        "value",
        ["Observe", "OBSERVE", "observ", "off", "no", "0", "true", "1", ""],
    )
    def test_only_exact_observe_activates_observe(
        self, monkeypatch, value: str
    ) -> None:
        # Case-sensitive; anything not exactly "observe" is enforce.
        monkeypatch.setenv("SCHEDULING_GATE_MODE", value)
        assert resolve_gate_mode() == GATE_MODE_ENFORCE

    def test_read_per_call_not_cached(self, monkeypatch) -> None:
        # Flip the env var between calls; both reads must reflect the
        # current value (no lru_cache freeze).
        monkeypatch.setenv("SCHEDULING_GATE_MODE", "observe")
        assert resolve_gate_mode() == GATE_MODE_OBSERVE
        monkeypatch.setenv("SCHEDULING_GATE_MODE", "enforce")
        assert resolve_gate_mode() == GATE_MODE_ENFORCE


# ============================================
# Anti-false-positive corpus — real production literals only.
# ============================================


class TestCorpusNoFalsePositive:
    """Regression guard — the gate must produce zero matches against
    real production strings that are legitimately about scheduling
    (handoff messages, prompt-side prohibition lists)."""

    def test_default_handoff_messages_all_reasons(self) -> None:
        cfg = HandoffServiceConfig()
        for reason, text in cfg.default_handoff_messages.items():
            assert (
                detect_scheduling_violation(text) is None
            ), f"default_handoff_messages[{reason!r}] false-positived: {text!r}"

    def test_en_handoff_messages_all_reasons(self) -> None:
        for reason, text in EN_HANDOFF_MESSAGES.items():
            assert (
                detect_scheduling_violation(text) is None
            ), f"EN_HANDOFF_MESSAGES[{reason!r}] false-positived: {text!r}"

    def test_en_handoff_scheduling_no_longer_promises_confirm(self) -> None:
        """Deliverable 6 #1 — the EN scheduling literal must not read as
        the agent confirming times."""
        text = EN_HANDOFF_MESSAGES["scheduling"]
        low = text.lower()
        # Team is the subject of any follow-up; "I confirm" / "I'll confirm"
        # must be gone.
        assert "confirm the best" not in low
        assert "confirm the times" not in low
        assert "i'll confirm" not in low
        assert "i confirm" not in low

    def test_handoff_responses_all_keys(self) -> None:
        for key, text in HANDOFF_RESPONSES.items():
            # Skip templated placeholders ({wait_time}, {agent_name}).
            if "{" in text and "}" in text:
                # Substitute plausible values so the assertion is meaningful.
                text = text.format(wait_time="5", agent_name="Ana")
            assert (
                detect_scheduling_violation(text) is None
            ), f"HANDOFF_RESPONSES[{key!r}] false-positived: {text!r}"

    def test_handoff_confirmation_pending_pt_block(self) -> None:
        # Even though the gate never reads this at runtime (it's part of
        # the system prompt, not suggested_response), the corpus test
        # asserts zero match so any future regex tightening stays safe.
        assert (
            detect_scheduling_violation(_render_handoff_confirmation_pending("pt-BR"))
            is None
        )

    def test_handoff_confirmation_pending_en_block(self) -> None:
        assert (
            detect_scheduling_violation(_render_handoff_confirmation_pending("en"))
            is None
        )

    def test_no_match_on_price_gate_wordings(self) -> None:
        # Sanity: price replies must not trigger scheduling gate.
        assert detect_scheduling_violation("A consulta custa R$ 200") is None
        assert detect_scheduling_violation("The consultation is R$ 200") is None

    def test_no_match_on_collection_prompt(self) -> None:
        # The standard "please share your full name and preferred time" reply.
        text = (
            "Perfeito, Maria! Pode me passar seu nome completo e qual dia "
            "e horário ficam melhores para você?"
        )
        assert detect_scheduling_violation(text) is None
