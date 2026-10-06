"""Durability and invariant scenarios run in Temporal's real time-skipping engine."""

import asyncio
import os
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
import pytest
from temporalio.worker import Replayer, Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from copenhagen.adapters.http import HTTPAdapter
from copenhagen.audit.chain import check_invariants
from copenhagen.db.models import Approval
from copenhagen.db.store import rows, transaction
from copenhagen.engine.control import ControlActivities
from copenhagen.engine.domain import DomainActivities
from copenhagen.engine.workflow import RunPlan
from copenhagen.mockworld import create_app

pytestmark = pytest.mark.workflow


def runner():
    return SandboxedWorkflowRunner(
        restrictions=SandboxRestrictions.default.with_passthrough_modules(
            "copenhagen.core", "copenhagen.engine.contracts", "jsonschema", "pydantic"
        )
    )


async def wait_pending(service, run_id, step_id=None):
    for _ in range(400):
        with transaction(service.engine) as session:
            pending = [
                a
                for a in rows(session, Approval, service.tenant)
                if a.data["run_id"] == run_id
                and a.status == "pending"
                and (step_id is None or a.data["step_id"] == step_id)
            ]
            if pending:
                return pending[0]
        await asyncio.sleep(0.025)
    raise AssertionError("approval never appeared")


async def workers(stack, env, service, tmp_path):
    world = create_app(str(tmp_path / "world.sqlite"))
    http = await stack.enter_async_context(
        httpx.AsyncClient(transport=httpx.ASGITransport(app=world))
    )
    for queue in ("finance", "comms", "people", "github", "platform", "customers", "trading"):
        domain = DomainActivities(
            queue, env="test", backends={}, fake_path=str(tmp_path / "fake.sqlite")
        )
        domain.adapters["http"] = HTTPAdapter(
            {
                name: "http://localhost"
                for name in (
                    "stripe",
                    "shop",
                    "slack",
                    "google",
                    "github",
                    "platform",
                    "payroll",
                    "email",
                    "customers",
                    "trading",
                )
            },
            dev_override=True,
            env="test",
            client=http,
        )
        await stack.enter_async_context(
            Worker(
                env.client,
                task_queue=queue,
                activities=[domain.invoke, domain.preview, domain.poll],
            )
        )
    return world


@pytest.mark.invariant("I1")
@pytest.mark.invariant("I11")
async def test_refund_unknown_outcome_verified_and_history_replays(
    time_skipping_env, service, tmp_path
):
    env = time_skipping_env
    control = ControlActivities(service.engine, service.settings)
    async with AsyncExitStack() as stack:
        world = await workers(stack, env, service, tmp_path)
        async with Worker(
            env.client,
            task_queue="control",
            identity="restarted-worker",
            max_cached_workflows=0,
            workflows=[RunPlan],
            activities=[control.execute],
            workflow_runner=runner(),
        ):
            preview = service.preview(
                "leela", recipe_name="commerce.refund_order", parameters={"order_id": "1182"}
            )
            envelope = service.confirm("leela", preview["id"])
            handle = await env.client.start_workflow(
                RunPlan.run, envelope, id=envelope.run_id, task_queue="control"
            )
            pending = await wait_pending(service, envelope.run_id)
            transport = httpx.ASGITransport(app=world)
            async with httpx.AsyncClient(transport=transport, base_url="http://world") as http:
                await http.post("/faults/refund/commit_then_drop")
            decision = service.decide("sam", pending.id, True, "confirmed duplicate charge")
            await handle.signal(RunPlan.approval_decided, decision)
            result = await asyncio.wait_for(handle.result(), 30)
            assert result["status"] == "succeeded"
            async with httpx.AsyncClient(transport=transport, base_url="http://world") as http:
                effects = (await http.get("/effects")).json()
                assert sum(e["operation"] == "refund" for e in effects) == 1
            history = await handle.fetch_history()
    await Replayer(
        workflows=[RunPlan], workflow_runner=runner(), data_converter=env.client.data_converter
    ).replay_workflow(history)
    if os.environ.get("COPENHAGEN_RECORD_HISTORY") == "1":
        await asyncio.to_thread(Path("tests/replay/refund.json").write_text, history.to_json())
    with transaction(service.engine) as session:
        assert check_invariants(session, service.tenant)["authorized_attempts"] >= 3


