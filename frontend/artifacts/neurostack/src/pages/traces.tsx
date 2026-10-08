import { useState, useMemo, useEffect, useCallback, useRef } from "react";
import { useAuth } from "@/context/auth";
import { useI18n } from "@/context/i18n";
import { Link } from "wouter";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  ChevronRight, ShieldAlert, AlertTriangle, Clock,
  FileDown, CalendarRange, BarChart3,
  ChevronLeft,
} from "lucide-react";
import { IntentBadge } from "@/components/trace-viewer";
import { Skeleton } from "@/components/ui/skeleton";
import { format } from "date-fns";
import { useToast } from "@/hooks/use-toast";
import { exceedsExportWindow, exportWindowErrorKey } from "@/lib/export-window";

const PAGE_SIZE = 50;

type ListErrorKey =
  | "traces_error_no_token"
  | "traces_error_unauthorized"
  | "traces_error_timezone_required"
  | "traces_error_day_required"
  | "traces_error_invalid_timezone"
  | "traces_error_day_range_conflict"
  | "traces_error_unknown";

type PaginationItem = number | "start-ellipsis" | "end-ellipsis";
type AccessScope =
  | readonly ["self"]
  | readonly ["tenant-pending"]
  | readonly ["tenant", string]
  | readonly ["investor"];

function buildListScope(
  day: string,
  timezone: string,
  onlySafety: boolean,
  onlyHandoff: boolean,
  channel: "all" | "whatsapp" | "web",
  access: AccessScope,
): string {
  return JSON.stringify([day, timezone, onlySafety, onlyHandoff, channel, access]);
}

function formatLocalDay(day: string, locale: "pt" | "en"): string {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return "";
  const [year, month, date] = day.split("-").map(Number);
  return new Intl.DateTimeFormat(locale === "en" ? "en-US" : "pt-BR", {
    dateStyle: "long",
  }).format(new Date(year, month - 1, date));
}

function listErrorKey(detail: unknown): ListErrorKey {
  const keys: Record<string, ListErrorKey> = {
    timezone_required_with_day: "traces_error_timezone_required",
    day_required_with_timezone: "traces_error_day_required",
    invalid_timezone: "traces_error_invalid_timezone",
    day_conflicts_with_date_range: "traces_error_day_range_conflict",
  };
  return typeof detail === "string" && keys[detail]
    ? keys[detail]
    : "traces_error_unknown";
}

function paginationItems(currentPage: number, totalPages: number): PaginationItem[] {
  if (totalPages <= 5) {
    return Array.from({ length: totalPages }, (_, index) => index + 1);
  }

  const windowStart = Math.min(Math.max(currentPage - 2, 1), totalPages - 4);
  const windowEnd = windowStart + 4;
  const items: PaginationItem[] = [];

  if (windowStart > 1) {
    items.push(1, "start-ellipsis");
  }
  for (let page = windowStart; page <= windowEnd; page += 1) {
    items.push(page);
  }
  if (windowEnd < totalPages) {
    items.push("end-ellipsis", totalPages);
  }
  return items;
}

