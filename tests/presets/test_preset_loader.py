from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import os
import re
import runpy
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = ROOT / "packages" / "neurostack-core"
BUNDLED = PACKAGE_ROOT / "neurostack_core" / "presets" / "pt_br_clinic"
PARITY_SCRIPT = ROOT / "scripts" / "preset-parity.py"

_spec = importlib.util.spec_from_file_location("preset_parity_for_tests", PARITY_SCRIPT)
assert _spec is not None and _spec.loader is not None
_parity = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parity)

_LEGACY_UNICODE_ESCAPES = {
    character: rf"\u{ord(character):04x}" for character in "àáãçéêíóôõú"
}

PROMPT_IMPERATIVE_CONSUMERS = (
    PACKAGE_ROOT / "neurostack_core" / "prompt" / "renderer.py",
    PACKAGE_ROOT / "neurostack_core" / "prompt" / "templates" / "base.py",
    PACKAGE_ROOT / "neurostack_core" / "prompt" / "templates" / "normal.py",
    PACKAGE_ROOT / "neurostack_core" / "prompt" / "templates" / "handoff.py",
    PACKAGE_ROOT / "neurostack_core" / "prompt" / "templates" / "safety.py",
)
FLOW_IMPERATIVE_CONSUMERS = tuple(
    PACKAGE_ROOT / "neurostack_core" / "flow" / "handlers" / f"{name}.py"
    for name in ("handoff", "safety", "normal", "rag")
)
SRM_IMPERATIVE_CONSUMERS = tuple(
    PACKAGE_ROOT / "neurostack_core" / "srm" / f"{name}.py"
    for name in (
        "engine",
        "funnel_detector",
        "handoff_decider",
        "intent_classifier",
        "models",
        "rag_decider",
        "safety_detector",
    )
)


def _all_srm_regex_patterns(document: dict[str, Any]) -> list[str]:
    patterns = [
        pattern
        for section in ("intents", "intents_en")
        for definition in document[section].values()
        for pattern in definition["patterns"]
    ]
    patterns.extend(document["safety_guards"])
    patterns.extend(
        pattern
        for definition in document["safety_patterns"].values()
        for pattern in definition["patterns"]
    )
    patterns.extend(
        pattern
        for category in document["rag"]["patterns"].values()
        for pattern in category
    )
    patterns.extend(document["handoff"]["explicit_patterns"])
    patterns.extend(document["handoff"]["regexes"].values())
    patterns.extend(document["engine"]["regexes"].values())
    return patterns


def _affected_unicode_regex_patterns(document: dict[str, Any]) -> list[str]:
    patterns = [
        pattern
        for definition in document["safety_patterns"].values()
        for pattern in definition["patterns"]
    ]
    patterns.extend(
        pattern
        for category in document["rag"]["patterns"].values()
        for pattern in category
    )
    patterns.extend(document["handoff"]["explicit_patterns"])
    patterns.extend(document["handoff"]["regexes"].values())
    return [
        pattern
        for pattern in patterns
        if any(character in pattern for character in _LEGACY_UNICODE_ESCAPES)
    ]


def _legacy_escape_unicode(pattern: str) -> str:
    return "".join(
        _LEGACY_UNICODE_ESCAPES.get(character, character) for character in pattern
    )


def _regex_opcode_dump(pattern: str) -> str:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        re.compile(pattern, re.IGNORECASE | re.UNICODE | re.DEBUG)
    return output.getvalue()


def _assignment_parts(
    node: ast.AST,
) -> tuple[str, ast.expr] | None:
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    ):
        return node.targets[0].id, node.value
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id, node.value
    return None


