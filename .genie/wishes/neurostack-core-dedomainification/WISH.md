# Wish: Externalize `neurostack-core` localized clinic content into a loadable preset

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `neurostack-core-dedomainification` |
| **Date** | 2026-09-25 |
| **Author** | Lorenzo |
| **Appetite** | large |
| **Branch** | `wish/neurostack-core-dedomainification` |
| **Repos touched** | neurostack-api |
| **Design** | _No brainstorm — direct wish_ |

## Summary

`neurostack-core` ships installable after `neurostack-core-extraction`, but it is still a
Brazilian aesthetic-clinic engine: the draft inventory measures **619 pt-BR localized literals across
18 files** compiled into the source. This wish moves the content into loadable preset files, leaves
the current content as the `pt_br_clinic` default so `neurostack-api` behaves identically, and proves
the result by starting a fresh process with an English preset over the same clinic workflow. This is
localization/content extraction, not arbitrary-domain reasoning: stable workflow identifiers remain
a compatibility protocol and are not translated or made preset-defined.

The scope is **closed by measurement, not by sampling**. Group 1 checks in the inventory script,
remeasures the merged extraction tree and records the authoritative baseline before source rewrites;
that script is the lexical definition of "done", every group drives its files to zero, and planted
violations prove the gate can fail. The draft's 619/18 estimate comes from prior enumeration but is
not trusted as post-extraction evidence.

## Scope

### IN

- Preset loader, preset schema, and the `use_preset()` public API in `neurostack_core`.
- `presets/pt_br_clinic/` carrying today's content verbatim, split one TOML file per module
  (`srm.toml`, `flow.toml`, `policy.toml`, `prompt.toml`) for schema and review clarity. Group 1
  creates and owns all four; module groups consume them read-only.
- Driving every measured content literal in the **18** measured files to zero, module by module.
  The current 619 count is a pre-extraction estimate; Group 1 remeasures it on the merged extraction
  tree. The only source literals the detector may exempt are the explicitly pinned stable protocol
  identifiers, and a guard fails if that allowlist grows or diverges from the canonical enums.

  | Module | Literals | Files |
  |--------|---------:|------:|
  | `srm` | 360 | 7 |
  | `prompt` | 155 | 5 |
  | `policy` | 77 | 2 |
  | `flow` | 27 | 4 |
  | **Total** | **619** | **18** |

- Stage, intent, category, reason, rule and response identifiers remain the static compatibility
  protocol, including legacy values already persisted or emitted externally. Every preset must
  provide the complete canonical identifier sets; presets may change language, regexes, transitions
  and presentation content, not the engine protocol.
- A second preset (`en_generic`) used only as the proof that the engine is preset-driven.
- Reconciling the duplicate `FunnelStage` at `src/api/schemas/sessions.py:16`, which re-declares the
  enum locally and can drift from the canonical protocol.
- A configure-once preset lifecycle: all four TOMLs validate into one immutable aggregate before it
  is published; first access selects `pt_br_clinic`, and selecting a different preset afterward is
  an error rather than a hot-swap.

### OUT

- **Publishing to PyPI.** Unchanged from the extraction wish: the package stays installable from
  source. A distribution/versioning wish comes later.
- **Translating the pt-BR preset.** `en_generic` is a proof fixture, not a production English
  preset — it needs only enough content to demonstrate preset-driven reasoning.
- **Removing the clinic workflow or medical-safety model itself.** Funnel semantics, policy concepts,
  action catalog and legacy identifiers remain the engine protocol. A truly arbitrary-domain engine
  needs a separate redesign around opaque identifiers and data-driven control flow.
- **Selecting a non-default preset from `neurostack-api` configuration.** This wish keeps the app on
  zero-configuration `pt_br_clinic`; explicit selection is a pre-import library/launcher operation.
- **A preset registry, preset inheritance, preset merging, arbitrary stage schemas, or runtime preset
  hot-swap.** One process loads one immutable preset at startup. See the Simplicity Case.
- **Changing any classification behaviour.** Every regex, threshold, stage and template moves
  byte-identical into the preset; the 1006-test parity oracle must stay exactly green.
- **`ctx`, `memory`, `handoff`, `llm`** and the rest of `src/` — except the named app-side enum
  consolidation in `src/api/schemas/sessions.py` and any import-only rewrites already owned by the
  extraction wish.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Preset format is **TOML with literal strings** (`'''…'''`) | The bulk of the payload is regexes. TOML basic strings decode `\uXXXX` and consume backslashes; literal strings pass them through raw, which is what a regex corpus needs. Verified against `tomllib` both ways. |
| 2 | Escapes are **decoded at extraction time**, not copied | Because Decision 1 mandates literal strings, a source literal written `"ç"` must land in the TOML as the character `ç`. Copying the six-character escape into a literal string would change the pattern. |
| 3 | One TOML file **per module**, not one per preset | The files mirror package boundaries, keep reviews navigable, and let each module's schema fail with a precise filename and key. Group 1 alone creates and populates them; later groups only replace source literals with lookups. |
| 4 | Internal identifiers are a **stable engine protocol** owned by `neurostack_core.protocol`, not preset-derived | The dependency-free leaf owns every public SRM enum, official intent/action ids, exact policy `rule_id` and `rule` namespaces, flow response keys, and prompt lookup keys. Existing values are persisted, emitted, or named by runtime control flow. Every preset supplies each namespaced set exactly, including legacy pt-BR values such as `lead_novo` and `agendar`; display text and matching content may differ. `protocol.py` imports no preset or consumer module, while loader and consumers both depend on it. |
| 5 | Preset selection is **atomic, configure-once and immutable** | `use_preset(name_or_path)` is allowed until the first preset access. The loader parses and validates all four files into one immutable `Preset` under a lock, then publishes it in one assignment. Re-selecting the same canonical source is idempotent; selecting a different source raises `PresetError`. A failed load leaves the process unconfigured. There is no public reset or hot-swap. |
| 6 | Scope is closed by a **checked-in inventory script**, not by a prose list | Four reviews blocked this plan by finding content the plan had not listed. With the script checked in, anything it counts is in scope by definition and anything it does not count is out of scope by definition — the argument moves from "did you find everything?" to "is the detector right?", which Group 1 answers with planted violations. |
| 7 | The detector matches **accents OR a pt-BR vocabulary list** | Accents alone are a lower bound: 133 literals contain pt-BR words with no accented character (`agendar`, `botox`, `preco`). Measured both ways; the union is 619 where accents alone give 488. |
| 8 | Closure is **lexical plus behavioural** | The inventory closes the enumerated source surface; fresh-process foreign-preset tests exercise every intent, transition, safety, policy, response and prompt path configured by `en_generic`. Neither a single scheduling example nor the detector alone is claimed to prove unreachable code. |
| 9 | `pt_br_clinic` ships **inside the wheel** as the default preset | `neurostack-api` must keep working with zero configuration, and the extraction wish already promised behavioural identity. |
| 10 | The duplicate enum in `src/api/schemas/sessions.py` **imports from the package in Group 2** | The enum is one protocol. Consolidating it with the SRM work keeps the type boundary and its validation in the same group rather than deferring a known drift point. |
| 11 | Preset paths are **trusted operator configuration** | A `str` must be a bundled name matching `[a-z0-9_]+`; an `os.PathLike[str]` is an explicit directory, resolved strictly against the current working directory with symlinks canonicalised, and reads only the four fixed filenames. Source identity is `bundled:<name>` or `path:<absolute-realpath>`; equivalent contents from different identities are different sources. Presets define regexes and system prompts, so remote, uploaded, request or tenant-controlled values are out of scope. The schema compiles regexes, validates placeholders/cross-references, and caps each file at 1 MiB before publication. |
| 12 | `use_preset` and `get_preset` are **public startup APIs** | `use_preset(name_or_path: str | os.PathLike[str]) -> Preset` validates/selects and returns the frozen aggregate; `get_preset() -> Preset` returns it or atomically selects `pt_br_clinic`, permanently closing the explicit-selection window. `Preset` exposes read-only `name`, `source_id`, `srm`, `flow`, `policy` and `prompt` fields. Package-root import alone does not select; importing a content consumer may. Lifecycle errors name selected/requested sources and say to restart the process. |

## Simplicity Case

**Simplest complete design.** One dependency-free static protocol plus one deeply immutable `Preset`
value (`frozen` records, tuples and read-only mappings; no reachable list or mutable dictionary).
`use_preset(path_or_name)` validates four TOMLs into that value before startup; otherwise first
access atomically selects bundled `pt_br_clinic`. Modules read the selected value instead of
hardcoded content. No registry, inheritance, merging, mutable module tables or hot-swap.

**Mechanisms added beyond that, each with a present requirement:**

1. **A complete preset schema with loud failures.** Required because missing content, bad regexes,
   mismatched format placeholders or cross-file ids must fail before any partial preset becomes
   visible. Group 1 populates all four production TOMLs before any module consumes them; there is no
   transitional "empty preset" mode.
2. **A lock around first selection.** Required because ASGI startup and lazy first access can race.
   The lock protects one validate-then-publish transition, not a cache or reload mechanism.
3. **A stable-id guard.** Required because persisted values and runtime branches depend on canonical
   stages, intents, categories, reasons, rules and responses. It compares every preset to those
   complete sets and pins the inventory allowlist to the same protocol instead of silently exempting
   arbitrary pt-BR text.
4. **A normalized corpus-parity manifest.** Required because accent totals and a later file hash can
   faithfully preserve a bad initial copy. Group 1 serializes every named pre-change container into
   deterministic JSON, compares the loaded TOMLs to it, and later groups rerun the comparison for
   their module. This is migration evidence, not a second runtime format.

**Deferred behind a concrete trigger:**

- **Preset inheritance / merging** — deferred until a second production preset exists that genuinely
  shares content with the first. `en_generic` is a proof fixture and shares nothing.
- **A preset registry or entry-point discovery** — deferred until a third party ships a preset as a
  separate distribution. Until then a path and a bundled name cover every case.
- **Runtime hot-swap** — deferred until a single process needs two domains at once. It requires an
  explicit redesign of singleton ownership, Pydantic/OpenAPI types and state isolation; current
  `use_preset` deliberately rejects it.
- **Arbitrary stage sets** — deferred until a real consumer needs a different state machine. That
  trigger replaces `FunnelStage` with opaque validated ids and removes every symbolic branch; it is
  not smuggled into this content-extraction wish.
- **Caching the parsed preset** — deferred until measurement shows load cost matters. It is read once
  at startup and the whole cognitive layer cold-starts in 0.54 s today.

