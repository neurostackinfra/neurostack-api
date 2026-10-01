# Wish: Permit packaged-resource loading without weakening the `neurostack-core` boundary

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `neurostack-core-resource-boundary` |
| **Date** | 2026-09-27 |
| **Author** | Lorenzo |
| **Appetite** | small |
| **Branch** | `wish/neurostack-core-resource-boundary` |
| **Repos touched** | neurostack-api |
| **Design** | _No brainstorm — direct wish_ |

## Summary

The package-boundary checker currently rejects every source-visible `importlib` spelling, so the
approved preset loader cannot use Python's standard packaged-resource API and Group 1 of
`neurostack-core-dedomainification` cannot pass `static_gate`. Add a trusted-source lexical
architectural exception for exactly one direct module-level, unaliased
`from importlib.resources import files` and at least one direct literal
`files("neurostack_core.presets")` call, while preserving every existing prohibition on direct
`src` coupling, dynamic-import API syntax and ambiguous `importlib` access.

This syntax gate is not a Python sandbox: it constrains direct AST import, identifier and call
forms, but does not claim to detect runtime-injected callables, reflective namespace access,
synthesized strings or names, or equivalent metaprogramming.

A direct wish is appropriate because the blocker, concrete preset-loader consumer, exact accepted
grammar, affected checker/helper callers, and focused plus aggregate validation surface are already
measured, leaving no unresolved product or technical decision for a brainstorm.

## Scope

### IN

- Add one syntax-bounded checker policy requiring exactly one absolute `ImportFrom` directly in
  `ast.Module.body`, whose module is exactly `importlib.resources`, whose sole imported symbol is
  exactly the unaliased name `files`, and whose file-wide direct `files` identifier occurrences are
  limited to at least one exact call with the single positional string constant
  `"neurostack_core.presets"` and no keywords.
- Keep the shared reserved-spelling classifier strict by default; only the package-boundary checker
  opts into the packaged-resource exception through a keyword-only flag.
- Add positive, near-miss and regression tests proving the lexical exception rejects direct
  `importlib`, `import_module`, `__import__`, `builtins`, `src`, alternate resource anchors,
  callable-attribute syntax and other disallowed direct `files` identifier forms.
- Keep `reserved_spellings` and the branch API extractor unchanged, and prove their existing strict
  behavior with the extractor's focused suite.

### OUT

- Changing the preset loader or any `neurostack_core` runtime module; the blocked dedomainification
  group consumes this policy after the dependency merges.
- Allowing `import importlib.resources`, `from importlib import resources`, star imports, multiple
  symbols, other `importlib.resources` functions, or any spelling of the dynamic import APIs.
- Allowing an alias for `files`; rebinding, passing, returning or storing `files`; calling it with a
  variable or any other literal anchor (including `"src"`); extra arguments or keywords; or any
  other non-call use of the identifier.
- Replacing the bounded structural AST walk with provenance analysis, control-flow interpretation,
  filename/path allowlists or runtime import hooks.
- Treating the checker as a sandbox or attempting to reject runtime-injected callables, reflective
  access such as `globals()["files"]` or `locals()[name]`, synthesized strings/names, or equivalent
  Python metaprogramming. Those are accepted residual risks of the trusted-source lexical gate,
  inherited from the extraction WISH.
- Adding a dependency or using the `importlib_resources` backport to evade the repository policy.
- Editing or validating the downstream preset loader in this dependency wish; dedomainification
  consumes the merged policy later from its own worktree.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | The only allowed import form is exactly one unaliased `from importlib.resources import files` node directly in `ast.Module.body`, and the file must contain at least one call in which every direct syntactic `files` identifier use is `files("neurostack_core.presets")` with one positional string constant and no keywords. | `files` resolves a string anchor through dynamic module import, so constraining only the import statement would still admit direct `files("src")` and `files(variable)` calls. Requiring one top-level binding and an actually used, narrow direct-call grammar covers the measured bundled-preset operation. |
