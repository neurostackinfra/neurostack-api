import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { I18nProvider } from "@/context/i18n";
import { CONSULTORIA_COPY } from "@/lib/consultoria-insights";
import Consultoria from "./consultoria";
import pageSource from "./consultoria.tsx?raw";

const mocks = vi.hoisted(() => ({
  auth: {
    tenantId: "clinic-id" as string,
    isBuilder: false,
    isSuperAdmin: false,
    isInvestor: false,
    viewingClinicId: "" as string,
  },
  fetch: vi.fn(),
}));

vi.mock("@/context/auth", () => ({
  useAuth: () => mocks.auth,
}));

// Local 2026-05-01, so the default period is 2026-04-01 -> 2026-05-01.
const TODAY = "2026-05-01";
const THIRTY_DAYS_AGO = "2026-04-01";

const STATS = {
  total_traces: 140,
  business_hours_configured: true,
  outside_hours_count: 30,
  outside_hours_pct: 99,
  conversational_trace_count: 100,
  new_users_count: 7,
  returning_users_count: 5,
  intent_distribution: { pedido_preco: 9, agendar: 4, saudacao: 2 },
};

const QUEUE = {
  items: [],
  avg_wait_seconds: 754,
  pct_within_sla: 61.3,
  within_sla_count: 20,
  confirmed_late_count: 3,
  unanswered_overdue_count: 2,
  maturing_count: 0,
  cancelled_unconfirmed_count: 0,
  unmeasured_legacy_count: 0,
  confirmed_count: 23,
  sla_eligible_count: 25,
};

type Reply = { status: number; body?: unknown };

let statsReply: Reply;
let queueReply: Reply;
let queryClient: QueryClient;
let unexpectedFetches: string[];

function jsonResponse({ status, body }: Reply): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

function calls(path: string): URL[] {
  return mocks.fetch.mock.calls
    .map(([input]) => new URL(String(input), "http://localhost"))
    .filter((url) => url.pathname === path);
}

function renderPage(locale: "pt" | "en" = "pt") {
  localStorage.setItem("ns_locale", locale);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <I18nProvider>{children}</I18nProvider>
    </QueryClientProvider>
  );
  return render(<Consultoria />, { wrapper });
}

function card(id: string) {
  return screen.getByTestId(`insight-${id}`);
}

async function waitForReady() {
  await waitFor(() => {
    for (const id of ["outside_hours", "new_contacts", "top_intents", "team_sla"]) {
      expect(card(id)).not.toHaveAttribute("data-state", "loading");
    }
  });
}

beforeEach(() => {
  vi.setSystemTime(new Date(2026, 4, 1, 10, 0, 0));
  localStorage.clear();
  localStorage.setItem("ns_auth", JSON.stringify({ token: "test-token" }));
  Object.assign(mocks.auth, {
    tenantId: "clinic-id",
    isBuilder: false,
    isSuperAdmin: false,
    isInvestor: false,
    viewingClinicId: "",
  });
  statsReply = { status: 200, body: STATS };
  queueReply = { status: 200, body: QUEUE };
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  unexpectedFetches = [];
  mocks.fetch.mockReset();
  mocks.fetch.mockImplementation(async (input: string | URL | Request) => {
    const url = new URL(String(input), "http://localhost");
    if (url.pathname === "/v1/traces/stats") return jsonResponse(statsReply);
    if (url.pathname === "/v1/handoffs/queue") return jsonResponse(queueReply);
    // Recorded and checked in afterEach: a throw alone would only become a query error.
    unexpectedFetches.push(String(input));
    throw new Error(`Unexpected fetch: ${url}`);
  });
  vi.stubGlobal("fetch", mocks.fetch);
});

