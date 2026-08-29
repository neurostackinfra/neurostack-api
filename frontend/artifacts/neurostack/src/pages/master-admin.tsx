import { useEffect, useState } from "react";
import { useLocation } from "wouter";
import {
  Shield, Building2, ToggleLeft, ToggleRight,
  Plus, Eye, CheckCircle2, XCircle, Loader2, AlertTriangle,
  Inbox, Paperclip, ChevronDown, ChevronRight, ChevronUp, CheckCheck, UserCheck,
  Phone, TrendingUp, TrendingDown, Download, Search, CalendarRange, RefreshCw,
  RotateCcw, Briefcase, Trash2, Pencil, DollarSign, Info, Receipt, CreditCard, Unlink,
  ExternalLink, Sliders,
} from "lucide-react";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";
import { useAuth } from "@/context/auth";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter,
  DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { useToast } from "@/hooks/use-toast";
import { useI18n } from "@/context/i18n";

// ── Types ─────────────────────────────────────────────────────────────────────

interface SupportTicket {
  id: number;
  clinic_id: number;
  clinic_name: string | null;
  email: string;
  tipo: string;
  mensagem: string;
  filename: string | null;
  file_type: string | null;
  file_data: string | null;
  status: "open" | "closed";
  created_at: string;
}

interface DemoLead {
  id: string;
  name: string;
  email: string;
  company: string;
  phone: string;
  created_at: string;
}

interface AdminStats {
  totalTraces: number;
  totalTokens: number;
  openTickets: number;
  activeClinics: number;
  activeBuilders: number;
  activeManagedClinics: number;
  estimatedMrr: number;
  totalClinics: number;
  totalBuilders: number;
}

interface AdminTimeSeriesPoint {
  date: string;
  count: number;
}

interface AdminAlert {
  type: string;
  severity: "critical" | "warning" | "info";
  clinic_id: number;
  clinic_name: string;
  message: string;
  detected_at: string;
}

interface AdminDashboardData {
  stats: {
    total_traces: number;
    active_pilots: number;
    active_builders: number;
    active_managed_clinics: number;
    estimated_mrr: number;
    safety_flags_7d: number;
    handoff_count_7d: number;
    total_clinics: number;
    active_clinics: number;
    total_builders: number;
    [key: string]: number;
  };
  time_series: AdminTimeSeriesPoint[];
  alerts: AdminAlert[];
}

interface BuilderClinic {
  clinic_id: number;
  clinic_name: string;
  admin_email?: string | null;
  slug: string | null;
  status: "active" | "inactive";
  plan_type: string;
  builder_id: number;
  trace_count: number;
  apis_configured: boolean;
  health_status?: "healthy" | "warning" | "critical";
  last_trace_at?: string | null;
  config_completeness?: number;
}

interface BuilderWithClinics {
  id: number;
  name: string;
  admin_email?: string | null;
  status: "active" | "inactive";
  subscription_status: string;
  created_at: string;
  clinics: BuilderClinic[];
}

const ACTIVE_BUILDER_SUBSCRIPTION_STATUSES = new Set(["trial", "pilot", "active"]);

function builderCountsAsActive(builder: BuilderWithClinics): boolean {
  return builder.status === "active" && ACTIVE_BUILDER_SUBSCRIPTION_STATUSES.has(builder.subscription_status);
}

interface ClinicSlot {
  clinic_id: number;
  clinic_name: string;
  agent_name: string;
  slug: string | null;
  status: "active" | "inactive";
  plan_type: "pilot" | "demo" | "admin";
  trace_count: number;
  apis_configured: boolean;
  users: { id: number; email: string; role: string }[];
  health_status?: "healthy" | "warning" | "critical";
  last_trace_at?: string | null;
  config_completeness?: number;
}

interface ClinicRowData {
  clinic_id: number;
  clinic_name: string;
  admin_email?: string | null;
  slug: string | null;
  status: "active" | "inactive";
  plan_type: string;
  health_status?: "healthy" | "warning" | "critical";
  last_trace_at?: string | null;
  config_completeness?: number;
}

interface Transaction {
  id: string;
  builder_id: string;
  builder_name: string;
  amount_cents: number;
  method: string;
  cycle: string;
  plan: string;
  coupon_code: string | null;
  external_id: string | null;
  event_type: string;
  created_at: string | null;
}

interface BuilderSubscription {
  id: string;
  name: string;
  subscription_plan: string | null;
  subscription_status: string;
  billing_cycle: string;
  updated_at: string | null;
}

// ── API helpers ───────────────────────────────────────────────────────────────

function useAdminToken() {
  const { clinicId } = useAuth();
  void clinicId;
  const raw = localStorage.getItem("ns_auth");
  try {
    return (JSON.parse(raw ?? "{}") as { token?: string }).token ?? "";
  } catch {
    return "";
  }
}

function authHeaders(token: string) {
  return { "Content-Type": "application/json", Authorization: `Bearer ${token}` };
}

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

// ── Status Badge ──────────────────────────────────────────────────────────────

function StatusBadge({ status }: { status: string }) {
  const { t } = useI18n();
  return status === "active" ? (
    <Badge className="bg-emerald-500/15 text-emerald-400 border-emerald-500/30 text-[10px]">
      <CheckCircle2 className="h-3 w-3 mr-1" /> {t("admin_status_active")}
    </Badge>
  ) : (
    <Badge variant="outline" className="text-slate-400 border-slate-600 text-[10px]">
      <XCircle className="h-3 w-3 mr-1" /> {t("admin_status_inactive")}
    </Badge>
  );
}

function PlanBadge({ plan }: { plan: string }) {
  const { t } = useI18n();
  const styles: Record<string, string> = {
    clinic: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
    pilot: "bg-blue-500/15 text-blue-400 border-blue-500/30",
    demo:  "bg-amber-500/15 text-amber-400 border-amber-500/30",
    admin: "bg-violet-500/15 text-violet-400 border-violet-500/30",
  };
  return (
    <Badge className={`${styles[plan] ?? styles.pilot} text-[10px]`}>
      {plan === "pilot" || plan === "clinic" ? t("admin_plan_clinic") : plan.toUpperCase()}
    </Badge>
  );
}

// ── Relative Time Helper ─────────────────────────────────────────────────────

function relativeTime(iso: string | null | undefined, locale: string): string {
  const formatter = new Intl.RelativeTimeFormat(locale === "pt" ? "pt-BR" : "en", { numeric: "auto" });
  if (!iso) return locale === "pt" ? "nunca" : "never";
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 0) return formatter.format(0, "minute");
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return formatter.format(0, "minute");
  if (mins < 60) return formatter.format(-mins, "minute");
  const hours = Math.floor(mins / 60);
  if (hours < 24) return formatter.format(-hours, "hour");
  const days = Math.floor(hours / 24);
  return formatter.format(-days, "day");
}

// ── Alerts Section ───────────────────────────────────────────────────────────

