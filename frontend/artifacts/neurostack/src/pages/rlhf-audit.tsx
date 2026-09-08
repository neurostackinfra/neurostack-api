import { useEffect, useMemo, useState } from "react";
import { Link } from "wouter";
import { customFetch } from "@workspace/api-client-react";
import { format } from "date-fns";
import {
  Brain, ShieldAlert, CheckCircle2, Zap, Loader2, AlertTriangle, Clock, MessageSquare, ExternalLink,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useToast } from "@/hooks/use-toast";
import { useAuth } from "@/context/auth";
import { useI18n } from "@/context/i18n";
import { DateRangePicker } from "@/components/date-range-picker";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell,
} from "recharts";

// ── Types ────────────────────────────────────────────────────────────────────

export interface ReviewHistoryRow {
  observacao: string | null;
  revisado: boolean;
  revisado_por_email: string | null;
  revisado_em: string;
}

export interface RatingEntry {
  id: string;
  ts: string;
  tenant_id: string;
  rating: number;
  reason: string | null;
  agent_response: string | null;
  user_message: string | null;
  submitted_by: string | null;
  ai_analysis: { status: "agreed" | "disagreed"; justification: string } | null;
  ai_analysis_status: "pending" | "done" | "failed" | null;
  approved_for_training: boolean;
  ignored: boolean;
  subject_tenant_id: string;
  subject_clinic_name: string | null;
  builder_name: string | null;
  accepted_by: string | null;
  accepted_at: string | null;
  accepted_by_email: string | null;
  // Added in Group 4 (backend). Every entry carries these; may be null.
  channel?: string;
  trace_event_id?: string | null;
  trace_tenant_id?: string | null;
  // Super-admin-only: JSON key is entirely absent for non-super callers.
  // Ordered newest-first — review_history[0] is current state.
  review_history?: ReviewHistoryRow[];
}

export interface RatingSummary {
  total: number;
  average: number;
  accepted_count: number;
  distribution: Record<"1" | "2" | "3" | "4" | "5", number>;
  // Super-admin-only: JSON key entirely absent for non-super callers.
  reviewed_count?: number;
}

export interface RatingFacetBuilder {
  id: string;
  name: string;
}

export interface RatingFacetClinic {
  id: string;
  name: string;
  builder_id: string | null;
}

export interface RatingsResponse {
  summary: RatingSummary;
  facets: {
    builders: RatingFacetBuilder[];
    clinics: RatingFacetClinic[];
  };
  entries: RatingEntry[];
}

// ── Helpers ──────────────────────────────────────────────────────────────────

// Minimum characters (after trimming) for a critique to be a candidate for
// acceptance into the training dataset. Mirrors the `_has_reason` threshold in
// `src/api/routes/rlhf.py`; the backend PATCH .../ratings/{id} action="approve"
// returns 422 when a reason falls short, so we gate the button here to avoid a
// doomed request.
const MIN_REASON_LENGTH = 8;

function hasQualifyingReason(entry: Pick<RatingEntry, "reason">): boolean {
  return typeof entry.reason === "string" && entry.reason.trim().length >= MIN_REASON_LENGTH;
}

const RATING_META: Record<number, { label: string; color: string; tailwind: string }> = {
  1: { label: "Péssimo", color: "#ef4444", tailwind: "bg-red-500"    },
  2: { label: "Ruim",    color: "#fb7185", tailwind: "bg-rose-400"   },
  3: { label: "Regular", color: "#facc15", tailwind: "bg-yellow-400" },
  4: { label: "Bom",     color: "#4ade80", tailwind: "bg-green-400"  },
  5: { label: "Ótimo",   color: "#22c55e", tailwind: "bg-green-500"  },
};

const ALL_BUILDERS = "__all_builders__";
const ALL_CLINICS  = "__all_clinics__";
const ALL_CHANNELS = "__all_channels__";
const ALL_REVIEWED = "__all_reviewed__";

function defaultDates() {
  const today       = format(new Date(), "yyyy-MM-dd");
  const thirtyAgo   = format(new Date(Date.now() - 30 * 24 * 60 * 60 * 1000), "yyyy-MM-dd");
  return { startDate: thirtyAgo, endDate: today };
}

