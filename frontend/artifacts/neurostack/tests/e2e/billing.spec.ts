import { test, expect } from "@playwright/test";
import type { Page, Route } from "@playwright/test";

// ─────────────────────────────────────────────────────────────────────────────
// Payment screen (`/billing`) E2E — wish `tela-pagamentos`, Group 2.
//
// The network is fully mocked: every `/v1/*` call is answered from the
// synthetic fixtures below; EVERY same-origin `fetch`/`xhr` request without a
// fixture (`/v1/*`, `/api/public/*` that Vite would proxy, anything else) is
// recorded and fails the test, and every unmocked request under a proxied
// prefix (`/v1/`, `/api/`) is aborted, so it never reaches Vite's proxy or a
// backend that happens to run; every cross-origin request is aborted (also
// recorded, except the web-font requests of `index.html`/`index.css`, which
// are aborted on purpose so the pages render with the pinned image's fallback
// font instead of a font downloaded at run time).
//
// The credit fixtures are coherent with the read contract
// (`src/api/routes/credits.py`, `docs/billing/credits.md`): thresholds come
// from the same book as the balance (nothing available is 100 % used), an
// observational ledger holds only grants (no debit, no reservation charge),
// and an account without a paid cycle has no monthly lot.
//
// Every value is synthetic: a hand-assembled JWT that no server signed, UUIDs
// in the `…-4000-8000-…` test range, `example.test` addresses and invented
// clinic names. Plan prices mirror the catalog's integer cents only because
// the widest real amounts are what the overflow assertions must hold for.
//
// Four approved states × desktop/mobile = eight baselines (deliverable 8).
// ─────────────────────────────────────────────────────────────────────────────

const BUILDER_ID = "00000000-0000-4000-8000-0000000b0001";
const CLINIC_ID = "00000000-0000-4000-8000-0000000c0001";
const CATALOG_VERSION = "catalog-e2e-v1";
const PLUS = "plan_plus_monthly_v1";
const PRO = "plan_pro_monthly_v1";
const MAX = "plan_max_monthly_v1";
const TOPUPS = ["package_credits_500_v1", "package_credits_1000_v1", "package_credits_2000_v1", "package_credits_4000_v1"];
const AS_OF = "2026-10-30T15:00:00Z";

const FAKE_JWT_PAYLOAD = Buffer.from(
  JSON.stringify({ sub: "e2e-builder", role: "builder", builder_id: BUILDER_ID, exp: 9999999999 }),
).toString("base64url");
const FAKE_JWT = `eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.${FAKE_JWT_PAYLOAD}.fake-signature`;

// ── Fixtures (shapes of src/api/schemas/billing.py and credits.py) ──────────

function storage(usedMb: number, limitMb: number) {
  const mb = 1_000_000;
  return {
    used_bytes: usedMb * mb,
    used_mb: usedMb,
    limit_bytes: limitMb * mb,
    limit_mb: limitMb,
    available_bytes: (limitMb - usedMb) * mb,
    available_mb: limitMb - usedMb,
    overage_bytes: 0,
    overage_mb: 0,
    percent_used: (usedMb / limitMb) * 100,
  };
}

const USAGE = {
  plan: null,
  as_of: AS_OF,
  trace_period: { start: "2026-10-01T03:00:00Z", end: "2026-11-01T03:00:00Z", timezone: "America/Sao_Paulo" },
  traces: { used: 1_240, included: 25_000, extra_credits: 0, available: 23_760, overage: 0, percent_used: 4.96 },
  storage: { rag: storage(180, 2_500), media: storage(420, 5_000) },
  credits: { purchase_enabled: false, coming_soon: true },
};

function planOffer(offer_id: string, amount_cents: number, credits: number, included_clinics: number, extra: number) {
  return {
    offer_id,
    kind: "subscription",
    currency: "BRL",
    amount_cents,
    credits,
    included_clinics,
    additional_clinic_amount_cents: extra,
  };
}

const CATALOG = {
  catalog_version_id: CATALOG_VERSION,
  offers: [
    planOffer(PLUS, 99_900, 1_000, 1, 20_000),
    planOffer(PRO, 250_000, 2_500, 3, 15_000),
    planOffer(MAX, 500_000, 5_000, 10, 10_000),
  ],
};

const CREDIT_CATALOG = {
  catalog_version_id: CATALOG_VERSION,
  millicredits_per_credit: 1000,
  units: {
    trace_mc: 1000,
    rag_characters_per_credit: 5000,
    media_bytes_per_credit: 20_000_000,
    egress_included_bytes_per_credit: 200_000_000,
    egress_overage_bytes_per_credit: 100_000_000,
  },
  packages: TOPUPS.map((offer_id, index) => ({
    offer_id,
    credits: [500, 1000, 2000, 4000][index],
    amount_cents: [4_900, 9_500, 18_500, 36_000][index],
    currency: "BRL",
    payment_methods: ["credit_card", "pix"],
    validity_months: 6,
  })),
  subscription_payment_methods: ["credit_card"],
  subscription_cycle: "monthly",
  checkout_endpoint: "/v1/billing/checkout",
};

