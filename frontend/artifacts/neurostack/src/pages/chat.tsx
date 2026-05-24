import { useState, useRef, useEffect, useCallback } from "react";
import {
  DecisionTrace,
  SRMDecisionIntent,
  SRMDecisionStage,
  customFetch,
} from "@workspace/api-client-react";
import { useAuth } from "@/context/auth";
import { useI18n } from "@/context/i18n";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  Send, User, Bot, Loader2, Activity, RotateCcw,
  SlidersHorizontal, ShieldAlert, CheckCircle2, Lightbulb, X, ArrowDown,
} from "lucide-react";
import { TraceViewer } from "@/components/trace-viewer";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useToast } from "@/hooks/use-toast";
// ── Investor demo: static mock conversations ──

interface MockTrace {
  intent: string;
  stage: string;
  confidence: number;
  processingMs: number;
  gate?: string;
  context: { title: string; content: string; source: string }[];
  llmResponse: string;
}

interface MockMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  trace?: MockTrace;
}

const MOCK_CONVERSATIONS: { id: string; label: string; messages: MockMessage[] }[] = [
  {
    id: "mock-conv-1",
    label: "Consulta de Preço - Facetas",
    messages: [
      { id: "m1", role: "user", content: "Oi! Tenho interesse em fazer facetas de porcelana. Qual é o valor?" },
      {
        id: "m2", role: "assistant",
        content: "Olá! Que ótima escolha. As facetas de porcelana são o padrão ouro para transformação de sorriso, com resultados naturais e duradouros. Para te oferecer um orçamento preciso, o ideal é uma avaliação personalizada com nosso especialista. Posso agendar para você?",
        trace: {
          intent: "Price Inquiry",
          stage: "authority",
          confidence: 96.0,
          processingMs: 287,
          gate: "Gate: Authority Before Price",
          context: [
            { title: "tempo médio de tratamento facetas", content: "tempo médio de tratamento facetas", source: "RAG" },
            { title: "diferenciais do serviço", content: "diferenciais do serviço", source: "CLINIC_CONFIG" },
          ],
          llmResponse: "Olá! Que ótima escolha. As facetas de porcelana são o padrão ouro para transformação de sorriso, com resultados naturais e duradouros. Para te oferecer um orçamento preciso, o ideal é uma avaliação personalizada com nosso especialista. Posso agendar para você?",
        },
      },
      { id: "m3", role: "user", content: "Mas me dá uma ideia de valor antes. E quanto tempo leva o tratamento?" },
      {
        id: "m4", role: "assistant",
        content: "O investimento varia conforme o número de facetas e o caso clínico, mas costuma ficar entre 6 consultas ao longo de 4 a 6 semanas. Nossa clínica trabalha com porcelana de altíssima qualidade importada, com garantia de 10 anos. Para valores exatos e um plano personalizado, agende sua consulta inicial aqui mesmo, no momento está sem custos!",
        trace: {
          intent: "Price Inquiry",
          stage: "consideration",
          confidence: 94.0,
          processingMs: 312,
          context: [
            { title: "tempo médio de tratamento facetas", content: "tempo médio de tratamento facetas", source: "RAG" },
            { title: "diferenciais do serviço", content: "diferenciais do serviço", source: "CLINIC_CONFIG" },
          ],
          llmResponse: "O investimento varia conforme o número de facetas e o caso clínico, mas costuma ficar entre 6 consultas ao longo de 4 a 6 semanas. Nossa clínica trabalha com porcelana de altíssima qualidade importada, com garantia de 10 anos. Para valores exatos e um plano personalizado, agende sua consulta inicial aqui mesmo, no momento está sem custos!",
        },
      },
    ],
  },
];

// --- Types for cognitive pipeline responses ---

interface CognitiveDecision {
  intent?: string;
  funnel_stage?: string;
  action?: string;
  confidence?: number;
  safety_concern?: boolean;
  handoff_required?: boolean;
  [key: string]: any;
}

interface CognitivePrompt {
  system?: string;
  instructions?: string;
  constraints?: string | string[];
  context_summary?: string;
  suggested_response?: string;
}

