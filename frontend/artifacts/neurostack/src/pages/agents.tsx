import {
  Network, ArrowRight, Bot, Calendar, RefreshCw, Heart,
  Info, Webhook, CreditCard, Copy, Check,
} from "lucide-react";
import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { useI18n } from "@/context/i18n";

function TriggerDocs() {
  const { locale } = useI18n();
  const txt = (p: string, e: string) => locale === "en" ? e : p;
  const [copied, setCopied] = useState<string | null>(null);

  const TRIGGERS = [
    { slug: "agendamento",  label: txt("Agendamento",        "Scheduling")          },
    { slug: "follow-up",   label: txt("Follow-up",           "Follow-up")           },
    { slug: "resgatar",    label: txt("Resgate de Inativos", "Inactive Reactivation") },
    { slug: "fidelizacao", label: txt("Fidelização",         "Loyalty")             },
    { slug: "pagamento",   label: txt("Pagamento",           "Payment")             },
  ];

  function copy(slug: string) {
    const path = `/api/agent/${slug}/trigger`;
    navigator.clipboard.writeText(path).catch(() => {});
    setCopied(slug);
    setTimeout(() => setCopied(null), 1500);
  }

  return (
    <Card className="border-border shadow-sm">
      <CardHeader className="pb-3 bg-slate-50/50 dark:bg-slate-900/50 border-b border-border">
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-2">
            <Webhook className="h-4 w-4 text-accent shrink-0" />
            <CardTitle className="text-sm font-semibold uppercase tracking-wider text-foreground">
              {txt("Documentação de Gatilhos (Triggers)", "Trigger Documentation")}
            </CardTitle>
          </div>
          <Badge
            variant="outline"
            className="text-[10px] font-bold uppercase tracking-wider text-violet-500 border-violet-400/40 shrink-0"
          >
            Sandboxed · V2
          </Badge>
        </div>
        <CardDescription className="text-xs mt-1">
          {txt(
            "Acione agentes especializados via endpoints seguros, compatíveis com qualquer plataforma de automação.",
            "Trigger specialized agents via secure endpoints, compatible with any automation platform."
          )}
        </CardDescription>
      </CardHeader>

      <CardContent className="pt-4 space-y-2">
        {TRIGGERS.map(({ slug, label }) => {
          const path = `/api/agent/${slug}/trigger`;
          const isCopied = copied === slug;
          return (
            <div
              key={slug}
              className="group flex items-center gap-3 rounded-lg bg-slate-950 border border-slate-800 px-4 py-2.5"
            >
              <span className="shrink-0 rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">
                POST
              </span>
              <code className="flex-1 font-mono text-xs text-slate-300 min-w-0 truncate">
                /api/agent/
                <span className="text-white font-semibold">{slug}</span>
                /trigger
              </code>
              <span className="hidden sm:block text-[10px] text-slate-500 shrink-0">{label}</span>
              <button
                type="button"
                onClick={() => copy(slug)}
                aria-label={`Copy ${path}`}
                className="shrink-0 ml-1 text-slate-600 hover:text-slate-300 transition-colors"
              >
                {isCopied
                  ? <Check className="h-3.5 w-3.5 text-emerald-400" />
                  : <Copy className="h-3.5 w-3.5" />
                }
              </button>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}

export default function Agents() {
  const { locale } = useI18n();
  const txt = (p: string, e: string) => locale === "en" ? e : p;

  const AGENTS = [
    { icon: Calendar,   name: txt("Agendamento",        "Scheduling"),            color: "text-blue-500"    },
    { icon: RefreshCw,  name: txt("Follow-up",           "Follow-up"),            color: "text-emerald-500" },
    { icon: Bot,        name: txt("Resgate de Inativos", "Inactive Reactivation"), color: "text-orange-500"  },
    { icon: Heart,      name: txt("Fidelização",         "Loyalty"),               color: "text-rose-500"    },
    { icon: CreditCard, name: txt("Pagamento",           "Payment"),               color: "text-violet-500"  },
  ];

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-start gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-violet-500/10 border border-violet-500/20 shrink-0">
          <Network className="h-5 w-5 text-violet-500" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h1 className="text-xl font-semibold text-foreground">
              {txt("Conexões & Agentes", "Connections & Agents")}
            </h1>
            <Badge className="text-[10px] font-bold uppercase tracking-wider bg-violet-100 text-violet-700 border-violet-200 dark:bg-violet-900/40 dark:text-violet-300 dark:border-violet-800">
              {txt("EM BREVE", "COMING SOON")}
            </Badge>
          </div>
          <p className="text-sm text-muted-foreground mt-0.5">
            {txt(
              "Roteamento Multi-Agente via API/Webhook para cada etapa do funil clínico.",
              "Multi-Agent routing via API/Webhook for each stage of the clinical funnel."
            )}
          </p>
        </div>
      </div>

      {/* Agent Control Center */}
      <Card className="border-border shadow-sm">
        <CardHeader className="pb-3 bg-slate-50/50 dark:bg-slate-900/50 border-b border-border">
          <CardTitle className="text-sm font-semibold uppercase tracking-wider text-foreground">
            {txt("Agentes Especializados", "Specialized Agents")}
          </CardTitle>
          <CardDescription className="text-xs">
            {txt(
              "Configure quais agentes ficam ativos na sua stack de atendimento.",
              "Configure which agents are active in your service stack."
            )}
          </CardDescription>
        </CardHeader>
        <CardContent className="pt-0 divide-y divide-border">
          {AGENTS.map((agent) => {
            const Icon = agent.icon;
            return (
              <div key={agent.name} className="flex items-center gap-4 py-3.5 px-1">
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-muted border border-border">
                  <Icon className={`h-4 w-4 ${agent.color}`} />
                </div>
                <p className="flex-1 text-sm font-medium text-foreground">{agent.name}</p>
                <Switch
                  checked={false}
                  disabled
                  aria-label={`${txt("Ativar agente", "Enable agent")} ${agent.name}`}
                  className="shrink-0 opacity-40 cursor-not-allowed"
                />
              </div>
            );
          })}
        </CardContent>
      </Card>

      {/* Trigger Docs */}
      <TriggerDocs />

      {/* Routing Logic */}
      <Card className="border-border shadow-sm">
        <CardHeader className="pb-3 bg-slate-50/50 dark:bg-slate-900/50 border-b border-border">
          <div className="flex items-center gap-2">
            <Info className="h-4 w-4 text-accent" />
            <CardTitle className="text-sm font-semibold uppercase tracking-wider text-foreground">
              {txt("Lógica de Roteamento", "Routing Logic")}
            </CardTitle>
          </div>
        </CardHeader>
        <CardContent className="pt-5 space-y-5">
          <div className="flex items-stretch gap-3 text-xs">
            <div className="flex-1 rounded-lg border border-border bg-muted/50 p-3 text-center">
              <p className="font-semibold text-foreground">NeuroStack Agent</p>
              <p className="text-muted-foreground mt-0.5">{txt("detecta handoff", "detects handoff")}</p>
            </div>
            <div className="flex items-center shrink-0">
              <ArrowRight className="h-4 w-4 text-muted-foreground" />
            </div>
            <div className="flex-1 flex flex-col gap-2">
              <div className="flex-1 rounded-lg border border-emerald-200 bg-emerald-50 dark:border-emerald-800 dark:bg-emerald-950/30 p-3 text-center">
                <p className="font-semibold text-emerald-700 dark:text-emerald-400">
                  {txt("Agente Especialista", "Specialist Agent")}
                </p>
                <p className="text-emerald-600 dark:text-emerald-500 mt-0.5 text-[11px]">
                  toggle <span className="font-bold">ON</span>
                </p>
              </div>
              <div className="flex-1 rounded-lg border border-slate-200 bg-slate-50 dark:border-slate-700 dark:bg-slate-900/50 p-3 text-center">
                <p className="font-semibold text-slate-600 dark:text-slate-400">
                  {txt("Atendimento Humano", "Human Support")}
                </p>
                <p className="text-slate-500 mt-0.5 text-[11px]">
                  toggle <span className="font-bold">OFF</span>
                </p>
              </div>
            </div>
          </div>

          <p className="text-xs text-muted-foreground leading-relaxed bg-muted/30 rounded-md border border-border px-3 py-2.5">
            {txt(
              <>Se ativado, o handoff vai para o <span className="font-semibold text-foreground">Agente Especialista</span> da stack do builder. Se desativado, o handoff é roteado para o <span className="font-semibold text-foreground">Atendimento Humano</span>.</>,
              <>If enabled, handoff goes to the <span className="font-semibold text-foreground">Specialist Agent</span> of the builder's stack. If disabled, handoff is routed to <span className="font-semibold text-foreground">Human Support</span>.</>
            )}
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