afterEach(() => {
  expect(unexpectedFetches, "fetch outside the two Consultoria endpoints").toEqual([]);
  cleanup();
  queryClient.clear();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("<Consultoria /> requests", () => {
  it("asks both endpoints for today - 30 days -> today on first render", async () => {
    renderPage();
    await waitForReady();

    const [stats] = calls("/v1/traces/stats");
    const [queue] = calls("/v1/handoffs/queue");
    expect(stats.searchParams.get("start_date")).toBe(THIRTY_DAYS_AGO);
    expect(stats.searchParams.get("end_date")).toBe(TODAY);
    // A clinic login must not name the tenant on stats (the backend answers 400).
    expect(stats.searchParams.has("tenant_id")).toBe(false);
    expect(queue.searchParams.get("from_date")).toBe(THIRTY_DAYS_AGO);
    expect(queue.searchParams.get("to_date")).toBe(TODAY);
    expect(queue.searchParams.get("tenant_id")).toBe("clinic-id");
    expect(mocks.fetch.mock.calls[0][1]).toEqual({ headers: { Authorization: "Bearer test-token" } });
  });

  it("uses the consultoria query keys", async () => {
    renderPage();
    await waitForReady();

    const keys = queryClient.getQueryCache().getAll().map((query) => query.queryKey);
    expect(keys).toEqual(
      expect.arrayContaining([
        ["consultoria", "trace-stats", THIRTY_DAYS_AGO, TODAY, "clinic-id"],
        ["consultoria", "handoff-queue", THIRTY_DAYS_AGO, TODAY, "clinic-id"],
      ]),
    );
    expect(keys).toHaveLength(2);
  });

  it("refetches both with the new dates when the picker changes", async () => {
    const { container } = renderPage();
    await waitForReady();

    const [start] = Array.from(container.querySelectorAll<HTMLInputElement>('input[type="date"]'));
    fireEvent.change(start, { target: { value: "2026-03-15" } });

    await waitFor(() => {
      expect(calls("/v1/traces/stats").at(-1)?.searchParams.get("start_date")).toBe("2026-03-15");
      expect(calls("/v1/handoffs/queue").at(-1)?.searchParams.get("from_date")).toBe("2026-03-15");
    });
    expect(calls("/v1/traces/stats").at(-1)?.searchParams.get("end_date")).toBe(TODAY);
    expect(calls("/v1/handoffs/queue").at(-1)?.searchParams.get("to_date")).toBe(TODAY);
  });

  it("caps the picker at 90 days", () => {
    const { container } = renderPage();
    const [start, end] = Array.from(container.querySelectorAll<HTMLInputElement>('input[type="date"]'));
    expect(start).toHaveAttribute("min", "2026-01-31");
    expect(end).toHaveAttribute("max", TODAY);
  });

  it("sends the selected clinic for a builder on both endpoints", async () => {
    Object.assign(mocks.auth, { isBuilder: true, tenantId: "builder-id", viewingClinicId: "managed-clinic" });
    renderPage();
    await waitForReady();

    expect(calls("/v1/traces/stats")[0].searchParams.get("tenant_id")).toBe("managed-clinic");
    expect(calls("/v1/handoffs/queue")[0].searchParams.get("tenant_id")).toBe("managed-clinic");
  });

  it("sends nothing and shows the period message on all 4 cards when the start is cleared", async () => {
    const { container } = renderPage();
    await waitForReady();
    mocks.fetch.mockClear();

    const [start] = Array.from(container.querySelectorAll<HTMLInputElement>('input[type="date"]'));
    fireEvent.change(start, { target: { value: "" } });

    for (const id of ["outside_hours", "new_contacts", "top_intents", "team_sla"]) {
      expect(card(id)).toHaveAttribute("data-state", "unavailable");
      expect(card(id)).toHaveTextContent(CONSULTORIA_COPY.pt.unavailable.period);
    }
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(mocks.fetch).not.toHaveBeenCalled();
  });

  it("fetches each endpoint once on failure, even when the client would retry", async () => {
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: 3, retryDelay: 0 } } });
    statsReply = { status: 500 };
    queueReply = { status: 400, body: { detail: "invalid_date_range" } };
    renderPage();
    await waitForReady();
    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(calls("/v1/traces/stats")).toHaveLength(1);
    expect(calls("/v1/handoffs/queue")).toHaveLength(1);
  });

  it("sends the selected clinic for a super admin on both endpoints", async () => {
    Object.assign(mocks.auth, { isSuperAdmin: true, tenantId: "admin-id", viewingClinicId: "picked-clinic" });
    renderPage();
    await waitForReady();

    expect(calls("/v1/traces/stats")[0].searchParams.get("tenant_id")).toBe("picked-clinic");
    expect(calls("/v1/handoffs/queue")[0].searchParams.get("tenant_id")).toBe("picked-clinic");
  });

  it("shows the no-clinic message to a super admin with no clinic, without fetching", async () => {
    Object.assign(mocks.auth, { isSuperAdmin: true, tenantId: "admin-id", viewingClinicId: "" });
    renderPage();

    expect(screen.getByTestId("consultoria-no-clinic")).toHaveTextContent(CONSULTORIA_COPY.pt.page.noClinic);
    expect(screen.queryByTestId("insight-outside_hours")).not.toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(mocks.fetch).not.toHaveBeenCalled();
  });

  it("shows the skeleton and then the new clinic's numbers when a builder switches clinic", async () => {
    Object.assign(mocks.auth, { isBuilder: true, tenantId: "builder-id", viewingClinicId: "clinic-a" });
    const { rerender } = renderPage();
    await waitForReady();
    expect(card("outside_hours")).toHaveTextContent("30% (30 de 100)");
    expect(card("team_sla")).toHaveTextContent("80% (20 de 25)");

    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const base = mocks.fetch.getMockImplementation()!;
    mocks.fetch.mockImplementation(async (input: string | URL | Request) => {
      const url = new URL(String(input), "http://localhost");
      if (url.searchParams.get("tenant_id") !== "clinic-b") return base(input);
      await gate;
      if (url.pathname === "/v1/traces/stats") {
        return jsonResponse({ status: 200, body: { ...STATS, outside_hours_count: 40, conversational_trace_count: 200 } });
      }
      return jsonResponse({ status: 200, body: { ...QUEUE, within_sla_count: 30, sla_eligible_count: 40, confirmed_late_count: 10 } });
    });

    mocks.auth.viewingClinicId = "clinic-b";
    rerender(<Consultoria />);

    for (const id of ["outside_hours", "new_contacts", "top_intents", "team_sla"]) {
      expect(card(id)).toHaveAttribute("data-state", "loading");
      expect(screen.getByTestId(`insight-${id}-loading`)).toBeInTheDocument();
    }
    expect(card("outside_hours")).not.toHaveTextContent("30 de 100");
    expect(card("team_sla")).not.toHaveTextContent("20 de 25");

    release();
    await waitForReady();
    expect(card("outside_hours")).toHaveTextContent("20% (40 de 200)");
    expect(card("team_sla")).toHaveTextContent("75% (30 de 40)");
    expect(calls("/v1/traces/stats").at(-1)?.searchParams.get("tenant_id")).toBe("clinic-b");
    expect(calls("/v1/handoffs/queue").at(-1)?.searchParams.get("tenant_id")).toBe("clinic-b");
  });

  function dateInputs(container: HTMLElement) {
    return Array.from(container.querySelectorAll<HTMLInputElement>('input[type="date"]'));
  }

  function expectPeriodMessageOnAllCards() {
    for (const id of ["outside_hours", "new_contacts", "top_intents", "team_sla"]) {
      expect(card(id)).toHaveAttribute("data-state", "unavailable");
      expect(card(id)).toHaveTextContent(CONSULTORIA_COPY.pt.unavailable.period);
    }
  }

  it("sends nothing and shows the period message on all 4 cards when the end is before the start", async () => {
    const { container } = renderPage();
    await waitForReady();
    mocks.fetch.mockClear();

    const [, end] = dateInputs(container);
    fireEvent.change(end, { target: { value: "2026-03-01" } });

    expectPeriodMessageOnAllCards();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(mocks.fetch).not.toHaveBeenCalled();
  });

  it("sends both requests for a span of exactly 90 days", async () => {
    const { container } = renderPage();
    await waitForReady();
    mocks.fetch.mockClear();

    const [start] = dateInputs(container);
    fireEvent.change(start, { target: { value: "2026-01-31" } });

    await waitFor(() => {
      expect(calls("/v1/traces/stats").at(-1)?.searchParams.get("start_date")).toBe("2026-01-31");
      expect(calls("/v1/handoffs/queue").at(-1)?.searchParams.get("from_date")).toBe("2026-01-31");
    });
  });

  it("sends nothing and shows the period message on all 4 cards for a span of 91 days", async () => {
    const { container } = renderPage();
    await waitForReady();
    mocks.fetch.mockClear();

    const [start] = dateInputs(container);
    fireEvent.change(start, { target: { value: "2026-01-30" } });

    expectPeriodMessageOnAllCards();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(mocks.fetch).not.toHaveBeenCalled();
  });
});

