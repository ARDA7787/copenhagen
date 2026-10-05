"""Spike 3 — Temporal dev-server state survives `docker compose restart`.

The Phase 3 demo approves a run *after* `make down && make up`, so the dev server must
keep its SQLite file on the named volume (and be allowed to write it: the image runs as
uid 1000, see `temporal-volume-init` in docker-compose.yml).

Needs the compose stack (`make up`). Run with: `pytest -m integration spikes/`.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from spikes.approval_workflow import TASK_QUEUE, WaitForApproval, sandbox_runner
from spikes.models import ApprovalDecision, ApprovalRequest, Outcome

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPORAL_ADDRESS = "localhost:7233"


async def _connect(deadline_s: float = 60) -> Client:
    """Connect, retrying while the server comes back up."""
    end = time.monotonic() + deadline_s
    while True:
        try:
            client = await Client.connect(TEMPORAL_ADDRESS, data_converter=pydantic_data_converter)
            await client.service_client.check_health()
            return client
        except Exception:
            if time.monotonic() > end:
                raise
            await asyncio.sleep(1)


def _compose(*args: str) -> None:
    subprocess.run(
        ["docker", "compose", *args], cwd=REPO_ROOT, check=True, capture_output=True, timeout=180
    )


def _worker(client: Client) -> Worker:
    return Worker(
        client, task_queue=TASK_QUEUE, workflows=[WaitForApproval], workflow_runner=sandbox_runner()
    )


async def test_waiting_workflow_survives_server_restart() -> None:
    try:
        client = await _connect(deadline_s=3)
    except Exception:
        pytest.skip("Temporal dev server not reachable on localhost:7233 — run `make up`")

    req = ApprovalRequest(
        run_id=f"run_spike3_{uuid.uuid4().hex[:8]}",
        amount_cents=30_000,
        recipient_email="customer@example.com",
        requested_at=datetime.now(UTC),
    )

    # 1. Start the workflow and let a worker take it to the waiting state.
    async with _worker(client):
        handle = await client.start_workflow(
            WaitForApproval.run, req, id=req.run_id, task_queue=TASK_QUEUE
        )
        assert await handle.query(WaitForApproval.waiting) is True

    # 2. Restart the server container (the worker is already gone).
    _compose("restart", "temporal")
    _compose("up", "--wait", "temporal")

    # 3. The workflow is still there, still waiting; a signal completes it.
    client = await _connect()
    # Typed handle: an untyped get_workflow_handle(id) returns the result as a plain dict.
    handle = client.get_workflow_handle_for(WaitForApproval.run, req.run_id)
    desc = await handle.describe()
    assert desc.status is not None
    assert desc.status.name == "RUNNING"

    async with _worker(client):
        assert await handle.query(WaitForApproval.waiting) is True
        await handle.signal(
            WaitForApproval.decide, ApprovalDecision(approver="leela", approved=True)
        )
        outcome = await handle.result()

    assert isinstance(outcome, Outcome)
    assert outcome.status == "approved"
    assert outcome.run_id == req.run_id
