from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import asaas_bootstrap_slot as slots  # noqa: E402

BUCKET = "neurostack-asaas-release-ledger-example"
BUCKET_ARN = f"arn:aws:s3:::{BUCKET}"
BUILD_ARTIFACT = {
    "bundle_digest": "a" * 64,
    "commit_sha": "b" * 64,
    "tree_sha": "c" * 64,
}
DEPLOYMENT_ARTIFACT = {
    "app_image_digest": "sha256:" + "d" * 64,
    "attestation_id": "5e9b5bce-4604-4d9b-880d-1d989956834c",
    "signed_envelope_sha256": "e" * 64,
    "workflow_run_id": 123456789,
}


class FakeClientError(Exception):
    def __init__(self, code: str, message: str = "sentinel-private-error") -> None:
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


def _expected_policy() -> dict[str, Any]:
    build_arn = f"{BUCKET_ARN}/{slots.BUILD_KEY}"
    deployment_arn = f"{BUCKET_ARN}/{slots.DEPLOYMENT_KEY}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "DenyInsecureTransport",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": [BUCKET_ARN, build_arn, deployment_arn],
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            },
            {
                "Sid": "RequireBuildClaimIfNoneMatchHeader",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:PutObject",
                "Resource": build_arn,
                "Condition": {"Null": {"s3:if-none-match": "true"}},
            },
            {
                "Sid": "RequireExactBuildClaimIfNoneMatchValue",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:PutObject",
                "Resource": build_arn,
                "Condition": {"StringNotEquals": {"s3:if-none-match": "*"}},
            },
            {
                "Sid": "RequireDeploymentClaimIfNoneMatchHeader",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:PutObject",
                "Resource": deployment_arn,
                "Condition": {"Null": {"s3:if-none-match": "true"}},
            },
            {
                "Sid": "RequireExactDeploymentClaimIfNoneMatchValue",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:PutObject",
                "Resource": deployment_arn,
                "Condition": {"StringNotEquals": {"s3:if-none-match": "*"}},
            },
            {
                "Sid": "DenyBuildClaimDeletion",
                "Effect": "Deny",
                "Principal": "*",
                "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"],
                "Resource": build_arn,
            },
            {
                "Sid": "DenyDeploymentClaimDeletion",
                "Effect": "Deny",
                "Principal": "*",
                "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"],
                "Resource": deployment_arn,
            },
        ],
    }


