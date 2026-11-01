import { useId } from "react";
import { AlertTriangle, CheckCircle2, Clock, Loader2, RefreshCw, Zap } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { formatCount, formatMoney, type Locale, type Txt } from "./format";

/**
 * One subscription offer of the backend catalog (`GET /v1/billing/catalog`),
 * already in catalog order. Every figure is the catalog's integer value; the
 * page decides `marker` and `canSubscribe` from the summary, so
 * this component makes no commercial decision.
 */
export interface PlanComparisonItem {
  offerId: string;
  name: string;
  currency: string;
  amountCents: number;
  credits: number;
  includedClinics: number;
  additionalClinicAmountCents: number;
  /**
   * Label of the subscription's own offer (sold under this very catalog
   * version), decided from the server's
   * `status`/`payment_state` (`subscriptionMarker`): `current` for a
   * confirmed plan, `pending` for a record the server has not confirmed yet,
   * `null` for every other offer.
   */
  marker: "current" | "pending" | null;
  canSubscribe: boolean;
}

export type PlanComparisonState =
  | { status: "loading" }
  | { status: "unavailable" }
  | { status: "error" }
  | { status: "ready"; plans: PlanComparisonItem[] };

function StateLine({
  state,
  txt,
  onRetry,
}: {
  state: Exclude<PlanComparisonState, { status: "ready" }>;
  txt: Txt;
  onRetry?: () => void;
}) {
  if (state.status === "loading") {
    return (
      <p role="status" aria-live="polite" className="flex items-center gap-2 text-xs text-muted-foreground">
        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
        {txt("Carregando o catálogo de planos…", "Loading the plan catalog…")}
      </p>
    );
  }
  return (
    <div role="alert" className="space-y-2 rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-xs">
      <p className="flex items-center gap-2 text-destructive">
        <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        {state.status === "unavailable"
          ? txt(
              "O catálogo de planos está temporariamente indisponível. Nenhum preço foi presumido.",
              "The plan catalog is temporarily unavailable. No price was assumed.",
            )
          : txt(
              "Não foi possível ler o catálogo de planos. Nenhum preço foi presumido.",
              "The plan catalog could not be read. No price was assumed.",
            )}
      </p>
      {onRetry && (
        <Button type="button" variant="outline" size="sm" className="gap-1.5" onClick={onRetry}>
          <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
          {txt("Tentar novamente", "Try again")}
        </Button>
      )}
    </div>
  );
}

/**
 * Classes of the one canonical collection. Every plan is one native table row
 * (`<tr>`) of the same `<table>` as the header, so in the table layout the
 * browser aligns each header with its cells by construction: there is no
 * second grid template that could drift. The layout follows the width the
 * table really has (a container query on `container`, at 48rem), not the
 * viewport: with the sidebar open, a `md` viewport leaves too little room for
 * six columns. 48rem (768px) clears the table's measured minimum width with
 * the real catalog amounts by 12px. Measured in Chromium with Inter: 652px PT
 * and 691px EN with every plan subscribable; when a pending subscription's
 * row carries both the "Aguardando confirmação" badge and the disabled
 * "Assinar" button (the badge's nowrap width sets the plan column), 728px PT
 * and 751px EN for pending Pro, 732px PT and 755px EN for pending Plus, and
 * 733px PT and 756px EN for pending Max (the widest row); smaller with the
 * fallback font. So a shown
 * table never needs its internal scroll. Below 48rem the same elements
 * are re-laid out as one card per plan (the header row is hidden and each
 * cell shows its own label). Explicit roles keep the table semantics when the
 * card layout overrides the table display. Tailwind needs literal class
 * names, hence the repeated variant.
 */
