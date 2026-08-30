import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, cleanup, fireEvent, within } from "@testing-library/react";
import type { ReactNode } from "react";

import {
  API_CAPABILITIES,
  ApiCredentialsForm,
  describeConfigSaveFailure,
  PROVIDERS,
  SELECTABLE_LLM_PROVIDERS,
  STT_PROVIDERS,
  TTS_PROVIDERS,
} from "./clinic-config";
import { I18nProvider } from "@/context/i18n";

// Backend canonical STT allowlist. Mirror of
// `PROVIDER_ALLOWLISTS["stt"]` in `src/services/api_credentials.py`.
// Any drift here breaks credential save/load parity.
const BACKEND_STT_IDS: readonly string[] = [
  "outros",
  "openai_whisper",
  "elevenlabs_scribe",
  "google_stt",
  "deepgram_stt",
  "microsoft_stt",
];

// Backend canonical LLM allowlist (excluding `slm_cortex` which the backend
// rejects for writes — see `_llm_operational`). The frontend must never send
// `slm_cortex` in a PATCH body.
const BACKEND_LLM_OPERATIONAL: readonly string[] = ["openai", "dropout", "outros"];
const BACKEND_TTS_IDS: readonly string[] = ["elevenlabs", "google"];

function Wrapper({ children }: { children: ReactNode }) {
  return <I18nProvider>{children}</I18nProvider>;
}

interface FetchResponse {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
}

function jsonResponse(body: unknown, ok = true, status = 200): FetchResponse {
  return {
    ok,
    status,
    json: async () => body,
  };
}

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

  it("lists every invalid business-hours field using friendly labels", () => {
    const description = describeConfigSaveFailure({
      detail: [
        { loc: ["business_hours_open"], input: "99:99" },
        { loc: ["business_hours_days"], input: ["invalid-day"] },
      ],
    }, 422, "pt");

    expect(description).toContain("horário de abertura");
    expect(description).toContain("dias de atendimento");
    expect(description).not.toContain("99:99");
    expect(description).not.toContain("invalid-day");
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
      detail: { error: "unsafe_api_url", field: "llm_api_url" },
    }, 422, "pt");
    const invalidPayload = describeConfigSaveFailure({
      detail: { error: "invalid_credentials_payload", fields: [{ field: "tts_api_key", code: "string_type" }] },
    }, 422, "pt");

    expect(unsafeUrl).toContain("API LLM:");
    expect(unsafeUrl).toContain("URL da API do LLM");
    expect(invalidPayload).toContain("API TTS:");
    expect(invalidPayload).toContain("API Key de voz");
  });

  it("falls back to a safe human label for an unknown credential field", () => {
    const description = describeConfigSaveFailure({
      detail: { error: "invalid_credentials_payload", fields: [{ field: "future_credential_field", input: "secret" }] },
    }, 422, "pt");

    expect(description).toContain("future credential field");
    expect(description).not.toContain("invalid_credentials_payload");
    expect(description).not.toContain("secret");
  });

  it("provides a useful fallback for server failures", () => {
    expect(describeConfigSaveFailure(null, 500, "en"))
      .toBe("The server could not save the configuration. Try again in a few moments.");
  });
});

