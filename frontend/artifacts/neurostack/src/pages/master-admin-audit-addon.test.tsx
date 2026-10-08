import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@/context/i18n";
import MasterAdmin from "./master-admin";

const mocks = vi.hoisted(() => ({
  fetch: vi.fn(),
  setViewingClinicId: vi.fn(),
  toast: vi.fn(),
  patchResponses: new Map<string, { ok: boolean; status: number; body: unknown }>(),
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

const linkedClinic = {
  clinic_id: 101,
  clinic_name: "Clínica Horizonte Azul",
  admin_email: "contato@horizonte.com.br",
  slug: "horizonte-azul",
  status: "active",
  plan_type: "clinic",
  builder_id: 11,
  trace_count: 37,
  apis_configured: true,
  audit_extended_access: false,
};

const builders = [
  {
    id: 11,
    name: "Aurora Digital",
    admin_email: "admin@auroradigital.com.br",
    status: "active",
    subscription_status: "active",
    created_at: "2026-08-01T12:00:00Z",
    clinics: [linkedClinic],
  },
];

const clinics = [
  {
    clinic_id: 101,
    clinic_name: "Clínica Horizonte Azul",
    agent_name: "Horizonte",
    slug: "horizonte-azul",
    status: "active",
    plan_type: "pilot",
    trace_count: 37,
    apis_configured: true,
    users: [{ id: 1, email: "contato@horizonte.com.br", role: "clinic_admin" }],
    audit_extended_access: false,
  },
  {
    clinic_id: 301,
    clinic_name: "Clínica Solo Serena",
    agent_name: "Serena",
    slug: "solo-serena",
    status: "active",
    plan_type: "pilot",
    trace_count: 4,
    apis_configured: true,
    users: [{ id: 2, email: "admin@serena.com.br", role: "clinic_admin" }],
    audit_extended_access: true,
  },
];

function respond(ok: boolean, status: number, data: unknown) {
  return Promise.resolve({ ok, status, json: () => Promise.resolve(data) });
}

function patchCalls() {
  return mocks.fetch.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === "PATCH");
}

async function openClinicRow(clinicName: string) {
  const name = await screen.findByText(clinicName);
  const row = name.closest("div.rounded-md") as HTMLElement;
  expect(row).not.toBeNull();
  const trigger = within(row).getAllByRole("button").find((button) => button.hasAttribute("aria-expanded"));
  expect(trigger).toBeDefined();
  fireEvent.click(trigger as HTMLElement);
  return row;
}

async function openBuilder(builderName: string) {
  const parentCard = (await screen.findByText(builderName)).closest("div.rounded-lg") as HTMLElement;
  fireEvent.click(within(parentCard).getAllByRole("button")[0]);
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("ns_locale", "pt");
  localStorage.setItem("ns_auth", JSON.stringify({ token: "master-admin-token" }));
  mocks.patchResponses.clear();
  mocks.fetch.mockReset();
  mocks.fetch.mockImplementation((input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === "PATCH") {
      const configured = mocks.patchResponses.get(url);
      if (!configured) throw new Error(`Unexpected PATCH: ${url}`);
      return respond(configured.ok, configured.status, configured.body);
    }
    if (url === "/v1/admin/builders") return respond(true, 200, builders);
    if (url === "/v1/admin/clinics") return respond(true, 200, clinics);
    if (url === "/v1/support/tickets") return respond(true, 200, []);
    if (url === "/v1/demo-leads") return respond(true, 200, []);
    if (url === "/v1/admin/stats") return respond(true, 200, {});
    if (url === "/v1/admin/dashboard") return respond(true, 200, { stats: {}, time_series: [], alerts: [] });
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

describe("Master Admin audit add-on switch", () => {
  it("persists the add-on on a builder-linked clinic row and shows the stored value", async () => {
    mocks.patchResponses.set("/v1/admin/clinics/101", {
      ok: true,
      status: 200,
      body: { ok: true, audit_extended_access: true },
    });
    render(<I18nProvider><MasterAdmin /></I18nProvider>);

    await openBuilder("Aurora Digital");
    const row = await openClinicRow("Clínica Horizonte Azul");
    const addon = within(row).getByRole("switch", { name: "Add-on Auditoria Estendida" });
    expect(addon).toHaveAttribute("aria-checked", "false");
    expect(
      within(row).getByText(
        "Libera exportações de traces e handoffs além de 6 meses atrás. O limite de 60 dias por arquivo continua valendo.",
      ),
    ).toBeInTheDocument();

    fireEvent.click(addon);

    await waitFor(() => expect(addon).toHaveAttribute("aria-checked", "true"));
    expect(patchCalls()).toHaveLength(1);
    const [url, init] = patchCalls()[0] as [string, RequestInit];
    expect(url).toBe("/v1/admin/clinics/101");
    expect(JSON.parse(String(init.body))).toEqual({ audit_extended_access: true });
    expect(mocks.toast).toHaveBeenCalledWith({
      title: "Add-on Auditoria Estendida ativado",
      description: "ID 101",
    });
  });

  it("persists the add-on on an unlinked clinic row and shows the stored value", async () => {
    mocks.patchResponses.set("/v1/admin/clinics/301", {
      ok: true,
      status: 200,
      body: { ok: true, audit_extended_access: false },
    });
    render(<I18nProvider><MasterAdmin /></I18nProvider>);

    const row = await openClinicRow("Clínica Solo Serena");
    const addon = within(row).getByRole("switch", { name: "Add-on Auditoria Estendida" });
    expect(addon).toHaveAttribute("aria-checked", "true");

    fireEvent.click(addon);

    await waitFor(() => expect(addon).toHaveAttribute("aria-checked", "false"));
    expect(patchCalls()).toHaveLength(1);
    const [url, init] = patchCalls()[0] as [string, RequestInit];
    expect(url).toBe("/v1/admin/clinics/301");
    expect(JSON.parse(String(init.body))).toEqual({ audit_extended_access: false });
    expect(mocks.toast).toHaveBeenCalledWith({
      title: "Add-on Auditoria Estendida desativado",
      description: "ID 301",
    });
    expect(addon).toBeEnabled();
  });

  it("shows the value the server stored, not the one requested", async () => {
    mocks.patchResponses.set("/v1/admin/clinics/101", {
      ok: true,
      status: 200,
      body: { ok: true, audit_extended_access: false },
    });
    render(<I18nProvider><MasterAdmin /></I18nProvider>);

    await openBuilder("Aurora Digital");
    const row = await openClinicRow("Clínica Horizonte Azul");
    const addon = within(row).getByRole("switch", { name: "Add-on Auditoria Estendida" });

    fireEvent.click(addon);

    await waitFor(() =>
      expect(mocks.toast).toHaveBeenCalledWith({
        title: "Add-on Auditoria Estendida desativado",
        description: "ID 101",
      }),
    );
    expect(JSON.parse(String((patchCalls()[0][1] as RequestInit).body))).toEqual({
      audit_extended_access: true,
    });
    expect(addon).toHaveAttribute("aria-checked", "false");
  });

  it("keeps the previous value and toasts when the update fails", async () => {
    mocks.patchResponses.set("/v1/admin/clinics/301", {
      ok: false,
      status: 500,
      body: { detail: "boom" },
    });
    render(<I18nProvider><MasterAdmin /></I18nProvider>);

    const row = await openClinicRow("Clínica Solo Serena");
    const addon = within(row).getByRole("switch", { name: "Add-on Auditoria Estendida" });

    fireEvent.click(addon);

    await waitFor(() =>
      expect(mocks.toast).toHaveBeenCalledWith({
        title: "Erro",
        description: "Não foi possível alterar o add-on Auditoria Estendida.",
        variant: "destructive",
      }),
    );
    expect(addon).toHaveAttribute("aria-checked", "true");
    expect(addon).toBeEnabled();
  });
});
