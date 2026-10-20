"""Static contract for the production Dockerfile: the frontend-build stage, the
digest-pinned base images, the Poetry install, the compiler-free runtime stage
and the revision label.

Everything here is static analysis of ``Dockerfile``, ``.dockerignore``,
``frontend/package.json``, ``poetry.lock`` and ``git ls-files``: no Docker
daemon is needed, so none of these assertions proves that an image builds or
what it contains at runtime. ``scripts/production_image_smoke.py`` (and
``tests/scripts/test_production_image_smoke.py`` on a Docker host) does.
"""

from __future__ import annotations

import itertools
import json
import re
import shlex
import subprocess
import tomllib
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from scripts.production_image_smoke import is_build_package

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
FRONTEND_PACKAGE = ROOT / "frontend" / "package.json"
NEUROSTACK = ROOT / "frontend" / "artifacts" / "neurostack"
VITE_CONFIG = NEUROSTACK / "vite.config.ts"
NEUROSTACK_PACKAGE = NEUROSTACK / "package.json"
WORKFLOWS = ROOT / ".github" / "workflows"
LOCK = ROOT / "poetry.lock"

EXPECTED_STAGES = ["base", "deps", "development", "frontend-build", "production"]
NODE_IMAGE = re.compile(
    r"^node:\d+\.\d+\.\d+-[a-z0-9.-]+@sha256:[0-9a-f]{64}$"  # tag AND digest
)
PYTHON_IMAGE = re.compile(
    r"^python:3\.11\.\d+-slim-[a-z]+@sha256:[0-9a-f]{64}$"  # tag AND digest
)
DIGEST_PINNED = re.compile(r"^[a-z0-9._/-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$")
# Poetry runs from its own venv, outside every path the production stage copies
# from deps, and is the version that writes poetry.lock and that CI installs.
POETRY_VENV = "/opt/poetry"
POETRY = f"{POETRY_VENV}/bin/poetry"
BASE_REQUIREMENTS = ROOT / "scripts" / "requirements-production-base.txt"
POETRY_REQUIREMENTS = ROOT / "scripts" / "requirements-poetry.txt"
POETRY_REQUIREMENTS_IN = ROOT / "scripts" / "requirements-poetry.in"
BASE_REQUIREMENTS_PATH = "/opt/requirements/production-base.txt"
POETRY_REQUIREMENTS_PATH = "/opt/requirements/poetry.txt"
REQUIREMENT_PATHS = {BASE_REQUIREMENTS_PATH, POETRY_REQUIREMENTS_PATH}
# Every pip install downloads only hash-checked wheels named in a file.
HASHED_OPTIONS = ["--require-hashes", "--only-binary", ":all:"]
HASHED_INSTALL = f"install {' '.join(HASHED_OPTIONS)} -r"
DEPS_RUNS = [
    f"python -m venv {POETRY_VENV}"
    f" && {POETRY_VENV}/bin/pip {HASHED_INSTALL} {BASE_REQUIREMENTS_PATH}"
    f" && {POETRY_VENV}/bin/pip {HASHED_INSTALL} {POETRY_REQUIREMENTS_PATH}",
    f"{POETRY} config virtualenvs.create false",
    f"{POETRY} install --no-interaction --no-ansi --only main --no-root",
]
# A few of Poetry's own dependencies: the hash-locked file is its whole closure.
POETRY_CLOSURE = {"poetry", "poetry-core", "dulwich", "virtualenv", "keyring", "cleo"}
DEVELOPMENT_RUNS = [f"{POETRY} install --no-interaction --no-ansi --no-root"]
# The base interpreter's own packages, raised to their poetry.lock versions.
BASE_TOOLING = ("pip", "setuptools", "packaging")
REVISION_ARG = "VCS_REF=unknown"
REVISION_LABEL = 'org.opencontainers.image.revision="${VCS_REF}"'
PNPM_PIN = re.compile(r"^\d+\.\d+\.\d+\+sha512\.[0-9a-f]{128}$")
SPA_SOURCE = "/frontend/artifacts/neurostack/dist/public"
NO_SOURCEMAPS = (
    "test -z \"$(find artifacts/neurostack/dist/public -name '*.map' -print -quit)\""
)
# An inline map (`--sourcemap inline`) is a data: URL inside a JS chunk, not a
# .map file: only the sourceMappingURL comment gives it away.
NO_INLINE_SOURCEMAPS = "! grep -rqs sourceMappingURL artifacts/neurostack/dist/public"
# A map under another name (`sourcemap: "hidden"` plus `sourcemapFileNames`
# renaming it to .json/.txt) is neither a .map file nor referenced by a
# sourceMappingURL comment: its raw sourcesContent key still gives it away.
NO_EMBEDDED_SOURCEMAPS = "! grep -rqs sourcesContent artifacts/neurostack/dist/public"
# Vite copies public/ into dist/public verbatim: a TypeScript source landing
# there (or anywhere in the output) ships as-is, with no map to give it away.
NO_TYPESCRIPT_SOURCES = (
    'test -z "$(find artifacts/neurostack/dist/public'
    " \\( -name '*.ts' -o -name '*.tsx' -o -name '*.mts' \\) -print -quit)\""
)
# The whole build RUN, step by step: any extra flag (`-- --sourcemap inline`)
# or masking operator (`|| true`, `;`) changes it and fails the contract.
BUILD_RUN_STEPS = [
    'PORT="${FRONTEND_PORT}" BASE_PATH="${FRONTEND_BASE_PATH}" NODE_ENV=production'
    " pnpm --filter @workspace/neurostack run build",
    "test -s artifacts/neurostack/dist/public/index.html",
    NO_SOURCEMAPS,
    NO_INLINE_SOURCEMAPS,
    NO_EMBEDDED_SOURCEMAPS,
    NO_TYPESCRIPT_SOURCES,
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
    "LABEL",
    "CMD",
}
# Instructions the frontend-build stage may use (FROM opens the stage and is
# pinned by EXPECTED_STAGES). SHELL could mask every exit status, ADD could
# fetch remote files, ONBUILD/heredocs hide instructions: all fail.
FRONTEND_BUILD_KEYWORDS = {"ARG", "ENV", "WORKDIR", "COPY", "RUN"}
# Every WORKDIR, COPY (shlex tokens: flags, sources, destination) and RUN
# (&&-separated steps) of frontend-build, in order. Nothing else may write
# into the workspace, so nothing reaches artifacts/neurostack/public (copied
# verbatim into dist/public) or dist/ except through the guarded build.
FRONTEND_BUILD_SEQUENCE = [
    ("RUN", "corepack enable", 'corepack prepare "pnpm@${PNPM_PIN}" --activate'),
    ("WORKDIR", "/frontend"),
    (
        "COPY",
        "frontend/package.json",
        "frontend/pnpm-lock.yaml",
        "frontend/pnpm-workspace.yaml",
        "frontend/.npmrc",
        "./",
    ),
    ("COPY", "frontend/lib/api-client-react/package.json", "lib/api-client-react/"),
    ("COPY", "frontend/scripts/package.json", "scripts/"),
    (
        "COPY",
        "frontend/artifacts/mockup-sandbox/package.json",
        "artifacts/mockup-sandbox/",
    ),
    ("COPY", "frontend/artifacts/neurostack/package.json", "artifacts/neurostack/"),
    (
        "RUN",
        "pnpm install --frozen-lockfile --ignore-scripts"
        ' --filter "@workspace/neurostack..."',
    ),
    ("COPY", "frontend/tsconfig.base.json", "frontend/tsconfig.json", "./"),
    ("COPY", "frontend/lib", "./lib"),
    (
        "COPY",
        "frontend/attached_assets/logo_png_1775411054369.png",
        "./attached_assets/",
    ),
    ("COPY", "frontend/artifacts/neurostack", "./artifacts/neurostack"),
    ("RUN", *BUILD_RUN_STEPS),
]
# Everything the production lineage may copy, per source stage (None = the
# build context). Anything else (e.g. `COPY . .`) fails the contract.
PRODUCTION_COPY_SOURCES = {
    None: [
        "scripts/requirements-production-base.txt",
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
# The production stage starts afresh from the base pin (never from a stage)
# and runs exactly these RUNs, in order: the runtime system packages, the
# base tooling at the lock versions (before the deps site-packages are copied
# over it) and the non-root user, both identical to base's.
RUNTIME_PACKAGES = ["curl"]
APT_OPTIONS = ["-y", "--no-install-recommends"]
APT_RUN_STEPS = [
    "apt-get update",
    f"apt-get install {' '.join(APT_OPTIONS)} {' '.join(RUNTIME_PACKAGES)}",
    "rm -rf /var/lib/apt/lists/*",
]
BASE_TOOLING_RUN = f"pip {HASHED_INSTALL} {BASE_REQUIREMENTS_PATH}"
USER_RUN = (
    "groupadd --gid 1000 neurostack"
    " && useradd --uid 1000 --gid 1000 --shell /bin/bash --create-home neurostack"
)
PRODUCTION_RUNS = [" && ".join(APT_RUN_STEPS), BASE_TOOLING_RUN, USER_RUN]
PRODUCTION_SETTINGS = {
    "USER": ["neurostack"],
    "WORKDIR": ["/app"],
    "EXPOSE": ["8000"],
    "HEALTHCHECK": [
        "--interval=30s --timeout=10s --start-period=5s --retries=3"
        " CMD curl -f http://localhost:8000/health || exit 1"
    ],
    "CMD": [
        '["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000",'
        ' "--workers", "4", "--no-access-log"]'
    ],
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


def _parse_stages(text: str) -> tuple[Stage, ...]:
    stages: list[Stage] = []
    for instruction in _instructions(text):
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


@cache
def _stages() -> tuple[Stage, ...]:
    return _parse_stages(DOCKERFILE.read_text(encoding="utf-8"))


def _stage(alias: str, stages: tuple[Stage, ...] | None = None) -> Stage:
    stages = _stages() if stages is None else stages
    return next(stage for stage in stages if stage.alias == alias)


def _lineage(alias: str, stages: tuple[Stage, ...] | None = None) -> list[Stage]:
    stages = _stages() if stages is None else stages
    aliases = {stage.alias for stage in stages}
    chain = [_stage(alias, stages)]
    while chain[-1].parent in aliases:
        chain.append(_stage(chain[-1].parent, stages))
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
    base = _stage("base").parent
    assert re.search(rf"^FROM {re.escape(base)} AS production$", text, re.MULTILINE)
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


def test_python_image_is_pinned_by_tag_and_digest() -> None:
    base = _stage("base")
    assert PYTHON_IMAGE.match(base.parent), base.parent
    python_stages = [s for s in _stages() if s.parent.startswith("python")]
    assert python_stages == [base, _stage("production")]
    assert {stage.parent for stage in python_stages} == {base.parent}


def test_every_external_base_image_is_pinned_by_digest() -> None:
    aliases = {stage.alias for stage in _stages()}
    external = [stage.parent for stage in _stages() if stage.parent not in aliases]
    # python (base and production, one pin) and node (frontend-build)
    assert len(external) == 3 and len(set(external)) == 2, external
    for parent in external:
        assert DIGEST_PINNED.match(parent), f"base image not pinned by digest: {parent}"


def _base_from_comment(text: str) -> tuple[str, str]:
    """The base FROM image and the comment block directly above it."""

    base = _stage("base", _parse_stages(text)).parent
    from_line = f"FROM {base} AS base"
    return base, text[: text.index(from_line)].rsplit("\n\n", 1)[-1]


def _assert_python_pin_matches_its_record(text: str) -> None:
    # The comment above the FROM records the floating tag that was resolved,
    # the exact tag it resolved to, the multi-arch INDEX digest and the
    # linux/amd64 manifest digest, so a reviewer can re-run the resolution.
    base, comments = _base_from_comment(text)
    flat = " ".join(line.lstrip("#").strip() for line in comments.splitlines())
    record = re.search(
        r"the floating tag (?P<floating>python:\S+) was (?P<tag>python:\S+?),"
        r" index sha256:(?P<index>[0-9a-f]{64}),",
        flat,
    )
    manifest = re.search(
        r"linux/amd64 manifest is sha256:(?P<amd64>[0-9a-f]{64})", flat
    )
    assert record and manifest, f"base image record missing: {flat}"
    tag, _, digest = base.partition("@")
    assert record["floating"] == "python:3.11-slim", record["floating"]
    assert tag == record["tag"], f"FROM tag {tag} differs from the record"
    assert digest == f"sha256:{record['index']}", "FROM digest is not the index digest"
    assert record["index"] != manifest["amd64"], "index and manifest digests coincide"


def test_python_pin_is_the_recorded_tag_and_index_digest() -> None:
    _assert_python_pin_matches_its_record(DOCKERFILE.read_text(encoding="utf-8"))


_INDEX = "e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce"
_AMD64 = "e529028263dbe6910a2d96f7d2b8f5266385e917fd45d286ef166977c094a51e"
_PY_FROM = f"FROM python:3.11.17-slim-trixie@sha256:{_INDEX} AS base"


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (_PY_FROM, _PY_FROM.replace(_INDEX, _AMD64), "not the index digest"),
        (
            _PY_FROM,
            _PY_FROM.replace("3.11.17-slim-trixie", "3.11.18-slim-trixie"),
            "differs from the record",
        ),
        (
            f"# sha256:{_INDEX},",
            f"# sha256:{_AMD64},",
            "not the index digest",
        ),
        (f"# sha256:{_AMD64}.", f"# sha256:{_INDEX}.", "digests coincide"),
        ("# python:3.11-slim was", "# python:3.12-slim was", "python:3.12-slim"),
    ],
    ids=["manifest-digest", "tag-drift", "record-drift", "same-digests", "floating"],
)
def test_python_pin_record_rejects_mutations(old: str, new: str, message: str) -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    with pytest.raises(AssertionError, match=re.escape(message)):
        _assert_python_pin_matches_its_record(text.replace(old, new))


def _runs(alias: str, stages: tuple[Stage, ...] | None = None) -> list[str]:
    stage = _stage(alias, stages)
    return [" ".join(i.args.split()) for i in stage.instructions if i.keyword == "RUN"]


def _lock_writer() -> str:
    text = LOCK.read_text(encoding="utf-8")
    writer = re.match(r"# This file is automatically @generated by Poetry (\S+) ", text)
    assert writer, text.splitlines()[0]
    return writer.group(1)


def _lock_files() -> dict[tuple[str, str], set[str]]:
    lock = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    return {
        (_normalise(package["name"]), package["version"]): {
            entry["hash"].removeprefix("sha256:") for entry in package["files"]
        }
        for package in lock["package"]
    }


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def hashed_requirements(text: str) -> dict[str, tuple[str, list[str]]]:
    """Name -> (version, sha256 list) of a hash-pinned pip requirements file."""

    requirements: dict[str, tuple[str, list[str]]] = {}
    for raw in re.sub(r"\\\n", " ", text).splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        spec, *options = line.split()
        pinned = re.fullmatch(
            r"([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9.+!_-]+)", spec
        )
        assert pinned, f"requirement not pinned exactly: {line}"
        hashes = []
        for option in options:
            digest = option.removeprefix("--hash=sha256:")
            valid = option.startswith("--hash=") and re.fullmatch(
                r"[0-9a-f]{64}", digest
            )
            assert valid, f"unexpected requirement option: {option}"
            hashes.append(digest)
        assert hashes, f"requirement without hash: {spec}"
        name = _normalise(pinned.group(1))
        assert name not in requirements, f"duplicate requirement: {name}"
        requirements[name] = (pinned.group(2), hashes)
    return requirements


def _assert_base_requirements_are_locked_wheels(text: str) -> None:
    requirements = hashed_requirements(text)
    assert sorted(requirements) == sorted(BASE_TOOLING), f"base tooling: {requirements}"
    files = _lock_files()
    for name, (version, hashes) in requirements.items():
        locked = files.get((name, version))
        assert locked is not None, f"{name}=={version} is not the locked version"
        for digest in hashes:
            assert (
                digest in locked
            ), f"{name}=={version} hash not in poetry.lock: {digest}"


def test_base_requirements_are_the_locked_versions_and_hashes() -> None:
    # The production stage merges deps' site-packages over base's: a base
    # package that deps replaced would leave its old metadata in the image.
    _assert_base_requirements_are_locked_wheels(
        BASE_REQUIREMENTS.read_text(encoding="utf-8")
    )


def _assert_poetry_requirements(text: str, requirements_in: str) -> None:
    requirements = hashed_requirements(text)
    writer = _lock_writer()
    assert requirements.get("poetry", ("",))[0] == writer, "Poetry pin differs"
    pins = [
        line.strip()
        for line in requirements_in.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert pins == [f"poetry=={writer}"], f"requirements-poetry.in: {pins}"
    header = " ".join(
        line
        for line in itertools.takewhile(
            lambda row: row.startswith("#"), text.splitlines()
        )
    )
    for token in (
        "uv pip compile scripts/requirements-poetry.in",
        "--python-version 3.11",
        "--python-platform x86_64-unknown-linux-gnu",
        "--generate-hashes",
        "--exclude-newer ",
    ):
        assert token in header, f"regeneration command lacks {token}"
    assert POETRY_CLOSURE <= set(requirements), POETRY_CLOSURE - set(requirements)


def test_poetry_requirements_are_fully_hash_pinned() -> None:
    _assert_poetry_requirements(
        POETRY_REQUIREMENTS.read_text(encoding="utf-8"),
        POETRY_REQUIREMENTS_IN.read_text(encoding="utf-8"),
    )


_BASE_FILE = "packaging==26.2 \\\n    --hash=sha256:"
_HASH = "5fc45236b9446107ff2415ce77c807cee2862cb6fac22b8a73826d0693b0980e"
_POETRY_PIN = "poetry==2.4.1 \\\n"
_DULWICH = "dulwich==1.2.17 \\\n"


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (f" \\\n    --hash=sha256:{_HASH}", "", "requirement without hash"),
        (_HASH, _HASH[:-1] + "0", "hash not in poetry.lock"),
        ("packaging==26.2 ", "packaging==26.3 ", "is not the locked version"),
        ("packaging==26.2 ", "packaging>=26.2 ", "not pinned exactly"),
        ("pip==26.2.1 ", "pip==26.2.1 --no-deps ", "unexpected requirement option"),
        ("setuptools==84.0.0 \\", "wheel==0.46.3 \\", "base tooling"),
    ],
    ids=["hash-removed", "wrong-hash", "version-drift", "unpinned", "option", "swap"],
)
def test_base_requirements_contract_rejects_mutations(
    old: str, new: str, message: str
) -> None:
    text = BASE_REQUIREMENTS.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    with pytest.raises(AssertionError, match=re.escape(message)):
        _assert_base_requirements_are_locked_wheels(text.replace(old, new))


