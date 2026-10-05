"""Spec for core/canonical.py (P1-01). See docs/walkthroughs/01-canonical.md.

What must be true:
- The same meaning always gives the same bytes: key order, tuples vs lists, time zones don't matter.
- Floats never reach a hash. Money is integer cents, so a float is a bug, not a rounding question.
- Keys sort by UTF-16 code units (RFC 8785), not by code points.
- An approval hash covers the inputs and, for infrastructure steps, the preview artifact.
- An idempotency key has no attempt number, so a retry reuses it (I4).
"""

from __future__ import annotations

import enum
import hashlib
import json
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from copenhagen.core.canonical import (
    CanonicalError,
    canonical_json,
    idempotency_key,
    inputs_hash,
    sha256_hex,
)

pytestmark = pytest.mark.unit

MAX_SAFE_INT = 2**53 - 1


# ------------------------------------------------------------------ strategies

json_scalars = st.none() | st.booleans() | st.integers(-MAX_SAFE_INT, MAX_SAFE_INT) | st.text()
json_values = st.recursive(
    json_scalars,
    lambda children: (
        st.lists(children, max_size=5) | st.dictionaries(st.text(max_size=8), children, max_size=5)
    ),
    max_leaves=25,
)
json_objects = st.dictionaries(st.text(max_size=8), json_values, max_size=6)

# Keys ASCII at every depth: only then do code-point and UTF-16 order agree.
ascii_keys = st.text(st.characters(max_codepoint=0x7F), max_size=8)
ascii_keyed_values = st.recursive(
    json_scalars,
    lambda children: (
        st.lists(children, max_size=5) | st.dictionaries(ascii_keys, children, max_size=5)
    ),
    max_leaves=25,
)


# ------------------------------------------------------------------ RFC 8785 vectors


def test_literals() -> None:
    assert canonical_json({"literals": [None, True, False]}) == b'{"literals":[null,true,false]}'


def test_keys_sort_by_utf16_code_units() -> None:
    # RFC 8785 section 3.2.3. Code-point order would put U+FB33 before U+1F600;
    # UTF-16 order does not.
    value = {
        "€": "Euro Sign",
        "\r": "Carriage Return",
        "דּ": "Hebrew Letter Dalet With Dagesh",
        "1": "One",
        "\U0001f600": "Emoji: Grinning Face",
        "\u0080": "Control",
        "ö": "Latin Small Letter O With Diaeresis",
    }
    order = [v for _, v in json.loads(canonical_json(value)).items()]
    assert order == [
        "Carriage Return",
        "One",
        "Control",
        "Latin Small Letter O With Diaeresis",
        "Euro Sign",
        "Emoji: Grinning Face",
        "Hebrew Letter Dalet With Dagesh",
    ]


def test_string_escaping() -> None:
    # RFC 8785 section 3.2.2.2: only ", \ and control characters are escaped;
    # everything else is written as raw UTF-8.
    value = "\u20ac$\u000f\nA'B" + '"' + "\\" + "\\" + '"' + "/"
    expected = '"\u20ac$' + r"\u000f\nA'B\"\\\\\"/" + '"'
    assert canonical_json(value) == expected.encode()


def test_nested_objects_sorted_without_whitespace() -> None:
    value = {"b": [1, {"z": None, "a": "x y"}], "a": {"d": True, "c": -7}}
    assert canonical_json(value) == b'{"a":{"c":-7,"d":true},"b":[1,{"a":"x y","z":null}]}'


def test_non_ascii_is_raw_utf8() -> None:
    assert canonical_json("é") == b'"\xc3\xa9"'


def test_tuple_is_a_list() -> None:
    assert canonical_json((1, 2)) == canonical_json([1, 2]) == b"[1,2]"


def test_largest_safe_integers_are_accepted() -> None:
    assert canonical_json([MAX_SAFE_INT, -MAX_SAFE_INT]) == b"[9007199254740991,-9007199254740991]"


# ------------------------------------------------------------------ datetimes and enums


def test_aware_datetime_is_utc_with_z() -> None:
    assert canonical_json(datetime(2026, 10, 5, 12, 0, tzinfo=UTC)) == b'"2026-10-05T12:00:00Z"'


def test_same_instant_in_any_zone_is_the_same() -> None:
    utc = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    cet = utc.astimezone(timezone(timedelta(hours=2)))
    assert canonical_json(cet) == canonical_json(utc)


def test_microseconds_are_kept() -> None:
    value = datetime(2026, 10, 5, 12, 0, 0, 123, tzinfo=UTC)
    assert canonical_json(value) == b'"2026-10-05T12:00:00.000123Z"'


class _Risk(enum.StrEnum):
    FINANCIAL = "financial"


class _Code(enum.IntEnum):
    ONE = 1


def test_enums_become_their_values() -> None:
    assert canonical_json({"r": _Risk.FINANCIAL, "c": _Code.ONE}) == b'{"c":1,"r":"financial"}'


