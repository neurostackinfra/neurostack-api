"""
NeuroStack - Prompt Engine - Normal Template
Template for standard conversation flow.
Aligned with NeuroStack v0.6 methodology (200-case dataset).
"""

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import cast

from neurostack_core.presets import get_preset
from neurostack_core.prompt.models import PromptMessage, PromptMode, PromptVariables
from neurostack_core.prompt.templates.base import BaseTemplate, TemplateSection
from neurostack_core.prompt.timeutils import clinic_now
from neurostack_core.protocol import (
    CAN_QUOTE_PRIVATE_PAY_CUSTOM_KEY,
    CARE_MODALITY_SLOT,
    INSURANCE_CARE_MODALITY,
    INSURANCE_TRIAGE_CUSTOM_KEY,
)

_PROMPT_PRESET = cast(Mapping[str, object], get_preset().prompt)
_LEXICON: Mapping[str, str] = MappingProxyType(
    dict(cast(Mapping[str, str], _PROMPT_PRESET["lexicon"]))
)

NORMAL_SYSTEM_PROMPT = cast(str, _PROMPT_PRESET["normal_system_prompt"])
EN_SYSTEM_PROMPT = cast(str, _PROMPT_PRESET["normal_system_prompt_en"])
_CANONICAL_FUNNEL_INSTRUCTIONS = cast(
    Mapping[str, str], _PROMPT_PRESET["funnel_instructions"]
)
_CANONICAL_FUNNEL_INSTRUCTIONS_EN = cast(
    Mapping[str, str], _PROMPT_PRESET["funnel_instructions_en"]
)
_LEGACY_FUNNEL_INSTRUCTIONS = cast(
    Mapping[str, str], _PROMPT_PRESET["legacy_funnel_instructions"]
)
_LEGACY_FUNNEL_INSTRUCTIONS_EN = cast(
    Mapping[str, str], _PROMPT_PRESET["legacy_funnel_instructions_en"]
)
FUNNEL_INSTRUCTIONS: Mapping[str, str] = MappingProxyType(
    {**_CANONICAL_FUNNEL_INSTRUCTIONS, **_LEGACY_FUNNEL_INSTRUCTIONS}
)
EN_FUNNEL_INSTRUCTIONS: Mapping[str, str] = MappingProxyType(
    {**_CANONICAL_FUNNEL_INSTRUCTIONS_EN, **_LEGACY_FUNNEL_INSTRUCTIONS_EN}
)

_IMPERATIVE_LITERALS = cast(Mapping[str, object], _PROMPT_PRESET["imperative_literals"])
_NORMAL_IMPERATIVES = cast(
    Mapping[str, tuple[str, ...]],
    _IMPERATIVE_LITERALS["prompt.templates.normal"],
)
_RESIDUAL_GROUPS = cast(
    Mapping[str, tuple[str, ...]],
    _IMPERATIVE_LITERALS["prompt.templates.normal.residual"],
)
_RESIDUAL_LITERALS: Mapping[str, str] = MappingProxyType(
    {key: values[0] for key, values in _RESIDUAL_GROUPS.items()}
)
_BUILD_SYSTEM_LITERALS = _NORMAL_IMPERATIVES["NormalTemplate.build_system_message"]
_GET_SYSTEM_LITERALS = _NORMAL_IMPERATIVES["NormalTemplate.get_system_prompt"]
_CONTEXT_LITERALS = _NORMAL_IMPERATIVES["NormalTemplate.get_context_instructions"]
_CLINIC_LITERALS = _NORMAL_IMPERATIVES["NormalTemplate._build_clinic_context"]
_INTENT_LITERALS = _NORMAL_IMPERATIVES["NormalTemplate._get_intent_instruction"]


