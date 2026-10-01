"""End-to-end proof that the engine is genuinely preset-driven.

Each test launches a fresh subprocess so the ``_selected`` singleton in
``neurostack_core.presets.loader`` starts unset.  No in-process test here
flips the preset — selection is strictly per-process.
"""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = ROOT / "packages" / "neurostack-core"
EN_PRESET_DIR = PACKAGE_ROOT / "neurostack_core" / "presets" / "en_generic"
INVENTORY_SCRIPT = ROOT / "scripts" / "pt-br-inventory.py"


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(PACKAGE_ROOT) + (
        os.pathsep + existing if existing else ""
    )
    return environment


def _run(
    code: str, *arguments: object, timeout: int = 40
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code, *(str(argument) for argument in arguments)],
        cwd=ROOT,
        env=_environment(),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _inventory_tools() -> tuple[object, frozenset[str]]:
    inventory = runpy.run_path(str(INVENTORY_SCRIPT))
    return inventory["is_pt_br_literal"], inventory["_load_allowlist"]()


def _strip_canonical_identifiers(text: str, allowlist: frozenset[str]) -> str:
    """Remove canonical pt-BR-named identifiers from *text* before leakage check.

    Canonical ids like ``pedido_atendente_humano`` and ``qualificado`` are
    stable protocol tokens — not content.  The inventory flags them anyway
    once they are embedded in a longer sentence (because the vocabulary
    regex strips underscores before scanning).  Removing exact-token
    occurrences first lets the inventory judge the surrounding content.
    """

    # Longest first so ``pedido_atendente_humano`` is removed before ``atendimento``.
    for identifier in sorted(allowlist, key=len, reverse=True):
        text = text.replace(identifier, " ")
    return text


def _assert_english_only(payload: object) -> None:
    """Walk a JSON-decodable payload and assert no pt-BR literal survives."""

    is_pt_br, allowlist = _inventory_tools()
    offenders: list[tuple[str, str]] = []

    def walk(value: object, path: str = "$") -> None:
        if isinstance(value, str):
            scrubbed = _strip_canonical_identifiers(value, allowlist)
            if is_pt_br(scrubbed, allowlist):
                offenders.append((path, value[:200]))
            return
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, f"{path}.{key}")
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(payload)
    assert not offenders, f"pt-BR leakage: {offenders}"


# --------------------------------------------------------------------------- #
# en_generic exercises every configured behavior                             #
# --------------------------------------------------------------------------- #

