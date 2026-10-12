"""Authoritative hosted-checkout orchestration for ASAAS.

This module is the application boundary between an authenticated builder and
the ASAAS client.  The browser can select an immutable offer and a permitted
payment method, but it cannot select a tenant, amount, due date, provider URL
or any payment data.  A checkout session is committed in ``creating`` before
the outbound request is attempted; therefore an uncertain provider result is
reconciled by the persisted session UUID instead of being retried.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable, Final, Literal, Mapping
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import settings
from src.models.billing import BillingCheckoutSession, BillingTopupOrder
from src.models.coupon import Coupon
from src.models.tenant import Tenant
from src.repositories.billing import BillingRepository
from src.services.asaas import (
    ASAAS_CHECKOUT_MINUTES_TO_EXPIRE,
    AsaasClient,
    AsaasOutcomeUnknown,
    AsaasRequestRejected,
    CheckoutCallback,
    CheckoutCreateRequest,
    CheckoutItem,
    authorize_creation_egress,
)
from src.services.billing_catalog import (
    BILLING_CATALOG,
    CATALOG_VERSION_ID,
    BillingOffer,
)
from src.services.billing_rollout import (
    BILLING_CHECKOUT_UNAVAILABLE,
    BillingCheckoutUnavailable,
    require_billing_checkout_rollout,
    require_billing_topup_rollout,
)
from src.services.egress_policy import require_financial_egress

PaymentMethod = Literal["credit_card", "pix"]
CheckoutKind = Literal["subscription", "topup"]

_LIVE_STATES: Final = (
    "creating",
    "active",
    "paid_pending_subscription",
    "reconciliation_required",
    "terminal_pending_readback",
)
_SUBSCRIPTION_OFFERS: Final = frozenset(
    offer_id for offer_id, offer in BILLING_CATALOG.items() if offer.cycle == "MONTHLY"
)
_TOPUP_OFFERS: Final = frozenset(
    offer_id for offer_id, offer in BILLING_CATALOG.items() if offer.cycle == "DETACHED"
)
_COUPON_CODE_MAX_LENGTH: Final = 50

# Group 3 (M-C) convergence of ambiguous attempts.  The provider Checkout
# expires ``ASAAS_CHECKOUT_MINUTES_TO_EXPIRE`` after creation; reads after the
# limit below can no longer be raced by a payer.  An attempt still
# ``creating`` after ``ORPHANED_CREATING_AFTER`` (far beyond the client's
# connect+write+read timeouts) belongs to a crashed request.
CHECKOUT_CONVERGENCE_LIMIT: Final = timedelta(
    minutes=ASAAS_CHECKOUT_MINUTES_TO_EXPIRE + 30
)
CONVERGENCE_READ_SPACING: Final = timedelta(minutes=5)
ORPHANED_CREATING_AFTER: Final = timedelta(minutes=5)


def _aware_or_none(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# The commercial calendar is São Paulo's, never the host's.  Resolving the zone
# at import time makes missing zone data a startup failure: there is no
# fallback to host-local or UTC dates.  Production resolves it from the locked
# main-group ``tzdata`` wheel even when the image has no system zoneinfo.
BILLING_TIMEZONE: Final = ZoneInfo("America/Sao_Paulo")


class BillingCheckoutError(RuntimeError):
    """A fixed-code checkout failure safe for an HTTP response or log."""

    code = "billing_checkout_error"

    def __init__(self, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(self.code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


class CheckoutInputRejected(BillingCheckoutError):
    code = "billing_checkout_input_rejected"


class CheckoutNotFound(BillingCheckoutError):
    code = "billing_checkout_not_found"


class CheckoutUnavailable(BillingCheckoutError):
    code = BILLING_CHECKOUT_UNAVAILABLE


class CheckoutCreationRejected(BillingCheckoutError):
    code = "billing_checkout_creation_rejected"


class CheckoutReconciliationRequired(BillingCheckoutError):
    code = "billing_checkout_reconciliation_required"


class CheckoutNotEligible(BillingCheckoutError):
    """One non-enumerating code for every subscription eligibility blocker."""

    code = "billing_checkout_not_eligible"


class CheckoutCouponRejected(BillingCheckoutError):
    """One non-enumerating code for absent, inactive, expired or exhausted."""

    code = "billing_checkout_coupon_invalid"


COUPON_NOT_ELIGIBLE: Final = "billing_checkout_coupon_not_eligible"


@dataclass(frozen=True, slots=True)
class _CreationPreconditions:
    """Side-effect-free creation inputs established before any reservation."""

    callback: CheckoutCallback
    client: AsaasClient


@dataclass(frozen=True, slots=True)
class ResolvedCoupon:
    """Server-resolved coupon terms frozen into one pricing snapshot."""

    code: str
    discount_percent: int


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def billing_local_date(instant: datetime) -> date:
    """Return the São Paulo calendar date of an aware instant.

    A naive instant has no defined position on the commercial calendar and
    is rejected instead of being interpreted in the host timezone.
    """

    if instant.tzinfo is None or instant.utcoffset() is None:
        raise CheckoutUnavailable()
    return instant.astimezone(BILLING_TIMEZONE).date()


def normalize_coupon_code(value: str | None) -> str | None:
    """Normalize a browser coupon selector exactly as coupons are stored.

    Admin creation persists ``strip().upper()`` and the legacy lookup compares
    ``upper()``; the stored column is at most 50 characters.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise CheckoutCouponRejected()
    normalized = value.strip().upper()
    if not 1 <= len(normalized) <= _COUPON_CODE_MAX_LENGTH:
        raise CheckoutCouponRejected()
    return normalized


