from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "asaas_build_release_preflight.py"
)
_SPEC = importlib.util.spec_from_file_location("asaas_backend_trust_test", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
preflight = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(preflight)


def _environment() -> tuple[dict[str, str], dict[str, str]]:
    public_key = ec.generate_private_key(ec.SECP256R1()).public_key()
    public_key_pem = public_key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    fingerprint = hashlib.sha256(
        public_key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).hexdigest()
    env = {
        "PIN_BACKEND_BASE_URL": "https://staging-api.example.test",
        "PIN_REPOSITORY_ID": "1152335815",
        "PIN_REPOSITORY_NAME": "neurostackinfra/neurostack-api",
        "PIN_WORKFLOW_PATH": ".github/workflows/asaas-build-attestation.yml",
        "PIN_WORKFLOW_REF": "refs/heads/main",
        "PIN_KMS_KEY_ARN": (
            "arn:aws:kms:us-east-1:388460142431:key/"
            "01234567-89ab-cdef-0123-456789abcdef"
        ),
        "PIN_BUILD_AUDIENCE": "billing-api:producer-build-attestation",
        "PIN_ISSUER": "github-actions:asaas-build",
        "PIN_OIDC_ISSUER": "https://token.actions.githubusercontent.com",
        "PIN_OIDC_SUBJECT": (
            "repo:neurostackinfra@340698182/neurostack-api@1152335815:"
            "environment:asaas-build:ref:refs/heads/main:workflow_ref:neurostackinfra/"
            "neurostack-api/.github/workflows/asaas-build-attestation.yml@refs/heads/main"
        ),
        "BUILD_PUBLIC_KEY": public_key_pem,
    }
    readback = {
        "schema": "billing_producer_build_trust_readback.v1",
        "schema_version": "v1",
        "environment": "staging",
        "type": "billing_build_attestation.v1",
        "purpose": "billing-build-attestation.v1",
        "algorithm": "ECDSA_SHA_256",
        "issuer": env["PIN_ISSUER"],
        "kid": env["PIN_KMS_KEY_ARN"],
        "audience": env["PIN_BUILD_AUDIENCE"],
        "public_key_fingerprint_sha256": fingerprint,
        "repository_id": env["PIN_REPOSITORY_ID"],
        "repository_name": env["PIN_REPOSITORY_NAME"],
        "workflow_path": env["PIN_WORKFLOW_PATH"],
        "workflow_ref": env["PIN_WORKFLOW_REF"],
        "oidc_issuer": env["PIN_OIDC_ISSUER"],
        "oidc_subject": env["PIN_OIDC_SUBJECT"],
    }
    return env, readback


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("issuer", "other-issuer", "backend_trust_pin_mismatch"),
        ("repository_id", "2", "backend_trust_pin_mismatch"),
        ("workflow_ref", "refs/heads/other", "backend_trust_pin_mismatch"),
        ("public_key_fingerprint_sha256", "0" * 64, "backend_trust_pin_mismatch"),
        ("public_key_fingerprint_sha256", "not-a-digest", "backend_trust_pin_mismatch"),
    ],
)
def test_backend_trust_readback_rejects_pin_and_public_fingerprint_mismatch(
    field: str, value: str, reason: str
) -> None:
    env, readback = _environment()
    readback[field] = value

    with pytest.raises(preflight.GateError, match=reason):
        preflight.validate_backend_trust_readback(env, readback)


@pytest.mark.parametrize(
    "readback",
    [
        {},
        {"schema_version": "billing_producer_build_trust_readback.v1"},
    ],
)
def test_backend_trust_readback_rejects_empty_and_incomplete_documents(
    readback,
) -> None:
    env, _ = _environment()
    with pytest.raises(preflight.GateError):
        preflight.validate_backend_trust_readback(env, readback)


def test_backend_trust_readback_rejects_unknown_and_empty_fields() -> None:
    env, readback = _environment()
    readback["secret"] = "must-not-be-returned"
    with pytest.raises(preflight.GateError, match="fields_invalid"):
        preflight.validate_backend_trust_readback(env, readback)

    env, readback = _environment()
    readback["issuer"] = ""
    with pytest.raises(preflight.GateError, match="incomplete"):
        preflight.validate_backend_trust_readback(env, readback)


