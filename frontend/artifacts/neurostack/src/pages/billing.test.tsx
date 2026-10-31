import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { I18nProvider } from "@/context/i18n";
import Billing from "./billing";
import { navigateToHostedCheckout } from "./billing-navigation";

vi.mock("./billing-navigation", () => ({ navigateToHostedCheckout: vi.fn() }));

const SESSION_KEY = "ns_billing_checkout_session";
const SESSION_ID = "0b6f1f0e-3c1a-4e7b-9d55-2f1f6f3f9a10";
const HOSTED_URL = "https://asaas.com/checkoutSession/show?id=chk_abc123";
// Group 6: the page reads the credit ledger once per visit (hidden on any
// answer but a valid 200); the counts below include that one read.
const CREDIT_SUMMARY = "/v1/credits/summary";

const summary = {
  plan: null,
  subscription_status: "trial",
  billing_cycle: "monthly",
  active_clinics: 2,
  usage: null,
};

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
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const handler = routes[`${method} ${url}`];
    if (!handler) throw new Error(`unexpected request ${method} ${url}`);
    return handler(url, init);
  });
}

function calls(fetchMock: ReturnType<typeof vi.fn>, method: string, url: string) {
  return fetchMock.mock.calls.filter(
    ([callUrl, init]) => callUrl === url && ((init as RequestInit | undefined)?.method ?? "GET") === method,
  );
}

