import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

import { I18nProvider } from "@/context/i18n";

// ── Mocks ────────────────────────────────────────────────────────────────

const mocks = vi.hoisted(() => ({
  auth: {
    isBuilder: false,
    isSuperAdmin: false,
    isInvestor: false,
    viewingClinicId: null as string | null,
  },
  routeParams: { id: "trace-1" } as Record<string, string>,
  routeSearch: "" as string,
  fetch: vi.fn(),
  traceViewerCalls: [] as Array<Record<string, unknown>>,
}));

vi.mock("@/context/auth", () => ({
  useAuth: () => mocks.auth,
}));

vi.mock("wouter", () => ({
  useParams: () => mocks.routeParams,
  useSearch: () => mocks.routeSearch,
  Link: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock("@/components/trace-viewer", () => ({
  TraceViewer: (props: Record<string, unknown>) => {
    mocks.traceViewerCalls.push(props);
    return (
      <div
        data-testid="trace-viewer-mock"
        data-clinic-tenant-id={String(props.clinicTenantId)}
      />
    );
  },
}));

// Import after mocks are registered.
import TraceDetail from "./trace-detail";

function response(data: unknown, ok = true) {
  return Promise.resolve({
    ok,
    status: ok ? 200 : 500,
    json: async () => data,
  } as Response);
}

function baseTraceResponse(id = "trace-1") {
  return {
    id,
    timestamp: "2026-05-03T14:32:00Z",
    user_message: "Hello",
    srm_decision: {
      intent: "faq",
      funnel_stage: "initial",
      action: "respond",
      confidence: 0.9,
      constraints: [],
      gate_authority_before_price: false,
      safety_concern: false,
      handoff_required: false,
    },
    selected_context: [],
    prompt_built: "",
    llm_response: "Hi",
    processing_time_ms: 12,
  };
}

function renderPage() {
  return render(
    <I18nProvider>
      <TraceDetail />
    </I18nProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("ns_auth", JSON.stringify({ token: "test-token" }));
  localStorage.setItem("ns_locale", "en");

  mocks.auth.isBuilder = false;
  mocks.auth.isSuperAdmin = false;
  mocks.auth.isInvestor = false;
  mocks.auth.viewingClinicId = null;
  mocks.routeParams = { id: "trace-1" };
  mocks.routeSearch = "";
  mocks.fetch.mockReset();
  mocks.fetch.mockImplementation(() => response(baseTraceResponse()));
  mocks.traceViewerCalls.length = 0;
  vi.stubGlobal("fetch", mocks.fetch);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// ── Tests ────────────────────────────────────────────────────────────────

describe("<TraceDetail /> tenant resolution", () => {
  it("uses ?tenant_id=X from the URL for builders even when the header selects a different clinic", async () => {
    mocks.auth.isBuilder = true;
    mocks.auth.viewingClinicId = "header-selected-clinic";
    mocks.routeSearch = "tenant_id=url-tenant-xyz";

    renderPage();

    await waitFor(() =>
      expect(mocks.fetch).toHaveBeenCalledWith(
        "/v1/traces/trace-1?tenant_id=url-tenant-xyz",
        expect.any(Object),
      ),
    );
    await waitFor(() => expect(mocks.traceViewerCalls.length).toBeGreaterThan(0));
    const last = mocks.traceViewerCalls.at(-1)!;
    expect(last.clinicTenantId).toBe("url-tenant-xyz");
  });

  it("uses ?tenant_id=X from the URL for super admins over the header selection", async () => {
    mocks.auth.isSuperAdmin = true;
    mocks.auth.viewingClinicId = "header-selected-clinic";
    mocks.routeSearch = "tenant_id=url-tenant-xyz";

    renderPage();

    await waitFor(() =>
      expect(mocks.fetch).toHaveBeenCalledWith(
        "/v1/traces/trace-1?tenant_id=url-tenant-xyz",
        expect.any(Object),
      ),
    );
    await waitFor(() => expect(mocks.traceViewerCalls.length).toBeGreaterThan(0));
    expect(mocks.traceViewerCalls.at(-1)!.clinicTenantId).toBe("url-tenant-xyz");
  });

  it("ignores ?tenant_id=X for clinic_admin (not builder, not super admin) and falls back to viewingClinicId", async () => {
    // clinic_admin has neither isBuilder nor isSuperAdmin flags.
    mocks.auth.isBuilder = false;
    mocks.auth.isSuperAdmin = false;
    mocks.auth.viewingClinicId = "clinic-admin-own-tenant";
    mocks.routeSearch = "tenant_id=other-tenant";

    renderPage();

    // For a non-builder/non-super-admin the tenant query string is NOT
    // forwarded to the API and clinicTenantId falls back to viewingClinicId.
    await waitFor(() =>
      expect(mocks.fetch).toHaveBeenCalledWith(
        "/v1/traces/trace-1",
        expect.any(Object),
      ),
    );
    // The URL param must not leak into the fetched URL.
    for (const call of mocks.fetch.mock.calls) {
      expect(String(call[0])).not.toContain("other-tenant");
    }
    await waitFor(() => expect(mocks.traceViewerCalls.length).toBeGreaterThan(0));
    expect(mocks.traceViewerCalls.at(-1)!.clinicTenantId).toBe("clinic-admin-own-tenant");
  });

  it("falls back to viewingClinicId for builders when the tenant_id query param is absent", async () => {
    mocks.auth.isBuilder = true;
    mocks.auth.viewingClinicId = "builder-selected-clinic";
    mocks.routeSearch = "";

    renderPage();

    await waitFor(() =>
      expect(mocks.fetch).toHaveBeenCalledWith(
        "/v1/traces/trace-1?tenant_id=builder-selected-clinic",
        expect.any(Object),
      ),
    );
    await waitFor(() => expect(mocks.traceViewerCalls.length).toBeGreaterThan(0));
    expect(mocks.traceViewerCalls.at(-1)!.clinicTenantId).toBe(
      "builder-selected-clinic",
    );
  });
});
