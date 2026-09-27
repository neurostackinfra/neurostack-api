# Wish: Extract the cognitive layer into the `neurostack-core` package

> **Normative-plan notice (user-approved analyzer simplification):** This active section is the sole execution contract.
> Everything under **Superseded pre-fix draft**, including Plan Review #6 and older active-looking
> headings, is immutable historical evidence and must not be executed.

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS — execution complete; final gate SHIP; awaiting commit/PR/CI and authorized merge |
| **Slug** | `neurostack-core-extraction` |
| **Date** | 2026-09-25 |
| **Author** | Lorenzo |
| **Appetite** | medium |
| **Branch** | `wish/neurostack-core-extraction` |
| **Repos touched** | neurostack-api |
| **Design** | _No brainstorm — direct wish_ |

## Active Summary and Scope

Move `src/{srm,flow,policy,prompt}` atomically into the installable monorepo package
`packages/neurostack-core/`, make `neurostack-api` its first consumer, and prove every supported
Poetry, pip, Compose and production-image path after every risk-bearing wave. This is bounded
mechanical extraction enabling the already-authored `neurostack-core-dedomainification` successor;
it is not evidence of external demand or domain neutrality.

The user supplied the extraction target, the four-module scope, the deployment/gate blockers and the
successor ordering. Those decisions are resolved and testable, so a separate brainstorm artifact
would duplicate them rather than reduce ambiguity. Direct-wish treatment does not waive independent
plan or architecture review; this draft remains blocked from execution until the terminal independent
re-review returns SHIP and its evidence is persisted.

In scope: physical moves and import rewrites; root/package build metadata; `tzdata`; PEP 561;
conservative syntax-denylist boundary enforcement; opt-in metrics; a branch-derived minimal facade;
artifact tests; and all install/deployment paths. Out of scope: de-domainification, publication,
compatibility shims, new HTTP endpoints, extracting other domains, behavior changes, moving the root
consumer-owned parity/artifact tests into the distribution, and path-sensitive interpretation of
arbitrary Python control flow for dynamic-import provenance.

The provisional facade is derived, not copied from this wish. Group 1 records a deterministic JSON
fixture from production import consumers and the root README quickstart at the recorded base SHA.
Groups 2 and 3 compare source and wheel exports to that immutable fixture. Deep imports are normalized
to their owning facade for derivation but remain private after the move.

## Active Decisions

1. Use one package and move all four modules atomically; delete the originals in the same group.
2. Introduce the empty package and repair all install paths together in Group 1. The reusable
   `install_surface_gate` then proves the complete path matrix after Groups 1, 2 and 3; Group 4 adds
   final deployment/liveness evidence rather than duplicating the gate body. The root Poetry path
   dependency uses `develop = true`, both requirements workflows inherit the editable local entry
   `-e ./packages/neurostack-core`, Compose bind-mounts that path, and Docker copies it before
   dependency installation and into the production stage at the same absolute path.
3. Build complete `metrics.py` and bind top-level `enable_metrics` in Group 1. Collectors use
   `registry=None`. The first enable call owns its supplied registry (default registry when omitted),
   same-registry repeats are idempotent, and a different registry is rejected. There is no public
   reset hook, weak set or multi-registry state machine.
4. Register one composite collector whose `describe()`/`collect()` reserves all fourteen families
   and generated suffixes (`_total`, `_created`, `_bucket`, `_sum`, `_count`) atomically. Counter
   suffix, histogram suffix and last-family collisions reject without registry mutation.
5. Preserve observations made before enablement; import registers nothing. Default-first,
   custom-first, different-owner and manual-reload/fail-closed cases run in separate subprocesses so
   process ownership cannot make tests order-dependent. Manual reload remains unsupported.
6. Reject every static import of `src` and reserve dynamic-import syntax inside the package. A
   bounded AST walk rejects imports/references whose source spelling exposes `importlib`, `builtins`,
   `__builtins__`, `import_module` or `__import__`, regardless of nesting, alias or argument shape.
   The gate deliberately rejects ordinary shadows using those reserved identifiers; the extracted
   modules have no present need for them. It does not interpret runtime-injected callables or
   synthesize arbitrary strings. The facade extractor separately uses a syntax-only static-import
   table, skips `TYPE_CHECKING` bodies by AST shape, and rejects ambiguous/rebound aliases instead of
   simulating execution.
7. Ship `py.typed`, name package source in static checks, and retain a site-packages consumer probe
   demonstrating that removing the marker makes the installed package untyped.
8. Declare `tzdata` in package runtime metadata. Wheel, requirements and image probes use
   `PYTHONTZPATH=""`, verify the installed distribution metadata, and resolve
   `ZoneInfo("America/Sao_Paulo")` without host zoneinfo masking omissions.
9. Full-suite success is exact equality to the freshly generated branch-point failure set plus the
   branch-point completed-outcome floor. Pytest exit 0/1 is completed; exits 2–5, a missing terminal
   summary or outcome loss fail. No literal failure count is normative.
10. Capture fixtures live under tracked `tests/baselines/`; executable gates never consume ignored
    `.genie/`. The wish files may be ignored/untracked before Group 1 and are explicitly excluded only
    from the pre-edit cleanliness check, then narrowly unignored, staged and proven tracked.
11. The wheel payload is exactly `neurostack_core/{srm,flow,policy,prompt}/**`, package root
    `neurostack_core/{__init__.py,metrics.py,py.typed}`, and distribution metadata whose `METADATA`
    carries the package name, dependencies and README long description. The sdist additionally owns
    `pyproject.toml`, README and consumer scripts. Root `tests/package_artifact/**` and the four parity
    suites exercise source and wheel but are consumer-owned tests and are never wheel payload.
12. `neurostack-core-dedomainification` remains the successor. Publication and compatibility
    machinery wait for a separate decision.

## Active Analyzer Simplicity Case

The simplest complete boundary is a structural denylist, not a Python abstract interpreter. The
package must not depend on `src`, and none of the four extracted modules needs dynamic imports.
Therefore Group 1 deletes the path-sensitive scope/call/fixed-point machinery and replaces it with:

1. one bounded `ast.walk` that reports every static `src` import and every reserved dynamic-import
   identifier/import/attribute occurrence;
2. one small syntax-only facade extractor that records direct static imports, skips syntactic
   `TYPE_CHECKING` bodies independent of statement ordering, and rejects conflicting or rebound
   aliases rather than guessing which runtime path wins; and
3. focused tests proving nested/static forms, aliased source spelling, deliberate reserved-name
   false positives, ambiguous-alias rejection and termination on the historical adversarial inputs.

This intentionally trades permissiveness for a finite, auditable policy. A future need to use
`importlib`, `builtins` reflection or an ordinary member named `import_module` inside
`neurostack_core` is the trigger for a separate boundary-policy wish with a concrete consumer and
measured examples. Runtime-injected callables, reflection through synthesized names and general
Python control-flow interpretation are deferred and explicitly outside this extraction.

## Active Dependencies and Success Criteria

**depends-on:** none
**blocks:** `neurostack-core-dedomainification`

- [x] Branch SHA, exact failure set, pass/outcome floors and minimal public facade are generated from
      an uncontaminated archive of freshly fetched `origin/main`, before package/move changes.
- [x] A wheel and sdist build; a fresh external install contains precisely the declared payload and
      dependency/README metadata. Root consumer tests exercise source and wheel but do not ship.
- [x] Source and installed-wheel facades equal the independently generated consumer fixture exactly.
- [x] No package file statically imports `src` or contains reserved dynamic-import syntax; the gate
      terminates on adversarial loops/functions because it never evaluates control flow.
- [x] Old source directories are absent; parity and full-suite gates retain branch-point evidence.
- [x] Import registers nothing; composite registration exposes all fourteen unchanged families;
      ownership, idempotence, collision atomicity and reload fail-closed semantics pass in isolated
      subprocesses; real lifespan traffic increments the decision counter.
- [x] Poetry main/dev, requirements main/dev, the running Compose `api` service and the production
      image import from their intended local package/artifact origins with no host-checkout leakage.
- [x] `tzdata` metadata and empty-`PYTHONTZPATH` resolution pass in wheel, requirements and image.
- [x] Static, parity, full-suite and install-surface gates pass after Groups 1, 2 and 3; Group 4
      repeats them and adds final `/live` proof.
- [x] Both wishes and `.genie/INDEX.md` are tracked with the declared dependency direction.

## Active Waves

Exactly four waves execute sequentially because each consumes the prior shippable state.

| Wave | Group | Complexity / route | Outcome |
|------|-------|--------------------|---------|
| 1 | 1 | 4 — multi-package, stateful metrics, release paths; `engineer-complex` / high | Safe branch evidence, empty package, metrics and all reusable gates |
| 2 | 2 | 3 — broad atomic move; `engineer-standard` / high | Four modules moved and every application consumer cut over |
| 3 | 3 | 2 — artifact/release proof; `engineer-standard` / medium | README, exact wheel/sdist payload and external consumer proof |
| 4 | 4 | 1 — terminal deployment proof; `engineer-trivial` / low | Final fresh installs, image import and random-port liveness |

Dependencies are `1 -> 2 -> 3 -> 4`; no group runs in parallel.

## Active Branch Capture Contract

Before any Group 1 file change, the orchestrator runs this safe precondition. It never cleans,
resets, deletes or overwrites a branch. If the branch already exists at another SHA, or any path
outside ignored lifecycle evidence is dirty/untracked, stop for human resolution.

```bash
set -eo pipefail
git fetch origin main
BASE_SHA=$(git rev-parse --verify origin/main^{commit})
test -n "$BASE_SHA"
test -z "$(git status --porcelain=v1 --untracked-files=all -- . ':(exclude).genie')"
if git show-ref --verify --quiet refs/heads/wish/neurostack-core-extraction; then
  test "$(git branch --show-current)" = wish/neurostack-core-extraction
  test "$(git rev-parse HEAD)" = "$BASE_SHA"
else
  git switch --create wish/neurostack-core-extraction "$BASE_SHA"
fi
test "$(git branch --show-current)" = wish/neurostack-core-extraction
test "$(git rev-parse HEAD)" = "$BASE_SHA"
test -z "$(git status --porcelain=v1 --untracked-files=all -- . ':(exclude).genie')"
```

The cleanliness scope is the whole repository at `origin/main`, excluding only `.genie` because the
two wish files and index may initially be ignored/untracked. Thus contamination in `src/`, `tests/`,
root README, `pyproject.toml`, lock/config files or any other generator/test input is rejected. After
the precondition, Group 1 may add gate tooling. Capture itself never trusts the now-edited worktree:
it exports a temporary tree with `git archive "$BASE_SHA"`, so its exact source/ref is the recorded
commit. The capture input manifests are:

- suite evidence: the entire archived tree, with pytest collection rooted by archived
  `pyproject.toml`, `tests/**`, `src/**` and `conftest.py` if present;
- facade evidence: `src/api/routes/policies.py`, `src/core/pipeline.py`,
  `src/memory/__init__.py`, `src/memory/models.py`, `src/services/business_hours.py`,
  `src/services/trace_adapter.py`, and Python fenced blocks under `README.md`'s Quickstart heading;
  facade candidates are statically read from archived `src/{srm,flow,policy,prompt}/__init__.py`;
  named production consumers may additionally promote a directly imported deep symbol after the
  extractor proves that symbol exists in the archived owning module.

Once the extractor/capture scripts and their focused tests pass, but before package, production
source, dependency, Docker or Compose changes, generate every fixture deterministically:

```bash
set -eo pipefail
mkdir -p tests/baselines
BASE_SHA=$(git rev-parse --verify origin/main^{commit})
BASE_TREE=$(mktemp -d)
trap 'rm -rf "$BASE_TREE"' EXIT
git archive "$BASE_SHA" | tar -x -C "$BASE_TREE"
poetry run pytest tests/scripts/test_capture_core_branch_baseline.py \
  tests/scripts/test_extract_branch_public_api.py -q
poetry run python scripts/capture-core-branch-baseline.py \
  --tree "$BASE_TREE" --base-sha "$BASE_SHA" --output tests/baselines
poetry run python scripts/extract-branch-public-api.py \
  --tree "$BASE_TREE" --output tests/baselines/neurostack-core-extraction-public-api.json
test "$(cat tests/baselines/neurostack-core-extraction-branch.txt)" = "$BASE_SHA"
```

`capture-core-branch-baseline.py` runs one completed archived-tree suite, accepts only pytest exit
0/1, writes the SHA, sorted exact `FAILED|ERROR` node IDs, completed-outcome floor and focused parity
pass floor, and rejects missing/partial summaries. No fixture is hand-authored.

`extract-branch-public-api.py` uses only `ast`, never imports archived code, and emits UTF-8 JSON with
sorted module keys/symbol arrays, two-space indentation and a terminal newline. It resolves
`Import`/`ImportFrom`, `as` aliases and attribute uses; normalizes `src.<domain>.<deep>` to
`<domain>` plus the original symbol; ignores type-only/test/doc imports; and rejects stars, dynamic
imports, unresolved aliases, and deep symbols not defined by the archived owning module. Direct
facade imports must occur in the statically evaluated archived `__all__`; used deep imports are
deliberately promoted into the new facade (including `resolve_clinic_tz`) because removing them
would break a named production consumer. The expected minimal contract is exactly symbols used by
the six named production consumers plus executable root-README Quickstart imports—no unused legacy
export and no wish-embedded JSON. Focused synthetic tests plant direct/deep/aliased positives,
unused and non-production negatives, README positives, star/dynamic/unresolved/missing-symbol
failures and stable ordering.

After generation, a second fresh archive must reproduce both generators byte-for-byte:

```bash
VERIFY_TREE=$(mktemp -d)
VERIFY_OUT=$(mktemp -d)
git archive "$(cat tests/baselines/neurostack-core-extraction-branch.txt)" | tar -x -C "$VERIFY_TREE"
poetry run python scripts/capture-core-branch-baseline.py --tree "$VERIFY_TREE" \
  --base-sha "$(cat tests/baselines/neurostack-core-extraction-branch.txt)" --output "$VERIFY_OUT"
poetry run python scripts/extract-branch-public-api.py --tree "$VERIFY_TREE" \
  --output "$VERIFY_OUT/neurostack-core-extraction-public-api.json"
for fixture in branch known-failures outcome-floor pass-floor; do
  diff -u "tests/baselines/neurostack-core-extraction-$fixture.txt" \
    "$VERIFY_OUT/neurostack-core-extraction-$fixture.txt"
done
diff -u tests/baselines/neurostack-core-extraction-public-api.json \
  "$VERIFY_OUT/neurostack-core-extraction-public-api.json"
```

## Active Shared Gates

Group 1 creates `scripts/wish-gates.sh` with four reusable functions:

- `core_suite_gate`: four existing root parity suites, exit 0 and branch pass floor;
- `full_suite_gate`: completed exit 0/1, exact sorted failure-set diff and outcome floor;
- `static_gate`: boundary checker, Black, isort, flake8 and mypy across root and package;
- `install_surface_gate`: isolated Poetry main and dev installs, fresh `requirements.txt` and
  `requirements-dev.txt` venvs, an actual `docker compose run --rm --no-deps api` import, and a fresh
  production-target image build/import. It owns invocation-local temp dirs, project/image names and
  cleanup, and accepts `--phase foundation|moved|artifact` only for diagnostics—never to skip a row.

`scripts/assert-neurostack-core-origin.py` is the shared oracle used inside every environment. It
imports `neurostack_core`, reads `importlib.metadata.distribution("neurostack-core")`, and rejects
cwd or ambient `PYTHONPATH` leakage. For editable installs it requires `__file__` under the expected
source prefix, dist-info in the active environment, and `direct_url.json`/editable metadata pointing
to that same source. For non-editable wheel installs it requires package code and dist-info under the
active environment prefix. It also checks `tzdata` metadata and zone resolution under
`PYTHONTZPATH=""`. Expected origins are explicit:

| Surface | Install command | Required origin |
|---------|-----------------|-----------------|
| Poetry main | fresh project env, `poetry install --only main` | checked-out `packages/neurostack-core` path dependency |
| Poetry dev | different fresh project env, `poetry install` | same checked-out local package, dev deps present |
| pip main | fresh venv, `pip install -r requirements.txt` | editable/local package under the checked-out package path |
| pip dev | different fresh venv, `pip install -r requirements-dev.txt` | same local package via inherited main requirements |
| Compose | build `api`, then `docker compose run --rm --no-deps api ...` | `/app/packages/neurostack-core`, backed by the declared Compose mount |
| Production | fresh `docker build --target production` then `docker run` | `/app/packages/neurostack-core` copied into the image; never a host bind mount |

Focused tests in `tests/scripts/test_install_surface_gate.py` and
`test_assert_neurostack_core_origin.py` use fake interpreters/metadata and command shims to prove all
six rows execute and that wrong prefix, cwd shadow package, missing distribution, absent editable
metadata, host `PYTHONPATH`, Compose config-only substitution and skipped image import each fail.
Other gate tests plant an extra full-suite failure, an in-scope parity failure, pytest exit 4,
missing summary and outcome loss. Boundary/extractor tests plant every static `src` import form,
reserved dynamic-import source spelling under nested and type-only scopes, deliberate ordinary-name
collisions, ambiguous/rebound static aliases, and the historical nonterminating loop/function inputs;
the structural scan must finish within the focused test timeout without evaluating control flow.

## Active Execution Groups

### Group 1 — Branch evidence, package foundation, metrics and install paths

**Goal:** Capture uncontaminated branch evidence, introduce a complete empty package, and make every
supported install surface shippable before the move.

**Deliverables:**

1. The two deterministic branch extractors, their tests, recorded SHA/failure/floor/API fixtures and
   the regeneration diff defined above.
2. Package `pyproject.toml`, README, root `__init__.py`, `metrics.py` and `py.typed`; runtime deps are
   `pydantic`, `prometheus-client`, `tzdata`.
3. Root path dependency/lock/build dev dependency and package coverage/static paths.
4. Docker build context/stages, Compose packages mount, both requirements files, root README,
   `SETUP.md` and `start-dev.sh` updated in the same group.
5. Complete composite metrics implementation and isolated-subprocess tests: default-first twice;
   custom-first twice; default then different custom; custom then default; observation retention;
   counter/histogram/last-family collision rollback; and module reload rejection. No reset API.
6. Boundary/completion/origin/install-surface helpers and planted focused tests. The boundary work
   deletes the path-sensitive `_lexical_imports.py` interpreter, introduces the bounded structural
   `_forbidden_imports.py` helper, and reduces facade alias handling to static imports with explicit
   ambiguity rejection and syntactic `TYPE_CHECKING` exclusion.
7. Narrow `.gitignore` exceptions; both wishes and `.genie/INDEX.md` staged/versioned, followed by
   `git ls-files --error-unmatch` for all three.

**Acceptance:** deterministic regeneration is byte-identical; all focused negative tests fail for
their planted reason; boundary/extractor adversarial cases terminate within five seconds; the deleted
interpreter has no remaining imports or file; package metadata declares `tzdata`; all six install
surfaces import from the required origin; static/parity/full gates pass.

```bash
set -eo pipefail
source scripts/wish-gates.sh
git ls-files --error-unmatch .genie/wishes/neurostack-core-extraction/WISH.md \
  .genie/wishes/neurostack-core-dedomainification/WISH.md .genie/INDEX.md
poetry run pytest tests/scripts tests/srm/test_metrics_optin.py -q
install_surface_gate --phase foundation
static_gate
core_suite_gate
full_suite_gate
```

**depends-on:** none

---

### Group 2 — Atomic four-module move and application cutover

**Goal:** Move all four modules and consumers together while preserving behavior and every install
surface.

**Deliverables:** move `src/{srm,flow,policy,prompt}` to package subpackages; rewrite package,
application, tests and docs imports; delete originals; expose only the captured minimal facade;
connect all fourteen handles to Group 1's collector; enable the default registry in real lifespan
only when configured. Root parity tests remain consumer-owned. Group 3 creates and owns
`tests/package_artifact/**`; Group 2 does not reference that not-yet-created path.

**Acceptance:** source facade exactly equals branch fixture; old paths/imports are absent; no moved
module registers at import; isolated metrics and real lifespan `/metrics` tests preserve names,
labels, disabled mode and decision-counter increment; every shared gate passes.

