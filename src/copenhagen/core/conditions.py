"""Structured conditions: the whole language for `approval.unattended_when` (PRD v2 §4.1).

Ten operators and nothing else (decision 10: no expression strings, no parser). A condition
compares one named step input with a literal from the spec. Evaluation is three-valued:

- TRUE / FALSE when the input is present, resolved and of the expected kind;
- UNKNOWN otherwise: missing, still a ``${ref}``, wrong kind, a float, a malformed email.

UNKNOWN never means "yes". The policy engine maps it to ``unknown_until_runtime`` at plan time
and to ``needs_approval`` at run time. ``Truth`` refuses ``bool()`` so nobody can write
``if evaluate(...)`` and treat UNKNOWN as true by accident.

Pure and workflow-safe: no clock, no randomness, no I/O.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Mapping
from typing import Any, Final, NoReturn, cast

from pydantic import AliasChoices, BaseModel, Field, model_validator

__all__ = [
    "MAX_MATCH_INPUT_CHARS",
    "MAX_PATTERN_CHARS",
    "UNRESOLVED",
    "Condition",
    "Op",
    "Truth",
    "evaluate",
    "evaluate_all",
    "is_reference",
]

#: Longest string ``matches`` will look at. Longer inputs are UNKNOWN (bounds regex cost).
MAX_MATCH_INPUT_CHARS: Final = 4096
#: Longest pattern a spec may declare.
MAX_PATTERN_CHARS: Final = 512

_MAX_SAFE_INT = 2**53 - 1
_REFERENCE = re.compile(r"\$\{[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*\}")
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class _Unresolved:
    """Marks an input whose value comes from a step that has not run yet."""

    _instance: _Unresolved | None = None

    def __new__(cls) -> _Unresolved:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNRESOLVED"


#: Pass this for an input that is a reference not yet resolved. ``${...}`` strings work too.
UNRESOLVED: Final = _Unresolved()


class Op(enum.StrEnum):
    """The ten operators. That is the whole language; anything richer is a Cedar policy."""

    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    IN = "in"
    NOT_IN = "not_in"
    MATCHES = "matches"
    IS_INTERNAL_DOMAIN = "is_internal_domain"


class Truth(enum.Enum):
    """Kleene three-valued truth. Combine with ``&``; never test with ``if``."""

    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"

    def __and__(self, other: Truth) -> Truth:
        if Truth.FALSE in (self, other):
            return Truth.FALSE
        if Truth.UNKNOWN in (self, other):
            return Truth.UNKNOWN
        return Truth.TRUE

    def __or__(self, other: Truth) -> Truth:
        if Truth.TRUE in (self, other):
            return Truth.TRUE
        if Truth.UNKNOWN in (self, other):
            return Truth.UNKNOWN
        return Truth.FALSE

    def __invert__(self) -> Truth:
        return {Truth.TRUE: Truth.FALSE, Truth.FALSE: Truth.TRUE}.get(self, Truth.UNKNOWN)

    def __bool__(self) -> NoReturn:
        raise TypeError("Truth is three-valued; compare with `is Truth.TRUE` explicitly")


# ------------------------------------------------------------------ the model

_ORDERING = frozenset({Op.LT, Op.LTE, Op.GT, Op.GTE})
_MEMBERSHIP = frozenset({Op.IN, Op.NOT_IN})


def check_safe_pattern(pattern: str) -> None:
    """Bound the supported regex subset to avoid backtracking denial of service.

    Literals, character classes, anchors, simple alternatives and one repetition
    are supported. Grouping, backreferences and compound repetitions need a more
    expressive policy instead. Escaped punctuation remains literal.
    """
    if len(pattern) > MAX_PATTERN_CHARS:
        raise ValueError("regex pattern is too long")
    repeats = 0
    alternatives = False
    in_class = False
    escaped = False
    for char in pattern:
        if escaped:
            if char.isdigit() or char in {"g", "k"}:
                raise ValueError("regex backreferences are not supported")
            escaped = False
            continue
        if char == "\\":
            escaped = True
        elif char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif not in_class:
            if char in "()":
                raise ValueError("regex groups and lookarounds are not supported")
            if char in "*+?{":
                repeats += 1
            if char == "|":
                alternatives = True
    if repeats > 1 or (repeats and alternatives):
        raise ValueError("compound regex repetition is not supported")


def _kind(value: Any) -> str | None:
    """The comparison kind of a scalar, or None if it is not one we compare.

    ``bool`` is checked before ``int`` because ``True`` is an ``int`` in Python. Floats have no
    kind: money is integer cents (C8), so a float in a condition is a bug, not a rounding issue.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, str):
        return "str"
    return None


