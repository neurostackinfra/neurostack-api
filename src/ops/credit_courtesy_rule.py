"""Pre-deploy list of the courtesy rule (debito-cortesia-automatico, Group 6).

One subcommand, and no other way in (no HTTP route, worker or script imports
this module)::

    python -m src.ops.credit_courtesy_rule status

It runs as a one-off ECS task of the NEW task definition (after the infra
apply, before ``update-service``: wish Decision 13), so the owner sees who the
deploy makes charged, blocked or protected before the new image serves
(DESIGN item 14 and D21, Risks 3, 4 and 5).  It writes nothing: everything is
read in ONE ``REPEATABLE READ READ ONLY`` transaction that is always rolled
back (wish Decision 10).

``status`` prints one entry per builder that ever received a ``courtesy``
lot (``credit_lots.lot_kind = 'courtesy'``), in builder id order:

* ``state`` at the database clock (the deploy instant): ``charged`` or
  ``blocked`` (the courtesy rule), ``protected`` (courtesy spent, a paid plan
  in force: observational), or ``global``/``mark`` when one of those wins
  (precedence global > mark > courtesy, DESIGN Decision 13), so the owner
  sees why a builder is not under the rule.  The state is
  ``CreditRepository.effective_enforcement`` -- the request path's own reader
  -- judged at the database clock with this command's own gate verdicts; it
  is formatted here, never re-derived.  The marks are read inside that
  repository method only (this module never names their table);
* ``courtesy``: the rule's reading (``CreditRepository.courtesy_rule``: the
  courtesy lots ever received, the first grant, the spendable balance) and
  the nearest and the last expiry of a courtesy lot that can still spend
  (the last: when a charged builder is blocked by expiry if nothing else
  changes);
* ``paid_plan``: ``CreditRepository.paid_plan_in_force`` (wish Decision 5),
  read here for every builder -- the rule itself reads it only once the
  courtesy is spent (wish Decision 8) -- so the owner sees what would
  protect a charged builder once its courtesy runs out -- with the latest
  future expiry of a paid lot (``paid_lot_expires_at``);
* ``open_debt``: the open ``credit_debts`` swept at the database clock in
  memory (the figure ``/v1/credits/debt`` reads: the stored rows assembled
  into a ``LedgerBook``, never persisted);
* ``reservation``: the estimated storage charge of the FIRST accounting
  window that opens after the deploy (see :func:`reservation_document`);
* ``clinics``: the builder's tenants per channel (Uazapi, Evolution, Z-API,
  Meta; see :data:`TENANT_CHANNELS_SQL`), and per tenant
  ``credentials_incomplete``: the channels the tenant is on whose SEND
  credentials are incomplete (owner decision 20).  Under the rule such a
  tenant is charged on every turn while nothing is delivered: the reply
  fails at send (``CreditReplySender.prepare`` raises
  ``no_credentials``) and the decision stays charged, until the builder is
  blocked.  "Complete" is exactly what ``CreditReplySender.prepare``
  (``src/api/routes/webhook.py``) requires of the tenant's raw
  ``clinic_configs.api_credentials`` before it sends
  (:data:`SEND_CREDENTIALS`):

  - ``uazapi``: ``uazapi_api_url`` and ``uazapi_token``, each non-empty
    after ``str.strip()``;
  - ``evolution``: ``evolution_api_url``, ``evolution_instance_name`` and
    ``evolution_api_key``, each present and non-empty, NOT stripped (main's
    check: a value of spaces counts as present);
  - ``zapi``: ``zapi_token`` and ``zapi_instance_id``, each non-empty after
    ``str.strip()`` (``zapi_client_token`` is optional);
  - ``meta``: ``meta_phone_number_id`` and ``meta_permanent_token``, each
    non-empty after ``str.strip()``.

  A tenant without a ``clinic_configs`` row (or with ``api_credentials``
  NULL) has none of them.  ``str.strip()`` is reproduced in the database
  with ``TRIM(value, <every character for which str.isspace() holds>)``
  (:data:`PYTHON_WHITESPACE`, a bound parameter), not the space-only
  ``TRIM(value)``.  The credential writer stores only strings
  (``WhatsAppCredentialsPatch``, strict); a non-string JSON value written
  some other way is judged by its text.  The egress flag of Uazapi
  (``uazapi_egress_enabled``) is process configuration, not a tenant's
  credential, and is not part of the verdict;
* ``events_without_idempotency_key``: the ``POST /v1/events`` traffic of the
  last 7 days that left no ingress claim (wish Decision 9; see
  :data:`EVENTS_WITHOUT_KEY_DAYS` and :func:`_events_without_key`): under
  the rule it is answered 400 ``idempotency_key_required``.  A floor, with
  one known excess (:data:`EVENTS_APPROXIMATION`, printed in
  ``events_window``);
* ``subscription``: whether an ASAAS subscription is active, and its status;
* ``binding_lag``: an active ASAAS subscription whose current cycle is paid
  (``settled``/``restored``) with no binding written yet -- the fact
  consumer's lag that protects while it lasts -- and how long it has lasted
  (see :func:`_binding_lag`);
* ``coverage``: ``CreditRolloutService.binding_coverage`` at the database
  clock (``usable``, ``reconciliation_failure``), read-only.

``summary`` totals the list: the builders per state and the open debt, and
the traffic without key, the clinics per channel and the clinics with
incomplete send credentials (``clinics_with_incomplete_credentials``, and
``incomplete_credentials_per_channel``) twice -- ``under_rule`` over the
``charged`` and ``blocked`` builders (the exposure to the new 400 and to the
charge) and ``all_states`` over every listed builder.

Guarantees:

1. The credit switch is re-read IN THIS PROCESS (``refresh_credit_switch``)
   and the command refuses unless it is effective (``switch_off``), reading
   nothing else.  With the 046 courtesy gate closed (database below 046,
   where no courtesy exists) it refuses (``courtesy_rule_gate_closed``)
   without reading a lot; the 047 marks gate is passed to the repository as
   read (below 047 the rule decides alone, as on the request path).
2. Everything is read in one ``REPEATABLE READ READ ONLY`` transaction (its
   first statement) that is rolled back: PostgreSQL refuses any write in
   it, and none is attempted.
3. No credential reaches the output or the log.  From
   ``clinic_configs.api_credentials`` only the key ``whatsapp_provider`` is
   read as a value -- printed only when it is a known provider name, else
   ``unrecognized`` -- and the four routing keys and the send keys of
   :data:`SEND_CREDENTIALS` are reduced to booleans INSIDE the database
   (non-empty or not): their values never leave it.  Builders
   are printed by id and display name, tenants by id: no e-mail, phone
   number, token, key or URL.

Output: exactly one JSON document on stdout, and on stderr the same document
plus the log fields (``event: credit_courtesy_rule_ops``), after any JSON
log line of the code it ran (``configure_task_logging``).  Exit codes are in
``EXIT_CODES``: 0 only for ``ok``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Callable, Final, Mapping, NoReturn, Sequence, TextIO
from uuid import UUID

# Every ``src`` import builds the global settings, which raise on an invalid
# environment.  The failure is kept so the command still prints its one JSON
# line instead of a traceback (the pattern of ``src.ops.credit_charge_mark``).
try:
    from sqlalchemy import Boolean, String, and_, bindparam, exists, func, select, text
    from sqlalchemy.dialects.postgresql import UUID as PG_UUID
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.config.settings import settings
    from src.models.billing import BillingSubscription, BillingSubscriptionCycle
    from src.models.builder import Builder
    from src.models.credit import (
        CreditAccount,
        CreditEnforcementBinding,
        CreditIngressClaim,
        CreditLot,
        CreditObservationalEffect,
    )
    from src.models.tenant import Tenant
    from src.repositories.credit import (
        PAID_CYCLE_STATES,
        PAID_LOT_KINDS,
        CourtesyRuleReading,
        CreditRepository,
        EffectiveEnforcement,
        PaidPlanInForce,
    )
    from src.repositories.credit_inventory import CreditInventoryRepository
    from src.services.api_credentials import PROVIDER_AVAILABILITY
    from src.services.credit_allocations import (
        AccountingWindow,
        accounting_window,
        aggregate_target_mc,
        charge_delta,
        courtesy_payable,
    )
    from src.services.credit_inventory import (
        CATEGORIES,
        InventoryReadbackFailed,
        InventorySnapshot,
    )
    from src.services.credit_ledger import (
        _DEBT_COLUMNS,
        _LOT_COLUMNS,
        LEDGER_EFFECT_KINDS,
        LedgerBook,
        assemble_book,
        civil_date,
    )
    from src.services.credit_rollout import CreditRolloutService
    from src.services.credit_switch import (
        CreditSwitchReading,
        courtesy_rule_gate_open,
        credit_switch_effective,
        enforcement_mark_gate_open,
        refresh_credit_switch,
    )
except Exception as exc:  # reported by ``amain`` as one JSON line
    _IMPORT_ERROR: BaseException | None = exc
else:
    _IMPORT_ERROR = None

LOGGER_NAME: Final = "src.ops.credit_courtesy_rule"
SCHEMA: Final = "neurostack.credit_courtesy_rule_ops.v1"
LOG_EVENT: Final = "credit_courtesy_rule_ops"
COMMANDS: Final = ("status",)
EXIT_CODES: Final[Mapping[str, int]] = MappingProxyType(
    {
        "ok": 0,
        "refused": 20,
        "switch_off": 21,
        "unconfigured": 30,
        "error": 50,
    }
)
REASONS: Final = frozenset(
    {
        "usage",
        "import_failed",
        "settings_invalid",
        "credit_switch_off",
        "courtesy_rule_gate_closed",
        "unexpected",
    }
)
# A builder's state at the deploy instant.  ``not_under_rule``: a courtesy
# lot without a grant entry (no ``first_grant_at``: the rule does not apply);
# ``rule_unread``: the repository read nothing (cannot happen once the 046
# gate was found open; kept so a surprise is printed, not hidden).
STATES: Final = (
    "charged",
    "blocked",
    "protected",
    "global",
    "mark",
    "not_under_rule",
    "rule_unread",
)
# The four channels of DESIGN item 14, in print order.
CHANNELS: Final = ("uazapi", "evolution", "zapi", "meta")
# ``whatsapp_provider`` values that name one of :data:`CHANNELS`.
PROVIDER_CHANNEL: Final[Mapping[str, str]] = MappingProxyType(
    {"uazapi": "uazapi", "evolution": "evolution", "zapi": "zapi", "meta_cloud": "meta"}
)
UNRECOGNIZED_PROVIDER: Final = "unrecognized"
# Decision 9: the last 7 days of ``/v1/events`` traffic.
EVENTS_WITHOUT_KEY_DAYS: Final = 7
# ``credit_metering.TRACE_EFFECT_KIND`` (pinned by the unit suite; not
# imported: that module pulls the HTTP stack into a one-off task).
TRACE_EFFECT_KIND: Final = "trace"
# ``InteractionOrigin.PRODUCTION.value`` (pinned by the unit suite; a literal
# so the constants exist even when the ``src`` imports above failed).
PRODUCTION_ORIGIN: Final = "production"
# What the ``/v1/events`` count leaves out or adds (printed with its window):
# it is a floor of the traffic the rule answers 400, plus one known excess.
EVENTS_APPROXIMATION: Final = (
    "a floor: only requests whose best-effort observational write was "
    "recorded are counted (a request that errored before its decision, or "
    "whose write was dropped -- timeout, busy pool, failure -- left no row); "
    "and a webhook decision of an authenticated production channel (Z-API, "
    "Meta, verified Uazapi) without a provider message id is counted too "
    "(the effect row stores no ingress source)"
)
# The states the rule acts on: the ``under_rule`` totals of the summary.
UNDER_RULE_STATES: Final = ("charged", "blocked")
# What the reservation estimate leaves out (printed with it).
RESERVATION_APPROXIMATION: Final = (
    "inventory as stored now (media bytes, knowledge characters); window "
    "bounds from the monthly cycles known now; RAG reprocessing and egress "
    "overage not included; payable_by_courtesy_mc at the current courtesy "
    "balance"
)
BINDING_LAG_SINCE: Final = "cycle_updated_at"
_READ_ONLY_SQL: Final = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"

# Per channel, the ``api_credentials`` keys ``CreditReplySender.prepare``
# (``src/api/routes/webhook.py``) requires before it sends a charged reply,
# each with how it judges the value: ``True`` non-empty after
# ``str.strip()``, ``False`` non-empty as stored (Evolution: main's check,
# unstripped).  Pinned against the sender by the unit and PostgreSQL suites.
SEND_CREDENTIALS: Final[Mapping[str, tuple[tuple[str, bool], ...]]] = MappingProxyType(
    {
        "uazapi": (("uazapi_api_url", True), ("uazapi_token", True)),
        "evolution": (
            ("evolution_api_url", False),
            ("evolution_instance_name", False),
            ("evolution_api_key", False),
        ),
        "zapi": (("zapi_token", True), ("zapi_instance_id", True)),
        "meta": (("meta_phone_number_id", True), ("meta_permanent_token", True)),
    }
)
# Every character ``str.strip()`` removes (those for which ``str.isspace()``
# holds; pinned by the unit suite): the set ``TRIM`` strips in SQL, so the
# database judges a send key as the sender does.
PYTHON_WHITESPACE: Final = (
    "\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


def _send_ready_sql(channel: str) -> str:
    """The boolean ``<channel>_send``: every key of
    :data:`SEND_CREDENTIALS` of ``channel`` non-empty, judged in SQL."""

    present = []
    for key, stripped in SEND_CREDENTIALS[channel]:
        value = f"c.api_credentials ->> '{key}'"
        if stripped:
            value = f"TRIM({value}, :whitespace)"
        present.append(f"COALESCE({value}, '') <> ''")
    return f"({' AND '.join(present)}) AS {channel}_send"


# The builder's tenants, each with its ``whatsapp_provider`` value and, for
# each channel, whether its INBOUND routing key is set -- the key the webhook
# route resolves the tenant by (``src.api.routes.webhook``:
# ``uazapi_instance_name``, ``evolution_instance_name``,
# ``zapi_inbound_token_digest``, ``meta_phone_number_id``) -- and whether its
# SEND credentials are complete (:data:`SEND_CREDENTIALS`).  The values are
# compared with '' inside the database and only the booleans come back: no
# credential or identifier of a provider ever reaches this process.
TENANT_CHANNELS_SQL: Final = (
    "SELECT t.id AS tenant_id, t.is_active AS is_active, "
    "c.api_credentials ->> 'whatsapp_provider' AS whatsapp_provider, "
    "COALESCE(TRIM(c.api_credentials ->> 'uazapi_instance_name'), '') <> '' "
    "AS uazapi_route, "
    "COALESCE(TRIM(c.api_credentials ->> 'evolution_instance_name'), '') <> '' "
    "AS evolution_route, "
    "COALESCE(TRIM(c.api_credentials ->> 'zapi_inbound_token_digest'), '') <> '' "
    "AS zapi_route, "
    "COALESCE(TRIM(c.api_credentials ->> 'meta_phone_number_id'), '') <> '' "
    "AS meta_route, "
    + ", ".join(_send_ready_sql(channel) for channel in CHANNELS)
    + " FROM tenants AS t "
    "LEFT JOIN clinic_configs AS c ON c.tenant_id = t.id "
    "WHERE t.builder_id = :builder_id "
    "ORDER BY t.id"
)

SessionFactory = Callable[[], "AsyncSession"]


def known_whatsapp_providers() -> frozenset[str]:
    """Every WhatsApp provider of the public availability matrix: the names
    printed as they are."""

    return frozenset(PROVIDER_AVAILABILITY["whatsapp"])


# ---------------------------------------------------------------------------
# Result contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunResult:
    """The one document a run prints; validated against closed vocabularies."""

    command: str | None
    verdict: str
    reasons: tuple[str, ...] = ()
    switch: Mapping[str, object] | None = None
    database_now: str | None = None
    gates: Mapping[str, object] | None = None
    global_enforcement_at: str | None = None
    events_window: Mapping[str, object] | None = None
    summary: Mapping[str, object] | None = None
    builders: Sequence[Mapping[str, object]] | None = None

    def __post_init__(self) -> None:
        if self.verdict not in EXIT_CODES:
            raise ValueError("verdict outside the closed vocabulary")
        if self.command is not None and self.command not in COMMANDS:
            raise ValueError("command outside the closed vocabulary")
        if self.verdict == "ok" and self.command != "status":
            raise ValueError("a success verdict without the command")
        for reason in self.reasons:
            if reason not in REASONS:
                raise ValueError("reason outside the closed vocabulary")
        for builder in self.builders or ():
            if builder.get("state") not in STATES:
                raise ValueError("state outside the closed vocabulary")
        object.__setattr__(self, "reasons", tuple(dict.fromkeys(self.reasons)))

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.verdict]

    def document(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "command": self.command,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "exit_code": self.exit_code,
            "switch": None if self.switch is None else dict(self.switch),
            "database_now": self.database_now,
            "gates": None if self.gates is None else dict(self.gates),
            "global_enforcement_at": self.global_enforcement_at,
            "events_window": (
                None if self.events_window is None else dict(self.events_window)
            ),
            "summary": None if self.summary is None else dict(self.summary),
            "builders": (
                None if self.builders is None else [dict(row) for row in self.builders]
            ),
        }


def _iso(value: datetime | None) -> str | None:
    """UTC ISO 8601 (a naive value, as SQLite returns it, is UTC)."""

    if value is None:
        return None
    return _utc(value).isoformat()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _day(value: date | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):  # SQLite returns a date's text
        value = date.fromisoformat(value)
    return value.isoformat()


# ---------------------------------------------------------------------------
# Pure formatting
# ---------------------------------------------------------------------------


def provider_label(raw: str | None) -> str | None:
    """The printable ``whatsapp_provider`` of one tenant: never a stored
    value outside the known names, so a credential written under the key
    cannot be printed."""

    if raw is None:
        return None
    return raw if raw in known_whatsapp_providers() else UNRECOGNIZED_PROVIDER


def builder_state(effective: EffectiveEnforcement, reading: CourtesyRuleReading) -> str:
    """The state printed for a builder that ever received courtesy: the
    origin of :class:`~src.repositories.credit.EffectiveEnforcement` (the
    request path's verdict) and, under the rule, its ``state``."""

    if effective.origin in ("global", "mark"):
        return str(effective.origin)
    if effective.origin == "courtesy":
        return "blocked" if effective.blocked else "charged"
    if effective.rule_unread:
        return "rule_unread"
    if not reading.under_rule:
        return "not_under_rule"
    return "protected"


def enforcement_document(effective: EffectiveEnforcement) -> dict[str, object]:
    return {
        "origin": "none" if effective.origin is None else effective.origin,
        "effective_at": _iso(effective.at),
        "rule_state": effective.state,
        "mark_unread": effective.mark_unread,
    }


def courtesy_document(
    reading: CourtesyRuleReading,
    nearest_expiry: datetime | None,
    last_expiry: datetime | None = None,
) -> dict[str, object]:
    """``nearest_expiry_at``/``last_expiry_at``: the first and the last
    expiry of a courtesy lot that can still spend -- the last is when a
    charged builder is blocked by expiry if nothing else changes."""

    return {
        "lots": reading.courtesy_lots,
        "first_grant_at": _iso(reading.first_grant_at),
        "spendable_mc": reading.spendable_mc,
        "nearest_expiry_at": _iso(nearest_expiry),
        "last_expiry_at": _iso(last_expiry),
    }


def paid_plan_document(
    plan: PaidPlanInForce, paid_lot_expires_at: datetime | None = None
) -> dict[str, object]:
    """``paid_lot_expires_at``: the latest future expiry of a paid lot
    (:func:`_paid_lot_expiry`), until which ``paid_lot`` protects."""

    return {
        "in_force": plan.in_force,
        "paid_lot": plan.paid_lot,
        "paid_lot_expires_at": _iso(paid_lot_expires_at),
        "last_binding_cycle_end": _day(plan.last_binding_cycle_end),
        "grace_ends_at": _iso(plan.grace_ends_at),
        "in_grace": plan.in_grace,
        "unbound_paid_cycle": plan.unbound_paid_cycle,
    }


@dataclass(frozen=True)
class TenantChannels:
    """One tenant as :data:`TENANT_CHANNELS_SQL` reads it."""

    tenant_id: UUID
    is_active: bool
    whatsapp_provider: str | None
    routes: Mapping[str, bool]
    # Per channel, whether the send credentials are complete
    # (:data:`SEND_CREDENTIALS`, judged in the database).
    send_ready: Mapping[str, bool]

    @property
    def channels(self) -> tuple[str, ...]:
        """The channels of :data:`CHANNELS` the tenant is on: the one its
        ``whatsapp_provider`` names, and every one whose inbound routing key
        is set (a webhook of that channel reaches the tenant)."""

        named = PROVIDER_CHANNEL.get(self.whatsapp_provider or "")
        return tuple(
            channel
            for channel in CHANNELS
            if channel == named or self.routes.get(channel, False)
        )

    @property
    def credentials_incomplete(self) -> tuple[str, ...]:
        """The channels the tenant is on (:attr:`channels`) whose send
        credentials are incomplete: a charged reply there is never sent."""

        return tuple(
            channel
            for channel in self.channels
            if not self.send_ready.get(channel, False)
        )


def clinics_document(rows: Sequence[TenantChannels]) -> dict[str, object]:
    """Per channel, the count and ids of the tenants on it (a tenant on two
    channels is in both); ``none`` lists the tenants on none of the four.
    ``credentials_incomplete``: the tenants with incomplete send credentials
    on at least one of their channels (``count``, ``tenant_ids``) and, per
    channel, those incomplete on it."""

    by_channel: dict[str, list[str]] = {channel: [] for channel in CHANNELS}
    incomplete: dict[str, list[str]] = {channel: [] for channel in CHANNELS}
    unrouted: list[str] = []
    any_incomplete: list[str] = []
    for row in rows:
        channels = row.channels
        for channel in channels:
            by_channel[channel].append(str(row.tenant_id))
        if not channels:
            unrouted.append(str(row.tenant_id))
        missing = row.credentials_incomplete
        for channel in missing:
            incomplete[channel].append(str(row.tenant_id))
        if missing:
            any_incomplete.append(str(row.tenant_id))
    return {
        "total": len(rows),
        "by_channel": {
            channel: {"count": len(ids), "tenant_ids": ids}
            for channel, ids in [*by_channel.items(), ("none", unrouted)]
        },
        "credentials_incomplete": {
            "count": len(any_incomplete),
            "tenant_ids": any_incomplete,
            "by_channel": {
                channel: {"count": len(ids), "tenant_ids": ids}
                for channel, ids in incomplete.items()
            },
        },
        "tenants": [
            {
                "tenant_id": str(row.tenant_id),
                "active": row.is_active,
                "whatsapp_provider": provider_label(row.whatsapp_provider),
                "inbound_routes": {
                    channel: bool(row.routes.get(channel, False))
                    for channel in CHANNELS
                },
                "channels": list(row.channels),
                "credentials_incomplete": list(row.credentials_incomplete),
            }
            for row in rows
        ],
    }


def first_window_after_deploy(
    cycles: Sequence[tuple[date, date]], now: datetime, enforcement_at: datetime
) -> tuple[AccountingWindow, str]:
    """The first accounting window whose opening CHARGES, after ``now``.

    The window open at the enforcement instant -- under the rule the first
    courtesy grant -- opens as a free baseline and maintenance starts with
    a window that begins after it (owner decision E,
    ``credit_inventory.WindowWork._open``: maintenance iff
    ``window.starts_at > enforcement_at``).  So: the current window when the
    enforcement came before it (``current``), else the one that starts when
    it ends (``next``), both by ``credit_allocations.accounting_window`` over
    the monthly cycles known now."""

    current = accounting_window(cycles, now)
    if current.starts_at > enforcement_at:
        return current, "current"
    return accounting_window(cycles, current.ends_at), "next"


def reservation_document(
    *,
    window: AccountingWindow,
    which: str,
    snapshot: InventorySnapshot,
    coverage: Mapping[str, int],
    opened: frozenset[str],
    applies: bool,
    courtesy_rule: bool,
    spendable_mc: int,
) -> dict[str, object]:
    """The estimated storage charge of ``window``, with the inventory code's
    own rules (``credit_inventory.WindowWork._open``/``charge``): per
    category the target ``aggregate_target_mc(units)``; a category not yet
    opened in the window opens as maintenance and debits the whole target, an
    opened one debits ``charge_delta(target, coverage)`` (what its next
    reservation bills).  Under the courtesy rule only what the courtesy can
    pay is debited (``courtesy_payable``); the rest is no debt.  ``applies``
    is false for a builder whose state charges nothing (protected): the
    figure is what it would be under the rule."""

    categories: dict[str, object] = {}
    total = 0
    for category in CATEGORIES:
        units = snapshot.units(category)
        target = aggregate_target_mc(units, category)
        covered = coverage.get(category, 0)
        charge = charge_delta(target, covered) if category in opened else target
        total += charge
        categories[category] = {
            "units": units,
            "target_mc": target,
            "coverage_mc": covered,
            "opened": category in opened,
            "estimated_charge_mc": charge,
        }
    return {
        "applies": applies,
        "window": {
            "which": which,
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "kind": window.kind,
        },
        "categories": categories,
        "estimated_charge_mc": total,
        "payable_by_courtesy_mc": (
            courtesy_payable(total, spendable_mc) if courtesy_rule else None
        ),
        "approximation": RESERVATION_APPROXIMATION,
    }


def _switch_document(reading: CreditSwitchReading) -> dict[str, object]:
    return {
        "effective": reading.effective,
        "reason": reading.reason,
        "revision": reading.revision,
    }


def _exposure(builders: Sequence[Mapping[str, Any]]) -> dict[str, object]:
    return {
        "builders": len(builders),
        "events_without_idempotency_key": sum(
            int(builder["events_without_idempotency_key"]["total"])
            for builder in builders
        ),
        "clinics_per_channel": {
            channel: sum(
                int(builder["clinics"]["by_channel"][channel]["count"])
                for builder in builders
            )
            for channel in (*CHANNELS, "none")
        },
        "clinics_with_incomplete_credentials": sum(
            int(builder["clinics"]["credentials_incomplete"]["count"])
            for builder in builders
        ),
        "incomplete_credentials_per_channel": {
            channel: sum(
                int(
                    builder["clinics"]["credentials_incomplete"]["by_channel"][channel][
                        "count"
                    ]
                )
                for builder in builders
            )
            for channel in CHANNELS
        },
    }


def summary_document(builders: Sequence[Mapping[str, Any]]) -> dict[str, object]:
    """The totals: ``under_rule`` over the builders the rule acts on
    (:data:`UNDER_RULE_STATES`: exposed to the new 400 and to the courtesy
    charge), ``all_states`` over every listed builder (protected, mark and
    global included)."""

    counts = {state: 0 for state in STATES}
    for builder in builders:
        counts[str(builder["state"])] += 1
    under_rule = [
        builder for builder in builders if builder["state"] in UNDER_RULE_STATES
    ]
    return {
        "builders": len(builders),
        "states": counts,
        "open_debt_mc": sum(
            int(builder["open_debt"]["open_debt_mc"]) for builder in builders
        ),
        "under_rule": {"states": list(UNDER_RULE_STATES), **_exposure(under_rule)},
        "all_states": _exposure(builders),
    }


# ---------------------------------------------------------------------------
# Database reads (one read-only transaction, rolled back)
# ---------------------------------------------------------------------------


async def _read_only(session: AsyncSession) -> None:
    """The first statement of the transaction: nothing after it can write."""

    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text(_READ_ONLY_SQL))


