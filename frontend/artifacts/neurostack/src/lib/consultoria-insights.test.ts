import { describe, expect, it } from "vitest";

import { BUCKET_ORDER } from "@/lib/intent-presentation";
import { en as enLocale } from "@/locales/en";
import { pt as ptLocale } from "@/locales/pt";
import {
  CONSULTORIA_COPY,
  NEW_CONTACTS_HIGH_PCT,
  OUTSIDE_HOURS_HIGH_PCT,
  SLA_GOOD_PCT,
  buildInsights,
  handoffCohortTotal,
  meetsThreshold,
  pickQueueStats,
  type ConsultoriaQueueStats,
  type ConsultoriaTraceStats,
  type HandoffQueuePayload,
  type Insight,
  type RequestResult,
} from "./consultoria-insights";
import moduleSource from "./consultoria-insights.ts?raw";

const LOCALES = ["pt", "en"] as const;
type Locale = (typeof LOCALES)[number];

const EMPTY_QUEUE: ConsultoriaQueueStats = {
  within_sla_count: 0,
  confirmed_late_count: 0,
  unanswered_overdue_count: 0,
  maturing_count: 0,
  cancelled_unconfirmed_count: 0,
  unmeasured_legacy_count: 0,
  sla_eligible_count: 0,
};

const BASE_STATS: ConsultoriaTraceStats = {
  business_hours_configured: true,
  outside_hours_count: 30,
  conversational_trace_count: 100,
  new_users_count: 12,
  returning_users_count: 8,
  intent_distribution: { pedido_preco: 30, agendar: 10, saudacao: 5 },
};

const loading = { status: "loading" } as const;
const ok = <T,>(data: T): RequestResult<T> => ({ status: "success", data });
const fail = (httpStatus?: number): RequestResult<never> => ({ status: "error", httpStatus });

function insights(
  stats: RequestResult<ConsultoriaTraceStats>,
  queue: RequestResult<ConsultoriaQueueStats> = ok(EMPTY_QUEUE),
  locale: Locale = "pt",
): Insight[] {
  return buildInsights(stats, queue, locale);
}

function card(list: Insight[], id: Insight["id"]): Insight {
  const found = list.find((insight) => insight.id === id);
  if (!found) throw new Error(`missing insight ${id}`);
  return found;
}

const stats = (overrides: Partial<ConsultoriaTraceStats>) => ok({ ...BASE_STATS, ...overrides });
const queue = (overrides: Partial<ConsultoriaQueueStats>) => ok({ ...EMPTY_QUEUE, ...overrides });

const pt = CONSULTORIA_COPY.pt.cards;
const en = CONSULTORIA_COPY.en.cards;

describe("buildInsights shape", () => {
  it("returns the four insights in page order", () => {
    expect(insights(ok(BASE_STATS)).map((i) => i.id)).toEqual([
      "outside_hours",
      "new_contacts",
      "top_intents",
      "team_sla",
    ]);
  });
});

describe("card 1: outside business hours", () => {
  it("asks for setup with a /config link when hours are not configured, even with 0 messages", () => {
    for (const overrides of [
      { business_hours_configured: false, conversational_trace_count: 0, outside_hours_count: 0 },
      { business_hours_configured: false },
      { business_hours_configured: undefined },
    ]) {
      const insight = card(insights(stats(overrides)), "outside_hours");
      expect(insight.state).toBe("needs_setup");
      expect(insight.action?.href).toBe("/config");
      expect(insight.action?.label).toBe(pt.outsideHours.needsSetupAction);
      expect(insight.statusMessage).toBe(pt.outsideHours.needsSetup);
      expect(insight.lines).toEqual([]);
      expect(insight.premise).toBe(pt.outsideHours.premiseNeutral);
    }
  });

  it("is empty when configured with 0 messages", () => {
    const insight = card(insights(stats({ conversational_trace_count: 0, outside_hours_count: 0 })), "outside_hours");
    expect(insight.state).toBe("empty");
    expect(insight.statusMessage).toBe(pt.outsideHours.empty);
    expect(insight.premise).toBe(pt.outsideHours.premiseNeutral);
  });

  it("renders the share from counts in pt and en", () => {
    expect(card(insights(ok(BASE_STATS)), "outside_hours").lines).toEqual([
      "Mensagens processadas pela IA fora do horário comercial: 30% (30 de 100)",
    ]);
    expect(card(insights(ok(BASE_STATS), ok(EMPTY_QUEUE), "en"), "outside_hours").lines).toEqual([
      "Messages processed by the AI outside business hours: 30% (30 of 100)",
    ]);
  });

  it("computes the percentage from counts, never from outside_hours_pct", () => {
    const withLyingPct = ok({ ...BASE_STATS, outside_hours_pct: 99 } as ConsultoriaTraceStats);
    const insight = card(insights(withLyingPct), "outside_hours");
    expect(insight.lines[0]).toContain("30% (30 de 100)");
    expect(insight.lines.join(" ")).not.toContain("99");
  });

  it("shows only the fraction below 20 messages", () => {
    const insight = card(insights(stats({ outside_hours_count: 3, conversational_trace_count: 10 })), "outside_hours");
    expect(insight.lines).toEqual(["Mensagens processadas pela IA fora do horário comercial: 3 de 10"]);
  });
});

