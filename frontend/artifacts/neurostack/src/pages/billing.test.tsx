import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { I18nProvider } from "@/context/i18n";
import Billing from "./billing";
import { navigateToHostedCheckout } from "./billing-navigation";

vi.mock("./billing-navigation", () => ({ navigateToHostedCheckout: vi.fn() }));

const SESSION_KEY = "ns_billing_checkout_session";
const SESSION_ID = "0b6f1f0e-3c1a-4e7b-9d55-2f1f6f3f9a10";
const HOSTED_URL = "https://asaas.com/checkoutSession/show?id=chk_abc123";
// The page reads the credit ledger once per visit; the lots and the packages
// only after the ledger answered 200. By default the credit switch is off
// (`503 credit_unavailable`), so the counts below include that one read.
const CREDIT_SUMMARY = "/v1/credits/summary";
const CREDIT_LOTS = "/v1/credits/lots";
const CREDIT_CATALOG = "/v1/credits/catalog";

// Synthetic fixtures shaped like `BillingSummaryResponse` and
// `BillingCatalogResponse` (`src/api/schemas/billing.py`). Every amount, credit
// and clinic count below is invented for the tests.
const CATALOG = "/v1/billing/catalog";
const CATALOG_VERSION = "catalog-test-v1";
const PLUS = "plan_plus_monthly_v1";
const PRO = "plan_pro_monthly_v1";
const MAX = "plan_max_monthly_v1";

function planOffer(offer_id: string, amount_cents: number, credits: number, included_clinics: number, additional_clinic_amount_cents: number) {
  return { offer_id, kind: "subscription", currency: "BRL", amount_cents, credits, included_clinics, additional_clinic_amount_cents };
}

function topupOffer(offer_id: string, amount_cents: number, credits: number) {
  return { offer_id, kind: "topup", currency: "BRL", amount_cents, credits, included_clinics: 0, additional_clinic_amount_cents: 0 };
}

const catalog = {
  catalog_version_id: CATALOG_VERSION,
  offers: [
    planOffer(PLUS, 111_100, 1_100, 1, 11_000),
    planOffer(PRO, 222_200, 2_200, 2, 22_000),
    planOffer(MAX, 333_300, 3_300, 3, 33_000),
    topupOffer("package_credits_500_v1", 4_400, 500),
  ],
};

function storageMetric(used_mb: number, limit_mb: number) {
  const mb = 1_000_000;
  return {
    used_bytes: used_mb * mb,
    used_mb,
    limit_bytes: limit_mb * mb,
    limit_mb,
    available_bytes: Math.max(limit_mb - used_mb, 0) * mb,
    available_mb: Math.max(limit_mb - used_mb, 0),
    overage_bytes: Math.max(used_mb - limit_mb, 0) * mb,
    overage_mb: Math.max(used_mb - limit_mb, 0),
    percent_used: limit_mb > 0 ? (used_mb / limit_mb) * 100 : 0,
  };
}

function usageFixture({ traces = 40, traceLimit = 400, rag = 12, ragLimit = 120, media = 7, mediaLimit = 70 } = {}) {
  return {
    plan: null,
    as_of: "2026-10-30T15:00:00Z",
    trace_period: { start: "2026-10-01T00:00:00Z", end: "2026-11-01T00:00:00Z", timezone: "UTC" },
    traces: {
      used: traces,
      included: traceLimit,
      extra_credits: 0,
      available: Math.max(traceLimit - traces, 0),
      overage: Math.max(traces - traceLimit, 0),
      percent_used: traceLimit > 0 ? (traces / traceLimit) * 100 : 0,
    },
    storage: { rag: storageMetric(rag, ragLimit), media: storageMetric(media, mediaLimit) },
    credits: { purchase_enabled: false, coming_soon: true },
  };
}

function activeSubscription(overrides: Record<string, unknown> = {}) {
  return {
    offer_id: PRO,
    catalog_version_id: CATALOG_VERSION,
    status: "active",
    amount_cents: 222_200,
    current_period_end: "2026-11-17",
    payment_state: "paid",
    ...overrides,
  };
}

// The legacy pricing fields carry server defaults; the page must ignore them,
// so they are present with eye-catching values that must never render.
const LEGACY_FIELDS = {
  plan: "legacy-plan",
  free_tier: 97,
  extra_clinics: 96,
  base_price: 9_999,
  extra_price: 9_998,
  total_monthly: 9_997,
  total_annual: 99_996,
  discount_pct: 95,
  subscription_status: "trial",
  billing_cycle: "monthly",
};

function summaryFixture(overrides: Record<string, unknown> = {}) {
  return {
    ...LEGACY_FIELDS,
    active_clinics: 2,
    usage: usageFixture(),
    subscription: null,
    checkout: { available_offer_ids: [PLUS, PRO, MAX], live_session: null, available: true },
    topup_commercial_enabled: false,
    ...overrides,
  };
}

// An eligible account without a subscription.
const summary = summaryFixture();
// A server-confirmed Pro subscription: no plan offer is listed for it.
const activeSummary = summaryFixture({
  subscription: activeSubscription(),
  checkout: { available_offer_ids: [], live_session: null, available: true },
});

function brl(cents: number, locale = "pt-BR") {
  // Intl separates the symbol with a no-break space; DOM matchers normalise it.
  return new Intl.NumberFormat(locale, { style: "currency", currency: "BRL" })
    .format(cents / 100)
    .replace(/\u00a0/g, " ");
}

function response(data: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => data,
  } as Response;
}

function session(state: string, checkout_url: string | null = null) {
  return {
    ok: true,
    checkout_session_id: SESSION_ID,
    kind: "subscription",
    state,
    checkout_url,
  };
}

type Route = (url: string, init?: RequestInit) => Response;

function routeFetch(fetchMock: ReturnType<typeof vi.fn>, routes: Record<string, Route>) {
  // The catalog is read once per visit; a test may override it.
  const all: Record<string, Route> = {
    [`GET ${CATALOG}`]: () => response(catalog),
    [`GET ${CREDIT_SUMMARY}`]: () => response({ code: "credit_unavailable" }, 503),
    [`GET ${CREDIT_LOTS}`]: () => response(creditLots([])),
    [`GET ${CREDIT_CATALOG}`]: () => response(creditCatalog),
    ...routes,
  };
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const handler = all[`${method} ${url}`];
    if (!handler) throw new Error(`unexpected request ${method} ${url}`);
    return handler(url, init);
  });
}

function calls(fetchMock: ReturnType<typeof vi.fn>, method: string, url: string) {
  return fetchMock.mock.calls.filter(
    ([callUrl, init]) => callUrl === url && ((init as RequestInit | undefined)?.method ?? "GET") === method,
  );
}

function renderBilling(locale: "pt" | "en" = "pt") {
  localStorage.setItem("ns_locale", locale);
  localStorage.setItem("ns_auth", JSON.stringify({ token: "test-token" }));
  return render(
    <I18nProvider>
      <Billing />
    </I18nProvider>,
  );
}

async function startCheckout(planName: "Plus" | "Pro" | "Max") {
  const subscribe = await screen.findByRole("button", { name: new RegExp(`Assinar ${planName}`) });
  await waitFor(() => expect(subscribe).toBeEnabled());
  fireEvent.click(subscribe);
  fireEvent.click(screen.getByRole("button", { name: /Continuar para o checkout seguro/i }));
}

