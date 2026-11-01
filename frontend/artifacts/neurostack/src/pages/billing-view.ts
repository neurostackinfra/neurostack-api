import type { PlanUsageState } from "@/components/plan-usage-summary";
import type { PlanComparisonItem, PlanComparisonState } from "@/components/billing/plan-comparison";
import type { PlanTerms, SubscriptionOverviewState } from "@/components/billing/subscription-overview";
import type { Txt } from "@/components/billing/format";
import type { TopupMethod, TopupState } from "@/components/billing/credits-and-topups";
import type {
  BillingCatalog,
  BillingLoad,
  BillingSummary,
  CatalogOffer,
  CreditCatalog,
  CreditStateInfo,
  CreditSummary,
  SubscriptionSummary,
} from "./billing-contract";

// The only place the payment screen's commercial view is derived. Pure: no
// fetch, no React, no storage. Every value comes from the two authoritative
// reads; a failed, refused or malformed read stays its own state.

// Display names of the catalog's subscription offers. Only the name lives
// here: price, credits, clinics, currency and order come from the catalog,
// and an offer missing from this map is shown by its catalog id.
export const PLAN_NAMES: Readonly<Record<string, string>> = {
  plan_plus_monthly_v1: "Plus",
  plan_pro_monthly_v1: "Pro",
  plan_max_monthly_v1: "Max",
};

export function planName(offerId: string): string {
  return PLAN_NAMES[offerId] ?? offerId;
}

/** Header alert: only actionable states take one; a stable state has none. */
export interface ContextAlert {
  tone: "error" | "status";
  message: string;
  retry: boolean;
}

export interface BillingView {
  summaryData: BillingSummary | null;
  catalogData: BillingCatalog | null;
  subscription: SubscriptionSummary | null;
  /** A 401/403 on either read: nothing financial is shown. */
  accessDenied: boolean;
  /** The current offer, only from the very catalog version it was sold under. */
  currentOffer: CatalogOffer | undefined;
  terms: PlanTerms;
  overviewState: SubscriptionOverviewState;
  usageState: PlanUsageState;
  plans: PlanComparisonItem[];
  comparisonState: PlanComparisonState;
  plansNotice: string | null;
  contextAlert: ContextAlert | null;
}

function paymentNotice(state: string, txt: Txt): string | null {
  switch (state) {
    case "overdue":
      return txt(
        "Pagamento em atraso: o período pago terminou e a renovação ainda não foi confirmada pelo provedor.",
        "Payment overdue: the paid period ended and the provider has not confirmed the renewal yet.",
      );
    case "reversed":
      return txt(
        "O último pagamento da assinatura foi estornado.",
        "The subscription's latest payment was refunded.",
      );
    case "pending":
      return txt(
        "Pagamento pendente: a assinatura só é confirmada depois da leitura do provedor.",
        "Payment pending: the subscription is confirmed only after the provider's readback.",
      );
    default:
      return null;
  }
}

/** The only values that confirm the subscription's plan (allowlist). */
const CONFIRMED_STATUSES: ReadonlySet<string> = new Set(["active", "active_linked", "cancelled"]);
const CONFIRMED_PAYMENT_STATES: ReadonlySet<string> = new Set(["paid", "overdue", "reversed"]);

/**
 * The plan-row marker of the subscription's own offer, from the server's
 * `status` and `payment_state` only (no date arithmetic in the browser).
 * Allowlist, fail closed:
 *
 * - `current`: `status` is `active`, `active_linked` or `cancelled` AND
 *   `payment_state` is `paid`, `overdue` or `reversed`: the plan the account
 *   holds; the overview and the header notice carry the payment state.
 * - `pending`: anything else -- `readback_required`,
 *   `non_billable_pending_readback`, `payment_state` `pending`, or any value
 *   this UI does not know: a record the server has not confirmed.
 */
export function subscriptionMarker(subscription: Pick<SubscriptionSummary, "status" | "payment_state">): "current" | "pending" {
  return CONFIRMED_STATUSES.has(subscription.status) && CONFIRMED_PAYMENT_STATES.has(subscription.payment_state)
    ? "current"
    : "pending";
}

/**
 * The single source of truth for "the credit ledger is on hold":
 *
 * - `not_applicable`: a standalone tenant (no builder, no credit account);
 *   the server answers `pending` with `standalone` and zeros that are not data.
 *   `standalone` as the ONLY reason is not applicable under any state (fail
 *   safe; the server does not produce it today); `standalone` next to another
 *   reason is not applicable under `pending` and a hold under any other state.
 * - `pending` (on hold): `state` is `pending`, or ANY `pending_reasons` entry
 *   other than `standalone` -- `quarantine`, `reconciliation` or a reason this
 *   UI does not know -- whatever the lifecycle state. The server lists the
 *   reason under `grace`, `suspended` and `deletion_due` too, and an enforced
 *   request is refused (`credit_pending`) before the lifecycle gate, so the
 *   balance is withheld and no top-up is offered. Fail closed.
 * - `standing`: no hold; the balance is shown.
 */