def _strip_hashes(text: str, requirement: str) -> str:
    start = text.index(requirement)
    end = text.index("\n    # via", start)
    return text[:start] + requirement.replace(" \\\n", "\n") + text[end + 1 :]


@pytest.mark.parametrize(
    ("mutate", "requirements_in", "message"),
    [
        (lambda t: _strip_hashes(t, _DULWICH), None, "requirement without hash"),
        (lambda t: t.replace(_POETRY_PIN, "poetry==2.4.0 \\\n"), None, "Poetry pin"),
        (lambda t: t.replace(_DULWICH, "dulwich>=1.2 \\\n"), None, "not pinned"),
        (lambda t: t, "poetry==2.4.0\n", "requirements-poetry.in"),
        (lambda t: t, "poetry\n", "requirements-poetry.in"),
        (lambda t: t.replace("--generate-hashes ", ""), None, "--generate-hashes"),
        (lambda t: t + "requests==2.34.2\n", None, "requirement without hash"),
    ],
    ids=[
        "unhashed-transitive",
        "pin-differs",
        "unpinned-transitive",
        "input-differs",
        "input-unpinned",
        "no-hash-generation",
        "unhashed-extra",
    ],
)
def test_poetry_requirements_contract_rejects_mutations(
    mutate: Any, requirements_in: str | None, message: str
) -> None:
    text = POETRY_REQUIREMENTS.read_text(encoding="utf-8")
    source = POETRY_REQUIREMENTS_IN.read_text(encoding="utf-8")
    with pytest.raises(AssertionError, match=re.escape(message)):
        _assert_poetry_requirements(mutate(text), requirements_in or source)


