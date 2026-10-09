import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider, type Locale } from "@/context/i18n";
import cloneContract from "../../../../../tests/fixtures/clinic_clone_contract.json";
import BuilderPanel from "./builder";

const CONSENT_PT = "Estou ciente dos itens que serão copiados e zerados. E compreendo que uma revisão dos dados para a nova clínica deve ser feita antes de ativar o agente.";
const CONSENT_EN = "I am aware of the items that will be copied and reset. I also understand that the new clinic's data must be reviewed before activating the agent.";
const preview = cloneContract.preview.body;

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
let cloneRequestBodies: unknown[];

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

function consentCheckbox() {
  return screen.getByRole("checkbox", { name: CONSENT_EN });
}

function ragCheckbox() {
  return screen.getByRole("checkbox", { name: "RAG" });
}

function mediaCheckbox() {
  return screen.getByRole("checkbox", { name: "Media" });
}

function submitButton() {
  return screen.getByRole("button", { name: "Create clone" });
}

function summarySection(title: string) {
  return screen.getByRole("region", { name: title });
}

function previewRequestCount() {
  return mocks.fetch.mock.calls.filter(([path]) => String(path).endsWith("/clone-preview")).length;
}

function lastCloneRequest() {
  const body = cloneRequestBodies.at(-1);
  return typeof body === "string" ? JSON.parse(body) as unknown : body;
}

