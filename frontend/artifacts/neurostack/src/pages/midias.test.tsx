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
import Midias from "./midias";

const mocks = vi.hoisted(() => ({
  auth: {
    clinicId: "clinic-own",
    tenantId: "tenant-own",
    role: "clinic_admin",
    isBuilder: false,
    isClinicAdmin: true,
    isInvestor: false,
    isSuperAdmin: false,
    viewingClinicId: "clinic-own",
  },
  fetch: vi.fn(),
}));

vi.mock("@/context/auth", () => ({
  useAuth: () => mocks.auth,
}));

interface TestAsset {
  id: string;
  tenant_id: string;
  display_name: string;
  original_filename: string;
  category: "video" | "audio" | "pdf" | "image";
  mime_type: string;
  size_bytes: number;
  usage_trigger: string | null;
  auto_suggest: boolean;
  created_at: string;
  updated_at: string;
}

function asset(overrides: Partial<TestAsset> = {}): TestAsset {
  return {
    id: "asset-1",
    tenant_id: "clinic-own",
    display_name: "Orientações Pós-Botox",
    original_filename: "orientações.pdf",
    category: "pdf",
    mime_type: "application/pdf",
    size_bytes: 1234,
    usage_trigger: "Após o procedimento, se solicitado",
    auto_suggest: true,
    created_at: "2026-10-08T12:00:00Z",
    updated_at: "2026-10-08T12:00:00Z",
    ...overrides,
  };
}

function jsonResponse(data: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => data,
    blob: async () => new Blob(["media"]),
  } as Response);
}

function deferredResponse() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return {
    promise,
    resolveJson(data: unknown, status = 200) {
      resolve({
        ok: status >= 200 && status < 300,
        status,
        json: async () => data,
        blob: async () => new Blob(["media"]),
      } as Response);
    },
  };
}

function setRole(
  role: "clinic_admin" | "builder" | "super_admin" | "investor",
  viewingClinicId = "clinic-selected",
) {
  mocks.auth.role = role;
  mocks.auth.isClinicAdmin = role === "clinic_admin";
  mocks.auth.isBuilder = role === "builder";
  mocks.auth.isSuperAdmin = role === "super_admin";
  mocks.auth.isInvestor = role === "investor";
  mocks.auth.viewingClinicId = viewingClinicId;
}

