#!/usr/bin/env python3
"""Run the dependency-free external-consumer smoke for neurostack-core."""

from uuid import uuid4

import neurostack_core.flow as flow
import neurostack_core.policy as policy
import neurostack_core.prompt as prompt
import neurostack_core.srm as srm
from neurostack_core.srm import SRMInput, get_srm_engine

FACADES = (flow, policy, prompt, srm)


def main() -> None:
    """Classify the fixed pt-BR scheduling message and print its contract."""
    if not all(module.__all__ for module in FACADES):
        raise RuntimeError("a neurostack-core facade has no public exports")
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


if __name__ == "__main__":
    main()
