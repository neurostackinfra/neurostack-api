"""
NeuroStack - ClinicConfig ORM (tabela clinic_configs)
Modelo fixo de configuração da clínica armazenado no banco SQL.
"""

from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin
from src.models.clinic_config import (
    ClinicConfig,
    ClinicHandoff,
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
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_clinic_configs_tenant_id"),)

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
        )