| 2 | `find_forbidden_imports` gains an explicit, default-false, keyword-only policy argument; `check-package-boundary.py` is its only opt-in caller. `reserved_spellings` and the facade extractor remain unchanged. | The package checker is the only caller with the concrete resource-loading requirement. The facade extractor calls `reserved_spellings` directly rather than `find_forbidden_imports`, so its focused suite proves the unchanged classifier stays strict. |
| 3 | The exception is defined by AST shape, never by filename or package path. | A path allowlist would make one loader privileged and let future edits hide behind its filename. The safe syntax is reusable and reviewable wherever it appears under the package boundary. |
| 4 | The finite rule rejects every other direct `files` identifier occurrence in a file that uses the exception, without attempting name provenance or execution semantics. | The checker can collect the exact `ast.Name` node used as each approved call target and reject all other syntax-level bindings/usages named `files`, including store/delete contexts and identifier-bearing definition, argument, exception, scope-declaration, pattern and, when available, Python 3.12 type-parameter fields. This deliberately conservative file-local rule rejects shadowed or unrelated direct `files` spellings rather than interpreting scope, control flow or runtime values. |
| 5 | The threat model is trusted source checked lexically, not adversarial Python sandboxing. | Runtime injection, reflective `globals`/`locals` lookup, synthesized names/strings and equivalent metaprogramming can resolve behavior without a direct `ast.Name("files")`; they remain explicit accepted residual risk inherited from the extraction WISH rather than motivating semantic analysis. |
| 6 | Execution is isolated from the blocked downstream worktree. | This dependency starts from the merged extraction baseline in its own branch/worktree. It does not execute inside the dirty dedomainification worktree, and the downstream loader remains out of scope until this policy merges. |

## Simplicity Case

- **Simplest complete design:** one predicate recognizes exactly one absolute, unaliased
  `ImportFrom` in `ast.Module.body`, one predicate recognizes one or more direct
  `files("neurostack_core.presets")` calls, and a finite file-local AST scan permits only the import
  alias plus the `ast.Name` call targets from those exact calls; a default-false, keyword-only
  argument opts the package checker into suppressing only the approved syntax.
- **Added machinery:** the policy argument is required because the lexical helper is shared with a
  stricter facade extractor; positive and negative fixtures are required because this code is an
  executable repository gate whose false negatives weaken an architectural boundary.
- **Deferred until measured:** any additional `importlib.resources` API, anchor or broader
  safe-import grammar requires a concrete consumer and a new policy review; semantic provenance,
  scope resolution and control-flow analysis remain deferred unless exact syntax can no longer
  express a required safe import. Reflective lookup, runtime injection and synthesized names remain
  accepted residual risk under the trusted-source model, not promised coverage.
- **Complexity removed:** no filename exceptions, configuration files, dependency additions,
  runtime hooks, alias/provenance tracking, data-flow lattice or general Python evaluator.

## Dependencies

**depends-on:** neurostack-core-extraction
**blocks:** neurostack-core-dedomainification

## Success Criteria

- [ ] The boundary checker accepts an otherwise-clean file containing the unaliased import
      `from importlib.resources import files` exactly once directly in `ast.Module.body` and one or
      more exact
      `files("neurostack_core.presets")` calls with one positional literal and no keywords.
- [ ] The checker still rejects `import importlib.resources`, `from importlib import resources`,
      direct `importlib.resources.files` access, nested/conditional imports, multiple exact imports,
      star/multi-symbol imports, every other imported resource function, every alias of `files`, and
      an unused exact import with no approved call.
- [ ] The checker rejects `files("src")`, every other literal anchor, variable or computed anchors,
      runtime concatenation and f-string anchors, zero/extra positional arguments, keywords,
      `*args`, `**kwargs`, `files.__call__`, direct attribute/non-call forms, rebinding, passing,
      returning, storing, and every other direct identifier occurrence of `files` in a file opting
      into the exception. Adjacent string literals are accepted because Python compiles them to the
      same approved `ast.Constant` value.
