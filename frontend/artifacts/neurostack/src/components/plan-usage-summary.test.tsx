import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CreditAllocationNotice,
  PlanUsageSummary,
  isCreditReservations,
  readCredit,
  type BillingUsage,
} from "./plan-usage-summary";

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

// ── Payments page variant and read states (tela-pagamentos Group 1) ─────────

const zeroUsage: BillingUsage = {
  ...usage,
  traces: { used: 0, included: 300, extra_credits: 0, available: 300, overage: 0, percent_used: 0 },
  storage: {
    rag: { ...usage.storage.rag, used_bytes: 0, used_mb: 0, available_mb: 64, limit_mb: 64, overage_bytes: 0, overage_mb: 0, percent_used: 0 },
    media: { ...usage.storage.media, used_bytes: 0, used_mb: 0, available_mb: 32, limit_mb: 32, overage_bytes: 0, overage_mb: 0, percent_used: 0 },
  },
};

describe("<PlanUsageSummary /> states", () => {
  afterEach(cleanup);

  it.each(["pt", "en"] as const)("gives loading, unavailable and error their own copy and no figure (%s)", (locale) => {
    const texts = (["loading", "unavailable", "error"] as const).map((status) => {
      const { unmount } = render(<PlanUsageSummary state={{ status }} locale={locale} variant="billing" />);
      const card = screen.getByTestId("plan-usage");
      expect(card).toHaveAttribute("data-state", status);
      const text = card.textContent ?? "";
      // No number, unit, percentage, progress bar or purchase action.
      expect(text).not.toMatch(/\d|MB|%/);
      expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
      expect(screen.queryByRole("button")).not.toBeInTheDocument();
      unmount();
      return text;
    });
    expect(new Set(texts).size).toBe(3);
  });

  it("announces loading as a polite status", () => {
    render(<PlanUsageSummary state={{ status: "loading" }} locale="pt" />);
    expect(screen.getByRole("status")).toHaveTextContent("Carregando o uso do plano");
  });

  it("shows zero usage as data, not as an empty or failed state", () => {
    render(<PlanUsageSummary state={{ status: "ready", usage: zeroUsage }} locale="pt" variant="billing" />);
    expect(screen.getByTestId("plan-usage")).toHaveAttribute("data-state", "ready");
    expect(screen.getByRole("group", { name: "Traces do mês" })).toHaveTextContent(/Usado0Limite300Disponível300/);
    expect(screen.getByRole("group", { name: "Armazenamento RAG" })).toHaveTextContent(/Usado0 MBLimite64 MB/);
    expect(screen.getByRole("group", { name: "Armazenamento de mídias" })).toHaveTextContent(/Usado0 MBLimite32 MB/);
    expect(document.body.textContent ?? "").not.toContain("—");
  });

  it("orders RAG, Traces and Media on the payments page and drops the legacy plan badge", () => {
    render(<PlanUsageSummary usage={usage} locale="en" variant="billing" />);
    expect(screen.getAllByRole("group").map((group) => group.getAttribute("aria-label"))).toEqual([
      "RAG storage",
      "Monthly traces",
      "Media storage",
    ]);
    expect(screen.queryByText("builder_pro")).not.toBeInTheDocument();
    expect(screen.queryByText(/No plan/)).not.toBeInTheDocument();
  });

  it("has no dead purchase button or coming-soon badge on the payments page while top-ups are closed", () => {
    render(<PlanUsageSummary usage={usage} locale="pt" variant="billing" />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByText("Em breve")).not.toBeInTheDocument();
    cleanup();

    const onBuy = vi.fn();
    render(
      <PlanUsageSummary usage={usage} locale="pt" variant="billing" creditPurchase={{ enabled: true, onBuy }} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /comprar créditos/i }));
    expect(onBuy).toHaveBeenCalledTimes(1);
  });

  it("keeps the dashboard card unchanged by default", () => {
    render(<PlanUsageSummary usage={usage} locale="pt" />);
    expect(screen.getAllByRole("group").map((group) => group.getAttribute("aria-label"))).toEqual([
      "Traces do mês",
      "Armazenamento RAG",
      "Armazenamento de mídias",
    ]);
    expect(screen.getByText("builder_pro")).toBeInTheDocument();
  });

  // The dashboard (default) keeps its pre-payments-page markup: truncating
  // meter labels and no test attributes on the card; only the payments page
  // wraps the labels and carries `data-testid`/`data-state`.
  it("keeps truncating labels and no data attributes on the dashboard card; the billing card wraps and has both", () => {
    const LABELS = ["Traces do mês", "Armazenamento RAG", "Armazenamento de mídias"];
    const { container, unmount } = render(<PlanUsageSummary usage={usage} locale="pt" />);
    const dashboardCard = container.firstElementChild as HTMLElement;
    expect(dashboardCard).not.toHaveAttribute("data-testid");
    expect(dashboardCard).not.toHaveAttribute("data-state");
    expect(container.querySelector("[data-testid]")).toBeNull();
    expect(screen.queryByTestId("plan-usage")).not.toBeInTheDocument();
    for (const label of LABELS) {
      const text = screen.getByText(label, { selector: "p" });
      expect(text).toHaveClass("truncate");
      expect(text).not.toHaveClass("break-words");
    }
    unmount();

    render(<PlanUsageSummary usage={usage} locale="pt" variant="billing" />);
    const billingCard = screen.getByTestId("plan-usage");
    expect(billingCard).toHaveAttribute("data-state", "ready");
    for (const label of LABELS) {
      const text = screen.getByText(label, { selector: "p" });
      expect(text).toHaveClass("break-words");
      expect(text).not.toHaveClass("truncate");
    }
  });

  it("puts no data attributes on a dashboard state card; the billing state card keeps them", () => {
    const { container, unmount } = render(<PlanUsageSummary state={{ status: "error" }} locale="pt" />);
    expect(container.firstElementChild).not.toHaveAttribute("data-testid");
    expect(container.firstElementChild).not.toHaveAttribute("data-state");
    unmount();
    render(<PlanUsageSummary state={{ status: "error" }} locale="pt" variant="billing" />);
    expect(screen.getByTestId("plan-usage")).toHaveAttribute("data-state", "error");
  });
});

