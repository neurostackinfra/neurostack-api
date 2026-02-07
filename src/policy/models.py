"""
NeuroStack - Policy Engine - Models
Pydantic models for policy evaluation and management.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class PolicyType(str, Enum):
    """Types of policies."""
    GLOBAL = "global"
    TENANT = "tenant"
    SAFETY = "safety"


class RuleSeverity(str, Enum):
    """Severity levels for policy rules."""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class RuleAction(str, Enum):
    """Actions to take when a rule is triggered."""
    LOG = "log"
    WARN = "warn"
    FLAG = "flag"
    BLOCK = "block"
    HANDOFF = "handoff"


class RuleCategory(str, Enum):
    """Categories of policy rules."""
    SAFETY_CLINICAL = "safety_clinical"
    SAFETY_LEGAL = "safety_legal"
    SAFETY_ETHICAL = "safety_ethical"
    CONTENT = "content"
    BEHAVIOR = "behavior"
    COMPLIANCE = "compliance"
    TONE = "tone"
    CUSTOM = "custom"


class PolicyRuleDefinition(BaseModel):
    """Definition of a single policy rule."""

    rule_id: str = Field(default_factory=lambda: str(uuid4())[:8])
    rule: str = Field(..., description="Short rule identifier")
    message: str = Field(..., description="User-facing message when triggered")
    description: Optional[str] = Field(default=None)
    category: RuleCategory = RuleCategory.CUSTOM
    severity: RuleSeverity = RuleSeverity.WARNING
    action: RuleAction = RuleAction.WARN
    patterns: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    def matches(self, text: str) -> bool:
        """Check if text matches rule patterns or keywords."""
        if not self.enabled:
            return False
        
        text_lower = text.lower()
        
        # Check patterns (substring match)
        for pattern in self.patterns:
            if pattern.lower() in text_lower:
                return True
        
        # Check keywords
        for keyword in self.keywords:
            if keyword.lower() in text_lower:
                return True
        
        return False


class PolicyDefinition(BaseModel):
    """Definition of a complete policy."""

    id: UUID = Field(default_factory=uuid4)
    tenant_id: Optional[UUID] = None
    name: str
    description: Optional[str] = None
    policy_type: PolicyType = PolicyType.TENANT
    rules: list[PolicyRuleDefinition] = Field(default_factory=list)
    priority: int = Field(default=0, ge=0)
    is_active: bool = True
    version: int = 1
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    @property
    def is_global(self) -> bool:
        return self.tenant_id is None

    @property
    def rule_count(self) -> int:
        return len(self.rules)


class RuleViolation(BaseModel):
    """Represents a triggered rule violation."""

    rule_id: str
    rule_name: str
    message: str
    category: RuleCategory
    severity: RuleSeverity
    action: RuleAction
    policy_id: UUID
    policy_name: str
    matched_text: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PolicyEvaluationResult(BaseModel):
    """Result of policy evaluation."""

    violations: list[RuleViolation] = Field(default_factory=list)
    passed: bool = True
    should_block: bool = False
    should_handoff: bool = False
    highest_severity: Optional[RuleSeverity] = None
    warnings: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    evaluation_time_ms: Optional[int] = None

    def add_violation(self, violation: RuleViolation) -> None:
        """Add a violation and update state."""
        self.violations.append(violation)
        self.passed = False
        
        if violation.action == RuleAction.BLOCK:
            self.should_block = True
        elif violation.action == RuleAction.HANDOFF:
            self.should_handoff = True
        
        if violation.action == RuleAction.WARN:
            self.warnings.append(violation.message)
        
        # Update highest severity
        severity_order = [
            RuleSeverity.INFO,
            RuleSeverity.WARNING,
            RuleSeverity.ERROR,
            RuleSeverity.CRITICAL,
        ]
        if self.highest_severity is None:
            self.highest_severity = violation.severity
        elif severity_order.index(violation.severity) > severity_order.index(self.highest_severity):
            self.highest_severity = violation.severity


class PolicyContext(BaseModel):
    """Context for policy evaluation."""

    tenant_id: UUID
    session_id: UUID
    message: str
    channel: str = "unknown"
    user_id: Optional[str] = None
    intent: Optional[str] = None
    funnel_stage: Optional[str] = None
    slots: dict[str, Any] = Field(default_factory=dict)
    flags: dict[str, bool] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Doc MVP v0.5: ClinicConfig e UserState (quando disponíveis)
    clinic_config: Optional[dict[str, Any]] = None
    user_state: Optional[dict[str, Any]] = None


class PolicyConstraints(BaseModel):
    """Constraints to apply to LLM prompt generation."""

    must_not_say: list[str] = Field(default_factory=list)
    must_include: list[str] = Field(default_factory=list)
    tone_guidelines: list[str] = Field(default_factory=list)
    safety_reminders: list[str] = Field(default_factory=list)
    custom_instructions: list[str] = Field(default_factory=list)

    def merge(self, other: "PolicyConstraints") -> "PolicyConstraints":
        """Merge another PolicyConstraints into this one."""
        return PolicyConstraints(
            must_not_say=self.must_not_say + other.must_not_say,
            must_include=self.must_include + other.must_include,
            tone_guidelines=self.tone_guidelines + other.tone_guidelines,
            safety_reminders=self.safety_reminders + other.safety_reminders,
            custom_instructions=self.custom_instructions + other.custom_instructions,
        )

    def to_prompt_constraints(self) -> list[str]:
        """Convert to list of constraint strings for prompt."""
        constraints = []
        
        for item in self.must_not_say:
            constraints.append(f"NÃO {item}")
        
        for item in self.must_include:
            constraints.append(f"SEMPRE {item}")
        
        constraints.extend(self.safety_reminders)
        constraints.extend(self.tone_guidelines)
        constraints.extend(self.custom_instructions)
        
        return constraints


# ============================================
# API Request/Response Models
# ============================================


class PolicyRuleCreate(BaseModel):
    """Request model for creating a policy rule."""

    rule: str
    message: str
    description: Optional[str] = None
    category: RuleCategory = RuleCategory.CUSTOM
    severity: RuleSeverity = RuleSeverity.WARNING
    action: RuleAction = RuleAction.WARN
    patterns: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    enabled: bool = True


class PolicyCreate(BaseModel):
    """Request model for creating a policy."""

    name: str
    description: Optional[str] = None
    policy_type: PolicyType = PolicyType.TENANT
    rules: list[PolicyRuleCreate] = Field(default_factory=list)
    priority: int = 0
    is_active: bool = True


class PolicyUpdate(BaseModel):
    """Request model for updating a policy."""

    name: Optional[str] = None
    description: Optional[str] = None
    rules: Optional[list[PolicyRuleCreate]] = None
    priority: Optional[int] = None
    is_active: Optional[bool] = None


class PolicyResponse(BaseModel):
    """Response model for policy endpoints."""

    id: UUID
    tenant_id: Optional[UUID]
    name: str
    description: Optional[str]
    policy_type: PolicyType
    rules: list[PolicyRuleDefinition]
    priority: int
    is_active: bool
    version: int
    rule_count: int
    created_at: datetime
    updated_at: datetime


class PolicyListResponse(BaseModel):
    """Response model for listing policies."""

    policies: list[PolicyResponse]
    total: int
    page: int
    page_size: int

