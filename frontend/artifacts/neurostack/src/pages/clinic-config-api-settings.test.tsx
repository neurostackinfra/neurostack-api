import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";

import { VoiceResponseControls } from "@/components/api-settings/voice-response-controls";
import ClinicConfig, {
  API_CAPABILITIES,
  API_PROVIDER_AVAILABILITY,
  ApiCredentialsForm,
  describeConfigSaveFailure,
  PROVIDERS,
  providerCardState,
  STT_PROVIDERS,
  TTS_PROVIDERS,
  WA_PROVIDERS,
} from "./clinic-config";
import { I18nProvider } from "@/context/i18n";
import { AuthProvider } from "@/context/auth";

function Wrapper({ children }: { children: ReactNode }) {
  return <I18nProvider>{children}</I18nProvider>;
}

function PageWrapper({ children }: { children: ReactNode }) {
  return (
    <AuthProvider>
      <I18nProvider>{children}</I18nProvider>
    </AuthProvider>
  );
}

function mountAsInvestor() {
  const investorAuth = {
    token: null,
    email: null,
    clinicId: null,
    tenantId: null,
    userId: null,
    builderId: null,
    role: "investor",
    viewingClinicId: null,
    leadSubmitted: false,
  };
  localStorage.setItem("ns_auth", JSON.stringify(investorAuth));
  localStorage.setItem("ns_locale", "pt");
  window.history.replaceState({}, "", "/config?tab=operacao");
}

function response(body: unknown, ok = true, status = ok ? 200 : 422): Response {
  return {
    ok,
    status,
    json: vi.fn().mockResolvedValue(body),
  } as unknown as Response;
}

/** A tenant that holds no provider at all — every card must stay coming soon. */
function emptyStatus(overrides: Record<string, unknown> = {}) {
  return {
    whatsapp_provider: null,
    llm_provider: null,
    stt_provider: null,
    multimodal_provider: null,
    tts_provider: null,
    whatsapp_readiness: "incomplete",
    llm_readiness: "incomplete",
    stt_readiness: "incomplete",
    multimodal_readiness: "incomplete",
    tts_readiness: "incomplete",
    ...overrides,
  };
}

async function renderForm(
  status: Record<string, unknown>,
  props: Partial<React.ComponentProps<typeof ApiCredentialsForm>> = {},
) {
  const fetchMock = vi.fn().mockResolvedValue(response(status));
  globalThis.fetch = fetchMock as unknown as typeof fetch;

  render(
    <Wrapper>
      <ApiCredentialsForm
        clinicId="clinic-42"
        isReadOnly={false}
        isSuperAdmin={false}
        {...props}
      />
    </Wrapper>,
  );

  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  await act(async () => { await Promise.resolve(); });
  return fetchMock;
}

const BACKEND_PROVIDER_AVAILABILITY = {
  whatsapp: { meta_cloud: "coming_soon", zapi: "coming_soon", uazapi: "coming_soon", evolution: "coming_soon", dialog360: "coming_soon", twilio: "coming_soon" },
  llm: { dropout: "coming_soon", openai: "coming_soon", outros: "coming_soon", slm_cortex: "coming_soon" },
  multimodal: { openrouter: "coming_soon" },
  stt: { outros: "coming_soon", openai_whisper: "coming_soon", elevenlabs_scribe: "coming_soon", google_stt: "coming_soon", deepgram_stt: "coming_soon", microsoft_stt: "coming_soon" },
  tts: { elevenlabs: "coming_soon", google: "coming_soon" },
} as const;
const COMING_SOON_COUNT = 19;