describe("Billing", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    routeFetch(fetchMock, { "GET /v1/billing/summary": () => response(summary) });
    Element.prototype.scrollIntoView = vi.fn();
    sessionStorage.clear();
  });

  afterEach(() => {
    cleanup();
    fetchMock.mockReset();
    vi.mocked(navigateToHostedCheckout).mockReset();
    vi.unstubAllGlobals();
    window.history.replaceState({}, "", "/");
  });

  it("offers only the three monthly card plans and keeps top-ups unavailable", async () => {
    renderBilling();

    expect(await screen.findByText("NeuroStack Plus")).toBeInTheDocument();
    expect(screen.getByText("NeuroStack Pro")).toBeInTheDocument();
    expect(screen.getByText("NeuroStack Max")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getAllByRole("button", { name: /^Assinar / })).toHaveLength(3),
    );
    fireEvent.click(screen.getByRole("button", { name: /Assinar Plus/ }));
    await screen.findByRole("button", { name: /Continuar para o checkout seguro/i });

    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/anual|annual|\bpix\b|parcel|installment|economize/i);
    // Per-element queries: concatenated body text hides word boundaries.
    const forbidden = /\bpix\b|anual|annual|parcel|installment|economize|boleto/i;
    expect(screen.queryAllByText(forbidden)).toHaveLength(0);
    for (const role of ["button", "radio", "option", "tab", "checkbox", "link"] as const) {
      expect(screen.queryAllByRole(role, { name: forbidden })).toHaveLength(0);
    }
    // The legacy gateway name is assembled so this file never contains it.
    expect(text).not.toMatch(new RegExp(["aba", "cate"].join(""), "i"));
    expect(text).not.toMatch(/Start|Growth|Scale|API SRM/);
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    // Top-ups closed: no dead "unavailable" card or button, no action at all.
    expect(screen.queryByText(/Recargas indisponíveis|Top-ups unavailable/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Recarregar|Comprar/i })).not.toBeInTheDocument();
  });

  it("keeps subscription actions absent for a server-confirmed subscription", async () => {
    routeFetch(fetchMock, { "GET /v1/billing/summary": () => response(activeSummary) });
    renderBilling();

    expect(await screen.findByTestId("subscription-status")).toHaveTextContent("Ativa");
    await screen.findByRole("table", { name: "Comparação de planos" });
    expect(screen.queryAllByRole("button", { name: /^Assinar / })).toHaveLength(0);
    // No plan-switch contract exists: no migrate CTA and no dead manage button.
    expect(screen.queryByRole("button", { name: /Migrar de plano|Gerenciar Assinatura/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/Migrar de plano/i)).not.toBeInTheDocument();
    expect(calls(fetchMock, "POST", "/v1/billing/checkout")).toHaveLength(0);
  });

  it("sends only the backend-owned monthly card contract and follows the returned checkout_url", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      "POST /v1/billing/checkout": () => response(session("active", HOSTED_URL)),
    });
    renderBilling();

    await startCheckout("Pro");

    await waitFor(() => expect(navigateToHostedCheckout).toHaveBeenCalledWith(HOSTED_URL));
    const [checkoutCall] = calls(fetchMock, "POST", "/v1/billing/checkout");
    const body = JSON.parse(String((checkoutCall[1] as RequestInit).body));
    expect(body).toEqual({ offer_id: "plan_pro_monthly_v1", method: "credit_card" });
    expect(sessionStorage.getItem(SESSION_KEY)).toBe(SESSION_ID);
  });

  it("offers no coupon field and never calls the coupon route (owner decision: coupon removed)", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      "POST /v1/billing/checkout": () => response(session("active", HOSTED_URL)),
    });
    renderBilling();

    await startCheckout("Plus");

    await waitFor(() => expect(calls(fetchMock, "POST", "/v1/billing/checkout")).toHaveLength(1));
    const [checkoutCall] = calls(fetchMock, "POST", "/v1/billing/checkout");
    expect(JSON.parse(String((checkoutCall[1] as RequestInit).body))).toEqual({
      offer_id: "plan_plus_monthly_v1",
      method: "credit_card",
    });
    expect(screen.queryByLabelText(/cupom|coupon/i)).not.toBeInTheDocument();
    expect(screen.queryAllByText(/cupom|coupon/i)).toHaveLength(0);
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/coupon"))).toBe(false);
  });

  it("does not treat a 202 reconciliation response as a created checkout", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      "POST /v1/billing/checkout": () =>
        response({ detail: "billing_checkout_reconciliation_required" }, 202),
    });
    renderBilling();

    await startCheckout("Plus");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Não pague novamente/i);
    expect(alert).toHaveTextContent(/clique em assinar de novo/i);
    expect(alert).not.toHaveTextContent(/Não tente novamente/i);
    // Retrying the same plan is how the existing session is recovered.
    expect(screen.getByRole("button", { name: /Continuar para o checkout seguro/i })).toBeEnabled();
    expect(navigateToHostedCheckout).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(SESSION_KEY)).toBeNull();
  });

  it.each([
    [503, "billing_checkout_unavailable", /Novas assinaturas estão indisponíveis/i],
    [409, "billing_checkout_not_eligible", /não pode iniciar uma nova assinatura/i],
    [422, "billing_checkout_input_rejected", /rejeitou os dados do checkout/i],
    [400, "invalid_request", /Não foi possível iniciar o checkout/i],
    [502, "billing_checkout_creation_rejected", /provedor de pagamento recusou/i],
    [401, "Not authenticated", /sessão expirou/i],
    [403, "Builder access required", /não tem permissão para gerenciar o faturamento/i],
  ])("maps a %s %s response without exposing the code", async (status, detail, message) => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      "POST /v1/billing/checkout": () => response({ detail }, status),
    });
    renderBilling();

    await startCheckout("Max");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(message);
    expect(alert).not.toHaveTextContent(detail);
    expect(navigateToHostedCheckout).not.toHaveBeenCalled();
  });

  it.each([
    "javascript:alert(1)",
    "http://asaas.com/checkoutSession/show?id=chk_abc123",
  ])("does not follow a non-HTTPS checkout_url (%s)", async (checkoutUrl) => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      "POST /v1/billing/checkout": () => response(session("active", checkoutUrl)),
    });
    renderBilling();

    await startCheckout("Plus");

    expect(await screen.findByText("Checkout aberto, aguardando pagamento")).toBeInTheDocument();
    expect(navigateToHostedCheckout).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /Abrir checkout hospedado/i })).not.toBeInTheDocument();
  });

  it("re-reads the stored session on return and ignores the query string as a result", async () => {
    sessionStorage.setItem(SESSION_KEY, SESSION_ID);
    window.history.replaceState({}, "", "/billing?checkout=success");
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET /v1/billing/checkout/${SESSION_ID}`]: () => response(session("paid_pending_subscription")),
    });
    renderBilling();

    expect(await screen.findByText(/Você voltou do checkout hospedado/i)).toBeInTheDocument();
    expect(
      await screen.findByText(/aguardando confirmação da assinatura/i),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("subscription-status")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByRole("button", { name: /^Assinar / })).toHaveLength(3));
    for (const button of screen.getAllByRole("button", { name: /^Assinar / })) {
      expect(button).toBeDisabled();
    }
    expect(window.location.search).toBe("");
    expect(calls(fetchMock, "GET", "/v1/billing/summary")).toHaveLength(1);
    expect(calls(fetchMock, "GET", `/v1/billing/checkout/${SESSION_ID}`)).toHaveLength(1);
    expect(calls(fetchMock, "POST", "/v1/billing/checkout")).toHaveLength(0);
    expect(sessionStorage.getItem(SESSION_KEY)).toBe(SESSION_ID);
  });

  it("shows activation only when the backend reports it, whatever the return marker says", async () => {
    sessionStorage.setItem(SESSION_KEY, SESSION_ID);
    window.history.replaceState({}, "", "/billing?checkout=cancel");
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(activeSummary),
      [`GET /v1/billing/checkout/${SESSION_ID}`]: () => response(session("completed")),
    });
    renderBilling();

    expect(await screen.findByText("Checkout concluído")).toBeInTheDocument();
    expect(await screen.findByTestId("subscription-status")).toHaveTextContent("Ativa");
    // Terminal sessions are shown once and then forgotten.
    expect(sessionStorage.getItem(SESSION_KEY)).toBeNull();
  });

  it("only re-reads the summary on a return without a stored session", async () => {
    window.history.replaceState({}, "", "/billing?checkout=success");
    renderBilling();

    expect(await screen.findByText(/Você voltou do checkout hospedado/i)).toBeInTheDocument();
    await waitFor(() => expect(calls(fetchMock, "GET", "/v1/billing/summary")).toHaveLength(1));
    expect(calls(fetchMock, "GET", CREDIT_SUMMARY)).toHaveLength(1);
    expect(calls(fetchMock, "GET", CATALOG)).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(screen.queryByTestId("subscription-status")).not.toBeInTheDocument();
  });

  it("forgets a stored session the server does not recognise", async () => {
    sessionStorage.setItem(SESSION_KEY, SESSION_ID);
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET /v1/billing/checkout/${SESSION_ID}`]: () => response({ detail: "Checkout not found" }, 404),
    });
    renderBilling();

    await waitFor(() => expect(sessionStorage.getItem(SESSION_KEY)).toBeNull());
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Assinar Plus/ })).toBeEnabled(),
    );
  });

  it("ignores a malformed stored session id", async () => {
    sessionStorage.setItem(SESSION_KEY, "../../admin");
    renderBilling();

    await waitFor(() => expect(calls(fetchMock, "GET", "/v1/billing/summary")).toHaveLength(1));
    expect(calls(fetchMock, "GET", CREDIT_SUMMARY)).toHaveLength(1);
    expect(calls(fetchMock, "GET", CATALOG)).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("lets the user ask the server to reconcile a session under verification", async () => {
    sessionStorage.setItem(SESSION_KEY, SESSION_ID);
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET /v1/billing/checkout/${SESSION_ID}`]: () => response(session("reconciliation_required")),
      [`POST /v1/billing/checkout/${SESSION_ID}/reconcile`]: () =>
        response(session("terminal_pending_readback")),
    });
    renderBilling();

    expect(await screen.findByText(/Não inicie outro checkout/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Verificar com o provedor/i }));

    expect(await screen.findByText(/Aguardando a leitura final do provedor/i)).toBeInTheDocument();
    expect(calls(fetchMock, "POST", `/v1/billing/checkout/${SESSION_ID}/reconcile`)).toHaveLength(1);
    expect(calls(fetchMock, "POST", "/v1/billing/checkout")).toHaveLength(0);
  });

  it("offers no subscription action when the summary cannot be read", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response({ detail: "unavailable" }, 500),
    });
    renderBilling();

    expect(await screen.findByTestId("billing-context-alert")).toBeInTheDocument();
    // The catalog still compares plans, but without the server's capability
    // there is nothing to buy.
    await screen.findByRole("table", { name: "Comparação de planos" });
    expect(screen.queryAllByRole("button", { name: /^Assinar / })).toHaveLength(0);
  });

  it("sends one checkout POST for a burst of clicks on the pay button", async () => {
    let release: (value: Response) => void = () => {};
    const pending = new Promise<Response>((resolve) => {
      release = resolve;
    });
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (method === "GET" && url === "/v1/billing/summary") return response(summary);
      if (method === "GET" && url === CATALOG) return response(catalog);
      if (method === "POST" && url === "/v1/billing/checkout") return pending;
      throw new Error(`unexpected request ${method} ${url}`);
    });
    renderBilling();

    const subscribe = await screen.findByRole("button", { name: /Assinar Plus/ });
    await waitFor(() => expect(subscribe).toBeEnabled());
    fireEvent.click(subscribe);
    const pay = screen.getByRole("button", { name: /Continuar para o checkout seguro/i });
    fireEvent.click(pay);
    fireEvent.click(pay);
    fireEvent.click(pay);

    expect(calls(fetchMock, "POST", "/v1/billing/checkout")).toHaveLength(1);
    release(response(session("active", HOSTED_URL)));
    await waitFor(() => expect(navigateToHostedCheckout).toHaveBeenCalledTimes(1));
    expect(calls(fetchMock, "POST", "/v1/billing/checkout")).toHaveLength(1);
  });

  it("stays pending and blocks subscribing when the stored session cannot be re-read", async () => {
    sessionStorage.setItem(SESSION_KEY, SESSION_ID);
    window.history.replaceState({}, "", "/billing?checkout=success");
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET /v1/billing/checkout/${SESSION_ID}`]: () => response({ detail: "boom" }, 500),
    });
    renderBilling();

    expect(
      await screen.findByText(/Não foi possível ler o estado do checkout no servidor/i),
    ).toBeVisible();
    await waitFor(() => expect(calls(fetchMock, "GET", "/v1/billing/summary")).toHaveLength(1));
    expect(await screen.findByText("NeuroStack Plus")).toBeInTheDocument();
    const buttons = await screen.findAllByRole("button", { name: /^Assinar / });
    expect(buttons).toHaveLength(3);
    for (const button of buttons) expect(button).toBeDisabled();
    expect(screen.queryByText("Assinatura Ativa")).not.toBeInTheDocument();
    // The id is kept so the page can still re-read it.
    expect(sessionStorage.getItem(SESSION_KEY)).toBe(SESSION_ID);
    expect(calls(fetchMock, "POST", "/v1/billing/checkout")).toHaveLength(0);
  });

  it("lets the user retry an unreadable stored session and unblocks only after a terminal read", async () => {
    sessionStorage.setItem(SESSION_KEY, SESSION_ID);
    let fail = true;
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET /v1/billing/checkout/${SESSION_ID}`]: () =>
        fail ? response({ detail: "boom" }, 500) : response(session("expired")),
    });
    renderBilling();

    const retry = await screen.findByRole("button", { name: /Tentar ler novamente/i });
    expect(screen.getByRole("button", { name: /Assinar Plus/ })).toBeDisabled();
    fail = false;
    fireEvent.click(retry);

    expect(await screen.findByText("Checkout expirado")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Assinar Plus/ })).toBeEnabled(),
    );
    expect(sessionStorage.getItem(SESSION_KEY)).toBeNull();
  });

  it("keeps the session under verification when the reconcile route returns 202", async () => {
    sessionStorage.setItem(SESSION_KEY, SESSION_ID);
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET /v1/billing/checkout/${SESSION_ID}`]: () => response(session("reconciliation_required")),
      [`POST /v1/billing/checkout/${SESSION_ID}/reconcile`]: () =>
        response({ detail: "billing_checkout_reconciliation_required" }, 202),
    });
    renderBilling();

    fireEvent.click(await screen.findByRole("button", { name: /Verificar com o provedor/i }));

    expect(
      await screen.findByText(/O provedor não pôde ser consultado agora/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Não foi possível ler o estado/i)).not.toBeInTheDocument();
    expect(screen.getByText("Em verificação pelo servidor")).toBeInTheDocument();
    for (const button of screen.getAllByRole("button", { name: /^Assinar / })) {
      expect(button).toBeDisabled();
    }
  });

  it("does not tell a non-builder that the session expired", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      "POST /v1/billing/checkout": () => response({ detail: "forbidden" }, 403),
    });
    renderBilling();

    await startCheckout("Plus");

    expect(await screen.findByRole("alert")).not.toHaveTextContent(/sessão expirou/i);
  });

  // ── `?checkout=cancel` is navigation only ───────────────────────────────
  // The cancel marker never unblocks subscribing by itself: the buttons stay
  // blocked exactly while the server reports a live session (or the stored
  // session is still unread), and unblock once the server says it is terminal.

  it.each([
    ["active", HOSTED_URL, "Checkout aberto, aguardando pagamento"],
    ["creating", null, "Criando o checkout no provedor"],
    ["paid_pending_subscription", null, "Pagamento recebido pelo provedor; aguardando confirmação da assinatura"],
    ["reconciliation_required", null, "Em verificação pelo servidor"],
    ["terminal_pending_readback", null, "Aguardando a leitura final do provedor"],
  ])(
    "keeps subscribing blocked after a cancel return while the stored session is %s",
    async (state, checkoutUrl, label) => {
      sessionStorage.setItem(SESSION_KEY, SESSION_ID);
      window.history.replaceState({}, "", "/billing?checkout=cancel");
      routeFetch(fetchMock, {
        "GET /v1/billing/summary": () => response(summary),
        [`GET /v1/billing/checkout/${SESSION_ID}`]: () => response(session(state, checkoutUrl)),
      });
      renderBilling();

      expect(await screen.findByText(label)).toBeInTheDocument();
      await waitFor(() => expect(calls(fetchMock, "GET", "/v1/billing/summary")).toHaveLength(1));
      for (const button of screen.getAllByRole("button", { name: /^Assinar / })) {
        expect(button).toBeDisabled();
      }
      // A return never redirects on its own; an open session is resumed only
      // by an explicit click.
      expect(navigateToHostedCheckout).not.toHaveBeenCalled();
      expect(calls(fetchMock, "POST", "/v1/billing/checkout")).toHaveLength(0);
      expect(sessionStorage.getItem(SESSION_KEY)).toBe(SESSION_ID);
      expect(window.location.search).toBe("");
    },
  );

  it("keeps subscribing blocked after a cancel return while the stored session is unread", async () => {
    sessionStorage.setItem(SESSION_KEY, SESSION_ID);
    window.history.replaceState({}, "", "/billing?checkout=cancel");
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET /v1/billing/checkout/${SESSION_ID}`]: () => response({ detail: "boom" }, 500),
    });
    renderBilling();

    expect(await screen.findByRole("button", { name: /Tentar ler novamente/i })).toBeInTheDocument();
    await waitFor(() => expect(calls(fetchMock, "GET", "/v1/billing/summary")).toHaveLength(1));
    for (const button of screen.getAllByRole("button", { name: /^Assinar / })) {
      expect(button).toBeDisabled();
    }
    expect(sessionStorage.getItem(SESSION_KEY)).toBe(SESSION_ID);
  });

  it.each([
    ["cancelled", "Checkout cancelado"],
    ["expired", "Checkout expirado"],
    ["abandoned", "Checkout encerrado sem conclusão"],
    ["failed", "Checkout não criado"],
  ])(
    "unblocks subscribing after a cancel return once the server reports the session %s",
    async (state, label) => {
      sessionStorage.setItem(SESSION_KEY, SESSION_ID);
      window.history.replaceState({}, "", "/billing?checkout=cancel");
      routeFetch(fetchMock, {
        "GET /v1/billing/summary": () => response(summary),
        [`GET /v1/billing/checkout/${SESSION_ID}`]: () => response(session(state, HOSTED_URL)),
      });
      renderBilling();

      expect(await screen.findByText(label)).toBeInTheDocument();
      await waitFor(() => {
        for (const button of screen.getAllByRole("button", { name: /^Assinar / })) {
          expect(button).toBeEnabled();
        }
      });
      expect(sessionStorage.getItem(SESSION_KEY)).toBeNull();
      expect(navigateToHostedCheckout).not.toHaveBeenCalled();
      expect(calls(fetchMock, "POST", "/v1/billing/checkout")).toHaveLength(0);
    },
  );

  it("unblocks subscribing after a cancel return without a stored session", async () => {
    window.history.replaceState({}, "", "/billing?checkout=cancel");
    renderBilling();

    await waitFor(() => {
      const buttons = screen.getAllByRole("button", { name: /^Assinar / });
      expect(buttons).toHaveLength(3);
      for (const button of buttons) expect(button).toBeEnabled();
    });
    expect(screen.queryByText("Assinatura Ativa")).not.toBeInTheDocument();
    expect(calls(fetchMock, "POST", "/v1/billing/checkout")).toHaveLength(0);
  });

  // ── checkout_url is followed only for an `active` session ───────────────

  const NON_ACTIVE_STATES = [
    "creating",
    "paid_pending_subscription",
    "reconciliation_required",
    "terminal_pending_readback",
    "completed",
    "abandoned",
    "cancelled",
    "expired",
    "failed",
    "some_future_state",
  ];

  it.each(NON_ACTIVE_STATES)(
    "never redirects to a checkout_url returned with a created %s session",
    async (state) => {
      routeFetch(fetchMock, {
        "GET /v1/billing/summary": () => response(summary),
        "POST /v1/billing/checkout": () => response(session(state, HOSTED_URL)),
      });
      renderBilling();

      await startCheckout("Plus");

      expect(await screen.findByRole("status")).toBeInTheDocument();
      expect(navigateToHostedCheckout).not.toHaveBeenCalled();
      expect(screen.queryByRole("button", { name: /Abrir checkout hospedado/i })).not.toBeInTheDocument();
    },
  );

  it.each(NON_ACTIVE_STATES)(
    "never offers a checkout_url read back with a %s session",
    async (state) => {
      sessionStorage.setItem(SESSION_KEY, SESSION_ID);
      routeFetch(fetchMock, {
        "GET /v1/billing/summary": () => response(summary),
        [`GET /v1/billing/checkout/${SESSION_ID}`]: () => response(session(state, HOSTED_URL)),
      });
      renderBilling();

      expect(await screen.findByRole("status")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /Abrir checkout hospedado/i })).not.toBeInTheDocument();
      expect(navigateToHostedCheckout).not.toHaveBeenCalled();
    },
  );

  it("offers an active session's checkout_url read back only behind an explicit click", async () => {
    sessionStorage.setItem(SESSION_KEY, SESSION_ID);
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET /v1/billing/checkout/${SESSION_ID}`]: () => response(session("active", HOSTED_URL)),
    });
    renderBilling();

    const open = await screen.findByRole("button", { name: /Abrir checkout hospedado/i });
    expect(navigateToHostedCheckout).not.toHaveBeenCalled();
    fireEvent.click(open);
    expect(navigateToHostedCheckout).toHaveBeenCalledWith(HOSTED_URL);
  });

  describe("without polling", () => {
    beforeEach(() => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    it("does not poll the stored session or the summary after a return", async () => {
      sessionStorage.setItem(SESSION_KEY, SESSION_ID);
      window.history.replaceState({}, "", "/billing?checkout=success");
      routeFetch(fetchMock, {
        "GET /v1/billing/summary": () => response(summary),
        [`GET /v1/billing/checkout/${SESSION_ID}`]: () => response(session("paid_pending_subscription")),
      });
      renderBilling();

      expect(await screen.findByText(/aguardando confirmação da assinatura/i)).toBeInTheDocument();
      await vi.advanceTimersByTimeAsync(10 * 60 * 1000);

      expect(calls(fetchMock, "GET", "/v1/billing/summary")).toHaveLength(1);
      expect(calls(fetchMock, "GET", `/v1/billing/checkout/${SESSION_ID}`)).toHaveLength(1);
      expect(calls(fetchMock, "GET", CREDIT_SUMMARY)).toHaveLength(1);
      expect(calls(fetchMock, "GET", CATALOG)).toHaveLength(1);
      expect(fetchMock).toHaveBeenCalledTimes(4);
    });

    it("does not poll the summary after a return without a stored session", async () => {
      window.history.replaceState({}, "", "/billing?checkout=success");
      renderBilling();

      expect(await screen.findByText(/Você voltou do checkout hospedado/i)).toBeInTheDocument();
      await waitFor(() => expect(calls(fetchMock, "GET", "/v1/billing/summary")).toHaveLength(1));
      await vi.advanceTimersByTimeAsync(10 * 60 * 1000);

      expect(calls(fetchMock, "GET", CREDIT_SUMMARY)).toHaveLength(1);
      expect(calls(fetchMock, "GET", CATALOG)).toHaveLength(1);
      expect(fetchMock).toHaveBeenCalledTimes(3);
    });
  });
});

