// -- Consultoria Comercial: rule-based insights ---
//
// Pure module: no React hooks and no clock. The page fetches
// `/v1/traces/stats` and `/v1/handoffs/queue`, wraps each answer in a
// RequestResult and draws whatever `buildInsights` returns. Every sentence the
// page shows lives in CONSULTORIA_COPY below (category labels come from the
// locales through `getBucketLabel`).

import { en, type TranslationKey } from "@/locales/en";
import { pt } from "@/locales/pt";
import { MIN_SAMPLE_FOR_PERCENT, formatShare } from "@/lib/honest-number";
import {
  BUCKET_ORDER,
  getBucketLabel,
  getIntentBucket,
  type IntentBucket,
} from "@/lib/intent-presentation";

// -- Request and insight shapes ---

export type RequestResult<T> =
  | { status: "loading" }
  | { status: "success"; data: T }
  | { status: "error"; httpStatus?: number };

export type InsightState = "loading" | "ready" | "empty" | "needs_setup" | "unavailable";

export type InsightId = "outside_hours" | "new_contacts" | "top_intents" | "team_sla";

export interface Insight {
  id: InsightId;
  state: InsightState;
  title: string;
  lines: string[];
  premise: string | null;
  statusMessage: string | null;
  action?: { href: string; label: string };
}

/** The `/v1/traces/stats` fields the Consultoria reads (snake_case, as served). */
export interface ConsultoriaTraceStats {
  business_hours_configured?: boolean;
  outside_hours_count?: number;
  conversational_trace_count?: number;
  new_users_count?: number;
  returning_users_count?: number;
  intent_distribution?: Record<string, number>;
}

/** The `/v1/handoffs/queue` statistics the Consultoria reads. */
export interface ConsultoriaQueueStats {
  within_sla_count: number;
  confirmed_late_count: number;
  unanswered_overdue_count: number;
  maturing_count: number;
  cancelled_unconfirmed_count: number;
  unmeasured_legacy_count: number;
  sla_eligible_count: number;
}

/**
 * What `/v1/handoffs/queue` may answer, as far as the Consultoria is concerned:
 * any count can be missing, and the rest (`items` with patient data, waits)
 * is never read.
 */
export type HandoffQueuePayload = Partial<Record<keyof ConsultoriaQueueStats, number | null>> & Record<string, unknown>;

