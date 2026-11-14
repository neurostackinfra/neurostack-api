import { render, screen, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { describe, expect, it } from "vitest";

import type { TranslationKey } from "@/locales/en";
import { en } from "@/locales/en";
import { pt } from "@/locales/pt";
import { KpiCards } from "./dashboard";

const stats = {
  total: 10,
  avgProcessingTimeMs: 120,
  safetyFlagCount: 0,
  handoffCount: 1,
  totalTokensSum: 500,
  modelBreakdown: [],
  byIntent: {},
  outsideHoursCount: 2,
  conversationalTraceCount: 10,
  businessHoursConfigured: true,
  newUsersCount: 0,
  returningUsersCount: 0,
  newUserInteractions: 0,
  returningUserInteractions: 0,
  identifiedUserInteractions: 0,
  unidentifiedUserInteractions: 0,
};

describe("<KpiCards />", () => {
  it("shows the full outside-hours title in Portuguese", () => {
    render(<KpiCards stats={stats} t={(key: TranslationKey) => pt[key]} locale="pt" />);

    expect(within(screen.getByTestId("stat-outside-hours")).getByText("Atendimentos fora do horário")).toBeInTheDocument();
  });

  it("keeps the existing outside-hours title in English", () => {
    render(<KpiCards stats={stats} t={(key: TranslationKey) => en[key]} locale="en" />);

    expect(within(screen.getByTestId("stat-outside-hours")).getByText("Outside hours")).toBeInTheDocument();
  });
});

describe("<KpiCards /> outside-hours honest number", () => {
  const renderKpi = (overrides: Partial<ComponentProps<typeof KpiCards>["stats"]>, locale: "pt" | "en" = "pt") =>
    render(
      <KpiCards
        stats={{ ...stats, ...overrides }}
        t={(key: TranslationKey) => (locale === "en" ? en : pt)[key]}
        locale={locale}
      />,
    );
  const kpi = () => screen.getByTestId("stat-outside-hours");

  it("shows the fraction below 20 messages, with the count line", () => {
    renderKpi({ outsideHoursCount: 3, conversationalTraceCount: 10 });

    expect(screen.getByTestId("stat-outside-hours-value")).toHaveTextContent(/^3 de 10$/);
    expect(kpi()).toHaveTextContent("3 / 10 mensagens processadas pela IA");
  });

  it("shows the whole percentage from 20 messages, computed from counts", () => {
    renderKpi({ outsideHoursCount: 30, conversationalTraceCount: 100 });

    expect(screen.getByTestId("stat-outside-hours-value")).toHaveTextContent(/^30%$/);
  });

  it("uses the English fraction and label", () => {
    renderKpi({ outsideHoursCount: 3, conversationalTraceCount: 10 }, "en");

    expect(screen.getByTestId("stat-outside-hours-value")).toHaveTextContent(/^3 of 10$/);
    expect(kpi()).toHaveTextContent("3 / 10 messages processed by the AI");
  });

  it("keeps today's setup prompt when business hours are not configured", () => {
    renderKpi({ businessHoursConfigured: false });

    expect(within(kpi()).getByText("Configure o horário")).toBeInTheDocument();
    expect(screen.queryByTestId("stat-outside-hours-value")).not.toBeInTheDocument();
  });

  it("keeps today's no-data text when configured with no messages", () => {
    renderKpi({ outsideHoursCount: 0, conversationalTraceCount: 0 });

    expect(within(kpi()).getByText("Sem dados")).toBeInTheDocument();
    expect(screen.queryByTestId("stat-outside-hours-value")).not.toBeInTheDocument();
  });

  it.each(["pt", "en"] as const)("never says conversas or conversations (%s)", (locale) => {
    renderKpi({}, locale);

    expect(kpi().textContent?.toLowerCase()).not.toMatch(/conversas|conversations/);
  });
});