interface CognitiveEventResponse {
  event_id: string;
  decision?: CognitiveDecision;
  state_patch?: Record<string, any>;
  prompt?: CognitivePrompt;
  context?: {
    recent_messages?: any[];
    session_summary?: string;
    context_sources?: any[];
    turn_count?: number;
  };
  mode?: string;
  processing_time_ms?: number;
}

interface SessionResponse {
  id?: string;
  session_id?: string;
  [key: string]: any;
}

// --- Message type ---

interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  trace?: DecisionTrace;
  decision?: CognitiveDecision;
}

type FeedbackPhase = "idle" | "input" | "analyzing" | "red" | "green";

// --- FeedbackBlock ---

interface FeedbackBlockProps {
  msg: Message;
  sessionId?: string | null;
  userMessage?: string;
  defaultPhase?: FeedbackPhase;
  defaultText?: string;
  isReadOnly?: boolean;
  showArrow?: boolean;
}

function FeedbackBlock({ msg, sessionId, userMessage = "", defaultPhase = "idle", defaultText = "", isReadOnly = false, showArrow = false }: FeedbackBlockProps) {
  const { toast } = useToast();
  const [phase, setPhase] = useState<FeedbackPhase>(defaultPhase);
  const [text, setText]   = useState(defaultText);

  const openFb  = () => { setPhase("input"); setText(""); };
  const closeFb = () => { setPhase("idle");  setText(""); };

  const analyze = async () => {
    if (!text.trim()) return;
    setPhase("analyzing");
    if (isReadOnly) {
      // Demo mode: simulate analysis without API call
      await new Promise(r => setTimeout(r, 1500));
      setPhase(Math.random() > 0.5 ? "green" : "red");
      return;
    }
    try {
      const res = await customFetch<{ status: string; violations: { rule: string; message: string; category: string }[] }>("/v1/rlhf/validate", {
        method: "POST",
        body: JSON.stringify({
          suggestion: text,
          original_message: userMessage,
          agent_response: msg.content,
        }),
      });
      setPhase(res.status === "aligned" ? "green" : "red");
    } catch {
      toast({ title: "Erro ao validar sugestao", variant: "destructive" });
      setPhase("input");
    }
  };

  const saveFeedback = async (status: "aligned" | "forced") => {
    if (isReadOnly) {
      toast({ title: "Feedback registrado!", description: "Obrigado pelo feedback. (Demonstração)" });
      closeFb();
      return;
    }
    try {
      await customFetch("/v1/rlhf/feedback", {
        method: "POST",
        body: JSON.stringify({
          session_id: sessionId || "",
          user_message: userMessage,
          agent_response: msg.content,
          suggested_response: text,
          status,
        }),
      });
      toast({ title: "Feedback salvo com sucesso." });
    } catch {
      toast({ title: "Erro ao salvar feedback", variant: "destructive" });
    }
    closeFb();
  };

  if (phase === "idle") {
    return (
      <div className="mt-1.5 ml-1 flex flex-col gap-0.5">
        {showArrow && (
          <div className="flex items-center gap-1 text-red-500 text-[11px] font-bold animate-bounce mb-0.5">
            <ArrowDown className="h-3 w-3" />
            <span>clique aqui</span>
          </div>
        )}
        <button
          onClick={openFb}
          className={`inline-flex items-center gap-1.5 text-[11px] font-medium transition-colors ${showArrow ? "text-red-500 hover:text-red-400 font-bold" : "text-muted-foreground hover:text-violet-400"}`}
          data-testid={`btn-open-feedback-${msg.id}`}
        >
          <Lightbulb className="h-3 w-3" />
          Corrigir / Enviar Feedback
        </button>
      </div>
    );
  }

  return (
    <div className="mt-2 rounded-lg border border-border bg-background overflow-hidden shadow-sm">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 bg-slate-50 dark:bg-slate-900/60 border-b border-border">
        <div className="flex items-center gap-1.5">
          <SlidersHorizontal className="h-3.5 w-3.5 text-violet-400" />
          <span className="text-[11px] font-bold uppercase tracking-widest text-violet-400">
            RLHF - Feedback de Abordagem
          </span>
        </div>
        {phase !== "analyzing" && (
          <button onClick={closeFb} className="text-muted-foreground hover:text-foreground">
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      <div className="p-3 space-y-3">

        {/* INPUT + ANALYZING */}
        {(phase === "input" || phase === "analyzing") && (
          <>
            <div>
              <label className="text-[11px] font-semibold text-foreground/70 mb-1.5 block">
                Como o agente deveria ter respondido para alinhar com a metodologia?
              </label>
              <Textarea
                value={text}
                onChange={(e) => setText(e.target.value)}
                placeholder="Digite a abordagem ideal que o agente deveria usar..."
                className="resize-none text-xs min-h-[80px] border-border"
                disabled={phase === "analyzing"}
                data-testid={`textarea-feedback-${msg.id}`}
              />
            </div>
            <div className="flex justify-end">
              {phase === "analyzing" ? (
                <div className="flex items-center gap-2 text-xs text-violet-400 font-medium">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  Validando com a Metodologia SRM NeuroStack...
                </div>
              ) : (
                <Button
                  size="sm"
                  className="text-xs h-7 bg-violet-600 hover:bg-violet-500 text-white gap-1.5"
                  onClick={analyze}
                  disabled={!text.trim()}
                  data-testid={`btn-analyze-${msg.id}`}
                >
                  <SlidersHorizontal className="h-3 w-3" />
                  Analisar Sugestao
                </Button>
              )}
            </div>
          </>
        )}

        {/* RED FLAG */}
        {phase === "red" && (
          <div className="space-y-3">
            <div className="flex items-start gap-2 rounded-md bg-red-500/8 border border-red-500/25 px-3 py-2.5">
              <ShieldAlert className="h-4 w-4 text-red-400 shrink-0 mt-0.5" />
              <div>
                <p className="text-xs font-bold text-red-400 mb-0.5">
                  Alerta: Esta sugestao fere a metodologia SRM NeuroStack.
                </p>
                <p className="text-[11px] text-red-400/70">
                  A abordagem sugerida desvia do Padrao Ouro de conversao e seguranca clinica.
                </p>
              </div>
            </div>
            <div className="bg-muted rounded px-2.5 py-2 text-[11px] text-foreground/60 italic border border-border line-clamp-2">
              "{text}"
            </div>
            <div className="flex justify-end gap-2">
              <Button
                size="sm"
                className="text-xs h-7 bg-violet-600 hover:bg-violet-500 text-white gap-1.5"
                onClick={closeFb}
                data-testid={`btn-keep-srm-${msg.id}`}
              >
                <CheckCircle2 className="h-3 w-3" />
                Manter Padrao SRM NeuroStack
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="text-xs h-7 border-red-500/40 text-red-400 hover:bg-red-500/10 gap-1.5"
                onClick={() => saveFeedback("forced")}
                data-testid={`btn-force-${msg.id}`}
              >
                <ShieldAlert className="h-3 w-3" />
                Aplicar Sugestao Pessoal
              </Button>
            </div>
          </div>
        )}

        {/* GREEN FLAG */}
        {phase === "green" && (
          <div className="space-y-3">
            <div className="flex items-start gap-2 rounded-md bg-green-500/8 border border-green-500/25 px-3 py-2.5">
              <CheckCircle2 className="h-4 w-4 text-green-400 shrink-0 mt-0.5" />
              <div>
                <p className="text-xs font-bold text-green-400 mb-0.5">
                  Sugestao alinhada com a metodologia.
                </p>
                <p className="text-[11px] text-green-400/70">
                  A abordagem proposta esta em conformidade com o Padrao Ouro SRM NeuroStack.
                </p>
              </div>
            </div>
            <div className="bg-muted rounded px-2.5 py-2 text-[11px] text-foreground/70 italic border border-border line-clamp-2">
              "{text}"
            </div>
            <div className="flex justify-end">
              <Button
                size="sm"
                className="text-xs h-7 bg-green-600 hover:bg-green-500 text-white gap-1.5"
                onClick={() => saveFeedback("aligned")}
                data-testid={`btn-apply-green-${msg.id}`}
              >
                <CheckCircle2 className="h-3 w-3" />
                Aplicar Sugestao Pessoal
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// --- Chat ---

// ── Investor Mock Chat ──

function InvestorMockChat() {
  const { toast } = useToast();
  const [activeConv, setActiveConv] = useState(0);
  const [activeTrace, setActiveTrace] = useState<MockTrace | null>(null);
  const conv = MOCK_CONVERSATIONS[activeConv];

  // Auto-select first assistant trace when switching conversations
  useEffect(() => {
    const firstAssistant = conv.messages.find(m => m.role === "assistant" && m.trace);
    setActiveTrace(firstAssistant?.trace ?? null);
  }, [activeConv]);

  return (
    <div className="flex flex-col lg:flex-row h-[calc(100vh-6rem)] lg:h-[calc(100vh-4rem)] gap-6">
      {/* Chat Panel */}
      <div className="flex-1 flex flex-col min-h-[400px] border border-border rounded-lg bg-card overflow-hidden shadow-sm">
        <div className="bg-slate-50 border-b border-border p-4 shrink-0">
          <div className="flex items-center justify-between mb-3">
            <div>
              <h2 className="font-semibold text-foreground uppercase tracking-wider text-sm flex items-center gap-2">
                <Bot className="h-4 w-4 text-accent" />
                SIMULADOR DE CHAT
              </h2>
              <p className="text-xs text-muted-foreground mt-1">Teste o agente de IA clínico. As respostas geram traces em tempo real.</p>
            </div>
          </div>
        </div>

        <ScrollArea className="flex-1 p-4">
          <div className="space-y-5 pb-4">
            {conv.messages.map((msg, idx) => {
              const isFirstAssistant = msg.role === "assistant" && !conv.messages.slice(0, idx).some(m => m.role === "assistant");
              const prevUserMsg = msg.role === "assistant" && idx > 0 && conv.messages[idx - 1].role === "user"
                ? conv.messages[idx - 1].content : "";
              return (
                <div
                  key={msg.id}
                  className={`flex gap-3 ${msg.role === "user" ? "justify-end" : "justify-start"}`}
                >
                  {msg.role === "assistant" && (
                    <div className="h-8 w-8 rounded-full bg-accent/10 flex items-center justify-center shrink-0 mt-0.5">
                      <Bot className="h-4 w-4 text-accent" />
                    </div>
                  )}
                  {msg.role === "assistant" ? (
                    <div className="max-w-[82%] flex flex-col">
                      <div
                        className={`rounded-lg px-4 py-3 text-sm bg-muted border text-foreground whitespace-pre-line cursor-pointer transition-colors ${
                          activeTrace === msg.trace ? "border-accent" : "border-border hover:border-accent/50"
                        }`}
                        onClick={() => msg.trace && setActiveTrace(msg.trace)}
                      >
                        {msg.content}
                      </div>
                      <FeedbackBlock
                        msg={msg as Message}
                        sessionId={null}
                        userMessage={prevUserMsg}
                        defaultPhase="idle"
                        defaultText=""
                        isReadOnly={true}
                        showArrow={isFirstAssistant}
                      />
                      <p className="text-[10px] text-muted-foreground/60 italic ml-1 mt-0.5">(Apenas demonstração, feature em breve)</p>
                    </div>
                  ) : (
                    <div className="max-w-[80%] rounded-lg px-4 py-3 text-sm bg-primary text-primary-foreground">
                      {msg.content}
                    </div>
                  )}
                  {msg.role === "user" && (
                    <div className="h-8 w-8 rounded-full bg-primary/10 flex items-center justify-center shrink-0 mt-0.5">
                      <User className="h-4 w-4 text-primary" />
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </ScrollArea>

        <div className="p-4 border-t border-border bg-card shrink-0">
          <div className="flex gap-2">
            <Input
              placeholder="Modo visualização, envio desativado"
              className="flex-1"
              disabled
            />
            <Button disabled>
              <Send className="h-4 w-4" />
            </Button>
          </div>
        </div>
      </div>

      {/* Trace Viewer Panel */}
      <div className="flex-[1.2] flex flex-col min-h-[400px] border border-border rounded-lg bg-card overflow-hidden shadow-sm">
        <div className="bg-slate-50 border-b border-border p-4 shrink-0 flex justify-between items-center">
          <div>
            <h2 className="font-semibold text-foreground uppercase tracking-wider text-sm">
              TRACE DE DECISÃO AO VIVO
            </h2>
            <p className="text-xs text-muted-foreground mt-1">Visão diagnóstica do último ciclo de raciocínio.</p>
          </div>
          <div className="text-xs font-mono text-muted-foreground bg-muted px-2 py-1 rounded">
            ID: demo-001...
          </div>
        </div>
        <ScrollArea className="flex-1">
          <div className="p-4 space-y-4">
            {activeTrace ? (
              <>
                {/* SRM Decision */}
                <div className="border border-border rounded-lg p-5 space-y-4">
                  <h3 className="font-semibold text-foreground text-sm flex items-center gap-2">
                    <Activity className="h-4 w-4" />
                    SRM DECISION
                  </h3>
                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <p className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider mb-1">Intent</p>
                      <span className="inline-block bg-blue-600 text-white text-xs font-medium px-3 py-1 rounded-md">
                        {activeTrace.intent}
                      </span>
                    </div>
                    <div>
                      <p className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider mb-1">Stage</p>
                      <p className="text-sm text-foreground">{activeTrace.stage}</p>
                    </div>
                    <div>
                      <p className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider mb-1">Confidence</p>
                      <p className="text-sm text-foreground font-medium">{activeTrace.confidence.toFixed(1)}%</p>
                    </div>
                    <div>
                      <p className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider mb-1">Processing</p>
                      <p className="text-sm text-foreground font-medium">{activeTrace.processingMs}ms</p>
                    </div>
                  </div>
                  {activeTrace.gate && (
                    <div className="pt-1">
                      <span className="inline-flex items-center gap-1.5 text-xs font-medium text-blue-600 bg-blue-50 border border-blue-200 rounded-full px-3 py-1">
                        🔑 {activeTrace.gate}
                      </span>
                    </div>
                  )}
                </div>

                {/* Selected Context */}
                <div className="border border-border rounded-lg p-5 space-y-3">
                  <h3 className="font-semibold text-foreground text-sm uppercase tracking-wider">
                    SELECTED CONTEXT
                  </h3>
                  <div className="space-y-3">
                    {activeTrace.context.map((ctx, i) => (
                      <div key={i} className="bg-slate-50 rounded-lg p-4 border border-border">
                        <div className="flex items-center justify-between mb-1">
                          <p className="text-sm font-medium text-blue-500">{ctx.title}</p>
                          <span className="text-[10px] font-mono text-muted-foreground bg-muted px-2 py-0.5 rounded border border-border">
                            {ctx.source}
                          </span>
                        </div>
                        <p className="text-sm text-foreground">{ctx.content}</p>
                      </div>
                    ))}
                  </div>
                </div>

                {/* LLM Response */}
                <div className="border border-border rounded-lg p-5 space-y-3">
                  <h3 className="font-semibold text-foreground text-sm uppercase tracking-wider">
                    LLM RESPONSE
                  </h3>
                  <div className="bg-slate-900 text-slate-100 rounded-lg p-4 text-sm whitespace-pre-line font-mono leading-relaxed">
                    {activeTrace.llmResponse}
                  </div>
                </div>
              </>
            ) : (
              <div className="h-full flex flex-col items-center justify-center text-muted-foreground pt-24 space-y-4">
                <div className="w-16 h-16 border-2 border-dashed border-muted-foreground/30 rounded-full flex items-center justify-center">
                  <Activity className="h-6 w-6 text-muted-foreground/50" />
                </div>
                <p className="text-sm text-center">Clique em uma mensagem da IA para ver o trace de decisão</p>
              </div>
            )}
          </div>
        </ScrollArea>
      </div>
    </div>
  );
}

export default function Chat() {
  const { clinicId: activeClinicId, tenantId, userId, isInvestor, isReadOnly, isBuilder, isSuperAdmin, viewingClinicId } = useAuth();

  if (isInvestor) return <InvestorMockChat />;
  const effectiveTenantId = (isBuilder || isSuperAdmin) ? viewingClinicId : tenantId;
  const { t, locale } = useI18n();
  const { toast } = useToast();

  const [messages, setMessages]       = useState<Message[]>([]);
  const [input, setInput]             = useState("");
  const [isPending, setIsPending]     = useState(false);
  const [sessionId, setSessionId]     = useState<string | null>(null);
  const [activeTrace, setActiveTrace] = useState<DecisionTrace | null>(null);

  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setMessages([]);
    setActiveTrace(null);
    setSessionId(null);
  }, [activeClinicId, effectiveTenantId]);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [messages, isPending]);

  // Create a new session via POST /v1/sessions
  const createSession = useCallback(async (): Promise<string> => {
    const resp = await customFetch<SessionResponse>("/v1/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tenant_id: effectiveTenantId,
        channel: "web",
        user_id: userId,
      }),
    });
    // Backend returns "id", not "session_id"
    return resp.session_id ?? resp.id;
  }, [effectiveTenantId, userId]);

  // Send a message via POST /v1/events (cognitive pipeline)
  const sendEvent = useCallback(async (content: string, currentSessionId: string): Promise<CognitiveEventResponse> => {
    return customFetch<CognitiveEventResponse>("/v1/events", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tenant_id: effectiveTenantId,
        session_id: currentSessionId,
        event_type: "message.in",
        payload: {
          content,
          channel: "web",
          user_id: userId,
          language: locale === "en" ? "en" : "pt-BR",
        },
      }),
    });
  }, [effectiveTenantId, userId, locale]);

  // Map cognitive pipeline response to a trace-like structure for the TraceViewer
  const mapToTrace = (resp: CognitiveEventResponse, userMessage: string): DecisionTrace => {
    // Extract constraints from prompt (backend sends list[str])
    const rawConstraints = resp.prompt?.constraints;
    const constraints: string[] = Array.isArray(rawConstraints)
      ? rawConstraints
      : typeof rawConstraints === "string" && rawConstraints.trim()
        ? rawConstraints.split(",").map((s) => s.trim())
        : [];

    // Derive gate_authority_before_price from state_patch or constraints
    const gateAuthority =
      resp.state_patch?.authority_done === false ||
      constraints.some((c) => c.includes("AUTHORITY_BEFORE_PRICE"));

    return {
      id: resp.event_id ?? crypto.randomUUID(),
      timestamp: new Date().toISOString(),
      userMessage,
      srmDecision: {
        intent: (resp.decision?.intent ?? "unknown") as SRMDecisionIntent,
        stage: (resp.decision?.funnel_stage ?? "initial") as SRMDecisionStage,
        confidence: resp.decision?.confidence ?? 0,
        action: resp.decision?.action ?? "respond",
        constraints,
        gate_authority_before_price: gateAuthority,
        safety_concern: resp.decision?.safety_concern ?? false,
        handoff_required: resp.decision?.handoff_required ?? false,
      },
      selectedContext: resp.context?.context_sources?.map((src: any) => ({
        key: src.field ?? "context",
        value: src.content ?? "",
        source: src.origin ?? "config",
        title: src.title ?? "",
        field: src.field ?? "",
      })) ?? [],
      promptBuilt: resp.prompt?.system ?? "",
      llmResponse: resp.prompt?.suggested_response ?? "",
      processingTimeMs: resp.processing_time_ms ?? 0,
      eventData: resp.decision as Record<string, any>,
      eventMetadata: { mode: resp.mode, turn_count: resp.context?.turn_count },
      fullContext: resp.context as Record<string, any>,
      fullPrompt: resp.prompt as Record<string, any>,
    };
  };

  const handleSend = async () => {
    if (!input.trim() || isPending) return;

    if (isReadOnly) {
      toast({
        title: "Modo Visualizacao: Alteracoes nao permitidas nesta conta de teste.",
        description: "Esta e uma demonstracao, nenhum envio foi realizado.",
      });
      setInput("");
      return;
    }

    // Guard: builder/super-admin must have a clinic selected
    if ((isBuilder || isSuperAdmin) && !effectiveTenantId) {
      toast({
        title: t("chat_no_clinic_selected_title") ?? "Nenhuma clínica selecionada",
        description: t("chat_no_clinic_selected_desc") ?? "Selecione uma clínica no menu lateral antes de usar o simulador.",
        variant: "destructive",
      });
      return;
    }

    const userMsg: Message = { id: Date.now().toString(), role: "user", content: input };
    setMessages((prev) => [...prev, userMsg]);
    const messageText = input;
    setInput("");
    setIsPending(true);

    try {
      // Create session on first message
      let currentSessionId = sessionId;
      if (!currentSessionId) {
        currentSessionId = await createSession();
        setSessionId(currentSessionId);
      }

      // Send message through cognitive pipeline
      const resp = await sendEvent(messageText, currentSessionId);

      const assistantContent =
        resp.prompt?.suggested_response ??
        "No response generated.";

      const trace = mapToTrace(resp, messageText);

      const botMsg: Message = {
        id: (Date.now() + 1).toString(),
        role: "assistant",
        content: assistantContent,
        trace,
        decision: resp.decision,
      };
      setMessages((prev) => [...prev, botMsg]);
      setActiveTrace(trace);
    } catch (err: any) {
      const errorMsg: Message = {
        id: (Date.now() + 1).toString(),
        role: "assistant",
        content: `Error: ${err?.message ?? "Failed to process message. Please try again."}`,
      };
      setMessages((prev) => [...prev, errorMsg]);
    } finally {
      setIsPending(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); }
  };

  return (
    <div className="flex flex-col lg:flex-row h-[calc(100vh-6rem)] lg:h-[calc(100vh-4rem)] gap-6">

      {/* -- Chat Panel -- */}
      <div className="flex-1 flex flex-col min-h-[400px] border border-border rounded-lg bg-card overflow-hidden shadow-sm">
        <div className="bg-slate-50 border-b border-border p-4 shrink-0 flex items-center justify-between">
          <div>
            <h2 className="font-semibold text-foreground uppercase tracking-wider text-sm flex items-center gap-2">
              <Bot className="h-4 w-4 text-accent" />
              {t("chat_title")}
            </h2>
            <p className="text-xs text-muted-foreground mt-1">{t("chat_subtitle")}</p>
          </div>
          {sessionId && (
            <Button
              variant="outline"
              size="sm"
              className="text-xs gap-1.5"
              onClick={() => { setSessionId(null); setMessages([]); setActiveTrace(null); }}
            >
              <RotateCcw className="h-3 w-3" />
              Nova Conversa
            </Button>
          )}
        </div>

        <ScrollArea className="flex-1 p-4" ref={scrollRef}>
          <div className="space-y-5 pb-4">
            {messages.length === 0 ? (
              <div className="h-full flex flex-col items-center justify-center text-muted-foreground pt-12">
                <Bot className="h-12 w-12 text-muted-foreground/30 mb-4" />
                <p className="text-sm">{t("chat_start")}</p>
              </div>
            ) : (
              messages.map((msg, idx) => {
                // For the demo last message, pre-open RLHF block with seed text
                const isDemoPreOpened = false;
                // Find the user message this assistant response is replying to
                const prevUserMsg = msg.role === "assistant" && idx > 0 && messages[idx - 1].role === "user"
                  ? messages[idx - 1].content : "";

                return (
                  <div
                    key={msg.id}
                    className={`flex gap-3 ${msg.role === "user" ? "justify-end" : "justify-start"}`}
                    data-testid={`message-${msg.role}-${msg.id}`}
                  >
                    {msg.role === "assistant" && (
                      <div className="h-8 w-8 rounded-full bg-accent/10 flex items-center justify-center shrink-0 mt-0.5">
                        <Bot className="h-4 w-4 text-accent" />
                      </div>
                    )}

                    {msg.role === "assistant" ? (
                      <div className="max-w-[82%] flex flex-col">
                        <div
                          className="rounded-lg px-4 py-3 text-sm bg-muted border border-border text-foreground cursor-pointer hover:border-accent transition-colors"
                          onClick={() => { if (msg.trace) setActiveTrace(msg.trace); }}
                        >
                          {msg.content}
                        </div>
                        <FeedbackBlock
                          msg={msg}
                          sessionId={sessionId}
                          userMessage={prevUserMsg}
                          defaultPhase={isDemoPreOpened ? "input" : "idle"}
                          defaultText=""
                          isReadOnly={isReadOnly}
                          showArrow={false}
                        />
                      </div>
                    ) : (
                      <div className="max-w-[80%] rounded-lg px-4 py-3 text-sm bg-primary text-primary-foreground">
                        {msg.content}
                      </div>
                    )}

                    {msg.role === "user" && (
                      <div className="h-8 w-8 rounded-full bg-primary/10 flex items-center justify-center shrink-0 mt-0.5">
                        <User className="h-4 w-4 text-primary" />
                      </div>
                    )}
                  </div>
                );
              })
            )}

            {isPending && (
              <div className="flex gap-3 justify-start">
                <div className="h-8 w-8 rounded-full bg-accent/10 flex items-center justify-center shrink-0">
                  <Bot className="h-4 w-4 text-accent" />
                </div>
                <div className="max-w-[80%] rounded-lg px-4 py-3 bg-muted border border-border flex items-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin text-accent" />
                  Processing through NeuroStack...
                </div>
              </div>
            )}
          </div>
        </ScrollArea>

        <div className="p-4 border-t border-border bg-card shrink-0">
          <div className="flex gap-2">
            <Input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={isReadOnly ? "Modo visualizacao, envio desativado" : t("chat_placeholder")}
              className="flex-1"
              disabled={isPending}
              data-testid="input-chat"
            />
            <Button
              onClick={handleSend}
              disabled={!input.trim() || isPending}
              data-testid="button-send-chat"
            >
              <Send className="h-4 w-4" />
              <span className="sr-only">Send</span>
            </Button>
          </div>
        </div>
      </div>

      {/* -- Trace Viewer Panel -- */}
      <div className="flex-[1.2] flex flex-col min-h-[400px] border border-border rounded-lg bg-card overflow-hidden shadow-sm">
        <div className="bg-slate-50 border-b border-border p-4 shrink-0 flex justify-between items-center">
          <div>
            <h2 className="font-semibold text-foreground uppercase tracking-wider text-sm">
              {t("chat_trace_title")}
            </h2>
            <p className="text-xs text-muted-foreground mt-1">{t("chat_trace_subtitle")}</p>
          </div>
          {activeTrace && (
            <div className="text-xs font-mono text-muted-foreground bg-muted px-2 py-1 rounded">
              ID: {activeTrace.id.slice(0, 8)}...
            </div>
          )}
        </div>
        <ScrollArea className="flex-1">
          <div className="p-4">
            {activeTrace ? (
              <TraceViewer trace={activeTrace} />
            ) : (
              <div className="h-full flex flex-col items-center justify-center text-muted-foreground pt-24 space-y-4">
                <div className="w-16 h-16 border-2 border-dashed border-muted-foreground/30 rounded-full flex items-center justify-center">
                  <Activity className="h-6 w-6 text-muted-foreground/50" />
                </div>
                <p className="text-sm max-w-[250px] text-center">{t("chat_waiting")}</p>
              </div>
            )}
          </div>
        </ScrollArea>
      </div>
    </div>
  );
}
