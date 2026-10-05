# 00 — Workflow vs activity

## Purpose

Copenhagen runs every plan as one Temporal **workflow** (`RunPlan`). Everything that touches the
outside world happens in **activities**. That split is how runs survive crashes, restarts and
two-day approval waits without doing anything twice. You need this distinction to review Phase 3.

## The two kinds of code

| | Workflow (`RunPlan`) | Activity |
|---|---|---|
| What it does | Decides what happens next: ordering, waiting, retries, compensation | Does one piece of I/O: call a vendor, write to Postgres, evaluate policy |
| Must be | Deterministic: same history in, same commands out | Idempotent: safe to run more than once |
| May use | Its inputs, signal values, activity results, `workflow.now()`, timers | Network, database, clock, randomness, secrets (domain workers only) |
| Must not use | Network, database, files, `datetime.now()`, `random`, threads, env vars | Workflow state (it only gets its arguments) |
| On a crash | **Replayed**: Temporal re-runs the code against recorded history; results come from history, not by re-calling activities | **Retried** according to its retry policy, or reported as failed |
| Lives in | `engine/workflow.py` | `engine/control_activities.py`, `engine/domain_activities.py` |

## Why replay matters

When a worker restarts, Temporal does not resume a paused Python process. It runs the workflow code
from the start and feeds it the recorded history: "activity X returned Y", "signal Z arrived". If the
code makes a different decision the second time, for example because it read the clock directly,
replay fails with a non-determinism error. That is why:

- the clock comes from `workflow.now()` and waits use `workflow.wait_condition` / `workflow.sleep`;
- the workflow only imports pure modules (`copenhagen.core`), passed through the sandbox (Spike 1);
- changes to `RunPlan` that alter decisions for in-flight runs need `workflow.patched()`, and replay tests
  against committed histories catch the mistake in CI (Phase 3).

## How this maps to Copenhagen's rules

- **Domain activities have `maximum_attempts=1` (ADR-003, I11).** A Temporal-level retry of a
  vendor write could charge a card twice. The activity reports a typed error instead
  (`retryable | not_retryable | unknown_outcome | needs_human`), and the workflow decides: back off and retry,
  verify first, or hand it to a human.
- **Control activities keep normal retries.** They are idempotent by construction (the audit
  `event_key` dedupes, policy evaluation is pure).
- **Approvals are signals.** The workflow waits on a durable timer. The approval is checked against
  `inputs_hash` (I5), and stale or duplicate signals are ignored (Spike 1).
- **Timeouts have meanings.** Schedule-to-start expiry means the call never started, so it is
  `retryable`. Start-to-close expiry on a write means it may have happened, so it is `unknown_outcome`
  and the step is verified before anything else.

## Five lines that matter (Phase 0 spike)

1. `spikes/approval_workflow.py`: `with_passthrough_modules(*PASSTHROUGH_MODULES)` is the only way
   our models enter the sandbox.
2. `spikes/approval_workflow.py`: `workflow.wait_condition(..., timeout=APPROVAL_TIMEOUT)` is a durable
   timer, not a sleeping thread.
3. `spikes/approval_workflow.py`: the "first decision wins" guard in the `decide` signal handler.
4. `conftest.py`: `data_converter=pydantic_data_converter` must match on client and worker.
5. `docker-compose.yml`: `--db-filename /data/temporal.db` on a named volume, plus the volume `chown`.

## How to break it

| Mutation | What catches it |
|---|---|
| Use `datetime.now()` instead of `workflow.now()` in the workflow | The sandbox raises at run time (spike 1 fails) |
| Drop `pydantic_data_converter` from the client | Spike 1 fails: the result comes back as a `dict` |
| Accept every signal (remove the first-decision guard) | `test_signal_after_one_day_completes` fails (it asserts `stale_signals == 1`) |
| Remove the `temporal-volume-init` service | Temporal exits on a fresh volume; `make up` never gets healthy |
| Drop `--db-filename` | Spike 3 fails: the workflow is gone after restart |

## Review checklist

- Does any workflow code read the clock, environment, network, files or randomness directly?
- Does every domain activity have `maximum_attempts=1`, and does every write carry an idempotency key?
- Is every non-retryable situation mapped to an error type the workflow handles explicitly?