function renderPage() {
  return render(
    <I18nProvider>
      <Midias />
    </I18nProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("ns_auth", JSON.stringify({ token: "media-token" }));
  localStorage.setItem("ns_locale", "en");
  setRole("clinic_admin", "clinic-own");
  mocks.auth.clinicId = "clinic-own";
  mocks.auth.tenantId = "tenant-own";
  mocks.fetch.mockReset();
  vi.stubGlobal("fetch", mocks.fetch);
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  vi.stubGlobal("URL", {
    ...URL,
    createObjectURL: vi.fn(() => "blob:media-download"),
    revokeObjectURL: vi.fn(),
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("<Midias /> catalog operations", () => {
  it("uses the clinic-admin tenant implicitly and uploads an exact four-field catalog roundtrip", async () => {
    const existing = asset();
    const created = asset({
      id: "asset-uploaded",
      display_name: "Boas-vindas",
      original_filename: "boas-vindas.mp3",
      category: "audio",
      mime_type: "audio/mpeg",
      usage_trigger: "Na primeira conversa",
      auto_suggest: true,
    });
    let uploadBody: FormData | undefined;
    mocks.fetch.mockImplementation(
      (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/v1/media-assets" && init?.method === "POST") {
          uploadBody = init.body as FormData;
          return jsonResponse(created, 201);
        }
        if (url === "/v1/media-assets" && !init?.method) {
          return jsonResponse({
            items: [existing],
            next_cursor: null,
            has_more: false,
          });
        }
        throw new Error(`Unexpected fetch: ${url}`);
      },
    );

    renderPage();

    expect(
      await screen.findByText("Orientações Pós-Botox"),
    ).toBeInTheDocument();
    expect(mocks.fetch).toHaveBeenCalledWith(
      "/v1/media-assets",
      expect.objectContaining({
        headers: { Authorization: "Bearer media-token" },
      }),
    );
    expect(
      screen.getByText(/they do not send media or run any runtime action/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Após o procedimento, se solicitado"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Suggestion enabled in catalog"),
    ).toBeInTheDocument();

    const file = new File(["ID3-media"], "boas-vindas.mp3", {
      type: "audio/mpeg",
    });
    fireEvent.change(screen.getByLabelText("File"), {
      target: { files: [file] },
    });
    fireEvent.change(screen.getByLabelText("Display name"), {
      target: { value: "Boas-vindas" },
    });
    fireEvent.change(screen.getByLabelText("Original filename"), {
      target: { value: "boas-vindas.mp3" },
    });
    fireEvent.change(screen.getByLabelText("Usage trigger"), {
      target: { value: "Na primeira conversa" },
    });
    fireEvent.click(
      screen.getByRole("switch", { name: "Automatic suggestion" }),
    );
    const uploadButton = screen.getByRole("button", { name: "Upload media" });
    expect(uploadButton).toBeEnabled();
    fireEvent.submit(uploadButton.closest("form")!);

    await waitFor(() => expect(uploadBody).toBeDefined());
    expect(
      await screen.findByText("Media uploaded successfully."),
    ).toBeInTheDocument();
    expect(uploadBody?.get("file")).toBeInstanceOf(File);
    expect(uploadBody?.get("display_name")).toBe("Boas-vindas");
    expect(uploadBody?.get("original_filename")).toBe("boas-vindas.mp3");
    expect(uploadBody?.get("usage_trigger")).toBe("Na primeira conversa");
    expect(uploadBody?.get("auto_suggest")).toBe("true");
    expect(screen.getByText("Boas-vindas")).toBeInTheDocument();
    expect(screen.getByText("Na primeira conversa")).toBeInTheDocument();
    expect(screen.getByLabelText("File")).toHaveValue("");
  });

  it.each(["builder", "super_admin"] as const)(
    "sends the selected clinic and bearer token on every %s operation",
    async (role) => {
      setRole(role, "clinic-42");
      const first = asset({ tenant_id: "clinic-42" });
      const second = asset({
        id: "asset-2",
        tenant_id: "clinic-42",
        display_name: "Foto da recepção",
        original_filename: "recepção.png",
        category: "image",
        mime_type: "image/png",
        usage_trigger: null,
        auto_suggest: false,
      });
      const updated = asset({
        tenant_id: "clinic-42",
        display_name: "Guia revisado",
        original_filename: "guia-final.pdf",
        usage_trigger: "Quando o paciente pedir",
        auto_suggest: false,
      });
      const uploaded = asset({
        id: "asset-uploaded",
        tenant_id: "clinic-42",
        display_name: "Áudio de boas-vindas",
        original_filename: "boas-vindas.mp3",
        category: "audio",
        mime_type: "audio/mpeg",
      });

      mocks.fetch.mockImplementation(
        (input: string | URL | Request, init?: RequestInit) => {
          const url = String(input);
          if (url === "/v1/media-assets?tenant_id=clinic-42" && !init?.method) {
            return jsonResponse({
              items: [first],
              next_cursor: "opaque-cursor",
              has_more: true,
            });
          }
          if (
            url ===
              "/v1/media-assets?tenant_id=clinic-42&cursor=opaque-cursor" &&
            !init?.method
          ) {
            return jsonResponse({
              items: [second],
              next_cursor: null,
              has_more: false,
            });
          }
          if (
            url === "/v1/media-assets?tenant_id=clinic-42" &&
            init?.method === "POST"
          ) {
            return jsonResponse(uploaded, 201);
          }
          if (
            url === "/v1/media-assets/asset-1?tenant_id=clinic-42" &&
            init?.method === "PATCH"
          ) {
            expect(JSON.parse(String(init.body))).toEqual({
              display_name: "Guia revisado",
              original_filename: "guia-final.pdf",
              usage_trigger: "Quando o paciente pedir",
              auto_suggest: false,
            });
            return jsonResponse(updated);
          }
          if (
            url === "/v1/media-assets/asset-1/content?tenant_id=clinic-42" &&
            !init?.method
          ) {
            return jsonResponse({});
          }
          if (
            url === "/v1/media-assets/asset-1?tenant_id=clinic-42" &&
            init?.method === "DELETE"
          ) {
            return jsonResponse(undefined, 204);
          }
          throw new Error(`Unexpected fetch: ${url}`);
        },
      );

      renderPage();
      expect(
        await screen.findByText("Orientações Pós-Botox"),
      ).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Load more" }));
      expect(await screen.findByText("Foto da recepção")).toBeInTheDocument();

      fireEvent.change(screen.getByLabelText("File"), {
        target: {
          files: [
            new File(["ID3-media"], "boas-vindas.mp3", {
              type: "audio/mpeg",
            }),
          ],
        },
      });
      fireEvent.change(screen.getByLabelText("Display name"), {
        target: { value: "Áudio de boas-vindas" },
      });
      fireEvent.submit(
        screen.getByRole("button", { name: "Upload media" }).closest("form")!,
      );
      expect(
        await screen.findByText("Media uploaded successfully."),
      ).toBeInTheDocument();

      fireEvent.click(
        screen.getByRole("button", { name: "Edit: Orientações Pós-Botox" }),
      );
      const dialog = screen.getByRole("dialog");
      fireEvent.change(within(dialog).getByLabelText("Display name"), {
        target: { value: "Guia revisado" },
      });
      fireEvent.change(within(dialog).getByLabelText("Original filename"), {
        target: { value: "guia-final.pdf" },
      });
      fireEvent.change(within(dialog).getByLabelText("Usage trigger"), {
        target: { value: "Quando o paciente pedir" },
      });
      fireEvent.click(
        within(dialog).getByRole("switch", { name: "Automatic suggestion" }),
      );
      fireEvent.click(
        within(dialog).getByRole("button", { name: "Save changes" }),
      );

      expect(
        await screen.findByText("Media metadata updated."),
      ).toBeInTheDocument();
      expect(screen.getByText("Guia revisado")).toBeInTheDocument();
      expect(screen.getByText("Quando o paciente pedir")).toBeInTheDocument();
      expect(
        screen.getAllByText("Suggestion disabled in catalog"),
      ).toHaveLength(2);

      fireEvent.click(
        screen.getByRole("button", { name: "Download: Guia revisado" }),
      );
      await waitFor(() =>
        expect(URL.createObjectURL).toHaveBeenCalledWith(expect.any(Blob)),
      );
      expect(HTMLAnchorElement.prototype.click).toHaveBeenCalled();

      fireEvent.click(
        screen.getByRole("button", { name: "Delete: Guia revisado" }),
      );
      fireEvent.click(screen.getByRole("button", { name: "Delete media" }));
      expect(await screen.findByText("Media deleted.")).toBeInTheDocument();
      expect(screen.queryByText("Guia revisado")).not.toBeInTheDocument();

      const mediaCalls = mocks.fetch.mock.calls.map(([input]) => String(input));
      expect(mediaCalls).toEqual([
        "/v1/media-assets?tenant_id=clinic-42",
        "/v1/media-assets?tenant_id=clinic-42&cursor=opaque-cursor",
        "/v1/media-assets?tenant_id=clinic-42",
        "/v1/media-assets/asset-1?tenant_id=clinic-42",
        "/v1/media-assets/asset-1/content?tenant_id=clinic-42",
        "/v1/media-assets/asset-1?tenant_id=clinic-42",
      ]);
      for (const [, init] of mocks.fetch.mock.calls) {
        expect(init).toEqual(
          expect.objectContaining({
            headers: expect.objectContaining({
              Authorization: "Bearer media-token",
            }),
          }),
        );
      }
    },
  );

  it("keeps a completed upload when an older initial list resolves afterward", async () => {
    const pendingList = deferredResponse();
    const created = asset({
      id: "asset-race-upload",
      display_name: "Upload preservado",
      original_filename: "preservado.pdf",
    });

    mocks.fetch.mockImplementation(
      (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/v1/media-assets" && init?.method === "POST") {
          return jsonResponse(created, 201);
        }
        if (url === "/v1/media-assets" && !init?.method) {
          return pendingList.promise;
        }
        throw new Error(`Unexpected fetch: ${url}`);
      },
    );

    renderPage();
    expect(screen.getByRole("status")).toHaveTextContent("Loading media");

    fireEvent.change(screen.getByLabelText("File"), {
      target: {
        files: [
          new File(["pdf"], "preservado.pdf", { type: "application/pdf" }),
        ],
      },
    });
    fireEvent.change(screen.getByLabelText("Display name"), {
      target: { value: "Upload preservado" },
    });
    fireEvent.submit(
      screen.getByRole("button", { name: "Upload media" }).closest("form")!,
    );

    expect(await screen.findByText("Upload preservado")).toBeInTheDocument();
    expect(screen.queryByText("Loading media…")).not.toBeInTheDocument();

    await act(async () => {
      pendingList.resolveJson({
        items: [],
        next_cursor: null,
        has_more: false,
      });
      await pendingList.promise;
      await Promise.resolve();
    });

    expect(screen.getByText("Upload preservado")).toBeInTheDocument();
    expect(screen.queryByText("Loading media…")).not.toBeInTheDocument();
  });

  it("aborts an old mutation and clears items, cursor, file, and edit state when the clinic changes", async () => {
    setRole("super_admin", "clinic-old");
    const oldAsset = asset({ tenant_id: "clinic-old" });
    const staleUpdated = asset({
      tenant_id: "clinic-old",
      display_name: "Stale mutation",
    });
    const newAsset = asset({
      id: "asset-new",
      tenant_id: "clinic-new",
      display_name: "New clinic asset",
      original_filename: "new.pdf",
    });
    const pendingPatch = deferredResponse();
    let patchSignal: AbortSignal | undefined;

    mocks.fetch.mockImplementation(
      (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/v1/media-assets?tenant_id=clinic-old" && !init?.method) {
          return jsonResponse({
            items: [oldAsset],
            next_cursor: "old-cursor",
            has_more: true,
          });
        }
        if (
          url === "/v1/media-assets/asset-1?tenant_id=clinic-old" &&
          init?.method === "PATCH"
        ) {
          patchSignal = init.signal as AbortSignal;
          return pendingPatch.promise;
        }
        if (url === "/v1/media-assets?tenant_id=clinic-new" && !init?.method) {
          return jsonResponse({
            items: [newAsset],
            next_cursor: null,
            has_more: false,
          });
        }
        throw new Error(`Unexpected fetch: ${url}`);
      },
    );

    const view = renderPage();
    expect(
      await screen.findByText("Orientações Pós-Botox"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Load more" }),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("File"), {
      target: {
        files: [new File(["pdf"], "draft.pdf", { type: "application/pdf" })],
      },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Edit: Orientações Pós-Botox" }),
    );
    fireEvent.change(
      screen.getByLabelText("Display name", {
        selector: "input#media-edit-display-name",
      }),
      {
        target: { value: "Pending old edit" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(patchSignal).toBeDefined());

    mocks.auth.viewingClinicId = "clinic-new";
    view.rerender(
      <I18nProvider>
        <Midias />
      </I18nProvider>,
    );

    expect(patchSignal?.aborted).toBe(true);
    expect(screen.queryByText("Orientações Pós-Botox")).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByLabelText("File")).toHaveValue("");
    expect(
      screen.queryByRole("button", { name: "Load more" }),
    ).not.toBeInTheDocument();
    expect(await screen.findByText("New clinic asset")).toBeInTheDocument();

    await act(async () => {
      pendingPatch.resolveJson(staleUpdated);
      await pendingPatch.promise;
      await Promise.resolve();
    });
    expect(screen.queryByText("Stale mutation")).not.toBeInTheDocument();
    expect(screen.getByText("New clinic asset")).toBeInTheDocument();
  });

  it("shows accessible loading, error, and retry states", async () => {
    let attempts = 0;
    mocks.fetch.mockImplementation(() => {
      attempts += 1;
      return attempts === 1
        ? jsonResponse({ detail: { code: "media_storage_unavailable" } }, 503)
        : jsonResponse({ items: [], next_cursor: null, has_more: false });
    });

    renderPage();
    expect(screen.getByRole("status")).toHaveTextContent("Loading media");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The media catalog could not be loaded",
    );
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(
      await screen.findByText("No media has been uploaded for this clinic."),
    ).toBeInTheDocument();
    expect(attempts).toBe(2);
  });
});

describe("<Midias /> role and locale guards", () => {
  it("denies investor direct access without making a catalog request", () => {
    setRole("investor", "investor-clinic");
    renderPage();

    expect(screen.getByTestId("media-access-denied")).toHaveTextContent(
      "Media access unavailable",
    );
    expect(screen.queryByTestId("media-catalog")).not.toBeInTheDocument();
    expect(mocks.fetch).not.toHaveBeenCalled();
  });

  it.each(["builder", "super_admin"] as const)(
    "requires a selected clinic for %s direct access",
    (role) => {
      setRole(role, "");
      renderPage();

      expect(screen.getByTestId("media-select-clinic")).toHaveTextContent(
        "Select a clinic",
      );
      expect(mocks.fetch).not.toHaveBeenCalled();
    },
  );

  it("renders the Portuguese catalog and explicit no-runtime notice", async () => {
    localStorage.setItem("ns_locale", "pt");
    mocks.fetch.mockImplementation(() =>
      jsonResponse({ items: [], next_cursor: null, has_more: false }),
    );

    renderPage();

    expect(await screen.findByText("Mídias & Ativos")).toBeInTheDocument();
    expect(
      screen.getByText(/não enviam mídias nem executam ações em runtime/i),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Arquivo")).toBeInTheDocument();
    expect(screen.getByText("Biblioteca de mídias")).toBeInTheDocument();
  });
});
