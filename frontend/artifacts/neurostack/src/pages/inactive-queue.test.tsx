import type { ReactNode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  vi.stubGlobal("fetch", mocks.fetch);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("<InactiveQueuePage />", () => {
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
    expect(mocks.fetch).not.toHaveBeenCalled();

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
  it("guards the direct route from investors without mounting the operational page", async () => {
    Object.assign(mocks.auth, {
      role: "investor",
      isInvestor: true,
      isClinicAdmin: false,
      isBuilder: false,
      isSuperAdmin: false,
      leadSubmitted: true,
    });
    window.history.replaceState({}, "", "/inactive-queue");
    mocks.fetch.mockImplementation(() => response(queueResponse([firstItem])));

    render(<App />);

    expect(await screen.findByRole("heading", { name: "404 Page Not Found" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Fila de Inativos" })).not.toBeInTheDocument();
    expect(mocks.fetch).not.toHaveBeenCalled();
  });
});
