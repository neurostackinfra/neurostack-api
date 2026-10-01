"""Adversarial isolation tests for preset-backed global policy values."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from typing import TypeVar

from neurostack_core.policy import global_rules
from neurostack_core.policy.global_rules import (
    get_clinical_safety_rules,
    get_global_constraints,
    get_global_policy,
    get_global_rules,
    get_legal_safety_rules,
)

_T = TypeVar("_T")


def _assert_fresh_equal(factory: Callable[[], _T]) -> None:
    first = factory()
    second = factory()

    assert first == second
    assert first is not second


def test_global_policy_getters_return_equal_fresh_snapshots() -> None:
    for factory in (
        get_global_policy,
        get_global_rules,
        get_global_constraints,
        get_clinical_safety_rules,
        get_legal_safety_rules,
    ):
        _assert_fresh_equal(factory)

    first_policy = get_global_policy()
    second_policy = get_global_policy()
    assert first_policy.rules[0] is not second_policy.rules[0]
    assert first_policy.rules[0].patterns is not second_policy.rules[0].patterns
    assert first_policy.rules[0].metadata is not second_policy.rules[0].metadata

    rule_constants = [
        global_rules.RULE_NO_DIAGNOSIS,
        global_rules.RULE_NO_MEDICATION,
        global_rules.RULE_NO_TREATMENT_GUARANTEE,
        global_rules.RULE_NO_LEGAL_ADVICE,
        global_rules.RULE_NO_FALSE_PROMISES,
        global_rules.RULE_PRIVACY_COMPLIANCE,
        global_rules.RULE_PROFESSIONAL_TONE,
        global_rules.RULE_EMERGENCY_REDIRECT,
    ]
    assert rule_constants == get_global_rules()
    assert global_rules.GLOBAL_SAFETY_POLICY == get_global_policy()
    assert global_rules.GLOBAL_CONSTRAINTS == get_global_constraints()
    assert all(
        constant is not snapshot
        for constant, snapshot in zip(rule_constants, get_global_rules())
    )


def test_public_mutation_cannot_poison_merger_or_engine() -> None:
    script = r"""
import asyncio
from uuid import uuid4

from neurostack_core.policy import global_rules as global_rules
from neurostack_core.policy.engine import PolicyEngine
from neurostack_core.policy.policy_merger import PolicyMerger


class EmptyStore:
    async def get_active_rules(self, tenant_id):
        return []

    async def list_policies(self, tenant_id, active_only=False):
        return []


def poison_rule(rule):
    rule.rule_id = "poisoned"
    rule.rule = "poisoned"
    rule.message = "poisoned"
    rule.enabled = False
    rule.patterns.clear()
    rule.keywords.append("poisoned")
    rule.metadata["nested"] = {"items": ["poisoned"]}


async def main():
    baseline_policy = global_rules.get_global_policy()
    baseline_rules = global_rules.get_global_rules()
    baseline_constraints = global_rules.get_global_constraints()
    baseline_clinical = global_rules.get_clinical_safety_rules()
    baseline_legal = global_rules.get_legal_safety_rules()

    rule_constants = (
        global_rules.RULE_NO_DIAGNOSIS,
        global_rules.RULE_NO_MEDICATION,
        global_rules.RULE_NO_TREATMENT_GUARANTEE,
        global_rules.RULE_NO_LEGAL_ADVICE,
        global_rules.RULE_NO_FALSE_PROMISES,
        global_rules.RULE_PRIVACY_COMPLIANCE,
        global_rules.RULE_PROFESSIONAL_TONE,
        global_rules.RULE_EMERGENCY_REDIRECT,
    )
    for rule in rule_constants:
        poison_rule(rule)

    global_rules.GLOBAL_SAFETY_POLICY.name = "poisoned"
    poison_rule(global_rules.GLOBAL_SAFETY_POLICY.rules[0])
    global_rules.GLOBAL_SAFETY_POLICY.rules.clear()
    global_rules.GLOBAL_CONSTRAINTS.must_not_say.clear()
    global_rules.GLOBAL_CONSTRAINTS.safety_reminders.append("poisoned")

    exposed_policy = global_rules.get_global_policy()
    exposed_policy.name = "poisoned"
    poison_rule(exposed_policy.rules[0])
    exposed_policy.rules.clear()

    exposed_rules = global_rules.get_global_rules()
    poison_rule(exposed_rules[0])
    exposed_rules.clear()

    exposed_constraints = global_rules.get_global_constraints()
    exposed_constraints.must_not_say.clear()
    exposed_constraints.custom_instructions.append("poisoned")

    exposed_clinical = global_rules.get_clinical_safety_rules()
    poison_rule(exposed_clinical[0])
    exposed_clinical.clear()

    exposed_legal = global_rules.get_legal_safety_rules()
    poison_rule(exposed_legal[0])
    exposed_legal.clear()

    assert global_rules.get_global_policy() == baseline_policy
    assert global_rules.get_global_rules() == baseline_rules
    assert global_rules.get_global_constraints() == baseline_constraints
    assert global_rules.get_clinical_safety_rules() == baseline_clinical
    assert global_rules.get_legal_safety_rules() == baseline_legal

    store = EmptyStore()
    merger = PolicyMerger(policy_store=store)

    merged_rules = await merger.get_merged_rules(uuid4())
    poison_rule(merged_rules[0])
    merged_rules.clear()
    assert await merger.get_merged_rules(uuid4()) == baseline_rules

    merged_policies = await merger.get_merged_policies(uuid4())
    merged_policies[0].name = "poisoned"
    poison_rule(merged_policies[0].rules[0])
    merged_policies.clear()
    assert await merger.get_merged_policies(uuid4()) == [baseline_policy]

    merged_constraints = await merger.get_merged_constraints(uuid4())
    merged_constraints.must_not_say.clear()
    merged_constraints.tone_guidelines.append("poisoned")
    assert await merger.get_merged_constraints(uuid4()) == baseline_constraints

    engine = PolicyEngine(policy_store=store, policy_merger=merger)
    exposed_safety_rules = await engine.get_safety_rules(uuid4())
    for rule in exposed_safety_rules:
        poison_rule(rule)

    first = await engine.evaluate_pre_srm(
        tenant_id=uuid4(),
        session_id=uuid4(),
        message="qual meu diagnóstico",
    )
    first.constraints.clear()
    second = await engine.evaluate_pre_srm(
        tenant_id=uuid4(),
        session_id=uuid4(),
        message="qual meu diagnóstico",
    )
    assert first.should_block
    assert second.should_block
    assert second.violations[0].rule_id == "C_MEDICAL_NO_DIAGNOSIS"
    assert second.constraints


asyncio.run(main())
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
