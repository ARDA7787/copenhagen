"""The API depends on a small run-engine interface, not workflow implementation details."""

import contextlib
from datetime import timedelta
from typing import Any, Protocol

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from copenhagen.engine.connection import connect_temporal
from copenhagen.engine.contracts import RunEnvelope
from copenhagen.settings import Settings


class PermanentDeliveryError(Exception):
    """Retrying cannot succeed (closed workflow, rejected payload); dead-letter it."""


# Statuses where the request itself is wrong or the target is gone for good.
PERMANENT = {
    RPCStatusCode.NOT_FOUND,
    RPCStatusCode.INVALID_ARGUMENT,
    RPCStatusCode.FAILED_PRECONDITION,
    RPCStatusCode.PERMISSION_DENIED,
    RPCStatusCode.UNIMPLEMENTED,
    RPCStatusCode.OUT_OF_RANGE,
}


def classify(error: RPCError) -> Exception:
    if error.status in PERMANENT:
        return PermanentDeliveryError(error.status.name)
    return error


class RunEngine(Protocol):
    async def start(self, run: RunEnvelope) -> None: ...
    async def signal(self, run_id: str, name: str, value: Any) -> None: ...


class TemporalEngine:
    def __init__(self, client: Client, timeout_hours: int = 72) -> None:
        self.client = client
        self.timeout_hours = timeout_hours

    @classmethod
    async def connect(cls, settings: Settings) -> "TemporalEngine":
        return cls(
            await connect_temporal(
                settings.temporal_address, settings.temporal_namespace, lazy=True
            ),
            settings.run_timeout_hours,
        )

    async def start(self, run: RunEnvelope) -> None:
        try:
            with contextlib.suppress(WorkflowAlreadyStartedError):
                await self.client.start_workflow(
                    "RunPlan",
                    run,
                    id=run.run_id,
                    task_queue="control",
                    execution_timeout=timedelta(hours=self.timeout_hours),
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )
        except RPCError as error:
            raise classify(error) from error

    async def signal(self, run_id: str, name: str, value: Any) -> None:
        try:
            await self.client.get_workflow_handle(run_id).signal(name, value)
        except RPCError as error:
            # NOT_FOUND here means the workflow already closed (completed, timed out).
            raise classify(error) from error

    async def status(self, run_id: str) -> str | None:
        """Temporal execution status name, or None when the workflow does not exist."""
        try:
            description = await self.client.get_workflow_handle(run_id).describe()
        except RPCError as error:
            if error.status == RPCStatusCode.NOT_FOUND:
                return None
            raise
        return description.status.name if description.status else None
