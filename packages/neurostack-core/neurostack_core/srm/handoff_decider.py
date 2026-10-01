"""
NeuroStack - SRM - Handoff Decider
Determines when human handoff is required based on conversation state.
"""

import re
from collections.abc import Mapping
from typing import Optional, cast

from neurostack_core.presets import get_preset
from neurostack_core.protocol import (
    FunnelStage,
    HandoffReason,
    IntentCategory,
    SafetyCategory,
)
from neurostack_core.srm.models import HandoffResult, SafetyResult

# ============================================
# Handoff Trigger Patterns
# ============================================

_HANDOFF_PRESET = cast(Mapping[str, object], get_preset().srm["handoff"])
EXPLICIT_HANDOFF_PATTERNS = cast(tuple[str, ...], _HANDOFF_PRESET["explicit_patterns"])
_HANDOFF_REGEXES = cast(Mapping[str, str], _HANDOFF_PRESET["regexes"])
_REGEX_FLAGS = re.IGNORECASE | re.UNICODE

# Regex to detect a scheduling time/date preference in the current message.
# Duplicated from engine.py so handoff_decider has no circular import.
_SCHEDULING_HAS_TIME_RE = re.compile(
    _HANDOFF_REGEXES["scheduling_has_time"], _REGEX_FLAGS
)

# Confirmation response patterns for the scheduling two-step flow.
#
# Three tiers (see _is_positive_confirmation for full rationale):
#   STRONG   \u2014 match at any message length (sim, claro, confirmo, encaminhe\u2026)
#   WEAK     \u2014 whole-message anchor, \u2264 3 words (pode, ok, vai, manda\u2026)
#   SHORT    \u2014 word-boundary, \u2264 4 words (perfeito, \u00f3timo, beleza)
_STRONG_POSITIVE_RE = re.compile(_HANDOFF_REGEXES["strong_positive"], _REGEX_FLAGS)
# Weak positives only fire when the ENTIRE trimmed message is \u2264 3 words.
# These are ambiguous single-word replies (pode, ok, vai\u2026) that only count
# when nothing else is in the message.
# NOTE: "pode encaminhar" is added here (not to STRONG) to avoid bare "encaminhar"
# matching inside negations like "n\u00e3o encaminhar" or "prefiro n\u00e3o encaminhar agora".
# The whole-message anchor makes this safe.
_WEAK_POSITIVE_RE = re.compile(_HANDOFF_REGEXES["weak_positive"], _REGEX_FLAGS)
# Short-affirmative positives: perfeito / \u00f3timo / beleza fire via word-boundary
# search when the message is \u2264 4 words.  This catches "est\u00e1 \u00f3timo", "beleza
# ent\u00e3o", "perfeito pra mim" (genuine confirmations) while keeping long
# preference sentences like "segunda \u00e0s 9h fica perfeito pra mim" (10 words)
# safely blocked.
_SHORT_AFFIRMATIVE_RE = re.compile(_HANDOFF_REGEXES["short_affirmative"], _REGEX_FLAGS)
_NEGATIVE_RE = re.compile(_HANDOFF_REGEXES["negative"], _REGEX_FLAGS)
# Scheduling-link enrich turn: patient reports the booking outcome. This vocabulary
# ("consegui", "agendei", "deu certo"\u2026) is distinct from the yes/no confirmation
# words, so it needs its own positive-completion matcher. Negation is vetoed first,
# so "n\u00e3o consegui" / "n\u00e3o deu certo" correctly read as NOT concluded.
_SCHEDULING_COMPLETED_RE = re.compile(
    _HANDOFF_REGEXES["scheduling_completed"], _REGEX_FLAGS
)

_IMPERATIVE_LITERALS = cast(
    Mapping[str, Mapping[str, tuple[str, ...]]],
    get_preset().srm["imperative_literals"],
)
_HANDOFF_IMPERATIVES = _IMPERATIVE_LITERALS["srm.handoff_decider"]
_INIT_LITERALS = _HANDOFF_IMPERATIVES["HandoffDecider.__init__"]
_DECIDE_LITERALS = _HANDOFF_IMPERATIVES["HandoffDecider.decide"]
_SAFETY_LITERALS = _HANDOFF_IMPERATIVES["HandoffDecider._check_safety_handoff"]
_ESCALATION_LITERALS = _HANDOFF_IMPERATIVES["HandoffDecider._check_escalation_keywords"]
_OUTPUT_VALUES = cast(
    Mapping[str, str], _HANDOFF_IMPERATIVES["HandoffDecider.output_values"]
)