async def _courtesy_builders(session: AsyncSession) -> list[tuple[UUID, str, bool]]:
    """``(id, name, active)`` of every builder that ever received a
    ``courtesy`` lot, in id order."""

    has_courtesy = (
        select(CreditLot.id)
        .where(CreditLot.builder_id == Builder.id, CreditLot.lot_kind == "courtesy")
        .exists()
    )
    rows = (
        await session.execute(
            select(Builder.id, Builder.name, Builder.is_active, Builder.status)
            .where(has_courtesy)
            .order_by(Builder.id)
        )
    ).all()
    return [
        (
            UUID(str(row.id)),
            str(row.name),
            row.is_active is True and str(row.status) == "active",
        )
        for row in rows
    ]


async def _book(session: AsyncSession, builder_id: UUID, now: datetime) -> LedgerBook:
    """The builder's book from its stored rows, swept at ``now`` in memory
    (the read of ``/v1/credits/debt``; no lock, nothing persisted)."""

    repository = CreditRepository(session)
    account_id = await session.scalar(
        select(CreditAccount.id).where(CreditAccount.builder_id == builder_id)
    )
    lots = (
        (
            await session.execute(
                select(CreditLot)
                .where(CreditLot.builder_id == builder_id)
                .order_by(CreditLot.id)
            )
        )
        .scalars()
        .all()
    )
    book = assemble_book(
        builder_id=builder_id,
        # A lot always has an account; the fallback keeps an odd row readable.
        account_id=account_id if account_id is not None else builder_id,
        lots=[
            {column: getattr(lot, column) for column in _LOT_COLUMNS} for lot in lots
        ],
        lot_sums=await repository.lot_entry_sums(builder_id),
        debts=[
            {column: getattr(debt, column) for column in _DEBT_COLUMNS}
            for debt in await repository.debts(builder_id)
        ],
        settled=await repository.settlement_sums(builder_id),
        known_effects=await repository.effect_ids_of_kinds(
            builder_id, LEDGER_EFFECT_KINDS
        ),
        release_rows=await repository.release_sums(builder_id),
        ledger_debt_mc=(await repository.derived_balance(builder_id)).debt_mc,
        clock_floor=await repository.latest_occurred_at(builder_id),
    )
    book.sweep_expired(now)
    return book


