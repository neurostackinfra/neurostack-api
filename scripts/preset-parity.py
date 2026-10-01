#!/usr/bin/env python3
"""Capture and compare deterministic source-container/preset parity manifests."""

from __future__ import annotations

import argparse
import ast
import difflib
import importlib
import json
import re
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from enum import Enum
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "packages" / "neurostack-core"
SOURCE_ROOT = PACKAGE_ROOT / "neurostack_core"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))


def _normalize_regex_pattern(pattern: str) -> str:
    """Decode active, semantically literal Unicode letter/mark escapes."""
    normalized: list[str] = []
    cursor = 0
    while cursor < len(pattern):
        if pattern[cursor] != "\\":
            normalized.append(pattern[cursor])
            cursor += 1
            continue

        run_start = cursor
        while cursor < len(pattern) and pattern[cursor] == "\\":
            cursor += 1
        backslash_count = cursor - run_start
        if cursor >= len(pattern) or pattern[cursor] != "u":
            normalized.append(pattern[run_start:cursor])
            continue

        if backslash_count == 2:
            normalized.append(pattern[run_start:cursor])
            continue
        if backslash_count != 1:
            raise ValueError(
                "unsupported backslash run before Unicode regex escape at "
                f"offset {run_start}: expected one active or two literal backslashes"
            )

        digits = pattern[cursor + 1 : cursor + 5]
        if len(digits) != 4 or any(
            character not in "0123456789abcdefABCDEF" for character in digits
        ):
            raise ValueError(
                f"malformed active Unicode regex escape at offset {run_start}: "
                "expected exactly four hexadecimal digits"
            )

        character = chr(int(digits, 16))
        category = unicodedata.category(character)
        if ord(character) < 128 or category[0] not in {"L", "M"}:
            raise ValueError(
                f"unsafe active Unicode regex escape \\u{digits} at offset "
                f"{run_start}: decoded codepoint must be a non-ASCII Unicode "
                "letter or mark"
            )
        normalized.append(character)
        cursor += 5

    return "".join(normalized)


def _normalize_regex_container(value: Any) -> Any:
    """Normalize a container already known to contain only regex payloads."""
    if isinstance(value, str):
        return _normalize_regex_pattern(value)
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_regex_container(item) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_regex_container(item) for item in value]
    raise TypeError(f"cannot normalize regex payload {type(value).__name__}")


def _normalize_definition_patterns(value: Any) -> Any:
    """Normalize only the ``patterns`` member of definition mappings."""
    normalized = _normalize(value)
    for definition in normalized.values():
        definition["patterns"] = _normalize_regex_container(definition["patterns"])
    return normalized


def _normalize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, re.Pattern):
        return value.pattern
    if hasattr(value, "model_dump"):
        return _normalize(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {str(_normalize(key)): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_normalize(item) for item in value), key=str)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"cannot normalize {type(value).__name__}")


def _inventory_helpers() -> dict[str, Any]:
    import runpy

    return runpy.run_path(str(ROOT / "scripts" / "pt-br-inventory.py"))


def _target_names(node: ast.Assign | ast.AnnAssign) -> set[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return {target.id for target in targets if isinstance(target, ast.Name)}


def _imperative_literals(
    relative_path: str, excluded_assignments: set[str]
) -> dict[str, list[str]]:
    helpers = _inventory_helpers()
    allowlist = helpers["_load_allowlist"]()
    protocol = importlib.import_module("neurostack_core.protocol")
    # Preserve the pre-migration imperative-literal parity surface even though
    # engine lookup keys are now canonical inventory exemptions.
    allowlist -= protocol.ENGINE_REGEX_KEYS
    is_pt_br = helpers["is_pt_br_literal"]
    path = SOURCE_ROOT / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = helpers["_docstring_nodes"](tree)
    result: dict[str, list[str]] = {}

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = (*scope, node.name)
        if (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and len(scope) == 0
            and (_target_names(node) & excluded_assignments)
        ):
            return
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and is_pt_br(node.value, allowlist)
        ):
            key = ".".join(scope) if scope else "<module>"
            result.setdefault(key, []).append(node.value)
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree, ())
    return result


def _all_imperative_literals(
    definitions: Mapping[str, set[str]],
) -> dict[str, dict[str, list[str]]]:
    return {
        relative_path.removesuffix(".py").replace("/", "."): _imperative_literals(
            relative_path, excluded
        )
        for relative_path, excluded in definitions.items()
        if (SOURCE_ROOT / relative_path).exists()
    }