## Dependencies

**depends-on:** neurostack-core-extraction, neurostack-core-resource-boundary

**blocks:** none

_Every path in this wish is a post-extraction path (`packages/neurostack-core/neurostack_core/...`).
This wish cannot start until the extraction wish has merged._

## Success Criteria

- [ ] `poetry run python scripts/pt-br-inventory.py --max 0 packages/neurostack-core/neurostack_core`
      exits 0 — every measured content literal is gone from package source; only the separately
      pinned protocol-id allowlist is exempt.
- [ ] The same script, run against `presets/pt_br_clinic/`, still finds the content — it moved, it
      was not deleted.
- [ ] `poetry run pytest tests/srm tests/flow tests/policy tests/prompt -q` reports **at least 1006
      passed and zero failed**, with the default preset active and no test edited to accommodate a
      behaviour change.
- [ ] Every bundled preset's separately namespaced ids equal `neurostack_core.protocol` exactly, and
      the API schema imports its canonical `FunnelStage` rather than declaring a duplicate.
- [ ] Fresh-process lifecycle probes prove: explicit selection before first access works, default
      first access selects `pt_br_clinic`, same-source reselection is idempotent, different-source
      reselection fails, and a failed load publishes no partial state.
- [ ] In a fresh process, `en_generic` passes its complete configured behavior matrix with no pt-BR
      leakage; a separate fresh process proves the unchanged pt-BR behavior.
- [ ] `poetry run mypy src packages/neurostack-core/neurostack_core` → `Success: no issues found`;
      the canonical static enum remains fully typed for package and app consumers.
- [ ] A clean wheel installed outside the repository loads both bundled presets through package
      resources and defaults to `pt_br_clinic` without source-tree access.
- [ ] The full suite's failing set equals the pinned known-failure set, unchanged.

## Execution Strategy

### Wave 1 (sequential)
**Group 1** — complete loader/schema contract, fully populated `pt_br_clinic`, inventory gate and
pre-change captures. Nothing downstream consumes a preset until all four production TOMLs validate
as one immutable aggregate.

### Wave 2 (sequential)
**Group 2** — `srm` lookups plus canonical `FunnelStage` consolidation in the API schema. Alone in
its wave because every later module uses the stage and preset contracts it proves.

### Wave 3 (parallel)
**Groups 3, 4, 5** — `prompt`, `policy`, `flow`. They only replace literals in disjoint source files
with lookups into the already-populated, read-only preset files, so they may run concurrently.

### Wave 4 (sequential)
**Group 6** — `en_generic`, fresh-process behavioral proof, installed-wheel proof and README.
Requires every module converted; enum reconciliation is already complete in Group 2.

### Shared validation preamble

_Every group's validation block assumes these functions are in scope. They are defined once in
`scripts/wish-gates.sh` (created by the extraction wish, extended by Group 1 here) and sourced.
`full_suite_gate` and `static_gate` are **inherited unchanged** from the extraction wish;
`inventory_gate`, `preset_loader_gate`, `pt_br_preset_unchanged` and `parity_tests_unchanged` are
added here. Three properties matter and each is proven in Group 1 against a planted violation,
never assumed._

```bash
# Property 1: inventory_gate fails when ANY named path still holds a measured literal.
# Property 2: core_suite_gate fails on a failing test AND on a disappearing test (pass-count floor).
# Property 3: preset_loader_gate rejects an incomplete or internally inconsistent four-file preset
#             before it can become the process-global selection.
#
# The fourth closure — a foreign preset actually reasoning — is deliberately NOT a reusable gate.
# It can only pass once every module group is done, so it lives inline in Group 6 rather than
# masquerading as a check earlier groups could run.
#
# full_suite_gate is inherited from scripts/wish-gates.sh (extraction wish): it diffs the failing
# set against the pinned known-failures file bidirectionally.

inventory_gate() {                       # named paths must hold zero measured pt-BR literals
  poetry run python scripts/pt-br-inventory.py --max 0 "$@"
}

preset_loader_gate() {                   # schema, lifecycle, stable ids and atomic failure
  poetry run pytest tests/presets/test_preset_loader.py -q -p no:cacheprovider
}

pt_br_preset_unchanged() {               # Group 1 owns the captured default corpus
  sha256sum --check tests/fixtures/presets/pt-br-preset.sha256
}

core_suite_gate() {                      # parity oracle: zero failures, never fewer than 1006 passed
  local out; out="$(mktemp "${TMPDIR:-/tmp}/core.XXXXXX")"
  trap 'rm -f "$out"' RETURN
  set -o pipefail
  poetry run pytest tests/srm tests/flow tests/policy tests/prompt -q -p no:cacheprovider | tee "$out"
  poetry run python -c "import re,sys; t=open(sys.argv[1]).read(); m=re.search(r'^=+ (\d+) passed', t, re.M); sys.exit(0 if m and int(m.group(1))>=1006 else 1)" "$out"
}

parity_tests_unchanged() {               # new tests are allowed; the 1006-test oracle is immutable
  local base; base="$(git merge-base HEAD origin/main)"
  if {
    git diff --name-only "$base" -- tests/srm tests/flow tests/policy tests/prompt
    git ls-files --others --exclude-standard -- tests/srm tests/flow tests/policy tests/prompt
  } | grep -q .; then
    echo "parity-oracle tests were modified" >&2; return 1
  fi
}

static_gate() {                          # what `make quality` checks, minus its always-red test step
  poetry run black --check src tests packages
  poetry run isort --check-only src tests packages
  poetry run flake8 src tests packages
  poetry run mypy src packages/neurostack-core/neurostack_core
}
```

## Execution Groups

### Group 1: Complete preset contract, populated default preset and inventory gate

**Goal.** Establish the complete data and lifecycle contract before source code consumes it. Copy
all current content into a valid `pt_br_clinic` aggregate without changing runtime behavior, and
prove the loader and inventory gates fail closed.

**Deliverables.**

1. `scripts/pt-br-inventory.py` — the checked-in detector. It walks Python AST string constants and,
   with `--include-toml`, TOML string values; matches accents **or** the pt-BR vocabulary list; skips
   docstrings and the exact stable-protocol allowlist; prints a per-file table; and exits non-zero
   when the total exceeds `--max`.
2. `tests/fixtures/presets/inventory-baseline.txt` — the script's output at
   the post-extraction branch point, committed with the allowlist printed separately. If the file
   topology or count differs from this draft, the evidence records the new measured total before any
   source rewrite begins.
3. `scripts/pt-br-protocol-ids.txt` — the only inventory exemptions. Its value set must equal the
   canonical enum values and official intent/rule/response ids it covers; comments explain why
   persisted identifiers remain source code.
4. `neurostack_core/protocol.py` — dependency-free owner of all public SRM enums and separately
   namespaced canonical sets: official intent/action ids, policy `rule_id` values, policy `rule`
   names, handoff/safety response keys, and prompt lookup keys. `srm.models` imports and re-exports
   these symbols for compatibility; `protocol.py` imports neither presets nor consumer modules.
5. `neurostack_core/presets/models.py` and `loader.py` — deeply frozen schema values, `PresetError`,
   public `use_preset(name_or_path: str | os.PathLike[str]) -> Preset` and public
   `get_preset() -> Preset`. The loader reads all four fixed filenames, enforces Decisions 11–12,
   validates every namespaced identifier set and cross-reference, then publishes one aggregate under
   a lock.
6. `neurostack_core/presets/pt_br_clinic/{srm,flow,policy,prompt}.toml` — created and **fully
   populated** with today's content. Group 1 owns these files for the wish; Groups 2–5 consume but do
   not edit them.
7. `tests/fixtures/presets/pt-br-preset.sha256` — hashes all four completed
   default-preset files. Every later group verifies it so data corrections return to Group 1 instead
   of being hidden inside a consumer rewrite.
8. `tests/presets/test_preset_loader.py` — schema, packaged-name/PathLike resolution, size cap, regex
   and placeholder validation, exact-set rejection for **every** protocol namespace, atomic failure,
   deep immutability, default selection, idempotent same-source selection and rejected
   different-source selection. Barrier-synchronized tests cover concurrent same-source calls,
   conflicting valid sources, and valid-versus-failing selection with no partial state or deadlock.
9. `use_preset`, `get_preset`, `Preset` and `PresetError` exported from
   `neurostack_core/__init__.py` and listed in `__all__`. Importing only the package root does not
   select a preset; `get_preset()` or content-consumer import selects `pt_br_clinic` when no explicit
   startup selection was made.
10. `scripts/preset-parity.py` plus `tests/fixtures/presets/pt-br-containers.json` — deterministic
   adapters capture every named pre-change container (including unaccented regexes, thresholds,
   transitions and template strings), compare the complete loaded default preset immediately, and
   support module-scoped comparisons after each consumer conversion.
11. `tests/fixtures/presets/prompt-parity-before.txt` and `prompt-parity-session.json` captured here,
    before any module begins reading the preset.
12. `tests/package_artifact/test_archives.py` updates its exact wheel/sdist payload oracle for the
    eight Group 1 runtime files required by this approved wish: `protocol.py`, the three preset
    Python modules, and the four `pt_br_clinic` TOMLs. Existing byte-for-byte archive checks remain
    intact; this is an oracle migration, not a relaxation of artifact coverage.

**Acceptance criteria.**

- [ ] `scripts/pt-br-inventory.py` with no `--max` reports a non-zero total for the package today and
      writes it to `inventory-baseline.txt`; the total and file count are recorded in the group's
      evidence.
- [ ] The inventory script **fails** on a planted accented literal and on a planted unaccented
      vocabulary literal, and **passes** on a planted English literal — all three probed.
- [ ] The inventory script does **not** count docstrings — probed with a planted pt-BR docstring.
- [ ] The stable-id allowlist is exact and cannot hide an added vocabulary term; planted unlisted
      identifiers still fail the inventory gate.
- [ ] `neurostack_core.protocol` imports successfully in isolation without importing presets, SRM
      engines, prompts, policies or flow handlers; compatibility re-exports preserve public types.
- [ ] All four `pt_br_clinic` files are complete and validate together; accented-content counts meet
      the module baselines, so content was copied rather than dropped.
- [ ] Normalized source containers and loaded TOMLs compare equal for every named container before
      any consumer rewrite; changing a planted unaccented regex, threshold or transition makes the
      comparison fail.
- [ ] A missing file/key, unknown key, oversized file, invalid regex, placeholder mismatch or broken
      cross-reference raises `PresetError` with file, dotted key/index, sanitized rejected-value
      summary, expected contract and next action, without dumping full prompt content.
