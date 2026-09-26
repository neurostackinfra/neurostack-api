"""
Scheduling / confirmation / availability forbidden-pattern scan.

Root-cause guard for the clinic-churn incident: the agent must NEVER
- confirm an appointment,
- state a specific time slot is available/free/open,
- process a cancellation or reschedule.

The agent has NO ERP/calendar access. Its only role is to collect
(name + preference) and hand off to the human team.

This scan renders `get_context_instructions` for every combination of
funnel_stage x last_intent x language (pt-BR, en) and asserts zero
polarity-positive occurrences of the forbidden pattern set.

Rule (polarity-aware):
    A match counts as a violation only when the physical line it occurs
    on carries NO negation marker (NUNCA, NÃO, NEVER, "Do NOT").
    The unit of analysis is the line, not the whole document.

Constraint enforced by deliverable 1: prohibition and imperative must
never share a physical line — every prohibition is on its own line.
Anchor negatives below verify this constraint holds on the currently
shipped file.
"""

from __future__ import annotations

import re
from typing import Iterable

import pytest

from neurostack_core.prompt.models import PromptVariables
from neurostack_core.prompt.templates.normal import (
    EN_FUNNEL_INSTRUCTIONS,
    FUNNEL_INSTRUCTIONS,
    NormalTemplate,
)

# ---------------------------------------------------------------------------
# Forbidden pattern set (pt-BR + en)
# Each pattern captures one of the three forbidden behaviors:
#   (a) confirming an appointment,
#   (b) stating a specific time slot is available/free/open,
#   (c) processing a cancellation or reschedule.
# Patterns are intentionally broader than the enumerated table so that
# occurrences not listed in the wish also fail.
# ---------------------------------------------------------------------------

FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # (a) confirming an appointment — PT
    (
        "pt: confirmed-appointment",
        re.compile(
            r"(agendamento|consulta|hor[áa]rio)\s+"
            r"(est[áa]|foi|j[áa]|agora)\s+"
            r"(confirmad[oa]|agendad[oa]|marcad[oa])",
            re.IGNORECASE,
        ),
    ),
    (
        "pt: confirme o agendamento (imperative)",
        re.compile(
            r"\bconfirm[ea]\b(?![^\n]*\b(NUNCA|N[ÃA]O|NEVER)\b)"
            r"[^\n]*\b(o\s+agendamento|a\s+consulta|a\s+presen[çc]a|com\s+data)\b",
            re.IGNORECASE,
        ),
    ),
    # (a) confirming an appointment — EN
    (
        "en: appointment is confirmed / scheduled / booked",
        re.compile(
            r"\b(your\s+)?appointment\s+"
            r"(is|has\s+been)\s+"
            r"(confirmed|scheduled|booked)",
            re.IGNORECASE,
        ),
    ),
    (
        "en: confirm the appointment (imperative)",
        re.compile(
            r"\bconfirm\s+(the\s+)?(appointment|scheduling|booking)\b",
            re.IGNORECASE,
        ),
    ),
    # (b) stating time-slot availability — PT
    (
        "pt: horário está disponível / livre",
        re.compile(
            r"(hor[áa]rio|slot|vaga|dia)\s+(est[áa]|fica)\s+(dispon[íi]vel|livre|aberto)",
            re.IGNORECASE,
        ),
    ),
    (
        "pt: informe/tem horários disponíveis",
        re.compile(
            r"(informe|tem|temos|h[áa])\s+(os\s+)?hor[áa]rios?\s+dispon[íi]ve(l|is)",
            re.IGNORECASE,
        ),
    ),
    # (b) stating time-slot availability — EN
    (
        "en: that time/slot is available/open/free",
        re.compile(
            r"(that\s+)?(time|slot|day)\s+is\s+(available|open|free)",
            re.IGNORECASE,
        ),
    ),
    (
        "en: available times / time slots",
        re.compile(r"\bavailable\s+(times?|(time\s+)?slots?)\b", re.IGNORECASE),
    ),
    # (c) processing a cancellation or reschedule — PT
    (
        "pt: processe o cancelamento/reagendamento",
        re.compile(
            r"process[ae]\s+(o\s+)?(cancelamento|reagendamento|agendamento)",
            re.IGNORECASE,
        ),
    ),
    (
        "pt: cancelamento foi/está feito/processado/confirmado",
        re.compile(
            r"(cancelamento|reagendamento)\s+"
            r"(foi|est[áa])\s+"
            r"(feito|processad[oa]|confirmad[oa]|efetuad[oa]|realizad[oa])",
            re.IGNORECASE,
        ),
    ),
    (
        "pt: confirme e processe",
        re.compile(r"\bconfirme\s+e\s+processe\b", re.IGNORECASE),
    ),
    # (c) processing a cancellation or reschedule — EN
    (
        "en: process the cancellation/reschedule",
        re.compile(
            r"\bprocess\s+(the\s+)?(cancellation|cancelation|reschedule|rescheduling|scheduling)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "en: process without resistance",
        re.compile(r"\bprocess\s+(it\s+)?without\s+resistance\b", re.IGNORECASE),
    ),
    (
        "en: cancellation/reschedule is done/processed",
        re.compile(
            r"(cancellation|cancelation|reschedule|rescheduling)\s+"
            r"(is|has\s+been)\s+"
            r"(done|processed|confirmed|completed)",
            re.IGNORECASE,
        ),
    ),
]


