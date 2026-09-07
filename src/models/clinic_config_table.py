"""
NeuroStack - ClinicConfig ORM (tabela clinic_configs)
Modelo fixo de configuração da clínica armazenado no banco SQL.
"""

from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, Integer, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin
from src.models.clinic_config import (
    ClinicBusinessRules,
    ClinicConfig,
    ClinicHandoff,
    ClinicIdentity,
    ClinicLGPD,
    ClinicOperations,
    ClinicPersona,
    ClinicPostCare,
    ClinicPricing,
    ClinicScheduling,
)

if TYPE_CHECKING:
    from src.models.tenant import Tenant


class ClinicConfigTable(Base, TimestampMixin):
    """
    Tabela clinic_configs: uma linha por tenant (1:1).
    Campos aninhados (persona, scheduling, etc.) em JSONB.
    """

    __tablename__ = "clinic_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_clinic_configs_tenant_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    clinic_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    persona: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="{}",
    )
    scheduling: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="{}",
    )
    pricing: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="{}",
    )
    post_care: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="{}",
    )
    handoff: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="{}",
    )

    # --- New admin-panel buckets (migration 005) ---

    identity: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=True,
        server_default=text("'{}'"),
    )
    operations: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=True,
        server_default=text("'{}'"),
    )
    business_rules: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=True,
        server_default=text("'{}'"),
    )
    lgpd: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=True,
        server_default=text("'{}'"),
    )
    professionals: Mapped[Optional[list[Any]]] = mapped_column(
        JSONB,
        nullable=True,
        server_default=text("'[]'"),
    )
    services: Mapped[Optional[list[Any]]] = mapped_column(
        JSONB,
        nullable=True,
        server_default=text("'[]'"),
    )

    # --- API credentials bucket (migration 006) ---

    api_credentials: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=True,
        server_default=text("'{}'"),
    )

    tenant: Mapped["Tenant"] = relationship(
        "Tenant",
        back_populates="clinic_config_row",
    )

    def to_pydantic(self) -> ClinicConfig:
        """Converte a linha ORM para o modelo Pydantic ClinicConfig."""
        return ClinicConfig(
            clinic_id=self.clinic_id,
            version=self.version,
            persona=ClinicPersona.model_validate(self.persona or {}),
            scheduling=ClinicScheduling.model_validate(self.scheduling or {}),
            pricing=ClinicPricing.model_validate(self.pricing or {}),
            post_care=ClinicPostCare.model_validate(self.post_care or {}),
            handoff=ClinicHandoff.model_validate(self.handoff or {}),
            identity=ClinicIdentity.model_validate(self.identity or {}),
            operations=ClinicOperations.model_validate(self.operations or {}),
            business_rules=ClinicBusinessRules.model_validate(
                self.business_rules or {}
            ),
            lgpd=ClinicLGPD.model_validate(self.lgpd or {}),
        )

    @classmethod
    def from_pydantic(
        cls,
        tenant_id: UUID,
        config: ClinicConfig,
        *,
        id: Optional[UUID] = None,
    ) -> "ClinicConfigTable":
        """Cria uma instância ORM a partir do modelo Pydantic."""
        return cls(
            id=id or uuid4(),
            tenant_id=tenant_id,
            clinic_id=config.clinic_id,
            version=config.version,
            persona=config.persona.model_dump(),
            scheduling=config.scheduling.model_dump(),
            pricing=config.pricing.model_dump(),
            post_care=config.post_care.model_dump(),
            handoff=config.handoff.model_dump(),
            identity=config.identity.model_dump(),
            operations=config.operations.model_dump(),
            business_rules=config.business_rules.model_dump(),
            lgpd=config.lgpd.model_dump(),
            professionals=config.professionals,
            services=config.services,
        )
