"""Fail-closed preset loading and configure-once process selection."""

from __future__ import annotations

import math
import os
import re
import string
import threading
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from neurostack_core.presets.models import Preset, PresetError, freeze
from neurostack_core.protocol import (
    ACTION_IDS,
    ENGINE_REGEX_KEYS,
    HANDOFF_RESPONSE_KEYS,
    OFFICIAL_INTENT_IDS,
    POLICY_RULE_IDS,
    POLICY_RULE_NAMES,
    POLICY_RULE_PAIRS,
    PROMPT_LOOKUP_KEYS,
    PROTOCOL_IDENTIFIERS,
    SAFETY_RESPONSE_KEYS,
    FlowMode,
    FunnelStage,
    HandoffReason,
    IntentCategory,
    SafetyCategory,
    SentimentLabel,
)

_FILES = ("srm.toml", "flow.toml", "policy.toml", "prompt.toml")
_FILE_LIMIT = 1024 * 1024
_BUNDLED_NAME_RE = re.compile(r"[a-z0-9_]+\Z")
_PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
_PROMPT_PLACEHOLDERS_BY_PATH: Mapping[str, frozenset[str]] = {
    "normal_system_prompt": frozenset(
        {
            "agent_name",
            "agent_presentation",
            "channel",
            "clinic_name",
            "funnel_stage",
            "turn_count",
        }
    ),
    "normal_system_prompt_en": frozenset(
        {
            "agent_name",
            "agent_presentation",
            "channel",
            "clinic_name",
            "funnel_stage",
            "turn_count",
        }
    ),
    "handoff_system_prompt": frozenset(
        {
            "agent_name",
            "channel",
            "clinic_name",
            "handoff_reason",
            "handoff_status",
        }
    ),
    "safety_system_prompt": frozenset(
        {
            "agent_name",
            "channel",
            "clinic_name",
            "safety_category",
            "safety_level",
        }
    ),
    "funnel_instructions.lead_novo": frozenset({"agent_name", "clinic_name"}),
    "funnel_instructions_en.lead_novo": frozenset({"agent_name", "clinic_name"}),
}
_PROMPT_LEXICON_KEYS = frozenset(
    {
        "complimentary_en",
        "complimentary_pt",
        "currency_prefix",
        "free_en",
        "free_feminine_pt",
        "free_pt",
    }
)
_PROMPT_IMPERATIVE_SCHEMA: Mapping[str, Mapping[str, int]] = {
    "prompt.renderer": {
        "PromptRenderer.render": 1,
        "VariableResolver.from_context_window": 3,
    },
    "prompt.renderer.residual": {"current_stage_label": 1},
    "prompt.templates.base": {
        "BaseTemplate._get_response_format_reminder": 1,
        "BaseTemplate.get_constraints": 1,
        "BaseTemplate.get_safety_reminders": 1,
    },
    "prompt.templates.handoff": {
        "HandoffTemplate.generate_handoff_summary": 7,
        "HandoffTemplate.get_context_instructions": 9,
    },
    "prompt.templates.handoff.residual": {
        "channel_label": 1,
        "context_title": 1,
        "history_title": 1,
        "language_instruction_en": 1,
        "turns_label": 1,
    },
    "prompt.templates.normal": {
        "NormalTemplate._build_clinic_context": 29,
        "NormalTemplate._get_intent_instruction": 33,
        "NormalTemplate.build_system_message": 25,
        "NormalTemplate.get_context_instructions": 8,
        "NormalTemplate.get_system_prompt": 1,
    },
    "prompt.templates.normal.residual": {
        "agent_clinic_joiner": 1,
        "assistant_default": 1,
        "confirmation_collected_en": 1,
        "confirmation_header_en": 1,
        "confirmation_label_en": 1,
        "confirmation_prohibited_en": 1,
        "confirmation_prohibited_es": 1,
        "confirmation_question_en": 1,
        "confirmation_question_es": 1,
        "context_language_override_en": 1,
        "current_datetime_label": 1,
        "current_datetime_format": 1,
        "differentials_label": 1,
        "emoji_instruction": 1,
        "good_evening": 1,
        "greeting_time_format": 1,
        "knowledge_context_title": 1,
        "knowledge_sources_label": 1,
        "language_instruction_en": 1,
        "no_announce_en": 1,
        "no_confirm_en": 1,
        "no_documents_en": 1,
        "no_extra_en": 1,
        "parking_label": 1,
        "scheduling_link_failure_en": 1,
        "scheduling_link_header": 1,
        "scheduling_link_success_en": 1,
        "specialties_label": 1,
        "system_confirmed_en": 1,
        "system_declined_en": 1,
        "system_declined_pt": 1,
    },
    "prompt.templates.safety": {
        "SafetyTemplate.get_constraints": 5,
        "SafetyTemplate.get_context_instructions": 6,
    },
    "prompt.templates.safety.residual": {
        "history_title": 1,
        "no_medication_dosages": 1,
        "resources_title": 1,
    },
}
_FLOW_IMPERATIVE_SCHEMA: Mapping[str, Mapping[str, int]] = {
    "flow.handlers.handoff": {
        "HandoffModeHandler.get_mode_prompt_additions": 4,
    },
    "flow.handlers.normal": {
        "NormalModeHandler.get_mode_prompt_additions": 3,
    },
    "flow.handlers.rag": {
        "RagModeHandler.get_mode_prompt_additions": 4,
    },
    "flow.handlers.safety": {
        "SafetyModeHandler.get_mode_prompt_additions": 5,
    },
}
_SRM_IMPERATIVE_ARRAY_SCHEMA: Mapping[str, Mapping[str, int]] = {
    "srm.engine": {
        "SRMEngine._extract_slots": 5,
        "SRMEngine.process": 3,
    },
    "srm.funnel_detector": {},
    "srm.handoff_decider": {
        "HandoffDecider.__init__": 1,
        "HandoffDecider._check_escalation_keywords": 1,
        "HandoffDecider._check_safety_handoff": 2,
        "HandoffDecider.decide": 11,
    },
    "srm.intent_classifier": {},
    "srm.models": {},
    "srm.rag_decider": {"RagDecider._generate_query": 8},
    "srm.safety_detector": {"SafetyDetector.get_recommended_response": 3},
}
_SRM_IMPERATIVE_MAPPING_SCHEMA: Mapping[str, Mapping[str, frozenset[str]]] = {
    "srm.engine": {
        "SRMEngine._get_action_details": ACTION_IDS,
        "SRMEngine.output_values": frozenset(
            {
                "high",
                "priority",
                "verification_completed",
                "verification_not_completed",
            }
        ),
    },
    "srm.handoff_decider": {
        "HandoffDecider.output_values": frozenset(
            {
                "administrative",
                "immediate",
                "priority",
                "routine",
                "verification_completed",
                "verification_not_completed",
            }
        ),
    },
}

