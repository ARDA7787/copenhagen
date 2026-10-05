"""Spike workflow: wait (up to two days) for a human decision delivered as a signal.

This is throwaway code that proves three things the real `RunPlan` depends on:

1. Pydantic models cross the Temporal boundary (inputs, signals, results) using
   `temporalio.contrib.pydantic.pydantic_data_converter`.
2. Model modules are passed through the workflow sandbox at the *worker* level
   (`with_passthrough_modules`), so the workflow file itself imports them normally.
3. A multi-day wait is just durable state: in the time-skipping test environment it takes
   milliseconds, and on the dev server it survives a restart (Spike 3).
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from spikes.models import ApprovalDecision, ApprovalRequest, Outcome

APPROVAL_TIMEOUT = timedelta(days=2)
TASK_QUEUE = "spike-approval"

# Modules whose objects cross the sandbox untouched. In Phase 3 this becomes
# ("copenhagen.core",) — pure, deterministic, side-effect-free code only.
PASSTHROUGH_MODULES = ("copenhagen.core", "spikes.models")


def sandbox_runner() -> SandboxedWorkflowRunner:
    return SandboxedWorkflowRunner(
        restrictions=SandboxRestrictions.default.with_passthrough_modules(*PASSTHROUGH_MODULES)
    )


@workflow.defn
class WaitForApproval:
    def __init__(self) -> None:
        self._decision: ApprovalDecision | None = None
        self._stale_signals = 0

    @workflow.signal
    def decide(self, decision: ApprovalDecision) -> None:
        # First decision wins; later ones are ignored (and counted) — the shape we
        # want for "stale signals are ignored" in the real approval flow.
        if self._decision is None:
            self._decision = decision
        else:
            self._stale_signals += 1

    @workflow.query
    def waiting(self) -> bool:
        return self._decision is None

    @workflow.run
    async def run(self, request: ApprovalRequest) -> Outcome:
        started = workflow.now()
        try:
            await workflow.wait_condition(
                lambda: self._decision is not None, timeout=APPROVAL_TIMEOUT
            )
        except TimeoutError:
            return Outcome(
                run_id=request.run_id,
                status="expired",
                decision=None,
                waited_seconds=(workflow.now() - started).total_seconds(),
                stale_signals=self._stale_signals,
            )
        decision = self._decision
        assert decision is not None
        return Outcome(
            run_id=request.run_id,
            status="approved" if decision.approved else "rejected",
            decision=decision,
            waited_seconds=(workflow.now() - started).total_seconds(),
            stale_signals=self._stale_signals,
        )
