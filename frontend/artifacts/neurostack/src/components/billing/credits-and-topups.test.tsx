import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BillingSummary, CreditCatalog, CreditLot, CreditSummary } from "@/pages/billing-contract";
import { deriveTopupState } from "@/pages/billing-view";
import {
  CreditsAndTopups,
  HISTORY_TABLE_CLASSES,
  creditStanding,
  type CreditHistoryState,
  type CreditsPanelState,
  type TopupState,
} from "./credits-and-topups";

// Synthetic ledger answers shaped like `src/api/schemas/credits.py`; every
// figure is invented for the tests.
const AS_OF = "2026-10-30T15:00:00Z";
const LOT_ID = "0f0e0d0c-0000-4000-8000-00000000a001";

function summary(state: Partial<CreditSummary["state"]> = {}, balance: Partial<CreditSummary["balance"]> = {}): CreditSummary {
  const window = { start: "2026-10-25", end: "2026-11-25", kind: "cycle" };
  return {
    as_of: AS_OF,
    state: { as_of: AS_OF, mode: "enforced", enforcement_at: null, state: "active", pending_reasons: [], ...state },
    balance: {
      as_of: AS_OF,
      monthly_available_mc: 7_000,
      monthly_not_started_mc: 0,
      topup_available_mc: 33_000,
      available_mc: 40_000,
      open_debt_mc: 0,
      net_available_mc: 40_000,
      reserved_mc: 0,
      ...balance,
    },
    expiries: [],
    debt: { as_of: AS_OF, open_debt_mc: 0, debts: [] },
    reservations: {
      as_of: AS_OF,
      window,
      reservations: [
        { category: "rag", units: 0, coverage_mc: 0, charged_mc: 0 },
        { category: "media", units: 0, coverage_mc: 0, charged_mc: 0 },
        { category: "egress", units: 0, coverage_mc: 0, charged_mc: 0 },
      ],
      egress_allowance_bytes: 0,
      egress_delivered_bytes: 0,
    },
    thresholds: { as_of: AS_OF, capacity_mc: 40_000, available_mc: 40_000, used_mc: 0, used_percent: 0, reached: [], alerts: [] },
    consumption: { as_of: AS_OF, mode: "enforced", window, charged_mc: 0, charged: [], observed_mc: 0, observed: [] },
  };
}

function lot(overrides: Partial<CreditLot> = {}): CreditLot {
  return {
    lot_id: LOT_ID,
    kind: "topup",
    status: "active",
    granted_mc: 33_000,
    available_mc: 33_000,
    consumed_mc: 0,
    reversed_mc: 0,
    expired_mc: 0,
    paid_at: "2026-10-12",
    expires_at: "2027-04-12T03:00:00Z",
    ...overrides,
  };
}

const READY_HISTORY: CreditHistoryState = { status: "ready", lots: [lot()] };
const HIDDEN: TopupState = { status: "hidden" };
const OFFERED: TopupState = {
  status: "offered",
  packages: [
    { offerId: "package_credits_500_v1", credits: 500, amountCents: 1_234, currency: "BRL", validityMonths: 6, methods: ["credit_card", "pix"] },
    { offerId: "package_credits_1000_v1", credits: 1000, amountCents: 2_468, currency: "BRL", validityMonths: 3, methods: ["credit_card"] },
  ],
};

function renderPanel(
  props: Partial<{ state: CreditsPanelState; history: CreditHistoryState; topup: TopupState; locale: "pt" | "en" }> & {
    onBuy?: (offerId: string, method: "credit_card" | "pix") => void;
    onRetry?: () => void;
    onRetryHistory?: () => void;
    buyError?: string | null;
  } = {},
) {
  return render(
    <CreditsAndTopups
      state={props.state ?? { status: "ready", summary: summary() }}
      history={props.history ?? READY_HISTORY}
      topup={props.topup ?? HIDDEN}
      locale={props.locale ?? "pt"}
      onBuy={props.onBuy}
      onRetry={props.onRetry}
      onRetryHistory={props.onRetryHistory}
      buyError={props.buyError}
    />,
  );
}

afterEach(cleanup);