- [ ] All existing static `src` import and reserved-spelling fixtures remain rejected.
- [ ] A direct helper-level test proves `find_forbidden_imports(tree)` with no policy keyword reports
      the exact resource import, while explicit opt-in accepts only the complete grammar.
- [ ] The documented contract remains explicitly lexical and trusted-source: reflective
      `globals`/`locals` lookup, synthesized strings/names, runtime-injected callables and equivalent
      metaprogramming remain accepted out-of-scope residual risk rather than claimed rejection.
- [ ] `reserved_spellings` and the branch public-API extractor remain unchanged, and the extractor's
      complete focused suite proves its direct use of the classifier remains strict.
- [ ] `static_gate` and `full_suite_gate` pass, preserving the repository's aggregate validation.

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 1 — prior boundary-analyzer rework (+1), otherwise deterministic and single-package | `engineer-trivial` / low | Add the exact resource-files exception and prove the shared gate remains fail-closed. |

No groups run in parallel. This independently shippable dependency group executes from merged
`neurostack-core-extraction` in its own branch/worktree, never inside the dirty dedomainification
worktree. It changes a shared executable gate and therefore finishes with both focused suites and
the repository aggregate gates; the downstream loader remains out of scope.

## Execution Groups

### Group 1: Exact `importlib.resources.files` boundary exception

**Goal:** Permit the narrow direct packaged-resource syntax required by the preset loader while
keeping the trusted-source lexical architectural boundary fail-closed for all other direct syntax.

**Deliverables:**
1. Small exact-import-count/placement and exact-call predicates plus a conservative finite direct
   identifier-use check and default-strict, keyword-only opt-in policy in
   `scripts/_forbidden_imports.py`.
2. `scripts/check-package-boundary.py` opts into that policy; no other caller does.
3. Focused tests in `tests/scripts/test_check_package_boundary.py` cover the accepted grammar,
   helper default, and complete direct import, anchor, argument, rebinding, attribute and non-call
   near-miss matrix, plus documented reflective residual-risk cases.
4. The unchanged `tests/scripts/test_extract_branch_public_api.py` focused suite proves the facade
   extractor remains strict through its direct call to unchanged `reserved_spellings`.

**Acceptance Criteria:**
- [ ] The accepted import is exactly one node in `ast.Module.body`; it has `level == 0`, module
      `importlib.resources`, exactly one alias named `files`, and `asname is None`. Nested or
      conditional imports, multiple exact imports, and an exact import with no approved call fail.
- [ ] Every accepted use is an `ast.Call` whose `func` is the direct `ast.Name("files")`, whose
      `args` contain exactly one `ast.Constant` string equal to `"neurostack_core.presets"`, and
      whose `keywords` are empty; at least one such call is required. Adjacent string literals such
      as `"neurostack_core." "presets"` are accepted as that same constant, while runtime
      concatenation and f-strings fail.
- [ ] When the exception import is present, a finite AST scan rejects every other direct identifier
      binding or use spelled `files`. It compares node shape/identity and identifier-bearing AST
      fields only, including Python 3.12 type-parameter nodes when available; it does not infer
      provenance, scopes, reachability, aliases, runtime values or control flow.
- [ ] The exception suppresses only the `importlib` occurrence created by that exact `ImportFrom`
      and the approved `files` call targets; reserved spellings and `src` syntax elsewhere in the
      same file are still reported, including fixtures mixing otherwise-valid resource syntax with
      forbidden `src` or reserved forms.
- [ ] The focused falsifier matrix covers nested/conditional imports, multiple exact imports, an
      unused exact import, valid syntax mixed with forbidden `src`/reserved forms, `files.__call__`,
      `*args`, `**kwargs`, direct attribute/non-call forms, rebinding forms, and identifier-bearing
      Python 3.12 type-parameter nodes when available; every disallowed case exits 1 and reports
      reserved import syntax.