class NormalTemplate(BaseTemplate):
    """
    Template for NORMAL mode conversations.

    Handles standard conversation flow with context-aware
    instructions based on funnel stage and intent.
    Aligned with NeuroStack v0.6 methodology.
    """

    def __init__(self) -> None:
        super().__init__(PromptMode.NORMAL)

    def build_system_message(self, variables: PromptVariables) -> PromptMessage:
        """Override to append scheduling confirmation, emoji, and language as absolute last instructions."""
        msg = super().build_system_message(variables)

        # Scheduling handoff confirmation — injected here so it comes AFTER
        # get_constraints / get_safety_reminders / _get_response_format_reminder
        # (all of which are assembled inside super().build_system_message).
        # Language-specific text avoids contradicting the language override below.
        if variables.custom.get("handoff_confirmation_pending"):
            lang = variables.language or "pt-BR"
            if lang == "en":
                _q = _RESIDUAL_LITERALS["confirmation_question_en"]
                _no_confirm = _RESIDUAL_LITERALS["no_confirm_en"]
                _no_docs = _RESIDUAL_LITERALS["no_documents_en"]
                _no_extra = _RESIDUAL_LITERALS["no_extra_en"]
                _no_ann = _RESIDUAL_LITERALS["no_announce_en"]
                _sys_yes = _RESIDUAL_LITERALS["system_confirmed_en"]
                _sys_no = _RESIDUAL_LITERALS["system_declined_en"]
                _header = _RESIDUAL_LITERALS["confirmation_header_en"]
                _collected = _RESIDUAL_LITERALS["confirmation_collected_en"]
                _label = _RESIDUAL_LITERALS["confirmation_label_en"]
                _prohib = _RESIDUAL_LITERALS["confirmation_prohibited_en"]
            elif lang == "es":
                _q = _RESIDUAL_LITERALS["confirmation_question_es"]
                _no_confirm = _BUILD_SYSTEM_LITERALS[0]
                _no_docs = _BUILD_SYSTEM_LITERALS[1]
                _no_extra = _BUILD_SYSTEM_LITERALS[2]
                _no_ann = _BUILD_SYSTEM_LITERALS[3]
                _sys_yes = _BUILD_SYSTEM_LITERALS[4]
                _sys_no = _BUILD_SYSTEM_LITERALS[5]
                _header = _BUILD_SYSTEM_LITERALS[6]
                _collected = _BUILD_SYSTEM_LITERALS[7]
                _label = _BUILD_SYSTEM_LITERALS[8]
                _prohib = _RESIDUAL_LITERALS["confirmation_prohibited_es"]
            else:  # pt-BR default
                _q = _BUILD_SYSTEM_LITERALS[9]
                _no_confirm = _BUILD_SYSTEM_LITERALS[10]
                _no_docs = _BUILD_SYSTEM_LITERALS[11]
                _no_extra = _BUILD_SYSTEM_LITERALS[12]
                _no_ann = _BUILD_SYSTEM_LITERALS[13]
                _sys_yes = _BUILD_SYSTEM_LITERALS[14]
                _sys_no = _RESIDUAL_LITERALS["system_declined_pt"]
                _header = _BUILD_SYSTEM_LITERALS[15]
                _collected = _BUILD_SYSTEM_LITERALS[16]
                _label = _BUILD_SYSTEM_LITERALS[17]
                _prohib = _BUILD_SYSTEM_LITERALS[18]

            msg.content += (
                f"\n\n## {_header}\n"
                f"{_collected}\n\n"
                f"**{_label}**\n"
                f'"{_q}"\n\n'
                f"**{_prohib}**\n"
                f"- {_no_confirm}\n"
                f"- {_no_docs}\n"
                f"- {_no_extra}\n"
                f"- {_no_ann}\n"
                f"- {_sys_yes}\n"
                f"- {_sys_no}\n"
            )

        # ── Convênio vs Particular triage (Group 2) ───────────────────
        # Deterministic booleans injected by the pipeline (Group 1):
        #   ask_convenio_triage  → clinic accepts insurance, slot undefined,
        #                          and (turn_count == 2 OR price/schedule intent).
        #   can_quote_particular → True only when atendimento_tipo == "particular".
        # This block instructs warm triage + total price hold until the modality
        # is chosen. The hold is total: no monetary value in ANY block until
        # can_quote_particular flips to True.
        if variables.custom.get(INSURANCE_TRIAGE_CUSTOM_KEY):
            lang = variables.language or "pt-BR"
            if lang == "en":
                msg.content += _BUILD_SYSTEM_LITERALS[19]
            elif lang == "es":
                msg.content += _BUILD_SYSTEM_LITERALS[20]
            else:  # pt-BR default
                msg.content += _BUILD_SYSTEM_LITERALS[21]

        if variables.custom.get("scheduling_link_verification_status"):
            status = variables.custom["scheduling_link_verification_status"]
            if variables.language == "en":
                if status == "concluiu":
                    guidance = _RESIDUAL_LITERALS["scheduling_link_success_en"]
                else:
                    guidance = _RESIDUAL_LITERALS["scheduling_link_failure_en"]
            else:
                if status == "concluiu":
                    guidance = _BUILD_SYSTEM_LITERALS[22]
                else:
                    guidance = _BUILD_SYSTEM_LITERALS[23]
            msg.content += (
                f"\n\n## {_RESIDUAL_LITERALS['scheduling_link_header']}\n{guidance}"
            )

        # Emoji directive — suppressed when handoff_confirmation_pending to avoid
        # conflict with "SOMENTE esta pergunta" constraint in the confirmation block.
        if variables.custom.get("use_emojis") and not variables.custom.get(
            "handoff_confirmation_pending"
        ):
            msg.content += _RESIDUAL_LITERALS["emoji_instruction"]
        if variables.language == "en":
            msg.content += _RESIDUAL_LITERALS["language_instruction_en"]
        elif variables.language == "es":
            msg.content += _BUILD_SYSTEM_LITERALS[24]
        return msg

    def get_system_prompt(self, variables: PromptVariables) -> str:
        """Get the base system prompt with clinic identity."""
        agent_name = variables.custom.get(
            "agent_name", _RESIDUAL_LITERALS["assistant_default"]
        )
        clinic_name = variables.custom.get("clinic_name", _GET_SYSTEM_LITERALS[0])
        agent_presentation = variables.custom.get("agent_presentation", "")

        base = EN_SYSTEM_PROMPT if variables.language == "en" else NORMAL_SYSTEM_PROMPT
        prompt = base.replace("{{agent_name}}", agent_name)
        prompt = prompt.replace("{{clinic_name}}", clinic_name)
        prompt = prompt.replace("{{agent_presentation}}", agent_presentation)

        return self.render_template(prompt, variables)

    def get_context_instructions(self, variables: PromptVariables) -> str:
        """Get context-specific instructions based on funnel stage and user journey."""
        now_local = clinic_now(variables.custom.get("timezone"))
        parts = [
            f"{_RESIDUAL_LITERALS['current_datetime_label']}"
            f"{now_local.strftime(_RESIDUAL_LITERALS['current_datetime_format'])}"
            f"{_CONTEXT_LITERALS[0]}"
        ]

        # Inject clinic identity for template placeholders in funnel instructions
        agent_name = variables.custom.get(
            "agent_name", _RESIDUAL_LITERALS["assistant_default"]
        )
        clinic_name = variables.custom.get("clinic_name", _CONTEXT_LITERALS[1])

        # Jornada do usuário no funil (contexto principal para condução)
        if variables.user_journey:
            journey_section = TemplateSection(_CONTEXT_LITERALS[2])
            journey_section.add(_CONTEXT_LITERALS[3])
            journey_section.add(variables.user_journey)
            parts.append(journey_section.build())

        # Funnel stage instructions — pick EN or PT dict based on language
        funnel_stage = variables.funnel_stage.lower()
        funnel_dict = (
            EN_FUNNEL_INSTRUCTIONS
            if variables.language == "en"
            else FUNNEL_INSTRUCTIONS
        )
        instruction = funnel_dict.get(funnel_stage)
        if instruction is not None:
            instruction = instruction.replace("{{agent_name}}", agent_name)
            instruction = instruction.replace("{{clinic_name}}", clinic_name)
            parts.append(instruction)

            # ── Convenio-scheduling addendum (Group 2) ─────────────────
            # When the modality is `convenio`, scheduling ALSO collects the
            # insurance name and card number in writing (never a photo).
            # This addendum augments the standard scheduling flow; the base
            # 2-step (nome + preferência) still runs.
            if (
                funnel_stage == "agendamento_solicitado"
                and variables.custom.get(CARE_MODALITY_SLOT) == INSURANCE_CARE_MODALITY
            ):
                if variables.language == "en":
                    parts.append(_CONTEXT_LITERALS[4])
                else:
                    parts.append(_CONTEXT_LITERALS[5])

        # Intent-specific instructions
        if variables.last_intent:
            intent_instruction = self._get_intent_instruction(
                variables.last_intent, agent_name, clinic_name, now_local
            )
            if intent_instruction:
                parts.append(intent_instruction)

        # Slots information
        if variables.slots:
            slots_section = TemplateSection(_CONTEXT_LITERALS[6])
            for key, value in variables.slots.items():
                slots_section.add(f"**{key}**: {value}")
            parts.append(slots_section.build())

        # Knowledge context (if available)
        if variables.knowledge_context:
            knowledge_section = TemplateSection(
                _RESIDUAL_LITERALS["knowledge_context_title"]
            )
            knowledge_section.add(variables.knowledge_context)
            if variables.knowledge_sources:
                knowledge_section.add(_RESIDUAL_LITERALS["knowledge_sources_label"])
                knowledge_section.add_list(variables.knowledge_sources)
            parts.append(knowledge_section.build())

        # Clinic config context (dynamic info)
        clinic_info = self._build_clinic_context(variables)
        if clinic_info:
            parts.append(clinic_info)

        # Language override: appended last so it has highest priority.
        # The funnel instructions above contain Portuguese examples — this
        # ensures the model responds in the requested language regardless.
        if variables.language == "en":
            parts.append(_RESIDUAL_LITERALS["context_language_override_en"])
        elif variables.language == "es":
            parts.append(_CONTEXT_LITERALS[7])

        return "\n".join(parts)

    def _build_clinic_context(self, variables: PromptVariables) -> str:
        """Build clinic-specific context from custom variables."""
        custom = variables.custom
        if not custom:
            return ""

        section = TemplateSection(_CLINIC_LITERALS[0])

        if custom.get("clinic_history"):
            section.add(f"{_CLINIC_LITERALS[1]}{custom['clinic_history']}")
        if custom.get("clinic_specialties"):
            section.add(
                f"{_RESIDUAL_LITERALS['specialties_label']}"
                f"{', '.join(custom['clinic_specialties'])}"
            )
        if custom.get("working_hours"):
            section.add(f"{_CLINIC_LITERALS[2]}{custom['working_hours']}")
        if custom.get("full_address"):
            section.add(f"{_CLINIC_LITERALS[3]}{custom['full_address']}")
        if custom.get("payment_methods"):
            section.add(f"{_CLINIC_LITERALS[4]}{custom['payment_methods']}")
        if custom.get("differentials"):
            section.add(
                f"{_RESIDUAL_LITERALS['differentials_label']}"
                f"{custom['differentials']}"
            )
        # ── Convenio triage gate (Group 2) ──────────────────────────
        # When the clinic accepts insurance AND the modality has not been
        # confirmed as `particular` yet (can_quote_particular=False), we must
        # emit NO monetary value in ANY block below — that includes the pricing
        # policy AND the initial-consultation/complimentary block. If insurance
        # is off the triage feature is inactive and behavior is unchanged.
        accepts_ins = custom.get("accepts_insurance")
        insurance_active = bool(
            accepts_ins and str(accepts_ins).lower() not in ("no", "false", "0", "none")
        )
        hold_price = insurance_active and not custom.get(
            CAN_QUOTE_PRIVATE_PAY_CUSTOM_KEY
        )

        if hold_price:
            # Convenio-safe pricing policy — no monetary value emitted.
            section.add(_CLINIC_LITERALS[5])
        else:
            # Pricing policy — two clear states: PODE_FALAR (yes) or NÃO_PODE_FALAR (else).
            # pricing_mode values are normalised to lower-case (pipeline sends range_allowed /
            # exact_allowed / consult_only).  provide_prices=yes always means exact_allowed.
            provide_prices = custom.get("provide_prices")
            pricing_mode = str(custom.get("pricing_mode", "")).lower().strip()
            is_range_allowed = pricing_mode == "range_allowed"
            is_exact_allowed = (
                provide_prices in (True, "yes", "true", "True")
                or pricing_mode == "exact_allowed"
            )
            can_share_price = is_range_allowed or is_exact_allowed

            if can_share_price:
                price_detail = (
                    _CLINIC_LITERALS[6] if is_range_allowed else _CLINIC_LITERALS[7]
                )
                section.add(f"{_CLINIC_LITERALS[8]}{price_detail}{_CLINIC_LITERALS[9]}")
            else:
                section.add(_CLINIC_LITERALS[10])

        # Extended config fields
        if insurance_active:
            insurance_text = _CLINIC_LITERALS[11]
            if custom.get("insurance_list"):
                insurance_text += f" — {custom['insurance_list']}"
            section.add(insurance_text)
        if custom.get("initial_consultation"):
            ic = custom["initial_consultation"]
            section.add(f"{_CLINIC_LITERALS[12]}{ic}")

            if hold_price:
                # Convenio-safe consulta-inicial: NO monetary value in any scenario.
                # Reframes complimentary and paid visits alike while coverage
                # remains unresolved.
                section.add(_CLINIC_LITERALS[13])
            else:
                ic_lower = str(ic).lower()
                is_complimentary = any(
                    w in ic_lower
                    for w in (
                        _LEXICON["free_pt"],
                        _LEXICON["free_feminine_pt"],
                        _LEXICON["complimentary_pt"],
                        _LEXICON["free_en"],
                        _LEXICON["complimentary_en"],
                    )
                )
                consult_price = str(custom.get("consult_service_price", "")).strip()
                consult_price_num = (
                    consult_price.replace(_LEXICON["currency_prefix"], "")
                    .replace(",", ".")
                    .strip()
                )
                # Empty string = "not configured", NOT free — only explicit "0" / "0.00" means explicitly free
                is_explicitly_zero = consult_price_num in ("0", "0.00", "0,00")
                has_followup_price = consult_price_num != "" and not is_explicitly_zero

                if is_complimentary and is_explicitly_zero:
                    # Explicit zero means every consultation has no charge.
                    section.add(_CLINIC_LITERALS[14])
                elif is_complimentary and has_followup_price:
                    # The first visit has no charge; later visits use the configured price.
                    section.add(
                        f"{_CLINIC_LITERALS[15]}{consult_price}{_CLINIC_LITERALS[16]}{consult_price}{_CLINIC_LITERALS[17]}"
                    )
                elif is_complimentary:
                    # No follow-up price is configured, so only describe the first visit.
                    section.add(_CLINIC_LITERALS[18])
                else:
                    # Cenário 2: Consulta inicial paga
                    section.add(_CLINIC_LITERALS[19])
        if custom.get("parking_info"):
            section.add(
                f"{_RESIDUAL_LITERALS['parking_label']}{custom['parking_info']}"
            )
        if custom.get("scheduling_hours"):
            section.add(f"{_CLINIC_LITERALS[20]}{custom['scheduling_hours']}")
        if custom.get("service_format"):
            section.add(f"{_CLINIC_LITERALS[21]}{custom['service_format']}")
        if custom.get("use_emojis"):
            section.add(_CLINIC_LITERALS[22])
        else:
            section.add(_CLINIC_LITERALS[23])
        if custom.get("language_policy"):
            section.add(f"{_CLINIC_LITERALS[24]}{custom['language_policy']}")
        # provide_prices already handled above in pricing policy section

        # Services section (with prices if available)
        if custom.get("services_detail"):
            section.add(_CLINIC_LITERALS[25])
            section.add(_CLINIC_LITERALS[26])
            section.add(custom["services_detail"])

        # Professionals section
        if custom.get("professionals"):
            section.add(_CLINIC_LITERALS[27])
            section.add(custom["professionals"])

        # LGPD — always inject privacy link so agent can respond when user asks
        if custom.get("privacy_link"):
            section.add(f"{_CLINIC_LITERALS[28]}{custom['privacy_link']}")

        content = section.build()
        return content if section.content else ""

    def _get_intent_instruction(
        self,
        intent: str,
        agent_name: str = _RESIDUAL_LITERALS["assistant_default"],
        clinic_name: str = _INTENT_LITERALS[0],
        now_local: datetime | None = None,
    ) -> str:
        """Get instruction for specific intent — NeuroStack v0.6 methodology."""
        # Compute correct greeting for current clinic-local time
        if now_local is not None:
            hour = now_local.hour
            if hour < 12:
                saudacao_correta = _INTENT_LITERALS[1]
            elif hour < 18:
                saudacao_correta = _INTENT_LITERALS[2]
            else:
                saudacao_correta = _RESIDUAL_LITERALS["good_evening"]
            hora_str = now_local.strftime(_RESIDUAL_LITERALS["greeting_time_format"])
        else:
            saudacao_correta = _INTENT_LITERALS[3]
            hora_str = ""

        intent_instructions = {
            # ===== SAUDAÇÃO =====
            "saudacao": f"{_INTENT_LITERALS[4]}{hora_str}{_INTENT_LITERALS[5]}{saudacao_correta}{_INTENT_LITERALS[6]}{saudacao_correta}{_INTENT_LITERALS[7]}{agent_name}{_RESIDUAL_LITERALS['agent_clinic_joiner']}{clinic_name}{_INTENT_LITERALS[8]}",
            # ===== INTERESSE EM TRATAMENTO =====
            "interesse_tratamento": _INTENT_LITERALS[9],
            # ===== QUALIFICAÇÃO =====
            "qualificacao_publico": _INTENT_LITERALS[10],
            # ===== PEDIDO DE PREÇO =====
            "pedido_preco": _INTENT_LITERALS[11],
            # ===== AGENDAMENTO =====
            "agendar": _INTENT_LITERALS[12],
            "reagendar": _INTENT_LITERALS[13],
            "cancelar": _INTENT_LITERALS[14],
            "confirmar_presenca": _INTENT_LITERALS[15],
            "disponibilidade_horarios": _INTENT_LITERALS[16],
            # ===== INFORMAÇÕES =====
            "detalhes_servicos": _INTENT_LITERALS[17],
            "informacoes_profissionais": _INTENT_LITERALS[18],
            "faq": _INTENT_LITERALS[19],
            "pre_atendimento": _INTENT_LITERALS[20],
            "cultura_historia": _INTENT_LITERALS[21],
            # ===== PÓS-ATENDIMENTO =====
            "pos_atendimento_duvida": _INTENT_LITERALS[22],
            "pos_atendimento_sintomas_preocupantes": _INTENT_LITERALS[23],
            # ===== MEDICAÇÃO =====
            "pedido_alterar_medicacao": _INTENT_LITERALS[24],
            # ===== RECLAMAÇÕES =====
            "reclamacao": _INTENT_LITERALS[25],
            # ===== CONDUTA INADEQUADA =====
            "conduta_inadequada_ameaca": _INTENT_LITERALS[26],
            # ===== OBJEÇÕES =====
            "objecao_vou_pensar": _INTENT_LITERALS[27],
            # ===== COMPARAÇÃO =====
            "comparacao_tratamentos": _INTENT_LITERALS[28],
            # ===== DOCUMENTOS =====
            "documentos_e_comprovantes": _INTENT_LITERALS[29],
            # ===== ORÇAMENTO =====
            "orcamento_existente": _INTENT_LITERALS[30],
            # ===== FINANCEIRO =====
            "cobranca_renegociacao": _INTENT_LITERALS[31],
            # ===== DÚVIDA GENÉRICA =====
            "duvida_generica_sem_contexto": _INTENT_LITERALS[32],
        }

        return intent_instructions.get(intent, "")
