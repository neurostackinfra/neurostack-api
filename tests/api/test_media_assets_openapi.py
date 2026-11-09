"""Final public OpenAPI contract for the six Media Assets operations."""

from typing import Any, cast

from fastapi.openapi.models import OpenAPI

from neurostack_core.protocol import MEDIA_BINDABLE_INTENT_SEQUENCE
from src.api.main import create_app

MEDIA_OPERATIONS = {
    ("/v1/media-assets", "post"),
    ("/v1/media-assets", "get"),
    ("/v1/media-assets/bindable-intents", "get"),
    ("/v1/media-assets/{media_id}/content", "get"),
    ("/v1/media-assets/{media_id}", "patch"),
    ("/v1/media-assets/{media_id}", "delete"),
}
_TRIGGER_INTENTS = {
    "type": "array",
    "items": {"type": "string", "enum": list(MEDIA_BINDABLE_INTENT_SEQUENCE)},
    "maxItems": 12,
}
MEDIA_SECURITY: list[dict[str, list[str]]] = [
    {"ApiKeyAuth": []},
    {"BearerAuth": []},
]


def _schema() -> dict[str, Any]:
    return create_app().openapi()


def _operation(schema: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    return cast(dict[str, Any], schema["paths"][path][method])


def _parameter(operation: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in operation["parameters"] if item["name"] == name)


def test_complete_document_is_valid_openapi_31() -> None:
    schema = _schema()

    assert schema["openapi"] == "3.1.0"
    OpenAPI.model_validate(schema)


def test_all_six_operations_publish_tenant_override_and_correct_security() -> None:
    schema = _schema()
    published = {
        (path, method)
        for path, item in schema["paths"].items()
        if path.startswith("/v1/media-assets")
        for method in item
        if method in {"get", "post", "patch", "delete"}
    }
    assert published == MEDIA_OPERATIONS

    for path, method in MEDIA_OPERATIONS:
        operation = _operation(schema, path, method)
        assert operation["security"] == MEDIA_SECURITY
        assert _parameter(operation, "tenant_id")["schema"]["anyOf"][0] == {
            "type": "string",
            "format": "uuid",
        }
        names = {parameter["name"] for parameter in operation["parameters"]}
        assert "X-API-Key" not in names
        assert "X-Request-ID" in names


def test_upload_documents_closed_single_file_multipart_and_byte_limits() -> None:
    operation = _operation(_schema(), "/v1/media-assets", "post")
    request_body = operation["requestBody"]
    multipart = request_body["content"]["multipart/form-data"]["schema"]

    # The request cap is the largest category (PDF) plus the multipart overhead;
    # the file cap is that largest category alone.
    assert request_body["x-maxBytes"] == 21_000_000
    assert multipart["additionalProperties"] is False
    assert multipart["maxProperties"] == 6
    assert multipart["required"] == ["file", "display_name", "original_filename"]
    assert set(multipart["properties"]) == {
        "file",
        "display_name",
        "original_filename",
        "usage_trigger",
        "auto_suggest",
        "trigger_intents",
    }
    # Repeated once per bound intent; at most one part per bindable intent.
    assert multipart["properties"]["trigger_intents"] == _TRIGGER_INTENTS
    assert "media_trigger_intent_unknown" in request_body["description"]
    assert multipart["properties"]["file"]["format"] == "binary"
    assert multipart["properties"]["file"]["x-maxBytes"] == 20_000_000
    assert multipart["properties"]["display_name"]["maxLength"] == 200
    assert multipart["properties"]["original_filename"]["maxLength"] == 255
    assert multipart["properties"]["usage_trigger"]["maxLength"] == 500
    assert multipart["properties"]["auto_suggest"] == {
        "type": "string",
        "enum": ["true", "false"],
        "default": "false",
    }


def test_list_patch_response_and_download_schemas_publish_exact_limits() -> None:
    schema = _schema()
    components = schema["components"]["schemas"]
    response = components["MediaAssetResponse"]["properties"]
    patch = components["MediaAssetPatchRequest"]

    assert response["display_name"]["minLength"] == 1
    assert response["display_name"]["maxLength"] == 200
    assert response["original_filename"]["maxLength"] == 255
    # A stored type missing here is a 500 on the very upload that created it.
    assert set(response["mime_type"]["enum"]) == {
        "video/mp4",
        "audio/mpeg",
        "audio/ogg",
        "audio/mp4",
        "application/pdf",
        "image/jpeg",
        "image/png",
    }
    assert response["size_bytes"]["exclusiveMinimum"] == 0
    # Wider than any upload cap on purpose: it describes what a stored row may
    # report, including rows that predate the per-category limits.
    assert response["size_bytes"]["maximum"] == 50_000_000
    assert response["usage_trigger"]["anyOf"][0]["maxLength"] == 500
    assert patch["additionalProperties"] is False
    assert patch["minProperties"] == 1
    assert patch["properties"]["auto_suggest"] == {"type": "boolean"}
    assert patch["properties"]["usage_trigger"]["anyOf"][1] == {"type": "null"}
    assert patch["properties"]["trigger_intents"] == _TRIGGER_INTENTS
    # Every response carries the bound intents (possibly empty).
    assert "trigger_intents" in components["MediaAssetResponse"]["required"]
    assert response["trigger_intents"]["type"] == "array"
    assert response["trigger_intents"]["items"] == {"type": "string"}

    listing = _operation(schema, "/v1/media-assets", "get")
    assert _parameter(listing, "page_size")["schema"] == {
        "type": "integer",
        "maximum": 100,
        "minimum": 1,
        "description": (
            "Number of metadata records to return (default 25, maximum 100)."
        ),
        "default": 25,
        "title": "Page Size",
    }
    category = _parameter(listing, "category")
    assert category["required"] is False
    assert category["schema"]["anyOf"][0] == {
        "type": "string",
        "enum": ["video", "audio", "pdf", "image"],
    }
    name = _parameter(listing, "name")
    assert name["required"] is False
    assert name["schema"]["anyOf"][0] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 200,
        "pattern": "^[^\\x00\\r\\n]*$",
    }

    download = _operation(schema, "/v1/media-assets/{media_id}/content", "get")
    content = download["responses"]["200"]["content"]
    assert set(content) == {
        "video/mp4",
        "audio/mpeg",
        "audio/ogg",
        "audio/mp4",
        "application/pdf",
        "image/jpeg",
        "image/png",
    }
    assert all(
        value["schema"] == {"type": "string", "format": "binary"}
        for value in content.values()
    )