describe("Backend / frontend STT provider parity", () => {
  it("STT_PROVIDERS covers every canonical id from the backend allowlist", () => {
    const uiIds = STT_PROVIDERS.map(p => p.id).sort();
    const backendIds = [...BACKEND_STT_IDS].sort();
    expect(uiIds).toEqual(backendIds);
  });

  it("has exactly one entry per canonical id (no duplicates)", () => {
    const ids = STT_PROVIDERS.map(p => p.id);
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe("LLM provider selection", () => {
  it("declares slm_cortex only as a display-only (comingSoon) card", () => {
    const cortex = PROVIDERS.find(p => p.id === "slm_cortex");
    expect(cortex).toBeDefined();
    expect(cortex?.comingSoon).toBe(true);
  });

  it("excludes slm_cortex from the selectable providers set", () => {
    expect(SELECTABLE_LLM_PROVIDERS.has("slm_cortex")).toBe(false);
  });

  it("selectable set matches the backend operational allowlist", () => {
    const selectable = [...SELECTABLE_LLM_PROVIDERS].sort();
    const backend = [...BACKEND_LLM_OPERATIONAL].sort();
    expect(selectable).toEqual(backend);
  });
});

describe("Backend / frontend TTS provider parity", () => {
  it("offers exactly ElevenLabs and Google Cloud TTS", () => {
    expect(TTS_PROVIDERS.map(p => p.id).sort()).toEqual([...BACKEND_TTS_IDS].sort());
    expect(TTS_PROVIDERS).toHaveLength(2);
  });
});

describe("<ApiCredentialsForm /> — strict credential payload", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    localStorage.setItem("ns_auth", JSON.stringify({ token: "test-token" }));
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    localStorage.clear();
    cleanup();
    vi.restoreAllMocks();
  });

  it("scopes the clinic through the query string without leaking clinic_id into the PATCH body", async () => {
    const status = {
      whatsapp_provider: "zapi",
      zapi_instance_id: null,
      zapi_token_set: false,
      zapi_token_masked: null,
      evolution_api_url: null,
      evolution_instance_name: null,
      evolution_api_key_set: false,
      evolution_api_key_masked: null,
      llm_api_key_set: false,
      llm_api_key_masked: null,
      llm_base_url: null,
      llm_model_name: null,
      tts_provider: "elevenlabs",
      tts_api_url: "https://api.elevenlabs.io/v1",
      tts_api_key_set: true,
      tts_api_key_masked: "••••••••",
      tts_model_name: "eleven_multilingual_v2",
      apis_configured: false,
    };
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PATCH") {
        return jsonResponse({ ok: true, apis_configured: false });
      }
      return jsonResponse(status);
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    const saveButton = await screen.findByRole("button", { name: /salvar configurações de api/i });
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(true);
    });
    const patchCall = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
    expect(patchCall).toBeDefined();
    expect(String(patchCall?.[0])).toBe("/v1/config/api-credentials?clinic_id=42");
    const payload = JSON.parse(String(patchCall?.[1]?.body));
    expect(payload).not.toHaveProperty("clinic_id");
    expect(payload).not.toHaveProperty("tts_provider");
  });

  it("renders both TTS choices and submits a Google Cloud configuration", async () => {
    const status = {
      whatsapp_provider: "zapi",
      zapi_instance_id: null,
      zapi_token_set: false,
      zapi_token_masked: null,
      evolution_api_url: null,
      evolution_instance_name: null,
      evolution_api_key_set: false,
      evolution_api_key_masked: null,
      llm_api_key_set: false,
      llm_api_key_masked: null,
      llm_base_url: null,
      llm_model_name: null,
      tts_provider: null,
      tts_api_url: null,
      tts_api_key_set: false,
      tts_api_key_masked: null,
      tts_model_name: null,
      apis_configured: false,
    };
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PATCH") {
        return jsonResponse({ ok: true, apis_configured: false });
      }
      return jsonResponse(status);
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    const ttsSection = await screen.findByTestId("tts-settings-section");
    expect(within(ttsSection).getByTestId("tts-provider-elevenlabs")).toBeInTheDocument();
    expect(within(ttsSection).getByTestId("tts-provider-google")).toBeInTheDocument();
    expect(within(ttsSection).queryByTestId("tts-provider-microsoft")).not.toBeInTheDocument();

    fireEvent.click(within(ttsSection).getByTestId("tts-provider-google"));
    fireEvent.change(within(ttsSection).getByPlaceholderText("AIza..."), {
      target: { value: "synthetic-google-tts-key" },
    });
    fireEvent.click(
      within(ttsSection).getByRole("switch", {
        name: /ativar respostas em áudio|enable audio replies/i,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: /salvar/i }));

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(true);
    });
    const patchCall = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
    const payload = JSON.parse(String(patchCall?.[1]?.body));
    expect(payload).toMatchObject({
      tts_provider: "google",
      tts_api_url: "https://texttospeech.googleapis.com/v1",
      tts_api_key: "synthetic-google-tts-key",
      tts_model_name: "pt-BR-Neural2-A",
      tts_voice_id: null,
      tts_enabled: true,
    });
  });

  it("saves a brand-new clinic that never touched STT without 422-ing on empty fields", async () => {
    // A clinic that has never configured STT: the GET status carries no STT
    // fields at all, so `sttApiUrl`/`sttModelName` stay at their initial "".
    // Regression for a production 422 — `stt_api_url`/`stt_model_name` are
    // `Identifier`/`ApiUrl` with min_length=1 on the backend, so sending ""
    // for either rejected every save, including saves unrelated to STT.
    const status = {
      whatsapp_provider: "zapi",
      zapi_instance_id: null,
      zapi_token_set: false,
      zapi_token_masked: null,
      llm_api_key_set: false,
      llm_api_key_masked: null,
      llm_base_url: null,
      llm_model_name: null,
      apis_configured: false,
    };
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PATCH") {
        return jsonResponse({ ok: true, apis_configured: false });
      }
      return jsonResponse(status);
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    const saveButton = await screen.findByRole("button", { name: /salvar configurações de api/i });
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(true);
    });
    const patchCall = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
    const payload = JSON.parse(String(patchCall?.[1]?.body));
    expect(payload).not.toHaveProperty("whisper_model");
    expect(payload.stt_model_name).toBeNull();
    expect(payload.stt_api_url).toBeNull();
  });
});

