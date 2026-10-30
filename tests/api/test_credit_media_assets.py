"""Media reservation, per-type limits and egress (Group 4a), SQLite.

The real application (lifespan, middleware, SQLite ``:memory:``) with the
credit switch forced per test.  What is pinned here:

* mídia uses ``media_assets`` (no new table or route);
* with the switch off, or on before ``credit_enforcement_at``, uploads and
  downloads answer exactly like ``main`` -- ``main``'s 50 MB limit and
  allowlist; M4A/OGG-Opus stay ``media_type_unsupported`` (deferred, owner
  decision A) -- and, switch on, are measured observationally; an injected
  writer failure changes nothing but a log;
* after the enforcement the PDF's per-type limits (image 5 MB, PDF 20 MB,
  audio/video 16 MB) refuse before anything is reserved; an accepted upload
  debits its media reservation in its transaction; a standalone tenant or a
  quarantine hold is 402 ``credit_pending`` with nothing stored;
* a download counts on every request (server delivery id, owner decision
  F) and its overage is charged BEFORE the answer (owner decision G): a hold
  or a standalone tenant is 402 before any byte.

SQLite has no ``pg_advisory_xact_lock``: the capacity guard's advisory lock
is skipped here (only that statement; every other lock and check of the
guard runs).  The real guard with the reservation, in its lock order, runs
on PostgreSQL in ``tests/integration/test_credit_maintenance_postgres.py``
and in the dominance oracle.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from src.api.middleware.auth import pwd_context
from src.api.routes import media_assets as media_routes
from src.models.billing import BillingTopupOrder
from src.models.builder import Builder
from src.models.credit import (
    CreditAccount,
    CreditEnforcementState,
    CreditLedgerEntry,
    CreditObservationalEffect,
)
from src.models.credit_inventory import (
    CreditAllocation,
    CreditEgressDelivery,
    CreditInventoryVersion,
)
from src.models.media_asset import MediaAsset
from src.models.tenant import Tenant
from src.repositories.credit_inventory import CreditInventoryRepository
from src.repositories.database import get_db_session
from src.services import credit_inventory, credit_metering
from src.services.credit_switch import CreditSwitch
from src.services.media_assets import MediaStorageCapacityGuard

MEDIA = "/v1/media-assets"
_FINANCIAL = (
    CreditAccount,
    CreditAllocation,
    CreditInventoryVersion,
    CreditLedgerEntry,
)


def _png(size: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * (size - 8)


def _pdf(size: int) -> bytes:
    return b"%PDF-1.7\n" + b"0" * (size - 9)


def _mp3(size: int) -> bytes:
    return b"\xff\xfb\x90\x64" + b"\x00" * (size - 4)


def _mp4(size: int) -> bytes:
    header = b"\x00\x00\x00\x10ftypisom\x00\x00\x02\x00"
    return header + b"\x00" * (size - len(header))


class World:
    def __init__(self) -> None:
        self.builder = uuid4()
        self.tenant = uuid4()
        self.key = secrets.token_urlsafe(24)
        self.standalone = uuid4()
        self.standalone_key = secrets.token_urlsafe(24)

    def headers(self, standalone: bool = False) -> dict[str, str]:
        return {"X-API-Key": self.standalone_key if standalone else self.key}


async def _seed(world: World) -> None:
    session = get_db_session()
    try:
        async with session.begin():
            session.add(Builder(id=world.builder, name="Media builder"))
            await session.flush()
            for tenant_id, builder, key in (
                (world.tenant, world.builder, world.key),
                (world.standalone, None, world.standalone_key),
            ):
                session.add(
                    Tenant(
                        id=tenant_id,
                        name=f"Media {tenant_id.hex[:6]}",
                        slug=f"media-{tenant_id.hex}",
                        api_key_hash=pwd_context.hash(key),
                        builder_id=builder,
                        config={},
                    )
                )
    finally:
        await session.close()


async def _enforce() -> None:
    at = datetime.now(timezone.utc) - timedelta(days=90)
    session = get_db_session()
    try:
        async with session.begin():
            session.add(
                CreditEnforcementState(
                    id=1, credit_enforcement_at=at, set_by="t", set_at=at
                )
            )
    finally:
        await session.close()


async def _hold(builder: UUID) -> None:
    session = get_db_session()
    try:
        async with session.begin():
            session.add(
                BillingTopupOrder(
                    id=uuid4(),
                    builder_id=builder,
                    payment_method="pix",
                    catalog_version_id="payment-2026-10-03",
                    offer_id="package_credits_500_v1",
                    amount_cents=10_000,
                    credit_grant_mc=500_000,
                    pricing_snapshot={},
                    financial_snapshot={},
                    state="quarantined",
                )
            )
    finally:
        await session.close()


async def _rows(model: Any, *conditions: Any) -> list[Any]:
    session = get_db_session()
    try:
        return list((await session.execute(select(model).where(*conditions))).scalars())
    finally:
        await session.close()


@pytest.fixture(autouse=True)
def _sqlite_capacity_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    original = MediaStorageCapacityGuard.__aenter__

    async def enter(self: MediaStorageCapacityGuard) -> MediaStorageCapacityGuard:
        session = self._session
        execute = session.execute

        async def without_advisory_lock(
            statement: Any, *args: Any, **kwargs: Any
        ) -> Any:
            if "pg_advisory_xact_lock" in str(statement):
                return None
            return await execute(statement, *args, **kwargs)

        # This request's own session only (never a shared singleton).
        session.execute = without_advisory_lock  # type: ignore[method-assign]
        try:
            return await original(self)
        finally:
            del session.execute

    monkeypatch.setattr(MediaStorageCapacityGuard, "__aenter__", enter)


@pytest.fixture
def world(client: Any) -> World:
    seeded = World()
    client._submit(_seed(seeded))
    return seeded


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    def force(effective: bool) -> None:
        monkeypatch.setattr(CreditSwitch, "effective", lambda _self: effective)

    force(True)
    return force


@pytest.fixture(autouse=True)
def _fresh_process_state() -> Iterator[None]:
    credit_inventory.forget_enforcement_seen()
    credit_metering.forget_enforcement_seen()
    yield
    credit_inventory.forget_enforcement_seen()
    credit_metering.forget_enforcement_seen()


@pytest.fixture
def writer_outcomes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    outcomes: list[str] = []
    for name in ("record_observational_upload", "record_observational_egress"):
        original = getattr(credit_inventory, name)

        def wrap(original: Any, name: str) -> Any:
            async def recorded(*args: Any, **kwargs: Any) -> Any:
                kwargs.setdefault("budget_seconds", 5)
                outcome = await original(*args, **kwargs)
                outcomes.append(f"{name.rsplit('_', 1)[-1]} {outcome}")
                return outcome

            return recorded

        monkeypatch.setattr(credit_inventory, name, wrap(original, name))
    return outcomes


def _upload(
    client: Any,
    world: World,
    payload: bytes,
    mime: str,
    *,
    standalone: bool = False,
    name: str = "arquivo",
) -> Any:
    return client.post(
        MEDIA,
        files={"file": (name, payload, mime)},
        data={"display_name": "Arquivo", "original_filename": name},
        headers=world.headers(standalone),
    )


def _comparable(response: Any) -> tuple[int, Any]:
    body = response.json()
    if isinstance(body, dict):
        body = {
            key: value
            for key, value in body.items()
            if key not in {"id", "created_at", "updated_at"}
        }
    return response.status_code, body


# ---------------------------------------------------------------------------
# Before the enforcement: main's answers, observational measurement.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "mime"),
    [
        (_png(6_000_000), "image/png"),  # above the PDF's 5 MB, within main's
        (_pdf(21_000_000), "application/pdf"),
        (_mp3(17_000_000), "audio/mpeg"),
        (b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 64, "audio/mp4"),
        (b"OggS" + b"\x00" * 64, "audio/ogg"),
    ],
    ids=["png-6mb", "pdf-21mb", "mp3-17mb", "m4a", "ogg-opus"],
)
def test_before_the_enforcement_uploads_answer_like_main(
    client: Any,
    world: World,
    switch: Callable[[bool], None],
    writer_outcomes: list[str],
    payload: bytes,
    mime: str,
) -> None:
    switch(False)
    off = _upload(client, world, payload, mime)
    switch(True)
    on = _upload(client, world, payload, mime)
    assert _comparable(on) == _comparable(off)
    if mime in ("audio/mp4", "audio/ogg"):
        # Deferred (owner decision A): main's answer, at any time.
        assert (on.status_code, on.json()) == (
            422,
            {"detail": {"code": "media_type_unsupported"}},
        )
        assert writer_outcomes == []
    else:
        assert on.status_code == 201
        assert writer_outcomes == ["upload recorded"]
        [effect] = client._submit(
            _rows(
                CreditObservationalEffect,
                CreditObservationalEffect.effect_kind == "media",
            )
        )
        assert effect.effect_id == f"media:{world.tenant}:{on.json()['id']}:reserve"
        assert effect.would_debit_mc > 0
    for model in _FINANCIAL:
        assert client._submit(_rows(model)) == []


def test_downloads_count_every_request_observationally(
    client: Any,
    world: World,
    switch: Callable[[bool], None],
    writer_outcomes: list[str],
) -> None:
    created = _upload(client, world, _pdf(1_000), "application/pdf")
    media_id = created.json()["id"]
    first = client.get(f"{MEDIA}/{media_id}/content", headers=world.headers())
    second = client.get(f"{MEDIA}/{media_id}/content", headers=world.headers())
    switch(False)
    off = client.get(f"{MEDIA}/{media_id}/content", headers=world.headers())
    for response in (first, second):
        assert response.status_code == off.status_code == 200
        assert response.content == off.content == _pdf(1_000)
        assert (
            response.headers["content-disposition"]
            == off.headers["content-disposition"]
        )
    deliveries = client._submit(_rows(CreditEgressDelivery))
    assert len(deliveries) == 2 and len({d.delivery_id for d in deliveries}) == 2
    assert {(d.mode, d.bytes) for d in deliveries} == {("observational", 1_000)}
    assert writer_outcomes.count("egress recorded") == 2
    for model in _FINANCIAL:
        assert client._submit(_rows(model)) == []


def test_an_injected_writer_failure_changes_neither_answer_nor_data(
    client: Any,
    world: World,
    switch: Callable[[bool], None],
    writer_outcomes: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(_self: Any, builder_id: UUID) -> Any:
        raise RuntimeError("trigger: append-only")

    alerts: list[dict[str, Any]] = []

    class _Logger:
        def warning(self, _message: str, **fields: Any) -> None:
            alerts.append(fields)

        def info(self, _message: str, **_fields: Any) -> None:
            pass

    monkeypatch.setattr(CreditInventoryRepository, "builder_sources", broken)
    monkeypatch.setattr(credit_inventory, "logger", _Logger())
    created = _upload(client, world, _pdf(1_000), "application/pdf")
    assert created.status_code == 201
    download = client.get(
        f"{MEDIA}/{created.json()['id']}/content", headers=world.headers()
    )
    assert (download.status_code, download.content) == (200, _pdf(1_000))
    assert writer_outcomes == ["upload failed", "egress failed"]
    # Logged with the observational-write alert, each time.
    assert [
        (fields["alert"], fields["reason"], fields["source"], fields["error_type"])
        for fields in alerts
    ] == [
        ("credit_observational_write", "failed", "media_upload", "RuntimeError"),
        ("credit_observational_write", "failed", "media_egress", "RuntimeError"),
    ]
    assert len(client._submit(_rows(MediaAsset))) == 1
    assert client._submit(_rows(CreditObservationalEffect)) == []
    assert client._submit(_rows(CreditEgressDelivery)) == []


# ---------------------------------------------------------------------------
# After the enforcement.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "mime"),
    [
        (_png(5_000_001), "image/png"),
        (_pdf(20_000_001), "application/pdf"),
        (_mp3(16_000_001), "audio/mpeg"),
        (_mp4(16_000_001), "video/mp4"),
    ],
    ids=["png", "pdf", "mp3", "mp4"],
)
def test_after_the_enforcement_the_per_type_limits_refuse_before_reserving(
    client: Any, world: World, switch: Callable[[bool], None], payload: bytes, mime: str
) -> None:
    client._submit(_enforce())
    response = _upload(client, world, payload, mime)
    assert (response.status_code, response.json()) == (
        413,
        {"detail": {"code": "media_size_exceeded"}},
    )
    assert client._submit(_rows(MediaAsset)) == []
    for model in _FINANCIAL:
        assert client._submit(_rows(model)) == []


def test_after_the_enforcement_m4a_and_ogg_stay_unsupported(
    client: Any, world: World, switch: Callable[[bool], None]
) -> None:
    client._submit(_enforce())
    for payload, mime in (
        (b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 64, "audio/mp4"),
        (b"OggS" + b"\x00" * 64, "audio/ogg"),
    ):
        response = _upload(client, world, payload, mime)
        assert (response.status_code, response.json()) == (
            422,
            {"detail": {"code": "media_type_unsupported"}},
        )
    for model in _FINANCIAL:
        assert client._submit(_rows(model)) == []


def test_after_the_enforcement_an_upload_reserves_its_media_once(
    client: Any, world: World, switch: Callable[[bool], None]
) -> None:
    client._submit(_enforce())
    first = _upload(client, world, _png(5_000_000), "image/png")
    second = _upload(client, world, _mp4(16_000_000), "video/mp4")
    assert (first.status_code, second.status_code) == (201, 201)
    reservations = sorted(
        (row.target_mc, row.charged_mc, row.effect_id)
        for row in client._submit(_rows(CreditAllocation))
        if row.allocation_kind == "reservation"
    )
    # 5 MB -> 250 mc; 21 MB in total -> 1,050 mc: the second pays 800.
    assert [(target, charged) for target, charged, _ in reservations] == [
        (250, 250),
        (1_050, 800),
    ]
    assert {effect for _, _, effect in reservations} == {
        f"media:{world.tenant}:{first.json()['id']}:reserve",
        f"media:{world.tenant}:{second.json()['id']}:reserve",
    }
    entries = client._submit(_rows(CreditLedgerEntry))
    assert sorted((e.entry_type, e.amount_mc) for e in entries) == [
        ("debt", 250),
        ("debt", 800),
    ]
    assert client._submit(_rows(CreditObservationalEffect)) == []


def test_after_the_enforcement_standalone_or_hold_is_credit_pending(
    client: Any, world: World, switch: Callable[[bool], None]
) -> None:
    client._submit(_enforce())
    pending = {"detail": {"code": "credit_pending"}}
    standalone = _upload(client, world, _pdf(1_000), "application/pdf", standalone=True)
    assert (standalone.status_code, standalone.json()) == (402, pending)
    client._submit(_hold(world.builder))
    held = _upload(client, world, _pdf(1_000), "application/pdf")
    assert (held.status_code, held.json()) == (402, pending)
    assert client._submit(_rows(MediaAsset)) == []
    assert client._submit(_rows(CreditAllocation)) == []
    assert client._submit(_rows(CreditLedgerEntry)) == []


def test_after_the_enforcement_a_download_is_charged_before_its_answer(
    client: Any, world: World, switch: Callable[[bool], None]
) -> None:
    client._submit(_enforce())
    created = _upload(client, world, _pdf(1_000), "application/pdf")
    media_id = created.json()["id"]
    path = f"{MEDIA}/{media_id}/content"
    first = client.get(path, headers=world.headers())
    second = client.get(path, headers=world.headers())
    assert [r.status_code for r in (first, second)] == [200, 200]
    assert first.content == second.content == _pdf(1_000)
    deliveries = client._submit(_rows(CreditEgressDelivery))
    assert {(d.mode, d.bytes) for d in deliveries} == {("enforced", 1_000)}
    assert len(deliveries) == 2
    # 2 KB served against 200 KB included by 1 mc of media: no overage.
    assert [
        row
        for row in client._submit(_rows(CreditAllocation))
        if row.category == "egress"
    ] == []
    client._submit(_hold(world.builder))
    held = client.get(path, headers=world.headers())
    assert (held.status_code, held.json()) == (
        402,
        {"detail": {"code": "credit_pending"}},
    )
    assert len(client._submit(_rows(CreditEgressDelivery))) == 2


def _instrument_download(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Records, in order, every egress charge the download route starts and
    finishes and the first byte of every answer -- with the enforced
    deliveries already COMMITTED when that byte is produced."""

    events: list[tuple[str, int]] = []
    original_charge = credit_inventory.charge_egress
    original_chunk = media_routes._single_chunk

    async def charge(*args: Any, **kwargs: Any) -> Any:
        events.append(("charging", 0))
        outcome = await original_charge(*args, **kwargs)
        events.append(("charged", 0))
        return outcome

    async def chunk(payload: bytes) -> AsyncIterator[bytes]:
        committed = await _rows(
            CreditEgressDelivery, CreditEgressDelivery.mode == "enforced"
        )
        events.append(("byte", len(committed)))
        async for part in original_chunk(payload):
            yield part

    monkeypatch.setattr(credit_inventory, "charge_egress", charge)
    monkeypatch.setattr(media_routes, "_single_chunk", chunk)
    return events


