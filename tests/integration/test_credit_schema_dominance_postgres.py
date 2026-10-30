"""Credit dominance oracle on real PostgreSQL (neurostack-credit-system 1b).

Item 5 (ii) of Group 1b.  The real application (``src.api.main.app``, its
settings read from the environment, its lifespan, middleware and pool) runs
against disposable PostgreSQL databases at ``037`` and at the credit-ready
head (``040`` since Group 4b; ``039`` before it), with
``CREDIT_HOT_PATH_ENABLED`` false and true.  Each run replays the fixed corpus
``tests/fixtures/credit_dominance/requests.json`` through every ingress (JWT,
API key, Z-API, Meta, Uazapi, Evolution; the LLM and the providers are faked)
in a fresh interpreter, so no in-process cache, rate-limit bucket or replay
store crosses runs, and every run starts from a clone of one seeded template.

* With the config on, status and body equal the config-off run at the same
  revision once the server-generated fields are masked (the corpus' named
  fields plus any UUID the world did not seed and any instant), and the
  stored events are equal by content.  The two config-off runs at ``037`` and
  ``039`` are equal too; the additional-copy migration alone changes nothing.
* The verdict: config off => off; config on at ``037`` => off
  (``schema_below_minimum``); config on at ``040`` => on.
* A ``before_cursor_execute`` listener, attached only after start-up, sees no
  SQL touching ``credit_*`` while the switch is not effective; no ``credit_*``
  row exists after a run at ``038`` with the config off.
* Group 3a: with the switch effective (config on at ``038``) the only credit
  SQL is the observational writer's -- ``credit_enforcement_state``,
  ``credit_ingress_claims``, ``credit_observational_effects`` -- and every
  accepted decision of the corpus left exactly its claim and observational
  trace (origin and ``authenticated`` set, anchored on the claim or the
  public ``event_id``), while the answers and the stored events stay equal
  to the config-off run.  ``POST /v1/events`` without ``Idempotency-Key`` is
  accepted.
* Group 3b-1: the corpus carries ``POST /v1/events`` cases with an
  ``Idempotency-Key``, one of them a synchronous phase 1 handoff (on
  PostgreSQL since #145/#146).  Before the enforcement they answer like the
  config-off run (no enforced sibling runs).  With ``credit_enforcement_at``
  set: config off answers and stores exactly like ``main`` and touches no
  ``credit_*`` table; config on enforces -- production without a key is 400,
  each keyed production decision is charged once with its stored answer (a
  retry replays it), the phase 1 handoff carries its ``credit_effect_id``,
  and simulator traffic answers like the config-off run, free.
* Group 3b-2: the corpus carries a Meta file attachment (its ack, free) and
  the patient's forward-on-request reply (a Meta file handoff).  With
  ``credit_enforcement_at`` set and the config on, every Z-API and Meta
  decision -- the file handoff included -- is claimed and charged once,
  answers exactly like the config-off run (its reply sent inline from the
  response outbox, ``delivered`` with phone and text erased) and stores the
  same events (plus ``credit_effect_id``); a redelivery is
  ``duplicate_message``; Uazapi and Evolution stay free and unclaimed.
  The one explicit exception to "no SQL touches ``credit_*`` while the
  switch is not effective" is the outbox's retention sweeper (owner decision
  OD-3: it only ERASES expired payloads, config on or off); its first tick
  comes one interval (``RETENTION_SWEEP_INTERVAL_SECONDS``) after start-up,
  so it never runs inside a replay (pinned below), and it skips a ``037``
  database (no outbox).
* Group 4a: the corpus carries a knowledge text upload (JSON), a media
  upload, a download of a seeded media asset and the multipart DOCX/PDF
  knowledge upload.  The first three answer like the config-off run in every
  run; with the switch effective and before the enforcement they leave only
  observational ``rag``/``media``/``egress``/``maintenance`` effects (and the
  delivery), with the enforcement set they are reserved and charged
  (``credit_allocations``, the ledger) with the same answers.  The multipart
  upload is a credit surface: 503 ``credit_unavailable`` while the switch is
  not effective (``037``, config off), 201 with it -- the one corpus case
  left out of the on/off comparison and asserted on its own.
* In one process, the background refresh turns the switch on after
  ``upgrade 037 -> 040`` and off again after the downgrade, each within one
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
# The only credit tables the observational writers may touch: Group 3a's
# and Group 4a's (the builder's monthly cycles, read for the accounting
# window, and the observational egress deliveries).
_METERING_TABLES = frozenset(
    {
        "credit_enforcement_state",
        "credit_ingress_claims",
        "credit_observational_effects",
        "credit_lots",
        "credit_egress_deliveries",
    }
)
# Group 4a: the credit surface of the corpus, outside the on/off comparison.
_CREDIT_SURFACES = frozenset({"knowledge-files-upload"})
_FIXTURES = _ROOT / "tests" / "fixtures"
_MULTIPART_BOUNDARY = "credit-dominance-boundary-4a"
_RUN_TIMEOUT_SECONDS = 300
# The one statement of ``CreditRepository.enforcement_at()``.
_ENFORCEMENT_READ = re.compile(
    r"SELECT credit_enforcement_state\.credit_enforcement_at\s+"
    r"FROM credit_enforcement_state\s+"
    r"WHERE credit_enforcement_state\.id = \$1::SMALLINT"
)


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
    # Group 3b-1: a session of the API-key tenant that only the synchronous
    # handoff case uses, so its answer does not depend on the earlier turns.
    handoff_session: str
    # Group 4a: a media asset of the API-key tenant, seeded, downloaded.
    media_asset: str

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
            handoff_session=str(uuid4()),
            media_asset=str(uuid4()),
        )

    def known_ids(self) -> set[str]:
        return {
            value.lower()
            for value in asdict(self).values()
            if _UUID.fullmatch(value) is not None
        }


_SEEDED_MEDIA = b"%PDF-1.4\n% credit dominance seeded media\n" + b"0" * 900


async def _seed_world(url: Any, world: _World) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import NullPool

    from src.api.middleware.auth import pwd_context
    from src.models.builder import Builder
    from src.models.clinic_config_table import ClinicConfigTable
    from src.models.media_asset import MediaAsset
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
                    session(world.handoff_session, world.api_tenant),
                    session(world.jwt_session, world.jwt_tenant),
                    MediaAsset(
                        id=UUID(world.media_asset),
                        tenant_id=UUID(world.api_tenant),
                        display_name="Cardápio",
                        original_filename="cardapio.pdf",
                        category="pdf",
                        mime_type="application/pdf",
                        size_bytes=len(_SEEDED_MEDIA),
                        sha256=hashlib.sha256(_SEEDED_MEDIA).digest(),
                        payload=_SEEDED_MEDIA,
                        usage_trigger=None,
                        auto_suggest=False,
                    ),
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


def _multipart(spec: dict[str, Any]) -> tuple[bytes, str]:
    """A ``multipart/form-data`` body (fixed boundary): ``fields`` then
    ``files``, each file's bytes read from its ``fixture``."""

    boundary = _MULTIPART_BOUNDARY.encode("ascii")
    body = bytearray()
    for name, value in spec.get("fields", {}).items():
        body += b"--" + boundary + b"\r\n"
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += str(value).encode("utf-8") + b"\r\n"
    for item in spec.get("files", []):
        body += b"--" + boundary + b"\r\n"
        body += (
            f'Content-Disposition: form-data; name="{item["field"]}"; '
            f'filename="{item["filename"]}"\r\n'
            f'Content-Type: {item["content_type"]}\r\n\r\n'
        ).encode()
        body += (_FIXTURES / item["fixture"]).read_bytes() + b"\r\n"
    body += b"--" + boundary + b"--\r\n"
    return bytes(body), f"multipart/form-data; boundary={_MULTIPART_BOUNDARY}"


