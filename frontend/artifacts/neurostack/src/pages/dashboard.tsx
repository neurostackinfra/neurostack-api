import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/context/auth";
import { useI18n } from "@/context/i18n";
import type { TranslationKey } from "@/locales/en";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { AlertTriangle, Clock, ListChecks, ActivitySquare, Zap, CalendarRange } from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { PieChart, Pie, Cell, Tooltip, ResponsiveContainer } from "recharts";
import { format } from "date-fns";

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

// -- Intent metadata ---

const INTENT_META: Record<string, { bar: string; dot: string; hex: string }> = {
  // Greeting
  saudacao:                              { bar: "bg-sky-500",     dot: "bg-sky-500",     hex: "#0ea5e9" },
  // Interest & Qualification
  interesse_tratamento:                  { bar: "bg-violet-500",  dot: "bg-violet-500",  hex: "#8b5cf6" },
  qualificacao_publico:                  { bar: "bg-purple-500",  dot: "bg-purple-500",  hex: "#a855f7" },
  comparacao_tratamentos:                { bar: "bg-fuchsia-500", dot: "bg-fuchsia-500", hex: "#d946ef" },
  // Price & Objections
  pedido_preco:                          { bar: "bg-blue-500",    dot: "bg-blue-500",    hex: "#3b82f6" },
  objecao_vou_pensar:                    { bar: "bg-amber-500",   dot: "bg-amber-500",   hex: "#f59e0b" },
  // Scheduling
  agendar:                               { bar: "bg-green-500",   dot: "bg-green-500",   hex: "#22c55e" },
  reagendar:                             { bar: "bg-emerald-500", dot: "bg-emerald-500", hex: "#10b981" },
  cancelar:                              { bar: "bg-orange-500",  dot: "bg-orange-500",  hex: "#f97316" },
  confirmar_presenca:                    { bar: "bg-teal-500",    dot: "bg-teal-500",    hex: "#14b8a6" },
  disponibilidade_horarios:              { bar: "bg-cyan-500",    dot: "bg-cyan-500",    hex: "#06b6d4" },
  // Info & FAQ
  detalhes_servicos:                     { bar: "bg-indigo-400",  dot: "bg-indigo-400",  hex: "#818cf8" },
  informacoes_profissionais:             { bar: "bg-indigo-500",  dot: "bg-indigo-500",  hex: "#6366f1" },
  faq:                                   { bar: "bg-slate-500",   dot: "bg-slate-500",   hex: "#64748b" },
  cultura_historia:                      { bar: "bg-slate-400",   dot: "bg-slate-400",   hex: "#94a3b8" },
  duvida_generica_sem_contexto:          { bar: "bg-gray-500",    dot: "bg-gray-500",    hex: "#6b7280" },
  pre_atendimento:                       { bar: "bg-lime-500",    dot: "bg-lime-500",    hex: "#84cc16" },
  // Post-care
  pos_atendimento_duvida:                { bar: "bg-indigo-500",  dot: "bg-indigo-500",  hex: "#6366f1" },
  pos_atendimento_sintomas_preocupantes: { bar: "bg-red-500",     dot: "bg-red-500",     hex: "#ef4444" },
  pedido_alterar_medicacao:              { bar: "bg-red-600",     dot: "bg-red-600",     hex: "#dc2626" },
  // Admin / Financial
  documentos_e_comprovantes:             { bar: "bg-zinc-500",    dot: "bg-zinc-500",    hex: "#71717a" },
  orcamento_existente:                   { bar: "bg-blue-400",    dot: "bg-blue-400",    hex: "#60a5fa" },
  cobranca_renegociacao:                 { bar: "bg-yellow-500",  dot: "bg-yellow-500",  hex: "#eab308" },
  // Escalation
  reclamacao:                            { bar: "bg-rose-500",    dot: "bg-rose-500",    hex: "#f43f5e" },
  conduta_inadequada_ameaca:             { bar: "bg-red-700",     dot: "bg-red-700",     hex: "#b91c1c" },
};