def _capture_srm() -> dict[str, Any]:
    protocol = importlib.import_module("neurostack_core.protocol")
    models = importlib.import_module("neurostack_core.srm.models")
    intents = importlib.import_module("neurostack_core.srm.intent_classifier")
    safety = importlib.import_module("neurostack_core.srm.safety_detector")
    funnel = importlib.import_module("neurostack_core.srm.funnel_detector")
    rag = importlib.import_module("neurostack_core.srm.rag_decider")
    handoff = importlib.import_module("neurostack_core.srm.handoff_decider")
    engine = importlib.import_module("neurostack_core.srm.engine")

    transitions = [
        {"from": source.value, "intent": intent, "to": target.value}
        for (source, intent), target in funnel.TRANSITION_TRIGGERS.items()
    ]
    handoff_defaults = handoff.HandoffDecider()
    return {
        "schema_version": 1,
        "protocol": {
            "intent_categories": [item.value for item in models.IntentCategory],
            "funnel_stages": [item.value for item in models.FunnelStage],
            "safety_categories": [item.value for item in models.SafetyCategory],
            "sentiment_labels": [item.value for item in models.SentimentLabel],
            "flow_modes": [item.value for item in models.FlowMode],
            "handoff_reasons": [item.value for item in models.HandoffReason],
            "official_intents": sorted(protocol.OFFICIAL_INTENT_IDS),
            "actions": sorted(protocol.ACTION_IDS),
            "engine_regex_keys": sorted(protocol.ENGINE_REGEX_KEYS),
        },
        "intents": _normalize_definition_patterns(intents.INTENT_DEFINITIONS),
        "intents_en": _normalize_definition_patterns(intents.EN_INTENT_PATTERNS),
        "safety_guards": [
            _normalize_regex_pattern(item.pattern)
            for item in intents._SAFETY_GUARD_PATTERNS
        ],
        "funnel": {
            "stages": [item.value for item in models.FunnelStage],
            "metadata": _normalize(funnel.FUNNEL_STAGES),
            "transitions": transitions,
            "turn_thresholds": _normalize(funnel.TURN_THRESHOLDS),
        },
        "safety_patterns": _normalize_definition_patterns(safety.SAFETY_PATTERNS),
        "rag": {
            "patterns": _normalize_regex_container(rag.RAG_TRIGGER_PATTERNS),
            "intent_categories": _normalize(rag.RAG_INTENT_CATEGORIES),
            "confidence_threshold": 0.7,
        },
        "handoff": {
            "explicit_patterns": _normalize_regex_container(
                handoff.EXPLICIT_HANDOFF_PATTERNS
            ),
            "regexes": {
                "scheduling_has_time": _normalize_regex_pattern(
                    handoff._SCHEDULING_HAS_TIME_RE.pattern
                ),
                "strong_positive": _normalize_regex_pattern(
                    handoff._STRONG_POSITIVE_RE.pattern
                ),
                "weak_positive": _normalize_regex_pattern(
                    handoff._WEAK_POSITIVE_RE.pattern
                ),
                "short_affirmative": _normalize_regex_pattern(
                    handoff._SHORT_AFFIRMATIVE_RE.pattern
                ),
                "negative": _normalize_regex_pattern(handoff._NEGATIVE_RE.pattern),
                "scheduling_completed": _normalize_regex_pattern(
                    handoff._SCHEDULING_COMPLETED_RE.pattern
                ),
            },
            "escalation_keywords": list(handoff_defaults.escalation_keywords),
            "max_turns": handoff_defaults.max_turns,
            "min_confidence": handoff_defaults.min_confidence,
        },
        "engine": {
            "regexes": {
                "scheduling_has_time": _normalize_regex_pattern(
                    engine._SCHEDULING_HAS_TIME_RE.pattern
                ),
                "atendimento_tipo": _normalize_regex_pattern(
                    engine._ATENDIMENTO_TIPO_RE.pattern
                ),
            },
            "intent_actions": _normalize(engine.INTENT_ACTIONS),
            "category_actions": _normalize(engine.CATEGORY_ACTIONS),
            "confidence_threshold": 0.7,
            "safety_threshold": 0.5,
            "max_turns_for_handoff": 10,
        },
        "imperative_literals": _all_imperative_literals(
            {
                "srm/intent_classifier.py": {
                    "INTENT_DEFINITIONS",
                    "EN_INTENT_PATTERNS",
                    "_SAFETY_GUARD_PATTERNS",
                },
                "srm/safety_detector.py": {"SAFETY_PATTERNS"},
                "srm/funnel_detector.py": {
                    "FUNNEL_STAGES",
                    "TRANSITION_TRIGGERS",
                    "TURN_THRESHOLDS",
                },
                "srm/rag_decider.py": {
                    "RAG_TRIGGER_PATTERNS",
                    "RAG_INTENT_CATEGORIES",
                },
                "srm/handoff_decider.py": {
                    "EXPLICIT_HANDOFF_PATTERNS",
                    "_SCHEDULING_HAS_TIME_RE",
                    "_STRONG_POSITIVE_RE",
                    "_WEAK_POSITIVE_RE",
                    "_SHORT_AFFIRMATIVE_RE",
                    "_NEGATIVE_RE",
                    "_SCHEDULING_COMPLETED_RE",
                },
                "srm/engine.py": {
                    "_SCHEDULING_HAS_TIME_RE",
                    "_ATENDIMENTO_TIPO_RE",
                    "INTENT_ACTIONS",
                    "CATEGORY_ACTIONS",
                },
                "srm/models.py": {"OFFICIAL_INTENTS"},
            }
        ),
    }