# Negation markers — if any of these appears on the same physical line as
# a match, the match is treated as a prohibition, not a violation.
NEGATION_MARKERS_RE = re.compile(
    r"(NUNCA|N[ÃA]O\b|NEVER\b|Do\s+NOT\b|DO\s+NOT\b|CANNOT\b|NO\s+ERP|N[ÃA]O\s+existe|there\s+is\s+NO|N[ÃA]O\s+tem)",
    re.IGNORECASE,
)


def _find_violations(text: str) -> list[tuple[str, str, int]]:
    """Return [(pattern_name, line_content, line_number), ...] for lines
    that contain a forbidden pattern AND no negation marker."""
    violations: list[tuple[str, str, int]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if NEGATION_MARKERS_RE.search(line):
            continue
        for name, pattern in FORBIDDEN_PATTERNS:
            if pattern.search(line):
                violations.append((name, line, lineno))
    return violations


# ---------------------------------------------------------------------------
# Enumeration — the scan must cover every rendered combination.
# ---------------------------------------------------------------------------

# All funnel stages present in either dict.
_FUNNEL_STAGES: list[str] = sorted(
    set(FUNNEL_INSTRUCTIONS.keys()) | set(EN_FUNNEL_INSTRUCTIONS.keys())
)

# Full intent list from _get_intent_instruction (mirrors normal.py).
_INTENTS: list[str] = [
    "saudacao",
    "interesse_tratamento",
    "qualificacao_publico",
    "pedido_preco",
    "agendar",
    "reagendar",
    "cancelar",
    "confirmar_presenca",
    "disponibilidade_horarios",
    "detalhes_servicos",
    "informacoes_profissionais",
    "faq",
    "pre_atendimento",
    "cultura_historia",
    "pos_atendimento_duvida",
    "pos_atendimento_sintomas_preocupantes",
    "pedido_alterar_medicacao",
    "reclamacao",
    "conduta_inadequada_ameaca",
    "objecao_vou_pensar",
    "comparacao_tratamentos",
    "documentos_e_comprovantes",
    "orcamento_existente",
    "cobranca_renegociacao",
    "duvida_generica_sem_contexto",
]

_LANGUAGES: list[str] = ["pt-BR", "en"]


def _combinations() -> Iterable[tuple[str, str, str]]:
    for lang in _LANGUAGES:
        for stage in _FUNNEL_STAGES:
            for intent in _INTENTS:
                yield lang, stage, intent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def template() -> NormalTemplate:
    return NormalTemplate()


def _make_vars(lang: str, stage: str, intent: str) -> PromptVariables:
    return PromptVariables(
        user_message="",
        funnel_stage=stage,
        last_intent=intent,
        channel="whatsapp",
        turn_count=3,
        language=lang,
        custom={
            "agent_name": "Ana",
            "clinic_name": "Clínica Teste",
        },
    )


# ---------------------------------------------------------------------------
# ANCHOR CASES — scan self-validation
# ---------------------------------------------------------------------------


def test_anchor_positive_pt_line_from_pre_change_file():
    """The scan MUST flag the exact PT line that lived at normal.py:311
    before this group's edits. Embedded as a literal string here because
    the line is deleted from the file by deliverable 1 and therefore
    cannot be read from disk after the change."""
    line = '- Confirmação: "Sim, seu agendamento está confirmado para [data/hora]."'
    violations = _find_violations(line)
    assert violations, (
        "Scan failed to detect the confirmed-appointment PT anchor line. "
        "The forbidden pattern set is too narrow."
    )


def test_anchor_positive_en_line_from_pre_change_file():
    """The scan MUST flag the exact EN line that lived at normal.py:491
    before this group's edits."""
    line = '- Confirmation: "Yes, your appointment is confirmed for [date/time]."'
    violations = _find_violations(line)
    assert violations, (
        "Scan failed to detect the confirmed-appointment EN anchor line. "
        "The forbidden pattern set is too narrow."
    )


def test_anchor_negative_pt_prohibition_line_survives(template: NormalTemplate):
    """The PT prohibition line (`NUNCA diga "está disponível", NUNCA
    confirme o agendamento`) survives in the rendered output and MUST
    NOT be flagged. Rendered via the actual template — not a literal —
    to prove the negation-aware rule works on the real output."""
    variables = _make_vars("pt-BR", "agendamento_solicitado", "agendar")
    rendered = template.get_context_instructions(variables)
    assert (
        "NUNCA confirme o agendamento" in rendered
    ), "Prohibition line was removed — deliverable 1 broke the guard."
    violations = _find_violations(rendered)
    # Only assert that the pt prohibition line itself is not flagged.
    for _name, line, _no in violations:
        assert (
            "NUNCA confirme" not in line
        ), f"Scan wrongly flagged a prohibition line as a violation: {line!r}"


def test_anchor_negative_en_prohibition_line_survives(template: NormalTemplate):
    """The EN prohibition line (`NEVER say "that time is available",
    NEVER confirm the appointment`) survives in the rendered output and
    MUST NOT be flagged."""
    variables = _make_vars("en", "agendamento_solicitado", "agendar")
    rendered = template.get_context_instructions(variables)
    assert (
        "NEVER confirm the appointment" in rendered
    ), "Prohibition line was removed — deliverable 1 broke the guard."
    violations = _find_violations(rendered)
    for _name, line, _no in violations:
        assert (
            "NEVER confirm" not in line
        ), f"Scan wrongly flagged a prohibition line as a violation: {line!r}"


# ---------------------------------------------------------------------------
# GLOBAL SCAN — full funnel x intent x language product
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "funnel_stage", "intent"),
    list(_combinations()),
)
def test_rendered_context_has_no_forbidden_scheduling_instruction(
    template: NormalTemplate,
    language: str,
    funnel_stage: str,
    intent: str,
) -> None:
    variables = _make_vars(language, funnel_stage, intent)
    rendered = template.get_context_instructions(variables)
    violations = _find_violations(rendered)
    if violations:
        detail = "\n".join(
            f"  [{name}] line {lineno}: {line.strip()}"
            for name, line, lineno in violations
        )
        pytest.fail(
            f"Forbidden scheduling instruction leaked into rendered prompt "
            f"(lang={language}, funnel={funnel_stage}, intent={intent}):\n{detail}"
        )


