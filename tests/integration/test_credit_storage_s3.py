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


# ---------------------------------------------------------------------------
# Group 5: the conformance fixtures' real bytes through the disposable bucket,
# exact-version purge with confirmed absence, and retention denials.
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

from botocore.exceptions import ClientError  # noqa: E402

from src.services.knowledge_service import (  # noqa: E402
    KnowledgeFileError,
    extract_document_text,
)
from src.storage.credit_object_store import (  # noqa: E402
    ObjectCopyHeld,
    StoredCopy,
    _client,
    set_legal_hold,
)

_FIXTURES = (
    Path(__file__).resolve().parents[1] / "fixtures" / "credit_storage_conformance"
)
_POSITIVE = sorted((_FIXTURES / "positive").iterdir())
_NEGATIVE = sorted((_FIXTURES / "negative").iterdir())


def _version_listed(config: Settings, copy: StoredCopy) -> bool:
    pages = (
        _client(config)
        .get_paginator("list_object_versions")
        .paginate(Bucket=config.credit_object_store_bucket, Prefix=copy.object_key)
    )
    return any(
        entry.get("Key") == copy.object_key
        and entry.get("VersionId") == copy.version_id
        for page in pages
        for entry in (*page.get("Versions", ()), *page.get("DeleteMarkers", ()))
    )


def _head_status(config: Settings, copy: StoredCopy) -> int:
    try:
        _client(config).head_object(
            Bucket=config.credit_object_store_bucket,
            Key=copy.object_key,
            VersionId=copy.version_id,
        )
    except ClientError as error:
        return int(error.response["ResponseMetadata"]["HTTPStatusCode"])
    return 200


def _exact_bytes(config: Settings, copy: StoredCopy) -> bytes:
    response = _client(config).get_object(
        Bucket=config.credit_object_store_bucket,
        Key=copy.object_key,
        VersionId=copy.version_id,
    )
    return bytes(response["Body"].read())


def test_conformance_fixtures_are_real_bytes_parsed_as_expected() -> None:
    """Positives parse; every negative fails -- on the fixture bytes and on
    the bytes read back from their exact S3 version."""

    config = _config()
    assert len(_POSITIVE) == 2 and len(_NEGATIVE) == 5
    tenant_id = uuid4()
    for path in (*_POSITIVE, *_NEGATIVE):
        data = path.read_bytes()
        copy = put_immutable_copy(
            tenant_id=tenant_id,
            object_id=uuid4(),
            payload=data,
            expected_sha256=hashlib.sha256(data).digest(),
            config=config,
        )
        try:
            remote = _exact_bytes(config, copy)
            assert remote == data
            assert hashlib.sha256(remote).hexdigest() == copy.content_sha256
            if path in _POSITIVE:
                assert extract_document_text(remote).text, path.name
            else:
                with pytest.raises(KnowledgeFileError, match="knowledge_file_un"):
                    extract_document_text(remote)
        finally:
            delete_exact_copy(copy=copy, config=config)


def test_exact_version_purge_confirms_absence_and_keeps_other_versions() -> None:
    config = _config()
    tenant_id, object_id = uuid4(), uuid4()
    first = _POSITIVE[0].read_bytes()
    second = first + b"\n% a later version of the same object"
    older = put_immutable_copy(
        tenant_id=tenant_id,
        object_id=object_id,
        payload=first,
        expected_sha256=hashlib.sha256(first).digest(),
        config=config,
    )
    newer = put_immutable_copy(
        tenant_id=tenant_id,
        object_id=object_id,
        payload=second,
        expected_sha256=hashlib.sha256(second).digest(),
        config=config,
    )
    try:
        delete_exact_copy(copy=older, config=config)
        assert _head_status(config, older) == 404
        assert not _version_listed(config, older)
        # Only the exact VersionId went: the other object version survives.
        assert _exact_bytes(config, newer) == second
    finally:
        delete_exact_copy(copy=newer, config=config)


def test_legal_hold_denies_the_purge_and_the_version_survives() -> None:
    config = _config()
    data = _POSITIVE[1].read_bytes()
    copy = put_immutable_copy(
        tenant_id=uuid4(),
        object_id=uuid4(),
        payload=data,
        expected_sha256=hashlib.sha256(data).digest(),
        config=config,
    )
    set_legal_hold(copy=copy, enabled=True, config=config)
    try:
        with pytest.raises(ObjectCopyHeld, match="legal hold"):
            delete_exact_copy(copy=copy, config=config)
        assert _exact_bytes(config, copy) == data
        assert _version_listed(config, copy)
    finally:
        set_legal_hold(copy=copy, enabled=False, config=config)
        delete_exact_copy(copy=copy, config=config)
    assert not _version_listed(config, copy)


def test_governance_retention_denies_the_purge() -> None:
    config = _config()
    data = _NEGATIVE[0].read_bytes()
    copy = put_immutable_copy(
        tenant_id=uuid4(),
        object_id=uuid4(),
        payload=data,
        expected_sha256=hashlib.sha256(data).digest(),
        config=config,
    )
    client = _client(config)
    client.put_object_retention(
        Bucket=config.credit_object_store_bucket,
        Key=copy.object_key,
        VersionId=copy.version_id,
        Retention={
            "Mode": "GOVERNANCE",
            "RetainUntilDate": datetime.now(timezone.utc) + timedelta(days=1),
        },
    )
    try:
        with pytest.raises(ObjectCopyHeld):
            delete_exact_copy(copy=copy, config=config)
        assert _exact_bytes(config, copy) == data
    finally:
        client.delete_object(
            Bucket=config.credit_object_store_bucket,
            Key=copy.object_key,
            VersionId=copy.version_id,
            BypassGovernanceRetention=True,
        )
    assert not _version_listed(config, copy)