def _capture_flow() -> dict[str, Any]:
    protocol = importlib.import_module("neurostack_core.protocol")
    models = importlib.import_module("neurostack_core.flow.models")
    state_machine = importlib.import_module("neurostack_core.flow.state_machine")
    handoff = importlib.import_module("neurostack_core.flow.handlers.handoff")
    safety = importlib.import_module("neurostack_core.flow.handlers.safety")
    rag = importlib.import_module("neurostack_core.flow.handlers.rag")

    transitions = [
        {
            "from": source.value,
            "trigger": trigger.value,
            "to": target.value,
        }
        for (source, trigger), target in state_machine.VALID_TRANSITIONS.items()
    ]
    return {
        "schema_version": 1,
        "protocol": {
            "handoff_response_keys": sorted(protocol.HANDOFF_RESPONSE_KEYS),
            "safety_response_keys": sorted(protocol.SAFETY_RESPONSE_KEYS),
        },
        "handoff_responses": _normalize(handoff.HANDOFF_RESPONSES),
        "safety_responses": _normalize(safety.SAFETY_RESPONSES),
        "state_machine": {
            "flow_modes": [item.value for item in models.FlowMode],
            "transition_triggers": [item.value for item in models.TransitionTrigger],
            "transitions": transitions,
            "mode_priority": _normalize(state_machine.MODE_PRIORITY),
        },
        "thresholds": {
            "safety_escalation": safety.SafetyModeHandler().escalation_threshold,
            "rag_max_turns": rag.RagModeHandler().max_rag_turns,
        },
        "imperative_literals": _all_imperative_literals(
            {
                "flow/handlers/handoff.py": {"HANDOFF_RESPONSES"},
                "flow/handlers/safety.py": {"SAFETY_RESPONSES"},
                "flow/handlers/normal.py": set(),
                "flow/handlers/rag.py": set(),
            }
        ),
    }


def _capture_policy() -> dict[str, Any]:
    protocol = importlib.import_module("neurostack_core.protocol")
    rules = importlib.import_module("neurostack_core.policy.global_rules")

    normalized_rules = []
    for rule in rules.GLOBAL_SAFETY_POLICY.rules:
        normalized_rules.append(
            {
                "rule_id": rule.rule_id,
                "rule": rule.rule,
                "message": rule.message,
                "description": rule.description,
                "category": rule.category.value,
                "severity": rule.severity.value,
                "action": rule.action.value,
                "patterns": list(rule.patterns),
                "keywords": list(rule.keywords),
                "enabled": rule.enabled,
                "metadata": _normalize(rule.metadata),
            }
        )
    return {
        "schema_version": 1,
        "protocol": {
            "rule_ids": sorted(protocol.POLICY_RULE_IDS),
            "rule_names": sorted(protocol.POLICY_RULE_NAMES),
        },
        "rules": normalized_rules,
        "global_policy": {
            "name": rules.GLOBAL_SAFETY_POLICY.name,
            "description": rules.GLOBAL_SAFETY_POLICY.description,
            "policy_type": rules.GLOBAL_SAFETY_POLICY.policy_type.value,
            "priority": rules.GLOBAL_SAFETY_POLICY.priority,
            "is_active": rules.GLOBAL_SAFETY_POLICY.is_active,
        },
        "constraints": _normalize(rules.GLOBAL_CONSTRAINTS),
        "constraint_ids": [
            rules.CONSTRAINT_SEQUENCE_AUTHORITY_BEFORE_PRICE,
            rules.CONSTRAINT_OBJECTION_NO_PASSIVE_CLOSE,
        ],
        "flow_policy_ids": [
            rules.POLICY_ONB_001,
            rules.POLICY_QUAL_001,
            rules.POLICY_OBJ_001,
            rules.POLICY_PRICE_002,
            rules.POLICY_HANDOFF_001,
        ],
        "imperative_literals": _all_imperative_literals(
            {
                "policy/global_rules.py": {
                    "RULE_NO_DIAGNOSIS",
                    "RULE_NO_MEDICATION",
                    "RULE_NO_TREATMENT_GUARANTEE",
                    "RULE_NO_LEGAL_ADVICE",
                    "RULE_NO_FALSE_PROMISES",
                    "RULE_PRIVACY_COMPLIANCE",
                    "RULE_PROFESSIONAL_TONE",
                    "RULE_EMERGENCY_REDIRECT",
                    "GLOBAL_SAFETY_POLICY",
                    "GLOBAL_CONSTRAINTS",
                },
                "policy/models.py": set(),
            }
        ),
    }


