"""Builder-scoped subscription usage metering."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.event import Event
from src.models.knowledge import KnowledgeDocument
from src.models.media_asset import MediaAsset
from src.models.tenant import Tenant

BYTES_PER_MB = 1_000_000


@dataclass(frozen=True)
class PlanUsageLimits:
    """Numeric entitlements attached to a commercial subscription plan."""

    monthly_traces: int
    rag_storage_mb: int
    media_storage_mb: int


def _monthly_period(now: datetime) -> tuple[datetime, datetime]:
    """Return the half-open UTC calendar-month window containing ``now``."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _percentage(used: int, limit: int) -> float:
    if limit <= 0:
        return 100.0 if used > 0 else 0.0
    return round(used / limit * 100, 2)


def _trace_usage(used: int, included: int) -> dict[str, int | float]:
    extra_credits = 0
    total_limit = included + extra_credits
    return {
        "used": used,
        "included": included,
        "extra_credits": extra_credits,
        "available": max(total_limit - used, 0),
        "overage": max(used - total_limit, 0),
        "percent_used": _percentage(used, total_limit),
    }


def _storage_usage(used_bytes: int, limit_mb: int) -> dict[str, int | float]:
    limit_bytes = limit_mb * BYTES_PER_MB
    available_bytes = max(limit_bytes - used_bytes, 0)
    overage_bytes = max(used_bytes - limit_bytes, 0)
    return {
        "used_bytes": used_bytes,
        "used_mb": round(used_bytes / BYTES_PER_MB, 2),
        "limit_bytes": limit_bytes,
        "limit_mb": limit_mb,
        "available_bytes": available_bytes,
        "available_mb": round(available_bytes / BYTES_PER_MB, 2),
        "overage_bytes": overage_bytes,
        "overage_mb": round(overage_bytes / BYTES_PER_MB, 2),
        "percent_used": _percentage(used_bytes, limit_bytes),
    }


async def get_builder_billing_usage(
    db: AsyncSession,
    builder_id: UUID,
    plan: str | None,
    limits: PlanUsageLimits,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate current usage across every clinic owned by a builder."""
    as_of = now or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    else:
        as_of = as_of.astimezone(timezone.utc)
    period_start, period_end = _monthly_period(as_of)

    tenant_ids = select(Tenant.id).where(Tenant.builder_id == builder_id)

    trace_result = await db.execute(
        select(func.count(Event.id)).where(
            Event.tenant_id.in_(tenant_ids),
            Event.event_type == "message.in",
            Event.created_at >= period_start,
            Event.created_at < period_end,
        )
    )
    traces_used = int(trace_result.scalar() or 0)

    rag_result = await db.execute(
        select(func.coalesce(func.sum(KnowledgeDocument.size_bytes), 0)).where(
            KnowledgeDocument.tenant_id.in_(tenant_ids)
        )
    )
    rag_used_bytes = int(rag_result.scalar() or 0)

    media_result = await db.execute(
        select(func.coalesce(func.sum(MediaAsset.size_bytes), 0)).where(
            MediaAsset.tenant_id.in_(tenant_ids)
        )
    )
    media_used_bytes = int(media_result.scalar() or 0)

    return {
        "plan": plan,
        "as_of": as_of,
        "trace_period": {
            "start": period_start,
            "end": period_end,
            "timezone": "UTC",
        },
        "traces": _trace_usage(traces_used, limits.monthly_traces),
        "storage": {
            "rag": _storage_usage(rag_used_bytes, limits.rag_storage_mb),
            "media": _storage_usage(media_used_bytes, limits.media_storage_mb),
        },
        "credits": {
            "purchase_enabled": False,
            "coming_soon": True,
        },
    }


__all__ = [
    "BYTES_PER_MB",
    "PlanUsageLimits",
    "get_builder_billing_usage",
]
