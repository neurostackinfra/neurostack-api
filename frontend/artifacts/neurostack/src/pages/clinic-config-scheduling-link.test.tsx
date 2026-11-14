import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";

import ClinicConfig, { describeConfigSaveFailure } from "./clinic-config";
import { AuthProvider } from "@/context/auth";
import { I18nProvider } from "@/context/i18n";

const HINT_PT =
  "Texto enviado ao paciente antes do link. A URL será adicionada logo abaixo. Não diga que a consulta está marcada ou confirmada, nem que há horário livre: quem confere o agendamento é a equipe.";
const HINT_EN =
  "Text sent to the patient before the link. The URL will be appended right below. Do not say the appointment is booked or confirmed, or that a time is available: the team checks the booking.";

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

function mountClinicAdmin(locale: "pt" | "en") {
  const payload = btoa(JSON.stringify({
    sub: "clinic-admin-user",
    tenant_id: "tenant-own",
    role: "clinic_admin",
    exp: 9999999999,
  }));
  localStorage.setItem("ns_locale", locale);
  localStorage.setItem("ns_auth", JSON.stringify({
    token: `eyJhbGciOiJub25lIn0.${payload}.synthetic`,
    email: "clinic_admin@example.test",
    clinicId: null,
    tenantId: "tenant-own",
    userId: "clinic-admin-user",
    builderId: null,
    role: "clinic_admin",
    viewingClinicId: null,
    leadSubmitted: true,
  }));
  window.history.replaceState({}, "", "/config?tab=operacao");
}

const linkOnConfig = {
  clinic_name: "Clinic",
  tenant_id: "tenant-own",
  services: [],
  scheduling_link_enabled: true,
  scheduling_link_url: "https://agenda.example/marcar",
  scheduling_link_message: "Clique no link para escolher o melhor horário para você:",
};

const promisePayload = {
  detail: [
    {
      loc: ["body", "scheduling_link_message"],
      msg: "x",
      type: "scheduling_link_message_promise",
    },
  ],
};

describe("ClinicConfig scheduling link message", () => {
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    globalThis.fetch = originalFetch;
    localStorage.clear();
    window.history.replaceState({}, "", "/");
    cleanup();
    vi.restoreAllMocks();
  });

  it.each([
    ["pt", HINT_PT],
    ["en", HINT_EN],
  ] as const)("shows the no-promise hint in %s when the link is on", async (locale, hint) => {
    mountClinicAdmin(locale);
    globalThis.fetch = vi.fn().mockResolvedValue(response(linkOnConfig)) as unknown as typeof fetch;

    render(<PageWrapper><ClinicConfig /></PageWrapper>);

    await screen.findByTestId("input-scheduling-link-message");
    expect(screen.getByText(hint)).toBeInTheDocument();
  });

  it("names the scheduling link message field for a promise rejection in pt", () => {
    const message = describeConfigSaveFailure(promisePayload, 422, "pt");

    expect(message).toContain("mensagem do link de agendamento");
    expect(message).not.toContain("scheduling_link_message");
  });

  it("names the scheduling link message field for a promise rejection in en", () => {
    const message = describeConfigSaveFailure(promisePayload, 422, "en");

    expect(message).toContain("scheduling link message");
    expect(message).not.toContain("scheduling_link_message");
  });
});
