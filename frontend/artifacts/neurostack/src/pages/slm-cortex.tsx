import { Cpu, FlaskConical, TrendingDown, Zap } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { useI18n } from "@/context/i18n";

export type CortexProvider = "gpt" | "dropout" | "cortex";

/** Reference prices in US$ per 1M tokens. This screen is a projection. */
export type CortexPrices = Record<CortexProvider, number>;

export const CORTEX_PRICES_PER_M: CortexPrices = { gpt: 5, dropout: 2.5, cortex: 0.5 };

/** Example volume, in millions of tokens per month. */
export const EXAMPLE_VOLUME_M = 30;

export interface ProviderCost {
  /** US$ per 1M tokens. */
  price: number;
  /** price × volume, in US$ per month. */
  monthlyCost: number;
  /** price ÷ highest price, from 0 to 1. */
  barFraction: number;
}

export interface CostComparison {
  providers: Record<CortexProvider, ProviderCost>;
  /** (gpt − cortex) × volume, in US$ per month. */
  monthlySavings: number;
  /** 1 − cortex ÷ gpt, from 0 to 1. */
  costReduction: number;
}

const PROVIDER_ORDER: readonly CortexProvider[] = ["gpt", "dropout", "cortex"];

export function buildCostComparison(prices: CortexPrices, volumeM: number): CostComparison {
  const maxPrice = Math.max(...PROVIDER_ORDER.map((p) => prices[p]));
  const cost = (p: CortexProvider): ProviderCost => ({
    price: prices[p],
    monthlyCost: prices[p] * volumeM,
    barFraction: maxPrice > 0 ? prices[p] / maxPrice : 0,
  });
  return {
    providers: { gpt: cost("gpt"), dropout: cost("dropout"), cortex: cost("cortex") },
    monthlySavings: (prices.gpt - prices.cortex) * volumeM,
    costReduction: prices.gpt > 0 ? 1 - prices.cortex / prices.gpt : 0,
  };
}

const BAR_STYLE: Record<CortexProvider, { label: string; shortLabel: string; barClass: string; textClass: string }> = {
  gpt: { label: "GPT (OpenAI)", shortLabel: "GPT", barClass: "bg-rose-500/60", textClass: "text-rose-600 dark:text-rose-400" },
  dropout: { label: "Dropout", shortLabel: "Dropout", barClass: "bg-amber-500/60", textClass: "text-amber-600 dark:text-amber-400" },
  cortex: { label: "SLM Cortex", shortLabel: "SLM Cortex", barClass: "bg-emerald-500/70", textClass: "text-emerald-600 dark:text-emerald-400" },
};

const SPECIALTIES = [
  { pt: "Odontologia Estética", en: "Aesthetic Dentistry" },
  { pt: "Dermatologia & Estética", en: "Dermatology & Aesthetics" },
  { pt: "Harmonização Facial", en: "Facial Harmonization" },
  { pt: "Cirurgia Plástica", en: "Plastic Surgery" },
  { pt: "Ortopedia", en: "Orthopedics" },
  { pt: "Oftalmologia", en: "Ophthalmology" },
];

