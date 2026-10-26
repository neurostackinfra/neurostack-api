"""Credit dominance oracle on real PostgreSQL (neurostack-credit-system 1b).

Item 5 (ii) of Group 1b.  The real application (``src.api.main.app``, its
settings read from the environment, its lifespan, middleware and pool) runs
against disposable PostgreSQL databases at ``037`` and at ``038``, with
``CREDIT_HOT_PATH_ENABLED`` false and true.  Each run replays the fixed corpus
``tests/fixtures/credit_dominance/requests.json`` through every ingress (JWT,
API key, Z-API, Meta, Uazapi, Evolution; the LLM and the providers are faked)
in a fresh interpreter, so no in-process cache, rate-limit bucket or replay
store crosses runs, and every run starts from a clone of one seeded template.

* With the config on, status and body equal the config-off run at the same
  revision once the server-generated fields are masked (the corpus' named
  fields plus any UUID the world did not seed and any instant), and the
  stored events are equal by content.  The two config-off runs at ``037`` and
  ``038`` are equal too: the credit schema alone changes nothing.
* The verdict: config off => off; config on at ``037`` => off
  (``schema_below_minimum``); config on at ``038`` => on.
* A ``before_cursor_execute`` listener, attached only after start-up, sees no
  SQL touching ``credit_*`` while the switch is not effective (nor, in Group
  1b, while it is: nothing writes yet); no ``credit_*`` row exists after a
  run at ``038``.
* In one process, the background refresh turns the switch on after
  ``upgrade 037 -> 038`` and off again after the downgrade, each within one
  interval, while requests keep answering.

Groups 3-7 append their surfaces to the corpus and to these runs, always
also against a ``037`` database.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[2]
_CORPUS_PATH = _ROOT / "tests" / "fixtures" / "credit_dominance" / "requests.json"
_INGRESSES = {"jwt", "api_key", "zapi", "meta", "uazapi", "evolution"}
_LLM_REPLY = "Olá! Posso ajudar com o seu agendamento."
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# Generated instants: ISO 8601, and the clinic's local clock that the system
# prompt carries ("Data e hora atual: dd/mm/yyyy HH:MM", "Agora são HH:MM").
_INSTANT = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
    r"|\d{2}/\d{2}/\d{4}(?: \d{2}:\d{2}(?::\d{2})?)?"
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\b"
)
# The greeting the prompt prescribes follows the same clock (pt-BR and English
# presets): a pair of runs across 12:00 or 18:00 must not diverge on it.
_GREETING = re.compile(
    r"\b(?:Bom dia|Boa tarde|Boa noite|Good morning|Good afternoon|Good evening)\b",
    re.IGNORECASE,
)
_CREDIT_SQL = re.compile(r"\bcredit_[a-z_]+", re.IGNORECASE)
_RUN_TIMEOUT_SECONDS = 300


def _corpus() -> dict[str, Any]:
    return json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The seeded world (generated per test module, never committed).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _World:
    builder: str
    api_tenant: str
    api_key: str
    unknown_api_key: str
    api_session: str
    jwt_tenant: str
    jwt_subject: str
    jwt_session: str
    meta_tenant: str
    meta_phone: str
    meta_secret: str
    wrong_meta_secret: str
    zapi_tenant: str
    zapi_token: str
    unknown_zapi_token: str
    zapi_instance: str
    uazapi_tenant: str
    uazapi_instance: str
    evolution_tenant: str
    evolution_instance: str

    @classmethod
    def generate(cls) -> "_World":
        def token() -> str:
            import base64

            raw = secrets.token_bytes(32)
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

        return cls(
            builder=str(uuid4()),
            api_tenant=str(uuid4()),
            api_key=secrets.token_urlsafe(24),
            unknown_api_key=secrets.token_urlsafe(24),
            api_session=str(uuid4()),
            jwt_tenant=str(uuid4()),
            jwt_subject=str(uuid4()),
            jwt_session=str(uuid4()),
            meta_tenant=str(uuid4()),
            meta_phone=f"credit-dominance-{uuid4().hex[:8]}",
            meta_secret=secrets.token_urlsafe(32),
            wrong_meta_secret=secrets.token_urlsafe(32),
            zapi_tenant=str(uuid4()),
            zapi_token=token(),
            unknown_zapi_token=token(),
            zapi_instance=f"3E{uuid4().hex[:10]}",
            uazapi_tenant=str(uuid4()),
            uazapi_instance=f"credit-uazapi-{uuid4().hex[:8]}",
            evolution_tenant=str(uuid4()),
            evolution_instance=f"credit-evolution-{uuid4().hex[:8]}",
        )

    def known_ids(self) -> set[str]:
        return {
            value.lower()
            for value in asdict(self).values()
            if _UUID.fullmatch(value) is not None
        }


async def _seed_world(url: Any, world: _World) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import NullPool

    from src.api.middleware.auth import pwd_context
    from src.models.builder import Builder
    from src.models.clinic_config_table import ClinicConfigTable
    from src.models.session import Session
    from src.models.tenant import Tenant

    def tenant(tenant_id: str, builder: str | None, **extra: Any) -> Tenant:
        return Tenant(
            id=UUID(tenant_id),
            name=f"Credit dominance {tenant_id[:8]}",
            slug=f"credit-dominance-{tenant_id}",
            api_key_hash=extra.pop("api_key_hash", f"unused-{tenant_id}"),
            builder_id=None if builder is None else UUID(builder),
            config={},
            **extra,
        )

    def session(session_id: str, tenant_id: str) -> Session:
        return Session(
            id=UUID(session_id),
            tenant_id=UUID(tenant_id),
            channel="test",
            cognitive_state={},
            session_metadata={},
        )

    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with AsyncSession(engine) as database, database.begin():
            database.add(
                Builder(id=UUID(world.builder), name="Credit dominance builder")
            )
            await database.flush()
            database.add_all(
                [
                    tenant(
                        world.api_tenant,
                        world.builder,
                        api_key_hash=pwd_context.hash(world.api_key),
                    ),
                    tenant(world.jwt_tenant, None),
                    tenant(world.meta_tenant, world.builder),
                    tenant(world.zapi_tenant, world.builder),
                    tenant(world.uazapi_tenant, None),
                    tenant(world.evolution_tenant, world.builder),
                ]
            )
            await database.flush()
            database.add_all(
                [
                    session(world.api_session, world.api_tenant),
                    session(world.jwt_session, world.jwt_tenant),
                    ClinicConfigTable(
                        tenant_id=UUID(world.meta_tenant),
                        api_credentials={
                            "whatsapp_provider": "meta",
                            "meta_phone_number_id": world.meta_phone,
                            "meta_app_secret": world.meta_secret,
                            "meta_permanent_token": secrets.token_urlsafe(24),
                        },
                    ),
                    ClinicConfigTable(
                        tenant_id=UUID(world.zapi_tenant),
                        api_credentials={
                            "whatsapp_provider": "zapi",
                            "zapi_instance_id": world.zapi_instance,
                            "zapi_token": secrets.token_urlsafe(24),
                            "zapi_inbound_token_digest": hashlib.sha256(
                                world.zapi_token.encode("ascii")
                            ).hexdigest(),
                        },
                    ),
                    ClinicConfigTable(
                        tenant_id=UUID(world.uazapi_tenant),
                        api_credentials={
                            "whatsapp_provider": "uazapi",
                            "uazapi_instance_name": world.uazapi_instance,
                            "uazapi_api_url": "https://uazapi.invalid",
                            "uazapi_token": secrets.token_urlsafe(24),
                        },
                    ),
                    ClinicConfigTable(
                        tenant_id=UUID(world.evolution_tenant),
                        api_credentials={
                            "whatsapp_provider": "evolution",
                            "evolution_instance_name": world.evolution_instance,
                            "evolution_api_url": "https://evolution.invalid",
                            "evolution_api_key": secrets.token_urlsafe(24),
                        },
                    ),
                ]
            )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Masking: server-generated values never decide equality.
# ---------------------------------------------------------------------------


def _mask(value: Any, masked_keys: frozenset[str], known_ids: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "<masked>"
                if key in masked_keys
                else _mask(item, masked_keys, known_ids)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask(item, masked_keys, known_ids) for item in value]
    if isinstance(value, str):

        def uuid(match: re.Match[str]) -> str:
            text = match.group(0)
            return text if text.lower() in known_ids else "<generated-uuid>"

        masked = _INSTANT.sub("<instant>", _UUID.sub(uuid, value))
        return _GREETING.sub("<greeting>", masked)
    return value


def _comparable(run: dict[str, Any], world: _World) -> dict[str, Any]:
    masked = frozenset(_corpus()["masked_fields"])
    known = world.known_ids()
    return {
        "responses": [
            {
                "id": response["id"],
                "status": response["status"],
                "body": _mask(response["body"], masked, known),
            }
            for response in run["responses"]
        ],
        "events": sorted(
            json.dumps(_mask(row, masked, known), sort_keys=True)
            for row in run["events"]
        ),
    }


# ---------------------------------------------------------------------------
# The replay, in a fresh interpreter per run.
# ---------------------------------------------------------------------------


@contextmanager
def _fake_llm_and_providers() -> Iterator[None]:
    """Deterministic LLM and provider sends; nothing leaves the process."""

    from unittest.mock import AsyncMock, MagicMock, patch

    class _FakeLLM:
        async def generate(
            self,
            messages: list[dict[str, str]],
            model: str | None = None,
            temperature: float | None = None,
            max_tokens: int | None = None,
        ) -> dict[str, Any]:
            return {
                "content": _LLM_REPLY,
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 8,
                    "total_tokens": 20,
                },
                "model": "credit-dominance-fake",
            }

    llm = _FakeLLM()
    webhook = "src.api.routes.webhook"
    fakes: dict[str, Any] = {
        "src.llm.client.get_llm_client": MagicMock(return_value=llm),
        "src.llm.client.get_tenant_llm_client": MagicMock(return_value=llm),
        "src.llm.client.get_multimodal_text_llm_client": MagicMock(return_value=None),
        "src.api.routes.rlhf.get_tenant_llm_client": MagicMock(return_value=llm),
        f"{webhook}.send_text_message": AsyncMock(
            return_value={"messageId": "credit-dominance-zapi-reply"}
        ),
        f"{webhook}.send_zapi_audio": AsyncMock(
            return_value={"messageId": "credit-dominance-zapi-audio"}
        ),
        f"{webhook}.send_evolution_text": AsyncMock(
            return_value={
                "key": {
                    "remoteJid": "5511900000401@s.whatsapp.net",
                    "fromMe": True,
                    "id": "credit-dominance-evolution-reply",
                }
            }
        ),
        f"{webhook}.send_evolution_audio": AsyncMock(
            return_value={"key": {"id": "credit-dominance-evolution-audio"}}
        ),
        f"{webhook}.send_uazapi_text": AsyncMock(
            return_value={
                "Details": "Sent",
                "Timestamp": "2026-10-26T00:00:00Z",
                "MessageID": "credit-dominance-uazapi-reply",
            }
        ),
        f"{webhook}.send_uazapi_audio": AsyncMock(
            return_value={"MessageID": "credit-dominance-uazapi-audio"}
        ),
        f"{webhook}.send_meta_cloud_text": AsyncMock(
            return_value={
                "messaging_product": "whatsapp",
                "messages": [{"id": "wamid.credit-dominance-reply"}],
            }
        ),
        f"{webhook}.send_meta_cloud_audio": AsyncMock(
            return_value={"messages": [{"id": "wamid.credit-dominance-audio"}]}
        ),
        f"{webhook}.mark_message_read": AsyncMock(return_value=None),
        f"{webhook}.mark_conversation_unread": AsyncMock(return_value=None),
        f"{webhook}.synthesize_speech": AsyncMock(
            side_effect=RuntimeError("speech synthesis is off in the oracle")
        ),
    }
    with ExitStack() as stack:
        for target, fake in fakes.items():
            stack.enter_context(patch(target, fake))
        yield


def _jwt_for(world: _World, *, tampered: bool = False) -> str:
    import jwt

    from src.config.settings import settings

    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": world.jwt_subject,
            "tenant_id": world.jwt_tenant,
            "role": "clinic_admin",
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        settings.secret_key,
        algorithm="HS256",
    )
    if not tampered:
        return token
    header, payload, signature = token.split(".")
    return ".".join(
        (header, payload, ("A" if signature[0] != "A" else "B") + signature[1:])
    )


def _resolve(value: Any, world: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _resolve(item, world) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, world) for item in value]
    if isinstance(value, str):
        return _PLACEHOLDER.sub(lambda match: world[match.group(1)], value)
    return value


def _request_for(
    item: dict[str, Any], world: _World
) -> tuple[str, bytes, dict[str, str]]:
    names = asdict(world)
    path = _resolve(item["path"], names)
    raw = json.dumps(_resolve(item["json"], names)).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    kind, _, subject = item["auth"].partition("/")
    if kind == "api_key":
        headers["X-API-Key"] = (
            world.unknown_api_key if subject == "unknown" else world.api_key
        )
    elif kind == "jwt":
        headers["Authorization"] = f"Bearer {_jwt_for(world)}"
    elif kind == "jwt_invalid":
        headers["Authorization"] = f"Bearer {_jwt_for(world, tampered=True)}"
    elif kind in {"meta", "meta_bad_signature"}:
        secret = world.meta_secret if kind == "meta" else world.wrong_meta_secret
        digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={digest}"
    elif kind != "none":
        raise AssertionError(f"unknown corpus auth {item['auth']!r}")
    return path, raw, headers


async def _read_back(database_url: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            events = [
                {
                    "tenant_id": str(row.tenant_id),
                    "session_id": (
                        None if row.session_id is None else str(row.session_id)
                    ),
                    "event_type": row.event_type,
                    "interaction_origin": row.interaction_origin,
                    "event_data": row.event_data,
                    "event_metadata": row.event_metadata,
                }
                for row in await connection.execute(
                    text(
                        "SELECT tenant_id, session_id, event_type, interaction_origin, "
                        "event_data, metadata AS event_metadata FROM events"
                    )
                )
            ]
            tables = [
                name
                for (name,) in await connection.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                        "AND tablename LIKE 'credit\\_%' ORDER BY tablename"
                    )
                )
            ]
            credit_rows = {
                name: int(
                    (
                        await connection.execute(text(f'SELECT count(*) FROM "{name}"'))
                    ).scalar_one()
                )
                for name in tables
            }
    finally:
        await engine.dispose()
    return events, credit_rows


async def _replay(spec: dict[str, Any]) -> dict[str, Any]:
    import httpx
    from sqlalchemy import event

    world = _World(**spec["world"])
    with _fake_llm_and_providers():
        from src.api.main import app
        from src.repositories.database import get_db_engine
        from src.services.credit_switch import credit_switch_reading

        responses: list[dict[str, Any]] = []
        statements: list[str] = []

        def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
            statements.append(statement)

        async with app.router.lifespan_context(app):
            reading = credit_switch_reading()
            engine = get_db_engine().sync_engine
            event.listen(engine, "before_cursor_execute", record)
            try:
                transport = httpx.ASGITransport(
                    app=app, raise_app_exceptions=False, client=("testclient", 50000)
                )
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as client:
                    for item in _corpus()["requests"]:
                        path, raw, headers = _request_for(item, world)
                        response = await client.request(
                            item["method"], path, content=raw, headers=headers
                        )
                        try:
                            body: Any = response.json()
                        except ValueError:
                            body = response.text
                        responses.append(
                            {
                                "id": item["id"],
                                "status": response.status_code,
                                "body": body,
                            }
                        )
            finally:
                event.remove(engine, "before_cursor_execute", record)
    events, credit_rows = await _read_back(spec["database_url"])
    return {
        "reading": asdict(reading),
        "responses": responses,
        "events": events,
        "statements": statements,
        "credit_rows": credit_rows,
    }


def _runner_main() -> None:
    """Subprocess entry point: ``python -c '...; _runner_main()' <spec.json>``."""

    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    result = asyncio.run(_replay(spec))
    Path(spec["output"]).write_text(json.dumps(result), encoding="utf-8")


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _run_in_subprocess(
    database_url: Any, world: _World, *, enabled: bool, workdir: Path
) -> dict[str, Any]:
    label = f"{database_url.database}-{'on' if enabled else 'off'}"
    spec_path = workdir / f"{label}.spec.json"
    output = workdir / f"{label}.result.json"
    url = database_url.render_as_string(hide_password=False)
    spec_path.write_text(
        json.dumps(
            {"database_url": url, "world": asdict(world), "output": str(output)}
        ),
        encoding="utf-8",
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONSTARTUP"}
    }
    environment.update(
        {
            "DATABASE_URL": url,
            "CREDIT_HOT_PATH_ENABLED": "true" if enabled else "false",
            # Nothing the app might dial is reachable: no Redis, no Qdrant.
            "REDIS_URL": f"redis://127.0.0.1:{_closed_port()}/0",
            "QDRANT_HOST": "127.0.0.1",
            "QDRANT_PORT": str(_closed_port()),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from tests.integration.test_credit_schema_dominance_postgres import "
        "_runner_main; _runner_main()",
        str(spec_path),
        cwd=_ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        transcript, _ = await asyncio.wait_for(
            process.communicate(), timeout=_RUN_TIMEOUT_SECONDS
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    log = transcript.decode(errors="replace")
    assert process.returncode == 0, f"replay {label} failed:\n{log[-6000:]}"
    result: dict[str, Any] = json.loads(output.read_text(encoding="utf-8"))
    result["log"] = log
    return result


# ---------------------------------------------------------------------------
# Databases: one seeded template per revision, one clone per run.
# ---------------------------------------------------------------------------


@dataclass
class _Oracle:
    admin: Any
    admin_url: Any
    world: _World
    templates: dict[str, str]
    created: list[str]
    workdir: Path
    runs: dict[tuple[str, bool], dict[str, Any]]

    async def clone(self, revision: str) -> str:
        name = f"neurostack_asaas_smoke_{uuid4().hex[:12]}"
        async with self.admin.connect() as connection:
            quote = connection.dialect.identifier_preparer.quote
            await connection.exec_driver_sql(
                f"CREATE DATABASE {quote(name)} TEMPLATE {quote(self.templates[revision])}"
            )
        self.created.append(name)
        return name

    async def drop(self, name: str) -> None:
        from tests.integration.test_asaas_billing_postgres import _drop_child

        await _drop_child(self.admin, name)
        self.created.remove(name)

    async def run(self, revision: str, enabled: bool) -> dict[str, Any]:
        key = (revision, enabled)
        if key not in self.runs:
            name = await self.clone(revision)
            try:
                self.runs[key] = await _run_in_subprocess(
                    self.admin_url.set(database=name),
                    self.world,
                    enabled=enabled,
                    workdir=self.workdir,
                )
            finally:
                await self.drop(name)
        return self.runs[key]


@pytest.fixture(scope="module")
async def oracle(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[_Oracle]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from tests.integration.test_asaas_billing_postgres import (
        _create_child,
        _drop_child,
        _run_alembic,
        _validated_admin_url,
    )

    admin_url = _validated_admin_url(raw_admin_url)
    admin = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    world = _World.generate()
    created: list[str] = []
    oracle = _Oracle(
        admin=admin,
        admin_url=admin_url,
        world=world,
        templates={},
        created=created,
        workdir=tmp_path_factory.mktemp("credit-dominance"),
        runs={},
    )
    try:
        at_037 = f"neurostack_asaas_smoke_{uuid4().hex[:12]}"
        await _create_child(admin, at_037)
        created.append(at_037)
        await _run_alembic(admin_url.set(database=at_037), "upgrade", "037")
        await _seed_world(admin_url.set(database=at_037), world)
        oracle.templates["037"] = at_037
        at_038 = await oracle.clone("037")
        await _run_alembic(admin_url.set(database=at_038), "upgrade", "038")
        oracle.templates["038"] = at_038
        yield oracle
    finally:
        for name in list(reversed(created)):
            await _drop_child(admin, name)
        await admin.dispose()


# ---------------------------------------------------------------------------
# The oracle.
# ---------------------------------------------------------------------------


def test_the_mask_hides_generated_values_only() -> None:
    """No database: the clock, its greeting and unseeded IDs; nothing else."""

    world = _World.generate()
    masked = frozenset(_corpus()["masked_fields"])
    known = world.known_ids()

    def mask(text: str) -> Any:
        return _mask({"text": text}, masked, known)

    noon = 'Agora são 11:59. A saudação correta é: **"Bom dia"**. 26/10/2026 11:59'
    after = 'Agora são 12:01. A saudação correta é: **"Boa tarde"**. 26/10/2026 12:01'
    evening = 'It is now 18:01 BRT. Start your response with "Good evening!"'
    morning = 'It is now 09:00 BRT. Start your response with "Good morning!"'
    assert mask(noon) == mask(after)
    assert mask(evening) == mask(morning)
    assert mask(f"tenant {world.api_tenant}") != mask(f"tenant {uuid4()}")
    assert mask(f"id {uuid4()}") == mask(f"id {uuid4()}")
    assert mask("Olá! Posso ajudar?") != mask("Oi! Posso ajudar?")
    assert mask("Boa consulta") == {"text": "Boa consulta"}
    assert _mask({"event_id": "x", "intent": "agendar"}, masked, known) == {
        "event_id": "<masked>",
        "intent": "agendar",
    }


def test_the_corpus_covers_every_ingress_accepted_and_rejected() -> None:
    corpus = _corpus()
    by_ingress: dict[str, set[str]] = {}
    for item in corpus["requests"]:
        by_ingress.setdefault(item["ingress"], set()).add(item["auth"])
    assert set(by_ingress) == _INGRESSES
    ids = [item["id"] for item in corpus["requests"]]
    assert len(ids) == len(set(ids))
    assert {"event_id", "timestamp", "processing_time_ms"} <= set(
        corpus["masked_fields"]
    )


def _assert_same(on: dict[str, Any], off: dict[str, Any], world: _World) -> None:
    expected, actual = _comparable(off, world), _comparable(on, world)
    for want, got in zip(expected["responses"], actual["responses"], strict=True):
        assert got == want, got["id"]
    assert actual["events"] == expected["events"]


@pytest.mark.parametrize("revision", ["037", "038"])
async def test_config_on_answers_and_stores_like_config_off(
    oracle: _Oracle, revision: str
) -> None:
    off = await oracle.run(revision, enabled=False)
    on = await oracle.run(revision, enabled=True)

    assert off["reading"] == {
        "effective": False,
        "reason": "config_disabled",
        "revision": None,
    }
    if revision == "037":
        assert on["reading"] == {
            "effective": False,
            "reason": "schema_below_minimum",
            "revision": "037",
        }
    else:
        assert on["reading"] == {
            "effective": True,
            "reason": "schema_ready",
            "revision": "038",
        }
    _assert_same(on, off, oracle.world)


async def test_the_corpus_exercises_real_answers(oracle: _Oracle) -> None:
    """Not vacuous: accepted and rejected requests of every ingress happened."""

    run = await oracle.run("037", enabled=False)
    status = {response["id"]: response["status"] for response in run["responses"]}
    body = {response["id"]: response["body"] for response in run["responses"]}

    assert status["events-api-key-scheduling"] == 200
    assert status["events-jwt-panel-simulator"] == 200
    assert status["events-api-key-cross-tenant-assertion"] == 403
    assert status["events-api-key-cross-tenant-session"] == 403
    assert status["events-unknown-api-key"] == 401
    assert status["events-invalid-jwt"] == 401
    assert status["events-missing-credentials"] == 401
    assert status["events-invalid-payload"] in {400, 422}
    assert status["zapi-unknown-token"] == 401
    assert status["zapi-instance-mismatch"] == 401
    assert body["zapi-duplicate-message"] == {
        "ok": True,
        "skipped": "duplicate_message",
    }
    assert body["zapi-legacy-route"] == {"ok": True, "skipped": "legacy"}
    assert status["meta-bad-signature"] == 401
    assert body["uazapi-unknown-instance"] == {
        "ok": True,
        "skipped": "unknown_instance",
    }
    assert body["evolution-unknown-instance"] == {
        "ok": True,
        "skipped": "unknown_instance",
    }
    for accepted in (
        "zapi-text-message",
        "meta-text-message",
        "uazapi-text-message",
        "evolution-text-message",
    ):
        assert status[accepted] == 200, (accepted, body[accepted])
        assert body[accepted].get("replied") is True, (accepted, body[accepted])
    assert run["events"], "the accepted requests stored no event"


async def test_the_comparison_catches_a_real_difference(oracle: _Oracle) -> None:
    """Masking hides generated values only: a changed answer or event fails."""

    import copy

    run = await oracle.run("037", enabled=False)
    _assert_same(run, run, oracle.world)

    changed_status = copy.deepcopy(run)
    changed_status["responses"][0]["status"] = 500
    changed_body = copy.deepcopy(run)
    changed_body["responses"][0]["body"]["decision"]["intent"] = "outro"
    changed_event = copy.deepcopy(run)
    changed_event["events"][0]["event_type"] = "credit.observed"
    missing_event = copy.deepcopy(run)
    missing_event["events"].pop()
    for mutated in (changed_status, changed_body, changed_event, missing_event):
        with pytest.raises(AssertionError):
            _assert_same(mutated, run, oracle.world)


async def test_the_schema_revision_alone_changes_nothing(oracle: _Oracle) -> None:
    _assert_same(
        await oracle.run("038", enabled=False),
        await oracle.run("037", enabled=False),
        oracle.world,
    )


@pytest.mark.parametrize(
    ("revision", "enabled"),
    [("037", False), ("037", True), ("038", False), ("038", True)],
)
async def test_no_sql_touches_credit_tables_and_no_credit_row_exists(
    oracle: _Oracle, revision: str, enabled: bool
) -> None:
    run = await oracle.run(revision, enabled=enabled)

    assert run["statements"], "the listener saw no SQL at all"
    touching = [s for s in run["statements"] if _CREDIT_SQL.search(s)]
    # Required while the switch is not effective; in Group 1b nothing writes
    # even while it is (Group 3 relaxes the effective 038 run).
    assert touching == []
    if revision == "038":
        assert run["credit_rows"] and set(run["credit_rows"].values()) == {0}
    else:
        assert run["credit_rows"] == {}


# ---------------------------------------------------------------------------
# The refresh follows upgrade and downgrade in a running process.
# ---------------------------------------------------------------------------


async def _wait_for(effective: bool, deadline: float) -> float:
    from src.services.credit_switch import credit_switch_effective

    loop = asyncio.get_running_loop()
    started = loop.time()
    while credit_switch_effective() is not effective:
        if loop.time() - started > deadline:
            raise AssertionError(f"the switch did not turn {effective} in {deadline}s")
        await asyncio.sleep(0.05)
    return loop.time() - started


async def test_the_refresh_follows_an_upgrade_and_a_downgrade(
    oracle: _Oracle,
    monkeypatch: pytest.MonkeyPatch,
    _inject_isolated_database: None,
) -> None:
    import httpx
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import create_async_engine

    import src.repositories as repositories
    from src.api.main import app
    from src.config.settings import settings
    from src.repositories.database import get_db_engine
    from src.services.credit_switch import credit_switch_reading
    from tests.conftest import _production_init_database
    from tests.integration.test_asaas_billing_postgres import _run_alembic

    interval = 1
    # One refresh interval plus a margin for the read and the scheduler.
    bound = interval + 1.5
    name = await oracle.clone("037")
    url = oracle.admin_url.set(database=name)
    monkeypatch.setattr(settings, "credit_hot_path_enabled", True)
    monkeypatch.setattr(settings, "credit_switch_refresh_seconds", interval)
    monkeypatch.setattr(
        repositories,
        "init_database",
        functools.partial(
            _production_init_database,
            database_url=url.render_as_string(hide_password=False),
            engine_factory=create_async_engine,
        ),
    )
    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    unknown_key = {"X-API-Key": oracle.world.unknown_api_key}
    try:
        async with app.router.lifespan_context(app):
            assert credit_switch_reading().reason == "schema_below_minimum"
            engine = get_db_engine().sync_engine
            event.listen(engine, "before_cursor_execute", record)
            transport = httpx.ASGITransport(app=app, client=("testclient", 50000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                before = await client.post("/v1/events", json={}, headers=unknown_key)

                await _run_alembic(url, "upgrade", "038")
                assert await _wait_for(True, bound) <= bound
                assert credit_switch_reading().revision == "038"
                during = await client.post("/v1/events", json={}, headers=unknown_key)

                await _run_alembic(url, "downgrade", "037")
                assert await _wait_for(False, bound) <= bound
                assert credit_switch_reading().reason == "schema_below_minimum"
                after = await client.post("/v1/events", json={}, headers=unknown_key)
            event.remove(engine, "before_cursor_execute", record)
        assert credit_switch_reading().reason == "stopped"
    finally:
        await oracle.drop(name)

    assert [r.status_code for r in (before, during, after)] == [401, 401, 401]
    assert before.json() == during.json() == after.json()
    assert any("alembic_version" in statement for statement in statements)
    assert [s for s in statements if _CREDIT_SQL.search(s)] == []
