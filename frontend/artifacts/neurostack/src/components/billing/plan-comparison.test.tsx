import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { formatMoney } from "./format";
import { PlanComparison, PLAN_TABLE_CLASSES, type PlanComparisonItem } from "./plan-comparison";

// Synthetic catalog-shaped items; every value is invented for the tests.
function item(overrides: Partial<PlanComparisonItem> & Pick<PlanComparisonItem, "offerId" | "name">): PlanComparisonItem {
  return {
    currency: "BRL",
    amountCents: 10_000,
    credits: 100,
    includedClinics: 1,
    additionalClinicAmountCents: 1_000,
    marker: null,
    canSubscribe: false,
    ...overrides,
  };
}

const plans: PlanComparisonItem[] = [
  item({ offerId: "plan_plus_monthly_v1", name: "Plus", amountCents: 123_400, credits: 1_234, includedClinics: 1, additionalClinicAmountCents: 12_300 }),
  item({ offerId: "plan_pro_monthly_v1", name: "Pro", amountCents: 234_500, credits: 2_345, includedClinics: 4, additionalClinicAmountCents: 23_400 }),
  item({ offerId: "plan_max_monthly_v1", name: "Max", amountCents: 345_600, credits: 3_456, includedClinics: 9, additionalClinicAmountCents: 34_500 }),
];

function money(cents: number, locale: "pt" | "en" = "pt") {
  return (formatMoney(cents, "BRL", locale) ?? "").replace(/ /g, " ");
}

function rows() {
  return within(screen.getByRole("table")).getAllByRole("row").slice(1);
}

