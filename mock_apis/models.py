"""Pydantic models for the mock APIs.

Two shapes: Dutch ERP (messy, as-received) and canonical platform (clean).
The mapping lives in the n8n Code node, not here.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# -------- ERP side (as the legacy system emits it) --------

class ErpTenant(BaseModel):
    huurder_id: str
    naam: Optional[str] = None
    kvk_nummer: Optional[str] = None
    unit_code: Optional[str] = None
    contract_start: Optional[str] = None     # DD-MM-YYYY
    maandhuur: Optional[str] = None          # "1.250,00"
    status: Optional[str] = None             # "actief" / "beeindigd" / anything
    gewijzigd_op: str                        # ISO timestamp


# -------- Platform side (canonical) --------

TenantStatus = Literal["active", "ended"]


class PlatformTenantIn(BaseModel):
    external_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    chamber_of_commerce_id: str = Field(pattern=r"^\d{8}$")
    unit: str = Field(min_length=1)
    lease_start: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    monthly_rent_eur: float = Field(ge=0)
    status: TenantStatus


class PlatformTenantOut(PlatformTenantIn):
    updated_at: str


class TurnoverRow(BaseModel):
    external_id: str = Field(min_length=1)
    centre: str = Field(min_length=1)
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    amount_eur: float


class TurnoverBulkIn(BaseModel):
    rows: list[TurnoverRow]


class TurnoverRowResult(BaseModel):
    index: int
    status: Literal["created", "duplicate", "error"]
    error: Optional[str] = None


class TurnoverBulkOut(BaseModel):
    idempotency_key: str
    received: int
    created: int
    duplicates: int
    errors: int
    results: list[TurnoverRowResult]


# -------- Integration errors --------

class IntegrationErrorIn(BaseModel):
    source: str
    payload: dict
    http_status: Optional[int] = None
    error_body: Optional[str] = None


class TriagePatch(BaseModel):
    category: Literal[
        "auth", "validation", "mapping", "rate_limit",
        "upstream_outage", "duplicate", "unknown",
    ]
    likely_cause: str
    suggested_fix: str
    client_message: str
    confidence: float = Field(ge=0.0, le=1.0)
    model: Optional[str] = None             # "claude-..." or "rules"


class IntegrationErrorOut(BaseModel):
    id: int
    source: str
    payload: dict
    http_status: Optional[int]
    error_body: Optional[str]
    status: Literal["new", "triaged"]
    triage: Optional[TriagePatch]
    created_at: str
    updated_at: str