class FakeS3:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.version_pages: list[dict[str, Any]] = [{"IsTruncated": False}]
        self.version_page_index = 0
        self.fail_method: str | None = None
        self.fail_code = "AccessDenied"
        self.versioning: dict[str, Any] = {"Status": "Enabled"}
        self.object_lock: dict[str, Any] = {
            "ObjectLockConfiguration": {
                "ObjectLockEnabled": "Enabled",
                "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Years": 100}},
            }
        }
        self.encryption: dict[str, Any] = {
            "ServerSideEncryptionConfiguration": {
                "Rules": [
                    {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                ]
            }
        }
        self.public_access: dict[str, Any] = {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            }
        }
        self.policy: dict[str, Any] = _expected_policy()
        self.lifecycle: dict[str, Any] = {"Rules": []}
        self.put_response: dict[str, Any] = {"VersionId": "claim-version-1"}
        self.put_error: Exception | None = None

    def _record(self, method: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((method, kwargs))
        if self.fail_method == method:
            raise FakeClientError(self.fail_code)

    def get_bucket_versioning(self, **kwargs: Any) -> dict[str, Any]:
        self._record("get_bucket_versioning", kwargs)
        return self.versioning

    def get_object_lock_configuration(self, **kwargs: Any) -> dict[str, Any]:
        self._record("get_object_lock_configuration", kwargs)
        return self.object_lock

    def get_bucket_encryption(self, **kwargs: Any) -> dict[str, Any]:
        self._record("get_bucket_encryption", kwargs)
        return self.encryption

    def get_public_access_block(self, **kwargs: Any) -> dict[str, Any]:
        self._record("get_public_access_block", kwargs)
        return self.public_access

    def get_bucket_policy(self, **kwargs: Any) -> dict[str, str]:
        self._record("get_bucket_policy", kwargs)
        return {"Policy": json.dumps(self.policy)}

    def get_bucket_lifecycle_configuration(self, **kwargs: Any) -> dict[str, Any]:
        self._record("get_bucket_lifecycle_configuration", kwargs)
        if self.fail_method == "no_lifecycle":
            raise FakeClientError("NoSuchLifecycleConfiguration")
        return self.lifecycle

    def list_object_versions(self, **kwargs: Any) -> dict[str, Any]:
        self._record("list_object_versions", kwargs)
        if self.version_page_index >= len(self.version_pages):
            raise AssertionError("helper requested beyond terminal page")
        page = self.version_pages[self.version_page_index]
        self.version_page_index += 1
        return page

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self._record("put_object", kwargs)
        if self.put_error is not None:
            raise self.put_error
        return self.put_response


def _claim(
    s3: FakeS3, *, slot: str = slots.BUILD_SLOT, artifact: dict[str, Any] | None = None
):
    return slots.claim_bootstrap_slot(
        s3,
        bucket_name=BUCKET,
        slot=slot,
        repository_id="1152335815",
        repository_name="neurostackinfra/neurostack-api",
        workflow_path=".github/workflows/asaas-build-attestation.yml",
        workflow_ref="refs/heads/main",
        workflow_run_id=987654321,
        workflow_run_attempt=1,
        operation_id="8b84213a-2ac9-4066-8cbf-b2b0445295c0",
        environment=(
            slots.BUILD_ENVIRONMENT
            if slot == slots.BUILD_SLOT
            else slots.DEPLOYMENT_ENVIRONMENT
        ),
        artifact=artifact
        or (BUILD_ARTIFACT if slot == slots.BUILD_SLOT else DEPLOYMENT_ARTIFACT),
    )


def test_claim_checks_controls_and_all_pages_before_conditional_put() -> None:
    s3 = FakeS3()
    s3.version_pages = [
        {
            "IsTruncated": True,
            "NextKeyMarker": slots.BUILD_KEY,
            "NextVersionIdMarker": "cursor-v1",
        },
        {"IsTruncated": False, "Versions": [], "DeleteMarkers": []},
    ]

    receipt = _claim(s3)

    assert receipt.slot == slots.BUILD_SLOT
    assert receipt.key == slots.BUILD_KEY
    assert receipt.version_id == "claim-version-1"
    assert receipt.version_pages_read == 2
    method_names = [method for method, _ in s3.calls]
    assert method_names[-1] == "put_object"
    assert method_names.count("list_object_versions") == 2
    assert method_names.index("get_bucket_versioning") < method_names.index(
        "list_object_versions"
    )
    assert method_names.index("list_object_versions") < method_names.index("put_object")
    put_call = s3.calls[-1][1]
    assert put_call["Bucket"] == BUCKET
    assert put_call["Key"] == slots.BUILD_KEY
    assert put_call["IfNoneMatch"] == "*"
    assert put_call["ContentType"] == "application/json"
    assert isinstance(put_call["Body"], bytes)
    assert put_call["ContentMD5"] == base64.b64encode(
        hashlib.md5(put_call["Body"], usedforsecurity=False).digest()
    ).decode("ascii")


def test_build_claim_payload_is_canonical_and_binds_run_and_source_tuple() -> None:
    s3 = FakeS3()
    receipt = _claim(s3)
    body = s3.calls[-1][1]["Body"]
    payload = json.loads(body)

    assert (
        body
        == json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    )
    assert payload == {
        "artifact": BUILD_ARTIFACT,
        "environment": slots.BUILD_ENVIRONMENT,
        "mode": "bootstrap",
        "operation_id": "8b84213a-2ac9-4066-8cbf-b2b0445295c0",
        "repository_id": "1152335815",
        "repository_name": "neurostackinfra/neurostack-api",
        "schema_version": slots.CLAIM_SCHEMA_VERSION,
        "slot": slots.BUILD_SLOT,
        "workflow_id": "github-actions:1152335815:.github/workflows/asaas-build-attestation.yml",
        "workflow_path": ".github/workflows/asaas-build-attestation.yml",
        "workflow_ref": "refs/heads/main",
        "workflow_run_attempt": 1,
        "workflow_run_id": 987654321,
    }
    assert receipt.claim_sha256 == hashlib.sha256(body).hexdigest()


def test_deployment_claim_binds_exact_group4_artifact_tuple() -> None:
    s3 = FakeS3()
    _claim(s3, slot=slots.DEPLOYMENT_SLOT)
    payload = json.loads(s3.calls[-1][1]["Body"])

    assert payload["environment"] == slots.DEPLOYMENT_ENVIRONMENT
    assert payload["slot"] == slots.DEPLOYMENT_SLOT
    assert payload["artifact"] == DEPLOYMENT_ARTIFACT
    assert "operation_id" in payload
    assert "operation_id" not in payload["artifact"]
    assert s3.calls[-1][1]["Key"] == slots.DEPLOYMENT_KEY
    assert s3.calls[-1][1]["IfNoneMatch"] == "*"


@pytest.mark.parametrize(
    ("slot", "artifact", "environment"),
    [
        ("anything", BUILD_ARTIFACT, slots.BUILD_ENVIRONMENT),
        (slots.BUILD_SLOT, BUILD_ARTIFACT, slots.DEPLOYMENT_ENVIRONMENT),
        (slots.DEPLOYMENT_SLOT, BUILD_ARTIFACT, slots.DEPLOYMENT_ENVIRONMENT),
    ],
)
def test_claim_rejects_unsupported_slot_or_cross_slot_payload_before_aws(
    slot: str, artifact: dict[str, Any], environment: str
) -> None:
    s3 = FakeS3()
    with pytest.raises(slots.BootstrapSlotError):
        slots.build_claim_document(
            slot=slot,
            repository_id="1152335815",
            repository_name="neurostackinfra/neurostack-api",
            workflow_path=".github/workflows/release.yml",
            workflow_ref="refs/heads/main",
            workflow_run_id=1,
            workflow_run_attempt=1,
            operation_id="8b84213a-2ac9-4066-8cbf-b2b0445295c0",
            environment=environment,
            artifact=artifact,
        )
    assert s3.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository_id", "0"),
        ("repository_name", "not-a-repository"),
        ("workflow_path", "../workflow.yml"),
        ("workflow_ref", "refs/tags/v1"),
        ("workflow_ref", "refs/heads/main/../release"),
        ("workflow_run_id", 0),
        ("workflow_run_attempt", True),
        ("operation_id", "not-a-uuid"),
    ],
)
def test_claim_rejects_invalid_run_identity(field: str, value: Any) -> None:
    values: dict[str, Any] = {
        "slot": slots.BUILD_SLOT,
        "repository_id": "1152335815",
        "repository_name": "neurostackinfra/neurostack-api",
        "workflow_path": ".github/workflows/release.yml",
        "workflow_ref": "refs/heads/main",
        "workflow_run_id": 1,
        "workflow_run_attempt": 1,
        "operation_id": "8b84213a-2ac9-4066-8cbf-b2b0445295c0",
        "environment": slots.BUILD_ENVIRONMENT,
        "artifact": BUILD_ARTIFACT,
    }
    values[field] = value
    with pytest.raises(slots.BootstrapSlotError):
        slots.build_claim_document(**values)


