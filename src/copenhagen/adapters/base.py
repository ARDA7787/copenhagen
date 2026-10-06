"""All adapter calls pass through the same schema and timeout boundary."""

import asyncio
from typing import Protocol, runtime_checkable

from copenhagen.core.calls import CapabilityCall, Credential, InvokeResult, Preview
from copenhagen.core.capability import ExecutorSpec
from copenhagen.core.schema import validate_values


class Adapter(Protocol):
    kind: str

    async def validate_config(self, executor: ExecutorSpec) -> None: ...
    async def preview(self, call: CapabilityCall, cred: Credential) -> Preview: ...
    async def invoke(self, call: CapabilityCall, cred: Credential) -> InvokeResult: ...
    async def poll(self, handle: str, cred: Credential) -> InvokeResult: ...
    async def cancel(self, handle: str, cred: Credential) -> None: ...


@runtime_checkable
class CallPoller(Protocol):
    """Adapters whose job status needs the original call (for its executor config)."""

    async def poll_call(
        self, call: CapabilityCall, handle: str, cred: Credential
    ) -> InvokeResult: ...


async def invoke_checked(adapter: Adapter, call: CapabilityCall, cred: Credential) -> InvokeResult:
    validate_values(call.capability.inputs, call.inputs)
    await adapter.validate_config(call.capability.executor)
    try:
        async with asyncio.timeout(call.capability.executor.timeout_seconds):
            result = await adapter.invoke(call, cred)
    except TimeoutError:
        return InvokeResult(
            ok=False, error_type="unknown_outcome", message="call timed out; verify first"
        )
    if result.ok:
        try:
            validate_values(call.capability.outputs, result.outputs)
        except ValueError:
            # A write may have landed even though its response was malformed.
            return InvokeResult(
                ok=False,
                error_type="unknown_outcome"
                if call.capability.risk.class_ != "read"
                else "not_retryable",
                message="backend outputs do not match capability schema",
            )
    return result