async def test_wait_survives_control_worker_restart(time_skipping_env, service, tmp_path):
    env = time_skipping_env
    control = ControlActivities(service.engine, service.settings)
    async with AsyncExitStack() as stack:
        await workers(stack, env, service, tmp_path)
        preview = service.preview(
            "arjun",
            recipe_name="trading.release_paper_strategy",
            parameters={"strategy_id": "mean_reversion_v1", "amount_cents": 100000},
        )
        envelope = service.confirm("arjun", preview["id"])
        async with Worker(
            env.client,
            task_queue="control",
            identity="restarted-worker",
            max_cached_workflows=0,
            workflows=[RunPlan],
            activities=[control.execute],
            workflow_runner=runner(),
        ):
            handle = await env.client.start_workflow(
                RunPlan.run, envelope, id=envelope.run_id, task_queue="control"
            )
            pending = await wait_pending(service, envelope.run_id)
        # A restarted process has a fresh client connection.
        from temporalio.client import Client

        fresh_client = await Client.connect(
            env.client.service_client.config.target_host,
            namespace=env.client.namespace,
            data_converter=env.client.data_converter,
        )
        # New process-equivalent worker replays history; no Python state is shared.
        async with Worker(
            fresh_client,
            task_queue="control",
            identity="restarted-worker",
            max_cached_workflows=0,
            workflows=[RunPlan],
            activities=[control.execute],
            workflow_runner=runner(),
        ):
            decision = service.decide("sam", pending.id, True, "paper deployment limit checked")
            await fresh_client.get_workflow_handle(envelope.run_id).signal(
                RunPlan.approval_decided, decision
            )
            try:
                outcome = await asyncio.wait_for(handle.result(), 10)
            except TimeoutError:
                print(service.read_run("arjun", envelope.run_id), flush=True)
                print(await asyncio.wait_for(handle.query(RunPlan.state), 2), flush=True)
                with transaction(service.engine) as session:
                    from copenhagen.audit.chain import events

                    print(
                        [
                            e.data
                            for e in events(session, service.tenant)
                            if e.data.get("run_id") == envelope.run_id
                        ]
                    )
                raise
            assert outcome["status"] == "succeeded"


async def test_onboarding_preapproval_leaves_aws_and_payroll_to_humans(
    time_skipping_env, service, tmp_path
):
    env = time_skipping_env
    service.preapprove("priya", "people.onboard_engineer", 1)
    control = ControlActivities(service.engine, service.settings)
    async with AsyncExitStack() as stack:
        await workers(stack, env, service, tmp_path)
        async with Worker(
            env.client,
            task_queue="control",
            identity="restarted-worker",
            max_cached_workflows=0,
            workflows=[RunPlan],
            activities=[control.execute],
            workflow_runner=runner(),
        ):
            preview = service.preview(
                "leela",
                recipe_name="people.onboard_engineer",
                parameters={
                    "full_name": "Ada Lovelace",
                    "personal_email": "ada@example.net",
                    "team": "backend",
                    "start_date": "2026-10-06",
                },
            )
            envelope = service.confirm("leela", preview["id"])
            handle = await env.client.start_workflow(
                RunPlan.run, envelope, id=envelope.run_id, task_queue="control"
            )
            aws = await wait_pending(service, envelope.run_id, "aws")
            payroll = await wait_pending(service, envelope.run_id, "payroll")
            decision = service.decide("omar", aws.id, True, "engineer role verified")
            await handle.signal(RunPlan.approval_decided, decision)
            # Sam approves payroll because the requester Leela may not approve herself.
            decision = service.decide("sam", payroll.id, True, "payroll details checked")
            await handle.signal(RunPlan.approval_decided, decision)
            result = await asyncio.wait_for(handle.result(), 30)
            assert result["status"] == "succeeded"
            with transaction(service.engine) as session:
                requested = [
                    a.data["step_id"]
                    for a in rows(session, Approval, service.tenant)
                    if a.data["run_id"] == envelope.run_id
                ]
            assert set(requested) == {"aws", "payroll"}


async def test_saved_history_replays():
    from temporalio.client import WorkflowHistory
    from temporalio.contrib.pydantic import pydantic_data_converter

    history = WorkflowHistory.from_json(
        "saved-refund", await asyncio.to_thread(Path("tests/replay/refund.json").read_text)
    )
    await Replayer(
        workflows=[RunPlan], workflow_runner=runner(), data_converter=pydantic_data_converter
    ).replay_workflow(history)