def _request_for(
    item: dict[str, Any], world: _World
) -> tuple[str, bytes, dict[str, str]]:
    names = asdict(world)
    path = _resolve(item["path"], names)
    if "multipart" in item:
        raw, content_type = _multipart(_resolve(item["multipart"], names))
        headers = {"Content-Type": content_type, **item.get("headers", {})}
    elif "json" in item:
        raw = json.dumps(_resolve(item["json"], names)).encode("utf-8")
        headers = {"Content-Type": "application/json", **item.get("headers", {})}
    else:
        raw, headers = b"", dict(item.get("headers", {}))
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


_CLAIM_COLUMNS = (
    "tenant_id",
    "builder_id",
    "source",
    "principal_id",
    "idempotency_key",
    "provider_message_id",
    "interaction_id",
    "payload_sha256",
    "decision_version",
    "session_version",
    "mode",
)
_EFFECT_COLUMNS = (
    "tenant_id",
    "builder_id",
    "effect_id",
    "effect_kind",
    "trace_id",
    "origin",
    "authenticated",
    "would_debit_mc",
    "decision_digest",
)


def _plain(value: Any) -> Any:
    return value if value is None or isinstance(value, (bool, int, str)) else str(value)


async def _metering_rows(connection: Any) -> dict[str, list[dict[str, Any]]]:
    from sqlalchemy import text

    rows: dict[str, list[dict[str, Any]]] = {}
    for table, columns in (
        ("credit_ingress_claims", _CLAIM_COLUMNS),
        ("credit_observational_effects", _EFFECT_COLUMNS),
    ):
        result = await connection.execute(
            text(f"SELECT {', '.join(columns)} FROM {table}")
        )
        rows[table] = [
            {column: _plain(value) for column, value in zip(columns, row)}
            for row in result
        ]
    return rows