```bash
set -eo pipefail
source scripts/wish-gates.sh
poetry run python packages/neurostack-core/scripts/check_public_api.py \
  --expected tests/baselines/neurostack-core-extraction-public-api.json --source
poetry run pytest tests/srm tests/flow tests/policy tests/prompt \
  tests/api/test_core_metrics_lifespan.py -q
for old in src/srm src/flow src/policy src/prompt; do test ! -d "$old"; done
install_surface_gate --phase moved
static_gate
core_suite_gate
full_suite_gate
```

**depends-on:** 1

---

### Group 3 — README and exact wheel/sdist consumer proof

**Goal:** Prove the finished documented artifact outside the checkout.

**Deliverables:** package README with source install, executable Quickstart, process-local
flow/policy state and pt-BR limitation; smoke/README extractors; root package-artifact tests for the
exact wheel payload, sdist inputs, dependency metadata, `py.typed`, source/wheel facade and timezone.
These tests consume artifacts and are explicitly excluded from wheel contents.

**Acceptance:** one fresh wheel and sdist build without missing-file warnings; wheel contains the
four recursive subpackages, root `__init__.py`, `metrics.py`, `py.typed` and correct dist-info
metadata/README long description—and no root tests; installed facade equals branch fixture; README
Quickstart and smoke run externally; every shared gate passes.

```bash
set -eo pipefail
source scripts/wish-gates.sh
poetry run pytest tests/package_artifact -q
install_surface_gate --phase artifact
static_gate
core_suite_gate
full_suite_gate
```

**depends-on:** 2

---

### Group 4 — Terminal deployment and liveness proof

**Goal:** Perform the final release-path proof against the completed artifact; no feature work.

```bash
set -eo pipefail
source scripts/wish-gates.sh
install_surface_gate --phase artifact
TMP=$(mktemp -d)
IMAGE="neurostack-api:core-extraction-final-$(git rev-parse --short HEAD)"
CID=""
cleanup() {
  if test -n "$CID"; then
    docker stop "$CID" >/dev/null 2>&1 || true
    docker container rm "$CID" >/dev/null 2>&1 || true
  fi
  docker image rm "$IMAGE" >/dev/null 2>&1 || true
  rmdir "$TMP" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker build --target production --tag "$IMAGE" .
docker run --rm -i --entrypoint env "$IMAGE" PYTHONTZPATH="" python - \
  --mode editable --expected-source /app/packages/neurostack-core \
  < scripts/assert-neurostack-core-origin.py
CID=$(docker run -d --publish 127.0.0.1::8000 --entrypoint uvicorn "$IMAGE" \
  src.api.main:app --host 0.0.0.0 --port 8000 --lifespan off --no-access-log)
PORT=$(docker port "$CID" 8000/tcp | awk -F: '{print $NF}')
for _ in $(seq 1 30); do curl --fail --silent "http://127.0.0.1:$PORT/live" && break; sleep 1; done
curl --fail --silent "http://127.0.0.1:$PORT/live" >/dev/null
poetry run pytest tests/package_artifact tests/api/test_core_metrics_lifespan.py -q
static_gate
core_suite_gate
full_suite_gate
```

**Acceptance:** reusable gate proves fresh Poetry, requirements, running Compose and image origins;
the independently tagged production image repeats empty-`PYTHONTZPATH` metadata/origin assertions,
imports the app and serves `/live` on a random loopback port; all aggregate gates pass.

**depends-on:** 3

## Active QA and Risks

QA verifies unchanged fixed-input behavior; exact source/wheel facade; root parity suites; boundary
negative cases; metrics ownership/collisions/reload in isolated subprocesses; real lifespan names,
labels and increment; fixture provenance/regeneration; every install origin; timezone metadata and
empty-path resolution; exact wheel payload; and no migration or latency-affecting indirection.

| Risk | Mitigation |
|------|------------|
| Capture is polluted by the planning checkout | Whole-tree pre-edit cleanliness excluding only `.genie`, then generation from a recorded immutable archive and byte-for-byte regeneration. |
| Implementation defines its own API expectation | Static branch consumer extractor with explicit inputs, planted tests and no wish JSON input. |
| Conservative denylist rejects a future legitimate reserved name | Accepted for this extraction because the measured four-module source has no such use; a concrete future consumer triggers a separate boundary-policy wish instead of reviving path-sensitive interpretation here. |
| A wave breaks a supported path | One tested six-row install gate runs after Groups 1, 2 and 3 and again at final proof. |
| Host checkout makes an import falsely pass | Shared origin oracle checks `__file__`, distribution files/direct-url metadata, prefix and `PYTHONPATH`. |
| Metrics tests depend on first-call order | Every mutually exclusive ownership/reload case is a fresh subprocess; no reset hook exists. |
| Wheel appears complete because root tests pass | Root consumer tests inspect exact wheel members and run installed code while remaining outside payload. |
| Host zoneinfo masks missing dependency | Installed metadata plus all external lookups under `PYTHONTZPATH=""`. |
| Move changes behavior or hides test loss | Atomic rename, unchanged parity suites, exact failure diff and pass/outcome floors after every wave. |
| Package is mistaken for domain-neutral/public | README and wish state pt-BR limitation, successor and unpublished status. |

## Active Files to Create/Modify

```text
CREATE  packages/neurostack-core/{pyproject.toml,README.md}
CREATE  packages/neurostack-core/neurostack_core/{__init__.py,metrics.py,py.typed}
CREATE  packages/neurostack-core/scripts/{smoke.py,check_public_api.py}
CREATE  scripts/_forbidden_imports.py
DELETE  scripts/_lexical_imports.py
CREATE  scripts/{wish-gates.sh,check-package-boundary.py,validate-pytest-completion.py}
CREATE  scripts/{capture-core-branch-baseline.py,extract-branch-public-api.py}
CREATE  scripts/{assert-neurostack-core-origin.py,extract-readme-quickstart.py}
CREATE  tests/scripts/test_{check_package_boundary,validate_pytest_completion}.py
CREATE  tests/scripts/test_{capture_core_branch_baseline,extract_branch_public_api}.py
CREATE  tests/scripts/test_{install_surface_gate,assert_neurostack_core_origin}.py
CREATE  tests/package_artifact/**
CREATE  tests/srm/test_metrics_optin.py
CREATE  tests/api/test_core_metrics_lifespan.py
CREATE  tests/baselines/neurostack-core-extraction-{branch,known-failures,outcome-floor,pass-floor}.txt
CREATE  tests/baselines/neurostack-core-extraction-public-api.json
MOVE    src/{srm,flow,policy,prompt}/** -> packages/neurostack-core/neurostack_core/{srm,flow,policy,prompt}/**
MODIFY  .gitignore .dockerignore .genie/INDEX.md
ADD     .genie/wishes/neurostack-core-extraction/WISH.md
ADD     .genie/wishes/neurostack-core-dedomainification/WISH.md
MODIFY  pyproject.toml poetry.lock Makefile Dockerfile docker-compose.yml
MODIFY  requirements.txt requirements-dev.txt README.md SETUP.md start-dev.sh
MODIFY  src/api/main.py src/api/routes/policies.py src/core/pipeline.py
MODIFY  src/memory/__init__.py src/memory/models.py
MODIFY  src/services/business_hours.py src/services/trace_adapter.py
MODIFY  tests/** docs/srm-hybrid-classifier.md docs/prompt-templates-and-funnel.md
DELETE  src/srm/ src/flow/ src/policy/ src/prompt/
```

## Superseded pre-fix draft (historical only; do not execute)

The following draft and its old active-looking headings are retained solely so the appended review
history remains auditable. The normative-plan notice above overrides every statement below until the
Review Results history.

| Field | Value |
|-------|-------|
| **Status** | FIX-FIRST — independent Plan Review #5; fix loop 1/2 in progress |
| **Slug** | `neurostack-core-extraction` |
| **Date** | 2026-09-25 |
| **Author** | Lorenzo |
| **Appetite** | medium |
| **Branch** | `wish/neurostack-core-extraction` |
| **Repos touched** | neurostack-api |
| **Design** | _No brainstorm — direct wish_ |

## Summary

Extract `src/srm`, `src/flow`, `src/policy` and `src/prompt` (11,233 lines) into a standalone,
installable Python package `neurostack-core` at `packages/neurostack-core/`, with `neurostack-api`
becoming the package's first consumer rather than its owner. The four modules already import nothing
from the rest of `src/`; this wish turns that latent boundary into an enforced one and proves the
result installs and runs outside this repository. The extraction is not complete until every real
installation path — Poetry, requirements files, the development Compose mount and the production
Docker image — consumes the package successfully.

This wish is the **mechanical extraction only**. Stripping the Brazilian aesthetic-clinic domain out
of the engine is a separate wish, `neurostack-core-dedomainification`, which this one blocks.

## Scope

### IN

- New package at `packages/neurostack-core/` with its own `pyproject.toml`, distribution name
  `neurostack-core`, import namespace `neurostack_core`, subpackages `srm`, `flow`, `policy`, `prompt`.
- Physical move of the four modules out of `src/`, imports rewritten to `from neurostack_core.<mod>...`.
- Rewiring of the 6 call-site files (23 import lines) in `src/` outside the four modules, the 10 test
  files, and the code examples in `docs/srm-hybrid-classifier.md` and `docs/prompt-templates-and-funnel.md`.
- Root `pyproject.toml` consuming the package as a develop path dependency; `build` added to dev deps.
- `tzdata` as a package runtime dependency because `prompt.timeutils` resolves IANA zones and the
  current app explicitly supplies the database even on minimal Linux images.
- An enforced boundary: an AST-based check that fails on every static absolute import of the app
  namespace from `packages/`, including `import src`, `import src.foo`, `from src import foo` and
  `from src.foo import bar`; dynamic `src` imports are forbidden by an explicit source scan.
- Prometheus metric registration (14 `Counter`/`Histogram`) moved from import-time global side effect
  to opt-in, so importing the library does not mutate the host's registry.
- A PEP 561 `py.typed` marker, without which the extracted package is silently exempt from `mypy`.
- Wheel build verified by installing into a clean virtualenv outside the repo and running a smoke
  script with an empty environment, no Postgres and no Redis.
- Consumer-facing README documenting install, quickstart, and the package's current limitation: it
  ships the pt-BR clinic domain as built-in content.
- A provisional supported API: the exact symbols exported by the four subpackage `__all__` lists.
  Application production imports use those facades; package-internal imports and tests may use
  implementation modules. A wheel-installed contract test imports every supported symbol.
- Production/developer install-path updates: `Dockerfile`, `docker-compose.yml`, both requirements
  files, `README.md`, `SETUP.md`, and `start-dev.sh`, with a production-image import/boot proof.
- Versioned, checkout-portable gate fixtures under `tests/baselines/`; no executable gate depends on
  ignored `.genie/` evidence.

### OUT

- **De-domainification.** Moving intent patterns, funnel stages, prompt templates and policy rules
  into loadable presets is `neurostack-core-dedomainification`. Its current pre-extraction estimate
  is **619 pt-BR literals across 18 files**, and its first group remeasures the merged package rather
  than treating that estimate as a frozen acceptance count. Attempting both tasks in one wish is what
  produced four consecutive blocked plan reviews — see Review #4.
- Publishing to PyPI or TestPyPI — the public distribution name is a one-way door needing its own
  wish with owner/credential decisions. This wish stops at a locally installable wheel.
- The stateless `POST /v1/srm/decide` HTTP endpoint — a separate adoption path, tracked as follow-up.
- Extracting `ctx`, `memory`, `handoff`, `llm`, `api`, `services` or `repositories`. These are
  genuinely coupled (`ctx` needs `KnowledgeService`, `memory` needs SQLAlchemy/Redis/event store).
- Moving `tests/{srm,flow,policy,prompt}` into the package. The tests stay in the app this wish, but
  are explicitly the package-owned parity gate and are supplemented by installed-wheel public-API,
  timezone, smoke and README tests. The source distribution does not ship the app test tree.
- Any behavior change whatsoever. Every regex, threshold, enum value, template string and policy
  message must survive byte-identical. The diff is import lines, file paths and metric registration.
- Changing the `neurostack-api` product in any user-visible way.
- Reformatting or type-fixing files the move does not touch.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Monorepo at `packages/neurostack-core/`, not a separate repo | Contract changes stay atomic in one commit and one CI run. A separate repo would force coordinated two-repo PRs for every signature change while adoption is still zero. The pyproject boundary plus the Decision-6 guard, not repo distance, is what makes the package real. |
| 2 | All four modules in one wish, not srm-first | The four share one dependency profile and have zero inter-module imports. Splitting repeats the same mechanical review three times and leaves the package advertising a partial cognitive layer. |
| 3 | Absolute in-package imports (`from neurostack_core.srm.models import ...`) | Matches existing style, keeps diffs greppable, avoids relative-depth churn. |
| 4 | Metric registration opt-in via explicit enable call, default off | Importing a library must not mutate the host's global Prometheus registry. `neurostack-api` enables it at startup and keeps emitting the same 14 metric names. Implemented with `Counter(..., registry=None)` rather than a no-op layer — verified that such a counter accepts `.inc()` while unregistered and preserves its value when registered later, giving opt-in semantics with no call-site branches. |
| 5 | `neurostack-api` becomes a consumer; the four `src/` directories are deleted | Leaving copies creates two sources of truth that silently diverge. The 1006 passing tests are the safety net. |
| 6 | The boundary is enforced by a check, not by convention | `develop = true` in one repo with one CI run means nothing structurally prevents `from src.services` appearing inside `neurostack_core` next month. A grep gate is the cheapest thing that makes the Summary's "enforced" claim true. |
| 7 | Stop at an installable wheel; PyPI is a separate wish | Reserving a public name and wiring publish credentials is external and hard to reverse, with no consumer waiting. |
| 8 | Validation asserts the exact known-failing test set, not pass/fail totals | Measured 2026-09-25: the suite is `2 failed, 2733 passed, 14 skipped`, `mypy src` is clean across 164 files, `black`/`flake8` are clean. A bidirectional `diff` against a 2-line known-failure list catches both new failures and silently disappearing tests, and unlike a summary-line match it survives the test count growing. |
| 9 | Every gate is proven against a planted failure before it is trusted | Plan Review #2 executed the gates instead of reading them and found five that could never pass — including `full_suite_gate`, which `set -eo pipefail` killed before it reached its own `diff`. Review #3 found the `core_suite_gate` probe writing to a directory that gate never collects. A guard nobody watched fail is not a guard. |
| 10 | The package ships a PEP 561 `py.typed` marker **and** `static_gate` names the package path | These are two independent fixes for one blind spot, and Plan Review #4 corrected an earlier claim that either alone sufficed. Measured in an isolated venv: when mypy resolves the package through site-packages (the wheel-consumer path, and any invocation that does not name it), an unmarked package is `Any` and `ignore_missing_imports = true` (`pyproject.toml:87-90`) suppresses the diagnostic — a planted bogus attribute yields `Success: no issues found`. When the package path *is* named on the command line, mypy analyses it as source and the marker is irrelevant there. So the marker protects consumers and the CLI path protects us; the probe in Group 1 must therefore use the un-named form or it can never fail. |
| 11 | De-domainification is a separate wish that this one blocks | Four consecutive plan reviews blocked on pt-BR content the plan had not enumerated, each round finding more than the last. The root cause is that "remove the domain" is an open-ended discovery problem while "move four directories" is a bounded mechanical one. The successor's current pre-extraction estimate is 619 literals across 18 files and its Group 1 remeasures the merged package; this wish no longer carries a competing inventory. |
| 12 | Deployment and developer installation are part of the extraction | A root path dependency changes the build graph. The current Docker dependency stage copies only root Poetry files, the production stage omits `packages/`, Compose mounts only `src`, and pip workflows install requirements without the local package. A wheel smoke alone cannot prove the application still deploys. |
| 13 | Gate fixtures live in tracked source, not ignored `.genie/` | `.gitignore` ignores `.genie/`; a normal clone would omit `known-failures.txt` and make every downstream full-suite gate unusable. The fixture lives at `tests/baselines/neurostack-core-extraction-known-failures.txt`, generated on the exact branch point. |
| 14 | Boundary enforcement is syntax-aware | The previous grep missed valid couplings such as `from src import services` and bare `import src`, and GNU `\s` made it needlessly shell-specific. A small AST checker covers all static import forms; a literal scan rejects `importlib.import_module("src...")` and `__import__("src...")`. |
| 15 | Metrics are opt-in for export, not for in-memory observation | The existing engine call sites keep unconditional `.labels(...).inc()`/`.time()` calls. Collectors created with `registry=None` therefore retain observations before registration. `enable_metrics()` is idempotent per registry, supports default and custom registries, preflights collisions before registering anything, and fails clearly on collision. Manual `importlib.reload()` of the metrics module is unsupported and must fail closed on duplicate names rather than silently split observations. |
| 16 | The four subpackage facades are the provisional public API | The app currently imports implementation modules directly. Extraction without a supported surface merely changes the spelling of that coupling. The package README and an exact `__all__` contract test define the supported symbols; deep imports remain private. |
| 17 | `tzdata` is a runtime dependency | `neurostack_core.prompt.timeutils` calls `ZoneInfo("America/Sao_Paulo")`. The app already declares `tzdata>=2024.1` because minimal images may lack an OS database. Omitting it from the wheel makes the clean-environment proof Linux-host-dependent. |
| 18 | Evidence is regenerated on a declared branch cut from current `origin/main` | Council Review #1 found the draft on `wish/clinic-service-modality` at `3545cfb`, seven commits behind `origin/main`, while `wish/neurostack-core-extraction` did not exist. Counts and known failures are evidence only for their exact tree, so Group 1 cuts/records the branch point and regenerates them before downstream work. |

## Simplicity Case

- **Simplest complete design:** move four already-decoupled directories into a second poetry package
  in the same repo, rewrite their import prefix, point the app at the package, delete the originals.
  No adapter layer, no compatibility shim, no plugin registry, no entry-point discovery.
- **Added machinery:** four mechanisms go beyond a pure move, each forced by a present fact.
  (a) *Opt-in metric registration* — 14 metrics construct at import time (`src/srm/engine.py:65`,
  `src/flow/manager.py:39`, `src/policy/engine.py:34`, `src/prompt/engine.py:35`), a side effect no
  embedded library may impose on its host.
  (b) *An AST boundary guard* — one small script, required because nothing else prevents the
  extraction decaying back into coupling and a regex does not cover Python's legal import forms.
  (c) *A `py.typed` marker* — one empty file, required because `strict = true` combined with
  `ignore_missing_imports = true` makes the package silently unchecked for anyone consuming the wheel.
  (d) *Per-registry metric enablement state* — one lock and a weak registry set, required because
  repeated FastAPI lifespans must be idempotent while independent hosts may supply distinct registries.
- **Deferred until measured:** no preset system, no loader, no TOML schema, no derived enum and no
  type stub — all of that belongs to the de-domainification wish and none of it is needed to make the
  package installable. Dropping it from this wish removed four execution groups and the two CRITICALs
  that blocked Review #4. No PyPI publish.
- **Complexity removed:** no dual-maintenance window (originals deleted in the same group that moves
  them), no re-export shim in `src/`, no runtime flag selecting old vs new import path, no separate
  repository, no data migration — `FunnelStage` stays an ordinary hardcoded enum in this wish, so
  nothing persisted changes.

## Dependencies

**depends-on:** none
**blocks:** neurostack-core-dedomainification

## Success Criteria

- [ ] `packages/neurostack-core/` builds a wheel and sdist with `python -m build`, with no warnings
      about missing files.
- [ ] The wheel installs into a fresh virtualenv outside the repository, pulling only `pydantic`,
      `prometheus_client`, `tzdata` and their transitive dependencies — no `fastapi`, `sqlalchemy`,
      `redis`, `asyncpg` or `qdrant-client`; `ZoneInfo("America/Sao_Paulo")` resolves there.
- [ ] In that virtualenv, with an empty environment and no services, a smoke script imports all four
      subpackages and classifies `"Olá, quero agendar botox para sexta às 10h"` as `intent=agendar`,
      `category=scheduling`, `stage=em_qualificacao`.
- [ ] `grep -rn "from src\.\(srm\|flow\|policy\|prompt\)" src tests docs` returns no matches.
- [ ] Nothing under `packages/` statically or dynamically imports the app's `src` namespace —
      enforced by an AST/literal check proven against all supported import forms.
