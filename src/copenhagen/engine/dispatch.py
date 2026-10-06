"""Transactional outbox: API restarts cannot lose a committed start or decision.

Delivery is at least once. Workflow IDs reject duplicate starts; signals carry stable
identifiers which the interpreter deduplicates, so redelivery is always safe.

Guarantees:
- Order per run. A message is never delivered while an earlier message for the same run
  is pending, so a decision can never overtake the start it belongs to. Other runs are
  not held up: one stuck run cannot stall the whole queue.
- Claim leases. A delivery claims its row by pushing ``next_attempt_at`` forward. Several
  API instances can deliver at once, and a crashed instance's claims expire and are
  picked up again.
- Backoff. Transient failures (Temporal unavailable, timeouts) back off exponentially,
  with jitter, up to ``MAX_BACKOFF``. They are retried indefinitely: losing a committed
  decision because Temporal was down for an hour would be worse than waiting.
- Dead letters. Permanent failures (closed workflow, rejected payload), and anything
  unclassified that keeps failing past ``max_attempts``, are moved to ``dead``. This is
  audited and the run is flagged ``needs_attention``. An operator can requeue them.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Engine, and_, exists, or_, select
from sqlalchemy.orm import Session, aliased

from copenhagen.audit.chain import append
from copenhagen.db.models import Outbox, Run
from copenhagen.db.store import lock, transaction
from copenhagen.engine.client import PermanentDeliveryError, RunEngine
from copenhagen.engine.contracts import RunEnvelope
from copenhagen.notify import LogNotifier, Notifier

logger = logging.getLogger(__name__)

LEASE = timedelta(seconds=60)
MAX_BACKOFF = timedelta(minutes=5)
BATCH = 100
TRANSIENT = (ConnectionError, TimeoutError, OSError)


def now() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime | None) -> datetime | None:
    # SQLite drops tzinfo on the way back; every stored time is UTC.
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def backoff(attempts: int) -> timedelta:
    base = min(2.0 ** max(attempts - 1, 0), MAX_BACKOFF.total_seconds())
    return timedelta(seconds=base * random.uniform(0.8, 1.2))  # noqa: S311 - jitter only


def enqueue(
    session: Session, tenant: str, id_: str, run_id: str | None, name: str, value: Any
) -> None:
    if session.get(Outbox, (tenant, id_)) is None:
        session.add(
            Outbox(
                tenant_id=tenant,
                id=id_,
                run_id=run_id,
                data={"run_id": run_id, "name": name, "value": value},
                status="pending",
                attempts=0,
                next_attempt_at=None,
                created_at=now(),
            )
        )
        session.flush()


@dataclass(frozen=True)
class Claim:
    tenant: str
    id: str
    run_id: str | None
    data: dict[str, Any]
    attempts: int


class Dispatcher:
    """Delivers outbox rows to the run engine. ``tenant=None`` serves every tenant."""

    def __init__(
        self,
        db: Engine,
        tenant: str | None,
        engine: RunEngine,
        *,
        max_attempts: int = 50,
        interval: float = 1.0,
        notifier: Notifier | None = None,
    ) -> None:
        self.db, self.tenant, self.engine = db, tenant, engine
        self.notifier = notifier or LogNotifier()
        self.max_attempts = max_attempts
        self.interval = interval
        self.mutex = asyncio.Lock()

    def claim(self) -> list[Claim]:
        """Claim the head message of each run, if it is due.

        Only a run's oldest pending message is eligible, so order holds per run. A run
        whose head is backing off holds back only its own later messages. Claims are
        serialised across instances with an advisory lock (one short transaction).
        """
        moment = now()
        earlier = aliased(Outbox)
        head = ~exists().where(
            earlier.tenant_id == Outbox.tenant_id,
            earlier.run_id == Outbox.run_id,
            earlier.status == "pending",
            or_(
                earlier.created_at < Outbox.created_at,
                and_(earlier.created_at == Outbox.created_at, earlier.id < Outbox.id),
            ),
        )
        query = (
            select(Outbox)
            .where(
                Outbox.status == "pending",
                or_(Outbox.run_id.is_(None), head),
                or_(Outbox.next_attempt_at.is_(None), Outbox.next_attempt_at <= moment),
            )
            .order_by(Outbox.created_at, Outbox.id)
            .limit(BATCH)
        )
        if self.tenant is not None:
            query = query.where(Outbox.tenant_id == self.tenant)
        with transaction(self.db) as session:
            lock(session, "dispatch:claim")
            claims: list[Claim] = []
            for row in session.scalars(query):
                row.attempts += 1
                row.next_attempt_at = moment + LEASE
                claims.append(Claim(row.tenant_id, row.id, row.run_id, row.data, row.attempts))
            return claims

    def delivered(self, claim: Claim) -> None:
        with transaction(self.db) as session:
            row = session.get(Outbox, (claim.tenant, claim.id))
            if row is not None and row.status == "pending":
                row.status = "delivered"
                row.next_attempt_at = None
                row.last_error = None

    def deferred(self, claim: Claim, error: str) -> None:
        with transaction(self.db) as session:
            row = session.get(Outbox, (claim.tenant, claim.id))
            if row is not None and row.status == "pending":
                row.next_attempt_at = now() + backoff(claim.attempts)
                row.last_error = error

    def dead(self, claim: Claim, error: str) -> None:
        with transaction(self.db) as session:
            row = session.get(Outbox, (claim.tenant, claim.id))
            if row is None or row.status != "pending":
                return
            row.status = "dead"
            row.next_attempt_at = None
            row.last_error = error
            run = session.get(Run, (claim.tenant, claim.run_id)) if claim.run_id else None
            if run is not None and run.status not in TERMINAL:
                run.status = "needs_attention"
                run.data = {**run.data, "attention": f"delivery of {claim.data['name']} failed"}
            append(
                session,
                claim.tenant,
                f"dispatch.dead:{claim.id}",
                "dispatch.dead_lettered",
                message_id=claim.id,
                run_id=claim.run_id,
                message=claim.data["name"],
                error=error,
                attempts=claim.attempts,
            )

    async def deliver(self, claim: Claim) -> None:
        data = claim.data
        try:
            if data["name"] == "notify":
                await self.notifier.send(data["value"])
            elif data["name"] == "start":
                await self.engine.start(RunEnvelope.model_validate(data["value"]))
            else:
                await self.engine.signal(data["run_id"], data["name"], data["value"])
        except (PermanentDeliveryError, ValidationError) as error:
            # Class names and status codes only: payloads and messages may hold secrets.
            reason = str(error) if isinstance(error, PermanentDeliveryError) else "invalid"
            logger.error(
                "Dispatch dead-lettered",
                extra={"message_id": claim.id, "error": type(error).__name__},
            )
            await asyncio.to_thread(self.dead, claim, f"{type(error).__name__}:{reason}")
            return
        except Exception as error:
            name = type(error).__name__
            if not isinstance(error, TRANSIENT) and claim.attempts >= self.max_attempts:
                await asyncio.to_thread(self.dead, claim, f"{name}:max_attempts")
                return
            logger.warning(
                "Dispatch deferred",
                extra={"message_id": claim.id, "error": name, "attempts": claim.attempts},
            )
            await asyncio.to_thread(self.deferred, claim, name)
            return
        await asyncio.to_thread(self.delivered, claim)

    async def flush(self) -> None:
        async with self.mutex:
            # Each pass delivers at most one message per run, so loop until nothing new
            # is due: a start and the decisions queued behind it arrive in one flush.
            for _ in range(BATCH):
                claims = await asyncio.to_thread(self.claim)
                if not claims:
                    return
                await asyncio.gather(*(self.deliver(claim) for claim in claims))

    async def start(self, run: RunEnvelope) -> None:
        await self.flush()

    async def signal(self, run_id: str, name: str, value: Any) -> None:
        await self.flush()

    async def run(self) -> None:
        while True:
            try:
                await self.flush()
            except Exception as error:
                logger.warning(
                    "Dispatch database unavailable; retrying",
                    extra={"error": type(error).__name__},
                )
            await asyncio.sleep(self.interval)


TERMINAL = {"succeeded", "failed", "cancelled", "compensated", "succeeded_with_skips"}


def requeue(session: Session, tenant: str, id_: str, actor: str) -> None:
    """Operator action: give a dead message another chance (e.g. after fixing a workflow)."""
    row = session.get(Outbox, (tenant, id_))
    if row is None:
        raise LookupError("outbox message not found")
    if row.status != "dead":
        raise ValueError("only dead messages can be requeued")
    row.status = "pending"
    row.attempts = 0
    row.next_attempt_at = None
    append(
        session,
        tenant,
        f"dispatch.requeued:{id_}:{now().isoformat()}",
        "dispatch.requeued",
        principal_id=actor,
        message_id=id_,
        run_id=row.run_id,
    )


def outstanding(session: Session, tenant: str, status: str = "dead") -> list[dict[str, Any]]:
    query = (
        select(Outbox)
        .where(Outbox.tenant_id == tenant, Outbox.status == status)
        .order_by(Outbox.created_at, Outbox.id)
        .limit(500)
    )
    return [
        {
            "id": row.id,
            "run_id": row.run_id,
            "message": row.data.get("name"),
            "status": row.status,
            "attempts": row.attempts,
            "last_error": row.last_error,
            "next_attempt_at": (aware(row.next_attempt_at) or now()).isoformat()
            if row.next_attempt_at
            else None,
            "created_at": row.created_at.isoformat(),
        }
        for row in session.scalars(query)
    ]
