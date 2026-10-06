"""Outbox delivery: per-run order, isolation, backoff, leases and dead letters."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from copenhagen.audit.chain import events
from copenhagen.db.models import Outbox, Run
from copenhagen.db.store import put, transaction
from copenhagen.engine.client import PermanentDeliveryError
from copenhagen.engine.dispatch import Dispatcher, backoff, enqueue, outstanding, requeue
from tests.unit.test_api import RecordingEngine


def queue(service, run_id, *names, tenant=None):
    tenant = tenant or service.tenant
    with transaction(service.engine) as session:
        if session.get(Run, (tenant, run_id)) is None:
            put(session, Run, tenant, run_id, {"plan_id": "p"}, "running")
        for name in names:
            enqueue(session, tenant, f"{run_id}:{name}", run_id, name, {"id": name})


def due_now(service):
    with transaction(service.engine) as session:
        session.execute(update(Outbox).values(next_attempt_at=None))


def statuses(service):
    with transaction(service.engine) as session:
        return {row.id: row.status for row in session.query(Outbox)}


class Failing(RecordingEngine):
    """Fails every delivery for the listed runs with ``error``."""

    def __init__(self, error, runs):
        super().__init__()
        self.error, self.runs = error, set(runs)

    async def signal(self, run_id, name, value):
        if run_id in self.runs:
            raise self.error
        await super().signal(run_id, name, value)


async def test_later_message_never_overtakes_an_undelivered_earlier_one(service):
    queue(service, "run_a", "first", "second", "third")
    engine = Failing(ConnectionError(), {"run_a"})
    await Dispatcher(service.engine, service.tenant, engine).flush()
    assert engine.signals == []
    assert set(statuses(service).values()) == {"pending"}

    due_now(service)
    healthy = RecordingEngine()
    await Dispatcher(service.engine, service.tenant, healthy).flush()
    assert [name for _, name, _ in healthy.signals] == ["first", "second", "third"]
    assert set(statuses(service).values()) == {"delivered"}


async def test_one_failing_run_does_not_stall_other_runs(service):
    queue(service, "run_stuck", "a", "b")
    queue(service, "run_ok", "a", "b")
    engine = Failing(ConnectionError(), {"run_stuck"})
    await Dispatcher(service.engine, service.tenant, engine).flush()
    assert engine.signals == [
        ("run_ok", "a", {"id": "a"}),
        ("run_ok", "b", {"id": "b"}),
    ]


async def test_transient_failures_back_off_and_are_never_dead_lettered(service):
    queue(service, "run_a", "only")
    dispatcher = Dispatcher(
        service.engine, service.tenant, Failing(ConnectionError(), {"run_a"}), max_attempts=2
    )
    for _ in range(5):
        await dispatcher.flush()
        with transaction(service.engine) as session:
            row = session.get(Outbox, (service.tenant, "run_a:only"))
            assert row is not None
            assert row.next_attempt_at is not None  # backing off, not hot-looping
        due_now(service)
    with transaction(service.engine) as session:
        row = session.get(Outbox, (service.tenant, "run_a:only"))
        assert row is not None
        assert (row.status, row.attempts, row.last_error) == ("pending", 5, "ConnectionError")


def test_backoff_grows_and_is_capped():
    assert backoff(1) < timedelta(seconds=2)
    assert backoff(5) > backoff(1)
    assert backoff(100) <= timedelta(minutes=6)


async def test_permanent_failure_is_dead_lettered_audited_and_flags_the_run(service):
    queue(service, "run_closed", "late_decision", "after")
    engine = Failing(PermanentDeliveryError("NOT_FOUND"), {"run_closed"})
    await Dispatcher(service.engine, service.tenant, engine).flush()
    assert statuses(service)["run_closed:late_decision"] == "dead"
    with transaction(service.engine) as session:
        run = session.get(Run, (service.tenant, "run_closed"))
        assert run is not None
        assert run.status == "needs_attention"
        dead = outstanding(session, service.tenant)
        assert dead[0]["id"] == "run_closed:late_decision"
        assert dead[0]["last_error"] == "PermanentDeliveryError:NOT_FOUND"
        kinds = [e.data["event_type"] for e in events(session, service.tenant)]
        assert "dispatch.dead_lettered" in kinds


async def test_dead_message_can_be_requeued_by_an_operator(service):
    queue(service, "run_x", "decision")
    await Dispatcher(
        service.engine, service.tenant, Failing(PermanentDeliveryError("NOT_FOUND"), {"run_x"})
    ).flush()
    with transaction(service.engine) as session:
        requeue(session, service.tenant, "run_x:decision", "admin")
    healthy = RecordingEngine()
    await Dispatcher(service.engine, service.tenant, healthy).flush()
    assert healthy.signals == [("run_x", "decision", {"id": "decision"})]
    with transaction(service.engine) as session:
        kinds = [e.data["event_type"] for e in events(session, service.tenant)]
        assert "dispatch.requeued" in kinds


async def test_unclassified_errors_dead_letter_after_max_attempts(service):
    queue(service, "run_bug", "decision")
    dispatcher = Dispatcher(
        service.engine, service.tenant, Failing(RuntimeError("bug"), {"run_bug"}), max_attempts=3
    )
    for _ in range(3):
        await dispatcher.flush()
        due_now(service)
    assert statuses(service)["run_bug:decision"] == "dead"


async def test_claim_lease_protects_in_flight_delivery_and_expires(service):
    queue(service, "run_a", "only")
    first = Dispatcher(service.engine, service.tenant, RecordingEngine())
    claims = first.claim()  # instance claims, then crashes before delivering
    assert len(claims) == 1
    other = RecordingEngine()
    await Dispatcher(service.engine, service.tenant, other).flush()
    assert other.signals == []  # leased: no double delivery while in flight
    with transaction(service.engine) as session:
        session.execute(
            update(Outbox).values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    await Dispatcher(service.engine, service.tenant, other).flush()
    assert other.signals == [("run_a", "only", {"id": "only"})]


async def test_shared_dispatcher_serves_every_tenant(service):
    queue(service, "run_1", "go")
    queue(service, "run_2", "go", tenant="other-company")
    engine = RecordingEngine()
    await Dispatcher(service.engine, None, engine).flush()
    assert sorted(run for run, _, _ in engine.signals) == ["run_1", "run_2"]


async def test_dead_letter_never_overwrites_a_finished_run(service):
    queue(service, "run_done", "late")
    with transaction(service.engine) as session:
        run = session.get(Run, (service.tenant, "run_done"))
        assert run is not None
        run.status = "succeeded"
    await Dispatcher(
        service.engine, service.tenant, Failing(PermanentDeliveryError("NOT_FOUND"), {"run_done"})
    ).flush()
    with transaction(service.engine) as session:
        run = session.get(Run, (service.tenant, "run_done"))
        assert run is not None
        assert run.status == "succeeded"
