import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PlanUsageSummary, type BillingUsage } from "./plan-usage-summary";

const usage: BillingUsage = {
  plan: "builder_pro",
  as_of: "2026-08-10T18:30:00Z",
  trace_period: {
    start: "2026-08-01T00:00:00Z",
    end: "2026-09-01T00:00:00Z",
    timezone: "UTC",
  },
  traces: {
    used: 12_500,
    included: 10_000,
    extra_credits: 0,
    available: 0,
    overage: 2_500,
    percent_used: 125,
  },
  storage: {
    rag: {
      used_bytes: 268_959_744,
      used_mb: 256.5,
      limit_bytes: 536_870_912,
      limit_mb: 512,
      available_bytes: 267_911_168,
      available_mb: 255.5,
      overage_bytes: 0,
      overage_mb: 0,
      percent_used: 50.1,
    },
    media: {
      used_bytes: 786_432_000,
      used_mb: 750,
      limit_bytes: 524_288_000,
      limit_mb: 500,
      available_bytes: 0,
      available_mb: 0,
      overage_bytes: 262_144_000,
      overage_mb: 250,
      percent_used: 150,
    },
  },
  credits: {
    purchase_enabled: false,
    coming_soon: true,
  },
};

describe("<PlanUsageSummary />", () => {
  it("shows the explicit monthly period and usage, limits, availability, and overages", () => {
    render(<PlanUsageSummary usage={usage} locale="pt" />);

    expect(
      screen.getByText(/01\/08\/2026 a 31\/08\/2026\s*·\s*UTC/i),
    ).toBeInTheDocument();

    const traces = screen.getByRole("group", { name: "Traces do mês" });
    expect(traces).toHaveTextContent(/usad[oa]s?\s*12\.500/i);
    expect(traces).toHaveTextContent(/(?:incluíd[oa]s?|limite)\s*10\.000/i);
    expect(traces).toHaveTextContent(/disponíve(?:l|is)\s*0/i);
    expect(traces).toHaveTextContent(/(?:exced\w*|acima do limite)\s*2\.500/i);

    const rag = screen.getByRole("group", { name: "Armazenamento RAG" });
    expect(rag).toHaveTextContent(/usad[oa]\s*256,5\s*MB/i);
    expect(rag).toHaveTextContent(/limite\s*512\s*MB/i);
    expect(rag).toHaveTextContent(/disponíve(?:l|is)\s*255,5\s*MB/i);
    expect(rag).not.toHaveTextContent(/exced|acima do limite/i);

    const media = screen.getByRole("group", {
      name: "Armazenamento de mídias",
    });
    expect(media).toHaveTextContent(/usad[oa]\s*750\s*MB/i);
    expect(media).toHaveTextContent(/limite\s*500\s*MB/i);
    expect(media).toHaveTextContent(/disponíve(?:l|is)\s*0\s*MB/i);
    expect(media).toHaveTextContent(/(?:exced\w*|acima do limite)\s*250\s*MB/i);
  });

  it("clamps progressbar values to 100 when usage exceeds a limit", () => {
    render(<PlanUsageSummary usage={usage} locale="pt" />);

    expect(
      screen.getByRole("progressbar", { name: "Traces do mês" }),
    ).toHaveAttribute("aria-valuenow", "100");
    expect(
      screen.getByRole("progressbar", { name: "Armazenamento RAG" }),
    ).toHaveAttribute("aria-valuenow", "50.1");
    expect(
      screen.getByRole("progressbar", { name: "Armazenamento de mídias" }),
    ).toHaveAttribute("aria-valuenow", "100");
  });

  it("adds extra credits to the displayed trace limit", () => {
    const usageWithExtraCredits: BillingUsage = {
      ...usage,
      traces: {
        ...usage.traces,
        used: 10_500,
        extra_credits: 2_000,
        available: 1_500,
        overage: 0,
        percent_used: 87.5,
      },
    };

    render(<PlanUsageSummary usage={usageWithExtraCredits} locale="pt" />);

    const traces = screen.getByRole("group", { name: "Traces do mês" });
    expect(traces).toHaveTextContent(/limite\s*12\.000/i);
    expect(traces).toHaveTextContent(
      /10\.000 incluídos no plano \+ 2\.000 créditos extras/i,
    );
    expect(traces).not.toHaveTextContent(/excedente/i);
  });

  it("keeps credit purchases disabled and marks them as coming soon", () => {
    render(<PlanUsageSummary usage={usage} locale="pt" />);

    expect(
      screen.getByRole("button", { name: /comprar créditos/i }),
    ).toBeDisabled();
    expect(screen.getByText("Em breve")).toBeInTheDocument();
  });
});
