import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/context/auth";
import { useI18n } from "@/context/i18n";
import type { TranslationKey } from "@/locales/en";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { AlertTriangle, Clock, ListChecks, ActivitySquare, Zap, CalendarRange, UserPlus } from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { DateRangePicker } from "@/components/date-range-picker";
import { PieChart, Pie, Cell, Tooltip, ResponsiveContainer } from "recharts";
import { format } from "date-fns";
import { getIntentLabel, getIntentPresentation, INTENT_PRESENTATION } from "@/lib/intent-presentation";

// -- Default date range: last 7 days ---

function defaultDates() {
  const today    = format(new Date(), "yyyy-MM-dd");
  const sevenAgo = format(new Date(Date.now() - 7 * 24 * 60 * 60 * 1000), "yyyy-MM-dd");
  return { startDate: sevenAgo, endDate: today };
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

// -- Custom tooltip ---

function DonutTooltip({ active, payload }: { active?: boolean; payload?: { name: string; value: number; payload: { pct: number } }[] }) {
  if (!active || !payload?.length) return null;
  const d = payload[0];
  return (
    <div className="rounded-lg border border-border bg-popover px-3 py-2 text-xs shadow-lg">
      <p className="font-semibold text-foreground">{d.name}</p>
      <p className="text-muted-foreground mt-0.5">{d.value} · {d.payload.pct.toFixed(1)}%</p>
    </div>
  );
}

// -- KPI stats interface ---

interface KpiStats {
  total: number;
  avgProcessingTimeMs: number;
  safetyFlagCount: number;
  handoffCount: number;
  totalTokensSum: number;
  modelBreakdown: { model: string; tokens: number }[];
  byIntent: Record<string, number>;
  outsideHoursCount: number;
  outsideHoursPct: number | null;
  conversationalTraceCount: number;
  businessHoursConfigured: boolean;
  newUsersCount: number;
  returningUsersCount: number;
  newUserInteractions: number;
  returningUserInteractions: number;
  newUserInteractionPct: number | null;
  returningUserInteractionPct: number | null;
  identifiedUserInteractions: number;
  unidentifiedUserInteractions: number;
}

// -- Technical KPI cards ---

export function KpiCards({ stats, t, locale }: { stats: KpiStats; t: (k: TranslationKey) => string; locale: string }) {
  const maxModelTokens = stats.modelBreakdown[0]?.tokens ?? 0;
  const txt = (pt: string, en: string) => locale === "en" ? en : pt;
  const outsideHoursHasData = stats.businessHoursConfigured
    && stats.conversationalTraceCount > 0
    && stats.outsideHoursPct !== null;

  return (
    <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-6">
      <Card data-testid="stat-total-traces">
        <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
          <CardTitle className="text-sm font-medium">{t("dash_total")}</CardTitle>
          <ListChecks className="h-4 w-4 text-muted-foreground" />
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-bold text-foreground">{stats.total.toLocaleString("pt-BR")}</div>
          <p className="text-xs text-muted-foreground mt-0.5">interacoes processadas</p>
        </CardContent>
      </Card>

      <Card data-testid="stat-avg-processing-time">
        <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
          <CardTitle className="text-sm font-medium">{t("dash_avg_time")}</CardTitle>
          <Clock className="h-4 w-4 text-muted-foreground" />
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-bold text-foreground">{Math.round(stats.avgProcessingTimeMs).toLocaleString("pt-BR")}ms</div>
          <p className="text-xs text-muted-foreground mt-0.5">latencia pipeline SRM-LLM</p>
        </CardContent>
      </Card>

      <Card data-testid="stat-safety-flags">
        <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
          <CardTitle className="text-sm font-medium">{t("dash_safety")}</CardTitle>
          <AlertTriangle className="h-4 w-4 text-destructive" />
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-bold text-foreground">{stats.safetyFlagCount.toLocaleString("pt-BR")}</div>
          <p className="text-xs text-muted-foreground mt-0.5">alertas de seguranca</p>
        </CardContent>
      </Card>

      <Card data-testid="stat-handoffs">
        <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
          <CardTitle className="text-sm font-medium">{t("dash_handoffs")}</CardTitle>
          <ActivitySquare className="h-4 w-4 text-orange-500" />
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-bold text-foreground">{stats.handoffCount.toLocaleString("pt-BR")}</div>
          <p className="text-xs text-muted-foreground mt-0.5">transferencias para humano</p>
        </CardContent>
      </Card>

      <Card data-testid="stat-outside-hours">
        <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
          <CardTitle className="text-sm font-medium">{txt("Atendimentos fora do horário", "Outside hours")}</CardTitle>
          <CalendarRange className="h-4 w-4 text-violet-400" />
        </CardHeader>
        <CardContent>
          {!stats.businessHoursConfigured ? (
            <div className="text-base font-semibold text-amber-400">{txt("Configure o horário", "Configure business hours")}</div>
          ) : outsideHoursHasData ? (
            <div className="text-2xl font-bold text-foreground">
              {stats.outsideHoursPct!.toLocaleString(locale === "en" ? "en-US" : "pt-BR", { maximumFractionDigits: 1 })}%
            </div>
          ) : (
            <div className="text-base font-semibold text-muted-foreground">{txt("Sem dados", "No data")}</div>
          )}
          {stats.businessHoursConfigured && (
            <p className="text-xs text-muted-foreground mt-0.5 tabular-nums">
              {stats.outsideHoursCount.toLocaleString(locale === "en" ? "en-US" : "pt-BR")} / {stats.conversationalTraceCount.toLocaleString(locale === "en" ? "en-US" : "pt-BR")} {txt("conversas", "conversations")}
            </p>
          )}
        </CardContent>
      </Card>

      <Card data-testid="stat-token-usage" className="md:col-span-2 lg:col-span-1">
        <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
          <CardTitle className="text-sm font-medium">{t("dash_tokens")}</CardTitle>
          <Zap className="h-4 w-4 text-muted-foreground" />
        </CardHeader>
        <CardContent className="space-y-3">
          <div>
            <div className="text-2xl font-bold text-foreground">{(stats.totalTokensSum ?? 0).toLocaleString("pt-BR")}</div>
            <p className="text-xs text-muted-foreground mt-0.5">acompanhamento em tempo real</p>
          </div>
          {stats.modelBreakdown.length > 0 && (
            <div className="space-y-2 pt-1 border-t border-border">
              {stats.modelBreakdown.map(({ model, tokens }) => {
                const pct = maxModelTokens > 0 ? (tokens / maxModelTokens) * 100 : 0;
                return (
                  <div key={model} className="space-y-1">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-[11px] font-medium text-foreground truncate max-w-[120px]" title={model}>{model}</span>
                      <span className="text-[11px] text-muted-foreground shrink-0 tabular-nums">{tokens.toLocaleString("pt-BR")}</span>
                    </div>
                    <div className="h-1 w-full rounded-full bg-muted overflow-hidden">
                      <div className="h-full bg-primary rounded-full transition-all duration-500" style={{ width: `${pct}%` }} />
                    </div>
                  </div>
                );
              })}
            </div>
          )}
          {stats.modelBreakdown.length === 0 && (
            <p className="text-[11px] text-muted-foreground pt-1 border-t border-border">Nenhum dado de modelo no periodo</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// -- Intent breakdown chart ---

const VISIBLE_INTENTS = ["pedido_preco", "agendar", "interesse_tratamento", "reclamacao", "pos_atendimento_duvida"];

export function IntentBreakdown({
  byIntent, t, locale, intentFilter, onIntentFilterChange,
}: {
  byIntent: Record<string, number>;
  t: (k: TranslationKey) => string;
  locale: string;
  intentFilter: string;
  onIntentFilterChange: (v: string) => void;
}) {
  const baseIntent = Object.fromEntries(
    VISIBLE_INTENTS.map(k => [k, byIntent[k] ?? 0])
  );
  const periodIntentTotal = Object.values(baseIntent).reduce((s, n) => s + n, 0);
  const visibleIntent = Object.fromEntries(
    Object.entries(baseIntent).filter(([k]) => !intentFilter || k === intentFilter)
  );
  const visibleIntentTotal = Object.values(visibleIntent).reduce((s, n) => s + n, 0);
  const numberLocale = locale === "en" ? "en-US" : "pt-BR";
  const chartData = Object.entries(visibleIntent)
    .map(([intent, count]) => ({
      key: intent,
      name: getIntentLabel(intent, t),
      value: count,
      hex: getIntentPresentation(intent).hex,
      pct: periodIntentTotal > 0 ? (count / periodIntentTotal) * 100 : 0,
    }));
  const emptyChart = chartData.length === 0;

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
          <CardTitle className="text-base">{t("dash_intents")}</CardTitle>
          <select
            value={intentFilter}
            onChange={e => onIntentFilterChange(e.target.value)}
            className="rounded-md border border-border bg-background px-3 py-1.5 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-accent h-8 w-full sm:w-48"
          >
            <option value="">{t("dash_intent_all")}</option>
            {VISIBLE_INTENTS.map(key => (
              <option key={key} value={key}>{getIntentLabel(key, t)}</option>
            ))}
          </select>
        </div>
      </CardHeader>
      <CardContent>
        <div className="grid gap-8 sm:grid-cols-2 items-center">
          <div className="space-y-3.5">
            {Object.entries(visibleIntent).map(([intent, count]) => {
              const pct = periodIntentTotal > 0 ? (count / periodIntentTotal) * 100 : 0;
              const m = getIntentPresentation(intent);
              return (
                <div key={intent} className="space-y-1" data-testid={`stat-intent-${intent}`}>
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-1.5 min-w-0">
                      <span className={`h-2 w-2 rounded-full shrink-0 ${m.baseClassName}`} />
                      <span className="text-xs font-medium text-foreground truncate">{getIntentLabel(intent, t)}</span>
                    </div>
                    <span className="text-xs font-bold text-foreground shrink-0 tabular-nums">
                      {count.toLocaleString(numberLocale)}
                      <span className="ml-1 font-medium text-muted-foreground">
                        {"\u00b7 "}{pct.toLocaleString(numberLocale, { maximumFractionDigits: 1 })}%
                      </span>
                    </span>
                  </div>
                  <div className="h-1.5 w-full rounded-full bg-muted overflow-hidden">
                    <div className={`h-full ${m.baseClassName} rounded-full transition-all duration-500`} style={{ width: `${pct}%` }} />
                  </div>
                </div>
              );
            })}
            {visibleIntentTotal === 0 && (
              <div className="text-sm text-muted-foreground">{t("dash_no_intents")}</div>
            )}
          </div>

          <div className="flex flex-col items-center gap-3">
            <div className="relative w-full max-w-[220px]">
              <ResponsiveContainer width="100%" height={200}>
                <PieChart>
                  <Pie
                    data={emptyChart ? [{ name: "Sem dados", value: 1, hex: "#1e293b", pct: 0 }] : chartData}
                    dataKey="value"
                    nameKey="name"
                    cx="50%"
                    cy="50%"
                    innerRadius={62}
                    outerRadius={90}
                    strokeWidth={0}
                    paddingAngle={emptyChart ? 0 : 2}
                    startAngle={90}
                    endAngle={-270}
                  >
                    {(emptyChart ? [{ hex: "#1e293b" }] : chartData).map((entry, i) => (
                      <Cell key={i} fill={entry.hex} />
                    ))}
                  </Pie>
                  {!emptyChart && <Tooltip content={<DonutTooltip />} />}
                </PieChart>
              </ResponsiveContainer>
              <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
                <span className="text-2xl font-bold text-foreground tabular-nums leading-none">{visibleIntentTotal}</span>
                <span className="text-[10px] text-muted-foreground uppercase tracking-wider font-medium mt-1">
                  {intentFilter ? getIntentLabel(intentFilter, t).split(" ")[0] : t("dash_intent_total")}
                </span>
              </div>
            </div>
            <div className="flex flex-wrap justify-center gap-x-3 gap-y-1.5">
              {Object.entries(INTENT_PRESENTATION)
                .filter(([key]) => VISIBLE_INTENTS.includes(key) && (!intentFilter || key === intentFilter))
                .map(([key, m]) => (
                  <div key={key} className="flex items-center gap-1">
                    <span className="h-2 w-2 rounded-full shrink-0" style={{ backgroundColor: m.hex }} />
                    <span className="text-[10px] text-muted-foreground">{getIntentLabel(key, t).split(" ")[0]}</span>
                  </div>
                ))}
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

// -- Commercial audience growth ---

function UserAcquisitionMetrics({ stats, locale }: { stats: KpiStats; locale: string }) {
  const txt = (pt: string, en: string) => locale === "en" ? en : pt;
  const numberLocale = locale === "en" ? "en-US" : "pt-BR";
  const hasIdentifiedInteractions = stats.identifiedUserInteractions > 0;
  const chartData = [
    {
      name: txt("Novos usuários", "New users"),
      value: stats.newUserInteractions,
      pct: stats.newUserInteractionPct ?? 0,
      hex: "#10b981",
    },
    {
      name: txt("Usuários recorrentes", "Returning users"),
      value: stats.returningUserInteractions,
      pct: stats.returningUserInteractionPct ?? 0,
      hex: "#3b82f6",
    },
  ];

  return (
    <Card data-testid="user-acquisition-metrics">
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-4">
          <div>
            <CardTitle className="text-base">{txt("Crescimento de usuários", "User growth")}</CardTitle>
            <p className="text-xs text-muted-foreground mt-1">
              {txt(
                "Usuário novo é o número ou username cuja primeira interação histórica ocorreu no período selecionado.",
                "A new user is a phone number or username whose first-ever interaction occurred in the selected period.",
              )}
            </p>
          </div>
          <UserPlus className="h-5 w-5 text-emerald-500 shrink-0" />
        </div>
      </CardHeader>
      <CardContent>
        <div className="grid gap-6 lg:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)] items-center">
          <div className="rounded-xl border border-emerald-500/25 bg-emerald-500/5 p-5">
            <p className="text-xs font-semibold uppercase tracking-wider text-emerald-500">
              {txt("Novos usuários no período", "New users in period")}
            </p>
            <div className="mt-2 text-4xl font-bold text-foreground tabular-nums" data-testid="stat-new-users">
              {stats.newUsersCount.toLocaleString(numberLocale)}
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              {txt("primeira interação registrada na NeuroStack", "first interaction recorded in NeuroStack")}
            </p>
            <div className="mt-4 border-t border-border/70 pt-3 flex items-center justify-between gap-3 text-xs">
              <span className="text-muted-foreground">{txt("Usuários recorrentes ativos", "Active returning users")}</span>
              <span className="font-semibold text-foreground tabular-nums">
                {stats.returningUsersCount.toLocaleString(numberLocale)}
              </span>
            </div>
          </div>

          <div className="grid gap-4 sm:grid-cols-[220px_minmax(0,1fr)] items-center">
            <div className="relative mx-auto w-full max-w-[220px]">
              <ResponsiveContainer width="100%" height={210}>
                <PieChart>
                  <Pie
                    data={hasIdentifiedInteractions ? chartData : [{ name: txt("Sem dados", "No data"), value: 1, pct: 0, hex: "#334155" }]}
                    dataKey="value"
                    nameKey="name"
                    cx="50%"
                    cy="50%"
                    innerRadius={62}
                    outerRadius={92}
                    strokeWidth={0}
                    paddingAngle={hasIdentifiedInteractions ? 2 : 0}
                    startAngle={90}
                    endAngle={-270}
                  >
                    {(hasIdentifiedInteractions ? chartData : [{ hex: "#334155" }]).map((entry, index) => (
                      <Cell key={index} fill={entry.hex} />
                    ))}
                  </Pie>
                  {hasIdentifiedInteractions && <Tooltip content={<DonutTooltip />} />}
                </PieChart>
              </ResponsiveContainer>
              <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
                <span className="text-2xl font-bold text-foreground tabular-nums">
                  {stats.identifiedUserInteractions.toLocaleString(numberLocale)}
                </span>
                <span className="mt-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                  {txt("interações", "interactions")}
                </span>
              </div>
            </div>

            <div className="space-y-3">
              {chartData.map((item) => (
                <div key={item.name} className="rounded-lg border border-border/70 bg-muted/15 p-3" data-testid={`audience-${item.hex === "#10b981" ? "new" : "returning"}`}>
                  <div className="flex items-center justify-between gap-3">
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="h-2.5 w-2.5 rounded-full shrink-0" style={{ backgroundColor: item.hex }} />
                      <span className="text-xs font-medium text-foreground truncate">{item.name}</span>
                    </div>
                    <span className="text-lg font-bold text-foreground tabular-nums">
                      {hasIdentifiedInteractions ? `${item.pct.toLocaleString(numberLocale, { maximumFractionDigits: 1 })}%` : "—"}
                    </span>
                  </div>
                  <p className="mt-1 pl-[18px] text-[11px] text-muted-foreground tabular-nums">
                    {item.value.toLocaleString(numberLocale)} {txt("interações", "interactions")}
                  </p>
                </div>
              ))}
              {stats.unidentifiedUserInteractions > 0 && (
                <p className="text-[11px] text-muted-foreground">
                  {stats.unidentifiedUserInteractions.toLocaleString(numberLocale)} {txt(
                    "interações sem número/username foram excluídas dos percentuais.",
                    "interactions without a phone/username were excluded from percentages.",
                  )}
                </p>
              )}
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

// -- Main Dashboard ---

// ── Investor mock stats ──
const INVESTOR_MOCK_STATS: KpiStats = {
  total: 291,
  avgProcessingTimeMs: 849,
  safetyFlagCount: 3,
  handoffCount: 11,
  totalTokensSum: 66967,
  modelBreakdown: [
    { model: "gpt-4o-mini", tokens: 46275 },
    { model: "claude-3-haiku", tokens: 20692 },
  ],
  byIntent: {
    pedido_preco: 78,
    agendar: 64,
    interesse_tratamento: 52,
    reclamacao: 12,
    pos_atendimento_duvida: 31,
  },
  outsideHoursCount: 24,
  outsideHoursPct: 8.2,
  conversationalTraceCount: 291,
  businessHoursConfigured: true,
  newUsersCount: 47,
  returningUsersCount: 83,
  newUserInteractions: 116,
  returningUserInteractions: 175,
  newUserInteractionPct: 39.9,
  returningUserInteractionPct: 60.1,
  identifiedUserInteractions: 291,
  unidentifiedUserInteractions: 0,
};

export default function Dashboard() {
  const { t, locale } = useI18n();
  const { isBuilder, isSuperAdmin, isInvestor, viewingClinicId } = useAuth();
  const { startDate: defStart, endDate: defEnd } = defaultDates();
  const [startDate, setStartDate] = useState(defStart);
  const [endDate, setEndDate] = useState(defEnd);
  const [intentFilter, setIntentFilter] = useState("");

  const { data: stats, isLoading, error } = useQuery<KpiStats>({
    queryKey: ["trace-stats", startDate, endDate, (isBuilder || isSuperAdmin) ? viewingClinicId : null, isInvestor],
    enabled: !((isBuilder || isSuperAdmin) && !viewingClinicId),
    queryFn: async () => {
      // Investor: return mock stats
      if (isInvestor) return INVESTOR_MOCK_STATS;
      const token = getToken();
      if (!token) throw new Error("Sessão expirada. Faça login novamente.");
      const params = new URLSearchParams({
        start_date: startDate,
        end_date: endDate,
      });
      if ((isBuilder || isSuperAdmin) && viewingClinicId) params.set("tenant_id", viewingClinicId);
      const res = await fetch(`/v1/traces/stats?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.status === 401) throw new Error("Token inválido ou expirado. Faça login novamente.");
      if (!res.ok) throw new Error(`Stats fetch failed: ${res.status}`);
      const data = await res.json();
      // Map from API snake_case to the KpiStats shape
      return {
        total: data.total_traces ?? data.total ?? 0,
        avgProcessingTimeMs: data.avg_processing_time_ms ?? data.avgProcessingTimeMs ?? 0,
        safetyFlagCount: data.safety_flags_count ?? data.safetyFlagCount ?? 0,
        handoffCount: data.handoff_count ?? data.handoffCount ?? 0,
        totalTokensSum: data.total_tokens_sum ?? data.totalTokensSum ?? 0,
        modelBreakdown: data.model_breakdown ?? data.modelBreakdown ?? [],
        byIntent: data.intent_distribution ?? data.byIntent ?? {},
        outsideHoursCount: data.outside_hours_count ?? 0,
        outsideHoursPct: data.outside_hours_pct ?? null,
        conversationalTraceCount: data.conversational_trace_count ?? 0,
        businessHoursConfigured: data.business_hours_configured === true,
        newUsersCount: data.new_users_count ?? 0,
        returningUsersCount: data.returning_users_count ?? 0,
        newUserInteractions: data.new_user_interactions ?? 0,
        returningUserInteractions: data.returning_user_interactions ?? 0,
        newUserInteractionPct: data.new_user_interaction_pct ?? null,
        returningUserInteractionPct: data.returning_user_interaction_pct ?? null,
        identifiedUserInteractions: data.identified_user_interactions ?? 0,
        unidentifiedUserInteractions: data.unidentified_user_interactions ?? 0,
      } as KpiStats;
    },
    staleTime: 30_000,
  });

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-foreground">{t("dash_title")}</h1>
          <p className="text-muted-foreground mt-1">{t("dash_subtitle")}</p>
        </div>
        <DateRangePicker
          startDate={startDate} endDate={endDate}
          onStartChange={setStartDate} onEndChange={setEndDate}
        />
      </div>

      {isLoading && (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-6">
          {[1, 2, 3, 4, 5, 6].map(i => (
            <Card key={i}>
              <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                <Skeleton className="h-4 w-24" />
                <Skeleton className="h-4 w-4 rounded-full" />
              </CardHeader>
              <CardContent>
                <Skeleton className="h-8 w-16" />
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {error && !isLoading && (
        <div className="p-8 text-center text-destructive">
          <p>{t("dash_fail")}</p>
        </div>
      )}

      {stats && !isLoading && (
        <>
          <KpiCards stats={stats} t={t} locale={locale} />
          <UserAcquisitionMetrics stats={stats} locale={locale} />
          <IntentBreakdown
            byIntent={stats.byIntent} t={t} locale={locale}
            intentFilter={intentFilter} onIntentFilterChange={setIntentFilter}
          />
        </>
      )}
    </div>
  );
}
