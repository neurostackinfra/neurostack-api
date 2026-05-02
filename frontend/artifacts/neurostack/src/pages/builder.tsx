import { useState, useEffect } from "react";
import { useLocation } from "wouter";
import {
  Wrench, Building2, Activity, Plus, CheckCircle2, XCircle,
  Loader2, AlertTriangle, KeyRound, ToggleLeft, ToggleRight,
  Sliders, Inbox,
} from "lucide-react";
import { useAuth } from "@/context/auth";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { useToast } from "@/hooks/use-toast";
import { useI18n } from "@/context/i18n";

// ── Types ─────────────────────────────────────────────────────────────────────

interface BuilderClinic {
  clinic_id: string;
  clinic_name: string;
  slug: string | null;
  status: "active" | "inactive";
  plan_type: string;
  trace_count: number;
  apis_configured: boolean;
}

interface BuilderStats {
  total_clinics: number;
  active_clinics: number;
  inactive_clinics: number;
  total_traces: number;
}

interface BuilderProfile {
  id: string;
  name: string;
  status: string;
  clinic_count: number;
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

// ── Builder Panel ─────────────────────────────────────────────────────────────

export default function BuilderPanel() {
  const { t } = useI18n();
  const { builderId, isBuilder, setViewingClinicId } = useAuth();
  const [, navigate] = useLocation();
  const token = useBuilderToken();
  const { toast } = useToast();

  // Data state
  const [profile, setProfile] = useState<BuilderProfile | null>(null);
  const [stats, setStats] = useState<BuilderStats | null>(null);
  const [clinics, setClinics] = useState<BuilderClinic[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Create clinic form state
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [newName, setNewName] = useState("");
  const [newEmail, setNewEmail] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  // Fetch all data on mount
  useEffect(() => {
    void fetchAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function fetchAll() {
    setLoading(true);
    setError(null);
    try {
      const headers = authHeaders(token);
      const [profileRes, statsRes, clinicsRes] = await Promise.all([
        fetch("/v1/builder/me", { headers }),
        fetch("/v1/builder/stats", { headers }),
        fetch("/v1/builder/clinics", { headers }),
      ]);
      if (profileRes.ok) setProfile(await profileRes.json() as BuilderProfile);
      if (statsRes.ok) setStats(await statsRes.json() as BuilderStats);
      if (clinicsRes.ok) setClinics(await clinicsRes.json() as BuilderClinic[]);
      if (!profileRes.ok && !statsRes.ok && !clinicsRes.ok) {
        setError("Erro ao carregar dados");
      }
    } catch {
      setError("Erro ao carregar dados");
    } finally {
      setLoading(false);
    }
  }

  // Create clinic handler
  async function handleCreateClinic() {
    if (!newName.trim() || !newEmail.trim() || !newPassword.trim()) {
      setCreateError(t("admin_create_required"));
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      const res = await fetch("/v1/builder/clinics", {
        method: "POST",
        headers: authHeaders(token),
        body: JSON.stringify({
          clinic_name: newName.trim(),
          admin_email: newEmail.trim(),
          admin_password: newPassword,
        }),
      });
      const data = await res.json() as { ok?: boolean; error?: string; id?: string };
      if (!res.ok) {
        setCreateError(data.error ?? t("admin_create_err_generic"));
        return;
      }
      toast({ title: t("builder_create_success") });
      setShowCreateForm(false);
      setNewName("");
      setNewEmail("");
      setNewPassword("");
      void fetchAll();
    } catch {
      setCreateError(t("admin_create_err_network"));
    } finally {
      setCreating(false);
    }
  }

  // Toggle clinic status handler
  async function handleToggleStatus(clinicId: string, currentStatus: string) {
    const newStatus = currentStatus === "active" ? "inactive" : "active";
    // Optimistic update
    setClinics((prev) =>
      prev.map((c) =>
        c.clinic_id === clinicId ? { ...c, status: newStatus as "active" | "inactive" } : c
      )
    );
    try {
      const res = await fetch(`/v1/builder/clinics/${clinicId}/status`, {
        method: "PATCH",
        headers: authHeaders(token),
        body: JSON.stringify({ status: newStatus }),
      });
      if (!res.ok) {
        // Revert optimistic update
        setClinics((prev) =>
          prev.map((c) =>
            c.clinic_id === clinicId ? { ...c, status: currentStatus as "active" | "inactive" } : c
          )
        );
        toast({ title: t("admin_toggle_err_title"), description: t("admin_toggle_err"), variant: "destructive" });
        return;
      }
      toast({
        title: newStatus === "active" ? t("admin_toggle_ok_on") : t("admin_toggle_ok_off"),
        description: `ID ${clinicId}`,
      });
    } catch {
      // Revert optimistic update
      setClinics((prev) =>
        prev.map((c) =>
          c.clinic_id === clinicId ? { ...c, status: currentStatus as "active" | "inactive" } : c
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

  // Guard: only builders
  if (!isBuilder) {
    return (
      <div className="flex items-center justify-center h-64 text-muted-foreground">
        <Wrench className="h-8 w-8 mr-2" /> Acesso restrito a Builders.
      </div>
    );
  }

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

      {/* Stats row — 4 cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Card className="border-border">
          <CardContent className="pt-4 pb-3">
            <p className="text-[11px] text-muted-foreground">{t("builder_total_clinics")}</p>
            <p className="text-2xl font-bold text-foreground">{stats?.total_clinics ?? 0}</p>
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
            <p className="text-2xl font-bold text-indigo-400">{stats?.total_traces?.toLocaleString("pt-BR") ?? 0}</p>
          </CardContent>
        </Card>
      </div>

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
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
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

      {/* Empty state */}
      {!loading && clinics.length === 0 && !error && (
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

      {/* Clinic list */}
      {!loading && clinics.length > 0 && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {clinics.map((clinic) => (
            <Card
              key={clinic.clinic_id}
              className={`border ${
                clinic.status === "active"
                  ? "border-border"
                  : "border-dashed border-slate-700 opacity-60"
              }`}
            >
              <CardHeader className="pb-2">
                <div className="flex items-start justify-between gap-2">
                  <div className="flex items-center gap-2 min-w-0">
                    <Building2 className="h-4 w-4 text-muted-foreground shrink-0" />
                    <div className="min-w-0">
                      <CardTitle className="text-sm truncate">{clinic.clinic_name}</CardTitle>
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
                    <Activity className="h-3 w-3" />
                    {clinic.trace_count} traces
                  </span>
                  <span className={`flex items-center gap-1 ml-auto ${clinic.apis_configured ? "text-emerald-400" : "text-slate-500"}`}>
                    <KeyRound className="h-3 w-3" />
                    {clinic.apis_configured ? "API ok" : "API --"}
                  </span>
                </div>
                <div className="flex items-center gap-2 pt-1">
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-7 text-xs gap-1.5 flex-1"
                    onClick={() => void handleToggleStatus(clinic.clinic_id, clinic.status)}
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
                    onClick={() => handleConfigureClinic(clinic.clinic_id)}
                  >
                    <Sliders className="h-3.5 w-3.5" /> {t("builder_configure")}
                  </Button>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
