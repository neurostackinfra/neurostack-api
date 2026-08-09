import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ProviderCredentialCard } from "./provider-credential-card";

const baseProps = {
  id: "openai",
  label: "GPT",
  sub: "by OpenAI",
  activeCls: "border-emerald-500/50 text-emerald-400 bg-emerald-500/10",
};

describe("<ProviderCredentialCard />", () => {
  it("renders in a closed state by default (children not visible)", () => {
    render(
      <ProviderCredentialCard
        {...baseProps}
        isSelected={false}
        onSelect={() => {}}
      >
        <div>Hidden form contents</div>
      </ProviderCredentialCard>,
    );
    expect(screen.getByText("GPT")).toBeInTheDocument();
    expect(screen.getByText("by OpenAI")).toBeInTheDocument();
    expect(screen.queryByText("Hidden form contents")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /gpt/i })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });

  it("expands when isSelected is true, revealing children", () => {
    render(
      <ProviderCredentialCard
        {...baseProps}
        isSelected
        onSelect={() => {}}
      >
        <div>Expanded form contents</div>
      </ProviderCredentialCard>,
    );
    expect(screen.getByText("Expanded form contents")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /gpt/i })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("invokes onSelect(id) when the card is clicked", () => {
    const onSelect = vi.fn();
    render(
      <ProviderCredentialCard
        {...baseProps}
        isSelected={false}
        onSelect={onSelect}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /gpt/i }));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("openai");
  });

  it("supports keyboard activation via Enter and Space", () => {
    const onSelect = vi.fn();
    render(
      <ProviderCredentialCard
        {...baseProps}
        isSelected={false}
        onSelect={onSelect}
      />,
    );
    const btn = screen.getByRole("button", { name: /gpt/i });
    fireEvent.keyDown(btn, { key: "Enter" });
    fireEvent.keyDown(btn, { key: " " });
    expect(onSelect).toHaveBeenCalledTimes(2);
    expect(onSelect).toHaveBeenNthCalledWith(1, "openai");
    expect(onSelect).toHaveBeenNthCalledWith(2, "openai");
  });

  it("ignores clicks and keyboard when isComingSoon is true", () => {
    const onSelect = vi.fn();
    render(
      <ProviderCredentialCard
        {...baseProps}
        isSelected={false}
        onSelect={onSelect}
        isComingSoon
      >
        <div>Should stay hidden</div>
      </ProviderCredentialCard>,
    );
    const btn = screen.getByRole("button", { name: /gpt/i });
    fireEvent.click(btn);
    fireEvent.keyDown(btn, { key: "Enter" });
    fireEvent.keyDown(btn, { key: " " });
    expect(onSelect).not.toHaveBeenCalled();
    expect(screen.queryByText("Should stay hidden")).not.toBeInTheDocument();
    // Coming-soon badge visible
    expect(screen.getByTestId("provider-badge-coming-soon-openai")).toBeInTheDocument();
  });

  it("renders the Config only badge when isConfigOnly is true", () => {
    render(
      <ProviderCredentialCard
        {...baseProps}
        isSelected={false}
        onSelect={() => {}}
        isConfigOnly
      />,
    );
    expect(screen.getByTestId("provider-badge-config-only-openai")).toBeInTheDocument();
    // Coming-soon badge NOT present when only config-only is set
    expect(screen.queryByTestId("provider-badge-coming-soon-openai")).not.toBeInTheDocument();
  });

  it("does not expand children when isComingSoon even if isSelected is true", () => {
    render(
      <ProviderCredentialCard
        {...baseProps}
        isSelected
        onSelect={() => {}}
        isComingSoon
      >
        <div>Never rendered</div>
      </ProviderCredentialCard>,
    );
    expect(screen.queryByText("Never rendered")).not.toBeInTheDocument();
  });
});
