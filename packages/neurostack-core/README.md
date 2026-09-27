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

## Known limitation: pt-BR clinic domain

The package currently ships Brazilian Portuguese (`pt-BR`) aesthetic-clinic
intents, rules, prompts, and response content as built-in data. It is not yet a
domain-neutral reasoning library. That extraction is tracked separately by the
`neurostack-core-dedomainification` wish; consumers must not assume other
languages or domains are supported until that work ships.
