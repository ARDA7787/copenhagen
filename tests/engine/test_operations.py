"""Generic company operations: no seeded recipes, personas, or mockworld endpoints."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from temporalio import activity
from temporalio.worker import Replayer, Worker
from tests.engine.test_workflow import runner, wait_pending

from copenhagen.core.calls import CapabilityCall, InvokeResult
from copenhagen.core.capability import CapabilitySpec
from copenhagen.core.plan import PlanIR
from copenhagen.db.store import transaction
from copenhagen.engine.control import ControlActivities
from copenhagen.engine.workflow import RunPlan
from copenhagen.registry.publish import publish

pytestmark = pytest.mark.workflow


def spec(name, *, risk="internal_write", **fields):
    return CapabilitySpec.model_validate(
        {
            "name": "ops." + name,
            "version": 1,
            "owner": "operations",
            "summary": name,
            "inputs": {},
            "outputs": {"record_id": {"type": "string"}, "status": {"type": "string"}},
            "executor": {
                "adapter": "fake",
                "backend": "company",
                "queue": "ops",
                "operation": name,
            },
            "risk": {"class": risk},
            "verify": "none",
            "verify_reason": "test attestation",
            "compensate": "none",
            "compensate_reason": "test operation",
            **fields,
        }
    )


class Backend:
    def __init__(self):
        self.calls = []
        self.errors = {}

    @activity.defn(name="invoke_capability")
    async def invoke(self, call: CapabilityCall) -> InvokeResult:
        self.calls.append(call)
        if error := self.errors.get(call.capability.name):
            return InvokeResult(ok=False, error_type=error, message="backend fault")
        return InvokeResult(
            ok=True, outputs={"record_id": call.step_id, "status": "succeeded"}, happened=True
        )


@asynccontextmanager
async def running(env, svc, backend):
    control = ControlActivities(svc.engine, svc.settings)
    async with (
        Worker(
            env.client,
            task_queue="control",
            workflows=[RunPlan],
            activities=[control.execute],
            workflow_runner=runner(),
        ),
        Worker(env.client, task_queue="ops", activities=[backend.invoke]),
    ):
        yield


def plan_run(svc, caps, steps, mode="continue"):
    with transaction(svc.engine) as session:
        for cap in caps:
            publish(session, svc.tenant, cap, "operator", "reviewer")
    preview = svc.preview(
        "operator", plan=PlanIR.model_validate({"steps": steps, "on_failure": mode})
    )
    return svc.confirm("operator", preview["id"])


async def eventually(predicate):
    for _ in range(400):
        if predicate():
            return
        await asyncio.sleep(0.025)
    raise AssertionError("operation did not progress")


async def test_unrelated_branch_progresses_while_approval_waits(time_skipping_env, company):
    env, backend = time_skipping_env, Backend()
    run = plan_run(
        company,
        [spec("wait", risk="identity"), spec("work")],
        [
            {"id": "approval", "capability": "ops.wait@1"},
            {"id": "first", "capability": "ops.work@1"},
            {"id": "second", "capability": "ops.work@1", "depends_on": ["first"]},
        ],
    )
    async with running(env, company, backend):
        handle = await env.client.start_workflow(
            RunPlan.run, run, id=run.run_id, task_queue="control"
        )
        pending = await wait_pending(company, run.run_id)
        await eventually(lambda: any(c.step_id == "second" for c in backend.calls))
        assert not any(c.step_id == "approval" for c in backend.calls)
        decision = company.decide("reviewer", pending.id, True, "reviewed")
        await handle.signal(RunPlan.approval_decided, decision)
        assert (await asyncio.wait_for(handle.result(), 15))["status"] == "succeeded"
        history = await handle.fetch_history()
    await Replayer(
        workflows=[RunPlan], workflow_runner=runner(), data_converter=env.client.data_converter
    ).replay_workflow(history)


async def test_atomic_compensates_reverse_dependencies_not_array_order(time_skipping_env, company):
    env, backend = time_skipping_env, Backend()
    undo = spec("undo")
    root = spec("root", compensate={"capability": undo.ref})
    child = spec("child", compensate={"capability": undo.ref})
    failure = spec("failure")
    backend.errors[failure.name] = "not_retryable"
    run = plan_run(
        company,
        [undo, root, child, failure],
        [
            {"id": "child", "capability": child.ref, "depends_on": ["root"]},
            {"id": "root", "capability": root.ref},
            {"id": "failure", "capability": failure.ref, "depends_on": ["child"]},
        ],
        "atomic",
    )
    async with running(env, company, backend):
        result = await asyncio.wait_for(
            env.client.execute_workflow(RunPlan.run, run, id=run.run_id, task_queue="control"), 15
        )
    assert result["status"] == "compensated"
    assert [c.step_id for c in backend.calls if c.capability.name == undo.name] == [
        "child_undo",
        "root_undo",
    ]


async def test_uncertain_write_never_repeats_on_inconclusive_human_retry(
    time_skipping_env, company
):
    env, backend = time_skipping_env, Backend()
    read = spec("lookup", risk="read", inputs={"idempotency_key": {"type": "string"}})
    write = spec(
        "write",
        verify={
            "capability": read.ref,
            "within": "1s",
            "expect": {"output": "status", "op": "eq", "value": "succeeded"},
        },
    )
    backend.errors = {write.name: "unknown_outcome", read.name: "unknown_outcome"}
    run = plan_run(company, [read, write], [{"id": "write", "capability": write.ref}])
    async with running(env, company, backend):
        handle = await env.client.start_workflow(
            RunPlan.run, run, id=run.run_id, task_queue="control"
        )
        await eventually(lambda: any(c.capability.name == read.name for c in backend.calls))
        await env.sleep(6)
        await eventually(
            lambda: company.read_run("operator", run.run_id)["status"] == "needs_attention"
        )
        await handle.signal(
            RunPlan.step_action, {"step_id": "write", "action": "retry", "id": "retry-1"}
        )
        await eventually(lambda: sum(c.capability.name == read.name for c in backend.calls) >= 2)
        await env.sleep(6)
        await eventually(
            lambda: (
                "inconclusive"
                in company.read_run("operator", run.run_id)["steps"][0].get("reason", "")
            )
        )
        assert sum(c.capability.name == write.name for c in backend.calls) == 1
        await handle.signal(
            RunPlan.step_action,
            {
                "step_id": "write",
                "action": "skip",
                "reason": "reconcile externally",
                "id": "skip-1",
            },
        )
        assert (await asyncio.wait_for(handle.result(), 15))["status"] == "succeeded_with_skips"


async def test_async_callback_finishes_registered_job(time_skipping_env, company):
    from copenhagen.db.models import StepRun
    from copenhagen.db.store import rows

    class AsyncBackend(Backend):
        @activity.defn(name="invoke_capability")
        async def invoke(self, call: CapabilityCall) -> InvokeResult:
            self.calls.append(call)
            return InvokeResult(ok=False, handle="job_42", error_type="needs_human")

    env, backend = time_skipping_env, AsyncBackend()
    cap = spec("async")
    run = plan_run(company, [cap], [{"id": "job", "capability": cap.ref}])

    def registered():
        with transaction(company.engine) as session:
            return any(
                s.data.get("handle") == "job_42" for s in rows(session, StepRun, company.tenant)
            )

    async with running(env, company, backend):
        handle = await env.client.start_workflow(
            RunPlan.run, run, id=run.run_id, task_queue="control"
        )
        await eventually(registered)
        await handle.signal(
            RunPlan.backend_completed,
            {
                "step_id": "job",
                "handle": "job_42",
                "outputs": {"record_id": "completed", "status": "succeeded"},
            },
        )
        assert (await asyncio.wait_for(handle.result(), 15))["status"] == "succeeded"
        assert len(backend.calls) == 1