- [ ] Reflective `globals`/`locals` lookup, synthesized-name/string cases, runtime injection and
      equivalent metaprogramming are documented in tests or test comments as accepted out-of-scope
      residual risks; no test claims that the lexical rule rejects them.
- [ ] Direct helper tests prove `find_forbidden_imports(tree)` without the keyword reports the exact
      resource import and explicit opt-in accepts only when the entire import/use grammar is valid.
- [ ] Existing boundary fixtures and all branch public-API extractor fixtures pass; neither
      `reserved_spellings` nor facade extractor code/tests are modified.
- [ ] The implementation remains a finite structural AST rule; it adds no general provenance,
      data-flow, scope-resolution or control-flow interpreter.
- [ ] Focused tests, `static_gate` and `full_suite_gate` pass.

**Validation:**
```bash
set -euo pipefail
poetry run pytest \
  tests/scripts/test_check_package_boundary.py \
  tests/scripts/test_extract_branch_public_api.py \
  -q -p no:cacheprovider
source scripts/wish-gates.sh
static_gate
full_suite_gate
```

_Scope rationale: the focused suites prove the exact AST policy and the unaffected shared caller;
the checker is part of repository lint and architectural enforcement, so the documented static and
full-suite gates are required aggregate evidence._

**depends-on:** none

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] A temporary package file using `from importlib.resources import files` followed by
      `files("neurostack_core.presets")` passes `scripts/check-package-boundary.py`.
- [ ] A temporary package file using `import importlib.resources` or `importlib.import_module`
      fails the same checker with an actionable reserved-syntax report.
- [ ] Temporary package files using nested/multiple imports, no approved call, `files("src")`, a
      variable anchor, runtime concatenation/f-strings, an alias, `files.__call__`, starred
      arguments, or a direct non-call `files` use fail with an actionable reserved-syntax report.
- [ ] Temporary package files mixing the accepted grammar with forbidden `src` or reserved syntax
      still fail; reflective/synthesized-name examples are recorded as out-of-scope residual risk.
- [ ] The repository static and full-suite gates remain green on the merged tree.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| The shared classifier is accidentally weakened while adding the package-checker exception | High | Keep `reserved_spellings` and facade code/tests unchanged; run the facade extractor's complete focused suite as proof. |
| A direct syntactic near miss broadens dynamic module resolution or direct callable use | High | Match the complete import count, placement and call shapes, conservatively reject every other direct file-local `files` binding/use, and test aliases, all other anchors, argument variations, rebinding, attributes and non-call cases. |
| Reviewers mistake a lexical architectural gate for a sandbox | High | State the trusted-source threat model in Summary, Scope, Decisions, Success, Acceptance, QA and Risks; record reflective lookup, synthesized names and runtime injection as accepted inherited residual risk. |
| A path-specific exception hides future unsafe code | Medium | Make the exception syntax-based and package-wide; no filenames or directories enter the policy. |
| The blocked loader continues using `import importlib.resources` after this merges | Medium | This wish deliberately does not edit the consumer; the dedomainification Group 1 acceptance gate remains red until it adopts the approved exact form. |
| Dirty downstream changes contaminate the dependency implementation or its evidence | High | Execute this wish from merged extraction in a separate branch/worktree; do not use the dirty dedomainification worktree. |

---

## Review Results

### 2026-09-27 — Initial plan review — **FIX-FIRST**

- **Reviewed SHA-256:** `a81213ffb7911b365751f414bff28e22628ffaafc2ed47b2a8fd33aa459219d5`
- **HIGH:** allowing the import alone also allowed arbitrary `files(anchor)` calls; CPython resolves a
  string anchor through dynamic module import. The draft therefore admitted `files("src")` and
  variable anchors despite claiming otherwise.