_selection_lock = threading.Lock()
_selected: Preset | None = None


@dataclass(frozen=True, slots=True)
class _Source:
    name: str
    source_id: str
    directory: Any


def _summary(value: Any, *, redact_strings: bool = False) -> str:
    if isinstance(value, str):
        if redact_strings:
            return f"<str length={len(value)}>"
        if len(value) <= 80 and "\n" not in value:
            return repr(value)
        return f"<str length={len(value)}>"
    if isinstance(value, Mapping):
        return f"<mapping keys={len(value)}>"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return f"<{type(value).__name__} length={len(value)}>"
    if value is None or isinstance(value, (bool, int, float)):
        rendered = repr(value)
        return rendered if len(rendered) <= 80 else f"<{type(value).__name__}>"
    return f"<{type(value).__name__}>"


def _error(
    filename: str,
    key: str,
    rejected: Any,
    expected: str,
    next_action: str,
) -> PresetError:
    return PresetError(
        f"{filename}: {key}: rejected "
        f"{_summary(rejected, redact_strings=filename == 'prompt.toml')}; "
        f"expected {expected}; "
        f"next action: {next_action}"
    )


def _canonical_identifier_or_redacted(value: str) -> str:
    if value in PROTOCOL_IDENTIFIERS:
        return value
    return "<redacted noncanonical identifier>"


