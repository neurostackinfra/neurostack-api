import { useState, type ElementType } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "wouter";
import { differenceInCalendarDays, format, isValid, parseISO, subDays } from "date-fns";
import { CalendarRange, Lightbulb, MessageSquareText, Sparkles, UserPlus, Users2 } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { DateRangePicker } from "@/components/date-range-picker";
import { useAuth } from "@/context/auth";
import { useI18n } from "@/context/i18n";
import {
  CONSULTORIA_COPY,
  buildInsights,
  pickQueueStats,
  type ConsultoriaQueueStats,
  type HandoffQueuePayload,
  type ConsultoriaTraceStats,
  type Insight,
  type InsightId,
  type RequestResult,
} from "@/lib/consultoria-insights";

// -- Period ---

const DEFAULT_DAYS = 30;
const MAX_DAYS = 90;

function defaultDates() {
  const today = new Date();
  return {
    startDate: format(subDays(today, DEFAULT_DAYS), "yyyy-MM-dd"),
    endDate: format(today, "yyyy-MM-dd"),
  };
}

/** Both dates valid, end not before start, and at most MAX_DAYS calendar days apart. */
function isValidPeriod(startDate: string, endDate: string): boolean {
  const start = parseISO(startDate);
  const end = parseISO(endDate);
  if (!isValid(start) || !isValid(end)) return false;
  const span = differenceInCalendarDays(end, start);
  return span >= 0 && span <= MAX_DAYS;
}

// -- Requests ---

class HttpError extends Error {
  constructor(readonly status: number) {
    super(`Request failed: ${status}`);
  }
}

function getToken(): string | null {
  try {
    const raw = localStorage.getItem("ns_auth");
    if (!raw) return null;
    return (JSON.parse(raw) as { token?: string }).token ?? null;
  } catch {
    return null;
  }
}

