"""
NeuroStack — Scheduling-Gate Guard (agenda safety observability)

Standalone observability guard that detects when the LLM response asserts:
  (a) an appointment is confirmed / booked / scheduled,
  (b) a specific time slot is available,
  (c) a cancellation or reschedule was processed.

The agent has no ERP/calendar integration, so any of the above is an
unfulfillable promise — the failure mode that lost a clinic account and
forced this whole wish. Prompt-side hardening (Group 3) removed the
instructions telling the LLM to confirm; this module is the deterministic
runtime backstop for anything the LLM still emits.

Lives in its own module — separate from ``observability.metrics`` — on
purpose: ``observability.metrics`` is a legacy business-metrics module
that re-declares Prometheus counters already owned by ``srm.engine``
(e.g. ``neurostack_srm_decisions_total``). Importing it into the live
pipeline/route graph would collide with ``srm.engine`` and crash app
startup. This guard only needs its own counter, so it stays dependency-
light and safe to import from the pipeline.
"""

from __future__ import annotations

import os
import re
from typing import Optional

from prometheus_client import Counter

from src.config.logging import get_logger


logger = get_logger(__name__)


# Category tokens exposed for callers/tests so the string constants stay in
# one place (they double as counter label values and as replacement-text
# selector keys).
CATEGORY_CONFIRMED = "confirmed_appointment"
CATEGORY_AVAILABILITY = "asserted_availability"
CATEGORY_PROCESSED_CHANGE = "processed_change"


# Increments whenever the finalized LLM response asserts a scheduling
# outcome the agent cannot actually deliver (see module docstring for
# the three categories). Must stay at zero on the happy path — every
# increment points to a leak past the Group-3 prompt hardening.
SCHEDULING_GATE_VIOLATIONS = Counter(
    "neurostack_scheduling_gate_violations_total",
    "Times the finalized LLM response asserted a scheduling outcome the agent cannot deliver",
    ["tenant_id", "category"],
)


# ─────────────────────────────────────────────────────────────────────────────
# Regex patterns — PT + EN, three categories.
#
# Design constraints (verified against the anti-false-positive corpus in
# tests/observability/test_scheduling_gate.py::TestCorpusNoFalsePositive):
#
# 1. Match natural claim forms, including possessive, first-person and bare
#    appointment subjects. A small polarity check below excludes prohibitions
#    such as ``NÃO diga que o horário está confirmado`` and ``Do NOT say the
#    time is confirmed``; that prevents the patterns from becoming a
#    whitelist of only the examples that happened to leak before.
#
# 2. Availability requires an assertion frame (``está disponível``,
#    ``temos disponibilidade``, ``that slot is available``) rather than the
#    noun phrase alone. The handoff literal ``verificará os melhores horários
#    disponíveis`` delegates a future check to the team and does not assert
#    availability now.
#
# 3. Processed-change patterns require an outcome/completion verb tied to a
#    cancellation or reschedule. They do not fire merely because the patient
#    asks to cancel or reschedule.
# ─────────────────────────────────────────────────────────────────────────────