async def _ledger_rows(connection: Any) -> dict[str, list[dict[str, Any]]]:
    """Group 3b-1: what the enforced path wrote (effects, ledger, stored
    answers) and the handoffs' ``credit_effect_id``."""

    from sqlalchemy import text

    queries = {
        "credit_effects": (
            "SELECT tenant_id, builder_id, effect_id, effect_kind, "
            "decision_digest FROM credit_effects ORDER BY effect_id"
        ),
        "credit_ledger_entries": (
            "SELECT builder_id, entry_type, amount_mc, effect_id "
            "FROM credit_ledger_entries ORDER BY effect_id, entry_type"
        ),
        "credit_response_outbox": (
            "SELECT tenant_id, effect_id, state, payload "
            "FROM credit_response_outbox ORDER BY effect_id"
        ),
        "handoff_records": (
            "SELECT tenant_id, session_id, context ->> 'credit_effect_id' "
            "AS credit_effect_id FROM handoff_records ORDER BY id"
        ),
        # Group 4a: the reservations, openings and egress of the run.
        "credit_allocations": (
            "SELECT builder_id, category, allocation_kind, units, target_mc, "
            "charged_mc, effect_id FROM credit_allocations "
            "ORDER BY category, allocation_kind, source_key"
        ),
        "credit_egress_deliveries": (
            "SELECT tenant_id, builder_id, media_id, bytes, mode "
            "FROM credit_egress_deliveries ORDER BY recorded_at"
        ),
    }
    rows: dict[str, list[dict[str, Any]]] = {}
    for table, sql in queries.items():
        result = await connection.execute(text(sql))
        rows[table] = [
            {
                key: value if key == "payload" else _plain(value)
                for key, value in row._mapping.items()
            }
            for row in result
        ]
    return rows


async def _read_back(
    database_url: str,
) -> tuple[
    list[dict[str, Any]],
    dict[str, int],
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
]:
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
            metering = await _metering_rows(connection) if tables else {}
            ledger = (
                await _ledger_rows(connection) if "credit_allocations" in tables else {}
            )
    finally:
        await engine.dispose()
    return events, credit_rows, metering, ledger


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
    events, credit_rows, metering, ledger = await _read_back(spec["database_url"])
    return {
        "reading": asdict(reading),
        "responses": responses,
        "events": events,
        "statements": statements,
        "credit_rows": credit_rows,
        "metering": metering,
        "ledger": ledger,
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
    runs: dict[tuple[str, bool, bool], dict[str, Any]]

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

    async def enforce(self, name: str) -> None:
        """Group 3b-1: set ``credit_enforcement_at`` in a credit-ready clone."""

        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        engine = create_async_engine(
            self.admin_url.set(database=name), poolclass=NullPool
        )
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO credit_enforcement_state (id, "
                        "credit_enforcement_at, set_by, set_at) VALUES (1, "
                        "clock_timestamp() - interval '1 minute', "
                        "'credit-dominance', clock_timestamp())"
                    )
                )
        finally:
            await engine.dispose()

    async def run(
        self, revision: str, enabled: bool, *, enforced: bool = False
    ) -> dict[str, Any]:
        key = (revision, enabled, enforced)
        if key not in self.runs:
            name = await self.clone(revision)
            try:
                if enforced:
                    await self.enforce(name)
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
        # The credit-ready head: 040 (Group 4b; 039 before it).
        at_040 = await oracle.clone("037")
        await _run_alembic(admin_url.set(database=at_040), "upgrade", "040")
        oracle.templates["040"] = at_040
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


def test_the_retention_sweeper_never_ticks_inside_a_replay() -> None:
    """OD-3's sweeper runs config on or off; the statement listeners of the
    runs below stay exact because its first tick waits a whole interval,
    longer than any replay is allowed to take."""

    from src.services.credit_metering import RETENTION_SWEEP_INTERVAL_SECONDS

    assert RETENTION_SWEEP_INTERVAL_SECONDS >= _RUN_TIMEOUT_SECONDS


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
        assert got["id"] == want["id"]
        if got["id"] in _CREDIT_SURFACES:
            continue  # asserted on its own (Group 4a)
        assert got == want, got["id"]
    assert actual["events"] == expected["events"]