function definitionFor(term: string) {
  return screen.getByText(term).nextElementSibling;
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
  cloneRequestBodies = [];

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
    if (/^\/v1\/builder\/clinics\/[^/]+\/clone$/.test(path)) {
      servedContracts.push({ method, path, status: cloneStatus, body: cloneBody });
      cloneRequestBodies.push(init?.body);
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
      copied_rag_documents: cloneContract.success.body.copied_rag_documents,
      copied_media_assets: cloneContract.success.body.copied_media_assets,
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
    expect(ragCheckbox()).toHaveAccessibleDescription(`${preview.document_count} documents · ${preview.total_bytes} B available`);
    expect(mediaCheckbox()).toHaveAccessibleDescription(`${preview.media_asset_count} files · ${preview.media_total_bytes} B available`);

    fireEvent.click(ragCheckbox());
    fireEvent.click(mediaCheckbox());
    fireEvent.click(consentCheckbox());
    fireEvent.click(submitButton());

    await screen.findByText(`ID: ${authoritativeResult.id}`);
    expect(servedContracts[1]).toEqual({
      method: cloneContract.success.method,
      path: contractPath(cloneContract.success.path),
      status: cloneContract.success.status,
      body: authoritativeResult,
    });
    expect(lastCloneRequest()).toEqual(cloneContract.success.request);
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
      media_asset_count: 4,
      media_total_bytes: 654,
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
    expect(ragCheckbox()).toHaveAccessibleDescription("7 documents · 987 B available");
    expect(mediaCheckbox()).toHaveAccessibleDescription("4 files · 654 B available");
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
    fireEvent.click(consentCheckbox());
    previewResponses.set(secondClinic.id, () => Promise.resolve(response(status, { detail: { error: "preview_failed" } })));

    fireEvent.change(screen.getByLabelText("Source clinic"), { target: { value: secondClinic.id } });

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByLabelText("Source clinic")).toHaveValue(secondClinic.id);
    expect(screen.queryByText(cloneContract.preview.body.suggested_name)).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: CONSENT_EN })).not.toBeInTheDocument();
  });

  it("reports a successful clone whose lists did not refresh and retries the refresh", async () => {
    clinicsRefreshStatus = 500;
    billingRefreshStatus = 500;
    renderBuilder();
    await loadPreview();
    fireEvent.click(consentCheckbox());
    fireEvent.click(submitButton());

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

  const postErrorMessages: Record<string, string> = {
    403: "You no longer have permission to clone this clinic. Your source selection and preview were kept.",
    404: "The source clinic could not be found. Your source selection and preview were kept.",
    409: "The clinic cannot be cloned with its current configuration. Your source selection and preview were kept.",
    "409_media_storage_cap": "Copying the media would exceed your media storage limit. No clinic was created; unselect Media or free up space. Your source selection and preview were kept.",
    422: "The clone selection could not be processed. No clinic was created; your source selection and preview were kept.",
    500: "The clone could not be saved. No clinic was created; your source selection and preview were kept.",
  };

  it.each(Object.entries(cloneContract.errors))(
    "keeps the selected source, selection, and preview when POST returns %s",
    async (statusKey, errorContract) => {
      cloneStatus = errorContract.status;
      cloneBody = errorContract.body;
      renderBuilder();
      await loadPreview();

      fireEvent.click(ragCheckbox());
      fireEvent.click(mediaCheckbox());
      fireEvent.click(consentCheckbox());
      fireEvent.click(submitButton());

      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent(postErrorMessages[statusKey]);
      expect(screen.getByLabelText("Source clinic")).toHaveValue(sourceClinic.id);
      expect(ragCheckbox()).toBeChecked();
      expect(mediaCheckbox()).toBeChecked();
      expect(screen.getByText(cloneContract.preview.body.suggested_name)).toBeInTheDocument();
      expect(screen.queryByText(cloneContract.success.body.id)).not.toBeInTheDocument();
      expect(servedContracts.at(-1)).toEqual({
        method: cloneContract.success.method,
        path: contractPath(cloneContract.success.path),
        status: errorContract.status,
        body: errorContract.body,
      });
      expect(lastCloneRequest()).toEqual({ include_rag: true, include_media: true });
    },
  );

  it("derives the retry request from the current selection after the media storage cap error", async () => {
    cloneStatus = cloneContract.errors["409_media_storage_cap"].status;
    cloneBody = cloneContract.errors["409_media_storage_cap"].body;
    renderBuilder();
    await loadPreview();
    fireEvent.click(ragCheckbox());
    fireEvent.click(mediaCheckbox());
    fireEvent.click(consentCheckbox());
    fireEvent.click(submitButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(/exceed your media storage limit/);

    fireEvent.click(mediaCheckbox());
    // The error described the previous selection, so it leaves with it.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(consentCheckbox()).not.toBeChecked();
    expect(submitButton()).toBeDisabled();

    cloneStatus = cloneContract.success.status;
    cloneBody = { ...cloneContract.success.body, copied_media_assets: 0 };
    fireEvent.click(consentCheckbox());
    fireEvent.click(submitButton());

    await screen.findByText(`ID: ${cloneContract.success.body.id}`);
    expect(lastCloneRequest()).toEqual({ include_rag: true, include_media: false });
    expect(definitionFor("Media files copied")).toHaveTextContent("0");
  });

  it("renders configuration locked with its note and RAG/Media unchecked by default", async () => {
    renderBuilder();
    await loadPreview();

    expect(screen.getByRole("group", { name: "Clone" })).toBeInTheDocument();
    const config = screen.getByRole("checkbox", { name: "Clinic configuration" });
    expect(config).toBeChecked();
    expect(config).toBeDisabled();
    expect(config).toHaveAccessibleDescription("except APIs and scheduling link");
    expect(ragCheckbox()).not.toBeChecked();
    expect(ragCheckbox()).toBeEnabled();
    expect(mediaCheckbox()).not.toBeChecked();
    expect(mediaCheckbox()).toBeEnabled();
    expect(consentCheckbox()).not.toBeChecked();
    expect(submitButton()).toBeDisabled();

    const copied = summarySection("Will be copied");
    expect(within(copied).getByText("Clinic configuration: services, professionals, persona, and business rules.")).toBeInTheDocument();
    expect(within(copied).queryByText(/^RAG:/)).not.toBeInTheDocument();
    expect(within(copied).queryByText(/^Media:/)).not.toBeInTheDocument();
    const reset = summarySection("Will be reset");
    expect(within(reset).getByText("API credentials and integrations are zeroed; the scheduling link is disabled and its URL is null.")).toBeInTheDocument();
    expect(within(reset).getByText("No admin user is created.")).toBeInTheDocument();
    expect(within(reset).getByText("The clone starts inactive.")).toBeInTheDocument();
    const unselected = summarySection("Not selected");
    expect(within(unselected).getByText(`RAG: ${preview.document_count} documents (${preview.total_bytes} B)`)).toBeInTheDocument();
    expect(within(unselected).getByText(`Media: ${preview.media_asset_count} files (${preview.media_total_bytes} B)`)).toBeInTheDocument();
    expect(within(unselected).getByText("Unselected items are not copied and stay unchanged in the source clinic.")).toBeInTheDocument();
    expect(definitionFor("RAG storage added")).toHaveTextContent("0 B");
    expect(definitionFor("Media storage added")).toHaveTextContent("0 B");
  });

  it("moves selected parts into the copied summary and computes the impact locally without refetching", async () => {
    renderBuilder();
    await loadPreview();
    expect(previewRequestCount()).toBe(1);

    fireEvent.click(ragCheckbox());
    expect(within(summarySection("Will be copied")).getByText(`RAG: ${preview.document_count} documents (${preview.total_bytes} B)`)).toBeInTheDocument();
    expect(within(summarySection("Not selected")).queryByText(/^RAG:/)).not.toBeInTheDocument();
    expect(definitionFor("RAG storage added")).toHaveTextContent(`${preview.total_bytes} B`);
    expect(screen.getByText("Knowledge documents are duplicated and their bytes count again toward RAG storage.")).toBeInTheDocument();

    fireEvent.click(mediaCheckbox());
    expect(within(summarySection("Will be copied")).getByText(`Media: ${preview.media_asset_count} files (${preview.media_total_bytes} B)`)).toBeInTheDocument();
    expect(within(summarySection("Not selected")).getByText("All optional items are selected.")).toBeInTheDocument();
    expect(definitionFor("Media storage added")).toHaveTextContent(`${preview.media_total_bytes} B`);
    expect(screen.getByText("Media files are duplicated and their bytes count again toward media storage.")).toBeInTheDocument();

    fireEvent.click(ragCheckbox());
    fireEvent.click(mediaCheckbox());
    expect(definitionFor("RAG storage added")).toHaveTextContent("0 B");
    expect(definitionFor("Media storage added")).toHaveTextContent("0 B");
    expect(previewRequestCount()).toBe(1);
  });

  it.each([
    { includeRag: false, includeMedia: false, copiedRag: 0, copiedMedia: 0 },
    { includeRag: true, includeMedia: false, copiedRag: 5, copiedMedia: 0 },
    { includeRag: false, includeMedia: true, copiedRag: 0, copiedMedia: 7 },
    { includeRag: true, includeMedia: true, copiedRag: 5, copiedMedia: 7 },
  ])(
    "sends include_rag=$includeRag include_media=$includeMedia and shows the authoritative copied counts",
    async ({ includeRag, includeMedia, copiedRag, copiedMedia }) => {
      cloneBody = {
        ...cloneContract.success.body,
        copied_rag_documents: copiedRag,
        copied_media_assets: copiedMedia,
      };
      renderBuilder();
      await loadPreview();

      if (includeRag) fireEvent.click(ragCheckbox());
      if (includeMedia) fireEvent.click(mediaCheckbox());
      fireEvent.click(consentCheckbox());
      fireEvent.click(submitButton());

      await screen.findByText(`ID: ${cloneContract.success.body.id}`);
      expect(cloneRequestBodies).toHaveLength(1);
      expect(lastCloneRequest()).toEqual({ include_rag: includeRag, include_media: includeMedia });
      expect(within(screen.getByRole("status")).getByText("RAG documents copied").nextElementSibling)
        .toHaveTextContent(new RegExp(`^${copiedRag}$`));
      expect(within(screen.getByRole("status")).getByText("Media files copied").nextElementSibling)
        .toHaveTextContent(new RegExp(`^${copiedMedia}$`));
    },
  );

  it.each([
    { trigger: "RAG", change: () => fireEvent.click(ragCheckbox()), request: { include_rag: true, include_media: false } },
    { trigger: "Media", change: () => fireEvent.click(mediaCheckbox()), request: { include_rag: false, include_media: true } },
  ])("invalidates consent when $trigger is toggled and submits the current selection", async ({ change, request }) => {
    renderBuilder();
    await loadPreview();
    fireEvent.click(consentCheckbox());
    expect(consentCheckbox()).toBeChecked();
    expect(submitButton()).toBeEnabled();

    change();
    expect(consentCheckbox()).not.toBeChecked();
    expect(submitButton()).toBeDisabled();
    fireEvent.click(submitButton());

    // Toggling back does not resurrect the earlier acknowledgement.
    change();
    expect(consentCheckbox()).not.toBeChecked();
    expect(submitButton()).toBeDisabled();
    change();

    fireEvent.click(consentCheckbox());
    expect(submitButton()).toBeEnabled();
    fireEvent.click(submitButton());

    await screen.findByText(`ID: ${cloneContract.success.body.id}`);
    expect(cloneRequestBodies).toHaveLength(1);
    expect(lastCloneRequest()).toEqual(request);
    expect(previewRequestCount()).toBe(1);
  });

  it("invalidates consent and resets RAG/Media when the source changes", async () => {
    availableClinics = [sourceClinic, secondClinic];
    renderBuilder();
    await loadPreview();
    fireEvent.click(ragCheckbox());
    fireEvent.click(mediaCheckbox());
    fireEvent.click(consentCheckbox());
    expect(submitButton()).toBeEnabled();

    fireEvent.change(screen.getByLabelText("Source clinic"), { target: { value: secondClinic.id } });
    await screen.findByText(cloneContract.preview.body.suggested_name);
    expect(ragCheckbox()).not.toBeChecked();
    expect(mediaCheckbox()).not.toBeChecked();
    expect(consentCheckbox()).not.toBeChecked();
    expect(submitButton()).toBeDisabled();

    fireEvent.click(ragCheckbox());
    fireEvent.click(consentCheckbox());
    fireEvent.click(submitButton());

    await screen.findByText(`ID: ${cloneContract.success.body.id}`);
    expect(cloneRequestBodies).toHaveLength(1);
    expect(servedContracts.at(-1)?.path).toBe(`/v1/builder/clinics/${secondClinic.id}/clone`);
    expect(lastCloneRequest()).toEqual({ include_rag: true, include_media: false });
  });

  it.each([
    {
      locale: "en" as const,
      tab: "Cloning Center",
      clone: "Clone",
      config: "Clinic configuration",
      note: "except APIs and scheduling link",
      media: "Media",
      copied: "Will be copied",
      reset: "Will be reset",
      unselected: "Not selected",
      resetItems: [
        "API credentials and integrations are zeroed; the scheduling link is disabled and its URL is null.",
        "No admin user is created.",
        "The clone starts inactive.",
      ],
      consent: CONSENT_EN,
    },
    {
      locale: "pt" as const,
      tab: "Central de Clonagem",
      clone: "Clonar",
      config: "Configuração da clínica",
      note: "exceto APIs e link de agendamento",
      media: "Mídias",
      copied: "Serão copiados",
      reset: "Serão zerados",
      unselected: "Não selecionados",
      resetItems: [
        "Credenciais de API e integrações são zeradas; o link de agendamento fica desabilitado e sua URL fica nula.",
        "Nenhum usuário administrador é criado.",
        "A clonagem começa inativa.",
      ],
      consent: CONSENT_PT,
    },
  ])("renders the clone list, summary groups, and exact consent text in $locale", async (copy) => {
    renderBuilder(copy.locale);
    await loadPreview();

    expect(screen.getByRole("tab", { name: copy.tab })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: copy.clone })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: copy.config })).toHaveAccessibleDescription(copy.note);
    expect(screen.getByRole("checkbox", { name: "RAG" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: copy.media })).not.toBeChecked();
    expect(summarySection(copy.copied)).toBeInTheDocument();
    const reset = summarySection(copy.reset);
    for (const item of copy.resetItems) {
      expect(within(reset).getByText(item)).toBeInTheDocument();
    }
    expect(summarySection(copy.unselected)).toBeInTheDocument();
    const consent = screen.getByRole("checkbox", { name: copy.consent });
    expect(screen.getByText(copy.consent).textContent).toBe(copy.consent);
    expect(consent).not.toBeChecked();
  });

  it("shows the media storage cap error in pt", async () => {
    const capError = cloneContract.errors["409_media_storage_cap"];
    cloneStatus = capError.status;
    cloneBody = capError.body;
    renderBuilder("pt");
    await loadPreview();

    fireEvent.click(screen.getByRole("checkbox", { name: "Mídias" }));
    fireEvent.click(screen.getByRole("checkbox", { name: CONSENT_PT }));
    fireEvent.click(screen.getByRole("button", { name: "Criar clonagem" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Copiar as mídias excederia o limite de armazenamento de mídias. Nenhuma clínica foi criada; desmarque Mídias ou libere espaço. Sua seleção e prévia foram mantidas.",
    );
    expect(lastCloneRequest()).toEqual({ include_rag: false, include_media: true });
  });

  it("prevents duplicate POSTs and disables cloning interactions while loading", async () => {
    let resolveClone!: (value: Response) => void;
    cloneResponsePromise = new Promise<Response>((resolve) => {
      resolveClone = resolve;
    });
    renderBuilder();
    await loadPreview();
    fireEvent.click(consentCheckbox());

    // Both clicks land in the same tick, before React re-renders and disables
    // the button, so only the in-flight guard can stop the second POST.
    const submit = submitButton();
    act(() => {
      submit.click();
      submit.click();
    });

    const loadingButton = await screen.findByRole("button", { name: "Creating clone…" });
    expect(loadingButton).toBeDisabled();
    expect(loadingButton).toHaveAttribute("aria-busy", "true");
    // Exactly one live announcement for the loading phase, and the clone list
    // itself is not a live region (it would re-announce on every toggle).
    const announcements = screen.getAllByRole("status")
      .filter((region) => region.textContent?.includes("Creating clone…"));
    expect(announcements).toHaveLength(1);
    expect(document.querySelectorAll("[aria-live]")).toHaveLength(0);
    const cloneList = screen.getByRole("group", { name: "Clone" });
    expect(cloneList.closest("[aria-live], [role='status'], [role='alert']")).toBeNull();
    expect(summarySection("Will be copied").closest("[aria-live], [role='status'], [role='alert']")).toBeNull();
    expect(screen.getByLabelText("Source clinic")).toBeDisabled();
    expect(ragCheckbox()).toBeDisabled();
    expect(mediaCheckbox()).toBeDisabled();
    expect(consentCheckbox()).toBeDisabled();
    expect(servedContracts.filter(({ method }) => method === cloneContract.success.method)).toHaveLength(1);

    await act(async () => {
      resolveClone(response(cloneContract.success.status, cloneContract.success.body));
    });
    await screen.findByText(`ID: ${cloneContract.success.body.id}`);
    expect(definitionFor("RAG documents copied")).toHaveTextContent(String(cloneContract.success.body.copied_rag_documents));
    expect(definitionFor("Media files copied")).toHaveTextContent(String(cloneContract.success.body.copied_media_assets));
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
