import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider, type Locale } from "@/context/i18n";
import cloneContract from "../../../../../tests/fixtures/clinic_clone_contract.json";
import BuilderPanel from "./builder";

const mocks = vi.hoisted(() => ({
  isBuilder: true,
  logout: vi.fn(),
  navigate: vi.fn(),
  setViewingClinicId: vi.fn(),
  toast: vi.fn(),
  fetch: vi.fn(),
}));

vi.mock("@/context/auth", () => ({
  useAuth: () => ({
    isBuilder: mocks.isBuilder,
    setViewingClinicId: mocks.setViewingClinicId,
    logout: mocks.logout,
  }),
}));
vi.mock("wouter", () => ({
  useLocation: () => ["/builder", mocks.navigate],
}));
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mocks.toast }),
}));
vi.mock("@/components/plan-usage-summary", () => ({
  PlanUsageSummary: ({ usage }: { usage: typeof billingUsage }) => (
    <div data-testid="billing-rag-used">{usage.storage.rag.used_bytes}</div>
  ),
}));

const sourceClinic = {
  id: "11111111-1111-4111-8111-111111111111",
  name: "Clínica Ocular",
  slug: "clinica-ocular",
  status: "active" as const,
  plan_type: "clinic",
  is_active: true,
  created_at: null,
  last_trace_at: null,
  health_status: "healthy" as const,
  trace_count_7d: 0,
  safety_flags_7d: 0,
  handoff_count_7d: 0,
  outside_hours_count_7d: 0,
  outside_hours_pct_7d: null,
  conversational_trace_count_7d: 0,
  business_hours_configured: false,
  config_completeness: 100,
};

const secondClinic = {
  ...sourceClinic,
  id: "33333333-3333-4333-8333-333333333333",
  name: "Clínica Retina",
  slug: "clinica-retina",
};

const clonedClinic = {
  ...sourceClinic,
  id: cloneContract.success.body.id,
  name: cloneContract.success.body.name,
  slug: "clinica-ocular-copia-2",
  status: "inactive" as const,
  is_active: cloneContract.success.body.is_active,
  health_status: "warning" as const,
};

const billingUsage = {
  plan: "builder",
  as_of: "2026-09-05T12:00:00Z",
  trace_period: {
    start: "2026-09-01T00:00:00Z",
    end: "2026-09-30T23:59:59Z",
    timezone: "UTC",
  },
  traces: {
    used: 0,
    included: 100,
    extra_credits: 0,
    available: 100,
    overage: 0,
    percent_used: 0,
  },
  storage: {
    rag: {
      used_bytes: 0,
      used_mb: 0,
      limit_bytes: 1048576,
      limit_mb: 1,
      available_bytes: 1048576,
      available_mb: 1,
      overage_bytes: 0,
      overage_mb: 0,
      percent_used: 0,
    },
    media: {
      used_bytes: 0,
      used_mb: 0,
      limit_bytes: 1048576,
      limit_mb: 1,
      available_bytes: 1048576,
      available_mb: 1,
      overage_bytes: 0,
      overage_mb: 0,
      percent_used: 0,
    },
  },
  credits: { purchase_enabled: false, coming_soon: true },
};

type ServedContract = {
  method: string;
  path: string;
  status: number;
  body: unknown;
};

let cloneStatus: number;
let cloneBody: unknown;
let cloneResponsePromise: Promise<Response> | null;
let cloneSucceeded: boolean;
let clinicsRequestCount: number;
let billingRequestCount: number;
let availableClinics: typeof sourceClinic[];
let clinicsRefreshStatus: number;
let billingRefreshStatus: number;
let previewResponses: Map<string, () => Promise<Response>>;
let servedContracts: ServedContract[];

function contractPath(path: string) {
  return path.replace("{source_id}", sourceClinic.id);
}

