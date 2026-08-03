"""
NeuroStack — PII Masking Utilities

Applied at persistence/observability boundaries (trace views, log lines,
CSV exports, RLHF training exports) so that sensitive identifiers such as
health-insurance card numbers (números de carteirinha) never leak into
observable/derived artifacts.

Design goals
------------
- **Conservative:** only masks digit sequences whose surrounding context
  suggests a carteirinha/convênio identifier. Plain times ("14:30"),
  slash-separated dates ("15/03/2026") and other unrelated numbers are
  left intact.
- **Reversible only through the raw event store:** the primary event
  store keeps the raw content because downstream flows (handoff to human
  attendant, LLM conversation history) still need it. Masking happens
  only at the outward-facing boundaries handled by trace_adapter,
  trace_service, rlhf serialization, etc.
"""

from __future__ import annotations

import re
from typing import Any


# ============================================
# Constants
# ============================================

MASK_TOKEN = "***MASKED***"

# Digit runs with 6+ digits, allowing internal hyphen/space separators.
# Dot, ':' and '/' are excluded on purpose so BR-formatted prices
# ("1.250.000,00"), times ("14:30") and slash-dates ("15/03/2026") are
# left readable — a carteirinha reply is a plain/space/dash digit run.
#
# Masking is context-free: a bare number reply (e.g. "998877665544",
# with no "carteirinha"/"convênio" word nearby) is the most common way a
# patient sends the card number, so it must be masked regardless of the
# surrounding words. Over-masking an occasional phone/id number in a
# trace is harmless; leaking a card number is an LGPD problem.
_DIGIT_SEQ_RE = re.compile(r"(?<!\d)\d(?:[\s\-]?\d){5,}(?!\d)")


# ============================================
# Public API
# ============================================


def mask_carteirinha(text: str | None) -> str | None:
    """Mask carteirinha-like values in *text*.

    Any run of 6+ digits (optionally grouped by spaces/hyphens) is
    replaced with the mask token. Non-string / falsy inputs are returned
    unchanged.
    """
    if not text or not isinstance(text, str):
        return text
    return _DIGIT_SEQ_RE.sub(MASK_TOKEN, text)


def mask_carteirinha_in_mapping(data: Any, keys: tuple[str, ...] = ("content", "user_message", "agent_response", "suggested_response", "trigger_message")) -> Any:
    """Return a shallow copy of *data* with *keys* passed through mask_carteirinha.

    - dict: copies only the recognised keys; leaves other fields untouched.
    - list: applies recursively to each element.
    - anything else: returned as-is.
    """
    if isinstance(data, dict):
        new = dict(data)
        for k in keys:
            if k in new and isinstance(new[k], str):
                new[k] = mask_carteirinha(new[k])
        return new
    if isinstance(data, list):
        return [mask_carteirinha_in_mapping(item, keys) for item in data]
    return data
