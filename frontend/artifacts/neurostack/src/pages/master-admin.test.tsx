import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@/context/i18n";
import type { Txt } from "@/components/billing/format";
import MasterAdmin from "./master-admin";
import { paymentNotice } from "./billing-view";
import { formatInstant } from "@/components/billing/format";

// "Assinaturas por Builder" (DESIGN creditos-cortesia, decision 12 and the
// Success Criteria "Tabela 'Assinaturas por Builder'").

const mocks = vi.hoisted(() => ({
  fetch: vi.fn(),
  setViewingClinicId: vi.fn(),
  toast: vi.fn(),
}));

vi.mock("@/context/auth", () => ({
  useAuth: () => ({
    clinicId: null,
    isSuperAdmin: true,
    isInvestor: false,
    setViewingClinicId: mocks.setViewingClinicId,
  }),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mocks.toast }),
}));

const ptTxt: Txt = (pt) => pt;
const enTxt: Txt = (_pt, en) => en;

const ACTIVE_COURTESY = { active_expires_at: "2026-12-09T03:00:00Z", available_mc: 15_000_000 };

/** Every row keeps the legacy fields, with the legacy default `trial`. */
function row(id: string, name: string, extra: Record<string, unknown>) {
  return {
    id,
    name,
    subscription_plan: "pro",
    subscription_status: "trial",
    billing_cycle: "monthly",
    updated_at: null,
    subscription: null,
    subscription_unavailable: false,
    courtesy: null,
    charging: null,
    courtesy_rule: null,
    ...extra,
  };
}

function subscription(status: string, paymentState: string, periodEnd: string | null = "2026-12-05") {
  return {
    offer_id: "plan_pro_monthly_v1",
    catalog_version_id: "payment-2026-10-03",
    status,
    amount_cents: 29700,
    current_period_end: periodEnd,
    payment_state: paymentState,
  };
}

const rows = [
  row("a-paid", "Builder A Pago", { subscription: subscription("active", "paid"), courtesy: ACTIVE_COURTESY }),
  row("a-overdue", "Builder A Atraso", { subscription: subscription("active_linked", "overdue") }),
  row("a-reversed", "Builder A Estorno", { subscription: subscription("active", "reversed") }),
  row("b", "Builder B Cancelado", { subscription: subscription("cancelled", "paid"), courtesy: ACTIVE_COURTESY }),
  // Owner decision 2026-11-10: state b also carries the /billing payment notice.
  row("b-overdue", "Builder B Atraso", { subscription: subscription("cancelled", "overdue") }),
  row("b-reversed", "Builder B Estorno", { subscription: subscription("cancelled", "reversed") }),
  row("c-readback", "Builder C Readback", { subscription: subscription("readback_required", "paid") }),
  row("c-pending", "Builder C Pagamento", { subscription: subscription("active", "pending"), courtesy: ACTIVE_COURTESY }),
  // A cancelled record whose payment is not confirmed is c, not b: c wins.
  row("c-cancelled", "Builder C Cancelado Pendente", { subscription: subscription("cancelled", "pending") }),
  row("d", "Builder D Cortesia", { courtesy: ACTIVE_COURTESY }),
  row("e", "Builder E Sem Plano", { courtesy: { active_expires_at: null, available_mc: 0 } }),
  row("f", "Builder F Indisponivel", { subscription_unavailable: true, courtesy: ACTIVE_COURTESY }),
  row("off", "Builder Credito Desligado", {}),
];

function ok(data: unknown) {
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(data) });
}

function installFetch(subscriptions: unknown[]) {
  mocks.fetch.mockImplementation((input: string | URL | Request) => {
    const url = String(input);
    if (url === "/v1/admin/builders") return ok([]);
    if (url === "/v1/admin/clinics") return ok([]);
    if (url === "/v1/support/tickets") return ok([]);
    if (url === "/v1/demo-leads") return ok([]);
    if (url === "/v1/admin/stats") return ok({});
    if (url === "/v1/admin/dashboard") return ok({ stats: {}, time_series: [], alerts: [] });
    if (url === "/v1/admin/transactions") return ok([]);
    if (url === "/v1/admin/builders/subscriptions") return ok(subscriptions);
    if (url.startsWith("/v1/admin/credits/courtesy-grants?builder_id=")) return ok({ lots: [] });
    throw new Error(`Unexpected fetch: ${url}`);
  });
}