describe("<Consultoria /> cache", () => {
  it("keeps the numbers on screen when a background refetch fails", async () => {
    renderPage();
    await waitForReady();
    expect(card("outside_hours")).toHaveTextContent("30% (30 de 100)");
    expect(card("team_sla")).toHaveTextContent("80% (20 de 25)");

    statsReply = { status: 500 };
    queueReply = { status: 500 };
    await queryClient.refetchQueries();
    await waitFor(() => {
      const states = queryClient.getQueryCache().getAll().map((query) => query.state.status);
      expect(states).toEqual(["error", "error"]);
    });

    expect(card("outside_hours")).toHaveAttribute("data-state", "ready");
    expect(card("outside_hours")).toHaveTextContent("30% (30 de 100)");
    expect(card("team_sla")).toHaveAttribute("data-state", "ready");
    expect(card("team_sla")).toHaveTextContent("80% (20 de 25)");
  });

  it("does not refetch on a remount while the data is fresh", async () => {
    const first = renderPage();
    await waitForReady();
    first.unmount();
    renderPage();
    await waitForReady();
    await new Promise((resolve) => setTimeout(resolve, 20));

    expect(calls("/v1/traces/stats")).toHaveLength(1);
    expect(calls("/v1/handoffs/queue")).toHaveLength(1);
  });

  it("holds no patient data from the queue in the query cache", async () => {
    queueReply = {
      status: 200,
      body: { ...QUEUE, items: [{ id: "h1", numero_whatsapp: "5511999990000", nome: "Paciente Teste" }] },
    };
    renderPage();
    await waitForReady();

    const cached = queryClient.getQueryData(["consultoria", "handoff-queue", THIRTY_DAYS_AGO, TODAY, "clinic-id"]);
    expect(cached).toEqual({
      within_sla_count: 20,
      confirmed_late_count: 3,
      unanswered_overdue_count: 2,
      maturing_count: 0,
      cancelled_unconfirmed_count: 0,
      unmeasured_legacy_count: 0,
      sla_eligible_count: 25,
    });
    expect(JSON.stringify(cached)).not.toContain("5511999990000");
  });

  it("renders a queue body missing sla_eligible_count as not measured", async () => {
    const { sla_eligible_count: _omitted, ...withoutEligible } = QUEUE;
    queueReply = { status: 200, body: withoutEligible };
    renderPage();
    await waitForReady();

    expect(card("team_sla")).toHaveAttribute("data-state", "ready");
    expect(card("team_sla")).toHaveTextContent("Transferências para a equipe no período: 25");
    expect(card("team_sla")).toHaveTextContent(CONSULTORIA_COPY.pt.cards.teamSla.slaNoEligible);
    expect(card("team_sla")).not.toHaveTextContent("NaN");
  });

  it("renders an empty queue body and a null intent distribution as empty", async () => {
    queueReply = { status: 200, body: {} };
    statsReply = { status: 200, body: { ...STATS, intent_distribution: null } };
    renderPage();
    await waitForReady();

    expect(card("team_sla")).toHaveAttribute("data-state", "empty");
    expect(card("team_sla")).toHaveTextContent(CONSULTORIA_COPY.pt.cards.teamSla.empty);
    expect(card("top_intents")).toHaveAttribute("data-state", "empty");
    expect(card("top_intents")).toHaveTextContent(CONSULTORIA_COPY.pt.cards.topIntents.empty);
  });
});

