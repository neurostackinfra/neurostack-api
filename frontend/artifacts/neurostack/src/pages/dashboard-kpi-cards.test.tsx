import { render, screen, within } from "@testing-library/react";
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
  outsideHoursPct: 20,
  conversationalTraceCount: 10,
  businessHoursConfigured: true,
  newUsersCount: 0,
  returningUsersCount: 0,
  newUserInteractions: 0,
  returningUserInteractions: 0,
  newUserInteractionPct: null,
  returningUserInteractionPct: null,
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
