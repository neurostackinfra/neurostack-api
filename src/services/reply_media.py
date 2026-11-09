"""The agent's delivery of one Central de Mídias file (envio-midia-agente G5).

:func:`deliver_reply_media` sends the file a turn's reply chose to the
patient, through the clinic's WhatsApp provider and after the reply's text
went out, at most once per session and media asset in
:data:`~src.repositories.media_deliveries.MEDIA_DELIVERY_REPEAT_WINDOW`.
Group 6 runs it, as a task of its own
(:mod:`src.services.reply_media_runner`), after the four inline webhook
replies and, as a step after ``_finish_delivered``, after the credit response
outbox's.

The checks run in this order and in no other (WISH Group 5, DESIGN Scope IN
5):

1. ``agent_media_send_enabled`` (AppConfig, fail-closed): ``flag_off``;
2. the asset, re-read in the turn's tenant -- deleted, of another tenant,
   no longer sent automatically (``auto_suggest`` off; an asset bound to no
   intent is still sent on the implied "Outras" turns), in a format the
   channel's provider does not send
   (:func:`~src.services.media_send_body.sendable_mime_types`, reason
   ``mime_not_sendable``) or whose first bytes its sender would refuse
   (:func:`~src.services.media_send_body.require_sendable_media` on
   :data:`REREAD_PREFIX_BYTES`: a non-mono OGG on Meta, reason
   ``ogg_opus_not_mono``): ``asset_gone``;
3. its category ceiling,
   :data:`~src.services.media_assets.MEDIA_CATEGORY_MAX_BYTES` (older assets
   of up to 50 MB still exist): ``too_large``;
4. one permit of the process's ``agent_sends`` pool, waited for in arrival
   order at most :data:`AGENT_MEDIA_PERMIT_WAIT_SECONDS` (60 s) -- none
   handed over in time: ``busy`` -- unless this process is already waiting
   for or delivering the same session and media pair, which answers
   ``deduplicated`` in silence, before any wait and with no write (see "In
   flight" below);
5. the credit mode, read without a ``Request``
   (:func:`~src.services.credit_inventory.read_credit_mode_without_request`)
   and, when enforced, the builder's suspension -- unavailable or suspended:
   ``credit_refused``;
6. the 24-hour claim -- taken within the window: ``deduplicated``, in
   silence;
7. the egress: off records nothing; observational records what it WOULD
   debit (:func:`~src.services.credit_inventory.record_observational_egress`);
   enforced charges it (:func:`~src.services.credit_inventory.charge_egress`,
   one server delivery id per send) -- ``credit_pending``:
   ``credit_refused``;
8. the provider's media sender, under ONE deadline per provider,
   :data:`REPLY_MEDIA_SEND_TIMEOUT_SECONDS` -- any failure: ``failed``;
   accepted: ``sent``;
9. ``record_sent_id`` with the provider's answer, so that its ``fromMe``
   echo is known as the agent's -- shielded from a cancellation of the
   caller, as ``_deliver_claimed`` shields its bookkeeping: once the
   provider answered, the message IS out.

Every outcome but ``sent`` and ``deduplicated`` sends the fixed notice
(:func:`reply_media_notice`, in the turn's language) through the same
provider, and its answer goes to ``record_sent_id`` too.  The text already
sent is never resent.

Transactions and connections.  The function owns its database work through
``session_factory``: every step is its own short session, closed before the
next one, so no transaction and no pooled connection is open across a
provider call (media or notice).  The flag is read before the first session.
Steps 1-5 write nothing in ``media_deliveries`` (step 5's suspension gate may
record the builder's lifecycle transition, as a download's does).  The claim
runs in a fresh transaction -- its ``now()`` is that transaction's start --
which COMMITS before step 7 whether it claimed or not: the claim locks the
pair's row until its transaction ends, also when it returns ``None``, and no
later step runs under that lock.  So a send whose claim did not commit never
leaves (DESIGN Scope IN 2), a concurrent turn of the same pair is answered
``deduplicated`` without waiting for this one's send, and a refusal at step
7 keeps the window consumed (accepted in the DESIGN; a charge is never
refunded either, like a download's).

Memory (Decision 9).  The asset's bytes are read only under the permit, at
step 8, in their own session closed before the sender runs; the permit is
released as soon as the media send ends, so neither the bookkeeping nor the
notice holds it.  Step 2 reads a bounded prefix of them only
(:data:`REREAD_PREFIX_BYTES`, by ``substring`` in the same query), before
step 4: a turn waiting for the permit holds that prefix and no payload, so
the wait costs no memory.

Waiting (owner, 2026-11-08: two patients of one worker both get their
file).  With ONE permit per process, a turn whose file another pair is
sending waits for that send to end, in arrival order behind the other
waiters (:class:`~src.api.middleware.media_limits.QueuedLimiter`), and
then sends; only when no permit is handed over within
:data:`AGENT_MEDIA_PERMIT_WAIT_SECONDS` is it ``busy``, with the notice.
The wait's length goes to the outcome log (``permit_wait_seconds``).

In flight.  Two turns of one pair in the same worker (a patient's two
messages a few seconds apart) would otherwise both reach the claim, or the
second would wait out the first one's send for nothing.  So the pairs past
step 3 are kept per ``agent_sends`` pool -- one pool per process -- added
BEFORE the wait, under the same lock as the check, and removed in the
``finally`` around the wait and the send, whatever ends them (sent, a
refusal or failure, ``busy`` after the wait, an error, a cancellation while
waiting or sending).  A pair already in flight -- waiting or sending -- is
``deduplicated`` (reason ``in_flight``), with no notice and no write;
another process's turn is still the claim's to refuse.

Worst case (for Group 6's bounds and lease budget).  The wait for the
permit is bounded (:data:`AGENT_MEDIA_PERMIT_WAIT_SECONDS`, 60 s); the
provider call by its deadline (Meta 250 s, the others 125 s); an accepted
file then has its record (:data:`REPLY_MEDIA_RECORD_TIMEOUT_SECONDS`, 30 s);
a refusal or failure has the notice
(:data:`REPLY_MEDIA_NOTICE_TIMEOUT_SECONDS`, 35 s) and its record (30 s).
So a call takes at most Meta 60 + 250 + 35 + 30 = 375 s, Z-API, uazapi and
Evolution 60 + 125 + 35 + 30 = 250 s (:data:`REPLY_MEDIA_MAX_SECONDS`),
PLUS the database steps, which have no bound of their own here.  An outer
bound below that cuts the wait, the notice or a record mid-way.
"""

