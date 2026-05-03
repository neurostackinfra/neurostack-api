"""
NeuroStack API - Demo Leads Schemas
Pydantic schemas for the demo gate lead capture endpoints.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DemoLeadCreate(BaseModel):
    """Request schema for creating a demo lead."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Full name of the lead",
    )
    email: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Email address",
    )
    company: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Company or organization name",
    )
    phone: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description="Phone number",
    )


class DemoLeadResponse(BaseModel):
    """Response schema for a demo lead."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="Unique lead identifier")
    name: str = Field(..., description="Full name of the lead")
    email: str = Field(..., description="Email address")
    company: str = Field(..., description="Company or organization name")
    phone: str = Field(..., description="Phone number")
    created_at: datetime = Field(..., description="When the lead was captured")
