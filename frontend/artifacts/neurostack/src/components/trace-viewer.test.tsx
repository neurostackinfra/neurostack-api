import { cleanup, render, screen, within } from "@testing-library/react";
import type {
  DecisionTrace,
  TraceContextItem,
} from "@workspace/api-client-react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { I18nProvider, type Locale } from "@/context/i18n";
import { IntentBadge, normalizeContextSource, TraceViewer } from "./trace-viewer";

vi.mock("@/context/auth", () => ({
  useAuth: () => ({
    isSuperAdmin: false,
    isInvestor: true,
    viewingClinicId: null,
  }),
}));

function contextItem(
  overrides: Partial<TraceContextItem> = {},
): TraceContextItem {
  return {
    key: "",
    value: "Context value",
    source: "rag",
    title: "Context title",
    field: "",
    ...overrides,
  };
}

function decisionTrace(selectedContext: TraceContextItem[]): DecisionTrace {
  return {
    id: "trace-1",
    timestamp: "2026-08-30T12:00:00Z",
    userMessage: "User message",
    srmDecision: {
      intent: "faq",
      stage: "initial",
      action: "respond",
      confidence: 0.9,
      constraints: [],
      gate_authority_before_price: false,
      safety_concern: false,
      handoff_required: false,
    },
    selectedContext,
    promptBuilt: "",
    llmResponse: "Response",
    processingTimeMs: 12,
  };
}

function renderViewer(
  selectedContext: TraceContextItem[],
  locale: Locale = "en",
) {
  localStorage.setItem("ns_locale", locale);
  return render(
    <I18nProvider>
      <TraceViewer trace={decisionTrace(selectedContext)} />
    </I18nProvider>,
  );
}

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("<IntentBadge />", () => {
  it("uses the shared localized palette and merges optional classes", () => {
    localStorage.setItem("ns_locale", "en");
    render(<I18nProvider><IntentBadge intent="pedido_preco" className="rounded-none" /></I18nProvider>);
    expect(screen.getByTestId("badge-intent-pedido_preco")).toHaveTextContent("Price Request");
    expect(screen.getByTestId("badge-intent-pedido_preco")).toHaveClass("bg-blue-500", "rounded-none", "text-white");
  });

  it("renders an unknown intent with the neutral fallback", () => {
    render(<I18nProvider><IntentBadge intent="future_intent" /></I18nProvider>);
    expect(screen.getByTestId("badge-intent-future_intent")).toHaveTextContent("future_intent");
    expect(screen.getByTestId("badge-intent-future_intent")).toHaveClass("bg-slate-500", "text-white");
  });

  it.each(["constructor", "__proto__", "toString"])(
    "renders inherited object property %s as a raw unknown intent",
    (intentName) => {
      render(<I18nProvider><IntentBadge intent={intentName} /></I18nProvider>);
      const badge = screen.getByTestId(`badge-intent-${intentName}`);
      expect(badge).toHaveTextContent(intentName);
      expect(badge).toHaveClass("bg-slate-500", "text-white");
    },
  );
});

describe("normalizeContextSource", () => {
  it("normalizes canonical and compatibility sources without classifying new values as config", () => {
    expect(normalizeContextSource("rag")).toBe("rag");
    expect(normalizeContextSource("  RAG  ")).toBe("rag");
    expect(normalizeContextSource("config")).toBe("config");
    expect(normalizeContextSource("  CONFIG  ")).toBe("config");
    expect(normalizeContextSource("clinic_config")).toBe("config");
    expect(normalizeContextSource("  CLINIC_CONFIG  ")).toBe("config");
    expect(normalizeContextSource("future_source")).toBe("unknown");
    expect(normalizeContextSource("   ")).toBe("unknown");
    expect(normalizeContextSource(undefined)).toBe("unknown");
  });
});

