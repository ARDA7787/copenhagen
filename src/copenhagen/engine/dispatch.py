"""Transactional outbox: API restarts cannot lose a committed start or decision.

Delivery is at least once. Workflow IDs reject duplicate starts; signals carry stable
identifiers which the interpreter deduplicates. Multiple API instances may deliver
the same message safely. A failed message remains pending for the next pass.
"""

import asyncio
import logging
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from copenhagen.db.models import Outbox
from copenhagen.db.store import put, transaction
from copenhagen.engine.client import RunEngine
from copenhagen.engine.contracts import RunEnvelope

logger = logging.getLogger(__name__)


def enqueue(session: Session, tenant: str, id_: str, run_id: str, name: str, value: Any) -> None:
    if session.get(Outbox, (tenant, id_)) is None:
        put(
            session,
            Outbox,
            tenant,
            id_,
            {"run_id": run_id, "name": name, "value": value},
            "pending",
        )


class Dispatcher:
    def __init__(self, db: Engine, tenant: str, engine: RunEngine) -> None:
        self.db, self.tenant, self.engine = db, tenant, engine
        self.mutex = asyncio.Lock()

    def pending(self) -> list[tuple[str, dict[str, Any]]]:
        with transaction(self.db) as session:
            return [
                (row.id, row.data)
                for row in session.scalars(
                    select(Outbox)
                    .where(Outbox.tenant_id == self.tenant, Outbox.status == "pending")
                    .order_by(Outbox.created_at, Outbox.id)
                    .limit(100)
                )
            ]

    def delivered(self, id_: str) -> None:
        with transaction(self.db) as session:
            row = session.get(Outbox, (self.tenant, id_))
            if row:
                row.status = "delivered"

    async def flush(self) -> None:
        async with self.mutex:
            for id_, data in await asyncio.to_thread(self.pending):
                try:
                    if data["name"] == "start":
                        await self.engine.start(RunEnvelope.model_validate(data["value"]))
                    else:
                        await self.engine.signal(data["run_id"], data["name"], data["value"])
                    await asyncio.to_thread(self.delivered, id_)
                except Exception:
                    # Deliberately exclude payloads and exception strings (may contain secrets).
                    logger.warning("Temporal dispatch deferred", extra={"message_id": id_})

    async def start(self, run: RunEnvelope) -> None:
        await self.flush()

    async def signal(self, run_id: str, name: str, value: Any) -> None:
        await self.flush()

    async def run(self) -> None:
        while True:
            try:
                await self.flush()
            except Exception:
                logger.warning("Dispatch database unavailable; retrying")
            await asyncio.sleep(1)
