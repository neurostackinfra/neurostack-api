"""Disposable PostgreSQL proof for the production handoff SLA radar path."""

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.config.settings import settings
from src.models.clinic_config_table import ClinicConfigTable
from src.models.handoff import HandoffRecord
from src.models.session import Session
from src.models.tenant import Tenant
from src.services.handoff_service import HandoffService
from tests.api.test_handoff_sla_migration import _run_alembic, _validated_admin_url

_CHILD_PATTERN = re.compile(r"^neurostack_handoff_radar_[0-9a-f]{12}$")
SessionMaker = async_sessionmaker[AsyncSession]


def _validated_child_name(name: str) -> str:
    if _CHILD_PATTERN.fullmatch(name) is None:
        raise ValueError("unsafe handoff radar child database name")
    return name


@pytest.fixture(scope="module")
async def postgres_sla_database() -> AsyncIterator[SessionMaker]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")

    admin_url = _validated_admin_url(raw_admin_url, settings.database_url)
    child_name = _validated_child_name(f"neurostack_handoff_radar_{uuid4().hex[:12]}")
    child_url = admin_url.set(database=child_name)
    admin_engine = create_async_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    child_engine: AsyncEngine | None = None
    created_database = False
    unexpected_backends: list[dict[str, Any]] = []
    try:
        async with admin_engine.connect() as connection:
            can_create = bool(
                (
                    await connection.execute(
                        sa.text(
                            "SELECT rolcreatedb OR rolsuper "
                            "FROM pg_roles WHERE rolname = current_user"
                        )
                    )
                ).scalar_one()
            )
            if not can_create:
                raise PermissionError("smoke database user must have CREATEDB")
            quoted_name = connection.dialect.identifier_preparer.quote(child_name)
            await connection.exec_driver_sql(f"CREATE DATABASE {quoted_name}")
            created_database = True

        await _run_alembic(child_url, "upgrade", "head")
        child_engine = create_async_engine(child_url, poolclass=NullPool)
        factory = async_sessionmaker(child_engine, expire_on_commit=False)
        async with child_engine.connect() as connection:
            assert (
                await connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                )
                == "021"
            )
        yield factory
    finally:
        if child_engine is not None:
            await child_engine.dispose()
        try:
            if created_database:
                async with admin_engine.connect() as connection:
                    unexpected_backends = [
                        dict(row)
                        for row in (
                            await connection.execute(
                                sa.text(
                                    "SELECT pid, application_name, state "
                                    "FROM pg_stat_activity WHERE datname = :database "
                                    "AND pid <> pg_backend_pid() ORDER BY pid"
                                ),
                                {"database": child_name},
                            )
                        )
                        .mappings()
                        .all()
                    ]
                    await connection.execute(
                        sa.text(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = :database AND pid <> pg_backend_pid()"
                        ),
                        {"database": child_name},
                    )
                    quoted_name = connection.dialect.identifier_preparer.quote(
                        child_name
                    )
                    await connection.exec_driver_sql(f"DROP DATABASE {quoted_name}")
        finally:
            await admin_engine.dispose()
        assert not unexpected_backends, (
            "unexpected child database sessions required termination: "
            f"{unexpected_backends}"
        )


def _identity(tenant_id: UUID, session_id: UUID, timezone_name: str) -> list[Any]:
    return [
        Tenant(
            id=tenant_id,
            name=f"Radar {tenant_id}",
            slug=f"radar-{tenant_id}",
            api_key_hash=f"radar-{tenant_id}",
            config={},
        ),
        Session(
            id=session_id,
            tenant_id=tenant_id,
            user_id=f"radar-{session_id}",
            channel="test",
            cognitive_state={},
            session_metadata={},
        ),
        ClinicConfigTable(
            tenant_id=tenant_id,
            handoff={"meta_minutos_atendimento": 99},
            operations={"timezone": timezone_name},
        ),
    ]


def _handoff(
    tenant_id: UUID,
    session_id: UUID,
    *,
    created_at: datetime,
    status: str = "pending",
    confirmed_after: int | None = None,
    target: int | None = 600,
) -> HandoffRecord:
    return HandoffRecord(
        id=uuid4(),
        tenant_id=tenant_id,
        session_id=session_id,
        reason="user_request",
        summary="PostgreSQL SLA radar",
        context={"context_version": 1, "key_info": {}},
        urgency="normal",
        status=status,
        data_hora_saida=(
            created_at + timedelta(seconds=confirmed_after)
            if confirmed_after is not None
            else None
        ),
        sla_target_seconds=target,
        created_at=created_at,
    )


