import { Cpu, Lock, KeyRound, Zap, TrendingDown, FlaskConical, SlidersHorizontal, Info } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Slider } from "@/components/ui/slider";
import { useI18n } from "@/context/i18n";

function SectionDivider({ num, label }: { num: string; label: string }) {
  return (
    <div className="flex items-center gap-3">
      <div className="flex h-5 w-5 items-center justify-center rounded-full bg-indigo-500/20 shrink-0">
        <span className="text-[9px] font-bold text-indigo-400">{num}</span>
      </div>
      <span className="text-[10px] font-bold uppercase tracking-widest text-indigo-500">{label}</span>
      <div className="flex-1 h-px bg-indigo-500/20" />
    </div>
  );
}

export default function SlmCortex() {
  const { locale } = useI18n();
  const txt = (p: string, e: string) => locale === "en" ? e : p;

  return (
    <div className="space-y-8 max-w-4xl">

      {/* ── Page header ──────────────────────────────────────────────────────── */}
      <div className="space-y-4">
        <div className="flex items-start justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-indigo-500/10 border border-indigo-500/20">
              <Cpu className="h-5 w-5 text-indigo-500" />
            </div>
            <div>
              <h1 className="text-2xl font-bold tracking-tight text-foreground">
                NeuroStack SLM Cortex
                <span className="text-foreground/60 font-normal text-lg ml-2">(Proprietary Engine)</span>
              </h1>
              <p className="text-sm font-medium text-foreground/70 mt-0.5">
                {txt(
                  "Motor de linguagem proprietário — rápido, privado e econômico",
                  "Proprietary language engine — fast, private and cost-efficient"
                )}
              </p>
            </div>
          </div>
          <Badge
            variant="outline"
            className="shrink-0 mt-1 bg-indigo-950/60 border-indigo-500/30 text-indigo-300 font-semibold text-[10px] uppercase tracking-wider px-3 py-1.5"
          >
            <FlaskConical className="h-3 w-3 mr-1.5" />
            {txt("Roadmap · Alpha", "Roadmap · Alpha")}
          </Badge>
        </div>

        {/* Banner */}
        <div className="relative overflow-hidden rounded-lg border border-indigo-800 bg-indigo-950 px-5 py-4">
          <div className="relative flex items-center gap-4">
            <Cpu className="h-8 w-8 text-white shrink-0" />
            <div className="flex-1">
              <p className="text-sm font-bold text-white">
                {txt("ROADMAP: Em fase de testes Alpha", "ROADMAP: In Alpha testing phase")}
              </p>
              <p className="text-xs font-medium text-white/80 mt-0.5 leading-relaxed">
                {txt(
                  "O SLM Cortex é o motor de IA proprietário da NeuroStack — treinado exclusivamente em dados do setor de saúde brasileiro. Processamento 100% local, sem vazamento de dados externos.",
                  "SLM Cortex is NeuroStack's proprietary AI engine — trained exclusively on Brazilian healthcare sector data. 100% local processing, no external data leakage."
                )}
              </p>
            </div>
            <div className="flex flex-col items-end gap-1 shrink-0 text-right">
              <Badge variant="outline" className="text-[9px] font-bold tracking-widest bg-indigo-900/40 border-indigo-500/30 text-indigo-200 px-2">
                {txt("10× Mais Barato", "10× Cheaper")}
              </Badge>
              <Badge variant="outline" className="text-[9px] font-bold tracking-widest bg-indigo-900/40 border-indigo-500/30 text-indigo-200 px-2">Zero Data Egress</Badge>
              <Badge variant="outline" className="text-[9px] font-bold tracking-widest bg-indigo-900/40 border-indigo-500/30 text-indigo-200 px-2">
                {txt("LGPD Nativo", "LGPD Native")}
              </Badge>
            </div>
          </div>
        </div>

        {/* Stat strip */}
        <div className="grid grid-cols-3 gap-3">
          {[
            { label: txt("Latência Média", "Average Latency"),      value: "< 180ms", note: "vs 800ms OpenAI"                                   },
            { label: txt("Custo por 1M tokens", "Cost per 1M tokens"), value: "$0.50",   note: "vs $5.00 GPT-4o"                                  },
            { label: txt("Domínios Treinados", "Trained Domains"),  value: "7",       note: txt("saúde & bem-estar", "health & wellness")         },
          ].map(s => (
            <div key={s.label} className="rounded-lg border border-indigo-800 bg-indigo-950 px-4 py-3 text-center">
              <p className="text-xl font-bold text-white">{s.value}</p>
              <p className="text-[11px] font-semibold text-white/90 mt-0.5">{s.label}</p>
              <p className="text-[10px] font-medium text-indigo-300 mt-0.5">{s.note}</p>
            </div>
          ))}
        </div>
      </div>

      {/* ── Block 1: API & Endpoint ──────────────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="01" label={txt("Gestão de API & Endpoint", "API & Endpoint Management")} />
        <Card className="border-border/60 bg-card opacity-80">
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <Lock className="h-4 w-4 text-indigo-500" />
              <CardTitle className="text-sm font-semibold text-foreground">
                {txt("Endpoint Interno do Cortex", "Cortex Internal Endpoint")}
              </CardTitle>
            </div>
            <p className="text-xs font-medium text-foreground/75 mt-1">
              {txt(
                "Processamento 100% local dentro da infraestrutura NeuroStack. Nenhum dado sai da sua rede.",
                "100% local processing within NeuroStack infrastructure. No data leaves your network."
              )}
            </p>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5">
              <Label className="text-xs font-semibold text-foreground/80">
                {txt("URL do Endpoint", "Endpoint URL")}
              </Label>
              <div className="relative">
                <Lock className="absolute left-3 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-foreground/40" />
                <Input
                  readOnly
                  disabled
                  value="https://cortex.neurostack.local/v1"
                  className="pl-8 font-mono text-xs text-foreground/70 bg-muted/30 border-border/50 cursor-not-allowed opacity-70"
                />
              </div>
            </div>

            <div className="flex items-center gap-3">
              <Button disabled variant="outline" size="sm" className="gap-2 opacity-50 cursor-not-allowed">
                <KeyRound className="h-3.5 w-3.5" />
                {txt("Gerar API Key do Cortex", "Generate Cortex API Key")}
              </Button>
              <Badge variant="outline" className="text-[10px] font-semibold bg-emerald-500/10 border-emerald-500/25 text-emerald-600 dark:text-emerald-400">
                {txt("Zero Data Egress · Processamento Local", "Zero Data Egress · Local Processing")}
              </Badge>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* ── Block 2: Hybrid Routing ──────────────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="02" label={txt("Roteamento Híbrido & Confiança", "Hybrid Routing & Confidence")} />
        <Card className="border-border/60 bg-card opacity-80">
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <SlidersHorizontal className="h-4 w-4 text-indigo-500" />
              <CardTitle className="text-sm font-semibold text-foreground">
                {txt("Threshold de Fallback (Confiança)", "Fallback Threshold (Confidence)")}
              </CardTitle>
            </div>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <Label className="text-xs font-semibold text-foreground/80 cursor-not-allowed">
                  {txt("Confiança mínima do Cortex", "Minimum Cortex confidence")}
                </Label>
                <span className="text-sm font-bold tabular-nums text-indigo-500">85%</span>
              </div>
              <Slider
                disabled
                defaultValue={[85]}
                max={100}
                step={1}
                className="opacity-50 cursor-not-allowed"
              />
              <div className="flex justify-between text-[10px] font-medium text-foreground/55">
                <span>{txt("0% (sempre fallback)", "0% (always fallback)")}</span>
                <span>{txt("100% (nunca fallback)", "100% (never fallback)")}</span>
              </div>
            </div>

            <div className="flex items-start gap-2.5 rounded-lg border border-indigo-500/20 bg-indigo-500/5 px-3 py-3">
              <Info className="h-4 w-4 text-indigo-500 mt-0.5 shrink-0" />
              <p className="text-xs font-medium text-foreground/85 leading-relaxed">
                {txt(
                  <>Se a confiança do Cortex for menor que{" "}<strong className="font-bold text-foreground">85%</strong>, a requisição será enviada automaticamente para o seu LLM de Fallback configurado (OpenAI / Neuralake).</>,
                  <>If Cortex confidence is below{" "}<strong className="font-bold text-foreground">85%</strong>, the request will be automatically forwarded to your configured Fallback LLM (OpenAI / Neuralake).</>
                )}
              </p>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* ── Block 3: Fine-Tuning Packs ───────────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="03" label={txt("Módulos de Especialidade (Fine-Tuning Packs)", "Specialty Modules (Fine-Tuning Packs)")} />
        <Card className="border-border/60 bg-card opacity-80">
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <Cpu className="h-4 w-4 text-indigo-500" />
              <CardTitle className="text-sm font-semibold text-foreground">
                {txt("Packs Médicos Pré-Treinados", "Pre-Trained Medical Packs")}
              </CardTitle>
            </div>
            <p className="text-xs font-medium text-foreground/75 mt-1">
              {txt(
                "O Cortex é treinado em milhões de interações reais do setor de saúde. Ative o pack correspondente à especialidade da sua clínica.",
                "Cortex is trained on millions of real healthcare sector interactions. Activate the pack matching your clinic's specialty."
              )}
            </p>
          </CardHeader>
          <CardContent className="space-y-2">
            {[
              { pt: "Pack Odontologia Estética", en: "Aesthetic Dentistry Pack" },
              { pt: "Pack Dermatologia & Estética",   en: "Dermatology & Aesthetics Pack" },
              { pt: "Pack Harmonização Facial",  en: "Facial Harmonization Pack" },
              { pt: "Pack Cirurgia Plástica",    en: "Plastic Surgery Pack" },
              { pt: "Pack Ortopedia",            en: "Orthopedics Pack" },
              { pt: "Pack Oftalmologia",         en: "Ophthalmology Pack" },
            ].map(({ pt, en }) => (
              <div key={pt} className="flex items-center justify-between gap-4 rounded-lg border border-border/60 bg-muted/20 px-4 py-3 cursor-not-allowed">
                <p className="text-sm font-semibold text-foreground">
                  {txt(pt, en)}
                </p>
                <Switch
                  checked={false}
                  disabled
                  className="opacity-50 cursor-not-allowed shrink-0"
                />
              </div>
            ))}
          </CardContent>
        </Card>
      </div>

      {/* ── Block 4: FinOps ──────────────────────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="04" label={txt("FinOps · Simulação de Economia", "FinOps · Savings Simulation")} />
        <Card className="border-border/60 bg-card opacity-80">
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <TrendingDown className="h-4 w-4 text-emerald-500" />
              <CardTitle className="text-sm font-semibold text-foreground">
                {txt("Comparativo de Custo por 1M Tokens", "Cost Comparison per 1M Tokens")}
              </CardTitle>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-3">
              {[
                { label: "GPT-4o (OpenAI)",           cost: "$5.00", pct: 100, barClass: "bg-rose-500/60",    textClass: "text-rose-600 dark:text-rose-400"    },
                { label: "Dropout (Neuralake)",        cost: "$2.50", pct: 50,  barClass: "bg-amber-500/60",   textClass: "text-amber-600 dark:text-amber-400"   },
                { label: "SLM Cortex (NeuroStack)",    cost: "$0.50", pct: 10,  barClass: "bg-emerald-500/70", textClass: "text-emerald-600 dark:text-emerald-400" },
              ].map(r => (
                <div key={r.label} className="space-y-1.5">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-semibold text-foreground/85">{r.label}</span>
                    <span className={`text-xs font-bold tabular-nums ${r.textClass}`}>{r.cost}</span>
                  </div>
                  <div className="h-2 w-full rounded-full bg-muted overflow-hidden">
                    <div
                      className={`h-full ${r.barClass} rounded-full transition-all duration-700`}
                      style={{ width: `${r.pct}%` }}
                    />
                  </div>
                </div>
              ))}
            </div>

            <div className="grid grid-cols-2 gap-3 pt-1">
              <div className="rounded-lg border border-emerald-500/25 bg-emerald-500/5 px-4 py-3 text-center">
                <div className="flex items-center justify-center gap-1.5 mb-1">
                  <Zap className="h-3.5 w-3.5 text-emerald-500" />
                  <span className="text-[10px] font-bold uppercase tracking-wider text-emerald-600 dark:text-emerald-400">
                    {txt("Economia Mensal", "Monthly Savings")}
                  </span>
                </div>
                <p className="text-2xl font-bold text-emerald-600 dark:text-emerald-400 tabular-nums">$ 150.00</p>
                <p className="text-[10px] font-medium text-foreground/65 mt-0.5">
                  {txt("vs OpenAI · 1M tokens/mês", "vs OpenAI · 1M tokens/month")}
                </p>
              </div>
              <div className="rounded-lg border border-indigo-500/25 bg-indigo-500/5 px-4 py-3 text-center">
                <div className="flex items-center justify-center gap-1.5 mb-1">
                  <TrendingDown className="h-3.5 w-3.5 text-indigo-500" />
                  <span className="text-[10px] font-bold uppercase tracking-wider text-indigo-600 dark:text-indigo-400">
                    {txt("Redução de Custo", "Cost Reduction")}
                  </span>
                </div>
                <p className="text-2xl font-bold text-indigo-600 dark:text-indigo-400 tabular-nums">90%</p>
                <p className="text-[10px] font-medium text-foreground/65 mt-0.5">
                  {txt("em relação ao GPT-4o", "compared to GPT-4o")}
                </p>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