class HandoffDecider:
    """
    Decides when to trigger human handoff.

    Handoff reasons:
    - USER_REQUEST: User explicitly asked for a human
    - SAFETY_CONCERN: Safety issue detected requiring human intervention
    - MAX_TURNS_EXCEEDED: Too many turns without resolution
    - LOW_CONFIDENCE: Low confidence in AI responses
    - COMPLEX_QUERY: Query too complex for AI
    - ESCALATION: Complaint or escalation request
    """

    def __init__(
        self,
        max_turns: int = 10,
        min_confidence: float = 0.5,
        escalation_keywords: Optional[list[str]] = None,
    ):
        """
        Initialize the handoff decider.

        Args:
            max_turns: Maximum turns before suggesting handoff
            min_confidence: Minimum confidence before suggesting handoff
            escalation_keywords: Additional keywords that trigger escalation
        """
        self.max_turns = max_turns
        self.min_confidence = min_confidence
        self.escalation_keywords = escalation_keywords or list(
            cast(tuple[str, ...], _HANDOFF_PRESET["escalation_keywords"])
        )

        # Compile explicit handoff patterns
        self._patterns = [
            re.compile(p, re.IGNORECASE | re.UNICODE) for p in EXPLICIT_HANDOFF_PATTERNS
        ]

    def decide(
        self,
        message: str,
        intent: str,
        intent_category: IntentCategory,
        confidence: float,
        turn_count: int,
        safety_result: SafetyResult,
        funnel_stage: FunnelStage,
        flags: dict[str, bool],
        scheduling_link_enabled: bool = False,
    ) -> HandoffResult:
        """
        Decide if handoff is required. Define setor e SLA conforme doc (MVP v0.5).
        Defaults: urgencia=imediato; agendamento/gerencia/clinico=prioritario; financeiro/administrativo=rotina.
        """
        message_lower = message.strip().lower()

        # Priority 1: Explicit handoff request → administrativo, prioritario
        explicit_result = self._check_explicit_request(message_lower)
        if explicit_result:
            return explicit_result

        # Priority 2: Safety concerns (intent + safety_result → setor/SLA)
        if not safety_result.is_safe:
            safety_handoff = self._check_safety_handoff(safety_result, intent)
            if safety_handoff:
                return safety_handoff

        # Priority 3: Escalation/complaint → gerencia, prioritario
        if intent_category == IntentCategory.COMPLAINT:
            return HandoffResult(
                required=True,
                reason=HandoffReason.ESCALATION,
                urgency="high",
                summary="User has a complaint that requires human attention",
                sector=_DECIDE_LITERALS[0],
                sla=_OUTPUT_VALUES["priority"],
            )

        # Intent-based handoff (doc: pos_atendimento_sintomas, conduta_inadequada, pedido_alterar_medicacao)
        if intent == "pos_atendimento_sintomas_preocupantes":
            return HandoffResult(
                required=True,
                reason=HandoffReason.SAFETY_CONCERN,
                urgency="critical",
                summary=_DECIDE_LITERALS[1],
                sector=_DECIDE_LITERALS[2],
                sla=_OUTPUT_VALUES["immediate"],
            )
        if intent == "conduta_inadequada_ameaca":
            return HandoffResult(
                required=True,
                reason=HandoffReason.ESCALATION,
                urgency="high",
                summary=_DECIDE_LITERALS[3],
                sector=_DECIDE_LITERALS[4],
                sla=_OUTPUT_VALUES["priority"],
            )
        if intent == "pedido_alterar_medicacao":
            return HandoffResult(
                required=True,
                reason=HandoffReason.SAFETY_CONCERN,
                urgency="high",
                summary=_DECIDE_LITERALS[5],
                sector=_DECIDE_LITERALS[6],
                sla=_OUTPUT_VALUES["priority"],
            )

        # Priority 4: Scheduling — three-step flow with explicit confirmation
        # Step 1 (scheduling_interest not set): LLM collects name + day/time, no handoff
        # Step 2 (scheduling_interest + scheduling_has_time both set): ask for confirmation
        #   — gated on scheduling_has_time so a bare name reply ("me chamo Ana") doesn't
        #     prematurely fire the confirmation prompt before a time preference is given.
        # Step 3 (handoff_confirmation_pending set): user replies yes/no → act accordingly
        already_collecting = flags.get("scheduling_interest", False)
        has_time = flags.get("scheduling_has_time", False)
        confirmation_pending = flags.get("handoff_confirmation_pending", False)
        is_scheduling = (
            intent in ("agendar", "reagendar", "cancelar")
            or intent_category == IntentCategory.SCHEDULING
        )

        # Scheduling-link enrich turn: the link was delivered last turn (pending handoff
        # already created). This turn we only read back whether the patient reported
        # finishing the booking, then annotate the SAME handoff — no new handoff fires.
        # Negative-first, and default to "nao_concluiu" on ambiguity so a human follows up.
        if flags.get("scheduling_link_sent", False):
            is_negative = self._is_negative_confirmation(message_lower)
            is_positive = self._is_positive_confirmation(message_lower) or bool(
                _SCHEDULING_COMPLETED_RE.search(message_lower)
            )
            concluded = is_positive and not is_negative
            return HandoffResult(
                required=False,
                verification_status=(
                    _OUTPUT_VALUES["verification_completed"]
                    if concluded
                    else _OUTPUT_VALUES["verification_not_completed"]
                ),
            )

        # P1a fix: _extract_slots() runs AFTER decide(), so scheduling_has_time is
        # not yet persisted for the current turn. Check the current message directly
        # so both-pieces-in-one-turn and time-only follow-ups work on the same turn.
        if (already_collecting or is_scheduling) and not has_time:
            has_time = bool(_SCHEDULING_HAS_TIME_RE.search(message_lower))

        if confirmation_pending:
            # User is answering the confirmation question.
            # Evaluate NEGATIVE first — a negation word is a hard veto even if
            # the message also contains an affirmative token (e.g. "não encaminhar").
            is_negative = self._is_negative_confirmation(message_lower)
            is_positive = self._is_positive_confirmation(message_lower)
            if is_positive and not is_negative:
                # Scheduling-link path: deliver the link and create a PENDING handoff this
                # same turn (verification_status="link_sent"). Excluded for cancellations —
                # a booking link is meaningless when the patient wants to cancel.
                if scheduling_link_enabled and not flags.get(
                    "scheduling_cancel_pending", False
                ):
                    return HandoffResult(
                        required=True,
                        reason=HandoffReason.SCHEDULING,
                        urgency="normal",
                        summary=_DECIDE_LITERALS[7],
                        sector=_DECIDE_LITERALS[8],
                        sla=_OUTPUT_VALUES["priority"],
                        verification_status="link_sent",
                    )
                return HandoffResult(
                    required=True,
                    reason=HandoffReason.SCHEDULING,
                    urgency="normal",
                    summary=_DECIDE_LITERALS[9],
                    sector=_DECIDE_LITERALS[10],
                    sla=_OUTPUT_VALUES["priority"],
                )
            if is_negative:
                # User declined → clear flags, continue normally
                return HandoffResult(required=False, scheduling_cancelled=True)
            # Ambiguous answer — keep confirmation pending
            return HandoffResult(required=False, confirmation_pending=True)

        if (already_collecting and has_time) or (
            is_scheduling and intent in ("reagendar", "cancelar")
        ):
            # Both scheduling interest and a time preference are present → ask for confirmation
            return HandoffResult(required=False, confirmation_pending=True)

        # Priority 5: Escalation keywords → gerencia
        escalation_result = self._check_escalation_keywords(message_lower)
        if escalation_result:
            return escalation_result

        # Priority 5: Max turns exceeded → administrativo, rotina
        if turn_count >= self.max_turns:
            return HandoffResult(
                required=True,
                reason=HandoffReason.MAX_TURNS_EXCEEDED,
                urgency="normal",
                summary=f"Conversation exceeded {self.max_turns} turns without resolution",
                sector=_OUTPUT_VALUES["administrative"],
                sla=_OUTPUT_VALUES["routine"],
            )

        # Priority 6: Low confidence → administrativo, rotina
        if (
            confidence < self.min_confidence
            and flags.get("low_confidence_count", 0) >= 2
        ):
            return HandoffResult(
                required=True,
                reason=HandoffReason.LOW_CONFIDENCE,
                urgency="normal",
                summary="Multiple low-confidence responses suggest human intervention needed",
                sector=_OUTPUT_VALUES["administrative"],
                sla=_OUTPUT_VALUES["routine"],
            )

        # Priority 7: Already in handoff waiting state
        if flags.get("handoff_requested", False):
            return HandoffResult(
                required=True,
                reason=HandoffReason.USER_REQUEST,
                urgency="normal",
                summary="User previously requested handoff and is still waiting",
                sector=_OUTPUT_VALUES["administrative"],
                sla=_OUTPUT_VALUES["priority"],
            )

        return HandoffResult(required=False)

    def _is_positive_confirmation(self, message: str) -> bool:
        """Return True if user message is a positive confirmation.

        Three tiers — evaluated in order:

        1. Strong positives (sim, claro, confirmo, encaminhe…): match at any
           message length.
        2. Weak positives (pode, ok, vai, manda…): only match when the ENTIRE
           trimmed message is ≤ 3 words.  These are ambiguous single-word
           replies that carry no clear meaning in longer sentences.
        3. Short affirmatives (perfeito, ótimo, beleza): word-boundary search
           when the message is ≤ 4 words.  This recovers short confirmations
           like "está ótimo", "beleza então", "perfeito pra mim" that were
           blocked by the strict whole-message anchor in tier 2, while still
           rejecting long preference sentences like
           "segunda às 9h fica perfeito pra mim" (10 words → > 4 → no match).
        """
        if _STRONG_POSITIVE_RE.search(message):
            return True
        words = message.strip().split()
        if len(words) <= 3 and _WEAK_POSITIVE_RE.search(message.strip()):
            return True
        if len(words) <= 4 and _SHORT_AFFIRMATIVE_RE.search(message):
            return True
        return False

    def _is_negative_confirmation(self, message: str) -> bool:
        """Return True if user message is a negative response (não, no, agora não, etc.)."""
        return bool(_NEGATIVE_RE.search(message))

    def _check_explicit_request(self, message: str) -> Optional[HandoffResult]:
        """Check if user explicitly requested a human → administrativo, prioritario (doc)."""
        for pattern in self._patterns:
            if pattern.search(message):
                return HandoffResult(
                    required=True,
                    reason=HandoffReason.USER_REQUEST,
                    urgency="normal",
                    summary="User explicitly requested to speak with a human",
                    sector=_OUTPUT_VALUES["administrative"],
                    sla=_OUTPUT_VALUES["priority"],
                )
        return None

    def _check_safety_handoff(
        self, safety_result: SafetyResult, intent: str
    ) -> Optional[HandoffResult]:
        """Check if safety concern requires handoff; set sector/SLA per doc."""
        if safety_result.category == SafetyCategory.CLINICAL_RISK:
            return HandoffResult(
                required=True,
                reason=HandoffReason.SAFETY_CONCERN,
                urgency="critical",
                summary=f"Clinical safety concern detected: {', '.join(safety_result.triggers[:3])}",
                sector=_SAFETY_LITERALS[0],
                sla=_OUTPUT_VALUES["immediate"],
            )
        if safety_result.category == SafetyCategory.CONDUCT_RISK:
            if safety_result.confidence >= 0.7:
                return HandoffResult(
                    required=True,
                    reason=HandoffReason.SAFETY_CONCERN,
                    urgency="high",
                    summary=f"Conduct concern detected: {', '.join(safety_result.triggers[:3])}",
                    sector=_SAFETY_LITERALS[1],
                    sla=_OUTPUT_VALUES["priority"],
                )
        if safety_result.category == SafetyCategory.LEGAL_RISK:
            if safety_result.action == "escalate":
                return HandoffResult(
                    required=True,
                    reason=HandoffReason.SAFETY_CONCERN,
                    urgency="normal",
                    summary="Legal risk requires human verification",
                    sector=_OUTPUT_VALUES["administrative"],
                    sla=_OUTPUT_VALUES["routine"],
                )
        return None

    def _check_escalation_keywords(self, message: str) -> Optional[HandoffResult]:
        """Check for escalation keywords → gerencia, prioritario."""
        matched = [kw for kw in self.escalation_keywords if kw in message]
        if matched:
            return HandoffResult(
                required=True,
                reason=HandoffReason.ESCALATION,
                urgency="high" if len(matched) > 1 else "normal",
                summary=f"Escalation keywords detected: {', '.join(matched[:3])}",
                sector=_ESCALATION_LITERALS[0],
                sla=_OUTPUT_VALUES["priority"],
            )
        return None

    def should_suggest_handoff(
        self,
        turn_count: int,
        low_confidence_count: int,
    ) -> bool:
        """
        Check if we should suggest handoff option to user.

        This is a softer check - doesn't force handoff but suggests it.
        """
        return turn_count >= self.max_turns - 2 or low_confidence_count >= 2

    # NOTE: A ``generate_handoff_message`` method used to live here with a
    # per-reason PT message dict, but it had zero callers anywhere in
    # ``src/`` or ``tests/`` (the live path uses
    # ``handoff.models.HandoffServiceConfig.default_handoff_messages`` and
    # ``flow.handlers.handoff.HANDOFF_RESPONSES``). Kept as dead code, its
    # copy of the scheduling literal silently drifted from those two
    # sources; per Group 4 of the scheduling-agent-guardrails wish it was
    # removed so there is only one production PT scheduling literal to
    # keep coherent.


# ============================================
# Singleton Instance
# ============================================

_decider: Optional[HandoffDecider] = None


def get_handoff_decider(
    max_turns: int = 10,
    min_confidence: float = 0.5,
) -> HandoffDecider:
    """Get or create the global HandoffDecider instance."""
    global _decider
    if _decider is None:
        _decider = HandoffDecider(
            max_turns=max_turns,
            min_confidence=min_confidence,
        )
    return _decider
