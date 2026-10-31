"""Small S3-compatible adapter for Group 4b's *additional* object copies.

The adapter is deliberately synchronous at the boto boundary and invoked with
``asyncio.to_thread`` by the worker.  It exposes exact-version operations so
collection and future legal-hold code cannot accidentally delete a newer
version.  It never reads a copy to answer an application request.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from uuid import UUID

import boto3
from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError

from src.config.settings import Settings, settings


class ObjectCopyError(RuntimeError):
    """An S3 copy operation failed without changing the authoritative source."""


class ObjectCopyHeld(ObjectCopyError):
    """The exact version is protected by Object Lock legal hold."""


@dataclass(frozen=True)
class StoredCopy:
    object_key: str
    version_id: str
    etag: str
    content_sha256: str


def copy_object_key(tenant_id: UUID, object_id: UUID, content_sha256: str) -> str:
    """Stable tenant-scoped immutable key; validate the digest before I/O."""

    if len(content_sha256) != 64 or any(
        c not in "0123456789abcdef" for c in content_sha256
    ):
        raise ValueError("content_sha256 must be lowercase SHA-256 hex")
    return f"tenants/{tenant_id}/objects/{object_id}/versions/{content_sha256}"


def _client(config: Settings = settings) -> BaseClient:
    if not (
        config.credit_object_store_endpoint
        and config.credit_object_store_bucket
        and config.credit_object_store_access_key_id
        and config.credit_object_store_secret_access_key.get_secret_value()
    ):
        raise ObjectCopyError("credit object store is not configured")
    return boto3.client(
        "s3",
        endpoint_url=config.credit_object_store_endpoint,
        aws_access_key_id=config.credit_object_store_access_key_id,
        aws_secret_access_key=config.credit_object_store_secret_access_key.get_secret_value(),
        region_name="us-east-1",
    )


def assert_bucket_object_lock(config: Settings = settings) -> None:
    """Fail closed unless versioning and Object Lock are both enabled.

    Object Lock is only meaningful for an immutable object version.  Checking
    both controls before a copy means a superficially compatible endpoint
    cannot silently turn the registry's ``VersionId`` into a best-effort key.
    """

    try:
        client = _client(config)
        versioning = client.get_bucket_versioning(
            Bucket=config.credit_object_store_bucket
        )
        result = client.get_object_lock_configuration(
            Bucket=config.credit_object_store_bucket
        )
    except (ClientError, BotoCoreError) as error:
        raise ObjectCopyError("could not read S3 bucket copy controls") from error
    if versioning.get("Status") != "Enabled":
        raise ObjectCopyError("S3 bucket versioning must be Enabled")
    enabled = result.get("ObjectLockConfiguration", {}).get("ObjectLockEnabled")
    if enabled != "Enabled":
        raise ObjectCopyError("S3 bucket Object Lock must be Enabled")


def put_immutable_copy(
    *,
    tenant_id: UUID,
    object_id: UUID,
    payload: bytes,
    expected_sha256: bytes,
    config: Settings = settings,
) -> StoredCopy:
    """Write and verify one versioned additional copy.

    The bucket is Object-Lock enabled but has no default retention, so
    ordinary source collection can delete the exact version. Group 5 uses
    GOVERNANCE legal holds through :func:`set_legal_hold` when an explicit
    hold is required.
    """

    digest = hashlib.sha256(payload).digest()
    if digest != expected_sha256:
        raise ObjectCopyError("authoritative payload digest mismatch")
    content_sha256 = digest.hex()
    # Keep the binary source digest as the authority.  The registry's hex
    # representation is only an immutable key component; it must round trip
    # precisely to ``media_assets.sha256`` (or a knowledge-content digest).
    if bytes.fromhex(content_sha256) != expected_sha256:
        raise ObjectCopyError("source SHA-256 hex does not round trip")
    key = copy_object_key(tenant_id, object_id, content_sha256)
    client = _client(config)
    try:
        assert_bucket_object_lock(config)
        response = client.put_object(
            Bucket=config.credit_object_store_bucket,
            Key=key,
            Body=payload,
            ChecksumAlgorithm="SHA256",
        )
        version_id = response.get("VersionId")
        if not isinstance(version_id, str) or not version_id:
            raise ObjectCopyError("S3 versioning is required for object copies")
        head = client.head_object(
            Bucket=config.credit_object_store_bucket, Key=key, VersionId=version_id
        )
    except (ClientError, BotoCoreError) as error:
        raise ObjectCopyError("S3 put/head failed") from error
    # S3 returns the checksum in base64 whereas the registry pins lowercase
    # hex. Content length is not an integrity check: a same-length divergent
    # object must never be registered as an additional copy.
    if int(head.get("ContentLength", -1)) != len(payload):
        raise ObjectCopyError("S3 copy size differs from authoritative payload")
    remote_checksum = head.get("ChecksumSHA256")
    if isinstance(remote_checksum, str):
        try:
            remote_digest = base64.b64decode(remote_checksum, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ObjectCopyError(
                "S3 copy returned an invalid SHA-256 checksum"
            ) from error
    else:
        # Some S3-compatible servers (including the disposable RustFS test
        # service) do not expose ChecksumSHA256 in HeadObject. Hash the exact
        # version bytes in that case; accepting ContentLength alone would let
        # a same-sized divergent copy enter the registry.
        try:
            remote = client.get_object(
                Bucket=config.credit_object_store_bucket,
                Key=key,
                VersionId=version_id,
            )
            remote_digest = hashlib.sha256(remote["Body"].read()).digest()
        except (ClientError, BotoCoreError, KeyError, AttributeError) as error:
            raise ObjectCopyError("could not verify S3 copy bytes") from error
    if remote_digest != digest:
        raise ObjectCopyError("S3 copy digest differs from authoritative payload")
    etag = str(head.get("ETag") or response.get("ETag") or "").strip('"')
    if not etag:
        raise ObjectCopyError("S3 copy did not return an ETag")
    return StoredCopy(key, version_id, etag, content_sha256)


def delete_exact_copy(*, copy: StoredCopy, config: Settings = settings) -> None:
    """Delete only the registered version and prove that version vanished."""

    client = _client(config)
    try:
        hold = client.get_object_legal_hold(
            Bucket=config.credit_object_store_bucket,
            Key=copy.object_key,
            VersionId=copy.version_id,
        )
        if hold.get("LegalHold", {}).get("Status") == "ON":
            raise ObjectCopyHeld("S3 object version is under legal hold")
        client.delete_object(
            Bucket=config.credit_object_store_bucket,
            Key=copy.object_key,
            VersionId=copy.version_id,
        )
        client.head_object(
            Bucket=config.credit_object_store_bucket,
            Key=copy.object_key,
            VersionId=copy.version_id,
        )
    except ObjectCopyHeld:
        raise
    except BotoCoreError as error:
        # A connection/endpoint failure (not an S3 answer): nothing proven.
        raise ObjectCopyError("S3 exact-version delete/verification failed") from error
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        if code in {"AccessDenied", "InvalidRequest"}:
            raise ObjectCopyHeld("S3 object version is under legal hold") from error
        if code not in {"404", "NoSuchKey", "NoSuchVersion"}:
            raise ObjectCopyError(
                "S3 exact-version delete/verification failed"
            ) from error
    else:
        raise ObjectCopyError("S3 exact version remains after delete")

    try:
        pages = client.get_paginator("list_object_versions").paginate(
            Bucket=config.credit_object_store_bucket,
            Prefix=copy.object_key,
        )
        for page in pages:
            versions = (*page.get("Versions", ()), *page.get("DeleteMarkers", ()))
            if any(
                entry.get("Key") == copy.object_key
                and entry.get("VersionId") == copy.version_id
                for entry in versions
            ):
                raise ObjectCopyError("S3 exact version remains in version listing")
    except ObjectCopyError:
        raise
    except (ClientError, BotoCoreError) as error:
        raise ObjectCopyError("S3 exact-version listing failed") from error


def set_legal_hold(
    *, copy: StoredCopy, enabled: bool, config: Settings = settings
) -> None:
    """``PutObjectLegalHold`` on the exact version (the Group 4b worker sets
    it from the credit legal hold in PostgreSQL)."""

    try:
        _client(config).put_object_legal_hold(
            Bucket=config.credit_object_store_bucket,
            Key=copy.object_key,
            VersionId=copy.version_id,
            LegalHold={"Status": "ON" if enabled else "OFF"},
        )
    except (ClientError, BotoCoreError) as error:
        raise ObjectCopyError("S3 legal-hold update failed") from error


__all__ = [
    "ObjectCopyError",
    "ObjectCopyHeld",
    "StoredCopy",
    "assert_bucket_object_lock",
    "copy_object_key",
    "delete_exact_copy",
    "put_immutable_copy",
    "set_legal_hold",
]
