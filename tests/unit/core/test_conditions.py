"""Spec for core/conditions.py (P1-02). See docs/walkthroughs/02-conditions.md.

What must be true:
- The language is exactly ten operators: eq ne lt lte gt gte in not_in matches is_internal_domain.
- Evaluation is three-valued. UNKNOWN means "cannot decide yet"; the policy engine turns it into
  `unknown_until_runtime` at plan time and into `needs_approval` at run time. It never means "yes".
- Evaluation is total: any input value gives TRUE, FALSE or UNKNOWN, never an exception. Anything
  odd (missing input, unresolved `${ref}`, wrong type, float, malformed email) is UNKNOWN.
- Types are strict: `True` is not `1`, `"5"` is not `5`, floats are never compared (money is cents).
- A list of conditions is a Kleene AND: one FALSE wins, then one UNKNOWN, else TRUE.
- A broken condition (unknown op, wrong value type, bad regex) is refused when the spec loads.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from copenhagen.core.conditions import (
    MAX_MATCH_INPUT_CHARS,
    UNRESOLVED,
    Condition,
    Op,
    Truth,
    evaluate,
    evaluate_all,
    is_reference,
)

pytestmark = pytest.mark.unit

T, F, U = Truth.TRUE, Truth.FALSE, Truth.UNKNOWN
INTERNAL = frozenset({"acme.com", "acme.co.uk"})


def cond(parameter: str, op: str, value: Any) -> Condition:
    return Condition.model_validate({"parameter": parameter, "op": op, "value": value})


def ev(c: Condition, inputs: dict[str, Any]) -> Truth:
    return evaluate(c, inputs, internal_domains=INTERNAL)


# ------------------------------------------------------------------ the language


def test_the_language_is_exactly_ten_operators() -> None:
    assert {op.value for op in Op} == {
        "eq",
        "ne",
        "lt",
        "lte",
        "gt",
        "gte",
        "in",
        "not_in",
        "matches",
        "is_internal_domain",
    }


# ------------------------------------------------------------------ Truth (Kleene logic)


def test_truth_and_table() -> None:
    table = {
        (T, T): T,
        (T, F): F,
        (T, U): U,
        (F, F): F,
        (F, U): F,
        (U, U): U,
    }
    for (a, b), want in table.items():
        assert (a & b) is want
        assert (b & a) is want


def test_truth_is_not_a_bool() -> None:
    # `if evaluate(...)` would treat UNKNOWN as true. Refuse it outright.
    with pytest.raises(TypeError):
        bool(U)
    with pytest.raises(TypeError):
        bool(T)


# ------------------------------------------------------------------ comparison operators


@pytest.mark.parametrize(
    ("op", "value", "inp", "want"),
    [
        ("eq", 5, 5, T),
        ("eq", 5, 6, F),
        ("ne", 5, 6, T),
        ("ne", 5, 5, F),
        ("eq", "usd", "usd", T),
        ("eq", "usd", "USD", F),
        ("eq", None, None, T),
        ("eq", None, "x", F),
        ("eq", None, 0, F),  # eq/ne null asks "is it null?" - a definite answer for any scalar
        ("ne", None, "x", T),
        ("eq", True, True, T),
        ("eq", False, True, F),
        ("lt", 20000, 19999, T),
        ("lt", 20000, 20000, F),
        ("lte", 20000, 20000, T),
        ("lte", 20000, 20001, F),
        ("gt", 0, 1, T),
        ("gt", 0, 0, F),
        ("gte", 0, 0, T),
        ("gte", 0, -1, F),
        ("lt", "2026-01-01", "2025-12-31", T),
        ("gte", "2026-01-01", "2025-12-31", F),
    ],
)
def test_comparisons(op: str, value: Any, inp: Any, want: Truth) -> None:
    assert ev(cond("x", op, value), {"x": inp}) is want


@pytest.mark.parametrize(
    ("op", "value", "inp"),
    [
        ("eq", 1, True),  # bool is not int
        ("eq", True, 1),
        ("eq", 0, False),
        ("ne", 1, True),  # ne is not "anything that isn't eq": wrong kind is UNKNOWN
        ("eq", 5, "5"),
        ("ne", 5, "5"),
        ("eq", 5, None),  # a null where a number was expected: UNKNOWN, not "not equal"
        ("ne", 5, None),
        ("lt", 100, "50"),
        ("lt", 100, True),
        ("lte", 100, None),
        ("gt", "a", 1),
        ("eq", 5, 5.0),  # floats are never compared
        ("lt", 100, 99.5),
        ("eq", "x", ["x"]),
        ("eq", "x", {"x": 1}),
        ("eq", None, ["x"]),
    ],
)
def test_wrong_kind_is_unknown(op: str, value: Any, inp: Any) -> None:
    assert ev(cond("x", op, value), {"x": inp}) is U


def test_ordering_needs_an_int_or_string_bound() -> None:
    for bad in (True, None, ["a"], 1.5):
        with pytest.raises(ValidationError):
            cond("x", "lt", bad)


def test_eq_needs_a_scalar() -> None:
    for bad in (1.5, ["a"], {"a": 1}):
        with pytest.raises(ValidationError):
            cond("x", "eq", bad)


# ------------------------------------------------------------------ in / not_in


def test_in_and_not_in() -> None:
    c_in = cond("currency", "in", ["usd", "eur"])
    c_out = cond("currency", "not_in", ["usd", "eur"])
    assert ev(c_in, {"currency": "usd"}) is T
    assert ev(c_in, {"currency": "gbp"}) is F
    assert ev(c_out, {"currency": "gbp"}) is T
    assert ev(c_out, {"currency": "usd"}) is F


def test_in_is_strict_about_kinds() -> None:
    c = cond("n", "in", [1, 2, 3])
    assert ev(c, {"n": True}) is U  # True == 1 in Python; not here
    assert ev(c, {"n": "1"}) is U
    assert ev(c, {"n": 1.0}) is U
    assert ev(cond("n", "not_in", [1, 2]), {"n": True}) is U


def test_in_with_mixed_kinds_in_the_list() -> None:
    c = cond("v", "in", ["none", None])
    assert ev(c, {"v": None}) is T
    assert ev(c, {"v": "none"}) is T
    assert ev(c, {"v": "x"}) is F


def test_in_needs_a_non_empty_list_of_scalars() -> None:
    for bad in ([], "usd", 5, [["a"]], [{"a": 1}], [1.5], None):
        with pytest.raises(ValidationError):
            cond("x", "in", bad)


def test_in_list_is_frozen() -> None:
    c = cond("x", "in", ["a", "b"])
    assert isinstance(c.value, tuple)


# ------------------------------------------------------------------ matches


def test_matches_is_a_full_match() -> None:
    c = cond("ref", "matches", r"INV-[0-9]{4}")
    assert ev(c, {"ref": "INV-1234"}) is T
    # A partial match would let "INV-1234; DROP" through. fullmatch, not search.
    assert ev(c, {"ref": "INV-1234; DROP"}) is F
    assert ev(c, {"ref": "x INV-1234"}) is F
    assert ev(c, {"ref": "INV-1234\n"}) is F


def test_matches_on_non_strings_is_unknown() -> None:
    c = cond("ref", "matches", r"[0-9]+")
    assert ev(c, {"ref": 123}) is U
    assert ev(c, {"ref": None}) is U


def test_matches_refuses_huge_inputs() -> None:
    c = cond("ref", "matches", r"a*")
    assert ev(c, {"ref": "a" * MAX_MATCH_INPUT_CHARS}) is T
    assert ev(c, {"ref": "a" * (MAX_MATCH_INPUT_CHARS + 1)}) is U


def test_matches_needs_a_valid_regex() -> None:
    for bad in ("(unclosed", "[z-a]", 5, None, ["a"], "", "a" * 513):
        with pytest.raises(ValidationError):
            cond("x", "matches", bad)


# ------------------------------------------------------------------ is_internal_domain


@pytest.mark.parametrize(
    ("email", "want"),
    [
        ("ana@acme.com", T),
        ("Ana@ACME.COM", T),
        ("ana@acme.com.", T),  # trailing root dot is the same domain
        ("ana@acme.co.uk", T),
        ("ana@gmail.com", F),
        ("ana@eu.acme.com", F),  # subdomains are not internal unless listed
        ("ana@acme.com.evil.io", F),
        ("ana@notacme.com", F),
    ],
)
def test_is_internal_domain(email: str, want: Truth) -> None:
    assert ev(cond("e", "is_internal_domain", True), {"e": email}) is want
    negated = {T: F, F: T}[want]
    assert ev(cond("e", "is_internal_domain", False), {"e": email}) is negated


@pytest.mark.parametrize(
    "email",
    [
        "no-at-sign",
        "@acme.com",
        "ana@",
        "a@b@acme.com",
        "ana@acme..com",
        "ana@-acme.com",
        "ana@acme-.com",
        "ana@acme.com ",
        " ana@acme.com",
        "ana@ácme.com",  # non-ASCII: lookalike risk; punycode must be used
        "ana@acme" + chr(0x2024) + "com",  # ONE DOT LEADER, not a full stop
        "ana@acme.com\n",
        "",
    ],
)
def test_malformed_email_is_unknown(email: str) -> None:
    assert ev(cond("e", "is_internal_domain", True), {"e": email}) is U
    assert ev(cond("e", "is_internal_domain", False), {"e": email}) is U


def test_is_internal_domain_on_non_strings_is_unknown() -> None:
    c = cond("e", "is_internal_domain", True)
    assert ev(c, {"e": None}) is U
    assert ev(c, {"e": 5}) is U


def test_internal_domains_are_normalised() -> None:
    c = cond("e", "is_internal_domain", True)
    assert evaluate(c, {"e": "a@acme.com"}, internal_domains=frozenset({"ACME.com."})) is T


def test_no_internal_domains_means_everything_is_external() -> None:
    c = cond("e", "is_internal_domain", False)
    assert evaluate(c, {"e": "a@acme.com"}, internal_domains=frozenset()) is T


def test_is_internal_domain_needs_a_bool() -> None:
    for bad in ("true", 1, None, ["acme.com"]):
        with pytest.raises(ValidationError):
            cond("e", "is_internal_domain", bad)


# ------------------------------------------------------------------ unknown inputs


def test_missing_input_is_unknown() -> None:
    assert ev(cond("amount", "lte", 100), {}) is U
    assert ev(cond("amount", "eq", None), {}) is U  # absent is not null


def test_unresolved_input_is_unknown_for_every_operator() -> None:
    samples: dict[Op, Any] = {
        Op.EQ: 1,
        Op.NE: 1,
        Op.LT: 1,
        Op.LTE: 1,
        Op.GT: 1,
        Op.GTE: 1,
        Op.IN: [1],
        Op.NOT_IN: [1],
        Op.MATCHES: ".*",
        Op.IS_INTERNAL_DOMAIN: False,
    }
    for op, value in samples.items():
        c = cond("x", op.value, value)
        assert ev(c, {"x": UNRESOLVED}) is U, op
        assert ev(c, {"x": "${find_order.outputs.amount}"}) is U, op


@pytest.mark.parametrize(
    ("value", "want"),
    [
        ("${a.outputs.b}", True),
        ("${find_order.outputs.amount_cents}", True),
        ("${intent.text}", True),
        ("  ${a.outputs.b}  ", False),
        ("prefix ${a.outputs.b}", False),
        ("${}", False),
        ("$a.b", False),
        ("plain", False),
        (5, False),
        (None, False),
    ],
)
def test_is_reference(value: Any, want: bool) -> None:
    assert is_reference(value) is want


def test_unresolved_has_a_readable_repr() -> None:
    assert repr(UNRESOLVED) == "UNRESOLVED"


# ------------------------------------------------------------------ evaluate_all


def test_evaluate_all_is_kleene_and() -> None:
    small = cond("amount", "lte", 20000)
    usd = cond("currency", "eq", "usd")

    def all_(inputs: dict[str, Any]) -> Truth:
        return evaluate_all([small, usd], inputs, internal_domains=INTERNAL)

    assert all_({"amount": 100, "currency": "usd"}) is T
    assert all_({"amount": 100, "currency": "eur"}) is F
    # FALSE beats UNKNOWN: we already know approval is needed.
    assert all_({"amount": UNRESOLVED, "currency": "eur"}) is F
    assert all_({"amount": UNRESOLVED, "currency": "usd"}) is U


def test_evaluate_all_of_nothing_is_true() -> None:
    # The identity of AND. CapabilitySpec refuses an empty unattended_when list (P1-03/P1-04),
    # so this never silently makes a capability unattended.
    assert evaluate_all([], {}, internal_domains=INTERNAL) is T


# ------------------------------------------------------------------ the model


def test_condition_is_frozen_and_strict() -> None:
    c = cond("amount", "lte", 20000)
    with pytest.raises(ValidationError):
        c.value = 1  # type: ignore[misc]
    with pytest.raises(ValidationError):
        Condition.model_validate({"parameter": "a", "op": "lte", "value": 1, "extra": 1})
    with pytest.raises(ValidationError):
        cond("a", "lessthan", 1)
    with pytest.raises(ValidationError):
        cond("a", "lte", 1.0)
    with pytest.raises(ValidationError):
        Condition.model_validate({"parameter": "a", "op": "eq"})


@pytest.mark.parametrize("name", ["", "1abc", "a-b", "a.b", "A", "a b", "${x}", "x" * 65])
def test_parameter_must_be_a_snake_case_name(name: str) -> None:
    with pytest.raises(ValidationError):
        cond(name, "eq", 1)


def test_condition_round_trips_through_json() -> None:
    c = cond("currency", "in", ["usd", "eur"])
    assert Condition.model_validate_json(c.model_dump_json()) == c
    assert c.model_dump(mode="json") == {
        "parameter": "currency",
        "op": "in",
        "value": ["usd", "eur"],
    }


def test_condition_is_hashable() -> None:
    # Frozen with a tuple value, so conditions can live in sets and be cached by the policy engine.
    assert len({cond("x", "in", ["a"]), cond("x", "in", ["a"])}) == 1


# ------------------------------------------------------------------ properties

# Spec literals stay within +/-(2**53 - 1), like everything else that gets hashed (P1-01).
scalars = st.none() | st.booleans() | st.integers(-(2**53 - 1), 2**53 - 1) | st.text(max_size=6)
anything = st.recursive(
    scalars | st.floats(allow_nan=True) | st.just(UNRESOLVED),
    lambda c: st.lists(c, max_size=3) | st.dictionaries(st.text(max_size=3), c, max_size=3),
    max_leaves=8,
)
conditions = st.one_of(
    st.builds(cond, st.just("x"), st.sampled_from(["eq", "ne"]), scalars),
    st.builds(
        cond,
        st.just("x"),
        st.sampled_from(["lt", "lte", "gt", "gte"]),
        st.integers(-100, 100) | st.text(max_size=3),
    ),
    st.builds(
        cond,
        st.just("x"),
        st.sampled_from(["in", "not_in"]),
        st.lists(scalars, min_size=1, max_size=4),
    ),
    st.builds(
        cond, st.just("x"), st.just("matches"), st.sampled_from(["a+", "[0-9]{2}", ".*", "x|y"])
    ),
    st.builds(cond, st.just("x"), st.just("is_internal_domain"), st.booleans()),
)


@given(conditions, anything)
def test_evaluation_is_total(c: Condition, inp: Any) -> None:
    assert ev(c, {"x": inp}) in (T, F, U)


@given(scalars, scalars)
def test_ne_is_the_negation_of_eq_when_known(value: Any, inp: Any) -> None:
    e = ev(cond("x", "eq", value), {"x": inp})
    n = ev(cond("x", "ne", value), {"x": inp})
    assert (e, n) in ((T, F), (F, T), (U, U))


@given(st.lists(scalars, min_size=1, max_size=4), anything)
def test_not_in_is_the_negation_of_in_when_known(values: list[Any], inp: Any) -> None:
    a = ev(cond("x", "in", values), {"x": inp})
    b = ev(cond("x", "not_in", values), {"x": inp})
    assert (a, b) in ((T, F), (F, T), (U, U))


@given(st.integers(-1000, 1000), st.integers(-1000, 1000))
def test_ordering_operators_agree_with_python_on_ints(bound: int, inp: int) -> None:
    assert ev(cond("x", "lt", bound), {"x": inp}) is (T if inp < bound else F)
    assert ev(cond("x", "lte", bound), {"x": inp}) is (T if inp <= bound else F)
    assert ev(cond("x", "gt", bound), {"x": inp}) is (T if inp > bound else F)
    assert ev(cond("x", "gte", bound), {"x": inp}) is (T if inp >= bound else F)


@given(st.lists(conditions, max_size=4), anything)
def test_evaluate_all_matches_folding_and(cs: list[Condition], inp: Any) -> None:
    want = T
    for c in cs:
        want = want & ev(c, {"x": inp})
    assert evaluate_all(cs, {"x": inp}, internal_domains=INTERNAL) is want


@given(conditions)
def test_unresolved_is_always_unknown(c: Condition) -> None:
    assert ev(c, {"x": UNRESOLVED}) is U


@pytest.mark.parametrize("pattern", ["(a+)+$", "a*a*a*a*$", "(a|aa)+$", r"(a)\1"])
def test_backtracking_patterns_are_refused_at_publication(pattern):
    with pytest.raises(ValueError, match="regex"):
        cond("x", "matches", pattern)
