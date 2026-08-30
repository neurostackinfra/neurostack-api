"""Validate the bounded provider-availability evidence without network access."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = ROOT / ".genie/wishes/api-provider-availability/evidence"
DESIGN_SHA = "527a35a82cdf4776004942bf1db99fe9e5e246214e60f1e3b86ff892a0d8ca0a"
RECEIPTS_PATH = EVIDENCE_DIR / "provider-availability-receipts.md"

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
    ("tts", "microsoft"): "removed",
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
    "depends_on": "none",
    "blocks": "api-provider-runtime-shutdown",
}

SENSITIVE_PATTERNS = {
    "secret": r"(?i)(api[_ -]?key|secret|token|password|bearer\s+[a-z0-9])",
    "tenant URL": r"(?i)(https?://|postgres(?:ql)?://)",
    "tenant identifier": r"(?i)(tenant[_ -]?id|tenant\s*=|tenant-[a-z0-9]{4,})",
    "clinic data": r"(?i)\bclinic\b",
    "patient data": r"(?i)\bpatient\b",
    "personal data": r"(?i)([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}|\b(personal|person)\s+(name|email|data)\b)",
}


def _parse_metadata(text: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"- ([a-z_]+): (.+)", line.strip())
        if match:
            metadata[match.group(1)] = match.group(2).strip()
    return metadata


def _parse_table(text: str, columns: tuple[str, ...]) -> list[dict[str, str]]:
    lines = [line.strip() for line in text.splitlines()]
    header = "| " + " | ".join(columns) + " |"
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
        if len(values) != len(columns):
            raise ValueError("table row has the wrong number of fields")
        rows.append(dict(zip(columns, values, strict=True)))
    return rows


def _valid_timestamp(value: str) -> bool:
    if not (value.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", value)):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _errors_for_metadata(metadata: dict[str, str]) -> list[str]:
    return [
        f"metadata {key} must be {expected!r}"
        for key, expected in HEADER_VALUES.items()
        if metadata.get(key) != expected
    ]


def _sensitive_errors(text: str) -> list[str]:
    return [
        f"evidence contains {description}"
        for description, pattern in SENSITIVE_PATTERNS.items()
        if re.search(pattern, text)
    ]


def _validate_receipts(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"cannot read receipts: {exc}"]
    errors = _errors_for_metadata(_parse_metadata(text)) + _sensitive_errors(text)
    try:
        rows = _parse_table(text, RECEIPT_COLUMNS)
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
        if row["decision"] not in {"active", "coming_soon", "removed"}:
            errors.append(f"invalid decision: {pair}")
        if row["displayable"] not in {"true", "false"}:
            errors.append(f"invalid displayable flag: {pair}")
        if not _valid_timestamp(row["validated_at"]):
            errors.append(f"invalid timestamp: {pair}")

    if set(seen) != set(EXPECTED_DECISIONS):
        errors.append("receipt pairs do not exactly match the 22-pair inventory")
    counts = {decision: sum(row["decision"] == decision for row in rows) for decision in ("active", "coming_soon", "removed")}
    if counts != {"active": 0, "coming_soon": 19, "removed": 3}:
        errors.append("receipt decisions must partition to 0 active, 19 coming_soon, 3 removed")
    if sum(row["displayable"] == "true" for row in rows) != 19:
        errors.append("exactly 19 receipt rows must be displayable")

    for row in rows:
        pair = (row["capability"], row["provider"])
        expected = EXPECTED_DECISIONS.get(pair)
        if expected and row["decision"] != expected:
            errors.append(f"wrong decision for {pair}")
        if row["displayable"] != ("false" if row["decision"] == "removed" else "true"):
            errors.append(f"displayability disagrees with decision: {pair}")
        if (
            row["test_command"],
            row["test_exit_code"],
            row["test_result"],
            row["validation_kind"],
            row["validation_authority"],
        ) != ("not_run", "not_run", "not_run", "not_available", "not_available"):
            errors.append(f"receipt must record unavailable validation only: {pair}")
    return errors


def validate(receipts_path: Path = RECEIPTS_PATH, phase: str = "inventory") -> list[str]:
    if phase not in {"inventory", "ui", "release"}:
        return [f"unknown phase: {phase}"]
    # UI/release are explicit phase hooks. Inventory is intentionally the only
    # evidence gate owned by this execution group.
    return _validate_receipts(receipts_path)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("inventory", "ui", "release"), required=True)
    parser.add_argument("--receipts", type=Path, default=RECEIPTS_PATH)
    args = parser.parse_args(argv)
    errors = validate(args.receipts, args.phase)
    if errors:
        print("provider availability evidence validation failed:")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print(f"provider availability evidence {args.phase} validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
