import { useEffect, useState, useCallback, useMemo, useRef } from "react";
import { useAuth } from "@/context/auth";
import { useI18n } from "@/context/i18n";
import { useToast } from "@/hooks/use-toast";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  getBucketLabel,
  getIntentBucket,
  type IntentBucket,
} from "@/lib/intent-presentation";
import {
  Repeat,
  Gauge,
  Download,
  CalendarRange,
  MessageCircle,
  ShieldAlert,
} from "lucide-react";

interface HandoffQueueItem {
  id: string;
  numero_whatsapp: string | null;
  session_id: string;
  intencao_detectada: string | null;
  estagio_atual: string | null;
  primeira_linha_mensagem: string | null;
  created_at: string;
  data_hora_saida: string | null;
  sla_target_seconds: number | null;
  status: string;
  safety_flag: boolean;
}

interface QueueStats {
  avg_wait_seconds: number | null;
  pct_within_sla: number | null;
  within_sla_count: number;
  confirmed_late_count: number;
  unanswered_overdue_count: number;
  maturing_count: number;
  cancelled_unconfirmed_count: number;
  unmeasured_legacy_count: number;
  confirmed_count: number;
  sla_eligible_count: number;
  period_timezone: string;
  period_start: string;
  period_end_exclusive: string;
}

interface QueueResponse extends QueueStats {
  items: HandoffQueueItem[];
}

interface DateScope {
  tenantId: string | null;
  startDate: string;
  endDate: string;
  explicit: boolean;
}

const EMPTY_STATS: QueueStats = {
  avg_wait_seconds: null,
  pct_within_sla: null,
  within_sla_count: 0,
  confirmed_late_count: 0,
  unanswered_overdue_count: 0,
  maturing_count: 0,
  cancelled_unconfirmed_count: 0,
  unmeasured_legacy_count: 0,
  confirmed_count: 0,
  sla_eligible_count: 0,
  period_timezone: "",
  period_start: "",
  period_end_exclusive: "",
};

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
    return (
      (
        JSON.parse(localStorage.getItem("ns_auth") ?? "{}") as {
          token?: string;
        }
      ).token ?? ""
    );
  } catch {
    return "";
  }
}

type SlaVisualState = "green" | "yellow" | "red" | "neutral";

function slaVisualState(
  seconds: number,
  targetSeconds: number | null,
): SlaVisualState {
  if (targetSeconds === null || targetSeconds <= 0) return "neutral";
  const ratio = seconds / targetSeconds;
  if (ratio < 0.5) return "green";
  if (ratio <= 1) return "yellow";
  return "red";
}

const SLA_BORDER: Record<SlaVisualState, string> = {
  green: "border-l-emerald-500",
  yellow: "border-l-amber-500",
  red: "border-l-rose-500",
  neutral: "border-l-slate-300 dark:border-l-slate-600",
};

function interpolate(
  template: string,
  values: Record<string, string | number>,
): string {
  return Object.entries(values).reduce(
    (text, [key, value]) => text.replace(`{${key}}`, String(value)),
    template,
  );
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

function formatPeriod(
  start: string,
  endExclusive: string,
  timezoneName: string,
  locale: "pt" | "en",
): string {
  if (!start || !endExclusive || !timezoneName) return "";
  try {
    const endInclusive = new Date(new Date(endExclusive).getTime() - 1);
    const formatter = new Intl.DateTimeFormat(
      locale === "pt" ? "pt-BR" : "en-US",
      {
        dateStyle: "medium",
        timeZone: timezoneName,
      },
    );
    return `${formatter.format(new Date(start))} – ${formatter.format(endInclusive)} (${timezoneName})`;
  } catch {
    return "";
  }
}

function civilDate(
  value: string | number,
  timezoneName: string,
): string | null {
  try {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return null;
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: timezoneName,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(date);
    const part = (type: "year" | "month" | "day") =>
      parts.find((item) => item.type === type)?.value;
    const year = part("year");
    const month = part("month");
    const day = part("day");
    return year && month && day ? `${year}-${month}-${day}` : null;
  } catch {
    return null;
  }
}

function periodDates(
  stats: QueueStats,
): { startDate: string; endDate: string } | null {
  const endExclusive = new Date(stats.period_end_exclusive).getTime();
  if (!Number.isFinite(endExclusive)) return null;
  const startDate = civilDate(stats.period_start, stats.period_timezone);
  const endDate = civilDate(endExclusive - 1, stats.period_timezone);
  return startDate && endDate ? { startDate, endDate } : null;
}

function safeTimeZone(timezoneName: string): string | undefined {
  if (!timezoneName) return undefined;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: timezoneName }).format(0);
    return timezoneName;
  } catch {
    return undefined;
  }
}

