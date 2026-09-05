import { describe, expect, it } from "vitest";

import { pt } from "@/locales/pt";
import {
  getIntentLabel,
  getIntentPresentation,
  INTENT_PRESENTATION,
  UNKNOWN_INTENT_PRESENTATION,
} from "./intent-presentation";

const expectedIntents = [
  "saudacao", "interesse_tratamento", "qualificacao_publico", "comparacao_tratamentos",
  "pedido_preco", "objecao_vou_pensar", "agendar", "reagendar", "cancelar",
  "confirmar_presenca", "disponibilidade_horarios", "detalhes_servicos",
  "informacoes_profissionais", "faq", "cultura_historia", "duvida_generica_sem_contexto",
  "pre_atendimento", "pos_atendimento_duvida", "pos_atendimento_sintomas_preocupantes",
  "pedido_alterar_medicacao", "documentos_e_comprovantes", "orcamento_existente",
  "cobranca_renegociacao", "reclamacao", "conduta_inadequada_ameaca",
  "pedido_atendente_humano", "paciente_ativo", "fora_do_escopo", "fora_do_horario",
].sort();

const expectedPresentation = {
  saudacao: { translationKey: "intent_saudacao", baseClassName: "bg-sky-500", badgeClassName: "bg-sky-500 hover:bg-sky-600 text-white", hex: "#0ea5e9" },
  interesse_tratamento: { translationKey: "intent_interesse_tratamento", baseClassName: "bg-violet-500", badgeClassName: "bg-violet-500 hover:bg-violet-600 text-white", hex: "#8b5cf6" },
  qualificacao_publico: { translationKey: "intent_qualificacao_publico", baseClassName: "bg-purple-500", badgeClassName: "bg-purple-500 hover:bg-purple-600 text-white", hex: "#a855f7" },
  comparacao_tratamentos: { translationKey: "intent_comparacao_tratamentos", baseClassName: "bg-fuchsia-500", badgeClassName: "bg-fuchsia-500 hover:bg-fuchsia-600 text-white", hex: "#d946ef" },
  pedido_preco: { translationKey: "intent_pedido_preco", baseClassName: "bg-blue-500", badgeClassName: "bg-blue-500 hover:bg-blue-600 text-white", hex: "#3b82f6" },
  objecao_vou_pensar: { translationKey: "intent_objecao_vou_pensar", baseClassName: "bg-amber-500", badgeClassName: "bg-amber-500 hover:bg-amber-600 text-white", hex: "#f59e0b" },
  agendar: { translationKey: "intent_agendar", baseClassName: "bg-green-500", badgeClassName: "bg-green-500 hover:bg-green-600 text-white", hex: "#22c55e" },
  reagendar: { translationKey: "intent_reagendar", baseClassName: "bg-emerald-500", badgeClassName: "bg-emerald-500 hover:bg-emerald-600 text-white", hex: "#10b981" },
  cancelar: { translationKey: "intent_cancelar", baseClassName: "bg-orange-500", badgeClassName: "bg-orange-500 hover:bg-orange-600 text-white", hex: "#f97316" },
  confirmar_presenca: { translationKey: "intent_confirmar_presenca", baseClassName: "bg-teal-500", badgeClassName: "bg-teal-500 hover:bg-teal-600 text-white", hex: "#14b8a6" },
  disponibilidade_horarios: { translationKey: "intent_disponibilidade_horarios", baseClassName: "bg-cyan-500", badgeClassName: "bg-cyan-500 hover:bg-cyan-600 text-white", hex: "#06b6d4" },
  detalhes_servicos: { translationKey: "intent_detalhes_servicos", baseClassName: "bg-indigo-400", badgeClassName: "bg-indigo-400 hover:bg-indigo-500 text-white", hex: "#818cf8" },
  informacoes_profissionais: { translationKey: "intent_informacoes_profissionais", baseClassName: "bg-indigo-500", badgeClassName: "bg-indigo-500 hover:bg-indigo-600 text-white", hex: "#6366f1" },
  faq: { translationKey: "intent_faq", baseClassName: "bg-slate-500", badgeClassName: "bg-slate-500 hover:bg-slate-600 text-white", hex: "#64748b" },
  cultura_historia: { translationKey: "intent_cultura_historia", baseClassName: "bg-slate-400", badgeClassName: "bg-slate-400 hover:bg-slate-500 text-white", hex: "#94a3b8" },
  duvida_generica_sem_contexto: { translationKey: "intent_duvida_generica_sem_contexto", baseClassName: "bg-gray-500", badgeClassName: "bg-gray-500 hover:bg-gray-600 text-white", hex: "#6b7280" },
  pre_atendimento: { translationKey: "intent_pre_atendimento", baseClassName: "bg-lime-500", badgeClassName: "bg-lime-500 hover:bg-lime-600 text-white", hex: "#84cc16" },
  pos_atendimento_duvida: { translationKey: "intent_pos_atendimento_duvida", baseClassName: "bg-indigo-500", badgeClassName: "bg-indigo-500 hover:bg-indigo-600 text-white", hex: "#6366f1" },
  pos_atendimento_sintomas_preocupantes: { translationKey: "intent_pos_atendimento_sintomas_preocupantes", baseClassName: "bg-red-500", badgeClassName: "bg-red-500 hover:bg-red-600 text-white", hex: "#ef4444" },
  pedido_alterar_medicacao: { translationKey: "intent_pedido_alterar_medicacao", baseClassName: "bg-red-600", badgeClassName: "bg-red-600 hover:bg-red-700 text-white", hex: "#dc2626" },
  documentos_e_comprovantes: { translationKey: "intent_documentos_e_comprovantes", baseClassName: "bg-zinc-500", badgeClassName: "bg-zinc-500 hover:bg-zinc-600 text-white", hex: "#71717a" },
  orcamento_existente: { translationKey: "intent_orcamento_existente", baseClassName: "bg-blue-400", badgeClassName: "bg-blue-400 hover:bg-blue-500 text-white", hex: "#60a5fa" },
  cobranca_renegociacao: { translationKey: "intent_cobranca_renegociacao", baseClassName: "bg-yellow-500", badgeClassName: "bg-yellow-500 hover:bg-yellow-600 text-white", hex: "#eab308" },
  reclamacao: { translationKey: "intent_reclamacao", baseClassName: "bg-rose-500", badgeClassName: "bg-rose-500 hover:bg-rose-600 text-white", hex: "#f43f5e" },
  conduta_inadequada_ameaca: { translationKey: "intent_conduta_inadequada_ameaca", baseClassName: "bg-red-700", badgeClassName: "bg-red-700 hover:bg-red-800 text-white", hex: "#b91c1c" },
  pedido_atendente_humano: { translationKey: "intent_pedido_atendente_humano", baseClassName: "bg-rose-600", badgeClassName: "bg-rose-600 hover:bg-rose-700 text-white", hex: "#e11d48" },
  paciente_ativo: { translationKey: "intent_paciente_ativo", baseClassName: "bg-teal-600", badgeClassName: "bg-teal-600 hover:bg-teal-700 text-white", hex: "#0d9488" },
  fora_do_escopo: { translationKey: "intent_fora_do_escopo", baseClassName: "bg-stone-500", badgeClassName: "bg-stone-500 hover:bg-stone-600 text-white", hex: "#78716c" },
  fora_do_horario: { translationKey: "intent_fora_do_horario", baseClassName: "bg-stone-600", badgeClassName: "bg-stone-600 hover:bg-stone-700 text-white", hex: "#57534e" },
};

