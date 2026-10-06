"""Recipes are parameterized plans, never executable code or Jinja programs."""

from __future__ import annotations

import re
from typing import Any, Literal, Self, cast

from pydantic import Field, model_validator

from copenhagen.core.capability import NAME, Spec
from copenhagen.core.conditions import Condition
from copenhagen.core.plan import PlanIR, Step, walk_values
from copenhagen.core.schema import FieldMap, check_fields, validate_values

PARAM = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")


class RecipeApproval(Spec, frozen=True):
    pre_approvable: bool = False
    covers_steps: tuple[str, ...] = ()
    constraints: tuple[Condition, ...] = ()


class RecipeSpec(Spec, frozen=True):
    api_version: Literal["copenhagen/v1"] = Field(default="copenhagen/v1", alias="apiVersion")
    kind: Literal["Recipe"] = "Recipe"
    name: str = Field(pattern=NAME)
    version: int = Field(ge=1, strict=True)
    owner: str
    description: str
    parameters: FieldMap = Field(default_factory=dict)
    on_failure: Literal["continue", "atomic"] = "continue"
    allow_event_trigger: bool = False
    approval: RecipeApproval = Field(default_factory=RecipeApproval)
    steps: tuple[Step, ...] = Field(min_length=1, max_length=25)

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"

    @model_validator(mode="after")
    def valid(self) -> Self:
        check_fields(self.parameters)
        PlanIR(steps=self.steps)
        if not set(self.approval.covers_steps) <= {s.id for s in self.steps}:
            raise ValueError("covers_steps contains unknown step")
        for condition in self.approval.constraints:
            if condition.parameter not in self.parameters:
                raise ValueError("constraint names unknown parameter")
        for step in self.steps:
            for value in walk_values(step.inputs):
                if isinstance(value, str):
                    for name in PARAM.findall(value):
                        if name not in self.parameters:
                            raise ValueError(f"unknown parameter {name}")
                    if "{{" in PARAM.sub("", value) or "}}" in PARAM.sub("", value):
                        raise ValueError("invalid parameter placeholder")
        return self


def instantiate(recipe: RecipeSpec, parameters: dict[str, Any]) -> PlanIR:
    values = {k: spec["default"] for k, spec in recipe.parameters.items() if "default" in spec}
    values.update(parameters)
    validate_values(recipe.parameters, values)
    # Untrusted form values must never introduce executable reference/template syntax.
    for value in walk_values(values):
        if isinstance(value, str) and any(token in value for token in ("${", "{{", "}}")):
            raise ValueError("parameter contains reserved reference/template syntax")

    def render(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: render(v) for k, v in cast("dict[str, Any]", value).items()}
        if isinstance(value, list | tuple):
            return [render(v) for v in cast("list[Any]", value)]
        if not isinstance(value, str):
            return value
        exact = PARAM.fullmatch(value)
        if exact:
            return values[exact[1]]
        return PARAM.sub(lambda m: str(values[m[1]]), value)

    return PlanIR(
        summary=recipe.description,
        on_failure=recipe.on_failure,
        steps=tuple(
            step.model_copy(
                update={"inputs": render(step.inputs), "sources": {k: "user" for k in step.inputs}}
            )
            for step in recipe.steps
        ),
    )
