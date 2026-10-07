import { useState, useEffect, useMemo, useRef } from "react";
import { useLocation } from "wouter";
import {
  Wrench, Building2, Activity, Plus, CheckCircle2, XCircle, Zap,
  Loader2, AlertTriangle, AlertCircle, Info, KeyRound, ToggleLeft, ToggleRight,
  Sliders, Inbox, ChevronDown, ChevronUp, TrendingUp, TrendingDown, ExternalLink,
  Clock, MessageCircle, LifeBuoy, Mail,
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/hooks/use-toast";
import { useI18n } from "@/context/i18n";
import { PasswordChangeDialog } from "@/components/password-change-dialog";
import {
  PlanUsageSummary,
  type BillingUsage,
} from "@/components/plan-usage-summary";

// ── Types ─────────────────────────────────────────────────────────────────────

interface BuilderClinic {
  id: string;
  name: string;
  slug: string | null;
  status: "active" | "inactive";
  plan_type: string;
  is_active: boolean;
  created_at: string | null;
  last_trace_at: string | null;
  health_status: "healthy" | "warning" | "critical";
  trace_count_7d: number;
  safety_flags_7d: number;
  handoff_count_7d: number;
  outside_hours_count_7d: number;
  outside_hours_pct_7d: number | null;
  conversational_trace_count_7d: number;
  business_hours_configured: boolean;
  config_completeness: number;
  contact_phone: string | null;
  admin_email: string | null;
}

interface BuilderTicket {
  id: number;
  clinic_id: string | null;
  clinic_name: string | null;
  contact_phone: string | null;
  email: string;
  tipo: string;
  mensagem: string;
  filename: string | null;
  file_type: string | null;
  status: string;
  created_at: string;
}

interface BuilderStats {
  clinic_count: number;
  active_clinics: number;
  inactive_clinics: number;
  total_traces: number;
  total_tokens: number;
  model_breakdown: { model: string; tokens: number }[];
}

interface BuilderAlert {
  type: "inactivity" | "deactivated" | "api_error";
  severity: "critical" | "warning" | "info";
  clinic_id: string;
  clinic_name: string;
  message: string;
  detected_at: string | null;
}

interface TraceTimeSeriesPoint {
  date: string;
  count: number;
}

interface DashboardData {
  stats: BuilderStats;
  time_series: TraceTimeSeriesPoint[];
  alerts: BuilderAlert[];
}

interface BuilderProfile {
  id: string;
  name: string;
  status: string;
  clinic_count: number;
  support_email: string | null;
}

interface ClinicClonePreview {
  suggested_name: string;
  document_count: number;
  total_bytes: number;
}

interface ClinicCloneResult {
  id: string;
  name: string;
  is_active: false;
}

// ── API helpers ───────────────────────────────────────────────────────────────

function useBuilderToken() {
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

// ── Plan Badge ────────────────────────────────────────────────────────────────

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

// ── Health Dot ────────────────────────────────────────────────────────────────

function HealthDot({ status }: { status: string }) {
  const color =
    status === "healthy" ? "bg-emerald-400" :
    status === "warning" ? "bg-amber-400" :
    "bg-red-400";
  return <span className={`inline-block w-2 h-2 rounded-full ${color}`} />;
}

function OutsideHoursClinicMetric({ clinic, locale }: { clinic: BuilderClinic; locale: string }) {
  const txt = (pt: string, en: string) => locale === "en" ? en : pt;
  const hasData = clinic.business_hours_configured
    && clinic.conversational_trace_count_7d > 0
    && clinic.outside_hours_pct_7d !== null;
  const numberLocale = locale === "en" ? "en-US" : "pt-BR";

  return (
    <div className="text-center p-2 rounded-lg bg-muted/30" data-testid={`clinic-outside-hours-${clinic.id}`}>
      <div className="flex items-center justify-center gap-1 text-[10px] text-muted-foreground">
        <Clock className="h-3 w-3" /> {txt("Fora do horário (7d)", "Outside hours (7d)")}
      </div>
      {!clinic.business_hours_configured ? (
        <p className="mt-1 text-xs font-semibold text-amber-400">{txt("Não configurado", "Not configured")}</p>
      ) : hasData ? (
        <p className="mt-1 text-lg font-bold text-violet-400">
          {clinic.outside_hours_pct_7d!.toLocaleString(numberLocale, { maximumFractionDigits: 1 })}%
        </p>
      ) : (
        <p className="mt-1 text-xs font-semibold text-muted-foreground">{txt("Sem dados", "No data")}</p>
      )}
      {clinic.business_hours_configured && (
        <p className="text-[10px] text-muted-foreground tabular-nums">
          {clinic.outside_hours_count_7d.toLocaleString(numberLocale)} / {clinic.conversational_trace_count_7d.toLocaleString(numberLocale)}
        </p>
      )}
    </div>
  );
}

// ── Chart Tooltip ─────────────────────────────────────────────────────────────

function ChartTooltip({ active, payload }: { active?: boolean; payload?: { value: number; payload: { date: string } }[] }) {
  if (!active || !payload?.length) return null;
  const d = payload[0];
  const dateParts = d.payload.date.split("-");
  const label = `${dateParts[2]}/${dateParts[1]}`;
  return (
    <div className="rounded-lg border border-border bg-popover px-3 py-2 text-xs shadow-lg">
      <p className="text-foreground font-semibold">{label}: {d.value} traces</p>
    </div>
  );
}

// ── Trend helper ──────────────────────────────────────────────────────────────

function computeTrend(timeSeries: TraceTimeSeriesPoint[]): { diff: number; direction: "up" | "down" | "flat" } {
  if (timeSeries.length < 14) return { diff: 0, direction: "flat" };
  const recent7 = timeSeries.slice(-7).reduce((s, p) => s + p.count, 0);
  const prev7 = timeSeries.slice(-14, -7).reduce((s, p) => s + p.count, 0);
  const diff = recent7 - prev7;
  return { diff, direction: diff > 0 ? "up" : diff < 0 ? "down" : "flat" };
}

// ── Relative time ─────────────────────────────────────────────────────────────

function relativeTime(dateStr: string | null, locale: string): string {
  if (!dateStr) return locale === "pt" ? "nunca" : "never";
  const formatter = new Intl.RelativeTimeFormat(locale === "pt" ? "pt-BR" : "en", { numeric: "auto" });
  const diff = Date.now() - new Date(dateStr).getTime();
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 1) return formatter.format(0, "minute");
  if (minutes < 60) return formatter.format(-minutes, "minute");
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return formatter.format(-hours, "hour");
  return formatter.format(-Math.floor(hours / 24), "day");
}

function normalizeClinicSearch(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "");
}

// ── Builder Panel ─────────────────────────────────────────────────────────────

