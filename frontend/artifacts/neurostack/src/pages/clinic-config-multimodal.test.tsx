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
    expect(API_PROVIDER_AVAILABILITY.multimodal).toEqual({ openrouter: "active" });
  });

  it("offers OpenRouter as the only multimodal card and makes no recommendation", async () => {
    localStorage.setItem("ns_locale", "pt");
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({}),
    }) as unknown as typeof fetch;

    render(<Wrapper><ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} /></Wrapper>);

    const section = screen.getByTestId("api-capability-multimodal");
    await waitFor(() => expect(
      within(section).getByTestId("provider-card-multimodal-openrouter").tagName,
    ).toBe("BUTTON"));
    expect(within(section).getAllByTestId(/provider-card-/)).toHaveLength(1);
    expect(within(section).getByText("DISPONÍVEL")).toHaveClass("text-[10px]", "text-sky-400");
    expect(within(section).queryByTestId("multimodal-recommended-badge")).not.toBeInTheDocument();
  });
});