def _resolve_source(name_or_path: str | os.PathLike[str]) -> _Source:
    if isinstance(name_or_path, str):
        if not _BUNDLED_NAME_RE.fullmatch(name_or_path):
            raise _error(
                "<preset>",
                "name",
                name_or_path,
                "a bundled preset name matching [a-z0-9_]+",
                "pass a safe bundled name, or use pathlib.Path for a trusted directory",
            )
        directory = files("neurostack_core.presets").joinpath(name_or_path)
        if not directory.is_dir():
            raise _error(
                "<preset>",
                "name",
                name_or_path,
                "an installed bundled preset directory",
                "install the package data or choose an available bundled preset",
            )
        return _Source(name_or_path, f"bundled:{name_or_path}", directory)

    if not isinstance(name_or_path, os.PathLike):
        raise _error(
            "<preset>",
            "source",
            name_or_path,
            "str bundled name or os.PathLike[str] trusted directory",
            "pass a bundled name or pathlib.Path",
        )
    raw_path = os.fspath(name_or_path)
    if not isinstance(raw_path, str):
        raise _error(
            "<preset>",
            "source",
            raw_path,
            "os.PathLike[str] rather than bytes",
            "return a text path from __fspath__",
        )
    try:
        resolved = Path(raw_path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _error(
            "<preset>",
            "path",
            raw_path,
            "an existing trusted preset directory",
            f"create or correct the directory ({type(exc).__name__})",
        ) from None
    if not resolved.is_dir():
        raise _error(
            "<preset>",
            "path",
            str(resolved),
            "a directory containing the four preset TOMLs",
            "pass the containing directory, not an individual file",
        )
    return _Source(resolved.name, f"path:{resolved}", resolved)


def _read_source(source: _Source) -> dict[str, dict[str, Any]]:
    try:
        extra = sorted(
            child.name
            for child in source.directory.iterdir()
            if child.name.endswith(".toml") and child.name not in _FILES
        )
    except OSError as exc:
        raise _error(
            "<preset>",
            "$",
            source.source_id,
            "a readable preset directory",
            f"check directory permissions ({type(exc).__name__})",
        ) from None
    if extra:
        raise _error(
            extra[0],
            "$",
            extra[0],
            f"only the fixed files {', '.join(_FILES)}",
            "remove or rename the unknown TOML file",
        )

    documents: dict[str, dict[str, Any]] = {}
    for filename in _FILES:
        resource = source.directory.joinpath(filename)
        if not resource.is_file():
            raise _error(
                filename,
                "$",
                "missing",
                "one readable TOML file for every preset module",
                f"add {filename} and retry selection",
            )
        try:
            with resource.open("rb") as handle:
                payload = handle.read(_FILE_LIMIT + 1)
        except OSError as exc:
            raise _error(
                filename,
                "$",
                "unreadable",
                "a readable UTF-8 TOML file",
                f"check file permissions ({type(exc).__name__})",
            ) from None
        if len(payload) > _FILE_LIMIT:
            raise _error(
                filename,
                "$",
                f"{len(payload)}+ bytes",
                f"at most {_FILE_LIMIT} bytes per preset file",
                "reduce the file below the 1 MiB safety cap",
            )
        try:
            parsed = tomllib.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            raise _error(
                filename,
                "$",
                "invalid syntax or encoding",
                "valid UTF-8 TOML",
                "fix the TOML syntax without embedding escaped source payloads",
            ) from None
        documents[filename] = parsed
    return documents


def _mapping(value: Any, filename: str, key: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _error(filename, key, value, "a TOML table", "replace it with a table")
    return value


def _list(value: Any, filename: str, key: str) -> list[Any]:
    if not isinstance(value, list):
        raise _error(filename, key, value, "a TOML array", "replace it with an array")
    return value


def _string(value: Any, filename: str, key: str) -> None:
    if not isinstance(value, str):
        raise _error(filename, key, value, "a string", "replace it with text")


def _enum_string(value: Any, filename: str, key: str) -> str:
    """Return a hashable string before callers perform enum membership checks."""

    if not isinstance(value, str):
        raise _error(filename, key, value, "a string", "replace it with text")
    return value


def _boolean(value: Any, filename: str, key: str) -> None:
    if not isinstance(value, bool):
        raise _error(filename, key, value, "a boolean", "replace it with true or false")


def _string_list(value: Any, filename: str, key: str) -> list[str]:
    items = _list(value, filename, key)
    for index, item in enumerate(items):
        if not isinstance(item, str):
            raise _error(
                filename,
                f"{key}[{index}]",
                item,
                "a string",
                "replace it with text",
            )
    return items


def _string_leaf_tree(value: Any, filename: str, key: str) -> None:
    """Validate generated imperative-literal tables without fixing their keys."""

    table = _mapping(value, filename, key)
    for child_key, child in table.items():
        dotted = f"{key}.{child_key}"
        if isinstance(child, Mapping):
            _string_leaf_tree(child, filename, dotted)
        else:
            _string_list(child, filename, dotted)


def _keys(
    value: Mapping[str, Any], filename: str, key: str, expected: Iterable[str]
) -> None:
    expected_set = set(expected)
    actual = set(value)
    if actual != expected_set:
        missing = sorted(expected_set - actual)
        extra = sorted(actual - expected_set)
        mismatch_key = key
        rejected: Any = f"missing={missing!r}; extra={extra!r}"
        if extra:
            mismatch_key = f"{key}.{extra[0]}" if key != "$" else extra[0]
            rejected = extra[0]
        elif missing:
            mismatch_key = f"{key}.{missing[0]}" if key != "$" else missing[0]
            rejected = "missing"
        raise _error(
            filename,
            mismatch_key,
            rejected,
            f"exact keys {sorted(expected_set)!r}",
            "add missing keys and remove unknown keys",
        )


def _redacted_exact_keys(
    value: Mapping[str, Any], filename: str, key: str, expected: Iterable[str]
) -> None:
    expected_set = set(expected)
    actual = set(value)
    if actual == expected_set:
        return
    missing = sorted(expected_set - actual)
    extra_count = len(actual - expected_set)
    diagnostics = [f"missing structural keys={missing!r}"]
    if extra_count:
        diagnostics.append(f"extra structural keys=<redacted count={extra_count}>")
    raise _error(
        filename,
        key,
        {"missing": len(missing), "extra": extra_count},
        f"the exact structural key set ({len(expected_set)} keys); "
        + "; ".join(diagnostics),
        "restore missing keys and remove unknown keys",
    )


def _redacted_mapping(value: Any, filename: str, key: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _error(
            filename,
            key,
            {"value": "redacted"},
            "a TOML table",
            "replace it with the required structural table",
        )
    return value


def _exact_string_array(
    value: Any, filename: str, key: str, expected_length: int
) -> None:
    if not isinstance(value, list):
        raise _error(
            filename,
            key,
            {"value": "redacted"},
            "a TOML array",
            "replace it with the required structural array",
        )
    items = value
    if len(items) != expected_length:
        raise _error(
            filename,
            key,
            {"item_count": len(items)},
            f"exactly {expected_length} string items",
            "restore the exact consumer-indexed array length",
        )
    for index, item in enumerate(items):
        if not isinstance(item, str):
            raise _error(
                filename,
                f"{key}[{index}]",
                {"value": "redacted"},
                "a string",
                "replace it with text",
            )


def _exact_string_mapping(
    value: Any,
    filename: str,
    key: str,
    expected_keys: Iterable[str],
) -> None:
    table = _redacted_mapping(value, filename, key)
    _redacted_exact_keys(table, filename, key, expected_keys)
    for child_key, child in table.items():
        if not isinstance(child, str):
            raise _error(
                filename,
                f"{key}.{child_key}",
                {"value": "redacted"},
                "a string",
                "replace it with text",
            )


def _path(document: Mapping[str, Any], filename: str, dotted: str) -> Any:
    current: Any = document
    traversed: list[str] = []
    for part in dotted.split("."):
        traversed.append(part)
        mapping = _mapping(current, filename, ".".join(traversed[:-1]) or "$")
        if part not in mapping:
            raise _error(
                filename,
                ".".join(traversed),
                "missing",
                "a required preset key",
                f"add {'.'.join(traversed)}",
            )
        current = mapping[part]
    return current


def _string_set(value: Any, filename: str, key: str) -> set[str]:
    items = _string_list(value, filename, key)
    result: set[str] = set()
    for index, item in enumerate(items):
        if item in result:
            raise _error(
                filename,
                f"{key}[{index}]",
                _canonical_identifier_or_redacted(item),
                "unique identifiers",
                "remove the duplicate",
            )
        result.add(item)
    return result


def _exact_values(
    document: Mapping[str, Any],
    filename: str,
    key: str,
    expected: Iterable[str],
) -> None:
    actual = _string_set(_path(document, filename, key), filename, key)
    expected_set = set(expected)
    if actual != expected_set:
        missing = sorted(expected_set - actual)
        extra = sorted(actual - expected_set)
        canonical_extra = sorted(set(extra) & PROTOCOL_IDENTIFIERS)
        redacted_extra_count = len(extra) - len(canonical_extra)
        diagnostics = [f"missing canonical identifiers={missing!r}"]
        if canonical_extra:
            diagnostics.append(f"extra canonical identifiers={canonical_extra!r}")
        if redacted_extra_count:
            diagnostics.append(
                "extra noncanonical identifiers="
                f"<redacted count={redacted_extra_count}>"
            )
        raise _error(
            filename,
            key,
            {"missing": len(missing), "extra": len(extra)},
            f"the exact canonical namespace ({len(expected_set)} values); "
            + "; ".join(diagnostics),
            "restore the missing canonical values and remove extras",
        )


def _compile_regex(value: Any, filename: str, key: str) -> None:
    if not isinstance(value, str):
        raise _error(filename, key, value, "a regex string", "replace it with a string")
    try:
        re.compile(value, re.IGNORECASE | re.UNICODE)
    except re.error as exc:
        raise _error(
            filename,
            key,
            value,
            "a compilable Python regular expression",
            f"fix the regex near position {getattr(exc, 'pos', 'unknown')}",
        ) from None


def _integer_at_least(value: Any, filename: str, key: str, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _error(
            filename,
            key,
            value,
            f"an integer greater than or equal to {minimum}",
            "replace it with a valid integer threshold",
        )


def _number_at_least(value: Any, filename: str, key: str, minimum: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (isinstance(value, float) and not math.isfinite(value))
        or value < minimum
    ):
        raise _error(
            filename,
            key,
            value,
            f"a number greater than or equal to {minimum}",
            "replace it with a valid number",
        )


def _schema_version(document: Mapping[str, Any], filename: str) -> None:
    value = document["schema_version"]
    if isinstance(value, bool) or not isinstance(value, int) or value != 1:
        raise _error(
            filename, "schema_version", value, "integer 1", "regenerate the preset"
        )


def _number_between(
    value: Any, filename: str, key: str, minimum: float, maximum: float
) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (isinstance(value, float) and not math.isfinite(value))
        or not minimum < value <= maximum
    ):
        raise _error(
            filename,
            key,
            value,
            f"a number greater than {minimum} and at most {maximum}",
            "replace it with a valid numeric threshold",
        )


def _regex_array(value: Any, filename: str, key: str) -> None:
    for index, item in enumerate(_list(value, filename, key)):
        _compile_regex(item, filename, f"{key}[{index}]")


def _validate_srm(document: Mapping[str, Any]) -> None:
    filename = "srm.toml"
    _keys(
        document,
        filename,
        "$",
        {
            "schema_version",
            "protocol",
            "intents",
            "intents_en",
            "safety_guards",
            "funnel",
            "safety_patterns",
            "rag",
            "handoff",
            "engine",
            "imperative_literals",
        },
    )
    _schema_version(document, filename)
    protocol = _mapping(document["protocol"], filename, "protocol")
    _keys(
        protocol,
        filename,
        "protocol",
        {
            "intent_categories",
            "funnel_stages",
            "safety_categories",
            "sentiment_labels",
            "flow_modes",
            "handoff_reasons",
            "official_intents",
            "actions",
            "engine_regex_keys",
        },
    )
    exact_namespaces = {
        "protocol.intent_categories": {item.value for item in IntentCategory},
        "protocol.funnel_stages": {item.value for item in FunnelStage},
        "protocol.safety_categories": {item.value for item in SafetyCategory},
        "protocol.sentiment_labels": {item.value for item in SentimentLabel},
        "protocol.flow_modes": {item.value for item in FlowMode},
        "protocol.handoff_reasons": {item.value for item in HandoffReason},
        "protocol.official_intents": OFFICIAL_INTENT_IDS,
        "protocol.actions": ACTION_IDS,
        "protocol.engine_regex_keys": ENGINE_REGEX_KEYS,
    }
    for key, expected in exact_namespaces.items():
        _exact_values(document, filename, key, expected)

    intents = _mapping(document["intents"], filename, "intents")
    intents_en = _mapping(document["intents_en"], filename, "intents_en")
    _keys(intents, filename, "intents", OFFICIAL_INTENT_IDS)
    _keys(intents_en, filename, "intents_en", OFFICIAL_INTENT_IDS)
    for table_name, table in (("intents", intents), ("intents_en", intents_en)):
        fields = {"patterns", "keywords"}
        if table_name == "intents":
            fields |= {"category", "weight"}
        for intent, raw in table.items():
            definition = _mapping(raw, filename, f"{table_name}.{intent}")
            _keys(definition, filename, f"{table_name}.{intent}", fields)
            _regex_array(
                definition["patterns"], filename, f"{table_name}.{intent}.patterns"
            )
            _string_list(
                definition["keywords"], filename, f"{table_name}.{intent}.keywords"
            )
            if table_name == "intents":
                category_key = f"{table_name}.{intent}.category"
                category = _enum_string(definition["category"], filename, category_key)
                if category not in {item.value for item in IntentCategory}:
                    raise _error(
                        filename,
                        category_key,
                        category,
                        "a canonical intent category",
                        "use a value from protocol.intent_categories",
                    )
                _number_at_least(
                    definition["weight"],
                    filename,
                    f"{table_name}.{intent}.weight",
                    0.0,
                )
    _regex_array(document["safety_guards"], filename, "safety_guards")

    funnel = _mapping(document["funnel"], filename, "funnel")
    _keys(
        funnel,
        filename,
        "funnel",
        {"stages", "metadata", "transitions", "turn_thresholds"},
    )
    stages = {item.value for item in FunnelStage}
    _exact_values(document, filename, "funnel.stages", stages)
    metadata = _mapping(funnel["metadata"], filename, "funnel.metadata")
    thresholds = _mapping(funnel["turn_thresholds"], filename, "funnel.turn_thresholds")
    _keys(metadata, filename, "funnel.metadata", stages)
    _keys(thresholds, filename, "funnel.turn_thresholds", stages)
    for stage, threshold in thresholds.items():
        _integer_at_least(threshold, filename, f"funnel.turn_thresholds.{stage}", 0)
    for stage, raw in metadata.items():
        definition = _mapping(raw, filename, f"funnel.metadata.{stage}")
        _keys(
            definition,
            filename,
            f"funnel.metadata.{stage}",
            {"description", "next_stages", "entry_conditions"},
        )
        _string(
            definition["description"], filename, f"funnel.metadata.{stage}.description"
        )
        next_stages = _string_set(
            definition["next_stages"], filename, f"funnel.metadata.{stage}.next_stages"
        )
        unknown = next_stages - stages
        if unknown:
            raise _error(
                filename,
                f"funnel.metadata.{stage}.next_stages",
                sorted(unknown),
                "only canonical funnel stages",
                "replace unknown cross-references",
            )
        _string_list(
            definition["entry_conditions"],
            filename,
            f"funnel.metadata.{stage}.entry_conditions",
        )
    transitions = _list(funnel["transitions"], filename, "funnel.transitions")
    for index, raw in enumerate(transitions):
        transition = _mapping(raw, filename, f"funnel.transitions[{index}]")
        _keys(
            transition,
            filename,
            f"funnel.transitions[{index}]",
            {"from", "intent", "to"},
        )
        for field in ("from", "to"):
            field_key = f"funnel.transitions[{index}].{field}"
            reference = _enum_string(transition[field], filename, field_key)
            if reference not in stages:
                raise _error(
                    filename,
                    field_key,
                    reference,
                    "a canonical funnel stage",
                    "repair the transition cross-reference",
                )
        intent_key = f"funnel.transitions[{index}].intent"
        intent = _enum_string(transition["intent"], filename, intent_key)
        if intent not in OFFICIAL_INTENT_IDS:
            raise _error(
                filename,
                intent_key,
                intent,
                "an official intent id",
                "repair the transition cross-reference",
            )

    safety_patterns = _mapping(document["safety_patterns"], filename, "safety_patterns")
    expected_safety = {
        item.value for item in SafetyCategory if item is not SafetyCategory.NONE
    }
    _keys(safety_patterns, filename, "safety_patterns", expected_safety)
    for category, raw in safety_patterns.items():
        definition = _mapping(raw, filename, f"safety_patterns.{category}")
        _keys(
            definition,
            filename,
            f"safety_patterns.{category}",
            {"description", "patterns", "keywords", "action", "urgency"},
        )
        _regex_array(
            definition["patterns"], filename, f"safety_patterns.{category}.patterns"
        )
        _string(
            definition["description"],
            filename,
            f"safety_patterns.{category}.description",
        )
        _string_list(
            definition["keywords"],
            filename,
            f"safety_patterns.{category}.keywords",
        )
        _string(definition["action"], filename, f"safety_patterns.{category}.action")
        _string(definition["urgency"], filename, f"safety_patterns.{category}.urgency")

    rag = _mapping(document["rag"], filename, "rag")
    _keys(
        rag, filename, "rag", {"patterns", "intent_categories", "confidence_threshold"}
    )
    rag_patterns = _mapping(rag["patterns"], filename, "rag.patterns")
    for topic, patterns in rag_patterns.items():
        _regex_array(patterns, filename, f"rag.patterns.{topic}")
    if not _string_set(rag["intent_categories"], filename, "rag.intent_categories") <= {
        item.value for item in IntentCategory
    }:
        raise _error(
            filename,
            "rag.intent_categories",
            rag["intent_categories"],
            "canonical intent categories",
            "remove unknown category references",
        )
    _number_between(
        rag["confidence_threshold"], filename, "rag.confidence_threshold", 0.0, 1.0
    )

    handoff = _mapping(document["handoff"], filename, "handoff")
    _keys(
        handoff,
        filename,
        "handoff",
        {
            "explicit_patterns",
            "regexes",
            "escalation_keywords",
            "max_turns",
            "min_confidence",
        },
    )
    _regex_array(handoff["explicit_patterns"], filename, "handoff.explicit_patterns")
    _string_list(
        handoff["escalation_keywords"], filename, "handoff.escalation_keywords"
    )
    regexes = _mapping(handoff["regexes"], filename, "handoff.regexes")
    _keys(
        regexes,
        filename,
        "handoff.regexes",
        {
            "scheduling_has_time",
            "strong_positive",
            "weak_positive",
            "short_affirmative",
            "negative",
            "scheduling_completed",
        },
    )
    for name, pattern in regexes.items():
        _compile_regex(pattern, filename, f"handoff.regexes.{name}")
    _integer_at_least(handoff["max_turns"], filename, "handoff.max_turns", 1)
    _number_between(
        handoff["min_confidence"], filename, "handoff.min_confidence", 0.0, 1.0
    )

    engine = _mapping(document["engine"], filename, "engine")
    _keys(
        engine,
        filename,
        "engine",
        {
            "regexes",
            "intent_actions",
            "category_actions",
            "confidence_threshold",
            "safety_threshold",
            "max_turns_for_handoff",
        },
    )
    engine_regexes = _mapping(engine["regexes"], filename, "engine.regexes")
    _keys(
        engine_regexes,
        filename,
        "engine.regexes",
        ENGINE_REGEX_KEYS,
    )
    for name, pattern in engine_regexes.items():
        _compile_regex(pattern, filename, f"engine.regexes.{name}")
    intent_actions = _mapping(
        engine["intent_actions"], filename, "engine.intent_actions"
    )
    category_actions = _mapping(
        engine["category_actions"], filename, "engine.category_actions"
    )
    if not set(intent_actions) <= set(OFFICIAL_INTENT_IDS):
        raise _error(
            filename,
            "engine.intent_actions",
            sorted(set(intent_actions) - set(OFFICIAL_INTENT_IDS)),
            "official intent keys",
            "remove unknown intent action keys",
        )
    _keys(
        category_actions,
        filename,
        "engine.category_actions",
        {item.value for item in IntentCategory},
    )
    for table_name, actions in (
        ("intent_actions", intent_actions),
        ("category_actions", category_actions),
    ):
        for key, raw_action in actions.items():
            action_key = f"engine.{table_name}.{key}"
            action = _enum_string(raw_action, filename, action_key)
            if action in ACTION_IDS:
                continue
            raise _error(
                filename,
                action_key,
                action,
                "a canonical action id",
                "use protocol.actions",
            )
    _number_between(
        engine["confidence_threshold"],
        filename,
        "engine.confidence_threshold",
        0.0,
        1.0,
    )
    _number_between(
        engine["safety_threshold"],
        filename,
        "engine.safety_threshold",
        0.0,
        1.0,
    )
    _integer_at_least(
        engine["max_turns_for_handoff"],
        filename,
        "engine.max_turns_for_handoff",
        1,
    )
    imperative_literals = _redacted_mapping(
        document["imperative_literals"], filename, "imperative_literals"
    )
    _redacted_exact_keys(
        imperative_literals,
        filename,
        "imperative_literals",
        _SRM_IMPERATIVE_ARRAY_SCHEMA,
    )
    for module_name, array_schema in _SRM_IMPERATIVE_ARRAY_SCHEMA.items():
        module_key = f"imperative_literals.{module_name}"
        module = _redacted_mapping(
            imperative_literals[module_name], filename, module_key
        )
        mapping_schema = _SRM_IMPERATIVE_MAPPING_SCHEMA.get(module_name, {})
        _redacted_exact_keys(
            module,
            filename,
            module_key,
            set(array_schema) | set(mapping_schema),
        )
        for entry_name, expected_length in array_schema.items():
            _exact_string_array(
                module[entry_name],
                filename,
                f"{module_key}.{entry_name}",
                expected_length,
            )
        for entry_name, expected_keys in mapping_schema.items():
            _exact_string_mapping(
                module[entry_name],
                filename,
                f"{module_key}.{entry_name}",
                expected_keys,
            )


def _validate_flow(document: Mapping[str, Any]) -> None:
    filename = "flow.toml"
    _keys(
        document,
        filename,
        "$",
        {
            "schema_version",
            "protocol",
            "handoff_responses",
            "safety_responses",
            "state_machine",
            "thresholds",
            "imperative_literals",
        },
    )
    _schema_version(document, filename)
    protocol = _mapping(document["protocol"], filename, "protocol")
    _keys(
        protocol,
        filename,
        "protocol",
        {"handoff_response_keys", "safety_response_keys"},
    )
    _exact_values(
        document, filename, "protocol.handoff_response_keys", HANDOFF_RESPONSE_KEYS
    )
    _exact_values(
        document, filename, "protocol.safety_response_keys", SAFETY_RESPONSE_KEYS
    )
    handoff = _mapping(document["handoff_responses"], filename, "handoff_responses")
    safety = _mapping(document["safety_responses"], filename, "safety_responses")
    _keys(handoff, filename, "handoff_responses", HANDOFF_RESPONSE_KEYS)
    _keys(safety, filename, "safety_responses", SAFETY_RESPONSE_KEYS)
    formatter = string.Formatter()
    allowed_fields = {"waiting": {"wait_time"}, "connected": {"agent_name"}}
    for response_key, response in handoff.items():
        if not isinstance(response, str):
            raise _error(
                filename,
                f"handoff_responses.{response_key}",
                response,
                "a response string",
                "replace it with text",
            )
        try:
            parsed_fields = [
                (field, format_spec, conversion)
                for _, field, format_spec, conversion in formatter.parse(response)
                if field
            ]
        except ValueError:
            raise _error(
                filename,
                f"handoff_responses.{response_key}",
                {"template": "redacted"},
                "valid format placeholders",
                "repair unmatched braces",
            ) from None
        unsupported_syntax = sum(
            bool(format_spec) or conversion is not None
            for _, format_spec, conversion in parsed_fields
        )
        if unsupported_syntax:
            raise _error(
                filename,
                f"handoff_responses.{response_key}",
                {"unsupported_placeholder_syntax": unsupported_syntax},
                "plain placeholders without conversions, format specs, or nested fields",
                "remove placeholder conversions and format specifications",
            )
        fields = {field for field, _, _ in parsed_fields}
        expected = allowed_fields.get(response_key, set())
        if fields != expected:
            unknown_count = len(fields - expected)
            missing = sorted(expected - fields)
            raise _error(
                filename,
                f"handoff_responses.{response_key}",
                {"missing": len(missing), "unknown": unknown_count},
                f"format placeholders {sorted(expected)!r}; "
                f"missing placeholders={missing!r}; "
                f"unknown placeholders=<redacted count={unknown_count}>",
                "restore the response placeholder contract",
            )

    for response_key, response in safety.items():
        _string(response, filename, f"safety_responses.{response_key}")

    state = _mapping(document["state_machine"], filename, "state_machine")
    _keys(
        state,
        filename,
        "state_machine",
        {"flow_modes", "transition_triggers", "transitions", "mode_priority"},
    )
    modes = {item.value for item in FlowMode}
    _exact_values(document, filename, "state_machine.flow_modes", modes)
    triggers = _string_set(
        state["transition_triggers"], filename, "state_machine.transition_triggers"
    )
    transitions = _list(state["transitions"], filename, "state_machine.transitions")
    for index, raw in enumerate(transitions):
        transition = _mapping(raw, filename, f"state_machine.transitions[{index}]")
        _keys(
            transition,
            filename,
            f"state_machine.transitions[{index}]",
            {"from", "trigger", "to"},
        )
        for field in ("from", "to"):
            field_key = f"state_machine.transitions[{index}].{field}"
            reference = _enum_string(transition[field], filename, field_key)
            if reference not in modes:
                raise _error(
                    filename,
                    field_key,
                    reference,
                    "a declared flow mode",
                    "repair the transition cross-reference",
                )
        trigger_key = f"state_machine.transitions[{index}].trigger"
        trigger = _enum_string(transition["trigger"], filename, trigger_key)
        if trigger not in triggers:
            raise _error(
                filename,
                trigger_key,
                trigger,
                "a declared transition trigger",
                "repair the transition cross-reference",
            )
    priority = _mapping(state["mode_priority"], filename, "state_machine.mode_priority")
    _keys(priority, filename, "state_machine.mode_priority", modes)
    for mode, value in priority.items():
        _integer_at_least(value, filename, f"state_machine.mode_priority.{mode}", 0)
    thresholds = _mapping(document["thresholds"], filename, "thresholds")
    _keys(
        thresholds,
        filename,
        "thresholds",
        {"safety_escalation", "rag_max_turns"},
    )
    _integer_at_least(
        thresholds["safety_escalation"],
        filename,
        "thresholds.safety_escalation",
        1,
    )
    _integer_at_least(
        thresholds["rag_max_turns"], filename, "thresholds.rag_max_turns", 1
    )
    imperative_literals = _redacted_mapping(
        document["imperative_literals"], filename, "imperative_literals"
    )
    _redacted_exact_keys(
        imperative_literals,
        filename,
        "imperative_literals",
        _FLOW_IMPERATIVE_SCHEMA,
    )
    for module_name, entry_schema in _FLOW_IMPERATIVE_SCHEMA.items():
        module_key = f"imperative_literals.{module_name}"
        module = _redacted_mapping(
            imperative_literals[module_name], filename, module_key
        )
        _redacted_exact_keys(module, filename, module_key, entry_schema)
        for entry_name, expected_length in entry_schema.items():
            _exact_string_array(
                module[entry_name],
                filename,
                f"{module_key}.{entry_name}",
                expected_length,
            )


def _validate_policy(document: Mapping[str, Any]) -> None:
    filename = "policy.toml"
    _keys(
        document,
        filename,
        "$",
        {
            "schema_version",
            "protocol",
            "rules",
            "global_policy",
            "constraints",
            "constraint_ids",
            "flow_policy_ids",
            "imperative_literals",
        },
    )
    _schema_version(document, filename)
    protocol = _mapping(document["protocol"], filename, "protocol")
    _keys(protocol, filename, "protocol", {"rule_ids", "rule_names"})
    _exact_values(document, filename, "protocol.rule_ids", POLICY_RULE_IDS)
    _exact_values(document, filename, "protocol.rule_names", POLICY_RULE_NAMES)
    _string_list(document["constraint_ids"], filename, "constraint_ids")
    _string_list(document["flow_policy_ids"], filename, "flow_policy_ids")

    global_policy = _mapping(document["global_policy"], filename, "global_policy")
    _keys(
        global_policy,
        filename,
        "global_policy",
        {"name", "description", "policy_type", "priority", "is_active"},
    )
    _string(global_policy["name"], filename, "global_policy.name")
    _string(global_policy["description"], filename, "global_policy.description")
    _string(global_policy["policy_type"], filename, "global_policy.policy_type")
    if global_policy["policy_type"] not in {"global", "tenant", "safety"}:
        raise _error(
            filename,
            "global_policy.policy_type",
            global_policy["policy_type"],
            "one of global, tenant, or safety",
            "replace it with a supported policy type",
        )
    _integer_at_least(global_policy["priority"], filename, "global_policy.priority", 0)
    _boolean(global_policy["is_active"], filename, "global_policy.is_active")

    constraints = _mapping(document["constraints"], filename, "constraints")
    constraint_fields = {
        "must_not_say",
        "must_include",
        "tone_guidelines",
        "safety_reminders",
        "custom_instructions",
    }
    _keys(constraints, filename, "constraints", constraint_fields)
    for field in constraint_fields:
        _string_list(constraints[field], filename, f"constraints.{field}")

    rules = _list(document["rules"], filename, "rules")
    pairs: dict[str, str] = {}
    rule_fields = {
        "rule_id",
        "rule",
        "message",
        "description",
        "category",
        "severity",
        "action",
        "patterns",
        "keywords",
        "enabled",
        "metadata",
    }
    for index, raw in enumerate(rules):
        rule = _mapping(raw, filename, f"rules[{index}]")
        _keys(rule, filename, f"rules[{index}]", rule_fields)
        rule_id = rule["rule_id"]
        rule_name = rule["rule"]
        if not isinstance(rule_id, str) or not isinstance(rule_name, str):
            raise _error(
                filename,
                f"rules[{index}]",
                {"rule_id": rule_id, "rule": rule_name},
                "string rule_id and rule fields",
                "restore the canonical pair",
            )
        for field in ("message", "description", "category", "severity", "action"):
            _string(rule[field], filename, f"rules[{index}].{field}")
        for field in ("patterns", "keywords"):
            _string_list(rule[field], filename, f"rules[{index}].{field}")
        _boolean(rule["enabled"], filename, f"rules[{index}].enabled")
        _mapping(rule["metadata"], filename, f"rules[{index}].metadata")
        enum_contracts = {
            "category": {
                "safety_clinical",
                "safety_legal",
                "safety_ethical",
                "content",
                "behavior",
                "compliance",
                "tone",
                "custom",
            },
            "severity": {"info", "warning", "error", "critical"},
            "action": {"log", "warn", "flag", "block", "handoff"},
        }
        for field, allowed in enum_contracts.items():
            if rule[field] not in allowed:
                raise _error(
                    filename,
                    f"rules[{index}].{field}",
                    rule[field],
                    f"one of {sorted(allowed)!r}",
                    "replace it with a supported policy value",
                )
        if rule_id in pairs:
            raise _error(
                filename,
                f"rules[{index}].rule_id",
                _canonical_identifier_or_redacted(rule_id),
                "unique rule ids",
                "remove the duplicate",
            )
        pairs[rule_id] = rule_name
    if pairs != dict(POLICY_RULE_PAIRS):
        missing_pairs = sorted(
            (rule_id, rule_name)
            for rule_id, rule_name in POLICY_RULE_PAIRS.items()
            if rule_id not in pairs
        )
        mismatched_pairs = sorted(
            (rule_id, expected_rule, pairs[rule_id])
            for rule_id, expected_rule in POLICY_RULE_PAIRS.items()
            if rule_id in pairs and pairs[rule_id] != expected_rule
        )
        extra_ids = sorted(set(pairs) - set(POLICY_RULE_PAIRS))
        canonical_extra_ids = sorted(set(extra_ids) & PROTOCOL_IDENTIFIERS)
        canonical_extra_rules = sorted(
            {
                pairs[rule_id]
                for rule_id in extra_ids
                if pairs[rule_id] in PROTOCOL_IDENTIFIERS
            }
        )
        redacted_extra_count = sum(
            rule_id not in PROTOCOL_IDENTIFIERS
            or pairs[rule_id] not in PROTOCOL_IDENTIFIERS
            for rule_id in extra_ids
        )
        diagnostics: list[str] = []
        if missing_pairs:
            diagnostics.append(f"missing canonical pairs={missing_pairs!r}")
        if mismatched_pairs:
            rendered_mismatches = []
            for rule_id, expected_rule, actual_rule in mismatched_pairs:
                actual = (
                    repr(actual_rule)
                    if actual_rule in PROTOCOL_IDENTIFIERS
                    else "<redacted noncanonical rule>"
                )
                rendered_mismatches.append(
                    f"rule_id={rule_id!r} expects rule={expected_rule!r}; "
                    f"received {actual}"
                )
            diagnostics.append(f"mismatched canonical pairs={rendered_mismatches!r}")
        if canonical_extra_ids:
            diagnostics.append(f"extra canonical rule_ids={canonical_extra_ids!r}")
        if canonical_extra_rules:
            diagnostics.append(f"extra canonical rules={canonical_extra_rules!r}")
        if redacted_extra_count:
            diagnostics.append(
                f"extra noncanonical pairs=<redacted count={redacted_extra_count}>"
            )
        raise _error(
            filename,
            "rules",
            {
                "missing": len(missing_pairs),
                "mismatched": len(mismatched_pairs),
                "extra": len(extra_ids),
            },
            "the exact canonical (rule_id, rule) pairs; " + "; ".join(diagnostics),
            "restore each canonical pair without cross-pairing namespaces",
        )
    imperative_literals = _mapping(
        document["imperative_literals"], filename, "imperative_literals"
    )
    _keys(
        imperative_literals,
        filename,
        "imperative_literals",
        {"policy.global_rules", "policy.models"},
    )
    global_rule_literals = _mapping(
        imperative_literals["policy.global_rules"],
        filename,
        "imperative_literals.policy.global_rules",
    )
    _keys(
        global_rule_literals,
        filename,
        "imperative_literals.policy.global_rules",
        set(),
    )
    policy_models = _mapping(
        imperative_literals["policy.models"],
        filename,
        "imperative_literals.policy.models",
    )
    constraint_method = "PolicyConstraints.to_prompt_constraints"
    _keys(
        policy_models,
        filename,
        "imperative_literals.policy.models",
        {constraint_method},
    )
    constraint_key = f"imperative_literals.policy.models.{constraint_method}"
    constraint_prefixes = _string_list(
        policy_models[constraint_method], filename, constraint_key
    )
    if len(constraint_prefixes) != 1:
        raise _error(
            filename,
            constraint_key,
            {"prefix_count": len(constraint_prefixes)},
            "exactly one string constraint prefix",
            "restore the single localized constraint prefix",
        )


def _walk_strings(value: Any, key: str = "$") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield key, value
    elif isinstance(value, Mapping):
        for child_key, child in value.items():
            yield from _walk_strings(
                child, f"{key}.{child_key}" if key != "$" else str(child_key)
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_strings(child, f"{key}[{index}]")


def _validate_prompt(document: Mapping[str, Any]) -> None:
    filename = "prompt.toml"
    _redacted_exact_keys(
        document,
        filename,
        "$",
        {
            "schema_version",
            "protocol",
            "normal_system_prompt",
            "normal_system_prompt_en",
            "funnel_instructions",
            "legacy_funnel_instructions",
            "funnel_instructions_en",
            "legacy_funnel_instructions_en",
            "handoff_system_prompt",
            "handoff_reason_instructions",
            "safety_system_prompt",
            "safety_category_instructions",
            "chars_per_token",
            "lexicon",
            "imperative_literals",
        },
    )
    _schema_version(document, filename)
    protocol = _redacted_mapping(document["protocol"], filename, "protocol")
    _redacted_exact_keys(protocol, filename, "protocol", {"lookup_keys"})
    _exact_values(document, filename, "protocol.lookup_keys", PROMPT_LOOKUP_KEYS)
    stages = {item.value for item in FunnelStage}
    legacy = {"awareness", "consideration", "decision", "retention"}
    for key in ("funnel_instructions", "funnel_instructions_en"):
        table = _redacted_mapping(document[key], filename, key)
        _redacted_exact_keys(table, filename, key, stages)
        for name, value in table.items():
            _string(value, filename, f"{key}.{name}")
    for key in ("legacy_funnel_instructions", "legacy_funnel_instructions_en"):
        table = _redacted_mapping(document[key], filename, key)
        _redacted_exact_keys(table, filename, key, legacy)
        for name, value in table.items():
            _string(value, filename, f"{key}.{name}")
    handoff = _redacted_mapping(
        document["handoff_reason_instructions"], filename, "handoff_reason_instructions"
    )
    safety = _redacted_mapping(
        document["safety_category_instructions"],
        filename,
        "safety_category_instructions",
    )
    _redacted_exact_keys(
        handoff,
        filename,
        "handoff_reason_instructions",
        {
            "explicit_request",
            "safety_concern",
            "max_turns_exceeded",
            "low_confidence",
            "complex_query",
            "escalation",
        },
    )
    _redacted_exact_keys(
        safety,
        filename,
        "safety_category_instructions",
        {"clinical_risk", "legal_risk", "conduct_risk", "emergency"},
    )
    lexicon = _redacted_mapping(document["lexicon"], filename, "lexicon")
    _redacted_exact_keys(lexicon, filename, "lexicon", _PROMPT_LEXICON_KEYS)
    for key in _PROMPT_LEXICON_KEYS:
        _string(lexicon[key], filename, f"lexicon.{key}")
    for key in (
        "normal_system_prompt",
        "normal_system_prompt_en",
        "handoff_system_prompt",
        "safety_system_prompt",
    ):
        _string(document[key], filename, key)
    for table_key, table in (
        ("handoff_reason_instructions", handoff),
        ("safety_category_instructions", safety),
    ):
        for name, value in table.items():
            _string(value, filename, f"{table_key}.{name}")
    _integer_at_least(document["chars_per_token"], filename, "chars_per_token", 1)
    imperative_literals = _redacted_mapping(
        document["imperative_literals"], filename, "imperative_literals"
    )
    _redacted_exact_keys(
        imperative_literals,
        filename,
        "imperative_literals",
        _PROMPT_IMPERATIVE_SCHEMA,
    )
    for module_name, entry_schema in _PROMPT_IMPERATIVE_SCHEMA.items():
        module_key = f"imperative_literals.{module_name}"
        module = _redacted_mapping(
            imperative_literals[module_name], filename, module_key
        )
        _redacted_exact_keys(module, filename, module_key, entry_schema)
        for entry_name, expected_length in entry_schema.items():
            _exact_string_array(
                module[entry_name],
                filename,
                f"{module_key}.{entry_name}",
                expected_length,
            )
    for key, value in _walk_strings(document):
        placeholders = set(_PLACEHOLDER_RE.findall(value))
        residual = _PLACEHOLDER_RE.sub("", value)
        if "{{" in residual or "}}" in residual:
            raise _error(
                filename,
                key,
                value,
                "well-formed {{name}} placeholders",
                "repair the placeholder syntax",
            )
        allowed = _PROMPT_PLACEHOLDERS_BY_PATH.get(key, frozenset())
        misplaced = placeholders - allowed
        if misplaced:
            raise _error(
                filename,
                key,
                {"placeholder_count": len(placeholders)},
                f"placeholders from {sorted(allowed)!r} for this field",
                "remove placeholders that are not rendered for this field",
            )


def _validate_toml_domain(value: Any, filename: str, key: str = "$") -> None:
    """Reject values that the immutable public preset domain cannot represent."""

    if isinstance(value, Mapping):
        for child_key, child in value.items():
            if not isinstance(child_key, str):
                raise _error(
                    filename,
                    key,
                    child_key,
                    "string TOML table keys",
                    "replace the table key with text",
                )
            dotted = child_key if key == "$" else f"{key}.{child_key}"
            _validate_toml_domain(child, filename, dotted)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_toml_domain(child, filename, f"{key}[{index}]")
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise _error(
            filename,
            key,
            value,
            "a finite TOML number",
            "replace nan or infinity with a finite number",
        )
    if isinstance(value, (str, int, float, bool)):
        return
    raise _error(
        filename,
        key,
        value,
        "a supported TOML scalar (string, integer, finite float, or boolean)",
        "replace dates and times with an explicit string representation",
    )


def _validate(documents: Mapping[str, Mapping[str, Any]]) -> None:
    for filename, document in documents.items():
        _validate_toml_domain(document, filename)
    _validate_srm(documents["srm.toml"])
    _validate_flow(documents["flow.toml"])
    _validate_policy(documents["policy.toml"])
    _validate_prompt(documents["prompt.toml"])


def _build_preset(source: _Source) -> Preset:
    documents = _read_source(source)
    _validate(documents)
    frozen = {filename: freeze(document) for filename, document in documents.items()}
    return Preset(
        name=source.name,
        source_id=source.source_id,
        srm=frozen["srm.toml"],  # type: ignore[arg-type]
        flow=frozen["flow.toml"],  # type: ignore[arg-type]
        policy=frozen["policy.toml"],  # type: ignore[arg-type]
        prompt=frozen["prompt.toml"],  # type: ignore[arg-type]
    )


def use_preset(name_or_path: str | os.PathLike[str]) -> Preset:
    """Validate and atomically select one bundled name or trusted directory.

    String inputs are bundled names only.  A :class:`~os.PathLike` is an
    explicit trusted operator input and is canonicalized with ``realpath``.
    Selection is configure-once; changing sources requires a process restart.
    """

    source = _resolve_source(name_or_path)
    global _selected
    with _selection_lock:
        if _selected is not None:
            if _selected.source_id == source.source_id:
                return _selected
            raise PresetError(
                f"preset lifecycle conflict: selected={_selected.source_id!r}, "
                f"requested={source.source_id!r}; restart the process to select a "
                "different preset"
            )
        candidate = _build_preset(source)
        _selected = candidate
        return candidate


def get_preset() -> Preset:
    """Return the selected preset, defaulting atomically to ``pt_br_clinic``."""

    selected = _selected
    if selected is not None:
        return selected
    return use_preset("pt_br_clinic")


__all__ = ["get_preset", "use_preset"]
