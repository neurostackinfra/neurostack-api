"""Knowledge ingestion and the RAG reservation (Group 4a), SQLite.

The real application (lifespan, middleware, SQLite ``:memory:``) with the
credit switch forced per test.  What is pinned here:

* ``POST /v1/knowledge/documents`` (JSON) answers like ``main`` with the
  switch off or on; on and before the enforcement it is measured by
  ``len(content)`` observationally (an injected writer failure changes
  nothing but a log); after the enforcement it debits its RAG reservation in
  its own transaction, and a standalone tenant is 402 ``credit_pending``
  with nothing stored;
* ``POST /v1/knowledge/documents/upload`` (multipart DOCX/textual PDF) is a
  credit surface: 503 ``credit_unavailable`` with the switch off, before the
  body is read; the positive fixtures are parsed, every negative one is a
  422 at any time (an OCR-only PDF and a PDF needing a password are
  unreadable from the start), the request has a technical cap (413), and
  the PDF's limits (10 files, 20 MB, 500,000 characters, no encrypted PDF)
  apply only after the enforcement, before anything is reserved;
* a batch is rounded once; a foreign tenant is refused before any credit
  lock or row (IDOR).
"""

from __future__ import annotations

import io
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from sqlalchemy import select

from src.api.middleware.auth import pwd_context
from src.api.middleware.performance import response_cache
from src.api.routes import knowledge as knowledge_route
from src.config.settings import settings
from src.models.builder import Builder
from src.models.credit import (
    CreditAccount,
    CreditEnforcementState,
    CreditLedgerEntry,
    CreditObservationalEffect,
)
from src.models.credit_inventory import CreditAllocation, CreditInventoryVersion
from src.models.knowledge import KnowledgeDocument
from src.models.tenant import Tenant
from src.repositories.credit_inventory import CreditInventoryRepository
from src.repositories.database import get_db_session
from src.services import credit_inventory, credit_metering, knowledge_service
from src.services.credit_switch import CreditSwitch
from tests.api.test_events_credit_enforced import _grant

DOCUMENTS = "/v1/knowledge/documents"
UPLOAD = "/v1/knowledge/documents/upload"
FIXTURES = (
    Path(__file__).resolve().parents[1] / "fixtures" / "credit_storage_conformance"
)
_CREDIT_TABLES = (
    CreditAccount,
    CreditAllocation,
    CreditInventoryVersion,
    CreditLedgerEntry,
)


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _docx(text: str) -> bytes:
    from docx import Document

    document = Document()
    document.add_paragraph(text)
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def _large_pdf(padding: int) -> bytes:
    """A valid textual PDF made larger by an unused stream of ``padding``
    bytes (offsets computed, so the cross-reference stays exact)."""

    text = b"BT /F1 12 Tf 72 720 Td (Documento grande) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(text) + text + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % padding + b"0" * padding + b"\nendstream",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, xref)
    )
    return out.getvalue()


def _empty_password_pdf() -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(
        clone_from=PdfReader(io.BytesIO(_fixture("positive/textual.pdf")))
    )
    writer.encrypt(user_password="", owner_password="owner-only", algorithm="RC4-128")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class World:
    def __init__(self) -> None:
        self.builder = uuid4()
        self.tenant = uuid4()
        self.key = secrets.token_urlsafe(24)
        self.standalone = uuid4()
        self.standalone_key = secrets.token_urlsafe(24)
        self.foreign = uuid4()

    def headers(self, standalone: bool = False) -> dict[str, str]:
        return {"X-API-Key": self.standalone_key if standalone else self.key}