- **Route:** fix loop 1 constrained both the import and the direct call grammar, added branch
  isolation and corrected dependency/index metadata.

### 2026-09-27 — Plan re-review (fix loop 1/2) — **FIX-FIRST**

- **Reviewed SHA-256:** `95bd737dffed807aa649f19fc5567211a13642420223372f8f33c3e1f0395818`
- **HIGH:** the direct-identifier rule still overclaimed reflection-proof confinement; synthesized
  namespace access such as `globals()["files"]` is outside a finite lexical scan.
- **Route:** fix loop 2 narrowed the threat model to trusted-source lexical enforcement, made
  reflection/metaprogramming an explicit inherited residual risk, required exactly one top-level
  import and at least one approved call, and expanded the falsifier matrix.

### 2026-09-27T18:33:10-03:00 — Final plan re-review (fix loop 2/2) — **SHIP**

- **Reviewed SHA-256:** `daed382488f2d13c28fb2ffe9f3852868652c2cac49d8e16c4152fc5ebe016e7`
- **Independent checklist verdict:** SHIP; no CRITICAL, HIGH, MEDIUM or LOW gaps. The exact import,
  literal anchor, strict-default helper, trusted-source residual-risk boundary, three-file scope,
  isolated execution and dependency DAG are internally consistent.
- **Questioner advisory:** no blocking gap. During execution, enumerate every promised
  identifier-bearing AST field rather than relying on an umbrella "rebinding" fixture.
- **Architecture advisory:** no CRITICAL/HIGH gap. Execution evidence must distinguish diagnostics:
  resource-grammar near misses report reserved syntax, while a valid resource form mixed with
  forbidden `src` reports static-src syntax. The pre-existing Makefile lint-wiring drift is outside
  this wish; `static_gate` directly invokes the checker and remains the aggregate contract.
- **Plan checks:** reviewed digest matched; complete WISH read; caller graph and three-file scope
  verified; extraction merge and both dependency edges verified; validation Bash and
  `scripts/wish-gates.sh` pass `bash -n`; Python 3.12 type-parameter AST nodes are available.
  Planned pytest/static/full gates were not run because implementation does not yet exist and the
  review was read-only. This repository does not declare the optional `wishes:lint` script.
- **Verdict:** SHIP — zero CRITICAL/HIGH gaps; dependency plan approved for execution.

### 2026-09-28 — Group 1 execution review — **BLOCKED**

- **Task:** `t_mukb9iao46df4e4f`, claimed by Turing and left `in_progress`; implementation remains
  uncommitted in the isolated `wish/neurostack-core-resource-boundary` worktree.
- **Scope:** exactly the approved three files. Final diff SHA-256:
  `ce9cf36b5f8429ffd843e6cb00acc44ee6d25c75f0f0554ce39b79fd5c4ff244`.
- **Fix loop 1/2:** repaired Python 3.11 compatibility, isort failure, dead suppression code and the
  first missing falsifiers. Focused tests reached 103 passed; static and full gates passed.
- **Fix loop 2/2:** added activated-exemption import cases and a valid-plus-invalid direct-use
  matrix. Final engineer evidence: 121 focused tests passed on Python 3.12.3; Python 3.11.15 omitted
  only unsupported type-parameter cases; `static_gate` passed; `full_suite_gate` accepted 3056
  passed, 16 skipped and the two exact known failures.
- **Ordinary execution reviewer:** SHIP. Current behavior, scope and supplied gate evidence satisfy
  the approved contract.
- **QA and final-gate HIGH:** three load-bearing predicates remain unowned by regression tests. The
  final gate reproduced each as REJECT on current source, ACCEPT after one mutation, while all 121
  focused tests remained green: removing the exact call-target `files` check; loosening exact module
  equality to a prefix; and removing `ast.Attribute.attr` identifier handling.
- **Final-gate verdict:** BLOCKED. Production behavior is currently correct, but the approved
  fail-closed architectural boundary is not adequately regression-protected.

