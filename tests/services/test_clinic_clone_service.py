from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.models.clinic_config_table import ClinicConfigTable
from src.models.knowledge import KnowledgeDocument
from src.services.clinic_clone_service import (
    build_clinic_config_clone,
    build_knowledge_document_clone,
    derive_clone_slug,
    next_clone_name,
    normalize_clone_root,
)

_EXPECTED_BUCKET_KEYS = {
    "persona": {
        "clinic_display_name",
        "agent_display_name",
        "use_personal_name",
    },
    "scheduling": {
        "has_scheduling_agent",
        "hours_rag_key",
        "scheduling_link_enabled",
        "scheduling_link_url",
        "scheduling_link_message",
    },
    "pricing": {
        "disclosure_mode",
        "range_table_available",
        "exact_table_available",
        "range_format",
        "special_conditions_note_enabled",
    },
    "post_care": {
        "standard_instructions_rag_key",
        "emergency_warning_text_enabled",
    },
    "handoff": {
        "default_mode",
        "sector_defaults",
        "handoff_active",
        "standby_mode",
        "handoff_period",
        "handoff_cycle_hours",
        "handoff_cycle_days",
    },
    "identity": {
        "clinic_name",
        "main_domain",
        "agent_name",
        "agent_presentation",
        "ai_disclosure",
        "use_emojis",
        "clinic_history",
        "clinic_specialties",
    },
    "operations": {
        "full_address",
        "parking_info",
        "working_hours",
        "scheduling_hours",
        "timezone",
        "business_hours",
        "service_format",
        "online_services_desc",
        "presential_services_desc",
    },
    "business_rules": {
        "accepts_insurance",
        "insurance_list",
        "initial_consultation",
        "provide_prices",
        "differentials",
        "language_policy",
        "language_policy_custom",
        "agent_language",
    },
    "lgpd": {
        "lgpd_enabled",
        "lgpd_message",
        "lgpd_message_en",
        "lgpd_custom_link",
        "clinic_legal_name",
        "clinic_cnpj",
        "clinic_contact",
        "dpo_name",
        "dpo_contact",
    },
}


class _AdversarialCloneSource:
    """Expose only cloneable fields and fail on credential/broad-copy access."""

    _allowed_fields = {*_EXPECTED_BUCKET_KEYS, "professionals", "services"}

    def __init__(self, source):
        self._source = source

    def __getattr__(self, name):
        if name not in self._allowed_fields:
            raise AssertionError(
                f"clone service accessed forbidden source field: {name}"
            )
        return getattr(self._source, name)


def _source_config(**overrides):
    values = {
        "tenant_id": uuid4(),
        "persona": {
            "clinic_display_name": "Olhos",
            "agent_display_name": "Lia",
            "use_personal_name": True,
            "future_secret": "excluded",
        },
        "scheduling": {
            "has_scheduling_agent": True,
            "hours_rag_key": "hours",
            "scheduling_link_enabled": True,
            "scheduling_link_url": "https://source.example/schedule",
            "scheduling_link_message": "Agende aqui",
            "webhook": "excluded",
        },
        "pricing": {
            "disclosure_mode": "range_allowed",
            "range_table_available": True,
            "exact_table_available": False,
            "range_format": "R$ X–Y",
            "special_conditions_note_enabled": True,
        },
        "post_care": {
            "standard_instructions_rag_key": "aftercare",
            "emergency_warning_text_enabled": False,
        },
        "handoff": {
            "default_mode": "crm_task_create",
            "sector_defaults": {"agendamento": "imediato", "clinico": "rotina"},
            "meta_minutos_atendimento": 23,
            "handoff_active": "yes",
            "standby_mode": "active",
            "handoff_period": "cycle",
            "handoff_cycle_hours": 6,
            "handoff_cycle_days": None,
        },
        "identity": {
            "clinic_name": "Source",
            "main_domain": "eyes.example",
            "agent_name": "Lia",
            "agent_presentation": "Olá",
            "ai_disclosure": "Sou IA",
            "use_emojis": True,
            "clinic_history": "Desde 1990",
            "clinic_specialties": ["Retina"],
            "private_token": "excluded",
        },
        "operations": {
            "full_address": "Rua Um",
            "parking_info": "Coberto",
            "working_hours": "9–18",
            "scheduling_hours": "10–17",
            "timezone": "America/Sao_Paulo",
            "business_hours": {"mon": {"open": "09:00", "close": "18:00"}},
            "service_format": "hybrid",
            "online_services_desc": "Teleconsulta",
            "presential_services_desc": "Consultório",
        },
        "business_rules": {
            "accepts_insurance": True,
            "insurance_list": ["Plano A"],
            "initial_consultation": "60 min",
            "provide_prices": True,
            "differentials": "Especialistas",
            "language_policy": "custom",
            "language_policy_custom": "Formal",
            "agent_language": "pt-BR",
        },
        "lgpd": {
            "lgpd_enabled": True,
            "lgpd_message": "Privacidade",
            "lgpd_message_en": "Privacy",
            "lgpd_custom_link": "https://eyes.example/privacy",
            "clinic_legal_name": "Olhos Ltda",
            "clinic_cnpj": "00.000.000/0001-00",
            "clinic_contact": "contact@example.com",
            "dpo_name": "Ana",
            "dpo_contact": "dpo@example.com",
        },
        "professionals": [{"name": "Dra. Ana", "tags": ["retina"]}],
        "services": [
            {
                "name": "Consulta",
                "service_format": "online",
                "details": {"duration": 60},
            }
        ],
        "api_credentials": {"llm_api_key": "never-copy-me"},
    }
    values.update(overrides)
    return ClinicConfigTable(**values)


