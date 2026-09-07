"""
NeuroStack API - RLHF Endpoints
Capture per-response ratings (1-5) with async AI analysis triggered for
ratings 1-3 that ship with a substantive written critique.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, JsonValue
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import (
    TenantContext,
    get_tenant_context,
    require_super_admin,
)
from src.llm import get_tenant_llm_client
from src.models.builder import Builder
from src.models.event import Event
from src.models.rlhf_rating import RlhfRating
from src.models.tenant import Tenant
from src.models.user import User
from src.observability.pii import mask_carteirinha
from src.repositories import get_db_session, get_db_session_dependency
from src.services.trace_service import _to_utc

logger = logging.getLogger(__name__)

router = APIRouter()


# ============================================
# Schemas
# ============================================


class RatingRequest(BaseModel):
    session_id: str
    message_id: str
    rating: int = Field(..., ge=1, le=5)
    reason: Optional[str] = None
    agent_response: str
    user_message: str
    clinic_tenant_id: Optional[str] = None


class RatingResponse(BaseModel):
    ok: bool = True
    rating_id: str


class RatingEntry(BaseModel):
    id: str
    ts: str
    tenant_id: str
    rating: int
    reason: Optional[str] = None
    agent_response: Optional[str] = None
    user_message: Optional[str] = None
    submitted_by: Optional[str] = None
    ai_analysis: Optional[dict[str, Any]] = None
    ai_analysis_status: Optional[str] = None
    approved_for_training: bool = False
    ignored: bool = False


class RatingActionRequest(BaseModel):
    action: Literal["approve", "ignore"]


# ============================================
# Rating Endpoints
# ============================================


def _build_analysis_prompt(
    rating: int, reason: str, agent_response: str, user_message: str
) -> str:
    # Use f-string so braces inside user-provided values are interpolated safely
    # and never treated as .format() placeholders (which would raise KeyError).
    #
    # The prompt is deliberately neutral: it asks whether the written critique
    # is justified given the assigned rating, without presupposing dissatisfaction
    # or agreement. The anchor to "metodologia SRM NeuroStack" is preserved so
    # the auditor still evaluates against the same rubric.
    return (
        f"Contexto: o agente respondeu '{agent_response}' à mensagem '{user_message}'. "
        f"O usuário atribuiu a nota {rating}/5 e escreveu a seguinte crítica: '{reason}'. "
        "Com base na metodologia SRM NeuroStack, a crítica escrita se justifica "
        "para a nota atribuída? "
        'Responda em JSON: {"status": "agreed" | "disagreed", "justification": "<máx 2 frases>"}'
    )


def _has_reason(reason: Optional[str]) -> bool:
    """Single definition of the reason-floor predicate (wish decision #3):
    reused by the analysis trigger, `only_with_reason`, and (Group 5/6)
    `accepted_count` and the accept-action 422 guard."""
    return len((reason or "").strip()) >= 8


def _parse_analysis_content(content: str) -> Optional[dict[str, str]]:
    """Best-effort JSON extraction from LLM response."""
    if not content:
        return None

    # Try direct JSON first
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        parsed = None

    # Fallback: extract first JSON object substring
    if parsed is None:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except (json.JSONDecodeError, TypeError):
                parsed = None

    if not isinstance(parsed, dict):
        return None

    status_value = str(parsed.get("status", "")).lower().strip()
    if status_value not in {"agreed", "disagreed"}:
        return None

    justification = str(parsed.get("justification", "")).strip()
    return {"status": status_value, "justification": justification}


def _channel_for_event_type(event_type: Optional[str]) -> str:
    """Map a resolved Event.event_type to the rating's channel string.

    Only ``message.in`` maps to WhatsApp (the source of trace-detail-view
    ratings). ``srm.decision`` and other simulator-produced event types map
    to ``web``. Anything else — including an event type unknown to this
    codebase — is stored as ``unknown`` so the audit report can flag rows
    whose provenance we cannot vouch for.
    """
    if event_type == "message.in":
        return "whatsapp"
    if event_type == "srm.decision":
        return "web"
    return "unknown"


async def _resolve_message_context(
    message_id: Optional[str],
    session: AsyncSession,
) -> tuple[str, Optional[UUID], Optional[UUID], Optional[Event]]:
    """Resolve a rating's ``message_id`` into a channel + trace pair + event.

    Returns a 4-tuple ``(channel, trace_event_id, trace_tenant_id, event)``.

    The four cases the caller must survive:

    * ``message_id`` absent or a non-UUID string (simulator flow — the panel
      generates a synthetic id like ``msg-abcd1234``): ``("web", None, None,
      None)``. The rating is a web-simulator artefact with no trace to
      point at.
    * ``message_id`` is a syntactically valid UUID but no ``Event`` row
      matches (orphaned): ``("unknown", None, None, None)``. We cannot vouch
      for what channel produced the rating.
    * ``message_id`` resolves to a ``message.in`` event: ``("whatsapp",
      event.id, event.tenant_id, event)``. This is the only shape that
      populates the trace pair — Group 6 will consume the pair to render a
      "View trace" link.
    * ``message_id`` resolves to any other event type (``srm.decision``,
      ``chat.response``, etc.): channel is derived from the event type via
      :func:`_channel_for_event_type` and the trace pair stays ``None``.

    Fail-open contract: if the DB call raises, the resolver returns
    ``("unknown", None, None, None)`` rather than propagating — a rating
    must never be dropped because its provenance lookup failed.
    """
    if not message_id:
        return ("web", None, None, None)

    try:
        msg_uuid = UUID(message_id)
    except (ValueError, AttributeError):
        # Non-UUID id — treat as a simulator-generated synthetic id.
        return ("web", None, None, None)

    try:
        original = (
            await session.execute(select(Event).where(Event.id == msg_uuid))
        ).scalar_one_or_none()
    except SQLAlchemyError as exc:
        logger.warning(
            "Rating message_id resolution failed message_id=%s err=%s",
            message_id,
            exc,
        )
        return ("unknown", None, None, None)

    if original is None:
        # Well-formed UUID, no matching Event — orphaned. Cannot vouch
        # for the channel that produced this rating.
        return ("unknown", None, None, None)

    channel = _channel_for_event_type(original.event_type)
    if original.event_type == "message.in":
        # Deliverable 3: the trace pair is filled ONLY for message.in and
        # never leaves the write with divergent nullness. Group 6 needs
        # both id and tenant to link back to the trace.
        return (channel, original.id, original.tenant_id, original)
    return (channel, None, None, original)


async def _analyze_rating_async(
    rating_id: UUID,
    rating: int,
    reason: str,
    agent_response: str,
    user_message: str,
    tenant_id: UUID,
    clinic_tenant_id: Optional[str] = None,
    resolved_event: Optional[Event] = None,
) -> None:
    """Run LLM analysis for a low-rating record and persist the result.

    Uses a dedicated prompt (NOT the SRM policy engine) since we're
    evaluating a user's critique, not an agent response. Opens its own
    DB session because the request-scoped one is closed by the time this
    background task runs.

    The credential-tenant resolution keeps the same three rungs the
    endpoint uses for ``subject_tenant_id``:

    1. Validated ``clinic_tenant_id`` (existence-checked against Tenant).
    2. ``resolved_event.tenant_id`` — the event ``message_id`` resolved to,
       resolved by ``submit_rating`` and forwarded here. This function no
       longer issues its own ``message_id`` query (deliverable 5); the
       middle rung is proven by an endpoint-level test that lets
       ``submit_rating`` perform the real resolution.
    3. ``tenant_id`` — the submitter's own tenant, the final fallback.
    """
    from src.services.event_service import _load_runtime_llm_credentials

    resolve_tenant_id = tenant_id
    clinic_tenant_resolved = False
    if clinic_tenant_id:
        try:
            candidate = UUID(clinic_tenant_id)
        except ValueError:
            candidate = None
        if candidate is not None:
            try:
                async with get_db_session() as db:
                    exists = (
                        await db.execute(
                            select(Tenant.id).where(Tenant.id == candidate)
                        )
                    ).scalar_one_or_none()
                if exists is not None:
                    resolve_tenant_id = candidate
                    clinic_tenant_resolved = True
                    logger.info(
                        "Rating analysis using explicit clinic_tenant_id rating_id=%s clinic_tenant=%s",
                        rating_id,
                        resolve_tenant_id,
                    )
                else:
                    logger.info(
                        "Rating analysis clinic_tenant_id not found in tenants, falling back rating_id=%s clinic_tenant=%s",
                        rating_id,
                        candidate,
                    )
            except Exception as exc:
                logger.warning(
                    "Rating analysis clinic_tenant_id lookup failed rating_id=%s clinic_tenant=%s err=%s",
                    rating_id,
                    candidate,
                    exc,
                )
    if not clinic_tenant_resolved and resolved_event is not None:
        # Middle rung — the endpoint already resolved message_id → Event
        # and threaded the resolved row through. We do NOT re-query here:
        # doing so would (a) waste a round-trip and (b) let a refactor
        # silently drop the middle rung without a visible symptom.
        resolve_tenant_id = resolved_event.tenant_id
        logger.info(
            "Rating analysis using clinic tenant from resolved event rating_id=%s clinic_tenant=%s",
            rating_id,
            resolve_tenant_id,
        )

    # DB-only resolver: returns ONLY the four LLM overlay fields (raw key held
    # in-memory for this call), builder-owner composition already applied.
    api_credentials: dict[str, JsonValue] = {}
    try:
        api_credentials = await _load_runtime_llm_credentials(resolve_tenant_id)
    except Exception:
        logger.warning(
            "Could not load tenant LLM credentials for rating analysis rating_id=%s",
            rating_id,
        )

    tenant_model_value = api_credentials.get("llm_model_name")
    tenant_model = tenant_model_value if isinstance(tenant_model_value, str) else None
    # Structured log MUST NOT contain any secret material or prefix — only the
    # public presence bit and the non-secret model id are safe to emit.
    logger.info(
        "Rating analysis LLM resolution rating_id=%s tenant_id=%s model=%s credentials_present=%s",
        rating_id,
        resolve_tenant_id,
        tenant_model,
        bool(api_credentials),
    )
    llm_client = get_tenant_llm_client(resolve_tenant_id, api_credentials)
    if llm_client is None:
        logger.warning(
            "LLM client unavailable; marking rating analysis as failed rating_id=%s",
            rating_id,
        )
        await _update_rating_status(rating_id, status_="failed")
        return

    prompt = _build_analysis_prompt(rating, reason, agent_response, user_message)

    try:
        result = await llm_client.generate(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Você é um auditor de qualidade de respostas de agentes "
                        "de saúde. Responda somente em JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            model=tenant_model,
        )
        parsed = _parse_analysis_content(result.get("content", ""))

        if not parsed:
            logger.warning(
                "Rating analysis returned unparseable content rating_id=%s",
                rating_id,
            )
            await _update_rating_status(rating_id, status_="failed")
            return

        await _update_rating_status(
            rating_id,
            status_="done",
            analysis=parsed,
        )
        logger.info(
            "Rating analysis complete rating_id=%s status=%s",
            rating_id,
            parsed["status"],
        )
    except Exception as exc:  # noqa: BLE001 — background task must swallow
        logger.error(
            "Rating analysis failed rating_id=%s error=%s",
            rating_id,
            str(exc),
            exc_info=True,
        )
        await _update_rating_status(rating_id, status_="failed")


async def _update_rating_status(
    rating_id: UUID,
    status_: str,
    analysis: Optional[dict[str, str]] = None,
) -> None:
    """Update the RlhfRating row's ``ai_analysis_status`` and, when the
    LLM returned a parseable verdict, ``ai_analysis``.

    Repointed from ``events`` to ``rlhf_ratings`` for Group 2. Prior to the
    port this function did a ``select(Event).where(...)`` and, on a miss,
    just logged that the row had "vanished" — which after the port would
    have silently dropped the AI's opinion on every rating, since the row
    was never an ``Event`` in the first place. Now the query lives on the
    real storage.
    """
    try:
        async with get_db_session() as session:
            result = await session.execute(
                select(RlhfRating).where(RlhfRating.id == rating_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                logger.warning(
                    "Rating row vanished before analysis update rating_id=%s",
                    rating_id,
                )
                return

            row.ai_analysis_status = status_
            if analysis is not None:
                row.ai_analysis = analysis
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to persist rating analysis status rating_id=%s status=%s",
            rating_id,
            status_,
        )


@router.post("/rating", response_model=RatingResponse)
async def submit_rating(
    body: RatingRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> RatingResponse:
    """Persist a rating (1-5) for an agent response into ``rlhf_ratings``.

    Resolves channel, trace pair, and subject-tenant attribution at
    submission time so a later reader never needs to re-derive them —
    write and read are atomic on the new storage.

    For ratings 1-3 that ship with a substantive written critique
    (reason of at least 8 characters after trimming), a background LLM
    analysis is triggered to evaluate whether the critique is justified
    given the assigned rating. Status is tracked via
    ``ai_analysis_status`` (pending/done/failed).
    """
    rating_id = uuid4()
    reason = (body.reason or "").strip() or None
    # Analysis threshold: rating 1-3 AND a written critique with substance.
    has_reason = _has_reason(reason)
    triggers_analysis = body.rating <= 3 and has_reason

    # -- Resolve channel + trace pair from message_id ---------------------
    # Fail-open per deliverable 7: the resolver never raises; if the DB
    # call fails, we still persist the rating with channel='unknown' and
    # a null trace pair.
    try:
        (
            channel,
            trace_event_id,
            trace_tenant_id,
            resolved_event,
        ) = await _resolve_message_context(body.message_id, session)
    except (RuntimeError, SQLAlchemyError) as exc:
        logger.warning(
            "Rating message_id context resolution raised message_id=%s err=%s",
            body.message_id,
            exc,
        )
        channel, trace_event_id, trace_tenant_id, resolved_event = (
            "unknown",
            None,
            None,
            None,
        )

    # -- subject_tenant_id: three rungs in precedence order ---------------
    # 1. Validated ``clinic_tenant_id`` — must parse as a UUID AND match a
    #    real Tenant row (clinic_admin callers may send a business slug,
    #    and a bare syntactic UUID would silently mislead the audit report).
    # 2. ``resolved_event.tenant_id`` — the tenant that owns the trace this
    #    rating is *about*, when message_id resolved to a real Event.
    # 3. The rater's own tenant — the record-owner floor, so the field is
    #    never null and never a dangling reference.
    subject_tenant_id: UUID = tenant.tenant_id
    if body.clinic_tenant_id:
        try:
            candidate = UUID(body.clinic_tenant_id)
        except (ValueError, AttributeError):
            candidate = None
        if candidate is not None:
            exists = await session.execute(
                select(Tenant.id).where(Tenant.id == candidate)
            )
            if exists.scalar_one_or_none() is not None:
                subject_tenant_id = candidate
            elif resolved_event is not None:
                subject_tenant_id = resolved_event.tenant_id
        elif resolved_event is not None:
            subject_tenant_id = resolved_event.tenant_id
    elif resolved_event is not None:
        subject_tenant_id = resolved_event.tenant_id

    session_uuid: Optional[UUID] = None
    if body.session_id:
        try:
            session_uuid = UUID(body.session_id)
        except (ValueError, AttributeError):
            session_uuid = None

    row = RlhfRating(
        id=rating_id,
        tenant_id=tenant.tenant_id,
        subject_tenant_id=subject_tenant_id,
        session_id=session_uuid,
        message_id=body.message_id,
        trace_event_id=trace_event_id,
        trace_tenant_id=trace_tenant_id,
        channel=channel,
        rating=body.rating,
        reason=reason,
        agent_response=body.agent_response,
        user_message=body.user_message,
        submitted_by=tenant.user_id if tenant.user_id else None,
        role=tenant.role,
        ai_analysis_status="pending" if triggers_analysis else None,
        approved_for_training=False,
        ignored=False,
    )

    session.add(row)
    await session.flush()
    await session.commit()

    if triggers_analysis:
        assert reason is not None
        asyncio.create_task(
            _analyze_rating_async(
                rating_id=rating_id,
                rating=body.rating,
                reason=reason,
                agent_response=body.agent_response,
                user_message=body.user_message,
                tenant_id=tenant.tenant_id,
                clinic_tenant_id=body.clinic_tenant_id,
                resolved_event=resolved_event,
            )
        )

    logger.info(
        "RLHF rating saved: rating_id=%s channel=%s rating=%s analysis=%s",
        rating_id,
        channel,
        body.rating,
        triggers_analysis,
    )
    return RatingResponse(rating_id=str(rating_id))


def _serialize_rating_event(
    row: RlhfRating,
    tenant_map: Optional[dict[UUID, tuple[str, Optional[UUID], Optional[str]]]] = None,
    accepted_email_map: Optional[dict[UUID, str]] = None,
) -> dict[str, Any]:
    """Serialize a ``RlhfRating`` row for the audit-report entries list.

    Emits ``channel``, ``trace_event_id``, and ``trace_tenant_id`` — the
    last two feed Group 6's "View trace" link. Note ``RatingEntry``
    (defined above at module top) is stale dead code; the endpoint does
    not declare a ``response_model`` and entries are returned as dicts
    from this serializer.

    Mask carteirinha PII on the training-lake export surface: rating rows
    flagged as ``approved_for_training`` feed the SLM fine-tuning dataset,
    so the raw number must never leave storage here.
    """
    subject_uuid = row.subject_tenant_id

    subject_clinic_name: Optional[str] = None
    builder_name: Optional[str] = None
    if tenant_map is not None:
        entry = tenant_map.get(subject_uuid)
        if entry is not None:
            subject_clinic_name = entry[0]
            builder_name = entry[2]

    accepted_by_email: Optional[str] = None
    if row.accepted_by is not None and accepted_email_map is not None:
        accepted_by_email = accepted_email_map.get(row.accepted_by)

    # SQLite's ``DateTime(timezone=True)`` binding strips ``tzinfo`` on
    # read (aiosqlite returns naive datetimes even for tz-aware columns).
    # Reattach UTC so the ISO string round-trips exactly against inputs
    # emitted by ``datetime.now(timezone.utc).isoformat()`` — the shape
    # every caller in this codebase sends and the ``accepted_at`` audit
    # trail asserts on.
    def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()

    return {
        "id": str(row.id),
        "ts": _iso_utc(row.created_at) or "",
        "tenant_id": str(row.tenant_id),
        "rating": int(row.rating),
        "reason": mask_carteirinha(row.reason),
        "agent_response": mask_carteirinha(row.agent_response),
        "user_message": mask_carteirinha(row.user_message),
        "submitted_by": str(row.submitted_by) if row.submitted_by else None,
        "ai_analysis": row.ai_analysis,
        "ai_analysis_status": row.ai_analysis_status,
        "approved_for_training": bool(row.approved_for_training),
        "ignored": bool(row.ignored),
        "subject_tenant_id": str(subject_uuid),
        "subject_clinic_name": subject_clinic_name,
        "builder_name": builder_name,
        "accepted_by": str(row.accepted_by) if row.accepted_by is not None else None,
        "accepted_at": _iso_utc(row.accepted_at),
        "accepted_by_email": accepted_by_email,
        # Group 2 additions — Group 6 will consume the trace pair.
        "channel": row.channel,
        "trace_event_id": (
            str(row.trace_event_id) if row.trace_event_id is not None else None
        ),
        "trace_tenant_id": (
            str(row.trace_tenant_id) if row.trace_tenant_id is not None else None
        ),
    }


@router.get("/ratings")
async def list_ratings(
    builder_id: Optional[UUID] = Query(None),
    tenant_id: Optional[UUID] = Query(None),
    start_date: Optional[datetime] = Query(
        default=None, description="Start of period (ISO 8601)"
    ),
    end_date: Optional[datetime] = Query(
        default=None, description="End of period (ISO 8601)"
    ),
    only_accepted: bool = Query(False),
    only_with_reason: bool = Query(False),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, Any]:
    """Return the rating audit report as `{ summary, facets, entries }`.

    The response shape (and the handler's fixed execution order) is what makes
    the report honest: KPIs are computed server-side over the filtered cut,
    facets are derived from the data actually seen (not from the /admin/*
    cadastres, which omit the tenants where simulator ratings land), and
    filtering matches the *subject* of the rating (`subject_tenant_id`) rather
    than the record owner (`Event.tenant_id`).

    Execution order (contract — see wish Group 4):

    1. SQL filters: `event_type`, tenant isolation for non-super-admin, and
       the date window. Only real columns — never `event_data` — so the query
       runs on SQLite (test suite) and Postgres (prod) identically.
    2. Python loads the full ordered result set and resolves a batched map
       `tenant_id -> (name, builder_id, builder_name)` for the distinct
       subject tenants seen (2 queries total, never N+1).
    3. Facets are computed BEFORE any builder/clinic narrowing, so selecting
       a builder in the panel does not shrink its own dropdown.
    4. Builder/clinic narrowing matches `subject_tenant_id` with a fallback to
       `Event.tenant_id` for rows that predate Group 3.
    5. Summary is computed over the result of step 4.
    6. `only_accepted` and `only_with_reason` filter entries only — they do
       not change the summary or facets.
    7. `limit`/`offset` slice AFTER step 6 so pagination never returns
       incomplete pages silently.
    """
    is_super = tenant.role == "super_admin"

    # -- Step 1: SQL filters on real columns only --------------------------
    # Group 2: the row source moved from ``events`` (filtered by
    # ``event_type='rlhf.rating'``) to the dedicated ``rlhf_ratings`` table.
    # No new panel filters are wired here — ``channel`` and ``reviewed``
    # narrowing belong to Group 4.
    stmt = select(RlhfRating)

    if not is_super:
        # Tenant isolation: non-super-admins can only see rows THEY own.
        # For super_admin, tenant/builder filtering happens in step 4 against
        # subject_tenant_id — a SQL clause on RlhfRating.tenant_id here would
        # AND owner-match with subject-match and return zero rows in the main
        # case (a builder rating a clinic's agent).
        stmt = stmt.where(RlhfRating.tenant_id == tenant.tenant_id)

    # Date window: same convention as trace_service._apply_filters — end_date
    # at midnight widens to 23:59:59 so the final day isn't dropped, and both
    # ends pass through _to_utc (naive dates read as BRT / UTC-3).
    if start_date is not None:
        stmt = stmt.where(RlhfRating.created_at >= _to_utc(start_date))
    if end_date is not None:
        if end_date.hour == 0 and end_date.minute == 0 and end_date.second == 0:
            end_date = end_date.replace(hour=23, minute=59, second=59)
        stmt = stmt.where(RlhfRating.created_at <= _to_utc(end_date))

    stmt = stmt.order_by(RlhfRating.created_at.desc())
    result = await session.execute(stmt)
    ratings_rows: list[RlhfRating] = list(result.scalars().all())

    # -- Step 2: batched resolution of subject tenants ---------------------
    # ``subject_tenant_id`` is a NOT NULL column on ``rlhf_ratings`` — no
    # more JSON blob inspection or Event.tenant_id fallback needed.
    subject_ids: set[UUID] = {r.subject_tenant_id for r in ratings_rows}
    tenant_map: dict[UUID, tuple[str, Optional[UUID], Optional[str]]] = {}
    builder_name_map: dict[UUID, str] = {}
    if subject_ids:
        tenants_result = await session.execute(
            select(Tenant.id, Tenant.name, Tenant.builder_id).where(
                Tenant.id.in_(subject_ids)
            )
        )
        tenant_rows = tenants_result.all()
        builder_ids: set[UUID] = {
            tr.builder_id for tr in tenant_rows if tr.builder_id is not None
        }
        if builder_ids:
            builders_result = await session.execute(
                select(Builder.id, Builder.name).where(Builder.id.in_(builder_ids))
            )
            builder_name_map = {b.id: b.name for b in builders_result.all()}
        for tr in tenant_rows:
            b_name = (
                builder_name_map.get(tr.builder_id)
                if tr.builder_id is not None
                else None
            )
            tenant_map[tr.id] = (tr.name, tr.builder_id, b_name)

    # -- Step 3: facets computed BEFORE builder/clinic narrowing -----------
    facet_clinics: dict[UUID, dict[str, Any]] = {}
    facet_builders: dict[UUID, dict[str, Any]] = {}
    for sid in subject_ids:
        entry = tenant_map.get(sid)
        if entry is not None:
            clinic_name, clinic_builder_id, clinic_builder_name = entry
        else:
            clinic_name = str(sid)
            clinic_builder_id = None
            clinic_builder_name = None
        facet_clinics[sid] = {
            "id": str(sid),
            "name": clinic_name,
            "builder_id": str(clinic_builder_id) if clinic_builder_id else None,
        }
        if clinic_builder_id is not None and clinic_builder_id not in facet_builders:
            facet_builders[clinic_builder_id] = {
                "id": str(clinic_builder_id),
                "name": clinic_builder_name or str(clinic_builder_id),
            }

    facets = {
        "builders": sorted(
            facet_builders.values(), key=lambda b: (b["name"] or "").lower()
        ),
        "clinics": sorted(
            facet_clinics.values(), key=lambda c: (c["name"] or "").lower()
        ),
    }

    # -- Step 4: apply builder/clinic narrowing against subject_tenant_id --
    def _matches_narrowing(r: RlhfRating) -> bool:
        sid = r.subject_tenant_id
        if tenant_id is not None and sid != tenant_id:
            return False
        if builder_id is not None:
            entry = tenant_map.get(sid)
            row_builder_id = entry[1] if entry is not None else None
            if row_builder_id != builder_id:
                return False
        return True

    if not is_super:
        cut: list[RlhfRating] = list(ratings_rows)
    else:
        cut = [r for r in ratings_rows if _matches_narrowing(r)]

    # -- Step 5: summary over the cut (before only_* toggles) --------------
    def _row_has_reason(r: RlhfRating) -> bool:
        return _has_reason(r.reason)

    def _is_accepted(r: RlhfRating) -> bool:
        return bool(r.approved_for_training)

    distribution: dict[str, int] = {str(i): 0 for i in range(1, 6)}
    total = 0
    rating_sum = 0
    accepted_count = 0
    for r in cut:
        rating_value = int(r.rating)
        if 1 <= rating_value <= 5:
            distribution[str(rating_value)] += 1
            total += 1
            rating_sum += rating_value
        if _is_accepted(r) and _row_has_reason(r):
            accepted_count += 1

    average = round(rating_sum / total, 1) if total > 0 else 0.0
    summary = {
        "total": total,
        "average": average,
        "accepted_count": accepted_count,
        "distribution": distribution,
    }

    # -- Step 6: only_accepted / only_with_reason (entries only) -----------
    filtered = cut
    if only_accepted:
        filtered = [r for r in filtered if _is_accepted(r)]
    if only_with_reason:
        filtered = [r for r in filtered if _row_has_reason(r)]

    # -- Step 7: paginate AFTER filtering ----------------------------------
    page = filtered[offset : offset + limit]

    # Batched email resolution for ``accepted_by`` on the paginated page.
    accepted_by_ids: set[UUID] = {
        r.accepted_by for r in page if r.accepted_by is not None
    }
    accepted_email_map: dict[UUID, str] = {}
    if accepted_by_ids:
        users_result = await session.execute(
            select(User.id, User.email).where(User.id.in_(accepted_by_ids))
        )
        accepted_email_map = {ur.id: ur.email for ur in users_result.all()}

    entries = [
        _serialize_rating_event(
            r, tenant_map=tenant_map, accepted_email_map=accepted_email_map
        )
        for r in page
    ]

    return {"summary": summary, "facets": facets, "entries": entries}


@router.patch("/ratings/{rating_id}")
async def update_rating_action(
    rating_id: UUID,
    body: RatingActionRequest,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, Any]:
    """Approve or ignore a rating entry for dataset curation.

    Super admin only — approvals are training-dataset gatekeepers.

    Ported to ``rlhf_ratings`` in Group 2; preserves the 422 guard,
    approve/ignore exclusivity, and the ``accepted_by`` /
    ``accepted_at`` timestamps.
    """
    result = await session.execute(select(RlhfRating).where(RlhfRating.id == rating_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Rating not found",
        )

    if body.action == "approve":
        # Group 5 guard: accepting an opinion is an act OVER written feedback.
        # Ratings without a qualifying reason (empty or under 8 chars) cannot
        # be approved — the training-lake export only ever contains rows the
        # operator explicitly endorsed the critique of, so silently accepting
        # a reasonless entry would poison the dataset with meaningless labels.
        if not _has_reason(row.reason):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Cannot approve rating without a qualifying reason",
            )
        row.approved_for_training = True
        # Audit trail: stamp who accepted and when so the panel can render
        # authorship. Stored as tz-aware ``DateTime`` on the dedicated
        # column now; serialization happens in ``_serialize_rating_event``.
        row.accepted_by = tenant.user_id if tenant.user_id else None
        row.accepted_at = datetime.now(timezone.utc)
        # Approve/ignore exclusivity: clearing ``ignored`` mirrors the
        # ``data.pop("ignored", None)`` semantic of the pre-port version.
        row.ignored = False
    else:
        # ``ignore`` action: no stamping, no reason guard — ignoring a
        # low-value rating is orthogonal to the accept flow's
        # dataset-curation semantics.
        row.ignored = True
        row.approved_for_training = False

    await session.commit()

    logger.info(
        "RLHF rating action: rating_id=%s action=%s by=%s",
        rating_id,
        body.action,
        tenant.user_id,
    )
    return {"ok": True, "id": str(rating_id), "action": body.action}
