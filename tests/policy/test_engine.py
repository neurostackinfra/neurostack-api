"""
NeuroStack - Policy Engine Unit Tests
Tests for the Policy & Guardrails Engine.
"""

from uuid import uuid4

import pytest

from src.policy.engine import PolicyEngine
from src.policy.global_rules import (
    get_clinical_safety_rules,
    get_global_constraints,
    get_global_policy,
    get_global_rules,
    get_legal_safety_rules,
)
from src.policy.models import (
    PolicyConstraints,
    PolicyCreate,
    PolicyDefinition,
    PolicyEvaluationResult,
    PolicyRuleCreate,
    PolicyRuleDefinition,
    PolicyType,
    RuleAction,
    RuleCategory,
    RuleSeverity,
    RuleViolation,
)
from src.policy.policy_merger import PolicyMerger
from src.policy.policy_store import PolicyStore, PolicyStoreError

# ============================================
# Fixtures
# ============================================


@pytest.fixture
def sample_tenant_id():
    return uuid4()


@pytest.fixture
def sample_session_id():
    return uuid4()


@pytest.fixture
def policy_store():
    store = PolicyStore()
    yield store
    store.clear()


@pytest.fixture
def policy_merger(policy_store):
    return PolicyMerger(policy_store=policy_store)


@pytest.fixture
def policy_engine(policy_store, policy_merger):
    return PolicyEngine(policy_store=policy_store, policy_merger=policy_merger)


@pytest.fixture
def sample_rule():
    return PolicyRuleDefinition(
        rule_id="test_rule",
        rule="test_no_pricing",
        message="Não discutir preços",
        description="Evitar discussões sobre preços",
        category=RuleCategory.CONTENT,
        severity=RuleSeverity.WARNING,
        action=RuleAction.WARN,
        patterns=["preço", "valor", "quanto custa"],
        keywords=["preço", "custo"],
    )


@pytest.fixture
def sample_policy(sample_tenant_id, sample_rule):
    return PolicyDefinition(
        tenant_id=sample_tenant_id,
        name="Test Policy",
        description="Policy for testing",
        policy_type=PolicyType.TENANT,
        rules=[sample_rule],
        priority=100,
        is_active=True,
    )


# ============================================
# Global Rules Tests
# ============================================


class TestGlobalRules:
    """Tests for global rules and policies."""

    def test_global_policy_exists(self):
        """Test that global policy is defined."""
        policy = get_global_policy()
        assert policy is not None
        assert policy.name == "NeuroStack Global Safety Policy"
        assert policy.policy_type == PolicyType.GLOBAL
        assert policy.priority == 1000

    def test_global_rules_count(self):
        """Test that global rules are defined."""
        rules = get_global_rules()
        assert len(rules) >= 5  # At least 5 global rules

    def test_clinical_safety_rules(self):
        """Test clinical safety rules."""
        rules = get_clinical_safety_rules()
        assert len(rules) >= 2

        # All should be clinical category
        for rule in rules:
            assert rule.category == RuleCategory.SAFETY_CLINICAL

    def test_legal_safety_rules(self):
        """Test legal safety rules."""
        rules = get_legal_safety_rules()
        assert len(rules) >= 1

        for rule in rules:
            assert rule.category == RuleCategory.SAFETY_LEGAL

    def test_global_constraints(self):
        """Test global constraints."""
        constraints = get_global_constraints()
        assert constraints is not None
        assert len(constraints.must_not_say) > 0
        assert len(constraints.safety_reminders) > 0

    def test_no_diagnosis_rule_matches(self):
        """Test that no-diagnosis rule matches correctly."""
        rules = get_global_rules()
        no_diagnosis = next(
            (r for r in rules if r.rule_id == "C_MEDICAL_NO_DIAGNOSIS"),
            None,
        )

        assert no_diagnosis is not None
        assert no_diagnosis.matches("Qual é meu diagnóstico?")
        assert no_diagnosis.matches("Me diga o que eu tenho")
        assert not no_diagnosis.matches("Olá, bom dia")

    def test_no_medication_rule_matches(self):
        """Test that no-medication rule matches correctly."""
        rules = get_global_rules()
        no_medication = next(
            (r for r in rules if r.rule_id == "C_MEDICAL_NO_MED_CHANGE_POSTCARE"),
            None,
        )

        assert no_medication is not None
        assert no_medication.matches("Que remédio devo tomar?")
        assert no_medication.matches("Me receita um medicamento")
        assert not no_medication.matches("Quero agendar uma consulta")


# ============================================
# PolicyRuleDefinition Tests
# ============================================


