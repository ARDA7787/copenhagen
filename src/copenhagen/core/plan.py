"""Pinned plan IR, strict references, pure graph and resolution operations."""

from __future__ import annotations

import re
from typing import Any, Literal, Self, cast

from pydantic import Field, model_validator

from copenhagen.core.capability import PIN, Source, Spec

REF = re.compile(r"^\$\{([a-z][a-z0-9_]*)\.outputs\.([a-z][a-z0-9_]*)\}$")


class Step(Spec, frozen=True):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    capability: str = Field(pattern=PIN)
    inputs: dict[str, Any] = Field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    sources: dict[str, Source] = Field(default_factory=dict)


class PlanIR(Spec, frozen=True):
    plan_version: Literal[2] = 2
    summary: str = ""
    on_failure: Literal["continue", "atomic"] = "continue"
    questions: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    steps: tuple[Step, ...] = Field(min_length=1, max_length=25)
    rationale: tuple[dict[str, str], ...] = ()

    @model_validator(mode="after")
    def graph(self) -> Self:
        ids = {s.id for s in self.steps}
        if len(ids) != len(self.steps):
            raise ValueError("duplicate step ids")
        completed: set[str] = set()
        while len(completed) < len(ids):
            ready = {
                s.id for s in self.steps if s.id not in completed and set(s.depends_on) <= completed
            }
            if not ready:
                raise ValueError("cycle or nonexistent dependency")
            completed |= ready
        for step in self.steps:
            if len(set(step.depends_on)) != len(step.depends_on):
                raise ValueError("duplicate dependency")
            for value in walk_values(step.inputs):
                if isinstance(value, str) and "${" in value:
                    match = REF.fullmatch(value)
                    if match is None:
                        raise ValueError("references must be exactly ${step.outputs.field}")
                    if match[1] not in step.depends_on:
                        raise ValueError(f"{step.id}: reference must name a direct dependency")
        return self


def walk_values(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [v for x in cast("dict[str, Any]", value).values() for v in walk_values(x)]
    if isinstance(value, list | tuple):
        return [v for x in cast("list[Any]", value) for v in walk_values(x)]
    return [value]


def resolve_refs(value: Any, outputs: dict[str, dict[str, Any]]) -> Any:
    if isinstance(value, dict):
        return {k: resolve_refs(v, outputs) for k, v in cast("dict[str, Any]", value).items()}
    if isinstance(value, list | tuple):
        return [resolve_refs(v, outputs) for v in cast("list[Any]", value)]
    if isinstance(value, str) and (match := REF.fullmatch(value)):
        try:
            return outputs[match[1]][match[2]]
        except KeyError as error:
            raise ValueError(f"unresolved reference {value}") from error
    return value


def ready_batches(steps: tuple[Step, ...], statuses: dict[str, str]) -> list[Step]:
    return [
        s
        for s in steps
        if s.id not in statuses
        and all(statuses.get(d) in {"succeeded", "skipped"} for d in s.depends_on)
    ][:4]


def backoff(attempt: int) -> int:
    return min(2 ** (attempt - 1), 60)
