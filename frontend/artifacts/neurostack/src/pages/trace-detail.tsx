import { useState, useEffect } from "react";
import { useParams, Link } from "wouter";
import { TraceViewer } from "@/components/trace-viewer";
import { Button } from "@/components/ui/button";
import { ArrowLeft, Clock, MessageSquare } from "lucide-react";
import { format } from "date-fns";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/context/auth";
import { useI18n } from "@/context/i18n";

function getToken(): string | null {
  try {
    const raw = localStorage.getItem("ns_auth");
    if (!raw) return null;
    return (JSON.parse(raw) as { token?: string }).token ?? null;
  } catch {
    return null;
  }
}

/** Map API snake_case response to the camelCase shape TraceViewer expects */
function mapTrace(data: any) {
  const srm = data.srm_decision ?? {};
  return {
    id: data.id,
    timestamp: data.timestamp,
    userMessage: data.user_message ?? data.userMessage ?? "",
    srmDecision: {
      intent: srm.intent ?? "",
      stage: srm.funnel_stage ?? srm.intent_category ?? srm.stage ?? "unknown",
      action: srm.action ?? "",
      confidence: srm.confidence ?? 0,
      safety_concern: srm.safety_concern ?? false,
      handoff_required: srm.handoff_required ?? false,
      constraints: srm.constraints ?? [],
      gate_authority_before_price: srm.gate_authority_before_price ?? (srm.intent === "pedido_preco"),
    },
    selectedContext: (data.selected_context ?? data.selectedContext ?? []).map((c: any) => ({
      key: c.field ?? c.key ?? "",
      value: c.content ?? c.value ?? "",
      source: c.origin ?? c.source ?? "",
      title: c.title ?? "",
      field: c.field ?? c.key ?? "",
    })),
    promptBuilt: data.prompt_built ?? data.promptBuilt ?? "",
    llmResponse: data.llm_response ?? data.llmResponse ?? data.full_prompt?.suggested_response ?? "",
    processingTimeMs: data.processing_time_ms ?? data.processingTimeMs ?? 0,
    eventData: data.event_data ?? data.eventData ?? undefined,
    eventMetadata: data.event_metadata ?? data.eventMetadata ?? undefined,
    fullContext: data.full_context ?? data.fullContext ?? undefined,
    fullPrompt: data.full_prompt ?? data.fullPrompt ?? undefined,
  };
}

// ── Investor mock trace data (matches MOCK_TRACES in traces.tsx) ──
const INVESTOR_MOCK_TRACES: Record<string, any> = {
  t1: { id: "t1", timestamp: "2026-05-03T14:32:00Z", user_message: "Quanto custa a aplicação de botox na testa?", srm_decision: { intent: "pedido_preco", funnel_stage: "qualification", action: "respond", confidence: 0.94, safety_concern: false, handoff_required: false, constraints: ["AUTHORITY_BEFORE_PRICE"], gate_authority_before_price: true }, selected_context: [{ title: "tabela de preços botox", field: "tabela de preços botox", content: "Botox testa: a partir de R$ 1.200", origin: "CLINIC_CONFIG" }, { title: "diferenciais do serviço", field: "diferenciais do serviço", content: "Dra. Ana Lima — 12 anos de experiência, +500 aplicações", origin: "CLINIC_CONFIG" }], llm_response: "Olá! Que bom que você se interessou pelo nosso tratamento de toxina botulínica!\n\nAntes de falar sobre valores, deixa eu compartilhar: aqui na clínica, o procedimento é realizado pela Dra. Ana Lima, dermatologista com mais de 12 anos de experiência e mais de 500 aplicações bem-sucedidas.\n\nO investimento para a região da testa é a partir de R$ 1.200, com avaliação personalizada inclusa. Posso agendar uma consulta de avaliação para você?", processing_time_ms: 823 },
  t2: { id: "t2", timestamp: "2026-05-03T14:15:00Z", user_message: "Gostaria de agendar uma limpeza de pele", srm_decision: { intent: "agendar", funnel_stage: "scheduling", action: "respond", confidence: 0.97, safety_concern: false, handoff_required: false }, selected_context: [{ title: "serviços disponíveis", field: "serviços disponíveis", content: "Limpeza de pele profunda", origin: "CLINIC_CONFIG" }], llm_response: "Olá! Será um prazer agendar para você!\n\nNossa limpeza de pele profunda é realizada por esteticistas especializadas e inclui: extração, peeling enzimático, máscara calmante e proteção solar.\n\nQual dia e horário ficam melhores para você?", processing_time_ms: 654 },
  t3: { id: "t3", timestamp: "2026-05-03T13:45:00Z", user_message: "Quero saber sobre tratamento para manchas", srm_decision: { intent: "interesse_tratamento", funnel_stage: "discovery", action: "respond", confidence: 0.91, safety_concern: false, handoff_required: false }, selected_context: [{ title: "tratamentos para manchas", field: "tratamentos para manchas", content: "Peeling químico, laser, microagulhamento", origin: "RAG" }], llm_response: "Entendo! Existem diversas opções de tratamento para manchas, como peeling químico, laser e microagulhamento. O tratamento ideal depende do tipo e profundidade da mancha.\n\nVocê já fez algum tratamento para manchas antes ou seria a primeira vez?", processing_time_ms: 912 },
  t4: { id: "t4", timestamp: "2026-05-03T12:22:00Z", user_message: "Fiz um preenchimento e estou com inchaço intenso, está muito roxo", srm_decision: { intent: "pos_atendimento_sintomas_preocupantes", funnel_stage: "post_care", action: "respond", confidence: 0.98, safety_concern: true, handoff_required: false }, selected_context: [{ title: "protocolo pós-procedimento", field: "protocolo pós-procedimento", content: "Sinais de alerta: inchaço excessivo, hematomas extensos", origin: "RAG" }], llm_response: "Entendo sua preocupação. Para sua segurança, vou encaminhar seu caso imediatamente para nossa equipe médica avaliar a situação.", processing_time_ms: 1102 },
  t5: { id: "t5", timestamp: "2026-05-03T11:50:00Z", user_message: "Preciso cancelar minha consulta de amanhã", srm_decision: { intent: "cancelar", funnel_stage: "scheduling", action: "respond", confidence: 0.96, safety_concern: false, handoff_required: false }, selected_context: [], llm_response: "Entendo! Posso te ajudar com isso. Poderia me informar o motivo do cancelamento? Talvez possamos remarcar para outro horário que seja mais conveniente para você.", processing_time_ms: 589 },
  t6: { id: "t6", timestamp: "2026-05-03T10:30:00Z", user_message: "O atendimento foi péssimo, quero falar com o gerente", srm_decision: { intent: "reclamacao", funnel_stage: "escalation", action: "handoff", confidence: 0.99, safety_concern: false, handoff_required: true }, selected_context: [], llm_response: "Sinto muito por essa experiência. Me conta o que aconteceu para eu poder encaminhar da melhor forma. Vou registrar e encaminhar para nossa equipe resolver isso.", processing_time_ms: 734 },
  t7: { id: "t7", timestamp: "2026-05-03T09:12:00Z", user_message: "Vocês fazem harmonização facial?", srm_decision: { intent: "interesse_tratamento", funnel_stage: "discovery", action: "respond", confidence: 0.88, safety_concern: false, handoff_required: false }, selected_context: [{ title: "harmonização facial", field: "harmonização facial", content: "Procedimento completo com análise facial personalizada", origin: "RAG" }], llm_response: "Sim, realizamos harmonização facial! É um procedimento completo que combina diferentes técnicas para equilibrar e valorizar os traços do rosto.\n\nVocê já fez algum procedimento estético antes ou seria a primeira vez?", processing_time_ms: 876 },
  t8: { id: "t8", timestamp: "2026-05-02T16:45:00Z", user_message: "Queria remarcar minha consulta para semana que vem", srm_decision: { intent: "reagendar", funnel_stage: "scheduling", action: "respond", confidence: 0.95, safety_concern: false, handoff_required: false }, selected_context: [], llm_response: "Claro! Qual dia e horário da semana que vem ficam melhores para você?", processing_time_ms: 701 },
};