def _assert_poetry_install(stages: tuple[Stage, ...]) -> None:
    assert _runs("deps", stages) == DEPS_RUNS, "deps Poetry RUNs"
    assert _runs("development", stages) == DEVELOPMENT_RUNS, "development RUNs"
    steps = re.split(r"\s*&&\s*", _runs("deps", stages)[0])
    assert steps[1].endswith(BASE_REQUIREMENTS_PATH), "venv pip not upgraded first"
    deps = _stage("deps", stages)
    copies = [(c.sources, c.destination) for c in _copies(deps)]
    expected = (["scripts/requirements-poetry.txt"], POETRY_REQUIREMENTS_PATH)
    assert copies[0] == expected, f"Poetry requirements COPY: {copies[0]}"
    keywords = [i.keyword for i in deps.instructions]
    assert keywords.index("COPY") < keywords.index("RUN"), "requirements copied late"


def test_poetry_is_installed_from_hashes_and_installs_only_the_main_group() -> None:
    _assert_poetry_install(_stages())


def _assert_every_pip_install_is_hash_checked(stages: tuple[Stage, ...]) -> None:
    installs = [
        (stage.alias, pip, options, requirements)
        for stage in stages
        for run in _runs(stage.alias, stages)
        for pip, options, requirements in _pip_installs(run)
    ]
    assert len(installs) == 4, f"pip installs: {installs}"
    for alias, pip, options, requirements in installs:
        assert "--require-hashes" in options, f"pip without --require-hashes: {pip}"
        assert options == [*HASHED_OPTIONS, "-r"], f"pip options in {alias}: {options}"
        assert len(requirements) == 1, f"pip requirements in {alias}: {requirements}"
        assert requirements[0] in REQUIREMENT_PATHS, requirements
    base = [c for c in _copies(_stage("base", stages))]
    assert [(c.sources, c.destination) for c in base] == [
        (["scripts/requirements-production-base.txt"], BASE_REQUIREMENTS_PATH)
    ], "base requirements COPY"
    assert _runs("base", stages)[1] == BASE_TOOLING_RUN, "base tooling RUN"
    # The runtime stage repeats exactly that install (see _assert_runtime_stage
    # for where it must sit).
    base_file = "scripts/requirements-production-base.txt"
    runtime = [
        (c.flags, c.sources, c.destination)
        for c in _copies(_stage("production", stages))
        if base_file in c.sources
    ]
    assert runtime == [
        ({}, [base_file], BASE_REQUIREMENTS_PATH)
    ], "production base requirements COPY"
    runs = _runs("production", stages)
    assert runs.count(BASE_TOOLING_RUN) == 1, "production tooling RUN"


