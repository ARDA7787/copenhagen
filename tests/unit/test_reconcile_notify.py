"""Notifications through the outbox, and reconciliation of runs whose workflow is gone."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import update

from copenhagen.audit.chain import events
from copenhagen.db.models import Approval, HumanTask, Outbox, Run
from copenhagen.db.store import get, put, transaction
from copenhagen.engine.dispatch import Dispatcher, enqueue
from copenhagen.engine.reconcile import Reconciler
from copenhagen.notify import (
    LogNotifier,
    NotificationError,
    WebhookNotifier,
    build,
    notification,
    signature,
)
from tests.unit.test_api import RecordingEngine

SECRET = "s" * 32


def note(kind="approval.requested", id_="a1"):
    return notification(
        kind, tenant="t", subject="Approval needed", public_url="https://ops.example", id=id_
    )


async def test_webhook_signs_body_and_timestamp():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await WebhookNotifier("https://hooks.example/x", SECRET, client=client).send(note())
    request = seen[0]
    timestamp = request.headers["X-Copenhagen-Timestamp"]
    assert request.headers["X-Copenhagen-Signature"] == signature(
        SECRET, timestamp, request.content
    )
    assert request.headers["X-Copenhagen-Event"] == "approval.requested"
    assert json.loads(request.content)["link"] == "https://ops.example/approvals"


async def test_webhook_failures_raise_notification_error():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(NotificationError):
        await WebhookNotifier("https://hooks.example/x", SECRET, client=client).send(note())

    def refuse(request):
        raise httpx.ConnectError("refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    with pytest.raises(NotificationError):
        await WebhookNotifier("https://hooks.example/x", SECRET, client=client).send(note())


def test_webhook_configuration_is_checked():
    with pytest.raises(ValueError, match="HTTPS"):
        WebhookNotifier("http://hooks.example/x", SECRET)
    with pytest.raises(ValueError, match="32"):
        WebhookNotifier("https://hooks.example/x", "short")
    with pytest.raises(ValueError, match="invalid"):
        WebhookNotifier("https://user:pw@hooks.example/x", SECRET)
    assert isinstance(WebhookNotifier("http://localhost/x", SECRET, allow_insecure=True), object)
    assert isinstance(build(None, None), LogNotifier)
    with pytest.raises(ValueError, match="secret"):
        build("https://hooks.example/x", None)
    with pytest.raises(ValueError, match="unknown"):
        notification("bogus", tenant="t", subject="s", public_url="")


def test_notifications_never_carry_inputs():
    event = notification(
        "task.created", tenant="t", subject="s", public_url="https://x/", id="k", role=None
    )
    assert event == {
        "kind": "task.created",
        "tenant_id": "t",
        "subject": "s",
        "link": "https://x/tasks",
        "id": "k",
    }


class Collecting:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send(self, event):
        if self.fail:
            raise NotificationError("down")
        self.sent.append(event)


def stage(service, run_id, status="waiting_approval", age=timedelta(hours=1)):
    with transaction(service.engine) as session:
        put(session, Run, service.tenant, run_id, {"plan_id": "p", "on_behalf_of": "leela"}, status)
        session.execute(
            update(Run).where(Run.id == run_id).values(created_at=datetime.now(UTC) - age)
        )


async def test_notifications_are_delivered_without_blocking_run_messages(service):
    stage(service, "run_n", "running")
    with transaction(service.engine) as session:
        enqueue(session, service.tenant, "notify:a1", None, "notify", note())
        enqueue(session, service.tenant, "run_n:go", "run_n", "go", {"id": "go"})
    engine, broken = RecordingEngine(), Collecting(fail=True)
    await Dispatcher(service.engine, service.tenant, engine, notifier=broken).flush()
    assert [name for _, name, _ in engine.signals] == ["go"]

    with transaction(service.engine) as session:
        session.execute(update(Outbox).values(next_attempt_at=None))
    healthy = Collecting()
    await Dispatcher(service.engine, service.tenant, engine, notifier=healthy).flush()
    assert [e["id"] for e in healthy.sent] == ["a1"]


async def test_dead_notification_does_not_flag_any_run(service):
    stage(service, "run_q", "running")
    with transaction(service.engine) as session:
        enqueue(session, service.tenant, "notify:a2", None, "notify", note(id_="a2"))
    dispatcher = Dispatcher(
        service.engine,
        service.tenant,
        RecordingEngine(),
        notifier=Collecting(fail=True),
        max_attempts=1,
    )
    await dispatcher.flush()
    with transaction(service.engine) as session:
        assert get(session, Outbox, service.tenant, "notify:a2").status == "dead"
        assert get(session, Run, service.tenant, "run_q").status == "running"


def lookup(states):
    async def status(run_id):
        state = states.get(run_id, "RUNNING")
        if isinstance(state, Exception):
            raise state
        return state

    return status


async def test_reconciler_closes_out_runs_whose_workflow_ended(service):
    stage(service, "run_timed_out")
    stage(service, "run_missing", "needs_attention")
    stage(service, "run_alive")
    with transaction(service.engine) as session:
        put(session, Approval, service.tenant, "ap1", {"run_id": "run_timed_out"}, "pending")
        put(session, HumanTask, service.tenant, "tk1", {"run_id": "run_missing"}, "pending")
        put(session, Approval, service.tenant, "ap2", {"run_id": "run_alive"}, "pending")
    states = {"run_timed_out": "TIMED_OUT", "run_missing": None}
    reconciler = Reconciler(
        service.engine, service.tenant, lookup(states), public_url="https://ops.example"
    )
    assert sorted(await reconciler.once()) == ["run_missing", "run_timed_out"]
    with transaction(service.engine) as session:
        run = get(session, Run, service.tenant, "run_timed_out")
        assert run.status == "failed"
        assert "TIMED_OUT" in run.data["attention"]
        assert get(session, Run, service.tenant, "run_missing").status == "failed"
        assert get(session, Run, service.tenant, "run_alive").status == "waiting_approval"
        assert get(session, Approval, service.tenant, "ap1").status == "expired"
        assert get(session, HumanTask, service.tenant, "tk1").status == "expired"
        assert get(session, Approval, service.tenant, "ap2").status == "pending"
        orphaned = [
            e for e in events(session, service.tenant) if e.data["event_type"] == "run.orphaned"
        ]
        assert {e.data["run_id"] for e in orphaned} == {"run_missing", "run_timed_out"}
        notify = get(session, Outbox, service.tenant, "notify:run.orphaned:run_timed_out")
        assert notify.run_id is None
        assert notify.data["value"]["link"] == "https://ops.example/runs/run_timed_out"
    # A second pass finds nothing left to do.
    assert await reconciler.once() == []


async def test_reconciler_leaves_young_and_undelivered_runs_alone(service):
    stage(service, "run_young", age=timedelta(seconds=5))
    stage(service, "run_queued")
    with transaction(service.engine) as session:
        enqueue(session, service.tenant, "run_queued:start", "run_queued", "start", {})
    reconciler = Reconciler(
        service.engine, service.tenant, lookup({"run_young": None, "run_queued": None})
    )
    assert await reconciler.once() == []


async def test_reconciler_does_nothing_when_temporal_is_unreachable(service):
    stage(service, "run_x")
    reconciler = Reconciler(service.engine, service.tenant, lookup({"run_x": ConnectionError()}))
    assert await reconciler.once() == []
    with transaction(service.engine) as session:
        assert get(session, Run, service.tenant, "run_x").status == "waiting_approval"