export default function TraceDetail() {
  const params = useParams();
  const id = params.id as string;
  const { t } = useI18n();
  const { isBuilder, isInvestor, viewingClinicId } = useAuth();

  const [trace, setTrace] = useState<ReturnType<typeof mapTrace> | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;

    // Investor: use mock data instead of API
    if (isInvestor && INVESTOR_MOCK_TRACES[id]) {
      setTrace(mapTrace(INVESTOR_MOCK_TRACES[id]));
      setLoading(false);
      return;
    }

    async function fetchTrace() {
      setLoading(true);
      setLoadError(null);
      try {
        const token = getToken();
        const tenantParam = isBuilder && viewingClinicId ? `?tenant_id=${viewingClinicId}` : "";
        const res = await fetch(`/v1/traces/${id}${tenantParam}`, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (!res.ok) throw new Error(`Fetch failed: ${res.status}`);
        const data = await res.json();
        if (!cancelled) setTrace(mapTrace(data));
      } catch (err) {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : "Unknown error");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    void fetchTrace();
    return () => { cancelled = true; };
  }, [id, isInvestor]);

  if (loading) {
    return (
      <div className="space-y-6 max-w-5xl mx-auto">
        <Skeleton className="h-8 w-32" />
        <Skeleton className="h-24 w-full" />
        <div className="grid grid-cols-2 gap-4">
          <Skeleton className="h-64 w-full" />
          <Skeleton className="h-64 w-full" />
        </div>
      </div>
    );
  }

  if (loadError || !trace) {
    return (
      <div className="p-8 text-center text-destructive bg-destructive/10 rounded-lg max-w-2xl mx-auto mt-12">
        <h2 className="font-bold mb-2">{t("trace_detail_not_found")}</h2>
        <p>{t("trace_detail_not_found_desc")}</p>
        <Link href="/traces">
          <Button variant="outline" className="mt-4">{t("trace_detail_back")}</Button>
        </Link>
      </div>
    );
  }

  return (
    <div className="space-y-6 max-w-5xl mx-auto pb-12">
      <div className="flex items-center gap-4">
        <Link href="/traces">
          <Button variant="outline" size="icon" data-testid="button-back">
            <ArrowLeft className="h-4 w-4" />
          </Button>
        </Link>
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-foreground flex items-center gap-3">
            {t("trace_detail_title")}
            <span className="text-sm font-mono font-normal text-muted-foreground bg-muted px-2 py-1 rounded">
              {trace.id}
            </span>
          </h1>
          <div className="text-sm text-muted-foreground mt-1 flex items-center gap-2">
            <Clock className="h-4 w-4" />
            {format(new Date(trace.timestamp), "PPpp")}
          </div>
        </div>
      </div>

      <div className="bg-card border border-border p-5 rounded-lg shadow-sm">
        <div className="text-sm font-semibold text-foreground uppercase tracking-wider mb-3 flex items-center gap-2">
          <MessageSquare className="h-4 w-4 text-accent" />
          {t("trace_detail_user_message")}
        </div>
        <div className="text-lg font-medium text-foreground pl-6 border-l-2 border-accent">
          "{trace.userMessage}"
        </div>
      </div>

      <TraceViewer trace={trace as any} />
    </div>
  );
}
