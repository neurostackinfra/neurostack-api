import { Link2, ArrowRight, Zap, BarChart2 } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { useI18n } from "@/context/i18n";

const CRM_PLATFORMS = ["RD Station", "HubSpot", "Kommo", "Agendor", "Outro"];

export default function CRMIntegration() {
  const { locale } = useI18n();
  const txt = (p: string, e: string) => locale === "en" ? e : p;

  const FUNNEL_STAGES = [
    {
      neurostack: txt("Lead Frio", "Cold Lead"),
      intent: "intent: curiosity",
      crmDefault: txt("Novo Lead", "New Lead"),
      crmOptions: [txt("Novo Lead", "New Lead"), txt("Lead Capturado", "Lead Captured"), txt("Primeiro Contato", "First Contact")],
      color: "border-slate-300 dark:border-slate-700",
      dotColor: "bg-slate-400",
    },
    {
      neurostack: txt("Qualificação", "Qualification"),
      intent: "intent: interest",
      crmDefault: txt("Em Qualificação", "In Qualification"),
      crmOptions: [txt("Em Qualificação", "In Qualification"), txt("Lead Qualificado", "Qualified Lead"), txt("Em Avaliação", "Under Evaluation")],
      color: "border-blue-300 dark:border-blue-700",
      dotColor: "bg-blue-500",
    },
    {
      neurostack: txt("Agendamento Handoff", "Scheduling Handoff"),
      intent: "intent: schedule",
      crmDefault: txt("Consulta Agendada", "Appointment Booked"),
      crmOptions: [txt("Consulta Agendada", "Appointment Booked"), txt("Consulta Marcada", "Consultation Set"), txt("Oportunidade Quente", "Hot Opportunity")],
      color: "border-emerald-300 dark:border-emerald-700",
      dotColor: "bg-emerald-500",
    },
    {
      neurostack: txt("Seguimento", "Follow-up"),
      intent: "intent: follow_up",
      crmDefault: txt("Em Negociação", "In Negotiation"),
      crmOptions: [txt("Em Negociação", "In Negotiation"), txt("Proposta Enviada", "Proposal Sent"), txt("Aguardando Retorno", "Awaiting Response")],
      color: "border-amber-300 dark:border-amber-700",
      dotColor: "bg-amber-500",
    },
    {
      neurostack: txt("Fidelização", "Loyalty"),
      intent: "intent: retention",
      crmDefault: txt("Cliente Ativo", "Active Client"),
      crmOptions: [txt("Cliente Ativo", "Active Client"), txt("Pós-Venda", "Post-Sale"), txt("Programa de Fidelidade", "Loyalty Program")],
      color: "border-rose-300 dark:border-rose-700",
      dotColor: "bg-rose-500",
    },
  ];

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-start gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-violet-500/10 border border-violet-500/20 shrink-0">
          <Link2 className="h-5 w-5 text-violet-500" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h1 className="text-xl font-semibold text-foreground">
              {txt("Integração CRM", "CRM Integration")}
            </h1>
            <Badge className="text-[10px] font-bold uppercase tracking-wider bg-violet-100 text-violet-700 border-violet-200 dark:bg-violet-900/40 dark:text-violet-300 dark:border-violet-800">
              {txt("EM BREVE", "COMING SOON")}
            </Badge>
          </div>
          <p className="text-sm text-muted-foreground mt-0.5">
            {txt(
              "Sincronização bidirecional em tempo real do Trace Decision com o seu CRM.",
              "Real-time bidirectional sync of Trace Decision with your CRM."
            )}
          </p>
        </div>
      </div>

      {/* CRM Platform Selector */}
      <Card className="border-border shadow-sm">
        <CardHeader className="pb-3 bg-slate-50/50 dark:bg-slate-900/50 border-b border-border">
          <CardTitle className="text-sm font-semibold uppercase tracking-wider text-foreground">
            {txt("Ecossistema CRM", "CRM Ecosystem")}
          </CardTitle>
          <CardDescription className="text-xs">
            {txt(
              "Selecione o CRM que receberá os eventos do pipeline cognitivo.",
              "Select the CRM that will receive events from the cognitive pipeline."
            )}
          </CardDescription>
        </CardHeader>
        <CardContent className="pt-5">
          <div className="grid grid-cols-5 gap-2">
            {CRM_PLATFORMS.map((crm, i) => (
              <div
                key={crm}
                className={`flex flex-col items-center justify-center rounded-lg border p-3 text-center text-xs font-medium cursor-not-allowed opacity-50 ${
                  i === 0
                    ? "border-accent bg-accent/5 text-accent"
                    : "border-border text-muted-foreground"
                }`}
              >
                <BarChart2 className="h-5 w-5 mb-1.5 shrink-0" />
                <span className="leading-tight">{crm}</span>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* Funnel Mapping */}
      <Card className="border-border shadow-sm">
        <CardHeader className="pb-3 bg-slate-50/50 dark:bg-slate-900/50 border-b border-border">
          <div className="flex items-center gap-2">
            <Zap className="h-4 w-4 text-accent" />
            <CardTitle className="text-sm font-semibold uppercase tracking-wider text-foreground">
              {txt("Mapeamento de Funil", "Funnel Mapping")}
            </CardTitle>
          </div>
          <CardDescription className="text-xs">
            {txt(
              "Mapeie cada stage cognitivo do NeuroStack para uma etapa do seu pipeline de CRM.",
              "Map each NeuroStack cognitive stage to a step in your CRM pipeline."
            )}
          </CardDescription>
        </CardHeader>
        <CardContent className="pt-4 space-y-0">
          {/* Column Headers */}
          <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-3 px-1 pb-2 mb-2 border-b border-border">
            <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
              NeuroStack Stage
            </p>
            <div className="w-5" />
            <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
              CRM Stage (RD Station)
            </p>
          </div>

          {FUNNEL_STAGES.map((stage) => (
            <div
              key={stage.neurostack}
              className="grid grid-cols-[1fr_auto_1fr] items-center gap-3 py-3 border-b border-border/50 last:border-0"
            >
              <div className={`rounded-md border ${stage.color} bg-muted/30 px-3 py-2`}>
                <div className="flex items-center gap-2">
                  <span className={`h-2 w-2 rounded-full shrink-0 ${stage.dotColor}`} />
                  <span className="text-sm font-medium text-foreground">{stage.neurostack}</span>
                </div>
                <p className="text-[10px] font-mono text-muted-foreground mt-0.5 ml-4">
                  {stage.intent}
                </p>
              </div>

              <ArrowRight className="h-4 w-4 text-muted-foreground/50 shrink-0" />

              <Select value={stage.crmDefault} disabled>
                <SelectTrigger className="h-[52px] text-sm opacity-60 cursor-not-allowed">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {stage.crmOptions.map((opt) => (
                    <SelectItem key={opt} value={opt}>
                      {opt}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          ))}
        </CardContent>
      </Card>

      {/* Sync Info */}
      <div className="space-y-3">
        <div className="flex items-start gap-2 rounded-md border border-accent/20 bg-accent/5 px-4 py-3 text-sm text-foreground">
          <Zap className="h-4 w-4 text-accent mt-0.5 shrink-0" />
          <p>
            {txt(
              <>Cada transição de stage no NeuroStack dispara um{" "}<span className="font-semibold">webhook em tempo real</span> para atualizar automaticamente o pipeline do seu CRM, sem intervenção manual.</>,
              <>Every stage transition in NeuroStack triggers a{" "}<span className="font-semibold">real-time webhook</span> to automatically update your CRM pipeline, no manual intervention needed.</>
            )}
          </p>
        </div>

        <div className="flex items-start gap-2 rounded-md border border-violet-200 bg-violet-50 px-4 py-3 text-sm text-violet-700 dark:border-violet-800 dark:bg-violet-950/40 dark:text-violet-300">
          <Link2 className="h-4 w-4 mt-0.5 shrink-0" />
          <p>
            {txt(
              <>A integração CRM bidirecional estará disponível na <span className="font-semibold">v2.0</span>. Suporte inicial para RD Station, HubSpot, Kommo e Agendor, com SDK aberto para demais plataformas.</>,
              <>Bidirectional CRM integration will be available in <span className="font-semibold">v2.0</span>. Initial support for RD Station, HubSpot, Kommo and Agendor, with open SDK for other platforms.</>
            )}
          </p>
        </div>
      </div>
    </div>
  );
}
