"""Spike 1 — a workflow waits for a signal, with Pydantic models and sandbox passthrough.

Runs in Temporal's time-skipping test environment: a two-day wait finishes in milliseconds.
The test-server binary comes from the root conftest (local `.tools/` copy or $TEMPORAL_TEST_SERVER),
so this runs offline.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from spikes.approval_workflow import TASK_QUEUE, WaitForApproval, sandbox_runner
from spikes.models import ApprovalDecision, ApprovalRequest, Outcome

pytestmark = pytest.mark.workflow


@pytest.fixture
def env(time_skipping_env: WorkflowEnvironment) -> WorkflowEnvironment:
    return time_skipping_env


def _request() -> ApprovalRequest:
    return ApprovalRequest(
        run_id=f"run_{uuid.uuid4().hex[:12]}",
        amount_cents=30_000,
        recipient_email="customer@example.com",
        requested_at=datetime(2026, 10, 5, 12, 0, tzinfo=UTC),
    )


def _worker(client: Client) -> Worker:
    return Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[WaitForApproval],
        workflow_runner=sandbox_runner(),
    )


async def test_signal_after_one_day_completes(env: WorkflowEnvironment) -> None:
    req = _request()
    wall = time.monotonic()
    async with _worker(env.client):
        handle = await env.client.start_workflow(
            WaitForApproval.run, req, id=req.run_id, task_queue=TASK_QUEUE
        )
        await env.sleep(timedelta(days=1))
        assert await handle.query(WaitForApproval.waiting) is True

        await handle.signal(
            WaitForApproval.decide, ApprovalDecision(approver="leela", approved=True)
        )
        # A second, stale decision is ignored.
        await handle.signal(
            WaitForApproval.decide, ApprovalDecision(approver="sam", approved=False)
        )
        outcome = await handle.result()

    assert isinstance(outcome, Outcome)  # a real model came back, not a dict
    assert outcome.status == "approved"
    assert outcome.decision == ApprovalDecision(approver="leela", approved=True)
    assert outcome.stale_signals == 1
    assert outcome.waited_seconds >= timedelta(days=1).total_seconds()
    assert time.monotonic() - wall < 30, "time-skipping should make a day take seconds"


async def test_no_signal_expires_after_two_days(env: WorkflowEnvironment) -> None:
    req = _request()
    wall = time.monotonic()
    async with _worker(env.client):
        outcome = await env.client.execute_workflow(
            WaitForApproval.run, req, id=req.run_id, task_queue=TASK_QUEUE
        )
    assert outcome.status == "expired"
    assert outcome.decision is None
    assert outcome.waited_seconds == pytest.approx(timedelta(days=2).total_seconds(), abs=1)
    assert time.monotonic() - wall < 30


async def test_pydantic_input_round_trips_datetime_and_extras(env: WorkflowEnvironment) -> None:
    """Datetimes keep their timezone through the converter; nothing is silently dropped."""
    req = _request()
    payload = pydantic_data_converter.payload_converter.to_payloads([req])
    back = pydantic_data_converter.payload_converter.from_payloads(payload, [ApprovalRequest])
    assert back == [req]
    assert back[0].requested_at.tzinfo is not None
