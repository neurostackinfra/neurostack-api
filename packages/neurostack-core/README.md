# neurostack-core

Framework-independent cognitive components shared by NeuroStack services.

This package contains NeuroStack's SRM, conversation-flow, policy, and prompt
components. It does not call an LLM, perform network I/O, or durably persist
application data. Importing `neurostack_core` also does not register Prometheus
collectors; applications opt in by calling `enable_metrics()` during startup.

## Status and installation

`neurostack-core` is currently unpublished and supported only as a source
install. It is not available from PyPI. Install it from a checkout:

```bash
git clone https://github.com/neurostackinfra-hub/neurostack-api.git
python -m pip install ./neurostack-api/packages/neurostack-core
```

Pin a commit when consuming the package from another project so the provisional
API cannot change underneath that project.

## Quickstart

<!-- readme-example: quickstart -->
```python
from uuid import uuid4

from neurostack_core.srm import SRMInput, get_srm_engine

decision = get_srm_engine().process(
    SRMInput(
        tenant_id=uuid4(),
        session_id=uuid4(),
        message="Olá, quero agendar botox para sexta às 10h",
    )
)

print(
    f"intent={decision.intent} "
    f"category={decision.intent_category.value} "
    f"stage={decision.funnel_stage.value}"
)
```
<!-- end-readme-example -->

The example runs locally without services or environment variables and prints:

```text
intent=agendar category=scheduling stage=em_qualificacao
```

## State and cleanup

The reasoning components do not durably persist data, but they are not all
stateless. `FlowManager` retains session state in the current Python process;
call and await `FlowManager.reset_session(session_id)` when the caller owns the
end of that session. `PolicyStore` retains policies in the current process;
call `PolicyStore.clear()` when the caller owns cleanup of the store. The
singletons returned by `get_flow_manager()` and `get_policy_store()` share that
process-local state.

Metric observations can also accumulate in memory before metrics are enabled.
Disabled metrics are unregistered and therefore unexported; disabling
registration does not discard observations.

## Public API

The supported provisional facades are `neurostack_core.srm`,
`neurostack_core.flow`, `neurostack_core.policy`, and
`neurostack_core.prompt`. Modules below those facades are implementation
details.

## Preset reference

The preset subsystem replaces the previous hard-coded domain content. Every
localized string, regex, and threshold lives in a bundled preset directory
and is loaded fail-closed on first access. The engine refuses to boot without
a valid preset.

### Public signatures

```text
neurostack_core.use_preset(name_or_path: str | os.PathLike[str]) -> Preset
neurostack_core.get_preset() -> Preset
neurostack_core.Preset           # frozen dataclass with name, source_id, srm, flow, policy, prompt
neurostack_core.PresetError      # raised for every rejection; subclass of RuntimeError
```

- `use_preset(name_or_path)` validates and atomically selects one source.
  A `str` is a bundled preset name matching `[a-z0-9_]+`; a `pathlib.Path`
  is an explicit trusted operator-supplied directory that is canonicalised
  with `realpath()`. Selection is configure-once; a second call with a
  different `source_id` raises `PresetError` and prompts a process restart.
- `get_preset()` returns the selected preset. If nothing has been selected
  yet it falls back atomically to `pt_br_clinic`.
- Both calls return the same `Preset` object; the `.srm`, `.flow`, `.policy`,
  and `.prompt` attributes are deeply immutable `FrozenMap` instances backed
  by `MappingProxyType` and recursively frozen tuples.

### The four TOML files

Every preset directory contains exactly these files:

```
<preset>/
  srm.toml      # intents, funnel, safety patterns, RAG, handoff, engine
  flow.toml     # handoff/safety responses, state machine, imperative literals
  policy.toml   # constraints, rule set, global policy, imperative literals
  prompt.toml   # system prompts, funnel instructions, imperative literals
```

A missing file, an unknown extra `*.toml`, or a file above the 1 MiB cap is
rejected before parsing with a redacted `PresetError`.

### Namespaced identifier contract

Preset `protocol` sections must exactly match the canonical namespaces
exported by `neurostack_core.protocol`:

- `srm.toml` → `protocol.intent_categories`, `protocol.funnel_stages`,
  `protocol.safety_categories`, `protocol.sentiment_labels`,
  `protocol.flow_modes`, `protocol.handoff_reasons`,
  `protocol.official_intents`, `protocol.actions`,
  `protocol.engine_regex_keys`.
