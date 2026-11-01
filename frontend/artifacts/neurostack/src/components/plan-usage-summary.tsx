import {
  Activity,
  AlertTriangle,
  CalendarRange,
  Coins,
  Database,
  FolderOpen,
  ShoppingCart,
} from "lucide-react";
import { useEffect, useState, type ElementType } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { formatCredits, formatNumber, type Txt } from "@/components/billing/format";

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

/**
 * What the page could read for the usage card. Only `ready` carries numbers;
 * every other state renders its own copy and no figure, so a failed or denied
 * read never looks like zero usage. The usage contract (`BillingUsageResponse`)
 * has no per-resource "not applicable" flag, so there is no such state here.
 */
export type PlanUsageState =
  | { status: "loading" }
  | { status: "unavailable" }
  | { status: "error" }
  | { status: "ready"; usage: BillingUsage };

type PlanUsageSummaryProps = {
  locale: "pt" | "en";
  className?: string;
  /**
   * Credit purchase (Group 6). No page passes it today: with
   * `variant="dashboard"` the absent prop keeps the button disabled, as on
   * `main`; with `variant="billing"` the button is shown only when `enabled`
   * is true, so the absent prop hides it. The payment screen's only purchase
   * action is `Recarregar`, in `CreditsAndTopups`.
   */
  creditPurchase?: { enabled: boolean; onBuy: () => void };
  /**
   * `dashboard` (default) is the original card. `billing` is the payments
   * page: RAG, Traces and Media in that order, no legacy plan badge (the
   * subscription overview owns the plan) and no "coming soon" badge or dead
   * purchase button while top-ups are closed.
   */
  variant?: "dashboard" | "billing";
} & ({ usage: BillingUsage; state?: never } | { state: PlanUsageState; usage?: never });

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
          {/* Wraps instead of truncating: a label is never cut off. */}
          <p className="text-sm font-semibold leading-tight text-foreground break-words">
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
          <p className="mt-0.5 text-sm font-bold text-emerald-800 dark:text-emerald-400 tabular-nums">
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

function UsageStateCard({
  state,
  locale,
  className,
}: {
  state: Exclude<PlanUsageState, { status: "ready" }>;
  locale: "pt" | "en";
  className: string;
}) {
  const txt = (pt: string, en: string) => (locale === "en" ? en : pt);
  const message =
    state.status === "loading"
      ? txt("Carregando o uso do plano…", "Loading plan usage…")
      : state.status === "unavailable"
        ? txt(
            "Uso temporariamente indisponível: o servidor de pagamentos não respondeu. Nenhum valor foi presumido.",
            "Usage is temporarily unavailable: the billing server did not answer. No value was assumed.",
          )
        : txt(
            "Não foi possível ler o uso do plano. Nenhum valor foi presumido.",
            "Plan usage could not be read. No value was assumed.",
          );
  return (
    <Card
      data-testid="plan-usage"
      data-state={state.status}
      className={`border-indigo-500/25 bg-gradient-to-br from-indigo-950/15 to-transparent ${className}`.trim()}
    >
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2">
          <Activity className="h-4 w-4 text-indigo-400" aria-hidden="true" />
          <CardTitle className="text-sm">{txt("Uso do plano", "Plan usage")}</CardTitle>
        </div>
      </CardHeader>
      <CardContent>
        <p
          className="flex items-center gap-2 text-xs text-muted-foreground"
          {...(state.status === "loading" ? { role: "status", "aria-live": "polite" as const } : {})}
        >
          {state.status === "error" && (
            <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-destructive" aria-hidden="true" />
          )}
          {message}
        </p>
      </CardContent>
    </Card>
  );
}

export function PlanUsageSummary(props: PlanUsageSummaryProps) {
  const { locale, className = "", creditPurchase, variant = "dashboard" } = props;
  const state: PlanUsageState = props.state ?? { status: "ready", usage: props.usage };
  if (state.status !== "ready") {
    return <UsageStateCard state={state} locale={locale} className={className} />;
  }
  return (
    <UsageReady
      usage={state.usage}
      locale={locale}
      className={className}
      creditPurchase={creditPurchase}
      variant={variant}
    />
  );
}

function UsageReady({
  usage,
  locale,
  className,
  creditPurchase,
  variant,
}: {
  usage: BillingUsage;
  locale: "pt" | "en";
  className: string;
  creditPurchase?: { enabled: boolean; onBuy: () => void };
  variant: "dashboard" | "billing";
}) {
  const billing = variant === "billing";
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

  const traceMeter = (
    <UsageMeter
      key="traces"
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
  );
  const ragMeter = (
    <UsageMeter
      key="rag"
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
  );
  const mediaMeter = (
    <UsageMeter
      key="media"
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
  );
  const meters = billing ? [ragMeter, traceMeter, mediaMeter] : [traceMeter, ragMeter, mediaMeter];
  // On the payments page a closed purchase is absent, not a dead button.
  const showPurchase = !billing || purchaseEnabled;

  return (
    <Card
      data-testid="plan-usage"
      data-state="ready"
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
              {!billing && (
                <Badge
                  variant="outline"
                  className="text-[9px] uppercase tracking-wider border-indigo-500/30 text-indigo-400"
                >
                  {planLabel}
                </Badge>
              )}
            </div>
            <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <CalendarRange className="h-3 w-3" />
              <span>
                {txt("Período de traces", "Trace period")}: {period} ·{" "}
                {timezone}
              </span>
            </div>
          </div>

          {showPurchase && (
            <div className="flex items-center gap-2 sm:justify-end">
              {!billing && usage.credits.coming_soon && !purchaseEnabled && (
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
          )}
        </div>
      </CardHeader>

      <CardContent>
        <div className="grid gap-3 lg:grid-cols-3">{meters}</div>
      </CardContent>
    </Card>
  );
}

// ── Credits shared with the knowledge-base and media pages ──────────────────
// Every figure below comes from `GET /v1/credits/*`, derived by the server
// from the credit ledger. While the credit switch is off the API answers
// `503 {"code": "credit_unavailable"}` and the allocation notice renders
// nothing. The billing page's credit panels live in
// `components/billing/credits-and-topups.tsx` and read through
// `pages/billing-contract.ts`, which keeps refusals, failures and the switch
// apart instead of hiding them all.

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

/** `loading` and `hidden` render nothing; only `ok` shows a credit notice. */
export type CreditLoad<T> =
  | { status: "loading" }
  | { status: "hidden" }
  | { status: "ok"; data: T };

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function isInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value);
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

/**
 * One read of a credit route with the stored session. Any answer other than
 * a valid 200 -- `503 credit_unavailable`, a refusal, an error, a network
 * failure -- hides the notice: an allocation notice never breaks a page.
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
