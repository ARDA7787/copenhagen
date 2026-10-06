"""Domain activities: vendor credentials, no DB import, and no blind retries."""

import os
from collections.abc import Mapping
from typing import Any

from temporalio import activity

from copenhagen.adapters.base import Adapter, CallPoller, invoke_checked
from copenhagen.adapters.registry import AdapterContext, build
from copenhagen.core.calls import CapabilityCall, InvokeResult, Preview
from copenhagen.core.schema import validate_values
from copenhagen.secrets.broker import CredentialBroker


class DomainActivities:
    def __init__(
        self,
        queue: str,
        *,
        env: str = "prod",
        backends: Mapping[str, str] | None = None,
        credentials: frozenset[str] = frozenset(),
        signing_key: str | None = None,
        dev_override: bool = False,
        plugins: str = "",
        extra_adapters: Mapping[str, Adapter] | None = None,
    ) -> None:
        self.queue = queue
        key = signing_key or os.environ.get("COPENHAGEN_AUTHORIZATION_KEY", "dev-control-key")
        if env == "prod" and (key == "dev-control-key" or dev_override):
            raise ValueError("production worker requires authorization key; no dev overrides")
        self.broker = CredentialBroker(queue, key, credentials)
        ctx = AdapterContext(
            env=env, backends=dict(backends or {}), allow_insecure_backends=dev_override
        )
        self.adapters: dict[str, Adapter] = build(ctx, plugins)
        for name, adapter in (extra_adapters or {}).items():
            if name in self.adapters:
                raise ValueError(f"adapter {name!r} already registered")
            if env == "prod" and not getattr(adapter, "production_ready", True):
                raise ValueError(f"adapter {name!r} is development-only and refused in production")
            self.adapters[name] = adapter

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
            if isinstance(adapter, CallPoller)
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
