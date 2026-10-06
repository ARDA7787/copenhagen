"""Spike 4 — is cedarpy fast enough to sit on every policy decision?

Measures the three costs the engine pays: building a `PolicyEngine` (validate + parse the
baseline policy), a raw `cedar()` authorization call, and a full `decide()`. Run with `-s`
to see the numbers. The assert is deliberately loose (p95 under 5 ms) so it survives CI noise;
the real verdict lives in docs/spikes/02-cedar.md.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable

from tests.policy.test_engine import refund

from copenhagen.core.identity import Principal, RunContext
from copenhagen.policy.engine import PolicyEngine

PRINCIPAL = Principal(
    id="leela",
    tenant_id="demo",
    email="leela@example.com",
    roles=("finance",),
    patterns=("payments.*",),
)
CONTEXT = {
    "role_permitted": True,
    "same_tenant": True,
    "enabled": True,
    "amount_cents": 10000,
    "money_ceiling": 500000,
    "risk_class": "financial",
    "approver_allowed": False,
}


def sample(fn: Callable[[], object], n: int, warmup: int = 50) -> list[float]:
    for _ in range(warmup):
        fn()
    out = []
    for _ in range(n):
        start = time.perf_counter()
        fn()
        out.append((time.perf_counter() - start) * 1000)
    return out


def report(label: str, ms: list[float]) -> tuple[float, float]:
    ms = sorted(ms)
    p50 = statistics.median(ms)
    p95 = ms[int(len(ms) * 0.95) - 1]
    print(f"{label:<28} n={len(ms):>5}  p50={p50:7.3f} ms  p95={p95:7.3f} ms  max={ms[-1]:7.3f} ms")
    return p50, p95


def test_cedar_latency() -> None:
    _, build_p95 = report("PolicyEngine() build", sample(PolicyEngine, 100, warmup=5))

    engine = PolicyEngine()
    cap = refund()
    _, cedar_p95 = report(
        "cedar('invoke')",
        sample(lambda: engine.cedar("invoke", "leela", cap.name, CONTEXT), 5000),
    )
    _, decide_p95 = report(
        "decide() allow path",
        sample(
            lambda: engine.decide(
                PRINCIPAL, cap, {"amount_cents": 10000}, {"amount_cents": "user"}, RunContext()
            ),
            5000,
        ),
    )
    report(
        "decide() needs_approval",
        sample(
            lambda: engine.decide(
                PRINCIPAL, cap, {"amount_cents": 30000}, {"amount_cents": "user"}, RunContext()
            ),
            5000,
        ),
    )
    assert cedar_p95 < 5
    assert decide_p95 < 5
    assert build_p95 < 100
