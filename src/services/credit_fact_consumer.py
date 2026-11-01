"""Credit consumer of ASAAS ``billing.fact.v1`` facts (Group 2).

The single ingestion point is :func:`handle_billing_fact`, a ``FactTransport``
(``src/services/billing_fact_worker.py``).  ``src/api/main.py`` is its only
wiring point: it hands it to the worker only with
``BILLING_FACT_TRANSPORT_ENABLED`` on (asaas-rollout-opening Group 2c, which
took the wiring over from the credit wish's Group 8); reading
``billing_outbox`` directly is never ingestion, only the lookup of a
presented fact's producer row.

One call, in order:

1. ``require_presentation_allowed()`` (``billing_outbox``): raises
   ``PresentationPreconditionsOpen`` only if the producer reopens its
   presentation guard (closed since asaas-rollout-opening Group 2c);
2. the dominance switch must be EFFECTIVE (``credit_switch_effective()``),
   otherwise :class:`CreditSwitchOff` -- nothing is read or written;
3. the consumer's own contract digests (:func:`consumer_contract_digests`),
   BEFORE ``validate_fact`` and outside the code that maps a fact violation
   to ``rejected``: the bundle manifest (``billing_fact_bundle_digest()``),
   the schema bytes and the catalog snapshot bytes must equal the pins below,
   and the bundle digest must equal the handoff's.  A divergence -- including
   ``BillingFactViolation`` from ``billing_fact_bundle_digest()`` when the
   manifest is unavailable -- raises :class:`ConsumerContractDivergence`
   without inbox or ack.  Wired through ``src/api/main.py``, an image-level
   divergence (bundle, schema or catalog against the pins, or bytes
   unavailable) is read first by the worker's readiness hook and the fact is
   turned away with no attempt spent; only a per-fact mismatch (the bundle
   digest against the handoff's) spends attempts, and the publisher presents
   it again until ``max_attempts`` of its ``FactRetryPolicy`` blocks it;
4. its own session (``get_db_session_factory()``), one transaction, the whole
   call inside ``TRANSPORT_TIMEOUT_SECONDS``; a cancelled connection is
   invalidated, never returned to the pool mid-statement;
5. resolve ``outbox_event_id`` by ``event_id`` (read-only lookup), then the
   builder's account lock (lock order step 2 of ``src.repositories.credit``)
   and the read-only quarantine hold, the same order as spend; the clock is
   read only once the lock is held and brought within the tolerance of the
   database clock before any path writes (owner decisions A and C): more
   than the tolerance ahead acts at the database clock plus the tolerance,
   with a warning, and more than the tolerance behind at the database clock
   minus the tolerance -- a fact is never refused for the writer's instant,
   whether it is applied, rejected or older than the applied revision.  The
   one clock refusal left is the ledger's: an applied fact of a builder
   whose ledger clock is already beyond the database clock plus the
   tolerance raises ``LedgerClockAhead`` -- the transaction, with the
   account row the lock step may have inserted, rolls back, so no inbox
   row, ack or gap is written, and the publisher presents it again;
6. a replay of a fact already in the inbox is a no-op (no second ack); the
   same identity with other bytes is a :class:`FactCollision`;
7. a problem IN the fact -- the recomputed ``payload_digest(payload)``
   differs from ``billing_outbox.payload_sha256``, ``validate_fact`` raises a
   ``BillingFactViolation`` (a partially settled credit-bearing line, money
   the lines do not attribute ...), or the fact disagrees with its outbox row
   or with the pinned catalog -- writes an inbox row whose aggregate identity
   comes from the OUTBOX ROW (never the payload) and a ``rejected`` ack with
   zero effects (``effects_digest = sha256(b"[]")``), and alerts.  The
   checks are ``validate_fact``'s closed invariants plus the outbox row and
   the catalog, not the producer's construction rules: a fact the producer
   itself would not emit (a partial refund attributed to one line of a
   multi-line aggregate) is accepted when it passes them (owner decision
   O4, 2026-10-26);
8. an accepted fact older than the aggregate's applied revision is a no-op
   with an ``accepted`` ack and zero effects -- the newer snapshot already
   holds it (owner decision O2, 2026-10-26); a newer
   one is applied as a complete cumulative snapshot -- the grant once, the
   cycle opening under the account lock, the missing reversal/restoration
   deltas (:mod:`src.services.credit_ledger`) -- and a revision jump opens a
   gap; the inbox row and the ``accepted`` ack (``fact_sha256`` recomputed,
   ``bundle_digest`` the consumer's own, ``deployment_digest`` copied from
   the handoff, ``effects_digest`` of the effects the fact itself wrote --
   not the expiries the deadline sweep records for other lots in the same
   transaction) are appended in the same transaction
   (``src/repositories/credit_fact_ack.py``);
9. the subscription cycle's ``credit_enforcement_binding`` is written with
   the grant and advanced (``financial_revision``, ``status``,
   ``updated_at``) by every applied cycle fact; ``status`` is ``reversed``
   only while the ``plan`` line is fully reversed net of restorations.  A
   second settled cycle of the same ``(subscription_id, cycle_start,
   cycle_end)`` raises :class:`CycleIdentityConflict` -- no ack, no binding:
   the delivery ends ``blocked`` after ``max_attempts`` with a ``backlog``
   alert, and the readiness stays false.

Alerts are log lines in this group (``credit fact rejected``, ``... older
than the applied revision``, ``... revision gap opened``, ``... cumulative
target regressed``); metrics are Group 7's (owner decision O3, 2026-10-26).

The consumer never writes an ASAAS table except that ack, never changes an
order, payment or subscription, and never reads ``tests/`` at import (the
bundle manifest is read when a fact is handled, through the producer's
``billing_fact_bundle_digest()``).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from types import MappingProxyType
from typing import Final, Literal, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from src.config.logging import get_logger
from src.models.credit import CreditEnforcementBinding
from src.observability.metric_dimensions import (
    ContractCounter,
    precreate_closed_children,
)
from src.repositories.credit import CreditRepository, FactOutboxRow
from src.repositories.credit_fact_ack import CreditFactAckRepository, FactHandoff
from src.services.billing_catalog import catalog_snapshot_bytes
from src.services.billing_fact_worker import TRANSPORT_TIMEOUT_SECONDS
from src.services.billing_outbox import (
    PRODUCER,
    AmbiguousLineAllocation,
    BillingFactViolation,
    PartialCreditLine,
    billing_fact_bundle_digest,
    billing_fact_schema_digest,
    payload_digest,
    require_presentation_allowed,
    validate_fact,
)
from src.services.credit_ledger import (
    DEBT_SETTLEMENT_EFFECT,
    MILLICREDITS_PER_CREDIT,
    CreditLedger,
    GrantSpec,
    LedgerBook,
    LedgerInvariantError,
    LotState,
    SpendHold,
    cumulative_target_mc,
    cycle_debt_settlement_effect_id,
    monthly_grant_effect_id,
    monthly_lot_expires_at,
    topup_grant_effect_id,
    topup_lot_expires_at,
)
from src.services.credit_switch import credit_switch_effective

logger = get_logger(__name__)

# The contract bytes this consumer was reviewed against (wish "Contract and
# dependency pins"): the manifest digest the bundle verifier emits, the
# schema digest the manifest lists and the producer's catalog snapshot.
PINNED_BUNDLE_DIGEST: Final = (
    "af630b3ea567ff68ddd4f6a0b3aae236b316d244a80ca9ac2c795c14051f4e77"
)
PINNED_SCHEMA_DIGEST: Final = (
    "9d441e6c6a63a83a8955f981989ab5160da3466b8b2771a5cee1151647a5ff79"
)
PINNED_CATALOG_SNAPSHOT_SHA256: Final = (
    "147da78a9cfa53bcb2f2a61aa3bbce4cb1453bf22855a6f66e028734034fca05"
)
CONSUMED_CATALOG_VERSION_ID: Final = "payment-2026-10-03"


@dataclass(frozen=True)
class ConsumedOffer:
    """What one catalog offer grants, as this consumer pins it (PDF)."""

    aggregate_kind: Literal["subscription_cycle", "topup_order"]
    credit_line_kind: Literal["plan", "topup"]
    credits: int

    @property
    def grant_mc(self) -> int:
        return self.credits * MILLICREDITS_PER_CREDIT


# Three plans (monthly allowance) and four packages (top-ups).
CONSUMED_OFFERS: Final[Mapping[str, ConsumedOffer]] = MappingProxyType(
    {
        "plan_plus_monthly_v1": ConsumedOffer("subscription_cycle", "plan", 5_000),
        "plan_pro_monthly_v1": ConsumedOffer("subscription_cycle", "plan", 15_000),
        "plan_max_monthly_v1": ConsumedOffer("subscription_cycle", "plan", 35_000),
        "package_credits_500_v1": ConsumedOffer("topup_order", "topup", 500),
        "package_credits_1000_v1": ConsumedOffer("topup_order", "topup", 1_000),
        "package_credits_2000_v1": ConsumedOffer("topup_order", "topup", 2_000),
        "package_credits_4000_v1": ConsumedOffer("topup_order", "topup", 4_000),
    }
)
# Lines that may accompany the credit-bearing one, per aggregate.
_COMPANION_LINES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "subscription_cycle": frozenset({"additional_clinic"}),
        "topup_order": frozenset(),
    }
)

FactOutcome = Literal["accepted", "rejected", "stale", "replay"]
RejectionReason = Literal[
    "payload_digest_mismatch",
    "partial_credit_line",
    "ambiguous_line_allocation",
    "fact_violation",
    "outbox_identity_mismatch",
    "catalog_mismatch",
]


# Group 7 deliverable 4: what the inbox took (and which rejected acks were
# written), counted after the commit and only with the switch effective.
# Closed vocabularies; no builder, aggregate or event identity.
CREDIT_FACT_INBOX_TOTAL = precreate_closed_children(
    ContractCounter(
        "neurostack_credit_fact_inbox_total",
        "billing.fact.v1 facts handled by the credit consumer, by outcome",
        ["outcome"],
    )
)
CREDIT_FACT_REJECTED_TOTAL = precreate_closed_children(
    ContractCounter(
        "neurostack_credit_fact_rejected_total",
        "billing.fact.v1 facts acked rejected by the credit consumer, by reason",
        ["reason"],
    )
)


def _count_handling(handling: FactHandling) -> None:
    if not credit_switch_effective():
        return
    CREDIT_FACT_INBOX_TOTAL.labels(outcome=handling.outcome).inc()
    if handling.outcome == "rejected" and handling.reason is not None:
        CREDIT_FACT_REJECTED_TOTAL.labels(reason=handling.reason).inc()


class CreditFactConsumerError(RuntimeError):
    """The consumer could not take the fact; nothing was written.

    The publisher presents the fact again (bounded by its retry policy).
    """


class CreditSwitchOff(CreditFactConsumerError):
    """The credit dominance switch is not effective."""


class ConsumerContractDivergence(CreditFactConsumerError):
    """The consumer's own contract bytes differ from the pins or the handoff
    (or the manifest is unavailable): a deployment problem, not the fact's."""