export const PLAN_TABLE_CLASSES = {
  container: "@container",
  /** Horizontal scroll at most: an action is never clipped silently. */
  wrapper: "@min-[48rem]:overflow-x-auto @min-[48rem]:rounded-lg @min-[48rem]:border @min-[48rem]:border-border/60",
  table: "block w-full @min-[48rem]:table @min-[48rem]:border-collapse",
  head: "hidden @min-[48rem]:table-header-group",
  headCell:
    "bg-muted/30 px-2 py-2 text-left align-bottom text-[10px] font-bold uppercase tracking-wider text-muted-foreground first:pl-4 last:pr-4",
  body: "block space-y-3 @min-[48rem]:table-row-group @min-[48rem]:space-y-0",
  row: "grid grid-cols-2 gap-3 rounded-lg border p-4 @min-[48rem]:table-row @min-[48rem]:rounded-none @min-[48rem]:border-0 @min-[48rem]:p-0",
  bodyCell:
    "@min-[48rem]:table-cell @min-[48rem]:border-t @min-[48rem]:border-border/60 @min-[48rem]:px-2 @min-[48rem]:py-4 @min-[48rem]:align-middle @min-[48rem]:first:pl-4 @min-[48rem]:last:pr-4",
  /**
   * Per-cell label: the card's accessible name in the card layout; removed
   * (`display: none`, so not read twice) where the column header exists.
   */
  cellLabel: "block text-[10px] uppercase tracking-wide text-muted-foreground @min-[48rem]:hidden",
  button: "w-full gap-1.5 @min-[48rem]:w-auto",
} as const;

/**
 * Plans compared from one canonical collection. A single DOM serves both
 * layouts: a native table with headers when the table fits, and one card per
 * plan otherwise, so wide and narrow screens never disagree on values or
 * actions.
 * When any plan can be subscribed, every row keeps an action cell (empty for
 * the others), so the action column exists for the whole table.
 */