# Category (a) — appointment confirmed / booked / scheduled.
_CONFIRMED_RE = re.compile(
    # PT: "seu agendamento está confirmado", "sua consulta foi marcada"
    r"\b(?:seu|sua|teu|tua|o\s+seu|a\s+sua)\s+"
    r"(?:agendamento|consulta|marcação|marcacao|horário|horario|atendimento|sessão|sessao)s?\s+"
    r"(?:já\s+|ja\s+)?(?:está|esta|foi|acabou\s+de\s+ser|encontra-se|encontrase)\s+"
    r"(?:confirmad[oa]s?|agendad[oa]s?|marcad[oa]s?|reservad[oa]s?)\b"
    # PT bare subject: "a consulta foi marcada", "o agendamento confirmado"
    r"|\b(?:o|a)\s+"
    r"(?:agendamento|consulta|marcação|marcacao|horário|horario|atendimento|sessão|sessao|reserva)s?\s+"
    r"(?:já\s+|ja\s+)?(?:está|esta|foi|ficou|acabou\s+de\s+ser)\s+"
    r"(?:confirmad[oa]s?|agendad[oa]s?|marcad[oa]s?|reservad[oa]s?)\b"
    # PT first-person: "agendei sua consulta"
    r"|\b(?:agendei|marquei|confirmei|reservei|agendamos|marcamos|confirmamos|reservamos)\s+"
    r"(?:o\s+seu|a\s+sua|seu|sua|teu|tua)\s+"
    r"(?:agendamento|consulta|marcação|marcacao|horário|horario|atendimento|sessão|sessao)\b"
    # PT sucess phrase: "agendamento confirmado com sucesso"
    r"|\b(?:agendamento|consulta|marcação|marcacao|reserva)s?\s+"
    r"(?:foi\s+)?(?:confirmad[oa]s?|agendad[oa]s?|marcad[oa]s?|reservad[oa]s?)"
    r"(?:\s+com\s+sucesso)?\b"
    # EN possessive: "your appointment is confirmed"
    r"|\byour\s+"
    r"(?:appointment|booking|consultation|visit|schedule|session|slot)\s+"
    r"(?:is|was|has\s+been|'s\s+been|has\s+just\s+been)\s+"
    r"(?:confirmed|booked|scheduled|set|reserved)\b"
    # EN bare subject: "the appointment was booked", "appointment scheduled"
    r"|\b(?:the\s+)?"
    r"(?:appointment|booking|consultation|visit|reservation)\s+"
    r"(?:(?:is|was|has\s+been|had\s+been)\s+)?"
    r"(?:confirmed|booked|scheduled|set|reserved)\b"
    # EN first-person: "I've booked your appointment"
    r"|\b(?:I|we)\s*(?:'ve|\s+have)?\s*(?:just\s+)?(?:booked|scheduled|confirmed|reserved)\s+"
    r"(?:your|you\s+for)\b"
    r"|\b(?:we\s+have|we've)\s+(?:you\s+)?(?:booked|scheduled)\s+"
    r"(?:for|at|on)\b",
    flags=re.IGNORECASE,
)


# Category (b) — asserted time-slot availability.
_AVAILABILITY_RE = re.compile(
    # PT verb form: "esse horário está disponível", "este dia está livre"
    r"\b(?:esse|este|essa|esta|o|a|nesse|neste|nessa|nesta)\s+"
    r"(?:horário|horario|dia|slot|período|periodo|momento|turno|hora)s?\s+"
    r"(?:está|esta|estão|estao|se\s+encontra|encontra-se)\s+"
    r"(?:disponível|disponivel|disponíveis|disponiveis|livre|livres|vago|vagos|liberad[oa]s?|em\s+aberto)\b"
    # PT availability frame: "temos disponibilidade no dia X"
    r"|\btemos\s+(?:disponibilidade|horário|horario|vaga|slot)s?\s+"
    r"(?:no|na|nos|nas|para|em|às|as|no\s+dia|na\s+data)\b"
    # PT direct plural assertion: "temos horários disponíveis"
    r"|\btemos\s+(?:horários?|horarios?|vagas?|slots?)\s+"
    r"(?:(?:estão|estao)\s+)?"
    r"(?:disponíveis|disponiveis|livres|vagos|abertos)\b"
    # PT "há/tem vaga/horário no dia X"
    r"|\b(?:há|ha|tem|temos)\s+(?:vaga|horário|horario|slot)s?\s+"
    r"(?:no|na|nos|nas|em|para|às|as|amanhã|amanha|hoje|"
    r"segunda|terça|terca|quarta|quinta|sexta|sábado|sabado|domingo|"
    r"no\s+dia|na\s+data)\b"
    # PT direct availability claim: "há disponibilidade para amanhã"
    r"|\b(?:há|ha|tem|temos)\s+disponibilidade\s+"
    r"(?:no|na|nos|nas|em|para|às|as|amanhã|amanha|hoje|"
    r"segunda|terça|terca|quarta|quinta|sexta|sábado|sabado|domingo|"
    r"no\s+dia|na\s+data)\b"
    # PT direct assertion: "sim, X horas está disponível"
    r"|\b\d{1,2}(?::\d{2})?\s*(?:h|hs|horas|hrs)?\s+(?:está|esta)\s+"
    r"(?:disponível|disponivel|livre|vago|liberad[oa])\b"
    # EN verb form: "that time is available", "that slot is free"
    r"|\b(?:that|this)\s+(?:time|slot|day|hour|appointment|opening)\s+"
    r"(?:is|'s)\s+(?:available|free|open|unbooked)\b"
    # EN direct availability claim: "the slot is available", "10am slot is open"
    r"|\bthe\s+(?:time|slot|day|hour|appointment|opening)\s+"
    r"(?:is|'s)\s+(?:available|free|open|unbooked)\b"
    r"|\b\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?\s+"
    r"(?:time|slot)\s+is\s+(?:available|free|open)\b"
    # EN availability frame: "we have availability on X"
    r"|\bwe\s*(?:'ve|\s+have)?\s+(?:availability|openings?|slots?|a\s+slot|a\s+time|space)\s+"
    r"(?:on|at|for|tomorrow|today|next|this)\b"
    r"|\bwe\s*(?:'ve|\s+have)?\s+(?:an?\s+)?"
    r"(?:opening|slot|time)\s+(?:available|free|open)\b"
    # EN direct assertion: "we have appointments available"
    r"|\bwe\s*(?:'ve|\s+have)?\s+(?:an?\s+)?"
    r"appointments?\s+(?:available|free|open)\b"
    # EN "yes, X is available"
    r"|\b\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?\s+is\s+"
    r"(?:available|free|open)\b",
    flags=re.IGNORECASE,
)


