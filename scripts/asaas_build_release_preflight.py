#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

REPOSITORY = "neurostackinfra/neurostack-api"
REPOSITORY_ID = 1152335815
OWNER_ID = 340698182
RELEASE_REF = "refs/heads/main"
WORKFLOW_PATH = ".github/workflows/asaas-build-attestation.yml"
EXPECTED_RULESET = "asaas-producer-release-main"
ENVIRONMENTS = ("asaas-build", "asaas-production-deployment")
OPERATIONAL_AUTHORIZATION_REFERENCE = (
    "asaas-producer-image-rollout/WISH.md#"
    "2026-10-15-separate-build-and-rollout-plan-approved"
)
EXPECTED_BUILD_SUBJECT = (
    "repo:neurostackinfra@340698182/neurostack-api@1152335815:environment:"
    "asaas-build:ref:refs/heads/main:workflow_ref:"
    "neurostackinfra/neurostack-api/.github/workflows/"
    "asaas-build-attestation.yml@refs/heads/main"
)
API_ROOT = "https://api.github.com"
API_PREFIX = f"/repos/{REPOSITORY}"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
BACKEND_TRUST_PATH = "/v1/admin/billing/producer-build-trust"
BACKEND_TRUST_SCHEMA = "billing_producer_build_trust_readback.v1"
BACKEND_TRUST_PROBE_SCHEMA = "billing_producer_build_trust_bootstrap_probe.v1"
BUILD_TRUST_MODES = frozenset({"normal", "bootstrap"})
BOOTSTRAP_BUILD_ROLE_ARN = (
    "arn:aws:iam::388460142431:role/"
    "neurostack-asaas-producer-release-BuildRole-ptpxs3j9NIZM"
)
BOOTSTRAP_KMS_KEY_ARN = (
    "arn:aws:kms:us-east-1:388460142431:key/" "6a74b35d-b97f-4a0a-9269-240a762d633b"
)
BOOTSTRAP_ECR_URI = (
    "388460142431.dkr.ecr.us-east-1.amazonaws.com/"
    "neurostack-asaas-producer-release-producerimagerepository-jqtdjqst9o1j"
)
BOOTSTRAP_BUILD_AUDIENCE = "billing-api:producer-build-attestation"
BOOTSTRAP_BUILD_ISSUER = "github-actions:neurostack-build"
BOOTSTRAP_BACKEND_ORIGIN = "https://staging.api.neurostack.global"
BOOTSTRAP_PUBLIC_KEY_FINGERPRINT_SHA256 = (
    "86d440d5c7f2248499bd6fefc37064ec4b5eae2fae0b3aebb30c43819bbd384b"
)
BOOTSTRAP_OLD_AUTH_RESPONSE = {
    "error": "unauthorized",
    "documentation_url": "https://docs.neurostack.ai/auth",
}
BACKEND_TRUST_FIELDS = frozenset(
    {
        "schema",
        "schema_version",
        "environment",
        "type",
        "purpose",
        "algorithm",
        "issuer",
        "kid",
        "audience",
        "public_key_fingerprint_sha256",
        "repository_id",
        "repository_name",
        "workflow_path",
        "workflow_ref",
        "oidc_issuer",
        "oidc_subject",
    }
)


class GateError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise GateError(reason)


