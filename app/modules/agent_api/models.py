"""Audit records for machine-initiated TN-Alpha actions."""
from datetime import datetime

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now()


class AgentAuditLog(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    request_id: str = Field(index=True)
    actor_id: str = Field(index=True)
    org_id: str = Field(index=True)
    role: str = "admin0"
    action: str = Field(index=True)
    resource_type: str = Field(index=True)
    resource_id: str = ""
    brand_id: int | None = Field(default=None, index=True)
    campaign_id: int | None = Field(default=None, index=True)
    payload_json: str = "{}"
    result: str = "success"
    created_at: datetime = Field(default_factory=_now, index=True)
