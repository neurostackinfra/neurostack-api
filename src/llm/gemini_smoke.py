"""Canonical, secret-free live smoke for the production Gemini transport."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import re
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.llm.client import LLMClient, LLMClientError
from src.services.api_credentials import GEMINI_API_BASE_URL

SMOKE_PROMPT = "Reply with exactly: GEMINI_SMOKE_OK"
EXPECTED_RESPONSE = "GEMINI_SMOKE_OK"
SCHEMA = "gemini-llm-smoke/v1"
VALIDATION_KIND = "direct_live_smoke"
VALIDATION_AUTHORITY = "Production Operations Owner"
RESULT_KEYS = frozenset(
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
NEXT_STEPS: Mapping[str, str] = {
    "missing_api_key": "Set GEMINI_API_KEY in the private runtime environment.",
    "missing_model": "Set GEMINI_MODEL to an enabled Gemini model identifier.",
    "provider_http_error": "Verify key and model access, then create a new run.",
    "provider_transport_error": "Verify pinned-host HTTPS egress, then create a new run.",
    "invalid_provider_response": "Verify OpenAI compatibility, then create a new run.",
    "unexpected_response": "Verify the fixed instruction, then create a new run.",
    "internal_error": (
        "Unexpected local smoke failure; inspect protected runner diagnostics, "
        "repair the runner or image, then create a new run."
    ),
}
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
IMAGE_RE = re.compile(
    r"[a-z0-9][a-z0-9._-]*(?::[0-9]+)?(?:/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}"
)
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def source_sha256() -> str:
    """Return the SHA-256 of the module bytes copied into the OCI image."""

    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


async def run_smoke(api_key: str | None, model: str | None) -> tuple[str, str]:
    """Run the fixed production request and return a public result/code pair."""

    if not api_key:
        return "fail", "missing_api_key"
    if not model:
        return "fail", "missing_model"

    client = LLMClient(
        base_url=GEMINI_API_BASE_URL,
        api_key=api_key,
        egress_kind="llm_gemini",
    )
    try:
        response = await client.generate(
            messages=[{"role": "user", "content": SMOKE_PROMPT}],
            model=model,
            temperature=0.0,
            max_tokens=32,
        )
    except LLMClientError as exc:
        code = exc.code if exc.code in NEXT_STEPS else "internal_error"
        return "fail", code
    except Exception:
        return "fail", "internal_error"
    finally:
        await client.close()

    if response.get("content", "").strip() != EXPECTED_RESPONSE:
        return "fail", "unexpected_response"
    return "pass", "ok"


def canonical_json(value: Mapping[str, Any]) -> str:
    """Serialize canonical UTF-8 JSON; the caller owns the final newline."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", required=True)
    parser.add_argument("--tested-commit", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--run-id", required=True)
    return parser


def _validated_args(argv: Sequence[str] | None) -> argparse.Namespace:
    args = _parser().parse_args(argv)
    if not COMMIT_RE.fullmatch(args.tested_commit):
        raise SystemExit("tested commit must be a lowercase 40-hex Git object id")
    if not IMAGE_RE.fullmatch(args.image_digest):
        raise SystemExit("image digest must be a full repository@sha256 reference")
    if not RUN_ID_RE.fullmatch(args.run_id):
        raise SystemExit("run id has an invalid public format")
    return args


def _artifact(
    args: argparse.Namespace,
    outcome: tuple[str, str],
    *,
    timestamp: str | None = None,
) -> dict[str, Any]:
    result, result_code = outcome
    exit_code = 0 if result == "pass" else 2
    return {
        "schema": SCHEMA,
        "result": result,
        "result_code": result_code,
        "validation_kind": VALIDATION_KIND,
        "validation_authority": VALIDATION_AUTHORITY,
        "timestamp": timestamp
        or datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "run_id": args.run_id,
        "tested_commit": args.tested_commit,
        "image_digest": args.image_digest,
        "smoke_module_sha256": source_sha256(),
        "exit_code": exit_code,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Emit exactly one canonical JSON line after isolating client diagnostics."""

    args = _validated_args(argv)
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        outcome = asyncio.run(
            run_smoke(os.environ.get("GEMINI_API_KEY"), os.environ.get("GEMINI_MODEL"))
        )
    artifact = _artifact(args, outcome)
    print(canonical_json(artifact), flush=True)
    return int(artifact["exit_code"])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
