"""Real S3-compatible contract for Group 4b's additive copy adapter.

The broad suite may skip this module when no disposable object store is
configured.  ``scripts/check-credit-integration.sh`` deliberately refuses
that skip for the Group 4b gate.
"""

from __future__ import annotations

import hashlib
import os
from base64 import b64encode
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest

from src.config.settings import Settings
from src.storage.credit_object_store import (
    ObjectCopyError,
    assert_bucket_object_lock,
    copy_object_key,
    delete_exact_copy,
    put_immutable_copy,
)

pytestmark = pytest.mark.integration


def _config() -> Settings:
    required = (
        "CREDIT_TEST_S3_ENDPOINT",
        "CREDIT_TEST_S3_BUCKET",
        "CREDIT_TEST_S3_ACCESS_KEY_ID",
        "CREDIT_TEST_S3_SECRET_ACCESS_KEY",
    )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        pytest.skip("S3 copy test configuration is absent: " + ", ".join(missing))
    return Settings(
        credit_object_store_endpoint=os.environ["CREDIT_TEST_S3_ENDPOINT"],
        credit_object_store_bucket=os.environ["CREDIT_TEST_S3_BUCKET"],
        credit_object_store_access_key_id=os.environ["CREDIT_TEST_S3_ACCESS_KEY_ID"],
        credit_object_store_secret_access_key=os.environ[
            "CREDIT_TEST_S3_SECRET_ACCESS_KEY"
        ],
    )


def _mock_config() -> Settings:
    return Settings(
        credit_object_store_endpoint="http://s3.invalid",
        credit_object_store_bucket="copies",
        credit_object_store_access_key_id="test-access",
        credit_object_store_secret_access_key="test-secret",
    )


def test_disposable_bucket_has_versioning_and_object_lock() -> None:
    assert_bucket_object_lock(_config())


def test_copy_is_sha_identical_and_exact_version_collectable() -> None:
    config = _config()
    tenant_id, object_id = uuid4(), uuid4()
    payload = b"group-4b-s3-authoritative-source"
    source_sha256 = hashlib.sha256(payload).digest()

    copy = put_immutable_copy(
        tenant_id=tenant_id,
        object_id=object_id,
        payload=payload,
        expected_sha256=source_sha256,
        config=config,
    )
    assert bytes.fromhex(copy.content_sha256) == source_sha256
    assert copy.object_key == copy_object_key(tenant_id, object_id, copy.content_sha256)
    delete_exact_copy(copy=copy, config=config)


@patch("src.storage.credit_object_store._client")
def test_same_length_remote_checksum_divergence_is_rejected(
    client_factory: Mock,
) -> None:
    """The adapter never registers a same-length but corrupt remote copy."""

    client = client_factory.return_value
    client.get_bucket_versioning.return_value = {"Status": "Enabled"}
    client.get_object_lock_configuration.return_value = {
        "ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled"}
    }
    client.put_object.return_value = {"VersionId": "v1", "ETag": '"etag"'}
    payload = b"copy"
    client.head_object.return_value = {
        "ContentLength": len(payload),
        "ChecksumSHA256": b64encode(hashlib.sha256(b"evil").digest()).decode(),
        "ETag": '"etag"',
    }

    with pytest.raises(ObjectCopyError, match="digest differs"):
        put_immutable_copy(
            tenant_id=uuid4(),
            object_id=uuid4(),
            payload=payload,
            expected_sha256=hashlib.sha256(payload).digest(),
            config=_mock_config(),
        )
