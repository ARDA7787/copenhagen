# Spike notes: Temporal (Phase 0)

These notes are a first draft. Please rewrite them in your own words; that is part of learning Temporal.

## Spike 1: a workflow waits for a signal (`spikes/spike_01_temporal_signal.py`)

What it proves:
- Pydantic models cross the Temporal boundary (inputs, signals, queries, results) when the client and the
  test environment use `temporalio.contrib.pydantic.pydantic_data_converter`. Timezone-aware datetimes
  round-trip intact.
- Modules we own can be passed through the workflow sandbox with
  `SandboxRestrictions.default.with_passthrough_modules(...)`. In Phase 3 this becomes
  `("copenhagen.core",)` — pure, deterministic code with no side effects.
- A two-day wait is just a durable timer. In the time-skipping test environment it takes milliseconds.
- "First decision wins": later signals are ignored and counted (`stale_signals`). `RunPlan`'s
  approvals work the same way, plus a hash check (I5).

Gotchas found:
- `WorkflowEnvironment.start_time_skipping()` downloads a `temporal-test-server` binary on first use.
  This machine could not reach that URL. `conftest.py` now looks for a local binary
  (`$TEMPORAL_TEST_SERVER`, then `.tools/temporal-test-server*/temporal-test-server`) and passes it as
  `test_server_existing_path`. `.tools/` is git-ignored; see "Setup on a new machine" below.
- macOS quarantines downloaded binaries; `xattr -d com.apple.quarantine` was needed.
- Using `wait_condition(..., timeout=...)` raises `asyncio.TimeoutError` on expiry. The workflow catches
  it and returns an `expired` outcome rather than failing.

## Spike 2: Pydantic → JSON Schema → `jsonschema` (`spikes/spike_02_pydantic_jsonschema.py`)

- `model_json_schema()` output is valid Draft 2020-12 and validates good and bad instances the
  same way Pydantic does for the cases we care about.
- Our extra keywords (`sensitive`, `unit`) are added with `json_schema_extra`. They survive export and
  `jsonschema` ignores them during validation, so the validator can still find them by walking the schema.
- Money is integer cents. A float amount is rejected by both sides.

## Spike 3: dev-server state survives a restart (`spikes/spike_03_dev_server_restart.py`)

- The dev server keeps its SQLite file at `/data/temporal.db` on the named volume `temporal-data`.
- A fresh named volume is root-owned, but the Temporal image runs as uid 1000. Without the one-shot
  `temporal-volume-init` service (`chown -R 1000:1000 /data`), the server cannot create its database and exits.
- With that fix, a workflow waiting for a signal is still `RUNNING` after `docker compose restart temporal`,
  and a signal sent afterwards completes it. This is what the Phase 3 restart demo depends on.

## Setup on a new machine

1. `make up` (needs Docker Desktop running).
2. If `temporal-test-server` cannot be downloaded, fetch the release archive for your platform by hand
   and unpack it into `.tools/`, or set `TEMPORAL_TEST_SERVER=/path/to/temporal-test-server`.
3. `make check` and `make test-int`.