describe("describeConfigSaveFailure", () => {
  it("identifies invalid backend fields without exposing their submitted values", () => {
    const description = describeConfigSaveFailure({
      detail: [{
        type: "value_error",
        loc: ["scheduling_link_url"],
        msg: "Value error",
        input: "sensitive-value-that-must-not-be-rendered",
      }],
    }, 422, "pt");

    expect(description).toContain("link de agendamento");
    expect(description).not.toContain("sensitive-value-that-must-not-be-rendered");
  });

  it("labels every nested business_hours 422 with the top-level field name", () => {
    // Pydantic's `loc` for the new weekly-schedule contract has depth 1, 2, or
    // 3 depending on which validation layer fires (day-key vs. cross-field vs.
    // per-field). All three must collapse to a single friendly label instead
    // of leaking the raw sub-token ("open" / "mon") into the banner.
    const dayKeyFailure = describeConfigSaveFailure({
      detail: [{ loc: ["business_hours"], input: { xxx: { open: "09:00", close: "18:00" } } }],
    }, 422, "pt");
    const crossFieldFailure = describeConfigSaveFailure({
      detail: [{ loc: ["business_hours", "mon"], input: { open: "18:00", close: "09:00" } }],
    }, 422, "pt");
    const fieldLevelFailure = describeConfigSaveFailure({
      detail: [{ loc: ["business_hours", "mon", "open"], input: "99:99" }],
    }, 422, "pt");

    for (const description of [dayKeyFailure, crossFieldFailure, fieldLevelFailure]) {
      expect(description).toContain("horários de funcionamento");
      expect(description).not.toContain("99:99");
      expect(description).not.toContain(": open");
      expect(description).not.toContain(": mon");
      expect(description).not.toContain(": xxx");
    }
  });

  it("translates authorization failures from their stable backend code", () => {
    expect(describeConfigSaveFailure({
      detail: { error: "target_tenant_forbidden" },
    }, 403, "pt")).toBe("Você não tem permissão para alterar a configuração desta clínica.");
  });

  it("translates provider_not_implemented with its capability and provider label", () => {
    const description = describeConfigSaveFailure({
      detail: { error: "provider_not_implemented", capability: "whatsapp", provider: "twilio" },
    }, 501, "pt");

    expect(description).toContain("API WhatsApp");
    expect(description).toContain("Twilio");
    expect(description).not.toContain("provider_not_implemented");
  });

  it("translates fresh_api_key_required with the capability prefix", () => {
    const description = describeConfigSaveFailure({
      detail: { error: "fresh_api_key_required", capability: "llm", reason: "api_url_changed" },
    }, 422, "pt");

    expect(description).toContain("API LLM: informe uma nova API Key");
    expect(description).toContain("trocar de provedor ou de endpoint exige uma chave nova");
    expect(description).not.toContain("fresh_api_key_required");
  });

  it("prefixes invalid providers and Meta phone conflicts with their capability", () => {
    const invalidProvider = describeConfigSaveFailure({
      detail: { error: "invalid_provider_for_capability", capability: "stt" },
    }, 422, "pt");
    const phoneConflict = describeConfigSaveFailure({
      detail: { error: "meta_phone_number_id_conflict", capability: "whatsapp", message: "backend-only detail" },
    }, 409, "pt");

    expect(invalidProvider).toContain("API STT:");
    expect(invalidProvider).toContain("O provedor selecionado não é válido");
    expect(invalidProvider).not.toContain("invalid_provider_for_capability");
    expect(phoneConflict).toContain("API WhatsApp:");
    expect(phoneConflict).toContain("Phone Number ID da Meta");
    expect(phoneConflict).not.toContain("meta_phone_number_id_conflict");
    expect(phoneConflict).not.toContain("backend-only detail");
  });

  it("keeps a safe human fallback when a capability is absent", () => {
    const phoneConflict = describeConfigSaveFailure({
      detail: { error: "meta_phone_number_id_conflict", message: "backend-only detail" },
    }, 409, "pt");

    expect(phoneConflict).toContain("Phone Number ID da Meta");
    expect(phoneConflict).not.toContain("meta_phone_number_id_conflict");
    expect(phoneConflict).not.toContain("backend-only detail");
  });

  it("does not expose backend details for capability errors", () => {
    expect(describeConfigSaveFailure({
      detail: { error: "meta_phone_number_id_conflict", message: "backend-only detail" },
    }, 409, "pt")).not.toContain("backend-only detail");
  });

  it("prefixes URL and payload errors with the capability inferred from fields", () => {
    const unsafeUrl = describeConfigSaveFailure({
      detail: { error: "unsafe_api_url", field: "stt_api_url" },
    }, 422, "pt");
    const invalidPayload = describeConfigSaveFailure({
      detail: { error: "invalid_credentials_payload", fields: [{ field: "zapi_token", code: "string_too_short" }] },
    }, 422, "pt");

    expect(unsafeUrl).toContain("API STT:");
    expect(unsafeUrl).toContain("HTTPS público");
    expect(unsafeUrl).not.toContain("unsafe_api_url");
    expect(invalidPayload).toContain("API WhatsApp:");
    expect(invalidPayload).toContain("token da Z-API");
    expect(invalidPayload).not.toContain("invalid_credentials_payload");
  });

  it("falls back to a safe human label for an unknown credential field", () => {
    const description = describeConfigSaveFailure({
      detail: { error: "invalid_credentials_payload", fields: [{ field: "future_credential_field", input: "secret" }] },
    }, 422, "pt");

    expect(description).toContain("future credential field");
    expect(description).not.toContain("API ");
    expect(description).not.toContain("invalid_credentials_payload");
    expect(description).not.toContain("secret");
  });

  it("provides a useful fallback for server failures", () => {
    expect(describeConfigSaveFailure(null, 500, "en"))
      .toBe("The server could not save the configuration. Try again in a few moments.");
  });
});