describe("card 2: new vs returning contacts", () => {
  it("is empty when there are no identified contacts", () => {
    const insight = card(insights(stats({ new_users_count: 0, returning_users_count: 0 })), "new_contacts");
    expect(insight.state).toBe("empty");
    expect(insight.statusMessage).toBe(pt.newContacts.empty);
    expect(insight.lines).toEqual([]);
  });

  it.each(LOCALES)("always carries the qualifier with numbers (%s)", (locale) => {
    for (const overrides of [
      { new_users_count: 1, returning_users_count: 0 },
      { new_users_count: 12, returning_users_count: 8 },
      { new_users_count: 0, returning_users_count: 40 },
    ]) {
      const insight = card(insights(stats(overrides), ok(EMPTY_QUEUE), locale), "new_contacts");
      expect(insight.state).toBe("ready");
      expect(insight.lines).toContain(CONSULTORIA_COPY[locale].cards.newContacts.qualifier);
    }
  });

  it("shows counts and the new-contacts share", () => {
    const insight = card(insights(stats({ new_users_count: 1240, returning_users_count: 760 })), "new_contacts");
    expect(insight.lines).toEqual([
      "Novos: 1.240 · Recorrentes: 760",
      "Contatos novos no período: 62% (1.240 de 2.000)",
      pt.newContacts.qualifier,
    ]);
  });
});