- [ ] A failed load leaves selection unset; default first access selects `pt_br_clinic`; selecting an
      explicit preset first works; same-source reselection is idempotent; different-source
      reselection raises. Each stateful case is tested in a fresh subprocess.
- [ ] Every nested preset mapping/list is read-only; attempted mutation fails. Concurrent first-use
      probes publish exactly one complete object, give every loser an actionable `PresetError`, never
      deadlock, and leave no partial state after an invalid contender.
- [ ] Missing or extra values in each namespaced protocol family fail independently for both bundled
      presets; lookup keys are never conflated with emitted policy IDs/names or enum reasons.
- [ ] Bundled names reject separators/traversal and resolve with `importlib.resources`; explicit paths
      are documented and tested as trusted operator input.
- [ ] `from neurostack_core import use_preset, get_preset, Preset, PresetError` succeeds, and
      lifecycle errors name selected/requested sources plus the restart requirement.
- [ ] Exact wheel and sdist payload tests include the eight required protocol/preset files and still
      reject every unexpected archive member while comparing packaged bytes to source bytes.
- [ ] `core_suite_gate` and `full_suite_gate` green — this group copies content but changes no runtime
      behavior; the parity-oracle test directories are unchanged.

**Validation.**

```bash
set -euo pipefail
source scripts/wish-gates.sh

# the inventory script must MEASURE, then be proven to fail correctly
poetry run python scripts/pt-br-inventory.py packages/neurostack-core/neurostack_core \
  | tee tests/fixtures/presets/inventory-baseline.txt
grep -qE 'CLOSED INVENTORY: [0-9]+ literals across [0-9]+ files' \
  tests/fixtures/presets/inventory-baseline.txt

probe="$(mktemp -d)"; parity_probe=""
trap 'rm -rf "$probe"; test -z "${parity_probe:-}" || rm -f "$parity_probe"' EXIT
printf 'X = "harmoniza\xc3\xa7\xc3\xa3o facial"\n'      > "$probe/accented.py"
printf 'X = "agendar consulta"\n'                        > "$probe/vocab.py"
printf 'X = "schedule an appointment"\n'                 > "$probe/english.py"
printf '"""agendar consulta."""\nX = "hello"\n'          > "$probe/docstring.py"
! poetry run python scripts/pt-br-inventory.py --max 0 "$probe/accented.py"   # must FAIL
! poetry run python scripts/pt-br-inventory.py --max 0 "$probe/vocab.py"      # must FAIL
poetry run python scripts/pt-br-inventory.py --max 0 "$probe/english.py"      # must PASS
poetry run python scripts/pt-br-inventory.py --max 0 "$probe/docstring.py"    # docstrings excluded

# the parity guard must see uncommitted working-tree edits, not only commits
parity_probe=tests/srm/.wish-parity-probe.py
test ! -e "$parity_probe"
printf 'raise AssertionError("planted parity edit")\n' > "$parity_probe"
! parity_tests_unchanged
rm -f "$parity_probe"; parity_probe=""
parity_tests_unchanged

# the TOMLs contain the full copied corpus before any source literal is removed
! poetry run python scripts/pt-br-inventory.py --max 0 --include-toml \
  packages/neurostack-core/neurostack_core/presets/pt_br_clinic

# schema, stable protocol, startup lifecycle and atomic failure
preset_loader_gate
pt_br_preset_unchanged
poetry run python scripts/preset-parity.py compare \
  --baseline tests/fixtures/presets/pt-br-containers.json --preset pt_br_clinic

# the exact artifact oracle must recognize, and byte-compare, the required new runtime payload
poetry run pytest \
  tests/package_artifact/test_archives.py::test_wheel_has_the_exact_recursive_payload \
  tests/package_artifact/test_archives.py::test_sdist_has_exact_build_and_consumer_inputs \
  -q -p no:cacheprovider

core_suite_gate
full_suite_gate
static_gate
parity_tests_unchanged
```

_Scope rationale: this group ships executable gate machinery that every later group's acceptance
depends on, so its validation is the full gate plus adversarial probes of each gate. A gate that has
never been observed to fail is not evidence._

**depends-on:** none

---

### Group 2: `srm` preset lookups and canonical `FunnelStage` consolidation

**Goal.** Replace all **360** measured literals across **7** `srm` files with lookups into the
already-populated `srm.toml`, retain `FunnelStage` as the stable protocol, and remove the duplicate
API declaration in the same group.

**Files and measured counts.**

| File | Literals |
|------|---------:|
| `srm/intent_classifier.py` | 254 |
| `srm/safety_detector.py` | 26 |
| `srm/engine.py` | 21 |
| `srm/funnel_detector.py` | 21 |
| `srm/rag_decider.py` | 20 |
| `srm/handoff_decider.py` | 16 |
| `srm/models.py` | 2 |

**Deliverables.**

1. `INTENT_DEFINITIONS` (197 accented literals, 29 pt-BR intents, 29 English overrides, 238 pt
   regexes) and `_SAFETY_GUARD_PATTERNS` (14 guards) read `[intents]` / `[safety_guards]` from the
   Group 1 `srm.toml`.
2. `SAFETY_PATTERNS`, `FUNNEL_STAGES`, `TRANSITION_TRIGGERS` (33 entries / 16 distinct intents),
   `TURN_THRESHOLDS`, and the two module-scope regexes in `engine.py` read the validated preset.
3. The imperative remainder — strings built inside method bodies in `engine.py`, `rag_decider.py`,
   `handoff_decider.py`, `safety_detector.py` — becomes preset lookups, not inline text.
4. `FunnelStage` is imported from the Group 1 protocol leaf with the same member names and values;
   `srm.models` re-exports it. The loader requires `srm.toml` metadata/transitions for exactly that
   value set, so runtime symbolic references remain valid without a preset dependency cycle.
5. `src/api/schemas/sessions.py` deletes its local enum, imports
   `neurostack_core.protocol.FunnelStage`, and keeps its Pydantic default. A compatibility test proves
   the schema, SRM models and stored string values use the same class and still deserialize all
   existing stage values.
6. `tests/presets/test_protocol_compatibility.py` pins enum values, API identity/default behavior,
   every canonical value through the actual app deserialization path and JSON round-trip, the
   existing unknown-value fallback, and rejection of missing or extra preset stage ids.
7. No Group 1 preset file is edited; discrepancies are loader/schema failures, not ad hoc data fixes
   hidden inside the consumer conversion.
8. `tests/scripts/test_application_core_imports.py` recognizes exactly one new app-side protocol
   consumer: `src/api/schemas/sessions.py` may import only `neurostack_core.protocol`. Existing
   consumers remain facade-only, unknown consumer paths still fail exact-set equality, and this
   exception does not make the protocol leaf a globally allowed facade.

**Acceptance criteria.**

- [ ] `inventory_gate` reports **0** for all 7 `srm` files.
- [ ] `srm.toml` parses under `tomllib`, and the content survived the move: the preset's accented
      literal count is at least the 255 removed, proving content moved rather than vanished.
- [ ] No escape survived as text: the TOML contains no `\u00` sequence, while containing the decoded
      characters.
- [ ] The public API is unchanged: `from neurostack_core.srm import get_srm_engine, SRMInput,
      FunnelStage` still works and `FunnelStage.LEAD_NOVO.value == 'lead_novo'`.
- [ ] The canonical enum value set equals the preset stage-id set exactly; missing and extra ids both
      raise `PresetError` before selection.
- [ ] `src/api/schemas/sessions.py` declares no enum, imports the exact canonical class, and its
      default and every existing persisted value validate unchanged.
- [ ] The application-import oracle permits only the approved sessions-to-protocol leaf edge while
      continuing to reject unknown consumers, deep imports and protocol imports from other paths.
- [ ] Unknown stage ids in a preset raise `PresetError`, not `KeyError` or a silent skip.
- [ ] `core_suite_gate` green and the four parity-oracle directories are unchanged; additive preset
      contract tests are allowed and collected.
- [ ] `full_suite_gate` green — the API schema consolidation broke nothing.
- [ ] `static_gate` green, including `mypy` over both trees.

**Validation.**

```bash
set -euo pipefail
source scripts/wish-gates.sh
PKG=packages/neurostack-core/neurostack_core
PRESET=$PKG/presets/pt_br_clinic

inventory_gate $PKG/srm
pt_br_preset_unchanged
poetry run python scripts/preset-parity.py compare \
  --baseline tests/fixtures/presets/pt-br-containers.json --preset pt_br_clinic --module srm

# content moved, not deleted: the preset must now carry what the source lost
poetry run python -c "
import tomllib, pathlib, re, sys
d = tomllib.loads(pathlib.Path('$PRESET/srm.toml').read_text('utf-8'))
def strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values(): yield from strings(v)
    elif isinstance(o, list):
        for v in o: yield from strings(v)
ACC = re.compile(r'[ãâáàçéêíóôõúüÃÂÁÀÇÉÊÍÓÔÕÚÜ]')
n = sum(1 for s in strings(d) if ACC.search(s))
print('preset accented literals:', n)
sys.exit(0 if n >= 255 else 1)
"
# escapes were decoded, not copied through as text
poetry run python -c "
import pathlib, sys
t = pathlib.Path('$PRESET/srm.toml').read_text('utf-8')
assert '\\\\u00' not in t, 'undecoded escape survived into the preset'
assert 'ç' in t, 'decoded characters missing'
"
# the static protocol must exactly match the preset data contract
poetry run python -c "
import pathlib, tomllib
from neurostack_core.srm import FunnelStage
preset = set(tomllib.loads(pathlib.Path('$PRESET/srm.toml').read_text('utf-8'))['funnel']['stages'])
canonical = {s.value for s in FunnelStage}
assert preset == canonical, preset ^ canonical
"
# unknown stage ids must surface as PresetError, never as a silent skip
poetry run python -c "
import neurostack_core as nc, pathlib, shutil, tempfile
tmp = pathlib.Path(tempfile.mkdtemp()) / 'broken'
shutil.copytree('$PRESET', tmp)
p = tmp / 'srm.toml'
p.write_text(p.read_text('utf-8').replace('lead_novo', 'stage_que_nao_existe', 1), 'utf-8')
try:
    nc.use_preset(tmp); raise SystemExit('unknown stage id did not raise')
except nc.PresetError as e:
    assert 'stage_que_nao_existe' in str(e) or 'lead_novo' in str(e), e
"
# the public contract must not shift
poetry run python -c "
from neurostack_core.srm import get_srm_engine, SRMInput, FunnelStage
assert FunnelStage.LEAD_NOVO.value == 'lead_novo'
"
poetry run pytest tests/presets/test_protocol_compatibility.py -q -p no:cacheprovider
poetry run pytest tests/scripts/test_application_core_imports.py -q -p no:cacheprovider
preset_loader_gate
core_suite_gate
full_suite_gate
static_gate
parity_tests_unchanged
```