function summary(overrides: Record<string, unknown>) {
  return {
    // Legacy default-carrying fields: present, ignored by the page.
    plan: null,
    free_tier: 2,
    extra_clinics: 0,
    base_price: 2000,
    extra_price: 0,
    total_monthly: 2000,
    total_annual: 20400,
    discount_pct: 15,
    subscription_status: "trial",
    billing_cycle: "monthly",
    active_clinics: 2,
    usage: USAGE,
    subscription: null,
    checkout: { available_offer_ids: [], live_session: null, available: true },
    topup_commercial_enabled: false,
    ...overrides,
  };
}

const CYCLE = { start: "2026-10-17", end: "2026-11-17", kind: "cycle" };
const CALENDAR = { start: "2026-10-01", end: "2026-11-01", kind: "calendar" };
const NO_RESERVATIONS = [
  { category: "rag", units: 0, coverage_mc: 0, charged_mc: 0 },
  { category: "media", units: 0, coverage_mc: 0, charged_mc: 0 },
  { category: "egress", units: 0, coverage_mc: 0, charged_mc: 0 },
];

/**
 * One `CreditSummaryResponse` from one book. `lots` are the lots in force
 * (spendable now); balance, expiries and thresholds are derived from them the
 * way the server derives them (`_used_percent`, `thresholds_reached`): no
 * debit and no debt in these fixtures, so net available equals available.
 */
function creditSummary({
  state = "active",
  pending_reasons = [] as string[],
  mode = "observational",
  window = CYCLE,
  lots = [] as Array<{ lot_id: string; kind: "monthly" | "topup"; available_mc: number; expires_at: string }>,
  observed = [] as Array<{ kind: string; amount_mc: number }>,
} = {}) {
  const monthly = lots.filter((lot) => lot.kind === "monthly").reduce((sum, lot) => sum + lot.available_mc, 0);
  const topup = lots.filter((lot) => lot.kind === "topup").reduce((sum, lot) => sum + lot.available_mc, 0);
  const available = monthly + topup;
  const expiries = [...lots].sort((a, b) => (a.expires_at < b.expires_at ? -1 : 1));
  return {
    as_of: AS_OF,
    state: {
      as_of: AS_OF,
      mode,
      enforcement_at: mode === "enforced" ? "2026-10-01T03:00:00Z" : null,
      state,
      pending_reasons,
      exhausted_at: null,
      suspension_due_at: null,
      deletion_due_at: null,
      export_deadline_at: null,
    },
    balance: {
      as_of: AS_OF,
      monthly_available_mc: monthly,
      monthly_not_started_mc: 0,
      topup_available_mc: topup,
      available_mc: available,
      open_debt_mc: 0,
      net_available_mc: available,
      reserved_mc: 0,
      next_expiry: expiries[0] ?? null,
    },
    expiries,
    debt: { as_of: AS_OF, open_debt_mc: 0, debts: [] },
    reservations: {
      as_of: AS_OF,
      window,
      reservations: NO_RESERVATIONS,
      egress_allowance_bytes: 0,
      egress_delivered_bytes: 0,
    },
    // Nothing consumed from the ledger: capacity in force equals available;
    // nothing available at all reports 100 % and every threshold reached.
    thresholds: {
      as_of: AS_OF,
      capacity_mc: available,
      available_mc: available,
      used_mc: 0,
      used_percent: available <= 0 ? 100 : 0,
      reached: available <= 0 ? [50, 90, 100] : [],
      alerts: [],
    },
    consumption: {
      as_of: AS_OF,
      mode,
      window,
      charged_mc: 0,
      charged: [],
      observed_mc: observed.reduce((sum, item) => sum + item.amount_mc, 0),
      observed,
    },
  };
}