export function creditLedgerStanding(
  state: Pick<CreditStateInfo, "state" | "pending_reasons">,
): "not_applicable" | "pending" | "standing" {
  const others = state.pending_reasons.filter((reason) => reason !== "standalone");
  const standalone = others.length < state.pending_reasons.length;
  // `pending` + `standalone` (with or without another reason): as before.
  if (state.state === "pending" && standalone) return "not_applicable";
  // `standalone` as the only reason under any other state: no account either.
  if (standalone && others.length === 0) return "not_applicable";
  if (state.state === "pending" || others.length > 0) return "pending";
  return "standing";
}

export function deriveBillingView({
  summary,
  catalog,
  txt,
}: {
  summary: BillingLoad<BillingSummary>;
  catalog: BillingLoad<BillingCatalog>;
  txt: Txt;
}): BillingView {
  const summaryData = summary.status === "ok" ? summary.data : null;
  const catalogData = catalog.status === "ok" ? catalog.data : null;
  const subscription = summaryData?.subscription ?? null;
  const accessDenied =
    summary.status === "unauthorized" ||
    summary.status === "forbidden" ||
    catalog.status === "unauthorized" ||
    catalog.status === "forbidden";
  // The current offer's terms apply only from the very catalog version the
  // subscription was sold under; anything else is "unavailable", not guessed.
  const currentOffer =
    subscription && catalogData && subscription.catalog_version_id === catalogData.catalog_version_id
      ? catalogData.offers.find(
          (offer) => offer.kind === "subscription" && offer.offer_id === subscription.offer_id,
        )
      : undefined;
  // A catalog that could not be read is an error; one the server reports as
  // unavailable, or that no longer carries the subscription's offer, is
  // unavailable. Neither is guessed.
  const terms: PlanTerms =
    catalog.status === "loading"
      ? { status: "loading" }
      : currentOffer
        ? {
            status: "ready",
            currency: currentOffer.currency,
            credits: currentOffer.credits,
            includedClinics: currentOffer.included_clinics,
            additionalClinicAmountCents: currentOffer.additional_clinic_amount_cents,
          }
        : catalog.status === "error"
          ? { status: "error" }
          : { status: "unavailable" };
  const overviewState: SubscriptionOverviewState =
    summary.status === "ok"
      ? {
          status: "ready",
          subscription: subscription
            ? {
                planName: planName(subscription.offer_id),
                status: subscription.status,
                paymentState: subscription.payment_state,
                amountCents: subscription.amount_cents,
                currentPeriodEnd: subscription.current_period_end,
              }
            : null,
          activeClinics: summary.data.active_clinics,
          terms,
        }
      : summary.status === "unavailable"
        ? { status: "unavailable" }
        : summary.status === "loading"
          ? { status: "loading" }
          : { status: "error" };
  const usageState: PlanUsageState =
    summary.status === "ok"
      ? { status: "ready", usage: summary.data.usage }
      : summary.status === "unavailable"
        ? { status: "unavailable" }
        : summary.status === "loading"
          ? { status: "loading" }
          : { status: "error" };
  // Subscribing is offered only for offers the server lists as available now
  // (advisory; the POST stays the authority). No offer is listed for an active
  // subscription, so no plan-switch action exists.
  const currentMarker = subscription ? subscriptionMarker(subscription) : null;
  const availableOfferIds = new Set(
    summaryData?.checkout.available ? summaryData.checkout.available_offer_ids : [],
  );
  const plans: PlanComparisonItem[] = (catalogData?.offers ?? [])
    .filter((offer) => offer.kind === "subscription")
    .map((offer) => ({
      offerId: offer.offer_id,
      name: planName(offer.offer_id),
      currency: offer.currency,
      amountCents: offer.amount_cents,
      credits: offer.credits,
      includedClinics: offer.included_clinics,
      additionalClinicAmountCents: offer.additional_clinic_amount_cents,
      marker: currentOffer?.offer_id === offer.offer_id ? currentMarker : null,
      canSubscribe: availableOfferIds.has(offer.offer_id),
    }));
  const comparisonState: PlanComparisonState =
    catalog.status === "ok"
      ? { status: "ready", plans }
      : catalog.status === "loading"
        ? { status: "loading" }
        : catalog.status === "unavailable"
          ? { status: "unavailable" }
          : { status: "error" };
  const plansNotice =
    summaryData === null || catalogData === null
      ? null
      : !summaryData.checkout.available
        ? txt(
            "Não foi possível confirmar agora quais planos podem ser contratados. Tente novamente mais tarde.",
            "Which plans can be subscribed to could not be confirmed right now. Try again later.",
          )
        : subscription === null && !plans.some((plan) => plan.canSubscribe)
          ? txt(
              "Nenhum plano está disponível para contratação nesta conta no momento.",
              "No plan is available to subscribe to on this account right now.",
            )
          : null;

  let contextAlert: ContextAlert | null = null;
  if (accessDenied) {
    const expired = summary.status === "unauthorized" || catalog.status === "unauthorized";
    contextAlert = {
      tone: "error",
      retry: false,
      message: expired
        ? txt(
            "Sua sessão expirou. Entre novamente para ver os dados de pagamento.",
            "Your session expired. Sign in again to see the payment details.",
          )
        : txt(
            "Sua conta não tem acesso aos dados de pagamento.",
            "Your account has no access to the payment details.",
          ),
    };
  } else if (summary.status === "unavailable") {
    contextAlert = {
      tone: "error",
      retry: true,
      message: txt(
        "Os dados de pagamento estão temporariamente indisponíveis. Nenhum plano, valor ou consumo foi presumido.",
        "Payment details are temporarily unavailable. No plan, amount or usage was assumed.",
      ),
    };
  } else if (summary.status === "error") {
    contextAlert = {
      tone: "error",
      retry: true,
      message: txt(
        "Não foi possível carregar os dados de pagamento. Nenhum plano, valor ou consumo foi presumido.",
        "The payment details could not be loaded. No plan, amount or usage was assumed.",
      ),
    };
  } else if (subscription) {
    const notice = paymentNotice(subscription.payment_state, txt);
    if (notice) contextAlert = { tone: "status", retry: false, message: notice };
  }

  return {
    summaryData,
    catalogData,
    subscription,
    accessDenied,
    currentOffer,
    terms,
    overviewState,
    usageState,
    plans,
    comparisonState,
    plansNotice,
    contextAlert,
  };
}