# ---------------------------------------------------------------------------
# Deliverable 2 — global rule states no ERP/calendar access exists.
# The rule must be present in both PT and EN system prompts.
# ---------------------------------------------------------------------------


def test_pt_global_rule_declares_no_erp(template: NormalTemplate):
    variables = _make_vars("pt-BR", "lead_novo", "saudacao")
    system = template.get_system_prompt(variables)
    lowered = system.lower()
    # Assert the rule about no ERP/agenda integration is explicit.
    assert "erp" in lowered or "agenda" in lowered
    assert any(
        marker in system
        for marker in (
            "NÃO existe nenhum outro agente",
            "NÃO tem acesso à agenda",
        )
    ), "PT global rule must declare no ERP/calendar access exists."


def test_en_global_rule_declares_no_erp(template: NormalTemplate):
    variables = _make_vars("en", "lead_novo", "saudacao")
    system = template.get_system_prompt(variables)
    assert (
        "ERP" in system or "calendar" in system.lower() or "schedule" in system.lower()
    )
    assert any(
        marker in system
        for marker in (
            "no ERP/calendar integration",
            "NO other agent",
            "do NOT have access to the clinic schedule",
        )
    ), "EN global rule must declare no ERP/calendar access exists."


# ---------------------------------------------------------------------------
# PT/EN parity — every stage that exists in one dict exists in the other,
# and neither language contains a scheduling prohibition the other lacks.
# ---------------------------------------------------------------------------


