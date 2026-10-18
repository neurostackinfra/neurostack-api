"""Static contract for the frontend-build stage of the production Dockerfile.

Everything here is static analysis of ``Dockerfile``, ``.dockerignore``,
``frontend/package.json`` and ``git ls-files``: no Docker daemon is needed, so
none of these assertions proves that an image builds or what it contains at
runtime.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
FRONTEND_PACKAGE = ROOT / "frontend" / "package.json"
NEUROSTACK = ROOT / "frontend" / "artifacts" / "neurostack"
VITE_CONFIG = NEUROSTACK / "vite.config.ts"
NEUROSTACK_PACKAGE = NEUROSTACK / "package.json"
WORKFLOWS = ROOT / ".github" / "workflows"

EXPECTED_STAGES = ["base", "deps", "development", "frontend-build", "production"]
NODE_IMAGE = re.compile(
    r"^node:\d+\.\d+\.\d+-[a-z0-9.-]+@sha256:[0-9a-f]{64}$"  # tag AND digest
)
PNPM_PIN = re.compile(r"^\d+\.\d+\.\d+\+sha512\.[0-9a-f]{128}$")
SPA_SOURCE = "/frontend/artifacts/neurostack/dist/public"
NO_SOURCEMAPS = (
    "test -z \"$(find artifacts/neurostack/dist/public -name '*.map' -print -quit)\""
)
# An inline map (`--sourcemap inline`) is a data: URL inside a JS chunk, not a
# .map file: only the sourceMappingURL comment gives it away.
NO_INLINE_SOURCEMAPS = "! grep -rqs sourceMappingURL artifacts/neurostack/dist/public"
# The whole build RUN, step by step: any extra flag (`-- --sourcemap inline`)
# or masking operator (`|| true`, `;`) changes it and fails the contract.
BUILD_RUN_STEPS = [
    'PORT="${FRONTEND_PORT}" BASE_PATH="${FRONTEND_BASE_PATH}" NODE_ENV=production'
    " pnpm --filter @workspace/neurostack run build",
    "test -s artifacts/neurostack/dist/public/index.html",
    NO_SOURCEMAPS,
    NO_INLINE_SOURCEMAPS,
]
NEUROSTACK_BUILD_SCRIPT = "vite build --config vite.config.ts"
# Instructions the production lineage may use. Anything else (ONBUILD, or a
# heredoc body parsed as instructions) fails the contract.
PRODUCTION_KEYWORDS = {
    "ARG",
    "ENV",
    "WORKDIR",
    "COPY",
    "RUN",
    "USER",
    "EXPOSE",
    "HEALTHCHECK",
    "CMD",
}
# Everything the production lineage may copy, per source stage (None = the
# build context). Anything else (e.g. `COPY . .`) fails the contract.
PRODUCTION_COPY_SOURCES = {
    None: [
        "src",
        "config",
        "alembic.ini",
        "alembic",
        "scripts/bootstrap_admin.py",
    ],
    "deps": [
        "/usr/local/lib/python3.11/site-packages",
        "/usr/local/bin",
        "/app/packages/neurostack-core",
    ],
    "frontend-build": [SPA_SOURCE],
}


@dataclass
class Instruction:
    keyword: str
    args: str


@dataclass
class Stage:
    parent: str
    alias: str
    platform: bool
    instructions: list[Instruction] = field(default_factory=list)


def _instructions(text: str) -> list[Instruction]:
    """Join continuation lines and drop comments, like the Docker parser."""

    result: list[Instruction] = []
    pending = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        line = pending + stripped
        pending = ""
        keyword, _, args = line.partition(" ")
        result.append(Instruction(keyword.upper(), args.strip()))
    assert not pending, "Dockerfile ends inside a line continuation"
    return result


@cache
def _stages() -> tuple[Stage, ...]:
    stages: list[Stage] = []
    for instruction in _instructions(DOCKERFILE.read_text(encoding="utf-8")):
        if instruction.keyword == "FROM":
            tokens = instruction.args.split()
            platform = any(token.startswith("--platform") for token in tokens)
            tokens = [token for token in tokens if not token.startswith("--")]
            assert len(tokens) == 3 and tokens[1].upper() == "AS", instruction.args
            stages.append(Stage(tokens[0], tokens[2], platform))
            continue
        assert stages, f"{instruction.keyword} before the first FROM"
        stages[-1].instructions.append(instruction)
    return tuple(stages)


def _stage(alias: str) -> Stage:
    return next(stage for stage in _stages() if stage.alias == alias)


def _lineage(alias: str) -> list[Stage]:
    aliases = {stage.alias for stage in _stages()}
    chain = [_stage(alias)]
    while chain[-1].parent in aliases:
        chain.append(_stage(chain[-1].parent))
    return chain


@dataclass
class Copy:
    flags: dict[str, str]
    sources: list[str]
    destination: str


def _copy(instruction: Instruction) -> Copy:
    tokens = shlex.split(instruction.args)
    flags: dict[str, str] = {}
    while tokens and tokens[0].startswith("--"):
        name, _, value = tokens.pop(0)[2:].partition("=")
        flags[name] = value
    assert len(tokens) >= 2, instruction.args
    return Copy(flags, tokens[:-1], tokens[-1])


def _copies(stage: Stage) -> list[Copy]:
    return [
        _copy(instruction)
        for instruction in stage.instructions
        if instruction.keyword in {"COPY", "ADD"}
    ]


def _args(stage: Stage) -> dict[str, str]:
    values: dict[str, str] = {}
    for instruction in stage.instructions:
        if instruction.keyword == "ARG":
            name, _, value = instruction.args.partition("=")
            values[name] = value
    return values


def _package_manager() -> str:
    return json.loads(FRONTEND_PACKAGE.read_text(encoding="utf-8"))["packageManager"]


@cache
def _tracked_files() -> frozenset[str]:
    """Tracked paths present in the working tree (identical to a clean checkout
    once committed; deletions not yet committed are already applied)."""

    def listing(*flags: str) -> set[str]:
        completed = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z", *flags],
            capture_output=True,
            check=True,
        )
        return {item for item in completed.stdout.decode().split("\0") if item}

    return frozenset(listing("--cached") - listing("--deleted"))


# --- Docker-faithful .dockerignore matcher ---------------------------------
#
# Mirrors github.com/moby/patternmatcher (used by the classic builder and by
# BuildKit): patterns are cleaned and anchored at the context root, ``*`` and
# ``?`` never cross ``/``, ``**`` crosses directories, a path is excluded when
# it or any parent directory matches, and the last matching pattern wins
# (``!`` re-includes). Unlike gitignore, ``lib/`` only matches ``./lib``.


def _clean(pattern: str) -> str:
    parts: list[str] = []
    for part in pattern.split("/"):
        if part in {"", "."}:
            continue
        if part == ".." and parts and parts[-1] != "..":
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    regex = "^"
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern[index + 1 : index + 2] == "*":
                index += 2
                while pattern[index : index + 1] == "/":
                    index += 1
                regex += ".*" if index >= len(pattern) else "(.*/)?"
                continue
            regex += "[^/]*"
        elif char == "?":
            regex += "[^/]"
        elif char == "[":
            end = pattern.index("]", index)
            regex += pattern[index : end + 1]
            index = end
        elif char == "\\" and index + 1 < len(pattern):
            index += 1
            regex += re.escape(pattern[index])
        else:
            regex += re.escape(char)
        index += 1
    return re.compile(regex + "$")


@cache
def _dockerignore_rules() -> tuple[tuple[bool, re.Pattern[str]], ...]:
    rules: list[tuple[bool, re.Pattern[str]]] = []
    for raw in DOCKERIGNORE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        exclusion = line.startswith("!")
        pattern = _clean(line[1:].strip() if exclusion else line)
        if pattern:
            rules.append((exclusion, _pattern_regex(pattern)))
    return tuple(rules)


def docker_ignores(path: str) -> bool:
    parts = path.split("/")
    candidates = ["/".join(parts[: index + 1]) for index in range(len(parts))]
    ignored = False
    for exclusion, regex in _dockerignore_rules():
        if any(regex.match(candidate) for candidate in candidates):
            ignored = not exclusion
    return ignored


# --- Stage layout ----------------------------------------------------------


def test_stage_order_keeps_production_last_and_verbatim() -> None:
    assert [stage.alias for stage in _stages()] == EXPECTED_STAGES
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^FROM base AS production$", text, re.MULTILINE)
    assert re.search(r"^FROM base AS deps$", text, re.MULTILINE)
    production_cmds = [
        instruction
        for instruction in _stage("production").instructions
        if instruction.keyword == "CMD"
    ]
    assert len(production_cmds) == 1
    assert not any(
        instruction.keyword in {"CMD", "ENTRYPOINT"}
        for instruction in _stage("frontend-build").instructions
    )


def test_no_stage_uses_from_platform() -> None:
    assert not any(stage.platform for stage in _stages())
    assert "--platform" not in DOCKERFILE.read_text(encoding="utf-8")


def test_node_image_is_pinned_by_tag_and_digest() -> None:
    frontend = _stage("frontend-build")
    assert NODE_IMAGE.match(frontend.parent), frontend.parent
    node_stages = [stage for stage in _stages() if stage.parent.startswith("node")]
    assert node_stages == [frontend]


def test_pnpm_pin_is_exact_and_equals_package_manager() -> None:
    frontend = _stage("frontend-build")
    pin = _args(frontend)["PNPM_PIN"]
    assert PNPM_PIN.match(pin), pin
    assert _package_manager() == f"pnpm@{pin}"
    runs = [i.args for i in frontend.instructions if i.keyword == "RUN"]
    assert any(
        'corepack prepare "pnpm@${PNPM_PIN}" --activate' in run for run in runs
    ), runs


def _install_index(stage: Stage) -> int:
    indexes = [
        index
        for index, instruction in enumerate(stage.instructions)
        if instruction.keyword == "RUN" and "pnpm install" in instruction.args
    ]
    assert len(indexes) == 1
    return indexes[0]


def test_install_is_frozen_and_runs_no_scripts() -> None:
    frontend = _stage("frontend-build")
    install = frontend.instructions[_install_index(frontend)].args
    tokens = shlex.split(install)
    assert "--frozen-lockfile" in tokens
    assert "--ignore-scripts" in tokens
    assert "--no-frozen-lockfile" not in tokens


def test_node_env_production_is_never_set_before_install() -> None:
    frontend = _stage("frontend-build")
    install_index = _install_index(frontend)
    for instruction in frontend.instructions[: install_index + 1]:
        assert "NODE_ENV" not in instruction.args, instruction
    for instruction in frontend.instructions:
        if instruction.keyword in {"ENV", "ARG"}:
            assert "NODE_ENV" not in instruction.args, instruction


def _build_run() -> str:
    frontend = _stage("frontend-build")
    builds = [
        instruction.args
        for instruction in frontend.instructions[_install_index(frontend) + 1 :]
        if instruction.keyword == "RUN" and "run build" in instruction.args
    ]
    assert len(builds) == 1, builds
    return builds[0]


def test_build_uses_root_base_path_and_checks_index_html() -> None:
    frontend = _stage("frontend-build")
    assert _args(frontend)["FRONTEND_BASE_PATH"] == "/"
    build = _build_run()
    assert 'BASE_PATH="${FRONTEND_BASE_PATH}"' in build
    assert "test -s artifacts/neurostack/dist/public/index.html" in build
    # A .map carries sourcesContent (the original sources): none may ship,
    # neither as a file nor inlined as a data: URL in a chunk.
    assert NO_SOURCEMAPS in build
    assert NO_INLINE_SOURCEMAPS in build


def test_build_run_guards_cannot_be_masked_or_bypassed() -> None:
    build = _build_run()
    assert "--sourcemap" not in build, f"build RUN enables sourcemaps: {build}"
    # Every step must gate the layer: `|| true`, `;`, `&` or a pipe would let a
    # failing guard pass silently.
    steps = [step.strip() for step in re.split(r"\s*&&\s*", build)]
    for step in steps:
        for operator in ("||", ";", "&", "|", "`"):
            assert operator not in step, f"{operator!r} masks a guard in: {step}"
    assert steps == BUILD_RUN_STEPS


def test_neurostack_build_script_is_pinned() -> None:
    # `vite build --sourcemap inline` here would ship sources in a JS chunk.
    package = json.loads(NEUROSTACK_PACKAGE.read_text(encoding="utf-8"))
    assert package["scripts"]["build"] == NEUROSTACK_BUILD_SCRIPT


def test_vite_config_does_not_enable_sourcemaps() -> None:
    text = VITE_CONFIG.read_text(encoding="utf-8")
    assert "build:" in text
    assert not re.search(r"\bsourcemap\b", text, re.IGNORECASE)


def test_pnpm_does_not_verify_deps_before_run() -> None:
    # Load-bearing: without it `pnpm run build` re-runs a full install with
    # scripts enabled and dies on the root preinstall.
    frontend = _stage("frontend-build")
    install_index = _install_index(frontend)
    envs = [
        token
        for instruction in frontend.instructions[:install_index]
        if instruction.keyword == "ENV"
        for token in shlex.split(instruction.args)
    ]
    assert "pnpm_config_verify_deps_before_run=false" in envs, envs


def test_signed_workflow_does_not_override_build_args() -> None:
    texts = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
    }
    builders = [name for name, text in texts.items() if "docker buildx build" in text]
    assert builders, "no workflow builds the image"
    for name in builders:
        assert "--build-arg" not in texts[name], name
    for name, text in texts.items():
        for arg in ("PNPM_PIN", "FRONTEND_BASE_PATH", "FRONTEND_PORT"):
            assert arg not in text, (name, arg)


# --- Final stage -----------------------------------------------------------


def test_exactly_one_copy_from_frontend_build_lands_in_final_stage() -> None:
    copies = [
        (stage.alias, copy)
        for stage in _stages()
        for copy in _copies(stage)
        if copy.flags.get("from") == "frontend-build"
    ]
    assert len(copies) == 1
    alias, copy = copies[0]
    assert alias == "production"
    assert copy.sources == [SPA_SOURCE]
    assert copy.destination == "/app/spa"
    assert copy.flags.get("chown") == "neurostack:neurostack"
    production_env = [
        i.args for i in _stage("production").instructions if i.keyword == "ENV"
    ]
    assert "SPA_DIST_DIR=/app/spa" in production_env


def test_production_lineage_has_no_node_runtime_or_modules() -> None:
    lineage = _lineage("production")
    assert [stage.alias for stage in lineage] == ["production", "base"]
    assert lineage[-1].parent.startswith("python:")
    for stage in lineage:
        assert not stage.parent.startswith("node")
        for instruction in stage.instructions:
            assert "node_modules" not in instruction.args, instruction

    copied: dict[str | None, list[str]] = {}
    for stage in lineage:
        for copy in _copies(stage):
            copied.setdefault(copy.flags.get("from"), []).extend(copy.sources)
    assert copied == PRODUCTION_COPY_SOURCES


def test_production_lineage_imports_files_only_through_the_copy_allowlist() -> None:
    # `RUN --mount=type=bind,from=frontend-build,... cp -r ...` (or a bind of
    # the build context) would import files without a COPY.
    for stage in _lineage("production"):
        for instruction in stage.instructions:
            assert instruction.keyword in PRODUCTION_KEYWORDS, (
                stage.alias,
                instruction,
            )
            assert "<<" not in instruction.args, (stage.alias, instruction)
            assert "--mount" not in instruction.args, (stage.alias, instruction)


# --- Build context ---------------------------------------------------------


def _frontend_copy_sources() -> list[str]:
    sources: list[str] = []
    for copy in _copies(_stage("frontend-build")):
        assert "from" not in copy.flags
        sources.extend(copy.sources)
    return sources


def test_every_frontend_copy_source_is_tracked_and_in_the_context() -> None:
    tracked = _tracked_files()
    sources = _frontend_copy_sources()
    assert sources
    for source in sources:
        assert source.startswith("frontend/"), source
        assert not any(char in source for char in "*?["), source
        assert not docker_ignores(source), source
        if source in tracked:
            continue
        children = [path for path in tracked if path.startswith(source + "/")]
        assert children, f"{source} is neither a tracked file nor a tracked dir"
        retained = [path for path in children if not docker_ignores(path)]
        assert retained, f"{source} is empty in the Docker context"


@pytest.mark.parametrize(
    "path",
    [
        "frontend/package.json",
        "frontend/pnpm-lock.yaml",
        "frontend/pnpm-workspace.yaml",
        "frontend/.npmrc",
        "frontend/tsconfig.base.json",
        "frontend/lib/api-client-react/package.json",
        "frontend/lib/api-client-react/src/index.ts",
        "frontend/lib/api-client-react/src/generated/api.ts",
        "frontend/attached_assets/logo_png_1775411054369.png",
        "frontend/artifacts/neurostack/index.html",
        "frontend/artifacts/neurostack/vite.config.ts",
        "frontend/artifacts/neurostack/src/main.tsx",
        "frontend/artifacts/neurostack/src/App.tsx",
        "frontend/artifacts/neurostack/public/favicon.png",
    ],
)
def test_frontend_build_inputs_are_tracked_and_retained(path: str) -> None:
    assert path in _tracked_files()
    assert not docker_ignores(path)


@pytest.mark.parametrize(
    "path",
    [
        "frontend/node_modules/.pnpm/vite/index.js",
        "frontend/artifacts/neurostack/node_modules/vite/index.js",
        "frontend/artifacts/neurostack/dist/public/index.html",
        "frontend/artifacts/neurostack/tests/e2e/api-provider-settings.spec.ts",
        "frontend/artifacts/neurostack/playwright-report/index.html",
        "frontend/artifacts/neurostack/test-results/run/trace.zip",
        "frontend/lib/api-client-react/tsconfig.tsbuildinfo",
        "frontend/lib/api-client-react/src/index.js",
        "frontend/lib/api-client-react/src/generated/api.js",
        "frontend/attached_assets/Pasted-notes_1775454231964.txt",
    ],
)
def test_frontend_generated_and_test_files_stay_out_of_the_context(
    path: str,
) -> None:
    assert docker_ignores(path)


@pytest.mark.parametrize(
    ("path", "ignored"),
    [
        ("lib/module.py", True),
        ("frontend/lib/api-client-react/src/index.ts", False),
        ("dist/package.whl", True),
        ("README.md", False),
        ("CHANGELOG.md", True),
        ("docs/guide.md", False),
        ("packages/neurostack-core/README.md", False),
        ("__pycache__/module.cpython-311.pyc", True),
        # Root-anchored: a nested __pycache__ is NOT excluded by Docker.
        ("src/api/__pycache__/main.cpython-311.pyc", False),
        ("src/api/main.py", False),
        (".git/HEAD", True),
    ],
)
def test_docker_matcher_is_root_anchored_unlike_gitignore(
    path: str, ignored: bool
) -> None:
    assert docker_ignores(path) is ignored