/** Short display code from a session id (UUID-safe). */
function shortCode(sessionId: string): string {
  return sessionId.replace(/-/g, "").slice(0, 9);
}

// ── Investor demo: static mock handoff queue (mirrors the design mock) ──
// created_at is relative to mount so "Aguardando há" and the SLA colors are live.
function buildMockHandoffs(): HandoffQueueItem[] {
  const base = Date.now();
  const agoMin = (m: number, s = 0) =>
    new Date(base - (m * 60 + s) * 1000).toISOString();
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
      sla_target_seconds: 600,
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
      sla_target_seconds: 600,
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
      sla_target_seconds: 600,
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
      sla_target_seconds: 600,
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
      sla_target_seconds: 600,
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
      sla_target_seconds: 600,
      status: "pending",
      safety_flag: true,
    },
  ];
}

// ── Semicircular gauge (goal achievement %) ──
const GAUGE = { cx: 100, cy: 105, r: 80 };
function polar(
  angleDeg: number,
  radius: number = GAUGE.r,
): { x: number; y: number } {
  const rad = (angleDeg * Math.PI) / 180;
  return {
    x: GAUGE.cx + radius * Math.cos(rad),
    y: GAUGE.cy - radius * Math.sin(rad),
  };
}

function SemiGauge({
  value,
  accessibleLabel,
}: {
  value: number;
  accessibleLabel: string;
}) {
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
    <svg
      viewBox="0 0 200 132"
      className="w-full max-w-[280px]"
      role="img"
      aria-label={accessibleLabel}
    >
      <defs>
        <linearGradient id="handoffGaugeFill" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stopColor="#93c5fd" />
          <stop offset="100%" stopColor="#2563eb" />
        </linearGradient>
      </defs>
      {/* track */}
      <path
        d={arcPath}
        fill="none"
        stroke="currentColor"
        className="text-muted/40"
        strokeWidth={14}
        strokeLinecap="round"
      />
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
      <text
        x={GAUGE.cx}
        y={94}
        textAnchor="middle"
        dominantBaseline="central"
        className="fill-foreground text-[28px] font-bold"
      >
        {Math.round(pct)}%
      </text>
    </svg>
  );
}