def _courtesy_expiries(
    book: LedgerBook, now: datetime
) -> tuple[datetime | None, datetime | None]:
    """The nearest and the last expiry of a courtesy lot that can still
    spend at ``now`` (the swept book)."""

    expiries = [
        lot.expires_at
        for lot in book.lots
        if lot.lot_kind == "courtesy" and lot.spendable_mc(now) > 0
    ]
    return (min(expiries), max(expiries)) if expiries else (None, None)


def _paid_lot_expiry(book: LedgerBook, now: datetime) -> datetime | None:
    """The latest ``expires_at`` after ``now`` of a ``monthly``/``topup``
    lot: the predicate of ``paid_plan_in_force``'s ``paid_lot`` (any paid
    lot not yet expired, whatever its balance), so ``paid_lot`` holds until
    this instant if nothing else changes."""

    expiries = [
        lot.expires_at
        for lot in book.lots
        if lot.lot_kind in PAID_LOT_KINDS and lot.expires_at > now
    ]
    return max(expiries) if expiries else None


async def _tenant_channels(
    session: AsyncSession, builder_id: UUID
) -> list[TenantChannels]:
    statement = (
        text(TENANT_CHANNELS_SQL)
        .bindparams(
            bindparam("builder_id", type_=PG_UUID(as_uuid=True)),
            bindparam("whitespace", type_=String()),
        )
        .columns(
            tenant_id=PG_UUID(as_uuid=True),
            is_active=Boolean(),
            whatsapp_provider=String(),
            **{f"{channel}_route": Boolean() for channel in CHANNELS},
            **{f"{channel}_send": Boolean() for channel in CHANNELS},
        )
    )
    rows = (
        await session.execute(
            statement, {"builder_id": builder_id, "whitespace": PYTHON_WHITESPACE}
        )
    ).all()
    return [
        TenantChannels(
            tenant_id=UUID(str(row.tenant_id)),
            is_active=bool(row.is_active),
            whatsapp_provider=(
                None if row.whatsapp_provider is None else str(row.whatsapp_provider)
            ),
            routes={
                channel: bool(getattr(row, f"{channel}_route")) for channel in CHANNELS
            },
            send_ready={
                channel: bool(getattr(row, f"{channel}_send")) for channel in CHANNELS
            },
        )
        for row in rows
    ]