_Scope rationale: this group rewrites shared core behavior and consolidates an API type boundary, so
the repository full gate, explicit persistence/API compatibility tests and type checking match its
reach. The narrow unchanged-test check preserves the parity oracle without rejecting new contract
tests the wish explicitly requires._

**depends-on:** 1

---

### Group 3: `prompt` templates → preset

**Goal.** Replace all **155** measured literals across **5** `prompt` files with lookups into the
Group 1 `prompt.toml`, without editing the captured preset data.

**Files and measured counts.**

| File | Literals | Shape |
|------|---------:|-------|
| `prompt/templates/normal.py` | 112 | 16 declarative, 96 inside five methods |
| `prompt/templates/handoff.py` | 23 | 7 declarative, 16 inside two methods |
| `prompt/templates/safety.py` | 13 | 5 declarative, 8 inside two methods |
| `prompt/renderer.py` | 4 | inside `PromptRenderer` / `VariableResolver` |
| `prompt/templates/base.py` | 3 | inside `BaseTemplate` |

**Deliverables.**

1. `NORMAL_SYSTEM_PROMPT`, `HANDOFF_SYSTEM_PROMPT`, `SAFETY_SYSTEM_PROMPT`, `FUNNEL_INSTRUCTIONS`
   (15 literals, keyed by stage), `HANDOFF_REASON_INSTRUCTIONS`, `SAFETY_CATEGORY_INSTRUCTIONS`
   become reads from their already-captured `prompt.toml` entries.
2. The imperative remainder — `_build_clinic_context` (25), `_get_intent_instruction` (31),
   `build_system_message` (20), `get_context_instructions` (8 + 9 + 3), `generate_handoff_summary`
   (6), `get_constraints` (5) — refactored to look strings up from the preset by key.
3. `FUNNEL_INSTRUCTIONS` keys are **stage ids from the preset**, not hardcoded pt-BR names — this is
   the Group 2 ↔ Group 3 contract, and a mismatch must raise `PresetError` at load.

**Acceptance criteria.**

- [ ] `inventory_gate` reports **0** for all 5 `prompt` files.
- [ ] `prompt.toml` parses; its accented-literal count is at least the 143 removed.
- [ ] `FUNNEL_INSTRUCTIONS` keys equal the canonical stage set; a missing or unknown key raises
      `PresetError` naming it.
- [ ] Rendering a fixed session produces a **byte-identical** prompt to a pre-change capture.
- [ ] `core_suite_gate` green and no parity-oracle test file edited.
- [ ] `full_suite_gate` green — app consumers of shared prompt exports remain compatible.
- [ ] `static_gate` green.

**Validation.**

```bash
set -euo pipefail
source scripts/wish-gates.sh
PKG=packages/neurostack-core/neurostack_core
PRESET=$PKG/presets/pt_br_clinic

inventory_gate $PKG/prompt
pt_br_preset_unchanged
poetry run python scripts/preset-parity.py compare \
  --baseline tests/fixtures/presets/pt-br-containers.json --preset pt_br_clinic --module prompt
poetry run python -c "
import tomllib, pathlib, re, sys
t = pathlib.Path('$PRESET/prompt.toml').read_text('utf-8')
d = tomllib.loads(t)
ACC = re.compile(r'[ãâáàçéêíóôõúüÃÂÁÀÇÉÊÍÓÔÕÚÜ]')
def strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values(): yield from strings(v)
    elif isinstance(o, list):
        for v in o: yield from strings(v)
n = sum(1 for s in strings(d) if ACC.search(s))
print('preset accented literals:', n)
sys.exit(0 if n >= 143 else 1)
"
# the G2 <-> G3 contract: every funnel-instruction key must be a real stage
poetry run python -c "
import tomllib, pathlib
from neurostack_core.srm import FunnelStage
d = tomllib.loads(pathlib.Path('$PRESET/prompt.toml').read_text('utf-8'))
keys = set(d['funnel_instructions'])
members = {m.value for m in FunnelStage}
assert keys == members, f'stage-key mismatch: {keys ^ members}'
"
# rendered output must be byte-identical to the pre-change capture
poetry run python scripts/capture-prompt.py --session tests/fixtures/presets/prompt-parity-session.json \
  > /tmp/prompt-after.txt
diff -u tests/fixtures/presets/prompt-parity-before.txt /tmp/prompt-after.txt
core_suite_gate
full_suite_gate
static_gate
parity_tests_unchanged
```

_Scope rationale: prompt text is the model's entire instruction surface, so a silent drift is a
behaviour change no unit test would necessarily catch — hence the byte-identical render diff on top
of the suite. The repository-wide gate is proportional because app tests outside the core parity
directories also import shared prompt exports. The capture is taken before the group starts and
committed._

**depends-on:** 2

---

### Group 4: `policy` rules → preset

**Goal.** Replace all **77** measured literals across **2** `policy` files with lookups into the
Group 1 `policy.toml`, without editing the captured preset data.

**Files and measured counts.**

| File | Literals |
|------|---------:|
| `policy/global_rules.py` | 76 |
| `policy/models.py` | 1 |

**Deliverables.**

1. The eight `RULE_*` constants (`RULE_NO_DIAGNOSIS` 9, `RULE_NO_MEDICATION` 8,
   `RULE_NO_TREATMENT_GUARANTEE` 2, `RULE_NO_LEGAL_ADVICE` 6, `RULE_NO_FALSE_PROMISES` 2,
   `RULE_PRIVACY_COMPLIANCE` 3, `RULE_PROFESSIONAL_TONE` 2, `RULE_EMERGENCY_REDIRECT` 8) become
   reads from `[[rules]]` entries in `policy.toml`, preserving both distinct fields:
   `rule_id` (for example `C_MEDICAL_NO_DIAGNOSIS`) and `rule` (for example
   `no_medical_diagnosis`).
2. `GLOBAL_SAFETY_POLICY` and `GLOBAL_CONSTRAINTS` (22) read `policy.toml`.
3. The one literal in `policy/models.py` (`class PolicyConstraints`) → preset lookup.
4. **Both rule namespaces stay byte-identical** — no invented shorthand such as `no_diagnosis`.
   Every preset supplies the exact canonical `(rule_id, rule)` pairs; missing, extra or cross-paired
   values raise `PresetError`.

**Acceptance criteria.**

- [ ] `inventory_gate` reports **0** for both `policy` files.
- [ ] `policy.toml` parses; accented-literal count at least the 64 removed.
- [ ] All eight exact `(rule_id, rule)` pairs are present. Separate planted missing, extra and
      cross-paired values raise `PresetError` naming the affected namespace/value.
- [ ] `RULE_EMERGENCY_REDIRECT`'s Brazilian emergency numbers (CVV 188, SAMU 192, Bombeiros 193,
      Polícia 190) live **only** in the preset — this is safety-critical copy and must not be
      inherited by a foreign preset. Asserted by grep against the package source.
- [ ] `core_suite_gate` green and no parity-oracle test file edited.
- [ ] `full_suite_gate` green — app consumers of shared policy behavior remain compatible.
- [ ] `static_gate` green.

**Validation.**

```bash
set -euo pipefail
source scripts/wish-gates.sh
PKG=packages/neurostack-core/neurostack_core
PRESET=$PKG/presets/pt_br_clinic

inventory_gate $PKG/policy
pt_br_preset_unchanged
poetry run python scripts/preset-parity.py compare \
  --baseline tests/fixtures/presets/pt-br-containers.json --preset pt_br_clinic --module policy
poetry run python -c "
import tomllib, pathlib
d = tomllib.loads(pathlib.Path('$PRESET/policy.toml').read_text('utf-8'))
pairs = {(r['rule_id'], r['rule']) for r in d['rules']}
required = {
  ('C_MEDICAL_NO_DIAGNOSIS', 'no_medical_diagnosis'),
  ('C_MEDICAL_NO_MED_CHANGE_POSTCARE', 'no_medication_prescription'),
  ('C_COMPLIANCE_NO_PROMISE_OF_RESULTS', 'no_treatment_guarantee'),
  ('global_no_legal_advice', 'no_legal_advice'),
  ('global_no_false_promises', 'no_false_promises'),
  ('global_privacy', 'privacy_compliance'),
  ('global_professional_tone', 'professional_tone'),
  ('POL_SAFETY_001_EMERGENCY_POSTCARE_OUT_OF_HOURS', 'emergency_redirect'),
}
assert required == pairs, f'rule contract mismatch: {required ^ pairs}'
"
# safety-critical Brazilian emergency copy must exist ONLY in the preset
if grep -rnE 'CVV|SAMU|Bombeiros' "$PKG" --include='*.py'; then
  echo "emergency copy still hardcoded in package source" >&2; exit 1
fi
grep -q 'SAMU' "$PRESET/policy.toml"
# a preset missing a required rule must fail loudly
poetry run python -c "
import neurostack_core as nc, pathlib, shutil, tempfile
tmp = pathlib.Path(tempfile.mkdtemp()) / 'broken'
shutil.copytree('$PRESET', tmp)
p = tmp / 'policy.toml'
p.write_text(p.read_text('utf-8').replace('POL_SAFETY_001_EMERGENCY_POSTCARE_OUT_OF_HOURS', 'renamed_away', 1), 'utf-8')
try:
    nc.use_preset(tmp); raise SystemExit('missing rule id did not raise')
except nc.PresetError as e:
    assert 'POL_SAFETY_001_EMERGENCY_POSTCARE_OUT_OF_HOURS' in str(e), e
"
core_suite_gate
full_suite_gate
static_gate
parity_tests_unchanged
```

_Scope rationale: these are the safety rules that stop the assistant giving medical and legal advice.
The repository-wide suite plus an explicit assertion that emergency copy cannot leak into a foreign
preset is proportional to shared runtime content whose failure mode is patient harm, not a broken
build._

**depends-on:** 2

---

### Group 5: `flow` handler responses → preset

**Goal.** Replace all **27** measured literals across **4** `flow` files with lookups into the Group 1
`flow.toml`, without editing the captured preset data.

**Files and measured counts.**