export function PlanComparison({
  state,
  locale,
  onSubscribe,
  onRetry,
  actionBlockedReason,
  notice,
}: {
  state: PlanComparisonState;
  locale: Locale;
  onSubscribe?: (offerId: string) => void;
  onRetry?: () => void;
  /** Temporary reason the subscribe actions are disabled; `null` when enabled. */
  actionBlockedReason?: string | null;
  /** Server-derived note shown under the table (e.g. availability unknown). */
  notice?: string | null;
}) {
  const txt: Txt = (pt, en) => (locale === "en" ? en : pt);
  const reasonId = useId();

  if (state.status !== "ready") {
    return <StateLine state={state} txt={txt} onRetry={onRetry} />;
  }
  if (state.plans.length === 0) {
    return (
      <p className="text-xs text-muted-foreground" data-testid="plan-comparison-empty">
        {txt("Nenhum plano publicado no catálogo.", "No plan is published in the catalog.")}
      </p>
    );
  }

  const unavailable = txt("Indisponível", "Unavailable");
  const perMonth = txt("/mês", "/month");
  const anyAction = state.plans.some((plan) => plan.canSubscribe);
  const blocked = Boolean(actionBlockedReason);
  const columns = [
    txt("Plano", "Plan"),
    txt("Mensalidade", "Monthly price"),
    txt("Créditos por mês", "Credits per month"),
    txt("Clínicas incluídas", "Included clinics"),
    txt("Preço por clínica adicional/mês", "Price per additional clinic/month"),
  ];
  const c = PLAN_TABLE_CLASSES;
  const monthly = (money: string | null) => (money ? `${money}${perMonth}` : unavailable);

  return (
    <div className="space-y-3">
      <div className={c.container}>
        <div className={c.wrapper} data-testid="plan-comparison-scroll">
          <table role="table" aria-label={txt("Comparação de planos", "Plan comparison")} className={c.table}>
            <thead role="rowgroup" className={c.head}>
              <tr role="row">
                {columns.map((label) => (
                  <th key={label} role="columnheader" scope="col" className={c.headCell}>
                    {label}
                  </th>
                ))}
                {anyAction && (
                  <th role="columnheader" scope="col" className={c.headCell}>
                    <span className="sr-only">{txt("Ação", "Action")}</span>
                  </th>
                )}
              </tr>
            </thead>
            <tbody role="rowgroup" className={c.body}>
              {state.plans.map((plan) => {
                // [label, value, may wrap]: money never wraps; a credit count
                // may break before its unit when the table is narrow.
                const cells: Array<[string, string, boolean]> = [
                  [columns[1], monthly(formatMoney(plan.amountCents, plan.currency, locale)), false],
                  [columns[2], `${formatCount(plan.credits, locale)} ${txt("créditos", "credits")}`, true],
                  [columns[3], formatCount(plan.includedClinics, locale), false],
                  [columns[4], monthly(formatMoney(plan.additionalClinicAmountCents, plan.currency, locale)), false],
                ];
                return (
                  <tr
                    key={plan.offerId}
                    role="row"
                    data-testid={`plan-row-${plan.offerId}`}
                    aria-current={plan.marker === "current" ? "true" : undefined}
                    className={`${c.row} ${
                      plan.marker === "current"
                        ? "border-emerald-500/50 bg-emerald-500/5"
                        : plan.marker === "pending"
                          ? "border-amber-500/50 bg-amber-500/5"
                          : "border-border/60 bg-card"
                    }`}
                  >
                    <th role="rowheader" scope="row" className={`col-span-2 block text-left font-normal ${c.bodyCell}`}>
                      <span className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-bold text-foreground">NeuroStack {plan.name}</span>
                        {plan.marker === "current" && (
                          <Badge
                            variant="outline"
                            data-testid="plan-marker"
                            data-marker="current"
                            className="gap-1 border-emerald-500/40 text-[10px] text-emerald-700 dark:text-emerald-400"
                          >
                            <CheckCircle2 className="h-3 w-3" aria-hidden="true" />
                            {txt("Plano atual", "Current plan")}
                          </Badge>
                        )}
                        {plan.marker === "pending" && (
                          <Badge
                            variant="outline"
                            data-testid="plan-marker"
                            data-marker="pending"
                            className="gap-1 border-amber-500/50 text-[10px] text-amber-800 dark:text-amber-300"
                          >
                            <Clock className="h-3 w-3" aria-hidden="true" />
                            {txt("Aguardando confirmação", "Awaiting confirmation")}
                          </Badge>
                        )}
                      </span>
                    </th>
                    {cells.map(([label, value, wraps]) => (
                      <td key={label} role="cell" className={`block space-y-0.5 ${c.bodyCell}`}>
                        <span className={c.cellLabel}>{label}</span>
                        <span className={`block text-sm font-semibold tabular-nums text-foreground ${wraps ? "" : "whitespace-nowrap"}`}>
                          {value}
                        </span>
                      </td>
                    ))}
                    {anyAction && (
                      <td role="cell" className={`col-span-2 block ${c.bodyCell}`} data-testid="plan-action-cell">
                        {plan.canSubscribe && (
                          <Button
                            type="button"
                            size="sm"
                            className={c.button}
                            disabled={blocked}
                            aria-describedby={blocked ? reasonId : undefined}
                            onClick={() => onSubscribe?.(plan.offerId)}
                          >
                            <Zap className="h-3.5 w-3.5" aria-hidden="true" />
                            {txt(`Assinar ${plan.name}`, `Subscribe to ${plan.name}`)}
                          </Button>
                        )}
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
      {anyAction && blocked && (
        <p id={reasonId} className="text-xs text-amber-800 dark:text-amber-400">
          {actionBlockedReason}
        </p>
      )}
      {anyAction && (
        <p className="text-xs text-muted-foreground">
          {txt(
            "Assinatura mensal no cartão de crédito, paga no checkout hospedado do ASAAS.",
            "Monthly credit card subscription, paid on the ASAAS-hosted checkout.",
          )}
        </p>
      )}
      {notice && <p className="text-xs text-muted-foreground">{notice}</p>}
    </div>
  );
}