@pytest.mark.parametrize(
    ("slot", "artifact"),
    [
        (slots.BUILD_SLOT, {**BUILD_ARTIFACT, "extra": "x"}),
        (slots.BUILD_SLOT, {**BUILD_ARTIFACT, "commit_sha": "A" * 64}),
        (slots.DEPLOYMENT_SLOT, {**DEPLOYMENT_ARTIFACT, "extra": "x"}),
        (slots.DEPLOYMENT_SLOT, {**DEPLOYMENT_ARTIFACT, "workflow_run_id": 0}),
        (
            slots.DEPLOYMENT_SLOT,
            {**DEPLOYMENT_ARTIFACT, "app_image_digest": "sha256:BAD"},
        ),
    ],
)
def test_claim_rejects_artifact_tuple_mismatch(
    slot: str, artifact: dict[str, Any]
) -> None:
    environment = (
        slots.BUILD_ENVIRONMENT
        if slot == slots.BUILD_SLOT
        else slots.DEPLOYMENT_ENVIRONMENT
    )
    with pytest.raises(slots.BootstrapSlotError):
        slots.build_claim_document(
            slot=slot,
            repository_id="1152335815",
            repository_name="neurostackinfra/neurostack-api",
            workflow_path=".github/workflows/release.yml",
            workflow_ref="refs/heads/main",
            workflow_run_id=1,
            workflow_run_attempt=1,
            operation_id="8b84213a-2ac9-4066-8cbf-b2b0445295c0",
            environment=environment,
            artifact=artifact,
        )