from __future__ import annotations

import asyncio
import threading
import time
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal, Protocol, TypeVar, cast, get_args
from uuid import UUID, uuid4

import httpx
from sqlalchemy import LargeBinary, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.logging import get_logger
from src.models.media_asset import MediaAsset
from src.observability.metrics import AGENT_MEDIA_DELIVERY_TOTAL
from src.repositories import media_deliveries
from src.services import (
    credit_inventory,
    credit_lifecycle,
    egress_policy,
    meta_service,
    uazapi_service,
    zapi_service,
)
from src.services.api_credentials import MEDIA_DELIVERY_PROVIDERS, MediaDeliveryProvider
from src.services.credit_inventory import CreditMode
from src.services.credit_metering import (
    REPLY_RECORD_TIMEOUT_SECONDS,
    CreditEnforcementUnavailable,
    CreditTenantGone,
)
from src.services.media_assets import MEDIA_CATEGORY_MAX_BYTES
from src.services.media_send_body import (
    MEDIA_SEND_TIMEOUT,
    MediaCategory,
    require_sendable_media,
    sendable_mime_types,
)

logger = get_logger(__name__)
_T = TypeVar("_T")

# The closed outcome vocabulary of ``neurostack_agent_media_delivery_total``
# (DESIGN Scope IN 10, ``src/observability/metric_dimensions.py``).
MediaDeliveryOutcome = Literal[
    "sent",
    "deduplicated",
    "failed",
    "asset_gone",
    "too_large",
    "busy",
    "credit_refused",
    "flag_off",
]
# The outcomes after which the patient gets the fixed notice: the reply's
# text may already have said "segue o arquivo".  ``deduplicated`` is silent
# (the file went within the window); ``sent`` needs none.
NOTICE_OUTCOMES: Final[frozenset[str]] = frozenset(get_args(MediaDeliveryOutcome)) - {
    "sent",
    "deduplicated",
}


def _request_seconds(timeout: httpx.Timeout) -> float:
    """One request's worst case under an httpx timeout, counted as the
    publisher's derivation counts it: one connect + write + read."""

    assert timeout.connect is not None
    assert timeout.write is not None and timeout.read is not None
    return timeout.connect + timeout.write + timeout.read


