"""Fail-closed domain gates for provider egress."""

from __future__ import annotations

import asyncio
from typing import Final, Literal, Protocol

from src.config.settings import settings
from src.infrastructure.appconfig import AppConfigClient

EgressFlag = Literal["financial_egress_enabled", "uazapi_egress_enabled"]
EgressDisabledCode = Literal[
    "financial_egress_disabled",
    "uazapi_egress_disabled",
]

FINANCIAL_EGRESS_DISABLED: Final = "financial_egress_disabled"
UAZAPI_EGRESS_DISABLED: Final = "uazapi_egress_disabled"


class FlagReader(Protocol):
    """Minimal AppConfig client contract used by the domain policy."""

    async def is_enabled(self, flag_name: str) -> bool: ...


class EgressDisabledError(RuntimeError):
    """A provider effect was rejected before its network boundary."""

    def __init__(self, code: EgressDisabledCode) -> None:
        self.code = code
        super().__init__(code)


class EgressPolicy:
    """Read the Agent once for each attempted outbound effect."""

    def __init__(self, flag_reader: FlagReader) -> None:
        self._flag_reader = flag_reader

    async def require_financial_egress(self) -> None:
        await self._require_enabled(
            "financial_egress_enabled",
            FINANCIAL_EGRESS_DISABLED,
        )

    async def require_uazapi_egress(self) -> None:
        await self._require_enabled(
            "uazapi_egress_enabled",
            UAZAPI_EGRESS_DISABLED,
        )

    async def _require_enabled(
        self,
        flag_name: EgressFlag,
        disabled_code: EgressDisabledCode,
    ) -> None:
        try:
            enabled = await self._flag_reader.is_enabled(flag_name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise EgressDisabledError(disabled_code) from exc
        if enabled is not True:
            raise EgressDisabledError(disabled_code)


def _current_policy() -> EgressPolicy:
    return EgressPolicy(AppConfigClient(settings))


async def require_financial_egress() -> None:
    """Require the financial creation gate for one outbound operation."""

    await _current_policy().require_financial_egress()


async def require_uazapi_egress() -> None:
    """Require the Uazapi gate for one outbound operation."""

    await _current_policy().require_uazapi_egress()
