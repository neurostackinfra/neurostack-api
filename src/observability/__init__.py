"""
NeuroStack API - Observability Module

Métricas de negócio, telemetria e instrumentação para
monitoramento completo do sistema.
"""

from src.observability.pii import (
    MASK_TOKEN,
    mask_carteirinha,
    mask_carteirinha_in_mapping,
)

# NOTE: the legacy ``src.observability.metrics`` module is intentionally NOT
# imported here. It re-declares Prometheus counters already owned by
# ``src.srm.engine`` (e.g. ``neurostack_srm_decisions_total``); eager-loading
# it into the live pipeline/route import graph would raise a duplicated-
# timeseries error at app startup. Import that module directly if you need it.
from src.observability.price_gate import PRICE_GATE_VIOLATIONS, check_price_gate

__all__ = [
    "PRICE_GATE_VIOLATIONS",
    "check_price_gate",
    "MASK_TOKEN",
    "mask_carteirinha",
    "mask_carteirinha_in_mapping",
]
