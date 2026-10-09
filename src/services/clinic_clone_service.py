"""Pure constructors used by the clinic-cloning workflow."""

import copy
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid4

from src.models.clinic_config import ClinicConfig
from src.models.clinic_config_table import ClinicConfigTable
from src.models.knowledge import KnowledgeDocument
from src.models.media_asset import MediaAsset

_COPY_SUFFIX = re.compile(r"\s+cópia\s+(\d+)$")
_TENANT_NAME_MAX_LENGTH = 255

# Every MediaAsset column is either copied (this allowlist) or assigned by the
# destination (the regenerated set). A new column must be added deliberately to
# one of them; the service tests fail on any unclassified column.
MEDIA_ASSET_CLONE_FIELDS: tuple[str, ...] = (
    "display_name",
    "original_filename",
    "category",
    "mime_type",
    "size_bytes",
    "sha256",
    "payload",
    "usage_trigger",
    "auto_suggest",
)
MEDIA_ASSET_CLONE_REGENERATED_FIELDS: frozenset[str] = frozenset(
    {"id", "tenant_id", "created_at", "updated_at"}
)
_BUCKET_FIELDS: dict[str, tuple[str, ...]] = {
    "persona": (
        "clinic_display_name",
        "agent_display_name",
        "use_personal_name",
    ),
    "scheduling": (
        "has_scheduling_agent",
        "hours_rag_key",
        "scheduling_link_message",
    ),
    "pricing": (
        "disclosure_mode",
        "range_table_available",
        "exact_table_available",
        "range_format",
        "special_conditions_note_enabled",
    ),
    "post_care": (
        "standard_instructions_rag_key",
        "emergency_warning_text_enabled",
    ),
    "handoff": (
        "default_mode",
        "sector_defaults",
        "handoff_active",
        "standby_mode",
        "handoff_period",
        "handoff_cycle_hours",
        "handoff_cycle_days",
    ),
    "identity": (
        "clinic_name",
        "main_domain",
        "agent_name",
        "agent_presentation",
        "ai_disclosure",
        "use_emojis",
        "clinic_history",
        "clinic_specialties",
    ),
    "operations": (
        "full_address",
        "parking_info",
        "working_hours",
        "scheduling_hours",
        "timezone",
        "business_hours",
        "service_format",
        "online_services_desc",
        "presential_services_desc",
    ),
    "business_rules": (
        "accepts_insurance",
        "insurance_list",
        "initial_consultation",
        "provide_prices",
        "differentials",
        "language_policy",
        "language_policy_custom",
        "agent_language",
    ),
    "lgpd": (
        "lgpd_enabled",
        "lgpd_message",
        "lgpd_message_en",
        "lgpd_custom_link",
        "clinic_legal_name",
        "clinic_cnpj",
        "clinic_contact",
        "dpo_name",
        "dpo_contact",
    ),
}


def normalize_clone_root(name: str) -> str:
    """Return the original root of a clinic name, even when cloning a clone."""
    normalized = name.strip()
    match = _COPY_SUFFIX.search(normalized)
    if match is not None:
        normalized = normalized[: match.start()].rstrip()
    return normalized


def next_clone_name(source_name: str, existing_names: Sequence[str]) -> str:
    """Choose ``max(N) + 1`` for clones sharing the source's normalized root."""
    root = normalize_clone_root(source_name)
    if not root:
        raise ValueError("clone name root cannot be empty")
    suffix_pattern = re.compile(rf"^{re.escape(root)}\s+cópia\s+(\d+)$")
    suffixes = (
        int(match.group(1))
        for name in existing_names
        if (match := suffix_pattern.fullmatch(name.strip())) is not None
    )
    clone_name = f"{root} cópia {max(suffixes, default=0) + 1}"
    if len(clone_name) > _TENANT_NAME_MAX_LENGTH:
        raise ValueError(
            f"clone name exceeds Tenant.name maximum of {_TENANT_NAME_MAX_LENGTH} characters"
        )
    return clone_name


def derive_clone_slug(name: str, tenant_id: UUID) -> str:
    """Build a stable, URL-friendly and tenant-unique slug of at most 100 chars."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-") or "clinic"
    suffix = f"-{tenant_id.hex}"
    return f"{base[: 100 - len(suffix)].rstrip('-')}{suffix}"


def _allowlisted_bucket(value: Any, fields: tuple[str, ...]) -> Any:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        return value
    return {field: copy.deepcopy(value[field]) for field in fields if field in value}


def build_clinic_config_clone(
    source: ClinicConfigTable, clone_name: str
) -> dict[str, Any]:
    """Construct a validated persistence payload from the fixed config allowlist."""
    buckets = {
        bucket: _allowlisted_bucket(getattr(source, bucket), fields)
        for bucket, fields in _BUCKET_FIELDS.items()
    }
    if isinstance(buckets["identity"], dict):
        buckets["identity"]["clinic_name"] = clone_name
    if isinstance(buckets["scheduling"], dict):
        buckets["scheduling"]["scheduling_link_enabled"] = False
        buckets["scheduling"]["scheduling_link_url"] = None

    validated = ClinicConfig.model_validate(
        {
            **buckets,
            "professionals": copy.deepcopy(source.professionals or []),
            "services": copy.deepcopy(source.services or []),
        }
    )
    payload = validated.model_dump(mode="json", exclude={"clinic_id"})

    # This tenant-local SLA target is intentionally not cloneable.  Validation
    # materializes the model default even when it was absent from the allowlist,
    # so remove it from the persistence payload explicitly.
    payload["handoff"].pop("meta_minutos_atendimento", None)

    # Dedicated integration state is assigned independently from the source payload.
    payload["api_credentials"] = {}
    payload["scheduling"]["scheduling_link_enabled"] = False
    payload["scheduling"]["scheduling_link_url"] = None
    return payload


def build_knowledge_document_clone(
    source: KnowledgeDocument, destination_tenant_id: UUID
) -> KnowledgeDocument:
    """Create an independent, pending-index copy of an authored document."""
    return KnowledgeDocument(
        id=uuid4(),
        tenant_id=destination_tenant_id,
        title=source.title,
        content=source.content,
        content_type=source.content_type,
        document_metadata=copy.deepcopy(source.document_metadata),
        vector_id=None,
        is_indexed=False,
        chunk_count=0,
        size_bytes=len(source.content.encode("utf-8")),
        index_error=None,
    )


def build_media_asset_clone(
    source: Mapping[str, Any], destination_tenant_id: UUID
) -> MediaAsset:
    """Create an independent media copy from the explicit column allowlist.

    ``source`` holds exactly the allowlisted columns of one locked source row.
    Timestamps are left unset so the destination row receives its own.
    """
    return MediaAsset(
        id=uuid4(),
        tenant_id=destination_tenant_id,
        **{field: source[field] for field in MEDIA_ASSET_CLONE_FIELDS},
    )
