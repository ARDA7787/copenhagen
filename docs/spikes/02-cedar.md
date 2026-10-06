# Spike notes: cedarpy latency (Phase 4, P4-01)

These notes are a first draft. Please rewrite them in your own words.

## Question

ADR-001 picked `cedarpy` (in-process Cedar) for tenant ceilings, with a Cedar sidecar as plan B.
Is the in-process call fast enough to sit on every `PolicyEngine.decide()` (preview, confirm, and
re-check at execution), or do we need to move it out of process?

## Spike 4: `spikes/cedar/spike_04_cedar_latency.py`

Run with `uv run pytest spikes/cedar -s`. It uses the `refund()` capability from
`tests/policy/test_engine.py` and the shipped `policy/defaults/baseline.cedar`.

Measured on an Apple M1, Python 3.13, cedarpy 4.12.1, warm-up of 50 calls before sampling:

| Operation                         | n     | p50      | p95      | max      |
|-----------------------------------|-------|----------|----------|----------|
| `PolicyEngine()` (validate+parse) | 100   | 0.503 ms | 0.537 ms | 0.631 ms |
| `cedar("invoke", ...)`            | 5000  | 0.177 ms | 0.183 ms | 0.280 ms |
| `decide()`, allow path            | 5000  | 0.359 ms | 0.380 ms | 1.081 ms |
| `decide()`, needs_approval path   | 5000  | 0.360 ms | 0.375 ms | 0.979 ms |

What it shows:
- A full decision is about 0.36 ms, and roughly all of it is the two Cedar calls `decide()` makes
  (`invoke` and `invoke_unattended`). The Python rules in front of Cedar are negligible.
- `schema()` is rebuilt on every `cedar()` call, but it costs about 2 µs. Caching it would not
  change anything measurable, so the code stays as it is.
- Building a `PolicyEngine` is about 0.5 ms. That is cheap, but it should still happen once per
  `Service`, not per request. P4-12 adds a test that pins this.
- A sidecar would add a network hop (loopback HTTP is typically 0.1–0.5 ms on its own) without
  saving any CPU. The in-process call is the faster option.

The assert in the spike is deliberately loose (p95 under 5 ms per decision, under 100 ms per build)
so it does not flake on a busy CI runner. It guards against regressions of an order of magnitude,
not small drift.

## Verdict: go

Keep `cedarpy` in process, as ADR-001 decided. No ADR change.

## When we would switch to plan B (the ADR-001 sidecar)

- p95 for `decide()` goes above about 5 ms. That could happen with large per-tenant policy sets
  or entity hierarchies, and it is the threshold this spike asserts.
- We need to share one policy decision point with non-Python services.
- `cedarpy` falls behind upstream Cedar (it tracks Cedar releases, and the Rust core sets the
  policy language we can use), or it stops shipping wheels for the Python version we run.
- We need to hot-reload policies without restarting the API and worker processes.