def _string_subscript(node: ast.AST, variable: str) -> str | None:
    if not (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == variable
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        return None
    return node.slice.value


def _derive_prompt_imperative_schema() -> dict[str, dict[str, int]]:
    derived: dict[str, dict[str, int]] = {}
    for path in PROMPT_IMPERATIVE_CONSUMERS:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assignments = [
            parts
            for node in tree.body
            if (parts := _assignment_parts(node)) is not None
        ]
        containers: dict[str, str] = {}
        for target, value in assignments:
            for child in ast.walk(value):
                module_name = _string_subscript(child, "_IMPERATIVE_LITERALS")
                if module_name is not None:
                    containers[target] = module_name
                    break

        residual_views: dict[str, str] = {}
        for target, value in assignments:
            if not target.endswith("_RESIDUAL_LITERALS"):
                continue
            names = {
                child.id for child in ast.walk(value) if isinstance(child, ast.Name)
            }
            sources = sorted(names & containers.keys())
            assert len(sources) == 1, (path, target, sources)
            residual_views[target] = containers[sources[0]]

        sequences: dict[str, tuple[str, str]] = {}
        for target, value in assignments:
            for child in ast.walk(value):
                if not isinstance(child, ast.Subscript) or not isinstance(
                    child.value, ast.Name
                ):
                    continue
                module_name = containers.get(child.value.id)
                if module_name is None or not isinstance(child.slice, ast.Constant):
                    continue
                entry_name = child.slice.value
                if isinstance(entry_name, str):
                    sequences[target] = (module_name, entry_name)
                    derived.setdefault(module_name, {})[entry_name] = 0
                    break

        for child in ast.walk(tree):
            if not isinstance(child, ast.Subscript) or not isinstance(
                child.value, ast.Name
            ):
                continue
            variable = child.value.id
            if variable in sequences and isinstance(child.slice, ast.Constant):
                index = child.slice.value
                if isinstance(index, int) and index >= 0:
                    module_name, entry_name = sequences[variable]
                    derived[module_name][entry_name] = max(
                        derived[module_name][entry_name], index + 1
                    )
            if variable in residual_views and isinstance(child.slice, ast.Constant):
                entry_name = child.slice.value
                if isinstance(entry_name, str):
                    derived.setdefault(residual_views[variable], {})[entry_name] = 1
        for child in ast.walk(tree):
            if not (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "list"
                and len(child.args) == 1
                and isinstance(child.args[0], ast.Name)
                and child.args[0].id in sequences
            ):
                continue
            module_name, entry_name = sequences[child.args[0].id]
            derived[module_name][entry_name] = -1
    return {
        module: dict(sorted(entries.items()))
        for module, entries in sorted(derived.items())
    }


def _derive_flow_imperative_schema() -> dict[str, dict[str, int]]:
    derived: dict[str, dict[str, int]] = {}
    for path in FLOW_IMPERATIVE_CONSUMERS:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        sequence: tuple[str, str] | None = None
        for node in tree.body:
            parts = _assignment_parts(node)
            if parts is None or parts[0] != "_PROMPT_ADDITIONS":
                continue
            for child in ast.walk(parts[1]):
                if not (
                    isinstance(child, ast.Subscript)
                    and isinstance(child.slice, ast.Constant)
                    and isinstance(child.slice.value, str)
                    and isinstance(child.value, ast.Subscript)
                    and isinstance(child.value.slice, ast.Constant)
                    and isinstance(child.value.slice.value, str)
                    and isinstance(child.value.value, ast.Name)
                    and child.value.value.id == "_IMPERATIVE_LITERALS"
                ):
                    continue
                sequence = (child.value.slice.value, child.slice.value)
                break
        assert sequence is not None, path
        module_name, entry_name = sequence
        consumed_length = 0
        for child in ast.walk(tree):
            if (
                isinstance(child, ast.Subscript)
                and isinstance(child.value, ast.Name)
                and child.value.id == "_PROMPT_ADDITIONS"
                and isinstance(child.slice, ast.Constant)
                and isinstance(child.slice.value, int)
            ):
                consumed_length = max(consumed_length, child.slice.value + 1)
            if (
                isinstance(child, ast.Starred)
                and isinstance(child.value, ast.Name)
                and child.value.id == "_PROMPT_ADDITIONS"
            ) or (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "list"
                and len(child.args) == 1
                and isinstance(child.args[0], ast.Name)
                and child.args[0].id == "_PROMPT_ADDITIONS"
            ):
                consumed_length = -1
        derived.setdefault(module_name, {})[entry_name] = consumed_length
    return {
        module: dict(sorted(entries.items()))
        for module, entries in sorted(derived.items())
    }


def _derive_srm_imperative_usage() -> tuple[
    set[str],
    dict[str, dict[str, int]],
    dict[str, dict[str, set[str]]],
    set[tuple[str, str]],
]:
    modules = {f"srm.{path.stem}" for path in SRM_IMPERATIVE_CONSUMERS}
    arrays: dict[str, dict[str, int]] = {}
    mappings: dict[str, dict[str, set[str]]] = {}
    whole_mappings: set[tuple[str, str]] = set()
    for path in SRM_IMPERATIVE_CONSUMERS:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assignments = [
            parts
            for node in tree.body
            if (parts := _assignment_parts(node)) is not None
        ]
        containers: dict[str, str] = {}
        for target, value in assignments:
            for child in ast.walk(value):
                module_name = _string_subscript(child, "_IMPERATIVE_LITERALS")
                if module_name is not None:
                    containers[target] = module_name
                    break

        entries: dict[str, tuple[str, str]] = {}
        for target, value in assignments:
            for child in ast.walk(value):
                if (
                    isinstance(child, ast.Subscript)
                    and isinstance(child.value, ast.Name)
                    and child.value.id in containers
                    and isinstance(child.slice, ast.Constant)
                    and isinstance(child.slice.value, str)
                ):
                    entries[target] = (containers[child.value.id], child.slice.value)
                    break
                if (
                    isinstance(child, ast.Subscript)
                    and isinstance(child.slice, ast.Constant)
                    and isinstance(child.slice.value, str)
                    and isinstance(child.value, ast.Subscript)
                    and isinstance(child.value.slice, ast.Constant)
                    and isinstance(child.value.slice.value, str)
                    and isinstance(child.value.value, ast.Name)
                    and child.value.value.id == "_IMPERATIVE_LITERALS"
                ):
                    entries[target] = (child.value.slice.value, child.slice.value)
                    break

        for child in ast.walk(tree):
            if isinstance(child, ast.Subscript) and isinstance(child.value, ast.Name):
                entry_path = entries.get(child.value.id)
                if entry_path is None or not isinstance(child.slice, ast.Constant):
                    continue
                module_name, entry_name = entry_path
                index_or_key = child.slice.value
                if isinstance(index_or_key, int):
                    module = arrays.setdefault(module_name, {})
                    module[entry_name] = max(
                        module.get(entry_name, 0), index_or_key + 1
                    )
                elif isinstance(index_or_key, str):
                    mappings.setdefault(module_name, {}).setdefault(
                        entry_name, set()
                    ).add(index_or_key)
            if not (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in {"items", "keys", "values"}
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id in entries
            ):
                continue
            whole_mappings.add(entries[child.func.value.id])
        for module_name, entry_name in entries.values():
            if (
                entry_name not in arrays.get(module_name, {})
                and entry_name not in mappings.get(module_name, {})
                and (module_name, entry_name) not in whole_mappings
            ):
                arrays.setdefault(module_name, {})[entry_name] = -1
    return modules, arrays, mappings, whole_mappings


def _declared_imperative_schema(name: str) -> dict[str, dict[str, int]]:
    loader_path = PACKAGE_ROOT / "neurostack_core" / "presets" / "loader.py"
    tree = ast.parse(loader_path.read_text(encoding="utf-8"), filename=str(loader_path))
    for node in tree.body:
        parts = _assignment_parts(node)
        if parts is not None and parts[0] == name:
            value = ast.literal_eval(parts[1])
            assert isinstance(value, dict)
            return value
    raise AssertionError(f"{name} declaration not found")


def _declared_prompt_imperative_schema() -> dict[str, dict[str, int]]:
    return _declared_imperative_schema("_PROMPT_IMPERATIVE_SCHEMA")


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(PACKAGE_ROOT) + (
        os.pathsep + existing if existing else ""
    )
    return environment


def _run(
    code: str, *arguments: object, timeout: int = 20
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code, *(str(argument) for argument in arguments)],
        cwd=ROOT,
        env=_environment(),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _copy_preset(tmp_path: Path, name: str = "preset") -> Path:
    destination = tmp_path / name
    shutil.copytree(BUNDLED, destination)
    return destination


def _rewrite(
    preset: Path, filename: str, mutation: Callable[[dict[str, Any]], None]
) -> None:
    path = preset / filename
    with path.open("rb") as handle:
        document = tomllib.load(handle)
    mutation(document)
    path.write_text(_parity.dump_toml(document), encoding="utf-8")


def _set_path(document: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = document
    for part in path[:-1]:
        current = current[part]
    current[path[-1]] = value


def _assert_rejected(preset: Path, *fragments: str) -> str:
    completed = _run(
        """
import pathlib, sys
from neurostack_core import PresetError, get_preset, use_preset
try:
    use_preset(pathlib.Path(sys.argv[1]))
except PresetError as error:
    print(error)
else:
    raise SystemExit('preset unexpectedly loaded')
assert get_preset().source_id == 'bundled:pt_br_clinic'
""",
        preset,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    for fragment in fragments:
        assert fragment in completed.stdout
    assert "expected" in completed.stdout
    assert "next action" in completed.stdout
    return completed.stdout


def _compare(preset: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(PARITY_SCRIPT),
            "compare",
            "--baseline",
            str(ROOT / "tests" / "fixtures" / "presets" / "pt-br-containers.json"),
            "--preset",
            str(preset),
        ],
        cwd=ROOT,
        env=_environment(),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_package_root_is_lazy_and_protocol_import_is_isolated() -> None:
    completed = _run("""
import sys
import neurostack_core
assert 'neurostack_core.presets' not in sys.modules
import neurostack_core.protocol as protocol
for prefix in (
    'neurostack_core.presets', 'neurostack_core.srm.engine',
    'neurostack_core.prompt', 'neurostack_core.policy',
    'neurostack_core.flow.handlers',
):
    assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules)
assert protocol.FunnelStage.LEAD_NOVO.value == 'lead_novo'
""")
    assert completed.returncode == 0, completed.stderr


def test_public_exports_and_default_first_access() -> None:
    completed = _run("""
from neurostack_core import Preset, PresetError, get_preset, use_preset
preset = get_preset()
assert isinstance(preset, Preset)
assert preset.name == 'pt_br_clinic'
assert preset.source_id == 'bundled:pt_br_clinic'
assert get_preset() is preset
assert use_preset('pt_br_clinic') is preset
assert issubclass(PresetError, RuntimeError)
""")
    assert completed.returncode == 0, completed.stderr


def test_fresh_default_load_keeps_srm_and_policy_imperative_contracts() -> None:
    completed = _run("""
from neurostack_core import get_preset
preset = get_preset()
assert 'srm.engine' in preset.srm['imperative_literals']
assert 'SRMEngine.process' in preset.srm['imperative_literals']['srm.engine']
assert 'policy.models' in preset.policy['imperative_literals']
assert preset.policy['imperative_literals']['policy.models'][
    'PolicyConstraints.to_prompt_constraints'
] == ('NÃO ',)
""")
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "unsafe",
    ["../pt_br_clinic", "pt/br", "pt\\br", "PT_BR", "pt-br", ""],
)
def test_bundled_names_reject_traversal_and_separators(unsafe: str) -> None:
    completed = _run(
        """
import sys
from neurostack_core import PresetError, use_preset
try:
    use_preset(sys.argv[1])
except PresetError as error:
    text = str(error)
    assert '[a-z0-9_]+' in text and 'next action' in text
else:
    raise SystemExit('unsafe bundled name loaded')
""",
        unsafe,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_explicit_path_is_canonicalized_and_same_source_is_idempotent(
    tmp_path: Path,
) -> None:
    preset = _copy_preset(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(preset, target_is_directory=True)
    completed = _run(
        """
import pathlib, sys
from neurostack_core import use_preset
first = use_preset(pathlib.Path(sys.argv[1]))
second = use_preset(pathlib.Path(sys.argv[2]))
assert first is second
assert first.source_id == 'path:' + str(pathlib.Path(sys.argv[1]).resolve())
""",
        preset,
        alias,
    )
    assert completed.returncode == 0, completed.stderr


def test_different_source_rejected_with_lifecycle_details(tmp_path: Path) -> None:
    first = _copy_preset(tmp_path, "first")
    second = _copy_preset(tmp_path, "second")
    completed = _run(
        """
import pathlib, sys
from neurostack_core import PresetError, use_preset
selected = use_preset(pathlib.Path(sys.argv[1]))
try:
    use_preset(pathlib.Path(sys.argv[2]))
except PresetError as error:
    text = str(error)
    assert selected.source_id in text
    assert 'path:' + str(pathlib.Path(sys.argv[2]).resolve()) in text
    assert 'restart the process' in text
else:
    raise SystemExit('hot swap unexpectedly succeeded')
""",
        first,
        second,
    )
    assert completed.returncode == 0, completed.stderr


def test_missing_and_unknown_files_fail_closed(tmp_path: Path) -> None:
    missing = _copy_preset(tmp_path, "missing")
    (missing / "flow.toml").unlink()
    _assert_rejected(missing, "flow.toml", "missing")

    unknown = _copy_preset(tmp_path, "unknown")
    (unknown / "surprise.toml").write_text("value = 1\n", encoding="utf-8")
    _assert_rejected(unknown, "surprise.toml", "fixed files")


def test_file_size_cap_precedes_parsing(tmp_path: Path) -> None:
    preset = _copy_preset(tmp_path)
    with (preset / "flow.toml").open("wb") as handle:
        handle.write(b"x" * (1024 * 1024 + 1))
    _assert_rejected(preset, "flow.toml", "1048576", "1 MiB")


def test_unknown_and_missing_schema_keys_name_the_dotted_contract(
    tmp_path: Path,
) -> None:
    extra = _copy_preset(tmp_path, "extra")
    _rewrite(extra, "srm.toml", lambda document: document.update({"surprise": 1}))
    _assert_rejected(extra, "srm.toml", "surprise", "exact keys")

    missing = _copy_preset(tmp_path, "missing")
    _rewrite(
        missing, "prompt.toml", lambda document: document.pop("normal_system_prompt")
    )
    _assert_rejected(missing, "prompt.toml", "normal_system_prompt", "missing")


def test_invalid_regex_and_placeholder_are_sanitized_and_actionable(
    tmp_path: Path,
) -> None:
    regex = _copy_preset(tmp_path, "regex")
    _rewrite(
        regex,
        "srm.toml",
        lambda document: document["intents"]["agendar"]["patterns"].__setitem__(
            0, "[unterminated"
        ),
    )
    output = _assert_rejected(
        regex, "srm.toml", "intents.agendar.patterns[0]", "regular expression"
    )
    assert "normal_system_prompt" not in output

    placeholder = _copy_preset(tmp_path, "placeholder")
    _rewrite(
        placeholder,
        "prompt.toml",
        lambda document: document.__setitem__(
            "normal_system_prompt", "Hello {{unsupported_secret_value}}"
        ),
    )
    output = _assert_rejected(
        placeholder,
        "prompt.toml",
        "normal_system_prompt",
        "placeholders",
    )
    assert "unsupported_secret_value" not in output
    assert "<mapping keys=1>" in output


@pytest.mark.parametrize(
    "value",
    (
        "Hello {{unterminated",
        "Hello {{agent_name}} then {{unterminated",
        "Hello {{agent_name!r}}",
        "Hello {{agent_name:>10}}",
        "Hello {{agent_name:{channel}}}",
    ),
)
def test_malformed_prompt_placeholders_are_rejected(tmp_path: Path, value: str) -> None:
    preset = _copy_preset(tmp_path)
    _rewrite(
        preset,
        "prompt.toml",
        lambda document: document.__setitem__("normal_system_prompt", value),
    )
    _assert_rejected(
        preset,
        "prompt.toml",
        "normal_system_prompt",
        "well-formed {{name}} placeholders",
    )


@pytest.mark.parametrize(
    "value,expected,rejected_summary",
    (
        (
            "PRIVATE-CLINIC-SENTINEL {{broken",
            "well-formed {{name}} placeholders",
            "<str length=",
        ),
        (
            "PRIVATE-CLINIC-SENTINEL {{PRIVATE_CLINIC_SENTINEL}}",
            "placeholders",
            "<mapping keys=1>",
        ),
    ),
)
def test_prompt_errors_never_expose_private_content(
    tmp_path: Path, value: str, expected: str, rejected_summary: str
) -> None:
    preset = _copy_preset(tmp_path)
    _rewrite(
        preset,
        "prompt.toml",
        lambda document: document.__setitem__("normal_system_prompt", value),
    )
    output = _assert_rejected(
        preset, "prompt.toml", "normal_system_prompt", expected, rejected_summary
    )
    assert value not in output
    assert "PRIVATE-CLINIC-SENTINEL" not in output
    assert "PRIVATE_CLINIC_SENTINEL" not in output


@pytest.mark.parametrize(
    "path,placeholder",
    (
        (("handoff_reason_instructions", "explicit_request"), "channel"),
        (("safety_category_instructions", "emergency"), "safety_level"),
        (("funnel_instructions", "lead_novo"), "handoff_status"),
        (
            (
                "imperative_literals",
                "prompt.renderer",
                "PromptRenderer.render",
                0,
            ),
            "agent_name",
        ),
    ),
)
def test_prompt_placeholders_are_restricted_to_their_rendered_field(
    tmp_path: Path,
    path: tuple[str | int, ...],
    placeholder: str,
) -> None:
    preset = _copy_preset(tmp_path)

    def mutate(document: dict[str, Any]) -> None:
        current: Any = document
        for part in path[:-1]:
            current = current[part]
        current[path[-1]] = f"Private prompt {{{{{placeholder}}}}}"

    _rewrite(preset, "prompt.toml", mutate)
    dotted = "".join(
        f"[{part}]" if isinstance(part, int) else ("." if index else "") + part
        for index, part in enumerate(path)
    )
    output = _assert_rejected(
        preset,
        "prompt.toml",
        dotted,
        "placeholders",
        "for this field",
    )
    assert placeholder not in output
    assert "Private prompt" not in output


@pytest.mark.parametrize(
    "location",
    ("top", "funnel", "handoff", "safety", "lexicon"),
)
def test_prompt_structural_errors_redact_unknown_keys(
    tmp_path: Path, location: str
) -> None:
    preset = _copy_preset(tmp_path)
    private = "PRIVATE-PROMPT-STRUCTURAL-SENTINEL"

    def mutate(document: dict[str, Any]) -> None:
        targets = {
            "top": document,
            "funnel": document["funnel_instructions"],
            "handoff": document["handoff_reason_instructions"],
            "safety": document["safety_category_instructions"],
            "lexicon": document["lexicon"],
        }
        targets[location][private] = private

    _rewrite(preset, "prompt.toml", mutate)
    output = _assert_rejected(
        preset,
        "prompt.toml",
        "extra structural keys=<redacted count=1>",
    )
    assert private not in output


@pytest.mark.parametrize("mode", ("missing", "extra", "wrong_table", "wrong_value"))
def test_prompt_lexicon_requires_exact_named_string_entries_without_disclosure(
    tmp_path: Path, mode: str
) -> None:
    preset = _copy_preset(tmp_path)
    private = "PRIVATE-PROMPT-LEXICON-SENTINEL"

    def mutate(document: dict[str, Any]) -> None:
        lexicon = document["lexicon"]
        if mode == "missing":
            lexicon.pop("free_feminine_pt")
        elif mode == "extra":
            lexicon[private] = private
        elif mode == "wrong_table":
            document["lexicon"] = private
        elif mode == "wrong_value":
            lexicon["free_feminine_pt"] = [private]
        else:
            raise AssertionError(mode)

    _rewrite(preset, "prompt.toml", mutate)
    output = _assert_rejected(preset, "prompt.toml", "lexicon")
    assert private not in output


def test_prompt_imperative_schema_matches_every_consumer_path_and_index() -> None:
    derived = _derive_prompt_imperative_schema()
    declared = _declared_prompt_imperative_schema()
    assert {module: set(entries) for module, entries in derived.items()} == {
        module: set(entries) for module, entries in declared.items()
    }
    for module, entries in derived.items():
        for entry, consumed_length in entries.items():
            if consumed_length == -1:
                assert declared[module][entry] > 0
            else:
                assert declared[module][entry] == consumed_length


@pytest.mark.parametrize(
    "mode,expected_key,expected_contract",
    (
        ("missing_module", "imperative_literals", "missing structural keys"),
        ("extra_module", "imperative_literals", "extra structural keys"),
        (
            "missing_entry",
            "imperative_literals.prompt.renderer",
            "missing structural keys",
        ),
        (
            "extra_entry",
            "imperative_literals.prompt.renderer",
            "extra structural keys",
        ),
        (
            "missing_residual_entry",
            "imperative_literals.prompt.templates.normal.residual",
            "missing structural keys",
        ),
        (
            "extra_residual_entry",
            "imperative_literals.prompt.templates.normal.residual",
            "extra structural keys",
        ),
        (
            "empty",
            "imperative_literals.prompt.renderer.PromptRenderer.render",
            "exactly 1 string items",
        ),
        (
            "short",
            "imperative_literals.prompt.renderer.VariableResolver.from_context_window",
            "exactly 3 string items",
        ),
        (
            "long",
            "imperative_literals.prompt.renderer.PromptRenderer.render",
            "exactly 1 string items",
        ),
        (
            "empty_residual",
            "imperative_literals.prompt.templates.normal.residual.assistant_default",
            "exactly 1 string items",
        ),
        (
            "long_residual",
            "imperative_literals.prompt.templates.normal.residual.assistant_default",
            "exactly 1 string items",
        ),
        (
            "wrong_module_type",
            "imperative_literals.prompt.renderer",
            "a TOML table",
        ),
        (
            "wrong_entry_type",
            "imperative_literals.prompt.renderer.PromptRenderer.render",
            "a TOML array",
        ),
        (
            "wrong_item_type",
            "imperative_literals.prompt.renderer.PromptRenderer.render[0]",
            "a string",
        ),
    ),
)
def test_prompt_imperative_schema_rejects_structural_drift_without_disclosure(
    tmp_path: Path,
    mode: str,
    expected_key: str,
    expected_contract: str,
) -> None:
    preset = _copy_preset(tmp_path)
    private = "PRIVATE-PROMPT-SCHEMA-SENTINEL"

    def mutate(document: dict[str, Any]) -> None:
        imperative = document["imperative_literals"]
        renderer = imperative["prompt.renderer"]
        normal_residual = imperative["prompt.templates.normal.residual"]
        if mode == "missing_module":
            imperative.pop("prompt.renderer")
        elif mode == "extra_module":
            imperative[private] = {}
        elif mode == "missing_entry":
            renderer.pop("PromptRenderer.render")
        elif mode == "extra_entry":
            renderer[private] = [private]
        elif mode == "missing_residual_entry":
            normal_residual.pop("assistant_default")
        elif mode == "extra_residual_entry":
            normal_residual[private] = [private]
        elif mode == "empty":
            renderer["PromptRenderer.render"] = []
        elif mode == "short":
            renderer["VariableResolver.from_context_window"].pop()
        elif mode == "long":
            renderer["PromptRenderer.render"].append(private)
        elif mode == "empty_residual":
            normal_residual["assistant_default"] = []
        elif mode == "long_residual":
            normal_residual["assistant_default"].append(private)
        elif mode == "wrong_module_type":
            imperative["prompt.renderer"] = private
        elif mode == "wrong_entry_type":
            renderer["PromptRenderer.render"] = private
        elif mode == "wrong_item_type":
            renderer["PromptRenderer.render"][0] = 7
        else:
            raise AssertionError(mode)

    _rewrite(preset, "prompt.toml", mutate)
    output = _assert_rejected(
        preset,
        "prompt.toml",
        expected_key,
        expected_contract,
    )
    assert private not in output


def test_flow_imperative_schema_matches_every_consumer_path_and_index() -> None:
    derived = _derive_flow_imperative_schema()
    declared = _declared_imperative_schema("_FLOW_IMPERATIVE_SCHEMA")
    assert {module: set(entries) for module, entries in derived.items()} == {
        module: set(entries) for module, entries in declared.items()
    }
    for module, entries in derived.items():
        for entry, consumed_length in entries.items():
            if consumed_length == -1:
                assert declared[module][entry] > 0
            else:
                assert declared[module][entry] == consumed_length


@pytest.mark.parametrize(
    "mode,expected_key,expected_contract",
    (
        ("missing_module", "imperative_literals", "missing structural keys"),
        ("extra_module", "imperative_literals", "extra structural keys"),
        (
            "missing_method",
            "imperative_literals.flow.handlers.handoff",
            "missing structural keys",
        ),
        (
            "extra_method",
            "imperative_literals.flow.handlers.handoff",
            "extra structural keys",
        ),
        (
            "empty",
            "imperative_literals.flow.handlers.handoff.HandoffModeHandler.get_mode_prompt_additions",
            "exactly 4 string items",
        ),
        (
            "short",
            "imperative_literals.flow.handlers.handoff.HandoffModeHandler.get_mode_prompt_additions",
            "exactly 4 string items",
        ),
        (
            "long",
            "imperative_literals.flow.handlers.handoff.HandoffModeHandler.get_mode_prompt_additions",
            "exactly 4 string items",
        ),
        (
            "wrong_module_type",
            "imperative_literals.flow.handlers.handoff",
            "a TOML table",
        ),
        (
            "wrong_method_type",
            "imperative_literals.flow.handlers.handoff.HandoffModeHandler.get_mode_prompt_additions",
            "a TOML array",
        ),
        (
            "wrong_item_type",
            "imperative_literals.flow.handlers.handoff.HandoffModeHandler.get_mode_prompt_additions[0]",
            "a string",
        ),
    ),
)
def test_flow_imperative_schema_rejects_structural_drift_without_disclosure(
    tmp_path: Path,
    mode: str,
    expected_key: str,
    expected_contract: str,
) -> None:
    preset = _copy_preset(tmp_path)
    private = "PRIVATE-FLOW-SCHEMA-SENTINEL"
    method_name = "HandoffModeHandler.get_mode_prompt_additions"

    def mutate(document: dict[str, Any]) -> None:
        imperative = document["imperative_literals"]
        handoff = imperative["flow.handlers.handoff"]
        if mode == "missing_module":
            imperative.pop("flow.handlers.handoff")
        elif mode == "extra_module":
            imperative[private] = {}
        elif mode == "missing_method":
            handoff.pop(method_name)
        elif mode == "extra_method":
            handoff[private] = [private]
        elif mode == "empty":
            handoff[method_name] = []
        elif mode == "short":
            handoff[method_name].pop()
        elif mode == "long":
            handoff[method_name].append(private)
        elif mode == "wrong_module_type":
            imperative["flow.handlers.handoff"] = private
        elif mode == "wrong_method_type":
            handoff[method_name] = private
        elif mode == "wrong_item_type":
            handoff[method_name][0] = 7
        else:
            raise AssertionError(mode)

    _rewrite(preset, "flow.toml", mutate)
    output = _assert_rejected(
        preset,
        "flow.toml",
        expected_key,
        expected_contract,
    )
    assert private not in output


def test_srm_imperative_schema_matches_every_consumer_path_key_and_index() -> None:
    from neurostack_core.presets import loader as preset_loader
    from neurostack_core.protocol import ACTION_IDS

    modules, consumed_arrays, consumed_mappings, whole_mappings = (
        _derive_srm_imperative_usage()
    )
    array_schema = preset_loader._SRM_IMPERATIVE_ARRAY_SCHEMA
    mapping_schema = preset_loader._SRM_IMPERATIVE_MAPPING_SCHEMA
    assert set(array_schema) == modules
    assert set(mapping_schema) <= modules
    for module in modules:
        declared_entries = set(array_schema[module]) | set(
            mapping_schema.get(module, {})
        )
        consumed_entries = (
            set(consumed_arrays.get(module, {}))
            | set(consumed_mappings.get(module, {}))
            | {entry for owner, entry in whole_mappings if owner == module}
        )
        assert declared_entries == consumed_entries
        for entry, consumed_length in consumed_arrays.get(module, {}).items():
            if consumed_length == -1:
                assert array_schema[module][entry] > 0
            else:
                assert array_schema[module][entry] == consumed_length
    assert whole_mappings == {("srm.engine", "SRMEngine._get_action_details")}
    assert mapping_schema["srm.engine"]["SRMEngine._get_action_details"] == ACTION_IDS
    for module, entries in mapping_schema.items():
        for entry, expected_keys in entries.items():
            if (module, entry) in whole_mappings:
                continue
            assert expected_keys == consumed_mappings[module][entry]


@pytest.mark.parametrize(
    "mode,expected_key,expected_contract",
    (
        ("missing_module", "imperative_literals", "missing structural keys"),
        ("extra_module", "imperative_literals", "extra structural keys"),
        (
            "missing_method",
            "imperative_literals.srm.engine",
            "missing structural keys",
        ),
        (
            "extra_method",
            "imperative_literals.srm.engine",
            "extra structural keys",
        ),
        (
            "empty_array",
            "imperative_literals.srm.engine.SRMEngine.process",
            "exactly 3 string items",
        ),
        (
            "short_array",
            "imperative_literals.srm.rag_decider.RagDecider._generate_query",
            "exactly 8 string items",
        ),
        (
            "long_array",
            "imperative_literals.srm.engine.SRMEngine.process",
            "exactly 3 string items",
        ),
        (
            "wrong_module_type",
            "imperative_literals.srm.engine",
            "a TOML table",
        ),
        (
            "wrong_array_type",
            "imperative_literals.srm.engine.SRMEngine.process",
            "a TOML array",
        ),
        (
            "wrong_array_item",
            "imperative_literals.srm.engine.SRMEngine.process[0]",
            "a string",
        ),
        (
            "empty_mapping",
            "imperative_literals.srm.engine.SRMEngine._get_action_details",
            "missing structural keys",
        ),
        (
            "missing_action",
            "imperative_literals.srm.engine.SRMEngine._get_action_details",
            "missing structural keys=['respond']",
        ),
        (
            "extra_action",
            "imperative_literals.srm.engine.SRMEngine._get_action_details",
            "extra structural keys=<redacted count=1>",
        ),
        (
            "wrong_mapping_type",
            "imperative_literals.srm.engine.SRMEngine._get_action_details",
            "a TOML table",
        ),
        (
            "wrong_mapping_item",
            "imperative_literals.srm.engine.SRMEngine._get_action_details.respond",
            "a string",
        ),
        (
            "missing_output_key",
            "imperative_literals.srm.handoff_decider.HandoffDecider.output_values",
            "missing structural keys=['routine']",
        ),
    ),
)
def test_srm_imperative_schema_rejects_structural_drift_without_disclosure(
    tmp_path: Path,
    mode: str,
    expected_key: str,
    expected_contract: str,
) -> None:
    preset = _copy_preset(tmp_path)
    private = "PRIVATE-SRM-SCHEMA-SENTINEL"

    def mutate(document: dict[str, Any]) -> None:
        imperative = document["imperative_literals"]
        engine = imperative["srm.engine"]
        process = engine["SRMEngine.process"]
        actions = engine["SRMEngine._get_action_details"]
        handoff_outputs = imperative["srm.handoff_decider"][
            "HandoffDecider.output_values"
        ]
        if mode == "missing_module":
            imperative.pop("srm.engine")
        elif mode == "extra_module":
            imperative[private] = {}
        elif mode == "missing_method":
            engine.pop("SRMEngine.process")
        elif mode == "extra_method":
            engine[private] = [private]
        elif mode == "empty_array":
            engine["SRMEngine.process"] = []
        elif mode == "short_array":
            imperative["srm.rag_decider"]["RagDecider._generate_query"].pop()
        elif mode == "long_array":
            process.append(private)
        elif mode == "wrong_module_type":
            imperative["srm.engine"] = private
        elif mode == "wrong_array_type":
            engine["SRMEngine.process"] = private
        elif mode == "wrong_array_item":
            process[0] = 7
        elif mode == "empty_mapping":
            engine["SRMEngine._get_action_details"] = {}
        elif mode == "missing_action":
            actions.pop("respond")
        elif mode == "extra_action":
            actions[private] = private
        elif mode == "wrong_mapping_type":
            engine["SRMEngine._get_action_details"] = private
        elif mode == "wrong_mapping_item":
            actions["respond"] = 7
        elif mode == "missing_output_key":
            handoff_outputs.pop("routine")
        else:
            raise AssertionError(mode)

    _rewrite(preset, "srm.toml", mutate)
    output = _assert_rejected(
        preset,
        "srm.toml",
        expected_key,
        expected_contract,
    )
    assert private not in output


@pytest.mark.parametrize(
    "response_key,template",
    (
        ("waiting", "{wait_time:NOT_A_FORMAT}"),
        ("waiting", "{wait_time:{agent_name}}"),
        ("connected", "{agent_name!z}"),
    ),
)
def test_handoff_responses_reject_unsafe_placeholder_syntax_before_publication(
    tmp_path: Path,
    response_key: str,
    template: str,
) -> None:
    preset = _copy_preset(tmp_path)
    _rewrite(
        preset,
        "flow.toml",
        lambda document: document["handoff_responses"].__setitem__(
            response_key, template
        ),
    )
    output = _assert_rejected(
        preset,
        "flow.toml",
        f"handoff_responses.{response_key}",
        "plain placeholders without conversions, format specs, or nested fields",
    )
    assert template not in output
    assert "NOT_A_FORMAT" not in output


@pytest.mark.parametrize(
    "mode,mutated_key",
    (("missing", "standby"), ("unknown", "stage_que_nao_existe")),
)
def test_prompt_funnel_instructions_require_exact_canonical_stage_keys(
    tmp_path: Path, mode: str, mutated_key: str
) -> None:
    preset = _copy_preset(tmp_path)

    def mutate(document: dict[str, Any]) -> None:
        instructions = document["funnel_instructions"]
        if mode == "missing":
            instructions.pop(mutated_key)
        else:
            instructions[mutated_key] = instructions["standby"]

    _rewrite(preset, "prompt.toml", mutate)
    output = _assert_rejected(
        preset,
        "prompt.toml",
        "funnel_instructions",
        "structural keys",
    )
    if mode == "missing":
        assert mutated_key in output
    else:
        assert mutated_key not in output


@pytest.mark.parametrize(
    "filename,path,value,expected",
    (
        ("srm.toml", ("intents", "saudacao", "keywords", 0), 7, "a string"),
        ("srm.toml", ("intents", "saudacao", "weight"), True, "a number"),
        (
            "flow.toml",
            ("safety_responses", "default"),
            False,
            "a string",
        ),
        (
            "flow.toml",
            ("state_machine", "mode_priority", "normal"),
            "not-an-integer",
            "integer",
        ),
        ("policy.toml", ("rules", 0, "enabled"), "not-a-bool", "a boolean"),
        ("policy.toml", ("global_policy",), "not-a-table", "a TOML table"),
        ("prompt.toml", ("normal_system_prompt",), 42, "a string"),
        (
            "prompt.toml",
            ("chars_per_token",),
            "not-a-number",
            "integer",
        ),
    ),
)
def test_consumed_leaf_types_fail_closed_before_publication(
    tmp_path: Path,
    filename: str,
    path: tuple[str | int, ...],
    value: Any,
    expected: str,
) -> None:
    preset = _copy_preset(tmp_path)

    def mutate(document: dict[str, Any]) -> None:
        current: Any = document
        for part in path[:-1]:
            current = current[part]
        current[path[-1]] = value

    _rewrite(preset, filename, mutate)
    dotted = "".join(
        f"[{part}]" if isinstance(part, int) else ("." if index else "") + part
        for index, part in enumerate(path)
    )
    _assert_rejected(preset, filename, dotted, expected)


NAMESPACES = (
    ("srm.toml", ("protocol", "intent_categories")),
    ("srm.toml", ("protocol", "funnel_stages")),
    ("srm.toml", ("protocol", "safety_categories")),
    ("srm.toml", ("protocol", "sentiment_labels")),
    ("srm.toml", ("protocol", "flow_modes")),
    ("srm.toml", ("protocol", "handoff_reasons")),
    ("srm.toml", ("protocol", "official_intents")),
    ("srm.toml", ("protocol", "actions")),
    ("srm.toml", ("protocol", "engine_regex_keys")),
    ("flow.toml", ("protocol", "handoff_response_keys")),
    ("flow.toml", ("protocol", "safety_response_keys")),
    ("policy.toml", ("protocol", "rule_ids")),
    ("policy.toml", ("protocol", "rule_names")),
    ("prompt.toml", ("protocol", "lookup_keys")),
)


@pytest.mark.parametrize("filename,path", NAMESPACES)
@pytest.mark.parametrize("mode", ("missing", "extra"))
def test_every_protocol_namespace_rejects_missing_and_extra_values(
    tmp_path: Path, filename: str, path: tuple[str, str], mode: str
) -> None:
    preset = _copy_preset(tmp_path)

    def mutate(document: dict[str, Any]) -> None:
        values = document[path[0]][path[1]]
        if mode == "missing":
            values.pop()
        else:
            values.append("unlisted_protocol_value")

    _rewrite(preset, filename, mutate)
    _assert_rejected(preset, filename, ".".join(path), mode)


def test_duplicate_noncanonical_protocol_identifier_is_redacted(tmp_path: Path) -> None:
    preset = _copy_preset(tmp_path)
    private_identifier = "PRIVATE-NAMESPACE-SENTINEL"

    def duplicate_private_identifier(document: dict[str, Any]) -> None:
        document["protocol"]["rule_ids"][0:2] = [private_identifier] * 2

    _rewrite(preset, "policy.toml", duplicate_private_identifier)
    output = _assert_rejected(
        preset,
        "policy.toml",
        "protocol.rule_ids[1]",
        "<redacted noncanonical identifier>",
        "unique identifiers",
    )
    assert private_identifier not in output


def test_duplicate_canonical_protocol_identifier_remains_actionable(
    tmp_path: Path,
) -> None:
    preset = _copy_preset(tmp_path)
    canonical_identifier = "C_MEDICAL_NO_DIAGNOSIS"

    def duplicate_canonical_identifier(document: dict[str, Any]) -> None:
        document["protocol"]["rule_ids"][0:2] = [canonical_identifier] * 2

    _rewrite(preset, "policy.toml", duplicate_canonical_identifier)
    _assert_rejected(
        preset,
        "policy.toml",
        "protocol.rule_ids[1]",
        canonical_identifier,
        "unique identifiers",
    )


def test_exact_namespace_reports_long_missing_identifier_and_redacts_replacement(
    tmp_path: Path,
) -> None:
    preset = _copy_preset(tmp_path)
    missing = "POL_SAFETY_001_EMERGENCY_POSTCARE_OUT_OF_HOURS"
    replacement = "PRIVATE-CLINIC-SENTINEL"

    def replace_rule_id(document: dict[str, Any]) -> None:
        rule_ids = document["protocol"]["rule_ids"]
        rule_ids[rule_ids.index(missing)] = replacement

    _rewrite(preset, "policy.toml", replace_rule_id)
    output = _assert_rejected(
        preset,
        "policy.toml",
        "protocol.rule_ids",
        f"missing canonical identifiers=['{missing}']",
        "extra noncanonical identifiers=<redacted count=1>",
    )
    assert replacement not in output


def test_exact_namespace_reports_safe_cross_namespace_extra_identifier(
    tmp_path: Path,
) -> None:
    preset = _copy_preset(tmp_path)

    def add_canonical_extra(document: dict[str, Any]) -> None:
        document["protocol"]["rule_names"].append("clinical_risk")

    _rewrite(preset, "policy.toml", add_canonical_extra)
    _assert_rejected(
        preset,
        "policy.toml",
        "protocol.rule_names",
        "missing canonical identifiers=[]",
        "extra canonical identifiers=['clinical_risk']",
    )


def test_policy_pairs_report_missing_canonical_rule_entry(tmp_path: Path) -> None:
    preset = _copy_preset(tmp_path)
    missing_rule_id = "C_MEDICAL_NO_DIAGNOSIS"
    missing_rule = "no_medical_diagnosis"

    def remove_rule(document: dict[str, Any]) -> None:
        document["rules"] = [
            row for row in document["rules"] if row["rule_id"] != missing_rule_id
        ]

    _rewrite(preset, "policy.toml", remove_rule)
    _assert_rejected(
        preset,
        "policy.toml",
        "rules",
        f"missing canonical pairs=[('{missing_rule_id}', '{missing_rule}')]",
    )


def test_policy_pairs_redact_extra_noncanonical_rule_entry(tmp_path: Path) -> None:
    preset = _copy_preset(tmp_path)
    private_rule_id = "PRIVATE-CLINIC-RULE-ID"
    private_rule = "PRIVATE-CLINIC-RULE"

    def add_rule(document: dict[str, Any]) -> None:
        row = document["rules"][0].copy()
        row["rule_id"] = private_rule_id
        row["rule"] = private_rule
        document["rules"].append(row)

    _rewrite(preset, "policy.toml", add_rule)
    output = _assert_rejected(
        preset,
        "policy.toml",
        "rules",
        "extra noncanonical pairs=<redacted count=1>",
    )
    assert private_rule_id not in output
    assert private_rule not in output


def test_duplicate_noncanonical_policy_rule_id_is_redacted(tmp_path: Path) -> None:
    preset = _copy_preset(tmp_path)
    private_rule_id = "PRIVATE-RULE-SENTINEL"

    def duplicate_private_rule_id(document: dict[str, Any]) -> None:
        document["rules"][0]["rule_id"] = private_rule_id
        document["rules"][1]["rule_id"] = private_rule_id

    _rewrite(preset, "policy.toml", duplicate_private_rule_id)
    output = _assert_rejected(
        preset,
        "policy.toml",
        "rules[1].rule_id",
        "<redacted noncanonical identifier>",
        "unique rule ids",
    )
    assert private_rule_id not in output


def test_policy_pairs_name_both_sides_of_canonical_cross_pair(
    tmp_path: Path,
) -> None:
    preset = _copy_preset(tmp_path)
    first_id = "C_MEDICAL_NO_DIAGNOSIS"
    first_rule = "no_medical_diagnosis"
    second_id = "C_MEDICAL_NO_MED_CHANGE_POSTCARE"
    second_rule = "no_medication_prescription"

    def cross_pair(document: dict[str, Any]) -> None:
        by_id = {row["rule_id"]: row for row in document["rules"]}
        by_id[first_id]["rule"], by_id[second_id]["rule"] = (
            by_id[second_id]["rule"],
            by_id[first_id]["rule"],
        )

    _rewrite(preset, "policy.toml", cross_pair)
    _assert_rejected(
        preset,
        "policy.toml",
        "rules",
        "mismatched canonical pairs=",
        f"rule_id={first_id!r} expects rule={first_rule!r}",
        f"rule_id={second_id!r} expects rule={second_rule!r}",
        f"received {first_rule!r}",
        f"received {second_rule!r}",
    )


def test_policy_imperative_literals_require_models_module(tmp_path: Path) -> None:
    preset = _copy_preset(tmp_path)
    _rewrite(
        preset,
        "policy.toml",
        lambda document: document["imperative_literals"].pop("policy.models"),
    )
    _assert_rejected(
        preset,
        "policy.toml",
        "imperative_literals.policy.models",
        "exact keys",
    )


def test_policy_imperative_literals_require_constraint_method(tmp_path: Path) -> None:
    preset = _copy_preset(tmp_path)
    method = "PolicyConstraints.to_prompt_constraints"
    _rewrite(
        preset,
        "policy.toml",
        lambda document: document["imperative_literals"]["policy.models"].pop(method),
    )
    _assert_rejected(
        preset,
        "policy.toml",
        f"imperative_literals.policy.models.{method}",
        "exact keys",
    )


def test_policy_constraint_prefixes_cannot_be_empty(tmp_path: Path) -> None:
    preset = _copy_preset(tmp_path)
    method = "PolicyConstraints.to_prompt_constraints"
    _rewrite(
        preset,
        "policy.toml",
        lambda document: document["imperative_literals"]["policy.models"].__setitem__(
            method, []
        ),
    )
    _assert_rejected(
        preset,
        "policy.toml",
        f"imperative_literals.policy.models.{method}",
        "exactly one string constraint prefix",
    )


def test_policy_constraint_prefixes_reject_extra_content_without_exposing_it(
    tmp_path: Path,
) -> None:
    preset = _copy_preset(tmp_path)
    method = "PolicyConstraints.to_prompt_constraints"
    private_prefix = "PRIVATE-CLINIC-PREFIX"

    def append_prefix(document: dict[str, Any]) -> None:
        document["imperative_literals"]["policy.models"][method].append(private_prefix)

    _rewrite(preset, "policy.toml", append_prefix)
    output = _assert_rejected(
        preset,
        "policy.toml",
        f"imperative_literals.policy.models.{method}",
        "exactly one string constraint prefix",
    )
    assert private_prefix not in output


def test_cross_references_and_namespaces_cannot_be_conflated(tmp_path: Path) -> None:
    transition = _copy_preset(tmp_path, "transition")
    _rewrite(
        transition,
        "srm.toml",
        lambda document: document["funnel"]["transitions"][0].__setitem__(
            "to", "unknown_stage"
        ),
    )
    _assert_rejected(
        transition, "srm.toml", "funnel.transitions[0].to", "unknown_stage"
    )

    pair = _copy_preset(tmp_path, "pair")

    def cross_pair(document: dict[str, Any]) -> None:
        document["rules"][0]["rule"], document["rules"][1]["rule"] = (
            document["rules"][1]["rule"],
            document["rules"][0]["rule"],
        )

    _rewrite(pair, "policy.toml", cross_pair)
    _assert_rejected(pair, "policy.toml", "rules", "canonical (rule_id, rule) pairs")

    response = _copy_preset(tmp_path, "response")

    def confuse_response(document: dict[str, Any]) -> None:
        document["handoff_responses"]["user_request"] = document[
            "handoff_responses"
        ].pop("explicit_request")

    _rewrite(response, "flow.toml", confuse_response)
    _assert_rejected(response, "flow.toml", "handoff_responses", "explicit_request")


@pytest.mark.parametrize("mode", ("missing", "extra"))
def test_flow_thresholds_require_exact_keys(tmp_path: Path, mode: str) -> None:
    preset = _copy_preset(tmp_path)

    def mutate(document: dict[str, Any]) -> None:
        thresholds = document["thresholds"]
        if mode == "missing":
            thresholds.pop("rag_max_turns")
        else:
            thresholds["unexpected"] = 1

    _rewrite(preset, "flow.toml", mutate)
    expected_key = (
        "thresholds.rag_max_turns" if mode == "missing" else "thresholds.unexpected"
    )
    _assert_rejected(preset, "flow.toml", expected_key, "exact keys")


THRESHOLD_PATHS = (
    ("flow.toml", ("thresholds", "safety_escalation"), 0),
    ("flow.toml", ("thresholds", "rag_max_turns"), 0),
    ("srm.toml", ("funnel", "turn_thresholds", "lead_novo"), -1),
    ("srm.toml", ("rag", "confidence_threshold"), 0),
    ("srm.toml", ("handoff", "max_turns"), 0),
    ("srm.toml", ("handoff", "min_confidence"), 0),
    ("srm.toml", ("engine", "confidence_threshold"), 0),
    ("srm.toml", ("engine", "safety_threshold"), 0),
    ("srm.toml", ("engine", "max_turns_for_handoff"), 0),
)


@pytest.mark.parametrize("filename,path,out_of_range", THRESHOLD_PATHS)
@pytest.mark.parametrize("invalid_kind", ("wrong_type", "bool", "out_of_range"))
def test_numeric_thresholds_reject_invalid_scalars(
    tmp_path: Path,
    filename: str,
    path: tuple[str, ...],
    out_of_range: int,
    invalid_kind: str,
) -> None:
    preset = _copy_preset(tmp_path)
    invalid: Any = {
        "wrong_type": "invalid",
        "bool": True,
        "out_of_range": out_of_range,
    }[invalid_kind]
    _rewrite(
        preset,
        filename,
        lambda document: _set_path(document, path, invalid),
    )
    _assert_rejected(preset, filename, ".".join(path), "threshold")


@pytest.mark.parametrize("literal", ("nan", "inf", "-inf"))
@pytest.mark.parametrize(
    "filename,path",
    (
        ("srm.toml", ("intents", "saudacao", "weight")),
        ("srm.toml", ("rag", "confidence_threshold")),
        ("srm.toml", ("engine", "safety_threshold")),
    ),
)
def test_non_finite_toml_numbers_fail_closed_before_publication(
    tmp_path: Path,
    filename: str,
    path: tuple[str, ...],
    literal: str,
) -> None:
    preset = _copy_preset(tmp_path)
    _rewrite(
        preset, filename, lambda document: _set_path(document, path, float(literal))
    )
    _assert_rejected(preset, filename, ".".join(path), "finite TOML number")


@pytest.mark.parametrize(
    "filename,path",
    (
        ("srm.toml", ("intents", "saudacao", "category")),
        ("srm.toml", ("funnel", "transitions", 0, "to")),
        ("srm.toml", ("engine", "category_actions", "greeting")),
        ("flow.toml", ("state_machine", "transitions", 0, "to")),
    ),
)
def test_list_valued_cross_references_surface_only_actionable_preset_error(
    tmp_path: Path,
    filename: str,
    path: tuple[str | int, ...],
) -> None:
    preset = _copy_preset(tmp_path)

    def mutate(document: dict[str, Any]) -> None:
        current: Any = document
        for part in path[:-1]:
            current = current[part]
        current[path[-1]] = ["malformed-private-value"]

    _rewrite(preset, filename, mutate)
    dotted = "".join(
        f"[{part}]" if isinstance(part, int) else ("." if index else "") + part
        for index, part in enumerate(path)
    )
    output = _assert_rejected(preset, filename, dotted, "a string")
    assert "malformed-private-value" not in output


@pytest.mark.parametrize(
    "literal,type_summary",
    (
        ("1979-05-27T07:32:00Z", "<datetime>"),
        ("1979-05-27", "<date>"),
        ("07:32:00", "<time>"),
    ),
)
def test_nested_toml_datetime_scalars_reject_before_freeze(
    tmp_path: Path, literal: str, type_summary: str
) -> None:
    preset = _copy_preset(tmp_path)
    path = preset / "policy.toml"
    document = path.read_text(encoding="utf-8")
    replacement = f'"metadata" = {{ "nested" = [{literal}] }}'
    assert '"metadata" = {}' in document
    document = document.replace('"metadata" = {}', replacement, 1)
    path.write_text(document, encoding="utf-8")
    output = _assert_rejected(
        preset,
        "policy.toml",
        "rules[0].metadata.nested[0]",
        "supported TOML scalar",
    )
    assert type_summary in output
    assert literal not in output


def test_group_one_python_sources_are_inventory_clean(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "pt-br-inventory.py"),
            "--max",
            "0",
            str(PACKAGE_ROOT / "neurostack_core" / "presets" / "loader.py"),
            str(PACKAGE_ROOT / "neurostack_core" / "protocol.py"),
        ],
        cwd=ROOT,
        env=_environment(),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert "CLOSED INVENTORY: 0 literals across 0 files" in completed.stdout
    assert "PROTOCOL ALLOWLIST: 138 exact identifiers (verified)" in completed.stdout

    unlisted = tmp_path / "unlisted.py"
    unlisted.write_text('KEY = "atendimento tipo adicional"\n', encoding="utf-8")
    rejected = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "pt-br-inventory.py"),
            "--max",
            "0",
            str(unlisted),
        ],
        cwd=ROOT,
        env=_environment(),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert rejected.returncode == 1
    assert "inventory exceeds --max 0: found 1" in rejected.stderr


def test_inventory_detects_unaccented_prompt_domain_content() -> None:
    inventory = runpy.run_path(str(ROOT / "scripts" / "pt-br-inventory.py"))
    is_pt_br_literal = inventory["is_pt_br_literal"]
    allowlist = inventory["_load_allowlist"]()
    residuals = (
        "assistente",
        "Se ele recusar: continue a conversa normalmente.",
        "Data e hora atual",
        "Contexto de Conhecimento",
        "Fontes",
        "Especialidades",
        "Diferenciais",
        "Estacionamento",
        "gratuita",
        "cortesia",
        "R$ 100",
        "Boa noite",
        "Resumo da Conversa",
        "Canal",
        "Turnos",
        "Recursos de Ajuda",
        "NUNCA sugira medicamentos ou dosagens",
        "Etapa atual do funil",
        "SAMU: 192",
        "CVV: 188",
        "%d/%m/%Y %H:%M",
        "%H:%M",
    )

    missed = [
        literal for literal in residuals if not is_pt_br_literal(literal, allowlist)
    ]
    assert missed == []


def test_inventory_rejects_localized_datetime_formats(tmp_path: Path) -> None:
    planted = tmp_path / "localized_formats.py"
    planted.write_text(
        'CURRENT_DATETIME_FORMAT = "%d/%m/%Y %H:%M"\n'
        'GREETING_TIME_FORMAT = "%H:%M"\n',
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "pt-br-inventory.py"),
            "--max",
            "0",
            str(planted),
        ],
        cwd=ROOT,
        env=_environment(),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 1
    assert "CLOSED INVENTORY: 2 literals across 1 files" in completed.stdout
    assert "inventory exceeds --max 0: found 2" in completed.stderr


