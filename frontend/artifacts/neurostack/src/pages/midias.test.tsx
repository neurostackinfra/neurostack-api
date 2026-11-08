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
    original_filename: "Orientações Pós-Botox.pdf",
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

function sizedFile(name: string, type: string, size: number) {
  const file = new File(["media"], name, { type });
  // jsdom keeps the real byte length; the page only ever reads ``size``.
  Object.defineProperty(file, "size", { value: size });
  return file;
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

const EMPTY_PAGE = { items: [], next_cursor: null, has_more: false };

function submitUpload() {
  fireEvent.submit(
    screen.getByRole("button", { name: "Upload media" }).closest("form")!,
  );
}

function pickFile(file: File) {
  fireEvent.change(screen.getByLabelText("File"), {
    target: { files: [file] },
  });
}

describe("<Midias /> catalog operations", () => {
  it("uses the clinic-admin tenant implicitly and uploads one file name with its locked extension", async () => {
    const existing = asset();
    const created = asset({
      id: "asset-uploaded",
      display_name: "Boas-vindas",
      original_filename: "Boas-vindas.mp3",
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

    const row = await screen.findByTestId("media-item-asset-1");
    expect(within(row).getByText("Orientações Pós-Botox.pdf")).toBeInTheDocument();
    expect(within(row).getAllByText("PDF")).toHaveLength(2);
    expect(mocks.fetch).toHaveBeenCalledWith(
      "/v1/media-assets",
      expect.objectContaining({
        headers: { Authorization: "Bearer media-token" },
      }),
    );
    // The catalog-only banner is gone; the trigger asks for the sending context.
    expect(screen.queryByText(/Catalog metadata only/)).not.toBeInTheDocument();
    expect(screen.queryByText(/do not send media/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Usage trigger")).toHaveAttribute(
      "placeholder",
      "Describe the sending context.",
    );
    expect(screen.queryByLabelText("Display name")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Original filename")).not.toBeInTheDocument();

    pickFile(
      new File(["ID3-media"], "boas-vindas.mp3", { type: "audio/mpeg" }),
    );
    // The type is filled from the picked file and the name from its stem.
    expect(screen.getByLabelText("File type")).toHaveValue("audio");
    expect(screen.getByLabelText("File name")).toHaveValue("boas-vindas");
    expect(screen.getByTestId("media-file-extension")).toHaveTextContent(
      ".mp3",
    );
    fireEvent.change(screen.getByLabelText("File name"), {
      target: { value: "  Boas-vindas  " },
    });
    fireEvent.change(screen.getByLabelText("Usage trigger"), {
      target: { value: "Na primeira conversa" },
    });
    fireEvent.click(
      screen.getByRole("switch", { name: "AI Automatic Suggestion" }),
    );
    const uploadButton = screen.getByRole("button", { name: "Upload media" });
    expect(uploadButton).toBeEnabled();
    submitUpload();

    await waitFor(() => expect(uploadBody).toBeDefined());
    expect(
      await screen.findByText("Media uploaded successfully."),
    ).toBeInTheDocument();
    expect(uploadBody?.get("file")).toBeInstanceOf(File);
    expect(uploadBody?.get("display_name")).toBe("Boas-vindas");
    expect(uploadBody?.get("original_filename")).toBe("Boas-vindas.mp3");
    expect(uploadBody?.get("usage_trigger")).toBe("Na primeira conversa");
    expect(uploadBody?.get("auto_suggest")).toBe("true");
    const uploadedRow = screen.getByTestId("media-item-asset-uploaded");
    expect(within(uploadedRow).getByText("Boas-vindas.mp3")).toBeInTheDocument();
    expect(within(uploadedRow).getByText("Audio")).toBeInTheDocument();
    expect(within(uploadedRow).getByText("MP3")).toBeInTheDocument();
    expect(screen.getByLabelText("File")).toHaveValue("");
    expect(screen.getByLabelText("File type")).toHaveValue("");
    expect(screen.getByLabelText("File name")).toHaveValue("");
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
        original_filename: "Foto da recepção.png",
        category: "image",
        mime_type: "image/png",
        usage_trigger: null,
        auto_suggest: false,
      });
      const updated = asset({
        tenant_id: "clinic-42",
        display_name: "Guia revisado",
        original_filename: "Guia revisado.pdf",
        usage_trigger: "Quando o paciente pedir",
        auto_suggest: false,
      });
      const uploaded = asset({
        id: "asset-uploaded",
        tenant_id: "clinic-42",
        display_name: "Áudio de boas-vindas",
        original_filename: "Áudio de boas-vindas.mp3",
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
              original_filename: "Guia revisado.pdf",
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
        await screen.findByText("Orientações Pós-Botox.pdf"),
      ).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Load more" }));
      expect(await screen.findByText("Foto da recepção.png")).toBeInTheDocument();

      pickFile(
        new File(["ID3-media"], "boas-vindas.mp3", { type: "audio/mpeg" }),
      );
      fireEvent.change(screen.getByLabelText("File name"), {
        target: { value: "Áudio de boas-vindas" },
      });
      submitUpload();
      expect(
        await screen.findByText("Media uploaded successfully."),
      ).toBeInTheDocument();

      fireEvent.click(
        screen.getByRole("button", { name: "Edit: Orientações Pós-Botox.pdf" }),
      );
      const dialog = screen.getByRole("dialog");
      expect(within(dialog).getByLabelText("File type")).toHaveValue("PDF");
      expect(within(dialog).getByLabelText("File type")).toBeDisabled();
      expect(within(dialog).getByLabelText("File name")).toHaveValue(
        "Orientações Pós-Botox",
      );
      fireEvent.change(within(dialog).getByLabelText("File name"), {
        target: { value: "Guia revisado" },
      });
      fireEvent.change(within(dialog).getByLabelText("Usage trigger"), {
        target: { value: "Quando o paciente pedir" },
      });
      fireEvent.click(
        within(dialog).getByRole("switch", { name: "AI Automatic Suggestion" }),
      );
      fireEvent.click(
        within(dialog).getByRole("button", { name: "Save changes" }),
      );

      expect(
        await screen.findByText("Media metadata updated."),
      ).toBeInTheDocument();
      expect(screen.getByText("Guia revisado.pdf")).toBeInTheDocument();

      fireEvent.click(
        screen.getByRole("button", { name: "Download: Guia revisado.pdf" }),
      );
      await waitFor(() =>
        expect(URL.createObjectURL).toHaveBeenCalledWith(expect.any(Blob)),
      );
      expect(HTMLAnchorElement.prototype.click).toHaveBeenCalled();

      fireEvent.click(
        screen.getByRole("button", { name: "Delete: Guia revisado.pdf" }),
      );
      expect(
        screen.getByRole("alertdialog", {
          name: 'Do you really want to permanently delete "Guia revisado.pdf"?',
        }),
      ).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Yes" }));
      expect(await screen.findByText("Media deleted.")).toBeInTheDocument();
      expect(screen.queryByText("Guia revisado.pdf")).not.toBeInTheDocument();

      // Group 6: one read of the clinic's credit allocation (hidden here,
      // the route is not mocked); every other call is a media operation.
      const allCalls = mocks.fetch.mock.calls.map(([input]) => String(input));
      expect(allCalls.filter((url) => url.startsWith("/v1/credits/"))).toEqual([
        "/v1/credits/reservations?tenant_id=clinic-42",
      ]);
      const mediaCalls = allCalls.filter((url) => !url.startsWith("/v1/credits/"));
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

  it("asks for the first page again when an upload lands while it is loading", async () => {
    const pendingList = deferredResponse();
    const existing = asset();
    const created = asset({
      id: "asset-race-upload",
      display_name: "Upload preservado",
      original_filename: "Upload preservado.pdf",
    });
    let listCalls = 0;

    mocks.fetch.mockImplementation(
      (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/v1/media-assets" && init?.method === "POST") {
          return jsonResponse(created, 201);
        }
        if (url === "/v1/media-assets" && !init?.method) {
          listCalls += 1;
          // The first answer predates the upload; the second one includes it.
          return listCalls === 1
            ? pendingList.promise
            : jsonResponse({
                items: [created, existing],
                next_cursor: null,
                has_more: false,
              });
        }
        throw new Error(`Unexpected fetch: ${url}`);
      },
    );

    renderPage();
    expect(screen.getByRole("status")).toHaveTextContent("Loading media");

    pickFile(new File(["pdf"], "preservado.pdf", { type: "application/pdf" }));
    fireEvent.change(screen.getByLabelText("File name"), {
      target: { value: "Upload preservado" },
    });
    submitUpload();
    expect(
      await screen.findByText("Media uploaded successfully."),
    ).toBeInTheDocument();

    await act(async () => {
      pendingList.resolveJson({
        items: [existing],
        next_cursor: null,
        has_more: false,
      });
      await pendingList.promise;
    });

    expect(await screen.findByText("Upload preservado.pdf")).toBeInTheDocument();
    expect(screen.getByText("Orientações Pós-Botox.pdf")).toBeInTheDocument();
    expect(screen.queryByText("Loading media…")).not.toBeInTheDocument();
    expect(listCalls).toBe(2);
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
      original_filename: "New clinic asset.pdf",
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
      await screen.findByText("Orientações Pós-Botox.pdf"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Load more" }),
    ).toBeInTheDocument();

    pickFile(new File(["pdf"], "draft.pdf", { type: "application/pdf" }));
    fireEvent.click(
      screen.getByRole("button", { name: "Edit: Orientações Pós-Botox.pdf" }),
    );
    fireEvent.change(
      screen.getByLabelText("File name", {
        selector: "input#media-edit-file-name",
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
    expect(
      screen.queryByText("Orientações Pós-Botox.pdf"),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByLabelText("File")).toHaveValue("");
    expect(screen.getByLabelText("File type")).toHaveValue("");
    expect(screen.getByLabelText("File name")).toHaveValue("");
    expect(
      screen.queryByRole("button", { name: "Load more" }),
    ).not.toBeInTheDocument();
    expect(await screen.findByText("New clinic asset.pdf")).toBeInTheDocument();

    await act(async () => {
      pendingPatch.resolveJson(staleUpdated);
      await pendingPatch.promise;
      await Promise.resolve();
    });
    expect(screen.queryByText(/Stale mutation/)).not.toBeInTheDocument();
    expect(screen.getByText("New clinic asset.pdf")).toBeInTheDocument();
  });

  it("shows accessible loading, error, and retry states", async () => {
    let attempts = 0;
    mocks.fetch.mockImplementation(() => {
      attempts += 1;
      return attempts === 1
        ? jsonResponse({ detail: { code: "media_storage_unavailable" } }, 503)
        : jsonResponse(EMPTY_PAGE);
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

describe("<Midias /> file name, type and format", () => {
  beforeEach(() => {
    mocks.fetch.mockImplementation(() => jsonResponse(EMPTY_PAGE));
  });

  it.each([
    ["image/jpeg", "foto.jpg", "Image", "JPG"],
    ["image/jpeg", "foto.jpeg", "Image", "JPEG"],
    ["image/png", "antesedepois.png", "Image", "PNG"],
    ["application/pdf", "guia.pdf", "PDF", "PDF"],
    ["video/mp4", "clipe.mp4", "Video", "MP4"],
    ["audio/mpeg", "voz.mp3", "Audio", "MP3"],
    ["audio/ogg", "voz.ogg", "Audio", "OGG/OPUS"],
    ["audio/ogg", "voz.opus", "Audio", "OGG/OPUS"],
    ["audio/mp4", "voz.m4a", "Audio", "M4A"],
  ])(
    "lists %s stored as %s with its type and format",
    async (mime, original, type, format) => {
      const category = (
        { Image: "image", PDF: "pdf", Video: "video", Audio: "audio" } as const
      )[type];
      const stem = original.replace(/\.[^.]+$/, "");
      mocks.fetch.mockImplementation(() =>
        jsonResponse({
          items: [
            asset({
              display_name: stem,
              original_filename: original,
              category,
              mime_type: mime,
            }),
          ],
          next_cursor: null,
          has_more: false,
        }),
      );

      renderPage();
      const row = await screen.findByTestId("media-item-asset-1");
      const cells = within(row).getAllByRole("cell");
      expect(cells[0]).toHaveTextContent(original);
      expect(cells[1]).toHaveTextContent(type);
      expect(cells[2]).toHaveTextContent(format);
      for (const action of ["Download", "Edit", "Delete"]) {
        expect(
          within(row).getByRole("button", { name: `${action}: ${original}` }),
        ).toBeInTheDocument();
      }
    },
  );

  it("shows a legacy row by the name it downloads and is sent with, and edits that name", async () => {
    let patchBody: unknown;
    const legacy = asset({
      display_name: "Antes e depois",
      original_filename: "IMG_1234.PNG",
      category: "image",
      mime_type: "image/png",
    });
    mocks.fetch.mockImplementation(
      (_input: string | URL | Request, init?: RequestInit) => {
        if (init?.method === "PATCH") {
          patchBody = JSON.parse(String(init.body));
          return jsonResponse({ ...legacy, ...(patchBody as object) });
        }
        return jsonResponse({ items: [legacy], next_cursor: null, has_more: false });
      },
    );

    renderPage();
    const row = await screen.findByTestId("media-item-asset-1");
    expect(within(row).getAllByRole("cell")[0]).toHaveTextContent("IMG_1234.PNG");
    expect(within(row).queryByText(/Antes e depois/)).not.toBeInTheDocument();

    fireEvent.click(within(row).getByRole("button", { name: "Edit: IMG_1234.PNG" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByLabelText("File name")).toHaveValue("IMG_1234");
    expect(dialog).toHaveTextContent(".png");
    fireEvent.click(within(dialog).getByRole("button", { name: "Save changes" }));

    expect(await screen.findByText("Media metadata updated.")).toBeInTheDocument();
    expect(patchBody).toEqual(
      expect.objectContaining({
        display_name: "IMG_1234",
        original_filename: "IMG_1234.png",
      }),
    );
  });

  it("keeps the extension locked to the real format whatever name is typed", async () => {
    let uploadBody: FormData | undefined;
    mocks.fetch.mockImplementation(
      (_input: string | URL | Request, init?: RequestInit) => {
        if (init?.method === "POST") {
          uploadBody = init.body as FormData;
          return jsonResponse(asset({ id: "asset-new" }), 201);
        }
        return jsonResponse(EMPTY_PAGE);
      },
    );

    renderPage();
    await screen.findByText("No media has been uploaded for this clinic.");
    pickFile(sizedFile("IMG_9.JPEG", "image/jpeg", 1_000));
    expect(screen.getByTestId("media-file-extension")).toHaveTextContent(
      ".jpeg",
    );
    // A typed extension of the same format is not doubled.
    fireEvent.change(screen.getByLabelText("File name"), {
      target: { value: "antesedepois.jpeg" },
    });
    submitUpload();

    await waitFor(() => expect(uploadBody).toBeDefined());
    expect(uploadBody?.get("display_name")).toBe("antesedepois");
    expect(uploadBody?.get("original_filename")).toBe("antesedepois.jpeg");
  });

  it("cannot rename a file into another format", async () => {
    let uploadBody: FormData | undefined;
    mocks.fetch.mockImplementation(
      (_input: string | URL | Request, init?: RequestInit) => {
        if (init?.method === "POST") {
          uploadBody = init.body as FormData;
          return jsonResponse(asset({ id: "asset-new" }), 201);
        }
        return jsonResponse(EMPTY_PAGE);
      },
    );

    renderPage();
    await screen.findByText("No media has been uploaded for this clinic.");
    pickFile(sizedFile("guia.pdf", "application/pdf", 1_000));
    fireEvent.change(screen.getByLabelText("File name"), {
      target: { value: "foto.png" },
    });
    submitUpload();

    await waitFor(() => expect(uploadBody).toBeDefined());
    expect(uploadBody?.get("display_name")).toBe("foto.png");
    expect(uploadBody?.get("original_filename")).toBe("foto.png.pdf");
  });

  it("keeps a typed name when another file is picked, but refreshes one it filled in", async () => {
    renderPage();
    await screen.findByText("No media has been uploaded for this clinic.");
    const name = screen.getByLabelText("File name");

    pickFile(sizedFile("primeiro.pdf", "application/pdf", 1_000));
    expect(name).toHaveValue("primeiro");
    pickFile(sizedFile("segundo.pdf", "application/pdf", 1_000));
    expect(name).toHaveValue("segundo");

    fireEvent.change(name, { target: { value: "Guia da clínica" } });
    pickFile(sizedFile("terceiro.pdf", "application/pdf", 1_000));
    expect(name).toHaveValue("Guia da clínica");
  });

  it("cannot upload until the name has a stem", async () => {
    renderPage();
    await screen.findByText("No media has been uploaded for this clinic.");
    pickFile(sizedFile("guia.pdf", "application/pdf", 1_000));
    const upload = screen.getByRole("button", { name: "Upload media" });
    expect(upload).toBeEnabled();

    for (const value of ["", "   ", ".pdf"]) {
      fireEvent.change(screen.getByLabelText("File name"), {
        target: { value },
      });
      expect(upload).toBeDisabled();
    }
  });

  it("stores a decomposed name composed, without trailing dots, and trims a long suggestion", async () => {
    const bodies: FormData[] = [];
    mocks.fetch.mockImplementation(
      (_input: string | URL | Request, init?: RequestInit) => {
        if (init?.method === "POST") {
          bodies.push(init.body as FormData);
          return jsonResponse(asset({ id: `asset-${bodies.length}` }), 201);
        }
        return jsonResponse(EMPTY_PAGE);
      },
    );

    renderPage();
    await screen.findByText("No media has been uploaded for this clinic.");
    // macOS hands this name over decomposed: "a" + U+0301.
    pickFile(sizedFile("Prontua\u0301rio.pdf", "application/pdf", 1_000));
    expect(screen.getByLabelText("File name")).toHaveValue("Prontu\u00e1rio");
    submitUpload();
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0].get("display_name")).toBe("Prontu\u00e1rio");
    expect(bodies[0].get("original_filename")).toBe("Prontu\u00e1rio.pdf");

    await screen.findByText("Media uploaded successfully.");
    pickFile(sizedFile("guia.pdf", "application/pdf", 1_000));
    fireEvent.change(screen.getByLabelText("File name"), {
      target: { value: "foto . " },
    });
    submitUpload();
    await waitFor(() => expect(bodies).toHaveLength(2));
    expect(bodies[1].get("display_name")).toBe("foto");
    expect(bodies[1].get("original_filename")).toBe("foto.pdf");

    await waitFor(() =>
      expect(screen.getByLabelText("File name")).toHaveValue(""),
    );
    // A pasted name can be decomposed too.
    pickFile(sizedFile("guia.pdf", "application/pdf", 1_000));
    fireEvent.change(screen.getByLabelText("File name"), {
      target: { value: "Relato\u0301rio" },
    });
    submitUpload();
    await waitFor(() => expect(bodies).toHaveLength(3));
    expect(bodies[2].get("display_name")).toBe("Relat\u00f3rio");
    expect(bodies[2].get("original_filename")).toBe("Relat\u00f3rio.pdf");

    await waitFor(() =>
      expect(screen.getByLabelText("File name")).toHaveValue(""),
    );
    pickFile(sizedFile(`${"a".repeat(240)}.pdf`, "application/pdf", 1_000));
    expect(screen.getByLabelText("File name")).toHaveValue("a".repeat(200));
  });

  it("caps an edit prefill at the 200 characters display_name accepts", async () => {
    let patchBody: { display_name?: string; original_filename?: string } = {};
    const long = asset({ original_filename: `${"b".repeat(230)}.pdf` });
    mocks.fetch.mockImplementation(
      (_input: string | URL | Request, init?: RequestInit) => {
        if (init?.method === "PATCH") {
          patchBody = JSON.parse(String(init.body));
          return jsonResponse({ ...long, ...patchBody });
        }
        return jsonResponse({ items: [long], next_cursor: null, has_more: false });
      },
    );

    renderPage();
    fireEvent.click(
      await screen.findByRole("button", { name: `Edit: ${"b".repeat(230)}.pdf` }),
    );
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByLabelText("File name")).toHaveValue("b".repeat(200));
    fireEvent.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await screen.findByText("Media metadata updated.");
    expect(patchBody.display_name).toHaveLength(200);
    expect(patchBody.original_filename).toBe(`${"b".repeat(200)}.pdf`);
  });

  it("puts a name with $ patterns into the delete question verbatim", async () => {
    mocks.fetch.mockImplementation(() =>
      jsonResponse({
        items: [asset({ original_filename: "Tabela R$& promo $$.pdf" })],
        next_cursor: null,
        has_more: false,
      }),
    );

    renderPage();
    fireEvent.click(
      await screen.findByRole("button", { name: "Delete: Tabela R$& promo $$.pdf" }),
    );
    expect(screen.getByRole("alertdialog")).toHaveTextContent(
      'Do you really want to permanently delete "Tabela R$& promo $$.pdf"?',
    );
  });

  it("narrows the picker to the chosen type and refuses a file of another type", async () => {
    renderPage();
    const input = await screen.findByLabelText("File");
    expect(input).toHaveAttribute(
      "accept",
      expect.stringContaining("application/pdf"),
    );

    fireEvent.change(screen.getByLabelText("File type"), {
      target: { value: "image" },
    });
    expect(input).toHaveAttribute(
      "accept",
      "image/jpeg,.jpg,.jpeg,image/png,.png",
    );
    expect(
      screen.getByText("Image: JPG, JPEG, or PNG, up to 5 MB."),
    ).toBeInTheDocument();

    pickFile(sizedFile("guia.pdf", "application/pdf", 1_000));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This file is not of the selected type.",
    );
    expect(input).toHaveValue("");
    expect(screen.getByLabelText("File type")).toHaveValue("image");
    expect(screen.getByRole("button", { name: "Upload media" })).toBeDisabled();
    expect(mocks.fetch).toHaveBeenCalledTimes(1);
  });

  it("drops a picked file when the type is changed to one it is not", async () => {
    renderPage();
    await screen.findByText("No media has been uploaded for this clinic.");
    pickFile(sizedFile("voz.mp3", "audio/mpeg", 1_000));
    expect(screen.getByLabelText("File type")).toHaveValue("audio");
    expect(screen.getByRole("button", { name: "Upload media" })).toBeEnabled();

    fireEvent.change(screen.getByLabelText("File type"), {
      target: { value: "video" },
    });
    expect(screen.getByLabelText("File")).toHaveValue("");
    expect(screen.queryByTestId("media-file-extension")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Upload media" })).toBeDisabled();
  });

  it("declares the resolved format for a file the system left untyped", async () => {
    let uploadBody: FormData | undefined;
    mocks.fetch.mockImplementation(
      (_input: string | URL | Request, init?: RequestInit) => {
        if (init?.method === "POST") {
          uploadBody = init.body as FormData;
          return jsonResponse(asset({ id: "asset-new" }), 201);
        }
        return jsonResponse(EMPTY_PAGE);
      },
    );

    renderPage();
    await screen.findByText("No media has been uploaded for this clinic.");
    pickFile(sizedFile("voz.opus", "", 2_000));
    expect(screen.getByLabelText("File type")).toHaveValue("audio");
    submitUpload();

    await waitFor(() => expect(uploadBody).toBeDefined());
    const sent = uploadBody?.get("file") as File;
    expect(sent.type).toBe("audio/ogg");
    expect(sent.name).toBe("voz.opus");
    expect(uploadBody?.get("original_filename")).toBe("voz.opus");
  });
});

// The server is authoritative on both the size and the format; these cover the
// client-side mirror of src/services/media_assets.py MEDIA_CATEGORY_MAX_BYTES
// and the mapping from its documented error codes to actionable prose.
describe("<Midias /> per-category limits and upload errors", () => {
  beforeEach(() => {
    mocks.fetch.mockImplementation(() => jsonResponse(EMPTY_PAGE));
  });

  it("accepts and advertises OGG-Opus alongside the original formats", async () => {
    const created = asset({
      id: "asset-ogg",
      display_name: "Áudio de boas-vindas",
      original_filename: "Áudio de boas-vindas.ogg",
      category: "audio",
      mime_type: "audio/ogg",
    });
    let uploadBody: FormData | undefined;
    mocks.fetch.mockImplementation(
      (_input: string | URL | Request, init?: RequestInit) => {
        if (init?.method === "POST") {
          uploadBody = init.body as FormData;
          return jsonResponse(created, 201);
        }
        return jsonResponse(EMPTY_PAGE);
      },
    );

    renderPage();
    const input = await screen.findByLabelText("File");
    for (const token of ["audio/ogg", ".ogg", ".opus", "audio/mp4", ".m4a"]) {
      expect(input).toHaveAttribute("accept", expect.stringContaining(token));
    }
    expect(
      screen.getByText(/JPG, JPEG, PNG, PDF, MP4, MP3, M4A, OGG\/OPUS/, {
        selector: "div",
      }),
    ).toBeInTheDocument();

    // WhatsApp and ffmpeg label Opus with a codecs parameter.
    fireEvent.change(input, {
      target: {
        files: [sizedFile("voz.ogg", "audio/ogg; codecs=opus", 2_000_000)],
      },
    });
    fireEvent.change(screen.getByLabelText("File name"), {
      target: { value: "Áudio de boas-vindas" },
    });
    submitUpload();

    await waitFor(() => expect(uploadBody).toBeDefined());
    expect((uploadBody?.get("file") as File).type).toBe(
      "audio/ogg; codecs=opus",
    );
    expect(
      await screen.findByText("Media uploaded successfully."),
    ).toBeInTheDocument();
    expect(screen.getByText("Áudio de boas-vindas.ogg")).toBeInTheDocument();
  });

  it.each([
    ["image", "foto.png", "image/png", 5_000_001, "The image exceeds the 5 MB limit."],
    ["pdf", "guia.pdf", "application/pdf", 20_000_001, "The PDF exceeds the 20 MB limit."],
    ["audio", "voz.ogg", "audio/ogg", 16_000_001, "The audio exceeds the 16 MB limit."],
    ["video", "clipe.mp4", "video/mp4", 16_000_001, "The video exceeds the 16 MB limit."],
    ["m4a audio", "voz.m4a", "audio/x-m4a", 16_000_001, "The audio exceeds the 16 MB limit."],
  ])(
    "blocks a %s one byte over its own limit before any upload request",
    async (_category, name, type, size, message) => {
      renderPage();
      const input = await screen.findByLabelText("File");
      fireEvent.change(input, { target: { files: [sizedFile(name, type, size)] } });

      expect(await screen.findByRole("alert")).toHaveTextContent(message);
      expect(input).toHaveValue("");
      expect(
        screen.getByRole("button", { name: "Upload media" }),
      ).toBeDisabled();
      expect(mocks.fetch).toHaveBeenCalledTimes(1);
    },
  );

  it.each([
    ["pdf", "guia.pdf", "application/pdf", 19_999_999],
    ["audio", "voz.mp3", "audio/mpeg", 15_999_999],
  ])(
    "keeps a %s that a flat image-sized limit would have rejected",
    async (category, name, type, size) => {
      renderPage();
      const input = await screen.findByLabelText("File");
      fireEvent.change(input, { target: { files: [sizedFile(name, type, size)] } });

      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(screen.getByLabelText("File type")).toHaveValue(category);
      expect(screen.getByLabelText("File name")).toHaveValue(
        name.replace(/\.[^.]+$/, ""),
      );
    },
  );

  it.each([
    ["an untyped file without an extension", "gravação", ""],
    ["a type the server does not accept", "voz.wav", "audio/wav"],
  ])(
    "refuses %s in the browser with the format message",
    async (_label, name, type) => {
      renderPage();
      const input = await screen.findByLabelText("File");
      fireEvent.change(input, {
        target: { files: [sizedFile(name, type, 1_000)] },
      });

      expect(await screen.findByRole("alert")).toHaveTextContent(
        "This format is not accepted",
      );
      expect(input).toHaveValue("");
      expect(screen.getByLabelText("File type")).toHaveValue("");
      expect(mocks.fetch).toHaveBeenCalledTimes(1);
    },
  );

  it.each([
    ["audio/x-m4a", "audio/x-m4a"],
    ["audio/m4a", "audio/m4a"],
    ["audio/mp4", "audio/mp4"],
    // Left untyped by the system: re-declared with the canonical MIME.
    ["", "audio/mp4"],
  ])(
    "uploads an .m4a declared as %j as audio, keeping its extension",
    async (declared, sent) => {
      let uploadBody: FormData | undefined;
      mocks.fetch.mockImplementation(
        (_input: string | URL | Request, init?: RequestInit) => {
          if (init?.method === "POST") {
            uploadBody = init.body as FormData;
            return jsonResponse(
              asset({
                id: "asset-m4a",
                display_name: "recado",
                original_filename: "recado.m4a",
                category: "audio",
                mime_type: "audio/mp4",
              }),
              201,
            );
          }
          return jsonResponse(EMPTY_PAGE);
        },
      );

      renderPage();
      await screen.findByText("No media has been uploaded for this clinic.");
      pickFile(sizedFile("recado.m4a", declared, 3_000_000));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(screen.getByLabelText("File type")).toHaveValue("audio");
      expect(screen.getByTestId("media-file-extension")).toHaveTextContent(".m4a");
      submitUpload();

      await waitFor(() => expect(uploadBody).toBeDefined());
      expect((uploadBody?.get("file") as File).type).toBe(sent);
      expect(uploadBody?.get("original_filename")).toBe("recado.m4a");
      const row = await screen.findByTestId("media-item-asset-m4a");
      expect(within(row).getAllByRole("cell")[2]).toHaveTextContent("M4A");
    },
  );

  it.each([
    [413, "media_size_exceeded", "exceeds the limit for its type"],
    [413, "media_request_size_exceeded", "exceeds the limit for its type"],
    [422, "media_type_unsupported", "This format is not accepted"],
    [422, "media_type_mismatch", "This format is not accepted"],
    [409, "media_storage_cap_exceeded", "media storage is full"],
    [402, "credit_pending", "media credits do not cover this upload"],
    [402, "credit_category_exhausted", "media credits do not cover this upload"],
    [503, "media_concurrency_exceeded", "busy right now"],
  ])(
    "explains a %s %s instead of one generic failure",
    async (status, code, expected) => {
      mocks.fetch.mockImplementation(
        (_input: string | URL | Request, init?: RequestInit) =>
          init?.method === "POST"
            ? jsonResponse({ detail: { code } }, status)
            : jsonResponse(EMPTY_PAGE),
      );

      renderPage();
      fireEvent.change(await screen.findByLabelText("File"), {
        target: { files: [sizedFile("arquivo.pdf", "application/pdf", 1_000)] },
      });
      submitUpload();

      expect(await screen.findByRole("alert")).toHaveTextContent(expected);
      expect(
        screen.queryByText(/Check the file and try again/),
      ).not.toBeInTheDocument();
      // A rejected upload keeps the form filled so the user can retry.
      expect(screen.getByLabelText("File name")).toHaveValue("arquivo");
      expect(screen.getByLabelText("File type")).toHaveValue("pdf");
    },
  );

  it.each([
    ["an undocumented code", { detail: { code: "media_storage_conflict" } }],
    ["a body that is not the envelope", { message: "gateway" }],
  ])("falls back to the generic message for %s", async (_label, body) => {
    mocks.fetch.mockImplementation(
      (_input: string | URL | Request, init?: RequestInit) =>
        init?.method === "POST"
          ? jsonResponse(body, 409)
          : jsonResponse(EMPTY_PAGE),
    );

    renderPage();
    fireEvent.change(await screen.findByLabelText("File"), {
      target: { files: [sizedFile("arquivo.pdf", "application/pdf", 1_000)] },
    });
    submitUpload();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The media could not be uploaded. Check the file and try again.",
    );
  });

  it("states every limit in Portuguese too", async () => {
    localStorage.setItem("ns_locale", "pt");
    renderPage();
    const input = await screen.findByLabelText("Arquivo");
    expect(
      screen.getByText(/5 MB para imagem, 20 MB para PDF, 16 MB para áudio/),
    ).toBeInTheDocument();

    fireEvent.change(input, {
      target: { files: [sizedFile("foto.png", "image/png", 5_000_001)] },
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "A imagem ultrapassa o limite de 5 MB.",
    );
  });
});

describe("<Midias /> type filter and name search", () => {
  const image = asset({
    id: "asset-image",
    display_name: "antesedepois",
    original_filename: "antesedepois.png",
    category: "image",
    mime_type: "image/png",
  });

  function routeList(
    pages: Record<string, unknown>,
    onPost?: (init: RequestInit) => Promise<Response>,
  ) {
    mocks.fetch.mockImplementation(
      (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (init?.method === "POST" && onPost) return onPost(init);
        if (url in pages) return jsonResponse(pages[url]);
        throw new Error(`Unexpected fetch: ${url}`);
      },
    );
  }

  it("asks the server for one type and shows the filtered empty state", async () => {
    routeList({
      "/v1/media-assets": { items: [asset(), image], next_cursor: null, has_more: false },
      "/v1/media-assets?category=video": EMPTY_PAGE,
      "/v1/media-assets?category=image": { items: [image], next_cursor: null, has_more: false },
    });

    renderPage();
    expect(await screen.findByText("antesedepois.png")).toBeInTheDocument();

    const filter = screen.getByLabelText("Filter by file type");
    expect(
      within(filter).getAllByRole("option").map((option) => option.textContent),
    ).toEqual(["All types", "Image", "Video", "Audio", "PDF"]);
    fireEvent.change(filter, { target: { value: "video" } });
    expect(await screen.findByText("No media matches these filters.")).toBeInTheDocument();
    expect(screen.queryByText("No media has been uploaded for this clinic.")).not.toBeInTheDocument();

    fireEvent.change(filter, { target: { value: "image" } });
    expect(await screen.findByText("antesedepois.png")).toBeInTheDocument();
    expect(screen.queryByText("Orientações Pós-Botox.pdf")).not.toBeInTheDocument();
  });

  it("searches the name on the server after the user stops typing, and pages with the same filters", async () => {
    setRole("builder", "clinic-42");
    const second = asset({
      id: "asset-image-2",
      display_name: "antes do tratamento",
      original_filename: "antes do tratamento.png",
      category: "image",
      mime_type: "image/png",
    });
    routeList({
      "/v1/media-assets?tenant_id=clinic-42": EMPTY_PAGE,
      "/v1/media-assets?tenant_id=clinic-42&category=image": EMPTY_PAGE,
      "/v1/media-assets?tenant_id=clinic-42&category=image&name=antes": {
        items: [image],
        next_cursor: "filtered-cursor",
        has_more: true,
      },
      "/v1/media-assets?tenant_id=clinic-42&category=image&name=antes&cursor=filtered-cursor": {
        items: [second],
        next_cursor: null,
        has_more: false,
      },
    });

    renderPage();
    await screen.findByText("No media has been uploaded for this clinic.");
    fireEvent.change(screen.getByLabelText("Filter by file type"), {
      target: { value: "image" },
    });
    const search = screen.getByLabelText("Search name");
    // Typed 50 ms apart: faster than the pause, slower than one tick.
    for (const value of ["a", "an", "antes  "]) {
      fireEvent.change(search, { target: { value } });
      await act(() => new Promise((resolve) => setTimeout(resolve, 50)));
    }
    expect(await screen.findByText("antesedepois.png")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("antes do tratamento.png")).toBeInTheDocument();

    const listCalls = mocks.fetch.mock.calls
      .map(([input]) => String(input))
      .filter((url) => url.startsWith("/v1/media-assets"));
    // Each keystroke inside the pause is not a request of its own.
    expect(listCalls.filter((url) => url.includes("name="))).toEqual([
      "/v1/media-assets?tenant_id=clinic-42&category=image&name=antes",
      "/v1/media-assets?tenant_id=clinic-42&category=image&name=antes&cursor=filtered-cursor",
    ]);
  });

  it("does not show an upload the active filter would hide, and the server shows it once cleared", async () => {
    const created = asset({
      id: "asset-created",
      display_name: "guia",
      original_filename: "guia.pdf",
    });
    let uploaded = false;
    mocks.fetch.mockImplementation(
      (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (init?.method === "POST") {
          uploaded = true;
          return jsonResponse(created, 201);
        }
        if (url === "/v1/media-assets") {
          return jsonResponse({
            items: uploaded ? [created, image] : [image],
            next_cursor: null,
            has_more: false,
          });
        }
        if (url === "/v1/media-assets?category=image") {
          return jsonResponse({ items: [image], next_cursor: null, has_more: false });
        }
        throw new Error(`Unexpected fetch: ${url}`);
      },
    );

    renderPage();
    expect(await screen.findByText("antesedepois.png")).toBeInTheDocument();
    const filter = screen.getByLabelText("Filter by file type");
    fireEvent.change(filter, { target: { value: "image" } });
    await waitFor(() =>
      expect(
        mocks.fetch.mock.calls.map(([input]) => String(input)),
      ).toContain("/v1/media-assets?category=image"),
    );
    expect(await screen.findByText("antesedepois.png")).toBeInTheDocument();

    pickFile(sizedFile("guia.pdf", "application/pdf", 1_000));
    submitUpload();
    expect(await screen.findByText("Media uploaded successfully.")).toBeInTheDocument();
    expect(screen.queryByText("guia.pdf")).not.toBeInTheDocument();
    expect(screen.getByText("antesedepois.png")).toBeInTheDocument();

    fireEvent.change(filter, { target: { value: "" } });
    expect(await screen.findByText("guia.pdf")).toBeInTheDocument();
    expect(screen.getByText("antesedepois.png")).toBeInTheDocument();
  });

  it("keeps the server's filtered page when an upload lands before it", async () => {
    const pendingUpload = deferredResponse();
    const pendingImages = deferredResponse();
    const older = asset({
      id: "asset-image-old",
      display_name: "fachada",
      original_filename: "fachada.jpg",
      category: "image",
      mime_type: "image/jpeg",
    });
    let imageCalls = 0;
    mocks.fetch.mockImplementation(
      (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (init?.method === "POST") return pendingUpload.promise;
        if (url === "/v1/media-assets") return jsonResponse(EMPTY_PAGE);
        if (url === "/v1/media-assets?category=image") {
          imageCalls += 1;
          return imageCalls === 1
            ? pendingImages.promise
            : jsonResponse({
                items: [image, older],
                next_cursor: "image-cursor",
                has_more: true,
              });
        }
        throw new Error(`Unexpected fetch: ${url}`);
      },
    );

    renderPage();
    await screen.findByText("No media has been uploaded for this clinic.");
    pickFile(sizedFile("guia.pdf", "application/pdf", 1_000));
    submitUpload();
    fireEvent.change(screen.getByLabelText("Filter by file type"), {
      target: { value: "image" },
    });
    await waitFor(() => expect(imageCalls).toBe(1));

    // The PDF upload lands first; the image page it raced arrives afterward.
    await act(async () => {
      pendingUpload.resolveJson(
        asset({ id: "asset-pdf", display_name: "guia", original_filename: "guia.pdf" }),
        201,
      );
      await pendingUpload.promise;
    });
    expect(screen.queryByText("No media matches these filters.")).not.toBeInTheDocument();
    await act(async () => {
      pendingImages.resolveJson({ items: [image], next_cursor: null, has_more: false });
      await pendingImages.promise;
    });

    expect(await screen.findByText("fachada.jpg")).toBeInTheDocument();
    expect(screen.getByText("antesedepois.png")).toBeInTheDocument();
    expect(screen.queryByText("guia.pdf")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load more" })).toBeInTheDocument();
    expect(imageCalls).toBe(2);
  });

  it("never aborts an upload in flight when a filter changes", async () => {
    const pendingUpload = deferredResponse();
    let uploadSignal: AbortSignal | undefined;
    routeList(
      {
        "/v1/media-assets": EMPTY_PAGE,
        "/v1/media-assets?category=pdf": EMPTY_PAGE,
      },
      (init) => {
        uploadSignal = init.signal as AbortSignal;
        return pendingUpload.promise;
      },
    );

    renderPage();
    await screen.findByText("No media has been uploaded for this clinic.");
    pickFile(sizedFile("guia.pdf", "application/pdf", 1_000));
    submitUpload();
    await waitFor(() => expect(uploadSignal).toBeDefined());

    fireEvent.change(screen.getByLabelText("Filter by file type"), {
      target: { value: "pdf" },
    });
    await screen.findByText("No media matches these filters.");
    expect(uploadSignal?.aborted).toBe(false);

    await act(async () => {
      pendingUpload.resolveJson(
        asset({ id: "asset-late", display_name: "guia", original_filename: "guia.pdf" }),
        201,
      );
      await pendingUpload.promise;
    });
    expect(await screen.findByText("guia.pdf")).toBeInTheDocument();
  });
});

describe("<Midias /> delete confirmation", () => {
  const image = asset({
    id: "asset-image",
    display_name: "antesedepois",
    original_filename: "antesedepois.png",
    category: "image",
    mime_type: "image/png",
  });

  it("asks in Portuguese with the file name and deletes nothing on Não", async () => {
    localStorage.setItem("ns_locale", "pt");
    mocks.fetch.mockImplementation(() =>
      jsonResponse({ items: [image], next_cursor: null, has_more: false }),
    );

    renderPage();
    fireEvent.click(
      await screen.findByRole("button", { name: "Excluir: antesedepois.png" }),
    );
    const dialog = screen.getByRole("alertdialog");
    expect(dialog).toHaveTextContent(
      'Deseja realmente excluir permanentemente o "antesedepois.png"?',
    );
    expect(within(dialog).getByRole("button", { name: "Sim" })).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Não" }));

    await waitFor(() =>
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument(),
    );
    expect(screen.getByText("antesedepois.png")).toBeInTheDocument();
    expect(
      mocks.fetch.mock.calls.some(([, init]) => init?.method === "DELETE"),
    ).toBe(false);
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

  it("renders the Portuguese catalog with the new fields and without the catalog-only notice", async () => {
    localStorage.setItem("ns_locale", "pt");
    mocks.fetch.mockImplementation(() => jsonResponse(EMPTY_PAGE));

    renderPage();

    expect(await screen.findByText("Mídias & Ativos")).toBeInTheDocument();
    expect(
      screen.queryByText(/Apenas metadados de catálogo/),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(/não enviam mídias nem executam ações em runtime/i),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("Arquivo")).toBeInTheDocument();
    expect(screen.getByLabelText("Nome do arquivo")).toBeInTheDocument();
    expect(
      within(screen.getByLabelText("Tipo de arquivo"))
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Selecione o tipo", "Imagem", "Vídeo", "Áudio", "PDF"]);
    expect(screen.getByLabelText("Gatilho de uso")).toHaveAttribute(
      "placeholder",
      "Descreva o contexto de envio.",
    );
    expect(screen.getByLabelText("Buscar nome")).toBeInTheDocument();
    expect(screen.getByText("Biblioteca de mídias")).toBeInTheDocument();

    const autoSuggest = screen.getByRole("region", {
      name: "Sugestão Automática por IA",
    });
    expect(
      within(autoSuggest).getByText(
        "Configure para que a IA sugira o envio deste arquivo automaticamente quando detectar a intenção correta do paciente.",
      ),
    ).toBeInTheDocument();
    expect(
      within(autoSuggest).getByText(
        "A IA analisa o contexto e envia o ativo no momento de maior impacto",
      ),
    ).toBeInTheDocument();
    expect(
      within(autoSuggest).getByRole("switch", {
        name: "Sugestão Automática por IA",
      }),
    ).not.toBeChecked();
    expect(
      screen.queryByText(/Apenas um marcador de catálogo/),
    ).not.toBeInTheDocument();
  });
});

// ── Credits (neurostack-credit-system Group 6) ──────────────────────────────

describe("<Midias /> credit allocation", () => {
  function routes(credit: () => Promise<Response>) {
    mocks.fetch.mockImplementation((input: string | URL | Request) => {
      const url = String(input);
      if (url.startsWith("/v1/credits/")) return credit();
      if (url.startsWith("/v1/media-assets")) {
        return jsonResponse({ items: [asset({ tenant_id: "clinic-42" })], next_cursor: null, has_more: false });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
  }

  it("shows a builder the selected clinic's media allocation", async () => {
    setRole("builder", "clinic-42");
    routes(() =>
      jsonResponse({
        as_of: "2026-10-30T15:00:00Z",
        window: { start: "2026-10-25", end: "2026-11-25", kind: "cycle" },
        reservations: [
          { category: "rag", units: 0, coverage_mc: 0, charged_mc: 0 },
          { category: "media", units: 40_000_000, coverage_mc: 2_000, charged_mc: 0 },
          { category: "egress", units: 0, coverage_mc: 0, charged_mc: 0 },
        ],
        egress_allowance_bytes: 400_000_000,
        egress_delivered_bytes: 0,
      }),
    );
    renderPage();

    const notice = await screen.findByTestId("credit-allocation-media");
    expect(notice).toHaveTextContent(/Media credits/);
    expect(notice).toHaveTextContent(/Allocated this cycle:\s*2 credits/);
    expect(notice).toHaveTextContent(/0 \/ 400 MB/);
    expect(mocks.fetch).toHaveBeenCalledWith(
      "/v1/credits/reservations?tenant_id=clinic-42",
      expect.objectContaining({ headers: { Authorization: "Bearer media-token" } }),
    );
  });

  it("hides the allocation on 503 and keeps the catalog as on main", async () => {
    setRole("super_admin", "clinic-42");
    routes(() => jsonResponse({ code: "credit_unavailable" }, 503));
    renderPage();

    expect(await screen.findByText("Orientações Pós-Botox.pdf")).toBeInTheDocument();
    await waitFor(() =>
      expect(
        mocks.fetch.mock.calls.filter(([input]) => String(input).startsWith("/v1/credits/")),
      ).toHaveLength(1),
    );
    expect(screen.queryByTestId("credit-allocation-media")).not.toBeInTheDocument();
  });

  it("reads no credit for a clinic admin", async () => {
    routes(() => jsonResponse({ code: "unexpected" }, 500));
    renderPage();

    expect(await screen.findByText("Orientações Pós-Botox.pdf")).toBeInTheDocument();
    expect(
      mocks.fetch.mock.calls.filter(([input]) => String(input).startsWith("/v1/credits/")),
    ).toHaveLength(0);
    expect(screen.queryByTestId("credit-allocation-media")).not.toBeInTheDocument();
  });
});