@dataclass(frozen=True, slots=True)
class CheckoutView:
    """Sanitized checkout state returned to the authenticated builder."""

    checkout_session_id: UUID
    kind: CheckoutKind
    offer_id: str
    state: str
    checkout_url: str | None


def _offer_kind(offer: BillingOffer) -> CheckoutKind:
    if offer.cycle == "MONTHLY":
        return "subscription"
    if offer.cycle == "DETACHED":
        return "topup"
    raise CheckoutInputRejected()


def _checked_offer(offer_id: str) -> BillingOffer:
    offer = BILLING_CATALOG.get(offer_id)
    if offer is None:
        raise CheckoutInputRejected()
    return offer


def _checked_method(kind: CheckoutKind, method: str) -> PaymentMethod:
    if kind == "subscription" and method == "credit_card":
        return "credit_card"
    if kind == "topup" and method == "credit_card":
        return "credit_card"
    if kind == "topup" and method == "pix":
        return "pix"
    raise CheckoutInputRejected()


def _snapshot_line(
    *,
    line_id: str,
    offer_id: str,
    kind: str,
    quantity: int,
    unit_amount_cents: int,
    credit_grant_mc: int,
) -> dict[str, int | str]:
    if type(quantity) is not int or quantity <= 0:
        raise CheckoutInputRejected()
    if type(unit_amount_cents) is not int or unit_amount_cents < 0:
        raise CheckoutInputRejected()
    if type(credit_grant_mc) is not int or credit_grant_mc < 0:
        raise CheckoutInputRejected()
    amount = quantity * unit_amount_cents
    return {
        "line_id": line_id,
        "offer_id": offer_id,
        "kind": kind,
        "quantity": quantity,
        "unit_amount_cents": unit_amount_cents,
        "line_amount_cents": amount,
        "credit_grant_mc": credit_grant_mc,
        "settled_cents": 0,
        "reversed_cents": 0,
        "restored_cents": 0,
        "line_financial_state": "pending",
    }


def subscription_pricing_snapshot(
    offer: BillingOffer,
    *,
    active_clinics: int,
    coupon_code: str | None = None,
    coupon_discount_percent: int = 0,
) -> dict[str, object]:
    """Build an immutable integer-only subscription snapshot.

    The coupon terms are an internal input: the browser may only name a code,
    which the service resolves against ``coupons`` before calling this.  Once
    this function's output is persisted, retries reuse the snapshot and cannot
    observe a changed coupon or clinic count.

    A coupon discounts each line's unit price (``unit * percent // 100``), so
    every line keeps ``line_amount_cents = quantity * unit_amount_cents``, the
    lines sum to ``amount_cents`` and only the persisted line kinds are used
    (the DB snapshot CHECK admits ``plan|additional_clinic|topup|tax`` only).
    The per-line discounts must add up to exactly
    ``gross_cents * percent // 100``; otherwise the snapshot is rejected
    rather than rounded.
    """

    if offer.offer_id not in _SUBSCRIPTION_OFFERS or type(active_clinics) is not int:
        raise CheckoutInputRejected()
    if type(coupon_discount_percent) is not int:
        raise CheckoutInputRejected()
    if active_clinics < 0 or not 0 <= coupon_discount_percent <= 100:
        raise CheckoutInputRejected()
    if coupon_code is not None and (
        not isinstance(coupon_code, str) or not 1 <= len(coupon_code) <= 50
    ):
        raise CheckoutInputRejected()
    if coupon_code is None and coupon_discount_percent != 0:
        # A discount must always be attributable to a persisted coupon code.
        raise CheckoutInputRejected()

    additional_clinics = max(0, active_clinics - offer.included_clinics)
    additional_cents = additional_clinics * offer.additional_clinic_amount_cents
    gross_cents = offer.amount_cents + additional_cents
    discount_cents = gross_cents * coupon_discount_percent // 100
    # Guard the arithmetic explicitly: discounts can reduce a checkout to zero
    # but can never make it negative.  ASAAS and the persistence schema both
    # require a positive checkout total, so a fully discounted checkout is not
    # a hosted-payment checkout and is rejected here.
    net_cents = gross_cents - discount_cents
    if gross_cents <= 0 or discount_cents < 0 or net_cents <= 0:
        raise CheckoutInputRejected()

    def discounted(unit_amount_cents: int) -> int:
        return unit_amount_cents - unit_amount_cents * coupon_discount_percent // 100

    plan_line = _snapshot_line(
        line_id="plan",
        offer_id=offer.offer_id,
        kind="plan",
        quantity=1,
        unit_amount_cents=discounted(offer.amount_cents),
        credit_grant_mc=offer.credits,
    )
    lines: list[dict[str, int | str]] = [plan_line]
    if additional_clinics:
        lines.append(
            _snapshot_line(
                line_id="additional_clinic",
                offer_id=offer.offer_id,
                kind="additional_clinic",
                quantity=additional_clinics,
                unit_amount_cents=discounted(offer.additional_clinic_amount_cents),
                credit_grant_mc=0,
            )
        )
    if sum(int(line["line_amount_cents"]) for line in lines) != net_cents:
        raise CheckoutInputRejected()

    snapshot: dict[str, object] = {
        "catalog_version_id": CATALOG_VERSION_ID,
        "offer_id": offer.offer_id,
        "amount_cents": net_cents,
        "credit_grant_mc": offer.credits,
        "included_clinics": offer.included_clinics,
        "additional_clinics": additional_clinics,
        "billing_cycle": "monthly",
        "payment_method": "credit_card",
        "lines": lines,
    }
    if coupon_code is not None:
        snapshot["coupon_code"] = coupon_code
    return snapshot


