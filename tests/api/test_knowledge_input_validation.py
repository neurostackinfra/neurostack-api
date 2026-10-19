"""Knowledge upload and list input is validated by the schema, not the database.

SQLite stores NUL and over-long text without complaint, so here a ``422`` can
only come from the schema: these tests pin each validator and limit directly
and through the real routes and request session.  The PostgreSQL behaviour
that motivated them (``500`` with a driver traceback) is pinned by
``tests/integration/test_knowledge_input_validation_postgres.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from pydantic import ValidationError
from sqlalchemy import String, func, select

from src.api.middleware.performance import response_cache
from src.api.schemas.knowledge import (
    KNOWLEDGE_CATEGORY_MAX_LENGTH,
    KNOWLEDGE_CONTENT_MAX_LENGTH,
    KNOWLEDGE_CONTENT_MIN_LENGTH,
    KNOWLEDGE_CUSTOM_MAX_BYTES,
    KNOWLEDGE_CUSTOM_MAX_DEPTH,
    KNOWLEDGE_CUSTOM_MAX_KEYS,
    KNOWLEDGE_RESERVED_METADATA_KEYS,
    KNOWLEDGE_SOURCE_MAX_LENGTH,
    KNOWLEDGE_TAG_MAX_LENGTH,
    KNOWLEDGE_TAGS_MAX_ITEMS,
    KNOWLEDGE_TITLE_MAX_LENGTH,
    DocumentMetadata,
    DocumentMetadataInput,
    DocumentUploadRequest,
)
from src.config.settings import settings
from src.models.knowledge import KnowledgeDocument
from src.models.tenant import Tenant
from src.repositories import get_db_session_dependency
from src.repositories.database import get_db_session
from tests.conftest import LifespanTestClient

DOCUMENTS = "/v1/knowledge/documents"
MARK = "SEGREDO-NAO-ECOAR"
CUSTOM = ("metadata", "custom")


# ============================================
# Limits mirror the storage
# ============================================


def test_title_limit_mirrors_the_column_and_other_limits_are_documented() -> None:
    title_type = KnowledgeDocument.__table__.c.title.type
    assert isinstance(title_type, String)
    assert title_type.length == KNOWLEDGE_TITLE_MAX_LENGTH == 500
    assert (KNOWLEDGE_CONTENT_MIN_LENGTH, KNOWLEDGE_CONTENT_MAX_LENGTH) == (
        10,
        100_000,
    )
    assert (
        KNOWLEDGE_SOURCE_MAX_LENGTH,
        KNOWLEDGE_CATEGORY_MAX_LENGTH,
        KNOWLEDGE_TAGS_MAX_ITEMS,
        KNOWLEDGE_TAG_MAX_LENGTH,
    ) == (2048, 100, 50, 100)
    assert (
        KNOWLEDGE_CUSTOM_MAX_DEPTH,
        KNOWLEDGE_CUSTOM_MAX_KEYS,
        KNOWLEDGE_CUSTOM_MAX_BYTES,
    ) == (5, 100, 10_000)
    assert KNOWLEDGE_RESERVED_METADATA_KEYS == ("title", "source", "category", "tags")
    # Every reserved key is a field of the metadata, and nothing else is.
    assert set(KNOWLEDGE_RESERVED_METADATA_KEYS) == (
        set(DocumentMetadataInput.model_fields) - {"custom"}
    )


# ============================================
# Schema validators
# ============================================


def _errors(payload: dict[str, Any]) -> list[tuple[tuple[Any, ...], str, str]]:
    with pytest.raises(ValidationError) as caught:
        DocumentUploadRequest.model_validate(payload)
    errors = caught.value.errors()
    return [(tuple(e["loc"]), e["type"], e["msg"]) for e in errors]


def _upload_body(**metadata: Any) -> dict[str, Any]:
    return {"content": "Conteudo valido", "metadata": metadata}


@pytest.mark.parametrize(
    ("payload", "loc"),
    [
        ({"content": "conteudo \x00 valido"}, ("content",)),
        ({"content": "\x00" * 20}, ("content",)),
        (_upload_body(title="a\x00"), ("metadata", "title")),
        (_upload_body(source="\x00"), ("metadata", "source")),
        (_upload_body(category="x\x00y"), ("metadata", "category")),
        (_upload_body(tags=["ok", "\x00"]), ("metadata", "tags", 1)),
        (_upload_body(custom={"k": "\x00"}), CUSTOM),
        (_upload_body(custom={"\x00": 1}), CUSTOM),
        (_upload_body(custom={"a": [{"b": ["\x00"]}]}), CUSTOM),
    ],
    ids=[
        "content",
        "content_only_nul",
        "title",
        "source",
        "category",
        "tag",
        "custom_value",
        "custom_key",
        "custom_nested",
    ],
)
def test_nul_is_rejected_in_every_free_text_field(
    payload: dict[str, Any], loc: tuple[Any, ...]
) -> None:
    errors = _errors(payload)
    assert (loc, "nul_character") in [(e[0], e[1]) for e in errors]
    assert all("\x00" not in message for _, _, message in errors)


def test_unpaired_surrogates_are_rejected_in_typed_and_custom_text() -> None:
    assert _errors({"content": "conteudo \ud800 x"})[0][:2] == (
        ("content",),
        "string_unicode",
    )
    assert _errors(_upload_body(title="\udfff"))[0][:2] == (
        ("metadata", "title"),
        "string_unicode",
    )
    for custom in ({"k": "\ud83d"}, {"\udc00": 1}, {"k": ["\ud800"]}):
        assert _errors(_upload_body(custom=custom))[0][:2] == (
            CUSTOM,
            "unpaired_surrogate",
        )
    # A properly paired astral character is ordinary text.
    DocumentUploadRequest.model_validate(_upload_body(custom={"k": "\U0001f600"}))


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")], ids=str)
def test_non_finite_numbers_in_custom_are_rejected(number: float) -> None:
    errors = _errors(_upload_body(custom={"n": {"m": [1, number]}}))
    assert errors[0][:2] == (CUSTOM, "non_finite_number")


@pytest.mark.parametrize(
    ("field", "limit"),
    [
        # Literal documented values, so a changed constant fails here too.
        ("title", 500),
        ("source", 2048),
        ("category", 100),
    ],
)
@pytest.mark.parametrize(
    "unit",
    ["a", "é", "\U0001f9e0", "é"],
    ids=["ascii", "accent", "4-byte", "combining"],
)
def test_text_limits_accept_the_boundary_and_reject_one_more(
    field: str, limit: int, unit: str
) -> None:
    # Limits count code points: a combining pair is two of them.
    at_limit = (unit * limit)[:limit]
    assert len(at_limit) == limit
    accepted = DocumentUploadRequest.model_validate(_upload_body(**{field: at_limit}))
    assert getattr(accepted.metadata, field) == at_limit
    errors = _errors(_upload_body(**{field: at_limit + "x"}))
    assert errors[0][:2] == (("metadata", field), "string_too_long")


def test_tags_count_and_item_length() -> None:
    tags = ["t" * 100] * 50  # documented: 50 tags of up to 100 characters
    accepted = DocumentUploadRequest.model_validate(_upload_body(tags=tags))
    assert accepted.metadata.tags == tags
    assert _errors(_upload_body(tags=tags + ["t"]))[0][:2] == (
        ("metadata", "tags"),
        "too_long",
    )
    assert _errors(_upload_body(tags=["t" * 101]))[0][:2] == (
        ("metadata", "tags", 0),
        "string_too_long",
    )


def test_content_limits_are_unchanged() -> None:
    for content in (
        "x" * KNOWLEDGE_CONTENT_MIN_LENGTH,
        "\U0001f600" * KNOWLEDGE_CONTENT_MAX_LENGTH,
    ):
        DocumentUploadRequest.model_validate({"content": content})
    assert _errors({"content": "x" * (KNOWLEDGE_CONTENT_MIN_LENGTH - 1)})[0][:2] == (
        ("content",),
        "string_too_short",
    )
    assert _errors({"content": "x" * (KNOWLEDGE_CONTENT_MAX_LENGTH + 1)})[0][:2] == (
        ("content",),
        "string_too_long",
    )


@pytest.mark.parametrize("reserved", KNOWLEDGE_RESERVED_METADATA_KEYS)
def test_each_reserved_key_in_custom_is_rejected_by_name(reserved: str) -> None:
    errors = _errors(_upload_body(custom={reserved: MARK, "livre": 1}))
    loc, error_type, message = errors[0]
    assert (loc, error_type) == (CUSTOM, "reserved_metadata_key")
    assert reserved in message
    assert MARK not in message


def test_reserved_keys_are_listed_together_and_near_misses_are_allowed() -> None:
    every = {key: MARK for key in KNOWLEDGE_RESERVED_METADATA_KEYS}
    message = _errors(_upload_body(custom=every))[0][2]
    assert message.endswith(": title, source, category, tags")
    near = {"Title": 1, "tag": 2, "title ": 3, "_source": 4, "metadata": 5}
    accepted = DocumentUploadRequest.model_validate(_upload_body(custom=near))
    assert accepted.metadata.custom == near


# Reserved names only collide at the top level of custom, where custom is
# merged next to the metadata fields; nested deeper they are ordinary keys.
NESTED_RESERVED: list[dict[str, Any]] = [
    {"a": {"title": 1}},
    {"a": [{"tags": "x"}]},
    {"a": {"b": {"source": None, "category": ["c"]}}},
]


@pytest.mark.parametrize("custom", NESTED_RESERVED, ids=["dict", "list", "deep"])
def test_reserved_names_nested_inside_custom_are_ordinary_keys(
    custom: dict[str, Any],
) -> None:
    accepted = DocumentUploadRequest.model_validate(_upload_body(custom=custom))
    assert accepted.metadata.custom == custom
    # The same name one level up is still a collision.
    (outer,) = custom.values()
    inner = outer[0] if isinstance(outer, list) else outer
    for key in inner:
        if key in KNOWLEDGE_RESERVED_METADATA_KEYS:
            errors = _errors(_upload_body(custom={key: 1}))
            assert errors[0][:2] == (CUSTOM, "reserved_metadata_key")


def _nested(depth: int) -> dict[str, Any]:
    value: Any = 1
    for level in range(depth - 1):
        value = [value] if level % 2 else {"n": value}
    return {"root": value}


def test_custom_depth_limit() -> None:
    DocumentUploadRequest.model_validate(
        _upload_body(custom=_nested(KNOWLEDGE_CUSTOM_MAX_DEPTH))
    )
    for depth in (KNOWLEDGE_CUSTOM_MAX_DEPTH + 1, 500):
        assert _errors(_upload_body(custom=_nested(depth)))[0][:2] == (
            CUSTOM,
            "metadata_too_deep",
        )
    # An empty container still counts as a level: custom, then 4 lists = 5.
    DocumentUploadRequest.model_validate(_upload_body(custom={"a": [[[[]]]]}))
    empty: dict[str, Any] = {"a": [[[[[]]]]]}
    assert _errors(_upload_body(custom=empty))[0][:2] == (CUSTOM, "metadata_too_deep")


def test_custom_key_count_counts_nested_keys() -> None:
    flat = {f"k{i}": i for i in range(KNOWLEDGE_CUSTOM_MAX_KEYS)}
    DocumentUploadRequest.model_validate(_upload_body(custom=flat))
    assert _errors(_upload_body(custom=dict(flat, extra=1)))[0][:2] == (
        CUSTOM,
        "metadata_too_many_keys",
    )
    nested: dict[str, Any] = {f"k{i}": i for i in range(KNOWLEDGE_CUSTOM_MAX_KEYS - 1)}
    nested["k0"] = {"inner": 1}
    DocumentUploadRequest.model_validate(_upload_body(custom=nested))
    nested["k1"] = [{"a": 1}]
    assert _errors(_upload_body(custom=nested))[0][:2] == (
        CUSTOM,
        "metadata_too_many_keys",
    )


def test_custom_size_is_measured_as_compact_utf8_json() -> None:
    framing = len('{"b":""}')
    exact = {"b": "é" * ((KNOWLEDGE_CUSTOM_MAX_BYTES - framing) // 2)}
    DocumentUploadRequest.model_validate(_upload_body(custom=exact))
    over = {"b": "é" * ((KNOWLEDGE_CUSTOM_MAX_BYTES - framing) // 2) + "x"}
    assert _errors(_upload_body(custom=over))[0][:2] == (CUSTOM, "metadata_too_large")


def test_empty_and_unusual_valid_metadata_is_accepted_unchanged() -> None:
    metadata = {
        "title": "",
        "source": "",
        "category": "",
        "tags": ["", "ação", "עברית", "👩‍⚕️"],
        "custom": {"": "", "versão": 1.5, "nulo": None, "lista": [True, "‮"]},
    }
    accepted = DocumentUploadRequest.model_validate(
        {"content": "Conteúdo válido", "metadata": metadata}
    )
    assert accepted.metadata.model_dump() == metadata


def test_the_response_model_stays_unconstrained_for_stored_documents() -> None:
    legacy = DocumentMetadata(
        title="t" * (KNOWLEDGE_TITLE_MAX_LENGTH + 1),
        category="c" * (KNOWLEDGE_CATEGORY_MAX_LENGTH + 1),
        tags=["x"] * (KNOWLEDGE_TAGS_MAX_ITEMS + 1),
        custom=_nested(KNOWLEDGE_CUSTOM_MAX_DEPTH + 3),
    )
    assert len(legacy.tags) == KNOWLEDGE_TAGS_MAX_ITEMS + 1


# ============================================
# Through the routes and the real request session
# ============================================


@pytest.fixture(autouse=True)
def _real_session_dependency(test_app: Any) -> Iterator[None]:
    assert get_db_session_dependency not in test_app.dependency_overrides
    response_cache.invalidate()
    yield
    response_cache.invalidate()


def _clinic_admin(tenant_id: UUID) -> dict[str, str]:
    payload = {
        "sub": str(uuid4()),
        "tenant_id": str(tenant_id),
        "role": "clinic_admin",
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    token = jwt.encode(payload, settings.secret_key, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _owner(client: LifespanTestClient) -> tuple[UUID, dict[str, str]]:
    tenant_id = uuid4()

    async def _run() -> None:
        db = get_db_session()
        try:
            db.add(
                Tenant(
                    id=tenant_id,
                    name=f"Clinic {tenant_id}",
                    slug=f"clinic-{tenant_id}",
                    api_key_hash=f"hash-{tenant_id}",
                    config={},
                )
            )
            await db.commit()
        finally:
            await db.close()

    client._submit(_run())
    return tenant_id, _clinic_admin(tenant_id)


def _count(client: LifespanTestClient, tenant_id: UUID) -> int:
    async def _run() -> int:
        db = get_db_session()
        try:
            value = await db.scalar(
                select(func.count())
                .select_from(KnowledgeDocument)
                .where(KnowledgeDocument.tenant_id == tenant_id)
            )
            return int(value or 0)
        finally:
            await db.close()

    count: int = client._submit(_run())
    return count


def _stored_metadata(client: LifespanTestClient, document_id: str) -> Any:
    async def _run() -> Any:
        db = get_db_session()
        try:
            return await db.scalar(
                select(KnowledgeDocument.document_metadata).where(
                    KnowledgeDocument.id == UUID(document_id)
                )
            )
        finally:
            await db.close()

    return client._submit(_run())


def _assert_no_input_field_422(
    response: Any, loc: list[Any], error_type: str, caplog: pytest.LogCaptureFixture
) -> None:
    assert response.status_code == 422, response.text
    assert response.headers.get("cache-control") == "no-store"
    issues = response.json()["detail"]
    assert all(set(issue) == {"type", "loc", "msg"} for issue in issues), issues
    assert (loc, error_type) in [(i["loc"], i["type"]) for i in issues], issues
    assert MARK not in response.text
    assert "Traceback" not in caplog.text
    assert "Unhandled exception" not in caplog.text


@pytest.mark.parametrize(
    ("body", "loc", "error_type"),
    [
        ({"content": f"{MARK} \x00 fim"}, ["body", "content"], "nul_character"),
        (
            _upload_body(title=MARK * 30),
            ["body", "metadata", "title"],
            "string_too_long",
        ),
        (
            _upload_body(custom={"title": MARK}),
            ["body", "metadata", "custom"],
            "reserved_metadata_key",
        ),
        (
            {"content": MARK + "x" * KNOWLEDGE_CONTENT_MAX_LENGTH},
            ["body", "content"],
            "string_too_long",
        ),
    ],
    ids=["nul", "long_title", "reserved_key", "long_content"],
)
def test_invalid_upload_is_a_422_without_input_and_stores_nothing(
    client: LifespanTestClient,
    caplog: pytest.LogCaptureFixture,
    body: dict[str, Any],
    loc: list[Any],
    error_type: str,
) -> None:
    tenant_id, headers = _owner(client)
    caplog.set_level(logging.DEBUG)

    response = client.post(DOCUMENTS, json=body, headers=headers)

    _assert_no_input_field_422(response, loc, error_type, caplog)
    assert _count(client, tenant_id) == 0


def test_list_category_filter_is_validated(
    client: LifespanTestClient, caplog: pytest.LogCaptureFixture
) -> None:
    _, headers = _owner(client)
    caplog.set_level(logging.DEBUG)
    loc = ["query", "category"]

    encoded = client.get(f"{DOCUMENTS}?category={MARK}%00", headers=headers)
    _assert_no_input_field_422(encoded, loc, "nul_character", caplog)
    too_long = client.get(
        DOCUMENTS,
        params={"category": MARK + "c" * KNOWLEDGE_CATEGORY_MAX_LENGTH},
        headers=headers,
    )
    _assert_no_input_field_422(too_long, loc, "string_too_long", caplog)
    at_limit = client.get(
        DOCUMENTS,
        params={"category": "c" * KNOWLEDGE_CATEGORY_MAX_LENGTH},
        headers=headers,
    )
    assert (at_limit.status_code, at_limit.json()["total"]) == (200, 0)
    # The empty filter keeps meaning "no filter".
    assert client.get(f"{DOCUMENTS}?category=", headers=headers).status_code == 200


def test_other_document_route_422s_carry_no_input_field(
    client: LifespanTestClient, caplog: pytest.LogCaptureFixture
) -> None:
    _, headers = _owner(client)
    caplog.set_level(logging.DEBUG)

    for response, loc, error_type in (
        (
            client.get(f"{DOCUMENTS}/{MARK}", headers=headers),
            "document_id",
            "uuid_parsing",
        ),
        (
            client.delete(f"{DOCUMENTS}/{MARK}", headers=headers),
            "document_id",
            "uuid_parsing",
        ),
        (
            client.get(DOCUMENTS, params={"tenant_id": MARK}, headers=headers),
            "tenant_id",
            "uuid_parsing",
        ),
        (
            client.get(DOCUMENTS, params={"page_size": 101}, headers=headers),
            "page_size",
            "less_than_equal",
        ),
    ):
        kind = "query" if loc in {"tenant_id", "page_size"} else "path"
        _assert_no_input_field_422(response, [kind, loc], error_type, caplog)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"content": "conteudo valido", "metadata": {"custom": {"a": '
        + b"[" * 20_000
        + b"]" * 20_000
        + b"}}}",
        b'{"content": "conteudo valido", "metadata": {"custom": {"n": '
        + b"9" * 4_301
        + b"}}}",
        b'{"content": "conteudo \xff\xfe ' + MARK.encode() + b'"}',
    ],
    ids=["nesting", "huge_integer", "invalid_utf8"],
)
def test_bodies_fastapi_cannot_decode_are_its_400(
    client: LifespanTestClient, caplog: pytest.LogCaptureFixture, raw: bytes
) -> None:
    """Documented: never reaches the schema, never a 500, never echoed."""
    tenant_id, headers = _owner(client)
    caplog.set_level(logging.DEBUG)

    response = client.post(
        DOCUMENTS,
        content=raw,
        headers={**headers, "content-type": "application/json"},
    )

    assert (response.status_code, response.json()) == (
        400,
        {"detail": "There was an error parsing the body"},
    )
    assert "cache-control" not in response.headers
    assert MARK not in response.text
    assert "Unhandled exception" not in caplog.text
    assert _count(client, tenant_id) == 0


def test_search_stub_keeps_the_framework_validation_error(
    client: LifespanTestClient,
) -> None:
    """Search is out of scope: its 422 is FastAPI's default, input included."""
    _, headers = _owner(client)

    response = client.post("/v1/knowledge/search", json={"query": "x"}, headers=headers)

    assert response.status_code == 422, response.text
    (issue,) = response.json()["detail"]
    assert issue["loc"] == ["body", "query"]
    assert issue["input"] == "x"