def test_upload_publishes_the_per_category_limits_unconditionally() -> None:
    operation = _operation(_schema(), "/v1/media-assets", "post")
    request_body = operation["requestBody"]
    file_property = request_body["content"]["multipart/form-data"]["schema"][
        "properties"
    ]["file"]
    prose = " ".join(
        [
            operation["description"],
            request_body["description"],
            file_property["description"],
            operation["responses"]["413"]["description"],
        ]
    )

    for limit in ("5,000,000", "20,000,000", "16,000,000"):
        assert limit in prose
    assert "OGG-Opus" in file_property["description"]
    assert "M4A" in file_property["description"]
    # The limits are a property of the category, not of the credit rollout.
    assert "credit_enforcement_at" not in prose


def test_each_operation_documents_its_stable_error_surface() -> None:
    schema = _schema()
    upload_conflict = _operation(schema, "/v1/media-assets", "post")["responses"][
        "409"
    ]["description"]
    assert "media_storage_cap_exceeded" in upload_conflict
    assert "media_storage_capacity_exceeded" not in upload_conflict

    expected = {
        ("/v1/media-assets", "post"): {"400", "401", "403", "409", "413", "422", "503"},
        ("/v1/media-assets", "get"): {"400", "401", "403", "422", "503"},
        ("/v1/media-assets/bindable-intents", "get"): {"401", "403", "422", "503"},
        ("/v1/media-assets/{media_id}/content", "get"): {
            "401",
            "403",
            "404",
            "422",
            "503",
        },
        ("/v1/media-assets/{media_id}", "patch"): {
            "401",
            "403",
            "404",
            "422",
            "503",
        },
        ("/v1/media-assets/{media_id}", "delete"): {
            "401",
            "403",
            "404",
            "422",
            "503",
        },
    }
    for key, statuses in expected.items():
        responses = _operation(schema, *key)["responses"]
        assert statuses <= set(responses)
        for status_code in statuses:
            assert responses[status_code]["description"]
            content = responses[status_code].get("content", {})
            assert "application/json" in content
            assert content["application/json"]["schema"]


def test_the_unknown_intent_code_is_documented_on_every_write() -> None:
    schema = _schema()
    for path, method in (
        ("/v1/media-assets", "post"),
        ("/v1/media-assets/{media_id}", "patch"),
    ):
        responses = _operation(schema, path, method)["responses"]
        assert "media_trigger_intent_unknown" in responses["422"]["description"]


def test_upload_documents_that_an_ogg_opus_must_be_mono() -> None:
    operation = _operation(_schema(), "/v1/media-assets", "post")
    assert "media_audio_not_mono" in operation["responses"]["422"]["description"]
    file_property = operation["requestBody"]["content"]["multipart/form-data"][
        "schema"
    ]["properties"]["file"]
    assert "OGG-Opus (mono only)" in file_property["description"]


def test_bindable_intents_publishes_the_allowlist_flag_state_and_limits() -> None:
    schema = _schema()
    operation = _operation(schema, "/v1/media-assets/bindable-intents", "get")
    reference = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert reference == {"$ref": "#/components/schemas/MediaBindableIntentsResponse"}
    components = schema["components"]["schemas"]
    body = components["MediaBindableIntentsResponse"]
    assert set(body["required"]) == {
        "intents",
        "delivery_enabled",
        "max_bytes_by_category",
        "sendable_mime_types",
    }
    assert body["properties"]["intents"]["items"] == {
        "type": "string",
        "enum": list(MEDIA_BINDABLE_INTENT_SEQUENCE),
    }
    assert body["properties"]["delivery_enabled"]["type"] == "boolean"
    sendable = body["properties"]["sendable_mime_types"]
    assert sendable["type"] == "array"
    assert sendable["items"] == {
        "type": "string",
        "enum": [
            "application/pdf",
            "audio/mp4",
            "audio/mpeg",
            "audio/ogg",
            "image/jpeg",
            "image/png",
            "video/mp4",
        ],
    }
    limits = components["MediaCategoryByteLimits"]
    assert set(limits["required"]) == {"video", "audio", "pdf", "image"}
    assert limits["additionalProperties"] is False
    # Not a media operation that reads rows: no media_id, no request body.
    assert "requestBody" not in operation
    assert {item["name"] for item in operation["parameters"]} >= {"tenant_id"}
