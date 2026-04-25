"""
NeuroStack API - Config Schemas
Pydantic schemas for clinic configuration CRUD endpoints.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict


class ConfigResponse(BaseModel):
    """
    Flat dict response containing all config fields from every JSONB bucket.

    Uses extra="allow" so any field from any bucket can be included
    without enumerating every key here.
    """

    model_config = ConfigDict(extra="allow")

    clinic_id: Any = None
    version: int = 1


class ConfigPatchRequest(BaseModel):
    """
    Partial update request.

    Accepts any subset of config fields.  The service layer routes
    each field to its correct JSONB bucket.
    """

    model_config = ConfigDict(extra="allow")
