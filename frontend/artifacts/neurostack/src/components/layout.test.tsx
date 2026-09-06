import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { I18nProvider } from "@/context/i18n";
import { Layout } from "./layout";

const mocks = vi.hoisted(() => ({
  auth: {
    logout: vi.fn(),
    email: "builder@example.com" as string,
    clinicId: "builder-hq",
    role: "builder" as string,
    isSuperAdmin: false,
    isInvestor: false,
    isBuilder: true,
    isClinicAdmin: false,
    isReadOnly: false,
    viewingClinicId: "builder-clinic-1",
    setViewingClinicId: vi.fn(),
  },
  fetch: vi.fn(),
}));

vi.mock("@/context/auth", () => ({
  useAuth: () => mocks.auth,
}));

const builderClinics = [
  { id: "builder-clinic-1", name: "Clínica Áurea" },
  { id: "builder-clinic-2", name: "Saúde Central" },
  { id: "builder-clinic-3", name: "Instituto Horizonte" },
];

const nextBuilderClinics = [
  { id: "next-builder-clinic-1", name: "Nova Clínica" },
  { id: "next-builder-clinic-2", name: "Espaço Íris" },
];

const superAdminClinics = [
  {
    clinic_id: "super-clinic-1",
    clinic_name: "Clínica São Lucas",
    status: "active",
  },
  {
    clinic_id: "super-clinic-2",
    clinic_name: "Instituto Órbita",
    status: "active",
  },
  {
    clinic_id: "inactive-clinic",
    clinic_name: "Clínica Inativa",
    status: "inactive",
  },
];

function response(data: unknown) {
  return Promise.resolve({
    ok: true,
    json: async () => data,
  } as Response);
}

function deferredResponse() {
  let resolve!: (value: Response) => void;
  const promise = new Promise<Response>((resolvePromise) => {
    resolve = resolvePromise;
  });

  return {
    promise,
    resolve: (data: unknown) =>
      resolve({
        ok: true,
        json: async () => data,
      } as Response),
  };
}

function setRole(
  role: "builder" | "super_admin" | "clinic_admin" | "investor",
) {
  mocks.auth.role = role;
  mocks.auth.isBuilder = role === "builder";
  mocks.auth.isSuperAdmin = role === "super_admin";
  mocks.auth.isClinicAdmin = role === "clinic_admin";
  mocks.auth.isInvestor = role === "investor";
  mocks.auth.isReadOnly = role === "investor";
}

function renderLayout() {
  return render(
    <I18nProvider>
      <Layout>
        <div>Page content</div>
      </Layout>
    </I18nProvider>,
  );
}

function optionLabels(select: HTMLSelectElement) {
  return within(select)
    .getAllByRole("option")
    .map((option) => option.textContent);
}

