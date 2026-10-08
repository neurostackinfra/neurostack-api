import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

import { I18nProvider } from "@/context/i18n";
import TracesList from "./traces";

const mocks = vi.hoisted(() => ({
  fetch: vi.fn(),
  toast: vi.fn(),
  auth: {
    isReadOnly: false,
    isBuilder: false,
    isSuperAdmin: false,
    isInvestor: false,
    viewingClinicId: "clinic-1" as string | null,
  },
}));

vi.mock("@/context/auth", () => ({
  useAuth: () => mocks.auth,
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mocks.toast }),
}));

interface TraceFixture {
  id: string;
  timestamp: string;
  user_message: string;
  srm_decision: {
    intent: string;
    intent_category: string;
    funnel_stage: string;
    action: string;
    confidence: number;
    safety_concern: boolean;
    handoff_required: boolean;
  };
  processing_time_ms: number;
  channel: string;
}

const longMessage =
  "Esta é a mensagem completa associada ao trace, com contexto importante e " +
  "TOKENEXTREMAMENTELONGOSEMESPACOSQUEPRECISAQUEBRARSEMCORTAROCONTEUDO";

function traceFixture(
  id: string,
  intent: string,
  userMessage = longMessage,
  flags: { safety_concern?: boolean; handoff_required?: boolean } = {},
): TraceFixture {
  return {
    id,
    timestamp: "2026-08-30T12:00:00Z",
    user_message: userMessage,
    srm_decision: {
      intent,
      intent_category: "post_care",
      funnel_stage: "pos_atendimento",
      action: "respond",
      confidence: 0.98,
      safety_concern: flags.safety_concern ?? false,
      handoff_required: flags.handoff_required ?? false,
    },
    processing_time_ms: 123,
    channel: "whatsapp",
  };
}

function tracesResponse(items: TraceFixture[], total = items.length) {
  return {
    items,
    total,
    limit: 50,
    offset: 0,
  };
}

function jsonResponse(data: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => data,
  } as Response;
}

function errorResponse(status: number, detail?: unknown): Response {
  return {
    ok: false,
    status,
    json: async () => ({ detail }),
  } as Response;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function mountTraces(locale: "pt" | "en" = "pt") {
  localStorage.setItem("ns_locale", locale);
  const location = memoryLocation({ path: "/traces", record: true });
  const tree = () => (
    <Router hook={location.hook}>
      <I18nProvider>
        <TracesList />
      </I18nProvider>
    </Router>
  );
  const view = render(tree());
  return { location, rerender: () => view.rerender(tree()) };
}

function renderTraces(
  items: TraceFixture[],
  total = items.length,
  locale: "pt" | "en" = "pt",
) {
  mocks.fetch.mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/v1/traces/export?")) {
      return {
        ok: true,
        status: 200,
        blob: async () => new Blob(["trace,data"]),
      } as Response;
    }
    return jsonResponse(tracesResponse(items, total));
  });

  return mountTraces(locale);
}

