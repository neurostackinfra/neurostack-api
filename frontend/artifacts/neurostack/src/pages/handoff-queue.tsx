import { useEffect, useState, useCallback, useMemo } from "react";
import { useAuth } from "@/context/auth";
import { useI18n } from "@/context/i18n";
import { useToast } from "@/hooks/use-toast";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { getBucketLabel, getIntentBucket, type IntentBucket } from "@/lib/intent-presentation";
import { Repeat, Gauge, Download, CalendarRange, MessageCircle, ShieldAlert } from "lucide-react";

interface HandoffQueueItem {
  id: string;
  numero_whatsapp: string | null;
  session_id: string;
  intencao_detectada: string | null;
  estagio_atual: string | null;
  primeira_linha_mensagem: string | null;
  created_at: string;
  data_hora_saida: string | null;
  status: string;
  safety_flag: boolean;
}

interface QueueStats {
  avg_wait_seconds: number;
  pct_within_sla: number;
}

// % goal target shown on the gauge (matches the design / clinic SLA target).
const HANDOFF_GOAL_PCT = 85;

const INTENT_BUCKETS: readonly IntentBucket[] = [
  "pedido_preco",
  "agendar",
  "interesse_tratamento",
  "reclamacao",
  "pos_atendimento_duvida",
  "outras",
];

function getToken(): string {
  try {
    return (JSON.parse(localStorage.getItem("ns_auth") ?? "{}") as { token?: string }).token ?? "";
  } catch {
    return "";
  }
}

/** Left-border accent class by waiting time (SLA tiers). */
function slaBorder(seconds: number): string {
  if (seconds < 300) return "border-l-emerald-500";
  if (seconds < 600) return "border-l-amber-500";
  return "border-l-rose-500";
}

function cleanNumber(s: string): string {
  return s.replace(/\D/g, "");
}

/** "Aguardando há" formatting → "25m 9s". */
function formatWait(s: number): string {
  const mins = Math.floor(s / 60);
  const secs = s % 60;
  return `${mins}m ${secs}s`;
}