describe("card 3: what they ask about most", () => {
  const top = (distribution: Record<string, number>, locale: Locale = "pt") =>
    card(insights(stats({ intent_distribution: distribution }), ok(EMPTY_QUEUE), locale), "top_intents");

  it("is empty with the N = 0 phrase", () => {
    for (const distribution of [{}, { pedido_preco: 0 }]) {
      const insight = top(distribution);
      expect(insight.state).toBe("empty");
      expect(insight.statusMessage).toBe(pt.topIntents.empty);
    }
  });

  it("is empty with the only-outras phrase when nothing lands in a named bucket", () => {
    const insight = top({ unknown: 5 });
    expect(insight.state).toBe("empty");
    expect(insight.statusMessage).toBe("Mensagens recebidas no período: 5, nenhuma nas categorias principais.");
  });

  it("ranks at most 3 named buckets by count, never outras, over the 6-bucket total", () => {
    // outras = saudacao 50 + faq 10 = 60, the largest bucket. N = 60 + 30 + 25 + 20 + 5 = 140.
    const insight = top({ saudacao: 50, faq: 10, agendar: 25, pedido_preco: 30, reclamacao: 5, cancelar: 0, reagendar: 0, comparacao_tratamentos: 20 });
    expect(insight.state).toBe("ready");
    expect(insight.lines).toEqual([
      "Mensagens recebidas no período: 140",
      "Pedido de Preço: 21% (30 de 140)",
      `${ptLocale.intent_agendar}: 18% (25 de 140)`,
      `${ptLocale.intent_interesse_tratamento}: 14% (20 de 140)`,
    ]);
    expect(insight.lines.join(" ")).not.toContain("Outras");
  });

  it("shows exactly 2 rows when only 2 named buckets are non-zero", () => {
    const insight = top({ reclamacao: 3, pos_atendimento_duvida: 2, saudacao: 9 });
    expect(insight.lines).toHaveLength(3);
    expect(insight.lines.slice(1)).toEqual([
      `${ptLocale.intent_reclamacao}: 3 de 14`,
      `${ptLocale.intent_pos_atendimento_duvida}: 2 de 14`,
    ]);
  });

  it("sorts by count, not by BUCKET_ORDER or by key order", () => {
    // Counts run against BUCKET_ORDER, and the keys arrive in yet another order.
    const insight = top({ agendar: 10, reclamacao: 30, saudacao: 5, pedido_preco: 20 });
    expect(insight.lines.slice(1).map((line) => line.split(":")[0])).toEqual([
      ptLocale.intent_reclamacao,
      ptLocale.intent_pedido_preco,
      ptLocale.intent_agendar,
    ]);
  });

  it("breaks a tie with BUCKET_ORDER whatever the key order", () => {
    const insight = top({ pos_atendimento_duvida: 7, reclamacao: 7, interesse_tratamento: 7, agendar: 3, pedido_preco: 2 });
    expect(insight.lines.slice(1).map((line) => line.split(":")[0])).toEqual([
      ptLocale.intent_interesse_tratamento,
      ptLocale.intent_reclamacao,
      ptLocale.intent_pos_atendimento_duvida,
    ]);
  });

  it("breaks ties with the imported BUCKET_ORDER", () => {
    const distribution = Object.fromEntries(BUCKET_ORDER.filter((b) => b !== "outras").map((b) => [b, 7]));
    const insight = top(distribution);
    const expectedLabels = [ptLocale.intent_pedido_preco, ptLocale.intent_agendar, ptLocale.intent_interesse_tratamento];
    expect(insight.lines.slice(1).map((line) => line.split(":")[0])).toEqual(expectedLabels);
  });

  it("uses the English bucket labels in en", () => {
    expect(top({ pedido_preco: 4 }, "en").lines).toEqual([
      "Messages received in this period: 4",
      `${enLocale.intent_pedido_preco}: 4 of 4`,
    ]);
  });

  it.each(BUCKET_ORDER.filter((b) => b !== "outras"))("picks the %s sentence when it is a unique top with 20 or more", (bucket) => {
    const other = bucket === "agendar" ? "pedido_preco" : "agendar";
    const insight = top({ [bucket]: 20, [other]: 19, saudacao: 40 });
    expect(insight.premise).toBe(pt.topIntents.premiseByBucket[bucket as keyof typeof pt.topIntents.premiseByBucket]);
  });

  it("stays neutral with a top count of 19", () => {
    expect(top({ pedido_preco: 19, saudacao: 100 }).premise).toBe(pt.topIntents.premiseNeutral);
  });

  it("stays neutral on a tie at the top, even at 20 or more", () => {
    expect(top({ pedido_preco: 25, agendar: 25 }).premise).toBe(pt.topIntents.premiseNeutral);
  });
});

