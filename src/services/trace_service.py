"""
NeuroStack API - Trace Service
Minimal query builders for read-only trace views over the events table.
"""

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.event import Event


def _base_trace_query(tenant_id: UUID):
    """
    Build base query for trace views.

    Traces are message.in events that have an associated SRM decision
    (indicated by the presence of decision-related data in event_data
    or event_metadata).
    """
    return (
        select(Event)
        .where(
            Event.tenant_id == tenant_id,
            Event.event_type == "message.in",
        )
        .order_by(Event.created_at.desc())
    )


def _to_utc(dt: datetime) -> datetime:
    """Convert naive datetime (assumed BRT / UTC-3) to UTC-aware."""
    if dt.tzinfo is not None:
        return dt
    from datetime import timezone, timedelta
    brt = timezone(timedelta(hours=-3))
    return dt.replace(tzinfo=brt).astimezone(timezone.utc)


def _apply_filters(
    query,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    safety_flag: Optional[bool] = None,
    handoff_required: Optional[bool] = None,
    channel: Optional[str] = None,
):
    """Apply optional filters to a trace query."""
    if start_date is not None:
        query = query.where(Event.created_at >= _to_utc(start_date))
    if end_date is not None:
        # When end_date arrives as "YYYY-MM-DD" (midnight), include the whole day
        if end_date.hour == 0 and end_date.minute == 0 and end_date.second == 0:
            end_date = end_date.replace(hour=23, minute=59, second=59)
        query = query.where(Event.created_at <= _to_utc(end_date))
    # Safety flag and handoff_required are stored in event_metadata
    # or associated srm.decision events. Filter via JSONB operators.
    if safety_flag is not None:
        query = query.where(
            Event.event_metadata["safety_concern"].as_boolean() == safety_flag
        )
    if handoff_required is not None:
        query = query.where(
            Event.event_metadata["handoff_required"].as_boolean() == handoff_required
        )
    if channel is not None:
        query = query.where(
            Event.event_data["channel"].as_string() == channel
        )
    return query


def _event_to_trace_dict(event: Event) -> dict:
    """
    Convert an Event model to a trace dictionary.

    Extracts decision data from event_data and event_metadata
    to build the trace representation.
    """
    event_data = event.event_data or {}
    metadata = event.event_metadata or {}

    # Extract SRM decision from event_data or metadata
    # The decision may be stored inline or as a nested object
    srm_decision = event_data.get("decision", {})
    if not srm_decision:
        # Build decision from top-level keys in event_data
        srm_decision = {
            "intent": event_data.get("intent", metadata.get("intent", "unknown")),
            "intent_category": event_data.get("intent_category", metadata.get("intent_category", "unknown")),
            "funnel_stage": event_data.get("funnel_stage", metadata.get("funnel_stage", "unknown")),
            "action": event_data.get("action", metadata.get("action", "unknown")),
            "confidence": event_data.get("confidence", metadata.get("confidence", 0.0)),
            "safety_concern": event_data.get("safety_concern", metadata.get("safety_concern", False)),
            "handoff_required": event_data.get("handoff_required", metadata.get("handoff_required", False)),
        }

    return {
        "id": event.id,
        "timestamp": event.created_at.isoformat() if event.created_at else "",
        "user_message": event_data.get("content", ""),
        "srm_decision": srm_decision,
        "context_summary": event_data.get("context_summary", metadata.get("context_summary")),
        "prompt_built": event_data.get("prompt_built", metadata.get("prompt_built")),
        "processing_time_ms": metadata.get("processing_time_ms"),
        "mode": metadata.get("mode", event_data.get("mode", "normal")),
        "channel": event_data.get("channel", "unknown"),
    }


async def list_traces(
    db: AsyncSession,
    tenant_id: UUID,
    limit: int = 20,
    offset: int = 0,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    safety_flag: Optional[bool] = None,
    handoff_required: Optional[bool] = None,
    channel: Optional[str] = None,
) -> tuple[list[Event], int]:
    """
    List traces with pagination and optional filters.

    Args:
        db: Async database session
        tenant_id: Tenant identifier for scoping
        limit: Maximum number of items to return
        offset: Pagination offset
        start_date: Optional start date filter
        end_date: Optional end date filter
        safety_flag: Optional safety concern filter
        handoff_required: Optional handoff required filter
        channel: Optional channel filter (whatsapp, web)

    Returns:
        Tuple of (list of Event objects, total count)
    """
    base = _base_trace_query(tenant_id)
    base = _apply_filters(base, start_date, end_date, safety_flag, handoff_required, channel)

    # Count query
    count_query = select(func.count()).select_from(base.subquery())
    count_result = await db.execute(count_query)
    total = count_result.scalar() or 0

    # Data query with pagination
    data_query = base.limit(limit).offset(offset)
    result = await db.execute(data_query)
    events = list(result.scalars().all())

    return events, total


