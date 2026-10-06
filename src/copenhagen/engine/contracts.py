"""Workflow/activity boundary contracts, containing data only."""

from typing import Any

from pydantic import Field

from copenhagen.core.capability import CapabilitySpec, Source, Spec
from copenhagen.core.identity import Decision
from copenhagen.core.plan import PlanIR


class RunEnvelope(Spec, frozen=True):
    tenant_id: str
    run_id: str
    requester_id: str
    plan: PlanIR
    caps: dict[str, CapabilitySpec]
    sources: dict[str, dict[str, Source]]


class ControlArgs(Spec, frozen=True):
    tenant_id: str
    run_id: str
    step_id: str = ""
    action: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    sources: dict[str, Source] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)
    attempt: int = 1


class ControlResult(Spec, frozen=True):
    decision: Decision | None = None
    authorization: str = ""
    id: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
