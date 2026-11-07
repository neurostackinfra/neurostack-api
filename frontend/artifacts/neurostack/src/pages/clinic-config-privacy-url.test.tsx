import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";

import ClinicConfig from "./clinic-config";
import { AuthProvider } from "@/context/auth";
import { I18nProvider } from "@/context/i18n";

function PageWrapper({ children }: { children: ReactNode }) {
  return <AuthProvider><I18nProvider>{children}</I18nProvider></AuthProvider>;
}

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: vi.fn().mockResolvedValue(body),
  } as unknown as Response;
}

// Real logins carry `clinic_id` from clinic_configs.clinic_id, which is
// usually null and never the tenant id the public privacy page is keyed by.
function mountAs(role: "clinic_admin" | "builder", tenantId: string, viewingClinicId: string | null) {
  const payload = btoa(JSON.stringify({
    sub: `${role}-user`,
    tenant_id: tenantId,
    role,
    exp: 9999999999,
  }));
  localStorage.setItem("ns_locale", "pt");
  localStorage.setItem("ns_auth", JSON.stringify({
    token: `eyJhbGciOiJub25lIn0.${payload}.synthetic`,
    email: `${role}@example.test`,
    clinicId: null,
    tenantId,
    userId: `${role}-user`,
    builderId: role === "builder" ? "builder-user" : null,
    role,
    viewingClinicId,
    leadSubmitted: true,
  }));
  window.history.replaceState({}, "", "/config?tab=negocio");
}

const baseConfig = {
  clinic_name: "Clinic",
  clinic_legal_name: "Clinic Legal Name",
  clinic_cnpj: "12.345.678/0001-90",
  dpo_name: "DPO Name",
  dpo_contact: "dpo@example.test",
  services: [],
};

describe("ClinicConfig unique privacy policy URL", () => {
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    globalThis.fetch = originalFetch;
    localStorage.clear();
    window.history.replaceState({}, "", "/");
    cleanup();
    vi.restoreAllMocks();
  });

  it("shows the clinic's own tenant hash for a clinic admin whose login has no clinicId", async () => {
    mountAs("clinic_admin", "tenant-own", null);
    globalThis.fetch = vi.fn().mockResolvedValue(
      response({ ...baseConfig, clinic_id: null, tenant_id: "tenant-own" }),
    ) as unknown as typeof fetch;

    render(<PageWrapper><ClinicConfig /></PageWrapper>);

    const url = await screen.findByTestId("privacy-policy-url");
    expect(url).toHaveValue(`${window.location.origin}/privacidade/tenant-own`);
    expect(screen.getByTestId("privacy-policy-open")).toHaveAttribute("href", "/privacidade/tenant-own");
  });

  it("shows the viewed clinic's tenant hash for a builder, not the builder's own tenant", async () => {
    mountAs("builder", "tenant-builder", "tenant-target");
    const fetchMock = vi.fn().mockResolvedValue(
      response({ ...baseConfig, builder_lgpd_consent: true, tenant_id: "tenant-target" }),
    );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    render(<PageWrapper><ClinicConfig /></PageWrapper>);

    const url = await screen.findByTestId("privacy-policy-url");
    expect(url).toHaveValue(`${window.location.origin}/privacidade/tenant-target`);
    expect(fetchMock.mock.calls.map(([input]) => String(input))).toContain("/v1/config?tenant_id=tenant-target");
  });

  it("hides the URL instead of showing one without a hash when the server names no tenant", async () => {
    mountAs("clinic_admin", "tenant-own", null);
    globalThis.fetch = vi.fn().mockResolvedValue(response(baseConfig)) as unknown as typeof fetch;

    render(<PageWrapper><ClinicConfig /></PageWrapper>);

    await screen.findByTestId("btn-save-config");
    expect(screen.queryByTestId("privacy-policy-url")).not.toBeInTheDocument();
  });

  it("hides the NeuroStack URL when the clinic uses its own privacy link", async () => {
    mountAs("clinic_admin", "tenant-own", null);
    globalThis.fetch = vi.fn().mockResolvedValue(
      response({ ...baseConfig, tenant_id: "tenant-own", lgpd_custom_link: "https://clinic.example/privacy" }),
    ) as unknown as typeof fetch;

    render(<PageWrapper><ClinicConfig /></PageWrapper>);

    await screen.findByTestId("btn-save-config");
    expect(screen.queryByTestId("privacy-policy-url")).not.toBeInTheDocument();
  });
});
