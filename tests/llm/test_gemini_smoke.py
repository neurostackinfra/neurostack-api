"""Contract tests for the canonical Gemini live-smoke artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from src.llm import gemini_smoke
from src.llm.client import LLMClientError
from src.services.api_credentials import GEMINI_API_BASE_URL

COMMIT = "a" * 40
IMAGE = "registry.example/neurostack/api@sha256:" + "b" * 64


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        json=True, tested_commit=COMMIT, image_digest=IMAGE, run_id="run-42"
    )


@pytest.mark.asyncio
async def test_smoke_uses_fixed_production_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def generate(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"content": gemini_smoke.EXPECTED_RESPONSE, "private": "discarded"}

        async def close(self) -> None:
            return None

    monkeypatch.setattr(gemini_smoke, "LLMClient", FakeClient)
    assert await gemini_smoke.run_smoke("PRIVATE", "gemini-model") == ("pass", "ok")
    assert captured["base_url"] == GEMINI_API_BASE_URL
    assert captured["egress_kind"] == "llm_gemini"
    assert captured["messages"] == [
        {"role": "user", "content": gemini_smoke.SMOKE_PROMPT}
    ]
    assert captured["model"] == "gemini-model"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_key", "model", "expected"),
    [(None, "m", "missing_api_key"), ("k", None, "missing_model")],
)
async def test_missing_configuration_fails_closed(
    api_key: str | None, model: str | None, expected: str
) -> None:
    assert await gemini_smoke.run_smoke(api_key, model) == ("fail", expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (LLMClientError("provider_http_error"), "provider_http_error"),
        (LLMClientError("provider_transport_error"), "provider_transport_error"),
        (RuntimeError("PRIVATE"), "internal_error"),
    ],
)
async def test_failures_are_reduced_to_static_codes(
    monkeypatch: pytest.MonkeyPatch, error: Exception, expected: str
) -> None:
    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def generate(self, **_kwargs: Any) -> dict[str, Any]:
            raise error

        async def close(self) -> None:
            pass

    monkeypatch.setattr(gemini_smoke, "LLMClient", FakeClient)
    assert await gemini_smoke.run_smoke("PRIVATE", "PRIVATE-MODEL") == (
        "fail",
        expected,
    )


def test_artifact_is_exact_closed_canonical_contract() -> None:
    artifact = gemini_smoke._artifact(
        _args(), ("pass", "ok"), timestamp="2026-09-08T20:00:00Z"
    )
    assert set(artifact) == gemini_smoke.RESULT_KEYS
    assert artifact["exit_code"] == 0
    rendered = gemini_smoke.canonical_json(artifact) + "\n"
    assert rendered.encode("utf-8").decode("utf-8") == rendered
    assert (
        rendered
        == json.dumps(
            artifact, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        + "\n"
    )
    assert (
        hashlib.sha256(Path(gemini_smoke.__file__).read_bytes()).hexdigest()
        == artifact["smoke_module_sha256"]
    )


@pytest.mark.parametrize(
    "argv",
    [
        [],
        [
            "--json",
            "--tested-commit",
            "bad",
            "--image-digest",
            IMAGE,
            "--run-id",
            "run",
        ],
        [
            "--json",
            "--tested-commit",
            COMMIT,
            "--image-digest",
            "api:tag",
            "--run-id",
            "run",
        ],
        [
            "--json",
            "--tested-commit",
            COMMIT,
            "--image-digest",
            IMAGE,
            "--run-id",
            "bad run",
        ],
    ],
)
def test_required_public_arguments_fail_closed(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        gemini_smoke._validated_args(argv)


def test_main_emits_one_line_and_reads_only_two_env_inputs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_MODEL", "model")
    monkeypatch.setenv("GEMINI_PROMPT", "PRIVATE-PROMPT")
    argv = [
        "--json",
        "--tested-commit",
        COMMIT,
        "--image-digest",
        IMAGE,
        "--run-id",
        "run",
    ]
    assert gemini_smoke.main(argv) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    parsed = json.loads(captured.out)
    assert set(parsed) == gemini_smoke.RESULT_KEYS
    assert parsed["result_code"] == "missing_api_key"
    assert "PRIVATE-PROMPT" not in captured.out
