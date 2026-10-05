# 01 — Canonical JSON, hashes and idempotency keys (P1-01)

> Status: **skeleton** (tests-first commit). The implementation sections are filled in by the
> third commit of P1-01. Review this file and `tests/unit/core/test_canonical.py` first: together
> they are the spec.

## Purpose

`src/copenhagen/core/canonical.py` is the one place that turns a value into bytes for hashing.
Approval hashes (I5), idempotency keys (I4), the audit hash chain (I9) and published capability
versions (I8) all depend on it. If two values that mean the same thing could produce different
bytes, an approval would stop matching the step it approved. If two different values could produce
the same bytes, an approval could be reused for a step nobody approved.

## What must be true

1. **Same meaning, same bytes.** Key order, tuples vs lists, and the time zone of an aware
   datetime do not change the output.
2. **RFC 8785 rules.** Keys are sorted by UTF-16 code units (not code points). Strings escape only
   `"`, `\` and control characters; everything else is raw UTF-8. No whitespace.
3. **No floats, anywhere.** Money is integer cents (C8). A float at any depth raises
   `CanonicalError`, so `49.99` can never be hashed and silently rounded.
4. **Integers stay in the I-JSON safe range** (±2^53 − 1), so every JSON reader agrees on them.
5. **Datetimes must be aware.** They are written as ISO 8601 UTC with a `Z` suffix. A naive
   datetime is an error, never a guess.
6. **Unsupported types are errors**: bytes, sets, arbitrary objects, non-string keys, lone
   surrogates.
7. **`inputs_hash` covers the inputs plus the preview artifact hash** (C1). For infrastructure
   steps, changing the plan after approval changes the hash.
8. **The idempotency key has no attempt number** (I4): `run_id:step_id:inputs_hash[:12]`. A retry
   reuses the key, while different inputs produce a new one. Parts that would make the key ambiguous
   (empty, containing `:`, or a malformed hash) are rejected.
9. **Pure**: no clock, randomness, I/O, network or environment. `tests/unit/test_core_purity.py`
   enforces this for all of `copenhagen.core`.

## The five lines that matter

_Filled in with the implementation._

## How to break it

Each row is a mutation, plus the test that must catch it. _Filled in with the implementation;_
the draft rows are:

| Mutation | Caught by |
|---|---|
| Sort keys by code point (`sorted(d)`) | `test_keys_sort_by_utf16_code_units` |
| Allow floats that are whole numbers (`1.0`) | `test_a_float_anywhere_is_rejected` |
| Drop `preview_artifact_hash` from the envelope | `test_inputs_hash_covers_the_preview_artifact`, golden value |
| Add the attempt number to the key | `test_retry_reuses_the_key_and_new_inputs_do_not` |
| Use `ensure_ascii=True` | `test_string_escaping` |
| Treat naive datetimes as UTC | `test_rejects[naive]` |

## Review checklist

- Do the RFC 8785 vectors in the tests match the RFC text?
- Is there any way to get a float, a naive datetime or a non-string key past the encoder?
- Does any code outside `core/canonical.py` build hash input bytes on its own?
