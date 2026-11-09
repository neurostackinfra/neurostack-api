"""Compatibility contracts for the canonical funnel-stage protocol."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from collections.abc import Mapping, MutableMapping, MutableSequence, MutableSet
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

import pytest

from neurostack_core.presets import get_preset
from neurostack_core.protocol import (
    MEDIA_BINDABLE_INTENT_IDS,
    MEDIA_BINDABLE_INTENT_SEQUENCE,
    MEDIA_IMPLICIT_INTENT_IDS,
    MEDIA_IMPLICIT_INTENT_SEQUENCE,
    MEDIA_TOPIC_INTENTS,
    MEDIA_TOPIC_SEQUENCE,
    OFFICIAL_INTENT_IDS,
    OFFICIAL_INTENT_SEQUENCE,
)
from neurostack_core.protocol import FunnelStage as ProtocolFunnelStage
from neurostack_core.srm import FunnelStage as PublicFunnelStage
from neurostack_core.srm import engine as srm_engine
from neurostack_core.srm import (
    funnel_detector,
    handoff_decider,
    intent_classifier,
    rag_decider,
    safety_detector,
)
from neurostack_core.srm.models import FunnelStage as ModelFunnelStage
from neurostack_core.srm.safety_detector import SafetyDetector
from src.api.schemas.sessions import CognitiveState
from src.api.schemas.sessions import FunnelStage as ApiFunnelStage
from src.models.session import Session
from src.services.session_service import SessionService

ROOT = Path(__file__).resolve().parents[2]
BUNDLED = (
    ROOT
    / "packages"
    / "neurostack-core"
    / "neurostack_core"
    / "presets"
    / "pt_br_clinic"
)


def test_api_and_srm_reexport_the_exact_protocol_class_and_default() -> None:
    assert PublicFunnelStage is ProtocolFunnelStage
    assert ModelFunnelStage is ProtocolFunnelStage
    assert ApiFunnelStage is ProtocolFunnelStage
    assert CognitiveState.model_fields["funnel_stage"].default is (
        ProtocolFunnelStage.LEAD_NOVO
    )
    assert CognitiveState().funnel_stage is ProtocolFunnelStage.LEAD_NOVO
    assert ProtocolFunnelStage.LEAD_NOVO.value == "lead_novo"


def test_every_preexisting_persisted_funnel_stage_is_literally_pinned() -> None:
    expected = {
        "LEAD_NOVO": "lead_novo",
        "EM_QUALIFICACAO": "em_qualificacao",
        "QUALIFICADO": "qualificado",
        "FOLLOWUP": "followup",
        "AGENDAMENTO_SOLICITADO": "agendamento_solicitado",
        "AGENDADO": "agendado",
        "PACIENTE_ATIVO": "paciente_ativo",
        "POS_ATENDIMENTO": "pos_atendimento",
        "FIDELIZACAO": "fidelizacao",
        "INATIVO": "inativo",
        "STANDBY": "standby",
    }

    assert {member.name: member.value for member in ProtocolFunnelStage} == expected


def _assert_deeply_immutable(value: object) -> None:
    assert not isinstance(
        value, (MutableMapping, MutableSequence, MutableSet, bytearray)
    ), f"reachable mutable {type(value).__name__}: {value!r}"
    if isinstance(value, Mapping):
        for key, item in value.items():
            _assert_deeply_immutable(key)
            _assert_deeply_immutable(item)
    elif isinstance(value, (tuple, frozenset)):
        for item in value:
            _assert_deeply_immutable(item)


def test_all_preset_derived_srm_module_defaults_are_deeply_immutable() -> None:
    defaults = (
        srm_engine.INTENT_ACTIONS,
        srm_engine.CATEGORY_ACTIONS,
        funnel_detector.FUNNEL_STAGES,
        funnel_detector.TRANSITION_TRIGGERS,
        funnel_detector.TURN_THRESHOLDS,
        handoff_decider.EXPLICIT_HANDOFF_PATTERNS,
        intent_classifier.INTENT_DEFINITIONS,
        intent_classifier.EN_INTENT_PATTERNS,
        intent_classifier._SAFETY_GUARD_PATTERNS,
        rag_decider.RAG_TRIGGER_PATTERNS,
        rag_decider.RAG_INTENT_CATEGORIES,
        safety_detector.SAFETY_PATTERNS,
    )

    for default in defaults:
        _assert_deeply_immutable(default)

    with pytest.raises(TypeError):
        safety_detector.SAFETY_PATTERNS[
            safety_detector.SafetyCategory.CLINICAL_RISK
        ] = {}  # type: ignore[index]
    clinical = safety_detector.SAFETY_PATTERNS[
        safety_detector.SafetyCategory.CLINICAL_RISK
    ]
    with pytest.raises(TypeError):
        clinical["patterns"] = ()  # type: ignore[index]


def test_custom_safety_patterns_are_isolated_from_defaults_and_later_instances() -> (
    None
):
    category = safety_detector.SafetyCategory.CLINICAL_RISK
    default_patterns = safety_detector.SAFETY_PATTERNS[category]["patterns"]
    preset_patterns = get_preset().srm["safety_patterns"][category.value]["patterns"]

    customized = SafetyDetector(
        custom_patterns={category: {"patterns": [r"isolated-custom-pattern"]}}
    )
    customized.patterns[category]["keywords"].append("isolated-custom-keyword")
    later = SafetyDetector()

    assert r"isolated-custom-pattern" in customized.patterns[category]["patterns"]
    assert "isolated-custom-keyword" in customized.patterns[category]["keywords"]
    assert safety_detector.SAFETY_PATTERNS[category]["patterns"] == default_patterns
    assert (
        get_preset().srm["safety_patterns"][category.value]["patterns"]
        == preset_patterns
    )
    assert r"isolated-custom-pattern" not in later.patterns[category]["patterns"]
    assert "isolated-custom-keyword" not in later.patterns[category]["keywords"]


@pytest.mark.parametrize("stage", list(ProtocolFunnelStage))
def test_every_persisted_stage_deserializes_and_json_round_trips(
    stage: ProtocolFunnelStage,
) -> None:
    state = CognitiveState.model_validate({"funnel_stage": stage.value})
    assert state.funnel_stage is stage

    encoded = state.model_dump_json()
    restored = CognitiveState.model_validate_json(encoded)
    assert restored.funnel_stage is stage
    assert restored.model_dump(mode="json")["funnel_stage"] == stage.value


def test_session_service_preserves_the_existing_unknown_stage_fallback() -> None:
    now = datetime.now(timezone.utc)
    session = Session(
        id=uuid4(),
        tenant_id=uuid4(),
        user_id=None,
        channel="web",
        cognitive_state={"funnel_stage": "legacy_unknown_stage"},
        session_metadata={},
        started_at=now,
        last_activity_at=now,
        is_active=True,
        message_count=0,
        handoff_status=None,
    )

    response = SessionService()._session_to_response(session)
    assert response.cognitive_state.funnel_stage is ProtocolFunnelStage.LEAD_NOVO


def test_canonical_protocol_values_exactly_match_the_preset() -> None:
    with (BUNDLED / "srm.toml").open("rb") as handle:
        document = tomllib.load(handle)

    canonical = {stage.value for stage in ProtocolFunnelStage}
    assert set(document["protocol"]["funnel_stages"]) == canonical
    assert set(document["funnel"]["stages"]) == canonical


@pytest.mark.parametrize("mode", ("missing", "extra"))
def test_missing_or_extra_preset_stage_ids_raise_preset_error(
    tmp_path: Path, mode: str
) -> None:
    preset = tmp_path / mode
    shutil.copytree(BUNDLED, preset)
    path = preset / "srm.toml"
    text = path.read_text(encoding="utf-8")
    marker = '["funnel"]'
    head, tail = text.split(marker, 1)
    if mode == "missing":
        tail = tail.replace("  '''standby''',\n", "", 1)
    else:
        tail = tail.replace(
            "  '''standby''',\n",
            "  '''standby''',\n  '''stage_que_nao_existe''',\n",
            1,
        )
    path.write_text(head + marker + tail, encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import pathlib, sys\n"
                "from neurostack_core import PresetError, use_preset\n"
                "try:\n"
                "    use_preset(pathlib.Path(sys.argv[1]))\n"
                "except PresetError as error:\n"
                "    print(error)\n"
                "else:\n"
                "    raise SystemExit('invalid stages unexpectedly loaded')\n"
            ),
            str(preset),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert "srm.toml" in completed.stdout
    assert "funnel.stages" in completed.stdout
    assert mode in completed.stdout


# envio-midia-agente Group 2: the intents a Central de Mídias file may be
# bound to, grouped in the four "Quando enviar" topics (owner, 2026-11-08).
# Zero tolerance: media never attaches to a scheduling flow that acts on an
# appointment, fora_do_horario enqueues a handoff, a complaint always hands
# off, and safety intents never carry media.
_MEDIA_TOPICS = {
    "pedido_preco": ("pedido_preco",),
    "agendar": ("agendar", "pre_atendimento"),
    "interesse_tratamento": (
        "interesse_tratamento",
        "comparacao_tratamentos",
        "objecao_vou_pensar",
        "detalhes_servicos",
        "cultura_historia",
        "qualificacao_publico",
        "informacoes_profissionais",
    ),
    "pos_atendimento_duvida": ("pos_atendimento_duvida",),
}
_MEDIA_BINDABLE_INTENTS = (
    "interesse_tratamento",
    "pedido_preco",
    "comparacao_tratamentos",
    "objecao_vou_pensar",
    "agendar",
    "detalhes_servicos",
    "pre_atendimento",
    "cultura_historia",
    "qualificacao_publico",
    "informacoes_profissionais",
    "pos_atendimento_duvida",
)
_MEDIA_IMPLICIT_INTENTS = ("saudacao", "faq")
_NEVER_MEDIA_BINDABLE = frozenset(
    {
        "reagendar",
        "cancelar",
        "confirmar_presenca",
        "disponibilidade_horarios",
        "fora_do_horario",
        "reclamacao",
        "conduta_inadequada_ameaca",
        "pos_atendimento_sintomas_preocupantes",
        "pedido_alterar_medicacao",
        "pedido_atendente_humano",
        "orcamento_existente",
        "cobranca_renegociacao",
        "documentos_e_comprovantes",
    }
)


def test_media_topics_are_the_four_quando_enviar_checkboxes_in_order() -> None:
    assert dict(MEDIA_TOPIC_INTENTS) == _MEDIA_TOPICS
    assert MEDIA_TOPIC_SEQUENCE == (
        "pedido_preco",
        "agendar",
        "interesse_tratamento",
        "pos_atendimento_duvida",
    )
    assert tuple(MEDIA_TOPIC_INTENTS) == MEDIA_TOPIC_SEQUENCE
    assert isinstance(MEDIA_TOPIC_INTENTS, MappingProxyType)
    assert all(isinstance(members, tuple) for members in MEDIA_TOPIC_INTENTS.values())


def test_media_topics_are_disjoint_official_and_in_canonical_order() -> None:
    members = [intent for group in MEDIA_TOPIC_INTENTS.values() for intent in group]
    assert len(members) == len(set(members))  # no intent in two topics
    assert set(members) <= OFFICIAL_INTENT_IDS
    for topic, group in MEDIA_TOPIC_INTENTS.items():
        assert topic in group  # each topic is named by one of its intents
        assert list(group) == [i for i in OFFICIAL_INTENT_SEQUENCE if i in group]


def test_media_bindable_intents_are_exactly_the_topics_intents() -> None:
    assert MEDIA_BINDABLE_INTENT_SEQUENCE == _MEDIA_BINDABLE_INTENTS
    assert MEDIA_BINDABLE_INTENT_IDS == frozenset(_MEDIA_BINDABLE_INTENTS)
    assert MEDIA_BINDABLE_INTENT_IDS == frozenset(
        intent for group in MEDIA_TOPIC_INTENTS.values() for intent in group
    )
    assert len(MEDIA_BINDABLE_INTENT_IDS) == len(MEDIA_BINDABLE_INTENT_SEQUENCE) == 11
    assert isinstance(MEDIA_BINDABLE_INTENT_SEQUENCE, tuple)
    assert isinstance(MEDIA_BINDABLE_INTENT_IDS, frozenset)


def test_media_implicit_intents_are_greeting_and_faq_and_never_bindable() -> None:
    assert MEDIA_IMPLICIT_INTENT_SEQUENCE == _MEDIA_IMPLICIT_INTENTS
    assert MEDIA_IMPLICIT_INTENT_IDS == frozenset(_MEDIA_IMPLICIT_INTENTS)
    assert MEDIA_IMPLICIT_INTENT_IDS <= OFFICIAL_INTENT_IDS
    assert not MEDIA_IMPLICIT_INTENT_IDS & MEDIA_BINDABLE_INTENT_IDS


def test_media_intents_are_official_and_never_act_on_an_appointment() -> None:
    assert MEDIA_BINDABLE_INTENT_IDS <= OFFICIAL_INTENT_IDS
    assert _NEVER_MEDIA_BINDABLE <= OFFICIAL_INTENT_IDS  # the names are real
    assert not MEDIA_BINDABLE_INTENT_IDS & _NEVER_MEDIA_BINDABLE
    assert not MEDIA_IMPLICIT_INTENT_IDS & _NEVER_MEDIA_BINDABLE
    # The canonical order the API stores and returns is the official one.
    official_order = [
        intent
        for intent in OFFICIAL_INTENT_SEQUENCE
        if intent in MEDIA_BINDABLE_INTENT_IDS
    ]
    assert list(MEDIA_BINDABLE_INTENT_SEQUENCE) == official_order
