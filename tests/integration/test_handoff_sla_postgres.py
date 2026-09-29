"""Disposable PostgreSQL proof for the production handoff SLA radar path."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import sys
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL, make_url
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

_ROOT = Path(__file__).resolve().parents[2]
_CHILD_PATTERN = re.compile(r"^neurostack_handoff_radar_[0-9a-f]{12}$")
_LOCAL_SOCKET_DIRECTORIES = {"/run/postgresql", "/var/run/postgresql"}
SessionMaker = async_sessionmaker[AsyncSession]


def _database_identity(url: URL) -> tuple[str, int, str | None]:
    query_host = url.query.get("host")
    query_port = url.query.get("port")
    return (
        str(query_host or url.host or "").lower(),
        int(query_port or url.port or 5432),
        url.database,
    )


def _validated_admin_url(raw_url: str) -> URL:
    url = make_url(raw_url)
    if not url.drivername.startswith("postgresql"):
        raise ValueError("smoke URL must use PostgreSQL")
    if url.database != "postgres":
        raise ValueError("smoke URL must use the postgres administrative database")
    if url.query:
        if url.host is not None or set(url.query) - {"host", "port"}:
            raise ValueError("smoke URL has unsafe connection parameters")
        if str(url.query.get("host", "")) not in _LOCAL_SOCKET_DIRECTORIES:
            raise ValueError("smoke URL must use an allowlisted local socket")
        if str(url.query.get("port", "5432")) != "5432":
            raise ValueError("smoke URL must use the local PostgreSQL port")
    else:
        try:
            is_loopback = ipaddress.ip_address(url.host or "").is_loopback
        except ValueError:
            is_loopback = (url.host or "").lower() == "localhost"
        if not is_loopback or (url.port or 5432) != 5432:
            raise ValueError("smoke URL must use local PostgreSQL")
    if _database_identity(url) == _database_identity(make_url(settings.database_url)):
        raise ValueError("smoke URL must not be the application database")
    return url.set(drivername="postgresql+asyncpg")


async def _run_alembic(database_url: URL) -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url.render_as_string(
        hide_password=False
    ).replace("%", "%%")
    executable = Path(sys.executable).with_name("alembic")
    if not executable.is_file():
        raise RuntimeError("Alembic executable is unavailable beside the test Python")
    process = await asyncio.create_subprocess_exec(
        str(executable),
        "upgrade",
        "head",
        cwd=_ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    assert process.returncode == 0, output.decode()


@pytest.fixture(scope="module")
async def postgres_sla_database() -> AsyncIterator[SessionMaker]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")

    admin_url = _validated_admin_url(raw_admin_url)
    child_name = f"neurostack_handoff_radar_{uuid4().hex[:12]}"
    if _CHILD_PATTERN.fullmatch(child_name) is None:
        raise ValueError("unsafe handoff radar child database name")
    child_url = admin_url.set(database=child_name)
    admin_engine = create_async_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    child_engine: AsyncEngine | None = None
    created_database = False
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

        await _run_alembic(child_url)
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
                    active = (
                        (
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
                    )
                    if active:
                        raise AssertionError(
                            "child database still has active sessions; refusing "
                            f"process teardown: {[dict(row) for row in active]}"
                        )
                    quoted_name = connection.dialect.identifier_preparer.quote(
                        child_name
                    )
                    await connection.exec_driver_sql(f"DROP DATABASE {quoted_name}")
        finally:
            await admin_engine.dispose()


def _identity(tenant_id: UUID, session_id: UUID, timezone_name: str) -> list[object]:
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

    identities = [(uuid4(), uuid4()) for _ in range(7)]
    (
        (normative_tenant, normative_session),
        (other_tenant, other_session),
        (empty_tenant, empty_session),
        (rounding_tenant, rounding_session),
        (spring_tenant, spring_session),
        (fall_tenant, fall_session),
        (deadline_tenant, deadline_session),
    ) = identities

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
    exactly_due = _handoff(
        deadline_tenant,
        deadline_session,
        created_at=as_of - timedelta(seconds=600),
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

    spring_start = datetime(2026, 3, 8, 5, tzinfo=timezone.utc)
    spring_included = _handoff(
        spring_tenant,
        spring_session,
        created_at=spring_start,
    )
    spring_excluded = _handoff(
        spring_tenant,
        spring_session,
        created_at=datetime(2026, 3, 9, 4, tzinfo=timezone.utc),
    )
    fall_start = datetime(2026, 11, 1, 4, tzinfo=timezone.utc)
    fall_included = _handoff(
        fall_tenant,
        fall_session,
        created_at=fall_start,
    )
    fall_excluded = _handoff(
        fall_tenant,
        fall_session,
        created_at=datetime(2026, 11, 2, 5, tzinfo=timezone.utc),
    )

    async with factory() as database:
        async with database.begin():
            for tenant_id, session_id, zone in (
                (normative_tenant, normative_session, "America/Sao_Paulo"),
                (other_tenant, other_session, "America/Sao_Paulo"),
                (empty_tenant, empty_session, "UTC"),
                (rounding_tenant, rounding_session, "UTC"),
                (spring_tenant, spring_session, "America/New_York"),
                (fall_tenant, fall_session, "America/New_York"),
                (deadline_tenant, deadline_session, "UTC"),
            ):
                database.add_all(_identity(tenant_id, session_id, zone))
            database.add_all(
                [
                    *normative,
                    *outside,
                    isolated,
                    exactly_due,
                    *rounding,
                    spring_included,
                    spring_excluded,
                    fall_included,
                    fall_excluded,
                ]
            )

    async with factory() as database:
        await database.execute(sa.text("ANALYZE handoff_records"))
        plan_value = (
            await database.execute(
                sa.text(
                    "EXPLAIN (FORMAT JSON) "
                    "SELECT id, numero_whatsapp, session_id, context, reason, "
                    "intencao_detectada, estagio_atual, created_at, "
                    "data_hora_saida, sla_target_seconds, status "
                    "FROM handoff_records "
                    "WHERE tenant_id = :tenant_id "
                    "AND created_at >= :start_utc "
                    "AND created_at < :end_exclusive_utc "
                    "ORDER BY created_at ASC, id ASC"
                ),
                {
                    "tenant_id": normative_tenant,
                    "start_utc": start,
                    "end_exclusive_utc": datetime(2026, 9, 2, 3, tzinfo=timezone.utc),
                },
            )
        ).scalar_one()
        plan_document = (
            json.loads(plan_value) if isinstance(plan_value, str) else plan_value
        )
        assert isinstance(plan_document, list) and plan_document
        plan = plan_document[0]["Plan"]
        plan_text = json.dumps(plan)
        # Keep this representative gate tolerant of PostgreSQL planner choices;
        # it proves tenant/range predicates and a bounded cohort estimate without
        # forcing an evidence-free composite index.
        assert "tenant_id" in plan_text
        assert "created_at" in plan_text
        assert int(plan["Plan Rows"]) <= len(normative)

    radar = await service.get_queue_sla(
        normative_tenant,
        from_date=date(2026, 9, 1),
        to_date=date(2026, 9, 1),
        as_of=as_of,
    )
    assert radar.period.timezone_name == "America/Sao_Paulo"
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
    assert radar.within_sla_count == 2
    assert radar.confirmed_late_count == 1
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

    default_window = await service.get_queue_sla(
        empty_tenant,
        as_of=as_of,
    )
    assert default_window.period.start_date == date(2026, 8, 2)
    assert default_window.period.end_date == date(2026, 9, 1)
    assert (
        default_window.period.end_date - default_window.period.start_date
    ).days == 30

    deadline = await service.get_queue_sla(
        deadline_tenant,
        from_date=date(2026, 9, 1),
        to_date=date(2026, 9, 1),
        as_of=as_of,
    )
    assert [record.id for record in deadline.items] == [exactly_due.id]
    assert deadline.unanswered_overdue_count == 1
    assert deadline.maturing_count == 0
    assert deadline.metrics.sla_eligible_count == 1
    assert deadline.metrics.pct_within_sla == Decimal("0.0")

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

    for tenant_id, local_day, expected_start, expected_hours, included in (
        (spring_tenant, date(2026, 3, 8), spring_start, 23, spring_included),
        (fall_tenant, date(2026, 11, 1), fall_start, 25, fall_included),
    ):
        dst = await service.get_queue_sla(
            tenant_id,
            from_date=local_day,
            to_date=local_day,
            as_of=datetime(2026, 11, 3, tzinfo=timezone.utc),
        )
        assert [record.id for record in dst.items] == [included.id]
        assert dst.period.start_utc == expected_start
        assert dst.period.end_exclusive_utc - dst.period.start_utc == timedelta(
            hours=expected_hours
        )
        browser_start = datetime(
            local_day.year,
            local_day.month,
            local_day.day,
            tzinfo=ZoneInfo("Asia/Tokyo"),
        ).astimezone(timezone.utc)
        assert dst.period.start_utc != browser_start
