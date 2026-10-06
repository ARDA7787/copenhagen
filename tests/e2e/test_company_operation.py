"""Real PostgreSQL, Temporal and an independent company HTTP API; no demo seed."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from temporalio.worker import Worker
from tests.engine.test_workflow import runner, wait_pending

from copenhagen.administration import PrincipalInput, RoleInput, define_role, initialize, provision
from copenhagen.api.app import create_app
from copenhagen.audit.chain import check_invariants, verify
from copenhagen.core.capability import CapabilitySpec
from copenhagen.core.recipe import RecipeSpec
from copenhagen.db.store import connect, new_id, transaction
from copenhagen.engine.client import TemporalEngine
from copenhagen.engine.connection import connect_temporal
from copenhagen.engine.control import ControlActivities
from copenhagen.engine.domain import DomainActivities
from copenhagen.engine.workflow import RunPlan
from copenhagen.registry.publish import publish
from copenhagen.service import Service
from copenhagen.settings import Settings

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


async def test_company_operation_survives_workers_restarting(tmp_path):
    effects = {}

    class CompanyAPI(BaseHTTPRequestHandler):
        def respond(self, result):
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.respond({"record_id": "company-record", "status": "succeeded"})

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            key = self.headers["Idempotency-Key"]
            assert self.headers["X-Copenhagen-Run"]
            effects.setdefault(key, data)
            self.respond({"record_id": "company-record", "status": "succeeded"})

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), CompanyAPI)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = Settings(
        env="test",
        tenant_id=new_id("company"),
        copenhagen_dev_login=True,
        database_url="postgresql+psycopg://copenhagen_app:copenhagen_app@localhost:5432/copenhagen_test",
    )
    db = connect(settings.db_url)
    svc = Service(db, settings)
    initialize(
        svc, "Independent business", PrincipalInput(id="operator", email="op@business.example")
    )
    provision(svc, "operator", PrincipalInput(id="reviewer", email="review@business.example"))
    define_role(svc, "operator", RoleInput(name="approver:operations"))
    svc.assign_role("operator", "reviewer", "approver:operations", True)
    base = {
        "name": "company.read",
        "version": 1,
        "owner": "operations",
        "summary": "Read status",
        "inputs": {},
        "outputs": {"record_id": {"type": "string"}, "status": {"type": "string"}},
        "executor": {
            "adapter": "http",
            "backend": "company",
            "operation": "GET /records",
            "queue": "operations",
            "allowed_hosts": ["127.0.0.1"],
        },
        "risk": {"class": "read"},
        "verify": "none",
        "verify_reason": "Authoritative read",
        "compensate": "none",
        "compensate_reason": "No side effect",
    }
    read = CapabilitySpec.model_validate(base)
    write = CapabilitySpec.model_validate(
        {
            **base,
            "name": "company.create",
            "summary": "Create record",
            "executor": {**base["executor"], "operation": "POST /records"},
            "risk": {"class": "identity"},
            "verify": {
                "capability": read.ref,
                "inputs": {},
                "expect": {"output": "status", "op": "eq", "value": "succeeded"},
                "within": "10s",
            },
        }
    )
    # Explicitly pass a known empty input schema to the read verifier.
    recipe = RecipeSpec.model_validate(
        {
            "name": "company.operate",
            "version": 1,
            "owner": "operations",
            "description": "An independent five-step business operation",
            "parameters": {},
            "steps": [
                {"id": "read", "capability": read.ref},
                {"id": "create", "capability": write.ref, "depends_on": ["read"]},
                *[
                    {"id": f"check_{i}", "capability": read.ref, "depends_on": ["create"]}
                    for i in range(3)
                ],
            ],
        }
    )
    with transaction(db) as session:
        publish(session, svc.tenant, read, "operator")
        publish(session, svc.tenant, write, "operator", "reviewer")
        publish(session, svc.tenant, recipe, "operator")
    client = await connect_temporal(settings.temporal_address, settings.temporal_namespace)
    control = ControlActivities(db, settings)
    domain = DomainActivities(
        "operations",
        env="test",
        fake_path=str(tmp_path / "unused.sqlite"),
        backends={"company": f"http://127.0.0.1:{server.server_port}"},
    )
    api = create_app(settings, db=db, run_engine=TemporalEngine(client))
    try:
        async with (
            api.router.lifespan_context(api),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api), base_url="http://control"
            ) as http,
        ):
            headers = {
                "Authorization": "Bearer " + svc.issue_key("operator", ["read", "run"]),
                "Idempotency-Key": "business-operation-1",
            }
            async with (
                Worker(
                    client,
                    task_queue="control",
                    workflows=[RunPlan],
                    activities=[control.execute],
                    workflow_runner=runner(),
                    max_cached_workflows=0,
                ),
                Worker(client, task_queue="operations", activities=[domain.invoke, domain.poll]),
            ):
                response = await http.post(
                    "/v1/recipes/company.operate/run", json={"parameters": {}}, headers=headers
                )
                assert response.status_code == 200, response.text
                run_id = response.json()["run_id"]
                pending = await wait_pending(svc, run_id)
            # All application workers stop while waiting. A new client and fresh workers
            # load durable history from the real Temporal server and database.
            fresh_client = await connect_temporal(
                settings.temporal_address, settings.temporal_namespace
            )
            fresh_domain = DomainActivities(
                "operations",
                env="test",
                fake_path=str(tmp_path / "fresh.sqlite"),
                backends={"company": f"http://127.0.0.1:{server.server_port}"},
            )
            async with (
                Worker(
                    fresh_client,
                    task_queue="control",
                    workflows=[RunPlan],
                    activities=[ControlActivities(db, settings).execute],
                    workflow_runner=runner(),
                ),
                Worker(
                    fresh_client,
                    task_queue="operations",
                    activities=[fresh_domain.invoke, fresh_domain.poll],
                ),
            ):
                approved = await http.post(
                    f"/v1/approvals/{pending.id}/decide",
                    json={"approved": True, "reason": "Reviewed business operation"},
                    headers={"Authorization": "Bearer " + svc.issue_key("reviewer", ["approve"])},
                )
                assert approved.status_code == 200, approved.text
                result = await asyncio.wait_for(
                    fresh_client.get_workflow_handle(run_id).result(), 30
                )
                assert result["status"] == "succeeded"
                repeated = await http.post(
                    "/v1/recipes/company.operate/run", json={"parameters": {}}, headers=headers
                )
                assert repeated.json()["run_id"] == run_id
                assert len(effects) == 1
        with transaction(db) as session:
            assert check_invariants(session, svc.tenant)["authorized_attempts"] == 5
            assert verify(session, svc.tenant)["valid"]
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join()
        db.dispose()