- `flow.toml` → `protocol.handoff_response_keys`,
  `protocol.safety_response_keys`.
- `policy.toml` → `protocol.rule_ids`, `protocol.rule_names`,
  plus the canonical `(rule_id, rule)` pairs declared in
  `POLICY_RULE_PAIRS`.
- `prompt.toml` → `protocol.lookup_keys`.

Any missing canonical value, any extra value outside the canonical set, and
any mismatched `(rule_id, rule)` pair raise `PresetError`. Canonical values
are shown by name; private values that fail validation are redacted.

### Import order and process lifecycle

```python
from neurostack_core import use_preset

# Optional explicit selection — must happen before any content accessor
# imports the preset.  Omit the call to default to pt_br_clinic.
use_preset("en_generic")

# Only now import engines that read preset content.
from neurostack_core.srm import SRMInput, get_srm_engine
```

The import order matters because every engine module eagerly calls
`get_preset()` at import time to materialise its lookup tables. Importing
an engine before `use_preset()` therefore pins the default `pt_br_clinic`
preset for the lifetime of the process.

### Error recovery

- `PresetError` always names the file, dotted key, rejected summary,
  expected shape, and the next action to take. Prompt-file errors redact
  the rejected string value to avoid leaking private content.
- A failed `use_preset()` leaves `_selected` unset, so `get_preset()` falls
  back to `pt_br_clinic` on the next call. The caller may re-attempt
  selection with a different source after fixing the underlying file.
- A lifecycle conflict (`use_preset(a)` then `use_preset(b)` where
  `b.source_id != a.source_id`) is final: restart the process to swap.

### Trusted-path boundary

- `str` inputs are restricted to the `[a-z0-9_]+` bundled-name allowlist.
  Traversal, separators, mixed case, and empty strings are rejected before
  disk I/O so that `str` can never reach the filesystem.
- `pathlib.Path` inputs are canonicalised with `realpath()` and must point
  at an existing directory. The resolved directory becomes the
  `source_id`; symlink aliases are deduplicated to a single `source_id`.
- Only `str` or `os.PathLike[str]` are accepted. `bytes` paths, non-text
  `__fspath__` returns, and `None` are rejected.

### Bundled presets

- `pt_br_clinic` — the production Brazilian Portuguese (pt-BR)
  aesthetic-clinic preset. It ships the full regex set and prompt library
  every current consumer depends on and is the implicit default of
  `get_preset()`.
- `en_generic` — a compact English proof fixture that exists solely to
  show the engine is genuinely preset-driven. It is intentionally generic
  and small; do not depend on it for real workloads.

### Known limitation: pt-BR clinic domain

Until a true domain-neutral content set ships, `neurostack-core` continues
to ship Brazilian Portuguese (`pt-BR`) aesthetic-clinic intents, rules,
prompts, and response content as the default bundled preset. That domain
tie-in is tracked by the `neurostack-core-dedomainification` wish;
consumers must not assume other languages or domains are production-ready
until that work ships.

### Preset-driven example (English)

<!-- readme-example: en_generic -->
```python
from uuid import uuid4

from neurostack_core import use_preset
use_preset("en_generic")

from neurostack_core.srm import SRMInput, get_srm_engine

decision = get_srm_engine().process(
    SRMInput(
        tenant_id=uuid4(),
        session_id=uuid4(),
        message="I would like to book an appointment for monday at 10am",
        language="en",
    )
)

print(
    f"intent={decision.intent} "
    f"category={decision.intent_category.value} "
    f"mode={decision.mode.value}"
)
```
<!-- end-readme-example -->

That example runs against the installed wheel and prints:

```text
intent=agendar category=scheduling mode=normal
```

### Lifecycle lock

<!-- readme-example: lifecycle_lock -->
```python
from neurostack_core import PresetError, use_preset

use_preset("en_generic")
try:
    use_preset("pt_br_clinic")
except PresetError as error:
    print("locked:", "restart" in str(error))
else:
    raise AssertionError("preset swap unexpectedly succeeded")
```
<!-- end-readme-example -->

That example prints `locked: True` — the only way to swap presets is a
process restart.
