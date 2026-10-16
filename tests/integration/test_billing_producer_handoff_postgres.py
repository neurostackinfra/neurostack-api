"""Disposable PostgreSQL proof for the producer migration and initial-pointer CAS."""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.models.billing_producer import (
    BillingProducerActivePair,
    BillingProducerReceipt,
)
from src.models.billing_producer_signed import (
    BillingProducerV1ActivePair,
    BillingProducerV1BuildAttestation,
    BillingProducerV1Grant,
    BillingProducerV1GrantConsumption,
    BillingProducerV1GrantRevocation,
    BillingProducerV1Receipt,
)
from src.models.tenant import Tenant
from src.models.user import User, UserRole
from src.repositories.billing_producer import BillingProducerReceiptRepository
from src.services.billing_producer_handoff import ProducerHandoffRejected

pytestmark = pytest.mark.integration
_ROOT = Path(__file__).resolve().parents[2]
_CHILD_PATTERN = re.compile(r"^neurostack_producer_smoke_[0-9a-f]{12}$")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True)
class _Harness:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    prior_revision: str
    signed_tables_before: set[str]


def _validated_admin_url(raw_url: str) -> URL:
    url = make_url(raw_url)
    if not url.drivername.startswith("postgresql"):
        raise ValueError("smoke URL must use PostgreSQL")
    if url.database != "postgres":
        raise ValueError("smoke URL must target the postgres administrative database")
    if (
        url.host is None
        or url.host.lower() not in _LOOPBACK_HOSTS
        or url.port is None
        or not 1 <= url.port <= 65535
        or not url.username
        or url.query
    ):
        raise ValueError(
            "smoke URL must use explicit loopback host, port, user, and no options"
        )
    return URL.create(
        drivername="postgresql+asyncpg",
        username=url.username,
        password=url.password,
        host=url.host,
        port=url.port,
        database="postgres",
    )


@pytest.mark.parametrize(
    "raw_url, expected_host",
    [
        ("postgresql://smoke:secret@127.0.0.1:55432/postgres", "127.0.0.1"),
        ("postgresql://smoke@localhost:55432/postgres", "localhost"),
        ("postgresql://smoke@[::1]:55432/postgres", "::1"),
    ],
)
def test_admin_url_accepts_only_explicit_loopback_postgres(raw_url, expected_host):
    parsed = _validated_admin_url(raw_url)

    assert parsed.drivername == "postgresql+asyncpg"
    assert parsed.host == expected_host
    assert parsed.port == 55432
    assert parsed.database == "postgres"
    assert parsed.username == "smoke"


@pytest.mark.parametrize(
    "raw_url",
    [
        "postgresql://smoke:secret@db.example.test:55432/postgres",
        "postgresql://smoke:secret@127.0.0.1/postgres",
        "postgresql://smoke:secret@127.0.0.1:0/postgres",
        "postgresql://smoke:secret@127.0.0.1:65536/postgres",
        "postgresql://smoke:secret@127.0.0.1:55432/neurostack",
        "postgresql://smoke:secret@127.0.0.1:55432/postgres?sslmode=require",
        "postgresql://smoke:secret@/postgres",
        "sqlite:///postgres",
    ],
)
def test_admin_url_rejects_non_loopback_or_ambiguous_targets(raw_url):
    with pytest.raises(ValueError):
        _validated_admin_url(raw_url)