export default function SlmCortex() {
  const { locale } = useI18n();
  const txt = <T,>(p: T, e: T): T => (locale === "en" ? e : p);

  const intlLocale = txt("pt-BR", "en-US");
  const currency = new Intl.NumberFormat(intlLocale, { style: "currency", currency: "USD" });
  const percent = new Intl.NumberFormat(intlLocale, { style: "percent" });
  const comparison = buildCostComparison(CORTEX_PRICES_PER_M, EXAMPLE_VOLUME_M);
  const volume = EXAMPLE_VOLUME_M;

  const exampleLine =
    txt(`Custo mensal com ${volume} milhões de tokens: `, `Monthly cost at ${volume} million tokens: `) +
    PROVIDER_ORDER.map(
      (p) => `${BAR_STYLE[p].shortLabel} ${currency.format(comparison.providers[p].monthlyCost)}`,
    ).join(" · ");

  return (
    <div className="space-y-6 max-w-4xl">
      {/* ── Page header ──────────────────────────────────────────────────────── */}
      <div className="space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex min-w-0 items-center gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-indigo-500/10 border border-indigo-500/20">
              <Cpu className="h-5 w-5 text-indigo-500" />
            </div>
            <div className="min-w-0">
              <h1 className="text-2xl font-bold tracking-tight text-foreground">NeuroStack SLM Cortex</h1>
              <p className="text-sm font-medium text-foreground/70 mt-0.5">
                {txt(
                  "Motor de linguagem próprio da NeuroStack, em desenvolvimento",
                  "NeuroStack's own language engine, in development",
                )}
              </p>
            </div>
          </div>
          <Badge
            variant="outline"
            className="shrink-0 mt-1 bg-indigo-950/60 border-indigo-500/30 text-indigo-300 font-semibold text-[11px] tracking-wide px-3 py-1.5"
          >
            <FlaskConical className="h-3 w-3 mr-1.5" />
            <span>Roadmap · Alpha</span>
          </Badge>
        </div>
        <p className="text-xs font-medium text-foreground/65">
          {txt("Os valores desta tela são uma projeção.", "The figures on this page are a projection.")}
        </p>
      </div>

      {/* ── Cost comparison ──────────────────────────────────────────────────── */}
      <Card className="border-border/60 bg-card">
        <CardHeader className="pb-3">
          <div className="flex items-center gap-2">
            <TrendingDown className="h-4 w-4 shrink-0 text-emerald-500" />
            <CardTitle className="text-sm font-semibold text-foreground">
              {txt("Comparativo de custo por 1M de tokens", "Cost comparison per 1M tokens")}
            </CardTitle>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-3">
            {PROVIDER_ORDER.map((p) => {
              const row = comparison.providers[p];
              const style = BAR_STYLE[p];
              return (
                <div key={p} className="space-y-1.5">
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-xs font-semibold text-foreground/85">{style.label}</span>
                    <span className={`text-xs font-bold tabular-nums ${style.textClass}`}>
                      {currency.format(row.price)}
                    </span>
                  </div>
                  <div className="h-2 w-full rounded-full bg-muted overflow-hidden" aria-hidden="true">
                    <div
                      className={`h-full ${style.barClass} rounded-full`}
                      style={{ width: `${row.barFraction * 100}%` }}
                    />
                  </div>
                </div>
              );
            })}
          </div>

          <div className="grid grid-cols-1 gap-3 pt-1 sm:grid-cols-2">
            <div className="rounded-lg border border-emerald-500/25 bg-emerald-500/5 px-4 py-3 text-center">
              <div className="flex items-center justify-center gap-1.5 mb-1">
                <Zap className="h-3.5 w-3.5 shrink-0 text-emerald-500" />
                <span className="text-[11px] font-bold tracking-wide text-emerald-600 dark:text-emerald-400">
                  {txt("Economia mensal", "Monthly savings")}
                </span>
              </div>
              <p className="text-2xl font-bold text-emerald-600 dark:text-emerald-400 tabular-nums">
                {currency.format(comparison.monthlySavings)}
              </p>
              <p className="text-[11px] font-medium text-foreground/65 mt-0.5">
                {txt(`vs GPT, com ${volume} milhões de tokens/mês`, `vs GPT, at ${volume} million tokens/month`)}
              </p>
            </div>
            <div className="rounded-lg border border-indigo-500/25 bg-indigo-500/5 px-4 py-3 text-center">
              <div className="flex items-center justify-center gap-1.5 mb-1">
                <TrendingDown className="h-3.5 w-3.5 shrink-0 text-indigo-500" />
                <span className="text-[11px] font-bold tracking-wide text-indigo-600 dark:text-indigo-400">
                  {txt("Redução de custo", "Cost reduction")}
                </span>
              </div>
              <p className="text-2xl font-bold text-indigo-600 dark:text-indigo-400 tabular-nums">
                {percent.format(comparison.costReduction)}
              </p>
              <p className="text-[11px] font-medium text-foreground/65 mt-0.5">
                {txt("em relação ao GPT", "compared to GPT")}
              </p>
            </div>
          </div>

          <p className="text-xs font-medium text-foreground/75 tabular-nums">{exampleLine}</p>
        </CardContent>
      </Card>

      {/* ── Fallback ─────────────────────────────────────────────────────────── */}
      <p className="text-sm font-medium text-foreground/80 leading-relaxed">
        {txt(
          "Quando o Cortex não tiver confiança suficiente, a conversa seguirá automaticamente para o GPT ou o Dropout.",
          "When Cortex is not confident enough, the conversation will automatically go to GPT or Dropout.",
        )}
      </p>

      {/* ── Planned specialties ──────────────────────────────────────────────── */}
      <div className="space-y-2">
        <h2 className="text-sm font-semibold text-foreground">
          {txt("Especialidades previstas", "Planned specialties")}
        </h2>
        <div className="flex flex-wrap gap-2">
          {SPECIALTIES.map(({ pt, en }) => (
            <Badge
              key={pt}
              variant="outline"
              className="border-indigo-500/25 bg-indigo-500/5 text-xs font-medium text-foreground/85 px-2.5 py-1"
            >
              {txt(pt, en)}
            </Badge>
          ))}
        </div>
      </div>
    </div>
  );
}
