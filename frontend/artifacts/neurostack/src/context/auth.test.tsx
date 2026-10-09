import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { AuthProvider, useAuth } from "./auth";

function token(payload: Record<string, unknown>): string {
  return `eyJhbGciOiJub25lIn0.${btoa(JSON.stringify({ exp: 9999999999, ...payload }))}.synthetic`;
}

function mount(role?: string) {
  const auth: Record<string, unknown> = {
    token: token({ sub: "user-1", tenant_id: "tenant-1", role }),
    email: "user@example.test",
    clinicId: "tenant-1",
    tenantId: "tenant-1",
    userId: "user-1",
    builderId: null,
    viewingClinicId: "tenant-1",
    leadSubmitted: false,
  };
  if (role !== undefined) auth.role = role;
  localStorage.setItem("ns_auth", JSON.stringify(auth));

  function Probe() {
    const value = useAuth();
    return (
      <output data-testid="auth-flags">
        {JSON.stringify({
          role: value.role,
          clinic: value.isClinicAdmin,
          builder: value.isBuilder,
          superAdmin: value.isSuperAdmin,
          investor: value.isInvestor,
        })}
      </output>
    );
  }

  render(<AuthProvider><Probe /></AuthProvider>);
  return JSON.parse(screen.getByTestId("auth-flags").textContent ?? "{}") as Record<string, unknown>;
}

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("AuthProvider role classification", () => {
  it("fails closed when persisted authentication has no role", () => {
    expect(mount()).toEqual({
      role: "",
      clinic: false,
      builder: false,
      superAdmin: false,
      investor: false,
    });
  });

  it("fails closed for an unknown role", () => {
    expect(mount("future_role")).toEqual({
      role: "future_role",
      clinic: false,
      builder: false,
      superAdmin: false,
      investor: false,
    });
  });

  it.each([
    ["clinic_admin", "clinic"],
    ["builder", "builder"],
    ["super_admin", "superAdmin"],
    ["investor", "investor"],
  ] as const)("preserves exact classification for %s", (role, activeFlag) => {
    const flags = mount(role);

    expect(flags.role).toBe(role);
    for (const flag of ["clinic", "builder", "superAdmin", "investor"] as const) {
      expect(flags[flag]).toBe(flag === activeFlag);
    }
  });
});
