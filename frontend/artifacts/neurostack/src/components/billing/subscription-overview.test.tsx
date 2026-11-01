import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { formatMoney } from "./format";
import {
  SubscriptionOverview,
  type PlanTerms,
  type SubscriptionOverviewState,
  type SubscriptionView,
} from "./subscription-overview";

// Synthetic contract-shaped props; every value is invented for the tests.
const terms: PlanTerms = {
  status: "ready",
  currency: "BRL",
  credits: 4_321,
  includedClinics: 3,
  additionalClinicAmountCents: 13_500,
};

function subscription(overrides: Partial<SubscriptionView> = {}): SubscriptionView {
  return {
    planName: "Pro",
    status: "active",
    paymentState: "paid",
    amountCents: 246_800,
    currentPeriodEnd: "2026-12-05",
    ...overrides,
  };
}

function ready(overrides: Partial<Extract<SubscriptionOverviewState, { status: "ready" }>> = {}): SubscriptionOverviewState {
  return { status: "ready", subscription: subscription(), activeClinics: 5, terms, ...overrides };
}

function money(cents: number, locale: "pt" | "en" = "pt") {
  return (formatMoney(cents, "BRL", locale) ?? "").replace(/ /g, " ");
}

function field(name: string) {
  return screen.getByRole("group", { name });
}

