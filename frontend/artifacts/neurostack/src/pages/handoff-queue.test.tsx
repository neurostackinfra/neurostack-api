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
}));

vi.mock("@/context/auth", () => ({
  useAuth: () => mocks.auth,
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
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
  vi.stubGlobal("fetch", mocks.fetch);
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("<HandoffQueuePage />", () => {
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
    const createObjectURL = vi.fn(() => "blob:handoffs");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL, revokeObjectURL }));
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    mocks.fetch.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/export?")) {
        return { ok: true, blob: async () => new Blob(["csv"]) } as Response;
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

    fireEvent.click(screen.getByRole("button", { name: "Exportar CSV" }));
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(2));

    const exportUrl = String(mocks.fetch.mock.calls[1][0]);
    expect(initialUrl).toMatch(/^\/v1\/handoffs\/queue\?tenant_id=clinic-id&from_date=\d{4}-\d{2}-\d{2}&to_date=\d{4}-\d{2}-\d{2}$/);
    expect(exportUrl).toMatch(/^\/v1\/handoffs\/queue\/export\?tenant_id=clinic-id&from_date=\d{4}-\d{2}-\d{2}&to_date=\d{4}-\d{2}-\d{2}$/);
    expect(exportUrl).not.toContain("safety");
    expect(exportUrl).not.toContain("intent");
    expect(createObjectURL).toHaveBeenCalledTimes(1);
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