async function openSubscriptionsTable() {
  render(<I18nProvider><MasterAdmin /></I18nProvider>);
  const title = await screen.findByText("Transações Financeiras");
  const card = title.closest("div.rounded-xl, div.rounded-lg, [class*='border-border/60']") as HTMLElement;
  fireEvent.click(within(card).getByRole("button", { name: /Ver/ }));
  const heading = await screen.findByText("Assinaturas por Builder");
  const table = heading.parentElement?.querySelector("table");
  expect(table).not.toBeNull();
  return table as HTMLTableElement;
}

async function rowOf(name: string) {
  const cell = await screen.findByText(name);
  const tr = cell.closest("tr");
  expect(tr).not.toBeNull();
  return within(tr as HTMLElement);
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("ns_locale", "pt");
  localStorage.setItem("ns_auth", JSON.stringify({ token: "master-admin-token" }));
  mocks.fetch.mockReset();
  installFetch(rows);
  vi.stubGlobal("fetch", mocks.fetch);
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  mocks.fetch.mockReset();
  mocks.setViewingClinicId.mockReset();
  mocks.toast.mockReset();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Master Admin: Assinaturas por Builder", () => {
  it("has no Ciclo column and never shows the legacy Trial badge", async () => {
    const table = await openSubscriptionsTable();
    const headers = within(table).getAllByRole("columnheader").map((th) => th.textContent?.trim());
    expect(headers).toEqual(["Builder", "Plano", "Status", "Atualizado", "Ações"]);
    expect(within(table).queryByText("Ciclo")).not.toBeInTheDocument();
    expect(within(table).queryByText(/trial/i)).not.toBeInTheDocument();
    // The legacy plan field is not the plan either.
    expect(within(table).queryByText("pro")).not.toBeInTheDocument();
  });

  it("state a: Ativo with the plan name, and the /billing payment notice when overdue or reversed", async () => {
    await openSubscriptionsTable();

    const paid = await rowOf("Builder A Pago");
    expect(paid.getByText("Pro")).toBeInTheDocument();
    expect(paid.getByText("Ativo")).toBeInTheDocument();
    expect(paid.queryByText(paymentNotice("overdue", ptTxt) as string)).not.toBeInTheDocument();
    expect(paid.getByText("Cortesia, expira em 09/12")).toBeInTheDocument();

    const overdue = await rowOf("Builder A Atraso");
    expect(overdue.getByText("Ativo")).toBeInTheDocument();
    expect(overdue.getByText(paymentNotice("overdue", ptTxt) as string)).toBeInTheDocument();

    const reversed = await rowOf("Builder A Estorno");
    expect(reversed.getByText("Ativo")).toBeInTheDocument();
    expect(reversed.getByText(paymentNotice("reversed", ptTxt) as string)).toBeInTheDocument();
  });

  it("state b: Cancelado, vale até the paid period end", async () => {
    await openSubscriptionsTable();
    const cancelled = await rowOf("Builder B Cancelado");
    expect(cancelled.getByText("Pro")).toBeInTheDocument();
    expect(cancelled.getByText("Cancelado, vale até 05/12/2026")).toBeInTheDocument();
    expect(cancelled.getByText("Cortesia, expira em 09/12")).toBeInTheDocument();
    expect(cancelled.queryByText("Ativo")).not.toBeInTheDocument();
    // Paid: no payment notice of any kind.
    for (const state of ["overdue", "reversed", "pending"]) {
      expect(cancelled.queryByText(paymentNotice(state, ptTxt) as string)).not.toBeInTheDocument();
    }
  });

  it("state b: the /billing payment notice under the label when overdue or reversed", async () => {
    await openSubscriptionsTable();
    const overdue = await rowOf("Builder B Atraso");
    expect(overdue.getByText("Cancelado, vale até 05/12/2026")).toBeInTheDocument();
    expect(overdue.getByText(paymentNotice("overdue", ptTxt) as string)).toBeInTheDocument();
    expect(overdue.queryByText(paymentNotice("reversed", ptTxt) as string)).not.toBeInTheDocument();
    expect(overdue.queryByText("Ativo")).not.toBeInTheDocument();

    const reversed = await rowOf("Builder B Estorno");
    expect(reversed.getByText("Cancelado, vale até 05/12/2026")).toBeInTheDocument();
    expect(reversed.getByText(paymentNotice("reversed", ptTxt) as string)).toBeInTheDocument();
    expect(reversed.queryByText(paymentNotice("overdue", ptTxt) as string)).not.toBeInTheDocument();
  });

  it("state c: Pendente for an unconfirmed record or a pending payment", async () => {
    await openSubscriptionsTable();
    const readback = await rowOf("Builder C Readback");
    expect(readback.getByText("Pro")).toBeInTheDocument();
    expect(readback.getByText("Pendente")).toBeInTheDocument();
    expect(readback.queryByText(/Cortesia/)).not.toBeInTheDocument();

    const pending = await rowOf("Builder C Pagamento");
    expect(pending.getByText("Pendente")).toBeInTheDocument();
    expect(pending.getByText("Cortesia, expira em 09/12")).toBeInTheDocument();

    const cancelledPending = await rowOf("Builder C Cancelado Pendente");
    expect(cancelledPending.getByText("Pendente")).toBeInTheDocument();
    expect(cancelledPending.queryByText(/Cancelado, vale até/)).not.toBeInTheDocument();
    expect(cancelledPending.queryByText(paymentNotice("pending", ptTxt) as string)).not.toBeInTheDocument();
  });

  it("state d: no subscription with an active courtesy", async () => {
    await openSubscriptionsTable();
    const courtesy = await rowOf("Builder D Cortesia");
    expect(courtesy.getAllByRole("cell")[1]).toHaveTextContent(/^-$/);
    // The status itself is the courtesy: no second badge.
    expect(courtesy.getAllByText("Cortesia, expira em 09/12")).toHaveLength(1);
    expect(courtesy.getByRole("button", { name: "Créditos de Builder D Cortesia" })).toBeInTheDocument();
  });

  it("state e: Sem plano, with the Créditos action while courtesy is not null", async () => {
    await openSubscriptionsTable();
    const none = await rowOf("Builder E Sem Plano");
    expect(none.getByText("Sem plano")).toBeInTheDocument();
    expect(none.queryByText(/Cortesia/)).not.toBeInTheDocument();
    expect(none.getByRole("button", { name: "Créditos de Builder E Sem Plano" })).toBeInTheDocument();
  });

  it("state f: Indisponível, with the courtesy badge", async () => {
    await openSubscriptionsTable();
    const unavailable = await rowOf("Builder F Indisponivel");
    expect(unavailable.getAllByRole("cell")[1]).toHaveTextContent(/^-$/);
    expect(unavailable.getByText("Indisponível")).toBeInTheDocument();
    expect(unavailable.getByText("Cortesia, expira em 09/12")).toBeInTheDocument();
  });

  it("with courtesy null neither the Créditos action nor the courtesy badge appears", async () => {
    await openSubscriptionsTable();
    const off = await rowOf("Builder Credito Desligado");
    expect(off.getByText("Sem plano")).toBeInTheDocument();
    expect(off.queryByRole("button", { name: /Créditos/ })).not.toBeInTheDocument();
    expect(off.queryByText(/Cortesia/)).not.toBeInTheDocument();
  });

  it("a row from a server without the new fields is Indisponível, never the legacy status", async () => {
    installFetch([
      {
        id: "legacy",
        name: "Builder Legado",
        subscription_plan: "pro",
        subscription_status: "active",
        billing_cycle: "monthly",
        updated_at: null,
      },
    ]);
    await openSubscriptionsTable();
    const legacy = await rowOf("Builder Legado");
    expect(legacy.getByText("Indisponível")).toBeInTheDocument();
    expect(legacy.queryByText("Ativo")).not.toBeInTheDocument();
    expect(legacy.queryByRole("button", { name: /Créditos/ })).not.toBeInTheDocument();
  });

  it("the Créditos action opens the builder's courtesy dialog", async () => {
    await openSubscriptionsTable();
    const courtesy = await rowOf("Builder D Cortesia");
    fireEvent.click(courtesy.getByRole("button", { name: "Créditos de Builder D Cortesia" }));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("Créditos de Builder D Cortesia")).toBeInTheDocument();
    expect(mocks.fetch).toHaveBeenCalledWith(
      "/v1/admin/credits/courtesy-grants?builder_id=d",
      expect.objectContaining({ headers: expect.objectContaining({ Authorization: "Bearer master-admin-token" }) }),
    );
  });

  it("in English: the six state labels, the payment notice and the Créditos action", async () => {
    localStorage.setItem("ns_locale", "en");
    await openSubscriptionsTable();
    expect((await rowOf("Builder A Pago")).getByText("Active")).toBeInTheDocument();
    expect((await rowOf("Builder A Pago")).getByText("Courtesy, expires on 12/09")).toBeInTheDocument();
    expect(
      (await rowOf("Builder A Atraso")).getByText(paymentNotice("overdue", enTxt) as string),
    ).toBeInTheDocument();
    expect(
      (await rowOf("Builder A Estorno")).getByText(paymentNotice("reversed", enTxt) as string),
    ).toBeInTheDocument();
    expect((await rowOf("Builder B Cancelado")).getByText("Cancelled, valid until 12/05/2026")).toBeInTheDocument();
    const cancelledOverdue = await rowOf("Builder B Atraso");
    expect(cancelledOverdue.getByText("Cancelled, valid until 12/05/2026")).toBeInTheDocument();
    expect(cancelledOverdue.getByText(paymentNotice("overdue", enTxt) as string)).toBeInTheDocument();
    const cancelledReversed = await rowOf("Builder B Estorno");
    expect(cancelledReversed.getByText("Cancelled, valid until 12/05/2026")).toBeInTheDocument();
    expect(cancelledReversed.getByText(paymentNotice("reversed", enTxt) as string)).toBeInTheDocument();
    expect((await rowOf("Builder C Readback")).getByText("Pending")).toBeInTheDocument();
    expect((await rowOf("Builder D Cortesia")).getAllByText("Courtesy, expires on 12/09")).toHaveLength(1);
    expect((await rowOf("Builder E Sem Plano")).getByText("No plan")).toBeInTheDocument();
    expect((await rowOf("Builder F Indisponivel")).getByText("Unavailable")).toBeInTheDocument();
    expect(
      (await rowOf("Builder D Cortesia")).getByRole("button", { name: "Builder D Cortesia credits" }),
    ).toBeInTheDocument();
  });
});