const TOPUP_METHODS: readonly TopupMethod[] = ["credit_card", "pix"];

/**
 * Whether `Recarregar` exists, from server readbacks only. Fail closed:
 *
 * - `hidden` (no action, no copy) unless the billing summary was read and says
 *   `topup_commercial_enabled === true` AND the credit ledger answered: a
 *   flag that is false, a summary that failed or is loading (inconclusive),
 *   a credit switch that is off, a failed credit read: nothing is offered.
 *   There is no revoked/lease signal in the contract, so "revoked" is the
 *   flag read back false.
 * - `blocked` (no action, a textual reason) while the ledger is on hold
 *   (`creditLedgerStanding`: any reason other than `standalone`, under any
 *   lifecycle state, or the `pending` state itself), while a checkout is live
 *   (the summary's `live_session`, or this tab's stored session, read or not
 *   yet read), or when the packages could not be read.
 * - `hidden` while the packages are still loading: loading is not an error.
 * - `offered` with the server's packages (`GET /v1/credits/catalog`) that the
 *   summary lists in `checkout.available_offer_ids`, and only their known
 *   payment methods. The client later sends `offer_id` and `method` only.
 */
export function deriveTopupState({
  summary,
  credit,
  creditCatalog,
  checkoutLive,
  txt,
}: {
  summary: BillingLoad<BillingSummary>;
  credit: BillingLoad<CreditSummary>;
  creditCatalog: BillingLoad<CreditCatalog>;
  /** A checkout is open or unread (summary `live_session`, tracked or stored session). */
  checkoutLive: boolean;
  txt: Txt;
}): TopupState {
  if (summary.status !== "ok" || summary.data.topup_commercial_enabled !== true) {
    return { status: "hidden" };
  }
  if (credit.status !== "ok") return { status: "hidden" };
  if (creditLedgerStanding(credit.data.state) !== "standing") {
    return {
      status: "blocked",
      reason: txt(
        "Recarga indisponível enquanto há uma pendência financeira em verificação pelo servidor.",
        "Top-ups are unavailable while the server is verifying a financial pending item.",
      ),
    };
  }
  if (checkoutLive) {
    return {
      status: "blocked",
      reason: txt(
        "Recarga indisponível enquanto há um checkout em andamento nesta conta. Ela volta quando o servidor confirmar o estado dele.",
        "Top-ups are unavailable while a checkout is in progress on this account. They return once the server confirms its state.",
      ),
    };
  }
  if (creditCatalog.status === "loading") return { status: "hidden" };
  if (creditCatalog.status !== "ok") {
    return {
      status: "blocked",
      reason: txt(
        "Não foi possível ler os pacotes de recarga agora. Nenhum valor foi presumido.",
        "The top-up packages could not be read right now. No amount was assumed.",
      ),
    };
  }
  const listed = new Set(
    summary.data.checkout.available ? summary.data.checkout.available_offer_ids : [],
  );
  const packages = creditCatalog.data.packages
    .filter((pkg) => listed.has(pkg.offer_id))
    .map((pkg) => ({
      offerId: pkg.offer_id,
      credits: pkg.credits,
      amountCents: pkg.amount_cents,
      currency: pkg.currency,
      validityMonths: pkg.validity_months,
      methods: TOPUP_METHODS.filter((method) => pkg.payment_methods.includes(method)),
    }))
    .filter((pkg) => pkg.methods.length > 0);
  return packages.length > 0 ? { status: "offered", packages } : { status: "hidden" };
}
