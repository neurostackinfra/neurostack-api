import { useEffect, useState } from "react";
import { customFetch } from "@workspace/api-client-react";
import {
  Brain, ShieldAlert, CheckCircle2, Filter, Zap, Loader2, AlertTriangle, Clock,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useToast } from "@/hooks/use-toast";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell,
} from "recharts";

// ── Types ────────────────────────────────────────────────────────────────────

interface RatingEntry {
  id: string;
  ts: string;
  tenant_id: string;
  rating: number;
  reason?: string;
  agent_response?: string;
  user_message?: string;
  submitted_by?: string;
  ai_analysis?: { status: "agreed" | "disagreed"; justification: string };
  ai_analysis_status?: "pending" | "done" | "failed";
  approved_for_training: boolean;
  ignored: boolean;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

const RATING_META: Record<number, { label: string; color: string; tailwind: string }> = {
  1: { label: "Péssimo", color: "#ef4444", tailwind: "bg-red-500"    },
  2: { label: "Ruim",    color: "#fb7185", tailwind: "bg-rose-400"   },
  3: { label: "Regular", color: "#facc15", tailwind: "bg-yellow-400" },
  4: { label: "Bom",     color: "#4ade80", tailwind: "bg-green-400"  },
  5: { label: "Ótimo",   color: "#22c55e", tailwind: "bg-green-500"  },
};

function RatingBadge({ rating }: { rating: number }) {
  const meta = RATING_META[rating] ?? RATING_META[3];
  return (
    <span
      className={`inline-flex items-center justify-center h-6 w-6 rounded text-white text-[11px] font-bold ${meta.tailwind}`}
    >
      {rating}
    </span>
  );
}

function AiAnalysisCell({ entry }: { entry: RatingEntry }) {
  if (!entry.ai_analysis_status) return <span className="text-[10px] text-muted-foreground">—</span>;

  if (entry.ai_analysis_status === "pending") {
    return (
      <span className="inline-flex items-center gap-1 text-[10px] text-muted-foreground">
        <Clock className="h-3 w-3" />
        Analisando...
      </span>
    );
  }

  if (entry.ai_analysis_status === "failed") {
    return (
      <span className="inline-flex items-center gap-1 text-[10px] text-amber-600">
        <AlertTriangle className="h-3 w-3" />
        Análise indisponível
      </span>
    );
  }

  if (entry.ai_analysis_status === "done" && entry.ai_analysis) {
    const agreed = entry.ai_analysis.status === "agreed";
    return (
      <div>
        <Badge
          variant="outline"
          className={`text-[10px] py-0 gap-1 mb-0.5 ${
            agreed
              ? "bg-green-500/10 text-green-600 border-green-500/30"
              : "bg-red-500/10 text-red-500 border-red-500/30"
          }`}
        >
          {agreed ? <CheckCircle2 className="h-2.5 w-2.5" /> : <ShieldAlert className="h-2.5 w-2.5" />}
          {agreed ? "Concorda" : "Discorda"}
        </Badge>
        <p className="text-[10px] text-muted-foreground italic leading-tight">
          {entry.ai_analysis.justification}
        </p>
      </div>
    );
  }

  return null;
}

// ── Avaliações 1-5 ───────────────────────────────────────────────────────────

function RatingsTab() {
  const { toast } = useToast();
  const [entries, setEntries]     = useState<RatingEntry[]>([]);
  const [loading, setLoading]     = useState(true);
  const [builderId, setBuilderId] = useState("");
  const [actioningId, setActioningId] = useState<string | null>(null);

  const loadRatings = async (bid?: string) => {
    setLoading(true);
    try {
      const qs = new URLSearchParams({ limit: "100" });
      if (bid) qs.set("builder_id", bid);
      const data = await customFetch<RatingEntry[]>(`/v1/rlhf/ratings?${qs}`);
      setEntries(Array.isArray(data) ? data : []);
    } catch {
      toast({ title: "Erro ao carregar avaliações", variant: "destructive" });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void loadRatings(); }, []);

  const applyFilter = () => loadRatings(builderId.trim() || undefined);

  const takeAction = async (entryId: string, action: "approve" | "ignore") => {
    setActioningId(entryId);
    try {
      await customFetch(`/v1/rlhf/ratings/${entryId}`, {
        method: "PATCH",
        body: JSON.stringify({ action }),
      });
      setEntries((prev) =>
        prev.map((e) =>
          e.id === entryId
            ? {
                ...e,
                approved_for_training: action === "approve",
                ignored: action === "ignore",
              }
            : e
        )
      );
      toast({ title: action === "approve" ? "Aprovado para Dataset" : "Ignorado" });
    } catch {
      toast({ title: "Erro ao executar ação", variant: "destructive" });
    } finally {
      setActioningId(null);
    }
  };

  // KPIs
  const total       = entries.length;
  const satisfied   = entries.filter((e) => e.rating >= 3).length;
  const unsatisfied = entries.filter((e) => e.rating <= 2).length;
  const approved    = entries.filter((e) => e.approved_for_training).length;
  const pctSat      = total > 0 ? Math.round((satisfied / total) * 100) : 0;

  // Chart data
  const chartData = [1, 2, 3, 4, 5].map((n) => ({
    name: RATING_META[n].label,
    count: entries.filter((e) => e.rating === n).length,
    color: RATING_META[n].color,
  }));

  return (
    <div className="space-y-6">
      {/* Filters */}
      <div className="flex items-center gap-2 flex-wrap">
        <input
          type="text"
          value={builderId}
          onChange={(e) => setBuilderId(e.target.value)}
          placeholder="Builder ID (super admin)"
          className="h-8 text-xs border border-border rounded px-2 bg-background text-foreground placeholder:text-muted-foreground w-64"
          onKeyDown={(e) => e.key === "Enter" && applyFilter()}
        />
        <Button size="sm" variant="outline" className="h-8 gap-1.5 text-xs" onClick={applyFilter}>
          <Filter className="h-3 w-3" />
          Filtrar
        </Button>
        {builderId && (
          <Button size="sm" variant="ghost" className="h-8 text-xs text-muted-foreground" onClick={() => { setBuilderId(""); loadRatings(); }}>
            Limpar
          </Button>
        )}
      </div>

      {/* KPI Cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
        {[
          { label: "Total de Avaliações", value: total,       sub: "Todas as clínicas",     color: "text-foreground"  },
          { label: "% Satisfação",        value: `${pctSat}%`,sub: "Notas ≥ 3",             color: "text-green-400"   },
          { label: "Insatisfação",        value: unsatisfied, sub: "Notas 1 ou 2",           color: "text-red-400"     },
          { label: "Aprovados p/ Dataset",value: approved,    sub: "Prontos para fine-tuning",color: "text-violet-400" },
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

      {/* Distribution Chart */}
      {total > 0 && (
        <Card className="border-border shadow-sm">
          <CardContent className="pt-4 pb-2">
            <p className="text-[10px] font-bold uppercase tracking-widest text-muted-foreground mb-3">
              Distribuição de Notas
            </p>
            <ResponsiveContainer width="100%" height={120}>
              <BarChart data={chartData} layout="vertical" margin={{ left: 8, right: 24 }}>
                <XAxis type="number" tick={{ fontSize: 10 }} />
                <YAxis type="category" dataKey="name" tick={{ fontSize: 10 }} width={80} />
                <Tooltip
                  formatter={(v: number) => [`${v} avaliações`, "Total"]}
                  contentStyle={{ fontSize: 11 }}
                />
                <Bar dataKey="count" radius={[0, 4, 4, 0]}>
                  {chartData.map((entry) => (
                    <Cell key={entry.name} fill={entry.color} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>
      )}

      {/* Entries List */}
      {loading ? (
        <div className="flex items-center justify-center py-12 gap-2 text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          <span className="text-sm">Carregando avaliações...</span>
        </div>
      ) : entries.length === 0 ? (
        <div className="text-center py-12 text-sm text-muted-foreground">
          Nenhuma avaliação registrada ainda.
        </div>
      ) : (
        <div className="space-y-3">
          <div className="hidden md:grid grid-cols-[0.6fr_1.8fr_1.5fr_1fr_0.9fr] gap-4 px-4 py-2 rounded-md bg-muted/50 border border-border">
            {["Nota", "Motivo", "Análise IA", "Clínica / Usuário", "Ação"].map((h) => (
              <span key={h} className="text-[10px] font-bold uppercase tracking-widest text-muted-foreground">{h}</span>
            ))}
          </div>

          {entries.map((entry) => (
            <Card
              key={entry.id}
              className={`border-border shadow-sm overflow-hidden transition-opacity ${entry.ignored ? "opacity-40" : ""}`}
            >
              <div className="flex items-center justify-between px-4 py-2 bg-slate-50/50 dark:bg-slate-900/50 border-b border-border">
                <span className="text-xs font-mono text-muted-foreground">{entry.id.slice(0, 8)}...</span>
                <span className="text-[10px] text-muted-foreground">
                  {entry.ts ? new Date(entry.ts).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" }) : ""}
                </span>
              </div>

              <CardContent className="pt-3 pb-3">
                <div className="grid grid-cols-1 md:grid-cols-[0.6fr_1.8fr_1.5fr_1fr_0.9fr] gap-4 items-start">
                  {/* Nota */}
                  <div className="flex items-center gap-1.5">
                    <RatingBadge rating={entry.rating} />
                    <span className="text-[10px] text-muted-foreground">{RATING_META[entry.rating]?.label}</span>
                  </div>

                  {/* Motivo */}
                  <div>
                    {entry.reason ? (
                      <p className="text-xs italic text-foreground/80 bg-muted rounded px-2 py-1 border border-border">
                        "{entry.reason}"
                      </p>
                    ) : (
                      <span className="text-[10px] text-muted-foreground">Sem motivo</span>
                    )}
                  </div>

                  {/* Análise IA */}
                  <AiAnalysisCell entry={entry} />

                  {/* Clínica / Usuário */}
                  <div>
                    <p className="text-[11px] font-semibold text-foreground">{entry.tenant_id.slice(0, 8)}...</p>
                    <p className="text-[10px] text-muted-foreground">{entry.submitted_by ?? "—"}</p>
                  </div>

                  {/* Ação */}
                  <div className="flex flex-col gap-1.5">
                    {entry.approved_for_training ? (
                      <Badge className="text-[9px] bg-violet-500/15 text-violet-400 border border-violet-500/30 flex items-center gap-1 w-fit py-0">
                        <Zap className="h-2.5 w-2.5" /> No Dataset
                      </Badge>
                    ) : entry.ignored ? (
                      <span className="text-[10px] text-muted-foreground">Ignorado</span>
                    ) : (
                      <>
                        <Button
                          size="sm"
                          className="text-[10px] h-6 bg-violet-600 hover:bg-violet-500 text-white gap-1 px-2"
                          onClick={() => takeAction(entry.id, "approve")}
                          disabled={actioningId === entry.id}
                        >
                          <Zap className="h-2.5 w-2.5" />
                          Aprovar p/ Dataset
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          className="text-[10px] h-6 gap-1 px-2 text-muted-foreground"
                          onClick={() => takeAction(entry.id, "ignore")}
                          disabled={actioningId === entry.id}
                        >
                          Ignorar
                        </Button>
                      </>
                    )}
                  </div>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Main Page ────────────────────────────────────────────────────────────────

export default function RlhfAudit() {
  return (
    <div className="space-y-7 max-w-6xl mx-auto">

      {/* Header */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <div className="flex items-center gap-3 mb-2">
            <div className="h-9 w-9 rounded-lg bg-violet-500/15 border border-violet-500/30 flex items-center justify-center">
              <Brain className="h-5 w-5 text-violet-400" />
            </div>
            <div>
              <div className="flex items-center gap-2.5">
                <h1 className="text-2xl font-bold text-foreground tracking-tight">Auditoria RLHF</h1>
              </div>
              <p className="text-xs text-violet-400 font-semibold uppercase tracking-widest">
                Relatório de Alinhamento — RLHF Data Flywheel
              </p>
            </div>
          </div>
          <p className="text-sm text-muted-foreground max-w-2xl">
            Registro centralizado de avaliações de resposta enviadas pelos builders.
            Cada entrada representa um sinal de alinhamento comportamental para fine-tuning do agente.
          </p>
        </div>
      </div>

      {/* Restricted badge */}
      <div className="flex items-center gap-2 px-3 py-2 rounded-md bg-gray-900 border border-black w-fit">
        <ShieldAlert className="h-3.5 w-3.5 text-white shrink-0" />
        <span className="text-xs font-bold text-white">
          Acesso restrito: Somente Master Admin. Contém metodologia proprietária de alinhamento SRM.
        </span>
      </div>

      <RatingsTab />
    </div>
  );
}
