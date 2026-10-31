import {
  Activity,
  AlertTriangle,
  CalendarRange,
  Clock,
  Coins,
  Database,
  Download,
  FolderOpen,
  ShoppingCart,
} from "lucide-react";
import { useEffect, useState, type ElementType } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";

export interface BillingUsageStorageMetric {
  used_bytes: number;
  used_mb: number;
  limit_bytes: number;
  limit_mb: number;
  available_bytes: number;
  available_mb: number;
  overage_bytes: number;
  overage_mb: number;
  percent_used: number;
}

export interface BillingUsage {
  plan: string | null;
  as_of: string;
  trace_period: {
    start: string;
    end: string;
    timezone: string;
  };
  traces: {
    used: number;
    included: number;
    extra_credits: number;
    available: number;
    overage: number;
    percent_used: number;
  };
  storage: {
    rag: BillingUsageStorageMetric;
    media: BillingUsageStorageMetric;
  };
  credits: {
    purchase_enabled: boolean;
    coming_soon: boolean;
  };
}

interface PlanUsageSummaryProps {
  usage: BillingUsage;
  locale: "pt" | "en";
  className?: string;
  /**
   * Credit purchase (Group 6). Absent (the default) keeps the button
   * disabled, as on `main`; the billing page enables it only when the
   * server offers top-ups (credit switch effective and
   * `topup_commercial_enabled` read back true).
   */
  creditPurchase?: { enabled: boolean; onBuy: () => void };
}

interface UsageMeterProps {
  label: string;
  usedLabel: string;
  limitLabel: string;
  availableLabel: string;
  overageLabel: string;
  used: string;
  limit: string;
  available: string;
  overage: string;
  overageValue: number;
  percentUsed: number;
  locale: "pt" | "en";
  icon: ElementType;
  iconClassName: string;
  extraDetail?: string;
}

function clampPercent(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.min(100, Math.max(0, value));
}

function formatNumber(
  value: number,
  locale: "pt" | "en",
  maximumFractionDigits = 0,
): string {
  return value.toLocaleString(locale === "en" ? "en-US" : "pt-BR", {
    maximumFractionDigits,
  });
}

function formatDate(
  value: string,
  locale: "pt" | "en",
  timezone: string,
): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;

  try {
    return new Intl.DateTimeFormat(locale === "en" ? "en-US" : "pt-BR", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      timeZone: timezone || "UTC",
    }).format(date);
  } catch {
    return new Intl.DateTimeFormat(locale === "en" ? "en-US" : "pt-BR", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      timeZone: "UTC",
    }).format(date);
  }
}

function UsageMeter({
  label,
  usedLabel,
  limitLabel,
  availableLabel,
  overageLabel,
  used,
  limit,
  available,
  overage,
  overageValue,
  percentUsed,
  locale,
  icon: Icon,
  iconClassName,
  extraDetail,
}: UsageMeterProps) {
  const progressValue = clampPercent(percentUsed);

  return (
    <div
      role="group"
      aria-label={label}
      className="rounded-lg border border-border/60 bg-muted/15 p-4 space-y-3"
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <Icon className={`h-4 w-4 shrink-0 ${iconClassName}`} />
          <p className="text-sm font-semibold text-foreground truncate">
            {label}
          </p>
        </div>
        <span className="text-xs font-semibold tabular-nums text-muted-foreground">
          {formatNumber(percentUsed, locale, 1)}%
        </span>
      </div>

      <Progress
        value={progressValue}
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progressValue}
        className={`h-2 ${overageValue > 0 ? "[&>div]:bg-red-500" : ""}`}
      />

      <div className="grid grid-cols-3 gap-2 text-center">
        <div>
          <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
            {usedLabel}
          </p>
          <p className="mt-0.5 text-sm font-bold text-foreground tabular-nums">
            {used}
          </p>
        </div>
        <div className="border-x border-border/60 px-1">
          <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
            {limitLabel}
          </p>
          <p className="mt-0.5 text-sm font-bold text-foreground tabular-nums">
            {limit}
          </p>
        </div>
        <div>
          <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
            {availableLabel}
          </p>
          <p className="mt-0.5 text-sm font-bold text-emerald-400 tabular-nums">
            {available}
          </p>
        </div>
      </div>

      {extraDetail && (
        <p className="text-[11px] text-muted-foreground text-center">
          {extraDetail}
        </p>
      )}

      {overageValue > 0 && (
        <div className="flex items-center justify-between gap-2 rounded-md border border-red-500/30 bg-red-500/10 px-2.5 py-2 text-xs">
          <span className="font-medium text-red-400">{overageLabel}</span>
          <span className="font-bold text-red-400 tabular-nums">{overage}</span>
        </div>
      )}
    </div>
  );
}

