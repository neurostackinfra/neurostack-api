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
      },
    ],
  };
}

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockImplementation(async () => makeResponse());
});

afterEach(() => {
  cleanup();
});

async function renderAndSettle() {
  render(<RlhfAudit />);
  // Wait for the initial async load to resolve and the KPI cards to hydrate
  // out of the empty response.
  await waitFor(() => {
    expect(screen.getByTestId("kpi-total")).toHaveTextContent("41");
  });
}

describe("<RlhfAudit /> — Auditoria RLHF panel", () => {
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
});