async def _events_without_key(
    session: AsyncSession, builder_id: UUID, since: datetime
) -> dict[str, object]:
    """``POST /v1/events`` traffic without ``Idempotency-Key`` (wish
    Decision 9), per tenant of the builder, since ``since``.

    The observational writer (``credit_metering._write_steps``) records one
    ``credit_observational_effects`` row per decision (``effect_kind =
    'trace'``, ``origin``, ``authenticated``, ``trace_id`` = the decision's
    anchor) and, for ``/v1/events``, takes an ingress claim ONLY for origin
    ``production``, an authenticated principal (an API key) and a valid
    ``Idempotency-Key`` (``credit_metering._claim_key``, H3); the claim's
    ``interaction_id`` then becomes the effect's ``trace_id``.  So the
    traffic that a missing or malformed key leaves is exactly::

        SELECT e.tenant_id, count(*), max(e.recorded_at)
        FROM credit_observational_effects AS e
        JOIN tenants AS t ON t.id = e.tenant_id
        WHERE t.builder_id = :builder_id
          AND e.effect_kind = 'trace'
          AND e.origin = 'production'
          AND e.authenticated IS TRUE
          AND e.recorded_at > :since          -- database clock - 7 days
          AND NOT EXISTS (
              SELECT 1 FROM credit_ingress_claims AS c
              WHERE c.tenant_id = e.tenant_id
                AND c.interaction_id = e.trace_id)
        GROUP BY e.tenant_id ORDER BY e.tenant_id

    The tenant's builder is the one stored NOW (``tenants.builder_id``): the
    rule answers the tenant's current builder.  A claim of any source
    matches (a provider webhook's claim names its own decision).  Accepted
    approximation: the effect row stores no ingress source, so a webhook
    decision of an authenticated production channel (Z-API, Meta, a
    verified Uazapi callback) WITHOUT a provider message id -- which those
    adapters send with every message, so their decisions are claimed --
    would be counted too.  And it is a floor: a request that errored before
    its decision, or whose best-effort write was dropped, left no row
    (:data:`EVENTS_APPROXIMATION`).  Simulator and unknown origins,
    unauthenticated principals and claimed requests are never counted."""

    effect, claim = CreditObservationalEffect, CreditIngressClaim
    claimed = exists().where(
        claim.tenant_id == effect.tenant_id, claim.interaction_id == effect.trace_id
    )
    rows = (
        await session.execute(
            select(
                effect.tenant_id,
                func.count().label("requests"),
                func.max(effect.recorded_at).label("latest"),
            )
            .join(Tenant, Tenant.id == effect.tenant_id)
            .where(
                Tenant.builder_id == builder_id,
                effect.effect_kind == TRACE_EFFECT_KIND,
                effect.origin == PRODUCTION_ORIGIN,
                effect.authenticated.is_(True),
                effect.recorded_at > since,
                ~claimed,
            )
            .group_by(effect.tenant_id)
            .order_by(effect.tenant_id)
        )
    ).all()
    by_tenant = [
        {
            "tenant_id": str(row.tenant_id),
            "requests": int(row.requests),
            "latest_at": _iso(row.latest),
        }
        for row in rows
    ]
    return {
        "total": sum(int(row.requests) for row in rows),
        "by_tenant": by_tenant,
    }