# ------------------------------------------------------------------ rejections


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(1.0, id="whole-float"),
        pytest.param(0.1, id="float"),
        pytest.param(-0.0, id="negative-zero"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="inf"),
        pytest.param(Decimal("49.99"), id="decimal"),
        pytest.param(b"bytes", id="bytes"),
        pytest.param({1, 2}, id="set"),
        pytest.param(datetime(2026, 10, 5, 12, 0), id="naive"),
        pytest.param(date(2026, 10, 5), id="date"),
        pytest.param({1: "non-string key"}, id="int-key"),
        pytest.param({("a",): "tuple key"}, id="tuple-key"),
        pytest.param(MAX_SAFE_INT + 1, id="int-too-big"),
        pytest.param(-MAX_SAFE_INT - 1, id="int-too-small"),
        pytest.param("\ud800", id="lone-surrogate"),  # not valid Unicode
        pytest.param({"\ud800": "lone surrogate key"}, id="lone-surrogate-key"),
        pytest.param(object(), id="object"),
        pytest.param([1, [2, {"amount": 49.99}]], id="nested-float-in-list"),
        pytest.param({"a": {"b": (1, 2.5)}}, id="nested-float-in-tuple"),
    ],
)
def test_rejects(value: Any) -> None:
    with pytest.raises(CanonicalError):
        canonical_json(value)


@given(json_values, st.floats(allow_nan=True, allow_infinity=True))
def test_a_float_anywhere_is_rejected(value: Any, f: float) -> None:
    for wrapped in ([value, f], {"x": value, "money": f}, [{"deep": [f]}]):
        with pytest.raises(CanonicalError):
            canonical_json(wrapped)


# ------------------------------------------------------------------ properties


@given(json_values)
def test_round_trips_through_json(value: Any) -> None:
    assert json.loads(canonical_json(value)) == value


@given(json_values)
def test_canonicalising_twice_changes_nothing(value: Any) -> None:
    once = canonical_json(value)
    assert canonical_json(json.loads(once)) == once


@given(json_objects)
def test_insertion_order_does_not_matter(value: dict[str, Any]) -> None:
    reversed_value = dict(reversed(list(value.items())))
    assert canonical_json(reversed_value) == canonical_json(value)


@given(st.dictionaries(ascii_keys, ascii_keyed_values))
def test_matches_stdlib_for_ascii_keys(value: dict[str, Any]) -> None:
    # With ASCII keys, code-point and UTF-16 order agree,
    # so the stdlib is a reference implementation.
    reference = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert canonical_json(value) == reference.encode()


# ------------------------------------------------------------------ hashes


def test_sha256_hex_hashes_the_canonical_bytes() -> None:
    value = {"b": 1, "a": [True, None]}
    assert sha256_hex(value) == hashlib.sha256(b'{"a":[true,null],"b":1}').hexdigest()


def test_inputs_hash_golden_value() -> None:
    # The envelope is part of the contract: changing it invalidates every pending approval.
    envelope = (
        b'{"inputs":{"amount_cents":4999,"payment_id":"pi_123"},"preview_artifact_hash":null}'
    )
    got = inputs_hash({"payment_id": "pi_123", "amount_cents": 4999})
    assert got == hashlib.sha256(envelope).hexdigest()


def test_inputs_hash_covers_the_preview_artifact() -> None:
    preview = "a" * 64
    inputs = {"plan": "tofu"}
    with_preview = inputs_hash(inputs, preview)
    assert with_preview != inputs_hash(inputs)
    assert with_preview != inputs_hash(inputs, "b" * 64)
    envelope = {"inputs": inputs, "preview_artifact_hash": preview}
    assert with_preview == sha256_hex(envelope)


@given(json_objects, json_objects)
def test_different_inputs_give_different_hashes(a: dict[str, Any], b: dict[str, Any]) -> None:
    if canonical_json(a) != canonical_json(b):
        assert inputs_hash(a) != inputs_hash(b)
    else:
        assert inputs_hash(a) == inputs_hash(b)


@pytest.mark.parametrize("preview", ["", "A" * 64, "a" * 63, "g" * 64, "a" * 65])
def test_inputs_hash_rejects_malformed_preview_hash(preview: str) -> None:
    with pytest.raises(CanonicalError):
        inputs_hash({"x": 1}, preview)


@pytest.mark.parametrize("inputs", [[1, 2], "x", None, 3])
def test_inputs_hash_rejects_non_objects(inputs: Any) -> None:
    with pytest.raises(CanonicalError):
        inputs_hash(inputs)


def test_inputs_hash_rejects_floats() -> None:
    with pytest.raises(CanonicalError):
        inputs_hash({"amount": 49.99})


# ------------------------------------------------------------------ idempotency keys

HASH = hashlib.sha256(b"x").hexdigest()


@pytest.mark.invariant("I4")
def test_idempotency_key_format() -> None:
    assert idempotency_key("run_01J", "refund", HASH) == f"run_01J:refund:{HASH[:12]}"


@pytest.mark.invariant("I4")
def test_retry_reuses_the_key_and_new_inputs_do_not() -> None:
    first = idempotency_key("run_1", "refund", inputs_hash({"amount_cents": 4999}))
    retry = idempotency_key("run_1", "refund", inputs_hash({"amount_cents": 4999}))
    edited = idempotency_key("run_1", "refund", inputs_hash({"amount_cents": 5000}))
    assert first == retry
    assert first != edited


@pytest.mark.invariant("I4")
@pytest.mark.parametrize(
    ("run_id", "step_id", "hash_hex"),
    [
        ("", "refund", HASH),
        ("run_1", "", HASH),
        ("run:1", "refund", HASH),  # a ':' would make "a:b"+"c" and "a"+"b:c" collide
        ("run_1", "re:fund", HASH),
        ("run_1", "refund", HASH[:12]),
        ("run_1", "refund", HASH.upper()),
    ],
)
def test_idempotency_key_rejects_ambiguous_parts(run_id: str, step_id: str, hash_hex: str) -> None:
    with pytest.raises(CanonicalError):
        idempotency_key(run_id, step_id, hash_hex)
