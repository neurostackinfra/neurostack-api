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
  it("contains the exact 19 backend pairs, all coming soon", () => {
    expect(API_PROVIDER_AVAILABILITY).toEqual(BACKEND_PROVIDER_AVAILABILITY);
    expect(Object.values(API_PROVIDER_AVAILABILITY).flatMap(Object.values))
      .toEqual(Array.from({ length: 19 }, () => "coming_soon"));
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
});

describe("providerCardState", () => {
  it("keeps a provider the tenant does not hold coming soon", () => {
    expect(providerCardState("llm", "openai", emptyStatus())).toBe("coming_soon");
    expect(providerCardState("llm", "openai", null)).toBe("coming_soon");
    expect(providerCardState(
      "llm",
      "openai",
      emptyStatus({ llm_provider: "outros", llm_readiness: "active" }),
    )).toBe("coming_soon");
  });

  it.each(["active", "configuration_only"])(
    "reads a held provider with %s readiness as configured",
    (readiness) => {
      expect(providerCardState(
        "stt",
        "openai_whisper",
        emptyStatus({ stt_provider: "openai_whisper", stt_readiness: readiness }),
      )).toBe("configured");
    },
  );

  it("reads a held provider with a partial bundle as incomplete", () => {
    expect(providerCardState(
      "llm",
      "openai",
      emptyStatus({ llm_provider: "openai", llm_readiness: "incomplete" }),
    )).toBe("incomplete");
  });

  it("does not claim a signature_pending Meta bundle is configured", () => {
    // `signature_pending` means the app secret is missing, so the bundle is
    // partial and must not carry the CONFIGURADO seal.
    expect(providerCardState(
      "whatsapp",
      "meta_cloud",
      emptyStatus({ whatsapp_provider: "meta_cloud", whatsapp_readiness: "signature_pending" }),
    )).toBe("incomplete");
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

  it("renders all 19 Portuguese cards as inert EM BREVE for a tenant holding nothing", async () => {
    localStorage.setItem("ns_locale", "pt");
    await renderForm(emptyStatus());

    const section = screen.getByTestId("api-settings-section");
    expect(within(section).getAllByTestId(/provider-card-/)).toHaveLength(19);
    expect(within(section).getAllByText("EM BREVE")).toHaveLength(19);
    expect(section.querySelectorAll("input, button, select, textarea, [tabindex]")).toHaveLength(0);
    expect(within(section).queryByRole("button")).not.toBeInTheDocument();
  });

  it("uses the English COMING SOON badge for every unheld card", async () => {
    localStorage.setItem("ns_locale", "en");
    await renderForm(emptyStatus());

    expect(screen.getAllByText("COMING SOON")).toHaveLength(19);
  });

  it("marks only the persisted provider as CONFIGURADO and leaves the rest inert", async () => {
    localStorage.setItem("ns_locale", "pt");
    await renderForm(emptyStatus({
      llm_provider: "openai",
      llm_readiness: "active",
      llm_api_url: "https://api.openai.com/v1",
      llm_model_name: "gpt-4o",
      llm_api_key_set: true,
    }));

    expect(screen.getByTestId("provider-badge-configured-llm-openai"))
      .toHaveTextContent("CONFIGURADO");
    expect(screen.getAllByText("EM BREVE")).toHaveLength(18);
    // Exactly one card became interactive: the one the tenant already holds.
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(screen.getByTestId("provider-card-llm-openai").tagName).toBe("BUTTON");
  });

  it("marks a persisted but partial bundle INCOMPLETO and still lets it be repaired", async () => {
    localStorage.setItem("ns_locale", "pt");
    await renderForm(emptyStatus({
      whatsapp_provider: "meta_cloud",
      whatsapp_readiness: "signature_pending",
      meta_phone_number_id: "1234567890",
      meta_permanent_token_set: true,
    }));

    expect(screen.getByTestId("provider-badge-incomplete-whatsapp-meta_cloud"))
      .toHaveTextContent("INCOMPLETO");

    fireEvent.click(screen.getByTestId("provider-card-whatsapp-meta_cloud"));
    const editor = await screen.findByTestId("provider-editor-whatsapp-meta_cloud");
    // The missing field is reachable, which is the whole point of keeping an
    // incomplete bundle editable.
    expect(within(editor).getByTestId("field-meta_app_secret")).toBeInTheDocument();
    expect(within(editor).getByTestId("field-meta_phone_number_id"))
      .toHaveValue("1234567890");
  });

  it("hydrates non-secret values but never pre-fills a secret", async () => {
    await renderForm(emptyStatus({
      llm_provider: "openai",
      llm_readiness: "active",
      llm_api_url: "https://api.openai.com/v1",
      llm_model_name: "gpt-4o",
      llm_api_key_set: true,
      llm_api_key_masked: "sk-…abcd",
    }));

    fireEvent.click(screen.getByTestId("provider-card-llm-openai"));
    const editor = await screen.findByTestId("provider-editor-llm-openai");
    expect(within(editor).getByTestId("field-llm_api_url"))
      .toHaveValue("https://api.openai.com/v1");
    expect(within(editor).getByTestId("field-llm_api_key")).toHaveValue("");
    expect(editor.textContent).not.toContain("sk-…abcd");
  });

  it("patches only changed fields and omits an untouched secret", async () => {
    const fetchMock = await renderForm(emptyStatus({
      llm_provider: "openai",
      llm_readiness: "active",
      llm_api_url: "https://api.openai.com/v1",
      llm_model_name: "gpt-4o",
      llm_api_key_set: true,
    }));

    fireEvent.click(screen.getByTestId("provider-card-llm-openai"));
    await screen.findByTestId("provider-editor-llm-openai");
    fireEvent.change(screen.getByTestId("field-llm_model_name"), {
      target: { value: "gpt-4o-mini" },
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId("save-llm-openai"));
    });

    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
    expect(patch).toBeDefined();
    // An empty secret input means "keep the stored value", so it must not be
    // sent as an empty string that would clear the credential.
    expect(JSON.parse(String(patch![1].body))).toEqual({ llm_model_name: "gpt-4o-mini" });
  });

  it("surfaces a provider_not_available rejection without closing the editor", async () => {
    localStorage.setItem("ns_locale", "pt");
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(emptyStatus({
        llm_provider: "openai",
        llm_readiness: "active",
        llm_model_name: "gpt-4o",
      })))
      .mockResolvedValueOnce(response({ detail: { error: "provider_not_available" } }, false));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    render(
      <Wrapper>
        <ApiCredentialsForm clinicId="clinic-42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );
    // The card only becomes interactive once the loaded status lands in state,
    // so waiting on the fetch call alone would click an inert <article>.
    await waitFor(() => expect(
      screen.getByTestId("provider-card-llm-openai").tagName,
    ).toBe("BUTTON"));

    fireEvent.click(screen.getByTestId("provider-card-llm-openai"));
    await screen.findByTestId("provider-editor-llm-openai");
    fireEvent.change(screen.getByTestId("field-llm_model_name"), {
      target: { value: "rejected" },
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId("save-llm-openai"));
    });

    expect(screen.getByTestId("provider-editor-llm-openai")).toBeInTheDocument();
  });

  it("opens no editor for a read-only viewer even when a provider is held", async () => {
    await renderForm(
      emptyStatus({ llm_provider: "openai", llm_readiness: "active" }),
      { isReadOnly: true },
    );

    const section = screen.getByTestId("api-settings-section");
    expect(section.querySelectorAll("input, textarea, select")).toHaveLength(0);
    fireEvent.click(screen.getByTestId("provider-card-llm-openai"));
    expect(within(section).queryByTestId(/provider-editor-/)).not.toBeInTheDocument();
  });

  it("drops the previous tenant's open editor when the viewed clinic changes", async () => {
    const held = emptyStatus({
      llm_provider: "openai",
      llm_readiness: "active",
      llm_model_name: "clinic-a-model",
    });
    const fetchMock = vi.fn().mockResolvedValue(response(held));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const { rerender } = render(
      <Wrapper><ApiCredentialsForm clinicId="clinic-a" isReadOnly={false} isSuperAdmin /></Wrapper>,
    );
    await waitFor(() => expect(
      screen.getByTestId("provider-card-llm-openai").tagName,
    ).toBe("BUTTON"));
    fireEvent.click(screen.getByTestId("provider-card-llm-openai"));
    await screen.findByTestId("provider-editor-llm-openai");

    // The builder switches clinics; the prop changes without a remount, so the
    // component itself has to drop clinic A's editor and draft.
    fetchMock.mockResolvedValue(response(emptyStatus()));
    await act(async () => {
      rerender(
        <Wrapper><ApiCredentialsForm clinicId="clinic-b" isReadOnly={false} isSuperAdmin /></Wrapper>,
      );
    });

    expect(screen.queryByTestId("provider-editor-llm-openai")).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue("clinic-a-model")).not.toBeInTheDocument();
    expect(screen.getByTestId("provider-card-llm-openai").tagName).toBe("ARTICLE");
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

  it("renders the new weekly business-hours editor with 7 day switches", () => {
    mountAsInvestor();
    render(<PageWrapper><ClinicConfig /></PageWrapper>);

    const editor = screen.getByTestId("business-hours-editor");
    for (const day of ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]) {
      expect(within(editor).getByTestId(`business-hours-row-${day}`)).toBeInTheDocument();
      expect(within(editor).getByTestId(`business-hours-switch-${day}`)).toBeInTheDocument();
    }
    // Static metric note anchors the "no opt-in" contract on the mounted page.
    expect(within(editor).getByTestId("business-hours-metric-note")).toBeInTheDocument();
  });
});