def topup_pricing_snapshot(
    offer: BillingOffer,
    *,
    payment_method: PaymentMethod,
) -> dict[str, object]:
    """Build an immutable, PII-free detached top-up snapshot."""

    if offer.offer_id not in _TOPUP_OFFERS:
        raise CheckoutInputRejected()
    return {
        "catalog_version_id": CATALOG_VERSION_ID,
        "offer_id": offer.offer_id,
        "amount_cents": offer.amount_cents,
        "credit_grant_mc": offer.credits,
        "payment_method": payment_method,
        "lines": [
            _snapshot_line(
                line_id="topup",
                offer_id=offer.offer_id,
                kind="topup",
                quantity=1,
                unit_amount_cents=offer.amount_cents,
                credit_grant_mc=offer.credits,
            )
        ],
    }


def checkout_items(
    snapshot: Mapping[str, object], offer: BillingOffer
) -> tuple[CheckoutItem, ...]:
    """Translate only persisted snapshot amounts into provider item values."""

    amount_cents = snapshot.get("amount_cents")
    if type(amount_cents) is not int or amount_cents <= 0:
        raise CheckoutInputRejected()
    if offer.cycle == "MONTHLY":
        labels = {
            "plan_plus_monthly_v1": "NeuroStack Plus",
            "plan_pro_monthly_v1": "NeuroStack Pro",
            "plan_max_monthly_v1": "NeuroStack Max",
        }
        name = labels.get(offer.offer_id)
        if name is None:
            raise CheckoutInputRejected()
        return (
            CheckoutItem(
                name=name,
                description="NeuroStack monthly subscription",
                quantity=1,
                unit_amount_cents=amount_cents,
            ),
        )
    return (
        CheckoutItem(
            name="NeuroStack credits",
            description="NeuroStack prepaid credit package",
            quantity=1,
            unit_amount_cents=amount_cents,
        ),
    )


