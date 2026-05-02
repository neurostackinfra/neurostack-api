import { DecisionTrace } from "@workspace/api-client-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  AlertTriangle,
  Activity,
  ShieldAlert,
  KeyRound,
  Lock,
  FlaskConical,
  ChevronDown,
} from "lucide-react";
import { useState } from "react";
import { useAuth } from "@/context/auth";
import { useI18n } from "@/context/i18n";
import type { TranslationKey } from "@/locales/en";

export function IntentBadge({ intent }: { intent: string }) {
  const intentConfig: Record<string, { className: string; label: string }> = {
    // Greeting / Farewell
    saudacao: {
      className: "bg-sky-500 hover:bg-sky-600 text-white",
      label: "Saudação",
    },
    // Interest & Qualification
    interesse_tratamento: {
      className: "bg-violet-500 hover:bg-violet-600 text-white",
      label: "Interesse em Tratamento",
    },
    qualificacao_publico: {
      className: "bg-purple-500 hover:bg-purple-600 text-white",
      label: "Qualificação",
    },
    comparacao_tratamentos: {
      className: "bg-fuchsia-500 hover:bg-fuchsia-600 text-white",
      label: "Comparação",
    },
    // Price & Objections
    pedido_preco: {
      className: "bg-blue-500 hover:bg-blue-600 text-white",
      label: "Pedido de Preço",
    },
    objecao_vou_pensar: {
      className: "bg-amber-500 hover:bg-amber-600 text-white",
      label: "Objeção",
    },
    // Scheduling
    agendar: {
      className: "bg-green-500 hover:bg-green-600 text-white",
      label: "Agendamento",
    },
    reagendar: {
      className: "bg-emerald-500 hover:bg-emerald-600 text-white",
      label: "Reagendamento",
    },
    cancelar: {
      className: "bg-orange-500 hover:bg-orange-600 text-white",
      label: "Cancelamento",
    },
    confirmar_presenca: {
      className: "bg-teal-500 hover:bg-teal-600 text-white",
      label: "Confirmação",
    },
    disponibilidade_horarios: {
      className: "bg-cyan-500 hover:bg-cyan-600 text-white",
      label: "Horários",
    },
    // Info & FAQ
    detalhes_servicos: {
      className: "bg-indigo-400 hover:bg-indigo-500 text-white",
      label: "Detalhes Serviços",
    },
    informacoes_profissionais: {
      className: "bg-indigo-500 hover:bg-indigo-600 text-white",
      label: "Info Profissionais",
    },
    faq: {
      className: "bg-slate-500 hover:bg-slate-600 text-white",
      label: "FAQ",
    },
    cultura_historia: {
      className: "bg-slate-400 hover:bg-slate-500 text-white",
      label: "Sobre a Clínica",
    },
    duvida_generica_sem_contexto: {
      className: "bg-gray-500 hover:bg-gray-600 text-white",
      label: "Dúvida Genérica",
    },
    pre_atendimento: {
      className: "bg-lime-500 hover:bg-lime-600 text-white",
      label: "Pré-Atendimento",
    },
    // Post-care
    pos_atendimento_duvida: {
      className: "bg-indigo-500 hover:bg-indigo-600 text-white",
      label: "Pós — Dúvida",
    },
    pos_atendimento_sintomas_preocupantes: {
      className: "bg-red-500 hover:bg-red-600 text-white",
      label: "Pós — Sintomas",
    },
    pedido_alterar_medicacao: {
      className: "bg-red-600 hover:bg-red-700 text-white",
      label: "Alterar Medicação",
    },
    // Admin / Financial
    documentos_e_comprovantes: {
      className: "bg-zinc-500 hover:bg-zinc-600 text-white",
      label: "Documentos",
    },
    orcamento_existente: {
      className: "bg-blue-400 hover:bg-blue-500 text-white",
      label: "Orçamento",
    },
    cobranca_renegociacao: {
      className: "bg-yellow-500 hover:bg-yellow-600 text-white",
      label: "Financeiro",
    },
    // Escalation
    reclamacao: {
      className: "bg-rose-500 hover:bg-rose-600 text-white",
      label: "Reclamação",
    },
    conduta_inadequada_ameaca: {
      className: "bg-red-700 hover:bg-red-800 text-white",
      label: "Conduta Inadequada",
    },
    pedido_atendente_humano: {
      className: "bg-rose-600 hover:bg-rose-700 text-white",
      label: "Pedido Atendente Humano",
    },
    // Patient & Out-of-scope
    paciente_ativo: {
      className: "bg-teal-600 hover:bg-teal-700 text-white",
      label: "Paciente Ativo",
    },
    fora_do_escopo: {
      className: "bg-stone-500 hover:bg-stone-600 text-white",
      label: "Fora do Escopo",
    },
    fora_do_horario: {
      className: "bg-stone-600 hover:bg-stone-700 text-white",
      label: "Fora do Horário",
    },
  };

  const config = intentConfig[intent];
  return (
    <Badge
      className={config?.className ?? "bg-slate-500 text-white"}
      data-testid={`badge-intent-${intent}`}
    >
      {config?.label ?? intent}
    </Badge>
  );
}