// 01-active, observational: the ledger holds only grants, so nothing was
// ever consumed; the September allowance expired whole.
const LOTS = {
  as_of: AS_OF,
  lots: [
    {
      lot_id: "00000000-0000-4000-8000-00000000a003",
      kind: "monthly",
      status: "expired",
      granted_mc: 2_500_000,
      available_mc: 0,
      consumed_mc: 0,
      reversed_mc: 0,
      expired_mc: 2_500_000,
      paid_at: "2026-09-17",
      expires_at: "2026-10-17T03:00:00Z",
      cycle_start: "2026-09-17",
      cycle_end: "2026-10-17",
    },
    {
      lot_id: "00000000-0000-4000-8000-00000000a002",
      kind: "topup",
      status: "active",
      granted_mc: 500_000,
      available_mc: 500_000,
      consumed_mc: 0,
      reversed_mc: 0,
      expired_mc: 0,
      paid_at: "2026-10-12",
      expires_at: "2027-04-12T03:00:00Z",
    },
    {
      lot_id: "00000000-0000-4000-8000-00000000a001",
      kind: "monthly",
      status: "active",
      granted_mc: 2_500_000,
      available_mc: 2_500_000,
      consumed_mc: 0,
      reversed_mc: 0,
      expired_mc: 0,
      paid_at: "2026-10-17",
      expires_at: "2026-11-17T03:00:00Z",
      cycle_start: "2026-10-17",
      cycle_end: "2026-11-17",
    },
  ],
};

// 02-pending: no paid cycle, so no monthly lot; only an old top-up that
// expired whole before the enforcement started.
const PENDING_LOTS = {
  as_of: AS_OF,
  lots: [
    {
      lot_id: "00000000-0000-4000-8000-00000000a004",
      kind: "topup",
      status: "expired",
      granted_mc: 500_000,
      available_mc: 0,
      consumed_mc: 0,
      reversed_mc: 0,
      expired_mc: 500_000,
      paid_at: "2026-03-02",
      expires_at: "2026-09-02T03:00:00Z",
    },
  ],
};

type Answer = { status?: number; json: unknown };
type Fixture = Record<string, Answer>;

const CREDIT_OFF: Answer = { status: 503, json: { code: "credit_unavailable" } };

// The approved states (deliverable 8).
const STATES: Record<string, Fixture> = {
  "01-active": {
    "GET /v1/billing/summary": {
      json: summary({
        subscription: {
          offer_id: PRO,
          catalog_version_id: CATALOG_VERSION,
          status: "active",
          amount_cents: 250_000,
          current_period_end: "2026-11-17",
          payment_state: "paid",
        },
        checkout: { available_offer_ids: TOPUPS, live_session: null, available: true },
        topup_commercial_enabled: true,
      }),
    },
    "GET /v1/credits/summary": {
      json: creditSummary({
        lots: [
          { lot_id: "00000000-0000-4000-8000-00000000a001", kind: "monthly", available_mc: 2_500_000, expires_at: "2026-11-17T03:00:00Z" },
          { lot_id: "00000000-0000-4000-8000-00000000a002", kind: "topup", available_mc: 500_000, expires_at: "2027-04-12T03:00:00Z" },
        ],
        // Observed only: what the observational metering would have debited.
        observed: [
          { kind: "rag", amount_mc: 18_000 },
          { kind: "trace", amount_mc: 611_500 },
          { kind: "media", amount_mc: 16_000 },
        ],
      }),
    },
    "GET /v1/credits/lots": { json: LOTS },
    "GET /v1/credits/catalog": { json: CREDIT_CATALOG },
    // A top-up POST that answers without a provider URL (session `creating`).
    "POST /v1/billing/checkout": {
      json: { ok: true, checkout_session_id: "00000000-0000-4000-8000-0000000d0003", kind: "topup", state: "creating", checkout_url: null },
    },
  },
  "02-pending": {
    "GET /v1/billing/summary": {
      json: summary({
        subscription: {
          offer_id: PRO,
          catalog_version_id: CATALOG_VERSION,
          status: "readback_required",
          amount_cents: 250_000,
          current_period_end: null,
          payment_state: "pending",
        },
        // What the server lists (`billing_read_contract.py`): the offer of the
        // live attempt is always listed, and with the flag true (general
        // rollout) every top-up package too.
        checkout: {
          available_offer_ids: [PRO, ...TOPUPS],
          live_session: {
            checkout_session_id: "00000000-0000-4000-8000-0000000d0001",
            kind: "subscription",
            state: "paid_pending_subscription",
            offer_id: PRO,
          },
          available: true,
        },
        topup_commercial_enabled: true,
      }),
    },
    "GET /v1/credits/summary": {
      json: creditSummary({ state: "pending", pending_reasons: ["reconciliation"], mode: "enforced", window: CALENDAR }),
    },
    "GET /v1/credits/lots": { json: PENDING_LOTS },
    "GET /v1/credits/catalog": { json: CREDIT_CATALOG },
  },
  "03-no-subscription": {
    "GET /v1/billing/summary": {
      json: summary({
        active_clinics: 0,
        // Test construct: every package listed while the commercial flag is
        // false, so only the flag can keep `Recarregar` away. The server lists
        // packages with the flag false only in the canary rollout (just
        // `package_credits_500_v1`, and then only `plan_plus_monthly_v1` among
        // the plans: `canary_reservation_admits`) or for a live attempt's
        // offer; it does not produce this exact combination.
        checkout: { available_offer_ids: [PLUS, PRO, MAX, ...TOPUPS], live_session: null, available: true },
        topup_commercial_enabled: false,
      }),
    },
    // No subscription, no grant: zero balance, 100 % used, nothing consumed.
    "GET /v1/credits/summary": { json: creditSummary({ window: CALENDAR }) },
    "GET /v1/credits/lots": { json: { as_of: AS_OF, lots: [] } },
    "GET /v1/credits/catalog": { json: CREDIT_CATALOG },
  },
  "04-summary-error": {
    "GET /v1/billing/summary": { status: 500, json: { detail: "Internal Server Error" } },
    "GET /v1/credits/summary": CREDIT_OFF,
  },
};