def test_upload_response_is_exactly_what_get_and_list_return(
    client: LifespanTestClient, caplog: pytest.LogCaptureFixture
) -> None:
    _, headers = _owner(client)
    caplog.set_level(logging.DEBUG)
    metadata: dict[str, Any] = {
        "title": "t" * KNOWLEDGE_TITLE_MAX_LENGTH,
        "source": "https://exemplo.com.br/ação",
        "category": "pediatria",
        "tags": ["ação", "עברית", "é", "𝄞"],
        "custom": {"versão": 2, "lista": [1, {"x": None}], "": "vazio"},
    }
    content = "Conteúdo legítimo, longo e válido. " * 2000

    created = client.post(
        DOCUMENTS, json={"content": content, "metadata": metadata}, headers=headers
    )

    assert created.status_code == 201, created.text
    uploaded = created.json()
    read = client.get(f"{DOCUMENTS}/{uploaded['id']}", headers=headers)
    (listed,) = client.get(DOCUMENTS, headers=headers).json()["documents"]
    assert uploaded == read.json() == listed
    assert uploaded["metadata"] == metadata
    stored = _stored_metadata(client, uploaded["id"])
    expected: dict[str, Any] = {
        key: value for key, value in metadata.items() if key != "custom"
    }
    expected.update(metadata["custom"])
    assert stored == expected
    assert "Traceback" not in caplog.text