describe("card 4: human team and SLA", () => {
  const sla = (overrides: Partial<ConsultoriaQueueStats> | RequestResult<ConsultoriaQueueStats>, locale: Locale = "pt") =>
    card(
      insights(ok(BASE_STATS), "status" in overrides ? (overrides as RequestResult<ConsultoriaQueueStats>) : queue(overrides as Partial<ConsultoriaQueueStats>), locale),
      "team_sla",
    );

  it("totals the six SLA category counts", () => {
    const counts = {
      within_sla_count: 1,
      confirmed_late_count: 2,
      unanswered_overdue_count: 3,
      maturing_count: 4,
      cancelled_unconfirmed_count: 5,
      unmeasured_legacy_count: 6,
      sla_eligible_count: 999,
    };
    expect(handoffCohortTotal(counts)).toBe(21);
    expect(sla({ ...counts, sla_eligible_count: 6 }).lines[0]).toBe("Transferências para a equipe no período: 21");
  });

  it("shows the percent and fraction from 20 eligible", () => {
    const insight = sla({ within_sla_count: 20, confirmed_late_count: 3, unanswered_overdue_count: 2, sla_eligible_count: 25 });
    expect(insight.state).toBe("ready");
    expect(insight.lines).toEqual([
      "Transferências para a equipe no período: 25",
      "Atendimentos elegíveis dentro da meta: 80% (20 de 25)",
    ]);
  });

  it("shows only the fraction below 20 eligible", () => {
    const insight = sla({ within_sla_count: 4, confirmed_late_count: 1, sla_eligible_count: 5 });
    expect(insight.lines[1]).toBe("Atendimentos elegíveis dentro da meta: 4 de 5");
  });

  it("is empty with all six counts at 0", () => {
    const insight = sla({});
    expect(insight.state).toBe("empty");
    expect(insight.statusMessage).toBe(pt.teamSla.empty);
  });

  it("is ready with the total and the not-measured line when only cancelled", () => {
    const insight = sla({ cancelled_unconfirmed_count: 3 });
    expect(insight.state).toBe("ready");
    expect(insight.lines).toEqual(["Transferências para a equipe no período: 3", pt.teamSla.slaNoEligible]);
    expect(insight.premise).toBe(pt.teamSla.premiseNeutral);
  });

  it("never shows a wait line", () => {
    const states = [
      sla({ within_sla_count: 20, sla_eligible_count: 20 }),
      sla({ cancelled_unconfirmed_count: 1 }),
      sla({}),
      sla(fail(500)),
      sla(loading),
    ];
    for (const insight of states) {
      const text = [...insight.lines, insight.statusMessage ?? ""].join(" ").toLowerCase();
      expect(text).not.toMatch(/espera|tempo|wait|min\b|seg\b/);
    }
  });

  it.each([400, 422])("shows the period message on %s", (httpStatus) => {
    const insight = sla(fail(httpStatus));
    expect(insight.state).toBe("unavailable");
    expect(insight.statusMessage).toBe(CONSULTORIA_COPY.pt.unavailable.period);
  });

  it.each([401, 403, 500, undefined])("shows the generic message on %s", (httpStatus) => {
    const insight = sla(fail(httpStatus));
    expect(insight.state).toBe("unavailable");
    expect(insight.statusMessage).toBe(CONSULTORIA_COPY.pt.unavailable.generic);
  });
});

describe("isolation and loading", () => {
  it("a stats error leaves cards 1-3 unavailable and card 4 untouched", () => {
    const list = insights(fail(500), queue({ within_sla_count: 4, sla_eligible_count: 5 }));
    for (const id of ["outside_hours", "new_contacts", "top_intents"] as const) {
      expect(card(list, id).state).toBe("unavailable");
      expect(card(list, id).statusMessage).toBe(CONSULTORIA_COPY.pt.unavailable.generic);
    }
    expect(card(list, "team_sla").state).toBe("ready");
  });

  it.each([400, 422])("a stats %s gives cards 1-3 the period message", (httpStatus) => {
    const list = insights(fail(httpStatus));
    for (const id of ["outside_hours", "new_contacts", "top_intents"] as const) {
      expect(card(list, id).statusMessage).toBe(CONSULTORIA_COPY.pt.unavailable.period);
    }
  });

  it("a queue error affects card 4 only", () => {
    const list = insights(ok(BASE_STATS), fail(503));
    expect(card(list, "team_sla").state).toBe("unavailable");
    for (const id of ["outside_hours", "new_contacts", "top_intents"] as const) {
      expect(card(list, id).state).toBe("ready");
    }
  });

  it("loading marks only the dependent cards, with the neutral premise", () => {
    const statsLoading = insights(loading, queue({ within_sla_count: 1, sla_eligible_count: 1 }));
    expect(statsLoading.map((i) => i.state)).toEqual(["loading", "loading", "loading", "ready"]);
    expect(card(statsLoading, "outside_hours").premise).toBe(pt.outsideHours.premiseNeutral);
    expect(card(statsLoading, "new_contacts").premise).toBe(pt.newContacts.premiseNeutral);
    expect(card(statsLoading, "top_intents").premise).toBe(pt.topIntents.premiseNeutral);
    expect(statsLoading[0].lines).toEqual([]);
    expect(statsLoading[0].statusMessage).toBeNull();

    const queueLoading = insights(ok(BASE_STATS), loading);
    expect(queueLoading.map((i) => i.state)).toEqual(["ready", "ready", "ready", "loading"]);
    expect(card(queueLoading, "team_sla").premise).toBe(pt.teamSla.premiseNeutral);
  });
});

