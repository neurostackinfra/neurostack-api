import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";

import {
  API_PROVIDER_AVAILABILITY,
  ApiCredentialsForm,
  default as ClinicConfig,
  PROVIDERS,
  STT_PROVIDERS,
  TTS_PROVIDERS,
  WA_PROVIDERS,
  providerCardState,
} from "./clinic-config";
import { VoiceResponseControls } from "@/components/api-settings/voice-response-controls";
import { AuthProvider } from "@/context/auth";
import { I18nProvider } from "@/context/i18n";

function Wrapper({ children }: { children: ReactNode }) {
  return <I18nProvider>{children}</I18nProvider>;
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
  whatsapp: {
    meta_cloud: "coming_soon",
    zapi: "coming_soon",
    uazapi: "coming_soon",
    evolution: "coming_soon",
    dialog360: "coming_soon",
    twilio: "coming_soon",
  },
  llm: {
    dropout: "coming_soon",
    openai: "coming_soon",
    outros: "coming_soon",
    slm_cortex: "coming_soon",
  },
  multimodal: { openrouter: "coming_soon" },
  stt: {
    outros: "coming_soon",
    openai_whisper: "coming_soon",
    elevenlabs_scribe: "coming_soon",
    google_stt: "coming_soon",
    deepgram_stt: "coming_soon",
    microsoft_stt: "coming_soon",
  },
  tts: { elevenlabs: "coming_soon", google: "coming_soon" },
} as const;

describe("frontend provider availability mirror", () => {
  it("contains the exact 19 backend pairs, all coming soon", () => {
    expect(API_PROVIDER_AVAILABILITY).toEqual(BACKEND_PROVIDER_AVAILABILITY);
    expect(Object.values(API_PROVIDER_AVAILABILITY)
      .flatMap((providers) => Object.values(providers)))
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
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

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

  it("keeps every card inert for a read-only viewer even when a provider is held", async () => {
    await renderForm(
      emptyStatus({ llm_provider: "openai", llm_readiness: "active" }),
      { isReadOnly: true },
    );

    const section = screen.getByTestId("api-settings-section");
    expect(section.querySelectorAll("input, textarea, select")).toHaveLength(0);
    expect(within(section).queryByTestId(/provider-editor-/)).not.toBeInTheDocument();
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