def test_clone_names_use_root_and_max_suffix_without_reusing_gaps():
    names = ["Clínica Ocular", "Clínica Ocular cópia 1", "Clínica Ocular cópia 4"]

    assert normalize_clone_root("  Clínica Ocular cópia 2  ") == "Clínica Ocular"
    assert next_clone_name("Clínica Ocular", names) == "Clínica Ocular cópia 5"
    assert next_clone_name("Clínica Ocular cópia 4", names) == "Clínica Ocular cópia 5"


def test_clone_name_rejects_empty_normalized_root():
    with pytest.raises(ValueError, match="empty"):
        next_clone_name(" \t\n ", [])


def test_clone_name_accepts_exact_tenant_name_boundary():
    root = "x" * 247

    clone_name = next_clone_name(root, [])

    assert clone_name == f"{root} cópia 1"
    assert len(clone_name) == 255


@pytest.mark.parametrize("root_length", [248, 255])
def test_clone_name_rejects_generated_name_beyond_tenant_name_boundary(root_length):
    root = "x" * root_length
    assert len(normalize_clone_root(root)) == root_length

    with pytest.raises(ValueError, match="255"):
        next_clone_name(root, [])


def test_clone_of_clone_uses_normalized_root_at_name_boundary():
    root = "x" * 247

    assert next_clone_name(f"{root} cópia 3", [f"{root} cópia 8"]) == (
        f"{root} cópia 9"
    )


def test_slug_is_normalized_deterministic_unique_and_bounded():
    tenant_id = uuid4()
    slug = derive_clone_slug("Á Clínica Ocular cópia 12", tenant_id)

    assert slug == f"a-clinica-ocular-copia-12-{tenant_id.hex}"
    assert derive_clone_slug("x" * 200, tenant_id) == derive_clone_slug(
        "x" * 200, tenant_id
    )
    assert len(derive_clone_slug("x" * 200, tenant_id)) == 100
    assert derive_clone_slug("Clinic", uuid4()) != derive_clone_slug("Clinic", uuid4())


def test_config_clone_copies_every_allowlisted_field_and_applies_resets():
    source = _source_config()
    clone = build_clinic_config_clone(source, "Olhos cópia 1")

    assert clone["persona"] == {
        "clinic_display_name": "Olhos",
        "agent_display_name": "Lia",
        "use_personal_name": True,
    }
    assert clone["scheduling"] == {
        "has_scheduling_agent": True,
        "hours_rag_key": "hours",
        "scheduling_link_enabled": False,
        "scheduling_link_url": None,
        "scheduling_link_message": "Agende aqui",
    }
    assert clone["pricing"] == source.pricing
    assert clone["post_care"] == source.post_care
    assert clone["handoff"]["default_mode"] == "crm_task_create"
    assert "meta_minutos_atendimento" not in clone["handoff"]
    assert clone["handoff"]["handoff_active"] == "yes"
    assert clone["handoff"]["standby_mode"] == "active"
    assert clone["handoff"]["handoff_period"] == "cycle"
    assert clone["handoff"]["handoff_cycle_hours"] == 6
    assert clone["handoff"]["handoff_cycle_days"] is None
    assert clone["handoff"]["sector_defaults"] == {
        "agendamento": "imediato",
        "financeiro": "rotina",
        "administrativo": "rotina",
        "gerencia": "prioritario",
        "clinico": "rotina",
        "urgencia": "imediato",
        "fidelizacao": "rotina",
    }
    assert clone["identity"] == {
        key: value for key, value in source.identity.items() if key != "private_token"
    } | {"clinic_name": "Olhos cópia 1"}
    assert clone["operations"] == source.operations
    assert clone["business_rules"] == source.business_rules
    assert clone["lgpd"] == source.lgpd
    assert clone["professionals"] == source.professionals
    assert clone["services"] == source.services
    assert clone["api_credentials"] == {}
    assert "future_secret" not in clone["persona"]
    assert "webhook" not in clone["scheduling"]


