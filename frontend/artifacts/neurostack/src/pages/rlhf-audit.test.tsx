import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

import { I18nProvider } from "@/context/i18n";
import type { RatingsResponse } from "./rlhf-audit";

// The panel talks to `/v1/rlhf/ratings` through the shared workspace client.
// These tests exercise the *panel behavior* (filter narrowing, KPIs sourced
// from `summary`, the accept button's visibility rules), not the network, so
// the client is a stub that returns a fixture matching the exact response
// shape the backend guarantees (see src/api/routes/rlhf.py).
const fetchMock = vi.fn();

vi.mock("@workspace/api-client-react", () => ({
  customFetch: (url: string, options?: RequestInit) => fetchMock(url, options),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

// The page introduces `useAuth()` in this group. Default the mock to
// super_admin so every pre-existing test (which asserts today's full/master
// view) keeps passing unmodified. Investor-visibility tests below flip these
// flags before rendering.
const authState = { isSuperAdmin: true, isInvestor: false };
vi.mock("@/context/auth", () => ({
  useAuth: () => authState,
}));

// wouter's Link renders an <a>; the smoke shim is sufficient for asserting
// href without pulling in a real router provider.
vi.mock("wouter", () => ({
  Link: ({ href, children, ...rest }: { href: string; children: ReactNode } & Record<string, unknown>) => (
    <a href={href} {...(rest as Record<string, unknown>)}>{children}</a>
  ),
}));

vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  BarChart: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Bar: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
  Cell: () => null,
}));

import RlhfAudit from "./rlhf-audit";

const BUILDER_A = "builder-a";
const BUILDER_B = "builder-b";
const CLINIC_A1 = "clinic-a1";
const CLINIC_A2 = "clinic-a2";
const CLINIC_B1 = "clinic-b1";
const CLINIC_ORPHAN = "clinic-orphan"; // builder_id: null

function makeResponse(): RatingsResponse {
  return {
    summary: {
      total: 41,
      average: 3.7,
      accepted_count: 4,
      distribution: { "1": 3, "2": 5, "3": 8, "4": 15, "5": 10 },
      reviewed_count: 7,
    },
    facets: {
      builders: [
        { id: BUILDER_A, name: "Alpha Builder" },
        { id: BUILDER_B, name: "Bravo Builder" },
      ],
      clinics: [
        { id: CLINIC_A1, name: "Alpha Clinic One", builder_id: BUILDER_A },
        { id: CLINIC_A2, name: "Alpha Clinic Two", builder_id: BUILDER_A },
        { id: CLINIC_B1, name: "Bravo Clinic One", builder_id: BUILDER_B },
        { id: CLINIC_ORPHAN, name: "Orphan Clinic",    builder_id: null },
      ],
    },
    entries: [
      {
        id: "entry-with-reason",
        ts: "2026-08-20T10:00:00Z",
        tenant_id: CLINIC_A1,
        rating: 2,
        reason: "Resposta genérica demais para a pergunta feita.",
        agent_response: "resposta",
        user_message: "pergunta",
        submitted_by: "user@example.com",
        ai_analysis: { status: "agreed", justification: "Concorda com o cliente." },
        ai_analysis_status: "done",
        approved_for_training: false,
        ignored: false,
        subject_tenant_id: CLINIC_A1,
        subject_clinic_name: "Alpha Clinic One",
        builder_name: "Alpha Builder",
        accepted_by: null,
        accepted_at: null,
        accepted_by_email: null,
        channel: "web",
        trace_event_id: "trace-abc",
        trace_tenant_id: CLINIC_A1,
        review_history: [
          {
            observacao: "Olhei tudo, ok",
            revisado: true,
            revisado_por_email: "reviewer@neurostack.com",
            revisado_em: "2026-08-26T09:00:00Z",
          },
        ],
      },
      {
        id: "entry-short-reason",
        ts: "2026-08-21T10:00:00Z",
        tenant_id: CLINIC_A2,
        rating: 3,
        // Below the 8-char qualifying floor after trim — accept button must be
        // hidden even though this appears in "Feedbacks escritos".
        reason: "curto",
        agent_response: "resposta",
        user_message: "pergunta",
        submitted_by: "user2@example.com",
        ai_analysis: null,
        ai_analysis_status: "pending",
        approved_for_training: false,
        ignored: false,
        subject_tenant_id: CLINIC_A2,
        subject_clinic_name: "Alpha Clinic Two",
        builder_name: "Alpha Builder",
        accepted_by: null,
        accepted_at: null,
        accepted_by_email: null,
        channel: "whatsapp",
        trace_event_id: null,
        trace_tenant_id: null,
        review_history: [],
      },
      {
        id: "entry-already-accepted",
        ts: "2026-08-22T10:00:00Z",
        tenant_id: CLINIC_B1,
        rating: 1,
        reason: "Não respondeu à minha dúvida completa.",
        agent_response: "resposta",
        user_message: "pergunta",
        submitted_by: "user3@example.com",
        ai_analysis: null,
        ai_analysis_status: "failed",
        approved_for_training: true,
        ignored: false,
        subject_tenant_id: CLINIC_B1,
        subject_clinic_name: "Bravo Clinic One",
        builder_name: "Bravo Builder",
        accepted_by: "admin-uuid-123",
        accepted_at: "2026-08-25T12:34:00Z",
        accepted_by_email: "admin@neurostack.com",
        channel: "unknown",
        trace_event_id: null,
        trace_tenant_id: null,
        review_history: [],
      },
    ],
  };
}

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockImplementation(async () => makeResponse());
  // Reset auth to super_admin defaults before each test — investor tests
  // override in-line then restore in afterEach.
  authState.isSuperAdmin = true;
  authState.isInvestor = false;
});