describe("<ApiCredentialsForm /> — Meta signature_pending banner", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    // Provide a JSON mock so the component's fetch(...) resolves.
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    // Provide a minimal localStorage stub for the getToken() helper.
    if (!globalThis.localStorage) {
      let store: Record<string, string> = {};
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (globalThis as any).localStorage = {
        getItem: (k: string) => store[k] ?? null,
        setItem: (k: string, v: string) => { store[k] = String(v); },
        removeItem: (k: string) => { delete store[k]; },
        clear: () => { store = {}; },
      };
    }
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    cleanup();
    vi.restoreAllMocks();
  });

  it("renders the signature_pending banner when meta_app_secret_set is false and identity is configured", async () => {
    // fetchStatus returns a Meta-configured tenant WITHOUT app secret.
    (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      jsonResponse({
        zapi_instance_id: null,
        zapi_token_set: false,
        zapi_token_masked: null,
        whatsapp_provider: "meta_cloud",
        evolution_api_url: null,
        evolution_instance_name: null,
        evolution_api_key_set: false,
        evolution_api_key_masked: null,
        meta_phone_number_id: "1234567890",
        meta_waba_id: "9876543210",
        meta_permanent_token_set: true,
        meta_permanent_token_masked: "EAAG****abcd",
        meta_app_secret_set: false,
        meta_app_secret_masked: null,
        meta_verify_token: null,
        llm_api_key_set: false,
        llm_api_key_masked: null,
        llm_base_url: null,
        llm_model_name: null,
        apis_configured: false,
      }),
    );

    render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    // The banner is only shown after fetchStatus resolves.
    await waitFor(() => {
      expect(screen.getByTestId("meta-signature-pending-banner")).toBeInTheDocument();
    });
    expect(screen.getByTestId("meta-signature-pending-badge")).toBeInTheDocument();
    // Meta App Secret field is rendered (marked required indirectly via label).
    expect(screen.getByTestId("meta-app-secret-field")).toBeInTheDocument();
  });

  it("does NOT render the banner when meta_app_secret_set is true", async () => {
    (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      jsonResponse({
        zapi_instance_id: null,
        zapi_token_set: false,
        zapi_token_masked: null,
        whatsapp_provider: "meta_cloud",
        evolution_api_url: null,
        evolution_instance_name: null,
        evolution_api_key_set: false,
        evolution_api_key_masked: null,
        meta_phone_number_id: "1234567890",
        meta_waba_id: "9876543210",
        meta_permanent_token_set: true,
        meta_permanent_token_masked: "EAAG****abcd",
        meta_app_secret_set: true,
        meta_app_secret_masked: "shhh****",
        meta_verify_token: "verify_xxx",
        llm_api_key_set: false,
        llm_api_key_masked: null,
        llm_base_url: null,
        llm_model_name: null,
        apis_configured: true,
      }),
    );

    render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    // Wait for load to complete so the banner would have appeared if applicable.
    await waitFor(() => {
      expect(screen.getByTestId("meta-app-secret-field")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("meta-signature-pending-banner")).not.toBeInTheDocument();
  });
});

