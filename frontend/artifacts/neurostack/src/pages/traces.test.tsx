import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

import { I18nProvider } from "@/context/i18n";
import TracesList from "./traces";

const mocks = vi.hoisted(() => ({
  fetch: vi.fn(),
  toast: vi.fn(),
}));

vi.mock("@/context/auth", () => ({
  useAuth: () => ({
    isReadOnly: false,
    isBuilder: false,
    isSuperAdmin: false,
    isInvestor: false,
    viewingClinicId: "clinic-1",
  }),
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
    limit: 20,
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

function renderTraces(items: TraceFixture[], total = items.length) {
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

  const location = memoryLocation({ path: "/traces", record: true });
  render(
    <Router hook={location.hook}>
      <I18nProvider>
        <TracesList />
      </I18nProvider>
    </Router>,
  );
  return location;
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
  localStorage.setItem("ns_locale", "pt");
  localStorage.setItem("ns_auth", JSON.stringify({ token: "test-token" }));
  vi.stubGlobal("fetch", mocks.fetch);
  vi.stubGlobal("URL", {
    ...URL,
    createObjectURL: vi.fn(() => "blob:trace-export"),
    revokeObjectURL: vi.fn(),
  });
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  mocks.fetch.mockReset();
  mocks.toast.mockReset();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("<TracesList /> presentation contract", () => {
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
    const location = renderTraces([knownTrace, unknownTrace]);

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

  it("keeps filters, pagination, and filtered export behavior intact", async () => {
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    renderTraces([traceFixture("page-trace", "pedido_preco")], 21);

    await screen.findByTestId("trace-row-page-trace");
    mocks.fetch.mockClear();

    fireEvent.click(screen.getByLabelText("Apenas com Safety Flag ⚠️"));
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "web" } });

    await waitFor(() => {
      expect(
        mocks.fetch.mock.calls.some(([input]) => {
          const url = String(input);
          return url.startsWith("/v1/traces?") &&
            url.includes("safety_flag=true") &&
            url.includes("channel=web");
        }),
      ).toBe(true);
    });

    fireEvent.click(screen.getByRole("button", { name: "Proximo" }));
    await waitFor(() => {
      expect(
        mocks.fetch.mock.calls.some(([input]) =>
          String(input).includes("offset=20"),
        ),
      ).toBe(true);
    });

    fireEvent.click(
      screen.getByRole("button", { name: "Exportar para Excel (CSV)" }),
    );
    await waitFor(() => {
      expect(
        mocks.fetch.mock.calls.some(([input]) => {
          const url = String(input);
          return url.startsWith("/v1/traces/export?") &&
            url.includes("safety_flag=true") &&
            url.includes("channel=web");
        }),
      ).toBe(true);
      expect(anchorClick).toHaveBeenCalledOnce();
    });
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
