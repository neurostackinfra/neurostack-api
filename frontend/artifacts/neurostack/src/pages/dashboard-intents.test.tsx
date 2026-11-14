import { cleanup, render, screen } from "@testing-library/react";
import { cloneElement, type ReactElement, type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { TranslationKey } from "@/locales/en";
import { en } from "@/locales/en";
import { pt } from "@/locales/pt";
import { DonutTooltip, INVESTOR_MOCK_STATS, IntentBreakdown, UserAcquisitionMetrics } from "./dashboard";

type ChartDatum = { name: string; value: number; total: number };

// The mock records what each donut hands to recharts, so a test can feed a
// slice back into the tooltip element exactly as recharts would.
const charts = vi.hoisted(() => ({
  pieData: [] as unknown[],
  tooltips: [] as unknown[],
}));

vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  PieChart: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Pie: ({ children, data }: { children: ReactNode; data: unknown }) => {
    charts.pieData.push(data);
    return <div>{children}</div>;
  },
  Cell: () => null,
  Tooltip: ({ content }: { content: unknown }) => {
    charts.tooltips.push(content);
    return null;
  },
}));

beforeEach(() => {
  charts.pieData.length = 0;
  charts.tooltips.length = 0;
});

/** Renders the last donut's tooltip for one of its slices, as recharts would on hover. */
function hoverSlice(name: string) {
  const data = charts.pieData.at(-1) as ChartDatum[];
  const datum = data.find((d) => d.name === name);
  if (!datum) throw new Error(`no slice ${name}`);
  const content = charts.tooltips.at(-1) as ReactElement<{ active?: boolean; payload?: unknown }>;
  cleanup();
  return render(cloneElement(content, { active: true, payload: [{ name: datum.name, value: datum.value, payload: datum }] }));
}

// Fixture spans raw keys inside and outside the 5 commercial buckets so the
// aggregation path is exercised (saudacao + duvida_generica_sem_contexto
// bucket into "outras"). Totals: 78 + 64 + 52 + 12 + 31 + 50 + 13 = 300.
const byIntent = {
  pedido_preco: 78,
  agendar: 64,
  interesse_tratamento: 52,
  reclamacao: 12,
  pos_atendimento_duvida: 31,
  saudacao: 50,
  duvida_generica_sem_contexto: 13,
};

const t = (key: TranslationKey) => pt[key];
const tEn = (key: TranslationKey) => en[key];

