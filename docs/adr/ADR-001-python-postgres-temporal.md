# ADR-001 — Python, Postgres and Temporal

- **Status:** accepted (carried from PRD v1 §7–8; reaffirmed by PRD v2 §2)
- **Date:** 2026-10-05

## Context

Copenhagen turns a request into a checked plan and runs it across tools a company already uses.
Runs can wait days for a human approval, must survive restarts, and must never repeat a side
effect by accident. The safety core (validator, policy, hashing, audit chain) must be small,
typed and testable as pure functions.

## Decision

1. **Python 3.13** everywhere, one language. Pydantic v2 models are the contracts; JSON Schema is
   generated from them. Python 3.14 later, once every dependency ships wheels.
2. **Postgres 17** for the control plane: registry, plans, runs, approvals, the audit log.
   Full-text search for capability lookup. No vector database before Phase 8.
3. **Temporal** as the durable runtime, behind a `RunEngine` protocol so DBOS stays a cheap
   alternative until the Phase 3 retrospective (ADR-002).
4. **Policy:** `cedarpy` for tenant ceilings, with a pure `PolicyEngine.decide()` in front of it
   (sidecar fallback = plan B).
5. **UI:** FastAPI + Jinja2 + HTMX, vendored. No SPA.

## Consequences

- One `pyproject.toml`, one lockfile, one Makefile.
- Workflow code must be deterministic; Pydantic models cross the Temporal sandbox via
  `pydantic_data_converter` plus a passthrough of `copenhagen.core` (Spike 1).
- Two Docker services are the only infrastructure for the first four phases.