async def _subscription(session: AsyncSession, builder_id: UUID) -> dict[str, object]:
    """Whether an ASAAS subscription of the builder is active (the
    ``provider = 'asaas' AND is_active`` of ``paid_plan_in_force``), and the
    status of the active one -- else of the latest one."""

    subscription = BillingSubscription
    rows = (
        await session.execute(
            select(
                subscription.id,
                subscription.provider,
                subscription.status,
                subscription.is_active,
            )
            .where(subscription.builder_id == builder_id)
            .order_by(subscription.created_at.desc(), subscription.id)
        )
    ).all()
    active = [row for row in rows if row.provider == "asaas" and row.is_active is True]
    shown = active[0] if active else (rows[0] if rows else None)
    return {
        "active": bool(active),
        "status": None if shown is None else str(shown.status),
        "subscriptions": [
            {
                "id": str(row.id),
                "provider": None if row.provider is None else str(row.provider),
                "status": str(row.status),
                "is_active": row.is_active is True,
            }
            for row in rows
        ],
    }


async def _binding_lag(
    session: AsyncSession, builder_id: UUID, now: datetime
) -> dict[str, object]:
    """The binding lag (wish Decision 5): an ACTIVE ASAAS subscription whose
    CURRENT cycle (``cycle_start <= today < cycle_end``, today the civil date
    of ``now`` in São Paulo) is in a paid state (``PAID_CYCLE_STATES``) with
    no binding written for it -- the conditions of
    ``paid_plan_in_force``'s ``unbound_paid_cycle``, whose boolean this
    reports with the cycle.  How long: since the cycle row's ``updated_at``
    (the producer's last write of it, the settlement or restoration that
    made it paid): when the fact became available to the consumer, to the
    precision of that column."""

    cycle, subscription = BillingSubscriptionCycle, BillingSubscription
    binding = CreditEnforcementBinding
    today = civil_date(now)
    bound = select(binding.id).where(binding.subscription_cycle_id == cycle.id)
    rows = (
        await session.execute(
            select(
                cycle.id,
                cycle.cycle_start,
                cycle.cycle_end,
                cycle.state,
                cycle.updated_at,
            )
            .join(
                subscription,
                and_(
                    subscription.id == cycle.subscription_id,
                    subscription.builder_id == cycle.builder_id,
                ),
            )
            .where(
                cycle.builder_id == builder_id,
                cycle.state.in_(PAID_CYCLE_STATES),
                cycle.cycle_start <= today,
                cycle.cycle_end > today,
                subscription.provider == "asaas",
                subscription.is_active.is_(True),
                ~bound.exists(),
            )
            .order_by(cycle.updated_at, cycle.id)
        )
    ).all()
    if not rows:
        return {
            "lagging": False,
            "cycles": [],
            "since": None,
            "since_column": BINDING_LAG_SINCE,
            "seconds": None,
        }
    since = min(_utc(row.updated_at) for row in rows)
    return {
        "lagging": True,
        "cycles": [
            {
                "cycle_id": str(row.id),
                "cycle_start": _day(row.cycle_start),
                "cycle_end": _day(row.cycle_end),
                "state": str(row.state),
                "updated_at": _iso(row.updated_at),
            }
            for row in rows
        ],
        "since": _iso(since),
        "since_column": BINDING_LAG_SINCE,
        "seconds": max(int((now - since).total_seconds()), 0),
    }


