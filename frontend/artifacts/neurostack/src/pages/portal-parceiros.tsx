import {
  Briefcase, Building2, AlertTriangle, Cpu, Copy, Zap,
  Users, LogIn, BookTemplate, Rocket, ShieldCheck, Star,
  ChevronRight, TrendingUp, Wifi, WifiOff,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card, CardContent, CardHeader, CardTitle, CardDescription,
} from "@/components/ui/card";
import { useI18n } from "@/context/i18n";

// ── Fake data ──────────────────────────────────────────────────────────────────

const CLINICS = [
  { name: "Clínica Odonto Prime",    spec: "Odontologia",   llm: "GPT-4o-mini", status: "active",  alert: true  },
  { name: "Vita Saúde Integrada",    spec: "Clínica Geral", llm: "GPT-4o-mini", status: "active",  alert: false },
  { name: "NovaMed Estética",        spec: "Dermatologia",  llm: "Llama 3",     status: "active",  alert: false },
  { name: "Centro Cardio São Lucas", spec: "Cardiologia",   llm: "GPT-4o",      status: "paused",  alert: false },
];

// ── Component ──────────────────────────────────────────────────────────────────

export default function PortalParceiros() {
  const { locale } = useI18n();
  const txt = (p: string, e: string) => locale === "en" ? e : p;

  return (
    <div className="max-w-5xl mx-auto space-y-8">

      {/* ── Header ──────────────────────────────────────────────────────────── */}
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2">
            <Briefcase className="h-5 w-5 text-indigo-400" />
            <h1 className="text-2xl font-bold tracking-tight text-foreground">
              {txt("Torre de Controle para Builders", "Builder Control Tower")}
            </h1>
          </div>
          <Badge className="bg-indigo-500/15 text-indigo-400 border-indigo-500/30 font-bold uppercase tracking-wide text-[10px]">
            ROADMAP · {txt("Gestão de Múltiplas Clínicas", "Multi-Clinic Management")}
          </Badge>
          <Badge className="bg-violet-500/10 text-violet-400 border-violet-500/20 text-[10px] font-bold uppercase">
            Master Tenant · v3
          </Badge>
        </div>
        <p className="text-sm text-foreground/70 max-w-2xl">
          {txt(
            "Gerencie um portfólio de clínicas, faça deploy de blueprints em segundos e exiba o Selo de Qualidade NeuroStack para seus clientes.",
            "Manage a portfolio of clinics, deploy blueprints in seconds, and display the NeuroStack Quality Seal to your clients."
          )}
        </p>
      </div>

      {/* ── Block 1: Command Center ──────────────────────────────────────────── */}
      <section className="space-y-4 opacity-70">
        <SectionTitle
          icon={<TrendingUp className="h-4 w-4 text-indigo-400" />}
          title={txt("Visão Global & Alertas", "Global View & Alerts")}
          subtitle={txt("Command Center", "Command Center")}
        />

        {/* Metric cards */}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <MetricCard
            value="12"
            label={txt("Clínicas Ativas", "Active Clinics")}
            color="text-emerald-400"
            icon={<Building2 className="h-4 w-4 text-emerald-400" />}
          />
          <MetricCard
            value="1.2M"
            label={txt("Tokens Consumidos (Mês)", "Tokens Consumed (Month)")}
            color="text-blue-400"
            icon={<Cpu className="h-4 w-4 text-blue-400" />}
          />
          <MetricCard
            value={
              <span className="flex items-center gap-1.5">
                1 <WifiOff className="h-4 w-4 text-rose-400 inline" />
              </span>
            }
            label={txt("Z-API Status - 1 Desconectada", "Z-API Status - 1 Disconnected")}
            color="text-rose-400"
            icon={<Wifi className="h-4 w-4 text-rose-400" />}
          />
        </div>

        {/* Alert banner */}
        <div className="flex items-start gap-3 rounded-lg border border-amber-500/30 bg-amber-500/8 px-4 py-3">
          <AlertTriangle className="h-4 w-4 text-amber-400 mt-0.5 shrink-0" />
          <div>
            <p className="text-sm font-semibold text-foreground">
              Clínica Odonto Prime
            </p>
            <p className="text-xs text-foreground/70 mt-0.5">
              {txt(
                "Necessita atenção no módulo de Handoff, agente sem resposta há 14 min.",
                "Requires attention in the Handoff module, agent unresponsive for 14 min."
              )}
            </p>
          </div>
          <Badge className="ml-auto shrink-0 bg-amber-500/15 text-amber-400 border-amber-500/30 text-[10px]">
            {txt("Atenção", "Attention")}
          </Badge>
        </div>
      </section>

      {/* ── Block 2: Sub-Account Management ──────────────────────────────────── */}
      <section className="space-y-4 opacity-70">
        <SectionTitle
          icon={<Users className="h-4 w-4 text-indigo-400" />}
          title={txt("Gestão de Sub-Contas", "Sub-Account Management")}
          subtitle={txt("Impersonation", "Impersonation")}
        />

        <Card className="border-border">
          <CardContent className="p-0">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border bg-muted/30">
                    <th className="text-left text-[11px] font-semibold uppercase tracking-wider text-muted-foreground px-4 py-2.5">
                      {txt("Nome da Clínica", "Clinic Name")}
                    </th>
                    <th className="text-left text-[11px] font-semibold uppercase tracking-wider text-muted-foreground px-4 py-2.5 hidden sm:table-cell">
                      {txt("Especialidade", "Specialty")}
                    </th>
                    <th className="text-left text-[11px] font-semibold uppercase tracking-wider text-muted-foreground px-4 py-2.5 hidden md:table-cell">
                      {txt("LLM Ativo", "Active LLM")}
                    </th>
                    <th className="text-left text-[11px] font-semibold uppercase tracking-wider text-muted-foreground px-4 py-2.5 hidden sm:table-cell">
                      Status
                    </th>
                    <th className="text-right text-[11px] font-semibold uppercase tracking-wider text-muted-foreground px-4 py-2.5">
                      {txt("Ação", "Action")}
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border/50">
                  {CLINICS.map((clinic) => (
                    <tr key={clinic.name} className="hover:bg-muted/20 transition-colors">
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-2">
                          {clinic.alert && (
                            <AlertTriangle className="h-3 w-3 text-amber-400 shrink-0" />
                          )}
                          <span className="font-medium text-foreground text-xs">{clinic.name}</span>
                        </div>
                      </td>
                      <td className="px-4 py-3 text-xs text-foreground/70 hidden sm:table-cell">
                        {clinic.spec}
                      </td>
                      <td className="px-4 py-3 hidden md:table-cell">
                        <code className="text-[11px] bg-slate-800 px-1.5 py-0.5 rounded text-blue-300">
                          {clinic.llm}
                        </code>
                      </td>
                      <td className="px-4 py-3 hidden sm:table-cell">
                        <Badge
                          variant="outline"
                          className={
                            clinic.status === "active"
                              ? "border-emerald-500/30 text-emerald-400 text-[10px]"
                              : "border-slate-600 text-slate-400 text-[10px]"
                          }
                        >
                          {clinic.status === "active"
                            ? txt("Ativo", "Active")
                            : txt("Pausado", "Paused")}
                        </Badge>
                      </td>
                      <td className="px-4 py-3 text-right">
                        <Button
                          variant="outline"
                          size="sm"
                          className="h-7 text-[11px] gap-1.5 border-indigo-500/30 text-indigo-400 hover:bg-indigo-500/10 cursor-not-allowed"
                          disabled
                        >
                          <LogIn className="h-3 w-3" />
                          {txt("Acessar Painel", "Access Panel")}
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      </section>

      {/* ── Block 3: Clone Central ────────────────────────────────────────────── */}
      <section className="space-y-4 opacity-70">
        <SectionTitle
          icon={<BookTemplate className="h-4 w-4 text-indigo-400" />}
          title={txt("Central de Clonagem", "Clone Central")}
          subtitle={txt("Deploy em 1-Clique", "1-Click Deploy")}
        />

        <Card className="border-border">
          <CardHeader className="pb-3 border-b border-border bg-slate-900/40">
            <div className="flex items-center justify-between gap-3 flex-wrap">
              <div>
                <CardTitle className="text-sm font-semibold text-foreground">
                  {txt("Blueprints e Templates de IA", "AI Blueprints & Templates")}
                </CardTitle>
                <CardDescription className="text-xs mt-1 max-w-xl">
                  {txt(
                    "Salve a personalidade e o SRM de uma clínica de sucesso e faça o deploy em um novo cliente em segundos.",
                    "Save the personality and SRM of a successful clinic and deploy it to a new client in seconds."
                  )}
                </CardDescription>
              </div>
              <Badge className="bg-indigo-500/15 text-indigo-400 border-indigo-500/30 text-[10px] font-bold">
                v3 · {txt("Em Desenvolvimento", "In Development")}
              </Badge>
            </div>
          </CardHeader>
          <CardContent className="pt-4">
            {/* Blueprint list placeholder */}
            <div className="space-y-2 mb-4">
              {[
                { name: txt("Blueprint: Odontologia Premium", "Blueprint: Premium Dentistry"), clinics: 3, score: 98 },
                { name: txt("Blueprint: Clínica Geral Express", "Blueprint: Express General Clinic"), clinics: 5, score: 95 },
                { name: txt("Blueprint: Dermatologia AI-First", "Blueprint: AI-First Dermatology"), clinics: 1, score: 91 },
              ].map((bp) => (
                <div
                  key={bp.name}
                  className="flex items-center gap-3 rounded-lg border border-border/60 bg-muted/10 px-3 py-2.5"
                >
                  <Zap className="h-3.5 w-3.5 text-indigo-400 shrink-0" />
                  <div className="flex-1 min-w-0">
                    <p className="text-xs font-medium text-foreground truncate">{bp.name}</p>
                    <p className="text-[10px] text-muted-foreground">
                      {bp.clinics} {txt("clínicas usando", "clinics using")} · NPS {bp.score}
                    </p>
                  </div>
                  <ChevronRight className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
                </div>
              ))}
            </div>

            <div className="flex flex-wrap gap-2">
              <Button
                variant="outline"
                size="sm"
                className="gap-2 text-xs border-slate-600 cursor-not-allowed"
                disabled
              >
                <Copy className="h-3.5 w-3.5" />
                {txt("Salvar como Blueprint", "Save as Blueprint")}
              </Button>
              <Button
                size="sm"
                className="gap-2 text-xs bg-indigo-600 hover:bg-indigo-700 cursor-not-allowed"
                disabled
              >
                <Rocket className="h-3.5 w-3.5" />
                {txt("Deploy em Nova Clínica", "Deploy to New Clinic")}
              </Button>
            </div>
          </CardContent>
        </Card>
      </section>

      {/* ── Block 4: Quality Seal / Co-Branding ──────────────────────────────── */}
      <section className="space-y-4 opacity-70">
        <SectionTitle
          icon={<Star className="h-4 w-4 text-indigo-400" />}
          title={txt("Selo de Qualidade & Co-Branding", "Quality Seal & Co-Branding")}
          subtitle="Powered by NeuroStack"
        />

        <Card className="border-indigo-500/25 bg-gradient-to-br from-slate-900/60 to-indigo-950/30">
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-semibold text-foreground">
              {txt("Selo de Qualidade NeuroStack", "NeuroStack Quality Seal")}
            </CardTitle>
            <CardDescription className="text-xs max-w-xl">
              {txt(
                "Seus clientes sabem que seus dados estão protegidos. Exiba sua marca lado a lado com a auditoria e compliance da NeuroStack.",
                "Your clients know their data is protected. Display your brand alongside NeuroStack's audit and compliance credentials."
              )}
            </CardDescription>
          </CardHeader>
          <CardContent className="pt-0">
            <div className="grid sm:grid-cols-2 gap-4">
              {/* Widget mockup */}
              <div className="rounded-xl border border-indigo-500/30 bg-slate-900/80 p-5 flex flex-col items-center gap-3">
                <p className="text-[10px] uppercase tracking-widest text-muted-foreground font-semibold">
                  {txt("Prévia do Widget", "Widget Preview")}
                </p>
                <div className="flex items-center gap-4 py-3 px-5 rounded-lg border border-border bg-background/60 shadow-lg">
                  {/* Agency logo placeholder */}
                  <div className="w-10 h-10 rounded-lg bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center shrink-0">
                    <Briefcase className="h-5 w-5 text-white" />
                  </div>
                  <div className="flex flex-col items-start text-left">
                    <span className="text-[10px] font-bold text-foreground leading-tight">
                      {txt("Logo da Agência", "Agency Logo")}
                    </span>
                    <div className="flex items-center gap-1 mt-0.5">
                      <span className="text-[9px] text-muted-foreground">
                        {txt("Powered by", "Powered by")}
                      </span>
                      <span className="text-[9px] font-black text-indigo-400 tracking-tight">
                        NeuroStack Health AI
                      </span>
                    </div>
                  </div>
                  <ShieldCheck className="h-5 w-5 text-emerald-400 shrink-0" />
                </div>
                <p className="text-[10px] text-muted-foreground/60 text-center max-w-[200px]">
                  {txt(
                    "Widget embedável em qualquer portal de clínica",
                    "Embeddable widget for any clinic portal"
                  )}
                </p>
              </div>

              {/* Co-branding info */}
              <div className="space-y-3">
                {[
                  {
                    icon: <ShieldCheck className="h-4 w-4 text-emerald-400" />,
                    title: txt("LGPD & HIPAA Compliance", "LGPD & HIPAA Compliance"),
                    desc: txt(
                      "Auditoria automática e relatórios de conformidade vinculados à sua conta.",
                      "Automatic audit and compliance reports linked to your account."
                    ),
                  },
                  {
                    icon: <Star className="h-4 w-4 text-amber-400" />,
                    title: txt("Estratégia Intel Inside", "Intel Inside Strategy"),
                    desc: txt(
                      "Co-branding como Selo de Qualidade, não white-label. Constrói equity de marca para você e para a NeuroStack.",
                      "Co-branding as a Quality Seal, not white-label. Builds brand equity for you and NeuroStack."
                    ),
                  },
                  {
                    icon: <Zap className="h-4 w-4 text-indigo-400" />,
                    title: txt("API de Certificação", "Certification API"),
                    desc: txt(
                      "Endpoint seguro para verificar o status de compliance de qualquer clínica em tempo real.",
                      "Secure endpoint to verify compliance status of any clinic in real time."
                    ),
                  },
                ].map((item) => (
                  <div key={item.title} className="flex items-start gap-3 rounded-lg border border-border/50 bg-muted/10 px-3 py-2.5">
                    <div className="mt-0.5 shrink-0">{item.icon}</div>
                    <div>
                      <p className="text-xs font-semibold text-foreground">{item.title}</p>
                      <p className="text-[11px] text-muted-foreground mt-0.5">{item.desc}</p>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </CardContent>
        </Card>
      </section>

      {/* ── Roadmap CTA ───────────────────────────────────────────────────────── */}
      <div className="flex items-center gap-3 rounded-xl border border-dashed border-indigo-500/30 bg-indigo-500/5 px-5 py-4">
        <Briefcase className="h-5 w-5 text-indigo-400 shrink-0" />
        <div className="flex-1 min-w-0">
          <p className="text-sm font-semibold text-foreground">
            {txt("Seja um Builder NeuroStack Certificado", "Become a Certified NeuroStack Builder")}
          </p>
          <p className="text-xs text-muted-foreground mt-0.5">
            {txt(
              "Acesso antecipado ao programa de parceiros, construa e monetize seu portfólio de clínicas com IA.",
              "Early access to the partner program, build and monetize your AI clinic portfolio."
            )}
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          className="shrink-0 gap-2 border-indigo-500/30 text-indigo-400 hover:bg-indigo-500/10 text-xs cursor-not-allowed"
          disabled
        >
          <ChevronRight className="h-3.5 w-3.5" />
          {txt("Em Breve", "Coming Soon")}
        </Button>
      </div>

    </div>
  );
}

// ── Sub-components ──────────────────────────────────────────────────────────────

function SectionTitle({
  icon, title, subtitle,
}: {
  icon: React.ReactNode;
  title: string;
  subtitle: string;
}) {
  return (
    <div className="flex items-center gap-2">
      {icon}
      <h2 className="text-base font-bold text-foreground">{title}</h2>
      <span className="text-muted-foreground/50 text-sm">·</span>
      <span className="text-xs text-muted-foreground">{subtitle}</span>
    </div>
  );
}

function MetricCard({
  value, label, color, icon,
}: {
  value: React.ReactNode;
  label: string;
  color: string;
  icon: React.ReactNode;
}) {
  return (
    <Card className="border-border">
      <CardContent className="pt-4 pb-3">
        <div className="flex items-center justify-between mb-1">
          {icon}
          <div className={`text-2xl font-black tabular-nums ${color}`}>{value}</div>
        </div>
        <p className="text-[11px] text-muted-foreground">{label}</p>
      </CardContent>
    </Card>
  );
}