function renderBilling() {
  localStorage.setItem("ns_locale", "pt");
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
    expect(screen.getByRole("button", { name: /Recargas indisponíveis/i })).toBeDisabled();
    expect(screen.getAllByText("Indisponível").length).toBeGreaterThanOrEqual(1);
  });

  it("keeps subscription actions unavailable for a server-confirmed subscription", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () =>
        response({ ...summary, plan: "plus", subscription_status: "active" }),
    });
    renderBilling();

    expect(await screen.findByText("Assinatura Ativa")).toBeInTheDocument();
    expect(screen.queryAllByRole("button", { name: /^Assinar / })).toHaveLength(0);
    for (const button of screen.getAllByRole("button", { name: /Gerenciar Assinatura/i })) {
      expect(button).toBeDisabled();
    }
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

  it("sends a coupon only after the server validated it", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      "GET /v1/billing/coupon/BEMVINDO": () =>
        response({ valid: true, code: "BEMVINDO", discount_percent: 10 }),
      "POST /v1/billing/checkout": () => response(session("active", HOSTED_URL)),
    });
    renderBilling();

    const subscribe = await screen.findByRole("button", { name: /Assinar Plus/ });
    await waitFor(() => expect(subscribe).toBeEnabled());
    fireEvent.click(subscribe);
    await waitFor(() => expect(screen.getByLabelText(/Cupom de desconto/i)).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText(/Cupom de desconto/i), { target: { value: "bemvindo" } });
    fireEvent.click(screen.getByRole("button", { name: "Aplicar" }));
    expect(await screen.findByText(/Cupom aplicado: 10% de desconto/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Continuar para o checkout seguro/i }));

    await waitFor(() => expect(calls(fetchMock, "POST", "/v1/billing/checkout")).toHaveLength(1));
    const [checkoutCall] = calls(fetchMock, "POST", "/v1/billing/checkout");
    expect(JSON.parse(String((checkoutCall[1] as RequestInit).body))).toEqual({
      offer_id: "plan_plus_monthly_v1",
      method: "credit_card",
      coupon_code: "BEMVINDO",
    });
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
    [400, "billing_checkout_coupon_invalid", /Cupom inválido, expirado ou esgotado/i],
    [400, "billing_checkout_coupon_not_eligible", /não pode ser usado nesta assinatura/i],
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
    expect(screen.queryByText("Assinatura Ativa")).not.toBeInTheDocument();
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
      "GET /v1/billing/summary": () =>
        response({ ...summary, plan: "pro", subscription_status: "active" }),
      [`GET /v1/billing/checkout/${SESSION_ID}`]: () => response(session("completed")),
    });
    renderBilling();

    expect(await screen.findByText("Checkout concluído")).toBeInTheDocument();
    expect(screen.getByText("Assinatura Ativa")).toBeInTheDocument();
    // Terminal sessions are shown once and then forgotten.
    expect(sessionStorage.getItem(SESSION_KEY)).toBeNull();
  });

  it("only re-reads the summary on a return without a stored session", async () => {
    window.history.replaceState({}, "", "/billing?checkout=success");
    renderBilling();

    expect(await screen.findByText(/Você voltou do checkout hospedado/i)).toBeInTheDocument();
    await waitFor(() => expect(calls(fetchMock, "GET", "/v1/billing/summary")).toHaveLength(1));
    expect(calls(fetchMock, "GET", CREDIT_SUMMARY)).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("Assinatura Ativa")).not.toBeInTheDocument();
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
    expect(fetchMock).toHaveBeenCalledTimes(2);
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

  it("keeps subscription actions unavailable when the summary cannot be read", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response({ detail: "unavailable" }, 500),
    });
    renderBilling();

    await waitFor(() => expect(calls(fetchMock, "GET", "/v1/billing/summary")).toHaveLength(1));
    const buttons = await screen.findAllByRole("button", { name: /^Assinar / });
    expect(buttons).toHaveLength(3);
    for (const button of buttons) expect(button).toBeDisabled();
  });

  it("sends one checkout POST for a burst of clicks on the pay button", async () => {
    let release: (value: Response) => void = () => {};
    const pending = new Promise<Response>((resolve) => {
      release = resolve;
    });
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (method === "GET" && url === "/v1/billing/summary") return response(summary);
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

  // ── Coupon: only a server-validated code is sent ────────────────────────

  async function openPlusCheckoutWithCoupon(code: string) {
    const subscribe = await screen.findByRole("button", { name: /Assinar Plus/ });
    await waitFor(() => expect(subscribe).toBeEnabled());
    fireEvent.click(subscribe);
    const input = await screen.findByLabelText(/Cupom de desconto/i);
    fireEvent.change(input, { target: { value: code } });
    return input;
  }

  async function checkoutBody() {
    fireEvent.click(screen.getByRole("button", { name: /Continuar para o checkout seguro/i }));
    await waitFor(() => expect(calls(fetchMock, "POST", "/v1/billing/checkout")).toHaveLength(1));
    const [checkoutCall] = calls(fetchMock, "POST", "/v1/billing/checkout");
    return JSON.parse(String((checkoutCall[1] as RequestInit).body));
  }

  it("does not send a typed coupon the server never validated", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      "POST /v1/billing/checkout": () => response(session("active", HOSTED_URL)),
    });
    renderBilling();

    await openPlusCheckoutWithCoupon("bemvindo");

    expect(await checkoutBody()).toEqual({ offer_id: "plan_plus_monthly_v1", method: "credit_card" });
    expect(calls(fetchMock, "GET", "/v1/billing/coupon/BEMVINDO")).toHaveLength(0);
  });

  it("does not send a coupon the server rejected", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      "GET /v1/billing/coupon/NOPE": () => response({ valid: false, message: "Cupom inválido" }),
      "POST /v1/billing/checkout": () => response(session("active", HOSTED_URL)),
    });
    renderBilling();

    await openPlusCheckoutWithCoupon("nope");
    fireEvent.click(screen.getByRole("button", { name: "Aplicar" }));
    expect(await screen.findByText("Cupom inválido")).toBeInTheDocument();

    expect(await checkoutBody()).toEqual({ offer_id: "plan_plus_monthly_v1", method: "credit_card" });
  });

  it("does not send a coupon edited after the server validated it", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () => response(summary),
      "GET /v1/billing/coupon/BEMVINDO": () =>
        response({ valid: true, code: "BEMVINDO", discount_percent: 10 }),
      "POST /v1/billing/checkout": () => response(session("active", HOSTED_URL)),
    });
    renderBilling();

    const input = await openPlusCheckoutWithCoupon("bemvindo");
    fireEvent.click(screen.getByRole("button", { name: "Aplicar" }));
    expect(await screen.findByText(/Cupom aplicado: 10% de desconto/i)).toBeInTheDocument();
    fireEvent.change(input, { target: { value: "bemvindo2" } });
    expect(screen.queryByText(/Cupom aplicado/i)).not.toBeInTheDocument();

    expect(await checkoutBody()).toEqual({ offer_id: "plan_plus_monthly_v1", method: "credit_card" });
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
      expect(fetchMock).toHaveBeenCalledTimes(3);
    });

    it("does not poll the summary after a return without a stored session", async () => {
      window.history.replaceState({}, "", "/billing?checkout=success");
      renderBilling();

      expect(await screen.findByText(/Você voltou do checkout hospedado/i)).toBeInTheDocument();
      await waitFor(() => expect(calls(fetchMock, "GET", "/v1/billing/summary")).toHaveLength(1));
      await vi.advanceTimersByTimeAsync(10 * 60 * 1000);

      expect(calls(fetchMock, "GET", CREDIT_SUMMARY)).toHaveLength(1);
      expect(fetchMock).toHaveBeenCalledTimes(2);
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
    expiries: [
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

  it("hides every credit panel and keeps the main page when the credit API answers 503", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () =>
        response({ ...summary, topup_commercial_enabled: true }),
      [`GET ${CREDIT_SUMMARY}`]: CREDIT_UNAVAILABLE,
    });
    renderBilling();

    expect(await screen.findByText("NeuroStack Plus")).toBeInTheDocument();
    await waitFor(() => expect(calls(fetchMock, "GET", CREDIT_SUMMARY)).toHaveLength(1));
    expect(screen.queryByTestId("credit-panel")).not.toBeInTheDocument();
    expect(screen.queryByText(/Saldo de créditos/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Exportar dados/i })).not.toBeInTheDocument();
    // Top-ups stay unavailable even with the commercial flag on: no balance
    // can be shown, so nothing is sold.
    expect(screen.queryByTestId("topup-offer")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Recargas indisponíveis/i })).toBeDisabled();
    expect(screen.queryAllByText(/\bpix\b/i)).toHaveLength(0);
    expect(document.body.textContent ?? "").not.toMatch(/créditos?\s+\d/i);
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

  it("keeps top-ups unavailable while the server says they are closed", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () =>
        response({ ...summary, topup_commercial_enabled: false }),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
    });
    renderBilling();

    expect(await screen.findByTestId("credit-panel")).toBeInTheDocument();
    expect(screen.queryByTestId("topup-offer")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Recargas indisponíveis/i })).toBeDisabled();
  });

  it("sells a package by card or PIX sending only offer_id and method", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () =>
        response({ ...summary, topup_commercial_enabled: true }),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
      "POST /v1/billing/checkout": () =>
        response({ ...session("active", HOSTED_URL), kind: "topup" }),
    });
    renderBilling();

    const offer = await screen.findByTestId("topup-offer");
    const packages = screen.getByRole("group", { name: "Pacote" });
    expect(
      Array.from(packages.querySelectorAll("button")).map((b) => b.textContent),
    ).toEqual(["500 créditos", "1.000 créditos", "2.000 créditos", "4.000 créditos"]);
    const methods = screen.getByRole("group", { name: "Forma de pagamento" });
    expect(Array.from(methods.querySelectorAll("button")).map((b) => b.textContent)).toEqual([
      "Cartão de crédito",
      "PIX",
    ]);
    const buy = screen.getByRole("button", { name: /Comprar recarga/i });
    expect(buy).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "2.000 créditos" }));
    fireEvent.click(screen.getByRole("button", { name: "PIX" }));
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
    // The purchase anticipated nothing: the balance is still the ledger's.
    expect(metric("Recargas")).toHaveTextContent("496 créditos");
    expect(sessionStorage.getItem(SESSION_KEY)).toBe(SESSION_ID);
  });

  it("keeps the subscription a monthly card subscription, apart from the top-ups", async () => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () =>
        response({ ...summary, topup_commercial_enabled: true }),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
      "POST /v1/billing/checkout": () => response(session("active", HOSTED_URL)),
    });
    renderBilling();

    await screen.findByTestId("topup-offer");
    await startCheckout("Plus");
    await waitFor(() => expect(calls(fetchMock, "POST", "/v1/billing/checkout")).toHaveLength(1));
    const [post] = calls(fetchMock, "POST", "/v1/billing/checkout");
    expect(JSON.parse(String((post[1] as RequestInit).body))).toEqual({
      offer_id: "plan_plus_monthly_v1",
      method: "credit_card",
    });
    // PIX is offered only inside the top-up card; the subscription cards and
    // checkout offer no PIX, Pix Automático, annual cycle or installments.
    const topup = screen.getByTestId("topup-offer");
    for (const element of screen.queryAllByText(/\bpix\b/i)) {
      expect(topup.contains(element)).toBe(true);
    }
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/pix autom|anual|annual|parcel|installment/i);
  });

  it.each([
    [409, "billing_checkout_not_eligible"],
    [503, "billing_checkout_unavailable"],
  ])("maps a %s top-up refusal without creating anything", async (status, detail) => {
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () =>
        response({ ...summary, topup_commercial_enabled: true }),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
      "POST /v1/billing/checkout": () => response({ detail }, status),
    });
    renderBilling();

    await screen.findByTestId("topup-offer");
    fireEvent.click(screen.getByRole("button", { name: "500 créditos" }));
    fireEvent.click(screen.getByRole("button", { name: "Cartão de crédito" }));
    fireEvent.click(screen.getByRole("button", { name: /Comprar recarga/i }));

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
      "GET /v1/billing/summary": () =>
        response({ ...summary, topup_commercial_enabled: true }),
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
    // Buying is blocked while that checkout is live.
    expect(screen.getByRole("button", { name: /Comprar recarga/i })).toBeDisabled();
    expect(navigateToHostedCheckout).not.toHaveBeenCalled();

    // The server applied the grant: only a re-read shows it.
    ledger = creditSummary({ balance: { topup_available_mc: 500_000 } });
    fireEvent.click(screen.getByRole("button", { name: /Atualizar estado/i }));
    await waitFor(() => expect(calls(fetchMock, "GET", CREDIT_SUMMARY)).toHaveLength(2));
    await waitFor(() => expect(metric("Recargas")).toHaveTextContent("500 créditos"));
    expect(calls(fetchMock, "GET", `/v1/billing/checkout/${SESSION_ID}`)).toHaveLength(2);
  });

  it("enables the plan usage purchase button only when top-ups are offered", async () => {
    const usage = {
      plan: "plus",
      as_of: AS_OF,
      trace_period: { start: "2026-10-01T00:00:00Z", end: "2026-11-01T00:00:00Z", timezone: "UTC" },
      traces: { used: 0, included: 0, extra_credits: 0, available: 0, overage: 0, percent_used: 0 },
      storage: {
        rag: { used_bytes: 0, used_mb: 0, limit_bytes: 0, limit_mb: 0, available_bytes: 0, available_mb: 0, overage_bytes: 0, overage_mb: 0, percent_used: 0 },
        media: { used_bytes: 0, used_mb: 0, limit_bytes: 0, limit_mb: 0, available_bytes: 0, available_mb: 0, overage_bytes: 0, overage_mb: 0, percent_used: 0 },
      },
      credits: { purchase_enabled: false, coming_soon: true },
    };
    routeFetch(fetchMock, {
      "GET /v1/billing/summary": () =>
        response({ ...summary, usage, topup_commercial_enabled: true }),
      [`GET ${CREDIT_SUMMARY}`]: () => response(creditSummary()),
    });
    renderBilling();

    await screen.findByTestId("topup-offer");
    const buy = screen.getByRole("button", { name: /Comprar créditos/i });
    expect(buy).toBeEnabled();
    fireEvent.click(buy);
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
  });
});
