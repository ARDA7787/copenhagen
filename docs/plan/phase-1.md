# Phase 1: capability registry

Demo script: `docs/demos/phase-1.md` (written before any code in this phase)

**Done when:** publishing a YAML capability makes it appear in the API and in search; a broken
file gives a clear error with line numbers; republishing the same version is refused;
`copenhagen audit verify` passes.

New tech: SQLAlchemy, Alembic, FastAPI.

## Checklist

Safety-core items (marked ★) follow the three-commit protocol: tests and walkthrough skeleton
first, then the implementation, then the finished walkthrough.

- [x] P1-01 ★ `core/canonical.py`: canonical JSON (RFC 8785 style, floats rejected), `inputs_hash`,
      idempotency key, ULID ids with prefixes; Hypothesis property tests
- [x] P1-02 ★ `core/conditions.py`: the condition operators (`eq ne lt lte gt gt in not_in
      matches is_internal_domain`) as a three-valued evaluator (true / false / unknown)
- [x] P1-03 `core/capability.py`: `CapabilitySpec` (`extra="forbid"`, frozen) and the allowed
      JSON Schema subset plus the extra keywords `sensitive` and `unit`
- [x] P1-04 `registry/rules.py`: R1–R11 and A7/C9 cross-field rules; examples must validate
      against their own schemas
- [x] P1-05 `registry/loader.py` + `copenhagen capability validate <file>`: YAML line numbers mapped
      onto Pydantic errors; golden error tests
- [x] P1-06 Settings and environment guards (`ENV=dev|test|prod`, dev-only switches refused in prod)
- [x] P1-07 Alembic baseline `0001`: `tenants`, `capabilities`, `audit_events`; role grants;
      append-only trigger; test proves the app role cannot UPDATE/DELETE/TRUNCATE (I9)
- [x] P1-08 ★ `audit/chain.py`: `append_audit_event` under an advisory lock, dedupe key,
      hash chain; `copenhagen audit verify`; concurrent-writer and tamper tests
- [x] P1-09 `registry/publish.py`: immutable versions (I8), identical republish is a no-op,
      `--second-reviewer` for high-risk classes, `capability.published` audit event
- [x] P1-10 FastAPI skeleton: `GET /v1/capabilities` (full-text search), `GET /v1/capabilities/{name}[/{version}]`, `/healthz`
- [x] P1-11 Ten example specs (five `fake.*`, Slack, Stripe, GitHub, one human-adapter spec);
      `schemas/capability.schema.json` export for editor autocompletion
- [ ] P1-12 ADR-004 contract clarifications, phase demo, walkthroughs, tag `phase-1-done`

Implementation/verification status: see [the current assessment](../assessment/milestone-a.md).
Historical demo descriptions are examples, not the product contract. Unchecked rollout,
mutation/load, live-account, documentation/tag and broader failure-injection items are
not implied complete by the implemented features above.