def _capture_prompt() -> dict[str, Any]:
    protocol = importlib.import_module("neurostack_core.protocol")
    normal = importlib.import_module("neurostack_core.prompt.templates.normal")
    handoff = importlib.import_module("neurostack_core.prompt.templates.handoff")
    safety = importlib.import_module("neurostack_core.prompt.templates.safety")
    renderer = importlib.import_module("neurostack_core.prompt.renderer")

    stages = {
        item.value
        for item in importlib.import_module("neurostack_core.protocol").FunnelStage
    }
    funnel_instructions = {
        key: value for key, value in normal.FUNNEL_INSTRUCTIONS.items() if key in stages
    }
    legacy_funnel_instructions = {
        key: value
        for key, value in normal.FUNNEL_INSTRUCTIONS.items()
        if key not in stages
    }
    funnel_instructions_en = {
        key: value
        for key, value in normal.EN_FUNNEL_INSTRUCTIONS.items()
        if key in stages
    }
    legacy_funnel_instructions_en = {
        key: value
        for key, value in normal.EN_FUNNEL_INSTRUCTIONS.items()
        if key not in stages
    }

    return {
        "schema_version": 1,
        "protocol": {"lookup_keys": sorted(protocol.PROMPT_LOOKUP_KEYS)},
        "normal_system_prompt": normal.NORMAL_SYSTEM_PROMPT,
        "normal_system_prompt_en": normal.EN_SYSTEM_PROMPT,
        "funnel_instructions": _normalize(funnel_instructions),
        "legacy_funnel_instructions": _normalize(legacy_funnel_instructions),
        "funnel_instructions_en": _normalize(funnel_instructions_en),
        "legacy_funnel_instructions_en": _normalize(legacy_funnel_instructions_en),
        "handoff_system_prompt": handoff.HANDOFF_SYSTEM_PROMPT,
        "handoff_reason_instructions": _normalize(handoff.HANDOFF_REASON_INSTRUCTIONS),
        "safety_system_prompt": safety.SAFETY_SYSTEM_PROMPT,
        "safety_category_instructions": _normalize(safety.SAFETY_CATEGORY_INSTRUCTIONS),
        "chars_per_token": renderer.CHARS_PER_TOKEN,
        "imperative_literals": _all_imperative_literals(
            {
                "prompt/templates/normal.py": {
                    "NORMAL_SYSTEM_PROMPT",
                    "EN_SYSTEM_PROMPT",
                    "FUNNEL_INSTRUCTIONS",
                    "EN_FUNNEL_INSTRUCTIONS",
                },
                "prompt/templates/handoff.py": {
                    "HANDOFF_SYSTEM_PROMPT",
                    "HANDOFF_REASON_INSTRUCTIONS",
                },
                "prompt/templates/safety.py": {
                    "SAFETY_SYSTEM_PROMPT",
                    "SAFETY_CATEGORY_INSTRUCTIONS",
                },
                "prompt/templates/base.py": set(),
                "prompt/renderer.py": {"CHARS_PER_TOKEN"},
            }
        ),
    }


def capture_source() -> dict[str, Any]:
    return {
        "format_version": 1,
        "modules": {
            "srm": _capture_srm(),
            "flow": _capture_flow(),
            "policy": _capture_policy(),
            "prompt": _capture_prompt(),
        },
    }