export function PlanUsageSummary({
  usage,
  locale,
  className = "",
  creditPurchase,
}: PlanUsageSummaryProps) {
  const purchaseEnabled = creditPurchase?.enabled === true;
  const txt = (pt: string, en: string) => (locale === "en" ? en : pt);
  const timezone = usage.trace_period.timezone || "UTC";
  const tracePeriodEnd = new Date(usage.trace_period.end);
  const inclusiveEnd = Number.isNaN(tracePeriodEnd.getTime())
    ? usage.trace_period.end
    : new Date(tracePeriodEnd.getTime() - 1).toISOString();
  const period = `${formatDate(usage.trace_period.start, locale, timezone)} ${txt("a", "to")} ${formatDate(inclusiveEnd, locale, timezone)}`;
  const planLabel = usage.plan?.trim() || txt("Sem plano", "No plan");
  const traceLimit = usage.traces.included + usage.traces.extra_credits;
  const traceExtraDetail =
    usage.traces.extra_credits > 0
      ? txt(
          `${formatNumber(usage.traces.included, locale)} incluídos no plano + ${formatNumber(usage.traces.extra_credits, locale)} créditos extras`,
          `${formatNumber(usage.traces.included, locale)} included in the plan + ${formatNumber(usage.traces.extra_credits, locale)} extra credits`,
        )
      : undefined;

  return (
    <Card
      className={`border-indigo-500/25 bg-gradient-to-br from-indigo-950/15 to-transparent ${className}`.trim()}
    >
      <CardHeader className="pb-3">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <Activity className="h-4 w-4 text-indigo-400" />
              <CardTitle className="text-sm">
                {txt("Uso do plano", "Plan usage")}
              </CardTitle>
              <Badge
                variant="outline"
                className="text-[9px] uppercase tracking-wider border-indigo-500/30 text-indigo-400"
              >
                {planLabel}
              </Badge>
            </div>
            <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <CalendarRange className="h-3 w-3" />
              <span>
                {txt("Período de traces", "Trace period")}: {period} ·{" "}
                {timezone}
              </span>
            </div>
          </div>

          <div className="flex items-center gap-2 sm:justify-end">
            {usage.credits.coming_soon && !purchaseEnabled && (
              <Badge
                variant="outline"
                className="text-[9px] text-muted-foreground"
              >
                {txt("Em breve", "Coming soon")}
              </Badge>
            )}
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={!purchaseEnabled}
              onClick={purchaseEnabled ? creditPurchase?.onBuy : undefined}
              className="h-8 gap-1.5 text-xs"
            >
              <ShoppingCart className="h-3.5 w-3.5" />
              {txt("Comprar créditos", "Buy credits")}
            </Button>
          </div>
        </div>
      </CardHeader>

      <CardContent>
        <div className="grid gap-3 lg:grid-cols-3">
          <UsageMeter
            label={txt("Traces do mês", "Monthly traces")}
            usedLabel={txt("Usado", "Used")}
            limitLabel={txt("Limite", "Limit")}
            availableLabel={txt("Disponível", "Available")}
            overageLabel={txt("Excedente", "Overage")}
            used={formatNumber(usage.traces.used, locale)}
            limit={formatNumber(traceLimit, locale)}
            available={formatNumber(usage.traces.available, locale)}
            overage={formatNumber(usage.traces.overage, locale)}
            overageValue={usage.traces.overage}
            percentUsed={usage.traces.percent_used}
            locale={locale}
            icon={Activity}
            iconClassName="text-indigo-400"
            extraDetail={traceExtraDetail}
          />
          <UsageMeter
            label={txt("Armazenamento RAG", "RAG storage")}
            usedLabel={txt("Usado", "Used")}
            limitLabel={txt("Limite", "Limit")}
            availableLabel={txt("Disponível", "Available")}
            overageLabel={txt("Excedente", "Overage")}
            used={`${formatNumber(usage.storage.rag.used_mb, locale, 2)} MB`}
            limit={`${formatNumber(usage.storage.rag.limit_mb, locale, 2)} MB`}
            available={`${formatNumber(usage.storage.rag.available_mb, locale, 2)} MB`}
            overage={`${formatNumber(usage.storage.rag.overage_mb, locale, 2)} MB`}
            overageValue={usage.storage.rag.overage_bytes}
            percentUsed={usage.storage.rag.percent_used}
            locale={locale}
            icon={Database}
            iconClassName="text-blue-400"
          />
          <UsageMeter
            label={txt("Armazenamento de mídias", "Media storage")}
            usedLabel={txt("Usado", "Used")}
            limitLabel={txt("Limite", "Limit")}
            availableLabel={txt("Disponível", "Available")}
            overageLabel={txt("Excedente", "Overage")}
            used={`${formatNumber(usage.storage.media.used_mb, locale, 2)} MB`}
            limit={`${formatNumber(usage.storage.media.limit_mb, locale, 2)} MB`}
            available={`${formatNumber(usage.storage.media.available_mb, locale, 2)} MB`}
            overage={`${formatNumber(usage.storage.media.overage_mb, locale, 2)} MB`}
            overageValue={usage.storage.media.overage_bytes}
            percentUsed={usage.storage.media.percent_used}
            locale={locale}
            icon={FolderOpen}
            iconClassName="text-sky-400"
          />
        </div>
      </CardContent>
    </Card>
  );
}

