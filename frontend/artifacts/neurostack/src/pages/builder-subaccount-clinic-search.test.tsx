import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@/context/i18n";
import BuilderPanel from "./builder";

const mocks = vi.hoisted(() => ({
  fetch: vi.fn(),
  setViewingClinicId: vi.fn(),
  toast: vi.fn(),
}));

vi.mock("@/context/auth", () => ({
  useAuth: () => ({
    isBuilder: true,
    setViewingClinicId: mocks.setViewingClinicId,
    email: "builder@example.com",
  }),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mocks.toast }),
}));

vi.mock("@/components/plan-usage-summary", () => ({
  PlanUsageSummary: () => null,
}));

const clinicBase = {
  slug: null,
  status: "active" as const,
  plan_type: "clinic",
  is_active: true,
  created_at: null,
  last_trace_at: null,
  safety_flags_7d: 0,
  handoff_count_7d: 0,
  outside_hours_count_7d: 0,
  outside_hours_pct_7d: null,
  conversational_trace_count_7d: 0,
  business_hours_configured: false,
  config_completeness: 100,
  contact_phone: null,
};

const clinics = [
  {
    ...clinicBase,
    id: "healthy-aurora",
    name: "Clínica Aurora",
    health_status: "healthy" as const,
    trace_count_7d: 3,
  },
  {
    ...clinicBase,
    id: "critical-sao-jose",
    name: "Clínica São José",
    health_status: "critical" as const,
    trace_count_7d: 31,
  },
  {
    ...clinicBase,
    id: "warning-acao",
    name: "Clínica Ação",
    health_status: "warning" as const,
    trace_count_7d: 12,
  },
];

const billingUsage = {
  plan: "builder",
  as_of: "2026-10-07T12:00:00Z",
  trace_period: {
    start: "2026-10-01T00:00:00Z",
    end: "2026-10-31T23:59:59Z",
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

let availableClinics: typeof clinics;

function ok(data: unknown) {
  return Promise.resolve({ ok: true, json: async () => data } as Response);
}

function renderBuilder() {
  return render(
    <I18nProvider>
      <BuilderPanel />
    </I18nProvider>,
  );
}

async function openSubaccounts() {
  const tab = await screen.findByRole("tab", { name: "Clinics" });
  fireEvent.mouseDown(tab, { button: 0, ctrlKey: false });
  await waitFor(() => expect(tab).toHaveAttribute("aria-selected", "true"));
}

function expectBefore(first: HTMLElement, second: HTMLElement) {
  expect(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("ns_auth", JSON.stringify({ token: "builder-token" }));
  localStorage.setItem("ns_locale", "en");
  availableClinics = clinics;
  mocks.fetch.mockReset();
  mocks.setViewingClinicId.mockReset();
  mocks.toast.mockReset();

  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );

  mocks.fetch.mockImplementation((input: string | URL | Request) => {
    const url = String(input);
    if (url === "/v1/builder/me") {
      return ok({
        id: "builder",
        name: "Builder",
        status: "active",
        clinic_count: availableClinics.length,
      });
    }
    if (url === "/v1/builder/dashboard") {
      return ok({
        stats: {
          clinic_count: availableClinics.length,
          active_clinics: availableClinics.length,
          inactive_clinics: 0,
          total_traces: 0,
          total_tokens: 0,
          model_breakdown: [],
        },
        time_series: [],
        alerts: [],
      });
    }
    if (url === "/v1/builder/clinics") return ok(availableClinics);
    if (url === "/v1/billing/usage") return ok(billingUsage);
    if (url === "/v1/builder/tickets") return ok([]);
    if (url === "/v1/builder/profile") return ok({ support_email: null });
    throw new Error(`Unexpected fetch: ${url}`);
  });
  vi.stubGlobal("fetch", mocks.fetch);
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
});

describe("Builder Sub-Accounts clinic search", () => {
  it("filters normalized names without requests or automatic expansion and preserves health order", async () => {
    renderBuilder();
    await openSubaccounts();

    const search = screen.getByRole("searchbox", { name: "Search clinics" });
    expect(search).toHaveAttribute("placeholder", "Search by clinic name");
    expect(screen.queryByText("No clinics registered")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Total Clinics 3" })).toBeInTheDocument();

    const critical = screen.getByText("Clínica São José");
    const warning = screen.getByText("Clínica Ação");
    const healthy = screen.getByText("Clínica Aurora");
    expectBefore(critical, warning);
    expectBefore(warning, healthy);

    fireEvent.change(search, { target: { value: "  SAO jo  " } });
    expect(screen.getByText("Clínica São José")).toBeInTheDocument();
    expect(screen.queryByText("Clínica Ação")).not.toBeInTheDocument();
    expect(screen.queryByText("Clínica Aurora")).not.toBeInTheDocument();

    fireEvent.change(search, { target: { value: "CLINICA" } });
    expectBefore(screen.getByText("Clínica São José"), screen.getByText("Clínica Ação"));
    expectBefore(screen.getByText("Clínica Ação"), screen.getByText("Clínica Aurora"));

    fireEvent.change(search, { target: { value: "aurora" } });
    const clinicToggle = screen
      .getAllByRole("button")
      .find((button) => button.dataset.state === "closed");
    expect(clinicToggle).toBeDefined();
    expect(screen.queryByTestId("input-clinic-phone-healthy-aurora")).not.toBeInTheDocument();
    fireEvent.click(clinicToggle!);
    expect(await screen.findByTestId("input-clinic-phone-healthy-aurora")).toBeInTheDocument();
    expect(screen.getByText("3 traces (7d)")).toBeInTheDocument();

    fireEvent.change(search, { target: { value: "missing clinic" } });
    expect(screen.getByText("No clinics found")).toBeInTheDocument();
    expect(screen.queryByText("No clinics registered")).not.toBeInTheDocument();

    fireEvent.change(search, { target: { value: "   " } });
    expect(screen.queryByText("No clinics found")).not.toBeInTheDocument();
    expectBefore(screen.getByText("Clínica São José"), screen.getByText("Clínica Ação"));
    expectBefore(screen.getByText("Clínica Ação"), screen.getByText("Clínica Aurora"));
    expect(mocks.fetch).toHaveBeenCalledTimes(6);
    expect(mocks.setViewingClinicId).not.toHaveBeenCalled();
  });

  it("keeps search absent and shows the existing empty state when there are no clinics", async () => {
    availableClinics = [];
    renderBuilder();
    await openSubaccounts();

    expect(screen.queryByRole("searchbox", { name: "Search clinics" })).not.toBeInTheDocument();
    expect(screen.getByText("No clinics registered")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "New Clinic" })).toHaveLength(2);
    expect(screen.queryByText("No clinics found")).not.toBeInTheDocument();
  });
});
