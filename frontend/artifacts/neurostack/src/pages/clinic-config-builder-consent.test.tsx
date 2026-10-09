import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

function mountAs(role: "clinic_admin" | "builder") {
  const payload = btoa(JSON.stringify({
    sub: `${role}-user`,
    clinic_id: "clinic-42",
    role,
    exp: 9999999999,
  }));
  localStorage.setItem("ns_locale", "pt");
  localStorage.setItem("ns_auth", JSON.stringify({
    token: `eyJhbGciOiJub25lIn0.${payload}.synthetic`,
    email: `${role}@example.test`,
    clinicId: "clinic-42",
    tenantId: "clinic-42",
    userId: `${role}-user`,
    builderId: role === "builder" ? "builder-user" : null,
    role,
    viewingClinicId: "clinic-42",
    leadSubmitted: true,
  }));
  window.history.replaceState({}, "", "/config?tab=negocio");
}

const editableConfig = {
  clinic_name: "Clinic",
  clinic_legal_name: "Clinic Legal Name",
  clinic_cnpj: "12.345.678/0001-90",
  dpo_name: "DPO Name",
  dpo_contact: "dpo@example.test",
  builder_lgpd_consent: false,
  builder_slm_training: true,
  services: [],
};

describe("ClinicConfig Builder-only legal declarations", () => {
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    globalThis.fetch = originalFetch;
    localStorage.clear();
    window.history.replaceState({}, "", "/");
    cleanup();
    vi.restoreAllMocks();
  });

  it("hides Builder declarations and preserves their values when a clinic saves", async () => {
    mountAs("clinic_admin");
    const fetchMock = vi.fn().mockResolvedValue(response(editableConfig));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    render(<PageWrapper><ClinicConfig /></PageWrapper>);

    await screen.findByTestId("btn-save-config");
    expect(screen.queryByText("Declarações do Builder")).not.toBeInTheDocument();
    expect(screen.queryByText(/possuo contrato formal com esta clínica/)).not.toBeInTheDocument();
    expect(screen.queryByText(/autoriza expressamente a anonimização/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("btn-save-config"));

    await waitFor(() => expect(
      fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH"),
    ).toHaveLength(1));
    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
    const payload = JSON.parse(String(patch?.[1]?.body));
    expect(payload).not.toHaveProperty("builder_lgpd_consent");
    expect(payload).not.toHaveProperty("builder_slm_training");
  });

  it("keeps both declarations visible for Builders and requires only LGPD consent", async () => {
    mountAs("builder");
    const fetchMock = vi.fn().mockResolvedValue(response(editableConfig));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    render(<PageWrapper><ClinicConfig /></PageWrapper>);

    expect(await screen.findByText("Declarações do Builder")).toBeInTheDocument();
    const mandatoryConsent = screen.getByRole("checkbox", {
      name: /Confirmo que possuo contrato formal com esta clínica/,
    });
    const optionalTraining = screen.getByRole("checkbox", {
      name: /autoriza expressamente a anonimização/,
    });
    expect(mandatoryConsent).not.toBeChecked();
    expect(optionalTraining).toBeChecked();

    fireEvent.click(screen.getByTestId("btn-save-config"));
    expect(await screen.findByText(/É necessário confirmar o contrato/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(0);

    fireEvent.click(mandatoryConsent);
    fireEvent.click(optionalTraining);
    fireEvent.click(screen.getByTestId("btn-save-config"));

    await waitFor(() => expect(
      fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH"),
    ).toHaveLength(1));
    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
    expect(JSON.parse(String(patch?.[1]?.body))).toMatchObject({
      builder_lgpd_consent: true,
      builder_slm_training: false,
    });
  });
});