def test_every_pip_install_is_hash_checked() -> None:
    _assert_every_pip_install_is_hash_checked(_stages())


def test_ci_installs_the_same_poetry_as_the_image() -> None:
    pins = {
        (path.name, version)
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
        for version in re.findall(
            r"poetry==([0-9][^\s\"']*)", path.read_text(encoding="utf-8")
        )
    }
    assert pins, "no workflow installs Poetry"
    assert {version for _, version in pins} == {_lock_writer()}, pins


def _assert_poetry_stays_out_of_production(stages: tuple[Stage, ...]) -> None:
    for stage in _lineage("production", stages):
        for instruction in stage.instructions:
            assert POETRY_VENV not in instruction.args, f"Poetry venv: {instruction}"
            assert "poetry" not in instruction.args.lower(), f"Poetry: {instruction}"
    for copy in _copies(_stage("production", stages)):
        if copy.flags.get("from") == "deps":
            for source in copy.sources:
                assert not POETRY_VENV.startswith(source.rstrip("/") + "/"), source
    # Only the venv's own pip may install Poetry: the system pip would put it
    # (and its dependencies) into the site-packages the production stage copies.
    for stage in stages:
        for run in _runs(stage.alias, stages):
            for pip, _, requirements in _pip_installs(run):
                names = {re.split(r"[=<>!~\[ ]", r, 1)[0] for r in requirements}
                if POETRY_REQUIREMENTS_PATH in requirements:
                    names.add("poetry")
                if "poetry" in names and pip != f"{POETRY_VENV}/bin/pip":
                    raise AssertionError(f"system pip installs Poetry: {run}")


def _pip_installs(run: str) -> list[tuple[str, list[str], list[str]]]:
    """(pip, options, other arguments) of every ``pip install`` step of a RUN."""

    installs: list[tuple[str, list[str], list[str]]] = []
    for step in re.split(r"\s*&&\s*", run):
        tokens = shlex.split(step)
        if tokens[:3] == ["python", "-m", "pip"]:
            pip, rest = "python -m pip", tokens[3:]
        elif tokens and re.fullmatch(r"pip[0-9.]*", tokens[0].rsplit("/", 1)[-1]):
            pip, rest = tokens[0], tokens[1:]
        else:
            continue
        if rest[:1] != ["install"]:
            continue
        options: list[str] = []
        arguments: list[str] = []
        for token in rest[1:]:
            if token.startswith("-"):
                options.append(token)
            elif options and options[-1] == "--only-binary" and token == ":all:":
                options.append(token)
            else:
                arguments.append(token)
        installs.append((pip, options, arguments))
    return installs


def test_poetry_never_reaches_the_production_image() -> None:
    _assert_poetry_stays_out_of_production(_stages())


_VENV_POETRY = (
    "/opt/poetry/bin/pip install --require-hashes --only-binary :all: "
    "-r /opt/requirements/poetry.txt"
)
_VENV_PIP = (
    "/opt/poetry/bin/pip install --require-hashes --only-binary :all: "
    "-r /opt/requirements/production-base.txt"
)
_PIP = (
    "RUN pip install --require-hashes --only-binary :all: "
    "-r /opt/requirements/production-base.txt\n"
)
# The same RUN opens base's and production's tooling: anchored by what follows.
_BASE_PIP = _PIP + "\n# Create non-root user for security\n"
_RUNTIME_PIP = _PIP + "\n# Non-root user, same ids as base.\n"
_MAIN_INSTALL = (
    "RUN /opt/poetry/bin/poetry install --no-interaction --no-ansi --only main "
    "--no-root\n"
)
_SPA_ENV = "ENV SPA_DIST_DIR=/app/spa\n"
_POETRY_COPY = "COPY scripts/requirements-poetry.txt /opt/requirements/poetry.txt\n"


