"""Transactions and tenant-scoped lookup; no domain worker may import this package."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session

from copenhagen.db.models import Record

T = TypeVar("T", bound=Record)


def new_id(prefix: str) -> str:
    # UUID hex is opaque, sortable time comes from created_at; never used inside workflows.
    return f"{prefix}_{uuid.uuid4().hex}"


def connect(url: str) -> Engine:
    return create_engine(
        url.replace("postgresql+asyncpg:", "postgresql+psycopg:"), pool_pre_ping=True
    )


@contextmanager
def transaction(engine: Engine) -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as session, session.begin():
        yield session


def lock(session: Session, key: str) -> None:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": number})


def get[T: Record](session: Session, model: type[T], tenant: str, id_: str) -> T:
    result = session.get(model, (tenant, id_))
    if result is None:
        raise ValueError(f"{model.__tablename__}: not found")
    return result


def rows[T: Record](session: Session, model: type[T], tenant: str) -> list[T]:
    return list(
        session.scalars(
            select(model).where(model.tenant_id == tenant).order_by(model.created_at, model.id)
        )
    )


def put[T: Record](
    session: Session,
    model: type[T],
    tenant: str,
    id_: str,
    data: dict[str, Any],
    status: str = "active",
) -> T:
    item = session.get(model, (tenant, id_))
    if item is None:
        item = model(tenant_id=tenant, id=id_, data=data, status=status)
        session.add(item)
    else:
        item.data = data
        item.status = status
    session.flush()
    return item