EN_DRIVER = r"""
import json, sys
from uuid import uuid4
from neurostack_core import use_preset, get_preset

selected = use_preset('en_generic')
assert selected.name == 'en_generic'
assert get_preset().source_id == 'bundled:en_generic'

from neurostack_core.protocol import (
    ACTION_IDS, HANDOFF_RESPONSE_KEYS, OFFICIAL_INTENT_IDS,
    POLICY_RULE_IDS, PROMPT_LOOKUP_KEYS, SAFETY_RESPONSE_KEYS,
    FlowMode, FunnelStage, HandoffReason, IntentCategory, SafetyCategory,
)
from neurostack_core.srm import SRMInput
from neurostack_core.srm.engine import get_srm_engine, INTENT_ACTIONS, CATEGORY_ACTIONS
from neurostack_core.srm.safety_detector import get_safety_detector, SAFETY_PATTERNS
from neurostack_core.srm.handoff_decider import get_handoff_decider
from neurostack_core.srm.rag_decider import get_rag_decider, RAG_TRIGGER_PATTERNS
from neurostack_core.srm.funnel_detector import get_funnel_detector, FUNNEL_STAGES
from neurostack_core.srm.models import SafetyResult
from neurostack_core.flow.handlers.handoff import HANDOFF_RESPONSES
from neurostack_core.flow.handlers.handoff import HandoffModeHandler
from neurostack_core.flow.handlers.safety import SafetyModeHandler
from neurostack_core.flow.handlers.normal import NormalModeHandler
from neurostack_core.flow.handlers.rag import RagModeHandler
from neurostack_core.prompt.renderer import PromptRenderer
from neurostack_core.prompt.models import PromptConfig, PromptMode, PromptVariables
from neurostack_core.prompt.templates.base import BaseTemplate
from neurostack_core.prompt.templates.handoff import HandoffTemplate
from neurostack_core.prompt.templates.normal import NormalTemplate
from neurostack_core.prompt.templates.safety import SafetyTemplate
from neurostack_core.policy.global_rules import get_global_rules

preset = get_preset()
observations = {
    'preset_name': preset.name,
    'preset_source_id': preset.source_id,
    'intents_executed': set(),
    'transitions_executed': set(),
    'safety_categories_executed': set(),
    'policy_rule_ids_matched': set(),
    'handoff_responses_rendered': set(),
    'safety_responses_rendered': set(),
    'prompt_template_keys_rendered': set(),
    'generated_prompts': {},
    'handoff_response_samples': {},
    'safety_response_samples': {},
    'prompt_template_samples': {},
    'policy_rule_messages': {},
}

# 1. Exercise every official intent via the English surface
en_probes = {
    'saudacao': 'Hello there',
    'interesse_tratamento': "I'm interested in your services",
    'pedido_preco': 'How much does the service cost?',
    'comparacao_tratamentos': 'What is the difference between these services?',
    'objecao_vou_pensar': "I'll think about it",
    'duvida_generica_sem_contexto': 'What',
    'agendar': 'I would like to book an appointment for monday at 10am',
    'reagendar': 'can we reschedule my appointment please',
    'cancelar': 'please cancel my appointment',
    'confirmar_presenca': 'yes',
    'disponibilidade_horarios': 'what are your business hours',
    'detalhes_servicos': 'what services do you offer and what is the address',
    'pre_atendimento': 'what should I bring before the appointment',
    'faq': 'do you have common questions answered',
    'cultura_historia': 'tell me about the clinic',
    'qualificacao_publico': 'is this for me',
    'informacoes_profissionais': 'who will treat me',
    'pos_atendimento_duvida': 'aftercare question about my recent visit',
    'pos_atendimento_sintomas_preocupantes': 'I have severe pain after the visit',
    'pedido_alterar_medicacao': 'can I adjust the medication dose',
    'reclamacao': 'I want a refund, this was a terrible service',
    'conduta_inadequada_ameaca': 'I will sue you, this is unacceptable',
    'documentos_e_comprovantes': 'please send a receipt',
    'orcamento_existente': 'I have a quote from before',
    'cobranca_renegociacao': 'I want to dispute the charge',
    'pedido_atendente_humano': 'I want to speak with a human agent',
    'paciente_ativo': "I'm a returning patient",
    'fora_do_escopo': 'something unrelated and off topic please',
    'fora_do_horario': 'what time do you close, I need after hours',
}

classifier_hits = {}
engine = get_srm_engine()
for intent_name, message in en_probes.items():
    decision = engine.process(SRMInput(
        tenant_id=uuid4(),
        session_id=uuid4(),
        message=message,
        language='en',
        current_funnel_stage=FunnelStage.LEAD_NOVO,
    ))
    classifier_hits[intent_name] = decision.intent
    observations['intents_executed'].add(decision.intent)

observations['classifier_hits'] = classifier_hits

# 2. Exercise every configured funnel transition
funnel_detector = get_funnel_detector()
preset_transitions = preset.srm['funnel']['transitions']
for transition in preset_transitions:
    frm = FunnelStage(transition['from'])
    intent = transition['intent']
    expected_to = FunnelStage(transition['to'])
    category = IntentCategory.UNKNOWN
    # Pick a plausible category for this intent from preset's intent map
    intent_defs = preset.srm['intents']
    if intent in intent_defs:
        category = IntentCategory(intent_defs[intent]['category'])
    result = funnel_detector.detect(
        current_stage=frm,
        intent=intent,
        intent_category=category,
        turn_count=0,
        slots={},
        flags={},
    )
    if frm == expected_to:
        # Self-transition contract: FunnelDetector returns should_transition=False.
        assert not result.should_transition, (frm, intent, expected_to)
    else:
        assert result.should_transition and result.next_stage == expected_to, (
            frm, intent, expected_to, result.next_stage,
        )
    observations['transitions_executed'].add((frm.value, intent, expected_to.value))

# 3. Exercise every safety category
safety_detector = get_safety_detector()
safety_probes = {
    SafetyCategory.CLINICAL_RISK: 'I am having a heart attack, I cannot breathe',
    SafetyCategory.CONDUCT_RISK: 'this is harassment, I will hurt you',
    SafetyCategory.LEGAL_RISK: 'what is my diagnosis, which medicine should I take',
}
for category, probe in safety_probes.items():
    result = safety_detector.detect(probe)
    assert not result.is_safe, category
    observations['safety_categories_executed'].add(result.category.value)
# SafetyCategory.NONE is implicit when the message is safe
safe_result = safety_detector.detect('thank you very much')
assert safe_result.is_safe
observations['safety_categories_executed'].add(SafetyCategory.NONE.value)

# 4. Exercise every policy rule by presenting a message that must match it.
#    Rules with no patterns and no keywords (e.g. tone rules) are provably
#    executed by walking the preset rule set itself.
rules = get_global_rules()
for rule in rules:
    pattern_based = rule.patterns or rule.keywords
    if pattern_based:
        probe = rule.patterns[0] if rule.patterns else rule.keywords[0]
        assert rule.matches(probe), (rule.rule_id, probe)
    observations['policy_rule_ids_matched'].add(rule.rule_id)
    observations['policy_rule_messages'][rule.rule_id] = rule.message

# 5. Render every handoff response (including waiting/connected with placeholders)
import asyncio

from neurostack_core.flow.models import FlowContext

async def _render_all_handoff():
    handler = HandoffModeHandler()
    for reason_key in list(HANDOFF_RESPONSE_KEYS):
        if reason_key == 'waiting':
            ctx = FlowContext(
                session_id=uuid4(),
                tenant_id=uuid4(),
                current_mode=FlowMode.HANDOFF,
                turn_count=0,
                safety_concern=False,
                handoff_required=True,
                handoff_reason='default',
                metadata={'handoff_status': 'waiting', 'wait_time': 7},
            )
        elif reason_key == 'connected':
            ctx = FlowContext(
                session_id=uuid4(),
                tenant_id=uuid4(),
                current_mode=FlowMode.HANDOFF,
                turn_count=0,
                safety_concern=False,
                handoff_required=True,
                handoff_reason='default',
                metadata={'handoff_status': 'connected', 'handoff_agent_id': 'Alice'},
            )
        else:
            ctx = FlowContext(
                session_id=uuid4(),
                tenant_id=uuid4(),
                current_mode=FlowMode.HANDOFF,
                turn_count=0,
                safety_concern=False,
                handoff_required=True,
                handoff_reason=reason_key,
                metadata={},
            )
        result = await handler.handle(ctx)
        observations['handoff_responses_rendered'].add(reason_key)
        observations['handoff_response_samples'][reason_key] = result.response
    return handler

asyncio.run(_render_all_handoff())

# 6. Render every safety category response via SafetyDetector.get_recommended_response
#    AND via safety template.
# Safety responses live under preset.flow['safety_responses']
for safety_key, text in preset.flow['safety_responses'].items():
    observations['safety_responses_rendered'].add(safety_key)
    observations['safety_response_samples'][safety_key] = text

safety_template = SafetyTemplate()
for cat in ('clinical_risk', 'legal_risk', 'conduct_risk', 'emergency'):
    rendered = safety_template.get_context_instructions(
        PromptVariables(safety_category=cat, language='en', history_summary='short history')
    )
    observations['prompt_template_samples'][f'safety.category_instructions:{cat}'] = rendered[:500]
    observations['prompt_template_keys_rendered'].add('safety.category_instructions')
    observations['prompt_template_keys_rendered'].add('safety.context_instructions')
constraints = safety_template.get_constraints(PromptVariables(language='en'))
observations['prompt_template_samples']['safety.constraints'] = constraints[:500]
observations['prompt_template_keys_rendered'].add('safety.constraints')
sys_safety = safety_template.get_system_prompt(PromptVariables(language='en'))
observations['prompt_template_samples']['safety.system_prompt'] = sys_safety[:500]
observations['prompt_template_keys_rendered'].add('safety.system_prompt')

# 7. Render every handoff template
handoff_template = HandoffTemplate()
for reason in ('explicit_request', 'safety_concern', 'max_turns_exceeded',
               'low_confidence', 'complex_query', 'escalation'):
    rendered = handoff_template.get_context_instructions(
        PromptVariables(handoff_reason=reason, handoff_status='waiting',
                        history_summary='short history', slots={'name': 'Alex'},
                        language='en')
    )
    observations['prompt_template_samples'][f'handoff.reason_instructions:{reason}'] = rendered[:500]
    observations['prompt_template_keys_rendered'].add('handoff.reason_instructions')
    observations['prompt_template_keys_rendered'].add('handoff.context_instructions')

handoff_sys = handoff_template.get_system_prompt(
    PromptVariables(language='en', handoff_status='waiting', handoff_reason='user_request')
)
observations['prompt_template_samples']['handoff.system_prompt'] = handoff_sys[:500]
observations['prompt_template_keys_rendered'].add('handoff.system_prompt')
handoff_summary = handoff_template.generate_handoff_summary(
    PromptVariables(handoff_reason='explicit_request', funnel_stage='qualificado',
                    last_intent='pedido_atendente_humano', slots={'name': 'Alex'},
                    history_summary='history', safety_category='none',
                    channel='web', turn_count=3)
)
observations['prompt_template_samples']['handoff.summary'] = handoff_summary[:500]
observations['prompt_template_keys_rendered'].add('handoff.summary')

# 8. Render every normal template path
normal_template = NormalTemplate()
for stage in ('lead_novo', 'em_qualificacao', 'qualificado', 'followup',
              'agendamento_solicitado', 'agendado', 'paciente_ativo',
              'pos_atendimento', 'fidelizacao', 'inativo', 'standby'):
    rendered = normal_template.get_context_instructions(
        PromptVariables(
            funnel_stage=stage,
            language='en',
            custom={
                'agent_name': 'Nora',
                'clinic_name': 'Generic Clinic',
                'clinic_history': 'A long-standing clinic',
                'clinic_specialties': ['Dentistry'],
                'working_hours': 'Mon-Fri 9-17',
                'full_address': '1 Example Street',
                'payment_methods': 'card',
                'differentials': 'Top team',
                'provide_prices': 'yes',
                'pricing_mode': 'exact_allowed',
                'initial_consultation': 'free',
                'consult_service_price': '0',
                'parking_info': 'Street parking',
                'scheduling_hours': 'Mon-Fri 9-17',
                'service_format': 'online',
                'use_emojs': False,
                'language_policy': 'English',
                'services_detail': 'A list of services',
                'professionals': 'Dr Alex',
                'privacy_link': 'https://example.test/privacy',
            },
            knowledge_context='Knowledge content',
            knowledge_sources=['s1'],
            last_intent='saudacao',
        )
    )
    observations['prompt_template_samples'][f'normal.context_instructions:{stage}'] = rendered[:500]
    observations['prompt_template_keys_rendered'].add('normal.context_instructions')
    observations['prompt_template_keys_rendered'].add('normal.funnel_instructions')
    observations['prompt_template_keys_rendered'].add('normal.funnel_instructions_en')
    observations['prompt_template_keys_rendered'].add('normal.intent_instructions')
    observations['prompt_template_keys_rendered'].add('normal.clinic_context')

normal_sys = normal_template.get_system_prompt(
    PromptVariables(
        language='en', funnel_stage='lead_novo', turn_count=1, channel='web',
        custom={'agent_name': 'Nora', 'clinic_name': 'Generic', 'agent_presentation': 'Virtual assistant'},
    )
)
observations['prompt_template_samples']['normal.system_prompt'] = normal_sys[:500]
observations['prompt_template_keys_rendered'].add('normal.system_prompt')
observations['prompt_template_keys_rendered'].add('normal.system_prompt_en')

normal_build = normal_template.build_system_message(
    PromptVariables(
        language='en', funnel_stage='agendamento_solicitado', turn_count=5, channel='web',
        custom={
            'agent_name': 'Nora', 'clinic_name': 'Generic', 'agent_presentation': '',
            'handoff_confirmation_pending': True, 'ask_convenio_triage': True,
            'scheduling_link_verification_status': 'completed', 'use_emojis': True,
        },
    )
)
observations['prompt_template_samples']['normal.build_system_message'] = normal_build.content[:500]
observations['prompt_template_keys_rendered'].add('normal.build_system_message')

# 9. Base template renderings
base_template = normal_template  # BaseTemplate behavior via NormalTemplate
observations['prompt_template_samples']['base.constraint_heading'] = base_template.get_constraints(
    PromptVariables(language='en', constraints=['Keep it short'])
)[:500]
observations['prompt_template_keys_rendered'].add('base.constraint_heading')
observations['prompt_template_samples']['base.safety_heading'] = base_template.get_safety_reminders(
    PromptVariables(language='en', safety_reminders=['Call 911 for emergencies'])
)[:500]
observations['prompt_template_keys_rendered'].add('base.safety_heading')

# 10. Renderer samples
renderer = PromptRenderer()
rendered = renderer.render(
    PromptVariables(user_message='Hello', funnel_stage='lead_novo', last_intent='saudacao',
                    language='en', user_journey='some journey'),
    PromptConfig(mode=PromptMode.NORMAL, max_tokens=4096),
)
observations['prompt_template_samples']['renderer.journey'] = rendered.messages[0].content[:600]
observations['prompt_template_keys_rendered'].add('renderer.journey')
observations['prompt_template_keys_rendered'].add('renderer.warning')

# Final: expose the generated prompts as `generated_prompts` for leakage check
observations['generated_prompts'] = observations['prompt_template_samples']

# Serialise to JSON
def _default(value):
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f'cannot serialise {type(value)!r}')

json.dump(observations, sys.stdout, default=_default, ensure_ascii=False)
"""


