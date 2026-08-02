import { useState } from "react";
import { customFetch } from "@workspace/api-client-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { CheckCircle2, Loader2 } from "lucide-react";
import { useToast } from "@/hooks/use-toast";

const RATING_META: Record<number, { label: string; bg: string; ring: string }> = {
  1: { label: "Péssimo",       bg: "bg-red-500",    ring: "ring-red-500"    },
  2: { label: "Ruim",          bg: "bg-rose-400",   ring: "ring-rose-400"   },
  3: { label: "Regular",       bg: "bg-yellow-400", ring: "ring-yellow-400" },
  4: { label: "Boa resposta",  bg: "bg-green-400",  ring: "ring-green-400"  },
  5: { label: "Ótima",         bg: "bg-green-500",  ring: "ring-green-500"  },
};

export interface RatingBlockProps {
  msgId: string;
  sessionId?: string | null;
  agentResponse: string;
  userMessage: string;
  isReadOnly?: boolean;
}

export function RatingBlock({
  msgId,
  sessionId,
  agentResponse,
  userMessage,
  isReadOnly = false,
}: RatingBlockProps) {
  const { toast } = useToast();
  const [selected, setSelected] = useState<number | null>(null);
  const [reason, setReason]     = useState("");
  const [phase, setPhase]       = useState<"idle" | "submitting" | "submitted">("idle");

  const needsReason = selected !== null && selected <= 2;
  const canSubmit   = selected !== null && (!needsReason || reason.trim().length > 0);

  const submit = async () => {
    if (!canSubmit || phase !== "idle") return;
    setPhase("submitting");

    if (isReadOnly) {
      await new Promise(r => setTimeout(r, 600));
      setPhase("submitted");
      return;
    }

    try {
      await customFetch("/v1/rlhf/rating", {
        method: "POST",
        body: JSON.stringify({
          session_id: sessionId || "",
          message_id: msgId,
          rating: selected,
          reason: reason.trim() || null,
          agent_response: agentResponse,
          user_message: userMessage,
        }),
      });
      setPhase("submitted");
    } catch {
      toast({ title: "Erro ao enviar avaliação", variant: "destructive" });
      setPhase("idle");
    }
  };

  if (phase === "submitted") {
    return (
      <div className="mt-1.5 ml-1 text-[10px] text-muted-foreground flex items-center gap-1">
        <CheckCircle2 className="h-3 w-3 text-green-500" />
        Avaliação enviada
      </div>
    );
  }

  return (
    <div className="mt-2 ml-1">
      <p className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground mb-1.5">
        Avaliação da resposta LLM
      </p>
      <div className="flex items-center gap-1.5 flex-wrap">
        {([1, 2, 3, 4, 5] as const).map((n) => {
          const meta = RATING_META[n];
          const isOn = selected === n;
          return (
            <button
              key={n}
              onClick={() => { if (phase === "idle") setSelected(n); }}
              disabled={phase === "submitting"}
              className={[
                "h-7 w-7 rounded text-white text-xs font-bold transition-all",
                meta.bg,
                isOn
                  ? `ring-2 ring-offset-1 ${meta.ring} scale-110`
                  : "opacity-80 hover:opacity-100 hover:scale-105",
              ].join(" ")}
              data-testid={`rating-btn-${n}-${msgId}`}
            >
              {n}
            </button>
          );
        })}
        {selected !== null && (
          <span className="text-[11px] text-muted-foreground ml-0.5">
            {RATING_META[selected].label}
          </span>
        )}
      </div>

      {needsReason && (
        <div className="mt-2 space-y-1.5">
          <p className="text-[10px] text-muted-foreground">
            Por gentileza, nos conte o motivo desta nota.
          </p>
          <Textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Descreva o motivo..."
            className="resize-none text-xs min-h-[60px] border-border"
            disabled={phase === "submitting"}
            data-testid={`rating-reason-${msgId}`}
          />
        </div>
      )}

      {selected !== null && (
        <div className="mt-2">
          <Button
            size="sm"
            className="text-xs h-7 bg-violet-600 hover:bg-violet-500 text-white gap-1.5"
            onClick={submit}
            disabled={!canSubmit || phase === "submitting"}
            data-testid={`rating-submit-${msgId}`}
          >
            {phase === "submitting"
              ? <><Loader2 className="h-3 w-3 animate-spin" /> Enviando...</>
              : "Enviar avaliação"}
          </Button>
        </div>
      )}
    </div>
  );
}
