"""Control-plane persistence. Every record carries a tenant key."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

Json = JSON().with_variant(JSONB, "postgresql")


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str]
    config: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)


class Record(Base):
    """Shared shape, separate tables and grants for each control-plane entity."""

    __abstract__ = True
    tenant_id: Mapped[str] = mapped_column(String, primary_key=True)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)
    status: Mapped[str] = mapped_column(String, default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Capability(Record):
    __tablename__ = "capabilities"
    name: Mapped[str] = mapped_column(String, index=True)
    version: Mapped[int] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint("tenant_id", "name", "version"),)


class Recipe(Record):
    __tablename__ = "recipes"
    name: Mapped[str] = mapped_column(String, index=True)
    version: Mapped[int] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint("tenant_id", "name", "version"),)


class Principal(Record):
    __tablename__ = "principals"


class Role(Record):
    __tablename__ = "roles"


class PrincipalRole(Record):
    __tablename__ = "principal_roles"


class CredentialRef(Record):
    __tablename__ = "credential_refs"


class Intent(Record):
    __tablename__ = "intents"


class Plan(Record):
    __tablename__ = "plans"


class Run(Record):
    __tablename__ = "runs"


class StepRun(Record):
    __tablename__ = "step_runs"


class Approval(Record):
    __tablename__ = "approvals"


class HumanTask(Record):
    __tablename__ = "human_tasks"


class Session(Record):
    __tablename__ = "sessions"


class ApiKey(Record):
    __tablename__ = "api_keys"


class Preapproval(Record):
    __tablename__ = "recipe_preapprovals"


class Usage(Record):
    __tablename__ = "usage_counters"


class Nonce(Record):
    __tablename__ = "hook_nonces"


class Outbox(Record):
    """Status: pending -> delivered, or dead after a permanent failure (operator retry)."""

    __tablename__ = "dispatch_outbox"
    run_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Doubles as a claim lease while a delivery is in flight.
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)


class PublicationReview(Record):
    __tablename__ = "publication_reviews"


class AuditEvent(Base):
    __tablename__ = "audit_events"
    tenant_id: Mapped[str] = mapped_column(String, primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    id: Mapped[str] = mapped_column(String)
    data: Mapped[dict[str, Any]] = mapped_column(Json)
    prev_hash: Mapped[str] = mapped_column(String)
    hash: Mapped[str] = mapped_column(String)
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)


class AuditAnchor(Record):
    __tablename__ = "audit_anchors"
