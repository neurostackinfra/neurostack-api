import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { TranslationKey } from "@/locales/en";
import { en } from "@/locales/en";
import { pt } from "@/locales/pt";
import { INVESTOR_MOCK_STATS, IntentBreakdown } from "./dashboard";

vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  PieChart: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Pie: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Cell: () => null,
  Tooltip: () => null,
}));

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
