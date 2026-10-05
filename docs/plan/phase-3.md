# Phase 3: plans, recipes, durable runs, approvals

Demo script: `docs/demos/phase-3.md` (written before any code in this phase)

**Done when:** a five-step recipe run waits for your click, survives a restart of every
container, then completes. A step failure in `continue` mode leaves the run in
`needs_attention`. Each attempt is audited. The `unknown_outcome` refund is verified, not
retried blindly. The phase retrospective and ADR-002 (Temporal vs DBOS) are written.

New tech: Jinja2 + HTMX, Temporal workers for real.

## Checklist

**Plan IR, validator and policy**
- [ ] P3-01 ★ `core/plan.py`: `PlanIR`, the `${step.outputs.x}` reference parser, JSON Schema export
- [ ] P3-02 `core/recipe.py`: `RecipeSpec`, the `{{param}}` renderer, `instantiate()`, source labels
- [ ] P3-03 `registry/recipes.py`: recipe publish, validate and list
- [ ] P3-04 ★ Validator checks 1–4 (schema, capabilities pinned, static policy, literal schemas and refs)
- [ ] P3-05 ★ Validator checks 5–7 (source labels, static budgets, irreversible-before-risky warning)
- [ ] P3-06 ★ `policy/engine.py`: `decide()` for this phase (kill switch, role gate,
      risk defaults, spec conditions, taint); allow/deny matrix in YAML
- [ ] P3-07 Preview builder (blocked steps say "requires role X (ask Y)")

**Engine**
- [ ] P3-08 Migration `0002`: principals, roles, credential_refs, recipes, intents, plans, runs, step_runs, approvals, human_tasks
- [ ] P3-09 ★ Pure functions: `resolve_refs` (with taint), `ready_batches`, step status logic, backoff
- [ ] P3-10 Control activities (`evaluate_step_policy`, `request_approval`, `create_human_task`,
      `record`) and domain activities (`invoke_capability`, `verify_capability`, `preview_capability`)
- [ ] P3-11 `RunEngine` interface and `RunPlan` skeleton in Temporal
- [ ] P3-12 Approvals inside the workflow: expiry, hash check (I5), stale signals ignored
- [ ] P3-13 Retries driven by error type; verification within its window (I11, C14)
- [ ] P3-14 `continue` mode with retry, skip and cancel signals
- [ ] P3-15 `atomic` mode, compensation in reverse order, "cannot undo" report, cancel semantics (I10)
- [ ] P3-16 `human` adapter and human tasks
- [ ] P3-17 Domain worker process (no `DATABASE_URL`, one queue)

**Surfaces**
- [ ] P3-18 API: plans, recipe runs, runs, approvals, tasks; dev login (persona picker)
- [ ] P3-19 HTMX: recipe form from the parameter schema, preview page, run page, approval inbox, task inbox
- [ ] P3-20 CLI: `run recipe --param k=v`, `plan validate|submit`, `runs …`

**Proof**
- [ ] P3-21 Workflow scenario suite in the time-skipping env; replay tests from committed histories
- [ ] P3-22 Invariant tests I1, I4, I5, I10, I11; `copenhagen audit check`
- [ ] P3-23 End-to-end: restart everything while waiting; kill a domain worker mid-call
- [ ] P3-24 Retrospective `docs/retro/phase-3.md`, ADR-002, walkthroughs, tag `phase-3-done`