class TestPolicyRuleDefinition:
    """Tests for PolicyRuleDefinition model."""

    def test_rule_creation(self, sample_rule):
        """Test rule creation."""
        assert sample_rule.rule_id == "test_rule"
        assert sample_rule.enabled is True
        assert len(sample_rule.patterns) > 0

    def test_rule_matches_pattern(self, sample_rule):
        """Test pattern matching."""
        assert sample_rule.matches("Qual o preço da consulta?")
        assert sample_rule.matches("Quanto custa?")
        assert not sample_rule.matches("Bom dia")

    def test_rule_matches_keyword(self, sample_rule):
        """Test keyword matching."""
        assert sample_rule.matches("O custo é alto")
        assert sample_rule.matches("Preço muito caro")

    def test_disabled_rule_no_match(self, sample_rule):
        """Test that disabled rules don't match."""
        sample_rule.enabled = False
        assert not sample_rule.matches("Qual o preço?")


# ============================================
# PolicyStore Tests
# ============================================


class TestPolicyStore:
    """Tests for PolicyStore."""

    @pytest.mark.asyncio
    async def test_create_policy(self, policy_store, sample_tenant_id):
        """Test policy creation."""
        policy_data = PolicyCreate(
            name="Test Policy",
            description="A test policy",
            policy_type=PolicyType.TENANT,
            rules=[
                PolicyRuleCreate(
                    rule="test_rule",
                    message="Test message",
                    category=RuleCategory.CUSTOM,
                )
            ],
            priority=50,
        )

        policy = await policy_store.create_policy(sample_tenant_id, policy_data)

        assert policy.name == "Test Policy"
        assert policy.tenant_id == sample_tenant_id
        assert len(policy.rules) == 1
        assert policy_store.policy_count == 1

    @pytest.mark.asyncio
    async def test_get_policy(self, policy_store, sample_tenant_id):
        """Test policy retrieval."""
        policy_data = PolicyCreate(
            name="Get Test",
            policy_type=PolicyType.TENANT,
            rules=[],
        )

        created = await policy_store.create_policy(sample_tenant_id, policy_data)
        retrieved = await policy_store.get_policy(created.id, sample_tenant_id)

        assert retrieved.id == created.id
        assert retrieved.name == "Get Test"

    @pytest.mark.asyncio
    async def test_get_policy_not_found(self, policy_store):
        """Test policy not found error."""
        with pytest.raises(PolicyStoreError):
            await policy_store.get_policy(uuid4())

    @pytest.mark.asyncio
    async def test_update_policy(self, policy_store, sample_tenant_id):
        """Test policy update."""
        policy_data = PolicyCreate(
            name="Original Name",
            policy_type=PolicyType.TENANT,
            rules=[],
        )

        created = await policy_store.create_policy(sample_tenant_id, policy_data)

        from src.policy.models import PolicyUpdate

        updates = PolicyUpdate(name="Updated Name", priority=200)
        updated = await policy_store.update_policy(
            created.id, sample_tenant_id, updates
        )

        assert updated.name == "Updated Name"
        assert updated.priority == 200
        assert updated.version == 2

    @pytest.mark.asyncio
    async def test_delete_policy(self, policy_store, sample_tenant_id):
        """Test policy deletion."""
        policy_data = PolicyCreate(
            name="To Delete",
            policy_type=PolicyType.TENANT,
            rules=[],
        )

        created = await policy_store.create_policy(sample_tenant_id, policy_data)
        assert policy_store.policy_count == 1

        deleted = await policy_store.delete_policy(created.id, sample_tenant_id)
        assert deleted is True
        assert policy_store.policy_count == 0

    @pytest.mark.asyncio
    async def test_list_policies(self, policy_store, sample_tenant_id):
        """Test listing policies."""
        # Create multiple policies
        for i in range(3):
            policy_data = PolicyCreate(
                name=f"Policy {i}",
                policy_type=PolicyType.TENANT,
                rules=[],
                priority=i * 10,
            )
            await policy_store.create_policy(sample_tenant_id, policy_data)

        policies = await policy_store.list_policies(sample_tenant_id)

        assert len(policies) == 3
        # Should be sorted by priority (highest first)
        assert policies[0].priority >= policies[1].priority

    @pytest.mark.asyncio
    async def test_get_active_rules(self, policy_store, sample_tenant_id):
        """Test getting active rules."""
        policy_data = PolicyCreate(
            name="With Rules",
            policy_type=PolicyType.TENANT,
            rules=[
                PolicyRuleCreate(
                    rule="rule1",
                    message="Message 1",
                    category=RuleCategory.CUSTOM,
                ),
                PolicyRuleCreate(
                    rule="rule2",
                    message="Message 2",
                    category=RuleCategory.SAFETY_CLINICAL,
                ),
            ],
        )

        await policy_store.create_policy(sample_tenant_id, policy_data)
        rules = await policy_store.get_active_rules(sample_tenant_id)

        assert len(rules) == 2