function AlertsSection({ alerts }: { alerts: AdminAlert[] }) {
  const { t, locale } = useI18n();
  if (alerts.length === 0) return null;
  return (
    <Card className="border-border">
      <CardHeader className="pb-2">
        <div className="flex items-center gap-2">
          <AlertTriangle className="h-4 w-4 text-amber-400" />
          <CardTitle className="text-sm font-semibold">{t("admin_alerts_title")}</CardTitle>
          <Badge className="bg-rose-500/15 text-rose-400 border-rose-500/30 text-[10px]">
            {alerts.length}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="pt-0 space-y-1.5">
        {alerts.map((alert, i) => {
          const alertMessage = alert.type === "no_traces"
            ? t("admin_demo_alert_no_traces")
            : alert.type === "low_config"
              ? t("admin_demo_alert_low_config")
              : alert.message;
          const iconColor =
            alert.severity === "critical" ? "text-rose-400" :
            alert.severity === "warning"  ? "text-amber-400" :
            "text-blue-400";
          const Icon =
            alert.severity === "critical" ? AlertTriangle :
            alert.severity === "warning"  ? AlertTriangle :
            Info;
          return (
            <div
              key={`${alert.clinic_id}-${alert.type}-${i}`}
              className="flex items-start gap-2.5 rounded-md border border-border/50 px-3 py-2 text-xs"
            >
              <Icon className={`h-3.5 w-3.5 shrink-0 mt-0.5 ${iconColor}`} />
              <div className="flex-1 min-w-0">
                <span className="font-medium text-foreground">{alert.clinic_name}</span>
                <span className="text-muted-foreground ml-1.5">{alertMessage}</span>
              </div>
              <span className="text-[10px] text-muted-foreground shrink-0">
                {relativeTime(alert.detected_at, locale)}
              </span>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}

// ── Trace Chart (30-day) ─────────────────────────────────────────────────────

function TraceChart({ data }: { data: AdminTimeSeriesPoint[] }) {
  const { t } = useI18n();
  if (data.length === 0) return null;
  return (
    <Card className="border-border">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">{t("admin_chart_traces_title")}</CardTitle>
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={220}>
          <LineChart data={data}>
            <XAxis
              dataKey="date"
              tickFormatter={(v: string) => { const p = v.split("-"); return `${p[2]}/${p[1]}`; }}
              stroke="#64748b"
              fontSize={10}
            />
            <YAxis stroke="#64748b" fontSize={10} width={40} />
            <Tooltip
              contentStyle={{ background: "#1e293b", border: "1px solid #334155", borderRadius: 8, fontSize: 12 }}
              labelFormatter={(v: string) => { const p = v.split("-"); return `${p[2]}/${p[1]}/${p[0]}`; }}
            />
            <Line
              type="monotone"
              dataKey="count"
              stroke="#818cf8"
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 4, fill: "#818cf8" }}
            />
          </LineChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}

// ── Create Pilot Dialog ───────────────────────────────────────────────────────

function CreatePilotDialog({ token, onCreated }: { token: string; onCreated: () => Promise<void> }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { toast } = useToast();

  const handleCreate = async () => {
    if (!name || !email || !password) {
      setError(t("admin_create_required"));
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/v1/admin/clinics`, {
        method: "POST",
        headers: authHeaders(token),
        body: JSON.stringify({ clinic_name: name, admin_email: email, admin_password: password, plan_type: "pilot" }),
      });
      const data = await res.json() as { ok?: boolean; error?: string; id?: string; clinic_id?: string };
      if (!res.ok) { setError((data as { detail?: string; error?: string }).detail ?? (data as { error?: string }).error ?? t("admin_create_err_generic")); return; }
      const newId = data.id ?? data.clinic_id ?? "-";
      toast({ title: t("admin_create_ok_title"), description: `${t("admin_create_ok_desc_prefix")} "${name}" ID ${newId}.` });
      setOpen(false);
      setName(""); setEmail(""); setPassword("");
      await onCreated();
    } catch {
      setError(t("admin_create_err_network"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm" className="gap-2">
          <Plus className="h-4 w-4" /> {t("admin_create_btn")}
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("admin_create_title")}</DialogTitle>
          <DialogDescription>
            {t("admin_create_desc")}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3 py-2">
          <div className="space-y-1">
            <Label>{t("admin_create_name_label")}</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("admin_create_name_ph")} />
          </div>
          <div className="space-y-1">
            <Label>{t("admin_create_email_label")}</Label>
            <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder={t("admin_create_email_ph")} />
          </div>
          <div className="space-y-1">
            <Label>{t("admin_create_pass_label")}</Label>
            <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="••••••••" />
          </div>
          {error && (
            <p className="text-sm text-destructive flex items-center gap-1.5">
              <AlertTriangle className="h-3.5 w-3.5" /> {error}
            </p>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>{t("admin_create_cancel")}</Button>
          <Button onClick={handleCreate} disabled={loading}>
            {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : t("admin_create_submit")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ── Clinic Row (list-style, expandable) ─────────────────────────────────────

function HealthDot({ status }: { status?: "healthy" | "warning" | "critical" }) {
  const color =
    status === "critical" ? "bg-rose-500" :
    status === "warning"  ? "bg-amber-500" :
    status === "healthy"  ? "bg-emerald-500" :
    "bg-slate-500";
  return <span className={`h-2 w-2 rounded-full shrink-0 ${color}`} title={status ?? "sem dados"} />;
}

function ClinicRow({
  clinic, isExpanded, onToggleExpand, canManage,
  onRename, onToggleStatus, onUnlink, onDelete,
  onViewTraces, onAccessPanel, onConfigure,
}: {
  clinic: ClinicRowData;
  isExpanded: boolean;
  onToggleExpand: () => void;
  canManage: boolean;
  onRename?: () => void;
  onToggleStatus: () => void;
  onUnlink?: () => void;
  onDelete?: () => void;
  onViewTraces: () => void;
  onAccessPanel: () => void;
  onConfigure: () => void;
}) {
  const { t, locale } = useI18n();
  return (
    <Collapsible open={isExpanded} onOpenChange={onToggleExpand}>
      <div className={`rounded-md border transition-colors ${clinic.status === "active" ? "bg-background/60 border-border/40" : "bg-muted/20 border-dashed border-slate-700 opacity-60"}`}>
        <div className="flex items-center gap-2.5 px-2.5 py-2 text-xs">
          <HealthDot status={clinic.health_status} />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="font-medium truncate">{clinic.clinic_name}</span>
              {clinic.admin_email && <span className="text-[10px] text-muted-foreground truncate">{clinic.admin_email}</span>}
              {clinic.slug && <span className="text-muted-foreground font-mono text-[10px]">/{clinic.slug}</span>}
            </div>
            <p className="text-[10px] text-muted-foreground mt-0.5">
              {t("admin_health_last_trace")}: {relativeTime(clinic.last_trace_at, locale)}
            </p>
          </div>
          <PlanBadge plan={clinic.plan_type} />
          <StatusBadge status={clinic.status} />
          {canManage && (
            <div className="flex items-center gap-0.5 shrink-0">
              {onRename && (
                <Button variant="ghost" size="sm" className="h-6 w-6 p-0 text-muted-foreground hover:text-foreground" title="Renomear" onClick={onRename}>
                  <Pencil className="h-3 w-3" />
                </Button>
              )}
              <Button
                variant="ghost" size="sm"
                className={`h-6 w-6 p-0 ${clinic.status === "active" ? "text-emerald-400 hover:text-emerald-300" : "text-muted-foreground hover:text-foreground"}`}
                title={clinic.status === "active" ? "Desativar clínica" : "Ativar clínica"}
                onClick={onToggleStatus}
              >
                {clinic.status === "active" ? <ToggleRight className="h-3.5 w-3.5" /> : <ToggleLeft className="h-3.5 w-3.5" />}
              </Button>
              {onUnlink && (
                <Button variant="ghost" size="sm" className="h-6 w-6 p-0 text-muted-foreground hover:text-amber-400" title="Desvincular do builder" onClick={onUnlink}>
                  <Unlink className="h-3 w-3" />
                </Button>
              )}
              {onDelete && (
                <Button variant="ghost" size="sm" className="h-6 w-6 p-0 text-muted-foreground hover:text-destructive" title="Excluir clínica" onClick={onDelete}>
                  <Trash2 className="h-3 w-3" />
                </Button>
              )}
            </div>
          )}
          <CollapsibleTrigger asChild>
            <Button variant="ghost" size="sm" className="h-6 w-6 p-0 text-muted-foreground shrink-0">
              {isExpanded ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
            </Button>
          </CollapsibleTrigger>
        </div>
        <CollapsibleContent className="px-2.5 pb-2.5 space-y-2.5 border-t border-border/40 pt-2.5">
          <div className="space-y-1">
            <div className="flex items-center justify-between text-[11px]">
              <span className="text-muted-foreground">{t("admin_health_config_completeness")}</span>
              <span className="font-medium">{clinic.config_completeness ?? 0}%</span>
            </div>
            <Progress value={clinic.config_completeness ?? 0} className="h-1.5" />
          </div>
          <div className="flex items-center gap-1.5">
            <Button variant="outline" size="sm" className="h-7 text-[11px] gap-1.5 flex-1" onClick={onViewTraces}>
              <Eye className="h-3.5 w-3.5" /> {t("admin_view_traces")}
            </Button>
            <Button variant="outline" size="sm" className="h-7 text-[11px] gap-1.5 flex-1" onClick={onAccessPanel}>
              <ExternalLink className="h-3.5 w-3.5" /> {t("admin_access_panel")}
            </Button>
            <Button variant="outline" size="sm" className="h-7 text-[11px] gap-1.5 flex-1" onClick={onConfigure}>
              <Sliders className="h-3.5 w-3.5" /> {t("builder_configure")}
            </Button>
          </div>
        </CollapsibleContent>
      </div>
    </Collapsible>
  );
}

// ── Demo mock clinics (investor short-circuit — no API call) ──────────────────

const DEMO_CLINICS: ClinicSlot[] = [
  {
    clinic_id: 1, clinic_name: "Clínica Odonto Prime", agent_name: "Odonto Prime",
    slug: "clinica-1", status: "active", plan_type: "pilot", trace_count: 12_500,
    apis_configured: true,
    users: [{ id: 10, email: "pilot1@clinic.com", role: "clinic_admin" }],
    health_status: "healthy", last_trace_at: "2026-05-02T08:30:00Z", config_completeness: 95,
  },
  {
    clinic_id: 2, clinic_name: "Instituto Face SP", agent_name: "Face SP",
    slug: "clinica-2", status: "active", plan_type: "pilot", trace_count: 5_200,
    apis_configured: true,
    users: [{ id: 11, email: "pilot2@clinic.com", role: "clinic_admin" }],
    health_status: "healthy", last_trace_at: "2026-05-01T22:15:00Z", config_completeness: 88,
  },
  {
    clinic_id: 3, clinic_name: "Estética Rio Norte", agent_name: "Rio Norte",
    slug: "clinica-3", status: "active", plan_type: "pilot", trace_count: 1_100,
    apis_configured: false,
    users: [{ id: 12, email: "pilot3@clinic.com", role: "clinic_admin" }],
    health_status: "warning", last_trace_at: "2026-04-28T14:00:00Z", config_completeness: 42,
  },
  {
    clinic_id: 5, clinic_name: "Lumina Derma Clinic", agent_name: "Lumina Derma",
    slug: "clinica-4", status: "active", plan_type: "pilot", trace_count: 8_400,
    apis_configured: true,
    users: [{ id: 13, email: "pilot4@clinic.com", role: "clinic_admin" }],
    health_status: "critical", last_trace_at: null, config_completeness: 70,
  },
];

// ── Demo mock builders (investor short-circuit) ───────────────────────────────

const DEMO_BUILDERS: BuilderWithClinics[] = [
  {
    id: 1, name: "Builder Alfa", status: "active", subscription_status: "active", created_at: "2026-01-15T10:00:00Z",
    clinics: [
      { clinic_id: 8,  clinic_name: "Clínica Alfa 1", slug: "alfa-1", status: "active", plan_type: "pilot", builder_id: 1, trace_count: 3200, apis_configured: true, health_status: "healthy", last_trace_at: "2026-05-02T07:00:00Z", config_completeness: 100 },
      { clinic_id: 9,  clinic_name: "Clínica Alfa 2", slug: "alfa-2", status: "active", plan_type: "pilot", builder_id: 1, trace_count: 1800, apis_configured: true, health_status: "healthy", last_trace_at: "2026-05-01T18:30:00Z", config_completeness: 85 },
      { clinic_id: 10, clinic_name: "Clínica Alfa 3", slug: "alfa-3", status: "active", plan_type: "pilot", builder_id: 1, trace_count: 950,  apis_configured: false, health_status: "warning", last_trace_at: "2026-04-27T10:00:00Z", config_completeness: 35 },
    ],
  },
  {
    id: 2, name: "Builder Beta", status: "active", subscription_status: "trial", created_at: "2026-02-01T10:00:00Z",
    clinics: [
      { clinic_id: 11, clinic_name: "Clínica Beta 1", slug: "beta-1", status: "active", plan_type: "pilot", builder_id: 2, trace_count: 2100, apis_configured: true, health_status: "healthy", last_trace_at: "2026-05-02T06:45:00Z", config_completeness: 92 },
      { clinic_id: 12, clinic_name: "Clínica Beta 2", slug: "beta-2", status: "active", plan_type: "pilot", builder_id: 2, trace_count: 890,  apis_configured: false, health_status: "critical", last_trace_at: null, config_completeness: 20 },
    ],
  },
];

// ── Demo dashboard data (investor short-circuit) ────────────────────────────

const DEMO_DASHBOARD: AdminDashboardData = {
  stats: {
    total_traces: 27200,
    active_pilots: DEMO_CLINICS.filter((clinic) => clinic.status === "active").length,
    active_builders: DEMO_BUILDERS.filter(builderCountsAsActive).length,
    active_managed_clinics: DEMO_BUILDERS.flatMap((builder) => builder.clinics).filter((clinic) => clinic.status === "active").length,
    estimated_mrr: 0,
    safety_flags_7d: 2,
    handoff_count_7d: 48,
    total_clinics: DEMO_CLINICS.length + DEMO_BUILDERS.reduce((count, builder) => count + builder.clinics.length, 0),
    active_clinics: DEMO_CLINICS.filter((clinic) => clinic.status === "active").length + DEMO_BUILDERS.reduce(
      (count, builder) => count + builder.clinics.filter((clinic) => clinic.status === "active").length,
      0,
    ),
    total_builders: DEMO_BUILDERS.length,
  },
  time_series: Array.from({ length: 31 }, (_, i) => {
    const d = new Date("2026-04-02");
    d.setDate(d.getDate() + i);
    return { date: d.toISOString().slice(0, 10), count: 600 + Math.floor(Math.random() * 400) };
  }),
  alerts: [
    { type: "no_traces", severity: "critical", clinic_id: 5, clinic_name: "Lumina Derma Clinic", message: "No traces in 7 days", detected_at: "2026-05-01T12:00:00Z" },
    { type: "low_config", severity: "warning", clinic_id: 3, clinic_name: "Estética Rio Norte", message: "Config completeness below 50%", detected_at: "2026-04-30T09:00:00Z" },
  ],
};

// ── Master Admin Page ─────────────────────────────────────────────────────────

export default function MasterAdmin() {
  const { t, locale } = useI18n();
  const { isSuperAdmin, isInvestor, setViewingClinicId } = useAuth();
  const [, navigate] = useLocation();
  const token = useAdminToken();
  const { toast } = useToast();

  // Demo users get instant static data — never call the restricted API
  const [clinics, setClinics] = useState<ClinicSlot[]>(isInvestor ? DEMO_CLINICS : []);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(isInvestor);          // already "loaded" for investors
  const [tickets, setTickets] = useState<SupportTicket[]>([]);
  const [ticketsLoaded, setTicketsLoaded] = useState(isInvestor);
  const [expandedTicket, setExpandedTicket] = useState<number | null>(null);
  const [ticketSearch, setTicketSearch] = useState("");
  const [ticketDateStart, setTicketDateStart] = useState("");
  const [ticketDateEnd, setTicketDateEnd] = useState("");
  const [ticketStatusFilter, setTicketStatusFilter] = useState<"all" | "open" | "closed">("all");
  const [leads, setLeads] = useState<DemoLead[]>([]);
  const [leadsLoaded, setLeadsLoaded] = useState(false);
  const [leadStart, setLeadStart] = useState("");
  const [leadEnd, setLeadEnd] = useState("");
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [statsLoaded, setStatsLoaded] = useState(false);
  const [dashboard, setDashboard] = useState<AdminDashboardData | null>(isInvestor ? DEMO_DASHBOARD : null);
  const [dashboardLoaded, setDashboardLoaded] = useState(isInvestor);

  // Alert filter state
  const [alertSeverityFilter, setAlertSeverityFilter] = useState<string>("all");
  const [alertClinicFilter, setAlertClinicFilter] = useState<string>("all");

  // Builders state
  const [builders, setBuilders] = useState<BuilderWithClinics[]>(isInvestor ? DEMO_BUILDERS : []);
  const [buildersLoaded, setBuildersLoaded] = useState(isInvestor);
  const [builderSearchQuery, setBuilderSearchQuery] = useState("");
  const [expandedBuilderId, setExpandedBuilderId] = useState<number | null>(null);
  const [expandedClinicId, setExpandedClinicId] = useState<number | null>(null);
  const [newBuilderName, setNewBuilderName] = useState("");
  const [newBuilderEmail, setNewBuilderEmail] = useState("");
  const [newBuilderPassword, setNewBuilderPassword] = useState("");
  const [addingBuilder, setAddingBuilder] = useState(false);
  const [editingBuilderId, setEditingBuilderId] = useState<number | null>(null);
  const [editBuilderName, setEditBuilderName] = useState("");
  const [editBuilderEmail, setEditBuilderEmail] = useState("");
  const [editBuilderPassword, setEditBuilderPassword] = useState("");
  const [editingClinicId, setEditingClinicId] = useState<number | null>(null);
  const [editClinicName, setEditClinicName] = useState("");
  const [editClinicEmail, setEditClinicEmail] = useState("");
  const [editClinicPassword, setEditClinicPassword] = useState("");
  const [addingClinicToBuilderId, setAddingClinicToBuilderId] = useState<number | null>(null);
  const [newClinicName, setNewClinicName] = useState("");
  const [newClinicEmail, setNewClinicEmail] = useState("");
  const [newClinicPassword, setNewClinicPassword] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<{ type: "builder" | "clinic"; id: number; name: string } | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [linkingClinicToBuilderId, setLinkingClinicToBuilderId] = useState<number | null>(null);
  const [selectedLinkClinicId, setSelectedLinkClinicId] = useState("");

  // Transactions state
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [builderSubscriptions, setBuilderSubscriptions] = useState<BuilderSubscription[]>([]);
  const [transactionsLoaded, setTransactionsLoaded] = useState(false);
  const [showTransactionsSection, setShowTransactionsSection] = useState(false);

  // Coupons state
  const [coupons, setCoupons] = useState<Array<{ id: string; code: string; discount_percent: number; max_uses: number | null; uses_count: number; expires_at: string | null; is_active: boolean; created_at: string | null }>>([]);
  const [couponsLoaded, setCouponsLoaded] = useState(false);
  const [showCouponsSection, setShowCouponsSection] = useState(false);
  const [newCouponCode, setNewCouponCode] = useState("");
  const [newCouponDiscount, setNewCouponDiscount] = useState("");
  const [newCouponMaxUses, setNewCouponMaxUses] = useState("");
  const [newCouponExpires, setNewCouponExpires] = useState("");
  const [addingCoupon, setAddingCoupon] = useState(false);

  const fetchClinics = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/v1/admin/clinics`, { headers: authHeaders(token) });
      if (!res.ok) { setError(t("admin_err_clinics")); return; }
      const data = await res.json() as ClinicSlot[];
      setClinics(data);
    } catch {
      setError(t("admin_err_network"));
    } finally {
      setLoading(false);
      setLoaded(true);
    }
  };

  const fetchBuilders = async () => {
    try {
      const res = await fetch(`/v1/admin/builders`, { headers: authHeaders(token) });
      if (!res.ok) return;
      setBuilders(await res.json() as BuilderWithClinics[]);
    } catch { /* silent */ }
    finally { setBuildersLoaded(true); }
  };

  const fetchCoupons = async () => {
    try {
      const res = await fetch(`/v1/admin/coupons`, { headers: authHeaders(token) });
      if (!res.ok) return;
      setCoupons(await res.json());
      setCouponsLoaded(true);
    } catch { /* silent */ }
  };

  const fetchTransactions = async () => {
    try {
      const [txRes, subRes] = await Promise.all([
        fetch(`/v1/admin/transactions`, { headers: authHeaders(token) }),
        fetch(`/v1/admin/builders/subscriptions`, { headers: authHeaders(token) }),
      ]);
      if (txRes.ok) setTransactions(await txRes.json() as Transaction[]);
      if (subRes.ok) setBuilderSubscriptions(await subRes.json() as BuilderSubscription[]);
      setTransactionsLoaded(true);
    } catch { /* silent */ }
  };

  const handleAddCoupon = async () => {
    if (!newCouponCode.trim() || !newCouponDiscount) return;
    setAddingCoupon(true);
    try {
      const body: Record<string, unknown> = {
        code: newCouponCode.trim().toUpperCase(),
        discount_percent: parseInt(newCouponDiscount, 10),
      };
      if (newCouponMaxUses) body.max_uses = parseInt(newCouponMaxUses, 10);
      if (newCouponExpires) body.expires_at = new Date(newCouponExpires).toISOString();
      const res = await fetch(`/v1/admin/coupons`, {
        method: "POST", headers: authHeaders(token),
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({})) as { detail?: string };
        toast({ title: err.detail ?? "Erro ao criar cupom", variant: "destructive" });
        return;
      }
      setNewCouponCode(""); setNewCouponDiscount(""); setNewCouponMaxUses(""); setNewCouponExpires("");
      await fetchCoupons();
      toast({ title: "Cupom criado com sucesso!" });
    } catch {
      toast({ title: "Erro de rede", variant: "destructive" });
    } finally {
      setAddingCoupon(false);
    }
  };

  const handleDeactivateCoupon = async (couponId: string) => {
    try {
      const res = await fetch(`/v1/admin/coupons/${couponId}`, {
        method: "PATCH", headers: authHeaders(token),
      });
      if (res.ok) {
        await fetchCoupons();
        toast({ title: "Cupom desativado" });
      }
    } catch { /* silent */ }
  };

  const handleAddBuilder = async () => {
    if (!newBuilderName.trim() || !newBuilderEmail.trim() || !newBuilderPassword.trim()) return;
    setAddingBuilder(true);
    try {
      const res = await fetch(`/v1/admin/builders`, {
        method: "POST", headers: authHeaders(token),
        body: JSON.stringify({ name: newBuilderName.trim(), admin_email: newBuilderEmail.trim(), admin_password: newBuilderPassword }),
      });
      if (!res.ok) { const err = await res.json().catch(() => ({})) as { detail?: string }; toast({ title: err.detail ?? "Erro ao criar builder", variant: "destructive" }); return; }
      setNewBuilderName(""); setNewBuilderEmail(""); setNewBuilderPassword("");
      await Promise.all([fetchBuilders(), refreshKpis()]);
      toast({ title: "Builder criado com sucesso!" });
    } catch {
      toast({ title: "Erro de rede", variant: "destructive" });
    } finally {
      setAddingBuilder(false);
    }
  };

  const handleToggleBuilder = async (id: number, newStatus: "active" | "inactive") => {
    if (isInvestor) { toast({ title: "Modo Visualização: ação desabilitada." }); return; }
    try {
      const res = await fetch(`/v1/admin/builders/${id}`, {
        method: "PATCH", headers: authHeaders(token),
        body: JSON.stringify({ status: newStatus }),
      });
      if (!res.ok) { toast({ title: "Erro ao alterar status", variant: "destructive" }); return; }
      setBuilders((prev) => prev.map((b) => b.id === id ? { ...b, status: newStatus } : b));
      await refreshKpis();
      toast({ title: newStatus === "active" ? "Builder ativado" : "Builder desativado", description: `ID ${id}` });
    } catch {
      toast({ title: "Erro de rede", variant: "destructive" });
    }
  };

  const handleSaveBuilder = async (id: number) => {
    if (!editBuilderName.trim()) return;
    const payload: Record<string, string> = { name: editBuilderName.trim() };
    if (editBuilderEmail.trim()) payload.email = editBuilderEmail.trim();
    if (editBuilderPassword.trim()) payload.password = editBuilderPassword;
    try {
      const res = await fetch(`/v1/admin/builders/${id}`, {
        method: "PATCH", headers: authHeaders(token),
        body: JSON.stringify(payload),
      });
      if (!res.ok) { const err = await res.json().catch(() => ({})) as { detail?: string }; toast({ title: err.detail ?? "Erro ao atualizar builder", variant: "destructive" }); return; }
      setBuilders((prev) => prev.map((b) => b.id === id ? { ...b, name: editBuilderName.trim(), admin_email: editBuilderEmail.trim() || b.admin_email } : b));
      setEditingBuilderId(null);
      setEditBuilderPassword("");
      toast({ title: "Builder atualizado com sucesso." });
    } catch {
      toast({ title: "Erro de rede", variant: "destructive" });
    }
  };

  const handleSaveClinic = async (clinicId: number) => {
    if (!editClinicName.trim()) return;
    const payload: Record<string, string> = { clinic_name: editClinicName.trim() };
    if (editClinicEmail.trim()) payload.email = editClinicEmail.trim();
    if (editClinicPassword.trim()) payload.password = editClinicPassword;
    try {
      const res = await fetch(`/v1/admin/clinics/${clinicId}`, {
        method: "PATCH", headers: authHeaders(token),
        body: JSON.stringify(payload),
      });
      if (!res.ok) { const err = await res.json().catch(() => ({})) as { detail?: string }; toast({ title: err.detail ?? "Erro ao atualizar clínica", variant: "destructive" }); return; }
      setBuilders((prev) => prev.map((b) => ({
        ...b,
        clinics: b.clinics.map((c) => c.clinic_id === clinicId ? { ...c, clinic_name: editClinicName.trim(), admin_email: editClinicEmail.trim() || c.admin_email } : c),
      })));
      setEditingClinicId(null);
      setEditClinicPassword("");
      toast({ title: "Clínica atualizada com sucesso." });
    } catch {
      toast({ title: "Erro de rede", variant: "destructive" });
    }
  };

  const handleDeleteBuilder = (id: number, name: string) => {
    setDeleteTarget({ type: "builder", id, name });
  };

  const handleAddClinicToBuilder = async (builderId: number) => {
    if (!newClinicName.trim() || !newClinicEmail.trim() || !newClinicPassword.trim()) {
      toast({ title: "Preencha nome, e-mail e senha.", variant: "destructive" }); return;
    }
    try {
      const res = await fetch(`/v1/admin/builders/${builderId}/clinics`, {
        method: "POST", headers: authHeaders(token),
        body: JSON.stringify({ clinic_name: newClinicName.trim(), admin_email: newClinicEmail.trim(), admin_password: newClinicPassword }),
      });
      if (!res.ok) {
        const err = await res.json() as { error?: string; detail?: unknown };
        const msg = err.error ?? (typeof err.detail === "string" ? err.detail : Array.isArray(err.detail) ? (err.detail as Array<{ msg?: string }>).map((e) => e.msg).join("; ") : "Erro ao criar clínica");
        toast({ title: msg, variant: "destructive" }); return;
      }
      setNewClinicName(""); setNewClinicEmail(""); setNewClinicPassword("");
      setAddingClinicToBuilderId(null);
      await Promise.all([fetchBuilders(), fetchClinics(), refreshKpis()]);
      toast({ title: "Clínica criada com sucesso!" });
    } catch {
      toast({ title: "Erro de rede", variant: "destructive" });
    }
  };

  const handleLinkClinic = async (builderId: number) => {
    if (!selectedLinkClinicId) return;
    try {
      const res = await fetch(`/v1/admin/builders/${builderId}/link-clinic`, {
        method: "POST", headers: authHeaders(token),
        body: JSON.stringify({ clinic_id: selectedLinkClinicId }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({})) as { detail?: string };
        toast({ title: err.detail ?? "Erro ao vincular clínica", variant: "destructive" });
        return;
      }
      setSelectedLinkClinicId("");
      setLinkingClinicToBuilderId(null);
      await Promise.all([fetchBuilders(), fetchClinics(), refreshKpis()]);
      toast({ title: "Clínica vinculada com sucesso!" });
    } catch {
      toast({ title: "Erro de rede", variant: "destructive" });
    }
  };

  const handleUnlinkClinic = async (builderId: number, clinicId: number) => {
    if (isInvestor) { toast({ title: "Modo Visualização: ação desabilitada." }); return; }
    try {
      const res = await fetch(`/v1/admin/builders/${builderId}/unlink-clinic`, {
        method: "POST", headers: authHeaders(token),
        body: JSON.stringify({ clinic_id: clinicId }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({})) as { detail?: string };
        toast({ title: err.detail ?? "Erro ao desvincular clínica", variant: "destructive" });
        return;
      }
      await Promise.all([fetchBuilders(), fetchClinics(), refreshKpis()]);
      toast({ title: "Clínica desvinculada com sucesso!" });
    } catch {
      toast({ title: "Erro de rede", variant: "destructive" });
    }
  };

  const handleConfirmDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    const { type, id, name } = deleteTarget;
    const url = type === "builder" ? `/v1/admin/builders/${id}` : `/v1/admin/clinics/${id}`;
    try {
      const res = await fetch(url, { method: "DELETE", headers: authHeaders(token) });
      if (!res.ok) {
        const err = await res.json().catch(() => ({})) as { detail?: string };
        toast({ title: err.detail ?? `Erro ao excluir ${type === "builder" ? "builder" : "clínica"}`, variant: "destructive" });
        return;
      }
      if (type === "builder") {
        setBuilders((prev) => prev.filter((b) => b.id !== id));
      } else {
        setClinics((prev) => prev.filter((c) => c.clinic_id !== id));
        setBuilders((prev) => prev.map((b) => ({ ...b, clinics: b.clinics.filter((c) => c.clinic_id !== id) })));
      }
      await refreshKpis();
      toast({ title: `${type === "builder" ? "Builder" : "Clínica"} "${name}" excluído(a).` });
    } catch {
      toast({ title: "Erro de rede", variant: "destructive" });
    } finally {
      setDeleting(false);
      setDeleteTarget(null);
    }
  };

  const fetchTickets = async () => {
    try {
      const res = await fetch(`/v1/support/tickets`, { headers: authHeaders(token) });
      if (!res.ok) return;
      setTickets(await res.json() as SupportTicket[]);
    } catch { /* silent */ }
    finally { setTicketsLoaded(true); }
  };

  const fetchLeads = async () => {
    try {
      const res = await fetch(`/v1/demo-leads`, { headers: authHeaders(token) });
      if (!res.ok) return;
      setLeads(await res.json() as DemoLead[]);
    } catch { /* silent */ }
    finally { setLeadsLoaded(true); }
  };

  const fetchStats = async () => {
    try {
      const res = await fetch(`/v1/admin/stats`, { headers: authHeaders(token) });
      if (!res.ok) { setStats(null); return; }
      const raw = await res.json() as Record<string, unknown>;
      setStats({
        totalTraces: (raw.total_traces ?? raw.totalTraces ?? 0) as number,
        totalTokens: (raw.total_tokens ?? raw.totalTokens ?? 0) as number,
        openTickets: (raw.open_tickets ?? raw.openTickets ?? 0) as number,
        activeClinics: (raw.active_clinics ?? raw.activeClinics ?? 0) as number,
        activeBuilders: (raw.active_builders ?? raw.activeBuilders ?? 0) as number,
        activeManagedClinics: (raw.active_managed_clinics ?? raw.activeManagedClinics ?? 0) as number,
        estimatedMrr: (raw.estimated_mrr ?? raw.estimatedMrr ?? 0) as number,
        totalClinics: (raw.total_clinics ?? raw.totalClinics ?? 0) as number,
        totalBuilders: (raw.total_builders ?? raw.totalBuilders ?? 0) as number,
      });
    } catch { setStats(null); }
    finally { setStatsLoaded(true); }
  };

  const fetchDashboard = async () => {
    try {
      const res = await fetch(`/v1/admin/dashboard`, { headers: authHeaders(token) });
      if (!res.ok) { setDashboard(null); return; }
      setDashboard(await res.json() as AdminDashboardData);
    } catch { setDashboard(null); }
    finally { setDashboardLoaded(true); }
  };

  const refreshKpis = async () => {
    await Promise.all([fetchStats(), fetchDashboard()]);
  };

  // Only real admins hit the network
  useEffect(() => {
    if (isSuperAdmin && !loaded && !loading) void fetchClinics();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isSuperAdmin, loaded, loading]);

  useEffect(() => {
    if (isSuperAdmin && !buildersLoaded) void fetchBuilders();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isSuperAdmin, buildersLoaded]);

  useEffect(() => {
    if (isSuperAdmin && !ticketsLoaded) void fetchTickets();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isSuperAdmin, ticketsLoaded]);

  useEffect(() => {
    if (!leadsLoaded && isSuperAdmin) void fetchLeads();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [leadsLoaded, isSuperAdmin]);

  useEffect(() => {
    if (!statsLoaded && isSuperAdmin) void fetchStats();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statsLoaded, isSuperAdmin]);

  useEffect(() => {
    if (isSuperAdmin && !dashboardLoaded) void fetchDashboard();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dashboardLoaded, isSuperAdmin]);

  if (!isSuperAdmin && !isInvestor) {
    return (
      <div className="flex items-center justify-center h-64 text-muted-foreground">
        <Shield className="h-8 w-8 mr-2" /> {t("admin_restricted")}
      </div>
    );
  }

  const handleToggleClinicStatus = async (id: number, currentStatus: "active" | "inactive") => {
    if (isInvestor) { toast({ title: "Modo Visualização: ação desabilitada." }); return; }
    const newStatus = currentStatus === "active" ? "inactive" : "active";
    try {
      const res = await fetch(`/v1/admin/clinics/${id}/status`, {
        method: "PATCH",
        headers: authHeaders(token),
        body: JSON.stringify({ status: newStatus }),
      });
      if (!res.ok) {
        toast({ title: t("admin_toggle_err_title"), description: t("admin_toggle_err"), variant: "destructive" });
        return;
      }
      setClinics((prev) => prev.map((c) => c.clinic_id === id ? { ...c, status: newStatus } : c));
      setBuilders((prev) => prev.map((b) => ({
        ...b,
        clinics: b.clinics.map((c) => c.clinic_id === id ? { ...c, status: newStatus } : c),
      })));
      await refreshKpis();
      toast({
        title: newStatus === "active" ? t("admin_toggle_ok_on") : t("admin_toggle_ok_off"),
        description: `ID ${id}`,
      });
    } catch {
      toast({ title: t("admin_create_err_network"), variant: "destructive" });
    }
  };

  const handleDeleteClinic = (id: number, name: string) => {
    setDeleteTarget({ type: "clinic", id, name });
  };

  const handleViewTraces = (id: number) => {
    setViewingClinicId(String(id));
    navigate("/traces");
  };

  const handleConfigureClinic = (id: number) => {
    setViewingClinicId(String(id));
    navigate("/config");
  };

  const handleAccessClinicPanel = (id: number) => {
    setViewingClinicId(String(id));
    navigate("/");
  };

  // ── Lead filtering & export ────────────────────────────────────────────────

  const filteredLeads = leads.filter((l) => {
    const d = new Date(l.created_at);
    if (leadStart && d < new Date(leadStart)) return false;
    if (leadEnd) {
      const end = new Date(leadEnd);
      end.setHours(23, 59, 59, 999);
      if (d > end) return false;
    }
    return true;
  });

  const displayedLeads = filteredLeads.slice(0, 10);

  const exportCSV = () => {
    const rows = [
      ["Nome", "E-mail", "Empresa / Fundo", "Telefone", "Data"],
      ...filteredLeads.map((l) => [
        l.name,
        l.email || "",
        l.company,
        l.phone,
        new Date(l.created_at).toLocaleString("pt-BR"),
      ]),
    ];
    const csv = rows
      .map((r) => r.map((v) => `"${String(v).replace(/"/g, '""')}"`).join(","))
      .join("\n");
    const blob = new Blob(["\uFEFF" + csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "neurostack_leads_export.csv";
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleUpdateTicketStatus = async (id: number, newStatus: "open" | "closed") => {
    try {
      const res = await fetch(`/v1/support/tickets/${id}/status`, {
        method: "PATCH",
        headers: authHeaders(token),
        body: JSON.stringify({ status: newStatus }),
      });
      if (!res.ok) throw new Error();
      setTickets((prev) => prev.map((tk) => tk.id === id ? { ...tk, status: newStatus } : tk));
      toast({
        title: newStatus === "closed" ? t("admin_ticket_closed_toast") : "Ticket reaberto",
        description: `#${id}`,
      });
    } catch {
      toast({ title: t("admin_ticket_err_toast"), variant: "destructive" });
    }
  };

  // ── Ticket filtering & export ──────────────────────────────────────────────

  const filteredTickets = tickets.filter((tk) => {
    if (ticketStatusFilter !== "all" && tk.status !== ticketStatusFilter) return false;
    if (ticketSearch) {
      const q = ticketSearch.toLowerCase();
      const nameMatch = (tk.clinic_name ?? "").toLowerCase().includes(q);
      const emailMatch = tk.email.toLowerCase().includes(q);
      const msgMatch = tk.mensagem.toLowerCase().includes(q);
      if (!nameMatch && !emailMatch && !msgMatch) return false;
    }
    if (ticketDateStart || ticketDateEnd) {
      const d = new Date(tk.created_at);
      if (ticketDateStart && d < new Date(ticketDateStart)) return false;
      if (ticketDateEnd) {
        const end = new Date(ticketDateEnd);
        end.setHours(23, 59, 59, 999);
        if (d > end) return false;
      }
    }
    return true;
  });

  const displayedTickets = filteredTickets.slice(0, 10);

  const exportTicketsCSV = () => {
    const rows = [
      ["#", "Clínica", "E-mail", "Tipo", "Mensagem", "Status", "Data"],
      ...filteredTickets.map((tk) => [
        String(tk.id),
        tk.clinic_name ?? `Clínica ${tk.clinic_id}`,
        tk.email,
        tk.tipo,
        tk.mensagem.replace(/\n/g, " "),
        tk.status === "open" ? "Pendente" : "Resolvido",
        new Date(tk.created_at).toLocaleString("pt-BR"),
      ]),
    ];
    const csv = rows
      .map((r) => r.map((v) => `"${String(v).replace(/"/g, '""')}"`).join(","))
      .join("\n");
    const blob = new Blob(["\uFEFF" + csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "neurostack_tickets_export.csv";
    a.click();
    URL.revokeObjectURL(url);
  };

  // Platform KPIs come from the dashboard stats endpoint (server-computed truth),
  // not from the client-side clinics/builders lists, which only hold what's been fetched.
  const totalClinics   = dashboard?.stats.total_clinics ?? stats?.totalClinics ?? "…";
  const activeClinics  = dashboard?.stats.active_clinics ?? stats?.activeClinics ?? "…";
  const totalBuilders  = dashboard?.stats.total_builders ?? stats?.totalBuilders ?? "…";
  const activeBuilders = dashboard?.stats.active_builders ?? stats?.activeBuilders ?? "…";
  const totalTraces   = isInvestor ? "27.2K" : (stats ? stats.totalTraces.toLocaleString("pt-BR") : "...");
  const tokenDisplay  = isInvestor ? "2.4M" : (stats ? `${(stats.totalTokens / 1_000_000).toFixed(1)}M` : "...");
  const openTicketCnt = isInvestor ? 0 : (ticketsLoaded ? tickets.filter((t) => t.status === "open").length : (stats?.openTickets ?? 0));

  // Trend: compare last 7 days vs previous 7 days from time_series
  const traceTrend = (() => {
    const ts = dashboard?.time_series;
    if (!ts || ts.length < 14) return null;
    const last7 = ts.slice(-7).reduce((s, p) => s + p.count, 0);
    const prev7 = ts.slice(-14, -7).reduce((s, p) => s + p.count, 0);
    if (prev7 === 0) return null;
    const pct = Math.round(((last7 - prev7) / prev7) * 100);
    return { direction: pct >= 0 ? "up" as const : "down" as const, pct: Math.abs(pct) };
  })();

  // Clinics already linked to a builder — used to build the "unlinked" list and the link-picker
  const linkedClinicIds = new Set(builders.flatMap((b) => b.clinics.map((c) => c.clinic_id)));
  const unlinkedClinics = clinics.filter((c) => !linkedClinicIds.has(c.clinic_id));

  const filteredBuilders = builderSearchQuery.trim()
    ? builders.filter((b) =>
        b.name.toLowerCase().includes(builderSearchQuery.trim().toLowerCase()) ||
        (b.admin_email ?? "").toLowerCase().includes(builderSearchQuery.trim().toLowerCase())
      )
    : builders;

  // ── Alert filtering (client-side) ────────────────────────────────────────
  const allAlerts = dashboard?.alerts ?? [];
  const alertClinicNames = Array.from(new Set(allAlerts.map((a) => a.clinic_name))).sort();
  const filteredAlerts = allAlerts.filter((a) => {
    if (alertSeverityFilter !== "all" && a.severity !== alertSeverityFilter) return false;
    if (alertClinicFilter !== "all" && a.clinic_name !== alertClinicFilter) return false;
    return true;
  });

  return (
    <>
    <div className="max-w-6xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Shield className="h-5 w-5 text-violet-400" />
            <h1 className="text-xl font-bold">Master Admin</h1>
            {isInvestor && (
              <span className="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-400 border border-amber-500/30">
                Modo Visualização
              </span>
            )}
          </div>
          <p className="text-sm text-muted-foreground mt-0.5">
            {t("admin_infra_desc")} · {totalClinics} clínicas · {activeClinics} {t("admin_pilots_label")}
          </p>
        </div>
        {isInvestor ? (
          <Button
            size="sm"
            className="gap-2 opacity-50 cursor-not-allowed"
            onClick={() => toast({ title: "Modo Visualização: Ações de administrador desabilitadas nesta conta." })}
          >
            <Plus className="h-4 w-4" /> {t("admin_create_btn")}
          </Button>
        ) : (
          <CreatePilotDialog
            token={token}
            onCreated={async () => { await Promise.all([fetchClinics(), refreshKpis()]); }}
          />
        )}
      </div>

      {/* Platform KPIs: builders + clinics, total created vs. currently active */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Card className="border-border">
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-1.5 mb-1">
              <Briefcase className="h-3.5 w-3.5 text-indigo-400" />
              <p className="text-[11px] text-muted-foreground">{t("admin_kpi_total_builders")}</p>
            </div>
            <p className="text-2xl font-bold text-foreground">{totalBuilders}</p>
          </CardContent>
        </Card>
        <Card className="border-border">
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-1.5 mb-1">
              <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />
              <p className="text-[11px] text-muted-foreground">{t("admin_kpi_active_builders")}</p>
            </div>
            <p className="text-2xl font-bold text-emerald-400">{activeBuilders}</p>
          </CardContent>
        </Card>
        <Card className="border-border">
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-1.5 mb-1">
              <Building2 className="h-3.5 w-3.5 text-blue-400" />
              <p className="text-[11px] text-muted-foreground">{t("admin_stat_total")}</p>
            </div>
            <p className="text-2xl font-bold text-foreground">{totalClinics}</p>
          </CardContent>
        </Card>
        <Card className="border-border">
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-1.5 mb-1">
              <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />
              <p className="text-[11px] text-muted-foreground">{t("admin_stat_active")}</p>
            </div>
            <p className="text-2xl font-bold text-emerald-400">{activeClinics}</p>
          </CardContent>
        </Card>
      </div>

      {/* Real-data metrics row (super_admin only) */}
      {isSuperAdmin && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <Card className="border-border bg-gradient-to-br from-emerald-950/20 to-transparent">
            <CardContent className="pt-4 pb-3">
              <div className="flex items-center gap-1.5 mb-1">
                <TrendingUp className="h-3.5 w-3.5 text-emerald-400" />
                <p className="text-[11px] text-muted-foreground">Total de Traces (Clínicas)</p>
              </div>
              <div className="flex items-center gap-2">
                <p className="text-2xl font-bold text-emerald-400">{totalTraces}</p>
                {traceTrend && (
                  <span className={`flex items-center gap-0.5 text-[11px] font-medium ${traceTrend.direction === "up" ? "text-emerald-400" : "text-rose-400"}`}>
                    {traceTrend.direction === "up" ? <TrendingUp className="h-3 w-3" /> : <TrendingDown className="h-3 w-3" />}
                    {traceTrend.pct}%
                  </span>
                )}
              </div>
            </CardContent>
          </Card>
          <Card className="border-border bg-gradient-to-br from-blue-950/20 to-transparent">
            <CardContent className="pt-4 pb-3">
              <div className="flex items-center gap-1.5 mb-1">
                <DollarSign className="h-3.5 w-3.5 text-blue-400" />
                <p className="text-[11px] text-muted-foreground">Consumo API (Mês)</p>
              </div>
              <p className="text-2xl font-bold text-blue-400">
                {tokenDisplay} <span className="text-sm font-medium">Tokens</span>
              </p>
            </CardContent>
          </Card>
          <Card className="border-border bg-gradient-to-br from-rose-950/20 to-transparent">
            <CardContent className="pt-4 pb-3">
              <div className="flex items-center gap-1.5 mb-1">
                <Inbox className="h-3.5 w-3.5 text-rose-400" />
                <p className="text-[11px] text-muted-foreground">Tickets Abertos</p>
              </div>
              <p className="text-2xl font-bold text-rose-400">{openTicketCnt}</p>
            </CardContent>
          </Card>
          <Card className="border-border bg-gradient-to-br from-amber-950/20 to-transparent">
            <CardContent className="pt-4 pb-3">
              <div className="flex items-center gap-1.5 mb-1">
                <UserCheck className="h-3.5 w-3.5 text-amber-400" />
                <p className="text-[11px] text-muted-foreground">Leads Capturados</p>
              </div>
              <p className="text-2xl font-bold text-amber-400">{leads.length}</p>
            </CardContent>
          </Card>
        </div>
      )}

      {/* ── Alerts ──────────────────────────────────────────────────────── */}
      {dashboard && allAlerts.length > 0 && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <select
              value={alertSeverityFilter}
              onChange={(e) => setAlertSeverityFilter(e.target.value)}
              className="h-8 rounded-md border border-border bg-background px-2 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-accent"
            >
              <option value="all">{t("admin_filter_severity")}: {t("admin_filter_all")}</option>
              <option value="critical">{t("admin_health_dot_critical")}</option>
              <option value="warning">{t("admin_health_dot_warning")}</option>
              <option value="info">Info</option>
            </select>
            <select
              value={alertClinicFilter}
              onChange={(e) => setAlertClinicFilter(e.target.value)}
              className="h-8 rounded-md border border-border bg-background px-2 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-accent"
            >
              <option value="all">{t("admin_filter_clinic")}: {t("admin_filter_all")}</option>
              {alertClinicNames.map((name) => (
                <option key={name} value={name}>{name}</option>
              ))}
            </select>
          </div>
          {filteredAlerts.length > 0 ? (
            <AlertsSection alerts={filteredAlerts} />
          ) : (
            <Card className="border-border">
              <CardContent className="py-6 text-center text-xs text-muted-foreground">
                {t("admin_alerts_empty")}
              </CardContent>
            </Card>
          )}
        </div>
      )}

      {/* ── 30-day Trace Chart ────────────────────────────────────────────── */}
      {dashboard && dashboard.time_series.length > 0 && (
        <TraceChart data={dashboard.time_series} />
      )}

      {/* ── Gestão de Builders ──────────────────────────────────────────── */}
      <Card className="border-border">
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between flex-wrap gap-2">
            <div className="flex items-center gap-2">
              <Briefcase className="h-4 w-4 text-violet-400" />
              <CardTitle className="text-sm font-semibold">Gestão de Builders</CardTitle>
              <span className="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-violet-500/15 text-violet-400 border border-violet-500/30">
                {builders.filter(builderCountsAsActive).length} ativo{builders.filter(builderCountsAsActive).length !== 1 ? "s" : ""}
              </span>
            </div>
            {isSuperAdmin && (
              <div className="flex items-center gap-2">
                <Input
                  value={newBuilderName}
                  onChange={(e) => setNewBuilderName(e.target.value)}
                  placeholder="Nome do builder..."
                  className="h-8 text-xs w-36"
                  onKeyDown={(e) => { if (e.key === "Enter") void handleAddBuilder(); }}
                />
                <Input
                  value={newBuilderEmail}
                  onChange={(e) => setNewBuilderEmail(e.target.value)}
                  placeholder="Email admin..."
                  className="h-8 text-xs w-40"
                  type="email"
                  onKeyDown={(e) => { if (e.key === "Enter") void handleAddBuilder(); }}
                />
                <Input
                  value={newBuilderPassword}
                  onChange={(e) => setNewBuilderPassword(e.target.value)}
                  placeholder="Senha (min 6)..."
                  className="h-8 text-xs w-32"
                  type="password"
                  onKeyDown={(e) => { if (e.key === "Enter") void handleAddBuilder(); }}
                />
                <Button size="sm" className="h-8 gap-1.5 text-xs" onClick={() => void handleAddBuilder()} disabled={addingBuilder || !newBuilderName.trim() || !newBuilderEmail.trim() || !newBuilderPassword.trim()}>
                  {addingBuilder ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
                  Novo Builder
                </Button>
              </div>
            )}
          </div>
          <CardDescription className="text-xs">
            Revendedores e agências.
          </CardDescription>
        </CardHeader>
        <CardContent className="pt-0 space-y-2">
          {builders.length > 0 && (
            <div className="relative mb-1">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
              <Input
                value={builderSearchQuery}
                onChange={(e) => setBuilderSearchQuery(e.target.value)}
                placeholder="Buscar builder por nome ou e-mail..."
                className="h-8 text-xs pl-8"
              />
            </div>
          )}
          {builders.length === 0 ? (
            <div className="flex flex-col items-center py-8 text-muted-foreground gap-2">
              <Briefcase className="h-8 w-8 opacity-25" />
              <p className="text-xs">Nenhum builder cadastrado ainda.</p>
            </div>
          ) : filteredBuilders.length === 0 ? (
            <div className="flex flex-col items-center py-8 text-muted-foreground gap-2">
              <Search className="h-8 w-8 opacity-25" />
              <p className="text-xs">Nenhum builder encontrado para &quot;{builderSearchQuery}&quot;.</p>
            </div>
          ) : (
            filteredBuilders.map((builder) => {
              const isExpanded = expandedBuilderId === builder.id;
              const isEditing = editingBuilderId === builder.id;
              const isAddingClinic = addingClinicToBuilderId === builder.id;
              const builderActiveClinics = builder.clinics.filter((c) => c.status === "active").length;
              const totalClinicCount = builder.clinics.length;


              return (
                <div key={builder.id} className={`rounded-lg border transition-all ${builder.status === "active" ? "border-border" : "border-dashed border-slate-700 opacity-60"}`}>
                  {/* Builder row header */}
                  <div className="flex items-center gap-2 px-3 py-2.5">
                    <button
                      type="button"
                      className="flex items-center gap-1.5 text-muted-foreground hover:text-foreground transition-colors"
                      onClick={() => setExpandedBuilderId(isExpanded ? null : builder.id)}
                    >
                      {isExpanded
                        ? <ChevronDown className="h-3.5 w-3.5 shrink-0" />
                        : <ChevronRight className="h-3.5 w-3.5 shrink-0" />}
                    </button>

                    {isEditing ? (
                      <div className="flex items-center gap-1.5 flex-1 min-w-0">
                        <Input
                          value={editBuilderName}
                          onChange={(e) => setEditBuilderName(e.target.value)}
                          className="h-7 text-xs w-32"
                          placeholder="Nome"
                          onKeyDown={(e) => { if (e.key === "Enter") void handleSaveBuilder(builder.id); if (e.key === "Escape") setEditingBuilderId(null); }}
                          autoFocus
                        />
                        <Input
                          value={editBuilderEmail}
                          onChange={(e) => setEditBuilderEmail(e.target.value)}
                          className="h-7 text-xs w-40"
                          placeholder="Email"
                          type="email"
                          onKeyDown={(e) => { if (e.key === "Enter") void handleSaveBuilder(builder.id); if (e.key === "Escape") setEditingBuilderId(null); }}
                        />
                        <Input
                          value={editBuilderPassword}
                          onChange={(e) => setEditBuilderPassword(e.target.value)}
                          className="h-7 text-xs w-28"
                          placeholder="Nova senha"
                          type="password"
                          onKeyDown={(e) => { if (e.key === "Enter") void handleSaveBuilder(builder.id); if (e.key === "Escape") setEditingBuilderId(null); }}
                        />
                        <Button size="sm" className="h-7 text-xs px-2" onClick={() => void handleSaveBuilder(builder.id)}>Salvar</Button>
                        <Button size="sm" variant="ghost" className="h-7 text-xs px-2" onClick={() => { setEditingBuilderId(null); setEditBuilderPassword(""); }}>✕</Button>
                      </div>
                    ) : (
                      <div className="flex items-center gap-2 flex-1 min-w-0">
                        <span className="font-medium text-sm truncate">{builder.name}</span>
                        {builder.admin_email && <span className="text-[10px] text-muted-foreground truncate">{builder.admin_email}</span>}
                      </div>
                    )}

                    <div className="flex items-center gap-1.5 shrink-0 ml-auto">
                      <span className="text-[11px] text-muted-foreground">
                        {builder.clinics.length} clínica{builder.clinics.length !== 1 ? "s" : ""} · {builderActiveClinics} ativa{builderActiveClinics !== 1 ? "s" : ""}
                      </span>


                      <StatusBadge status={builder.status} />

                      {isSuperAdmin && (
                        <>
                          <Button
                            variant="ghost" size="sm"
                            className="h-7 w-7 p-0 text-muted-foreground hover:text-foreground"
                            title="Renomear"
                            onClick={() => { setEditingBuilderId(builder.id); setEditBuilderName(builder.name); setEditBuilderEmail(builder.admin_email ?? ""); setEditBuilderPassword(""); }}
                          >
                            <Pencil className="h-3.5 w-3.5" />
                          </Button>
                          <Button
                            variant="ghost" size="sm"
                            className={`h-7 w-7 p-0 ${builder.status === "active" ? "text-emerald-400 hover:text-emerald-300" : "text-muted-foreground hover:text-foreground"}`}
                            title={builder.status === "active" ? "Desativar builder" : "Ativar builder"}
                            onClick={() => void handleToggleBuilder(builder.id, builder.status === "active" ? "inactive" : "active")}
                          >
                            {builder.status === "active" ? <ToggleRight className="h-4 w-4" /> : <ToggleLeft className="h-4 w-4" />}
                          </Button>
                          <Button
                            variant="ghost" size="sm"
                            className="h-7 w-7 p-0 text-muted-foreground hover:text-destructive"
                            title="Excluir builder"
                            onClick={() => void handleDeleteBuilder(builder.id, builder.name)}
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </Button>
                        </>
                      )}
                    </div>
                  </div>

                  {/* Expanded: list of clinics */}
                  {isExpanded && (
                    <div className="border-t border-border/40 px-3 py-3 space-y-2 bg-muted/10 rounded-b-lg">
                      <div className="flex items-center justify-between mb-2">
                        <p className="text-[11px] font-medium text-muted-foreground uppercase tracking-wide">
                          Clínicas de {builder.name}
                        </p>
                        {isSuperAdmin && (
                          <div className="flex items-center gap-2">
                            <Button
                              variant="outline" size="sm"
                              className="h-7 text-xs gap-1.5"
                              onClick={() => { setAddingClinicToBuilderId(isAddingClinic ? null : builder.id); setLinkingClinicToBuilderId(null); }}
                            >
                              <Plus className="h-3.5 w-3.5" /> Nova Clínica
                            </Button>
                            <Button
                              variant="outline" size="sm"
                              className="h-7 text-xs gap-1.5"
                              onClick={() => { setLinkingClinicToBuilderId(linkingClinicToBuilderId === builder.id ? null : builder.id); setAddingClinicToBuilderId(null); setSelectedLinkClinicId(""); }}
                            >
                              <Building2 className="h-3.5 w-3.5" /> Vincular Existente
                            </Button>
                          </div>
                        )}
                      </div>

                      {/* Add clinic form */}
                      {isAddingClinic && (
                        <div className="rounded-md border border-dashed border-violet-500/40 bg-violet-500/5 p-3 space-y-2">
                          <p className="text-[11px] font-medium text-violet-400">Nova clínica em {builder.name}</p>
                          <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                            <Input value={newClinicName} onChange={(e) => setNewClinicName(e.target.value)} placeholder="Nome da clínica" className="h-7 text-xs" />
                            <Input value={newClinicEmail} onChange={(e) => setNewClinicEmail(e.target.value)} placeholder="E-mail do admin" className="h-7 text-xs" type="email" />
                            <Input value={newClinicPassword} onChange={(e) => setNewClinicPassword(e.target.value)} placeholder="Senha" className="h-7 text-xs" type="password" />
                          </div>
                          <div className="flex gap-2">
                            <Button size="sm" className="h-7 text-xs gap-1.5" onClick={() => void handleAddClinicToBuilder(builder.id)}>
                              <Plus className="h-3.5 w-3.5" /> Criar Clínica
                            </Button>
                            <Button size="sm" variant="ghost" className="h-7 text-xs" onClick={() => setAddingClinicToBuilderId(null)}>Cancelar</Button>
                          </div>
                        </div>
                      )}

                      {/* Link existing clinic form */}
                      {linkingClinicToBuilderId === builder.id && (() => {
                        const available = unlinkedClinics;
                        return (
                          <div className="rounded-md border border-dashed border-emerald-500/40 bg-emerald-500/5 p-3 space-y-2">
                            <p className="text-[11px] font-medium text-emerald-400">Vincular clínica existente a {builder.name}</p>
                            {available.length === 0 ? (
                              <p className="text-xs text-muted-foreground">Nenhuma clínica disponível para vincular.</p>
                            ) : (
                              <div className="flex items-center gap-2">
                                <select
                                  value={selectedLinkClinicId}
                                  onChange={(e) => setSelectedLinkClinicId(e.target.value)}
                                  className="h-7 text-xs rounded-md border border-border bg-background px-2 flex-1"
                                >
                                  <option value="">Selecione uma clínica...</option>
                                  {available.map((c) => (
                                    <option key={c.clinic_id} value={String(c.clinic_id)}>{c.clinic_name} {c.slug ? `(/${c.slug})` : ""}</option>
                                  ))}
                                </select>
                                <Button size="sm" className="h-7 text-xs gap-1.5" onClick={() => void handleLinkClinic(builder.id)} disabled={!selectedLinkClinicId}>
                                  <Building2 className="h-3.5 w-3.5" /> Vincular
                                </Button>
                                <Button size="sm" variant="ghost" className="h-7 text-xs" onClick={() => setLinkingClinicToBuilderId(null)}>Cancelar</Button>
                              </div>
                            )}
                          </div>
                        );
                      })()}

                      {builder.clinics.length === 0 ? (
                        <p className="text-xs text-muted-foreground text-center py-3">Nenhuma clínica vinculada.</p>
                      ) : (
                        <div className="space-y-1">
                          {builder.clinics.map((c) => {
                            const isEditingClinic = editingClinicId === c.clinic_id;
                            if (isEditingClinic) {
                              return (
                                <div key={c.clinic_id} className="rounded-md px-2.5 py-2 text-xs bg-background/60 border border-border/40">
                                  <div className="flex items-center gap-1.5 flex-wrap">
                                    <Building2 className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
                                    <Input
                                      value={editClinicName}
                                      onChange={(e) => setEditClinicName(e.target.value)}
                                      className="h-7 text-xs w-32"
                                      placeholder="Nome"
                                      autoFocus
                                      onKeyDown={(e) => { if (e.key === "Enter") void handleSaveClinic(c.clinic_id); if (e.key === "Escape") setEditingClinicId(null); }}
                                    />
                                    <Input
                                      value={editClinicEmail}
                                      onChange={(e) => setEditClinicEmail(e.target.value)}
                                      className="h-7 text-xs w-40"
                                      placeholder="Email"
                                      type="email"
                                      onKeyDown={(e) => { if (e.key === "Enter") void handleSaveClinic(c.clinic_id); if (e.key === "Escape") setEditingClinicId(null); }}
                                    />
                                    <Input
                                      value={editClinicPassword}
                                      onChange={(e) => setEditClinicPassword(e.target.value)}
                                      className="h-7 text-xs w-28"
                                      placeholder="Nova senha"
                                      type="password"
                                      onKeyDown={(e) => { if (e.key === "Enter") void handleSaveClinic(c.clinic_id); if (e.key === "Escape") setEditingClinicId(null); }}
                                    />
                                    <Button size="sm" className="h-7 text-xs px-2" onClick={() => void handleSaveClinic(c.clinic_id)}>Salvar</Button>
                                    <Button size="sm" variant="ghost" className="h-7 text-xs px-2" onClick={() => { setEditingClinicId(null); setEditClinicPassword(""); }}>✕</Button>
                                  </div>
                                </div>
                              );
                            }
                            return (
                              <ClinicRow
                                key={c.clinic_id}
                                clinic={c}
                                isExpanded={expandedClinicId === c.clinic_id}
                                onToggleExpand={() => setExpandedClinicId(expandedClinicId === c.clinic_id ? null : c.clinic_id)}
                                canManage={isSuperAdmin}
                                onRename={() => { setEditingClinicId(c.clinic_id); setEditClinicName(c.clinic_name); setEditClinicEmail(c.admin_email ?? ""); setEditClinicPassword(""); }}
                                onToggleStatus={() => void handleToggleClinicStatus(c.clinic_id, c.status)}
                                onUnlink={() => void handleUnlinkClinic(builder.id, c.clinic_id)}
                                onDelete={() => handleDeleteClinic(c.clinic_id, c.clinic_name)}
                                onViewTraces={() => handleViewTraces(c.clinic_id)}
                                onAccessPanel={() => handleAccessClinicPanel(c.clinic_id)}
                                onConfigure={() => handleConfigureClinic(c.clinic_id)}
                              />
                            );
                          })}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })
          )}
        </CardContent>
      </Card>

      {/* Clinics without a builder remain available in the same compact format. */}
      {loading && (
        <div className="flex items-center gap-2 text-muted-foreground py-8 justify-center">
          <Loader2 className="h-5 w-5 animate-spin" /> {t("admin_loading")}
        </div>
      )}
      {error && (
        <div className="flex items-center gap-2 text-destructive py-4">
          <AlertTriangle className="h-4 w-4" /> {error}
        </div>
      )}
      {!loading && loaded && (
        <Card className="border-border">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">{t("admin_unlinked_title")}</CardTitle>
            <CardDescription className="text-xs">{t("admin_unlinked_desc")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-1">
            {unlinkedClinics.length === 0 ? (
              <p className="py-3 text-center text-xs text-muted-foreground">{t("admin_unlinked_empty")}</p>
            ) : unlinkedClinics.map((clinic) => {
              const adminEmail = clinic.users.find((user) => user.role === "clinic_admin")?.email;
              return (
                <ClinicRow
                  key={clinic.clinic_id}
                  clinic={{ ...clinic, admin_email: adminEmail }}
                  isExpanded={expandedClinicId === clinic.clinic_id}
                  onToggleExpand={() => setExpandedClinicId(expandedClinicId === clinic.clinic_id ? null : clinic.clinic_id)}
                  canManage={isSuperAdmin}
                  onToggleStatus={() => void handleToggleClinicStatus(clinic.clinic_id, clinic.status)}
                  onDelete={() => handleDeleteClinic(clinic.clinic_id, clinic.clinic_name)}
                  onViewTraces={() => handleViewTraces(clinic.clinic_id)}
                  onAccessPanel={() => handleAccessClinicPanel(clinic.clinic_id)}
                  onConfigure={() => handleConfigureClinic(clinic.clinic_id)}
                />
              );
            })}
          </CardContent>
        </Card>
      )}

      {/* ── Support Tickets Inbox ─────────────────────────────────────────── */}
      <Card className="border-border">
        <CardHeader className="pb-3">
          {/* Row 1: title + badges + refresh + export */}
          <div className="flex items-center justify-between flex-wrap gap-2">
            <div className="flex items-center gap-2">
              <Inbox className="h-4 w-4 text-foreground/70" />
              <CardTitle className="text-sm font-semibold text-foreground">
                {t("admin_tickets_heading")}
              </CardTitle>
              {tickets.filter((tk) => tk.status === "open").length > 0 && (
                <Badge className="bg-rose-500/15 text-rose-400 border-rose-500/30 text-[10px]">
                  {tickets.filter((tk) => tk.status === "open").length} {t("admin_tickets_open_badge")}
                </Badge>
              )}
              {(ticketSearch || ticketStatusFilter !== "all" || ticketDateStart || ticketDateEnd) && (
                <Badge className="bg-violet-500/15 text-violet-400 border-violet-500/30 text-[10px]">
                  {filteredTickets.length}/{tickets.length} filtrados
                </Badge>
              )}
            </div>
            <div className="flex items-center gap-1.5">
              <Button
                variant="ghost"
                size="sm"
                className="h-7 text-xs gap-1.5 text-muted-foreground"
                onClick={() => { setTicketsLoaded(false); }}
              >
                <RefreshCw className="h-3 w-3" /> {t("admin_tickets_refresh")}
              </Button>
              <Button
                size="sm"
                className="h-7 text-xs gap-1.5 bg-emerald-600 hover:bg-emerald-700 text-white"
                onClick={exportTicketsCSV}
                disabled={filteredTickets.length === 0}
              >
                <Download className="h-3 w-3" /> Exportar CSV
              </Button>
            </div>
          </div>
          <CardDescription className="text-xs">
            {t("admin_tickets_desc")}
          </CardDescription>

          {/* Row 2: search + status filter */}
          <div className="flex flex-wrap gap-2 pt-2">
            <div className="relative flex-1 min-w-[180px]">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground pointer-events-none" />
              <input
                type="text"
                value={ticketSearch}
                onChange={(e) => setTicketSearch(e.target.value)}
                placeholder="Buscar por clínica, e-mail ou palavra-chave..."
                className="w-full h-8 pl-8 pr-3 rounded-md border border-border bg-background text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-accent"
              />
            </div>
            <select
              value={ticketStatusFilter}
              onChange={(e) => setTicketStatusFilter(e.target.value as "all" | "open" | "closed")}
              className="h-8 rounded-md border border-border bg-background px-2 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-accent"
            >
              <option value="all">Todos os status</option>
              <option value="open">Pendente</option>
              <option value="closed">Resolvido</option>
            </select>
          </div>

          {/* Row 3: date range filters */}
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <div className="flex items-center gap-1.5">
              <CalendarRange className="h-3.5 w-3.5 text-muted-foreground" />
              <Label className="text-[11px] text-muted-foreground whitespace-nowrap">De</Label>
              <input
                type="date"
                value={ticketDateStart}
                onChange={(e) => setTicketDateStart(e.target.value)}
                className="h-7 rounded-md border border-border bg-background px-2 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-accent"
              />
            </div>
            <div className="flex items-center gap-1.5">
              <Label className="text-[11px] text-muted-foreground whitespace-nowrap">Até</Label>
              <input
                type="date"
                value={ticketDateEnd}
                onChange={(e) => setTicketDateEnd(e.target.value)}
                className="h-7 rounded-md border border-border bg-background px-2 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-accent"
              />
            </div>
            {(ticketSearch || ticketStatusFilter !== "all" || ticketDateStart || ticketDateEnd) && (
              <button
                type="button"
                onClick={() => { setTicketSearch(""); setTicketStatusFilter("all"); setTicketDateStart(""); setTicketDateEnd(""); }}
                className="flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground transition-colors"
              >
                <RotateCcw className="h-3 w-3" /> Limpar filtros
              </button>
            )}
          </div>
        </CardHeader>
        <CardContent className="pt-0">
          {tickets.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-8 text-center text-muted-foreground gap-2">
              <Inbox className="h-8 w-8 opacity-30" />
              <p className="text-xs font-medium">{t("admin_tickets_empty")}</p>
            </div>
          ) : filteredTickets.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-6 text-center text-muted-foreground gap-2">
              <Search className="h-6 w-6 opacity-30" />
              <p className="text-xs font-medium">Nenhum ticket encontrado com os filtros aplicados.</p>
            </div>
          ) : (
            <div className="space-y-2">
              {displayedTickets.map((ticket) => {
                const isExpanded = expandedTicket === ticket.id;
                const tipoLabel =
                  ticket.tipo === "solicitacao" ? t("admin_ticket_tipo_request")
                  : ticket.tipo === "feedback"   ? t("admin_ticket_tipo_feedback")
                  :                                t("admin_ticket_tipo_financial");
                const tipoColor = ticket.tipo === "financeiro"
                  ? "bg-amber-500/15 text-amber-400 border-amber-500/30"
                  : ticket.tipo === "feedback"
                    ? "bg-blue-500/15 text-blue-400 border-blue-500/30"
                    : "bg-muted text-muted-foreground border-border";
                const dateStr = new Date(ticket.created_at).toLocaleString("pt-BR", {
                  day: "2-digit", month: "2-digit", year: "2-digit",
                  hour: "2-digit", minute: "2-digit",
                });
                return (
                  <div key={ticket.id} className={`rounded-lg border transition-colors ${ticket.status === "closed" ? "border-border/40 opacity-70" : "border-border/70"}`}>
                    {/* Row header */}
                    <button
                      type="button"
                      className="w-full flex items-center gap-3 px-3 py-2.5 text-left hover:bg-muted/20 transition-colors rounded-lg"
                      onClick={() => setExpandedTicket(isExpanded ? null : ticket.id)}
                    >
                      {isExpanded
                        ? <ChevronDown className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
                        : <ChevronRight className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
                      }
                      <Badge variant="outline" className={`text-[10px] shrink-0 ${tipoColor}`}>{tipoLabel}</Badge>
                      <span className="text-xs font-medium text-foreground truncate flex-1">
                        {ticket.clinic_name ?? `Clínica ${ticket.clinic_id}`}
                        <span className="text-muted-foreground font-normal ml-1">· {ticket.email}</span>
                      </span>
                      {ticket.filename && <Paperclip className="h-3 w-3 text-muted-foreground shrink-0" />}
                      <span className="text-[10px] text-muted-foreground shrink-0">{dateStr}</span>
                      <Badge
                        variant="outline"
                        className={`text-[10px] shrink-0 ${
                          ticket.status === "open"
                            ? "border-amber-500/30 text-amber-400"
                            : "border-emerald-500/30 text-emerald-400"
                        }`}
                      >
                        {ticket.status === "open" ? "Pendente" : "Resolvido"}
                      </Badge>
                    </button>
                    {/* Expanded content */}
                    {isExpanded && (
                      <div className="px-4 pb-3 pt-1 border-t border-border/40 space-y-3">
                        <p className="text-sm text-foreground/85 whitespace-pre-wrap leading-relaxed">{ticket.mensagem}</p>
                        {ticket.filename && ticket.file_data && (
                          <div className="flex items-center gap-2">
                            <Paperclip className="h-3.5 w-3.5 text-muted-foreground" />
                            <a
                              href={`data:${ticket.file_type};base64,${ticket.file_data}`}
                              download={ticket.filename}
                              className="text-xs font-medium text-blue-400 hover:underline underline-offset-2"
                            >
                              {ticket.filename}
                            </a>
                          </div>
                        )}
                        <div className="flex items-center justify-between pt-1">
                          <span className="text-[11px] text-muted-foreground">
                            Resposta via e-mail manual · <span className="font-mono">{ticket.email}</span>
                          </span>
                          <div className="flex gap-1.5">
                            {ticket.status === "open" ? (
                              <Button
                                size="sm"
                                variant="outline"
                                className="h-7 text-xs gap-1.5 border-emerald-500/30 text-emerald-400 hover:bg-emerald-500/10"
                                onClick={() => handleUpdateTicketStatus(ticket.id, "closed")}
                              >
                                <CheckCheck className="h-3.5 w-3.5" /> Marcar Resolvido
                              </Button>
                            ) : (
                              <Button
                                size="sm"
                                variant="outline"
                                className="h-7 text-xs gap-1.5 border-amber-500/30 text-amber-400 hover:bg-amber-500/10"
                                onClick={() => handleUpdateTicketStatus(ticket.id, "open")}
                              >
                                <RotateCcw className="h-3.5 w-3.5" /> Reabrir
                              </Button>
                            )}
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
              {filteredTickets.length > 10 && (
                <p className="text-center text-[11px] text-muted-foreground pt-2">
                  Mostrando 10 de {filteredTickets.length} tickets filtrados. Use "Exportar CSV" para ver todos.
                </p>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {/* ── Demo Leads Monitoring (super_admin only) ─────────────────────── */}
      {isSuperAdmin && (
        <Card className="border-border">
          <CardHeader className="pb-3">
            {/* Row 1: title + badge + refresh */}
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <UserCheck className="h-4 w-4 text-amber-400" />
                <CardTitle className="text-sm font-semibold text-foreground">
                  Leads do Demo Gate
                </CardTitle>
                {leads.length > 0 && (
                  <Badge className="bg-amber-500/15 text-amber-400 border-amber-500/30 text-[10px]">
                    {filteredLeads.length}{leads.length !== filteredLeads.length ? `/${leads.length}` : ""} lead{filteredLeads.length !== 1 ? "s" : ""}
                  </Badge>
                )}
              </div>
              <Button
                variant="ghost"
                size="sm"
                className="h-7 text-xs gap-1.5 text-muted-foreground"
                onClick={() => { setLeadsLoaded(false); }}
              >
                <Loader2 className="h-3 w-3" /> Atualizar
              </Button>
            </div>
            <CardDescription className="text-xs">
              Contatos capturados no formulário de acesso ao demo do investidor.
            </CardDescription>
            {/* Row 2: date filters + export */}
            <div className="flex flex-wrap items-center gap-2 pt-2">
              <div className="flex items-center gap-1.5">
                <Label className="text-[11px] text-muted-foreground whitespace-nowrap">De</Label>
                <input
                  type="date"
                  value={leadStart}
                  onChange={(e) => setLeadStart(e.target.value)}
                  className="h-7 rounded-md border border-border bg-background px-2 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                />
              </div>
              <div className="flex items-center gap-1.5">
                <Label className="text-[11px] text-muted-foreground whitespace-nowrap">Até</Label>
                <input
                  type="date"
                  value={leadEnd}
                  onChange={(e) => setLeadEnd(e.target.value)}
                  className="h-7 rounded-md border border-border bg-background px-2 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                />
              </div>
              {(leadStart || leadEnd) && (
                <button
                  type="button"
                  onClick={() => { setLeadStart(""); setLeadEnd(""); }}
                  className="text-[11px] text-muted-foreground hover:text-foreground transition-colors"
                >
                  Limpar
                </button>
              )}
              <Button
                size="sm"
                className="h-7 text-xs gap-1.5 bg-emerald-600 hover:bg-emerald-700 text-white ml-auto"
                onClick={exportCSV}
                disabled={filteredLeads.length === 0}
              >
                <Download className="h-3 w-3" /> Exportar CSV
              </Button>
            </div>
          </CardHeader>
          <CardContent className="pt-0">
            {displayedLeads.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-8 text-center text-muted-foreground gap-2">
                <UserCheck className="h-8 w-8 opacity-30" />
                <p className="text-xs font-medium">
                  {leads.length === 0 ? "Nenhum lead capturado ainda" : "Nenhum lead no período selecionado"}
                </p>
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-border/50">
                      <th className="text-left pb-2 pr-4 font-medium text-muted-foreground">Nome</th>
                      <th className="text-left pb-2 pr-4 font-medium text-muted-foreground">E-mail</th>
                      <th className="text-left pb-2 pr-4 font-medium text-muted-foreground">Empresa / Fundo</th>
                      <th className="text-left pb-2 pr-4 font-medium text-muted-foreground">
                        <Phone className="h-3 w-3 inline mr-1" />Telefone
                      </th>
                      <th className="text-left pb-2 font-medium text-muted-foreground">Data</th>
                    </tr>
                  </thead>
                  <tbody>
                    {displayedLeads.map((lead) => (
                      <tr key={lead.id} className="border-b border-border/30 last:border-0">
                        <td className="py-2 pr-4 font-medium text-foreground">{lead.name}</td>
                        <td className="py-2 pr-4 text-violet-400/90 font-mono text-[11px]">{lead.email || "-"}</td>
                        <td className="py-2 pr-4 text-muted-foreground">{lead.company}</td>
                        <td className="py-2 pr-4 font-mono text-amber-400/90">{lead.phone}</td>
                        <td className="py-2 text-muted-foreground whitespace-nowrap">
                          {new Date(lead.created_at).toLocaleString("pt-BR", {
                            day: "2-digit", month: "2-digit", year: "2-digit",
                            hour: "2-digit", minute: "2-digit",
                          })}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {filteredLeads.length > 10 && (
                  <p className="text-[11px] text-muted-foreground text-center pt-3">
                    Mostrando 10 de {filteredLeads.length} leads. Use "Exportar CSV" para ver todos.
                  </p>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Transactions Section */}
      {isSuperAdmin && (
        <Card className="border-border/60">
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Receipt className="h-4 w-4 text-indigo-400" />
                <CardTitle className="text-sm">Transações Financeiras</CardTitle>
              </div>
              <Button
                variant="outline"
                size="sm"
                className="gap-1.5 text-xs"
                onClick={() => {
                  setShowTransactionsSection(!showTransactionsSection);
                  if (!transactionsLoaded) void fetchTransactions();
                }}
              >
                {showTransactionsSection ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                {showTransactionsSection ? "Fechar" : "Ver"}
              </Button>
            </div>
          </CardHeader>
          {showTransactionsSection && (
            <CardContent className="space-y-6">
              {/* Builder Subscription Overview */}
              <div>
                <p className="text-xs font-semibold text-muted-foreground mb-2 flex items-center gap-1.5">
                  <CreditCard className="h-3.5 w-3.5" /> Assinaturas por Builder
                </p>
                {builderSubscriptions.length === 0 ? (
                  <p className="text-xs text-muted-foreground text-center py-4">
                    {transactionsLoaded ? "Nenhum builder encontrado." : "Carregando…"}
                  </p>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="border-b border-border/50">
                          <th className="text-left pb-2 pr-4 font-medium text-muted-foreground">Builder</th>
                          <th className="text-left pb-2 pr-4 font-medium text-muted-foreground">Plano</th>
                          <th className="text-left pb-2 pr-4 font-medium text-muted-foreground">Status</th>
                          <th className="text-left pb-2 pr-4 font-medium text-muted-foreground">Ciclo</th>
                          <th className="text-left pb-2 font-medium text-muted-foreground">Atualizado</th>
                        </tr>
                      </thead>
                      <tbody>
                        {builderSubscriptions.map((s) => (
                          <tr key={s.id} className="border-b border-border/30 last:border-0">
                            <td className="py-2 pr-4 font-medium">{s.name}</td>
                            <td className="py-2 pr-4">
                              <Badge className="bg-indigo-500/15 text-indigo-400 border-indigo-500/30 text-[10px]">
                                {s.subscription_plan ?? "-"}
                              </Badge>
                            </td>
                            <td className="py-2 pr-4">
                              {s.subscription_status === "active" ? (
                                <Badge className="bg-emerald-500/15 text-emerald-400 border-emerald-500/30 text-[10px]">
                                  <CheckCircle2 className="h-3 w-3 mr-1" /> Ativo
                                </Badge>
                              ) : s.subscription_status === "trial" ? (
                                <Badge className="bg-amber-500/15 text-amber-400 border-amber-500/30 text-[10px]">Trial</Badge>
                              ) : (
                                <Badge variant="outline" className="text-slate-400 border-slate-600 text-[10px]">
                                  {s.subscription_status}
                                </Badge>
                              )}
                            </td>
                            <td className="py-2 pr-4 text-muted-foreground capitalize">{s.billing_cycle}</td>
                            <td className="py-2 text-muted-foreground">
                              {s.updated_at ? relativeTime(s.updated_at, locale) : "-"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>

              {/* Payment Log */}
              <div>
                <p className="text-xs font-semibold text-muted-foreground mb-2 flex items-center gap-1.5">
                  <Receipt className="h-3.5 w-3.5" /> Histórico de Pagamentos
                </p>
                {transactions.length === 0 ? (
                  <p className="text-xs text-muted-foreground text-center py-4">
                    {transactionsLoaded ? "Nenhum pagamento registrado ainda. Os pagamentos aparecem aqui após confirmação via webhook." : "Carregando…"}
                  </p>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="border-b border-border/50">
                          <th className="text-left pb-2 pr-4 font-medium text-muted-foreground">Data</th>
                          <th className="text-left pb-2 pr-4 font-medium text-muted-foreground">Builder</th>
                          <th className="text-left pb-2 pr-4 font-medium text-muted-foreground">Método</th>
                          <th className="text-left pb-2 pr-4 font-medium text-muted-foreground">Ciclo</th>
                          <th className="text-left pb-2 font-medium text-muted-foreground">Cupom</th>
                        </tr>
                      </thead>
                      <tbody>
                        {transactions.map((tx) => (
                          <tr key={tx.id} className="border-b border-border/30 last:border-0">
                            <td className="py-2 pr-4 text-muted-foreground tabular-nums">
                              {tx.created_at ? new Date(tx.created_at).toLocaleDateString("pt-BR") : "-"}
                            </td>
                            <td className="py-2 pr-4 font-medium">{tx.builder_name}</td>
                            <td className="py-2 pr-4">
                              <Badge
                                className={
                                  tx.method === "pix"
                                    ? "bg-teal-500/15 text-teal-400 border-teal-500/30 text-[10px]"
                                    : "bg-blue-500/15 text-blue-400 border-blue-500/30 text-[10px]"
                                }
                              >
                                {tx.method.toUpperCase()}
                              </Badge>
                            </td>
                            <td className="py-2 pr-4 text-muted-foreground capitalize">{tx.cycle}</td>
                            <td className="py-2">
                              {tx.coupon_code ? (
                                <Badge variant="outline" className="text-amber-400 border-amber-500/30 text-[10px] font-mono">
                                  {tx.coupon_code}
                                </Badge>
                              ) : (
                                <span className="text-muted-foreground/40">-</span>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </CardContent>
          )}
        </Card>
      )}

      {/* Coupons Section */}
      {isSuperAdmin && (
        <Card className="border-border/60">
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <DollarSign className="h-4 w-4 text-emerald-400" />
                <CardTitle className="text-sm">Cupons de Desconto</CardTitle>
              </div>
              <Button
                variant="outline"
                size="sm"
                className="gap-1.5 text-xs"
                onClick={() => { setShowCouponsSection(!showCouponsSection); if (!couponsLoaded) void fetchCoupons(); }}
              >
                {showCouponsSection ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                {showCouponsSection ? "Fechar" : "Gerenciar"}
              </Button>
            </div>
          </CardHeader>
          {showCouponsSection && (
            <CardContent className="space-y-4">
              {/* Create coupon form */}
              <div className="grid grid-cols-2 md:grid-cols-5 gap-2 items-end">
                <div>
                  <Label className="text-[10px] text-muted-foreground">Código</Label>
                  <Input
                    value={newCouponCode}
                    onChange={(e) => setNewCouponCode(e.target.value.toUpperCase())}
                    placeholder="PILOTO50"
                    className="text-xs font-mono uppercase h-8"
                  />
                </div>
                <div>
                  <Label className="text-[10px] text-muted-foreground">Desconto %</Label>
                  <Input
                    type="number"
                    min="1"
                    max="100"
                    value={newCouponDiscount}
                    onChange={(e) => setNewCouponDiscount(e.target.value)}
                    placeholder="50"
                    className="text-xs h-8"
                  />
                </div>
                <div>
                  <Label className="text-[10px] text-muted-foreground">Max usos</Label>
                  <Input
                    type="number"
                    min="1"
                    value={newCouponMaxUses}
                    onChange={(e) => setNewCouponMaxUses(e.target.value)}
                    placeholder="∞"
                    className="text-xs h-8"
                  />
                </div>
                <div>
                  <Label className="text-[10px] text-muted-foreground">Expira em</Label>
                  <Input
                    type="date"
                    value={newCouponExpires}
                    onChange={(e) => setNewCouponExpires(e.target.value)}
                    className="text-xs h-8"
                  />
                </div>
                <Button
                  size="sm"
                  onClick={() => void handleAddCoupon()}
                  disabled={addingCoupon || !newCouponCode.trim() || !newCouponDiscount}
                  className="gap-1.5 h-8"
                >
                  {addingCoupon ? <Loader2 className="h-3 w-3 animate-spin" /> : <Plus className="h-3 w-3" />}
                  Criar
                </Button>
              </div>

              {/* Coupons table */}
              {couponsLoaded && coupons.length > 0 && (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="border-b border-border/50">
                        <th className="text-left pb-2 pr-4 font-medium text-muted-foreground">Código</th>
                        <th className="text-left pb-2 pr-4 font-medium text-muted-foreground">Desconto</th>
                        <th className="text-left pb-2 pr-4 font-medium text-muted-foreground">Usos</th>
                        <th className="text-left pb-2 pr-4 font-medium text-muted-foreground">Expira</th>
                        <th className="text-left pb-2 pr-4 font-medium text-muted-foreground">Status</th>
                        <th className="text-left pb-2 font-medium text-muted-foreground">Ação</th>
                      </tr>
                    </thead>
                    <tbody>
                      {coupons.map((c) => (
                        <tr key={c.id} className="border-b border-border/30 last:border-0">
                          <td className="py-2 pr-4 font-mono font-bold text-foreground">{c.code}</td>
                          <td className="py-2 pr-4 text-emerald-400 font-semibold">{c.discount_percent}%</td>
                          <td className="py-2 pr-4 text-muted-foreground">
                            {c.uses_count}{c.max_uses ? ` / ${c.max_uses}` : " / ∞"}
                          </td>
                          <td className="py-2 pr-4 text-muted-foreground">
                            {c.expires_at
                              ? new Date(c.expires_at).toLocaleDateString("pt-BR")
                              : "-"}
                          </td>
                          <td className="py-2 pr-4">
                            <Badge className={c.is_active
                              ? "bg-emerald-500/15 text-emerald-400 border-emerald-500/30 text-[10px]"
                              : "bg-red-500/15 text-red-400 border-red-500/30 text-[10px]"
                            }>
                              {c.is_active ? "Ativo" : "Inativo"}
                            </Badge>
                          </td>
                          <td className="py-2">
                            {c.is_active && (
                              <Button
                                variant="ghost"
                                size="sm"
                                className="h-6 px-2 text-[10px] text-red-400 hover:text-red-300 hover:bg-red-500/10"
                                onClick={() => void handleDeactivateCoupon(c.id)}
                              >
                                Desativar
                              </Button>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {couponsLoaded && coupons.length === 0 && (
                <p className="text-xs text-muted-foreground text-center py-4">Nenhum cupom criado ainda.</p>
              )}
            </CardContent>
          )}
        </Card>
      )}

    </div>

    <AlertDialog open={!!deleteTarget} onOpenChange={(open) => { if (!open) setDeleteTarget(null); }}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>
            Excluir {deleteTarget?.type === "builder" ? "builder" : "clínica"} &quot;{deleteTarget?.name}&quot;?
          </AlertDialogTitle>
          <AlertDialogDescription>
            {deleteTarget?.type === "builder"
              ? "O builder, seu tenant e todos os usuários vinculados serão excluídos permanentemente. As clínicas gerenciadas serão desvinculadas, mas não deletadas."
              : "A clínica e todos os seus dados (usuários, sessões, traces) serão excluídos permanentemente. Esta ação não pode ser desfeita."}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={deleting}>Cancelar</AlertDialogCancel>
          <AlertDialogAction
            onClick={(e) => { e.preventDefault(); void handleConfirmDelete(); }}
            disabled={deleting}
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
          >
            {deleting ? <Loader2 className="h-4 w-4 animate-spin mr-2" /> : null}
            Excluir
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
    </>
  );
}
