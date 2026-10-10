"""Cancellation and fail-closed contracts for provider egress policy."""

from __future__ import annotations

import asyncio

import pytest

from src.services.egress_policy import EgressDisabledError, EgressPolicy


class _FailingFlagReader:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def is_enabled(self, flag_name: str) -> bool:
        raise self._error


@pytest.mark.parametrize(
    "method_name",
    [
        "require_financial_egress",
        "require_uazapi_egress",
    ],
)
@pytest.mark.asyncio
async def test_policy_propagates_cancellation(method_name: str) -> None:
    policy = EgressPolicy(_FailingFlagReader(asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await getattr(policy, method_name)()


@pytest.mark.parametrize(
    ("method_name", "disabled_code"),
    [
        ("require_financial_egress", "financial_egress_disabled"),
        ("require_uazapi_egress", "uazapi_egress_disabled"),
    ],
)
@pytest.mark.asyncio
async def test_policy_maps_operational_errors_to_stable_disabled_code(
    method_name: str,
    disabled_code: str,
) -> None:
    policy = EgressPolicy(_FailingFlagReader(RuntimeError("agent unavailable")))

    with pytest.raises(EgressDisabledError) as exc_info:
        await getattr(policy, method_name)()

    assert exc_info.value.code == disabled_code
