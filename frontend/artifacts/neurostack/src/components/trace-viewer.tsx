import type { DecisionTrace, TraceContextItem } from "@workspace/api-client-react";
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
  BookOpen,
  Settings2,
  CircleHelp,
  type LucideIcon,
} from "lucide-react";
import { useState } from "react";
import { useAuth } from "@/context/auth";
import { useI18n } from "@/context/i18n";
import type { TranslationKey } from "@/locales/en";
import { RatingBlock } from "@/components/rating-block";
import { getIntentLabel, getIntentPresentation } from "@/lib/intent-presentation";
import { cn } from "@/lib/utils";

const STAGE_KEYS = [
  "initial",
  "em_qualificacao",
  "qualificado",
  "followup",
  "agendamento_solicitado",
  "agendado",
  "paciente_ativo",
  "pos_atendimento",
  "fidelizacao",
  "inativo",
  "escalation",
] as const;

export function IntentBadge({ intent, className }: { intent: string; className?: string }) {
  const { t } = useI18n();
  const presentation = getIntentPresentation(intent);
  return (
    <Badge
      className={cn(presentation.badgeClassName, className)}
      data-testid={`badge-intent-${intent}`}
    >
      {getIntentLabel(intent, t)}
    </Badge>
  );
}

function getStageLabel(stage: string, t: (k: TranslationKey) => string): string {
  if ((STAGE_KEYS as readonly string[]).includes(stage)) {
    const translated = t(`stage_${stage}` as TranslationKey);
    if (translated) return translated;
  }
  return stage;
}

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

export type ContextSourceKind = "rag" | "config" | "unknown";

export function normalizeContextSource(source: string | null | undefined): ContextSourceKind {
  switch (source?.trim().toLowerCase()) {
    case "rag":
      return "rag";
    case "config":
    case "clinic_config":
      return "config";
    default:
      return "unknown";
  }
}

function getContextTitle(
  context: TraceContextItem,
  t: (key: TranslationKey) => string,
): string {
  const title = context.title?.trim();
  if (title) return title;

  const fieldKey = context.field?.trim() || context.key?.trim();
  if (fieldKey) {
    const translationKey = `ctx_field_${fieldKey.replace(/\./g, "_")}` as TranslationKey;
    return t(translationKey) || fieldKey;
  }

  return t("ctx_title_fallback");
}

type KnownContextSource = Exclude<ContextSourceKind, "unknown">;

function ContextSourceCard({
  contexts,
  icon: SourceIcon,
  label,
  source,
  t,
}: {
  contexts: TraceContextItem[];
  icon: LucideIcon;
  label: string;
  source: KnownContextSource;
  t: (key: TranslationKey) => string;
}) {
  return (
    <section
      aria-labelledby={`context-heading-${source}`}
      className="min-w-0 overflow-hidden rounded-md border border-border bg-muted text-sm"
      data-testid={`context-card-${source}`}
    >
      <div
        className="flex items-center gap-2 px-4 py-3 font-semibold text-foreground"
        data-testid={`context-header-${source}`}
      >
        <SourceIcon
          className="h-4 w-4 shrink-0"
          aria-hidden="true"
          data-testid={`context-origin-icon-${source}`}
        />
        <h3 id={`context-heading-${source}`}>{label}</h3>
      </div>
      <div
        className="border-t border-border"
        aria-hidden="true"
        data-testid={`context-divider-${source}`}
      />
      <div className="p-4" data-testid={`context-body-${source}`}>
        {contexts.length > 0 ? (
          <div className="space-y-4" role="list" data-testid={`context-list-${source}`}>
            {contexts.map((context, index) => (
              <div
                key={index}
                className="min-w-0"
                data-source-kind={source}
                data-testid={`context-item-${source}-${index}`}
                role="listitem"
              >
                <div
                  className="break-words text-sm font-semibold text-foreground [overflow-wrap:anywhere]"
                  data-testid={`context-title-${source}-${index}`}
                >
                  {getContextTitle(context, t)}
                </div>
                <div
                  className="mt-2 whitespace-pre-wrap break-words text-foreground [overflow-wrap:anywhere]"
                  data-testid={`context-value-${source}-${index}`}
                >
                  {context.value}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div
            className="text-sm italic text-muted-foreground"
            data-testid={`context-empty-${source}`}
          >
            {t("ctx_empty")}
          </div>
        )}
      </div>
    </section>
  );
}

export function TraceViewer({
  trace,
  clinicTenantId = null,
}: {
  trace: DecisionTrace;
  clinicTenantId?: string | null;
}) {
  const { isSuperAdmin, isInvestor } = useAuth();
  const { t } = useI18n();
  const srm = trace.srmDecision;
  const hasConstraints = srm.constraints && srm.constraints.length > 0;
  const hasFlags =
    srm.gate_authority_before_price || srm.safety_concern || srm.handoff_required;
  const contexts = (trace.selectedContext ?? []).reduce<
    Record<ContextSourceKind, TraceContextItem[]>
  >(
    (grouped, context) => {
      grouped[normalizeContextSource(context.source)].push(context);
      return grouped;
    },
    { rag: [], config: [], unknown: [] },
  );

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
                {getStageLabel(srm.stage, t)}
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
          <div
            className="grid grid-cols-1 gap-3 md:grid-cols-2"
            data-testid="context-grid"
          >
            <ContextSourceCard
              contexts={contexts.rag}
              icon={BookOpen}
              label={t("ctx_origin_rag")}
              source="rag"
              t={t}
            />
            <ContextSourceCard
              contexts={contexts.config}
              icon={Settings2}
              label={t("ctx_origin_config")}
              source="config"
              t={t}
            />
          </div>
          {contexts.unknown.length > 0 && (
            <div
              className="mt-3 rounded-md border border-border bg-slate-50 p-3 text-sm text-slate-700 dark:bg-slate-900/40 dark:text-slate-300"
              data-testid="context-unknown-fallback"
            >
              <div className="mb-2 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
                <CircleHelp
                  className="h-3.5 w-3.5 shrink-0"
                  aria-hidden="true"
                  data-testid="context-origin-icon-unknown"
                />
                <span>{t("ctx_origin_unknown")}</span>
              </div>
              <div className="space-y-3" role="list">
                {contexts.unknown.map((context, index) => (
                  <div
                    key={index}
                    className="min-w-0"
                    data-source-kind="unknown"
                    data-testid={`context-item-unknown-${index}`}
                    role="listitem"
                  >
                    <div
                      className="break-words font-semibold [overflow-wrap:anywhere]"
                      data-testid={`context-title-unknown-${index}`}
                    >
                      {getContextTitle(context, t)}
                    </div>
                    <div
                      className="mt-1 whitespace-pre-wrap break-words [overflow-wrap:anywhere]"
                      data-testid={`context-value-unknown-${index}`}
                    >
                      {context.value}
                    </div>
                  </div>
                ))}
              </div>
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
                    system_prompt.txt, read-only
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
          {!isInvestor && (
            <RatingBlock
              msgId={trace.id}
              sessionId=""
              agentResponse={trace.llmResponse}
              userMessage={trace.userMessage}
              clinicTenantId={clinicTenantId}
            />
          )}
        </CardContent>
      </Card>
    </div>
  );
}