async def get_trace(
    db: AsyncSession,
    tenant_id: UUID,
    trace_id: UUID,
) -> Optional[Event]:
    """
    Get a single trace by ID, scoped to tenant.

    Args:
        db: Async database session
        tenant_id: Tenant identifier for scoping
        trace_id: Event ID to retrieve

    Returns:
        Event if found and belongs to tenant, None otherwise
    """
    query = select(Event).where(
        Event.id == trace_id,
        Event.tenant_id == tenant_id,
        Event.event_type == "message.in",
    )
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_prompt_event_for_trace(
    db: AsyncSession,
    tenant_id: UUID,
    session_id: UUID,
    trace_created_at,
) -> Optional[Event]:
    """
    Fetch the prompt.generated event associated with a message.in trace.

    Looks for the prompt.generated event in the same session that was
    created right after (within 5 seconds of) the message.in event.
    """
    from datetime import timedelta

    query = (
        select(Event)
        .where(
            Event.tenant_id == tenant_id,
            Event.session_id == session_id,
            Event.event_type == "prompt.generated",
            Event.created_at >= trace_created_at,
            Event.created_at <= trace_created_at + timedelta(seconds=5),
        )
        .order_by(Event.created_at.asc())
        .limit(1)
    )
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_trace_stats(
    db: AsyncSession,
    tenant_id: UUID,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> dict:
    """
    Get aggregated trace statistics for a time period.

    Args:
        db: Async database session
        tenant_id: Tenant identifier for scoping
        start_date: Start of period (default: 7 days ago)
        end_date: End of period (default: now)

    Returns:
        Dictionary with aggregated statistics
    """
    now = datetime.now(timezone.utc)
    if end_date is None:
        end_date = now
    else:
        if end_date.hour == 0 and end_date.minute == 0 and end_date.second == 0:
            end_date = end_date.replace(hour=23, minute=59, second=59)
        end_date = _to_utc(end_date)
    if start_date is None:
        start_date = now - timedelta(days=7)
    else:
        start_date = _to_utc(start_date)

    base = _base_trace_query(tenant_id)
    base = base.where(
        Event.created_at >= start_date,
        Event.created_at <= end_date,
    )

    # Fetch all matching events for aggregation
    result = await db.execute(base)
    events = list(result.scalars().all())

    total = len(events)
    processing_times = []
    safety_count = 0
    handoff_count = 0
    intent_dist: dict[str, int] = {}
    total_tokens_sum = 0
    model_tokens: dict[str, int] = {}

    for event in events:
        metadata = event.event_metadata or {}
        event_data = event.event_data or {}
        decision = event_data.get("decision", {})

        # Processing time
        pt = metadata.get("processing_time_ms")
        if pt is not None:
            processing_times.append(pt)

        # Safety flags
        safety = decision.get("safety_concern", event_data.get("safety_concern", metadata.get("safety_concern", False)))
        if safety:
            safety_count += 1

        # Handoff
        ho = decision.get("handoff_required", event_data.get("handoff_required", metadata.get("handoff_required", False)))
        if ho:
            handoff_count += 1

        # Intent distribution
        intent = decision.get("intent", event_data.get("intent", metadata.get("intent", "unknown")))
        intent_dist[intent] = intent_dist.get(intent, 0) + 1

        # Token usage (LLM)
        llm_usage = metadata.get("llm_usage", {})
        llm_model = metadata.get("llm_model", "")
        tokens = llm_usage.get("total_tokens", 0)
        if tokens:
            total_tokens_sum += tokens
            if llm_model:
                model_tokens[llm_model] = model_tokens.get(llm_model, 0) + tokens
        # Audio transcription (Whisper) — track separately with audio seconds as unit
        whisper_model = metadata.get("whisper_model")
        if whisper_model and metadata.get("is_audio"):
            audio_sec = int(metadata.get("audio_seconds") or 0)
            model_tokens[f"🎙️ {whisper_model}"] = model_tokens.get(f"🎙️ {whisper_model}", 0) + max(audio_sec, 1)

    avg_pt = (sum(processing_times) / len(processing_times)) if processing_times else 0.0

    model_breakdown = [
        {"model": model, "tokens": toks}
        for model, toks in sorted(model_tokens.items(), key=lambda x: x[1], reverse=True)
    ]

    return {
        "total_traces": total,
        "avg_processing_time_ms": round(avg_pt, 2),
        "safety_flags_count": safety_count,
        "handoff_count": handoff_count,
        "total_tokens_sum": total_tokens_sum,
        "model_breakdown": model_breakdown,
        "intent_distribution": intent_dist,
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
    }


async def export_traces_csv(
    db: AsyncSession,
    tenant_id: UUID,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    safety_flag: Optional[bool] = None,
    handoff_required: Optional[bool] = None,
    channel: Optional[str] = None,
) -> str:
    """
    Export traces as a CSV string.

    Args:
        db: Async database session
        tenant_id: Tenant identifier for scoping
        start_date: Optional start date filter
        end_date: Optional end date filter
        safety_flag: Optional safety concern filter
        handoff_required: Optional handoff required filter
        channel: Optional channel filter (whatsapp, web)

    Returns:
        CSV formatted string with trace data
    """
    base = _base_trace_query(tenant_id)
    base = _apply_filters(base, start_date, end_date, safety_flag, handoff_required, channel)

    result = await db.execute(base)
    events = list(result.scalars().all())

    output = io.StringIO()
    writer = csv.writer(output)

    # Header row
    writer.writerow([
        "id",
        "timestamp",
        "user_message",
        "intent",
        "intent_category",
        "funnel_stage",
        "action",
        "confidence",
        "safety_concern",
        "handoff_required",
        "processing_time_ms",
        "mode",
        "context_summary",
    ])

    for event in events:
        trace = _event_to_trace_dict(event)
        decision = trace["srm_decision"]
        writer.writerow([
            str(trace["id"]),
            trace["timestamp"],
            trace["user_message"],
            decision.get("intent", ""),
            decision.get("intent_category", ""),
            decision.get("funnel_stage", ""),
            decision.get("action", ""),
            decision.get("confidence", ""),
            decision.get("safety_concern", ""),
            decision.get("handoff_required", ""),
            trace["processing_time_ms"] or "",
            trace["mode"],
            trace["context_summary"] or "",
        ])

    return output.getvalue()
