"""One deterministic interpreter for every business runbook. All I/O is in activities."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, TimeoutType
from temporalio.exceptions import TimeoutError as TemporalTimeoutError

with workflow.unsafe.imports_passed_through():
    from copenhagen.core.calls import CapabilityCall, InvokeResult
    from copenhagen.core.canonical import idempotency_key, inputs_hash
    from copenhagen.core.capability import CapabilitySpec, Source, duration_seconds
    from copenhagen.core.conditions import Truth, evaluate
    from copenhagen.core.plan import REF, Step, backoff, ready_batches, resolve_refs, walk_values
    from copenhagen.core.schema import validate_values
    from copenhagen.engine.contracts import ControlArgs, ControlResult, RunEnvelope


@workflow.defn
class RunPlan:
    def __init__(self) -> None:
        self.approvals: dict[str, str] = {}
        self.actions: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, str] = {}
        self.cancel_requested = False
        self.compensation_requested = False
        self.statuses: dict[str, str] = {}
        self.outputs: dict[str, dict[str, Any]] = {}
        self.origins: dict[str, bool] = {}
        self.generations: dict[str, int] = {}
        self.envelope: RunEnvelope | None = None
        self.completed_order: list[str] = []
        self.seen_actions: set[str] = set()
        self.attention_count: dict[str, int] = {}
        self.callbacks: dict[str, dict[str, Any]] = {}
        self.uncertain_steps: set[str] = set()

    @workflow.signal
    def backend_completed(self, result: dict[str, Any]) -> None:
        self.callbacks[result["step_id"]] = result

    @workflow.signal
    def approval_decided(self, decision: dict[str, str]) -> None:
        self.approvals[decision["step_id"]] = decision["id"]

    @workflow.signal
    def step_action(self, action: dict[str, Any]) -> None:
        if action.get("id"):
            if action["id"] in self.seen_actions:
                return
            self.seen_actions.add(action["id"])
        self.actions[action["step_id"]] = action

    @workflow.signal
    def task_completed(self, task: dict[str, str]) -> None:
        self.tasks[task["step_id"]] = task["id"]

    @workflow.signal
    def cancel(self, compensate: bool = False) -> None:
        self.cancel_requested = True
        self.compensation_requested = compensate

    @workflow.query
    def state(self) -> dict[str, Any]:
        return {"steps": self.statuses, "cancel_requested": self.cancel_requested}

    async def control(
        self,
        step: str,
        action: str,
        *,
        inputs: dict[str, Any] | None = None,
        sources: dict[str, Source] | None = None,
        data: dict[str, Any] | None = None,
        attempt: int = 1,
    ) -> ControlResult:
        assert self.envelope is not None
        return await workflow.execute_activity(
            "control",
            ControlArgs(
                tenant_id=self.envelope.tenant_id,
                run_id=self.envelope.run_id,
                step_id=step,
                action=action,
                inputs=inputs or {},
                sources=sources or {},
                data=data or {},
                attempt=attempt,
            ),
            task_queue="control",
            start_to_close_timeout=timedelta(seconds=30),
            result_type=ControlResult,
        )

    async def record(self, step: str, event: str, **data: Any) -> None:
        assert self.envelope is not None
        generation = self.generations.get(step, 1)
        suffix = data.pop("suffix", "")
        await self.control(
            step,
            "record",
            data={
                "event": event,
                "event_id": f"{self.envelope.run_id}:{step}:{event}:{generation}:{suffix}",
                **data,
            },
            attempt=generation,
        )

    @workflow.run
    async def run(self, envelope: RunEnvelope) -> dict[str, Any]:
        self.envelope = envelope
        await self.record("", "run.started", run_status="running")
        # Keep the original interpreter available for histories created before this change.
        if workflow.patched("independent-ready-steps-v1"):
            return await self.run_ready(envelope)
        while len(self.statuses) < len(envelope.plan.steps) and not self.cancel_requested:
            batch = ready_batches(envelope.plan.steps, self.statuses)
            if not batch:
                for step in envelope.plan.steps:
                    if step.id not in self.statuses:
                        self.statuses[step.id] = "skipped"
                        await self.record(
                            step.id,
                            "step.skipped",
                            status="skipped",
                            reason="dependency did not succeed",
                        )
                break
            results = await asyncio.gather(
                *(self.execute_step(step, envelope.caps[step.id]) for step in batch)
            )
            for step, result in zip(batch, results, strict=True):
                self.statuses[step.id] = result["status"]
                if result["status"] == "succeeded":
                    self.outputs[step.id] = result["outputs"]
            if envelope.plan.on_failure == "atomic" and any(
                r["status"] == "failed" for r in results
            ):
                return await self.compensate(envelope)
        if self.cancel_requested:
            if self.compensation_requested:
                return await self.compensate(envelope)
            return await self.finish("cancelled")
        statuses = set(self.statuses.values())
        return await self.finish(
            "needs_attention"
            if "failed" in statuses
            else "succeeded_with_skips"
            if "skipped" in statuses
            else "succeeded"
        )

    async def run_ready(self, envelope: RunEnvelope) -> dict[str, Any]:
        running: dict[str, asyncio.Task[dict[str, Any]]] = {}
        atomic_failure = False
        while len(self.statuses) < len(envelope.plan.steps):
            for step in envelope.plan.steps:
                if step.id in self.statuses or step.id in running:
                    continue
                if any(
                    self.statuses.get(d) in {"failed", "skipped", "cancelled"}
                    for d in step.depends_on
                ):
                    self.statuses[step.id] = "skipped"
                    await self.record(
                        step.id,
                        "step.skipped",
                        status="skipped",
                        reason="dependency did not succeed",
                    )
                elif (
                    not self.cancel_requested
                    and not atomic_failure
                    and len(running) < 4
                    and all(self.statuses.get(d) == "succeeded" for d in step.depends_on)
                ):
                    running[step.id] = asyncio.create_task(
                        self.execute_step(step, envelope.caps[step.id])
                    )
            if not running:
                break
            await workflow.wait_condition(lambda: any(task.done() for task in running.values()))
            # Iterate in plan order, never in the nondeterministic order of an asyncio set.
            for step_id in list(running):
                task = running[step_id]
                if not task.done():
                    continue
                result = await task
                del running[step_id]
                self.statuses[step_id] = result["status"]
                if result["status"] == "succeeded":
                    self.outputs[step_id] = result["outputs"]
                    self.completed_order.append(step_id)
                if envelope.plan.on_failure == "atomic" and result["status"] == "failed":
                    atomic_failure = True
                    self.cancel_requested = True
        if atomic_failure or self.compensation_requested:
            return await self.compensate(envelope)
        if self.cancel_requested:
            return await self.finish("cancelled")
        statuses = set(self.statuses.values())
        return await self.finish(
            "needs_attention"
            if "failed" in statuses
            else "succeeded_with_skips"
            if "skipped" in statuses
            else "succeeded"
        )

    async def execute_step(
        self, step: Step, cap: CapabilitySpec, *, subcall: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        assert self.envelope is not None
        try:
            values = resolve_refs(step.inputs, self.outputs)
        except ValueError:
            await self.record(
                step.id, "step.skipped", status="skipped", reason="dependency output unavailable"
            )
            return {"status": "skipped"}
        origins: dict[str, Source] = dict(self.envelope.sources.get(step.id, step.sources))
        for key, value in step.inputs.items():
            for leaf in walk_values(value):
                if (
                    isinstance(leaf, str)
                    and (match := REF.fullmatch(leaf))
                    and self.origins.get(match[1], False)
                ):
                    origins[key] = "untrusted_output"
        self.origins[step.id] = cap.data.output_trust == "untrusted" or any(
            v in {"ai", "untrusted_output"} for v in origins.values()
        )
        approval_id: str | None = None
        generation = 0
        while not self.cancel_requested:
            generation += 1
            self.generations[step.id] = generation
            h = inputs_hash(values)
            await self.record(
                step.id, "step.resolved", inputs_hash=h, sources=origins, capability=cap.ref
            )
            policy_data = {**(subcall or {}), "approval_id": approval_id}
            checked = await self.control(
                step.id,
                "policy",
                inputs=values,
                sources=origins,
                data=policy_data,
                attempt=generation,
            )
            assert checked.decision is not None
            if checked.data.get("capability"):
                cap = CapabilitySpec.model_validate(checked.data["capability"])
            if checked.decision.outcome == "needs_approval":
                requested = await self.control(
                    step.id,
                    "approval",
                    inputs=values,
                    sources=origins,
                    data={**(subcall or {}), "roles": list(checked.decision.approver_roles)},
                    attempt=generation,
                )
                expected_id = requested.id
                try:
                    await workflow.wait_condition(
                        lambda expected_id=expected_id: (
                            self.approvals.get(step.id) == expected_id or self.cancel_requested
                        ),
                        timeout=timedelta(seconds=duration_seconds(cap.approval.expires_after)),
                    )
                except TimeoutError:
                    await self.record(
                        step.id, "approval.expired", inputs_hash=h, status="needs_attention"
                    )
                    if cap.approval.on_expiry == "fail_step":
                        return {"status": "failed"}
                    action = await self.attention(step.id, "approval expired")
                    if action != "retry":
                        return {"status": "skipped" if action == "skip" else "cancelled"}
                    approval_id = None
                    continue
                if self.cancel_requested:
                    return {"status": "cancelled"}
                result = await self.control(step.id, "approval_result", data={"id": expected_id})
                if result.data["status"] != "approved":
                    if self.envelope.plan.on_failure == "atomic":
                        return {"status": "failed"}
                    action = await self.attention(step.id, "approval rejected")
                    if action == "retry":
                        continue
                    return {"status": "skipped" if action == "skip" else "cancelled"}
                values = result.data["inputs"]
                origins = result.data["sources"]
                approval_id = expected_id
                # Re-check policy and approval hash with the real edited values.
                checked = await self.control(
                    step.id,
                    "policy",
                    inputs=values,
                    sources=origins,
                    data={**(subcall or {}), "approval_id": approval_id},
                    attempt=generation,
                )
                assert checked.decision is not None
                h = inputs_hash(values)
            if checked.decision.outcome != "allow":
                await self.record(
                    step.id,
                    "step.denied",
                    inputs_hash=h,
                    status="failed",
                    reason="; ".join(checked.decision.reasons),
                )
                if self.envelope.plan.on_failure == "atomic":
                    return {"status": "failed"}
                action = await self.attention(step.id, "; ".join(checked.decision.reasons))
                if action == "retry":
                    approval_id = None
                    continue
                return {"status": "skipped" if action == "skip" else "cancelled"}
            if cap.risk.class_ == "infrastructure":
                # A saved-plan infrastructure adapter arrives in phase 7. Fail closed now.
                await self.record(
                    step.id,
                    "step.failed",
                    inputs_hash=h,
                    status="failed",
                    reason="saved infrastructure artifact adapter required",
                )
                return {"status": "failed"}
            call = CapabilityCall(
                tenant_id=self.envelope.tenant_id,
                run_id=self.envelope.run_id,
                step_id=step.id,
                capability=cap,
                inputs=values,
                inputs_hash=h,
                idempotency_key=idempotency_key(self.envelope.run_id, step.id, h),
                authorization=checked.authorization,
            )
            result = await self.invoke_with_retries(
                call, origins, policy_data | {"approval_id": approval_id}
            )
            if result.ok:
                self.uncertain_steps.discard(step.id)
                await self.record(
                    step.id,
                    "step.verified",
                    suffix=str(generation),
                    inputs_hash=h,
                    capability=cap.ref,
                    status="succeeded",
                    outputs=result.outputs,
                    verify_status="verified",
                )
                return {"status": "succeeded", "outputs": result.outputs}
            if result.error_type == "unknown_outcome":
                self.uncertain_steps.add(step.id)
            if result.error_type == "needs_human":
                task = await self.control(
                    step.id, "task", inputs=values, data=subcall or {}, attempt=generation
                )
                await workflow.wait_condition(
                    lambda task=task: self.tasks.get(step.id) == task.id or self.cancel_requested
                )
                if self.cancel_requested:
                    return {"status": "cancelled"}
                completed = await self.control(step.id, "task_result", data={"id": task.id})
                if completed.data["status"] == "completed":
                    await self.record(
                        step.id,
                        "step.verified",
                        inputs_hash=h,
                        status="succeeded",
                        outputs=completed.data["outputs"],
                        verify_status="human_attested",
                    )
                    return {"status": "succeeded", "outputs": completed.data["outputs"]}
            await self.record(
                step.id,
                "step.failed",
                inputs_hash=h,
                status="needs_attention",
                error_type=result.error_type,
                reason=result.message,
            )
            if self.envelope.plan.on_failure == "atomic" and result.error_type != "unknown_outcome":
                return {"status": "failed"}
            action = await self.attention(step.id, result.message)
            if action != "retry":
                return {"status": "skipped" if action == "skip" else "cancelled"}
            # Even a human retry never blindly re-invokes an uncertain write.
            while result.error_type == "unknown_outcome":
                verified = await self.verify(call, result, origins)
                if verified.ok:
                    self.uncertain_steps.discard(step.id)
                    await self.record(
                        step.id,
                        "step.verified",
                        suffix="human_retry",
                        inputs_hash=h,
                        status="succeeded",
                        outputs=verified.outputs,
                        verify_status="verified",
                    )
                    return {"status": "succeeded", "outputs": verified.outputs}
                if verified.happened is False:
                    self.uncertain_steps.discard(step.id)
                    break
                action = await self.attention(step.id, "verification inconclusive; no safe retry")
                if action != "retry":
                    return {"status": "skipped" if action == "skip" else "cancelled"}
            approval_id = None
        return {"status": "cancelled"}

    async def attention(self, step: str, reason: str) -> str:
        self.attention_count[step] = self.attention_count.get(step, 0) + 1
        await self.record(
            step,
            "step.needs_attention",
            suffix=f"{self.generations.get(step, 1)}:{self.attention_count[step]}",
            status="needs_attention",
            run_status="needs_attention",
            reason=reason,
        )
        await workflow.wait_condition(lambda: step in self.actions or self.cancel_requested)
        if self.cancel_requested:
            return "cancel"
        action = self.actions.pop(step)
        if action["action"] == "skip":
            await self.record(step, "step.skipped", status="skipped", reason=action["reason"])
        return action["action"]

    async def invoke_with_retries(
        self, call: CapabilityCall, origins: dict[str, Source], subcall: dict[str, Any] | None
    ) -> InvokeResult:
        result = InvokeResult(ok=False, error_type="not_retryable", message="no attempts")
        for attempt in range(1, call.capability.retry.max_attempts + 1):
            if self.cancel_requested:
                return InvokeResult(ok=False, error_type="needs_human", message="cancelled")
            if attempt > 1:
                check = await self.control(
                    call.step_id,
                    "policy",
                    inputs=call.inputs,
                    sources=origins,
                    data=subcall or {},
                    attempt=self.generations[call.step_id] * 100 + attempt,
                )
                if check.decision is None or check.decision.outcome != "allow":
                    return InvokeResult(
                        ok=False, error_type="needs_human", message="policy changed before retry"
                    )
                call = call.model_copy(update={"authorization": check.authorization})
            await self.record(
                call.step_id,
                "step.started",
                suffix=str(attempt),
                inputs_hash=call.inputs_hash,
                capability=call.capability.ref,
                status="running",
                idempotency_key=call.idempotency_key,
            )
            result = await self.domain(call)
            if result.handle:
                handle = result.handle
                await self.record(
                    call.step_id,
                    "step.async_pending",
                    suffix=str(attempt),
                    status="waiting_backend",
                    handle=handle,
                )
                deadline = workflow.now() + timedelta(
                    seconds=duration_seconds(call.capability.verify.within)
                    if call.capability.verify != "none"
                    else 600
                )
                while workflow.now() < deadline and not self.cancel_requested:
                    with contextlib.suppress(TimeoutError):
                        await workflow.wait_condition(
                            lambda call=call, handle=handle: (
                                self.callbacks.get(call.step_id, {}).get("handle") == handle
                                or self.cancel_requested
                            ),
                            timeout=timedelta(seconds=5),
                        )
                    callback = self.callbacks.pop(call.step_id, None)
                    if callback and callback["handle"] == handle:
                        result = InvokeResult(ok=True, outputs=callback["outputs"], happened=True)
                        break
                    try:
                        result = await workflow.execute_activity(
                            "poll_capability",
                            {"call": call.model_dump(mode="json"), "handle": result.handle},
                            task_queue=call.capability.executor.queue,
                            start_to_close_timeout=timedelta(seconds=30),
                            schedule_to_start_timeout=timedelta(seconds=30),
                            retry_policy=RetryPolicy(maximum_attempts=1),
                            result_type=InvokeResult,
                        )
                    except ActivityError:
                        result = InvokeResult(
                            ok=False,
                            error_type="unknown_outcome",
                            message="job polling interrupted; verify before retry",
                        )
                    if not result.handle:
                        if not result.ok:
                            result = InvokeResult(
                                ok=False,
                                error_type="unknown_outcome",
                                message="job status inconclusive; verify outcome",
                            )
                        break
                if result.handle:
                    result = InvokeResult(
                        ok=False,
                        error_type="unknown_outcome",
                        message="asynchronous job did not finish within its window",
                    )
            await self.record(
                call.step_id,
                "step.attempted",
                suffix=str(attempt),
                inputs_hash=call.inputs_hash,
                outcome="ok" if result.ok else "error",
                error_type=result.error_type,
            )
            if result.ok or result.error_type == "unknown_outcome":
                verified = await self.verify(call, result, origins)
                if verified.ok:
                    return verified
                if result.ok:
                    return InvokeResult(
                        ok=False,
                        error_type="unknown_outcome",
                        message="verification window exhausted",
                    )
                if verified.happened is not False:
                    return InvokeResult(
                        ok=False,
                        error_type="unknown_outcome",
                        message="verification inconclusive; no safe retry",
                    )
            elif result.error_type in {"not_retryable", "needs_human"}:
                return result
            elif (
                call.capability.retry.mode == "safe_only"
                and call.capability.risk.class_ != "read"
                and result.happened is not False
            ):
                verified = await self.verify(call, result, origins)
                if verified.ok:
                    return verified
                if verified.happened is not False:
                    return InvokeResult(
                        ok=False, error_type="unknown_outcome", message="retry safety unproven"
                    )
            if call.capability.retry.mode == "never":
                return result
            if attempt < call.capability.retry.max_attempts:
                await workflow.sleep(result.retry_after or backoff(attempt))
        return InvokeResult(
            ok=False,
            error_type=result.error_type or "not_retryable",
            message="retry attempts exhausted",
        )

    async def domain(self, call: CapabilityCall) -> InvokeResult:
        try:
            return await workflow.execute_activity(
                "invoke_capability",
                call,
                task_queue=call.capability.executor.queue,
                start_to_close_timeout=timedelta(
                    seconds=call.capability.executor.timeout_seconds + 2
                ),
                schedule_to_start_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=1),
                result_type=InvokeResult,
            )
        except ActivityError as error:
            cause = error.cause
            if (
                isinstance(cause, TemporalTimeoutError)
                and cause.type == TimeoutType.SCHEDULE_TO_START
            ):
                return InvokeResult(
                    ok=False,
                    error_type="retryable",
                    message="worker unavailable; call never started",
                    happened=False,
                )
            return InvokeResult(
                ok=False,
                error_type="unknown_outcome",
                message="activity interrupted; verify before retry",
            )

    async def verify(
        self, call: CapabilityCall, result: InvokeResult, origins: dict[str, Source]
    ) -> InvokeResult:
        spec = call.capability.verify
        if spec == "none":
            return (
                result
                if result.ok
                else InvokeResult(ok=False, error_type="unknown_outcome", message="no verifier")
            )
        assert self.envelope is not None
        available = {**call.inputs, **result.outputs, "idempotency_key": call.idempotency_key}
        values = {
            key: available.get(value[2:-1], value)
            if isinstance(value, str) and value.startswith("${")
            else value
            for key, value in (spec.inputs or {}).items()
        }
        if spec.inputs is None:
            values = {"idempotency_key": call.idempotency_key}
        deadline = workflow.now() + timedelta(seconds=duration_seconds(spec.within))
        last = InvokeResult(ok=False, error_type="unknown_outcome", message="not yet verified")
        index = 0
        while workflow.now() < deadline and not self.cancel_requested:
            index += 1
            verify_step = call.step_id + "_verify"
            checked = await self.control(
                verify_step,
                "policy",
                inputs=values,
                sources={key: "trusted_output" for key in values},
                data={
                    "subcall": True,
                    "original_capability": call.capability.ref,
                    "capability": spec.capability,
                },
                attempt=self.generations[call.step_id] * 100 + index,
            )
            if checked.decision is None or checked.decision.outcome != "allow":
                return InvokeResult(
                    ok=False, error_type="unknown_outcome", message="verification policy denied"
                )
            cap = CapabilitySpec.model_validate(checked.data["capability"])
            if cap.risk.class_ != "read":
                return InvokeResult(
                    ok=False, error_type="unknown_outcome", message="verifier must be read-only"
                )
            h = inputs_hash(values)
            verifier = CapabilityCall(
                tenant_id=call.tenant_id,
                run_id=call.run_id,
                step_id=verify_step,
                capability=cap,
                inputs=values,
                inputs_hash=h,
                idempotency_key=idempotency_key(call.run_id, verify_step, h),
                authorization=checked.authorization,
            )
            last = await self.domain(verifier)
            await self.record(
                verify_step,
                "verification.checked",
                suffix=f"{self.generations[call.step_id]}:{index}",
                inputs_hash=h,
                capability=cap.ref,
                outcome="ok" if last.ok else "error",
            )
            if (
                last.ok
                and evaluate(spec.expect, last.outputs, internal_domains=frozenset()) is Truth.TRUE
            ):
                outputs = result.outputs or {
                    key: value
                    for key, value in last.outputs.items()
                    if key in call.capability.outputs
                }
                try:
                    validate_values(call.capability.outputs, outputs)
                except ValueError:
                    return InvokeResult(
                        ok=False,
                        error_type="unknown_outcome",
                        happened=True,
                        message="effect verified but required outputs unavailable",
                    )
                return InvokeResult(ok=True, outputs=outputs, happened=True)
            if not result.ok and last.happened is False:
                return last
            await workflow.sleep(5)
        return InvokeResult(
            ok=False,
            error_type="unknown_outcome",
            message="verification window exhausted",
            happened=last.happened,
        )

    async def compensate(self, envelope: RunEnvelope) -> dict[str, Any]:
        await self.record("", "run.compensating", run_status="compensating")
        cannot_undo: list[str] = [
            f"{step}: outcome uncertain; reconcile before attempting compensation"
            for step in sorted(self.uncertain_steps)
        ]
        ordered = {s.id: s for s in envelope.plan.steps}
        steps = (
            [ordered[id_] for id_ in self.completed_order]
            if self.completed_order
            else list(envelope.plan.steps)
        )
        for step in reversed(steps):
            if self.statuses.get(step.id) != "succeeded":
                continue
            cap = envelope.caps[step.id]
            if cap.risk.class_ == "read":
                continue
            if cap.compensate == "none":
                cannot_undo.append(f"{step.id}: {cap.compensate_reason}")
                continue
            ref = cap.compensate if isinstance(cap.compensate, str) else cap.compensate.capability
            values: dict[str, Any] = (
                {}
                if isinstance(cap.compensate, str)
                else resolve_refs(cap.compensate.inputs, self.outputs)
            )
            checked = await self.control(
                step.id + "_undo",
                "policy",
                inputs=values,
                sources={key: "trusted_output" for key in values},
                data={"subcall": True, "original_capability": cap.ref, "capability": ref},
            )
            undo_cap = CapabilitySpec.model_validate(checked.data["capability"])
            undo = Step(
                id=step.id + "_undo",
                capability=ref,
                inputs=values,
                sources={key: "trusted_output" for key in values},
            )
            # Cancellation with explicit compensation is a new approved human action.
            self.cancel_requested = False
            outcome = await self.execute_step(
                undo,
                undo_cap,
                subcall={"subcall": True, "original_capability": cap.ref, "capability": ref},
            )
            if outcome["status"] != "succeeded":
                cannot_undo.append(f"{step.id}: compensation {outcome['status']}")
        return await self.finish(
            "needs_attention" if cannot_undo else "compensated", cannot_undo=cannot_undo
        )

    async def finish(self, status: str, **extra: Any) -> dict[str, Any]:
        result = {
            "status": status,
            "steps": self.statuses,
            "uncertain_steps": sorted(self.uncertain_steps),
            **extra,
        }
        await self.record("", "run.finished", run_status=status, result=result)
        return result