- [ ] `src/srm`, `src/flow`, `src/policy`, `src/prompt` no longer exist.
- [ ] Importing any `neurostack_core` subpackage registers zero collectors in the default Prometheus
      registry; the 14 metric names appear only after `enable_metrics()`, and a counter demonstrably
      increments after a real decision.
- [ ] `neurostack-api` still exposes all 14 original metric families with unchanged types and label
      names at `/metrics` after a real lifespan startup; real traffic increments the decision counter.
- [ ] Metrics-disabled startup exports none of the 14 package families; repeated app lifespans and
      repeated enable calls do not duplicate collectors; custom registries work independently;
      collisions and manual reload fail clearly before partial registration.
- [ ] The package ships `py.typed` and is inside `mypy`'s scope — proven by the marker's removal
      changing the result of a site-packages-resolved check, not by the checker merely succeeding.
- [ ] `tests/srm tests/flow tests/policy tests/prompt` reports zero failures and at least 1006 passed
      at every group boundary, with no test deleted or skipped to achieve it.
- [ ] The full app suite's failing set stays exactly the 2 known failures — no new failure, no
      collection error, neither known failure silently disappearing, and no interrupted/internal
      pytest run accepted as success. The total outcome floor is regenerated at the branch point.
- [ ] `black`, `flake8`, `isort` and `mypy` stay clean over **both** `src` and the package.
- [ ] The package README documents install, quickstart and the pt-BR limitation, with its quickstart
      block executed rather than attested.
- [ ] Every symbol in the four documented subpackage `__all__` facades imports from the installed
      wheel; production application imports use those facades rather than private engine modules.
- [ ] `pip install -r requirements.txt` and `pip install -r requirements-dev.txt` from the repository
      root make `neurostack_core` importable in a fresh environment.
- [ ] The production Docker target builds with the path dependency available during installation,
      contains the package at runtime and imports every supported facade; Compose resolves the
      package mount; the image serves `/live` on a random port, while Group 4 separately proves the
      real lifespan and package metrics response.
- [ ] The extraction and successor wishes are recorded in `.genie/INDEX.md`, both WISH files are
      versioned, and the successor retains `depends-on: neurostack-core-extraction`.

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 3 — multi-package work (+1), build/lockfile change (+1), CI/release gates (+1) | engineer-standard / high | Package skeleton, build tooling, `py.typed`, AST boundary guard, tracked test baselines, exact branch evidence. |

### Wave 2 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 2 | engineer | 2 — multi-package work (+1), import contract with `src/memory` (+1) | engineer-standard / medium | Move `srm`, rewire call sites, delete the original. |

### Wave 3 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 3 | engineer | 2 — multi-package work (+1), three modules in one move (+1) | engineer-standard / medium | Move `flow`, `policy`, `prompt`; activate the boundary guard. |

_Wave 3 is sequential behind Wave 2, not parallel with it. `known_first_party = ["src"]`
(`pyproject.toml:85`) makes isort hoist `neurostack_core.*` out of the first-party block, so both
groups relocate lines into the **same** import block of `src/core/pipeline.py`. Verified conflicting
via `git merge-file`._

### Wave 4 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 4 | engineer | 3 — multi-package work (+1), process-global registry state (+2) | engineer-standard / high | Opt-in metric registration. |

### Wave 5 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 5 | engineer | 2 — multi-package work (+1), build/release artifact (+1) | engineer-standard / medium | Wheel build, clean-environment proof, consumer README. |

### Wave 6 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 6 | engineer | 2 — multi-package work (+1), CI/release work (+1) | engineer-standard / high | Repair every Poetry/pip/Docker/Compose install path and prove the production image. |

_Every wave is sequential here. With de-domainification split out there is no longer a parallel wave:
Groups 2 and 3 serialize on isort blocks in `src/core/pipeline.py`, Group 4 edits the four `engine.py`
files the moves produce, Group 5 consumes the finished package artifact, and Group 6 validates that
artifact through every application installation path. The absence of parallelism is a consequence
of those handoffs, not an oversight — the domain-content groups are in the successor wish._

Complexity scoring rubric: score each group independently and record the total plus a short rationale in **Complexity**. Add:

- **+2** each for orchestration / agent-lifecycle / routing; cost / model / escalation; stateful work; subjective acceptance.
- **+1** each for multi-package work; OTel-label dependency; no deterministic test; prior rework; prompt-skill change; CI / release work.

Route the total in **Model** by portable role and reasoning effort: **0–1** →
`engineer-trivial` / low; **2–3** → `engineer-standard` / medium or high;
**4–6** → `engineer-complex` / high; **7+** → `engineer-complex` plus an
independent `final-gate` at the highest justified effort. Codex maps these to
the `genie_*` profiles; other runtimes use their matching native roles. Keep
model and effort in runtime session/agent configuration, never skill frontmatter.

### Shared validation preamble

Every group's validation block begins with `set -eo pipefail`. Three helpers are referenced below and
created in Group 1 as `scripts/wish-gates.sh`:

```bash
core_suite_gate() {                      # zero failures, never fewer than 1006 passed
  local out rc; out="$(mktemp "${TMPDIR:-/tmp}/core.XXXXXX")"
  trap 'rm -f "$out"' RETURN
  set +e
  poetry run pytest tests/srm tests/flow tests/policy tests/prompt -q -p no:cacheprovider | tee "$out"
  rc=${PIPESTATUS[0]}
  set -e
  test "$rc" -eq 0
  grep -qE '^=+ [0-9]+ passed' "$out"    # a failure makes the banner read "N failed, M passed"
  poetry run python -c "import re,sys; t=open(sys.argv[1]).read(); m=re.search(r'^=+ (\d+) passed', t, re.M); sys.exit(0 if m and int(m.group(1))>=1006 else 1)" "$out"
}

full_suite_gate() {                      # exact failures plus a completed-outcome floor
  local out bad rc
  out="$(mktemp "${TMPDIR:-/tmp}/full.XXXXXX")"; bad="$(mktemp "${TMPDIR:-/tmp}/bad.XXXXXX")"
  trap 'rm -f "$out" "$bad"' RETURN
  set +e
  poetry run pytest -q -p no:cacheprovider 2>&1 | tee "$out"
  rc=${PIPESTATUS[0]}
  set -e
  case "$rc" in 0|1) ;; *) echo "FAIL: pytest did not complete (exit $rc)"; return 1 ;; esac
  grep -oE '^(FAILED|ERROR) [^ ]+' "$out" | LC_ALL=C sort > "$bad" || true
  diff -u tests/baselines/neurostack-core-extraction-known-failures.txt "$bad"
  poetry run python scripts/validate-pytest-completion.py \
    "$out" tests/baselines/neurostack-core-extraction-outcome-floor.txt
}

static_gate() {                          # static checks plus the package boundary
  poetry run python scripts/check-package-boundary.py packages/neurostack-core
  poetry run black --check src tests packages
  poetry run isort --check-only src tests packages
  poetry run flake8 src tests packages
  poetry run mypy src packages/neurostack-core/neurostack_core
}
```

Four properties of these gates were established by execution, not by reading — Plan Reviews #2, #3
and #4 each found a gate that could never fail:

- **Pytest's status is captured, not discarded.** The 2 known failures make `pytest` exit 1, which is
  an expected completed run; exit 0 is a completed clean run. Exit codes 2–5 are interruption,
  internal error, usage error or no-tests and fail immediately. This replaces the earlier `|| true`
  design, which could accept a crash after printing the pinned failures or accept an empty crash when
  the baseline eventually became clean.
- **`grep -oE '^(FAILED|ERROR) [^ ]+'`, not `grep -E '^(FAILED|ERROR)'`.** `-o` with the trailing
  `[^ ]+` drops pytest's ` - <exception message>` suffix, which appears only at wide terminal widths
  (`COLUMNS=80` omits it, `COLUMNS=250` includes it) and would otherwise make `known-failures.txt`
  width-dependent. It also excludes captured `ERROR`-level **log lines**, which start at column 0 with
  multiple spaces and would otherwise enter the diff carrying UUIDs and timestamps. Verified against
  synthetic output containing all four shapes: 3 real outcomes matched, the log line excluded.
- **Per-invocation temp files.** Fixed `/tmp` paths let concurrent runs read each other's results.
- **Tracked fixtures plus a completion floor.** Both baseline files live under `tests/baselines/`,
  not ignored `.genie/`. The terminal summary must exist and its completed outcome count may grow but
  may not fall below the exact branch-point floor, so deleting unrelated passing tests is visible.
- **`static_gate` names `packages` in all four tools, not just `black`.** Plan Review #3 found the
  previous version checking `black` on `packages` while running `isort`, `flake8` and `mypy` on
  `src tests` only — so the artifact this wish exists to produce was outside three of the four static
  checks. Note that naming the package path here does **not** substitute for `py.typed`: it fixes
  *our* type-checking, while the marker fixes every consumer's (Decision 10).

`core_suite_gate` uses a ≥1006 floor rather than a fixed count because later groups add tests.
`full_suite_gate` matches `ERROR` as well as `FAILED` because a broken import interrupts collection
with **zero** `FAILED` lines — the precise failure mode Groups 2 and 3 exist to catch — and its `diff`
is bidirectional, so a known failure silently vanishing is caught too. `static_gate` exists because
`make quality` is `format-check lint typecheck test` and its `test:` target is `pytest tests/ -v`,
which exits 1 on the known failures: **`make quality` is red at baseline and cannot be used as a gate
in a `set -e` block.** Note that `pyproject.toml:102` sets `addopts = "-v --tb=short --strict-markers"`,
so `-q` nets to verbosity 0 rather than true quiet mode; the `^=+ [0-9]+ passed` banner is present
either way (verified), but do not "simplify" these greps on the assumption that output is quiet.

## Execution Groups

**Execution precondition (orchestrator-owned):** fetch `origin`, create
`wish/neurostack-core-extraction` from the intended current `origin/main`, and record that exact SHA
in `tests/baselines/neurostack-core-extraction-branch.txt`. No group may claim work from the
historical `wish/clinic-service-modality` checkout or reuse its failure baseline.

### Group 1: Package skeleton, build tooling, `py.typed` and boundary guard

**Goal:** Stand up an installable but empty `neurostack-core` that the app already depends on, plus every gate later groups rely on.

**Deliverables:**
1. `packages/neurostack-core/pyproject.toml` — distribution `neurostack-core`, version `0.1.0`,
   `python = "^3.11"`, runtime deps limited to `pydantic`, `prometheus-client` and `tzdata`, plus
   the package-data include for `py.typed`.
2. `packages/neurostack-core/neurostack_core/__init__.py` exporting `__version__`.
3. Root `pyproject.toml` gains `neurostack-core = {path = "packages/neurostack-core", develop = true}`
   **and `build` in `[tool.poetry.group.dev.dependencies]`** (absent today, and Group 5 needs it);
   coverage source includes both `src` and `packages/neurostack-core/neurostack_core`; refreshed
   `poetry.lock`.
4. `scripts/wish-gates.sh` with `core_suite_gate`, `full_suite_gate` and `static_gate`.
5. Three tracked branch-point fixtures: `tests/baselines/neurostack-core-extraction-branch.txt`,
   `tests/baselines/neurostack-core-extraction-known-failures.txt`, and
   `tests/baselines/neurostack-core-extraction-outcome-floor.txt`. The latter two are generated from
   one completed full-suite run, never hand-written or copied from this document.
6. `scripts/validate-pytest-completion.py` plus focused tests. It requires a terminal pytest summary,
   reports the completed outcome count for baseline capture, and rejects a count below a supplied
   floor. `full_suite_gate` separately accepts only pytest exit 0 or 1 and rejects 2–5.
7. `scripts/check-package-boundary.py` plus focused tests. It parses every `.py` under the supplied
   package root and rejects all `ast.Import`/`ast.ImportFrom` references to `src`, including bare and
   aliased forms, plus constant-string `importlib.import_module("src...")` and
   `__import__("src...")` calls.
8. `packages/neurostack-core/neurostack_core/py.typed` — empty PEP 561 marker (Decision 10), plus the
   package-data include that ships it in the wheel. Without this file the package is `Any` to every
   consumer that installs it, and `ignore_missing_imports = true` hides that fact.