# How many ``MEDIA_SEND_TIMEOUT`` requests one media send makes: Meta
# uploads, then sends; the others post one request.
_MEDIA_SEND_REQUESTS: Final[Mapping[str, int]] = MappingProxyType(
    {"meta": 2, "zapi": 1, "uazapi": 1, "evolution": 1}
)
# ONE deadline over the whole provider call of a media send, per provider.
# The senders' httpx timeout applies to each connect, read or write
# operation, so a peer that keeps trickling bytes could hold a request -- and
# the process's only ``agent_sends`` permit -- for ever.  The deadline is the
# senders' own healthy worst case, never less: with ``MEDIA_SEND_TIMEOUT``
# (60 s, connect 5 s) one request is 5 + 60 + 60 = 125 s, so Meta gets 250 s
# and Z-API, uazapi and Evolution 125 s.  Past it the send is cut, the
# outcome is ``failed`` and the permit is released.
REPLY_MEDIA_SEND_TIMEOUT_SECONDS: Final[Mapping[str, float]] = MappingProxyType(
    {
        provider: requests * _request_seconds(MEDIA_SEND_TIMEOUT)
        for provider, requests in _MEDIA_SEND_REQUESTS.items()
    }
)
# The notice is one text send: one connect + write + read of the text
# senders (5 + 15 + 15 s, as the publisher's derivation counts them).
REPLY_MEDIA_NOTICE_TIMEOUT_SECONDS: Final = 35.0
# The echo bookkeeping after a send, bounded like the outbox's (its failure
# only logs: the message IS delivered once the provider answered).
REPLY_MEDIA_RECORD_TIMEOUT_SECONDS: Final = REPLY_RECORD_TIMEOUT_SECONDS
# Step 4's bound on the wait for the process's only ``agent_sends`` permit
# (owner, 2026-11-08, amending Decision 9's never-waited ``try_acquire``):
# a second patient of the same worker waits, in arrival order, for the
# first one's send to end, then gets the file; only a turn to which no
# permit is handed over within this bound is ``busy``, with the notice.
# Raising the permits instead would put the task's memory worst case near
# 101 % (Decision 9); the wait costs none -- the bytes are read only under
# the permit, a waiter holds the 301-byte re-read prefix at most.  It does
# add to every call's worst case (:data:`REPLY_MEDIA_MAX_SECONDS`).  A
# constant, not a setting.
AGENT_MEDIA_PERMIT_WAIT_SECONDS: Final = 60
# A call's worst case per provider, the database steps excepted (they have
# no bound here), for Group 6's outer bounds: the wait for the permit, the
# send's deadline, then the notice and its record -- a failure at the
# deadline is the longest path (an accepted file has its record only, a
# ``busy`` turn no send).  Meta 60 + 250 + 35 + 30 = 375 s; Z-API, uazapi
# and Evolution 60 + 125 + 35 + 30 = 250 s.
REPLY_MEDIA_MAX_SECONDS: Final[Mapping[str, float]] = MappingProxyType(
    {
        provider: AGENT_MEDIA_PERMIT_WAIT_SECONDS
        + deadline
        + REPLY_MEDIA_NOTICE_TIMEOUT_SECONDS
        + REPLY_MEDIA_RECORD_TIMEOUT_SECONDS
        for provider, deadline in REPLY_MEDIA_SEND_TIMEOUT_SECONDS.items()
    }
)

# The fixed notice (DESIGN Scope IN 6).  ``en`` for an English turn, the
# Portuguese one for any other language, as the rest of the code base does.
REPLY_MEDIA_NOTICES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "pt": (
            "Não consegui enviar o arquivo agora. "
            "A equipe da clínica pode te enviar por aqui."
        ),
        "en": (
            "I couldn't send the file right now. "
            "The clinic team can send it to you here."
        ),
    }
)

DELIVERY_LOG: Final = "agent media delivery"

# The payload bytes step 2 reads with the asset, so that the sender's own
# pre-network check (``require_sendable_media``) runs BEFORE the claim and the
# charge.  The OpusHead locator
# (:func:`~src.services.media_assets.ogg_opus_identification_header`) needs
# the whole first-page header -- 27 bytes, then a segment table of up to 255
# -- and the full 19-byte OpusHead after it: 27 + 255 + 19 = 301.  Fewer
# bytes would refuse a mono file whose first page has a long segment table.
REREAD_PREFIX_BYTES: Final = 27 + 255 + 19


def reply_media_notice(response_language: str | None) -> str:
    """The fixed notice in the turn's language (any ``en*`` is English)."""

    language = (response_language or "pt-BR").strip().lower()
    return REPLY_MEDIA_NOTICES["en" if language.startswith("en") else "pt"]


# ---------------------------------------------------------------------------
# The inputs.
# ---------------------------------------------------------------------------


class SendMedia(Protocol):
    """A provider's media sender, bound to its credentials and the phone."""

    def __call__(
        self,
        payload: bytes,
        *,
        category: MediaCategory,
        mime_type: str,
        filename: str,
    ) -> Awaitable[Mapping[str, object]]: ...


class SendText(Protocol):
    """A provider's text sender, bound to its credentials and the phone."""

    def __call__(self, text: str) -> Awaitable[Mapping[str, object]]: ...


