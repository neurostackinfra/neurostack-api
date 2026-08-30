import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, cleanup, fireEvent, within } from "@testing-library/react";
import type { ReactNode } from "react";

import {
  ApiCredentialsForm,
  MULTIMODAL_PROVIDERS,
  MULTIMODAL_PROVIDER_API_URL,
  OPERATIONAL_MULTIMODAL_PROVIDERS,
} from "./clinic-config";
import { I18nProvider } from "@/context/i18n";

// Backend canonical multimodal allowlist. Mirror of
// `PROVIDER_ALLOWLISTS["multimodal"]` in `src/services/api_credentials.py`.
const BACKEND_MULTIMODAL_IDS: readonly string[] = ["openrouter", "groq", "openai"];

// The three hosts pinned by egress policy — `MULTIMODAL_OPENROUTER_HOSTS`,
// `MULTIMODAL_GROQ_HOSTS` and `MULTIMODAL_OPENAI_HOSTS` in
// `src/services/safe_egress.py`. Any derived URL outside this set would be
// accepted by PATCH and then refused at runtime as `unsafe_api_url`.
const PINNED_MULTIMODAL_HOSTS: readonly string[] = [
  "api.groq.com",
  "api.openai.com",
  "openrouter.ai",
];

function Wrapper({ children }: { children: ReactNode }) {
  return <I18nProvider>{children}</I18nProvider>;
}

interface FetchResponse {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
}

function jsonResponse(body: unknown, ok = true, status = 200): FetchResponse {
  return { ok, status, json: async () => body };
}

// The card's key input is the only password field inside the section; the
// shared `ApiInput` renders its label unassociated, so query by type.
function multimodalKeyInput(section: HTMLElement): HTMLInputElement {
  const input = section.querySelector('input[type="password"]');
  if (!input) throw new Error("multimodal API key field not found");
  return input as HTMLInputElement;
}

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
  apis_configured: false,
};

// An already-saved OpenRouter bundle as `credential_status` returns it: the
// key is never echoed back in the clear, only as a constant-length mask.
const SAVED_OPENROUTER_STATUS = {
  ...EMPTY_STATUS,
  multimodal_provider: "openrouter",
  multimodal_api_url: "https://openrouter.ai/api/v1",
  multimodal_model_name: "google/gemini-2.5-flash",
  multimodal_api_key_set: true,
  multimodal_api_key_masked: "••••••••",
  multimodal_readiness: "active",
};

// The same bundle on a non-default provider, so an assertion on the rendered
// URL can only pass if the status fetch actually resolved.
const SAVED_GROQ_STATUS = {
  ...SAVED_OPENROUTER_STATUS,
  multimodal_provider: "groq",
  multimodal_api_url: "https://api.groq.com/openai/v1",
  multimodal_model_name: "whisper-large-v3",
};

const TEXT_FALLBACK_ENABLED_STATUS = {
  ...SAVED_OPENROUTER_STATUS,
  multimodal_text_fallback_enabled: true,
};

describe("Backend / frontend multimodal provider parity", () => {
  it("MULTIMODAL_PROVIDERS covers every canonical id from the backend allowlist", () => {
    expect(MULTIMODAL_PROVIDERS.map(p => p.id).sort())
      .toEqual([...BACKEND_MULTIMODAL_IDS].sort());
  });

  it("marks only OpenRouter as operational, mirroring the backend set", () => {
    expect([...OPERATIONAL_MULTIMODAL_PROVIDERS]).toEqual(["openrouter"]);
  });

  it("derives every API URL from a host pinned by the egress policy", () => {
    const hosts = Object.values(MULTIMODAL_PROVIDER_API_URL)
      .map(url => new URL(url).hostname)
      .sort();
    expect(hosts).toEqual([...PINNED_MULTIMODAL_HOSTS].sort());
    for (const url of Object.values(MULTIMODAL_PROVIDER_API_URL)) {
      expect(url.startsWith("https://")).toBe(true);
    }
  });
});

