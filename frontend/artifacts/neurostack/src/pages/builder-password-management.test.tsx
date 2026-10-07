import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@/context/i18n";
import BuilderPanel from "./builder";

const mocks = vi.hoisted(() => ({
  logout: vi.fn(),
  setViewingClinicId: vi.fn(),
  toast: vi.fn(),
  fetch: vi.fn(),
}));

vi.mock("@/context/auth", () => ({
  useAuth: () => ({
    isBuilder: true,
    setViewingClinicId: mocks.setViewingClinicId,
    logout: mocks.logout,
  }),
}));
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mocks.toast }),
}));
vi.mock("@/components/plan-usage-summary", () => ({
  PlanUsageSummary: () => null,
}));

const clinic = {
  id: "clinic-42",
  name: "Clinic Aurora",
  slug: null,
  status: "active",
  plan_type: "clinic",
  is_active: true,
  created_at: null,
  last_trace_at: null,
  health_status: "healthy",
  trace_count_7d: 0,
  safety_flags_7d: 0,
  handoff_count_7d: 0,
  outside_hours_count_7d: 0,
  outside_hours_pct_7d: null,
  conversational_trace_count_7d: 0,
  business_hours_configured: false,
  config_completeness: 0,
  contact_phone: null,
  admin_email: "admin@aurora.test",
};

const secondClinic = {
  ...clinic,
  id: "clinic-99",
  name: "Clinic Boreal",
  admin_email: "admin@boreal.test",
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

function ok(data: unknown) {
  return Promise.resolve({ ok: true, json: async () => data } as Response);
}

function error(status: number, data: unknown) {
  return Promise.resolve({ ok: false, status, json: async () => data } as Response);
}

async function openClinicDetails(name = "Clinic Aurora") {
  const clinicsTab = await screen.findByRole("tab", { name: "Clinics" });
  fireEvent.mouseDown(clinicsTab, { button: 0, ctrlKey: false });
  await waitFor(() =>
    expect(clinicsTab).toHaveAttribute("aria-selected", "true"),
  );
  const clinicToggle = await waitFor(() => {
    const heading = screen.getByText(name);
    const card = heading.closest(".border");
    const toggle = card?.querySelector<HTMLButtonElement>(
      'button[data-state="closed"]',
    );
    expect(toggle).toBeDefined();
    return toggle!;
  });
  fireEvent.click(clinicToggle);
}

beforeEach(() => {
  localStorage.setItem("ns_auth", JSON.stringify({ token: "builder-token" }));
  localStorage.setItem("ns_locale", "en");
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  mocks.fetch.mockReset();
  mocks.fetch.mockImplementation((input: string | URL | Request) => {
    const url = String(input);
    if (url === "/v1/builder/me")
      return ok({
        id: "builder",
        name: "Builder",
        status: "active",
        clinic_count: 1,
      });
    if (url === "/v1/builder/dashboard")
      return ok({
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
      });
    if (url === "/v1/builder/clinics") return ok([clinic, secondClinic]);
    if (url === "/v1/billing/usage") return ok(billingUsage);
    if (url === "/v1/builder/tickets") return ok([]);
    if (url === "/v1/builder/profile") return ok({ support_email: null });
    if (url === "/v1/builder/clinics/clinic-42/password")
      return ok({ ok: true });
    if (url === "/v1/builder/clinics/clinic-42/email")
      return ok({ ok: true, admin_email: "stored-admin@aurora.test" });
    if (url === "/v1/builder/clinics/clinic-99/email")
      return ok({ ok: true, admin_email: "stored-admin@boreal.test" });
    throw new Error(`Unexpected fetch: ${url}`);
  });
  vi.stubGlobal("fetch", mocks.fetch);
  mocks.logout.mockReset();
});

describe("BuilderPanel password reset", () => {
  it("targets the clinic selected on its card and never logs out the builder", async () => {
    render(
      <I18nProvider>
        <BuilderPanel />
      </I18nProvider>,
    );
    await openClinicDetails();
    fireEvent.click(
      await screen.findByTestId("btn-change-clinic-password-clinic-42"),
    );
    expect(screen.getByTestId("password-change-target")).toHaveTextContent(
      "Clinic Aurora (clinic-42)",
    );
    const fields = [
      screen.getByLabelText("New password"),
      screen.getByLabelText("Confirm new password"),
    ];
    fireEvent.change(fields[0], { target: { value: "secret" } });
    fireEvent.change(fields[1], { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    await waitFor(() =>
      expect(mocks.fetch).toHaveBeenCalledWith(
        "/v1/builder/clinics/clinic-42/password",
        expect.objectContaining({
          method: "PATCH",
          headers: {
            "Content-Type": "application/json",
            Authorization: "Bearer builder-token",
          },
          body: JSON.stringify({
            new_password: "secret",
            confirm_password: "secret",
          }),
        }),
      ),
    );
    expect(mocks.logout).not.toHaveBeenCalled();
  });
});

describe("BuilderPanel clinic login email", () => {
  it("targets the selected clinic once and syncs the stored server value", async () => {
    render(
      <I18nProvider>
        <BuilderPanel />
      </I18nProvider>,
    );
    await openClinicDetails("Clinic Boreal");

    const emailInput = await screen.findByTestId("input-clinic-email-clinic-99");
    expect(emailInput).toHaveValue("admin@boreal.test");
    fireEvent.change(emailInput, { target: { value: "draft@boreal.test" } });
    const save = screen.getByTestId("btn-save-clinic-email-clinic-99");
    fireEvent.click(save);
    fireEvent.click(save);

    await waitFor(() =>
      expect(emailInput).toHaveValue("stored-admin@boreal.test"),
    );
    const emailCalls = mocks.fetch.mock.calls.filter(
      ([input]) => String(input) === "/v1/builder/clinics/clinic-99/email",
    );
    expect(emailCalls).toHaveLength(1);
    expect(emailCalls[0][1]).toEqual(expect.objectContaining({
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer builder-token",
      },
      body: JSON.stringify({ email: "draft@boreal.test" }),
    }));
    expect(mocks.logout).not.toHaveBeenCalled();
  });

  it("keeps the editor open with its draft when the server rejects the email", async () => {
    render(
      <I18nProvider>
        <BuilderPanel />
      </I18nProvider>,
    );
    await openClinicDetails();
    const emailInput = await screen.findByLabelText("Clinic login email");
    fireEvent.change(emailInput, { target: { value: "duplicate@aurora.test" } });
    mocks.fetch.mockImplementationOnce(() =>
      error(409, { detail: { error: "email_already_in_use" } }),
    );

    fireEvent.click(screen.getByTestId("btn-save-clinic-email-clinic-42"));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not save the clinic login email",
    );
    expect(screen.getByLabelText("Clinic login email")).toHaveValue(
      "duplicate@aurora.test",
    );
    expect(mocks.logout).not.toHaveBeenCalled();
  });
});
