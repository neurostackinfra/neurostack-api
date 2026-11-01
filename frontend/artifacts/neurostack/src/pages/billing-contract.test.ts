import { afterEach, describe, expect, it, vi } from "vitest";

import {
  authHeaders,
  isBillingCatalog,
  isBillingSummary,
  isCheckoutSession,
  isCreditCatalog,
  isCreditLots,
  isCreditSummary,
  readBilling,
  readCredits,
} from "./billing-contract";

// Synthetic payloads shaped like `src/api/schemas/billing.py`; every value is invented.
const PRO = "plan_pro_monthly_v1";
const SESSION_ID = "0b6f1f0e-3c1a-4e7b-9d55-2f1f6f3f9a10";

function storage() {
  return {
    used_bytes: 0,
    used_mb: 0,
    limit_bytes: 10,
    limit_mb: 10,
    available_bytes: 10,
    available_mb: 10,
    overage_bytes: 0,
    overage_mb: 0,
    percent_used: 0,
  };
}

function usage() {
  return {
    plan: null,
    trace_period: { start: "2026-10-01T00:00:00Z", end: "2026-11-01T00:00:00Z", timezone: "UTC" },
    traces: { used: 0, included: 10, extra_credits: 0, available: 10, overage: 0, percent_used: 0 },
    storage: { rag: storage(), media: storage() },
    credits: { purchase_enabled: false, coming_soon: true },
  };
}

function subscription(overrides: Record<string, unknown> = {}) {
  return {
    offer_id: PRO,
    catalog_version_id: "v1",
    status: "active",
    amount_cents: 12_300,
    current_period_end: "2026-11-17",
    payment_state: "paid",
    ...overrides,
  };
}

function summary(overrides: Record<string, unknown> = {}) {
  return {
    active_clinics: 0,
    usage: usage(),
    subscription: null,
    checkout: { available_offer_ids: [PRO], live_session: null, available: true },
    topup_commercial_enabled: false,
    ...overrides,
  };
}

function offer(overrides: Record<string, unknown> = {}) {
  return {
    offer_id: PRO,
    kind: "subscription",
    currency: "BRL",
    amount_cents: 0,
    credits: 0,
    included_clinics: 0,
    additional_clinic_amount_cents: 0,
    ...overrides,
  };
}

function response(status: number, body: unknown): Response {
  return { status, ok: status >= 200 && status < 300, json: async () => body } as Response;
}

describe("billing contract guards", () => {
  it("accepts the final summary contract with and without a subscription, zeros included", () => {
    expect(isBillingSummary(summary())).toBe(true);
    expect(isBillingSummary(summary({ subscription: subscription() }))).toBe(true);
    expect(isBillingSummary(summary({ subscription: subscription({ current_period_end: null }) }))).toBe(true);
    expect(isBillingSummary(summary({ subscription: subscription({ status: "cancelled", payment_state: "reversed" }) }))).toBe(true);
  });

  it("accepts the legacy default-carrying fields alongside the final contract (they are ignored, not required)", () => {
    const withLegacy = { ...summary(), free_tier: 97, base_price: 9_999, subscription_status: "trial", plan: "legacy" };
    expect(isBillingSummary(withLegacy)).toBe(true);
  });

  it.each(["subscription", "checkout", "topup_commercial_enabled", "active_clinics", "usage"])(
    "rejects a summary missing the final contract field %s",
    (field) => {
      const payload: Record<string, unknown> = summary();
      delete payload[field];
      expect(isBillingSummary(payload)).toBe(false);
    },
  );

  it("rejects the legacy-only shape and malformed fields instead of completing them", () => {
    expect(isBillingSummary({ free_tier: 3, base_price: 9_999, subscription_status: "trial" })).toBe(false);
    expect(isBillingSummary(summary({ subscription: subscription({ amount_cents: 1.5 }) }))).toBe(false);
    expect(isBillingSummary(summary({ subscription: subscription({ current_period_end: 20261117 }) }))).toBe(false);
    expect(isBillingSummary(summary({ checkout: { available_offer_ids: [1], live_session: null, available: true } }))).toBe(false);
    expect(isBillingSummary(summary({ checkout: { available_offer_ids: [], live_session: null } }))).toBe(false);
    expect(isBillingSummary(summary({ active_clinics: "2" }))).toBe(false);
    expect(isBillingSummary(null)).toBe(false);
    expect(isBillingSummary([])).toBe(false);
  });

  it("accepts a catalog of integer offers and rejects a malformed one", () => {
    expect(isBillingCatalog({ catalog_version_id: "v1", offers: [offer(), offer({ kind: "topup" })] })).toBe(true);
    expect(isBillingCatalog({ catalog_version_id: "v1", offers: [] })).toBe(true);
    expect(isBillingCatalog({ catalog_version_id: "v1", offers: [offer({ kind: "addon" })] })).toBe(false);
    expect(isBillingCatalog({ catalog_version_id: "v1", offers: [offer({ credits: "10" })] })).toBe(false);
    expect(isBillingCatalog({ catalog_version_id: "v1", offers: [offer({ amount_cents: 9.9 })] })).toBe(false);
    expect(isBillingCatalog({ offers: [offer()] })).toBe(false);
  });

  it("accepts a checkout session only with a UUID id and a known kind", () => {
    expect(isCheckoutSession({ checkout_session_id: SESSION_ID, kind: "subscription", state: "active", checkout_url: null })).toBe(true);
    expect(isCheckoutSession({ checkout_session_id: SESSION_ID, kind: "topup", state: "creating" })).toBe(true);
    expect(isCheckoutSession({ checkout_session_id: "chk_123", kind: "subscription", state: "active" })).toBe(false);
    expect(isCheckoutSession({ checkout_session_id: SESSION_ID, kind: "plan", state: "active" })).toBe(false);
    expect(isCheckoutSession({ checkout_session_id: SESSION_ID, kind: "topup", state: "active", checkout_url: 1 })).toBe(false);
  });
});