const SHELL: Fixture = {
  "GET /v1/billing/catalog": { json: CATALOG },
  "GET /v1/config": { json: { clinic_id: CLINIC_ID, clinic_name: "Clínica Sintética E2E", bot_active: true } },
  "GET /v1/builder/clinics": { json: [{ id: CLINIC_ID, name: "Clínica Sintética E2E" }] },
};

// ── Harness ──────────────────────────────────────────────────────────────────

const FONT_HOSTS = new Set(["fonts.googleapis.com", "fonts.gstatic.com"]);

async function mockNetwork(page: Page, fixture: Fixture, baseURL: string) {
  const origin = new URL(baseURL).origin;
  const answers: Fixture = { ...SHELL, ...fixture };
  const unmocked: string[] = [];
  const posted: string[] = [];
  // Every same-origin fetch/xhr, whatever its path (`/v1/*`, `/api/public/*`
  // that Vite would proxy to a real backend, ...), must have a fixture.
  page.on("request", (request) => {
    if (!["fetch", "xhr"].includes(request.resourceType())) return;
    const url = new URL(request.url());
    if (url.origin !== origin) return;
    const key = `${request.method()} ${url.pathname}`;
    if (!answers[key]) unmocked.push(key);
  });
  await page.route(
    (url) => url.origin !== origin,
    async (route: Route) => {
      const url = new URL(route.request().url());
      if (!FONT_HOSTS.has(url.hostname)) unmocked.push(`${route.request().method()} ${url.href}`);
      await route.abort();
    },
  );
  // The prefixes Vite proxies to a backend (`vite.config.ts`): answered from
  // a fixture or aborted, never forwarded.
  await page.route(
    (url) => url.origin === origin && (url.pathname.startsWith("/v1/") || url.pathname.startsWith("/api/")),
    async (route: Route) => {
      const request = route.request();
      const key = `${request.method()} ${new URL(request.url()).pathname}`;
      if (request.method() !== "GET") posted.push(key);
      const answer = answers[key];
      if (!answer) {
        // fetch/xhr are already recorded by the request listener.
        if (!["fetch", "xhr"].includes(request.resourceType())) unmocked.push(key);
        await route.abort();
        return;
      }
      await route.fulfill({ status: answer.status ?? 200, json: answer.json });
    },
  );
  return { unmocked, posted };
}

async function signIn(page: Page, locale: "pt" | "en") {
  await page.addInitScript(
    ({ token, builderId, clinicId, locale: lang }) => {
      window.localStorage.setItem("ns_locale", lang);
      window.localStorage.setItem(
        "ns_auth",
        JSON.stringify({
          token,
          email: "builder@example.test",
          clinicId,
          tenantId: clinicId,
          userId: "00000000-0000-4000-8000-0000000e0001",
          builderId,
          role: "builder",
          viewingClinicId: clinicId,
          leadSubmitted: true,
        }),
      );
    },
    { token: FAKE_JWT, builderId: BUILDER_ID, clinicId: CLINIC_ID, locale },
  );
}

async function openBilling(page: Page, state: keyof typeof STATES, baseURL: string, locale: "pt" | "en" = "pt") {
  const network = await mockNetwork(page, STATES[state], baseURL);
  await signIn(page, locale);
  await page.goto("/billing");
  await expect(page.getByRole("heading", { level: 1, name: locale === "en" ? "Payment" : "Pagamento" })).toBeVisible({
    timeout: 15_000,
  });
  // Settled: overview read (or failed), plans read, credits read. With the
  // credit switch off (04) the credits area does not exist at all.
  await expect(page.getByTestId("subscription-overview")).not.toHaveAttribute("data-state", "loading");
  if (state === "04-summary-error") {
    await expect(page.getByTestId("billing-area")).toHaveCount(2);
    await expect(page.getByTestId("plan-comparison-scroll")).toBeVisible();
  } else {
    await expect(page.getByTestId("billing-area")).toHaveCount(3);
    await expect(page.getByTestId("credits-and-topups")).not.toHaveAttribute("data-state", "loading");
    await expect(page.getByTestId("credit-history")).not.toHaveAttribute("data-state", "loading");
  }
  await page.addStyleTag({
    content:
      "*, *::before, *::after { animation: none !important; transition: none !important; caret-color: transparent !important; }",
  });
  return network;
}

