"""Contract attacks: schemas, versions, graph references and template injection."""

import pytest
from pydantic import ValidationError

from copenhagen.core.capability import CapabilitySpec
from copenhagen.core.plan import PlanIR
from copenhagen.core.recipe import RecipeSpec, instantiate
from copenhagen.core.schema import validate_values


def test_bool_is_not_money() -> None:
    with pytest.raises(ValueError, match=r".+"):
        validate_values({"amount": {"type": "integer"}}, {"amount": True})


def test_unknown_input_refused() -> None:
    with pytest.raises(ValueError, match=r".+"):
        validate_values({"name": {"type": "string"}}, {"name": "Alice", "extra": "oops"})


def test_cycle_refused() -> None:
    with pytest.raises(ValidationError):
        PlanIR.model_validate(
            {
                "steps": [
                    {"id": "a", "capability": "fake.read@1", "depends_on": ["b"]},
                    {"id": "b", "capability": "fake.read@1", "depends_on": ["a"]},
                ]
            }
        )


def test_unpinned_capability_refused() -> None:
    with pytest.raises(ValidationError):
        PlanIR.model_validate({"steps": [{"id": "a", "capability": "fake.read"}]})


def test_template_preserves_type_and_does_not_expand_user_refs() -> None:
    recipe = RecipeSpec.model_validate(
        {
            "name": "test.example",
            "version": 1,
            "owner": "test",
            "description": "test",
            "parameters": {"value": {"type": "integer"}},
            "steps": [{"id": "a", "capability": "fake.read@1", "inputs": {"value": "{{value}}"}}],
        }
    )
    assert instantiate(recipe, {"value": 7}).steps[0].inputs["value"] == 7
    with pytest.raises(ValueError, match=r".+"):
        instantiate(recipe, {"value": "${victim.outputs.money}"})


def test_http_requires_hosts() -> None:
    with pytest.raises(ValidationError):
        CapabilitySpec.model_validate(
            {
                "name": "test.example",
                "version": 1,
                "owner": "test",
                "summary": "test",
                "executor": {
                    "adapter": "http",
                    "backend": "test",
                    "operation": "POST /test",
                    "queue": "test",
                },
                "risk": {"class": "read"},
                "verify": "none",
                "verify_reason": "read",
                "compensate": "none",
                "compensate_reason": "read",
            }
        )
