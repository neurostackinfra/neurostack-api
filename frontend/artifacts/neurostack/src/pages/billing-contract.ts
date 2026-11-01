import type { BillingUsage, CreditReservations } from "@/components/plan-usage-summary";
import { MILLICREDITS_PER_CREDIT } from "@/components/billing/format";

// Response types, runtime guards and the read helper of the billing routes
// (`src/api/schemas/billing.py`, `src/api/routes/billing.py`). Nothing here
// renders, holds state or defaults a missing value: a payload that does not
// match the final contract is an error, never a partial success.

// ── Types ────────────────────────────────────────────────────────────────────

// `BillingSubscriptionSummaryResponse` in `src/api/schemas/billing.py`.
export interface SubscriptionSummary {
  offer_id: string;
  catalog_version_id: string;
  status: string;
  amount_cents: number;
  current_period_end: string | null;
  payment_state: string;
}

// `BillingLiveCheckoutResponse`: the builder's newest live checkout attempt,
// read by the server (survives a redirect, a refresh and another tab). It has
// no provider URL. `state` stays a string: any value means "still open".
export interface LiveCheckout {
  checkout_session_id: string;
  kind: "subscription" | "topup";
  state: string;
  offer_id: string;
}

// `BillingCheckoutSummaryResponse`: advisory; `POST /checkout` stays the authority.
// With `available: false` the server sends `live_session: null` and
// `topup_commercial_enabled: false` (closed for buying), never "nothing live".
export interface CheckoutAvailability {
  available_offer_ids: string[];
  live_session: LiveCheckout | null;
  available: boolean;
}

// The fields of `BillingSummaryResponse` this page reads. The legacy pricing
// fields (`plan`, `subscription_status`, `base_price`, `total_annual`, ...)
// carry server-side defaults and are deliberately ignored.
export interface BillingSummary {
  active_clinics: number;
  usage: BillingUsage;
  subscription: SubscriptionSummary | null;
  checkout: CheckoutAvailability;
  // Group 6b read contract: true only when the server read back that
  // detached top-ups are commercially open (never set by the browser).
  topup_commercial_enabled: boolean;
}

// `BillingCatalogOfferResponse` / `BillingCatalogResponse`.
export interface CatalogOffer {
  offer_id: string;
  kind: "subscription" | "topup";
  currency: string;
  amount_cents: number;
  credits: number;
  included_clinics: number;
  additional_clinic_amount_cents: number;
}

export interface BillingCatalog {
  catalog_version_id: string;
  offers: CatalogOffer[];
}

/** One authoritative read. Only `ok` carries data; nothing is defaulted. */
export type BillingLoad<T> =
  | { status: "loading" }
  | { status: "ok"; data: T }
  | { status: "unauthorized" }
  | { status: "forbidden" }
  | { status: "unavailable" }
  | { status: "error" };

// `CheckoutResponse` in `src/api/schemas/billing.py`.
export interface CheckoutSession {
  ok?: boolean;
  checkout_session_id: string;
  kind: "subscription" | "topup";
  state: string;
  checkout_url?: string | null;
}

// ── Credit API (`src/api/schemas/credits.py`, `GET /v1/credits/*`) ─────────
// Every amount is integer millicredits (`*_mc`). The Literal unions of the
// schema are closed: a value outside them is contract drift and fails the read,
// except `pending_reasons`, kept as strings so a reason this UI does not know
// yet degrades to a generic, fail-closed pending state.

export type CreditStateName = "active" | "pending" | "grace" | "suspended" | "deletion_due";

export interface CreditStateInfo {
  as_of: string;
  mode: "observational" | "enforced";
  enforcement_at: string | null;
  state: CreditStateName;
  /** `quarantine`, `standalone`, `reconciliation` (schema `PendingReason`). */
  pending_reasons: string[];
  exhausted_at?: string | null;
  suspension_due_at?: string | null;
  deletion_due_at?: string | null;
  export_deadline_at?: string | null;
}

export interface CreditExpiry {
  lot_id: string;
  kind: "monthly" | "topup";
  expires_at: string;
  available_mc: number;
}

export interface CreditBalance {
  as_of: string;
  monthly_available_mc: number;
  monthly_not_started_mc: number;
  topup_available_mc: number;
  available_mc: number;
  open_debt_mc: number;
  net_available_mc: number;
  reserved_mc: number;
  next_expiry?: CreditExpiry | null;
}