// cobranca-credito-por-builder Group 5: the read-only "Cobrança ativa" badge.
describe("Master Admin: Cobrança ativa", () => {
  const MARKED_AT = "2026-11-11T12:00:00Z";
  const chargingRows = [
    row("m", "Builder Marcado", {
      subscription: subscription("active", "paid"),
      courtesy: ACTIVE_COURTESY,
      charging: { marked_at: MARKED_AT },
    }),
    row("u", "Builder Nao Marcado", { courtesy: { active_expires_at: null, available_mc: 0 } }),
    // Below 047 or with the switch off the server sends `charging: null`.
    row("off", "Builder Credito Desligado", {}),
  ];

  it("shows the badge only on the marked builder's row, with the mark instant in its title", async () => {
    installFetch(chargingRows);
    await openSubscriptionsTable();
    const marked = await rowOf("Builder Marcado");
    const badge = marked.getByText("Cobrança ativa");
    expect(badge).toHaveAttribute("title", "Cobrança ativa desde 11/11/2026, 09:00");
    // The date is also text (sr-only), not only a hover tooltip.
    const since = marked.getByTestId("charging-since");
    expect(since).toHaveTextContent(/^desde 11\/11\/2026, 09:00$/);
    expect(since).toHaveClass("sr-only");
    // Nothing else of the row changes.
    expect(marked.getByText("Ativo")).toBeInTheDocument();
    expect(marked.getByText("Cortesia, expira em 09/12")).toBeInTheDocument();
    expect(marked.getByRole("button", { name: "Créditos de Builder Marcado" })).toBeInTheDocument();
    for (const name of ["Builder Nao Marcado", "Builder Credito Desligado"]) {
      expect((await rowOf(name)).queryByText(/Cobrança/)).not.toBeInTheDocument();
      expect((await rowOf(name)).queryByTestId("charging-since")).not.toBeInTheDocument();
    }
  });

  it("a row without the field (an older server) has no badge", async () => {
    const { charging: _ignored, ...legacy } = row("old", "Builder Sem Campo", {
      courtesy: { active_expires_at: null, available_mc: 0 },
    });
    installFetch([legacy]);
    await openSubscriptionsTable();
    const old = await rowOf("Builder Sem Campo");
    expect(old.getByText("Sem plano")).toBeInTheDocument();
    expect(old.queryByText(/Cobrança/)).not.toBeInTheDocument();
    expect(old.queryByTestId("charging-since")).not.toBeInTheDocument();
  });

  it("offers no control to mark or unmark: the badge is plain text and nothing calls a marking route", async () => {
    installFetch(chargingRows);
    const table = await openSubscriptionsTable();
    const badge = within(table).getByText("Cobrança ativa");
    expect(badge.closest("button, a, [role='button'], [role='menuitem']")).toBeNull();
    fireEvent.click(badge);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    const controls = [
      ...screen.queryAllByRole("button"),
      ...screen.queryAllByRole("menuitem"),
      ...screen.queryAllByRole("checkbox"),
      ...screen.queryAllByRole("switch"),
      ...screen.queryAllByRole("link"),
    ];
    for (const control of controls) {
      const name = `${control.textContent ?? ""} ${control.getAttribute("aria-label") ?? ""}`;
      expect(name).not.toMatch(/marcar|desmarcar|cobran|unmark|\bmark|charging/i);
    }
    const urls = mocks.fetch.mock.calls.map(([input]) => String(input));
    expect(urls.filter((url) => /mark|enforc|charg|cobran/i.test(url))).toEqual([]);
  });

  it("in English: Charging active, with the mark instant in its title", async () => {
    localStorage.setItem("ns_locale", "en");
    installFetch(chargingRows);
    await openSubscriptionsTable();
    const badge = (await rowOf("Builder Marcado")).getByText("Charging active");
    expect(badge).toHaveAttribute("title", `Charging active since ${formatInstant(MARKED_AT, "en")}`);
    expect((await rowOf("Builder Marcado")).getByTestId("charging-since")).toHaveTextContent(
      `since ${formatInstant(MARKED_AT, "en")}`,
    );
    expect((await rowOf("Builder Nao Marcado")).queryByText("Charging active")).not.toBeInTheDocument();
  });
});