async def _seed(world: World) -> None:
    session = get_db_session()
    try:
        async with session.begin():
            session.add(Builder(id=world.builder, name="RAG builder"))
            await session.flush()
            for tenant_id, builder, key in (
                (world.tenant, world.builder, world.key),
                (world.standalone, None, world.standalone_key),
                (world.foreign, None, secrets.token_urlsafe(8)),
            ):
                session.add(
                    Tenant(
                        id=tenant_id,
                        name=f"RAG {tenant_id.hex[:6]}",
                        slug=f"rag-{tenant_id.hex}",
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


async def _rows(model: Any, *conditions: Any) -> list[Any]:
    session = get_db_session()
    try:
        return list((await session.execute(select(model).where(*conditions))).scalars())
    finally:
        await session.close()


@pytest.fixture
def world(client: Any) -> World:
    seeded = World()
    client._submit(_seed(seeded))
    response_cache.invalidate()
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
    """The observational writer's outcomes, with a generous budget (none of
    these tests is about the 250 ms)."""

    outcomes: list[str] = []
    original = credit_inventory.record_observational_upload

    async def recorded(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("budget_seconds", 5)
        outcome = await original(*args, **kwargs)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(credit_inventory, "record_observational_upload", recorded)
    return outcomes


def _post_files(
    client: Any, world: World, files: list[tuple[str, bytes]], **kwargs: Any
) -> Any:
    return client.post(
        UPLOAD,
        files=[
            ("files", (name, data, "application/octet-stream")) for name, data in files
        ],
        headers=kwargs.pop("headers", world.headers()),
        **kwargs,
    )


def _json(
    world: World, content: str = "Atendemos de segunda a sexta."
) -> dict[str, Any]:
    return {"content": content, "metadata": {"title": "Horários"}}


# ---------------------------------------------------------------------------
# Switch off: main.
# ---------------------------------------------------------------------------


def test_with_the_switch_off_the_upload_endpoint_is_503_before_the_body(
    client: Any, world: World, switch: Callable[[bool], None]
) -> None:
    switch(False)
    malformed = client.post(
        UPLOAD,
        content=b"not multipart at all",
        headers={**world.headers(), "Content-Type": "multipart/form-data; boundary=x"},
    )
    assert (malformed.status_code, malformed.json()) == (
        503,
        {"code": "credit_unavailable"},
    )
    positive = _post_files(client, world, [("a.pdf", _fixture("positive/textual.pdf"))])
    assert (positive.status_code, positive.json()) == (
        503,
        {"code": "credit_unavailable"},
    )
    stored = client.post(DOCUMENTS, json=_json(world), headers=world.headers())
    assert stored.status_code == 201
    for model in (*_CREDIT_TABLES, CreditObservationalEffect):
        assert client._submit(_rows(model)) == []


# ---------------------------------------------------------------------------
# Switch on, before the enforcement: observational only.
# ---------------------------------------------------------------------------


def test_json_upload_answers_like_main_and_is_measured_observationally(
    client: Any,
    world: World,
    switch: Callable[[bool], None],
    writer_outcomes: list[str],
) -> None:
    switch(False)
    off = client.post(
        DOCUMENTS, json=_json(world, "x" * 10_000), headers=world.headers()
    )
    switch(True)
    on = client.post(
        DOCUMENTS, json=_json(world, "x" * 10_000), headers=world.headers()
    )
    assert (off.status_code, on.status_code) == (201, 201)
    mask = {"id", "created_at"}
    assert {k: v for k, v in on.json().items() if k not in mask} == {
        k: v for k, v in off.json().items() if k not in mask
    }
    assert writer_outcomes == ["recorded"]
    effects = {
        row.effect_id: row for row in client._submit(_rows(CreditObservationalEffect))
    }
    reservation = effects[f"rag:{world.tenant}:{on.json()['id']}:reserve"]
    # 20,000 characters stored (both documents): the second adds 2 credits.
    assert (reservation.effect_kind, reservation.would_debit_mc) == ("rag", 2_000)
    assert {row.effect_kind for row in effects.values()} == {"rag", "maintenance"}
    for model in _CREDIT_TABLES:
        assert client._submit(_rows(model)) == []


def test_an_injected_observational_failure_leaves_the_answer_and_the_data(
    client: Any,
    world: World,
    switch: Callable[[bool], None],
    writer_outcomes: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(_self: Any, builder_id: UUID) -> Any:
        raise RuntimeError("lock timeout")

    alerts: list[dict[str, Any]] = []

    class _Logger:
        def warning(self, _message: str, **fields: Any) -> None:
            alerts.append(fields)

        def info(self, _message: str, **_fields: Any) -> None:
            pass

    monkeypatch.setattr(CreditInventoryRepository, "builder_sources", broken)
    monkeypatch.setattr(credit_inventory, "logger", _Logger())
    switch(False)
    off = client.post(DOCUMENTS, json=_json(world), headers=world.headers())
    switch(True)
    response = client.post(DOCUMENTS, json=_json(world), headers=world.headers())
    assert response.status_code == off.status_code == 201
    mask = {"id", "created_at"}
    assert {k: v for k, v in response.json().items() if k not in mask} == {
        k: v for k, v in off.json().items() if k not in mask
    }
    assert writer_outcomes == ["failed"]
    assert [
        (fields["alert"], fields["reason"], fields["source"], fields["error_type"])
        for fields in alerts
    ] == [("credit_observational_write", "failed", "rag_upload", "RuntimeError")]
    stored = client._submit(
        _rows(KnowledgeDocument, KnowledgeDocument.id == UUID(response.json()["id"]))
    )
    assert len(stored) == 1
    assert client._submit(_rows(CreditObservationalEffect)) == []


def test_the_fixtures_parse_and_every_negative_one_is_refused(
    client: Any,
    world: World,
    switch: Callable[[bool], None],
    writer_outcomes: list[str],
) -> None:
    accepted = _post_files(
        client,
        world,
        [
            ("horarios.pdf", _fixture("positive/textual.pdf")),
            ("clinica.docx", _fixture("positive/textual.docx")),
        ],
    )
    assert accepted.status_code == 201, accepted.text
    documents = accepted.json()["documents"]
    assert [doc["metadata"]["title"] for doc in documents] == [
        "horarios.pdf",
        "clinica.docx",
    ]
    stored = {
        row.title: row
        for row in client._submit(
            _rows(KnowledgeDocument, KnowledgeDocument.tenant_id == world.tenant)
        )
    }
    assert "Horario de atendimento" in stored["horarios.pdf"].content
    assert stored["horarios.pdf"].content_type == "pdf"
    assert "R$ 200,00" in stored["clinica.docx"].content
    assert writer_outcomes == ["recorded"]
    for name, code in (
        ("negative/ocr-only.pdf", "knowledge_file_unreadable"),
        ("negative/password-protected.pdf", "knowledge_file_unreadable"),
        ("negative/corrupt.pdf", "knowledge_file_unreadable"),
        ("negative/corrupt.docx", "knowledge_file_unreadable"),
    ):
        refused = _post_files(client, world, [(name, _fixture(name))])
        assert (refused.status_code, refused.json()) == (
            422,
            {"detail": {"code": code}},
        ), name
    png = _post_files(client, world, [("x.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64)])
    assert (png.status_code, png.json()["detail"]["code"]) == (
        422,
        "knowledge_file_unsupported",
    )
    # One bad file refuses the whole batch: nothing of it is stored.
    mixed = _post_files(
        client,
        world,
        [
            ("ok.pdf", _fixture("positive/textual.pdf")),
            ("bad.pdf", _fixture("negative/corrupt.pdf")),
        ],
    )
    assert mixed.status_code == 422
    assert (
        len(
            client._submit(
                _rows(KnowledgeDocument, KnowledgeDocument.tenant_id == world.tenant)
            )
        )
        == 2
    )


def test_before_the_enforcement_the_pdfs_commercial_limits_do_not_apply(
    client: Any,
    world: World,
    switch: Callable[[bool], None],
    writer_outcomes: list[str],
) -> None:
    eleven = [(f"doc-{index}.docx", _docx(f"Documento {index}")) for index in range(11)]
    assert _post_files(client, world, eleven).status_code == 201
    long_text = _post_files(client, world, [("long.docx", _docx("y" * 500_001))])
    assert long_text.status_code == 201
    owner_only = _post_files(client, world, [("owner.pdf", _empty_password_pdf())])
    assert owner_only.status_code == 201
    large = _post_files(client, world, [("large.pdf", _large_pdf(20_000_001))])
    assert large.status_code == 201, large.text
    for model in _CREDIT_TABLES:
        assert client._submit(_rows(model)) == []


def test_the_request_has_a_technical_cap_and_a_closed_shape(
    client: Any,
    world: World,
    switch: Callable[[bool], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(knowledge_route, "KNOWLEDGE_UPLOAD_REQUEST_MAX_BYTES", 2_000)
    too_large = _post_files(
        client, world, [("a.pdf", _fixture("positive/textual.pdf") * 4)]
    )
    assert (too_large.status_code, too_large.json()) == (
        413,
        {"detail": {"code": "knowledge_request_too_large"}},
    )
    monkeypatch.setattr(
        knowledge_route, "KNOWLEDGE_UPLOAD_REQUEST_MAX_BYTES", 10_000_000
    )
    other_field = client.post(
        UPLOAD,
        files=[
            ("other", ("a.pdf", _fixture("positive/textual.pdf"), "application/pdf"))
        ],
        headers=world.headers(),
    )
    assert (other_field.status_code, other_field.json()["detail"]["code"]) == (
        422,
        "knowledge_multipart_invalid",
    )
    text_field = client.post(
        UPLOAD,
        data={"note": "x"},
        files=[
            ("files", ("a.pdf", _fixture("positive/textual.pdf"), "application/pdf"))
        ],
        headers=world.headers(),
    )
    assert text_field.status_code == 422


def test_the_extraction_is_bounded_and_never_starts_jbig2dec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parser safety: whatever the process configuration says, an extraction
    reads with ``jbig2dec`` switched off (and pypdf's legacy constants
    ignored); the page cap, a DOCX archive's declared size and entry count
    and the deadline each make a file unreadable."""

    import pypdf

    seen: list[tuple[Any, bool]] = []

    class Reader(pypdf.PdfReader):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            configuration = pypdf.get_configuration()
            seen.append(
                (configuration.jbig2dec_binary, configuration.disable_legacy_handling)
            )
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(pypdf, "PdfReader", Reader)
    pdf, docx = _fixture("positive/textual.pdf"), _fixture("positive/textual.docx")
    with pypdf.apply_configuration(jbig2dec_binary="/usr/bin/jbig2dec"):
        assert "Horario" in knowledge_service.extract_document_text(pdf).text
    assert seen == [(None, True)]

    def unreadable(data: bytes, **kwargs: Any) -> None:
        with pytest.raises(knowledge_service.KnowledgeFileError) as info:
            knowledge_service.extract_document_text(data, **kwargs)
        assert info.value.code == "knowledge_file_unreadable"

    unreadable(pdf, deadline=time.monotonic() - 1)
    unreadable(docx, deadline=time.monotonic() - 1)
    for name, bound, data in (
        ("KNOWLEDGE_PDF_MAX_PAGES", 0, pdf),
        ("KNOWLEDGE_DOCX_MAX_UNCOMPRESSED_BYTES", 1_000, docx),
        ("KNOWLEDGE_DOCX_MAX_ENTRIES", 3, docx),
    ):
        with monkeypatch.context() as patch:
            patch.setattr(knowledge_service, name, bound)
            unreadable(data)
    # Within the bounds the same files read.
    assert knowledge_service.extract_document_text(docx).kind == "docx"


def test_the_upload_parses_off_the_event_loop_holding_no_transaction(
    client: Any,
    world: World,
    switch: Callable[[bool], None],
    writer_outcomes: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The files are parsed in a worker thread, never on the event loop, and
    the request's session holds no transaction (no pooled connection) while
    the body arrives and is parsed."""

    sessions: list[Any] = []
    probes: list[tuple[bool, int, int]] = []
    original_require = knowledge_route._require_tenant_row
    original_extract = knowledge_route.extract_documents
    original_text = knowledge_service.extract_document_text
    threads: list[int] = []

    async def require(session: Any, tenant_id: UUID) -> None:
        sessions.append(session)
        await original_require(session, tenant_id)

    def text(data: bytes, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        return original_text(data, **kwargs)

    async def extract(files: Any, **kwargs: Any) -> Any:
        [session] = sessions
        loop_thread = threading.get_ident()
        result = await original_extract(files, **kwargs)
        probes.append((session.in_transaction(), loop_thread, len(threads)))
        return result

    monkeypatch.setattr(knowledge_route, "_require_tenant_row", require)
    monkeypatch.setattr(knowledge_route, "extract_documents", extract)
    monkeypatch.setattr(knowledge_service, "extract_document_text", text)
    response = _post_files(
        client,
        world,
        [
            ("a.pdf", _fixture("positive/textual.pdf")),
            ("b.docx", _fixture("positive/textual.docx")),
        ],
    )
    assert response.status_code == 201, response.text
    [(in_transaction, loop_thread, parsed)] = probes
    assert in_transaction is False
    assert parsed == 2 and loop_thread not in threads


# ---------------------------------------------------------------------------
# After the enforcement.
# ---------------------------------------------------------------------------


def test_after_the_enforcement_limits_refuse_before_anything_is_reserved(
    client: Any, world: World, switch: Callable[[bool], None]
) -> None:
    client._submit(_enforce())
    eleven = [(f"doc-{index}.docx", _docx(f"Documento {index}")) for index in range(11)]
    cases = [
        (eleven, 422, "knowledge_too_many_files"),
        ([("long.docx", _docx("y" * 500_001))], 422, "knowledge_document_too_long"),
        (
            [("owner.pdf", _empty_password_pdf())],
            422,
            "knowledge_file_password_protected",
        ),
        (
            [("large.pdf", _large_pdf(20_000_001))],
            413,
            "knowledge_file_too_large",
        ),
    ]
    for files, status_code, code in cases:
        response = _post_files(client, world, files)
        assert (response.status_code, response.json()) == (
            status_code,
            {"detail": {"code": code}},
        )
    assert client._submit(_rows(KnowledgeDocument)) == []
    for model in _CREDIT_TABLES:
        assert client._submit(_rows(model)) == []
    # Ten files of 500,000 characters exactly are within the limits.
    ten = [(f"ok-{index}.docx", _docx("z" * 50_000)) for index in range(10)]
    assert _post_files(client, world, ten).status_code == 201


def test_after_the_enforcement_a_batch_is_reserved_once_and_rounded_once(
    client: Any, world: World, switch: Callable[[bool], None]
) -> None:
    client._submit(_enforce())
    # Three documents of 1,001 characters: rounded once on 3,003 (1 credit
    # minus 399 mc), never three times (3 x 201 mc).
    files = [(f"d{index}.docx", _docx("a" * 1_001)) for index in range(3)]
    response = _post_files(client, world, files)
    assert response.status_code == 201, response.text
    [reservation] = [
        row
        for row in client._submit(_rows(CreditAllocation))
        if row.allocation_kind == "reservation"
    ]
    assert (reservation.units, reservation.target_mc, reservation.charged_mc) == (
        3_003,
        601,
        601,
    )
    entries = client._submit(
        _rows(CreditLedgerEntry, CreditLedgerEntry.effect_id == reservation.effect_id)
    )
    # No lot: the reservation is served as debt, never refused.
    assert [(entry.entry_type, entry.amount_mc) for entry in entries] == [("debt", 601)]
    assert client._submit(_rows(CreditObservationalEffect)) == []
    # Group 5: that debt opened the builder's exhaustion episode, so new
    # content is refused (nothing stored) until the builder pays.
    refused = client.post(
        DOCUMENTS, json=_json(world, "b" * 2_000), headers=world.headers()
    )
    assert (refused.status_code, refused.json()) == (
        402,
        {"detail": {"code": "credit_category_exhausted"}},
    )
    client._submit(_grant(world.builder, 100_000))
    # The JSON route reserves too, in the same window, by the delta.
    stored = client.post(
        DOCUMENTS, json=_json(world, "b" * 2_000), headers=world.headers()
    )
    assert stored.status_code == 201
    [json_row] = [
        row
        for row in client._submit(_rows(CreditAllocation))
        if row.effect_id == f"rag:{world.tenant}:{stored.json()['id']}:reserve"
    ]
    assert (json_row.target_mc, json_row.charged_mc) == (1_001, 400)


def test_after_the_enforcement_a_standalone_tenant_is_credit_pending(
    client: Any, world: World, switch: Callable[[bool], None]
) -> None:
    client._submit(_enforce())
    pending = {"detail": {"code": "credit_pending"}}
    json_upload = client.post(DOCUMENTS, json=_json(world), headers=world.headers(True))
    files = _post_files(
        client,
        world,
        [("a.pdf", _fixture("positive/textual.pdf"))],
        headers=world.headers(True),
    )
    assert (json_upload.status_code, json_upload.json()) == (402, pending)
    assert (files.status_code, files.json()) == (402, pending)
    assert client._submit(_rows(KnowledgeDocument)) == []
    for model in _CREDIT_TABLES:
        assert client._submit(_rows(model)) == []


def _jwt(role: str, tenant_id: UUID) -> dict[str, str]:
    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(tenant_id),
            "role": role,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def test_a_foreign_tenant_is_refused_before_any_credit_lock_or_row(
    client: Any, world: World, switch: Callable[[bool], None]
) -> None:
    client._submit(_enforce())
    headers = _jwt("clinic_admin", world.tenant)
    json_upload = client.post(
        DOCUMENTS,
        json={**_json(world), "tenant_id": str(world.foreign)},
        headers=headers,
    )
    files = client.post(
        f"{UPLOAD}?tenant_id={world.foreign}",
        files=[
            ("files", ("a.pdf", _fixture("positive/textual.pdf"), "application/pdf"))
        ],
        headers=headers,
    )
    for response in (json_upload, files):
        assert response.status_code == 403
        assert response.json() == {"detail": {"code": "tenant_override_forbidden"}}
    assert client._submit(_rows(KnowledgeDocument)) == []
    for model in (*_CREDIT_TABLES, CreditObservationalEffect):
        assert client._submit(_rows(model)) == []


def test_the_openapi_documents_the_upload_surface(client: Any) -> None:
    schema = client.get("/openapi.json").json()
    operation = schema["paths"][UPLOAD]["post"]
    assert {"201", "402", "413", "422", "503"} <= set(operation["responses"])
    assert "multipart/form-data" in operation["requestBody"]["content"]
    assert "402" in schema["paths"][DOCUMENTS]["post"]["responses"]