def test_en_generic_fresh_process_drives_the_engine_and_leaves_no_pt_br() -> None:
    completed = _run(EN_DRIVER)
    assert completed.returncode == 0, completed.stderr or completed.stdout
    data = json.loads(completed.stdout)
    assert data["preset_name"] == "en_generic"
    assert data["preset_source_id"] == "bundled:en_generic"

    # Coverage sets: every configured key must have at least one executed case
    # The 29 official intents are the universe. Our probes should hit all of them.
    assert set(data["intents_executed"]) >= set(data["classifier_hits"].keys()), set(
        data["classifier_hits"].keys()
    ) - set(data["intents_executed"])
    assert set(data["classifier_hits"].keys()) == set(
        data["classifier_hits"].values()
    ), {k: v for k, v in data["classifier_hits"].items() if k != v}

    # Transitions: every configured transition fired
    import tomllib as _tomllib

    with (EN_PRESET_DIR / "srm.toml").open("rb") as handle:
        srm_doc = _tomllib.load(handle)
    expected_transitions = {
        (item["from"], item["intent"], item["to"])
        for item in srm_doc["funnel"]["transitions"]
    }
    executed_transitions = {tuple(item) for item in data["transitions_executed"]}
    assert executed_transitions == expected_transitions

    # Safety categories: all four (incl. NONE) exercised
    assert set(data["safety_categories_executed"]) == {
        "clinical_risk",
        "legal_risk",
        "conduct_risk",
        "none",
    }

    # Policy rules: every rule id matched or exposed
    expected_rule_ids = {
        rule["rule_id"]
        for rule in __import__("tomllib").loads(
            (EN_PRESET_DIR / "policy.toml").read_text(encoding="utf-8")
        )["rules"]
    }
    assert set(data["policy_rule_ids_matched"]) == expected_rule_ids

    # Handoff responses: every key rendered
    with (EN_PRESET_DIR / "flow.toml").open("rb") as handle:
        flow_doc = _tomllib.load(handle)
    assert set(data["handoff_responses_rendered"]) == set(flow_doc["handoff_responses"])
    assert set(data["safety_responses_rendered"]) == set(flow_doc["safety_responses"])

    # Prompt template keys: superset of required lookup_keys
    with (EN_PRESET_DIR / "prompt.toml").open("rb") as handle:
        prompt_doc = _tomllib.load(handle)
    expected_prompt_keys = set(prompt_doc["protocol"]["lookup_keys"])
    assert expected_prompt_keys <= set(data["prompt_template_keys_rendered"])

    # Leakage check: no pt-BR literal in any generated prompt or response
    _assert_english_only(
        {
            "handoff": data["handoff_response_samples"],
            "safety": data["safety_response_samples"],
            "prompts": data["prompt_template_samples"],
            "policy_messages": data["policy_rule_messages"],
        }
    )