# Category (c) — cancellation / reschedule asserted as processed.
_PROCESSED_CHANGE_RE = re.compile(
    # PT possessive: "seu cancelamento foi feito", "sua remarcação está processada"
    r"\b(?:seu|sua|teu|tua|o\s+seu|a\s+sua)\s+"
    r"(?:cancelamento|reagendamento|remarcação|remarcacao|remarcamento)\s+"
    r"(?:já\s+|ja\s+)?(?:foi|está|esta|acabou\s+de\s+ser|encontra-se|encontrase)\s+"
    r"(?:feit[oa]s?|processad[oa]s?|confirmad[oa]s?|realizad[oa]s?|efetuad[oa]s?|conclu[íi]d[oa]s?)\b"
    # PT bare noun + completion verb: "cancelamento foi processado"
    r"|\b(?:cancelamento|reagendamento|remarcação|remarcacao|remarcamento)\s+"
    r"(?:foi|está|esta)\s+"
    r"(?:feit[oa]s?|processad[oa]s?|realizad[oa]s?|efetuad[oa]s?|conclu[íi]d[oa]s?)\b"
    # PT appointment outcome: "sua consulta foi cancelada/reagendada"
    r"|\b(?:seu|sua|teu|tua|o\s+seu|a\s+sua)\s+"
    r"(?:agendamento|consulta|marcação|marcacao|horário|horario|atendimento|sessão|sessao)s?\s+"
    r"(?:já\s+|ja\s+)?(?:foi|está|esta|acabou\s+de\s+ser)\s+"
    r"(?:cancelad[oa]s?|reagendad[oa]s?|remarcad[oa]s?)\b"
    # PT first-person: "cancelei seu agendamento", "remarquei sua consulta"
    r"|\b(?:cancelei|reagendei|remarquei|processei|realizei|efetuei|conclui|cancelamos|reagendamos|remarcamos)\s+"
    r"(?:o\s+seu|a\s+sua|seu|sua|teu|tua)\s+"
    r"(?:agendamento|consulta|marcação|marcacao|horário|horario|"
    r"cancelamento|reagendamento|remarcação|remarcacao|sessão|sessao)\b"
    # EN possessive: "your cancellation is done", "your reschedule was processed"
    r"|\byour\s+"
    r"(?:cancellation|cancelation|reschedul(?:ing|e)|rebooking)\s+"
    r"(?:is|was|has\s+been|'s\s+been)\s+"
    r"(?:done|processed|confirmed|complete|completed)\b"
    # EN appointment outcome: "your appointment has been cancelled"
    r"|\byour\s+"
    r"(?:appointment|booking|consultation|visit|schedule|session|slot)\s+"
    r"(?:is|was|has\s+been|'s\s+been)\s+"
    r"(?:cancelled|canceled|rescheduled|rebooked|moved)\b"
    # EN first-person: "I've cancelled your appointment"
    r"|\bI\s*(?:'ve|\s+have)?\s*(?:just\s+)?"
    r"(?:cancell?ed|reschedul(?:ed|ing)|rebooked|moved|processed)\s+"
    r"your\s+"
    r"(?:appointment|booking|consultation|visit|schedule|session|slot)\b",
    flags=re.IGNORECASE,
)