describe("<SubscriptionOverview />", () => {
  afterEach(cleanup);

  it("presents plan, status, cycle, price, renewal, credits and clinics from props", () => {
    render(<SubscriptionOverview state={ready()} locale="pt" />);

    expect(screen.getByText("NeuroStack Pro")).toBeInTheDocument();
    expect(screen.getByTestId("subscription-status")).toHaveTextContent("Ativa · Pagamento em dia");
    expect(field("Ciclo")).toHaveTextContent("Mensal");
    expect(field("Mensalidade")).toHaveTextContent(money(246_800));
    expect(field("Próxima renovação")).toHaveTextContent("05/12/2026");
    expect(field("Próxima renovação")).toHaveTextContent("Fim do período pago");
    expect(field("Créditos por mês")).toHaveTextContent("4.321 créditos");
    expect(field("Clínicas incluídas")).toHaveTextContent(/^Clínicas incluídas3$/);
    expect(field("Clínicas ativas")).toHaveTextContent(/^Clínicas ativas5$/);
    expect(field("Preço por clínica adicional/mês")).toHaveTextContent(money(13_500));
  });

  it("follows different values without any component change", () => {
    render(
      <SubscriptionOverview
        state={ready({
          subscription: subscription({ planName: "Max", amountCents: 1_000, currentPeriodEnd: "2027-02-28" }),
          activeClinics: 11,
          terms: { status: "ready", currency: "BRL", credits: 9, includedClinics: 8, additionalClinicAmountCents: 700 },
        })}
        locale="pt"
      />,
    );

    expect(screen.getByText("NeuroStack Max")).toBeInTheDocument();
    expect(field("Mensalidade")).toHaveTextContent(money(1_000));
    expect(field("Próxima renovação")).toHaveTextContent("28/02/2027");
    expect(field("Créditos por mês")).toHaveTextContent("9 créditos");
    expect(field("Clínicas ativas")).toHaveTextContent("11");
    expect(document.body.textContent ?? "").not.toMatch(/4\.321|2\.468|05\/12\/2026/);
  });

  it("shows zero as data", () => {
    render(
      <SubscriptionOverview
        state={ready({ activeClinics: 0, terms: { ...terms, status: "ready", includedClinics: 0, additionalClinicAmountCents: 0 } })}
        locale="pt"
      />,
    );
    expect(field("Clínicas ativas")).toHaveTextContent(/^Clínicas ativas0$/);
    expect(field("Clínicas incluídas")).toHaveTextContent(/^Clínicas incluídas0$/);
    expect(field("Preço por clínica adicional/mês")).toHaveTextContent(money(0));
    expect(document.body.textContent ?? "").not.toContain("—");
  });

  it("keeps absent, not applicable, unavailable and loading values distinct", () => {
    const { rerender } = render(
      <SubscriptionOverview state={ready({ subscription: subscription({ currentPeriodEnd: null }) })} locale="pt" />,
    );
    const absent = field("Próxima renovação").textContent;
    expect(absent).toContain("Nenhum ciclo pago ainda");

    rerender(
      <SubscriptionOverview state={ready({ subscription: subscription({ status: "cancelled" }) })} locale="pt" />,
    );
    const notApplicable = field("Próxima renovação").textContent;
    expect(notApplicable).toContain("Não se aplica: assinatura cancelada");
    expect(notApplicable).toContain("Acesso pago até 05/12/2026");
    expect(screen.getByTestId("subscription-status")).toHaveTextContent("Cancelada");

    rerender(<SubscriptionOverview state={ready({ terms: { status: "unavailable" } })} locale="pt" />);
    const unavailable = field("Créditos por mês").textContent;
    expect(unavailable).toContain("Indisponível");
    expect(field("Mensalidade")).toHaveTextContent("Indisponível");
    expect(field("Clínicas incluídas")).toHaveTextContent("Indisponível");
    // Clinics counted by the server stay visible: they do not depend on the catalog.
    expect(field("Clínicas ativas")).toHaveTextContent("5");

    rerender(<SubscriptionOverview state={ready({ terms: { status: "loading" } })} locale="pt" />);
    const loading = field("Créditos por mês").textContent;
    expect(loading).toContain("Carregando");

    // Compare the values alone, without their field labels.
    const values = [
      absent?.replace("Próxima renovação", ""),
      notApplicable?.replace("Próxima renovação", ""),
      unavailable?.replace("Créditos por mês", ""),
      loading?.replace("Créditos por mês", ""),
    ];
    expect(new Set(values).size).toBe(4);
  });

  it("gives loading, unavailable and error their own copy and no plan or figure", () => {
    const texts = (["loading", "unavailable", "error"] as const).map((status) => {
      const { unmount } = render(<SubscriptionOverview state={{ status }} locale="pt" />);
      const card = screen.getByTestId("subscription-overview");
      expect(card).toHaveAttribute("data-state", status);
      const text = card.textContent ?? "";
      expect(text).not.toMatch(/R\$|\d|NeuroStack|Trial|Ativa|Mensal\b/);
      unmount();
      return text;
    });
    expect(new Set(texts).size).toBe(3);
    render(<SubscriptionOverview state={{ status: "loading" }} locale="pt" />);
    expect(screen.getByRole("status")).toHaveTextContent("Carregando a assinatura");
  });

  it("shows an account without subscription without inventing a plan", () => {
    render(<SubscriptionOverview state={ready({ subscription: null, activeClinics: 0 })} locale="pt" />);

    expect(screen.getByTestId("subscription-none")).toHaveTextContent("Esta conta não tem assinatura.");
    expect(field("Clínicas ativas")).toHaveTextContent("0");
    expect(screen.queryByText(/NeuroStack|Trial|Mensalidade|R\$/)).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Próxima renovação" })).not.toBeInTheDocument();
  });

  it.each([
    ["readback_required", "pending", "Em verificação pelo provedor · Pagamento pendente"],
    ["active", "overdue", "Ativa · Pagamento em atraso"],
    ["active_linked", "reversed", "Ativa · Pagamento estornado"],
    ["some_future_status", "some_future_state", "Em verificação pelo servidor · Pagamento em verificação"],
  ])("labels status %s with payment %s by text", (status, paymentState, label) => {
    render(<SubscriptionOverview state={ready({ subscription: subscription({ status, paymentState }) })} locale="pt" />);
    expect(screen.getByTestId("subscription-status")).toHaveTextContent(label);
    expect(screen.getByTestId("subscription-status")).not.toHaveTextContent(/some_future/);
  });

  it("shows a malformed renewal date as unavailable rather than raw", () => {
    render(<SubscriptionOverview state={ready({ subscription: subscription({ currentPeriodEnd: "soon" }) })} locale="pt" />);
    expect(field("Próxima renovação")).toHaveTextContent(/^Próxima renovaçãoIndisponível$/);
  });

  it.each([
    ["overdue", "pt", "Período pago até", "05/12/2026", "Próxima renovação"],
    ["reversed", "pt", "Período pago até", "05/12/2026", "Próxima renovação"],
    ["overdue", "en", "Paid through", "12/05/2026", "Next renewal"],
    ["reversed", "en", "Paid through", "12/05/2026", "Next renewal"],
  ] as const)(
    "labels the period end of a %s payment as paid through (%s), from payment_state only",
    (paymentState, locale, label, date, renewalLabel) => {
      render(<SubscriptionOverview state={ready({ subscription: subscription({ paymentState }) })} locale={locale} />);
      expect(field(label)).toHaveTextContent(date);
      expect(field(label)).not.toHaveTextContent(/Fim do período pago|End of the paid period/);
      expect(screen.queryByRole("group", { name: renewalLabel })).not.toBeInTheDocument();
    },
  );

  it.each([
    ["paid", "pt", "Próxima renovação"],
    ["pending", "pt", "Próxima renovação"],
    ["paid", "en", "Next renewal"],
  ] as const)("keeps the renewal label for a %s payment (%s)", (paymentState, locale, label) => {
    render(<SubscriptionOverview state={ready({ subscription: subscription({ paymentState }) })} locale={locale} />);
    expect(field(label)).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: /Período pago até|Paid through/ })).not.toBeInTheDocument();
  });

  it("keeps the cancelled wording over the paid-through label", () => {
    render(
      <SubscriptionOverview state={ready({ subscription: subscription({ status: "cancelled", paymentState: "overdue" }) })} locale="pt" />,
    );
    expect(field("Próxima renovação")).toHaveTextContent("Não se aplica: assinatura cancelada");
  });

  it("tells a catalog read error apart from an unavailable catalog in the plan terms", () => {
    const { rerender } = render(<SubscriptionOverview state={ready({ terms: { status: "error" } })} locale="pt" />);
    const error = field("Créditos por mês").textContent?.replace("Créditos por mês", "");
    expect(error).toBe("Não foi possível ler");
    expect(field("Clínicas incluídas")).toHaveTextContent("Não foi possível ler");
    expect(field("Mensalidade")).toHaveTextContent("Não foi possível ler");

    rerender(<SubscriptionOverview state={ready({ terms: { status: "unavailable" } })} locale="pt" />);
    const unavailable = field("Créditos por mês").textContent?.replace("Créditos por mês", "");
    expect(unavailable).toBe("Indisponível");
    expect(error).not.toEqual(unavailable);

    rerender(<SubscriptionOverview state={ready({ terms: { status: "error" } })} locale="en" />);
    expect(field("Credits per month")).toHaveTextContent("Could not be read");
  });

  it("renders the overview in EN", () => {
    render(<SubscriptionOverview state={ready()} locale="en" />);

    const card = screen.getByTestId("subscription-overview");
    expect(within(card).getByText("Current subscription")).toBeInTheDocument();
    expect(screen.getByTestId("subscription-status")).toHaveTextContent("Active · Payment up to date");
    expect(field("Cycle")).toHaveTextContent("Monthly");
    expect(field("Monthly price")).toHaveTextContent(money(246_800, "en"));
    expect(field("Next renewal")).toHaveTextContent("12/05/2026");
    expect(field("Credits per month")).toHaveTextContent("4,321 credits");
    expect(field("Price per additional clinic/month")).toHaveTextContent(money(13_500, "en"));
  });
});
