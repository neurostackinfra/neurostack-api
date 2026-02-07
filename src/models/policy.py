"""
NeuroStack - Policy Model
Represents policy rules that govern agent behavior.
"""

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from src.models.tenant import Tenant


class PolicyType(str, Enum):
    """Types of policies."""
    GLOBAL = "global"     # Applies to all tenants (system-level)
    TENANT = "tenant"     # Specific to a single tenant
    SAFETY = "safety"     # Safety-related rules (higher priority)


class RuleSeverity(str, Enum):
    """Severity levels for policy rules."""
    INFO = "info"         # Informational, no action required
    WARNING = "warning"   # Should be flagged but not blocked
    ERROR = "error"       # Should be blocked with explanation
    CRITICAL = "critical" # Must be blocked, may require handoff


class RuleAction(str, Enum):
    """Actions to take when a rule is triggered."""
    LOG = "log"           # Log the occurrence only
    WARN = "warn"         # Include warning in response
    FLAG = "flag"         # Flag for review
    BLOCK = "block"       # Block the action
    HANDOFF = "handoff"   # Trigger human handoff


class PolicyRule:
    """
    Represents a single rule within a policy.
    
    Rules define specific behaviors that should be enforced or monitored.
    Each rule has:
    - Identifier and message for humans
    - Severity and action for automated handling
    - Patterns for matching (keywords, regex, etc.)
    """

    def __init__(
        self,
        rule: str,
        message: str,
        rule_id: Optional[str] = None,
        description: Optional[str] = None,
        severity: RuleSeverity = RuleSeverity.WARNING,
        action: RuleAction = RuleAction.WARN,
        patterns: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ):
        self.rule_id = rule_id or rule
        self.rule = rule
        self.message = message
        self.description = description
        self.severity = severity
        self.action = action
        self.patterns = patterns or []
        self.metadata = metadata or {}

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON storage."""
        return {
            "rule_id": self.rule_id,
            "rule": self.rule,
            "message": self.message,
            "description": self.description,
            "severity": self.severity.value if isinstance(self.severity, RuleSeverity) else self.severity,
            "action": self.action.value if isinstance(self.action, RuleAction) else self.action,
            "patterns": self.patterns,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PolicyRule":
        """Create from dictionary."""
        severity = data.get("severity", "warning")
        action = data.get("action", "warn")
        
        return cls(
            rule=data.get("rule", ""),
            message=data.get("message", ""),
            rule_id=data.get("rule_id"),
            description=data.get("description"),
            severity=RuleSeverity(severity) if isinstance(severity, str) else severity,
            action=RuleAction(action) if isinstance(action, str) else action,
            patterns=data.get("patterns", []),
            metadata=data.get("metadata", {}),
        )

    def matches(self, text: str) -> bool:
        """Check if text matches any of the rule patterns."""
        text_lower = text.lower()
        for pattern in self.patterns:
            if pattern.lower() in text_lower:
                return True
        return False


class Policy(Base, TimestampMixin):
    """
    Policy model for defining agent behavior rules.
    
    Policies control how the AI agent behaves, including:
    - What it can and cannot say
    - Safety constraints
    - Tone and style guidelines
    - Compliance requirements
    
    Policies can be:
    - Global: Apply to all tenants (managed by NeuroStack)
    - Tenant: Apply to a specific tenant
    - Safety: High-priority safety rules
    
    Attributes:
        id: Unique policy identifier (UUID)
        tenant_id: Owning tenant (NULL for global policies)
        name: Human-readable policy name
        description: Detailed policy description
        policy_type: Type of policy (global, tenant, safety)
        rules: JSON array of policy rules
        priority: Priority level (higher = applied first)
        is_active: Whether the policy is currently active
        version: Policy version number
        created_at: Creation timestamp
        updated_at: Last update timestamp
    
    Relationships:
        tenant: The tenant that owns this policy (if tenant-specific)
    """

    __tablename__ = "policies"

    # Primary Key
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    # Foreign Key (NULL for global policies)
    tenant_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Core Fields
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )
    policy_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
    )

    # Rules (JSONB array)
    rules: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        default=list,
        nullable=False,
    )

    # Priority and Status
    priority: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False,
    )

    # Relationship
    tenant: Mapped[Optional["Tenant"]] = relationship(
        "Tenant",
        back_populates="policies",
    )

    def get_rules(self) -> list[PolicyRule]:
        """Get typed list of policy rules."""
        return [PolicyRule.from_dict(rule) for rule in (self.rules or [])]

    def set_rules(self, rules: list[PolicyRule]) -> None:
        """Set rules from typed list."""
        self.rules = [rule.to_dict() for rule in rules]

    def add_rule(self, rule: PolicyRule) -> None:
        """Add a single rule to the policy."""
        if self.rules is None:
            self.rules = []
        self.rules.append(rule.to_dict())

    def remove_rule(self, rule_id: str) -> bool:
        """Remove a rule by ID. Returns True if removed."""
        if self.rules is None:
            return False
        original_len = len(self.rules)
        self.rules = [r for r in self.rules if r.get("rule_id") != rule_id]
        return len(self.rules) < original_len

    @property
    def is_global(self) -> bool:
        """Check if this is a global policy."""
        return self.tenant_id is None

    @property
    def is_safety_policy(self) -> bool:
        """Check if this is a safety policy."""
        return self.policy_type == PolicyType.SAFETY.value

    @property
    def rule_count(self) -> int:
        """Get number of rules in policy."""
        return len(self.rules) if self.rules else 0

    def increment_version(self) -> None:
        """Increment policy version on update."""
        self.version += 1

    def evaluate(self, text: str) -> list[tuple[PolicyRule, bool]]:
        """
        Evaluate text against all rules.
        
        Args:
            text: Text to evaluate
        
        Returns:
            List of tuples (rule, matched) for all rules
        """
        results = []
        for rule in self.get_rules():
            matched = rule.matches(text)
            results.append((rule, matched))
        return results

    def get_triggered_rules(self, text: str) -> list[PolicyRule]:
        """
        Get all rules triggered by the text.
        
        Args:
            text: Text to evaluate
        
        Returns:
            List of triggered PolicyRule objects
        """
        return [rule for rule, matched in self.evaluate(text) if matched]

    def __repr__(self) -> str:
        scope = "global" if self.is_global else f"tenant={self.tenant_id}"
        return f"<Policy(id={self.id}, name='{self.name}', {scope}, active={self.is_active})>"

