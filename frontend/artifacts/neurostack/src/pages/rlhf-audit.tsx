import { Brain, ShieldAlert, CheckCircle2, Filter, Download, FlaskConical, Zap } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useToast } from "@/hooks/use-toast";

interface AuditEntry {
  id: string;
  ts: string;
  clinic: string;
  submittedBy: string;
  userInput: string;
  agentResponse: string;
  suggestedResponse: string;
  status: "aligned" | "forced";
  intent: string;
  approvedForTraining: boolean;
}

const MOCK_ENTRIES: AuditEntry[] = [
  {
    id: "rlhf-001",
    ts: "2026-04-08 09:14",
    clinic: "Clínica São Paulo",
    submittedBy: "dr.lima@clinicasp.com",
    userInput: "Qual o valor da rinoplastia?",
    agentResponse: "O preço varia conforme a complexidade do procedimento e será apresentado após avaliação.",
    suggestedResponse: "Antes de falarmos sobre valores, que tal agendar uma avaliação gratuita com nosso especialista? Assim podemos entender exatamente o que você precisa.",
    status: "aligned",
    intent: "pedido_preco",
    approvedForTraining: false,
  },
  {
    id: "rlhf-002",
    ts: "2026-04-07 16:38",
    clinic: "Clínica Rio Norte",
    submittedBy: "admin@rionorte.com",
    userInput: "Tenho medo de cirurgia, é perigoso?",
    agentResponse: "Sim, nossos procedimentos seguem todos os protocolos de segurança.",
    suggestedResponse: "Pode passar o diagnóstico do remédio que o médico receitou para que eu possa ajudar melhor?",
    status: "forced",
    intent: "interesse_tratamento",
    approvedForTraining: false,
  },
  {
    id: "rlhf-003",
    ts: "2026-04-07 11:02",
    clinic: "Clínica Belo Horizonte",
    submittedBy: "coordenador@bhmedi.com",
    userInput: "Fiquei insatisfeito com o resultado.",
    agentResponse: "Lamentamos muito. Por favor, entre em contato com nossa equipe.",
    suggestedResponse: "Sinto muito que sua experiência não foi positiva. Vou conectar você imediatamente com nosso coordenador médico para resolvermos isso da melhor forma, sem burocracia.",
    status: "aligned",
    intent: "reclamacao",
    approvedForTraining: true,
  },
];

const INTENT_LABELS: Record<string, string> = {
  pedido_preco: "Price Inquiry",
  interesse_tratamento: "Treatment Interest",
  reclamacao: "Reclamação",
  agendar: "Scheduling",
  duvida_pos_procedimento: "Pós-Procedimento",
};

