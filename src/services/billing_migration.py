"""Fail-closed orchestration for AbacatePay-to-ASAAS subscription migration.

The database triggers introduced with the billing foundation own the two
financially sensitive writes: removing the legacy provider and binding the
ASAAS provider.  This service deliberately performs no provider I/O.  It
re-reads both migration and subscription rows by ID while holding the global
roster fence, so an operator never advances a saga from a stale revision.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Final, Literal, Mapping, NoReturn, cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.billing import (
    BillingCheckoutSession,
    BillingMigration,
    BillingMigrationAbsenceRead,
    BillingMigrationReceipt,
    BillingSubscription,
)
from src.observability.metrics import BILLING_MIGRATION_OPERATIONS_TOTAL
from src.repositories.billing import (
    BillingRepository,
    BillingRevisionConflict,
    BillingStateUnavailable,
    BillingTransitionRejected,
)

_LOGGER = logging.getLogger(__name__)

MIGRATION_TIMEZONE: Final = ZoneInfo("America/Sao_Paulo")
# Stable rejection codes for an attempt that missed its São Paulo deadline.
MIGRATION_OPEN_LATE: Final = "billing_migration_open_late"
MIGRATION_CUTOVER_LATE: Final = "billing_migration_cutover_late"
# Absence evidence policy.  These mirror ``billing_checkout``'s
# ``CHECKOUT_CONVERGENCE_LIMIT`` (Checkout expiry 60 min + 30 min) and
# ``CONVERGENCE_READ_SPACING``; migration 032 refuses anything earlier at the
# database.  Kept local so this module never imports settings at import time.
ABSENCE_CONVERGENCE_LIMIT: Final = timedelta(minutes=90)
ABSENCE_READ_SPACING: Final = timedelta(minutes=5)
PROVIDER_EVIDENCE_MAX_AGE: Final = timedelta(minutes=15)
LIVE_MIGRATION_CHECKOUT_STATES: Final = frozenset(
    {
        "creating",
        "active",
        "paid_pending_subscription",
        "reconciliation_required",
        "terminal_pending_readback",
    }
)
_SNAPSHOT_KEYS: Final = frozenset(
    {
        "catalog_version_id",
        "offer_id",
        "plan",
        "billing_cycle",
        "coupon_code",
        "amount_cents",
        "credit_grant_mc",
        "included_clinics",
        "additional_clinics",
        "lines",
    }
)
_PII_KEYS: Final = frozenset(
    {
        "address",
        "cnpj",
        "customer",
        "document",
        "document_number",
        "email",
        "name",
        "phone",
    }
)
_PROVIDER_ID: Final = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_PROVIDER_EVENT_ID: Final = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_PROVIDER_KEY_ID: Final = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
_PROVIDER_AUTHORITY: Final = "asaas.api.v3"
PROVIDER_FLIP_OPERATION: Final = "subscription.migration.provider_flip"
PROVIDER_ABSENCE_OPERATION: Final = "subscription.migration.absence"
_PROVIDER_OPERATIONS: Final = frozenset(
    {PROVIDER_FLIP_OPERATION, PROVIDER_ABSENCE_OPERATION}
)

MigrationState = Literal[
    "pending",
    "in_progress",
    "legacy_cancel_pending",
    "legacy_cancelled",
    "recreate_pending",
    "migrated",
    "manual_intervention",
]


class BillingMigrationRejected(RuntimeError):
    """A fixed code safe to return from an administrative endpoint."""

    code = "billing_migration_rejected"

    def __init__(self, code: str | None = None, *, reason: str | None = None) -> None:
        self.code = code or self.code
        # ``reason`` names the exact guard for logs/tests; only ``code`` is
        # ever returned to an administrative client.
        self.reason = reason
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class MigrationWindow:
    """T, T-7 and T-2 on São Paulo's commercial calendar."""

    renewal_date: date
    open_by_date: date
    cutover_by_date: date


@dataclass(frozen=True, slots=True)
class MigrationTarget:
    """Closed target values preserved through retries and recreation."""

    catalog_version_id: str
    offer_id: str
    plan: str
    coupon_code: str | None
    amount_cents: int
    snapshot: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class MigrationView:
    """Safe operational view; it never includes customer or secret material."""

    migration_id: UUID
    subscription_id: UUID
    state: MigrationState
    attempt_count: int
    source_roster_revision: int
    renewal_date: date
    open_by_date: date
    cutover_by_date: date


def migration_window(renewal: date | datetime) -> MigrationWindow:
    """Calculate the fixed T/T-7/T-2 cutoffs in America/Sao_Paulo.

    A bare ``date`` is already a calendar value.  A timestamp must be aware;
    accepting host-local timestamps would silently move a payment window.
    """

    if isinstance(renewal, datetime):
        if renewal.tzinfo is None or renewal.utcoffset() is None:
            raise BillingMigrationRejected("billing_migration_renewal_invalid")
        renewal_date = renewal.astimezone(MIGRATION_TIMEZONE).date()
    elif isinstance(renewal, date):
        renewal_date = renewal
    else:
        raise BillingMigrationRejected("billing_migration_renewal_invalid")
    return MigrationWindow(
        renewal_date=renewal_date,
        open_by_date=renewal_date - timedelta(days=7),
        cutover_by_date=renewal_date - timedelta(days=2),
    )


def migration_local_date(now: datetime) -> date:
    """Return the São Paulo calendar date of an aware instant."""

    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise BillingMigrationRejected("billing_migration_clock_invalid")
    return now.astimezone(MIGRATION_TIMEZONE).date()


def migration_deadline_passed(now: datetime, deadline: date) -> bool:
    """True once ``now`` is after 23:59:59.999999 local of ``deadline``.

    Deadlines (``T-7`` opening and ``T-2`` cutover) are inclusive through the
    whole São Paulo day; the next local day is late regardless of the UTC date
    or of the timezone the caller's clock happens to carry.
    """

    return migration_local_date(now) > deadline


def _aware_utc(value: datetime) -> datetime:
    """Treat a naive database timestamp (SQLite only) as UTC."""

    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _optional_aware(value: datetime | None) -> datetime | None:
    return None if value is None else _aware_utc(value)


@dataclass(frozen=True, slots=True)
class ProviderEvidenceEnvelope:
    """One provider-signed ASAAS observation exactly as the authority signed it.

    ``external_reference`` is the observed ASAAS subscription ID (provider
    flip) or the persisted lookup reference (absence read).  Every field is
    covered by ``provider_evidence_digest`` and that digest by the Ed25519
    signature, so no value here is a caller assertion once verified.
    """

    external_reference: str
    provider_event_id: str
    provider_key_id: str
    provider_operation: str
    provider_signature: str
    provider_observed_at: datetime
    provider_expires_at: datetime
    provider_snapshot_digest: str
    provider_evidence_digest: str
    provider: str = "asaas"
    provider_authority: str = "asaas.api.v3"


@dataclass(frozen=True, slots=True)
class AbsenceReadView:
    """Safe result of recording one empty ASAAS read."""

    read_id: UUID
    read_no: int
    prior_checkout_session_id: UUID | None
    convergence_limit_at: datetime
    authorization_receipt_id: UUID | None


def _contains_prohibited_key(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str) or key.lower() in _PII_KEYS:
                return True
            if _contains_prohibited_key(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_prohibited_key(item) for item in value)
    return False


