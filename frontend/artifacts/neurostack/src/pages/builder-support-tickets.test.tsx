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
    email: "builder-login@example.com",
  }),
}));
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mocks.toast }),
}));
vi.mock("@/components/plan-usage-summary", () => ({
  PlanUsageSummary: () => null,
}));

const clinicWithPhone = {
  id: "clinic-1",
  name: "Clinic With Phone",
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
  contact_phone: "5511987654321",
};

const clinicWithoutPhone = {
  ...clinicWithPhone,
  id: "clinic-2",
  name: "Clinic No Phone",
  contact_phone: null,
};

const openTicket = {
  id: 101,
  clinic_id: "clinic-1",
  clinic_name: "Clinic With Phone",
  contact_phone: "5511987654321",
  email: "user@clinic.com",
  tipo: "bug",
  mensagem: "Chat is broken",
  filename: null,
  file_type: null,
  status: "open",
  created_at: "2026-09-01T10:00:00Z",
};

const openTicketNoPhone = {
  id: 102,
  clinic_id: "clinic-2",
  clinic_name: "Clinic No Phone",
  contact_phone: null,
  email: "user2@clinic.com",
  tipo: "feature",
  mensagem: "Please add reports",
  filename: null,
  file_type: null,
  status: "open",
  created_at: "2026-09-02T11:00:00Z",
};

const billingUsage = {
  plan: "builder",
  as_of: "2026-09-05T12:00:00Z",
  trace_period: { start: "2026-09-01T00:00:00Z", end: "2026-09-30T23:59:59Z", timezone: "UTC" },
  traces: { used: 0, included: 100, extra_credits: 0, available: 100, overage: 0, percent_used: 0 },
  storage: {
    rag: {
      used_bytes: 0, used_mb: 0, limit_bytes: 1048576, limit_mb: 1,
      available_bytes: 1048576, available_mb: 1, overage_bytes: 0, overage_mb: 0, percent_used: 0,
    },
    media: {
      used_bytes: 0, used_mb: 0, limit_bytes: 1048576, limit_mb: 1,
      available_bytes: 1048576, available_mb: 1, overage_bytes: 0, overage_mb: 0, percent_used: 0,
    },
  },
  credits: { purchase_enabled: false, coming_soon: true },
};

function ok(data: unknown) {
  return Promise.resolve({ ok: true, json: async () => data } as Response);
}

function fail(status: number, data: unknown = {}) {
  return Promise.resolve({ ok: false, status, json: async () => data } as Response);
}

type FetchInit = { method?: string; body?: string; headers?: Record<string, string> };

type BuildOpts = {
  clinics?: unknown[];
  tickets?: unknown[];
  supportEmail?: string | null;
  ticketStatusOverride?: (id: string, body: string) => Promise<Response>;
  profilePatchOverride?: (body: string) => Promise<Response>;
};

function installFetch(opts: BuildOpts = {}) {
  const clinics = opts.clinics ?? [clinicWithPhone, clinicWithoutPhone];
  const tickets = opts.tickets ?? [openTicket, openTicketNoPhone];
  const supportEmail = opts.supportEmail ?? null;

  mocks.fetch.mockImplementation((input: string | URL | Request, init?: FetchInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (url === "/v1/builder/me" && method === "GET")
      return ok({
        id: "builder",
        name: "Builder",
        status: "active",
        clinic_count: clinics.length,
        support_email: supportEmail,
      });
    if (url === "/v1/builder/dashboard" && method === "GET")
      return ok({
        stats: {
          clinic_count: clinics.length,
          active_clinics: clinics.length,
          inactive_clinics: 0,
          total_traces: 0,
          total_tokens: 0,
          model_breakdown: [],
        },
        time_series: [],
        alerts: [],
      });
    if (url === "/v1/builder/clinics" && method === "GET") return ok(clinics);
    if (url === "/v1/billing/usage" && method === "GET") return ok(billingUsage);
    if (url === "/v1/builder/tickets" && method === "GET") return ok(tickets);
    if (url === "/v1/builder/profile" && method === "GET")
      return ok({ support_email: supportEmail });
    if (url === "/v1/builder/profile" && method === "PATCH") {
      if (opts.profilePatchOverride) return opts.profilePatchOverride(init?.body ?? "");
      const body = JSON.parse(init?.body ?? "{}") as { support_email: string | null };
      return ok({ support_email: body.support_email });
    }
    const ticketPatchMatch = url.match(/^\/v1\/builder\/tickets\/(\d+)\/status$/);
    if (ticketPatchMatch && method === "PATCH") {
      if (opts.ticketStatusOverride) return opts.ticketStatusOverride(ticketPatchMatch[1], init?.body ?? "");
      return ok({ ok: true });
    }
    throw new Error(`Unexpected fetch: ${method} ${url}`);
  });
}

async function openSupportTab() {
  const tab = await screen.findByRole("tab", { name: "Support" });
  fireEvent.mouseDown(tab, { button: 0, ctrlKey: false });
  await waitFor(() => expect(tab).toHaveAttribute("aria-selected", "true"));
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
  mocks.toast.mockReset();
  installFetch();
  vi.stubGlobal("fetch", mocks.fetch);
  mocks.logout.mockReset();
});

