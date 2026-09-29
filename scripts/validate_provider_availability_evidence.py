#!/usr/bin/env python3
"""Stdlib-only validator for provider inventory and signed Gemini releases."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = ROOT / ".genie/wishes/api-provider-availability/evidence"
RECEIPTS_PATH = EVIDENCE_DIR / "provider-availability-receipts.md"
GEMINI_RECEIPT_RELATIVE = Path(
    ".genie/wishes/gemini-credentials/evidence/gemini-llm-receipt.md"
)
GEMINI_RECEIPT_PATH = ROOT / GEMINI_RECEIPT_RELATIVE
BASE_RECEIPTS_SHA256 = (
    "02422ad8c8ac3cb101b8dfee56a2f212a2884b25eb9b6ff221db29c385b21d4a"
)
DESIGN_SHA = "527a35a82cdf4776004942bf1db99fe9e5e246214e60f1e3b86ff892a0d8ca0a"
EXPECTED_DEPENDS_ON = "api-provider-settings, api-config-clarity"
SMOKE_SCHEMA = "gemini-llm-smoke/v1"
SMOKE_VALIDATION_KIND = "direct_live_smoke"
SMOKE_VALIDATION_AUTHORITY = "Production Operations Owner"
SMOKE_KEYS = frozenset(
    {
        "schema",
        "result",
        "result_code",
        "validation_kind",
        "validation_authority",
        "timestamp",
        "run_id",
        "tested_commit",
        "image_digest",
        "smoke_module_sha256",
        "exit_code",
    }
)
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
IMAGE_DIGEST = re.compile(
    r"[a-z0-9][a-z0-9._-]*(?::[0-9]+)?(?:/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}"
)
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")

EXPECTED_DECISIONS = {
    ("whatsapp", "meta_cloud"): "coming_soon",
    ("whatsapp", "zapi"): "coming_soon",
    ("whatsapp", "uazapi"): "coming_soon",
    ("whatsapp", "evolution"): "coming_soon",
    ("whatsapp", "dialog360"): "coming_soon",
    ("whatsapp", "twilio"): "coming_soon",
    ("llm", "dropout"): "coming_soon",
    ("llm", "openai"): "coming_soon",
    ("llm", "outros"): "coming_soon",
    ("llm", "slm_cortex"): "coming_soon",
    ("multimodal", "openrouter"): "coming_soon",
    ("multimodal", "groq"): "removed",
    ("multimodal", "openai"): "removed",
    ("stt", "outros"): "coming_soon",
    ("stt", "openai_whisper"): "coming_soon",
    ("stt", "elevenlabs_scribe"): "coming_soon",
    ("stt", "google_stt"): "coming_soon",
    ("stt", "deepgram_stt"): "coming_soon",
    ("stt", "microsoft_stt"): "coming_soon",
    ("tts", "elevenlabs"): "coming_soon",
    ("tts", "google"): "coming_soon",
    ("tts", "openai"): "coming_soon",
    ("tts", "microsoft"): "removed",
}
EXPECTED_PROMOTED_MATRIX = {
    capability: {
        provider: decision
        for (row_capability, provider), decision in EXPECTED_DECISIONS.items()
        if row_capability == capability and decision != "removed"
    }
    for capability in ("whatsapp", "llm", "multimodal", "stt", "tts")
}
EXPECTED_PROMOTED_MATRIX["llm"]["gemini"] = "active"
EXPECTED_PRODUCTION_IMAGES = {
    "postgres": "postgres@sha256:cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685",
    "redis": "redis@sha256:ff02b58f971e7d7d156a1267e283fcbbeee91773b6aa36c49dac28ecfe28eadf",
    "qdrant": "qdrant/qdrant@sha256:6ac4807063bbecddca0250bfbcff52acf18c22263b904d12919349e6d0a408f1",
}
RECEIPT_COLUMNS = (
    "capability",
    "provider",
    "test_command",
    "test_exit_code",
    "test_result",
    "validation_kind",
    "validation_authority",
    "validated_at",
    "decision",
    "displayable",
)
HEADER_VALUES = {
    "wish_slug": "api-provider-availability",
    "governing_design_sha": DESIGN_SHA,
    "status": "IN_PROGRESS",
    "depends_on": EXPECTED_DEPENDS_ON,
    "blocks": "api-provider-runtime-shutdown",
}
SENSITIVE_PATTERNS = {
    "secret material": r"(?i)(api[_ -]?key|secret|token|password|bearer\s+[a-z0-9])",
    "tenant URL": r"(?i)(https?://|postgres(?:ql)?://)",
    "tenant data": r"(?i)(tenant[_ -]?id|tenant\s*=|tenant-[a-z0-9]{4,}|\bclinic\b|\bpatient\b)",
    "personal data": r"(?i)([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}|\b(personal|person)\s+(name|email|data)\b)",
}


def _parse_metadata(text: str) -> dict[str, str]:
    return {
        match.group(1): match.group(2).strip()
        for line in text.splitlines()
        if (match := re.fullmatch(r"- ([a-z_]+): (.+)", line.strip()))
    }


def _parse_table(text: str) -> list[dict[str, str]]:
    lines = [line.strip() for line in text.splitlines()]
    header = "| " + " | ".join(RECEIPT_COLUMNS) + " |"
    try:
        start = lines.index(header)
    except ValueError as exc:
        raise ValueError("required table header is missing") from exc
    if start + 1 >= len(lines) or not re.fullmatch(
        r"\|(?:\s*:?-{3,}:?\s*\|)+", lines[start + 1]
    ):
        raise ValueError("required table separator is missing")
    rows: list[dict[str, str]] = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        values = [value.strip() for value in line.split("|")[1:-1]]
        if len(values) != len(RECEIPT_COLUMNS):
            raise ValueError("table row has the wrong number of fields")
        rows.append(dict(zip(RECEIPT_COLUMNS, values, strict=True)))
    return rows


def _valid_timestamp(value: str) -> bool:
    if not (value.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", value)):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _validate_receipts(path: Path, *, require_bound_bytes: bool = False) -> list[str]:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeError):
        return ["base receipt is unavailable or not UTF-8"]
    errors: list[str] = []
    if require_bound_bytes and hashlib.sha256(raw).hexdigest() != BASE_RECEIPTS_SHA256:
        errors.append("base receipt bytes do not match the protected SHA-256")
    metadata = _parse_metadata(text)
    errors.extend(
        f"metadata {key} must be {expected!r}"
        for key, expected in HEADER_VALUES.items()
        if metadata.get(key) != expected
    )
    errors.extend(
        f"evidence contains {label}"
        for label, pattern in SENSITIVE_PATTERNS.items()
        if re.search(pattern, text)
    )
    try:
        rows = _parse_table(text)
    except ValueError as exc:
        return errors + [f"receipts {exc}"]
    seen: set[tuple[str, str]] = set()
    for row in rows:
        pair = (row["capability"], row["provider"])
        if pair in seen:
            errors.append(f"duplicate receipt row: {pair}")
        seen.add(pair)
        if any(not value for value in row.values()):
            errors.append(f"receipt has an empty field: {pair}")
        if not _valid_timestamp(row["validated_at"]):
            errors.append(f"invalid timestamp: {pair}")
        expected = EXPECTED_DECISIONS.get(pair)
        if row["decision"] != expected:
            errors.append(f"wrong decision for {pair}")
        if row["displayable"] != ("false" if expected == "removed" else "true"):
            errors.append(f"displayability disagrees with decision: {pair}")
        unavailable = (
            row["test_command"],
            row["test_exit_code"],
            row["test_result"],
            row["validation_kind"],
            row["validation_authority"],
        )
        if unavailable != (
            "not_run",
            "not_run",
            "not_run",
            "not_available",
            "not_available",
        ):
            errors.append(f"receipt must record unavailable validation only: {pair}")
    if seen != set(EXPECTED_DECISIONS):
        errors.append("receipt pairs do not exactly match the 23-pair inventory")
    counts = {
        state: sum(row["decision"] == state for row in rows)
        for state in ("active", "coming_soon", "removed")
    }
    if counts != {"active": 0, "coming_soon": 20, "removed": 3}:
        errors.append(
            "base receipt decisions must remain 0 active, 20 coming_soon, 3 removed"
        )
    return errors


def _extract_gemini_receipt(
    path: Path,
) -> tuple[dict[str, Any] | None, bytes | None, list[str]]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return (
            None,
            None,
            [
                "Gemini receipt is absent; Production Operations Owner must add the "
                "canonical receipt in a signed receipt-only successor commit"
            ],
        )
    except OSError:
        return (
            None,
            None,
            [
                "Gemini receipt cannot be read; restore the canonical receipt path "
                "and retry the release checker"
            ],
        )
    pattern = re.compile(
        rb"\A# Gemini LLM release receipt\n\n"
        rb"- smoke_artifact_sha256: ([0-9a-f]{64})\n\n"
        rb"```json\n([^`]+)```\n\Z"
    )
    match = pattern.fullmatch(raw)
    if not match:
        return (
            None,
            None,
            [
                "Gemini receipt bytes are non-canonical; regenerate exact LF-only "
                "receipt bytes with one final newline"
            ],
        )
    embedded = match.group(2)
    errors: list[str] = []
    if hashlib.sha256(embedded).hexdigest().encode("ascii") != match.group(1):
        errors.append("smoke artifact SHA-256 does not match embedded bytes")
    try:
        artifact = json.loads(embedded)
    except (json.JSONDecodeError, UnicodeError):
        return None, embedded, errors + ["embedded smoke artifact is invalid JSON"]
    if not isinstance(artifact, dict) or set(artifact) != SMOKE_KEYS:
        errors.append("embedded smoke artifact does not have the exact 11-key schema")
        return None, embedded, errors
    for key in SMOKE_KEYS - {"exit_code"}:
        if not isinstance(artifact[key], str):
            errors.append(f"embedded smoke artifact {key} has the wrong type")
    if type(artifact["exit_code"]) is not int:
        errors.append("embedded smoke artifact exit_code has the wrong type")
    canonical = (
        json.dumps(
            artifact, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        + b"\n"
    )
    if embedded != canonical:
        errors.append("embedded smoke artifact bytes are not canonical JSON")
    return artifact, embedded, errors


def _git(root: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=text,
        check=False,
        env={**os.environ, "LC_ALL": "C"},
    )


def _validate_commit_signature(root: Path, head: str) -> list[str]:
    fingerprint = os.environ.get("GEMINI_RECEIPT_SIGNER_FINGERPRINT", "")
    fingerprint_format = re.fullmatch(
        r"(?:[0-9A-F]{40}|[0-9A-F]{64}|SHA256:[A-Za-z0-9+/]{43})", fingerprint
    )
    errors: list[str] = []
    if not fingerprint_format:
        errors.append(
            "protected signer identity is missing or invalid; configure the exact "
            "GPG fingerprint or SSH SHA256 fingerprint and retry"
        )
    verify = _git(root, "verify-commit", "--raw", head)
    identity = _git(root, "show", "-s", "--format=%G?%n%GF", head)
    identity_lines = identity.stdout.splitlines() if not identity.returncode else []
    if (
        verify.returncode
        or len(identity_lines) != 2
        or identity_lines[0] not in {"G", "U"}
    ):
        errors.append(
            "receipt HEAD signature verification failed; configure the approved "
            "GPG trust or SSH allowedSignersFile, then retry git verify-commit"
        )
    elif fingerprint_format and identity_lines[1] != fingerprint:
        errors.append(
            "receipt signer identity does not exactly match protected authority"
        )
    return errors


def _validate_git_release(root: Path, artifact: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    head_result = _git(root, "rev-parse", "HEAD")
    if head_result.returncode:
        return ["HEAD cannot be resolved"]
    head = head_result.stdout.strip()
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if status.returncode or status.stdout:
        errors.append("checkout and index must be exactly clean")
    entries = _git(root, "ls-files", "-s")
    if entries.returncode or any(
        line.startswith(("120000 ", "160000 ")) for line in entries.stdout.splitlines()
    ):
        errors.append("tracked symlink or gitlink entries are forbidden")
    parents = _git(root, "rev-list", "--parents", "-n", "1", head)
    tokens = parents.stdout.split() if not parents.returncode else []
    if len(tokens) != 2:
        errors.append("receipt HEAD must have exactly one parent")
        parent = ""
    else:
        parent = tokens[1]
        if parent != artifact["tested_commit"]:
            errors.append("receipt parent does not equal tested candidate")
    if parent:
        diff = _git(
            root,
            "diff-tree",
            "--no-commit-id",
            "--name-status",
            "-r",
            parent,
            head,
        )
        names = diff.stdout.splitlines() if not diff.returncode else []
        expected_add = f"A\t{GEMINI_RECEIPT_RELATIVE.as_posix()}"
        if names != [expected_add]:
            errors.append(
                "receipt commit must add only the new receipt (name-status A)"
            )
        parent_receipt = _git(
            root, "cat-file", "-e", f"{parent}:{GEMINI_RECEIPT_RELATIVE.as_posix()}"
        )
        if parent_receipt.returncode == 0:
            errors.append("receipt already exists in the candidate parent")
        head_receipt = _git(
            root, "show", f"{head}:{GEMINI_RECEIPT_RELATIVE.as_posix()}", text=False
        )
        try:
            worktree_receipt = (root / GEMINI_RECEIPT_RELATIVE).read_bytes()
        except OSError:
            worktree_receipt = None
        if head_receipt.returncode or head_receipt.stdout != worktree_receipt:
            errors.append("receipt bytes do not exactly match the signed HEAD blob")
        blob = _git(root, "show", f"{parent}:src/llm/gemini_smoke.py", text=False)
        if (
            blob.returncode
            or hashlib.sha256(blob.stdout).hexdigest()
            != artifact["smoke_module_sha256"]
        ):
            errors.append("smoke module SHA-256 does not match the candidate blob")
    errors.extend(_validate_commit_signature(root, head))
    return errors


def _parse_frontend_matrix(text: str) -> dict[str, dict[str, str]] | None:
    match = re.search(
        r"export const API_PROVIDER_AVAILABILITY\s*=\s*\{(.*?)\}\s*as const satisfies ProviderAvailabilityMatrix;",
        text,
        re.DOTALL,
    )
    if not match:
        return None
    matrix: dict[str, dict[str, str]] = {}
    for capability, body in re.findall(r"(\w+):\s*\{([^{}]*)\}", match.group(1)):
        pairs = re.findall(r'(\w+):\s*"(active|coming_soon)"\s*,?', body)
        if not pairs or len(pairs) != len(re.findall(r"\w+\s*:", body)):
            return None
        matrix[capability] = dict(pairs)
    return matrix


def _validate_product_promotion(root: Path) -> list[str]:
    """Bind the release gate to the sole approved product-matrix overlay."""

    policy_path = root / "src/services/api_credentials.py"
    try:
        tree = ast.parse(policy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, SyntaxError):
        return ["provider policy source is unavailable or invalid"]
    matrix: Any = None
    operational: Any = None
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "PROVIDER_AVAILABILITY" and node.value is not None:
                matrix = ast.literal_eval(node.value)
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "PUBLICLY_ACTIVE_LLM_PROVIDERS"
                for target in node.targets
            )
            and isinstance(node.value, ast.Call)
            and len(node.value.args) == 1
        ):
            operational = ast.literal_eval(node.value.args[0])
    if not isinstance(matrix, dict):
        return ["provider availability matrix is not a static closed mapping"]
    errors: list[str] = []
    if matrix != EXPECTED_PROMOTED_MATRIX:
        errors.append(
            "backend provider matrix does not exactly match the approved overlay"
        )
    if operational != {"gemini"}:
        errors.append("publicly active LLM membership must contain only Gemini")
    frontend_path = root / "frontend/artifacts/neurostack/src/pages/clinic-config.tsx"
    try:
        frontend = frontend_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        errors.append("frontend provider matrix is unavailable")
    else:
        if _parse_frontend_matrix(frontend) != EXPECTED_PROMOTED_MATRIX:
            errors.append(
                "frontend provider matrix does not exactly match the approved overlay"
            )
    return errors


def _validate_release(
    root: Path, receipts_path: Path, receipt_path: Path
) -> tuple[list[str], str | None]:
    errors = _validate_receipts(receipts_path, require_bound_bytes=True)
    errors.extend(_validate_product_promotion(root))
    artifact, _embedded, receipt_errors = _extract_gemini_receipt(receipt_path)
    errors.extend(receipt_errors)
    if artifact is None:
        return errors, None
    expected = {
        "schema": SMOKE_SCHEMA,
        "result": "pass",
        "result_code": "ok",
        "validation_kind": SMOKE_VALIDATION_KIND,
        "validation_authority": SMOKE_VALIDATION_AUTHORITY,
        "exit_code": 0,
    }
    for key, value in expected.items():
        if artifact.get(key) != value:
            errors.append(f"smoke artifact {key} is not approved")
    if not _valid_timestamp(str(artifact.get("timestamp", ""))):
        errors.append("smoke artifact timestamp is invalid")
    if not RUN_ID.fullmatch(str(artifact.get("run_id", ""))):
        errors.append("smoke artifact run id is invalid")
    if not HEX40.fullmatch(str(artifact.get("tested_commit", ""))):
        errors.append("smoke artifact candidate is invalid")
    if not IMAGE_DIGEST.fullmatch(str(artifact.get("image_digest", ""))):
        errors.append("smoke artifact image is not a full repository@sha256 digest")
    if not HEX64.fullmatch(str(artifact.get("smoke_module_sha256", ""))):
        errors.append("smoke artifact module SHA-256 is invalid")
    compose = root / "docker-compose.production.yml"
    try:
        compose_text = compose.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        errors.append("production compose is unavailable")
    else:
        if (
            "image: ${GEMINI_IMAGE_DIGEST:?checker-approved repository@sha256 digest required}"
            not in compose_text
        ):
            errors.append(
                "production compose does not consume the checker-approved digest"
            )
        for service, image in EXPECTED_PRODUCTION_IMAGES.items():
            if f"image: {image}" not in compose_text:
                errors.append(
                    f"production {service} image is not pinned to its approved digest"
                )
    if not errors:
        errors.extend(_validate_git_release(root, artifact))
    return errors, str(artifact["image_digest"])


def validate(
    receipts_path: Path = RECEIPTS_PATH,
    phase: str = "inventory",
    *,
    root: Path = ROOT,
    gemini_receipt_path: Path | None = None,
) -> list[str]:
    if phase not in {"inventory", "ui", "release"}:
        return [f"unknown phase: {phase}"]
    if phase != "release":
        return _validate_receipts(receipts_path)
    errors, _digest = _validate_release(
        root, receipts_path, gemini_receipt_path or root / GEMINI_RECEIPT_RELATIVE
    )
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        print(
            "release validation failed: Python 3.11 or newer is required",
            file=sys.stderr,
        )
        return 1
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--phase", choices=("inventory", "ui", "release"), required=True
    )
    parser.add_argument("--receipts", type=Path, default=RECEIPTS_PATH)
    parser.add_argument("--gemini-receipt", type=Path, default=GEMINI_RECEIPT_PATH)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    if args.phase == "release":
        errors, digest = _validate_release(
            args.root, args.receipts, args.gemini_receipt
        )
    else:
        errors, digest = _validate_receipts(args.receipts), None
    if errors:
        print("provider availability evidence validation failed:", file=sys.stderr)
        print("\n".join(f"- {error}" for error in errors), file=sys.stderr)
        return 1
    if digest:
        print(digest)
    else:
        print(f"provider availability evidence {args.phase} validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