describe("premise rules", () => {
  it("compares in exact integers", () => {
    expect(meetsThreshold(29, 100, 29)).toBe(true);
    expect(meetsThreshold(28, 100, 29)).toBe(false);
    expect(meetsThreshold(1, 3, 33)).toBe(true);
    expect(meetsThreshold(1, 3, 34)).toBe(false);
  });

  it("ships the owner's thresholds", () => {
    expect([OUTSIDE_HOURS_HIGH_PCT, NEW_CONTACTS_HIGH_PCT, SLA_GOOD_PCT]).toEqual([30, 60, 80]);
  });

  // Each case is (x, n) at a denominator of 20 or more: above, equal, below, then n = 19.
  const cases = [
    {
      name: "card 1",
      id: "outside_hours" as const,
      limit: OUTSIDE_HOURS_HIGH_PCT,
      copy: pt.outsideHours,
      build: (x: number, n: number) => insights(stats({ outside_hours_count: x, conversational_trace_count: n })),
    },
    {
      name: "card 2",
      id: "new_contacts" as const,
      limit: NEW_CONTACTS_HIGH_PCT,
      copy: pt.newContacts,
      build: (x: number, n: number) => insights(stats({ new_users_count: x, returning_users_count: n - x })),
    },
    {
      name: "card 4",
      id: "team_sla" as const,
      limit: SLA_GOOD_PCT,
      copy: pt.teamSla,
      build: (x: number, n: number) =>
        insights(ok(BASE_STATS), queue({ within_sla_count: x, confirmed_late_count: n - x, sla_eligible_count: n })),
    },
  ];

  it.each(cases)("$name picks A above and at the limit, B below, neutral at 19", ({ id, limit, copy, build }) => {
    const atLimit = (limit * 20) / 100; // exact at n = 20 for 30, 60 and 80
    expect(card(build(atLimit + 1, 20), id).premise).toBe(copy.premiseA);
    expect(card(build(atLimit, 20), id).premise).toBe(copy.premiseA);
    expect(card(build(atLimit - 1, 20), id).premise).toBe(copy.premiseB);
    expect(card(build(19, 19), id).premise).toBe(copy.premiseNeutral);
    expect(card(build(0, 19), id).premise).toBe(copy.premiseNeutral);
  });

  it("card 1 reads 6 of 20 (= 30%) as A", () => {
    expect(card(insights(stats({ outside_hours_count: 6, conversational_trace_count: 20 })), "outside_hours").premise).toBe(
      pt.outsideHours.premiseA,
    );
  });

  it("every non-ready state carries that card's own neutral premise", () => {
    const ownNeutral: Record<Insight["id"], string> = {
      outside_hours: pt.outsideHours.premiseNeutral,
      new_contacts: pt.newContacts.premiseNeutral,
      top_intents: pt.topIntents.premiseNeutral,
      team_sla: pt.teamSla.premiseNeutral,
    };
    const cases: [string, Insight][] = [
      ...insights(fail(500), fail(400)).map((i): [string, Insight] => [`error ${i.id}`, i]),
      ...insights(loading, loading).map((i): [string, Insight] => [`loading ${i.id}`, i]),
      ["card 1 needs setup", card(insights(stats({ business_hours_configured: false })), "outside_hours")],
      ["card 1 empty", card(insights(stats({ conversational_trace_count: 0, outside_hours_count: 0 })), "outside_hours")],
      ["card 2 empty", card(insights(stats({ new_users_count: 0, returning_users_count: 0 })), "new_contacts")],
      ["card 3 empty", card(insights(stats({ intent_distribution: {} })), "top_intents")],
      ["card 3 only outras", card(insights(stats({ intent_distribution: { saudacao: 40 } })), "top_intents")],
      ["card 4 empty", card(insights(ok(BASE_STATS), ok(EMPTY_QUEUE)), "team_sla")],
    ];
    for (const [name, insight] of cases) {
      expect(insight.state, name).not.toBe("ready");
      expect(insight.premise, name).toBe(ownNeutral[insight.id]);
    }
  });

  it("uses the English sentences in en", () => {
    const list = insights(stats({ outside_hours_count: 30, conversational_trace_count: 100 }), ok(EMPTY_QUEUE), "en");
    expect(card(list, "outside_hours").premise).toBe(en.outsideHours.premiseA);
  });
});

