"""Deny-first policy with Cedar and conditions; rechecked on resolved values."""

from __future__ import annotations

from fnmatch import fnmatchcase
from importlib.resources import files
from pathlib import Path
from typing import Any

import cedarpy

from copenhagen.core.canonical import sha256_hex
from copenhagen.core.capability import CapabilitySpec, Source
from copenhagen.core.conditions import Truth, evaluate_all
from copenhagen.core.identity import Decision, Principal, RunContext
from copenhagen.core.plan import REF, walk_values
from copenhagen.core.schema import is_sensitive

POLICY_DIR = Path(str(files("copenhagen.policy"))) / "defaults"


def schema() -> dict[str, Any]:
    context = {
        "type": "Record",
        "attributes": {
            k: {"type": typ}
            for k, typ in {
                "role_permitted": "Boolean",
                "same_tenant": "Boolean",
                "enabled": "Boolean",
                "approver_allowed": "Boolean",
                "amount_cents": "Long",
                "money_ceiling": "Long",
                "risk_class": "String",
            }.items()
        },
    }
    return {
        "": {
            "entityTypes": {
                "Principal": {"shape": {"type": "Record", "attributes": {}}},
                "Capability": {"shape": {"type": "Record", "attributes": {}}},
            },
            "actions": {
                name: {
                    "appliesTo": {
                        "principalTypes": ["Principal"],
                        "resourceTypes": ["Capability"],
                        "context": context,
                    }
                }
                for name in ("invoke", "invoke_unattended", "approve", "publish")
            },
        }
    }


class PolicyEngine:
    def __init__(self, policies: str | None = None, directory: str | None = None) -> None:
        source = (
            policies
            if policies is not None
            else "\n".join(
                path.read_text()
                for path in sorted((Path(directory) if directory else POLICY_DIR).glob("*.cedar"))
            )
        )
        if not source:
            raise ValueError("policy set is empty")
        validation = cedarpy.validate_policies(source, schema())  # pyright: ignore[reportUnknownMemberType]
        if not validation.validation_passed:
            raise ValueError(f"invalid Cedar policies: {validation}")
        self.policies = cedarpy.PolicySet.from_str(source)
        self.policy_version = sha256_hex({"cedar": source, "python_semantics": 1})[:16]

    def cedar(self, action: str, principal: str, capability: str, context: dict[str, Any]) -> bool:
        result = cedarpy.is_authorized(  # pyright: ignore[reportUnknownMemberType]
            {
                "principal": {"type": "Principal", "id": principal},
                "action": {"type": "Action", "id": action},
                "resource": {"type": "Capability", "id": capability},
                "context": context,
            },
            self.policies,
            [],
            schema=schema(),
        )
        return result.allowed and not result.diagnostics.errors

    def decide(
        self,
        principal: Principal,
        cap: CapabilitySpec,
        inputs: dict[str, Any] | None,
        taint: dict[str, Source],
        ctx: RunContext,
    ) -> Decision:
        values = inputs or {}
        roles = (cap.approval.approver_role or f"approver:{cap.executor.queue}",)

        def decision(outcome: Any, *reasons: str, via: str | None = None) -> Decision:
            return Decision(
                outcome=outcome,
                reasons=reasons,
                approver_roles=roles,
                policy_version=self.policy_version,
                approved_via=via,
            )

        granted = any(fnmatchcase(cap.name, pattern) for pattern in principal.patterns)
        if cap.risk.class_ == "infrastructure":
            return decision("deny", "infrastructure requires a saved-plan adapter (Phase 7)")
        if principal.status != "active" or cap.status != "active" or ctx.kill_switch:
            return decision("deny", "principal/capability disabled or tenant kill switch active")
        if not granted:
            role = cap.executor.queue
            return decision(
                "deny", f"requires role {role}; ask your administrator or domain approver"
            )
        if ctx.budget_exceeded:
            return decision("deny", "usage budget exceeded")
        amount = values.get("amount_cents", 0)
        unresolved = any(isinstance(v, str) and REF.fullmatch(v) for v in walk_values(values))
        cedar_context = {
            "role_permitted": granted,
            "same_tenant": True,
            "enabled": not ctx.kill_switch,
            "amount_cents": amount if type(amount) is int else 0,
            "money_ceiling": ctx.money_ceiling,
            "risk_class": cap.risk.class_,
            "approver_allowed": False,
        }
        if not self.cedar("invoke", principal.id, cap.name, cedar_context):
            return decision(
                "deny", "Cedar policy denied invocation (hard ceiling or tenant restriction)"
            )
        if cap.risk.class_ == "financial" and type(amount) is not int and not unresolved:
            return decision("deny", "financial amount must be integer cents")
        needs = cap.risk.class_ in {"identity", "infrastructure", "destructive", "legal"}
        reasons: list[str] = ["risk class requires independent approval"] if needs else []
        unknown = False
        if cap.approval.unattended_when is not None:
            truth = evaluate_all(
                cap.approval.unattended_when, values, internal_domains=ctx.internal_domains
            )
            if truth is Truth.FALSE:
                needs = True
                reasons.append("outside unattended conditions")
            elif truth is Truth.UNKNOWN:
                unknown = True
        elif cap.risk.class_ == "financial":
            needs = True
            reasons.append("financial write has no unattended limit")
        if not self.cedar("invoke_unattended", principal.id, cap.name, cedar_context):
            needs = True
        tainted = any(
            is_sensitive(spec) and taint.get(key, "ai") in {"ai", "untrusted_output"}
            for key, spec in cap.inputs.items()
        )
        if tainted:
            needs = True
            reasons.append("sensitive value from AI or untrusted output; review its origin")
        # Static ceilings with referenced amounts cannot be decided from placeholder zeroes.
        if unresolved and cap.risk.class_ == "financial":
            unknown = True
        if ctx.runtime and unknown:
            needs = True
            reasons.append("condition unresolved at execution; human approval required")
        if (
            needs
            and ctx.preapproval_id
            and ctx.step_id in ctx.preapproval_steps
            and cap.risk.class_ != "destructive"
            and not tainted
        ):
            return decision(
                "allow",
                "covered by valid constrained recipe pre-approval",
                via=f"recipe_preapproval:{ctx.preapproval_id}",
            )
        if needs:
            return decision("needs_approval", *(reasons or ["Cedar unattended permission absent"]))
        if unknown:
            return decision("unknown_until_runtime", "decision depends on resolved step outputs")
        return decision("allow", "role, Cedar, conditions and source checks passed")

    def may_approve(
        self, principal: Principal, cap: CapabilitySpec, inputs: dict[str, Any], ctx: RunContext
    ) -> bool:
        return self.cedar(
            "approve",
            principal.id,
            cap.name,
            {
                "role_permitted": True,
                "same_tenant": True,
                "enabled": principal.status == "active" and not ctx.kill_switch,
                "approver_allowed": True,
                "risk_class": cap.risk.class_,
                "amount_cents": inputs.get("amount_cents", 0),
                "money_ceiling": ctx.money_ceiling,
            },
        )