def _check_scalar(value: Any, what: str) -> None:
    if _kind(value) is None:
        raise ValueError(f"{what} must be a string, integer, boolean or null, not {_name(value)}")
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > _MAX_SAFE_INT:
        raise ValueError(f"{what} is outside +/-(2**53 - 1)")


def _name(value: Any) -> str:
    return "null" if value is None else type(value).__name__


class Condition(BaseModel, frozen=True, extra="forbid"):
    """One condition: ``{parameter: amount_cents, op: lte, value: 20000}``.

    ``value`` depends on ``op``:

    - ``eq``, ``ne``: a string, integer, boolean or null;
    - ``lt``, ``lte``, ``gt``, ``gte``: an integer or a string (ISO dates compare as strings);
    - ``in``, ``not_in``: a non-empty list of strings, integers, booleans or nulls;
    - ``matches``: a regular expression, matched against the whole input;
    - ``is_internal_domain``: ``true`` or ``false``.
    """

    parameter: str = Field(
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
        validation_alias=AliasChoices("parameter", "input", "output"),
    )
    op: Op
    value: Any

    @model_validator(mode="after")
    def _value_fits_op(self) -> Condition:
        op, value = self.op, self.value
        if op in (Op.EQ, Op.NE):
            _check_scalar(value, f"`{op}` value")
        elif op in _ORDERING:
            if _kind(value) not in ("int", "str"):
                raise ValueError(f"`{op}` value must be an integer or a string, not {_name(value)}")
            _check_scalar(value, f"`{op}` value")
        elif op in _MEMBERSHIP:
            if not isinstance(value, list | tuple) or not value:
                raise ValueError(f"`{op}` value must be a non-empty list")
            items = tuple(cast("Iterable[Any]", value))
            for i, item in enumerate(items):
                _check_scalar(item, f"`{op}` item {i}")
            object.__setattr__(self, "value", items)
        elif op is Op.MATCHES:
            if not isinstance(value, str) or not value:
                raise ValueError("`matches` value must be a non-empty regular expression string")
            if len(value) > MAX_PATTERN_CHARS:
                raise ValueError(f"`matches` pattern is longer than {MAX_PATTERN_CHARS} characters")
            check_safe_pattern(value)
            try:
                re.compile(value)
            except re.error as e:
                raise ValueError(
                    f"`matches` value is not a valid regular expression: {e}"
                ) from None
        elif not isinstance(value, bool):
            raise ValueError(
                f"`is_internal_domain` value must be true or false, not {_name(value)}"
            )
        return self


# ------------------------------------------------------------------ evaluation


def is_reference(value: Any) -> bool:
    """Is ``value`` exactly one ``${step.outputs.x}``-style reference (not yet resolved)?"""
    return isinstance(value, str) and _REFERENCE.fullmatch(value) is not None


def _truth(b: bool) -> Truth:
    return Truth.TRUE if b else Truth.FALSE


def _eq(expected: Any, actual: Any) -> Truth:
    """Strict equality. Same kind: compare. ``null`` against any scalar: definite. Else UNKNOWN."""
    actual_kind = _kind(actual)
    if actual_kind is None:
        return Truth.UNKNOWN
    expected_kind = _kind(expected)
    if expected_kind == "null":
        return _truth(actual_kind == "null")
    if actual_kind != expected_kind:
        return Truth.UNKNOWN
    return _truth(expected == actual)


