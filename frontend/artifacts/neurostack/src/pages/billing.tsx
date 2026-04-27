import {
  CreditCard,
  CheckCircle2,
  FlaskConical,
  Wallet,
  Zap,
  FileText,
  Download,
  Plus,
  Star,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/context/i18n";

function SectionDivider({ num, label }: { num: string; label: string }) {
  return (
    <div className="flex items-center gap-3">
      <div className="flex h-5 w-5 items-center justify-center rounded-full bg-emerald-500/20 shrink-0">
        <span className="text-[9px] font-bold text-emerald-500">{num}</span>
      </div>
      <span className="text-[10px] font-bold uppercase tracking-widest text-emerald-500">{label}</span>
      <div className="flex-1 h-px bg-emerald-500/20" />
    </div>
  );
}

function PaymentMethodRow({
  icon: Icon,
  label,
  detail,
  active = false,
}: {
  icon: React.ElementType;
  label: string;
  detail?: string;
  active?: boolean;
}) {
  const { locale } = useI18n();
  const txt = (p: string, e: string) => locale === "en" ? e : p;
  return (
    <div className="flex items-center gap-3 rounded-lg border border-border/60 bg-muted/20 px-4 py-3 cursor-not-allowed opacity-75">
      <div className="flex h-8 w-8 items-center justify-center rounded-md border border-border/50 bg-background shrink-0">
        <Icon className="h-4 w-4 text-foreground/60" />
      </div>
      <div className="flex-1">
        <p className="text-sm font-semibold text-foreground">{label}</p>
        {detail && <p className="text-[11px] font-medium text-foreground/55 mt-0.5">{detail}</p>}
      </div>
      {active && (
        <Badge variant="outline" className="text-[9px] font-bold bg-emerald-500/10 border-emerald-500/25 text-emerald-600 dark:text-emerald-400">
          {txt("Principal", "Primary")}
        </Badge>
      )}
    </div>
  );
}

function InvoiceRow({ description, status }: { description: string; status: "Paga" | "Pendente" }) {
  const { locale } = useI18n();
  const txt = (p: string, e: string) => locale === "en" ? e : p;
  return (
    <div className="flex items-center gap-3 py-2.5 border-b border-border/30 last:border-0 opacity-75 cursor-not-allowed">
      <FileText className="h-3.5 w-3.5 text-foreground/40 shrink-0" />
      <p className="flex-1 text-xs font-medium text-foreground/80">{description}</p>
      <Badge
        variant="outline"
        className={`text-[9px] font-bold shrink-0 ${
          status === "Paga"
            ? "bg-emerald-500/10 border-emerald-500/25 text-emerald-600 dark:text-emerald-400"
            : "bg-amber-500/10 border-amber-500/25 text-amber-600 dark:text-amber-400"
        }`}
      >
        {status === "Paga" ? txt("Paga", "Paid") : txt("Pendente", "Pending")}
      </Badge>
      <Button
        disabled
        variant="ghost"
        size="sm"
        className="h-6 px-2 gap-1 opacity-40 cursor-not-allowed text-[10px]"
      >
        <Download className="h-3 w-3" />
        PDF
      </Button>
    </div>
  );
}

export default function Billing() {
  const { locale } = useI18n();
  const txt = (p: string, e: string) => locale === "en" ? e : p;

  return (
    <div className="space-y-8 max-w-4xl">

      {/* ── Page header ──────────────────────────────────────────────────────── */}
      <div className="space-y-4">
        <div className="flex items-start justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-emerald-500/10 border border-emerald-500/20">
              <CreditCard className="h-5 w-5 text-emerald-500" />
            </div>
            <div>
              <h1 className="text-2xl font-bold tracking-tight text-foreground">
                {txt("Faturamento e Gestão de Assinatura", "Billing & Subscription Management")}
                <span className="text-foreground/60 font-normal text-lg ml-2">(Billing)</span>
              </h1>
              <p className="text-sm font-medium text-foreground/70 mt-0.5">
                {txt(
                  "Autogestão de planos, créditos e pagamentos, com Product-Led Growth nativo",
                  "Self-managed plans, credits and payments, with native Product-Led Growth"
                )}
              </p>
            </div>
          </div>
          <Badge
            variant="outline"
            className="shrink-0 mt-1 bg-emerald-950/60 border-emerald-500/30 text-emerald-300 font-semibold text-[10px] uppercase tracking-wider px-3 py-1.5"
          >
            <FlaskConical className="h-3 w-3 mr-1.5" />
            {txt("Roadmap · Módulo Financeiro", "Roadmap · Financial Module")}
          </Badge>
        </div>

        {/* Banner */}
        <div className="rounded-lg border border-indigo-800 bg-indigo-950 px-5 py-4">
          <div className="flex items-center gap-4">
            <CreditCard className="h-8 w-8 text-white shrink-0" />
            <div className="flex-1">
              <p className="text-sm font-bold text-white">
                {txt("ROADMAP: Módulo Financeiro Integrado", "ROADMAP: Integrated Financial Module")}
              </p>
              <p className="text-xs font-medium text-white/80 mt-0.5 leading-relaxed">
                {txt(
                  "Gestão completa de assinaturas, créditos e faturas diretamente na plataforma. Self-service billing com suporte a cartão de crédito e PIX, sem intervenção manual.",
                  "Complete management of subscriptions, credits and invoices directly on the platform. Self-service billing with credit card and PIX support, no manual intervention required."
                )}
              </p>
            </div>
            <div className="flex flex-col items-end gap-1 shrink-0 text-right">
              <Badge variant="outline" className="text-[9px] font-bold tracking-widest bg-indigo-900/40 border-indigo-500/30 text-indigo-200 px-2">Self-Service</Badge>
              <Badge variant="outline" className="text-[9px] font-bold tracking-widest bg-indigo-900/40 border-indigo-500/30 text-indigo-200 px-2">
                {txt("PIX & Cartão", "PIX & Card")}
              </Badge>
              <Badge variant="outline" className="text-[9px] font-bold tracking-widest bg-indigo-900/40 border-indigo-500/30 text-indigo-200 px-2">PLG {txt("Nativo", "Native")}</Badge>
            </div>
          </div>
        </div>

        {/* Stat strip */}
        <div className="grid grid-cols-3 gap-3">
          {[
            { label: txt("Modelos de Plano", "Plan Models"),        value: "3",    note: "Starter / Pro / Enterprise"                                      },
            { label: txt("Métodos de Pagamento", "Payment Methods"), value: "2",   note: txt("Cartão de crédito + PIX", "Credit card + PIX")               },
            { label: txt("Ciclo de Faturamento", "Billing Cycle"),   value: "30d", note: txt("renovação automática", "automatic renewal")                  },
          ].map(s => (
            <div key={s.label} className="rounded-lg border border-indigo-800 bg-indigo-950 px-4 py-3 text-center">
              <p className="text-xl font-bold text-white">{s.value}</p>
              <p className="text-[11px] font-semibold text-white/90 mt-0.5">{s.label}</p>
              <p className="text-[10px] font-medium text-indigo-300 mt-0.5">{s.note}</p>
            </div>
          ))}
        </div>
      </div>

      {/* ── Block 1: Current Plan ──────────────────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="01" label={txt("Plano Atual", "Current Plan")} />
        <Card className="border-border/60 bg-card opacity-80">
          <CardContent className="pt-5">
            <div className="rounded-lg border border-emerald-500/25 bg-gradient-to-r from-emerald-500/5 via-transparent to-emerald-500/5 px-5 py-5 space-y-3">
              <div className="flex items-center justify-between gap-3">
                <div className="flex items-center gap-2.5">
                  <Star className="h-5 w-5 text-emerald-500 shrink-0" />
                  <p className="text-base font-bold text-foreground">NeuroStack Enterprise</p>
                </div>
                <Badge variant="outline" className="gap-1 text-[10px] font-bold bg-emerald-500/10 border-emerald-500/30 text-emerald-600 dark:text-emerald-400 px-2.5 py-1">
                  <CheckCircle2 className="h-3 w-3" />
                  {txt("Ativo", "Active")}
                </Badge>
              </div>

              <div className="flex items-center gap-2 pt-1">
                <Button disabled size="sm" className="gap-1.5 opacity-50 cursor-not-allowed">
                  <Zap className="h-3.5 w-3.5" />
                  {txt("Fazer Upgrade de Plano", "Upgrade Plan")}
                </Button>
                <Button disabled size="sm" variant="outline" className="gap-1.5 opacity-50 cursor-not-allowed border-border/60">
                  {txt("Gerenciar Assinatura", "Manage Subscription")}
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* ── Block 2: Credits ────────────────────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="02" label={txt("Recarga de Créditos (Add-ons)", "Credit Top-Up (Add-ons)")} />
        <Card className="border-border/60 bg-card opacity-80">
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <Wallet className="h-4 w-4 text-emerald-500" />
              <CardTitle className="text-sm font-semibold text-foreground">
                {txt("Saldo de Créditos Extras", "Extra Credits Balance")}
              </CardTitle>
            </div>
            <p className="text-xs font-medium text-foreground/75 mt-1">
              {txt(
                "Adquira pacotes de créditos para uso de funcionalidades Premium — Voz Sintética ElevenLabs e SLM Cortex.",
                "Purchase credit packages for Premium features — ElevenLabs Synthetic Voice and SLM Cortex."
              )}
            </p>
          </CardHeader>
          <CardContent className="space-y-4">
            <Button disabled size="sm" variant="outline" className="gap-2 opacity-50 cursor-not-allowed border-border/60 w-full">
              <Plus className="h-3.5 w-3.5" />
              {txt("Adicionar Créditos (Pacotes Avulsos)", "Add Credits (Individual Packages)")}
            </Button>
          </CardContent>
        </Card>
      </div>

      {/* ── Block 3: Payment Methods ────────────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="03" label={txt("Métodos de Pagamento", "Payment Methods")} />
        <Card className="border-border/60 bg-card opacity-80">
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <CreditCard className="h-4 w-4 text-emerald-500" />
              <CardTitle className="text-sm font-semibold text-foreground">
                {txt("Formas de Pagamento Cadastradas", "Registered Payment Methods")}
              </CardTitle>
            </div>
          </CardHeader>
          <CardContent className="space-y-2.5">
            <PaymentMethodRow
              icon={CreditCard}
              label={txt("Cartão de Crédito", "Credit Card")}
              detail="Mastercard •••• 4242"
              active
            />
            <PaymentMethodRow
              icon={Zap}
              label={txt("PIX (Faturamento Automático)", "PIX (Automatic Billing)")}
              detail={txt("QR Code · Copia e Cola", "QR Code · Copy & Paste")}
            />
            <Button disabled size="sm" variant="outline" className="gap-2 opacity-50 cursor-not-allowed border-border/60 w-full mt-1">
              <Plus className="h-3.5 w-3.5" />
              {txt("Adicionar Novo Cartão / Método", "Add New Card / Method")}
            </Button>
          </CardContent>
        </Card>
      </div>

      {/* ── Block 4: Invoice History ────────────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="04" label={txt("Histórico de Faturas", "Invoice History")} />
        <Card className="border-border/60 bg-card opacity-80">
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <FileText className="h-4 w-4 text-emerald-500" />
              <CardTitle className="text-sm font-semibold text-foreground">
                {txt("Transações Recentes", "Recent Transactions")}
              </CardTitle>
            </div>
          </CardHeader>
          <CardContent>
            <InvoiceRow description={txt("Fatura Referente à Assinatura Mensal", "Monthly Subscription Invoice")} status="Paga" />
            <InvoiceRow description={txt("Compra de Pacote de Créditos (Voz Sintética ElevenLabs)", "Credit Package Purchase (ElevenLabs Synthetic Voice)")} status="Paga" />
            <InvoiceRow description={txt("Fatura Referente à Assinatura Mensal (Ciclo Anterior)", "Monthly Subscription Invoice (Previous Cycle)")} status="Paga" />
            <div className="pt-3">
              <Button disabled variant="ghost" size="sm" className="gap-2 opacity-40 cursor-not-allowed text-xs w-full">
                <FileText className="h-3.5 w-3.5" />
                {txt("Ver Histórico Completo", "View Full History")}
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