afterEach(() => {
  cleanup();
  authState.isSuperAdmin = true;
  authState.isInvestor = false;
});

function renderPage() {
  render(
    <I18nProvider>
      <RlhfAudit />
    </I18nProvider>,
  );
}

async function renderAndSettle() {
  renderPage();
  // Wait for the initial async load to resolve and the KPI cards to hydrate
  // out of the empty response.
  await waitFor(() => {
    expect(screen.getByTestId("kpi-total")).toHaveTextContent("41");
  });
}

describe("<RlhfAudit /> — Auditoria RLHF panel", () => {
  it("omits the restricted-access wording from the audit notice", async () => {
    await renderAndSettle();

    expect(
      screen.getByText("Somente Master Admin. Contém metodologia proprietária de alinhamento SRM."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/acesso restrito/i)).not.toBeInTheDocument();
  });

  it("narrows the clinic dropdown to the selected builder's clinics and hides null-builder clinics", async () => {
    await renderAndSettle();

    const clinic = screen.getByTestId("filter-clinic") as HTMLSelectElement;

    // "Todos" (empty) — all four clinics + the "Todas as clínicas" placeholder,
    // including the orphan (null builder_id).
    const optionsAll = Array.from(clinic.querySelectorAll("option")).map((o) => o.textContent);
    expect(optionsAll).toEqual([
      "Todas as clínicas",
      "Alpha Clinic One",
      "Alpha Clinic Two",
      "Bravo Clinic One",
      "Orphan Clinic",
    ]);

    // Select Bravo Builder — should reduce to just Bravo Clinic One (plus placeholder).
    // Changing the builder triggers another fetch; we wait for it to settle.
    fetchMock.mockClear();
    await act(async () => {
      fireEvent.change(screen.getByTestId("filter-builder"), { target: { value: BUILDER_B } });
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const optionsB = Array.from(
      (screen.getByTestId("filter-clinic") as HTMLSelectElement).querySelectorAll("option"),
    ).map((o) => o.textContent);
    expect(optionsB).toEqual(["Todas as clínicas", "Bravo Clinic One"]);
    expect(optionsB).not.toContain("Orphan Clinic");
    expect(optionsB).not.toContain("Alpha Clinic One");
  });

  it("renders KPI values verbatim from the server summary, not derived from entries", async () => {
    await renderAndSettle();

    // Total, average, accepted_count come straight from summary — not from
    // counting the 3-entry fixture page.
    expect(screen.getByTestId("kpi-total")).toHaveTextContent("41");
    expect(screen.getByTestId("kpi-average")).toHaveTextContent("3,7");
    expect(screen.getByTestId("kpi-accepted")).toHaveTextContent("4");

    // % Satisfação = (dist[4]+dist[5])/total = (15+10)/41 = 60.97... -> 61%
    // % Insatisfação = (dist[1]+dist[2])/total = (3+5)/41 = 19.51... -> 19.5%
    // The 3-entry page (2, 3, 1) would give completely different numbers if
    // we were deriving from entries, so any pass here proves we read summary.
    const sat = screen.getByTestId("kpi-satisfaction");
    expect(sat).toHaveTextContent("61%");
    const unsat = screen.getByTestId("kpi-unsatisfaction");
    expect(unsat).toHaveTextContent("19,5%");
  });

  it("shows the accept button only for entries with a qualifying reason, and never for already-accepted or short-reason entries", async () => {
    await renderAndSettle();

    // Entry with a substantive reason (>= 8 trimmed chars): accept button offered.
    expect(screen.getByTestId("accept-btn-entry-with-reason")).toBeInTheDocument();
    expect(screen.getByTestId("accept-btn-entry-with-reason")).toHaveTextContent(
      "Aceitar e gravar opinião",
    );

    // Entry with a too-short reason ("curto" = 5 chars): appears in the
    // written-feedback list but no accept button.
    expect(screen.getByTestId("written-entry-entry-short-reason")).toBeInTheDocument();
    expect(screen.queryByTestId("accept-btn-entry-short-reason")).not.toBeInTheDocument();
    // Ignore option is still available for short-reason entries.
    expect(screen.getByTestId("ignore-btn-entry-short-reason")).toBeInTheDocument();

    // Already-accepted entry: no accept button, and the accepted_by email +
    // formatted date are surfaced instead of a raw UUID.
    expect(screen.queryByTestId("accept-btn-entry-already-accepted")).not.toBeInTheDocument();
    const acceptedMeta = screen.getByTestId("accepted-meta-entry-already-accepted");
    expect(acceptedMeta).toHaveTextContent("admin@neurostack.com");
    expect(acceptedMeta).not.toHaveTextContent("admin-uuid-123");
  });

  it("hides the accept button entirely outside the Feedbacks escritos section", async () => {
    await renderAndSettle();

    // The accept-btn testids must live only inside the written-feedback list.
    const acceptButtons = screen.queryAllByTestId(/^accept-btn-/);
    expect(acceptButtons.length).toBe(1);
    // And that single button must be inside the written-entry card for the
    // qualifying entry.
    const writtenEntry = screen.getByTestId("written-entry-entry-with-reason");
    expect(within(writtenEntry).getByTestId("accept-btn-entry-with-reason")).toBeInTheDocument();
  });

  it("always sends start_date, end_date and only_with_reason=true on the initial load", async () => {
    await renderAndSettle();

    expect(fetchMock).toHaveBeenCalled();
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("start_date=");
    expect(url).toContain("end_date=");
    expect(url).toContain("only_with_reason=true");
    // "Somente aceitas" is off by default — the query must not include
    // only_accepted on first load.
    expect(url).not.toContain("only_accepted=true");
    // No channel/reviewed filter set by default either.
    expect(url).not.toContain("channel=");
    expect(url).not.toContain("reviewed=");
  });

  it("adds only_accepted=true when the 'Somente aceitas' toggle is enabled", async () => {
    await renderAndSettle();

    fetchMock.mockClear();
    await act(async () => {
      fireEvent.click(screen.getByTestId("filter-only-accepted"));
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("only_accepted=true");
    expect(url).toContain("only_with_reason=true");
  });

  // ── Group 6 additions ─────────────────────────────────────────────────────

  it("sends the channel query param when the channel dropdown changes, without shrinking builder/clinic dropdowns", async () => {
    await renderAndSettle();

    // Baseline: builder and clinic dropdowns each render N + 1 options.
    const builderOptionsBefore = Array.from(
      (screen.getByTestId("filter-builder") as HTMLSelectElement).querySelectorAll("option"),
    ).length;
    const clinicOptionsBefore = Array.from(
      (screen.getByTestId("filter-clinic") as HTMLSelectElement).querySelectorAll("option"),
    ).length;

    fetchMock.mockClear();
    await act(async () => {
      fireEvent.change(screen.getByTestId("filter-channel"), { target: { value: "whatsapp" } });
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("channel=whatsapp");

    // Same builder/clinic option counts — channel is a universal narrower,
    // not a facet-shrinker.
    const builderOptionsAfter = Array.from(
      (screen.getByTestId("filter-builder") as HTMLSelectElement).querySelectorAll("option"),
    ).length;
    const clinicOptionsAfter = Array.from(
      (screen.getByTestId("filter-clinic") as HTMLSelectElement).querySelectorAll("option"),
    ).length;
    expect(builderOptionsAfter).toBe(builderOptionsBefore);
    expect(clinicOptionsAfter).toBe(clinicOptionsBefore);
  });

  it("sends the reviewed query param when the reviewed dropdown changes (super_admin only)", async () => {
    await renderAndSettle();

    fetchMock.mockClear();
    await act(async () => {
      fireEvent.change(screen.getByTestId("filter-reviewed"), { target: { value: "true" } });
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("reviewed=true");
  });

  it("renders the reviewed_count KPI card for super_admin sourced from summary.reviewed_count", async () => {
    await renderAndSettle();
    expect(screen.getByTestId("kpi-reviewed")).toHaveTextContent("7");
  });

  it("renders 'Ver trace' link when the entry has a trace_event_id, and explanatory text when it does not", async () => {
    await renderAndSettle();

    // Entry with trace links to /traces/{trace_event_id}?tenant_id={trace_tenant_id}
    const link = screen.getByTestId("trace-link-entry-with-reason") as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe(`/traces/trace-abc?tenant_id=${CLINIC_A1}`);

    // Entry without trace shows the missing-trace text in its place.
    expect(screen.queryByTestId("trace-link-entry-short-reason")).not.toBeInTheDocument();
    expect(screen.getByTestId("trace-missing-entry-short-reason")).toBeInTheDocument();
  });

  it("shows the human-review panel with history for super_admin, including current reviewer email", async () => {
    await renderAndSettle();

    const panel = screen.getByTestId("review-panel-entry-with-reason");
    expect(panel).toBeInTheDocument();

    // History renders the fixture's single review row.
    const historyRow = screen.getByTestId("review-history-row-entry-with-reason-0");
    expect(historyRow).toHaveTextContent("reviewer@neurostack.com");
    expect(historyRow).toHaveTextContent("Olhei tudo, ok");

    // Entry with an empty history renders the "not yet reviewed" placeholder.
    const emptyHistory = screen.getByTestId("review-history-entry-short-reason");
    expect(emptyHistory).toBeInTheDocument();
    expect(screen.queryByTestId("review-history-row-entry-short-reason-0")).not.toBeInTheDocument();
  });

  it("POSTs to /review with observacao=null when marking REVISADO with no observation typed", async () => {
    await renderAndSettle();

    // The short-reason entry has never been reviewed (history is empty), so
    // the toggle currently reads "Marcar como REVISADO" and posting flips to
    // revisado: true.
    fetchMock.mockClear();
    // The follow-up fetch after POST is the ratings reload; both need to
    // succeed for the toggle path.
    fetchMock.mockImplementation(async (url: string) => {
      if (url.includes("/review")) return { ok: true, id: "hist-1", rating_id: "entry-short-reason", revisado: true };
      return makeResponse();
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId("revisado-toggle-entry-short-reason"));
    });

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([u]) => (u as string).includes("/review"));
      expect(call).toBeDefined();
    });

    const reviewCall = fetchMock.mock.calls.find(([u]) => (u as string).includes("/review"));
    expect(reviewCall).toBeDefined();
    const url = reviewCall![0] as string;
    const options = reviewCall![1] as RequestInit;
    expect(url).toContain("/v1/rlhf/ratings/entry-short-reason/review");
    expect(options.method).toBe("POST");
    const body = JSON.parse(options.body as string) as { revisado: boolean; observacao: string | null };
    expect(body.revisado).toBe(true);
    expect(body.observacao).toBeNull();
  });

  it("POSTs revisado:false when the current state is reviewed (toggle reverts)", async () => {
    await renderAndSettle();

    fetchMock.mockClear();
    fetchMock.mockImplementation(async (url: string) => {
      if (url.includes("/review")) return { ok: true, id: "hist-2", rating_id: "entry-with-reason", revisado: false };
      return makeResponse();
    });

    // entry-with-reason has one history row with revisado:true — the toggle
    // reads "Desfazer REVISADO" and posts revisado:false.
    await act(async () => {
      fireEvent.click(screen.getByTestId("revisado-toggle-entry-with-reason"));
    });

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([u]) => (u as string).includes("/review"));
      expect(call).toBeDefined();
    });
    const reviewCall = fetchMock.mock.calls.find(([u]) => (u as string).includes("/review"));
    const body = JSON.parse((reviewCall![1] as RequestInit).body as string) as { revisado: boolean };
    expect(body.revisado).toBe(false);
  });

  it("sends the typed observation trimmed on the POST body", async () => {
    await renderAndSettle();

    fetchMock.mockClear();
    fetchMock.mockImplementation(async (url: string) => {
      if (url.includes("/review")) return { ok: true, id: "hist-3", rating_id: "entry-short-reason", revisado: true };
      return makeResponse();
    });

    fireEvent.change(screen.getByTestId("observation-entry-short-reason"), {
      target: { value: "  minha nota  " },
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId("revisado-toggle-entry-short-reason"));
    });

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([u]) => (u as string).includes("/review"));
      expect(call).toBeDefined();
    });
    const reviewCall = fetchMock.mock.calls.find(([u]) => (u as string).includes("/review"));
    const body = JSON.parse((reviewCall![1] as RequestInit).body as string) as { revisado: boolean; observacao: string | null };
    expect(body.observacao).toBe("minha nota");
  });

  describe("investor visibility cuts", () => {
    beforeEach(() => {
      authState.isSuperAdmin = false;
      authState.isInvestor = true;
    });

    it("hides reviewed dropdown, reviewed_count KPI, builder/clinic dropdowns, review panel, trace link, and accept/ignore buttons", async () => {
      await renderAndSettle();

      // Filters that only super_admin sees are gone.
      expect(screen.queryByTestId("filter-builder")).not.toBeInTheDocument();
      expect(screen.queryByTestId("filter-clinic")).not.toBeInTheDocument();
      expect(screen.queryByTestId("filter-reviewed")).not.toBeInTheDocument();

      // Reviewed KPI card is not rendered.
      expect(screen.queryByTestId("kpi-reviewed")).not.toBeInTheDocument();

      // Base KPI cards and the channel dropdown remain visible.
      expect(screen.getByTestId("kpi-total")).toBeInTheDocument();
      expect(screen.getByTestId("filter-channel")).toBeInTheDocument();

      // Review panel + trace link + accept/ignore buttons are cut everywhere.
      expect(screen.queryByTestId("review-panel-entry-with-reason")).not.toBeInTheDocument();
      expect(screen.queryByTestId("trace-link-entry-with-reason")).not.toBeInTheDocument();
      expect(screen.queryByTestId("trace-missing-entry-short-reason")).not.toBeInTheDocument();
      expect(screen.queryAllByTestId(/^accept-btn-/).length).toBe(0);
      expect(screen.queryAllByTestId(/^ignore-btn-/).length).toBe(0);

      // The written-feedback list itself (with reason + AI analysis + clinic
      // name) still renders.
      expect(screen.getByTestId("written-entry-entry-with-reason")).toBeInTheDocument();
    });
  });
});