@pytest.mark.parametrize("revision", ["037", "040"])
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
            "revision": "040",
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
    # Group 3b-2: a Meta file ack, then a Meta file handoff on PostgreSQL.
    assert body["meta-file-document"]["action"] == "file_ack"
    assert body["meta-file-forward"] == {
        "ok": True,
        "replied": True,
        "action": "file_handoff",
    }
    assert run["events"], "the accepted requests stored no event"
    # Group 3b-1: a synchronous phase 1 handoff on PostgreSQL (#145/#146).
    assert status["events-api-key-handoff"] == 200
    assert body["events-api-key-handoff"]["handoff_summary"] is not None
    assert status["events-api-key-enforced-retry"] == 200
    # Group 4a: the uploads and the download are main's real answers.
    assert status["knowledge-json-upload"] == 201, body["knowledge-json-upload"]
    assert status["media-upload-pdf"] == 201, body["media-upload-pdf"]
    assert body["media-upload-pdf"]["category"] == "pdf"
    assert status["media-download-seeded"] == 200
    assert body["media-download-seeded"].startswith("%PDF-1.4")
    # The multipart upload is a credit surface: off here.
    assert (status["knowledge-files-upload"], body["knowledge-files-upload"]) == (
        503,
        {"code": "credit_unavailable"},
    )


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
        await oracle.run("040", enabled=False),
        await oracle.run("037", enabled=False),
        oracle.world,
    )


@pytest.mark.parametrize(
    ("revision", "enabled"),
    [("037", False), ("037", True), ("040", False)],
)
async def test_no_sql_touches_credit_tables_and_no_credit_row_exists(
    oracle: _Oracle, revision: str, enabled: bool
) -> None:
    """While the switch is not effective (config off, or a 037 database)."""

    run = await oracle.run(revision, enabled=enabled)

    assert run["statements"], "the listener saw no SQL at all"
    touching = [s for s in run["statements"] if _CREDIT_SQL.search(s)]
    assert touching == []
    if revision == "040":
        assert run["credit_rows"] and set(run["credit_rows"].values()) == {0}
    else:
        assert run["credit_rows"] == {}


async def test_the_effective_switch_only_meters_observationally(
    oracle: _Oracle,
) -> None:
    """Config on at the credit-ready head (Groups 3a and 4a): only the
    writers' tables are touched, and nothing financial is ever written
    before the enforcement."""

    run = await oracle.run("040", enabled=True)

    names = {
        match.group(0).lower()
        for statement in run["statements"]
        for match in _CREDIT_SQL.finditer(statement)
    }
    rows = run["credit_rows"]
    assert names & set(rows) == _METERING_TABLES
    # The one other credit_* name in that SQL is the enforcement column.
    assert names - set(rows) == {"credit_enforcement_at"}
    assert rows["credit_ingress_claims"] > 0
    assert rows["credit_observational_effects"] > 0
    assert {
        table: count
        for table, count in rows.items()
        if table not in _METERING_TABLES and count
    } == {}


def _event_id(run: dict[str, Any], case: str) -> str:
    [body] = [r["body"] for r in run["responses"] if r["id"] == case]
    return str(body["event_id"])