describe("frontend provider availability mirror", () => {
  it("contains the exact 19 backend pairs and keeps all of them coming soon", () => {
    expect(API_PROVIDER_AVAILABILITY).toEqual(BACKEND_PROVIDER_AVAILABILITY);
    const states = Object.values(API_PROVIDER_AVAILABILITY).flatMap(Object.values);
    expect(states).toHaveLength(19);
    expect(states.filter((state) => state === "active")).toHaveLength(0);
    expect(states.filter((state) => state === "coming_soon")).toHaveLength(COMING_SOON_COUNT);
  });

  it("does not expose a provider outside the availability matrix", () => {
    const displayed = {
      whatsapp: WA_PROVIDERS.map((provider) => provider.id).sort(),
      llm: PROVIDERS.map((provider) => provider.id).sort(),
      multimodal: ["openrouter"],
      stt: STT_PROVIDERS.map((provider) => provider.id).sort(),
      tts: TTS_PROVIDERS.map((provider) => provider.id).sort(),
    };

    for (const [capability, providerIds] of Object.entries(displayed)) {
      expect(providerIds).toEqual(Object.keys(
        API_PROVIDER_AVAILABILITY[capability as keyof typeof API_PROVIDER_AVAILABILITY],
      ).sort());
    }
  });

  it("keeps the three removed providers outside the display matrix", () => {
    expect("groq" in API_PROVIDER_AVAILABILITY.multimodal).toBe(false);
    expect("openai" in API_PROVIDER_AVAILABILITY.multimodal).toBe(false);
    expect("microsoft" in API_PROVIDER_AVAILABILITY.tts).toBe(false);
  });
});

describe("providerCardState", () => {
  it.each([
    ["without status", null],
    ["without a held provider", emptyStatus()],
    ["with active legacy credentials", emptyStatus({ llm_provider: "openai", llm_readiness: "active" })],
    ["with partial legacy credentials", emptyStatus({ llm_provider: "openai", llm_readiness: "incomplete" })],
    ["with configuration-only legacy credentials", emptyStatus({ llm_provider: "openai", llm_readiness: "configuration_only" })],
  ])("keeps an all-coming-soon provider inert %s", (_case, status) => {
    expect(providerCardState("llm", "openai", status)).toBe("coming_soon");
  });
});

