"""Final public OpenAPI contract for the five Media Assets operations."""

from typing import Any, cast

from fastapi.openapi.models import OpenAPI

from src.api.main import create_app

MEDIA_OPERATIONS = {
    ("/v1/media-assets", "post"),
    ("/v1/media-assets", "get"),
    ("/v1/media-assets/{media_id}/content", "get"),
    ("/v1/media-assets/{media_id}", "patch"),
    ("/v1/media-assets/{media_id}", "delete"),
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


def test_all_five_operations_publish_tenant_override_and_correct_security() -> None:
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

    assert request_body["x-maxBytes"] == 51_000_000
    assert multipart["additionalProperties"] is False
    assert multipart["maxProperties"] == 5
    assert multipart["required"] == ["file", "display_name", "original_filename"]
    assert set(multipart["properties"]) == {
        "file",
        "display_name",
        "original_filename",
        "usage_trigger",
        "auto_suggest",
    }
    assert multipart["properties"]["file"]["format"] == "binary"
    assert multipart["properties"]["file"]["x-maxBytes"] == 50_000_000
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
    assert response["size_bytes"]["exclusiveMinimum"] == 0
    assert response["size_bytes"]["maximum"] == 50_000_000
    assert response["usage_trigger"]["anyOf"][0]["maxLength"] == 500
    assert patch["additionalProperties"] is False
    assert patch["minProperties"] == 1
    assert patch["properties"]["auto_suggest"] == {"type": "boolean"}
    assert patch["properties"]["usage_trigger"]["anyOf"][1] == {"type": "null"}

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

    download = _operation(schema, "/v1/media-assets/{media_id}/content", "get")
    content = download["responses"]["200"]["content"]
    assert set(content) == {
        "video/mp4",
        "audio/mpeg",
        "application/pdf",
        "image/jpeg",
        "image/png",
    }
    assert all(
        value["schema"] == {"type": "string", "format": "binary"}
        for value in content.values()
    )


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
