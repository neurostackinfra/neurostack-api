import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { I18nProvider } from "@/context/i18n";
import Billing from "./billing";
import { navigateToHostedCheckout } from "./billing-navigation";

vi.mock("./billing-navigation", () => ({ navigateToHostedCheckout: vi.fn() }));

const SESSION_KEY = "ns_billing_checkout_session";
const SESSION_ID = "0b6f1f0e-3c1a-4e7b-9d55-2f1f6f3f9a10";
const HOSTED_URL = "https://asaas.com/checkoutSession/show?id=chk_abc123";

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
    expect(fetchMock).toHaveBeenCalledTimes(1);
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
    expect(fetchMock).toHaveBeenCalledTimes(1);
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
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });

    it("does not poll the summary after a return without a stored session", async () => {
      window.history.replaceState({}, "", "/billing?checkout=success");
      renderBilling();

      expect(await screen.findByText(/Você voltou do checkout hospedado/i)).toBeInTheDocument();
      await waitFor(() => expect(calls(fetchMock, "GET", "/v1/billing/summary")).toHaveLength(1));
      await vi.advanceTimersByTimeAsync(10 * 60 * 1000);

      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
  });
});
