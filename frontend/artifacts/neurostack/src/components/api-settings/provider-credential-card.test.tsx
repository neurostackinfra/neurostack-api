import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { ProviderCredentialCard } from "./provider-credential-card";

describe("<ProviderCredentialCard />", () => {
  it("renders the exact 10px orange coming-soon badge", () => {
    render(
      <ProviderCredentialCard
        id="llm-openai"
        label="GPT"
        sub="by OpenAI"
        state="coming_soon"
        badgeLabel="COMING SOON"
      />,
    );

    const badge = screen.getByTestId("provider-badge-coming_soon-llm-openai");
    expect(badge).toHaveTextContent("COMING SOON");
    expect(badge).toHaveClass("text-[10px]", "text-orange-400", "bg-orange-500/10");
  });

  it("gives a coming-soon provider no focusable or selectable control", () => {
    const onToggle = vi.fn();
    render(
      <ProviderCredentialCard
        id="llm-openai"
        label="GPT"
        sub="by OpenAI"
        state="coming_soon"
        badgeLabel="EM BREVE"
        onToggle={onToggle}
      />,
    );

    const card = screen.getByTestId("provider-card-llm-openai");
    expect(card).toHaveAttribute("data-card-state", "coming_soon");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(card.querySelector("input, button, select, textarea, [tabindex]")).toBeNull();

    // Even when a handler is passed, an unavailable provider cannot be adopted
    // through this surface.
    fireEvent.click(card);
    expect(onToggle).not.toHaveBeenCalled();
  });

  it.each([
    ["configured", "CONFIGURADO", "text-emerald-400"],
    ["incomplete", "INCOMPLETO", "text-amber-400"],
    ["available", "DISPONÍVEL", "text-sky-400"],
  ] as const)(
    "renders a %s provider as a button the tenant can open",
    (state, badgeLabel, badgeClass) => {
      const onToggle = vi.fn();
      render(
        <ProviderCredentialCard
          id="llm-openai"
          label="GPT"
          sub="by OpenAI"
          state={state}
          badgeLabel={badgeLabel}
          onToggle={onToggle}
        />,
      );

      const card = screen.getByTestId("provider-card-llm-openai");
      expect(card.tagName).toBe("BUTTON");
      expect(card).toHaveAttribute("data-card-state", state);
      expect(card).toHaveAttribute("aria-expanded", "false");
      expect(screen.getByTestId(`provider-badge-${state}-llm-openai`))
        .toHaveClass(badgeClass);

      fireEvent.click(card);
      expect(onToggle).toHaveBeenCalledTimes(1);
    },
  );

  it("renders a visible non-coming provider as a disabled button without a handler", () => {
    render(
      <ProviderCredentialCard
        id="llm-openai"
        label="GPT"
        sub="by OpenAI"
        state="available"
        badgeLabel="DISPONÍVEL"
      />,
    );

    const card = screen.getByTestId("provider-card-llm-openai");
    expect(card).toBeDisabled();
    expect(card).toHaveClass("cursor-not-allowed", "opacity-70");
    expect(card).not.toHaveAttribute("aria-expanded");
  });

  it("reports the expanded state through aria-expanded", () => {
    render(
      <ProviderCredentialCard
        id="llm-openai"
        label="GPT"
        sub="by OpenAI"
        state="configured"
        badgeLabel="CONFIGURADO"
        isExpanded
        onToggle={() => undefined}
      />,
    );

    expect(screen.getByTestId("provider-card-llm-openai"))
      .toHaveAttribute("aria-expanded", "true");
  });
});