async def _reservation(
    session: AsyncSession,
    builder_id: UUID,
    *,
    now: datetime,
    effective: EffectiveEnforcement,
    reading: CourtesyRuleReading,
) -> dict[str, object] | None:
    """:func:`reservation_document` of the first window after the deploy;
    ``None`` with no enforcement instant at all (not under the rule)."""

    enforcement_at = (
        effective.at if effective.at is not None else reading.first_grant_at
    )
    if enforcement_at is None:
        return None
    inventory = CreditInventoryRepository(session)
    window, which = first_window_after_deploy(
        await inventory.monthly_cycles(builder_id), now, _utc(enforcement_at)
    )
    coverage: dict[str, int] = {}
    opened: set[str] = set()
    for row in await inventory.window_allocations(builder_id, window.start):
        coverage[row.category] = max(coverage.get(row.category, 0), row.target_mc)
        if row.allocation_kind in ("baseline", "maintenance"):
            opened.add(row.category)
    try:
        snapshot = InventorySnapshot.of(await inventory.builder_sources(builder_id))
    except InventoryReadbackFailed:
        # The request path answers ``credit_pending`` for such an inventory.
        return {"applies": effective.enforced, "error": "inventory_unreadable"}
    return reservation_document(
        window=window,
        which=which,
        snapshot=snapshot,
        coverage=coverage,
        opened=frozenset(opened),
        applies=effective.enforced,
        courtesy_rule=effective.origin == "courtesy" or effective.origin is None,
        spendable_mc=reading.spendable_mc,
    )