# ============================================
# PolicyMerger Tests
# ============================================


class TestPolicyMerger:
    """Tests for PolicyMerger."""

    @pytest.mark.asyncio
    async def test_get_merged_rules_includes_global(
        self, policy_merger, sample_tenant_id
    ):
        """Test that merged rules include global rules."""
        rules = await policy_merger.get_merged_rules(sample_tenant_id)

        # Should include global rules
        global_rule_ids = [r.rule_id for r in get_global_rules()]
        merged_rule_ids = [r.rule_id for r in rules]

        for gid in global_rule_ids:
            assert gid in merged_rule_ids

    @pytest.mark.asyncio
    async def test_get_merged_rules_with_tenant(
        self, policy_merger, policy_store, sample_tenant_id
    ):
        """Test merged rules with tenant policies."""
        # Add tenant policy
        policy_data = PolicyCreate(
            name="Tenant Policy",
            policy_type=PolicyType.TENANT,
            rules=[
                PolicyRuleCreate(
                    rule="custom_tenant_rule",
                    message="Tenant specific rule",
                    category=RuleCategory.CUSTOM,
                ),
            ],
        )
        await policy_store.create_policy(sample_tenant_id, policy_data)

        rules = await policy_merger.get_merged_rules(sample_tenant_id)

        # Should include both global and tenant rules
        rule_names = [r.rule for r in rules]
        assert "custom_tenant_rule" in rule_names

    @pytest.mark.asyncio
    async def test_get_merged_constraints(self, policy_merger, sample_tenant_id):
        """Test getting merged constraints."""
        constraints = await policy_merger.get_merged_constraints(sample_tenant_id)

        assert constraints is not None
        assert len(constraints.must_not_say) > 0

    @pytest.mark.asyncio
    async def test_get_safety_rules(self, policy_merger, sample_tenant_id):
        """Test getting safety rules."""
        safety_rules = await policy_merger.get_safety_rules(sample_tenant_id)

        # All should be safety-related
        for rule in safety_rules:
            assert rule.category.value.startswith("safety")


# ============================================
# PolicyEngine Tests
# ============================================


class TestPolicyEngine:
    """Tests for PolicyEngine."""

    @pytest.mark.asyncio
    async def test_evaluate_safe_message(
        self, policy_engine, sample_tenant_id, sample_session_id
    ):
        """Test evaluating a safe message."""
        result = await policy_engine.evaluate_pre_srm(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="Olá, gostaria de agendar uma consulta",
        )

        assert result.passed is True
        assert len(result.violations) == 0
        assert result.should_block is False

    @pytest.mark.asyncio
    async def test_evaluate_diagnosis_request(
        self, policy_engine, sample_tenant_id, sample_session_id
    ):
        """Test evaluating a diagnosis request."""
        result = await policy_engine.evaluate_pre_srm(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="Qual é o meu diagnóstico?",
        )

        assert result.passed is False
        assert len(result.violations) > 0
        assert result.should_block is True

    @pytest.mark.asyncio
    async def test_evaluate_medication_request(
        self, policy_engine, sample_tenant_id, sample_session_id
    ):
        """Test evaluating a medication request."""
        result = await policy_engine.evaluate_pre_srm(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="Me receita um remédio para dor de cabeça",
        )

        assert result.passed is False
        assert len(result.violations) > 0

    @pytest.mark.asyncio
    async def test_evaluate_emergency_message(
        self, policy_engine, sample_tenant_id, sample_session_id
    ):
        """Test evaluating an emergency message."""
        result = await policy_engine.evaluate_pre_srm(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="Estou passando muito mal, é uma emergência",
        )

        # Should trigger handoff for emergencies
        if not result.passed:
            assert result.should_handoff is True

    @pytest.mark.asyncio
    async def test_get_constraints(self, policy_engine, sample_tenant_id):
        """Test getting constraints."""
        constraints = await policy_engine.get_constraints(sample_tenant_id)

        assert constraints is not None
        assert len(constraints.must_not_say) > 0

        # Convert to prompt constraints
        prompt_constraints = constraints.to_prompt_constraints()
        assert len(prompt_constraints) > 0

    @pytest.mark.asyncio
    async def test_get_policies_summary(self, policy_engine, sample_tenant_id):
        """Test getting policies summary."""
        summary = await policy_engine.get_policies_summary(sample_tenant_id)

        assert "total_policies" in summary
        assert "total_rules" in summary
        assert "safety_rules" in summary
        assert summary["total_policies"] >= 1  # At least global policy

    @pytest.mark.asyncio
    async def test_validate_response_safe(self, policy_engine, sample_tenant_id):
        """Test validating a safe response."""
        result = await policy_engine.validate_response(
            tenant_id=sample_tenant_id,
            response="Posso ajudá-lo a agendar uma consulta.",
        )

        assert result.passed is True

    @pytest.mark.asyncio
    async def test_violation_message(
        self, policy_engine, sample_tenant_id, sample_session_id
    ):
        """Test getting violation message."""
        result = await policy_engine.evaluate_pre_srm(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="Qual meu diagnóstico?",
        )

        if not result.passed:
            message = await policy_engine.get_violation_message(result)
            assert len(message) > 0

    @pytest.mark.asyncio
    async def test_evaluation_time_recorded(
        self, policy_engine, sample_tenant_id, sample_session_id
    ):
        """Test that evaluation time is recorded."""
        result = await policy_engine.evaluate_pre_srm(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="Olá",
        )

        assert result.evaluation_time_ms is not None
        assert result.evaluation_time_ms >= 0


