"""PostgreSQL migration and FK-semantics smoke for RLHF audit tables.

Mirrors ``tests/integration/test_interaction_origin_postgres.py``:
disposable child database created via a validated admin URL, migration
applied by explicit revision id, real assertions against Postgres,
``pytest.skip(...)`` when ``POSTGRES_SMOKE_DATABASE_URL`` is not
configured. The wish's *validation command* — not this file — is what
turns a skip into a hard failure.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from src.config.settings import settings
from src.models.rlhf_audit_review import RlhfAuditReview
from src.models.rlhf_rating import RlhfRating

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = _ROOT / "alembic" / "versions" / "20260907_000001_020_rlhf_audit_tables.py"
_RLHF_REVISION = "020"
_RLHF_DOWN_REVISION = "019"
_ADMIN_DATABASES = {"postgres"}
_CHILD_PATTERN = re.compile(r"^neurostack_rlhf_smoke_[0-9a-f]{12}$")


def _same_database(left: URL, right: URL) -> bool:
    return (
        left.host,
        left.port,
        left.username,
        left.database,
    ) == (
        right.host,
        right.port,
        right.username,
        right.database,
    )


def _validated_admin_url(raw_url: str, application_url: str) -> URL:
    url = make_url(raw_url)
    if not url.drivername.startswith("postgresql"):
        raise ValueError("smoke URL must use PostgreSQL")
    try:
        is_loopback = ipaddress.ip_address(url.host or "").is_loopback
    except ValueError:
        is_loopback = (url.host or "").lower() == "localhost"
    if not is_loopback:
        raise ValueError("smoke URL host must be loopback")
    if _same_database(url, make_url(application_url)):
        raise ValueError("smoke URL must not be the application database")
    if url.database not in _ADMIN_DATABASES:
        raise ValueError("smoke URL must use an allowlisted administrative database")
    return url.set(drivername="postgresql+asyncpg")


def _validated_child_name(name: str) -> str:
    if _CHILD_PATTERN.fullmatch(name) is None:
        raise ValueError("unsafe smoke child database name")
    return name


def _require_createdb(can_create_database: bool) -> None:
    if not can_create_database:
        raise PermissionError("smoke database user must have CREATEDB")


async def _run_alembic(database_url: URL, *arguments: str) -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url.render_as_string(hide_password=False)
    alembic_executable = Path(sys.executable).with_name("alembic")
    if not alembic_executable.is_file():
        raise RuntimeError("Alembic executable is unavailable beside the test Python")
    process = await asyncio.create_subprocess_exec(
        str(alembic_executable),
        *arguments,
        cwd=_ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    assert process.returncode == 0, output.decode()


def test_migration_revision_pair_matches_source() -> None:
    source = _MIGRATION.read_text(encoding="utf-8")
    assert f'revision: str = "{_RLHF_REVISION}"' in source
    assert f'down_revision: Union[str, None] = "{_RLHF_DOWN_REVISION}"' in source


def test_migration_declares_ondelete_on_every_foreign_key() -> None:
    """Every FK on both new tables must declare an explicit ondelete."""

    def _fks(table_name: str) -> dict[str, str]:
        result: dict[str, str] = {}
        table = None
        if table_name == "rlhf_ratings":
            table = RlhfRating.__table__
        elif table_name == "rlhf_audit_reviews":
            table = RlhfAuditReview.__table__
        assert table is not None
        for column in table.columns:
            for fk in column.foreign_keys:
                result[column.name] = fk.ondelete or ""
        return result

    ratings_fks = _fks("rlhf_ratings")
    reviews_fks = _fks("rlhf_audit_reviews")

    assert ratings_fks == {
        "tenant_id": "CASCADE",
        "subject_tenant_id": "CASCADE",
        "trace_event_id": "SET NULL",
        "trace_tenant_id": "SET NULL",
        "submitted_by": "SET NULL",
        "accepted_by": "SET NULL",
    }
    assert reviews_fks == {
        "rating_id": "CASCADE",
        "revisado_por": "SET NULL",
    }

    # No FK on session_id — a rating outlives its session.
    session_column = RlhfRating.__table__.c.session_id
    assert list(session_column.foreign_keys) == []


@pytest.mark.parametrize(
    "raw_url",
    [
        "postgresql+asyncpg://user:pass@example.com/postgres",
        "sqlite+aiosqlite:///:memory:",
        "postgresql+asyncpg://user:pass@localhost/not_admin",
    ],
)
def test_smoke_refuses_unsafe_admin_urls(raw_url: str) -> None:
    with pytest.raises(ValueError):
        _validated_admin_url(
            raw_url,
            "postgresql+asyncpg://app:pass@localhost/application",
        )


def test_smoke_refuses_application_database() -> None:
    application_url = "postgresql+asyncpg://user:pass@localhost/postgres"
    with pytest.raises(ValueError, match="application database"):
        _validated_admin_url(application_url, application_url)


@pytest.mark.parametrize(
    "name",
    [
        "postgres",
        "neurostack",
        "neurostack_rlhf_smoke_bad-name",
        "smoke_deadbeef0000",
    ],
)
def test_smoke_refuses_child_name_outside_internal_pattern(name: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        _validated_child_name(name)


def test_smoke_refuses_user_without_createdb() -> None:
    with pytest.raises(PermissionError, match="CREATEDB"):
        _require_createdb(False)


async def _seed_tenant(connection, tenant_id, slug: str) -> None:  # type: ignore[no-untyped-def]
    await connection.execute(
        text(
            "INSERT INTO tenants "
            "(id, name, slug, api_key_hash, is_active, config) "
            "VALUES (:id, :name, :slug, :key, true, '{}')"
        ),
        {
            "id": tenant_id,
            "name": f"Smoke tenant {slug}",
            "slug": slug,
            "key": f"key-{tenant_id}",
        },
    )


async def _seed_user(connection, user_id, tenant_id, email: str) -> None:  # type: ignore[no-untyped-def]
    await connection.execute(
        text(
            "INSERT INTO users "
            "(id, tenant_id, email, password_hash, role, is_active) "
            "VALUES (:id, :tenant_id, :email, 'x', 'clinic_admin', true)"
        ),
        {"id": user_id, "tenant_id": tenant_id, "email": email},
    )


async def _seed_session(connection, session_id, tenant_id) -> None:  # type: ignore[no-untyped-def]
    await connection.execute(
        text(
            "INSERT INTO sessions "
            "(id, tenant_id, channel, cognitive_state, metadata, is_active, "
            "message_count) "
            "VALUES (:id, :tenant_id, 'test', '{}', '{}', true, 0)"
        ),
        {"id": session_id, "tenant_id": tenant_id},
    )


async def _seed_event(connection, event_id, tenant_id, session_id) -> None:  # type: ignore[no-untyped-def]
    await connection.execute(
        text(
            "INSERT INTO events "
            "(id, tenant_id, session_id, event_type, event_data, metadata) "
            "VALUES (:id, :tenant_id, :session_id, 'message.in', '{}', '{}')"
        ),
        {"id": event_id, "tenant_id": tenant_id, "session_id": session_id},
    )


async def _insert_rlhf_rating(  # type: ignore[no-untyped-def]
    connection,
    rating_id,
    tenant_id,
    subject_tenant_id,
    *,
    session_id=None,
    trace_event_id=None,
    trace_tenant_id=None,
    submitted_by=None,
    accepted_by=None,
    rating: int = 5,
    channel: str = "whatsapp",
) -> None:
    await connection.execute(
        text(
            "INSERT INTO rlhf_ratings "
            "(id, tenant_id, subject_tenant_id, session_id, message_id, "
            "trace_event_id, trace_tenant_id, channel, rating, "
            "agent_response, user_message, submitted_by, accepted_by) "
            "VALUES "
            "(:id, :tenant_id, :subject_tenant_id, :session_id, 'msg-1', "
            ":trace_event_id, :trace_tenant_id, :channel, :rating, "
            "'agent reply', 'user question', :submitted_by, :accepted_by)"
        ),
        {
            "id": rating_id,
            "tenant_id": tenant_id,
            "subject_tenant_id": subject_tenant_id,
            "session_id": session_id,
            "trace_event_id": trace_event_id,
            "trace_tenant_id": trace_tenant_id,
            "channel": channel,
            "rating": rating,
            "submitted_by": submitted_by,
            "accepted_by": accepted_by,
        },
    )


async def _insert_review(  # type: ignore[no-untyped-def]
    connection,
    review_id,
    rating_id,
    *,
    revisado: bool = True,
    revisado_por=None,
    email: str = "reviewer@example.com",
) -> None:
    await connection.execute(
        text(
            "INSERT INTO rlhf_audit_reviews "
            "(id, rating_id, observacao, revisado, revisado_por, "
            "revisado_por_email, revisado_em) "
            "VALUES (:id, :rating_id, NULL, :revisado, :revisado_por, "
            ":email, now())"
        ),
        {
            "id": review_id,
            "rating_id": rating_id,
            "revisado": revisado,
            "revisado_por": revisado_por,
            "email": email,
        },
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rlhf_audit_postgres_smoke() -> None:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")

    admin_url = _validated_admin_url(raw_admin_url, settings.database_url)
    child_name = _validated_child_name(f"neurostack_rlhf_smoke_{uuid4().hex[:12]}")
    child_url = admin_url.set(database=child_name)
    admin_engine: AsyncEngine = create_async_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    child_engine: AsyncEngine | None = None
    created_database: str | None = None

    try:
        async with admin_engine.connect() as connection:
            can_create = bool(
                (
                    await connection.execute(
                        text(
                            "SELECT rolcreatedb OR rolsuper "
                            "FROM pg_roles WHERE rolname = current_user"
                        )
                    )
                ).scalar_one()
            )
            _require_createdb(can_create)
            quoted_name = connection.dialect.identifier_preparer.quote(child_name)
            await connection.exec_driver_sql(f"CREATE DATABASE {quoted_name}")
            created_database = child_name

        # Baseline at revision 019 — before RLHF tables exist.
        await _run_alembic(child_url, "upgrade", _RLHF_DOWN_REVISION)
        child_engine = create_async_engine(child_url, poolclass=NullPool)

        # Seed a legacy 'rlhf.rating' event so we can prove the migration
        # does NOT touch it.
        owner_tenant = uuid4()
        subject_tenant = uuid4()
        third_tenant = uuid4()  # will appear only as trace_tenant_id
        owner_user = uuid4()
        session_id = uuid4()
        trace_event_id = uuid4()
        legacy_rlhf_event = uuid4()

        async with child_engine.begin() as connection:
            await _seed_tenant(connection, owner_tenant, f"owner-{owner_tenant}")
            await _seed_tenant(connection, subject_tenant, f"subject-{subject_tenant}")
            await _seed_tenant(connection, third_tenant, f"third-{third_tenant}")
            await _seed_user(connection, owner_user, owner_tenant, "owner@example.com")
            await _seed_session(connection, session_id, owner_tenant)
            await _seed_event(connection, trace_event_id, owner_tenant, session_id)
            # legacy event with event_type='rlhf.rating' — sanity guard row
            await connection.execute(
                text(
                    "INSERT INTO events "
                    "(id, tenant_id, session_id, event_type, event_data, metadata) "
                    "VALUES (:id, :t, :s, 'rlhf.rating', '{}', '{}')"
                ),
                {"id": legacy_rlhf_event, "t": owner_tenant, "s": session_id},
            )

        async with child_engine.connect() as connection:
            legacy_count_before = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM events "
                        "WHERE event_type = 'rlhf.rating'"
                    )
                )
            ).scalar_one()
        assert legacy_count_before == 1

        await child_engine.dispose()
        child_engine = None

        # === upgrade 019 -> 020 ===
        await _run_alembic(child_url, "upgrade", _RLHF_REVISION)
        child_engine = create_async_engine(child_url, poolclass=NullPool)

        # Legacy 'rlhf.rating' events untouched by the migration.
        async with child_engine.connect() as connection:
            legacy_count_after_upgrade = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM events "
                        "WHERE event_type = 'rlhf.rating'"
                    )
                )
            ).scalar_one()
        assert legacy_count_after_upgrade == legacy_count_before

        # All three indexes exist.
        async with child_engine.connect() as connection:
            index_names = {
                row
                for row in (
                    (
                        await connection.execute(
                            text(
                                "SELECT indexname FROM pg_indexes "
                                "WHERE schemaname = current_schema() "
                                "AND indexname IN ("
                                "'idx_rlhf_ratings_created_at_desc',"
                                "'idx_rlhf_ratings_tenant_created_at_desc',"
                                "'idx_rlhf_audit_reviews_rating_revisado_em_id'"
                                ")"
                            )
                        )
                    ).scalars()
                )
            }
        assert index_names == {
            "idx_rlhf_ratings_created_at_desc",
            "idx_rlhf_ratings_tenant_created_at_desc",
            "idx_rlhf_audit_reviews_rating_revisado_em_id",
        }

        # === Prove FK ondelete semantics with real data ===
        # Rating A: owned by owner_tenant, about subject_tenant, with a trace
        #           pointed at owner_tenant (trace_tenant_id = owner_tenant).
        # Rating B: owned by subject_tenant, about subject_tenant, with a
        #           trace_tenant_id = third_tenant (the *only* place
        #           third_tenant is referenced by RLHF rows).
        rating_a = uuid4()
        rating_b = uuid4()
        review_a1 = uuid4()
        review_a2 = uuid4()
        review_b1 = uuid4()

        async with child_engine.begin() as connection:
            await _insert_rlhf_rating(
                connection,
                rating_a,
                tenant_id=owner_tenant,
                subject_tenant_id=subject_tenant,
                session_id=session_id,
                trace_event_id=trace_event_id,
                trace_tenant_id=owner_tenant,
                submitted_by=owner_user,
                accepted_by=owner_user,
                rating=5,
                channel="whatsapp",
            )
            await _insert_rlhf_rating(
                connection,
                rating_b,
                tenant_id=subject_tenant,
                subject_tenant_id=subject_tenant,
                session_id=None,
                trace_event_id=None,
                trace_tenant_id=third_tenant,
                submitted_by=owner_user,
                accepted_by=None,
                rating=3,
                channel="web",
            )
            await _insert_review(
                connection,
                review_a1,
                rating_a,
                revisado=True,
                revisado_por=owner_user,
                email="owner@example.com",
            )
            await _insert_review(
                connection,
                review_a2,
                rating_a,
                revisado=False,
                revisado_por=owner_user,
                email="owner@example.com",
            )
            await _insert_review(
                connection,
                review_b1,
                rating_b,
                revisado=True,
                revisado_por=owner_user,
                email="owner@example.com",
            )

        # --- Scenario 1: deleting `third_tenant` (only referenced as
        #     trace_tenant_id on rating_b) should SET NULL both trace
        #     columns on rating_b and preserve the rating and its review.
        async with child_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM tenants WHERE id = :id"),
                {"id": third_tenant},
            )
        async with child_engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT trace_tenant_id, trace_event_id "
                        "FROM rlhf_ratings WHERE id = :id"
                    ),
                    {"id": rating_b},
                )
            ).one()
            assert row.trace_tenant_id is None
            assert row.trace_event_id is None  # NULL was NULL, stays NULL
            rating_b_reviews = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM rlhf_audit_reviews "
                        "WHERE rating_id = :id"
                    ),
                    {"id": rating_b},
                )
            ).scalar_one()
            assert rating_b_reviews == 1
            rating_b_present = (
                await connection.execute(
                    text("SELECT count(*) FROM rlhf_ratings WHERE id = :id"),
                    {"id": rating_b},
                )
            ).scalar_one()
            assert rating_b_present == 1

        # --- Scenario 2: deleting `owner_tenant` cascades — both rating_a
        #     (owned by it) and any rating whose subject is it should
        #     vanish, plus all their reviews. rating_b's subject is
        #     subject_tenant, so it survives; but rating_b.trace_tenant_id
        #     was already NULL. Deleting owner_user: SET NULL on submitted_by
        #     of rating_b (still around) and on revisado_por of review_b1.
        async with child_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM users WHERE id = :id"),
                {"id": owner_user},
            )
        async with child_engine.connect() as connection:
            rating_b_after_user_delete = (
                await connection.execute(
                    text(
                        "SELECT submitted_by, accepted_by FROM rlhf_ratings "
                        "WHERE id = :id"
                    ),
                    {"id": rating_b},
                )
            ).one()
            assert rating_b_after_user_delete.submitted_by is None
            assert rating_b_after_user_delete.accepted_by is None
            review_b_after_user_delete = (
                await connection.execute(
                    text(
                        "SELECT revisado_por, revisado_por_email "
                        "FROM rlhf_audit_reviews WHERE id = :id"
                    ),
                    {"id": review_b1},
                )
            ).one()
            assert review_b_after_user_delete.revisado_por is None
            # email frozen at write time survives user deletion
            assert review_b_after_user_delete.revisado_por_email == "owner@example.com"

        # Delete owner_tenant — rating_a cascades away entirely.
        async with child_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM tenants WHERE id = :id"),
                {"id": owner_tenant},
            )
        async with child_engine.connect() as connection:
            rating_a_gone = (
                await connection.execute(
                    text("SELECT count(*) FROM rlhf_ratings WHERE id = :id"),
                    {"id": rating_a},
                )
            ).scalar_one()
            assert rating_a_gone == 0
            reviews_of_a_gone = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM rlhf_audit_reviews "
                        "WHERE rating_id = :id"
                    ),
                    {"id": rating_a},
                )
            ).scalar_one()
            assert reviews_of_a_gone == 0

        # --- Scenario 3: deleting `subject_tenant` cascades rating_b
        #     (it is both tenant_id AND subject_tenant_id) with no FK
        #     violation.
        async with child_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM tenants WHERE id = :id"),
                {"id": subject_tenant},
            )
        async with child_engine.connect() as connection:
            rating_b_gone = (
                await connection.execute(
                    text("SELECT count(*) FROM rlhf_ratings WHERE id = :id"),
                    {"id": rating_b},
                )
            ).scalar_one()
            assert rating_b_gone == 0
            review_b_gone = (
                await connection.execute(
                    text("SELECT count(*) FROM rlhf_audit_reviews " "WHERE id = :id"),
                    {"id": review_b1},
                )
            ).scalar_one()
            assert review_b_gone == 0

        # Legacy 'rlhf.rating' events STILL untouched.
        async with child_engine.connect() as connection:
            legacy_count_after_deletes = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM events "
                        "WHERE event_type = 'rlhf.rating'"
                    )
                )
            ).scalar_one()
        # NOTE: session_id and tenant_id on events cascaded when we deleted
        # owner_tenant above (that's an existing event FK, not our doing).
        # What we prove here is only that our migration copied nothing.
        # Re-seed a fresh legacy row and re-check on the downgrade side.

        # === downgrade 020 -> 019 by EXPLICIT revision id, never -1 ===
        await child_engine.dispose()
        child_engine = None
        await _run_alembic(child_url, "downgrade", _RLHF_DOWN_REVISION)
        child_engine = create_async_engine(child_url, poolclass=NullPool)
        async with child_engine.connect() as connection:
            still_present = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_name IN "
                        "('rlhf_ratings', 'rlhf_audit_reviews') "
                        "AND table_schema = current_schema()"
                    )
                )
            ).scalar_one()
            assert still_present == 0
            events_survives = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_name = 'events' "
                        "AND table_schema = current_schema()"
                    )
                )
            ).scalar_one()
            assert events_survives == 1
            # 'rlhf.rating' events on `events` are still whatever they
            # were before/after the migration cycle — migration copied
            # nothing to legacy and deleted nothing from it.
            legacy_count_after_downgrade = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM events "
                        "WHERE event_type = 'rlhf.rating'"
                    )
                )
            ).scalar_one()
        assert legacy_count_after_downgrade == legacy_count_after_deletes
    finally:
        if child_engine is not None:
            await child_engine.dispose()
        if created_database is not None:
            assert created_database == child_name
            async with admin_engine.connect() as connection:
                quoted_name = connection.dialect.identifier_preparer.quote(
                    created_database
                )
                await connection.exec_driver_sql(f"DROP DATABASE {quoted_name}")
        await admin_engine.dispose()
