#!/usr/bin/env python3
"""Render one deterministic prompt-parity session as stable JSON text."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "packages" / "neurostack-core"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    payload: dict[str, Any] = json.loads(args.session.read_text(encoding="utf-8"))

    from neurostack_core.prompt.models import PromptConfig, PromptVariables
    from neurostack_core.prompt.renderer import PromptRenderer
    from neurostack_core.prompt.templates import normal

    fixed_now = datetime.fromisoformat(payload["now"])
    normal.clinic_now = lambda _timezone: fixed_now  # type: ignore[assignment]

    variables = PromptVariables.model_validate(payload["variables"])
    config = PromptConfig.model_validate(payload["config"])
    generated = PromptRenderer().render(
        variables, config, history_messages=payload.get("history")
    )
    stable = {
        "messages": generated.to_messages_list(),
        "mode": generated.mode.value,
        "total_tokens_estimate": generated.total_tokens_estimate,
        "variables_used": sorted(generated.variables_used),
        "warnings": generated.warnings,
    }
    print(json.dumps(stable, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