describe("<IntentBreakdown />", () => {
  it("shows each count together with its percentage of the period total", () => {
    render(
      <IntentBreakdown
        byIntent={byIntent}
        t={t}
        locale="pt"
        intentFilter=""
        onIntentFilterChange={vi.fn()}
      />,
    );

    // reclamacao = 12/300 = 4.0%; pedido_preco = 78/300 = 26.0%
    expect(screen.getByTestId("stat-intent-reclamacao")).toHaveTextContent("12· 4%");
    expect(screen.getByTestId("stat-intent-pedido_preco")).toHaveTextContent("78· 26%");
  });

  it("keeps the period percentage when a single intention is selected", () => {
    render(
      <IntentBreakdown
        byIntent={byIntent}
        t={t}
        locale="pt"
        intentFilter="reclamacao"
        onIntentFilterChange={vi.fn()}
      />,
    );

    expect(screen.getByTestId("stat-intent-reclamacao")).toHaveTextContent("12· 4%");
    expect(screen.getByTestId("stat-intent-reclamacao")).not.toHaveTextContent("100%");
  });

  it("uses the shared palette for dots and bars", () => {
    render(
      <IntentBreakdown byIntent={byIntent} t={t} locale="pt" intentFilter="" onIntentFilterChange={vi.fn()} />,
    );
    const price = screen.getByTestId("stat-intent-pedido_preco");
    expect(price.querySelectorAll(".bg-blue-500")).toHaveLength(2);
  });

  // -- C4 --
  it("aggregates every raw key into a bucket so nothing is dropped", () => {
    render(
      <IntentBreakdown byIntent={byIntent} t={t} locale="pt" intentFilter="" onIntentFilterChange={vi.fn()} />,
    );
    const fixtureTotal = Object.values(byIntent).reduce((a, b) => a + b, 0);
    const bucketKeys = [
      "pedido_preco",
      "agendar",
      "interesse_tratamento",
      "reclamacao",
      "pos_atendimento_duvida",
      "outras",
    ] as const;
    // sum of the leading number in each rendered bar row
    const renderedSum = bucketKeys.reduce((sum, bucket) => {
      const bar = screen.getByTestId(`stat-intent-${bucket}`);
      const match = bar.textContent?.match(/(\d+)/);
      return sum + (match ? Number(match[1]) : 0);
    }, 0);
    expect(renderedSum).toBe(fixtureTotal);
    // outras absorbs 50 + 13 = 63
    expect(screen.getByTestId("stat-intent-outras").textContent).toMatch(/63/);
  });

  // -- C7 --
  it("renders the investor mock stats with outras > 0, agendar bucket = 73, total = 291", () => {
    render(
      <IntentBreakdown
        byIntent={INVESTOR_MOCK_STATS.byIntent}
        t={t}
        locale="pt"
        intentFilter=""
        onIntentFilterChange={vi.fn()}
      />,
    );
    const fixtureTotal = Object.values(INVESTOR_MOCK_STATS.byIntent).reduce((a, b) => a + b, 0);
    expect(fixtureTotal).toBe(291);

    const readCount = (bucket: string) => {
      const bar = screen.getByTestId(`stat-intent-${bucket}`);
      const match = bar.textContent?.match(/(\d+)/);
      return match ? Number(match[1]) : 0;
    };
    expect(readCount("agendar")).toBe(73);
    expect(readCount("outras")).toBeGreaterThan(0);

    const bucketKeys = [
      "pedido_preco",
      "agendar",
      "interesse_tratamento",
      "reclamacao",
      "pos_atendimento_duvida",
      "outras",
    ];
    const sum = bucketKeys.reduce((s, k) => s + readCount(k), 0);
    expect(sum).toBe(291);
  });

  // -- C9 --
  it("shows 6 select options and 6 legend entries including Outras", () => {
    const { container } = render(
      <IntentBreakdown byIntent={byIntent} t={t} locale="pt" intentFilter="" onIntentFilterChange={vi.fn()} />,
    );
    const options = container.querySelectorAll("select option");
    // 1 "all" option + 6 bucket options
    expect(options).toHaveLength(7);
    // 6 real buckets, no raw keys
    const bucketValues = Array.from(options).map(o => (o as HTMLOptionElement).value).filter(v => v !== "");
    expect(bucketValues).toEqual([
      "pedido_preco",
      "agendar",
      "interesse_tratamento",
      "reclamacao",
      "pos_atendimento_duvida",
      "outras",
    ]);
    // Legend items sit alongside the donut; scope by the small [10px] labels
    const legendLabels = Array.from(container.querySelectorAll(".text-\\[10px\\].text-muted-foreground"));
    // filter to the wrap-around legend entries (not the donut center label which is uppercase)
    const legendTexts = legendLabels
      .map(el => el.textContent?.trim() ?? "")
      .filter(text => text.length > 0);
    // The donut center adds one uppercase entry ("TOTAL"), plus 6 legend entries
    expect(legendTexts).toContain("Outras");
    // 6 bucket-legend entries + 1 donut-center label = 7 total
    expect(legendTexts).toHaveLength(7);
  });

  // -- C10 --
  it("renders exactly 6 stat-intent bars, one per bucket, never per raw key", () => {
    const { container } = render(
      <IntentBreakdown byIntent={byIntent} t={t} locale="pt" intentFilter="" onIntentFilterChange={vi.fn()} />,
    );
    const bars = container.querySelectorAll('[data-testid^="stat-intent-"]');
    expect(bars).toHaveLength(6);
    const testids = Array.from(bars).map(el => el.getAttribute("data-testid"));
    expect(testids).toContain("stat-intent-outras");
    // No raw key ever bleeds through
    expect(testids).not.toContain("stat-intent-saudacao");
    expect(testids).not.toContain("stat-intent-duvida_generica_sem_contexto");
    expect(testids).not.toContain("stat-intent-cancelar");
  });

  // -- C12 --
  it("shows the bucket label in the donut center when filtering by outras", () => {
    render(
      <IntentBreakdown
        byIntent={byIntent}
        t={t}
        locale="pt"
        intentFilter="outras"
        onIntentFilterChange={vi.fn()}
      />,
    );
    // pt label for outras is "Outras"
    expect(screen.getAllByText("Outras").length).toBeGreaterThan(0);
    // and never the raw key
    expect(screen.queryByText("saudacao")).toBeNull();
  });

  it("shows the English bucket label in the donut center when filtering by outras (en)", () => {
    render(
      <IntentBreakdown
        byIntent={byIntent}
        t={tEn}
        locale="en"
        intentFilter="outras"
        onIntentFilterChange={vi.fn()}
      />,
    );
    expect(screen.getAllByText("Other").length).toBeGreaterThan(0);
  });
});

