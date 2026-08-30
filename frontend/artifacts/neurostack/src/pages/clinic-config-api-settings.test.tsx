import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";

import {
  API_CAPABILITIES,
  API_PROVIDER_AVAILABILITY,
  ApiCredentialsForm,
  PROVIDERS,
  STT_PROVIDERS,
  TTS_PROVIDERS,
  WA_PROVIDERS,
} from "./clinic-config";
import { I18nProvider } from "@/context/i18n";

function Wrapper({ children }: { children: ReactNode }) {
  return <I18nProvider>{children}</I18nProvider>;
}

const BACKEND_PROVIDER_AVAILABILITY = {
  whatsapp: { meta_cloud: "coming_soon", zapi: "coming_soon", uazapi: "coming_soon", evolution: "coming_soon", dialog360: "coming_soon", twilio: "coming_soon" },
  llm: { dropout: "coming_soon", openai: "coming_soon", outros: "coming_soon", slm_cortex: "coming_soon" },
  multimodal: { openrouter: "coming_soon" },
  stt: { outros: "coming_soon", openai_whisper: "coming_soon", elevenlabs_scribe: "coming_soon", google_stt: "coming_soon", deepgram_stt: "coming_soon", microsoft_stt: "coming_soon" },
  tts: { elevenlabs: "coming_soon", google: "coming_soon" },
} as const;

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

describe("<ApiCredentialsForm /> all-coming-soon boundary", () => {
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    globalThis.fetch = originalFetch;
    localStorage.clear();
    cleanup();
    vi.restoreAllMocks();
  });

  it("keeps the multimodal fallback copy while making the provider inert", () => {
    expect(API_CAPABILITIES.find(({ id }) => id === "multimodal")).toMatchObject({
      short: "FallBack LLM Multimodal",
      desc: {
        pt: expect.stringContaining("LLM primário falhar"),
        en: expect.stringContaining("primary LLM fails"),
      },
    });
  });

  it("renders all 19 Portuguese cards as inert EM BREVE cards", () => {
    localStorage.setItem("ns_locale", "pt");
    render(<Wrapper><ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} /></Wrapper>);

    const section = screen.getByTestId("api-settings-section");
    expect(within(section).getAllByTestId(/provider-card-/)).toHaveLength(19);
    expect(within(section).getAllByText("EM BREVE")).toHaveLength(19);
    expect(section.querySelectorAll("input, button, select, textarea, [tabindex]")).toHaveLength(0);
    expect(within(section).queryByRole("button")).not.toBeInTheDocument();
  });

  it("uses the English COMING SOON badge for every card", () => {
    localStorage.setItem("ns_locale", "en");
    render(<Wrapper><ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} /></Wrapper>);
    expect(screen.getAllByText("COMING SOON")).toHaveLength(19);
  });

  it("does not read, normalize, or submit legacy credentials", () => {
    const fetchMock = vi.fn();
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    render(<Wrapper><ApiCredentialsForm clinicId="legacy-clinic" isReadOnly={false} isSuperAdmin /></Wrapper>);
    expect(screen.getByTestId("api-settings-legacy-state-note")).toHaveTextContent("configurações de API existentes são preservadas");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
