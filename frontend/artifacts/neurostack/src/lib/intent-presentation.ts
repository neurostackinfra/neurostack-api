import type { TranslationKey } from "@/locales/en";

interface IntentPresentationStyle {
  baseClassName: string;
  badgeClassName: string;
  hex: string;
}

export interface IntentPresentation extends IntentPresentationStyle {
  translationKey: TranslationKey;
}

const intent = (translationKey: TranslationKey, baseClassName: string, hoverClassName: string, hex: string): IntentPresentation => ({
  translationKey,
  baseClassName,
  badgeClassName: `${baseClassName} ${hoverClassName} text-white`,
  hex,
});

export const INTENT_PRESENTATION = {
  saudacao: intent("intent_saudacao", "bg-sky-500", "hover:bg-sky-600", "#0ea5e9"),
  interesse_tratamento: intent("intent_interesse_tratamento", "bg-violet-500", "hover:bg-violet-600", "#8b5cf6"),
  qualificacao_publico: intent("intent_qualificacao_publico", "bg-purple-500", "hover:bg-purple-600", "#a855f7"),
  comparacao_tratamentos: intent("intent_comparacao_tratamentos", "bg-fuchsia-500", "hover:bg-fuchsia-600", "#d946ef"),
  pedido_preco: intent("intent_pedido_preco", "bg-blue-500", "hover:bg-blue-600", "#3b82f6"),
  objecao_vou_pensar: intent("intent_objecao_vou_pensar", "bg-amber-500", "hover:bg-amber-600", "#f59e0b"),
  agendar: intent("intent_agendar", "bg-green-500", "hover:bg-green-600", "#22c55e"),
  reagendar: intent("intent_reagendar", "bg-emerald-500", "hover:bg-emerald-600", "#10b981"),
  cancelar: intent("intent_cancelar", "bg-orange-500", "hover:bg-orange-600", "#f97316"),
  confirmar_presenca: intent("intent_confirmar_presenca", "bg-teal-500", "hover:bg-teal-600", "#14b8a6"),
  disponibilidade_horarios: intent("intent_disponibilidade_horarios", "bg-cyan-500", "hover:bg-cyan-600", "#06b6d4"),
  detalhes_servicos: intent("intent_detalhes_servicos", "bg-indigo-400", "hover:bg-indigo-500", "#818cf8"),
  informacoes_profissionais: intent("intent_informacoes_profissionais", "bg-indigo-500", "hover:bg-indigo-600", "#6366f1"),
  faq: intent("intent_faq", "bg-slate-500", "hover:bg-slate-600", "#64748b"),
  cultura_historia: intent("intent_cultura_historia", "bg-slate-400", "hover:bg-slate-500", "#94a3b8"),
  duvida_generica_sem_contexto: intent("intent_duvida_generica_sem_contexto", "bg-gray-500", "hover:bg-gray-600", "#6b7280"),
  pre_atendimento: intent("intent_pre_atendimento", "bg-lime-500", "hover:bg-lime-600", "#84cc16"),
  pos_atendimento_duvida: intent("intent_pos_atendimento_duvida", "bg-indigo-500", "hover:bg-indigo-600", "#6366f1"),
  pos_atendimento_sintomas_preocupantes: intent("intent_pos_atendimento_sintomas_preocupantes", "bg-red-500", "hover:bg-red-600", "#ef4444"),
  pedido_alterar_medicacao: intent("intent_pedido_alterar_medicacao", "bg-red-600", "hover:bg-red-700", "#dc2626"),
  documentos_e_comprovantes: intent("intent_documentos_e_comprovantes", "bg-zinc-500", "hover:bg-zinc-600", "#71717a"),
  orcamento_existente: intent("intent_orcamento_existente", "bg-blue-400", "hover:bg-blue-500", "#60a5fa"),
  cobranca_renegociacao: intent("intent_cobranca_renegociacao", "bg-yellow-500", "hover:bg-yellow-600", "#eab308"),
  reclamacao: intent("intent_reclamacao", "bg-rose-500", "hover:bg-rose-600", "#f43f5e"),
  conduta_inadequada_ameaca: intent("intent_conduta_inadequada_ameaca", "bg-red-700", "hover:bg-red-800", "#b91c1c"),
  pedido_atendente_humano: intent("intent_pedido_atendente_humano", "bg-rose-600", "hover:bg-rose-700", "#e11d48"),
  paciente_ativo: intent("intent_paciente_ativo", "bg-teal-600", "hover:bg-teal-700", "#0d9488"),
  fora_do_escopo: intent("intent_fora_do_escopo", "bg-stone-500", "hover:bg-stone-600", "#78716c"),
  fora_do_horario: intent("intent_fora_do_horario", "bg-stone-600", "hover:bg-stone-700", "#57534e"),
} as const satisfies Record<string, IntentPresentation>;

export const UNKNOWN_INTENT_PRESENTATION: IntentPresentationStyle = {
  baseClassName: "bg-slate-500",
  badgeClassName: "bg-slate-500 hover:bg-slate-600 text-white",
  hex: "#64748b",
};

function hasKnownIntent(key: string): key is keyof typeof INTENT_PRESENTATION {
  return Object.prototype.hasOwnProperty.call(INTENT_PRESENTATION, key);
}

export function getIntentPresentation(key: string): IntentPresentationStyle {
  return hasKnownIntent(key) ? INTENT_PRESENTATION[key] : UNKNOWN_INTENT_PRESENTATION;
}

export function getIntentLabel(key: string, t: (translationKey: TranslationKey) => string): string {
  if (!hasKnownIntent(key)) return key;
  const presentation = INTENT_PRESENTATION[key];
  const translated = t(presentation.translationKey);
  return translated && translated !== presentation.translationKey ? translated : key;
}