@dataclass(frozen=True)
class ReplyMediaChannel:
    """Where the delivery goes: the provider (the metric's label, a server
    parameter, never the payload's) and its two bound senders."""

    provider: MediaDeliveryProvider
    send_media: SendMedia
    send_text: SendText


# Receives the provider's answer of every message the delivery sends (the
# media and the notice) and records its id for the ``fromMe`` echo
# (``_track_sent_message`` / ``_track_meta_sent_message`` and
# ``_store_ai_msg_id_to_db``, in the route module).
RecordSentId = Callable[[Mapping[str, object]], Awaitable[None]]


class Permit(Protocol):
    def release(self) -> None: ...


class PermitPool(Protocol):
    """``MediaConcurrencyLimiters.agent_sends``: a pool whose callers wait for
    a permit, bounded and in arrival order
    (:class:`~src.api.middleware.media_limits.QueuedLimiter`).

    ``acquire(timeout)`` answers a permit, or ``None`` when none was handed
    over within ``timeout`` seconds; a cancellation of the waiter must leave
    no permit behind.  The pool must be hashable and weak-referenceable: the
    pairs in flight are kept in a :class:`weakref.WeakKeyDictionary` keyed by
    the pool (see ``_IN_FLIGHT``), so a pool with ``__slots__`` and no
    ``__weakref__``, or an unhashable one, would raise ``TypeError`` out of
    :func:`deliver_reply_media`.  ``QueuedLimiter`` is both.
    """

    async def acquire(self, timeout: float) -> Permit | None: ...


# The (session, media) pairs waiting for or holding a permit, per
# ``agent_sends`` pool: one pool per process, so each set is a process's (see
# "In flight").  Keyed by the pool, two pools never see each other's pairs
# (so the pool must be hashable and weak-referenceable, see ``PermitPool``).
# The lock keeps check-and-add whole across event-loop threads.  It is
# RE-ENTRANT on purpose, as ``QueuedLimiter``'s is: a delivery stranded on a
# loop that closed is closed by the garbage collector, and its ``finally``
# takes this lock; CPython 3.12 runs a due collection at the next
# eval-breaker check, which can fall inside a critical section of the same
# thread, where a plain ``Lock`` deadlocks.  Re-entering is safe: such a
# finalizer only discards its own pair, between two whole set operations.
_IN_FLIGHT: Final[weakref.WeakKeyDictionary[PermitPool, set[tuple[UUID, UUID]]]] = (
    weakref.WeakKeyDictionary()
)
_IN_FLIGHT_LOCK: Final = threading.RLock()


@dataclass(frozen=True)
class ReplyMediaResult:
    outcome: MediaDeliveryOutcome
    notice_sent: bool


def _credential(credentials: Mapping[str, Any], name: str) -> str:
    value = credentials.get(name)
    return value.strip() if isinstance(value, str) else ""


def _required(*values: str) -> None:
    if not all(values):
        raise ValueError("reply_media_credentials_incomplete")


