# Phase 2: adapters and single calls

Demo script: `docs/demos/phase-2.md` (written before any code in this phase)

**Done when:** `copenhagen call comms.post_slack_message@1 --input channel=general …` lands in
mockworld's Slack view and in the audit log; an injected `commit_then_drop` produces
`unknown_outcome`, then verification finds the effect; a request to a host outside
`allowed_hosts` is refused before any network I/O.

New tech: httpx, respx.

## Checklist

- [ ] P2-01 `core/calls.py` (`CapabilityCall`, `InvokeResult`, preview, credential) and the
      `adapters/base.py` protocol; one shared wrapper enforces A3, A5 and A6 for every adapter
- [ ] P2-02 `adapters/fake.py`: deterministic, scriptable outcomes, SQLite-backed so it survives
      a crash, can inject every error type
- [ ] P2-03 `secrets/store.py` (`EnvSecretStore`) and `secrets/broker.py` (credential checked
      against the executor queue); structlog redaction; a test proves no secret reaches the logs
- [ ] P2-04 ★ `adapters/http.py`: request mapping (path, query, body), `Idempotency-Key` and
      `X-Copenhagen-Run` headers, A3 timeouts, A7 methods, A8 allow-list on every hop, no redirects
- [ ] P2-05 ★ `adapters/errors.py`: the error-mapping table (C11) and output validation (A5),
      as a table-driven test matrix
- [ ] P2-06 `webhook_callback` mode: 202 returns a job handle, `poll()`; HMAC helper
- [ ] P2-07 `config/backends.yaml` and the dev-only host override (refused when `ENV=prod`)
- [ ] P2-08 mockworld: Stripe, Shop, Slack, Google Directory, GitHub, email, payroll; world view
- [ ] P2-09 mockworld fault injection (`fail_next`, `timeout`, `rate_limit`, `commit_then_drop`)
- [ ] P2-10 Adapter contract suite run against `fake` and `http` (respx)
- [ ] P2-11 `copenhagen call` (dev-only): validate, policy `dev-direct`, invoke, verify, audit
- [ ] P2-12 Phase demo, walkthroughs (adapters, error mapping), tag `phase-2-done`
