"""
NeuroStack - SQLAlchemy ORM Models
Data models for persistence layer.

This module exports all ORM models for database operations.
"""

from src.models.base import Base, TimestampMixin
from src.models.billing import (
    BillingCheckoutSession,
    BillingFactAck,
    BillingFactDelivery,
    BillingFactHandoff,
    BillingFactReconciliationAlert,
    BillingOutboxEvent,
    BillingPaymentEffect,
    BillingPaymentProjection,
    BillingRolloutControl,
    BillingRolloutReceipt,
    BillingSubscription,
    BillingSubscriptionCycle,
    BillingTopupCapabilityReceipt,
    BillingTopupOrder,
    BillingWebhookDelivery,
)
from src.models.billing_producer import (
    BillingProducerActivePair,
    BillingProducerBuildAttestation,
    BillingProducerGrant,
    BillingProducerReceipt,
)
from src.models.billing_producer_signed import (
    BillingProducerV1ActivePair,
    BillingProducerV1BuildAttestation,
    BillingProducerV1Grant,
    BillingProducerV1GrantConsumption,
    BillingProducerV1GrantRevocation,
    BillingProducerV1Receipt,
)
from src.models.builder import Builder
from src.models.clinic_config import ClinicConfig
from src.models.clinic_config_table import ClinicConfigTable
from src.models.coupon import Coupon
from src.models.credit import (
    CreditAccount,
    CreditCapabilityReadiness,
    CreditDebt,
    CreditDebtOrigin,
    CreditDebtSettlement,
    CreditEffect,
    CreditEnforcementBinding,
    CreditEnforcementState,
    CreditIngressClaim,
    CreditLedgerEntry,
    CreditLot,
    CreditObservationalEffect,
    CreditResponseOutbox,
)
from src.models.credit_fact_inbox import CreditFactInbox, CreditRevisionGap
from src.models.credit_inventory import (
    CreditAllocation,
    CreditEgressDelivery,
    CreditInventoryVersion,
)
from src.models.credit_object_copy import (
    CreditObjectCopy,
    CreditObjectCopyJob,
    CreditQdrantJob,
)
from src.models.demo_lead import DemoLead
from src.models.event import Event
from src.models.handoff import HandoffRecord
from src.models.knowledge import KnowledgeDocument
from src.models.media_asset import MediaAsset
from src.models.policy import Policy
from src.models.rlhf_audit_review import RlhfAuditReview
from src.models.rlhf_rating import RlhfRating
from src.models.session import Session
from src.models.support_ticket import SupportTicket
from src.models.tenant import Tenant, TenantConfig
from src.models.transaction import Transaction
from src.models.user import User, UserRole

__all__ = [
    # Base
    "Base",
    "TimestampMixin",
    # Billing Models
    "BillingSubscription",
    "BillingCheckoutSession",
    "BillingSubscriptionCycle",
    "BillingWebhookDelivery",
    "BillingPaymentProjection",
    "BillingPaymentEffect",
    "BillingRolloutControl",
    "BillingRolloutReceipt",
    "BillingTopupCapabilityReceipt",
    "BillingTopupOrder",
    "BillingOutboxEvent",
    "BillingFactDelivery",
    "BillingFactHandoff",
    "BillingFactAck",
    "BillingFactReconciliationAlert",
    "BillingProducerGrant",
    "BillingProducerBuildAttestation",
    "BillingProducerReceipt",
    "BillingProducerActivePair",
    "BillingProducerV1Grant",
    "BillingProducerV1GrantConsumption",
    "BillingProducerV1GrantRevocation",
    "BillingProducerV1BuildAttestation",
    "BillingProducerV1Receipt",
    "BillingProducerV1ActivePair",
    # Credit Models (neurostack-credit-system, migration 038)
    "CreditAccount",
    "CreditEffect",
    "CreditLot",
    "CreditLedgerEntry",
    "CreditDebt",
    "CreditDebtOrigin",
    "CreditDebtSettlement",
    "CreditFactInbox",
    "CreditRevisionGap",
    "CreditCapabilityReadiness",
    "CreditIngressClaim",
    "CreditResponseOutbox",
    "CreditEnforcementState",
    "CreditEnforcementBinding",
    "CreditObservationalEffect",
    # Credit inventory (neurostack-credit-system Group 4a, migration 039)
    "CreditInventoryVersion",
    "CreditAllocation",
    "CreditEgressDelivery",
    "CreditObjectCopy",
    "CreditObjectCopyJob",
    "CreditQdrantJob",
    # Core Models
    "Builder",
    "Coupon",
    "Tenant",
    "TenantConfig",
    "ClinicConfig",
    "ClinicConfigTable",
    "Session",
    "Event",
    "Policy",
    "KnowledgeDocument",
    "MediaAsset",
    "HandoffRecord",
    "RlhfRating",
    "RlhfAuditReview",
    "User",
    "UserRole",
    "DemoLead",
    "SupportTicket",
    "Transaction",
]