@pytest.mark.parametrize(
    ("old", "new", "check", "message"),
    [
        (
            _MAIN_INSTALL,
            _MAIN_INSTALL.replace(" --only main", ""),
            _assert_poetry_install,
            "deps Poetry RUNs",
        ),
        (
            _MAIN_INSTALL,
            _MAIN_INSTALL.replace("--only main", "--with dev"),
            _assert_poetry_install,
            "deps Poetry RUNs",
        ),
        (
            f"    && {_VENV_PIP} \\\n",
            "",
            _assert_poetry_install,
            "deps Poetry RUNs",
        ),
        (
            _POETRY_COPY,
            _POETRY_COPY.replace("/opt/requirements/poetry.txt", "/tmp/poetry.txt"),
            _assert_poetry_install,
            "Poetry requirements COPY",
        ),
        (
            _VENV_POETRY,
            _VENV_POETRY.replace("--require-hashes ", ""),
            _assert_every_pip_install_is_hash_checked,
            "pip without --require-hashes",
        ),
        (
            _BASE_PIP,
            _BASE_PIP.replace("--require-hashes ", ""),
            _assert_every_pip_install_is_hash_checked,
            "pip without --require-hashes",
        ),
        (
            _VENV_POETRY,
            _VENV_POETRY.replace(":all: ", ":all: poetry==2.4.1 "),
            _assert_every_pip_install_is_hash_checked,
            "pip requirements in deps",
        ),
        (
            _BASE_PIP,
            _BASE_PIP.replace("--only-binary :all: ", ""),
            _assert_every_pip_install_is_hash_checked,
            "pip options in base",
        ),
        (
            _RUNTIME_PIP,
            _RUNTIME_PIP.replace("--require-hashes ", ""),
            _assert_every_pip_install_is_hash_checked,
            "pip without --require-hashes",
        ),
        (
            _RUNTIME_PIP,
            _RUNTIME_PIP.replace("--only-binary :all: ", ""),
            _assert_every_pip_install_is_hash_checked,
            "pip options in production",
        ),
        (
            _RUNTIME_PIP,
            _RUNTIME_PIP.replace("production-base.txt", "poetry.txt"),
            _assert_poetry_stays_out_of_production,
            "Poetry: ",
        ),
        (
            _RUNTIME_PIP,
            _RUNTIME_PIP.replace(":all: ", ":all: urllib3==2.8.0 "),
            _assert_every_pip_install_is_hash_checked,
            "pip requirements in production",
        ),
        (
            _SPA_ENV,
            _SPA_ENV + "COPY --from=deps /opt/poetry /opt/poetry\n",
            _assert_poetry_stays_out_of_production,
            "Poetry venv",
        ),
        (
            _SPA_ENV,
            _SPA_ENV + "COPY --from=deps /opt/poetry/lib/python3.11/site-packages "
            "/usr/local/lib/python3.11/site-packages\n",
            _assert_poetry_stays_out_of_production,
            "Poetry venv",
        ),
        (
            _VENV_POETRY,
            _VENV_POETRY.replace("/opt/poetry/bin/pip", "pip"),
            _assert_poetry_stays_out_of_production,
            "system pip installs Poetry",
        ),
        (
            _MAIN_INSTALL,
            "RUN pip install poetry==2.4.1\n" + _MAIN_INSTALL,
            _assert_poetry_stays_out_of_production,
            "system pip installs Poetry",
        ),
    ],
    ids=[
        "dev-group-installed",
        "with-dev",
        "pip-not-upgraded-first",
        "requirements-copied-elsewhere",
        "poetry-require-hashes-dropped",
        "base-require-hashes-dropped",
        "unhashed-inline-requirement",
        "base-only-binary-dropped",
        "runtime-require-hashes-dropped",
        "runtime-only-binary-dropped",
        "runtime-system-pip-installs-poetry-file",
        "runtime-unhashed-inline-requirement",
        "poetry-venv-in-final-stage",
        "poetry-site-packages-in-final-stage",
        "system-pip-installs-poetry-file",
        "system-pip-poetry-extra-run",
    ],
)
def test_poetry_contract_rejects_mutations(
    old: str, new: str, check: Any, message: str
) -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    with pytest.raises(AssertionError, match=re.escape(message)):
        check(_parse_stages(text.replace(old, new)))


def _production_labels(stages: tuple[Stage, ...]) -> list[str]:
    return [
        instruction.args
        for stage in _lineage("production", stages)
        for instruction in stage.instructions
        if instruction.keyword == "LABEL"
    ]


def _assert_revision_label(stages: tuple[Stage, ...]) -> None:
    production = _stage("production", stages).instructions
    keywords = [instruction.keyword for instruction in production]
    assert _production_labels(stages) == [REVISION_LABEL], "revision label"
    args = [i.args for i in production if i.keyword == "ARG"]
    assert args == [REVISION_ARG], f"revision ARG: {args}"
    label = keywords.index("LABEL")
    assert keywords.index("ARG") < label, "revision ARG must precede the LABEL"
    last_copy = max(i for i, keyword in enumerate(keywords) if keyword == "COPY")
    # A changing revision must never invalidate a cached COPY layer.
    assert last_copy < label, "revision label must follow every COPY"
    # VCS_REF only feeds the final label: used by any earlier instruction it
    # would invalidate cached layers or leak into what was built.
    users = [
        (stage.alias, instruction.keyword)
        for stage in stages
        for instruction in stage.instructions
        if REVISION_ARG.partition("=")[0] in instruction.args
    ]
    assert users == [
        ("production", "ARG"),
        ("production", "LABEL"),
    ], f"VCS_REF used outside the final ARG and LABEL: {users}"


def test_production_image_carries_the_revision_label() -> None:
    _assert_revision_label(_stages())


_LABEL = 'LABEL org.opencontainers.image.revision="${VCS_REF}"\n'


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (_LABEL, "", "revision label"),
        (_LABEL, _LABEL.replace("revision", "version"), "revision label"),
        (_LABEL, _LABEL + 'LABEL extra="x"\n', "revision label"),
        ("ARG VCS_REF=unknown\n", "ARG VCS_REF\n", "revision ARG"),
        ("ARG VCS_REF=unknown\n", "", "revision ARG"),
        (
            "ENV SPA_DIST_DIR=/app/spa\n",
            "ENV SPA_DIST_DIR=/app/spa\n" + _LABEL.replace("VCS_REF", "X"),
            "revision label",
        ),
        (
            "ARG VCS_REF=unknown\n",
            "ARG VCS_REF=unknown\nENV REVISION=${VCS_REF}\n",
            "VCS_REF used outside",
        ),
        (
            "RUN /opt/poetry/bin/poetry config virtualenvs.create false\n",
            "ARG VCS_REF\nRUN /opt/poetry/bin/poetry config virtualenvs.create false"
            " && echo $VCS_REF\n",
            "VCS_REF used outside",
        ),
    ],
    ids=[
        "missing",
        "wrong-key",
        "extra-label",
        "no-default",
        "no-arg",
        "second-label",
        "used-before-label",
        "used-in-deps",
    ],
)
def test_revision_label_contract_rejects_mutations(
    old: str, new: str, message: str
) -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    with pytest.raises(AssertionError, match=re.escape(message)):
        _assert_revision_label(_parse_stages(text.replace(old, new)))


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


def _build_run(stages: tuple[Stage, ...] | None = None) -> str:
    frontend = _stage("frontend-build", stages)
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
    assert NO_EMBEDDED_SOURCEMAPS in build


def _build_steps(build: str) -> list[str]:
    return [step.strip() for step in re.split(r"\s*&&\s*", build)]


def test_build_run_guards_cannot_be_masked_or_bypassed() -> None:
    build = _build_run()
    assert "--sourcemap" not in build, f"build RUN enables sourcemaps: {build}"
    # Every step must gate the layer: `|| true`, `;`, `&` or a pipe would let a
    # failing guard pass silently.
    steps = _build_steps(build)
    for step in steps:
        for operator in ("||", ";", "&", "|", "`"):
            assert operator not in step, f"{operator!r} masks a guard in: {step}"
    assert steps == BUILD_RUN_STEPS


def test_index_html_check_precedes_every_grep_guard() -> None:
    # `! grep -rqs` also succeeds when dist/public is missing (grep rc=2 is
    # negated to success): only the earlier `test -s .../index.html` step
    # stops a build that wrote nothing from passing the guards vacuously.
    steps = _build_steps(_build_run())
    index_check = steps.index("test -s artifacts/neurostack/dist/public/index.html")
    greps = [i for i, step in enumerate(steps) if "grep" in step]
    assert greps and index_check < min(greps), steps
    assert index_check == 1, steps