async def test_every_allowlisted_decision_leaves_its_claim_and_trace(
    oracle: _Oracle,
) -> None:
    run = await oracle.run("040", enabled=True)
    world = oracle.world
    status = {r["id"]: r["status"] for r in run["responses"]}
    claims = run["metering"]["credit_ingress_claims"]
    # The traces (Group 3a); Group 4a's upload effects are asserted below.
    effects = [
        effect
        for effect in run["metering"]["credit_observational_effects"]
        if effect["effect_kind"] == "trace"
    ]

    # No Idempotency-Key is still main's 200 (observational only).
    assert status["events-api-key-scheduling"] == 200
    assert status["events-api-key-invalid-idempotency-key"] == 200
    # Every trace row carries origin, authenticated and its decision digest.
    for effect in effects:
        assert effect["origin"] in {"production", "simulator", "unknown"}
        assert effect["authenticated"] in {True, False}
        assert effect["effect_kind"] == "trace"
        assert effect["decision_digest"] and len(effect["decision_digest"]) == 64
        assert effect["effect_id"] == (
            f"trace:{effect['tenant_id']}:{effect['trace_id']}:decision:1"
        )
    by_tenant: dict[str, list[dict[str, Any]]] = {}
    for effect in effects:
        by_tenant.setdefault(effect["tenant_id"], []).append(effect)

    # API key: one claim for the key (the retry reused it), one trace per
    # other accepted event, anchored on its public event_id.
    keyed = {c["idempotency_key"]: c for c in claims if c["source"] == "events"}
    assert set(keyed) == {
        "credit-dominance-key-1",
        "credit-dominance-enforced-1",
        "credit-dominance-handoff-1",
    }
    key_claim = keyed["credit-dominance-key-1"]
    assert key_claim["principal_id"] == f"api_key:{world.api_tenant}"
    assert key_claim["builder_id"] == world.builder
    assert key_claim["mode"] == "observational"
    assert key_claim["interaction_id"] == _event_id(
        run, "events-api-key-idempotency-key"
    )
    api = by_tenant[world.api_tenant]
    assert sorted(e["trace_id"] for e in api) == sorted(
        [
            _event_id(run, "events-api-key-scheduling"),
            _event_id(run, "events-api-key-second-turn"),
            key_claim["interaction_id"],
            _event_id(run, "events-api-key-invalid-idempotency-key"),
            _event_id(run, "events-api-key-session-start"),
            keyed["credit-dominance-enforced-1"]["interaction_id"],
            keyed["credit-dominance-handoff-1"]["interaction_id"],
        ]
    )
    # Group 3b-1 cases before the enforcement: observational claims anchored
    # on the first execution (the retry reused it), the handoff too.
    for key, case in (
        ("credit-dominance-enforced-1", "events-api-key-enforced-decision"),
        ("credit-dominance-handoff-1", "events-api-key-handoff"),
    ):
        assert keyed[key]["mode"] == "observational"
        assert keyed[key]["interaction_id"] == _event_id(run, case)
    assert {(e["origin"], e["authenticated"], e["would_debit_mc"]) for e in api} == {
        ("production", True, 1_000)
    }
    # The panel JWT: simulator, no claim even with a key (H3), costs nothing.
    jwt = by_tenant[world.jwt_tenant]
    assert sorted(e["trace_id"] for e in jwt) == sorted(
        [
            _event_id(run, "events-jwt-panel-simulator"),
            _event_id(run, "events-jwt-panel-idempotency-key"),
        ]
    )
    assert {(e["origin"], e["authenticated"], e["would_debit_mc"]) for e in jwt} == {
        ("simulator", True, 0)
    }
    # Providers: one claim per delivered message, its trace on the claim.
    providers = {c["source"]: [] for c in claims if c["source"] != "events"}
    for claim in claims:
        if claim["source"] != "events":
            providers[claim["source"]].append(claim)
    assert sorted(c["provider_message_id"] for c in providers["zapi"]) == [
        "credit-dominance-zapi-1",
        "credit-dominance-zapi-5",
    ]
    assert set(providers) == {"zapi", "meta", "uazapi", "evolution"}
    expected = {
        "zapi": (world.zapi_tenant, "production", True, 1_000),
        "meta": (world.meta_tenant, "production", True, 1_000),
        "uazapi": (world.uazapi_tenant, "unknown", False, 0),
        "evolution": (world.evolution_tenant, "unknown", False, 0),
    }
    for source, (tenant, origin, authenticated, cost) in expected.items():
        traced = by_tenant[tenant]
        assert sorted(e["trace_id"] for e in traced) == sorted(
            c["interaction_id"] for c in providers[source]
        )
        assert {c["principal_id"] for c in providers[source]} == {f"{source}:{tenant}"}
        assert {
            (e["origin"], e["authenticated"], e["would_debit_mc"]) for e in traced
        } == {(origin, authenticated, cost)}
    # Rejected and skipped requests left nothing: only these six tenants.
    assert set(by_tenant) == {
        world.api_tenant,
        world.jwt_tenant,
        world.zapi_tenant,
        world.meta_tenant,
        world.uazapi_tenant,
        world.evolution_tenant,
    }


def _bodies(run: dict[str, Any]) -> dict[str, Any]:
    return {r["id"]: r["body"] for r in run["responses"]}


@pytest.mark.parametrize(
    ("revision", "enabled", "expected"),
    [
        ("037", False, 503),
        ("037", True, 503),
        ("040", False, 503),
        ("040", True, 201),
    ],
)
async def test_the_multipart_upload_exists_only_with_the_switch_effective(
    oracle: _Oracle, revision: str, enabled: bool, expected: int
) -> None:
    run = await oracle.run(revision, enabled=enabled)
    [answer] = [r for r in run["responses"] if r["id"] == "knowledge-files-upload"]
    assert answer["status"] == expected, answer
    if expected == 503:
        assert answer["body"] == {"code": "credit_unavailable"}
    else:
        [document] = answer["body"]["documents"]
        assert document["tenant_id"] == oracle.world.api_tenant
        assert document["metadata"]["title"] == "horarios.pdf"