describe("<Consultoria /> content", () => {
  it("shows the numbers derived from the fixture counts", async () => {
    renderPage();
    await waitForReady();

    expect(card("outside_hours")).toHaveTextContent("30% (30 de 100)");
    expect(card("outside_hours")).not.toHaveTextContent("99");
    expect(card("new_contacts")).toHaveTextContent("Novos: 7 · Recorrentes: 5");
    expect(card("new_contacts")).toHaveTextContent("Contatos novos no período: 7 de 12");
    expect(card("top_intents")).toHaveTextContent("Mensagens recebidas no período: 15");
    expect(card("top_intents")).toHaveTextContent("Pedido de Preço: 9 de 15");
    expect(card("team_sla")).toHaveTextContent("Transferências para a equipe no período: 25");
    expect(card("team_sla")).toHaveTextContent("80% (20 de 25)");
    // The queue's wait and its own percentage are never shown.
    expect(card("team_sla")).not.toHaveTextContent("754");
    expect(card("team_sla")).not.toHaveTextContent("61");

    // Each card shows only numbers derived from its own counts.
    const numbersByCard: Record<string, string[]> = {
      outside_hours: ["30", "30", "100"],
      new_contacts: ["7", "5", "7", "12"],
      top_intents: ["15", "9", "15", "4", "15"],
      team_sla: ["25", "80", "20", "25"],
    };
    for (const [id, expected] of Object.entries(numbersByCard)) {
      expect(card(id).textContent?.match(/\d+/g) ?? [], id).toEqual(expected);
    }
  });

  it("shows the premise picked by the rule", async () => {
    renderPage();
    await waitForReady();

    expect(screen.getByTestId("insight-outside_hours-premise")).toHaveTextContent(
      CONSULTORIA_COPY.pt.cards.outsideHours.premiseA,
    );
    expect(screen.getByTestId("insight-team_sla-premise")).toHaveTextContent(CONSULTORIA_COPY.pt.cards.teamSla.premiseA);
    // 12 contacts and a top count of 9 are below 20: neutral.
    expect(screen.getByTestId("insight-new_contacts-premise")).toHaveTextContent(
      CONSULTORIA_COPY.pt.cards.newContacts.premiseNeutral,
    );
    expect(screen.getByTestId("insight-top_intents-premise")).toHaveTextContent(
      CONSULTORIA_COPY.pt.cards.topIntents.premiseNeutral,
    );
  });

  it("shows a skeleton and the neutral premise while loading", () => {
    mocks.fetch.mockImplementation(() => new Promise(() => undefined));
    renderPage();

    expect(card("outside_hours")).toHaveAttribute("data-state", "loading");
    expect(screen.getByTestId("insight-outside_hours-loading")).toBeInTheDocument();
    expect(screen.getByTestId("insight-outside_hours-premise")).toHaveTextContent(
      CONSULTORIA_COPY.pt.cards.outsideHours.premiseNeutral,
    );
  });

  it.each(["pt", "en"] as const)("shows the card 2 qualifier in %s", async (locale) => {
    renderPage(locale);
    await waitForReady();

    expect(card("new_contacts")).toHaveTextContent(CONSULTORIA_COPY[locale].cards.newContacts.qualifier);
  });

  it("links the needs-setup state to /config", async () => {
    statsReply = { status: 200, body: { ...STATS, business_hours_configured: false } };
    renderPage();
    await waitForReady();

    const link = within(card("outside_hours")).getByRole("link", {
      name: CONSULTORIA_COPY.pt.cards.outsideHours.needsSetupAction,
    });
    expect(link).toHaveAttribute("href", "/config");
  });

  it("lists exactly the 4 coming-soon items, with no digits and no buttons", () => {
    renderPage();

    const section = screen.getByTestId("consultoria-coming-soon");
    const items = within(section).getAllByTestId("consultoria-coming-soon-item");
    expect(items.map((item) => item.querySelector("span")?.textContent)).toEqual(
      CONSULTORIA_COPY.pt.comingSoon.items.map((item) => item.name),
    );
    CONSULTORIA_COPY.pt.comingSoon.items.forEach((item, index) => {
      expect(items[index]).toHaveTextContent(`${item.name}: ${item.description}`);
    });
    expect(section.textContent).not.toMatch(/\d/);
    expect(section.querySelectorAll("button")).toHaveLength(0);
  });

  it("carries none of the old mock literals in its source or rendered page", async () => {
    const { container } = renderPage();
    await waitForReady();

    const literals = ["847", "92%", "+23%", "1.240", "1,240", "2,800", "+40%", "+18%", "+28%", "+17%", "12 min", "3 seg", "$ 82", "48h", "Harmonização Corporal", "Enterprise Only"];
    for (const literal of literals) {
      expect(pageSource).not.toContain(literal);
      expect(container.textContent).not.toContain(literal);
    }
  });
});

