import {
  Lightbulb,
  TrendingUp,
  Users,
  Sparkles,
  DollarSign,
  Clock,
  FileBarChart2,
  FlaskConical,
  AlertTriangle,
  ArrowRight,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/context/i18n";

function SectionDivider({ num, label }: { num: string; label: string }) {
  return (
    <div className="flex items-center gap-3">
      <div className="flex h-5 w-5 items-center justify-center rounded-full bg-violet-500/20 shrink-0">
        <span className="text-[9px] font-bold text-violet-500">{num}</span>
      </div>
      <span className="text-[10px] font-bold uppercase tracking-widest text-violet-500">{label}</span>
      <div className="flex-1 h-px bg-violet-500/20" />
    </div>
  );
}

function InsightCallout({ icon: Icon, text }: { icon: React.ElementType; text: React.ReactNode }) {
  return (
    <div className="flex items-start gap-3 rounded-lg border border-violet-500/20 bg-violet-500/5 px-4 py-3">
      <Icon className="h-4 w-4 text-violet-500 mt-0.5 shrink-0" />
      <p className="text-sm font-medium text-foreground/85 leading-relaxed">{text}</p>
    </div>
  );
}

export default function Consultoria() {
  const { locale } = useI18n();
  const txt = <T,>(p: T, e: T): T => (locale === "en" ? e : p);

  return (
    <div className="space-y-8 max-w-4xl">

      {/* ── Page header ──────────────────────────────────────────────────────── */}
      <div className="space-y-4">
        <div className="flex items-start justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-violet-500/10 border border-violet-500/20">
              <Lightbulb className="h-5 w-5 text-violet-500" />
            </div>
            <div>
              <h1 className="text-2xl font-bold tracking-tight text-foreground">
                {txt("Consultoria Comercial & Insights", "Commercial Consulting & Insights")}
              </h1>
              <p className="text-sm font-medium text-foreground/70 mt-0.5">
                {txt(
                  "Agente de IA que analisa os dados da sua clínica e sugere ações estratégicas de gestão",
                  "AI agent that analyzes your clinic data and suggests strategic management actions"
                )}
              </p>
            </div>
          </div>
          <Badge
            variant="outline"
            className="shrink-0 mt-1 bg-violet-950/60 border-violet-500/30 text-violet-300 font-semibold text-[10px] uppercase tracking-wider px-3 py-1.5"
          >
            <FlaskConical className="h-3 w-3 mr-1.5" />
            {txt("Roadmap · Em desenvolvimento", "Roadmap · In development")}
          </Badge>
        </div>

        {/* Banner */}
        <div className="rounded-lg border border-indigo-800 bg-indigo-950 px-5 py-4">
          <div className="flex items-center gap-4">
            <Lightbulb className="h-8 w-8 text-white shrink-0" />
            <div className="flex-1">
              <p className="text-sm font-bold text-white">
                {txt("ROADMAP: Em desenvolvimento / IA Analítica", "ROADMAP: In development / Analytical AI")}
              </p>
              <p className="text-xs font-medium text-white/80 mt-0.5 leading-relaxed">
                {txt(
                  "O Business Partner analisa automaticamente suas conversas, taxas de conversão e padrões de comportamento para gerar insights estratégicos personalizados para sua clínica.",
                  "The Business Partner automatically analyzes your conversations, conversion rates and behavior patterns to generate personalized strategic insights for your clinic."
                )}
              </p>
            </div>
            <div className="flex flex-col items-end gap-1 shrink-0 text-right">
              <Badge variant="outline" className="text-[9px] font-bold tracking-widest bg-indigo-900/40 border-indigo-500/30 text-indigo-200 px-2">
                {txt("IA Analítica", "Analytical AI")}
              </Badge>
              <Badge variant="outline" className="text-[9px] font-bold tracking-widest bg-indigo-900/40 border-indigo-500/30 text-indigo-200 px-2">
                {txt("Tempo Real", "Real Time")}
              </Badge>
              <Badge variant="outline" className="text-[9px] font-bold tracking-widest bg-indigo-900/40 border-indigo-500/30 text-indigo-200 px-2">
                {txt("Personalizado", "Personalized")}
              </Badge>
            </div>
          </div>
        </div>

        {/* Stat strip */}
        <div className="grid grid-cols-3 gap-3">
          {[
            { label: txt("Insights Gerados", "Generated Insights"),   value: "847",  note: txt("último trimestre", "last quarter")   },
            { label: txt("Taxa de Adoção", "Adoption Rate"),           value: "92%",  note: txt("clínicas piloto", "pilot clinics")   },
            { label: txt("Aumento de Receita", "Revenue Increase"),    value: "+23%", note: txt("média nos pilotos", "average in pilots") },
          ].map(s => (
            <div key={s.label} className="rounded-lg border border-indigo-800 bg-indigo-950 px-4 py-3 text-center">
              <p className="text-xl font-bold text-white">{s.value}</p>
              <p className="text-[11px] font-semibold text-white/90 mt-0.5">{s.label}</p>
              <p className="text-[10px] font-medium text-indigo-300 mt-0.5">{s.note}</p>
            </div>
          ))}
        </div>
      </div>

      {/* ── Block 1: Bottleneck Detection ────────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="01" label={txt("Analisador de Gargalos (Bottleneck Detection)", "Bottleneck Analyzer (Bottleneck Detection)")} />
        <Card className="border-border/60 bg-card opacity-80">
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <AlertTriangle className="h-4 w-4 text-violet-500" />
              <CardTitle className="text-sm font-semibold text-foreground">
                {txt("Dica de Gestão - Ponto Crítico Identificado", "Management Tip - Critical Point Identified")}
              </CardTitle>
              <Badge variant="outline" className="ml-auto text-[9px] font-bold bg-amber-500/10 border-amber-500/25 text-amber-600 dark:text-amber-400">
                {txt("Alta Prioridade", "High Priority")}
              </Badge>
            </div>
            <p className="text-xs font-medium text-foreground/75 mt-1">
              {txt(
                "Baseado na análise de 1.240 conversas dos últimos 30 dias.",
                "Based on analysis of 1,240 conversations over the last 30 days."
              )}
            </p>
          </CardHeader>
          <CardContent className="space-y-3">
            <InsightCallout
              icon={Lightbulb}
              text={
                <>
                  {txt(
                    <><strong className="text-foreground">Identificamos que 30% das desistências</strong> ocorrem após o envio da tabela de preços. Sugerimos testar o envio de um{" "}<strong className="text-foreground">vídeo explicativo antes do valor</strong> para aumentar o engajamento e reduzir o choque de preço.</>,
                    <><strong className="text-foreground">We identified that 30% of drop-offs</strong> occur after sending the price list. We suggest testing the sending of an{" "}<strong className="text-foreground">explanatory video before the price</strong> to increase engagement and reduce price shock.</>
                  )}
                </>
              }
            />
            <div className="grid grid-cols-3 gap-2 pt-1">
              {[
                { label: txt("Desistências na etapa de preço", "Drop-offs at price stage"), value: "30%",   color: "text-rose-600 dark:text-rose-400"     },
                { label: txt("Conversas analisadas", "Analyzed conversations"),              value: "1.240", color: "text-foreground"                       },
                { label: txt("Potencial de melhora", "Improvement potential"),               value: "+18%",  color: "text-emerald-600 dark:text-emerald-400" },
              ].map(m => (
                <div key={m.label} className="rounded-md border border-border/50 bg-muted/20 px-3 py-2 text-center">
                  <p className={`text-base font-bold tabular-nums ${m.color}`}>{m.value}</p>
                  <p className="text-[10px] font-medium text-foreground/65 mt-0.5 leading-tight">{m.label}</p>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* ── Block 2: Human vs IA Performance ─────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="02" label={txt("Performance de Equipe vs. IA", "Team vs. AI Performance")} />
        <Card className="border-border/60 bg-card opacity-80">
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <Users className="h-4 w-4 text-violet-500" />
              <CardTitle className="text-sm font-semibold text-foreground">
                {txt("Tempo Médio de Resposta", "Average Response Time")}
              </CardTitle>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-3">
              {[
                { label: txt("Equipe Humana", "Human Team"), value: txt("12 min", "12 min"), pct: 100, barClass: "bg-rose-500/60",    textClass: "text-rose-600 dark:text-rose-400"    },
                { label: "NeuroStack IA",                    value: txt("3 seg", "3 sec"),   pct: 0.4, barClass: "bg-emerald-500/70", textClass: "text-emerald-600 dark:text-emerald-400" },
              ].map(r => (
                <div key={r.label} className="space-y-1.5">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-semibold text-foreground/85">{r.label}</span>
                    <span className={`text-xs font-bold tabular-nums ${r.textClass}`}>{r.value}</span>
                  </div>
                  <div className="h-2.5 w-full rounded-full bg-muted overflow-hidden">
                    <div
                      className={`h-full ${r.barClass} rounded-full`}
                      style={{ width: r.pct < 1 ? "2%" : `${r.pct}%` }}
                    />
                  </div>
                </div>
              ))}
            </div>

            <InsightCallout
              icon={TrendingUp}
              text={
                <>
                  {txt(
                    <>Sua <strong className="text-foreground">taxa de conversão sobe 15%</strong> quando o humano intervém em menos de <strong className="text-foreground">5 minutos</strong>. Configure alertas de escalada para sua equipe responder dentro desse janela.</>,
                    <>Your <strong className="text-foreground">conversion rate increases 15%</strong> when a human intervenes within <strong className="text-foreground">5 minutes</strong>. Configure escalation alerts so your team responds within that window.</>
                  )}
                </>
              }
            />
          </CardContent>
        </Card>
      </div>

      {/* ── Block 3: Market Trends ───────────────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="03" label={txt("Detecção de Tendências (Market Trends)", "Trend Detection (Market Trends)")} />
        <Card className="border-border/60 bg-card opacity-80">
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <TrendingUp className="h-4 w-4 text-violet-500" />
              <CardTitle className="text-sm font-semibold text-foreground">
                {txt("Tendências nos seus Chats", "Trends in your Chats")}
              </CardTitle>
              <Badge variant="outline" className="ml-auto text-[9px] font-bold bg-emerald-500/10 border-emerald-500/25 text-emerald-600 dark:text-emerald-400">
                {txt("Novo Procedimento em Alta", "New Procedure Trending")}
              </Badge>
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="rounded-lg border border-violet-500/20 bg-violet-500/5 px-4 py-4 space-y-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-bold text-foreground">
                  {txt("Harmonização Corporal", "Body Harmonization")}
                </span>
                <div className="flex items-center gap-1.5">
                  <TrendingUp className="h-3.5 w-3.5 text-emerald-500" />
                  <span className="text-sm font-bold text-emerald-600 dark:text-emerald-400">+40%</span>
                </div>
              </div>
              <div className="h-1.5 w-full rounded-full bg-muted overflow-hidden">
                <div className="h-full w-[40%] bg-violet-500/70 rounded-full" />
              </div>
              <p className="text-xs font-medium text-foreground/70 leading-relaxed">
                {txt(
                  <><strong className="text-foreground">40%</strong> de aumento nas buscas por este procedimento nos seus chats este mês. Considere criar uma promoção direcionada à sua base de leads.</>,
                  <>A <strong className="text-foreground">40%</strong> increase in searches for this procedure in your chats this month. Consider creating a promotion targeted at your lead base.</>
                )}
              </p>
            </div>

            <div className="grid grid-cols-2 gap-2">
              {[
                { term: txt("Bioestimuladores", "Biostimulators"), pct: "+28%" },
                { term: txt("Peeling Químico", "Chemical Peel"),   pct: "+17%" },
              ].map(t => (
                <div key={t.term} className="flex items-center justify-between rounded-md border border-border/50 bg-muted/20 px-3 py-2">
                  <div className="flex items-center gap-2">
                    <ArrowRight className="h-3 w-3 text-violet-400 shrink-0" />
                    <span className="text-xs font-semibold text-foreground/80">{t.term}</span>
                  </div>
                  <span className="text-xs font-bold text-emerald-600 dark:text-emerald-400">{t.pct}</span>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* ── Block 4: Revenue Forecast ────────────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="04" label={txt("Previsibilidade de Receita (Revenue Forecast)", "Revenue Predictability (Revenue Forecast)")} />
        <Card className="border-border/60 bg-card opacity-80">
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <DollarSign className="h-4 w-4 text-violet-500" />
              <CardTitle className="text-sm font-semibold text-foreground">
                {txt("Oportunidades Detectadas Hoje", "Opportunities Detected Today")}
              </CardTitle>
            </div>
            <p className="text-xs font-medium text-foreground/75 mt-1">
              {txt(
                "Baseado nos chats abertos nas últimas 24h com sinais de intenção de agendamento.",
                "Based on open chats in the last 24h with booking intent signals."
              )}
            </p>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="rounded-lg border border-emerald-500/25 bg-emerald-500/5 px-5 py-4 flex items-center gap-4">
              <div className="flex h-12 w-12 items-center justify-center rounded-full bg-emerald-500/15 border border-emerald-500/25 shrink-0">
                <DollarSign className="h-6 w-6 text-emerald-600 dark:text-emerald-400" />
              </div>
              <div>
                <p className="text-2xl font-bold text-emerald-600 dark:text-emerald-400 tabular-nums">$ 2,800</p>
                <p className="text-xs font-semibold text-foreground/80 mt-0.5">
                  {txt(
                    "em potenciais agendamentos detectados nos chats abertos hoje",
                    "in potential appointments detected in open chats today"
                  )}
                </p>
              </div>
            </div>

            <div className="grid grid-cols-3 gap-2">
              {[
                { label: txt("Chats com intenção", "Chats with intent"),          value: "34",    icon: Sparkles   },
                { label: txt("Ticket médio estimado", "Estimated avg ticket"),     value: "$ 82",  icon: DollarSign },
                { label: txt("Janela de conversão", "Conversion window"),          value: "48h",   icon: Clock      },
              ].map(m => (
                <div key={m.label} className="rounded-md border border-border/50 bg-muted/20 px-3 py-2.5 text-center">
                  <m.icon className="h-3.5 w-3.5 text-violet-500 mx-auto mb-1" />
                  <p className="text-sm font-bold text-foreground tabular-nums">{m.value}</p>
                  <p className="text-[10px] font-medium text-foreground/65 mt-0.5 leading-tight">{m.label}</p>
                </div>
              ))}
            </div>

            <div className="flex items-center gap-3 pt-1">
              <Button
                disabled
                variant="outline"
                size="sm"
                className="gap-2 opacity-50 cursor-not-allowed border-border/60"
              >
                <FileBarChart2 className="h-3.5 w-3.5" />
                {txt("Gerar Relatório Estratégico (Enterprise Only)", "Generate Strategic Report (Enterprise Only)")}
              </Button>
              <span className="text-[11px] font-medium text-foreground/50">
                {txt("Disponível no plano Enterprise", "Available on Enterprise plan")}
              </span>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
