import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { ProviderCredentialCard } from "./provider-credential-card";

describe("<ProviderCredentialCard />", () => {
  it("renders the exact 10px orange coming-soon badge", () => {
    render(
      <ProviderCredentialCard
        id="llm-openai"
        label="GPT"
        sub="by OpenAI"
        comingSoonLabel="COMING SOON"
      />,
    );

    const badge = screen.getByTestId("provider-badge-coming-soon-llm-openai");
    expect(badge).toHaveTextContent("COMING SOON");
    expect(badge).toHaveClass("text-[10px]", "text-orange-400", "bg-orange-500/10");
  });

  it("has no focusable or selectable control", () => {
    render(
      <ProviderCredentialCard
        id="llm-openai"
        label="GPT"
        sub="by OpenAI"
        comingSoonLabel="EM BREVE"
      />,
    );

    const card = screen.getByTestId("provider-card-llm-openai");
    expect(card).toHaveAttribute("data-coming-soon", "true");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(card.querySelector("input, button, select, textarea, [tabindex]")).toBeNull();

    fireEvent.click(card);
    expect(screen.getByTestId("provider-badge-coming-soon-llm-openai"))
      .toHaveTextContent("EM BREVE");
  });
});
