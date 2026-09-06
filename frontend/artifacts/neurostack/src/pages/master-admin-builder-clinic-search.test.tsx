import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@/context/i18n";
import MasterAdmin from "./master-admin";

const mocks = vi.hoisted(() => ({
  fetch: vi.fn(),
  setViewingClinicId: vi.fn(),
  toast: vi.fn(),
}));

vi.mock("@/context/auth", () => ({
  useAuth: () => ({
    clinicId: null,
    isSuperAdmin: true,
    isInvestor: false,
    setViewingClinicId: mocks.setViewingClinicId,
  }),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mocks.toast }),
}));

const builders = [
  {
    id: 11,
    name: "Aurora Digital",
    admin_email: "admin@auroradigital.com.br",
    status: "active",
    subscription_status: "active",
    created_at: "2026-08-01T12:00:00Z",
    clinics: [
      {
        clinic_id: 101,
        clinic_name: "Clínica Horizonte Azul",
        admin_email: "contato@horizonte.com.br",
        slug: "horizonte-azul",
        status: "active",
        plan_type: "clinic",
        builder_id: 11,
        trace_count: 37,
        apis_configured: true,
      },
      {
        clinic_id: 102,
        clinic_name: "Instituto Primavera",
        admin_email: "admin@primavera.com.br",
        slug: "instituto-primavera",
        status: "active",
        plan_type: "clinic",
        builder_id: 11,
        trace_count: 19,
        apis_configured: true,
      },
    ],
  },
  {
    id: 22,
    name: "Nexo Automações",
    admin_email: "gestao@nexo.ai",
    status: "active",
    subscription_status: "trial",
    created_at: "2026-08-15T12:00:00Z",
    clinics: [
      {
        clinic_id: 201,
        clinic_name: "Centro Vida Plena",
        admin_email: "atendimento@vidaplena.com.br",
        slug: "vida-plena",
        status: "active",
        plan_type: "clinic",
        builder_id: 22,
        trace_count: 8,
        apis_configured: false,
      },
    ],
  },
];

function ok(data: unknown) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve(data) });
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("ns_locale", "pt");
  localStorage.setItem("ns_auth", JSON.stringify({ token: "master-admin-token" }));
  mocks.fetch.mockReset();
  mocks.fetch.mockImplementation((input: string | URL | Request) => {
    const url = String(input);
    if (url === "/v1/admin/builders") return ok(builders);
    if (url === "/v1/admin/clinics") return ok([]);
    if (url === "/v1/support/tickets") return ok([]);
    if (url === "/v1/demo-leads") return ok([]);
    if (url === "/v1/admin/stats") return ok({});
    if (url === "/v1/admin/dashboard") return ok({ stats: {}, time_series: [], alerts: [] });
    throw new Error(`Unexpected fetch: ${url}`);
  });
  vi.stubGlobal("fetch", mocks.fetch);
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  mocks.fetch.mockReset();
  mocks.setViewingClinicId.mockReset();
  mocks.toast.mockReset();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Master Admin builder search", () => {
  it("finds parent builders by builder, clinic, or builder email without pruning clinics", async () => {
    render(<I18nProvider><MasterAdmin /></I18nProvider>);

    const search = await screen.findByPlaceholderText("Buscar builder, clínica ou e-mail do builder...");
    expect(screen.getByText("Aurora Digital")).toBeInTheDocument();
    expect(screen.getByText("Nexo Automações")).toBeInTheDocument();

    fireEvent.change(search, { target: { value: "aurora digital" } });
    expect(screen.getByText("Aurora Digital")).toBeInTheDocument();
    expect(screen.queryByText("Nexo Automações")).not.toBeInTheDocument();

    fireEvent.change(search, { target: { value: "gestao@nexo.ai" } });
    expect(screen.getByText("Nexo Automações")).toBeInTheDocument();
    expect(screen.queryByText("Aurora Digital")).not.toBeInTheDocument();

    fireEvent.change(search, { target: { value: "  HORIZONTE AZUL  " } });
    const parentName = screen.getByText("Aurora Digital");
    expect(screen.queryByText("Clínica Horizonte Azul")).not.toBeInTheDocument();
    expect(screen.queryByText("Nexo Automações")).not.toBeInTheDocument();

    const parentCard = parentName.closest("div.rounded-lg");
    expect(parentCard).not.toBeNull();
    const expandButton = within(parentCard as HTMLElement).getAllByRole("button")[0];
    expect(expandButton).toBeInTheDocument();
    fireEvent.click(expandButton);
    expect(screen.getByText("Clínica Horizonte Azul")).toBeInTheDocument();
    expect(screen.getByText("Instituto Primavera")).toBeInTheDocument();

    fireEvent.change(search, { target: { value: "sem resultado" } });
    expect(screen.getByText(/Nenhum builder encontrado para/)).toHaveTextContent('"sem resultado"');
    expect(screen.queryByText("Aurora Digital")).not.toBeInTheDocument();

    fireEvent.change(search, { target: { value: "   " } });
    await waitFor(() => {
      expect(screen.getByText("Aurora Digital")).toBeInTheDocument();
      expect(screen.getByText("Nexo Automações")).toBeInTheDocument();
    });

    fireEvent.change(search, { target: { value: "" } });
    expect(screen.getByText("Aurora Digital")).toBeInTheDocument();
    expect(screen.getByText("Nexo Automações")).toBeInTheDocument();
    expect(screen.queryByText(/Nenhum builder encontrado para/)).not.toBeInTheDocument();
  });
});
