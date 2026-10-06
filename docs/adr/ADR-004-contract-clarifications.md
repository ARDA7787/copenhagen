# ADR-004: Milestone A contract clarifications

Status: accepted, 6 October 2026.

- Runtime side effects require policy on resolved inputs and a signed authorization.
  Caller-supplied source labels are not trusted. Approval edits are schema-checked,
  rehashed, and re-evaluated before invocation.
- HTTP contracts may specify query/path inputs, body encoding, field renaming, output
  projection, static non-secret headers and credential-header shape. Backend URLs and
  vendor tokens remain deployment configuration. They are never inferred from examples.
- An omitted verification input map supplies the idempotency key. An explicit empty map
  calls a zero-input verifier. Verification must provide the declared outputs before a
  step can count as succeeded; a proven effect with missing outputs requires attention.
- An asynchronous 202 result contains an opaque job ID. Polls use the same capability's
  backend and credential. Signed callbacks bind run, step and job and enter the same
  durable outbox. Their outputs still pass the configured verification capability.
- Regex inputs/conditions use a bounded subset: literals, character classes, anchors,
  simple alternatives, and at most one repetition. Groups, backreferences and compound
  repetitions are refused to prevent catastrophic backtracking. More elaborate checks
  belong in reviewed Cedar policies or the authoritative backend.
- Recipe forms support scalar, boolean, array and object parameters, defaults, and
  version pinning. Defaults are applied before pre-approval constraints are evaluated.
- An uncertain external outcome remains visible after cancellation, skip or failed
  compensation. Human retry cannot bypass inconclusive verification.
- Infrastructure execution requiring a saved plan remains fail-closed until the Phase 7
  adapter exists. Ordinary company API identity/operational capabilities work now.
