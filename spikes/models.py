"""Pydantic models used by the spike workflow. Frozen and strict, like the real contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ApprovalRequest(_Frozen):
    run_id: str
    amount_cents: int = Field(ge=0, json_schema_extra={"unit": "money_cents"})
    recipient_email: str = Field(json_schema_extra={"sensitive": True})
    requested_at: datetime


class ApprovalDecision(_Frozen):
    approver: str
    approved: bool
    reason: str | None = None


class Outcome(_Frozen):
    run_id: str
    status: Literal["approved", "rejected", "expired"]
    decision: ApprovalDecision | None
    waited_seconds: float
    stale_signals: int
