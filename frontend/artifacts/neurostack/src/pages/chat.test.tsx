import type { ReactNode } from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { I18nProvider } from "@/context/i18n";
import Chat from "./chat";

const mocks = vi.hoisted(() => ({
  auth: {
    isAuthenticated: true,
    email: "investor@example.com",
    clinicId: null,
    tenantId: null,
    userId: null,
    builderId: null,
    role: "investor",
    isSuperAdmin: false,
    isInvestor: true,
    isBuilder: false,
    isClinicAdmin: false,
    isReadOnly: false,
    leadSubmitted: true,
    viewingClinicId: "",
    setViewingClinicId: vi.fn(),
    setLeadSubmitted: vi.fn(),
    login: vi.fn(),
    loginWithToken: vi.fn(),
    logout: vi.fn(),
  },
  toast: vi.fn(),
}));

vi.mock("@/context/auth", () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: () => mocks.auth,
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mocks.toast }),
}));

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("ns_locale", "pt");
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  mocks.toast.mockReset();
});

function renderInvestorChat() {
  return render(
    <I18nProvider>
      <Chat />
    </I18nProvider>,
  );
}

describe("<Chat /> investor demo", () => {
  it("renders the pedido_preco bucket label (not the raw legacy 'Price Inquiry' string) on the live trace intent badge", () => {
    renderInvestorChat();

    const badge = screen.getByTestId("badge-intent-pedido_preco");
    expect(badge).toHaveTextContent("Pedido de Preço");
    expect(badge.textContent).not.toContain("Price Inquiry");
    expect(badge.textContent).not.toContain("pedido_preco");
    // Bucket-driven color: pedido_preco bucket uses bg-blue-500
    expect(badge).toHaveClass("bg-blue-500", "text-white");
  });
});