export default function RlhfAudit() {
  const { toast } = useToast();

  const handleApprove = (id: string) => {
    toast({
      title: "Aprovado para Treinamento SLM",
      description: `Entrada ${id} adicionada ao dataset de fine-tuning.`,
    });
  };

  const stats = {
    total: MOCK_ENTRIES.length,
    aligned: MOCK_ENTRIES.filter((e) => e.status === "aligned").length,
    forced: MOCK_ENTRIES.filter((e) => e.status === "forced").length,
    approved: MOCK_ENTRIES.filter((e) => e.approvedForTraining).length,
  };

  return (
    <div className="space-y-7 max-w-6xl mx-auto">

      {/* ── Header ────────────────────────────────────────────────────────────── */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <div className="flex items-center gap-3 mb-2">
            <div className="h-9 w-9 rounded-lg bg-violet-500/15 border border-violet-500/30 flex items-center justify-center">
              <Brain className="h-5 w-5 text-violet-400" />
            </div>
            <div>
              <div className="flex items-center gap-2.5">
                <h1 className="text-2xl font-bold text-foreground tracking-tight">Auditoria RLHF</h1>
                <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 border border-amber-300 text-amber-800 text-[10px] font-bold uppercase tracking-widest px-2 py-0.5">
                  <Zap className="h-2.5 w-2.5" />
                  Preview do Roadmap
                </span>
              </div>
              <p className="text-xs text-violet-400 font-semibold uppercase tracking-widest">
                Relatório de Alinhamento - RLHF Data Flywheel
              </p>
            </div>
          </div>
          <p className="text-sm text-muted-foreground max-w-2xl">
            Registro centralizado de sugestões de correção enviadas pelos builders.
            Cada entrada representa um sinal de alinhamento comportamental para fine-tuning do agente.
          </p>
        </div>
        <div className="flex gap-2 shrink-0">
          <Button variant="outline" size="sm" className="gap-2 text-xs">
            <Filter className="h-3.5 w-3.5" />
            Filtrar
          </Button>
          <Button variant="outline" size="sm" className="gap-2 text-xs">
            <Download className="h-3.5 w-3.5" />
            Exportar Dataset
          </Button>
        </div>
      </div>

      {/* ── Stats ─────────────────────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
        {[
          { label: "Total de Feedbacks",   value: stats.total,   sub: "Todas as clínicas",      color: "text-foreground"  },
          { label: "Alinhados",            value: stats.aligned, sub: "Conformes ao Padrão SRM", color: "text-green-400"   },
          { label: "Forçados / Override",  value: stats.forced,  sub: "Ferem a Metodologia",     color: "text-red-400"     },
          { label: "Aprovados p/ SLM",     value: stats.approved,sub: "No dataset de treino",    color: "text-violet-400"  },
        ].map((s) => (
          <Card key={s.label} className="border-border bg-card shadow-sm">
            <CardContent className="pt-4 pb-4">
              <div className={`text-3xl font-bold ${s.color}`}>{s.value}</div>
              <div className="text-xs font-semibold text-foreground/80 mt-0.5">{s.label}</div>
              <div className="text-[10px] text-muted-foreground">{s.sub}</div>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* ── Restricted badge ──────────────────────────────────────────────────── */}
      <div className="flex items-center gap-2 px-3 py-2 rounded-md bg-gray-900 border border-black w-fit">
        <ShieldAlert className="h-3.5 w-3.5 text-white shrink-0" />
        <span className="text-xs font-bold text-white">Acesso restrito: Somente Master Admin. Contém metodologia proprietária de alinhamento SRM.</span>
      </div>

      {/* ── Data Table ────────────────────────────────────────────────────────── */}
      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <FlaskConical className="h-4 w-4 text-violet-400" />
          <h2 className="text-sm font-semibold text-foreground uppercase tracking-wider">
            Entradas de Feedback
          </h2>
          <span className="text-xs text-muted-foreground ml-1">({MOCK_ENTRIES.length} registros)</span>
        </div>

        {/* Table header */}
        <div className="hidden md:grid grid-cols-[1.2fr_0.9fr_2.2fr_0.9fr] gap-4 px-4 py-2 rounded-md bg-muted/50 border border-border">
          <span className="text-[10px] font-bold uppercase tracking-widest text-muted-foreground">Clínica / Usuário</span>
          <span className="text-[10px] font-bold uppercase tracking-widest text-muted-foreground">Status</span>
          <span className="text-[10px] font-bold uppercase tracking-widest text-muted-foreground">Feedback Original vs. Sugestão</span>
          <span className="text-[10px] font-bold uppercase tracking-widest text-muted-foreground">Ação</span>
        </div>

        {/* Table rows */}
        {MOCK_ENTRIES.map((entry) => (
          <Card key={entry.id} className="border-border shadow-sm overflow-hidden">
            {/* Mobile/small header */}
            <div className="flex items-center justify-between px-4 py-2.5 bg-slate-50/50 dark:bg-slate-900/50 border-b border-border">
              <span className="text-xs font-mono text-muted-foreground">{entry.id}</span>
              <div className="flex items-center gap-2">
                <Badge
                  variant="outline"
                  className={`text-[10px] py-0 gap-1 ${
                    entry.status === "aligned"
                      ? "bg-green-500/10 text-green-400 border-green-500/30"
                      : "bg-red-500/10 text-red-400 border-red-500/30"
                  }`}
                >
                  {entry.status === "aligned"
                    ? <><CheckCircle2 className="h-2.5 w-2.5" /> Alinhado</>
                    : <><ShieldAlert className="h-2.5 w-2.5" /> Forçado, Fere Metodologia</>
                  }
                </Badge>
                <span className="text-[10px] text-muted-foreground hidden sm:block">{entry.ts}</span>
              </div>
            </div>

            <CardContent className="pt-4 pb-4">
              {/* Desktop: 4-col grid */}
              <div className="grid grid-cols-1 md:grid-cols-[1.2fr_0.9fr_2.2fr_0.9fr] gap-4 items-start">

                {/* Col 1: Clínica / Usuário */}
                <div>
                  <div className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground mb-1 md:hidden">Clínica / Usuário</div>
                  <p className="text-sm font-semibold text-foreground">{entry.clinic}</p>
                  <p className="text-[11px] text-muted-foreground mt-0.5">{entry.submittedBy}</p>
                  <Badge variant="outline" className="mt-1.5 text-[9px] py-0 text-blue-400 border-blue-400/30 bg-blue-500/10">
                    {INTENT_LABELS[entry.intent] ?? entry.intent}
                  </Badge>
                </div>

                {/* Col 2: Status (detailed) */}
                <div>
                  <div className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground mb-1 md:hidden">Status</div>
                  {entry.status === "aligned" ? (
                    <div className="flex items-center gap-1.5 text-green-400">
                      <CheckCircle2 className="h-4 w-4 shrink-0" />
                      <div>
                        <p className="text-xs font-bold">Alinhado</p>
                        <p className="text-[10px] text-green-400/70">Conforme Padrão SRM</p>
                      </div>
                    </div>
                  ) : (
                    <div className="flex items-center gap-1.5 text-red-400">
                      <ShieldAlert className="h-4 w-4 shrink-0" />
                      <div>
                        <p className="text-xs font-bold">Override Forçado</p>
                        <p className="text-[10px] text-red-400/70">Fere a Metodologia</p>
                      </div>
                    </div>
                  )}
                  {entry.approvedForTraining && (
                    <Badge className="mt-2 text-[9px] bg-violet-500/15 text-violet-400 border border-violet-500/30 flex items-center gap-1 w-fit py-0">
                      <Zap className="h-2.5 w-2.5" /> No Dataset SLM
                    </Badge>
                  )}
                </div>

                {/* Col 3: Feedback comparison */}
                <div className="space-y-2">
                  <div className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground mb-1 md:hidden">Feedback vs. Sugestão</div>
                  <div>
                    <p className="text-[10px] font-bold text-gray-600 dark:text-gray-400 mb-0.5 uppercase tracking-wider">Resposta Original do Agente</p>
                    <p className="text-xs text-gray-900 dark:text-gray-100 italic bg-gray-50 dark:bg-gray-800 rounded px-2.5 py-1.5 border border-gray-200 dark:border-gray-700">
                      {entry.agentResponse}
                    </p>
                  </div>
                  <div>
                    <p className={`text-[10px] font-bold mb-0.5 uppercase tracking-wider ${
                      entry.status === "aligned" ? "text-emerald-700 dark:text-emerald-400" : "text-rose-700 dark:text-rose-400"
                    }`}>
                      {entry.status === "aligned" ? "✅ Sugestão Alinhada" : "⚠️ Sugestão Forçada"}
                    </p>
                    <p className={`text-xs rounded px-2.5 py-1.5 border font-medium ${
                      entry.status === "aligned"
                        ? "text-emerald-950 dark:text-emerald-100 bg-emerald-50 dark:bg-emerald-950/30 border-emerald-200 dark:border-emerald-800/40"
                        : "text-rose-950 dark:text-rose-100 bg-rose-50 dark:bg-rose-950/30 border-rose-200 dark:border-rose-800/40"
                    }`}>
                      {entry.suggestedResponse}
                    </p>
                  </div>
                </div>

                {/* Col 4: Action */}
                <div className="flex flex-col gap-2 justify-start">
                  <div className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground mb-1 md:hidden">Ação</div>
                  {entry.approvedForTraining ? (
                    <div className="flex items-center gap-1 text-[11px] text-violet-400 font-medium">
                      <Zap className="h-3.5 w-3.5" />
                      Aprovado
                    </div>
                  ) : (
                    <Button
                      size="sm"
                      className="text-[11px] h-7 bg-violet-600 text-white gap-1.5 w-full opacity-60 cursor-not-allowed"
                      disabled
                      title="Pipeline SLM disponível na V2 - Roadmap"
                    >
                      <Zap className="h-3 w-3" />
                      Aprovar para Treinamento SLM
                    </Button>
                  )}
                  {entry.status === "forced" ? (
                    <p className="text-[10px] text-rose-600 dark:text-rose-400 font-medium">
                      Não elegível, fere metodologia
                    </p>
                  ) : (
                    <p className="text-[10px] text-amber-600 dark:text-amber-400 font-medium">
                      Pipeline SLM - V2
                    </p>
                  )}
                </div>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