describe("<PlanComparison />", () => {
  afterEach(cleanup);

  it("renders every plan, in the given order, from its props", () => {
    render(<PlanComparison state={{ status: "ready", plans }} locale="pt" />);

    expect(rows().map((row) => within(row).getByRole("rowheader").textContent)).toEqual([
      "NeuroStack Plus",
      "NeuroStack Pro",
      "NeuroStack Max",
    ]);
    expect(within(screen.getByRole("table")).getAllByRole("columnheader").map((h) => h.textContent)).toEqual([
      "Plano",
      "Mensalidade",
      "Créditos por mês",
      "Clínicas incluídas",
      "Preço por clínica adicional/mês",
    ]);
    const [plus] = rows();
    const cells = within(plus).getAllByRole("cell").map((cell) => (cell.textContent ?? "").replace(/\u00a0/g, " "));
    expect(cells).toEqual([
      `Mensalidade${money(123_400)}/mês`,
      "Créditos por mês1.234 créditos",
      "Clínicas incluídas1",
      `Preço por clínica adicional/mês${money(12_300)}/mês`,
    ]);
  });

  it("follows different fixture values without any component change", () => {
    const changed = [
      item({ offerId: "plan_max_monthly_v1", name: "Max", amountCents: 9_900, credits: 7, includedClinics: 0, additionalClinicAmountCents: 0 }),
      item({ offerId: "plan_plus_monthly_v1", name: "Plus", amountCents: 1_100, credits: 70_000, includedClinics: 12, additionalClinicAmountCents: 4_200 }),
    ];
    render(<PlanComparison state={{ status: "ready", plans: changed }} locale="pt" />);

    const [max, plus] = rows();
    expect(max).toHaveTextContent(`${money(9_900)}/mês`);
    expect(max).toHaveTextContent("7 créditos");
    // Zero is data: no included clinic and a free extra clinic both show.
    expect(within(max).getAllByRole("cell")[2]).toHaveTextContent(/^Clínicas incluídas0$/);
    expect(within(max).getAllByRole("cell")[3]).toHaveTextContent(`${money(0)}/mês`);
    expect(plus).toHaveTextContent("70.000 créditos");
    expect(plus).toHaveTextContent(`${money(4_200)}/mês`);
    expect(document.body.textContent ?? "").not.toMatch(/1\.234|2\.345|3\.456|—/);
  });

  it("identifies the current plan by text and aria-current, not by colour only", () => {
    const withCurrent = plans.map((plan) => ({ ...plan, marker: plan.name === "Pro" ? ("current" as const) : null }));
    render(<PlanComparison state={{ status: "ready", plans: withCurrent }} locale="pt" />);

    const [plus, pro] = rows();
    expect(within(pro).getByText("Plano atual")).toBeInTheDocument();
    expect(pro).toHaveAttribute("aria-current", "true");
    expect(plus).not.toHaveAttribute("aria-current");
    expect(within(plus).queryByText("Plano atual")).not.toBeInTheDocument();
  });

  it("qualifies an unconfirmed subscription's plan: an amber text-and-icon marker, no aria-current", () => {
    const withPending = plans.map((plan) => ({ ...plan, marker: plan.name === "Pro" ? ("pending" as const) : null }));
    render(<PlanComparison state={{ status: "ready", plans: withPending }} locale="pt" />);

    const [plus, pro] = rows();
    const marker = within(pro).getByTestId("plan-marker");
    expect(marker).toHaveTextContent("Aguardando confirmação");
    expect(marker).toHaveAttribute("data-marker", "pending");
    expect(marker.querySelector("svg")).not.toBeNull();
    expect(marker.className).toContain("text-amber-800");
    expect(within(pro).queryByText("Plano atual")).not.toBeInTheDocument();
    expect(pro).not.toHaveAttribute("aria-current");
    expect(plus).not.toHaveAttribute("aria-current");
    expect(within(plus).queryByTestId("plan-marker")).not.toBeInTheDocument();
    cleanup();

    render(<PlanComparison state={{ status: "ready", plans: withPending }} locale="en" />);
    expect(within(rows()[1]).getByTestId("plan-marker")).toHaveTextContent("Awaiting confirmation");
  });

  it("renders no action and no plan-switch CTA without a capability", () => {
    const withCurrent = plans.map((plan) => ({ ...plan, marker: plan.name === "Plus" ? ("current" as const) : null }));
    render(<PlanComparison state={{ status: "ready", plans: withCurrent }} locale="pt" onSubscribe={vi.fn()} />);

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByText(/Migrar|Trocar de plano|Assinar/)).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Ação" })).not.toBeInTheDocument();
  });

  it("offers an action only for authorised plans and reports the chosen offer once", () => {
    const onSubscribe = vi.fn();
    const authorised = plans.map((plan) => ({ ...plan, canSubscribe: plan.name !== "Max" }));
    render(<PlanComparison state={{ status: "ready", plans: authorised }} locale="pt" onSubscribe={onSubscribe} />);

    const [plus, pro, max] = rows();
    expect(within(max).queryByRole("button")).not.toBeInTheDocument();
    expect(within(plus).getByRole("button", { name: "Assinar Plus" })).toBeEnabled();
    fireEvent.click(within(pro).getByRole("button", { name: "Assinar Pro" }));
    expect(onSubscribe).toHaveBeenCalledTimes(1);
    expect(onSubscribe).toHaveBeenCalledWith("plan_pro_monthly_v1");
    expect(screen.getByText(/Assinatura mensal no cartão de crédito/)).toBeInTheDocument();
  });

  it("disables temporarily blocked actions with an associated reason", () => {
    const onSubscribe = vi.fn();
    const authorised = plans.map((plan) => ({ ...plan, canSubscribe: true }));
    render(
      <PlanComparison
        state={{ status: "ready", plans: authorised }}
        locale="pt"
        onSubscribe={onSubscribe}
        actionBlockedReason="Motivo sintético do bloqueio."
      />,
    );

    for (const button of screen.getAllByRole("button", { name: /^Assinar / })) {
      expect(button).toBeDisabled();
      expect(button).toHaveAccessibleDescription("Motivo sintético do bloqueio.");
    }
  });

  it("gives loading, unavailable, error and empty catalogs distinct content", () => {
    const onRetry = vi.fn();
    const texts: string[] = [];
    const { rerender } = render(<PlanComparison state={{ status: "loading" }} locale="pt" onRetry={onRetry} />);
    expect(screen.getByRole("status")).toHaveTextContent("Carregando o catálogo de planos");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    texts.push(document.body.textContent ?? "");

    rerender(<PlanComparison state={{ status: "unavailable" }} locale="pt" onRetry={onRetry} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/temporariamente indisponível/);
    texts.push(document.body.textContent ?? "");

    rerender(<PlanComparison state={{ status: "error" }} locale="pt" onRetry={onRetry} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/Não foi possível ler o catálogo/);
    fireEvent.click(screen.getByRole("button", { name: "Tentar novamente" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    texts.push(document.body.textContent ?? "");

    rerender(<PlanComparison state={{ status: "ready", plans: [] }} locale="pt" />);
    expect(screen.getByTestId("plan-comparison-empty")).toHaveTextContent("Nenhum plano publicado no catálogo.");
    texts.push(document.body.textContent ?? "");

    expect(new Set(texts).size).toBe(4);
    for (const text of texts) expect(text).not.toMatch(/R\$|\d/);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows an invalid currency as unavailable instead of guessing one", () => {
    render(
      <PlanComparison
        state={{ status: "ready", plans: [item({ offerId: "x", name: "Plus", currency: "not-a-currency" })] }}
        locale="pt"
      />,
    );
    const [row] = rows();
    expect(within(row).getAllByRole("cell")[0]).toHaveTextContent(/^MensalidadeIndisponível$/);
    expect(row).not.toHaveTextContent("R$");
  });

  // Structure only: jsdom computes no layout, so these tests cannot prove pixel
  // alignment. They pin the construction that makes alignment hold (one native
  // table shared by header and rows, the action column present in every row)
  // and the absence of silent clipping; pixel alignment and the visible
  // Assinar button at real viewports are covered by Group 2's Playwright
  // baselines.
  it("lays header and plans out as one native table with the action column in every row", () => {
    const onlyPro = plans.map((plan) => ({ ...plan, canSubscribe: plan.name === "Pro" }));
    render(<PlanComparison state={{ status: "ready", plans: onlyPro }} locale="pt" onSubscribe={vi.fn()} />);

    const table = screen.getByRole("table", { name: "Comparação de planos" });
    expect(table.tagName).toBe("TABLE");
    const headers = within(table).getAllByRole("columnheader");
    expect(headers).toHaveLength(6);
    for (const header of headers) expect(header.closest("thead")).not.toBeNull();
    for (const row of rows()) {
      expect(row.tagName).toBe("TR");
      expect(row.parentElement?.tagName).toBe("TBODY");
      // Rowheader + four values + the action cell, even for plans without a button.
      expect(row.children).toHaveLength(headers.length);
      expect(within(row).getByRole("rowheader").tagName).toBe("TH");
      expect(within(row).getAllByRole("cell")).toHaveLength(headers.length - 1);
      expect(within(row).getByTestId("plan-action-cell")).toBeInTheDocument();
    }
    const [plus, pro, max] = rows();
    expect(within(plus).getByTestId("plan-action-cell")).toBeEmptyDOMElement();
    expect(within(max).getByTestId("plan-action-cell")).toBeEmptyDOMElement();
    expect(within(pro).getByRole("button", { name: "Assinar Pro" })).toBeInTheDocument();
  });

  it("drops the action column from header and rows together when no plan has an action", () => {
    render(<PlanComparison state={{ status: "ready", plans }} locale="pt" onSubscribe={vi.fn()} />);

    const headers = within(screen.getByRole("table")).getAllByRole("columnheader");
    expect(headers).toHaveLength(5);
    for (const row of rows()) {
      expect(row.children).toHaveLength(headers.length);
      expect(within(row).queryByTestId("plan-action-cell")).not.toBeInTheDocument();
    }
  });

  it("scrolls rather than clips, and labels cells only where no column header is shown", () => {
    const authorised = plans.map((plan) => ({ ...plan, canSubscribe: true }));
    render(<PlanComparison state={{ status: "ready", plans: authorised }} locale="pt" onSubscribe={vi.fn()} />);

    const scroll = screen.getByTestId("plan-comparison-scroll");
    expect(scroll.className).toContain("overflow-x-auto");
    expect(scroll.className).not.toMatch(/overflow-hidden|overflow-clip/);
    // The table layout and the hidden cell labels switch on the same breakpoint.
    const breakpoint = "@min-[48rem]:";
    expect(PLAN_TABLE_CLASSES.table).toContain(`${breakpoint}table`);
    expect(PLAN_TABLE_CLASSES.head).toContain(`${breakpoint}table-header-group`);
    for (const row of rows()) {
      for (const cell of within(row).getAllByRole("cell").slice(0, 4)) {
        const label = cell.firstElementChild as HTMLElement;
        // Display:none in the table layout, so a screen reader hears the
        // column header once; visible in the card layout, where it names the value.
        expect(label.className).toBe(PLAN_TABLE_CLASSES.cellLabel);
        expect(label.className).toContain(`${breakpoint}hidden`);
        expect(label.className).not.toMatch(/sr-only/);
      }
    }
  });

  it("renders the comparison in EN", () => {
    const withCurrent = plans.map((plan) => ({ ...plan, marker: plan.name === "Max" ? ("current" as const) : null, canSubscribe: plan.name === "Plus" }));
    render(<PlanComparison state={{ status: "ready", plans: withCurrent }} locale="en" onSubscribe={vi.fn()} />);

    expect(screen.getByRole("table", { name: "Plan comparison" })).toBeInTheDocument();
    expect(screen.getAllByRole("columnheader").map((h) => h.textContent)).toEqual([
      "Plan",
      "Monthly price",
      "Credits per month",
      "Included clinics",
      "Price per additional clinic/month",
      "Action",
    ]);
    const [plus, , max] = rows();
    expect(plus).toHaveTextContent(`${money(123_400, "en")}/month`);
    expect(plus).toHaveTextContent("1,234 credits");
    expect(within(max).getByText("Current plan")).toBeInTheDocument();
    expect(within(plus).getByRole("button", { name: "Subscribe to Plus" })).toBeEnabled();
  });
});