# Order matters only for the "which category won" reporting — a response
# that matches more than one is charged to the first-matched category by
# ordering here (confirmed > availability > processed_change). Every
# category still gets its own counter, and enforce mode replaces once with
# that category's safe text.
_CATEGORY_ORDER: tuple[tuple[str, re.Pattern[str]], ...] = (
    (CATEGORY_CONFIRMED, _CONFIRMED_RE),
    (CATEGORY_AVAILABILITY, _AVAILABILITY_RE),
    (CATEGORY_PROCESSED_CHANGE, _PROCESSED_CHANGE_RE),
)


# The corpus includes actual prompt text that says what the assistant must
# *not* say. These are not patient-facing LLM replies, but keeping the
# detector polarity-aware prevents a prompt leak from being treated as a
# scheduling outcome. This is deliberately a semantic prohibition frame,
# never a list of individual strings to allow through.
_NEGATED_CLAIM_PREFIX_RE = re.compile(
    r"\b(?:"
    r"n(?:ão|ao)\s+(?:diga|afirme|confirme|posso|consigo)"
    r"|nunca\s+(?:diga|afirme|confirme)"
    r"|(?:do\s+not|don't|never)\s+(?:say|claim|confirm)"
    r"|(?:cannot|can't)\s+(?:say|confirm)"
    r")\b",
    flags=re.IGNORECASE,
)


_NEGATED_CLAIM_LINK_RE = re.compile(
    r"^\s*(?:(?:confirmar|confirm|dizer|say|afirmar|claim)\s+)?"
    r"(?:(?:que|that|se|whether)\s*)?$",
    flags=re.IGNORECASE,
)


def _is_negated_or_prohibited(response_text: str, match: re.Match[str]) -> bool:
    """Return whether a nearby negation/prohibition applies to this claim.

    The text between the negation and a matched claim must be only the
    grammatical connector that completes that negation. This keeps polarity
    attached to one semantic assertion and cannot suppress a later
    independent assertion separated by punctuation, a comma, or a
    conjunction.
    """
    sentence_start = max(
        response_text.rfind(boundary, 0, match.start())
        for boundary in ("\n", ".", "!", "?", ";")
    ) + 1
    prefix = response_text[sentence_start:match.start()]
    negations = tuple(_NEGATED_CLAIM_PREFIX_RE.finditer(prefix))
    if not negations:
        return False

    # The most recent negation is the only one that can govern this match.
    negation_end = sentence_start + negations[-1].end()
    between = response_text[negation_end:match.start()]
    return _NEGATED_CLAIM_LINK_RE.fullmatch(between) is not None


def detect_scheduling_violation(
    response_text: Optional[str],
) -> Optional[tuple[str, int]]:
    """Return ``(category, match_offset)`` on violation, ``None`` otherwise.

    Pure detector, no side effects: no counter, no log, no env-var read.
    Callers wanting the observability side effects should use
    :func:`check_scheduling_gate`. Never raises on garbage input
    (``None`` / empty string / non-``str``).
    """
    try:
        if not response_text or not isinstance(response_text, str):
            return None
        for category, pattern in _CATEGORY_ORDER:
            for match in pattern.finditer(response_text):
                if not _is_negated_or_prohibited(response_text, match):
                    return category, match.start()
    except Exception:
        # The detector is a safety backstop and deliberately fail-open on an
        # unexpected parser/runtime error; its caller must never lose a turn.
        return None
    return None