describe("BuilderPanel support tab", () => {
  it("renders tickets from the fetched list", async () => {
    render(
      <I18nProvider>
        <BuilderPanel />
      </I18nProvider>,
    );
    await openSupportTab();
    expect(await screen.findByTestId("ticket-row-101")).toBeInTheDocument();
    expect(screen.getByTestId("ticket-row-102")).toBeInTheDocument();
    expect(screen.getByText("Chat is broken")).toBeInTheDocument();
    expect(screen.getByText("Please add reports")).toBeInTheDocument();
  });

  it("shows the empty state when no tickets match the filter", async () => {
    installFetch({ tickets: [] });
    render(
      <I18nProvider>
        <BuilderPanel />
      </I18nProvider>,
    );
    await openSupportTab();
    expect(await screen.findByTestId("support-empty")).toHaveTextContent(
      "No tickets right now.",
    );
  });

  it("resolves a ticket optimistically and sends the right PATCH body", async () => {
    render(
      <I18nProvider>
        <BuilderPanel />
      </I18nProvider>,
    );
    await openSupportTab();
    // Switch to "all" so the ticket stays rendered after status flips to closed
    fireEvent.click(await screen.findByTestId("btn-ticket-filter-all"));
    const status = await screen.findByTestId("ticket-status-101");
    expect(status).toHaveTextContent("Open");
    fireEvent.click(screen.getByTestId("btn-ticket-toggle-101"));
    // Optimistic: status flips before await resolves
    await waitFor(() =>
      expect(screen.getByTestId("ticket-status-101")).toHaveTextContent("Resolved"),
    );
    await waitFor(() =>
      expect(mocks.fetch).toHaveBeenCalledWith(
        "/v1/builder/tickets/101/status",
        expect.objectContaining({
          method: "PATCH",
          body: JSON.stringify({ status: "closed" }),
        }),
      ),
    );
  });

  it("reverts the optimistic status change and toasts an error when the PATCH fails", async () => {
    installFetch({
      ticketStatusOverride: () => fail(500, { error: "boom" }),
    });
    render(
      <I18nProvider>
        <BuilderPanel />
      </I18nProvider>,
    );
    await openSupportTab();
    fireEvent.click(await screen.findByTestId("btn-ticket-filter-all"));
    const status = await screen.findByTestId("ticket-status-101");
    expect(status).toHaveTextContent("Open");
    fireEvent.click(screen.getByTestId("btn-ticket-toggle-101"));
    await waitFor(() =>
      expect(mocks.toast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Could not update ticket",
          variant: "destructive",
        }),
      ),
    );
    // After failure, the row is still there and reverted to "Open"
    expect(screen.getByTestId("ticket-status-101")).toHaveTextContent("Open");
  });

  it("shows a WhatsApp link when contact_phone is present and a hint when absent", async () => {
    render(
      <I18nProvider>
        <BuilderPanel />
      </I18nProvider>,
    );
    await openSupportTab();
    const link = await screen.findByTestId("link-ticket-whatsapp-101");
    expect(link).toHaveAttribute("href", "https://wa.me/5511987654321");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.queryByTestId("link-ticket-whatsapp-102")).toBeNull();
    expect(screen.getByTestId("ticket-whatsapp-hint-102")).toHaveTextContent(
      "No phone on file",
    );
  });

  it("shows the login-email fallback when support_email is null", async () => {
    render(
      <I18nProvider>
        <BuilderPanel />
      </I18nProvider>,
    );
    await openSupportTab();
    const fallback = await screen.findByTestId("support-email-fallback");
    expect(fallback).toHaveTextContent("builder-login@example.com");
  });

  it("does NOT show the fallback when support_email is set", async () => {
    installFetch({ supportEmail: "support@team.com" });
    render(
      <I18nProvider>
        <BuilderPanel />
      </I18nProvider>,
    );
    await openSupportTab();
    // Wait until the input is populated with the fetched value
    await waitFor(() => {
      const input = screen.getByTestId("input-support-email") as HTMLInputElement;
      expect(input.value).toBe("support@team.com");
    });
    expect(screen.queryByTestId("support-email-fallback")).toBeNull();
  });

  it("saves the notification email with the right PATCH body", async () => {
    render(
      <I18nProvider>
        <BuilderPanel />
      </I18nProvider>,
    );
    await openSupportTab();
    const input = (await screen.findByTestId("input-support-email")) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "notify@team.com" } });
    fireEvent.click(screen.getByTestId("btn-save-support-email"));
    await waitFor(() =>
      expect(mocks.fetch).toHaveBeenCalledWith(
        "/v1/builder/profile",
        expect.objectContaining({
          method: "PATCH",
          body: JSON.stringify({ support_email: "notify@team.com" }),
        }),
      ),
    );
    await waitFor(() =>
      expect(mocks.toast).toHaveBeenCalledWith(
        expect.objectContaining({ title: "Notification email updated" }),
      ),
    );
  });

  it("sends null when the notification email is cleared", async () => {
    installFetch({ supportEmail: "prev@team.com" });
    render(
      <I18nProvider>
        <BuilderPanel />
      </I18nProvider>,
    );
    await openSupportTab();
    const input = (await screen.findByTestId("input-support-email")) as HTMLInputElement;
    await waitFor(() => expect(input.value).toBe("prev@team.com"));
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.click(screen.getByTestId("btn-save-support-email"));
    await waitFor(() =>
      expect(mocks.fetch).toHaveBeenCalledWith(
        "/v1/builder/profile",
        expect.objectContaining({
          method: "PATCH",
          body: JSON.stringify({ support_email: null }),
        }),
      ),
    );
  });
});
