"""Fail-closed domain gates for provider egress, and the flag questions.

A gate (``require_*``) raises :class:`EgressDisabledError` unless its flag is
effective.  A question (``agent_media_send_enabled``,
``uazapi_webhook_auth_enforced``, ``uazapi_egress_open``) answers ``False``
for any doubt and never raises (only cancellation propagates).
"""

from __future__ import annotations

import asyncio
from typing import Final, Literal, Protocol

from src.config.logging import get_logger
from src.config.settings import settings
from src.infrastructure.appconfig import AppConfigClient, AppConfigDataFlagReader

EgressFlag = Literal[
    "financial_egress_enabled",
    "uazapi_egress_enabled",
    "agent_media_send_enabled",
]
# The flags read as questions (``False`` for any doubt, never raising).
QuestionFlag = Literal[
    "agent_media_send_enabled",
    "uazapi_egress_enabled",
    "uazapi_webhook_auth_enforced",
]
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

    async def agent_media_send_enabled(self) -> bool:
        """Whether the agent may send media now; ``False`` for any doubt.

        A question, not a gate: the media path turns ``False`` into its own
        outcome (no candidates, ``flag_off``) instead of an error.  Only
        cancellation propagates.
        """

        return await self._question("agent_media_send_enabled")

    async def uazapi_webhook_auth_enforced(self) -> bool:
        """Whether the Uazapi webhook refuses an unverified callback now.

        ``False`` for any doubt is the shadow mode (measure only): here
        "fail-closed" means *no* enforcement, never a refused delivery.
        """

        return await self._question("uazapi_webhook_auth_enforced")

    async def uazapi_egress_open(self) -> bool:
        """``uazapi_egress_enabled`` as a question; ``False`` for any doubt."""

        return await self._question("uazapi_egress_enabled")

    async def _question(self, flag_name: QuestionFlag) -> bool:
        try:
            enabled = await self._flag_reader.is_enabled(flag_name)
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return enabled is True

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


async def agent_media_send_enabled() -> bool:
    """Read ``agent_media_send_enabled`` once; never raises (fail-closed).

    ``True`` only when the selected reader answers the flag unambiguously
    ``{"enabled": true}``: absent, false, not a boolean, AppConfig disabled
    or unconfigured, the Agent unreachable, a stale or failed SDK poll, or a
    reader that cannot even be built all read ``False``.  Read by the
    Central de Mídias promise (``delivery_enabled``) and, in later groups,
    once at selection and again at delivery.  Only cancellation propagates.
    """

    try:
        return await _current_policy().agent_media_send_enabled()
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


async def uazapi_webhook_auth_enforced() -> bool:
    """Read ``uazapi_webhook_auth_enforced`` once; never raises (shadow).

    ``True`` only when the selected reader answers the flag unambiguously
    ``{"enabled": true}``: absent, false, not a boolean, AppConfig disabled
    or unconfigured, the Agent unreachable, a stale or failed SDK poll, or a
    reader that cannot even be built all read ``False`` -- the Uazapi webhook
    then only measures its check (shadow) and serves as before.  Read once
    per Uazapi callback, before its first database statement.  Only
    cancellation propagates.

    With AppConfig disabled no reader is consulted at all: both readers
    answer ``False`` there anyway, and a process without AppConfig (local
    development, the suites) keeps exactly the flag reads it had before this
    check existed.
    """

    if settings.appconfig_enabled is not True:
        return False
    try:
        return await _current_policy().uazapi_webhook_auth_enforced()
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


async def uazapi_egress_open() -> bool:
    """Read ``uazapi_egress_enabled`` once as a question; never raises.

    For a readiness answer (the Central de Mídias promise), not a gate: an
    outbound Uazapi call still goes through :func:`require_uazapi_egress`.
    Only cancellation propagates.
    """

    try:
        return await _current_policy().uazapi_egress_open()
    except asyncio.CancelledError:
        raise
    except Exception:
        return False
