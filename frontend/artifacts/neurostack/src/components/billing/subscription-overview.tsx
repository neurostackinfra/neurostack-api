import type { ReactNode } from "react";
import { CreditCard, Loader2 } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatCivilDate, formatCount, formatMoney, type Locale, type Txt } from "./format";

/**
 * `BillingSubscriptionSummaryResponse` of `GET /v1/billing/summary`, with the
 * display name the page resolved for its `offer_id`. `status` and
 * `paymentState` stay strings so a future server value degrades to a generic
 * label instead of breaking the page.
 */
export interface SubscriptionView {
  planName: string;
  status: string;
  paymentState: string;
  amountCents: number;
  /** Calendar date `YYYY-MM-DD` (no time zone), or `null` if no cycle is fully paid. */
  currentPeriodEnd: string | null;
}

/**
 * The current offer's terms from `GET /v1/billing/catalog`. `unavailable`
 * covers a catalog the server reports as unavailable (503) and a subscription
 * whose offer or catalog version is not in the catalog served now; `error`
 * is a catalog that could not be read (failure or malformed answer). No term
 * is guessed in either case.
 */
export type PlanTerms =
  | { status: "loading" }
  | { status: "unavailable" }
  | { status: "error" }
  | {
      status: "ready";
      currency: string;
      credits: number;
      includedClinics: number;
      additionalClinicAmountCents: number;
    };

export type SubscriptionOverviewState =
  | { status: "loading" }
  | { status: "unavailable" }
  | { status: "error" }
  | {
      status: "ready";
      /** `null`: the account has no ASAAS subscription. */
      subscription: SubscriptionView | null;
      activeClinics: number;
      terms: PlanTerms;
    };

export function subscriptionStatusLabel(status: string, txt: Txt): string {
  switch (status) {
    case "active":
    case "active_linked":
      return txt("Ativa", "Active");
    case "readback_required":
    case "non_billable_pending_readback":
      return txt("Em verificação pelo provedor", "Under provider verification");
    case "cancelled":
      return txt("Cancelada", "Cancelled");
    default:
      return txt("Em verificação pelo servidor", "Under server verification");
  }
}

export function paymentStateLabel(state: string, txt: Txt): string {
  switch (state) {
    case "paid":
      return txt("Pagamento em dia", "Payment up to date");
    case "overdue":
      return txt("Pagamento em atraso", "Payment overdue");
    case "reversed":
      return txt("Pagamento estornado", "Payment refunded");
    case "pending":
      return txt("Pagamento pendente", "Payment pending");
    default:
      return txt("Pagamento em verificação", "Payment under verification");
  }
}

/**
 * Payment states in which the server reports the paid period as not being
 * renewed on schedule: its end is shown as "paid through", not as a renewal.
 * Read only from the server's `payment_state`; the browser does no date math.
 */
const NOT_RENEWING_PAYMENT_STATES = new Set(["overdue", "reversed"]);

function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <div role="group" aria-label={label} className="rounded-lg border border-border/60 bg-muted/15 p-3">
      <p className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</p>
      <p className="mt-0.5 text-sm font-bold tabular-nums text-foreground">{children}</p>
      {hint && <p className="mt-0.5 text-[10px] text-muted-foreground">{hint}</p>}
    </div>
  );
}

function Shell({ txt, children, state }: { txt: Txt; children: ReactNode; state: string }) {
  return (
    <Card className="border-emerald-500/25 bg-card" data-testid="subscription-overview" data-state={state}>
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2">
          <CreditCard className="h-4 w-4 text-emerald-500" aria-hidden="true" />
          <CardTitle className="text-sm font-semibold text-foreground">
            {txt("Assinatura atual", "Current subscription")}
          </CardTitle>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">{children}</CardContent>
    </Card>
  );
}

/**
 * The current subscription, straight from typed props. No fetch, no default:
 * a value the contract did not send is shown as its own state, never as a
 * number, a plan or `trial`.
 */