def _assert_frontend_build_is_sealed_by_the_guarded_build(
    stages: tuple[Stage, ...],
) -> None:
    frontend = _stage("frontend-build", stages)
    for instruction in frontend.instructions:
        allowed = instruction.keyword in FRONTEND_BUILD_KEYWORDS
        assert allowed, f"instruction not allowed in frontend-build: {instruction}"
        assert "<<" not in instruction.args, f"heredoc in frontend-build: {instruction}"
    # The guards only prove what dist/public held when they ran: anything
    # after them (a second `vite build --sourcemap inline`, a COPY into
    # dist/public, even ENV/LABEL) would reach /app/spa unchecked.
    last = frontend.instructions[-1]
    sealed = last.keyword == "RUN" and last.args == _build_run(stages)
    assert sealed, f"guarded build RUN is not the last instruction: {last}"
    assert _build_steps(last.args) == BUILD_RUN_STEPS
    sequence = _workspace_writes(frontend)
    assert sequence == FRONTEND_BUILD_SEQUENCE, (
        "frontend-build WORKDIR/COPY/RUN sequence differs from the allow-list: "
        f"{[step for step in sequence if step not in FRONTEND_BUILD_SEQUENCE]}"
    )


def _workspace_writes(stage: Stage) -> list[tuple[str, ...]]:
    sequence: list[tuple[str, ...]] = []
    for instruction in stage.instructions:
        if instruction.keyword == "RUN":
            sequence.append(("RUN", *_build_steps(instruction.args)))
        elif instruction.keyword == "COPY":
            sequence.append(("COPY", *shlex.split(instruction.args)))
        elif instruction.keyword == "WORKDIR":
            sequence.append(("WORKDIR", instruction.args))
    return sequence


def test_guarded_build_run_is_the_last_instruction_of_frontend_build() -> None:
    _assert_frontend_build_is_sealed_by_the_guarded_build(_stages())


_GUARD_TAIL = BUILD_RUN_STEPS[-1] + "\n"
_SOURCE_COPY = "COPY frontend/artifacts/neurostack ./artifacts/neurostack\n"
_SEQUENCE = "sequence differs from the allow-list"
_BUILD_HEAD = 'RUN PORT="${FRONTEND_PORT}"'


@pytest.mark.parametrize(
    ("anchor", "replacement", "message"),
    [
        (
            _GUARD_TAIL,
            _GUARD_TAIL + "RUN PORT=3000 BASE_PATH=/ pnpm --filter "
            "@workspace/neurostack exec vite build --config vite.config.ts "
            "--sourcemap inline\n",
            "not the last instruction",
        ),
        (
            _GUARD_TAIL,
            _GUARD_TAIL + "COPY frontend/artifacts/neurostack/src "
            "artifacts/neurostack/dist/public/src\n",
            "not the last instruction",
        ),
        (_GUARD_TAIL, _GUARD_TAIL + "ENV NODE_OPTIONS=\n", "not the last instruction"),
        (
            _BUILD_HEAD,
            'SHELL ["/bin/sh", "-c", "eval \\"$0\\"; true"]\n' + _BUILD_HEAD,
            "not allowed in frontend-build",
        ),
        (_GUARD_TAIL, _GUARD_TAIL + 'LABEL x="y"\n', "not allowed in frontend-build"),
        (_BUILD_HEAD, "ONBUILD RUN true\n" + _BUILD_HEAD, "not allowed"),
        (_BUILD_HEAD, "ADD https://x.invalid/a.js ./\n" + _BUILD_HEAD, "not allowed"),
        (_BUILD_HEAD, "RUN <<EOF\ntrue\nEOF\n" + _BUILD_HEAD, "heredoc"),
        (
            _BUILD_HEAD,
            "COPY frontend/artifacts/neurostack/src "
            "artifacts/neurostack/public/src\n" + _BUILD_HEAD,
            _SEQUENCE,
        ),
        (
            _BUILD_HEAD,
            "COPY frontend/lib/api-client-react/src ./artifacts/neurostack/public/\n"
            + _BUILD_HEAD,
            _SEQUENCE,
        ),
        (
            _SOURCE_COPY,
            _SOURCE_COPY.replace("COPY ", "COPY --link "),
            _SEQUENCE,
        ),
        (
            _BUILD_HEAD,
            "ADD frontend/artifacts/neurostack/src artifacts/neurostack/public/\n"
            + _BUILD_HEAD,
            "not allowed in frontend-build",
        ),
        (
            _BUILD_HEAD,
            "RUN cp -r artifacts/neurostack/src artifacts/neurostack/public/\n"
            + _BUILD_HEAD,
            _SEQUENCE,
        ),
        (
            _BUILD_HEAD,
            "WORKDIR /frontend/artifacts/neurostack/public\n"
            "COPY frontend/artifacts/neurostack/src ./\n"
            "WORKDIR /frontend\n" + _BUILD_HEAD,
            _SEQUENCE,
        ),
        (
            _SOURCE_COPY,
            _SOURCE_COPY.replace("./artifacts/neurostack", "./artifacts/neurostack/"),
            _SEQUENCE,
        ),
    ],
    ids=[
        "rebuild-with-inline-maps",
        "copy-sources-into-dist",
        "env-after-guard",
        "shell-masks-exit-status",
        "label-after-guard",
        "onbuild",
        "add-remote",
        "heredoc-run",
        "copy-sources-into-vite-public",
        "copy-into-vite-public-other-destination",
        "copy-link-flag",
        "add-into-vite-public",
        "run-cp-into-vite-public",
        "workdir-redirect",
        "destination-spelling",
    ],
)
def test_frontend_build_seal_rejects_instructions_around_the_guard(
    anchor: str, replacement: str, message: str
) -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert text.count(anchor) == 1, anchor
    stages = _parse_stages(text.replace(anchor, replacement))
    with pytest.raises(AssertionError, match=re.escape(message)):
        _assert_frontend_build_is_sealed_by_the_guarded_build(stages)


def _typescript_in_vite_public(paths: frozenset[str] | set[str]) -> list[str]:
    public = f"{NEUROSTACK.relative_to(ROOT).as_posix()}/public/"
    return sorted(
        path
        for path in paths
        if path.startswith(public) and path.endswith((".ts", ".tsx", ".mts"))
    )


def test_typescript_in_vite_public_detector_flags_sources() -> None:
    artifact = NEUROSTACK.relative_to(ROOT).as_posix()
    paths = {
        f"{artifact}/public/favicon.svg",
        f"{artifact}/public/a.ts",
        f"{artifact}/public/x/b.tsx",
        f"{artifact}/public/c.mts",
        f"{artifact}/src/d.ts",
    }
    assert _typescript_in_vite_public(paths) == [
        f"{artifact}/public/a.ts",
        f"{artifact}/public/c.mts",
        f"{artifact}/public/x/b.tsx",
    ]


def test_vite_public_dir_holds_no_typescript_sources() -> None:
    public = f"{NEUROSTACK.relative_to(ROOT).as_posix()}/public/"
    assert any(
        path.startswith(public) for path in _tracked_files()
    ), "vite public/ dir vanished: update this contract"
    sources = _typescript_in_vite_public(_tracked_files())
    assert not sources, sources


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
    assert [stage.alias for stage in lineage] == ["production"]
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


def _supply_stages(stages: tuple[Stage, ...]) -> list[Stage]:
    """The production lineage and, transitively, every stage it copies from
    (deps and its base, frontend-build): whatever runs there reaches the image
    through a COPY."""

    aliases = {stage.alias for stage in stages}
    found: dict[str, Stage] = {}
    pending = ["production"]
    while pending:
        for stage in _lineage(pending.pop(), stages):
            if stage.alias in found:
                continue
            found[stage.alias] = stage
            pending += [
                copy.flags["from"]
                for copy in _copies(stage)
                if copy.flags.get("from") in aliases
            ]
    return list(found.values())


