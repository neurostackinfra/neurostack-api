"""
NeuroStack — Price-Gate Guard (convenio triage observability)

Standalone observability guard for the convenio/particular price gate.

Lives in its own module — separate from ``observability.metrics`` — on
purpose: ``observability.metrics`` is a legacy business-metrics module
that re-declares Prometheus counters already owned by ``srm.engine``
(e.g. ``neurostack_srm_decisions_total``). Importing it into the live
pipeline/route graph would collide with ``srm.engine`` and crash app
startup. This guard only needs its own counter, so it stays dependency-
light and safe to import from the pipeline.
"""

from __future__ import annotations

import re
from typing import Optional

from prometheus_client import Counter

from src.config.logging import get_logger


logger = get_logger(__name__)


# Increments whenever the agent's finalized response contains a monetary
# token (R$ / "reais") but the session's atendimento_tipo is anything
# other than "particular". Must stay at zero on the happy path — every
# increment points to a leak past the deterministic price gate injected
# by the pipeline (custom_vars["can_quote_particular"]).
PRICE_GATE_VIOLATIONS = Counter(
    "neurostack_price_gate_violations_total",
    "Times a monetary value appeared in the agent response while atendimento_tipo != particular",
    ["tenant_id", "atendimento_tipo"],  # atendimento_tipo: convenio | undefined
)


# Money-token detector. Keyed off unambiguous currency markers so plain
# integers and the common adjective "real" ("caso real") never trip it —
# only the currency plural "reais" and the R$ marker count as a price.
_MONEY_RE = re.compile(
    r"R\$\s*\d"                      # R$ 100 / R$100 / R$ 1.500,00
    r"|\bR\$"                        # R$ marker (agent quoting the currency)
    r"|\breais\b"                    # "cem reais", "50 reais" (currency plural)
    r"|\bRs\.?\s*\d",                # informal "Rs 200" / "Rs. 200"
    flags=re.IGNORECASE,
)


def check_price_gate(
    response_text: Optional[str],
    atendimento_tipo: Optional[str],
    tenant_id: Optional[str] = None,
) -> bool:
    """Return True when a price mis-gate violation is detected.

    A violation is: *response_text* contains a monetary token AND
    *atendimento_tipo* is not "particular" (i.e. is "convenio" or unset).
    On violation we bump the Prometheus counter and emit a structured
    warning log so the leak is visible in dashboards + logs. Never
    raises: observability failures must not break the pipeline.

    On the happy path (particular caller, or no money mentioned) this
    returns False without side effects, so the counter stays at zero.
    """
    if not response_text:
        return False
    if atendimento_tipo == "particular":
        return False
    if not _MONEY_RE.search(response_text):
        return False

    tipo_label = atendimento_tipo or "undefined"
    tenant_label = str(tenant_id) if tenant_id else "unknown"
    try:
        PRICE_GATE_VIOLATIONS.labels(
            tenant_id=tenant_label,
            atendimento_tipo=tipo_label,
        ).inc()
    except Exception as exc:  # noqa: BLE001 — metrics must never break pipeline
        logger.error(
            "Failed to record price gate violation metric",
            error=str(exc),
            error_type=type(exc).__name__,
        )
    logger.warning(
        "price_gate_violation",
        atendimento_tipo=tipo_label,
        tenant_id=tenant_label,
        response_snippet=response_text[:120],
    )
    return True