async def _run_alembic(database_url: URL, *arguments: str) -> None:
    environment = os.environ.copy()
    # Alembic ConfigParser treats percent escapes in URLs as interpolation
    # syntax; double them for its set_main_option round-trip.
    environment["DATABASE_URL"] = database_url.render_as_string(
        hide_password=False
    ).replace("%", "%%")
    environment.pop("PYTHONPATH", None)
    runner = """
import sys
from alembic import command
from alembic.config import Config
sys.path.insert(0, sys.argv[3])
config = Config(sys.argv[1])
config.set_main_option("script_location", sys.argv[2])
getattr(command, sys.argv[4])(config, sys.argv[5])
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        runner,
        str(_ROOT / "alembic.ini"),
        str(_ROOT / "alembic"),
        str(_ROOT),
        *arguments,
        cwd=Path("/tmp"),
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    assert process.returncode == 0, output.decode(errors="replace")


async def _create_child(engine: AsyncEngine, name: str) -> None:
    assert _CHILD_PATTERN.fullmatch(name)
    async with engine.connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        identifier = connection.dialect.identifier_preparer.quote(name)
        await connection.exec_driver_sql(f"CREATE DATABASE {identifier}")


async def _drop_child(engine: AsyncEngine, name: str) -> None:
    assert _CHILD_PATTERN.fullmatch(name)
    async with engine.connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :database AND pid <> pg_backend_pid()",
            ),
            {"database": name},
        )
        identifier = connection.dialect.identifier_preparer.quote(name)
        await connection.exec_driver_sql(f"DROP DATABASE IF EXISTS {identifier}")


@pytest.fixture
async def postgres_harness() -> AsyncIterator[_Harness]:
    raw_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if not raw_url:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_url)
    application_url = make_url(
        os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    )
    if (admin_url.host, admin_url.port, "postgres") == (
        application_url.host,
        application_url.port,
        application_url.database,
    ):
        raise ValueError("smoke URL cannot target the application database")

    admin_engine = create_async_engine(admin_url, poolclass=NullPool)
    async with admin_engine.connect() as connection:
        target = (
            await connection.exec_driver_sql(
                "SELECT current_user AS connected_user, "
                "host(inet_server_addr()) AS server_address, "
                "inet_server_port() AS server_port"
            )
        ).one()
        assert target.connected_user == admin_url.username
        assert target.server_address is not None
        # Docker Desktop publishes a host loopback port to the container's
        # private bridge address and internal PostgreSQL port. The URL above
        # remains restricted to a loopback host and explicit port; verify the
        # server endpoint itself is private rather than public.
        server_address = ipaddress.ip_address(target.server_address)
        assert server_address.is_loopback or server_address.is_private
        assert 1 <= target.server_port <= 65535
    child_name = f"neurostack_producer_smoke_{uuid4().hex[:12]}"
    await _create_child(admin_engine, child_name)
    child_url = admin_url.set(database=child_name)
    child_engine = create_async_engine(child_url, poolclass=NullPool)
    try:
        await _run_alembic(child_url, "upgrade", "033")
        async with child_engine.connect() as connection:
            prior_revision = await connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            signed_tables_before = set(
                (
                    await connection.exec_driver_sql(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname = current_schema() "
                        "AND tablename LIKE 'billing_producer_v1_%'"
                    )
                ).scalars()
            )
        await _run_alembic(child_url, "upgrade", "034")
        yield _Harness(
            engine=child_engine,
            sessions=async_sessionmaker(child_engine, expire_on_commit=False),
            prior_revision=prior_revision,
            signed_tables_before=signed_tables_before,
        )
    finally:
        await child_engine.dispose()
        await _drop_child(admin_engine, child_name)
        await admin_engine.dispose()


async def test_producer_migration_installs_executable_plpgsql(
    postgres_harness: _Harness,
) -> None:
    assert postgres_harness.prior_revision == "033"
    assert postgres_harness.signed_tables_before == set()
    async with postgres_harness.engine.connect() as connection:
        functions = set(
            (
                await connection.exec_driver_sql(
                    "SELECT proname FROM pg_proc "
                    "WHERE proname IN ('reject_billing_producer_receipt_mutation', "
                    "'validate_billing_producer_grant_state', "
                    "'validate_billing_producer_active_pair', "
                    "'reject_billing_producer_v1_mutation', "
                    "'validate_billing_producer_v1_grant_event', "
                    "'validate_billing_producer_v1_active_pair')"
                )
            ).scalars()
        )
    assert functions == {
        "reject_billing_producer_receipt_mutation",
        "validate_billing_producer_grant_state",
        "validate_billing_producer_active_pair",
        "reject_billing_producer_v1_mutation",
        "validate_billing_producer_v1_grant_event",
        "validate_billing_producer_v1_active_pair",
    }


@pytest.mark.asyncio
async def test_postgres_v1_artifacts_are_immutable_and_events_are_single_use(
    postgres_harness: _Harness,
) -> None:
    now = datetime.now(timezone.utc)
    operation_id = uuid4()
    tenant_id = uuid4()
    issuer_id, executor_id, approver_id, registrar_id, verifier_id = [
        uuid4() for _ in range(5)
    ]
    assignment_id, authorization_id = uuid4(), uuid4()
    attestation_id, deploy_id, verification_id = [uuid4() for _ in range(3)]
    assignment_digest, authorization_digest = "a" * 64, "b" * 64
    attestation_digest, deploy_digest, verification_digest = (
        "c" * 64,
        "d" * 64,
        "e" * 64,
    )
    payload_digest = "f" * 64
    scope = {"operation_id": str(operation_id)}
    candidate_pairs: list[tuple[UUID, str, UUID, str]] = []

    async with postgres_harness.sessions() as session, session.begin():
        session.add(
            Tenant(
                id=tenant_id,
                name=f"producer-v1-smoke-{uuid4().hex[:12]}",
                slug=f"producer-v1-smoke-{uuid4().hex[:12]}",
                api_key_hash=f"producer-v1-smoke-{uuid4().hex}",
            )
        )
        for user_id in (issuer_id, executor_id, approver_id, registrar_id, verifier_id):
            session.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    email=f"{user_id}@producer-v1-smoke.invalid",
                    password_hash="not-used-in-smoke",
                    role=UserRole.super_admin,
                )
            )
        await session.flush()

        assignment = BillingProducerV1Grant(
            id=assignment_id,
            grant_kind="assignment",
            envelope={"id": str(assignment_id)},
            envelope_digest=assignment_digest,
            payload_digest=payload_digest,
            issuer_user_id=issuer_id,
            executor_user_id=executor_id,
            operation_id=operation_id,
            environment="staging",
            operational_role="production-serving",
            cluster_arn="arn:aws:ecs:us-east-1:388460142431:cluster/neurostack-staging",
            service_arn="arn:aws:ecs:us-east-1:388460142431:service/neurostack-staging/neurostack-staging",
            action="create_billing_producer_deploy_receipt",
            receipt_id=deploy_id,
            verification_receipt_id=verification_id,
            scope=scope,
            scope_digest=payload_digest,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        authorization = BillingProducerV1Grant(
            id=authorization_id,
            grant_kind="authorization",
            envelope={"id": str(authorization_id)},
            envelope_digest=authorization_digest,
            payload_digest=payload_digest,
            issuer_user_id=issuer_id,
            executor_user_id=executor_id,
            approver_user_id=approver_id,
            assignment_id=assignment_id,
            assignment_digest=assignment_digest,
            operation_id=operation_id,
            environment="staging",
            operational_role="production-serving",
            cluster_arn=assignment.cluster_arn,
            service_arn=assignment.service_arn,
            action=assignment.action,
            receipt_id=deploy_id,
            verification_receipt_id=verification_id,
            scope=scope,
            scope_digest=payload_digest,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        attestation = BillingProducerV1BuildAttestation(
            id=attestation_id,
            operation_id=operation_id,
            envelope={"id": str(attestation_id)},
            envelope_digest=attestation_digest,
            payload_digest=payload_digest,
            registered_by_user_id=registrar_id,
            registered_at=now,
        )

        def receipt(
            *, receipt_id: UUID, receipt_digest: str, receipt_type: str
        ) -> BillingProducerV1Receipt:
            return BillingProducerV1Receipt(
                id=receipt_id,
                operation_id=operation_id,
                receipt_type=receipt_type,
                environment="staging",
                operational_role="production-serving",
                deployment_id="ecs-deployment-smoke",
                task_set_id="ecs-task-set-smoke",
                config_generation="config-smoke",
                compatibility_generation="compat-smoke",
                app_digest="sha256:" + "1" * 64,
                bundle_digest="2" * 64,
                readback_digest="3" * 64,
                build_attestation_id=attestation_id,
                build_attestation_digest=attestation_digest,
                executor_user_id=executor_id,
                verifier_user_id=(
                    verifier_id if receipt_type.endswith("verified.v1") else None
                ),
                envelope={"id": str(receipt_id)},
                envelope_digest=receipt_digest,
                payload_digest=payload_digest,
                issued_at=now,
                expires_at=now + timedelta(days=1),
            )

        deploy = receipt(
            receipt_id=deploy_id,
            receipt_digest=deploy_digest,
            receipt_type="billing_producer_deployed.v1",
        )
        verified = receipt(
            receipt_id=verification_id,
            receipt_digest=verification_digest,
            receipt_type="billing_producer_deploy_verified.v1",
        )
        session.add_all([assignment, authorization, attestation, deploy, verified])
        await session.flush()
        for _ in range(2):
            candidate_deploy_id, candidate_verify_id = uuid4(), uuid4()
            candidate_deploy_digest, candidate_verify_digest = (
                uuid4().hex + uuid4().hex,
                uuid4().hex + uuid4().hex,
            )
            candidate_deploy = receipt(
                receipt_id=candidate_deploy_id,
                receipt_digest=candidate_deploy_digest,
                receipt_type="billing_producer_deployed.v1",
            )
            candidate_verify = receipt(
                receipt_id=candidate_verify_id,
                receipt_digest=candidate_verify_digest,
                receipt_type="billing_producer_deploy_verified.v1",
            )
            session.add_all([candidate_deploy, candidate_verify])
            candidate_pairs.append(
                (
                    candidate_deploy_id,
                    candidate_deploy_digest,
                    candidate_verify_id,
                    candidate_verify_digest,
                )
            )
        await session.flush()
        session.add(
            BillingProducerV1ActivePair(
                environment="staging",
                operation_id=operation_id,
                receipt_id=deploy_id,
                receipt_digest=deploy_digest,
                verification_receipt_id=verification_id,
                verification_receipt_digest=verification_digest,
                revision=1,
            )
        )
        session.add(
            BillingProducerV1GrantConsumption(
                id=uuid4(),
                grant_id=authorization_id,
                grant_digest=authorization_digest,
                operation_id=operation_id,
                request_id=uuid4(),
                executor_user_id=executor_id,
                result_digest=payload_digest,
                consumed_at=now + timedelta(seconds=1),
            )
        )
        session.add(
            BillingProducerV1GrantRevocation(
                id=uuid4(),
                grant_id=assignment_id,
                grant_digest=assignment_digest,
                operation_id=operation_id,
                actor_user_id=approver_id,
                reason_code="operator_rehearsal",
                envelope={"id": str(uuid4())},
                envelope_digest="9" * 64,
                payload_digest=payload_digest,
                revoked_at=now + timedelta(seconds=2),
            )
        )
        await session.flush()

    # Every signed artifact and event table rejects UPDATE and DELETE.
    immutable_rows = (
        ("billing_producer_v1_grants", assignment_id),
        ("billing_producer_v1_build_attestations", attestation_id),
        ("billing_producer_v1_receipts", deploy_id),
        ("billing_producer_v1_grant_consumptions", None),
        ("billing_producer_v1_grant_revocations", None),
    )
    for table, row_id in immutable_rows:
        where = "id = :row_id" if row_id is not None else "operation_id = :operation_id"
        params = {"row_id": row_id, "operation_id": operation_id}
        for verb in ("UPDATE", "DELETE"):
            async with postgres_harness.sessions() as session:
                with pytest.raises(
                    DBAPIError,
                    match="canonical billing producer artifacts are append-only",
                ):
                    async with session.begin():
                        if verb == "UPDATE":
                            await session.execute(
                                text(
                                    f"UPDATE {table} SET operation_id = operation_id WHERE {where}"
                                ),
                                params,
                            )
                        else:
                            await session.execute(
                                text(f"DELETE FROM {table} WHERE {where}"), params
                            )

    # The event table's unique grant key makes a concurrent replay single-winner.
    replay_grant_id = uuid4()
    async with postgres_harness.sessions() as session, session.begin():
        session.add(
            BillingProducerV1Grant(
                id=replay_grant_id,
                grant_kind="assignment",
                envelope={"id": str(replay_grant_id)},
                envelope_digest="8" * 64,
                payload_digest=payload_digest,
                issuer_user_id=issuer_id,
                executor_user_id=executor_id,
                operation_id=operation_id,
                environment="staging",
                operational_role="production-serving",
                cluster_arn="arn:aws:ecs:us-east-1:388460142431:cluster/neurostack-staging",
                service_arn="arn:aws:ecs:us-east-1:388460142431:service/neurostack-staging/neurostack-staging",
                action="create_billing_producer_deploy_receipt",
                receipt_id=uuid4(),
                verification_receipt_id=uuid4(),
                scope=scope,
                scope_digest=payload_digest,
                issued_at=now,
                expires_at=now + timedelta(minutes=5),
            )
        )

    start = asyncio.Event()

    async def consume_replay() -> str:
        async with postgres_harness.sessions() as session:
            await start.wait()
            try:
                async with session.begin():
                    session.add(
                        BillingProducerV1GrantConsumption(
                            id=uuid4(),
                            grant_id=replay_grant_id,
                            grant_digest="8" * 64,
                            operation_id=operation_id,
                            request_id=uuid4(),
                            executor_user_id=executor_id,
                            result_digest=payload_digest,
                            consumed_at=now + timedelta(seconds=3),
                        )
                    )
                    await session.flush()
                return "winner"
            except IntegrityError:
                await session.rollback()
                return "replay-conflict"

    replays = [asyncio.create_task(consume_replay()) for _ in range(2)]
    await asyncio.sleep(0)
    start.set()
    assert sorted(await asyncio.gather(*replays)) == ["replay-conflict", "winner"]

    cas_start = asyncio.Event()
    both_saw_initial = asyncio.Event()
    readers = 0
    readers_lock = asyncio.Lock()

    async def activate_candidate(
        pair: tuple[UUID, str, UUID, str],
    ) -> str:
        nonlocal readers
        async with postgres_harness.sessions() as session:
            await cas_start.wait()
            async with session.begin():
                pointer = await session.get(
                    BillingProducerV1ActivePair,
                    "staging",
                    with_for_update=False,
                )
                assert pointer is not None and pointer.revision == 1
                async with readers_lock:
                    readers += 1
                    if readers == 2:
                        both_saw_initial.set()
                await both_saw_initial.wait()
                result = await session.execute(
                    text(
                        "UPDATE billing_producer_v1_active_pairs "
                        "SET receipt_id = :receipt_id, receipt_digest = :receipt_digest, "
                        "verification_receipt_id = :verification_id, "
                        "verification_receipt_digest = :verification_digest, "
                        "revision = revision + 1, updated_at = clock_timestamp() "
                        "WHERE environment = 'staging' AND revision = 1 "
                        "AND receipt_id = :expected_receipt_id "
                        "AND receipt_digest = :expected_receipt_digest "
                        "AND verification_receipt_id = :expected_verification_id "
                        "AND verification_receipt_digest = :expected_verification_digest"
                    ),
                    {
                        "receipt_id": pair[0],
                        "receipt_digest": pair[1],
                        "verification_id": pair[2],
                        "verification_digest": pair[3],
                        "expected_receipt_id": deploy_id,
                        "expected_receipt_digest": deploy_digest,
                        "expected_verification_id": verification_id,
                        "expected_verification_digest": verification_digest,
                    },
                )
                return "winner" if result.rowcount == 1 else "cas-conflict"

    cas_jobs = [
        asyncio.create_task(activate_candidate(pair)) for pair in candidate_pairs
    ]
    await asyncio.sleep(0)
    cas_start.set()
    cas_outcomes = await asyncio.gather(*cas_jobs)
    assert sorted(cas_outcomes) == ["cas-conflict", "winner"]
    async with postgres_harness.sessions() as session:
        pointer = await session.get(BillingProducerV1ActivePair, "staging")
    assert pointer is not None and pointer.revision == 2
    assert (pointer.receipt_id, pointer.verification_receipt_id) in {
        (pair[0], pair[2]) for pair in candidate_pairs
    }


@pytest.mark.asyncio
async def test_postgres_initial_pair_cas_has_only_one_concurrent_winner(
    postgres_harness: _Harness,
) -> None:
    # Reuse the signed fixture builder and service path from the focused API proof.
    from src.services.billing_producer_handoff import (
        CREATE_ACTION,
        VERIFY_ACTION,
        digest_json,
        receipt_scope,
    )
    from tests.api.test_billing_operations import (
        _add_grant_pair,
        _handoff_setup,
        _verification_scope,
    )

    now = datetime.now(timezone.utc)
    fixture = _handoff_setup(now)
    repository = fixture["repository"]
    service = fixture["service"]
    operation_id = fixture["operation_id"]
    receipt_id = fixture["receipt_id"]
    verification_id = fixture["verification_receipt_id"]
    assignment, authorization = [
        grant
        for grant in repository.grants.values()
        if grant.action == "create_billing_producer_deploy_receipt"
    ]
    deploy = await service.create(
        actor_user_id=fixture["actor_id"],
        operation_id=operation_id,
        receipt_id=receipt_id,
        verification_receipt_id=verification_id,
        assignment_id=assignment.id,
        authorization_id=authorization.id,
        readback=fixture["readback"],
        build_attestation_id=fixture["build_id"],
        build_attestation_digest=fixture["attestation_digest"],
        commit_sha=fixture["commit_sha"],
        tree_sha=fixture["tree_sha"],
        workflow_id=fixture["workflow_id"],
        expected_current=None,
        now=now,
    )
    verifier_id = uuid4()
    verify_scope = _verification_scope(
        operation_id=operation_id,
        deploy=deploy,
        verification_receipt_id=verification_id,
        readback=fixture["readback"],
    )
    grants = _add_grant_pair(
        repository,
        fixture["private_key"],
        actor_id=verifier_id,
        operation_id=operation_id,
        action="verify_billing_producer_deploy_receipt",
        scope_digest=__import__(
            "src.services.billing_producer_handoff", fromlist=["digest_json"]
        ).digest_json(verify_scope),
        receipt_id=receipt_id,
        verification_receipt_id=verification_id,
        issued_at=now,
    )
    verified = await service.verify(
        actor_user_id=verifier_id,
        operation_id=operation_id,
        receipt_id=receipt_id,
        verification_receipt_id=verification_id,
        assignment_id=grants[0].id,
        authorization_id=grants[1].id,
        readback=fixture["readback"],
        expected_current=None,
        now=now + timedelta(seconds=1),
    )
    initial_pointer = repository.pointer
    assert initial_pointer is not None
    initial_pointer_snapshot = copy.deepcopy(initial_pointer)
    prior_pair = {
        "receipt_id": str(initial_pointer.receipt_id),
        "receipt_digest": initial_pointer.receipt_digest,
        "verification_receipt_id": str(initial_pointer.verification_receipt_id),
        "verification_receipt_digest": initial_pointer.verification_receipt_digest,
    }
    assert initial_pointer.operation_id == operation_id
    refresh_candidates = []
    refresh_time = now + timedelta(days=7, seconds=-200)
    for index in range(2):
        assert repository.pointer is not None
        assert {
            "receipt_id": str(repository.pointer.receipt_id),
            "receipt_digest": repository.pointer.receipt_digest,
            "verification_receipt_id": str(repository.pointer.verification_receipt_id),
            "verification_receipt_digest": repository.pointer.verification_receipt_digest,
        } == prior_pair
        assert service.repository is repository
        assert {
            "receipt_id": str(repository.pointer.receipt_id),
            "receipt_digest": repository.pointer.receipt_digest,
            "verification_receipt_id": str(repository.pointer.verification_receipt_id),
            "verification_receipt_digest": repository.pointer.verification_receipt_digest,
        } == prior_pair
        candidate_receipt_id, candidate_verification_id = uuid4(), uuid4()
        creator_id, refresh_verifier_id = uuid4(), uuid4()
        payload_scope = receipt_scope(
            operation_id=operation_id,
            receipt_id=candidate_receipt_id,
            verification_receipt_id=candidate_verification_id,
            readback=fixture["readback"],
            build_attestation_id=fixture["build_id"],
            build_attestation_digest=fixture["attestation_digest"],
            commit_sha=fixture["commit_sha"],
            tree_sha=fixture["tree_sha"],
            workflow_id=fixture["workflow_id"],
            supersedes=prior_pair,
        )
        create_grants = _add_grant_pair(
            repository,
            fixture["private_key"],
            actor_id=creator_id,
            operation_id=operation_id,
            action=CREATE_ACTION,
            scope_digest=digest_json(payload_scope),
            receipt_id=candidate_receipt_id,
            verification_receipt_id=candidate_verification_id,
            issued_at=refresh_time - timedelta(seconds=10),
        )
        candidate_deploy = await service.create(
            actor_user_id=creator_id,
            operation_id=operation_id,
            receipt_id=candidate_receipt_id,
            verification_receipt_id=candidate_verification_id,
            assignment_id=create_grants[0].id,
            authorization_id=create_grants[1].id,
            readback=fixture["readback"],
            build_attestation_id=fixture["build_id"],
            build_attestation_digest=fixture["attestation_digest"],
            commit_sha=fixture["commit_sha"],
            tree_sha=fixture["tree_sha"],
            workflow_id=fixture["workflow_id"],
            expected_current=prior_pair,
            now=refresh_time,
        )
        verify_scope = _verification_scope(
            operation_id=operation_id,
            deploy=candidate_deploy,
            verification_receipt_id=candidate_verification_id,
            readback=fixture["readback"],
            supersedes=prior_pair,
        )
        verify_grants = _add_grant_pair(
            repository,
            fixture["private_key"],
            actor_id=refresh_verifier_id,
            operation_id=operation_id,
            action=VERIFY_ACTION,
            scope_digest=digest_json(verify_scope),
            receipt_id=candidate_receipt_id,
            verification_receipt_id=candidate_verification_id,
            issued_at=refresh_time,
        )
        candidate_verified = await service.verify(
            actor_user_id=refresh_verifier_id,
            operation_id=operation_id,
            receipt_id=candidate_receipt_id,
            verification_receipt_id=candidate_verification_id,
            assignment_id=verify_grants[0].id,
            authorization_id=verify_grants[1].id,
            readback=fixture["readback"],
            expected_current=prior_pair,
            now=refresh_time + timedelta(seconds=1),
        )
        assert candidate_deploy.payload["supersedes"] == prior_pair
        assert candidate_verified.payload["supersedes"] == prior_pair
        refresh_candidates.append((candidate_deploy, candidate_verified))
        repository.pointer = copy.deepcopy(initial_pointer_snapshot)
    attestation = fixture["repository"].attestation
    async with postgres_harness.sessions() as session, session.begin():
        tenant_id = uuid4()
        session.add(
            Tenant(
                id=tenant_id,
                name=f"producer-smoke-{uuid4().hex[:12]}",
                slug=f"producer-smoke-{uuid4().hex[:12]}",
                api_key_hash=f"producer-smoke-{uuid4().hex}",
            )
        )
        await session.flush()
        actors = {fixture["actor_id"], verifier_id}
        for receipt in repository.receipts.values():
            actors.add(receipt.attestor_user_id)
            if receipt.verifier_user_id is not None:
                actors.add(receipt.verifier_user_id)
        for actor in actors:
            session.add(
                User(
                    id=actor,
                    tenant_id=tenant_id,
                    email=f"{actor}@producer-smoke.invalid",
                    password_hash="not-used-in-smoke",
                    role=UserRole.super_admin,
                )
            )
        await session.flush()
        session.add(attestation)
        session.add_all(repository.receipts.values())

    start = asyncio.Event()
    ready = 0
    ready_lock = asyncio.Lock()
    both_selected_empty = asyncio.Event()

    class RacingRepository(BillingProducerReceiptRepository):
        async def get_active_pair(
            self, *, environment: str = "staging", for_update: bool = False
        ) -> BillingProducerActivePair | None:
            nonlocal ready
            pair = await super().get_active_pair(
                environment=environment, for_update=for_update
            )
            if for_update:
                async with ready_lock:
                    ready += 1
                    if ready == 2:
                        both_selected_empty.set()
                await both_selected_empty.wait()
            return pair

    async def publish() -> str:
        async with postgres_harness.sessions() as session:
            repo = RacingRepository(session)
            await start.wait()
            try:
                async with session.begin():
                    await repo.activate_verified_pair(
                        operation_id=operation_id,
                        deploy_receipt=deploy,
                        verification_receipt=verified,
                        expected_current=None,
                        deploy_public_key=fixture["public_key"],
                    )
                return "winner"
            except (ProducerHandoffRejected, IntegrityError):
                await session.rollback()
                return "conflict"

    jobs = [asyncio.create_task(publish()) for _ in range(2)]
    await asyncio.sleep(0)
    start.set()
    outcomes = await asyncio.gather(*jobs)
    assert sorted(outcomes) == ["conflict", "winner"]
    async with postgres_harness.sessions() as session:
        pointers = list(
            (await session.execute(select(BillingProducerActivePair))).scalars()
        )
    assert len(pointers) == 1
    assert pointers[0].receipt_id == receipt_id
    assert pointers[0].verification_receipt_id == verification_id

    published_prior = {
        "receipt_id": str(pointers[0].receipt_id),
        "receipt_digest": pointers[0].receipt_digest,
        "verification_receipt_id": str(pointers[0].verification_receipt_id),
        "verification_receipt_digest": pointers[0].verification_receipt_digest,
    }
    assert published_prior == prior_pair
    refresh_ready = 0
    both_observed_prior = asyncio.Event()
    refresh_lock = asyncio.Lock()

    async def refresh(
        candidate: tuple[BillingProducerReceipt, BillingProducerReceipt],
    ) -> tuple[str, tuple[BillingProducerReceipt, BillingProducerReceipt]]:
        candidate_deploy, candidate_verified = candidate
        async with postgres_harness.sessions() as session:
            repo = BillingProducerReceiptRepository(session)
            observed = await repo.get_active_pair()
            assert observed is not None
            assert {
                "receipt_id": str(observed.receipt_id),
                "receipt_digest": observed.receipt_digest,
                "verification_receipt_id": str(observed.verification_receipt_id),
                "verification_receipt_digest": observed.verification_receipt_digest,
            } == published_prior
            await session.commit()
            nonlocal refresh_ready
            async with refresh_lock:
                refresh_ready += 1
                if refresh_ready == 2:
                    both_observed_prior.set()
            await both_observed_prior.wait()
            try:
                async with session.begin():
                    await repo.activate_verified_pair(
                        operation_id=operation_id,
                        deploy_receipt=candidate_deploy,
                        verification_receipt=candidate_verified,
                        expected_current=published_prior,
                        deploy_public_key=fixture["public_key"],
                    )
                return "winner", candidate
            except (ProducerHandoffRejected, IntegrityError):
                await session.rollback()
                return "conflict", candidate

    refresh_outcomes = await asyncio.gather(
        *(refresh(candidate) for candidate in refresh_candidates)
    )
    assert sorted(outcome for outcome, _ in refresh_outcomes) == [
        "conflict",
        "winner",
    ]
    winning_deploy, winning_verified = next(
        candidate for outcome, candidate in refresh_outcomes if outcome == "winner"
    )
    async with postgres_harness.sessions() as session:
        pointer = await session.get(BillingProducerActivePair, "staging")
    assert pointer is not None
    assert pointer.receipt_id == winning_deploy.id
    assert pointer.verification_receipt_id == winning_verified.id
    assert pointer.revision == 2
