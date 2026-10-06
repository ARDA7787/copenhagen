"""Product setup and crash recovery without relying on the example catalog."""

import pytest

from copenhagen.administration import PrincipalInput, RoleInput, define_role, initialize, provision
from copenhagen.db.models import Base, Capability, Outbox, Principal, Recipe, Tenant
from copenhagen.db.store import connect, rows, transaction
from copenhagen.engine.dispatch import Dispatcher
from copenhagen.service import Service
from copenhagen.settings import Settings
from tests.unit.test_api import RecordingEngine
from tests.unit.test_service import pending


def test_empty_company_bootstrap_and_least_privilege(tmp_path):
    db = connect(f"sqlite:///{tmp_path}/company.sqlite")
    Base.metadata.create_all(db)
    svc = Service(db, Settings(env="test", tenant_id="acme"))
    initialize(svc, "Acme", PrincipalInput(id="owner", email="owner@acme.example"))
    provision(svc, "owner", PrincipalInput(id="operator", email="ops@acme.example"))
    define_role(svc, "owner", RoleInput(name="operations", patterns=["acme.*"]))
    svc.assign_role("owner", "operator", "operations", True)
    with transaction(db) as session:
        tenant = session.get(Tenant, "acme")
        assert tenant is not None
        assert tenant.name == "Acme"
        assert len(rows(session, Principal, "acme")) == 2
        assert rows(session, Capability, "acme") == []
        assert rows(session, Recipe, "acme") == []
        assert svc.identity(session, "operator").patterns == ("acme.*",)
    with pytest.raises(ValueError, match="already initialized"):
        initialize(svc, "Wrong", PrincipalInput(id="attacker", email="bad@acme.example"))
    with pytest.raises(ValueError, match="administrator"):
        define_role(svc, "operator", RoleInput(name="admin", patterns=["*"]))
    db.dispose()


async def test_committed_start_survives_api_outage(service):
    preview = service.preview(
        "leela", recipe_name="commerce.refund_order", parameters={"order_id": "1182"}
    )
    run = service.confirm("leela", preview["id"])

    class Unavailable(RecordingEngine):
        async def start(self, run):
            raise ConnectionError("Temporal unavailable")

    await Dispatcher(service.engine, service.tenant, Unavailable()).flush()
    with transaction(service.engine) as session:
        assert rows(session, Outbox, service.tenant)[0].status == "pending"
    restarted_engine = RecordingEngine()
    restarted = Dispatcher(service.engine, service.tenant, restarted_engine)
    await restarted.flush()
    await restarted.flush()
    assert [r.run_id for r in restarted_engine.started] == [run.run_id]
    # Confirm is idempotent even after successful delivery.
    assert service.confirm("leela", preview["id"]).run_id == run.run_id
    await restarted.flush()
    assert len(restarted_engine.started) == 1


async def test_approval_and_task_delivery_is_transactional(service):
    pending(service)
    service.decide("sam", "approval", True, "checked")
    engine = RecordingEngine()
    await Dispatcher(service.engine, service.tenant, engine).flush()
    assert engine.signals[0][1] == "approval_decided"
    assert engine.signals[0][2]["id"] == "approval"


def test_repeated_recipe_requests_use_one_run(service):
    first = service.run_recipe(
        "leela", "commerce.refund_order", {"order_id": "1182"}, key="order-1"
    )
    second = service.run_recipe(
        "leela", "commerce.refund_order", {"order_id": "1182"}, key="order-1"
    )
    assert first.run_id == second.run_id
    with pytest.raises(ValueError, match="different parameters"):
        service.run_recipe("leela", "commerce.refund_order", {"order_id": "7500"}, key="order-1")


def test_publication_requires_independent_authenticated_review(company):
    from copenhagen.registry.publish import capability
    from copenhagen.registry.review import decide, propose
    from tests.engine.test_operations import spec

    cap = spec("identity", risk="identity")
    review_id = propose(company, "operator", cap)
    with pytest.raises(ValueError, match="independent"):
        decide(company, "operator", review_id, True, "self approved")
    with pytest.raises(ValueError, match="authority"):
        decide(company, "reviewer", review_id, True, "no publish role")
    company.assign_role("operator", "reviewer", "capability_author", True)
    decide(company, "reviewer", review_id, True, "checked hosts and least privilege")
    with transaction(company.engine) as session:
        assert capability(session, company.tenant, cap.ref).ref == cap.ref


def test_nested_reference_does_not_hide_invalid_literal_or_type(company):
    from copenhagen.core.plan import PlanIR
    from copenhagen.registry.publish import publish
    from tests.engine.test_operations import spec

    reader = spec("read", risk="read")
    writer = spec(
        "write",
        inputs={
            "record": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "count": {"type": "integer", "minimum": 1},
                },
                "required": True,
            }
        },
    )
    with transaction(company.engine) as session:
        publish(session, company.tenant, reader, "operator")
        publish(session, company.tenant, writer, "operator")

    def plan(record):
        return PlanIR.model_validate(
            {
                "steps": [
                    {"id": "read", "capability": reader.ref},
                    {
                        "id": "write",
                        "capability": writer.ref,
                        "depends_on": ["read"],
                        "inputs": {"record": record},
                    },
                ]
            }
        )

    with pytest.raises(ValueError, match="minimum"):
        company.preview("operator", plan=plan({"name": "${read.outputs.record_id}", "count": -1}))
    with pytest.raises(ValueError, match="type mismatch"):
        company.preview(
            "operator", plan=plan({"name": "valid", "count": "${read.outputs.record_id}"})
        )