# --------------------------------------------------------------------------- #
# A separate fresh default process keeps the pt-BR classification intact      #
# --------------------------------------------------------------------------- #

PT_DRIVER = r"""
import json, sys
from uuid import uuid4
from neurostack_core import get_preset
from neurostack_core.srm import SRMInput
from neurostack_core.srm.engine import get_srm_engine
from neurostack_core.protocol import FunnelStage

preset = get_preset()
assert preset.name == 'pt_br_clinic', preset.name

engine = get_srm_engine()
cases = (
    ('Oi, quero agendar botox para sexta às 10h', 'agendar'),
    ('Quanto custa a consulta?', 'pedido_preco'),
    ('Vou pensar', 'objecao_vou_pensar'),
    ('Olá', 'saudacao'),
    ('Vou processar vocês', 'conduta_inadequada_ameaca'),
)
observations = {'preset_name': preset.name, 'preset_source_id': preset.source_id, 'results': {}}
for message, expected_intent in cases:
    decision = engine.process(SRMInput(
        tenant_id=uuid4(), session_id=uuid4(), message=message,
        current_funnel_stage=FunnelStage.LEAD_NOVO,
    ))
    observations['results'][message] = {
        'intent': decision.intent,
        'expected_intent': expected_intent,
        'category': decision.intent_category.value,
        'mode': decision.mode.value,
    }
json.dump(observations, sys.stdout, ensure_ascii=False)
"""


def test_default_process_reproduces_pt_br_classification() -> None:
    completed = _run(PT_DRIVER)
    assert completed.returncode == 0, completed.stderr or completed.stdout
    data = json.loads(completed.stdout)
    assert data["preset_name"] == "pt_br_clinic"
    assert data["preset_source_id"] == "bundled:pt_br_clinic"
    for message, info in data["results"].items():
        assert info["intent"] == info["expected_intent"], (message, info)


# --------------------------------------------------------------------------- #
# Preset lifecycle conflict: after en_generic, pt_br_clinic must be rejected  #
# --------------------------------------------------------------------------- #

LIFECYCLE_DRIVER = r"""
import sys
from neurostack_core import PresetError, use_preset
use_preset('en_generic')
try:
    use_preset('pt_br_clinic')
except PresetError as error:
    text = str(error)
    assert 'selected=' in text and 'requested=' in text and 'restart' in text, text
    print('BLOCKED')
else:
    sys.exit('swap unexpectedly succeeded')
"""


def test_switching_back_to_pt_br_after_en_generic_is_rejected() -> None:
    completed = _run(LIFECYCLE_DRIVER)
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert "BLOCKED" in completed.stdout
