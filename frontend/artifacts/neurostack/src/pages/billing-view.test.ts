import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  BillingCatalog,
  BillingLoad,
  BillingSummary,
  CreditCatalog,
  CreditSummary,
  SubscriptionSummary,
} from "./billing-contract";
import {
  PLAN_NAMES,
  creditLedgerStanding,
  deriveBillingView,
  deriveTopupState,
  planName,
  subscriptionMarker,
} from "./billing-view";

// Synthetic, already-validated reads; every value is invented for the tests.
const VERSION = "catalog-test-v1";
const PLUS = "plan_plus_monthly_v1";
const PRO = "plan_pro_monthly_v1";
const MAX = "plan_max_monthly_v1";

const pt = (p: string, _e: string) => p;
const en = (_p: string, e: string) => e;

function offer(offer_id: string, amount_cents: number, credits: number, included_clinics: number, extra: number) {
  return {
    offer_id,
    kind: "subscription" as const,
    currency: "BRL",
    amount_cents,
    credits,
    included_clinics,
    additional_clinic_amount_cents: extra,
  };
}

const catalogData: BillingCatalog = {
  catalog_version_id: VERSION,
  offers: [
    offer(PLUS, 111_100, 1_100, 1, 11_000),
    offer(PRO, 222_200, 2_200, 2, 22_000),
    offer(MAX, 333_300, 3_300, 3, 33_000),
    { ...offer("package_credits_500_v1", 4_400, 500, 0, 0), kind: "topup" },
  ],
};

function subscription(overrides: Partial<SubscriptionSummary> = {}): SubscriptionSummary {
  return {
    offer_id: PRO,
    catalog_version_id: VERSION,
    status: "active",
    amount_cents: 222_200,
    current_period_end: "2026-11-17",
    payment_state: "paid",
    ...overrides,
  };
}

function summaryData(overrides: Partial<BillingSummary> = {}): BillingSummary {
  return {
    active_clinics: 2,
    usage: {} as BillingSummary["usage"],
    subscription: null,
    checkout: { available_offer_ids: [PLUS, PRO, MAX], live_session: null, available: true },
    topup_commercial_enabled: false,
    ...overrides,
  };
}

const ok = <T,>(data: T): BillingLoad<T> => ({ status: "ok", data });
const activeSummary = summaryData({
  subscription: subscription(),
  checkout: { available_offer_ids: [], live_session: null, available: true },
});

function view(
  summary: BillingLoad<BillingSummary>,
  catalog: BillingLoad<BillingCatalog> = ok(catalogData),
  txt = pt,
) {
  return deriveBillingView({ summary, catalog, txt });
}