def test_the_egress_is_committed_before_the_first_byte_and_a_hold_sends_none(
    client: Any,
    world: World,
    switch: Callable[[bool], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner decision G: the charge runs and commits BEFORE the answer
    produces a byte; a hold is 402 before any byte."""

    client._submit(_enforce())
    created = _upload(client, world, _pdf(1_000), "application/pdf")
    path = f"{MEDIA}/{created.json()['id']}/content"
    events = _instrument_download(monkeypatch)
    assert client.get(path, headers=world.headers()).content == _pdf(1_000)
    assert client.get(path, headers=world.headers()).status_code == 200
    assert events == [
        ("charging", 0),
        ("charged", 0),
        ("byte", 1),
        ("charging", 0),
        ("charged", 0),
        ("byte", 2),
    ]
    events.clear()
    client._submit(_hold(world.builder))
    held = client.get(path, headers=world.headers())
    assert (held.status_code, held.json()) == (
        402,
        {"detail": {"code": "credit_pending"}},
    )
    assert events == [("charging", 0), ("charged", 0)]  # no byte
    assert len(client._submit(_rows(CreditEgressDelivery))) == 2


def test_a_failed_egress_charge_sends_no_byte_and_records_nothing(
    client: Any,
    world: World,
    switch: Callable[[bool], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client._submit(_enforce())
    created = _upload(client, world, _pdf(1_000), "application/pdf")
    path = f"{MEDIA}/{created.json()['id']}/content"
    events = _instrument_download(monkeypatch)

    async def broken(_self: Any, **_kwargs: Any) -> None:
        raise OperationalError("INSERT", {}, Exception("disk I/O error"))

    monkeypatch.setattr(CreditInventoryRepository, "insert_delivery", broken)
    response = client.get(path, headers=world.headers())
    assert (response.status_code, response.json()) == (
        503,
        {"detail": {"code": "media_storage_unavailable"}},
    )
    assert events == [("charging", 0)]  # the charge failed: no byte
    assert client._submit(_rows(CreditEgressDelivery)) == []
    assert [
        row
        for row in client._submit(_rows(CreditAllocation))
        if row.category == "egress"
    ] == []


@pytest.mark.parametrize("enforced", [False, True], ids=["observational", "enforced"])
def test_a_client_request_id_never_names_a_delivery(
    client: Any,
    world: World,
    switch: Callable[[bool], None],
    writer_outcomes: list[str],
    enforced: bool,
) -> None:
    """Owner decision F: the delivery id is generated by the server for every
    request; two downloads sent with the same ``X-Request-ID`` are two
    deliveries, and the client's value names none of them."""

    if enforced:
        client._submit(_enforce())
    created = _upload(client, world, _pdf(1_000), "application/pdf")
    media_id = created.json()["id"]
    path = f"{MEDIA}/{media_id}/content"
    client_id = str(uuid4())
    headers = {**world.headers(), "X-Request-ID": client_id}
    for _ in range(2):
        assert client.get(path, headers=headers).status_code == 200
    deliveries = client._submit(_rows(CreditEgressDelivery))
    mode = "enforced" if enforced else "observational"
    assert [d.mode for d in deliveries] == [mode, mode]
    ids = {str(d.delivery_id) for d in deliveries}
    assert len(ids) == 2 and client_id not in ids
    effects = client._submit(_rows(CreditObservationalEffect))
    assert all(client_id not in effect.effect_id for effect in effects)


def test_the_overage_beyond_the_allowance_is_debited_per_100_mb(
    client: Any, world: World, switch: Callable[[bool], None]
) -> None:
    client._submit(_enforce())
    created = _upload(client, world, _pdf(1_000_000), "application/pdf")  # 50 mc
    path = f"{MEDIA}/{created.json()['id']}/content"
    # 50 mc of media include 10 MB of downloads: the 11th MB is overage.
    for _ in range(11):
        assert client.get(path, headers=world.headers()).status_code == 200
    [egress] = [
        row
        for row in client._submit(_rows(CreditAllocation))
        if row.category == "egress"
    ]
    assert (egress.units, egress.target_mc, egress.charged_mc) == (1_000_000, 10, 10)
    entries = client._submit(
        _rows(CreditLedgerEntry, CreditLedgerEntry.effect_id == egress.effect_id)
    )
    assert [(e.entry_type, e.amount_mc) for e in entries] == [("debt", 10)]


def test_media_uses_media_assets_only(client: Any) -> None:
    schema = client.get("/openapi.json").json()
    media_paths = sorted(path for path in schema["paths"] if "media" in path)
    assert media_paths == [
        "/v1/media-assets",
        "/v1/media-assets/{media_id}",
        "/v1/media-assets/{media_id}/content",
    ]
    assert "402" in schema["paths"]["/v1/media-assets"]["post"]["responses"]
    assert (
        "402"
        in schema["paths"]["/v1/media-assets/{media_id}/content"]["get"]["responses"]
    )


def test_a_foreign_tenant_is_refused_before_any_credit_lock_or_row(
    client: Any, world: World, switch: Callable[[bool], None]
) -> None:
    """IDOR: the target tenant is authorized before any credit read, lock
    or row; an API key never reaches another tenant's media or credits."""

    client._submit(_enforce())
    seeded = _upload(client, world, _pdf(1_000), "application/pdf", standalone=True)
    assert seeded.status_code == 402  # the standalone tenant: nothing stored
    upload = client.post(
        f"{MEDIA}?tenant_id={world.standalone}",
        files={"file": ("x.pdf", _pdf(1_000), "application/pdf")},
        data={"display_name": "X", "original_filename": "x.pdf"},
        headers=world.headers(),
    )
    download = client.get(
        f"{MEDIA}/{uuid4()}/content?tenant_id={world.standalone}",
        headers=world.headers(),
    )
    for response in (upload, download):
        assert response.status_code == 403, response.text
    assert client._submit(_rows(MediaAsset)) == []
    for model in (*_FINANCIAL, CreditEgressDelivery, CreditObservationalEffect):
        assert client._submit(_rows(model)) == []