async def test_uploads_and_downloads_are_metered_observationally_only(
    oracle: _Oracle,
) -> None:
    """Group 4a, switch effective, before the enforcement: what each upload
    and download WOULD debit, the window's maintenance once per category,
    the delivery -- and nothing financial."""

    run = await oracle.run("040", enabled=True)
    world = oracle.world
    bodies = _bodies(run)
    effects = {
        effect["effect_id"]: effect
        for effect in run["metering"]["credit_observational_effects"]
        if effect["effect_kind"] != "trace"
    }
    tenant = world.api_tenant
    json_doc = bodies["knowledge-json-upload"]["id"]
    media_id = bodies["media-upload-pdf"]["id"]
    rag = effects[f"rag:{tenant}:{json_doc}:reserve"]
    media = effects[f"media:{tenant}:{media_id}:reserve"]
    from src.services.credit_allocations import aggregate_target_mc

    [item] = [i for i in _corpus()["requests"] if i["id"] == "knowledge-json-upload"]
    characters = len(item["json"]["content"])
    uploaded = (
        _FIXTURES / "credit_storage_conformance/positive/textual.pdf"
    ).read_bytes()
    assert (rag["effect_kind"], rag["builder_id"]) == ("rag", world.builder)
    # The builder's first RAG content: its whole target, rounded once, up.
    assert rag["would_debit_mc"] == aggregate_target_mc(characters, "rag") > 0
    # What the upload adds on top of the seeded asset (rounded once).
    assert (media["effect_kind"], media["would_debit_mc"]) == (
        "media",
        aggregate_target_mc(len(_SEEDED_MEDIA) + len(uploaded), "media")
        - aggregate_target_mc(len(_SEEDED_MEDIA), "media"),
    )
    [egress] = [e for e in effects.values() if e["effect_kind"] == "egress"]
    assert egress["effect_id"].startswith(f"egress:{tenant}:{world.media_asset}:")
    assert egress["would_debit_mc"] == 0  # within the included 200 KB per mc
    maintenance = sorted(
        e["effect_id"].rsplit(":", 1)[-1]
        for e in effects.values()
        if e["effect_kind"] == "maintenance"
    )
    assert maintenance == ["media", "rag"]
    # The multipart upload is measured too (one effect for its batch).
    batch = [
        e
        for e in effects.values()
        if e["effect_kind"] == "rag" and e["effect_id"] != rag["effect_id"]
    ]
    assert len(batch) == 1
    rows = run["credit_rows"]
    assert rows["credit_egress_deliveries"] == 1
    for financial in (
        "credit_allocations",
        "credit_inventory_versions",
        "credit_ledger_entries",
        "credit_effects",
        "credit_accounts",
    ):
        assert rows[financial] == 0, financial


# ---------------------------------------------------------------------------
# Group 3b-1: the corpus with ``credit_enforcement_at`` set.
# ---------------------------------------------------------------------------

# Production (API key) cases the enforcement answers differently: no usable
# ``Idempotency-Key`` is a 400 before anything else (MEDIUM-1).
_KEYLESS = {
    "events-api-key-scheduling": "idempotency_key_required",
    "events-api-key-second-turn": "idempotency_key_required",
    "events-api-key-cross-tenant-session": "idempotency_key_required",
    "events-api-key-invalid-idempotency-key": "idempotency_key_invalid",
    "events-api-key-session-start": "idempotency_key_required",
}
# Each keyed production decision, charged once ...
_CHARGED = {
    "credit-dominance-key-1": "events-api-key-idempotency-key",
    "credit-dominance-enforced-1": "events-api-key-enforced-decision",
    "credit-dominance-handoff-1": "events-api-key-handoff",
}
# ... and its retry, which replays the stored answer.
_REPLAYS = {
    "events-api-key-idempotency-retry": "events-api-key-idempotency-key",
    "events-api-key-enforced-retry": "events-api-key-enforced-decision",
}
# Group 3b-2: each Z-API/Meta decision of the corpus, charged once -- the
# duplicate delivery and the free file ack are not.  ``True`` when its reply
# went through the response outbox (the file handoff texts the patient as
# today, without it).
_WEBHOOK_CHARGED = {
    ("zapi", "credit-dominance-zapi-1"): True,
    ("zapi", "credit-dominance-zapi-5"): True,
    ("meta", "wamid.credit-dominance-meta-1"): True,
    ("meta", "wamid.credit-dominance-meta-forward"): False,
}