// ── Credits (neurostack-credit-system Group 6) ───────────────────────────────
// Every figure below comes from `GET /v1/credits/*`, derived by the server
// from the credit ledger. The browser never computes, anticipates or keeps a
// balance: a checkout return, a PIX QR code or a pending payment changes
// nothing here until the server reads the grant from the ledger. While the
// credit switch is off the API answers `503 {"code": "credit_unavailable"}`
// and every credit panel stays hidden (the pages render as on `main`).

export type CreditStateName =
  | "active"
  | "pending"
  | "grace"
  | "suspended"
  | "deletion_due";

export interface CreditStateInfo {
  as_of: string;
  mode: "observational" | "enforced";
  enforcement_at: string | null;
  state: CreditStateName;
  pending_reasons: Array<"quarantine" | "standalone">;
  exhausted_at?: string | null;
  suspension_due_at?: string | null;
  deletion_due_at?: string | null;
  export_deadline_at?: string | null;
}

export interface CreditExpiry {
  lot_id: string;
  kind: "monthly" | "topup";
  expires_at: string;
  available_mc: number;
}

export interface CreditBalance {
  as_of: string;
  monthly_available_mc: number;
  monthly_not_started_mc: number;
  topup_available_mc: number;
  available_mc: number;
  open_debt_mc: number;
  net_available_mc: number;
  reserved_mc: number;
  next_expiry?: CreditExpiry | null;
}

export interface CreditDebt {
  as_of: string;
  open_debt_mc: number;
  debts: Array<{
    debt_id: string;
    source_kind: string;
    amount_mc: number;
    settled_mc: number;
    open_mc: number;
    created_at: string;
  }>;
}