describe("API capability headers driven by the shared registry", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    localStorage.setItem("ns_auth", JSON.stringify({ token: "test-token" }));
    localStorage.setItem("ns_locale", "pt");
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    localStorage.clear();
    cleanup();
    vi.restoreAllMocks();
  });

  const EMPTY_STATUS = {
    whatsapp_provider: "zapi",
    zapi_instance_id: null,
    zapi_token_set: false,
    zapi_token_masked: null,
    evolution_api_url: null,
    evolution_instance_name: null,
    evolution_api_key_set: false,
    evolution_api_key_masked: null,
    llm_api_key_set: false,
    llm_api_key_masked: null,
    llm_base_url: null,
    llm_model_name: null,
    multimodal_provider: null,
    multimodal_api_url: null,
    multimodal_model_name: null,
    multimodal_api_key_set: false,
    multimodal_api_key_masked: null,
    multimodal_readiness: "incomplete",
    tts_provider: null,
    tts_api_url: null,
    tts_api_key_set: false,
    tts_api_key_masked: null,
    tts_model_name: null,
    tts_readiness: "incomplete",
    apis_configured: false,
  };

  function mockStatus(status: unknown) {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PATCH") {
        return jsonResponse({ ok: true, apis_configured: false });
      }
      return jsonResponse(status);
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    return fetchMock;
  }

  it("declares the 5 canonical capability entries in the registry", () => {
    const ids = API_CAPABILITIES.map(c => c.id).sort();
    expect(ids).toEqual(["llm", "multimodal", "stt", "tts", "whatsapp"]);
    const required = API_CAPABILITIES.filter(c => c.required).map(c => c.id).sort();
    expect(required).toEqual(["llm", "whatsapp"]);
  });

  it("pins the exact labels, descriptions, and required values in the registry", () => {
    expect(API_CAPABILITIES).toEqual([
      {
        id: "whatsapp",
        short: "API WhatsApp",
        desc: {
          pt: "Canal de mensagens (recebe e envia no WhatsApp)",
          en: "Messaging channel (receives and sends on WhatsApp)",
        },
        required: true,
      },
      {
        id: "llm",
        short: "API LLM",
        desc: {
          pt: "Large Language Model (gera as respostas em texto)",
          en: "Large Language Model (generates the text replies)",
        },
        required: true,
      },
      {
        id: "stt",
        short: "API STT",
        desc: {
          pt: "Speech to Text (transcrição de áudio)",
          en: "Speech to Text (audio transcription)",
        },
        required: false,
      },
      {
        id: "multimodal",
        short: "FallBack LLM Multimodal",
        desc: {
          pt: "Fallback multimodal para duas contingências: quando o LLM primário falhar (com prompt e histórico enviados ao provedor multimodal) e quando o STT falhar (o modelo ouve o áudio direto).",
          en: "Multimodal fallback for two contingencies: when the primary LLM fails (prompt and history are sent to the multimodal provider) and when STT fails (the model listens to the audio directly).",
        },
        required: false,
      },
      {
        id: "tts",
        short: "API TTS",
        desc: {
          pt: "Text to Speech (gerar áudio)",
          en: "Text to Speech (generate audio)",
        },
        required: false,
      },
    ]);
  });

  it("renders every short header name, description paragraph, and requirement marker in PT", async () => {
    mockStatus(EMPTY_STATUS);

    render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    // Wait until the form has finished loading (a stable landmark).
    await screen.findByRole("button", { name: /salvar configurações de api/i });

    // Short names - one per block, from the registry.
    expect(screen.getByText("API WhatsApp")).toBeInTheDocument();
    expect(screen.getByText("API LLM")).toBeInTheDocument();
    expect(screen.getByText("API STT")).toBeInTheDocument();
    expect(screen.getByText("FallBack LLM Multimodal")).toBeInTheDocument();
    expect(screen.getByText("API TTS")).toBeInTheDocument();

    // Final PT paragraphs from the acceptance table.
    expect(screen.getByText("Canal de mensagens (recebe e envia no WhatsApp)")).toBeInTheDocument();
    expect(screen.getByText("Large Language Model (gera as respostas em texto)")).toBeInTheDocument();
    expect(screen.getByText("Speech to Text (transcrição de áudio)")).toBeInTheDocument();
    expect(screen.getByText(
      "Fallback multimodal para duas contingências: quando o LLM primário falhar (com prompt e histórico enviados ao provedor multimodal) e quando o STT falhar (o modelo ouve o áudio direto).",
    )).toBeInTheDocument();
    expect(screen.getByText(
      "Text to Speech (gerar áudio). Quando ativado, as respostas do agente são sintetizadas e enviadas como áudio no WhatsApp. Se o provedor falhar, a resposta é enviada em texto.",
    )).toBeInTheDocument();

    // Required vs optional markers, keyed by capability id.
    expect(screen.getByTestId("capability-requirement-whatsapp")).toHaveTextContent("obrigatório");
    expect(screen.getByTestId("capability-requirement-llm")).toHaveTextContent("obrigatório");
    expect(screen.getByTestId("capability-requirement-stt")).toHaveTextContent("opcional");
    expect(screen.getByTestId("capability-requirement-multimodal")).toHaveTextContent("opcional");
    expect(screen.getByTestId("capability-requirement-tts")).toHaveTextContent("opcional");
  });

  it("renders the requirement marker in English when locale is en", async () => {
    localStorage.setItem("ns_locale", "en");
    mockStatus(EMPTY_STATUS);

    render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    await screen.findByRole("button", { name: /salvar configurações de api/i });

    expect(screen.getByTestId("capability-requirement-whatsapp")).toHaveTextContent("Required");
    expect(screen.getByTestId("capability-requirement-llm")).toHaveTextContent("Required");
    expect(screen.getByTestId("capability-requirement-stt")).toHaveTextContent("Optional");
    expect(screen.getByTestId("capability-requirement-multimodal")).toHaveTextContent("Optional");
    expect(screen.getByTestId("capability-requirement-tts")).toHaveTextContent("Optional");

    expect(screen.getByText("Messaging channel (receives and sends on WhatsApp)")).toBeInTheDocument();
    expect(screen.getByText("Large Language Model (generates the text replies)")).toBeInTheDocument();
    expect(screen.getByText("Speech to Text (audio transcription)")).toBeInTheDocument();
    expect(screen.getByText("FallBack LLM Multimodal")).toBeInTheDocument();
    expect(screen.getByText(
      "Multimodal fallback for two contingencies: when the primary LLM fails (prompt and history are sent to the multimodal provider) and when STT fails (the model listens to the audio directly).",
    )).toBeInTheDocument();
    expect(screen.getByText(
      "Text to Speech (generate audio). When enabled, agent replies are synthesized and sent as WhatsApp audio. If the provider fails, the reply is sent as text.",
    )).toBeInTheDocument();
  });
});
