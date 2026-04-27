import { useState, useEffect } from "react";
import { useParams, Link } from "wouter";
import { TraceViewer } from "@/components/trace-viewer";
import { Button } from "@/components/ui/button";
import { ArrowLeft, Clock, MessageSquare } from "lucide-react";
import { format } from "date-fns";
import { Skeleton } from "@/components/ui/skeleton";
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
      key: c.key ?? "",
      value: c.value ?? "",
      source: c.source ?? "",
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

export default function TraceDetail() {
  const params = useParams();
  const id = params.id as string;
  const { t } = useI18n();

  const [trace, setTrace] = useState<ReturnType<typeof mapTrace> | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;

    async function fetchTrace() {
      setLoading(true);
      setLoadError(null);
      try {
        const token = getToken();
        const res = await fetch(`/v1/traces/${id}`, {
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
  }, [id]);

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