// ── Investor mock traces ──
const MOCK_TRACES = [
  { id: "t1", timestamp: "2026-05-03T14:32:00Z", userMessage: "Quanto custa a aplicação de botox na testa?", srmDecision: { intent: "pedido_preco", stage: "qualification", action: "respond", confidence: 0.94, safety_concern: false, handoff_required: false, constraints: [], gate_authority_before_price: true }, processingTimeMs: 823, channel: "whatsapp" },
  { id: "t2", timestamp: "2026-05-03T14:15:00Z", userMessage: "Gostaria de agendar uma limpeza de pele", srmDecision: { intent: "agendar", stage: "scheduling", action: "respond", confidence: 0.97, safety_concern: false, handoff_required: false, constraints: [], gate_authority_before_price: false }, processingTimeMs: 654, channel: "whatsapp" },
  { id: "t3", timestamp: "2026-05-03T13:45:00Z", userMessage: "Quero saber sobre tratamento para manchas", srmDecision: { intent: "interesse_tratamento", stage: "discovery", action: "respond", confidence: 0.91, safety_concern: false, handoff_required: false, constraints: [], gate_authority_before_price: false }, processingTimeMs: 912, channel: "web" },
  { id: "t4", timestamp: "2026-05-03T12:22:00Z", userMessage: "Fiz um preenchimento e estou com inchaço intenso, está muito roxo", srmDecision: { intent: "pos_atendimento_sintomas_preocupantes", stage: "post_care", action: "respond", confidence: 0.98, safety_concern: true, handoff_required: false, constraints: [], gate_authority_before_price: false }, processingTimeMs: 1102, channel: "whatsapp" },
  { id: "t5", timestamp: "2026-05-03T11:50:00Z", userMessage: "Preciso cancelar minha consulta de amanhã", srmDecision: { intent: "cancelar", stage: "scheduling", action: "respond", confidence: 0.96, safety_concern: false, handoff_required: false, constraints: [], gate_authority_before_price: false }, processingTimeMs: 589, channel: "whatsapp" },
  { id: "t6", timestamp: "2026-05-03T10:30:00Z", userMessage: "O atendimento foi péssimo, quero falar com o gerente", srmDecision: { intent: "reclamacao", stage: "escalation", action: "handoff", confidence: 0.99, safety_concern: false, handoff_required: true, constraints: [], gate_authority_before_price: false }, processingTimeMs: 734, channel: "whatsapp" },
  { id: "t7", timestamp: "2026-05-03T09:12:00Z", userMessage: "Vocês fazem harmonização facial?", srmDecision: { intent: "interesse_tratamento", stage: "discovery", action: "respond", confidence: 0.88, safety_concern: false, handoff_required: false, constraints: [], gate_authority_before_price: false }, processingTimeMs: 876, channel: "web" },
  { id: "t8", timestamp: "2026-05-02T16:45:00Z", userMessage: "Queria remarcar minha consulta para semana que vem", srmDecision: { intent: "reagendar", stage: "scheduling", action: "respond", confidence: 0.95, safety_concern: false, handoff_required: false, constraints: [], gate_authority_before_price: false }, processingTimeMs: 701, channel: "whatsapp" },
];

function getToken(): string | null {
  try {
    const raw = localStorage.getItem("ns_auth");
    if (!raw) return null;
    return (JSON.parse(raw) as { token?: string }).token ?? null;
  } catch {
    return null;
  }
}

interface ApiTrace {
  id: string;
  timestamp: string;
  user_message: string;
  srm_decision: {
    intent: string;
    intent_category?: string;
    funnel_stage?: string;
    action: string;
    confidence: number;
    safety_concern: boolean;
    handoff_required: boolean;
  };
  context_summary?: string;
  prompt_built?: string;
  processing_time_ms: number;
  mode?: string;
  channel?: string;
}

interface TracesResponse {
  items: ApiTrace[];
  total: number;
  limit: number;
  offset: number;
}

interface ListResult {
  requestKey: string;
  tenant: string;
  traces: ReturnType<typeof mapTrace>[];
  total: number;
  error: ListErrorKey | null;
}

/** Map API snake_case trace to the shape the UI components expect */
function mapTrace(t: ApiTrace) {
  return {
    id: t.id,
    timestamp: t.timestamp,
    userMessage: t.user_message,
    srmDecision: {
      intent: t.srm_decision.intent,
      stage: t.srm_decision.funnel_stage ?? t.srm_decision.intent_category ?? "unknown",
      action: t.srm_decision.action,
      confidence: t.srm_decision.confidence,
      safety_concern: t.srm_decision.safety_concern,
      handoff_required: t.srm_decision.handoff_required,
      constraints: [],
      gate_authority_before_price: t.srm_decision.intent === "pedido_preco",
    },
    processingTimeMs: t.processing_time_ms,
    channel: t.channel ?? "unknown",
  };
}

