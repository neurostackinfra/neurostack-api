import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";

import { I18nProvider } from "@/context/i18n";
import { HitlComingSoonControl } from "./clinic-config";

describe("<HitlComingSoonControl />", () => {
  afterEach(() => {
    localStorage.clear();
    cleanup();
  });

  it.each([
    ["pt", "Modo co-piloto - EM BREVE"],
    ["en", "Co-pilot mode - COMING SOON"],
  ] as const)("shows only the titles and a locked-off switch in %s", (locale, modeTitle) => {
    localStorage.setItem("ns_locale", locale);

    render(
      <I18nProvider>
        <HitlComingSoonControl />
      </I18nProvider>,
    );

    const card = screen.getByTestId("hitl-coming-soon");
    expect(within(card).getByText("Human-in-the-loop (HITL)")).toBeInTheDocument();
    expect(within(card).getByText(modeTitle)).toBeInTheDocument();
    expect(within(card).queryByText(/Recomendado|Recommended|ROADMAP|V2/)).not.toBeInTheDocument();
    expect(within(card).queryByText(/WhatsApp Web|Cooldown|Pausa Ativa|Active Pause/)).not.toBeInTheDocument();

    const toggle = within(card).getByRole("switch", { name: modeTitle });
    expect(toggle).toBeDisabled();
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(toggle).toHaveAttribute("data-state", "unchecked");
  });
});