class BillingCheckoutService:
    """Reserve, commit, create and reconcile a hosted checkout session."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        client_factory: Callable[[], AsaasClient],
        clock: Callable[[], datetime] = _utc_now,
        creation_precondition: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._db = db
        self._client_factory = client_factory
        self._clock = clock
        # Group 3: an async, side-effect-free creation precondition (the
        # webhook readiness readback) awaited before any reservation/lock.
        self._creation_precondition = creation_precondition

    async def create_checkout(
        self,
        *,
        builder_id: UUID,
        offer_id: str,
        payment_method: str,
        coupon_code: str | None = None,
    ) -> CheckoutView:
        """Create or return a live checkout without ever blind-retrying egress.

        Order of operations (each step fails closed, with no row and no POST):

        1. input validation, then a lock-free persisted-rollout pre-check;
        2. a lock-free read of a live session for the same offer and method,
           which is returned as-is (its frozen snapshot, no coupon resolution,
           no flag read, no client);
        3. coupon resolution (a selector only; one fixed code when unusable);
        4. every side-effect-free creation precondition: the callback/return
           URL, a financial-egress flag read and the client build;
        5. the environment rollout lock, a live-session re-check, subscription
           eligibility, the request build and the reservation, committed;
        6. a fresh rollout re-authorization, a second flag read that mints the
           single-use permit, then exactly one POST.

        Steps 1-4 run before any reservation, so a closed flag, a bad return
        URL or a client/configuration error never consumes a canary slot.
        """

        offer = _checked_offer(offer_id)
        kind = _offer_kind(offer)
        method = _checked_method(kind, payment_method)
        coupon_selector = normalize_coupon_code(coupon_code)
        if coupon_selector is not None and kind != "subscription":
            raise CheckoutInputRejected()
        repository = BillingRepository(self._db)

        try:
            # This inexpensive guard establishes configuration plus baseline
            # rollout eligibility; the following serialized reservation is the
            # authoritative, race-safe enforcement immediately before egress.
            await require_billing_checkout_rollout(self._db, builder_id)
        except BillingCheckoutUnavailable as exc:
            raise CheckoutUnavailable() from exc
        except Exception as exc:
            raise CheckoutUnavailable() from exc

        try:
            existing = await self._live_checkout(
                builder_id=builder_id,
                offer_id=offer.offer_id,
                payment_method=method,
            )
            if existing is not None:
                return self._view(existing)
            if coupon_selector is not None:
                await self._resolve_coupon(coupon_selector)
        except CheckoutCouponRejected:
            await self._rollback()
            raise
        except Exception as exc:
            await self._rollback()
            raise CheckoutUnavailable() from exc

        prepared = await self._prepare_creation()

        try:
            await repository.lock_rollout_control(environment=settings.environment)
            live = await self._live_checkout(
                builder_id=builder_id,
                offer_id=offer.offer_id,
                payment_method=method,
            )
        except Exception as exc:
            await self._rollback()
            raise CheckoutUnavailable() from exc
        if live is not None:
            view = self._view(live)
            await self._rollback()
            return view

        try:
            if kind == "subscription":
                checkout, request = await self._reserve_subscription(
                    repository=repository,
                    builder_id=builder_id,
                    offer=offer,
                    callback=prepared.callback,
                    coupon_code=coupon_selector,
                )
            else:
                checkout, request = await self._reserve_topup(
                    builder_id=builder_id,
                    offer=offer,
                    payment_method=method,
                    callback=prepared.callback,
                )
            # This is the irrevocable pre-egress boundary.  A later timeout
            # leaves a durable UUID that reconciliation can correlate exactly.
            await self._db.commit()
        except (CheckoutCouponRejected, CheckoutNotEligible):
            await self._rollback()
            raise
        except (
            BillingCheckoutUnavailable,
            CheckoutInputRejected,
            CheckoutUnavailable,
        ) as exc:
            await self._rollback()
            raise CheckoutUnavailable() from exc
        except Exception as exc:
            await self._rollback()
            raise CheckoutUnavailable() from exc

        return await self._send_reserved(checkout, request, prepared)

    async def get_checkout(
        self, *, builder_id: UUID, checkout_session_id: UUID
    ) -> CheckoutView:
        """Read a checkout inside the tenant scope without a row lock or egress."""

        result = await self._db.execute(
            select(BillingCheckoutSession).where(
                BillingCheckoutSession.id == checkout_session_id,
                BillingCheckoutSession.builder_id == builder_id,
            )
        )
        checkout = result.scalar_one_or_none()
        if checkout is None:
            # Do not distinguish an alien UUID from an absent UUID; in
            # particular, do not lock it or ask ASAAS about it.
            raise CheckoutNotFound()
        return self._view(checkout)

    async def reconcile_checkout(
        self, *, builder_id: UUID, checkout_session_id: UUID
    ) -> CheckoutView:
        """Perform read-only, exact-reference reconciliation; never create again.

        Provider reads happen first, with no row lock and no open database
        transaction; the session is then row-locked and re-validated before any
        write (a concurrent webhook or reconciliation may have moved it, in
        which case this call is an idempotent no-op).

        Convergence (Group 3, M-C):

        * an orphaned ``creating`` attempt (older than
          ``ORPHANED_CREATING_AFTER``) may already have reached ASAAS, so it
          becomes ``reconciliation_required`` together with its first read;
        * automatic ``abandoned`` is DORMANT unless the protected setting
          ``ASAAS_AUTO_ABANDON_ENABLED`` is true.  The empty reads here are by
          ``externalReference`` = our session UUID, and ASAAS does not document
          that a Checkout copies its ``externalReference`` to the subscription
          or payments it creates; if it does not, a checkout that timed out
          during creation but was paid would read "empty" and could be
          abandoned, opening the door to a second recurring subscription.
          Enable the setting only after Group 8 proves propagation by readback.
          With it off, such a session stays ``reconciliation_required`` and
          keeps blocking new subscription checkouts for that builder (it is
          counted in the readback backlog and alerted) until an operator
          resolves it; with it on, ``abandoned`` needs TWO empty exact-ID reads
          after ``created_at + CHECKOUT_CONVERGENCE_LIMIT`` (past the provider
          Checkout expiry), at least ``CONVERGENCE_READ_SPACING`` apart.  The
          first post-limit empty read is recorded by stamping ``updated_at``;
          nothing else writes this state's row.
        """

        result = await self._db.execute(
            select(BillingCheckoutSession)
            .where(
                BillingCheckoutSession.id == checkout_session_id,
                BillingCheckoutSession.builder_id == builder_id,
            )
            .execution_options(populate_existing=True)
        )
        observed = result.scalar_one_or_none()
        if observed is None:
            await self._rollback()
            raise CheckoutNotFound()
        view = self._view(observed)
        observed_state = observed.state
        observed_created = _aware_or_none(observed.created_at)
        # End the read transaction before any network I/O (plain values only:
        # a rollback expires ORM instances).
        await self._rollback()
        if observed_state not in ("reconciliation_required", "creating"):
            return view
        now = self._clock()
        if observed_state == "creating" and (
            observed_created is None or now - observed_created < ORPHANED_CREATING_AFTER
        ):
            return view  # the originating request may still be in flight

        try:
            client = self._client_factory()
            subscriptions = await client.list_subscriptions_by_external_reference(
                checkout_session_id
            )
            payments = await client.list_payments_by_external_reference(
                checkout_session_id
            )
        except Exception as exc:
            raise CheckoutReconciliationRequired() from exc

        try:
            checkout = await BillingRepository(self._db).lock_checkout_session(
                builder_id=builder_id,
                checkout_session_id=checkout_session_id,
            )
            if checkout is None:
                await self._rollback()
                raise CheckoutNotFound()
            created_at = _aware_or_none(checkout.created_at)
            if checkout.state not in ("reconciliation_required", "creating") or (
                checkout.state == "creating"
                and (created_at is None or now - created_at < ORPHANED_CREATING_AFTER)
            ):
                current = self._view(checkout)
                await self._rollback()
                return current
            was_reconciling = checkout.state == "reconciliation_required"
            previous_read = _aware_or_none(checkout.updated_at)
            if subscriptions or payments:
                checkout.reconciliation_attempts += 1
                # A matching provider record proves a request may have been
                # applied, but settlement is owned by webhook/readback work.
                checkout.state = "terminal_pending_readback"
                await self._db.commit()
                return self._view(checkout)
            past_limit = (
                created_at is not None
                and now >= created_at + CHECKOUT_CONVERGENCE_LIMIT
            )
            prior_empty_read_after_limit = bool(
                was_reconciling
                and past_limit
                and created_at is not None
                and previous_read is not None
                and previous_read >= created_at + CHECKOUT_CONVERGENCE_LIMIT
            )
            if prior_empty_read_after_limit:
                assert previous_read is not None
                if now - previous_read < CONVERGENCE_READ_SPACING:
                    # Too soon to count as an independent second read.
                    current = self._view(checkout)
                    await self._rollback()
                    return current
                checkout.state = (
                    "abandoned"
                    if settings.asaas_auto_abandon_enabled
                    else "reconciliation_required"
                )
            else:
                checkout.state = "reconciliation_required"
            checkout.reconciliation_attempts += 1
            checkout.updated_at = now
            await self._db.commit()
        except CheckoutNotFound:
            raise
        except Exception as exc:
            await self._rollback()
            raise CheckoutReconciliationRequired() from exc
        return self._view(checkout)

    async def _prepare_creation(self) -> _CreationPreconditions:
        """Check every side-effect-free creation precondition before reserving.

        Permit semantics: the financial-egress flag is read twice per creation.
        This first read is a gate only (its permit is discarded), so a closed
        or unreadable flag never reserves a row or a canary slot.  The permit
        that reaches the POST is minted by a second read after the fresh
        rollout re-authorization (``_send_reserved``), so a kill switch thrown
        while the reservation was being made still stops the POST; that
        attempt then ends ``failed`` like any other post-reservation denial.
        """

        try:
            callback = CheckoutCallback.from_return_url(
                settings.asaas_checkout_return_url
            )
            await authorize_creation_egress(require_financial_egress)
            if self._creation_precondition is not None:
                # End the read transaction first: no connection idles in a
                # transaction while the provider readback runs.
                await self._rollback()
                await self._creation_precondition()
            client = self._client_factory()
        except Exception as exc:
            await self._rollback()
            raise CheckoutUnavailable() from exc
        return _CreationPreconditions(callback=callback, client=client)

    async def _require_subscription_eligibility(
        self, repository: BillingRepository, builder_id: UUID
    ) -> None:
        """Deliverable 9: reject an ineligible builder under the rollout lock.

        Called only while ``lock_rollout_control`` is held, before any row is
        created.  Every reason maps to one stable, non-enumerating code; the
        reason itself never leaves the process.
        """

        if await repository.subscription_checkout_blocker(builder_id=builder_id):
            raise CheckoutNotEligible()

    @staticmethod
    def _build_request(
        *,
        kind: CheckoutKind,
        checkout_session_id: UUID,
        snapshot: Mapping[str, object],
        offer: BillingOffer,
        next_due_date: date | None,
        callback: CheckoutCallback,
    ) -> CheckoutCreateRequest:
        return CheckoutCreateRequest(
            kind=kind,
            external_reference=checkout_session_id,
            items=checkout_items(snapshot, offer),
            callback=callback,
            next_due_date=next_due_date,
        )

    @staticmethod
    def _verify_reserved(
        checkout: BillingCheckoutSession,
        *,
        checkout_session_id: UUID,
        request: CheckoutCreateRequest,
    ) -> None:
        """The persisted attempt must be exactly the request that will be sent."""

        total = sum(item.quantity * item.unit_amount_cents for item in request.items)
        if (
            checkout.id != checkout_session_id
            or checkout.state != "creating"
            or checkout.amount_cents != total
            or checkout.next_due_date != request.next_due_date
        ):
            raise CheckoutUnavailable()

    async def _reserve_subscription(
        self,
        *,
        repository: BillingRepository,
        builder_id: UUID,
        offer: BillingOffer,
        callback: CheckoutCallback,
        coupon_code: str | None = None,
    ) -> tuple[BillingCheckoutSession, CheckoutCreateRequest]:
        control = await repository.lock_rollout_control(
            environment=settings.environment
        )
        await self._require_subscription_eligibility(repository, builder_id)
        # Design contract: a new monthly subscription is first due on the
        # current São Paulo calendar date, whatever the host timezone is.
        next_due_date = billing_local_date(self._clock())
        checkout_session_id = uuid4()
        if control.state == "canary_allowlist":
            # The Group-1 canary primitive deliberately accepts only the exact
            # Plus contract and controls its own immutable snapshot.
            if offer.offer_id != "plan_plus_monthly_v1":
                raise CheckoutUnavailable()
            if coupon_code is not None:
                # The DB pins the canary to the canonical undiscounted Plus
                # snapshot; a coupon-priced canary can never be created.
                raise CheckoutCouponRejected(COUPON_NOT_ELIGIBLE)
            request = self._build_request(
                kind="subscription",
                checkout_session_id=checkout_session_id,
                snapshot={"amount_cents": offer.amount_cents},
                offer=offer,
                next_due_date=next_due_date,
                callback=callback,
            )
            checkout = await repository.reserve_canary_subscription_checkout(
                environment=settings.environment,
                builder_id=builder_id,
                intent_id=uuid4(),
                checkout_session_id=checkout_session_id,
                next_due_date=next_due_date,
            )
            self._verify_reserved(
                checkout, checkout_session_id=checkout_session_id, request=request
            )
            return checkout, request
        if control.state != "general":
            raise CheckoutUnavailable()
        # Re-resolve under the serialized reservation: the terms frozen into
        # the snapshot are the ones read in the transaction that persists it.
        coupon = (
            await self._resolve_coupon(coupon_code) if coupon_code is not None else None
        )
        active_clinics = await self._count_active_clinics(builder_id)
        snapshot = subscription_pricing_snapshot(
            offer,
            active_clinics=active_clinics,
            coupon_code=coupon.code if coupon is not None else None,
            coupon_discount_percent=(
                coupon.discount_percent if coupon is not None else 0
            ),
        )
        request = self._build_request(
            kind="subscription",
            checkout_session_id=checkout_session_id,
            snapshot=snapshot,
            offer=offer,
            next_due_date=next_due_date,
            callback=callback,
        )
        checkout = await repository.reserve_general_subscription_checkout(
            environment=settings.environment,
            builder_id=builder_id,
            intent_id=uuid4(),
            migration_id=None,
            checkout_session_id=checkout_session_id,
            attempt_no=1,
            catalog_version_id=CATALOG_VERSION_ID,
            offer_id=offer.offer_id,
            amount_cents=self._snapshot_amount(snapshot),
            pricing_snapshot=snapshot,
            next_due_date=next_due_date,
        )
        self._verify_reserved(
            checkout, checkout_session_id=checkout_session_id, request=request
        )
        return checkout, request

    async def _reserve_topup(
        self,
        *,
        builder_id: UUID,
        offer: BillingOffer,
        payment_method: PaymentMethod,
        callback: CheckoutCallback,
    ) -> tuple[BillingCheckoutSession, CheckoutCreateRequest]:
        snapshot = topup_pricing_snapshot(offer, payment_method=payment_method)
        checkout_session_id = uuid4()
        request = self._build_request(
            kind="topup",
            checkout_session_id=checkout_session_id,
            snapshot=snapshot,
            offer=offer,
            next_due_date=None,
            callback=callback,
        )
        order = BillingTopupOrder(
            id=uuid4(),
            builder_id=builder_id,
            provider="asaas",
            catalog_version_id=CATALOG_VERSION_ID,
            offer_id=offer.offer_id,
            payment_method=payment_method,
            amount_cents=self._snapshot_amount(snapshot),
            credit_grant_mc=offer.credits,
            pricing_snapshot=snapshot,
            state="pending",
        )
        self._db.add(order)
        await self._db.flush()
        checkout = await require_billing_topup_rollout(
            self._db,
            builder_id=builder_id,
            topup_order_id=order.id,
            checkout_session_id=checkout_session_id,
            attempt_no=1,
        )
        self._verify_reserved(
            checkout, checkout_session_id=checkout_session_id, request=request
        )
        return checkout, request

    async def _send_reserved(
        self,
        checkout: BillingCheckoutSession,
        request: CheckoutCreateRequest,
        prepared: _CreationPreconditions,
    ) -> CheckoutView:
        # Captured before any rollback can expire the ORM instance.
        builder_id = checkout.builder_id
        checkout_session_id = checkout.id

        try:
            await self._reauthorize_before_post(
                builder_id=builder_id, checkout_session_id=checkout_session_id
            )
            # Read after the locks are released: no flag I/O under a lock.
            permit = await authorize_creation_egress(require_financial_egress)
        except Exception as exc:
            # Nothing was sent: this attempt ends conclusively as ``failed``
            # (non-live), so the builder can retry once rollout allows it.
            await self._rollback()
            await self._mark_failed_not_sent(
                builder_id=builder_id, checkout_session_id=checkout_session_id
            )
            raise CheckoutUnavailable() from exc

        try:
            created = await prepared.client.create_checkout(request, permit=permit)
        except AsaasRequestRejected as exc:
            await self._mark_failed_not_sent(
                builder_id=builder_id, checkout_session_id=checkout_session_id
            )
            raise CheckoutCreationRejected() from exc
        except AsaasOutcomeUnknown as exc:
            # The client reaches this path for read/write timeouts and malformed
            # success payloads.  It must never create a second Checkout here.
            await self._set_reconciliation_required(checkout)
            raise CheckoutReconciliationRequired() from exc
        except Exception as exc:
            # A custom transport/client failure occurs after the sealed permit
            # was handed to the egress boundary, so the safe classification is
            # also ambiguous.
            await self._set_reconciliation_required(checkout)
            raise CheckoutReconciliationRequired() from exc

        try:
            current = await BillingRepository(self._db).lock_checkout_session(
                builder_id=builder_id,
                checkout_session_id=checkout_session_id,
            )
            if current is None or current.state != "creating":
                raise CheckoutReconciliationRequired()
            current.external_checkout_id = created.checkout_id
            current.state = "active"
            await self._db.commit()
        except Exception as exc:
            await self._rollback()
            # The provider identity was observed but is not safely persisted;
            # force exact-ID reconciliation rather than returning a URL.
            raise CheckoutReconciliationRequired() from exc
        return self._view(current)

    async def _reauthorize_before_post(
        self, *, builder_id: UUID, checkout_session_id: UUID
    ) -> None:
        """Re-read persisted authorization immediately before a creation POST.

        The reservation commit released the rollout lock, so a disable, a
        canary builder change, a closed top-up gate or an invalidated
        capability may have landed since.  This takes the rollout advisory and
        row locks again, re-validates the committed ``creating`` attempt
        against the current control row (canary slot ownership, ``general``
        state, top-up gate and capability) and commits to release the locks.
        Any denial or read error raises, and the caller then sends nothing.

        Residual window: the locks are deliberately not held across HTTP I/O,
        so a transition committed after this commit and before the request
        leaves the process can still race one POST.  That POST carries the
        persisted session UUID and is reconciled like any other attempt.
        """

        await require_billing_checkout_rollout(self._db, builder_id)
        await BillingRepository(self._db).authorize_reserved_checkout_egress(
            environment=settings.environment,
            builder_id=builder_id,
            checkout_session_id=checkout_session_id,
        )
        await self._db.commit()

    async def _resolve_coupon(self, code: str) -> ResolvedCoupon:
        """Resolve eligibility with the legacy rules; never reserve a use.

        Absent, inactive, expired (``expires_at < now``) and exhausted
        (``uses_count >= max_uses``) all share one fixed code so the response
        does not enumerate coupon state.  A 100 % coupon is not a hosted
        payment and is not eligible; out-of-range stored data is invalid.
        """

        result = await self._db.execute(select(Coupon).where(Coupon.code == code))
        coupon = result.scalar_one_or_none()
        if coupon is None or coupon.is_active is not True:
            raise CheckoutCouponRejected()
        expires_at = coupon.expires_at
        if expires_at is not None:
            if expires_at.tzinfo is None or expires_at < self._clock():
                raise CheckoutCouponRejected()
        max_uses = coupon.max_uses
        uses_count = coupon.uses_count
        if type(uses_count) is not int or uses_count < 0:
            raise CheckoutCouponRejected()
        if max_uses is not None and uses_count >= max_uses:
            raise CheckoutCouponRejected()
        percent = coupon.discount_percent
        if type(percent) is not int or not 1 <= percent <= 100:
            raise CheckoutCouponRejected()
        if percent == 100:
            raise CheckoutCouponRejected(COUPON_NOT_ELIGIBLE)
        return ResolvedCoupon(code=coupon.code, discount_percent=percent)

    async def _mark_failed_not_sent(
        self, *, builder_id: UUID, checkout_session_id: UUID
    ) -> None:
        """Conclusively end a ``creating`` attempt whose POST was never sent.

        ``failed`` is non-live (outside the live-intent unique index), is
        allowed by the state CHECK and the canary triggers, and is the outcome
        this service already uses for every conclusive pre-egress failure.
        ``reconciliation_required`` would claim possible provider contact, and
        ``abandoned``/``cancelled``/``expired`` record provider observations.
        A canary attempt keeps its lifetime slot: the budget stays consumed.
        """

        try:
            current = await BillingRepository(self._db).lock_checkout_session(
                builder_id=builder_id,
                checkout_session_id=checkout_session_id,
            )
            if current is not None and current.state == "creating":
                current.state = "failed"
                await self._db.commit()
        except Exception:
            await self._rollback()

    async def _set_reconciliation_required(
        self, checkout: BillingCheckoutSession
    ) -> None:
        try:
            current = await BillingRepository(self._db).lock_checkout_session(
                builder_id=checkout.builder_id,
                checkout_session_id=checkout.id,
            )
            if current is not None and current.state == "creating":
                current.state = "reconciliation_required"
                current.reconciliation_attempts += 1
                await self._db.commit()
        except Exception:
            await self._rollback()

    async def _live_checkout(
        self,
        *,
        builder_id: UUID,
        offer_id: str,
        payment_method: PaymentMethod,
    ) -> BillingCheckoutSession | None:
        result = await self._db.execute(
            select(BillingCheckoutSession)
            .where(
                BillingCheckoutSession.builder_id == builder_id,
                BillingCheckoutSession.offer_id == offer_id,
                BillingCheckoutSession.state.in_(_LIVE_STATES),
            )
            .order_by(BillingCheckoutSession.created_at.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        candidate = result.scalar_one_or_none()
        if candidate is None:
            return None
        snapshot_method = candidate.pricing_snapshot.get("payment_method")
        return candidate if snapshot_method == payment_method else None

    async def _count_active_clinics(self, builder_id: UUID) -> int:
        result = await self._db.execute(
            select(func.count(Tenant.id)).where(
                Tenant.builder_id == builder_id,
                Tenant.is_active.is_(True),
                Tenant.plan_type != "admin",
            )
        )
        count = result.scalar_one()
        if type(count) is not int or count < 0:
            raise CheckoutUnavailable()
        return count

    @staticmethod
    def _snapshot_amount(snapshot: Mapping[str, object]) -> int:
        amount = snapshot.get("amount_cents")
        if type(amount) is not int or amount <= 0:
            raise CheckoutInputRejected()
        return amount

    @staticmethod
    def _view(checkout: BillingCheckoutSession) -> CheckoutView:
        kind: CheckoutKind
        if checkout.intent_kind == "subscription":
            kind = "subscription"
        elif checkout.intent_kind == "topup":
            kind = "topup"
        else:
            raise CheckoutNotFound()
        checkout_url = None
        if checkout.state == "active" and checkout.external_checkout_id is not None:
            # Import lazily to keep the only provider URL construction local to
            # the hardened client boundary.
            from src.services.asaas import checkout_page_url

            checkout_url = checkout_page_url(checkout.external_checkout_id)
        return CheckoutView(
            checkout_session_id=checkout.id,
            kind=kind,
            offer_id=checkout.offer_id,
            state=checkout.state,
            checkout_url=checkout_url,
        )

    async def _rollback(self) -> None:
        try:
            await self._db.rollback()
        except Exception:
            # The original fixed-code failure remains the only safe output.
            return