// debito-cortesia-automatico Group 5b: the read-only courtesy rule state,
// independent of the operator mark badge.
describe("Master Admin: regra da cortesia", () => {
  const ruleRows = [
    row("rc", "Builder Cortesia Cobrando", { courtesy: ACTIVE_COURTESY, courtesy_rule: "charged" }),
    row("rb", "Builder Cortesia Bloqueado", {
      courtesy: { active_expires_at: null, available_mc: 0 },
      courtesy_rule: "blocked",
    }),
    // Protected, marked, never courtesy, or the switch off: `null`.
    row("rp", "Builder Protegido", {
      subscription: subscription("active", "paid"),
      courtesy: { active_expires_at: null, available_mc: 0 },
    }),
    row("rm", "Builder Marcado Com Cortesia", {
      courtesy: ACTIVE_COURTESY,
      charging: { marked_at: "2026-11-11T12:00:00Z" },
    }),
    row("rx", "Builder Valor Desconhecido", { courtesy: ACTIVE_COURTESY, courtesy_rule: "a_future_value" }),
  ];

  it("shows Cortesia: cobrando and Cortesia: bloqueado on the rule's rows, nothing for null", async () => {
    installFetch(ruleRows);
    await openSubscriptionsTable();

    const charged = await rowOf("Builder Cortesia Cobrando");
    const chargedBadge = charged.getByTestId("courtesy-rule");
    expect(chargedBadge).toHaveTextContent(/^Cortesia: cobrando$/);
    expect(chargedBadge).toHaveAttribute("data-rule", "charged");
    expect(chargedBadge).toHaveAttribute("title", "Regra da cortesia: o consumo é debitado dos créditos de cortesia.");
    // The rest of the row is unchanged: the courtesy status and no mark badge.
    expect(charged.getByText("Cortesia, expira em 09/12")).toBeInTheDocument();
    expect(charged.queryByText("Cobrança ativa")).not.toBeInTheDocument();

    const blocked = await rowOf("Builder Cortesia Bloqueado");
    const blockedBadge = blocked.getByTestId("courtesy-rule");
    expect(blockedBadge).toHaveTextContent(/^Cortesia: bloqueado$/);
    expect(blockedBadge).toHaveAttribute("data-rule", "blocked");
    expect(blockedBadge).toHaveAttribute(
      "title",
      "Regra da cortesia: sem cortesia e sem plano pago em vigor, o agente está bloqueado.",
    );
    expect(blocked.getByText("Sem plano")).toBeInTheDocument();

    for (const name of ["Builder Protegido", "Builder Marcado Com Cortesia", "Builder Valor Desconhecido"]) {
      const other = await rowOf(name);
      expect(other.queryByTestId("courtesy-rule")).not.toBeInTheDocument();
      expect(other.queryByText(/Cortesia: /)).not.toBeInTheDocument();
    }
  });

  it("is independent of the Cobrança ativa mark badge: both can show, each from its own field", async () => {
    installFetch([
      row("both", "Builder Ambos", {
        courtesy: ACTIVE_COURTESY,
        charging: { marked_at: "2026-11-11T12:00:00Z" },
        courtesy_rule: "charged",
      }),
    ]);
    await openSubscriptionsTable();
    const both = await rowOf("Builder Ambos");
    expect(both.getByText("Cobrança ativa")).toBeInTheDocument();
    expect(both.getByTestId("courtesy-rule")).toHaveTextContent("Cortesia: cobrando");
  });

  it("a row without the field (an older server) shows no rule state", async () => {
    const { courtesy_rule: _ignored, ...legacy } = row("old", "Builder Sem Regra", { courtesy: ACTIVE_COURTESY });
    installFetch([legacy]);
    await openSubscriptionsTable();
    expect((await rowOf("Builder Sem Regra")).queryByTestId("courtesy-rule")).not.toBeInTheDocument();
  });

  it("is read-only text: no control and no write route", async () => {
    installFetch(ruleRows);
    const table = await openSubscriptionsTable();
    const badge = within(table).getByText("Cortesia: bloqueado");
    expect(badge.closest("button, a, [role='button'], [role='menuitem']")).toBeNull();
    fireEvent.click(badge);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    const methods = mocks.fetch.mock.calls.map(([, init]) => (init as RequestInit | undefined)?.method ?? "GET");
    expect(methods.filter((method) => method !== "GET")).toEqual([]);
  });

  it("in English: Courtesy: charging and Courtesy: blocked", async () => {
    localStorage.setItem("ns_locale", "en");
    installFetch(ruleRows);
    await openSubscriptionsTable();
    expect((await rowOf("Builder Cortesia Cobrando")).getByTestId("courtesy-rule")).toHaveTextContent(
      /^Courtesy: charging$/,
    );
    const blocked = (await rowOf("Builder Cortesia Bloqueado")).getByTestId("courtesy-rule");
    expect(blocked).toHaveTextContent(/^Courtesy: blocked$/);
    expect(blocked).toHaveAttribute("title", "Courtesy rule: no courtesy and no paid plan in force, so the agent is blocked.");
  });
});
