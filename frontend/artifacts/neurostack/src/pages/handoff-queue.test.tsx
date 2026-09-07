import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { I18nProvider } from "@/context/i18n";
import HandoffQueuePage from "./handoff-queue";

const mocks = vi.hoisted(() => ({
  auth: {
    tenantId: "clinic-id",
    isBuilder: false,
    isSuperAdmin: false,
    isInvestor: false,
    viewingClinicId: "",
  },
  fetch: vi.fn(),
}));

vi.mock("@/context/auth", () => ({
  useAuth: () => mocks.auth,
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

function renderPage() {
  return render(
    <I18nProvider>
      <HandoffQueuePage />
    </I18nProvider>,
  );
}

beforeEach(() => {
  localStorage.setItem("ns_locale", "pt");
  localStorage.setItem("ns_auth", JSON.stringify({ token: "test-token" }));
  Object.assign(mocks.auth, { isInvestor: false, tenantId: "clinic-id" });
  mocks.fetch.mockReset();
  vi.stubGlobal("fetch", mocks.fetch);
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
});

describe("<HandoffQueuePage />", () => {
  it("loads a payload containing estagio_atual without rendering its label or value", async () => {
    mocks.fetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        items: [{
          id: "handoff-1",
          numero_whatsapp: "5511999999999",
          session_id: "12345678-abcd-4abc-8def-1234567890ab",
          intencao_detectada: "pedido_preco",
          estagio_atual: "stage-payload-marker",
          primeira_linha_mensagem: null,
          created_at: new Date().toISOString(),
          data_hora_saida: null,
          status: "pending",
        }],
        avg_wait_seconds: 10,
        pct_within_sla: 90,
      }),
    } as Response);

    renderPage();

    expect(await screen.findByText("pedido_preco")).toBeInTheDocument();
    expect(screen.queryByText("Estágio")).not.toBeInTheDocument();
    expect(screen.queryByText("stage-payload-marker")).not.toBeInTheDocument();
  });

  it("does not reveal estagio_atual from investor mock data", async () => {
    Object.assign(mocks.auth, { isInvestor: true, tenantId: null });

    renderPage();

    expect(await screen.findAllByText("scheduling")).not.toHaveLength(0);
    expect(screen.queryByText("Estágio")).not.toBeInTheDocument();
    expect(screen.queryByText("agendamento_solicitado")).not.toBeInTheDocument();
    expect(screen.queryByText("em_qualificacao")).not.toBeInTheDocument();
    expect(mocks.fetch).not.toHaveBeenCalled();
  });
});
