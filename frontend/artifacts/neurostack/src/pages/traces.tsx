import { useState, useMemo, useEffect, useCallback } from "react";
import { useAuth } from "@/context/auth";
import { useI18n } from "@/context/i18n";
import { Link } from "wouter";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  ChevronRight, ShieldAlert, AlertTriangle, Clock,
  FileDown, CalendarRange, BarChart3, Loader2,
  ChevronLeft,
} from "lucide-react";
import { IntentBadge } from "@/components/trace-viewer";
import { Skeleton } from "@/components/ui/skeleton";
import { format, parseISO } from "date-fns";
import { useToast } from "@/hooks/use-toast";

const PAGE_SIZE = 20;

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
}

interface TracesResponse {
  items: ApiTrace[];
  total: number;
  limit: number;
  offset: number;
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
  };
}

export default function TracesList() {
  const { isReadOnly, isBuilder, viewingClinicId } = useAuth();
  const { t, locale } = useI18n();
  const txt = (p: string, e: string) => locale === "en" ? e : p;
  const { toast } = useToast();

  const today = format(new Date(), "yyyy-MM-dd");
  const thirtyDaysAgo = format(new Date(Date.now() - 30 * 24 * 60 * 60 * 1000), "yyyy-MM-dd");

  const [startDate, setStartDate] = useState(thirtyDaysAgo);
  const [endDate, setEndDate] = useState(today);
  const [onlySafety, setOnlySafety] = useState(false);
  const [onlyHandoff, setOnlyHandoff] = useState(false);
  const [exporting, setExporting] = useState(false);

  // Pagination
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);

  // Data
  const [traces, setTraces] = useState<ReturnType<typeof mapTrace>[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const fetchTraces = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const token = getToken();
      if (!token) {
        setLoadError("Sessão expirada. Faça login novamente.");
        setTraces([]);
        return;
      }
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String(offset),
      });
      if (startDate) params.set("start_date", startDate);
      if (endDate) params.set("end_date", endDate);
      if (onlySafety) params.set("safety_flag", "true");
      if (onlyHandoff) params.set("handoff_required", "true");
      if (isBuilder && viewingClinicId) params.set("tenant_id", viewingClinicId);

      const res = await fetch(`/v1/traces?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.status === 401) {
        setLoadError("Token inválido ou expirado. Faça login novamente.");
        setTraces([]);
        return;
      }
      if (!res.ok) throw new Error(`Fetch failed: ${res.status}`);
      const data: TracesResponse = await res.json();
      setTraces((data.items ?? []).map(mapTrace));
      setTotal(data.total ?? 0);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : "Unknown error");
      setTraces([]);
    } finally {
      setLoading(false);
    }
  }, [offset, startDate, endDate, onlySafety, onlyHandoff, isBuilder, viewingClinicId]);

  useEffect(() => {
    void fetchTraces();
  }, [fetchTraces]);

  // Reset to first page when filters change
  useEffect(() => {
    setOffset(0);
  }, [startDate, endDate, onlySafety, onlyHandoff]);

  const recentTraces = useMemo(() => {
    return [...traces].sort(
      (a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime()
    );
  }, [traces]);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  async function handleExport() {
    if (isReadOnly) {
      toast({ title: t("traces_view_mode_title"), description: t("traces_view_mode_desc") });
      return;
    }
    if (!startDate || !endDate) return;
    setExporting(true);
    try {
      const token = getToken();
      const params = new URLSearchParams({
        format: "csv",
        start_date: startDate,
        end_date: endDate,
      });
      if (onlySafety) params.set("safety_flag", "true");
      if (onlyHandoff) params.set("handoff_required", "true");
      if (isBuilder && viewingClinicId) params.set("tenant_id", viewingClinicId);

      const res = await fetch(`/v1/traces/export?${params.toString()}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!res.ok) throw new Error(`Export failed: ${res.status}`);
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
    <div className="space-y-6 max-w-5xl mx-auto">

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
              {txt("Exibindo", "Showing")}{" "}
              <span className="font-semibold text-foreground">{recentTraces.length}</span>{" "}
              {txt("de", "of")}{" "}
              <span className="font-semibold text-foreground">{total}</span> traces
            </span>
          </div>
        )}
      </div>

      {/* -- Reports & Export -- */}
      <div className="rounded-xl border border-border bg-card p-5 space-y-4">
        <div className="flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent/10 border border-accent/20">
            <FileDown className="h-4 w-4 text-accent" />
          </div>
          <div>
            <p className="text-sm font-semibold text-foreground">{t("traces_report_title")}</p>
            {locale === "pt" && <p className="text-xs text-muted-foreground">Reports & Export</p>}
          </div>
        </div>

        {/* Date range + export button */}
        <div className="flex flex-col sm:flex-row gap-3 items-end">
          <div className="flex flex-col sm:flex-row gap-3 flex-1">
            <div className="space-y-1.5 flex-1">
              <label className="text-xs font-medium text-muted-foreground flex items-center gap-1.5">
                <CalendarRange className="h-3 w-3" /> {t("traces_start_date")}
              </label>
              <input
                type="date"
                value={startDate}
                max={endDate}
                onChange={e => setStartDate(e.target.value)}
                className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-accent"
              />
            </div>
            <div className="space-y-1.5 flex-1">
              <label className="text-xs font-medium text-muted-foreground flex items-center gap-1.5">
                <CalendarRange className="h-3 w-3" /> {t("traces_end_date")}
              </label>
              <input
                type="date"
                value={endDate}
                min={startDate}
                max={today}
                onChange={e => setEndDate(e.target.value)}
                className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-accent"
              />
            </div>
          </div>

          <button
            onClick={handleExport}
            disabled={exporting}
            className="inline-flex items-center gap-2 rounded-md bg-accent px-5 py-2 text-sm font-semibold text-accent-foreground shadow-sm hover:bg-accent/90 disabled:opacity-60 disabled:cursor-not-allowed transition-all shrink-0 h-[38px]"
          >
            <FileDown className="h-4 w-4" />
            {exporting ? t("traces_generating") : t("traces_export_btn")}
          </button>
        </div>

        {/* -- Advanced filter toggles -- */}
        <div className="flex flex-wrap items-center gap-4 pt-1 border-t border-border">
          <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">{t("traces_filters_label")}</span>

          <label className="flex items-center gap-2 cursor-pointer group select-none">
            <input
              type="checkbox"
              checked={onlySafety}
              onChange={e => setOnlySafety(e.target.checked)}
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
              onChange={e => setOnlyHandoff(e.target.checked)}
              className="h-4 w-4 rounded border-border accent-orange-500 cursor-pointer"
            />
            <span className="text-sm text-foreground font-medium group-hover:text-orange-500 transition-colors">
              {t("traces_filter_handoff")}
            </span>
            {onlyHandoff && (
              <Badge className="text-[10px] px-1.5 py-0 h-4 bg-orange-500 text-white hover:bg-orange-500">{t("traces_filter_active_badge")}</Badge>
            )}
          </label>
        </div>

        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
          <p className="text-xs text-muted-foreground leading-relaxed">
            {t("traces_report_hint")}
            {(onlySafety || onlyHandoff) && (
              <span className="ml-1 font-medium text-foreground">{t("traces_filter_csv_note")}</span>
            )}
          </p>
          {!loading && (
            <span className="text-xs font-medium text-muted-foreground whitespace-nowrap">
              <span className="text-foreground font-semibold">{total}</span> trace{total !== 1 ? "s" : ""}{" "}
              {txt("no periodo", "in period")}
            </span>
          )}
        </div>
      </div>

      {/* -- Trace list -- */}
      <div className="space-y-3">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
          {(onlySafety || onlyHandoff)
            ? `${txt("Filtrado", "Filtered")}: ${recentTraces.length} trace${recentTraces.length !== 1 ? "s" : ""} · ${[onlySafety && "Safety Flag", onlyHandoff && "Handoff"].filter(Boolean).join(" + ")}`
            : `${txt("Pagina", "Page")} ${currentPage} ${txt("de", "of")} ${totalPages} · ${total} traces`
          }
        </p>

        {loading ? (
          Array.from({ length: 5 }).map((_, i) => (
            <Card key={i} className="p-4 flex gap-4">
              <Skeleton className="h-10 w-24 shrink-0" />
              <div className="space-y-2 flex-1">
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-3 w-3/4" />
              </div>
            </Card>
          ))
        ) : loadError ? (
          <div className="p-8 text-center text-destructive bg-destructive/10 rounded-lg space-y-2">
            <p className="font-medium">{t("traces_fail")}</p>
            <p className="text-sm opacity-80">{loadError}</p>
          </div>
        ) : recentTraces.length === 0 ? (
          <div className="p-12 text-center text-muted-foreground bg-muted/30 rounded-lg border border-border">
            {(onlySafety || onlyHandoff)
              ? t("traces_no_filter_match")
              : t("traces_empty")
            }
          </div>
        ) : (
          recentTraces.map((trace) => (
            <Link key={trace.id} href={`/traces/${trace.id}`}>
              <Card
                className="group p-4 flex flex-col sm:flex-row gap-4 sm:items-center hover:border-accent transition-colors cursor-pointer"
                data-testid={`trace-row-${trace.id}`}
              >
                <div className="w-32 shrink-0 flex flex-col gap-2">
                  <IntentBadge intent={trace.srmDecision.intent} />
                  <div className="text-[10px] text-muted-foreground font-mono">
                    {format(new Date(trace.timestamp), "MMM d, HH:mm:ss")}
                  </div>
                </div>

                <div className="flex-1 min-w-0">
                  <div className="text-sm text-foreground truncate font-medium mb-1">
                    "{trace.userMessage}"
                  </div>
                  <div className="flex items-center gap-3 text-xs text-muted-foreground">
                    <span className="capitalize text-foreground font-medium">
                      {trace.srmDecision.stage}
                    </span>
                    <span className="flex items-center gap-1">
                      <Clock className="h-3 w-3" />
                      {Math.round(trace.processingTimeMs)}ms
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
          <div className="flex items-center justify-center gap-4 pt-2">
            <button
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              disabled={offset === 0}
              className="inline-flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-sm font-medium text-foreground hover:bg-muted disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              <ChevronLeft className="h-4 w-4" />
              {txt("Anterior", "Previous")}
            </button>
            <span className="text-xs text-muted-foreground">
              {txt("Pagina", "Page")} {currentPage} {txt("de", "of")} {totalPages}
            </span>
            <button
              onClick={() => setOffset(offset + PAGE_SIZE)}
              disabled={offset + PAGE_SIZE >= total}
              className="inline-flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-sm font-medium text-foreground hover:bg-muted disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              {txt("Proximo", "Next")}
              <ChevronRight className="h-4 w-4" />
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