export interface CreditDebt {
  as_of: string;
  open_debt_mc: number;
  debts: Array<{
    debt_id: string;
    source_kind: string;
    amount_mc: number;
    settled_mc: number;
    open_mc: number;
    created_at: string;
  }>;
}

export interface CreditThresholds {
  as_of: string;
  capacity_mc: number;
  available_mc: number;
  used_mc: number;
  used_percent: number;
  reached: number[];
  alerts: Array<{ threshold: number; occurred_at: string }>;
}

export interface CreditConsumption {
  as_of: string;
  mode: "observational" | "enforced";
  window: { start: string; end: string; kind: string };
  charged_mc: number;
  charged: Array<{ kind: string; amount_mc: number }>;
  observed_mc: number;
  observed: Array<{ kind: string; amount_mc: number }>;
}

/** `CreditSummaryResponse`: every credit panel from one ledger snapshot. */
export interface CreditSummary {
  as_of: string;
  state: CreditStateInfo;
  balance: CreditBalance;
  expiries: CreditExpiry[];
  debt: CreditDebt;
  reservations: CreditReservations;
  thresholds: CreditThresholds;
  consumption: CreditConsumption;
}

/**
 * `CreditLot`: one grant (monthly allowance or top-up). `lot_id` is an
 * internal identifier: it may key a list, never be rendered.
 */
export interface CreditLot {
  lot_id: string;
  kind: "monthly" | "topup";
  status: "active" | "not_started" | "exhausted" | "expired";
  granted_mc: number;
  available_mc: number;
  consumed_mc: number;
  reversed_mc: number;
  expired_mc: number;
  /** Calendar date `YYYY-MM-DD`. */
  paid_at: string;
  expires_at: string;
  cycle_start?: string | null;
  cycle_end?: string | null;
}

/** `CreditLotsResponse`: every lot, no cursor and no `has_more`. */
export interface CreditLots {
  as_of: string;
  lots: CreditLot[];
}

/** `CreditPackage`: one detached top-up of the ASAAS catalog. */
export interface CreditPackage {
  offer_id: string;
  credits: number;
  amount_cents: number;
  currency: string;
  payment_methods: string[];
  validity_months: number;
}

/** `CreditCatalogResponse`; packages are bought only through `POST /v1/billing/checkout`. */
export interface CreditCatalog {
  catalog_version_id: string;
  millicredits_per_credit: number;
  packages: CreditPackage[];
}

export const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// ── Contract guards ──────────────────────────────────────────────────────────

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function isInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value);
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function hasNumbers(value: unknown, keys: readonly string[]): boolean {
  return isRecord(value) && keys.every((key) => isFiniteNumber(value[key]));
}

const STORAGE_KEYS = [
  "used_bytes",
  "used_mb",
  "limit_bytes",
  "limit_mb",
  "available_bytes",
  "available_mb",
  "overage_bytes",
  "overage_mb",
  "percent_used",
] as const;

function isBillingUsage(value: unknown): value is BillingUsage {
  return (
    isRecord(value) &&
    isRecord(value.trace_period) &&
    typeof value.trace_period.start === "string" &&
    typeof value.trace_period.end === "string" &&
    hasNumbers(value.traces, ["used", "included", "extra_credits", "available", "overage", "percent_used"]) &&
    isRecord(value.storage) &&
    hasNumbers(value.storage.rag, STORAGE_KEYS) &&
    hasNumbers(value.storage.media, STORAGE_KEYS) &&
    isRecord(value.credits)
  );
}

function isLiveCheckout(value: unknown): value is LiveCheckout {
  return (
    isRecord(value) &&
    typeof value.checkout_session_id === "string" &&
    UUID_PATTERN.test(value.checkout_session_id) &&
    (value.kind === "subscription" || value.kind === "topup") &&
    typeof value.state === "string" &&
    value.state.length > 0 &&
    typeof value.offer_id === "string" &&
    value.offer_id.length > 0
  );
}

function isSubscriptionSummary(value: unknown): value is SubscriptionSummary {
  return (
    isRecord(value) &&
    typeof value.offer_id === "string" &&
    typeof value.catalog_version_id === "string" &&
    typeof value.status === "string" &&
    isInteger(value.amount_cents) &&
    (value.current_period_end === null || typeof value.current_period_end === "string") &&
    typeof value.payment_state === "string"
  );
}