function expectInvestorSalesCtaPlacement(cta: HTMLElement) {
  const sidebarContent = cta.parentElement;
  expect(sidebarContent).not.toBeNull();

  const logo = within(sidebarContent!).getByAltText("NeuroStack");
  const activeClinicName = within(sidebarContent!).getByTestId(
    "active-clinic-name",
  );

  expect(logo.parentElement?.nextElementSibling).toBe(cta);
  expect(cta.nextElementSibling).toBe(activeClinicName.parentElement);
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("ns_auth", JSON.stringify({ token: "test-token" }));
  localStorage.setItem("ns_locale", "en");
  window.history.replaceState({}, "", "/");

  setRole("builder");
  mocks.auth.email = "builder@example.com";
  mocks.auth.clinicId = "builder-hq";
  mocks.auth.viewingClinicId = "builder-clinic-1";
  mocks.auth.logout.mockReset();
  mocks.auth.setViewingClinicId.mockReset();
  mocks.fetch.mockReset();
  mocks.fetch.mockImplementation((input: string | URL | Request) => {
    const url = String(input);
    if (url === "/v1/builder/clinics") {
      return response(
        mocks.auth.email === "next-builder@example.com"
          ? nextBuilderClinics
          : builderClinics,
      );
    }
    if (url === "/v1/admin/clinics") return response(superAdminClinics);
    if (url.startsWith("/v1/config?clinic_id=")) {
      return response({ clinic_name: "Selected Clinic", bot_active: true });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  });
  vi.stubGlobal("fetch", mocks.fetch);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("<Layout /> clinic selector search", () => {
  it("filters the builder selector with shared trim, case, diacritic, and partial matching", async () => {
    renderLayout();

    const search = await screen.findByTestId("builder-clinic-search");
    const select = screen.getByTestId(
      "builder-clinic-dropdown",
    ) as HTMLSelectElement;
    await waitFor(() =>
      expect(optionLabels(select)).toEqual(
        builderClinics.map((clinic) => clinic.name),
      ),
    );

    expect(screen.getByRole("searchbox", { name: "Search clinics" })).toBe(
      search,
    );
    expect(search).toHaveAttribute("placeholder", "Search by clinic name");
    expect(
      screen.queryByTestId("super-admin-clinic-search"),
    ).not.toBeInTheDocument();

    mocks.auth.setViewingClinicId.mockClear();
    fireEvent.change(search, { target: { value: "  sAuDe CeN  " } });

    expect(optionLabels(select)).toEqual(["Select Clinic", "Saúde Central"]);
    expect(select.value).toBe("");
    const hiddenSelection = within(select).getByRole("option", {
      name: "Select Clinic",
    }) as HTMLOptionElement;
    expect(hiddenSelection.value).toBe("");
    expect(hiddenSelection.disabled).toBe(true);
    expect(mocks.auth.viewingClinicId).toBe("builder-clinic-1");
    expect(mocks.auth.setViewingClinicId).not.toHaveBeenCalled();

    fireEvent.change(search, { target: { value: "" } });
    expect(optionLabels(select)).toEqual(
      builderClinics.map((clinic) => clinic.name),
    );
    expect(select.value).toBe("builder-clinic-1");
    expect(mocks.auth.setViewingClinicId).not.toHaveBeenCalled();

    fireEvent.change(select, { target: { value: "builder-clinic-2" } });
    expect(mocks.auth.setViewingClinicId).toHaveBeenCalledTimes(1);
    expect(mocks.auth.setViewingClinicId).toHaveBeenCalledWith(
      "builder-clinic-2",
    );
  });

  it("renders a localized builder empty state without changing context", async () => {
    renderLayout();

    const search = await screen.findByTestId("builder-clinic-search");
    const select = screen.getByTestId(
      "builder-clinic-dropdown",
    ) as HTMLSelectElement;
    await waitFor(() =>
      expect(optionLabels(select)).toContain("Clínica Áurea"),
    );
    mocks.auth.setViewingClinicId.mockClear();

    fireEvent.change(search, { target: { value: "missing clinic" } });

    expect(search).not.toBeDisabled();
    expect(search).toHaveValue("missing clinic");
    expect(optionLabels(select)).toEqual(["No clinics found"]);
    expect(select.value).toBe("");
    const noResults = within(select).getByRole("option", {
      name: "No clinics found",
    }) as HTMLOptionElement;
    expect(noResults.value).toBe("");
    expect(noResults.disabled).toBe(true);
    expect(mocks.auth.viewingClinicId).toBe("builder-clinic-1");
    expect(mocks.auth.setViewingClinicId).not.toHaveBeenCalled();
  });

  it("filters only active super-admin clinics and uses Portuguese search copy", async () => {
    localStorage.setItem("ns_locale", "pt");
    setRole("super_admin");
    mocks.auth.email = "admin@example.com";
    mocks.auth.clinicId = "";
    mocks.auth.viewingClinicId = "super-clinic-1";
    renderLayout();

    const search = await screen.findByRole("searchbox", {
      name: "Buscar clínicas",
    });
    const select = screen.getByTestId(
      "super-admin-clinic-dropdown",
    ) as HTMLSelectElement;
    await waitFor(() => {
      expect(optionLabels(select)).toEqual([
        "Clínica São Lucas",
        "Instituto Órbita",
      ]);
    });

    expect(search).toHaveAttribute(
      "placeholder",
      "Buscar pelo nome da clínica",
    );
    expect(
      screen.queryByTestId("builder-clinic-search"),
    ).not.toBeInTheDocument();
    expect(optionLabels(select)).not.toContain("Clínica Inativa");

    mocks.auth.setViewingClinicId.mockClear();
    fireEvent.change(search, { target: { value: "  ORBITA " } });

    expect(optionLabels(select)).toEqual([
      "Selecionar Clínica",
      "Instituto Órbita",
    ]);
    expect(select.value).toBe("");
    const hiddenSelection = within(select).getByRole("option", {
      name: "Selecionar Clínica",
    }) as HTMLOptionElement;
    expect(hiddenSelection.value).toBe("");
    expect(hiddenSelection.disabled).toBe(true);
    expect(mocks.auth.setViewingClinicId).not.toHaveBeenCalled();

    fireEvent.change(search, { target: { value: "missing clinic" } });
    expect(optionLabels(select)).toEqual(["Nenhuma clínica encontrada"]);
    const noResults = within(select).getByRole("option", {
      name: "Nenhuma clínica encontrada",
    }) as HTMLOptionElement;
    expect(noResults.value).toBe("");
    expect(noResults.disabled).toBe(true);
    expect(mocks.auth.setViewingClinicId).not.toHaveBeenCalled();

    fireEvent.change(search, { target: { value: "orbita" } });
    fireEvent.change(select, { target: { value: "super-clinic-2" } });
    expect(mocks.auth.setViewingClinicId).toHaveBeenCalledWith(
      "super-clinic-2",
    );
  });

  it("does not expose either clinic search to an unauthorized role", async () => {
    setRole("clinic_admin");
    mocks.auth.email = "clinic-admin@example.com";
    mocks.auth.clinicId = "clinic-admin-clinic";
    mocks.auth.viewingClinicId = "clinic-admin-clinic";
    renderLayout();

    await waitFor(() =>
      expect(mocks.fetch).toHaveBeenCalledWith(
        "/v1/config?clinic_id=clinic-admin-clinic",
        expect.any(Object),
      ),
    );
    expect(
      screen.queryByTestId("builder-clinic-search"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("super-admin-clinic-search"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("builder-clinic-dropdown"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("super-admin-clinic-dropdown"),
    ).not.toBeInTheDocument();
    expect(mocks.fetch).not.toHaveBeenCalledWith(
      "/v1/builder/clinics",
      expect.anything(),
    );
    expect(mocks.fetch).not.toHaveBeenCalledWith(
      "/v1/admin/clinics",
      expect.anything(),
    );
  });

  it("clears search and reloads full scoped clinics when session identity and role change", async () => {
    const view = renderLayout();

    const initialSearch = await screen.findByTestId("builder-clinic-search");
    const initialSelect = screen.getByTestId(
      "builder-clinic-dropdown",
    ) as HTMLSelectElement;
    await waitFor(() =>
      expect(optionLabels(initialSelect)).toContain("Clínica Áurea"),
    );
    fireEvent.change(initialSearch, { target: { value: "horizonte" } });
    expect(optionLabels(initialSelect)).toEqual([
      "Select Clinic",
      "Instituto Horizonte",
    ]);

    mocks.auth.email = "next-builder@example.com";
    mocks.auth.viewingClinicId = "next-builder-clinic-1";
    mocks.auth.setViewingClinicId.mockClear();
    view.rerender(
      <I18nProvider>
        <Layout>
          <div>Page content</div>
        </Layout>
      </I18nProvider>,
    );

    const nextSearch = await screen.findByTestId("builder-clinic-search");
    const nextSelect = screen.getByTestId(
      "builder-clinic-dropdown",
    ) as HTMLSelectElement;
    await waitFor(() => expect(nextSearch).toHaveValue(""));
    await waitFor(() =>
      expect(optionLabels(nextSelect)).toEqual(
        nextBuilderClinics.map((clinic) => clinic.name),
      ),
    );
    expect(mocks.auth.setViewingClinicId).not.toHaveBeenCalled();

    fireEvent.change(nextSearch, { target: { value: "iris" } });
    setRole("super_admin");
    mocks.auth.email = "admin@example.com";
    mocks.auth.clinicId = "";
    mocks.auth.viewingClinicId = "super-clinic-1";
    mocks.auth.setViewingClinicId.mockClear();
    view.rerender(
      <I18nProvider>
        <Layout>
          <div>Page content</div>
        </Layout>
      </I18nProvider>,
    );

    const adminSearch = await screen.findByTestId("super-admin-clinic-search");
    const adminSelect = screen.getByTestId(
      "super-admin-clinic-dropdown",
    ) as HTMLSelectElement;
    await waitFor(() => expect(adminSearch).toHaveValue(""));
    await waitFor(() => {
      expect(optionLabels(adminSelect)).toEqual([
        "Clínica São Lucas",
        "Instituto Órbita",
      ]);
    });
    expect(
      screen.queryByTestId("builder-clinic-search"),
    ).not.toBeInTheDocument();
    expect(mocks.auth.setViewingClinicId).not.toHaveBeenCalled();
  });

  it("ignores an old builder response after a same-role session transition", async () => {
    const oldBuilderResponse = deferredResponse();
    const newBuilderResponse = deferredResponse();
    let builderRequestCount = 0;
    mocks.fetch.mockImplementation((input: string | URL | Request) => {
      const url = String(input);
      if (url === "/v1/builder/clinics") {
        builderRequestCount += 1;
        return builderRequestCount === 1
          ? oldBuilderResponse.promise
          : newBuilderResponse.promise;
      }
      if (url.startsWith("/v1/config?clinic_id=")) {
        return response({ clinic_name: "Selected Clinic", bot_active: true });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    const view = renderLayout();
    await waitFor(() => expect(builderRequestCount).toBe(1));

    mocks.auth.email = "next-builder@example.com";
    mocks.auth.viewingClinicId = "next-builder-clinic-1";
    view.rerender(
      <I18nProvider>
        <Layout>
          <div>Page content</div>
        </Layout>
      </I18nProvider>,
    );
    await waitFor(() => expect(builderRequestCount).toBe(2));

    newBuilderResponse.resolve(nextBuilderClinics);
    const select = (await screen.findByTestId(
      "builder-clinic-dropdown",
    )) as HTMLSelectElement;
    await waitFor(() => {
      expect(optionLabels(select)).toEqual(
        nextBuilderClinics.map((clinic) => clinic.name),
      );
    });
    mocks.auth.setViewingClinicId.mockClear();

    await act(async () => {
      oldBuilderResponse.resolve(builderClinics);
      await oldBuilderResponse.promise;
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(optionLabels(select)).toEqual(
      nextBuilderClinics.map((clinic) => clinic.name),
    );
    expect(mocks.auth.setViewingClinicId).not.toHaveBeenCalled();
  });

  it("ignores an old builder response after transitioning to super admin", async () => {
    const oldBuilderResponse = deferredResponse();
    const currentAdminResponse = deferredResponse();
    mocks.fetch.mockImplementation((input: string | URL | Request) => {
      const url = String(input);
      if (url === "/v1/builder/clinics") return oldBuilderResponse.promise;
      if (url === "/v1/admin/clinics") return currentAdminResponse.promise;
      if (url.startsWith("/v1/config?clinic_id=")) {
        return response({ clinic_name: "Selected Clinic", bot_active: true });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    const view = renderLayout();
    await waitFor(() => {
      expect(mocks.fetch).toHaveBeenCalledWith(
        "/v1/builder/clinics",
        expect.any(Object),
      );
    });

    setRole("super_admin");
    mocks.auth.email = "admin@example.com";
    mocks.auth.clinicId = "";
    mocks.auth.viewingClinicId = "super-clinic-1";
    view.rerender(
      <I18nProvider>
        <Layout>
          <div>Page content</div>
        </Layout>
      </I18nProvider>,
    );
    await waitFor(() => {
      expect(mocks.fetch).toHaveBeenCalledWith(
        "/v1/admin/clinics",
        expect.any(Object),
      );
    });

    currentAdminResponse.resolve(superAdminClinics);
    const select = (await screen.findByTestId(
      "super-admin-clinic-dropdown",
    )) as HTMLSelectElement;
    await waitFor(() => {
      expect(optionLabels(select)).toEqual([
        "Clínica São Lucas",
        "Instituto Órbita",
      ]);
    });
    mocks.auth.setViewingClinicId.mockClear();

    await act(async () => {
      oldBuilderResponse.resolve(builderClinics);
      await oldBuilderResponse.promise;
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(optionLabels(select)).toEqual([
      "Clínica São Lucas",
      "Instituto Órbita",
    ]);
    expect(mocks.auth.setViewingClinicId).not.toHaveBeenCalled();
  });

  it("keeps the loaded builder selector mounted when explicit selection changes context", async () => {
    const unexpectedRefetch = deferredResponse();
    let builderRequestCount = 0;
    mocks.fetch.mockImplementation((input: string | URL | Request) => {
      const url = String(input);
      if (url === "/v1/builder/clinics") {
        builderRequestCount += 1;
        return builderRequestCount === 1
          ? response(builderClinics)
          : unexpectedRefetch.promise;
      }
      if (url.startsWith("/v1/config?clinic_id=")) {
        return response({ clinic_name: "Selected Clinic", bot_active: true });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });

    const view = renderLayout();
    const search = await screen.findByTestId("builder-clinic-search");
    const select = screen.getByTestId(
      "builder-clinic-dropdown",
    ) as HTMLSelectElement;
    await waitFor(() =>
      expect(optionLabels(select)).toEqual(
        builderClinics.map((clinic) => clinic.name),
      ),
    );

    fireEvent.change(select, { target: { value: "builder-clinic-2" } });
    expect(mocks.auth.setViewingClinicId).toHaveBeenCalledWith(
      "builder-clinic-2",
    );

    mocks.auth.viewingClinicId = "builder-clinic-2";
    view.rerender(
      <I18nProvider>
        <Layout>
          <div>Page content</div>
        </Layout>
      </I18nProvider>,
    );

    expect(screen.getByTestId("builder-clinic-search")).toBe(search);
    expect(screen.getByTestId("builder-clinic-dropdown")).toBe(select);
    expect(optionLabels(select)).toEqual(
      builderClinics.map((clinic) => clinic.name),
    );
    expect(select).toHaveValue("builder-clinic-2");
    expect(builderRequestCount).toBe(1);
  });
});

describe("<Layout /> inactive queue navigation", () => {
  it("places the user-inactive item between handoff and traces and applies the normal selected style", async () => {
    localStorage.setItem("ns_locale", "pt");
    setRole("clinic_admin");
    mocks.auth.email = "clinic-admin@example.com";
    mocks.auth.clinicId = "clinic-admin-clinic";
    mocks.auth.viewingClinicId = "clinic-admin-clinic";
    window.history.replaceState({}, "", "/inactive-queue");

    renderLayout();
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalled());

    const handoff = screen.getByTestId("nav-link-handoff-queue");
    const inactive = screen.getByTestId("nav-link-inactive-queue");
    const traces = screen.getByTestId("nav-link-trace-history");
    expect(
      handoff.compareDocumentPosition(inactive) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      inactive.compareDocumentPosition(traces) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(inactive).toHaveTextContent("Fila de Inativos");
    expect(inactive).toHaveClass(
      "bg-sidebar-accent",
      "text-sidebar-accent-foreground",
    );
    expect(inactive.querySelector(".lucide-user-x")).toBeInTheDocument();
  });

  it("does not show the inactive queue navigation item to investors", async () => {
    setRole("investor");
    mocks.auth.email = "investor@example.com";
    mocks.auth.clinicId = "investor-clinic";
    mocks.auth.viewingClinicId = "investor-clinic";

    renderLayout();
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalled());

    expect(
      screen.queryByTestId("nav-link-inactive-queue"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Inactive Queue")).not.toBeInTheDocument();
  });
});

describe("<Layout /> investor sales CTA", () => {
  it("renders one secure, styled English CTA in each mounted sidebar at the required position", async () => {
    setRole("investor");
    mocks.auth.email = "investor@example.com";
    mocks.auth.clinicId = "investor-clinic";
    mocks.auth.viewingClinicId = "investor-clinic";

    renderLayout();
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalled());

    const desktopCta = screen.getByRole("link", { name: "Talk to sales" });
    expect(screen.getAllByTestId("investor-sales-cta")).toHaveLength(1);
    expect(desktopCta).toHaveAttribute(
      "href",
      "https://wa.me/551121105486",
    );
    expect(desktopCta).toHaveAttribute("target", "_blank");
    expect(desktopCta).toHaveAttribute("rel", "noopener noreferrer");
    expect(desktopCta).toHaveClass(
      "bg-amber-400",
      "text-black",
      "font-bold",
      "h-12",
      "mx-2",
      "rounded-md",
    );
    expect(
      desktopCta.querySelector(".lucide-message-square"),
    ).toBeInTheDocument();
    expectInvestorSalesCtaPlacement(desktopCta);

    fireEvent.click(screen.getByTestId("mobile-menu-trigger"));
    await waitFor(() =>
      expect(screen.getAllByTestId("investor-sales-cta")).toHaveLength(2),
    );
    screen
      .getAllByTestId("investor-sales-cta")
      .forEach(expectInvestorSalesCtaPlacement);
  });

  it("uses the exact Portuguese copy", async () => {
    localStorage.setItem("ns_locale", "pt");
    setRole("investor");
    mocks.auth.email = "investidor@example.com";
    mocks.auth.clinicId = "investor-clinic";
    mocks.auth.viewingClinicId = "investor-clinic";

    renderLayout();

    expect(
      await screen.findByRole("link", { name: "Falar com comercial" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Talk to sales")).not.toBeInTheDocument();
  });

  it.each(["clinic_admin", "builder", "super_admin"] as const)(
    "does not render the CTA for %s",
    async (role) => {
      setRole(role);
      mocks.auth.email = `${role}@example.com`;
      mocks.auth.clinicId = `${role}-clinic`;
      mocks.auth.viewingClinicId = `${role}-clinic`;

      renderLayout();
      await waitFor(() => expect(mocks.fetch).toHaveBeenCalled());

      expect(
        screen.queryByTestId("investor-sales-cta"),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("link", { name: "Talk to sales" }),
      ).not.toBeInTheDocument();
    },
  );
});

describe("<Layout /> password self-service", () => {
  it("shows the accessible trigger only to builder and clinic admins, then logs out only after its successful request", async () => {
    mocks.fetch.mockImplementation(
      (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/v1/auth/me/password") {
          expect(init).toMatchObject({ method: "PATCH" });
          return response({ ok: true });
        }
        if (url === "/v1/builder/clinics") return response(builderClinics);
        if (url === "/v1/admin/clinics") return response(superAdminClinics);
        if (url.startsWith("/v1/config?clinic_id="))
          return response({ clinic_name: "Selected Clinic" });
        throw new Error(`Unexpected fetch: ${url}`);
      },
    );
    renderLayout();
    fireEvent.click(await screen.findByTestId("btn-change-own-password"));
    const fields = [
      screen.getByLabelText("New password"),
      screen.getByLabelText("Confirm new password"),
    ];
    fireEvent.change(fields[0], { target: { value: "secret" } });
    fireEvent.change(fields[1], { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    await waitFor(() => expect(mocks.auth.logout).toHaveBeenCalledTimes(1));
    expect(mocks.fetch).toHaveBeenCalledWith(
      "/v1/auth/me/password",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({
          new_password: "secret",
          confirm_password: "secret",
        }),
      }),
    );

    cleanup();
    setRole("super_admin");
    renderLayout();
    expect(
      screen.queryByTestId("btn-change-own-password"),
    ).not.toBeInTheDocument();
  });

  it("shows the trigger to clinic admins and keeps it absent for investors", async () => {
    setRole("clinic_admin");
    mocks.auth.email = "clinic-admin@example.com";
    mocks.auth.clinicId = "clinic-1";
    mocks.auth.viewingClinicId = "clinic-1";
    renderLayout();
    expect(
      await screen.findByTestId("btn-change-own-password"),
    ).toBeInTheDocument();

    cleanup();
    setRole("investor");
    mocks.auth.email = "investor@example.com";
    renderLayout();
    expect(
      screen.queryByTestId("btn-change-own-password"),
    ).not.toBeInTheDocument();
  });

  it("opens from the native password button and closes the dialog with Escape", async () => {
    renderLayout();
    const trigger = await screen.findByTestId("btn-change-own-password");
    expect(trigger.tagName).toBe("BUTTON");

    // Keyboard activation of a native button is delivered to React as a click
    // with detail 0; jsdom does not synthesize that browser default itself.
    fireEvent.click(trigger, { detail: 0 });
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
  });
});
