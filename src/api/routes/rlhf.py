"""
NeuroStack API - RLHF Endpoints
Capture per-response ratings (1-5) with async AI analysis triggered for
ratings 1-3 that ship with a substantive written critique.
"""

import asyncio
import json
import logging
import re
from collections.abc import Coroutine
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import (
    TenantContext,
    get_tenant_context,
    require_super_admin,
)
from src.llm import get_tenant_llm_client
from src.models.builder import Builder
from src.models.event import Event
from src.models.tenant import Tenant
from src.models.user import User
from src.observability.pii import mask_carteirinha
from src.repositories import get_db_session, get_db_session_dependency
from src.services.trace_service import _to_utc

logger = logging.getLogger(__name__)

router = APIRouter()

_rating_analysis_tasks: set[asyncio.Task[None]] = set()
_rating_analysis_errors: list[BaseException] = []


def _task_finished(task: asyncio.Task[None]) -> None:
    """Remove a completed task and retain its terminal error for the drain."""
    if task not in _rating_analysis_tasks:
        return
    _rating_analysis_tasks.remove(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        _rating_analysis_errors.append(error)


def spawn(coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task[None]:
    """Create one owned rating-analysis task on the running application loop."""
    task = asyncio.create_task(coro, name=name)
    _rating_analysis_tasks.add(task)
    task.add_done_callback(_task_finished)
    return task


def _raise_rating_analysis_errors() -> None:
    if not _rating_analysis_errors:
        return
    errors = tuple(_rating_analysis_errors)
    _rating_analysis_errors.clear()
    if len(errors) == 1:
        raise errors[0]
    raise BaseExceptionGroup("RLHF rating analysis tasks failed", list(errors))


async def wait_idle() -> None:
    """Wait until every owned analysis completes, then propagate retained errors."""
    while _rating_analysis_tasks:
        done, _ = await asyncio.wait(tuple(_rating_analysis_tasks))
        for task in done:
            _task_finished(task)
    _raise_rating_analysis_errors()


async def shutdown() -> None:
    """Cancel and await every owned analysis before application resources close."""
    pending = tuple(_rating_analysis_tasks)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
        for task in pending:
            _task_finished(task)
    await wait_idle()


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
    event_id: str


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


async def _analyze_rating_async(
    event_id: UUID,
    rating: int,
    reason: str,
    agent_response: str,
    user_message: str,
    tenant_id: UUID,
    message_id: Optional[str] = None,
    clinic_tenant_id: Optional[str] = None,
) -> None:
    """Run LLM analysis for a low-rating event and persist the result.

    Uses a dedicated prompt (NOT the SRM policy engine) since we're
    evaluating a user's critique, not an agent response. Opens its own
    DB session because the request-scoped one is closed by the time this
    background task runs.

    When message_id is a valid UUID that matches an Event row (e.g. a trace
    event from the Histórico de Traces), we use that event's tenant_id to
    resolve LLM credentials — because the runtime cache only stores the
    non-secret projection, so raw key resolution runs DB-only against the
    CLINIC's tenant, not the builder's. Falls back to the rating's own
    tenant_id (simulator flow, where message_id is a frontend UUID).
    """
    from src.services.event_service import _load_runtime_llm_credentials

    # Resolve which tenant's credentials to use for LLM.
    # Priority: explicit clinic_tenant_id (from frontend) > message_id event lookup > submitter's tenant.
    #
    # clinic_tenant_id must (a) parse as a UUID AND (b) match a real Tenant row
    # before we trust it — otherwise a caller could point analysis at any
    # syntactically valid UUID and pick which tenant's LLM credentials are
    # loaded for this call. Mirrors the validation in submit_rating. When the
    # UUID is well-formed but no Tenant matches, fall through to the message_id
    # lookup rather than silently staying on the submitter tenant, preserving
    # the declared priority order.
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
                        "Rating analysis using explicit clinic_tenant_id event_id=%s clinic_tenant=%s",
                        event_id,
                        resolve_tenant_id,
                    )
                else:
                    logger.info(
                        "Rating analysis clinic_tenant_id not found in tenants, falling back event_id=%s clinic_tenant=%s",
                        event_id,
                        candidate,
                    )
            except Exception as exc:
                logger.warning(
                    "Rating analysis clinic_tenant_id lookup failed event_id=%s clinic_tenant=%s err=%s",
                    event_id,
                    candidate,
                    exc,
                )
    if not clinic_tenant_resolved and message_id:
        try:
            msg_uuid = UUID(message_id)
            async with get_db_session() as db:
                original = (
                    await db.execute(select(Event).where(Event.id == msg_uuid))
                ).scalar_one_or_none()
                if original is not None:
                    resolve_tenant_id = original.tenant_id
                    logger.info(
                        "Rating analysis using clinic tenant event_id=%s clinic_tenant=%s",
                        event_id,
                        resolve_tenant_id,
                    )
                else:
                    logger.info(
                        "Rating analysis message_id not found in events, using submitter tenant event_id=%s message_id=%s",
                        event_id,
                        message_id,
                    )
        except Exception as exc:
            logger.warning(
                "Rating analysis message_id lookup failed event_id=%s message_id=%s err=%s",
                event_id,
                message_id,
                exc,
            )

    # DB-only resolver: returns ONLY the four LLM overlay fields (raw key held
    # in-memory for this call), builder-owner composition already applied.
    api_credentials: dict = {}
    try:
        api_credentials = await _load_runtime_llm_credentials(resolve_tenant_id)
    except Exception:
        logger.warning(
            "Could not load tenant LLM credentials for rating analysis event_id=%s",
            event_id,
        )

    tenant_model: Optional[str] = api_credentials.get("llm_model_name") or None
    # Structured log MUST NOT contain any secret material or prefix — only the
    # public presence bit and the non-secret model id are safe to emit.
    logger.info(
        "Rating analysis LLM resolution event_id=%s tenant_id=%s model=%s credentials_present=%s",
        event_id,
        resolve_tenant_id,
        tenant_model,
        bool(api_credentials),
    )
    llm_client = get_tenant_llm_client(resolve_tenant_id, api_credentials)
    if llm_client is None:
        logger.warning(
            "LLM client unavailable; marking rating analysis as failed event_id=%s",
            event_id,
        )
        await _update_rating_status(event_id, status_="failed")
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
                "Rating analysis returned unparseable content event_id=%s",
                event_id,
            )
            await _update_rating_status(event_id, status_="failed")
            return

        await _update_rating_status(
            event_id,
            status_="done",
            analysis=parsed,
        )
        logger.info(
            "Rating analysis complete event_id=%s status=%s",
            event_id,
            parsed["status"],
        )
    except (Exception,) as exc:  # noqa: BLE001 — supervisor must receive the error
        logger.error(
            "Rating analysis failed event_id=%s error=%s",
            event_id,
            str(exc),
            exc_info=True,
        )
        await _update_rating_status(event_id, status_="failed")
        raise