9. `packages/neurostack-core/neurostack_core/__init__.py` also pre-declares `enable_metrics`, so
   Group 4 adds an implementation rather than a new top-level name (Plan Review #4, H1).
10. `.gitignore` narrowly unignores the extraction and successor WISH files; both are versioned, and
    `.genie/INDEX.md` records `neurostack-core-extraction` blocking
    `neurostack-core-dedomainification`. No executable fixture remains under `.genie/`.

**Acceptance Criteria:**
- [ ] `poetry install` succeeds; `poetry run python -c "import neurostack_core"` exits 0.
- [ ] `poetry run python -c "import build"` exits 0.
- [ ] The package pyproject declares no dependency beyond `pydantic`, `prometheus-client` and
      `tzdata`; `ZoneInfo("America/Sao_Paulo")` resolves in the Poetry environment.
- [ ] The branch fixture equals `git rev-parse HEAD` at the clean branch point. The failure and
      outcome fixtures byte-match a fresh completed run. A clean failure list is valid; an empty or
      truncated terminal summary is never valid.
- [ ] `core_suite_gate`, `full_suite_gate` and `static_gate` all pass with nothing moved yet.
- [ ] **Each gate is proven to fail when it should** (Decision 9): `full_suite_gate` against an
      injected extra failure, `core_suite_gate` against a failure injected **inside `tests/srm/`** —
      its own selection, not `tests/_gateprobe`, which `core_suite_gate` never collects — and the
      full gate against pytest exit 4, a missing terminal summary, and an outcome count below floor.
- [ ] Boundary tests cover `import src`, `import src.foo as x`, `from src import foo`,
      `from src.foo import bar`, both supported dynamic-call forms, multiline imports and a clean
      relative import. The checker fails every forbidden case and passes the clean case.
- [ ] Both WISH files are visible to `git status`/`git ls-files`, the two inventory documents agree
      that 619/18 is only the successor's pre-extraction estimate, and `.genie/INDEX.md` records the
      dependency direction.
- [ ] **`py.typed` is proven load-bearing** (Decision 10): with the marker removed,
      `poetry run mypy --no-incremental src/_typed_probe.py` reports `Success`; with it restored, the
      same command reports the error. The command must **not** name the package path — Plan Review #4
      measured that naming it makes mypy analyse the package as source and report the error either
      way, which would make this criterion unsatisfiable.

**Validation:**
```bash
set -eo pipefail
source scripts/wish-gates.sh

# First action after the two gate scripts/tests exist, before changing Poetry or production source.
# These tracked files are never regenerated later merely to make a changed tree green.
BASELINE_OUT=$(mktemp "${TMPDIR:-/tmp}/core-extraction-baseline.XXXXXX")
trap 'rm -f "$BASELINE_OUT"' EXIT
git rev-parse HEAD > tests/baselines/neurostack-core-extraction-branch.txt
set +e
poetry run pytest -q -p no:cacheprovider 2>&1 | tee "$BASELINE_OUT"
BASELINE_RC=${PIPESTATUS[0]}
set -e
case "$BASELINE_RC" in 0|1) ;; *) echo "FAIL: branch-point pytest exit $BASELINE_RC"; exit 1 ;; esac
grep -oE '^(FAILED|ERROR) [^ ]+' "$BASELINE_OUT" | LC_ALL=C sort \
  > tests/baselines/neurostack-core-extraction-known-failures.txt || true
poetry run python scripts/validate-pytest-completion.py --print-outcomes "$BASELINE_OUT" \
  > tests/baselines/neurostack-core-extraction-outcome-floor.txt

poetry install
poetry run python -c "import neurostack_core; print(neurostack_core.__version__)"
poetry run python -c "import build"
poetry run python -c 'from zoneinfo import ZoneInfo; ZoneInfo("America/Sao_Paulo")'

# tracked baseline provenance and gate-parser tests
test "$(cat tests/baselines/neurostack-core-extraction-branch.txt)" = "$(git rev-parse HEAD)"
if git check-ignore -q .genie/wishes/neurostack-core-extraction/WISH.md; then exit 1; fi
if git check-ignore -q .genie/wishes/neurostack-core-dedomainification/WISH.md; then exit 1; fi
poetry run pytest tests/scripts/test_check_package_boundary.py \
  tests/scripts/test_validate_pytest_completion.py -q
poetry run python scripts/check-package-boundary.py packages/neurostack-core

# gates must fire: a planted failing test has to break full_suite_gate
mkdir -p tests/_gateprobe && printf 'def test_planted_failure():\n    assert False\n' > tests/_gateprobe/test_probe.py
if full_suite_gate; then rm -rf tests/_gateprobe; echo "FAIL: full_suite_gate ignored a new failure"; exit 1; fi
rm -rf tests/_gateprobe

# core_suite_gate only ever collects tests/{srm,flow,policy,prompt}, so its probe must live there
printf 'def test_planted_failure():\n    assert False\n' > tests/srm/test_zz_gateprobe.py
if core_suite_gate; then rm -f tests/srm/test_zz_gateprobe.py; echo "FAIL: core_suite_gate ignored a new failure"; exit 1; fi
rm -f tests/srm/test_zz_gateprobe.py

# pytest usage/internal/interruption statuses must never be converted into success
if PYTEST_ADDOPTS='--definitely-invalid-option' full_suite_gate; then
  echo "FAIL: full_suite_gate accepted an incomplete pytest run"; exit 1
fi

# py.typed must be load-bearing, not decorative (Decision 10)
printf 'from neurostack_core import __version__\n__version__.no_such_method()\n' > src/_typed_probe.py
mv packages/neurostack-core/neurostack_core/py.typed /tmp/py.typed.bak
if ! poetry run mypy --no-incremental src/_typed_probe.py 2>&1 | grep -q 'Success'; then
  mv /tmp/py.typed.bak packages/neurostack-core/neurostack_core/py.typed; rm -f src/_typed_probe.py
  echo "FAIL: probe is not exercising the py.typed path"; exit 1
fi
mv /tmp/py.typed.bak packages/neurostack-core/neurostack_core/py.typed
if poetry run mypy --no-incremental src/_typed_probe.py 2>&1 | grep -q 'Success'; then
  rm -f src/_typed_probe.py
  echo "FAIL: py.typed present but package still unchecked"; exit 1
fi
rm -f src/_typed_probe.py

core_suite_gate
full_suite_gate
static_gate
```
_Scope: this changes the root lockfile, package typing and every downstream gate, so it escalates to
the full app suite.
The negative probes are the point. Plan Review #2 executed the previous gates and found five that
could never pass. Council Review #1 then found that the `|| true` repair discarded crashes as well as
expected test failures, and that the ignored `.genie/` fixture made clean-checkout execution impossible.
Every gate this wish depends on is therefore proven against a planted violation here, before any
group trusts it. Two probes were added by Plan Review #3, and their form corrected by #4: the `core_suite_gate` probe now lands in
`tests/srm/` because the previous one wrote to `tests/_gateprobe`, a path `core_suite_gate`'s explicit
four-directory selection never collects, so it proved nothing about that gate. The `py.typed` probe is
**bidirectional on purpose** — it asserts the marker's absence makes the error disappear before
asserting its presence brings it back, because a probe that only checks the second half would pass
just as well against an error mypy was reporting for some unrelated reason._

**depends-on:** none

---

### Group 2: Move `srm` into the package

**Goal:** Relocate `src/srm` to `neurostack_core/srm` and repoint every consumer, leaving no copy behind.

**Deliverables:**
1. All 9 files of `src/srm/` moved, with the 20 intra-module imports rewritten to `neurostack_core.srm`.
2. The 4 call-site files outside `src/srm` repointed through the supported
   `neurostack_core.srm` facade: `src/core/pipeline.py`, `src/memory/__init__.py`,
   `src/memory/models.py` (imports `FunnelStage`), `src/services/trace_adapter.py`. Any symbol those
   consumers need is exported in `srm.__all__`; production code does not deep-import private modules.
3. Test imports and the `from src.srm...` examples in `docs/srm-hybrid-classifier.md` (lines 57, 70,
   98, 175, 184) repointed.
4. `src/srm/` deleted.

**Acceptance Criteria:**
- [ ] `grep -rn "from src\.srm" src tests docs` returns no matches.
- [ ] `src/srm/` does not exist.
- [ ] No production file under `src/` imports `neurostack_core.srm.<implementation_module>`.
- [ ] `core_suite_gate`, `full_suite_gate` and `static_gate` pass. `static_gate`'s `isort` check
      matters here specifically: the move relocates imports across isort blocks rather than renaming
      them in place.
- [ ] `mypy` clean on the moved package as well as on `src`.
- [ ] No behavior change: no regex, threshold, intent name or enum value edited. The diff is import
      lines and file paths only.

**Validation:**
```bash
set -eo pipefail
source scripts/wish-gates.sh
test ! -d src/srm
if grep -rn "from src\.srm" src tests docs; then echo "FAIL: leftover src.srm imports"; exit 1; fi
poetry run python scripts/check-package-boundary.py packages/neurostack-core
poetry run mypy packages/neurostack-core/neurostack_core/srm
static_gate
core_suite_gate
full_suite_gate
```
_Scope: `src/memory` and `src/core/pipeline.py` are on the live request path, so the narrow suite is
not enough — `full_suite_gate` catches import breakage anywhere, including the collection errors a
`FAILED`-only grep would miss. `isort` is checked because the move relocates import lines rather than
renaming them in place, which is exactly what `make quality` would flag later._

**depends-on:** 1

---

### Group 3: Move `flow`, `policy` and `prompt` into the package

**Goal:** Relocate the remaining three modules using the recipe Group 2 proves, and turn the boundary guard on for real.

**Deliverables:**
1. `src/flow/` → `neurostack_core/flow/` (26 intra-module imports).
2. `src/policy/` → `neurostack_core/policy/` (15 intra-module imports).
3. `src/prompt/` → `neurostack_core/prompt/`, including the `templates/` subpackage (28 intra-module
   imports).
4. The 3 remaining call-site files repointed through the supported subpackage facades:
   `src/api/routes/policies.py` (7 import lines), `src/core/pipeline.py` (remaining lines of its 12),
   `src/services/business_hours.py`; `resolve_clinic_tz` is exported by `neurostack_core.prompt`.
   Test imports and the `src/prompt/...` references in `docs/prompt-templates-and-funnel.md`
   (lines 58, 167, 168) are also repointed.
5. `src/flow/`, `src/policy/`, `src/prompt/` deleted.
6. `scripts/check-package-boundary.py` wired into the `lint:` target of the `Makefile`, so it runs as part of
   `make quality` without this group having to gate on `make quality` itself — that target chains
   `test:` (`pytest tests/ -v`), which exits 1 on the 2 known failures and is therefore red at
   baseline (Plan Review #2, C4).

**Acceptance Criteria:**
- [ ] `grep -rn "from src\.\(flow\|policy\|prompt\)" src tests docs` returns no matches.
- [ ] None of the three directories exist under `src/`.
- [ ] `neurostack_core.prompt.templates` is importable.
- [ ] No production file under `src/` deep-imports a `neurostack_core` implementation module; all
      application imports resolve through `neurostack_core.{srm,flow,policy,prompt}` facades.
- [ ] `core_suite_gate`, `full_suite_gate` and `static_gate` all pass.
- [ ] `make lint` runs the boundary check and is green, so `make quality` picks it up.
- [ ] No behavior change: diff limited to import lines and file paths.

**Validation:**
```bash
set -eo pipefail
source scripts/wish-gates.sh
test ! -d src/flow
test ! -d src/policy
test ! -d src/prompt
if grep -rn "from src\.\(flow\|policy\|prompt\)" src tests docs; then echo "FAIL: leftover imports"; exit 1; fi
poetry run python -c "import neurostack_core.prompt.templates"
poetry run python scripts/check-package-boundary.py packages/neurostack-core
make lint
static_gate
core_suite_gate
full_suite_gate
```
_Scope: same reasoning as Group 2 — `src/core/pipeline.py` orchestrates all three. Each `test ! -d`
is its own line because chaining them with `&&` under `set -e` lets everything but the last one pass
silently. The explicit templates import guards the one packaging trap here: a subpackage silently
dropped from the wheel. `make lint` replaces the previous `make quality`, which chains a `test:`
target that is red at baseline and would have made this group unpassable._

**depends-on:** 2

---

### Group 4: Opt-in Prometheus metric registration

**Goal:** Stop the package mutating the host's global metric registry at import time, without changing a single metric the app emits.

**Deliverables:**
1. `packages/neurostack-core/neurostack_core/metrics.py` — the 14 `Counter`/`Histogram` objects
   constructed with `registry=None`, so they exist and are fully usable but belong to no registry.
2. `neurostack_core.enable_metrics(registry=None)` — registers those 14 objects into the given
   registry, defaulting to the standard one. A module lock and weak set make repeated calls
   idempotent **per registry** while permitting independent custom registries. Before registering the
   first collector, it compares the whole expected family-name set with the target registry and
   raises `MetricRegistrationError` naming every collision; a collision never leaves a partial set.
3. **No no-op metrics layer.** Verified against `prometheus_client` 0.19.0: a `Counter(...,
   registry=None)` accepts `.inc()` while unregistered and **preserves its accumulated value** when
   later passed to `REGISTRY.register()` (probed: 2 increments before enabling, `2.0` visible after).
   That gives opt-in semantics with no call-site branches and none of the call-order hazard a no-op
   sentinel introduces — a module that captured the no-op at import would register names and count
   nothing.
4. `neurostack-api` calls `enable_metrics()` at the beginning of FastAPI lifespan startup only when
   `settings.metrics_enabled` is true. The `/metrics` ASGI mount may be constructed earlier because
   it reads the registry at request time, but no application request can precede successful lifespan.
5. `tests/srm/test_metrics_optin.py` covers fresh-import isolation, exact family type/label contracts,
   default and two custom registries, repeated enablement, preflight collision atomicity, observations
   made before registration, and the documented fail-closed behavior after a manual metrics-module
   reload. The reload case runs in a fresh subprocess so it cannot poison later registry tests.
   Manual in-process reload is unsupported; process reload remains supported.
6. `tests/api/test_core_metrics_lifespan.py` starts the real app lifespan with its database boundary
   stubbed like existing API tests, requests `/metrics`, sends one request through the event/pipeline
   route, proves `neurostack_srm_decisions_total` increments, repeats app creation/startup, and covers
   metrics-disabled startup in a fresh subprocess.

**Acceptance Criteria:**
- [ ] Importing all four subpackages in a fresh interpreter adds zero `neurostack_*` collectors.
- [ ] After `enable_metrics()`, all 14 names are present with unchanged names, labels and types.
- [ ] `enable_metrics()` is idempotent per registry; two custom registries work; a colliding registry
      fails before partial registration and names the conflict.
- [ ] After enabling, running one `SRMEngine.process(...)` increases `neurostack_srm_decisions` by 1.
      Name presence alone is insufficient: opt-in plus a no-op fallback makes emission depend on call
      order, so a module that captured the no-op would still register names and count nothing.
- [ ] A real FastAPI lifespan and event request expose the same 14 families at `/metrics` and
      increment the decision counter; a second app lifespan does not duplicate them.
- [ ] With metrics disabled, importing and starting the app exports none of the 14 package families.
      The README accurately says disabled metrics may still accumulate process-local observations;
      opt-in controls registration/export, not collection.
- [ ] `static_gate`, `core_suite_gate` and `full_suite_gate` all pass after the runtime change.

**Validation:**
```bash
set -eo pipefail
source scripts/wish-gates.sh
poetry run pytest tests/srm/test_metrics_optin.py tests/api/test_core_metrics_lifespan.py -q
static_gate
core_suite_gate
full_suite_gate
```
_Scope: this changes process-global observability and application startup, so the narrow metric and
core tests are followed by static and full-suite gates. `prometheus_client` 0.19.0 strips the
`_total` suffix from Counters on `collect()` while leaving Histogram `_seconds` names intact; the
contract test records both collector family and exported sample names explicitly._

**depends-on:** 3

---

### Group 5: Wheel build, clean-environment proof and consumer README

**Goal:** Prove the package is consumable by someone outside this repository, in an environment with none of its infrastructure.

**Deliverables:**
1. A built wheel and sdist.
2. `packages/neurostack-core/scripts/smoke.py` — imports all four subpackages and classifies a
   message, with no environment variables and no services.
3. `packages/neurostack-core/README.md` — install, quickstart, public API surface, an explicit "this
   package does not call an LLM, perform network I/O, or durably persist anything", a statement that
   `FlowManager` and `PolicyStore` retain process-local state and expose `reset_session()`/`clear()`
   for caller-owned cleanup, a statement that it is unpublished and installable from source only,
   and a **known limitation** section stating that the engine currently ships pt-BR aesthetic-clinic
   content as built-in data, with a pointer to the `neurostack-core-dedomainification` wish. It also
   states that disabled metrics remain in-memory observations and are merely unregistered/unexported.
4. A root `README.md` pointer for developers who want only the reasoning layer.
5. `scripts/extract-readme-quickstart.py` — invoked by this group's own validation and listed in Files
   to Create, but previously owned by no group. Contract: read the README, emit the contents of the
   **first** fenced block tagged ```` ```python ```` that follows the `## Quickstart` heading, and
   exit non-zero if no such block exists, so a README that loses its quickstart fails loudly rather
   than silently executing an empty file.
6. `packages/neurostack-core/scripts/check_public_api.py` — carries the exact provisional symbol set
   for each of `neurostack_core.{srm,flow,policy,prompt}`, asserts `__all__` equality, imports every
   named symbol, and is executed with the installed wheel's interpreter outside the repository.

**Acceptance Criteria:**
- [ ] `python -m build` targets a fresh temporary output directory and produces exactly one new wheel
      and one sdist with no warnings about missing files; no persistent/stale `dist/` is consulted.
- [ ] The wheel installs into a fresh virtualenv created outside the repository tree.
- [ ] That virtualenv contains no `fastapi`, `sqlalchemy`, `redis`, `asyncpg` or `qdrant-client`.
- [ ] All four subpackages **import** from the installed wheel — archive presence is not importability.
- [ ] Every documented facade symbol imports from the installed wheel and each facade's `__all__`
      equals the reviewed provisional contract; package deep modules are documented private.
- [ ] `py.typed` and the `prompt/templates` subpackage are present in the wheel, and all four
      template modules ship — not merely the directory entry.
- [ ] The smoke script runs with an empty environment and prints
      `intent=agendar category=scheduling stage=em_qualificacao`.
- [ ] The README quickstart is extracted programmatically and executed in the clean virtualenv — not
      attested by the engineer.
- [ ] `ZoneInfo("America/Sao_Paulo")` resolves in the clean environment through the wheel's declared
      dependency closure.
- [ ] `static_gate`, `core_suite_gate` and `full_suite_gate` pass on the final package artifact tree.

**Validation:**
```bash
set -eo pipefail
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/dist" "$TMP/wheelhouse"
poetry run python -m build --no-isolation --outdir "$TMP/dist" packages/neurostack-core 2>&1 | tee "$TMP/build.log"
if grep -iE "warning.*(missing|not found)" "$TMP/build.log"; then echo "FAIL: build warned about missing files"; exit 1; fi
mapfile -t WHEELS < <(find "$TMP/dist" -maxdepth 1 -type f -name '*.whl' -print)
mapfile -t SDISTS < <(find "$TMP/dist" -maxdepth 1 -type f -name '*.tar.gz' -print)
test "${#WHEELS[@]}" -eq 1
test "${#SDISTS[@]}" -eq 1
WHEEL=${WHEELS[0]}
unzip -l "$WHEEL" | grep -q 'neurostack_core/py.typed'
for t in base handoff normal safety; do
  unzip -l "$WHEEL" | grep -q "prompt/templates/$t\.py" || { echo "FAIL: template $t missing from wheel"; exit 1; }
done
python3 -m venv "$TMP/bootstrap"
"$TMP/bootstrap/bin/pip" wheel --wheel-dir "$TMP/wheelhouse" "$WHEEL"
python3 -m venv "$TMP/venv"
"$TMP/venv/bin/pip" install --no-index --find-links "$TMP/wheelhouse" neurostack-core==0.1.0
if "$TMP/venv/bin/pip" list --format=freeze | grep -Eiq '^(fastapi|sqlalchemy|redis|asyncpg|qdrant-client)='; then echo "FAIL: platform dependency leaked into wheel"; exit 1; fi
"$TMP/venv/bin/python" -c "import neurostack_core.srm, neurostack_core.flow, neurostack_core.policy, neurostack_core.prompt, neurostack_core.prompt.templates"
"$TMP/venv/bin/python" -c 'from zoneinfo import ZoneInfo; ZoneInfo("America/Sao_Paulo")'
"$TMP/venv/bin/python" packages/neurostack-core/scripts/check_public_api.py
env -i LC_ALL=C.UTF-8 "$TMP/venv/bin/python" packages/neurostack-core/scripts/smoke.py | tee "$TMP/smoke.txt"
grep -q 'intent=agendar category=scheduling stage=em_qualificacao' "$TMP/smoke.txt"
poetry run python scripts/extract-readme-quickstart.py packages/neurostack-core/README.md > "$TMP/quickstart.py"
env -i LC_ALL=C.UTF-8 "$TMP/venv/bin/python" "$TMP/quickstart.py"
static_gate
core_suite_gate
full_suite_gate
```
_Scope: this is the release-artifact gate, so it validates the built wheel rather than the working
tree — the two differ precisely on packaging bugs. `env -i` strips the environment to prove the
no-`.env` claim rather than asserting it; `LC_ALL=C.UTF-8` is preserved because the smoke input is
Portuguese. The dependency-leak check is what would catch the whole extraction being cosmetic, and
`trap` ensures the temp venv is removed even when an earlier assertion aborts under `set -e`.
`--no-isolation` prevents build-time dependency fetching. Dependency wheels may be materialized once
into the temporary wheelhouse; the actual clean install is then `--no-index`, proving a closed and
repeatable install set. The per-template loop replaced a single directory-prefix grep that one match
satisfied. The aggregate gates make this the first complete package handoff, not only an archive test._

**depends-on:** 4

---

### Group 6: Application install paths and production image

**Goal:** Make every supported way of installing or deploying `neurostack-api` consume the extracted
package, and prove the production artifact can import and serve it.

**Deliverables:**
1. `Dockerfile` copies the package manifest, README and source into the dependency stage **before**
   `poetry install`. Because the root dependency is editable, the production stage copies
   `packages/neurostack-core` to the same `/app/packages/neurostack-core` path referenced by the
   installed link. `.dockerignore` explicitly includes `packages/neurostack-core/README.md`, which
   the nested pyproject declares as package metadata. A build-layer change that merely makes
   installation pass but leaves a dangling runtime path is unacceptable.
2. `docker-compose.yml` mounts `./packages:/app/packages` for development reload alongside `./src`.
   `docker compose config` must resolve both mounts.
3. `requirements.txt` installs `-e ./packages/neurostack-core` from the repository root;
   `requirements-dev.txt` inherits that line through its existing `-r requirements.txt`. Comments
   state that running either requirements command outside the repository root is unsupported.
4. `README.md`, `SETUP.md` and `start-dev.sh` use/test the same Poetry and requirements contracts;
   the root README links the package README without calling the package domain-neutral or published.
5. `Makefile` includes `packages` in format/lint/type/coverage targets, exposes a non-destructive
   `docker-core-smoke` target that builds a uniquely tagged production image, and documents that the
   full test target remains baseline-red until its two pinned failures are repaired.
6. Production-image smoke uses a random host port and starts Uvicorn with `--lifespan off` only to
   prove the immutable image imports `src.api.main`, imports all four `neurostack_core` facades and
   serves `/live` without infrastructure. Group 4's integration test remains the authoritative real
   lifespan and `/metrics` proof; disabling lifespan here avoids coupling an image-import check to
   developer databases or fixed Compose container names.

**Acceptance Criteria:**
- [ ] `poetry install --only main` and `poetry install` both make `neurostack_core` importable.
- [ ] Fresh production and development pip virtualenvs installed from their respective requirements
      files import all four facades and resolve `America/Sao_Paulo`.
- [ ] `docker compose config` shows both `/app/src` and `/app/packages` mounts for the API service.
- [ ] `docker build --target production` succeeds from a clean build context; the resulting image
      imports the app and all four facades, contains `py.typed`, and has no dangling editable path.
- [ ] The uniquely tagged image serves `/live` on a random loopback port with `--lifespan off` and is
      removed by the validation trap. No existing named Compose service or fixed host port is touched.
- [ ] The real-lifespan metrics integration test from Group 4, `static_gate`, `core_suite_gate` and
      `full_suite_gate` all pass again at final handoff.

**Validation:**
```bash
set -eo pipefail
source scripts/wish-gates.sh
TMP=$(mktemp -d)
IMAGE="neurostack-api:core-extraction-$(git rev-parse --short HEAD)"
CID=""
cleanup() {
  test -z "$CID" || docker rm -f "$CID" >/dev/null 2>&1 || true
  docker image rm "$IMAGE" >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT

poetry install --only main
poetry run python -c "import neurostack_core.srm, neurostack_core.flow, neurostack_core.policy, neurostack_core.prompt"
poetry install

python3 -m venv "$TMP/prod-venv"
"$TMP/prod-venv/bin/pip" install -r requirements.txt
"$TMP/prod-venv/bin/python" -c 'from zoneinfo import ZoneInfo; import neurostack_core.srm, neurostack_core.flow, neurostack_core.policy, neurostack_core.prompt; ZoneInfo("America/Sao_Paulo")'
python3 -m venv "$TMP/dev-venv"
"$TMP/dev-venv/bin/pip" install -r requirements-dev.txt
"$TMP/dev-venv/bin/python" -c "import neurostack_core"

docker compose config > "$TMP/compose.yml"
grep -q '/app/src' "$TMP/compose.yml"
grep -q '/app/packages' "$TMP/compose.yml"
docker build --target production --tag "$IMAGE" .
docker run --rm --entrypoint python "$IMAGE" -c \
  'import pathlib, src.api.main, neurostack_core.srm, neurostack_core.flow, neurostack_core.policy, neurostack_core.prompt; assert pathlib.Path("/app/packages/neurostack-core/neurostack_core/py.typed").is_file()'
CID=$(docker run -d --publish 127.0.0.1::8000 --entrypoint uvicorn "$IMAGE" \
  src.api.main:app --host 0.0.0.0 --port 8000 --lifespan off --no-access-log)
PORT=$(docker port "$CID" 8000/tcp | awk -F: '{print $NF}')
for _ in $(seq 1 30); do curl --fail --silent "http://127.0.0.1:$PORT/live" && break; sleep 1; done
curl --fail --silent "http://127.0.0.1:$PORT/live" >/dev/null

poetry run pytest tests/api/test_core_metrics_lifespan.py -q
static_gate
core_suite_gate
full_suite_gate
```
_Scope: path dependencies change every installation and deployment path. This group therefore uses
fresh Poetry/pip installs, resolved Compose configuration, an actual production image and server,
the real-lifespan integration test, and the final aggregate gates. The random tag/port and cleanup
trap avoid mutating existing developer services._

**depends-on:** 5

---
## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Functional: a WhatsApp message through the live pipeline classifies intent, funnel stage and
      safety identically to pre-merge — spot-check a scheduling message, a price question and a
      safety-triggering message.
- [ ] Functional: generated prompts are unchanged — compare a rendered prompt for a fixed session
      against a pre-merge capture.
- [ ] Integration: app boots, `/health` green, `/metrics` exposes all 14 `neurostack_*` series with
      unchanged names and labels, **and the decision counter increases after real traffic**.
- [ ] Integration: the production Docker image contains the editable package target, imports every
      provisional public facade and serves `/live`; the development Compose config mounts `packages`.
- [ ] Integration: clean requirements-based production and development environments both import the
      package; the installed wheel resolves `America/Sao_Paulo` without relying on host zone data.
- [ ] Integration: handoff fires on the same conditions, confirming `src/memory` and
      `src/core/pipeline.py` consume the relocated `FunnelStage` correctly.
- [ ] Regression: `alembic_version` unchanged after deploy; this wish introduces no migration.
- [ ] Regression: cognitive-pipeline latency unchanged — the move adds no indirection.
- [ ] Boundary: `make lint` on dev runs the package-boundary check and is green (`make quality`
      chains an always-red `test:` target, so it is not the gate).

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| **The gate scripts are themselves unverified code.** Plan Review #2 found five gates that could never pass; #3 found a probe written to a directory its gate never collects; #4 found a `py.typed` criterion that was unsatisfiable; Council #1 found the repaired full gate could accept interrupted runs | High | Decisions 9 and 13: Group 1 proves focused/full/boundary/completion gates against planted violations, rejects pytest exits 2–5, requires a terminal outcome floor, and stores fixtures under tracked `tests/baselines/`. |
| **The path dependency installs locally but breaks Docker or pip deployments** | High | Decision 12 and Group 6 own `Dockerfile`, Compose, requirements, setup docs and start scripts; final validation builds/imports/serves the production image and installs both requirements files in fresh venvs. |
| **The package is invisible to `mypy` and nobody notices**, because `ignore_missing_imports = true` (`pyproject.toml:87-90`) suppresses the one diagnostic that would say so | High | Decision 10: ship `py.typed`, name the package path in `static_gate`, and prove the marker load-bearing with a bidirectional probe in the site-packages-resolved form. Measured without it: a planted bogus attribute yields `Success: no issues found`. |
| Moving 11,233 lines produces a diff too large to review, hiding an accidental behavior edit | High | Groups 2 and 3 are pure moves — acceptance forbids editing any regex, threshold or enum. Review with rename detection and run both gates at every step. |
| `make quality` is red at baseline — `quality: format-check lint typecheck test` and `test:` is `pytest tests/ -v`, which exits 1 on the 2 known failures — so any group gating on it inherits a pre-existing failure | Medium | `static_gate` runs the three green checks plus `mypy`; the boundary check is wired into `lint:` so `make quality` still picks it up without any group shelling to it under `set -e`. |
| The extraction decays back into coupling once merged | Medium | Decision 14's AST boundary check runs in `make lint`; focused tests prove every static import form and the two supported dynamic forms fail before the check is trusted. |
| Metric names drift, register too late, collide, or split across registries/reloads | High | Decision 15 and Group 4 define per-registry semantics, preflight collisions atomically, document reload as unsupported/fail-closed, and test real lifespan, `/metrics`, real traffic, disabled mode, repeated apps and custom registries before full-suite validation. |
| Disabled metrics still retain high-cardinality label observations in memory | Medium | README states opt-in controls export, not collection. No new no-op/multiplexer is introduced; callers that cannot tolerate accumulation must not exercise the engines in that process. `tenant_id` label behavior is unchanged from the app. |
| The README falsely promises a stateless package | Medium | Decision 16 and Group 5 document process-local `FlowManager` session state and `PolicyStore` state plus their existing cleanup methods; only durable persistence and network I/O are denied. |
| Timezone behavior works on the developer host but fails in a minimal image | High | Decision 17 makes `tzdata` a runtime dependency; clean wheel, pip and production-image checks all resolve `America/Sao_Paulo`. |
| The wheel builds locally but ships incomplete | Medium | Group 5 inspects wheel contents per template module and for `py.typed`, imports every subpackage from the installed venv, and runs the README quickstart programmatically. |
| A parallel Codex agent commits to this repository concurrently, moving the failure baseline | Medium | Three tracked fixtures record branch SHA, known failures and completed-outcome floor from one branch-point run; `full_suite_gate` diffs bidirectionally and rejects outcome loss. |
| The package ships installable but still pt-BR-specific, and someone treats it as domain-neutral | Medium | Group 5's README carries an explicit known-limitation section naming the successor wish. This wish's Summary and Scope OUT say the same. The limitation is **known and accepted**, not hidden — which is the substantive difference from the four-times-blocked version of this plan. |
| Version `0.1.0` and the distribution name get treated as public before the PyPI wish decides | Low | The README states it is unpublished and installable from source only. |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### 2026-09-25 — Plan Review #1 — **FIX-FIRST**

**Target:** `WISH.md` @ `git hash-object e7a27f1bb129275d0b2de2f9d212d7e1e265c5d6`
**Reviewers:** independent plan reviewer + questioner advisory lens (author did not self-review)
**Environment:** branch `wish/clinic-service-modality` @ `3545cfb`, Python 3.12.3, `prometheus_client` 0.19.0

**Commands run and outcomes (re-verified by the orchestrator):**

| Command | Outcome |
|---------|---------|
| `poetry run pytest -q -p no:cacheprovider` | `2 failed, 2733 passed, 14 skipped in 114.46s` |
| `poetry run mypy src` | `Success: no issues found in 164 source files` |
| `poetry run black --check src tests` | `264 files would be left unchanged` |
| `poetry run pytest tests/srm tests/flow tests/policy tests/prompt -q` | `1006 passed` |
| `grep -n Status .genie/wishes/backend-test-baseline-repair/WISH.md` | `SHIPPED` |

**Gaps and disposition:**

| ID | Sev | Gap | Disposition |
|----|-----|-----|-------------|
| C1 | CRITICAL | The premise that the suite is red (~253 failures) is **false**; the baseline/`comm` apparatus worked around a condition that no longer exists, and Group 1's "baseline must be non-empty" would hard-block execution. Root cause: a stale agent memory, since corrected. | Fixed — Decision 11, `full_suite_gate`, `known-failures.txt` pinned by name with a bidirectional `diff`. |
| C2 | CRITICAL | `! grep` under `set -e` is a no-op; three leftover-import guards could never fail. | Fixed — all converted to `if grep ...; then exit 1; fi`. |
| C3 | CRITICAL | `grep '^FAILED'` is blind to collection errors, the exact failure mode Groups 2/3 exist to catch. | Fixed — `full_suite_gate` matches `^(FAILED\|ERROR)`. |
| H1 | HIGH | `pytest \| tee` without `pipefail` plus unanchored `grep -q "1006 passed"` passes on `3 failed, 1006 passed`. | Fixed — `set -eo pipefail` everywhere, anchored `^=+ [0-9]+ passed`. |
| H2 | HIGH | Groups adding tests then asserting exactly 1006 is wrong by construction. | Fixed — `core_suite_gate` asserts zero failures and a ≥1006 floor. |
| H3 | HIGH | `test ! -d a && test ! -d b && ...` under `set -e`: only the last can fail. | Fixed — one `test` per line in Group 3. |
| H4 | HIGH | `build` absent from dev deps; `cd packages/... && poetry run` targets the package's own empty project. | Fixed — `build` added in Group 1; Group 9 builds from repo root with `--outdir`. |
| H5 | HIGH | Wave 2 parallelism conflicts under isort (`known_first_party = ["src"]` hoists `neurostack_core.*` into another block); no group validated isort. | Fixed — Group 3 serialized behind Group 2; `isort --check-only` added to both. |
| M1 | MEDIUM | 20 English patterns contain a literal `'`, which single-quoted TOML literals cannot escape. | Fixed — Decision 4 mandates `'''…'''`; verified to cover all 402 patterns. |
| M2–M4 | MEDIUM | Parity omitted keywords (216) and English pattern total (150); no flag parity; the grep guard missed 10 inline `re.compile(r"…")` forms. | Fixed in Group 5. |
| M5 | MEDIUM | `comm` was locale-dependent and one-directional. | Moot — replaced by bidirectional `diff` in `full_suite_gate`. |
| M6 | MEDIUM | `docs/` code examples reference `from src.srm...`; greps were scoped to `src tests`. | Fixed — `docs` added to greps and to Groups 2/3 deliverables. |
| M7–M8 | MEDIUM | Archive presence ≠ importability; two criteria were attestable rather than testable. | Fixed in Group 9 — installed-venv imports, build-log grep, programmatic README execution. |
| Q1 | HIGH | **(questioner)** Group 5's adoption justification collapsed: `FunnelStage`, prompt templates and policy rules remain domain-locked. | Fixed by user decision — scope expanded to full de-domainification (Groups 6, 7, 8) and Decision 7 records why partial is the worst outcome. |
| Q2 | HIGH | **(questioner)** "Enforced boundary" had no enforcement. | Fixed — Decision 9 plus `check-package-boundary.sh`, proven against a planted violation in Group 1. |
| Q3 | HIGH | **(questioner)** Group 4 asserted name presence, not that a counter increments. | Fixed — increment assertion added to Group 4 and to QA. |
| Q4 | MEDIUM | **(questioner)** No external consumer exists; a cheap demand experiment was proposed first. | Accepted risk by user decision — proceed with the full extraction. |
| Q5 | MEDIUM | **(questioner)** Group 6's "Adoption" QA criterion was unsatisfiable for an unpublished wheel. | Fixed — criterion removed; README states source-only installation. |
| L1 | LOW | Call-site counts wrong. | Fixed — re-measured: 6 files, 23 import lines (`api/routes/policies.py` 7, `core/pipeline.py` 12, and 1 each in `memory/__init__.py`, `memory/models.py`, `services/business_hours.py`, `services/trace_adapter.py`). |
| L2–L5 | LOW | Tests not moved; `$TMP` leak; `env -i` strips locale; branch not cut. | Fixed — test relocation now explicit in OUT; `trap` added; `LC_ALL=C.UTF-8` preserved; branch-point measurement required by Group 1. |

**Verified correct, carried forward unchanged:** Group 4's expected metric-name set in collected form
(`_total` stripped from the 10 Counters, the 4 Histograms unchanged — confirmed against
`prometheus_client` 0.19.0); the four metric line references; pattern counts; zero outbound coupling
and the 11,233-line figure; that Groups 2/3 each leave a consistent tree at their commit boundary;
and all four settled decisions.

**Amendments beyond the review, from user decisions:** de-domainification expanded from patterns only
to patterns + funnel + templates + policy rules (Groups 6–8 are new); Decision 8 added after
discovering `FunnelStage` values are persisted in session state and duplicated at
`src/api/schemas/sessions.py:16`; ~~policy rule count corrected from 8 to 4 by measurement~~ —
**this amendment was wrong and is superseded by Plan Review #2 (C2): `get_global_rules()` returns 8.**

**Next step:** re-review required before `work`. This document has changed substantially since the
reviewed SHA.

---

### 2026-09-25 — Plan Review #2 — **FIX-FIRST** (fixes applied; re-review pending)

**Target:** `WISH.md` @ `git hash-object c93475f8c8910580bc603bda65d18ff50b16a78f`
**Reviewer:** independent plan reviewer, read-only, no repo mutation (author did not self-review)
**Environment:** branch `wish/clinic-service-modality` @ `3545cfb`, `origin/main` @ `a6d6e65`,
Python 3.12.3, pytest 7.4.4, `prometheus_client` 0.19.0

This review **executed** the gates rather than reading them. That is what distinguishes it from
Review #1: five gates in the reviewed document could never pass, two of them for reasons already
present in the repo.

**Commands run and outcomes (re-verified by the orchestrator before applying fixes):**

| Command | Outcome |
|---------|---------|
| `full_suite_gate` verbatim under `set -eo pipefail` | `EXIT=1`, **never reached its own `diff`** |
| `set -eo pipefail; (exit 1) \| tee /dev/null; echo REACHED` | prints nothing — confirms the abort |
| same pipeline with `\|\| true` | `REACHED`; still exits 1 on an injected new failure |
| `get_global_rules()` in a live interpreter | **8 rules**; id field is `rule_id`; `r.id` → `AttributeError` |
| mypy on `Enum("FunnelStage", …, type=str)` | `error: "type[FunnelStage]" has no attribute "LEAD_NOVO"` |
| same, with a checked-in `.pyi` stub | `Success: no issues found in 1 source file`; runtime members still preset-derived |
| pydantic round-trip on a functional enum | `"standby"` → member → `{"stage":"standby"}`; JSON-schema `enum` unchanged |
| `grep -rciE "botox\|harmoniza\|peeling\|clareamento\|est[eé]tic" src/prompt/templates/*.py` | **0, 0, 0, 0, 0** — Group 7's guard was vacuous |
| pt-BR stopword density, same files | 321 lines (`normal.py` 291, `safety.py` 15, `base.py` 8, `handoff.py` 7) |
| `grep -nE '^(quality\|test):' Makefile` | `quality: format-check lint typecheck test`; `test: pytest tests/ -v` → red at baseline |
| `grep -oE '^(FAILED\|ERROR) [^ ]+'` vs `grep -E`, on synthetic output | 3 real outcomes vs 4 lines — the `-E` form swallowed an `ERROR`-level log line |
| `COLUMNS=80` vs `COLUMNS=250` on the 2 known failures | bare nodeid vs nodeid + ` - AttributeError: …` |
| `Counter(…, registry=None)` then `REGISTRY.register(c)` | unregistered, `.inc()` works, value `2.0` preserved on register |
| Rebuild all 14 metrics, compare `collect()` names to Group 4's set | **MATCH** — the 14-name set is exactly correct |
| Group 4's increment assertion against today's `src.srm` | `before 0 after 1.0` — mechanically valid |
| Intent/pattern/keyword/en/guard counts from `src.srm.intent_classifier` | `29 / 238 / 216 / 29 / 150 / 14`, all guards `flags == 34` |
| `core_suite_gate` verbatim | `1006 passed in 5.09s` — sound |

**Gaps and disposition:**

| ID | Sev | Gap | Disposition |
|----|-----|-----|-------------|
| C1 | CRITICAL | `full_suite_gate` aborted under `pipefail` before its `diff`; Groups 1, 2, 3, 6, 8 could never pass. | Fixed — `\|\| true` on the pytest pipeline, with a note on why it stays fail-closed. |
| C2 | CRITICAL | Group 8 asserted `r.id` (raises `AttributeError`) over 4 rules; there are **8**, keyed by `rule_id`. Review #1's own "8 → 4" amendment was the error. | Fixed — all 8 ids pinned, `rule_id`, severities asserted, deliverables widened, Review #1's note struck through. |
| C3 | CRITICAL | Group 6's preset-derived enum breaks `mypy src` at 130 call sites; Group 6 ran no mypy, so it would hand off a tree failing `make quality` with its own gate green. | Fixed — Decision 8 rewritten around a checked-in `funnel.pyi` in a dedicated module, verified against mypy and pydantic; Group 6 now runs `static_gate`, asserts member **names**, and asserts stub/preset agreement. |
| C4 | CRITICAL | Group 3 gated on `make quality`, which chains an always-red `test:` target. | Fixed — `static_gate` introduced; the boundary check moves into `lint:` so `make quality` still covers it. |
| C5 | CRITICAL | Group 5 imported `neurostack_core.srm.presets`, which no group creates. | Fixed — corrected to `neurostack_core.presets` (Group 1, Decision 12) and the returned object's surface pinned. |
| H1 | HIGH | Group 7's domain grep matched 0 lines in all 5 template files — the group could move nothing and pass. | Fixed — pt-BR stopword guard (321-line measured baseline) plus a ≥300-line floor on `templates.toml` so "moved" is distinguished from "deleted". |
| H2 | HIGH | pt-BR regexes survive in `engine.py`, `rag_decider.py`, `handoff_decider.py`, `safety_detector.py` — no group owned them, contradicting Decision 7 and a success criterion. | Fixed — folded into Group 5, which now depends on Group 4 since both touch `engine.py`. New Wave 5. |
| H3+H4 | HIGH | The failure grep harvested `ERROR`-level log lines, and `known-failures.txt` content was terminal-width dependent. | Fixed — `grep -oE '^(FAILED\|ERROR) [^ ]+'`, verified; Group 1 must **generate** the file with that same pipeline. |
| H5 | HIGH | All of Wave 4 calls `core_suite_gate` concurrently against fixed `/tmp` paths. | Fixed — `mktemp` per invocation with a `RETURN` trap. |
| H6 | HIGH | `presets/__init__.py` was a deliverable of no group; four parallel groups would have edited it. Group 6 would have reached into Group 4's `engine.py`. | Fixed — Decision 12 assigns the single loader to Group 1; the stub keeps Group 6 out of all call sites; Wave 4 disjointness re-derived. |
| H7 | HIGH | `scripts/extract-readme-quickstart.py` was invoked by Group 9 but owned by nobody. | Fixed — Group 9 deliverable 5, with its extraction contract and a fail-loud requirement. |
| H8 | HIGH | Groups 4–8 ran no static checks though `black`/`flake8`/`isort`/`mypy` clean is a success criterion. | Fixed — `static_gate` added to all five. |
| M1 | MEDIUM | Group 6's alembic check diffed against a moving `origin/main`. | Fixed — `git merge-base origin/main HEAD`. |
| M2 | MEDIUM | AC required `flags == 34`; validation asserted only `flags & IGNORECASE`. | Fixed — validation tightened to `== 34`. |
| M3 | MEDIUM | `addopts = "-v …"` means `-q` is not true quiet mode. | Fixed — recorded in the preamble so the greps are not "simplified" on a false premise. |
| M4 | MEDIUM | Simplicity Case claimed three mechanisms while the plan shipped four "mirroring" loaders. | Fixed — Decision 12 (one loader, four schemas); Simplicity Case now lists four mechanisms including the stub. |
| M5 | MEDIUM | Group 8 deliverable 2 said "reviewed … and moved on the same basis" — not actionable. | Fixed — stated as a move with a parity assertion. |
| L1 | LOW | `\br"` missed `rf"…"`. | Fixed — `grep -nP '(?<![A-Za-z])[rR][fFbB]?"'`, verified to match `r"`/`rf"` and skip `for_x(`/`thing_r(`. |
| L2 | LOW | Boundary guard matched only `from src.`, not `import src.`. | Fixed — `^\s*(from\|import)\s+src\.`, both forms probed in Group 1. |
| L3 | LOW | Group 7's extra `pytest tests/prompt` was subsumed by `core_suite_gate`. | Fixed — removed. |
| L4 | LOW | `wc -l … -le 5` is trivially true and undercounts a file with no trailing newline. | Fixed — replaced by a `diff` against a fresh extraction. |
| L5 | LOW | "2,060 lines" included `templates/__init__.py`; the four named files sum to 2,043. | Fixed throughout. |
| L6 | LOW | Two `FunnelStage` consumers reach it via `src.api.schemas.sessions`, not `src.srm`. | Fixed — noted in Group 6 so the implementer does not rediscover it. |

**Verified correct, carried forward unchanged:** Group 4's 14-name expected set in collected form and
its increment assertion; all six Group 5 counts (29 / 238 / 216 / 29 / 150 / 14) and `flags == 34`;
Group 6's 11 identifiers and their order; Group 8's three cited line numbers (191, 233, 268);
`core_suite_gate`; the `^(FAILED|ERROR)` rationale; zero cross-module imports among the four modules;
and Wave 3's isort serialization argument, which the reviewer confirmed does **not** extend to any
Wave 4 pair.

**Simplification adopted from the review:** Group 4's no-op metrics layer was replaced by
`Counter(..., registry=None)` construction, which gives opt-in semantics with no call-site branches
and removes the call-order hazard the no-op design introduced.

**Next step:** re-review. All 25 gaps above are addressed in this document, and every new mechanism
(the stub, the unregistered counters, the corrected greps, the `|| true` abort fix) was executed
before being written in — but the fixes themselves have not been independently reviewed.

---

### 2026-09-25 — Plan Review #3 — **BLOCKED** → scope expanded by user decision

**Target:** `WISH.md` @ `git hash-object adc6570253a20c30637a73a132a08a18515bbf07`
**Reviewer:** independent plan reviewer, read-only (author did not self-review)
**Environment:** branch `wish/clinic-service-modality` @ `3545cfb`

Review #3 returned **BLOCKED** — the second consecutive FIX-FIRST, which exhausts the two-loop budget.
Both CRITICALs were reproduced by the orchestrator before any fix was written.

| ID | Sev | Gap | Disposition |
|----|-----|-----|-------------|
| C1 | CRITICAL | The package ships no `py.typed`, and `static_gate` ran `mypy src` only. With `strict = true` + `ignore_missing_imports = true` (`pyproject.toml:87-90`), `neurostack_core` resolves to `Any` and every mypy criterion — including Decision 8's whole reason for existing — passes vacuously. | Fixed — Decision 14; Group 1 deliverable 8; `static_gate` now names the package for `mypy`, `isort` and `flake8`. Reproduced in an isolated venv: a planted `FunnelStage.NO_SUCH_MEMBER` gives `Success: no issues found` without the marker and `[attr-defined]` with it. |
| C2 | CRITICAL | `srm/funnel_detector.py` was owned by no group, yet holds three module-scope tables keyed on `FunnelStage` members. Since `srm/__init__.py:30` imports it, deriving the enum from a non-pt-BR preset would raise `AttributeError` at import — the package would remain pt-BR-only with every count-based criterion green. | Fixed — folded into Group 6 (complexity 4→5) with the TOML shape verified end-to-end, an AST import-time guard, and a foreign two-stage-preset import test. Disjointness re-verified: G4/G7/G8 do not touch the file. |
| H1 | HIGH | `core_suite_gate`'s negative probe wrote to `tests/_gateprobe`, a path its four-directory selection never collects — the probe proved nothing. | Fixed — the probe now lands in `tests/srm/`. |
| H2/H3 | HIGH | `isort`, `flake8` and `mypy` excluded `packages/` while `black` included it. | Fixed in `static_gate`. |

**Counts corrected while fixing, having been wrong in the plan:**

| Claim in the plan | Measured | Note |
|---|---|---|
| 139 `\u00xx` escapes | **302 occurrences across 139 lines** | 139 was a line count. `safety.py` 151, `handoff.py` 148, `base.py` 3; 17 distinct sequences, `ç` = 93. |
| 86 `FunnelStage.` refs in `funnel_detector.py` | **108 import-time references across 86 lines** | Several lines carry more than one. |
| 33 pt-BR transition intent ids | **33 entries over 16 distinct intent ids** | Entry count was right; the intent count was not. |
| "stray regexes at 4 sites" | **94 raw-string literals across 4 files**, 497 including `intent_classifier.py` | Carried over from Review #2's H2; now a per-file table. |

**New finding not raised by the reviewer** — TOML literal strings (Decision 4) pass `\u00xx` through
raw, so the 302 escapes would have stopped decoding on the way into `templates.toml` and reached the
LLM as literal backslash text. Verified both branches on identical input: basic (`"""`) → `atenção`,
literal (`'''`) → `atenção`. Recorded as Decision 15 with a paired absence/presence gate.

**Disposition — user decision, not a third fix attempt.** The orchestrator surfaced the pattern
(each round finding more pt-BR welded into the engine than the last) and offered narrow / split /
expand. The user chose **expand**: cover everything found. Because this is a directed scope change
rather than another failed remediation of the same plan, the fix-loop counter resets; the next review
judges a materially larger plan, not the plan that was blocked.

**Every mechanism added in this round was executed before being written in:** the `py.typed`
bidirectional probe, the funnel TOML round-trip with name resolution and a loud failure on unknown
ids, the scope-aware AST guard (validated on three synthetic files — clean 0, module-scope 1,
class-attribute 1), the per-corpus `grep -c` loop under `set -e`, and the TOML escape-decoding check.

**Next step:** independent re-review of the expanded plan.

---
### 2026-09-25 — Plan Review #4 — **BLOCKED** → wish split by user decision

**Reviewer:** independent plan-review subagent. **Target:** WISH.md at `19c585dc` (1400 lines, 9 groups,
6 waves — the version expanded after Review #3).

**Verdict:** BLOCKED. Fourth consecutive non-SHIP verdict on this plan.

| Sev | Gap | Disposition |
|-----|-----|-------------|
| CRITICAL | C1 — pt-BR literals outside every sweep: `src/flow/handlers/safety.py:21,33` carries Brazilian emergency copy (`CVV: 188`, `SAMU: 192`, `Bombeiros 193`), plus pt-BR in the policy models and the prompt renderer. Groups 5–8 sweep the classifier, funnel, templates and policy rules — none of them reach these files | → Wish B |
| CRITICAL | C2 — `src/srm/models.py:181` `current_funnel_stage: FunnelStage = FunnelStage.LEAD_NOVO` is a class-body default, so it is evaluated at import; `srm/__init__.py:33` imports `models`. Group 6's preset-derived enum would crash on a foreign preset. Independently reproduced: the scope-aware AST guard scores this file 1 | → Wish B |
| HIGH | H1 — `enable_metrics` was never declared in `neurostack_core/__init__.py`, so Group 4's public entry point did not exist | **Fixed in this wish** (Group 1, deliverable 8) |
| HIGH | H2 — `use_preset` ownership is ambiguous across Groups 5–8 | → Wish B |
| HIGH | H3 — no group owns the preset schema file | → Wish B |
| HIGH | H4 — naming the package path on the `mypy` command line makes mypy analyse it **as source**, so `py.typed` is irrelevant for that invocation. Decision 14's claim that "either one alone leaves the package unchecked" was wrong for the CLI half, and Group 1's acceptance criterion as worded could never fail. Independently reproduced: with the path named and no marker, a planted bogus attribute is still reported | **Fixed in this wish** — Decision 10 rewritten; the probe now uses the site-packages-resolved form, where the marker is genuinely load-bearing |
| MEDIUM | M1 AST guard blind spots · M2 non-raw escapes in `patterns.toml` · M3 loader missing-file semantics · M4 policy guard coverage · M5 `renderer.py` ownership and the G6↔G7 stage-key contract · M6 cedilla floor 93 → ~350 · M7 test-file naming | → Wish B |
| LOW | L1 — count correction (`86` was a line count) | → Wish B |
| LOW | L2 — a single directory-prefix grep on the wheel is satisfied by one match | **Fixed in this wish** (Group 5, per-template loop) |
| LOW | L3 — `.gitkeep` handling | → Wish B |
| LOW | L4 — `python -m build` needs `--no-isolation`; the sandbox has no network and `build` would otherwise fetch `poetry-core` into a fresh environment | **Fixed in this wish** (Group 5) |

**Root cause of the four blocked rounds.** The plan bundled a *bounded mechanical* task — move four
directories — with an *open-ended discovery* task — remove all pt-BR domain content from 11,233 lines.
Each review sampled a different part of the corpus and found more, so every fix round widened the
scope instead of closing it. No amount of fixing converges while the second task's extent is unknown.

**Action taken.** The discovery was converted into a measurement: the complete pt-BR surface is
**313 literals across 21 files** (513 escape sequences, 618 lines). The user then chose to split:

| Wish A — this one | Wish B — `neurostack-core-dedomainification` |
|---|---|
| G1 skeleton + `py.typed` + boundary guard | G1 preset loader |
| G2 move `srm` | G2 `srm` — 158 strings / 5 files |
| G3 move `flow`, `policy`, `prompt` | G3 `prompt` — 113 strings / 6 files |
| G4 metrics opt-in | G4 `policy` — 24 strings / 2 files |
| G5 wheel + README | G5 `flow` — 18 strings / 4 files |
| 5 groups, 5 waves, green today | 5 groups, scope **closed** — 21 files measured, not discovered by review |

Wish A is a pure move: no regex, threshold, enum or template may be edited, so the 1006-test parity
oracle is exact rather than approximate. Every CRITICAL and MEDIUM gap above that concerns pt-BR
content moves to Wish B, which now enters review with a closed inventory. The two HIGH gaps and two
LOW gaps that concern the extraction mechanics itself (H1, H4, L2, L4) are fixed in this document.

**Known and accepted:** the package this wish ships is installable but still pt-BR-specific. That
limitation is stated in the Summary, in Scope OUT, and in the README this wish writes — it is not
hidden, which is the substantive difference from the four-times-blocked version of this plan.

**Next step:** independent plan review of this rescoped wish.

---
### 2026-09-25 — Council Review #1 — **REVISE** (amendments applied; independent re-review pending)

**Lenses:** architecture, delivery, product and security supported with conditions; dissent opposed.
The orchestrator resolved the conflict as `revise`: the extraction seam is sound, but independently
corroborated deployment and gate defects made the prior draft unsafe to execute.

**Fresh evidence on the reviewed tree (`3545cfb`):** focused cognitive suite `1006 passed`; full
suite `2 failed, 2733 passed, 14 skipped`; `mypy src` clean across 164 files; four source directories
total 11,233 Python lines. The declared extraction branch did not exist and the checkout was seven
commits behind `origin/main`, so those results are evidence for that tree only.

| Sev | Gap | Disposition in this revision |
|-----|-----|------------------------------|
| CRITICAL | Docker installed the root path dependency before copying `packages/`, production omitted the editable target, and Compose/pip workflows did not expose the package | Decision 12; new Group 6 owns Dockerfile, `.dockerignore`, Compose, requirements, setup docs and production-image/pip proofs |
| HIGH | `full_suite_gate` discarded pytest status and its required fixture lived under ignored `.genie/` | Decisions 9/13; exits 2–5 rejected, terminal outcome floor required, fixtures moved to tracked `tests/baselines/` |
| HIGH | Boundary regex missed legal Python import forms | Decision 14; AST/dynamic-call checker with planted tests |
| HIGH | Metrics validation omitted real lifespan, `/metrics`, repeated apps, custom registries and collisions | Decision 15; Group 4 specifies semantics and runs focused, static, core and full gates |
| HIGH | Extracted timezone helper relied on host IANA data while the app explicitly depended on `tzdata` | Decision 17; dependency and clean-wheel/pip/image probes added |
| HIGH | No supported package API; production consumers deep-imported implementation modules | Decision 16; exact facade `__all__` contract and installed-wheel importer added |
| MEDIUM | README's "does not persist anything" claim contradicted process-local flow/policy stores | Group 5 now denies only durable persistence/network I/O and documents state plus cleanup APIs |
| MEDIUM | Extraction and successor inventories disagreed and neither wish was lifecycle-visible | This wish adopts the successor's 619/18 pre-extraction estimate, requires post-extraction remeasurement, versions both wishes and updates `.genie/INDEX.md` |

**Next step:** independent plan review of this amended draft. No implementation group may start
until that review returns SHIP and the orchestrator records `APPROVED`.

---
### 2026-09-25 — Plan Review #5 — **FIX-FIRST** (fix loop 1/2)

**Reviewer:** independent `genie_reviewer`, read-only. **Target SHA-256:**
`d2a9802edd042ac6877f9ace8abdb2146260fbcbd8fb6c81f6ad9fccdddb3e0f` (1166 lines).
The reviewer inspected the complete wish, repository install paths, Prometheus registration logic,
facades, lifespan, successor wish and current git state. No implementation/build mutation was run.

**Verified strengths:** the problem and IN/OUT scope are concrete; the package is a genuine dependency
leaf; Group 6 covers real deployment breaks; the revised pytest gate correctly distinguishes
completed exit 0/1 from exit 2–5 and adds an outcome floor.

| Sev | Gap | Required correction |
|-----|-----|---------------------|
| HIGH | Groups 1–5 introduce a path dependency before Docker/pip/Compose can resolve it, and moved modules would retain import-time metrics until a later group | Move install-path changes into the group that introduces the dependency; make each moved module's metrics conversion atomic with its move, or prepare the complete unregistered metrics layer first |
| HIGH | Branch fixture only proves `HEAD == fixture`, not named branch at freshly resolved `origin/main`; unignored wishes are not necessarily tracked | Capture fetched `origin/main`, assert branch name and exact HEAD, then `git ls-files --error-unmatch` both wishes and INDEX |
| HIGH | Metric collision preflight covers family names but not Prometheus secondary names, so late `_created`/`_bucket` collisions can leave partial registration | Use one composite collector or otherwise reserve every name atomically; test counter/histogram suffix and last-family collisions with registry unchanged |
| HIGH | ZoneInfo probes can use host `/usr/share/zoneinfo` | Assert `tzdata` metadata and run with `PYTHONTZPATH=""` in wheel, pip and image environments |
| HIGH | Public API expectation is produced by the same group as implementation | Pin an independent branch-point manifest before moves and compare source and installed wheel against it |
| MEDIUM | Dynamic-import criterion is broader than the two constant-call forms checked | Narrow the promise or reject dynamic import APIs for all argument forms |
| MEDIUM | Group 1 pre-declares `enable_metrics` without usable semantics | Add the binding atomically with its complete implementation |

**Advisory lenses:** architecture additionally recommended package-owned isolated tests and a
consumer-driven facade; questioner challenged unproven external-consumer machinery and the stale
literal "2 failures" criterion. Both independently called the multi-registry/reload design
over-engineered for the only current default-registry host.

**Next step:** fix loop 1 simplifies metrics to one process-owned registry, pins a minimal
consumer-driven facade, normalizes the full-suite criterion to the regenerated branch baseline, and
repairs the five blocking plan gaps; then a fresh reviewer re-runs Plan Review.

---
### 2026-09-25 — Plan Review #6 — **FIX-FIRST** (fix loop 2/2 required)

**Reviewer:** fresh independent `genie_reviewer`, read-only. **Reviewed SHA-256:**
`dfc48ae6b25bfff5edc9a441ad51ef08a4c72568d1969743ed0b4a3c29c6b34d`.
The reviewer evaluated only the normative active section; an independent architecture lens confirmed
the package boundary and metrics design but agreed that deployment evidence remained blocking.

| Sev | Gap | Required correction |
|-----|-----|---------------------|
| HIGH | Branch-point capture writes into a missing directory, does not reject a contaminated worktree, and gives no deterministic commands for deriving the fixtures | Create the baseline directory; assert the capture inputs are clean at freshly fetched `origin/main`; name and test deterministic fixture generators; regenerate/diff against the recorded base SHA |
| HIGH | Group 1 parses Compose but does not execute its mounted import, while Groups 2–3 omit fresh Poetry/requirements/Compose/image proof after the risk-bearing move | Define an import-origin contract and one reusable install-surface gate covering Poetry, both requirements files, Compose and the production image; run it after Groups 1–3 and retain final Group 4 probes |
| HIGH | The pinned public API JSON has no independently testable derivation path | Add a branch-point consumer extractor with explicit inputs, alias/deep-import normalization, planted tests, generation command and regeneration diff |
| MEDIUM | Process-global metrics ownership makes mutually exclusive first-owner/reload cases order-dependent | Run those cases in isolated subprocesses; do not add a public reset hook |
| MEDIUM | Success criteria say the wheel contains package artifact tests although the manifest correctly keeps them as root consumer tests | State that tests exercise but are not shipped in the wheel; list the wheel payload precisely |
| MEDIUM | The direct-wish rationale is only implicit | Record why a separate brainstorm/design artifact is unnecessary for this user-directed extraction |

**Verified strengths:** the four-module boundary and minimal consumer facade are coherent; the
single composite Prometheus collector makes suffix-collision registration atomic; `tzdata` metadata
and `PYTHONTZPATH=""` probes are sound; dynamic imports are fully prohibited; the full-suite gate no
longer hard-codes two failures; four sequential waves are appropriate.

**Next step:** final allowed fix loop adds executable capture/install/API-generation contracts and
resolves the three medium consistency gaps, then a new independent reviewer returns the terminal
plan verdict.

---
### 2026-09-25 — Plan Review #7 — **BLOCKED** (terminal after fix loop 2/2)

**Reviewer:** new independent `genie_reviewer`, read-only. **Reviewed SHA-256:**
`237839092993d09ec5932116fbfcc44a1e76f500994c3161de2e0690769ef26c`.
The reviewer inspected only the normative active section and verified the target hash. A fresh
architecture advisory independently returned SHIP; its remaining suggestions were non-blocking.

| Sev | Gap | Diagnosis |
|-----|-----|-----------|
| HIGH | Group 2's mandatory pytest command names `tests/package_artifact`, but Group 3 owns creation of that path and the path is absent at the recorded base | Deterministic execution-order contradiction: Group 2 exits with pytest usage error before reaching its shared gates. Either create the consumer artifact tests in Group 1/2 or remove that path from Group 2 and retain it from Group 3 onward. |

**Verified resolved:** uncontaminated archive capture and deterministic regeneration; AST-derived
facade with validated deep-symbol promotion; editable/non-editable origin semantics; the six-row
install-surface gate after every wave; final image/liveness proof; isolated metrics ownership tests;
artifact payload ownership; direct-wish rationale; timezone and composite-registration contracts.

**Terminal disposition:** the allowed two fix loops are exhausted. Do not execute this plan until a
new revision resolves the ordering defect and a new independent review returns SHIP.

---
### 2026-09-25 — User-approved ordering revision

The user authorized the terminal review's simplest correction as a new plan revision: Group 2 no
longer invokes `tests/package_artifact`, because Group 3 creates and owns that consumer artifact
suite. Groups 3 and 4 retain artifact validation. This simplification invalidates Plan Review #7 as
an execution verdict and requires a fresh independent plan review; it is not a third fix-loop attempt.

---
### 2026-09-25 — Plan Review #8 — **SHIP**

**Reviewer:** fresh independent `genie_reviewer`, read-only. **Timestamp:**
`2026-09-25T21:12:13Z`. **Reviewed full-file SHA-256:**
`1ebd00bd4e7bf3d9f82562ddea49e2d8504d954d00eafbe7e5cfc9b8e0c5a160`.
**Reviewed scope:** normative active section, lines 1–432; active-section SHA-256
`1888fa1bbf7a25b5c1708816dbcb577757581428031a896d180fc41e6767b4ef`.

**Read-only evidence:** exact digest match; complete active-contract inspection; dependency,
artifact-test, public-API and shared-gate trace; confirmation that `origin/main` contains neither
`tests/package_artifact` nor `packages/neurostack-core`; inspection of repository build/deployment
configuration, source consumers, successor wish and index. No installs, builds, Docker operations,
git mutations or cache-producing test runs were performed for this documentation-only revision.

**Findings:** Group 2 no longer invokes the Group 3-owned artifact suite and retains exact source
facade, four parity suites, lifespan metrics, old-directory absence, six-surface install, static,
core and full-suite validation. Group 3 creates and runs the artifact tests; Group 4 repeats them
with terminal image/liveness proof. Dependencies remain `1 -> 2 -> 3 -> 4`; all groups are
independently testable and retain non-zero risk-proportional gates. Provenance/API capture, metrics
isolation, deployment continuity, acceptance traceability, manifest completeness and scope control
all passed review.

**Questioner advisory:** independently returned SHIP with no approval blocker. Its sole advisory was
to route any Group 4 application-startup failure back to the earlier owning group rather than adding
feature work in the validation-only final wave.

**Severity-tagged gaps:** CRITICAL none; HIGH none; MEDIUM none; LOW none.

**Verdict:** **SHIP**. The orchestrator persisted `APPROVED`; `$genie:work` may now begin Group 1.

---
### 2026-09-25 — Group 1 Execution Review #1 — **FIX-FIRST** (fix loop 1/2)

**Context:** uncommitted Group 1 tree at base
`a6d6e65afab2889b83a857bdd2d0db0bd617d228`. **Reviewers:** independent acceptance reviewer plus
independent security/maintainability quality reviewer; neither edited the tree.

**Engineer evidence:** focused scripts/metrics tests `184 passed`; `static_gate` reported pass;
`core_suite_gate` reported `1046 passed` against archived floor `1038`; second-archive baseline
comparison was byte-identical. Poetry main/dev install rows passed before the engineer was
time-boxed during pip main. Pip dev, Compose, image and `full_suite_gate` remain pending orchestrator
validation and are not themselves review findings.

| Sev | Gap | Required correction |
|-----|-----|---------------------|
| HIGH | Public API extractor admits `src.ctx` and `src.core.exceptions`, producing facade modules outside the approved `srm/flow/policy/prompt` move | Restrict extraction to the four approved domains, add out-of-scope negatives, and regenerate both archived fixtures byte-identically |
| HIGH | Boundary checker and API alias analysis only inspect top-level statements | Walk nested scopes with alias-aware handling and add nested static, importlib and builtins `__import__` cases |
| HIGH | `static_gate` omits isort/flake8 and narrows Black/mypy below the approved root/package scope; `py.typed` is not load-bearing | Restore every declared check/scope and add the installed-consumer negative marker probe |
| HIGH | Poetry/pip origin oracle finds a spec without importing, while Compose/image rows use weaker inline assertions; focused shims cannot detect skipped real rows | Import actual package, apply one oracle contract to all six rows, add promised hostile-origin/metadata negatives and command-level Compose/image shims |
| HIGH | Capture CLI names and fixture schema diverge from the normative regeneration commands and manifest | Implement `--tree`, `--base-sha`, `--output` and the four named text fixtures, update every gate reference, and regenerate from the recorded archive |
| HIGH | Successor wish remains ignored and INDEX lacks the explicit extraction-to-dedomainification direction | Narrowly unignore both wishes and record the dependency; orchestrator owns staging/tracking |
| MEDIUM | Install cleanup/phase validation and metrics/install negative matrices are incomplete | Add scoped interruption-safe cleanup, validate the three phase values, and complete owner-direction, exact-family/label and row-failure tests |

**Verified strengths:** all fourteen metric definitions match current names, labels, descriptions and
buckets; import does not mutate the default registry; the single lock/composite registration and
reload sentinel are structurally coherent; Docker retains non-root execution; archive extraction
uses safe tar filtering; `git diff --check`, shell syntax and Python AST parsing passed.

**Disposition:** task `t_muhhmmdfe432b7cb` remains `in_progress`; dispatch a separate fixer for loop
1/2, then a new reviewer. No downstream group may start.

---
### 2026-09-25 — Group 1 Execution Re-review #2 — **FIX-FIRST** (fix loop 2/2)

**Context:** fresh acceptance and quality re-review after fix loop 1/2. Both reviewers inspected the
uncommitted tree read-only. Authorized `DRAFT -> APPROVED -> IN_PROGRESS` lifecycle status changes
explain the active-section digest change; no execution criterion changed.

**Verified closed:** facade fixture now contains only `srm/flow/policy/prompt`; normative capture CLI
and four-text-fixture schema are present; static tools/scopes and installed `py.typed` probe exist;
all six install rows invoke the shared importing oracle; install cleanup and phase validation exist;
metrics family/ownership assertions, both wish exceptions, and INDEX dependency direction are
structurally present. Focused suite reported `212 passed`; static gate and two-archive API hash passed.

| Sev | Remaining gap | Required correction |
|-----|---------------|---------------------|
| HIGH | Boundary/API analyzers use a module-global alias map, miss callable assignment aliases, mishandle sibling/parameter shadowing, and include `TYPE_CHECKING` imports | Use lexical-scope binding environments, follow simple callable assignments, respect shadowing, skip type-only branches, and add adversarial cases to both suites |
| HIGH | `core_suite_gate` permits pytest exit 1 and no test executes the actual core/full gate functions against required planted failures | Require exit 0 for focused mode; keep 0/1 only for full mode; add shell-level command-shim tests for focused exit 1/4, parity failure, extra full failure, missing summary, and outcome loss |
| HIGH | Poetry main/dev rows may reuse ambient `VIRTUAL_ENV`/`CONDA_PREFIX` instead of fresh temp environments | Clear active-environment variables, force virtualenv creation, assert selected interpreters live under per-invocation temp roots, and add a hostile-environment test |
| MEDIUM | Origin oracle verifies installed `tzdata` but not `neurostack-core` `Requires-Dist` provenance | Require normalized `pydantic`, `prometheus-client`, and `tzdata` from package metadata; add missing-requirement negative without host-site-package leakage |
| MEDIUM | Pytest/static temp cleanup is manual and leaks on interruption | Wrap each gate in guarded subshell cleanup traps |
| LOW | Extra `REVIEW.md` ignore exception and package README `context` promise exceed scope | Remove both |

**Disposition:** final fix loop 2/2. Task remains `in_progress`; after the separate fixer, a fresh
reviewer must return SHIP or the orchestrator stops and records an escalation diagnosis.

---
### 2026-09-25 — Group 1 Execution Re-review #3 — **BLOCKED** (ordinary fix loops exhausted)

**Context:** fresh independent acceptance and quality reviews of the uncommitted Group 1 tree after
fix loop 2/2. Both reviewers were read-only. They verified closure of the previously recorded gate
exit semantics, hostile Poetry environment isolation, dependency provenance, guarded cleanup,
scope cleanup and the documented common alias/shadowing/type-only cases.

| Sev | Remaining gap | Reproduced evidence |
|-----|---------------|---------------------|
| HIGH | The shared lexical analyzer does not conservatively merge mutually exclusive branch provenance | Both boundary and API analyzers accepted a callable assigned to `importlib.import_module` in one branch and to an ordinary loader in the other, then called with `"src.policy"`; incomplete `with ... as` binding also creates false positives |
| HIGH | The shared lexical analyzer does not model `global`/`nonlocal` declarations | Both analyzers missed a forbidden dynamic import in a valid function using `global importlib` before a later import/binding |
| LOW | `scripts/_lexical_imports.py` is absent from the Active Files inventory | The helper is executable gate machinery introduced by Group 1 |

**Checks that passed:** `git diff --check`; shell syntax; Python AST parsing; current boundary scan;
source public-API fixture equality; package metadata source; focused metrics import, idempotence and
collision-atomicity probes. Heavy install surfaces, full suite and regeneration remain orchestrator
validation work and were not run by the read-only reviewers.

**Escalation diagnosis:** `model-capacity`. The specification is complete and decidable, the
environment works, and two new deterministic reproducers isolate omissions in the second lexical
scope correction. This rules out missing context, ambiguous specification, environment/tool failure
and optional overdesign. Ordinary fix loops are exhausted at 2/2; model-capacity escalation is 0/2.
Selected route: one fresh fixer at the next available model tier, limited to conservative branch
state merging, complete Python binding semantics, adversarial tests in both suites, and the inventory
entry. The task remains `in_progress`; Groups 2–4 remain blocked.

---
### 2026-09-25 — Group 1 Execution Re-review #4 — **BLOCKED** (escalation 1/2)

**Context:** fresh acceptance and quality re-reviews after the first model-capacity escalation. The
fixer changed only the shared lexical analyzer and its two focused suites; reported focused tests and
Black passed. Both reviewers replayed runtime-valid probes read-only.

**Verified closed:** the exact conditional branch-alias bypass, original `global`/`nonlocal`
before-later-binding bypass, ordinary module/class/function `with ... as` shadows, helper inventory,
and earlier Group 1 corrections.

| Sev | Remaining gap | Reproduced evidence |
|-----|---------------|---------------------|
| HIGH | Loop control flow is not conservatively joined | Both analyzers accepted forbidden calls after zero-iteration `for` and `while` bodies; `for` target provenance is also missed |
| HIGH | Exception flow and handler binding are incomplete | Both analyzers accepted reachable forbidden calls through partial `try` execution/unreachable later assignment; `except ... as importlib` can false-positive |
| HIGH | Deferred functions mutate ancestor analysis state | Merely traversing uncalled global/nonlocal setter functions changes ancestor provenance; invoked global/nonlocal write shapes can then evade detection |

**Escalation diagnosis:** `model-capacity`. The contract and environment remain complete, the first
escalation fixed its exact reproducers, and these new deterministic probes isolate adjacent omissions
in Python control-flow semantics. Missing context, ambiguity, environment failure and optional
overdesign remain ruled out. Model-capacity escalation 1/2 is consumed; ordinary fix loops remain
exhausted at 2/2. Selected route: the second and final higher-tier fixer, limited to conservative
`for`/`async for`/`while`, complete `try`/`except`/`else` binding joins, and isolated deferred
global/nonlocal write effects with matching adversarial tests. Groups 2–4 remain blocked.

---
### 2026-09-25 — Group 1 Execution Re-review #5 — **BLOCKED** (terminal)

**Context:** terminal fresh acceptance and quality re-reviews after model-capacity escalation 2/2.
Both reviewers were read-only and replayed the exact prior loop, exception, shadowing and
global/nonlocal probes in both analyzers.

**Verified closed:** every previously reported reproducer now passes, including conditional callable
aliases; zero-iteration `for`/`while`; `for` target provenance; partial `try` and `try/else`;
exception-target shadows; and uncalled, invoked-setter and invoked-clearer global/nonlocal cases. The
fixer reported 88 focused tests and Black passing. Reviewers also confirmed `git diff --check`, shell
syntax, changed-file AST parsing, current package boundary scan and public-API fixture equality.

| Sev | Terminal gap | Reproduced evidence |
|-----|--------------|---------------------|
| HIGH | Function global reads are frozen at definition time instead of resolved at invocation | Both analyzers miss a forbidden global rebound to `importlib` after function definition and false-positive when an imported global is rebound to an ordinary object before the call |
| HIGH | Loop analysis is zero-or-one iteration, not a fixed point | Both analyzers miss a dynamic alias that becomes active on the second iteration |
| HIGH | Deferred effects omit parameter substitution and conditional effect joins | Both analyzers miss parameter-fed global setters, rebound captured names, and a conditionally selected function effect because possible effects overwrite rather than join |

**Terminal diagnosis:** `model-capacity`. The specification and environment are complete, every
prior exact probe was corrected, and the new deterministic failures isolate call-time global lookup,
parameter substitution, possible-callee joins and loop fixed-point semantics in the hand-built
abstract interpreter. Missing context, ambiguity, environment failure and optional overdesign remain
ruled out. Ordinary fix loops are exhausted at 2/2 and model-capacity escalations are exhausted at
2/2. No automated retry remains. Group 1 stays `in_progress`; Groups 2–4 were not dispatched and
heavy install/static/parity/full-suite validation was skipped because review did not return SHIP.
Resolution requires a human-approved analyzer simplification/replan or explicit authorization for
additional work beyond the recorded caps.

---
### 2026-09-25 — Human escalation authorization #1

At `2026-09-25T21:27:21-03:00`, the user explicitly authorized work beyond the recorded retry caps.
Approver: repository user (Lorenzo). Reason: terminal reviewers supplied new deterministic evidence
for call-time global lookup, multi-iteration fixed points, parameter-fed effects and possible-callee
joins after all prior reproducers were closed.

**Old settings:** ordinary fixer loops exhausted at 2/2; automatic model-capacity escalations
exhausted at 2/2; Group 1 blocked. **Authorized setting:** one additional implementation attempt by
`engineer-complex` at its configured `xhigh` reasoning effort, limited to the shared lexical analyzer
and its two focused suites. This authorization does not reset either historical counter, broaden the
wish, waive independent acceptance/quality review, or waive Group 1's full validation block. If the
fresh reviews do not return SHIP, execution stops again for a new human decision.

---
### 2026-09-25 — Group 1 Execution Re-review #6 — **BLOCKED** (human authorization #1 consumed)

**Context:** independent acceptance and quality re-reviews after the human-authorized
`engineer-complex` attempt. The engineer changed only the shared analyzer and its two focused suites,
reported 116 focused tests and Black passing, and left task completion to the orchestrator.

**Verified closed:** the complete Review #3–#5 matrix passes in both analyzers, including live
call-time global lookup, ordinary rebinding, second-iteration aliases, parameter-fed global/nonlocal
setters, captured rebinding, alternative callees, prior control-flow joins and safe shadows. Current
package boundary scan, public-API fixture equality, shell syntax, Python AST parsing and
`git diff --check` also pass. Prior non-analyzer Group 1 closures were unaffected.

| Sev | New deterministic gap | Reproduced evidence |
|-----|-----------------------|---------------------|
| HIGH | The loop fixed point has an infinite-height provenance lattice | Both analyzers time out as loop iterations grow values such as `importlib.next.next...` or `importlib.import_module.__call__.__call__...`; equality is never reached |
| HIGH | Default arguments and nested call/effect composition are incomplete | Both analyzers accept forbidden calls through a default `loader=importlib.import_module`, a nested helper invocation, and a nested global setter |
| MEDIUM | Late-bound `TYPE_CHECKING` guards can false-positive | The API extractor treats a function-local type-only branch as executable when the module imports `TYPE_CHECKING` later |

**Diagnosis:** `model-capacity`. All prior exact failures were corrected and the environment remains
functional, but new probes show the bespoke abstract interpreter is still incomplete and unbounded.
Human escalation authorization #1 is consumed. No aggregate install/static/parity/full-suite
validation ran because review did not return SHIP. Group 1 remains `in_progress`; Groups 2–4 remain
undispatched. Another implementation attempt requires a new explicit human decision recorded with
its scope and cap, or a user-approved simplification/replan.

---
### 2026-09-25 — Human analyzer simplification authorization #2

At `2026-09-25T22:06:13-03:00`, the user explicitly authorized a simplification/replan of the
analyzer. Approver: repository user (Lorenzo). The path-sensitive lexical interpreter, its
call/effect model and Reviews #3–#6 as execution approval are superseded; they remain historical
evidence explaining why the simpler boundary was chosen. Non-analyzer Group 1 work is preserved
only where it still satisfies the revised active contract.

The amended plan reserves dynamic-import syntax instead of interpreting arbitrary Python execution,
deletes `_lexical_imports.py`, adds a bounded structural helper, and makes ambiguous static facade
aliases fail closed. Old setting: blocked execution after ordinary 2/2, automatic escalation 2/2 and
human authorization #1. New setting: `DRAFT` pending a fresh independent plan review; no
implementation resumes until that review is persisted as SHIP/APPROVED.

---
### 2026-09-25 — Simplified Analyzer Plan Review #9 — **SHIP**

**Context:** fresh plan review after Human analyzer simplification authorization #2. The reviewer
judged only the active contract and authorization; superseded execution reviews were historical
evidence. Reviewed full-file SHA-256 before this evidence block:
`536e2dead309ef04ec5bd5c5416371befe81ca1fc36bf9ca8f3a5c1200c5ac39`.

**Evidence:** Decision 6, Active Analyzer Simplicity Case, success criteria, Group 1 deliverables and
acceptance, shared gates, risks and file inventory consistently delete `_lexical_imports.py`, create
the bounded structural `_forbidden_imports.py`, reserve dynamic-import syntax, keep facade extraction
syntax-only, reject ambiguous aliases, exclude `TYPE_CHECKING` by AST shape and require five-second
termination. Runtime-injected callables, synthesized reflection and arbitrary control-flow
interpretation are explicitly out with a concrete future trigger. Groups remain sequential
`1 -> 2 -> 3 -> 4`; every group retains non-zero validation and the static/core/full/six-surface
aggregate gates. Branch, `HEAD`, `origin/main` and recorded baseline all resolve to
`a6d6e65afab2889b83a857bdd2d0db0bd617d228`. `git diff --check` and placeholder checks passed;
this repository exposes no `wishes:lint` script, so the guarded lint was unavailable.

**Questioner advisory:** SHIP. The real problem is accidental source-visible package-to-app coupling,
not adversarial Python interpretation. Current modules have no reserved dynamic-import spelling and
no measured requirement needs path-sensitive execution. LOW implementation advice: include all AST
binding fields in deliberate collision tests and freeze the accepted runtime-injected-loader boundary.

**Architecture advisory:** SHIP. The structural classifier is the simplest complete boundary and a
deep replacement for the 769-line interpreter. `_forbidden_imports.py` should share categorized
syntax findings only; the boundary checker owns whole-tree policy and the facade extractor owns its
`TYPE_CHECKING`-pruned static alias traversal. Do not add traversal mode flags or facade resolution to
the shared helper.

**Severity-tagged gaps:** CRITICAL none; HIGH none; MEDIUM none; LOW none.

**Verdict:** **SHIP**. Status persisted as `APPROVED`; execution may resume through `$genie:work`.

---
### 2026-09-25 — Group 1 Simplified Analyzer Execution Review #7 — **SHIP**

**Context:** fresh acceptance and quality reviews of the uncommitted Group 1 tree under Simplified
Analyzer Plan Review #9. Both reviewers were read-only and judged the revised accidental-coupling
contract rather than superseded interpreter criteria.

**Evidence:** `_lexical_imports.py` is absent and unreferenced. `_forbidden_imports.py` is a bounded
single `ast.walk` classifier with no scope, call graph, fixed point, traversal modes or facade policy.
Independent replay rejected all static `src` forms and reserved spellings across nested, type-only,
alias, argument and binding positions; accepted injected loaders and synthesized reflection as the
explicit OUT boundary; rejected conflicting/rebound facade aliases; and skipped late
`TYPE_CHECKING` shapes. Historical growing-value/call/default/nested inputs terminated in
`0.000028–0.000102s`; a 1,000-iteration stress replay completed in under 0.15 seconds. Fresh archived
facade extraction was byte-identical to the 793-byte fixture with SHA-256
`f48e1653a514b30b0aa217f55a96487ccff109f3aa4e8b6ffef10bf38b88451f`. Current package boundary,
AST parsing and `git diff --check` passed. The engineer reported 47 focused tests plus Black and mypy
clean; orchestrator validation remains required before task completion.

**Severity-tagged gaps:** CRITICAL none; HIGH none; LOW none. MEDIUM: automated suites use generic
termination cases rather than every exact historical growing-provenance snippet and do not freeze
every synthesized-reflection pass case; independent direct replay proves current behavior, so this is
non-blocking regression-coverage debt.

**Verdict:** **SHIP** from both acceptance and quality reviewers. Task remains `in_progress` pending
the complete Group 1 validation block.

---
### 2026-09-25 — Group 1 Validation #1 — **FIX-FIRST** (simplified-plan loop 1/2)

**Scope rationale:** Group 1 changes shared package metadata, dependency/install surfaces, Docker
configuration, executable gates and metrics state, so the repository-approved full Group 1 block was
run rather than substituting focused analyzer checks.

**Passed:** all three lifecycle files are tracked; focused scripts/metrics suite `245 passed`; fresh
Poetry main, Poetry dev, pip main and pip dev rows imported `neurostack-core` from their required
editable origins; Poetry dev and pip dev also built wheel/sdist successfully.

**Failure:** the Compose row remained in Docker build-context loading for 1,651 seconds and reported
only `27B` transferred. Read-only diagnosis found `frontend/node_modules/.pnpm` present while
`.dockerignore` has no nested `node_modules` exclusion. A concurrent pre-existing BuildKit job ended,
but this invocation remained CPU-active in the same context step. The orchestrator interrupted only
its own validation; the gate cleanup removed its containers, network and volumes. The production
row, `static_gate`, `core_suite_gate` and `full_suite_gate` did not run.

**Disposition:** delivery-path implementation gap, not an analyzer regression. Add the narrow nested
dependency-tree ignore and a deterministic regression assertion, then re-review and rerun the full
Group 1 block. Task remains `in_progress`; Groups 2–4 remain blocked.

---
### 2026-09-25 — Group 1 Validation Fix #1 Re-review — **SHIP**

**Evidence:** `.dockerignore` now contains `**/node_modules/`, which covers root and arbitrarily
nested dependency trees including `frontend/node_modules/.pnpm` while retaining root/package
READMEs, Poetry manifests, frontend manifests and package source. A deterministic contract regression
proved representative root/frontend/package dependency paths were included without the rule and
excluded with it. The focused install-surface suite reported `15 passed`; scoped Black and
`git diff --check` passed. Existing real Compose run, production build/run and failure-injection rows
remain unchanged.

**Severity-tagged gaps:** CRITICAL none; HIGH none; MEDIUM none. LOW quality advisory: the focused
test's Git-ignore matcher is valid for this documented `**/node_modules/` rule but must not be treated
as a universal Dockerignore oracle without compatibility cases.

**Verdict:** **SHIP** from acceptance and quality reviewers. Full Group 1 validation rerun required.

---
### 2026-09-25 — Group 1 Validation Fix #2 Re-review — **SHIP**

**Failure reproduced:** the approved aggregate command enables caller `errexit`; the full-suite
pytest pipeline therefore aborted on the expected branch-baseline exit `1` before `_pytest_gate`
could capture `PIPESTATUS[0]` and invoke the completion/failure-set validator.

**Fix and evidence:** the pytest/`tee` pipeline now executes as an `if` condition and captures the
pytest status in both branches. Deterministic tests prove full mode accepts only exits `0/1`, reaches
the validator for expected exit `1` under `set -eo pipefail`, rejects exits `2–5` before validation,
and focused mode rejects every non-zero exit. The focused file reports `11 passed`; Bash syntax,
Black and scoped `git diff --check` pass. Reviewed file hashes: `scripts/wish-gates.sh`
`40e5f60b31f8ce6c76b53ee0a0785e71f411c5b1a4e6463202f1fb8003cdcebc` and
`tests/scripts/test_wish_gates.py`
`a0dc3ce1bc79bd4c847d5936b60381851149e79871213f5a6bf55a5bb275f2a3`.

**Severity-tagged gaps:** CRITICAL none; HIGH none; MEDIUM none; LOW none.

**Verdict:** **SHIP** from the independent quality reviewer. No validation rule was weakened.

---
### 2026-09-25 — Group 1 Aggregate Validation #2 — **PASS**

The complete approved Group 1 command ran coherently with `set -eo pipefail` and exited `0`. A
temporary empty `DOCKER_CONFIG` isolated the run from the host's failing
`docker-credential-desktop.exe`; no repository or account configuration was changed.

| Gate | Result |
|------|--------|
| lifecycle tracking | extraction WISH, successor WISH and `.genie/INDEX.md` all tracked |
| focused scripts/metrics | `251 passed`, 3 warnings |
| Poetry main/dev | PASS; required editable package origin; dev wheel/sdist build PASS |
| pip main/dev | PASS; required editable package origin; dev wheel/sdist build PASS |
| Compose API | PASS; context `4.66MB`; container origin `/app/packages/neurostack-core` |
| production image | PASS; context `28.60kB`; container origin `/app/packages/neurostack-core` |
| package boundary | clean across 2 application Python files |
| Black | 279 files unchanged |
| mypy | no issues in 167 source files |
| core parity | `1017 passed`; completion validator PASS |
| full parity | `2958 passed`, 14 skipped, exactly 2 archived failures; completion validator PASS |

The two full-suite failures are the branch-point scheduling-link nodes recorded verbatim in
`tests/baselines/neurostack-core-extraction-known-failures.txt`; there was no failure-set drift and
the completed-outcome floor was exceeded. Validation Fix #1 kept both Docker contexts bounded, and
Validation Fix #2 allowed the baseline validator to judge the expected pytest exit `1` correctly.

**Group 1 disposition:** implementation review SHIP, both validation-fix re-reviews SHIP, and the
complete group validation contract PASS. Task may be marked done; Group 2 is unblocked.

---
### 2026-09-25 — Group 2 Execution Review #1 — **FIX-FIRST** (loop 1/2)

**Verified:** the atomic move has exact 37/37 Python-file parity; all four old directories are
absent; source facade equality, package boundary, metric-object identity/import neutrality,
configured/disabled lifespan mounting, focused parity (`1019 passed`), application consumers
(`155 passed`), checker tests (`4 passed`), Black, isort, Flake8, mypy and `git diff --check` pass.
The package import graph is acyclic and has no upward `src` dependency. No Group 3 artifact/smoke
scope leaked into this implementation.

| Sev | Gap | Required correction |
|-----|-----|---------------------|
| HIGH | Production consumers in `src/core/pipeline.py`, `src/memory/{__init__,models}.py` and `src/services/{business_hours,trace_adapter}.py` still deep-import private implementation modules | Import every already-exported symbol through `neurostack_core.{flow,policy,prompt,srm}` and add a regression covering all six consumers |
| HIGH | The lifespan metrics test directly invokes `get_srm_engine().process()`; its only HTTP request is `/metrics`, so it does not prove the real app event/pipeline cutover increments the decision counter, and it checks only one family | Send a request through the real event/pipeline route with only external/database boundaries stubbed; assert the decision counter delta/labels from `/metrics`, all fourteen enabled families and all fourteen absent when disabled |
| MEDIUM | The checker test named as installed coverage actually resolves the Poetry editable source and installed mode performs no origin assertion | Name this proof accurately; reserve genuine external wheel plus origin/shadow proof for Group 3 and ensure the current test cannot be mistaken for it |
| MEDIUM | Removed source paths remain in `tests/prompt/test_timezone_greeting.py` and `src/repositories/INTEGRATION.md` | Update both references to the package paths and include them in the old-path audit |

**Verdict:** **FIX-FIRST** from independent acceptance and quality reviewers. No aggregate install,
static, parity or full-suite validation runs until a fresh re-review returns SHIP.

---
### 2026-09-25 — Group 2 Execution Re-review #2 — **SHIP**

**Closed:** all six production consumers now import exclusively through the four captured facades,
with a whole-`src/**/*.py` AST regression that rejects deep imports and freezes the exact consumer
set. The real lifespan proof now sends `POST /v1/events`, traverses `EventService.run_pipeline` and
`CognitivePipeline.process`, stubs only authentication/persistence/database/tracing/external-LLM
boundaries, and proves an exact `+1` decision-counter delta for the expected three-label sample via
`/metrics`. It also verifies all fourteen families enabled, all fourteen absent when disabled, a
disabled `/metrics` 404, and repeated-lifespan idempotence.

The public-API test is now accurately scoped as Poetry-editable/source coverage, while external
wheel origin/shadow proof remains Group 3 work. Both stale deleted-source references were updated
and are guarded. Source/editable facades match fixture SHA-256
`f48e1653a514b30b0aa217f55a96487ccff109f3aa4e8b6ffef10bf38b88451f`; the package boundary is
clean across 39 Python files; its graph has zero cycles; atomic file parity remains 37/37; import
neutrality and identity of all fourteen engine handles pass; mypy covers 167 source files cleanly;
Flake8, isort and `git diff --check` pass. Focused fix evidence reported 13 tests passed.

Reviewed focused hashes: `tests/api/test_core_metrics_lifespan.py`
`28bf51fe12f4ea858243f18425676bcfa725899e8b05c86dcedc57fa49fd5104`,
`tests/scripts/test_application_core_imports.py`
`e88a1a8b3e5feaab9094606c159fff5a261ac90adf516b970cb8c0968d0e0a2d`, and
`packages/neurostack-core/scripts/check_public_api.py`
`7adcfaccaf6b3efc7608dfcdbb1a05c9c0f81b1b8d3af7fe264c6fdaabbbb4de`.

**Severity-tagged gaps:** CRITICAL none; HIGH none; MEDIUM none; LOW none.

**Verdict:** **SHIP** from independent acceptance and quality reviewers. The complete Group 2
validation block remains required before task completion.

---
### 2026-09-25 — Group 2 Aggregate Validation — **PASS**

The complete approved Group 2 command ran coherently with `set -eo pipefail` and exited `0`. As in
Group 1, a temporary empty `DOCKER_CONFIG` isolated validation from the host credential-helper
failure without changing repository or account configuration.

| Gate | Result |
|------|--------|
| captured source facade | exact fixture equality |
| moved-module + real lifespan suite | `1019 passed`, 3 warnings |
| old source directories | all four absent |
| Poetry main/dev | PASS; required editable origins; dev wheel/sdist build PASS |
| pip main/dev | PASS; required editable origins; dev wheel/sdist build PASS |
| Compose API | PASS; context `4.65MB`; package origin `/app/packages/neurostack-core` |
| production image | PASS; context `31.79kB`; package origin `/app/packages/neurostack-core` |
| package boundary | clean across 40 Python files |
| Black | 283 files unchanged |
| mypy | no issues in 167 source files |
| core parity | `1017 passed`; completion validator PASS |
| full parity | `2966 passed`, 14 skipped, exactly 2 archived failures; completion validator PASS |

The full-suite failure set remained exactly the two branch-point scheduling-link nodes and exceeded
the completed-outcome floor. No collection error, new failure, outcome loss or install-origin drift
occurred.

**Group 2 disposition:** execution re-review SHIP and complete validation PASS. Task may be marked
done; Group 3 is unblocked.

---
### 2026-09-26 — Group 3 Execution Review #1 — **FIX-FIRST** (loop 1/2)

**Acceptance evidence verified:** one fresh wheel/sdist, exact recursive source payload, no app tests,
external venv imports, source/installed facade equality, empty-`PYTHONTZPATH` timezone resolution,
documented smoke/Quickstart output, README state/cleanup/domain limitations and no Group 4 scope
leakage all pass on the current happy path. Focused implementation reported `11 passed` and combined
facade/artifact coverage `15 passed`; boundary, Black, isort, Flake8, mypy and Poetry check pass.

| Sev | Gap | Required correction |
|-----|-----|---------------------|
| HIGH | Installed origin accepts any facade under broad `sys.prefix`; RECORD checks only one path and ignores exact rows, hashes and sizes | Derive exact site-packages/package origin from the one canonical dist-info; bind every facade to it; validate safe exact RECORD member coverage, hashes and sizes; plant corruption and prefix-internal shadow cases |
| HIGH | Wheel metadata reduces `Requires-Dist` to names | Compare normalized full requirements against pyproject, reject duplicates/direct URLs/unexpected markers or specifiers, assert `Requires-Python`, version and wheel/sdist `PKG-INFO` parity |
| HIGH | CWD-shadow test depends on files created by a prior test | Give each test its own explicit immutable checker/fixture inputs and prove the shadow node passes in isolation/sharding |
| MEDIUM | Archive prefixes and paths are inferred; smoke/Quickstart use checkout rather than sdist bytes | Require canonical safe POSIX wheel/sdist roots/name/version, compare exact archive bytes, and execute README/smoke inputs extracted from the sdist |
| MEDIUM | Quickstart extractor scans beyond the `## Quickstart` section | Stop at the next same/higher-level heading and reject missing heading, later-section Python, malformed and unclosed fences |
| MEDIUM | Build fixture inherits ambient Python/pip/cache/user configuration and leaves backend provenance open | Sanitize build environment, localize caches/config, disable user pip configuration, and constrain or record the isolated backend provenance |

**Verdict:** acceptance reviewer SHIP; quality reviewer **FIX-FIRST**, therefore aggregate verdict
**FIX-FIRST**. No Group 3 aggregate validation until the corrected proof receives fresh review.

---
### 2026-09-26 — Group 3 Execution Re-review #2 — **SHIP**

All six proof-system findings are closed. Facades bind to the one canonical dist-info-adjacent
package; RECORD validation enforces safe unique paths, exact owned-file coverage, hashes and sizes,
with truncation/hash/size/path and prefix-internal-shadow plants. Full normalized requirements,
duplicates/direct URLs/markers/specifiers, version, `Requires-Python`, wheel/sdist metadata parity and
isolated Poetry backend provenance are checked. External installs and checker inputs are
function-scoped, including an independently passing shadow node.

Wheel/sdist roots and paths are canonical safe POSIX names; payload/input bytes equal source; the
README and smoke program actually executed come from the sdist. Quickstart extraction stops at the
next H1/H2 and rejects missing headings, later-section Python, malformed and unclosed fences. Build
and install environments localize home/temp/cache/config state, disable user pip configuration and
remove ambient Python-path state. Exact-tree focused evidence reports `18 passed`; the isolated
shadow node reports `1 passed`; boundary, source smoke, extracted Quickstart, AST parsing and
`git diff --check` pass. The accepted wheel remains 43 members, the canonical sdist 45 files, and all
39 installed modules/templates import externally.

Reviewed hashes include `check_public_api.py`
`1db189eb18177f2ecb1cca4c9f29695b8e61b7cb67d468390c933aa174d2a6c8`,
`extract-readme-quickstart.py`
`e58a42d94bf5ee7a6d0b687adc8317ba8183a4589c8931e55b3fd6434a47a074`, and the four artifact
test/fixture files recorded by the orchestrator before this evidence block.

**Severity-tagged gaps:** CRITICAL none; HIGH none; MEDIUM none; LOW none.

**Verdict:** **SHIP** from independent acceptance and quality reviewers. Fresh artifact and
aggregate validation remain required before task completion.

---
### 2026-09-26 — Group 3 Aggregate Validation — **PASS**

The complete approved Group 3 block ran coherently with `set -eo pipefail` and exited `0`, using the
same temporary empty Docker-config isolation as prior groups.

| Gate | Result |
|------|--------|
| hardened artifact/external-consumer suite | `18 passed`, 3 warnings |
| Poetry main/dev | PASS; editable origins; dev wheel/sdist build PASS |
| pip main/dev | PASS; editable origins; dev wheel/sdist build PASS |
| Compose API | PASS; context `4.67MB`; package origin `/app/packages/neurostack-core` |
| production image | PASS; context `32.01kB`; package origin `/app/packages/neurostack-core` |
| package boundary | clean across 41 Python files |
| Black | 288 files unchanged |
| mypy | no issues in 167 source files |
| core parity | `1017 passed`; completion validator PASS |
| full parity | `2984 passed`, 14 skipped, exactly 2 archived failures; completion validator PASS |

The full suite now includes the artifact tests and retained exactly the recorded scheduling-link
failure set with no collection error or outcome loss.

**Group 3 disposition:** execution re-review SHIP and complete artifact/aggregate validation PASS.
Task may be marked done; Group 4 is unblocked.

---
### 2026-09-26 — Group 4 Terminal Validation #1 — **FIX-FIRST (plan command only)**

The artifact install-surface gate completed all six rows successfully, and the separately tagged
production image built successfully. The next probe exited `2` before evaluating the image because
the recorded command still passed the removed `--expected-prefix` option to the reviewed origin
oracle. Container/image cleanup then completed and was verified; no final-tag image or container was
left behind.

The Group 4 command is amended to use the oracle's current, already-reviewed contract:
`--mode editable --expected-source /app/packages/neurostack-core`. Its cleanup is also expressed as
explicit stop/container-remove/image-remove/empty-directory removal operations so the terminal block
can run under command-safety policy. These changes do not weaken the asserted package-origin
semantics or alter product code. Independent plan review is required before the terminal proof is
retried.

---
### 2026-09-26 — Group 4 Plan Amendment Review — **SHIP**

Independent review of WISH SHA-256
`58564427dc2f8e32ee02be341a6f7feed2440ff451b05ef03e46972e06909fbc` found no CRITICAL,
HIGH, MEDIUM or LOW gaps. The amended invocation exactly matches the oracle CLI and proves the
package lies below the expected source, the distribution belongs to the active environment,
`direct_url.json` points exactly to that editable source, editable metadata is present, required
dependencies and `tzdata` exist, and empty-`PYTHONTZPATH` resolves `America/Sao_Paulo`. It is the
same contract already used by the passing Compose and production-image gate rows.

The explicit stop/remove/image-remove/empty-directory cleanup preserves best-effort semantics and
owns only resources created by the validation. The shell block parses under `bash -n`, the oracle
parses as Python, and `git diff --check` passes.

**Verdict:** **SHIP**. The terminal proof may be retried.

---
### 2026-09-26 — Group 4 Terminal Validation #2 — **PASS**

The complete amended Group 4 block ran coherently with `set -eo pipefail` and exited `0`, using an
empty temporary Docker configuration to isolate host credential-helper state. Cleanup was verified:
the independently tagged image and its container are absent.

| Gate | Result |
|------|--------|
| Poetry main/dev | PASS; editable origins; dev wheel/sdist build PASS |
| pip main/dev | PASS; editable origins; dev wheel/sdist build PASS |
| Compose API | PASS; context `4.67MB`; origin `/app/packages/neurostack-core` |
| production install-surface image | PASS; context `32.01kB`; origin `/app/packages/neurostack-core` |
| independently tagged production image | build PASS; empty-`PYTHONTZPATH` origin/metadata PASS |
| running image liveness | `/live` PASS on a random loopback port with lifespan disabled |
| artifact plus real-lifespan suite | `20 passed`, 3 warnings |
| package boundary | clean across 41 Python files |
| Black | 288 files unchanged |
| mypy | no issues in 167 source files |
| core parity | `1017 passed`; completion validator PASS |
| full parity | `2984 passed`, 14 skipped, exactly 2 archived failures; completion validator PASS |

The full suite collected 3000 tests, retained precisely the recorded scheduling-link failure set,
and had no collection error or outcome loss.

**Group 4 disposition:** plan-amendment review SHIP and complete terminal validation PASS. Task may
be marked done; the whole wish is ready for final independent review.

---
### 2026-09-26 — Final Execution Gate — **SHIP**

The independent final gate reviewed the current uncommitted extraction tree at base
`a6d6e65afab2889b83a857bdd2d0db0bd617d228` (`HEAD == origin/main`) and WISH SHA-256
`53453f452f45879fde2faf7ba72cf11da38c15f0f730263bb5a34af36c07ae99`.

Fresh read-only/focused evidence reported `277 passed`; the 41-file package boundary and exact
branch-derived source facade passed; old source directories and `_lexical_imports.py` are absent;
all key implementation hashes still match; `git diff --check` passes; and the extraction wish,
successor wish and index are tracked. The reviewer accepted the recorded six-surface, artifact,
image/origin/timezone, liveness, metrics, `1017`-test core parity and full-suite evidence.

**Severity-tagged gaps:** CRITICAL none; HIGH none. One non-blocking MEDIUM commit-scope hygiene
note requires excluding the unrelated pre-existing lifecycle edits in
`.genie/wishes/api-provider-availability/WISH.md` and
`.genie/wishes/backend-test-baseline-repair/WISH.md` from any extraction commit.

**Verdict:** **SHIP**. All four active `t_muhh...` work groups are done; the older `t_muh5...`
cards are superseded historical-roadmap entries. Per the work lifecycle, status remains
`IN_PROGRESS` through commit/PR/CI; only authorized merge plus required post-merge QA may set
`SHIPPED`.

---
## Historical pre-fix file inventory (superseded; do not execute)

```
CREATE  packages/neurostack-core/pyproject.toml
CREATE  packages/neurostack-core/README.md
CREATE  packages/neurostack-core/neurostack_core/__init__.py
CREATE  packages/neurostack-core/neurostack_core/py.typed              # Group 1 — PEP 561 marker
CREATE  packages/neurostack-core/neurostack_core/metrics.py            # Group 4
CREATE  packages/neurostack-core/scripts/smoke.py
CREATE  packages/neurostack-core/scripts/check_public_api.py
CREATE  scripts/wish-gates.sh
CREATE  scripts/check-package-boundary.py
CREATE  scripts/validate-pytest-completion.py
CREATE  scripts/extract-readme-quickstart.py
CREATE  tests/scripts/test_check_package_boundary.py
CREATE  tests/scripts/test_validate_pytest_completion.py
CREATE  tests/srm/test_metrics_optin.py                                # Group 4
CREATE  tests/api/test_core_metrics_lifespan.py                        # Group 4
CREATE  tests/baselines/neurostack-core-extraction-branch.txt
CREATE  tests/baselines/neurostack-core-extraction-known-failures.txt
CREATE  tests/baselines/neurostack-core-extraction-outcome-floor.txt
ADD     .genie/wishes/neurostack-core-extraction/WISH.md               # version the reviewed plan
ADD     .genie/wishes/neurostack-core-dedomainification/WISH.md        # version the dependent plan

MOVE    src/srm/**    -> packages/neurostack-core/neurostack_core/srm/**
MOVE    src/flow/**   -> packages/neurostack-core/neurostack_core/flow/**
MOVE    src/policy/** -> packages/neurostack-core/neurostack_core/policy/**
MOVE    src/prompt/** -> packages/neurostack-core/neurostack_core/prompt/**

MODIFY  .gitignore                            # narrowly unignore both core WISH files
MODIFY  .dockerignore                         # include nested package README in build context
MODIFY  .genie/INDEX.md                       # record extraction -> dedomainification dependency
MODIFY  pyproject.toml                        # path dependency + build dev dep + coverage source
MODIFY  poetry.lock
MODIFY  Makefile                              # package quality/coverage + docker-core-smoke
MODIFY  Dockerfile                            # path dependency available in deps and production
MODIFY  docker-compose.yml                    # development mount for packages/
MODIFY  requirements.txt                      # editable local package for pip workflows
MODIFY  requirements-dev.txt                  # document inherited editable package
MODIFY  README.md                             # install contract + package pointer
MODIFY  SETUP.md                              # requirements install contract
MODIFY  start-dev.sh                          # verify local package in the venv
MODIFY  src/api/main.py                       # enable_metrics() at startup
MODIFY  src/api/routes/policies.py            # 7 import lines
MODIFY  src/core/pipeline.py                  # 12 import lines
MODIFY  src/memory/__init__.py                # 1 import line
MODIFY  src/memory/models.py                  # 1 import line (FunnelStage)
MODIFY  src/services/business_hours.py        # 1 import line
MODIFY  src/services/trace_adapter.py         # 1 import line
MODIFY  tests/**                              # 10 test files
MODIFY  docs/srm-hybrid-classifier.md         # lines 57, 70, 98, 175, 184
MODIFY  docs/prompt-templates-and-funnel.md   # lines 58, 167, 168

DELETE  src/srm/ src/flow/ src/policy/ src/prompt/
```

_`src/api/schemas/sessions.py` is **not** in this list. It re-declares its own `FunnelStage` enum,
which matters only once the enum becomes preset-derived — that is the successor wish's problem. In
this wish `FunnelStage` stays a hardcoded enum and the duplicate stays correct._
