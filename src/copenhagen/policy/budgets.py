"""Transactional reservations prevent concurrent runs overspending the same budget."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from copenhagen.db.models import Usage
from copenhagen.db.store import lock, put, rows


def reserve(
    session: Session,
    tenant: str,
    principal: str,
    key: str,
    cost: dict[str, int],
    limits: dict[str, int],
) -> bool:
    lock(session, f"budget:{tenant}")
    # A retry after midnight is the same reservation, not a second spend.
    for existing in rows(session, Usage, tenant):
        if existing.id.endswith(":" + principal) and (
            key in existing.data.get("reserved", {}) or key in existing.data.get("committed", {})
        ):
            return True
    day = "run" if principal.startswith("run:") else datetime.now(UTC).date().isoformat()
    # Tenant, principal and capability counters use the same reservation ledger.
    id_ = f"{day}:{principal}"
    row = session.get(Usage, (tenant, id_))
    data: dict[str, Any] = row.data if row else {"reserved": {}, "committed": {}}
    if key in data["reserved"] or key in data["committed"]:
        return True
    total = {
        dimension: sum(v.get(dimension, 0) for group in data.values() for v in group.values())
        for dimension in cost
    }
    if any(total.get(d, 0) + amount > limits.get(d, 2**53 - 1) for d, amount in cost.items()):
        return False
    put(session, Usage, tenant, id_, {**data, "reserved": {**data["reserved"], key: cost}})
    return True


def settle(session: Session, tenant: str, principal: str, key: str, *, commit: bool) -> None:
    lock(session, f"budget:{tenant}")
    for row in rows(session, Usage, tenant):
        if not row.id.endswith(":" + principal) or key not in row.data.get("reserved", {}):
            continue
        reserved = dict(row.data["reserved"])
        cost = reserved.pop(key)
        committed = dict(row.data["committed"])
        if commit:
            committed[key] = cost
        row.data = {"reserved": reserved, "committed": committed}