function getIntentLabel(key: string, t: (k: TranslationKey) => string): string {
  const tk = `intent_${key}` as TranslationKey;
  return t(tk) || key;
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

// -- Date Range Picker ---

function DateRangePicker({
  startDate, endDate,
  onStartChange, onEndChange,
}: {
  startDate: string; endDate: string;
  onStartChange: (v: string) => void; onEndChange: (v: string) => void;
}) {
  const today = format(new Date(), "yyyy-MM-dd");
  return (
    <div className="flex flex-col sm:flex-row items-end gap-2 shrink-0">
      <div className="space-y-1">
        <label className="text-[11px] font-medium text-muted-foreground flex items-center gap-1">
          <CalendarRange className="h-3 w-3" /> Data Inicial
        </label>
        <input
          type="date"
          value={startDate}
          max={endDate}
          onChange={e => onStartChange(e.target.value)}
          className="rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-accent h-9 w-36"
        />
      </div>
      <div className="space-y-1">
        <label className="text-[11px] font-medium text-muted-foreground flex items-center gap-1">
          <CalendarRange className="h-3 w-3" /> Data Final
        </label>
        <input
          type="date"
          value={endDate}
          min={startDate}
          max={today}
          onChange={e => onEndChange(e.target.value)}
          className="rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-accent h-9 w-36"
        />
      </div>
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
}

// -- Technical KPI cards ---

function KpiCards({ stats, t }: { stats: KpiStats; t: (k: TranslationKey) => string }) {
  const maxModelTokens = stats.modelBreakdown[0]?.tokens ?? 0;

  return (
    <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-5">
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

const INVESTOR_INTENTS = ["pedido_preco", "agendar", "interesse_tratamento", "reclamacao", "pos_atendimento_duvida"];

function IntentBreakdown({
  byIntent, t, intentFilter, onIntentFilterChange, isInvestor,
}: {
  byIntent: Record<string, number>;
  t: (k: TranslationKey) => string;
  intentFilter: string;
  onIntentFilterChange: (v: string) => void;
  isInvestor?: boolean;
}) {
  const baseIntent = isInvestor
    ? Object.fromEntries(Object.entries(byIntent).filter(([k]) => INVESTOR_INTENTS.includes(k)))
    : byIntent;
  const visibleIntent = Object.fromEntries(
    Object.entries(baseIntent).filter(([k]) => !intentFilter || k === intentFilter)
  );
  const intentTotal = Object.values(visibleIntent).reduce((s, n) => s + n, 0);
  const chartData = Object.entries(visibleIntent)
    .filter(([, count]) => count > 0)
    .map(([intent, count]) => ({
      key: intent,
      name: getIntentLabel(intent, t),
      value: count,
      hex: INTENT_META[intent]?.hex ?? "#64748b",
      pct: intentTotal > 0 ? (count / intentTotal) * 100 : 0,
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
            {(isInvestor ? INVESTOR_INTENTS : Object.keys(INTENT_META)).map(key => (
              <option key={key} value={key}>{getIntentLabel(key, t)}</option>
            ))}
          </select>
        </div>
      </CardHeader>
      <CardContent>
        <div className="grid gap-8 sm:grid-cols-2 items-center">
          <div className="space-y-3.5">
            {Object.entries(visibleIntent).map(([intent, count]) => {
              const pct = intentTotal > 0 ? (count / intentTotal) * 100 : 0;
              const m = INTENT_META[intent] ?? { bar: "bg-accent", dot: "bg-accent" };
              return (
                <div key={intent} className="space-y-1" data-testid={`stat-intent-${intent}`}>
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-1.5 min-w-0">
                      <span className={`h-2 w-2 rounded-full shrink-0 ${m.dot}`} />
                      <span className="text-xs font-medium text-foreground truncate">{getIntentLabel(intent, t)}</span>
                    </div>
                    <span className="text-xs font-bold text-foreground shrink-0 tabular-nums">{count}</span>
                  </div>
                  <div className="h-1.5 w-full rounded-full bg-muted overflow-hidden">
                    <div className={`h-full ${m.bar} rounded-full transition-all duration-500`} style={{ width: `${pct}%` }} />
                  </div>
                </div>
              );
            })}
            {intentTotal === 0 && (
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
                <span className="text-2xl font-bold text-foreground tabular-nums leading-none">{intentTotal}</span>
                <span className="text-[10px] text-muted-foreground uppercase tracking-wider font-medium mt-1">
                  {intentFilter ? getIntentLabel(intentFilter, t).split(" ")[0] : t("dash_intent_total")}
                </span>
              </div>
            </div>
            <div className="flex flex-wrap justify-center gap-x-3 gap-y-1.5">
              {Object.entries(INTENT_META)
                .filter(([key]) => (!isInvestor || INVESTOR_INTENTS.includes(key)) && (!intentFilter || key === intentFilter))
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

// -- Main Dashboard ---

export default function Dashboard() {
  const { t } = useI18n();
  const { isInvestor } = useAuth();
  const { startDate: defStart, endDate: defEnd } = defaultDates();
  const [startDate, setStartDate] = useState(defStart);
  const [endDate, setEndDate] = useState(defEnd);
  const [intentFilter, setIntentFilter] = useState("");

  const { data: stats, isLoading, error } = useQuery<KpiStats>({
    queryKey: ["trace-stats", startDate, endDate],
    queryFn: async () => {
      const token = getToken();
      if (!token) throw new Error("Sessão expirada. Faça login novamente.");
      const params = new URLSearchParams({
        start_date: startDate,
        end_date: endDate,
      });
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
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-5">
          {[1, 2, 3, 4, 5].map(i => (
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
          <KpiCards stats={stats} t={t} />
          <IntentBreakdown
            byIntent={stats.byIntent} t={t}
            intentFilter={intentFilter} onIntentFilterChange={setIntentFilter}
            isInvestor={isInvestor}
          />
        </>
      )}
    </div>
  );
}
