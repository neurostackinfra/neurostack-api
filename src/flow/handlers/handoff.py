"""
NeuroStack - Flow Manager - Handoff Mode Handler
Handles human handoff flow.
"""

import logging
import time
from typing import Optional

from src.flow.models import (
    FlowMode,
    FlowContext,
    ModeHandlerResult,
    TransitionTrigger,
)
from src.flow.handlers.base import BaseModeHandler

logger = logging.getLogger(__name__)


# Handoff response templates
# REGRA: Nunca mencionar "humano", "atendente humano" ou "transferir".
# Sempre usar "nossa equipe", "equipe especializada" ou "setor especializado".
HANDOFF_RESPONSES = {
    "explicit_request": (
        "Claro! Vou encaminhar voc\u00ea para nossa equipe agora. "
        "Em breve algu\u00e9m dar\u00e1 continuidade ao seu atendimento."
    ),
    "safety_concern": (
        "Para garantir o melhor cuidado para voc\u00ea, vou encaminhar para nossa equipe especializada. "
        "Um profissional entrar\u00e1 em contato em breve."
    ),
    "max_turns_exceeded": (
        "Para te ajudar ainda melhor, vou encaminhar para o setor da nossa equipe "
        "que poder\u00e1 dar continuidade ao seu atendimento."
    ),
    "low_confidence": (
        "Para garantir que voc\u00ea receba a orienta\u00e7\u00e3o mais completa, "
        "vou encaminhar para nossa equipe especializada."
    ),
    "complex_query": (
        "Essa quest\u00e3o merece uma aten\u00e7\u00e3o mais detalhada. "
        "Vou encaminhar para a nossa equipe especializada."
    ),
    "escalation": (
        "Entendo a sua situa\u00e7\u00e3o. Vou encaminhar para o setor respons\u00e1vel "
        "da nossa equipe, que poder\u00e1 ajud\u00e1-lo da melhor forma."
    ),
    "scheduling": (
        "Vou te encaminhar para a nossa equipe, que dar\u00e1 seguimento "
        "e verificar\u00e1 os melhores hor\u00e1rios dispon\u00edveis para voc\u00ea."
    ),
    "default": (
        "Vou encaminhar voc\u00ea para nossa equipe, que dar\u00e1 continuidade ao seu atendimento. "
        "Por favor, aguarde um momento."
    ),
    "waiting": (
        "Voc\u00ea est\u00e1 na fila de atendimento. Nossa equipe estar\u00e1 "
        "dispon\u00edvel em breve. Tempo estimado: {wait_time} minutos."
    ),
    "connected": (
        "Voc\u00ea est\u00e1 conectado com {agent_name}. Como posso ajud\u00e1-lo?"
    ),
}


class HandoffModeHandler(BaseModeHandler):
    """
    Handler for HANDOFF mode.
    
    Manages the transition from AI to human agent, including:
    - Queue management
    - Agent notification
    - Context transfer
    - Session state preservation
    """

    def __init__(self):
        super().__init__(FlowMode.HANDOFF)
        self.default_wait_time = 5  # minutes

    async def handle(self, context: FlowContext) -> ModeHandlerResult:
        """
        Handle a message in HANDOFF mode.
        
        In handoff mode, we:
        1. Acknowledge the handoff request
        2. Notify the queue system (placeholder)
        3. Provide status updates
        """
        start_time = time.perf_counter()
        
        # Get handoff reason
        handoff_reason = context.handoff_reason or "default"
        
        # Get current handoff status from metadata
        handoff_status = context.metadata.get("handoff_status", "pending")
        agent_id = context.metadata.get("handoff_agent_id")

        # Determine response based on status
        if handoff_status == "connected" and agent_id:
            response = HANDOFF_RESPONSES["connected"].format(
                agent_name=agent_id
            )
        elif handoff_status == "waiting":
            wait_time = context.metadata.get("wait_time", self.default_wait_time)
            response = HANDOFF_RESPONSES["waiting"].format(
                wait_time=wait_time
            )
        else:
            response = HANDOFF_RESPONSES.get(
                handoff_reason,
                HANDOFF_RESPONSES["default"]
            )

        # Log handoff event
        events = [{
            "event_type": "flow.handoff.process",
            "data": {
                "reason": handoff_reason,
                "status": handoff_status,
                "agent_id": agent_id,
                "turn_count": context.turn_count,
            },
        }]

        self.logger.info(
            f"HANDOFF mode: reason={handoff_reason}, status={handoff_status}, "
            f"session={context.session_id}"
        )

        # Check for transitions
        should_exit, trigger = self.should_exit(context)
        
        # Safety can override handoff
        if context.safety_concern:
            events.append({
                "event_type": "flow.handoff.safety_override",
                "data": {"category": context.safety_category},
            })
            return self.create_result(
                success=True,
                response=response,
                should_transition=True,
                next_trigger=TransitionTrigger.SAFETY_CONCERN_DETECTED,
                state_updates={
                    "handoff_status": handoff_status,
                    "safety_override": True,
                },
                events=events,
                start_time=start_time,
            )

        return self.create_result(
            success=True,
            response=response,
            should_transition=should_exit,
            next_trigger=trigger,
            state_updates={
                "handoff_status": handoff_status,
                "handoff_reason": handoff_reason,
                "last_mode": "handoff",
            },
            events=events,
            start_time=start_time,
        )

    def should_exit(self, context: FlowContext) -> tuple[bool, Optional[TransitionTrigger]]:
        """
        Check if we should exit HANDOFF mode.
        
        Exit conditions:
        - Safety concern -> SAFETY (priority override)
        - Handoff completed -> NORMAL
        - Handoff cancelled -> NORMAL
        """
        # Safety has highest priority
        if context.safety_concern:
            return True, TransitionTrigger.SAFETY_CONCERN_DETECTED
        
        # Check handoff status
        handoff_status = context.metadata.get("handoff_status", "pending")
        
        if handoff_status == "completed":
            return True, TransitionTrigger.HANDOFF_COMPLETED
        
        if handoff_status == "cancelled":
            return True, TransitionTrigger.HANDOFF_CANCELLED
        
        # Check if handoff is no longer required
        if not context.handoff_required and handoff_status == "pending":
            return True, TransitionTrigger.HANDOFF_CANCELLED
        
        return False, None

    async def initiate_handoff(
        self,
        context: FlowContext,
        priority: str = "normal",
    ) -> dict:
        """
        Initiate a handoff to human agent.
        
        This is a placeholder for the actual handoff system integration.
        
        Args:
            context: Flow context
            priority: Handoff priority (low, normal, high, urgent)
            
        Returns:
            Handoff initiation result
        """
        # Placeholder - would integrate with actual queue system
        return {
            "status": "queued",
            "queue_position": 1,
            "estimated_wait_time": self.default_wait_time,
            "priority": priority,
            "session_id": str(context.session_id),
        }

    def get_mode_prompt_additions(self) -> list[str]:
        """Get additional prompt instructions for HANDOFF mode."""
        return [
            "O usu\u00e1rio ser\u00e1 encaminhado para nossa equipe especializada.",
            "Mantenha o usu\u00e1rio informado sobre o status.",
            "Colete informa\u00e7\u00f5es relevantes para facilitar o atendimento.",
            "Seja emp\u00e1tico com a espera.",
        ]

    def get_response_modifiers(self) -> list[str]:
        """Get response modifiers for HANDOFF mode."""
        return ["transitional", "supportive", "informative"]

