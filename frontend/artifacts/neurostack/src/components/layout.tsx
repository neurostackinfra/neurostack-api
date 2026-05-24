import { useState, useEffect } from "react";
import { Link, useLocation } from "wouter";
import {
  MessageSquare,
  Activity,
  LayoutDashboard,
  Menu,
  Sliders,
  LogOut,
  Building2,
  Globe,
  Network,
  Link2,
  Library,
  Shield,
  Eye,
  GraduationCap,
  ShieldCheck,
  Cpu,
  Lightbulb,
  FolderOpen,
  CreditCard,
  Mic,
  HelpCircle,
  Briefcase,
  FlaskConical,
  Wrench,
  ChevronDown,
} from "lucide-react";
import logoSrc from "@assets/logo_png_1775411054369.png";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTrigger } from "@/components/ui/sheet";
import { useAuth } from "@/context/auth";
import { useI18n } from "@/context/i18n";
// useGetClinicConfig removed — it ignores clinic_id; layout now fetches directly

interface LayoutProps {
  children: React.ReactNode;
}

export function Layout({ children }: LayoutProps) {
  const [location] = useLocation();
  const { logout, email, clinicId, role, isSuperAdmin, isInvestor, isBuilder, isClinicAdmin, isReadOnly, viewingClinicId } = useAuth();
  const { t, locale, setLocale } = useI18n();
  const { setViewingClinicId } = useAuth();
  const displayClinicId = (isSuperAdmin || isBuilder) ? viewingClinicId : clinicId;

  // useGetClinicConfig ignores clinic_id — fetch directly with ?clinic_id= so
  // builders see the selected clinic's name instead of their own HQ config.
  const [clinicName, setClinicName] = useState<string>("");
  const [botActive, setBotActive] = useState<boolean | null>(null);
  useEffect(() => {
    if (!displayClinicId) { setClinicName(""); setBotActive(null); return; }
    const raw = localStorage.getItem("ns_auth");
    const token = raw ? (JSON.parse(raw) as { token?: string }).token : null;
    if (!token) return;
    fetch(`/v1/config?clinic_id=${displayClinicId}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((r) => r.ok ? r.json() : Promise.reject(r))
      .then((cfg: { clinic_name?: string; bot_active?: boolean }) => {
        setClinicName(cfg.clinic_name ?? "");
        if (cfg.bot_active !== undefined) setBotActive(cfg.bot_active);
      })
      .catch(() => {});
  }, [displayClinicId]);

  // Builder clinic list for dropdown
  const [builderClinics, setBuilderClinics] = useState<Array<{ id: string; name: string }>>([]);
  useEffect(() => {
    if (!isBuilder) return;
    const raw = localStorage.getItem("ns_auth");
    const token = raw ? (JSON.parse(raw) as { token?: string }).token : null;
    if (!token) return;
    fetch("/v1/builder/clinics", {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((r) => r.ok ? r.json() : Promise.reject(r))
      .then((data: Array<{ id: string; name: string }> | { clinics?: Array<{ id: string; name: string }> }) => {
        // API returns a plain array, not { clinics: [...] }
        const clinics = Array.isArray(data) ? data : (data.clinics ?? []);
        setBuilderClinics(clinics);
        // Stale viewingClinicId guard — use c.id (the tenant UUID)
        if (clinics.length > 0 && !clinics.some((c) => c.id === viewingClinicId)) {
          setViewingClinicId(clinics[0].id);
        }
      })
      .catch(() => {});
  }, [isBuilder, viewingClinicId, setViewingClinicId]);

  // Super admin clinic list for dropdown (all active clinics on the platform)
  const [superAdminClinics, setSuperAdminClinics] = useState<Array<{ id: string; name: string }>>([]);
  useEffect(() => {
    if (!isSuperAdmin) return;
    const raw = localStorage.getItem("ns_auth");
    const token = raw ? (JSON.parse(raw) as { token?: string }).token : null;
    if (!token) return;
    fetch("/v1/admin/clinics", {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((r) => r.ok ? r.json() : Promise.reject(r))
      .then((data: Array<{ clinic_id: string; clinic_name: string; status: string }>) => {
        const active = data
          .filter((c) => c.status === "active")
          .map((c) => ({ id: c.clinic_id, name: c.clinic_name }));
        setSuperAdminClinics(active);
        // Auto-select first clinic if current viewingClinicId is not in the list
        if (active.length > 0 && !active.some((c) => c.id === viewingClinicId)) {
          setViewingClinicId(active[0].id);
        }
      })
      .catch(() => {});
  }, [isSuperAdmin, viewingClinicId, setViewingClinicId]);

  const navItems = [
    { href: "/",         label: t("nav_chat"),      icon: MessageSquare,  id: "chat-simulator", hideFor: [] },
    { href: "/traces",   label: t("nav_traces"),    icon: Activity,       id: "trace-history",  hideFor: [] },
    { href: "/dashboard",label: t("nav_dashboard"), icon: LayoutDashboard,id: "dashboard",      hideFor: [] },
    { href: "/config",   label: t("nav_config"),    icon: Sliders,        id: "clinic-config",  hideFor: [] },
    { href: "/suporte",  label: t("nav_support"),    icon: HelpCircle,    id: "suporte",        hideFor: ["super_admin"], badge: isClinicAdmin ? t("badge_coming_soon") : undefined },
    { href: "/billing",  label: t("nav_billing"),    icon: CreditCard,    id: "billing",        hideFor: ["clinic_admin", "super_admin", "investor"] },
  ].filter(item => !(item.hideFor as string[]).includes(role));

  const roadmapItems = [
    { href: "/agents",         label: t("nav_agents"),         icon: Network,       id: "agents",         v: "v3",  hideFor: ["clinic_admin"] },
    { href: "/crm",            label: t("nav_crm"),            icon: Link2,         id: "crm",            v: "v2",  hideFor: ["clinic_admin"] },
    { href: "/knowledge-base", label: t("nav_knowledge_base"), icon: Library,       id: "knowledge-base", v: "v2",  hideFor: ["clinic_admin"] },
    { href: "/onboarding",     label: "Onboarding",            icon: GraduationCap, id: "onboarding",     v: "v2",  hideFor: [] },
    { href: "/seguranca",      label: t("nav_security"),       icon: ShieldCheck,   id: "seguranca",      v: "v2",  hideFor: ["clinic_admin"] },
    { href: "/slm-cortex",     label: "SLM Cortex",            icon: Cpu,           id: "slm-cortex",     v: "v3",  hideFor: ["clinic_admin"] },
    { href: "/rlhf-audit",     label: t("nav_rlhf"),           icon: FlaskConical,  id: "rlhf-audit",     v: "v3",  hideFor: ["clinic_admin"] },
    { href: "/consultoria",    label: t("nav_consulting"),     icon: Lightbulb,     id: "consultoria",    v: "v2",  hideFor: [] },
    { href: "/midias",         label: t("nav_media"),          icon: FolderOpen,    id: "midias",         v: "v3",  hideFor: ["clinic_admin"] },
    { href: "/voice-ai",       label: t("nav_voice"),          icon: Mic,           id: "voice-ai",       v: "v3",  hideFor: ["clinic_admin"] },
    { href: "/portal-parceiros",label: t("nav_partners"),      icon: Briefcase,     id: "portal-parceiros",v: "v3", hideFor: ["clinic_admin"] },
  ].filter(item => !item.hideFor.includes(role));

  const SidebarContent = () => (
    <div className="flex h-full flex-col bg-sidebar text-sidebar-foreground">
      {/* Logo */}
      <div className="flex h-40 items-center border-b border-sidebar-border px-4">
        <img src={logoSrc} alt="NeuroStack" className="h-32 w-auto max-w-full object-contain" />
      </div>

      {/* Active Clinic (read-only — set by login) */}
      <div className="border-b border-sidebar-border px-3 py-3">
        <div className="flex items-center gap-1.5 mb-1 px-1">
          <Building2 className="h-3 w-3 text-muted-foreground" />
          <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            {t("nav_active_clinic")}
          </span>
        </div>
        <p
          className="text-sm font-medium text-sidebar-foreground px-1 truncate"
          data-testid="active-clinic-name"
        >
          {clinicName}
        </p>
      </div>

      {/* Builder clinic dropdown */}
      {isBuilder && builderClinics.length > 0 && (
        <div className="border-b border-sidebar-border px-3 py-2">
          <div className="flex items-center gap-1.5 mb-1 px-1">
            <ChevronDown className="h-3 w-3 text-indigo-400" />
            <span className="text-[10px] font-semibold uppercase tracking-wider text-indigo-400/60">
              {t("label_select_clinic")}
            </span>
          </div>
          <select
            value={viewingClinicId}
            onChange={(e) => setViewingClinicId(e.target.value)}
            className="w-full rounded-md border border-sidebar-border bg-sidebar px-2 py-1.5 text-xs text-sidebar-foreground focus:outline-none focus:ring-1 focus:ring-indigo-500"
            data-testid="builder-clinic-dropdown"
          >
            {builderClinics.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </div>
      )}

      {/* Super admin clinic dropdown */}
      {isSuperAdmin && superAdminClinics.length > 0 && (
        <div className="border-b border-sidebar-border px-3 py-2">
          <div className="flex items-center gap-1.5 mb-1 px-1">
            <ChevronDown className="h-3 w-3 text-violet-400" />
            <span className="text-[10px] font-semibold uppercase tracking-wider text-violet-400/60">
              {t("label_select_clinic")}
            </span>
          </div>
          <select
            value={viewingClinicId ?? ""}
            onChange={(e) => setViewingClinicId(e.target.value)}
            className="w-full rounded-md border border-sidebar-border bg-sidebar px-2 py-1.5 text-xs text-sidebar-foreground focus:outline-none focus:ring-1 focus:ring-violet-500"
            data-testid="super-admin-clinic-dropdown"
          >
            {superAdminClinics.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </div>
      )}

      {/* Investor / read-only badge */}
      {isReadOnly && (
        <div className="mx-3 mb-1 mt-2 flex items-center gap-1.5 rounded-md bg-amber-500/10 border border-amber-500/30 px-2 py-1.5">
          <Eye className="h-3 w-3 text-amber-400 shrink-0" />
          <span className="text-[10px] text-amber-400 font-medium">{t("label_read_only_mode")}</span>
        </div>
      )}

      {/* IA Pausada badge — shown when bot_active=false for clinic_admin or builder */}
      {(isClinicAdmin || isBuilder) && botActive === false && (
        <div
          className="mx-3 mb-1 mt-1 flex items-center gap-1.5 rounded-md bg-amber-500/10 border border-amber-500/30 px-2 py-1.5"
          data-testid="badge-ia-pausada"
        >
          <Cpu className="h-3 w-3 text-amber-400 shrink-0" />
          <span className="text-[10px] text-amber-400 font-semibold">IA Pausada</span>
        </div>
      )}

      {/* Nav */}
      <div className="flex-1 overflow-auto py-4 sidebar-scroll">
        <nav className="grid gap-1 px-2">
          {navItems.map((item) => (
            <Link key={item.href} href={item.href}>
              <Button
                variant={location === item.href ? "secondary" : "ghost"}
                className={`w-full justify-start gap-2 ${
                  location === item.href
                    ? "bg-sidebar-accent text-sidebar-accent-foreground"
                    : "text-sidebar-foreground hover:bg-sidebar-accent/50"
                }`}
                data-testid={`nav-link-${item.id}`}
              >
                <item.icon className="h-4 w-4" />
                {item.label}
                {"badge" in item && item.badge && (
                  <Badge variant="outline" className="ml-auto text-[9px] px-1.5 py-0 border-amber-500/40 text-amber-500">
                    {item.badge}
                  </Badge>
                )}
              </Button>
            </Link>
          ))}

          {/* Master Admin section — super_admin + investor (hide from builders) */}
          {(isSuperAdmin || isInvestor) && !isBuilder && (
            <>
              <div className="mt-3 pt-3 border-t border-sidebar-border">
                <p className="text-[10px] font-semibold uppercase tracking-wider text-violet-400/60 px-2 mb-1.5 flex items-center gap-1">
                  <Shield className="h-3 w-3" />
                  Master Admin
                </p>
              </div>
              <Link href="/master-admin">
                <Button
                  variant={location === "/master-admin" ? "secondary" : "ghost"}
                  className={`w-full justify-start gap-2 ${
                    location === "/master-admin"
                      ? "bg-violet-500/20 text-violet-400"
                      : "text-violet-400/70 hover:bg-violet-500/10 hover:text-violet-400"
                  }`}
                  data-testid="nav-link-master-admin"
                >
                  <Shield className="h-4 w-4" />
                  Master Admin
                </Button>
              </Link>
            </>
          )}

          {/* Builder Panel section */}
          {isBuilder && (
            <>
              <div className="mt-3 pt-3 border-t border-sidebar-border">
                <p className="text-[10px] font-semibold uppercase tracking-wider text-indigo-400/60 px-2 mb-1.5 flex items-center gap-1">
                  <Wrench className="h-3 w-3" />
                  Builder
                </p>
              </div>
              <Link href="/builder">
                <Button
                  variant={location === "/builder" ? "secondary" : "ghost"}
                  className={`w-full justify-start gap-2 ${
                    location === "/builder"
                      ? "bg-indigo-500/20 text-indigo-400"
                      : "text-indigo-400/70 hover:bg-indigo-500/10 hover:text-indigo-400"
                  }`}
                  data-testid="nav-link-builder"
                >
                  <Wrench className="h-4 w-4" />
                  {t("builder_panel_title")}
                </Button>
              </Link>
            </>
          )}

          {/* Roadmap Section */}
          <div className="mt-3 pt-3">
            <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground px-2 mb-1.5">
              {t("nav_roadmap_label")}
            </p>
            {roadmapItems.map((item) => (
              <Link key={item.href} href={item.href}>
                <Button
                  variant={location === item.href ? "secondary" : "ghost"}
                  className={`w-full justify-start gap-2 text-xs ${
                    location === item.href
                      ? "bg-violet-500/15 text-violet-600 dark:text-violet-400"
                      : "text-muted-foreground hover:bg-sidebar-accent/50 hover:text-sidebar-foreground"
                  }`}
                  data-testid={`nav-link-${item.id}`}
                >
                  <item.icon className="h-3.5 w-3.5 text-violet-500" />
                  {item.label}
                  <span className={`ml-auto text-[9px] font-bold uppercase tracking-wider opacity-70 ${
                    item.v === "v3"  ? "text-indigo-400" :
                    item.v === "slm" ? "text-amber-400"  : "text-violet-500"
                  }`}>
                    {item.v}
                  </span>
                </Button>
              </Link>
            ))}
          </div>
        </nav>
      </div>

      {/* Footer: locale toggle + email + sign out */}
      <div className="border-t border-sidebar-border px-3 py-3 space-y-2">
        <div className="flex items-center justify-between px-1">
          <p className="text-[10px] text-muted-foreground truncate max-w-[150px]">{email}</p>
          <button
            onClick={() => setLocale(locale === "en" ? "pt" : "en")}
            className="flex items-center gap-1 text-[10px] font-medium text-muted-foreground hover:text-foreground transition-colors shrink-0 ml-2"
            title="Switch language"
            data-testid="btn-locale-toggle"
          >
            <Globe className="h-3 w-3" />
            <span className={locale === "pt" ? "font-bold text-foreground" : ""}>PT</span>
            <span className="opacity-40 mx-0.5">·</span>
            <span className={locale === "en" ? "font-bold text-foreground" : ""}>EN</span>
          </button>
        </div>
        <Button
          variant="ghost"
          size="sm"
          className="w-full justify-start gap-2 text-muted-foreground hover:text-destructive hover:bg-destructive/10 text-xs"
          onClick={logout}
          data-testid="btn-logout"
        >
          <LogOut className="h-3.5 w-3.5" />
          {t("nav_sign_out")}
        </Button>
        <div className="flex items-center justify-center pt-1">
          <span className="text-[10px] font-semibold text-muted-foreground/40 tracking-widest uppercase">
            NeuroStack v1
          </span>
        </div>
      </div>
    </div>
  );

  return (
    <div className="flex min-h-screen w-full flex-col bg-background">
      {/* Desktop Sidebar */}
      <div className="hidden border-r border-border md:block md:w-64 lg:w-72 fixed inset-y-0 left-0 z-50">
        <SidebarContent />
      </div>

      <div className="flex flex-col md:pl-64 lg:pl-72 flex-1">
        {/* Mobile Header */}
        <header className="sticky top-0 z-30 flex h-14 items-center gap-4 border-b border-sidebar-border bg-sidebar px-4 sm:px-6 md:hidden">
          <Sheet>
            <SheetTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="shrink-0 md:hidden text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-foreground"
                data-testid="mobile-menu-trigger"
              >
                <Menu className="h-5 w-5" />
                <span className="sr-only">{t("nav_menu_sr")}</span>
              </Button>
            </SheetTrigger>
            <SheetContent side="left" className="w-64 p-0 border-r-0">
              <SidebarContent />
            </SheetContent>
          </Sheet>
          <div className="flex items-center">
            <img src={logoSrc} alt="NeuroStack" className="h-20 w-auto max-w-full object-contain" />
          </div>
          <div className="ml-auto text-xs text-sidebar-foreground/70">{clinicName}</div>
        </header>

        {/* Main Content */}
        <main className="flex-1 p-4 sm:p-6 lg:p-8">{children}</main>
      </div>
    </div>
  );
}
