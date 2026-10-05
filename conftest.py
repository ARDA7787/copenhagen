"""Shared pytest fixtures for tests/ and spikes/.

`time_skipping_env` starts Temporal's time-skipping test server with the Pydantic
data converter. To stay offline it uses a local binary, in this order:
1. $TEMPORAL_TEST_SERVER (path to the `temporal-test-server` executable)
2. the first `.tools/temporal-test-server_*/temporal-test-server` in the repo
3. otherwise the SDK downloads it (needs network once).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

REPO_ROOT = Path(__file__).resolve().parent


def temporal_test_server_path() -> str | None:
    explicit = os.environ.get("TEMPORAL_TEST_SERVER")
    if explicit:
        if not Path(explicit).is_file():
            raise FileNotFoundError(f"TEMPORAL_TEST_SERVER points to a missing file: {explicit}")
        return explicit
    candidates = sorted(REPO_ROOT.glob(".tools/temporal-test-server_*/temporal-test-server"))
    return str(candidates[-1]) if candidates else None


@pytest.fixture
async def time_skipping_env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter,
        test_server_existing_path=temporal_test_server_path(),
    ) as env:
        yield env