@pytest.mark.parametrize(
    ("versions", "delete_markers"),
    [
        ([{"Key": slots.BUILD_KEY, "VersionId": "v1"}], []),
        ([], [{"Key": slots.BUILD_KEY, "VersionId": "d1"}]),
    ],
)
def test_any_existing_version_or_delete_marker_permanently_blocks_claim(
    versions: list[dict[str, Any]], delete_markers: list[dict[str, Any]]
) -> None:
    s3 = FakeS3()
    s3.version_pages = [
        {"IsTruncated": False, "Versions": versions, "DeleteMarkers": delete_markers}
    ]

    with pytest.raises(slots.BootstrapSlotError, match="permanently consumed"):
        _claim(s3)

    assert all(method != "put_object" for method, _ in s3.calls)


def test_prefix_neighbor_key_is_rejected_even_when_exact_key_is_absent() -> None:
    s3 = FakeS3()
    s3.version_pages = [
        {"IsTruncated": False, "Versions": [{"Key": slots.BUILD_KEY + ".old"}]}
    ]

    with pytest.raises(slots.BootstrapSlotError, match="unexpected key"):
        _claim(s3)

    assert all(method != "put_object" for method, _ in s3.calls)


def test_truncated_version_pages_follow_both_markers_and_require_terminal_page() -> (
    None
):
    s3 = FakeS3()
    s3.version_pages = [
        {
            "IsTruncated": True,
            "NextKeyMarker": slots.BUILD_KEY,
            "NextVersionIdMarker": "v1",
        },
        {
            "IsTruncated": True,
            "NextKeyMarker": slots.BUILD_KEY,
            "NextVersionIdMarker": "v2",
        },
        {"IsTruncated": False},
    ]

    assert slots.read_empty_slot(s3, bucket_name=BUCKET, slot=slots.BUILD_SLOT) == 3
    list_calls = [
        kwargs for method, kwargs in s3.calls if method == "list_object_versions"
    ]
    assert list_calls[0] == {"Bucket": BUCKET, "Prefix": slots.BUILD_KEY}
    assert list_calls[1]["KeyMarker"] == slots.BUILD_KEY
    assert list_calls[1]["VersionIdMarker"] == "v1"
    assert list_calls[2]["VersionIdMarker"] == "v2"


def test_repeated_pagination_cursor_fails_closed() -> None:
    s3 = FakeS3()
    s3.version_pages = [
        {
            "IsTruncated": True,
            "NextKeyMarker": slots.BUILD_KEY,
            "NextVersionIdMarker": "v1",
        },
        {
            "IsTruncated": True,
            "NextKeyMarker": slots.BUILD_KEY,
            "NextVersionIdMarker": "v1",
        },
    ]

    with pytest.raises(slots.BootstrapSlotError, match="repeated pagination cursor"):
        slots.read_empty_slot(s3, bucket_name=BUCKET, slot=slots.BUILD_SLOT)


@pytest.mark.parametrize(
    "page",
    [
        {"Versions": []},
        {"IsTruncated": True, "Versions": []},
        {"IsTruncated": False, "Versions": None},
        {"IsTruncated": True, "NextKeyMarker": ""},
    ],
)
def test_incomplete_or_malformed_version_pages_fail_closed(
    page: dict[str, Any],
) -> None:
    s3 = FakeS3()
    s3.version_pages = [page]
    with pytest.raises(slots.BootstrapSlotError):
        slots.read_empty_slot(s3, bucket_name=BUCKET, slot=slots.BUILD_SLOT)


def test_version_list_api_failure_is_sanitized_and_prevents_write() -> None:
    s3 = FakeS3()
    s3.fail_method = "list_object_versions"
    with pytest.raises(slots.BootstrapSlotError) as error:
        _claim(s3)
    assert "sentinel-private-error" not in str(error.value)
    assert "version history: read failed" in str(error.value)
    assert all(method != "put_object" for method, _ in s3.calls)


def test_no_lifecycle_configuration_is_accepted_but_lifecycle_rule_blocks() -> None:
    s3 = FakeS3()
    s3.fail_method = "no_lifecycle"
    slots.verify_bucket_controls(s3, bucket_name=BUCKET, expected_bucket_arn=BUCKET_ARN)

    s3 = FakeS3()
    s3.lifecycle = {"Rules": [{"Status": "Enabled", "Expiration": {"Days": 365}}]}
    with pytest.raises(slots.BootstrapSlotError, match="lifecycle"):
        slots.verify_bucket_controls(s3, bucket_name=BUCKET)


