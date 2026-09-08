import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";

import ClinicConfig, { describeConfigSaveFailure } from "./clinic-config";
import * as authContext from "@/context/auth";
import { AuthProvider } from "@/context/auth";
import { I18nProvider } from "@/context/i18n";
import { Toaster } from "@/components/ui/toaster";

function PageWrapper({ children }: { children: ReactNode }) {
  return <AuthProvider><I18nProvider>{children}<Toaster /></I18nProvider></AuthProvider>;
}

function response(body: unknown, ok = true, status = ok ? 200 : 422): Response {
  return { ok, status, json: vi.fn().mockResolvedValue(body) } as unknown as Response;
}

function mountAs(role: "clinic_admin" | "investor" = "clinic_admin", locale: "pt" | "en" = "pt") {
  const payload = btoa(JSON.stringify({ sub: `${role}-user`, clinic_id: "clinic-42", role, exp: 9999999999 }));
  localStorage.setItem("ns_locale", locale);
  localStorage.setItem("ns_auth", JSON.stringify({
    token: `eyJhbGciOiJub25lIn0.${payload}.synthetic`,
    email: `${role}@example.test`,
    clinicId: "clinic-42",
    tenantId: "clinic-42",
    userId: `${role}-user`,
    builderId: null,
    role,
    viewingClinicId: "clinic-42",
    leadSubmitted: true,
  }));
  window.history.replaceState({}, "", "/config?tab=servicos");
}

function configWith(services: unknown[], serviceFormat = "both") {
  return {
    clinic_name: "Clinic",
    clinic_legal_name: "Clinic Legal Name",
    clinic_cnpj: "12.345.678/0001-90",
    dpo_name: "DPO Name",
    dpo_contact: "dpo@example.test",
    builder_lgpd_consent: true,
    service_format: serviceFormat,
    services,
  };
}

async function renderConfig(config: Record<string, unknown>, locale: "pt" | "en" = "pt") {
  mountAs("clinic_admin", locale);
  const fetchMock = vi.fn().mockResolvedValue(response(config));
  globalThis.fetch = fetchMock as unknown as typeof fetch;
  render(<PageWrapper><ClinicConfig /></PageWrapper>);
  await screen.findByTestId("service-item-0");
  return fetchMock;
}

function selectFormat(index: number, label: string) {
  fireEvent.click(screen.getByTestId(`select-service-format-${index}`));
  fireEvent.click(screen.getByRole("option", { name: label }));
}

