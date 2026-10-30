"""Contract tests for the additive Group 4b S3 adapter."""

from __future__ import annotations

import base64
import hashlib
from unittest.mock import Mock, patch
from uuid import UUID

import pytest
from botocore.exceptions import ClientError

from src.config.settings import Settings
from src.storage.credit_object_store import (
    ObjectCopyError,
    StoredCopy,
    copy_object_key,
    delete_exact_copy,
    put_immutable_copy,
)


def _settings() -> Settings:
    return Settings(
        credit_object_store_endpoint="http://127.0.0.1:9002",
        credit_object_store_bucket="copies",
        credit_object_store_access_key_id="test-access",
        credit_object_store_secret_access_key="test-secret",
    )


def test_copy_key_is_tenant_scoped_and_uses_lowercase_digest() -> None:
    tenant_id = UUID("00000000-0000-4000-8000-000000000001")
    object_id = UUID("00000000-0000-4000-8000-000000000002")
    digest = "a" * 64

    assert copy_object_key(tenant_id, object_id, digest) == (
        f"tenants/{tenant_id}/objects/{object_id}/versions/{digest}"
    )


@patch("src.storage.credit_object_store._client")
def test_put_records_exact_version_without_changing_authoritative_bytes(
    client_factory: Mock,
) -> None:
    client = client_factory.return_value
    client.get_bucket_versioning.return_value = {"Status": "Enabled"}
    client.get_object_lock_configuration.return_value = {
        "ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled"}
    }
    client.put_object.return_value = {"VersionId": "v1", "ETag": '"etag"'}
    client.head_object.return_value = {
        "VersionId": "v1",
        "ETag": '"etag"',
        "ContentLength": 4,
        "ChecksumSHA256": base64.b64encode(hashlib.sha256(b"copy").digest()).decode(),
    }
    payload = b"copy"

    stored = put_immutable_copy(
        tenant_id=UUID("00000000-0000-4000-8000-000000000001"),
        object_id=UUID("00000000-0000-4000-8000-000000000002"),
        payload=payload,
        expected_sha256=hashlib.sha256(payload).digest(),
        config=_settings(),
    )

    assert stored.version_id == "v1"
    assert stored.etag == "etag"
    assert stored.content_sha256 == hashlib.sha256(payload).hexdigest()
    assert client.put_object.call_args.kwargs["Body"] == payload
    assert "ObjectLockMode" not in client.put_object.call_args.kwargs


@patch("src.storage.credit_object_store._client")
def test_put_rejects_same_length_remote_digest_divergence(
    client_factory: Mock,
) -> None:
    """A same-sized corrupt object is not registered as a valid copy."""

    client = client_factory.return_value
    client.get_bucket_versioning.return_value = {"Status": "Enabled"}
    client.get_object_lock_configuration.return_value = {
        "ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled"}
    }
    client.put_object.return_value = {"VersionId": "v1", "ETag": '"etag"'}
    client.head_object.return_value = {
        "VersionId": "v1",
        "ETag": '"etag"',
        "ContentLength": 4,
        "ChecksumSHA256": base64.b64encode(hashlib.sha256(b"evil").digest()).decode(),
    }

    with pytest.raises(ObjectCopyError, match="digest differs"):
        put_immutable_copy(
            tenant_id=UUID("00000000-0000-4000-8000-000000000001"),
            object_id=UUID("00000000-0000-4000-8000-000000000002"),
            payload=b"copy",
            expected_sha256=hashlib.sha256(b"copy").digest(),
            config=_settings(),
        )


@patch("src.storage.credit_object_store._client")
def test_delete_requires_and_verifies_the_registered_version(
    client_factory: Mock,
) -> None:
    client = client_factory.return_value
    client.get_object_legal_hold.return_value = {"LegalHold": {"Status": "OFF"}}
    client.get_paginator.return_value.paginate.return_value = [
        {"Versions": [], "DeleteMarkers": []}
    ]
    client.head_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchVersion", "Message": "gone"}}, "HeadObject"
    )
    copy = StoredCopy("tenants/t/object", "v1", "etag", "a" * 64)

    delete_exact_copy(copy=copy, config=_settings())

    assert client.delete_object.call_args.kwargs == {
        "Bucket": "copies",
        "Key": "tenants/t/object",
        "VersionId": "v1",
    }
    assert client.head_object.call_args.kwargs["VersionId"] == "v1"