def test_non_default_prompt_content_replaces_residual_portuguese_labels(
    tmp_path: Path,
) -> None:
    preset = _copy_preset(tmp_path)

    def mutate(document: dict[str, Any]) -> None:
        imperative = document["imperative_literals"]
        normal = imperative["prompt.templates.normal"]
        normal["NormalTemplate.get_context_instructions"][0] = " (clinic local time)"
        clinic = normal["NormalTemplate._build_clinic_context"]
        clinic[0] = "Clinic Configuration"
        clinic[11] = "Pricing is configured by the clinic."
        clinic[25] = "Style: do not use emojis."

        normal_residual = imperative["prompt.templates.normal.residual"]
        replacements = {
            "assistant_default": "Preset Assistant",
            "current_datetime_label": "**Current date and time:** ",
            "knowledge_context_title": "Knowledge Context",
            "knowledge_sources_label": "\nSources:",
            "context_language_override_en": "English response required.",
            "specialties_label": "**Specialties:** ",
            "differentials_label": "**Differentials:** ",
            "parking_label": "**Parking:** ",
        }
        for key, value in replacements.items():
            normal_residual[key] = [value]

        handoff = imperative["prompt.templates.handoff"]
        handoff["HandoffTemplate.get_context_instructions"] = [
            "Information to Collect",
            "User name",
            "Request summary",
            "Previous attempts",
            "Urgency",
            "Contact preference",
            "Collected Information",
            "Waiting guidance",
            "Connected guidance",
        ]
        handoff["HandoffTemplate.generate_handoff_summary"] = [
            "Not specified",
            "## Agent Summary",
            "**Transfer reason:** ",
            "**Stage:** ",
            "**Last intent:** ",
            "### Collected Information:",
            "**Safety Alert:** ",
        ]
        handoff_residual = imperative["prompt.templates.handoff.residual"]
        handoff_residual["history_title"] = ["Conversation Summary"]
        handoff_residual["channel_label"] = ["**Channel:** "]
        handoff_residual["turns_label"] = ["**Turns:** "]
        handoff_residual["context_title"] = ["### Conversation Context:"]

        safety = imperative["prompt.templates.safety"]
        safety["SafetyTemplate.get_context_instructions"] = [
            "Crisis line",
            "Emergency medical service",
            "Fire service",
            "Police",
            "Human rights line",
            "Violence support line",
        ]
        safety["SafetyTemplate.get_constraints"] = [
            "Never diagnose",
            "Never minimize risk",
            "Always offer professional resources",
            "Always remain empathetic",
            "\n## Mandatory Safety Constraints\n",
        ]
        safety_residual = imperative["prompt.templates.safety.residual"]
        safety_residual["resources_title"] = ["Help Resources"]
        safety_residual["history_title"] = ["Conversation Context"]
        safety_residual["no_medication_dosages"] = [
            "Never suggest medication or dosage"
        ]

        renderer = imperative["prompt.renderer"]
        renderer["VariableResolver.from_context_window"] = [
            "**Last intent:** ",
            "**Previous intents:** ",
            "**Collected data:** ",
        ]
        imperative["prompt.renderer.residual"]["current_stage_label"] = [
            "**Current funnel stage:** "
        ]

    _rewrite(preset, "prompt.toml", mutate)
    completed = _run(
        """
import pathlib, sys
from types import SimpleNamespace
from neurostack_core import use_preset
use_preset(pathlib.Path(sys.argv[1]))
from neurostack_core.prompt.models import PromptVariables
from neurostack_core.prompt.renderer import VariableResolver
from neurostack_core.prompt.templates.handoff import HandoffTemplate
from neurostack_core.prompt.templates.normal import NormalTemplate
from neurostack_core.prompt.templates.safety import SafetyTemplate

normal = NormalTemplate()
system = normal.get_system_prompt(PromptVariables(language='en'))
context = normal.get_context_instructions(
    PromptVariables(
        language='en',
        funnel_stage='unknown',
        knowledge_context='English knowledge',
        knowledge_sources=['English source'],
    )
)
clinic = normal._build_clinic_context(
    PromptVariables(
        custom={
            'clinic_specialties': ['One'],
            'differentials': 'Two',
            'parking_info': 'Three',
        }
    )
)
handoff_variables = PromptVariables(
    handoff_reason='unknown',
    history_summary='English history',
    funnel_stage='',
)
handoff = HandoffTemplate().get_context_instructions(handoff_variables)
summary = HandoffTemplate().generate_handoff_summary(handoff_variables)
safety_variables = PromptVariables(
    safety_category='unknown', history_summary='English history'
)
safety_template = SafetyTemplate()
safety = safety_template.get_context_instructions(safety_variables)
constraints = safety_template.get_constraints(safety_variables)
context_window = SimpleNamespace(
    cognitive=SimpleNamespace(
        funnel_stage='new_lead',
        last_intent='greeting',
        intent_history=['greeting'],
        slots={'name': 'Alex'},
    ),
    session_id=None,
    tenant_id=None,
    constraints=[],
    safety_reminders=[],
    compressed_history=None,
)
journey = VariableResolver.from_context_window(context_window).user_journey or ''
rendered = '\\n'.join((system, context, clinic, handoff, summary, safety, constraints, journey))
required = (
    'Preset Assistant',
    'Current date and time',
    'Knowledge Context',
    'Specialties',
    'Conversation Summary',
    'Help Resources',
    'Current funnel stage',
)
for marker in required:
    assert marker in rendered, marker
for forbidden in (
    'assistente',
    'Data e hora atual',
    'Contexto de Conhecimento',
    'Especialidades',
    'Diferenciais',
    'Estacionamento',
    'Resumo da Conversa',
    '**Canal:**',
    '**Turnos:**',
    'Contexto da Conversa',
    'Recursos de Ajuda',
    'medicamentos ou dosagens',
    'Etapa atual do funil',
):
    assert forbidden not in rendered, forbidden
""",
        preset,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_non_default_prompt_datetime_formats_control_rendering(tmp_path: Path) -> None:
    preset = _copy_preset(tmp_path)

    def mutate(document: dict[str, Any]) -> None:
        residual = document["imperative_literals"]["prompt.templates.normal.residual"]
        residual["current_datetime_format"] = ["%Y-%m-%dT%H:%M"]
        residual["greeting_time_format"] = ["%H.%M"]

    _rewrite(preset, "prompt.toml", mutate)
    completed = _run(
        """
import pathlib, re, sys
from datetime import datetime
from neurostack_core import use_preset
use_preset(pathlib.Path(sys.argv[1]))
from neurostack_core.prompt.models import PromptVariables
from neurostack_core.prompt.templates.normal import NormalTemplate

template = NormalTemplate()
context = template.get_context_instructions(
    PromptVariables(funnel_stage='unknown')
)
assert re.search(r'\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}', context), context
greeting = template._get_intent_instruction(
    'saudacao', now_local=datetime(2026, 10, 1, 14, 5)
)
assert '14.05' in greeting, greeting
assert '14:05' not in greeting, greeting
""",
        preset,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_content_only_preset_cannot_mutate_prompt_control_protocol(
    tmp_path: Path,
) -> None:
    preset = _copy_preset(tmp_path)

    def mutate(document: dict[str, Any]) -> None:
        normal = document["imperative_literals"]["prompt.templates.normal"]
        build = normal["NormalTemplate.build_system_message"]
        build[19:22] = [
            "FOREIGN_TRIAGE_EN",
            "FOREIGN_TRIAGE_ES",
            "FOREIGN_TRIAGE_PT",
        ]
        context = normal["NormalTemplate.get_context_instructions"]
        context[4:6] = [
            "FOREIGN_INSURANCE_COLLECTION_EN",
            "FOREIGN_INSURANCE_COLLECTION_PT",
        ]
        clinic = normal["NormalTemplate._build_clinic_context"]
        clinic[5] = "FOREIGN_PRICE_HOLD"
        document["lexicon"].update(
            {
                "free_pt": "FOREIGN_FREE_PT",
                "free_feminine_pt": "FOREIGN_FREE_FEMININE_PT",
                "complimentary_pt": "FOREIGN_COMPLIMENTARY_PT",
                "free_en": "FOREIGN_FREE_EN",
                "complimentary_en": "FOREIGN_COMPLIMENTARY_EN",
                "currency_prefix": "USD",
            }
        )

    _rewrite(preset, "prompt.toml", mutate)
    completed = _run(
        """
import pathlib, sys
from neurostack_core import use_preset
use_preset(pathlib.Path(sys.argv[1]))
from neurostack_core.protocol import (
    CAN_QUOTE_PRIVATE_PAY_CUSTOM_KEY,
    CARE_MODALITY_SLOT,
    INSURANCE_CARE_MODALITY,
    INSURANCE_TRIAGE_CUSTOM_KEY,
    PRIVATE_PAY_CARE_MODALITY,
)
from neurostack_core.prompt.models import PromptVariables
from neurostack_core.prompt.templates.normal import NormalTemplate

assert INSURANCE_TRIAGE_CUSTOM_KEY == 'ask_convenio_triage'
assert CARE_MODALITY_SLOT == 'atendimento_tipo'
assert INSURANCE_CARE_MODALITY == 'convenio'
assert PRIVATE_PAY_CARE_MODALITY == 'particular'
assert CAN_QUOTE_PRIVATE_PAY_CUSTOM_KEY == 'can_quote_particular'

template = NormalTemplate()
triage = template.build_system_message(
    PromptVariables(custom={INSURANCE_TRIAGE_CUSTOM_KEY: True})
).content
assert 'FOREIGN_TRIAGE_PT' in triage

insurance_collection = template.get_context_instructions(
    PromptVariables(
        funnel_stage='agendamento_solicitado',
        custom={CARE_MODALITY_SLOT: INSURANCE_CARE_MODALITY},
    )
)
assert 'FOREIGN_INSURANCE_COLLECTION_PT' in insurance_collection

held = template._build_clinic_context(
    PromptVariables(
        custom={
            'accepts_insurance': 'yes',
            CAN_QUOTE_PRIVATE_PAY_CUSTOM_KEY: False,
            'provide_prices': 'yes',
            'pricing_mode': 'exact_allowed',
            'initial_consultation': 'FOREIGN_PAID',
            'consult_service_price': 'R$ 999,00',
        }
    )
)
assert 'FOREIGN_PRICE_HOLD' in held
assert 'R$' not in held
assert '999' not in held
""",
        preset,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_failed_load_leaves_selection_unset(tmp_path: Path) -> None:
    preset = _copy_preset(tmp_path)
    _rewrite(
        preset,
        "srm.toml",
        lambda document: document["handoff"]["regexes"].__setitem__(
            "negative", "[broken"
        ),
    )
    completed = _run(
        """
import pathlib, sys
from neurostack_core import PresetError, get_preset, use_preset
try:
    use_preset(pathlib.Path(sys.argv[1]))
except PresetError:
    pass
else:
    raise SystemExit('invalid preset selected')
assert get_preset().source_id == 'bundled:pt_br_clinic'
""",
        preset,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("kind", ("regex", "threshold", "transition"))
def test_parity_compare_rejects_behavioral_drift(tmp_path: Path, kind: str) -> None:
    preset = _copy_preset(tmp_path)
    if kind == "regex":
        _rewrite(
            preset,
            "srm.toml",
            lambda document: document["engine"]["regexes"].__setitem__(
                "atendimento_tipo",
                document["engine"]["regexes"]["atendimento_tipo"].replace(
                    "particular", "private"
                ),
            ),
        )
    elif kind == "threshold":
        _rewrite(
            preset,
            "flow.toml",
            lambda document: document["thresholds"].__setitem__(
                "rag_max_turns", document["thresholds"]["rag_max_turns"] + 1
            ),
        )
    else:
        _rewrite(
            preset,
            "srm.toml",
            lambda document: document["funnel"]["transitions"][0].__setitem__(
                "to", "em_qualificacao"
            ),
        )
    completed = _compare(preset)
    assert completed.returncode == 1
    assert "baseline:" in completed.stderr and "preset:" in completed.stderr


def test_bundled_regex_payloads_use_decoded_unicode() -> None:
    with (BUNDLED / "srm.toml").open("rb") as handle:
        document = tomllib.load(handle)

    patterns = _affected_unicode_regex_patterns(document)
    assert len(patterns) == 51
    assert (
        sum(
            pattern.count(character)
            for pattern in patterns
            for character in _LEGACY_UNICODE_ESCAPES
        )
        == 84
    )
    assert all(
        re.search(r"\\u00[0-9A-Fa-f]{2}", pattern) is None
        for pattern in _all_srm_regex_patterns(document)
    )


def test_regex_unicode_normalization_is_narrow() -> None:
    pattern = r"\bconvuls\u00e3o\s+grave\d+\x20\N{SPACE}"
    assert _parity._normalize_regex_pattern(pattern) == (
        r"\bconvulsão\s+grave\d+\x20\N{SPACE}"
    )

    definitions = {
        "sample": {
            "patterns": [r"\bn\u00e3o\b"],
            "keywords": [r"prompt preserva \u00e3 como texto"],
        }
    }
    normalized = _parity._normalize_definition_patterns(definitions)
    assert normalized["sample"]["patterns"] == [r"\bnão\b"]
    assert normalized["sample"]["keywords"] == [r"prompt preserva \u00e3 como texto"]
    assert _parity._normalize(r"conteúdo \u00e3 literal") == (
        r"conteúdo \u00e3 literal"
    )


def test_regex_unicode_normalization_preserves_doubled_backslash_literals() -> None:
    pattern = r"prefix\\u00e3suffix"
    normalized = _parity._normalize_regex_pattern(pattern)

    assert normalized == pattern
    assert _regex_opcode_dump(normalized) == _regex_opcode_dump(pattern)
    assert re.fullmatch(normalized, r"prefix\u00e3suffix") is not None


@pytest.mark.parametrize(
    "pattern",
    [
        r"\u002e",
        r"\u005b",
        r"\u005c",
        r"\u000a",
        r"\u007f",
        r"\u200d",
        r"\u00zz",
        r"\u00e",
        r"\u",
        r"\\\u00e3",
        r"\\\\u00e3",
    ],
)
def test_regex_unicode_normalization_fails_closed_for_unsafe_or_ambiguous_escapes(
    pattern: str,
) -> None:
    with pytest.raises(ValueError, match=r"Unicode regex escape|backslash run"):
        _parity._normalize_regex_pattern(pattern)


@pytest.mark.parametrize("escape", [r"\u00e3", r"\u00e7", r"\u0301"])
def test_regex_unicode_normalization_decodes_only_safe_letters_and_marks(
    escape: str,
) -> None:
    decoded = chr(int(escape[2:], 16))
    normalized = _parity._normalize_regex_pattern(r"\b" + escape + r"\b")

    assert normalized == r"\b" + decoded + r"\b"
    assert _regex_opcode_dump(normalized) == _regex_opcode_dump(r"\b" + escape + r"\b")


def test_regex_unicode_normalization_counterexamples_are_mutation_sensitive() -> None:
    assert _parity._normalize_regex_pattern(r"\\u00e3") == r"\\u00e3"
    for unsafe in (r"\u002e", r"\u005b"):
        with pytest.raises(ValueError, match="non-ASCII Unicode letter or mark"):
            _parity._normalize_regex_pattern(unsafe)


def test_all_repaired_regexes_keep_equivalent_compiled_opcodes() -> None:
    with (BUNDLED / "srm.toml").open("rb") as handle:
        document = tomllib.load(handle)

    decoded_patterns = _affected_unicode_regex_patterns(document)
    escaped_patterns = [_legacy_escape_unicode(pattern) for pattern in decoded_patterns]
    assert len(decoded_patterns) == 51
    assert sum(pattern.count(r"\u00") for pattern in escaped_patterns) == 84
    assert all(
        _regex_opcode_dump(escaped) == _regex_opcode_dump(decoded)
        for escaped, decoded in zip(escaped_patterns, decoded_patterns, strict=True)
    )


def test_parity_rejects_reintroduced_literal_unicode_regex_escape(
    tmp_path: Path,
) -> None:
    preset = _copy_preset(tmp_path, "escaped-regex")
    _rewrite(
        preset,
        "srm.toml",
        lambda document: document["safety_patterns"]["clinical_risk"][
            "patterns"
        ].__setitem__(
            0,
            document["safety_patterns"]["clinical_risk"]["patterns"][0].replace(
                "convulsão", r"convuls\u00e3o"
            ),
        ),
    )
    assert r"\u00e3" in (preset / "srm.toml").read_text(encoding="utf-8")

    completed = _compare(preset)
    assert completed.returncode == 1
    assert "baseline:srm" in completed.stderr and "preset:srm" in completed.stderr


def test_preset_has_no_reachable_mutable_container() -> None:
    completed = _run("""
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from neurostack_core import get_preset

preset = get_preset()
seen = set()
def walk(value):
    marker = id(value)
    if marker in seen:
        return
    seen.add(marker)
    assert not isinstance(value, (dict, list, set, bytearray)), type(value)
    if isinstance(value, Mapping):
        for child in value.values(): walk(child)
    elif isinstance(value, tuple):
        for child in value: walk(child)
for module in (preset.srm, preset.flow, preset.policy, preset.prompt): walk(module)
try:
    preset.srm['new'] = 'value'
except TypeError:
    pass
else:
    raise AssertionError('mapping mutated')
try:
    preset.name = 'other'
except (FrozenInstanceError, AttributeError):
    pass
else:
    raise AssertionError('preset mutated')
""")
    assert completed.returncode == 0, completed.stderr


def test_frozen_map_storage_cannot_be_rebound_at_any_depth() -> None:
    completed = _run("""
from neurostack_core import get_preset

preset = get_preset()
top_level = preset.srm
nested = preset.srm['funnel']
before_top = dict(top_level)
before_nested = dict(nested)
for value in (top_level, nested):
    try:
        value._data = {'corrupted': []}
    except AttributeError:
        pass
    else:
        raise AssertionError('FrozenMap._data was rebound')
assert dict(top_level) == before_top
assert dict(nested) == before_nested
assert 'corrupted' not in top_level and 'corrupted' not in nested
""")
    assert completed.returncode == 0, completed.stderr


def test_barrier_concurrency_same_source_publishes_one_object(tmp_path: Path) -> None:
    preset = _copy_preset(tmp_path)
    completed = _run(
        """
import pathlib, sys, threading
from neurostack_core import use_preset
path = pathlib.Path(sys.argv[1])
count = 12
barrier = threading.Barrier(count)
objects = []
errors = []
def worker():
    try:
        barrier.wait(timeout=5)
        objects.append(use_preset(path))
    except BaseException as error:
        errors.append(error)
threads = [threading.Thread(target=worker) for _ in range(count)]
for thread in threads: thread.start()
for thread in threads: thread.join(timeout=10)
assert all(not thread.is_alive() for thread in threads), 'deadlock'
assert not errors, errors
assert len({id(value) for value in objects}) == 1
assert all(len(value.srm) and len(value.flow) and len(value.policy) and len(value.prompt) for value in objects)
""",
        preset,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def test_barrier_concurrency_conflicting_valid_sources_rejects_losers(
    tmp_path: Path,
) -> None:
    first = _copy_preset(tmp_path, "first")
    second = _copy_preset(tmp_path, "second")
    completed = _run(
        """
import pathlib, sys, threading
from neurostack_core import PresetError, get_preset, use_preset
paths = [pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])]
count = 12
barrier = threading.Barrier(count)
objects = []
errors = []
def worker(index):
    try:
        barrier.wait(timeout=5)
        objects.append(use_preset(paths[index % 2]))
    except BaseException as error:
        errors.append(error)
threads = [threading.Thread(target=worker, args=(index,)) for index in range(count)]
for thread in threads: thread.start()
for thread in threads: thread.join(timeout=10)
assert all(not thread.is_alive() for thread in threads), 'deadlock'
assert objects and errors
assert len({id(value) for value in objects}) == 1
selected = get_preset()
assert selected is objects[0]
assert all(isinstance(error, PresetError) for error in errors)
assert all('selected=' in str(error) and 'requested=' in str(error) and 'restart' in str(error) for error in errors)
""",
        first,
        second,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def test_barrier_valid_vs_failing_contender_has_no_partial_state(
    tmp_path: Path,
) -> None:
    valid = _copy_preset(tmp_path, "valid")
    failing = _copy_preset(tmp_path, "failing")
    _rewrite(
        failing,
        "srm.toml",
        lambda document: document["engine"]["regexes"].__setitem__(
            "atendimento_tipo", "[broken"
        ),
    )
    completed = _run(
        """
import pathlib, sys, threading
from neurostack_core import PresetError, get_preset, use_preset
valid, failing = map(pathlib.Path, sys.argv[1:3])
barrier = threading.Barrier(2)
objects = []
errors = []
def worker(path):
    try:
        barrier.wait(timeout=5)
        objects.append(use_preset(path))
    except BaseException as error:
        errors.append(error)
threads = [threading.Thread(target=worker, args=(path,)) for path in (valid, failing)]
for thread in threads: thread.start()
for thread in threads: thread.join(timeout=10)
assert all(not thread.is_alive() for thread in threads), 'deadlock'
if not objects:
    objects.append(use_preset(valid))
selected = get_preset()
assert selected.source_id == 'path:' + str(valid.resolve())
assert selected is objects[-1]
assert len(selected.srm) and len(selected.flow) and len(selected.policy) and len(selected.prompt)
assert errors and all(isinstance(error, PresetError) for error in errors)
""",
        valid,
        failing,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