def _without_credit_effect_id(row: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(row.get("event_metadata") or {})
    metadata.pop("credit_effect_id", None)
    return {**row, "event_metadata": metadata}


async def test_with_the_config_off_an_enforced_database_answers_like_main(
    oracle: _Oracle,
) -> None:
    """``credit_enforcement_at`` set but the switch off: ``main`` exactly,
    no SQL touches a ``credit_*`` table, nothing is claimed or charged."""

    off = await oracle.run("040", enabled=False)
    enforced = await oracle.run("040", enabled=False, enforced=True)

    assert enforced["reading"] == off["reading"]
    _assert_same(enforced, off, oracle.world)
    assert [s for s in enforced["statements"] if _CREDIT_SQL.search(s)] == []
    assert {
        table: count for table, count in enforced["credit_rows"].items() if count
    } == {"credit_enforcement_state": 1}


async def test_the_enforced_run_charges_each_production_decision_once(
    oracle: _Oracle,
) -> None:
    off = await oracle.run("040", enabled=False)
    run = await oracle.run("040", enabled=True, enforced=True)
    world = oracle.world

    assert run["reading"]["effective"] is True
    answers = {r["id"]: r for r in run["responses"]}
    for case, code in _KEYLESS.items():
        assert (answers[case]["status"], answers[case]["body"]) == (
            400,
            {"code": code},
        ), case
    for case in _CHARGED.values():
        assert answers[case]["status"] == 200, (case, answers[case]["body"])
    for retry, first in _REPLAYS.items():
        assert answers[retry]["status"] == 200
        assert answers[retry]["body"] == answers[first]["body"]  # replayed
    assert answers["events-api-key-handoff"]["body"]["handoff_summary"]

    # Everything else -- the panel JWT (simulator), rejected requests, the
    # provider webhooks, enforced or not, and Group 4a's uploads and
    # download (reserved and charged below) -- answers like the config-off
    # run.  The multipart upload is a credit surface (off: 503; here: 201).
    production = set(_KEYLESS) | set(_REPLAYS) | set(_CHARGED.values())
    expected, actual = _comparable(off, world), _comparable(run, world)
    for want, got in zip(expected["responses"], actual["responses"], strict=True):
        if got["id"] not in production | _CREDIT_SURFACES:
            assert got == want, got["id"]
    assert answers["knowledge-files-upload"]["status"] == 201
    # The handoff runs on a session of its own: its enforced answer and its
    # stored events are main's (plus the srm.decision's credit_effect_id).
    [want] = [r for r in expected["responses"] if r["id"] == "events-api-key-handoff"]
    [got] = [r for r in actual["responses"] if r["id"] == "events-api-key-handoff"]
    assert got == want

    def handoff_events(source: dict[str, Any]) -> list[str]:
        rows = [
            _without_credit_effect_id(row)
            for row in source["events"]
            if row["session_id"] == world.handoff_session
        ]
        return _comparable({"responses": [], "events": rows}, world)["events"]

    assert handoff_events(run) == handoff_events(off) != []

    # Group 3b-2: the webhook tenants stored the same events as main (the
    # srm.decision rows carry their credit_effect_id on top).
    def tenant_events(source: dict[str, Any], tenant: str) -> list[str]:
        rows = [
            _without_credit_effect_id(row)
            for row in source["events"]
            if row["tenant_id"] == tenant
        ]
        return _comparable({"responses": [], "events": rows}, world)["events"]

    for tenant in (world.zapi_tenant, world.meta_tenant):
        assert tenant_events(run, tenant) == tenant_events(off, tenant) != []

    claims = run["metering"]["credit_ingress_claims"]
    keyed = {c["idempotency_key"]: c for c in claims if c["source"] == "events"}
    by_message = {
        (c["source"], c["provider_message_id"]): c
        for c in claims
        if c["source"] != "events"
    }
    # Only the enforced claims: no observational claim or trace once the
    # enforcement is set (the writer stands down), none for the simulator,
    # Uazapi or Evolution.
    assert set(keyed) == set(_CHARGED) and set(by_message) == set(_WEBHOOK_CHARGED)
    assert len(claims) == len(_CHARGED) + len(_WEBHOOK_CHARGED)
    tenant_of = {
        "events": world.api_tenant,
        "zapi": world.zapi_tenant,
        "meta": world.meta_tenant,
    }
    assert {(c["mode"], c["tenant_id"]) for c in claims} == {
        ("enforced", tenant_of[c["source"]]) for c in claims
    }
    assert {c["principal_id"] for c in by_message.values()} == {
        f"zapi:{world.zapi_tenant}",
        f"meta:{world.meta_tenant}",
    }
    assert all(c["session_version"] is not None for c in claims)
    assert run["metering"]["credit_observational_effects"] == []

    def trace(claim: dict[str, Any]) -> str:
        return f"trace:{claim['tenant_id']}:{claim['interaction_id']}:decision:1"

    effect_ids = {key: trace(keyed[key]) for key in _CHARGED}
    webhook_effects = {key: trace(claim) for key, claim in by_message.items()}
    every_effect = sorted([*effect_ids.values(), *webhook_effects.values()])
    ledger = run["ledger"]
    # The traces; Group 4a's reservations are asserted below.
    effects = [e for e in ledger["credit_effects"] if e["effect_kind"] == "trace"]
    assert sorted(e["effect_id"] for e in effects) == every_effect
    assert {(e["effect_kind"], e["builder_id"]) for e in effects} == {
        ("trace", world.builder)
    }
    assert all(len(e["decision_digest"] or "") == 64 for e in effects)
    # No lot was ever granted: each trace is 1 credit of debt, once.
    entries = [
        e
        for e in ledger["credit_ledger_entries"]
        if e["effect_id"].startswith("trace:")
    ]
    assert sorted(
        (e["effect_id"], e["entry_type"], e["amount_mc"]) for e in entries
    ) == (sorted((effect, "debt", 1_000) for effect in every_effect))
    outbox = {row["effect_id"]: row for row in ledger["credit_response_outbox"]}
    replied = {
        webhook_effects[key] for key, outboxed in _WEBHOOK_CHARGED.items() if outboxed
    }
    assert set(outbox) == set(effect_ids.values()) | replied
    for key, case in _CHARGED.items():
        assert outbox[effect_ids[key]]["state"] == "delivered"
        assert outbox[effect_ids[key]]["payload"] == answers[case]["body"]
    for effect in replied:
        # Sent inline, then delivered with phone and text erased.
        assert outbox[effect]["state"] == "delivered"
        payload = outbox[effect]["payload"]
        assert "phone" not in payload and "text" not in payload
        assert payload["kind"] == "webhook_reply.v1"
    handoffs = [
        h
        for h in ledger["handoff_records"]
        if h["tenant_id"] in (world.api_tenant, world.meta_tenant)
    ]
    assert sorted((h["tenant_id"], h["credit_effect_id"]) for h in handoffs) == sorted(
        [
            (world.api_tenant, effect_ids["credit-dominance-handoff-1"]),
            (
                world.meta_tenant,
                webhook_effects[("meta", "wamid.credit-dominance-meta-forward")],
            ),
        ]
    )
    linked = sorted(
        row["event_metadata"]["credit_effect_id"]
        for row in run["events"]
        if row["event_type"] == "srm.decision"
        and "credit_effect_id" in (row["event_metadata"] or {})
    )
    # A file handoff has no srm.decision: its link is the handoff's context.
    assert linked == sorted(set(every_effect) - set(webhook_effects.values()) | replied)
    assert run["credit_rows"]["credit_accounts"] == 1

    # Group 4a: the window open at the enforcement got a free baseline
    # (nothing stored before it is charged: the seeded media asset); each
    # RAG upload reserved its aggregate delta, as debt (no lot); the media
    # upload added nothing above the baseline's coverage (rounded once); the
    # download was a delivery within the allowance (no egress charge).
    bodies = {r["id"]: r["body"] for r in run["responses"]}
    tenant = world.api_tenant
    allocations = ledger["credit_allocations"]
    assert sorted((a["category"], a["allocation_kind"]) for a in allocations) == [
        ("media", "baseline"),
        ("rag", "baseline"),
        ("rag", "reservation"),
        ("rag", "reservation"),
    ]
    assert all(
        a["charged_mc"] == 0 and a["effect_id"] is None
        for a in allocations
        if a["allocation_kind"] == "baseline"
    )
    reserved = {
        a["effect_id"]: a for a in allocations if a["allocation_kind"] == "reservation"
    }
    json_effect = f"rag:{tenant}:{bodies['knowledge-json-upload']['id']}:reserve"
    media_effect = f"media:{tenant}:{bodies['media-upload-pdf']['id']}:reserve"
    assert json_effect in reserved and media_effect not in reserved
    [baseline] = [
        a
        for a in allocations
        if a["allocation_kind"] == "baseline" and a["category"] == "media"
    ]
    assert baseline["units"] == len(_SEEDED_MEDIA)
    upload_entries = sorted(
        (e["effect_id"], e["entry_type"], e["amount_mc"])
        for e in ledger["credit_ledger_entries"]
        if not e["effect_id"].startswith("trace:")
    )
    assert upload_entries == sorted(
        (effect, "debt", allocation["charged_mc"])
        for effect, allocation in reserved.items()
    )
    assert [
        (d["tenant_id"], d["media_id"], d["mode"])
        for d in ledger["credit_egress_deliveries"]
    ] == [(tenant, world.media_asset, "enforced")]
    assert {e["effect_kind"] for e in ledger["credit_effects"]} == {"trace", "rag"}


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

                await _run_alembic(url, "upgrade", "040")
                assert await _wait_for(True, bound) <= bound
                assert credit_switch_reading().revision == "040"
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
    # The rejected requests touch no credit table.  Group 3b-2: with the
    # config on, the response-outbox publisher's tick reads
    # credit_enforcement_at while the switch is effective -- that read and
    # nothing else (no claim, no sweep: the enforcement is not set).
    credit_sql = [s for s in statements if _CREDIT_SQL.search(s)]
    assert all(_ENFORCEMENT_READ.fullmatch(s.strip()) for s in credit_sql), credit_sql
