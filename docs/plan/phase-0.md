# Phase 0: setup and spikes

Demo script: [`docs/demos/phase-0.md`](../demos/phase-0.md)

**Done when:** `make up` starts everything, `make check` is green, all three spikes pass,
and `docs/walkthroughs/00-workflow-vs-activity.md` exists.

## Checklist

- [x] P0-01 `git init`, uv package (src layout), `pyproject.toml` (ruff, pyright strict on
      `core/ validator/ policy/ engine/ audit/`, pytest markers), `.gitignore`, `.env.example`
- [x] P0-02 `Makefile` (`up down dev worker Q= check test test-int e2e demo fmt migrate`);
      `make up` says so plainly when the Docker daemon is down, and waits for health checks
- [x] P0-03 `docker-compose.yml`: `postgres:17` with an init script that creates
      `copenhagen_owner` and `copenhagen_app` and the databases `copenhagen` and
      `copenhagen_test`; Temporal dev server on a named volume (UI on 8233); images pinned by digest
- [x] P0-04 `.pre-commit-config.yaml` (local hooks only: detect-private-key, `make check`),
      import-linter contracts (I2, I3, pure `core`), `scripts/check_pth.py`, first unit tests
- [x] P0-05 Spike 1: a workflow waits for a signal with `pydantic_data_converter` and sandbox
      passthrough; a 2-day wait takes milliseconds in the time-skipping env
      — `spikes/spike_01_temporal_signal.py`, notes in `docs/spikes/01-temporal.md`
- [x] P0-06 Spike 2: Pydantic model to JSON Schema (Draft 2020-12) and back via `jsonschema`,
      including the extra keywords `sensitive` and `unit` — `spikes/spike_02_pydantic_jsonschema.py`
- [x] P0-07 Spike 3 (added): a waiting workflow survives `docker compose restart temporal`,
      including volume permissions — `spikes/spike_03_dev_server_restart.py`
- [x] P0-08 Docs: ADR-001, ADR-003, `NOW.md`, `docs/later.md`, `docs/plan/phase-0..4.md`,
      `docs/demos/phase-0.md`, `docs/walkthroughs/README.md`,
      `docs/walkthroughs/00-workflow-vs-activity.md`
- [x] P0-09 Demo run-through, tag `phase-0-done`