function response(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

function renderBuilder(locale: Locale = "en") {
  localStorage.setItem("ns_locale", locale);
  return render(
    <I18nProvider>
      <BuilderPanel />
    </I18nProvider>,
  );
}

async function openCloningCenter() {
  const tab = await screen.findByRole("tab", { name: /Cloning Center|Central de Clonagem/ });
  fireEvent.mouseDown(tab, { button: 0, ctrlKey: false });
  await waitFor(() => expect(tab).toHaveAttribute("aria-selected", "true"));
  return tab;
}

async function loadPreview() {
  await openCloningCenter();
  fireEvent.change(screen.getByLabelText(/Source clinic|Clínica de origem/), {
    target: { value: sourceClinic.id },
  });
  await screen.findByText(cloneContract.preview.body.suggested_name);
}

beforeEach(() => {
  localStorage.setItem("ns_auth", JSON.stringify({ token: "builder-token" }));
  mocks.isBuilder = true;
  mocks.fetch.mockReset();
  mocks.navigate.mockReset();
  mocks.setViewingClinicId.mockReset();
  mocks.toast.mockReset();
  cloneStatus = cloneContract.success.status;
  cloneBody = cloneContract.success.body;
  cloneResponsePromise = null;
  cloneSucceeded = false;
  clinicsRequestCount = 0;
  billingRequestCount = 0;
  availableClinics = [sourceClinic];
  clinicsRefreshStatus = 200;
  billingRefreshStatus = 200;
  previewResponses = new Map();
  servedContracts = [];

  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );

  mocks.fetch.mockImplementation((input: string | URL | Request, init?: RequestInit) => {
    const path = String(input);
    const method = init?.method ?? "GET";
    if (path === "/v1/builder/me") {
      return Promise.resolve(response(200, {
        id: "builder",
        name: "Builder",
        status: "active",
        clinic_count: 1,
      }));
    }
    if (path === "/v1/builder/dashboard") {
      return Promise.resolve(response(200, {
        stats: {
          clinic_count: 1,
          active_clinics: 1,
          inactive_clinics: 0,
          total_traces: 0,
          total_tokens: 0,
          model_breakdown: [],
        },
        time_series: [],
        alerts: [],
      }));
    }
    if (path === "/v1/builder/clinics" && method === "GET") {
      clinicsRequestCount += 1;
      const authoritativeClone = {
        ...clonedClinic,
        ...(typeof cloneBody === "object" && cloneBody !== null ? cloneBody : {}),
      };
      return Promise.resolve(response(
        clinicsRequestCount > 1 ? clinicsRefreshStatus : 200,
        cloneSucceeded ? [...availableClinics, authoritativeClone] : availableClinics,
      ));
    }
    if (path === "/v1/billing/usage") {
      billingRequestCount += 1;
      const refreshedUsage = {
        ...billingUsage,
        storage: {
          ...billingUsage.storage,
          rag: {
            ...billingUsage.storage.rag,
            used_bytes: cloneSucceeded ? cloneContract.preview.body.total_bytes : 0,
          },
        },
      };
      return Promise.resolve(response(
        billingRequestCount > 1 ? billingRefreshStatus : 200,
        refreshedUsage,
      ));
    }
    if (path === "/v1/builder/tickets") {
      return Promise.resolve(response(200, []));
    }
    if (path === "/v1/builder/profile") {
      return Promise.resolve(response(200, { support_email: null }));
    }
    if (path.endsWith("/clone-preview")) {
      servedContracts.push({
        method,
        path,
        status: cloneContract.preview.status,
        body: cloneContract.preview.body,
      });
      const sourceId = path.split("/").at(-2) ?? "";
      const previewResponse = previewResponses.get(sourceId);
      if (previewResponse) return previewResponse();
      return Promise.resolve(response(cloneContract.preview.status, cloneContract.preview.body));
    }
    if (path === contractPath(cloneContract.success.path)) {
      servedContracts.push({ method, path, status: cloneStatus, body: cloneBody });
      cloneSucceeded = cloneStatus === cloneContract.success.status;
      return cloneResponsePromise ?? Promise.resolve(response(cloneStatus, cloneBody));
    }
    throw new Error(`Unexpected fetch: ${method} ${path}`);
  });
  vi.stubGlobal("fetch", mocks.fetch);
});