def reply_media_channel(
    provider: str, credentials: Mapping[str, Any], phone: str
) -> ReplyMediaChannel:
    """Bind the provider's Group 3 media sender and its text sender to the
    tenant's raw ``api_credentials`` (the same keys the webhook's replies
    read) and the patient's phone.

    Pure, no I/O.  ``ValueError`` for a provider outside
    :data:`MEDIA_DELIVERY_PROVIDERS` or incomplete credentials, before any
    step runs: neither the file nor the notice could go.
    """

    if not phone:
        raise ValueError("reply_media_phone_missing")
    if provider == "meta":
        pnid = _credential(credentials, "meta_phone_number_id")
        meta_token = _credential(credentials, "meta_permanent_token")
        _required(pnid, meta_token)

        async def meta_media(
            payload: bytes, *, category: MediaCategory, mime_type: str, filename: str
        ) -> Mapping[str, object]:
            return await meta_service.send_meta_cloud_media(
                pnid,
                meta_token,
                phone,
                payload,
                category=category,
                mime_type=mime_type,
                filename=filename,
            )

        async def meta_text(text: str) -> Mapping[str, object]:
            return await meta_service.send_meta_cloud_text(
                pnid, meta_token, phone, text
            )

        return ReplyMediaChannel("meta", meta_media, meta_text)
    if provider == "zapi":
        instance = _credential(credentials, "zapi_instance_id")
        zapi_token = _credential(credentials, "zapi_token")
        client_token = _credential(credentials, "zapi_client_token")
        _required(instance, zapi_token)

        async def zapi_media(
            payload: bytes, *, category: MediaCategory, mime_type: str, filename: str
        ) -> Mapping[str, object]:
            return await zapi_service.send_zapi_media(
                instance,
                zapi_token,
                phone,
                payload,
                category=category,
                mime_type=mime_type,
                filename=filename,
                client_token=client_token,
            )

        async def zapi_text(text: str) -> Mapping[str, object]:
            return await zapi_service.send_text_message(
                instance, zapi_token, phone, text, client_token=client_token
            )

        return ReplyMediaChannel("zapi", zapi_media, zapi_text)
    if provider == "uazapi":
        url = _credential(credentials, "uazapi_api_url")
        uazapi_token = _credential(credentials, "uazapi_token")
        _required(url, uazapi_token)

        async def uazapi_media(
            payload: bytes, *, category: MediaCategory, mime_type: str, filename: str
        ) -> Mapping[str, object]:
            return await uazapi_service.send_uazapi_media(
                url,
                uazapi_token,
                phone,
                payload,
                category=category,
                mime_type=mime_type,
                filename=filename,
            )

        async def uazapi_text(text: str) -> Mapping[str, object]:
            return await uazapi_service.send_uazapi_text(url, uazapi_token, phone, text)

        return ReplyMediaChannel("uazapi", uazapi_media, uazapi_text)
    if provider == "evolution":
        evolution_url = _credential(credentials, "evolution_api_url")
        name = _credential(credentials, "evolution_instance_name")
        key = _credential(credentials, "evolution_api_key")
        _required(evolution_url, name, key)

        async def evolution_media(
            payload: bytes, *, category: MediaCategory, mime_type: str, filename: str
        ) -> Mapping[str, object]:
            return await zapi_service.send_evolution_media(
                evolution_url,
                name,
                key,
                phone,
                payload,
                category=category,
                mime_type=mime_type,
                filename=filename,
            )

        async def evolution_text(text: str) -> Mapping[str, object]:
            return await zapi_service.send_evolution_text(
                evolution_url, name, key, phone, text
            )

        return ReplyMediaChannel("evolution", evolution_media, evolution_text)
    raise ValueError("reply_media_provider_unknown")


# ---------------------------------------------------------------------------
# The delivery.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Asset:
    category: MediaCategory
    mime_type: str
    size_bytes: int
    original_filename: str
    # The first :data:`REREAD_PREFIX_BYTES` of the payload, never more.
    prefix: bytes


class _Refused(Exception):
    """A step ended the delivery with ``outcome`` (internal)."""

    def __init__(self, outcome: MediaDeliveryOutcome, reason: str) -> None:
        super().__init__(reason)
        self.outcome: MediaDeliveryOutcome = outcome
        self.reason = reason


async def deliver_reply_media(
    *,
    channel: ReplyMediaChannel,
    media_id: UUID,
    tenant_id: UUID,
    session_id: UUID,
    response_language: str,
    session_factory: Callable[[], AsyncSession],
    agent_sends: PermitPool,
    record_sent_id: RecordSentId,
    credit_switch_on: bool | None = None,
) -> ReplyMediaResult:
    """Deliver the turn's file once, in the module's nine steps; never raises
    except a cancellation of the caller (the permit is released then too).

    Call it only after the provider accepted the reply's text.  ``media_id``
    is the turn's attachment (all the outbox payload carries); the asset is
    re-read in the turn's tenant (step 2) and what is read is sent.
    ``session_factory`` opens the delivery's own sessions; the caller's
    session, if any, is never used.

    The caller must hold NO pooled connection across the call: commit (or
    roll back) and close its session first -- a ``flush()`` alone keeps the
    connection checked out.  The call can wait up to
    :data:`AGENT_MEDIA_PERMIT_WAIT_SECONDS` for the process's permit and then
    send for up to the provider's deadline, at most
    :data:`REPLY_MEDIA_MAX_SECONDS` for its provider plus the database
    steps; every turn queued behind the permit would keep its caller's
    connection that long, and a few of them exhaust the worker's pool, which
    the delivery's own steps then wait on.  A caller's open transaction
    could also hold a lock the claim waits on.

    ``credit_switch_on`` is the switch the caller's request captured at
    entry, when it has one; ``None`` reads the process-wide verdict now (the
    outbox publisher).
    """

    fields = {
        "provider": channel.provider,
        "tenant_id": str(tenant_id),
        "session_id": str(session_id),
        "media_id": str(media_id),
    }
    sent: Mapping[str, object] | None = None
    # The outcome log's measurements (``permit_wait_seconds`` once step 4
    # waited, ``busy`` included).
    measured: dict[str, float] = {}
    try:
        sent = await _deliver(
            channel=channel,
            media_id=media_id,
            tenant_id=tenant_id,
            session_id=session_id,
            session_factory=session_factory,
            agent_sends=agent_sends,
            credit_switch_on=credit_switch_on,
            measured=measured,
        )
        outcome: MediaDeliveryOutcome = "sent"
        reason = "sent"
    except _Refused as refused:
        outcome, reason = refused.outcome, refused.reason
    AGENT_MEDIA_DELIVERY_TOTAL.labels(provider=channel.provider, outcome=outcome).inc()
    logger.info(DELIVERY_LOG, outcome=outcome, reason=reason, **fields, **measured)
    if sent is not None:
        await _shielded(_record(record_sent_id, sent, "media", fields))  # step 9
        return ReplyMediaResult(outcome, notice_sent=False)
    if outcome not in NOTICE_OUTCOMES:
        return ReplyMediaResult(outcome, notice_sent=False)
    notice_sent = await _send_notice(channel, response_language, record_sent_id, fields)
    return ReplyMediaResult(outcome, notice_sent=notice_sent)