async function horizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
}

// A cold Vite dev server optimizes dependencies on the first page load and
// may answer that load with stale chunks (a blank page until it reloads).
// One warm-up visit per worker, before any assertion, absorbs it; the tests
// themselves keep their strict waits.
test.beforeAll(async ({ browser, baseURL }) => {
  // Per attempt: navigation at most 15 s, then the page at most 25 s, so 40 s;
  // two full attempts take at most 80 s, 10 s under the 90 s hook timeout
  // (the default is 30 s), which leaves room for opening and closing the pages
  // and for the explicit error below instead of a generic hook timeout.
  const GOTO_TIMEOUT = 15_000;
  const READY_TIMEOUT = 25_000;
  test.setTimeout(90_000);
  let ready = false;
  let lastError: unknown = null;
  for (let attempt = 0; attempt < 2 && !ready; attempt += 1) {
    // A fresh page per attempt: after a failed navigation Chromium loads its
    // own error page, which would interrupt a second `goto` on the same page.
    // A navigation error is retried like a page that never rendered.
    const page = await browser.newPage();
    try {
      await mockNetwork(page, STATES["01-active"], baseURL!);
      await signIn(page, "pt");
      await page.goto(`${baseURL}/billing`, { timeout: GOTO_TIMEOUT });
      await page.getByTestId("billing-area").first().waitFor({ timeout: READY_TIMEOUT });
      ready = true;
    } catch (error: unknown) {
      lastError = error;
    } finally {
      await page.close();
    }
  }
  // Both attempts failed: fail here, loudly, instead of letting the first
  // real test fail on a cold dev server with a confusing message.
  if (!ready) {
    throw new Error(
      `Warm-up failed: ${baseURL}/billing never rendered [data-testid="billing-area"] in 2 attempts ` +
        `(navigation ${GOTO_TIMEOUT / 1000} s + page ${READY_TIMEOUT / 1000} s each). ` +
        `Last error: ${lastError instanceof Error ? lastError.message : String(lastError)}`,
    );
  }
});

// ── Baselines: 4 states × 2 projects = 8 PNGs ───────────────────────────────