function RatingBadge({ rating }: { rating: number }) {
  const meta = RATING_META[rating] ?? RATING_META[3];
  return (
    <span
      className={`inline-flex items-center justify-center h-6 w-6 rounded text-white text-[11px] font-bold ${meta.tailwind}`}
    >
      {rating}
    </span>
  );
}

function AiAnalysisCell({ entry }: { entry: RatingEntry }) {
  if (!entry.ai_analysis_status) return <span className="text-[10px] text-muted-foreground">—</span>;

  if (entry.ai_analysis_status === "pending") {
    return (
      <span className="inline-flex items-center gap-1 text-[10px] text-muted-foreground">
        <Clock className="h-3 w-3" />
        Analisando...
      </span>
    );
  }

  if (entry.ai_analysis_status === "failed") {
    return (
      <span className="inline-flex items-center gap-1 text-[10px] text-amber-600">
        <AlertTriangle className="h-3 w-3" />
        Análise indisponível
      </span>
    );
  }

  if (entry.ai_analysis_status === "done" && entry.ai_analysis) {
    const agreed = entry.ai_analysis.status === "agreed";
    return (
      <div>
        <Badge
          variant="outline"
          className={`text-[10px] py-0 gap-1 mb-0.5 ${
            agreed
              ? "bg-green-500/10 text-green-600 border-green-500/30"
              : "bg-red-500/10 text-red-500 border-red-500/30"
          }`}
        >
          {agreed ? <CheckCircle2 className="h-2.5 w-2.5" /> : <ShieldAlert className="h-2.5 w-2.5" />}
          {agreed ? "Concorda" : "Discorda"}
        </Badge>
        <p className="text-[10px] text-muted-foreground italic leading-tight">
          {entry.ai_analysis.justification}
        </p>
      </div>
    );
  }

  return null;
}

// ── KPI cards ────────────────────────────────────────────────────────────────

function pct(numerator: number, total: number): number {
  if (total <= 0) return 0;
  return Math.round((numerator / total) * 1000) / 10;
}

function formatNumber(value: number): string {
  return value.toLocaleString("pt-BR");
}