| File | Literals |
|------|---------:|
| `flow/handlers/handoff.py` | 14 |
| `flow/handlers/safety.py` | 9 |
| `flow/handlers/normal.py` | 2 |
| `flow/handlers/rag.py` | 2 |

**Deliverables.**

1. `HANDOFF_RESPONSES` (10) and `SAFETY_RESPONSES` (5) read `flow.toml`, keyed by handoff reason and
   safety category respectively.
2. The imperative remainder inside `HandoffModeHandler` (4), `SafetyModeHandler` (3),
   `NormalModeHandler` (2) and `RagModeHandler` (2) → preset lookups.
3. `flow/handlers/safety.py` lines 21 and 33 — the Brazilian emergency strings
   (`CVV: 188 (24h) | SAMU: 192 | Emergência: 190` and `SAMU 192 | Bombeiros 193 | Polícia 190`) →
   preset. **This is Plan Review #4's C1**: user-facing crisis copy that no sweep in the previous
   combined plan reached.
4. Response keys are their own stable namespaces, distinct from enum reasons: handoff responses use
   exactly `explicit_request`, `safety_concern`, `max_turns_exceeded`, `low_confidence`,
   `complex_query`, `escalation`, `scheduling`, `default`, `waiting`, `connected`; safety responses
   use exactly `clinical_risk`, `legal_risk`, `conduct_risk`, `emergency`, `default`.

**Acceptance criteria.**