describe("<TraceViewer /> selected context", () => {
  it("renders normalized source variants with icons and preserves input order", () => {
    renderViewer([
      contextItem({ title: "First", value: "Value 1", source: "  RAG  " }),
      contextItem({ title: "Second", value: "Value 2", source: " CONFIG " }),
      contextItem({ title: "Third", value: "Value 3", source: " CLINIC_CONFIG " }),
      contextItem({ title: "Fourth", value: "Value 4", source: "new_source" }),
    ]);

    const cards = screen.getAllByTestId(/^context-item-/);
    expect(cards.map((card) => within(card).getByTestId(/^context-title-/).textContent)).toEqual([
      "First",
      "Second",
      "Third",
      "Fourth",
    ]);
    expect(cards.map((card) => card.getAttribute("data-source-kind"))).toEqual([
      "rag",
      "config",
      "config",
      "unknown",
    ]);

    expect(within(cards[0]).getByText("RAG")).toBeInTheDocument();
    expect(within(cards[1]).getByText("Clinic Config")).toBeInTheDocument();
    expect(within(cards[2]).getByText("Clinic Config")).toBeInTheDocument();
    expect(within(cards[3]).getByText("Unknown Source")).toBeInTheDocument();
    expect(within(cards[3]).queryByText("Clinic Config")).not.toBeInTheDocument();

    for (const source of screen.getAllByTestId(/^context-source-/)) {
      expect(source.querySelector("svg")).not.toBeNull();
    }
  });

  it("uses title, localized field, localized key, and terminal fallbacks while retaining values", () => {
    const longValue = `First line\n${"unbroken-value-".repeat(30)}`;
    renderViewer([
      contextItem({
        title: "  Friendly title  ",
        field: "identity.clinic_name",
        value: longValue,
      }),
      contextItem({ title: "   ", field: "identity.clinic_name", value: "Field value" }),
      contextItem({ title: "", field: "", key: "persona.agent_name", value: "Key value" }),
      contextItem({ title: "", field: "", key: "", value: "Terminal value" }),
    ]);

    expect(screen.getByTestId("context-title-0")).toHaveTextContent("Friendly title");
    expect(screen.getByTestId("context-title-1")).toHaveTextContent("Clinic Name");
    expect(screen.getByTestId("context-title-2")).toHaveTextContent("Agent Name");
    expect(screen.getByTestId("context-title-3")).toHaveTextContent("Context");
    expect(screen.getByTestId("context-value-0").textContent).toBe(longValue);
    expect(screen.getByTestId("context-value-1")).toHaveTextContent("Field value");
    expect(screen.getByTestId("context-value-2")).toHaveTextContent("Key value");
    expect(screen.getByTestId("context-value-3")).toHaveTextContent("Terminal value");
  });

  it("declares the responsive grid and wrapping classes without truncation", () => {
    renderViewer([contextItem()]);

    expect(screen.getByTestId("context-grid")).toHaveClass(
      "grid-cols-1",
      "md:grid-cols-2",
    );
    expect(screen.getByTestId("context-item-0")).toHaveClass("min-w-0");
    expect(screen.getByTestId("context-title-0")).toHaveClass("break-words");
    expect(screen.getByTestId("context-value-0")).toHaveClass(
      "whitespace-pre-wrap",
      "break-words",
    );
    expect(screen.getByTestId("context-item-0").className).not.toMatch(
      /truncate|line-clamp/,
    );
  });

  it("renders the Portuguese panel, source labels, terminal title, and empty state", () => {
    renderViewer(
      [
        contextItem({ source: "RAG" }),
        contextItem({ source: "CONFIG" }),
        contextItem({ title: "", source: "other" }),
      ],
      "pt",
    );

    expect(screen.getByText("Contexto Selecionado")).toBeInTheDocument();
    expect(screen.getByText("RAG")).toBeInTheDocument();
    expect(screen.getByText("Configuração da Clínica")).toBeInTheDocument();
    expect(screen.getByText("Origem Desconhecida")).toBeInTheDocument();
    expect(screen.getByTestId("context-title-2")).toHaveTextContent("Contexto");

    cleanup();
    renderViewer([], "pt");
    expect(screen.getByText("Contexto Selecionado")).toBeInTheDocument();
    expect(screen.getByText("Nenhum contexto utilizado.")).toBeInTheDocument();
  });

  it("renders the English panel and current empty-state copy", () => {
    renderViewer([]);

    expect(screen.getByText("Selected Context")).toBeInTheDocument();
    expect(screen.getByText("No context used.")).toBeInTheDocument();
  });
});