async function getJson<T>(path: string, params: URLSearchParams): Promise<T> {
  const token = getToken();
  if (!token) throw new Error("Missing session token");
  const res = await fetch(`${path}?${params.toString()}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) throw new HttpError(res.status);
  return (await res.json()) as T;
}

/** Data already on screen wins: a failed background refetch never replaces it with an error. */
function toResult<T>(query: { data: T | undefined; isError: boolean; error: unknown }): RequestResult<T> {
  if (query.data !== undefined) return { status: "success", data: query.data };
  if (query.isError) {
    return { status: "error", httpStatus: query.error instanceof HttpError ? query.error.status : undefined };
  }
  return { status: "loading" };
}

/** One rule for a bad period: caught here or answered by the server as 400. */
const INVALID_PERIOD: RequestResult<never> = { status: "error", httpStatus: 400 };

// -- Cards ---

const INSIGHT_ICONS: Record<InsightId, ElementType> = {
  outside_hours: CalendarRange,
  new_contacts: UserPlus,
  top_intents: MessageSquareText,
  team_sla: Users2,
};

function InsightCard({ insight }: { insight: Insight }) {
  const Icon = INSIGHT_ICONS[insight.id];
  return (
    <Card data-testid={`insight-${insight.id}`} data-state={insight.state} className="flex flex-col">
      <CardHeader className="flex flex-row items-center gap-2 space-y-0 pb-3">
        <Icon className="h-4 w-4 text-violet-500 shrink-0" />
        <CardTitle className="text-base">{insight.title}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-1 flex-col gap-4">
        {insight.state === "loading" ? (
          <div className="space-y-2" data-testid={`insight-${insight.id}-loading`}>
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="h-4 w-1/2" />
          </div>
        ) : (
          <div className="space-y-1.5">
            {insight.lines.map((line, index) => (
              <p key={index} className="text-sm text-foreground tabular-nums">{line}</p>
            ))}
            {insight.statusMessage && (
              <p className="text-sm text-muted-foreground">{insight.statusMessage}</p>
            )}
            {insight.action && (
              <Link
                href={insight.action.href}
                className="inline-block text-sm font-medium text-violet-500 hover:underline"
              >
                {insight.action.label}
              </Link>
            )}
          </div>
        )}
        {insight.premise && (
          <div className="mt-auto flex items-start gap-3 rounded-lg border border-violet-500/20 bg-violet-500/5 px-4 py-3">
            <Lightbulb className="h-4 w-4 text-violet-500 mt-0.5 shrink-0" />
            <p className="text-sm text-foreground/85 leading-relaxed" data-testid={`insight-${insight.id}-premise`}>
              {insight.premise}
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// -- Page ---

export default function Consultoria() {
  const { locale } = useI18n();
  const { tenantId, isBuilder, isSuperAdmin, viewingClinicId } = useAuth();
  const copy = locale === "en" ? CONSULTORIA_COPY.en : CONSULTORIA_COPY.pt;

  const effectiveTenantId = (isBuilder || isSuperAdmin ? viewingClinicId : tenantId) || null;
  const [initialDates] = useState(defaultDates);
  const [startDate, setStartDate] = useState(initialDates.startDate);
  const [endDate, setEndDate] = useState(initialDates.endDate);
  const periodValid = isValidPeriod(startDate, endDate);
  const enabled = Boolean(effectiveTenantId) && periodValid;

  const statsQuery = useQuery<ConsultoriaTraceStats>({
    queryKey: ["consultoria", "trace-stats", startDate, endDate, effectiveTenantId],
    enabled,
    retry: false,
    staleTime: 30_000,
    queryFn: () => {
      const params = new URLSearchParams({ start_date: startDate, end_date: endDate });
      // Only builder and super admin may name the clinic; a clinic login gets a 400 for it.
      if ((isBuilder || isSuperAdmin) && effectiveTenantId) params.set("tenant_id", effectiveTenantId);
      return getJson<ConsultoriaTraceStats>("/v1/traces/stats", params);
    },
  });

  const queueQuery = useQuery<ConsultoriaQueueStats>({
    queryKey: ["consultoria", "handoff-queue", startDate, endDate, effectiveTenantId],
    enabled,
    retry: false,
    staleTime: 30_000,
    queryFn: async () => {
      const params = new URLSearchParams({
        tenant_id: effectiveTenantId ?? "",
        from_date: startDate,
        to_date: endDate,
      });
      return pickQueueStats(await getJson<HandoffQueuePayload>("/v1/handoffs/queue", params));
    },
  });

  const insights = buildInsights(
    periodValid ? toResult(statsQuery) : INVALID_PERIOD,
    periodValid ? toResult(queueQuery) : INVALID_PERIOD,
    locale,
  );

  return (
    <div className="space-y-8 max-w-5xl">
      <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-violet-500/10 border border-violet-500/20 shrink-0">
            <Lightbulb className="h-5 w-5 text-violet-500" />
          </div>
          <div>
            <h1 className="text-2xl font-bold tracking-tight text-foreground">{copy.page.title}</h1>
            <p className="text-sm text-muted-foreground mt-0.5">{copy.page.subtitle}</p>
          </div>
        </div>
        <DateRangePicker
          startDate={startDate} endDate={endDate}
          onStartChange={setStartDate} onEndChange={setEndDate}
          maxDays={MAX_DAYS}
        />
      </div>

      {effectiveTenantId ? (
        <div className="grid gap-4 md:grid-cols-2">
          {insights.map((insight) => (
            <InsightCard key={insight.id} insight={insight} />
          ))}
        </div>
      ) : (
        <Card data-testid="consultoria-no-clinic">
          <CardContent className="py-12 text-center text-sm text-muted-foreground">
            {copy.page.noClinic}
          </CardContent>
        </Card>
      )}

      <section className="space-y-3" data-testid="consultoria-coming-soon">
        <h2 className="flex items-center gap-2 text-sm font-semibold uppercase tracking-wider text-muted-foreground">
          <Sparkles className="h-4 w-4 text-violet-500" />
          {copy.comingSoon.badge}
        </h2>
        <ul className="grid gap-3 sm:grid-cols-2">
          {copy.comingSoon.items.map((item) => (
            <li
              key={item.name}
              className="rounded-lg border border-border/70 bg-muted/15 p-4"
              data-testid="consultoria-coming-soon-item"
            >
              <div className="flex items-start justify-between gap-3">
                <p className="text-sm text-foreground/85 leading-relaxed">
                  <span className="font-semibold text-foreground">{item.name}</span>: {item.description}
                </p>
                <Badge variant="outline" className="shrink-0 text-[10px] uppercase tracking-wider text-violet-500 border-violet-500/30">
                  {copy.comingSoon.badge}
                </Badge>
              </div>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