// ── Credits (neurostack-credit-system Group 6) ──────────────────────────────

describe("<PlanUsageSummary /> credit purchase", () => {
  it("enables the purchase only when the page passes an enabled purchase", () => {
    const onBuy = vi.fn();
    render(
      <PlanUsageSummary
        usage={usage}
        locale="pt"
        creditPurchase={{ enabled: true, onBuy }}
      />,
    );

    const buy = screen.getByRole("button", { name: /comprar créditos/i });
    expect(buy).toBeEnabled();
    expect(screen.queryByText("Em breve")).not.toBeInTheDocument();
    fireEvent.click(buy);
    expect(onBuy).toHaveBeenCalledTimes(1);
    cleanup();

    render(
      <PlanUsageSummary
        usage={usage}
        locale="pt"
        creditPurchase={{ enabled: false, onBuy }}
      />,
    );
    expect(screen.getByRole("button", { name: /comprar créditos/i })).toBeDisabled();
    expect(screen.getByText("Em breve")).toBeInTheDocument();
  });
});

const reservations = {
  as_of: "2026-10-30T15:00:00Z",
  window: { start: "2026-10-25", end: "2026-11-25", kind: "cycle" },
  reservations: [
    { category: "rag", units: 15_000, coverage_mc: 3_000, charged_mc: 2_000 },
    { category: "media", units: 40_000_000, coverage_mc: 2_500, charged_mc: 500 },
    { category: "egress", units: 0, coverage_mc: 0, charged_mc: 0 },
  ],
  egress_allowance_bytes: 500_000_000,
  egress_delivered_bytes: 12_000_000,
};

function reply(data: unknown, status = 200): Response {
  return { ok: status === 200, status, json: async () => data } as Response;
}

describe("credit reads", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it.each([
    [503, { code: "credit_unavailable" }],
    [403, { detail: { code: "credit_scope_forbidden" } }],
    [500, { error: "internal_server_error" }],
    [200, { reservations: "not a list" }],
  ])("hides a credit read answered %s", async (status, body) => {
    vi.stubGlobal("fetch", vi.fn(async () => reply(body, status)));
    expect(await readCredit("/v1/credits/reservations", "t", isCreditReservations)).toEqual({
      status: "hidden",
    });
  });

  it("hides a credit read whose request fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("network");
      }),
    );
    expect(await readCredit("/v1/credits/reservations", "t", isCreditReservations)).toEqual({
      status: "hidden",
    });
  });

  it("shows a category's allocation only when the server answers", async () => {
    const fetchMock = vi.fn(async () => reply(reservations));
    vi.stubGlobal("fetch", fetchMock);
    localStorage.setItem("ns_auth", JSON.stringify({ token: "builder-token" }));
    render(
      <CreditAllocationNotice category="media" locale="pt" tenantId="clinic-9" enabled />,
    );

    const notice = await screen.findByTestId("credit-allocation-media");
    expect(notice).toHaveTextContent(/Alocado no ciclo:\s*2,5 créditos/);
    expect(notice).toHaveTextContent(/Debitado:\s*0,5 créditos/);
    expect(notice).toHaveTextContent(/Downloads incluídos:\s*12 \/ 500 MB/);
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/credits/reservations?tenant_id=clinic-9",
      expect.objectContaining({ headers: { Authorization: "Bearer builder-token" } }),
    );
  });

  it("renders nothing and reads nothing while disabled, and nothing on 503", async () => {
    const fetchMock = vi.fn(async () => reply({ code: "credit_unavailable" }, 503));
    vi.stubGlobal("fetch", fetchMock);
    const { container, rerender } = render(
      <CreditAllocationNotice category="rag" locale="pt" tenantId="clinic-9" enabled={false} />,
    );
    expect(container).toBeEmptyDOMElement();
    expect(fetchMock).not.toHaveBeenCalled();

    rerender(
      <CreditAllocationNotice category="rag" locale="pt" tenantId="clinic-9" enabled />,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(container).toBeEmptyDOMElement();
  });
});