describe("ClinicConfig service delivery modes", () => {
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    globalThis.fetch = originalFetch;
    localStorage.clear();
    window.history.replaceState({}, "", "/");
    cleanup();
    vi.restoreAllMocks();
  });

  it("hydrates every canonical delivery mode and preserves adjacent service fields in the PATCH", async () => {
    const service = {
      name: "Consulta",
      description: "Avaliação detalhada",
      price: "R$ 250",
      authority_notes: "Especialista certificada",
      details: { duration: 45 },
      custom_flag: true,
      service_format: "online",
    };
    const config = configWith([service]);
    const fetchMock = await renderConfig(config);
    const savedConfig = configWith([{ ...service, service_format: "both" }]);
    fetchMock.mockImplementation((_url: RequestInfo | URL, init?: RequestInit) => Promise.resolve(
      response(init?.method === "PATCH" ? savedConfig : config),
    ));

    expect(screen.getByTestId("select-service-format-0")).toHaveTextContent("Online");
    selectFormat(0, "Online e Presencial");
    fireEvent.click(screen.getByTestId("btn-save-config"));

    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(true));
    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
    expect(JSON.parse(String(patch?.[1]?.body)).services).toEqual([{
      ...service,
      service_format: "both",
    }]);
    await waitFor(() => expect(screen.getByTestId("select-service-format-0"))
      .toHaveTextContent("Online e Presencial"));
  });

  it("starts a new named service without a selection and blocks PATCH until it is confirmed", async () => {
    const config = configWith([]);
    const fetchMock = vi.fn().mockResolvedValue(response(config));
    mountAs();
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    render(<PageWrapper><ClinicConfig /></PageWrapper>);
    await screen.findByTestId("btn-add-service");

    fireEvent.click(screen.getByTestId("btn-add-service"));
    fireEvent.change(screen.getByTestId("input-service-name-0"), { target: { value: "Consulta nova" } });
    expect(screen.getByTestId("select-service-format-0")).toHaveTextContent("Confirme a modalidade");
    fireEvent.click(screen.getByTestId("btn-save-config"));

    await screen.findByTestId("service-format-error-0");
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(0);

    selectFormat(0, "Online");
    fireEvent.click(screen.getByTestId("btn-save-config"));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(true));
  });

  it.each([
    ["online", "presential", true],
    ["online", "both", true],
    ["presential", "online", true],
    ["presential", "both", true],
    ["both", "both", false],
  ] as const)("enforces the canonical global %s boundary for a %s service", async (globalFormat, itemFormat, blocked) => {
    const fetchMock = await renderConfig(configWith([{
      name: "Consulta",
      description: "",
      service_format: itemFormat,
    }], globalFormat));

    fireEvent.click(screen.getByTestId("btn-save-config"));

    if (blocked) {
      await screen.findByTestId("service-format-error-0");
      expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(0);
    } else {
      await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(true));
    }
  });

  it("marks every incompatible service before blocking a PATCH", async () => {
    const fetchMock = await renderConfig(configWith([
      { name: "Consulta online", description: "", service_format: "online" },
      { name: "Consulta híbrida", description: "", service_format: "both" },
    ], "presential"));

    fireEvent.click(screen.getByTestId("btn-save-config"));

    await screen.findByTestId("service-format-error-0");
    expect(screen.getByTestId("service-format-error-1")).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(0);
  });

  it("keeps a legacy string visible and converts it to an object only after explicit confirmation", async () => {
    const config = configWith(["Consulta legada"]);
    const fetchMock = await renderConfig(config);
    const savedConfig = configWith([{
      name: "Consulta legada",
      description: "",
      price: "",
      authority_notes: "",
      service_format: "presential",
    }]);
    fetchMock.mockImplementation((_url: RequestInfo | URL, init?: RequestInit) => Promise.resolve(
      response(init?.method === "PATCH" ? savedConfig : config),
    ));

    expect(screen.getByTestId("input-service-name-0")).toHaveValue("Consulta legada");
    fireEvent.click(screen.getByTestId("btn-save-config"));
    await screen.findByTestId("service-format-error-0");
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(0);

    selectFormat(0, "Presencial");
    fireEvent.click(screen.getByTestId("btn-save-config"));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(true));
    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
    expect(JSON.parse(String(patch?.[1]?.body)).services).toEqual([{
      name: "Consulta legada",
      description: "",
      price: "",
      authority_notes: "",
      service_format: "presential",
    }]);
    await waitFor(() => expect(screen.getByTestId("select-service-format-0")).toHaveTextContent("Presencial"));
  });

  it("keeps a legacy string normalized but unconverted for a read-only viewer", async () => {
    const config = configWith(["Consulta legada"]);
    const fetchMock = vi.fn().mockResolvedValue(response(config));
    mountAs();
    vi.spyOn(authContext, "useAuth").mockReturnValue({
      clinicId: "clinic-42",
      isReadOnly: true,
      isClinicAdmin: true,
      role: "clinic_admin",
      isBuilder: false,
      isSuperAdmin: false,
      isInvestor: false,
      viewingClinicId: "clinic-42",
    } as ReturnType<typeof authContext.useAuth>);
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    render(<PageWrapper><ClinicConfig /></PageWrapper>);

    expect(await screen.findByTestId("input-service-name-0")).toHaveValue("Consulta legada");
    expect(screen.getByTestId("select-service-format-0")).toBeDisabled();
    expect(screen.getByTestId("select-service-format-0")).toHaveTextContent("Confirme a modalidade");
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(0);
  });

  it("shows delivery modes but disables their selectors for read-only investors", async () => {
    mountAs("investor");
    render(<PageWrapper><ClinicConfig /></PageWrapper>);

    const online = await screen.findByTestId("select-service-format-3");
    expect(online).toBeDisabled();
    expect(screen.getByTestId("select-service-format-0")).toHaveTextContent("Presencial");
    expect(screen.getByTestId("select-service-format-2")).toHaveTextContent("Online e Presencial");
  });

  it("maps remote conflicts to fixed localized copy without echoing backend content", async () => {
    const malicious = "<img src=x onerror=alert('leak')> consulta secreta";
    const config = configWith([{
      name: malicious,
      description: malicious,
      service_format: "online",
    }]);
    mountAs("clinic_admin", "pt");
    const fetchMock = vi.fn().mockImplementation((_url: RequestInfo | URL, init?: RequestInit) => (
      Promise.resolve(init?.method === "PATCH"
        ? response({ detail: { error: "service_format_conflict", message: malicious } }, false, 422)
        : response(config))
    ));
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    render(<PageWrapper><ClinicConfig /></PageWrapper>);
    await screen.findByTestId("service-item-0");

    fireEvent.click(screen.getByTestId("btn-save-config"));
    const description = await screen.findByText("A modalidade de um serviço não é compatível com a modalidade geral da clínica. Revise o catálogo.");
    const toast = description.closest('[data-state="open"]');
    expect(toast).not.toBeNull();
    expect(toast).not.toHaveTextContent(malicious);
    expect(describeConfigSaveFailure({ detail: { error: "service_format_conflict", message: malicious } }, 422, "en"))
      .toBe("A service delivery mode is incompatible with the clinic's overall delivery mode. Review the catalog.");
  });
});