export default function TracesList() {
  const { isReadOnly, isBuilder, isSuperAdmin, isInvestor, viewingClinicId } = useAuth();
  const { t, locale } = useI18n();
  const { toast } = useToast();

  const [initialDates] = useState(() => {
    const now = new Date();
    return {
      today: format(now, "yyyy-MM-dd"),
      todayDateTime: `${format(now, "yyyy-MM-dd")}T${format(now, "HH:mm")}`,
      exportStart: `${format(new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000), "yyyy-MM-dd")}T00:00`,
    };
  });
  const [browserTimezone] = useState(
    () => Intl.DateTimeFormat().resolvedOptions().timeZone,
  );
  const [pendingDay, setPendingDay] = useState(initialDates.today);
  const [appliedDay, setAppliedDay] = useState(initialDates.today);
  const [exportStart, setExportStart] = useState(initialDates.exportStart);
  const [exportEnd, setExportEnd] = useState(initialDates.todayDateTime);
  const [onlySafety, setOnlySafety] = useState(false);
  const [onlyHandoff, setOnlyHandoff] = useState(false);
  const [channelFilter, setChannelFilter] = useState<"all" | "whatsapp" | "web">("all");
  const [exporting, setExporting] = useState(false);
  const exportTooLong = exceedsExportWindow(exportStart, exportEnd);

  // Pagination
  const effectiveTenant = (isBuilder || isSuperAdmin) ? viewingClinicId ?? "" : "";
  const accessScope: AccessScope = isInvestor
    ? ["investor"]
    : (isBuilder || isSuperAdmin)
      ? viewingClinicId
        ? ["tenant", viewingClinicId]
        : ["tenant-pending"]
      : ["self"];
  const listScope = buildListScope(
    appliedDay,
    browserTimezone,
    onlySafety,
    onlyHandoff,
    channelFilter,
    accessScope,
  );
  const [pagination, setPagination] = useState({ scope: listScope, page: 1 });
  const currentPage = pagination.scope === listScope ? pagination.page : 1;
  const offset = (currentPage - 1) * PAGE_SIZE;
  const requestKey = JSON.stringify([listScope, currentPage, offset]);
  const listStartRef = useRef<HTMLDivElement>(null);
  const requestSequenceRef = useRef(0);

  // Data
  const [listResult, setListResult] = useState<ListResult | null>(null);
  const currentResult = listResult?.requestKey === requestKey ? listResult : null;
  const traces = currentResult?.traces ?? [];
  const total = currentResult?.total ?? 0;
  const loadError = currentResult?.error ?? null;
  const resultTenant = currentResult?.tenant ?? "";
  const loading = currentResult === null;

  const fetchTraces = useCallback(async () => {
    const requestSequence = ++requestSequenceRef.current;
    const resultTenant = effectiveTenant;
    // Investor: use mock data
    if (isInvestor) {
      setListResult({
        requestKey,
        tenant: resultTenant,
        traces: MOCK_TRACES,
        total: MOCK_TRACES.length,
        error: null,
      });
      return;
    }
    // For builders/super_admin, wait until viewingClinicId is resolved
    if ((isBuilder || isSuperAdmin) && !viewingClinicId) {
      return;
    }
    try {
      const token = getToken();
      if (!token) {
        if (requestSequence === requestSequenceRef.current) {
          setListResult({
            requestKey,
            tenant: resultTenant,
            traces: [],
            total: 0,
            error: "traces_error_no_token",
          });
        }
        return;
      }
      const params = new URLSearchParams({
        day: appliedDay,
        timezone: browserTimezone,
        limit: String(PAGE_SIZE),
        offset: String(offset),
      });
      if (onlySafety) params.set("safety_flag", "true");
      if (onlyHandoff) params.set("handoff_required", "true");
      if (channelFilter !== "all") params.set("channel", channelFilter);
      if ((isBuilder || isSuperAdmin) && viewingClinicId) params.set("tenant_id", viewingClinicId);

      const res = await fetch(`/v1/traces?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (requestSequence !== requestSequenceRef.current) return;
      if (res.status === 401) {
        setListResult({
          requestKey,
          tenant: resultTenant,
          traces: [],
          total: 0,
          error: "traces_error_unauthorized",
        });
        return;
      }
      if (!res.ok) {
        let detail: unknown;
        try {
          detail = ((await res.json()) as { detail?: unknown }).detail;
        } catch {
          detail = undefined;
        }
        if (requestSequence !== requestSequenceRef.current) return;
        setListResult({
          requestKey,
          tenant: resultTenant,
          traces: [],
          total: 0,
          error: listErrorKey(detail),
        });
        return;
      }
      const data: TracesResponse = await res.json();
      if (requestSequence !== requestSequenceRef.current) return;
      const responseTotal = data.total ?? 0;
      const lastPage = Math.max(1, Math.ceil(responseTotal / PAGE_SIZE));
      if (currentPage > lastPage) {
        setPagination({ scope: listScope, page: lastPage });
        return;
      }
      setListResult({
        requestKey,
        tenant: resultTenant,
        traces: (data.items ?? []).slice(0, PAGE_SIZE).map(mapTrace),
        total: responseTotal,
        error: null,
      });
    } catch {
      if (requestSequence === requestSequenceRef.current) {
        setListResult({
          requestKey,
          tenant: resultTenant,
          traces: [],
          total: 0,
          error: "traces_error_unknown",
        });
      }
    }
  }, [offset, appliedDay, browserTimezone, onlySafety, onlyHandoff, channelFilter, isBuilder, isSuperAdmin, isInvestor, viewingClinicId, effectiveTenant, requestKey, currentPage, listScope]);

  useEffect(() => {
    void fetchTraces();
  }, [fetchTraces]);

  useEffect(() => {
    if (pagination.scope !== listScope) {
      setPagination({ scope: listScope, page: 1 });
    }
  }, [listScope, pagination.scope]);

  const recentTraces = useMemo(() => {
    return [...traces].sort(
      (a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime()
    );
  }, [traces]);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const localizedAppliedDay = useMemo(
    () => formatLocalDay(appliedDay, locale),
    [appliedDay, locale],
  );
  const localizedPendingDay = useMemo(
    () => formatLocalDay(pendingDay, locale),
    [pendingDay, locale],
  );
  const visiblePaginationItems = useMemo(
    () => paginationItems(currentPage, totalPages),
    [currentPage, totalPages],
  );

  function applyPendingDay() {
    setAppliedDay(pendingDay);
    setPagination({
      scope: buildListScope(
        pendingDay,
        browserTimezone,
        onlySafety,
        onlyHandoff,
        channelFilter,
        accessScope,
      ),
      page: 1,
    });
  }

  function goToPage(page: number) {
    if (page < 1 || page > totalPages || page === currentPage) return;
    setPagination({ scope: listScope, page });
    listStartRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function handleExport() {
    if (isReadOnly) {
      toast({ title: t("traces_view_mode_title"), description: t("traces_view_mode_desc") });
      return;
    }
    if (!exportStart || !exportEnd || exportTooLong) return;
    setExporting(true);
    try {
      const token = getToken();
      const params = new URLSearchParams({
        format: "csv",
        start_date: exportStart,
        end_date: exportEnd,
      });
      if (onlySafety) params.set("safety_flag", "true");
      if (onlyHandoff) params.set("handoff_required", "true");
      if (channelFilter !== "all") params.set("channel", channelFilter);
      if ((isBuilder || isSuperAdmin) && viewingClinicId) params.set("tenant_id", viewingClinicId);

      const res = await fetch(`/v1/traces/export?${params.toString()}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!res.ok) {
        let detail: unknown;
        try {
          detail = (await res.json())?.detail;
        } catch {
          detail = undefined;
        }
        const windowErrorKey = exportWindowErrorKey(detail);
        if (windowErrorKey) {
          toast({ title: t("traces_export_err_title"), description: t(windowErrorKey), variant: "destructive" });
          return;
        }
        throw new Error(`Export failed: ${res.status}`);
      }
      const blob = await res.blob();
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = "neurostack_traces_report.csv";
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
      URL.revokeObjectURL(objectUrl);
    } catch {
      toast({ title: t("traces_export_err_title"), description: t("traces_export_err_desc"), variant: "destructive" });
    } finally {
      setExporting(false);
    }
  }

  return (
    <div className="space-y-6 max-w-7xl mx-auto" data-testid="traces-content">

      {/* -- Header -- */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-foreground">{t("traces_title")}</h1>
          <p className="text-muted-foreground mt-1">{t("traces_subtitle")}</p>
        </div>
        {!loading && (
          <div className="flex items-center gap-2 text-xs text-muted-foreground bg-muted/40 border border-border rounded-lg px-3 py-2 shrink-0">
            <BarChart3 className="h-3.5 w-3.5 text-accent" />
            <span>
              {t("traces_showing")}{" "}
              <span className="font-semibold text-foreground">{recentTraces.length}</span>{" "}
              {t("traces_of")}{" "}
              <span className="font-semibold text-foreground">{total}</span> traces{" "}
              {t("traces_on_day")} {localizedAppliedDay}
            </span>
          </div>
        )}
      </div>

      {/* -- Daily list controls -- */}
      <div className="rounded-xl border border-border bg-card p-5 space-y-4">
        <div className="flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent/10 border border-accent/20">
            <CalendarRange className="h-4 w-4 text-accent" />
          </div>
          <p className="text-sm font-semibold text-foreground">{t("traces_day_section_title")}</p>
        </div>

        <div className="flex flex-col sm:flex-row sm:items-end gap-3">
          <div className="space-y-1.5 w-full sm:max-w-xs">
            <label htmlFor="trace-day" className="text-xs font-medium text-muted-foreground">
              {t("traces_day_label")}
            </label>
            <input
              id="trace-day"
              type="date"
              value={pendingDay}
              max={initialDates.today}
              onChange={event => setPendingDay(event.target.value)}
              className="w-full min-w-0 rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-accent"
            />
          </div>
          <button
            type="button"
            onClick={applyPendingDay}
            disabled={!pendingDay}
            className="inline-flex max-w-full items-center justify-center rounded-md bg-accent px-5 py-2 text-sm font-semibold text-accent-foreground shadow-sm hover:bg-accent/90 disabled:opacity-60 disabled:cursor-not-allowed transition-all sm:self-end"
          >
            <span className="break-words">{t("traces_show_day")} {localizedPendingDay}</span>
          </button>
        </div>

        <div className="flex flex-wrap items-center gap-4 pt-3 border-t border-border">
          <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">{t("traces_filters_label")}</span>

          <label className="flex items-center gap-2 cursor-pointer group select-none">
            <input
              type="checkbox"
              checked={onlySafety}
              onChange={event => setOnlySafety(event.target.checked)}
              className="h-4 w-4 rounded border-border accent-destructive cursor-pointer"
            />
            <span className="text-sm text-foreground font-medium group-hover:text-destructive transition-colors">
              {t("traces_filter_safety")}
            </span>
            {onlySafety && (
              <Badge variant="destructive" className="text-[10px] px-1.5 py-0 h-4">{t("traces_filter_active_badge")}</Badge>
            )}
          </label>

          <label className="flex items-center gap-2 cursor-pointer group select-none">
            <input
              type="checkbox"
              checked={onlyHandoff}
              onChange={event => setOnlyHandoff(event.target.checked)}
              className="h-4 w-4 rounded border-border accent-orange-500 cursor-pointer"
            />
            <span className="text-sm text-foreground font-medium group-hover:text-orange-500 transition-colors">
              {t("traces_filter_handoff")}
            </span>
            {onlyHandoff && (
              <Badge className="text-[10px] px-1.5 py-0 h-4 bg-orange-500 text-white hover:bg-orange-500">{t("traces_filter_active_badge")}</Badge>
            )}
          </label>

          <label className="flex flex-wrap items-center gap-2 sm:ml-2 sm:pl-2 sm:border-l border-border">
            <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">{t("traces_source")}</span>
            <select
              value={channelFilter}
              onChange={event => setChannelFilter(event.target.value as "all" | "whatsapp" | "web")}
              className="h-7 max-w-full rounded-md border border-border bg-background px-2 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-accent cursor-pointer"
            >
              <option value="all">{t("traces_source_all")}</option>
              <option value="whatsapp">WhatsApp</option>
              <option value="web">{t("traces_source_simulator")}</option>
            </select>
            {channelFilter !== "all" && (
              <Badge className={`text-[10px] px-1.5 py-0 h-4 ${channelFilter === "whatsapp" ? "bg-emerald-500 text-white hover:bg-emerald-500" : "bg-blue-500 text-white hover:bg-blue-500"}`}>
                {channelFilter === "whatsapp" ? "WhatsApp" : t("traces_source_simulator")}
              </Badge>
            )}
          </label>
        </div>
      </div>

      {/* -- Reports & Export -- */}
      <div className="rounded-xl border border-border bg-card p-5 space-y-4">
        <div className="flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent/10 border border-accent/20">
            <FileDown className="h-4 w-4 text-accent" />
          </div>
          <div>
            <p className="text-sm font-semibold text-foreground">{t("traces_report_title")}</p>
          </div>
        </div>

        {/* Date range + export button */}
        <div className="flex flex-col sm:flex-row gap-3 items-end">
          <div className="flex flex-col sm:flex-row gap-3 flex-1">
            <div className="space-y-1.5 flex-1">
              <label htmlFor="trace-export-start" className="text-xs font-medium text-muted-foreground flex items-center gap-1.5">
                <CalendarRange className="h-3 w-3" /> {t("traces_start_date")}
              </label>
              <input
                id="trace-export-start"
                type="datetime-local"
                value={exportStart}
                max={exportEnd}
                onChange={event => setExportStart(event.target.value)}
                className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-accent"
              />
            </div>
            <div className="space-y-1.5 flex-1">
              <label htmlFor="trace-export-end" className="text-xs font-medium text-muted-foreground flex items-center gap-1.5">
                <CalendarRange className="h-3 w-3" /> {t("traces_end_date")}
              </label>
              <input
                id="trace-export-end"
                type="datetime-local"
                value={exportEnd}
                min={exportStart}
                max={initialDates.todayDateTime}
                onChange={event => setExportEnd(event.target.value)}
                className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-accent"
              />
            </div>
          </div>

          <button
            onClick={handleExport}
            disabled={exporting || exportTooLong}
            aria-describedby={exportTooLong ? "trace-export-window-hint" : undefined}
            className="inline-flex items-center gap-2 rounded-md bg-accent px-5 py-2 text-sm font-semibold text-accent-foreground shadow-sm hover:bg-accent/90 disabled:opacity-60 disabled:cursor-not-allowed transition-all shrink-0 h-[38px]"
          >
            <FileDown className="h-4 w-4" />
            {exporting ? t("traces_generating") : t("traces_export_btn")}
          </button>
        </div>

        {exportTooLong && (
          <p id="trace-export-window-hint" role="status" className="text-xs font-medium text-destructive">
            {t("audit_export_window_exceeded_hint")}
          </p>
        )}
        <p className="text-xs text-muted-foreground">{t("audit_export_window_rule")}</p>

        <p className="text-xs text-muted-foreground leading-relaxed">
          {t("traces_report_hint")}
          {(onlySafety || onlyHandoff || channelFilter !== "all") && (
            <span className="ml-1 font-medium text-foreground">{t("traces_filter_csv_note")}</span>
          )}
        </p>
      </div>

      {/* -- Trace list -- */}
      <div ref={listStartRef} className="space-y-3 scroll-mt-4">
        <h2 className="text-base font-semibold text-foreground">
          {t("traces_list_title")} {localizedAppliedDay}
        </h2>
        {!loading && (
          <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            {t("traces_page")} {currentPage} {t("traces_of")} {totalPages} · {total} traces · {localizedAppliedDay}
          </p>
        )}

        {loading ? (
          <div aria-live="polite" className="space-y-3">
            <p className="sr-only">{t("traces_loading_day")} {localizedAppliedDay}</p>
            {Array.from({ length: 5 }).map((_, i) => (
              <Card key={i} className="p-4 flex gap-4">
                <Skeleton className="h-10 w-24 shrink-0" />
                <div className="space-y-2 flex-1">
                  <Skeleton className="h-4 w-full" />
                  <Skeleton className="h-3 w-3/4" />
                </div>
              </Card>
            ))}
          </div>
        ) : loadError ? (
          <div role="alert" className="p-8 text-center text-destructive bg-destructive/10 rounded-lg space-y-2">
            <p className="font-medium">{t("traces_fail")}</p>
            <p className="text-sm opacity-80">{t(loadError)}</p>
            <p className="text-xs opacity-80">{localizedAppliedDay}</p>
          </div>
        ) : recentTraces.length === 0 ? (
          <div role="status" className="p-12 text-center text-muted-foreground bg-muted/30 rounded-lg border border-border">
            {(onlySafety || onlyHandoff || channelFilter !== "all")
              ? `${t("traces_no_filter_match_day")} ${localizedAppliedDay}.`
              : `${t("traces_empty_day")} ${localizedAppliedDay}.`
            }
          </div>
        ) : (
          recentTraces.map((trace) => (
            <Link
              key={trace.id}
              href={`/traces/${trace.id}${resultTenant ? `?tenant_id=${encodeURIComponent(resultTenant)}` : ""}`}
            >
              <Card
                className="group p-4 flex flex-col gap-4 md:min-h-24 md:flex-row md:items-center hover:border-accent transition-colors cursor-pointer"
                data-testid={`trace-row-${trace.id}`}
              >
                <div
                  className="w-full md:w-56 shrink-0 flex flex-col gap-2"
                  data-testid={`trace-intent-${trace.id}`}
                >
                  <IntentBadge
                    intent={trace.srmDecision.intent}
                    className="w-full justify-center"
                  />
                  <div className="text-[10px] text-muted-foreground font-mono">
                    {format(new Date(trace.timestamp), "MMM d, HH:mm:ss")}
                  </div>
                </div>

                <div className="flex-1 min-w-0">
                  <div
                    className="text-sm text-foreground font-medium mb-1 whitespace-normal break-words [overflow-wrap:anywhere]"
                    data-testid={`trace-message-${trace.id}`}
                  >
                    "{trace.userMessage}"
                  </div>
                  <div
                    className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground"
                    data-testid={`trace-metadata-${trace.id}`}
                  >
                    <span className="capitalize text-foreground font-medium">
                      {trace.srmDecision.stage}
                    </span>
                    <span className="flex items-center gap-1">
                      <Clock className="h-3 w-3" />
                      {Math.round(trace.processingTimeMs)}ms
                    </span>
                    <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium ${trace.channel === "whatsapp" ? "bg-emerald-500/10 text-emerald-500" : trace.channel === "web" ? "bg-blue-500/10 text-blue-500" : "bg-muted text-muted-foreground"}`}>
                      {trace.channel === "whatsapp" ? "WhatsApp" : trace.channel === "web" ? t("traces_source_simulator") : trace.channel}
                    </span>
                    {(trace.srmDecision.safety_concern || trace.srmDecision.handoff_required) && (
                      <div className="flex items-center gap-2 ml-2">
                        {trace.srmDecision.safety_concern && (
                          <ShieldAlert className="h-3 w-3 text-destructive" />
                        )}
                        {trace.srmDecision.handoff_required && (
                          <AlertTriangle className="h-3 w-3 text-orange-500" />
                        )}
                      </div>
                    )}
                  </div>
                </div>

                <div className="shrink-0 text-muted-foreground group-hover:text-accent transition-colors">
                  <ChevronRight className="h-5 w-5" />
                </div>
              </Card>
            </Link>
          ))
        )}

        {/* Pagination controls */}
        {!loading && total > PAGE_SIZE && (
          <nav
            aria-label={`${t("traces_page")} ${currentPage} ${t("traces_of")} ${totalPages}`}
            className="flex flex-wrap items-center justify-center gap-2 pt-2"
          >
            <button
              type="button"
              onClick={() => goToPage(currentPage - 1)}
              disabled={currentPage === 1}
              className="inline-flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-sm font-medium text-foreground hover:bg-muted disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              <ChevronLeft className="h-4 w-4" />
              {t("traces_previous")}
            </button>
            {visiblePaginationItems.map(item => item === "start-ellipsis" || item === "end-ellipsis" ? (
              <span key={item} aria-hidden="true" className="px-1 text-sm text-muted-foreground">…</span>
            ) : (
              <button
                key={item}
                type="button"
                aria-label={`${t("traces_page")} ${item}`}
                aria-current={item === currentPage ? "page" : undefined}
                onClick={() => goToPage(item)}
                className={`min-w-8 rounded-md border px-2 py-1.5 text-sm font-medium transition-colors ${item === currentPage ? "border-accent bg-accent text-accent-foreground" : "border-border text-foreground hover:bg-muted"}`}
              >
                {item}
              </button>
            ))}
            <button
              type="button"
              onClick={() => goToPage(currentPage + 1)}
              disabled={currentPage === totalPages}
              className="inline-flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-sm font-medium text-foreground hover:bg-muted disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              {t("traces_next")}
              <ChevronRight className="h-4 w-4" />
            </button>
          </nav>
        )}
      </div>
    </div>
  );
}
