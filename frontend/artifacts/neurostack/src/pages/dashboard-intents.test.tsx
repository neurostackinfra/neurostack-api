import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { TranslationKey } from "@/locales/en";
import { pt } from "@/locales/pt";
import { IntentBreakdown } from "./dashboard";

vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  PieChart: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Pie: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Cell: () => null,
  Tooltip: () => null,
}));

const byIntent = {
  pedido_preco: 78,
  agendar: 64,
  interesse_tratamento: 52,
  reclamacao: 12,
  pos_atendimento_duvida: 31,
};

const t = (key: TranslationKey) => pt[key];

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

    expect(screen.getByTestId("stat-intent-reclamacao")).toHaveTextContent("12\u00b7 5,1%");
    expect(screen.getByTestId("stat-intent-pedido_preco")).toHaveTextContent("78\u00b7 32,9%");
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

    expect(screen.getByTestId("stat-intent-reclamacao")).toHaveTextContent("12\u00b7 5,1%");
    expect(screen.getByTestId("stat-intent-reclamacao")).not.toHaveTextContent("100%");
  });

  it("uses the shared palette for dots and bars", () => {
    render(
      <IntentBreakdown byIntent={byIntent} t={t} locale="pt" intentFilter="" onIntentFilterChange={vi.fn()} />,
    );
    const price = screen.getByTestId("stat-intent-pedido_preco");
    expect(price.querySelectorAll(".bg-blue-500")).toHaveLength(2);
  });
});
