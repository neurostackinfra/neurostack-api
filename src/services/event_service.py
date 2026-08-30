"""
NeuroStack API - Event Service
Business logic for event processing and management.
"""

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.events import (
    ContextOutput,
    ContextSource,
    EventDetailResponse,
    EventRequest,
    EventResponse,
    EventType,
    FlowMode,
    HandoffSummary,
    MessageHistory,
    PromptOutput,
    SRMDecision,
    StatePatch,
)
from src.config.logging import get_logger
from src.core.exceptions import EventStoreError
from src.memory import EventData, get_event_store
from src.models.session import CognitiveStateData
from src.observability.metrics import LLM_GENERATION_ATTEMPTS_TOTAL
from src.repositories import EventRepository
from src.repositories.session import SessionRepository
from src.services.api_credentials import (
    LLM_OVERLAY_FIELDS,
    build_cache_payload,
    compose_llm_credentials,
    config_cache_key,
    is_cache_v2_payload,
)

logger = get_logger(__name__)

# These replies bypass the LLM when a handoff is required. Keep this mapping
# at module scope so the production literal has one inspectable source for the
# scheduling-gate corpus. The scheduling message deliberately leaves calendar
# validation to the team; it never says that this agent confirmed a slot.
EN_HANDOFF_MESSAGES = {
    "user_request": (
        "Of course! I'll connect you with our team right away. "
        "Someone will follow up with you shortly."
    ),
    "safety_concern": (
        "To make sure you get the best care, I'll connect you with our "
        "specialist team. Someone will be in touch soon."
    ),
    "max_turns_exceeded": (
        "To give you the best support, I'll connect you with the right "
        "department on our team."
    ),
    "low_confidence": (
        "To make sure you get the most complete guidance, I'll connect you "
        "with our specialist team."
    ),
    "escalation": (
        "I understand your situation. I'll connect you with the right team "
        "who can help you in the best way."
    ),
    "complex_query": (
        "This deserves more detailed attention. I'll connect you with our "
        "specialist team."
    ),
    "scheduling": (
        "I'll connect you with our team, who will follow up and share the "
        "best times with you."
    ),
}

# ============================================
# Context Source Relevance Filter
# ============================================

# Fields always shown (they form the agent identity in every response)
_ALWAYS_RELEVANT_FIELDS = {"identity.clinic_name", "persona.agent_name"}


def _filter_relevant_sources(
    sources: list[dict],
    user_message: str,
    llm_response: Optional[str],
) -> list[dict]:
    """
    Keep only context sources whose content is reflected in the LLM response
    or whose keywords match the user's question. Identity fields always pass.
    """
    if not sources:
        return []

    combined = (user_message or "").lower() + " " + (llm_response or "").lower()
    if not combined.strip():
        return sources  # no response yet, show all

    relevant = []
    for src in sources:
        field = src.get("field", "")
        content = src.get("content", "")

        # Identity fields always relevant
        if field in _ALWAYS_RELEVANT_FIELDS:
            relevant.append(src)
            continue

        # Check if any significant word from the content appears in the combined text
        # Split content into tokens, check tokens >= 3 chars to avoid noise
        content_lower = content.lower()
        tokens = [
            t
            for t in content_lower.replace(",", " ").replace("-", " ").split()
            if len(t) >= 3
        ]

        if not tokens:
            continue

        # For grouped items (services, professionals), check individual names
        if field in ("services", "professionals"):
            names = [n.strip().lower() for n in content.split(",")]
            if any(name in combined for name in names if len(name) >= 2):
                relevant.append(src)
            continue

        # For boolean-like flags (provide_prices, accepts_insurance, use_emojis),
        # check if the topic is referenced in user message or response
        if content_lower in ("yes", "no", "true", "false", "both", "sim", "não"):
            # Map field to related keywords
            _FIELD_KEYWORDS: dict[str, list[str]] = {
                "business_rules.provide_prices": [
                    "preço",
                    "preco",
                    "valor",
                    "custo",
                    "price",
                    "quanto custa",
                ],
                "business_rules.accepts_insurance": [
                    "convênio",
                    "convenio",
                    "plano",
                    "insurance",
                ],
                "persona.use_emojis": ["emoji"],
                "persona.language_policy": ["idioma", "língua", "lingua", "language"],
                "operations.service_format": [
                    "formato",
                    "presencial",
                    "online",
                    "teleconsulta",
                ],
            }
            keywords = _FIELD_KEYWORDS.get(field, [])
            if any(kw in combined for kw in keywords):
                relevant.append(src)
            continue

        # General match: check if meaningful tokens from content appear in combined text
        matched = sum(1 for t in tokens if t in combined)
        if matched >= 1 and (matched / len(tokens)) >= 0.15:
            relevant.append(src)

    return relevant


# ============================================
# Clinic Config Cache (Redis, v2 marker + projected credentials only)
# ============================================
_CONFIG_CACHE_TTL = 900  # 15 minutes


async def _open_redis() -> Optional[Any]:
    """Return a Redis client or None if the module/URL is unavailable."""
    try:
        import redis.asyncio as aioredis  # noqa: WPS433 (local import for testability)

        from src.config.settings import get_settings

        client: Any = aioredis.from_url(get_settings().redis_url, decode_responses=True)
        return client
    except Exception:
        return None


async def _delete_cache_entry(tenant_id: UUID) -> bool:
    """Delete a stale cache entry. Returns True on confirmed delete, False on error."""
    client = await _open_redis()
    if client is None:
        return False
    try:
        await client.delete(config_cache_key(tenant_id))
        return True
    except Exception:
        return False
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


async def _get_cached_config(tenant_id: UUID) -> Optional[dict]:
    """Return the cached config only when the v2 marker matches.

    Any other outcome (missing entry, invalid JSON, stale marker) causes a DEL
    attempt. If the DEL fails, the caller must NOT reuse or renew the entry —
    we return ``None`` in every non-v2 case so the loader falls through to DB.
    """
    client = await _open_redis()
    raw: Optional[str] = None
    if client is not None:
        try:
            raw = await client.get(config_cache_key(tenant_id))
        except Exception:
            raw = None
        finally:
            try:
                await client.aclose()
            except Exception:
                pass

    if raw is None:
        # Missing entry: no need to DEL, no reuse.
        return None

    try:
        import json

        payload = json.loads(raw)
    except Exception:
        payload = None

    if is_cache_v2_payload(payload):
        return dict(payload) if isinstance(payload, dict) else None

    # Marker absent/invalid: DEL before any DB load. Failure to delete does not
    # allow reuse or renew — the loader will refuse to SET after DB load.
    await _delete_cache_entry(tenant_id)
    return None


async def _set_cached_config(tenant_id: UUID, config: dict) -> bool:
    """Store the v2-projected snapshot. Returns True on confirmed write.

    Callers must project via ``build_cache_payload`` (or a helper that already
    did) BEFORE invoking this function. This function refuses to write anything
    without the schema marker present.
    """
    if not is_cache_v2_payload(config):
        # Defensive: never write an unmarked/legacy payload.
        return False
    client = await _open_redis()
    if client is None:
        return False
    try:
        import json

        await client.set(
            config_cache_key(tenant_id),
            json.dumps(config),
            ex=_CONFIG_CACHE_TTL,
        )
        return True
    except Exception:
        return False
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


