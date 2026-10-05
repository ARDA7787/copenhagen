# ADR-003 — The workflow owns retries

- **Status:** accepted
- **Date:** 2026-10-05
- **Note:** ADR-002 is reserved for the PRD's Phase 3 decision (Temporal or DBOS).

## Context

Temporal retries activities on its own by default. For a domain activity that writes to a vendor,
a blind retry after a timeout can repeat a side effect (a second refund). The PRD (I11) requires
that a retry only happens after verification proves the first attempt did not land.

## Decision

- **Domain activities** (`invoke_capability`, `verify_capability`, `preview_capability`) run with
  `RetryPolicy(maximum_attempts=1)`. They report a typed error instead of retrying:
  `retryable | not_retryable | unknown_outcome | needs_human`.
- **The `RunPlan` workflow** decides what to do with that error: back off and retry, verify first,
  stop for a human, or fail the step. Every decision is recorded in the audit log.
- **Control activities** (policy evaluation, audit writes, approvals) are idempotent by key and keep
  Temporal's normal retries.
- A start-to-close timeout on a write is `unknown_outcome`; a schedule-to-start timeout is
  `retryable` (the call provably never started).

## Consequences

- Retry behaviour is visible in one place (the workflow) and covered by workflow tests.
- Every write must carry an idempotency key `run_id:step_id:inputs_hash[:12]` with no attempt
  number, so a deliberate retry reuses it.
