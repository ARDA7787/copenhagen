"""Serialized, deduplicated append-only tenant audit chains."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from copenhagen.core.canonical import canonical_json
from copenhagen.db.models import AuditEvent
from copenhagen.db.store import lock

GENESIS = "0" * 64


def digest(previous: str, event: dict[str, Any]) -> str:
    return hashlib.sha256(previous.encode() + canonical_json(event)).hexdigest()


def append(
    session: Session, tenant: str, event_id: str, event_type: str, **fields: Any
) -> AuditEvent:
    lock(session, f"audit:{tenant}")
    existing = session.scalar(
        select(AuditEvent).where(AuditEvent.tenant_id == tenant, AuditEvent.id == event_id)
    )
    payload = {"tenant_id": tenant, "id": event_id, "event_type": event_type, **fields}
    if existing:
        comparable = {k: v for k, v in existing.data.items() if k not in {"ts", "seq"}}
        if comparable != payload:
            raise ValueError("audit dedupe key reused with different event")
        return existing
    head = session.scalar(
        select(AuditEvent)
        .where(AuditEvent.tenant_id == tenant)
        .order_by(AuditEvent.seq.desc())
        .limit(1)
    )
    seq = head.seq + 1 if head else 1
    previous = head.hash if head else GENESIS
    payload.update(seq=seq, ts=datetime.now(UTC).isoformat())
    event = AuditEvent(
        tenant_id=tenant,
        seq=seq,
        id=event_id,
        data=payload,
        prev_hash=previous,
        hash=digest(previous, payload),
    )
    session.add(event)
    session.flush()
    return event


def events(session: Session, tenant: str) -> list[AuditEvent]:
    return list(
        session.scalars(
            select(AuditEvent).where(AuditEvent.tenant_id == tenant).order_by(AuditEvent.seq)
        )
    )


def verify(session: Session, tenant: str) -> dict[str, Any]:
    previous = GENESIS
    items = events(session, tenant)
    for seq, event in enumerate(items, 1):
        if event.seq != seq or event.data["seq"] != seq or event.prev_hash != previous:
            raise ValueError(f"audit chain gap/fork at sequence {event.seq}")
        if event.data["tenant_id"] != tenant or event.hash != digest(previous, event.data):
            raise ValueError(f"audit tampering at sequence {event.seq}")
        previous = event.hash
    return {"events": len(items), "head_hash": previous, "valid": True}


def check_invariants(session: Session, tenant: str) -> dict[str, Any]:
    verify(session, tenant)
    allowed: set[tuple[Any, ...]] = set()
    started = 0
    for item in events(session, tenant):
        event = item.data
        key = (event.get("run_id"), event.get("step_id"), event.get("inputs_hash"))
        if event["event_type"] == "policy.decided" and event.get("decision") == "allow":
            allowed.add(key)
        if event["event_type"] == "step.started":
            if key not in allowed:
                raise ValueError(f"I1 violation at sequence {item.seq}")
            started += 1
    return {"valid": True, "authorized_attempts": started}