describe("<CreditsAndTopups /> state matrix", () => {
  it("gives loading, forbidden, error and zero distinct content and semantics", () => {
    // `credit_unavailable` has no state here: the page omits the whole area.
    const texts: string[] = [];
    for (const state of [{ status: "loading" }, { status: "forbidden" }, { status: "error" }] as CreditsPanelState[]) {
      renderPanel({ state });
      const root = screen.getByTestId("credits-and-topups");
      expect(root).toHaveAttribute("data-state", state.status);
      // None of them shows a figure.
      expect(root.textContent ?? "").not.toMatch(/\d/);
      texts.push(root.textContent ?? "");
      cleanup();
    }
    expect(new Set(texts).size).toBe(3);

    renderPanel({ state: { status: "loading" } });
    expect(screen.getByTestId("credits-and-topups")).toHaveAttribute("role", "status");
    cleanup();
    renderPanel({ state: { status: "forbidden" } });
    expect(screen.getByTestId("credits-and-topups")).not.toHaveAttribute("role");
    cleanup();
    const onRetry = vi.fn();
    renderPanel({ state: { status: "error" }, onRetry });
    expect(screen.getByTestId("credits-and-topups")).toHaveAttribute("role", "alert");
    fireEvent.click(screen.getByRole("button", { name: "Tentar novamente" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    cleanup();

    // Zero is data: a real "0 créditos", not one of the states above.
    renderPanel({
      state: { status: "ready", summary: summary({}, { monthly_available_mc: 0, topup_available_mc: 0, net_available_mc: 0, available_mc: 0 }) },
    });
    expect(screen.getByRole("group", { name: "Disponível líquido" })).toHaveTextContent(/^Disponível líquido0 créditos$/);
  });

  it("shows every pending reason as text, holds the balance and fails closed for an unknown reason", () => {
    const cases: Array<[string[], RegExp]> = [
      [["quarantine"], /um pagamento está em verificação/],
      [["reconciliation"], /em conciliação pelo servidor/],
      [["quarantine", "reconciliation"], /em verificação.*em conciliação/],
      [["a_future_reason"], /^PendentePendência financeira em verificação pelo servidor\./],
      [[], /^PendentePendência financeira em verificação pelo servidor\./],
    ];
    for (const [pending_reasons, line] of cases) {
      renderPanel({ state: { status: "ready", summary: summary({ state: "pending", pending_reasons }) } });
      const notice = screen.getByTestId("credit-state");
      expect(notice).toHaveAttribute("data-state", "pending");
      expect(notice).toHaveTextContent(line);
      // The truth next to listed grants: server records, not available balance.
      expect(notice).toHaveTextContent(/Os lotes do histórico abaixo são registros do servidor, não saldo disponível\./);
      expect(notice).not.toHaveTextContent(/nenhum crédito é concedido/);
      expect(screen.queryByRole("group", { name: "Disponível líquido" })).not.toBeInTheDocument();
      expect(screen.queryByText(/40 créditos|33 créditos/)).toBeInTheDocument(); // history only: the grant record
      expect(screen.getByTestId("credit-panel").textContent ?? "").not.toMatch(/\d+\s*créditos/);
      cleanup();
    }
  });

  it("classifies the ledger states for presentation", () => {
    expect(creditStanding(summary().state)).toBe("standing");
    expect(creditStanding(summary({ state: "grace" }).state)).toBe("standing");
    expect(creditStanding(summary({ state: "pending", pending_reasons: ["standalone"] }).state)).toBe("not_applicable");
    expect(creditStanding(summary({ state: "pending", pending_reasons: ["standalone", "quarantine"] }).state)).toBe("not_applicable");
    expect(creditStanding(summary({ state: "pending", pending_reasons: ["x"] }).state)).toBe("pending");
    for (const state of ["grace", "suspended", "deletion_due"] as const) {
      for (const reason of ["quarantine", "reconciliation", "x"]) {
        expect(creditStanding(summary({ state, pending_reasons: [reason] }).state)).toBe("pending");
      }
    }
  });

  it.each(
    (["grace", "suspended", "deletion_due"] as const).flatMap((state) =>
      [
        ["quarantine", /um pagamento está em verificação/],
        ["reconciliation", /em conciliação pelo servidor/],
        ["a_future_reason", /Pendência financeira em verificação pelo servidor\./],
      ].map(([reason, line]) => [state, reason as string, line as RegExp] as const),
    ),
  )("holds the balance in %s with the reason %s, keeping the deadline and export dates", (state, reason, line) => {
    renderPanel({
      topup: { status: "blocked", reason: "Motivo sintético." },
      state: {
        status: "ready",
        summary: summary({
          state,
          pending_reasons: [reason],
          suspension_due_at: "2026-11-01T12:00:00Z",
          export_deadline_at: "2026-11-28T12:00:00Z",
        }),
      },
    });
    const notice = screen.getByTestId("credit-state");
    expect(notice).toHaveAttribute("data-state", "pending");
    expect(notice).toHaveAttribute("data-lifecycle", state);
    expect(notice).toHaveTextContent(line);
    expect(notice).toHaveTextContent(/O saldo fica retido/);
    const lifecycle = screen.getByTestId("credit-hold-lifecycle");
    expect(lifecycle).toHaveTextContent(/Exporte seus dados até 28\/11\/2026/);
    expect(lifecycle).toHaveTextContent(
      state === "grace" ? /dívida até 01\/11\/2026/ : state === "suspended" ? /Serviço suspenso/ : /Prazo de retenção encerrado/,
    );
    // No top-up call to action next to a blocked top-up.
    expect(notice).not.toHaveTextContent(/Recarregue/);
    // Balance withheld; the blocked top-up shows only its reason.
    expect(screen.queryByRole("group", { name: "Disponível líquido" })).not.toBeInTheDocument();
    expect(screen.getByTestId("credit-panel").textContent ?? "").not.toMatch(/\d+\s*créditos/);
    expect(screen.queryByRole("button", { name: "Recarregar" })).not.toBeInTheDocument();
    expect(screen.getByTestId("topup-blocked")).toHaveTextContent("Motivo sintético.");
  });

  it("shows a standalone tenant as not applicable, with no balance and no history", () => {
    renderPanel({ state: { status: "ready", summary: summary({ state: "pending", pending_reasons: ["standalone"] }, { net_available_mc: 0 }) } });
    expect(screen.getByTestId("credit-state")).toHaveTextContent(/^Não se aplica:/);
    expect(screen.queryByTestId("credit-history")).not.toBeInTheDocument();
    expect(screen.getByTestId("credits-and-topups").textContent ?? "").not.toMatch(/\d+\s*créditos/);
  });

  // The panel fed by the real `deriveTopupState`: flag on, packages listed and
  // read, yet a not-applicable ledger shows no top-up action and no hold
  // notice next to "Não se aplica", in every lifecycle state.
  it.each(["pending", "active", "grace", "suspended", "deletion_due"] as const)(
    "shows a not-applicable ledger in %s without a top-up action or a top-up notice",
    (lifecycleState) => {
      const ledger = summary({ state: lifecycleState, pending_reasons: ["standalone"] }, { net_available_mc: 0 });
      const offerId = "package_credits_500_v1";
      const topup = deriveTopupState({
        summary: {
          status: "ok",
          data: {
            topup_commercial_enabled: true,
            checkout: { available: true, available_offer_ids: [offerId], live_session: null },
          } as unknown as BillingSummary,
        },
        credit: { status: "ok", data: ledger },
        creditCatalog: {
          status: "ok",
          data: {
            millicredits_per_credit: 1000,
            packages: [
              { offer_id: offerId, credits: 500, amount_cents: 1_234, currency: "BRL", payment_methods: ["pix"], validity_months: 6 },
            ],
          } as unknown as CreditCatalog,
        },
        checkoutLive: false,
        txt: (pt) => pt,
      });
      renderPanel({ state: { status: "ready", summary: ledger }, topup });
      expect(screen.getByTestId("credit-state")).toHaveTextContent(/^Não se aplica:/);
      const panel = screen.getByTestId("credits-and-topups");
      expect(panel.textContent ?? "").not.toMatch(/Recarga indisponível/);
      expect(screen.queryByTestId("topup-blocked")).not.toBeInTheDocument();
      expect(screen.queryByTestId("topup-offer")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Recarregar" })).not.toBeInTheDocument();
    },
  );

  it("keeps the deadline notice of a standing account and its balance", () => {
    renderPanel({
      topup: OFFERED,
      state: {
        status: "ready",
        summary: summary({ state: "grace", suspension_due_at: "2026-11-01T12:00:00Z", export_deadline_at: "2026-11-28T12:00:00Z" }),
      },
    });
    const notice = screen.getByTestId("credit-state");
    expect(screen.getByTestId("credit-notices")).toContainElement(notice);
    expect(notice).toHaveTextContent(/até 01\/11\/2026, 09:00\. Recarregue para evitar a suspensão\. Exporte seus dados até 28\/11\/2026/);
    expect(screen.getByRole("group", { name: "Disponível líquido" })).toHaveTextContent("40 créditos");
  });
});

describe("<CreditsAndTopups /> top-up call to action", () => {
  const lifecycle = (state: "grace" | "suspended" | "deletion_due") =>
    summary({ state, suspension_due_at: "2026-11-01T12:00:00Z", export_deadline_at: "2026-11-28T12:00:00Z" });

  it.each(["grace", "suspended", "deletion_due"] as const)(
    "tells the user to top up in %s only when Recarregar exists",
    (state) => {
      renderPanel({ state: { status: "ready", summary: lifecycle(state) }, topup: OFFERED });
      expect(screen.getByTestId("credit-state")).toHaveTextContent(/Recarregue para/);
      expect(screen.getByRole("button", { name: "Recarregar" })).toBeInTheDocument();
      cleanup();
      for (const topup of [HIDDEN, { status: "blocked", reason: "Motivo sintético." }] as TopupState[]) {
        renderPanel({ state: { status: "ready", summary: lifecycle(state) }, topup });
        const notice = screen.getByTestId("credit-state");
        expect(notice).not.toHaveTextContent(/Recarregue/);
        // The deadline and export date stay: they are true.
        expect(notice).toHaveTextContent(/Exporte seus dados até 28\/11\/2026/);
        expect(screen.queryByRole("button", { name: "Recarregar" })).not.toBeInTheDocument();
        cleanup();
      }
    },
  );

  it("speaks English without a call to action when no top-up exists", () => {
    renderPanel({ locale: "en", state: { status: "ready", summary: lifecycle("suspended") }, topup: HIDDEN });
    expect(screen.getByTestId("credit-state")).toHaveTextContent(/^Service suspended for lack of credits\. Export your data by/);
  });
});

describe("<CreditsAndTopups /> net available tone", () => {
  it("shows a zero net available in the neutral foreground and a positive one in green", () => {
    renderPanel({ state: { status: "ready", summary: summary({}, { net_available_mc: 0 }) } });
    const zero = within(screen.getByRole("group", { name: "Disponível líquido" })).getByText("0 créditos");
    expect(zero.className).toContain("text-foreground");
    expect(zero.className).not.toMatch(/emerald/);
    cleanup();
    renderPanel({ state: { status: "ready", summary: summary({}, { net_available_mc: 40_000 }) } });
    const positive = within(screen.getByRole("group", { name: "Disponível líquido" })).getByText("40 créditos");
    expect(positive.className).toContain("text-emerald-700");
  });
});

describe("<CreditsAndTopups /> semantics", () => {
  it("names the balance and the history with h3 headings under the area h2", () => {
    renderPanel();
    expect(screen.getByRole("heading", { level: 3, name: "Saldo de créditos" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "Histórico de créditos" })).toBeInTheDocument();
  });

  it("writes the state notices into a polite region that exists before them", () => {
    const { rerender } = renderPanel();
    const region = screen.getByTestId("credit-notices");
    expect(region).toHaveAttribute("role", "status");
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(region).toBeEmptyDOMElement();
    rerender(
      <CreditsAndTopups
        state={{ status: "ready", summary: summary({ state: "pending", pending_reasons: ["quarantine"] }) }}
        history={READY_HISTORY}
        topup={HIDDEN}
        locale="pt"
      />,
    );
    // The same node received the notice: it was not inserted already filled.
    expect(screen.getByTestId("credit-notices")).toBe(region);
    expect(region).toContainElement(screen.getByTestId("credit-state"));
  });
});

describe("<CreditsAndTopups /> history", () => {
  it("distinguishes loading, error and empty history", () => {
    renderPanel({ history: { status: "loading" } });
    expect(within(screen.getByTestId("credit-history")).getByRole("status")).toHaveTextContent(/Carregando o histórico/);
    cleanup();
    const onRetryHistory = vi.fn();
    renderPanel({ history: { status: "error" }, onRetryHistory });
    const alert = within(screen.getByTestId("credit-history")).getByRole("alert");
    fireEvent.click(within(alert).getByRole("button", { name: "Tentar novamente" }));
    expect(onRetryHistory).toHaveBeenCalledTimes(1);
    cleanup();
    renderPanel({ history: { status: "ready", lots: [] } });
    expect(screen.getByTestId("credit-history-empty")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Histórico de créditos" })).not.toBeInTheDocument();
  });

  it("renders one collection as a table that becomes cards below 36rem, without the internal id", () => {
    renderPanel({ history: { status: "ready", lots: [lot(), lot({ lot_id: "other", kind: "monthly", status: "exhausted", granted_mc: 0, paid_at: "2026-09-25" })] } });
    const table = screen.getByRole("table", { name: "Histórico de créditos" });
    expect(table.className).toBe(HISTORY_TABLE_CLASSES.table);
    const rows = within(table).getAllByTestId("credit-history-row");
    expect(rows).toHaveLength(2);
    expect(rows[0].className).toBe(HISTORY_TABLE_CLASSES.row);
    // Each card cell carries its own label (hidden where the header exists).
    expect(within(rows[0]).getAllByText("Situação")[0].className).toBe(HISTORY_TABLE_CLASSES.cellLabel);
    expect(rows[0]).toHaveTextContent(/12\/10\/2026.*Recarga.*33 créditos.*Em vigor.*12\/04\/2027/);
    // A zero grant is data.
    expect(rows[1]).toHaveTextContent(/Franquia mensal.*0 créditos.*Esgotado/);
    expect(document.body.innerHTML).not.toContain(LOT_ID);
  });

  it("never reads a lot as spendable while the ledger is on hold", () => {
    const lots = [
      lot({ lot_id: "a", status: "active", paid_at: "2026-10-04" }),
      lot({ lot_id: "b", status: "not_started", paid_at: "2026-10-03" }),
      lot({ lot_id: "c", status: "exhausted", paid_at: "2026-10-02" }),
      lot({ lot_id: "d", status: "expired", paid_at: "2026-10-01" }),
    ];
    for (const state of [
      { state: "pending" as const, pending_reasons: ["reconciliation"] },
      { state: "grace" as const, pending_reasons: ["quarantine"] },
    ]) {
      renderPanel({ state: { status: "ready", summary: summary(state) }, history: { status: "ready", lots } });
      const labels = screen.getAllByTestId("credit-lot-status");
      expect(labels.map((label) => label.textContent)).toEqual(["Retido", "Retido", "Esgotado", "Vencido"]);
      // Icon and text, not colour only.
      expect(labels[0].querySelector("svg")).not.toBeNull();
      expect(screen.queryByText("Em vigor")).not.toBeInTheDocument();
      expect(screen.queryByText("Ainda não iniciado")).not.toBeInTheDocument();
      cleanup();
    }
    // Without a hold the same lots keep their own labels.
    renderPanel({ history: { status: "ready", lots } });
    expect(screen.getAllByTestId("credit-lot-status").map((label) => label.textContent)).toEqual([
      "Em vigor",
      "Ainda não iniciado",
      "Esgotado",
      "Vencido",
    ]);
    cleanup();
    renderPanel({ locale: "en", state: { status: "ready", summary: summary({ state: "pending", pending_reasons: ["quarantine"] }) }, history: { status: "ready", lots } });
    expect(screen.getAllByTestId("credit-lot-status")[0]).toHaveTextContent("On hold");
  });

  it("writes a missing date the same way everywhere", () => {
    renderPanel({ history: { status: "ready", lots: [lot({ paid_at: "not-a-date", expires_at: "not-a-date" })] } });
    const row = screen.getByTestId("credit-history-row");
    expect(within(row).getAllByText("data não informada")).toHaveLength(2);
  });

  it("speaks English with the same structure", () => {
    renderPanel({ locale: "en", history: { status: "ready", lots: [lot({ status: "not_started" })] } });
    const table = screen.getByRole("table", { name: "Credit history" });
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["Date", "Origin", "Credits", "Status", "Expires on"]);
    expect(within(table).getByTestId("credit-history-row")).toHaveTextContent(/10\/12\/2026.*Top-up.*33 credits.*Not started yet.*04\/12\/2027/);
  });
});

describe("<CreditsAndTopups /> top-ups", () => {
  it("renders nothing for a hidden top-up and only a textual reason for a blocked one", () => {
    renderPanel({ topup: HIDDEN });
    expect(screen.queryByTestId("topup-offer")).not.toBeInTheDocument();
    expect(screen.queryByTestId("topup-blocked")).not.toBeInTheDocument();
    cleanup();
    renderPanel({ topup: { status: "blocked", reason: "Motivo sintético." } });
    expect(screen.getByTestId("topup-blocked")).toHaveTextContent("Motivo sintético.");
    expect(screen.queryByRole("button", { name: /Recarregar/ })).not.toBeInTheDocument();
  });

  it("opens the chooser on Recarregar, moves focus into it and buys only an offered package and method", async () => {
    const onBuy = vi.fn();
    renderPanel({ topup: OFFERED, onBuy });

    const open = screen.getByRole("button", { name: "Recarregar" });
    expect(open).toHaveAttribute("aria-expanded", "false");
    // Closed, nothing is controlled yet: no dangling `aria-controls`.
    expect(open).not.toHaveAttribute("aria-controls");
    fireEvent.click(open);
    const heading = screen.getByRole("heading", { level: 3, name: "Recarregar créditos" });
    await waitFor(() => expect(document.activeElement).toBe(heading));

    const packages = screen.getByRole("group", { name: "Pacote" });
    const labels = within(packages).getAllByRole("button").map((b) => b.textContent?.replace(/ /g, " "));
    expect(labels).toEqual(["500 créditosR$ 12,34 · válida por 6 meses", "1.000 créditosR$ 24,68 · válida por 3 meses"]);
    // No method before a package; the action names its reason while disabled.
    expect(screen.queryByRole("group", { name: "Forma de pagamento" })).not.toBeInTheDocument();
    const buy = screen.getByRole("button", { name: "Ir para o pagamento da recarga" });
    expect(buy).toBeDisabled();
    expect(buy).toHaveAccessibleDescription(/Escolha um pacote/);

    fireEvent.click(within(packages).getByRole("button", { name: /^500 créditos/ }));
    fireEvent.click(screen.getByRole("button", { name: "PIX" }));
    // Switching to a card-only package drops the PIX choice.
    fireEvent.click(within(packages).getByRole("button", { name: /^1\.000 créditos/ }));
    expect(screen.queryByRole("button", { name: "PIX" })).not.toBeInTheDocument();
    expect(buy).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Cartão de crédito" }));
    expect(buy).toBeEnabled();
    expect(buy).not.toHaveAttribute("aria-describedby");
    fireEvent.click(buy);
    expect(onBuy).toHaveBeenCalledWith("package_credits_1000_v1", "credit_card");

    fireEvent.click(screen.getByRole("button", { name: "Fechar recarga" }));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Recarregar" })));
  });

  it("closes the top-up panel on Escape and returns focus to Recarregar", async () => {
    renderPanel({ topup: OFFERED });
    fireEvent.click(screen.getByRole("button", { name: "Recarregar" }));
    const heading = screen.getByRole("heading", { level: 3, name: "Recarregar créditos" });
    await waitFor(() => expect(document.activeElement).toBe(heading));
    fireEvent.keyDown(heading, { key: "Escape" });
    expect(screen.queryByRole("heading", { level: 3, name: "Recarregar créditos" })).not.toBeInTheDocument();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Recarregar" })));
  });

  it("announces a purchase error", () => {
    renderPanel({ topup: OFFERED, buyError: "Recusado." });
    fireEvent.click(screen.getByRole("button", { name: "Recarregar" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Recusado.");
  });

  it("speaks English for the top-up action", () => {
    renderPanel({ topup: OFFERED, locale: "en" });
    fireEvent.click(screen.getByRole("button", { name: "Top up" }));
    expect(screen.getByRole("group", { name: "Package" })).toHaveTextContent(/500 credits.*valid for 6 months/);
    expect(screen.getByRole("button", { name: "Continue to top-up payment" })).toHaveAccessibleDescription(/Choose a package/);
  });
});