describe("readBilling", () => {
  const fetchMock = vi.fn();

  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("sends the bearer token and returns ok only for a valid 200", async () => {
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockResolvedValue(response(200, summary()));

    const load = await readBilling("/v1/billing/summary", "tok", isBillingSummary);

    expect(load).toEqual({ status: "ok", data: summary() });
    expect(fetchMock).toHaveBeenCalledWith("/v1/billing/summary", { headers: authHeaders("tok") });
    expect(authHeaders("tok")).toEqual({ "Content-Type": "application/json", Authorization: "Bearer tok" });
  });

  it.each([
    [401, "unauthorized"],
    [403, "forbidden"],
    [503, "unavailable"],
    [500, "error"],
    [404, "error"],
    [202, "error"],
  ])("keeps the meaning of a %s answer (%s)", async (status, expected) => {
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockResolvedValue(response(status, summary()));
    expect(await readBilling("/v1/billing/summary", "tok", isBillingSummary)).toEqual({ status: expected });
  });

  it("treats a malformed 200 and a network failure as errors", async () => {
    vi.stubGlobal("fetch", fetchMock);
    const { checkout: _checkout, ...missingCheckout } = summary();
    fetchMock.mockResolvedValueOnce(response(200, missingCheckout));
    expect(await readBilling("/v1/billing/summary", "tok", isBillingSummary)).toEqual({ status: "error" });

    fetchMock.mockResolvedValueOnce({ status: 200, json: async () => Promise.reject(new SyntaxError("bad json")) } as Response);
    expect(await readBilling("/v1/billing/catalog", "tok", isBillingCatalog)).toEqual({ status: "error" });

    fetchMock.mockRejectedValueOnce(new TypeError("network"));
    expect(await readBilling("/v1/billing/catalog", "tok", isBillingCatalog)).toEqual({ status: "error" });
  });
});

// ── Credit API (`src/api/schemas/credits.py`) ────────────────────────────────

const LIVE = { checkout_session_id: SESSION_ID, kind: "topup", state: "active", offer_id: "package_credits_500_v1" };

function creditSummary(state: Record<string, unknown> = {}) {
  const window = { start: "2026-10-25", end: "2026-11-25", kind: "cycle" };
  return {
    as_of: "2026-10-30T15:00:00Z",
    state: { as_of: "2026-10-30T15:00:00Z", mode: "observational", enforcement_at: null, state: "active", pending_reasons: [], ...state },
    balance: {
      as_of: "2026-10-30T15:00:00Z",
      monthly_available_mc: 0,
      monthly_not_started_mc: 0,
      topup_available_mc: 0,
      available_mc: 0,
      open_debt_mc: 0,
      net_available_mc: 0,
      reserved_mc: 0,
      next_expiry: null,
    },
    expiries: [],
    debt: { as_of: "2026-10-30T15:00:00Z", open_debt_mc: 0, debts: [] },
    reservations: {
      as_of: "2026-10-30T15:00:00Z",
      window,
      reservations: [{ category: "rag", units: 0, coverage_mc: 0, charged_mc: 0 }],
      egress_allowance_bytes: 0,
      egress_delivered_bytes: 0,
    },
    thresholds: { as_of: "2026-10-30T15:00:00Z", capacity_mc: 0, available_mc: 0, used_mc: 0, used_percent: 100, reached: [], alerts: [] },
    consumption: { as_of: "2026-10-30T15:00:00Z", mode: "observational", window, charged_mc: 0, charged: [], observed_mc: 0, observed: [] },
  };
}

function lot(overrides: Record<string, unknown> = {}) {
  return {
    lot_id: "0f0e0d0c-0000-4000-8000-00000000a001",
    kind: "monthly",
    status: "active",
    granted_mc: 1_000,
    available_mc: 0,
    consumed_mc: 1_000,
    reversed_mc: 0,
    expired_mc: 0,
    paid_at: "2026-10-25",
    expires_at: "2026-11-25T03:00:00Z",
    ...overrides,
  };
}

const PACKAGE = { offer_id: "package_credits_500_v1", credits: 500, amount_cents: 1, currency: "BRL", payment_methods: ["credit_card", "pix"], validity_months: 6 };

