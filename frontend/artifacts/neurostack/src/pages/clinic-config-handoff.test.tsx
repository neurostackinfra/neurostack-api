import type { ReactNode } from "react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

import { AuthProvider } from "@/context/auth";
import * as authContext from "@/context/auth";
import { I18nProvider } from "@/context/i18n";
import ClinicConfig, { HitlComingSoonControl } from "./clinic-config";

const mocks = vi.hoisted(() => ({ toast: vi.fn() }));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mocks.toast }),
}));

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

function I18nWrapper({ children }: { children: ReactNode }) {
  return <I18nProvider>{children}</I18nProvider>;
}

function PageWrapper({ children }: { children: ReactNode }) {
  return (
    <AuthProvider>
      <I18nProvider>{children}</I18nProvider>
    </AuthProvider>
  );
}

function response(body: unknown, ok = true, status = ok ? 200 : 422): Response {
  return {
    ok,
    status,
    json: vi.fn().mockResolvedValue(body),
  } as unknown as Response;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

type EditableRole = "builder" | "super_admin" | "clinic_admin";

function mountAs(
  role: EditableRole | "investor",
  locale: "pt" | "en" = "pt",
  viewingClinicId = "selected-tenant",
) {
  const payload = btoa(
    JSON.stringify({
      sub: `${role}-user`,
      clinic_id: "home-clinic",
      tenant_id: "home-clinic",
      role,
      exp: 9999999999,
    }),
  );
  localStorage.setItem("ns_locale", locale);
  localStorage.setItem(
    "ns_auth",
    JSON.stringify({
      token:
        role === "investor" ? null : `eyJhbGciOiJub25lIn0.${payload}.synthetic`,
      email: `${role}@example.test`,
      clinicId: "home-clinic",
      tenantId: "home-clinic",
      userId: `${role}-user`,
      builderId: role === "builder" ? "builder-user" : null,
      role,
      viewingClinicId,
      leadSubmitted: true,
    }),
  );
  window.history.replaceState({}, "", "/config?tab=handoff");
}

const BASE_CONFIG = {
  clinic_name: "Clinic",
  clinic_legal_name: "Clinic Legal Name",
  clinic_cnpj: "12.345.678/0001-90",
  dpo_name: "DPO Name",
  dpo_contact: "dpo@example.test",
  builder_lgpd_consent: true,
  services: [],
  handoff_active: "yes",
  standby_mode: "active",
  handoff_period: "permanent",
  handoff_cycle_hours: null,
  handoff_cycle_days: null,
};

async function renderEditable(
  overrides: Record<string, unknown> = {},
  options: {
    role?: EditableRole;
    locale?: "pt" | "en";
    viewingClinicId?: string;
  } = {},
) {
  const config = { ...BASE_CONFIG, ...overrides };
  const role = options.role ?? "builder";
  mountAs(
    role,
    options.locale ?? "pt",
    options.viewingClinicId ?? "selected-tenant",
  );
  const fetchMock = vi
    .fn()
    .mockImplementation(async (_url: RequestInfo | URL, init?: RequestInit) =>
      response(init?.method === "PATCH" ? config : config),
    );
  globalThis.fetch = fetchMock as unknown as typeof fetch;
  render(
    <PageWrapper>
      <ClinicConfig />
    </PageWrapper>,
  );
  await screen.findByTestId("handoff-standby-settings");
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  return fetchMock;
}

function standbySection() {
  return screen.getByTestId("handoff-standby-settings");
}

function modeSelect(locale: "pt" | "en" = "pt") {
  return within(standbySection()).getByRole("combobox", {
    name:
      locale === "pt"
        ? "Quando reativar a IA?"
        : "When should the AI be reactivated?",
  });
}

function unitSelect(locale: "pt" | "en" = "pt") {
  return within(standbySection()).getByRole("combobox", {
    name: locale === "pt" ? "Unidade do prazo" : "Period unit",
  });
}

function selectOption(trigger: HTMLElement, label: string) {
  fireEvent.click(trigger);
  fireEvent.click(screen.getByRole("option", { name: label }));
}

function patchBodies(fetchMock: ReturnType<typeof vi.fn>) {
  return fetchMock.mock.calls
    .filter(([, init]) => init?.method === "PATCH")
    .map(
      ([, init]) => JSON.parse(String(init?.body)) as Record<string, unknown>,
    );
}

function handoffPayload(body: Record<string, unknown>) {
  return Object.fromEntries(
    Object.entries(body).filter(([key]) =>
      [
        "handoff_active",
        "standby_mode",
        "handoff_period",
        "handoff_cycle_hours",
        "handoff_cycle_days",
      ].includes(key),
    ),
  );
}

describe("<HitlComingSoonControl />", () => {
  afterEach(() => {
    localStorage.clear();
    cleanup();
  });

  it.each([
    ["pt", "Modo co-piloto - EM BREVE"],
    ["en", "Co-pilot mode - COMING SOON"],
  ] as const)(
    "shows only the titles and a locked-off switch in %s",
    (locale, modeTitle) => {
      localStorage.setItem("ns_locale", locale);

      render(
        <I18nWrapper>
          <HitlComingSoonControl />
        </I18nWrapper>,
      );

      const card = screen.getByTestId("hitl-coming-soon");
      expect(
        within(card).getByText("Human-in-the-loop (HITL)"),
      ).toBeInTheDocument();
      expect(within(card).getByText(modeTitle)).toBeInTheDocument();
      expect(
        within(card).queryByText(/Recomendado|Recommended|ROADMAP|V2/),
      ).not.toBeInTheDocument();
      expect(
        within(card).queryByText(
          /WhatsApp Web|Cooldown|Pausa Ativa|Active Pause/,
        ),
      ).not.toBeInTheDocument();

      const toggle = within(card).getByRole("switch", { name: modeTitle });
      expect(toggle).toBeDisabled();
      expect(toggle).toHaveAttribute("aria-checked", "false");
      expect(toggle).toHaveAttribute("data-state", "unchecked");
    },
  );
});

describe("<ClinicConfig /> handoff stand-by", () => {
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    globalThis.fetch = originalFetch;
    localStorage.clear();
    window.history.replaceState({}, "", "/");
    cleanup();
    vi.restoreAllMocks();
    mocks.toast.mockReset();
  });

  it("round-trips a canonical custom duration through save and remount", async () => {
    mountAs("builder", "pt", "tenant-round-trip");
    let storedConfig: Record<string, unknown> = { ...BASE_CONFIG };
    const fetchMock = vi
      .fn()
      .mockImplementation(
        async (_url: RequestInfo | URL, init?: RequestInit) => {
          if (init?.method === "PATCH") {
            storedConfig = {
              ...storedConfig,
              ...(JSON.parse(String(init.body)) as Record<string, unknown>),
            };
          }
          return response(storedConfig);
        },
      );
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    render(
      <PageWrapper>
        <ClinicConfig />
      </PageWrapper>,
    );
    await screen.findByTestId("handoff-standby-settings");

    selectOption(modeSelect(), "Após um prazo personalizado");
    fireEvent.change(within(standbySection()).getByRole("spinbutton"), {
      target: { value: "14" },
    });
    selectOption(unitSelect(), "Dias");
    fireEvent.click(screen.getByTestId("btn-save-config"));

    await waitFor(() => expect(patchBodies(fetchMock)).toHaveLength(1));
    expect(handoffPayload(patchBodies(fetchMock)[0])).toEqual({
      handoff_active: "yes",
      standby_mode: "active",
      handoff_period: "cycle",
      handoff_cycle_hours: null,
      handoff_cycle_days: 14,
    });
    cleanup();

    render(
      <PageWrapper>
        <ClinicConfig />
      </PageWrapper>,
    );
    await screen.findByTestId("handoff-standby-settings");
    expect(modeSelect()).toHaveTextContent("Após um prazo personalizado");
    expect(within(standbySection()).getByRole("spinbutton")).toHaveValue(14);
    expect(unitSelect()).toHaveTextContent("Dias");
  });

  it("preserves edits made while an older PATCH response is pending", async () => {
    mountAs("builder", "pt", "tenant-pending");
    const pendingPatch = deferred<Response>();
    const hydratedConfig = {
      ...BASE_CONFIG,
      handoff_period: "cycle",
      handoff_cycle_hours: 4,
    };
    const fetchMock = vi
      .fn()
      .mockImplementation(
        async (_url: RequestInfo | URL, init?: RequestInit) =>
          init?.method === "PATCH"
            ? pendingPatch.promise
            : response(hydratedConfig),
      );
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    render(
      <PageWrapper>
        <ClinicConfig />
      </PageWrapper>,
    );
    await screen.findByTestId("handoff-standby-settings");

    fireEvent.click(screen.getByTestId("btn-save-config"));
    await waitFor(() => expect(patchBodies(fetchMock)).toHaveLength(1));
    fireEvent.change(within(standbySection()).getByRole("spinbutton"), {
      target: { value: "9" },
    });
    selectOption(unitSelect(), "Dias");

    await act(async () => {
      pendingPatch.resolve(response(hydratedConfig));
      await Promise.resolve();
    });
    await waitFor(() =>
      expect(screen.getByTestId("btn-save-config")).toBeEnabled(),
    );
    expect(within(standbySection()).getByRole("spinbutton")).toHaveValue(9);
    expect(unitSelect()).toHaveTextContent("Dias");
  });

  it.each([
    [
      "pt",
      "Revise os seguintes campos: configuração de stand-by após handoff, horas do prazo de handoff.",
    ],
    [
      "en",
      "Review the following fields: post-handoff stand-by settings, handoff period hours.",
    ],
  ] as const)(
    "maps sanitized handoff policy violations to actionable %s copy",
    async (locale, expectedDescription) => {
      const fetchMock = await renderEditable({}, { locale });
      const secret = "raw-secret-policy-value";
      fetchMock.mockImplementation(
        async (_url: RequestInfo | URL, init?: RequestInit) =>
          init?.method === "PATCH"
            ? response(
                {
                  detail: {
                    error: "invalid_handoff_policy",
                    violations: [
                      {
                        field: "handoff_policy",
                        code: "invalid_combination",
                        message: secret,
                      },
                      {
                        field: "handoff_cycle_hours",
                        code: "int_type",
                        input: secret,
                      },
                    ],
                    raw: secret,
                  },
                },
                false,
                422,
              )
            : response(BASE_CONFIG),
      );

      fireEvent.click(screen.getByTestId("btn-save-config"));
      await waitFor(() =>
        expect(mocks.toast).toHaveBeenCalledWith(
          expect.objectContaining({
            description: expectedDescription,
            variant: "destructive",
          }),
        ),
      );
      const renderedDescriptions = mocks.toast.mock.calls.map(([value]) =>
        String(value.description),
      );
      expect(renderedDescriptions.join(" ")).not.toContain(secret);
    },
  );

  it("renders the exact Portuguese OFF state without any reactivation controls", async () => {
    await renderEditable({
      standby_mode: "inactive",
      handoff_period: "cycle",
      handoff_cycle_hours: 12,
      handoff_cycle_days: 3,
      handoff_inactive_default_hours: 99,
    });

    const section = standbySection();
    expect(
      within(section).getByText("Stand-by após handoff"),
    ).toBeInTheDocument();
    expect(
      within(section).getByText(
        "Defina por quanto tempo a IA permanece inativa após transferir a conversa para um atendente.",
      ),
    ).toBeInTheDocument();
    expect(
      within(section).getByRole("switch", {
        name: "Ativar stand-by após handoff",
      }),
    ).toHaveAttribute("aria-checked", "false");
    expect(
      within(section).getByText("Stand-by desativado"),
    ).toBeInTheDocument();
    expect(
      within(section).getByText(
        "A IA volta a responder automaticamente 24 horas após o handoff.",
      ),
    ).toBeInTheDocument();
    expect(
      within(section).queryByText("Quando reativar a IA?"),
    ).not.toBeInTheDocument();
    expect(within(section).queryByRole("combobox")).not.toBeInTheDocument();
    expect(within(section).queryByRole("spinbutton")).not.toBeInTheDocument();
    expect(section).not.toHaveTextContent("99");
  });

  it("renders the equivalent English manual state and the common ON explanation", async () => {
    await renderEditable({}, { locale: "en" });

    const section = standbySection();
    expect(
      within(section).getByText("Stand-by after handoff"),
    ).toBeInTheDocument();
    expect(
      within(section).getByText(
        "Define how long the AI remains inactive after transferring the conversation to an attendant.",
      ),
    ).toBeInTheDocument();
    expect(within(section).getByText("Stand-by enabled")).toBeInTheDocument();
    expect(
      within(section).getByText(
        "Choose when the AI may start responding again:",
      ),
    ).toBeInTheDocument();
    expect(
      within(section).getByText(
        "Only after manual reactivation: the AI remains inactive until it is reactivated by the master message or the inactive conversations panel.",
      ),
    ).toBeInTheDocument();
    expect(
      within(section).getByText(
        "After a custom period: the AI starts responding again automatically after the number of hours or days defined below.",
      ),
    ).toBeInTheDocument();
    expect(modeSelect("en")).toHaveTextContent("Manually only");
    expect(within(section).queryByRole("spinbutton")).not.toBeInTheDocument();
  });

  it("opens the mode menu by keyboard with ordered options and the current option indicated", async () => {
    await renderEditable();
    const trigger = modeSelect();

    trigger.focus();
    fireEvent.keyDown(trigger, { key: "Enter", code: "Enter" });

    const manual = await screen.findByRole("option", {
      name: "Somente manualmente",
    });
    const custom = screen.getByRole("option", {
      name: "Após um prazo personalizado",
    });
    expect(
      manual.compareDocumentPosition(custom) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(manual).toHaveAttribute("aria-selected", "true");
    expect(custom).toHaveAttribute("aria-selected", "false");
    expect(
      within(standbySection()).queryByRole("spinbutton"),
    ).not.toBeInTheDocument();
  });

  it("shows the custom input and an ordered keyboard-opened unit menu", async () => {
    await renderEditable({
      handoff_period: "cycle",
      handoff_cycle_hours: 6,
    });

    const section = standbySection();
    expect(modeSelect()).toHaveTextContent("Após um prazo personalizado");
    expect(
      within(section).getByText("Prazo para reativação automática"),
    ).toBeInTheDocument();
    expect(
      within(section).getByRole("spinbutton", {
        name: "Prazo para reativação automática",
      }),
    ).toHaveValue(6);
    const unit = unitSelect();
    expect(unit).toHaveTextContent("Horas");
    unit.focus();
    fireEvent.keyDown(unit, { key: "Enter", code: "Enter" });
    const hours = await screen.findByRole("option", { name: "Horas" });
    const days = screen.getByRole("option", { name: "Dias" });
    expect(
      hours.compareDocumentPosition(days) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(hours).toHaveAttribute("aria-selected", "true");
    expect(days).toHaveAttribute("aria-selected", "false");
  });

  it.each([
    ["manual", {}, "Somente manualmente", null, null],
    [
      "hours",
      { handoff_period: "cycle", handoff_cycle_hours: 48 },
      "Após um prazo personalizado",
      48,
      "Horas",
    ],
    [
      "numeric days",
      { handoff_period: "cycle", handoff_cycle_days: 9 },
      "Após um prazo personalizado",
      9,
      "Dias",
    ],
    [
      "legacy string days",
      { handoff_period: "cycle", handoff_cycle_days: "12" },
      "Após um prazo personalizado",
      12,
      "Dias",
    ],
    [
      "hours precedence",
      {
        handoff_period: "cycle",
        handoff_cycle_hours: 5,
        handoff_cycle_days: "12",
      },
      "Após um prazo personalizado",
      5,
      "Horas",
    ],
    [
      "missing duration",
      { handoff_period: "cycle" },
      "Somente manualmente",
      null,
      null,
    ],
  ] as const)(
    "hydrates %s deterministically as UI strings",
    async (_case, overrides, modeLabel, expectedValue, expectedUnit) => {
      await renderEditable(overrides);

      expect(modeSelect()).toHaveTextContent(modeLabel);
      if (expectedValue === null) {
        expect(
          within(standbySection()).queryByRole("spinbutton"),
        ).not.toBeInTheDocument();
      } else {
        expect(within(standbySection()).getByRole("spinbutton")).toHaveValue(
          expectedValue,
        );
        expect(unitSelect()).toHaveTextContent(expectedUnit as string);
      }
    },
  );

  it("hydrates a cycle without duration as manual and starts newly selected custom in hours", async () => {
    await renderEditable({
      handoff_period: "cycle",
      handoff_cycle_hours: null,
      handoff_cycle_days: null,
    });

    expect(modeSelect()).toHaveTextContent("Somente manualmente");
    selectOption(modeSelect(), "Após um prazo personalizado");
    expect(within(standbySection()).getByRole("spinbutton")).toHaveValue(null);
    expect(unitSelect()).toHaveTextContent("Horas");
  });

  it.each([
    ["off", { standby_mode: "inactive" }, null, null, null],
    ["manual", {}, null, null, null],
    [
      "hours",
      { handoff_period: "cycle", handoff_cycle_hours: 48 },
      "hours",
      48,
      null,
    ],
    [
      "days",
      { handoff_period: "cycle", handoff_cycle_days: "12" },
      "days",
      null,
      12,
    ],
  ] as const)(
    "saves the canonical five-key %s payload with numeric durations",
    async (state, overrides, _unit, hours, days) => {
      const fetchMock = await renderEditable(overrides);
      fireEvent.click(screen.getByTestId("btn-save-config"));

      await waitFor(() => expect(patchBodies(fetchMock)).toHaveLength(1));
      const payload = handoffPayload(patchBodies(fetchMock)[0]);
      expect(payload).toEqual({
        handoff_active: "yes",
        standby_mode: state === "off" ? "inactive" : "active",
        handoff_period:
          state === "hours" || state === "days" ? "cycle" : "permanent",
        handoff_cycle_hours: hours,
        handoff_cycle_days: days,
      });
      expect(patchBodies(fetchMock)[0]).not.toHaveProperty(
        "handoff_inactive_default_hours",
      );
      if (hours !== null)
        expect(typeof payload.handoff_cycle_hours).toBe("number");
      if (days !== null)
        expect(typeof payload.handoff_cycle_days).toBe("number");
    },
  );

  it.each([
    ["", "hours"],
    ["0", "hours"],
    ["-1", "hours"],
    ["1.5", "hours"],
    ["1e2", "hours"],
    ["721", "hours"],
    ["366", "days"],
  ] as const)(
    "blocks PATCH for invalid custom value %j in %s",
    async (value, unit) => {
      const fetchMock = await renderEditable({
        handoff_period: "cycle",
        handoff_cycle_hours: 1,
      });
      const input = within(standbySection()).getByRole("spinbutton");
      if (unit === "days") selectOption(unitSelect(), "Dias");
      fireEvent.change(input, { target: { value } });
      fireEvent.click(screen.getByTestId("btn-save-config"));

      const error = await within(standbySection()).findByRole("alert");
      expect(error).toHaveTextContent(
        unit === "hours"
          ? "Informe um número inteiro de 1 a 720 horas."
          : "Informe um número inteiro de 1 a 365 dias.",
      );
      expect(input).toHaveAttribute("aria-invalid", "true");
      expect(input).toHaveAttribute("aria-describedby", error.id);
      expect(patchBodies(fetchMock)).toHaveLength(0);
    },
  );

  it("clears the localized error after correction and then saves", async () => {
    const fetchMock = await renderEditable(
      { handoff_period: "cycle", handoff_cycle_hours: 4 },
      { locale: "en" },
    );
    const input = within(standbySection()).getByRole("spinbutton");
    fireEvent.change(input, { target: { value: "0" } });
    fireEvent.click(screen.getByTestId("btn-save-config"));

    expect(
      await within(standbySection()).findByRole("alert"),
    ).toHaveTextContent("Enter a whole number from 1 to 720 hours.");
    fireEvent.change(input, { target: { value: "720" } });
    expect(
      within(standbySection()).queryByRole("alert"),
    ).not.toBeInTheDocument();
    expect(input).toHaveAttribute("aria-invalid", "false");
    fireEvent.click(screen.getByTestId("btn-save-config"));
    await waitFor(() => expect(patchBodies(fetchMock)).toHaveLength(1));
  });

  it("resets incompatible controls across custom, manual, OFF, and ON transitions", async () => {
    await renderEditable();
    selectOption(modeSelect(), "Após um prazo personalizado");
    expect(within(standbySection()).getByRole("spinbutton")).toHaveValue(null);
    expect(unitSelect()).toHaveTextContent("Horas");

    fireEvent.change(within(standbySection()).getByRole("spinbutton"), {
      target: { value: "8" },
    });
    selectOption(modeSelect(), "Somente manualmente");
    expect(
      within(standbySection()).queryByRole("spinbutton"),
    ).not.toBeInTheDocument();
    expect(
      within(standbySection()).queryByRole("combobox", {
        name: "Unidade do prazo",
      }),
    ).not.toBeInTheDocument();

    const toggle = within(standbySection()).getByRole("switch", {
      name: "Ativar stand-by após handoff",
    });
    fireEvent.click(toggle);
    expect(
      within(standbySection()).queryByRole("combobox"),
    ).not.toBeInTheDocument();
    fireEvent.click(toggle);
    expect(modeSelect()).toHaveTextContent("Somente manualmente");
    expect(
      within(standbySection()).queryByRole("spinbutton"),
    ).not.toBeInTheDocument();
  });

  it("keeps the hydrated investor structure read-only and does not open menus", async () => {
    mountAs("investor", "pt");
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    render(
      <PageWrapper>
        <ClinicConfig />
      </PageWrapper>,
    );

    const section = await screen.findByTestId("handoff-standby-settings");
    const toggle = within(section).getByRole("switch", {
      name: "Ativar stand-by após handoff",
    });
    const mode = modeSelect();
    expect(toggle).toBeDisabled();
    expect(mode).toBeDisabled();
    fireEvent.click(mode);
    fireEvent.keyDown(mode, { key: "Enter", code: "Enter" });
    expect(
      screen.queryByRole("option", { name: "Após um prazo personalizado" }),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId("btn-save-config")).toBeDisabled();
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it("disables every control in a read-only hydrated custom state", async () => {
    mountAs("builder", "pt");
    vi.spyOn(authContext, "useAuth").mockReturnValue({
      clinicId: "selected-tenant",
      isReadOnly: true,
      isClinicAdmin: false,
      role: "investor",
      isBuilder: false,
      isSuperAdmin: false,
      isInvestor: false,
      viewingClinicId: "selected-tenant",
    } as ReturnType<typeof authContext.useAuth>);
    globalThis.fetch = vi.fn().mockResolvedValue(
      response({
        ...BASE_CONFIG,
        handoff_period: "cycle",
        handoff_cycle_days: 30,
      }),
    ) as unknown as typeof fetch;
    render(
      <PageWrapper>
        <ClinicConfig />
      </PageWrapper>,
    );

    const section = await screen.findByTestId("handoff-standby-settings");
    const toggle = within(section).getByRole("switch", {
      name: "Ativar stand-by após handoff",
    });
    const mode = modeSelect();
    const input = within(section).getByRole("spinbutton", {
      name: "Prazo para reativação automática",
    });
    const unit = unitSelect();
    expect(toggle).toBeDisabled();
    expect(mode).toBeDisabled();
    expect(input).toBeDisabled();
    expect(unit).toBeDisabled();
    fireEvent.keyDown(unit, { key: "Enter", code: "Enter" });
    expect(
      screen.queryByRole("option", { name: "Horas" }),
    ).not.toBeInTheDocument();
  });

  it.each(["builder", "super_admin"] as const)(
    "uses the selected tenant for %s GET and PATCH",
    async (role) => {
      const fetchMock = await renderEditable(
        {},
        { role, viewingClinicId: "tenant-42" },
      );
      expect(fetchMock).toHaveBeenCalledWith(
        "/v1/config?tenant_id=tenant-42",
        expect.objectContaining({ headers: expect.any(Object) }),
      );

      fireEvent.click(screen.getByTestId("btn-save-config"));
      await waitFor(() => expect(patchBodies(fetchMock)).toHaveLength(1));
      const patch = fetchMock.mock.calls.find(
        ([, init]) => init?.method === "PATCH",
      );
      expect(patch?.[0]).toBe("/v1/config?tenant_id=tenant-42");
    },
  );

  it("keeps the Handoff tab unavailable to clinic admins", async () => {
    mountAs("clinic_admin", "en", "clinic-admin");
    const fetchMock = vi.fn().mockResolvedValue(response(BASE_CONFIG));
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    render(
      <PageWrapper>
        <ClinicConfig />
      </PageWrapper>,
    );

    await screen.findByRole("tab", { name: "Identity" });
    expect(
      screen.queryByRole("tab", { name: "Handoff" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("handoff-standby-settings"),
    ).not.toBeInTheDocument();
  });

  it("lets clinic admins save allowed tabs without validating or sending hidden handoff policy", async () => {
    mountAs("clinic_admin", "pt", "clinic-admin");
    const malformedPolicy = {
      ...BASE_CONFIG,
      handoff_active: true,
      standby_mode: "active",
      handoff_period: "cycle",
      handoff_cycle_hours: true,
      handoff_cycle_days: "999",
    };
    const fetchMock = vi.fn().mockResolvedValue(response(malformedPolicy));
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    render(
      <PageWrapper>
        <ClinicConfig />
      </PageWrapper>,
    );

    await screen.findByRole("tab", { name: "Identidade" });
    fireEvent.click(screen.getByTestId("btn-save-config"));
    await waitFor(() => expect(patchBodies(fetchMock)).toHaveLength(1));
    expect(handoffPayload(patchBodies(fetchMock)[0])).toEqual({});
    expect(screen.queryByTestId("handoff-cycle-error")).not.toBeInTheDocument();
    expect(mocks.toast).not.toHaveBeenCalledWith(
      expect.objectContaining({ variant: "destructive" }),
    );
  });
});
