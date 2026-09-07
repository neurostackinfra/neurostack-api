import { cleanup, render, screen, within } from "@testing-library/react";
import type {
  DecisionTrace,
  TraceContextItem,
} from "@workspace/api-client-react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { I18nProvider, type Locale } from "@/context/i18n";
import { IntentBadge, normalizeContextSource, TraceViewer } from "./trace-viewer";

const authState = {
  isSuperAdmin: false,
  isInvestor: true,
};

vi.mock("@/context/auth", () => ({
  useAuth: () => authState,
}));

const ratingBlockCalls: Array<Record<string, unknown>> = [];
vi.mock("@/components/rating-block", () => ({
  RatingBlock: (props: Record<string, unknown>) => {
    ratingBlockCalls.push(props);
    return <div data-testid="rating-block-mock" data-clinic-tenant-id={String(props.clinicTenantId)} />;
  },
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

function decisionTrace(selectedContext?: TraceContextItem[]): DecisionTrace {
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
    ...(selectedContext === undefined ? {} : { selectedContext }),
    promptBuilt: "",
    llmResponse: "Response",
    processingTimeMs: 12,
  } as DecisionTrace;
}

function renderViewer(
  selectedContext?: TraceContextItem[],
  locale: Locale = "en",
  clinicTenantId?: string | null,
) {
  localStorage.setItem("ns_locale", locale);
  return render(
    <I18nProvider>
      <TraceViewer
        trace={decisionTrace(selectedContext)}
        clinicTenantId={clinicTenantId}
      />
    </I18nProvider>,
  );
}

afterEach(() => {
  cleanup();
  localStorage.clear();
  ratingBlockCalls.length = 0;
  authState.isSuperAdmin = false;
  authState.isInvestor = true;
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
  it("keeps the hardcoded Stage heading and localized stage value", () => {
    renderViewer(undefined, "pt");

    expect(screen.getByText("Stage")).toBeInTheDocument();
    expect(screen.getByTestId("trace-stage")).toHaveTextContent("Inicial");
  });

  it("groups mixed normalized sources into exactly two fixed cards in relative input order", () => {
    renderViewer([
      contextItem({ title: "RAG first", value: "RAG value 1", source: "  RAG  " }),
      contextItem({ title: "Config first", value: "Config value 1", source: " CONFIG " }),
      contextItem({ title: "RAG second", value: "RAG value 2", source: "rag" }),
      contextItem({ title: "Config second", value: "Config value 2", source: " CLINIC_CONFIG " }),
    ]);

    const grid = screen.getByTestId("context-grid");
    expect(within(grid).getAllByTestId(/^context-card-/)).toHaveLength(2);
    expect(screen.getByTestId("context-card-rag")).toBeInTheDocument();
    expect(screen.getByTestId("context-card-config")).toBeInTheDocument();
    expect(screen.queryByTestId("context-card-unknown")).not.toBeInTheDocument();

    const ragCard = screen.getByTestId("context-card-rag");
    expect(
      within(ragCard).getAllByTestId(/^context-title-rag-/).map((title) => title.textContent),
    ).toEqual(["RAG first", "RAG second"]);
    expect(within(ragCard).queryByText("Config first")).not.toBeInTheDocument();

    const configCard = screen.getByTestId("context-card-config");
    expect(
      within(configCard).getAllByTestId(/^context-title-config-/).map((title) => title.textContent),
    ).toEqual(["Config first", "Config second"]);
    expect(within(configCard).queryByText("RAG first")).not.toBeInTheDocument();
  });

  it("gives each card its own labeled header, Lucide icon, divider, body, titles, and values", () => {
    renderViewer([
      contextItem({ title: "RAG title", value: "RAG value", source: "rag" }),
      contextItem({ title: "Config title", value: "Config value", source: "config" }),
    ]);

    for (const [source, label] of [
      ["rag", "RAG"],
      ["config", "Clinic Config"],
    ] as const) {
      const card = screen.getByTestId(`context-card-${source}`);
      expect(screen.getByRole("region", { name: label })).toBe(card);
      expect(within(card).getByRole("heading", { name: label })).toBeInTheDocument();
      const header = within(card).getByTestId(`context-header-${source}`);
      const icon = within(header).getByTestId(`context-origin-icon-${source}`);
      const divider = within(card).getByTestId(`context-divider-${source}`);
      const body = within(card).getByTestId(`context-body-${source}`);
      const title = within(body).getByTestId(`context-title-${source}-0`);
      const value = within(body).getByTestId(`context-value-${source}-0`);

      expect(header).toHaveTextContent(label);
      expect(icon.tagName.toLowerCase()).toBe("svg");
      expect(divider).toHaveClass("border-t");
      expect(title).not.toBe(value);
      expect(title).toHaveTextContent(source === "rag" ? "RAG title" : "Config title");
      expect(value).toHaveTextContent(source === "rag" ? "RAG value" : "Config value");
    }
  });

  it("keeps both cards visible and gives each collection its own total empty state", () => {
    renderViewer([]);

    expect(screen.getAllByTestId(/^context-card-/)).toHaveLength(2);
    expect(screen.getByTestId("context-empty-rag")).toHaveTextContent("Not used");
    expect(screen.getByTestId("context-empty-config")).toHaveTextContent("Not used");
    expect(screen.queryByTestId("context-unknown-fallback")).not.toBeInTheDocument();
  });

  it("keeps both fixed cards empty when selectedContext is omitted", () => {
    renderViewer(undefined);

    expect(screen.getAllByTestId(/^context-card-/)).toHaveLength(2);
    expect(screen.getByTestId("context-empty-rag")).toHaveTextContent("Not used");
    expect(screen.getByTestId("context-empty-config")).toHaveTextContent("Not used");
  });

  it.each([
    ["rag", "config"],
    ["config", "rag"],
  ] as const)(
    "keeps the %s card populated and shows the partial empty state only for %s",
    (populatedSource, emptySource) => {
      renderViewer([
        contextItem({ source: populatedSource, title: "Used title", value: "Used value" }),
      ]);

      expect(screen.getAllByTestId(/^context-card-/)).toHaveLength(2);
      expect(screen.getByTestId(`context-item-${populatedSource}-0`)).toHaveTextContent(
        "Used title",
      );
      expect(screen.queryByTestId(`context-empty-${populatedSource}`)).not.toBeInTheDocument();
      expect(screen.getByTestId(`context-empty-${emptySource}`)).toHaveTextContent("Not used");
    },
  );

  it("keeps unknown items visible in a neutral fallback outside the two-card grid", () => {
    renderViewer([
      contextItem({ title: "Known", value: "Known value", source: "rag" }),
      contextItem({ title: "Unknown first", value: "Unknown value 1", source: "future_source" }),
      contextItem({ title: "Unknown second", value: "Unknown value 2", source: "   " }),
    ]);

    const grid = screen.getByTestId("context-grid");
    const fallback = screen.getByTestId("context-unknown-fallback");
    expect(grid).not.toContainElement(fallback);
    expect(within(grid).getAllByTestId(/^context-card-/)).toHaveLength(2);
    expect(screen.queryByTestId("context-card-unknown")).not.toBeInTheDocument();
    expect(within(fallback).getByText("Unknown Source")).toBeInTheDocument();
    expect(within(fallback).queryByText("RAG")).not.toBeInTheDocument();
    expect(within(fallback).queryByText("Clinic Config")).not.toBeInTheDocument();
    expect(
      within(fallback).getAllByTestId(/^context-title-unknown-/).map((title) => title.textContent),
    ).toEqual(["Unknown first", "Unknown second"]);
    expect(within(fallback).getByTestId("context-value-unknown-0")).toHaveTextContent(
      "Unknown value 1",
    );
  });

  it("uses title, localized field, localized key, and terminal fallbacks while retaining full values", () => {
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

    expect(screen.getByTestId("context-title-rag-0")).toHaveTextContent("Friendly title");
    expect(screen.getByTestId("context-title-rag-1")).toHaveTextContent("Clinic Name");
    expect(screen.getByTestId("context-title-rag-2")).toHaveTextContent("Agent Name");
    expect(screen.getByTestId("context-title-rag-3")).toHaveTextContent("Context");
    expect(screen.getByTestId("context-value-rag-0").textContent).toBe(longValue);
    expect(screen.getByTestId("context-value-rag-1")).toHaveTextContent("Field value");
    expect(screen.getByTestId("context-value-rag-2")).toHaveTextContent("Key value");
    expect(screen.getByTestId("context-value-rag-3")).toHaveTextContent("Terminal value");
  });

  it("declares the responsive grid and wrapping classes without truncation", () => {
    renderViewer([contextItem()]);

    expect(screen.getByTestId("context-grid")).toHaveClass(
      "grid-cols-1",
      "md:grid-cols-2",
    );
    expect(screen.getByTestId("context-card-rag")).toHaveClass("min-w-0");
    expect(screen.getByTestId("context-item-rag-0")).toHaveClass("min-w-0");
    expect(screen.getByTestId("context-title-rag-0")).toHaveClass("break-words");
    expect(screen.getByTestId("context-value-rag-0")).toHaveClass(
      "whitespace-pre-wrap",
      "break-words",
    );
    expect(screen.getByTestId("context-item-rag-0").className).not.toMatch(/truncate|line-clamp/);
  });

  it("renders Portuguese labels, title fallback, and per-card empty copy", () => {
    renderViewer(
      [
        contextItem({ title: "", source: "other" }),
      ],
      "pt",
    );

    expect(screen.getByText("Contexto Selecionado")).toBeInTheDocument();
    expect(screen.getByText("RAG")).toBeInTheDocument();
    expect(screen.getByText("Configuração da Clínica")).toBeInTheDocument();
    expect(screen.getByText("Origem Desconhecida")).toBeInTheDocument();
    expect(screen.getByTestId("context-title-unknown-0")).toHaveTextContent("Contexto");
    expect(screen.getByTestId("context-empty-rag")).toHaveTextContent("Não utilizado");
    expect(screen.getByTestId("context-empty-config")).toHaveTextContent("Não utilizado");
  });

  it("renders the English labels and per-card empty copy", () => {
    renderViewer([]);

    expect(screen.getByText("Selected Context")).toBeInTheDocument();
    expect(screen.getByText("RAG")).toBeInTheDocument();
    expect(screen.getByText("Clinic Config")).toBeInTheDocument();
    expect(screen.getAllByText("Not used")).toHaveLength(2);
  });
});

describe("<TraceViewer /> rating tenant wiring", () => {
  it("forwards the clinicTenantId prop to RatingBlock (not any header/auth value)", () => {
    authState.isInvestor = false;
    renderViewer(undefined, "en", "trace-tenant-xyz");

    expect(ratingBlockCalls).toHaveLength(1);
    expect(ratingBlockCalls[0].clinicTenantId).toBe("trace-tenant-xyz");
    expect(screen.getByTestId("rating-block-mock")).toHaveAttribute(
      "data-clinic-tenant-id",
      "trace-tenant-xyz",
    );
  });

  it("defaults clinicTenantId to null when the prop is omitted", () => {
    authState.isInvestor = false;
    render(
      <I18nProvider>
        <TraceViewer trace={decisionTrace()} />
      </I18nProvider>,
    );

    expect(ratingBlockCalls).toHaveLength(1);
    expect(ratingBlockCalls[0].clinicTenantId).toBeNull();
  });

  it("does not render the rating block for investors", () => {
    authState.isInvestor = true;
    renderViewer(undefined, "en", "trace-tenant-xyz");

    expect(ratingBlockCalls).toHaveLength(0);
    expect(screen.queryByTestId("rating-block-mock")).not.toBeInTheDocument();
  });
});