def test_pt_en_funnel_dict_parity():
    pt_keys = set(FUNNEL_INSTRUCTIONS.keys())
    en_keys = set(EN_FUNNEL_INSTRUCTIONS.keys())
    assert pt_keys == en_keys, (
        f"PT/EN funnel keys diverge — pt-only={pt_keys - en_keys}, "
        f"en-only={en_keys - pt_keys}"
    )


# ---------------------------------------------------------------------------
# Regression guards — collection flow + handoff_confirmation_pending block
# + insurance addendum MUST remain intact.
# ---------------------------------------------------------------------------


def test_collection_flow_preserved_pt(template: NormalTemplate):
    variables = _make_vars("pt-BR", "agendamento_solicitado", "agendar")
    rendered = template.get_context_instructions(variables)
    assert "nome completo" in rendered.lower()
    assert "preferência" in rendered.lower() or "preferencia" in rendered.lower()
    assert "encaminhar" in rendered.lower()


def test_collection_flow_preserved_en(template: NormalTemplate):
    variables = _make_vars("en", "agendamento_solicitado", "agendar")
    rendered = template.get_context_instructions(variables)
    lowered = rendered.lower()
    assert "full name" in lowered
    assert "day and time" in lowered or "time preference" in lowered
    assert "hand off" in lowered or "scheduling team" in lowered


def test_handoff_confirmation_pending_block_intact_pt(template: NormalTemplate):
    variables = PromptVariables(
        user_message="",
        funnel_stage="agendamento_solicitado",
        last_intent="agendar",
        channel="whatsapp",
        turn_count=4,
        language="pt-BR",
        custom={
            "agent_name": "Ana",
            "clinic_name": "Clínica Teste",
            "handoff_confirmation_pending": True,
        },
    )
    msg = template.build_system_message(variables)
    assert "Posso te encaminhar para nossa equipe de agendamento agora?" in msg.content
    assert "AÇÃO OBRIGATÓRIA" in msg.content
    # And the scan still passes on this rendered content.
    violations = _find_violations(msg.content)
    assert not violations, (
        "Forbidden pattern leaked into the handoff_confirmation_pending block: "
        f"{violations}"
    )


def test_insurance_addendum_intact_pt(template: NormalTemplate):
    variables = PromptVariables(
        user_message="",
        funnel_stage="agendamento_solicitado",
        last_intent="agendar",
        channel="whatsapp",
        turn_count=4,
        language="pt-BR",
        custom={
            "agent_name": "Ana",
            "clinic_name": "Clínica Teste",
            "atendimento_tipo": "convenio",
        },
    )
    rendered = template.get_context_instructions(variables)
    assert "convênio" in rendered.lower()
    assert "carteirinha" in rendered.lower()
    assert "por escrito" in rendered.lower()