describe("copy table", () => {
  const BANNED = {
    pt: ["convers", "atendeu", "respondid", "primeira resposta", "receita"],
    en: ["convers", "handled", "answered", "first response", "revenue"],
  };

  function strings(value: unknown): string[] {
    if (typeof value === "string") return [value];
    if (Array.isArray(value)) return value.flatMap(strings);
    if (value && typeof value === "object") return Object.values(value).flatMap(strings);
    return [];
  }

  it.each(LOCALES)("has no banned substring inside the four cards (%s)", (locale) => {
    // W8 scope: everything that renders inside a card (titles, lines, states,
    // premises, bucket premises, action labels and both unavailable messages).
    const scope = strings([CONSULTORIA_COPY[locale].cards, CONSULTORIA_COPY[locale].unavailable]);
    expect(scope.length).toBeGreaterThan(30);
    for (const entry of scope) {
      for (const banned of BANNED[locale]) {
        expect(entry.toLowerCase(), entry).not.toContain(banned);
      }
    }
  });

  it("has no em dash anywhere", () => {
    for (const entry of strings(CONSULTORIA_COPY)) expect(entry).not.toContain("\u2014");
  });

  it("has no em dash in any rendered insight", () => {
    const all = [
      ...insights(ok(BASE_STATS), queue({ within_sla_count: 20, sla_eligible_count: 25 })),
      ...insights(fail(400), fail(500), "en"),
    ];
    for (const entry of strings(all)) expect(entry).not.toContain("\u2014");
  });

  it("carries none of the old mock literals", () => {
    const literals = ["847", "92%", "+23%", "1.240", "1,240", "2,800", "+40%", "+18%", "+28%", "+17%", "12 min", "3 seg", "$ 82", "48h", "Harmonização Corporal", "Enterprise Only"];
    for (const literal of literals) expect(moduleSource).not.toContain(literal);
  });
});

describe("pickQueueStats", () => {
  const full = {
    within_sla_count: 1,
    confirmed_late_count: 2,
    unanswered_overdue_count: 3,
    maturing_count: 4,
    cancelled_unconfirmed_count: 5,
    unmeasured_legacy_count: 6,
    sla_eligible_count: 7,
  };
  const zeros = {
    within_sla_count: 0,
    confirmed_late_count: 0,
    unanswered_overdue_count: 0,
    maturing_count: 0,
    cancelled_unconfirmed_count: 0,
    unmeasured_legacy_count: 0,
    sla_eligible_count: 0,
  };

  it("keeps a full numeric body as given", () => {
    expect(pickQueueStats({ ...full })).toEqual(full);
  });

  it.each([
    ["null", null],
    ["undefined", undefined],
    ["a numeric string", "5"],
    ["NaN", Number.NaN],
    ["Infinity", Number.POSITIVE_INFINITY],
    ["-Infinity", Number.NEGATIVE_INFINITY],
  ])("turns a %s count into 0", (_label, value) => {
    const body = { ...full, maturing_count: value } as unknown as HandoffQueuePayload;
    expect(pickQueueStats(body)).toEqual({ ...full, maturing_count: 0 });
  });

  it("turns an absent count into 0", () => {
    const { sla_eligible_count: _omit, ...rest } = full;
    expect(pickQueueStats(rest)).toEqual({ ...full, sla_eligible_count: 0 });
  });

  it("drops extra keys, including items with patient data", () => {
    const body = { ...full, items: [{ numero_whatsapp: "5511999999999" }], wait_seconds: 9 };
    const stats = pickQueueStats(body);
    expect(Object.keys(stats).sort()).toEqual(Object.keys(full).sort());
    expect(JSON.stringify(stats)).not.toContain("numero_whatsapp");
  });

  it("drops everything from an all-odd body to zeros", () => {
    const body = { within_sla_count: "1", confirmed_late_count: null, maturing_count: Number.NaN } as unknown as HandoffQueuePayload;
    expect(pickQueueStats(body)).toEqual(zeros);
  });

  it("yields zeros for a string body without throwing", () => {
    expect(pickQueueStats("oops" as unknown as HandoffQueuePayload)).toEqual(zeros);
  });

  it.each([
    ["a string count", { ...full, within_sla_count: "5" }],
    ["a null count", { ...full, confirmed_late_count: null }],
  ])("keeps the cohort total finite with %s", (_label, body) => {
    const total = handoffCohortTotal(pickQueueStats(body as unknown as HandoffQueuePayload));
    expect(Number.isFinite(total)).toBe(true);
    expect(typeof total).toBe("number");
  });
});