- [ ] `inventory_gate` reports **0** for all 4 `flow` files.
- [ ] `flow.toml` parses; accented-literal count at least the 26 removed.
- [ ] No emergency-number literal remains anywhere in the package source (asserted by grep, jointly
      with Group 4's check).
- [ ] Both exact response-key sets match the stable protocol. Missing, extra or confused enum-reason
      keys raise `PresetError`.
- [ ] `core_suite_gate` green and no parity-oracle test file edited.
- [ ] `full_suite_gate` green — app consumers of shared flow responses remain compatible.
- [ ] `static_gate` green.

**Validation.**

```bash
set -euo pipefail
source scripts/wish-gates.sh
PKG=packages/neurostack-core/neurostack_core
PRESET=$PKG/presets/pt_br_clinic

inventory_gate $PKG/flow
pt_br_preset_unchanged
poetry run python scripts/preset-parity.py compare \
  --baseline tests/fixtures/presets/pt-br-containers.json --preset pt_br_clinic --module flow
poetry run python -c "
import tomllib, pathlib
d = tomllib.loads(pathlib.Path('$PRESET/flow.toml').read_text('utf-8'))
expected = {
  'handoff_responses': {'explicit_request','safety_concern','max_turns_exceeded',
                        'low_confidence','complex_query','escalation','scheduling',
                        'default','waiting','connected'},
  'safety_responses': {'clinical_risk','legal_risk','conduct_risk','emergency','default'},
}
for table, keys in expected.items():
    actual = set(d[table])
    assert actual == keys, f'{table}: key mismatch {actual ^ keys}'
"
# crisis copy must have left the source entirely (Plan Review #4, C1)
if grep -rnE 'CVV|SAMU|Bombeiros' "$PKG" --include='*.py'; then
  echo "crisis copy still hardcoded in package source" >&2; exit 1
fi
grep -q 'SAMU' "$PRESET/flow.toml"
core_suite_gate
full_suite_gate
static_gate
parity_tests_unchanged
```

_Scope rationale: the flow handlers emit the text the patient actually reads in a crisis. The grep is
narrow but it is the one check that would have caught the CRITICAL the previous plan missed. The
repository-wide gate is proportional because app tests outside the core parity directories consume
these shared response exports._

**depends-on:** 2

---

### Group 6: Foreign-preset proof, installed-wheel proof and README

**Goal.** Prove the engine is genuinely preset-driven by running it on a preset it was not written
for, in a fresh process that respects the immutable lifecycle, and prove both presets survive wheel
packaging.

**Deliverables.**

1. `neurostack_core/presets/en_generic/{srm,flow,policy,prompt}.toml` — a compact but schema-complete
   English preset. It supplies every canonical stage, intent, category, reason, rule and response id
   with enough transitions, patterns and templates to exercise every schema path. It is a proof
   fixture, not a production translation (Scope OUT).
2. `tests/presets/test_foreign_preset.py` — launches fresh subprocesses. The English process selects
   `en_generic` before first content access and exercises every configured behavior path; the
   default process selects `pt_br_clinic` implicitly and proves original classification. Generated
   prompts and user-visible responses are checked for pt-BR leakage with the inventory detector.
   The case matrix records coverage sets and asserts equality with every configured intent,
   transition, safety category, policy rule, response key and template key, so fixture and test
   cannot omit the same item silently.
3. Clean-wheel validation builds and installs outside the repository, then loads each bundled preset
   in a separate installed-only process via `importlib.resources` and verifies the default selection.
4. README reference and how-to sections: both public signatures/return fields/exceptions; the four
   files; complete namespaced identifier contract; exact order `import neurostack_core` → optional
   `use_preset(...)` → import consumer; imports that close selection; idempotent same-source calls;
   rejected different-source calls; error recovery; `str` name versus `PathLike` resolution; trusted
   path boundary; library-only non-default selection; and the proof-only status of `en_generic`.
   Errors say what/why/next without echoing full prompt content.
5. `scripts/run-core-readme-examples.py` extracts the marked default, bundled-selection and trusted
   PathLike examples from the README and runs each in a fresh installed-wheel process. The examples
   are the artifact proof; no handwritten duplicate snippets are accepted.
6. Final whole-package `inventory_gate` at `--max 0` plus `preset_loader_gate` over both bundled
   presets.

**Acceptance criteria.**

- [ ] `inventory_gate --max 0` over the **entire** package source exits 0 for measured content; the
      separately pinned protocol-id allowlist still equals the canonical enums.
- [ ] The same script over `presets/pt_br_clinic/` still reports a large non-zero count: the content
      moved rather than being deleted.
- [ ] A fresh `en_generic` process exercises every configured intent, transition, safety category,
      policy rule, flow response and prompt template; all outputs are English and use canonical
      stage ids.
- [ ] Mechanical coverage-set equality proves every configured key has at least one executed case;
      a planted unreferenced configured item fails the matrix.
- [ ] A separate fresh default process reproduces the original pt-BR classification and prompt
      capture. No test changes the selected preset in-process.
- [ ] Calling `use_preset('pt_br_clinic')` after selecting `en_generic` is explicitly tested to raise
      `PresetError`; hot-swap is not accidentally implemented to satisfy the proof.
- [ ] The built wheel contains all eight TOMLs. From a working directory outside the repository,
      clean-venv processes load each bundled preset and default to `pt_br_clinic` with no source-tree
      path available.
- [ ] All marked README examples are extracted and executed against that installed wheel, including
      explicit ordering and actionable lifecycle failure output.
- [ ] `core_suite_gate` green with the default preset; `full_suite_gate` matches the pinned set.
- [ ] `static_gate` green.

**Validation.**

```bash
set -euo pipefail
source scripts/wish-gates.sh
PKG=packages/neurostack-core/neurostack_core

# the whole package, at zero
inventory_gate "$PKG"
# ...but the content still exists, in the preset
if poetry run python scripts/pt-br-inventory.py --max 0 --include-toml "$PKG/presets/pt_br_clinic"; then
  echo "preset is empty — content was deleted, not moved" >&2; exit 1
fi

# the localized-content extraction claim itself: each preset gets a fresh process
poetry run python -c "
import neurostack_core as nc
from uuid import uuid4
nc.use_preset('en_generic')
from neurostack_core.srm import get_srm_engine, SRMInput
out = get_srm_engine().process(SRMInput(
    tenant_id=uuid4(), session_id=uuid4(),
    message='I want to book an appointment for next week'))
assert out.intent_category.value == 'scheduling', out
print('en_generic ->', out.intent, out.intent_category, out.funnel_stage)
try:
    nc.use_preset('pt_br_clinic')
except nc.PresetError:
    pass
else:
    raise SystemExit('different-source reselection did not fail')
"
poetry run python -c "
from uuid import uuid4
from neurostack_core.srm import get_srm_engine, SRMInput
out = get_srm_engine().process(SRMInput(
    tenant_id=uuid4(), session_id=uuid4(), message='quero agendar uma consulta'))
assert out.intent == 'agendar', out
print('pt_br_clinic ->', out.intent, out.intent_category, out.funnel_stage)
"

# exhaustive proof-fixture matrix and lifecycle assertions run in subprocesses
poetry run pytest tests/presets/test_foreign_preset.py -q -p no:cacheprovider
preset_loader_gate
pt_br_preset_unchanged

# prove package data from the artifact, not the source checkout
artifact="$(mktemp -d)"; trap 'rm -rf "$artifact"' EXIT
poetry run python -m build packages/neurostack-core --outdir "$artifact/dist"
wheel="$(find "$artifact/dist" -maxdepth 1 -name '*.whl' -print -quit)"
test -n "$wheel"
for preset in pt_br_clinic en_generic; do
  for part in srm flow policy prompt; do
    unzip -l "$wheel" | grep -q "presets/$preset/$part.toml"
  done
done
python3 -m venv "$artifact/venv"
"$artifact/venv/bin/pip" install --quiet "$wheel"
(
  cd "$artifact"
  env -i LC_ALL=C.UTF-8 "$artifact/venv/bin/python" -c \
    "import neurostack_core as n; assert n.get_preset().name == 'pt_br_clinic'"
  env -i LC_ALL=C.UTF-8 "$artifact/venv/bin/python" -c \
    "import neurostack_core as n; n.use_preset('en_generic'); assert n.get_preset().name == 'en_generic'"
)
poetry run python scripts/run-core-readme-examples.py \
  --python "$artifact/venv/bin/python" --workdir "$artifact"

core_suite_gate
full_suite_gate
static_gate
parity_tests_unchanged
```

_Scope rationale: this is the release gate for the wish's central claim. It runs every gate plus the
fresh-process behavior matrix and installed-wheel test. The detector, configured-path coverage and
artifact proof cover different failure classes; none is treated as sufficient alone._

**depends-on:** 3, 4, 5

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Functional: a real WhatsApp message through the live pipeline classifies intent, funnel stage
      and safety identically to pre-merge — spot-check a scheduling message, a price question and a
      safety-triggering message.
- [ ] Functional: a crisis message still returns the Brazilian emergency numbers (CVV 188, SAMU 192)
      — the copy moved into the preset but must still reach the patient.
- [ ] Functional: generated prompts are unchanged against a pre-merge capture for a fixed session.
- [ ] Integration: app boots, `/health` green, `/metrics` unchanged; handoff fires on the same
      conditions, confirming app consumers use the canonical `FunnelStage` and selected preset.
- [ ] Integration: session rows written before the merge still deserialise — stage values are
      unchanged strings, so stored records must remain readable.
- [ ] Regression: `alembic_version` unchanged after deploy; this wish introduces no migration.
- [ ] Regression: cognitive-pipeline latency unchanged — preset load happens once at startup.
- [ ] Boundary: `make lint` green on dev, including the package-boundary check inherited from the
      extraction wish.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| **The detector is a lower bound and something is missed anyway.** Accents alone miss 133 literals; the vocabulary list is finite and a pt-BR word outside it is invisible | High | Decision 8 deliberately combines the lexical gate with a fresh-process matrix over every path configured by `en_generic`. The plan does not claim one scheduling example proves unreachable code. |
| **A stage id changes value and stored session rows stop deserialising** | High | Decision 4 keeps the enum static and byte-identical; Group 2 asserts its full value set, API class identity, existing-value deserialization and exact equality with every preset. |
| **Preset selection races or a failed load publishes mixed module state** | High | Decision 5 parses and validates all four files into a frozen aggregate under a lock and publishes once. Group 1 probes concurrent first access and failure atomicity in fresh subprocesses. |
| **Safety-critical crisis copy is lost, or inherited by a foreign preset** | High | Groups 4 and 5 both assert that no emergency literal remains in package source and that it is present in the pt-BR preset; a QA criterion re-checks it against live traffic. |
| **An external preset supplies catastrophic regexes or malicious prompt text** | High | Decision 11 makes paths trusted operator configuration, rejects tenant/remote selection, caps file size, compiles regexes and validates placeholders before publication. Untrusted third-party preset execution remains explicitly out of scope. |
| **Prompt text drifts during the move and the model silently degrades** | High | Group 3 diffs a rendered prompt byte-for-byte against a capture taken before the group starts. |
| **A group edits a parity test to make its move pass**, destroying the oracle | High | `parity_tests_unchanged` protects exactly `tests/{srm,flow,policy,prompt}` while allowing the additive preset contract tests required by Groups 1, 2 and 6. |
| **The imperative remainder (154 literals inside method bodies) is harder than the declarative move** and blows the appetite | Medium | It is measured and attributed per file and per method up front — `normal.py` alone is 96 across five methods. It is the reason the appetite is large rather than medium, and the reason `prompt` is its own group. |
| **Group 1 copies a large corpus incorrectly before consumers switch** | Medium | The normalized manifest compares every named source container with the loaded TOMLs before rewrites and module-by-module afterward; counts and prompt parity are supplemental. Groups 2–5 treat TOMLs as read-only. |
| **The extraction dependency changes the measured baseline** | Medium | Group 1 remeasures the merged dependency tree and records the authoritative topology/count before edits; the draft's 619/18 figure is not used as an unverified gate. |
| **Distinct identifier namespaces are conflated** | Medium | The protocol leaf separately names enum reasons, policy `rule_id`, policy `rule`, handoff-response and safety-response keys. Loader tests plant missing, extra and cross-paired values in every namespace. |
| **TOMLs work in-tree but are absent from the wheel** | Medium | Group 6 inspects all eight archive paths, installs the wheel in a clean external venv and loads each preset through package resources. |
| **`en_generic` is mistaken for a production English preset** | Low | Named `generic`, documented in the README as a proof fixture, and Scope OUT says so. |

---

## Review Results

### 2026-09-25T17:31:04-03:00 — Plan Review — **FIX-FIRST**

- **Context:** plan review
- **Reviewed path:** `.genie/wishes/neurostack-core-dedomainification/WISH.md`
- **Reviewed SHA-256:** `8d1d0780843e81dab087a99649b0399e181ee39d64a82a13b4748dda1d7df5ce`
- **Validation:** all fenced Bash parsed with `bash -n`; implementation tests were not run because
  `neurostack-core-extraction` is unmerged and the review was read-only.
- **HIGH:** Groups 3–5 changed shared runtime behavior but omitted `full_suite_gate`, leaving app-side
  consumers outside the four core parity directories unvalidated and failures unattributable to the
  responsible parallel group.
- **Required fix:** add `full_suite_gate` to each group's acceptance criteria and validation, retaining
  the focused content/parity checks and adding proportional scope rationale.

### 2026-09-25T17:32:44-03:00 — Plan Re-review (fix loop 1/2) — **SHIP**

- **Context:** plan re-review after a separate fixer changed only the reported gap.
- **Reviewed path:** `.genie/wishes/neurostack-core-dedomainification/WISH.md`
- **Reviewed SHA-256:** `95eb5ee1ff576cb3d69b8d7c0dbcb9d83080927778ae71e6e5d8b609888394f2`
- **Validation:** Groups 3, 4 and 5 each contain one `full_suite_gate` invocation, an acceptance
  criterion and repository-wide risk/reach rationale; all fenced Bash passes `bash -n`; tracked
  fixture paths are unignored.
- **Findings:** no CRITICAL, HIGH, MEDIUM or LOW gaps.
- **Verdict:** SHIP — the plan has complete, executable, risk-proportional evidence and retains its
  aggregate release gate.

### 2026-09-27 — Group 1 execution — **BLOCKED**

- **Task:** `t_muhbvik05b9193ff`, claimed by Curie and left `in_progress` with its implementation
  preserved in the working tree.
- **Diagnosis:** wish-level contract conflict; no implementation fix attempt consumed (0/2). The
  extraction boundary deliberately rejects every source-visible `importlib` spelling, while Group 1
  requires bundled preset resolution through `importlib.resources`.
- **Reproduction:** `source scripts/wish-gates.sh && static_gate` exits 1 with
  `presets/loader.py:5` and `presets/loader.py:100` reported as reserved dynamic-import syntax.
- **Passing evidence before the stop:** inventory baseline 632 literals/17 files with 132 exact
  protocol exemptions; preset inventory 633 literals; loader suite 49 passed; core suite 1017
  passed; wish-gate tests 11 passed; normalized four-module parity, prompt capture, preset hashes,
  package mypy, targeted Flake8 and wheel TOML payload checks passed.
- **Corrective route:** return to wish/dependency design. The extraction WISH explicitly says a
  legitimate future `importlib` consumer triggers a separate boundary-policy wish. Approve that
  dependency (or explicitly revise this WISH's resource-loading contract), re-review the amended
  plan, then resume this claimed group. Do not evade the gate or broaden Group 1 implicitly.

### 2026-10-01 — Group 1 archive-oracle scope amendment — **AUTHORIZED**

- **Observed failure:** after the resource-boundary dependency was merged and Group 1 resumed, the
  focused loader, boundary, parity, core and static gates passed, while `full_suite_gate` gained two
  failures at `tests/package_artifact/test_archives.py:125` and `:160` in addition to the two pinned
  unrelated config-service failures.
- **Diagnosis:** both exact-set assertions rejected exactly the eight runtime files already required
  by Group 1. Production packaging is correct; excluding those files would violate Deliverables 4–6
  and Decision 9. This is a plan file-scope omission, not an implementation or packaging defect.
- **Authorization:** Lorenzo explicitly authorized delivery of the plan and the minimal scope
  amendment. Group 1 now owns the exact archive-oracle migration described in Deliverable 12; no
  other artifact-test behavior or production scope is added.
- **Required revalidation:** the two focused wheel/sdist tests, `full_suite_gate`, the original Group
  1 validation, independent execution review, and quality review must all pass before task
  `t_muhbvik05b9193ff` may be marked done.

### 2026-10-01 — Group 1 execution review — **FIX-FIRST**

- **Verified clean surfaces:** dependency boundary blobs match approved commit `6355cbd`; inventory
  baseline and preset totals are 633/18 and 633/4; four-module normalized parity, TOML hashes,
  prompt capture, lazy root import, protocol isolation, public exports, lifecycle basics, unchanged
  parity-oracle directories, and exact archive membership/byte comparisons are intact.
- **HIGH — inventory closure:** `presets/loader.py` contains the measured literal
  `atendimento_tipo`, so the eventual whole-package `inventory_gate --max 0` cannot pass even after
  consumer conversion. Move this persisted slot key into an exact canonical protocol namespace,
  use that namespace in loader validation, update the exact allowlist/data/hash/parity fixtures, and
  add a regression proving Group 1 loader/protocol sources are inventory-clean.
- **HIGH — incomplete threshold schema:** `flow.thresholds` has no exact-key or scalar type/range
  validation; missing `rag_max_turns`, unknown keys, and invalid threshold types are accepted. Add
  fail-closed validation and nested missing/extra/type regressions.
- **HIGH — malformed placeholders:** unmatched or residual `{{`/`}}` can survive when no complete
  supported placeholder is found, including mixed valid/malformed strings. Reject residual brace
  syntax after recognized placeholders are removed and add both malformed-only and mixed cases.
- **Diagnosis:** implementation/test defect; specification and context are complete, environment is
  functional, and no model/effort escalation is authorized.
- **Verdict:** FIX-FIRST. Correct in fix loop 2/2, then rerun the complete original Group 1
  validation and repeat independent execution review.

### 2026-10-01 — Group 1 execution re-review (fix loop 2/2) — **SHIP**

- **Corrected:** the loader now consumes the exact canonical `ENGINE_REGEX_KEYS` namespace;
  inventory is clean for loader/protocol sources, the allowlist equals all 134 protocol identifiers,
  and unlisted variants still fail. Exact threshold keys/types/ranges and residual malformed prompt
  placeholders now fail closed with focused regressions.
- **Authoritative measurements:** source baseline 629 literals across 17 files; preset corpus 630
  literals across four TOMLs. The four-literal delta from the earlier 633 baseline is exactly the
  newly canonical `atendimento_tipo` key in one former loader literal plus three existing container
  key occurrences; no content or regex was removed.
- **Independent evidence:** original counterexamples reject; missing/extra engine regex keys reject;
  four-module normalized parity, four TOML hashes, byte-identical prompt capture, protocol isolation,
  lazy selection/public exports, lifecycle basics, archive exactness and unchanged parity-oracle
  directories all pass.
- **Exact-tree gate evidence:** loader 83 passed; boundary 102 passed; core 1,017 passed; artifact
  oracle 2 passed; full gate 3,479 passed and 19 skipped with only the two pinned config-service
  failures; static, package-boundary and planted parity-guard gates passed.
- **Findings:** no CRITICAL, HIGH, MEDIUM or LOW gaps.
- **Verdict:** SHIP.

### 2026-10-01 — Group 1 quality review — **FIX-FIRST**

- **HIGH — reachable mutability:** `FrozenMap._data` can be rebound after construction, replacing a
  selected preset mapping with a mutable container. Block attribute reassignment after
  initialization and add top-level plus nested rebinding regressions.
- **HIGH — incomplete leaf typing:** exact keys and selected cross-references are validated, but
  malformed consumed leaf values such as string `prompt.chars_per_token`, string flow mode priority,
  and string policy `enabled` can publish. Add small reusable validators covering every consumed
  leaf across all four modules, with representative fail-closed regressions.
- **HIGH — prompt leakage in errors:** short rejected prompt strings are rendered verbatim by the
  generic value summary. Redact prompt values regardless of length while retaining filename,
  dotted key, type/length, expected contract and next action; assert a private sentinel never
  appears.
- **Clean evidence:** dependency boundary blobs, preset hashes, source/preset inventory, exact
  allowlist, unchanged parity-oracle directories, archive exactness, configure-once locking and
  first-use-only I/O remain sound.
- **Diagnosis:** implementation/test defect; one quality fix loop is available, with no model or
  effort escalation justified.
- **Verdict:** FIX-FIRST. Limit remediation to `presets/models.py`, `presets/loader.py`, and
  `tests/presets/test_preset_loader.py`, then rerun the complete Group 1 validation and quality
  re-review.

### 2026-10-01 — Group 1 quality re-review — **BLOCKED**

- **Corrected:** top-level/nested `FrozenMap._data` rebinding now fails; the original malformed leaf
  types reject; malformed/unknown placeholder sentinels are redacted with actionable structural
  diagnostics.
- **HIGH — non-finite numbers:** intent weights accept TOML `nan`/`inf`, which can suppress an intent
  or force maximum confidence. Numeric validators must reject every non-finite float.
- **HIGH — raw type errors:** non-string cross-references in SRM/flow membership checks and
  TOML-native date/time values can escape as raw `TypeError` instead of actionable `PresetError`.
  Validate string types before membership and recursively validate the freezeable metadata domain
  with dotted paths (or immutably support every accepted TOML scalar).
- **Clean evidence:** hashes, inventory 629/17, preset corpus 630/4, exact 134-id allowlist,
  dependency boundary, archive/parity-oracle scope, core/static/full gate evidence, locking and
  byte-parity remain clean.
- **Diagnosis:** implementation/test defect; complete context and functional environment; no model
  escalation. The normal quality fix budget is exhausted.
- **Verdict:** BLOCKED pending explicit authority for another narrowly scoped correction or human
  acceptance of residual risk.

### 2026-10-01 — Additional quality remediation — **AUTHORIZED**

- **Human direction:** Lorenzo instructed the orchestrator to deliver this plan “a qualquer custo”.
  This authorizes persistence through an additional correction cycle inside the already-approved
  Group 1 code/test scope; it does not authorize weakening gates, accepting the two HIGH risks,
  changing behavior, or expanding product scope.
- **Authorized correction:** reject non-finite numerics, type-check cross-references before set
  membership, and convert unsupported TOML scalar/container values into dotted, sanitized
  `PresetError` diagnostics. Add exact regressions and rerun the complete Group 1 validation plus
  quality re-review.

### 2026-10-01 — Group 1 final quality re-review — **SHIP**

- **Corrected:** all finite-number validators reject `nan` and positive/negative infinity;
  enum/set/mapping references validate strings before membership; nested TOML datetime/date/time
  values reject before freezing; failed public loads leave selection unpublished; error summaries
  remain sanitized.
- **Independent counterexamples:** top-level/nested immutability, malformed leaves, prompt
  redaction, non-finite SRM/flow values, four list-valued cross-references and nested temporal values
  all raise actionable `PresetError`; no raw `TypeError` or `KeyError` remains.
- **Regression evidence:** fixture hashes, inventory 629/17, loader/protocol zero-literal gate,
  dependency boundary, parity-oracle scope, shell syntax and exact artifact byte/membership contracts
  pass. Exact-tree evidence records loader 110, core 1,017, archive 2 and full suite 3,506 passed/19
  skipped with only the two pinned failures, plus static/type/parity gates.
- **Findings:** no CRITICAL, HIGH, MEDIUM or LOW gaps; no residual security, maintainability,
  performance or scope-fidelity issue.
- **Verdict:** SHIP.

### 2026-10-01 — Group 1 orchestrator validation — **PASS**

- **Scope rationale:** Group 1 establishes shared executable schema/lifecycle/gate machinery,
  packaged runtime resources and public API boundaries; the full repository gate, focused
  adversarial probes, artifact builds and static/type gates are proportional and required.
- **Inventory:** authoritative source fixture byte-matches 629 localized literals across 17 files;
  loader/protocol are zero; exact allowlist has 134 identifiers; accented and unaccented planted
  literals fail, English/docstring probes pass; preset corpus remains nonzero at 630/4.
- **Focused behavior:** loader 110 passed; four TOML hashes, four-module normalized parity,
  byte-identical prompt capture and exact artifact tests 2/2 passed.
- **Aggregate behavior:** core parity 1,017/1,017 passed. `full_suite_gate` accepted 3,506 passed and
  19 skipped with only the two exact pinned `test_config_service.py` failures; completion validation
  passed.
- **Static/artifact boundary:** package boundary clean across 45 Python files; Black, isort, Flake8,
  mypy over 173 files, isolated wheel builds, typed-wheel/public-API checks and `git diff --check`
  passed.
- **Gate falsification:** an untracked planted edit under `tests/srm` made
  `parity_tests_unchanged` exit 1; after the probe was removed the guard and diff check passed.
- **Result:** acceptance review SHIP, final quality re-review SHIP, and orchestrator validation PASS;
  task `t_muhbvik05b9193ff` is eligible for `done`.

### 2026-10-01 — Group 2 dependency discovery — Group 1 evidence **INVALIDATED**

- **Observed contradiction:** Group 2 reached SRM inventory 372→0 and passed normalized parity, but
  the required decoded-text gate found 84 literal `\\u00XX` sequences across 51 regex strings in
  `srm.toml`. Group 1's hash and baseline JSON had captured the same representation, so their green
  checks did not prove Decision 2.
- **Behavioral diagnosis:** the 51 escaped and decoded regexes compile to identical `re` opcodes;
  runtime behavior is preserved accidentally, but TOML literal strings retain the six-character
  escape and violate the extraction contract. Cause is `missing-context`: parity normalization did
  not model raw-Python-regex Unicode escapes versus TOML literal-string representation.
- **Corrective route:** reopen Group 1. Decode only Unicode `\\uXXXX` sequences in regex fields,
  never generic `unicode_escape`; make extraction/parity normalize those fields deliberately; update
  baseline/hash fixtures; add lexical rejection and 51-pattern compiled-equivalence coverage; rerun
  Group 1 review, quality and full validation. Preserve the Group 2 consumer rewrite pending repair.

### 2026-10-01 — Group 2 stale import-oracle scope amendment — **AUTHORIZED**

- **Observed failure:** the full suite added one failure because
  `tests/scripts/test_application_core_imports.py` discovered the newly required
  `src/api/schemas/sessions.py` consumer but its fixed consumer set predates Group 2.
- **Architecture:** `sessions.py -> neurostack_core.protocol.FunnelStage` is the approved
  dependency-free leaf edge; routing through `neurostack_core.srm` would contradict the deliverable
  and eagerly load content/preset modules.
- **Authorized migration:** Group 2 now owns the path-specific oracle change in Deliverable 8. Only
  `sessions.py` may import only `neurostack_core.protocol`; all legacy facade, unknown-consumer and
  deep-import prohibitions remain fail-closed. This uses Lorenzo's explicit authorization to deliver
  the plan and does not broaden application imports generally.
- **Diagnosis:** `missing-context` / plan file-scope omission, not implementation drift, ambiguity,
  environment failure, overdesign or model capacity.

### 2026-10-01 — Corrective task 1R execution review — **SHIP**

- **Evidence:** `srm.toml` and canonical baseline contain zero literal Unicode regex escapes; exactly
  84 decoded characters span the 51 affected regexes; reconstructed legacy and decoded forms have
  zero structural/opcode mismatches; all four module parity checks and hashes pass, with only the
  SRM hash changed.
- **Scope:** normalization is confined to explicit regex containers; no generic `unicode_escape`,
  runtime consumer, API schema or non-regex content changed. Loader/default/lazy/protocol/immutability
  guarantees and compilation of all 480 SRM regexes reproduce cleanly.
- **Findings:** no CRITICAL, HIGH, MEDIUM or LOW acceptance gaps.
- **Verdict:** SHIP.

### 2026-10-01 — Corrective task 1R quality review — **FIX-FIRST**

- **HIGH — unsafe normalization:** the initial regex replacement cannot distinguish an active
  Unicode escape from the second slash of a doubled literal and can decode codepoints into regex
  metacharacters. Reproductions include doubled `\\\\u00e3`, `\\u002e` becoming wildcard `.`, and
  `\\u005b` becoming an invalid `[` pattern while parity/hash could remain green.
- **Required correction:** preserve doubled-backslash literals, decode only active intended Unicode
  letter/mark escapes, reject unsafe or unsupported codepoints and malformed active escapes, and add
  mutation-sensitive semantic-equivalence tests. Current TOML, baseline and hashes must not change.
- **Clean evidence:** current 51 patterns remain opcode-equivalent; normalization is linear,
  capture-only and deterministic; no production hot path or trust boundary changed.
- **Diagnosis:** implementation/test defect; one narrow quality fix loop is authorized in
  `scripts/preset-parity.py` and `tests/presets/test_preset_loader.py`.
- **Verdict:** FIX-FIRST.

### 2026-10-01 — Corrective task 1R final quality re-review — **SHIP**

- **Corrected:** the linear scanner preserves exactly doubled escapes; decodes exactly one active
  slash only for four-hex non-ASCII Unicode letter/mark codepoints; and rejects ambiguous runs,
  malformed escapes, ASCII/metacharacter/control/surrogate/unsupported codepoints.
- **Independent counterexamples:** doubled `\\\\u00e3` remains byte/opcode-identical; active
  `\\u002e`, `\\u005b` and `\\u005c` fail closed; three/four/five slash runs reject; safe Greek,
  accented and combining-mark escapes remain equivalent. All 51 current patterns/84 escapes match
  the checked-in decoded forms with zero text/opcode mismatch.
- **Quality:** scanner is single-pass, bounded-memory, deterministic and capture-only; non-regex
  strings, `\\x`, `\\N`, ordinary regex tokens, TOML, baseline and hashes remain unchanged.
- **Findings:** no CRITICAL, HIGH, MEDIUM or LOW gaps.
- **Verdict:** SHIP.

### 2026-10-01 — Corrective task 1R orchestrator validation — **PASS**

- **Scope rationale:** parity is the migration oracle for all downstream conversions, so focused
  semantic falsifiers plus full runtime, artifact and static gates are required.
- **Direct evidence:** zero `\\u00XX` remains in `srm.toml`; loader/normalizer 130 passed; all four
  hashes and SRM parity pass; prompt capture is byte-identical; artifact/import/protocol tests 21
  passed; core parity 1,017/1,017 passed.
- **Aggregate evidence:** `full_suite_gate` accepted 3,543 passed and 19 skipped with exactly the two
  pinned config-service failures; package boundary, Black, isort, Flake8, mypy, isolated builds,
  parity-oracle guard and `git diff --check` passed.
- **Result:** execution review SHIP, quality re-review SHIP, orchestrator validation PASS; corrective
  task `t_muplwb536ea560e1` is eligible for `done`.

### 2026-10-01 — Group 2 execution review — **FIX-FIRST**

- **HIGH — import-oracle bypass:** `from neurostack_core import protocol` is recorded only as the
  root package and evades the path-specific protocol policy. Normalize package-root submodule alias
  imports while preserving the intentional root-symbol `enable_metrics` edge, and add source-level
  falsifiers for unknown, legacy and sessions consumers.
- **HIGH — mutable module defaults:** preset-derived globals recreate mutable dict/list tables. A
  supported `SafetyDetector(custom_patterns=...)` instance mutates shallow-copied nested default
  lists, contaminating subsequent instances. Keep module defaults deeply immutable and copy only
  into per-instance customization state; audit all Group 2 preset-derived globals and regress cross-
  instance/preset isolation.
- **MEDIUM — incomplete persisted-value pin:** compatibility tests literally pin only
  `LEAD_NOVO`; coordinated enum/preset drift could pass. Add the full literal 11-member name/value
  mapping and assert exact equality.
- **Clean evidence:** SRM inventory 372→0; normalized parity and hashes pass; `srm.toml` has 307
  accented literals, zero literal Unicode escapes and all 480 regexes compile; current enum mapping,
  lazy cached access, unchanged parity-oracle directories and aggregate/static evidence are clean.
- **Diagnosis:** implementation/test defect; use fix loop 1/2 with no model escalation.
- **Verdict:** FIX-FIRST.

### 2026-10-01 — Group 2 execution re-review (fix loop 1/2) — **SHIP**

- **Corrected:** package-root submodule syntax is normalized into the path-specific oracle while all
  19 legitimate root exports remain valid; legacy, unknown, additional sessions, mixed, deep and
  unknown-root imports reject.
- **Immutability:** all 41 preset-derived globals rejected 315 top-level/nested mutation attempts;
  custom intent/safety/RAG/funnel/handoff instances remain isolated from defaults, selected preset
  and later instances.
- **Protocol pin:** a literal expected mapping fixes all 11 `FunnelStage` names/values independently
  of enum and preset data.
- **Regression evidence:** inventory 372→0; SRM parity/hashes pass; preset contains 307 accented
  literals and zero Unicode escape text; 480 regexes compile; enum identity, round-trip, fallback,
  dependency-free protocol and unchanged parity-oracle scope pass. Exact-tree gates report focused
  22, loader 130, core 1,017, full 3,546 passed/19 skipped with two pinned failures, and clean
  static/type/build gates.
- **Findings:** no CRITICAL, HIGH, MEDIUM or LOW gaps.
- **Verdict:** SHIP.

### 2026-10-01 — Group 2 quality review — **SHIP**

- **Concurrency/lifecycle:** 64 concurrent accesses across 16 workers build one preset and publish
  one identity; protocol-only import leaves selection open; app package imports select the default
  through existing content consumers as allowed by Decision 12.
- **Performance:** all 480 regexes validate before publication; compilation is bounded to module or
  singleton construction with no repeated preset I/O. Per-instance copies are small and linear; no
  measured need for another cache.
- **Architecture/security:** immutable globals preserve customization APIs without cross-instance
  contamination; enum serialization and list-returning compatibility remain; direct/aliased/root/
  nested/parenthesized/deep/star import forms fail closed under the static oracle's declared scope;
  trusted preset path boundary is unchanged.
- **Findings:** no CRITICAL, HIGH, MEDIUM or LOW security, maintainability, performance, regression
  or scope-fidelity gaps.
- **Verdict:** SHIP.

### 2026-10-01 — Group 2 orchestrator validation — **PASS**

- **Scope rationale:** Group 2 rewrites shared SRM behavior and consolidates an app persistence/type
  boundary, so module parity, persistence/import-oracle probes, repository full gate and static/type/
  artifact checks are proportional.
- **Direct content evidence:** SRM inventory is zero; all four preset hashes and SRM normalized parity
  pass; `srm.toml` carries 307 accented literals, decoded characters and zero literal Unicode escape
  text; canonical stages equal preset stages and unknown stage injection raises `PresetError`.
- **Focused evidence:** protocol compatibility plus import-oracle suites 22 passed; preset loader 130
  passed; public SRM imports and canonical `LEAD_NOVO` value pass.
- **Aggregate evidence:** core parity 1,017/1,017 passed; `full_suite_gate` accepted 3,546 passed and
  19 skipped with exactly the two pinned config-service failures; package boundary, Black, isort,
  Flake8, mypy over 173 files, isolated builds, parity guard and `git diff --check` passed.
- **Result:** execution re-review SHIP, quality review SHIP and orchestrator validation PASS; task
  `t_muhbviqz8f0103ed` is eligible for `done`.

---

## Files to Create/Modify

_All package paths are post-extraction paths; this wish depends on `neurostack-core-extraction`._

```
CREATE  scripts/pt-br-inventory.py                                          # Group 1 — scope definition
CREATE  scripts/pt-br-protocol-ids.txt                                      # Group 1 — exact exemptions
CREATE  scripts/capture-prompt.py                                           # Group 1
CREATE  scripts/preset-parity.py                                             # Group 1 — normalized corpus proof
CREATE  scripts/run-core-readme-examples.py                                  # Group 6
CREATE  tests/fixtures/presets/inventory-baseline.txt
CREATE  tests/fixtures/presets/pt-br-preset.sha256
CREATE  tests/fixtures/presets/pt-br-containers.json
CREATE  tests/fixtures/presets/prompt-parity-before.txt
CREATE  tests/fixtures/presets/prompt-parity-session.json                    # Group 1
CREATE  packages/neurostack-core/neurostack_core/protocol.py                 # dependency-free IDs
CREATE  packages/neurostack-core/neurostack_core/presets/__init__.py
CREATE  packages/neurostack-core/neurostack_core/presets/models.py           # frozen schema
CREATE  packages/neurostack-core/neurostack_core/presets/loader.py           # use_preset, PresetError
CREATE  packages/neurostack-core/neurostack_core/presets/pt_br_clinic/srm.toml
CREATE  packages/neurostack-core/neurostack_core/presets/pt_br_clinic/flow.toml
CREATE  packages/neurostack-core/neurostack_core/presets/pt_br_clinic/policy.toml
CREATE  packages/neurostack-core/neurostack_core/presets/pt_br_clinic/prompt.toml
CREATE  packages/neurostack-core/neurostack_core/presets/en_generic/*.toml    # Group 6
CREATE  tests/presets/test_preset_loader.py                                  # Group 1
CREATE  tests/presets/test_protocol_compatibility.py                          # Group 2
CREATE  tests/presets/test_foreign_preset.py                                 # Group 6

MODIFY  packages/neurostack-core/neurostack_core/__init__.py                 # public preset API
MODIFY  packages/neurostack-core/neurostack_core/srm/intent_classifier.py    # 254 literals
MODIFY  packages/neurostack-core/neurostack_core/srm/safety_detector.py      # 26
MODIFY  packages/neurostack-core/neurostack_core/srm/engine.py               # 21
MODIFY  packages/neurostack-core/neurostack_core/srm/funnel_detector.py      # 21 content literals
MODIFY  packages/neurostack-core/neurostack_core/srm/rag_decider.py          # 20
MODIFY  packages/neurostack-core/neurostack_core/srm/handoff_decider.py      # 16
MODIFY  packages/neurostack-core/neurostack_core/srm/models.py               # protocol re-exports + content
MODIFY  packages/neurostack-core/neurostack_core/prompt/templates/normal.py  # 112
MODIFY  packages/neurostack-core/neurostack_core/prompt/templates/handoff.py # 23
MODIFY  packages/neurostack-core/neurostack_core/prompt/templates/safety.py  # 13
MODIFY  packages/neurostack-core/neurostack_core/prompt/renderer.py          # 4
MODIFY  packages/neurostack-core/neurostack_core/prompt/templates/base.py    # 3
MODIFY  packages/neurostack-core/neurostack_core/policy/global_rules.py      # 76
MODIFY  packages/neurostack-core/neurostack_core/policy/models.py            # 1
MODIFY  packages/neurostack-core/neurostack_core/flow/handlers/handoff.py    # 14
MODIFY  packages/neurostack-core/neurostack_core/flow/handlers/safety.py     # 9 (Review #4 C1)
MODIFY  packages/neurostack-core/neurostack_core/flow/handlers/normal.py     # 2
MODIFY  packages/neurostack-core/neurostack_core/flow/handlers/rag.py        # 2
MODIFY  packages/neurostack-core/pyproject.toml            # ship presets/**/*.toml in the wheel
MODIFY  tests/package_artifact/test_archives.py             # Group 1 exact wheel/sdist oracle
MODIFY  tests/scripts/test_application_core_imports.py      # Group 2 exact protocol-leaf edge
MODIFY  packages/neurostack-core/README.md                 # how to write a preset
MODIFY  scripts/wish-gates.sh                              # inventory, loader, parity gates
MODIFY  src/api/schemas/sessions.py                        # Group 2 — canonical FunnelStage import
```

_No file under `tests/srm`, `tests/flow`, `tests/policy` or `tests/prompt` appears in this list, and
every module group asserts that none changed. Those 1006 tests are the parity oracle; editing one to
accommodate a move would destroy the only evidence that the move was faithful._
