# Phase 4: identity, roles, policy, pre-approval → Milestone A

Demo script: `docs/demos/phase-4.md` and `docs/demos/milestone-a.md` (written before any code)

**Done when:**
- Omar (not finance) is blocked from refund 1182: "requires role finance — ask Leela".
- Leela's $300 refund waits for Sam; she cannot approve her own.
- A $7,500 refund is denied by the Cedar ceiling.
- Priya pre-approves onboarding: Google, Slack and GitHub run unattended; AWS (Omar) and payroll (Leela) wait.
- A signed webhook can only start a pre-approved recipe that allows event triggers.

New tech: cedarpy, Authlib, Keycloak (opt-in profile).

## Checklist

- [x] P4-01 One-day cedarpy spike (`spikes/cedar/`): go, or fall back to the sidecar (plan B) — **go**, see `docs/spikes/02-cedar.md`
- [x] P4-02 Migration `0003`: `api_keys`, `sessions`, `recipe_preapprovals`, `usage_counters`;
      principals and roles move to Postgres; `copenhagen dev seed`
- [ ] P4-03 OIDC with Authlib (Keycloak profile; Google documented); server-side sessions
- [x] P4-04 API keys `cph_live_…` stored as a sha256 hash, with scopes and expiry, audited
- [x] P4-05 Role administration; the role gate reads from the database
- [x] P4-06 ★ Cedar schema, baseline and example policies; PolicySet built once; `policy_version`
- [x] P4-07 ★ Budgets: reserve, commit and release; static money check
- [x] P4-08 ★ Pre-approval backend: eligibility, `constraints_hash`, voiding, audit events
- [x] P4-09 Pre-approval UI and API
- [x] P4-10 Separation of duties hardening; step-up re-authentication for high-risk approvals
- [x] P4-11 `POST /v1/hooks/{source}`: HMAC, timestamp window, nonce, service principal (I12)
- [ ] P4-12 Full policy matrix; I6, I7, I12 tests; performance smoke tests
- [ ] P4-13 End-to-end test for each done-when scenario
- [ ] P4-14 Mutation-testing pass on `policy/` and `validator/`
- [ ] P4-15 `make demo` from a clean clone, `docs/demos/milestone-a.md`, guides, tag `milestone-a`

Implementation/verification status: see [the current assessment](../assessment/milestone-a.md).
Historical demo descriptions are examples, not the product contract. Unchecked rollout,
mutation/load, live-account, documentation/tag and broader failure-injection items are
not implied complete by the implemented features above.
