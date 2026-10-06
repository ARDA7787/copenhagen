"""Domain activities: vendor credentials, no DB import, and no blind retries."""

import os
from typing import Any

from temporalio import activity

from copenhagen.adapters.base import Adapter, invoke_checked
from copenhagen.adapters.fake import FakeAdapter, World
from copenhagen.adapters.http import HTTPAdapter
from copenhagen.adapters.human import HumanAdapter
from copenhagen.core.calls import CapabilityCall, InvokeResult, Preview
from copenhagen.core.schema import validate_values
from copenhagen.secrets.broker import CredentialBroker


class DomainActivities:
    def __init__(
        self,
        queue: str,
        *,
        env: str = "dev",
        backends: dict[str, str] | None = None,
        fake_path: str = ".data/fake.sqlite",
        credentials: frozenset[str] = frozenset(),
        signing_key: str | None = None,
        dev_override: bool = False,
    ) -> None:
        self.queue = queue
        key = signing_key or os.environ.get("COPENHAGEN_AUTHORIZATION_KEY", "dev-control-key")
        if env == "prod" and (key == "dev-control-key" or dev_override):
            raise ValueError("production worker requires authorization key; no dev overrides")
        self.broker = CredentialBroker(queue, key, credentials)
        self.adapters: dict[str, Adapter] = {
            "http": HTTPAdapter(backends or {}, dev_override=dev_override, env=env),
            "human": HumanAdapter(),
        }
        if env != "prod":
            self.adapters["fake"] = FakeAdapter(World(fake_path))

    @activity.defn(name="invoke_capability")
    async def invoke(self, call: CapabilityCall) -> InvokeResult:
        cred = self.broker.get(call)
        adapter = self.adapters.get(call.capability.executor.adapter)
        if adapter is None:
            raise ValueError("adapter unavailable in this environment")
        return await invoke_checked(adapter, call, cred)

    @activity.defn(name="preview_capability")
    async def preview(self, call: CapabilityCall) -> Preview:
        cred = self.broker.get(call)
        adapter = self.adapters[call.capability.executor.adapter]
        await adapter.validate_config(call.capability.executor)
        return await adapter.preview(call, cred)

    @activity.defn(name="poll_capability")
    async def poll(self, args: dict[str, Any]) -> InvokeResult:
        call = CapabilityCall.model_validate(args["call"])
        cred = self.broker.get(call)
        adapter = self.adapters[call.capability.executor.adapter]
        await adapter.validate_config(call.capability.executor)
        result = (
            await adapter.poll_call(call, args["handle"], cred)
            if isinstance(adapter, HTTPAdapter)
            else await adapter.poll(call.capability.executor.backend + ":" + args["handle"], cred)
        )
        if result.ok:
            try:
                validate_values(call.capability.outputs, result.outputs)
            except ValueError:
                return InvokeResult(
                    ok=False,
                    error_type="unknown_outcome",
                    message="asynchronous outputs do not match capability schema",
                )
        return result