async def _update_rating_status(
    event_id: UUID,
    status_: str,
    analysis: Optional[dict[str, str]] = None,
) -> None:
    """Update the rating event's ai_analysis_status (and optionally ai_analysis)."""
    try:
        async with get_db_session() as session:
            result = await session.execute(select(Event).where(Event.id == event_id))
            event = result.scalar_one_or_none()
            if event is None:
                logger.warning(
                    "Rating event vanished before analysis update event_id=%s",
                    event_id,
                )
                return

            data = dict(event.event_data or {})
            data["ai_analysis_status"] = status_
            if analysis is not None:
                data["ai_analysis"] = analysis
            event.event_data = data
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to persist rating analysis status event_id=%s status=%s",
            event_id,
            status_,
        )
        raise


@router.post("/rating", response_model=RatingResponse)
async def submit_rating(
    body: RatingRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    """Persist a rating (1-5) for an agent response.

    For ratings 1-3 that ship with a substantive written critique
    (reason of at least 8 characters after trimming), a background LLM
    analysis is triggered to evaluate whether the critique is justified
    given the assigned rating. Status is tracked via `ai_analysis_status`
    (pending/done/failed).
    """
    event_id = uuid4()
    reason = (body.reason or "").strip() or None
    # Analysis threshold: rating 1-3 AND a written critique with substance.
    has_reason = _has_reason(reason)
    triggers_analysis = body.rating <= 3 and has_reason

    # subject_tenant_id records WHICH clinic the rating is *about*, so that
    # per-clinic filtering on the audit report (Group 4) actually resolves to
    # a real tenant. It is separate from Event.tenant_id (the rater's own
    # tenant, i.e. the record owner). We only trust body.clinic_tenant_id when
    # it (a) parses as a UUID AND (b) matches a real Tenant row — clinic_admin
    # callers can send a business slug from ClinicConfigTable, and a syntactic
    # UUID that doesn't exist in Tenant would also mislead the report. Any
    # failure falls back to the rater's own tenant so the field is never null
    # nor a dangling reference.
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

    event_data: dict[str, Any] = {
        "message_id": body.message_id,
        "rating": body.rating,
        "reason": reason,
        "agent_response": body.agent_response,
        "user_message": body.user_message,
        "submitted_by": str(tenant.user_id) if tenant.user_id else None,
        "role": tenant.role,
        "subject_tenant_id": str(subject_tenant_id),
    }
    if triggers_analysis:
        event_data["ai_analysis_status"] = "pending"

    session_uuid: Optional[UUID] = None
    if body.session_id:
        try:
            session_uuid = UUID(body.session_id)
        except (ValueError, AttributeError):
            session_uuid = None

    event = Event(
        id=event_id,
        tenant_id=tenant.tenant_id,
        session_id=session_uuid,
        event_type="rlhf.rating",
        event_data=event_data,
        event_metadata={"source": "chat_simulator"},
    )

    session.add(event)
    await session.flush()
    await session.commit()

    if triggers_analysis:
        spawn(
            _analyze_rating_async(
                event_id=event_id,
                rating=body.rating,
                reason=reason,
                agent_response=body.agent_response,
                user_message=body.user_message,
                tenant_id=tenant.tenant_id,
                message_id=body.message_id,
                clinic_tenant_id=body.clinic_tenant_id,
            ),
            name=f"rlhf-rating-analysis:{event_id}",
        )

    logger.info(
        "RLHF rating saved: event_id=%s rating=%s analysis=%s",
        event_id,
        body.rating,
        triggers_analysis,
    )
    return RatingResponse(event_id=str(event_id))


def _serialize_rating_event(
    event: Event,
    tenant_map: Optional[dict[UUID, tuple[str, Optional[UUID], Optional[str]]]] = None,
    accepted_email_map: Optional[dict[UUID, str]] = None,
) -> dict[str, Any]:
    data = event.event_data or {}
    # Mask carteirinha PII on the training-lake export surface: rating
    # rows flagged as approved_for_training feed the SLM fine-tuning
    # dataset, so the raw number must never leave the event store here.
    #
    # subject_tenant_id records the clinic the rating is *about* (see Group 3);
    # older rows lack it, so fall back to Event.tenant_id (the record owner)
    # for display and filtering — this is what keeps pre-Group-3 ratings from
    # silently vanishing from the report.
    subject_raw = data.get("subject_tenant_id")
    subject_uuid: Optional[UUID] = None
    if subject_raw is not None:
        try:
            subject_uuid = UUID(str(subject_raw))
        except (ValueError, AttributeError):
            subject_uuid = None
    if subject_uuid is None:
        subject_uuid = event.tenant_id

    subject_clinic_name: Optional[str] = None
    builder_name: Optional[str] = None
    if tenant_map is not None:
        entry = tenant_map.get(subject_uuid)
        if entry is not None:
            subject_clinic_name = entry[0]
            builder_name = entry[2]

    # Group 5: accept-audit trail. `accepted_by` / `accepted_at` are stamped
    # by the PATCH approve action (see `update_rating_action`); legacy rows
    # approved before this wish carry no stamp and surface as `None`, which
    # the panel renders as "—" (no migration, per acceptance criterion).
    accepted_by_raw = data.get("accepted_by")
    accepted_by_uuid: Optional[UUID] = None
    if accepted_by_raw is not None:
        try:
            accepted_by_uuid = UUID(str(accepted_by_raw))
        except (ValueError, AttributeError):
            accepted_by_uuid = None
    accepted_by_email: Optional[str] = None
    if accepted_by_uuid is not None and accepted_email_map is not None:
        accepted_by_email = accepted_email_map.get(accepted_by_uuid)

    return {
        "id": str(event.id),
        "ts": event.created_at.isoformat() if event.created_at else "",
        "tenant_id": str(event.tenant_id),
        "rating": int(data.get("rating", 0)),
        "reason": mask_carteirinha(data.get("reason")),
        "agent_response": mask_carteirinha(data.get("agent_response")),
        "user_message": mask_carteirinha(data.get("user_message")),
        "submitted_by": data.get("submitted_by"),
        "ai_analysis": data.get("ai_analysis"),
        "ai_analysis_status": data.get("ai_analysis_status"),
        "approved_for_training": bool(data.get("approved_for_training", False)),
        "ignored": bool(data.get("ignored", False)),
        "subject_tenant_id": str(subject_uuid),
        "subject_clinic_name": subject_clinic_name,
        "builder_name": builder_name,
        "accepted_by": str(accepted_by_uuid) if accepted_by_uuid is not None else None,
        "accepted_at": data.get("accepted_at"),
        "accepted_by_email": accepted_by_email,
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
    stmt = select(Event).where(Event.event_type == "rlhf.rating")

    if not is_super:
        # Tenant isolation: non-super-admins can only see rows THEY own.
        # For super_admin, tenant/builder filtering happens in step 4 against
        # subject_tenant_id — a SQL clause on Event.tenant_id here would AND
        # owner-match with subject-match and return zero rows in the main
        # case (a builder rating a clinic's agent).
        stmt = stmt.where(Event.tenant_id == tenant.tenant_id)

    # Date window: reuse the exact convention from
    # `src/services/trace_service.py:_apply_filters` — end_date at midnight
    # widens to 23:59:59 so the final day isn't dropped (the panel's default
    # end_date is "today"), and both ends pass through `_to_utc` which treats
    # naive datetimes as BRT (UTC-3). Event.created_at is DateTime(timezone=
    # True), so comparing naive against tz-aware passes on SQLite (test) but
    # shifts 3 hours on Postgres (prod).
    if start_date is not None:
        stmt = stmt.where(Event.created_at >= _to_utc(start_date))
    if end_date is not None:
        if end_date.hour == 0 and end_date.minute == 0 and end_date.second == 0:
            end_date = end_date.replace(hour=23, minute=59, second=59)
        stmt = stmt.where(Event.created_at <= _to_utc(end_date))

    stmt = stmt.order_by(Event.created_at.desc())
    result = await session.execute(stmt)
    events = list(result.scalars().all())

    # -- Step 2: batched resolution of subject tenants ---------------------
    # subject_tenant_id lives in event_data (see Group 3). Read it in Python
    # to keep the query dialect-agnostic (JSON operators are Postgres-only).
    # Fall back to Event.tenant_id for pre-Group-3 rows so they still show up
    # named in the report instead of vanishing.
    def _subject_of(event: Event) -> UUID:
        data = event.event_data or {}
        raw = data.get("subject_tenant_id")
        if raw:
            try:
                return UUID(str(raw))
            except (ValueError, AttributeError):
                pass
        return event.tenant_id

    subject_ids: set[UUID] = {_subject_of(e) for e in events}
    tenant_map: dict[UUID, tuple[str, Optional[UUID], Optional[str]]] = {}
    builder_name_map: dict[UUID, str] = {}
    if subject_ids:
        tenants_result = await session.execute(
            select(Tenant.id, Tenant.name, Tenant.builder_id).where(
                Tenant.id.in_(subject_ids)
            )
        )
        rows = tenants_result.all()
        builder_ids: set[UUID] = {
            row.builder_id for row in rows if row.builder_id is not None
        }
        if builder_ids:
            builders_result = await session.execute(
                select(Builder.id, Builder.name).where(Builder.id.in_(builder_ids))
            )
            builder_name_map = {b.id: b.name for b in builders_result.all()}
        for row in rows:
            b_name = (
                builder_name_map.get(row.builder_id)
                if row.builder_id is not None
                else None
            )
            tenant_map[row.id] = (row.name, row.builder_id, b_name)

    # -- Step 3: facets computed BEFORE builder/clinic narrowing -----------
    # Distinct builders and clinics seen among the subjects of this cut.
    # Clinics carry `builder_id` so the panel can narrow the clinic dropdown
    # client-side without a second HTTP round-trip.
    facet_clinics: dict[UUID, dict[str, Any]] = {}
    facet_builders: dict[UUID, dict[str, Any]] = {}
    for sid in subject_ids:
        entry = tenant_map.get(sid)
        if entry is not None:
            clinic_name, clinic_builder_id, clinic_builder_name = entry
        else:
            # Subject tenant not found in the Tenant table (e.g. deleted).
            # Emit a stable placeholder so the row still faces up.
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
    def _matches_narrowing(event: Event) -> bool:
        sid = _subject_of(event)
        if tenant_id is not None and sid != tenant_id:
            return False
        if builder_id is not None:
            entry = tenant_map.get(sid)
            row_builder_id = entry[1] if entry is not None else None
            if row_builder_id != builder_id:
                return False
        return True

    if not is_super:
        # Non-super-admin: `tenant_id` / `builder_id` query params were also
        # ignored in the pre-Group-4 handler; keep that contract (their
        # tenant-isolation clause in step 1 already scopes the row set).
        cut = list(events)
    else:
        cut = [e for e in events if _matches_narrowing(e)]

    # -- Step 5: summary over the cut (before only_* toggles) --------------
    def _row_has_reason(event: Event) -> bool:
        data = event.event_data or {}
        return _has_reason(data.get("reason"))

    def _is_accepted(event: Event) -> bool:
        data = event.event_data or {}
        return bool(data.get("approved_for_training", False))

    distribution: dict[str, int] = {str(i): 0 for i in range(1, 6)}
    total = 0
    rating_sum = 0
    accepted_count = 0
    for event in cut:
        data = event.event_data or {}
        try:
            rating_value = int(data.get("rating", 0))
        except (TypeError, ValueError):
            rating_value = 0
        if 1 <= rating_value <= 5:
            distribution[str(rating_value)] += 1
            total += 1
            rating_sum += rating_value
        # Group 5: `accepted_count` counts only entries that are BOTH
        # approved AND carry a qualifying reason. This mirrors the ONE archive
        # the panel actually surfaces (`only_with_reason=true&only_accepted=true`)
        # and excludes legacy approvals that predate this wish and have no
        # written critique on file.
        if _is_accepted(event) and _row_has_reason(event):
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
        filtered = [e for e in filtered if _is_accepted(e)]
    if only_with_reason:
        filtered = [e for e in filtered if _row_has_reason(e)]

    # -- Step 7: paginate AFTER filtering ----------------------------------
    page = filtered[offset : offset + limit]

    # Batched email resolution for `accepted_by` on the already-paginated page
    # (never one query per row — see wish Group 5, deliverable 4). Runs over
    # the page — not the full cut — because the panel only ever displays the
    # emails it renders.
    accepted_by_ids: set[UUID] = set()
    for event in page:
        raw = (event.event_data or {}).get("accepted_by")
        if raw:
            try:
                accepted_by_ids.add(UUID(str(raw)))
            except (ValueError, AttributeError):
                pass
    accepted_email_map: dict[UUID, str] = {}
    if accepted_by_ids:
        users_result = await session.execute(
            select(User.id, User.email).where(User.id.in_(accepted_by_ids))
        )
        accepted_email_map = {row.id: row.email for row in users_result.all()}

    entries = [
        _serialize_rating_event(
            e, tenant_map=tenant_map, accepted_email_map=accepted_email_map
        )
        for e in page
    ]

    return {"summary": summary, "facets": facets, "entries": entries}


@router.patch("/ratings/{event_id}")
async def update_rating_action(
    event_id: UUID,
    body: RatingActionRequest,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, Any]:
    """Approve or ignore a rating entry for dataset curation.

    Super admin only — approvals are training-dataset gatekeepers.
    """
    result = await session.execute(
        select(Event).where(
            Event.id == event_id,
            Event.event_type == "rlhf.rating",
        )
    )
    event = result.scalar_one_or_none()
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Rating not found",
        )

    data = dict(event.event_data or {})
    if body.action == "approve":
        # Group 5 guard: accepting an opinion is an act OVER written feedback.
        # Ratings without a qualifying reason (empty or under 8 chars) cannot
        # be approved — the training-lake export only ever contains rows the
        # operator explicitly endorsed the critique of, so silently accepting
        # a reasonless entry would poison the dataset with meaningless labels.
        if not _has_reason(data.get("reason")):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Cannot approve rating without a qualifying reason",
            )
        data["approved_for_training"] = True
        # Audit trail: stamp who accepted and when so the panel can render
        # authorship. `accepted_at` is ISO-8601 UTC to stay dialect-agnostic
        # (event_data is a JSON blob — no timestamptz coercion happens).
        data["accepted_by"] = str(tenant.user_id) if tenant.user_id else None
        data["accepted_at"] = datetime.now(timezone.utc).isoformat()
        data.pop("ignored", None)
    else:
        # `ignore` unchanged (per wish Group 5, deliverable 6): no stamping,
        # no reason guard — ignoring a low-value rating is orthogonal to the
        # accept flow's dataset-curation semantics.
        data["ignored"] = True
        data.pop("approved_for_training", None)

    event.event_data = data
    await session.commit()

    logger.info(
        "RLHF rating action: event_id=%s action=%s by=%s",
        event_id,
        body.action,
        tenant.user_id,
    )
    return {"ok": True, "id": str(event_id), "action": body.action}
