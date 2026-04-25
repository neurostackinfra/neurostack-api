import { useState, useRef, useEffect, useCallback } from "react";
import {
  DecisionTrace,
  customFetch,
} from "@workspace/api-client-react";
import { useAuth } from "@/context/auth";
import { useI18n } from "@/context/i18n";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  Send, User, Bot, Loader2, Activity,
  SlidersHorizontal, ShieldAlert, CheckCircle2, Lightbulb, X, ArrowDown,
} from "lucide-react";
import { TraceViewer } from "@/components/trace-viewer";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useToast } from "@/hooks/use-toast";
// Demo data removed — chat now works against real backend only

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
  constraints?: string;
  context_summary?: string;
  suggested_response?: string;
}

interface CognitiveEventResponse {
  event_id: string;
  decision?: CognitiveDecision;
  prompt?: CognitivePrompt;
  context?: {
    recent_messages?: any[];
    session_summary?: string;
    rag_sources?: any[];
    turn_count?: number;
  };
  mode?: string;
  processing_time_ms?: number;
}

interface SessionResponse {
  session_id: string;
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

const RED_KEYWORDS = ["diagnostico", "remedio", "urgencia", "preco", "diagnose", "medicine"];

function runGuardrail(text: string): boolean {
  return RED_KEYWORDS.some((kw) => text.toLowerCase().includes(kw));
}

// --- FeedbackBlock ---

interface FeedbackBlockProps {
  msg: Message;
  defaultPhase?: FeedbackPhase;
  defaultText?: string;
  isReadOnly?: boolean;
  showArrow?: boolean;
}

function FeedbackBlock({ msg, defaultPhase = "idle", defaultText = "", isReadOnly = false, showArrow = false }: FeedbackBlockProps) {
  const { toast } = useToast();
  const [phase, setPhase] = useState<FeedbackPhase>(defaultPhase);
  const [text, setText]   = useState(defaultText);

  const openFb  = () => { setPhase("input"); setText(""); };
  const closeFb = () => { setPhase("idle");  setText(""); };

  const analyze = () => {
    if (!text.trim()) return;
    setPhase("analyzing");
    setTimeout(() => setPhase(runGuardrail(text) ? "red" : "green"), 1200);
  };

  const finishDemo = () => {
    closeFb();
    if (isReadOnly) {
      toast({
        title: "Modo Visualizacao: Alteracoes nao permitidas nesta conta de teste.",
        description: "Esta e uma demonstracao — nenhum dado foi salvo.",
      });
    } else {
      toast({
        title: "[Demonstracao] Fluxo de feedback testado com sucesso.",
        description: "Nenhum dado foi salvo.",
      });
    }
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
        <span className="text-[10px] text-gray-400 italic">
          (Apenas demonstracao — feature em breve)
        </span>
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
            RLHF — Feedback de Abordagem
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
                onClick={finishDemo}
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
                onClick={finishDemo}
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

export default function Chat() {
  const { clinicId: activeClinicId, tenantId, userId, isInvestor, isReadOnly } = useAuth();
  const { t } = useI18n();
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
  }, [activeClinicId]);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [messages, isPending]);

  // Create a new session via POST /v1/sessions
  const createSession = useCallback(async (): Promise<string> => {
    const resp = await customFetch<SessionResponse>("/v1/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tenant_id: tenantId,
        channel: "web",
        user_id: userId,
      }),
    });
    return resp.session_id;
  }, [tenantId, userId]);

  // Send a message via POST /v1/events (cognitive pipeline)
  const sendEvent = useCallback(async (content: string, currentSessionId: string): Promise<CognitiveEventResponse> => {
    return customFetch<CognitiveEventResponse>("/v1/events", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tenant_id: tenantId,
        session_id: currentSessionId,
        event_type: "message.in",
        payload: {
          content,
          channel: "web",
          user_id: userId,
        },
      }),
    });
  }, [tenantId, userId]);

  // Map cognitive pipeline response to a trace-like structure for the TraceViewer
  const mapToTrace = (resp: CognitiveEventResponse, userMessage: string): DecisionTrace => {
    return {
      id: resp.event_id ?? crypto.randomUUID(),
      timestamp: new Date().toISOString(),
      userMessage,
      srmDecision: {
        intent: resp.decision?.intent ?? "unknown",
        funnelStage: resp.decision?.funnel_stage ?? "unknown",
        confidence: resp.decision?.confidence ?? 0,
        action: resp.decision?.action ?? "respond",
        safetyConcern: resp.decision?.safety_concern ?? false,
        handoffRequired: resp.decision?.handoff_required ?? false,
      } as any,
      selectedContext: resp.context?.rag_sources?.map((src: any) => ({
        source: src.source ?? "rag",
        content: src.content ?? "",
        score: src.score ?? 0,
      })) ?? [],
      promptBuilt: resp.prompt?.system ?? "",
      llmResponse: resp.prompt?.suggested_response ?? "",
      processingTimeMs: resp.processing_time_ms ?? 0,
    };
  };

  const handleSend = async () => {
    if (!input.trim() || isPending) return;

    if (isReadOnly) {
      toast({
        title: "Modo Visualizacao: Alteracoes nao permitidas nesta conta de teste.",
        description: "Esta e uma demonstracao — nenhum envio foi realizado.",
      });
      setInput("");
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
        <div className="bg-slate-50 border-b border-border p-4 shrink-0">
          <h2 className="font-semibold text-foreground uppercase tracking-wider text-sm flex items-center gap-2">
            <Bot className="h-4 w-4 text-accent" />
            {t("chat_title")}
          </h2>
          <p className="text-xs text-muted-foreground mt-1">{t("chat_subtitle")}</p>
        </div>

        <ScrollArea className="flex-1 p-4" ref={scrollRef}>
          <div className="space-y-5 pb-4">
            {messages.length === 0 ? (
              <div className="h-full flex flex-col items-center justify-center text-muted-foreground pt-12">
                <Bot className="h-12 w-12 text-muted-foreground/30 mb-4" />
                <p className="text-sm">{t("chat_start")}</p>
              </div>
            ) : (
              messages.map((msg) => {
                // For the demo last message, pre-open RLHF block with seed text
                const isDemoPreOpened = false;

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
              placeholder={isReadOnly ? "Modo visualizacao — envio desativado" : t("chat_placeholder")}
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