export interface CreditReservation {
  category: "rag" | "media" | "egress";
  units: number;
  coverage_mc: number;
  charged_mc: number;
}

export interface CreditReservations {
  as_of: string;
  window: { start: string; end: string; kind: string };
  reservations: CreditReservation[];
  egress_allowance_bytes: number;
  egress_delivered_bytes: number;
}

export interface CreditThresholds {
  as_of: string;
  capacity_mc: number;
  available_mc: number;
  used_mc: number;
  used_percent: number;
  reached: number[];
  alerts: Array<{ threshold: number; occurred_at: string }>;
}

export interface CreditConsumption {
  as_of: string;
  mode: "observational" | "enforced";
  window: { start: string; end: string; kind: string };
  charged_mc: number;
  charged: Array<{ kind: string; amount_mc: number }>;
  observed_mc: number;
  observed: Array<{ kind: string; amount_mc: number }>;
}

export interface CreditSummary {
  as_of: string;
  state: CreditStateInfo;
  balance: CreditBalance;
  expiries: CreditExpiry[];
  debt: CreditDebt;
  reservations: CreditReservations;
  thresholds: CreditThresholds;
  consumption: CreditConsumption;
}

/** `loading` and `hidden` render nothing; only `ok` shows a credit panel. */
export type CreditLoad<T> =
  | { status: "loading" }
  | { status: "hidden" }
  | { status: "ok"; data: T };

const CREDIT_STATES = new Set<string>([
  "active",
  "pending",
  "grace",
  "suspended",
  "deletion_due",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function isInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value);
}

export function isCreditState(value: unknown): value is CreditStateInfo {
  return (
    isRecord(value) &&
    typeof value.state === "string" &&
    CREDIT_STATES.has(value.state) &&
    (value.mode === "observational" || value.mode === "enforced") &&
    Array.isArray(value.pending_reasons)
  );
}

export function isCreditBalance(value: unknown): value is CreditBalance {
  return (
    isRecord(value) &&
    [
      "monthly_available_mc",
      "monthly_not_started_mc",
      "topup_available_mc",
      "available_mc",
      "open_debt_mc",
      "net_available_mc",
      "reserved_mc",
    ].every((key) => isInteger(value[key]))
  );
}

export function isCreditReservations(
  value: unknown,
): value is CreditReservations {
  return (
    isRecord(value) &&
    Array.isArray(value.reservations) &&
    isInteger(value.egress_allowance_bytes) &&
    isInteger(value.egress_delivered_bytes)
  );
}

export function isCreditSummary(value: unknown): value is CreditSummary {
  return (
    isRecord(value) &&
    isCreditState(value.state) &&
    isCreditBalance(value.balance) &&
    Array.isArray(value.expiries) &&
    isRecord(value.debt) &&
    isInteger(value.debt.open_debt_mc) &&
    Array.isArray(value.debt.debts) &&
    isCreditReservations(value.reservations) &&
    isRecord(value.thresholds) &&
    isInteger(value.thresholds.used_percent) &&
    isRecord(value.consumption) &&
    isInteger(value.consumption.charged_mc)
  );
}

/**
 * One read of a credit route with the stored session. Any answer other than
 * a valid 200 -- `503 credit_unavailable`, a refusal, an error, a network
 * failure -- hides the panel: credit panels never break a page.
 */
export async function readCredit<T>(
  path: string,
  token: string,
  isValid: (value: unknown) => value is T,
  signal?: AbortSignal,
): Promise<CreditLoad<T>> {
  try {
    const response = await fetch(path, {
      headers: { Authorization: `Bearer ${token}` },
      signal,
    });
    if (response.status !== 200) return { status: "hidden" };
    const data: unknown = await response.json();
    return isValid(data) ? { status: "ok", data } : { status: "hidden" };
  } catch {
    return { status: "hidden" };
  }
}

