"""User-facing controls and revocation, not just implementation mirroring."""

import pytest

from copenhagen.audit.chain import append, verify
from copenhagen.core.canonical import inputs_hash
from copenhagen.db.models import Approval, AuditEvent
from copenhagen.db.store import put, transaction
from copenhagen.policy.preapproval import valid_preapproval
from copenhagen.registry.publish import capability, publish, recipe, set_status


def test_omar_blocked_and_leela_preview(service):
    omar = service.preview(
        "omar", recipe_name="commerce.refund_order", parameters={"order_id": "1182"}
    )
    assert omar["preview"]["blocked"]
    assert "finance" in omar["preview"]["steps"][1]["decision"]["reasons"][0]
    with pytest.raises(ValueError, match="blocked"):
        service.confirm("omar", omar["id"])
    leela = service.preview(
        "leela", recipe_name="commerce.refund_order", parameters={"order_id": "1182"}
    )
    assert not leela["preview"]["blocked"]
    assert leela["preview"]["steps"][1]["decision"]["outcome"] == "unknown_until_runtime"


def pending(service):
    from datetime import UTC, datetime, timedelta

    with transaction(service.engine) as session:
        put(
            session,
            Approval,
            service.tenant,
            "approval",
            {
                "run_id": "run",
                "step_id": "refund",
                "capability": "payments.refund@1",
                "inputs": {"payment_id": "pi_1182", "amount_cents": 30000, "reason": "duplicate"},
                "sources": {
                    "payment_id": "trusted_output",
                    "amount_cents": "trusted_output",
                    "reason": "user",
                },
                "inputs_hash": "original",
                "requester_id": "leela",
                "required_roles": ["approver:finance"],
                "risk_class": "financial",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
            "pending",
        )


@pytest.mark.invariant("I6")
def test_separation_of_duties(service):
    pending(service)
    with pytest.raises(ValueError, match="own request"):
        service.decide("leela", "approval", True, "looks good")
    service.decide("sam", "approval", True, "confirmed duplicate payment")


@pytest.mark.invariant("I5")
def test_edits_are_rehashed_and_ceiling_still_applies(service):
    pending(service)
    with pytest.raises(ValueError, match="policy denies"):
        service.decide("sam", "approval", True, "increase refund", edits={"amount_cents": 750000})
    service.decide("sam", "approval", True, "corrected amount", edits={"amount_cents": 25000})
    with transaction(service.engine) as session:
        item = session.get(Approval, (service.tenant, "approval"))
        assert item is not None
        assert item.data["inputs_hash"] == inputs_hash(item.data["inputs"])
        assert item.data["sources"]["amount_cents"] == "user"


def test_preapproval_revoked_on_new_version_and_disabled_capability(service):
    service.preapprove("priya", "people.onboard_engineer", 1)
    params = {
        "full_name": "New Engineer",
        "personal_email": "new@example.net",
        "team": "backend",
        "start_date": "2026-10-06",
    }
    with transaction(service.engine) as session:
        spec = recipe(session, service.tenant, "people.onboard_engineer")
        assert valid_preapproval(session, service.tenant, spec, params, "leela") is not None
        set_status(session, service.tenant, "people.create_google_account@1", "disabled", "admin")
        assert valid_preapproval(session, service.tenant, spec, params, "leela") is None


def test_immutable_version_and_tenant_lookup(service):
    with transaction(service.engine) as session:
        spec = capability(session, service.tenant, "payments.refund@1")
        assert publish(session, service.tenant, spec, "priya", "sam") is False
        with pytest.raises(ValueError, match="immutable"):
            publish(
                session,
                service.tenant,
                spec.model_copy(update={"summary": "changed"}),
                "priya",
                "sam",
            )
        with pytest.raises(ValueError, match="does not exist"):
            capability(session, "another-business", "payments.refund@1")


def test_audit_deduplication_and_tampering(service):
    with transaction(service.engine) as session:
        event = append(session, service.tenant, "dedupe", "test.event", value=1)
        assert append(session, service.tenant, "dedupe", "test.event", value=1).hash == event.hash
        with pytest.raises(ValueError, match="different event"):
            append(session, service.tenant, "dedupe", "test.event", value=2)
    with transaction(service.engine) as session:
        item = session.get(AuditEvent, (service.tenant, 1))
        assert item is not None
        item.data = {**item.data, "principal_id": "tampered"}
    with transaction(service.engine) as session, pytest.raises(ValueError, match="tampering"):
        verify(session, service.tenant)
