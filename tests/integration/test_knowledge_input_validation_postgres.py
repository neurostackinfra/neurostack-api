"""Real PostgreSQL evidence that knowledge input is validated before storage.

PostgreSQL ``text``/``varchar`` and ``jsonb`` cannot hold NUL (U+0000) or an
unpaired UTF-16 surrogate, ``jsonb`` rejects ``NaN``/``Infinity``, and
``knowledge_documents.title`` is ``VARCHAR(500)``.  Before the schema enforced
these, such an upload (or the list's ``category`` filter) reached the driver and
answered ``500`` with a traceback.  These tests pin, on the real application
and its real request session, that:

* every such input is a ``422`` whose issues carry only ``type``, ``loc`` and
  ``msg`` (no ``input`` field), nothing is stored, and the log holds
  no traceback;
* every documented limit accepts its boundary and rejects one past it;
* ``metadata.custom`` cannot use a reserved key (``title``, ``source``,
  ``category``, ``tags``), so it can never overwrite the stored metadata;
* the upload response is exactly what ``GET`` and the list return afterwards,
  which is exactly what the row holds, also for unusual but legal input.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from src.api.schemas.knowledge import (
    KNOWLEDGE_CATEGORY_MAX_LENGTH,
    KNOWLEDGE_CONTENT_MAX_LENGTH,
    KNOWLEDGE_CUSTOM_MAX_BYTES,
    KNOWLEDGE_CUSTOM_MAX_DEPTH,
    KNOWLEDGE_CUSTOM_MAX_KEYS,
    KNOWLEDGE_RESERVED_METADATA_KEYS,
    KNOWLEDGE_SOURCE_MAX_LENGTH,
    KNOWLEDGE_TAG_MAX_LENGTH,
    KNOWLEDGE_TAGS_MAX_ITEMS,
    KNOWLEDGE_TITLE_MAX_LENGTH,
)
from src.models.knowledge import KnowledgeDocument
from tests.integration.test_knowledge_persistence_postgres import (  # noqa: F401
    _clinic_admin,
    _document_count,
    _Knowledge,
    _seed_tenants,
    knowledge,
    template_database,
)

pytestmark = pytest.mark.integration

_DOCUMENTS = "/v1/knowledge/documents"
_MARK = "SEGREDO-NAO-ECOAR"
_JSON = {"content-type": "application/json"}
# Anything that would show a driver error or an unhandled exception.
_LOG_FORBIDDEN = (
    "Traceback",
    "Unhandled exception",
    "CharacterNotInRepertoire",
    "UntranslatableCharacter",
    "StringDataRightTruncation",
    "UnicodeEncodeError",
    "invalid input syntax for type json",
    "unsupported Unicode escape sequence",
)


def _content(extra: str = "") -> str:
    return f"Conteudo valido do documento {extra}".strip()


def _body(**metadata: Any) -> dict[str, Any]:
    return {"content": _content(), "metadata": metadata}


async def _post_json(kb: _Knowledge, headers: dict[str, str], body: Any) -> Any:
    return await kb.client.post(_DOCUMENTS, json=body, headers=headers)


async def _post_raw(kb: _Knowledge, headers: dict[str, str], raw: bytes) -> Any:
    return await kb.client.post(_DOCUMENTS, content=raw, headers={**headers, **_JSON})


async def _row(kb: _Knowledge, document_id: str) -> dict[str, Any]:
    async with kb.sessions() as session:
        row = (
            await session.execute(
                select(
                    KnowledgeDocument.tenant_id,
                    KnowledgeDocument.title,
                    KnowledgeDocument.content,
                    KnowledgeDocument.document_metadata,
                    KnowledgeDocument.size_bytes,
                ).where(KnowledgeDocument.id == UUID(document_id))
            )
        ).one()
    return {
        "tenant_id": row[0],
        "title": row[1],
        "content": row[2],
        "metadata": row[3],
        "size_bytes": row[4],
    }


def _assert_clean_422(
    response: Any,
    caplog: pytest.LogCaptureFixture,
    *,
    loc: tuple[Any, ...],
    error_type: str,
    secret: str = _MARK,
) -> dict[str, Any]:
    assert response.status_code == 422, response.text
    assert response.headers.get("cache-control") == "no-store"
    body = response.json()
    issues = body["detail"]
    assert isinstance(issues, list) and issues
    for issue in issues:
        assert set(issue) == {"type", "loc", "msg"}, issue
    assert (list(loc), error_type) in [
        (issue["loc"], issue["type"]) for issue in issues
    ], issues
    # Neither the submitted text nor the NUL itself is echoed back.
    assert secret not in response.text
    assert "\\u0000" not in response.text
    for forbidden in _LOG_FORBIDDEN:
        assert forbidden not in caplog.text, forbidden
    matched: dict[str, Any] = next(
        issue
        for issue in issues
        if (issue["loc"], issue["type"]) == (list(loc), error_type)
    )
    return matched


async def _owner(kb: _Knowledge) -> dict[str, str]:
    owner = uuid4()
    await _seed_tenants(kb, owner)
    return _clinic_admin(owner)


# --------------------------------------------------------------------------
# NUL and unpaired surrogates in every free-text field
# --------------------------------------------------------------------------

_NUL_CASES = [
    ("content", {"content": f"{_MARK} antes \x00 depois"}, ("body", "content")),
    (
        "title",
        _body(title=f"{_MARK}\x00"),
        ("body", "metadata", "title"),
    ),
    (
        "source",
        _body(source=f"https://x/{_MARK}\x00"),
        ("body", "metadata", "source"),
    ),
    (
        "category",
        _body(category=f"{_MARK}\x00"),
        ("body", "metadata", "category"),
    ),
    (
        "tag",
        _body(tags=["ok", f"{_MARK}\x00"]),
        ("body", "metadata", "tags", 1),
    ),
    (
        "custom_value",
        _body(custom={"nota": f"{_MARK}\x00"}),
        ("body", "metadata", "custom"),
    ),
    (
        "custom_key",
        _body(custom={f"{_MARK}\x00": "valor"}),
        ("body", "metadata", "custom"),
    ),
    (
        "custom_nested",
        _body(custom={"a": {"b": [1, {"c": f"{_MARK}\x00"}]}}),
        ("body", "metadata", "custom"),
    ),
    ("nul_only", _body(title="\x00"), ("body", "metadata", "title")),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "loc"),
    [(body, loc) for _, body, loc in _NUL_CASES],
    ids=[case for case, _, _ in _NUL_CASES],
)
async def test_nul_in_any_upload_field_is_a_clean_422(
    knowledge: _Knowledge,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
    body: dict[str, Any],
    loc: tuple[Any, ...],
) -> None:
    headers = await _owner(knowledge)
    caplog.set_level(logging.DEBUG)

    response = await _post_json(knowledge, headers, body)

    issue = _assert_clean_422(response, caplog, loc=loc, error_type="nul_character")
    assert "NUL" in issue["msg"]
    assert await _document_count(knowledge) == 0


@pytest.mark.asyncio
async def test_nul_written_as_a_json_escape_is_a_clean_422(
    knowledge: _Knowledge,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    headers = await _owner(knowledge)
    caplog.set_level(logging.DEBUG)
    raw = (
        b'{"content": "' + _MARK.encode() + b' conteudo \\u0000 fim",'
        b' "metadata": {"title": "ok"}}'
    )

    escaped = await _post_raw(knowledge, headers, raw)
    _assert_clean_422(
        escaped, caplog, loc=("body", "content"), error_type="nul_character"
    )

    # A literal NUL byte inside a JSON string is not valid JSON at all.
    literal = await _post_raw(
        knowledge,
        headers,
        b'{"content": "conteudo \x00 ' + _MARK.encode() + b' fim"}',
    )
    assert literal.status_code == 422, literal.text
    assert _MARK not in literal.text
    for forbidden in _LOG_FORBIDDEN:
        assert forbidden not in caplog.text, forbidden
    assert await _document_count(knowledge) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_field", "loc", "error_type"),
    [
        # Typed string fields: pydantic-core itself refuses the surrogate.
        (
            b'"content": "' + _MARK.encode() + b' texto \\ud800 fim"',
            ("body", "content"),
            "string_unicode",
        ),
        (
            b'"content": "conteudo valido", "metadata": {"title": "\\udfff'
            + _MARK.encode()
            + b'"}',
            ("body", "metadata", "title"),
            "string_unicode",
        ),
        # Free-form custom values and keys: the schema's own check.
        (
            b'"content": "conteudo valido", "metadata": {"custom": {"k": "\\ud83d'
            + _MARK.encode()
            + b'"}}',
            ("body", "metadata", "custom"),
            "unpaired_surrogate",
        ),
        (
            b'"content": "conteudo valido", "metadata": {"custom": {"\\udc00'
            + _MARK.encode()
            + b'": 1}}',
            ("body", "metadata", "custom"),
            "unpaired_surrogate",
        ),
    ],
    ids=["content", "title", "custom_value", "custom_key"],
)
async def test_unpaired_surrogate_is_a_clean_422(
    knowledge: _Knowledge,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
    raw_field: bytes,
    loc: tuple[Any, ...],
    error_type: str,
) -> None:
    headers = await _owner(knowledge)
    caplog.set_level(logging.DEBUG)

    response = await _post_raw(knowledge, headers, b"{" + raw_field + b"}")

    _assert_clean_422(response, caplog, loc=loc, error_type=error_type)
    assert await _document_count(knowledge) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("literal", [b"NaN", b"Infinity", b"-Infinity", b"1e400"])
async def test_non_finite_number_in_custom_is_a_clean_422(
    knowledge: _Knowledge,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
    literal: bytes,
) -> None:
    headers = await _owner(knowledge)
    caplog.set_level(logging.DEBUG)
    raw = (
        b'{"content": "conteudo valido", "metadata": {"custom": {"n": [1, '
        + literal
        + b"]}}}"
    )

    response = await _post_raw(knowledge, headers, raw)

    _assert_clean_422(
        response,
        caplog,
        loc=("body", "metadata", "custom"),
        error_type="non_finite_number",
    )
    assert await _document_count(knowledge) == 0


# --------------------------------------------------------------------------
# Lengths: boundary accepted and persisted, one past it rejected
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unit",
    ["a", "ç", "\U0001f600", "א"],
    ids=["ascii", "accent", "4-byte", "rtl"],
)
async def test_title_at_the_column_limit_persists_and_one_more_is_422(
    knowledge: _Knowledge,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
    unit: str,
) -> None:
    headers = await _owner(knowledge)
    caplog.set_level(logging.DEBUG)
    assert KNOWLEDGE_TITLE_MAX_LENGTH == 500  # knowledge_documents.title

    at_limit = unit * KNOWLEDGE_TITLE_MAX_LENGTH
    created = await _post_json(knowledge, headers, _body(title=at_limit))
    assert created.status_code == 201, created.text
    row = await _row(knowledge, created.json()["id"])
    assert row["title"] == at_limit
    assert row["metadata"]["title"] == at_limit

    over = await _post_json(knowledge, headers, _body(title=_MARK + at_limit))
    _assert_clean_422(
        over, caplog, loc=("body", "metadata", "title"), error_type="string_too_long"
    )
    assert await _document_count(knowledge) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "limit"),
    [
        ("source", KNOWLEDGE_SOURCE_MAX_LENGTH),
        ("category", KNOWLEDGE_CATEGORY_MAX_LENGTH),
    ],
)
async def test_source_and_category_limits(
    knowledge: _Knowledge,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
    field: str,
    limit: int,
) -> None:
    headers = await _owner(knowledge)
    caplog.set_level(logging.DEBUG)

    at_limit = "\U0001f9e0" * limit
    created = await _post_json(knowledge, headers, _body(**{field: at_limit}))
    assert created.status_code == 201, created.text
    assert (await _row(knowledge, created.json()["id"]))["metadata"][field] == at_limit

    over = await _post_json(
        knowledge, headers, _body(**{field: _MARK + "x" * (limit - len(_MARK) + 1)})
    )
    _assert_clean_422(
        over, caplog, loc=("body", "metadata", field), error_type="string_too_long"
    )
    assert await _document_count(knowledge) == 1


@pytest.mark.asyncio
async def test_tags_count_and_length_limits(
    knowledge: _Knowledge,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    headers = await _owner(knowledge)
    caplog.set_level(logging.DEBUG)
    tags = [f"{i:03d}" + "t" * (KNOWLEDGE_TAG_MAX_LENGTH - 3) for i in range(50)]
    assert len(tags) == KNOWLEDGE_TAGS_MAX_ITEMS

    created = await _post_json(knowledge, headers, _body(tags=tags))
    assert created.status_code == 201, created.text
    assert (await _row(knowledge, created.json()["id"]))["metadata"]["tags"] == tags

    too_many = await _post_json(knowledge, headers, _body(tags=tags + [_MARK]))
    _assert_clean_422(
        too_many, caplog, loc=("body", "metadata", "tags"), error_type="too_long"
    )
    too_long = await _post_json(
        knowledge, headers, _body(tags=[_MARK + "t" * KNOWLEDGE_TAG_MAX_LENGTH])
    )
    _assert_clean_422(
        too_long,
        caplog,
        loc=("body", "metadata", "tags", 0),
        error_type="string_too_long",
    )
    assert await _document_count(knowledge) == 1


@pytest.mark.asyncio
async def test_content_limit_counts_characters_and_persists_at_the_limit(
    knowledge: _Knowledge,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    headers = await _owner(knowledge)
    caplog.set_level(logging.DEBUG)
    content = "\U0001f600" * KNOWLEDGE_CONTENT_MAX_LENGTH

    created = await _post_json(knowledge, headers, {"content": content})
    assert created.status_code == 201, created.text
    row = await _row(knowledge, created.json()["id"])
    assert row["content"] == content
    assert row["size_bytes"] == 4 * KNOWLEDGE_CONTENT_MAX_LENGTH

    over = await _post_json(knowledge, headers, {"content": _MARK + content})
    _assert_clean_422(
        over, caplog, loc=("body", "content"), error_type="string_too_long"
    )
    assert await _document_count(knowledge) == 1


# --------------------------------------------------------------------------
# metadata.custom: reserved keys and size bounds
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved", KNOWLEDGE_RESERVED_METADATA_KEYS)
async def test_custom_using_a_reserved_key_is_422_and_never_overwrites(
    knowledge: _Knowledge,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
    reserved: str,
) -> None:
    headers = await _owner(knowledge)
    caplog.set_level(logging.DEBUG)
    body = _body(
        title="Titulo real",
        source="fonte real",
        category="real",
        tags=["real"],
        custom={reserved: _MARK, "livre": 1},
    )

    response = await _post_json(knowledge, headers, body)

    issue = _assert_clean_422(
        response,
        caplog,
        loc=("body", "metadata", "custom"),
        error_type="reserved_metadata_key",
    )
    assert reserved in issue["msg"]
    assert await _document_count(knowledge) == 0


@pytest.mark.asyncio
async def test_custom_using_every_reserved_key_lists_them_all(
    knowledge: _Knowledge,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    headers = await _owner(knowledge)
    caplog.set_level(logging.DEBUG)
    custom = {key: _MARK for key in KNOWLEDGE_RESERVED_METADATA_KEYS}

    response = await _post_json(knowledge, headers, _body(custom=custom))

    issue = _assert_clean_422(
        response,
        caplog,
        loc=("body", "metadata", "custom"),
        error_type="reserved_metadata_key",
    )
    for key in KNOWLEDGE_RESERVED_METADATA_KEYS:
        assert key in issue["msg"]
    # Near-misses are ordinary custom keys, stored under their own name.
    near = {"Title": "a", "tag": "b", "title ": "c", "_source": "d"}
    created = await _post_json(knowledge, headers, _body(title="T", custom=near))
    assert created.status_code == 201, created.text
    stored = (await _row(knowledge, created.json()["id"]))["metadata"]
    assert stored["title"] == "T"
    assert {key: stored[key] for key in near} == near


@pytest.mark.asyncio
async def test_reserved_names_nested_inside_custom_are_stored_as_sent(
    knowledge: _Knowledge,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    headers = await _owner(knowledge)
    caplog.set_level(logging.DEBUG)
    fields = {"title": "T", "source": "S", "category": "C", "tags": ["t"]}
    custom = {"a": {"title": 1}, "b": [{"tags": "x"}], "c": {"d": {"source": None}}}

    created = await _post_json(knowledge, headers, _body(**fields, custom=custom))

    assert created.status_code == 201, created.text
    assert (await _row(knowledge, created.json()["id"]))["metadata"] == {
        **fields,
        **custom,
    }
    read = await knowledge.client.get(
        f"{_DOCUMENTS}/{created.json()['id']}", headers=headers
    )
    assert read.json() == created.json()
    assert created.json()["metadata"] == {**fields, "custom": custom}
    # At the top level the same names are still rejected.
    top = await _post_json(knowledge, headers, _body(custom={"title": _MARK}))
    _assert_clean_422(
        top,
        caplog,
        loc=("body", "metadata", "custom"),
        error_type="reserved_metadata_key",
    )
    assert await _document_count(knowledge) == 1


def _nested(depth: int) -> dict[str, Any]:
    """``custom`` whose nesting is exactly ``depth`` containers (itself = 1)."""
    value: Any = "folha"
    for level in range(depth - 1):
        value = {"n": value} if level % 2 else [value]
    return {"raiz": value}


@pytest.mark.asyncio
async def test_custom_depth_keys_and_size_limits(
    knowledge: _Knowledge,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    headers = await _owner(knowledge)
    caplog.set_level(logging.DEBUG)
    custom_loc = ("body", "metadata", "custom")

    deep_ok = await _post_json(
        knowledge, headers, _body(custom=_nested(KNOWLEDGE_CUSTOM_MAX_DEPTH))
    )
    assert deep_ok.status_code == 201, deep_ok.text
    too_deep = await _post_json(
        knowledge, headers, _body(custom=_nested(KNOWLEDGE_CUSTOM_MAX_DEPTH + 1))
    )
    _assert_clean_422(too_deep, caplog, loc=custom_loc, error_type="metadata_too_deep")
    very_deep = await _post_raw(
        knowledge,
        headers,
        b'{"content": "conteudo valido", "metadata": {"custom": {"a": '
        + b"[" * 200
        + b"]" * 200
        + b"}}}",
    )
    _assert_clean_422(very_deep, caplog, loc=custom_loc, error_type="metadata_too_deep")

    keys_ok: dict[str, Any] = {f"k{i}": i for i in range(KNOWLEDGE_CUSTOM_MAX_KEYS - 2)}
    keys_ok["n"] = {"x": True}  # nested keys count too: 100 in total
    created = await _post_json(knowledge, headers, _body(custom=keys_ok))
    assert created.status_code == 201, created.text
    too_many = dict(keys_ok, extra={"y": None})
    too_many_keys = await _post_json(knowledge, headers, _body(custom=too_many))
    _assert_clean_422(
        too_many_keys, caplog, loc=custom_loc, error_type="metadata_too_many_keys"
    )

    # Compact UTF-8 JSON: {"b":"<filler>"} is 8 bytes of framing.
    filler = KNOWLEDGE_CUSTOM_MAX_BYTES - 8
    size_ok = {"b": "é" * (filler // 2)}
    assert len(
        json.dumps(size_ok, ensure_ascii=False, separators=(",", ":")).encode()
    ) == (KNOWLEDGE_CUSTOM_MAX_BYTES)
    sized = await _post_json(knowledge, headers, _body(custom=size_ok))
    assert sized.status_code == 201, sized.text
    assert (await _row(knowledge, sized.json()["id"]))["metadata"]["b"] == size_ok["b"]
    too_large = await _post_json(
        knowledge, headers, _body(custom={"b": _MARK + "x" * filler})
    )
    _assert_clean_422(
        too_large, caplog, loc=custom_loc, error_type="metadata_too_large"
    )
    assert await _document_count(knowledge) == 3


# --------------------------------------------------------------------------
# The list's category filter
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_category_filter_rejects_nul_and_oversize(
    knowledge: _Knowledge,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    headers = await _owner(knowledge)
    caplog.set_level(logging.DEBUG)
    category = "ç" * KNOWLEDGE_CATEGORY_MAX_LENGTH
    created = await _post_json(knowledge, headers, _body(category=category))
    assert created.status_code == 201, created.text
    loc = ("query", "category")

    encoded = await knowledge.client.get(
        f"{_DOCUMENTS}?category={_MARK}%00", headers=headers
    )
    _assert_clean_422(encoded, caplog, loc=loc, error_type="nul_character")
    as_param = await knowledge.client.get(
        _DOCUMENTS, params={"category": f"\x00{_MARK}"}, headers=headers
    )
    _assert_clean_422(as_param, caplog, loc=loc, error_type="nul_character")
    over = await knowledge.client.get(
        _DOCUMENTS, params={"category": _MARK + category}, headers=headers
    )
    _assert_clean_422(over, caplog, loc=loc, error_type="string_too_long")

    at_limit = await knowledge.client.get(
        _DOCUMENTS, params={"category": category}, headers=headers
    )
    assert at_limit.status_code == 200, at_limit.text
    assert [d["id"] for d in at_limit.json()["documents"]] == [created.json()["id"]]
    assert at_limit.json()["total"] == 1


# --------------------------------------------------------------------------
# The upload response is what is stored
# --------------------------------------------------------------------------

_UNUSUAL_METADATA: list[dict[str, Any]] = [
    {
        "title": "Atenção às crianças: ÁÉÍÓÚ çãõ ñ",
        "source": "https://exemplo.com.br/protocolo?año=2026&ç=1",
        "category": "pediatria-ção",
        "tags": ["ação", "حروف", "עברית", "é", "👩‍⚕️", "𝄞"],
        "custom": {
            "versão": 2,
            "preço": 19.9,
            "ativo": True,
            "nulo": None,
            "lista": [1, "dois", 3.5, False, None, {"x": "‮RTL"}],
            "aninhado": {"a": {"b": "combińado"}},
            "": "chave vazia",
            "grande": 10**30,
            "zero": -0.0,
        },
    },
    {"title": "", "source": "", "category": "", "tags": ["", " "], "custom": {}},
    {},
    {"title": None, "tags": []},
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata", _UNUSUAL_METADATA, ids=["unicode", "empty", "absent", "nulls"]
)
async def test_upload_response_equals_what_get_list_and_the_row_return(
    knowledge: _Knowledge,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
    metadata: dict[str, Any],
) -> None:
    headers = await _owner(knowledge)
    caplog.set_level(logging.DEBUG)
    content = "Conteúdo legítimo com acentuação, עברית, 😀 e marcas é. " * 200

    created = await _post_json(
        knowledge, headers, {"content": content, "metadata": metadata}
    )

    assert created.status_code == 201, created.text
    uploaded = created.json()
    document_id = uploaded["id"]
    read = await knowledge.client.get(f"{_DOCUMENTS}/{document_id}", headers=headers)
    assert read.status_code == 200, read.text
    listed = await knowledge.client.get(_DOCUMENTS, headers=headers)
    assert listed.status_code == 200, listed.text
    (entry,) = listed.json()["documents"]
    # Every field, not only the metadata.
    assert uploaded == read.json() == entry

    row = await _row(knowledge, document_id)
    assert row["content"] == content
    stored = row["metadata"]
    expected = {
        "title": metadata.get("title"),
        "source": metadata.get("source"),
        "category": metadata.get("category"),
        "tags": metadata.get("tags", []),
    }
    assert {key: stored[key] for key in expected} == expected
    assert uploaded["metadata"] == {**expected, "custom": metadata.get("custom", {})}
    assert {k: v for k, v in stored.items() if k not in expected} == metadata.get(
        "custom", {}
    )
    assert row["title"] == (metadata.get("title") or "Untitled Document")
    for forbidden in _LOG_FORBIDDEN:
        assert forbidden not in caplog.text, forbidden
