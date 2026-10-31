import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { I18nProvider } from "@/context/i18n";
import KnowledgeBase from "./knowledge-base";

function renderPage() {
  return render(
    <I18nProvider>
      <KnowledgeBase />
    </I18nProvider>,
  );
}

describe("<KnowledgeBase />", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("presents Botox as a post-procedure document", () => {
    renderPage();

    expect(screen.getByText("Protocolo_Pos_Procedimento_Botox.pdf")).toBeInTheDocument();
    expect(screen.queryByText("Protocolo_Pos_Operatorio_Botox.pdf")).not.toBeInTheDocument();
    expect(screen.getByText("Regras_Internas_Clinica_Lumina.docx")).toBeInTheDocument();
  });

  // ── Credits (neurostack-credit-system Group 6) ────────────────────────────

  it("reads no credit for a clinic session and stays as on main", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    localStorage.setItem(
      "ns_auth",
      JSON.stringify({ token: "clinic-token", role: "clinic_admin", clinicId: "c1" }),
    );
    renderPage();

    expect(screen.getByText("Regras_Internas_Clinica_Lumina.docx")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.queryByTestId("credit-allocation-rag")).not.toBeInTheDocument();
  });

  it("hides the credit allocation while the credit API is unavailable", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 503,
      json: async () => ({ code: "credit_unavailable" }),
    }));
    vi.stubGlobal("fetch", fetchMock);
    localStorage.setItem(
      "ns_auth",
      JSON.stringify({ token: "builder-token", role: "builder", viewingClinicId: "clinic-7" }),
    );
    renderPage();

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/v1/credits/reservations?tenant_id=clinic-7",
        expect.objectContaining({ headers: { Authorization: "Bearer builder-token" } }),
      ),
    );
    expect(screen.queryByTestId("credit-allocation-rag")).not.toBeInTheDocument();
    expect(screen.getByText("Protocolo_Pos_Procedimento_Botox.pdf")).toBeInTheDocument();
  });

  it("shows the selected clinic's RAG allocation to a builder", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => ({
          as_of: "2026-10-30T15:00:00Z",
          window: { start: "2026-10-25", end: "2026-11-25", kind: "cycle" },
          reservations: [
            { category: "rag", units: 15_000, coverage_mc: 3_000, charged_mc: 2_000 },
            { category: "media", units: 0, coverage_mc: 0, charged_mc: 0 },
            { category: "egress", units: 0, coverage_mc: 0, charged_mc: 0 },
          ],
          egress_allowance_bytes: 0,
          egress_delivered_bytes: 0,
        }),
      })),
    );
    localStorage.setItem("ns_locale", "pt");
    localStorage.setItem(
      "ns_auth",
      JSON.stringify({ token: "builder-token", role: "builder", viewingClinicId: "clinic-7" }),
    );
    renderPage();

    const notice = await screen.findByTestId("credit-allocation-rag");
    expect(notice).toHaveTextContent(/Créditos da base de conhecimento/);
    expect(notice).toHaveTextContent(/Alocado no ciclo:\s*3 créditos/);
    expect(notice).toHaveTextContent(/Debitado:\s*2 créditos/);
  });
});