function KpiCards({
  summary,
  showReviewed,
}: {
  summary: RatingSummary;
  showReviewed: boolean;
}) {
  const { t } = useI18n();
  const total       = summary.total;
  const satisfied   = (summary.distribution["4"] ?? 0) + (summary.distribution["5"] ?? 0);
  const unsatisfied = (summary.distribution["1"] ?? 0) + (summary.distribution["2"] ?? 0);
  const pctSat      = pct(satisfied, total);
  const pctUnsat    = pct(unsatisfied, total);

  const cards: { label: string; value: string; sub: string; color: string; testId: string }[] = [
    {
      label: "Total de Avaliações",
      value: formatNumber(total),
      sub: "Todas as clínicas no período",
      color: "text-foreground",
      testId: "kpi-total",
    },
    {
      label: "Nota média",
      value: summary.average.toLocaleString("pt-BR", { minimumFractionDigits: 1, maximumFractionDigits: 1 }),
      sub: "Escala 1 a 5",
      color: "text-foreground",
      testId: "kpi-average",
    },
    {
      label: "% Satisfação",
      value: `${pctSat.toLocaleString("pt-BR", { maximumFractionDigits: 1 })}%`,
      sub: "Notas 4 ou 5",
      color: "text-green-400",
      testId: "kpi-satisfaction",
    },
    {
      label: "% Insatisfação",
      value: `${pctUnsat.toLocaleString("pt-BR", { maximumFractionDigits: 1 })}%`,
      sub: "Notas 1 ou 2",
      color: "text-red-400",
      testId: "kpi-unsatisfaction",
    },
    {
      label: "Opiniões aceitas",
      value: formatNumber(summary.accepted_count),
      sub: "Gravadas no dataset",
      color: "text-violet-400",
      testId: "kpi-accepted",
    },
  ];

  // Belt-and-suspenders: backend already omits `reviewed_count` for non-super
  // callers, but we also gate client-side per repo convention.
  if (showReviewed && summary.reviewed_count !== undefined) {
    cards.push({
      label: t("rlhf_audit_kpi_reviewed_label"),
      value: formatNumber(summary.reviewed_count),
      sub: t("rlhf_audit_kpi_reviewed_sub"),
      color: "text-sky-400",
      testId: "kpi-reviewed",
    });
  }

  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
      {cards.map((s) => (
        <Card key={s.label} className="border-border bg-card shadow-sm" data-testid={s.testId}>
          <CardContent className="pt-4 pb-4">
            <div className={`text-3xl font-bold ${s.color}`}>{s.value}</div>
            <div className="text-xs font-semibold text-foreground/80 mt-0.5">{s.label}</div>
            <div className="text-[10px] text-muted-foreground">{s.sub}</div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

// ── Distribution chart ───────────────────────────────────────────────────────

function DistributionChart({ summary }: { summary: RatingSummary }) {
  const total = summary.total;
  const chartData = [1, 2, 3, 4, 5].map((n) => {
    const key = String(n) as "1" | "2" | "3" | "4" | "5";
    const count = summary.distribution[key] ?? 0;
    return {
      name: RATING_META[n].label,
      count,
      color: RATING_META[n].color,
      pct: pct(count, total),
    };
  });

  return (
    <Card className="border-border shadow-sm">
      <CardContent className="pt-4 pb-2">
        <p className="text-[10px] font-bold uppercase tracking-widest text-muted-foreground mb-3">
          Distribuição de Notas
        </p>
        <ResponsiveContainer width="100%" height={140}>
          <BarChart data={chartData} layout="vertical" margin={{ left: 8, right: 24 }}>
            <XAxis type="number" tick={{ fontSize: 10 }} />
            <YAxis type="category" dataKey="name" tick={{ fontSize: 10 }} width={80} />
            <Tooltip
              formatter={(_v: number, _name: string, props: { payload?: { count: number; pct: number } }) => {
                const p = props.payload;
                if (!p) return ["", ""];
                return [`${p.count} (${p.pct.toLocaleString("pt-BR", { maximumFractionDigits: 1 })}%)`, "Total"];
              }}
              contentStyle={{ fontSize: 11 }}
            />
            <Bar dataKey="count" radius={[0, 4, 4, 0]}>
              {chartData.map((entry) => (
                <Cell key={entry.name} fill={entry.color} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
        <div className="mt-2 grid grid-cols-5 gap-2 px-2">
          {chartData.map((d) => (
            <div key={d.name} className="text-center" data-testid={`dist-${d.name}`}>
              <div className="text-xs font-semibold text-foreground tabular-nums">
                {formatNumber(d.count)}
              </div>
              <div className="text-[10px] text-muted-foreground tabular-nums">
                {d.pct.toLocaleString("pt-BR", { maximumFractionDigits: 1 })}%
              </div>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

// ── Filters ──────────────────────────────────────────────────────────────────

interface FiltersState {
  builderId: string;   // "" (todos) or a real facet id
  clinicId: string;    // "" (todas) or a real facet id
  startDate: string;
  endDate: string;
  onlyAccepted: boolean;
  channel: string;     // "" (todos) | "web" | "whatsapp" | "unknown"
  reviewed: string;    // "" (todos) | "true" | "false"
}

function Filters({
  filters,
  onChange,
  facets,
  showBuilderClinic,
  showReviewed,
}: {
  filters: FiltersState;
  onChange: (next: FiltersState) => void;
  facets: RatingsResponse["facets"];
  showBuilderClinic: boolean;
  showReviewed: boolean;
}) {
  const { t } = useI18n();
  // Client-side narrowing: only clinics whose builder_id matches the currently
  // selected builder appear. When "Todos" is selected (empty string), clinics
  // with a null builder_id are also included; otherwise they are hidden.
  const visibleClinics = useMemo(() => {
    if (!filters.builderId) return facets.clinics;
    return facets.clinics.filter((c) => c.builder_id === filters.builderId);
  }, [facets.clinics, filters.builderId]);

  const onBuilderChange = (raw: string) => {
    const next: FiltersState = {
      ...filters,
      builderId: raw === ALL_BUILDERS ? "" : raw,
      // Reset clinic when builder changes so the two filters stay consistent.
      clinicId: "",
    };
    onChange(next);
  };

  const onClinicChange = (raw: string) => {
    onChange({ ...filters, clinicId: raw === ALL_CLINICS ? "" : raw });
  };

  const onChannelChange = (raw: string) => {
    onChange({ ...filters, channel: raw === ALL_CHANNELS ? "" : raw });
  };

  const onReviewedChange = (raw: string) => {
    onChange({ ...filters, reviewed: raw === ALL_REVIEWED ? "" : raw });
  };

  return (
    <div className="flex flex-col gap-3 md:flex-row md:flex-wrap md:items-end">
      {showBuilderClinic && (
        <>
          <div className="space-y-1">
            <label className="text-[11px] font-medium text-muted-foreground">Builder</label>
            <select
              data-testid="filter-builder"
              value={filters.builderId || ALL_BUILDERS}
              onChange={(e) => onBuilderChange(e.target.value)}
              className="h-9 rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-accent w-56"
            >
              <option value={ALL_BUILDERS}>Todos os builders</option>
              {facets.builders.map((b) => (
                <option key={b.id} value={b.id}>{b.name}</option>
              ))}
            </select>
          </div>

          <div className="space-y-1">
            <label className="text-[11px] font-medium text-muted-foreground">Clínica</label>
            <select
              data-testid="filter-clinic"
              value={filters.clinicId || ALL_CLINICS}
              onChange={(e) => onClinicChange(e.target.value)}
              className="h-9 rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-accent w-56"
            >
              <option value={ALL_CLINICS}>Todas as clínicas</option>
              {visibleClinics.map((c) => (
                <option key={c.id} value={c.id}>{c.name}</option>
              ))}
            </select>
          </div>
        </>
      )}

      <div className="space-y-1">
        <label className="text-[11px] font-medium text-muted-foreground">
          {t("rlhf_audit_filter_channel_label")}
        </label>
        <select
          data-testid="filter-channel"
          value={filters.channel || ALL_CHANNELS}
          onChange={(e) => onChannelChange(e.target.value)}
          className="h-9 rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-accent w-44"
        >
          <option value={ALL_CHANNELS}>{t("rlhf_audit_filter_channel_all")}</option>
          <option value="web">{t("rlhf_audit_filter_channel_web")}</option>
          <option value="whatsapp">{t("rlhf_audit_filter_channel_whatsapp")}</option>
          <option value="unknown">{t("rlhf_audit_filter_channel_unknown")}</option>
        </select>
      </div>

      {showReviewed && (
        <div className="space-y-1">
          <label className="text-[11px] font-medium text-muted-foreground">
            {t("rlhf_audit_filter_reviewed_label")}
          </label>
          <select
            data-testid="filter-reviewed"
            value={filters.reviewed || ALL_REVIEWED}
            onChange={(e) => onReviewedChange(e.target.value)}
            className="h-9 rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-accent w-44"
          >
            <option value={ALL_REVIEWED}>{t("rlhf_audit_filter_reviewed_all")}</option>
            <option value="true">{t("rlhf_audit_filter_reviewed_yes")}</option>
            <option value="false">{t("rlhf_audit_filter_reviewed_no")}</option>
          </select>
        </div>
      )}

      <DateRangePicker
        startDate={filters.startDate}
        endDate={filters.endDate}
        onStartChange={(v) => onChange({ ...filters, startDate: v })}
        onEndChange={(v) => onChange({ ...filters, endDate: v })}
      />

      <label className="flex items-center gap-2 h-9 text-xs text-foreground cursor-pointer select-none">
        <input
          data-testid="filter-only-accepted"
          type="checkbox"
          className="h-4 w-4 rounded border-border accent-violet-500"
          checked={filters.onlyAccepted}
          onChange={(e) => onChange({ ...filters, onlyAccepted: e.target.checked })}
        />
        Somente aceitas
      </label>
    </div>
  );
}

// ── Written feedback section ─────────────────────────────────────────────────

function formatAcceptedAt(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
  } catch {
    return "—";
  }
}

function TraceLink({ entry }: { entry: RatingEntry }) {
  const { t } = useI18n();
  const traceId = entry.trace_event_id ?? null;
  const traceTenant = entry.trace_tenant_id ?? null;
  if (!traceId || !traceTenant) {
    return (
      <span
        data-testid={`trace-missing-${entry.id}`}
        className="text-[10px] text-muted-foreground italic"
      >
        {t("rlhf_audit_no_trace")}
      </span>
    );
  }
  const href = `/traces/${traceId}?tenant_id=${encodeURIComponent(traceTenant)}`;
  return (
    <Link
      href={href}
      data-testid={`trace-link-${entry.id}`}
      className="inline-flex items-center gap-1 text-[10px] font-medium text-sky-500 hover:text-sky-400"
    >
      <ExternalLink className="h-3 w-3" />
      {t("rlhf_audit_view_trace")}
    </Link>
  );
}

function ReviewPanel({
  entry,
  onSubmitted,
}: {
  entry: RatingEntry;
  onSubmitted: () => Promise<void>;
}) {
  const { t } = useI18n();
  const { toast } = useToast();
  const history = entry.review_history ?? [];
  const currentlyReviewed = history[0]?.revisado ?? false;
  const [observation, setObservation] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);

  const submit = async (nextRevisado: boolean) => {
    setSubmitting(true);
    try {
      const raw = observation.trim();
      // Marking REVISADO with no written observation is an explicitly
      // legitimate verdict per the DESIGN — we pass null to the API in that
      // case rather than an empty string.
      const body = JSON.stringify({
        revisado: nextRevisado,
        observacao: raw.length > 0 ? raw : null,
      });
      await customFetch(`/v1/rlhf/ratings/${entry.id}/review`, {
        method: "POST",
        body,
      });
      setObservation("");
      toast({ title: t("rlhf_audit_review_ok") });
      // Reload from the authoritative summary so review_history and
      // reviewed_count refresh in a single round-trip.
      await onSubmitted();
    } catch {
      toast({ title: t("rlhf_audit_review_err"), variant: "destructive" });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      data-testid={`review-panel-${entry.id}`}
      className="rounded-md border border-border bg-muted/30 p-3 space-y-2"
    >
      <p className="text-[10px] font-bold uppercase tracking-widest text-muted-foreground">
        {t("rlhf_audit_parecer_title")}
      </p>
      <div className="space-y-1">
        <label
          htmlFor={`observation-${entry.id}`}
          className="text-[10px] font-medium text-muted-foreground"
        >
          {t("rlhf_audit_observation_label")}
        </label>
        <textarea
          id={`observation-${entry.id}`}
          data-testid={`observation-${entry.id}`}
          value={observation}
          onChange={(e) => setObservation(e.target.value)}
          placeholder={t("rlhf_audit_observation_placeholder")}
          rows={2}
          className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-accent"
        />
      </div>
      <div className="flex flex-wrap gap-2">
        <Button
          data-testid={`revisado-toggle-${entry.id}`}
          size="sm"
          className={`text-[10px] h-7 gap-1 px-2 ${
            currentlyReviewed
              ? "bg-slate-500 hover:bg-slate-400 text-white"
              : "bg-sky-600 hover:bg-sky-500 text-white"
          }`}
          onClick={() => void submit(!currentlyReviewed)}
          disabled={submitting}
        >
          {submitting
            ? t("rlhf_audit_review_submitting")
            : currentlyReviewed
              ? t("rlhf_audit_unmark_reviewed")
              : t("rlhf_audit_mark_reviewed")}
        </Button>
      </div>

      <div data-testid={`review-history-${entry.id}`} className="space-y-1">
        <p className="text-[10px] font-bold uppercase tracking-widest text-muted-foreground pt-1">
          {t("rlhf_audit_history_title")}
        </p>
        {history.length === 0 ? (
          <p className="text-[10px] text-muted-foreground italic">
            {t("rlhf_audit_history_empty")}
          </p>
        ) : (
          <ul className="space-y-1">
            {history.map((row, idx) => (
              <li
                key={`${entry.id}-hist-${idx}`}
                data-testid={`review-history-row-${entry.id}-${idx}`}
                className="text-[10px] text-foreground/80 bg-background rounded border border-border px-2 py-1"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-semibold">
                    {row.revisado
                      ? t("rlhf_audit_history_marked_reviewed")
                      : t("rlhf_audit_history_unmarked_reviewed")}
                  </span>
                  <span className="text-muted-foreground">
                    {formatAcceptedAt(row.revisado_em)}
                  </span>
                </div>
                <div className="text-muted-foreground">
                  {t("rlhf_audit_history_reviewed_by")} {row.revisado_por_email ?? "—"}
                </div>
                {row.observacao && row.observacao.trim().length > 0 && (
                  <div className="italic text-foreground/80 mt-0.5">"{row.observacao}"</div>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function FeedbackEntryCard({
  entry,
  onAction,
  actioningId,
  showSuperControls,
  onReviewSubmitted,
}: {
  entry: RatingEntry;
  onAction: (entryId: string, action: "approve" | "ignore") => void;
  actioningId: string | null;
  showSuperControls: boolean;
  onReviewSubmitted: () => Promise<void>;
}) {
  const acceptable  = hasQualifyingReason(entry);
  const isProcessing = actioningId === entry.id;
  return (
    <Card
      data-testid={`written-entry-${entry.id}`}
      className={`border-border shadow-sm overflow-hidden transition-opacity ${entry.ignored ? "opacity-40" : ""}`}
    >
      <div className="flex items-center justify-between px-4 py-2 bg-slate-50/50 dark:bg-slate-900/50 border-b border-border">
        <div className="flex items-center gap-2">
          <RatingBadge rating={entry.rating} />
          <span className="text-[11px] text-muted-foreground">
            {RATING_META[entry.rating]?.label}
          </span>
        </div>
        <span className="text-[10px] text-muted-foreground">
          {entry.ts ? new Date(entry.ts).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" }) : ""}
        </span>
      </div>

      <CardContent className="pt-3 pb-3 space-y-3">
        <div>
          <p className="text-[10px] font-bold uppercase tracking-widest text-muted-foreground mb-1">
            Opinião do cliente
          </p>
          <p className="text-xs italic text-foreground/80 bg-muted rounded px-2 py-1.5 border border-border">
            "{entry.reason}"
          </p>
        </div>

        <div>
          <p className="text-[10px] font-bold uppercase tracking-widest text-muted-foreground mb-1">
            Opinião da IA
          </p>
          <AiAnalysisCell entry={entry} />
        </div>

        <div className="grid gap-2 sm:grid-cols-2">
          <div>
            <p className="text-[10px] font-bold uppercase tracking-widest text-muted-foreground mb-0.5">
              Clínica / Usuário
            </p>
            <p className="text-[11px] font-semibold text-foreground">
              {entry.subject_clinic_name ?? entry.subject_tenant_id.slice(0, 8) + "..."}
            </p>
            <p className="text-[10px] text-muted-foreground">
              {entry.submitted_by ?? "—"}
              {entry.builder_name ? ` · Builder: ${entry.builder_name}` : ""}
            </p>
          </div>

          <div className="flex flex-col items-start sm:items-end gap-1.5">
            {showSuperControls && <TraceLink entry={entry} />}
            {showSuperControls && (entry.approved_for_training ? (
              <>
                <Badge className="text-[9px] bg-violet-500/15 text-violet-400 border border-violet-500/30 flex items-center gap-1 w-fit py-0">
                  <Zap className="h-2.5 w-2.5" /> No Dataset
                </Badge>
                <p
                  data-testid={`accepted-meta-${entry.id}`}
                  className="text-[10px] text-muted-foreground text-right"
                >
                  Aceito por {entry.accepted_by_email ?? "—"}
                  <br />em {formatAcceptedAt(entry.accepted_at)}
                </p>
              </>
            ) : entry.ignored ? (
              <span className="text-[10px] text-muted-foreground">Ignorado</span>
            ) : (
              <>
                {acceptable && (
                  <Button
                    data-testid={`accept-btn-${entry.id}`}
                    size="sm"
                    className="text-[10px] h-7 bg-violet-600 hover:bg-violet-500 text-white gap-1 px-2"
                    onClick={() => onAction(entry.id, "approve")}
                    disabled={isProcessing}
                  >
                    <Zap className="h-2.5 w-2.5" />
                    Aceitar e gravar opinião
                  </Button>
                )}
                <Button
                  data-testid={`ignore-btn-${entry.id}`}
                  size="sm"
                  variant="outline"
                  className="text-[10px] h-7 gap-1 px-2 text-muted-foreground"
                  onClick={() => onAction(entry.id, "ignore")}
                  disabled={isProcessing}
                >
                  Ignorar
                </Button>
              </>
            ))}
          </div>
        </div>

        {showSuperControls && (
          <ReviewPanel entry={entry} onSubmitted={onReviewSubmitted} />
        )}
      </CardContent>
    </Card>
  );
}

function WrittenFeedbackList({
  entries,
  onAction,
  actioningId,
  showSuperControls,
  onReviewSubmitted,
}: {
  entries: RatingEntry[];
  onAction: (entryId: string, action: "approve" | "ignore") => void;
  actioningId: string | null;
  showSuperControls: boolean;
  onReviewSubmitted: () => Promise<void>;
}) {
  const written = entries.filter((e) => e.reason && e.reason.trim().length > 0);

  return (
    <Card className="border-border shadow-sm">
      <CardContent className="pt-4 pb-4 space-y-3">
        <div className="flex items-center gap-2">
          <MessageSquare className="h-4 w-4 text-violet-400" />
          <h2 className="text-sm font-bold uppercase tracking-widest text-foreground">
            Feedbacks escritos
          </h2>
        </div>
        <p className="text-xs text-muted-foreground">
          Opiniões com texto do cliente. Somente aqui é possível aceitar e gravar a opinião no dataset.
        </p>

        {written.length === 0 ? (
          <div
            data-testid="written-empty"
            className="text-center py-8 text-sm text-muted-foreground"
          >
            Nenhum feedback escrito para os filtros atuais.
          </div>
        ) : (
          <div className="space-y-3">
            {written.map((entry) => (
              <FeedbackEntryCard
                key={entry.id}
                entry={entry}
                onAction={onAction}
                actioningId={actioningId}
                showSuperControls={showSuperControls}
                onReviewSubmitted={onReviewSubmitted}
              />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ── Panel ────────────────────────────────────────────────────────────────────

const EMPTY_RESPONSE: RatingsResponse = {
  summary: { total: 0, average: 0, accepted_count: 0, distribution: { "1": 0, "2": 0, "3": 0, "4": 0, "5": 0 } },
  facets: { builders: [], clinics: [] },
  entries: [],
};

export function RatingsPanel() {
  const { toast } = useToast();
  const { isSuperAdmin, isInvestor } = useAuth();
  const initial = defaultDates();
  const [filters, setFilters] = useState<FiltersState>({
    builderId: "",
    clinicId: "",
    startDate: initial.startDate,
    endDate: initial.endDate,
    onlyAccepted: false,
    channel: "",
    reviewed: "",
  });
  const [data, setData] = useState<RatingsResponse>(EMPTY_RESPONSE);
  const [loading, setLoading] = useState(true);
  const [actioningId, setActioningId] = useState<string | null>(null);

  const load = async (current: FiltersState) => {
    setLoading(true);
    try {
      const qs = new URLSearchParams({
        start_date: current.startDate,
        end_date: current.endDate,
        limit: "100",
        only_with_reason: "true",
      });
      if (current.builderId) qs.set("builder_id", current.builderId);
      if (current.clinicId)  qs.set("tenant_id", current.clinicId);
      if (current.onlyAccepted) qs.set("only_accepted", "true");
      if (current.channel) qs.set("channel", current.channel);
      // Backend silently ignores `reviewed` for non-super callers, but per the
      // wish the UI never even offers the dropdown to investors.
      if (current.reviewed) qs.set("reviewed", current.reviewed);
      const response = await customFetch<RatingsResponse>(`/v1/rlhf/ratings?${qs}`);
      // Defensive: if the backend ever returns a partial payload, keep shape stable.
      setData({
        summary: response?.summary ?? EMPTY_RESPONSE.summary,
        facets: response?.facets ?? EMPTY_RESPONSE.facets,
        entries: Array.isArray(response?.entries) ? response.entries : [],
      });
    } catch {
      toast({ title: "Erro ao carregar avaliações", variant: "destructive" });
      setData(EMPTY_RESPONSE);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load(filters);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    filters.builderId,
    filters.clinicId,
    filters.startDate,
    filters.endDate,
    filters.onlyAccepted,
    filters.channel,
    filters.reviewed,
  ]);

  const takeAction = async (entryId: string, action: "approve" | "ignore") => {
    setActioningId(entryId);
    try {
      await customFetch(`/v1/rlhf/ratings/${entryId}`, {
        method: "PATCH",
        body: JSON.stringify({ action }),
      });
      toast({ title: action === "approve" ? "Opinião aceita e gravada no dataset" : "Ignorado" });
      // Re-fetch so summary.accepted_count and accepted_by_email refresh in
      // one round trip — no client-side mutation that would drift from the
      // authoritative summary.
      await load(filters);
    } catch {
      toast({ title: "Erro ao executar ação", variant: "destructive" });
    } finally {
      setActioningId(null);
    }
  };

  return (
    <div className="space-y-6">
      <Filters
        filters={filters}
        onChange={setFilters}
        facets={data.facets}
        showBuilderClinic={isSuperAdmin}
        showReviewed={!isInvestor}
      />

      <KpiCards summary={data.summary} showReviewed={!isInvestor} />

      <DistributionChart summary={data.summary} />

      {loading ? (
        <div className="flex items-center justify-center py-12 gap-2 text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          <span className="text-sm">Carregando avaliações...</span>
        </div>
      ) : (
        <WrittenFeedbackList
          entries={data.entries}
          onAction={takeAction}
          actioningId={actioningId}
          showSuperControls={isSuperAdmin}
          onReviewSubmitted={() => load(filters)}
        />
      )}
    </div>
  );
}

// ── Main Page ────────────────────────────────────────────────────────────────

export default function RlhfAudit() {
  return (
    <div className="space-y-7 max-w-6xl mx-auto">

      {/* Header */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <div className="flex items-center gap-3 mb-2">
            <div className="h-9 w-9 rounded-lg bg-violet-500/15 border border-violet-500/30 flex items-center justify-center">
              <Brain className="h-5 w-5 text-violet-400" />
            </div>
            <div>
              <div className="flex items-center gap-2.5">
                <h1 className="text-2xl font-bold text-foreground tracking-tight">Auditoria RLHF</h1>
              </div>
              <p className="text-xs text-violet-400 font-semibold uppercase tracking-widest">
                Relatório de Alinhamento — RLHF Data Flywheel
              </p>
            </div>
          </div>
          <p className="text-sm text-muted-foreground max-w-2xl">
            Registro centralizado de avaliações de resposta enviadas pelos builders.
            Cada entrada representa um sinal de alinhamento comportamental para fine-tuning do agente.
          </p>
        </div>
      </div>

      {/* Restricted badge */}
      <div className="flex items-center gap-2 px-3 py-2 rounded-md bg-gray-900 border border-black w-fit">
        <ShieldAlert className="h-3.5 w-3.5 text-white shrink-0" />
        <span className="text-xs font-bold text-white">
          Acesso restrito: Somente Master Admin. Contém metodologia proprietária de alinhamento SRM.
        </span>
      </div>

      <RatingsPanel />
    </div>
  );
}
