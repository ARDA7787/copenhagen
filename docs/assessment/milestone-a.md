# Milestone A implementation assessment

Assessed and corrected on 6 October 2026 against `Copenhagen-PRD-v2.md`, phases 1–4.
The PRD remains the contract. This work implements a company control plane for governed
runbooks; the engine does not contain business-specific refund, onboarding, SaaS or
trading workflows.

## What the audit found

The working tree already contained a substantial implementation, but its product setup
was coupled to development seeding and mockworld. Existing tests missed operational
failures: committed decisions could be lost before delivery to Temporal; an approval
could block an independent branch; human retry could re-invoke an uncertain write;
compensation followed array order rather than dependencies; nested references could
hide invalid literals; retries lost their approval context; API keys could mint broader
scopes; high-risk HTTP publication had no authenticated review path; and installed
packages depended on repository-relative policy files. The pre-existing restart test
also failed under the Temporal time-skipping server's sticky cache. It now uses an
uncached fresh worker; a separate acceptance test covers the real Temporal server.

## Implemented product behavior and evidence

| Area | Implementation | Evidence |
|---|---|---|
| Empty company setup | `copenhagen init`, provisioned users/services, configurable roles and grants, audited budgets and kill switch; administration UI/API | Empty-workspace tests with no seeded capabilities or recipes |
| Capability registry | Immutable pinned versions, strict contracts, YAML diagnostics, catalog search, deprecation, independent high-risk publication reviews | Contract, registry, publication-review and PostgreSQL tests |
| Recipes and preview | Published parameterized plans, typed forms including objects/arrays/booleans/defaults, pinned form version, deterministic validation and origins | Form/API, nested-reference, template-injection and policy tests |
| HTTP execution | Host allow-list, no redirects, credential scoping, idempotency, JSON/form mapping, output extraction, polling and signed callbacks | Adapter contracts; real HTTP backend in the end-to-end test |
| Durable interpretation | One Temporal interpreter, up to four ready steps, independent progress, workflow-owned retries, safe verification, approval waits and human tasks | Time-skipping workflow tests, saved-history replay, real-server worker restart |
| Recovery | Retry/skip/cancel, reverse dependency compensation, independent compensation policy, explicit unresolved-outcome report | Atomic-order and inconclusive-human-retry regressions |
| Reliable delivery | Transactional outbox for starts, approvals, tasks, callbacks and recovery; idempotent machine recipe requests | API-outage/restart tests and duplicate request assertion against a real backend |
| Identity and authorization | OIDC authorization code/PKCE, verified company-domain binding, server-side sessions/CSRF, scoped hashed keys and revocation, step-up, no self-approval | API/policy tests; live Google acceptance remains external |
| Pre-approval and events | Constrained independent version signatures, revocation on new recipe/capability deprecation, service-only signed event starts, source-bound HMAC and replay protection | Pre-approval and signed-hook tests |
| Budgets and audit | Locked reservations across tenant/principal/capability/run, per-run monetary ceiling, serialized append-only audit, export and invariant checks | Real PostgreSQL concurrent-writer and restricted-role tests; end-to-end I1 check |
| Distribution | CLI, bundled policy/UI resources, Dockerfile, separate application process compose, deployment/integration guides | Wheel built and loaded outside the repository; deployment image built and non-root/read-only smoke test passed |

`make dev` now runs only the API and control worker. Domain workers have separate
configuration and credentials. It does not start mockworld or assume a list of business
domains. Development fixtures remain opt-in and production rejects fake execution and
development overrides.

## Verification record

- `make check`: lint, formatting, type checking, three import-boundary contracts,
  252 unit/policy/adapter/workflow/replay tests, and the `.pth` dependency guard passed.
- `make test-int`: all seven real-service tests passed, including the Temporal container restart.
- PostgreSQL audit tests passed: the app role cannot update/delete/truncate audit rows;
  concurrent writers produce a single verifiable chain.
- Real-service acceptance passed: a freshly initialized company, real PostgreSQL,
  real Temporal, and an independent HTTP service execute a five-step operation. Both
  application workers stop while approval is pending. Fresh workers resume and complete
  it. Repeating the API request returns the same run and there is exactly one backend write.
- The Linux deployment image builds with locked dependencies and starts its policy/API/UI in a non-root, read-only smoke test. The wheel and source distribution also build. Bundled Cedar policies and UI
  templates load from an extracted installation with the working directory outside the repo.
- Two upstream deprecation warnings remain (Authlib/Starlette's httpx integration).
  They do not cause check failures; changing that dependency integration is separate work.

The additive outbox and publication-review migrations were also applied to the local
development database. Existing data was preserved.

## Acceptance boundaries

The governed-runbook software is implemented and locally verified. **A live company
rollout has not been performed in this workspace.** Google OIDC requires the company's
OAuth client. Real Slack/vendor operations require restricted accounts and contracts
verified against those actual APIs. The sample vendor files use mock conventions and
must not be represented as certified integrations. A friendly startup running a recipe
weekly for a month is an adoption criterion, not something automated tests establish.

An independent security review, mutation-testing campaign and a
50-concurrent-run load test have not been established by the verification above.
Do not infer production certification from the local test count.

Later-phase PRD work remains later-phase work: AI planning (5), external audit anchoring
and broader crash/reconciliation hardening (6), saved-plan infrastructure adapters,
egress container enforcement and a managed secret broker (7), and enterprise hosted
multi-tenancy (9). Generic infrastructure execution fails closed until saved-artifact
execution exists. This is an operational control plane over company APIs, not a
low-latency algo execution engine.