describe("BuilderPanel Cloning Center", () => {
  it("uses the shared preview/POST contract, discards advisory state, refreshes, and configures the authoritative clone", async () => {
    const authoritativeResult = {
      id: "22222222-2222-4222-8222-22222222222b",
      name: "Clínica Ocular cópia 3",
      is_active: false,
    };
    cloneBody = authoritativeResult;
    renderBuilder();
    await loadPreview();

    expect(servedContracts[0]).toEqual({
      method: cloneContract.preview.method,
      path: contractPath(cloneContract.preview.path),
      status: cloneContract.preview.status,
      body: cloneContract.preview.body,
    });
    expect(screen.getByText("This name is advisory and may change when the clone is created.")).toBeInTheDocument();
    expect(screen.getByText(String(cloneContract.preview.body.document_count))).toBeInTheDocument();
    expect(screen.getByText(`${cloneContract.preview.body.total_bytes} B`)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("checkbox", { name: /I understand what will be copied/ }));
    fireEvent.click(screen.getByRole("button", { name: "Create clone" }));

    await screen.findByText(`ID: ${authoritativeResult.id}`);
    expect(servedContracts[1]).toEqual({
      method: cloneContract.success.method,
      path: contractPath(cloneContract.success.path),
      status: cloneContract.success.status,
      body: authoritativeResult,
    });
    expect(screen.queryByText("Clone preview")).not.toBeInTheDocument();
    expect(screen.queryByText("Suggested name")).not.toBeInTheDocument();
    expect(screen.queryByText(cloneContract.preview.body.suggested_name)).not.toBeInTheDocument();
    expect(screen.getByText(authoritativeResult.name)).toBeInTheDocument();
    expect(screen.getByText("This is the authoritative clinic returned by the server.")).toBeInTheDocument();
    expect(screen.getByText("The clone starts inactive.")).toBeInTheDocument();
    expect(clinicsRequestCount).toBe(2);
    expect(mocks.fetch.mock.calls.filter(([path]) => String(path) === "/v1/billing/usage")).toHaveLength(2);

    fireEvent.mouseDown(screen.getByRole("tab", { name: "Clinics" }), { button: 0, ctrlKey: false });
    await waitFor(() => expect(screen.getByRole("tab", { name: "Clinics" })).toHaveAttribute("aria-selected", "true"));
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText(authoritativeResult.name)).toBeInTheDocument();

    fireEvent.mouseDown(screen.getByRole("tab", { name: "Dashboard" }), { button: 0, ctrlKey: false });
    await waitFor(() => expect(screen.getByRole("tab", { name: "Dashboard" })).toHaveAttribute("aria-selected", "true"));
    expect(screen.getByTestId("billing-rag-used")).toHaveTextContent(String(cloneContract.preview.body.total_bytes));

    fireEvent.mouseDown(screen.getByRole("tab", { name: "Cloning Center" }), { button: 0, ctrlKey: false });
    await waitFor(() => expect(screen.getByRole("tab", { name: "Cloning Center" })).toHaveAttribute("aria-selected", "true"));
    fireEvent.click(screen.getByRole("button", { name: "Configure cloned clinic" }));
    expect(mocks.setViewingClinicId).toHaveBeenCalledWith(authoritativeResult.id);
    expect(mocks.navigate).toHaveBeenCalledWith("/config");
  });

  it("keeps only the latest clinic preview when requests resolve out of order", async () => {
    availableClinics = [sourceClinic, secondClinic];
    let resolveFirst!: (value: Response) => void;
    let resolveSecond!: (value: Response) => void;
    previewResponses.set(sourceClinic.id, () => new Promise((resolve) => { resolveFirst = resolve; }));
    previewResponses.set(secondClinic.id, () => new Promise((resolve) => { resolveSecond = resolve; }));
    const latestPreview = {
      suggested_name: "Clínica Retina cópia 4",
      document_count: 7,
      total_bytes: 987,
    };

    renderBuilder();
    await openCloningCenter();
    const select = screen.getByLabelText("Source clinic");
    fireEvent.change(select, { target: { value: sourceClinic.id } });
    fireEvent.change(select, { target: { value: secondClinic.id } });
    await act(async () => { resolveSecond(response(200, latestPreview)); });
    await screen.findByText(latestPreview.suggested_name);
    await act(async () => { resolveFirst(response(200, cloneContract.preview.body)); });

    expect(select).toHaveValue(secondClinic.id);
    expect(screen.getByText(latestPreview.suggested_name)).toBeInTheDocument();
    expect(screen.getByText(String(latestPreview.document_count))).toBeInTheDocument();
    expect(screen.getByText(`${latestPreview.total_bytes} B`)).toBeInTheDocument();
    expect(screen.queryByText(cloneContract.preview.body.suggested_name)).not.toBeInTheDocument();
  });

  it.each([
    [403, "You no longer have permission to preview this clinic. The source remains selected."],
    [404, "The source clinic could not be found for preview. The source remains selected."],
    [409, "A preview is unavailable for the clinic's current configuration. The source remains selected."],
    [500, "The clone preview could not be loaded. The source remains selected."],
  ])("clears stale confirmation and shows preview-specific copy for %s", async (status, message) => {
    availableClinics = [sourceClinic, secondClinic];
    renderBuilder();
    await loadPreview();
    fireEvent.click(screen.getByRole("checkbox", { name: /I understand what will be copied/ }));
    previewResponses.set(secondClinic.id, () => Promise.resolve(response(status, { detail: { error: "preview_failed" } })));

    fireEvent.change(screen.getByLabelText("Source clinic"), { target: { value: secondClinic.id } });

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByLabelText("Source clinic")).toHaveValue(secondClinic.id);
    expect(screen.queryByText(cloneContract.preview.body.suggested_name)).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /I understand what will be copied/ })).not.toBeInTheDocument();
  });

  it("reports a successful clone whose lists did not refresh and retries the refresh", async () => {
    clinicsRefreshStatus = 500;
    billingRefreshStatus = 500;
    renderBuilder();
    await loadPreview();
    fireEvent.click(screen.getByRole("checkbox", { name: /I understand what will be copied/ }));
    fireEvent.click(screen.getByRole("button", { name: "Create clone" }));

    await screen.findByText(`ID: ${cloneContract.success.body.id}`);
    expect(screen.getByText("The clone was created, but clinics or billing could not be refreshed.")).toBeInTheDocument();

    clinicsRefreshStatus = 200;
    billingRefreshStatus = 200;
    fireEvent.click(screen.getByRole("button", { name: "Retry refresh" }));
    await waitFor(() => {
      expect(screen.queryByText("The clone was created, but clinics or billing could not be refreshed.")).not.toBeInTheDocument();
    });
    expect(clinicsRequestCount).toBe(3);
    expect(billingRequestCount).toBe(3);
  });

  it.each(Object.entries(cloneContract.errors))(
    "keeps the selected source and preview when POST returns %s",
    async (_statusKey, errorContract) => {
      cloneStatus = errorContract.status;
      cloneBody = errorContract.body;
      renderBuilder();
      await loadPreview();

      fireEvent.click(screen.getByRole("checkbox", { name: /I understand what will be copied/ }));
      fireEvent.click(screen.getByRole("button", { name: "Create clone" }));

      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent(/source selection and preview were kept/i);
      expect(screen.getByLabelText("Source clinic")).toHaveValue(sourceClinic.id);
      expect(screen.getByText(cloneContract.preview.body.suggested_name)).toBeInTheDocument();
      expect(screen.queryByText(cloneContract.success.body.id)).not.toBeInTheDocument();
      expect(servedContracts.at(-1)).toEqual({
        method: cloneContract.success.method,
        path: contractPath(cloneContract.success.path),
        status: errorContract.status,
        body: errorContract.body,
      });
    },
  );

  it.each([
    {
      locale: "en" as const,
      tab: "Cloning Center",
      included: "Configuration, services, professionals, persona, business rules, and knowledge are copied.",
      admin: "No admin user is created.",
      integrations: "API credentials and integrations are zeroed; the scheduling link is disabled and its URL is null.",
      inactive: "The clone starts inactive.",
      rag: "Knowledge documents are duplicated and their bytes count again toward RAG storage.",
    },
    {
      locale: "pt" as const,
      tab: "Central de Clonagem",
      included: "Configuração, serviços, profissionais, persona, regras de negócio e conhecimento são copiados.",
      admin: "Nenhum usuário administrador é criado.",
      integrations: "Credenciais de API e integrações são zeradas; o link de agendamento fica desabilitado e sua URL fica nula.",
      inactive: "A clonagem começa inativa.",
      rag: "Os documentos de conhecimento são duplicados e seus bytes contam novamente no armazenamento RAG.",
    },
  ])("states the included, excluded, inactive, and RAG effects in $locale", async (copy) => {
    renderBuilder(copy.locale);
    await loadPreview();

    expect(screen.getByRole("tab", { name: copy.tab })).toBeInTheDocument();
    expect(screen.getByText(copy.included)).toBeInTheDocument();
    expect(screen.getByText(copy.admin)).toBeInTheDocument();
    expect(screen.getByText(copy.integrations)).toBeInTheDocument();
    expect(screen.getByText(copy.inactive)).toBeInTheDocument();
    expect(screen.getByText(copy.rag)).toBeInTheDocument();
  });

  it("prevents duplicate POSTs and disables cloning interactions while loading", async () => {
    let resolveClone!: (value: Response) => void;
    cloneResponsePromise = new Promise<Response>((resolve) => {
      resolveClone = resolve;
    });
    renderBuilder();
    await loadPreview();
    fireEvent.click(screen.getByRole("checkbox", { name: /I understand what will be copied/ }));

    const submit = screen.getByRole("button", { name: "Create clone" });
    fireEvent.click(submit);
    fireEvent.click(submit);

    const loadingButton = await screen.findByRole("button", { name: "Creating clone…" });
    expect(loadingButton).toBeDisabled();
    expect(screen.getByLabelText("Source clinic")).toBeDisabled();
    expect(servedContracts.filter(({ method }) => method === cloneContract.success.method)).toHaveLength(1);

    await act(async () => {
      resolveClone(response(cloneContract.success.status, cloneContract.success.body));
    });
    await screen.findByText(`ID: ${cloneContract.success.body.id}`);
  });

  it("keeps the existing tabs and clinic creation entry point keyboard-accessible", async () => {
    renderBuilder();
    const overviewTab = await screen.findByRole("tab", { name: "Dashboard" });
    const clinicsTab = screen.getByRole("tab", { name: "Clinics" });
    const cloningTab = screen.getByRole("tab", { name: "Cloning Center" });

    act(() => {
      overviewTab.focus();
      fireEvent.keyDown(overviewTab, { key: "ArrowRight", code: "ArrowRight" });
    });
    await waitFor(() => expect(clinicsTab).toHaveAttribute("aria-selected", "true"));
    expect(clinicsTab).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "New Clinic" }));
    expect(screen.getByText("Provision a new clinic and its first admin user.")).toBeInTheDocument();

    act(() => {
      clinicsTab.focus();
      fireEvent.keyDown(clinicsTab, { key: "ArrowRight", code: "ArrowRight" });
    });
    await waitFor(() => expect(cloningTab).toHaveAttribute("aria-selected", "true"));
    expect(cloningTab).toHaveFocus();
    expect(screen.getByLabelText("Source clinic")).toBeEnabled();
  });

  it("does not expose the Cloning Center outside the Builder panel", async () => {
    mocks.isBuilder = false;
    renderBuilder();
    await act(async () => {});
    expect(mocks.fetch).not.toHaveBeenCalled();
    expect(screen.queryByRole("tab", { name: "Cloning Center" })).not.toBeInTheDocument();
    expect(screen.getByText("Acesso restrito a Builders.")).toBeInTheDocument();
  });
});
