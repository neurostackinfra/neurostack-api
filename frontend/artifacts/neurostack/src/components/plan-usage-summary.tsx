import {
  Activity,
  CalendarRange,
  Database,
  FolderOpen,
  ShoppingCart,
} from "lucide-react";
import type { ElementType } from "react";
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
}: PlanUsageSummaryProps) {
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
            {usage.credits.coming_soon && (
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
              disabled
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