@pytest.mark.parametrize("custom", NESTED_RESERVED, ids=["dict", "list", "deep"])
def test_nested_reserved_names_are_stored_and_do_not_touch_the_fields(
    client: LifespanTestClient, custom: dict[str, Any]
) -> None:
    _, headers = _owner(client)
    metadata = {"title": "T", "source": "S", "category": "C", "tags": ["t"]}

    created = client.post(
        DOCUMENTS, json=_upload_body(**metadata, custom=custom), headers=headers
    )

    assert created.status_code == 201, created.text
    assert created.json()["metadata"] == {**metadata, "custom": custom}
    assert _stored_metadata(client, created.json()["id"]) == {**metadata, **custom}
    read = client.get(f"{DOCUMENTS}/{created.json()['id']}", headers=headers)
    assert read.json() == created.json()


def test_upload_response_comes_from_the_stored_row_not_the_request(
    client: LifespanTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If storage ever changed a value, the response would show the stored one."""
    from src.repositories import KnowledgeDocumentRepository

    _, headers = _owner(client)
    original_create = KnowledgeDocumentRepository.create

    async def create(self: Any, **kwargs: Any) -> KnowledgeDocument:
        stored = dict(kwargs["document_metadata"], category="armazenada")
        return await original_create(self, **{**kwargs, "document_metadata": stored})

    monkeypatch.setattr(KnowledgeDocumentRepository, "create", create)

    created = client.post(
        DOCUMENTS, json=_upload_body(category="pedida"), headers=headers
    )

    assert created.status_code == 201, created.text
    assert created.json()["metadata"]["category"] == "armazenada"
    read = client.get(f"{DOCUMENTS}/{created.json()['id']}", headers=headers)
    assert read.json() == created.json()


def test_documents_stored_before_the_limits_stay_readable(
    client: LifespanTestClient,
) -> None:
    tenant_id, headers = _owner(client)
    legacy = {
        "title": "t" * 600,
        "category": "c" * 300,
        "tags": ["x"] * 80,
        "deep": _nested(12),
    }
    document_id = uuid4()

    async def _seed() -> None:
        db = get_db_session()
        try:
            db.add(
                KnowledgeDocument(
                    id=document_id,
                    tenant_id=tenant_id,
                    title="legado",
                    content="conteudo legado",
                    document_metadata=legacy,
                )
            )
            await db.commit()
        finally:
            await db.close()

    client._submit(_seed())

    read = client.get(f"{DOCUMENTS}/{document_id}", headers=headers)
    assert read.status_code == 200, read.text
    assert read.json()["metadata"]["tags"] == legacy["tags"]
    assert read.json()["metadata"]["custom"] == {"deep": legacy["deep"]}
    listed = client.get(DOCUMENTS, headers=headers)
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
