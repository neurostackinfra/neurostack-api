import { useState } from "react";
import { useLocation } from "wouter";
import {
  Shield, Building2, Users, ToggleLeft, ToggleRight,
  Plus, Eye, CheckCircle2, XCircle, Loader2, AlertTriangle,
  Inbox, Paperclip, ChevronDown, ChevronRight, CheckCheck, UserCheck,
  Phone, TrendingUp, TrendingDown, Download, Search, CalendarRange, RefreshCw,
  RotateCcw, Briefcase, Trash2, Pencil, DollarSign, Info,
} from "lucide-react";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";
import { useAuth } from "@/context/auth";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
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
  traceCount: number;
  apisConfigured: boolean;
  health_status?: "healthy" | "warning" | "critical";
  last_trace_at?: string | null;
  config_completeness?: number;
}

interface BuilderWithClinics {
  id: number;
  name: string;
  admin_email?: string | null;
  status: "active" | "inactive";
  created_at: string;
  clinics: BuilderClinic[];
}

interface ClinicSlot {
  clinic_id: number;
  clinic_name: string;
  agent_name: string;
  slug: string | null;
  status: "active" | "inactive";
  plan_type: "pilot" | "demo" | "admin";
  traceCount: number;
  apisConfigured: boolean;
  zapiInstanceId: string | null;
  users: { id: number; email: string; role: string }[];
  health_status?: "healthy" | "warning" | "critical";
  last_trace_at?: string | null;
  config_completeness?: number;
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
  const styles: Record<string, string> = {
    clinic: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
    pilot: "bg-blue-500/15 text-blue-400 border-blue-500/30",
    demo:  "bg-amber-500/15 text-amber-400 border-amber-500/30",
    admin: "bg-violet-500/15 text-violet-400 border-violet-500/30",
  };
  return (
    <Badge className={`${styles[plan] ?? styles.pilot} text-[10px]`}>
      {plan.toUpperCase()}
    </Badge>
  );
}

// ── Relative Time Helper ─────────────────────────────────────────────────────

function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "never";
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 0) return "just now";
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

// ── Alerts Section ───────────────────────────────────────────────────────────