def check_scheduling_gate(
    response_text: Optional[str],
    tenant_id: Optional[str] = None,
) -> Optional[str]:
    """Return the violated category, or ``None`` if the response is clean.

    Side effects on violation:
      * bump ``neurostack_scheduling_gate_violations_total{category,tenant_id}``
      * emit a structured ``scheduling_gate_violation`` warning log with
        ``tenant_id``, ``category``, and the match offset — deliberately
        **no response snippet**, because this conversation flow collects
        the patient's full name and day/time preference by design
        (see ``normal.py`` "agendar" intent), so any excerpt could leak
        PII on every violation and the existing ``mask_carteirinha``
        helper only masks digit runs, not names or free text.

    Never raises: observability failures must not break the pipeline.
    The metrics ``.inc()`` call is wrapped in ``try/except`` because
    Prometheus can raise from labels/inc on bad state; the detection
    itself is guarded by :func:`detect_scheduling_violation`'s
    ``isinstance`` check.

    Enforcement (rewriting the response) is not this function's job —
    it only reports. See :func:`resolve_gate_mode` and
    :func:`get_safe_replacement` for the enforcement wiring the pipeline
    calls after this.
    """
    detection = detect_scheduling_violation(response_text)
    if detection is None:
        return None
    category, offset = detection

    tenant_label = str(tenant_id) if tenant_id else "unknown"
    try:
        SCHEDULING_GATE_VIOLATIONS.labels(
            tenant_id=tenant_label,
            category=category,
        ).inc()
    except Exception as exc:  # noqa: BLE001 — metrics must never break pipeline
        try:
            logger.error(
                "Failed to record scheduling gate violation metric",
                error=str(exc),
                error_type=type(exc).__name__,
            )
        except Exception:
            # Logging the metric failure is itself best-effort. In particular,
            # a broken sink must not propagate out of this patient-facing path.
            pass
    try:
        logger.warning(
            "scheduling_gate_violation",
            tenant_id=tenant_label,
            category=category,
            match_offset=offset,
        )
    except Exception:
        # The gate protects the patient-facing path. A broken log sink must
        # not turn an observed violation into a failed conversation.
        pass
    return category


# ─────────────────────────────────────────────────────────────────────────────
# Category-specific safe replacement text.
#
# Constraints (from wish deliverable 2):
#   * None may promise team follow-up — the gate only fires on turns where
#     no handoff was triggered (event_service gates the LLM call itself on
#     ``elif not result.handoff_required:``), so a follow-up promise here
#     would itself be an unfulfillable promise, the same class of error
#     this whole wish exists to eliminate.
#   * None may mention "humano" / "atendente humano" / "transferir"
#     (normal.py:38's rule applies to gate output too).
#   * Category (a) only resumes name/day-time collection when the funnel
#     stage is ``agendamento_solicitado`` (not ``agendado``) — this keeps
#     the gate coherent with the ``agendado`` funnel-stage instructions
#     that explicitly say never restart collection for a patient who
#     already has an appointment.
#   * Categories (b) and (c) never restart full-name collection.
#   * When the funnel is ``agendado`` and the violation is (a), the reply
#     may neither deny nor reaffirm the existing appointment — it stays
#     purely a hold-and-forward stance.
# ─────────────────────────────────────────────────────────────────────────────

_AGENDAMENTO_SOLICITADO = "agendamento_solicitado"
_AGENDADO = "agendado"


def _confirmed_replacement(language: str, funnel_stage: Optional[str]) -> str:
    if language == "en":
        if funnel_stage == _AGENDAMENTO_SOLICITADO:
            return (
                "Thanks for the details. Appointment confirmation depends on "
                "the clinic team, so I can't confirm it here. Please share "
                "your full name and preferred day and period."
            )
        # agendado or anything else: no denial, no reaffirmation.
        return (
            "Thanks for your message. Appointment confirmation depends on "
            "the clinic team, so I can't confirm it here."
        )
    # pt-BR default
    if funnel_stage == _AGENDAMENTO_SOLICITADO:
        return (
            "Obrigado pelas informações. A confirmação do agendamento "
            "depende da equipe da clínica, então não consigo confirmar por "
            "aqui. Informe seu nome completo e a preferência de dia e período."
        )
    # agendado or anything else: no denial, no reaffirmation.
    return (
        "Obrigado pela sua mensagem. A confirmação do agendamento depende "
        "da equipe da clínica, então não consigo confirmar por aqui."
    )