export function isBillingSummary(value: unknown): value is BillingSummary {
  if (!isRecord(value)) return false;
  const checkout = value.checkout;
  return (
    isInteger(value.active_clinics) &&
    isBillingUsage(value.usage) &&
    (value.subscription === null || isSubscriptionSummary(value.subscription)) &&
    isRecord(checkout) &&
    typeof checkout.available === "boolean" &&
    Array.isArray(checkout.available_offer_ids) &&
    checkout.available_offer_ids.every((id) => typeof id === "string") &&
    (checkout.live_session === null || isLiveCheckout(checkout.live_session)) &&
    typeof value.topup_commercial_enabled === "boolean"
  );
}

function isCatalogOffer(value: unknown): value is CatalogOffer {
  return (
    isRecord(value) &&
    typeof value.offer_id === "string" &&
    (value.kind === "subscription" || value.kind === "topup") &&
    typeof value.currency === "string" &&
    isInteger(value.amount_cents) &&
    isInteger(value.credits) &&
    isInteger(value.included_clinics) &&
    isInteger(value.additional_clinic_amount_cents)
  );
}

export function isBillingCatalog(value: unknown): value is BillingCatalog {
  return (
    isRecord(value) &&
    typeof value.catalog_version_id === "string" &&
    Array.isArray(value.offers) &&
    value.offers.every(isCatalogOffer)
  );
}

export function isCheckoutSession(value: unknown): value is CheckoutSession {
  if (!value || typeof value !== "object") return false;
  const data = value as Record<string, unknown>;
  return (
    typeof data.checkout_session_id === "string" &&
    UUID_PATTERN.test(data.checkout_session_id) &&
    (data.kind === "subscription" || data.kind === "topup") &&
    typeof data.state === "string" &&
    (data.checkout_url === undefined ||
      data.checkout_url === null ||
      typeof data.checkout_url === "string")
  );
}

const CREDIT_STATES = new Set(["active", "pending", "grace", "suspended", "deletion_due"]);
const LOT_KINDS = new Set(["monthly", "topup"]);
const LOT_STATUSES = new Set(["active", "not_started", "exhausted", "expired"]);

function isStringList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function hasIntegers(value: unknown, keys: readonly string[]): boolean {
  return isRecord(value) && keys.every((key) => isInteger(value[key]));
}

function isCreditState(value: unknown): value is CreditStateInfo {
  return (
    isRecord(value) &&
    typeof value.state === "string" &&
    CREDIT_STATES.has(value.state) &&
    (value.mode === "observational" || value.mode === "enforced") &&
    isStringList(value.pending_reasons)
  );
}

function isCreditExpiry(value: unknown): value is CreditExpiry {
  return (
    isRecord(value) &&
    typeof value.lot_id === "string" &&
    typeof value.kind === "string" &&
    LOT_KINDS.has(value.kind) &&
    typeof value.expires_at === "string" &&
    isInteger(value.available_mc)
  );
}

const BALANCE_KEYS = [
  "monthly_available_mc",
  "monthly_not_started_mc",
  "topup_available_mc",
  "available_mc",
  "open_debt_mc",
  "net_available_mc",
  "reserved_mc",
] as const;

function isCreditReservationList(value: unknown): value is CreditReservations {
  return (
    isRecord(value) &&
    isRecord(value.window) &&
    typeof value.window.start === "string" &&
    typeof value.window.end === "string" &&
    Array.isArray(value.reservations) &&
    value.reservations.every((item) => hasIntegers(item, ["units", "coverage_mc", "charged_mc"])) &&
    isInteger(value.egress_allowance_bytes) &&
    isInteger(value.egress_delivered_bytes)
  );
}

function isAmountList(value: unknown): boolean {
  return (
    Array.isArray(value) &&
    value.every((item) => isRecord(item) && typeof item.kind === "string" && isInteger(item.amount_mc))
  );
}

