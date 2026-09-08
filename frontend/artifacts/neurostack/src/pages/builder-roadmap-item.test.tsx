import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { I18nProvider, type Locale } from "@/context/i18n";
import BuilderPanel from "./builder";

const mocks = vi.hoisted(() => ({
  fetch: vi.fn(),
  navigate: vi.fn(),
}));

vi.mock("@/context/auth", () => ({
  useAuth: () => ({
    isBuilder: true,
    setViewingClinicId: vi.fn(),
    email: "builder@example.com",
  }),
}));

vi.mock("wouter", () => ({
  useLocation: () => ["/builder", mocks.navigate],
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

vi.mock("@/components/plan-usage-summary", () => ({
  PlanUsageSummary: () => <div data-testid="plan-usage" />,
}));

function response(body: unknown): Promise<Response> {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: async () => body,
  } as Response);
}

function renderBuilder(locale: Locale) {
  localStorage.setItem("ns_locale", locale);
  return render(
    <I18nProvider>
      <BuilderPanel />
    </I18nProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("ns_auth", JSON.stringify({ token: "builder-token" }));
  mocks.fetch.mockReset();
  mocks.fetch.mockImplementation((input: string | URL | Request) => {
    const path = String(input);
    if (path === "/v1/builder/me") {
      return response({ id: "builder", name: "Builder", status: "active", clinic_count: 0 });
    }
    if (path === "/v1/builder/dashboard") {
      return response({
        stats: { clinic_count: 0, active_clinics: 0, inactive_clinics: 0, total_traces: 0, total_tokens: 0, model_breakdown: [] },
        time_series: [],
        alerts: [],
      });
    }
    if (path === "/v1/builder/clinics" || path === "/v1/builder/tickets") return response([]);
    if (path === "/v1/billing/usage") return response(null);
    if (path === "/v1/builder/profile") return response({ support_email: null });
    throw new Error(`Unexpected fetch: ${path}`);
  });
  vi.stubGlobal("fetch", mocks.fetch);
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Builder roadmap item", () => {
  it.each([
    ["pt", "Certificação e co-branding NeuroStack", "EM BREVE"],
    ["en", "NeuroStack certification and co-branding", "COMING SOON"],
  ] as const)("renders one localized, informational-only card in %s", async (locale, title, badge) => {
    renderBuilder(locale);

    await screen.findByText(title);
    const cards = screen.getAllByTestId("builder-roadmap-certification-card");
    expect(cards).toHaveLength(1);
    expect(within(cards[0]).getByText(title)).toBeInTheDocument();
    expect(within(cards[0]).getByText(badge)).toBeInTheDocument();
    expect(within(cards[0]).queryByRole("link")).not.toBeInTheDocument();
    expect(within(cards[0]).queryByRole("button")).not.toBeInTheDocument();
    expect(within(cards[0]).queryByRole("checkbox")).not.toBeInTheDocument();
    expect(within(cards[0]).queryByRole("switch")).not.toBeInTheDocument();
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(6));
  });
});