def validate_target(target: MigrationTarget) -> dict[str, object]:
    """Validate and copy the immutable non-PII target snapshot."""

    if (
        not target.catalog_version_id
        or not target.offer_id
        or not target.plan
        or type(target.amount_cents) is not int
        or target.amount_cents < 0
        or (target.coupon_code is not None and not target.coupon_code)
    ):
        raise BillingMigrationRejected("billing_migration_target_invalid")
    snapshot = dict(target.snapshot)
    if set(snapshot) != _SNAPSHOT_KEYS or _contains_prohibited_key(snapshot):
        raise BillingMigrationRejected("billing_migration_snapshot_invalid")
    if (
        snapshot.get("catalog_version_id") != target.catalog_version_id
        or snapshot.get("offer_id") != target.offer_id
        or snapshot.get("plan") != target.plan
        or snapshot.get("billing_cycle") != "monthly"
        or snapshot.get("coupon_code") != target.coupon_code
        or snapshot.get("amount_cents") != target.amount_cents
        or not isinstance(snapshot.get("lines"), list)
    ):
        raise BillingMigrationRejected("billing_migration_snapshot_invalid")
    # JSON round-tripping rejects UUIDs, Decimals, and other accidental
    # process objects before SQLAlchemy can serialize them differently.
    try:
        json.dumps(snapshot, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise BillingMigrationRejected("billing_migration_snapshot_invalid") from exc
    return snapshot


def checkout_pricing_snapshot(target_snapshot: Mapping[str, object]) -> dict[str, object]:
    """Project the migration target onto the checkout snapshot allowlist.

    ``billing_checkout_sessions`` (Group 1 CHECK
    ``ck_billing_checkout_sessions_snapshot_allowlist``) has no ``plan`` key:
    the plan is implied by ``offer_id`` and stays on the migration row.  Every
    other value, including ``lines`` and the clinic counts, is copied as is.
    """

    return {key: value for key, value in target_snapshot.items() if key != "plan"}


def _simple_receipt_digest(values: Mapping[str, object]) -> str:
    """Hash a local non-sensitive receipt for non-provider observations."""

    encoded = json.dumps(
        values, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def provider_snapshot_digest(migration: BillingMigration) -> str:
    """Digest the local target only as a binding expected by provider evidence.

    This digest is never accepted as provider evidence.  The provider receipt
    carries it as one field in its signed envelope, so a receipt for another
    target cannot authorize the local saga.
    """

    return _simple_receipt_digest(
        {
            "migration_id": str(migration.id),
            "builder_id": str(migration.builder_id),
            "subscription_id": str(migration.subscription_id),
            "operation_id": str(migration.operation_id),
            "source_roster_revision": migration.source_roster_revision,
            "target_provider": migration.target_provider,
            "target_external_subscription_id": migration.target_external_subscription_id,
            "target_catalog_version_id": migration.target_catalog_version_id,
            "target_offer_id": migration.target_offer_id,
            "target_plan": migration.target_plan,
            "target_billing_cycle": migration.target_billing_cycle,
            "target_coupon_code": migration.target_coupon_code,
            "target_amount_cents": migration.target_amount_cents,
            "target_snapshot": migration.target_snapshot,
        }
    )


def provider_evidence_digest(
    *,
    migration_id: UUID,
    operation_id: UUID,
    provider: str,
    external_reference: str,
    provider_event_id: str,
    provider_authority: str,
    provider_snapshot_digest: str,
    provider_observed_at: datetime,
    provider_expires_at: datetime,
    provider_operation: str = PROVIDER_FLIP_OPERATION,
) -> str:
    """Hash the closed, deterministic provider evidence envelope."""

    if (
        provider_observed_at.tzinfo is None
        or provider_expires_at.tzinfo is None
        or provider_operation not in _PROVIDER_OPERATIONS
    ):
        raise BillingMigrationRejected("billing_migration_receipt_invalid")
    return _simple_receipt_digest(
        {
            "authority": provider_authority,
            "event_id": provider_event_id,
            "expires_at": provider_expires_at.astimezone(timezone.utc).isoformat(),
            "external_reference": external_reference,
            "migration_id": str(migration_id),
            "observed_at": provider_observed_at.astimezone(timezone.utc).isoformat(),
            "operation_id": str(operation_id),
            "provider": provider,
            "operation": provider_operation,
            "snapshot_digest": provider_snapshot_digest,
        }
    )


def provider_evidence_signature(
    *, provider_authority: str, provider_digest: str, signing_key: bytes
) -> str:
    """Sign a provider digest for deterministic non-production fixtures.

    The migration service never has this private key.  It is intentionally a
    required argument so a local caller cannot manufacture provider authority
    by hashing an envelope with a public/local value.
    """

    if (
        provider_authority != _PROVIDER_AUTHORITY
        or not re.fullmatch(r"[0-9a-f]{64}", provider_digest)
        or not isinstance(signing_key, bytes)
        or len(signing_key) != 32
    ):
        raise BillingMigrationRejected("billing_migration_receipt_invalid")
    return Ed25519PrivateKey.from_private_bytes(signing_key).sign(
        provider_digest.encode("ascii")
    ).hex()


def _provider_public_key(value: bytes | str | None) -> Ed25519PublicKey:
    if value is None:
        try:
            from src.config.settings import settings

            value = settings.asaas_provider_evidence_public_key
        except (AttributeError, ImportError) as exc:
            raise BillingMigrationRejected(
                "billing_migration_provider_key_unavailable"
            ) from exc
    if isinstance(value, str):
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise BillingMigrationRejected("billing_migration_provider_key_invalid")
        value = bytes.fromhex(value)
    if not isinstance(value, bytes) or len(value) != 32:
        raise BillingMigrationRejected("billing_migration_provider_key_invalid")
    try:
        return Ed25519PublicKey.from_public_bytes(value)
    except ValueError as exc:
        raise BillingMigrationRejected(
            "billing_migration_provider_key_invalid"
        ) from exc


def verify_provider_evidence_signature(
    *,
    provider_authority: str,
    provider_digest: str,
    provider_signature: str,
    provider_public_key: bytes | str | None = None,
) -> None:
    """Verify the provider's asymmetric signature over the exact digest."""

    if (
        provider_authority != _PROVIDER_AUTHORITY
        or not re.fullmatch(r"[0-9a-f]{64}", provider_digest)
        or not re.fullmatch(r"[0-9a-f]{128}", provider_signature)
    ):
        raise BillingMigrationRejected(
            "billing_migration_readback_unverified", reason="signature"
        )
    try:
        _provider_public_key(provider_public_key).verify(
            bytes.fromhex(provider_signature), provider_digest.encode("ascii")
        )
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise BillingMigrationRejected(
            "billing_migration_readback_unverified", reason="signature"
        ) from exc


def _configured_provider_evidence_authority() -> tuple[str, str]:
    try:
        from src.config.settings import settings

        key_id = settings.asaas_provider_evidence_key_id
        public_key = settings.asaas_provider_evidence_public_key
    except (AttributeError, ImportError) as exc:
        raise BillingMigrationRejected(
            "billing_migration_provider_key_unavailable"
        ) from exc
    if not _PROVIDER_KEY_ID.fullmatch(key_id) or not public_key:
        raise BillingMigrationRejected("billing_migration_provider_key_unavailable")
    return key_id, public_key


def _evidence_rejected(reason: str) -> NoReturn:
    raise BillingMigrationRejected(
        "billing_migration_readback_unverified", reason=reason
    )


def validate_provider_evidence_envelope(
    *,
    migration_id: UUID,
    operation_id: UUID,
    expected_external_reference: str,
    expected_snapshot_digest: str,
    migration_created_at: datetime,
    provider: str | None,
    external_reference: str | None,
    provider_event_id: str | None,
    provider_authority: str | None,
    provider_key_id: str | None,
    provider_operation: str | None,
    provider_signature: str | None,
    provider_observed_at: datetime | None,
    provider_expires_at: datetime | None,
    provider_snapshot_digest: str | None,
    provider_evidence_digest_value: str | None,
    observed_at: datetime,
    now: datetime,
    provider_public_key: bytes | str | None = None,
    expected_provider_key_id: str | None = None,
    expected_provider_operation: str | None = None,
    max_age: timedelta | None = None,
) -> None:
    """Validate a provider-signed envelope without reading local state as proof.

    Every failure has the public code ``billing_migration_readback_unverified``
    and a ``reason`` naming the guard that rejected it.
    """

    if provider != "asaas":
        _evidence_rejected("provider")
    if external_reference != expected_external_reference:
        _evidence_rejected("external_reference")
    if provider_event_id is None or not _PROVIDER_EVENT_ID.fullmatch(provider_event_id):
        _evidence_rejected("event_id")
    if provider_authority != _PROVIDER_AUTHORITY:
        _evidence_rejected("authority")
    if (
        provider_key_id is None
        or not _PROVIDER_KEY_ID.fullmatch(provider_key_id)
        or (
            expected_provider_key_id is not None
            and provider_key_id != expected_provider_key_id
        )
    ):
        _evidence_rejected("key_id")
    if provider_operation not in _PROVIDER_OPERATIONS or (
        expected_provider_operation is not None
        and provider_operation != expected_provider_operation
    ):
        _evidence_rejected("operation")
    if provider_signature is None:
        _evidence_rejected("signature")
    if (
        provider_observed_at is None
        or provider_expires_at is None
        or migration_created_at.tzinfo is None
        or observed_at.tzinfo is None
        or now.tzinfo is None
        or provider_observed_at.tzinfo is None
        or provider_expires_at.tzinfo is None
    ):
        _evidence_rejected("time")
    if (
        provider_observed_at < migration_created_at
        or provider_observed_at > observed_at
        or observed_at > now
    ):
        _evidence_rejected("observed_at")
    if max_age is not None and now - provider_observed_at > max_age:
        _evidence_rejected("stale")
    if provider_expires_at <= provider_observed_at or provider_expires_at <= now:
        _evidence_rejected("expired")
    if (
        provider_snapshot_digest is None
        or provider_snapshot_digest != expected_snapshot_digest
    ):
        _evidence_rejected("snapshot_digest")
    if provider_evidence_digest_value is None or (
        provider_evidence_digest_value
        != provider_evidence_digest(
            migration_id=migration_id,
            operation_id=operation_id,
            provider=provider,
            external_reference=external_reference,
            provider_event_id=provider_event_id,
            provider_authority=provider_authority,
            provider_operation=provider_operation,
            provider_snapshot_digest=provider_snapshot_digest,
            provider_observed_at=provider_observed_at,
            provider_expires_at=provider_expires_at,
        )
    ):
        _evidence_rejected("evidence_digest")
    verify_provider_evidence_signature(
        provider_authority=provider_authority,
        provider_digest=provider_evidence_digest_value,
        provider_signature=provider_signature,
        provider_public_key=provider_public_key,
    )


class BillingMigrationService:
    """Roster-fenced local saga transitions; it never talks to a provider."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._repository = BillingRepository(session)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _view(migration: BillingMigration) -> MigrationView:
        return MigrationView(
            migration_id=migration.id,
            subscription_id=migration.subscription_id,
            state=cast(MigrationState, migration.state),
            attempt_count=migration.attempt_count,
            source_roster_revision=migration.source_roster_revision,
            renewal_date=migration.renewal_date,
            open_by_date=migration.open_by_date,
            cutover_by_date=migration.cutover_by_date,
        )

    @staticmethod
    def _record_operation(operation: str, outcome: str = "accepted") -> None:
        """Emit a bounded local control-plane event without identity labels."""

        BILLING_MIGRATION_OPERATIONS_TOTAL.labels(
            operation=operation, outcome=outcome
        ).inc()

    def _reject_if_late(self, *, operation: str, deadline: date, code: str) -> None:
        """Refuse an attempt that missed its inclusive São Paulo deadline.

        Called under the roster/row locks and before any mutation, so a late
        attempt leaves the legacy binding, the roster revision and the saga
        operation untouched.  The rejection uses the existing bounded metric
        (``outcome="rejected"``) and a log line without identities.
        """

        if migration_deadline_passed(self._clock(), deadline):
            self._record_operation(operation, outcome="rejected")
            _LOGGER.warning(
                "billing migration attempt rejected after its deadline",
                extra={"billing_operation": operation, "billing_reason": code},
            )
            raise BillingMigrationRejected(code)

    @staticmethod
    def _provider_reference(migration: BillingMigration) -> str:
        """Return the exact persisted/provider-intent lookup reference."""

        return migration.target_external_subscription_id or f"migration:{migration.id}"

    async def _ensure_provider_delete_receipt(
        self,
        *,
        migration: BillingMigration,
        actor_user_id: UUID,
        provider: str | None,
        external_reference: str | None,
        content: Mapping[str, object],
        observed_at: datetime,
    ) -> BillingMigrationReceipt:
        """Persist one immutable quarantine receipt for the migration operation.

        The roster and migration locks held by every caller serialize this
        lookup with the other saga writers.  A replay therefore returns the
        first immutable receipt instead of attempting a second insert into
        ``(migration_id, operation_id, receipt_kind)``.  Migrations that have
        not acquired an operation yet acquire one here and keep it on the
        migration, so later quarantine writers use the same idempotency key.
        """

        receipt_query = select(BillingMigrationReceipt).where(
            BillingMigrationReceipt.builder_id == migration.builder_id,
            BillingMigrationReceipt.migration_id == migration.id,
            BillingMigrationReceipt.receipt_kind == "provider_delete",
        )
        if migration.operation_id is not None:
            receipt_query = receipt_query.where(
                BillingMigrationReceipt.operation_id == migration.operation_id
            )
        receipt = await self._session.scalar(
            receipt_query.order_by(BillingMigrationReceipt.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        if receipt is not None:
            migration.operation_id = receipt.operation_id
            return receipt

        operation_id = migration.operation_id or uuid4()
        migration.operation_id = operation_id
        receipt = BillingMigrationReceipt(
            builder_id=migration.builder_id,
            migration_id=migration.id,
            operation_id=operation_id,
            receipt_kind="provider_delete",
            provider=provider,
            external_reference=external_reference,
            status="inconclusive",
            content_sha256=_simple_receipt_digest(content),
            actor_user_id=actor_user_id,
            observed_at=observed_at,
        )
        self._session.add(receipt)
        await self._session.flush()
        return receipt

    async def _locked_pair(
        self, migration_id: UUID
    ) -> tuple[int, BillingMigration, BillingSubscription]:
        """Re-read both IDs after acquiring the global roster fence."""

        roster_revision = await self._repository.lock_billing_roster()
        migration_result = await self._session.execute(
            select(BillingMigration)
            .where(BillingMigration.id == migration_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        migration = migration_result.scalar_one_or_none()
        if migration is None:
            raise BillingMigrationRejected("billing_migration_not_found")
        subscription = await self._repository.lock_subscription(
            builder_id=migration.builder_id, subscription_id=migration.subscription_id
        )
        if subscription is None:
            raise BillingMigrationRejected("billing_migration_subscription_missing")
        return roster_revision, migration, subscription

    @staticmethod
    def _require_current(
        roster_revision: int,
        migration: BillingMigration,
        subscription: BillingSubscription,
    ) -> None:
        if (
            migration.source_roster_revision != roster_revision
            or subscription.roster_revision != roster_revision
        ):
            raise BillingRevisionConflict("billing migration roster revision changed")

    async def import_roster_entry(
        self,
        *,
        builder_id: UUID,
        subscription_id: UUID,
        renewal: date | datetime,
        target: MigrationTarget,
    ) -> MigrationView:
        """Adopt one canonical legacy roster row without provider contact."""

        snapshot = validate_target(target)
        window = migration_window(renewal)
        roster_revision = await self._repository.lock_billing_roster()
        subscription = await self._repository.lock_subscription(
            builder_id=builder_id, subscription_id=subscription_id
        )
        if (
            subscription is None
            or subscription.provider != "abacatepay"
            or not subscription.is_active
            or subscription.roster_revision != roster_revision
        ):
            raise BillingMigrationRejected("billing_migration_roster_unavailable")
        existing = await self._session.scalar(
            select(BillingMigration)
            .where(
                BillingMigration.builder_id == builder_id,
                BillingMigration.subscription_id == subscription_id,
            )
            .with_for_update()
        )
        if existing is not None:
            return self._view(existing)
        # A row whose T-7 opening deadline already passed is late: adopting
        # it could only start an attempt that cannot open in time.
        self._reject_if_late(
            operation="roster_import",
            deadline=window.open_by_date,
            code=MIGRATION_OPEN_LATE,
        )
        migration = BillingMigration(
            builder_id=builder_id,
            subscription_id=subscription_id,
            state="pending",
            renewal_date=window.renewal_date,
            open_by_date=window.open_by_date,
            cutover_by_date=window.cutover_by_date,
            target_provider="asaas",
            target_catalog_version_id=target.catalog_version_id,
            target_offer_id=target.offer_id,
            target_plan=target.plan,
            target_billing_cycle="monthly",
            target_coupon_code=target.coupon_code,
            target_amount_cents=target.amount_cents,
            target_snapshot=snapshot,
            source_roster_revision=roster_revision,
        )
        self._session.add(migration)
        await self._session.flush()
        self._record_operation("roster_import")
        return self._view(migration)

    async def begin_legacy_cancel(
        self, *, migration_id: UUID, operation_id: UUID
    ) -> MigrationView:
        """Begin cancellation locally and make the provider intentionally unknown.

        No cancellation request is sent here.  The legacy binding becomes
        ``provider=NULL`` before any later receipt can authorize the flip.
        The database trigger advances the roster revision atomically.
        """

        roster_revision, migration, subscription = await self._locked_pair(migration_id)
        self._require_current(roster_revision, migration, subscription)
        # DESIGN: cutover is allowed through 23:59:59 local of T-2; a later
        # attempt is late and must never cancel the legacy subscription.
        self._reject_if_late(
            operation="cancel_begin",
            deadline=migration.cutover_by_date,
            code=MIGRATION_CUTOVER_LATE,
        )
        if migration.state not in {"pending", "in_progress"}:
            raise BillingMigrationRejected("billing_migration_cancel_not_allowed")
        if migration.operation_id not in {None, operation_id}:
            raise BillingMigrationRejected("billing_migration_operation_conflict")
        if (
            subscription.provider != "abacatepay"
            or not subscription.is_active
            or subscription.status
            not in {"active", "active_linked", "readback_required"}
        ):
            raise BillingMigrationRejected("billing_migration_legacy_binding_invalid")
        migration.operation_id = operation_id
        migration.state = "in_progress"
        subscription.provider = None
        subscription.is_active = False
        subscription.status = "legacy_cancel_pending"
        subscription.roster_revision = roster_revision + 1
        await self._session.flush()
        # The subscription trigger owns legacy_cancel_pending/revision.
        await self._session.refresh(migration)
        self._record_operation("cancel_begin")
        return self._view(migration)

    async def _provider_digest(
        self,
        *,
        migration_id: UUID,
        receipt_kind: str,
        observed_at: datetime,
        target_external_subscription_id: str | None = None,
    ) -> str:
        """Ask PostgreSQL for the exact digest enforced by the saga trigger.

        For ``provider_flip`` an explicit ``target_external_subscription_id``
        computes the digest of the target *as it will be bound* by the flip,
        which is what the provider signs; the row itself is not changed.
        """

        dialect = self._session.get_bind().dialect.name
        if dialect != "postgresql":
            # SQLite is used by local unit tests.  Keep its evidence digest
            # equivalent to PostgreSQL's canonical JSONB digest so a test
            # cannot accidentally exercise a weaker local-only authority path.
            migration = await self._session.scalar(
                select(BillingMigration).where(BillingMigration.id == migration_id)
            )
            if migration is None:
                raise BillingStateUnavailable(
                    "billing migration receipt digest unavailable"
                )
            if receipt_kind == "provider_flip":
                return _simple_receipt_digest(
                    {
                        "migration_id": str(migration.id),
                        "builder_id": str(migration.builder_id),
                        "subscription_id": str(migration.subscription_id),
                        "operation_id": str(migration.operation_id),
                        "source_roster_revision": migration.source_roster_revision,
                        "target_provider": migration.target_provider,
                        "target_external_subscription_id": (
                            target_external_subscription_id
                            or migration.target_external_subscription_id
                        ),
                        "target_catalog_version_id": migration.target_catalog_version_id,
                        "target_offer_id": migration.target_offer_id,
                        "target_plan": migration.target_plan,
                        "target_billing_cycle": migration.target_billing_cycle,
                        "target_coupon_code": migration.target_coupon_code,
                        "target_amount_cents": migration.target_amount_cents,
                        "target_snapshot": migration.target_snapshot,
                    }
                )
            return _simple_receipt_digest(
                {
                    "migration_id": str(migration_id),
                    "receipt_kind": receipt_kind,
                    "observed_at": observed_at.isoformat(),
                }
            )
        if receipt_kind == "cancel_observation":
            statement = text("""
                SELECT encode(sha256(convert_to(jsonb_build_object(
                    'migration_id', migration.id,
                    'builder_id', migration.builder_id,
                    'subscription_id', migration.subscription_id,
                    'operation_id', migration.operation_id,
                    'legacy_external_subscription_id', subscription.external_subscription_id,
                    'provider', 'abacatepay', 'status', 'cancelled',
                    'observed_at', CAST(:observed_at AS TIMESTAMPTZ)
                )::text, 'UTF8')), 'hex')
                FROM billing_migrations AS migration
                JOIN billing_subscriptions AS subscription
                  ON subscription.id = migration.subscription_id
                 AND subscription.builder_id = migration.builder_id
                WHERE migration.id = :migration_id
                """)
        elif receipt_kind == "provider_flip":
            statement = text("""
                SELECT encode(sha256(convert_to(jsonb_build_object(
                    'migration_id', migration.id,
                    'builder_id', migration.builder_id,
                    'subscription_id', migration.subscription_id,
                    'operation_id', migration.operation_id,
                    'source_roster_revision', migration.source_roster_revision,
                    'target_provider', migration.target_provider,
                    'target_external_subscription_id', COALESCE(
                        CAST(:target_external_subscription_id AS TEXT),
                        migration.target_external_subscription_id),
                    'target_catalog_version_id', migration.target_catalog_version_id,
                    'target_offer_id', migration.target_offer_id,
                    'target_plan', migration.target_plan,
                    'target_billing_cycle', migration.target_billing_cycle,
                    'target_coupon_code', migration.target_coupon_code,
                    'target_amount_cents', migration.target_amount_cents,
                    'target_snapshot', migration.target_snapshot
                )::text, 'UTF8')), 'hex')
                FROM billing_migrations AS migration
                WHERE migration.id = :migration_id
                """)
        else:
            raise BillingMigrationRejected("billing_migration_receipt_invalid")
        parameters: dict[str, object] = {
            "migration_id": migration_id,
            "observed_at": observed_at,
        }
        if receipt_kind == "provider_flip":
            parameters["target_external_subscription_id"] = (
                target_external_subscription_id
            )
        digest = await self._session.scalar(statement, parameters)
        if not isinstance(digest, str) or len(digest) != 64:
            raise BillingStateUnavailable(
                "billing migration receipt digest unavailable"
            )
        return digest

    async def reconcile_legacy_cancel(
        self,
        *,
        migration_id: UUID,
        actor_user_id: UUID,
        cancelled: bool,
        observed_at: datetime | None = None,
    ) -> MigrationView:
        """Record a fresh panel observation; unresolved means intervention.

        A negative or malformed observation never restores AbacatePay and never
        creates ASAAS state.  It retains ``provider=NULL`` and sends the saga
        to the manual lane.

        A ``cancelled`` observation is persisted as the immutable
        ``cancel_observation`` receipt for the exact legacy external ID and
        operation, in the same transaction as the subscription transition.
        That is the Group 1 database contract: the receipt trigger recomputes
        the digest, and the deferred commit trigger refuses a cancellation
        receipt whose transaction does not converge the saga to
        ``legacy_cancelled`` with ``legacy_cancelled_at = observed_at``.
        Separation of duties is enforced by the route's migration quorum.
        """

        now = self._clock()
        observed = observed_at or now
        if (
            observed.tzinfo is None
            or observed.utcoffset() is None
            or now.tzinfo is None
            or observed > now
            or now - observed > PROVIDER_EVIDENCE_MAX_AGE
        ):
            raise BillingMigrationRejected("billing_migration_receipt_invalid")
        roster_revision, migration, subscription = await self._locked_pair(migration_id)
        self._require_current(roster_revision, migration, subscription)
        if (
            migration.state != "legacy_cancel_pending"
            or migration.operation_id is None
            or subscription.provider is not None
            or subscription.status != "legacy_cancel_pending"
        ):
            raise BillingMigrationRejected("billing_migration_reconcile_not_allowed")
        if not cancelled:
            migration.state = "manual_intervention"
            subscription.status = "manual_intervention"
            await self._ensure_provider_delete_receipt(
                migration=migration,
                actor_user_id=actor_user_id,
                provider="abacatepay",
                external_reference=subscription.external_subscription_id,
                content={
                    "migration_id": str(migration.id),
                    "operation_id": str(migration.operation_id),
                    "state": "inconclusive",
                },
                observed_at=observed,
            )
            self._record_operation("cancel_reconcile")
            return self._view(migration)

        if (
            not _PROVIDER_ID.fullmatch(subscription.external_subscription_id)
            or observed < _aware_utc(migration.created_at)
        ):
            raise BillingMigrationRejected("billing_migration_receipt_invalid")
        self._session.add(
            BillingMigrationReceipt(
                builder_id=migration.builder_id,
                migration_id=migration.id,
                operation_id=migration.operation_id,
                receipt_kind="cancel_observation",
                provider="abacatepay",
                external_reference=subscription.external_subscription_id,
                status="cancelled",
                content_sha256=await self._provider_digest(
                    migration_id=migration.id,
                    receipt_kind="cancel_observation",
                    observed_at=observed,
                ),
                actor_user_id=actor_user_id,
                observed_at=observed,
            )
        )
        await self._session.flush()
        # The subscription trigger finds the exact receipt above and owns the
        # migration transition to legacy_cancelled.
        subscription.status = "legacy_cancelled"
        await self._session.flush()
        await self._session.refresh(migration)
        self._record_operation("cancel_reconcile")
        return self._view(migration)

    async def mark_recreate_pending(
        self, *, migration_id: UUID, actor_user_id: UUID
    ) -> MigrationView:
        """Quarantine an unresolved recreation without assuming provider state."""

        roster_revision, migration, subscription = await self._locked_pair(migration_id)
        self._require_current(roster_revision, migration, subscription)
        if (
            migration.state != "legacy_cancelled"
            or subscription.provider is not None
            or subscription.status != "legacy_cancelled"
            or migration.operation_id is None
        ):
            raise BillingMigrationRejected("billing_migration_recreate_not_allowed")
        migration.state = "recreate_pending"
        await self._ensure_provider_delete_receipt(
            migration=migration,
            actor_user_id=actor_user_id,
            provider=None,
            external_reference=None,
            content={
                "migration_id": str(migration.id),
                "operation_id": str(migration.operation_id),
                "state": "recreate_pending",
            },
            observed_at=self._clock(),
        )
        self._record_operation("recreate_pending")
        return self._view(migration)

    async def reserve_recreate_attempt(
        self,
        *,
        migration_id: UUID,
        environment: str,
        absence_receipt_id: UUID | None = None,
    ) -> BillingCheckoutSession:
        """Reserve attempt N locally; caller must commit before any future I/O.

        The target snapshot and T cutoffs are copied from the migration, never
        recalculated from a mutable catalog or host timezone.
        """

        roster_revision, migration, subscription = await self._locked_pair(migration_id)
        self._require_current(roster_revision, migration, subscription)
        self._reject_if_late(
            operation="recreate_reserve",
            deadline=migration.cutover_by_date,
            code=MIGRATION_CUTOVER_LATE,
        )
        if (
            migration.state != "legacy_cancelled"
            or subscription.provider is not None
            or subscription.status != "legacy_cancelled"
            or migration.attempt_count != 0
        ):
            raise BillingMigrationRejected("billing_migration_recreate_not_allowed")
        if absence_receipt_id is None:
            raise BillingMigrationRejected("billing_migration_readback_required")
        await self._validate_recreate_absence_receipt(
            migration=migration,
            receipt_id=absence_receipt_id,
        )
        if await self._live_attempt_exists(migration):
            raise BillingMigrationRejected("billing_migration_attempt_unresolved")
        checkout = await self._repository.reserve_general_subscription_checkout(
            environment=environment,
            builder_id=migration.builder_id,
            intent_id=migration.id,
            migration_id=migration.id,
            checkout_session_id=uuid4(),
            attempt_no=migration.attempt_count + 1,
            catalog_version_id=migration.target_catalog_version_id,
            offer_id=migration.target_offer_id,
            amount_cents=migration.target_amount_cents,
            pricing_snapshot=checkout_pricing_snapshot(migration.target_snapshot),
            next_due_date=migration.renewal_date,
        )
        migration.attempt_count += 1
        await self._session.flush()
        self._record_operation("recreate_reserve")
        return checkout

    async def reopen_recreate_after_absent_readback(
        self,
        *,
        migration_id: UUID,
        actor_user_id: UUID,
        readback_receipt_id: UUID | None = None,
    ) -> MigrationView:
        """Re-arm one migration only after two exact empty readbacks.

        The service receives an operator's already obtained conclusion; it does
        not call ASAAS itself.  The receipt makes the otherwise dangerous
        ``recreate_pending -> legacy_cancelled`` transition auditable and the
        database trigger rejects the transition without it and both reads.
        """

        roster_revision, migration, subscription = await self._locked_pair(migration_id)
        self._require_current(roster_revision, migration, subscription)
        self._reject_if_late(
            operation="recreate_reopen",
            deadline=migration.cutover_by_date,
            code=MIGRATION_CUTOVER_LATE,
        )
        if (
            migration.state != "recreate_pending"
            or migration.operation_id is None
            or subscription.provider is not None
            or subscription.status != "legacy_cancelled"
            or subscription.is_active
        ):
            raise BillingMigrationRejected("billing_migration_recovery_not_allowed")
        if readback_receipt_id is None:
            raise BillingMigrationRejected("billing_migration_readback_required")
        receipt = await self._session.scalar(
            select(BillingMigrationReceipt)
            .where(
                BillingMigrationReceipt.id == readback_receipt_id,
                BillingMigrationReceipt.builder_id == migration.builder_id,
                BillingMigrationReceipt.migration_id == migration.id,
                BillingMigrationReceipt.operation_id == migration.operation_id,
                BillingMigrationReceipt.receipt_kind == "recreate_authorized",
                BillingMigrationReceipt.provider == "asaas",
                BillingMigrationReceipt.external_reference
                == self._provider_reference(migration),
                BillingMigrationReceipt.status == "absent",
                BillingMigrationReceipt.observed_at
                >= self._clock() - timedelta(minutes=15),
            )
            .with_for_update()
        )
        if receipt is None:
            raise BillingMigrationRejected("billing_migration_readback_unverified")
        await self._validate_recreate_absence_receipt(
            migration=migration,
            receipt_id=receipt.id,
        )
        migration.state = "legacy_cancelled"
        await self._session.flush()
        self._record_operation("recreate_reopen")
        return self._view(migration)

    async def _latest_attempt(
        self, migration: BillingMigration
    ) -> BillingCheckoutSession | None:
        """The prior checkout attempt an absence read must be bound to."""

        return cast(
            BillingCheckoutSession | None,
            await self._session.scalar(
                select(BillingCheckoutSession)
                .where(
                    BillingCheckoutSession.migration_id == migration.id,
                    BillingCheckoutSession.builder_id == migration.builder_id,
                )
                .order_by(BillingCheckoutSession.attempt_no.desc())
                .limit(1)
                .with_for_update()
            ),
        )

    async def _live_attempt_exists(self, migration: BillingMigration) -> bool:
        live = await self._session.scalar(
            select(BillingCheckoutSession.id)
            .where(
                BillingCheckoutSession.migration_id == migration.id,
                BillingCheckoutSession.state.in_(LIVE_MIGRATION_CHECKOUT_STATES),
            )
            .limit(1)
        )
        return live is not None

    @staticmethod
    def _convergence_limit(
        migration: BillingMigration, prior: BillingCheckoutSession | None
    ) -> datetime:
        """Reads only count after the cancellation and the prior attempt converged."""

        if migration.legacy_cancelled_at is None:
            raise BillingMigrationRejected("billing_migration_absence_read_not_allowed")
        base = _aware_utc(migration.legacy_cancelled_at)
        if prior is not None and prior.created_at is not None:
            base = max(base, _aware_utc(prior.created_at))
        return base + ABSENCE_CONVERGENCE_LIMIT

    async def _absence_reads(
        self, migration: BillingMigration
    ) -> list[BillingMigrationAbsenceRead]:
        result = await self._session.execute(
            select(BillingMigrationAbsenceRead)
            .where(
                BillingMigrationAbsenceRead.migration_id == migration.id,
                BillingMigrationAbsenceRead.builder_id == migration.builder_id,
                BillingMigrationAbsenceRead.operation_id == migration.operation_id,
            )
            .order_by(BillingMigrationAbsenceRead.read_no)
            .with_for_update()
        )
        return list(result.scalars().all())

    async def record_absence_read(
        self,
        *,
        migration_id: UUID,
        actor_user_id: UUID,
        evidence: ProviderEvidenceEnvelope,
        observed_at: datetime | None = None,
    ) -> AbsenceReadView:
        """Persist one provider-signed empty ASAAS read (DESIGN decision 15).

        The backend binds the read to the latest prior checkout attempt (or to
        none) and to the convergence limit it derives under the roster lock;
        the provider signature binds migration, operation, reference, event
        and observation time.  The second valid read also persists the same
        signed envelope as the single ``recreate_authorized`` receipt, so an
        authorization can never exist without both reads.
        """

        now = self._clock()
        observed = observed_at or now
        if (
            observed.tzinfo is None
            or observed.utcoffset() is None
            or now.tzinfo is None
            or observed > now
        ):
            raise BillingMigrationRejected("billing_migration_receipt_invalid")
        # Evidence only: the roster and row locks are held, but the global
        # revision is not required to be current (a recreate_pending saga
        # cannot be re-based).  The signed snapshot digest binds the source
        # revision, so a read never survives a later re-base.
        _, migration, subscription = await self._locked_pair(migration_id)
        operation_id = migration.operation_id
        if (
            migration.state not in {"legacy_cancelled", "recreate_pending"}
            or operation_id is None
            or migration.legacy_cancelled_at is None
            or subscription.provider is not None
            or subscription.status != "legacy_cancelled"
            or subscription.is_active
        ):
            raise BillingMigrationRejected("billing_migration_absence_read_not_allowed")
        if await self._live_attempt_exists(migration):
            raise BillingMigrationRejected("billing_migration_attempt_unresolved")
        prior = await self._latest_attempt(migration)
        prior_id = prior.id if prior is not None else None
        limit = self._convergence_limit(migration, prior)
        reference = self._provider_reference(migration)
        key_id, public_key = _configured_provider_evidence_authority()
        validate_provider_evidence_envelope(
            migration_id=migration.id,
            operation_id=operation_id,
            expected_external_reference=reference,
            expected_snapshot_digest=await self._provider_digest(
                migration_id=migration.id,
                receipt_kind="provider_flip",
                observed_at=observed,
            ),
            migration_created_at=_aware_utc(migration.created_at),
            provider=evidence.provider,
            external_reference=evidence.external_reference,
            provider_event_id=evidence.provider_event_id,
            provider_authority=evidence.provider_authority,
            provider_key_id=evidence.provider_key_id,
            provider_operation=evidence.provider_operation,
            provider_signature=evidence.provider_signature,
            provider_observed_at=evidence.provider_observed_at,
            provider_expires_at=evidence.provider_expires_at,
            provider_snapshot_digest=evidence.provider_snapshot_digest,
            provider_evidence_digest_value=evidence.provider_evidence_digest,
            observed_at=observed,
            now=now,
            provider_public_key=public_key,
            expected_provider_key_id=key_id,
            expected_provider_operation=PROVIDER_ABSENCE_OPERATION,
            max_age=PROVIDER_EVIDENCE_MAX_AGE,
        )
        if evidence.provider_observed_at <= limit:
            raise BillingMigrationRejected(
                "billing_migration_absence_read_before_convergence"
            )
        reads = await self._absence_reads(migration)
        if not reads:
            read_no = 1
        elif len(reads) == 1 and reads[0].read_no == 1:
            first = reads[0]
            if first.provider_event_id == evidence.provider_event_id:
                raise BillingMigrationRejected("billing_migration_absence_read_duplicate")
            if (
                first.prior_checkout_session_id != prior_id
                or _aware_utc(first.convergence_limit_at) != limit
            ):
                raise BillingMigrationRejected(
                    "billing_migration_absence_read_attempt_changed"
                )
            if (
                evidence.provider_observed_at
                < _aware_utc(first.provider_observed_at) + ABSENCE_READ_SPACING
            ):
                raise BillingMigrationRejected("billing_migration_absence_read_too_soon")
            read_no = 2
        else:
            raise BillingMigrationRejected("billing_migration_absence_reads_complete")
        read = BillingMigrationAbsenceRead(
            builder_id=migration.builder_id,
            migration_id=migration.id,
            operation_id=operation_id,
            read_no=read_no,
            prior_checkout_session_id=prior_id,
            convergence_limit_at=limit,
            provider=evidence.provider,
            external_reference=evidence.external_reference,
            provider_event_id=evidence.provider_event_id,
            provider_authority=evidence.provider_authority,
            provider_key_id=evidence.provider_key_id,
            provider_operation=evidence.provider_operation,
            provider_signature=evidence.provider_signature,
            provider_observed_at=evidence.provider_observed_at,
            provider_expires_at=evidence.provider_expires_at,
            provider_snapshot_digest=evidence.provider_snapshot_digest,
            provider_evidence_digest=evidence.provider_evidence_digest,
            status="absent",
            actor_user_id=actor_user_id,
            observed_at=observed,
        )
        self._session.add(read)
        await self._session.flush()
        authorization_id: UUID | None = None
        if read_no == 2:
            authorization = BillingMigrationReceipt(
                builder_id=migration.builder_id,
                migration_id=migration.id,
                operation_id=operation_id,
                receipt_kind="recreate_authorized",
                provider=evidence.provider,
                external_reference=evidence.external_reference,
                provider_event_id=evidence.provider_event_id,
                provider_authority=evidence.provider_authority,
                provider_key_id=evidence.provider_key_id,
                provider_operation=evidence.provider_operation,
                provider_signature=evidence.provider_signature,
                provider_observed_at=evidence.provider_observed_at,
                provider_expires_at=evidence.provider_expires_at,
                provider_snapshot_digest=evidence.provider_snapshot_digest,
                provider_evidence_digest=evidence.provider_evidence_digest,
                status="absent",
                content_sha256=evidence.provider_evidence_digest,
                actor_user_id=actor_user_id,
                observed_at=observed,
            )
            self._session.add(authorization)
            await self._session.flush()
            authorization_id = authorization.id
        return AbsenceReadView(
            read_id=read.id,
            read_no=read_no,
            prior_checkout_session_id=prior_id,
            convergence_limit_at=limit,
            authorization_receipt_id=authorization_id,
        )

    async def _validate_recreate_absence_receipt(
        self, *, migration: BillingMigration, receipt_id: UUID
    ) -> BillingMigrationReceipt:
        """Require the signed authorization *and* both signed empty reads.

        The authorization must be the persisted copy of the second read; both
        reads must be bound to this migration operation, to the latest prior
        attempt and to the convergence limit recomputed now, have distinct
        provider event IDs and be spaced by ``ABSENCE_READ_SPACING``.
        """

        now = self._clock()
        operation_id = migration.operation_id
        receipt = await self._session.scalar(
            select(BillingMigrationReceipt)
            .where(
                BillingMigrationReceipt.id == receipt_id,
                BillingMigrationReceipt.builder_id == migration.builder_id,
                BillingMigrationReceipt.migration_id == migration.id,
                BillingMigrationReceipt.operation_id == migration.operation_id,
                BillingMigrationReceipt.receipt_kind == "recreate_authorized",
                BillingMigrationReceipt.provider == "asaas",
                BillingMigrationReceipt.external_reference
                == self._provider_reference(migration),
                BillingMigrationReceipt.status == "absent",
            )
            .with_for_update()
        )
        if receipt is None or operation_id is None:
            raise BillingMigrationRejected("billing_migration_readback_unverified")
        key_id, public_key = _configured_provider_evidence_authority()
        reference = self._provider_reference(migration)
        expected_snapshot = await self._provider_digest(
            migration_id=migration.id,
            receipt_kind="provider_flip",
            observed_at=_aware_utc(receipt.observed_at),
        )
        created_at = _aware_utc(migration.created_at)
        receipt_observed = _aware_utc(receipt.observed_at)
        if receipt_observed < created_at or receipt_observed > now:
            raise BillingMigrationRejected("billing_migration_readback_unverified")
        validate_provider_evidence_envelope(
            migration_id=migration.id,
            operation_id=operation_id,
            expected_external_reference=reference,
            expected_snapshot_digest=expected_snapshot,
            migration_created_at=created_at,
            provider=receipt.provider,
            external_reference=receipt.external_reference,
            provider_event_id=receipt.provider_event_id,
            provider_authority=receipt.provider_authority,
            provider_key_id=receipt.provider_key_id,
            provider_operation=receipt.provider_operation,
            provider_signature=receipt.provider_signature,
            provider_observed_at=_optional_aware(receipt.provider_observed_at),
            provider_expires_at=_optional_aware(receipt.provider_expires_at),
            provider_snapshot_digest=receipt.provider_snapshot_digest,
            provider_evidence_digest_value=receipt.provider_evidence_digest,
            observed_at=receipt_observed,
            now=now,
            provider_public_key=public_key,
            expected_provider_key_id=key_id,
            expected_provider_operation=PROVIDER_ABSENCE_OPERATION,
        )
        if receipt.content_sha256 != receipt.provider_evidence_digest:
            raise BillingMigrationRejected("billing_migration_readback_unverified")

        reads = await self._absence_reads(migration)
        if [read.read_no for read in reads] != [1, 2]:
            raise BillingMigrationRejected("billing_migration_absence_reads_incomplete")
        first, second = reads
        prior = await self._latest_attempt(migration)
        prior_id = prior.id if prior is not None else None
        limit = self._convergence_limit(migration, prior)
        for read in reads:
            if (
                read.prior_checkout_session_id != prior_id
                or _aware_utc(read.convergence_limit_at) != limit
            ):
                raise BillingMigrationRejected(
                    "billing_migration_absence_read_attempt_changed"
                )
            if _aware_utc(read.provider_observed_at) <= limit:
                raise BillingMigrationRejected(
                    "billing_migration_absence_read_before_convergence"
                )
            # Re-verify the signature and every bound field as of the instant
            # the read was recorded (its expiry was enforced then).
            read_observed = _aware_utc(read.observed_at)
            validate_provider_evidence_envelope(
                migration_id=migration.id,
                operation_id=operation_id,
                expected_external_reference=reference,
                expected_snapshot_digest=expected_snapshot,
                migration_created_at=created_at,
                provider=read.provider,
                external_reference=read.external_reference,
                provider_event_id=read.provider_event_id,
                provider_authority=read.provider_authority,
                provider_key_id=read.provider_key_id,
                provider_operation=read.provider_operation,
                provider_signature=read.provider_signature,
                provider_observed_at=_aware_utc(read.provider_observed_at),
                provider_expires_at=_aware_utc(read.provider_expires_at),
                provider_snapshot_digest=read.provider_snapshot_digest,
                provider_evidence_digest_value=read.provider_evidence_digest,
                observed_at=read_observed,
                now=read_observed,
                provider_public_key=public_key,
                expected_provider_key_id=key_id,
                expected_provider_operation=PROVIDER_ABSENCE_OPERATION,
            )
        if first.provider_event_id == second.provider_event_id or _aware_utc(
            second.provider_observed_at
        ) < _aware_utc(first.provider_observed_at) + ABSENCE_READ_SPACING:
            raise BillingMigrationRejected("billing_migration_absence_reads_invalid")
        if (
            receipt.provider_event_id != second.provider_event_id
            or receipt.provider_evidence_digest != second.provider_evidence_digest
            or _optional_aware(receipt.provider_observed_at)
            != _aware_utc(second.provider_observed_at)
            or _optional_aware(receipt.provider_expires_at)
            != _aware_utc(second.provider_expires_at)
        ):
            raise BillingMigrationRejected(
                "billing_migration_absence_authorization_mismatch"
            )
        return receipt

    async def recover_stuck_checkout(
        self,
        *,
        migration_id: UUID,
        checkout_session_id: UUID,
        environment: str,
        actor_user_id: UUID,
    ) -> MigrationView:
        """Contain a pre-general checkout crash without inventing provider state."""

        roster_revision, migration, subscription = await self._locked_pair(migration_id)
        self._require_current(roster_revision, migration, subscription)
        checkout = await self._repository.lock_checkout_session(
            builder_id=migration.builder_id, checkout_session_id=checkout_session_id
        )
        if (
            checkout is None
            or checkout.migration_id != migration.id
            or checkout.state != "creating"
            or subscription.provider is not None
        ):
            raise BillingMigrationRejected("billing_migration_recovery_not_allowed")
        control = await self._repository.lock_rollout_control(environment=environment)
        if control.state == "general":
            raise BillingMigrationRejected("billing_migration_recovery_not_allowed")
        # This does not claim that the provider did or did not create anything.
        # A later exact-ID readback is required before a fresh attempt.
        checkout.state = "terminal_pending_readback"
        migration.state = "recreate_pending"
        await self._ensure_provider_delete_receipt(
            migration=migration,
            actor_user_id=actor_user_id,
            provider=None,
            external_reference=None,
            content={
                "checkout_id": str(checkout.id),
                "migration_id": str(migration.id),
                "state": "terminal_pending_readback",
            },
            observed_at=self._clock(),
        )
        self._record_operation("checkout_recovery")
        return self._view(migration)

    async def mark_manual_intervention(
        self, *, migration_id: UUID, actor_user_id: UUID
    ) -> MigrationView:
        """Stop the saga with provider NULL; no fallback provider is inferred."""

        roster_revision, migration, subscription = await self._locked_pair(migration_id)
        self._require_current(roster_revision, migration, subscription)
        if subscription.provider is not None or migration.state == "migrated":
            raise BillingMigrationRejected("billing_migration_manual_not_allowed")
        migration.state = "manual_intervention"
        subscription.is_active = False
        subscription.status = "manual_intervention"
        await self._ensure_provider_delete_receipt(
            migration=migration,
            actor_user_id=actor_user_id,
            provider=None,
            external_reference=None,
            content={"migration_id": str(migration.id), "state": "manual"},
            observed_at=self._clock(),
        )
        self._record_operation("manual_intervention")
        return self._view(migration)

    async def record_provider_flip(
        self,
        *,
        migration_id: UUID,
        actor_user_id: UUID,
        evidence: ProviderEvidenceEnvelope,
        observed_at: datetime | None = None,
    ) -> MigrationView:
        """Bind ASAAS only from a verified provider-signed observation.

        Order inside one transaction, all under the roster and row locks:

        1. verify the Ed25519-signed envelope against the configured provider
           key, before any write.  The ASAAS subscription ID is the envelope's
           ``external_reference``: signed, never a caller argument;
        2. bind ``target_external_subscription_id`` (NULL -> signed ID); the
           unique ``(target_provider, target_external_subscription_id)``
           constraint refuses an ID already bound to another migration;
        3. persist the envelope as the ``provider_flip`` receipt; the evidence
           trigger re-checks it against the now-bound target;
        4. flip the subscription; its trigger requires that exact receipt,
           advances the roster revision and marks the saga ``migrated``.

        The provider identifier is opaque and is never logged or returned.
        """

        now = self._clock()
        observed = observed_at or now
        if (
            observed.tzinfo is None
            or observed.utcoffset() is None
            or now.tzinfo is None
            or observed > now
        ):
            raise BillingMigrationRejected("billing_migration_receipt_invalid")
        asaas_subscription_id = evidence.external_reference
        if (
            not isinstance(asaas_subscription_id, str)
            or not _PROVIDER_ID.fullmatch(asaas_subscription_id)
            or asaas_subscription_id.startswith("migration:")
        ):
            raise BillingMigrationRejected("billing_migration_provider_id_invalid")
        roster_revision, migration, subscription = await self._locked_pair(migration_id)
        self._require_current(roster_revision, migration, subscription)
        operation_id = migration.operation_id
        if (
            migration.state != "legacy_cancelled"
            or operation_id is None
            or subscription.provider is not None
            or subscription.status != "legacy_cancelled"
            or subscription.is_active
            or (
                migration.target_external_subscription_id is not None
                and migration.target_external_subscription_id != asaas_subscription_id
            )
        ):
            raise BillingMigrationRejected("billing_migration_flip_not_allowed")
        expected_snapshot = await self._provider_digest(
            migration_id=migration.id,
            receipt_kind="provider_flip",
            observed_at=observed,
            target_external_subscription_id=asaas_subscription_id,
        )
        key_id, public_key = _configured_provider_evidence_authority()
        validate_provider_evidence_envelope(
            migration_id=migration.id,
            operation_id=operation_id,
            expected_external_reference=asaas_subscription_id,
            expected_snapshot_digest=expected_snapshot,
            migration_created_at=_aware_utc(migration.created_at),
            provider=evidence.provider,
            external_reference=evidence.external_reference,
            provider_event_id=evidence.provider_event_id,
            provider_authority=evidence.provider_authority,
            provider_key_id=evidence.provider_key_id,
            provider_operation=evidence.provider_operation,
            provider_signature=evidence.provider_signature,
            provider_observed_at=evidence.provider_observed_at,
            provider_expires_at=evidence.provider_expires_at,
            provider_snapshot_digest=evidence.provider_snapshot_digest,
            provider_evidence_digest_value=evidence.provider_evidence_digest,
            observed_at=observed,
            now=now,
            provider_public_key=public_key,
            expected_provider_key_id=key_id,
            expected_provider_operation=PROVIDER_FLIP_OPERATION,
            max_age=PROVIDER_EVIDENCE_MAX_AGE,
        )

        if migration.target_external_subscription_id is None:
            migration.target_external_subscription_id = asaas_subscription_id
            await self._session.flush()
        self._session.add(
            BillingMigrationReceipt(
                builder_id=migration.builder_id,
                migration_id=migration.id,
                operation_id=operation_id,
                receipt_kind="provider_flip",
                provider=evidence.provider,
                external_reference=asaas_subscription_id,
                provider_event_id=evidence.provider_event_id,
                provider_authority=evidence.provider_authority,
                provider_key_id=evidence.provider_key_id,
                provider_operation=evidence.provider_operation,
                provider_signature=evidence.provider_signature,
                provider_observed_at=evidence.provider_observed_at,
                provider_expires_at=evidence.provider_expires_at,
                provider_snapshot_digest=evidence.provider_snapshot_digest,
                provider_evidence_digest=evidence.provider_evidence_digest,
                status="verified",
                # The Group 1 subscription trigger binds the flip to a receipt
                # whose content is the digest of the exact bound target.
                content_sha256=expected_snapshot,
                actor_user_id=actor_user_id,
                observed_at=observed,
            )
        )
        await self._session.flush()
        subscription.provider = "asaas"
        subscription.external_subscription_id = asaas_subscription_id
        subscription.plan = migration.target_plan
        subscription.billing_cycle = migration.target_billing_cycle
        subscription.coupon_code = migration.target_coupon_code
        subscription.catalog_version_id = migration.target_catalog_version_id
        subscription.offer_id = migration.target_offer_id
        subscription.amount_cents = migration.target_amount_cents
        subscription.status = "active_linked"
        subscription.is_active = True
        subscription.roster_revision = roster_revision + 1
        await self._session.flush()
        await self._session.refresh(migration)
        self._record_operation("provider_flip")
        return self._view(migration)