async def test_handoff_sla_postgres_smoke(
    postgres_sla_database: SessionMaker,
) -> None:
    factory = postgres_sla_database
    service = HandoffService(session_factory=factory)

    normative_tenant = uuid4()
    normative_session = uuid4()
    other_tenant = uuid4()
    other_session = uuid4()
    empty_tenant = uuid4()
    empty_session = uuid4()
    rounding_tenant = uuid4()
    rounding_session = uuid4()
    dst_tenant = uuid4()
    dst_session = uuid4()
    fall_back_tenant = uuid4()
    fall_back_session = uuid4()

    start = datetime(2026, 9, 1, 3, tzinfo=timezone.utc)
    as_of = datetime(2026, 9, 1, 6, tzinfo=timezone.utc)
    normative = [
        _handoff(
            normative_tenant,
            normative_session,
            created_at=start,
            status="completed",
            confirmed_after=599,
        ),
        _handoff(
            normative_tenant,
            normative_session,
            created_at=start + timedelta(seconds=1),
            status="in_progress",
            confirmed_after=600,
        ),
        _handoff(
            normative_tenant,
            normative_session,
            created_at=start + timedelta(seconds=2),
            status="completed",
            confirmed_after=601,
        ),
        _handoff(
            normative_tenant,
            normative_session,
            created_at=start + timedelta(minutes=1),
        ),
        _handoff(
            normative_tenant,
            normative_session,
            created_at=start + timedelta(minutes=2),
        ),
        _handoff(
            normative_tenant,
            normative_session,
            created_at=as_of - timedelta(seconds=599),
            status="accepted",
        ),
        _handoff(
            normative_tenant,
            normative_session,
            created_at=start + timedelta(minutes=3),
            status="cancelled",
        ),
        _handoff(
            normative_tenant,
            normative_session,
            created_at=start + timedelta(minutes=4),
            target=None,
        ),
    ]
    outside = [
        _handoff(
            normative_tenant,
            normative_session,
            created_at=start - timedelta(microseconds=1),
        ),
        _handoff(
            normative_tenant,
            normative_session,
            created_at=datetime(2026, 9, 2, 3, tzinfo=timezone.utc),
        ),
    ]
    isolated = _handoff(
        other_tenant,
        other_session,
        created_at=start,
        status="completed",
        confirmed_after=60,
    )

    rounding_start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    rounding = [
        _handoff(
            rounding_tenant,
            rounding_session,
            created_at=rounding_start,
            status="completed",
            confirmed_after=60,
        ),
        *[
            _handoff(
                rounding_tenant,
                rounding_session,
                created_at=rounding_start + timedelta(seconds=index),
                target=60,
            )
            for index in range(1, 16)
        ],
    ]

    dst_start = datetime(2026, 3, 8, 5, tzinfo=timezone.utc)
    dst_included = _handoff(
        dst_tenant,
        dst_session,
        created_at=dst_start,
    )
    dst_excluded = _handoff(
        dst_tenant,
        dst_session,
        created_at=datetime(2026, 3, 9, 4, tzinfo=timezone.utc),
    )

    fall_back_start = datetime(2026, 11, 1, 4, tzinfo=timezone.utc)
    fall_back_included = [
        _handoff(
            fall_back_tenant,
            fall_back_session,
            created_at=fall_back_start,
        ),
        _handoff(
            fall_back_tenant,
            fall_back_session,
            created_at=datetime(2026, 11, 2, 4, 59, 59, 999999, tzinfo=timezone.utc),
        ),
    ]
    fall_back_excluded = _handoff(
        fall_back_tenant,
        fall_back_session,
        created_at=datetime(2026, 11, 2, 5, tzinfo=timezone.utc),
    )

    async with factory() as database:
        async with database.begin():
            for args in (
                (normative_tenant, normative_session, "America/Sao_Paulo"),
                (other_tenant, other_session, "America/Sao_Paulo"),
                (empty_tenant, empty_session, "UTC"),
                (rounding_tenant, rounding_session, "UTC"),
                (dst_tenant, dst_session, "America/New_York"),
                (fall_back_tenant, fall_back_session, "America/New_York"),
            ):
                database.add_all(_identity(*args))
            database.add_all(
                [
                    *normative,
                    *outside,
                    isolated,
                    *rounding,
                    dst_included,
                    dst_excluded,
                    *fall_back_included,
                    fall_back_excluded,
                ]
            )

    async with factory() as database:
        await database.execute(sa.text("ANALYZE handoff_records"))
        explain_statement = sa.text(
            """
            EXPLAIN (FORMAT JSON)
            SELECT id, numero_whatsapp, session_id,
                   CASE
                       WHEN jsonb_typeof(context -> 'safety_concern') = 'boolean'
                       THEN (context ->> 'safety_concern')::boolean
                   END AS safety_concern,
                   reason, intencao_detectada, estagio_atual, created_at,
                   data_hora_saida, sla_target_seconds, status
            FROM handoff_records
            WHERE tenant_id = :tenant_id
              AND created_at >= :start_utc
              AND created_at < :end_exclusive_utc
            ORDER BY created_at ASC, id ASC
            """
        ).bindparams(
            sa.bindparam("tenant_id", type_=sa.Uuid()),
            sa.bindparam("start_utc", type_=sa.DateTime(timezone=True)),
            sa.bindparam("end_exclusive_utc", type_=sa.DateTime(timezone=True)),
        )
        raw_plan = (
            await database.execute(
                explain_statement,
                {
                    "tenant_id": normative_tenant,
                    "start_utc": start,
                    "end_exclusive_utc": datetime(2026, 9, 2, 3, tzinfo=timezone.utc),
                },
            )
        ).scalar_one()
        explained = json.loads(raw_plan) if isinstance(raw_plan, str) else raw_plan
        plan = explained[0]["Plan"]

        plan_nodes = [plan]
        for node in plan_nodes:
            plan_nodes.extend(node.get("Plans", []))
        predicates = " ".join(
            str(node.get(key, ""))
            for node in plan_nodes
            for key in ("Index Cond", "Recheck Cond", "Filter")
        )
        assert "tenant_id" in predicates
        assert "created_at" in predicates
        assert ">=" in predicates
        assert "<" in predicates
        assert 1 <= int(plan["Plan Rows"]) <= len(normative) + 2

    radar = await service.get_queue_sla(
        normative_tenant,
        from_date=date(2026, 9, 1),
        to_date=date(2026, 9, 1),
        as_of=as_of,
    )
    assert radar.period_timezone == "America/Sao_Paulo"
    assert radar.period.start_utc == start
    assert radar.period.end_exclusive_utc == datetime(
        2026, 9, 2, 3, tzinfo=timezone.utc
    )
    assert radar.period.as_of == as_of
    assert {record.id for record in radar.items} == {
        normative[3].id,
        normative[4].id,
        normative[5].id,
        normative[7].id,
    }
    assert radar.within_sla_count == 2  # 599s and exact-deadline 600s
    assert radar.confirmed_late_count == 1  # 601s
    assert radar.unanswered_overdue_count == 2
    assert radar.maturing_count == 1
    assert radar.cancelled_unconfirmed_count == 1
    assert radar.unmeasured_legacy_count == 1
    assert radar.metrics.confirmed_count == 3
    assert radar.metrics.sla_eligible_count == 5
    assert radar.metrics.average_confirmed_seconds == 600.0
    assert radar.metrics.pct_within_sla == Decimal("40.0")
    assert sum(
        (
            radar.within_sla_count,
            radar.confirmed_late_count,
            radar.unanswered_overdue_count,
            radar.maturing_count,
            radar.cancelled_unconfirmed_count,
            radar.unmeasured_legacy_count,
        )
    ) == len(normative)
    assert isolated.id not in {record.id for record in radar.items}

    no_data = await service.get_queue_sla(
        empty_tenant,
        from_date=date(2026, 9, 1),
        to_date=date(2026, 9, 1),
        as_of=as_of,
    )
    assert no_data.items == ()
    assert no_data.metrics.average_confirmed_seconds is None
    assert no_data.metrics.pct_within_sla is None
    assert no_data.metrics.confirmed_count == 0
    assert no_data.metrics.sla_eligible_count == 0

    half_up = await service.get_queue_sla(
        rounding_tenant,
        from_date=date(2026, 10, 1),
        to_date=date(2026, 10, 1),
        as_of=datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
    )
    assert half_up.within_sla_count == 1
    assert half_up.unanswered_overdue_count == 15
    assert half_up.metrics.sla_eligible_count == 16
    assert half_up.metrics.pct_within_sla == Decimal("6.3")

    dst = await service.get_queue_sla(
        dst_tenant,
        from_date=date(2026, 3, 8),
        to_date=date(2026, 3, 8),
        as_of=datetime(2026, 3, 10, tzinfo=timezone.utc),
    )
    assert [record.id for record in dst.items] == [dst_included.id]
    assert dst.period.start_utc == dst_start
    assert dst.period.end_exclusive_utc == datetime(2026, 3, 9, 4, tzinfo=timezone.utc)
    assert dst.period.end_exclusive_utc - dst.period.start_utc == timedelta(hours=23)
    browser_start = datetime(
        2026,
        3,
        8,
        tzinfo=ZoneInfo("Asia/Tokyo"),
    ).astimezone(timezone.utc)
    assert dst.period.start_utc != browser_start

    fall_back = await service.get_queue_sla(
        fall_back_tenant,
        from_date=date(2026, 11, 1),
        to_date=date(2026, 11, 1),
        as_of=datetime(2026, 11, 3, tzinfo=timezone.utc),
    )
    assert [record.id for record in fall_back.items] == [
        record.id for record in fall_back_included
    ]
    assert fall_back.period.start_utc == fall_back_start
    assert fall_back.period.end_exclusive_utc == datetime(
        2026, 11, 2, 5, tzinfo=timezone.utc
    )
    assert fall_back.period.end_exclusive_utc - fall_back.period.start_utc == timedelta(
        hours=25
    )
