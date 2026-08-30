import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
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

describe("multimodal coming-soon boundary", () => {
  afterEach(() => {
    localStorage.clear();
    cleanup();
  });

  it("mirrors the sole backend multimodal pair", () => {
    expect(MULTIMODAL_PROVIDERS.map((provider) => provider.id))
      .toEqual(Object.keys(API_PROVIDER_AVAILABILITY.multimodal));
    expect(API_PROVIDER_AVAILABILITY.multimodal).toEqual({ openrouter: "coming_soon" });
  });

  it("renders OpenRouter as an inert card without a recommendation or credential fields", () => {
    localStorage.setItem("ns_locale", "pt");

    render(
      <Wrapper>
        <ApiCredentialsForm clinicId="42" isReadOnly={false} isSuperAdmin={false} />
      </Wrapper>,
    );

    const section = screen.getByTestId("api-capability-multimodal");
    expect(within(section).getAllByTestId(/provider-card-/)).toHaveLength(1);
    expect(within(section).getByText("EM BREVE")).toHaveClass("text-[10px]", "text-orange-400");
    expect(within(section).queryByTestId("multimodal-recommended-badge")).not.toBeInTheDocument();
    expect(section.querySelector("input, button, select, textarea, [tabindex]")).toBeNull();
  });
});