describe("deriveBillingView", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("is pure: no fetch, same input gives the same view", () => {
    const fetchSpy = vi.fn(() => {
      throw new Error("deriveBillingView must not fetch");
    });
    vi.stubGlobal("fetch", fetchSpy);
    const first = view(ok(activeSummary));
    const second = view(ok(activeSummary));
    expect(second).toEqual(first);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("presents an active subscriber from the summary and the matching catalog version", () => {
    const v = view(ok(activeSummary));

    expect(v.accessDenied).toBe(false);
    expect(v.overviewState).toEqual({
      status: "ready",
      subscription: {
        planName: "Pro",
        status: "active",
        paymentState: "paid",
        amountCents: 222_200,
        currentPeriodEnd: "2026-11-17",
      },
      activeClinics: 2,
      terms: { status: "ready", currency: "BRL", credits: 2_200, includedClinics: 2, additionalClinicAmountCents: 22_000 },
    });
    expect(v.plans.map((plan) => [plan.offerId, plan.marker, plan.canSubscribe])).toEqual([
      [PLUS, null, false],
      [PRO, "current", false],
      [MAX, null, false],
    ]);
    expect(v.comparisonState).toEqual({ status: "ready", plans: v.plans });
    expect(v.plansNotice).toBeNull();
    expect(v.contextAlert).toBeNull();
  });

  it("offers only subscription offers, in catalog order, subscribable from available_offer_ids alone", () => {
    const v = view(ok(summaryData({ checkout: { available_offer_ids: [PRO, "package_credits_500_v1"], live_session: null, available: true } })));

    expect(v.overviewState).toMatchObject({ status: "ready", subscription: null, activeClinics: 2 });
    expect(v.plans.map((plan) => plan.offerId)).toEqual([PLUS, PRO, MAX]);
    expect(v.plans.map((plan) => plan.canSubscribe)).toEqual([false, true, false]);
    expect(v.plans.every((plan) => plan.marker === null)).toBe(true);
    expect(v.contextAlert).toBeNull();
  });

  it("offers nothing when the server says availability is unknown, and says so", () => {
    const v = view(ok(summaryData({ checkout: { available_offer_ids: [PLUS, PRO, MAX], live_session: null, available: false } })));
    expect(v.plans.some((plan) => plan.canSubscribe)).toBe(false);
    expect(v.plansNotice).toMatch(/Não foi possível confirmar agora quais planos/);
  });

  it("says no plan is available when an account without subscription has no listed offer", () => {
    const v = view(ok(summaryData({ checkout: { available_offer_ids: [], live_session: null, available: true } })));
    expect(v.plans.some((plan) => plan.canSubscribe)).toBe(false);
    expect(v.plansNotice).toMatch(/Nenhum plano está disponível/);
  });

  it.each([
    ["pending", /Pagamento pendente: a assinatura só é confirmada/],
    ["overdue", /Pagamento em atraso: o período pago terminou/],
    ["reversed", /último pagamento da assinatura foi estornado/],
  ])("turns a %s payment into a status notice, not an error", (payment_state, message) => {
    const v = view(ok({ ...activeSummary, subscription: subscription({ payment_state }) }));
    expect(v.contextAlert).toEqual({ tone: "status", retry: false, message: expect.stringMatching(message) });
    expect(v.overviewState).toMatchObject({ status: "ready", subscription: { paymentState: payment_state } });
  });

  it("passes a cancelled subscription through with no alert of its own", () => {
    const v = view(ok({ ...activeSummary, subscription: subscription({ status: "cancelled" }) }));
    expect(v.overviewState).toMatchObject({ status: "ready", subscription: { status: "cancelled", planName: "Pro" } });
    expect(v.contextAlert).toBeNull();
  });

  it("keeps an unavailable summary (503) apart from a failed or malformed one (500)", () => {
    const unavailable = view({ status: "unavailable" });
    const failed = view({ status: "error" });

    expect(unavailable.overviewState).toEqual({ status: "unavailable" });
    expect(unavailable.usageState).toEqual({ status: "unavailable" });
    expect(failed.overviewState).toEqual({ status: "error" });
    expect(failed.usageState).toEqual({ status: "error" });
    expect(unavailable.contextAlert).toMatchObject({ tone: "error", retry: true });
    expect(failed.contextAlert).toMatchObject({ tone: "error", retry: true });
    expect(unavailable.contextAlert?.message).toMatch(/temporariamente indisponíveis/);
    expect(failed.contextAlert?.message).toMatch(/Não foi possível carregar/);
    // No summary, no capability: the catalog is listed but nothing is subscribable.
    expect(failed.plans.some((plan) => plan.canSubscribe)).toBe(false);
    expect(failed.plansNotice).toBeNull();
  });

  it("keeps loading as its own state", () => {
    const v = view({ status: "loading" }, { status: "loading" });
    expect(v.overviewState).toEqual({ status: "loading" });
    expect(v.usageState).toEqual({ status: "loading" });
    expect(v.comparisonState).toEqual({ status: "loading" });
    expect(v.contextAlert).toBeNull();
  });

  it.each([
    [{ status: "unauthorized" } as const, ok(catalogData), /sessão expirou/],
    [{ status: "forbidden" } as const, ok(catalogData), /não tem acesso/],
    [ok(activeSummary), { status: "unauthorized" } as const, /sessão expirou/],
    [ok(activeSummary), { status: "forbidden" } as const, /não tem acesso/],
  ])("denies access on a refused read (%o / %o) without a retry", (summary, catalog, message) => {
    const v = view(summary, catalog);
    expect(v.accessDenied).toBe(true);
    expect(v.contextAlert).toEqual({ tone: "error", retry: false, message: expect.stringMatching(message) });
  });

  it("tells a catalog read error, an unavailable catalog and a version mismatch apart", () => {
    const error = view(ok(activeSummary), { status: "error" });
    const unavailable = view(ok(activeSummary), { status: "unavailable" });
    const mismatch = view(ok({ ...activeSummary, subscription: subscription({ catalog_version_id: "catalog-old" }) }));

    expect(error.terms).toEqual({ status: "error" });
    expect(error.comparisonState).toEqual({ status: "error" });
    expect(unavailable.terms).toEqual({ status: "unavailable" });
    expect(unavailable.comparisonState).toEqual({ status: "unavailable" });
    // The catalog answered but no longer carries the version sold: terms are
    // unavailable, the plans still list, and no plan is marked current.
    expect(mismatch.terms).toEqual({ status: "unavailable" });
    expect(mismatch.currentOffer).toBeUndefined();
    expect(mismatch.comparisonState.status).toBe("ready");
    expect(mismatch.plans.some((plan) => plan.marker !== null)).toBe(false);
    for (const v of [error, unavailable, mismatch]) {
      expect(v.overviewState).toMatchObject({ status: "ready", terms: v.terms });
    }
  });

  it("treats a subscription offer missing from the catalog as unavailable terms", () => {
    const v = view(ok({ ...activeSummary, subscription: subscription({ offer_id: "plan_gone_monthly_v1" }) }));
    expect(v.terms).toEqual({ status: "unavailable" });
    expect(v.overviewState).toMatchObject({ subscription: { planName: "plan_gone_monthly_v1" } });
  });

  it("does not offer an offer the server did not list, even when the catalog has it", () => {
    const v = view(ok(summaryData({ checkout: { available_offer_ids: ["plan_other_monthly_v1"], live_session: null, available: true } })));
    expect(v.plans.some((plan) => plan.canSubscribe)).toBe(false);
  });

  it("shows an unknown offer by its raw id", () => {
    const unknown = "plan_future_monthly_v9";
    const v = view(ok(summaryData({ checkout: { available_offer_ids: [unknown], live_session: null, available: true } })), ok({
      catalog_version_id: VERSION,
      offers: [offer(unknown, 1_000, 10, 1, 100)],
    }));
    expect(v.plans).toEqual([expect.objectContaining({ offerId: unknown, name: unknown, canSubscribe: true })]);
    expect(planName(unknown)).toBe(unknown);
    expect(Object.keys(PLAN_NAMES)).toEqual([PLUS, PRO, MAX]);
  });

  it("keeps zero values as zero", () => {
    const zero = offer(PRO, 0, 0, 0, 0);
    const v = view(ok({ ...activeSummary, active_clinics: 0, subscription: subscription({ amount_cents: 0 }) }), ok({
      catalog_version_id: VERSION,
      offers: [zero],
    }));
    expect(v.terms).toEqual({ status: "ready", currency: "BRL", credits: 0, includedClinics: 0, additionalClinicAmountCents: 0 });
    expect(v.overviewState).toMatchObject({ activeClinics: 0, subscription: { amountCents: 0 } });
    expect(v.plans[0]).toMatchObject({ amountCents: 0, credits: 0, includedClinics: 0, additionalClinicAmountCents: 0 });
  });

  it("ignores the legacy default-carrying fields", () => {
    const legacy = {
      ...summaryData(),
      plan: "legacy-plan",
      free_tier: 97,
      base_price: 9_999,
      total_monthly: 9_997,
      subscription_status: "trial",
    } as BillingSummary;
    const v = view(ok(legacy));
    const serialized = JSON.stringify({ ...v, summaryData: null });
    expect(serialized).not.toMatch(/legacy-plan|trial|9999|9997|"97"|:97\b/);
    expect(v.overviewState).toMatchObject({ status: "ready", subscription: null, activeClinics: 2 });
  });

  // Every value of `SubscriptionSummaryStatus` x `PaymentState`
  // (`src/services/billing_read_contract.py`): only the server's fields decide.
  const STATUSES = ["active", "active_linked", "readback_required", "non_billable_pending_readback", "cancelled"] as const;
  const PAYMENT_STATES = ["paid", "overdue", "reversed", "pending"] as const;
  const MARKERS: Record<(typeof STATUSES)[number], Record<(typeof PAYMENT_STATES)[number], "current" | "pending">> = {
    active: { paid: "current", overdue: "current", reversed: "current", pending: "pending" },
    active_linked: { paid: "current", overdue: "current", reversed: "current", pending: "pending" },
    readback_required: { paid: "pending", overdue: "pending", reversed: "pending", pending: "pending" },
    non_billable_pending_readback: { paid: "pending", overdue: "pending", reversed: "pending", pending: "pending" },
    cancelled: { paid: "current", overdue: "current", reversed: "current", pending: "pending" },
  };
  it.each(STATUSES.flatMap((status) => PAYMENT_STATES.map((payment_state) => [status, payment_state] as const)))(
    "marks the subscription's plan for status %s and payment_state %s from the server fields only",
    (status, payment_state) => {
      const expected = MARKERS[status][payment_state];
      expect(subscriptionMarker({ status, payment_state })).toBe(expected);
      const v = view(ok({ ...activeSummary, subscription: subscription({ status, payment_state }) }));
      expect(v.plans.map((plan) => plan.marker)).toEqual([null, expected, null]);
      // No date arithmetic: the period end does not change the marker.
      const noPeriod = view(ok({ ...activeSummary, subscription: subscription({ status, payment_state, current_period_end: null }) }));
      expect(noPeriod.plans[1].marker).toBe(expected);
    },
  );

  it.each([
    ["an unknown status", { status: "future_status", payment_state: "paid" }],
    ["an unknown payment_state", { status: "active", payment_state: "future_payment_state" }],
    ["both unknown", { status: "x", payment_state: "y" }],
    ["an empty status", { status: "", payment_state: "paid" }],
  ])("fails closed on %s: awaiting confirmation, never the current plan", (_label, fields) => {
    expect(subscriptionMarker(fields)).toBe("pending");
    const v = view(ok({ ...activeSummary, subscription: subscription(fields) }));
    expect(v.plans.map((plan) => plan.marker)).toEqual([null, "pending", null]);
  });

  it("marks nothing without a subscription or when the catalog version differs", () => {
    expect(view(ok(summaryData())).plans.every((plan) => plan.marker === null)).toBe(true);
    const pendingOld = view(
      ok({ ...activeSummary, subscription: subscription({ status: "readback_required", payment_state: "pending", catalog_version_id: "old" }) }),
    );
    expect(pendingOld.plans.every((plan) => plan.marker === null)).toBe(true);
  });

  it("writes its copy in the requested locale", () => {
    const v = view({ status: "unavailable" }, ok(catalogData), en);
    expect(v.contextAlert?.message).toMatch(/Payment details are temporarily unavailable/);
  });
});

// ── creditLedgerStanding ─────────────────────────────────────────────────────

describe("creditLedgerStanding", () => {
  const LIFECYCLE = ["active", "grace", "suspended", "deletion_due"] as const;

  it.each(LIFECYCLE.flatMap((state) => ["quarantine", "reconciliation", "a_future_reason"].map((reason) => [state, reason] as const)))(
    "puts the ledger on hold in %s with the reason %s",
    (state, reason) => {
      expect(creditLedgerStanding({ state, pending_reasons: [reason] })).toBe("pending");
    },
  );

  it.each(LIFECYCLE)("leaves %s without a reason standing", (state) => {
    expect(creditLedgerStanding({ state, pending_reasons: [] })).toBe("standing");
  });

  it.each(["active", "grace", "suspended", "deletion_due"] as const)(
    "treats standalone as the only reason in %s as not applicable (fail safe)",
    (state) => {
      expect(creditLedgerStanding({ state, pending_reasons: ["standalone"] })).toBe("not_applicable");
      expect(creditLedgerStanding({ state, pending_reasons: ["standalone", "standalone"] })).toBe("not_applicable");
    },
  );

  it.each(["active", "grace", "suspended", "deletion_due"] as const)(
    "keeps standalone next to another reason in %s a hold",
    (state) => {
      expect(creditLedgerStanding({ state, pending_reasons: ["standalone", "quarantine"] })).toBe("pending");
      expect(creditLedgerStanding({ state, pending_reasons: ["a_future_reason", "standalone"] })).toBe("pending");
    },
  );

  it("keeps the pending state on hold, with any reason or none, and standalone not applicable", () => {
    expect(creditLedgerStanding({ state: "pending", pending_reasons: [] })).toBe("pending");
    expect(creditLedgerStanding({ state: "pending", pending_reasons: ["quarantine", "reconciliation"] })).toBe("pending");
    // `standalone` (no builder) is answered only as `pending` + `["standalone"]`.
    expect(creditLedgerStanding({ state: "pending", pending_reasons: ["standalone"] })).toBe("not_applicable");
    expect(creditLedgerStanding({ state: "pending", pending_reasons: ["standalone", "quarantine"] })).toBe("not_applicable");
  });
});

// ── deriveTopupState ─────────────────────────────────────────────────────────

const P500 = "package_credits_500_v1";
const P1000 = "package_credits_1000_v1";

const creditCatalogData: CreditCatalog = {
  catalog_version_id: VERSION,
  millicredits_per_credit: 1000,
  packages: [
    { offer_id: P500, credits: 500, amount_cents: 4_400, currency: "BRL", payment_methods: ["credit_card", "pix"], validity_months: 6 },
    { offer_id: P1000, credits: 1000, amount_cents: 8_800, currency: "BRL", payment_methods: ["pix", "boleto"], validity_months: 6 },
  ],
};

function credit(state: CreditSummary["state"]["state"], pending_reasons: string[] = []): BillingLoad<CreditSummary> {
  return ok({ state: { state, pending_reasons, mode: "enforced" } } as unknown as CreditSummary);
}

const openSummary = summaryData({
  topup_commercial_enabled: true,
  checkout: { available_offer_ids: [PLUS, P500, P1000], live_session: null, available: true },
});

function topup(overrides: Partial<Parameters<typeof deriveTopupState>[0]> = {}) {
  return deriveTopupState({
    summary: ok(openSummary),
    credit: credit("active"),
    creditCatalog: ok(creditCatalogData),
    checkoutLive: false,
    txt: pt,
    ...overrides,
  });
}

describe("deriveTopupState", () => {
  it("offers the listed packages with their known methods only when every readback is affirmative", () => {
    expect(topup()).toEqual({
      status: "offered",
      packages: [
        { offerId: P500, credits: 500, amountCents: 4_400, currency: "BRL", validityMonths: 6, methods: ["credit_card", "pix"] },
        // An unknown method from the server is not offered; the known one is.
        { offerId: P1000, credits: 1000, amountCents: 8_800, currency: "BRL", validityMonths: 6, methods: ["pix"] },
      ],
    });
  });

  it.each([
    // The packages ARE listed (canary or live attempt), so only the flag hides the action.
    [
      "the flag false (or revoked) while the packages are listed",
      { summary: ok(summaryData({ topup_commercial_enabled: false, checkout: openSummary.checkout })) },
    ],
    ["a loading summary (inconclusive)", { summary: { status: "loading" } as BillingLoad<BillingSummary> }],
    ["a failed summary", { summary: { status: "error" } as BillingLoad<BillingSummary> }],
    ["an unavailable summary", { summary: { status: "unavailable" } as BillingLoad<BillingSummary> }],
    ["a credit switch that is off", { credit: { status: "unavailable" } as BillingLoad<CreditSummary> }],
    ["a failed credit read", { credit: { status: "error" } as BillingLoad<CreditSummary> }],
    ["a loading credit read", { credit: { status: "loading" } as BillingLoad<CreditSummary> }],
    [
      "no package listed by the server",
      { summary: ok(summaryData({ topup_commercial_enabled: true, checkout: { available_offer_ids: [PLUS], live_session: null, available: true } })) },
    ],
    [
      "an advisory read that failed",
      { summary: ok(summaryData({ topup_commercial_enabled: true, checkout: { available_offer_ids: [P500], live_session: null, available: false } })) },
    ],
  ])("is hidden for %s", (_label, overrides) => {
    expect(topup(overrides)).toEqual({ status: "hidden" });
  });

  it.each([["quarantine"], ["reconciliation"], ["a_future_reason"], []])(
    "is blocked, with a reason and no action, while the ledger is pending (%s)",
    (...reasons) => {
      const result = topup({ credit: credit("pending", reasons as string[]) });
      expect(result.status).toBe("blocked");
      expect(result.status === "blocked" && result.reason).toMatch(/pendência financeira/);
    },
  );

  it("is blocked while a checkout is live, and when the packages cannot be read", () => {
    const live = topup({ checkoutLive: true });
    expect(live.status === "blocked" && live.reason).toMatch(/checkout em andamento/);
    const unread = topup({ creditCatalog: { status: "error" } });
    expect(unread.status === "blocked" && unread.reason).toMatch(/pacotes de recarga/);
    const en_ = topup({ checkoutLive: true, txt: en });
    expect(en_.status === "blocked" && en_.reason).toMatch(/checkout is in progress/);
  });

  it.each(["grace", "suspended", "deletion_due"] as const)("still offers top-ups in %s: the ledger asks for one", (state) => {
    expect(topup({ credit: credit(state) }).status).toBe("offered");
  });

  // The server lists a hold reason under a lifecycle state too; it is checked
  // before the lifecycle gate, so it blocks the top-up whatever the state.
  it.each(
    (["grace", "suspended", "deletion_due"] as const).flatMap((state) =>
      ["quarantine", "reconciliation", "a_future_reason"].map((reason) => [state, reason] as const),
    ),
  )("is blocked in %s with the hold reason %s", (state, reason) => {
    const result = topup({ credit: credit(state, [reason]) });
    expect(result.status).toBe("blocked");
    expect(result.status === "blocked" && result.reason).toMatch(/pendência financeira/);
  });

  // A not-applicable ledger (standalone tenant, no credit account) has nothing
  // to top up: no action and no hold notice next to "Não se aplica", even with
  // the flag on and the packages listed (`openSummary`), in every state.
  it.each(["pending", "active", "grace", "suspended", "deletion_due"] as const)(
    "is hidden, with no action and no notice, for a not-applicable ledger in %s",
    (state) => {
      expect(creditLedgerStanding({ state, pending_reasons: ["standalone"] })).toBe("not_applicable");
      expect(topup({ credit: credit(state, ["standalone"]) })).toEqual({ status: "hidden" });
      // A live checkout or an unread catalog does not bring a notice back.
      expect(topup({ credit: credit(state, ["standalone"]), checkoutLive: true })).toEqual({ status: "hidden" });
      expect(topup({ credit: credit(state, ["standalone"]), creditCatalog: { status: "error" } })).toEqual({ status: "hidden" });
    },
  );

  it("is hidden for pending + standalone next to another reason (not applicable), blocked for that pair elsewhere", () => {
    expect(topup({ credit: credit("pending", ["standalone", "quarantine"]) })).toEqual({ status: "hidden" });
    const hold = topup({ credit: credit("grace", ["standalone", "quarantine"]) });
    expect(hold.status === "blocked" && hold.reason).toMatch(/pendência financeira/);
  });

  it("is hidden while the packages are loading: loading is not an error", () => {
    expect(topup({ creditCatalog: { status: "loading" } })).toEqual({ status: "hidden" });
    for (const status of ["error", "unavailable", "forbidden", "unauthorized"] as const) {
      const result = topup({ creditCatalog: { status } });
      expect(result.status === "blocked" && result.reason).toMatch(/pacotes de recarga/);
    }
  });
});