function AlertsSection({ alerts }: { alerts: AdminAlert[] }) {
  const { t } = useI18n();
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
                <span className="text-muted-foreground ml-1.5">{alert.message}</span>
              </div>
              <span className="text-[10px] text-muted-foreground shrink-0">
                {relativeTime(alert.detected_at)}
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

function CreatePilotDialog({ token, onCreated }: { token: string; onCreated: () => void }) {
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
      const data = await res.json() as { ok?: boolean; error?: string; clinic_id?: number };
      if (!res.ok) { setError(data.error ?? t("admin_create_err_generic")); return; }
      toast({ title: t("admin_create_ok_title"), description: `${t("admin_create_ok_desc_prefix")} "${name}" ID ${data.clinic_id}.` });
      setOpen(false);
      setName(""); setEmail(""); setPassword("");
      onCreated();
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

// ── Clinic Card ───────────────────────────────────────────────────────────────

function ClinicCard({
  clinic, token, onToggle, onViewTraces, onDelete, isReadOnly = false,
}: {
  clinic: ClinicSlot;
  token: string;
  onToggle: (id: number, newStatus: "active" | "inactive") => Promise<void>;
  onViewTraces: (id: number) => void;
  onDelete?: (id: number, name: string) => void;
  isReadOnly?: boolean;
}) {
  const { t } = useI18n();
  const { toast } = useToast();
  const [toggling, setToggling] = useState(false);
  const isEmpty = !clinic.clinic_name || clinic.clinic_name.startsWith("Piloto Slot") || clinic.clinic_name.startsWith("Slot");
  const adminUser = clinic.users.find((u) => u.role === "clinic_admin" || u.role === "investor");

  const handleToggle = async () => {
    if (isReadOnly) {
      toast({ title: "Modo Visualização: Ações de administrador desabilitadas nesta conta." });
      return;
    }
    setToggling(true);
    const next = clinic.status === "active" ? "inactive" : "active";
    await onToggle(clinic.clinic_id, next);
    setToggling(false);
  };

  return (
    <Card className={`border ${clinic.status === "active" ? "border-border" : "border-dashed border-slate-700 opacity-60"}`}>
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <Building2 className="h-4 w-4 text-muted-foreground shrink-0" />
            <div className="min-w-0">
              <CardTitle className="text-sm truncate">
                {isEmpty ? <span className="text-muted-foreground italic">{t("admin_slot_available")}</span> : clinic.clinic_name}
              </CardTitle>
              <CardDescription className="text-[10px]">
                ID {clinic.clinic_id} {clinic.slug ? `· /${clinic.slug}` : ""}
              </CardDescription>
            </div>
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            <PlanBadge plan={clinic.plan_type} />
            <StatusBadge status={clinic.status} />
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          <span className="flex items-center gap-1">
            <Users className="h-3 w-3" />
            {clinic.users.length} {clinic.users.length === 1 ? t("admin_users_label") : t("admin_users_label_pl")}
          </span>
        </div>
        {clinic.zapiInstanceId && (
          <p className="text-[10px] font-mono text-muted-foreground/60 truncate">
            Z-API: {clinic.zapiInstanceId.slice(0, 12)}…
          </p>
        )}
        {adminUser && (
          <p className="text-[11px] text-muted-foreground truncate">
            <span className="text-foreground">{adminUser.email}</span>
          </p>
        )}
        <div className="flex items-center gap-2 pt-1">
          <Button
            variant="outline"
            size="sm"
            className={`h-7 text-xs gap-1.5 flex-1 ${isReadOnly ? "opacity-50 cursor-not-allowed" : ""}`}
            onClick={handleToggle}
            disabled={toggling}
          >
            {toggling ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : clinic.status === "active" ? (
              <><ToggleRight className="h-3.5 w-3.5 text-emerald-400" /> {t("admin_toggle_deactivate")}</>
            ) : (
              <><ToggleLeft className="h-3.5 w-3.5" /> {t("admin_toggle_activate")}</>
            )}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 text-xs gap-1.5 flex-1"
            onClick={() => onViewTraces(clinic.clinic_id)}
            disabled={isEmpty}
          >
            <Eye className="h-3.5 w-3.5" /> {t("admin_view_traces")}
          </Button>
          {onDelete && !isReadOnly && (
            <Button
              variant="ghost"
              size="sm"
              className="h-7 w-7 p-0 text-muted-foreground hover:text-destructive shrink-0"
              title="Excluir clínica"
              onClick={() => onDelete(clinic.clinic_id, clinic.clinic_name)}
            >
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// ── Demo mock clinics (investor short-circuit — no API call) ──────────────────

const DEMO_CLINICS: ClinicSlot[] = [
  {
    clinic_id: 1, clinic_name: "Clínica Odonto Prime", agent_name: "Odonto Prime",
    slug: "piloto-1", status: "active", plan_type: "pilot", traceCount: 12_500,
    apisConfigured: true,  zapiInstanceId: "inst_a1b2c3",
    users: [{ id: 10, email: "pilot1@clinic.com", role: "clinic_admin" }],
    health_status: "healthy", last_trace_at: "2026-05-02T08:30:00Z", config_completeness: 95,
  },
  {
    clinic_id: 2, clinic_name: "Instituto Face SP", agent_name: "Face SP",
    slug: "piloto-2", status: "active", plan_type: "pilot", traceCount: 5_200,
    apisConfigured: true,  zapiInstanceId: "inst_e5f6g7",
    users: [{ id: 11, email: "pilot2@clinic.com", role: "clinic_admin" }],
    health_status: "healthy", last_trace_at: "2026-05-01T22:15:00Z", config_completeness: 88,
  },
  {
    clinic_id: 3, clinic_name: "Estética Rio Norte", agent_name: "Rio Norte",
    slug: "piloto-3", status: "active", plan_type: "pilot", traceCount: 1_100,
    apisConfigured: false, zapiInstanceId: null,
    users: [{ id: 12, email: "pilot3@clinic.com", role: "clinic_admin" }],
    health_status: "warning", last_trace_at: "2026-04-28T14:00:00Z", config_completeness: 42,
  },
  {
    clinic_id: 5, clinic_name: "Lumina Derma Clinic", agent_name: "Lumina Derma",
    slug: "piloto-4", status: "active", plan_type: "pilot", traceCount: 8_400,
    apisConfigured: true,  zapiInstanceId: "inst_i9j0k1",
    users: [{ id: 13, email: "pilot4@clinic.com", role: "clinic_admin" }],
    health_status: "critical", last_trace_at: null, config_completeness: 70,
  },
];

// ── Demo mock builders (investor short-circuit) ───────────────────────────────

const DEMO_BUILDERS: BuilderWithClinics[] = [
  {
    id: 1, name: "Builder Alfa", status: "active", created_at: "2026-01-15T10:00:00Z",
    clinics: [
      { clinic_id: 8,  clinic_name: "Clínica Alfa 1", slug: "alfa-1", status: "active", plan_type: "pilot", builder_id: 1, traceCount: 3200, apisConfigured: true, health_status: "healthy", last_trace_at: "2026-05-02T07:00:00Z", config_completeness: 100 },
      { clinic_id: 9,  clinic_name: "Clínica Alfa 2", slug: "alfa-2", status: "active", plan_type: "pilot", builder_id: 1, traceCount: 1800, apisConfigured: true, health_status: "healthy", last_trace_at: "2026-05-01T18:30:00Z", config_completeness: 85 },
      { clinic_id: 10, clinic_name: "Clínica Alfa 3", slug: "alfa-3", status: "active", plan_type: "pilot", builder_id: 1, traceCount: 950,  apisConfigured: false, health_status: "warning", last_trace_at: "2026-04-27T10:00:00Z", config_completeness: 35 },
    ],
  },
  {
    id: 2, name: "Builder Beta", status: "active", created_at: "2026-02-01T10:00:00Z",
    clinics: [
      { clinic_id: 11, clinic_name: "Clínica Beta 1", slug: "beta-1", status: "active", plan_type: "pilot", builder_id: 2, traceCount: 2100, apisConfigured: true, health_status: "healthy", last_trace_at: "2026-05-02T06:45:00Z", config_completeness: 92 },
      { clinic_id: 12, clinic_name: "Clínica Beta 2", slug: "beta-2", status: "active", plan_type: "pilot", builder_id: 2, traceCount: 890,  apisConfigured: false, health_status: "critical", last_trace_at: null, config_completeness: 20 },
    ],
  },
];

// ── Demo dashboard data (investor short-circuit) ────────────────────────────

const DEMO_DASHBOARD: AdminDashboardData = {
  stats: {
    total_traces: 27200,
    active_pilots: 170,
    active_builders: 80,
    active_managed_clinics: 170,
    estimated_mrr: 270000,
    safety_flags_7d: 2,
    handoff_count_7d: 48,
    // Extended stats for investor breakdown
    total_clinics: 200,
    inactive_clinics: 30,
    total_builders: 100,
    inactive_builders: 20,
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
  const { t } = useI18n();
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
  const [alertBuilderFilter, setAlertBuilderFilter] = useState<string>("all");

  // Builders state
  const [builders, setBuilders] = useState<BuilderWithClinics[]>(isInvestor ? DEMO_BUILDERS : []);
  const [buildersLoaded, setBuildersLoaded] = useState(isInvestor);
  const [expandedBuilderId, setExpandedBuilderId] = useState<number | null>(null);
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

  const fetchClinics = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/v1/admin/clinics`, { headers: authHeaders(token) });
      if (!res.ok) { setError(t("admin_err_clinics")); return; }
      const data = await res.json() as ClinicSlot[];
      setClinics(data);
      setLoaded(true);
    } catch {
      setError(t("admin_err_network"));
    } finally {
      setLoading(false);
    }
  };

  const fetchBuilders = async () => {
    try {
      const res = await fetch(`/v1/admin/builders`, { headers: authHeaders(token) });
      if (!res.ok) return;
      setBuilders(await res.json() as BuilderWithClinics[]);
      setBuildersLoaded(true);
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
      await fetchBuilders();
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
      await fetchBuilders();
      toast({ title: "Clínica criada com sucesso!" });
    } catch {
      toast({ title: "Erro de rede", variant: "destructive" });
    }
  };

  const handleToggleBuilderClinic = async (clinicId: number, newStatus: "active" | "inactive") => {
    if (isInvestor) { toast({ title: "Modo Visualização: ação desabilitada." }); return; }
    try {
      const res = await fetch(`/v1/admin/clinics/${clinicId}/status`, {
        method: "PATCH", headers: authHeaders(token),
        body: JSON.stringify({ status: newStatus }),
      });
      if (!res.ok) { toast({ title: "Erro ao alterar status da clínica", variant: "destructive" }); return; }
      setBuilders((prev) => prev.map((b) => ({
        ...b,
        clinics: b.clinics.map((c) => c.clinic_id === clinicId ? { ...c, status: newStatus } : c),
      })));
    } catch {
      toast({ title: "Erro de rede", variant: "destructive" });
    }
  };

  const handleDeleteBuilderClinic = (clinicId: number, clinicName: string) => {
    setDeleteTarget({ type: "clinic", id: clinicId, name: clinicName });
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
      await fetchBuilders();
      await fetchClinics();
      toast({ title: "Clínica vinculada com sucesso!" });
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
      toast({ title: `${type === "builder" ? "Builder" : "Clínica"} "${name}" excluído(a).` });
    } catch {
      toast({ title: "Erro de rede", variant: "destructive" });
    } finally {
      setDeleting(false);
      setDeleteTarget(null);
    }
  };

  // Only real admins hit the network
  if (!isInvestor && !loaded && !loading) { void fetchClinics(); }
  if (!isInvestor && !buildersLoaded) { void fetchBuilders(); }

  if (!isSuperAdmin && !isInvestor) {
    return (
      <div className="flex items-center justify-center h-64 text-muted-foreground">
        <Shield className="h-8 w-8 mr-2" /> {t("admin_restricted")}
      </div>
    );
  }

  const handleToggle = async (id: number, newStatus: "active" | "inactive") => {
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
    setViewingClinicId(id);
    navigate("/traces");
  };

  const fetchTickets = async () => {
    try {
      const res = await fetch(`/v1/support/tickets`, { headers: authHeaders(token) });
      if (!res.ok) return;
      setTickets(await res.json() as SupportTicket[]);
      setTicketsLoaded(true);
    } catch { /* silent */ }
  };

  const fetchLeads = async () => {
    try {
      const res = await fetch(`/v1/demo-leads`, { headers: authHeaders(token) });
      if (!res.ok) return;
      setLeads(await res.json() as DemoLead[]);
      setLeadsLoaded(true);
    } catch { /* silent */ }
  };

  const fetchStats = async () => {
    try {
      const res = await fetch(`/v1/admin/stats`, { headers: authHeaders(token) });
      if (!res.ok) return;
      const raw = await res.json() as Record<string, unknown>;
      setStats({
        totalTraces: (raw.total_traces ?? raw.totalTraces ?? 0) as number,
        totalTokens: (raw.total_tokens ?? raw.totalTokens ?? 0) as number,
        openTickets: (raw.open_tickets ?? raw.openTickets ?? 0) as number,
        activeClinics: (raw.active_pilots ?? raw.activeClinics ?? 0) as number,
        activeBuilders: (raw.active_builders ?? raw.activeBuilders ?? 0) as number,
        activeManagedClinics: (raw.active_managed_clinics ?? raw.activeManagedClinics ?? 0) as number,
        estimatedMrr: (raw.estimated_mrr ?? raw.estimatedMrr ?? 0) as number,
      });
      setStatsLoaded(true);
    } catch { /* silent */ }
  };

  const fetchDashboard = async () => {
    try {
      const res = await fetch(`/v1/admin/dashboard`, { headers: authHeaders(token) });
      if (!res.ok) return;
      setDashboard(await res.json() as AdminDashboardData);
      setDashboardLoaded(true);
    } catch { /* silent */ }
  };

  if (!ticketsLoaded) { void fetchTickets(); }
  if (!leadsLoaded && isSuperAdmin) { void fetchLeads(); }
  if (!statsLoaded && isSuperAdmin) { void fetchStats(); }
  if (!dashboardLoaded && !isInvestor) { void fetchDashboard(); }

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
        headers: { "Content-Type": "application/json", ...authHeaders(token) },
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

  // Investors see impressive demo numbers; real admins see live DB stats
  const totalClinics  = isInvestor ? 200 : clinics.filter((c) => c.plan_type === "pilot").length;
  const activeClinics  = isInvestor ? 170 : (stats?.activeClinics ?? clinics.filter((c) => c.plan_type === "pilot" && c.status === "active").length);
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

  // ── Alert filtering (client-side) ────────────────────────────────────────
  const allAlerts = dashboard?.alerts ?? [];
  const alertBuilderNames = Array.from(new Set(allAlerts.map((a) => a.clinic_name))).sort();
  const filteredAlerts = allAlerts.filter((a) => {
    if (alertSeverityFilter !== "all" && a.severity !== alertSeverityFilter) return false;
    if (alertBuilderFilter !== "all" && a.clinic_name !== alertBuilderFilter) return false;
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
            {t("admin_infra_desc")} · {totalClinics} slots · {activeClinics} {t("admin_pilots_label")}
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
          <CreatePilotDialog token={token} onCreated={fetchClinics} />
        )}
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Card className="border-border">
          <CardContent className="pt-4 pb-3">
            <p className="text-[11px] text-muted-foreground">{t("admin_stat_total")}</p>
            <p className="text-2xl font-bold text-foreground">{totalClinics}</p>
          </CardContent>
        </Card>
        <Card className="border-border">
          <CardContent className="pt-4 pb-3">
            <p className="text-[11px] text-muted-foreground">{t("admin_stat_active")}</p>
            <p className="text-2xl font-bold text-emerald-400">{activeClinics}</p>
          </CardContent>
        </Card>
        <Card className="border-violet-500/30 bg-gradient-to-br from-violet-950/30 to-transparent">
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-1.5 mb-1">
              <DollarSign className="h-3.5 w-3.5 text-violet-400" />
              <p className="text-[11px] text-muted-foreground font-medium">MRR Estimado</p>
            </div>
            <p className="text-2xl font-bold text-violet-400">
              {isInvestor
                ? "R$ 270.000"
                : stats?.estimatedMrr != null
                  ? `R$ ${stats.estimatedMrr.toLocaleString("pt-BR")}`
                  : "R$ …"}
            </p>
            <p className="text-[10px] text-muted-foreground/60 mt-0.5">
              {isInvestor
                ? "100 builders × R$2k + 200 clínicas × R$350"
                : stats
                  ? `${stats.activeBuilders} builder${stats.activeBuilders !== 1 ? "s" : ""} × R$2k + ${stats.activeManagedClinics} clínica${stats.activeManagedClinics !== 1 ? "s" : ""} × R$350`
                  : ""}
            </p>
          </CardContent>
        </Card>
        <Card className="border-border bg-gradient-to-br from-blue-950/20 to-transparent">
          <CardContent className="pt-4 pb-3">
            <p className="text-[11px] text-muted-foreground">Consumo API (Mês)</p>
            <p className="text-2xl font-bold text-blue-400">
              {tokenDisplay} <span className="text-base font-medium">Tokens</span>
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Investor breakdown: clinics + builders totals */}
      {isInvestor && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          {/* Clinics breakdown */}
          <Card className="border-border">
            <CardHeader className="pb-2">
              <div className="flex items-center gap-2">
                <Building2 className="h-4 w-4 text-blue-400" />
                <CardTitle className="text-sm font-semibold">Clínicas</CardTitle>
              </div>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-xs text-muted-foreground">Total Clínicas</span>
                <span className="text-xl font-bold text-foreground">200</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-xs text-muted-foreground flex items-center gap-1.5">
                  <CheckCircle2 className="h-3 w-3 text-emerald-400" /> Clínicas Ativas
                </span>
                <span className="text-lg font-bold text-emerald-400">170</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-xs text-muted-foreground flex items-center gap-1.5">
                  <XCircle className="h-3 w-3 text-slate-400" /> Clínicas Inativas
                </span>
                <span className="text-lg font-bold text-slate-400">30</span>
              </div>
              <div className="h-2 w-full rounded-full bg-muted overflow-hidden">
                <div className="h-full bg-emerald-500 rounded-full" style={{ width: "85%" }} />
              </div>
            </CardContent>
          </Card>
          {/* Builders breakdown */}
          <Card className="border-border">
            <CardHeader className="pb-2">
              <div className="flex items-center gap-2">
                <Users className="h-4 w-4 text-indigo-400" />
                <CardTitle className="text-sm font-semibold">Builders</CardTitle>
              </div>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-xs text-muted-foreground">Total Builders</span>
                <span className="text-xl font-bold text-foreground">100</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-xs text-muted-foreground flex items-center gap-1.5">
                  <CheckCircle2 className="h-3 w-3 text-emerald-400" /> Builders Ativos
                </span>
                <span className="text-lg font-bold text-emerald-400">80</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-xs text-muted-foreground flex items-center gap-1.5">
                  <XCircle className="h-3 w-3 text-slate-400" /> Builders Inativos
                </span>
                <span className="text-lg font-bold text-slate-400">20</span>
              </div>
              <div className="h-2 w-full rounded-full bg-muted overflow-hidden">
                <div className="h-full bg-indigo-500 rounded-full" style={{ width: "80%" }} />
              </div>
            </CardContent>
          </Card>
        </div>
      )}

      {/* Real-data metrics row (super_admin only) */}
      {isSuperAdmin && (
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
          <Card className="border-border bg-gradient-to-br from-emerald-950/20 to-transparent">
            <CardContent className="pt-4 pb-3">
              <div className="flex items-center gap-1.5 mb-1">
                <TrendingUp className="h-3.5 w-3.5 text-emerald-400" />
                <p className="text-[11px] text-muted-foreground">Total de Traces (Pilotos)</p>
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
              value={alertBuilderFilter}
              onChange={(e) => setAlertBuilderFilter(e.target.value)}
              className="h-8 rounded-md border border-border bg-background px-2 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-accent"
            >
              <option value="all">{t("admin_filter_builder")}: {t("admin_filter_all")}</option>
              {alertBuilderNames.map((name) => (
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
                {builders.filter((b) => b.status === "active").length} ativo{builders.filter((b) => b.status === "active").length !== 1 ? "s" : ""}
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
            Revendedores e agências. MRR = R$2.000/builder ativo + R$350/clínica ativa gerenciada.
          </CardDescription>
        </CardHeader>
        <CardContent className="pt-0 space-y-2">
          {builders.length === 0 ? (
            <div className="flex flex-col items-center py-8 text-muted-foreground gap-2">
              <Briefcase className="h-8 w-8 opacity-25" />
              <p className="text-xs">Nenhum builder cadastrado ainda.</p>
            </div>
          ) : (
            builders.map((builder) => {
              const isExpanded = expandedBuilderId === builder.id;
              const isEditing = editingBuilderId === builder.id;
              const isAddingClinic = addingClinicToBuilderId === builder.id;
              const activeClinics = builder.clinics.filter((c) => c.status === "active").length;
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
                        {builder.clinics.length} clínica{builder.clinics.length !== 1 ? "s" : ""} · {activeClinics} ativa{activeClinics !== 1 ? "s" : ""}
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
                        const linkedIds = new Set(builders.flatMap((b) => b.clinics.map((c) => c.clinic_id)));
                        const available = clinics.filter((c) => !linkedIds.has(c.clinic_id));
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
                            return (
                            <div key={c.clinic_id} className={`rounded-md px-2.5 py-2 text-xs transition-colors ${c.status === "active" ? "bg-background/60 border border-border/40" : "bg-muted/20 border border-dashed border-slate-700 opacity-60"}`}>
                              {isEditingClinic ? (
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
                              ) : (
                                <div className="flex items-center gap-3">
                                  <Building2 className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
                                  <span className="font-medium truncate">{c.clinic_name}</span>
                                  {c.admin_email && <span className="text-[10px] text-muted-foreground truncate">{c.admin_email}</span>}
                                  {c.slug && <span className="text-muted-foreground font-mono text-[10px]">/{c.slug}</span>}

                                  <div className="ml-auto flex items-center gap-1 shrink-0">
                                    <StatusBadge status={c.status} />
                                    {isSuperAdmin && (
                                      <>
                                        <Button
                                          variant="ghost" size="sm"
                                          className="h-6 w-6 p-0 text-muted-foreground hover:text-foreground"
                                          title="Editar clínica"
                                          onClick={() => { setEditingClinicId(c.clinic_id); setEditClinicName(c.clinic_name); setEditClinicEmail(c.admin_email ?? ""); setEditClinicPassword(""); }}
                                        >
                                          <Pencil className="h-3 w-3" />
                                        </Button>
                                        <Button
                                          variant="ghost" size="sm"
                                          className={`h-6 w-6 p-0 ${c.status === "active" ? "text-emerald-400 hover:text-emerald-300" : "text-muted-foreground"}`}
                                          onClick={() => void handleToggleBuilderClinic(c.clinic_id, c.status === "active" ? "inactive" : "active")}
                                        >
                                          {c.status === "active" ? <ToggleRight className="h-3.5 w-3.5" /> : <ToggleLeft className="h-3.5 w-3.5" />}
                                        </Button>
                                        <Button
                                          variant="ghost" size="sm"
                                          className="h-6 w-6 p-0 text-muted-foreground hover:text-destructive"
                                          onClick={() => void handleDeleteBuilderClinic(c.clinic_id, c.clinic_name)}
                                        >
                                          <Trash2 className="h-3 w-3" />
                                        </Button>
                                      </>
                                    )}
                                  </div>
                                </div>
                              )}
                            </div>
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

      {/* Clinic grid */}
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
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {clinics.map((c) => (
            <ClinicCard
              key={c.clinic_id}
              clinic={c}
              token={token}
              onToggle={handleToggle}
              onViewTraces={handleViewTraces}
              onDelete={isSuperAdmin ? handleDeleteClinic : undefined}
              isReadOnly={isInvestor}
            />
          ))}
        </div>
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
                        <td className="py-2 pr-4 text-violet-400/90 font-mono text-[11px]">{lead.email || "—"}</td>
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

      {/* Webhook reference */}
      <Card className="border-dashed border-violet-500/30 bg-violet-500/5">
        <CardHeader className="pb-2">
          <CardTitle className="text-sm text-violet-400">{t("admin_webhook_title")}</CardTitle>
          <CardDescription className="text-xs">{t("admin_webhook_desc")}</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="space-y-1">
            {clinics.filter((c) => c.status === "active" && c.plan_type !== "demo").map((c) => (
              <div key={c.clinic_id} className="flex items-center gap-2 text-[11px] font-mono bg-slate-900 rounded px-2 py-1">
                <span className="text-violet-400 shrink-0">POST</span>
                <span className="text-muted-foreground truncate">/api/whatsapp/webhook?clinic_id={c.clinic_id}</span>
                <Badge variant="outline" className="ml-auto text-[9px] shrink-0">
                  {c.clinic_name.split(" ").slice(0, 2).join(" ")}
                </Badge>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
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