def _ordered(op: Op, bound: int | str, actual: Any) -> Truth:
    if _kind(actual) != _kind(bound):
        return Truth.UNKNOWN
    if op is Op.LT:
        return _truth(actual < bound)
    if op is Op.LTE:
        return _truth(actual <= bound)
    if op is Op.GT:
        return _truth(actual > bound)
    return _truth(actual >= bound)


def _member(items: tuple[Any, ...], actual: Any) -> Truth:
    """``in`` is the Kleene OR of ``eq`` over the items."""
    result = Truth.FALSE
    for item in items:
        result = result | _eq(item, actual)
        if result is Truth.TRUE:
            break
    return result


def _matches(pattern: str, actual: Any) -> Truth:
    if not isinstance(actual, str) or len(actual) > MAX_MATCH_INPUT_CHARS:
        return Truth.UNKNOWN
    # fullmatch: a partial match would let "INV-1234; anything" through.
    return _truth(re.fullmatch(pattern, actual) is not None)


def _normalise_domain(domain: str) -> str | None:
    """Lower-case ASCII hostname without a trailing root dot, or None if it is not one."""
    if not domain.isascii():
        return None  # lookalikes: internationalised domains must arrive as punycode
    domain = domain.lower()
    if domain.endswith("."):
        domain = domain[:-1]
    labels = domain.split(".")
    if len(labels) < 2 or not all(_DOMAIN_LABEL.fullmatch(label) for label in labels):
        return None
    return domain


def _email_domain(email: str) -> str | None:
    local, at, domain = email.partition("@")
    if not at or not local or "@" in domain:
        return None
    if any(c.isspace() for c in local) or not local.isprintable():
        return None
    return _normalise_domain(domain)


def _internal(expected: bool, actual: Any, internal_domains: frozenset[str]) -> Truth:
    if not isinstance(actual, str):
        return Truth.UNKNOWN
    domain = _email_domain(actual)
    if domain is None:
        return Truth.UNKNOWN
    internal = {d for d in map(_normalise_domain, internal_domains) if d is not None}
    # Exact match only: a subdomain is internal only if it is listed.
    return _truth((domain in internal) == expected)


def evaluate(
    condition: Condition, inputs: Mapping[str, Any], *, internal_domains: frozenset[str]
) -> Truth:
    """Evaluate one condition against a step's inputs. Never raises.

    ``internal_domains`` is the tenant's list of its own email domains (for
    ``is_internal_domain``); pass it explicitly so no hidden configuration is read here.
    """
    if condition.parameter not in inputs:
        return Truth.UNKNOWN  # absent is not null
    actual = inputs[condition.parameter]
    if actual is UNRESOLVED or is_reference(actual):
        return Truth.UNKNOWN

    op, value = condition.op, condition.value
    if op is Op.EQ:
        return _eq(value, actual)
    if op is Op.NE:
        return ~_eq(value, actual)
    if op in _ORDERING:
        return _ordered(op, value, actual)
    if op is Op.IN:
        return _member(value, actual)
    if op is Op.NOT_IN:
        return ~_member(value, actual)
    if op is Op.MATCHES:
        return _matches(value, actual)
    return _internal(value, actual, internal_domains)


def evaluate_all(
    conditions: Iterable[Condition],
    inputs: Mapping[str, Any],
    *,
    internal_domains: frozenset[str],
) -> Truth:
    """Kleene AND: any FALSE gives FALSE, else any UNKNOWN gives UNKNOWN, else TRUE.

    An empty list is TRUE (the identity of AND). The spec model refuses an empty
    ``unattended_when`` list, so this never makes a capability unattended by omission.
    """
    result = Truth.TRUE
    for condition in conditions:
        result = result & evaluate(condition, inputs, internal_domains=internal_domains)
        if result is Truth.FALSE:
            break
    return result