const STAGE_LABELS: Record<string, string> = {
  initial: "Inicial",
  em_qualificacao: "Em Qualificação",
  qualificado: "Qualificado",
  followup: "Follow-up",
  agendamento_solicitado: "Agendamento Solicitado",
  agendado: "Agendado",
  paciente_ativo: "Paciente Ativo",
  pos_atendimento: "Pós-Atendimento",
  fidelizacao: "Fidelização",
  inativo: "Inativo",
  escalation: "Escalação",
};

const CONSTRAINT_META: Record<
  string,
  { label: string; className: string; icon: React.ReactNode }
> = {
  C_SEQUENCE_AUTHORITY_BEFORE_PRICE: {
    label: "C_SEQUENCE_AUTHORITY_BEFORE_PRICE",
    className:
      "bg-amber-50 text-amber-900 border-amber-300 dark:bg-amber-900/20 dark:text-amber-300 dark:border-amber-700",
    icon: <Lock className="h-3 w-3" />,
  },
};

function PayloadSection({ title, data }: { title: string; data: any }) {
  const [open, setOpen] = useState(false);
  if (!data || (typeof data === "object" && Object.keys(data).length === 0)) return null;

  const content = typeof data === "string" ? data : JSON.stringify(data, null, 2);

  return (
    <div className="border-t border-slate-800">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between px-5 py-3 text-xs font-mono font-semibold text-slate-400 hover:text-slate-200 transition-colors uppercase tracking-wider"
      >
        {title}
        <ChevronDown className={`h-3.5 w-3.5 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <pre className="text-xs text-green-400 font-mono whitespace-pre-wrap leading-relaxed px-5 pb-5 max-h-[500px] overflow-auto">
          {content}
        </pre>
      )}
    </div>
  );
}

function ctxFieldTitle(field: string, count: string | undefined, t: (k: TranslationKey) => string): string {
  const key = `ctx_field_${field.replace(/\./g, "_")}` as TranslationKey;
  const base = t(key) || field;
  return count ? `${base} (${count})` : base;
}

export function TraceViewer({ trace }: { trace: DecisionTrace }) {
  const { isSuperAdmin } = useAuth();
  const { t } = useI18n();
  const srm = trace.srmDecision;
  const hasConstraints = srm.constraints && srm.constraints.length > 0;
  const hasFlags =
    srm.gate_authority_before_price || srm.safety_concern || srm.handoff_required;

  return (
    <div className="space-y-4">
      {/* SRM Decision Panel */}
      <Card className="border-border shadow-sm">
        <CardHeader className="pb-2 bg-slate-50/50 dark:bg-slate-900/50 border-b border-border">
          <CardTitle className="text-sm font-semibold text-foreground flex items-center gap-2 uppercase tracking-wider">
            <Activity className="h-4 w-4" />
            SRM Decision
          </CardTitle>
        </CardHeader>
        <CardContent className="pt-4 space-y-4">
          {/* Core fields grid */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <div className="text-xs font-medium text-muted-foreground mb-1 uppercase tracking-wider">
                Intent
              </div>
              <IntentBadge intent={srm.intent} />
            </div>
            <div>
              <div className="text-xs font-medium text-muted-foreground mb-1 uppercase tracking-wider">
                Stage
              </div>
              <div className="text-sm font-mono font-medium" data-testid="trace-stage">
                {STAGE_LABELS[srm.stage] ?? srm.stage}
              </div>
            </div>
            <div>
              <div className="text-xs font-medium text-muted-foreground mb-1 uppercase tracking-wider">
                Confidence
              </div>
              <div className="text-sm font-medium" data-testid="trace-confidence">
                {(srm.confidence * 100).toFixed(1)}%
              </div>
            </div>
            <div>
              <div className="text-xs font-medium text-muted-foreground mb-1 uppercase tracking-wider">
                Processing
              </div>
              <div className="text-sm font-medium" data-testid="trace-processing-time">
                {Math.round(trace.processingTimeMs)}ms
              </div>
            </div>
          </div>

          {/* Active Constraints */}
          {hasConstraints && (
            <div className="pt-2 border-t border-border">
              <div className="text-xs font-medium text-muted-foreground mb-2 uppercase tracking-wider flex items-center gap-1">
                <Lock className="h-3 w-3" />
                Active Constraints
              </div>
              <div className="flex flex-wrap gap-2">
                {srm.constraints.map((constraint) => {
                  const meta = CONSTRAINT_META[constraint];
                  return (
                    <Badge
                      key={constraint}
                      variant="outline"
                      className={`gap-1 font-mono text-xs ${meta?.className ?? "bg-amber-50 text-amber-900 border-amber-300"}`}
                      data-testid={`constraint-badge-${constraint}`}
                    >
                      {meta?.icon ?? <Lock className="h-3 w-3" />}
                      {constraint}
                    </Badge>
                  );
                })}
              </div>
            </div>
          )}

          {/* Routing Flags */}
          {hasFlags && (
            <div className="flex flex-wrap gap-2 pt-2 border-t border-border">
              {srm.gate_authority_before_price && (
                <Badge
                  variant="outline"
                  className="gap-1 bg-blue-50 text-blue-800 border-blue-200 dark:bg-blue-900/30 dark:text-blue-300"
                  data-testid="flag-gate-authority"
                >
                  <KeyRound className="h-3 w-3" />
                  Gate: Authority Before Price
                </Badge>
              )}
              {srm.safety_concern && (
                <Badge
                  variant="destructive"
                  className="gap-1 bg-red-100 text-red-800 border-red-200 dark:bg-red-900/30 dark:text-red-400"
                  data-testid="flag-safety-concern"
                >
                  <ShieldAlert className="h-3 w-3" />
                  Safety Concern
                </Badge>
              )}
              {srm.handoff_required && (
                <Badge
                  variant="outline"
                  className="gap-1 bg-orange-100 text-orange-800 border-orange-200 dark:bg-orange-900/30 dark:text-orange-400"
                  data-testid="flag-handoff"
                >
                  <AlertTriangle className="h-3 w-3" />
                  Handoff Required
                </Badge>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Context Sources Panel */}
      <Card className="border-border shadow-sm">
        <CardHeader className="pb-2 bg-slate-50/50 dark:bg-slate-900/50 border-b border-border">
          <CardTitle className="text-sm font-semibold text-foreground uppercase tracking-wider">
            {t("ctx_panel_title")}
          </CardTitle>
        </CardHeader>
        <CardContent className="pt-4">
          {trace.selectedContext && trace.selectedContext.length > 0 ? (
            <div className="space-y-3">
              {trace.selectedContext.map((ctx, idx) => {
                const countMatch = ctx.title?.match(/\((\d+)\)$/);
                const count = countMatch ? countMatch[1] : undefined;
                const fieldKey = ctx.field || ctx.key;
                const label = ctxFieldTitle(fieldKey, count, t);
                return (
                  <div
                    key={idx}
                    className="text-sm bg-muted rounded-md p-3 border border-border"
                    data-testid={`context-item-${idx}`}
                  >
                    <div className="flex justify-between items-start mb-1">
                      <div className="flex flex-col gap-0.5">
                        <span className="text-xs font-bold text-foreground">
                          {label}
                        </span>
                        {fieldKey && (
                          <span className="font-mono text-[10px] text-muted-foreground">
                            {fieldKey}
                          </span>
                        )}
                      </div>
                      <span className={`text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded font-medium ${
                        ctx.source === "rag"
                          ? "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300"
                          : "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300"
                      }`}>
                        {ctx.source === "rag" ? t("ctx_origin_rag") : t("ctx_origin_config")}
                      </span>
                    </div>
                    <div className="text-foreground mt-1">{ctx.value}</div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="text-sm text-muted-foreground italic">
              {t("ctx_empty")}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Developer Payload — Admin & Builder (not investor) */}
      {isSuperAdmin && (
        <Card className="border-slate-700 shadow-lg overflow-hidden">
          <CardHeader className="pb-2.5 pt-3 bg-slate-900 border-b border-slate-700">
            <CardTitle className="text-sm font-semibold text-slate-100 flex items-center gap-2 uppercase tracking-wider">
              <FlaskConical className="h-4 w-4 text-amber-400" />
              Developer Payload
              <span className="ml-auto text-[10px] font-bold px-2 py-0.5 rounded bg-amber-400/15 text-amber-300 border border-amber-400/30 normal-case tracking-normal">
                Interno
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent className="p-0 bg-black">
            {trace.promptBuilt ? (
              <div
                className="overflow-x-auto border-t-0"
                data-testid="trace-prompt"
              >
                {/* Terminal top bar */}
                <div className="flex items-center gap-1.5 px-4 py-2 bg-slate-900 border-b border-slate-800">
                  <span className="h-2.5 w-2.5 rounded-full bg-red-500/70" />
                  <span className="h-2.5 w-2.5 rounded-full bg-yellow-500/70" />
                  <span className="h-2.5 w-2.5 rounded-full bg-green-500/70" />
                  <span className="ml-3 text-[10px] font-mono text-slate-500 tracking-wider">
                    system_prompt.txt — read-only
                  </span>
                </div>
                <pre className="text-xs text-green-400 font-mono whitespace-pre-wrap leading-relaxed p-5 max-h-[400px] overflow-auto">
                  {trace.promptBuilt}
                </pre>
              </div>
            ) : null}
            <PayloadSection title="SRM Decision (raw)" data={trace.srmDecision} />
            <PayloadSection title="Full Prompt" data={trace.fullPrompt} />
            <PayloadSection title="Full Context" data={trace.fullContext} />
            <PayloadSection title="Event Data" data={trace.eventData} />
            <PayloadSection title="Event Metadata" data={trace.eventMetadata} />
          </CardContent>
        </Card>
      )}

      {/* LLM Response Panel */}
      <Card className="border-border shadow-sm">
        <CardHeader className="pb-2 bg-slate-50/50 dark:bg-slate-900/50 border-b border-border">
          <CardTitle className="text-sm font-semibold text-foreground uppercase tracking-wider">
            LLM Response
          </CardTitle>
        </CardHeader>
        <CardContent className="pt-4">
          <div
            className="bg-slate-950 rounded-md p-4 overflow-x-auto border border-slate-800"
            data-testid="trace-llm-response"
          >
            <pre className="text-xs text-sky-300 font-mono whitespace-pre-wrap leading-relaxed">
              {trace.llmResponse}
            </pre>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
