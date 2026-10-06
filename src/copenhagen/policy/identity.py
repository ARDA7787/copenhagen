"""Current roles always come from the database, including at execution time."""

from sqlalchemy.orm import Session

from copenhagen.core.identity import Principal as Identity
from copenhagen.db.models import Principal, PrincipalRole, Role
from copenhagen.db.store import get, rows


def principal(session: Session, tenant: str, id_: str) -> Identity:
    item = get(session, Principal, tenant, id_)
    assigned = [
        r.data["role"]
        for r in rows(session, PrincipalRole, tenant)
        if r.data["principal_id"] == id_ and r.status == "active"
    ]
    roles = [get(session, Role, tenant, role) for role in assigned]
    return Identity(
        id=id_,
        tenant_id=tenant,
        email=item.data["email"],
        kind=item.data.get("kind", "user"),
        status=item.status,
        roles=tuple(r.id for r in roles if r.status == "active"),
        patterns=tuple(
            p for r in roles if r.status == "active" for p in r.data.get("patterns", [])
        ),
    )
