"""
NeuroStack - Config Service
Minimal service layer for CRUD clinic configuration.
Maps flat frontend fields to JSONB bucket columns.
"""

from typing import Any, Union

from src.models.clinic_config_table import ClinicConfigTable


# ============================================
# Field-to-Bucket Mapping
# ============================================

FIELD_TO_BUCKET: dict[str, str] = {
    # persona
    "use_personal_name": "persona",
    "agent_display_name": "persona",
    "clinic_display_name": "persona",
    # identity
    "clinic_name": "identity",
    "main_domain": "identity",
    "agent_name": "identity",
    "agent_presentation": "identity",
    "ai_disclosure": "identity",
    "use_emojis": "identity",
    "clinic_history": "identity",
    "clinic_specialties": "identity",
    # operations
    "full_address": "operations",
    "parking_info": "operations",
    "working_hours": "operations",
    "scheduling_hours": "operations",
    "service_format": "operations",
    "online_services_desc": "operations",
    "presential_services_desc": "operations",
    # business_rules
    "accepts_insurance": "business_rules",
    "insurance_list": "business_rules",
    "initial_consultation": "business_rules",
    "provide_prices": "business_rules",
    "differentials": "business_rules",
    "language_policy": "business_rules",
    "language_policy_custom": "business_rules",
    "agent_language": "business_rules",
    # lgpd
    "lgpd_enabled": "lgpd",
    "lgpd_message": "lgpd",
    "lgpd_message_en": "lgpd",
    "lgpd_custom_link": "lgpd",
    "clinic_legal_name": "lgpd",
    "clinic_cnpj": "lgpd",
    "clinic_contact": "lgpd",
    "dpo_name": "lgpd",
    "dpo_contact": "lgpd",
    # handoff
    "handoff_active": "handoff",
    "handoff_period": "handoff",
    "handoff_cycle_days": "handoff",
    "standby_mode": "handoff",
    "handoff_sectors": "handoff",
    "default_mode": "handoff",
    "trigger_message": "handoff",
    "reactivation_delay_hours": "handoff",
    # top-level array buckets (field name == bucket name)
    "professionals": "professionals",
    "services": "services",
    # api_credentials
    "whatsapp_provider": "api_credentials",
    "zapi_instance_id": "api_credentials",
    "zapi_token": "api_credentials",
    "evolution_api_url": "api_credentials",
    "evolution_instance_name": "api_credentials",
    "evolution_api_key": "api_credentials",
    "llm_api_key": "api_credentials",
    "llm_api_url": "api_credentials",
    "llm_provider": "api_credentials",
    "llm_model_name": "api_credentials",
    "whisper_api_key": "api_credentials",
    "whisper_model": "api_credentials",
    "audio_max_seconds": "api_credentials",
}

# All JSONB bucket column names on ClinicConfigTable
ALL_BUCKETS = [
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
    "api_credentials",
]

# Buckets that are stored as arrays (not dicts)
ARRAY_BUCKETS = {"professionals", "services"}


def flatten_config(config_row: ClinicConfigTable) -> dict[str, Any]:
    """
    Merge all JSONB bucket columns into a single flat dict.

    Dict buckets have their keys promoted to the top level.
    Array buckets (professionals, services) appear as top-level keys.
    Also includes clinic_id and version.
    """
    flat: dict[str, Any] = {
        "clinic_id": str(config_row.clinic_id) if config_row.clinic_id else None,
        "version": config_row.version,
    }

    for bucket in ALL_BUCKETS:
        data = getattr(config_row, bucket, None) or ({} if bucket not in ARRAY_BUCKETS else [])
        if bucket in ARRAY_BUCKETS:
            # Array buckets go directly as a top-level key
            flat[bucket] = data
        else:
            # Dict buckets are spread into the flat dict
            if isinstance(data, dict):
                flat.update(data)

    return flat


def unflatten_patch(patch: dict[str, Any]) -> dict[str, Union[dict[str, Any], list]]:
    """
    Group patch fields by their target JSONB bucket.

    Returns a dict of {bucket_name: {field: value, ...}} for dict buckets,
    or {bucket_name: [list_value]} for array buckets.
    """
    buckets: dict[str, Any] = {}

    for field, value in patch.items():
        bucket = FIELD_TO_BUCKET.get(field)
        if bucket is None:
            # Unknown fields are silently ignored
            continue

        if bucket in ARRAY_BUCKETS:
            # Array buckets: the value replaces the whole array
            buckets[bucket] = value
        else:
            buckets.setdefault(bucket, {})
            buckets[bucket][field] = value

    return buckets