export function SubscriptionOverview({
  state,
  locale,
}: {
  state: SubscriptionOverviewState;
  locale: Locale;
}) {
  const txt: Txt = (pt, en) => (locale === "en" ? en : pt);

  if (state.status === "loading") {
    return (
      <Shell txt={txt} state="loading">
        <p role="status" aria-live="polite" className="flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          {txt("Carregando a assinatura…", "Loading the subscription…")}
        </p>
      </Shell>
    );
  }
  if (state.status === "unavailable" || state.status === "error") {
    return (
      <Shell txt={txt} state={state.status}>
        <p className="text-xs text-muted-foreground">
          {state.status === "unavailable"
            ? txt(
                "Dados da assinatura temporariamente indisponíveis. Nenhum plano ou valor foi presumido.",
                "Subscription data is temporarily unavailable. No plan or amount was assumed.",
              )
            : txt(
                "Não foi possível ler a assinatura. Nenhum plano ou valor foi presumido.",
                "The subscription could not be read. No plan or amount was assumed.",
              )}
        </p>
      </Shell>
    );
  }

  const { subscription, activeClinics, terms } = state;
  const termsValue = (render: (t: Extract<PlanTerms, { status: "ready" }>) => string): string =>
    terms.status === "ready"
      ? render(terms)
      : terms.status === "loading"
        ? txt("Carregando…", "Loading…")
        : terms.status === "error"
          ? txt("Não foi possível ler", "Could not be read")
          : txt("Indisponível", "Unavailable");
  const clinicsField = (
    <Field label={txt("Clínicas ativas", "Active clinics")}>{formatCount(activeClinics, locale)}</Field>
  );

  if (subscription === null) {
    return (
      <Shell txt={txt} state="none">
        <p className="text-sm text-foreground" data-testid="subscription-none">
          {txt(
            "Esta conta não tem assinatura. Compare os planos abaixo.",
            "This account has no subscription. Compare the plans below.",
          )}
        </p>
        <div className="grid grid-cols-2 gap-2 md:grid-cols-3">{clinicsField}</div>
      </Shell>
    );
  }

  const cancelled = subscription.status === "cancelled";
  const periodEnd = formatCivilDate(subscription.currentPeriodEnd, locale);
  const notRenewing = !cancelled && NOT_RENEWING_PAYMENT_STATES.has(subscription.paymentState);
  let renewal: string;
  let renewalHint: string | undefined;
  if (cancelled) {
    renewal = txt("Não se aplica: assinatura cancelada", "Not applicable: subscription cancelled");
    renewalHint = periodEnd
      ? txt(`Acesso pago até ${periodEnd}`, `Paid access until ${periodEnd}`)
      : undefined;
  } else if (subscription.currentPeriodEnd === null) {
    renewal = txt("Nenhum ciclo pago ainda", "No paid cycle yet");
  } else if (periodEnd === null) {
    renewal = txt("Indisponível", "Unavailable");
  } else {
    renewal = periodEnd;
    renewalHint = notRenewing ? undefined : txt("Fim do período pago", "End of the paid period");
  }
  const renewalLabel = notRenewing
    ? txt("Período pago até", "Paid through")
    : txt("Próxima renovação", "Next renewal");
  const price =
    terms.status === "ready" ? formatMoney(subscription.amountCents, terms.currency, locale) : null;

  return (
    <Shell txt={txt} state="ready">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <p className="text-lg font-bold text-foreground">NeuroStack {subscription.planName}</p>
        <p className="text-xs font-semibold text-muted-foreground" data-testid="subscription-status">
          {subscriptionStatusLabel(subscription.status, txt)} · {paymentStateLabel(subscription.paymentState, txt)}
        </p>
      </div>
      <div className="grid grid-cols-2 gap-2 md:grid-cols-3">
        <Field label={txt("Ciclo", "Cycle")}>{termsValue(() => txt("Mensal", "Monthly"))}</Field>
        <Field label={txt("Mensalidade", "Monthly price")}>
          {price ?? termsValue(() => txt("Indisponível", "Unavailable"))}
        </Field>
        <Field label={renewalLabel} hint={renewalHint}>
          {renewal}
        </Field>
        <Field label={txt("Créditos por mês", "Credits per month")}>
          {termsValue((t) => `${formatCount(t.credits, locale)} ${txt("créditos", "credits")}`)}
        </Field>
        <Field label={txt("Clínicas incluídas", "Included clinics")}>
          {termsValue((t) => formatCount(t.includedClinics, locale))}
        </Field>
        {clinicsField}
        <Field label={txt("Preço por clínica adicional/mês", "Price per additional clinic/month")}>
          {termsValue(
            (t) =>
              formatMoney(t.additionalClinicAmountCents, t.currency, locale) ?? txt("Indisponível", "Unavailable"),
          )}
        </Field>
      </div>
    </Shell>
  );
}
