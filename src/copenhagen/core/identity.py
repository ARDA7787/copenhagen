"""Deterministic identity and decision contracts."""

from typing import Literal

from copenhagen.core.capability import Spec


class Principal(Spec, frozen=True):
    id: str
    tenant_id: str
    email: str
    kind: Literal["user", "service"] = "user"
    roles: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    status: str = "active"


class Decision(Spec, frozen=True):
    outcome: Literal["allow", "needs_approval", "deny", "unknown_until_runtime"]
    approver_roles: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    policy_version: str
    approved_via: str | None = None


class RunContext(Spec, frozen=True):
    env: str = "dev"
    runtime: bool = False
    internal_domains: frozenset[str] = frozenset()
    kill_switch: bool = False
    budget_exceeded: bool = False
    preapproval_id: str | None = None
    preapproval_steps: tuple[str, ...] = ()
    step_id: str = ""
    money_ceiling: int = 500000