// ── Credits (neurostack-credit-system Group 6) ──────────────────────────────
// The page shows only what `GET /v1/credits/summary` read from the ledger;
// with `503 credit_unavailable` every credit panel is hidden and the page is
// the `main` page. Top-ups are offered only when the ledger answered AND the
// server read back `topup_commercial_enabled`; they send `offer_id` and
// `method` only. The export downloads `GET /v1/credits/export`.

const CREDIT_UNAVAILABLE = () => response({ code: "credit_unavailable" }, 503);
const AS_OF = "2026-10-30T15:00:00Z";

function creditSummary(overrides: Record<string, unknown> = {}) {
  const state = {
    as_of: AS_OF,
    mode: "enforced",
    enforcement_at: "2026-10-01T00:00:00Z",
    state: "active",
    pending_reasons: [],
    exhausted_at: null,
    suspension_due_at: null,
    deletion_due_at: null,
    export_deadline_at: null,
    ...((overrides.state as Record<string, unknown>) ?? {}),
  };
  return {
    as_of: AS_OF,
    state,
    balance: {
      as_of: AS_OF,
      monthly_available_mc: 10_000,
      monthly_not_started_mc: 25_000,
      topup_available_mc: 496_000,
      available_mc: 506_000,
      open_debt_mc: 0,
      net_available_mc: 506_000,
      reserved_mc: 2_000,
      next_expiry: null,
      ...((overrides.balance as Record<string, unknown>) ?? {}),
    },
    expiries: (overrides.expiries as unknown[] | undefined) ?? [
      { lot_id: "lot-1", kind: "monthly", expires_at: "2026-11-25T03:00:00Z", available_mc: 10_000 },
      { lot_id: "lot-2", kind: "topup", expires_at: "2027-04-20T03:00:00Z", available_mc: 496_000 },
    ],
    debt: {
      as_of: AS_OF,
      open_debt_mc: 0,
      debts: [],
      ...((overrides.debt as Record<string, unknown>) ?? {}),
    },
    reservations: {
      as_of: AS_OF,
      window: { start: "2026-10-25", end: "2026-11-25", kind: "cycle" },
      reservations: [
        { category: "rag", units: 15_000, coverage_mc: 3_000, charged_mc: 2_000 },
        { category: "media", units: 40_000_000, coverage_mc: 2_000, charged_mc: 0 },
        { category: "egress", units: 0, coverage_mc: 0, charged_mc: 0 },
      ],
      egress_allowance_bytes: 400_000_000,
      egress_delivered_bytes: 0,
    },
    thresholds: {
      as_of: AS_OF,
      capacity_mc: 510_000,
      available_mc: 506_000,
      used_mc: 4_000,
      used_percent: 0,
      reached: [],
      alerts: [],
      ...((overrides.thresholds as Record<string, unknown>) ?? {}),
    },
    consumption: {
      as_of: AS_OF,
      mode: state.mode,
      window: { start: "2026-10-25", end: "2026-11-25", kind: "cycle" },
      charged_mc: 14_000,
      charged: [
        { kind: "rag", amount_mc: 2_000 },
        { kind: "trace", amount_mc: 12_000 },
      ],
      observed_mc: 1_500,
      observed: [{ kind: "trace", amount_mc: 1_500 }],
    },
  };
}

function lot(overrides: Record<string, unknown> = {}) {
  return {
    lot_id: "0f0e0d0c-0000-4000-8000-00000000a001",
    kind: "topup",
    status: "active",
    granted_mc: 500_000,
    available_mc: 496_000,
    consumed_mc: 4_000,
    reversed_mc: 0,
    expired_mc: 0,
    paid_at: "2026-10-20",
    expires_at: "2027-04-20T03:00:00Z",
    cycle_start: null,
    cycle_end: null,
    ...overrides,
  };
}

function creditLots(lots: unknown[]) {
  return { as_of: AS_OF, lots };
}

function creditPackage(offer_id: string, credits: number, amount_cents: number) {
  return { offer_id, credits, amount_cents, currency: "BRL", payment_methods: ["credit_card", "pix"], validity_months: 6 };
}

// Invented prices: the screen must show these, never a local table.
const creditCatalog = {
  catalog_version_id: CATALOG_VERSION,
  millicredits_per_credit: 1000,
  units: {
    trace_mc: 1000,
    rag_characters_per_credit: 5000,
    media_bytes_per_credit: 20_000_000,
    egress_included_bytes_per_credit: 200_000_000,
    egress_overage_bytes_per_credit: 100_000_000,
  },
  packages: [
    creditPackage("package_credits_500_v1", 500, 4_400),
    creditPackage("package_credits_1000_v1", 1000, 8_800),
    creditPackage("package_credits_2000_v1", 2000, 17_600),
    creditPackage("package_credits_4000_v1", 4000, 35_200),
  ],
  subscription_payment_methods: ["credit_card"],
  subscription_cycle: "monthly",
  checkout_endpoint: "/v1/billing/checkout",
};

const TOPUP_IDS = creditCatalog.packages.map((pkg) => pkg.offer_id);
// Top-ups open: the server lists the packages as available.
const topupSummary = summaryFixture({
  topup_commercial_enabled: true,
  checkout: { available_offer_ids: [PLUS, PRO, MAX, ...TOPUP_IDS], live_session: null, available: true },
});

function metric(name: string) {
  return screen.getByRole("group", { name });
}

