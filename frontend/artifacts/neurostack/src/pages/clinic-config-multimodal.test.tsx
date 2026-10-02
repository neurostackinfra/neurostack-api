import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
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

  it("shows OpenRouter as the only inert multimodal card and makes no recommendation", async () => {
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
    ).toBe("ARTICLE"));
    expect(within(section).getByText("API LLM Fallback Multimodal")).toBeInTheDocument();
    expect(within(section).getByTestId("capability-requirement-multimodal"))
      .toHaveTextContent("opcional");
    expect(within(section).getAllByTestId(/provider-card-/)).toHaveLength(1);
    expect(within(section).getByText("EM BREVE"))
      .toHaveClass("text-[10px]", "text-orange-400", "bg-orange-500/10");
    expect(section.querySelector("button, input, select, textarea, [tabindex]")).toBeNull();
    expect(within(section).queryByTestId("multimodal-recommended-badge")).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(0);
  });
});