function expectNoClippingClasses(node: HTMLElement, boundary: HTMLElement) {
  let current: HTMLElement | null = node;
  while (current) {
    expect(current.className).not.toMatch(
      /(?:^|\s)(?:truncate|line-clamp-\S+|text-ellipsis|overflow-hidden)(?:\s|$)/,
    );
    if (current === boundary) return;
    current = current.parentElement;
  }
  throw new Error("Expected clipping check to reach the trace content boundary");
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(2026, 8, 28, 14, 30));
  localStorage.setItem("ns_locale", "pt");
  localStorage.setItem("ns_auth", JSON.stringify({ token: "test-token" }));
  Object.assign(mocks.auth, {
    isReadOnly: false,
    isBuilder: false,
    isSuperAdmin: false,
    isInvestor: false,
    viewingClinicId: "clinic-1",
  });
  vi.stubGlobal("fetch", mocks.fetch);
  vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockReturnValue({
    locale: "pt-BR",
    calendar: "gregory",
    numberingSystem: "latn",
    timeZone: "America/Sao_Paulo",
  });
  Element.prototype.scrollIntoView = vi.fn();
  const NativeURL = URL;
  vi.stubGlobal("URL", class extends NativeURL {
    static createObjectURL = vi.fn(() => "blob:trace-export");
    static revokeObjectURL = vi.fn();
  });
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  mocks.fetch.mockReset();
  mocks.toast.mockReset();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("<TracesList /> presentation contract", () => {
  it.each([
    { locale: "pt" as const, visible: "Relatórios e Exportação", hidden: "Reports & Export" },
    { locale: "en" as const, visible: "Reports & Export", hidden: "Relatórios e Exportação" },
  ])("shows a single report heading in $locale", async ({ locale, visible, hidden }) => {
    renderTraces([], 0, locale);

    expect(await screen.findByText(visible)).toBeInTheDocument();
    expect(screen.queryByText(hidden)).not.toBeInTheDocument();
  });

  it("loads the browser's local day with timezone, 50-item limit, and no export range", async () => {
    renderTraces([]);

    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledOnce());
    const url = new URL(String(mocks.fetch.mock.calls[0][0]), "https://neurostack.test");
    expect(url.pathname).toBe("/v1/traces");
    expect(Object.fromEntries(url.searchParams)).toMatchObject({
      day: "2026-09-28",
      timezone: "America/Sao_Paulo",
      limit: "50",
      offset: "0",
    });
    expect(url.searchParams.has("start_date")).toBe(false);
    expect(url.searchParams.has("end_date")).toBe(false);
  });

  it.each([
    {
      locale: "pt" as const,
      label: "Dia dos traces",
      initialButton: "Exibir traces do dia 28 de setembro de 2026",
      changedButton: "Exibir traces do dia 27 de setembro de 2026",
      title: "Traces de 27 de setembro de 2026",
    },
    {
      locale: "en" as const,
      label: "Trace day",
      initialButton: "Show traces for September 28, 2026",
      changedButton: "Show traces for September 27, 2026",
      title: "Traces for September 27, 2026",
    },
  ])("applies a drafted day explicitly in $locale", async ({ locale, label, initialButton, changedButton, title }) => {
    renderTraces([], 0, locale);
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledOnce());
    expect(screen.getByRole("button", { name: initialButton })).toBeInTheDocument();
    mocks.fetch.mockClear();

    fireEvent.change(screen.getByLabelText(label), { target: { value: "2026-09-27" } });
    expect(screen.getByRole("button", { name: changedButton })).toBeInTheDocument();
    expect(mocks.fetch).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: changedButton }));
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledOnce());
    const url = new URL(String(mocks.fetch.mock.calls[0][0]), "https://neurostack.test");
    expect(url.searchParams.get("day")).toBe("2026-09-27");
    expect(url.searchParams.get("offset")).toBe("0");
    expect(screen.getByRole("heading", { name: title })).toBeInTheDocument();
  });

  it("caps cards at 50 and uses compact numeric pagination with the correct offset and scroll", async () => {
    const items = Array.from({ length: 51 }, (_, index) =>
      traceFixture(`trace-${index + 1}`, "faq", `Mensagem ${index + 1}`),
    );
    renderTraces(items, 251);

    await screen.findByTestId("trace-row-trace-1");
    expect(screen.getAllByTestId(/^trace-row-/)).toHaveLength(50);
    expect(screen.getByRole("button", { name: "Página 1" })).toHaveAttribute("aria-current", "page");
    for (const page of [1, 2, 3, 4, 5, 6]) {
      expect(screen.getByRole("button", { name: `Página ${page}` })).toBeInTheDocument();
    }
    expect(screen.getByText("…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Anterior" })).toBeDisabled();

    mocks.fetch.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Página 3" }));
    await waitFor(() => {
      expect(mocks.fetch).toHaveBeenCalledOnce();
      expect(new URL(String(mocks.fetch.mock.calls[0][0]), "https://neurostack.test").searchParams.get("offset")).toBe("100");
    });
    expect(Element.prototype.scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth", block: "start" });
    expect(screen.getByRole("button", { name: "Página 3" })).toHaveAttribute("aria-current", "page");

    fireEvent.click(screen.getByRole("button", { name: "Página 6" }));
    await waitFor(() => {
      expect(mocks.fetch).toHaveBeenCalledTimes(2);
      expect(new URL(String(mocks.fetch.mock.calls.at(-1)?.[0]), "https://neurostack.test").searchParams.get("offset")).toBe("250");
    });
    expect(screen.getByRole("button", { name: "Próximo" })).toBeDisabled();

    mocks.fetch.mockClear();
    fireEvent.change(screen.getByLabelText("Dia dos traces"), { target: { value: "2026-09-27" } });
    fireEvent.click(screen.getByRole("button", { name: "Exibir traces do dia 27 de setembro de 2026" }));
    await waitFor(() => {
      const url = new URL(String(mocks.fetch.mock.calls[0][0]), "https://neurostack.test");
      expect(url.searchParams.get("day")).toBe("2026-09-27");
      expect(url.searchParams.get("offset")).toBe("0");
    });
  });

  it("resets filters and an effective tenant to page one while retaining tenant-correct detail links", async () => {
    mocks.auth.isBuilder = true;
    const { rerender } = renderTraces([traceFixture("tenant-trace", "faq")], 251);
    const row = await screen.findByTestId("trace-row-tenant-trace");
    expect(row.closest("a")).toHaveAttribute("href", "/traces/tenant-trace?tenant_id=clinic-1");
    expect(String(mocks.fetch.mock.calls[0][0])).toContain("tenant_id=clinic-1");

    mocks.fetch.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Página 3" }));
    await waitFor(() => {
      expect(mocks.fetch).toHaveBeenCalledOnce();
      expect(String(mocks.fetch.mock.calls[0][0])).toContain("offset=100");
    });

    mocks.fetch.mockClear();
    fireEvent.click(screen.getByLabelText("Apenas com Safety Flag ⚠️"));
    await waitFor(() => {
      expect(mocks.fetch).toHaveBeenCalledOnce();
      const url = String(mocks.fetch.mock.calls[0][0]);
      expect(url).toContain("offset=0");
      expect(url).toContain("safety_flag=true");
    });

    mocks.fetch.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Página 3" }));
    await waitFor(() => {
      expect(mocks.fetch).toHaveBeenCalledOnce();
      expect(String(mocks.fetch.mock.calls[0][0])).toContain("offset=100");
    });
    mocks.fetch.mockClear();
    fireEvent.click(screen.getByLabelText("Apenas com Handoff 🔄"));
    await waitFor(() => {
      expect(mocks.fetch).toHaveBeenCalledOnce();
      const url = String(mocks.fetch.mock.calls[0][0]);
      expect(url).toContain("offset=0");
      expect(url).toContain("handoff_required=true");
    });

    mocks.fetch.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Página 3" }));
    await waitFor(() => {
      expect(mocks.fetch).toHaveBeenCalledOnce();
      expect(String(mocks.fetch.mock.calls[0][0])).toContain("offset=100");
    });
    mocks.fetch.mockClear();
    fireEvent.change(screen.getByRole("combobox", { name: "Origem" }), { target: { value: "web" } });
    await waitFor(() => {
      expect(mocks.fetch).toHaveBeenCalledOnce();
      const url = String(mocks.fetch.mock.calls[0][0]);
      expect(url).toContain("offset=0");
      expect(url).toContain("channel=web");
    });

    mocks.fetch.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Página 3" }));
    await waitFor(() => {
      expect(mocks.fetch).toHaveBeenCalledOnce();
      expect(String(mocks.fetch.mock.calls[0][0])).toContain("offset=100");
    });
    mocks.fetch.mockClear();
    mocks.auth.viewingClinicId = "clinic-2";
    rerender();
    await waitFor(() => {
      expect(mocks.fetch).toHaveBeenCalledOnce();
      const url = String(mocks.fetch.mock.calls[0][0]);
      expect(url).toContain("tenant_id=clinic-2");
      expect(url).toContain("offset=0");
      expect(url).toContain("safety_flag=true");
    });
  });

  it("keeps the raw funnel stage in trace history", async () => {
    renderTraces([traceFixture("stage-trace", "faq")]);

    const metadata = await screen.findByTestId("trace-metadata-stage-trace");
    expect(metadata).toHaveTextContent("pos_atendimento");
    expect(metadata).not.toHaveTextContent("Pós-Atendimento");
  });

  it("shows complete long intents and messages with responsive geometry and no clipping", async () => {
    const knownTrace = traceFixture("long-trace", "pos_atendimento_duvida");
    const unknownTrace = traceFixture(
      "unknown-trace",
      "future_intent",
      "Mensagem de intent ainda desconhecido",
    );
    const { location } = renderTraces([knownTrace, unknownTrace]);

    const content = screen.getByTestId("traces-content");
    const row = await screen.findByTestId("trace-row-long-trace");
    const intentColumn = screen.getByTestId("trace-intent-long-trace");
    const badge = screen.getByTestId("badge-intent-pos_atendimento_duvida");
    const message = screen.getByTestId("trace-message-long-trace");
    const metadata = screen.getByTestId("trace-metadata-long-trace");

    expect(content).toHaveClass("max-w-7xl");
    expect(row).toHaveClass("md:min-h-24", "md:flex-row", "md:items-center");
    expect(row).not.toHaveClass("h-24", "md:h-24", "max-h-24", "md:max-h-24");
    expect(intentColumn).toHaveClass("w-full", "md:w-56", "shrink-0");
    expect(badge).toHaveTextContent("Dúvida Pós-Procedimento");
    expect(badge).toHaveClass("w-full", "justify-center");
    expect(message).toHaveTextContent(longMessage);
    expect(message).toHaveClass(
      "whitespace-normal",
      "break-words",
      "[overflow-wrap:anywhere]",
    );
    expect(message).not.toHaveAttribute("title");
    expect(metadata).toHaveClass("flex-wrap");

    const unknownBadge = screen.getByTestId("badge-intent-future_intent");
    expect(unknownBadge).toHaveTextContent("Outras");
    expect(unknownBadge.textContent).not.toContain("future_intent");
    expect(unknownBadge).toHaveClass("bg-neutral-500", "text-white");

    expectNoClippingClasses(message, content);
    expectNoClippingClasses(badge, content);
    expectNoClippingClasses(unknownBadge, content);

    const cardLink = row.closest("a");
    expect(cardLink).toHaveAttribute("href", "/traces/long-trace");
    fireEvent.click(row);
    await waitFor(() => {
      expect(location.history.at(-1)).toBe("/traces/long-trace");
    });
  });

  it("keeps page data stable while editing the independent filtered export", async () => {
    mocks.auth.isBuilder = true;
    let downloadedFilename = "";
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        downloadedFilename = this.download;
      });
    renderTraces([traceFixture("page-trace", "pedido_preco")], 151);

    await screen.findByTestId("trace-row-page-trace");
    mocks.fetch.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "Página 3" }));
    await waitFor(() => {
      expect(mocks.fetch).toHaveBeenCalledOnce();
      expect(String(mocks.fetch.mock.calls[0][0])).toContain("offset=100");
    });
    expect(screen.getByRole("button", { name: "Página 3" })).toHaveAttribute("aria-current", "page");
    mocks.fetch.mockClear();

    fireEvent.change(screen.getByLabelText("Data Inicial"), { target: { value: "2026-09-01T00:00" } });
    fireEvent.change(screen.getByLabelText("Data Final"), { target: { value: "2026-09-20T23:59" } });
    expect(mocks.fetch).not.toHaveBeenCalled();
    expect(screen.getByTestId("trace-row-page-trace")).toBeInTheDocument();
    expect(screen.getByText(/Página 3 de 4 · 151 traces/)).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("Apenas com Safety Flag ⚠️"));
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledOnce());
    mocks.fetch.mockClear();

    fireEvent.click(screen.getByLabelText("Apenas com Handoff 🔄"));
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledOnce());
    mocks.fetch.mockClear();

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "web" } });
    await waitFor(() => {
      expect(mocks.fetch).toHaveBeenCalledOnce();
      const url = String(mocks.fetch.mock.calls[0][0]);
      expect(url).toContain("safety_flag=true");
      expect(url).toContain("handoff_required=true");
      expect(url).toContain("channel=web");
    });
    mocks.fetch.mockClear();

    fireEvent.click(
      screen.getByRole("button", { name: "Exportar para Excel (CSV)" }),
    );
    await waitFor(() => {
      expect(mocks.fetch).toHaveBeenCalledOnce();
      const url = String(mocks.fetch.mock.calls[0][0]);
      expect(url).toContain("/v1/traces/export?");
      expect(url).toContain("safety_flag=true");
      expect(url).toContain("handoff_required=true");
      expect(url).toContain("channel=web");
      expect(url).toContain("tenant_id=clinic-1");
      expect(url).toContain("start_date=2026-09-01T00%3A00");
      expect(url).toContain("end_date=2026-09-20T23%3A59");
      expect(url).not.toContain("day=");
      expect(anchorClick).toHaveBeenCalledOnce();
      expect(downloadedFilename).toBe("neurostack_traces_report.csv");
    });
  });

  it("counts export days by date part only, blocking 61 days and allowing 60", async () => {
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    renderTraces([]);
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledOnce());
    mocks.fetch.mockClear();

    expect(
      screen.getByText("Exportação: até 6 meses atrás, 60 dias por arquivo."),
    ).toBeInTheDocument();
    const exportButton = screen.getByRole("button", { name: "Exportar para Excel (CSV)" });
    const hint = "O período selecionado passa de 60 dias. Reduza o intervalo para exportar.";
    expect(exportButton).toBeEnabled();
    expect(screen.queryByText(hint)).not.toBeInTheDocument();

    // Under 60 elapsed days, but 30/07..28/09 spans 61 civil dates.
    fireEvent.change(screen.getByLabelText("Data Final"), { target: { value: "2026-09-28T00:15" } });
    fireEvent.change(screen.getByLabelText("Data Inicial"), { target: { value: "2026-07-30T23:30" } });
    expect(exportButton).toBeDisabled();
    expect(screen.getByText(hint)).toBeInTheDocument();
    expect(exportButton).toHaveAttribute("aria-describedby", "trace-export-window-hint");
    fireEvent.click(exportButton);
    expect(mocks.fetch).not.toHaveBeenCalled();

    // 31/07..28/09 is exactly 60 civil dates whatever the times are.
    fireEvent.change(screen.getByLabelText("Data Inicial"), { target: { value: "2026-07-31T23:30" } });
    expect(exportButton).toBeEnabled();
    expect(screen.queryByText(hint)).not.toBeInTheDocument();
    fireEvent.click(exportButton);
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledOnce());
    const url = String(mocks.fetch.mock.calls[0][0]);
    expect(url).toContain("start_date=2026-07-31T23%3A30");
    expect(url).toContain("end_date=2026-09-28T00%3A15");
  });

  it.each([
    {
      locale: "pt" as const,
      detail: "export_window_60_days",
      button: "Exportar para Excel (CSV)",
      title: "Erro ao exportar",
      description: "Cada arquivo cobre no máximo 60 dias. Reduza o período e tente novamente.",
    },
    {
      locale: "pt" as const,
      detail: "export_lookback_6_months",
      button: "Exportar para Excel (CSV)",
      title: "Erro ao exportar",
      description: "A exportação alcança no máximo 6 meses atrás. Escolha uma data inicial mais recente.",
    },
    {
      locale: "en" as const,
      detail: "export_window_60_days",
      button: "Export to Excel (CSV)",
      title: "Export error",
      description: "Each file covers at most 60 days. Shorten the range and try again.",
    },
    {
      locale: "en" as const,
      detail: "export_lookback_6_months",
      button: "Export to Excel (CSV)",
      title: "Export error",
      description: "Exports reach back at most 6 months. Choose a more recent start date.",
    },
    {
      locale: "pt" as const,
      detail: "invalid_date_range",
      button: "Exportar para Excel (CSV)",
      title: "Erro ao exportar",
      description: "Não foi possível gerar o relatório. Tente novamente.",
    },
  ])("toasts the $detail export error in $locale", async ({ locale, detail, button, title, description }) => {
    mocks.fetch.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).startsWith("/v1/traces/export?")) return errorResponse(400, detail);
      return jsonResponse(tracesResponse([]));
    });
    mountTraces(locale);
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledOnce());

    fireEvent.click(screen.getByRole("button", { name: button }));

    await waitFor(() =>
      expect(mocks.toast).toHaveBeenCalledWith({ title, description, variant: "destructive" }),
    );
    expect(mocks.toast).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: button })).toBeEnabled();
  });

  it("hides a resolved tenant result immediately while the next tenant is pending", async () => {
    mocks.auth.isBuilder = true;
    const clinicTwo = deferred<Response>();
    mocks.fetch
      .mockResolvedValueOnce(jsonResponse(tracesResponse([traceFixture("clinic-one", "faq")], 73)))
      .mockImplementationOnce(() => clinicTwo.promise);
    const { rerender } = mountTraces();

    const oldRow = await screen.findByTestId("trace-row-clinic-one");
    expect(oldRow.closest("a")).toHaveAttribute("href", "/traces/clinic-one?tenant_id=clinic-1");
    expect(screen.getByText(/73 traces/)).toBeInTheDocument();

    mocks.auth.viewingClinicId = "clinic-2";
    rerender();

    expect(screen.queryByTestId("trace-row-clinic-one")).not.toBeInTheDocument();
    expect(screen.queryByText(/73 traces/)).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("Carregando traces de 28 de setembro de 2026")).toBeInTheDocument();
    await waitFor(() => {
      expect(mocks.fetch).toHaveBeenCalledTimes(2);
      expect(String(mocks.fetch.mock.calls[1][0])).toContain("tenant_id=clinic-2");
    });

    await act(async () => {
      clinicTwo.resolve(jsonResponse(tracesResponse([traceFixture("clinic-two", "faq")], 12)));
      await clinicTwo.promise;
    });

    const newRow = await screen.findByTestId("trace-row-clinic-two");
    expect(newRow.closest("a")).toHaveAttribute("href", "/traces/clinic-two?tenant_id=clinic-2");
    expect(screen.queryByTestId("trace-row-clinic-one")).not.toBeInTheDocument();
    expect(screen.getByText(/12 traces/)).toBeInTheDocument();
  });

  it.each(["isBuilder", "isSuperAdmin"] as const)(
    "clears self history while %s waits for a clinic, then publishes only that tenant",
    async accessRole => {
      mocks.fetch.mockResolvedValueOnce(
        jsonResponse(tracesResponse([traceFixture("self-trace", "faq")], 41)),
      );
      const { rerender } = mountTraces();

      const selfRow = await screen.findByTestId("trace-row-self-trace");
      expect(selfRow.closest("a")).toHaveAttribute("href", "/traces/self-trace");
      expect(screen.getByText(/41 traces/)).toBeInTheDocument();
      expect(String(mocks.fetch.mock.calls[0][0])).not.toContain("tenant_id=");
      mocks.fetch.mockClear();

      mocks.auth[accessRole] = true;
      mocks.auth.viewingClinicId = null;
      rerender();

      expect(screen.queryByTestId("trace-row-self-trace")).not.toBeInTheDocument();
      expect(screen.queryByText(/41 traces/)).not.toBeInTheDocument();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(screen.getByText("Carregando traces de 28 de setembro de 2026")).toBeInTheDocument();
      await act(async () => {
        await Promise.resolve();
      });
      expect(mocks.fetch).not.toHaveBeenCalled();

      const tenantResponse = deferred<Response>();
      mocks.fetch.mockImplementationOnce(() => tenantResponse.promise);
      mocks.auth.viewingClinicId = "clinic-2";
      rerender();

      await waitFor(() => expect(mocks.fetch).toHaveBeenCalledOnce());
      const tenantUrl = new URL(String(mocks.fetch.mock.calls[0][0]), "https://neurostack.test");
      expect(tenantUrl.searchParams.get("tenant_id")).toBe("clinic-2");
      expect(tenantUrl.searchParams.get("offset")).toBe("0");

      await act(async () => {
        tenantResponse.resolve(
          jsonResponse(tracesResponse([traceFixture("tenant-trace", "faq")], 9)),
        );
        await tenantResponse.promise;
      });

      const tenantRow = await screen.findByTestId("trace-row-tenant-trace");
      expect(tenantRow.closest("a")).toHaveAttribute(
        "href",
        "/traces/tenant-trace?tenant_id=clinic-2",
      );
      expect(screen.getByText(/9 traces/)).toBeInTheDocument();
      expect(screen.queryByTestId("trace-row-self-trace")).not.toBeInTheDocument();
      expect(mocks.fetch).toHaveBeenCalledOnce();
    },
  );

  it("refetches the last valid page instead of publishing an out-of-range page", async () => {
    mocks.fetch.mockImplementation(async input => {
      const offset = new URL(String(input), "https://neurostack.test").searchParams.get("offset");
      if (offset === "250") return jsonResponse(tracesResponse([], 250));
      if (offset === "200") return jsonResponse(tracesResponse([traceFixture("last-valid", "faq")], 250));
      return jsonResponse(tracesResponse([traceFixture("first-page", "faq")], 251));
    });
    mountTraces();

    await screen.findByTestId("trace-row-first-page");
    mocks.fetch.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Página 6" }));

    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(2));
    expect(new URL(String(mocks.fetch.mock.calls[0][0]), "https://neurostack.test").searchParams.get("offset")).toBe("250");
    expect(new URL(String(mocks.fetch.mock.calls[1][0]), "https://neurostack.test").searchParams.get("offset")).toBe("200");
    expect(screen.queryByText(/Página 6 de 5/)).not.toBeInTheDocument();
    expect(await screen.findByTestId("trace-row-last-valid")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Página 5" })).toHaveAttribute("aria-current", "page");
  });

  it("uses bounded pagination windows at the beginning, middle, and end", async () => {
    renderTraces([traceFixture("window-trace", "faq")], 550);
    await screen.findByTestId("trace-row-window-trace");

    for (const page of [1, 2, 3, 4, 5, 11]) {
      expect(screen.getByRole("button", { name: `Página ${page}` })).toBeInTheDocument();
    }
    expect(screen.getAllByText("…")).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "Página 5" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Página 5" })).toHaveAttribute("aria-current", "page"));
    for (const page of [1, 3, 4, 5, 6, 7, 11]) {
      expect(screen.getByRole("button", { name: `Página ${page}` })).toBeInTheDocument();
    }
    expect(screen.getAllByText("…")).toHaveLength(2);

    fireEvent.click(screen.getByRole("button", { name: "Página 11" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Página 11" })).toHaveAttribute("aria-current", "page"));
    for (const page of [1, 7, 8, 9, 10, 11]) {
      expect(screen.getByRole("button", { name: `Página ${page}` })).toBeInTheDocument();
    }
    expect(screen.getAllByText("…")).toHaveLength(1);
  });

  it("uses Investor mock history and detail links without fetching", async () => {
    mocks.auth.isInvestor = true;
    mountTraces();

    const row = await screen.findByTestId("trace-row-t1");
    expect(row.closest("a")).toHaveAttribute("href", "/traces/t1");
    expect(mocks.fetch).not.toHaveBeenCalled();
  });

  it.each(["resolve", "reject"] as const)(
    "ignores an obsolete request when it later %s after the newer day succeeds",
    async obsoleteOutcome => {
      const requestA = deferred<Response>();
      const requestB = deferred<Response>();
      mocks.fetch
        .mockImplementationOnce(() => requestA.promise)
        .mockImplementationOnce(() => requestB.promise);
      mountTraces();
      await waitFor(() => expect(mocks.fetch).toHaveBeenCalledOnce());

      fireEvent.change(screen.getByLabelText("Dia dos traces"), { target: { value: "2026-09-27" } });
      fireEvent.click(screen.getByRole("button", { name: "Exibir traces do dia 27 de setembro de 2026" }));
      await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(2));

      await act(async () => {
        requestB.resolve(jsonResponse(tracesResponse([traceFixture("new-day", "faq", "Resposta B")], 7)));
        await requestB.promise;
      });
      expect(await screen.findByTestId("trace-row-new-day")).toBeInTheDocument();
      expect(screen.getByText(/Página 1 de 1 · 7 traces · 27 de setembro de 2026/)).toBeInTheDocument();
      expect(screen.queryByText("Carregando traces de 27 de setembro de 2026")).not.toBeInTheDocument();

      await act(async () => {
        if (obsoleteOutcome === "resolve") {
          requestA.resolve(jsonResponse(tracesResponse([traceFixture("old-day", "faq", "Resposta A")], 99)));
          await requestA.promise;
        } else {
          requestA.reject(new Error("obsolete raw failure"));
          await requestA.promise.catch(() => undefined);
        }
      });

      await waitFor(() => {
        expect(screen.getByTestId("trace-row-new-day")).toBeInTheDocument();
        expect(screen.queryByTestId("trace-row-old-day")).not.toBeInTheDocument();
        expect(screen.getByText(/Página 1 de 1 · 7 traces · 27 de setembro de 2026/)).toBeInTheDocument();
        expect(screen.queryByText("obsolete raw failure")).not.toBeInTheDocument();
      });
    },
  );

  describe.each([
    {
      locale: "pt" as const,
      noToken: "Faça login para carregar os traces.",
      unauthorized: "Sua sessão expirou. Faça login novamente.",
      timezoneRequired: "O fuso horário é obrigatório para consultar esse dia. Atualize a página e tente novamente.",
      dayRequired: "Escolha um dia para usar o fuso horário informado.",
      invalidTimezone: "O fuso horário do navegador não foi reconhecido. Atualize as configurações e tente novamente.",
      conflict: "Use um dia para a lista ou um intervalo para o CSV, não ambos na mesma consulta.",
      unknown: "Não foi possível carregar os traces. Tente novamente; se persistir, contate o suporte.",
    },
    {
      locale: "en" as const,
      noToken: "Sign in to load traces.",
      unauthorized: "Your session expired. Sign in again.",
      timezoneRequired: "A timezone is required for this day. Refresh the page and try again.",
      dayRequired: "Choose a day to use the supplied timezone.",
      invalidTimezone: "The browser timezone was not recognized. Update your settings and try again.",
      conflict: "Use a day for the list or a range for CSV, not both in the same request.",
      unknown: "Could not load traces. Try again; if the problem persists, contact support.",
    },
  ])("localized list errors in $locale", errorText => {
    it("explains how to authenticate when no token exists", async () => {
      localStorage.removeItem("ns_auth");
      mountTraces(errorText.locale);

      expect(await screen.findByText(errorText.noToken)).toBeInTheDocument();
      expect(screen.getByRole("alert")).toHaveTextContent(errorText.noToken);
      expect(mocks.fetch).not.toHaveBeenCalled();
    });

    it.each([
      { status: 401, detail: undefined, message: "unauthorized" as const },
      { status: 400, detail: "timezone_required_with_day", message: "timezoneRequired" as const },
      { status: 400, detail: "day_required_with_timezone", message: "dayRequired" as const },
      { status: 400, detail: "invalid_timezone", message: "invalidTimezone" as const },
      { status: 400, detail: "day_conflicts_with_date_range", message: "conflict" as const },
      { status: 500, detail: "sensitive_raw_code", message: "unknown" as const },
    ])("maps HTTP $status / $detail without exposing raw codes", async ({ status, detail, message }) => {
      mocks.fetch.mockResolvedValue(errorResponse(status, detail));
      mountTraces(errorText.locale);

      expect(await screen.findByText(errorText[message])).toBeInTheDocument();
      expect(screen.getByRole("alert")).toHaveTextContent(errorText[message]);
      if (detail) expect(screen.queryByText(detail)).not.toBeInTheDocument();
    });
  });

  it("announces an empty resolved list as status", async () => {
    renderTraces([], 0);

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Nenhum trace encontrado em 28 de setembro de 2026.",
    );
  });

  it("renders the Dúvida Pós-Procedimento bucket badge alongside the ShieldAlert icon when safety_concern is true", async () => {
    renderTraces([
      traceFixture(
        "safety-trace",
        "pos_atendimento_sintomas_preocupantes",
        "Estou com sintomas preocupantes",
        { safety_concern: true },
      ),
    ]);

    const row = await screen.findByTestId("trace-row-safety-trace");
    const badge = screen.getByTestId("badge-intent-pos_atendimento_sintomas_preocupantes");
    expect(badge).toHaveTextContent("Dúvida Pós-Procedimento");
    const svgs = row.querySelectorAll("svg.text-destructive");
    expect(svgs.length).toBeGreaterThan(0);
  });

  it("renders the Reclamação bucket badge alongside the AlertTriangle icon when handoff_required is true", async () => {
    renderTraces([
      traceFixture(
        "handoff-trace",
        "reclamacao",
        "Quero falar com um humano",
        { handoff_required: true },
      ),
    ]);

    const row = await screen.findByTestId("trace-row-handoff-trace");
    const badge = screen.getByTestId("badge-intent-reclamacao");
    expect(badge).toHaveTextContent("Reclamação");
    const svgs = row.querySelectorAll("svg.text-orange-500");
    expect(svgs.length).toBeGreaterThan(0);
  });
});