async def _builder_entry(
    session: AsyncSession,
    *,
    builder_id: UUID,
    name: str,
    active: bool,
    now: datetime,
    since: datetime,
    marks_readable: bool,
) -> dict[str, object]:
    repository = CreditRepository(session)
    effective = await repository.effective_enforcement(
        builder_id, marks_readable=marks_readable, rule_readable=True, at=now
    )
    reading = await repository.courtesy_rule(builder_id, at=now)
    plan = await repository.paid_plan_in_force(builder_id, at=now)
    book = await _book(session, builder_id, now)
    open_by_id = book.open_debt_by_id()
    coverage = await CreditRolloutService(session).binding_coverage(builder_id, at=now)
    return {
        "builder_id": str(builder_id),
        "name": name,
        "active": active,
        "state": builder_state(effective, reading),
        "enforcement": enforcement_document(effective),
        "courtesy": courtesy_document(reading, *_courtesy_expiries(book, now)),
        "paid_plan": paid_plan_document(plan, _paid_lot_expiry(book, now)),
        "open_debt": {
            "open_debt_mc": book.open_debt_mc,
            "open_debts": sum(1 for value in open_by_id.values() if value > 0),
        },
        "reservation": await _reservation(
            session, builder_id, now=now, effective=effective, reading=reading
        ),
        "clinics": clinics_document(await _tenant_channels(session, builder_id)),
        "events_without_idempotency_key": await _events_without_key(
            session, builder_id, since
        ),
        "subscription": await _subscription(session, builder_id),
        "binding_lag": await _binding_lag(session, builder_id, now),
        "coverage": {
            "usable": coverage.usable,
            "reconciliation_failure": coverage.reconciliation_failure,
        },
    }


