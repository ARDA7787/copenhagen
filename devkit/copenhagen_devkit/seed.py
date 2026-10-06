"""Repeatable local fixture seeding; forbidden in production."""

from pathlib import Path

from copenhagen.audit.chain import append
from copenhagen.core.capability import CapabilitySpec
from copenhagen.core.recipe import RecipeSpec
from copenhagen.db.models import Principal, PrincipalRole, Role, Tenant
from copenhagen.db.store import put, transaction
from copenhagen.registry.loader import load
from copenhagen.registry.publish import publish
from copenhagen.service import Service


def seed(service: Service, root: Path | None = None) -> None:
    if service.settings.env == "prod":
        raise ValueError("demo seeding forbidden in production")
    root = root or Path(__file__).resolve().parents[2] / "examples"
    tenant = service.tenant
    with transaction(service.engine) as session:
        if session.get(Tenant, tenant) is None:
            session.add(
                Tenant(
                    id=tenant,
                    name="Copenhagen demo business",
                    config={
                        "internal_domains": ["example.com"],
                        "money_ceiling": 500000,
                        "daily_money_cents": 1000000,
                        "hooks": {
                            "operations": {
                                "principal_id": "events",
                                "allowed_recipes": ["comms.event_notice"],
                            }
                        },
                    },
                )
            )
        grants = {
            "requester": ["comms.*", "shop.*"],
            "finance": ["payments.*", "shop.*", "comms.*"],
            "people": ["people.*", "comms.*", "github.*", "platform.*"],
            "platform": ["platform.*", "comms.*", "shop.*"],
            "customers": ["customers.*", "comms.*"],
            "trading": ["trading.*", "comms.*"],
            "admin": ["*"],
            "auditor": [],
        }
        for queue in (
            "finance",
            "people",
            "platform",
            "comms",
            "github",
            "customers",
            "trading",
            "fake",
        ):
            grants["approver:" + queue] = []
        for name, patterns in grants.items():
            put(session, Role, tenant, name, {"patterns": patterns})
        users = {
            "omar": ["requester", "platform", "approver:platform"],
            "leela": ["finance", "people", "approver:finance"],
            "sam": ["finance", "approver:finance", "approver:trading"],
            "priya": [
                "people",
                "customers",
                "approver:people",
                "approver:comms",
                "approver:github",
                "approver:customers",
            ],
            "dina": ["customers"],
            "arjun": ["trading"],
            "admin": ["admin"],
            "auditor": ["auditor"],
            "events": ["requester"],
        }
        for id_, roles in users.items():
            put(
                session,
                Principal,
                tenant,
                id_,
                {"email": f"{id_}@example.com", "kind": "service" if id_ == "events" else "user"},
            )
            for role in roles:
                put(
                    session,
                    PrincipalRole,
                    tenant,
                    f"{id_}:{role}",
                    {"principal_id": id_, "role": role},
                )
        for path in sorted((root / "capabilities").glob("*.yaml")):
            publish(session, tenant, load(path, CapabilitySpec), "priya", "sam")
        for path in sorted((root / "recipes").glob("*.yaml")):
            publish(session, tenant, load(path, RecipeSpec), "priya")
        append(session, tenant, "demo-seed-v1", "dev.seeded", principal_id="admin")