export default function HandoffQueuePage() {
  const { tenantId, isBuilder, isSuperAdmin, isInvestor, viewingClinicId } =
    useAuth();
  const { t, locale } = useI18n();
  const { toast } = useToast();

  const effectiveTenantId =
    isBuilder || isSuperAdmin ? viewingClinicId : tenantId;
  const effectiveTenantRef = useRef(effectiveTenantId);
  effectiveTenantRef.current = effectiveTenantId;

  // Investor demo: stable mock data, generated once at mount.
  const [mockHandoffs] = useState<HandoffQueueItem[]>(() =>
    buildMockHandoffs(),
  );

  const [handoffs, setHandoffs] = useState<HandoffQueueItem[]>([]);
  const [stats, setStats] = useState<QueueStats>(EMPTY_STATS);
  const [loading, setLoading] = useState(false);
  const [now, setNow] = useState<Date>(new Date());
  const [confirmingIds, setConfirmingIds] = useState<Set<string>>(new Set());
  const confirmingKeysRef = useRef<Set<string>>(new Set());
  const fetchSequenceRef = useRef(0);
  const fetchQueueRef = useRef<() => Promise<void>>(async () => undefined);
  const loadedScopeRef = useRef<string | null>(null);
  const [loadedScope, setLoadedScope] = useState<string | null>(null);

  // The backend resolves the clinic-local default period; browser dates never
  // participate until the user explicitly changes an input.
  const [dateScope, setDateScope] = useState<DateScope>(() => ({
    tenantId: effectiveTenantId,
    startDate: "",
    endDate: "",
    explicit: false,
  }));
  const dateScopeMatchesTenant = dateScope.tenantId === effectiveTenantId;
  const startDate = dateScopeMatchesTenant ? dateScope.startDate : "";
  const endDate = dateScopeMatchesTenant ? dateScope.endDate : "";
  const requestedStartDate =
    dateScopeMatchesTenant && dateScope.explicit ? startDate : "";
  const requestedEndDate =
    dateScopeMatchesTenant && dateScope.explicit ? endDate : "";
  const [onlySafety, setOnlySafety] = useState(false);
  const [intentBucket, setIntentBucket] = useState<"all" | IntentBucket>("all");
  const requestScope = `${effectiveTenantId ?? ""}|${requestedStartDate}|${requestedEndDate}`;
  const requestScopeRef = useRef(requestScope);
  requestScopeRef.current = requestScope;
  const exportGenerationRef = useRef(0);

  useEffect(() => {
    exportGenerationRef.current += 1;
    return () => {
      exportGenerationRef.current += 1;
    };
  }, [requestScope]);

  const fetchQueue = useCallback(async () => {
    if (!effectiveTenantId) return;
    const token = getToken();
    if (!token) return;
    const requestId = ++fetchSequenceRef.current;
    const requestedScope = `${effectiveTenantId}|${requestedStartDate}|${requestedEndDate}`;
    try {
      setLoading(true);
      if (loadedScopeRef.current !== requestedScope) {
        loadedScopeRef.current = null;
        setLoadedScope(null);
        setHandoffs([]);
        setStats(EMPTY_STATS);
      }
      const params = new URLSearchParams({ tenant_id: effectiveTenantId });
      if (requestedStartDate) params.set("from_date", requestedStartDate);
      if (requestedEndDate) params.set("to_date", requestedEndDate);
      const res = await fetch(`/v1/handoffs/queue?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) return;
      const data = (await res.json()) as QueueResponse;
      if (
        requestId !== fetchSequenceRef.current ||
        requestedScope !== requestScopeRef.current
      )
        return;
      const { items, ...nextStats } = data;
      setHandoffs(items);
      setStats(nextStats);
      loadedScopeRef.current = requestedScope;
      setLoadedScope(requestedScope);
      const resolvedDates = periodDates(nextStats);
      if (resolvedDates) {
        setDateScope((current) => {
          if (current.tenantId !== effectiveTenantId || current.explicit)
            return current;
          return {
            tenantId: effectiveTenantId,
            ...resolvedDates,
            explicit: false,
          };
        });
      }
    } catch {
      // silent
    } finally {
      if (
        requestId === fetchSequenceRef.current &&
        requestedScope === requestScopeRef.current
      ) {
        setLoading(false);
      }
    }
  }, [effectiveTenantId, requestedStartDate, requestedEndDate]);
  fetchQueueRef.current = fetchQueue;

  // 1-second global tick
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    fetchSequenceRef.current += 1;
    setHandoffs([]);
    setStats(EMPTY_STATS);
    loadedScopeRef.current = null;
    setLoadedScope(null);
    setLoading(false);
    setDateScope({
      tenantId: effectiveTenantId,
      startDate: "",
      endDate: "",
      explicit: false,
    });
  }, [effectiveTenantId]);

  useEffect(() => {
    const confirmationPrefix = `${effectiveTenantId ?? "investor"}:`;
    setConfirmingIds(
      new Set(
        [...confirmingKeysRef.current]
          .filter((key) => key.startsWith(confirmationPrefix))
          .map((key) => key.slice(confirmationPrefix.length)),
      ),
    );
  }, [effectiveTenantId]);

  // 30-second polling (investor sees static mock data, no API calls)
  useEffect(() => {
    if (isInvestor) {
      setHandoffs(mockHandoffs);
      setStats({
        avg_wait_seconds: 138,
        pct_within_sla: 80,
        within_sla_count: 4,
        confirmed_late_count: 1,
        unanswered_overdue_count: 0,
        maturing_count: 1,
        cancelled_unconfirmed_count: 0,
        unmeasured_legacy_count: 0,
        confirmed_count: 5,
        sla_eligible_count: 5,
        period_timezone: "",
        period_start: "",
        period_end_exclusive: "",
      });
      return;
    }
    fetchQueue();
    const id = setInterval(fetchQueue, 30000);
    return () => clearInterval(id);
  }, [fetchQueue, isInvestor, mockHandoffs]);

  // Preserve the API's FIFO order: all card filters run in one pass, without sorting.
  const visibleHandoffs = useMemo(() => {
    return handoffs.filter((h) => {
      if (onlySafety && !h.safety_flag) return false;
      if (intentBucket === "all") return true;
      if (h.intencao_detectada === null) return false;
      return getIntentBucket(h.intencao_detectada) === intentBucket;
    });
  }, [handoffs, onlySafety, intentBucket]);

  const hasOperationalFilters = onlySafety || intentBucket !== "all";

  const clearOperationalFilters = () => {
    setOnlySafety(false);
    setIntentBucket("all");
  };

  const handleConfirm = async (id: string) => {
    const confirmTenant = effectiveTenantId;
    const confirmScope = requestScopeRef.current;
    const confirmationKey = `${confirmTenant ?? "investor"}:${id}`;
    if (confirmingKeysRef.current.has(confirmationKey)) return;
    const token = getToken();
    if (!isInvestor && (!confirmTenant || !token)) return;

    confirmingKeysRef.current.add(confirmationKey);
    setConfirmingIds((prev) => new Set(prev).add(id));
    try {
      if (isInvestor) {
        setHandoffs((prev) => prev.filter((handoff) => handoff.id !== id));
        return;
      }
      const params = new URLSearchParams({ tenant_id: confirmTenant! });
      const res = await fetch(
        `/v1/handoffs/queue/${id}/confirm?${params.toString()}`,
        {
          method: "POST",
          headers: { Authorization: `Bearer ${token}` },
        },
      );
      if (res.status === 409) {
        if (
          effectiveTenantRef.current === confirmTenant &&
          requestScopeRef.current === confirmScope
        ) {
          toast({
            title: t("handoff_confirm_conflict"),
            variant: "destructive",
          });
        }
        return;
      }
      if (!res.ok) {
        if (
          effectiveTenantRef.current === confirmTenant &&
          requestScopeRef.current === confirmScope
        ) {
          toast({ title: t("handoff_confirm_error"), variant: "destructive" });
        }
        return;
      }
      const tenantStillActive = effectiveTenantRef.current === confirmTenant;
      if (tenantStillActive && requestScopeRef.current === confirmScope) {
        setHandoffs((prev) => prev.filter((handoff) => handoff.id !== id));
      }
      if (tenantStillActive) await fetchQueueRef.current();
    } catch {
      if (
        effectiveTenantRef.current === confirmTenant &&
        requestScopeRef.current === confirmScope
      ) {
        toast({ title: t("handoff_confirm_error"), variant: "destructive" });
      }
    } finally {
      confirmingKeysRef.current.delete(confirmationKey);
      if (effectiveTenantRef.current === confirmTenant) {
        setConfirmingIds((prev) => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
      }
    }
  };

  const handleOpenWhatsApp = (numero: string | null) => {
    if (numero) {
      window.open(`https://wa.me/${cleanNumber(numero)}`, "_blank");
    }
  };

  const handleExport = async () => {
    const exportTenant = effectiveTenantId;
    const exportScope = requestScopeRef.current;
    const exportGeneration = exportGenerationRef.current;
    const exportIsCurrent = () =>
      exportGenerationRef.current === exportGeneration &&
      effectiveTenantRef.current === exportTenant &&
      requestScopeRef.current === exportScope &&
      loadedScopeRef.current === exportScope;
    if (!exportTenant || !exportIsCurrent()) return;
    const token = getToken();
    if (!token) return;
    try {
      const params = new URLSearchParams({ tenant_id: exportTenant });
      if (startDate) params.set("from_date", startDate);
      if (endDate) params.set("to_date", endDate);
      const res = await fetch(
        `/v1/handoffs/queue/export?${params.toString()}`,
        {
          headers: { Authorization: `Bearer ${token}` },
        },
      );
      if (!exportIsCurrent()) return;
      if (!res.ok) {
        let detail: unknown;
        try {
          const error = await res.json();
          detail = error?.detail;
        } catch {
          detail = undefined;
        }
        if (!exportIsCurrent()) return;
        if (detail === "export_limit_10000_exceeded") {
          toast({
            title: t("handoff_export_limit_error"),
            variant: "destructive",
          });
        }
        return;
      }
      const blob = await res.blob();
      if (!exportIsCurrent()) return;
      const url = URL.createObjectURL(blob);
      if (!exportIsCurrent()) {
        URL.revokeObjectURL(url);
        return;
      }
      const a = document.createElement("a");
      a.href = url;
      a.download = "handoffs.csv";
      if (!exportIsCurrent()) {
        URL.revokeObjectURL(url);
        return;
      }
      document.body.appendChild(a);
      if (!exportIsCurrent()) {
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        return;
      }
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch {
      // silent
    }
  };

  const periodLabel = formatPeriod(
    stats.period_start,
    stats.period_end_exclusive,
    stats.period_timezone,
    locale,
  );
  const slaPercentageLabel =
    stats.pct_within_sla === null
      ? t("handoff_no_data")
      : `${new Intl.NumberFormat(locale === "pt" ? "pt-BR" : "en-US", {
          maximumFractionDigits: 1,
        }).format(stats.pct_within_sla)}%`;
  const displayTimeZone = safeTimeZone(stats.period_timezone);
  const canExport = loadedScope === requestScope;

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
          <p className="text-sm text-muted-foreground mt-1">
            {t("handoff_subtitle")}
          </p>
          {periodLabel && (
            <p
              data-testid="handoff-period"
              className="text-xs text-muted-foreground mt-1"
            >
              {t("handoff_period")}: {periodLabel}
            </p>
          )}
        </div>

        <div className="flex flex-wrap items-end gap-3">
          <div className="space-y-1">
            <label className="text-[11px] font-medium text-muted-foreground flex items-center gap-1">
              <CalendarRange className="h-3 w-3" /> {t("handoff_date_start")}
            </label>
            <input
              type="date"
              value={startDate}
              max={endDate || undefined}
              onChange={(event) =>
                setDateScope({
                  tenantId: effectiveTenantId,
                  startDate: event.target.value,
                  endDate,
                  explicit: true,
                })
              }
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
              min={startDate || undefined}
              onChange={(event) =>
                setDateScope({
                  tenantId: effectiveTenantId,
                  startDate,
                  endDate: event.target.value,
                  explicit: true,
                })
              }
              className="rounded-md border border-border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
            />
          </div>
          <Button
            variant="outline"
            size="sm"
            disabled={!canExport}
            onClick={handleExport}
            className="gap-1.5 h-[34px]"
          >
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
          <p className="text-base font-bold">{t("handoff_avg_wait")}</p>
          <div className="mx-auto mt-4 w-fit rounded-xl bg-muted/60 px-8 py-3">
            <span className="font-mono text-5xl font-bold tracking-wider tabular-nums">
              {stats.avg_wait_seconds === null
                ? t("handoff_no_data")
                : formatHMS(stats.avg_wait_seconds)}
            </span>
          </div>
          <p className="text-xs text-muted-foreground mt-3">
            {t("handoff_avg_description")}
          </p>
        </div>

        {/* Goal achievement gauge */}
        <div className="relative rounded-xl border bg-card p-5 shadow-sm text-center">
          <Gauge className="absolute left-5 top-5 h-5 w-5 text-primary" />
          <p className="text-base font-bold">{t("handoff_pct_sla")}</p>
          <div className="flex justify-center mt-1">
            {stats.pct_within_sla === null ? (
              <div
                role="img"
                aria-label={t("handoff_no_data")}
                className="flex h-[132px] w-full max-w-[280px] items-center justify-center text-4xl font-bold text-muted-foreground"
              >
                —
              </div>
            ) : (
              <SemiGauge
                value={stats.pct_within_sla}
                accessibleLabel={slaPercentageLabel}
              />
            )}
          </div>
          <p className="text-sm font-semibold -mt-2">
            {interpolate(t("handoff_sla_context"), {
              within: stats.within_sla_count,
              eligible: stats.sla_eligible_count,
            })}
          </p>
          <p className="text-xs text-muted-foreground mt-1">
            {t("handoff_sla_description")}
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
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2.5 w-2.5 rounded-full bg-slate-300 dark:bg-slate-600" />
          {t("handoff_legend_neutral")}
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
            <span className="text-sm font-medium">
              {t("handoff_filter_safety")}
            </span>
          </label>

          <div className="space-y-1">
            <label
              htmlFor="handoff-intent-filter"
              className="block text-xs font-medium text-muted-foreground"
            >
              {t("handoff_filter_intent")}
            </label>
            <select
              id="handoff-intent-filter"
              value={intentBucket}
              onChange={(event) =>
                setIntentBucket(event.target.value as "all" | IntentBucket)
              }
              aria-describedby="handoff-filter-scope"
              className="h-8 rounded-md border border-border bg-background px-2 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring cursor-pointer"
            >
              <option value="all">{t("handoff_filter_all_intents")}</option>
              {INTENT_BUCKETS.map((bucket) => (
                <option key={bucket} value={bucket}>
                  {getBucketLabel(bucket, t)}
                </option>
              ))}
            </select>
          </div>

          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={clearOperationalFilters}
          >
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
          {hasOperationalFilters
            ? t("handoff_filtered_empty")
            : t("handoff_empty")}
        </p>
      )}

      <div className="space-y-3">
        {visibleHandoffs.map((item) => {
          const secondsWaiting = Math.max(
            0,
            Math.floor(
              (now.getTime() - new Date(item.created_at).getTime()) / 1000,
            ),
          );
          const slaState = slaVisualState(
            secondsWaiting,
            item.sla_target_seconds,
          );
          const isConfirming = confirmingIds.has(item.id);

          return (
            <div
              key={item.id}
              data-testid="handoff-card"
              data-handoff-id={item.id}
              data-sla-state={slaState}
              className={`rounded-xl border border-l-4 bg-card p-4 shadow-sm ${SLA_BORDER[slaState]}`}
            >
              <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
                {/* Number + code */}
                <div className="min-w-[10rem]">
                  <p className="text-lg font-bold tracking-wide">
                    {item.numero_whatsapp || "—"}
                  </p>
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
                  <p>
                    <span className="text-muted-foreground">
                      {t("handoff_intent")}:
                    </span>{" "}
                    {item.intencao_detectada ?? "—"}
                  </p>
                  {item.safety_flag && (
                    <Badge
                      variant="destructive"
                      className="gap-1"
                      aria-label={t("handoff_safety_marker")}
                    >
                      <ShieldAlert className="h-3 w-3" aria-hidden="true" />
                      {t("handoff_safety_marker")}
                    </Badge>
                  )}
                </div>

                {/* Entry + waiting */}
                <div className="min-w-[12rem] text-sm space-y-0.5">
                  <p>
                    <span className="text-muted-foreground">
                      {t("handoff_entry_time")}:
                    </span>{" "}
                    {new Date(item.created_at).toLocaleString(
                      locale === "pt" ? "pt-BR" : "en-US",
                      displayTimeZone
                        ? { timeZone: displayTimeZone }
                        : undefined,
                    )}
                  </p>
                  <p>
                    <span className="text-muted-foreground">
                      {t("handoff_waiting_since")}
                    </span>{" "}
                    <span className="font-mono">
                      {formatWait(secondsWaiting)}
                    </span>
                  </p>
                </div>

                {/* Spacer pushes the actions to the right (no message/name shown for privacy) */}
                <div className="flex-1 min-w-[2rem]" />

                {/* Actions */}
                <div className="ml-auto shrink-0 space-y-1">
                  <p className="text-right text-[11px] text-muted-foreground">
                    {t("handoff_confirm_guidance")}
                  </p>
                  <div className="flex flex-wrap justify-end gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={!item.numero_whatsapp}
                      onClick={() => handleOpenWhatsApp(item.numero_whatsapp)}
                    >
                      <MessageCircle className="h-4 w-4 mr-1" />
                      {t("handoff_btn_open_whatsapp")}
                    </Button>
                    <Button
                      size="sm"
                      disabled={isConfirming}
                      aria-busy={isConfirming}
                      onClick={() => handleConfirm(item.id)}
                      className="bg-slate-900 text-white hover:bg-slate-800 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white border-transparent"
                    >
                      {isConfirming
                        ? t("handoff_btn_confirming")
                        : t("handoff_btn_confirm")}
                    </Button>
                  </div>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