describe("intent presentation catalog", () => {
  it("preserves the complete 29-intent union", () => {
    expect(Object.keys(INTENT_PRESENTATION).sort()).toEqual(expectedIntents);
  });

  it("preserves every presentation token and exact chart color", () => {
    const actualPresentation = Object.fromEntries(
      Object.entries(INTENT_PRESENTATION).map(([key, presentation]) => [
        key,
        {
          translationKey: presentation.translationKey,
          baseClassName: presentation.baseClassName,
          badgeClassName: presentation.badgeClassName,
          hex: presentation.hex,
        },
      ]),
    );

    expect(actualPresentation).toEqual(expectedPresentation);
  });

  it("localizes known intents and leaves unknown values readable", () => {
    expect(getIntentLabel("agendar", key => pt[key])).toBe("Solicitação de Agendamento");
    expect(getIntentLabel("future_intent", () => "ignored")).toBe("future_intent");
    expect(getIntentPresentation("future_intent")).toBe(UNKNOWN_INTENT_PRESENTATION);
    expect(UNKNOWN_INTENT_PRESENTATION.badgeClassName).toContain("bg-slate-500");
    expect(UNKNOWN_INTENT_PRESENTATION).not.toHaveProperty("translationKey");
  });

  it.each(["constructor", "__proto__", "toString"])(
    "treats inherited object property %s as an unknown intent",
    (intentName) => {
      expect(getIntentPresentation(intentName)).toBe(UNKNOWN_INTENT_PRESENTATION);
      expect(getIntentLabel(intentName, () => "wrong inherited label")).toBe(intentName);
    },
  );
});