def test_config_clone_never_reads_credentials_or_unknown_top_level_fields():
    source = _AdversarialCloneSource(_source_config())

    clone = build_clinic_config_clone(source, "Olhos cópia 1")

    assert set(clone) == {
        "version",
        *_EXPECTED_BUCKET_KEYS,
        "professionals",
        "services",
        "api_credentials",
    }
    for bucket, expected_keys in _EXPECTED_BUCKET_KEYS.items():
        assert set(clone[bucket]) == expected_keys
    assert set(clone["handoff"]["sector_defaults"]) == {
        "agendamento",
        "financeiro",
        "administrativo",
        "gerencia",
        "clinico",
        "urgencia",
        "fidelizacao",
    }
    assert clone["api_credentials"] == {}


def test_config_clone_is_deeply_independent_and_does_not_use_to_pydantic(monkeypatch):
    source = _source_config()
    monkeypatch.setattr(
        ClinicConfigTable,
        "to_pydantic",
        lambda self: pytest.fail("to_pydantic must not be used"),
    )

    clone = build_clinic_config_clone(source, "Clone")
    clone["identity"]["clinic_specialties"].append("Córnea")
    clone["operations"]["business_hours"]["mon"]["open"] = "08:00"
    clone["handoff"]["sector_defaults"]["agendamento"] = "rotina"
    clone["professionals"][0]["tags"].append("córnea")
    clone["services"][0]["details"]["duration"] = 30
    clone["services"][0]["service_format"] = "presential"

    assert source.identity["clinic_specialties"] == ["Retina"]
    assert source.operations["business_hours"]["mon"]["open"] == "09:00"
    assert source.handoff["sector_defaults"]["agendamento"] == "imediato"
    assert source.professionals[0]["tags"] == ["retina"]
    assert source.services[0]["details"]["duration"] == 60
    assert source.services[0]["service_format"] == "online"


def test_missing_config_values_use_current_model_defaults():
    empty_fields = {
        key: None
        for key in (
            "persona",
            "scheduling",
            "pricing",
            "post_care",
            "handoff",
            "identity",
            "operations",
            "business_rules",
            "lgpd",
            "professionals",
            "services",
        )
    }
    clone = build_clinic_config_clone(_source_config(**empty_fields), "Empty clone")

    assert clone["identity"]["clinic_name"] == "Empty clone"
    assert clone["persona"]["use_personal_name"] is False
    assert "meta_minutos_atendimento" not in clone["handoff"]
    assert clone["professionals"] == []
    assert clone["services"] == []
    assert clone["api_credentials"] == {}


def test_invalid_legacy_allowlisted_config_is_rejected():
    source = _source_config(
        operations={"business_hours": {"mon": {"open": "18:00", "close": "09:00"}}}
    )

    with pytest.raises(ValidationError):
        build_clinic_config_clone(source, "Invalid clone")


def test_knowledge_clone_preserves_authored_data_and_resets_derived_state():
    source = KnowledgeDocument(
        id=uuid4(),
        tenant_id=uuid4(),
        title="Pós-operatório 👁️",
        content="Ação 👁️\n漢字",
        content_type="markdown",
        document_metadata={"tags": ["visão"], "nested": {"lang": "pt"}},
        vector_id="source-vector",
        is_indexed=True,
        chunk_count=9,
        size_bytes=1,
        index_error="old error",
    )
    destination_id = uuid4()

    clone = build_knowledge_document_clone(source, destination_id)

    assert clone.id != source.id
    assert clone.tenant_id == destination_id
    assert (clone.title, clone.content, clone.content_type) == (
        source.title,
        source.content,
        source.content_type,
    )
    assert clone.document_metadata == source.document_metadata
    assert clone.vector_id is None
    assert clone.is_indexed is False
    assert clone.chunk_count == 0
    assert clone.index_error is None
    assert clone.size_bytes == len(source.content.encode("utf-8"))
    clone.document_metadata["tags"].append("independent")
    assert source.document_metadata["tags"] == ["visão"]