describe("<ApiCredentialsForm /> — multimodal card", () => {
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

  function mockCredentials(statuses: unknown[]) {
    let reads = 0;
    const patches: Record<string, unknown>[] = [];
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PATCH") {
        patches.push(JSON.parse(String(init.body)) as Record<string, unknown>);
        return jsonResponse({ ok: true, apis_configured: false });
      }
      const status = statuses[Math.min(reads, statuses.length - 1)];
      reads += 1;
      return jsonResponse(status);
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    return { fetchMock, patches };
  }

  it("renders the recommended badge in Portuguese", async () => {
    localStorage.setItem("ns_locale", "pt");
    mockCredentials([EMPTY_STATUS]);

    render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    const section = await screen.findByTestId("multimodal-settings-section");
    expect(within(section).getByTestId("multimodal-recommended-badge"))
      .toHaveTextContent("RECOMENDÁVEL");
  });

  it("renders the recommended badge in English", async () => {
    localStorage.setItem("ns_locale", "en");
    mockCredentials([EMPTY_STATUS]);

    render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    const section = await screen.findByTestId("multimodal-settings-section");
    expect(within(section).getByTestId("multimodal-recommended-badge"))
      .toHaveTextContent("RECOMMENDED");
  });

  it("uses the exact title and describes both text and audio contingencies in PT and EN", async () => {
    localStorage.setItem("ns_locale", "pt");
    mockCredentials([EMPTY_STATUS]);

    const { unmount } = render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    const portuguese = await screen.findByTestId("multimodal-settings-section");
    expect(within(portuguese).getByText("FallBack LLM Multimodal")).toBeInTheDocument();
    expect(within(portuguese).getByText(
      "Fallback multimodal para duas contingências: quando o LLM primário falhar (com prompt e histórico enviados ao provedor multimodal) e quando o STT falhar (o modelo ouve o áudio direto).",
    )).toBeInTheDocument();
    expect(within(portuguese).getByRole("switch", {
      name: "Autorizar fallback textual multimodal",
    })).toBeInTheDocument();

    unmount();
    localStorage.setItem("ns_locale", "en");
    mockCredentials([EMPTY_STATUS]);
    render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    const english = await screen.findByTestId("multimodal-settings-section");
    expect(within(english).getByText("FallBack LLM Multimodal")).toBeInTheDocument();
    expect(within(english).getByText(
      "Multimodal fallback for two contingencies: when the primary LLM fails (prompt and history are sent to the multimodal provider) and when STT fails (the model listens to the audio directly).",
    )).toBeInTheDocument();
    expect(within(english).getByRole("switch", {
      name: "Authorize multimodal text fallback",
    })).toBeInTheDocument();
  });

  it("defaults a legacy status to unchecked and never sends the opt-in while saving other multimodal edits", async () => {
    localStorage.setItem("ns_locale", "pt");
    const { patches } = mockCredentials([EMPTY_STATUS]);

    render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    const section = await screen.findByTestId("multimodal-settings-section");
    const toggle = within(section).getByRole("switch", { name: /autorizar fallback textual multimodal/i });
    await waitFor(() => expect(toggle).toBeEnabled());
    expect(toggle).not.toBeChecked();

    fireEvent.change(within(section).getByPlaceholderText("google/gemini-2.5-flash"), {
      target: { value: "replacement-model" },
    });
    fireEvent.click(screen.getByRole("button", { name: /salvar configurações de api/i }));

    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]).not.toHaveProperty("multimodal_text_fallback_enabled");
    expect(toggle).not.toBeChecked();
  });

  it("exposes an accessible text opt-in that PATCHes only the explicit boolean and reloads persisted state", async () => {
    localStorage.setItem("ns_locale", "pt");
    const { patches } = mockCredentials([
      EMPTY_STATUS,
      TEXT_FALLBACK_ENABLED_STATUS,
      TEXT_FALLBACK_ENABLED_STATUS,
    ]);

    const { unmount } = render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    const section = await screen.findByTestId("multimodal-settings-section");
    const toggle = within(section).getByRole("switch", { name: /autorizar fallback textual multimodal/i });
    await waitFor(() => expect(toggle).toBeEnabled());
    expect(toggle).toHaveAttribute("aria-checked", "false");

    fireEvent.click(toggle);

    await waitFor(() => expect(patches).toEqual([
      { multimodal_text_fallback_enabled: true },
    ]));
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "true"));

    unmount();
    render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    const reloaded = await screen.findByTestId("multimodal-settings-section");
    expect(within(reloaded).getByRole("switch", {
      name: /autorizar fallback textual multimodal/i,
    })).toHaveAttribute("aria-checked", "true");
  });

  it("shows the persisted opt-in but disables it and credentials for read-only users", async () => {
    localStorage.setItem("ns_locale", "pt");
    const { patches } = mockCredentials([TEXT_FALLBACK_ENABLED_STATUS]);

    render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly isSuperAdmin={false} />
      </Wrapper>,
    );

    const section = await screen.findByTestId("multimodal-settings-section");
    const toggle = within(section).getByRole("switch", { name: /autorizar fallback textual multimodal/i });
    expect(toggle).toBeChecked();
    expect(toggle).toBeDisabled();
    expect(multimodalKeyInput(section)).toBeDisabled();
    expect(within(section).getByTestId("multimodal-provider-openrouter")).toBeDisabled();

    fireEvent.click(toggle);
    expect(patches).toHaveLength(0);
  });

  it("offers every allowlisted provider and flags the non-operational ones", async () => {
    mockCredentials([EMPTY_STATUS]);

    render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    const list = await screen.findByTestId("multimodal-provider-list");
    for (const id of BACKEND_MULTIMODAL_IDS) {
      expect(within(list).getByTestId(`multimodal-provider-${id}`)).toBeInTheDocument();
    }
    expect(within(list).queryByTestId("multimodal-config-only-openrouter")).not.toBeInTheDocument();
    expect(within(list).getByTestId("multimodal-config-only-groq")).toBeInTheDocument();
    expect(within(list).getByTestId("multimodal-config-only-openai")).toBeInTheDocument();
  });

  it("keeps the API URL read-only and derived from the selected provider", async () => {
    mockCredentials([EMPTY_STATUS]);

    render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    const urlField = await screen.findByTestId("multimodal-api-url") as HTMLInputElement;
    expect(urlField.readOnly).toBe(true);
    expect(urlField.value).toBe(MULTIMODAL_PROVIDER_API_URL.openrouter);

    fireEvent.click(screen.getByTestId("multimodal-provider-groq"));
    expect(urlField.value).toBe(MULTIMODAL_PROVIDER_API_URL.groq);

    // A typed value cannot reach the field: it is read-only, so any attempt
    // leaves the derived URL untouched and no off-policy host can be submitted.
    fireEvent.change(urlField, { target: { value: "https://proxy.example/v1" } });
    expect(urlField.value).toBe(MULTIMODAL_PROVIDER_API_URL.groq);
  });

  it("persists the multimodal bundle and shows the key masked after a refetch", async () => {
    // The saved bundle deliberately uses Groq, not the default OpenRouter: the
    // card falls back to OpenRouter before any fetch resolves, so asserting the
    // OpenRouter URL after the remount would pass even if the refetch never
    // happened. A non-default provider makes the URL check load-bearing.
    const { patches } = mockCredentials([EMPTY_STATUS, SAVED_GROQ_STATUS]);

    const { unmount } = render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    const section = await screen.findByTestId("multimodal-settings-section");
    fireEvent.click(within(section).getByTestId("multimodal-provider-groq"));
    fireEvent.change(multimodalKeyInput(section), {
      target: { value: "fake-groq-multimodal-key" },
    });
    fireEvent.change(within(section).getByPlaceholderText("whisper-large-v3"), {
      target: { value: "whisper-large-v3" },
    });
    fireEvent.click(screen.getByRole("button", { name: /salvar configurações de api/i }));

    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]).toMatchObject({
      multimodal_provider: "groq",
      multimodal_api_url: "https://api.groq.com/openai/v1",
      multimodal_api_key: "fake-groq-multimodal-key",
      multimodal_model_name: "whisper-large-v3",
    });

    // Remount: the refetched status must repopulate the card with the mask,
    // never with the key material.
    unmount();
    render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    const reloaded = await screen.findByTestId("multimodal-settings-section");
    await waitFor(() => {
      expect((within(reloaded).getByTestId("multimodal-api-url") as HTMLInputElement).value)
        .toBe(MULTIMODAL_PROVIDER_API_URL.groq);
    });
    expect(multimodalKeyInput(reloaded).value).toBe("••••••••");
    expect(reloaded.innerHTML).not.toContain("fake-groq-multimodal-key");
  });

  it("clears the model name when the provider changes and the field is left blank", async () => {
    // Regression: switching provider resets the model field to "". Omitting the
    // key from the PATCH body let the previous provider's model survive the
    // backend merge, yielding an `active` bundle pointing at a model the new
    // provider does not serve. The body must carry an explicit null instead —
    // never "", which the backend rejects (`Identifier` is min_length=1).
    const { patches } = mockCredentials([SAVED_OPENROUTER_STATUS]);

    render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    const section = await screen.findByTestId("multimodal-settings-section");
    // Provider A's model is loaded from the saved bundle.
    expect((within(section).getByPlaceholderText("google/gemini-2.5-flash") as HTMLInputElement).value)
      .toBe("google/gemini-2.5-flash");

    // Switch to provider B and supply a fresh key, leaving the model blank.
    fireEvent.click(within(section).getByTestId("multimodal-provider-groq"));
    expect((within(section).getByPlaceholderText("whisper-large-v3") as HTMLInputElement).value).toBe("");
    fireEvent.change(multimodalKeyInput(section), {
      target: { value: "fake-groq-multimodal-key" },
    });
    fireEvent.click(screen.getByRole("button", { name: /salvar configurações de api/i }));

    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0].multimodal_provider).toBe("groq");
    expect(patches[0]).toHaveProperty("multimodal_model_name");
    expect(patches[0].multimodal_model_name).toBeNull();
    expect(patches[0].multimodal_model_name).not.toBe("google/gemini-2.5-flash");
    expect(patches[0].multimodal_model_name).not.toBe("");
  });

  it("does not submit an implicit multimodal bundle when the card is untouched", async () => {
    const { patches } = mockCredentials([EMPTY_STATUS]);

    render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    fireEvent.click(await screen.findByRole("button", { name: /salvar configurações de api/i }));

    await waitFor(() => expect(patches).toHaveLength(1));
    expect(Object.keys(patches[0]).some(key => key.startsWith("multimodal_"))).toBe(false);
  });

  it("keeps an existing masked key without requiring or resending it on an unrelated save", async () => {
    const { patches } = mockCredentials([SAVED_OPENROUTER_STATUS]);

    render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    const section = await screen.findByTestId("multimodal-settings-section");
    await waitFor(() => expect(multimodalKeyInput(section).value).toBe("••••••••"));
    fireEvent.click(screen.getByRole("button", { name: /salvar configurações de api/i }));

    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]).not.toHaveProperty("multimodal_api_key");
    expect(patches[0]).not.toHaveProperty("multimodal_text_fallback_enabled");
  });

  it("asks for a new key when the provider changes instead of saving a stale one", async () => {
    localStorage.setItem("ns_locale", "pt");
    const { patches } = mockCredentials([SAVED_OPENROUTER_STATUS]);

    render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    const section = await screen.findByTestId("multimodal-settings-section");
    fireEvent.click(within(section).getByTestId("multimodal-provider-groq"));

    // The stored key is cleared, and the card says why before any save runs.
    expect(multimodalKeyInput(section).value).toBe("");
    expect(within(section).getByTestId("multimodal-fresh-key-hint")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /salvar configurações de api/i }));

    await waitFor(() => {
      expect(screen.getByText(/API Key do novo provedor multimodal/i)).toBeInTheDocument();
    });
    expect(patches).toHaveLength(0);
    expect(screen.queryByText(/fresh_api_key_required/i)).not.toBeInTheDocument();
  });

  it("translates a backend fresh_api_key_required refusal instead of echoing the code", async () => {
    localStorage.setItem("ns_locale", "pt");
    globalThis.fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PATCH") {
        return jsonResponse(
          { detail: { error: "fresh_api_key_required", capability: "multimodal", reason: "provider_changed" } },
          false,
          422,
        );
      }
      return jsonResponse(SAVED_OPENROUTER_STATUS);
    }) as unknown as typeof fetch;

    render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    const section = await screen.findByTestId("multimodal-settings-section");
    fireEvent.change(multimodalKeyInput(section), {
      target: { value: "synthetic-replacement-key" },
    });
    fireEvent.click(screen.getByRole("button", { name: /salvar configurações de api/i }));

    await waitFor(() => {
      expect(screen.getByText(/Informe uma nova API Key do provedor multimodal/i)).toBeInTheDocument();
    });
    expect(screen.queryByText(/fresh_api_key_required/i)).not.toBeInTheDocument();
  });
});
