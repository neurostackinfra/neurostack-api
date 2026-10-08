import type { ReactNode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import { I18nProvider } from "@/context/i18n";
import InactiveQueuePage, { type InactiveSessionItem } from "./inactive-queue";

const mocks = vi.hoisted(() => ({
  auth: {
    isAuthenticated: true,
    email: "admin@clinic.test",
    clinicId: "clinic-id",
    tenantId: "own-tenant",
    userId: "user-id",
    builderId: null,
    role: "clinic_admin",
    isSuperAdmin: false,
    isInvestor: false,
    isBuilder: false,
    isClinicAdmin: true,
    isReadOnly: false,
    leadSubmitted: true,
    viewingClinicId: "",
    setViewingClinicId: vi.fn(),
    setLeadSubmitted: vi.fn(),
    login: vi.fn(),
    loginWithToken: vi.fn(),
    logout: vi.fn(),
  },
  toast: vi.fn(),
  fetch: vi.fn(),
  configFetch: vi.fn(),
}));

vi.mock("@/context/auth", () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: () => mocks.auth,
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mocks.toast }),
}));

vi.mock("@/components/layout", () => ({
  Layout: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

vi.mock("@/pages/login", () => ({
  default: () => <div>Login</div>,
}));

vi.mock("@/components/ui/toaster", () => ({
  Toaster: () => null,
}));

const firstItem = {
  session_id: "12345678-abcd-4abc-8def-1234567890ab",
  contact_identifier: "+55 11 99999-0000",
  channel: "whatsapp",
  last_intent: "agendar",
  funnel_stage: "qualificacao",
  last_activity_at: "2026-08-30T13:45:00Z",
} satisfies InactiveSessionItem;

const secondItem = {
  session_id: "87654321-abcd-4abc-8def-1234567890ab",
  contact_identifier: "patient@example.com",
  channel: "webchat",
  last_intent: "pedido_preco",
  funnel_stage: "consideracao",
  last_activity_at: "2026-08-29T10:15:00Z",
} satisfies InactiveSessionItem;

const nullIdentityItem = {
  ...firstItem,
  session_id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
  contact_identifier: null,
} satisfies InactiveSessionItem;

function queueResponse(items: InactiveSessionItem[], overrides: Partial<{
  total: number;
  page: number;
  has_next: boolean;
  has_previous: boolean;
}> = {}) {
  return {
    items,
    total: overrides.total ?? items.length,
    page: overrides.page ?? 1,
    page_size: 20,
    has_next: overrides.has_next ?? false,
    has_previous: overrides.has_previous ?? false,
  };
}

function response(data: unknown, ok = true) {
  return Promise.resolve({
    ok,
    status: ok ? 200 : 500,
    json: async () => data,
  } as Response);
}

function deferredResponse() {
  let resolve!: (value: Response) => void;
  const promise = new Promise<Response>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return {
    promise,
    resolve: (data: unknown, ok = true) => resolve({
      ok,
      status: ok ? 200 : 500,
      json: async () => data,
    } as Response),
  };
}

function deferredFailure() {
  let reject!: (reason: Error) => void;
  const promise = new Promise<Response>((_resolve, rejectPromise) => {
    reject = rejectPromise;
  });
  return { promise, reject };
}

function renderPage() {
  return render(
    <I18nProvider>
      <InactiveQueuePage />
    </I18nProvider>,
  );
}

function setClinicAdmin() {
  Object.assign(mocks.auth, {
    role: "clinic_admin",
    isSuperAdmin: false,
    isInvestor: false,
    isBuilder: false,
    isClinicAdmin: true,
    viewingClinicId: "",
  });
}

function setBuilder(viewingClinicId: string) {
  Object.assign(mocks.auth, {
    role: "builder",
    isSuperAdmin: false,
    isInvestor: false,
    isBuilder: true,
    isClinicAdmin: false,
    viewingClinicId,
  });
}

function setSuperAdmin(viewingClinicId: string) {
  Object.assign(mocks.auth, {
    role: "super_admin",
    isSuperAdmin: true,
    isInvestor: false,
    isBuilder: false,
    isClinicAdmin: false,
    viewingClinicId,
  });
}

function setInvestor() {
  Object.assign(mocks.auth, {
    role: "investor",
    isSuperAdmin: false,
    isInvestor: true,
    isBuilder: false,
    isClinicAdmin: false,
    viewingClinicId: "",
  });
}

beforeEach(() => {
  vi.useRealTimers();
  localStorage.clear();
  localStorage.setItem("ns_auth", JSON.stringify({ token: "test-token" }));
  localStorage.setItem("ns_locale", "pt");
  window.history.replaceState({}, "", "/");
  setClinicAdmin();
  mocks.auth.isAuthenticated = true;
  mocks.auth.leadSubmitted = true;
  mocks.toast.mockReset();
  mocks.fetch.mockReset();
  mocks.configFetch.mockReset();
  mocks.configFetch.mockImplementation(() => response({}));
  vi.stubGlobal("fetch", (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    return url === "/v1/config" || url.startsWith("/v1/config?")
      ? mocks.configFetch(input, init)
      : mocks.fetch(input, init);
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("<InactiveQueuePage />", () => {
  it("renders recent ordered investor fixtures and default guidance without a clinic or network access", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-07T15:00:00.000Z"));
    setInvestor();
    renderPage();

    const rows = screen.getAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(rows.map((row) => row.getAttribute("data-testid"))).toEqual([
      "inactive-row-d3e0a001-7a11-4b01-8a01-000000000001",
      "inactive-row-d3e0a002-7a11-4b02-8a02-000000000002",
      "inactive-row-d3e0a003-7a11-4b03-8a03-000000000003",
    ]);
    expect(rows[0]).toHaveTextContent("Contato demonstrativo 01");
    expect(rows[0]).toHaveTextContent("whatsapp");
    expect(rows[0]).toHaveTextContent("Solicitação de Agendamento");
    expect(rows[1]).toHaveTextContent("webchat");
    expect(rows[1]).toHaveTextContent("Pedido de Preço");
    expect(rows[2]).toHaveTextContent("Interesse Tratamento");

    const formatter = new Intl.DateTimeFormat("pt-BR", { dateStyle: "short", timeStyle: "short" });
    expect(rows[0]).toHaveTextContent(formatter.format(new Date("2026-10-07T14:15:00.000Z")));
    expect(rows[1]).toHaveTextContent(formatter.format(new Date("2026-10-07T12:00:00.000Z")));
    expect(rows[2]).toHaveTextContent(formatter.format(new Date("2026-10-06T13:00:00.000Z")));
    expect(screen.getByText("3 conversas inativas")).toBeInTheDocument();
    expect(screen.getByText("Precisando de algo mais, estamos a disposição.")).toBeInTheDocument();
    expect(screen.getByText(/a IA voltará a atender após 1 hora/)).toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(30_000);
      await Promise.resolve();
    });
    expect(mocks.fetch).not.toHaveBeenCalled();
    expect(mocks.configFetch).not.toHaveBeenCalled();
  });

  it("reactivates only the selected investor fixture locally and restores all fixtures on remount", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-07T15:00:00.000Z"));
    setInvestor();
    const view = renderPage();

    const selectedRowId = "inactive-row-d3e0a002-7a11-4b02-8a02-000000000002";
    const untouchedRowId = "inactive-row-d3e0a001-7a11-4b01-8a01-000000000001";
    const selectedRow = screen.getByTestId(selectedRowId);
    fireEvent.click(within(selectedRow).getByRole("switch", { name: "Voltar atendimento para IA" }));

    expect(within(selectedRow).getByText("IA no controle")).toBeInTheDocument();
    expect(screen.getByTestId(untouchedRowId)).toBeInTheDocument();
    expect(within(screen.getByTestId(untouchedRowId)).queryByText("IA no controle")).not.toBeInTheDocument();
    expect(mocks.fetch).not.toHaveBeenCalled();
    expect(mocks.configFetch).not.toHaveBeenCalled();

    await act(async () => {
      vi.advanceTimersByTime(600);
      await Promise.resolve();
    });
    expect(screen.queryByTestId(selectedRowId)).not.toBeInTheDocument();
    expect(screen.getByTestId(untouchedRowId)).toBeInTheDocument();
    expect(screen.getByText("2 conversas inativas")).toBeInTheDocument();

    view.unmount();
    renderPage();
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
    expect(screen.getByTestId(selectedRowId)).toBeInTheDocument();
    expect(mocks.fetch).not.toHaveBeenCalled();
    expect(mocks.configFetch).not.toHaveBeenCalled();
  });

  it("shows the exact master reactivation guidance and copies the registered phrase", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    mocks.fetch.mockImplementation(() => response(queueResponse([])));
    renderPage();

    expect(screen.getByRole("heading", { name: "Frase mestra de reativação" })).toBeInTheDocument();
    expect(await screen.findByText(
      "Quando a frase de gatilho for enviada exatamente como cadastrada, inclusive com a mesma pontuação, a IA voltará a atender após 1 hora. Nesse intervalo, o paciente poderá continuar conversando com a equipe humana para esclarecer dúvidas pendentes.",
    )).toBeInTheDocument();
    expect(screen.getByText("Frase mestra")).toHaveClass("uppercase");
    expect(screen.getByText("Precisando de algo mais, estamos a disposição.")).toBeInTheDocument();

    const copyButton = screen.getByRole("button", { name: "Copiar frase mestra" });
    fireEvent.click(copyButton);

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(
      "Precisando de algo mais, estamos a disposição.",
    ));
    expect(copyButton).toHaveTextContent("Copiado!");
    expect(copyButton).toHaveAccessibleName("Frase mestra copiada");
  });

  it("loads and copies the selected clinic's configured phrase and reactivation delay", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    setBuilder("selected-tenant");
    mocks.configFetch.mockImplementation(() => response({
      trigger_message: { invalid: "legacy value" },
      reactivation_delay_hours: 3.0,
      effective_trigger_message: "Encerramos por aqui; posso ajudar em algo mais?",
      effective_reactivation_delay_hours: 3,
    }));
    mocks.fetch.mockImplementation(() => response(queueResponse([])));
    renderPage();

    expect(await screen.findByText("Encerramos por aqui; posso ajudar em algo mais?")).toBeInTheDocument();
    expect(screen.getByText(
      "Quando a frase de gatilho for enviada exatamente como cadastrada, inclusive com a mesma pontuação, a IA voltará a atender após 3 horas. Nesse intervalo, o paciente poderá continuar conversando com a equipe humana para esclarecer dúvidas pendentes.",
    )).toBeInTheDocument();
    expect(String(mocks.configFetch.mock.calls[0][0])).toBe(
      "/v1/config?tenant_id=selected-tenant",
    );

    fireEvent.click(screen.getByRole("button", { name: "Copiar frase mestra" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(
      "Encerramos por aqui; posso ajudar em algo mais?",
    ));
  });

  it("does not show a stale master phrase after the selected clinic changes", async () => {
    setBuilder("old-tenant");
    const oldConfig = deferredResponse();
    mocks.configFetch
      .mockImplementationOnce(() => oldConfig.promise)
      .mockImplementationOnce(() => response({
        trigger_message: "Frase da clínica nova.",
        reactivation_delay_hours: 2,
      }));
    mocks.fetch.mockImplementation(() => response(queueResponse([])));

    const view = renderPage();
    await waitFor(() => expect(mocks.configFetch).toHaveBeenCalledTimes(1));

    setBuilder("new-tenant");
    view.rerender(<I18nProvider><InactiveQueuePage /></I18nProvider>);
    expect(await screen.findByText("Frase da clínica nova.")).toBeInTheDocument();
    expect(String(mocks.configFetch.mock.calls[1][0])).toBe(
      "/v1/config?tenant_id=new-tenant",
    );

    await act(async () => {
      oldConfig.resolve({
        trigger_message: "Frase obsoleta da clínica anterior.",
        reactivation_delay_hours: 9,
      });
      await oldConfig.promise;
      await Promise.resolve();
    });

    expect(screen.queryByText("Frase obsoleta da clínica anterior.")).not.toBeInTheDocument();
    expect(screen.getByText("Frase da clínica nova.")).toBeInTheDocument();
  });

  it("ignores a pending clipboard result after the selected clinic changes", async () => {
    let resolveCopy!: () => void;
    const pendingCopy = new Promise<void>((resolve) => {
      resolveCopy = resolve;
    });
    const writeText = vi.fn().mockReturnValue(pendingCopy);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    setBuilder("old-tenant");
    mocks.configFetch
      .mockImplementationOnce(() => response({ trigger_message: "Frase para copiar da clínica antiga." }))
      .mockImplementationOnce(() => response({ trigger_message: "Frase atual da clínica nova." }));
    mocks.fetch.mockImplementation(() => response(queueResponse([])));

    const view = renderPage();
    expect(await screen.findByText("Frase para copiar da clínica antiga.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Copiar frase mestra" }));

    setBuilder("new-tenant");
    view.rerender(<I18nProvider><InactiveQueuePage /></I18nProvider>);
    expect(await screen.findByText("Frase atual da clínica nova.")).toBeInTheDocument();

    await act(async () => {
      resolveCopy();
      await pendingCopy;
      await Promise.resolve();
    });

    expect(writeText).toHaveBeenCalledWith("Frase para copiar da clínica antiga.");
    expect(screen.getByRole("button", { name: "Copiar frase mestra" })).toHaveTextContent("Copiar");
    expect(mocks.toast).not.toHaveBeenCalled();
  });

  it("offers a retry when the clinic master phrase cannot be loaded", async () => {
    mocks.configFetch
      .mockImplementationOnce(() => response({}, false))
      .mockImplementationOnce(() => response({ trigger_message: "Frase recuperada." }));
    mocks.fetch.mockImplementation(() => response(queueResponse([])));
    renderPage();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Não foi possível carregar a frase mestra desta clínica.");
    fireEvent.click(screen.getByRole("button", { name: "Tentar novamente" }));

    expect(await screen.findByText("Frase recuperada.")).toBeInTheDocument();
    expect(mocks.configFetch).toHaveBeenCalledTimes(2);
  });

  it("keeps the phrase selectable and reports a clipboard failure", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("permission denied"));
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    mocks.fetch.mockImplementation(() => response(queueResponse([])));
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Copiar frase mestra" }));

    await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith({
      title: "Não foi possível copiar a frase mestra",
      description: "Selecione e copie a frase manualmente.",
      variant: "destructive",
    }));
    expect(screen.getByText("Precisando de algo mais, estamos a disposição.")).toBeInTheDocument();
  });

  it("renders the em-dash fallback for a null last_intent without falling into the Outras bucket", async () => {
    const nullIntentItem = {
      ...firstItem,
      session_id: "cccccccc-dddd-4eee-8fff-999999999999",
      last_intent: null,
    } satisfies InactiveSessionItem;
    mocks.fetch.mockImplementation(() => response(queueResponse([nullIntentItem])));
    renderPage();

    const row = await screen.findByTestId(`inactive-row-${nullIntentItem.session_id}`);
    expect(row).toHaveTextContent("—");
    expect(row).not.toHaveTextContent("Outras");
    expect(row).not.toHaveTextContent("Solicitação de Agendamento");
  });

  it("accepts a null contact identity and renders a safe fallback without hiding valid fields", async () => {
    mocks.fetch.mockImplementation(() => response(queueResponse([nullIdentityItem])));
    renderPage();

    const row = await screen.findByTestId(`inactive-row-${nullIdentityItem.session_id}`);
    expect(row).toHaveTextContent("—");
    expect(row).toHaveTextContent(nullIdentityItem.channel);
    expect(row).toHaveTextContent("Solicitação de Agendamento");
    expect(row.textContent).not.toContain(nullIdentityItem.last_intent!);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("renders the exact inactive DTO and omits tenant_id for a clinic-admin request", async () => {
    mocks.fetch.mockImplementation(() => response(queueResponse([firstItem])));
    renderPage();

    expect(await screen.findByText(firstItem.contact_identifier)).toBeInTheDocument();
    expect(screen.getByText("#12345678")).toBeInTheDocument();
    expect(screen.getByText(firstItem.channel)).toBeInTheDocument();
    expect(screen.getByText("Solicitação de Agendamento")).toBeInTheDocument();
    expect(screen.queryByText(firstItem.last_intent!)).not.toBeInTheDocument();
    expect(screen.queryByText("Etapa do funil")).not.toBeInTheDocument();
    expect(screen.queryByText(firstItem.funnel_stage!)).not.toBeInTheDocument();
    expect(screen.getAllByText("Última atividade").length).toBeGreaterThan(0);
    expect(screen.queryByText(/pausa/i)).not.toBeInTheDocument();

    const requestedUrl = String(mocks.fetch.mock.calls[0][0]);
    expect(requestedUrl).toBe("/v1/sessions/inactive?page=1&page_size=20");
    expect(mocks.fetch.mock.calls[0][1]).toMatchObject({
      headers: { Authorization: "Bearer test-token" },
    });
  });

  it("uses the selected builder tenant, page size 20, and pagination response flags", async () => {
    setBuilder("selected-tenant");
    const secondPage = deferredResponse();
    mocks.fetch
      .mockImplementationOnce(() => response(queueResponse([firstItem], { total: 21, has_next: true })))
      .mockImplementationOnce(() => secondPage.promise);
    renderPage();

    expect(await screen.findByText(firstItem.contact_identifier)).toBeInTheDocument();
    expect(String(mocks.fetch.mock.calls[0][0])).toBe(
      "/v1/sessions/inactive?page=1&page_size=20&tenant_id=selected-tenant",
    );

    fireEvent.click(screen.getByRole("button", { name: "Próxima" }));
    expect(screen.queryByText(firstItem.contact_identifier)).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Carregando conversas inativas");
    await act(async () => {
      secondPage.resolve(queueResponse([secondItem], { total: 21, page: 2, has_previous: true }));
      await secondPage.promise;
      await Promise.resolve();
    });
    expect(await screen.findByText(secondItem.contact_identifier)).toBeInTheDocument();
    expect(String(mocks.fetch.mock.calls[1][0])).toBe(
      "/v1/sessions/inactive?page=2&page_size=20&tenant_id=selected-tenant",
    );
    expect(screen.getByText("Página 2 de 2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Anterior" })).toBeEnabled();
  });

  it("polls every 30 seconds with the current query", async () => {
    vi.useFakeTimers();
    mocks.fetch.mockImplementation(() => response(queueResponse([firstItem])));
    renderPage();

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(mocks.fetch).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(30_000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(mocks.fetch).toHaveBeenCalledTimes(2);
    expect(String(mocks.fetch.mock.calls[1][0])).toBe("/v1/sessions/inactive?page=1&page_size=20");
  });

  it("ignores a stale clinic response after the selected tenant changes", async () => {
    setBuilder("old-tenant");
    const oldRequest = deferredResponse();
    const newRequest = deferredResponse();
    mocks.fetch
      .mockImplementationOnce(() => oldRequest.promise)
      .mockImplementationOnce(() => newRequest.promise);

    const view = renderPage();
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(1));

    setBuilder("new-tenant");
    view.rerender(
      <I18nProvider>
        <InactiveQueuePage />
      </I18nProvider>,
    );
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(2));
    expect(String(mocks.fetch.mock.calls[1][0])).toContain("tenant_id=new-tenant");

    await act(async () => {
      newRequest.resolve(queueResponse([secondItem]));
      await newRequest.promise;
      await Promise.resolve();
    });
    expect(await screen.findByText(secondItem.contact_identifier)).toBeInTheDocument();

    await act(async () => {
      oldRequest.resolve(queueResponse([firstItem]));
      await oldRequest.promise;
      await Promise.resolve();
    });
    expect(screen.getByText(secondItem.contact_identifier)).toBeInTheDocument();
    expect(screen.queryByText(firstItem.contact_identifier)).not.toBeInTheDocument();
  });

  it("never renders tenant A rows in the first committed tenant B frame", async () => {
    setBuilder("tenant-a");
    const tenantBRequest = deferredResponse();
    mocks.fetch
      .mockImplementationOnce(() => response(queueResponse([firstItem])))
      .mockImplementationOnce(() => tenantBRequest.promise);
    const view = renderPage();
    expect(await screen.findByText(firstItem.contact_identifier)).toBeInTheDocument();

    setBuilder("tenant-b");
    view.rerender(<I18nProvider><InactiveQueuePage /></I18nProvider>);
    expect(screen.queryByText(firstItem.contact_identifier)).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Carregando conversas inativas");

    await act(async () => {
      tenantBRequest.resolve(queueResponse([secondItem]));
      await tenantBRequest.promise;
      await Promise.resolve();
    });
    expect(screen.getByText(secondItem.contact_identifier)).toBeInTheDocument();
  });

  it("keeps the accessible switch off until 2xx, then shows success and removes only that row", async () => {
    vi.useFakeTimers();
    const reactivation = deferredResponse();
    let getCount = 0;
    mocks.fetch.mockImplementation((input: string | URL | Request, init?: RequestInit) => {
      if (init?.method === "POST") return reactivation.promise;
      getCount += 1;
      return response(queueResponse(getCount === 1 ? [firstItem, secondItem] : [secondItem]));
    });
    renderPage();

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    const control = screen.getAllByRole("switch", { name: "Voltar atendimento para IA" })[0];
    expect(control).toHaveAttribute("data-state", "unchecked");
    expect(control).not.toBeDisabled();

    fireEvent.click(control);
    expect(control).toBeDisabled();
    expect(control).toHaveAttribute("data-state", "unchecked");
    expect(screen.queryByText("IA no controle")).not.toBeInTheDocument();

    await act(async () => {
      reactivation.resolve({ session_id: firstItem.session_id, status: "reactivated" });
      await reactivation.promise;
      await Promise.resolve();
    });
    expect(screen.getAllByRole("switch", { name: "Voltar atendimento para IA" })[0]).toHaveAttribute("data-state", "checked");
    expect(screen.getAllByText("Voltar atendimento para IA")).toHaveLength(2);
    expect(screen.getByText("IA no controle")).toHaveClass("text-emerald-600");
    expect(mocks.toast).toHaveBeenCalledWith({
      title: "Atendimento retornado para IA",
      description: "O bot reassumiu o controle desta conversa.",
    });
    expect(screen.getByTestId(`inactive-row-${firstItem.session_id}`)).toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(600);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.queryByTestId(`inactive-row-${firstItem.session_id}`)).not.toBeInTheDocument();
    expect(screen.getByTestId(`inactive-row-${secondItem.session_id}`)).toBeInTheDocument();
  });

  it("clamps an emptied page 2 and refetches page 1 from the authoritative total", async () => {
    vi.useFakeTimers();
    setBuilder("selected-tenant");
    mocks.fetch
      .mockImplementationOnce(() => response(queueResponse([firstItem], { total: 21, has_next: true })))
      .mockImplementationOnce(() => response(queueResponse([secondItem], { total: 21, page: 2, has_previous: true })))
      .mockImplementationOnce(() => response({ session_id: secondItem.session_id, status: "reactivated" }))
      .mockImplementationOnce(() => response(queueResponse([], { total: 20, page: 2, has_previous: true })))
      .mockImplementationOnce(() => response(queueResponse([], { total: 20, page: 2, has_previous: true })))
      .mockImplementationOnce(() => response(queueResponse([firstItem], { total: 20 })));
    renderPage();

    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    fireEvent.click(screen.getByRole("button", { name: "Próxima" }));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    await act(async () => { vi.advanceTimersByTime(29_450); });
    fireEvent.click(screen.getByRole("switch", { name: "Voltar atendimento para IA" }));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    await act(async () => {
      vi.advanceTimersByTime(550);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText("IA no controle")).toBeInTheDocument();
    expect(screen.getByText("Página 2 de 2")).toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(50);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(String(mocks.fetch.mock.calls[4][0])).toContain("page=2&page_size=20");
    expect(String(mocks.fetch.mock.calls[5][0])).toContain("page=1&page_size=20");
    expect(String(mocks.fetch.mock.calls[2][0])).toBe(
      `/v1/sessions/${secondItem.session_id}/reactivate?tenant_id=selected-tenant`,
    );
    expect(mocks.fetch.mock.calls[2][1]).toMatchObject({
      method: "POST",
      headers: { Authorization: "Bearer test-token" },
    });
    expect(screen.getByText("Página 1 de 1")).toBeInTheDocument();
    expect(screen.getByText(firstItem.contact_identifier)).toBeInTheDocument();
  });

  it("uses the authoritative 21 to 20 boundary instead of locally decrementing pagination", async () => {
    vi.useFakeTimers();
    mocks.fetch
      .mockImplementationOnce(() => response(queueResponse([firstItem, secondItem], { total: 21, has_next: true })))
      .mockImplementationOnce(() => response({ session_id: firstItem.session_id, status: "reactivated" }))
      .mockImplementationOnce(() => response(queueResponse([secondItem], { total: 20, has_next: false })));
    renderPage();

    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    fireEvent.click(screen.getAllByRole("switch", { name: "Voltar atendimento para IA" })[0]);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(screen.getByText("21 conversas inativas")).toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(600);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByText("20 conversas inativas")).toBeInTheDocument();
    expect(screen.getByText("Página 1 de 1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Próxima" })).toBeDisabled();
  });

  it("ignores a tenant A POST completion and timer after switching to tenant B", async () => {
    vi.useFakeTimers();
    setBuilder("tenant-a");
    const tenantAPost = deferredResponse();
    mocks.fetch
      .mockImplementationOnce(() => response(queueResponse([firstItem])))
      .mockImplementationOnce(() => tenantAPost.promise)
      .mockImplementationOnce(() => response(queueResponse([secondItem])));
    const view = renderPage();

    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    fireEvent.click(screen.getByRole("switch", { name: "Voltar atendimento para IA" }));
    setBuilder("tenant-b");
    view.rerender(<I18nProvider><InactiveQueuePage /></I18nProvider>);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(screen.getByText(secondItem.contact_identifier)).toBeInTheDocument();

    await act(async () => {
      tenantAPost.resolve({ session_id: firstItem.session_id, status: "reactivated" });
      await tenantAPost.promise;
      vi.advanceTimersByTime(600);
      await Promise.resolve();
    });

    expect(screen.getByText(secondItem.contact_identifier)).toBeInTheDocument();
    expect(screen.queryByText("IA no controle")).not.toBeInTheDocument();
    expect(mocks.fetch).toHaveBeenCalledTimes(3);
  });

  it("keeps the success tombstone through a poll during the 600ms transition", async () => {
    vi.useFakeTimers();
    mocks.fetch
      .mockImplementationOnce(() => response(queueResponse([firstItem], { total: 1 })))
      .mockImplementationOnce(() => response({ session_id: firstItem.session_id, status: "already_active" }))
      .mockImplementationOnce(() => response(queueResponse([], { total: 0 })))
      .mockImplementationOnce(() => response(queueResponse([], { total: 0 })));
    renderPage();

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      vi.advanceTimersByTime(29_450);
    });
    fireEvent.click(screen.getByRole("switch", { name: "Voltar atendimento para IA" }));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    await act(async () => {
      vi.advanceTimersByTime(550);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByTestId(`inactive-row-${firstItem.session_id}`)).toBeInTheDocument();
    expect(screen.getByText("IA no controle")).toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(50);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.queryByTestId(`inactive-row-${firstItem.session_id}`)).not.toBeInTheDocument();
    expect(mocks.fetch).toHaveBeenCalledTimes(4);
  });

  it("preserves the mutation-start snapshot when a poll empties the queue before POST 2xx", async () => {
    vi.useFakeTimers();
    const reactivation = deferredResponse();
    let getCount = 0;
    mocks.fetch.mockImplementation((input: string | URL | Request, init?: RequestInit) => {
      if (init?.method === "POST") return reactivation.promise;
      getCount += 1;
      return response(queueResponse(getCount === 1 ? [firstItem] : []));
    });
    renderPage();

    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    const control = screen.getByRole("switch", { name: "Voltar atendimento para IA" });
    fireEvent.click(control);
    expect(control).toBeDisabled();

    await act(async () => {
      vi.advanceTimersByTime(30_000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByTestId(`inactive-row-${firstItem.session_id}`)).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Voltar atendimento para IA" })).toBeDisabled();

    await act(async () => {
      reactivation.resolve({ session_id: firstItem.session_id, status: "reactivated" });
      await reactivation.promise;
      await Promise.resolve();
    });
    expect(screen.getByText("Voltar atendimento para IA")).toBeInTheDocument();
    expect(screen.getByText("IA no controle")).toHaveClass("text-emerald-600");

    await act(async () => {
      vi.advanceTimersByTime(600);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.queryByTestId(`inactive-row-${firstItem.session_id}`)).not.toBeInTheDocument();
    expect(mocks.fetch).toHaveBeenCalledTimes(4);
  });

  it("invalidates an outstanding pre-POST GET so it cannot resurrect the successful row", async () => {
    vi.useFakeTimers();
    const stalePoll = deferredResponse();
    mocks.fetch
      .mockImplementationOnce(() => response(queueResponse([firstItem])))
      .mockImplementationOnce(() => stalePoll.promise)
      .mockImplementationOnce(() => response({ session_id: firstItem.session_id, status: "reactivated" }))
      .mockImplementationOnce(() => response(queueResponse([], { total: 0 })));
    renderPage();

    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    await act(async () => { vi.advanceTimersByTime(30_000); });
    fireEvent.click(screen.getByRole("switch", { name: "Voltar atendimento para IA" }));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    await act(async () => {
      stalePoll.resolve(queueResponse([firstItem]));
      await stalePoll.promise;
      await Promise.resolve();
    });
    expect(screen.getByText("IA no controle")).toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(600);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.queryByTestId(`inactive-row-${firstItem.session_id}`)).not.toBeInTheDocument();
  });

  it("uses the selected super-admin tenant and resets page/no-clinic contexts", async () => {
    setBuilder("");
    const view = renderPage();
    expect(screen.getByText("Selecione uma clínica para ver a fila de inativos.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Frase mestra de reativação" })).not.toBeInTheDocument();
    expect(mocks.fetch).not.toHaveBeenCalled();
    expect(mocks.configFetch).not.toHaveBeenCalled();

    setSuperAdmin("super-tenant");
    mocks.fetch
      .mockImplementationOnce(() => response(queueResponse([firstItem], { total: 21, has_next: true })))
      .mockImplementationOnce(() => response(queueResponse([secondItem], { total: 21, page: 2, has_previous: true })))
      .mockImplementationOnce(() => response(queueResponse([firstItem])));
    view.rerender(<I18nProvider><InactiveQueuePage /></I18nProvider>);
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(firstItem.contact_identifier)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Próxima" }));
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(2));

    setSuperAdmin("other-super-tenant");
    view.rerender(<I18nProvider><InactiveQueuePage /></I18nProvider>);
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(3));
    expect(String(mocks.fetch.mock.calls[2][0])).toBe(
      "/v1/sessions/inactive?page=1&page_size=20&tenant_id=other-super-tenant",
    );
  });

  it("provides English action copy, keyboard focus, and responsive no-overflow structure", async () => {
    localStorage.setItem("ns_locale", "en");
    mocks.fetch.mockImplementation((input: string | URL | Request, init?: RequestInit) => (
      init?.method === "POST"
        ? response({ session_id: firstItem.session_id, status: "already_active" })
        : response(queueResponse([firstItem]))
    ));
    renderPage();

    const control = await screen.findByRole("switch", { name: "Return service to AI" });
    control.focus();
    expect(control).toHaveFocus();
    expect(control).toHaveClass("focus-visible:ring-2");
    fireEvent.keyDown(control, { key: " ", code: "Space" });
    expect(control).toBeDisabled();
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(2));
    expect(String(mocks.fetch.mock.calls[1][0])).toBe(
      `/v1/sessions/${firstItem.session_id}/reactivate`,
    );
    expect(mocks.fetch.mock.calls[1][1]).toMatchObject({
      method: "POST",
      headers: { Authorization: "Bearer test-token" },
    });
    expect(await screen.findByText("AI in control")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Inactive Queue" })).toBeInTheDocument();
    expect(screen.getByTestId(`inactive-row-${firstItem.session_id}`)).toHaveClass("grid-cols-1", "md:grid-cols-[1.2fr_.8fr_.65fr_1fr_1fr_1.25fr]");
    expect(screen.getByRole("list").parentElement).toHaveClass("overflow-hidden");
    expect(screen.getByRole("list").parentElement).not.toHaveClass("overflow-x-auto");
  });

  it("supports Enter keyboard activation with the same pending/success transition", async () => {
    const reactivation = deferredResponse();
    mocks.fetch.mockImplementation((input: string | URL | Request, init?: RequestInit) => (
      init?.method === "POST" ? reactivation.promise : response(queueResponse([firstItem]))
    ));
    renderPage();

    const control = await screen.findByRole("switch", { name: "Voltar atendimento para IA" });
    control.focus();
    fireEvent.keyDown(control, { key: "Enter", code: "Enter" });
    expect(control).toBeDisabled();
    expect(mocks.fetch).toHaveBeenCalledTimes(2);

    await act(async () => {
      reactivation.resolve({ session_id: firstItem.session_id, status: "already_active" });
      await reactivation.promise;
      await Promise.resolve();
    });
    expect(screen.getByText("IA no controle")).toBeInTheDocument();
  });

  it.each(["resolve", "reject"] as const)(
    "makes a deferred POST %s inert after unmount",
    async (outcome) => {
      vi.useFakeTimers();
      const reactivation = outcome === "resolve" ? deferredResponse() : deferredFailure();
      mocks.fetch.mockImplementation((input: string | URL | Request, init?: RequestInit) => (
        init?.method === "POST" ? reactivation.promise : response(queueResponse([firstItem]))
      ));
      const view = renderPage();
      await act(async () => { await Promise.resolve(); await Promise.resolve(); });
      fireEvent.click(screen.getByRole("switch", { name: "Voltar atendimento para IA" }));
      expect(mocks.fetch).toHaveBeenCalledTimes(2);
      view.unmount();

      await act(async () => {
        if ("resolve" in reactivation) {
          reactivation.resolve({ session_id: firstItem.session_id, status: "reactivated" });
        } else {
          reactivation.reject(new Error("late failure"));
        }
        await reactivation.promise.catch(() => undefined);
        vi.runAllTimers();
        await Promise.resolve();
      });

      expect(mocks.toast).not.toHaveBeenCalled();
      expect(mocks.fetch).toHaveBeenCalledTimes(2);
      expect(vi.getTimerCount()).toBe(0);
    },
  );

  it.each([
    { session_id: firstItem.session_id, status: "unexpected" },
    { session_id: secondItem.session_id, status: "reactivated" },
  ])("keeps the row and reports invalid POST 2xx payloads", async (payload) => {
    mocks.fetch.mockImplementation((input: string | URL | Request, init?: RequestInit) => (
      init?.method === "POST" ? response(payload) : response(queueResponse([firstItem]))
    ));
    renderPage();

    const control = await screen.findByRole("switch", { name: "Voltar atendimento para IA" });
    fireEvent.click(control);
    await waitFor(() => expect(mocks.toast).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId(`inactive-row-${firstItem.session_id}`)).toBeInTheDocument();
    expect(screen.queryByText("IA no controle")).not.toBeInTheDocument();
    expect(control).not.toBeDisabled();
  });

  it("rejects malformed GET 2xx payloads into the localized safe error state", async () => {
    mocks.fetch.mockImplementation(() => response({
      items: [{ ...firstItem, channel: 42 }],
      total: -1,
      page: 0,
      page_size: 0,
      has_next: "no",
      has_previous: false,
    }));
    renderPage();

    expect(await screen.findByRole("alert")).toHaveTextContent("Não foi possível carregar a fila de inativos.");
    expect(screen.queryByTestId(`inactive-row-${firstItem.session_id}`)).not.toBeInTheDocument();
  });

  it("preserves the off row and shows a localized toast when reactivation fails", async () => {
    mocks.fetch.mockImplementation((input: string | URL | Request, init?: RequestInit) => {
      if (init?.method === "POST") return response({ detail: "failure" }, false);
      return response(queueResponse([firstItem]));
    });
    renderPage();

    const control = await screen.findByRole("switch", { name: "Voltar atendimento para IA" });
    fireEvent.click(control);

    await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith({
      title: "Não foi possível devolver o atendimento para a IA",
      description: "A conversa continua inativa. Tente novamente.",
      variant: "destructive",
    }));
    expect(screen.getByTestId(`inactive-row-${firstItem.session_id}`)).toBeInTheDocument();
    expect(control).toHaveAttribute("data-state", "unchecked");
    expect(control).not.toBeDisabled();
  });

  it("shows localized loading, empty, and non-2xx error states", async () => {
    const loadingRequest = deferredResponse();
    mocks.fetch.mockImplementationOnce(() => loadingRequest.promise);
    const view = renderPage();
    expect(screen.getByRole("status")).toHaveTextContent("Carregando conversas inativas");

    await act(async () => {
      loadingRequest.resolve(queueResponse([]));
      await loadingRequest.promise;
      await Promise.resolve();
    });
    expect(screen.getByText("Nenhuma conversa inativa no momento.")).toBeInTheDocument();

    view.unmount();
    mocks.fetch.mockReset();
    mocks.fetch.mockImplementation(() => response({}, false));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("Não foi possível carregar a fila de inativos.");
    expect(screen.getByRole("button", { name: "Tentar novamente" })).toBeInTheDocument();
  });
});

describe("inactive queue route access", () => {
  it("mounts the local demo for investors after the lead gate without operational network access", async () => {
    Object.assign(mocks.auth, {
      role: "investor",
      isInvestor: true,
      isClinicAdmin: false,
      isBuilder: false,
      isSuperAdmin: false,
      leadSubmitted: true,
    });
    window.history.replaceState({}, "", "/inactive-queue");

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Fila de Inativos" })).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
    expect(mocks.fetch).not.toHaveBeenCalled();
    expect(mocks.configFetch).not.toHaveBeenCalled();
  });

  it("keeps the demo lead gate ahead of the investor inactive queue route", async () => {
    Object.assign(mocks.auth, {
      role: "investor",
      isInvestor: true,
      isClinicAdmin: false,
      isBuilder: false,
      isSuperAdmin: false,
      leadSubmitted: false,
    });
    window.history.replaceState({}, "", "/inactive-queue");

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Welcome to the Exclusive Demo" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Fila de Inativos" })).not.toBeInTheDocument();
    expect(mocks.fetch).not.toHaveBeenCalled();
    expect(mocks.configFetch).not.toHaveBeenCalled();
  });
});