def test_supply_stages_are_the_production_closure() -> None:
    aliases = [stage.alias for stage in _supply_stages(_stages())]
    assert sorted(aliases) == ["base", "deps", "frontend-build", "production"]


def _assert_production_lineage_imports_only_through_copies(
    stages: tuple[Stage, ...],
) -> None:
    for stage in _supply_stages(stages):
        for instruction in stage.instructions:
            allowed = instruction.keyword in PRODUCTION_KEYWORDS
            assert allowed, f"instruction not allowed in {stage.alias}: {instruction}"
            assert "<<" not in instruction.args, (stage.alias, instruction)
            assert "--mount" not in instruction.args, (stage.alias, instruction)


def test_production_lineage_imports_files_only_through_the_copy_allowlist() -> None:
    # `RUN --mount=type=bind,from=frontend-build,... cp -r ...` (or a bind of
    # the build context) would import files without a COPY.
    _assert_production_lineage_imports_only_through_copies(_stages())


@pytest.mark.parametrize(
    ("anchor", "stage"),
    [
        ("# Create non-root user for security\n", "base"),
        ("# Configure Poetry to not create virtual environment\n", "deps"),
        ("# Non-root user, same ids as base.\n", "production"),
        ("ENV SPA_DIST_DIR=/app/spa\n", "production"),
    ],
)
def test_production_lineage_rejects_shell_that_masks_exit_statuses(
    anchor: str, stage: str
) -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert text.count(anchor) == 1, anchor
    shell = 'SHELL ["/bin/sh", "-c", "eval \\"$0\\"; true"]\n'
    stages = _parse_stages(text.replace(anchor, shell + anchor))
    with pytest.raises(AssertionError, match=f"not allowed in {stage}"):
        _assert_production_lineage_imports_only_through_copies(stages)


# --- Runtime stage (no compilers) ------------------------------------------


def _assert_runtime_stage_is_fresh(stages: tuple[Stage, ...]) -> None:
    aliases = {stage.alias for stage in stages}
    parent = _stage("production", stages).parent
    assert parent not in aliases, f"production builds on stage {parent}"
    base = _stage("base", stages).parent
    assert parent == base, f"production FROM {parent} is not the base pin {base}"
    assert PYTHON_IMAGE.match(parent), parent


def test_production_starts_afresh_from_the_base_pin() -> None:
    # Built on base (or deps), the image would carry build-essential and
    # libpq-dev: gcc, g++, make, binutils, cpp, pg_config and the C headers.
    _assert_runtime_stage_is_fresh(_stages())


_PROD_FROM = f"FROM python:3.11.17-slim-trixie@sha256:{_INDEX} AS production\n"


@pytest.mark.parametrize(
    ("new", "message"),
    [
        ("FROM base AS production\n", "production builds on stage base"),
        ("FROM deps AS production\n", "production builds on stage deps"),
        (_PROD_FROM.replace(_INDEX, _AMD64), "is not the base pin"),
        (_PROD_FROM.replace("3.11.17", "3.11.18"), "is not the base pin"),
        ("FROM python:3.11-slim AS production\n", "is not the base pin"),
    ],
    ids=["from-base", "from-deps", "manifest-digest", "other-tag", "unpinned"],
)
def test_runtime_stage_origin_rejects_mutations(new: str, message: str) -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert text.count(_PROD_FROM) == 1, _PROD_FROM
    with pytest.raises(AssertionError, match=re.escape(message)):
        _assert_runtime_stage_is_fresh(_parse_stages(text.replace(_PROD_FROM, new)))


def _package_run(stages: tuple[Stage, ...]) -> list[str]:
    """The &&-steps of production's one RUN that touches the package manager."""

    runs = [
        run
        for run in _runs("production", stages)
        if re.search(r"\b(?:apt|apt-get|aptitude|dpkg)\b", run)
    ]
    assert len(runs) == 1, f"production package RUNs: {runs}"
    return _build_steps(runs[0])


def _runtime_packages(stages: tuple[Stage, ...]) -> list[str]:
    installs = [
        shlex.split(step)[2:]
        for step in _package_run(stages)
        if shlex.split(step)[:2] == ["apt-get", "install"]
    ]
    assert len(installs) == 1, f"production apt-get install steps: {installs}"
    return [token for token in installs[0] if not token.startswith("-")]


def _assert_runtime_packages(stages: tuple[Stage, ...]) -> None:
    packages = _runtime_packages(stages)
    build = [package for package in packages if is_build_package(package)]
    assert not build, f"build package in production: {build}"
    steps = _package_run(stages)
    install = next(step for step in steps if step.startswith("apt-get install"))
    options = [token for token in shlex.split(install) if token.startswith("-")]
    assert options == APT_OPTIONS, f"apt-get install options: {options}"
    assert packages == RUNTIME_PACKAGES, f"runtime packages: {packages}"
    assert steps == APT_RUN_STEPS, f"production apt RUN: {steps}"


def test_runtime_stage_installs_only_the_runtime_packages() -> None:
    _assert_runtime_packages(_stages())


def _assert_healthcheck_tool_is_installed(stages: tuple[Stage, ...]) -> None:
    production = _stage("production", stages)
    checks = [
        " ".join(i.args.split())
        for i in production.instructions
        if i.keyword == "HEALTHCHECK"
    ]
    assert len(checks) == 1, f"production HEALTHCHECK: {checks}"
    _, cmd, command = checks[0].partition(" CMD ")
    assert cmd, f"HEALTHCHECK without CMD: {checks[0]}"
    argv = json.loads(command) if command.startswith("[") else shlex.split(command)
    tool = argv[0]
    assert tool in _runtime_packages(
        stages
    ), f"HEALTHCHECK runs {tool}, which no runtime package installs"


def test_healthcheck_tool_is_a_runtime_package() -> None:
    _assert_healthcheck_tool_is_installed(_stages())


def _settings(stage: Stage, keyword: str) -> list[str]:
    return [
        " ".join(i.args.split()) for i in stage.instructions if i.keyword == keyword
    ]


def _assert_runtime_matches_base(stages: tuple[Stage, ...]) -> None:
    base, production = _stage("base", stages), _stage("production", stages)
    expected_env = [*_settings(base, "ENV"), "SPA_DIST_DIR=/app/spa"]
    assert _settings(production, "ENV") == expected_env, "production ENV"
    assert USER_RUN in _runs("base", stages), "base user RUN"
    assert _runs("production", stages) == PRODUCTION_RUNS, "production RUNs"
    for keyword, expected in PRODUCTION_SETTINGS.items():
        found = _settings(production, keyword)
        assert found == expected, f"production {keyword}: {found}"
    keywords = [instruction.keyword for instruction in production.instructions]
    last_run = max(index for index, word in enumerate(keywords) if word == "RUN")
    assert last_run < keywords.index("USER"), "USER must follow every RUN"


def test_runtime_stage_runs_like_the_base_stage() -> None:
    # Same interpreter settings, user, workdir, port, health check and command
    # as the image built on base had.
    _assert_runtime_matches_base(_stages())


