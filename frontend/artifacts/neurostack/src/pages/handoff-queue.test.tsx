import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { I18nProvider } from "@/context/i18n";
import HandoffQueuePage from "./handoff-queue";

const mocks = vi.hoisted(() => ({
  auth: {
    tenantId: "clinic-id" as string | null,
    isBuilder: false,
    isSuperAdmin: false,
    isInvestor: false,
    viewingClinicId: "",
  },
  fetch: vi.fn(),
  toast: vi.fn(),
}));

vi.mock("@/context/auth", () => ({
  useAuth: () => mocks.auth,
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mocks.toast }),
}));

interface QueueItemFixture {
  id: string;
  numero_whatsapp: string;
  session_id: string;
  intencao_detectada: string | null;
  estagio_atual: string;
  primeira_linha_mensagem: null;
  created_at: string;
  data_hora_saida: null;
  status: string;
  safety_flag: boolean;
}

function todayAt(hour: number): string {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate(), hour).toISOString();
}

function todayYmd(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

function item(
  id: string,
  intent: string | null,
  safetyFlag: boolean,
  hour: number,
): QueueItemFixture {
  return {
    id,
    numero_whatsapp: `55119999999${id.charCodeAt(0)}`,
    session_id: `${id}-12345678-abcd-4abc-8def-1234567890ab`,
    intencao_detectada: intent,
    estagio_atual: `hidden-stage-${id}`,
    primeira_linha_mensagem: null,
    created_at: todayAt(hour),
    data_hora_saida: null,
    status: "pending",
    safety_flag: safetyFlag,
  };
}

// Deliberately not chronological: the UI must preserve this API/FIFO order.
const FIFO_ITEMS = [
  item("a", "pedido_preco", false, 15),
  item("b", "agendar", true, 9),
  item("c", "orcamento_existente", true, 14),
  item("d", null, true, 10),
  item("e", "future_intent", true, 13),
];

function queueResponse(items: QueueItemFixture[] = FIFO_ITEMS, avg = 138, pct = 82): Response {
  return {
    ok: true,
    json: async () => ({ items, avg_wait_seconds: avg, pct_within_sla: pct }),
  } as Response;
}

function renderPage(locale: "pt" | "en" = "pt") {
  localStorage.setItem("ns_locale", locale);
  return render(
    <I18nProvider>
      <HandoffQueuePage />
    </I18nProvider>,
  );
}

function cardIds(): string[] {
  return screen.queryAllByTestId("handoff-card").map((card) => card.getAttribute("data-handoff-id") ?? "");
}

async function renderLoaded(locale: "pt" | "en" = "pt") {
  mocks.fetch.mockResolvedValue(queueResponse());
  renderPage(locale);
  await waitFor(() => expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]));
}

