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
import HandoffQueuePage from "./handoff-queue";

const mocks = vi.hoisted(() => ({
  auth: {
    tenantId: "clinic-id" as string | null,
    isBuilder: false,
    isSuperAdmin: false,
    isInvestor: false,
    viewingClinicId: "",
  },
  fetch: vi.fn(),
  toast: vi.fn(),
}));

vi.mock("@/context/auth", () => ({
  useAuth: () => mocks.auth,
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mocks.toast }),
}));

interface QueueItemFixture {
  id: string;
  numero_whatsapp: string | null;
  session_id: string;
  intencao_detectada: string | null;
  estagio_atual: string;
  primeira_linha_mensagem: null;
  created_at: string;
  data_hora_saida: null;
  sla_target_seconds: number | null;
  status: string;
  safety_flag: boolean;
}

interface QueueStatsFixture {
  avg_wait_seconds: number | null;
  pct_within_sla: number | null;
  within_sla_count: number;
  confirmed_late_count: number;
  unanswered_overdue_count: number;
  maturing_count: number;
  cancelled_unconfirmed_count: number;
  unmeasured_legacy_count: number;
  confirmed_count: number;
  sla_eligible_count: number;
  period_timezone: string;
  period_start: string;
  period_end_exclusive: string;
}

const DEFAULT_STATS: QueueStatsFixture = {
  avg_wait_seconds: 138,
  pct_within_sla: 40,
  within_sla_count: 2,
  confirmed_late_count: 3,
  unanswered_overdue_count: 4,
  maturing_count: 5,
  cancelled_unconfirmed_count: 6,
  unmeasured_legacy_count: 7,
  confirmed_count: 5,
  sla_eligible_count: 5,
  period_timezone: "America/Sao_Paulo",
  period_start: "2026-09-01T03:00:00Z",
  period_end_exclusive: "2026-10-01T03:00:00Z",
};

function item(
  id: string,
  intent: string | null = "pedido_preco",
  safetyFlag = false,
  createdAt = "2001-01-01T12:00:00.000Z",
  slaTargetSeconds: number | null = 600,
): QueueItemFixture {
  return {
    id,
    numero_whatsapp: `55119999999${id.charCodeAt(0)}`,
    session_id: `${id}-12345678-abcd-4abc-8def-1234567890ab`,
    intencao_detectada: intent,
    estagio_atual: `hidden-stage-${id}`,
    primeira_linha_mensagem: null,
    created_at: createdAt,
    data_hora_saida: null,
    sla_target_seconds: slaTargetSeconds,
    status: "pending",
    safety_flag: safetyFlag,
  };
}

// Deliberately not chronological: the UI must preserve this API/FIFO order.
const FIFO_ITEMS = [
  item("a", "pedido_preco"),
  item("b", "agendar", true),
  item("c", "orcamento_existente", true),
  item("d", null, true),
  item("e", "future_intent", true),
];

function queueResponse(
  items: QueueItemFixture[] = FIFO_ITEMS,
  stats: Partial<QueueStatsFixture> = {},
): Response {
  return {
    ok: true,
    status: 200,
    json: async () => ({ items, ...DEFAULT_STATS, ...stats }),
  } as Response;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function renderPage(locale: "pt" | "en" = "pt") {
  localStorage.setItem("ns_locale", locale);
  return render(
    <I18nProvider>
      <HandoffQueuePage />
    </I18nProvider>,
  );
}

function cardIds(): string[] {
  return screen
    .queryAllByTestId("handoff-card")
    .map((card) => card.getAttribute("data-handoff-id") ?? "");
}

function dateInputs(): HTMLInputElement[] {
  return [...document.querySelectorAll<HTMLInputElement>('input[type="date"]')];
}

async function renderLoaded(locale: "pt" | "en" = "pt") {
  mocks.fetch.mockResolvedValue(queueResponse());
  renderPage(locale);
  await waitFor(() => expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]));
}