def _availability_replacement(language: str, funnel_stage: Optional[str]) -> str:
    # Never cite any time; never resume name collection.
    if language == "en":
        return (
            "Thanks for asking about a time. I don't have access to the "
            "clinic calendar, so I can't say whether a specific slot is "
            "open. Please share the day and period you prefer."
        )
    return (
        "Obrigado por perguntar sobre um horário. Eu não tenho acesso à "
        "agenda da clínica, então não consigo dizer se um horário "
        "específico está livre. Informe o dia e o período que prefere."
    )


def _processed_change_replacement(language: str, funnel_stage: Optional[str]) -> str:
    # Acknowledge the request; never claim processed; never restart name collection.
    if language == "en":
        return (
            "Thanks for letting me know. I don't have access to the clinic "
            "calendar, so I can't cancel or reschedule anything here. Please "
            "share the new preferred day and period, or the reason for cancelling."
        )
    return (
        "Obrigado por avisar. Eu não tenho "
        "acesso à agenda da clínica, então não consigo processar "
        "cancelamento nem reagendamento por aqui. Informe a nova preferência "
        "de dia e período, ou o motivo do cancelamento."
    )


_REPLACEMENTS = {
    CATEGORY_CONFIRMED: _confirmed_replacement,
    CATEGORY_AVAILABILITY: _availability_replacement,
    CATEGORY_PROCESSED_CHANGE: _processed_change_replacement,
}


def get_safe_replacement(
    category: str,
    language: Optional[str] = None,
    funnel_stage: Optional[str] = None,
) -> str:
    """Return the safe replacement text for the given violation category.

    ``language`` accepts "en" (case-insensitive) or any pt-BR variant /
    ``None`` — anything not exactly "en" falls back to Portuguese, matching
    the rest of the code base's language handling.
    """
    builder = _REPLACEMENTS.get(category)
    if builder is None:
        # Unknown category — fall back to the confirmed-appointment reply,
        # which is the most conservative "team validates, I can't confirm"
        # posture. Should be unreachable via detect_scheduling_violation.
        builder = _confirmed_replacement
    lang = (language or "pt-BR").lower()
    lang_key = "en" if lang.startswith("en") else "pt"
    return builder(lang_key, funnel_stage)


# ─────────────────────────────────────────────────────────────────────────────
# Rollback lever — env var ``SCHEDULING_GATE_MODE``.
#
# Read via raw ``os.getenv`` per call — NOT through ``src.config.settings``
# because that ``Settings`` class is ``lru_cache``d and would freeze the
# value at first read, breaking both test monkeypatching (each test sets
# a different value) and the emergency-rollback purpose (operators must be
# able to flip to observe without redeploying).
#
# Only the exact string "observe" (case-sensitive) enables observe mode.
# Anything else, including unset, missing, or misspelled, is treated as
# enforce — the fail-safe default.
# ─────────────────────────────────────────────────────────────────────────────

GATE_MODE_ENFORCE = "enforce"
GATE_MODE_OBSERVE = "observe"


def resolve_gate_mode() -> str:
    """Return the current gate mode ("enforce" or "observe").

    Reads ``SCHEDULING_GATE_MODE`` from the process environment on every
    call so the emergency-rollback lever is not frozen by import-time
    caching. Anything other than the exact case-sensitive string
    ``"observe"`` (including unset) is treated as enforce.
    """
    raw = os.getenv("SCHEDULING_GATE_MODE", GATE_MODE_ENFORCE)
    return GATE_MODE_OBSERVE if raw == GATE_MODE_OBSERVE else GATE_MODE_ENFORCE