function storedSession(): { token: string; role: string; viewing: string } {
  try {
    const raw = localStorage.getItem("ns_auth");
    const data = raw
      ? (JSON.parse(raw) as {
          token?: string;
          role?: string;
          viewingClinicId?: string;
          clinicId?: string;
        })
      : {};
    return {
      token: data.token ?? "",
      role: data.role ?? "",
      viewing: data.viewingClinicId ?? data.clinicId ?? "",
    };
  } catch {
    return { token: "", role: "", viewing: "" };
  }
}

/** Credit amounts are integer millicredits; shown as credits. */
export function formatCredits(mc: number, locale: "pt" | "en"): string {
  return (mc / 1000).toLocaleString(locale === "en" ? "en-US" : "pt-BR", {
    maximumFractionDigits: 3,
  });
}

function formatInstant(value: string | null | undefined, locale: "pt" | "en") {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(locale === "en" ? "en-US" : "pt-BR", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "America/Sao_Paulo",
  }).format(date);
}

function formatCivilDate(value: string, locale: "pt" | "en") {
  const [year, month, day] = value.split("-");
  if (!year || !month || !day) return value;
  return locale === "en" ? `${month}/${day}/${year}` : `${day}/${month}/${year}`;
}

type Txt = (pt: string, en: string) => string;

function CreditMetric({
  label,
  value,
  tone = "text-foreground",
  hint,
}: {
  label: string;
  value: string;
  tone?: string;
  hint?: string;
}) {
  return (
    <div
      role="group"
      aria-label={label}
      className="rounded-lg border border-border/60 bg-muted/15 p-3"
    >
      <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
        {label}
      </p>
      <p className={`mt-0.5 text-sm font-bold tabular-nums ${tone}`}>{value}</p>
      {hint && <p className="mt-0.5 text-[10px] text-muted-foreground">{hint}</p>}
    </div>
  );
}

/** The credit state, its deadlines and the export deadline. */
export function CreditStateNotice({
  state,
  locale,
}: {
  state: CreditStateInfo;
  locale: "pt" | "en";
}) {
  const txt: Txt = (pt, en) => (locale === "en" ? en : pt);
  const when = (value?: string | null) => formatInstant(value, locale);
  const exportLine = state.export_deadline_at
    ? txt(
        `Exporte seus dados até ${when(state.export_deadline_at)}.`,
        `Export your data by ${when(state.export_deadline_at)}.`,
      )
    : "";
  let tone = "border-amber-500/30 bg-amber-500/5 text-amber-300";
  let message = "";
  switch (state.state) {
    case "pending":
      message = state.pending_reasons.includes("standalone")
        ? txt(
            "Pendência financeira: esta clínica não pertence a um builder com conta de créditos.",
            "Financial pending: this clinic does not belong to a builder with a credit account.",
          )
        : txt(
            "Pendência financeira: um pagamento está em verificação. Os créditos ficam retidos até a conclusão.",
            "Financial pending: a payment is under verification. Credits stay on hold until it completes.",
          );
      break;
    case "grace":
      message = txt(
        `Créditos esgotados: o serviço continua como dívida até ${when(state.suspension_due_at)}. Recarregue para evitar a suspensão.`,
        `Credits exhausted: service continues as debt until ${when(state.suspension_due_at)}. Top up to avoid suspension.`,
      );
      break;
    case "suspended":
      tone = "border-red-500/40 bg-red-500/10 text-red-300";
      message = txt(
        "Serviço suspenso por falta de créditos. Recarregue para reativar.",
        "Service suspended for lack of credits. Top up to reactivate.",
      );
      break;
    case "deletion_due":
      tone = "border-red-500/40 bg-red-500/10 text-red-300";
      message = txt(
        "Prazo de retenção encerrado: o conteúdo pode ser removido. Recarregue para reativar.",
        "Retention deadline reached: content may be removed. Top up to reactivate.",
      );
      break;
    default:
      message = "";
  }
  return (
    <div className="space-y-2">
      {state.mode === "observational" && (
        <p
          data-testid="credit-mode"
          className="rounded-md border border-sky-500/30 bg-sky-500/5 px-3 py-2 text-xs text-sky-300"
        >
          {txt(
            "Modo observacional: o consumo é medido, mas ainda não é cobrado dos seus créditos.",
            "Observational mode: consumption is measured but not yet charged to your credits.",
          )}
        </p>
      )}
      {message && (
        <div
          role="status"
          data-testid="credit-state"
          data-state={state.state}
          className={`flex items-start gap-2 rounded-md border px-3 py-2 text-xs ${tone}`}
        >
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            {message} {exportLine}
          </span>
        </div>
      )}
    </div>
  );
}