export default function BuilderPanel() {
  const { t, locale } = useI18n();
  const { isBuilder, setViewingClinicId, email: authEmail } = useAuth();
  const [, navigate] = useLocation();
  const token = useBuilderToken();
  const { toast } = useToast();

  // Data state
  const [profile, setProfile] = useState<BuilderProfile | null>(null);
  const [dashboard, setDashboard] = useState<DashboardData | null>(null);
  const [clinics, setClinics] = useState<BuilderClinic[]>([]);
  const [usage, setUsage] = useState<BillingUsage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Tab state
  const [activeTab, setActiveTab] = useState("overview");
  const [clinicSearchQuery, setClinicSearchQuery] = useState("");

  // Create clinic form state
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [newName, setNewName] = useState("");
  const [newEmail, setNewEmail] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [newPhone, setNewPhone] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  // Per-clinic phone editing state (map of clinicId -> current draft)
  const [phoneDrafts, setPhoneDrafts] = useState<Record<string, string>>({});
  const [savingPhoneClinicId, setSavingPhoneClinicId] = useState<string | null>(null);
  const [emailDrafts, setEmailDrafts] = useState<Record<string, string>>({});
  const [emailErrors, setEmailErrors] = useState<Record<string, string | undefined>>({});
  const [savingEmailClinicId, setSavingEmailClinicId] = useState<string | null>(null);
  const emailRequestInFlightRef = useRef(new Set<string>());

  // Support tickets state
  const [tickets, setTickets] = useState<BuilderTicket[]>([]);
  const [ticketsError, setTicketsError] = useState<string | null>(null);
  const [ticketStatusFilter, setTicketStatusFilter] = useState<"all" | "open" | "closed">("open");

  // Support notification email state
  const [supportEmailDraft, setSupportEmailDraft] = useState("");
  const [savingSupportEmail, setSavingSupportEmail] = useState(false);

  // Clone clinic state
  const [cloneSourceId, setCloneSourceId] = useState("");
  const [clonePreview, setClonePreview] = useState<ClinicClonePreview | null>(null);
  const [clonePreviewLoading, setClonePreviewLoading] = useState(false);
  const [clonePreviewError, setClonePreviewError] = useState<string | null>(null);
  const [cloneConfirmed, setCloneConfirmed] = useState(false);
  const [cloning, setCloning] = useState(false);
  const [cloneError, setCloneError] = useState<string | null>(null);
  const [cloneResult, setCloneResult] = useState<ClinicCloneResult | null>(null);
  const [cloneRefreshWarning, setCloneRefreshWarning] = useState(false);
  const clonePreviewRequestRef = useRef(0);
  const cloneRequestInFlightRef = useRef(false);

  // Expandable clinic state
  const [expandedClinicId, setExpandedClinicId] = useState<string | null>(null);
  const [passwordClinic, setPasswordClinic] = useState<BuilderClinic | null>(null);

  // Fetch all data on mount and when token changes
  useEffect(() => {
    if (isBuilder && token) {
      void fetchAll();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isBuilder, token]);

  async function fetchAll() {
    setLoading(true);
    setError(null);
    setUsage(null);
    try {
      const headers = authHeaders(token);
      const [profileRes, dashboardRes, clinicsRes, usageRes, ticketsRes, supportProfileRes] = await Promise.all([
        fetch("/v1/builder/me", { headers }),
        fetch("/v1/builder/dashboard", { headers }),
        fetch("/v1/builder/clinics", { headers }),
        fetch("/v1/billing/usage", { headers }),
        fetch("/v1/builder/tickets", { headers }),
        fetch("/v1/builder/profile", { headers }),
      ]);

      const errors: string[] = [];

      if (profileRes.ok) {
        setProfile(await profileRes.json() as BuilderProfile);
      } else {
        errors.push(`profile: ${profileRes.status}`);
      }

      if (dashboardRes.ok) {
        setDashboard(await dashboardRes.json() as DashboardData);
      } else {
        errors.push(`dashboard: ${dashboardRes.status}`);
      }

      if (clinicsRes.ok) {
        setClinics(await clinicsRes.json() as BuilderClinic[]);
      } else {
        errors.push(`clinics: ${clinicsRes.status}`);
      }

      if (usageRes.ok) {
        setUsage(await usageRes.json() as BillingUsage);
      } else {
        errors.push(`usage: ${usageRes.status}`);
      }

      if (ticketsRes.ok) {
        setTickets(await ticketsRes.json() as BuilderTicket[]);
        setTicketsError(null);
      } else {
        errors.push(`tickets: ${ticketsRes.status}`);
        setTicketsError(`tickets: ${ticketsRes.status}`);
      }

      if (supportProfileRes.ok) {
        const data = await supportProfileRes.json() as { support_email: string | null };
        setSupportEmailDraft(data.support_email ?? "");
      } else {
        errors.push(`support-profile: ${supportProfileRes.status}`);
      }

      if (errors.length > 0) {
        console.warn("[Builder] Fetch errors:", errors);
        if (errors.length === 6) {
          setError("Erro ao carregar dados");
        }
      }
    } catch (err) {
      console.error("[Builder] fetchAll failed:", err);
      setError("Erro ao carregar dados");
    } finally {
      setLoading(false);
    }
  }

  // Sort clinics: critical first, then warning, then healthy
  const sortedClinics = useMemo(() => {
    const order: Record<string, number> = { critical: 0, warning: 1, healthy: 2 };
    return [...clinics].sort(
      (a, b) => (order[a.health_status] ?? 2) - (order[b.health_status] ?? 2)
    );
  }, [clinics]);

  const normalizedClinicSearchQuery = normalizeClinicSearch(clinicSearchQuery);
  const filteredClinics = useMemo(
    () => normalizedClinicSearchQuery
      ? sortedClinics.filter((clinic) =>
          normalizeClinicSearch(clinic.name).includes(normalizedClinicSearchQuery),
        )
      : sortedClinics,
    [normalizedClinicSearchQuery, sortedClinics],
  );

  // Trend calculation
  const trend = useMemo(
    () => computeTrend(dashboard?.time_series ?? []),
    [dashboard]
  );

  // Create clinic handler
  async function handleCreateClinic() {
    if (!newName.trim() || !newEmail.trim() || !newPassword.trim()) {
      setCreateError(t("admin_create_required"));
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      const phoneTrimmed = newPhone.trim();
      const res = await fetch("/v1/builder/clinics", {
        method: "POST",
        headers: authHeaders(token),
        body: JSON.stringify({
          clinic_name: newName.trim(),
          admin_email: newEmail.trim(),
          admin_password: newPassword,
          contact_phone: phoneTrimmed ? phoneTrimmed : null,
        }),
      });
      const data = await res.json() as { ok?: boolean; error?: string; detail?: unknown; id?: string };
      if (!res.ok) {
        // Backend returns "detail" (FastAPI) or "error"
        let msg = data.error ?? t("admin_create_err_generic");
        if (data.detail) {
          if (typeof data.detail === "string") {
            msg = data.detail;
          } else if (Array.isArray(data.detail)) {
            // Pydantic validation errors
            msg = (data.detail as Array<{ msg?: string }>).map((e) => e.msg).join("; ");
          }
        }
        setCreateError(msg);
        return;
      }
      toast({ title: t("builder_create_success") });
      setShowCreateForm(false);
      setNewName("");
      setNewEmail("");
      setNewPassword("");
      setNewPhone("");
      void fetchAll();
    } catch {
      setCreateError(t("admin_create_err_network"));
    } finally {
      setCreating(false);
    }
  }

  function cloneErrorMessage(status: number): string {
    if (status === 403) return t("builder_clone_error_403");
    if (status === 404) return t("builder_clone_error_404");
    if (status === 409) return t("builder_clone_error_409");
    if (status === 500) return t("builder_clone_error_500");
    return t("builder_clone_error_network");
  }

  function clonePreviewErrorMessage(status: number): string {
    if (status === 403) return t("builder_clone_preview_error_403");
    if (status === 404) return t("builder_clone_preview_error_404");
    if (status === 409) return t("builder_clone_preview_error_409");
    if (status === 500) return t("builder_clone_preview_error_500");
    return t("builder_clone_preview_error_network");
  }

  async function handleCloneSourceChange(sourceId: string) {
    const requestId = ++clonePreviewRequestRef.current;
    setCloneSourceId(sourceId);
    setClonePreview(null);
    setClonePreviewError(null);
    setCloneConfirmed(false);
    setCloneError(null);
    setCloneResult(null);
    setCloneRefreshWarning(false);

    if (!sourceId) {
      setClonePreviewLoading(false);
      return;
    }

    setClonePreviewLoading(true);
    try {
      const response = await fetch(`/v1/builder/clinics/${sourceId}/clone-preview`, {
        method: "GET",
        headers: authHeaders(token),
      });
      if (requestId !== clonePreviewRequestRef.current) return;
      if (!response.ok) {
        setClonePreviewError(clonePreviewErrorMessage(response.status));
        return;
      }
      const preview = await response.json() as ClinicClonePreview;
      if (requestId === clonePreviewRequestRef.current) {
        setClonePreview(preview);
      }
    } catch {
      if (requestId === clonePreviewRequestRef.current) {
        setClonePreviewError(t("builder_clone_preview_error_network"));
      }
    } finally {
      if (requestId === clonePreviewRequestRef.current) {
        setClonePreviewLoading(false);
      }
    }
  }

  async function refreshCloneLists(): Promise<boolean> {
    const headers = authHeaders(token);
    const [clinicsResponse, usageResponse] = await Promise.all([
      fetch("/v1/builder/clinics", { headers }),
      fetch("/v1/billing/usage", { headers }),
    ]);
    if (clinicsResponse.ok) {
      setClinics(await clinicsResponse.json() as BuilderClinic[]);
    }
    if (usageResponse.ok) {
      setUsage(await usageResponse.json() as BillingUsage);
    }
    return clinicsResponse.ok && usageResponse.ok;
  }

  async function handleRetryCloneRefresh() {
    try {
      setCloneRefreshWarning(!(await refreshCloneLists()));
    } catch (refreshError) {
      console.warn("[Builder] Clone refresh retry failed:", refreshError);
      setCloneRefreshWarning(true);
    }
  }

  async function handleCloneClinic() {
    if (
      cloneRequestInFlightRef.current
      || !cloneSourceId
      || !clonePreview
      || !cloneConfirmed
    ) return;

    cloneRequestInFlightRef.current = true;
    setCloning(true);
    setCloneError(null);
    setCloneRefreshWarning(false);
    try {
      const response = await fetch(`/v1/builder/clinics/${cloneSourceId}/clone`, {
        method: "POST",
        headers: authHeaders(token),
      });
      if (!response.ok) {
        setCloneError(cloneErrorMessage(response.status));
        return;
      }
      const result = await response.json() as ClinicCloneResult;
      setCloneResult(result);
      setClonePreview(null);
      setCloneConfirmed(false);
      try {
        setCloneRefreshWarning(!(await refreshCloneLists()));
      } catch (refreshError) {
        console.warn("[Builder] Clone refresh failed:", refreshError);
        setCloneRefreshWarning(true);
      }
    } catch {
      setCloneError(t("builder_clone_error_network"));
    } finally {
      cloneRequestInFlightRef.current = false;
      setCloning(false);
    }
  }

  // Toggle clinic status handler
  async function handleToggleStatus(clinicId: string, currentStatus: string) {
    const newActive = currentStatus !== "active";
    setClinics((prev) =>
      prev.map((c) =>
        c.id === clinicId ? { ...c, status: (newActive ? "active" : "inactive") as "active" | "inactive", is_active: newActive } : c
      )
    );
    try {
      const res = await fetch(`/v1/builder/clinics/${clinicId}/status`, {
        method: "PATCH",
        headers: authHeaders(token),
        body: JSON.stringify({ is_active: newActive }),
      });
      if (!res.ok) {
        setClinics((prev) =>
          prev.map((c) =>
            c.id === clinicId ? { ...c, status: currentStatus as "active" | "inactive", is_active: currentStatus === "active" } : c
          )
        );
        toast({ title: t("admin_toggle_err_title"), description: t("admin_toggle_err"), variant: "destructive" });
        return;
      }
      toast({
        title: newActive ? t("admin_toggle_ok_on") : t("admin_toggle_ok_off"),
        description: `ID ${clinicId}`,
      });
    } catch {
      setClinics((prev) =>
        prev.map((c) =>
          c.id === clinicId ? { ...c, status: currentStatus as "active" | "inactive", is_active: currentStatus === "active" } : c
        )
      );
      toast({ title: t("admin_create_err_network"), variant: "destructive" });
    }
  }

  // Navigate to clinic config
  function handleConfigureClinic(clinicId: string) {
    setViewingClinicId(clinicId);
    navigate("/config");
  }

  // Navigate to clinic panel (simulator)
  function handleAccessClinicPanel(clinicTenantId: string) {
    setViewingClinicId(clinicTenantId);
    navigate("/");
  }

  // Navigate to clinic from alert
  function handleAlertClick(clinicId: string) {
    setActiveTab("clinics");
    setExpandedClinicId(clinicId);
  }

  async function changeClinicPassword(payload: { new_password: string; confirm_password: string }) {
    if (!passwordClinic) return false;
    const response = await fetch(`/v1/builder/clinics/${passwordClinic.id}/password`, {
      method: "PATCH",
      headers: authHeaders(token),
      body: JSON.stringify(payload),
    });
    return response.ok;
  }

  async function handleSaveClinicEmail(clinic: BuilderClinic) {
    if (emailRequestInFlightRef.current.has(clinic.id)) return;

    emailRequestInFlightRef.current.add(clinic.id);
    setSavingEmailClinicId(clinic.id);
    setEmailErrors((prev) => ({ ...prev, [clinic.id]: undefined }));
    try {
      const response = await fetch(`/v1/builder/clinics/${clinic.id}/email`, {
        method: "PATCH",
        headers: authHeaders(token),
        body: JSON.stringify({ email: (emailDrafts[clinic.id] ?? clinic.admin_email ?? "").trim() }),
      });
      if (!response.ok) {
        setEmailErrors((prev) => ({ ...prev, [clinic.id]: t("builder_clinic_email_error") }));
        return;
      }

      const data = await response.json() as { ok: true; admin_email: string };
      setClinics((prev) =>
        prev.map((current) =>
          current.id === clinic.id ? { ...current, admin_email: data.admin_email } : current,
        ),
      );
      setEmailDrafts((prev) => ({ ...prev, [clinic.id]: data.admin_email }));
      toast({ title: t("builder_clinic_email_saved") });
    } catch {
      setEmailErrors((prev) => ({ ...prev, [clinic.id]: t("builder_clinic_email_error") }));
    } finally {
      emailRequestInFlightRef.current.delete(clinic.id);
      setSavingEmailClinicId((current) => current === clinic.id ? null : current);
    }
  }

  // Save contact_phone for a clinic (inline edit surface)
  async function handleSavePhone(clinic: BuilderClinic) {
    const draft = phoneDrafts[clinic.id] ?? "";
    const trimmed = draft.trim();
    const previous = clinic.contact_phone;
    const newValue = trimmed ? trimmed : null;
    // Optimistic update
    setClinics((prev) =>
      prev.map((c) => (c.id === clinic.id ? { ...c, contact_phone: newValue } : c)),
    );
    setSavingPhoneClinicId(clinic.id);
    try {
      const res = await fetch(`/v1/builder/clinics/${clinic.id}`, {
        method: "PATCH",
        headers: authHeaders(token),
        body: JSON.stringify({ contact_phone: newValue }),
      });
      if (!res.ok) {
        setClinics((prev) =>
          prev.map((c) => (c.id === clinic.id ? { ...c, contact_phone: previous } : c)),
        );
        toast({ title: t("builder_clinic_phone_error"), variant: "destructive" });
        return;
      }
      const data = await res.json() as { contact_phone: string | null };
      // Sync with server-normalized value (backend strips non-digits)
      setClinics((prev) =>
        prev.map((c) => (c.id === clinic.id ? { ...c, contact_phone: data.contact_phone } : c)),
      );
      setPhoneDrafts((prev) => ({ ...prev, [clinic.id]: data.contact_phone ?? "" }));
      toast({ title: t("builder_clinic_phone_saved") });
    } catch {
      setClinics((prev) =>
        prev.map((c) => (c.id === clinic.id ? { ...c, contact_phone: previous } : c)),
      );
      toast({ title: t("builder_clinic_phone_error"), variant: "destructive" });
    } finally {
      setSavingPhoneClinicId(null);
    }
  }

  // Optimistically flip a ticket's status
  async function handleToggleTicketStatus(ticketId: number, currentStatus: string) {
    const nextStatus = currentStatus === "open" ? "closed" : "open";
    setTickets((prev) =>
      prev.map((tk) => (tk.id === ticketId ? { ...tk, status: nextStatus } : tk)),
    );
    try {
      const res = await fetch(`/v1/builder/tickets/${ticketId}/status`, {
        method: "PATCH",
        headers: authHeaders(token),
        body: JSON.stringify({ status: nextStatus }),
      });
      if (!res.ok) {
        setTickets((prev) =>
          prev.map((tk) => (tk.id === ticketId ? { ...tk, status: currentStatus } : tk)),
        );
        toast({ title: t("builder_support_resolve_error"), variant: "destructive" });
        return;
      }
      toast({ title: t("builder_support_resolve_ok") });
    } catch {
      setTickets((prev) =>
        prev.map((tk) => (tk.id === ticketId ? { ...tk, status: currentStatus } : tk)),
      );
      toast({ title: t("builder_support_resolve_error"), variant: "destructive" });
    }
  }

  // Save notification email
  async function handleSaveSupportEmail() {
    const trimmed = supportEmailDraft.trim();
    const value = trimmed ? trimmed : null;
    setSavingSupportEmail(true);
    try {
      const res = await fetch("/v1/builder/profile", {
        method: "PATCH",
        headers: authHeaders(token),
        body: JSON.stringify({ support_email: value }),
      });
      if (!res.ok) {
        toast({ title: t("builder_support_email_error"), variant: "destructive" });
        return;
      }
      const data = await res.json() as { support_email: string | null };
      setSupportEmailDraft(data.support_email ?? "");
      setProfile((prev) => (prev ? { ...prev, support_email: data.support_email } : prev));
      toast({ title: t("builder_support_email_saved") });
    } catch {
      toast({ title: t("builder_support_email_error"), variant: "destructive" });
    } finally {
      setSavingSupportEmail(false);
    }
  }

  const filteredTickets = useMemo(() => {
    if (ticketStatusFilter === "all") return tickets;
    return tickets.filter((tk) => tk.status === ticketStatusFilter);
  }, [tickets, ticketStatusFilter]);

  // Guard: only builders
  if (!isBuilder) {
    return (
      <div className="flex items-center justify-center h-64 text-muted-foreground">
        <Wrench className="h-8 w-8 mr-2" /> Acesso restrito a Builders.
      </div>
    );
  }

  const stats = dashboard?.stats;

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Wrench className="h-5 w-5 text-indigo-400" />
            <h1 className="text-xl font-bold">{t("builder_panel_title")}</h1>
          </div>
          <p className="text-sm text-muted-foreground mt-0.5">
            {profile?.name ?? "..."}
          </p>
        </div>
        <StatusBadge status={profile?.status ?? "active"} />
      </div>

      {/* Loading state */}
      {loading && (
        <div className="flex items-center gap-2 text-muted-foreground py-8 justify-center">
          <Loader2 className="h-5 w-5 animate-spin" /> {t("admin_loading")}
        </div>
      )}

      {/* Error state */}
      {error && (
        <div className="flex items-center gap-2 text-destructive py-4">
          <AlertTriangle className="h-4 w-4" /> {error}
        </div>
      )}

      {/* Partial load warning */}
      {!loading && !error && !dashboard && (
        <div className="flex items-center gap-2 text-amber-500 py-4">
          <AlertCircle className="h-4 w-4" />
          <span className="text-sm">Falha ao carregar dashboard. Tente sair e entrar novamente.</span>
        </div>
      )}

      {/* Tabs */}
      {!loading && !error && (
        <>
        <Tabs value={activeTab} onValueChange={setActiveTab}>
          <TabsList>
            <TabsTrigger value="overview">{t("builder_tab_overview")}</TabsTrigger>
            <TabsTrigger value="clinics">{t("builder_tab_clinics")}</TabsTrigger>
            <TabsTrigger value="cloning-center">{t("builder_tab_cloning_center")}</TabsTrigger>
            <TabsTrigger value="support">{t("builder_tab_support")}</TabsTrigger>
          </TabsList>

          {/* ══════════════════════════════════════════════════════════════════
              VISAO GLOBAL TAB
             ══════════════════════════════════════════════════════════════════ */}
          <TabsContent value="overview" className="space-y-6 mt-4">
            {usage && <PlanUsageSummary usage={usage} locale={locale} />}

            <Card className="border-border" data-testid="builder-roadmap-certification-card">
              <CardContent className="flex items-center justify-between gap-3 py-4">
                <p className="text-sm font-semibold text-foreground">
                  {t("builder_roadmap_certification_title")}
                </p>
                <Badge variant="outline">{t("builder_roadmap_coming_soon")}</Badge>
              </CardContent>
            </Card>

            {/* KPI row — 4 cards with trend */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <Card className="border-border">
                <CardContent className="pt-4 pb-3">
                  <p className="text-[11px] text-muted-foreground">{t("builder_total_clinics")}</p>
                  <p className="text-2xl font-bold text-foreground">{stats?.clinic_count ?? 0}</p>
                </CardContent>
              </Card>
              <Card className="border-border">
                <CardContent className="pt-4 pb-3">
                  <p className="text-[11px] text-muted-foreground">{t("builder_active")}</p>
                  <p className="text-2xl font-bold text-emerald-400">{stats?.active_clinics ?? 0}</p>
                </CardContent>
              </Card>
              <Card className="border-border">
                <CardContent className="pt-4 pb-3">
                  <p className="text-[11px] text-muted-foreground">{t("builder_inactive")}</p>
                  <p className="text-2xl font-bold text-slate-400">{stats?.inactive_clinics ?? 0}</p>
                </CardContent>
              </Card>
              <Card className="border-border bg-gradient-to-br from-indigo-950/20 to-transparent">
                <CardContent className="pt-4 pb-3">
                  <div className="flex items-center gap-1.5 mb-1">
                    <Activity className="h-3.5 w-3.5 text-indigo-400" />
                    <p className="text-[11px] text-muted-foreground">{t("builder_total_traces")}</p>
                  </div>
                  <div className="flex items-center gap-2">
                    <p className="text-2xl font-bold text-indigo-400">
                      {stats?.total_traces?.toLocaleString("pt-BR") ?? 0}
                    </p>
                    {trend.direction !== "flat" && (
                      <span className={`flex items-center gap-0.5 text-[10px] ${trend.direction === "up" ? "text-emerald-400" : "text-red-400"}`}>
                        {trend.direction === "up" ? <TrendingUp className="h-3 w-3" /> : <TrendingDown className="h-3 w-3" />}
                        {Math.abs(trend.diff)}
                      </span>
                    )}
                  </div>
                </CardContent>
              </Card>
            </div>

            {/* LLM Token Consumption */}
            {(stats?.total_tokens ?? 0) > 0 && (
              <Card className="border-border">
                <CardHeader className="pb-2">
                  <div className="flex items-center gap-2">
                    <Zap className="h-4 w-4 text-amber-400" />
                    <CardTitle className="text-sm">{t("builder_token_title")}</CardTitle>
                  </div>
                </CardHeader>
                <CardContent>
                  <p className="text-2xl font-bold text-foreground mb-3">
                    {(stats?.total_tokens ?? 0).toLocaleString("pt-BR")} tokens
                  </p>
                  {stats?.model_breakdown && stats.model_breakdown.length > 0 && (
                    <div className="space-y-2 pt-2 border-t border-border">
                      {stats.model_breakdown.map(({ model, tokens: toks }) => {
                        const maxToks = stats.model_breakdown[0]?.tokens ?? 1;
                        const pct = (toks / maxToks) * 100;
                        return (
                          <div key={model} className="space-y-1">
                            <div className="flex items-center justify-between gap-2">
                              <span className="text-[11px] font-medium text-foreground truncate max-w-[200px]" title={model}>{model}</span>
                              <span className="text-[11px] text-muted-foreground shrink-0 tabular-nums">{toks.toLocaleString("pt-BR")}</span>
                            </div>
                            <div className="h-1 w-full rounded-full bg-muted overflow-hidden">
                              <div className="h-full bg-amber-400 rounded-full transition-all duration-500" style={{ width: `${pct}%` }} />
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </CardContent>
              </Card>
            )}

            {/* Traces chart */}
            <Card className="border-border">
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">{t("builder_traces_chart_title")}</CardTitle>
              </CardHeader>
              <CardContent>
                {dashboard?.time_series ? (
                  <ResponsiveContainer width="100%" height={220}>
                    <LineChart data={dashboard.time_series}>
                      <XAxis
                        dataKey="date"
                        tickFormatter={(v: string) => { const p = v.split("-"); return `${p[2]}/${p[1]}`; }}
                        stroke="#64748b"
                        fontSize={10}
                        tickLine={false}
                        axisLine={false}
                      />
                      <YAxis stroke="#64748b" fontSize={10} tickLine={false} axisLine={false} width={30} />
                      <Tooltip content={<ChartTooltip />} />
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
                ) : (
                  <Skeleton className="h-[220px] w-full" />
                )}
              </CardContent>
            </Card>

            {/* Alerts */}
            <Card className="border-border">
              <CardHeader className="pb-2">
                <CardTitle className="text-sm flex items-center gap-2">
                  <AlertTriangle className="h-4 w-4 text-amber-400" />
                  {t("builder_alerts_title")}
                  {(dashboard?.alerts?.length ?? 0) > 0 && (
                    <Badge variant="outline" className="text-[10px]">{dashboard?.alerts.length}</Badge>
                  )}
                </CardTitle>
              </CardHeader>
              <CardContent>
                {!dashboard?.alerts || dashboard.alerts.length === 0 ? (
                  <div className="flex items-center gap-2 text-emerald-400 text-sm py-3">
                    <CheckCircle2 className="h-4 w-4" /> {t("builder_alerts_empty")}
                  </div>
                ) : (
                  <div className="space-y-2">
                    {dashboard.alerts.map((alert, i) => (
                      <button
                        key={`${alert.clinic_id}-${alert.type}-${i}`}
                        className="flex items-start gap-3 w-full text-left p-2.5 rounded-lg border border-border hover:bg-muted/50 transition-colors"
                        onClick={() => handleAlertClick(alert.clinic_id)}
                      >
                        {alert.severity === "critical" ? (
                          <AlertTriangle className="h-4 w-4 text-red-400 mt-0.5 shrink-0" />
                        ) : alert.severity === "warning" ? (
                          <AlertCircle className="h-4 w-4 text-amber-400 mt-0.5 shrink-0" />
                        ) : (
                          <Info className="h-4 w-4 text-blue-400 mt-0.5 shrink-0" />
                        )}
                        <div className="min-w-0">
                          <p className="text-sm font-medium text-foreground">{alert.clinic_name}</p>
                          <p className="text-xs text-muted-foreground">{alert.message}</p>
                        </div>
                        {alert.type === "api_error" && (
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-6 text-[10px] ml-auto shrink-0"
                            onClick={(e) => { e.stopPropagation(); handleConfigureClinic(alert.clinic_id); }}
                          >
                            <Sliders className="h-3 w-3 mr-1" /> {t("builder_configure")}
                          </Button>
                        )}
                      </button>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>

            {/* Compact clinic health list */}
            {clinics.length > 0 && (
              <Card className="border-border">
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">{t("builder_health_grid_title")}</CardTitle>
                  <CardDescription className="text-[10px]">{t("builder_health_legend")}</CardDescription>
                </CardHeader>
                <CardContent>
                  <div className="divide-y divide-border/50 rounded-md border border-border/50">
                    {sortedClinics.map((clinic) => (
                      <button
                        key={clinic.id}
                        className="flex w-full items-center gap-2 px-2.5 py-2 hover:bg-muted/50 transition-colors text-left"
                        onClick={() => { setActiveTab("clinics"); setExpandedClinicId(clinic.id); }}
                      >
                        <HealthDot status={clinic.health_status} />
                        <div className="min-w-0">
                          <p className="text-xs font-medium truncate">{clinic.name}</p>
                          <p className="text-[10px] text-muted-foreground">
                            {t("builder_last_trace")}: {relativeTime(clinic.last_trace_at, locale)}
                          </p>
                        </div>
                      </button>
                    ))}
                  </div>
                </CardContent>
              </Card>
            )}
          </TabsContent>

          {/* ══════════════════════════════════════════════════════════════════
              SUB-CONTAS TAB
             ══════════════════════════════════════════════════════════════════ */}
          <TabsContent value="clinics" className="space-y-4 mt-4">
            {/* Create clinic section */}
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold flex items-center gap-2">
                <Building2 className="h-4 w-4 text-muted-foreground" />
                {t("builder_total_clinics")}
                <Badge variant="outline" className="text-[10px]">
                  {clinics.length}
                </Badge>
              </h2>
              <Button
                size="sm"
                className="gap-2"
                onClick={() => setShowCreateForm(!showCreateForm)}
              >
                <Plus className="h-4 w-4" /> {t("builder_new_clinic")}
              </Button>
            </div>

            {/* Create clinic form */}
            {showCreateForm && (
              <Card className="border-dashed border-indigo-500/40 bg-indigo-500/5">
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm text-indigo-400">{t("builder_new_clinic")}</CardTitle>
                  <CardDescription className="text-xs">
                    {t("admin_create_desc")}
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div className="space-y-1">
                      <Label className="text-xs">{t("admin_create_name_label")}</Label>
                      <Input
                        value={newName}
                        onChange={(e) => setNewName(e.target.value)}
                        placeholder={t("admin_create_name_ph")}
                        className="h-8 text-xs"
                      />
                    </div>
                    <div className="space-y-1">
                      <Label className="text-xs">{t("admin_create_email_label")}</Label>
                      <Input
                        type="email"
                        value={newEmail}
                        onChange={(e) => setNewEmail(e.target.value)}
                        placeholder={t("admin_create_email_ph")}
                        className="h-8 text-xs"
                      />
                    </div>
                    <div className="space-y-1">
                      <Label className="text-xs">{t("admin_create_pass_label")}</Label>
                      <Input
                        type="password"
                        value={newPassword}
                        onChange={(e) => setNewPassword(e.target.value)}
                        placeholder="••••••••"
                        className="h-8 text-xs"
                      />
                    </div>
                    <div className="space-y-1">
                      <Label className="text-xs">{t("builder_clinic_phone_label")}</Label>
                      <Input
                        type="tel"
                        value={newPhone}
                        onChange={(e) => setNewPhone(e.target.value)}
                        placeholder={t("builder_clinic_phone_placeholder")}
                        className="h-8 text-xs"
                        data-testid="input-new-clinic-phone"
                      />
                    </div>
                  </div>
                  {createError && (
                    <p className="text-sm text-destructive flex items-center gap-1.5">
                      <AlertTriangle className="h-3.5 w-3.5" /> {createError}
                    </p>
                  )}
                  <div className="flex gap-2">
                    <Button size="sm" className="gap-1.5" onClick={() => void handleCreateClinic()} disabled={creating}>
                      {creating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
                      {t("admin_create_submit")}
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => { setShowCreateForm(false); setCreateError(null); }}>
                      {t("admin_create_cancel")}
                    </Button>
                  </div>
                </CardContent>
              </Card>
            )}

            {clinics.length > 0 && (
              <div className="space-y-1">
                <Label htmlFor="builder-clinic-search" className="text-xs">
                  {t("label_search_clinics")}
                </Label>
                <Input
                  id="builder-clinic-search"
                  type="search"
                  value={clinicSearchQuery}
                  onChange={(event) => setClinicSearchQuery(event.target.value)}
                  placeholder={t("placeholder_search_clinics")}
                  className="h-9 text-sm"
                />
              </div>
            )}

            {/* Empty state */}
            {clinics.length === 0 && (
              <Card className="border-dashed">
                <CardContent className="flex flex-col items-center justify-center py-12 text-center text-muted-foreground gap-3">
                  <Inbox className="h-10 w-10 opacity-25" />
                  <p className="text-sm font-medium">{t("builder_empty")}</p>
                  <Button
                    size="sm"
                    variant="outline"
                    className="gap-1.5"
                    onClick={() => setShowCreateForm(true)}
                  >
                    <Plus className="h-3.5 w-3.5" /> {t("builder_new_clinic")}
                  </Button>
                </CardContent>
              </Card>
            )}

            {clinics.length > 0 && normalizedClinicSearchQuery && filteredClinics.length === 0 && (
              <Card className="border-dashed">
                <CardContent className="py-8 text-center text-sm text-muted-foreground">
                  {t("clinic_search_no_results")}
                </CardContent>
              </Card>
            )}

            {/* Clinic list */}
            {filteredClinics.length > 0 && (
              <div className="space-y-3">
                {filteredClinics.map((clinic) => (
                  <Collapsible
                    key={clinic.id}
                    open={expandedClinicId === clinic.id}
                    onOpenChange={(open) => setExpandedClinicId(open ? clinic.id : null)}
                  >
                    <Card
                      className={`border ${
                        clinic.status === "active"
                          ? "border-border"
                          : "border-dashed border-slate-700 opacity-60"
                      }`}
                    >
                      <CardHeader className="pb-2">
                        <div className="flex items-start justify-between gap-2">
                          <div className="flex items-center gap-2 min-w-0">
                            <HealthDot status={clinic.health_status} />
                            <div className="min-w-0">
                              <CardTitle className="text-sm truncate">{clinic.name}</CardTitle>
                              <CardDescription className="text-[10px]">
                                {t("builder_last_trace")}: {relativeTime(clinic.last_trace_at, locale)}
                              </CardDescription>
                            </div>
                          </div>
                          <div className="flex items-center gap-1.5 shrink-0">
                            <PlanBadge plan={clinic.plan_type} />
                            <StatusBadge status={clinic.status} />
                            <CollapsibleTrigger asChild>
                              <Button variant="ghost" size="sm" className="h-7 w-7 p-0">
                                {expandedClinicId === clinic.id ? (
                                  <ChevronUp className="h-4 w-4" />
                                ) : (
                                  <ChevronDown className="h-4 w-4" />
                                )}
                              </Button>
                            </CollapsibleTrigger>
                          </div>
                        </div>
                      </CardHeader>
                      <CardContent className="space-y-3">
                        <div className="flex items-center gap-3 text-xs text-muted-foreground">
                          <span className="flex items-center gap-1">
                            <Activity className="h-3 w-3" />
                            {clinic.trace_count_7d} traces (7d)
                          </span>
                          <span className={`flex items-center gap-1 ml-auto ${clinic.config_completeness > 0 ? "text-emerald-400" : "text-slate-500"}`}>
                            <KeyRound className="h-3 w-3" />
                            {clinic.config_completeness > 0 ? "API ok" : "API --"}
                          </span>
                        </div>

                        {/* Expandable detail */}
                        <CollapsibleContent className="space-y-3 pt-2 border-t border-border">
                          {/* Mini stats row */}
                          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                            <div className="text-center p-2 rounded-lg bg-muted/30">
                              <p className="text-lg font-bold text-foreground">{clinic.trace_count_7d}</p>
                              <p className="text-[10px] text-muted-foreground">{t("builder_traces_7d")}</p>
                            </div>
                            <div className="text-center p-2 rounded-lg bg-muted/30">
                              <p className="text-lg font-bold text-amber-400">{clinic.safety_flags_7d}</p>
                              <p className="text-[10px] text-muted-foreground">{t("builder_safety_7d")}</p>
                            </div>
                            <div className="text-center p-2 rounded-lg bg-muted/30">
                              <p className="text-lg font-bold text-blue-400">{clinic.handoff_count_7d}</p>
                              <p className="text-[10px] text-muted-foreground">{t("builder_handoffs_7d")}</p>
                            </div>
                            <OutsideHoursClinicMetric clinic={clinic} locale={locale} />
                          </div>

                          {/* Config completeness */}
                          <div className="space-y-1">
                            <div className="flex items-center justify-between text-xs">
                              <span className="text-muted-foreground">{t("builder_config_completeness")}</span>
                              <span className="font-medium">{clinic.config_completeness}%</span>
                            </div>
                            <Progress value={clinic.config_completeness} className="h-2" />
                          </div>

                          {/* Login email editor */}
                          <div className="space-y-1">
                            <Label htmlFor={`clinic-email-${clinic.id}`} className="text-xs">
                              {t("builder_clinic_email_label")}
                            </Label>
                            <div className="flex gap-2">
                              <Input
                                id={`clinic-email-${clinic.id}`}
                                type="email"
                                value={emailDrafts[clinic.id] ?? clinic.admin_email ?? ""}
                                onChange={(event) =>
                                  setEmailDrafts((prev) => ({ ...prev, [clinic.id]: event.target.value }))
                                }
                                placeholder={t("builder_clinic_email_placeholder")}
                                className="h-8 text-xs"
                                data-testid={`input-clinic-email-${clinic.id}`}
                              />
                              <Button
                                size="sm"
                                variant="outline"
                                className="h-8 text-xs"
                                onClick={() => void handleSaveClinicEmail(clinic)}
                                disabled={savingEmailClinicId === clinic.id}
                                data-testid={`btn-save-clinic-email-${clinic.id}`}
                              >
                                {savingEmailClinicId === clinic.id ? (
                                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                                ) : (
                                  t("builder_clinic_email_save")
                                )}
                              </Button>
                            </div>
                            {emailErrors[clinic.id] && (
                              <p className="text-xs text-destructive" role="alert">
                                {emailErrors[clinic.id]}
                              </p>
                            )}
                          </div>

                          {/* Contact phone editor */}
                          <div className="space-y-1">
                            <Label className="text-xs">{t("builder_clinic_phone_label")}</Label>
                            <div className="flex gap-2">
                              <Input
                                type="tel"
                                value={phoneDrafts[clinic.id] ?? clinic.contact_phone ?? ""}
                                onChange={(e) =>
                                  setPhoneDrafts((prev) => ({ ...prev, [clinic.id]: e.target.value }))
                                }
                                placeholder={t("builder_clinic_phone_placeholder")}
                                className="h-8 text-xs"
                                data-testid={`input-clinic-phone-${clinic.id}`}
                              />
                              <Button
                                size="sm"
                                variant="outline"
                                className="h-8 text-xs"
                                onClick={() => void handleSavePhone(clinic)}
                                disabled={savingPhoneClinicId === clinic.id}
                                data-testid={`btn-save-clinic-phone-${clinic.id}`}
                              >
                                {savingPhoneClinicId === clinic.id ? (
                                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                                ) : (
                                  t("builder_clinic_phone_save")
                                )}
                              </Button>
                            </div>
                            <p className="text-[10px] text-muted-foreground">
                              {t("builder_clinic_phone_hint")}
                            </p>
                          </div>

                          {/* Actions stay hidden with the clinic details. */}
                          <div className="flex items-center gap-2 pt-1">
                            <Button
                              variant="outline"
                              size="sm"
                              className="h-7 text-xs gap-1.5 flex-1"
                              onClick={() => void handleToggleStatus(clinic.id, clinic.status)}
                            >
                              {clinic.status === "active" ? (
                                <><ToggleRight className="h-3.5 w-3.5 text-emerald-400" /> {t("admin_toggle_deactivate")}</>
                              ) : (
                                <><ToggleLeft className="h-3.5 w-3.5" /> {t("admin_toggle_activate")}</>
                              )}
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-7 text-xs gap-1.5 flex-1"
                              onClick={() => handleAccessClinicPanel(clinic.id)}
                            >
                              <ExternalLink className="h-3.5 w-3.5" /> Acessar Painel
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-7 text-xs gap-1.5 flex-1"
                              onClick={() => handleConfigureClinic(clinic.id)}
                            >
                              <Sliders className="h-3.5 w-3.5" /> {t("builder_configure")}
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-7 text-xs gap-1.5 flex-1"
                              onClick={() => setPasswordClinic(clinic)}
                              data-testid={`btn-change-clinic-password-${clinic.id}`}
                            >
                              <KeyRound className="h-3.5 w-3.5" /> {t("builder_change_clinic_password")}
                            </Button>
                          </div>
                        </CollapsibleContent>
                      </CardContent>
                    </Card>
                  </Collapsible>
                ))}
              </div>
            )}
          </TabsContent>

          {/* ══════════════════════════════════════════════════════════════════
              CLONING CENTER TAB
             ══════════════════════════════════════════════════════════════════ */}
          <TabsContent value="cloning-center" className="space-y-4 mt-4">
            <Card className="border-border">
              <CardHeader>
                <CardTitle className="text-base">{t("builder_clone_title")}</CardTitle>
                <CardDescription>{t("builder_clone_description")}</CardDescription>
              </CardHeader>
              <CardContent className="space-y-5">
                {cloneResult ? (
                  <div className="space-y-4" role="status" aria-live="polite">
                    <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/5 p-4">
                      <div className="flex items-start gap-3">
                        <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-emerald-400" />
                        <div className="space-y-1">
                          <h2 className="font-semibold text-foreground">{t("builder_clone_success")}</h2>
                          <p className="text-sm text-muted-foreground">{t("builder_clone_success_desc")}</p>
                          <p className="pt-2 font-medium text-foreground">{cloneResult.name}</p>
                          <p className="break-all text-xs text-muted-foreground">ID: {cloneResult.id}</p>
                          <p className="text-xs font-medium text-slate-400">{t("builder_clone_inactive")}</p>
                        </div>
                      </div>
                    </div>
                    {cloneRefreshWarning && (
                      <div className="flex flex-wrap items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-sm text-amber-400" role="alert">
                        <AlertTriangle className="h-4 w-4 shrink-0" />
                        <span>{t("builder_clone_refresh_warning")}</span>
                        <Button size="sm" variant="outline" onClick={() => void handleRetryCloneRefresh()}>
                          {t("builder_clone_refresh_retry")}
                        </Button>
                      </div>
                    )}
                    <Button onClick={() => handleConfigureClinic(cloneResult.id)} className="gap-2">
                      <Sliders className="h-4 w-4" /> {t("builder_clone_configure")}
                    </Button>
                  </div>
                ) : (
                  <>
                    <div className="space-y-2">
                      <Label htmlFor="clone-source">{t("builder_clone_source_label")}</Label>
                      <select
                        id="clone-source"
                        value={cloneSourceId}
                        onChange={(event) => void handleCloneSourceChange(event.target.value)}
                        disabled={cloning || clinics.length === 0}
                        className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        <option value="">{t("builder_clone_source_placeholder")}</option>
                        {clinics.map((clinic) => (
                          <option key={clinic.id} value={clinic.id}>{clinic.name}</option>
                        ))}
                      </select>
                      {clinics.length === 0 && (
                        <p className="text-sm text-muted-foreground">{t("builder_clone_source_empty")}</p>
                      )}
                    </div>

                    {clonePreviewLoading && (
                      <div className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
                        <Loader2 className="h-4 w-4 animate-spin" /> {t("builder_clone_preview_loading")}
                      </div>
                    )}

                    {clonePreviewError && (
                      <p className="flex items-center gap-2 text-sm text-destructive" role="alert">
                        <AlertTriangle className="h-4 w-4 shrink-0" /> {clonePreviewError}
                      </p>
                    )}

                    {clonePreview && (
                      <div className="space-y-5" aria-live="polite">
                        <div className="rounded-lg border border-border bg-muted/20 p-4 space-y-3">
                          <h2 className="font-semibold text-foreground">{t("builder_clone_preview_title")}</h2>
                          <div>
                            <p className="text-xs text-muted-foreground">{t("builder_clone_suggested_name")}</p>
                            <p className="font-medium">{clonePreview.suggested_name}</p>
                            <p className="mt-1 text-xs text-muted-foreground">{t("builder_clone_advisory_note")}</p>
                          </div>
                          <dl className="grid grid-cols-2 gap-3 text-sm">
                            <div>
                              <dt className="text-xs text-muted-foreground">{t("builder_clone_rag_documents")}</dt>
                              <dd className="font-semibold">{clonePreview.document_count.toLocaleString(locale === "pt" ? "pt-BR" : "en-US")}</dd>
                            </div>
                            <div>
                              <dt className="text-xs text-muted-foreground">{t("builder_clone_rag_bytes")}</dt>
                              <dd className="font-semibold">{clonePreview.total_bytes.toLocaleString(locale === "pt" ? "pt-BR" : "en-US")} B</dd>
                            </div>
                          </dl>
                          <p className="text-xs text-amber-400">{t("builder_clone_rag_impact")}</p>
                        </div>

                        <div className="grid gap-3 md:grid-cols-2">
                          <section className="rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-4" aria-labelledby="clone-included-title">
                            <h2 id="clone-included-title" className="font-semibold text-emerald-400">{t("builder_clone_included_title")}</h2>
                            <p className="mt-2 text-sm text-muted-foreground">{t("builder_clone_included_copy")}</p>
                          </section>
                          <section className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-4" aria-labelledby="clone-excluded-title">
                            <h2 id="clone-excluded-title" className="font-semibold text-amber-400">{t("builder_clone_excluded_title")}</h2>
                            <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-muted-foreground">
                              <li>{t("builder_clone_excluded_admin")}</li>
                              <li>{t("builder_clone_excluded_integrations")}</li>
                              <li>{t("builder_clone_inactive")}</li>
                            </ul>
                          </section>
                        </div>

                        {cloneError && (
                          <p className="flex items-center gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive" role="alert">
                            <AlertTriangle className="h-4 w-4 shrink-0" /> {cloneError}
                          </p>
                        )}

                        <div className="space-y-3 border-t border-border pt-4">
                          <label className="flex cursor-pointer items-start gap-3 text-sm">
                            <input
                              type="checkbox"
                              checked={cloneConfirmed}
                              onChange={(event) => setCloneConfirmed(event.target.checked)}
                              disabled={cloning}
                              className="mt-0.5 h-4 w-4 rounded border-input accent-indigo-500"
                            />
                            <span>{t("builder_clone_confirm")}</span>
                          </label>
                          <Button
                            onClick={() => void handleCloneClinic()}
                            disabled={!cloneConfirmed || cloning}
                            className="gap-2"
                          >
                            {cloning ? <Loader2 className="h-4 w-4 animate-spin" /> : <Building2 className="h-4 w-4" />}
                            {cloning ? t("builder_clone_loading") : t("builder_clone_submit")}
                          </Button>
                        </div>
                      </div>
                    )}
                  </>
                )}
              </CardContent>
            </Card>
          </TabsContent>

          {/* ══════════════════════════════════════════════════════════════════
              SUPPORT TAB
             ══════════════════════════════════════════════════════════════════ */}
          <TabsContent value="support" className="space-y-4 mt-4">
            {/* Notification email card */}
            <Card className="border-border">
              <CardHeader className="pb-2">
                <div className="flex items-center gap-2">
                  <Mail className="h-4 w-4 text-indigo-400" />
                  <CardTitle className="text-sm">{t("builder_support_email_title")}</CardTitle>
                </div>
                <CardDescription className="text-xs">
                  {t("builder_support_email_description")}
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-2">
                <Label className="text-xs" htmlFor="support-email-input">
                  {t("builder_support_email_label")}
                </Label>
                <div className="flex gap-2">
                  <Input
                    id="support-email-input"
                    type="email"
                    value={supportEmailDraft}
                    onChange={(e) => setSupportEmailDraft(e.target.value)}
                    placeholder={t("builder_support_email_placeholder")}
                    className="h-8 text-xs"
                    data-testid="input-support-email"
                  />
                  <Button
                    size="sm"
                    onClick={() => void handleSaveSupportEmail()}
                    disabled={savingSupportEmail}
                    data-testid="btn-save-support-email"
                  >
                    {savingSupportEmail ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      t("builder_support_email_save")
                    )}
                  </Button>
                </div>
                <p className="text-[10px] text-muted-foreground">
                  {t("builder_support_email_hint")}
                </p>
                {(!profile?.support_email || profile.support_email === "") && (
                  <p className="text-xs text-muted-foreground" data-testid="support-email-fallback">
                    {t("builder_support_email_fallback").replace("{email}", authEmail ?? "—")}
                  </p>
                )}
              </CardContent>
            </Card>

            {/* Tickets list */}
            <Card className="border-border">
              <CardHeader className="pb-2">
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <LifeBuoy className="h-4 w-4 text-indigo-400" />
                    <CardTitle className="text-sm">{t("builder_support_title")}</CardTitle>
                    <Badge variant="outline" className="text-[10px]">{tickets.length}</Badge>
                  </div>
                  <div className="flex items-center gap-1">
                    {(["open", "closed", "all"] as const).map((flt) => (
                      <Button
                        key={flt}
                        size="sm"
                        variant={ticketStatusFilter === flt ? "default" : "outline"}
                        className="h-7 text-[11px] px-2"
                        onClick={() => setTicketStatusFilter(flt)}
                        data-testid={`btn-ticket-filter-${flt}`}
                      >
                        {flt === "all"
                          ? t("builder_support_filter_all")
                          : flt === "open"
                            ? t("builder_support_filter_open")
                            : t("builder_support_filter_closed")}
                      </Button>
                    ))}
                  </div>
                </div>
                <CardDescription className="text-xs">
                  {t("builder_support_description")}
                </CardDescription>
              </CardHeader>
              <CardContent>
                {ticketsError && (
                  <p className="text-xs text-destructive flex items-center gap-1.5 mb-2">
                    <AlertTriangle className="h-3.5 w-3.5" /> {ticketsError}
                  </p>
                )}
                {filteredTickets.length === 0 ? (
                  <div className="flex flex-col items-center justify-center py-10 text-center text-muted-foreground gap-2">
                    <Inbox className="h-10 w-10 opacity-25" />
                    <p className="text-sm font-medium" data-testid="support-empty">{t("builder_support_empty")}</p>
                  </div>
                ) : (
                  <div className="space-y-2">
                    {filteredTickets.map((ticket) => {
                      const digits = (ticket.contact_phone ?? "").replace(/\D/g, "");
                      const hasPhone = digits.length > 0;
                      const isOpen = ticket.status === "open";
                      const dateLabel = new Date(ticket.created_at).toLocaleString(
                        locale === "pt" ? "pt-BR" : "en-US",
                      );
                      return (
                        <div
                          key={ticket.id}
                          className="rounded-lg border border-border p-3 space-y-2"
                          data-testid={`ticket-row-${ticket.id}`}
                        >
                          <div className="flex items-start justify-between gap-2">
                            <div className="min-w-0">
                              <p className="text-sm font-medium truncate">
                                {ticket.clinic_name ?? "—"}
                              </p>
                              <p className="text-[10px] text-muted-foreground">
                                {ticket.tipo} · {dateLabel}
                              </p>
                            </div>
                            <Badge
                              variant="outline"
                              className={`text-[10px] ${
                                isOpen
                                  ? "border-amber-500/40 text-amber-400"
                                  : "border-emerald-500/40 text-emerald-400"
                              }`}
                              data-testid={`ticket-status-${ticket.id}`}
                            >
                              {isOpen ? t("builder_support_status_open") : t("builder_support_status_closed")}
                            </Badge>
                          </div>
                          <p className="text-xs text-foreground whitespace-pre-wrap break-words">
                            {ticket.mensagem}
                          </p>
                          <div className="flex flex-wrap items-center gap-2 pt-1">
                            <Button
                              size="sm"
                              variant="outline"
                              className="h-7 text-xs gap-1.5"
                              onClick={() => void handleToggleTicketStatus(ticket.id, ticket.status)}
                              data-testid={`btn-ticket-toggle-${ticket.id}`}
                            >
                              {isOpen ? (
                                <><CheckCircle2 className="h-3.5 w-3.5" /> {t("builder_support_action_resolve")}</>
                              ) : (
                                <><ToggleLeft className="h-3.5 w-3.5" /> {t("builder_support_action_reopen")}</>
                              )}
                            </Button>
                            {hasPhone ? (
                              <a
                                href={`https://wa.me/${digits}`}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="inline-flex h-7 items-center gap-1.5 rounded-md border border-emerald-500/40 bg-emerald-500/5 px-2 text-xs text-emerald-400 hover:bg-emerald-500/10"
                                data-testid={`link-ticket-whatsapp-${ticket.id}`}
                              >
                                <MessageCircle className="h-3.5 w-3.5" /> {t("builder_support_whatsapp")}
                              </a>
                            ) : (
                              <span
                                className="text-[10px] text-muted-foreground"
                                data-testid={`ticket-whatsapp-hint-${ticket.id}`}
                              >
                                {t("builder_support_whatsapp_hint")}
                              </span>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </CardContent>
            </Card>
          </TabsContent>
      </Tabs>
      <PasswordChangeDialog
        open={passwordClinic !== null}
        onOpenChange={(open) => { if (!open) setPasswordClinic(null); }}
        title={t("password_change_clinic_title")}
        description={t("password_change_clinic_description")}
        target={passwordClinic ? `${passwordClinic.name} (${passwordClinic.id})` : undefined}
        newPasswordLabel={t("password_change_new_label")}
        confirmPasswordLabel={t("password_change_confirm_label")}
        submitLabel={t("password_change_submit")}
        cancelLabel={t("password_change_cancel")}
        mismatchMessage={t("password_change_mismatch")}
        errorMessage={t("password_change_error")}
        loadingLabel={t("password_change_loading")}
        onSubmit={changeClinicPassword}
      />
      </>
      )}
    </div>
  );
}