class UnresolvableFact(CreditFactConsumerError):
    """The presented ``event_id`` names no outbox fact with a handoff."""


class FactCollision(CreditFactConsumerError):
    """The inbox already holds another fact for this identity."""


class CycleIdentityConflict(CreditFactConsumerError):
    """A second settled cycle for one subscription period, or a binding whose
    cycle identity differs from the fact's."""


@dataclass(frozen=True)
class ConsumerDigests:
    """The consumer's own digests of the contract it runs."""

    bundle_digest: str
    schema_digest: str
    catalog_digest: str


@dataclass(frozen=True)
class FactHandling:
    """What one call did (for callers other than the transport, and tests)."""

    outcome: FactOutcome
    event_id: UUID
    effect_ids: tuple[str, ...] = ()
    reason: RejectionReason | None = None
    spend_hold: SpendHold | None = None


@dataclass(frozen=True)
class _CreditLine:
    line_id: str
    grant_mc: int
    settled_cents: int
    reversed_cents: int
    restored_cents: int


@dataclass(frozen=True)
class _Cycle:
    subscription_id: UUID
    cycle_start: date
    cycle_end: date


@dataclass(frozen=True)
class _Fact:
    """The parts of an accepted fact the ledger reads (already validated).

    Exactly one of ``cycle`` (``subscription_cycle``) and ``order_id``
    (``topup_order``) is set; ``paid_at`` is set whenever money settled.
    """

    aggregate_kind: str
    aggregate_id: UUID
    builder_id: UUID
    financial_revision: int
    payment_id: str
    paid_at: date | None
    cycle: _Cycle | None
    order_id: UUID | None
    credit_line: _CreditLine


