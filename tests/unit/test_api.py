import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from copenhagen.api.app import create_app


class RecordingEngine:
    def __init__(self):
        self.started = []
        self.signals = []

    async def start(self, run):
        self.started.append(run)

    async def signal(self, run_id, name, value):
        self.signals.append((run_id, name, value))


def client(service):
    return TestClient(create_app(service.settings, db=service.engine, run_engine=RecordingEngine()))


def test_login_csrf_and_private_reads(service):
    with client(service) as http:
        assert http.get("/v1/capabilities").status_code == 401
        assert http.post("/auth/dev", data={"principal_id": "leela"}).status_code == 200
        response = http.get("/")
        assert "Refund Order" in response.text
        assert (
            http.post(
                "/v1/recipes/commerce.refund_order/run", json={"parameters": {"order_id": "1182"}}
            ).status_code
            == 403
        )
        key = service.issue_key("leela", ["read"])
        assert (
            http.post(
                "/v1/recipes/commerce.refund_order/run",
                json={"parameters": {"order_id": "1182"}},
                headers={"Authorization": "Bearer " + key},
            ).status_code
            == 403
        )


def test_signed_hook_only_preapproved_and_no_replay(service):
    body = json.dumps(
        {
            "recipe": "comms.event_notice",
            "parameters": {"channel": "ops", "text": "All systems healthy"},
        }
    ).encode()
    timestamp = str(int(datetime.now(UTC).timestamp()))
    nonce = secrets.token_hex(16)
    signature = hmac.new(
        service.settings.hook_secret.encode(),
        b"operations." + timestamp.encode() + b"." + nonce.encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "X-Copenhagen-Timestamp": timestamp,
        "X-Copenhagen-Nonce": nonce,
        "X-Copenhagen-Signature": signature,
        "Content-Type": "application/json",
    }
    with client(service) as http:
        assert http.post("/v1/hooks/operations", content=body, headers=headers).status_code == 400
        service.preapprove("priya", "comms.event_notice", 1)
        assert http.post("/v1/hooks/operations", content=body, headers=headers).status_code == 200
        assert http.post("/v1/hooks/operations", content=body, headers=headers).status_code == 409
        assert (
            http.post("/v1/hooks/operations", content=body + b" ", headers=headers).status_code
            == 401
        )


def test_key_cannot_escalate_its_own_scopes(service):
    with client(service) as http:
        key = service.issue_key("admin", ["run"])
        response = http.post(
            "/v1/api-keys",
            json={"scopes": ["admin"], "hours": 1},
            headers={"Authorization": "Bearer " + key},
        )
        assert response.status_code == 403


def test_admin_pages_and_role_boundaries(service):
    with client(service) as http:
        http.post("/auth/dev", data={"principal_id": "admin"})
        assert http.get("/admin").status_code == 200
        assert http.get("/reviews").status_code == 200
        http.post("/auth/dev", data={"principal_id": "leela"})
        assert http.get("/admin").status_code == 403


def test_signed_callback_bound_to_job_and_outputs(company):
    from copenhagen.core.capability import CapabilitySpec
    from copenhagen.db.models import StepRun
    from copenhagen.db.store import put, transaction
    from tests.engine.test_operations import plan_run, spec

    company.settings.callback_secret = "callback-test-secret"
    cap = spec("async_job")
    data = cap.model_dump(mode="json", by_alias=True)
    data["executor"]["mode"] = "webhook_callback"
    cap = CapabilitySpec.model_validate(data)
    run = plan_run(company, [cap], [{"id": "job", "capability": cap.ref}])
    with transaction(company.engine) as session:
        put(
            session,
            StepRun,
            company.tenant,
            run.run_id + ":job",
            {"run_id": run.run_id, "step_id": "job", "handle": "job_1", "capability": cap.ref},
            "waiting_backend",
        )
    body = json.dumps(
        {"job_id": "job_1", "outputs": {"record_id": "record", "status": "succeeded"}}
    ).encode()
    timestamp, nonce = str(int(datetime.now(UTC).timestamp())), secrets.token_hex(16)
    payload = f"{run.run_id}.job.{timestamp}.{nonce}.".encode() + body
    signature = hmac.new(
        company.settings.callback_secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    headers = {
        "X-Copenhagen-Timestamp": timestamp,
        "X-Copenhagen-Nonce": nonce,
        "X-Copenhagen-Signature": signature,
        "Content-Type": "application/json",
    }
    with client(company) as http:
        path = f"/v1/callbacks/{run.run_id}/job"
        assert http.post(path, content=body, headers=headers).status_code == 200
        assert http.post(path, content=body, headers=headers).status_code == 200
        assert (
            http.post(path.replace("/job", "/other"), content=body, headers=headers).status_code
            == 401
        )
        assert http.post(path, content=body + b" ", headers=headers).status_code == 401


def test_admin_can_list_and_requeue_dead_messages(service):
    from copenhagen.db.models import Outbox
    from copenhagen.db.store import get, transaction
    from copenhagen.engine.dispatch import enqueue

    with transaction(service.engine) as session:
        enqueue(session, service.tenant, "notify:x", None, "notify", {"kind": "task.created"})
        get(session, Outbox, service.tenant, "notify:x").status = "dead"
    admin_key = {"Authorization": "Bearer " + service.issue_key("admin", ["read", "admin"])}
    with client(service) as http:
        http.post("/auth/dev", data={"principal_id": "leela"})
        assert http.get("/v1/admin/outbox").status_code == 403
        http.cookies.clear()
        dead = http.get("/v1/admin/outbox", headers=admin_key).json()
        assert [m["id"] for m in dead] == ["notify:x"]
        assert http.get("/v1/admin/outbox?status=delivered", headers=admin_key).status_code == 400
        response = http.post("/v1/admin/outbox/notify:x/requeue", headers=admin_key)
        assert response.status_code == 200, response.text
        assert http.post("/v1/admin/outbox/nope/requeue", headers=admin_key).status_code == 404
        assert http.get("/v1/admin/outbox", headers=admin_key).json() == []
        # RecordingEngine has no status lookup, so reconciliation is unavailable.
        assert http.post("/v1/admin/reconcile", headers=admin_key).status_code == 409