async def _deliver(
    *,
    channel: ReplyMediaChannel,
    media_id: UUID,
    tenant_id: UUID,
    session_id: UUID,
    session_factory: Callable[[], AsyncSession],
    agent_sends: PermitPool,
    credit_switch_on: bool | None,
    measured: dict[str, float],
) -> Mapping[str, object]:
    """Steps 1-8; the provider's answer, or :class:`_Refused`."""

    # 1. The flag, before any session (no transaction open, no media SQL).
    # The helper never raises but for a cancellation; fail closed anyway.
    try:
        enabled = await egress_policy.agent_media_send_enabled()
    except Exception as error:
        raise _Refused("flag_off", f"flag_unreadable:{type(error).__name__}") from None
    if enabled is not True:
        raise _Refused("flag_off", "flag_off")
    # 2. The asset, re-read in the turn's tenant (a bounded prefix of its
    # bytes, never the payload).
    asset = await _step("asset_read", _read_asset(session_factory, tenant_id, media_id))
    if asset is None:
        raise _Refused("asset_gone", "asset_gone")
    # A format the provider's sender would refuse: no longer deliverable on
    # this channel, as an asset no longer automatic is -- refused here,
    # before the permit and the claim, so it costs no window and no charge
    # (the selection already leaves such files out; this is the re-read).
    if asset.mime_type not in sendable_mime_types(channel.provider):
        raise _Refused("asset_gone", "mime_not_sendable")
    # What the sender would refuse before the network on these first bytes
    # (a non-mono OGG on Meta, or no bytes at all): refused here too, so a
    # file stored without the upload's refusal (the bridge image accepts
    # stereo OGG) is never claimed nor charged.  The reason is the check's.
    try:
        require_sendable_media(
            channel.provider, asset.category, asset.mime_type, asset.prefix
        )
    except ValueError as refused:
        raise _Refused("asset_gone", str(refused)) from None
    # 3. The category ceiling.
    ceiling = MEDIA_CATEGORY_MAX_BYTES.get(asset.category)
    if ceiling is None or asset.size_bytes > ceiling:
        raise _Refused("too_large", "too_large")
    # 4. One permit, waited for in arrival order at most
    # AGENT_MEDIA_PERMIT_WAIT_SECONDS -- unless the pair is in flight here.
    # The pair is in flight from ``_enter_in_flight`` on; no ``await``
    # separates it from the ``try`` whose ``finally`` removes it.
    pair = (session_id, media_id)
    _enter_in_flight(agent_sends, pair)
    permit: Permit | None = None
    try:
        permit = await _wait_for_permit(agent_sends, measured)
        return await _deliver_with_permit(
            channel=channel,
            asset=asset,
            media_id=media_id,
            tenant_id=tenant_id,
            session_id=session_id,
            session_factory=session_factory,
            credit_switch_on=credit_switch_on,
        )
    finally:
        try:
            with _IN_FLIGHT_LOCK:
                _IN_FLIGHT[agent_sends].discard(pair)
        finally:
            if permit is not None:
                permit.release()


def _enter_in_flight(agent_sends: PermitPool, pair: tuple[UUID, UUID]) -> None:
    """Step 4, first: a pair this process is already waiting for or sending
    is ``deduplicated``, in silence, before any wait and with no write;
    otherwise the pair is in flight -- its wait included -- until the
    caller's ``finally``."""

    with _IN_FLIGHT_LOCK:
        in_flight = _IN_FLIGHT.setdefault(agent_sends, set())
        if pair in in_flight:
            raise _Refused("deduplicated", "in_flight")
        in_flight.add(pair)