def _assert_tooling_precedes_the_deps_copy(stages: tuple[Stage, ...]) -> None:
    production = _stage("production", stages).instructions
    tooling = [
        index
        for index, i in enumerate(production)
        if i.keyword == "RUN" and " ".join(i.args.split()) == BASE_TOOLING_RUN
    ]
    deps = [
        index
        for index, i in enumerate(production)
        if i.keyword == "COPY" and _copy(i).flags.get("from") == "deps"
    ]
    assert len(tooling) == 1 and deps, f"tooling RUNs {tooling}, deps COPYs {deps}"
    # The fresh image ships packaging 26.3: copied over it, deps' 26.2 would
    # sit next to it as a second dist-info.
    assert tooling[0] < min(deps), "base tooling must be installed before deps COPY"


def test_runtime_tooling_is_installed_before_the_deps_site_packages() -> None:
    _assert_tooling_precedes_the_deps_copy(_stages())


_APT_RUN = (
    "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
    "    curl \\\n"
    "    && rm -rf /var/lib/apt/lists/*\n"
)
_DEPS_BIN = "COPY --from=deps /usr/local/bin /usr/local/bin\n"
# Production's USER (development has one too: anchored by what follows).
_USER = "USER neurostack\n\n# Expose port\nEXPOSE 8000\n\n# Health check"


@pytest.mark.parametrize(
    ("old", "new", "check", "message"),
    [
        (
            _APT_RUN,
            _APT_RUN.replace("curl \\", "curl build-essential \\"),
            _assert_runtime_packages,
            "build package in production: ['build-essential']",
        ),
        (
            _APT_RUN,
            _APT_RUN.replace("curl \\", "curl libpq-dev \\"),
            _assert_runtime_packages,
            "build package in production: ['libpq-dev']",
        ),
        (
            _APT_RUN,
            _APT_RUN.replace("curl \\", "curl libssl-dev \\"),
            _assert_runtime_packages,
            "build package in production: ['libssl-dev']",
        ),
        (
            _APT_RUN,
            _APT_RUN.replace("curl \\", "curl gcc-14 \\"),
            _assert_runtime_packages,
            "build package in production: ['gcc-14']",
        ),
        (
            _APT_RUN,
            _APT_RUN.replace("curl \\", "curl libpq5 \\"),
            _assert_runtime_packages,
            "runtime packages: ['curl', 'libpq5']",
        ),
        (
            _APT_RUN,
            _APT_RUN.replace("--no-install-recommends ", ""),
            _assert_runtime_packages,
            "apt-get install options: ['-y']",
        ),
        (
            _APT_RUN,
            _APT_RUN.replace(" \\\n    && rm -rf /var/lib/apt/lists/*\n", "\n"),
            _assert_runtime_packages,
            "production apt RUN",
        ),
        (
            _APT_RUN,
            _APT_RUN + "RUN apt-get update && apt-get install -y make\n",
            _assert_runtime_packages,
            "production package RUNs",
        ),
        (
            _APT_RUN,
            _APT_RUN.replace("    curl \\\n", ""),
            _assert_healthcheck_tool_is_installed,
            "HEALTHCHECK runs curl, which no runtime package installs",
        ),
        (
            "CMD curl -f http://localhost:8000/health",
            "CMD wget -q -O- http://localhost:8000/health",
            _assert_healthcheck_tool_is_installed,
            "HEALTHCHECK runs wget, which no runtime package installs",
        ),
        (
            _RUNTIME_PIP,
            _RUNTIME_PIP.replace(_PIP, ""),
            _assert_tooling_precedes_the_deps_copy,
            "tooling RUNs [], deps COPYs",
        ),
        (
            _RUNTIME_PIP,
            _RUNTIME_PIP.replace(_PIP, ""),
            _assert_runtime_matches_base,
            "production RUNs",
        ),
        (
            _USER,
            _USER.replace("USER neurostack", "USER root"),
            _assert_runtime_matches_base,
            "production USER: ['root']",
        ),
        (
            "# Same as base.\nENV PYTHONDONTWRITEBYTECODE=1 \\\n",
            "# Same as base.\nENV \\\n",
            _assert_runtime_matches_base,
            "production ENV",
        ),
        (
            "useradd --uid 1000 --gid 1000 --shell /bin/bash --create-home "
            "neurostack\n\nWORKDIR /app\n",
            "useradd --uid 0 --gid 1000 --shell /bin/bash --create-home "
            "neurostack\n\nWORKDIR /app\n",
            _assert_runtime_matches_base,
            "production RUNs",
        ),
        (
            "WORKDIR /app\n\n# The deps",
            "WORKDIR /srv\n\n# The deps",
            _assert_runtime_matches_base,
            "production WORKDIR",
        ),
        (
            "EXPOSE 8000\n\n# Health check",
            "EXPOSE 8080\n\n# Health check",
            _assert_runtime_matches_base,
            "production EXPOSE",
        ),
        (
            _USER,
            _USER.replace("USER neurostack\n", "USER neurostack\nRUN true\n"),
            _assert_runtime_matches_base,
            "production RUNs",
        ),
    ],
    ids=[
        "build-essential",
        "libpq-dev",
        "other-dev-package",
        "versioned-gcc",
        "unneeded-runtime-library",
        "recommends",
        "apt-lists-kept",
        "second-package-run",
        "curl-removed",
        "healthcheck-tool-not-installed",
        "tooling-not-installed",
        "tooling-run-missing",
        "user-root",
        "env-dropped",
        "root-uid",
        "workdir",
        "expose",
        "run-after-user",
    ],
)
def test_runtime_stage_contract_rejects_mutations(
    old: str, new: str, check: Any, message: str
) -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    with pytest.raises(AssertionError, match=re.escape(message)):
        check(_parse_stages(text.replace(old, new)))


def test_runtime_tooling_order_rejects_a_swap() -> None:
    # The fresh image's own packaging 26.3 dist-info would survive next to the
    # copied 26.2 one when pip runs after the COPY.
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert text.count(_RUNTIME_PIP) == 1 and text.count(_DEPS_BIN) == 1
    swapped = text.replace(_RUNTIME_PIP, _RUNTIME_PIP.replace(_PIP, ""))
    swapped = swapped.replace(_DEPS_BIN, _DEPS_BIN + _PIP)
    with pytest.raises(AssertionError, match="before deps COPY"):
        _assert_tooling_precedes_the_deps_copy(_parse_stages(swapped))
    with pytest.raises(AssertionError, match="production RUNs"):
        _assert_runtime_matches_base(_parse_stages(swapped))


def test_runtime_user_must_follow_every_run() -> None:
    # As neurostack, apt-get and pip could not write the system paths; the
    # image must still end up running as neurostack.
    text = DOCKERFILE.read_text(encoding="utf-8")
    anchor = "# Non-root user, same ids as base.\n"
    assert text.count(_USER) == 1 and text.count(anchor) == 1
    early = text.replace(_USER, _USER.replace("USER neurostack\n", ""))
    early = early.replace(anchor, "USER neurostack\n" + anchor)
    with pytest.raises(AssertionError, match="USER must follow every RUN"):
        _assert_runtime_matches_base(_parse_stages(early))


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