function countOf(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

/** Only the counts the cards read, so no patient field is ever kept in the cache. */
export function pickQueueStats(payload: HandoffQueuePayload): ConsultoriaQueueStats {
  return {
    within_sla_count: countOf(payload.within_sla_count),
    confirmed_late_count: countOf(payload.confirmed_late_count),
    unanswered_overdue_count: countOf(payload.unanswered_overdue_count),
    maturing_count: countOf(payload.maturing_count),
    cancelled_unconfirmed_count: countOf(payload.cancelled_unconfirmed_count),
    unmeasured_legacy_count: countOf(payload.unmeasured_legacy_count),
    sla_eligible_count: countOf(payload.sla_eligible_count),
  };
}

export type ConsultoriaLocale = "pt" | "en";

// -- Premise thresholds ---
//
// The owner's commercial judgment, not a calibrated statistic: prod has no
// real traffic yet. Compared on the unrounded ratio, and equality picks
// sentence A. Re-read both sentences of a card when changing its limit.

export const OUTSIDE_HOURS_HIGH_PCT = 30;
export const NEW_CONTACTS_HIGH_PCT = 60;
export const SLA_GOOD_PCT = 80;

/** `x / n * 100 >= limit`, exact in integers (29 of 100 against 29 is true). */
export function meetsThreshold(x: number, n: number, limit: number): boolean {
  return x * 100 >= limit * n;
}

/**
 * Every handoff in the selected cohort falls into exactly one `SlaCategory`
 * (`src/handoff/sla.py`), so the cohort total is the sum of the six category
 * counts. This copies the backend partition: a seventh category would make it
 * undercount. Follow-up: serve the cohort total on the queue response.
 */
export function handoffCohortTotal(queue: ConsultoriaQueueStats): number {
  return (
    queue.within_sla_count +
    queue.confirmed_late_count +
    queue.unanswered_overdue_count +
    queue.maturing_count +
    queue.cancelled_unconfirmed_count +
    queue.unmeasured_legacy_count
  );
}

// -- Copy table ---

type NamedBucket = Exclude<IntentBucket, "outras">;

export const CONSULTORIA_COPY = {
  pt: {
    page: {
      title: "Consultoria Comercial",
      subtitle: "Leituras comerciais a partir dos dados reais da sua clínica.",
      noClinic: "Selecione uma clínica para ver a consultoria.",
    },
    unavailable: {
      generic: "Não foi possível carregar este insight agora. Tente de novo em instantes.",
      period: "Período inválido para este insight. Escolha um intervalo de até 90 dias.",
    },
    cards: {
      outsideHours: {
        title: "Fora do horário comercial",
        line: "Mensagens processadas pela IA fora do horário comercial: {share}",
        premiseA: "Boa parte da sua demanda chega com a recepção fechada; é nesse horário que a resposta imediata mais pesa.",
        premiseB: "A maior parte da sua demanda chega no horário comercial; vale garantir equipe disponível nesses horários.",
        premiseNeutral: "Este card mostra quanto da sua demanda chega fora do horário comercial.",
        needsSetup: "Configure o horário comercial da clínica para ver este insight.",
        needsSetupAction: "Configurar horário",
        empty: "Nenhuma mensagem processada pela IA no período.",
      },
      newContacts: {
        title: "Contatos novos x recorrentes",
        counts: "Novos: {new} · Recorrentes: {ret}",
        line: "Contatos novos no período: {share}",
        qualifier: "Novo = primeiro contato registrado na NeuroStack dentro do período.",
        premiseA: "A maior parte dos contatos aparece pela primeira vez na NeuroStack. Nas primeiras semanas de uso isso é esperado, porque o histórico ainda está começando.",
        premiseB: "Boa parte dos contatos já tinha falado com a clínica pela NeuroStack antes; vale cuidar bem de quem volta.",
        premiseNeutral: "Este card mostra quanto da sua demanda vem de contatos novos e de quem volta.",
        empty: "Nenhum contato identificado no período.",
      },
      topIntents: {
        title: "O que mais procuram",
        header: "Mensagens recebidas no período: {n}",
        row: "{label}: {share}",
        premiseByBucket: {
          pedido_preco: "Preço é o assunto que mais aparece: vale revisar como a IA apresenta valores e condições.",
          agendar: "Agendamento é o assunto que mais aparece: horários claros e fáceis de marcar fazem diferença.",
          interesse_tratamento: "Interesse em tratamento é o assunto que mais aparece: mídias e explicações de cada procedimento ajudam a avançar.",
          reclamacao: "Reclamação é o assunto que mais aparece: vale olhar essas mensagens com a equipe antes de qualquer outra coisa.",
          pos_atendimento_duvida: "Dúvidas pós-procedimento são o assunto que mais aparece: orientações de cuidado enviadas antes ajudam a reduzir essas mensagens.",
        } satisfies Record<NamedBucket, string>,
        premiseNeutral: "Este card aponta o assunto que mais aparece nas mensagens e onde vale caprichar primeiro.",
        empty: "Nenhuma mensagem recebida no período.",
        emptyOnlyOther: "Mensagens recebidas no período: {n}, nenhuma nas categorias principais.",
      },
      teamSla: {
        title: "Equipe humana e meta",
        total: "Transferências para a equipe no período: {total}",
        sla: "Atendimentos elegíveis dentro da meta: {share}",
        slaNoEligible: "Nenhuma transferência do período pôde ser medida contra a meta.",
        premiseA: "A equipe assume a grande maioria dos atendimentos elegíveis dentro da meta; mantenha esse ritmo na Fila.",
        premiseB: "Uma parte relevante dos atendimentos elegíveis passa da meta; vale revisar quem cuida da Fila e em quais horários.",
        premiseNeutral: "Este card mostra quanto dos atendimentos elegíveis a equipe assume dentro da meta.",
        empty: "Nenhuma transferência para a equipe no período.",
      },
    },
    comingSoon: {
      badge: "Em breve",
      items: [
        { name: "Analisador de gargalos", description: "onde os pacientes param de responder ao longo do atendimento, para você saber o que ajustar." },
        { name: "Tendências de procedimentos", description: "os procedimentos que estão ganhando espaço nas mensagens do mês." },
        { name: "Previsão de receita", description: "estimativa de agendamentos e receita a partir das mensagens em aberto." },
        { name: "Relatório estratégico", description: "um relatório periódico com as recomendações da Consultoria para a sua clínica." },
      ],
    },
  },
  en: {
    page: {
      title: "Commercial Consulting",
      subtitle: "Commercial readings from your clinic's real data.",
      noClinic: "Select a clinic to see the consulting.",
    },
    unavailable: {
      generic: "Couldn't load this insight right now. Try again shortly.",
      period: "Invalid period for this insight. Pick a range of up to 90 days.",
    },
    cards: {
      outsideHours: {
        title: "Outside business hours",
        line: "Messages processed by the AI outside business hours: {share}",
        premiseA: "A large share of your demand arrives while the front desk is closed; that is when an immediate reply matters most.",
        premiseB: "Most of your demand arrives during business hours; make sure the team is available at those times.",
        premiseNeutral: "This card shows how much of your demand arrives outside business hours.",
        needsSetup: "Set the clinic's business hours to see this insight.",
        needsSetupAction: "Set business hours",
        empty: "No messages processed by the AI in this period.",
      },
      newContacts: {
        title: "New vs returning contacts",
        counts: "New: {new} · Returning: {ret}",
        line: "New contacts in this period: {share}",
        qualifier: "New = first contact recorded in NeuroStack within the period.",
        premiseA: "Most contacts appear for the first time in NeuroStack. In the first weeks of use this is expected, because the history is just starting.",
        premiseB: "A good share of contacts had already talked to the clinic through NeuroStack before; it pays to look after those who come back.",
        premiseNeutral: "This card shows how much of your demand comes from new contacts and from people who return.",
        empty: "No identified contacts in this period.",
      },
      topIntents: {
        title: "What they ask about most",
        header: "Messages received in this period: {n}",
        row: "{label}: {share}",
        premiseByBucket: {
          pedido_preco: "Price is the top subject: review how the AI presents prices and terms.",
          agendar: "Scheduling is the top subject: clear, easy-to-book time slots make a difference.",
          interesse_tratamento: "Treatment interest is the top subject: media and explanations for each procedure help move things forward.",
          reclamacao: "Complaints are the top subject: review those messages with the team before anything else.",
          pos_atendimento_duvida: "Post-procedure questions are the top subject: care instructions sent in advance help reduce these messages.",
        } satisfies Record<NamedBucket, string>,
        premiseNeutral: "This card points to the most frequent subject in messages and where to polish first.",
        empty: "No messages received in this period.",
        emptyOnlyOther: "Messages received in this period: {n}, none in the main categories.",
      },
      teamSla: {
        title: "Human team and SLA",
        total: "Handoffs to the team in this period: {total}",
        sla: "Eligible handoffs within SLA: {share}",
        slaNoEligible: "No handoff in this period could be measured against the SLA.",
        premiseA: "The team takes over the large majority of eligible handoffs within the SLA; keep that pace in the Queue.",
        premiseB: "A relevant share of eligible handoffs misses the SLA; review who covers the Queue and at which times.",
        premiseNeutral: "This card shows how many eligible handoffs the team takes over within the SLA.",
        empty: "No handoffs to the team in this period.",
      },
    },
    comingSoon: {
      badge: "Coming soon",
      items: [
        { name: "Bottleneck analyzer", description: "where patients stop replying along the way, so you know what to adjust." },
        { name: "Procedure trends", description: "the procedures gaining ground in this month's messages." },
        { name: "Revenue forecast", description: "an estimate of appointments and revenue from open messages." },
        { name: "Strategic report", description: "a periodic report with Consulting's recommendations for your clinic." },
      ],
    },
  },
} as const;

type CopyLocale = (typeof CONSULTORIA_COPY)[ConsultoriaLocale];

// -- Helpers ---

function copyFor(locale: string): CopyLocale {
  return locale === "en" ? CONSULTORIA_COPY.en : CONSULTORIA_COPY.pt;
}

function fill(template: string, values: Record<string, string>): string {
  return template.replace(/\{(\w+)\}/g, (match, key: string) => values[key] ?? match);
}

function formatCount(value: number, locale: string): string {
  return value.toLocaleString(locale === "en" ? "en-US" : "pt-BR");
}

/** Consultoria's `{share}`: "30% (30 de 100)" from N = 20, "3 de 10" below. */
function shareText(x: number, n: number, locale: string): string {
  const share = formatShare(x, n, locale);
  if (!share) return "";
  return share.percent !== null ? `${share.percent} (${share.fraction})` : share.fraction;
}

function unavailableMessage(httpStatus: number | undefined, copy: CopyLocale): string {
  return httpStatus === 400 || httpStatus === 422 ? copy.unavailable.period : copy.unavailable.generic;
}

type PendingResult = Exclude<RequestResult<never>, { status: "success" }>;

/** Loading and error states, which do not depend on the payload: premise is neutral. */
function pendingInsight(
  id: InsightId,
  result: PendingResult,
  title: string,
  premiseNeutral: string,
  copy: CopyLocale,
): Insight {
  if (result.status === "loading") {
    return { id, state: "loading", title, lines: [], premise: premiseNeutral, statusMessage: null };
  }
  return {
    id,
    state: "unavailable",
    title,
    lines: [],
    premise: premiseNeutral,
    statusMessage: unavailableMessage(result.httpStatus, copy),
  };
}

function aOrB(x: number, n: number, limit: number, card: { premiseA: string; premiseB: string; premiseNeutral: string }): string {
  if (n < MIN_SAMPLE_FOR_PERCENT) return card.premiseNeutral;
  return meetsThreshold(x, n, limit) ? card.premiseA : card.premiseB;
}

// -- Cards ---

function outsideHoursInsight(stats: RequestResult<ConsultoriaTraceStats>, locale: string, copy: CopyLocale): Insight {
  const card = copy.cards.outsideHours;
  if (stats.status !== "success") return pendingInsight("outside_hours", stats, card.title, card.premiseNeutral, copy);
  const base = { id: "outside_hours" as const, title: card.title, premise: card.premiseNeutral };

  if (stats.data.business_hours_configured !== true) {
    return {
      ...base,
      state: "needs_setup",
      lines: [],
      statusMessage: card.needsSetup,
      action: { href: "/config", label: card.needsSetupAction },
    };
  }
  const x = stats.data.outside_hours_count ?? 0;
  const n = stats.data.conversational_trace_count ?? 0;
  if (n <= 0) return { ...base, state: "empty", lines: [], statusMessage: card.empty };

  return {
    ...base,
    state: "ready",
    lines: [fill(card.line, { share: shareText(x, n, locale) })],
    premise: aOrB(x, n, OUTSIDE_HOURS_HIGH_PCT, card),
    statusMessage: null,
  };
}

function newContactsInsight(stats: RequestResult<ConsultoriaTraceStats>, locale: string, copy: CopyLocale): Insight {
  const card = copy.cards.newContacts;
  if (stats.status !== "success") return pendingInsight("new_contacts", stats, card.title, card.premiseNeutral, copy);
  const base = { id: "new_contacts" as const, title: card.title, premise: card.premiseNeutral };

  const fresh = stats.data.new_users_count ?? 0;
  const returning = stats.data.returning_users_count ?? 0;
  const n = fresh + returning;
  if (n <= 0) return { ...base, state: "empty", lines: [], statusMessage: card.empty };

  return {
    ...base,
    state: "ready",
    lines: [
      fill(card.counts, { new: formatCount(fresh, locale), ret: formatCount(returning, locale) }),
      fill(card.line, { share: shareText(fresh, n, locale) }),
      card.qualifier,
    ],
    premise: aOrB(fresh, n, NEW_CONTACTS_HIGH_PCT, card),
    statusMessage: null,
  };
}

function topIntentsInsight(stats: RequestResult<ConsultoriaTraceStats>, locale: string, copy: CopyLocale): Insight {
  const card = copy.cards.topIntents;
  if (stats.status !== "success") return pendingInsight("top_intents", stats, card.title, card.premiseNeutral, copy);
  const base = { id: "top_intents" as const, title: card.title, premise: card.premiseNeutral };

  const counts: Record<IntentBucket, number> = {
    pedido_preco: 0,
    agendar: 0,
    interesse_tratamento: 0,
    reclamacao: 0,
    pos_atendimento_duvida: 0,
    outras: 0,
  };
  for (const [rawKey, count] of Object.entries(stats.data.intent_distribution ?? {})) {
    counts[getIntentBucket(rawKey)] += count;
  }
  // Same denominator as the Dashboard: every bucket, `outras` included.
  const n = BUCKET_ORDER.reduce((sum, bucket) => sum + counts[bucket], 0);
  if (n <= 0) return { ...base, state: "empty", lines: [], statusMessage: card.empty };

  const ranked = BUCKET_ORDER
    .filter((bucket): bucket is NamedBucket => bucket !== "outras" && counts[bucket] > 0)
    .map((bucket) => ({ bucket, count: counts[bucket], order: BUCKET_ORDER.indexOf(bucket) }))
    .sort((a, b) => b.count - a.count || a.order - b.order);
  if (ranked.length === 0) {
    return { ...base, state: "empty", lines: [], statusMessage: fill(card.emptyOnlyOther, { n: formatCount(n, locale) }) };
  }

  const dictionary = locale === "en" ? en : pt;
  const t = (key: TranslationKey) => dictionary[key];
  const rows = ranked.slice(0, 3).map(({ bucket, count }) =>
    fill(card.row, { label: getBucketLabel(bucket, t), share: shareText(count, n, locale) }),
  );
  const [top, second] = ranked;
  const uniqueTop = !second || second.count < top.count;
  const premise = top.count >= MIN_SAMPLE_FOR_PERCENT && uniqueTop
    ? card.premiseByBucket[top.bucket]
    : card.premiseNeutral;

  return {
    ...base,
    state: "ready",
    lines: [fill(card.header, { n: formatCount(n, locale) }), ...rows],
    premise,
    statusMessage: null,
  };
}

function teamSlaInsight(queue: RequestResult<ConsultoriaQueueStats>, locale: string, copy: CopyLocale): Insight {
  const card = copy.cards.teamSla;
  if (queue.status !== "success") return pendingInsight("team_sla", queue, card.title, card.premiseNeutral, copy);
  const base = { id: "team_sla" as const, title: card.title, premise: card.premiseNeutral };

  const total = handoffCohortTotal(queue.data);
  if (total <= 0) return { ...base, state: "empty", lines: [], statusMessage: card.empty };

  const within = queue.data.within_sla_count;
  const eligible = queue.data.sla_eligible_count;
  const totalLine = fill(card.total, { total: formatCount(total, locale) });
  if (eligible <= 0) {
    return { ...base, state: "ready", lines: [totalLine, card.slaNoEligible], statusMessage: null };
  }
  return {
    ...base,
    state: "ready",
    lines: [totalLine, fill(card.sla, { share: shareText(within, eligible, locale) })],
    premise: aOrB(within, eligible, SLA_GOOD_PCT, card),
    statusMessage: null,
  };
}

// -- Entry point ---

/** The four Consultoria insights, in page order, with lines and premise resolved. */
export function buildInsights(
  statsResult: RequestResult<ConsultoriaTraceStats>,
  queueResult: RequestResult<ConsultoriaQueueStats>,
  locale: string,
): Insight[] {
  const copy = copyFor(locale);
  return [
    outsideHoursInsight(statsResult, locale, copy),
    newContactsInsight(statsResult, locale, copy),
    topIntentsInsight(statsResult, locale, copy),
    teamSlaInsight(queueResult, locale, copy),
  ];
}
