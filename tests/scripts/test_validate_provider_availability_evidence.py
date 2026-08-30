from __future__ import annotations

from pathlib import Path

import pytest

from scripts.validate_provider_availability_evidence import (
    EXPECTED_DECISIONS,
    validate,
)


def _metadata(extra: str = "") -> str:
    return "\n".join(
        (
            "- wish_slug: api-provider-availability",
            "- governing_design_sha: 527a35a82cdf4776004942bf1db99fe9e5e246214e60f1e3b86ff892a0d8ca0a",
            "- status: IN_PROGRESS",
            "- depends_on: none",
            "- blocks: api-provider-runtime-shutdown",
            extra,
        )
    ).strip()


def _receipt_row(pair: tuple[str, str], decision: str) -> str:
    return "| " + " | ".join(
        (
            pair[0], pair[1], "not_run", "not_run", "not_run", "not_available", "not_available",
            "2026-08-29T00:00:00Z", decision,
            "false" if decision == "removed" else "true",
        )
    ) + " |"


def _write_valid(tmp_path: Path) -> Path:
    receipts = tmp_path / "receipts.md"
    receipt_rows = "\n".join(_receipt_row(pair, decision) for pair, decision in EXPECTED_DECISIONS.items())
    receipts.write_text(
        "# Provider availability receipts\n\n"
        + _metadata()
        + "\n\n| capability | provider | test_command | test_exit_code | test_result | validation_kind | validation_authority | validated_at | decision | displayable |\n"
        + "|---|---|---|---|---|---|---|---|---|---|\n"
        + receipt_rows
        + "\n",
        encoding="utf-8",
    )
    return receipts


def test_valid_fixture_passes(tmp_path: Path) -> None:
    assert validate(_write_valid(tmp_path)) == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        (lambda text: text.replace("| whatsapp | meta_cloud |", "| whatsapp | absent |", 1), "22-pair"),
        (lambda text: text.replace("| whatsapp | zapi |", "| whatsapp | meta_cloud |", 1), "duplicate"),
        (lambda text: text.replace("| whatsapp | evolution | not_run | not_run | not_run | not_available | not_available | 2026-08-29T00:00:00Z | coming_soon | true |", "| whatsapp | evolution | not_run | not_run | not_run | not_available | not_available | 2026-08-29T00:00:00Z | active | true |"), "0 active"),
        (lambda text: text.replace("| multimodal | groq | not_run | not_run | not_run | not_available | not_available | 2026-08-29T00:00:00Z | removed | false |", "| multimodal | groq | not_run | not_run | not_run | not_available | not_available | 2026-08-29T00:00:00Z | removed | true |"), "displayability"),
        (lambda text: text.replace("| not_run | not_run | not_run | not_available | not_available |", "| command | not_run | not_run | not_available | not_available |", 1), "unavailable validation"),
        (lambda text: text.replace("| not_run | not_run | not_run | not_available | not_available |", "| not_run |  | not_run | not_available | not_available |", 1), "empty field"),
        (lambda text: text.replace("not_available", "Verifier", 1), "unavailable validation"),
        (lambda text: text.replace("2026-08-29T00:00:00Z", "2026-08-29", 1), "invalid timestamp"),
    ),
)
def test_receipt_violations_fail_closed(tmp_path: Path, mutation, expected: str) -> None:
    receipts = _write_valid(tmp_path)
    receipts.write_text(mutation(receipts.read_text(encoding="utf-8")), encoding="utf-8")
    assert any(expected in error for error in validate(receipts))


def test_receipt_only_validation_does_not_require_extra_evidence(tmp_path: Path) -> None:
    receipts = _write_valid(tmp_path)
    assert validate(receipts, "inventory") == []


def test_all_displayable_pairs_are_coming_soon() -> None:
    assert sum(decision == "coming_soon" for decision in EXPECTED_DECISIONS.values()) == 19
    assert sum(decision == "active" for decision in EXPECTED_DECISIONS.values()) == 0
    assert sum(decision == "removed" for decision in EXPECTED_DECISIONS.values()) == 3


@pytest.mark.parametrize(
    "sensitive_value",
    (
        "api_key: example",
        "https://tenant.example.invalid",
        "tenant_id: 1234",
        "clinic record",
        "patient record",
        "alex@example.invalid",
    ),
)
def test_sensitive_data_fails_closed(tmp_path: Path, sensitive_value: str) -> None:
    receipts = _write_valid(tmp_path)
    receipts.write_text(receipts.read_text(encoding="utf-8") + f"\n{sensitive_value}\n", encoding="utf-8")
    assert any("evidence contains" in error for error in validate(receipts))
