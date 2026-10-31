"""Fail-closed domain gates for provider egress."""

from __future__ import annotations

import asyncio
from typing import Final, Literal, Protocol

from src.config.logging import get_logger
from src.config.settings import settings
from src.infrastructure.appconfig import AppConfigClient, AppConfigDataFlagReader

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


# One per process (F2): every uvicorn worker owns its reader and refresher.
APPCONFIG_DATA_READER: Final = AppConfigDataFlagReader(settings)
logger = get_logger(__name__)


def _flag_reader() -> FlagReader:
    if settings.appconfig_source == "sdk":
        return APPCONFIG_DATA_READER
    return AppConfigClient(settings)


def _current_policy() -> EgressPolicy:
    return EgressPolicy(_flag_reader())


def start_egress_flag_refresher() -> asyncio.Task[None] | None:
    """Start this process's AppConfig Data refresher (lifespan, F2).

    Only for ``APPCONFIG_SOURCE=sdk`` with AppConfig enabled; otherwise
    nothing is read and the egress stays as the selected source decides
    (closed while unconfigured).
    """

    if not (settings.appconfig_enabled and settings.appconfig_source == "sdk"):
        return None
    return asyncio.create_task(
        APPCONFIG_DATA_READER.run_refresher(),
        name="appconfig-egress-flags",
    )


async def stop_egress_flag_refresher(task: asyncio.Task[None] | None) -> None:
    """Cancel the refresher and close the process flags; never raises."""

    try:
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("appconfig_egress_refresher_failed")
    finally:
        APPCONFIG_DATA_READER.reset()


async def require_financial_egress() -> None:
    """Require the financial creation gate for one outbound operation."""

    await _current_policy().require_financial_egress()


async def require_uazapi_egress() -> None:
    """Require the Uazapi gate for one outbound operation."""

    await _current_policy().require_uazapi_egress()
