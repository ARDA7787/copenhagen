"""Reconcile the database with Temporal so no run is left waiting forever.

The database records what a run is doing; Temporal is what actually does it. They can
drift apart when a workflow ends without recording its own outcome: the execution
timeout fires while a step waits for a person, an operator terminates it from the
Temporal UI, or a start was dead-lettered and the workflow never existed.

The reconciler finds runs the database still thinks are active, asks Temporal for their
workflow state, and closes out the ones whose workflow is gone. Such a run becomes
``failed`` with an ``run.orphaned`` audit event, its pending approvals and tasks are
expired so nobody acts on them, and the requester is notified.

Runs with pending outbox messages are skipped: their start or signal has not been
delivered yet, so Temporal not knowing them is expected. Recently created runs are
skipped for the same reason.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, exists, select

from copenhagen.audit.chain import append
from copenhagen.db.models import Approval, HumanTask, Outbox, Run
from copenhagen.db.store import lock, transaction
from copenhagen.engine.dispatch import TERMINAL, enqueue
from copenhagen.notify import notification

logger = logging.getLogger(__name__)

# Temporal execution states that mean the workflow will never record anything again.
CLOSED = {"COMPLETED", "FAILED", "CANCELED", "TERMINATED", "TIMED_OUT"}
GRACE = timedelta(minutes=10)
BATCH = 200

# Returns the Temporal execution status name, or None when no such workflow exists.
StatusLookup = Callable[[str], Awaitable[str | None]]


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class Reconciler:
    def __init__(
        self,
        db: Engine,
        tenant: str | None,
        status: StatusLookup,
        *,
        public_url: str = "",
        grace: timedelta = GRACE,
        interval: float = 300.0,
    ) -> None:
        self.db, self.tenant, self.status = db, tenant, status
        self.public_url = public_url
        self.grace, self.interval = grace, interval

    def candidates(self) -> list[tuple[str, str]]:
        cutoff = datetime.now(UTC) - self.grace
        undelivered = exists().where(
            Outbox.tenant_id == Run.tenant_id,
            Outbox.run_id == Run.id,
            Outbox.status == "pending",
        )
        query = (
            select(Run.tenant_id, Run.id)
            .where(Run.status.not_in(TERMINAL), Run.created_at < cutoff, ~undelivered)
            .order_by(Run.created_at)
            .limit(BATCH)
        )
        if self.tenant is not None:
            query = query.where(Run.tenant_id == self.tenant)
        with transaction(self.db) as session:
            return [(tenant, id_) for tenant, id_ in session.execute(query)]

    async def once(self) -> list[str]:
        """One pass. Returns the runs that were closed out."""
        closed: list[str] = []
        for tenant, run_id in await asyncio.to_thread(self.candidates):
            try:
                state = await self.status(run_id)
            except Exception as error:  # Temporal unreachable: try again next pass.
                logger.warning(
                    "Reconcile lookup failed",
                    extra={"run_id": run_id, "error": type(error).__name__},
                )
                return closed
            if state is not None and state not in CLOSED:
                continue
            if await asyncio.to_thread(self.orphan, tenant, run_id, state or "NOT_FOUND"):
                closed.append(run_id)
        return closed

    def orphan(self, tenant: str, run_id: str, state: str) -> bool:
        with transaction(self.db) as session:
            lock(session, f"reconcile:{tenant}:{run_id}")
            run = session.get(Run, (tenant, run_id))
            if run is None or run.status in TERMINAL:
                return False
            pending = session.scalar(
                select(Outbox.id).where(
                    Outbox.tenant_id == tenant,
                    Outbox.run_id == run_id,
                    Outbox.status == "pending",
                )
            )
            if pending is not None:
                return False
            previous = run.status
            run.status = "failed"
            run.data = {**run.data, "attention": f"workflow ended without a result ({state})"}
            expired: list[str] = []
            for model in (Approval, HumanTask):
                for row in session.scalars(
                    select(model).where(model.tenant_id == tenant, model.status == "pending")
                ):
                    if row.data.get("run_id") == run_id:
                        row.status = "expired"
                        expired.append(row.id)
            append(
                session,
                tenant,
                f"run.orphaned:{run_id}",
                "run.orphaned",
                run_id=run_id,
                workflow_state=state,
                previous_status=previous,
                expired=sorted(expired),
            )
            enqueue(
                session,
                tenant,
                f"notify:run.orphaned:{run_id}",
                None,
                "notify",
                notification(
                    "run.orphaned",
                    tenant=tenant,
                    subject=f"Run {run_id} ended without a result",
                    public_url=self.public_url,
                    run_id=run_id,
                    requester=run.data.get("on_behalf_of"),
                    workflow_state=state,
                ),
            )
            logger.warning("Run orphaned", extra={"run_id": run_id, "workflow_state": state})
            return True

    async def run(self) -> None:
        while True:
            try:
                await self.once()
            except Exception as error:
                logger.warning("Reconcile pass failed", extra={"error": type(error).__name__})
            await asyncio.sleep(self.interval)