beforeEach(() => {
  localStorage.setItem("ns_auth", JSON.stringify({ token: "test-token" }));
  Object.assign(mocks.auth, {
    tenantId: "clinic-id",
    isBuilder: false,
    isSuperAdmin: false,
    isInvestor: false,
    viewingClinicId: "",
  });
  mocks.fetch.mockReset();
  mocks.toast.mockReset();
  vi.stubGlobal("fetch", mocks.fetch);
  vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockReturnValue({
    locale: "pt-BR",
    calendar: "gregory",
    numberingSystem: "latn",
    timeZone: "America/Sao_Paulo",
  });
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("<HandoffQueuePage />", () => {
  it("loads a fetched payload containing estagio_atual without rendering its label or value", async () => {
    mocks.fetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        items: [{
          id: "handoff-1",
          numero_whatsapp: "5511999999999",
          session_id: "12345678-abcd-4abc-8def-1234567890ab",
          intencao_detectada: "pedido_preco",
          estagio_atual: "stage-payload-marker",
          primeira_linha_mensagem: null,
          created_at: new Date().toISOString(),
          data_hora_saida: null,
          status: "pending",
          safety_flag: false,
        }],
        avg_wait_seconds: 10,
        pct_within_sla: 90,
      }),
    } as Response);

    renderPage();

    expect(await screen.findByText("pedido_preco")).toBeInTheDocument();
    expect(screen.queryByText("Estágio")).not.toBeInTheDocument();
    expect(screen.queryByText("stage-payload-marker")).not.toBeInTheDocument();
  });

  it.each([
    {
      locale: "pt" as const,
      averageTitle: "Tempo médio de handoff",
      averageDescription: "Tempo médio que o humano demora para atender",
      goalTitle: "Meta de atendimento no handoff",
      goalLabel: "Meta: ≥ 85%",
      goalDescription: "Porcentagem da meta do tempo de atendimento alcançado",
      hidden: ["Average Handoff Time", "Handoff Goal Achievement"],
    },
    {
      locale: "en" as const,
      averageTitle: "Average Handoff Time",
      averageDescription: "Average time before a human starts the service",
      goalTitle: "Handoff Goal Achievement",
      goalLabel: "Goal: ≥ 85%",
      goalDescription: "Percentage of the service-time goal achieved",
      hidden: ["Tempo médio de handoff", "Meta de atendimento no handoff"],
    },
  ])("shows handoff analytics only in $locale", async ({
    locale,
    averageTitle,
    averageDescription,
    goalTitle,
    goalLabel,
    goalDescription,
    hidden,
  }) => {
    await renderLoaded(locale);

    expect(screen.getByText(averageTitle)).toBeInTheDocument();
    expect(screen.getByText(averageDescription)).toBeInTheDocument();
    expect(screen.getByText(goalTitle)).toBeInTheDocument();
    expect(screen.getByText((_, element) => (
      element?.tagName === "P" && element.textContent === goalLabel
    ))).toBeInTheDocument();
    expect(screen.getByText(goalDescription)).toBeInTheDocument();
    hidden.forEach((text) => expect(screen.queryByText(text)).not.toBeInTheDocument());
  });

  it("preserves received order through safety, bucket, AND, and clear without fetching", async () => {
    await renderLoaded();
    const requestCount = mocks.fetch.mock.calls.length;
    const dates = screen.getAllByDisplayValue(todayYmd());

    expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]);
    expect(screen.getByText("00:02:18")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("checkbox", { name: "Somente com sinalização de segurança" }));
    expect(cardIds()).toEqual(["b", "c", "d", "e"]);

    fireEvent.change(screen.getByRole("combobox", { name: "Grupo de intenção" }), {
      target: { value: "pedido_preco" },
    });
    expect(cardIds()).toEqual(["c"]);

    fireEvent.click(screen.getByRole("checkbox", { name: "Somente com sinalização de segurança" }));
    expect(cardIds()).toEqual(["a", "c"]);

    fireEvent.click(screen.getByRole("button", { name: "Limpar filtros" }));
    expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]);
    expect(screen.getByRole("combobox", { name: "Grupo de intenção" })).toHaveValue("all");
    expect(dates).toHaveLength(2);
    expect(dates.every((date) => (date as HTMLInputElement).value === todayYmd())).toBe(true);
    expect(screen.getByText("00:02:18")).toBeInTheDocument();
    expect(mocks.fetch).toHaveBeenCalledTimes(requestCount);
  });

  it("renders null intent as a dash, excludes it from 'outras', and distinguishes filtered empty", async () => {
    await renderLoaded();

    expect(within(screen.getAllByTestId("handoff-card")[3]).getByText("—")).toBeInTheDocument();

    fireEvent.change(screen.getByRole("combobox", { name: "Grupo de intenção" }), {
      target: { value: "outras" },
    });
    expect(cardIds()).toEqual(["e"]);

    fireEvent.click(screen.getByRole("checkbox", { name: "Somente com sinalização de segurança" }));
    expect(cardIds()).toEqual(["e"]);

    fireEvent.change(screen.getByRole("combobox", { name: "Grupo de intenção" }), {
      target: { value: "reclamacao" },
    });
    expect(cardIds()).toEqual([]);
    expect(screen.getByText("Nenhum card corresponde aos filtros selecionados.")).toBeInTheDocument();
    expect(screen.queryByText("Nenhum handoff ativo no momento.")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Limpar filtros" }));
    expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]);
  });

  it("preserves the next polled response order and the active filters", async () => {
    vi.useFakeTimers();
    const polledItems = [FIFO_ITEMS[4], FIFO_ITEMS[2], FIFO_ITEMS[0], FIFO_ITEMS[1], FIFO_ITEMS[3]];
    mocks.fetch
      .mockResolvedValueOnce(queueResponse())
      .mockResolvedValueOnce(queueResponse(polledItems));

    renderPage();
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]);

    fireEvent.click(screen.getByRole("checkbox", { name: "Somente com sinalização de segurança" }));
    expect(cardIds()).toEqual(["b", "c", "d", "e"]);

    await act(async () => {
      vi.advanceTimersByTime(30000);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(mocks.fetch).toHaveBeenCalledTimes(2);
    expect(cardIds()).toEqual(["e", "c", "b", "d"]);
  });

  it("shows a translated safety marker only on flagged cards", async () => {
    await renderLoaded();

    expect(screen.getAllByLabelText("Sinalização de segurança")).toHaveLength(4);
    expect(within(screen.getAllByTestId("handoff-card")[0]).queryByLabelText("Sinalização de segurança")).not.toBeInTheDocument();
    expect(within(screen.getAllByTestId("handoff-card")[1]).getByLabelText("Sinalização de segurança")).toBeInTheDocument();
  });

  it.each([
    {
      locale: "pt" as const,
      safety: "Somente com sinalização de segurança",
      intent: "Grupo de intenção",
      scope: "Os filtros de segurança e intenção se aplicam somente aos cards.",
      clear: "Limpar filtros",
      empty: "Nenhum card corresponde aos filtros selecionados.",
    },
    {
      locale: "en" as const,
      safety: "Only safety-flagged",
      intent: "Intent group",
      scope: "Safety and intent filters apply only to cards.",
      clear: "Clear filters",
      empty: "No cards match the selected filters.",
    },
  ])("keeps accessible controls, scope, clear, and filtered empty in $locale", async ({ locale, safety, intent, scope, clear, empty }) => {
    await renderLoaded(locale);
    const requestCount = mocks.fetch.mock.calls.length;

    expect(screen.getByText(scope)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: safety }));
    fireEvent.change(screen.getByRole("combobox", { name: intent }), { target: { value: "reclamacao" } });
    expect(screen.getByText(empty)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: clear }));
    expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]);
    expect(mocks.fetch).toHaveBeenCalledTimes(requestCount);
  });

  it("keeps KPI and date-only CSV semantics when card filters change", async () => {
    const exportedBlob = new Blob(["csv"]);
    const blob = vi.fn(async () => exportedBlob);
    const createObjectURL = vi.fn(() => "blob:handoffs");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL, revokeObjectURL }));
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    mocks.fetch.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/export?")) {
        return { ok: true, blob } as unknown as Response;
      }
      return queueResponse();
    });

    renderPage();
    await waitFor(() => expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]));
    const initialUrl = String(mocks.fetch.mock.calls[0][0]);

    fireEvent.click(screen.getByRole("checkbox", { name: "Somente com sinalização de segurança" }));
    fireEvent.change(screen.getByRole("combobox", { name: "Grupo de intenção" }), {
      target: { value: "pedido_preco" },
    });
    expect(screen.getByText("00:02:18")).toBeInTheDocument();
    expect(mocks.fetch).toHaveBeenCalledTimes(1);

    const createElement = vi.spyOn(document, "createElement");
    const appendChild = vi.spyOn(document.body, "appendChild");
    const removeChild = vi.spyOn(document.body, "removeChild");

    fireEvent.click(screen.getByRole("button", { name: "Exportar CSV" }));
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(2));

    const exportUrl = String(mocks.fetch.mock.calls[1][0]);
    expect(initialUrl).toMatch(/^\/v1\/handoffs\/queue\?tenant_id=clinic-id&from_date=\d{4}-\d{2}-\d{2}&to_date=\d{4}-\d{2}-\d{2}$/);
    expect(exportUrl).toMatch(/^\/v1\/handoffs\/queue\/export\?tenant_id=clinic-id&from_date=\d{4}-\d{2}-\d{2}&to_date=\d{4}-\d{2}-\d{2}&timezone=America%2FSao_Paulo$/);
    expect(exportUrl).not.toContain("safety");
    expect(exportUrl).not.toContain("intent");
    expect(mocks.fetch.mock.calls[1][1]).toEqual({
      headers: { Authorization: "Bearer test-token" },
    });
    expect(blob).toHaveBeenCalledTimes(1);
    expect(createObjectURL).toHaveBeenCalledWith(exportedBlob);
    const anchor = createElement.mock.results.find(
      (result) => result.value instanceof HTMLAnchorElement,
    )?.value as HTMLAnchorElement;
    expect(anchor).toBeInstanceOf(HTMLAnchorElement);
    expect(anchor.download).toBe("handoffs.csv");
    expect(anchor.href).toBe("blob:handoffs");
    expect(appendChild).toHaveBeenCalledWith(anchor);
    expect(anchorClick).toHaveBeenCalledTimes(1);
    expect(removeChild).toHaveBeenCalledWith(anchor);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:handoffs");
  });

  it.each([
    {
      locale: "pt" as const,
      detail: "export_limit_10000_exceeded",
      message: "A exportação excede o limite de 10.000 registros. Reduza o período.",
      button: "Exportar CSV",
    },
    {
      locale: "en" as const,
      detail: "export_limit_10000_exceeded",
      message: "The export exceeds the 10,000-record limit. Shorten the date range.",
      button: "Export CSV",
    },
    {
      locale: "pt" as const,
      detail: "invalid_timezone",
      message: "Não foi possível usar o fuso horário do navegador. Verifique as configurações de data e fuso horário e tente novamente.",
      button: "Exportar CSV",
    },
    {
      locale: "en" as const,
      detail: "invalid_timezone",
      message: "The browser timezone could not be used. Check your date and timezone settings, then try again.",
      button: "Export CSV",
    },
  ])("shows the localized $detail export error in $locale without downloading", async ({ locale, detail, message, button }) => {
    const createObjectURL = vi.fn();
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL }));
    mocks.fetch.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/export?")) {
        return {
          ok: false,
          status: 400,
          json: async () => ({ detail }),
        } as Response;
      }
      return queueResponse();
    });

    renderPage(locale);
    await waitFor(() => expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]));
    fireEvent.click(screen.getByRole("button", { name: button }));

    await waitFor(() => {
      expect(mocks.toast).toHaveBeenCalledWith({ title: message, variant: "destructive" });
    });
    const exportUrl = String(mocks.fetch.mock.calls[1][0]);
    expect(exportUrl).toContain("timezone=America%2FSao_Paulo");
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(anchorClick).not.toHaveBeenCalled();
  });

  it("keeps checkout and attend-and-redirect actions operational", async () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    mocks.fetch
      .mockResolvedValueOnce(queueResponse())
      .mockResolvedValue({ ok: true } as Response);

    renderPage();
    await waitFor(() => expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]));
    const firstCard = screen.getAllByTestId("handoff-card")[0];
    fireEvent.click(within(firstCard).getByRole("button", { name: "Atender e direcionar" }));

    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(2));
    expect(open).toHaveBeenCalledWith("https://wa.me/5511999999997", "_blank");
    expect(mocks.fetch).toHaveBeenLastCalledWith(
      "/v1/handoffs/queue/a/checkout",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("filters investor mocks across buckets and safety without fetching or revealing stage", async () => {
    Object.assign(mocks.auth, { isInvestor: true, tenantId: null });

    renderPage();
    await waitFor(() => expect(screen.getAllByTestId("handoff-card")).toHaveLength(6));
    expect(screen.getAllByText("agendar")).toHaveLength(2);

    fireEvent.change(screen.getByRole("combobox", { name: "Grupo de intenção" }), {
      target: { value: "pedido_preco" },
    });
    expect(screen.getAllByTestId("handoff-card")).toHaveLength(1);

    fireEvent.click(screen.getByRole("checkbox", { name: "Somente com sinalização de segurança" }));
    expect(screen.getAllByTestId("handoff-card")).toHaveLength(1);
    expect(screen.queryByText("Estágio")).not.toBeInTheDocument();
    expect(screen.queryByText("agendamento_solicitado")).not.toBeInTheDocument();
    expect(screen.queryByText("em_qualificacao")).not.toBeInTheDocument();
    expect(mocks.fetch).not.toHaveBeenCalled();
  });
});
