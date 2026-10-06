"""The denial and approval matrix is the security specification."""

import pytest

from copenhagen.core.capability import CapabilitySpec
from copenhagen.core.identity import Principal, RunContext
from copenhagen.policy.engine import PolicyEngine


def refund() -> CapabilitySpec:
    return CapabilitySpec.model_validate(
        {
            "name": "payments.refund",
            "version": 1,
            "owner": "finance",
            "summary": "Refund",
            "inputs": {"amount_cents": {"type": "integer", "minimum": 1, "sensitive": True}},
            "executor": {
                "adapter": "fake",
                "backend": "fake",
                "operation": "refund",
                "queue": "finance",
            },
            "risk": {"class": "financial"},
            "approval": {
                "approver_role": "approver:finance",
                "unattended_when": [{"input": "amount_cents", "op": "lte", "value": 20000}],
            },
            "verify": "none",
            "verify_reason": "unit only",
            "compensate": "none",
            "compensate_reason": "cannot undo",
        }
    )


@pytest.mark.parametrize(
    ("amount", "roles", "patterns", "source", "expected"),
    [
        (10000, (), (), "user", "deny"),
        (10000, ("finance",), ("payments.*",), "user", "allow"),
        (30000, ("finance",), ("payments.*",), "user", "needs_approval"),
        (750000, ("finance",), ("payments.*",), "user", "deny"),
        (10000, ("finance",), ("payments.*",), "ai", "needs_approval"),
        (10000, ("finance",), ("payments.*",), "untrusted_output", "needs_approval"),
        (
            "${order.outputs.total}",
            ("finance",),
            ("payments.*",),
            "trusted_output",
            "unknown_until_runtime",
        ),
    ],
)
def test_matrix(amount, roles, patterns, source, expected):
    principal = Principal(
        id="leela", tenant_id="demo", email="leela@example.com", roles=roles, patterns=patterns
    )
    assert (
        PolicyEngine()
        .decide(
            principal, refund(), {"amount_cents": amount}, {"amount_cents": source}, RunContext()
        )
        .outcome
        == expected
    )


def test_preapproval_never_overrides_ceiling():
    principal = Principal(
        id="leela", tenant_id="demo", email="leela@example.com", patterns=("payments.*",)
    )
    ctx = RunContext(
        runtime=True, preapproval_id="signed", preapproval_steps=("refund",), step_id="refund"
    )
    assert (
        PolicyEngine()
        .decide(principal, refund(), {"amount_cents": 750000}, {"amount_cents": "user"}, ctx)
        .outcome
        == "deny"
    )


def test_cedar_forbid_override():
    engine = PolicyEngine()
    assert (
        engine.cedar(
            "invoke",
            "leela",
            "payments.refund",
            {
                "role_permitted": True,
                "same_tenant": True,
                "enabled": True,
                "amount_cents": 750000,
                "money_ceiling": 500000,
                "risk_class": "financial",
                "approver_allowed": False,
            },
        )
        is False
    )


def test_nested_sensitive_fields_require_origin_review():
    cap = refund().model_copy(
        update={
            "inputs": {
                "recipient": {
                    "type": "object",
                    "properties": {"email": {"type": "string", "sensitive": True}},
                }
            },
            "risk": refund().risk.model_copy(update={"class_": "internal_write"}),
            "approval": refund().approval.model_copy(update={"unattended_when": None}),
        }
    )
    principal = Principal(
        id="operator", tenant_id="company", email="op@company.example", patterns=("payments.*",)
    )
    decision = PolicyEngine().decide(
        principal,
        cap,
        {"recipient": {"email": "external@example.net"}},
        {"recipient": "untrusted_output"},
        RunContext(),
    )
    assert decision.outcome == "needs_approval"
