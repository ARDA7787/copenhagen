"""Security and governance rules that stop one person or one leaked key from acting alone."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from copenhagen.api.app import create_app, hook_secret
from copenhagen.audit.chain import events
from copenhagen.core.schema import check_schema, money_values
from copenhagen.db.models import ApiKey, Approval, Outbox, Tenant
from copenhagen.db.store import get, rows, transaction
from copenhagen.registry.publish import capability, publish
from copenhagen.settings import Settings
from tests.unit.test_api import RecordingEngine
from tests.unit.test_service import pending


def client(service):
    return TestClient(create_app(service.settings, db=service.engine, run_engine=RecordingEngine()))


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + key}


def key_row(service, key_id: str) -> ApiKey:
    with transaction(service.engine) as session:
        return next(k for k in rows(session, ApiKey, service.tenant) if k.data["key_id"] == key_id)


# API keys


def test_key_minted_by_a_key_cannot_outlive_it(service):
    parent = service.issue_key("leela", ["read", "run"], 1)
    with client(service) as http:
        response = http.post(
            "/v1/api-keys", json={"scopes": ["read"], "hours": 720}, headers=bearer(parent)
        )
        assert response.status_code == 200
        listing = http.get("/v1/api-keys", headers=bearer(parent)).json()
    expiries = sorted(datetime.fromisoformat(k["expires_at"]) for k in listing)
    assert len(expiries) == 2
    assert expiries[1] - expiries[0] < timedelta(seconds=5)
    assert expiries[1] < datetime.now(UTC) + timedelta(hours=1, minutes=1)


def test_key_listing_hides_secrets_and_other_principals(service):
    service.issue_key("leela", ["read"], 1)
    sam = service.issue_key("sam", ["read"], 1)
    with client(service) as http:
        listing = http.get("/v1/api-keys", headers=bearer(sam)).json()
    assert {k["principal_id"] for k in listing} == {"sam"}
    assert all(
        set(k) == {"key_id", "principal_id", "scopes", "expires_at", "status"} for k in listing
    )


def test_admin_can_revoke_another_principals_key(service):
    leaked = service.issue_key("leela", ["read", "run"], 24)
    admin = service.issue_key("admin", ["read", "run", "admin"], 1)
    sam = service.issue_key("sam", ["read", "run"], 1)
    with client(service) as http:
        listing = http.get("/v1/api-keys", headers=bearer(admin)).json()
        target = next(k["key_id"] for k in listing if k["principal_id"] == "leela")
        # A non-admin cannot revoke someone else's key.
        assert http.post(f"/v1/api-keys/{target}/revoke", headers=bearer(sam)).status_code == 404
        assert http.post(f"/v1/api-keys/{target}/revoke", headers=bearer(admin)).status_code == 200
        assert http.get("/v1/capabilities", headers=bearer(leaked)).status_code == 401
    assert key_row(service, target).status == "revoked"


# Approvals and roles


def test_high_risk_edit_needs_an_independent_approver(service):
    pending(service)
    service.assign_role("admin", "dina", "approver:finance", True)
    result = service.decide(
        "sam", "approval", True, "lower to the disputed amount", edits={"amount_cents": 25000}
    )
    assert result["status"] == "pending_review"
    with transaction(service.engine) as session:
        item = get(session, Approval, service.tenant, "approval")
        assert item.status == "pending"
        assert item.data["edited_by"] == "sam"
        assert item.data["inputs"]["amount_cents"] == 25000
        # Nothing reached the workflow, but the next approver was notified.
        names = [o.data["name"] for o in rows(session, Outbox, service.tenant)]
        assert "approval_decided" not in names
        assert "notify" in names
    with pytest.raises(ValueError, match="independent approver"):
        service.decide("sam", "approval", True, "approving my own edit")
    service.decide("dina", "approval", True, "edit checked against the dispute")
    with transaction(service.engine) as session:
        item = get(session, Approval, service.tenant, "approval")
        assert item.status == "approved"
        assert item.data["decided_by"] == "dina"
        kinds = [e.data["event_type"] for e in events(session, service.tenant)]
        assert "approval.edited" in kinds


def test_unchanged_edit_does_not_need_a_second_review(service):
    pending(service)
    result = service.decide("sam", "approval", True, "matches", edits={"amount_cents": 30000})
    assert "status" not in result


def test_rejection_with_edits_is_still_a_rejection(service):
    pending(service)
    service.decide("sam", "approval", False, "not a duplicate", edits={"amount_cents": 1})
    with transaction(service.engine) as session:
        assert get(session, Approval, service.tenant, "approval").status == "rejected"


def test_admin_cannot_grant_themselves_roles_when_another_admin_exists(service):
    # A lone administrator can still bootstrap their own access.
    service.assign_role("admin", "admin", "approver:finance", True)
    service.assign_role("admin", "dina", "admin", True)
    with pytest.raises(ValueError, match="another administrator"):
        service.assign_role("admin", "admin", "approver:trading", True)
    # Another administrator can.
    service.assign_role("dina", "admin", "approver:trading", True)
    # Dropping a role from yourself stays allowed.
    service.assign_role("admin", "admin", "approver:finance", False)


# Money and risk classes


def test_money_fields_are_found_by_unit_nested_and_in_arrays():
    fields = {
        "total": {"type": "integer", "unit": "cents"},
        "amount_cents": {"type": "integer"},
        "count": {"type": "integer"},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"price": {"type": "integer", "unit": "cents"}},
            },
        },
        "transfer": {
            "type": "object",
            "properties": {"value": {"type": "integer", "unit": "cents"}},
        },
    }
    values = {
        "total": 100,
        "amount_cents": 7,
        "count": 999999,
        "lines": [{"price": 10}, {"price": 20}],
        "transfer": {"value": 5},
    }
    assert sorted(money_values(fields, values)) == [5, 7, 10, 20, 100]
    with pytest.raises(ValueError, match="minor units"):
        check_schema({"type": "string", "unit": "cents"})


def test_money_ceiling_covers_fields_not_named_amount_cents(service):
    with transaction(service.engine) as session:
        spec = capability(session, service.tenant, "payments.refund@1")
    inputs = {k: v for k, v in spec.inputs.items() if k != "amount_cents"}
    inputs["refund_total"] = {"type": "integer", "unit": "cents"}
    renamed = spec.model_copy(update={"version": spec.version + 10, "inputs": inputs})
    values = {"payment_id": "pi_1", "refund_total": 750000, "reason": "duplicate"}
    with transaction(service.engine) as session:
        ctx = service.context(session)
        decision = service.policy.decide(
            service.identity(session, "leela"),
            renamed,
            values,
            dict.fromkeys(values, "user"),
            ctx.model_copy(update={"runtime": True}),
        )
    assert decision.outcome == "deny"


def set_config(service, **config):
    with transaction(service.engine) as session:
        tenant = session.get(Tenant, service.tenant)
        assert tenant is not None
        tenant.config = {**tenant.config, **config}


def test_risk_floor_stops_a_payment_declared_as_internal(service):
    set_config(service, risk_floors={"payments.*": "financial", "comms.*": "external_write"})
    with transaction(service.engine) as session:
        spec = capability(session, service.tenant, "payments.refund@1")
    downgraded = spec.model_copy(
        update={
            "version": spec.version + 1,
            "risk": spec.risk.model_copy(update={"class_": "internal_write"}),
        }
    )
    with transaction(service.engine) as session, pytest.raises(ValueError, match="risk floor"):
        publish(session, service.tenant, downgraded, "admin", second_reviewer="sam")
    # Identity is high risk too, but a floor of "financial" must match exactly.
    other = spec.model_copy(
        update={
            "version": spec.version + 2,
            "risk": spec.risk.model_copy(update={"class_": "identity"}),
        }
    )
    with transaction(service.engine) as session, pytest.raises(ValueError, match="risk floor"):
        publish(session, service.tenant, other, "admin", second_reviewer="sam")


def test_tenant_can_require_review_for_more_classes(service):
    with transaction(service.engine) as session:
        spec = capability(session, service.tenant, "payments.refund@1")
    external = spec.model_copy(
        update={
            "name": "comms.announce",
            "version": 1,
            "risk": spec.risk.model_copy(update={"class_": "external_write"}),
        }
    )
    set_config(service, review_required=["external_write"])
    with transaction(service.engine) as session, pytest.raises(ValueError, match="second reviewer"):
        publish(session, service.tenant, external, "admin")
    with transaction(service.engine) as session:
        assert publish(session, service.tenant, external, "admin", second_reviewer="sam")


# Webhooks


def test_each_hook_source_signs_with_its_own_secret(monkeypatch):
    monkeypatch.setenv("COPENHAGEN_HOOK_SECRET_STRIPE", "s" * 40)
    dev = Settings(env="test", hook_secret="shared-dev-secret-shared-dev-secret")
    assert hook_secret(dev, "stripe") == "s" * 40
    assert hook_secret(dev, "github") == "shared-dev-secret-shared-dev-secret"
    assert hook_secret(dev, "../etc") is None
    prod = dev.model_copy(update={"env": "prod"})
    assert hook_secret(prod, "stripe") == "s" * 40
    # Production never falls back to a secret shared between vendors.
    assert hook_secret(prod, "github") is None