async def _wait_for_permit(
    agent_sends: PermitPool, measured: dict[str, float]
) -> Permit:
    """Step 4, then: the permit, waited for in arrival order at most
    :data:`AGENT_MEDIA_PERMIT_WAIT_SECONDS`; none handed over in time:
    ``busy``.  A cancellation leaves the pool's queue with no permit held
    (the pool's contract) and propagates."""

    started = time.monotonic()
    permit = await agent_sends.acquire(AGENT_MEDIA_PERMIT_WAIT_SECONDS)
    measured["permit_wait_seconds"] = round(time.monotonic() - started, 3)
    if permit is None:
        raise _Refused("busy", "busy")
    return permit


async def _deliver_with_permit(
    *,
    channel: ReplyMediaChannel,
    asset: _Asset,
    media_id: UUID,
    tenant_id: UUID,
    session_id: UUID,
    session_factory: Callable[[], AsyncSession],
    credit_switch_on: bool | None,
) -> Mapping[str, object]:
    # 5. The credit mode (and, enforced, the suspension).
    mode = await _credit_mode(session_factory, tenant_id, credit_switch_on)
    # 6. The claim, committed before anything else happens.
    claimed = await _step(
        "claim", _claim(session_factory, tenant_id, session_id, media_id)
    )
    if claimed is None:
        raise _Refused("deduplicated", "deduplicated")
    # 7. The egress.
    await _egress(session_factory, mode, tenant_id, media_id, asset.size_bytes)
    # 8. The bytes (under the permit), then the sender under one deadline.
    payload = await _step(
        "payload_read", _read_payload(session_factory, tenant_id, media_id)
    )
    if payload is None:
        raise _Refused("asset_gone", "asset_gone_before_send")
    try:
        async with asyncio.timeout(REPLY_MEDIA_SEND_TIMEOUT_SECONDS[channel.provider]):
            return await channel.send_media(
                payload,
                category=asset.category,
                mime_type=asset.mime_type,
                filename=asset.original_filename,
            )
    except TimeoutError:
        raise _Refused("failed", "send_timeout") from None
    except Exception as error:
        # The senders' errors are sanitized; the type is all that is logged.
        raise _Refused("failed", f"send_failed:{type(error).__name__}") from None


async def _step(name: str, work: Awaitable[_T]) -> _T:
    """A database step that cannot decide (an error): ``failed``."""

    try:
        return await work
    except _Refused:
        raise
    except Exception as error:
        raise _Refused("failed", f"{name}_failed:{type(error).__name__}") from None


async def _read_asset(
    session_factory: Callable[[], AsyncSession], tenant_id: UUID, media_id: UUID
) -> _Asset | None:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(
                    MediaAsset.category,
                    MediaAsset.mime_type,
                    MediaAsset.size_bytes,
                    MediaAsset.original_filename,
                    MediaAsset.auto_suggest,
                    func.substring(
                        MediaAsset.payload, 1, REREAD_PREFIX_BYTES, type_=LargeBinary
                    ).label("prefix"),
                ).where(MediaAsset.id == media_id, MediaAsset.tenant_id == tenant_id)
            )
        ).one_or_none()
    if row is None or row.auto_suggest is not True:
        return None
    return _Asset(
        category=cast(MediaCategory, row.category),
        mime_type=row.mime_type,
        size_bytes=int(row.size_bytes),
        original_filename=row.original_filename,
        prefix=bytes(row.prefix or b""),
    )


async def _read_payload(
    session_factory: Callable[[], AsyncSession], tenant_id: UUID, media_id: UUID
) -> bytes | None:
    async with session_factory() as session:
        payload = await session.scalar(
            select(MediaAsset.payload).where(
                MediaAsset.id == media_id, MediaAsset.tenant_id == tenant_id
            )
        )
    return payload


async def _credit_mode(
    session_factory: Callable[[], AsyncSession],
    tenant_id: UUID,
    credit_switch_on: bool | None,
) -> CreditMode:
    # Opening and closing the session are inside the ``try`` too: the
    # delivery never raises (a cancellation is not an ``Exception``).
    try:
        async with session_factory() as session:
            try:
                mode = await credit_inventory.read_credit_mode_without_request(
                    session, switch_on=credit_switch_on
                )
            except CreditEnforcementUnavailable:
                raise _Refused(
                    "credit_refused", "credit_enforcement_unavailable"
                ) from None
            except Exception as error:
                raise _Refused(
                    "credit_refused", f"credit_mode_failed:{type(error).__name__}"
                ) from None
            if not mode.enforced:
                return mode
            try:
                await session.rollback()  # end the read before the gate's locks
                # The gate may record the builder's transition: committed
                # either way, as by the download route.
                suspended = await credit_lifecycle.tenant_suspended(session, tenant_id)
                await session.commit()
            except Exception as error:
                raise _Refused(
                    "credit_refused", f"suspension_unreadable:{type(error).__name__}"
                ) from None
    except _Refused:
        raise
    except Exception as error:
        raise _Refused(
            "credit_refused", f"credit_mode_failed:{type(error).__name__}"
        ) from None
    if suspended:
        raise _Refused("credit_refused", "tenant_suspended")
    return mode