def backend_trust_origin(env):
    base_url = env.get("PIN_BACKEND_BASE_URL", "")
    try:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname
        labels = hostname.split(".") if hostname is not None else []
        valid_hostname = (
            len(labels) >= 2
            and all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in labels
            )
            and len(hostname) <= 253
        )
        valid = (
            parsed.scheme == "https"
            and hostname is not None
            and hostname == hostname.lower()
            and valid_hostname
            and parsed.netloc == hostname
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        valid = False
    require(bool(valid), "backend_base_url_pin_invalid")
    return f"https://{hostname}"


def backend_trust_url(env):
    return f"{backend_trust_origin(env)}{BACKEND_TRUST_PATH}"


def _public_key_fingerprint(public_key_pem):
    require(
        isinstance(public_key_pem, str)
        and public_key_pem.startswith("-----BEGIN PUBLIC KEY-----\n")
        and public_key_pem.rstrip().endswith("-----END PUBLIC KEY-----"),
        "build_public_key_pin_invalid",
    )
    lines = public_key_pem.strip().splitlines()
    require(
        len(lines) >= 3
        and lines[0] == "-----BEGIN PUBLIC KEY-----"
        and lines[-1] == "-----END PUBLIC KEY-----",
        "build_public_key_pin_invalid",
    )
    encoded = "".join(lines[1:-1])
    try:
        der = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise GateError("build_public_key_pin_invalid") from None
    require(
        bool(der) and base64.b64encode(der).decode("ascii") == encoded,
        "build_public_key_pin_invalid",
    )
    return hashlib.sha256(der).hexdigest()


def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def trust_mode(env):
    mode = env.get("INPUT_TRUST_MODE", env.get("ASAAS_BUILD_TRUST_MODE", "normal"))
    require(mode in BUILD_TRUST_MODES, "build_trust_mode_invalid")
    return mode


def _bootstrap_pins(env):
    expected = {
        "PIN_REPOSITORY_ID": str(REPOSITORY_ID),
        "PIN_REPOSITORY_NAME": REPOSITORY,
        "PIN_WORKFLOW_PATH": WORKFLOW_PATH,
        "PIN_WORKFLOW_REF": RELEASE_REF,
        "PIN_ROLE_ARN": BOOTSTRAP_BUILD_ROLE_ARN,
        "PIN_KMS_KEY_ARN": BOOTSTRAP_KMS_KEY_ARN,
        "PIN_BUILD_AUDIENCE": BOOTSTRAP_BUILD_AUDIENCE,
        "PIN_ISSUER": BOOTSTRAP_BUILD_ISSUER,
        "PIN_OIDC_ISSUER": "https://token.actions.githubusercontent.com",
        "PIN_OIDC_SUBJECT": EXPECTED_BUILD_SUBJECT,
        "PIN_ECR_URI": BOOTSTRAP_ECR_URI,
    }
    require(
        all(env.get(key) == value for key, value in expected.items()),
        "bootstrap_trust_pin_mismatch",
    )
    fingerprint = _public_key_fingerprint(env.get("BUILD_PUBLIC_KEY", ""))
    require(
        fingerprint == BOOTSTRAP_PUBLIC_KEY_FINGERPRINT_SHA256,
        "bootstrap_public_key_fingerprint_mismatch",
    )


def _expected_bootstrap_absence_response(raw):
    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        return False
    return (
        isinstance(value, dict)
        and set(value) == set(BOOTSTRAP_OLD_AUTH_RESPONSE) | {"message"}
        and value.get("error") == BOOTSTRAP_OLD_AUTH_RESPONSE["error"]
        and value.get("documentation_url")
        == BOOTSTRAP_OLD_AUTH_RESPONSE["documentation_url"]
        and isinstance(value.get("message"), str)
        and re.fullmatch(
            r"Missing Authorization or [A-Za-z0-9_-]+ header",
            value["message"],
        )
        is not None
    )


class _NoBackendTrustRedirect(HTTPRedirectHandler):
    """Do not follow redirects while fetching the pinned backend trust."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def validate_backend_trust_readback(env, readback, *, expected_digest=None):
    require(isinstance(readback, dict), "backend_trust_readback_invalid")
    require(
        set(readback) == BACKEND_TRUST_FIELDS,
        "backend_trust_readback_fields_invalid",
    )
    require(
        all(
            isinstance(value, str) and value and value.strip() == value
            for value in readback.values()
        ),
        "backend_trust_readback_incomplete",
    )
    expected = {
        "schema": BACKEND_TRUST_SCHEMA,
        "schema_version": "v1",
        "environment": "staging",
        "type": "billing_build_attestation.v1",
        "purpose": "billing-build-attestation.v1",
        "algorithm": "ECDSA_SHA_256",
        "issuer": env.get("PIN_ISSUER", ""),
        "kid": env.get("PIN_KMS_KEY_ARN", ""),
        "audience": env.get("PIN_BUILD_AUDIENCE", ""),
        "public_key_fingerprint_sha256": _public_key_fingerprint(
            env.get("BUILD_PUBLIC_KEY", "")
        ),
        "repository_id": env.get("PIN_REPOSITORY_ID", ""),
        "repository_name": env.get("PIN_REPOSITORY_NAME", ""),
        "workflow_path": env.get("PIN_WORKFLOW_PATH", ""),
        "workflow_ref": env.get("PIN_WORKFLOW_REF", ""),
        "oidc_issuer": env.get("PIN_OIDC_ISSUER", ""),
        "oidc_subject": env.get("PIN_OIDC_SUBJECT", ""),
    }
    require(
        all(
            isinstance(value, str) and value and value.strip() == value
            for value in expected.values()
        ),
        "backend_trust_pins_incomplete",
    )
    require(
        all(expected[key] and readback[key] == expected[key] for key in expected),
        "backend_trust_pin_mismatch",
    )
    require(
        re.fullmatch(r"[0-9a-f]{64}", readback["public_key_fingerprint_sha256"])
        is not None,
        "backend_trust_fingerprint_invalid",
    )
    digest = hashlib.sha256(_canonical_json(readback)).hexdigest()
    if expected_digest is not None:
        require(
            re.fullmatch(r"[0-9a-f]{64}", expected_digest) is not None,
            "backend_trust_preflight_digest_invalid",
        )
        require(digest == expected_digest, "backend_trust_changed_after_preflight")
    return readback, digest


def fetch_backend_trust_readback(env, *, opener=None, expected_digest=None):
    origin = backend_trust_origin(env)
    expected_origin = env.get("ASAAS_PREFLIGHT_BACKEND_ORIGIN")
    if expected_origin is not None:
        require(
            expected_origin == origin,
            "backend_trust_origin_changed_after_preflight",
        )
    url = backend_trust_url(env)
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    open_url = (
        build_opener(_NoBackendTrustRedirect()).open if opener is None else opener
    )
    try:
        with open_url(request, timeout=5) as response:
            require(response.status == 200, "backend_trust_readback_unavailable")
            require(
                response.geturl() == url,
                "backend_trust_readback_redirect_rejected",
            )
            content_type = response.headers.get("Content-Type", "")
            require(
                content_type.split(";", 1)[0].strip().lower() == "application/json",
                "backend_trust_readback_media_type_invalid",
            )
            raw = response.read(64 * 1024 + 1)
    except GateError:
        raise
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise GateError("backend_trust_readback_redirect_rejected") from None
        raise GateError("backend_trust_readback_unavailable") from None
    except (OSError, TimeoutError, ValueError):
        raise GateError("backend_trust_readback_unavailable") from None
    require(len(raw) <= 64 * 1024, "backend_trust_readback_too_large")

    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "backend_trust_readback_invalid")
            result[key] = value
        return result

    try:
        readback = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        raise GateError("backend_trust_readback_invalid") from None
    return validate_backend_trust_readback(
        env, readback, expected_digest=expected_digest
    )


def probe_backend_trust_absent(env, *, opener=None, expected_digest=None):
    """Accept only the known pre-route 401 response for explicit bootstrap."""
    origin = backend_trust_origin(env)
    require(
        origin == BOOTSTRAP_BACKEND_ORIGIN,
        "bootstrap_backend_origin_pin_mismatch",
    )
    expected_origin = env.get("ASAAS_PREFLIGHT_BACKEND_ORIGIN")
    if expected_origin is not None:
        require(
            expected_origin == origin,
            "backend_trust_origin_changed_after_preflight",
        )
    url = backend_trust_url(env)
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    open_url = (
        build_opener(_NoBackendTrustRedirect()).open if opener is None else opener
    )
    status = None
    final_url = url
    headers = None
    raw = b""
    try:
        with open_url(request, timeout=5) as response:
            status = response.status
            final_url = response.geturl()
            headers = response.headers
            raw = response.read(64 * 1024 + 1)
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise GateError("backend_trust_readback_redirect_rejected") from None
        if exc.code != 401:
            raise GateError("bootstrap_trust_absence_unconfirmed") from None
        status = exc.code
        final_url = exc.geturl()
        headers = exc.headers
        try:
            raw = exc.read(64 * 1024 + 1)
        except OSError:
            raise GateError("bootstrap_trust_absence_unconfirmed") from None
        finally:
            try:
                exc.close()
            except OSError:
                pass
    except (OSError, TimeoutError, ValueError):
        raise GateError("bootstrap_trust_absence_unconfirmed") from None

    require(final_url == url, "backend_trust_readback_redirect_rejected")
    require(status == 401, "bootstrap_trust_already_active")
    require(len(raw) <= 64 * 1024, "bootstrap_trust_probe_too_large")
    content_type = headers.get("Content-Type", "") if headers is not None else ""
    require(
        content_type.split(";", 1)[0].strip().lower() == "application/json",
        "bootstrap_trust_absence_unconfirmed",
    )
    require(
        _expected_bootstrap_absence_response(raw),
        "bootstrap_trust_absence_unconfirmed",
    )
    probe = {
        "schema": BACKEND_TRUST_PROBE_SCHEMA,
        "origin": origin,
        "path": BACKEND_TRUST_PATH,
        "status": status,
        "body_sha256": hashlib.sha256(raw).hexdigest(),
    }
    digest = hashlib.sha256(_canonical_json(probe)).hexdigest()
    if expected_digest is not None:
        require(
            re.fullmatch(r"[0-9a-f]{64}", expected_digest) is not None,
            "bootstrap_trust_probe_digest_invalid",
        )
        require(digest == expected_digest, "bootstrap_trust_probe_changed")
    return digest


def validate_scope(env):
    mode = trust_mode(env)
    require(
        env.get("INPUT_WISH") == "asaas-producer-image-rollout", "wish_scope_invalid"
    )
    require(env.get("INPUT_GROUP") in {"3", "4"}, "group_scope_invalid")
    reference = env.get("INPUT_AUTHORIZATION_REFERENCE", "")
    require(
        reference == OPERATIONAL_AUTHORIZATION_REFERENCE,
        "authorization_reference_unrecorded",
    )
    try:
        operation_id = UUID(env.get("INPUT_OPERATION_ID", ""))
    except (TypeError, ValueError):
        raise GateError("operation_id_invalid") from None
    require(
        str(operation_id) == env.get("INPUT_OPERATION_ID"), "operation_id_not_canonical"
    )
    require(env.get("GITHUB_REPOSITORY") == REPOSITORY, "repository_name_mismatch")
    require(
        env.get("GITHUB_REPOSITORY_ID") == str(REPOSITORY_ID), "repository_id_mismatch"
    )
    require(
        env.get("GITHUB_REPOSITORY_OWNER_ID") == str(OWNER_ID),
        "repository_owner_id_mismatch",
    )
    require(env.get("GITHUB_REF") == RELEASE_REF, "source_ref_mismatch")
    expected_workflow_ref = f"{REPOSITORY}/{WORKFLOW_PATH}@{RELEASE_REF}"
    require(
        env.get("GITHUB_WORKFLOW_REF") == expected_workflow_ref, "workflow_ref_mismatch"
    )
    require(
        env.get("PIN_REPOSITORY_ID") == str(REPOSITORY_ID), "repository_id_pin_mismatch"
    )
    require(
        env.get("PIN_REPOSITORY_NAME") == REPOSITORY, "repository_name_pin_mismatch"
    )
    require(env.get("PIN_WORKFLOW_PATH") == WORKFLOW_PATH, "workflow_path_pin_mismatch")
    require(env.get("PIN_WORKFLOW_REF") == RELEASE_REF, "workflow_ref_pin_mismatch")
    require(
        env.get("PIN_ROLE_ARN", "").startswith("arn:aws:iam::388460142431:role/"),
        "build_role_pin_missing",
    )
    require(
        bool(
            re.fullmatch(
                r"arn:aws:kms:us-east-1:388460142431:key/[A-Za-z0-9-]+",
                env.get("PIN_KMS_KEY_ARN", ""),
            )
        ),
        "build_key_pin_invalid",
    )
    build_audience = env.get("PIN_BUILD_AUDIENCE", "")
    require(
        bool(build_audience)
        and build_audience.strip() == build_audience
        and build_audience != "sts.amazonaws.com",
        "build_audience_pin_invalid",
    )
    require(
        env.get("PIN_OIDC_AUDIENCE") == "sts.amazonaws.com",
        "oidc_audience_pin_mismatch",
    )
    require(bool(env.get("PIN_ISSUER", "").strip()), "attestation_issuer_pin_missing")
    require(
        env.get("PIN_OIDC_ISSUER") == "https://token.actions.githubusercontent.com",
        "oidc_issuer_pin_mismatch",
    )
    require(
        env.get("PIN_OIDC_SUBJECT") == EXPECTED_BUILD_SUBJECT,
        "oidc_subject_pin_mismatch",
    )
    require(
        bool(
            re.fullmatch(
                r"388460142431\.dkr\.ecr\.us-east-1\.amazonaws\.com/[A-Za-z0-9._/-]+",
                env.get("PIN_ECR_URI", ""),
            )
        ),
        "ecr_repository_pin_invalid",
    )
    require(
        bool(env.get("BUILD_PUBLIC_KEY", "").strip()), "build_public_key_pin_missing"
    )
    if mode == "bootstrap":
        require(env.get("INPUT_GROUP") == "4", "bootstrap_group_scope_invalid")
        require(
            env.get("GITHUB_RUN_ATTEMPT") == "1",
            "bootstrap_run_attempt_must_be_first",
        )
        _bootstrap_pins(env)


def _next_link(link_header):
    if not link_header:
        return None
    for item in link_header.split(","):
        if 'rel="next"' in item:
            match = re.search(r"<([^>]+)>", item)
            return match.group(1) if match else "invalid"
    return None


def github_get(url, token):
    parsed = urlsplit(url)
    require(
        parsed.scheme == "https" and parsed.netloc == "api.github.com",
        "github_api_url_invalid",
    )
    require(
        parsed.path == API_PREFIX or parsed.path.startswith(API_PREFIX + "/"),
        "github_api_path_invalid",
    )
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Cache-Control": "no-cache",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=10) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            require(status == 200, "github_api_readback_failed")
            final_url = urlsplit(response.geturl())
            require(
                final_url.scheme == "https"
                and final_url.netloc == "api.github.com"
                and final_url.path == parsed.path,
                "github_api_redirect_invalid",
            )
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            require(len(raw) <= MAX_RESPONSE_BYTES, "github_api_response_too_large")
            value = json.loads(raw)
            return value, response.headers.get("Link")
    except GateError:
        raise
    except (HTTPError, URLError, OSError, json.JSONDecodeError):
        raise GateError("github_api_readback_failed") from None


def github_collection(url, token, *, list_field=None):
    collected = []
    expected_total = None
    page = 0
    while url:
        page += 1
        require(page <= 100, "github_api_pagination_limit")
        value, link = github_get(url, token)
        if list_field is None:
            require(isinstance(value, list), "github_api_collection_invalid")
            collected.extend(value)
        else:
            require(isinstance(value, dict), "github_api_collection_invalid")
            total = value.get("total_count")
            rows = value.get(list_field)
            require(type(total) is int and total >= 0, "github_api_total_count_missing")
            require(isinstance(rows, list), "github_api_collection_invalid")
            if expected_total is None:
                expected_total = total
            require(total == expected_total, "github_api_pagination_changed")
            collected.extend(rows)
        next_url = _next_link(link)
        if next_url is not None:
            url = urljoin(url, next_url)
        else:
            url = None
    if expected_total is not None:
        require(len(collected) == expected_total, "github_api_pagination_incomplete")
    return collected


def validate_ruleset(rulesets, token, *, github_get_fn=None):
    get = github_get if github_get_fn is None else github_get_fn
    require(len(rulesets) == 1, "ruleset_count_mismatch")
    summary = rulesets[0]
    require(
        isinstance(summary, dict) and summary.get("name") == EXPECTED_RULESET,
        "ruleset_identity_mismatch",
    )
    ruleset_id = summary.get("id")
    require(type(ruleset_id) is int and ruleset_id > 0, "ruleset_id_invalid")
    url = f"{API_ROOT}{API_PREFIX}/rulesets/{ruleset_id}"
    ruleset, _ = get(url, token)
    require(isinstance(ruleset, dict), "ruleset_readback_invalid")
    require(
        ruleset.get("name") == EXPECTED_RULESET
        and ruleset.get("target") == "branch"
        and ruleset.get("enforcement") == "active",
        "ruleset_inactive_or_mismatched",
    )
    conditions = ruleset.get("conditions")
    ref_name = conditions.get("ref_name") if isinstance(conditions, dict) else None
    require(
        isinstance(ref_name, dict)
        and ref_name.get("include") == [RELEASE_REF]
        and ref_name.get("exclude") == [],
        "ruleset_ref_scope_mismatch",
    )
    require(
        ruleset.get("bypass_actors") == [], "ruleset_bypass_not_empty_or_unreadable"
    )
    rows = ruleset.get("rules")
    require(isinstance(rows, list), "ruleset_rules_missing")
    by_type = {}
    for row in rows:
        require(
            isinstance(row, dict) and isinstance(row.get("type"), str),
            "ruleset_rule_invalid",
        )
        rule_type = row["type"]
        require(rule_type not in by_type, "ruleset_rule_duplicate")
        by_type[rule_type] = row
    require(
        set(by_type)
        == {"pull_request", "required_status_checks", "deletion", "non_fast_forward"},
        "ruleset_required_controls_missing",
    )
    pr = by_type["pull_request"].get("parameters")
    require(isinstance(pr, dict), "ruleset_pull_request_controls_missing")
    require(
        type(pr.get("required_approving_review_count")) is int
        and pr["required_approving_review_count"] >= 1,
        "ruleset_review_required",
    )
    require(
        pr.get("dismiss_stale_reviews_on_push") is True
        and pr.get("require_last_push_approval") is True,
        "ruleset_review_controls_weak",
    )
    checks = by_type["required_status_checks"].get("parameters")
    require(
        isinstance(checks, dict)
        and type(checks.get("strict_required_status_checks_policy")) is bool,
        "ruleset_named_checks_missing",
    )
    required_checks = (
        checks.get("required_status_checks") if isinstance(checks, dict) else None
    )
    require(
        isinstance(required_checks, list) and required_checks,
        "ruleset_named_checks_missing",
    )
    check_names = []
    for check in required_checks:
        require(
            isinstance(check, dict)
            and isinstance(check.get("context"), str)
            and bool(check["context"].strip()),
            "ruleset_named_check_invalid",
        )
        check_names.append(check["context"])
    require(len(check_names) == len(set(check_names)), "ruleset_named_check_duplicate")
    return ruleset


def validate_environment(name, token, *, github_get_fn=None, github_collection_fn=None):
    get = github_get if github_get_fn is None else github_get_fn
    collect = (
        github_collection if github_collection_fn is None else github_collection_fn
    )
    escaped = quote(name, safe="")
    root = f"{API_ROOT}{API_PREFIX}/environments/{escaped}"
    environment, _ = get(root, token)
    require(
        isinstance(environment, dict) and environment.get("name") == name,
        "environment_identity_missing",
    )
    require(
        environment.get("can_admins_bypass") is False,
        "environment_admin_bypass_enabled_or_unreadable",
    )
    protection_rules = environment.get("protection_rules")
    require(
        isinstance(protection_rules, list) and len(protection_rules) == 1,
        "environment_reviewer_rule_missing",
    )
    rule = protection_rules[0]
    require(
        isinstance(rule, dict) and rule.get("type") == "required_reviewers",
        "environment_required_reviewer_missing",
    )
    reviewers = rule.get("reviewers")
    require(isinstance(reviewers, list) and reviewers, "environment_reviewer_missing")
    require(
        rule.get("prevent_self_review") is True,
        "environment_self_review_allowed_or_unreadable",
    )
    normalized_reviewers = []
    for reviewer in reviewers:
        require(
            isinstance(reviewer, dict) and reviewer.get("type") == "User",
            "environment_reviewer_identity_invalid",
        )
        reviewer_identity = reviewer.get("reviewer")
        require(
            isinstance(reviewer_identity, dict)
            and type(reviewer_identity.get("id")) is int
            and reviewer_identity["id"] > 0
            and isinstance(reviewer_identity.get("login"), str)
            and reviewer_identity["login"].strip(),
            "environment_reviewer_identity_invalid",
        )
        normalized_reviewers.append(
            {
                "type": "User",
                "id": reviewer_identity["id"],
                "login": reviewer_identity["login"],
            }
        )
    policy = environment.get("deployment_branch_policy")
    require(
        policy == {"protected_branches": False, "custom_branch_policies": True},
        "environment_branch_policy_unrestricted_or_mismatched",
    )
    rows = collect(
        root + "/deployment-branch-policies?per_page=100&page=1",
        token,
        list_field="branch_policies",
    )
    require(len(rows) == 1, "environment_branch_policy_count_mismatch")
    row = rows[0]
    require(isinstance(row, dict), "environment_branch_policy_invalid")
    # GitHub may include the branch/tag type. If it omits the type, the
    # independent environment reviewer must confirm the exact UI policy.
    require(
        set(row)
        in (
            {"id", "node_id", "name"},
            {"name", "type"},
            {"id", "node_id", "name", "type"},
        ),
        "environment_branch_policy_shape_invalid",
    )
    if "id" in row:
        require(
            type(row.get("id")) is int and row["id"] > 0,
            "environment_branch_policy_id_invalid",
        )
        require(
            isinstance(row.get("node_id"), str) and bool(row["node_id"].strip()),
            "environment_branch_policy_node_id_invalid",
        )
    if "type" in row:
        require(
            row.get("type") in {"branch", "branch_name"},
            "environment_policy_is_not_exact_branch_only",
        )
    require(row.get("name") == "main", "environment_policy_is_not_exact_branch_only")
    return {
        "name": name,
        "can_admins_bypass": environment["can_admins_bypass"],
        "reviewers": normalized_reviewers,
        "prevent_self_review": rule["prevent_self_review"],
        "deployment_branch_policy": policy,
        "branch_policies": [
            {key: row[key] for key in ("id", "node_id", "name", "type") if key in row}
        ],
    }


def validate_current_run(token, env, *, github_get_fn=None, github_collection_fn=None):
    get = github_get if github_get_fn is None else github_get_fn
    collect = (
        github_collection if github_collection_fn is None else github_collection_fn
    )
    run_id = env.get("GITHUB_RUN_ID", "")
    require(bool(re.fullmatch(r"[1-9][0-9]*", run_id)), "actions_run_id_invalid")
    run, _ = get(f"{API_ROOT}{API_PREFIX}/actions/runs/{run_id}", token)
    require(isinstance(run, dict), "actions_run_readback_invalid")
    require(
        type(run.get("id")) is int and run["id"] == int(run_id),
        "actions_run_id_mismatch",
    )
    require(
        run.get("event") == "workflow_dispatch" and run.get("status") == "in_progress",
        "actions_run_not_current_dispatch",
    )
    require(
        run.get("head_sha") == env.get("GITHUB_SHA")
        and run.get("head_branch") == "main",
        "actions_run_commit_or_ref_mismatch",
    )
    require(
        bool(re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", ""))),
        "actions_run_commit_or_ref_mismatch",
    )
    expected_path = f"{WORKFLOW_PATH}@{RELEASE_REF}"
    require(run.get("path") == expected_path, "actions_run_workflow_path_mismatch")
    if trust_mode(env) == "bootstrap":
        expected_title = (
            f"ASAAS producer build bootstrap {env.get('INPUT_OPERATION_ID', '')}"
        )
        require(
            run.get("display_title") == expected_title,
            "bootstrap_run_title_mismatch",
        )
    run_repository = run.get("repository")
    require(
        isinstance(run_repository, dict)
        and run_repository.get("full_name") == REPOSITORY
        and run_repository.get("id") == REPOSITORY_ID,
        "actions_run_repository_mismatch",
    )
    repo = run.get("head_repository")
    require(
        isinstance(repo, dict) and repo.get("id") == REPOSITORY_ID,
        "actions_run_head_repository_mismatch",
    )
    created_at = run.get("created_at")
    require(isinstance(created_at, str), "actions_run_timestamp_missing")
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        raise GateError("actions_run_timestamp_invalid") from None
    now = datetime.now(timezone.utc)
    require(
        created.tzinfo is not None
        and created <= now + timedelta(minutes=5)
        and now - created <= timedelta(hours=1),
        "actions_run_is_stale",
    )
    jobs = collect(
        f"{API_ROOT}{API_PREFIX}/actions/runs/{run_id}/jobs?per_page=100&page=1",
        token,
        list_field="jobs",
    )
    preflight_jobs = [
        job
        for job in jobs
        if isinstance(job, dict)
        and job.get("name") == "preflight"
        and job.get("head_sha") == env.get("GITHUB_SHA")
    ]
    require(
        len(preflight_jobs) == 1 and preflight_jobs[0].get("status") == "in_progress",
        "actions_preflight_job_readback_mismatch",
    )
    return {
        "run_id": run_id,
        "workflow_path": WORKFLOW_PATH,
        "sha": run["head_sha"],
        "ref": RELEASE_REF,
    }


def validate_bootstrap_operation_unused(
    token, operation_id, current_run_id, *, github_collection_fn=None
):
    collect = (
        github_collection if github_collection_fn is None else github_collection_fn
    )
    url = (
        f"{API_ROOT}{API_PREFIX}/actions/workflows/asaas-build-attestation.yml/runs"
        "?event=workflow_dispatch&per_page=100&page=1"
    )
    runs = collect(url, token, list_field="workflow_runs")
    for run in runs:
        require(isinstance(run, dict), "bootstrap_run_history_invalid")
        run_id = run.get("id")
        title = run.get("display_title")
        require(
            type(run_id) is int
            and run_id > 0
            and run.get("event") == "workflow_dispatch"
            and isinstance(title, str),
            "bootstrap_run_history_invalid",
        )
        match = re.fullmatch(
            r"ASAAS producer build (?:normal|bootstrap) "
            r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            title,
        )
        require(match is not None, "bootstrap_run_history_unattributable")
        historical_operation_id = match.group(1)
        if str(run_id) == current_run_id:
            require(
                historical_operation_id == operation_id,
                "bootstrap_current_run_operation_mismatch",
            )
            continue
        require(
            historical_operation_id != operation_id,
            "bootstrap_operation_already_used",
        )


def write_admin_ui_approval_instructions(
    run_context,
    environment_name,
    readback_digest,
    observed_at,
    backend_trust_digest,
    backend_trust_origin,
    *,
    trust_mode_value="normal",
    backend_probe_digest="",
):
    require(isinstance(run_context, dict), "environment_approval_context_missing")
    require(
        bool(re.fullmatch(r"[1-9][0-9]*", run_context.get("run_id", ""))),
        "environment_approval_context_missing",
    )
    require(
        run_context.get("workflow_path") == WORKFLOW_PATH,
        "environment_approval_context_missing",
    )
    require(
        bool(re.fullmatch(r"[0-9a-f]{40}", run_context.get("sha", ""))),
        "environment_approval_context_missing",
    )
    require(
        run_context.get("ref") == RELEASE_REF, "environment_approval_context_missing"
    )
    require(environment_name == "asaas-build", "environment_approval_context_missing")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    require(bool(summary_path), "environment_approval_context_missing")
    require(
        bool(re.fullmatch(r"[0-9a-f]{64}", readback_digest)),
        "environment_approval_context_missing",
    )
    require(
        trust_mode_value in BUILD_TRUST_MODES,
        "environment_approval_context_missing",
    )
    if trust_mode_value == "normal":
        require(
            bool(re.fullmatch(r"[0-9a-f]{64}", backend_trust_digest)),
            "environment_approval_context_missing",
        )
        require(not backend_probe_digest, "environment_approval_context_missing")
    else:
        require(not backend_trust_digest, "environment_approval_context_missing")
        require(
            bool(re.fullmatch(r"[0-9a-f]{64}", backend_probe_digest)),
            "environment_approval_context_missing",
        )
    require(
        isinstance(backend_trust_origin, str)
        and backend_trust_origin.startswith("https://"),
        "environment_approval_context_missing",
    )
    require(
        isinstance(observed_at, str) and observed_at.endswith("Z"),
        "environment_approval_context_missing",
    )
    with open(summary_path, "a", encoding="utf-8") as summary:
        summary.write(
            "## Independent administrator UI review required before approval\n\n"
            "Inspect the exact source-ref ruleset and `asaas-build` environment "
            "in GitHub settings immediately before approving the protected job. "
            "Confirm the ruleset bypass list is empty, administrator bypass is "
            "disabled, and the environment allows only the `main` branch with "
            "no tag policy. Capture a sanitized PNG, then attach it to a PR or "
            "issue comment while this run waits for approval; this stores the "
            "image outside repository history. Include this run ID, SHA, ref, "
            "and environment in that comment. The protected job downloads the "
            "PNG and verifies its SHA-256 before requesting OIDC.\n\n"
            f"- Run ID: `{run_context['run_id']}`\n"
            f"- Workflow: `{run_context['workflow_path']}`\n"
            f"- SHA: `{run_context['sha']}`\n"
            f"- Ref: `{run_context['ref']}`\n"
            f"- Environment: `{environment_name}`\n"
            f"- API readback SHA-256: `{readback_digest}`\n"
            f"- Build-trust mode: `{trust_mode_value}`\n"
            + (
                f"- Backend build-trust readback SHA-256: `{backend_trust_digest}`\n"
                if trust_mode_value == "normal"
                else f"- Bootstrap absence probe SHA-256: `{backend_probe_digest}`\n"
            )
            + f"- Backend origin: `{backend_trust_origin}`\n"
            f"- API readback time: `{observed_at}`\n\n"
            "Use this exact shape as the environment approval comment, replacing "
            "the reviewer, attachment URL, and digest with the actual values. The "
            "protected environment approval is the authoritative gate; GitHub's "
            "approval-history API does not expose the human click timestamp:\n\n"
            "```text\n"
            "ASAAS-ADMIN-UI-ATTESTATION/2 "
            f"run_id={run_context['run_id']} "
            f"sha={run_context['sha']} "
            f"ref={run_context['ref']} "
            f"workflow_path={run_context['workflow_path']} "
            f"environment={environment_name} reviewer_id=<github-user-id> "
            "reviewer_login=<github-login> "
            f"preflight_sha256={readback_digest} "
            "ruleset_bypass=empty environment_admin_bypass=disabled "
            "branch_policy=main-only "
            "evidence_ref=https://github.com/user-attachments/assets/<uuid> "
            "evidence_sha256=<64-lowercase-hex>\n"
            "```\n"
        )


def controls_digest(
    repository,
    ruleset,
    environments,
    backend_trust_digest,
    backend_trust_origin,
    *,
    trust_mode_value="normal",
    backend_probe_digest="",
):
    owner = repository.get("owner")
    snapshot = {
        "repository": {
            "full_name": repository.get("full_name"),
            "id": repository.get("id"),
            "owner_id": owner.get("id") if isinstance(owner, dict) else None,
            "default_branch": repository.get("default_branch"),
        },
        "ruleset": ruleset,
        "environments": environments,
        "backend_build_trust_readback_sha256": backend_trust_digest,
        "backend_build_trust_mode": trust_mode_value,
        "backend_build_trust_probe_sha256": backend_probe_digest,
        "backend_build_trust_origin": backend_trust_origin,
    }
    encoded = _canonical_json(snapshot)
    return hashlib.sha256(encoded).hexdigest()


def main():
    env = os.environ
    validate_scope(env)
    mode = trust_mode(env)
    backend_trust_digest = ""
    backend_probe_digest = ""
    if mode == "normal":
        _backend_trust, backend_trust_digest = fetch_backend_trust_readback(env)
    else:
        backend_probe_digest = probe_backend_trust_absent(env)
    backend_origin = backend_trust_origin(env)
    token = env.get("GH_TOKEN", "")
    require(bool(token), "github_read_token_unavailable")
    repository, _ = github_get(f"{API_ROOT}{API_PREFIX}", token)
    require(
        isinstance(repository, dict)
        and repository.get("full_name") == REPOSITORY
        and repository.get("id") == REPOSITORY_ID,
        "repository_readback_mismatch",
    )
    owner = repository.get("owner")
    require(
        isinstance(owner, dict) and owner.get("id") == OWNER_ID,
        "repository_owner_readback_mismatch",
    )
    require(
        repository.get("default_branch") == "main", "repository_default_branch_mismatch"
    )
    rulesets = github_collection(
        f"{API_ROOT}{API_PREFIX}/rulesets?includes_parents=true&per_page=100&page=1",
        token,
    )
    ruleset = validate_ruleset(rulesets, token)
    environment_readbacks = {}
    for environment_name in ENVIRONMENTS:
        environment_readbacks[environment_name] = validate_environment(
            environment_name, token
        )
    run_context = validate_current_run(token, env)
    if mode == "bootstrap":
        validate_bootstrap_operation_unused(
            token,
            env["INPUT_OPERATION_ID"],
            run_context["run_id"],
        )
    observed_at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    readback_digest = controls_digest(
        repository,
        ruleset,
        environment_readbacks,
        backend_trust_digest,
        backend_origin,
        trust_mode_value=mode,
        backend_probe_digest=backend_probe_digest,
    )
    write_admin_ui_approval_instructions(
        run_context,
        "asaas-build",
        readback_digest,
        observed_at,
        backend_trust_digest,
        backend_origin,
        trust_mode_value=mode,
        backend_probe_digest=backend_probe_digest,
    )
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write("rollout_scope=validated\n")
        output.write(f"preflight_readback_sha256={readback_digest}\n")
        output.write(f"build_trust_mode={mode}\n")
        output.write(f"backend_trust_readback_sha256={backend_trust_digest}\n")
        output.write(f"backend_trust_probe_sha256={backend_probe_digest}\n")
        output.write(f"backend_trust_origin={backend_origin}\n")
        output.write(f"preflight_observed_at={observed_at}\n")
    print("GitHub protection and rollout preflight passed")


if __name__ == "__main__":
    try:
        main()
    except GateError as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValueError, TypeError):
        print("blocked: GitHub protection preflight failed", file=sys.stderr)
        raise SystemExit(1) from None
