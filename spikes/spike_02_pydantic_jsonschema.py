"""Spike 2 — a Pydantic model goes to JSON Schema and validates back via `jsonschema`.

Capability specs (Phase 1) store inputs/outputs as JSON Schema with two extra keywords:
`sensitive: true` (redact / show origin to approvers) and `unit: money_cents` (budgets, C8).
This spike checks that:
1. Pydantic emits Draft 2020-12 schemas carrying those keywords through `json_schema_extra`.
2. `jsonschema`'s Draft202012Validator accepts the schema and agrees with Pydantic on
   valid and invalid instances.
3. The extra keywords are ignored by validation (they're annotations) but can be found
   by walking the schema, which is how the validator will find sensitive inputs.
"""

from __future__ import annotations

from typing import Any

import jsonschema
import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, ValidationError

pytestmark = pytest.mark.unit


class RefundInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    payment_id: str = Field(pattern=r"^pi_", min_length=4, max_length=64)
    amount_cents: int = Field(ge=1, json_schema_extra={"unit": "money_cents"})
    reason: str = Field(pattern=r"^(duplicate|fraudulent|requested_by_customer)$")
    customer_email: str = Field(json_schema_extra={"sensitive": True})


def _schema() -> dict[str, Any]:
    schema = RefundInputs.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return schema


def _annotated(schema: dict[str, Any], keyword: str) -> set[str]:
    """Top-level property names carrying an extra keyword (the walk Phase 1 needs)."""
    return {name for name, prop in schema["properties"].items() if keyword in prop}


def test_schema_is_valid_draft_2020_12() -> None:
    Draft202012Validator.check_schema(_schema())


def test_extra_keywords_survive_and_are_discoverable() -> None:
    schema = _schema()
    assert schema["properties"]["amount_cents"]["unit"] == "money_cents"
    assert schema["properties"]["customer_email"]["sensitive"] is True
    assert _annotated(schema, "sensitive") == {"customer_email"}
    assert _annotated(schema, "unit") == {"amount_cents"}
    assert schema["additionalProperties"] is False  # extra="forbid" carries over


GOOD = {
    "payment_id": "pi_123",
    "amount_cents": 4999,
    "reason": "duplicate",
    "customer_email": "customer@example.com",
}

BAD: list[dict[str, Any]] = [
    {**GOOD, "amount_cents": 0},  # below minimum
    {**GOOD, "amount_cents": "4999"},  # wrong type
    {**GOOD, "payment_id": "ch_123"},  # pattern
    {**GOOD, "reason": "because"},  # enum-by-pattern
    {**GOOD, "extra": 1},  # additionalProperties
    {k: v for k, v in GOOD.items() if k != "customer_email"},  # missing required
]


def test_both_validators_accept_good_instance() -> None:
    jsonschema.validate(GOOD, _schema(), cls=Draft202012Validator)
    RefundInputs.model_validate(GOOD)


@pytest.mark.parametrize("instance", BAD, ids=lambda i: str(sorted(i.items()))[:40])
def test_both_validators_reject_bad_instances(instance: dict[str, Any]) -> None:
    errors = list(Draft202012Validator(_schema()).iter_errors(instance))
    assert errors, "jsonschema should reject"
    with pytest.raises(ValidationError):
        RefundInputs.model_validate(instance)


def test_float_money_is_rejected_by_schema() -> None:
    """Money is integer cents everywhere; a float must never pass (C1)."""
    errors = list(Draft202012Validator(_schema()).iter_errors({**GOOD, "amount_cents": 49.99}))
    assert errors