export function isCreditSummary(value: unknown): value is CreditSummary {
  if (!isRecord(value)) return false;
  const { debt, thresholds, consumption } = value;
  return (
    typeof value.as_of === "string" &&
    isCreditState(value.state) &&
    hasIntegers(value.balance, BALANCE_KEYS) &&
    Array.isArray(value.expiries) &&
    value.expiries.every(isCreditExpiry) &&
    isRecord(debt) &&
    isInteger(debt.open_debt_mc) &&
    Array.isArray(debt.debts) &&
    debt.debts.every(
      (item) => hasIntegers(item, ["open_mc"]) && typeof (item as Record<string, unknown>).debt_id === "string",
    ) &&
    isCreditReservationList(value.reservations) &&
    hasIntegers(thresholds, ["used_percent"]) &&
    isRecord(thresholds) &&
    Array.isArray(thresholds.reached) &&
    thresholds.reached.every(isInteger) &&
    isRecord(consumption) &&
    (consumption.mode === "observational" || consumption.mode === "enforced") &&
    isInteger(consumption.charged_mc) &&
    isAmountList(consumption.charged) &&
    isAmountList(consumption.observed)
  );
}

function isCreditLot(value: unknown): value is CreditLot {
  return (
    isRecord(value) &&
    typeof value.lot_id === "string" &&
    typeof value.kind === "string" &&
    LOT_KINDS.has(value.kind) &&
    typeof value.status === "string" &&
    LOT_STATUSES.has(value.status) &&
    hasIntegers(value, ["granted_mc", "available_mc", "consumed_mc", "reversed_mc", "expired_mc"]) &&
    typeof value.paid_at === "string" &&
    typeof value.expires_at === "string"
  );
}

export function isCreditLots(value: unknown): value is CreditLots {
  return (
    isRecord(value) &&
    typeof value.as_of === "string" &&
    Array.isArray(value.lots) &&
    value.lots.every(isCreditLot)
  );
}

function isCreditPackage(value: unknown): value is CreditPackage {
  return (
    isRecord(value) &&
    typeof value.offer_id === "string" &&
    value.offer_id.length > 0 &&
    hasIntegers(value, ["credits", "amount_cents", "validity_months"]) &&
    typeof value.currency === "string" &&
    isStringList(value.payment_methods)
  );
}

/**
 * The catalog states the unit every `*_mc` amount uses; the screen divides by
 * `MILLICREDITS_PER_CREDIT` to display credits, so another value is contract
 * drift and fails the read (fail closed), never a silently wrong figure.
 */
export function isCreditCatalog(value: unknown): value is CreditCatalog {
  return (
    isRecord(value) &&
    typeof value.catalog_version_id === "string" &&
    value.millicredits_per_credit === MILLICREDITS_PER_CREDIT &&
    Array.isArray(value.packages) &&
    value.packages.every(isCreditPackage)
  );
}

// ── Read helpers ─────────────────────────────────────────────────────────────

export function authHeaders(token: string) {
  return { "Content-Type": "application/json", Authorization: `Bearer ${token}` };
}

/** One read of a billing route; refusals and failures keep their meaning. */
export async function readBilling<T>(
  path: string,
  token: string,
  isValid: (value: unknown) => value is T,
): Promise<BillingLoad<T>> {
  try {
    const res = await fetch(path, { headers: authHeaders(token) });
    if (res.status === 401) return { status: "unauthorized" };
    if (res.status === 403) return { status: "forbidden" };
    if (res.status === 503) return { status: "unavailable" };
    if (res.status !== 200) return { status: "error" };
    const data: unknown = await res.json();
    return isValid(data) ? { status: "ok", data } : { status: "error" };
  } catch {
    return { status: "error" };
  }
}

/**
 * One read of a credit route (`GET /v1/credits/*`). Unlike the allocation
 * notice's `readCredit`, the outcomes stay apart: `unavailable` is ONLY the
 * stable `503 {"code": "credit_unavailable"}` of a credit switch that is not
 * effective (a feature not launched); any other failure -- another 5xx, a
 * network error, a malformed body -- is `error`, shown and retryable.
 */
export async function readCredits<T>(
  path: string,
  token: string,
  isValid: (value: unknown) => value is T,
): Promise<BillingLoad<T>> {
  try {
    const res = await fetch(path, { headers: { Authorization: `Bearer ${token}` } });
    if (res.status === 401) return { status: "unauthorized" };
    if (res.status === 403) return { status: "forbidden" };
    if (res.status === 503) {
      const body: unknown = await res.json().catch(() => null);
      return isRecord(body) && body.code === "credit_unavailable"
        ? { status: "unavailable" }
        : { status: "error" };
    }
    if (res.status !== 200) return { status: "error" };
    const data: unknown = await res.json();
    return isValid(data) ? { status: "ok", data } : { status: "error" };
  } catch {
    return { status: "error" };
  }
}
