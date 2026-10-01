"""Detect and manage canonical funnel-stage transitions."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Optional, TypeAlias, cast

from neurostack_core.presets import get_preset
from neurostack_core.protocol import FunnelStage, IntentCategory
from neurostack_core.srm.models import FunnelResult

FunnelStageInfo: TypeAlias = Mapping[
    str, str | tuple[FunnelStage, ...] | tuple[str, ...]
]


def _load_funnel_stages() -> Mapping[FunnelStage, FunnelStageInfo]:
    funnel = cast(Mapping[str, object], get_preset().srm["funnel"])
    metadata = cast(Mapping[str, Mapping[str, object]], funnel["metadata"])
    result: dict[FunnelStage, FunnelStageInfo] = {}
    for stage_id, definition in metadata.items():
        result[FunnelStage(stage_id)] = MappingProxyType(
            {
                "description": cast(str, definition["description"]),
                "next_stages": tuple(
                    FunnelStage(item)
                    for item in cast(tuple[str, ...], definition["next_stages"])
                ),
                "entry_conditions": cast(
                    tuple[str, ...], definition["entry_conditions"]
                ),
            }
        )
    return MappingProxyType(result)


def _load_transition_triggers() -> Mapping[tuple[FunnelStage, str], FunnelStage]:
    funnel = cast(Mapping[str, object], get_preset().srm["funnel"])
    transitions = cast(tuple[Mapping[str, object], ...], funnel["transitions"])
    return MappingProxyType(
        {
            (
                FunnelStage(cast(str, item["from"])),
                cast(str, item["intent"]),
            ): FunnelStage(cast(str, item["to"]))
            for item in transitions
        }
    )


def _load_turn_thresholds() -> Mapping[FunnelStage, int]:
    funnel = cast(Mapping[str, object], get_preset().srm["funnel"])
    thresholds = cast(Mapping[str, int], funnel["turn_thresholds"])
    return MappingProxyType(
        {FunnelStage(stage): threshold for stage, threshold in thresholds.items()}
    )


FUNNEL_STAGES: Mapping[FunnelStage, FunnelStageInfo] = _load_funnel_stages()
TRANSITION_TRIGGERS: Mapping[tuple[FunnelStage, str], FunnelStage] = (
    _load_transition_triggers()
)
TURN_THRESHOLDS: Mapping[FunnelStage, int] = _load_turn_thresholds()


class FunnelDetector:
    """Detect transitions for the canonical funnel."""

    def __init__(
        self,
        custom_transitions: Optional[dict[tuple[FunnelStage, str], FunnelStage]] = None,
    ):
        self.transitions = dict(TRANSITION_TRIGGERS)
        if custom_transitions:
            self.transitions.update(custom_transitions)

    def detect(
        self,
        current_stage: FunnelStage,
        intent: str,
        intent_category: IntentCategory,
        turn_count: int,
        slots: dict[str, object],
        flags: dict[str, bool],
    ) -> FunnelResult:
        """Detect a funnel transition from intent and conversation state."""
        if current_stage == FunnelStage.STANDBY:
            return FunnelResult(
                current_stage=current_stage,
                should_transition=False,
                next_stage=None,
                transition_reason=None,
            )

        transition_key = (current_stage, intent)
        if transition_key in self.transitions:
            next_stage = self.transitions[transition_key]
            if next_stage != current_stage:
                return FunnelResult(
                    current_stage=current_stage,
                    should_transition=True,
                    next_stage=next_stage,
                    transition_reason=(
                        f"Intent '{intent}' triggers transition to {next_stage.value}"
                    ),
                )

        if (
            current_stage == FunnelStage.LEAD_NOVO
            and slots.get("already_patient") is True
        ):
            return FunnelResult(
                current_stage=current_stage,
                should_transition=True,
                next_stage=FunnelStage.PACIENTE_ATIVO,
                transition_reason="User declared already patient",
            )

        if (
            current_stage == FunnelStage.EM_QUALIFICACAO
            and slots.get("lead_name")
            and slots.get("service_interest")
            and intent in ("agendar", "disponibilidade_horarios", "pedido_preco")
        ):
            return FunnelResult(
                current_stage=current_stage,
                should_transition=True,
                next_stage=FunnelStage.QUALIFICADO,
                transition_reason=(
                    "Qualification data collected, advancing to qualificado"
                ),
            )

        if (
            current_stage == FunnelStage.AGENDAMENTO_SOLICITADO
            and intent == "confirmar_presenca"
            and flags.get("scheduling_completed")
        ):
            return FunnelResult(
                current_stage=current_stage,
                should_transition=True,
                next_stage=FunnelStage.AGENDADO,
                transition_reason="User confirmed scheduling completed",
            )

        category_result = self._check_category_fallback(
            current_stage, intent, intent_category, turn_count, flags
        )
        if category_result:
            return category_result

        return FunnelResult(
            current_stage=current_stage,
            should_transition=False,
            next_stage=None,
            transition_reason=None,
        )

    def _check_category_fallback(
        self,
        current_stage: FunnelStage,
        intent: str,
        intent_category: IntentCategory,
        turn_count: int,
        flags: dict[str, bool],
    ) -> Optional[FunnelResult]:
        """Apply backward-compatible category transitions."""
        if current_stage == FunnelStage.LEAD_NOVO and intent_category in (
            IntentCategory.SCHEDULING,
            IntentCategory.QUESTION,
        ):
            if (
                "agendar" in intent
                or "scheduling" in intent
                or "question_price" in intent
            ):
                return FunnelResult(
                    current_stage=current_stage,
                    should_transition=True,
                    next_stage=FunnelStage.EM_QUALIFICACAO,
                    transition_reason="Interest/scheduling intent in lead_novo",
                )
        if (
            current_stage == FunnelStage.QUALIFICADO
            and intent_category == IntentCategory.SCHEDULING
        ):
            return FunnelResult(
                current_stage=current_stage,
                should_transition=True,
                next_stage=FunnelStage.AGENDAMENTO_SOLICITADO,
                transition_reason="Scheduling intent in qualificado",
            )

        threshold = TURN_THRESHOLDS.get(current_stage, 5)
        if (
            current_stage == FunnelStage.LEAD_NOVO
            and turn_count >= threshold
            and intent_category == IntentCategory.QUESTION
        ):
            return FunnelResult(
                current_stage=current_stage,
                should_transition=True,
                next_stage=FunnelStage.EM_QUALIFICACAO,
                transition_reason=f"Extended engagement ({turn_count} turns)",
            )
        return None

    def get_stage_info(self, stage: FunnelStage) -> FunnelStageInfo:
        """Get metadata for a funnel stage."""
        return FUNNEL_STAGES.get(stage, MappingProxyType({}))

    def get_valid_transitions(self, stage: FunnelStage) -> list[FunnelStage]:
        """Get valid next stages from a funnel stage."""
        info = FUNNEL_STAGES.get(stage)
        if info is None:
            return []
        return list(cast(tuple[FunnelStage, ...], info.get("next_stages", ())))


_detector: Optional[FunnelDetector] = None


def get_funnel_detector() -> FunnelDetector:
    """Get or create the global funnel detector."""
    global _detector
    if _detector is None:
        _detector = FunnelDetector()
    return _detector
