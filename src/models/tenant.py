"""
NeuroStack - Tenant Model
Represents a builder or clinic using the NeuroStack platform.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from src.models.builder import Builder
    from src.models.session import Session
    from src.models.event import Event
    from src.models.policy import Policy
    from src.models.knowledge import KnowledgeDocument
    from src.models.handoff import HandoffRecord
    from src.models.clinic_config_table import ClinicConfigTable


class TenantConfig:
    """
    Tenant configuration structure.
    
    Defines configurable options per tenant including rate limits,
    session settings, and feature flags.
    """

    def __init__(
        self,
        description: str = "",
        rate_limit: int = 60,
        session_ttl_seconds: int = 86400,
        max_sessions_per_user: int = 10,
        webhook_url: Optional[str] = None,
        features: Optional[dict[str, bool]] = None,
        branding: Optional[dict[str, str]] = None,
        custom_settings: Optional[dict[str, Any]] = None,
    ):
        self.description = description
        self.rate_limit = rate_limit
        self.session_ttl_seconds = session_ttl_seconds
        self.max_sessions_per_user = max_sessions_per_user
        self.webhook_url = webhook_url
        self.features = features or {
            "rag_enabled": True,
            "handoff_enabled": True,
            "safety_strict_mode": False,
        }
        self.branding = branding or {}
        self.custom_settings = custom_settings or {}

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON storage."""
        return {
            "description": self.description,
            "rate_limit": self.rate_limit,
            "session_ttl_seconds": self.session_ttl_seconds,
            "max_sessions_per_user": self.max_sessions_per_user,
            "webhook_url": self.webhook_url,
            "features": self.features,
            "branding": self.branding,
            "custom_settings": self.custom_settings,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TenantConfig":
        """Create from dictionary."""
        return cls(
            description=data.get("description", ""),
            rate_limit=data.get("rate_limit", 60),
            session_ttl_seconds=data.get("session_ttl_seconds", 86400),
            max_sessions_per_user=data.get("max_sessions_per_user", 10),
            webhook_url=data.get("webhook_url"),
            features=data.get("features"),
            branding=data.get("branding"),
            custom_settings=data.get("custom_settings"),
        )


class Tenant(Base, TimestampMixin):
    """
    Tenant model representing a builder or clinic.
    
    Tenants are the top-level entity in NeuroStack. Each tenant has:
    - Unique API key for authentication
    - Configuration settings (rate limits, features, etc.)
    - Associated sessions, events, policies, and knowledge documents
    
    Attributes:
        id: Unique tenant identifier (UUID)
        name: Human-readable tenant name
        slug: URL-friendly identifier (unique)
        api_key_hash: Hashed API key for authentication
        is_active: Whether the tenant is active
        config: JSON configuration object
        created_at: Timestamp of creation
        updated_at: Timestamp of last update
    
    Relationships:
        sessions: All sessions for this tenant
        events: All events for this tenant
        policies: Tenant-specific policies
        knowledge_documents: Knowledge base documents
        handoff_records: Handoff history
    """

    __tablename__ = "tenants"

    # Primary Key
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    # Core Fields
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )
    slug: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        unique=True,
        index=True,
    )
    api_key_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
    )

    # Status
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True,
    )

    # AI Bot Active (pause toggle — independent from billing)
    # When False: webhook skips pipeline, sends offline_message once per session.
    bot_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )

    # Builder FK (nullable — standalone clinics have no builder)
    builder_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("builders.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Plan type: pilot, demo, admin
    plan_type: Mapped[str] = mapped_column(
        String(20),
        default="pilot",
        server_default="pilot",
        nullable=False,
    )

    # Clinic contact phone (surfaced on support screens; not used for messaging)
    contact_phone: Mapped[Optional[str]] = mapped_column(
        String(32),
        nullable=True,
    )

    # Configuration (JSONB)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default=dict,
        nullable=False,
    )

    # Relationships
    sessions: Mapped[list["Session"]] = relationship(
        "Session",
        back_populates="tenant",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )
    events: Mapped[list["Event"]] = relationship(
        "Event",
        back_populates="tenant",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )
    policies: Mapped[list["Policy"]] = relationship(
        "Policy",
        back_populates="tenant",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )
    knowledge_documents: Mapped[list["KnowledgeDocument"]] = relationship(
        "KnowledgeDocument",
        back_populates="tenant",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )
    handoff_records: Mapped[list["HandoffRecord"]] = relationship(
        "HandoffRecord",
        back_populates="tenant",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )
    builder: Mapped[Optional["Builder"]] = relationship(
        "Builder",
        back_populates="tenants",
        lazy="select",
    )
    clinic_config_row: Mapped[Optional["ClinicConfigTable"]] = relationship(
        "ClinicConfigTable",
        back_populates="tenant",
        uselist=False,
        cascade="all, delete-orphan",
        lazy="select",
    )

    def get_config(self) -> TenantConfig:
        """Get typed configuration object."""
        return TenantConfig.from_dict(self.config or {})

    def set_config(self, config: TenantConfig) -> None:
        """Set configuration from typed object."""
        self.config = config.to_dict()

    def get_clinic_config(self) -> Optional["ClinicConfig"]:
        """Get ClinicConfig: primeiro da tabela clinic_configs, depois fallback em config JSON (retrocompat)."""
        from src.models.clinic_config import ClinicConfig
        if self.clinic_config_row:
            return self.clinic_config_row.to_pydantic()
        data = (self.config or {}).get("clinic_config")
        if not data:
            return None
        if isinstance(data, dict):
            return ClinicConfig.model_validate(data)
        return None

    @property
    def rate_limit(self) -> int:
        """Get rate limit from config."""
        return self.config.get("rate_limit", 60) if self.config else 60

    @property
    def session_ttl_seconds(self) -> int:
        """Get session TTL from config."""
        return self.config.get("session_ttl_seconds", 86400) if self.config else 86400

    @property
    def is_rag_enabled(self) -> bool:
        """Check if RAG is enabled for this tenant."""
        features = self.config.get("features", {}) if self.config else {}
        return features.get("rag_enabled", True)

    @property
    def is_handoff_enabled(self) -> bool:
        """Check if handoff is enabled for this tenant."""
        features = self.config.get("features", {}) if self.config else {}
        return features.get("handoff_enabled", True)

    def __repr__(self) -> str:
        return f"<Tenant(id={self.id}, name='{self.name}', active={self.is_active})>"

