import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";

import {
  API_PROVIDER_AVAILABILITY,
  ApiCredentialsForm,
  MULTIMODAL_PROVIDERS,
} from "./clinic-config";
import { I18nProvider } from "@/context/i18n";

function Wrapper({ children }: { children: ReactNode }) {
  return <I18nProvider>{children}</I18nProvider>;
}

describe("multimodal provider boundary", () => {
  const originalFetch = globalThis.fetch;
  const openRouterBaseUrl = "https://openrouter.ai/api/v1";

  afterEach(() => {
    globalThis.fetch = originalFetch;
    localStorage.clear();
    cleanup();
    vi.restoreAllMocks();
  });

  it("mirrors the sole backend multimodal pair", () => {
    expect(MULTIMODAL_PROVIDERS.map((provider) => provider.id))
      .toEqual(Object.keys(API_PROVIDER_AVAILABILITY.multimodal));
    expect(API_PROVIDER_AVAILABILITY.multimodal).toEqual({ openrouter: "active" });
  });

  it("opens the available OpenRouter editor with its official URL and submits the exact bundle", async () => {
    localStorage.setItem("ns_locale", "pt");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({}),
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    render(<Wrapper><ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} /></Wrapper>);

    const section = screen.getByTestId("api-capability-multimodal");
    await waitFor(() => expect(
      within(section).getByTestId("provider-card-multimodal-openrouter").tagName,
    ).toBe("BUTTON"));
    expect(within(section).getByText("API LLM Fallback Multimodal")).toBeInTheDocument();
    expect(within(section).getByTestId("capability-requirement-multimodal"))
      .toHaveTextContent("obrigatório");
    expect(within(section).getAllByTestId(/provider-card-/)).toHaveLength(1);
    expect(within(section).getByText("DISPONÍVEL"))
      .toHaveClass("text-[10px]", "border-sky-500/30", "bg-sky-500/10", "text-sky-400");
    expect(within(section).queryByTestId("multimodal-recommended-badge")).not.toBeInTheDocument();

    fireEvent.click(within(section).getByTestId("provider-card-multimodal-openrouter"));
    const editor = within(section).getByTestId("provider-editor-multimodal-openrouter");
    expect(within(editor).getByTestId("field-multimodal_api_url"))
      .toHaveValue(openRouterBaseUrl);
    fireEvent.change(within(editor).getByTestId("field-multimodal_api_key"), {
      target: { value: "fresh-openrouter-key" },
    });
    fireEvent.change(within(editor).getByTestId("field-multimodal_model_name"), {
      target: { value: "google/gemini-2.5-flash" },
    });
    fireEvent.click(within(editor).getByTestId("save-multimodal-openrouter"));

    await waitFor(() => expect(
      fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH"),
    ).toHaveLength(1));
    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
    expect(JSON.parse(String(patch?.[1]?.body))).toEqual({
      multimodal_provider: "openrouter",
      multimodal_api_url: openRouterBaseUrl,
      multimodal_api_key: "fresh-openrouter-key",
      multimodal_model_name: "google/gemini-2.5-flash",
    });
  });

  it.each([
    ["incomplete", openRouterBaseUrl],
    ["configuration_only", "https://historical-openrouter-proxy.example/v1"],
  ])(
    "remediates a held %s OpenRouter bundle with fresh canonical credentials",
    async (readiness, storedUrl) => {
      localStorage.setItem("ns_locale", "pt");
      const status = {
        multimodal_provider: "openrouter",
        multimodal_api_url: storedUrl,
        multimodal_api_key_set: true,
        multimodal_api_key_masked: "••••••••",
        multimodal_model_name: "stored-audio-model",
        multimodal_readiness: readiness,
      };
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => status,
      });
      globalThis.fetch = fetchMock as unknown as typeof fetch;

      render(
        <Wrapper>
          <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
        </Wrapper>,
      );

      const section = screen.getByTestId("api-capability-multimodal");
      const card = await within(section).findByTestId(
        "provider-card-multimodal-openrouter",
      );
      fireEvent.click(card);

      const editor = within(section).getByTestId(
        "provider-editor-multimodal-openrouter",
      );
      expect(within(editor).getByTestId("field-multimodal_api_url"))
        .toHaveValue(openRouterBaseUrl);
      expect(within(editor).getByTestId("field-multimodal_api_key"))
        .toHaveValue("");
      expect(within(editor).getByTestId("field-multimodal_model_name"))
        .toHaveValue("stored-audio-model");

      fireEvent.change(within(editor).getByTestId("field-multimodal_api_key"), {
        target: { value: "fresh-remediation-key" },
      });
      fireEvent.click(within(editor).getByTestId("save-multimodal-openrouter"));

      await waitFor(() => expect(
        fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH"),
      ).toHaveLength(1));
      const patch = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
      expect(JSON.parse(String(patch?.[1]?.body))).toEqual({
        multimodal_provider: "openrouter",
        multimodal_api_url: openRouterBaseUrl,
        multimodal_api_key: "fresh-remediation-key",
        multimodal_model_name: "stored-audio-model",
      });
    },
  );

  const descByLocale = {
    pt: "Fallback multimodal para três usos: quando o LLM primário falhar (com prompt e histórico enviados ao provedor multimodal), quando o STT falhar (o modelo ouve o áudio direto) e para entender imagens recebidas no WhatsApp.",
    en: "Multimodal fallback for three uses: when the primary LLM fails (prompt and history are sent to the multimodal provider), when STT fails (the model listens to the audio directly) and to understand images received on WhatsApp.",
  } as const;
  const helpStates = [
    ["available", {}],
    ["incomplete", { multimodal_provider: "openrouter", multimodal_readiness: "incomplete" }],
    ["configured", { multimodal_provider: "openrouter", multimodal_readiness: "active" }],
  ] as const;

  it.each((["pt", "en"] as const).flatMap((locale) =>
    helpStates.map(([state, status]) => [locale, state, status] as const),
  ))("shows no extra help text on the %s OpenRouter card when %s", async (locale, state, status) => {
    localStorage.setItem("ns_locale", locale);
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => status,
    }) as unknown as typeof fetch;

    render(<Wrapper><ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} /></Wrapper>);

    const section = screen.getByTestId("api-capability-multimodal");
    const card = within(section).getByTestId("provider-card-multimodal-openrouter");
    await waitFor(() => expect(card).toHaveAttribute("data-card-state", state));
    expect(within(card).queryByTestId("provider-help-multimodal-openrouter")).toBeNull();
    expect(within(section).getByText(descByLocale[locale])).toBeInTheDocument();
  });
});
