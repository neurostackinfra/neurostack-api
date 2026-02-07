"""
NeuroStack - SRM (Small Reasoning Module)
Deterministic decision engine for cognitive processing.

The SRM is the core cognitive component that:
- Classifies user intent using rule-based patterns
- Detects funnel stage transitions
- Evaluates safety concerns (clinical, legal, conduct)
- Decides on handoff requirements
- Determines RAG necessity
- Generates state patches for session updates

Usage:
    from src.srm import SRMEngine, get_srm_engine, process_message

    # Using the engine directly
    engine = get_srm_engine()
    decision = engine.process(input_data)

    # Using the convenience function
    decision = await process_message(
        tenant_id=tenant_id,
        session_id=session_id,
        message="Ol\u00e1, gostaria de agendar uma consulta",
        channel="whatsapp",
    )
"""

from src.srm.models import (
    # Enums
    IntentCategory,
    FunnelStage,
    SafetyCategory,
    SentimentLabel,
    FlowMode,
    HandoffReason,
    ActionType,
    # Input/Output models
    SRMInput,
    SRMDecision,
    StatePatch,
    IntentResult,
    SafetyResult,
    FunnelResult,
    HandoffResult,
    RagResult,
    SentimentResult,
)
from src.srm.sentiment_classifier import (
    SentimentClassifier,
    get_sentiment_classifier,
)
from src.srm.intent_classifier import (
    IntentClassifier,
    get_intent_classifier,
)
from src.srm.funnel_detector import (
    FunnelDetector,
    get_funnel_detector,
)
from src.srm.safety_detector import (
    SafetyDetector,
    get_safety_detector,
)
from src.srm.handoff_decider import (
    HandoffDecider,
    get_handoff_decider,
)
from src.srm.rag_decider import (
    RagDecider,
    get_rag_decider,
)
from src.srm.engine import (
    SRMEngine,
    get_srm_engine,
    process_message,
)

__all__ = [
    # Enums
    "IntentCategory",
    "FunnelStage",
    "SafetyCategory",
    "SentimentLabel",
    "FlowMode",
    "HandoffReason",
    "ActionType",
    # Models
    "SRMInput",
    "SRMDecision",
    "StatePatch",
    "IntentResult",
    "SafetyResult",
    "FunnelResult",
    "HandoffResult",
    "RagResult",
    "SentimentResult",
    # Components
    "IntentClassifier",
    "get_intent_classifier",
    "FunnelDetector",
    "get_funnel_detector",
    "SafetyDetector",
    "get_safety_detector",
    "HandoffDecider",
    "get_handoff_decider",
    "RagDecider",
    "get_rag_decider",
    "SentimentClassifier",
    "get_sentiment_classifier",
    # Engine
    "SRMEngine",
    "get_srm_engine",
    "process_message",
]
