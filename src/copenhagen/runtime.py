"""Process entrypoints keep the control zone and vendor worker zone separate."""

import asyncio
import os
from pathlib import Path

import yaml
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from copenhagen.engine.connection import connect_temporal


async def control_worker() -> None:
    from copenhagen.db.store import connect
    from copenhagen.engine.control import ControlActivities
    from copenhagen.engine.workflow import RunPlan
    from copenhagen.settings import Settings

    settings = Settings()
    client = await connect_temporal(settings.temporal_address, settings.temporal_namespace)
    activities = ControlActivities(connect(settings.db_url), settings)
    async with Worker(
        client,
        task_queue="control",
        workflows=[RunPlan],
        activities=[activities.execute],
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules(
                "copenhagen.core", "copenhagen.engine.contracts", "jsonschema", "pydantic"
            )
        ),
    ):
        await asyncio.Event().wait()


async def domain_worker(queue: str) -> None:
    # Intentionally do not read dotenv, Settings or import the DB/control modules.
    if any(key in os.environ for key in ("DATABASE_URL", "DATABASE_OWNER_URL")):
        raise ValueError("domain workers must not have database credentials (I2)")
    from copenhagen.engine.domain import DomainActivities
    from copenhagen.secrets.redaction import configure_logging

    configure_logging()

    env = os.environ.get("ENV", "dev")
    override = os.environ.get("COPENHAGEN_DEV_BACKENDS", "0") == "1"
    config = Path(
        os.environ.get(
            "COPENHAGEN_BACKENDS_FILE",
            "config/backends.dev.yaml" if override else "config/backends.yaml",
        )
    )
    backends = yaml.safe_load(await asyncio.to_thread(config.read_text))
    credentials = frozenset(filter(None, os.environ.get("COPENHAGEN_CREDENTIALS", "").split(",")))
    activities = DomainActivities(
        queue,
        env=env,
        backends=backends,
        credentials=credentials,
        dev_override=override,
        fake_path=os.environ.get("COPENHAGEN_FAKE_DB", ".data/fake.sqlite"),
    )
    client = await connect_temporal(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        os.environ.get("TEMPORAL_NAMESPACE", "default"),
    )
    async with Worker(
        client,
        task_queue=queue,
        activities=[activities.invoke, activities.preview, activities.poll],
    ):
        await asyncio.Event().wait()
