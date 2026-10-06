"""Deterministic validation and preview; the workflow must check again at run time."""

from typing import Any, cast

from copenhagen.core.capability import CapabilitySpec, Source
from copenhagen.core.identity import Principal, RunContext
from copenhagen.core.plan import REF, PlanIR, Step, walk_values
from copenhagen.core.schema import object_schema
from copenhagen.policy.engine import PolicyEngine


def sources(
    step: Step, caps: dict[str, CapabilitySpec], *, trusted_form: bool = False
) -> dict[str, Source]:
    result: dict[str, Source] = {}
    for key, value in step.inputs.items():
        labels: set[Source] = set()
        for leaf in walk_values(value):
            if isinstance(leaf, str) and (match := REF.fullmatch(leaf)):
                labels.add(
                    "trusted_output"
                    if caps[match[1]].data.output_trust == "trusted"
                    else "untrusted_output"
                )
            else:
                labels.add("user" if trusted_form else "ai")
        result[key] = (
            "untrusted_output"
            if "untrusted_output" in labels
            else "ai"
            if "ai" in labels
            else "trusted_output"
            if "trusted_output" in labels
            else "user"
        )
    return result


def validate(
    plan: PlanIR,
    caps: dict[str, CapabilitySpec],
    principal: Principal,
    policy: PolicyEngine,
    ctx: RunContext,
    *,
    trusted_form: bool = False,
) -> dict[str, Any]:
    previews: list[dict[str, Any]] = []
    warnings: list[str] = []
    money = 0
    for index, step in enumerate(plan.steps):
        cap = caps[step.id]
        if cap.ref != step.capability or cap.status != "active":
            raise ValueError(f"{step.id}: pinned capability is unavailable")

        def checked_schema(value: Any, schema: dict[str, Any], step: Step = step) -> dict[str, Any]:
            if isinstance(value, str) and (match := REF.fullmatch(value)):
                upstream = caps[match[1]]
                if match[2] not in upstream.outputs:
                    raise ValueError(f"{step.id}: output {match[2]} does not exist")
                if upstream.outputs[match[2]]["type"] != schema.get("type"):
                    raise ValueError(f"{step.id}: reference type mismatch")
                # Only this reference's value is deferred; sibling literals and
                # the containing object's required/closed shape still validate.
                return {}
            result = dict(schema)
            if isinstance(value, dict) and schema.get("type") == "object":
                result["properties"] = {
                    key: checked_schema(value[key], child) if key in value else child
                    for key, child in schema.get("properties", {}).items()
                }
            elif isinstance(value, list) and schema.get("type") == "array":
                result["prefixItems"] = [
                    checked_schema(v, schema["items"]) for v in cast("list[Any]", value)
                ]
                result["items"] = False
            return result

        from jsonschema import Draft202012Validator, FormatChecker

        input_schema = object_schema(cap.inputs)
        input_schema["properties"] = {
            key: checked_schema(step.inputs[key], spec) if key in step.inputs else spec
            for key, spec in input_schema["properties"].items()
        }
        errors = list(
            Draft202012Validator(input_schema, format_checker=FormatChecker()).iter_errors(  # pyright: ignore[reportUnknownMemberType]
                step.inputs
            )
        )
        if errors:
            raise ValueError(f"{step.id}: {errors[0].message}")
        origin = sources(step, caps, trusted_form=trusted_form)
        decision = policy.decide(
            principal, cap, step.inputs, origin, ctx.model_copy(update={"step_id": step.id})
        )
        if type(step.inputs.get("amount_cents")) is int and cap.risk.class_ == "financial":
            money += step.inputs["amount_cents"]
        if not cap.risk.reversible and cap.risk.class_ != "read" and index < len(plan.steps) - 1:
            warnings.append(
                f"{step.id}: irreversible operation before later steps; review ordering"
            )
        previews.append(
            {
                "id": step.id,
                "capability": step.capability,
                "inputs": step.inputs,
                "sources": origin,
                "risk": cap.risk.class_,
                "decision": decision.model_dump(mode="json"),
                "schema": object_schema(cap.inputs),
            }
        )
    if money > ctx.money_ceiling:
        raise ValueError("static plan money budget exceeds tenant ceiling")
    return {
        "valid": True,
        "blocked": any(s["decision"]["outcome"] == "deny" for s in previews),
        "steps": previews,
        "warnings": warnings,
        "money_cents": money,
    }