describe("live_session and credit guards", () => {
  it("accepts a well-formed live_session and rejects a malformed one instead of reading it as nothing live", () => {
    expect(isBillingSummary(summary({ checkout: { available_offer_ids: [], live_session: LIVE, available: true } }))).toBe(true);
    for (const bad of [{ ...LIVE, checkout_session_id: "../x" }, { ...LIVE, kind: "plan" }, { ...LIVE, offer_id: "" }, { kind: "topup" }, "x"]) {
      expect(isBillingSummary(summary({ checkout: { available_offer_ids: [], live_session: bad, available: true } }))).toBe(false);
    }
  });

  it("accepts the final credit summary with zeros and any pending reason string, rejects drift", () => {
    expect(isCreditSummary(creditSummary())).toBe(true);
    expect(isCreditSummary(creditSummary({ state: "pending", pending_reasons: ["reconciliation", "a_future_reason"] }))).toBe(true);
    expect(isCreditSummary(creditSummary({ state: "frozen" }))).toBe(false);
    expect(isCreditSummary(creditSummary({ mode: "billing" }))).toBe(false);
    expect(isCreditSummary(creditSummary({ pending_reasons: [1] }))).toBe(false);
    expect(isCreditSummary({ ...creditSummary(), balance: { ...creditSummary().balance, available_mc: 1.5 } })).toBe(false);
    expect(isCreditSummary({ ...creditSummary(), expiries: [{ lot_id: "x", kind: "bonus", expires_at: "t", available_mc: 1 }] })).toBe(false);
  });

  it("accepts lots with closed kinds and statuses only, the empty list included", () => {
    expect(isCreditLots({ as_of: "t", lots: [] })).toBe(true);
    expect(isCreditLots({ as_of: "t", lots: [lot(), lot({ kind: "topup", status: "expired" })] })).toBe(true);
    expect(isCreditLots({ as_of: "t", lots: [lot({ status: "refunded" })] })).toBe(false);
    expect(isCreditLots({ as_of: "t", lots: [lot({ kind: "bonus" })] })).toBe(false);
    expect(isCreditLots({ as_of: "t", lots: [lot({ granted_mc: "1000" })] })).toBe(false);
    expect(isCreditLots({ lots: [] })).toBe(false);
  });

  it("accepts the credit catalog of integer packages and rejects a malformed one", () => {
    const catalog = { catalog_version_id: "v1", millicredits_per_credit: 1000, packages: [PACKAGE] };
    expect(isCreditCatalog(catalog)).toBe(true);
    expect(isCreditCatalog({ ...catalog, packages: [{ ...PACKAGE, amount_cents: 1.5 }] })).toBe(false);
    expect(isCreditCatalog({ ...catalog, packages: [{ ...PACKAGE, payment_methods: "pix" }] })).toBe(false);
    expect(isCreditCatalog({ ...catalog, packages: [{ ...PACKAGE, offer_id: "" }] })).toBe(false);
  });

  it("fails closed when the catalog's unit is not the one the screen divides by", () => {
    const catalog = { catalog_version_id: "v1", millicredits_per_credit: 1000, packages: [PACKAGE] };
    for (const unit of [100, 1_000_000, 0, "1000", 1000.5]) {
      expect(isCreditCatalog({ ...catalog, millicredits_per_credit: unit })).toBe(false);
    }
  });
});

describe("readCredits", () => {
  const fetchMock = vi.fn();

  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("sends only the bearer token and returns ok for a valid 200", async () => {
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockResolvedValue(response(200, creditSummary()));
    expect(await readCredits("/v1/credits/summary", "tok", isCreditSummary)).toEqual({ status: "ok", data: creditSummary() });
    expect(fetchMock).toHaveBeenCalledWith("/v1/credits/summary", { headers: { Authorization: "Bearer tok" } });
  });

  it.each([
    [503, { code: "credit_unavailable" }, "unavailable"],
    [503, { detail: "Service Unavailable" }, "error"],
    [503, null, "error"],
    [401, { detail: "Not authenticated" }, "unauthorized"],
    [403, { detail: { code: "credit_scope_forbidden" } }, "forbidden"],
    [500, {}, "error"],
    [200, { balance: "nope" }, "error"],
  ])("maps a %s answer %j to %s: only the switch-off code is 'unavailable'", async (status, body, expected) => {
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockResolvedValue(response(status, body));
    expect(await readCredits("/v1/credits/summary", "tok", isCreditSummary)).toEqual({ status: expected });
  });

  it("treats a network failure and an unparsable 503 as errors", async () => {
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockRejectedValueOnce(new TypeError("network"));
    expect(await readCredits("/v1/credits/lots", "tok", isCreditLots)).toEqual({ status: "error" });
    fetchMock.mockResolvedValueOnce({ status: 503, json: async () => Promise.reject(new SyntaxError("html")) } as Response);
    expect(await readCredits("/v1/credits/lots", "tok", isCreditLots)).toEqual({ status: "error" });
  });
});