describe("<ApiCredentialsForm /> card states", () => {
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    globalThis.fetch = originalFetch;
    localStorage.clear();
    window.history.replaceState({}, "", "/");
    cleanup();
    vi.restoreAllMocks();
  });

  it("keeps the multimodal fallback capability copy", () => {
    expect(API_CAPABILITIES.find(({ id }) => id === "multimodal")).toMatchObject({
      short: "FallBack LLM Multimodal",
      desc: {
        pt: expect.stringContaining("LLM primário falhar"),
        en: expect.stringContaining("primary LLM fails"),
      },
    });
  });

  it.each([
    ["pt", "EM BREVE", "DISPONÍVEL", "Configuração apenas"],
    ["en", "COMING SOON", "AVAILABLE", "Configuration only"],
  ])("renders all 19 cards as inert orange surfaces in %s", async (
    locale,
    comingSoonLabel,
    availableLabel,
    configurationOnlyLabel,
  ) => {
    localStorage.setItem("ns_locale", locale);
    const fetchMock = await renderForm(emptyStatus());

    const section = screen.getByTestId("api-settings-section");
    const cards = within(section).getAllByTestId(/provider-card-/);
    const badges = within(section).getAllByText(comingSoonLabel);
    expect(cards).toHaveLength(COMING_SOON_COUNT);
    expect(badges).toHaveLength(COMING_SOON_COUNT);
    expect(within(section).queryByText(availableLabel)).not.toBeInTheDocument();
    expect(within(section).queryByText(configurationOnlyLabel)).not.toBeInTheDocument();
    expect(section.querySelectorAll("button, input, select, textarea, [tabindex]")).toHaveLength(0);

    for (const card of cards) {
      expect(card.tagName).toBe("ARTICLE");
      expect(card).toHaveAttribute("data-card-state", "coming_soon");
      expect(card).not.toHaveAttribute("aria-expanded");
      fireEvent.click(card);
    }

    expect(within(section).queryByTestId(/provider-editor-/)).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(0);
  });

  it("uses the English COMING SOON badge on every card", async () => {
    localStorage.setItem("ns_locale", "en");
    await renderForm(emptyStatus());

    const section = screen.getByTestId("api-settings-section");
    expect(within(section).getAllByText("COMING SOON")).toHaveLength(COMING_SOON_COUNT);
    expect(within(section).queryByText("AVAILABLE")).not.toBeInTheDocument();
  });

  it("does not reopen a card or emit a payload for persisted legacy credentials", async () => {
    localStorage.setItem("ns_locale", "pt");
    const fetchMock = await renderForm(emptyStatus({
      whatsapp_provider: "meta_cloud",
      whatsapp_readiness: "signature_pending",
      meta_phone_number_id: "1234567890",
      llm_provider: "openai",
      llm_readiness: "active",
      llm_api_url: "https://api.openai.com/v1",
      llm_model_name: "gpt-4o",
      llm_api_key_set: true,
    }));

    const section = screen.getByTestId("api-settings-section");
    fireEvent.click(screen.getByTestId("provider-card-whatsapp-meta_cloud"));
    fireEvent.click(screen.getByTestId("provider-card-llm-openai"));

    expect(within(section).getAllByText("EM BREVE")).toHaveLength(COMING_SOON_COUNT);
    expect(within(section).queryByText("CONFIGURADO")).not.toBeInTheDocument();
    expect(within(section).queryByText("INCOMPLETO")).not.toBeInTheDocument();
    expect(within(section).queryByTestId(/provider-editor-/)).not.toBeInTheDocument();
    expect(section.querySelectorAll("input, select, textarea")).toHaveLength(0);
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(0);
  });

  it("keeps every card inert and warns when the status fetch fails", async () => {
    localStorage.setItem("ns_locale", "pt");
    const fetchMock = vi.fn().mockResolvedValue(response({}, false, 500));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    render(
      <Wrapper><ApiCredentialsForm clinicId="clinic-42" isReadOnly={false} isSuperAdmin={false} /></Wrapper>,
    );
    await screen.findByTestId("api-settings-load-error");

    // Fail closed: an unknown tenant state must never make a card editable.
    const section = screen.getByTestId("api-settings-section");
    expect(within(section).getAllByText("EM BREVE")).toHaveLength(19);
    expect(within(section).queryByRole("button")).not.toBeInTheDocument();
  });

  it("keeps voice controls as a sibling of the provider grid", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(emptyStatus()));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    render(
      <Wrapper>
        <div data-testid="integrations-content">
          <VoiceResponseControls clinicId="preview" isReadOnly role="investor" />
          <ApiCredentialsForm clinicId="preview" isReadOnly isSuperAdmin={false} />
        </div>
      </Wrapper>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const voiceSection = screen.getByTestId("voice-response-controls");
    const apiSection = screen.getByTestId("api-settings-section");
    expect(apiSection.contains(voiceSection)).toBe(false);
    expect(within(apiSection).getAllByTestId(/provider-card-/)).toHaveLength(19);
  });

  it("does not expose the Integrations tab or voice section to clinic admins", async () => {
    const payload = btoa(JSON.stringify({
      sub: "clinic-admin",
      clinic_id: "clinic-admin",
      role: "clinic_admin",
      exp: 9999999999,
    }));
    const token = `eyJhbGciOiJub25lIn0.${payload}.synthetic`;
    localStorage.setItem("ns_locale", "en");
    localStorage.setItem("ns_auth", JSON.stringify({
      token,
      email: "admin@example.test",
      clinicId: "clinic-admin",
      tenantId: "clinic-admin",
      userId: "clinic-admin",
      builderId: null,
      role: "clinic_admin",
      viewingClinicId: "clinic-admin",
      leadSubmitted: true,
    }));
    window.history.replaceState({}, "", "/config?tab=integracoes");
    let resolveConfig!: (value: Response) => void;
    const fetchMock = vi.fn().mockImplementation(() => new Promise<Response>((resolve) => {
      resolveConfig = resolve;
    }));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    render(
      <AuthProvider>
        <Wrapper>
          <ClinicConfig />
        </Wrapper>
      </AuthProvider>,
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await act(async () => {
      resolveConfig(response({ clinic_name: "Clinic" }));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    await screen.findByRole("tab", { name: "Identity" });
    expect(screen.queryByRole("tab", { name: "Integrations" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("voice-response-controls")).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("api-credentials"))).toBe(false);
  });
});

describe("<ClinicConfig /> operations tab — business-hours UI surface", () => {
  const originalFetch = globalThis.fetch;

  function mountAsRole(role: "clinic_admin" | "builder", viewingClinicId: string) {
    const payload = btoa(JSON.stringify({
      sub: `${role}-user`,
      clinic_id: viewingClinicId,
      role,
      exp: 9999999999,
    }));
    localStorage.setItem("ns_locale", "en");
    localStorage.setItem("ns_auth", JSON.stringify({
      token: `eyJhbGciOiJub25lIn0.${payload}.synthetic`,
      email: `${role}@example.test`,
      clinicId: viewingClinicId,
      tenantId: viewingClinicId,
      userId: `${role}-user`,
      builderId: role === "builder" ? "builder-user" : null,
      role,
      viewingClinicId,
      leadSubmitted: true,
    }));
    window.history.replaceState({}, "", "/config?tab=operacao");
  }

  const editableConfig = {
    clinic_name: "Clinic",
    clinic_legal_name: "Clinic Legal Name",
    clinic_cnpj: "12.345.678/0001-90",
    dpo_name: "DPO Name",
    dpo_contact: "dpo@example.test",
    builder_lgpd_consent: true,
    meta_minutos_atendimento: 15,
  };

  afterEach(() => {
    globalThis.fetch = originalFetch;
    localStorage.clear();
    window.history.replaceState({}, "", "/");
    cleanup();
    vi.restoreAllMocks();
  });

  it("no longer renders the free-text 'Horário de Funcionamento' input or the outside-hours-metric toggle", () => {
    mountAsInvestor();
    render(<PageWrapper><ClinicConfig /></PageWrapper>);

    // Removed free-text field — its stable testid must be absent from the DOM.
    expect(screen.queryByTestId("input-working-hours")).toBeNull();

    // Removed opt-in toggle — the SwitchRow's copy is unique to that control.
    expect(screen.queryByText(/Métrica de atendimentos fora do horário/)).toBeNull();
    expect(screen.queryByText(/Ativar métrica de atendimentos fora do horário/)).toBeNull();

    // Confirm the operations tab actually rendered by locating the sibling
    // "Horário do Setor de Agendamento" field that must remain untouched.
    expect(screen.getByText(/Horário do Setor de Agendamento/)).toBeInTheDocument();
  });

  it("renders both hours fields as compact summaries with on-demand editors", () => {
    mountAsInvestor();
    render(<PageWrapper><ClinicConfig /></PageWrapper>);

    const editor = screen.getByTestId("business-hours-editor");
    expect(within(editor).getByTestId("business-hours-editor-summary")).toHaveTextContent(
      "Seg–Sex: 09:00–19:00 | Sáb: 09:00–14:00 | Dom: fechado",
    );
    expect(within(editor).queryByTestId("business-hours-row-mon")).toBeNull();

    fireEvent.click(within(editor).getByTestId("business-hours-editor-toggle"));
    for (const day of ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]) {
      expect(within(editor).getByTestId(`business-hours-row-${day}`)).toBeInTheDocument();
      expect(within(editor).getByTestId(`business-hours-switch-${day}`)).toBeInTheDocument();
    }
    expect(within(editor).getByTestId("business-hours-metric-note")).toBeInTheDocument();

    const schedulingEditor = screen.getByTestId("scheduling-hours-editor");
    expect(within(schedulingEditor).getByTestId("scheduling-hours-editor-summary"))
      .toHaveTextContent("Seg–Sex: 8h–18h");
    expect(within(schedulingEditor).queryByRole("textbox")).toBeNull();

    fireEvent.click(within(schedulingEditor).getByTestId("scheduling-hours-editor-toggle"));
    expect(within(schedulingEditor).getByRole("textbox", { name: /Horário do Setor de Agendamento/ }))
      .toHaveValue("Seg–Sex: 8h–18h");
  });

  it("lets clinic admins edit the hydrated service goal in its operation-tab position and saves a number", async () => {
    mountAsRole("clinic_admin", "clinic-admin");
    const fetchMock = vi.fn().mockImplementation(async (_url: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PATCH") return response(editableConfig);
      return response(editableConfig);
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    render(<PageWrapper><ClinicConfig /></PageWrapper>);

    const input = await screen.findByTestId("input-meta-minutos-atendimento");
    const schedulingEditor = screen.getByTestId("scheduling-hours-editor");
    const schedulingLink = screen.getByText("Scheduling Link");
    expect(input).toBeEnabled();
    expect(input).toHaveValue(15);
    expect(input).toHaveAttribute("type", "number");
    expect(input).toHaveAttribute("min", "1");
    expect(input).toHaveAttribute("max", "120");
    expect(schedulingEditor.compareDocumentPosition(input) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(input.compareDocumentPosition(schedulingLink) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByRole("tab", { name: "Handoff" })).not.toBeInTheDocument();

    fireEvent.change(input, { target: { value: "20" } });
    fireEvent.click(screen.getByTestId("btn-save-config"));

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(true);
    });
    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
    const body = JSON.parse(String(patch?.[1]?.body));
    expect(body.meta_minutos_atendimento).toBe(20);
    expect(typeof body.meta_minutos_atendimento).toBe("number");
  });

  it("renders one service-goal input in Operation and none after an all-tabs role opens Handoff", async () => {
    mountAsRole("builder", "clinic-builder");
    globalThis.fetch = vi.fn().mockResolvedValue(response(editableConfig)) as unknown as typeof fetch;

    render(<PageWrapper><ClinicConfig /></PageWrapper>);

    await screen.findByTestId("input-meta-minutos-atendimento");
    expect(screen.getAllByTestId("input-meta-minutos-atendimento")).toHaveLength(1);
    cleanup();
    window.history.replaceState({}, "", "/config?tab=handoff");
    render(<PageWrapper><ClinicConfig /></PageWrapper>);
    await waitFor(() => expect(screen.getByRole("tab", { name: "Handoff" }))
      .toHaveAttribute("aria-selected", "true"));
    expect(within(screen.getByRole("tabpanel")).queryByTestId("input-meta-minutos-atendimento"))
      .not.toBeInTheDocument();
  });
});