**Fix loop exhausted (2/2). Group remains in progress.**

- **Remaining gaps:** add focused rejection fixtures for
  `other("neurostack_core.presets")`,
  `from importlib.resources.extra import files` plus the exact call, and `client.files` plus the
  otherwise-valid grammar. Each must fail under its corresponding single mutation.
- **Cause:** execution/test-spec insufficiency; not ambiguous specification, missing context,
  environment failure, overdesign or evidenced model-capacity. Model/effort escalation is not
  authorized.
- **Corrective route:** human authorization for one additional **test-only** remediation cycle,
  followed by Python 3.11/3.12 probes, focused tests, `static_gate`, `full_suite_gate` and a fresh
  final review. No production-source correction is currently indicated.
- **Budget:** fix attempts 2/2; effort escalations 0/2.
- **Appeal:** ordinary reviewer SHIP versus QA/final-gate BLOCKED; final gate independently
  reproduced QA's evidence. Human resolution is required before advancement.
- **Human resolution (2026-09-28):** Lorenzo explicitly authorized one additional test-only
  remediation cycle beyond the normal 2/2 limit, limited to the three final-gate counterexamples
  and their required validation/re-review. This is a one-cycle exception, not a reset of the fix
  budget or authority for production redesign.
- **Orchestrator validation:** not rerun after the blocking final review; the task was not marked
  done and no quality/next-stage handoff was claimed.

### 2026-09-28 — Authorized test-only remediation and final execution review — **SHIP**

- **Reviewed diff SHA-256:**
  `2349325dbfd2554fca27f9939fed6c1c85eb12ad33dc1347caf5c3ba75b26a20`, based on
  `f90c69f`; scope remains exactly the three approved files.
- **Authorized remediation:** production code was unchanged. The test file gained exactly three
  isolated rejection fixtures for the previously surviving mutations: an approved anchor called
  through `other`, `files` imported from `importlib.resources.extra`, and standalone
  `client.files` alongside otherwise-valid resource grammar.
- **Mutation evidence:** each fixture passes against current source and fails when its one target
  predicate is removed or loosened: exact call-target identity, exact import-module equality, and
  `ast.Attribute.attr` identifier handling.
- **Compatibility:** Python 3.11.15 loaded the module with the unsupported type-parameter cases
  omitted; Python 3.12.3 retained all three type-parameter cases.
- **Orchestrator validation:** `git diff --check` passed; the focused boundary/facade pair passed
  124 tests in 117.22 seconds; `static_gate` passed; `full_suite_gate` exited 0 with 3059 passed,
  16 skipped, exactly the two documented baseline failures in `tests/core/test_config_service.py`,
  and `pytest completion valid (full)`.
- **Independent reviews:** QA, quality, and the adversarial final gate all returned SHIP with no
  severity-tagged gaps. Security/trust-boundary behavior, maintainability, linear AST-scan cost,
  Python 3.11 compatibility, and exact three-file scope were clean.
- **Appeal resolution:** the earlier ordinary-SHIP versus QA/final-gate-BLOCKED conflict is resolved
  by the human-authorized test-only remediation and convergent SHIP evidence. No further human
  resolution is required for this execution gate.
- **Residual risk:** unchanged and accepted. This is a trusted-source lexical gate, not a sandbox;
  reflection, synthesized identifiers, runtime-injected callables, and metaprogramming remain out
  of scope.
- **Lifecycle decision:** Group 1 satisfies its acceptance criteria and task
  `t_mukb9iao46df4e4f` was marked `done` at 2026-09-28 20:25. The WISH remains `IN_PROGRESS` for
  the separate review-stage handoff.

---

## Files to Create/Modify

```
MODIFY  scripts/_forbidden_imports.py
MODIFY  scripts/check-package-boundary.py
MODIFY  tests/scripts/test_check_package_boundary.py
```