async def _claim(
    session_factory: Callable[[], AsyncSession],
    tenant_id: UUID,
    session_id: UUID,
    media_id: UUID,
) -> UUID | None:
    """The claim in a fresh transaction, committed on return -- ``None``
    included, which ends its row lock too."""

    async with session_factory() as session:
        async with session.begin():
            return await media_deliveries.claim_media_delivery(
                session, tenant_id=tenant_id, session_id=session_id, media_id=media_id
            )


async def _egress(
    session_factory: Callable[[], AsyncSession],
    mode: CreditMode,
    tenant_id: UUID,
    media_id: UUID,
    size_bytes: int,
) -> None:
    if mode.observational:
        # Best effort in its own session and budget; it never raises, and a
        # failure to record never changes the delivery (main's answer).
        await credit_inventory.record_observational_egress(
            None,
            tenant_id=tenant_id,
            media_id=media_id,
            size_bytes=size_bytes,
            session_factory=session_factory,
        )
        return
    if not mode.enforced:
        return
    assert mode.enforcement_at is not None
    try:
        async with session_factory() as session:
            charged = await credit_inventory.charge_egress(
                session,
                tenant_id=tenant_id,
                media_id=media_id,
                size_bytes=size_bytes,
                enforcement_at=mode.enforcement_at,
                # One delivery per send (owner decision F): never the claim's
                # row id, which every send of the pair shares.
                delivery_id=uuid4(),
            )
            if charged.status == "credit_pending":
                await session.rollback()
                raise _Refused("credit_refused", "credit_pending")
            await session.commit()
    except _Refused:
        raise
    except CreditTenantGone:
        raise _Refused("asset_gone", "tenant_gone") from None
    except Exception as error:
        raise _Refused(
            "credit_refused", f"egress_failed:{type(error).__name__}"
        ) from None


async def _record(
    record_sent_id: RecordSentId,
    sent: Mapping[str, object],
    kind: Literal["media", "notice"],
    fields: Mapping[str, str],
) -> None:
    try:
        async with asyncio.timeout(REPLY_MEDIA_RECORD_TIMEOUT_SECONDS):
            await record_sent_id(sent)
    except Exception as error:
        logger.warning(
            DELIVERY_LOG,
            alert="agent_media_record",
            reason=f"{kind}_record_failed",
            error_type=type(error).__name__,
            **fields,
        )


async def _shielded(bookkeeping: Awaitable[None]) -> None:
    """Run the bookkeeping of a message the provider accepted to its end,
    shielded from a cancellation of the caller (as ``_deliver_claimed``
    does), and only then let that cancellation through.  The bookkeeping
    has its own bound (:func:`_record`), so the wait is bounded too."""

    task = asyncio.ensure_future(bookkeeping)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        raise


async def _send_notice(
    channel: ReplyMediaChannel,
    response_language: str,
    record_sent_id: RecordSentId,
    fields: Mapping[str, str],
) -> bool:
    try:
        async with asyncio.timeout(REPLY_MEDIA_NOTICE_TIMEOUT_SECONDS):
            sent = await channel.send_text(reply_media_notice(response_language))
    except Exception as error:
        logger.warning(
            DELIVERY_LOG,
            alert="agent_media_notice",
            reason="notice_failed",
            error_type=type(error).__name__,
            **fields,
        )
        return False
    await _shielded(_record(record_sent_id, sent, "notice", fields))
    return True


__all__ = [
    "AGENT_MEDIA_PERMIT_WAIT_SECONDS",
    "MEDIA_DELIVERY_PROVIDERS",
    "NOTICE_OUTCOMES",
    "REPLY_MEDIA_MAX_SECONDS",
    "REPLY_MEDIA_NOTICES",
    "REPLY_MEDIA_NOTICE_TIMEOUT_SECONDS",
    "REPLY_MEDIA_RECORD_TIMEOUT_SECONDS",
    "REPLY_MEDIA_SEND_TIMEOUT_SECONDS",
    "REREAD_PREFIX_BYTES",
    "MediaDeliveryOutcome",
    "MediaDeliveryProvider",
    "PermitPool",
    "RecordSentId",
    "ReplyMediaChannel",
    "ReplyMediaResult",
    "deliver_reply_media",
    "reply_media_channel",
    "reply_media_notice",
]
