# 02 — Structured conditions (P1-02)

Status: **skeleton** (tests written, implementation pending).
Code: `src/copenhagen/core/conditions.py`. Spec: `tests/unit/core/test_conditions.py`.

## 1. Purpose

A capability spec says when a step may run without a human, for example
`approval.unattended_when: [{parameter: amount_cents, op: lte, value: 20000}]`. This module is
the whole condition language (PRD v2 §4.1, decision 10): ten operators, no expression strings,
no parser. It evaluates a list of conditions against a step's inputs and answers TRUE, FALSE or
UNKNOWN.

## 2. Invariants enforced

- **PRD §4.5 step 4:** an input that is still a `${ref}` gives `unknown_until_runtime`; any
  condition that is false gives `needs_approval`. This module produces the UNKNOWN and the FALSE.
- **Fail closed:** UNKNOWN is never "yes". `Truth` refuses `bool()` so `if evaluate(...)` cannot
  silently treat UNKNOWN as true.
- **C8 (money is integer cents):** floats are refused in specs and never compared at run time.
- **Determinism (core):** pure, no clock, no I/O; safe in the Temporal workflow sandbox.

## 3. The five lines that matter

_(filled in after the implementation commit)_

## 4. How to break it

_(filled in after the implementation commit)_

| Mutation | Caught by |
|---|---|

## 5. Review checklist

- Can any input value make `evaluate` raise? (It must not: the property test feeds it anything.)
- Is there any path where a wrong-kind input gives TRUE?
- Does `matches` use a full match?
- Could a lookalike or subdomain email count as internal?