test.describe("Pagamento — approved state baselines", () => {
  for (const state of Object.keys(STATES) as Array<keyof typeof STATES>) {
    test(`${state}`, async ({ page, baseURL }, testInfo) => {
      const network = await openBilling(page, state, baseURL!);

      switch (state) {
        case "01-active":
          await expect(page.getByTestId("subscription-status")).toHaveText("Ativa · Pagamento em dia");
          await expect(page.getByTestId(`plan-row-${PRO}`).getByTestId("plan-marker")).toHaveText("Plano atual");
          await expect(page.getByTestId(`plan-row-${PRO}`)).toHaveAttribute("aria-current", "true");
          await expect(page.getByRole("button", { name: "Recarregar" })).toBeVisible();
          await expect(page.getByTestId("credit-history-row")).toHaveCount(3);
          // Observational: grants only, nothing charged or reserved.
          await expect(page.getByRole("group", { name: "Disponível líquido" })).toHaveText(/3\.000 créditos/);
          await expect(page.getByRole("group", { name: "Reservas do ciclo" })).toHaveText(/0 créditos/);
          await expect(page.getByTestId("credit-consumption")).toContainText("Consumo estimado no ciclo (não cobrado)");
          break;
        case "02-pending":
          await expect(page.getByTestId("checkout-status")).toContainText("aguardando confirmação da assinatura");
          // The plan is not shown as current while the server has not confirmed it.
          await expect(page.getByTestId(`plan-row-${PRO}`).getByTestId("plan-marker")).toHaveText("Aguardando confirmação");
          await expect(page.getByTestId(`plan-row-${PRO}`)).not.toHaveAttribute("aria-current");
          await expect(page.getByText("Plano atual")).toHaveCount(0);
          // The live attempt's offer is listed: its action stays disabled,
          // with the live-checkout reason associated.
          {
            const subscribe = page.getByTestId(`plan-row-${PRO}`).getByRole("button", { name: "Assinar Pro" });
            await expect(subscribe).toBeVisible();
            await expect(subscribe).toBeDisabled();
            const describedBy = await subscribe.getAttribute("aria-describedby");
            expect(describedBy).toBeTruthy();
            const reason = page.locator(`[id="${describedBy}"]`);
            await expect(reason).toHaveCount(1);
            await expect(reason).toBeVisible();
            await expect(reason).toContainText("Há um checkout em andamento nesta conta.");
            await expect(subscribe).toHaveAccessibleDescription(/Há um checkout em andamento nesta conta/);
            await expect(page.getByRole("button", { name: /^Assinar / })).toHaveCount(1);
          }
          // The ledger hold blocks the top-up with ITS reason, apart from the
          // live checkout (which would block with another one).
          await expect(page.getByTestId("topup-blocked")).toHaveText(
            "Recarga indisponível enquanto há uma pendência financeira em verificação pelo servidor.",
          );
          await expect(page.getByRole("button", { name: "Recarregar" })).toHaveCount(0);
          await expect(page.getByTestId("credit-state")).toHaveAttribute("data-state", "pending");
          await expect(page.getByTestId("credit-state")).toContainText("em conciliação pelo servidor");
          // No paid cycle: no monthly lot in the history.
          await expect(page.getByTestId("credit-history-row")).toHaveCount(1);
          await expect(page.getByTestId("credit-history-row")).toContainText("Recarga");
          break;
        case "03-no-subscription":
          await expect(page.getByTestId("subscription-none")).toBeVisible();
          await expect(page.getByRole("button", { name: /^Assinar / })).toHaveCount(3);
          await expect(page.getByTestId("credit-history-empty")).toBeVisible();
          // Packages listed, flag false: no action, no block reason.
          await expect(page.getByRole("button", { name: "Recarregar" })).toHaveCount(0);
          await expect(page.getByTestId("topup-blocked")).toHaveCount(0);
          await expect(page.getByTestId("credit-thresholds")).toContainText("100%");
          break;
        case "04-summary-error":
          await expect(page.getByRole("alert").filter({ hasText: "Não foi possível carregar os dados de pagamento" })).toBeVisible();
          await expect(page.getByRole("button", { name: /^Assinar / })).toHaveCount(0);
          // Credits not launched: the whole area is absent, numbering 01, 02.
          await expect(page.getByRole("heading", { level: 2 })).toHaveText(["Assinatura e uso", "Planos"]);
          await expect(page.getByTestId("credits-and-topups")).toHaveCount(0);
          await expect(page.locator("#billing-area-credits")).toHaveCount(0);
          break;
      }

      // No roadmap marker, out-of-scope item or internal id in the DOM.
      const text = (await page.locator("main").textContent()) ?? "";
      expect(text).not.toMatch(/roadmap|em breve|fatura|cupom|anual|parcel|cancelar assinatura|trocar cartão/i);
      expect(await page.content()).not.toContain("00000000-0000-4000-8000-00000000a00");
      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

      // The page this wish owns: the `<main>` region, whole. The sidebar and
      // header belong to the layout, outside this wish, and would make these
      // baselines drift with unrelated navigation changes.
      // Captured unscrolled (full page) and clipped to `<main>`'s document box,
      // so the layout's sticky mobile header never covers the page.
      await page.evaluate(() => window.scrollTo(0, 0));
      const main = (await page.locator("main").boundingBox())!;
      await expect(page).toHaveScreenshot(`${state}-${testInfo.project.name}.png`, {
        fullPage: true,
        clip: { x: main.x, y: main.y, width: main.width, height: main.height },
        maxDiffPixelRatio: 0.001,
      });
      expect(network.unmocked).toEqual([]);
      expect(network.posted).toEqual([]);
    });
  }
});

// ── Layout, keyboard and announcements (no extra PNG) ────────────────────────