beforeEach(() => {
  localStorage.setItem("ns_auth", JSON.stringify({ token: "test-token" }));
  Object.assign(mocks.auth, {
    tenantId: "clinic-id",
    isBuilder: false,
    isSuperAdmin: false,
    isInvestor: false,
    viewingClinicId: "",
  });
  mocks.fetch.mockReset();
  mocks.toast.mockReset();
  vi.stubGlobal("fetch", mocks.fetch);
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("<HandoffQueuePage />", () => {
  it("lets the clinic resolve the initial period and formats cards in its timezone", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-02T00:30:00.000Z"));
    vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockReturnValue({
      locale: "ja-JP",
      calendar: "gregory",
      numberingSystem: "latn",
      timeZone: "Asia/Tokyo",
    });
    mocks.fetch.mockResolvedValue(
      queueResponse(
        [
          item(
            "clinic-date",
            "pedido_preco",
            false,
            "2026-09-02T01:30:00.000Z",
          ),
        ],
        {
          period_timezone: "America/Sao_Paulo",
          period_start: "2026-09-01T03:00:00Z",
          period_end_exclusive: "2026-09-02T03:00:00Z",
        },
      ),
    );

    renderPage();
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(cardIds()).toEqual(["clinic-date"]);
    expect(mocks.fetch).toHaveBeenCalledTimes(1);
    expect(String(mocks.fetch.mock.calls[0][0])).toBe(
      "/v1/handoffs/queue?tenant_id=clinic-id",
    );
    expect(String(mocks.fetch.mock.calls[0][0])).not.toMatch(
      /from_date|to_date|timezone/,
    );
    expect(dateInputs().map((input) => input.value)).toEqual([
      "2026-09-01",
      "2026-09-01",
    ]);
    expect(screen.getByTestId("handoff-period")).toHaveTextContent(
      "America/Sao_Paulo",
    );
    expect(
      within(screen.getByTestId("handoff-card")).getByText(/01\/09\/2026/),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("hidden-stage-clinic-date"),
    ).not.toBeInTheDocument();
  });

  it("URL-encodes the effective managed clinic in queue requests", async () => {
    Object.assign(mocks.auth, {
      tenantId: "builder-id",
      isBuilder: true,
      viewingClinicId: "managed clinic/id",
    });
    mocks.fetch.mockResolvedValue(queueResponse([]));

    renderPage();

    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(1));
    expect(mocks.fetch).toHaveBeenCalledWith(
      "/v1/handoffs/queue?tenant_id=managed+clinic%2Fid",
      { headers: { Authorization: "Bearer test-token" } },
    );
  });

  it.each([
    {
      locale: "pt" as const,
      averageTitle: "Tempo Médio na Fila",
      averageDescription:
        "Tempo médio até a confirmação do atendimento humano.",
      slaTitle: "Dentro da Meta",
      slaDescription:
        "Percentual calculado sobre os atendimentos elegíveis no período.",
      hidden: ["Avg. Queue Wait", "Within SLA"],
    },
    {
      locale: "en" as const,
      averageTitle: "Avg. Queue Wait",
      averageDescription: "Average time until human service is confirmed.",
      slaTitle: "Within SLA",
      slaDescription:
        "Percentage calculated from eligible handoffs in the period.",
      hidden: ["Tempo Médio na Fila", "Dentro da Meta"],
    },
  ])(
    "shows SLA analytics only in $locale",
    async ({
      locale,
      averageTitle,
      averageDescription,
      slaTitle,
      slaDescription,
      hidden,
    }) => {
      await renderLoaded(locale);

      expect(screen.getByText(averageTitle)).toBeInTheDocument();
      expect(screen.getByText(averageDescription)).toBeInTheDocument();
      expect(
        screen.getByText(
          (_, element) =>
            element?.tagName === "P" && element.textContent === slaTitle,
        ),
      ).toBeInTheDocument();
      expect(screen.getByText(slaDescription)).toBeInTheDocument();
      hidden.forEach((text) =>
        expect(screen.queryByText(text)).not.toBeInTheDocument(),
      );
    },
  );

  it("preserves received order through operational filters without refetching", async () => {
    await renderLoaded();
    const requestCount = mocks.fetch.mock.calls.length;
    const dates = dateInputs();

    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "Somente com sinalização de segurança",
      }),
    );
    expect(cardIds()).toEqual(["b", "c", "d", "e"]);

    fireEvent.change(
      screen.getByRole("combobox", { name: "Grupo de intenção" }),
      {
        target: { value: "pedido_preco" },
      },
    );
    expect(cardIds()).toEqual(["c"]);

    fireEvent.click(screen.getByRole("button", { name: "Limpar filtros" }));
    expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]);
    expect(dates).toHaveLength(2);
    expect(dates.map((input) => input.value)).toEqual([
      "2026-09-01",
      "2026-09-30",
    ]);
    expect(mocks.fetch).toHaveBeenCalledTimes(requestCount);
  });

  it("ignores a stale queue response after the date scope changes", async () => {
    const stale = deferred<Response>();
    mocks.fetch
      .mockImplementationOnce(() => stale.promise)
      .mockResolvedValueOnce(queueResponse([item("fresh")]));

    renderPage();
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(1));
    fireEvent.change(dateInputs()[0], { target: { value: "2020-01-01" } });

    await waitFor(() => expect(cardIds()).toEqual(["fresh"]));
    await act(async () => {
      stale.resolve(queueResponse([item("stale")]));
      await stale.promise;
    });
    expect(cardIds()).toEqual(["fresh"]);
  });

  it("clears stale data and blocks export until the edited period loads", async () => {
    const nextPeriod = deferred<Response>();
    mocks.fetch
      .mockResolvedValueOnce(queueResponse())
      .mockImplementationOnce(() => nextPeriod.promise)
      .mockResolvedValueOnce({
        ok: false,
        status: 500,
        json: async () => ({}),
      } as Response);
    renderPage();
    await waitFor(() => expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]));
    const exportButton = screen.getByRole("button", { name: "Exportar CSV" });

    fireEvent.change(dateInputs()[0], { target: { value: "2026-09-02" } });
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(2));

    expect(cardIds()).toEqual([]);
    expect(screen.queryByText("00:02:18")).not.toBeInTheDocument();
    expect(exportButton).toBeDisabled();
    fireEvent.click(exportButton);
    expect(mocks.fetch).toHaveBeenCalledTimes(2);

    await act(async () => {
      nextPeriod.resolve(
        queueResponse([item("new-period")], {
          avg_wait_seconds: 222,
          period_start: "2026-09-02T03:00:00Z",
        }),
      );
      await nextPeriod.promise;
    });

    await waitFor(() => expect(cardIds()).toEqual(["new-period"]));
    expect(exportButton).toBeEnabled();
    fireEvent.click(exportButton);
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(3));
    expect(String(mocks.fetch.mock.calls[2][0])).toContain(
      "from_date=2026-09-02",
    );
  });

  it.each(["http", "network"] as const)(
    "keeps stale data hidden and export unavailable after a %s period failure",
    async (failure) => {
      mocks.fetch
        .mockResolvedValueOnce(queueResponse())
        .mockImplementationOnce(() =>
          failure === "http"
            ? Promise.resolve({ ok: false, status: 500 } as Response)
            : Promise.reject(new Error("offline")),
        );
      renderPage();
      await waitFor(() => expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]));

      fireEvent.change(dateInputs()[0], { target: { value: "2026-09-02" } });
      await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(2));
      await waitFor(() =>
        expect(
          screen.getByRole("button", { name: "Exportar CSV" }),
        ).toBeDisabled(),
      );

      expect(cardIds()).toEqual([]);
      expect(screen.queryByText("00:02:18")).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Exportar CSV" }));
      expect(mocks.fetch).toHaveBeenCalledTimes(2);
    },
  );

  it("resets to backend defaults when the active clinic changes", async () => {
    mocks.fetch
      .mockResolvedValueOnce(
        queueResponse([item("clinic-one")], {
          period_timezone: "America/Sao_Paulo",
          period_start: "2026-09-01T03:00:00Z",
          period_end_exclusive: "2026-09-02T03:00:00Z",
        }),
      )
      .mockResolvedValueOnce(
        queueResponse([item("clinic-two")], {
          period_timezone: "Asia/Tokyo",
          period_start: "2026-10-01T15:00:00Z",
          period_end_exclusive: "2026-10-02T15:00:00Z",
        }),
      );
    const view = renderPage();
    await waitFor(() => expect(cardIds()).toEqual(["clinic-one"]));
    expect(dateInputs().map((input) => input.value)).toEqual([
      "2026-09-01",
      "2026-09-01",
    ]);

    mocks.auth.tenantId = "clinic-two";
    view.rerender(
      <I18nProvider>
        <HandoffQueuePage />
      </I18nProvider>,
    );

    await waitFor(() => expect(cardIds()).toEqual(["clinic-two"]));
    expect(mocks.fetch).toHaveBeenCalledTimes(2);
    expect(String(mocks.fetch.mock.calls[1][0])).toBe(
      "/v1/handoffs/queue?tenant_id=clinic-two",
    );
    expect(dateInputs().map((input) => input.value)).toEqual([
      "2026-10-02",
      "2026-10-02",
    ]);
  });

  it("deduplicates confirmation across date scopes and reconciles the current cohort after commit", async () => {
    const confirmation = deferred<Response>();
    const preCommitScope = deferred<Response>();
    const authoritativeScope = deferred<Response>();
    let queueRequests = 0;
    mocks.fetch.mockImplementation(
      (input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "POST") return confirmation.promise;
        queueRequests += 1;
        if (queueRequests === 1) {
          return Promise.resolve(
            queueResponse([item("shared-card")], {
              avg_wait_seconds: 60,
              period_start: "2026-09-01T03:00:00Z",
              period_end_exclusive: "2026-10-01T03:00:00Z",
            }),
          );
        }
        if (queueRequests === 2) return preCommitScope.promise;
        return authoritativeScope.promise;
      },
    );
    renderPage();
    await waitFor(() => expect(cardIds()).toEqual(["shared-card"]));

    fireEvent.click(
      screen.getByRole("button", { name: "Confirmar atendimento" }),
    );
    fireEvent.change(dateInputs()[0], { target: { value: "2026-09-02" } });
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(3));
    expect(cardIds()).toEqual([]);
    await act(async () => {
      preCommitScope.resolve(
        queueResponse([item("shared-card")], {
          avg_wait_seconds: 120,
          within_sla_count: 4,
          sla_eligible_count: 5,
          period_start: "2026-09-02T03:00:00Z",
          period_end_exclusive: "2026-10-01T03:00:00Z",
        }),
      );
      await preCommitScope.promise;
    });
    await waitFor(() => expect(cardIds()).toEqual(["shared-card"]));
    const currentScopeButton = screen.getByRole("button", {
      name: "Confirmando…",
    });
    expect(currentScopeButton).toBeDisabled();
    fireEvent.click(currentScopeButton);
    expect(
      mocks.fetch.mock.calls.filter(([, init]) => init?.method === "POST"),
    ).toHaveLength(1);

    await act(async () => {
      confirmation.resolve({ ok: true, status: 204 } as Response);
      await confirmation.promise;
    });
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(4));
    await act(async () => {
      authoritativeScope.resolve(
        queueResponse([], {
          avg_wait_seconds: 222,
          within_sla_count: 9,
          sla_eligible_count: 10,
          period_start: "2026-09-02T03:00:00Z",
          period_end_exclusive: "2026-10-01T03:00:00Z",
        }),
      );
      await authoritativeScope.promise;
    });

    expect(cardIds()).toEqual([]);
    expect(screen.getByText("00:03:42")).toBeInTheDocument();
    expect(
      screen.getByText("9 de 10 atendimentos elegíveis dentro da meta"),
    ).toBeInTheDocument();
    expect(
      mocks.fetch.mock.calls.filter(([, init]) => init?.method === "POST"),
    ).toHaveLength(1);
  });

  it("releases a stale failed confirmation for retry without toasting the new scope", async () => {
    const staleFailure = deferred<Response>();
    const retry = deferred<Response>();
    let postRequests = 0;
    mocks.fetch.mockImplementation(
      (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "POST") {
          postRequests += 1;
          return postRequests === 1 ? staleFailure.promise : retry.promise;
        }
        return Promise.resolve(queueResponse([item("shared-card")]));
      },
    );
    renderPage();
    await waitFor(() => expect(cardIds()).toEqual(["shared-card"]));

    fireEvent.click(
      screen.getByRole("button", { name: "Confirmar atendimento" }),
    );
    fireEvent.change(dateInputs()[0], { target: { value: "2026-09-02" } });
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(3));
    expect(screen.getByRole("button", { name: "Confirmando…" })).toBeDisabled();

    await act(async () => {
      staleFailure.resolve({ ok: false, status: 500 } as Response);
      await staleFailure.promise;
    });

    const retryButton = await screen.findByRole("button", {
      name: "Confirmar atendimento",
    });
    expect(mocks.toast).not.toHaveBeenCalled();
    fireEvent.click(retryButton);
    expect(
      mocks.fetch.mock.calls.filter(([, init]) => init?.method === "POST"),
    ).toHaveLength(2);
  });

  it("opens WhatsApp without confirming or removing the handoff", async () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    await renderLoaded();

    const firstCard = screen.getAllByTestId("handoff-card")[0];
    fireEvent.click(
      within(firstCard).getByRole("button", { name: "Abrir WhatsApp" }),
    );

    expect(open).toHaveBeenCalledWith("https://wa.me/5511999999997", "_blank");
    expect(mocks.fetch).toHaveBeenCalledTimes(1);
    expect(cardIds()).toContain("a");
  });

  it.each([
    ["pt" as const, "Confirme somente após iniciar o atendimento humano."],
    ["en" as const, "Confirm only after human service has started."],
  ])("shows confirmation timing guidance in %s", async (locale, guidance) => {
    await renderLoaded(locale);

    expect(screen.getAllByText(guidance)).toHaveLength(FIFO_ITEMS.length);
  });

  it.each([
    { role: "clinic", auth: {}, tenant: "clinic-id" },
    {
      role: "builder",
      auth: {
        tenantId: "builder-id",
        isBuilder: true,
        viewingClinicId: "managed clinic/id",
      },
      tenant: "managed+clinic%2Fid",
    },
    {
      role: "super-admin",
      auth: {
        tenantId: "admin-id",
        isSuperAdmin: true,
        viewingClinicId: "viewed-clinic",
      },
      tenant: "viewed-clinic",
    },
  ])(
    "confirms once through the canonical endpoint for $role and removes only after 2xx",
    async ({ auth, tenant }) => {
      Object.assign(mocks.auth, auth);
      const confirmation = deferred<Response>();
      mocks.fetch
        .mockResolvedValueOnce(
          queueResponse([item("confirm-me"), item("other")]),
        )
        .mockImplementationOnce(() => confirmation.promise)
        .mockResolvedValueOnce(
          queueResponse([item("other")], {
            avg_wait_seconds: 321,
            within_sla_count: 8,
            sla_eligible_count: 10,
          }),
        );
      renderPage();
      await waitFor(() => expect(cardIds()).toEqual(["confirm-me", "other"]));

      const [confirmCard, otherCard] = screen.getAllByTestId("handoff-card");
      const button = within(confirmCard).getByRole("button", {
        name: "Confirmar atendimento",
      });
      fireEvent.click(button);
      fireEvent.click(button);

      expect(cardIds()).toEqual(["confirm-me", "other"]);
      expect(
        screen.getByRole("button", { name: "Confirmando…" }),
      ).toBeDisabled();
      expect(
        within(otherCard).getByRole("button", {
          name: "Confirmar atendimento",
        }),
      ).toBeEnabled();
      expect(mocks.fetch).toHaveBeenCalledTimes(2);
      expect(mocks.fetch).toHaveBeenLastCalledWith(
        `/v1/handoffs/queue/confirm-me/confirm?tenant_id=${tenant}`,
        { method: "POST", headers: { Authorization: "Bearer test-token" } },
      );
      expect(String(mocks.fetch.mock.calls[1][0])).not.toContain("checkout");

      await act(async () => {
        confirmation.resolve({ ok: true, status: 204 } as Response);
        await confirmation.promise;
      });
      await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(3));
      await waitFor(() => expect(cardIds()).toEqual(["other"]));
      expect(screen.getByText("00:05:21")).toBeInTheDocument();
      expect(
        screen.getByText("8 de 10 atendimentos elegíveis dentro da meta"),
      ).toBeInTheDocument();
    },
  );

  it.each([
    {
      locale: "pt" as const,
      status: 409,
      message:
        "A confirmação entrou em conflito. Aguarde a atualização da fila e tente novamente.",
      button: "Confirmar atendimento",
    },
    {
      locale: "en" as const,
      status: 409,
      message:
        "The confirmation conflicted with a newer state. Wait for the queue to refresh, then try again.",
      button: "Confirm service",
    },
    {
      locale: "pt" as const,
      status: 500,
      message: "Não foi possível confirmar o atendimento. Tente novamente.",
      button: "Confirmar atendimento",
    },
    {
      locale: "en" as const,
      status: 500,
      message: "Could not confirm service. Please try again.",
      button: "Confirm service",
    },
  ])(
    "keeps the card and re-enables confirmation after $status in $locale",
    async ({ locale, status, message, button }) => {
      mocks.fetch
        .mockResolvedValueOnce(queueResponse([item("retry-me")]))
        .mockResolvedValueOnce({ ok: false, status } as Response);
      renderPage(locale);
      await waitFor(() => expect(cardIds()).toEqual(["retry-me"]));

      fireEvent.click(screen.getByRole("button", { name: button }));

      await waitFor(() =>
        expect(mocks.toast).toHaveBeenCalledWith({
          title: message,
          variant: "destructive",
        }),
      );
      expect(cardIds()).toEqual(["retry-me"]);
      expect(screen.getByRole("button", { name: button })).toBeEnabled();
    },
  );

  it("distinguishes null KPI data from measured zero", async () => {
    mocks.fetch.mockResolvedValue(
      queueResponse([], {
        avg_wait_seconds: null,
        pct_within_sla: null,
        within_sla_count: 0,
        sla_eligible_count: 0,
      }),
    );
    const view = renderPage();

    expect(await screen.findByText("Sem dados")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Sem dados" })).toHaveTextContent(
      "—",
    );
    expect(screen.queryByText("00:00:00")).not.toBeInTheDocument();

    view.unmount();
    mocks.fetch.mockReset();
    mocks.fetch.mockResolvedValue(
      queueResponse([], {
        avg_wait_seconds: 0,
        pct_within_sla: 0,
        within_sla_count: 0,
        sla_eligible_count: 0,
      }),
    );
    renderPage();

    expect(await screen.findByText("00:00:00")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "0%" })).toBeInTheDocument();
    expect(
      screen.getByText("0 de 0 atendimentos elegíveis dentro da meta"),
    ).toBeInTheDocument();
  });

  it.each([
    {
      locale: "pt" as const,
      title: "Dentro da Meta",
      removedWithin: "Dentro da meta",
      removedLabels: [
        "Confirmados fora",
        "Pendentes vencidos",
        "Em maturação",
        "Cancelados sem confirmação",
        "Legado sem medição",
      ],
      context: "2 de 5 atendimentos elegíveis dentro da meta",
    },
    {
      locale: "en" as const,
      title: "Within SLA",
      removedWithin: "Within SLA",
      removedLabels: [
        "Confirmed late",
        "Unanswered overdue",
        "Maturing",
        "Cancelled unconfirmed",
        "Unmeasured legacy",
      ],
      context: "2 of 5 eligible handoffs within SLA",
    },
  ])(
    "removes the six-counter summary while preserving the $locale SLA KPI",
    async ({ locale, title, removedWithin, removedLabels, context }) => {
      mocks.fetch.mockResolvedValue(queueResponse([], { pct_within_sla: 40 }));
      renderPage(locale);

      const gauge = await screen.findByRole("img", { name: "40%" });
      const kpiTitle = screen.getByText(title);
      const kpiCard = kpiTitle.parentElement!;

      expect(within(kpiCard).getByRole("img", { name: "40%" })).toBe(gauge);
      expect(within(kpiCard).getByText(context)).toBeInTheDocument();
      removedLabels.forEach((label) => {
        expect(screen.queryByText(label)).not.toBeInTheDocument();
      });

      if (locale === "en") {
        expect(screen.getAllByText(removedWithin)).toHaveLength(1);
      } else {
        expect(screen.queryByText(removedWithin)).not.toBeInTheDocument();
      }
    },
  );

  it("keeps decimal precision in the Portuguese accessible label while rounding the visual value", async () => {
    mocks.fetch.mockResolvedValue(queueResponse([], { pct_within_sla: 66.7 }));
    renderPage();

    expect(
      await screen.findByRole("img", { name: "66,7%" }),
    ).toBeInTheDocument();
    expect(screen.getByText("67%")).toBeInTheDocument();
  });

  it.each([
    {
      locale: "pt" as const,
      legends: [
        "Abaixo de 50% do SLA individual",
        "De 50% a 100% do SLA individual",
        "Acima de 100% do SLA individual",
        "Sem meta individual",
      ],
    },
    {
      locale: "en" as const,
      legends: [
        "Below 50% of individual SLA",
        "50% to 100% of individual SLA",
        "Above 100% of individual SLA",
        "No individual SLA target",
      ],
    },
  ])(
    "maps SLA snapshot color boundaries and legends in $locale",
    async ({ locale, legends }) => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date("2026-09-28T12:00:00.000Z"));
      const createdAt = (secondsAgo: number) =>
        new Date(Date.now() - secondsAgo * 1000).toISOString();
      mocks.fetch.mockResolvedValue(
        queueResponse([
          item("green", "pedido_preco", false, createdAt(299), 600),
          item("yellow-half", "pedido_preco", false, createdAt(300), 600),
          item("yellow-full", "pedido_preco", false, createdAt(600), 600),
          item("red", "pedido_preco", false, createdAt(601), 600),
          item("neutral", "pedido_preco", false, createdAt(999), null),
        ]),
      );

      renderPage(locale);
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });

      const state = (id: string) =>
        document
          .querySelector(`[data-handoff-id="${id}"]`)
          ?.getAttribute("data-sla-state");
      expect(state("green")).toBe("green");
      expect(state("yellow-half")).toBe("yellow");
      expect(state("yellow-full")).toBe("yellow");
      expect(state("red")).toBe("red");
      expect(state("neutral")).toBe("neutral");
      legends.forEach((legend) =>
        expect(screen.getByText(legend)).toBeInTheDocument(),
      );
    },
  );

  it("exports the selected backend date scope without a browser timezone", async () => {
    const exportedBlob = new Blob(["csv"]);
    const blob = vi.fn(async () => exportedBlob);
    const createObjectURL = vi.fn(() => "blob:handoffs");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal(
      "URL",
      Object.assign(URL, { createObjectURL, revokeObjectURL }),
    );
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    mocks.fetch.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/export?"))
        return { ok: true, blob } as unknown as Response;
      return queueResponse();
    });
    renderPage();
    await waitFor(() => expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]));

    const createElement = vi.spyOn(document, "createElement");
    fireEvent.click(screen.getByRole("button", { name: "Exportar CSV" }));
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(2));

    const exportUrl = String(mocks.fetch.mock.calls[1][0]);
    expect(exportUrl).toBe(
      "/v1/handoffs/queue/export?tenant_id=clinic-id&from_date=2026-09-01&to_date=2026-09-30",
    );
    expect(exportUrl).not.toContain("timezone=");
    expect(blob).toHaveBeenCalledTimes(1);
    expect(createObjectURL).toHaveBeenCalledWith(exportedBlob);
    const anchor = createElement.mock.results.find(
      (result) => result.value instanceof HTMLAnchorElement,
    )?.value as HTMLAnchorElement;
    expect(anchor.download).toBe("handoffs.csv");
    expect(anchorClick).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:handoffs");
  });

  it.each(["response", "blob"] as const)(
    "discards an export when the period changes before the %s resolves",
    async (pendingStage) => {
      const exportResponse = deferred<Response>();
      const exportedBlob = deferred<Blob>();
      const blob = vi.fn(() => exportedBlob.promise);
      const createObjectURL = vi.fn(() => "blob:stale-handoffs");
      const revokeObjectURL = vi.fn();
      vi.stubGlobal(
        "URL",
        Object.assign(URL, { createObjectURL, revokeObjectURL }),
      );
      const anchorClick = vi
        .spyOn(HTMLAnchorElement.prototype, "click")
        .mockImplementation(() => undefined);
      mocks.fetch.mockImplementation((input: RequestInfo | URL) => {
        if (String(input).includes("/export?")) {
          if (pendingStage === "response") return exportResponse.promise;
          return Promise.resolve({ ok: true, blob } as unknown as Response);
        }
        return Promise.resolve(queueResponse());
      });
      renderPage();
      await waitFor(() => expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]));
      const appendChild = vi.spyOn(document.body, "appendChild");

      fireEvent.click(screen.getByRole("button", { name: "Exportar CSV" }));
      await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(2));
      if (pendingStage === "blob") {
        await waitFor(() => expect(blob).toHaveBeenCalledTimes(1));
      }
      fireEvent.change(dateInputs()[0], {
        target: { value: "2026-09-02" },
      });
      await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(3));

      await act(async () => {
        if (pendingStage === "response") {
          exportResponse.resolve({ ok: true, blob } as unknown as Response);
          await exportResponse.promise;
        }
        exportedBlob.resolve(new Blob(["stale csv"]));
        await exportedBlob.promise;
        await Promise.resolve();
      });

      expect(createObjectURL).not.toHaveBeenCalled();
      expect(
        appendChild.mock.calls.filter(
          ([node]) => node instanceof HTMLAnchorElement,
        ),
      ).toHaveLength(0);
      expect(anchorClick).not.toHaveBeenCalled();
      expect(revokeObjectURL).not.toHaveBeenCalled();
    },
  );

  it.each([
    { isBuilder: true, isSuperAdmin: false },
    { isBuilder: false, isSuperAdmin: true },
  ])(
    "discards an export after a privileged clinic changes away and back",
    async ({ isBuilder, isSuperAdmin }) => {
      Object.assign(mocks.auth, {
        tenantId: "privileged-user",
        isBuilder,
        isSuperAdmin,
        viewingClinicId: "clinic-one",
      });
      const exportResponse = deferred<Response>();
      const blob = vi.fn(async () => new Blob(["stale csv"]));
      const createObjectURL = vi.fn(() => "blob:stale-handoffs");
      const revokeObjectURL = vi.fn();
      vi.stubGlobal(
        "URL",
        Object.assign(URL, { createObjectURL, revokeObjectURL }),
      );
      const anchorClick = vi
        .spyOn(HTMLAnchorElement.prototype, "click")
        .mockImplementation(() => undefined);
      mocks.fetch.mockImplementation((input: RequestInfo | URL) => {
        if (String(input).includes("/export?")) return exportResponse.promise;
        return Promise.resolve(queueResponse());
      });
      const view = renderPage();
      await waitFor(() => expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]));
      const appendChild = vi.spyOn(document.body, "appendChild");

      fireEvent.click(screen.getByRole("button", { name: "Exportar CSV" }));
      await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(2));
      mocks.auth.viewingClinicId = "clinic-two";
      view.rerender(
        <I18nProvider>
          <HandoffQueuePage />
        </I18nProvider>,
      );
      await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(3));
      mocks.auth.viewingClinicId = "clinic-one";
      view.rerender(
        <I18nProvider>
          <HandoffQueuePage />
        </I18nProvider>,
      );
      await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(4));

      await act(async () => {
        exportResponse.resolve({ ok: true, blob } as unknown as Response);
        await exportResponse.promise;
        await Promise.resolve();
      });

      expect(blob).not.toHaveBeenCalled();
      expect(createObjectURL).not.toHaveBeenCalled();
      expect(
        appendChild.mock.calls.filter(
          ([node]) => node instanceof HTMLAnchorElement,
        ),
      ).toHaveLength(0);
      expect(anchorClick).not.toHaveBeenCalled();
      expect(revokeObjectURL).not.toHaveBeenCalled();
    },
  );

  it("does not toast a stale non-2xx export after its error JSON resolves", async () => {
    const errorJson = deferred<{ detail: string }>();
    const json = vi.fn(() => errorJson.promise);
    mocks.fetch.mockImplementation((input: RequestInfo | URL) => {
      if (String(input).includes("/export?")) {
        return Promise.resolve({
          ok: false,
          status: 400,
          json,
        } as unknown as Response);
      }
      return Promise.resolve(queueResponse());
    });
    renderPage();
    await waitFor(() => expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]));

    fireEvent.click(screen.getByRole("button", { name: "Exportar CSV" }));
    await waitFor(() => expect(json).toHaveBeenCalledTimes(1));
    fireEvent.change(dateInputs()[0], {
      target: { value: "2026-09-02" },
    });
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(3));

    await act(async () => {
      errorJson.resolve({ detail: "export_limit_10000_exceeded" });
      await errorJson.promise;
      await Promise.resolve();
    });

    expect(mocks.toast).not.toHaveBeenCalled();
  });

  it("revokes a URL without clicking when the export becomes stale after URL creation", async () => {
    const revokeObjectURL = vi.fn();
    const createObjectURL = vi.fn(() => {
      fireEvent.change(dateInputs()[0], {
        target: { value: "2026-09-02" },
      });
      return "blob:stale-handoffs";
    });
    vi.stubGlobal(
      "URL",
      Object.assign(URL, { createObjectURL, revokeObjectURL }),
    );
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    const appendChild = vi.spyOn(document.body, "appendChild");
    mocks.fetch.mockImplementation((input: RequestInfo | URL) => {
      if (String(input).includes("/export?")) {
        return Promise.resolve({
          ok: true,
          blob: async () => new Blob(["stale csv"]),
        } as Response);
      }
      return Promise.resolve(queueResponse());
    });
    renderPage();
    await waitFor(() => expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]));

    fireEvent.click(screen.getByRole("button", { name: "Exportar CSV" }));

    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:stale-handoffs");
    expect(
      appendChild.mock.calls.filter(
        ([node]) => node instanceof HTMLAnchorElement,
      ),
    ).toHaveLength(0);
    expect(anchorClick).not.toHaveBeenCalled();
  });

  it.each([
    {
      locale: "pt" as const,
      button: "Exportar CSV",
      message:
        "A exportação excede o limite de 10.000 registros. Reduza o período.",
    },
    {
      locale: "en" as const,
      button: "Export CSV",
      message:
        "The export exceeds the 10,000-record limit. Shorten the date range.",
    },
  ])(
    "preserves the localized export limit error in $locale",
    async ({ locale, button, message }) => {
      mocks.fetch.mockImplementation(async (input: RequestInfo | URL) => {
        if (String(input).includes("/export?")) {
          return {
            ok: false,
            status: 400,
            json: async () => ({ detail: "export_limit_10000_exceeded" }),
          } as Response;
        }
        return queueResponse();
      });
      renderPage(locale);
      await waitFor(() => expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]));

      fireEvent.click(screen.getByRole("button", { name: button }));

      await waitFor(() =>
        expect(mocks.toast).toHaveBeenCalledWith({
          title: message,
          variant: "destructive",
        }),
      );
      expect(String(mocks.fetch.mock.calls[1][0])).not.toContain("timezone=");
    },
  );

  it.each([
    {
      locale: "pt" as const,
      detail: "export_window_60_days",
      button: "Exportar CSV",
      message:
        "Cada arquivo cobre no máximo 60 dias. Reduza o período e tente novamente.",
    },
    {
      locale: "pt" as const,
      detail: "export_lookback_6_months",
      button: "Exportar CSV",
      message:
        "A exportação alcança no máximo 6 meses atrás. Escolha uma data inicial mais recente.",
    },
    {
      locale: "en" as const,
      detail: "export_window_60_days",
      button: "Export CSV",
      message: "Each file covers at most 60 days. Shorten the range and try again.",
    },
    {
      locale: "en" as const,
      detail: "export_lookback_6_months",
      button: "Export CSV",
      message:
        "Exports reach back at most 6 months. Choose a more recent start date.",
    },
  ])(
    "toasts the $detail export error in $locale",
    async ({ locale, detail, button, message }) => {
      mocks.fetch.mockImplementation(async (input: RequestInfo | URL) => {
        if (String(input).includes("/export?")) {
          return {
            ok: false,
            status: 400,
            json: async () => ({ detail }),
          } as Response;
        }
        return queueResponse();
      });
      renderPage(locale);
      await waitFor(() => expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]));

      fireEvent.click(screen.getByRole("button", { name: button }));

      await waitFor(() =>
        expect(mocks.toast).toHaveBeenCalledWith({
          title: message,
          variant: "destructive",
        }),
      );
      expect(mocks.toast).toHaveBeenCalledOnce();
    },
  );

  it("keeps loading 61-90 day queue windows while export stays blocked above 60 days", async () => {
    const hint =
      "O período selecionado passa de 60 dias. Reduza o intervalo para exportar.";
    mocks.fetch.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/export?")) {
        return { ok: false, status: 500, json: async () => ({}) } as Response;
      }
      return queueResponse();
    });
    renderPage();
    await waitFor(() => expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]));
    expect(
      screen.getByText("Exportação: até 6 meses atrás, 60 dias por arquivo."),
    ).toBeInTheDocument();
    const exportButton = screen.getByRole("button", { name: "Exportar CSV" });
    expect(exportButton).toBeEnabled();
    expect(screen.queryByText(hint)).not.toBeInTheDocument();

    // 03/07..30/09 = 90 civil dates: the queue loads, the export does not.
    fireEvent.change(dateInputs()[0], { target: { value: "2026-07-03" } });
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(2));
    expect(String(mocks.fetch.mock.calls[1][0])).toBe(
      "/v1/handoffs/queue?tenant_id=clinic-id&from_date=2026-07-03&to_date=2026-09-30",
    );
    await waitFor(() => expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]));
    expect(exportButton).toBeDisabled();
    expect(screen.getByText(hint)).toBeInTheDocument();
    expect(exportButton).toHaveAttribute(
      "aria-describedby",
      "handoff-export-window-hint",
    );

    // 01/08..30/09 = 61 civil dates: still blocked.
    fireEvent.change(dateInputs()[0], { target: { value: "2026-08-01" } });
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]));
    expect(exportButton).toBeDisabled();
    fireEvent.click(exportButton);
    expect(mocks.fetch).toHaveBeenCalledTimes(3);

    // 02/08..30/09 = 60 civil dates: export allowed.
    fireEvent.change(dateInputs()[0], { target: { value: "2026-08-02" } });
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(4));
    await waitFor(() => expect(exportButton).toBeEnabled());
    expect(screen.queryByText(hint)).not.toBeInTheDocument();
    fireEvent.click(exportButton);
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(5));
    expect(String(mocks.fetch.mock.calls[4][0])).toBe(
      "/v1/handoffs/queue/export?tenant_id=clinic-id&from_date=2026-08-02&to_date=2026-09-30",
    );
  });

  it("leaves a partial range to the server and toasts its verdict", async () => {
    mocks.fetch.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/export?")) {
        return {
          ok: false,
          status: 400,
          json: async () => ({ detail: "export_lookback_6_months" }),
        } as Response;
      }
      return queueResponse();
    });
    renderPage();
    await waitFor(() => expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]));
    const exportButton = screen.getByRole("button", { name: "Exportar CSV" });

    fireEvent.change(dateInputs()[0], { target: { value: "2026-03-01" } });
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(cardIds()).toEqual(["a", "b", "c", "d", "e"]));
    expect(exportButton).toBeDisabled();

    fireEvent.change(dateInputs()[1], { target: { value: "" } });
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(3));
    expect(String(mocks.fetch.mock.calls[2][0])).toBe(
      "/v1/handoffs/queue?tenant_id=clinic-id&from_date=2026-03-01",
    );
    await waitFor(() => expect(exportButton).toBeEnabled());
    expect(
      screen.queryByText(
        "O período selecionado passa de 60 dias. Reduza o intervalo para exportar.",
      ),
    ).not.toBeInTheDocument();

    fireEvent.click(exportButton);
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(4));
    expect(String(mocks.fetch.mock.calls[3][0])).toBe(
      "/v1/handoffs/queue/export?tenant_id=clinic-id&from_date=2026-03-01",
    );
    await waitFor(() =>
      expect(mocks.toast).toHaveBeenCalledWith({
        title:
          "A exportação alcança no máximo 6 meses atrás. Escolha uma data inicial mais recente.",
        variant: "destructive",
      }),
    );
  });

  it("keeps investor filtering local and does not reveal internal stage values", async () => {
    Object.assign(mocks.auth, { isInvestor: true, tenantId: null });
    renderPage();

    await waitFor(() =>
      expect(screen.getAllByTestId("handoff-card")).toHaveLength(6),
    );
    fireEvent.change(
      screen.getByRole("combobox", { name: "Grupo de intenção" }),
      {
        target: { value: "pedido_preco" },
      },
    );
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "Somente com sinalização de segurança",
      }),
    );

    expect(screen.getAllByTestId("handoff-card")).toHaveLength(1);
    expect(
      screen.queryByText("agendamento_solicitado"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("em_qualificacao")).not.toBeInTheDocument();
    expect(mocks.fetch).not.toHaveBeenCalled();
  });
});
