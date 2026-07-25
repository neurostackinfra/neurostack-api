import { useEffect, useState } from "react";
import { customFetch } from "@workspace/api-client-react";
import {
  Brain, ShieldAlert, CheckCircle2, Filter, Download, FlaskConical,
  Zap, Loader2, AlertTriangle, Clock,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useToast } from "@/hooks/use-toast";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell,
} from "recharts";

// ── Types ────────────────────────────────────────────────────────────────────

interface SuggestionEntry {
  id: string;
  ts: string;
  tenant_id: string;
  submitted_by?: string;
  user_message?: string;
  agent_response?: string;
  suggested_response?: string;
  status?: "aligned" | "forced";
  intent?: string;
}

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
  1: { label: "Péssimo",      color: "#ef4444", tailwind: "bg-red-500"    },
  2: { label: "Ruim",         color: "#fb7185", tailwind: "bg-rose-400"   },
  3: { label: "Regular",      color: "#facc15", tailwind: "bg-yellow-400" },
  4: { label: "Boa resposta", color: "#4ade80", tailwind: "bg-green-400"  },
  5: { label: "Ótima",        color: "#22c55e", tailwind: "bg-green-500"  },
};

const INTENT_LABELS: Record<string, string> = {
  pedido_preco: "Pedido de Preço",
  interesse_tratamento: "Interesse Tratamento",
  reclamacao: "Reclamação",
  agendar: "Agendamento",
  duvida_pos_procedimento: "Pós-Procedimento",
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

// ── Tab: Sugestões RLHF ──────────────────────────────────────────────────────

function SuggestionsTab() {
  const [entries, setEntries]   = useState<SuggestionEntry[]>([]);
  const [loading, setLoading]   = useState(true);

  useEffect(() => {
    const load = async () => {
      try {
        const raw = localStorage.getItem("ns_auth");
        const token = (() => { try { return (JSON.parse(raw ?? "{}") as { token?: string }).token ?? ""; } catch { return ""; } })();
        const res = await fetch("/v1/admin/rlhf/pending?limit=100", {
          headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        });
        if (res.ok) {
          const data = await res.json();
          setEntries(Array.isArray(data) ? data : (data.entries ?? []));
        }
      } catch { /* silently degrade */ } finally {
        setLoading(false);
      }
    };
    void load();
  }, []);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16 text-muted-foreground gap-2">
        <Loader2 className="h-4 w-4 animate-spin" />
        <span className="text-sm">Carregando sugestões...</span>
      </div>
    );
  }

  if (entries.length === 0) {
    return (
      <div className="text-center py-16 text-sm text-muted-foreground">
        Nenhuma sugestão de correção registrada ainda.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {/* Table header */}
      <div className="hidden md:grid grid-cols-[1.2fr_0.9fr_2.2fr_0.9fr] gap-4 px-4 py-2 rounded-md bg-muted/50 border border-border">
        {["Clínica / Usuário", "Status", "Resposta vs. Sugestão", "Intenção"].map((h) => (
          <span key={h} className="text-[10px] font-bold uppercase tracking-widest text-muted-foreground">{h}</span>
        ))}
      </div>

      {entries.map((entry) => (
        <Card key={entry.id} className="border-border shadow-sm overflow-hidden">
          <div className="flex items-center justify-between px-4 py-2.5 bg-slate-50/50 dark:bg-slate-900/50 border-b border-border">
            <span className="text-xs font-mono text-muted-foreground">{entry.id.slice(0, 8)}...</span>
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
                  : <><ShieldAlert className="h-2.5 w-2.5" /> Forçado</>}
              </Badge>
              <span className="text-[10px] text-muted-foreground hidden sm:block">
                {entry.ts ? new Date(entry.ts).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" }) : ""}
              </span>
            </div>
          </div>
          <CardContent className="pt-4 pb-4">
            <div className="grid grid-cols-1 md:grid-cols-[1.2fr_0.9fr_2.2fr_0.9fr] gap-4 items-start">
              <div>
                <p className="text-sm font-semibold text-foreground">{entry.tenant_id.slice(0, 8)}...</p>
                <p className="text-[11px] text-muted-foreground mt-0.5">{entry.submitted_by ?? "—"}</p>
              </div>
              <div>
                {entry.status === "aligned" ? (
                  <div className="flex items-center gap-1.5 text-green-400">
                    <CheckCircle2 className="h-4 w-4 shrink-0" />
                    <div>
                      <p className="text-xs font-bold">Alinhado</p>
                      <p className="text-[10px] text-green-400/70">Conforme SRM</p>
                    </div>
                  </div>
                ) : (
                  <div className="flex items-center gap-1.5 text-red-400">
                    <ShieldAlert className="h-4 w-4 shrink-0" />
                    <div>
                      <p className="text-xs font-bold">Override Forçado</p>
                      <p className="text-[10px] text-red-400/70">Fere Metodologia</p>
                    </div>
                  </div>
                )}
              </div>
              <div className="space-y-2">
                <div>
                  <p className="text-[10px] font-bold text-muted-foreground mb-0.5 uppercase tracking-wider">Resposta Original</p>
                  <p className="text-xs italic bg-muted rounded px-2.5 py-1.5 border border-border text-foreground/80">
                    {entry.agent_response ?? "—"}
                  </p>
                </div>
                <div>
                  <p className={`text-[10px] font-bold mb-0.5 uppercase tracking-wider ${entry.status === "aligned" ? "text-emerald-600" : "text-rose-600"}`}>
                    {entry.status === "aligned" ? "✅ Sugestão Alinhada" : "⚠️ Sugestão Forçada"}
                  </p>
                  <p className={`text-xs rounded px-2.5 py-1.5 border font-medium ${
                    entry.status === "aligned"
                      ? "text-emerald-950 dark:text-emerald-100 bg-emerald-50 dark:bg-emerald-950/30 border-emerald-200 dark:border-emerald-800/40"
                      : "text-rose-950 dark:text-rose-100 bg-rose-50 dark:bg-rose-950/30 border-rose-200 dark:border-rose-800/40"
                  }`}>
                    {entry.suggested_response ?? "—"}
                  </p>
                </div>
              </div>
              <div>
                <Badge variant="outline" className="text-[9px] py-0 text-blue-400 border-blue-400/30 bg-blue-500/10">
                  {INTENT_LABELS[entry.intent ?? ""] ?? (entry.intent ?? "—")}
                </Badge>
              </div>
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

// ── Tab: Avaliações 1-5 ───────────────────────────────────────────────────────

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
  const [tab, setTab] = useState<"suggestions" | "ratings">("suggestions");

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
            Registro centralizado de sugestões de correção e avaliações de resposta enviadas pelos builders.
            Cada entrada representa um sinal de alinhamento comportamental para fine-tuning do agente.
          </p>
        </div>
        <Button variant="outline" size="sm" className="gap-2 text-xs shrink-0">
          <Download className="h-3.5 w-3.5" />
          Exportar Dataset
        </Button>
      </div>

      {/* Restricted badge */}
      <div className="flex items-center gap-2 px-3 py-2 rounded-md bg-gray-900 border border-black w-fit">
        <ShieldAlert className="h-3.5 w-3.5 text-white shrink-0" />
        <span className="text-xs font-bold text-white">
          Acesso restrito: Somente Master Admin. Contém metodologia proprietária de alinhamento SRM.
        </span>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 border-b border-border">
        {(["suggestions", "ratings"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={[
              "flex items-center gap-1.5 px-4 py-2 text-xs font-semibold uppercase tracking-wider border-b-2 transition-colors",
              tab === t
                ? "border-violet-500 text-violet-400"
                : "border-transparent text-muted-foreground hover:text-foreground",
            ].join(" ")}
          >
            {t === "suggestions" ? (
              <><FlaskConical className="h-3.5 w-3.5" /> Sugestões RLHF</>
            ) : (
              <><Brain className="h-3.5 w-3.5" /> Avaliações 1–5</>
            )}
          </button>
        ))}
      </div>

      {/* Tab Content */}
      {tab === "suggestions" ? <SuggestionsTab /> : <RatingsTab />}
    </div>
  );
}
