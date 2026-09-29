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

const mocks = vi.hoisted(() => ({ toast: vi.fn() }));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mocks.toast }),
}));

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

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

/** A tenant that holds no provider at all. */
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
  llm: { dropout: "coming_soon", gemini: "active", openai: "coming_soon", outros: "coming_soon", slm_cortex: "coming_soon" },
  multimodal: { openrouter: "coming_soon" },
  stt: { outros: "coming_soon", openai_whisper: "coming_soon", elevenlabs_scribe: "coming_soon", google_stt: "coming_soon", deepgram_stt: "coming_soon", microsoft_stt: "coming_soon" },
  tts: { elevenlabs: "coming_soon", google: "coming_soon", openai: "coming_soon" },
} as const;
const TOTAL_PROVIDER_CARD_COUNT = 21;
const COMING_SOON_PROVIDER_COUNT = 20;

const ACTIVE_GEMINI_PROVIDER_AVAILABILITY = {
  ...API_PROVIDER_AVAILABILITY,
  llm: {
    ...API_PROVIDER_AVAILABILITY.llm,
    gemini: "active",
  },
} as const;

const ACTIVE_GEMINI_AND_OPENAI_PROVIDER_AVAILABILITY = {
  ...ACTIVE_GEMINI_PROVIDER_AVAILABILITY,
  llm: {
    ...ACTIVE_GEMINI_PROVIDER_AVAILABILITY.llm,
    openai: "active",
  },
} as const;

const ACTIVE_OPENAI_TTS_PROVIDER_AVAILABILITY = {
  ...API_PROVIDER_AVAILABILITY,
  tts: {
    ...API_PROVIDER_AVAILABILITY.tts,
    openai: "active",
  },
} as const;

const GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai";
const OPENAI_BASE_URL = "https://api.openai.com/v1";

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

  it.each([
    [
      "pt",
      "invalid_provider_endpoint",
      "API LLM: A URL da API não corresponde ao endpoint oficial deste provedor",
    ],
    [
      "en",
      "invalid_provider_endpoint",
      "API LLM: The API URL does not match this provider's official endpoint",
    ],
    [
      "pt",
      "provider_switch_bundle_required",
      "API LLM: Para trocar o provedor, informe juntos a URL da API, uma nova API Key e o modelo",
    ],
    [
      "en",
      "provider_switch_bundle_required",
      "API LLM: To switch providers, enter the API URL, a new API key, and the model together",
    ],
  ])("maps %s %s without echoing backend bundle values", (locale, error, expected) => {
    const description = describeConfigSaveFailure({
      detail: {
        error,
        capability: "llm",
        submitted_url: "https://private.example.test/secret-path",
        api_key: "gemini-secret-key",
        bundle: { llm_model_name: "tenant-private-model" },
      },
    }, 422, locale);

    expect(description).toContain(expected);
    expect(description).not.toContain(error);
    expect(description).not.toContain("private.example.test");
    expect(description).not.toContain("gemini-secret-key");
    expect(description).not.toContain("tenant-private-model");
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
  it("contains the exact 21 backend pairs with Gemini as the sole active overlay", () => {
    expect(API_PROVIDER_AVAILABILITY).toEqual(BACKEND_PROVIDER_AVAILABILITY);
    const states = Object.values(API_PROVIDER_AVAILABILITY).flatMap(Object.values);
    expect(states).toHaveLength(TOTAL_PROVIDER_CARD_COUNT);
    expect(states.filter((state) => state === "active")).toHaveLength(1);
    expect(states.filter((state) => state === "coming_soon")).toHaveLength(COMING_SOON_PROVIDER_COUNT);
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

  it("keeps OpenAI TTS coming soon without tenant status", () => {
    expect(providerCardState("tts", "openai", null)).toBe("coming_soon");
  });

  it("promotes Gemini in the real matrix and exercises every state", () => {
    expect(providerCardState("llm", "gemini", emptyStatus())).toBe("available");
    expect(providerCardState(
      "llm",
      "gemini",
      emptyStatus(),
      ACTIVE_GEMINI_PROVIDER_AVAILABILITY,
    )).toBe("available");
    expect(providerCardState(
      "llm",
      "gemini",
      emptyStatus({ llm_provider: "gemini", llm_readiness: "incomplete" }),
      ACTIVE_GEMINI_PROVIDER_AVAILABILITY,
    )).toBe("incomplete");
    expect(providerCardState(
      "llm",
      "gemini",
      emptyStatus({ llm_provider: "gemini", llm_readiness: "active" }),
      ACTIVE_GEMINI_PROVIDER_AVAILABILITY,
    )).toBe("configured");
  });
});

describe("<ApiCredentialsForm /> card states", () => {
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    globalThis.fetch = originalFetch;
    localStorage.clear();
    window.history.replaceState({}, "", "/");
    cleanup();
    mocks.toast.mockReset();
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
  ])("renders 20 coming-soon cards and one active Gemini card in %s", async (
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
    expect(cards).toHaveLength(TOTAL_PROVIDER_CARD_COUNT);
    expect(badges).toHaveLength(COMING_SOON_PROVIDER_COUNT);
    expect(within(section).getByText(availableLabel)).toBeInTheDocument();
    expect(within(section).queryByText(configurationOnlyLabel)).not.toBeInTheDocument();
    expect(section.querySelectorAll("button")).toHaveLength(1);

    for (const card of cards.filter(card => card.dataset.cardState === "coming_soon")) {
      expect(card.tagName).toBe("ARTICLE");
      expect(card).toHaveAttribute("data-card-state", "coming_soon");
      expect(card).not.toHaveAttribute("aria-expanded");
      fireEvent.click(card);
    }

    expect(within(section).queryByTestId(/provider-editor-/)).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(0);
  });

  it("shows OpenAI TTS copy and prefills only its URL in the credentials editor", async () => {
    localStorage.setItem("ns_locale", "en");
    const fetchMock = await renderForm(emptyStatus(), {
      providerAvailability: ACTIVE_OPENAI_TTS_PROVIDER_AVAILABILITY,
    });

    const card = screen.getByTestId("provider-card-tts-openai");
    expect(within(card).getByText("OpenAI TTS")).toBeInTheDocument();
    expect(within(card).getByText("gpt-4o-audio / tts-1")).toBeInTheDocument();
    fireEvent.click(card);

    const editor = screen.getByTestId("provider-editor-tts-openai");
    const url = within(editor).getByTestId("field-tts_api_url");
    const key = within(editor).getByTestId("field-tts_api_key");
    const model = within(editor).getByTestId("field-tts_model_name");
    const voice = within(editor).getByTestId("field-tts_voice_id");
    expect(url).toHaveValue(OPENAI_BASE_URL);
    expect(key).toHaveValue("");
    expect(model).toHaveValue("");
    expect(voice).toHaveValue("");

    fireEvent.change(key, { target: { value: "new-openai-key" } });
    fireEvent.change(model, { target: { value: "gpt-4o-mini-tts" } });
    fireEvent.change(voice, { target: { value: "alloy" } });
    fireEvent.click(within(editor).getByTestId("save-tts-openai"));

    await waitFor(() => expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH"))
      .toHaveLength(1));
    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
    expect(JSON.parse(String(patch?.[1]?.body))).toEqual({
      tts_provider: "openai",
      tts_api_url: OPENAI_BASE_URL,
      tts_api_key: "new-openai-key",
      tts_model_name: "gpt-4o-mini-tts",
      tts_voice_id: "alloy",
    });
  });

  it("uses the English COMING SOON badge on 20 cards and AVAILABLE on active Gemini", async () => {
    localStorage.setItem("ns_locale", "en");
    await renderForm(emptyStatus());

    const section = screen.getByTestId("api-settings-section");
    expect(within(section).getAllByText("COMING SOON")).toHaveLength(COMING_SOON_PROVIDER_COUNT);
    expect(within(section).getByText("AVAILABLE")).toBeInTheDocument();
  });

  it.each([
    ["pt", "INCOMPLETO"],
    ["en", "INCOMPLETE"],
  ])("renders an incomplete Gemini fixture with the %s badge", async (locale, badge) => {
    localStorage.setItem("ns_locale", locale);
    await renderForm(emptyStatus({
      llm_provider: "gemini",
      llm_readiness: "incomplete",
    }), { providerAvailability: ACTIVE_GEMINI_PROVIDER_AVAILABILITY });

    const card = screen.getByTestId("provider-card-llm-gemini");
    expect(card).toHaveAttribute("data-card-state", "incomplete");
    expect(within(card).getByText("Gemini")).toBeInTheDocument();
    expect(within(card).getByText("by Google")).toBeInTheDocument();
    expect(within(card).getByText(badge)).toBeInTheDocument();
  });

  it("prefills a new Gemini URL, isolates the other provider, and sends the full shared switch bundle", async () => {
    localStorage.setItem("ns_locale", "en");
    const fetchMock = await renderForm(emptyStatus({
      llm_provider: "openai",
      llm_readiness: "active",
      llm_base_url: "https://api.openai.com/v1",
      llm_model_name: "gpt-private-model",
      llm_api_key_set: true,
    }), { providerAvailability: ACTIVE_GEMINI_PROVIDER_AVAILABILITY });

    fireEvent.click(screen.getByTestId("provider-card-llm-gemini"));
    const editor = screen.getByTestId("provider-editor-llm-gemini");
    const url = within(editor).getByTestId("field-llm_api_url");
    const key = within(editor).getByTestId("field-llm_api_key");
    const model = within(editor).getByTestId("field-llm_model_name");

    expect(url).toHaveValue(GEMINI_BASE_URL);
    expect(key).toHaveValue("");
    expect(key).not.toHaveAttribute("placeholder");
    expect(model).toHaveValue("");
    expect(editor).not.toHaveTextContent("gpt-private-model");

    fireEvent.change(key, { target: { value: "new-gemini-key" } });
    fireEvent.change(model, { target: { value: "gemini-2.5-flash" } });
    fireEvent.click(within(editor).getByTestId("save-llm-gemini"));

    await waitFor(() => expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH"))
      .toHaveLength(1));
    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
    expect(JSON.parse(String(patch?.[1]?.body))).toEqual({
      llm_provider: "gemini",
      llm_api_url: GEMINI_BASE_URL,
      llm_api_key: "new-gemini-key",
      llm_model_name: "gemini-2.5-flash",
    });
  });

  it("resets unsaved shared LLM drafts when switching between active provider cards", async () => {
    localStorage.setItem("ns_locale", "en");
    await renderForm(emptyStatus(), {
      providerAvailability: ACTIVE_GEMINI_AND_OPENAI_PROVIDER_AVAILABILITY,
    });

    fireEvent.click(screen.getByTestId("provider-card-llm-gemini"));
    expect(screen.getByTestId("field-llm_api_url")).toHaveValue(GEMINI_BASE_URL);
    fireEvent.change(screen.getByTestId("field-llm_api_key"), {
      target: { value: "unsaved-gemini-secret" },
    });
    fireEvent.change(screen.getByTestId("field-llm_model_name"), {
      target: { value: "unsaved-gemini-model" },
    });

    fireEvent.click(screen.getByTestId("provider-card-llm-openai"));
    expect(screen.getByTestId("provider-editor-llm-openai")).toBeInTheDocument();
    expect(screen.getByTestId("field-llm_api_url")).toHaveValue("");
    expect(screen.getByTestId("field-llm_api_key")).toHaveValue("");
    expect(screen.getByTestId("field-llm_model_name")).toHaveValue("");

    fireEvent.click(screen.getByTestId("provider-card-llm-gemini"));
    expect(screen.getByTestId("field-llm_api_url")).toHaveValue(GEMINI_BASE_URL);
    expect(screen.getByTestId("field-llm_api_key")).toHaveValue("");
    expect(screen.getByTestId("field-llm_model_name")).toHaveValue("");
    expect(screen.getByTestId("provider-editor-llm-gemini"))
      .not.toHaveTextContent("unsaved-gemini-model");
  });

  it("hydrates only held Gemini non-secrets and keeps a same-provider model edit delta-only", async () => {
    localStorage.setItem("ns_locale", "pt");
    const fetchMock = await renderForm(emptyStatus({
      llm_provider: "gemini",
      llm_readiness: "active",
      llm_base_url: GEMINI_BASE_URL,
      llm_model_name: "gemini-2.0-flash",
      llm_api_key_set: true,
    }), { providerAvailability: ACTIVE_GEMINI_PROVIDER_AVAILABILITY });

    const card = screen.getByTestId("provider-card-llm-gemini");
    expect(card).toHaveAttribute("data-card-state", "configured");
    expect(within(card).getByText("CONFIGURADO")).toBeInTheDocument();
    fireEvent.click(card);

    const editor = screen.getByTestId("provider-editor-llm-gemini");
    expect(within(editor).getByTestId("field-llm_api_url")).toHaveValue(GEMINI_BASE_URL);
    expect(within(editor).getByTestId("field-llm_api_key")).toHaveValue("");
    expect(within(editor).getByTestId("field-llm_api_key"))
      .toHaveAttribute("placeholder", "•••••••• (manter atual)");
    const model = within(editor).getByTestId("field-llm_model_name");
    expect(model).toHaveValue("gemini-2.0-flash");

    fireEvent.change(model, { target: { value: "gemini-2.5-pro" } });
    fireEvent.click(within(editor).getByTestId("save-llm-gemini"));

    await waitFor(() => expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH"))
      .toHaveLength(1));
    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
    expect(JSON.parse(String(patch?.[1]?.body))).toEqual({
      llm_model_name: "gemini-2.5-pro",
    });
  });

  it("drops an open Gemini draft and all prior-clinic hydration when the clinic changes", async () => {
    localStorage.setItem("ns_locale", "en");
    const fetchMock = vi.fn().mockImplementation(async (url: RequestInfo | URL) => {
      if (String(url).includes("clinic-a")) {
        return response(emptyStatus({
          llm_provider: "gemini",
          llm_readiness: "active",
          llm_base_url: GEMINI_BASE_URL,
          llm_model_name: "clinic-a-model",
          llm_api_key_set: true,
        }));
      }
      return response(emptyStatus({
        llm_provider: "openai",
        llm_readiness: "active",
        llm_base_url: "https://api.openai.com/v1",
        llm_model_name: "clinic-b-openai-model",
        llm_api_key_set: true,
      }));
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const { rerender } = render(
      <Wrapper>
        <ApiCredentialsForm
          clinicId="clinic-a"
          isReadOnly={false}
          isSuperAdmin={false}
          providerAvailability={ACTIVE_GEMINI_PROVIDER_AVAILABILITY}
        />
      </Wrapper>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByTestId("provider-card-llm-gemini"));
    fireEvent.change(screen.getByTestId("field-llm_api_key"), {
      target: { value: "clinic-a-unsaved-secret" },
    });
    fireEvent.change(screen.getByTestId("field-llm_model_name"), {
      target: { value: "clinic-a-unsaved-model" },
    });

    rerender(
      <Wrapper>
        <ApiCredentialsForm
          clinicId="clinic-b"
          isReadOnly={false}
          isSuperAdmin={false}
          providerAvailability={ACTIVE_GEMINI_PROVIDER_AVAILABILITY}
        />
      </Wrapper>,
    );
    expect(screen.queryByTestId("provider-editor-llm-gemini")).not.toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByTestId("provider-card-llm-gemini"))
      .toHaveAttribute("data-card-state", "available"));

    fireEvent.click(screen.getByTestId("provider-card-llm-gemini"));
    expect(screen.getByTestId("field-llm_api_url")).toHaveValue(GEMINI_BASE_URL);
    expect(screen.getByTestId("field-llm_api_key")).toHaveValue("");
    expect(screen.getByTestId("field-llm_api_key")).not.toHaveAttribute("placeholder");
    expect(screen.getByTestId("field-llm_model_name")).toHaveValue("");
    expect(screen.getByTestId("provider-editor-llm-gemini"))
      .not.toHaveTextContent("clinic-a-unsaved-model");
    expect(screen.getByTestId("provider-editor-llm-gemini"))
      .not.toHaveTextContent("clinic-b-openai-model");
  });

  it("discards a stale clinic GET that resolves after the current clinic", async () => {
    localStorage.setItem("ns_locale", "en");
    const clinicAGet = deferred<Response>();
    const clinicBGet = deferred<Response>();
    const clinicBPatch = deferred<Response>();
    const fetchMock = vi.fn().mockImplementation((url: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PATCH") return clinicBPatch.promise;
      return String(url).includes("clinic-a") ? clinicAGet.promise : clinicBGet.promise;
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const { rerender } = render(
      <Wrapper>
        <ApiCredentialsForm
          clinicId="clinic-a"
          isReadOnly={false}
          isSuperAdmin={false}
          providerAvailability={ACTIVE_GEMINI_PROVIDER_AVAILABILITY}
        />
      </Wrapper>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    rerender(
      <Wrapper>
        <ApiCredentialsForm
          clinicId="clinic-b"
          isReadOnly={false}
          isSuperAdmin={false}
          providerAvailability={ACTIVE_GEMINI_PROVIDER_AVAILABILITY}
        />
      </Wrapper>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await act(async () => {
      clinicBGet.resolve(response(emptyStatus()));
      await clinicBGet.promise;
    });
    await waitFor(() => expect(screen.getByTestId("provider-card-llm-gemini"))
      .toHaveAttribute("data-card-state", "available"));

    await act(async () => {
      clinicAGet.resolve(response(emptyStatus({
        llm_provider: "gemini",
        llm_readiness: "active",
        llm_base_url: "https://clinic-a.example.test/v1",
        llm_model_name: "clinic-a-model",
        llm_api_key_set: true,
      })));
      await clinicAGet.promise;
    });

    expect(screen.getByTestId("provider-card-llm-gemini"))
      .toHaveAttribute("data-card-state", "available");
    fireEvent.click(screen.getByTestId("provider-card-llm-gemini"));
    expect(screen.getByTestId("field-llm_api_url")).toHaveValue(GEMINI_BASE_URL);
    expect(screen.getByTestId("field-llm_api_key")).toHaveValue("");
    expect(screen.getByTestId("field-llm_api_key")).not.toHaveAttribute("placeholder");
    expect(screen.getByTestId("field-llm_model_name")).toHaveValue("");

    fireEvent.change(screen.getByTestId("field-llm_api_key"), {
      target: { value: "clinic-b-key" },
    });
    fireEvent.change(screen.getByTestId("field-llm_model_name"), {
      target: { value: "clinic-b-model" },
    });
    fireEvent.click(screen.getByTestId("save-llm-gemini"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(JSON.parse(String(fetchMock.mock.calls[2]?.[1]?.body))).toEqual({
      llm_provider: "gemini",
      llm_api_url: GEMINI_BASE_URL,
      llm_api_key: "clinic-b-key",
      llm_model_name: "clinic-b-model",
    });

    await act(async () => {
      clinicBPatch.resolve(response(emptyStatus({
        llm_provider: "gemini",
        llm_readiness: "active",
        llm_base_url: GEMINI_BASE_URL,
        llm_model_name: "clinic-b-model",
        llm_api_key_set: true,
      })));
      await clinicBPatch.promise;
    });
    await waitFor(() => expect(screen.getByTestId("provider-card-llm-gemini"))
      .toHaveAttribute("data-card-state", "configured"));
    expect(mocks.toast).toHaveBeenCalledWith({ title: "Credentials saved" });
  });

  it.each(["success", "failure"] as const)(
    "discards a stale clinic PATCH %s after switching clinics",
    async (outcome) => {
      localStorage.setItem("ns_locale", "en");
      const clinicAPatch = deferred<Response>();
      const clinicBPatch = deferred<Response>();
      const fetchMock = vi.fn().mockImplementation((url: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "PATCH") {
          return String(url).includes("clinic-a") ? clinicAPatch.promise : clinicBPatch.promise;
        }
        if (String(url).includes("clinic-a")) return Promise.resolve(response(emptyStatus()));
        return Promise.resolve(response(emptyStatus({
          llm_provider: "gemini",
          llm_readiness: "active",
          llm_base_url: GEMINI_BASE_URL,
          llm_model_name: "clinic-b-model",
          llm_api_key_set: true,
        })));
      });
      globalThis.fetch = fetchMock as unknown as typeof fetch;

      const { rerender } = render(
        <Wrapper>
          <ApiCredentialsForm
            clinicId="clinic-a"
            isReadOnly={false}
            isSuperAdmin={false}
            providerAvailability={ACTIVE_GEMINI_PROVIDER_AVAILABILITY}
          />
        </Wrapper>,
      );
      await waitFor(() => expect(screen.getByTestId("provider-card-llm-gemini"))
        .toHaveAttribute("data-card-state", "available"));
      fireEvent.click(screen.getByTestId("provider-card-llm-gemini"));
      fireEvent.change(screen.getByTestId("field-llm_api_key"), {
        target: { value: "clinic-a-key" },
      });
      fireEvent.change(screen.getByTestId("field-llm_model_name"), {
        target: { value: "clinic-a-model" },
      });
      fireEvent.click(screen.getByTestId("save-llm-gemini"));
      await waitFor(() => expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH"))
        .toHaveLength(1));

      rerender(
        <Wrapper>
          <ApiCredentialsForm
            clinicId="clinic-b"
            isReadOnly={false}
            isSuperAdmin={false}
            providerAvailability={ACTIVE_GEMINI_PROVIDER_AVAILABILITY}
          />
        </Wrapper>,
      );
      await waitFor(() => expect(screen.getByTestId("provider-card-llm-gemini"))
        .toHaveAttribute("data-card-state", "configured"));
      fireEvent.click(screen.getByTestId("provider-card-llm-gemini"));
      expect(screen.getByTestId("field-llm_api_url")).toHaveValue(GEMINI_BASE_URL);
      expect(screen.getByTestId("field-llm_api_key"))
        .toHaveAttribute("placeholder", "•••••••• (keep current)");
      expect(screen.getByTestId("field-llm_model_name")).toHaveValue("clinic-b-model");

      await act(async () => {
        clinicAPatch.resolve(outcome === "success"
          ? response(emptyStatus({
            llm_provider: "gemini",
            llm_readiness: "active",
            llm_base_url: "https://clinic-a.example.test/v1",
            llm_model_name: "clinic-a-model",
            llm_api_key_set: false,
          }))
          : response({ detail: { error: "unsafe_api_url", field: "llm_api_url" } }, false));
        await clinicAPatch.promise;
      });

      expect(screen.getByTestId("provider-editor-llm-gemini")).toBeInTheDocument();
      expect(screen.getByTestId("field-llm_api_url")).toHaveValue(GEMINI_BASE_URL);
      expect(screen.getByTestId("field-llm_api_key"))
        .toHaveAttribute("placeholder", "•••••••• (keep current)");
      expect(screen.getByTestId("field-llm_model_name")).toHaveValue("clinic-b-model");
      expect(mocks.toast).not.toHaveBeenCalled();

      fireEvent.change(screen.getByTestId("field-llm_model_name"), {
        target: { value: "clinic-b-next-model" },
      });
      fireEvent.click(screen.getByTestId("save-llm-gemini"));
      await waitFor(() => expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH"))
        .toHaveLength(2));
      const patchCalls = fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH");
      expect(String(patchCalls[1]?.[0])).toContain("clinic-b");
      expect(JSON.parse(String(patchCalls[1]?.[1]?.body))).toEqual({
        llm_model_name: "clinic-b-next-model",
      });

      await act(async () => {
        clinicBPatch.resolve(response(emptyStatus({
          llm_provider: "gemini",
          llm_readiness: "active",
          llm_base_url: GEMINI_BASE_URL,
          llm_model_name: "clinic-b-next-model",
          llm_api_key_set: true,
        })));
        await clinicBPatch.promise;
      });
      await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith({ title: "Credentials saved" }));
    },
  );

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

    expect(within(section).getAllByText("EM BREVE")).toHaveLength(COMING_SOON_PROVIDER_COUNT);
    expect(within(section).getByText("DISPONÍVEL")).toBeInTheDocument();
    expect(within(section).queryByText("CONFIGURADO")).not.toBeInTheDocument();
    expect(within(section).queryByText("INCOMPLETO")).not.toBeInTheDocument();
    expect(within(section).queryByTestId(/provider-editor-/)).not.toBeInTheDocument();
    expect(section.querySelectorAll("input, select, textarea")).toHaveLength(0);
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(0);
  });

  it("fails closed all 21 provider cards and warns when the status fetch fails", async () => {
    localStorage.setItem("ns_locale", "pt");
    const fetchMock = vi.fn().mockResolvedValue(response({}, false, 500));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    render(
      <Wrapper><ApiCredentialsForm clinicId="clinic-42" isReadOnly={false} isSuperAdmin={false} /></Wrapper>,
    );
    await screen.findByTestId("api-settings-load-error");

    // Fail closed: an unknown tenant state must never make a card editable.
    const section = screen.getByTestId("api-settings-section");
    expect(within(section).getAllByText("EM BREVE")).toHaveLength(TOTAL_PROVIDER_CARD_COUNT);
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
    expect(within(apiSection).getAllByTestId(/provider-card-/)).toHaveLength(TOTAL_PROVIDER_CARD_COUNT);
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
