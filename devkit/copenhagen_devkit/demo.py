"""Milestone A acceptance against PostgreSQL and the running Temporal service.

No live vendor account is contacted: HTTP calls use a persistent mock business API.
The production workflow, policy, approval and audit implementation are exercised unchanged.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from copenhagen.adapters.http import HTTPAdapter
from copenhagen.audit.chain import check_invariants, verify
from copenhagen.db.models import Approval
from copenhagen.db.store import connect, rows, transaction
from copenhagen.engine.control import ControlActivities
from copenhagen.engine.domain import DomainActivities
from copenhagen.engine.workflow import RunPlan
from copenhagen.service import Service
from copenhagen.settings import Settings
from copenhagen_devkit.fake import FakeAdapter, World
from copenhagen_devkit.mockworld import create_app
from copenhagen_devkit.seed import seed


async def demo() -> dict[str, Any]:
    settings = Settings()
    if settings.env == "prod":
        raise ValueError("acceptance demo is development-only")
    db = connect(settings.db_url)
    service = Service(db, settings)
    seed(service)
    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        data_converter=pydantic_data_converter,
    )
    world = create_app(".data/mockworld.sqlite")
    results: dict[str, Any] = {}
    runner = SandboxedWorkflowRunner(
        restrictions=SandboxRestrictions.default.with_passthrough_modules(
            "copenhagen.core", "copenhagen.engine.contracts", "jsonschema", "pydantic"
        )
    )
    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=world))
        )
        for queue in ("finance", "people", "comms", "github", "platform", "customers", "trading"):
            worker = DomainActivities(
                queue,
                env=settings.env,
                extra_adapters={"fake": FakeAdapter(World(".data/fake.sqlite"))},
            )
            worker.adapters["http"] = HTTPAdapter(
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
                env="dev",
                dev_override=True,
                client=http,
            )
            await stack.enter_async_context(
                Worker(
                    client,
                    task_queue=queue,
                    activities=[worker.invoke, worker.preview, worker.poll],
                )
            )
        control = ControlActivities(db, settings)
        await stack.enter_async_context(
            Worker(
                client,
                task_queue="control",
                workflows=[RunPlan],
                activities=[control.execute],
                workflow_runner=runner,
            )
        )
        omar = service.preview(
            "omar", recipe_name="commerce.refund_order", parameters={"order_id": "1182"}
        )
        assert omar["preview"]["blocked"], "non-finance user must be blocked"
        results["role_gate"] = "passed"

        async def run(
            actor: str,
            name: str,
            params: dict[str, Any],
            approvers: dict[str, str],
            *,
            fault: bool = False,
        ) -> dict[str, Any]:
            preview = service.preview(actor, recipe_name=name, parameters=params)
            envelope = service.confirm(actor, preview["id"])
            if fault:
                await http.post("http://localhost/faults/refund/commit_then_drop")
            handle = await client.start_workflow(
                RunPlan.run, envelope, id=envelope.run_id, task_queue="control"
            )
            completion = asyncio.create_task(handle.result())
            decided: set[str] = set()
            try:
                for _ in range(600):
                    if completion.done():
                        return {"run_id": envelope.run_id, **await completion}
                    with transaction(db) as session:
                        pending = [
                            a
                            for a in rows(session, Approval, service.tenant)
                            if a.status == "pending" and a.data["run_id"] == envelope.run_id
                        ]
                    for item in pending:
                        if item.id in decided:
                            continue
                        if item.data["step_id"] not in approvers:
                            raise AssertionError(f"unexpected approval: {item.data['step_id']}")
                        if name == "commerce.refund_order":
                            try:
                                service.decide(actor, item.id, True, "self approval attempt")
                            except ValueError as error:
                                if "own request" not in str(error):
                                    raise
                            else:
                                raise AssertionError("I6 self-approval allowed")
                        approver = approvers[item.data["step_id"]]
                        decision = service.decide(
                            approver, item.id, True, "Demo: checked the resolved values"
                        )
                        await handle.signal(RunPlan.approval_decided, decision)
                        decided.add(item.id)
                    await asyncio.sleep(0.05)
                raise TimeoutError(f"demo run {envelope.run_id} did not complete")
            finally:
                if not completion.done():
                    completion.cancel()

        results["b2c_refund"] = await run(
            "leela", "commerce.refund_order", {"order_id": "1182"}, {"refund": "sam"}, fault=True
        )
        service.preapprove("priya", "people.onboard_engineer", 1)
        results["staff_onboarding"] = await run(
            "leela",
            "people.onboard_engineer",
            {
                "full_name": "Ada Lovelace",
                "personal_email": "ada@example.net",
                "team": "backend",
                "start_date": "2026-10-06",
            },
            {"aws": "omar", "payroll": "sam"},
        )
        service.preapprove("priya", "saas.onboard_customer", 1)
        results["b2b_saas"] = await run(
            "dina",
            "saas.onboard_customer",
            {
                "company": "Northstar Systems",
                "owner_email": "founder@northstar.example",
                "plan": "starter",
            },
            {},
        )
        results["trading_operations"] = await run(
            "arjun",
            "trading.release_paper_strategy",
            {"strategy_id": "mean_reversion_v1", "amount_cents": 100000},
            {"release": "sam"},
        )
        for name, result in results.items():
            if isinstance(result, dict):
                assert result["status"] == "succeeded", f"{name} failed: {result}"
        with transaction(db) as session:
            results["audit_chain"] = verify(session, service.tenant)
            results["audit_invariants"] = check_invariants(session, service.tenant)
    path = Path(".data/milestone-a-report.json")
    await asyncio.to_thread(path.write_text, json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))
    db.dispose()
    return results
