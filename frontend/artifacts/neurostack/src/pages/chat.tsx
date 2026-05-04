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

const MOCK_CONVERSATIONS = [
  {
    id: "mock-conv-1",
    label: "Consulta de Preço — Botox",
    messages: [
      { id: "m1", role: "user" as const, content: "Olá, boa tarde! Quanto custa a aplicação de botox na testa?" },
      { id: "m2", role: "assistant" as const, content: "Olá! Que bom que você se interessou pelo nosso tratamento de toxina botulínica! 😊\n\nAntes de falar sobre valores, deixa eu compartilhar: aqui na clínica, o procedimento é realizado pela Dra. Ana Lima, dermatologista com mais de 12 anos de experiência e mais de 500 aplicações bem-sucedidas.\n\nO investimento para a região da testa é a partir de R$ 1.200, com avaliação personalizada inclusa. Posso agendar uma consulta de avaliação para você?" },
      { id: "m3", role: "user" as const, content: "Parece bom! Quais horários tem disponível?" },
      { id: "m4", role: "assistant" as const, content: "Fico feliz com o seu interesse! 😊\n\nTemos disponibilidade esta semana:\n• Quarta-feira: 14h ou 16h\n• Quinta-feira: 10h ou 15h\n• Sexta-feira: 9h ou 11h\n\nQual horário funciona melhor para você? A consulta de avaliação é cortesia e dura cerca de 30 minutos." },
    ],
  },
  {
    id: "mock-conv-2",
    label: "Pós-Procedimento — Dúvida",
    messages: [
      { id: "m5", role: "user" as const, content: "Fiz um preenchimento labial ontem e estou com um inchaço. É normal?" },
      { id: "m6", role: "assistant" as const, content: "Entendo sua preocupação! O inchaço nas primeiras 48-72 horas após o preenchimento labial é completamente normal e esperado. 😊\n\nAlgumas orientações importantes:\n• Aplique compressas frias por 10 minutos a cada hora\n• Evite exercícios físicos intensos por 24-48h\n• Não manipule a região com as mãos\n• Beba bastante água\n\nO resultado final aparece em 7-14 dias, quando o inchaço reduz completamente. Se notar assimetria acentuada, dor intensa ou manchas roxas que aumentam, entre em contato conosco imediatamente." },
      { id: "m7", role: "user" as const, content: "Ok, obrigada! E posso usar maquiagem?" },
      { id: "m8", role: "assistant" as const, content: "De nada! 😊 Sobre maquiagem: o ideal é aguardar pelo menos 12 horas após o procedimento para aplicar qualquer produto na região.\n\nDepois desse período, pode usar maquiagem normalmente, mas prefira produtos hipoalergênicos e evite pressionar muito a área durante a aplicação.\n\nSe tiver mais alguma dúvida sobre os cuidados pós-procedimento, estou aqui para ajudar!" },
    ],
  },
  {
    id: "mock-conv-3",
    label: "Agendamento — Limpeza de Pele",
    messages: [
      { id: "m9", role: "user" as const, content: "Oi! Gostaria de agendar uma limpeza de pele profunda." },
      { id: "m10", role: "assistant" as const, content: "Olá! Será um prazer agendar para você! 😊\n\nNossa limpeza de pele profunda é realizada por esteticistas especializadas e inclui: extração, peeling enzimático, máscara calmante e proteção solar.\n\nTemos disponibilidade:\n• Segunda: 14h\n• Terça: 10h ou 16h\n• Quarta: 9h\n\nQual dia e horário prefere?" },
      { id: "m11", role: "user" as const, content: "Terça às 10h está ótimo!" },
      { id: "m12", role: "assistant" as const, content: "Perfeito! Agendei sua limpeza de pele profunda para terça-feira às 10h. ✅\n\nAlgumas orientações para o dia:\n• Venha sem maquiagem, se possível\n• Evite exposição solar intensa 48h antes\n• O procedimento dura cerca de 1h30\n\nEndereço: Rua das Flores, 123 — Sala 45 — Jardim Paulista\n🅿️ Estacionamento conveniado no Shopping ao lado.\n\nTe aguardamos! Se precisar reagendar, é só me avisar." },
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
  const conv = MOCK_CONVERSATIONS[activeConv];

  const handleFeedback = () => {
    toast({ title: "Feedback registrado!", description: "Obrigado pelo feedback. A sugestão será analisada pela equipe." });
  };

  return (
    <div className="flex flex-col lg:flex-row h-[calc(100vh-6rem)] lg:h-[calc(100vh-4rem)] gap-6">
      {/* Chat Panel */}
      <div className="flex-1 flex flex-col min-h-[400px] border border-border rounded-lg bg-card overflow-hidden shadow-sm">
        <div className="bg-slate-50 border-b border-border p-4 shrink-0">
          <div className="flex items-center justify-between mb-3">
            <div>
              <h2 className="font-semibold text-foreground uppercase tracking-wider text-sm flex items-center gap-2">
                <Bot className="h-4 w-4 text-accent" />
                Chat Simulator — Demo
              </h2>
              <p className="text-xs text-muted-foreground mt-1">Conversas estáticas pré-definidas</p>
            </div>
          </div>
          <div className="flex gap-2">
            {MOCK_CONVERSATIONS.map((c, i) => (
              <button
                key={c.id}
                onClick={() => setActiveConv(i)}
                className={`px-3 py-1.5 rounded-md text-xs font-medium transition-all ${
                  activeConv === i
                    ? "bg-accent text-accent-foreground shadow-sm"
                    : "bg-muted text-muted-foreground hover:bg-muted/80"
                }`}
              >
                {c.label}
              </button>
            ))}
          </div>
        </div>

        <ScrollArea className="flex-1 p-4">
          <div className="space-y-5 pb-4">
            {conv.messages.map((msg) => (
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
                    <div className="rounded-lg px-4 py-3 text-sm bg-muted border border-border text-foreground whitespace-pre-line">
                      {msg.content}
                    </div>
                    <div className="mt-1.5 ml-1">
                      <button
                        onClick={handleFeedback}
                        className="inline-flex items-center gap-1.5 text-[11px] font-medium text-muted-foreground hover:text-violet-400 transition-colors"
                      >
                        <Lightbulb className="h-3 w-3" />
                        Corrigir / Enviar Feedback
                      </button>
                    </div>
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
            ))}
          </div>
        </ScrollArea>

        <div className="p-4 border-t border-border bg-card shrink-0">
          <div className="flex gap-2">
            <Input
              placeholder="Modo visualização — envio desativado"
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
        <div className="bg-slate-50 border-b border-border p-4 shrink-0">
          <h2 className="font-semibold text-foreground uppercase tracking-wider text-sm">
            Decision Trace — Real-time
          </h2>
          <p className="text-xs text-muted-foreground mt-1">Pipeline de decisão em tempo real</p>
        </div>
        <div className="flex-1 flex flex-col items-center justify-center text-muted-foreground p-8 space-y-4">
          <div className="w-16 h-16 border-2 border-dashed border-muted-foreground/30 rounded-full flex items-center justify-center">
            <Activity className="h-6 w-6 text-muted-foreground/50" />
          </div>
          <p className="text-sm text-center">Clique em uma mensagem da IA para ver o trace de decisão</p>
        </div>
      </div>
    </div>
  );
}

export default function Chat() {
  const { clinicId: activeClinicId, tenantId, userId, isInvestor, isReadOnly, isBuilder, viewingClinicId } = useAuth();

  const effectiveTenantId = (isBuilder || isInvestor) ? viewingClinicId : tenantId;
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
        },
      }),
    });
  }, [effectiveTenantId, userId]);

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
                // Show arrow on first assistant message for investors
                const isFirstAssistant = isInvestor && msg.role === "assistant" && !messages.slice(0, idx).some(m => m.role === "assistant");
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
                          isReadOnly={isReadOnly || isInvestor}
                          showArrow={isFirstAssistant}
                        />
                        {isInvestor && (
                          <p className="text-[10px] text-muted-foreground/60 italic ml-1 mt-0.5">(Apenas demonstração — feature em breve)</p>
                        )}
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