describe("<IntentBreakdown /> honest number", () => {
  it("shows the fraction after the count below 20 messages", () => {
    render(
      <IntentBreakdown byIntent={{ pedido_preco: 3, saudacao: 7 }} t={t} locale="pt" intentFilter="" onIntentFilterChange={vi.fn()} />,
    );
    expect(screen.getByTestId("stat-intent-pedido_preco")).toHaveTextContent("3· 3 de 10");
    expect(screen.getByTestId("stat-intent-outras")).toHaveTextContent("7· 7 de 10");
    expect(screen.getByTestId("stat-intent-agendar")).toHaveTextContent("0· 0 de 10");
  });

  it("uses the English fraction", () => {
    render(
      <IntentBreakdown byIntent={{ pedido_preco: 3, saudacao: 7 }} t={tEn} locale="en" intentFilter="" onIntentFilterChange={vi.fn()} />,
    );
    expect(screen.getByTestId("stat-intent-pedido_preco")).toHaveTextContent("3· 3 of 10");
  });

  it("keeps today's 0· 0% rows when the period has no messages", () => {
    render(<IntentBreakdown byIntent={{}} t={t} locale="pt" intentFilter="" onIntentFilterChange={vi.fn()} />);
    expect(screen.getByTestId("stat-intent-pedido_preco")).toHaveTextContent("0· 0%");
    expect(screen.getByTestId("stat-intent-outras")).toHaveTextContent("0· 0%");
  });

  it("shows value and share in the intent donut tooltip", () => {
    render(<IntentBreakdown byIntent={byIntent} t={t} locale="pt" intentFilter="" onIntentFilterChange={vi.fn()} />);
    hoverSlice(pt.intent_reclamacao);
    expect(screen.getByText("12 · 4%")).toBeInTheDocument();
  });

  it("shows the fraction in the intent donut tooltip below 20 messages", () => {
    render(
      <IntentBreakdown byIntent={{ pedido_preco: 3, saudacao: 7 }} t={tEn} locale="en" intentFilter="" onIntentFilterChange={vi.fn()} />,
    );
    hoverSlice(en.intent_pedido_preco);
    expect(screen.getByText("3 · 3 of 10")).toBeInTheDocument();
  });
});

describe("<UserAcquisitionMetrics /> honest number", () => {
  const audience = (overrides: Partial<typeof INVESTOR_MOCK_STATS>) => ({ ...INVESTOR_MOCK_STATS, ...overrides });

  it("shows the legend percent from interaction counts", () => {
    render(
      <UserAcquisitionMetrics
        stats={audience({
          newUserInteractions: 116,
          returningUserInteractions: 175,
          identifiedUserInteractions: 291,
        })}
        locale="pt"
      />,
    );
    // 116 / 291 = 39.86% and 175 / 291 = 60.14%
    expect(screen.getByTestId("audience-new")).toHaveTextContent("40%");
    expect(screen.getByTestId("audience-returning")).toHaveTextContent("60%");
  });

  it("shows the fraction below 20 identified interactions", () => {
    render(
      <UserAcquisitionMetrics
        stats={audience({ newUserInteractions: 3, returningUserInteractions: 7, identifiedUserInteractions: 10 })}
        locale="pt"
      />,
    );
    expect(screen.getByTestId("audience-new")).toHaveTextContent("3 de 10");
    expect(screen.getByTestId("audience-returning")).toHaveTextContent("7 de 10");
  });

  it("keeps today's dash with no identified interactions", () => {
    render(
      <UserAcquisitionMetrics
        stats={audience({ newUserInteractions: 0, returningUserInteractions: 0, identifiedUserInteractions: 0 })}
        locale="pt"
      />,
    );
    expect(screen.getByTestId("audience-new")).toHaveTextContent("\u2014");
    expect(screen.getByTestId("audience-returning")).toHaveTextContent("\u2014");
  });

  it("shows value and share in the users donut tooltip", () => {
    render(
      <UserAcquisitionMetrics
        stats={audience({ newUserInteractions: 3, returningUserInteractions: 7, identifiedUserInteractions: 10 })}
        locale="en"
      />,
    );
    hoverSlice("Returning users");
    expect(screen.getByText("7 · 7 of 10")).toBeInTheDocument();
  });

  it("renders the investor mock through the same path", () => {
    render(<UserAcquisitionMetrics stats={INVESTOR_MOCK_STATS} locale="pt" />);
    expect(screen.getByTestId("audience-new")).toHaveTextContent("40%");
  });
});

describe("<DonutTooltip />", () => {
  it.each([
    { value: 12, total: 300, locale: "pt", text: "12 · 4%" },
    { value: 3, total: 10, locale: "pt", text: "3 · 3 de 10" },
    { value: 1, total: 300, locale: "en", text: "1 · <1%" },
  ])("shows $text", ({ value, total, locale, text }) => {
    render(<DonutTooltip active locale={locale} payload={[{ name: "Slice", value, payload: { total } }]} />);
    expect(screen.getByText(text)).toBeInTheDocument();
  });

  it("renders nothing when inactive", () => {
    const { container } = render(<DonutTooltip locale="pt" payload={[{ name: "Slice", value: 1, payload: { total: 1 } }]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
