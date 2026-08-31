import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
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

async function renderWithStatus(status: Record<string, unknown>) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: vi.fn().mockResolvedValue(status),
  } as unknown as Response);
  globalThis.fetch = fetchMock as unknown as typeof fetch;

  render(
    <Wrapper>
      <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
    </Wrapper>,
  );
  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  await act(async () => { await Promise.resolve(); });
}

describe("multimodal provider boundary", () => {
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    globalThis.fetch = originalFetch;
    localStorage.clear();
    cleanup();
    vi.restoreAllMocks();
  });

  it("mirrors the sole backend multimodal pair", () => {
    expect(MULTIMODAL_PROVIDERS.map((provider) => provider.id))
      .toEqual(Object.keys(API_PROVIDER_AVAILABILITY.multimodal));
    expect(API_PROVIDER_AVAILABILITY.multimodal).toEqual({ openrouter: "coming_soon" });
  });

  it("renders OpenRouter as an inert card when the tenant does not hold it", async () => {
    localStorage.setItem("ns_locale", "pt");
    await renderWithStatus({ multimodal_provider: null, multimodal_readiness: "incomplete" });

    const section = screen.getByTestId("api-capability-multimodal");
    expect(within(section).getAllByTestId(/provider-card-/)).toHaveLength(1);
    expect(within(section).getByText("EM BREVE")).toHaveClass("text-[10px]", "text-orange-400");
    expect(within(section).queryByTestId("multimodal-recommended-badge")).not.toBeInTheDocument();
    expect(section.querySelector("input, button, select, textarea, [tabindex]")).toBeNull();
  });

  it("lets a tenant that already holds OpenRouter keep maintaining it", async () => {
    localStorage.setItem("ns_locale", "pt");
    await renderWithStatus({
      multimodal_provider: "openrouter",
      multimodal_readiness: "configuration_only",
      multimodal_api_url: "https://openrouter.ai/api/v1",
      multimodal_model_name: "gpt-4o-mini",
      multimodal_api_key_set: true,
    });

    const section = screen.getByTestId("api-capability-multimodal");
    expect(within(section).getByText("CONFIGURADO")).toBeInTheDocument();
    expect(within(section).queryByTestId("multimodal-recommended-badge")).not.toBeInTheDocument();
    expect(within(section).getByTestId("provider-card-multimodal-openrouter").tagName)
      .toBe("BUTTON");
  });
});