async def _status(
    sessions: SessionFactory,
    reading: CreditSwitchReading,
    switch: dict[str, object],
) -> RunResult:
    marks_readable = enforcement_mark_gate_open(reading)
    gates = {"courtesy_rule": True, "marks": marks_readable}
    async with sessions() as session:
        try:
            await _read_only(session)
            repository = CreditRepository(session)
            now = _utc(await repository.database_now())
            since = now - timedelta(days=EVENTS_WITHOUT_KEY_DAYS)
            global_at = await repository.enforcement_at()
            builders = [
                await _builder_entry(
                    session,
                    builder_id=builder_id,
                    name=name,
                    active=active,
                    now=now,
                    since=since,
                    marks_readable=marks_readable,
                )
                for builder_id, name, active in await _courtesy_builders(session)
            ]
        finally:
            await session.rollback()
    return RunResult(
        command="status",
        verdict="ok",
        switch=switch,
        database_now=_iso(now),
        gates=gates,
        global_enforcement_at=_iso(global_at),
        events_window={
            "since": _iso(since),
            "until": _iso(now),
            "days": EVENTS_WITHOUT_KEY_DAYS,
            "approximation": EVENTS_APPROXIMATION,
        },
        summary=summary_document(builders),
        builders=builders,
    )


async def run(
    args: argparse.Namespace, *, session_factory: SessionFactory | None = None
) -> RunResult:
    """Run one parsed command; owns the process pool unless one is injected."""

    owned = session_factory is None
    sessions = session_factory
    try:
        if owned and settings.credit_hot_path_enabled is True:
            from src.repositories.database import get_db_session_factory, init_database

            await init_database()
            sessions = get_db_session_factory()
        # Re-read the switch here: the verdict is per process and nothing
        # else refreshes it in a one-off task.  Off, it reads nothing.
        reading = await refresh_credit_switch(session_factory=sessions)
        switch = _switch_document(reading)
        if not (reading.effective and credit_switch_effective()):
            return RunResult(
                command=args.command,
                verdict="switch_off",
                reasons=("credit_switch_off",),
                switch=switch,
            )
        if not courtesy_rule_gate_open(reading):
            # Below 046 there is no courtesy: nothing is read.
            return RunResult(
                command=args.command,
                verdict="refused",
                reasons=("courtesy_rule_gate_closed",),
                switch=switch,
            )
        if sessions is None:  # effective implies a database was read
            raise RuntimeError("no session factory for an effective switch")
        return await _status(sessions, reading, switch)
    finally:
        if owned:
            from src.repositories.database import close_database

            await close_database()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError()


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    """Parse one invocation; anything malformed raises ``_UsageError``."""

    # No ``--help``: the only exit 0 is the command's success verdict.
    parser = _Parser(
        prog="python -m src.ops.credit_courtesy_rule",
        add_help=False,
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=_Parser
    )
    commands.add_parser("status", add_help=False, allow_abbrev=False)
    return parser.parse_args(list(argv))


def _import_failure(command: str, error: BaseException) -> RunResult:
    """Settings that cannot load are ``unconfigured``; a broken image is an error."""

    if type(error).__name__ in {"ValidationError", "ConfigurationError"}:
        return RunResult(
            command=command, verdict="unconfigured", reasons=("settings_invalid",)
        )
    return RunResult(command=command, verdict="error", reasons=("import_failed",))


def _dumps(document: Mapping[str, object]) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


async def amain(
    argv: Sequence[str] | None = None,
    *,
    session_factory: SessionFactory | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run one invocation; print one document (stdout) and one log line (stderr)."""

    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    started = time.monotonic()
    error_class: str | None = None
    result: RunResult
    try:
        args = parse_arguments(sys.argv[1:] if argv is None else argv)
    except _UsageError:
        result = RunResult(command=None, verdict="unconfigured", reasons=("usage",))
    else:
        try:
            if _IMPORT_ERROR is not None:
                result = _import_failure(args.command, _IMPORT_ERROR)
            else:
                result = await run(args, session_factory=session_factory)
        except Exception as exc:
            error_class = type(exc).__name__
            result = RunResult(
                command=args.command, verdict="error", reasons=("unexpected",)
            )
    document = result.document()
    out.write(_dumps(document) + "\n")
    out.flush()
    err.write(
        _dumps(
            {
                **document,
                "event": LOG_EVENT,
                "level": "info" if result.exit_code == 0 else "warning",
                "logger": LOGGER_NAME,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "error_class": error_class,
            }
        )
        + "\n"
    )
    err.flush()
    return result.exit_code


def configure_task_logging(stream: TextIO) -> None:
    """JSON log lines on *stream*, whatever ``LOG_FORMAT`` says.

    The configuration of the charge-mark and L10 tasks (kept apart on
    purpose: no module imports those tasks).  The API's ``configure_logging``
    is not used: with ``LOG_FORMAT=console`` it renders exceptions through
    structlog's console renderer, which prints a traceback's local variables
    when ``rich`` is importable -- among them the database URL with its
    password.  ``format_exc_info`` renders the plain traceback, no locals.
    """

    import logging

    import structlog

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
    logging.basicConfig(
        format="%(message)s", stream=stream, level=logging.INFO, force=True
    )
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main(argv: Sequence[str] | None = None) -> int:
    """The process entry point: anything stray goes to stderr, so stdout
    carries exactly the one document."""

    out, err = sys.stdout, sys.stderr
    configure_task_logging(err)
    with contextlib.redirect_stdout(err):
        return asyncio.run(amain(argv, stdout=out, stderr=err))


if __name__ == "__main__":
    raise SystemExit(main())
