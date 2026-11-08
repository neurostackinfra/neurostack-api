import type { ReactNode } from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";

const mocks = vi.hoisted(() => ({
  auth: {
    isAuthenticated: true,
    isSuperAdmin: false,
    isInvestor: false,
    isBuilder: false,
    leadSubmitted: true,
  },
}));

vi.mock("@/context/auth", () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: () => mocks.auth,
}));

vi.mock("@/components/layout", () => ({
  Layout: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

// Login imports an `@assets` image the vitest config does not alias.
vi.mock("@/pages/login", () => ({
  default: () => <div data-testid="login-page" />,
}));

vi.mock("@/pages/chat", () => ({
  default: () => <div data-testid="chat-simulator-page" />,
}));

vi.mock("@/pages/builder", () => ({
  default: () => <div data-testid="builder-panel-page" />,
}));

function setRole(role: "builder" | "super_admin" | "clinic_admin") {
  mocks.auth.isBuilder = role === "builder";
  mocks.auth.isSuperAdmin = role === "super_admin";
}

beforeEach(() => {
  window.history.replaceState({}, "", "/");
});

afterEach(() => {
  cleanup();
});

describe("<App /> chat simulator route", () => {
  it("sends a builder at / to the builder panel instead of the simulator", async () => {
    setRole("builder");

    render(<App />);

    expect(await screen.findByTestId("builder-panel-page")).toBeInTheDocument();
    await waitFor(() => expect(window.location.pathname).toBe("/builder"));
    expect(screen.queryByTestId("chat-simulator-page")).not.toBeInTheDocument();
  });

  it.each(["super_admin", "clinic_admin"] as const)(
    "renders the chat simulator at / for %s",
    async (role) => {
      setRole(role);

      render(<App />);

      expect(await screen.findByTestId("chat-simulator-page")).toBeInTheDocument();
      expect(window.location.pathname).toBe("/");
    },
  );
});
