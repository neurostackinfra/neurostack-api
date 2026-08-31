import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { I18nProvider } from "@/context/i18n";

import { VoiceResponseControls } from "./voice-response-controls";

function Wrapper({ children }: { children: ReactNode }) {
  return <I18nProvider>{children}</I18nProvider>;
}

function response(body: unknown, ok = true, status = ok ? 200 : 422): Response {
  return {
    ok,
    status,
    json: vi.fn().mockResolvedValue(body),
  } as unknown as Response;
}

function activeStatus(overrides: Record<string, unknown> = {}) {
  return {
    tts_enabled: true,
    tts_readiness: "active",
    tts_reply_policy: "audio_input",
    ...overrides,
  };
}

function renderControls({
  clinicId = "clinic-42",
  isReadOnly = false,
  role = "builder",
}: Partial<React.ComponentProps<typeof VoiceResponseControls>> = {}) {
  return render(
    <Wrapper>
      <VoiceResponseControls clinicId={clinicId} isReadOnly={isReadOnly} role={role} />
    </Wrapper>,
  );
}

describe("<VoiceResponseControls />", () => {
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    globalThis.fetch = originalFetch;
    localStorage.clear();
    cleanup();
    vi.restoreAllMocks();
  });

  it.each(["builder", "super_admin"])(
    "loads tenant-aware active settings for a %s editor and applies the legacy policy default",
    async (role) => {
      localStorage.setItem("ns_locale", "pt");
      localStorage.setItem("ns_auth", JSON.stringify({ token: "editor-token" }));
      const fetchMock = vi.fn().mockResolvedValue(response(activeStatus({ tts_reply_policy: undefined })));
      globalThis.fetch = fetchMock as unknown as typeof fetch;

      renderControls({ clinicId: "tenant/a", role });

      expect(screen.getByRole("heading", { name: "Gatilhos de Voz" })).toBeInTheDocument();
      expect(screen.getByRole("status")).toHaveTextContent("Carregando configurações de voz");
      expect(screen.getByRole("switch", { name: "Responder em áudio" })).toBeDisabled();

      await waitFor(() => expect(screen.getByRole("switch", { name: "Responder em áudio" })).toBeEnabled());
      expect(screen.getByRole("switch", { name: "Responder em áudio" })).toBeChecked();
      expect(screen.getByRole("radiogroup", { name: "Quando enviar áudio" })).toBeInTheDocument();
      expect(screen.getByRole("radio", { name: /Apenas quando receber áudio/ })).toBeChecked();
      expect(screen.getByRole("radio", { name: /Em todos os tipos/ })).not.toBeChecked();
      expect(screen.getByText(/Perguntas em texto mantêm a resposta em texto/)).toBeInTheDocument();
      expect(screen.getByText(/Envie áudio para qualquer tipo de pergunta/)).toBeInTheDocument();
      expect(screen.getByText("Em breve mais gatilhos de voz")).toBeInTheDocument();
      expect(fetchMock).toHaveBeenCalledWith(
        "/v1/config/api-credentials?tenant_id=tenant%2Fa",
        { headers: { Authorization: "Bearer editor-token" } },
      );
    },
  );

  it("keeps loading and incomplete settings visible but disabled with an explanation", async () => {
    let resolveGet!: (value: Response) => void;
    const fetchMock = vi.fn().mockImplementation(() => new Promise<Response>((resolve) => {
      resolveGet = resolve;
    }));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    renderControls();

    expect(screen.getByRole("switch", { name: "Reply with audio" })).toBeDisabled();
    expect(screen.getAllByRole("radio").every((radio) => radio.hasAttribute("disabled"))).toBe(true);

    await act(async () => {
      resolveGet(response(activeStatus({ tts_readiness: "incomplete" })));
    });

    expect(screen.getByText(/Complete the saved voice provider setup/)).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Reply with audio" })).toBeDisabled();
    expect(screen.getAllByRole("radio").every((radio) => radio.hasAttribute("disabled"))).toBe(true);
  });

  it("sends only tts_enabled, locks both controls while pending, and keeps credentials out of the payload", async () => {
    let resolvePatch!: (value: Response) => void;
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(activeStatus()))
      .mockImplementationOnce(() => new Promise<Response>((resolve) => {
        resolvePatch = resolve;
      }));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    renderControls();
    const master = await screen.findByRole("switch", { name: "Reply with audio" });
    await waitFor(() => expect(master).toBeEnabled());

    fireEvent.click(master);

    expect(master).not.toBeChecked();
    expect(master).toBeDisabled();
    expect(screen.getAllByRole("radio").every((radio) => radio.hasAttribute("disabled"))).toBe(true);
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/v1/config/api-credentials?tenant_id=clinic-42", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tts_enabled: false }),
    });
    fireEvent.click(screen.getByRole("radio", { name: /For every type/ }));
    expect(fetchMock).toHaveBeenCalledTimes(2);

    await act(async () => {
      resolvePatch(response(activeStatus({ tts_enabled: false })));
    });
    await waitFor(() => expect(master).toBeEnabled());
  });

  it("persists only the selected policy and displays it again after reload", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(activeStatus()))
      .mockResolvedValueOnce(response(activeStatus({ tts_reply_policy: "all_inputs" })))
      .mockResolvedValueOnce(response(activeStatus({ tts_reply_policy: "all_inputs" })));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const first = renderControls({ clinicId: "selected-tenant", role: "super_admin" });
    const allInputs = await screen.findByRole("radio", { name: /For every type/ });
    await waitFor(() => expect(allInputs).toBeEnabled());
    fireEvent.click(allInputs);

    await waitFor(() => expect(allInputs).toBeChecked());
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/v1/config/api-credentials?tenant_id=selected-tenant",
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tts_reply_policy: "all_inputs" }),
      },
    );

    first.unmount();
    renderControls({ clinicId: "selected-tenant", role: "super_admin" });

    await waitFor(() => {
      expect(screen.getByRole("radio", { name: /For every type/ })).toBeChecked();
    });
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/v1/config/api-credentials?tenant_id=selected-tenant",
      { headers: {} },
    );
  });

  it("rolls back a failed policy PATCH, preserves the master, and shows a visible error", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(activeStatus()))
      .mockResolvedValueOnce(response({ detail: { error: "provider_not_available" } }, false));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    renderControls();
    const allInputs = await screen.findByRole("radio", { name: /For every type/ });
    await waitFor(() => expect(allInputs).toBeEnabled());
    fireEvent.click(allInputs);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("previous value was restored");
    });
    expect(screen.getByRole("radio", { name: /Only when the question/ })).toBeChecked();
    expect(allInputs).not.toBeChecked();
    expect(screen.getByRole("switch", { name: "Reply with audio" })).toBeChecked();
  });

  it("uses radio keyboard navigation and sends the exclusive policy value", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(activeStatus()))
      .mockResolvedValueOnce(response(activeStatus({ tts_reply_policy: "all_inputs" })));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    renderControls();
    const audioInput = await screen.findByRole("radio", { name: /Only when the question/ });
    await waitFor(() => expect(audioInput).toBeEnabled());
    await act(async () => {
      audioInput.focus();
      fireEvent.keyDown(audioInput, { key: "ArrowDown", code: "ArrowDown" });
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    await waitFor(() => {
      expect(screen.getByRole("radio", { name: /For every type/ })).toBeChecked();
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("renders an English investor preview with static defaults and zero credential requests", () => {
    localStorage.setItem("ns_locale", "en");
    const fetchMock = vi.fn();
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    renderControls({ clinicId: "private-tenant", isReadOnly: true, role: "investor" });

    expect(screen.getByRole("heading", { name: "Voice Triggers" })).toBeInTheDocument();
    expect(screen.getByText("More voice triggers coming soon")).toBeInTheDocument();
    expect(screen.getByText(/Text questions keep their text response/)).toBeInTheDocument();
    expect(screen.getByText(/Send audio for any supported question type/)).toBeInTheDocument();
    expect(screen.getByText(/does not represent persisted clinic settings/)).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Reply with audio" })).not.toBeChecked();
    expect(screen.getByRole("switch", { name: "Reply with audio" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: /Only when the question/ })).toBeChecked();
    expect(screen.getAllByRole("radio").every((radio) => radio.hasAttribute("disabled"))).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