@pytest.mark.parametrize(
    ("attribute", "value", "message"),
    [
        ("versioning", {"Status": "Suspended"}, "versioning"),
        (
            "object_lock",
            {
                "ObjectLockConfiguration": {
                    "ObjectLockEnabled": "Enabled",
                    "Rule": {"DefaultRetention": {"Mode": "GOVERNANCE", "Years": 100}},
                }
            },
            "object lock",
        ),
        (
            "encryption",
            {
                "ServerSideEncryptionConfiguration": {
                    "Rules": [
                        {
                            "ApplyServerSideEncryptionByDefault": {
                                "SSEAlgorithm": "aws:kms"
                            }
                        }
                    ]
                }
            },
            "encryption",
        ),
        (
            "public_access",
            {
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True,
                    "BlockPublicPolicy": True,
                    "IgnorePublicAcls": True,
                    "RestrictPublicBuckets": False,
                }
            },
            "public access block",
        ),
    ],
)
def test_bucket_control_drift_fails_closed(
    attribute: str, value: Any, message: str
) -> None:
    s3 = FakeS3()
    setattr(s3, attribute, value)
    with pytest.raises(slots.BootstrapSlotError, match=message):
        slots.verify_bucket_controls(s3, bucket_name=BUCKET)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda policy: policy["Statement"].pop(1),
        lambda policy: policy["Statement"][1].update(Resource=f"{BUCKET_ARN}/*"),
        lambda policy: policy["Statement"][1]["Condition"]["Null"].update(
            {"s3:if-none-match": "false"}
        ),
        lambda policy: policy["Statement"].append(
            {
                "Sid": "UnexpectedAllow",
                "Effect": "Allow",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": "*",
            }
        ),
    ],
)
def test_bucket_policy_missing_broad_or_extra_controls_fails_closed(
    mutate: Any,
) -> None:
    s3 = FakeS3()
    mutate(s3.policy)
    with pytest.raises(slots.BootstrapSlotError, match="bucket policy"):
        slots.verify_bucket_controls(s3, bucket_name=BUCKET)


def test_expected_bucket_arn_is_checked_before_api_calls() -> None:
    s3 = FakeS3()
    with pytest.raises(slots.BootstrapSlotError, match="ARN mismatch"):
        slots.verify_bucket_controls(
            s3, bucket_name=BUCKET, expected_bucket_arn="arn:aws:s3:::other-bucket"
        )
    assert s3.calls == []


def test_control_api_failure_is_sanitized_and_prevents_claim() -> None:
    s3 = FakeS3()
    s3.fail_method = "get_object_lock_configuration"
    with pytest.raises(slots.BootstrapSlotError) as error:
        _claim(s3)
    assert "sentinel-private-error" not in str(error.value)
    assert "object lock: read failed" in str(error.value)
    assert all(method != "put_object" for method, _ in s3.calls)


def test_conditional_claim_conflict_is_not_retried() -> None:
    s3 = FakeS3()
    s3.put_error = FakeClientError("PreconditionFailed")

    with pytest.raises(slots.BootstrapSlotError, match="ambiguous") as error:
        _claim(s3)

    assert "sentinel-private-error" not in str(error.value)
    assert sum(method == "put_object" for method, _ in s3.calls) == 1


def test_concurrent_claim_becomes_visible_and_blocks_a_later_attempt() -> None:
    first = FakeS3()
    first.put_error = FakeClientError("ConditionalRequestConflict")
    with pytest.raises(slots.BootstrapSlotError):
        _claim(first)

    second = FakeS3()
    second.version_pages = [
        {
            "IsTruncated": False,
            "Versions": [{"Key": slots.BUILD_KEY, "VersionId": "winner"}],
        }
    ]
    with pytest.raises(slots.BootstrapSlotError, match="permanently consumed"):
        _claim(second)
    assert all(method != "put_object" for method, _ in second.calls)


def test_missing_version_id_after_put_is_reported_as_maybe_consumed() -> None:
    s3 = FakeS3()
    s3.put_response = {"ETag": '"etag"'}
    with pytest.raises(slots.BootstrapSlotError, match="may be consumed"):
        _claim(s3)
    assert sum(method == "put_object" for method, _ in s3.calls) == 1