/** Average handoff time as HH:MM:SS → "00:02:18". */
function formatHMS(totalSeconds: number): string {
  const s = Math.max(0, Math.round(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(h)}:${p(m)}:${p(sec)}`;
}

/** Short display code from a session id (UUID-safe). */
function shortCode(sessionId: string): string {
  return sessionId.replace(/-/g, "").slice(0, 9);
}

/** Local yyyy-mm-dd for a Date. */
function ymd(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// ── Investor demo: static mock handoff queue (mirrors the design mock) ──
// created_at is relative to mount so "Aguardando há" and the SLA colors are live.
function buildMockHandoffs(): HandoffQueueItem[] {
  const base = Date.now();
  const agoMin = (m: number, s = 0) => new Date(base - (m * 60 + s) * 1000).toISOString();
  return [
    {
      id: "mock-1",
      numero_whatsapp: "553197924877",
      session_id: "f843ae9f1",
      intencao_detectada: "agendar",
      estagio_atual: "agendamento_solicitado",
      primeira_linha_mensagem: null,
      created_at: agoMin(25, 9),
      data_hora_saida: null,
      status: "pending",
      safety_flag: true,
    },
    {
      id: "mock-2",
      numero_whatsapp: "551197334455",
      session_id: "ff2e3b8eec",
      intencao_detectada: "agendar",
      estagio_atual: "agendamento_solicitado",
      primeira_linha_mensagem: null,
      created_at: agoMin(11, 42),
      data_hora_saida: null,
      status: "pending",
      safety_flag: false,
    },
    {
      id: "mock-3",
      numero_whatsapp: "5511988776655",
      session_id: "9ec721f00",
      intencao_detectada: "interesse_tratamento",
      estagio_atual: "em_qualificacao",
      primeira_linha_mensagem: null,
      created_at: agoMin(8, 18),
      data_hora_saida: null,
      status: "pending",
      safety_flag: false,
    },
    {
      id: "mock-4",
      numero_whatsapp: "5511966443322",
      session_id: "83b4e6a1",
      intencao_detectada: "pedido_preco",
      estagio_atual: "em_qualificacao",
      primeira_linha_mensagem: null,
      created_at: agoMin(11, 3),
      data_hora_saida: null,
      status: "pending",
      safety_flag: true,
    },
    {
      id: "mock-5",
      numero_whatsapp: "5511982233445",
      session_id: "1bc2f9e7",
      intencao_detectada: "duvida_gerencia",
      estagio_atual: "em_qualificacao",
      primeira_linha_mensagem: null,
      created_at: agoMin(3, 27),
      data_hora_saida: null,
      status: "pending",
      safety_flag: false,
    },
    {
      id: "mock-6",
      numero_whatsapp: "5511995566778",
      session_id: "d3a3fffa7",
      intencao_detectada: "interesse_tratamento",
      estagio_atual: "agendamento_solicitado",
      primeira_linha_mensagem: null,
      created_at: agoMin(14, 58),
      data_hora_saida: null,
      status: "pending",
      safety_flag: true,
    },
  ];
}

// ── Semicircular gauge (goal achievement %) ──
const GAUGE = { cx: 100, cy: 105, r: 80 };
function polar(angleDeg: number, radius: number = GAUGE.r): { x: number; y: number } {
  const rad = (angleDeg * Math.PI) / 180;
  return { x: GAUGE.cx + radius * Math.cos(rad), y: GAUGE.cy - radius * Math.sin(rad) };
}

function SemiGauge({ value }: { value: number }) {
  const pct = Math.max(0, Math.min(100, value));
  const start = polar(180);
  const end = polar(0);
  const arcLen = Math.PI * GAUGE.r;
  const arcPath = `M ${start.x} ${start.y} A ${GAUGE.r} ${GAUGE.r} 0 0 1 ${end.x} ${end.y}`;
  // 25/50/75 hug the top of the arc; 0/100 sit at the bottom corners (all centered).
  const ticks: { label: string; x: number; y: number }[] = [
    { label: "0%", x: 14, y: 116 },
    { label: "25%", ...polar(135, GAUGE.r + 18) },
    { label: "50%", ...polar(90, GAUGE.r + 18) },
    { label: "75%", ...polar(45, GAUGE.r + 18) },
    { label: "100%", x: 186, y: 116 },
  ];

  return (
    <svg viewBox="0 0 200 132" className="w-full max-w-[280px]" role="img" aria-label={`${pct}%`}>
      <defs>
        <linearGradient id="handoffGaugeFill" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stopColor="#93c5fd" />
          <stop offset="100%" stopColor="#2563eb" />
        </linearGradient>
      </defs>
      {/* track */}
      <path d={arcPath} fill="none" stroke="currentColor" className="text-muted/40" strokeWidth={14} strokeLinecap="round" />
      {/* value */}
      <path
        d={arcPath}
        fill="none"
        stroke="url(#handoffGaugeFill)"
        strokeWidth={14}
        strokeLinecap="round"
        strokeDasharray={`${(pct / 100) * arcLen} ${arcLen}`}
      />
      {/* tick labels */}
      {ticks.map((tick) => (
        <text
          key={tick.label}
          x={tick.x}
          y={tick.y}
          textAnchor="middle"
          dominantBaseline="central"
          className="fill-muted-foreground text-[9px]"
        >
          {tick.label}
        </text>
      ))}
      {/* center value */}
      <text x={GAUGE.cx} y={94} textAnchor="middle" dominantBaseline="central" className="fill-foreground text-[28px] font-bold">
        {Math.round(pct)}%
      </text>
    </svg>
  );
}

export default function HandoffQueuePage() {
  const { tenantId, isBuilder, isSuperAdmin, isInvestor, viewingClinicId } = useAuth();
  const { t } = useI18n();
  const { toast } = useToast();

  const effectiveTenantId = (isBuilder || isSuperAdmin) ? viewingClinicId : tenantId;

  // Investor demo: stable mock data, generated once at mount.
  const [mockHandoffs] = useState<HandoffQueueItem[]>(() => buildMockHandoffs());

  const [handoffs, setHandoffs] = useState<HandoffQueueItem[]>([]);
  const [stats, setStats] = useState<QueueStats>({ avg_wait_seconds: 0, pct_within_sla: 0 });
  const [loading, setLoading] = useState(false);
  const [now, setNow] = useState<Date>(new Date());
  const [fadingIds, setFadingIds] = useState<Set<string>>(new Set());

  // Date range: defaults to today (drives the visible list + CSV export).
  const today = ymd(new Date());
  const [startDate, setStartDate] = useState<string>(today);
  const [endDate, setEndDate] = useState<string>(today);
  const [onlySafety, setOnlySafety] = useState(false);
  const [intentBucket, setIntentBucket] = useState<"all" | IntentBucket>("all");

  const fetchQueue = useCallback(async () => {
    if (!effectiveTenantId) return;
    const token = getToken();
    if (!token) return;
    try {
      setLoading(true);
      const params = new URLSearchParams({ tenant_id: effectiveTenantId });
      if (startDate) params.set("from_date", startDate);
      if (endDate) params.set("to_date", endDate);
      const res = await fetch(`/v1/handoffs/queue?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) return;
      const data = await res.json();
      setHandoffs(data.items ?? []);
      setStats({ avg_wait_seconds: data.avg_wait_seconds ?? 0, pct_within_sla: data.pct_within_sla ?? 0 });
    } catch {
      // silent
    } finally {
      setLoading(false);
    }
  }, [effectiveTenantId, startDate, endDate]);

  // 1-second global tick
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  // 30-second polling (investor sees static mock data, no API calls)
  useEffect(() => {
    if (isInvestor) {
      setHandoffs(mockHandoffs);
      setStats({ avg_wait_seconds: 138, pct_within_sla: 82 });
      return;
    }
    fetchQueue();
    const id = setInterval(fetchQueue, 30000);
    return () => clearInterval(id);
  }, [fetchQueue, isInvestor, mockHandoffs]);

  // Preserve the API's FIFO order: all card filters run in one pass, without sorting.
  const visibleHandoffs = useMemo(() => {
    return handoffs.filter((h) => {
      const day = ymd(new Date(h.created_at));
      if (day < startDate || day > endDate) return false;
      if (onlySafety && !h.safety_flag) return false;
      if (intentBucket === "all") return true;
      if (h.intencao_detectada === null) return false;
      return getIntentBucket(h.intencao_detectada) === intentBucket;
    });
  }, [handoffs, startDate, endDate, onlySafety, intentBucket]);

  const hasOperationalFilters = onlySafety || intentBucket !== "all";

  const clearOperationalFilters = () => {
    setOnlySafety(false);
    setIntentBucket("all");
  };

  const handleCheckout = async (id: string) => {
    // Investor demo: remove the card locally, no API call.
    if (isInvestor) {
      setFadingIds((prev) => new Set(prev).add(id));
      setTimeout(() => {
        setHandoffs((prev) => prev.filter((h) => h.id !== id));
        setFadingIds((prev) => { const n = new Set(prev); n.delete(id); return n; });
      }, 300);
      return;
    }
    const token = getToken();
    if (!token) return;
    try {
      const res = await fetch(`/v1/handoffs/queue/${id}/checkout`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.status === 409) {
        toast({ title: "Already attended", variant: "destructive" });
        return;
      }
      if (!res.ok) return;
      setFadingIds((prev) => new Set(prev).add(id));
      setTimeout(() => {
        setHandoffs((prev) => prev.filter((h) => h.id !== id));
        setFadingIds((prev) => { const n = new Set(prev); n.delete(id); return n; });
      }, 300);
    } catch {
      // silent
    }
  };

  const handleAtendido = (id: string) => handleCheckout(id);

  const handleAtenderDirecionar = (id: string, numero: string | null) => {
    if (numero) {
      window.open(`https://wa.me/${cleanNumber(numero)}`, "_blank");
    }
    handleCheckout(id);
  };

  const handleExport = async () => {
    if (!effectiveTenantId) return;
    const token = getToken();
    if (!token) return;
    try {
      const params = new URLSearchParams({ tenant_id: effectiveTenantId });
      if (startDate) params.set("from_date", startDate);
      if (endDate) params.set("to_date", endDate);
      const res = await fetch(`/v1/handoffs/queue/export?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) return;
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "handoffs.csv";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch {
      // silent
    }
  };

  if (!effectiveTenantId && !isInvestor) {
    return (
      <div className="flex items-center justify-center h-64">
        <p className="text-muted-foreground">{t("handoff_no_clinic")}</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <h1 className="text-2xl font-bold">{t("handoff_queue_title")}</h1>
          <p className="text-sm text-muted-foreground mt-1">{t("handoff_subtitle")}</p>
        </div>

        <div className="flex flex-wrap items-end gap-3">
          <div className="space-y-1">
            <label className="text-[11px] font-medium text-muted-foreground flex items-center gap-1">
              <CalendarRange className="h-3 w-3" /> {t("handoff_date_start")}
            </label>
            <input
              type="date"
              value={startDate}
              max={endDate}
              onChange={(e) => setStartDate(e.target.value)}
              className="rounded-md border border-border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
            />
          </div>
          <div className="space-y-1">
            <label className="text-[11px] font-medium text-muted-foreground flex items-center gap-1">
              <CalendarRange className="h-3 w-3" /> {t("handoff_date_end")}
            </label>
            <input
              type="date"
              value={endDate}
              min={startDate}
              max={today}
              onChange={(e) => setEndDate(e.target.value)}
              className="rounded-md border border-border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
            />
          </div>
          <Button variant="outline" size="sm" onClick={handleExport} className="gap-1.5 h-[34px]">
            <Download className="h-4 w-4" />
            {t("handoff_export_csv")}
          </Button>
        </div>
      </div>

      {/* Analytics: average time + goal gauge */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Average handoff time */}
        <div className="relative rounded-xl border bg-card p-5 shadow-sm text-center">
          <Repeat className="absolute left-5 top-5 h-5 w-5 text-primary" />
          <p className="text-base font-bold">Tempo médio de handoff</p>
          <p className="text-xs text-muted-foreground">Average Handoff Time</p>
          <div className="mx-auto mt-4 w-fit rounded-xl bg-muted/60 px-8 py-3">
            <span className="font-mono text-5xl font-bold tracking-wider tabular-nums">
              {formatHMS(stats.avg_wait_seconds)}
            </span>
          </div>
          <p className="text-xs text-muted-foreground mt-3">
            Tempo médio que o humano demora para atender
          </p>
        </div>

        {/* Goal achievement gauge */}
        <div className="relative rounded-xl border bg-card p-5 shadow-sm text-center">
          <Gauge className="absolute left-5 top-5 h-5 w-5 text-primary" />
          <p className="text-base font-bold">Meta de atendimento no handoff</p>
          <p className="text-xs text-muted-foreground">Handoff Goal Achievement</p>
          <div className="flex justify-center mt-1">
            <SemiGauge value={stats.pct_within_sla} />
          </div>
          <p className="text-sm font-semibold -mt-2">Meta: ≥ {HANDOFF_GOAL_PCT}%</p>
          <p className="text-xs text-muted-foreground mt-1">
            {t("handoff_goal_current")} {formatHMS(stats.avg_wait_seconds)}
          </p>
          <p className="text-xs text-muted-foreground mt-1">
            Porcentagem da meta do tempo de atendimento alcançado
          </p>
        </div>
      </div>

      {/* Color legend */}
      <div className="flex flex-wrap gap-4 text-xs">
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2.5 w-2.5 rounded-full bg-emerald-500" />
          {t("handoff_legend_green")}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2.5 w-2.5 rounded-full bg-amber-500" />
          {t("handoff_legend_yellow")}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2.5 w-2.5 rounded-full bg-rose-500" />
          {t("handoff_legend_red")}
        </span>
      </div>

      {/* Operational filters affect cards only; analytics and CSV keep their date scope. */}
      <div className="rounded-lg border bg-card px-4 py-3 space-y-2">
        <div className="flex flex-wrap items-end gap-4">
          <span className="self-center text-xs font-semibold text-muted-foreground uppercase tracking-wide">
            {t("handoff_filters_label")}
          </span>

          <label className="flex items-center gap-2 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={onlySafety}
              onChange={(event) => setOnlySafety(event.target.checked)}
              aria-describedby="handoff-filter-scope"
              className="h-4 w-4 rounded border-border accent-destructive cursor-pointer"
            />
            <span className="text-sm font-medium">{t("handoff_filter_safety")}</span>
          </label>

          <div className="space-y-1">
            <label htmlFor="handoff-intent-filter" className="block text-xs font-medium text-muted-foreground">
              {t("handoff_filter_intent")}
            </label>
            <select
              id="handoff-intent-filter"
              value={intentBucket}
              onChange={(event) => setIntentBucket(event.target.value as "all" | IntentBucket)}
              aria-describedby="handoff-filter-scope"
              className="h-8 rounded-md border border-border bg-background px-2 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring cursor-pointer"
            >
              <option value="all">{t("handoff_filter_all_intents")}</option>
              {INTENT_BUCKETS.map((bucket) => (
                <option key={bucket} value={bucket}>{getBucketLabel(bucket, t)}</option>
              ))}
            </select>
          </div>

          <Button type="button" variant="outline" size="sm" onClick={clearOperationalFilters}>
            {t("handoff_clear_filters")}
          </Button>
        </div>
        <p id="handoff-filter-scope" className="text-xs text-muted-foreground">
          {t("handoff_filters_cards_only")}
        </p>
      </div>

      {/* Queue list */}
      {visibleHandoffs.length === 0 && !loading && (
        <p className="text-center text-muted-foreground py-12">
          {hasOperationalFilters ? t("handoff_filtered_empty") : t("handoff_empty")}
        </p>
      )}

      <div className="space-y-3">
        {visibleHandoffs.map((item) => {
          const secondsWaiting = Math.max(0, Math.floor((now.getTime() - new Date(item.created_at).getTime()) / 1000));
          const isFading = fadingIds.has(item.id);

          return (
            <div
              key={item.id}
              data-testid="handoff-card"
              data-handoff-id={item.id}
              className={`rounded-xl border border-l-4 bg-card p-4 shadow-sm transition-all duration-300 ${slaBorder(secondsWaiting)} ${isFading ? "opacity-0" : "opacity-100"}`}
            >
              <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
                {/* Number + code */}
                <div className="min-w-[10rem]">
                  <p className="text-lg font-bold tracking-wide">{item.numero_whatsapp || "—"}</p>
                  <div className="flex items-center gap-2 mt-0.5">
                    <span className="text-xs text-muted-foreground">
                      {t("handoff_session_code")}: {shortCode(item.session_id)}
                    </span>
                    <span className="text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded bg-muted text-muted-foreground">
                      {item.status}
                    </span>
                  </div>
                </div>

                {/* Intent */}
                <div className="min-w-[11rem] text-sm space-y-0.5">
                  <p><span className="text-muted-foreground">{t("handoff_intent")}:</span> {item.intencao_detectada ?? "—"}</p>
                  {item.safety_flag && (
                    <Badge variant="destructive" className="gap-1" aria-label={t("handoff_safety_marker")}>
                      <ShieldAlert className="h-3 w-3" aria-hidden="true" />
                      {t("handoff_safety_marker")}
                    </Badge>
                  )}
                </div>

                {/* Entry + waiting */}
                <div className="min-w-[12rem] text-sm space-y-0.5">
                  <p>
                    <span className="text-muted-foreground">{t("handoff_entry_time")}:</span>{" "}
                    {new Date(item.created_at).toLocaleString("pt-BR")}
                  </p>
                  <p>
                    <span className="text-muted-foreground">{t("handoff_waiting_since")}</span>{" "}
                    <span className="font-mono">{formatWait(secondsWaiting)}</span>
                  </p>
                </div>

                {/* Spacer pushes the actions to the right (no message/name shown for privacy) */}
                <div className="flex-1 min-w-[2rem]" />

                {/* Actions */}
                <div className="flex flex-wrap gap-2 ml-auto shrink-0">
                  <Button
                    size="sm"
                    onClick={() => handleAtendido(item.id)}
                    className="bg-slate-900 text-white hover:bg-slate-800 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white border-transparent"
                  >
                    {t("handoff_btn_attended")}
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => handleAtenderDirecionar(item.id, item.numero_whatsapp)}
                  >
                    <MessageCircle className="h-4 w-4 mr-1" />
                    {t("handoff_btn_attend_redirect")}
                  </Button>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