test.describe("Pagamento — layout invariants", () => {
  test("no horizontal overflow in any state", async ({ page, baseURL }) => {
    for (const state of Object.keys(STATES) as Array<keyof typeof STATES>) {
      const network = await openBilling(page, state, baseURL!);
      expect(await horizontalOverflow(page), state).toBeLessThanOrEqual(0);
      expect(network.unmocked, state).toEqual([]);
      await page.unrouteAll({ behavior: "ignoreErrors" });
    }
  });

  test("plans: a table without clipping on desktop, cards with a visible action at 1024 and on mobile", async ({ page, baseURL }, testInfo) => {
    const network = await openBilling(page, "03-no-subscription", baseURL!);
    const table = page.getByRole("table", { name: "Comparação de planos" });
    const display = () => table.evaluate((el) => getComputedStyle(el).display);
    const scroll = page.getByTestId("plan-comparison-scroll");

    const assertActionsVisible = async () => {
      const viewport = page.viewportSize()!;
      const wrapper = (await scroll.boundingBox())!;
      for (const button of await page.getByRole("button", { name: /^Assinar / }).all()) {
        await expect(button).toBeVisible();
        const box = (await button.boundingBox())!;
        expect(box.x).toBeGreaterThanOrEqual(wrapper.x - 0.5);
        expect(box.x + box.width).toBeLessThanOrEqual(Math.min(wrapper.x + wrapper.width, viewport.width) + 0.5);
      }
      // No internal clipping: the table never needs its own horizontal scroll.
      expect(await scroll.evaluate((el) => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(0);
      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    };

    if (testInfo.project.name === "desktop") {
      expect(await display()).toBe("table");
      await assertActionsVisible();
      // The table/cards switch follows the container, not the viewport.
      await page.setViewportSize({ width: 1024, height: 900 });
      await expect.poll(display).toBe("block");
      await assertActionsVisible();

      // A pending subscription's row carries the "Aguardando confirmação"
      // badge AND the disabled subscribe button: the widest table. At 1056 px
      // (sidebar open) a shown table must fit without its internal scroll,
      // with every action visible and each header over its cells; otherwise
      // the cards are shown.
      await page.setViewportSize({ width: 1056, height: 900 });
      for (const locale of ["pt", "en"] as const) {
        await page.unrouteAll({ behavior: "ignoreErrors" });
        const pending = await openBilling(page, "02-pending", baseURL!, locale);
        const plans = page.getByRole("table", { name: locale === "en" ? "Plan comparison" : "Comparação de planos" });
        const wrapper = (await scroll.boundingBox())!;
        const action = page.getByRole("button", { name: locale === "en" ? "Subscribe to Pro" : "Assinar Pro" });
        await expect(action).toBeVisible();
        await expect(action).toBeDisabled();
        const box = (await action.boundingBox())!;
        expect(box.x, locale).toBeGreaterThanOrEqual(wrapper.x - 0.5);
        expect(box.x + box.width, locale).toBeLessThanOrEqual(Math.min(wrapper.x + wrapper.width, 1056) + 0.5);
        if ((await plans.evaluate((el) => getComputedStyle(el).display)) === "table") {
          expect(await scroll.evaluate((el) => el.scrollWidth - el.clientWidth), locale).toBeLessThanOrEqual(1);
          const misalignment = await plans.evaluate((el) => {
            const heads = [...el.querySelectorAll("thead th")];
            const cells = [...el.querySelector("tbody tr")!.children];
            return Math.max(
              ...heads.map((head, index) => {
                const a = head.getBoundingClientRect();
                const b = cells[index].getBoundingClientRect();
                return Math.max(Math.abs(a.left - b.left), Math.abs(a.right - b.right));
              }),
            );
          });
          expect(misalignment, locale).toBeLessThanOrEqual(1);
        }
        expect(await horizontalOverflow(page), locale).toBeLessThanOrEqual(0);
        expect(pending.unmocked, locale).toEqual([]);
      }
    } else {
      expect(await display()).toBe("block");
      await assertActionsVisible();
      // Movement cards come from the same table, re-laid out.
      await page.unrouteAll({ behavior: "ignoreErrors" });
      await openBilling(page, "01-active", baseURL!);
      const history = page.getByRole("table", { name: "Histórico de créditos" });
      expect(await history.evaluate((el) => getComputedStyle(el).display)).toBe("block");
      await expect(history.getByTestId("credit-history-row")).toHaveCount(3);
      const box = (await history.boundingBox())!;
      expect(box.x + box.width).toBeLessThanOrEqual(page.viewportSize()!.width);
      const recharge = (await page.getByRole("button", { name: "Recarregar" }).boundingBox())!;
      expect(recharge.x + recharge.width).toBeLessThanOrEqual(page.viewportSize()!.width);
    }
    expect(network.unmocked).toEqual([]);
  });

  test("keyboard reaches the actions in visual order; Recarregar moves focus into its panel", async ({ page, baseURL }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "keyboard order is checked once, on the desktop layout");
    const network = await openBilling(page, "01-active", baseURL!);
    await page.getByRole("heading", { level: 1, name: "Pagamento" }).focus();
    // Tab from the title until Recarregar: every action met on the way, in
    // order, and never above the previous one on the page (visual order, not
    // only DOM order).
    const order: string[] = [];
    const tops: number[] = [];
    for (let i = 0; i < 30 && order[order.length - 1] !== "Recarregar"; i += 1) {
      await page.keyboard.press("Tab");
      const focused = await page.evaluate(() => {
        const el = document.activeElement as HTMLElement | null;
        return { text: el?.innerText?.trim() ?? "", top: (el?.getBoundingClientRect().top ?? 0) + window.scrollY };
      });
      order.push(focused.text);
      tops.push(focused.top);
    }
    for (let i = 1; i < tops.length; i += 1) {
      expect(tops[i], `${order[i]} is above ${order[i - 1]}`).toBeGreaterThanOrEqual(tops[i - 1] - 1);
    }
    // The visual order of area 03: export (balance card), then Recarregar.
    const exportIndex = order.indexOf("Exportar dados (ZIP)");
    expect(exportIndex).toBeGreaterThanOrEqual(0);
    expect(order[order.length - 1]).toBe("Recarregar");
    expect(order.length - 1).toBeGreaterThan(exportIndex);
    await expect(page.getByRole("button", { name: "Recarregar" })).toBeFocused();

    await page.keyboard.press("Enter");
    await expect(page.getByRole("heading", { level: 3, name: "Recarregar créditos" })).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(page.getByRole("button", { name: "Fechar recarga" })).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(page.getByRole("button", { name: /^500 créditos/ })).toBeFocused();
    // Escape closes the panel and returns focus to Recarregar.
    await page.keyboard.press("Escape");
    await expect(page.getByRole("heading", { level: 3, name: "Recarregar créditos" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Recarregar" })).toBeFocused();
    // Choosing never purchases: no POST leaves the page without the explicit action.
    expect(network.posted).toEqual([]);
    expect(network.unmocked).toEqual([]);
  });

  test("a top-up POST that does not navigate moves focus to the checkout status, never to <body>", async ({ page, baseURL }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "focus handling does not depend on the viewport");
    const network = await openBilling(page, "01-active", baseURL!);
    await page.getByRole("button", { name: "Recarregar" }).click();
    await page.getByRole("button", { name: /^500 créditos/ }).click();
    await page.getByRole("button", { name: "PIX" }).click();
    await page.getByRole("button", { name: "Ir para o pagamento da recarga" }).click();
    const status = page.getByTestId("checkout-status");
    await expect(status).toBeFocused();
    await expect(status).toHaveAccessibleName("Criando o checkout no provedor");
    expect(await page.evaluate(() => document.activeElement === document.body)).toBe(false);
    expect(network.posted).toEqual(["POST /v1/billing/checkout"]);
    expect(network.unmocked).toEqual([]);
  });

  test("status and errors are announced through live regions", async ({ page, baseURL }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "semantics do not depend on the viewport");
    let network = await openBilling(page, "02-pending", baseURL!);
    const status = page.locator('[data-live-region="billing-status"]');
    await expect(status).toHaveAttribute("aria-live", "polite");
    await expect(status).toHaveAttribute("role", "status");
    await expect(status).toContainText("Pagamento pendente");
    // The ledger notice lives in a polite region mounted with the panel.
    const notices = page.getByTestId("credit-notices");
    await expect(notices).toHaveAttribute("role", "status");
    await expect(notices).toHaveAttribute("aria-live", "polite");
    await expect(notices.getByTestId("credit-state")).toBeVisible();
    expect(network.unmocked).toEqual([]);

    await page.unrouteAll({ behavior: "ignoreErrors" });
    network = await openBilling(page, "04-summary-error", baseURL!);
    // The switched-off credits area is absent, not an empty region.
    await expect(page.getByRole("region", { name: "Créditos e recargas" })).toHaveCount(0);
    const alert = page.getByTestId("billing-context-alert");
    await expect(alert).toHaveAttribute("role", "alert");
    await alert.getByRole("button", { name: "Tentar novamente" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Pagamento" })).toBeFocused();
    expect(network.unmocked).toEqual([]);
  });

  test("the English page keeps the same areas and actions", async ({ page, baseURL }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "one EN pass is enough");
    const network = await openBilling(page, "01-active", baseURL!, "en");
    await expect(page.getByRole("heading", { level: 2 })).toHaveText(["Subscription and usage", "Plans", "Credits and top-ups"]);
    await expect(page.getByRole("button", { name: "Top up" })).toBeVisible();
    await expect(page.getByRole("table", { name: "Credit history" })).toBeVisible();
    expect(((await page.locator("main").textContent()) ?? "")).not.toMatch(/roadmap|coming soon|invoice|coupon|annual|installment/i);
    expect(network.unmocked).toEqual([]);

    // Credits not launched (04): the English page also drops the whole area.
    await page.unrouteAll({ behavior: "ignoreErrors" });
    const off = await openBilling(page, "04-summary-error", baseURL!, "en");
    await expect(page.getByRole("heading", { level: 2 })).toHaveText(["Subscription and usage", "Plans"]);
    await expect(page.getByText(/Credits and top-ups|Credits are not available/)).toHaveCount(0);
    expect(off.unmocked).toEqual([]);
  });
});