def _toml_key(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_string(value: str) -> str:
    if "'''" in value or any(
        ord(character) < 0x20 and character not in "\n\t" for character in value
    ):
        return json.dumps(value, ensure_ascii=False)
    prefix = "\n" if value.startswith("\n") else ""
    return "'''" + prefix + value + "'''"


def _toml_scalar(value: Any) -> str:
    if isinstance(value, str):
        return _toml_string(value)
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        if not value:
            return "[]"
        rendered = ",\n".join("  " + _toml_scalar(item) for item in value)
        return "[\n" + rendered + ",\n]"
    if isinstance(value, dict) and not value:
        return "{}"
    raise TypeError(f"unsupported TOML scalar: {type(value).__name__}")


def _is_array_table(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, dict) for item in value)
    )


def _write_table(
    lines: list[str], path: tuple[str, ...], value: Mapping[str, Any], header: bool
) -> None:
    if header:
        lines.append("[" + ".".join(_toml_key(part) for part in path) + "]")
    nested: list[tuple[str, Mapping[str, Any]]] = []
    arrays: list[tuple[str, Sequence[Mapping[str, Any]]]] = []
    for key, item in value.items():
        if isinstance(item, dict) and item:
            nested.append((key, item))
        elif _is_array_table(item):
            arrays.append((key, item))
        else:
            lines.append(f"{_toml_key(key)} = {_toml_scalar(item)}")
    lines.append("")
    for key, item in nested:
        _write_table(lines, (*path, key), item, True)
    for key, items in arrays:
        table_path = (*path, key)
        for item in items:
            lines.append("[[" + ".".join(_toml_key(part) for part in table_path) + "]]")
            nested_items: list[tuple[str, Mapping[str, Any]]] = []
            array_items: list[tuple[str, Sequence[Mapping[str, Any]]]] = []
            for item_key, item_value in item.items():
                if isinstance(item_value, dict) and item_value:
                    nested_items.append((item_key, item_value))
                elif _is_array_table(item_value):
                    array_items.append((item_key, item_value))
                else:
                    lines.append(f"{_toml_key(item_key)} = {_toml_scalar(item_value)}")
            lines.append("")
            for item_key, item_value in nested_items:
                _write_table(lines, (*table_path, item_key), item_value, True)
            for item_key, item_value in array_items:
                # The captured schemas do not currently need doubly nested arrays
                # of tables; fail instead of emitting ambiguous TOML.
                raise TypeError(
                    f"nested array table not supported at {'.'.join((*table_path, item_key))}"
                )


def dump_toml(value: Mapping[str, Any]) -> str:
    lines = [
        "# Generated from pre-rewrite source containers by scripts/preset-parity.py."
    ]
    _write_table(lines, (), value, False)
    return "\n".join(lines).rstrip() + "\n"


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def command_capture(args: argparse.Namespace) -> int:
    captured = capture_source()
    args.baseline.parent.mkdir(parents=True, exist_ok=True)
    args.baseline.write_text(
        json.dumps(captured, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if args.preset_dir is not None:
        args.preset_dir.mkdir(parents=True, exist_ok=True)
        for module, value in captured["modules"].items():
            (args.preset_dir / f"{module}.toml").write_text(
                dump_toml(value), encoding="utf-8"
            )
    print(f"captured {len(captured['modules'])} modules to {args.baseline.as_posix()}")
    return 0


def command_compare(args: argparse.Namespace) -> int:
    from neurostack_core.presets import use_preset

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    preset = use_preset(args.preset)
    requested = [args.module] if args.module else ["srm", "flow", "policy", "prompt"]
    failures = 0
    for module in requested:
        expected = baseline["modules"][module]
        actual = _thaw(getattr(preset, module))
        if actual == expected:
            print(f"PASS {module}: normalized containers match")
            continue
        failures += 1
        expected_text = json.dumps(
            expected, ensure_ascii=False, indent=2, sort_keys=True
        )
        actual_text = json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True)
        diff = difflib.unified_diff(
            expected_text.splitlines(),
            actual_text.splitlines(),
            fromfile=f"baseline:{module}",
            tofile=f"preset:{module}",
            n=2,
        )
        print("\n".join(list(diff)[:80]), file=sys.stderr)
    return 1 if failures else 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser("capture")
    capture.add_argument("--baseline", type=Path, required=True)
    capture.add_argument("--preset-dir", type=Path)
    capture.set_defaults(func=command_capture)
    compare = subparsers.add_parser("compare")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument(
        "--preset",
        required=True,
        type=lambda value: value if re.fullmatch(r"[a-z0-9_]+", value) else Path(value),
    )
    compare.add_argument("--module", choices=("srm", "flow", "policy", "prompt"))
    compare.set_defaults(func=command_compare)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