def consumer_contract_digests() -> ConsumerDigests:
    """Hash the contract bytes this process runs and check them against the
    pins.  Called before ``validate_fact``; every failure is the consumer's."""

    try:
        bundle = billing_fact_bundle_digest()
        schema = billing_fact_schema_digest()
    except BillingFactViolation as error:
        raise ConsumerContractDivergence(
            "the billing fact contract bytes are unavailable"
        ) from error
    catalog = hashlib.sha256(catalog_snapshot_bytes()).hexdigest()
    if bundle != PINNED_BUNDLE_DIGEST:
        raise ConsumerContractDivergence("bundle digest differs from the pin")
    if schema != PINNED_SCHEMA_DIGEST:
        raise ConsumerContractDivergence("schema digest differs from the pin")
    if catalog != PINNED_CATALOG_SNAPSHOT_SHA256:
        raise ConsumerContractDivergence("catalog snapshot differs from the pin")
    return ConsumerDigests(
        bundle_digest=bundle, schema_digest=schema, catalog_digest=catalog
    )


# ---------------------------------------------------------------------------
# Fact checks (a failure here is a problem IN the fact: ``rejected``).
# ---------------------------------------------------------------------------


def _lines(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = payload.get("lines")
    if not isinstance(raw, list):
        return []
    return [
        cast(Mapping[str, object], line) for line in raw if isinstance(line, Mapping)
    ]


def _int(value: object) -> int | None:
    return value if type(value) is int else None


def _violation_reason(
    payload: Mapping[str, object], error: BillingFactViolation
) -> RejectionReason:
    """Name the closed invariant a ``validate_fact`` failure broke."""

    if isinstance(error, PartialCreditLine):
        return "partial_credit_line"
    if isinstance(error, AmbiguousLineAllocation):
        return "ambiguous_line_allocation"
    lines = _lines(payload)
    for line in lines:
        grant = _int(line.get("credit_grant_mc"))
        settled = _int(line.get("settled_cents"))
        amount = _int(line.get("line_amount_cents"))
        if (
            grant is not None
            and settled is not None
            and amount is not None
            and grant > 0
            and 0 < settled < amount
        ):
            return "partial_credit_line"
    for field_name in ("settled_cents", "reversed_cents", "restored_cents"):
        total = _int(payload.get(field_name))
        parts = [_int(line.get(field_name)) for line in lines]
        if total is not None and lines and None not in parts:
            if total != sum(cast(list[int], parts)):
                return "ambiguous_line_allocation"
    return "fact_violation"


def _uuid(value: object) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _date(value: object) -> date | None:
    return date.fromisoformat(value) if isinstance(value, str) else None


def _parse_fact(
    payload: Mapping[str, object], outbox: FactOutboxRow
) -> _Fact | RejectionReason:
    """The validated fact, or why it disagrees with its row or the catalog."""

    kind = payload.get("aggregate_kind")
    aggregate_id = _uuid(payload.get("aggregate_id"))
    builder_id = _uuid(payload.get("builder_id"))
    revision = _int(payload.get("financial_revision"))
    if (
        aggregate_id is None
        or builder_id is None
        or revision is None
        or kind != outbox.aggregate_kind
        or aggregate_id != outbox.aggregate_id
        or builder_id != outbox.builder_id
        or revision != outbox.financial_revision
        or payload.get("schema_version") != outbox.schema_version
    ):
        return "outbox_identity_mismatch"
    cycle: _Cycle | None = None
    order_id: UUID | None = None
    if kind == "subscription_cycle":
        subscription_id = _uuid(payload.get("subscription_id"))
        period = cast(Mapping[str, object], payload["subscription_cycle"])
        cycle_start = _date(period.get("cycle_start"))
        cycle_end = _date(period.get("cycle_end"))
        if (
            subscription_id is None
            or cycle_start is None
            or cycle_end is None
            or outbox.subscription_cycle_id != aggregate_id
            or outbox.subscription_id != subscription_id
        ):
            return "outbox_identity_mismatch"
        cycle = _Cycle(subscription_id, cycle_start, cycle_end)
    else:
        order_id = _uuid(payload.get("order_id"))
        if order_id is None or outbox.topup_order_id != order_id:
            return "outbox_identity_mismatch"
    offer = CONSUMED_OFFERS.get(str(payload.get("offer_id")))
    if (
        payload.get("catalog_version_id") != CONSUMED_CATALOG_VERSION_ID
        or offer is None
        or offer.aggregate_kind != kind
    ):
        return "catalog_mismatch"
    lines = _lines(payload)
    credit_lines = [line for line in lines if line.get("kind") in ("plan", "topup")]
    companions = {
        str(line.get("kind"))
        for line in lines
        if line.get("kind") not in ("plan", "topup")
    }
    if (
        len(credit_lines) != 1
        or credit_lines[0].get("kind") != offer.credit_line_kind
        or credit_lines[0].get("quantity") != 1
        or credit_lines[0].get("credit_grant_mc") != offer.grant_mc
        or not companions <= _COMPANION_LINES[str(kind)]
    ):
        return "catalog_mismatch"
    line = credit_lines[0]
    return _Fact(
        aggregate_kind=str(kind),
        aggregate_id=aggregate_id,
        builder_id=builder_id,
        financial_revision=revision,
        payment_id=str(payload.get("payment_id")),
        paid_at=_date(payload.get("paid_at")),
        cycle=cycle,
        order_id=order_id,
        credit_line=_CreditLine(
            line_id=str(line.get("line_id")),
            grant_mc=int(cast(int, line.get("credit_grant_mc"))),
            settled_cents=int(cast(int, line.get("settled_cents"))),
            reversed_cents=int(cast(int, line.get("reversed_cents"))),
            restored_cents=int(cast(int, line.get("restored_cents"))),
        ),
    )


def check_fact(
    payload: Mapping[str, object],
    *,
    outbox: FactOutboxRow,
    handoff: FactHandoff,
    fact_sha256: str,
) -> _Fact | RejectionReason:
    """Every check whose failure is a problem in the fact itself."""

    if fact_sha256 != handoff.payload_sha256:
        return "payload_digest_mismatch"
    try:
        validate_fact(payload)
    except BillingFactViolation as error:
        return _violation_reason(payload, error)
    return _parse_fact(payload, outbox)


def binding_status(line: _CreditLine) -> Literal["active", "reversed"]:
    """``reversed`` only while the plan line is fully reversed net of
    restorations (cents, never the producer's aggregate ``state``)."""

    if (
        line.settled_cents > 0
        and line.reversed_cents - line.restored_cents >= line.settled_cents
    ):
        return "reversed"
    return "active"


def _event_id(payload: Mapping[str, object]) -> UUID:
    event_id = _uuid(payload.get("event_id"))
    if event_id is None:
        raise UnresolvableFact("the presented fact has no event id")
    return event_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


def default_session_factory() -> Callable[[], AsyncSession]:
    """The process pool's session factory (``src/repositories/database.py``)."""

    from src.repositories.database import get_db_session_factory

    return get_db_session_factory()


# ---------------------------------------------------------------------------
# The consumer.
# ---------------------------------------------------------------------------


class CreditFactConsumer:
    """Applies one presented fact per call in its own session."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession] | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        timeout_seconds: float = TRANSPORT_TIMEOUT_SECONDS,
        clock_tolerance: timedelta | None = None,
        reference_clock: Callable[[], Awaitable[datetime]] | None = None,
    ) -> None:
        """``clock_tolerance`` (default: the setting
        ``credit_clock_tolerance_seconds``) and ``reference_clock`` go to the
        ledger's clock rule (:class:`~src.services.credit_ledger.CreditLedger`):
        the call's instant is brought within the tolerance of the database
        clock (never refused); only a builder whose ledger clock is already
        beyond it raises ``LedgerClockAhead``, without inbox or ack, and the
        publisher presents the fact again."""

        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._session_factory = session_factory
        self._now = now or _now
        self._timeout = timeout_seconds
        self._clock_tolerance = clock_tolerance
        self._reference_clock = reference_clock

    def _ledger(self, session: AsyncSession) -> CreditLedger:
        return CreditLedger(
            session,
            clock_tolerance=self._clock_tolerance,
            reference_clock=self._reference_clock,
        )

    async def handle(self, payload: Mapping[str, object]) -> FactHandling:
        require_presentation_allowed()
        if not credit_switch_effective():
            raise CreditSwitchOff("the credit switch is not effective")
        digests = consumer_contract_digests()
        factory = self._session_factory or default_session_factory()
        async with asyncio.timeout(self._timeout):
            handling = await self._in_own_session(factory, payload, digests)
        _count_handling(handling)
        return handling

    async def _in_own_session(
        self,
        factory: Callable[[], AsyncSession],
        payload: Mapping[str, object],
        digests: ConsumerDigests,
    ) -> FactHandling:
        session = factory()
        connection: AsyncConnection | None = None
        try:
            connection = await session.connection()
            handling = await self._consume(session, payload, digests)
            await session.commit()
            return handling
        except asyncio.CancelledError:
            if connection is not None:
                with suppress(Exception):
                    await connection.invalidate()
            raise
        except BaseException:
            with suppress(Exception):
                await session.rollback()
            raise
        finally:
            with suppress(Exception):
                await session.close()

    async def _consume(
        self,
        session: AsyncSession,
        payload: Mapping[str, object],
        digests: ConsumerDigests,
    ) -> FactHandling:
        repository = CreditRepository(session)
        acks = CreditFactAckRepository(session)
        event_id = _event_id(payload)
        handoff = await acks.resolve_handoff(event_id)
        if handoff is None:
            raise UnresolvableFact("no outbox fact and handoff for this event id")
        outbox = await repository.fact_outbox_row(handoff.outbox_event_id)
        if outbox is None:
            raise UnresolvableFact("the handoff names no outbox row")
        if digests.bundle_digest != handoff.bundle_digest:
            raise ConsumerContractDivergence("bundle digest differs from the handoff")

        # Lock order step 2, then the read-only hold (before any lot lock).
        await repository.ensure_account(outbox.builder_id)
        await repository.lock_account(outbox.builder_id)
        # The clock is read only now -- a reading taken before waiting on the
        # lock could be older than what the previous holder wrote -- and
        # brought within the tolerance of the database clock before any path
        # (rejected, old revision, applied) writes: inbox, ack and ledger
        # rows are never dated beyond database clock + tolerance.
        ledger = self._ledger(session)
        at = await ledger.checked_instant(outbox.builder_id, _aware_now(self._now()))
        hold = await ledger.spend_hold(outbox.builder_id)

        fact_sha256 = payload_digest(payload)
        replayed = await repository.inbox_by_event(
            PRODUCER, event_id, outbox.schema_version
        )
        if replayed is not None:
            if replayed.fact_sha256 != fact_sha256:
                raise FactCollision("this event id was taken with other bytes")
            return FactHandling("replay", event_id, spend_hold=hold)
        taken = await repository.inbox_by_revision(
            PRODUCER,
            outbox.aggregate_kind,
            outbox.aggregate_id,
            outbox.financial_revision,
        )
        if taken is not None:
            raise FactCollision("this aggregate revision holds another fact")

        revisions = await repository.inbox_revisions(
            PRODUCER, outbox.aggregate_kind, outbox.aggregate_id
        )
        checked = check_fact(
            payload, outbox=outbox, handoff=handoff, fact_sha256=fact_sha256
        )
        if not isinstance(checked, _Fact):
            await self._record(
                session,
                outbox,
                handoff,
                event_id,
                fact_sha256,
                digests,
                outcome="rejected",
                effect_ids=(),
                revisions=revisions,
                at=at,
            )
            logger.warning(
                "credit fact rejected",
                reason=checked,
                event_id=str(event_id),
                aggregate_kind=outbox.aggregate_kind,
                financial_revision=outbox.financial_revision,
            )
            return FactHandling("rejected", event_id, reason=checked, spend_hold=hold)

        applied = max(
            (r for r, outcome in revisions.items() if outcome == "accepted"), default=0
        )
        if checked.financial_revision < applied:
            await self._record(
                session,
                outbox,
                handoff,
                event_id,
                fact_sha256,
                digests,
                outcome="accepted",
                effect_ids=(),
                revisions=revisions,
                at=at,
            )
            logger.warning(
                "credit fact older than the applied revision: no-op",
                event_id=str(event_id),
                aggregate_kind=outbox.aggregate_kind,
                financial_revision=checked.financial_revision,
                applied_revision=applied,
            )
            return FactHandling("stale", event_id, spend_hold=hold)

        below = max((r for r in revisions if r < checked.financial_revision), default=0)
        if checked.financial_revision > below + 1:
            await repository.open_revision_gap(
                outbox.aggregate_kind, outbox.aggregate_id, below + 1, at
            )
            logger.warning(
                "credit fact revision gap opened",
                event_id=str(event_id),
                aggregate_kind=outbox.aggregate_kind,
                first_missing_revision=below + 1,
                financial_revision=checked.financial_revision,
            )
        effect_ids = await self._apply(session, checked, fact_sha256, at)
        await self._record(
            session,
            outbox,
            handoff,
            event_id,
            fact_sha256,
            digests,
            outcome="accepted",
            effect_ids=effect_ids,
            revisions=revisions,
            at=at,
        )
        return FactHandling(
            "accepted", event_id, effect_ids=effect_ids, spend_hold=hold
        )

    async def _apply(
        self, session: AsyncSession, fact: _Fact, fact_sha256: str, at: datetime
    ) -> tuple[str, ...]:
        """Grant once, open the cycle, apply the cumulative line targets and
        write/advance the binding -- one transaction, account lock held."""

        repository = CreditRepository(session)
        ledger = self._ledger(session)
        book = await ledger.open_book(fact.builder_id, at=at)
        line = fact.credit_line
        lot: LotState | None = None
        binding: CreditEnforcementBinding | None = None
        new_binding_effect: str | None = None
        if fact.cycle is not None:
            lot, binding, new_binding_effect = await self._open_cycle(
                repository, book, fact, fact.cycle, fact_sha256, at
            )
        elif fact.order_id is not None:
            lot = book.topup_lot(fact.order_id)
            if lot is None and line.settled_cents > 0:
                lot = book.grant(
                    GrantSpec(
                        lot_kind="topup",
                        effect_id=topup_grant_effect_id(fact.order_id),
                        payment_id=fact.payment_id,
                        paid_at=_settled_on(fact),
                        granted_mc=line.grant_mc,
                        expires_at=topup_lot_expires_at(_settled_on(fact)),
                        order_id=fact.order_id,
                    ),
                    at=at,
                    decision_digest=fact_sha256,
                )
        if lot is not None:
            application = book.apply_line_targets(
                lot.id,
                line_id=line.line_id,
                payment_id=fact.payment_id,
                financial_revision=fact.financial_revision,
                reversal_target_mc=cumulative_target_mc(
                    line.grant_mc, line.reversed_cents, line.settled_cents
                ),
                restoration_target_mc=cumulative_target_mc(
                    line.grant_mc, line.restored_cents, line.settled_cents
                ),
                at=at,
                decision_digest=fact_sha256,
            )
            if (
                application.reversal_regressed
                or application.restoration_regressed
                or application.restoration_capped
            ):
                logger.warning(
                    "credit fact cumulative target regressed: not applied",
                    aggregate_kind=fact.aggregate_kind,
                    financial_revision=fact.financial_revision,
                    reversal_regressed=application.reversal_regressed,
                    restoration_regressed=application.restoration_regressed,
                    restoration_capped=application.restoration_capped,
                )
        # The ack names the fact's own effects; expiries the deadline sweep
        # recorded for other lots in this transaction are persisted but are
        # not the fact's.
        effect_ids = book.writes.operation_effect_ids
        await ledger.persist(book)
        if fact.cycle is not None:
            status = binding_status(line)
            if new_binding_effect is not None:
                await repository.insert_binding(
                    builder_id=fact.builder_id,
                    subscription_id=fact.cycle.subscription_id,
                    subscription_cycle_id=fact.aggregate_id,
                    cycle_start=fact.cycle.cycle_start,
                    cycle_end=fact.cycle.cycle_end,
                    financial_revision=fact.financial_revision,
                    grant_effect_id=new_binding_effect,
                    status=status,
                    at=at,
                )
            elif binding is not None:
                await repository.update_binding(
                    binding,
                    financial_revision=fact.financial_revision,
                    status=status,
                    at=at,
                )
        return effect_ids

    async def _open_cycle(
        self,
        repository: CreditRepository,
        book: LedgerBook,
        fact: _Fact,
        cycle: _Cycle,
        fact_sha256: str,
        at: datetime,
    ) -> tuple[LotState | None, CreditEnforcementBinding | None, str | None]:
        """The cycle's lot and binding; on its first settled fact, the cycle
        opening (due expiries already swept by ``open_book``): settle open
        debt with still-valid capacity, then grant and settle the rest."""

        subscription_id = cycle.subscription_id
        cycle_start, cycle_end = cycle.cycle_start, cycle.cycle_end
        binding = await repository.binding_for_cycle(fact.aggregate_id)
        if binding is not None:
            if (
                binding.builder_id != fact.builder_id
                or binding.subscription_id != subscription_id
                or binding.cycle_start != cycle_start
                or binding.cycle_end != cycle_end
            ):
                raise CycleIdentityConflict("the binding names another cycle")
            lot = book.monthly_lot(subscription_id, cycle_start, cycle_end)
            if lot is None:
                raise CycleIdentityConflict("the binding's monthly lot is missing")
            return lot, binding, None
        if (
            await repository.binding_for_period(subscription_id, cycle_start, cycle_end)
            is not None
            or book.monthly_lot(subscription_id, cycle_start, cycle_end) is not None
        ):
            raise CycleIdentityConflict(
                "another settled cycle holds this subscription period"
            )
        if fact.credit_line.settled_cents == 0:
            return None, None, None
        book.settle_open_debts(
            cycle_debt_settlement_effect_id(subscription_id, cycle_start, cycle_end),
            at=at,
            effect_kind=DEBT_SETTLEMENT_EFFECT,
        )
        grant_effect = monthly_grant_effect_id(subscription_id, cycle_start, cycle_end)
        lot = book.grant(
            GrantSpec(
                lot_kind="monthly",
                effect_id=grant_effect,
                payment_id=fact.payment_id,
                paid_at=_settled_on(fact),
                granted_mc=fact.credit_line.grant_mc,
                expires_at=monthly_lot_expires_at(cycle_end),
                subscription_id=subscription_id,
                cycle_start=cycle_start,
                cycle_end=cycle_end,
            ),
            at=at,
            decision_digest=fact_sha256,
        )
        return lot, None, grant_effect

    async def _record(
        self,
        session: AsyncSession,
        outbox: FactOutboxRow,
        handoff: FactHandoff,
        event_id: UUID,
        fact_sha256: str,
        digests: ConsumerDigests,
        *,
        outcome: Literal["accepted", "rejected"],
        effect_ids: tuple[str, ...],
        revisions: Mapping[int, str],
        at: datetime,
    ) -> None:
        """The inbox row (identity from the outbox row), the ack in the same
        transaction, and the gaps this revision closes."""

        repository = CreditRepository(session)
        inbox = await repository.insert_inbox(
            producer=PRODUCER,
            event_id=event_id,
            schema_version=outbox.schema_version,
            aggregate_kind=outbox.aggregate_kind,
            aggregate_id=outbox.aggregate_id,
            financial_revision=outbox.financial_revision,
            fact_sha256=fact_sha256,
            bundle_digest=digests.bundle_digest,
            schema_digest=digests.schema_digest,
            outcome=outcome,
            received_at=at,
        )
        await CreditFactAckRepository(session).insert_ack(
            handoff=handoff,
            outcome=outcome,
            inbox_event_id=inbox.id,
            fact_sha256=fact_sha256,
            bundle_digest=digests.bundle_digest,
            effect_ids=effect_ids,
            acknowledged_at=at,
        )
        present = set(revisions) | {outbox.financial_revision}
        highest = max(present)
        for gap in await repository.open_revision_gaps(
            outbox.aggregate_kind, outbox.aggregate_id
        ):
            missing = [
                revision
                for revision in range(gap.first_missing_revision, highest + 1)
                if revision not in present
            ]
            if not missing:
                await repository.close_revision_gap(gap, at)


def _settled_on(fact: _Fact) -> date:
    """``paid_at`` of a settled fact (``validate_fact`` requires it)."""

    if fact.paid_at is None:
        raise LedgerInvariantError("a settled fact without paid_at passed validation")
    return fact.paid_at


def _aware_now(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("the consumer clock must be timezone-aware")
    return value.astimezone(timezone.utc)


async def handle_billing_fact(payload: dict[str, object]) -> None:
    """``FactTransport`` of the credit consumer (see the module docstring)."""

    await CreditFactConsumer().handle(payload)


__all__ = [
    "CONSUMED_CATALOG_VERSION_ID",
    "CONSUMED_OFFERS",
    "PINNED_BUNDLE_DIGEST",
    "PINNED_CATALOG_SNAPSHOT_SHA256",
    "PINNED_SCHEMA_DIGEST",
    "ConsumedOffer",
    "ConsumerContractDivergence",
    "ConsumerDigests",
    "CreditFactConsumer",
    "CreditFactConsumerError",
    "CreditSwitchOff",
    "CycleIdentityConflict",
    "FactCollision",
    "FactHandling",
    "UnresolvableFact",
    "binding_status",
    "check_fact",
    "consumer_contract_digests",
    "default_session_factory",
    "handle_billing_fact",
]