const CONSUMPTION_LABELS: Record<string, [string, string]> = {
  trace: ["Decisões", "Decisions"],
  rag: ["Base de conhecimento", "Knowledge base"],
  media: ["Mídias", "Media"],
  egress: ["Downloads", "Downloads"],
  maintenance: ["Manutenção", "Maintenance"],
};

/** Balance by origin, expiries, debt, reservations, thresholds, consumption. */
export function CreditBalancePanel({
  summary,
  locale,
  exportAction,
}: {
  summary: CreditSummary;
  locale: "pt" | "en";
  exportAction?: {
    onExport: () => void;
    disabled: boolean;
    loading: boolean;
    message?: string | null;
  };
}) {
  const txt: Txt = (pt, en) => (locale === "en" ? en : pt);
  const credits = (mc: number) => `${formatCredits(mc, locale)} ${txt("créditos", "credits")}`;
  const { balance, debt, reservations, thresholds, consumption } = summary;
  const byCategory = new Map(
    reservations.reservations.map((item) => [item.category, item]),
  );
  const consumptionItems =
    consumption.mode === "enforced" ? consumption.charged : consumption.observed;
  return (
    <Card className="border-emerald-500/25 bg-card" data-testid="credit-panel">
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2">
          <Coins className="h-4 w-4 text-emerald-500" />
          <CardTitle className="text-sm font-semibold text-foreground">
            {txt("Saldo de créditos", "Credit balance")}
          </CardTitle>
          <span className="ml-auto text-[10px] text-muted-foreground">
            {txt("Atualizado em", "Updated at")} {formatInstant(summary.as_of, locale)}
          </span>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <CreditStateNotice state={summary.state} locale={locale} />
        <div className="grid grid-cols-2 gap-2 md:grid-cols-3">
          <CreditMetric
            label={txt("Franquia mensal", "Monthly allowance")}
            value={credits(balance.monthly_available_mc)}
          />
          <CreditMetric
            label={txt("Recargas", "Top-ups")}
            value={credits(balance.topup_available_mc)}
          />
          <CreditMetric
            label={txt("Dívida", "Debt")}
            value={credits(balance.open_debt_mc)}
            tone={balance.open_debt_mc > 0 ? "text-red-400" : "text-foreground"}
          />
          <CreditMetric
            label={txt("Reservas do ciclo", "Cycle reservations")}
            value={credits(balance.reserved_mc)}
            hint={txt(
              "Base de conhecimento e mídias já debitadas",
              "Knowledge base and media already debited",
            )}
          />
          <CreditMetric
            label={txt("Disponível líquido", "Net available")}
            value={credits(balance.net_available_mc)}
            tone="text-emerald-400"
          />
          <CreditMetric
            label={txt("Franquia do próximo ciclo", "Next cycle allowance")}
            value={credits(balance.monthly_not_started_mc)}
            hint={txt("Liberada no início do ciclo", "Usable when the cycle starts")}
          />
        </div>

        {summary.expiries.length > 0 && (
          <div data-testid="credit-expiries" className="space-y-1">
            <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
              {txt("Vencimentos", "Expiries")}
            </p>
            {summary.expiries.slice(0, 4).map((expiry) => (
              <div
                key={expiry.lot_id}
                className="flex items-center justify-between gap-2 text-xs"
              >
                <span className="flex items-center gap-1.5 text-muted-foreground">
                  <Clock className="h-3 w-3" />
                  {expiry.kind === "monthly"
                    ? txt("Franquia mensal", "Monthly allowance")
                    : txt("Recarga", "Top-up")}{" "}
                  · {formatInstant(expiry.expires_at, locale)}
                </span>
                <span className="font-semibold tabular-nums">
                  {credits(expiry.available_mc)}
                </span>
              </div>
            ))}
          </div>
        )}

        {debt.debts.length > 0 && (
          <div data-testid="credit-debts" className="space-y-1">
            <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
              {txt("Dívida em aberto", "Open debt")}
            </p>
            {debt.debts.map((item) => (
              <div
                key={item.debt_id}
                className="flex items-center justify-between gap-2 text-xs"
              >
                <span className="text-muted-foreground">
                  {item.source_kind === "reversal"
                    ? txt("Estorno de pagamento", "Payment refund")
                    : txt("Consumo acima do saldo", "Consumption beyond balance")}{" "}
                  · {formatInstant(item.created_at, locale)}
                </span>
                <span className="font-semibold tabular-nums text-red-400">
                  {credits(item.open_mc)}
                </span>
              </div>
            ))}
          </div>
        )}

        <div className="space-y-1" data-testid="credit-reservations">
          <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
            {txt("Alocações do ciclo", "Cycle allocations")} ·{" "}
            {formatCivilDate(reservations.window.start, locale)} {txt("a", "to")}{" "}
            {formatCivilDate(reservations.window.end, locale)}
          </p>
          {(["rag", "media", "egress"] as const).map((category) => {
            const item = byCategory.get(category);
            const label =
              category === "rag"
                ? txt("Base de conhecimento", "Knowledge base")
                : category === "media"
                  ? txt("Mídias", "Media")
                  : txt("Downloads excedentes", "Download overage");
            return (
              <div
                key={category}
                className="flex items-center justify-between gap-2 text-xs"
              >
                <span className="text-muted-foreground">{label}</span>
                <span className="tabular-nums">
                  {credits(item?.coverage_mc ?? 0)}
                </span>
              </div>
            );
          })}
        </div>

        <div className="space-y-1.5" data-testid="credit-thresholds">
          <div className="flex items-center justify-between text-xs">
            <span className="text-muted-foreground">
              {txt("Capacidade consumida", "Capacity used")}
            </span>
            <span className="font-semibold tabular-nums">
              {thresholds.used_percent}%
            </span>
          </div>
          <Progress
            value={clampPercent(thresholds.used_percent)}
            aria-label={txt("Capacidade consumida", "Capacity used")}
            className={`h-2 ${thresholds.reached.includes(90) ? "[&>div]:bg-red-500" : ""}`}
          />
          {thresholds.reached.length > 0 && (
            <p className="text-[11px] text-amber-400">
              {txt("Alertas atingidos", "Thresholds reached")}:{" "}
              {thresholds.reached.map((value) => `${value}%`).join(", ")}
            </p>
          )}
        </div>

        <div className="space-y-1" data-testid="credit-consumption">
          <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
            {consumption.mode === "enforced"
              ? txt("Consumo cobrado no ciclo", "Consumption charged this cycle")
              : txt(
                  "Consumo estimado no ciclo (não cobrado)",
                  "Estimated consumption this cycle (not charged)",
                )}
          </p>
          {consumptionItems.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              {txt("Nenhum consumo no ciclo.", "No consumption this cycle.")}
            </p>
          ) : (
            consumptionItems.map((item) => {
              const label = CONSUMPTION_LABELS[item.kind];
              return (
                <div
                  key={item.kind}
                  className="flex items-center justify-between gap-2 text-xs"
                >
                  <span className="text-muted-foreground">
                    {label ? txt(label[0], label[1]) : item.kind}
                  </span>
                  <span className="tabular-nums">{credits(item.amount_mc)}</span>
                </div>
              );
            })
          )}
        </div>

        {exportAction && (
          <div className="space-y-1.5 border-t border-border/40 pt-3">
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="gap-1.5"
              disabled={exportAction.disabled || exportAction.loading}
              onClick={exportAction.onExport}
            >
              <Download className="h-3.5 w-3.5" />
              {txt("Exportar dados (ZIP)", "Export data (ZIP)")}
            </Button>
            {exportAction.message && (
              <p role="alert" className="text-xs text-muted-foreground">
                {exportAction.message}
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/**
 * The current window's credit allocation of one content category, for the
 * knowledge-base and media pages. Shown only to a builder or super admin
 * viewing a clinic (the credit API refuses everyone else) and only when the
 * server answers 200: with the credit switch off nothing renders.
 */
export function CreditAllocationNotice({
  category,
  locale,
  tenantId,
  enabled,
}: {
  category: "rag" | "media";
  locale: "pt" | "en";
  tenantId: string;
  enabled: boolean;
}) {
  const txt: Txt = (pt, en) => (locale === "en" ? en : pt);
  const [load, setLoad] = useState<CreditLoad<CreditReservations>>({
    status: "loading",
  });
  useEffect(() => {
    setLoad({ status: "loading" });
    if (!enabled || !tenantId) return;
    const controller = new AbortController();
    const query = new URLSearchParams({ tenant_id: tenantId }).toString();
    void readCredit(
      `/v1/credits/reservations?${query}`,
      storedSession().token,
      isCreditReservations,
      controller.signal,
    ).then((result) => {
      if (!controller.signal.aborted) setLoad(result);
    });
    return () => controller.abort();
  }, [enabled, tenantId]);

  if (load.status !== "ok") return null;
  const data = load.data;
  const item = data.reservations.find((entry) => entry.category === category);
  const credits = (mc: number) =>
    `${formatCredits(mc, locale)} ${txt("créditos", "credits")}`;
  const title =
    category === "rag"
      ? txt("Créditos da base de conhecimento", "Knowledge-base credits")
      : txt("Créditos de mídia", "Media credits");
  return (
    <section
      aria-label={title}
      data-testid={`credit-allocation-${category}`}
      className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-emerald-500/25 bg-emerald-500/5 px-4 py-2.5 text-xs"
    >
      <span className="flex items-center gap-1.5 font-semibold text-emerald-400">
        <Coins className="h-3.5 w-3.5" />
        {title}
      </span>
      <span className="text-muted-foreground">
        {txt("Alocado no ciclo", "Allocated this cycle")}:{" "}
        <strong className="text-foreground">{credits(item?.coverage_mc ?? 0)}</strong>
      </span>
      <span className="text-muted-foreground">
        {txt("Debitado", "Debited")}:{" "}
        <strong className="text-foreground">{credits(item?.charged_mc ?? 0)}</strong>
      </span>
      {category === "media" && (
        <span className="text-muted-foreground">
          {txt("Downloads incluídos", "Included downloads")}:{" "}
          <strong className="text-foreground">
            {formatNumber(data.egress_delivered_bytes / 1_000_000, locale, 1)} /{" "}
            {formatNumber(data.egress_allowance_bytes / 1_000_000, locale, 1)} MB
          </strong>
        </span>
      )}
    </section>
  );
}

/** Role and selected clinic of the stored session (pages without context). */
export function storedCreditViewer(): { canRead: boolean; tenantId: string } {
  const session = storedSession();
  const canRead =
    Boolean(session.token) &&
    (session.role === "builder" || session.role === "super_admin");
  return { canRead, tenantId: session.viewing };
}