describe("<Consultoria /> no clinic and failures", () => {
  it("shows the header, the no-clinic message and coming soon to a builder with no clinic, without fetching", async () => {
    Object.assign(mocks.auth, { isBuilder: true, tenantId: "builder-id", viewingClinicId: "" });
    renderPage();

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(CONSULTORIA_COPY.pt.page.title);
    expect(screen.getByTestId("consultoria-no-clinic")).toHaveTextContent(CONSULTORIA_COPY.pt.page.noClinic);
    expect(screen.getByTestId("consultoria-coming-soon")).toBeInTheDocument();
    expect(screen.queryByTestId("insight-outside_hours")).not.toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(mocks.fetch).not.toHaveBeenCalled();
  });

  it("keeps card 4 and coming soon when stats fails with 500", async () => {
    statsReply = { status: 500 };
    renderPage();
    await waitForReady();

    for (const id of ["outside_hours", "new_contacts", "top_intents"]) {
      expect(card(id)).toHaveAttribute("data-state", "unavailable");
      expect(card(id)).toHaveTextContent(CONSULTORIA_COPY.pt.unavailable.generic);
    }
    expect(card("team_sla")).toHaveAttribute("data-state", "ready");
    expect(card("team_sla")).toHaveTextContent("80% (20 de 25)");
    expect(screen.getByTestId("consultoria-coming-soon")).toBeInTheDocument();
  });

  it("shows the period message only on card 4 when the queue answers 400", async () => {
    queueReply = { status: 400, body: { detail: "invalid_date_range" } };
    renderPage();
    await waitForReady();

    expect(card("team_sla")).toHaveAttribute("data-state", "unavailable");
    expect(card("team_sla")).toHaveTextContent(CONSULTORIA_COPY.pt.unavailable.period);
    for (const id of ["outside_hours", "new_contacts", "top_intents"]) {
      expect(card(id)).toHaveAttribute("data-state", "ready");
      expect(card(id)).not.toHaveTextContent(CONSULTORIA_COPY.pt.unavailable.period);
    }
  });
});