async def invalidate_clinic_config_cache(tenant_id: UUID) -> None:
    """Invalidate cached clinic config for a tenant. Call after config updates."""
    await _delete_cache_entry(tenant_id)


# ============================================
# DB-only loader (no JWT, no cache) with builder-owner resolution
# ============================================


async def _resolve_builder_owner_tenant_id(
    db_session: Any, tenant_id: UUID
) -> Optional[UUID]:
    """Resolve the single builder-owner tenant for the given clinic tenant.

    Rules (see WISH Group 3 deliverable 1):
      * Tenant.builder_id must be set.
      * Owner candidates are users where ``User.builder_id == Tenant.builder_id``
        AND ``User.role == "builder"``.
      * Group by distinct ``User.tenant_id``. Cardinality must be exactly 1;
        zero or multiple distinct tenants is ``builder_llm_owner_ambiguous``
        and returns ``None`` without any secret exposure.
    """
    from sqlalchemy import select

    from src.models.tenant import Tenant
    from src.models.user import User

    tenant_row = (
        await db_session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant_row is None or tenant_row.builder_id is None:
        return None

    distinct_stmt = (
        select(User.tenant_id)
        .where(
            User.builder_id == tenant_row.builder_id,
            User.role == "builder",
        )
        .distinct()
    )
    distinct_result = await db_session.execute(distinct_stmt)
    owner_tenant_ids = [row for row in distinct_result.scalars().all() if row]

    if len(owner_tenant_ids) != 1:
        logger.warning(
            "builder_llm_owner_ambiguous",
            tenant_id=str(tenant_id),
            builder_id=str(tenant_row.builder_id),
            candidate_count=len(owner_tenant_ids),
        )
        return None
    resolved: UUID = owner_tenant_ids[0]
    return resolved


async def _load_config_from_db(tenant_id: UUID) -> Optional[dict]:
    """Load a clinic config row with composed LLM overlay applied.

    Returns the flat dict WITH raw ``api_credentials`` (secrets present).
    Internal use only: callers must either project via ``build_cache_payload``
    before caching, or use ``_load_runtime_llm_credentials`` which keeps raw
    only in memory for the outbound call.
    """
    try:
        from sqlalchemy import select

        from src.models.clinic_config_table import ClinicConfigTable
        from src.repositories import get_db_session

        async with get_db_session() as db_session:
            row = (
                await db_session.execute(
                    select(ClinicConfigTable).where(
                        ClinicConfigTable.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None

            clinic_creds: dict = dict(row.api_credentials or {})
            builder_creds: Optional[dict] = None

            owner_tenant_id = await _resolve_builder_owner_tenant_id(
                db_session, tenant_id
            )
            if owner_tenant_id is not None and owner_tenant_id != tenant_id:
                builder_row = (
                    await db_session.execute(
                        select(ClinicConfigTable).where(
                            ClinicConfigTable.tenant_id == owner_tenant_id
                        )
                    )
                ).scalar_one_or_none()
                if builder_row is not None:
                    builder_creds = dict(builder_row.api_credentials or {})

            composed = compose_llm_credentials(clinic_creds, builder_creds)

            return {
                "identity": row.identity or {},
                "persona": row.persona or {},
                "operations": row.operations or {},
                "business_rules": row.business_rules or {},
                "services": row.services or {},
                "pricing": row.pricing or {},
                "handoff": row.handoff or {},
                "professionals": row.professionals or [],
                "api_credentials": composed,
                "lgpd": row.lgpd or {},
            }
    except Exception as exc:
        logger.warning("Failed to load clinic config", error=str(exc))
        return None


async def _load_runtime_llm_credentials(tenant_id: UUID) -> dict:
    """Return exactly the four LLM fields needed for an outbound LLM call.

    JWT-free, DB-only. Raw key stays in the returned dict for the duration of
    the caller's request; it is NEVER cached, projected, or reserialized.
    Returns an empty dict if the tenant/config is missing or resolution fails.
    """
    loaded = await _load_config_from_db(tenant_id)
    if loaded is None:
        return {}
    creds = loaded.get("api_credentials") or {}
    return {field: creds.get(field) for field in LLM_OVERLAY_FIELDS if creds.get(field)}


# Allowlisted bundles the private multimodal-fallback loader is allowed to
# return. Kept as an explicit constant so any drift is a wish-level change
# rather than a silent widening of the outbound payload. The RLHF consumer
# keeps using ``_load_runtime_llm_credentials`` — this loader is separate on
# purpose so the "exactly four LLM fields" invariant that route relies on
# stays byte-for-byte identical.
_MULTIMODAL_FALLBACK_BUNDLES: dict[str, tuple[str, ...]] = {
    "primary": (
        "llm_provider",
        "llm_api_url",
        "llm_api_key",
        "llm_model_name",
    ),
    "multimodal_text": (
        "multimodal_provider",
        "multimodal_api_url",
        "multimodal_api_key",
        "multimodal_model_name",
        "multimodal_text_fallback_enabled",
    ),
}


async def _load_multimodal_fallback_credentials(tenant_id: UUID) -> dict[str, Any]:
    """Return only the allowlisted `primary` + `multimodal_text` bundles.

    JWT-free, DB-only, no cache, no reserialization. Every returned field is
    named explicitly in :data:`_MULTIMODAL_FALLBACK_BUNDLES`; any other
    credential — from any capability — is dropped, so a future addition to
    ``api_credentials`` cannot silently reach the fallback factory. Returns an
    empty dict when the tenant/config is missing.
    """
    loaded = await _load_config_from_db(tenant_id)
    if loaded is None:
        return {}
    creds = loaded.get("api_credentials") or {}
    result: dict[str, Any] = {}
    for fields in _MULTIMODAL_FALLBACK_BUNDLES.values():
        for field in fields:
            if field in creds:
                result[field] = creds[field]
    return result


# ============================================
# LLM attempts contract (Wave 2, Group 3)
# ============================================
#
# The three functions below own the deterministic parts of the generation
# accounting contract so the routing loop in
# ``_convert_pipeline_result_to_response`` stays readable. They are pure and
# module-level on purpose: the tests exercise them directly and any drift
# would be a wish-level change rather than a silent contract widening.
#
# ``_is_usable_llm_content`` is the sole predicate that decides whether an
# LLM response can enter the accepted trio (``suggested_response`` /
# ``llm_usage`` / ``llm_model``). Empty / whitespace / non-string / non-dict
# never accepts, and never blocks the corresponding usage from entering the
# breakdown/fold.
#
# ``_normalize_llm_usage`` returns a ``(status, normalized)`` pair. The
# normalized dict only contains the three canonical integer fields — nothing
# else is copied, nothing missing is synthesised. Booleans are rejected up
# front because Python's ``bool`` is a subclass of ``int``.
#
# ``_fold_llm_usage_total`` is the ONLY writer of ``llm_usage_total``. It
# folds strictly across attempts with ``usage_status == "reported"``. No
# other code path may increment or mutate the aggregate.

_USAGE_FIELDS: tuple[str, str, str] = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
)


def _is_usable_llm_content(llm_result: Any) -> bool:
    """Return True iff ``llm_result`` is a mapping with a non-empty stripped
    ``content: str``. Any other shape (missing, wrong type, empty string,
    whitespace-only) is unusable and never enters the accepted trio.
    """
    if not isinstance(llm_result, Mapping):
        return False
    content = llm_result.get("content")
    if not isinstance(content, str):
        return False
    return bool(content.strip())


def _normalize_llm_usage(raw: Any) -> tuple[str, dict[str, int]]:
    """Return ``(usage_status, normalized_usage)``.

    * ``reported``  — ``raw`` is a mapping and every present canonical field
      is a non-negative integer (booleans rejected). Missing fields stay
      absent; they are NEVER synthesised (in particular ``total_tokens`` is
      never computed from ``prompt_tokens + completion_tokens``).
    * ``invalid``   — a present non-mapping usage payload, or a mapping with
      at least one canonical field that failed the integer / non-negative /
      non-bool check.
    * ``unavailable`` — absent usage (``None``), or a mapping with no
      canonical fields present.
    """
    if raw is None:
        return "unavailable", {}
    if not isinstance(raw, Mapping):
        return "invalid", {}

    present: dict[str, int] = {}
    seen = False
    for field in _USAGE_FIELDS:
        if field not in raw:
            continue
        seen = True
        value = raw[field]
        # Reject bool explicitly: Python `bool` is a subclass of `int`.
        if isinstance(value, bool):
            return "invalid", {}
        if not isinstance(value, int):
            return "invalid", {}
        if value < 0:
            return "invalid", {}
        present[field] = value

    if not seen:
        # Mapping with none of the canonical fields present — no reported
        # usage was carried at all.
        return "unavailable", {}
    return "reported", present


def _fold_llm_usage_total(attempts: list[dict[str, Any]]) -> dict[str, int]:
    """Deterministic fold of ``usage_status=="reported"`` entries.

    Sums each canonical field only across attempts that carry a reported
    usage. Fields present in some attempts and absent in others contribute
    their present values without being inferred anywhere else. Returns an
    empty dict when no attempt reported anything.
    """
    total: dict[str, int] = {}
    for entry in attempts:
        if entry.get("usage_status") != "reported":
            continue
        usage = entry.get("usage") or {}
        for field in _USAGE_FIELDS:
            value = usage.get(field)
            if isinstance(value, bool):
                # Defensive: normalise should have filtered this already, but
                # never let a stray bool enter the fold.
                continue
            if isinstance(value, int) and value >= 0:
                total[field] = total.get(field, 0) + value
    return total


def _model_for_breakdown(requested: Optional[str], reported: Any) -> str:
    """Return ``reported`` when it is a non-empty string; else ``requested``.

    Used for the per-attempt model attribution in ``llm_attempts`` so the
    downstream breakdown can prefer the provider-reported model identity and
    fall back to what the caller requested when the provider omitted it.
    """
    if isinstance(reported, str) and reported.strip():
        return reported
    return requested or ""


def _reported_model_or_none(reported: Any) -> Optional[str]:
    """Keep only a provider-reported model that is meaningful for attribution."""
    if isinstance(reported, str) and reported.strip():
        return reported
    return None


def _record_llm_generation_attempt(entry: Mapping[str, Any]) -> None:
    """Emit bounded metrics and a safe structured event for one closed call.

    The caller invokes this only after the attempt has its final outcome.
    Prompt text, generated content and credentials are intentionally absent
    from this event; the provider/model fields are diagnostics for logs only.
    """
    tier = str(entry["tier"])
    outcome = str(entry["outcome"])
    try:
        LLM_GENERATION_ATTEMPTS_TOTAL.labels(tier=tier, outcome=outcome).inc()
    except Exception as exc:  # noqa: BLE001 - telemetry must not fail generation
        logger.error(
            "Failed to record LLM generation attempt metric",
            error_type=type(exc).__name__,
        )

    logger.info(
        "llm_generation_attempt",
        tier=tier,
        ordinal=entry["attempt"],
        provider=entry["provider"],
        requested_model=entry["requested_model"],
        reported_model=entry["reported_model"],
        outcome=outcome,
        accepted=entry["accepted"],
        usage_status=entry["usage_status"],
    )


def _append_llm_attempt(attempts: list[dict[str, Any]], entry: dict[str, Any]) -> None:
    """Store and observe a final LLM attempt exactly once."""
    attempts.append(entry)
    _record_llm_generation_attempt(entry)


class EventService:
    """Service for processing and managing events."""

    def __init__(
        self,
        pipeline: Optional["CognitivePipeline"] = None,
        event_repo: Optional[EventRepository] = None,
    ):
        """
        Initialize the event service.

        Args:
            pipeline: Cognitive pipeline instance
            event_repo: Event repository instance
        """
        # Lazy import to avoid circular dependency
        if pipeline is None:
            from src.core.pipeline import get_cognitive_pipeline

            pipeline = get_cognitive_pipeline()
        self._pipeline = pipeline
        self._event_repo = event_repo

    async def _load_clinic_config(self, tenant_id: UUID) -> Optional[dict]:
        """Load projected clinic config from cache or DB.

        Public callers receive the v2 projected snapshot: ``api_credentials``
        never contains raw secrets. When a runtime consumer needs LLM key
        material for an outbound call, it MUST use
        ``_load_runtime_llm_credentials`` instead.
        """
        cached = await _get_cached_config(tenant_id)
        if cached is not None:
            return cached

        loaded = await _load_config_from_db(tenant_id)
        if loaded is None:
            return None

        payload = build_cache_payload(loaded)
        # SET is best-effort: failure never falls back to legacy formats.
        await _set_cached_config(tenant_id, payload)
        return payload

    async def _load_session_state(
        self,
        session_id: UUID,
        db: Optional[AsyncSession],
    ) -> CognitiveStateData:
        """Load cognitive state from PG Session, fallback to default."""
        if db:
            try:
                repo = SessionRepository(db)
                session_row = await repo.get_by_id(session_id)
                if session_row and session_row.cognitive_state:
                    state = session_row.get_cognitive_state()
                    logger.info(
                        "session_state_loaded",
                        state_source="pg",
                        session_id=str(session_id),
                        turn_count=state.turn_count,
                        funnel_stage=state.funnel_stage,
                    )
                    return state
            except Exception as exc:
                logger.warning(
                    "Failed to load session state from PG",
                    session_id=str(session_id),
                    error=str(exc),
                )
        logger.info(
            "session_state_loaded",
            state_source="default",
            session_id=str(session_id),
            turn_count=0,
        )
        return CognitiveStateData()

    async def _persist_session_state(
        self,
        session_id: UUID,
        result: "PipelineResult",
        db: Optional[AsyncSession],
    ) -> None:
        """Persist pipeline result back to PG Session cognitive_state.

        Intentionally does NOT swallow exceptions — the caller is responsible
        for rollback so that failures are detected *before* the slow LLM path.
        """
        if not db:
            return
        repo = SessionRepository(db)
        session_row = await repo.get_by_id(session_id)
        if session_row:
            cognitive = session_row.get_cognitive_state()
            cognitive.add_intent(result.intent)
            cognitive.funnel_stage = result.funnel_stage
            cognitive.sentiment_label = result.sentiment_label
            cognitive.sentiment_score = result.sentiment_score
            # Merge slots and flags from state_patch
            if result.state_patch:
                for k, v in result.state_patch.get("slots", {}).items():
                    cognitive.update_slot(k, v)
                for k, v in result.state_patch.get("flags", {}).items():
                    cognitive.set_flag(k, v)
                if result.state_patch.get("funnel_stage"):
                    cognitive.funnel_stage = result.state_patch["funnel_stage"]
            session_row.set_cognitive_state(cognitive)
            session_row.increment_message_count()

            # Handoff engine: persist pause state from pipeline result
            if result.should_pause:
                session_row.is_paused = True
                if result.paused_until:
                    from datetime import datetime

                    session_row.paused_until = datetime.fromisoformat(
                        result.paused_until
                    )
                else:
                    session_row.paused_until = None
                logger.info(
                    "session_pause_persisted",
                    extra={
                        "session_id": str(session_id),
                        "paused_until": result.paused_until,
                    },
                )

            await db.flush()

    async def _load_message_history(
        self,
        tenant_id: UUID,
        session_id: UUID,
        db: Optional[AsyncSession],
        limit: int = 20,
    ) -> list[dict[str, str]]:
        """Reconstruct conversation history from Event Store for multi-turn LLM context."""
        if not db:
            return []
        try:
            from sqlalchemy import or_, select

            from src.models.event import Event as EventModel

            stmt = (
                select(EventModel)
                .where(
                    EventModel.session_id == session_id,
                    EventModel.tenant_id == tenant_id,
                    or_(
                        EventModel.event_type == "message.in",
                        EventModel.event_type == "prompt.generated",
                    ),
                )
                .order_by(EventModel.created_at.desc())
                .limit(limit)
            )
            result = await db.execute(stmt)
            events = list(result.scalars().all())
            events.reverse()  # Chronological order

            total_available = len(events)
            history: list[dict[str, str]] = []
            for e in events:
                data = e.event_data or {}
                if e.event_type == "message.in":
                    content = data.get("content", "")
                    if content:
                        history.append({"role": "user", "content": content})
                elif e.event_type == "prompt.generated":
                    resp = data.get("suggested_response", "")
                    if resp:
                        history.append({"role": "assistant", "content": resp})

            if total_available >= limit:
                logger.info(
                    "history_truncated",
                    history_truncated=True,
                    session_id=str(session_id),
                    messages_available=total_available,
                    messages_sent=len(history),
                )
            return history
        except Exception as exc:
            logger.warning(
                "Failed to load message history",
                session_id=str(session_id),
                error=str(exc),
            )
            return []

    async def run_pipeline(
        self,
        request: EventRequest,
        session: Optional[AsyncSession] = None,
    ) -> tuple["PipelineResult", Optional[dict], list[dict]]:
        """
        Phase 1 — fast, DB-bound work: load state → run pipeline → flush state.

        The caller MUST commit (or rollback) the session immediately after this
        returns, *before* starting any slow I/O (LLM calls, event store writes).
        This ensures:
        - Overlapping calls for the same session_id see the updated cognitive_state
        - The pooled DB connection is released before the LLM round-trip
        - If the flush fails, the caller can rollback and abort without having
          wasted an LLM call or produced orphaned event-store rows

        Raises on persistence failure (does NOT swallow) so the route handler
        can detect the failure and rollback before calling finish_response().
        """
        # Load clinic config for the tenant (provides identity, services, pricing)
        clinic_config = await self._load_clinic_config(request.tenant_id)

        # Load session state from PG
        state = await self._load_session_state(request.session_id, session)

        # Load message history from Event Store
        message_history = await self._load_message_history(
            request.tenant_id, request.session_id, session
        )

        # Build pipeline input with loaded state
        from src.core.pipeline import PipelineInput

        pipeline_input = PipelineInput(
            tenant_id=request.tenant_id,
            session_id=request.session_id,
            message=request.payload.content,
            channel=request.payload.channel,
            user_id=request.payload.user_id,
            current_funnel_stage=state.funnel_stage,
            turn_count=state.turn_count,
            intent_history=state.intent_history,
            slots=state.slots,
            flags=state.flags,
            message_history=message_history,
            clinic_config=clinic_config,
            language=request.payload.language,
        )

        # Process through the complete pipeline (CPU-bound, no I/O)
        from src.core.pipeline import PipelineResult

        result: PipelineResult = await self._pipeline.process(pipeline_input)

        # Persist state back to PG (flush only — caller must commit)
        # Raises on failure so the caller can rollback before starting LLM
        await self._persist_session_state(request.session_id, result, session)

        return result, clinic_config, message_history

    async def finish_response(
        self,
        request: EventRequest,
        result: "PipelineResult",
        clinic_config: Optional[dict],
        message_history: list[dict],
    ) -> EventResponse:
        """
        Phase 2 — slow I/O: LLM call + event-store writes.

        Called only after the DB transaction from run_pipeline() has been
        committed, so the connection is already released when this runs.
        """
        # Convert pipeline result to API response (LLM call happens here)
        (
            response,
            llm_usage,
            llm_model,
            llm_attempts,
            llm_usage_total,
        ) = await self._convert_pipeline_result_to_response(
            result,
            user_message=request.payload.content,
            clinic_config=clinic_config,
            tenant_id=request.tenant_id,
            message_history=message_history,
            language=request.payload.language,
        )

        # Save events to Event Store (separate DB connections, committed independently)
        suggested_response = None
        if response.prompt and response.prompt.suggested_response:
            suggested_response = response.prompt.suggested_response
        await self._save_events_to_store(
            request,
            result,
            suggested_response=suggested_response,
            llm_usage=llm_usage,
            llm_model=llm_model,
            llm_attempts=llm_attempts,
            llm_usage_total=llm_usage_total,
        )

        return response

    async def process_event(
        self,
        request: EventRequest,
        session: Optional[AsyncSession] = None,
    ) -> EventResponse:
        """
        Backwards-compatible entry point (session=None path, used by tests and
        internal callers that don't need state persistence).

        The /v1/events route handler calls run_pipeline() + finish_response()
        directly so it can commit between the two phases.
        """
        result, clinic_config, message_history = await self.run_pipeline(
            request, session
        )
        # When session is provided the caller must commit — here we don't have
        # that responsibility, so just proceed to finish (stateless path).
        return await self.finish_response(
            request, result, clinic_config, message_history
        )

    async def _convert_pipeline_result_to_response(
        self,
        result: "PipelineResult",
        user_message: str = "",
        clinic_config: Optional[dict] = None,
        tenant_id: Optional[UUID] = None,
        message_history: Optional[list[dict[str, str]]] = None,
        language: str = "pt-BR",
    ) -> tuple[EventResponse, dict, str, list[dict[str, Any]], dict[str, int]]:
        """
        Convert pipeline result to API response.

        If an LLM provider is configured, calls it to generate suggested_response.
        On unusable primary output (empty/whitespace/non-string/non-mapping),
        or on primary transport/HTTP/JSON exceptions, this method also invokes
        the multimodal-text fallback exactly once with the same ``messages``
        and ``max_tokens`` when the tenant has opted in via
        ``multimodal_text_fallback_enabled`` and holds an operational
        provider bundle. See :func:`_is_usable_llm_content`,
        :func:`_normalize_llm_usage` and :func:`_fold_llm_usage_total` for
        the accepted trio / attempts / total contract.

        Args:
            result: Pipeline result
            user_message: Original user message for LLM context

        Returns:
            ``(response, llm_usage, llm_model, llm_attempts, llm_usage_total)``
            where the legacy trio attributes ONLY the accepted result and the
            two new fields carry the full attempt history + deterministic
            fold of every ``usage_status=="reported"`` entry.
        """
        decision = SRMDecision(
            intent=result.intent,
            intent_category=result.intent_category,
            funnel_stage=result.funnel_stage,
            action=result.action,
            confidence=result.confidence,
            safety_concern=result.safety_concern,
            safety_category=result.safety_category,
            sentiment_label=result.sentiment_label,
            sentiment_score=result.sentiment_score,
            sentiment_confidence=result.sentiment_confidence,
            needs_rag=result.needs_rag,
            handoff_required=result.handoff_required,
            handoff_reason=result.handoff_reason,
            reasoning=result.reasoning,
        )

        state_patch = StatePatch(
            slots=result.state_patch.get("slots", {}),
            funnel_stage=result.state_patch.get("funnel_stage"),
            flags=result.state_patch.get("flags", {}),
        )

        suggested_response = None
        llm_usage: dict = {}
        llm_model: str = ""
        llm_attempts: list[dict[str, Any]] = []
        language_code = (
            language or "pt-BR"
        )  # initialised here so handoff block can use it
        deterministic_response = getattr(result, "deterministic_response", None)
        if deterministic_response is not None:
            suggested_response = deterministic_response
        elif not result.handoff_required:
            # Only call LLM when NOT handing off.  When handoff fires, the canned
            # HANDOFF_RESPONSES message is the correct response — calling the LLM
            # risks wrong language ("atendente humano") and unwanted questions.
            try:
                from src.llm.client import (
                    get_multimodal_text_llm_client,
                    get_tenant_llm_client,
                )

                # Runtime LLM call: cached config only contains the non-secret
                # projection, so we resolve raw creds DB-only for this call.
                # Raw key stays in-memory for the outbound request only.
                api_creds = (
                    await _load_runtime_llm_credentials(tenant_id) if tenant_id else {}
                )
                llm = get_tenant_llm_client(tenant_id, api_creds)
                tenant_model = api_creds.get("llm_model_name") or None
                primary_provider = api_creds.get("llm_provider") or "unknown"
                if llm and user_message:
                    sys_prompt = result.system_prompt or ""
                    messages = [{"role": "system", "content": sys_prompt}]
                    # Insert conversation history for multi-turn context
                    for msg in message_history or []:
                        messages.append(msg)
                    messages.append({"role": "user", "content": user_message})
                    # Reinforce greeting for saudacao intent — LLM tends to echo patient's wrong greeting
                    if result.state_patch.get("last_intent") == "saudacao":
                        from datetime import datetime as _dt
                        from datetime import timedelta as _td
                        from datetime import timezone as _tz

                        _h = _dt.now(_tz(_td(hours=-3))).hour
                        _raw_emojis = (
                            (clinic_config or {}).get("identity", {}).get("use_emojis")
                        )
                        _use_emojis = _raw_emojis in (True, "yes", "true", "1", 1)
                        _emoji_hint = (
                            " Use 1-2 emojis na resposta. 😊" if _use_emojis else ""
                        )
                        if language_code == "en":
                            _g = (
                                "Good morning"
                                if _h < 12
                                else ("Good afternoon" if _h < 18 else "Good evening")
                            )
                            messages.append(
                                {
                                    "role": "system",
                                    "content": f'REMINDER: It is now {_dt.now(_tz(_td(hours=-3))).strftime("%H:%M")} BRT. Start your response with "{_g}!" — do NOT use any other greeting.{_emoji_hint}',
                                }
                            )
                        else:
                            _g = (
                                "Bom dia"
                                if _h < 12
                                else ("Boa tarde" if _h < 18 else "Boa noite")
                            )
                            messages.append(
                                {
                                    "role": "system",
                                    "content": f'LEMBRETE: Agora são {_dt.now(_tz(_td(hours=-3))).strftime("%H:%M")} BRT. Comece sua resposta com "{_g}!" — NÃO use outra saudação.{_emoji_hint}',
                                }
                            )

                    # ---- Primary attempts (up to 2 on unusable content) ----
                    # An exception on the primary (transport/HTTP/JSON) does
                    # NOT trigger a second primary attempt; it goes straight
                    # to the fallback path so the SAME messages/max_tokens
                    # cross exactly one alternate provider boundary.
                    primary_exception_bailed = False
                    for attempt in range(2):
                        logger.info(
                            "Calling LLM",
                            attempt=attempt + 1,
                            prompt_len=len(sys_prompt),
                            user_msg_len=len(user_message),
                            mode=result.mode,
                            tenant_model=tenant_model,
                        )
                        entry: dict[str, Any] = {
                            "tier": "primary",
                            "attempt": attempt + 1,
                            "provider": primary_provider,
                            "requested_model": tenant_model,
                            "reported_model": None,
                            "outcome": "error",
                            "accepted": False,
                            "usage_status": "unavailable",
                            "usage": {},
                        }
                        try:
                            llm_result = await llm.generate(
                                messages, model=tenant_model, max_tokens=300
                            )
                        except Exception as exc:
                            logger.error(
                                "LLM primary call failed",
                                attempt=attempt + 1,
                                error=str(exc),
                                exc_info=True,
                            )
                            # outcome/error, usage_unavailable — no tokens invented.
                            _append_llm_attempt(llm_attempts, entry)
                            primary_exception_bailed = True
                            break

                        # Successful await — populate the entry from what we got.
                        reported = (
                            llm_result.get("model")
                            if isinstance(llm_result, Mapping)
                            else None
                        )
                        entry["reported_model"] = _reported_model_or_none(reported)
                        raw_usage = (
                            llm_result.get("usage")
                            if isinstance(llm_result, Mapping)
                            else None
                        )
                        status, normalized = _normalize_llm_usage(raw_usage)
                        entry["usage_status"] = status
                        entry["usage"] = normalized
                        usable = _is_usable_llm_content(llm_result)
                        if usable:
                            entry["outcome"] = "response"
                            entry["accepted"] = True
                            _append_llm_attempt(llm_attempts, entry)
                            content_str = llm_result["content"]
                            suggested_response = content_str
                            llm_usage = dict(normalized)
                            llm_model = _model_for_breakdown(tenant_model, reported)
                            logger.info(
                                "LLM response generated",
                                length=len(content_str),
                                attempt=attempt + 1,
                            )
                            break

                        # Unusable content — decide invalid vs empty for
                        # observability. ``invalid`` = wrong shape/type;
                        # ``empty`` = mapping with str content that stripped
                        # down to nothing (the canonical retry trigger).
                        if not isinstance(llm_result, Mapping) or not isinstance(
                            llm_result.get("content"), str
                        ):
                            entry["outcome"] = "invalid"
                        else:
                            entry["outcome"] = "empty"
                        _append_llm_attempt(llm_attempts, entry)
                        logger.warning(
                            (
                                "LLM returned empty, retrying"
                                if attempt == 0
                                else "LLM empty after retry"
                            ),
                            mode=result.mode,
                        )

                    # ---- Multimodal-text fallback (at most one call) ----
                    # Runs when the primary did not produce an accepted
                    # response. Fail-closed factory: opt-in gate, operational
                    # provider, provider↔host coherence and complete bundle
                    # all validated inside ``get_multimodal_text_llm_client``.
                    # ``None`` → silently skip (no attempt entry).
                    if not any(a.get("accepted") for a in llm_attempts):
                        fallback_creds = (
                            await _load_multimodal_fallback_credentials(tenant_id)
                            if tenant_id
                            else {}
                        )
                        fb_client = get_multimodal_text_llm_client(
                            tenant_id, fallback_creds
                        )
                        if fb_client is not None:
                            fb_requested = fallback_creds.get("multimodal_model_name")
                            fb_provider = (
                                fallback_creds.get("multimodal_provider") or "unknown"
                            )
                            logger.info(
                                "Calling multimodal-text fallback LLM",
                                mode=result.mode,
                                requested_model=fb_requested,
                                after_primary_exception=primary_exception_bailed,
                            )
                            fb_entry: dict[str, Any] = {
                                "tier": "multimodal_fallback",
                                "attempt": 1,
                                "provider": fb_provider,
                                "requested_model": fb_requested,
                                "reported_model": None,
                                "outcome": "error",
                                "accepted": False,
                                "usage_status": "unavailable",
                                "usage": {},
                            }
                            try:
                                fb_result = await fb_client.generate(
                                    messages,
                                    model=fb_requested,
                                    max_tokens=300,
                                )
                            except Exception as exc:
                                logger.error(
                                    "Multimodal-text fallback LLM call failed",
                                    error=str(exc),
                                    exc_info=True,
                                )
                                _append_llm_attempt(llm_attempts, fb_entry)
                            else:
                                fb_reported = (
                                    fb_result.get("model")
                                    if isinstance(fb_result, Mapping)
                                    else None
                                )
                                fb_entry["reported_model"] = _reported_model_or_none(
                                    fb_reported
                                )
                                fb_raw_usage = (
                                    fb_result.get("usage")
                                    if isinstance(fb_result, Mapping)
                                    else None
                                )
                                fb_status, fb_normalized = _normalize_llm_usage(
                                    fb_raw_usage
                                )
                                fb_entry["usage_status"] = fb_status
                                fb_entry["usage"] = fb_normalized
                                if _is_usable_llm_content(fb_result):
                                    fb_entry["outcome"] = "response"
                                    fb_entry["accepted"] = True
                                    _append_llm_attempt(llm_attempts, fb_entry)
                                    fb_content = fb_result["content"]
                                    suggested_response = fb_content
                                    llm_usage = dict(fb_normalized)
                                    llm_model = _model_for_breakdown(
                                        fb_requested, fb_reported
                                    )
                                    logger.info(
                                        "Multimodal-text fallback response generated",
                                        length=len(fb_content),
                                    )
                                else:
                                    if not isinstance(
                                        fb_result, Mapping
                                    ) or not isinstance(fb_result.get("content"), str):
                                        fb_entry["outcome"] = "invalid"
                                    else:
                                        fb_entry["outcome"] = "empty"
                                    _append_llm_attempt(llm_attempts, fb_entry)
                                    logger.warning(
                                        "Multimodal-text fallback returned unusable content",
                                        outcome=fb_entry["outcome"],
                                    )
                else:
                    logger.warning(
                        "LLM skipped",
                        llm_configured=llm is not None,
                        has_message=bool(user_message),
                    )
            except Exception as exc:
                # Defensive: the primary/fallback bodies catch their own
                # transport errors and record attempt entries; this outer
                # except protects credential resolution and factory import
                # from failing silently.
                logger.error("LLM call failed", error=str(exc), exc_info=True)

        # Deterministic fold — the ONLY writer of ``llm_usage_total``.
        # Computed once after all attempts are collected so no code path can
        # increment or mutate the aggregate in parallel.
        llm_usage_total: dict[str, int] = _fold_llm_usage_total(llm_attempts)

        # Observability guard: flag when a monetary token appears in the
        # finalized response while atendimento_tipo != particular.
        # Increments a Prometheus counter + emits a warning log; stays at
        # zero on the happy path (particular caller or no price emitted).
        #
        # Only armed when the clinic actually runs convenio triage
        # (accepts_insurance on) — otherwise atendimento_tipo is always
        # undefined and every legit price for a cash-only clinic would trip
        # the counter. Uses the effective slot (result.atendimento_tipo),
        # not the this-turn patch, so a "particular" chosen turns ago still
        # suppresses the guard.
        if suggested_response:
            _accepts_ins = (
                (clinic_config or {}).get("business_rules", {}).get("accepts_insurance")
            )
            _insurance_active = bool(
                _accepts_ins
                and str(_accepts_ins).lower() not in ("no", "false", "0", "none")
            )
            if _insurance_active:
                self._pipeline.check_response_price_gate(
                    response_text=suggested_response,
                    atendimento_tipo=result.atendimento_tipo,
                    tenant_id=tenant_id,
                )

            # Scheduling gate (Group 4): the agent has no ERP/calendar, so
            # any claim of confirmed appointment, available time slot, or
            # processed cancellation/reschedule is unfulfillable. We rewrite
            # the response before it leaves. Sibling of the insurance
            # branch — NOT nested inside — because this failure mode is
            # independent of whether the clinic accepts insurance.
            # Skipped for deterministic responses (link builder etc.) since
            # those are literal, LLM-free strings authored by the pipeline.
            if deterministic_response is None:
                _sched_category, _sched_replacement = (
                    self._pipeline.check_response_scheduling_gate(
                        response_text=suggested_response,
                        tenant_id=tenant_id,
                        language=language_code,
                        funnel_stage=result.funnel_stage,
                    )
                )
                if _sched_replacement is not None:
                    suggested_response = _sched_replacement

        # If handoff is required, append the handoff user message to the response.
        # When language is EN, use English fallback instead of the PT template message.
        if result.handoff_required and deterministic_response is None:
            # RULE: Never say "human agent", "transfer to human", or expose AI.
            # Always use "our team", "our specialist team", or "the right department".
            handoff_msg = result.handoff_user_message or ""
            if language_code == "en" and handoff_msg:
                # Use top-level handoff_reason field (handoff_summary only carries id/status/summary/urgency/sector/sla)
                handoff_reason = result.handoff_reason or "escalation"
                handoff_msg = EN_HANDOFF_MESSAGES.get(
                    handoff_reason, EN_HANDOFF_MESSAGES["escalation"]
                )
            if handoff_msg:
                if suggested_response:
                    suggested_response = f"{suggested_response}\n\n{handoff_msg}"
                else:
                    suggested_response = handoff_msg

        prompt = PromptOutput(
            system=result.system_prompt,
            instructions=result.instructions,
            constraints=result.constraints,
            context_summary=result.context_summary,
            suggested_response=suggested_response,
            deterministic=deterministic_response is not None,
        )

        recent_messages = [
            MessageHistory(
                role=m.get("role", "user"),
                content=m.get("content", ""),
                timestamp=m.get("timestamp", datetime.utcnow().isoformat()),
            )
            for m in result.recent_messages
        ]

        # Filter context_sources to only those relevant to the user question + LLM response
        filtered_sources = _filter_relevant_sources(
            result.context_sources, user_message, suggested_response
        )

        context = ContextOutput(
            recent_messages=recent_messages,
            session_summary=result.context_summary,
            context_sources=[ContextSource(**src) for src in filtered_sources],
            turn_count=result.turn_count,
        )

        # Map mode string to FlowMode enum
        mode_map = {
            "normal": FlowMode.NORMAL,
            "rag": FlowMode.RAG,
            "safety": FlowMode.SAFETY,
            "handoff": FlowMode.HANDOFF,
        }
        flow_mode = mode_map.get(result.mode, FlowMode.NORMAL)

        # Build handoff summary if available
        handoff_summary = None
        if result.handoff_summary:
            handoff_summary = HandoffSummary(
                reason=result.handoff_reason or "User request",
                summary=result.handoff_summary.get("summary", ""),
                key_info=result.state_patch.get("slots", {}),
                urgency=result.handoff_summary.get("urgency", "normal"),
            )

        return (
            EventResponse(
                event_id=result.event_id,
                decision=decision,
                state_patch=state_patch,
                prompt=prompt,
                context=context,
                mode=flow_mode,
                handoff_summary=handoff_summary,
                processed_at=result.processed_at,
                processing_time_ms=result.processing_time_ms,
            ),
            llm_usage,
            llm_model,
            llm_attempts,
            llm_usage_total,
        )

    async def _save_events_to_store(
        self,
        request: EventRequest,
        result: "PipelineResult",
        suggested_response: Optional[str] = None,
        llm_usage: Optional[dict] = None,
        llm_model: str = "",
        llm_attempts: Optional[list[dict[str, Any]]] = None,
        llm_usage_total: Optional[dict[str, int]] = None,
    ) -> None:
        """
        Save events to Event Store (non-blocking).

        Args:
            request: Original event request
            result: Pipeline result
            suggested_response: LLM-generated response (if available)
            llm_usage: Usage from the ACCEPTED result only (legacy trio).
            llm_model: Model attribution for the ACCEPTED result only.
            llm_attempts: Full attempt history (primary + fallback), ordered,
                one entry per initiated call. Serialised as-is into
                ``message.in`` metadata for downstream consumers.
            llm_usage_total: Deterministic fold over the ``reported`` entries.
        """
        try:
            event_store = await get_event_store()

            # Save the incoming message event — enriched with SRM decision data
            # so that trace_service can extract decision fields directly from message.in
            message_event = EventData(
                id=uuid4(),
                tenant_id=request.tenant_id,
                session_id=request.session_id,
                event_type=request.event_type.value,
                event_data={
                    "content": request.payload.content,
                    "channel": request.payload.channel,
                    "user_id": request.payload.user_id,
                    "role": "user",
                    # SRM decision data (enrichment for trace_service)
                    "intent": result.intent,
                    "intent_category": result.intent_category,
                    "funnel_stage": result.funnel_stage,
                    "action": result.action,
                    "confidence": result.confidence,
                    "safety_concern": result.safety_concern,
                    "safety_category": result.safety_category,
                    "needs_rag": result.needs_rag,
                    "handoff_required": result.handoff_required,
                    "handoff_reason": result.handoff_reason,
                    "mode": result.mode,
                    "reasoning": result.reasoning,
                    "sentiment_label": result.sentiment_label,
                    "sentiment_score": result.sentiment_score,
                },
                event_metadata={
                    "processing_time_ms": result.processing_time_ms,
                    "mode": result.mode,
                    # Duplicated for trace_service filter queries (JSONB operators)
                    "safety_concern": result.safety_concern,
                    "handoff_required": result.handoff_required,
                    "intent": result.intent,
                    # LLM token usage — legacy accepted trio (unchanged
                    # contract: attribution of ONLY the accepted result).
                    "llm_model": llm_model,
                    "llm_usage": llm_usage or {},
                    # Full attempt history + deterministic fold. Group 4
                    # trace/builder consumers prefer these over the trio when
                    # present, and fall back to the trio for legacy events.
                    "llm_attempts": list(llm_attempts or []),
                    "llm_usage_total": dict(llm_usage_total or {}),
                    "funnel_stage": result.funnel_stage,
                    # Audio transcription metadata (from webhook payload)
                    **{
                        k: v
                        for k, v in (request.payload.metadata or {}).items()
                        if k in ("whisper_model", "is_audio", "audio_seconds")
                    },
                },
            )
            await event_store.append(message_event)

            # Decision Trace (CTXDecisionOutput / doc MVP v0.5): version, event_id, risk_flags, policy_refs, etc.
            risk_flags = {
                "safety_concern": result.safety_concern,
                "human_handoff_needed": result.handoff_required,
                "abuse_risk": False,
                "compliance_risk": False,
                "reputacao_risco": False,
                "urgencia_clinica": (
                    result.safety_category == "clinical_risk"
                    if result.safety_category
                    else False
                ),
                "lead_irritado": False,
                "pedido_promessa_resultado": False,
            }
            policy_refs = (result.state_patch or {}).get("policy_refs") or []
            constraints = (
                result.constraints
                if hasattr(result, "constraints") and result.constraints
                else (result.state_patch or {}).get("constraints") or []
            )

            decision_event = EventData(
                id=result.event_id,
                tenant_id=request.tenant_id,
                session_id=request.session_id,
                event_type="srm.decision",
                event_data={
                    "version": 1,
                    "event_id": str(result.event_id),
                    "timestamp": result.processed_at
                    or datetime.now(timezone.utc).isoformat(),
                    "clinic_id": str(request.tenant_id),
                    "user_id": (
                        getattr(request.payload, "user_id", None)
                        if hasattr(request, "payload")
                        else None
                    ),
                    "channel": (
                        getattr(request.payload, "channel", "unknown")
                        if hasattr(request, "payload")
                        else "unknown"
                    ),
                    "message_id": None,
                    "intent": result.intent,
                    "action": result.action,
                    "funnel_stage_before": getattr(result, "funnel_stage_before", None),
                    "funnel_stage_after": result.funnel_stage,
                    "risk_flags": risk_flags,
                    "policy_refs": policy_refs,
                    "constraints": constraints,
                    "rag": {"needs_rag": result.needs_rag},
                    "srm": {
                        "intent": result.intent,
                        "intent_category": result.intent_category,
                        "confidence": result.confidence,
                        "mode": result.mode,
                    },
                    "handoff": (
                        {
                            "handoff_required": result.handoff_required,
                            "handoff_reason": result.handoff_reason,
                            "handoff_summary": result.handoff_summary,
                        }
                        if result.handoff_required
                        else None
                    ),
                    "entities": {},
                    "justificativa_curta": result.reasoning,
                    "intent_category": result.intent_category,
                    "funnel_stage": result.funnel_stage,
                    "confidence": result.confidence,
                    "safety_concern": result.safety_concern,
                    "safety_category": result.safety_category,
                    "needs_rag": result.needs_rag,
                    "handoff_required": result.handoff_required,
                    "handoff_reason": result.handoff_reason,
                    "mode": result.mode,
                    "state_patch": result.state_patch,
                    "reasoning": result.reasoning,
                },
                event_metadata={
                    "processing_time_ms": result.processing_time_ms,
                    "processed_at": (
                        result.processed_at if result.processed_at else None
                    ),
                },
            )
            await event_store.append(decision_event)

            # Save the prompt generation event
            prompt_event = EventData(
                id=uuid4(),
                tenant_id=request.tenant_id,
                session_id=request.session_id,
                event_type="prompt.generated",
                event_data={
                    "system_prompt": result.system_prompt,
                    "instructions": result.instructions,
                    "constraints": result.constraints,
                    "context_summary": result.context_summary,
                    "context_sources": _filter_relevant_sources(
                        result.context_sources or [],
                        request.payload.content,
                        suggested_response,
                    ),
                    "mode": result.mode,
                    "intent": result.intent,
                    "funnel_stage": result.funnel_stage,
                    "suggested_response": suggested_response,
                },
                event_metadata={
                    "processing_time_ms": result.processing_time_ms,
                    "processed_at": (
                        result.processed_at if result.processed_at else None
                    ),
                    "stage_timings": result.stage_timings,
                },
            )
            await event_store.append(prompt_event)

            # convenio-particular-triage: emit when the modality slot is set.
            # Engine only writes atendimento_tipo into state_patch when the value
            # changes, so this fires on first set + explicit switches only.
            _patch_slots = (result.state_patch or {}).get("slots") or {}
            _new_atendimento_tipo = _patch_slots.get("atendimento_tipo")
            if _new_atendimento_tipo:
                tipo_event = EventData(
                    id=uuid4(),
                    tenant_id=request.tenant_id,
                    session_id=request.session_id,
                    event_type="ns.atendimento_tipo_set",
                    event_data={
                        "atendimento_tipo": _new_atendimento_tipo,
                        "trigger_message": request.payload.content,
                    },
                    event_metadata={
                        "channel": request.payload.channel,
                        "user_id": request.payload.user_id,
                    },
                )
                await event_store.append(tipo_event)

            # CRM integration events (doc MVP v0.5): ns.funnel_stage_changed, ns.handoff_created
            if result.state_patch and result.state_patch.get("funnel_stage"):
                funnel_crm_event = EventData(
                    id=uuid4(),
                    tenant_id=request.tenant_id,
                    session_id=request.session_id,
                    event_type="ns.funnel_stage_changed",
                    event_data={
                        "ns_stage": result.funnel_stage,
                        "crm_stage_id": None,
                    },
                    event_metadata={},
                )
                await event_store.append(funnel_crm_event)

            if result.handoff_required and result.handoff_summary:
                handoff_crm_event = EventData(
                    id=uuid4(),
                    tenant_id=request.tenant_id,
                    session_id=request.session_id,
                    event_type="ns.handoff_created",
                    event_data={
                        "handoff_id": result.handoff_summary.get("id"),
                        "setor": result.handoff_summary.get("sector"),
                        "prioridade": result.handoff_summary.get("sla"),
                        "resumo": result.handoff_summary.get("summary", ""),
                        "urgency": result.handoff_summary.get("urgency"),
                        "status": result.handoff_summary.get("status"),
                    },
                    event_metadata={},
                )
                await event_store.append(handoff_crm_event)
        except EventStoreError as e:
            # Event Store error - log but don't fail the request
            logger.warning(
                "Failed to save events to Event Store",
                tenant_id=str(request.tenant_id),
                session_id=str(request.session_id),
                error=str(e),
                error_type=type(e).__name__,
            )
        except Exception as e:
            # Unexpected error - log but don't fail the request
            logger.error(
                "Unexpected error saving events to Event Store",
                tenant_id=str(request.tenant_id),
                session_id=str(request.session_id),
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )

    async def get_event(
        self, event_id: UUID, session: AsyncSession
    ) -> EventDetailResponse:
        """
        Get event details by ID.

        Args:
            event_id: Event ID
            session: Database session

        Returns:
            Event detail response

        Raises:
            ValueError: If event not found
        """
        event_repo = EventRepository(session)

        event = await event_repo.get_by_id(event_id)

        if not event:
            raise ValueError(f"Event {event_id} not found")

        return EventDetailResponse(
            event_id=event.id,
            tenant_id=event.tenant_id,
            session_id=event.session_id,
            event_type=EventType(event.event_type),
            payload=event.event_data,
            decision=event.event_data.get("decision"),
            created_at=event.created_at.isoformat(),
        )

    async def list_events(
        self,
        tenant_id: UUID,
        session: AsyncSession,
        session_id: Optional[UUID] = None,
        event_type: Optional[EventType] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EventDetailResponse]:
        """
        List events with filtering and pagination.

        Args:
            tenant_id: Tenant ID
            session: Database session
            session_id: Optional session ID filter
            event_type: Optional event type filter
            limit: Maximum events to return
            offset: Offset for pagination

        Returns:
            List of event detail responses
        """
        event_repo = EventRepository(session)

        if session_id:
            events_list = await event_repo.find_by_session(
                tenant_id=tenant_id,
                session_id=session_id,
                skip=offset,
                limit=limit,
                event_type=event_type.value if event_type else None,
            )
        else:
            events_list = await event_repo.find_by_tenant(
                tenant_id=tenant_id,
                skip=offset,
                limit=limit,
                event_type=event_type.value if event_type else None,
            )

        return [
            EventDetailResponse(
                event_id=event.id,
                tenant_id=event.tenant_id,
                session_id=event.session_id,
                event_type=EventType(event.event_type),
                payload=event.event_data,
                decision=event.event_data.get("decision"),
                created_at=event.created_at.isoformat(),
            )
            for event in events_list
        ]


# Singleton instance
_event_service: Optional[EventService] = None


def get_event_service() -> EventService:
    """Get or create the global EventService instance."""
    global _event_service
    if _event_service is None:
        _event_service = EventService()
    return _event_service