describe("Billing credits", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    Element.prototype.scrollIntoView = vi.fn();
    sessionStorage.clear();
  });

  afterEach(() => {
    cleanup();
    fetchMock.mockReset();
    vi.mocked(navigateToHostedCheckout).mockReset();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    window.history.replaceState({}, "", "/");
  });

  it.each(["pt", "en"] as const)(
    "omits the whole credits area when the credit switch is off (503 credit_unavailable, %s)",
    async (locale) => {
      routeFetch(fetchMock, {
        "GET /v1/billing/summary": () => response(topupSummary),
        [`GET ${CREDIT_SUMMARY}`]: CREDIT_UNAVAILABLE,
      });
      renderBilling(locale);

      expect(await screen.findByText("NeuroStack Plus")).toBeInTheDocument();
      await waitFor(() => expect(screen.queryByTestId("credits-and-topups")).not.toBeInTheDocument());
      const areas = screen.getAllByTestId("billing-area");
      expect(areas.map((area) => within(area).getByRole("heading", { level: 2 }).textContent)).toEqual(
        locale === "en" ? ["Subscription and usage", "Plans"] : ["Assinatura e uso", "Planos"],
      );
      // Numbering stays contiguous: 01, 02 and no orphan 03, heading or line.
      expect(areas.map((area) => area.querySelector("[aria-hidden='true'] span")?.textContent)).toEqual(["01", "02"]);
      expect(screen.queryByRole("heading", { name: /Créditos e recargas|Credits and top-ups/ })).not.toBeInTheDocument();
      expect(document.getElementById("billing-area-credits")).toBeNull();
      expect(document.body.textContent ?? "").not.toMatch(/Os créditos ainda não estão disponíveis|Credits are not available/);
      expect(screen.queryByRole("button", { name: /Recarregar|Top up/ })).not.toBeInTheDocument();
      expect(calls(fetchMock, "GET", CREDIT_LOTS)).toHaveLength(0);
      expect(calls(fetchMock, "GET", CREDIT_CATALOG)).toHaveLength(0);
    },
  );

  it.each([
    [401, /Sua conta não tem acesso aos créditos/],
    [403, /Sua conta não tem acesso aos créditos/],
  ])("keeps the credits area with its refusal line on a %s credit read", async (status, line) => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(topupSummary),
      [`GET ${CREDIT_SUMMARY}`]: () => response({ detail: "refused" }, status),
    });
    renderBilling();

    const credits = await screen.findByTestId("credits-and-topups");
    await waitFor(() => expect(credits).toHaveAttribute("data-state", "forbidden"));
    expect(credits).toHaveTextContent(line);
    expect(screen.getAllByTestId("billing-area")).toHaveLength(3);
    expect(screen.queryByRole("button", { name: /Recarregar/ })).not.toBeInTheDocument();
  });

  it("sells nothing and asks nothing more while the credit switch is off, even with the commercial flag on", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(topupSummary),
      [`GET ${CREDIT_SUMMARY}`]: CREDIT_UNAVAILABLE,
    });
    renderBilling();

    expect(await screen.findByText("NeuroStack Plus")).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByTestId("billing-area")).toHaveLength(2));
    expect(screen.queryByTestId("credit-panel")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Exportar dados/i })).not.toBeInTheDocument();
    // Top-ups stay absent even with the commercial flag on: no ledger, no sale.
    expect(screen.queryByTestId("topup-offer")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Recarregar|Recargas indisponíveis/i })).not.toBeInTheDocument();
    expect(screen.queryAllByText(/\bpix\b/i)).toHaveLength(0);
    expect(document.body.textContent ?? "").not.toMatch(/créditos?\s+\d/i);
    // Nothing more is asked of a switch that is off.
    expect(calls(fetchMock, "GET", CREDIT_LOTS)).toHaveLength(0);
    expect(calls(fetchMock, "GET", CREDIT_CATALOG)).toHaveLength(0);
  });

  it.each([
    ["a 500", () => response({ error: "internal_server_error" }, 500)],
    ["a 503 without the credit_unavailable code", () => response({ detail: "upstream" }, 503)],
    ["a malformed 200", () => response({ balance: "not a summary" })],
    ["a network failure", () => {
      throw new TypeError("network");
    }],
  ])("announces %s credit read as an error with a retry, never as zero or absent", async (_label, failure) => {
    let fail = true;
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET ${CREDIT_SUMMARY}`]: () => (fail ? failure() : response(creditSummary())),
    });
    renderBilling();

    const credits = await screen.findByTestId("credits-and-topups");
    await waitFor(() => expect(credits).toHaveAttribute("data-state", "error"));
    expect(credits).toHaveAttribute("role", "alert");
    expect(credits).toHaveTextContent("Não foi possível ler os créditos. Nenhum saldo foi presumido.");
    // Not the switched-off answer: the area stays, with its heading.
    expect(screen.getAllByTestId("billing-area")).toHaveLength(3);
    const creditsArea = screen.getAllByTestId("billing-area")[2];
    expect(creditsArea.textContent ?? "").not.toMatch(/\d\s*créditos/);

    fail = false;
    const heading = screen.getByRole("heading", { level: 2, name: "Créditos e recargas" });
    fireEvent.click(within(credits).getByRole("button", { name: "Tentar novamente" }));
    // The retry button goes away; focus lands on the area heading.
    expect(document.activeElement).toBe(heading);
    expect(await screen.findByTestId("credit-panel")).toBeInTheDocument();
    expect(document.activeElement).toBe(heading);
    expect(calls(fetchMock, "GET", CREDIT_SUMMARY)).toHaveLength(2);
  });

  it("shows zero balances as data and an empty history as an explicit absence of grants", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET ${CREDIT_SUMMARY}`]: () =>
        response(
          // Coherent with the server: nothing available is 100 % used,
          // nothing expires and nothing was consumed or reserved.
          creditSummary({
            balance: {
              monthly_available_mc: 0,
              monthly_not_started_mc: 0,
              topup_available_mc: 0,
              available_mc: 0,
              net_available_mc: 0,
              reserved_mc: 0,
            },
            expiries: [],
            thresholds: { capacity_mc: 0, available_mc: 0, used_mc: 0, used_percent: 100 },
          }),
        ),
      [`GET ${CREDIT_LOTS}`]: () => response(creditLots([])),
    });
    renderBilling();

    expect(await screen.findByTestId("credit-panel")).toBeInTheDocument();
    expect(metric("Disponível líquido")).toHaveTextContent(/^Disponível líquido0 créditos$/);
    expect(metric("Recargas")).toHaveTextContent("0 créditos");
    const history = await screen.findByTestId("credit-history");
    await waitFor(() => expect(history).toHaveAttribute("data-state", "ready"));
    expect(within(history).getByTestId("credit-history-empty")).toHaveTextContent(
      "Nenhum crédito foi concedido a esta conta até agora.",
    );
    expect(within(history).queryByRole("table")).not.toBeInTheDocument();
  });

  it("lists the history from the lots with date, origin, credits and a textual status, newest first", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
      [`GET ${CREDIT_LOTS}`]: () =>
        response(
          creditLots([
            lot({
              lot_id: "0f0e0d0c-0000-4000-8000-00000000a002",
              kind: "monthly",
              status: "exhausted",
              granted_mc: 2_200_000,
              available_mc: 0,
              paid_at: "2026-09-25",
              expires_at: "2026-10-25T03:00:00Z",
            }),
            lot(),
            lot({
              lot_id: "0f0e0d0c-0000-4000-8000-00000000a003",
              kind: "monthly",
              status: "not_started",
              granted_mc: 2_200_000,
              paid_at: "2026-10-28",
              expires_at: "2026-12-25T03:00:00Z",
            }),
            lot({
              lot_id: "0f0e0d0c-0000-4000-8000-00000000a004",
              status: "expired",
              granted_mc: 1_000_000,
              available_mc: 0,
              paid_at: "2026-03-02",
              expires_at: "2026-09-02T03:00:00Z",
            }),
          ]),
        ),
    });
    renderBilling();

    const table = await screen.findByRole("table", { name: "Histórico de créditos" });
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual([
      "Data",
      "Origem",
      "Créditos",
      "Situação",
      "Expira em",
    ]);
    const rows = within(table).getAllByTestId("credit-history-row");
    expect(rows.map((row) => within(row).getAllByRole("cell")[0].textContent)).toEqual([
      "Data28/10/2026",
      "Data20/10/2026",
      "Data25/09/2026",
      "Data02/03/2026",
    ]);
    expect(rows[0]).toHaveTextContent(/Franquia mensal.*2\.200 créditos.*Ainda não iniciado.*25\/12\/2026/);
    expect(rows[1]).toHaveTextContent(/Recarga.*500 créditos.*Em vigor.*20\/04\/2027/);
    expect(rows[2]).toHaveTextContent(/Esgotado/);
    expect(rows[3]).toHaveTextContent(/Vencido/);
    // Internal identifiers never reach the DOM, and nothing paginates.
    expect(document.body.innerHTML).not.toContain("0f0e0d0c-0000-4000-8000");
    expect(screen.queryByRole("button", { name: /próxima|anterior|carregar mais|ver mais/i })).not.toBeInTheDocument();
  });

  it("shows a failed history read as an error with a retry, not as an empty history", async () => {
    let fail = true;
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
      [`GET ${CREDIT_LOTS}`]: () => (fail ? response({}, 500) : response(creditLots([lot()]))),
    });
    renderBilling();

    const history = await screen.findByTestId("credit-history");
    const alert = await within(history).findByRole("alert");
    expect(alert).toHaveTextContent("Não foi possível ler o histórico de créditos.");
    expect(within(history).queryByTestId("credit-history-empty")).not.toBeInTheDocument();
    fail = false;
    fireEvent.click(within(alert).getByRole("button", { name: "Tentar novamente" }));
    expect(await within(history).findByRole("table")).toBeInTheDocument();
    expect(calls(fetchMock, "GET", CREDIT_LOTS)).toHaveLength(2);
  });

  it("shows the balance by origin, debt, reservations, expiries and consumption from the ledger", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET ${CREDIT_SUMMARY}`]: () =>
        response(
          creditSummary({
            balance: { open_debt_mc: 1_500, net_available_mc: 504_500 },
            debt: {
              open_debt_mc: 1_500,
              debts: [
                {
                  debt_id: "debt-1",
                  source_kind: "consumption",
                  amount_mc: 1_500,
                  settled_mc: 0,
                  open_mc: 1_500,
                  created_at: "2026-10-29T12:00:00Z",
                },
              ],
            },
          }),
        ),
    });
    renderBilling();

    expect(await screen.findByTestId("credit-panel")).toBeInTheDocument();
    expect(metric("Franquia mensal")).toHaveTextContent("10 créditos");
    expect(metric("Recargas")).toHaveTextContent("496 créditos");
    expect(metric("Dívida")).toHaveTextContent("1,5 créditos");
    expect(metric("Reservas do ciclo")).toHaveTextContent("2 créditos");
    expect(metric("Disponível líquido")).toHaveTextContent("504,5 créditos");
    expect(metric("Franquia do próximo ciclo")).toHaveTextContent("25 créditos");
    expect(screen.getByTestId("credit-expiries")).toHaveTextContent(/Recarga.*496 créditos/);
    expect(screen.getByTestId("credit-debts")).toHaveTextContent(/Consumo acima do saldo.*1,5 créditos/);
    expect(screen.getByTestId("credit-reservations")).toHaveTextContent(/Base de conhecimento\s*3 créditos/);
    expect(screen.getByTestId("credit-reservations")).toHaveTextContent(/Mídias\s*2 créditos/);
    expect(screen.getByTestId("credit-consumption")).toHaveTextContent(/Consumo cobrado no ciclo/);
    expect(screen.getByTestId("credit-consumption")).toHaveTextContent(/Decisões\s*12 créditos/);
    // Enforced: no observational notice.
    expect(screen.queryByTestId("credit-mode")).not.toBeInTheDocument();
  });

  it("marks consumption as not charged while the mode is observational", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET ${CREDIT_SUMMARY}`]: () =>
        response(creditSummary({ state: { mode: "observational", enforcement_at: null } })),
    });
    renderBilling();

    expect(await screen.findByTestId("credit-mode")).toHaveTextContent(/Modo observacional/);
    expect(screen.getByTestId("credit-consumption")).toHaveTextContent(
      /Consumo estimado no ciclo \(não cobrado\)\s*Decisões\s*1,5 créditos/,
    );
  });

  it.each([
    [
      "pending",
      { pending_reasons: ["quarantine"] },
      /Pendência financeira: um pagamento está em verificação/,
      false,
    ],
    [
      "grace",
      {
        exhausted_at: "2026-10-29T12:00:00Z",
        suspension_due_at: "2026-11-01T12:00:00Z",
        deletion_due_at: "2026-11-28T12:00:00Z",
        export_deadline_at: "2026-11-28T12:00:00Z",
      },
      /serviço continua como dívida até 01\/11\/2026/,
      true,
    ],
    [
      "suspended",
      {
        exhausted_at: "2026-10-20T12:00:00Z",
        suspension_due_at: "2026-10-23T12:00:00Z",
        deletion_due_at: "2026-11-19T12:00:00Z",
        export_deadline_at: "2026-11-19T12:00:00Z",
      },
      /Serviço suspenso por falta de créditos/,
      true,
    ],
    [
      "deletion_due",
      {
        exhausted_at: "2026-09-20T12:00:00Z",
        suspension_due_at: "2026-09-23T12:00:00Z",
        deletion_due_at: "2026-10-20T12:00:00Z",
        export_deadline_at: "2026-10-20T12:00:00Z",
      },
      /Prazo de retenção encerrado/,
      true,
    ],
  ])("shows the %s state from the server", async (state, fields, message, deadline) => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary({ state: { state, ...fields } })),
    });
    renderBilling();

    const notice = await screen.findByTestId("credit-state");
    expect(notice).toHaveAttribute("data-state", state);
    expect(notice).toHaveTextContent(message);
    if (deadline) {
      expect(notice).toHaveTextContent(/Exporte seus dados até \d{2}\/\d{2}\/\d{4}/);
    } else {
      expect(notice).not.toHaveTextContent(/Exporte seus dados/);
    }
  });

  it.each([
    [["quarantine"], /um pagamento está em verificação/],
    [["reconciliation"], /o vínculo entre um pagamento e os seus créditos está em conciliação/],
    [["a_future_reason"], /^PendentePendência financeira em verificação pelo servidor\./],
    [[], /^PendentePendência financeira em verificação pelo servidor\./],
  ])("fails closed on a pending ledger %j: text-labelled, no balance as usable, no top-up", async (pending_reasons, line) => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(topupSummary),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary({ state: { state: "pending", pending_reasons } })),
    });
    renderBilling();

    const notice = await screen.findByTestId("credit-state");
    expect(notice).toHaveAttribute("data-state", "pending");
    expect(screen.getByTestId("credit-notices")).toHaveAttribute("role", "status");
    expect(screen.getByTestId("credit-notices")).toContainElement(notice);
    expect(notice).toHaveTextContent(line);
    expect(notice).toHaveTextContent(/O saldo fica retido e não é exibido como disponível/);
    expect(notice).not.toHaveTextContent(/não pertence a um builder/);
    // No figure is presented as usable while the ledger is pending.
    expect(screen.queryByRole("group", { name: "Disponível líquido" })).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Recargas" })).not.toBeInTheDocument();
    expect(screen.queryByText("506 créditos")).not.toBeInTheDocument();
    // Top-ups are open commercially, yet the pending ledger exposes no action.
    expect(screen.queryByRole("button", { name: /Recarregar/ })).not.toBeInTheDocument();
    expect(screen.getByTestId("topup-blocked")).toHaveTextContent(/pendência financeira em verificação/);
    expect(screen.getByRole("heading", { level: 1, name: "Pagamento" })).toBeInTheDocument();
  });

  it.each(
    (["grace", "suspended", "deletion_due"] as const).flatMap((state) =>
      ["quarantine", "reconciliation", "a_future_reason"].map((reason) => [state, reason] as const),
    ),
  )("holds the ledger in %s with the reason %s: no balance, no Recarregar, the reason shown", async (state, reason) => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(topupSummary),
      [`GET ${CREDIT_SUMMARY}`]: () =>
        response(
          creditSummary({
            state: {
              state,
              pending_reasons: [reason],
              exhausted_at: "2026-10-20T12:00:00Z",
              suspension_due_at: "2026-10-23T12:00:00Z",
              deletion_due_at: "2026-11-19T12:00:00Z",
              export_deadline_at: "2026-11-19T12:00:00Z",
            },
          }),
        ),
      [`GET ${CREDIT_LOTS}`]: () => response(creditLots([lot()])),
    });
    renderBilling();

    const notice = await screen.findByTestId("credit-state");
    expect(notice).toHaveAttribute("data-state", "pending");
    expect(notice).toHaveTextContent(
      reason === "quarantine"
        ? /um pagamento está em verificação/
        : reason === "reconciliation"
          ? /em conciliação pelo servidor/
          : /Pendência financeira em verificação pelo servidor\./,
    );
    expect(screen.getByTestId("credit-hold-lifecycle")).toHaveTextContent(/Exporte seus dados até 19\/11\/2026/);
    expect(screen.queryByRole("group", { name: "Disponível líquido" })).not.toBeInTheDocument();
    expect(screen.queryByText("506 créditos")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Recarregar" })).not.toBeInTheDocument();
    expect(screen.getByTestId("topup-blocked")).toHaveTextContent(/pendência financeira em verificação/);
    // The listed grant is a server record, not spendable balance.
    expect(await screen.findByTestId("credit-lot-status")).toHaveTextContent("Retido");
  });

  it.each(["grace", "suspended", "deletion_due"] as const)(
    "shows the balance and offers Recarregar in %s without a hold reason",
    async (state) => {
      routeFetch(fetchMock, {
        "GET /v1/billing/summary": () => response(topupSummary),
        [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary({ state: { state, pending_reasons: [] } })),
      });
      renderBilling();

      expect(await screen.findByRole("button", { name: "Recarregar" })).toBeEnabled();
      expect(metric("Disponível líquido")).toHaveTextContent("506 créditos");
      expect(screen.getByTestId("credit-state")).toHaveAttribute("data-state", state);
      expect(screen.queryByTestId("topup-blocked")).not.toBeInTheDocument();
    },
  );

  it("shows a standalone tenant's credits as not applicable, without zeros or history", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET ${CREDIT_SUMMARY}`]: () =>
        response(
          creditSummary({
            state: { state: "pending", pending_reasons: ["standalone"] },
            balance: { monthly_available_mc: 0, topup_available_mc: 0, available_mc: 0, net_available_mc: 0 },
          }),
        ),
    });
    renderBilling();

    const notice = await screen.findByTestId("credit-state");
    expect(notice).toHaveAttribute("data-state", "not_applicable");
    expect(notice).toHaveTextContent("Não se aplica: esta clínica não pertence a um builder com conta de créditos.");
    expect(screen.queryByRole("group", { name: "Disponível líquido" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("credit-history")).not.toBeInTheDocument();
    expect(screen.getByTestId("credits-and-topups").textContent ?? "").not.toMatch(/\d\s*créditos/);
  });

  it.each(["pending", "active", "grace", "suspended", "deletion_due"] as const)(
    "shows a not-applicable ledger in %s with top-ups open but no top-up action or notice",
    async (state) => {
      routeFetch(fetchMock, {
        // Flag on and the packages listed: only the ledger keeps the top-up away.
        "GET /v1/billing/summary": () => response(topupSummary),
        [`GET ${CREDIT_SUMMARY}`]: () =>
          response(
            creditSummary({
              state: { state, pending_reasons: ["standalone"] },
              balance: { monthly_available_mc: 0, topup_available_mc: 0, available_mc: 0, net_available_mc: 0 },
            }),
          ),
      });
      renderBilling();

      const notice = await screen.findByTestId("credit-state");
      expect(notice).toHaveAttribute("data-state", "not_applicable");
      expect(notice).toHaveTextContent("Não se aplica: esta clínica não pertence a um builder com conta de créditos.");
      await waitFor(() => expect(calls(fetchMock, "GET", CREDIT_CATALOG).length).toBeGreaterThan(0));
      const credits = screen.getByTestId("credits-and-topups");
      await waitFor(() => expect(credits.textContent ?? "").not.toMatch(/Recarga indisponível/));
      expect(screen.queryByTestId("topup-blocked")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Recarregar" })).not.toBeInTheDocument();
    },
  );

  it("shows no state notice for an active account", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
    });
    renderBilling();

    expect(await screen.findByTestId("credit-panel")).toBeInTheDocument();
    expect(screen.queryByTestId("credit-state")).not.toBeInTheDocument();
  });

  it("downloads the export with the builder session", async () => {
    const blob = new Blob(["PK\u0003\u0004"], { type: "application/zip" });
    const createObjectURL = vi.fn(() => "blob:credit-export");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
      "GET /v1/credits/export": () =>
        ({
          ok: true,
          status: 200,
          headers: new Headers({
            "Content-Disposition": 'attachment; filename="neurostack-export-tenant.zip"',
          }),
          blob: async () => blob,
        }) as Response,
    });
    renderBilling();

    fireEvent.click(await screen.findByRole("button", { name: /Exportar dados/i }));

    await waitFor(() => expect(createObjectURL).toHaveBeenCalledWith(blob));
    const [exportCall] = calls(fetchMock, "GET", "/v1/credits/export");
    expect((exportCall[1] as RequestInit).headers).toEqual({ Authorization: "Bearer test-token" });
    expect(click).toHaveBeenCalledTimes(1);
    const anchor = click.mock.instances[0] as unknown as HTMLAnchorElement;
    expect(anchor.download).toBe("neurostack-export-tenant.zip");
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:credit-export");
    expect(screen.getByRole("button", { name: /Exportar dados/i })).toBeEnabled();
  });

  it("makes the export unavailable when the export answers 503", async () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
      "GET /v1/credits/export": CREDIT_UNAVAILABLE,
    });
    renderBilling();

    const button = await screen.findByRole("button", { name: /Exportar dados/i });
    fireEvent.click(button);

    expect(await screen.findByText(/Exportação indisponível no momento/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Exportar dados/i })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /Exportar dados/i }));
    expect(calls(fetchMock, "GET", "/v1/credits/export")).toHaveLength(1);
    expect(click).not.toHaveBeenCalled();
  });

  async function openTopup() {
    const open = await screen.findByRole("button", { name: "Recarregar" });
    fireEvent.click(open);
    const heading = await screen.findByRole("heading", { level: 3, name: "Recarregar créditos" });
    await waitFor(() => expect(document.activeElement).toBe(heading));
    return heading;
  }

  it.each([
    // The packages ARE listed (canary or a live attempt): only the flag hides the action.
    ["the flag false", summaryFixture({ topup_commercial_enabled: false, checkout: topupSummary.checkout })],
    [
      "an advisory read that failed (inconclusive)",
      summaryFixture({ topup_commercial_enabled: false, checkout: { available_offer_ids: [], live_session: null, available: false } }),
    ],
  ])("exposes no top-up action with %s", async (_label, billingSummary) => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(billingSummary),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
    });
    renderBilling();

    expect(await screen.findByTestId("credit-panel")).toBeInTheDocument();
    expect(screen.queryByTestId("topup-offer")).not.toBeInTheDocument();
    expect(screen.queryByTestId("topup-blocked")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Recarregar|Recargas indisponíveis/i })).not.toBeInTheDocument();
  });

  it.each([
    // A summary that never answers: the router returns the pending promise.
    ["is loading", (() => new Promise<Response>(() => {})) as unknown as Route],
    ["failed", () => response({}, 500)],
  ] as Array<[string, Route]>)("exposes no top-up action while the billing summary %s", async (_label, answer) => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": answer,
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
    });
    renderBilling();

    await waitFor(() => expect(calls(fetchMock, "GET", CREDIT_SUMMARY)).toHaveLength(1));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByRole("button", { name: /Recarregar/ })).not.toBeInTheDocument();
    expect(screen.queryByTestId("topup-offer")).not.toBeInTheDocument();
  });

  it("explains a top-up blocked by unreadable packages instead of guessing them", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(topupSummary),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
      [`GET ${CREDIT_CATALOG}`]: () => response({}, 500),
    });
    renderBilling();

    expect(await screen.findByTestId("topup-blocked")).toHaveTextContent(/Não foi possível ler os pacotes de recarga/);
    expect(screen.queryByRole("button", { name: /Recarregar/ })).not.toBeInTheDocument();
  });

  it("shows neither an error nor an action while the packages are still loading", async () => {
    let answer: (value: Response) => void = () => {};
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(topupSummary),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
      [`GET ${CREDIT_CATALOG}`]: () => new Promise<Response>((resolve) => (answer = resolve)) as unknown as Response,
    });
    renderBilling();

    expect(await screen.findByTestId("credit-panel")).toBeInTheDocument();
    await waitFor(() => expect(calls(fetchMock, "GET", CREDIT_CATALOG)).toHaveLength(1));
    await new Promise((resolve) => setTimeout(resolve, 20));
    // In between: loading is not "could not read the packages".
    expect(screen.queryByTestId("topup-blocked")).not.toBeInTheDocument();
    expect(screen.queryByText(/Não foi possível ler os pacotes de recarga/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Recarregar" })).not.toBeInTheDocument();

    answer(response(creditCatalog));
    expect(await screen.findByRole("button", { name: "Recarregar" })).toBeEnabled();
    expect(screen.queryByTestId("topup-blocked")).not.toBeInTheDocument();
  });

  it("moves focus to the checkout status when a top-up POST answers without a provider URL", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(topupSummary),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
      "POST /v1/billing/checkout": () => response({ ...session("creating"), kind: "topup" }),
    });
    renderBilling();

    await openTopup();
    fireEvent.click(screen.getByRole("button", { name: /^500 créditos/ }));
    fireEvent.click(screen.getByRole("button", { name: "PIX" }));
    const buy = screen.getByRole("button", { name: /Ir para o pagamento da recarga/i });
    buy.focus();
    fireEvent.click(buy);

    const status = await screen.findByTestId("checkout-status");
    await waitFor(() => expect(document.activeElement).toBe(status));
    expect(status).toHaveAttribute("tabindex", "-1");
    expect(status).toHaveAccessibleName("Criando o checkout no provedor");
    expect(document.activeElement).not.toBe(document.body);
    expect(calls(fetchMock, "POST", "/v1/billing/checkout")).toHaveLength(1);
    expect(navigateToHostedCheckout).not.toHaveBeenCalled();
  });

  it("moves focus to the checkout status when a subscription POST answers without a provider URL", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      "POST /v1/billing/checkout": () => response(session("creating")),
    });
    renderBilling();

    await startCheckout("Plus");
    const status = await screen.findByTestId("checkout-status");
    await waitFor(() => expect(document.activeElement).toBe(status));
    expect(calls(fetchMock, "POST", "/v1/billing/checkout")).toHaveLength(1);
  });

  it("offers only the packages the server lists as available, with the catalog's prices and validity", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () =>
        response(
          summaryFixture({
            topup_commercial_enabled: true,
            checkout: { available_offer_ids: ["package_credits_1000_v1", "package_credits_4000_v1"], live_session: null, available: true },
          }),
        ),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
    });
    renderBilling();

    await openTopup();
    const packages = screen.getByRole("group", { name: "Pacote" });
    expect(within(packages).getAllByRole("button").map((b) => b.textContent?.replace(/\u00a0/g, " "))).toEqual([
      `1.000 créditos${brl(8_800)} · válida por 6 meses`,
      `4.000 créditos${brl(35_200)} · válida por 6 meses`,
    ]);
  });

  it("sells a package by card or PIX sending only offer_id and method, granting nothing locally", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(topupSummary),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
      "POST /v1/billing/checkout": () =>
        response({ ...session("active", HOSTED_URL), kind: "topup" }),
    });
    renderBilling();

    await openTopup();
    const offer = screen.getByTestId("topup-offer");
    const packages = screen.getByRole("group", { name: "Pacote" });
    expect(within(packages).getAllByRole("button")).toHaveLength(4);
    const buy = screen.getByRole("button", { name: /Ir para o pagamento da recarga/i });
    // Temporarily disabled with its reason associated, never silently.
    expect(buy).toBeDisabled();
    expect(buy).toHaveAccessibleDescription("Escolha um pacote e a forma de pagamento para continuar.");
    fireEvent.click(within(packages).getByRole("button", { name: /^2\.000 créditos/ }));
    const methods = screen.getByRole("group", { name: "Forma de pagamento" });
    expect(within(methods).getAllByRole("button").map((b) => b.textContent)).toEqual(["Cartão de crédito", "PIX"]);
    fireEvent.click(within(methods).getByRole("button", { name: "PIX" }));
    expect(buy).toBeEnabled();
    fireEvent.click(buy);
    fireEvent.click(buy);

    await waitFor(() => expect(navigateToHostedCheckout).toHaveBeenCalledWith(HOSTED_URL));
    const posts = calls(fetchMock, "POST", "/v1/billing/checkout");
    expect(posts).toHaveLength(1);
    expect(JSON.parse(String((posts[0][1] as RequestInit).body))).toEqual({
      offer_id: "package_credits_2000_v1",
      method: "pix",
    });
    expect(offer).toHaveTextContent(/válida por 6 meses/);
    // The purchase anticipated nothing: the balance is still the ledger's,
    // and the open checkout now blocks another purchase.
    expect(metric("Recargas")).toHaveTextContent("496 créditos");
    expect(await screen.findByTestId("topup-blocked")).toHaveTextContent(/checkout em andamento/);
    expect(sessionStorage.getItem(SESSION_KEY)).toBe(SESSION_ID);
  });

  it("returns focus to Recarregar when the top-up panel closes", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(topupSummary),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
    });
    renderBilling();

    await openTopup();
    fireEvent.click(screen.getByRole("button", { name: "Fechar recarga" }));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Recarregar" })));
  });

  it("keeps offering top-ups to a suspended account, which the ledger asks to top up", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(topupSummary),
      [`GET ${CREDIT_SUMMARY}`]: () =>
        response(
          creditSummary({
            state: { state: "suspended", exhausted_at: "2026-10-20T12:00:00Z", suspension_due_at: "2026-10-23T12:00:00Z" },
          }),
        ),
    });
    renderBilling();

    expect(await screen.findByTestId("credit-state")).toHaveTextContent(/Recarregue para reativar/);
    expect(screen.getByRole("button", { name: "Recarregar" })).toBeEnabled();
  });

  it.each([
    ["the commercial flag false", summaryFixture({ topup_commercial_enabled: false, checkout: topupSummary.checkout }), undefined],
    [
      "a live checkout",
      summaryFixture({
        topup_commercial_enabled: true,
        checkout: {
          ...topupSummary.checkout,
          live_session: { checkout_session_id: "7c1d2e3f-0000-4000-8000-00000000c0de", kind: "topup", state: "active", offer_id: "package_credits_500_v1" },
        },
      }),
      undefined,
    ],
    ["unreadable packages", topupSummary, () => response({}, 500)],
  ] as Array<[string, unknown, Route | undefined]>)(
    "does not tell a suspended account to top up with %s (no Recarregar exists)",
    async (_label, billingSummary, catalogAnswer) => {
      routeFetch(fetchMock, {
        "GET /v1/billing/summary": () => response(billingSummary),
        [`GET ${CREDIT_SUMMARY}`]: () =>
          response(
            creditSummary({
              state: { state: "suspended", exhausted_at: "2026-10-20T12:00:00Z", suspension_due_at: "2026-10-23T12:00:00Z", export_deadline_at: "2026-11-19T12:00:00Z" },
            }),
          ),
        ...(catalogAnswer ? { [`GET ${CREDIT_CATALOG}`]: catalogAnswer } : {}),
      });
      renderBilling();

      const notice = await screen.findByTestId("credit-state");
      await waitFor(() => expect(calls(fetchMock, "GET", CREDIT_CATALOG)).toHaveLength(1));
      await new Promise((resolve) => setTimeout(resolve, 20));
      expect(notice).toHaveTextContent(/Serviço suspenso por falta de créditos\./);
      expect(notice).toHaveTextContent(/Exporte seus dados até 19\/11\/2026/);
      expect(notice).not.toHaveTextContent(/Recarregue/);
      expect(screen.queryByRole("button", { name: "Recarregar" })).not.toBeInTheDocument();
    },
  );

  it("keeps the subscription a monthly card subscription, apart from the top-ups", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(topupSummary),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
      "POST /v1/billing/checkout": () => response(session("active", HOSTED_URL)),
    });
    renderBilling();

    await openTopup();
    fireEvent.click(screen.getByRole("button", { name: /^500 créditos/ }));
    await startCheckout("Plus");
    await waitFor(() => expect(calls(fetchMock, "POST", "/v1/billing/checkout")).toHaveLength(1));
    const [post] = calls(fetchMock, "POST", "/v1/billing/checkout");
    expect(JSON.parse(String((post[1] as RequestInit).body))).toEqual({
      offer_id: "plan_plus_monthly_v1",
      method: "credit_card",
    });
    // PIX is offered only inside the top-up panel; the subscription offers no
    // PIX, Pix Automático, annual cycle or installments.
    const topup = screen.queryByTestId("topup-offer");
    for (const element of screen.queryAllByText(/\bpix\b/i)) {
      expect(topup?.contains(element)).toBe(true);
    }
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/pix autom|anual|annual|parcel|installment/i);
  });

  it.each([
    [409, "billing_checkout_not_eligible"],
    [503, "billing_checkout_unavailable"],
  ])("maps a %s top-up refusal without creating anything", async (status, detail) => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(topupSummary),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
      "POST /v1/billing/checkout": () => response({ detail }, status),
    });
    renderBilling();

    await openTopup();
    fireEvent.click(screen.getByRole("button", { name: /^500 créditos/ }));
    fireEvent.click(screen.getByRole("button", { name: "Cartão de crédito" }));
    fireEvent.click(screen.getByRole("button", { name: /Ir para o pagamento da recarga/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Recargas estão indisponíveis no momento/);
    expect(alert).not.toHaveTextContent(detail);
    expect(navigateToHostedCheckout).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(SESSION_KEY)).toBeNull();
  });

  it("re-reads the order and the ledger after a return and never anticipates the grant", async () => {
    sessionStorage.setItem(SESSION_KEY, SESSION_ID);
    window.history.replaceState({}, "", "/billing?checkout=success");
    let ledger = creditSummary({ balance: { topup_available_mc: 0, available_mc: 10_000 } });
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(topupSummary),
      [`GET ${CREDIT_SUMMARY}`]: () => response(ledger),
      [`GET /v1/billing/checkout/${SESSION_ID}`]: () =>
        response({ ...session("active", HOSTED_URL), kind: "topup" }),
    });
    renderBilling();

    expect(await screen.findByText(/o QR do PIX não adicionam créditos/i)).toBeInTheDocument();
    await screen.findByTestId("credit-panel");
    // The return marker activates nothing: the top-up is not in the balance.
    expect(metric("Recargas")).toHaveTextContent("0 créditos");
    expect(calls(fetchMock, "GET", CREDIT_SUMMARY)).toHaveLength(1);
    expect(calls(fetchMock, "GET", `/v1/billing/checkout/${SESSION_ID}`)).toHaveLength(1);
    // Buying is blocked (no action, a reason) while that checkout is live.
    expect(await screen.findByTestId("topup-blocked")).toHaveTextContent(/checkout em andamento/);
    expect(screen.queryByRole("button", { name: "Recarregar" })).not.toBeInTheDocument();
    expect(navigateToHostedCheckout).not.toHaveBeenCalled();

    // The server applied the grant: only a re-read shows it.
    ledger = creditSummary({ balance: { topup_available_mc: 500_000 } });
    fireEvent.click(screen.getByRole("button", { name: /Atualizar estado/i }));
    await waitFor(() => expect(calls(fetchMock, "GET", CREDIT_SUMMARY)).toHaveLength(2));
    await waitFor(() => expect(metric("Recargas")).toHaveTextContent("500 créditos"));
    expect(calls(fetchMock, "GET", `/v1/billing/checkout/${SESSION_ID}`)).toHaveLength(2);
  });

  it("keeps a payment pending across a refresh from the summary's live_session alone", async () => {
    // No stored session (another tab, or storage cleared): the server's live
    // attempt still blocks subscribing and top-ups and is shown as pending.
    const LIVE_ID = "7c1d2e3f-0000-4000-8000-00000000c0de";
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () =>
        response(
          summaryFixture({
            topup_commercial_enabled: true,
            checkout: {
              available_offer_ids: [PLUS, PRO, MAX, ...TOPUP_IDS],
              live_session: { checkout_session_id: LIVE_ID, kind: "topup", state: "active", offer_id: "package_credits_500_v1" },
              available: true,
            },
          }),
        ),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
    });
    renderBilling();

    const status = await screen.findByTestId("checkout-status");
    expect(status).toHaveTextContent("Checkout aberto, aguardando pagamento");
    expect(status).toHaveTextContent(/o QR do PIX não adicionam créditos/);
    // The summary carries no provider URL: nothing to follow automatically.
    expect(within(status).queryByRole("button", { name: /Abrir checkout hospedado/ })).not.toBeInTheDocument();
    expect(await screen.findByTestId("topup-blocked")).toHaveTextContent(/checkout em andamento nesta conta/);
    for (const button of screen.getAllByRole("button", { name: /^Assinar / })) {
      expect(button).toBeDisabled();
      expect(button).toHaveAccessibleDescription(/checkout em andamento nesta conta/);
    }
    expect(metric("Recargas")).toHaveTextContent("496 créditos");
    expect(sessionStorage.getItem(SESSION_KEY)).toBeNull();
    expect(navigateToHostedCheckout).not.toHaveBeenCalled();
    expect(calls(fetchMock, "POST", "/v1/billing/checkout")).toHaveLength(0);
  });

  it("treats a malformed live_session as a failed summary, not as nothing live", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () =>
        response(
          summaryFixture({
            checkout: { available_offer_ids: [PLUS], live_session: { kind: "topup" }, available: true },
          }),
        ),
    });
    renderBilling();

    expect(await screen.findByTestId("billing-context-alert")).toHaveTextContent(/Não foi possível carregar/);
    expect(screen.queryAllByRole("button", { name: /^Assinar / })).toHaveLength(0);
  });

  it("keeps Recarregar as the only purchase action: the usage card has none", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(topupSummary),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
    });
    renderBilling();

    expect(await screen.findByRole("button", { name: "Recarregar" })).toBeEnabled();
    expect(within(screen.getByTestId("plan-usage")).queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Comprar créditos/i })).not.toBeInTheDocument();
  });
});

// ── Pagamento: composition, canonical data and fail-closed states ──────────
// Header, three contiguous areas, subscription overview, usage and the plan
// comparison, all fed by synthetic contract fixtures. No figure on screen may
// come from the page itself.

describe("Pagamento page", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    Element.prototype.scrollIntoView = vi.fn();
    sessionStorage.clear();
  });

  afterEach(() => {
    cleanup();
    fetchMock.mockReset();
    vi.mocked(navigateToHostedCheckout).mockReset();
    vi.unstubAllGlobals();
    window.history.replaceState({}, "", "/");
  });

  function areaHeadings() {
    return screen.getAllByTestId("billing-area").map((area) => within(area).getByRole("heading", { level: 2 }).textContent);
  }

  function planRow(offerId: string) {
    return screen.getByTestId(`plan-row-${offerId}`);
  }

  it("shows the PT header and the three contiguous areas in order", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
    });
    renderBilling();

    expect(await screen.findByRole("heading", { level: 1, name: "Pagamento" })).toBeInTheDocument();
    const breadcrumb = screen.getByRole("navigation", { name: "Trilha de navegação" });
    expect(breadcrumb).toHaveTextContent(/NeuroStack.*Financeiro/);
    expect(screen.getByText("Acompanhe sua assinatura, o consumo do plano e seus créditos.")).toBeInTheDocument();
    await screen.findByTestId("credit-panel");
    expect(areaHeadings()).toEqual(["Assinatura e uso", "Planos", "Créditos e recargas"]);
    // Decorative numbers are contiguous: 01, 02, 03 and nothing else.
    const numbers = screen.getAllByTestId("billing-area").map((area) => area.querySelector("[aria-hidden='true'] span")?.textContent);
    expect(numbers).toEqual(["01", "02", "03"]);
    // Overview and usage live in the first area, the comparison in the second.
    const [first, second] = screen.getAllByTestId("billing-area");
    expect(within(first).getByTestId("subscription-overview")).toBeInTheDocument();
    expect(within(first).getByTestId("plan-usage")).toBeInTheDocument();
    expect(within(second).getByRole("table", { name: "Comparação de planos" })).toBeInTheDocument();
    // No roadmap, invoice skeleton, fabricated trial or stable-state banner.
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/Roadmap|Histórico de Faturas|Em breve|Trial|Faturamento e Gestão/);
    expect(screen.queryByTestId("billing-context-alert")).not.toBeInTheDocument();
  });

  it("runs the full EN flow: header, areas and one hosted checkout for an eligible account", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
      "POST /v1/billing/checkout": () => response(session("active", HOSTED_URL)),
    });
    renderBilling("en");

    expect(await screen.findByRole("heading", { level: 1, name: "Payment" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Breadcrumb" })).toHaveTextContent(/NeuroStack.*Finance/);
    expect(screen.getByText("Track your subscription, plan usage and credits.")).toBeInTheDocument();
    await screen.findByTestId("credit-panel");
    expect(areaHeadings()).toEqual(["Subscription and usage", "Plans", "Credits and top-ups"]);
    expect(await screen.findByTestId("subscription-none")).toHaveTextContent("This account has no subscription.");
    const table = await screen.findByRole("table", { name: "Plan comparison" });
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual([
      "Plan",
      "Monthly price",
      "Credits per month",
      "Included clinics",
      "Price per additional clinic/month",
      "Action",
    ]);
    expect(planRow(PRO)).toHaveTextContent(`${brl(222_200, "en-US")}/month`);

    const subscribe = within(planRow(PRO)).getByRole("button", { name: "Subscribe to Pro" });
    await waitFor(() => expect(subscribe).toBeEnabled());
    fireEvent.click(subscribe);
    fireEvent.click(screen.getByRole("button", { name: /Continue to secure checkout/i }));

    await waitFor(() => expect(navigateToHostedCheckout).toHaveBeenCalledTimes(1));
    expect(navigateToHostedCheckout).toHaveBeenCalledWith(HOSTED_URL);
    const posts = calls(fetchMock, "POST", "/v1/billing/checkout");
    expect(posts).toHaveLength(1);
    expect(JSON.parse(String((posts[0][1] as RequestInit).body))).toEqual({ offer_id: PRO, method: "credit_card" });
    expect(document.body.textContent ?? "").not.toMatch(/annual|installment|plan switch|Coming soon/i);
  });

  it("presents an active subscriber from the summary and the catalog only", async () => {
    routeFetch(fetchMock, { "GET /v1/billing/summary": () => response(activeSummary) });
    renderBilling();

    const overview = await screen.findByTestId("subscription-overview");
    await waitFor(() => expect(overview).toHaveAttribute("data-state", "ready"));
    await waitFor(() => expect(within(overview).getByRole("group", { name: "Mensalidade" })).toHaveTextContent(brl(222_200)));
    expect(overview).toHaveTextContent("NeuroStack Pro");
    expect(screen.getByTestId("subscription-status")).toHaveTextContent("Ativa · Pagamento em dia");
    expect(within(overview).getByRole("group", { name: "Ciclo" })).toHaveTextContent("Mensal");
    expect(within(overview).getByRole("group", { name: "Próxima renovação" })).toHaveTextContent("17/11/2026");
    expect(within(overview).getByRole("group", { name: "Créditos por mês" })).toHaveTextContent("2.200 créditos");
    expect(within(overview).getByRole("group", { name: "Clínicas incluídas" })).toHaveTextContent("2");
    expect(within(overview).getByRole("group", { name: "Clínicas ativas" })).toHaveTextContent("2");
    expect(within(overview).getByRole("group", { name: "Preço por clínica adicional/mês" })).toHaveTextContent(brl(22_000));
    // The legacy default-carrying fields never render.
    const text = document.body.textContent ?? "";
    for (const legacy of ["legacy-plan", "97", "96", "99.996", brl(9_999), brl(999_600)]) {
      expect(text).not.toContain(legacy);
    }
    // The legacy prices are in reais: also pin the forms a leak would render
    // (`base_price` 9_999 as R$ 9.999,00, and its siblings), not only cents.
    for (const reais of [LEGACY_FIELDS.base_price, LEGACY_FIELDS.extra_price, LEGACY_FIELDS.total_monthly, LEGACY_FIELDS.total_annual]) {
      expect(text).not.toContain(brl(reais * 100));
    }
    expect(text).not.toMatch(/9\.99[6-9]/);
    // `subscription_status: "trial"` is a legacy default, never a state shown.
    expect(text).not.toMatch(/trial/i);
    // Current plan by text (and aria-current), never by colour alone.
    expect(within(planRow(PRO)).getByText("Plano atual")).toBeInTheDocument();
    expect(planRow(PRO)).toHaveAttribute("aria-current", "true");
    expect(within(planRow(PLUS)).queryByText("Plano atual")).not.toBeInTheDocument();
    expect(screen.queryAllByRole("button", { name: /^Assinar |Migrar/ })).toHaveLength(0);
    // A stable, paid subscription takes no contextual alert.
    expect(screen.queryByTestId("billing-context-alert")).not.toBeInTheDocument();
  });

  it.each([
    ["pending", "readback_required", /Pagamento pendente: a assinatura só é confirmada/, "Em verificação pelo provedor · Pagamento pendente"],
    ["overdue", "active", /Pagamento em atraso: o período pago terminou/, "Ativa · Pagamento em atraso"],
    ["reversed", "active", /último pagamento da assinatura foi estornado/, "Ativa · Pagamento estornado"],
  ])("announces a %s payment as an actionable status", async (payment_state, status, alert, label) => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () =>
        response({ ...activeSummary, subscription: activeSubscription({ payment_state, status }) }),
    });
    renderBilling();

    const notice = await screen.findByTestId("billing-context-alert");
    expect(notice).toHaveAttribute("role", "status");
    expect(notice).toHaveTextContent(alert);
    expect(screen.getByTestId("subscription-status")).toHaveTextContent(label);
    expect(screen.queryByText("Ativa · Pagamento em dia")).not.toBeInTheDocument();
  });

  it.each([
    ["readback_required", "pending"],
    ["non_billable_pending_readback", "paid"],
    ["active", "pending"],
  ])("qualifies the plan of an unconfirmed subscription (%s / %s): awaiting confirmation, not current", async (status, payment_state) => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response({ ...activeSummary, subscription: activeSubscription({ status, payment_state }) }),
    });
    renderBilling();

    await screen.findByRole("table", { name: "Comparação de planos" });
    const marker = within(planRow(PRO)).getByTestId("plan-marker");
    expect(marker).toHaveTextContent("Aguardando confirmação");
    expect(within(planRow(PRO)).queryByText("Plano atual")).not.toBeInTheDocument();
    expect(planRow(PRO)).not.toHaveAttribute("aria-current");
    expect(screen.queryByText("Plano atual")).not.toBeInTheDocument();
  });

  it.each([
    ["cancelled", "paid"],
    ["active", "overdue"],
    ["active", "reversed"],
  ])("keeps the current-plan marker for %s / %s", async (status, payment_state) => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response({ ...activeSummary, subscription: activeSubscription({ status, payment_state }) }),
    });
    renderBilling();

    await screen.findByRole("table", { name: "Comparação de planos" });
    expect(within(planRow(PRO)).getByTestId("plan-marker")).toHaveTextContent("Plano atual");
    expect(planRow(PRO)).toHaveAttribute("aria-current", "true");
  });

  it("shows a pending subscription with no paid cycle as absent, not as a date", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () =>
        response({
          ...activeSummary,
          subscription: activeSubscription({ status: "readback_required", payment_state: "pending", current_period_end: null }),
        }),
    });
    renderBilling();

    const overview = await screen.findByTestId("subscription-overview");
    await waitFor(() =>
      expect(within(overview).getByRole("group", { name: "Próxima renovação" })).toHaveTextContent("Nenhum ciclo pago ainda"),
    );
  });

  it.each([
    [503, "unavailable", /temporariamente indisponíveis/],
    [500, "error", /Não foi possível carregar os dados de pagamento/],
  ])("keeps a %s summary explicit, retryable and free of fabricated data", async (status, state, message) => {
    let answer: Response = response({ detail: "billing_summary_unavailable" }, status);
    routeFetch(fetchMock, { "GET /v1/billing/summary": () => answer });
    renderBilling();

    const alert = await screen.findByTestId("billing-context-alert");
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent(message);
    expect(alert).toHaveTextContent(/Nenhum plano, valor ou consumo foi presumido/);
    expect(screen.getByTestId("subscription-overview")).toHaveAttribute("data-state", state);
    expect(screen.getByTestId("plan-usage")).toHaveAttribute("data-state", state);
    const [first] = screen.getAllByTestId("billing-area");
    // No plan, price, clinic count or usage figure in the first area.
    expect(first.textContent ?? "").not.toMatch(/R\$|NeuroStack (Plus|Pro|Max)|Trial|Ativa|\d+\s*MB|Clínicas/);
    expect(screen.queryAllByRole("button", { name: /^Assinar / })).toHaveLength(0);

    answer = response(activeSummary);
    fireEvent.click(within(alert).getByRole("button", { name: "Tentar novamente" }));
    expect(await screen.findByTestId("subscription-status")).toHaveTextContent("Ativa");
    expect(calls(fetchMock, "GET", "/v1/billing/summary")).toHaveLength(2);
    expect(screen.queryByTestId("billing-context-alert")).not.toBeInTheDocument();
  });

  it("gives an unavailable summary and a failed summary different content", async () => {
    routeFetch(fetchMock, { "GET /v1/billing/summary": () => response({}, 503) });
    const first = renderBilling();
    const unavailable = (await screen.findByTestId("billing-context-alert")).textContent;
    const unavailableOverview = screen.getByTestId("subscription-overview").textContent;
    first.unmount();
    routeFetch(fetchMock, { "GET /v1/billing/summary": () => response({}, 500) });
    renderBilling();
    const failed = (await screen.findByTestId("billing-context-alert")).textContent;
    expect(failed).not.toEqual(unavailable);
    expect(screen.getByTestId("subscription-overview").textContent).not.toEqual(unavailableOverview);
  });

  it("treats a summary without the final read contract as an error, not as defaults", async () => {
    // The pre-6b shape (no `subscription`/`checkout`) must not be completed locally.
    const { subscription: _s, checkout: _c, topup_commercial_enabled: _t, ...legacyOnly } = summary;
    routeFetch(fetchMock, { "GET /v1/billing/summary": () => response(legacyOnly) });
    renderBilling();

    expect(await screen.findByTestId("billing-context-alert")).toHaveTextContent(/Não foi possível carregar/);
    expect(screen.getByTestId("subscription-overview")).toHaveAttribute("data-state", "error");
    expect(screen.queryAllByRole("button", { name: /^Assinar / })).toHaveLength(0);
  });

  it.each([
    [401, /Sua sessão expirou/],
    [403, /não tem acesso aos dados de pagamento/],
  ])("fails closed on a %s summary: no financial data in the DOM", async (status, message) => {
    // Even if the other reads answered, a refused summary reveals nothing.
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response({ detail: "Not authenticated" }, status),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
    });
    renderBilling();

    const alert = await screen.findByTestId("billing-context-alert");
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent(message);
    await waitFor(() => expect(calls(fetchMock, "GET", CREDIT_SUMMARY)).toHaveLength(1));
    await waitFor(() => expect(calls(fetchMock, "GET", CATALOG)).toHaveLength(1));
    expect(screen.queryAllByTestId("billing-area")).toHaveLength(0);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByTestId("credit-panel")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Tentar novamente|Assinar/ })).not.toBeInTheDocument();
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/R\$|\d\s*créditos|\bMB\b|NeuroStack (Plus|Pro|Max)|Trial|\d/);
  });

  it("fails closed when the catalog read is refused", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(activeSummary),
      [`GET ${CATALOG}`]: () => response({ detail: "Builder access required" }, 403),
    });
    renderBilling();

    expect(await screen.findByTestId("billing-context-alert")).toHaveTextContent(/não tem acesso/);
    expect(screen.queryAllByTestId("billing-area")).toHaveLength(0);
    expect(document.body.textContent ?? "").not.toMatch(/R\$|NeuroStack Pro/);
  });

  it("renders plans and clinic prices entirely from the catalog fixture", async () => {
    // Different invented values, a different order and version: the screen
    // must follow without any component edit.
    const altCatalog = {
      catalog_version_id: "catalog-test-v2",
      offers: [
        planOffer(MAX, 777_700, 7_700, 17, 7_100),
        planOffer(PLUS, 55_500, 550, 0, 5_100),
        planOffer(PRO, 666_600, 6_600, 16, 6_100),
      ],
    };
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () =>
        response({
          ...activeSummary,
          active_clinics: 0,
          subscription: activeSubscription({ offer_id: MAX, catalog_version_id: "catalog-test-v2", amount_cents: 777_700 }),
        }),
      [`GET ${CATALOG}`]: () => response(altCatalog),
    });
    renderBilling();

    const table = await screen.findByRole("table", { name: "Comparação de planos" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getByRole("rowheader").textContent)).toEqual([
      "NeuroStack MaxPlano atual",
      "NeuroStack Plus",
      "NeuroStack Pro",
    ]);
    expect(planRow(PLUS)).toHaveTextContent(`${brl(55_500)}/mês`);
    expect(planRow(PLUS)).toHaveTextContent("550 créditos");
    expect(within(planRow(PLUS)).getAllByRole("cell")[2]).toHaveTextContent("0");
    expect(planRow(PLUS)).toHaveTextContent(`${brl(5_100)}/mês`);
    expect(planRow(MAX)).toHaveTextContent("7.700 créditos");
    expect(planRow(PRO)).toHaveTextContent(`${brl(6_100)}/mês`);
    const overview = screen.getByTestId("subscription-overview");
    await waitFor(() => expect(within(overview).getByRole("group", { name: "Mensalidade" })).toHaveTextContent(brl(777_700)));
    expect(within(overview).getByRole("group", { name: "Clínicas incluídas" })).toHaveTextContent("17");
    // Zero is data: no active clinic is "0", not a dash or a blank.
    expect(within(overview).getByRole("group", { name: "Clínicas ativas" })).toHaveTextContent(/^Clínicas ativas0$/);
    // None of the first fixture's values survive.
    const text = document.body.textContent ?? "";
    for (const old of [brl(111_100), brl(222_200), brl(333_300), "1.100 créditos", "2.200 créditos", "3.300 créditos"]) {
      expect(text).not.toContain(old);
    }
    expect(text).not.toContain("—");
  });

  it("does not claim terms for a subscription sold under another catalog version", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () =>
        response({ ...activeSummary, subscription: activeSubscription({ catalog_version_id: "catalog-old" }) }),
    });
    renderBilling();

    await screen.findByRole("table", { name: "Comparação de planos" });
    const overview = screen.getByTestId("subscription-overview");
    expect(within(overview).getByRole("group", { name: "Créditos por mês" })).toHaveTextContent("Indisponível");
    expect(within(overview).getByRole("group", { name: "Mensalidade" })).toHaveTextContent("Indisponível");
    expect(screen.queryByText("Plano atual")).not.toBeInTheDocument();
  });

  it("offers subscribing only for the offers the server lists", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () =>
        response(summaryFixture({ checkout: { available_offer_ids: [PRO], live_session: null, available: true } })),
    });
    renderBilling();

    await waitFor(() => expect(screen.getAllByRole("button", { name: /^Assinar / })).toHaveLength(1));
    expect(within(planRow(PRO)).getByRole("button", { name: "Assinar Pro" })).toBeEnabled();
    expect(within(planRow(PLUS)).queryByRole("button")).not.toBeInTheDocument();
  });

  it.each([
    [{ available_offer_ids: [], live_session: null, available: false }, /Não foi possível confirmar agora quais planos podem ser contratados/],
    [{ available_offer_ids: [], live_session: null, available: true }, /Nenhum plano está disponível para contratação nesta conta/],
  ])("distinguishes unknown availability from nothing for sale (%#)", async (checkout, notice) => {
    routeFetch(fetchMock, { "GET /v1/billing/summary": () => response(summaryFixture({ checkout })) });
    renderBilling();

    expect(await screen.findByText(notice)).toBeInTheDocument();
    expect(screen.queryAllByRole("button", { name: /^Assinar / })).toHaveLength(0);
  });

  it("explains a temporarily blocked subscribe action", async () => {
    sessionStorage.setItem(SESSION_KEY, SESSION_ID);
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET /v1/billing/checkout/${SESSION_ID}`]: () => response(session("creating")),
    });
    renderBilling();

    expect(await screen.findByText("Criando o checkout no provedor")).toBeInTheDocument();
    const subscribe = await within(await screen.findByTestId(`plan-row-${PLUS}`)).findByRole("button", { name: "Assinar Plus" });
    expect(subscribe).toBeDisabled();
    expect(subscribe).toHaveAccessibleDescription(/checkout em andamento nesta conta/);
  });

  it("shows a catalog failure in the plans area with a retry and no action", async () => {
    let answer: Response = response({}, 500);
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET ${CATALOG}`]: () => answer,
    });
    renderBilling();

    // Credits switched off by default here: two areas.
    await waitFor(() => expect(screen.getAllByTestId("billing-area")).toHaveLength(2));
    const [, plansArea] = screen.getAllByTestId("billing-area");
    const alert = await within(plansArea).findByRole("alert");
    expect(alert).toHaveTextContent(/Não foi possível ler o catálogo de planos/);
    expect(screen.queryAllByRole("button", { name: /^Assinar / })).toHaveLength(0);

    answer = response(catalog);
    fireEvent.click(within(alert).getByRole("button", { name: "Tentar novamente" }));
    expect(await within(plansArea).findByRole("table")).toBeInTheDocument();
    expect(calls(fetchMock, "GET", CATALOG)).toHaveLength(2);
  });

  it("shows usage in RAG, Traces, Media order with zero as data", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () =>
        response(summaryFixture({ usage: usageFixture({ traces: 0, rag: 0, media: 0 }) })),
    });
    renderBilling();

    await waitFor(() => expect(screen.getByTestId("plan-usage")).toHaveAttribute("data-state", "ready"));
    const usage = screen.getByTestId("plan-usage");
    const meters = within(usage).getAllByRole("group").map((group) => group.getAttribute("aria-label"));
    expect(meters).toEqual(["Armazenamento RAG", "Traces do mês", "Armazenamento de mídias"]);
    expect(within(usage).getByRole("group", { name: "Traces do mês" })).toHaveTextContent(/Usado0/);
    expect(within(usage).getByRole("group", { name: "Armazenamento RAG" })).toHaveTextContent(/Usado0 MB/);
    // Top-ups closed: no dead purchase button, no "coming soon" badge.
    expect(within(usage).queryByRole("button")).not.toBeInTheDocument();
    expect(within(usage).queryByText(/Em breve/)).not.toBeInTheDocument();
  });

  it("shows a cancelled subscription's renewal as not applicable, distinct from absent and unavailable", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () =>
        response({ ...activeSummary, subscription: activeSubscription({ status: "cancelled" }) }),
    });
    renderBilling();

    const overview = await screen.findByTestId("subscription-overview");
    await waitFor(() => expect(within(overview).getByRole("group", { name: "Mensalidade" })).toHaveTextContent(brl(222_200)));
    const renewal = within(overview).getByRole("group", { name: "Próxima renovação" });
    expect(renewal).toHaveTextContent("Não se aplica: assinatura cancelada");
    expect(renewal).toHaveTextContent("Acesso pago até 17/11/2026");
    expect(renewal).not.toHaveTextContent(/Nenhum ciclo pago ainda|Indisponível/);
    expect(screen.getByTestId("subscription-status")).toHaveTextContent("Cancelada");
  });

  it("writes a payment notice into a polite live region mounted empty before it", async () => {
    // The summary answers only when the test says so; the catalog answers now.
    let answer: (value: Response) => void = () => {};
    fetchMock.mockImplementation(async (url: string) => {
      if (url === "/v1/billing/summary") return new Promise<Response>((resolve) => (answer = resolve));
      if (url === CATALOG) return response(catalog);
      throw new Error(`unexpected request ${url}`);
    });
    renderBilling();

    await screen.findByTestId("subscription-overview");
    const region = document.querySelector<HTMLElement>('[data-live-region="billing-status"]');
    expect(region).not.toBeNull();
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(region).toBeEmptyDOMElement();
    expect(screen.queryByTestId("billing-context-alert")).not.toBeInTheDocument();

    answer(response({ ...activeSummary, subscription: activeSubscription({ payment_state: "overdue" }) }));
    const notice = await screen.findByTestId("billing-context-alert");
    // The same node received the text: it was not inserted already filled.
    expect(notice).toBe(region);
    expect(notice).toHaveAttribute("role", "status");
    expect(notice).toHaveTextContent(/Pagamento em atraso: o período pago terminou/);
    // The plan-status line is a static label, not a second live region.
    expect(screen.getByTestId("subscription-status")).not.toHaveAttribute("role");
  });

  it("keeps an error in its own alert, outside the polite region", async () => {
    routeFetch(fetchMock, { "GET /v1/billing/summary": () => response({}, 500) });
    renderBilling();

    const alert = await screen.findByTestId("billing-context-alert");
    expect(alert).toHaveAttribute("role", "alert");
    const region = document.querySelector('[data-live-region="billing-status"]');
    expect(region).toBeEmptyDOMElement();
    expect(region).not.toHaveAttribute("role");
  });

  it("moves focus to the page title when the summary retry button goes away", async () => {
    let answer: Response = response({}, 503);
    routeFetch(fetchMock, { "GET /v1/billing/summary": () => answer });
    renderBilling();

    const retry = within(await screen.findByTestId("billing-context-alert")).getByRole("button", { name: "Tentar novamente" });
    retry.focus();
    answer = response(summary);
    fireEvent.click(retry);

    const title = screen.getByRole("heading", { level: 1, name: "Pagamento" });
    expect(document.activeElement).toBe(title);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Tentar novamente" })).not.toBeInTheDocument());
    expect(document.activeElement).toBe(title);
    expect(document.activeElement).not.toBe(document.body);
  });

  it("moves focus to the plans heading when the catalog retry button goes away", async () => {
    let answer: Response = response({}, 500);
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      [`GET ${CATALOG}`]: () => answer,
    });
    renderBilling();

    await waitFor(() => expect(screen.getAllByTestId("billing-area")).toHaveLength(2));
    const [, plansArea] = screen.getAllByTestId("billing-area");
    const retry = within(await within(plansArea).findByRole("alert")).getByRole("button", { name: "Tentar novamente" });
    retry.focus();
    answer = response(catalog);
    fireEvent.click(retry);

    const heading = within(plansArea).getByRole("heading", { level: 2, name: "Planos" });
    expect(document.activeElement).toBe(heading);
    expect(await within(plansArea).findByRole("table")).toBeInTheDocument();
    expect(document.activeElement).toBe(heading);
  });

  it("does not render card, annual, installment or subscription PIX controls", async () => {
    routeFetch(fetchMock, { "GET /v1/billing/summary": () => response(summary) });
    renderBilling();

    fireEvent.click(await within(await screen.findByTestId(`plan-row-${MAX}`)).findByRole("button", { name: "Assinar Max" }));
    await screen.findByRole("button", { name: /Continuar para o checkout seguro/i });
    // No input at all (the coupon field was removed); card data is entered
    // only on the hosted checkout.
    expect(document.querySelectorAll("input, select, textarea")).toHaveLength(0);
    const forbidden = /número do cartão|card number|cvv|validade do cartão|parcel|installment|anual|annual|\bpix\b|boleto/i;
    expect(screen.queryAllByText(forbidden)).toHaveLength(0);
    for (const role of ["button", "radio", "option", "tab", "checkbox", "textbox"] as const) {
      expect(screen.queryAllByRole(role, { name: forbidden })).toHaveLength(0);
    }
  });

  it("shows no plan or credit area while the summary loads, and none after it is denied", async () => {
    let answer: (value: Response) => void = () => {};
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => new Promise<Response>((resolve) => (answer = resolve)) as unknown as Response,
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
      [`GET ${CREDIT_LOTS}`]: () => response(creditLots([lot()])),
    });
    renderBilling();

    // The other reads already answered; the summary has not.
    await waitFor(() => expect(calls(fetchMock, "GET", CREDIT_LOTS)).toHaveLength(1));
    await waitFor(() => expect(calls(fetchMock, "GET", CATALOG)).toHaveLength(1));
    expect(areaHeadings()).toEqual(["Assinatura e uso"]);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByTestId("credit-panel")).not.toBeInTheDocument();
    expect(document.body.textContent ?? "").not.toMatch(/R\$|\d\s*créditos|NeuroStack (Plus|Pro|Max)/);

    answer(response({ detail: "Builder access required" }, 403));
    expect(await screen.findByTestId("billing-context-alert")).toHaveTextContent(/não tem acesso/);
    expect(screen.queryAllByTestId("billing-area")).toHaveLength(0);
    expect(screen.queryByTestId("credit-panel")).not.toBeInTheDocument();
    expect(document.body.textContent ?? "").not.toMatch(/R\$|\d\s*créditos|NeuroStack (Plus|Pro|Max)/);
  });

  it.each(["pt", "en"] as const)("renders no out-of-scope item or roadmap marker (%s), every panel open", async (locale) => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(topupSummary),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
      [`GET ${CREDIT_LOTS}`]: () => response(creditLots([lot()])),
    });
    renderBilling(locale);

    const subscribe = await screen.findByRole("button", { name: locale === "en" ? "Subscribe to Plus" : "Assinar Plus" });
    fireEvent.click(subscribe);
    fireEvent.click(await screen.findByRole("button", { name: locale === "en" ? "Top up" : "Recarregar" }));
    await screen.findByRole("group", { name: locale === "en" ? "Package" : "Pacote" });

    const text = document.body.textContent ?? "";
    const out =
      /roadmap|em breve|coming soon|fatura|invoice|\bpdf\b|detalhamento mensal|monthly breakdown|cupom|coupon|promocional|promotional|trocar (cartão|forma)|change (card|payment method)|cancelar assinatura|cancel subscription|anual|annual|parcel|installment|migrar de plano|switch plan|número do cartão|card number|cvv/i;
    expect(text).not.toMatch(out);
    for (const role of ["button", "link", "textbox", "checkbox", "radio", "tab"] as const) {
      expect(screen.queryAllByRole(role, { name: out })).toHaveLength(0);
    }
    // No permanently disabled control: every disabled button has its reason.
    for (const button of screen.getAllByRole("button")) {
      if ((button as HTMLButtonElement).disabled) expect(button).toHaveAccessibleDescription(/.+/);
    }
  });
});
