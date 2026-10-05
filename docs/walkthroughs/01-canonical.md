# 01 — Canonical JSON, hashes and idempotency keys (P1-01)

> Status: **done** (P1-01). Spec: this file plus `tests/unit/core/test_canonical.py`.
> Code: `src/copenhagen/core/canonical.py`.

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

Line numbers are for `src/copenhagen/core/canonical.py` at the P1-01 commit.

1. **`keys = sorted(items, key=_utf16_units)`** (l.140). `_utf16_units` encodes the key as
   UTF-16-BE, so comparing bytes compares UTF-16 code units. This is the only place RFC 8785 and
   Python's default `sorted()` disagree: a character above U+FFFF (stored as a D800–DBFF surrogate
   pair) sorts *before* U+E000–U+FFFF.
2. **`elif isinstance(value, float): raise CanonicalError(...)`** (l.121). It comes after the `int`
   and `bool` branches, because `bool` is an `int`. No float ever reaches the output, not even `1.0`.
3. **`if not -MAX_SAFE_INT <= value <= MAX_SAFE_INT`** (l.105). Any reader that parses numbers as
   doubles would silently round a larger integer.
4. **`return sha256_hex({"inputs": inputs, "preview_artifact_hash": preview_artifact_hash})`**
   (l.74). The envelope is part of the contract: `"preview_artifact_hash": null` is always present,
   so adding a preview later changes every hash. The golden test pins the exact bytes.
5. **`return f"{run_id}:{step_id}:{inputs_hash_hex[:_KEY_HASH_CHARS]}"`** (l.85). No attempt
   number. `:` is banned in `run_id` and `step_id` so the key can't be read two ways.

Strings go through `json.dumps(..., ensure_ascii=False)` (l.167), which escapes exactly `"`, `\`
and U+0000–U+001F. It uses the short forms where they exist and lowercase `\u00xx` otherwise,
which is what RFC 8785 section 3.2.2.2 asks for. Datetimes are converted to UTC and written with `Z` (l.173).

## How to break it

Each row is a mutation, plus a test that catches it. Every row was applied to the code by hand
and the test run, at the P1-01 commit: all were caught. `mutmut` will repeat this
systematically at the end of Phase 4.

| Mutation | Caught by |
|---|---|
| Sort keys by code point (`sorted(items)`) | `test_keys_sort_by_utf16_code_units` |
| Allow whole-number floats (`1.0` → `1`) | `test_rejects[whole-float]`, `test_a_float_anywhere_is_rejected` |
| Drop `preview_artifact_hash` from the envelope | `test_inputs_hash_golden_value`, `test_inputs_hash_covers_the_preview_artifact` |
| Use `ensure_ascii=True` | `test_string_escaping`, `test_non_ascii_is_raw_utf8` |
| Treat naive datetimes as UTC | `test_rejects[naive]` |
| Keep the original offset (`value.isoformat()`) | `test_aware_datetime_is_utc_with_z`, `test_same_instant_in_any_zone_is_the_same` |
| Allow `date` (write it as a string) | `test_rejects[date]` |
| Remove the integer range check | `test_rejects[int-too-big]`, `test_rejects[int-too-small]` |
| Remove the `:` check on key parts | `test_idempotency_key_rejects_ambiguous_parts` |
| Add the attempt number to the key | `test_retry_reuses_the_key_and_new_inputs_do_not` |

### Something Hypothesis found

The first run of `test_matches_stdlib_for_ascii_keys` (stdlib `json.dumps` as the reference) failed:
the strategy made the *top-level* keys ASCII, but nested objects could have any key.
Hypothesis found `{"": {"\U000xxxxx": ..., "\ue000": ...}}`, where UTF-16 and code-point order
really differ. The implementation was right and the reference was wrong. The strategy now makes keys
ASCII at every depth, and the UTF-16 rule itself is pinned by the RFC vector test.

## Review checklist

- Do the RFC 8785 vectors in the tests match the RFC text?
- Is there any way to get a float, a naive datetime or a non-string key past the encoder?
- Does any code outside `core/canonical.py` build hash input bytes on its own?
- Do the `pyright: ignore[reportUnnecessaryIsInstance]` guards in `inputs_hash` and
  `idempotency_key` still make sense? They are there because values reach these functions from
  untyped JSON.