def test_backend_trust_readback_digest_is_canonical_and_detects_preflight_drift() -> (
    None
):
    env, readback = _environment()
    validated, digest = preflight.validate_backend_trust_readback(env, readback)
    assert validated == readback
    assert (
        digest
        == hashlib.sha256(
            json.dumps(
                readback,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    with pytest.raises(preflight.GateError, match="changed_after_preflight"):
        preflight.validate_backend_trust_readback(
            env, readback, expected_digest="0" * 64
        )


def test_backend_url_must_be_a_pinned_https_origin() -> None:
    env, _ = _environment()
    assert preflight.backend_trust_url(env) == (
        "https://staging-api.example.test/v1/admin/billing/producer-build-trust"
    )
    for invalid in (
        "",
        "http://staging-api.example.test",
        "https://user:pass@staging-api.example.test",
        "https://staging-api.example.test/other",
        "https://staging-api.example.test?redirect=elsewhere",
        "https://staging-api.example.test:8443",
    ):
        env["PIN_BACKEND_BASE_URL"] = invalid
        with pytest.raises(preflight.GateError, match="backend_base_url_pin_invalid"):
            preflight.backend_trust_url(env)


def test_backend_https_fetch_rejects_unavailable_redirect_corrupt_and_duplicate_json() -> (
    None
):
    env, readback = _environment()

    def unavailable(_request, *, timeout):
        assert timeout == 5
        raise OSError("private transport details must not escape")

    with pytest.raises(preflight.GateError, match="readback_unavailable"):
        preflight.fetch_backend_trust_readback(env, opener=unavailable)

    class FakeResponse:
        status = 200
        headers = {"Content-Type": "application/json; charset=utf-8"}

        def __init__(self, raw: bytes, *, url: str | None = None) -> None:
            self.raw = raw
            self.url = url or preflight.backend_trust_url(env)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return self.url

        def read(self, _length):
            return self.raw

    def opener_for(response):
        def open_response(request, *, timeout):
            assert request.full_url == preflight.backend_trust_url(env)
            assert timeout == 5
            return response

        return open_response

    with pytest.raises(preflight.GateError, match="redirect_rejected"):
        preflight.fetch_backend_trust_readback(
            env,
            opener=opener_for(FakeResponse(b"{}", url="https://other.example.test/")),
        )
    with pytest.raises(preflight.GateError, match="readback_invalid"):
        preflight.fetch_backend_trust_readback(
            env, opener=opener_for(FakeResponse(b"{broken"))
        )

    duplicate = json.dumps(readback).replace(
        '"issuer":', '"issuer":"first","issuer":', 1
    )
    with pytest.raises(preflight.GateError, match="readback_invalid"):
        preflight.fetch_backend_trust_readback(
            env, opener=opener_for(FakeResponse(duplicate.encode("utf-8")))
        )

    canonical = json.dumps(readback).encode("utf-8")
    response = FakeResponse(canonical)
    validated, digest = preflight.fetch_backend_trust_readback(
        env, opener=opener_for(response)
    )
    assert validated == readback
    assert digest == preflight.validate_backend_trust_readback(env, readback)[1]


def test_default_backend_fetch_installs_no_redirect_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, _readback = _environment()
    opened_urls = []

    class FakeOpener:
        @staticmethod
        def open(request, *, timeout):
            opened_urls.append(request.full_url)
            assert timeout == 5
            raise preflight.HTTPError(request.full_url, 302, "Found", {}, None)

    def build_opener(*handlers):
        assert len(handlers) == 1
        assert isinstance(handlers[0], preflight._NoBackendTrustRedirect)
        assert (
            handlers[0].redirect_request(
                None, None, 302, "Found", {}, "https://other.invalid/"
            )
            is None
        )
        return FakeOpener()

    monkeypatch.setattr(preflight, "build_opener", build_opener)
    with pytest.raises(
        preflight.GateError, match="backend_trust_readback_redirect_rejected"
    ):
        preflight.fetch_backend_trust_readback(env)
    assert opened_urls == [preflight.backend_trust_url(env)]


def test_build_time_backend_origin_change_is_rejected_before_fetch() -> None:
    env, _readback = _environment()
    env["ASAAS_PREFLIGHT_BACKEND_ORIGIN"] = "https://different.example.test"

    def unexpected_fetch(_request, *, timeout):
        pytest.fail(f"unexpected backend fetch with timeout {timeout}")

    with pytest.raises(
        preflight.GateError, match="backend_trust_origin_changed_after_preflight"
    ):
        preflight.fetch_backend_trust_readback(env, opener=unexpected_fetch)