# ============================================
# PolicyConstraints Tests
# ============================================


class TestPolicyConstraints:
    """Tests for PolicyConstraints model."""

    def test_merge_constraints(self):
        """Test merging constraints."""
        c1 = PolicyConstraints(
            must_not_say=["item1"],
            must_include=["include1"],
        )
        c2 = PolicyConstraints(
            must_not_say=["item2"],
            safety_reminders=["safety1"],
        )

        merged = c1.merge(c2)

        assert "item1" in merged.must_not_say
        assert "item2" in merged.must_not_say
        assert "include1" in merged.must_include
        assert "safety1" in merged.safety_reminders

    def test_to_prompt_constraints(self):
        """Test converting to prompt constraints."""
        constraints = PolicyConstraints(
            must_not_say=["fornecer diagnóstico"],
            must_include=["ser educado"],
            safety_reminders=["Ligar SAMU em emergência"],
        )

        prompt = constraints.to_prompt_constraints()

        assert any("NÃO" in p for p in prompt)
        assert any("SEMPRE" in p for p in prompt)
        assert any("SAMU" in p for p in prompt)


# ============================================
# PolicyEvaluationResult Tests
# ============================================


class TestPolicyEvaluationResult:
    """Tests for PolicyEvaluationResult model."""

    def test_add_violation(self):
        """Test adding violations."""
        result = PolicyEvaluationResult()

        violation = RuleViolation(
            rule_id="test",
            rule_name="Test Rule",
            message="Test message",
            category=RuleCategory.SAFETY_CLINICAL,
            severity=RuleSeverity.ERROR,
            action=RuleAction.BLOCK,
            policy_id=uuid4(),
            policy_name="Test Policy",
        )

        result.add_violation(violation)

        assert result.passed is False
        assert result.should_block is True
        assert len(result.violations) == 1
        assert result.highest_severity == RuleSeverity.ERROR

    def test_handoff_violation(self):
        """Test handoff violation action."""
        result = PolicyEvaluationResult()

        violation = RuleViolation(
            rule_id="test",
            rule_name="Test Rule",
            message="Test message",
            category=RuleCategory.SAFETY_CLINICAL,
            severity=RuleSeverity.CRITICAL,
            action=RuleAction.HANDOFF,
            policy_id=uuid4(),
            policy_name="Test Policy",
        )

        result.add_violation(violation)

        assert result.should_handoff is True

    def test_severity_escalation(self):
        """Test that highest severity is tracked."""
        result = PolicyEvaluationResult()

        # Add warning first
        result.add_violation(
            RuleViolation(
                rule_id="test1",
                rule_name="Test 1",
                message="Test",
                category=RuleCategory.CUSTOM,
                severity=RuleSeverity.WARNING,
                action=RuleAction.WARN,
                policy_id=uuid4(),
                policy_name="Test",
            )
        )

        assert result.highest_severity == RuleSeverity.WARNING

        # Add critical
        result.add_violation(
            RuleViolation(
                rule_id="test2",
                rule_name="Test 2",
                message="Test",
                category=RuleCategory.SAFETY_CLINICAL,
                severity=RuleSeverity.CRITICAL,
                action=RuleAction.BLOCK,
                policy_id=uuid4(),
                policy_name="Test",
            )
        )

        assert result.highest_severity == RuleSeverity.CRITICAL
