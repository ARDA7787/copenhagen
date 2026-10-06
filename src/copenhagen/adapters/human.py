"""Human work is a task with typed outputs, distinct from authorization approval."""

from copenhagen.core.calls import CapabilityCall, Credential, InvokeResult, Preview
from copenhagen.core.capability import ExecutorSpec


class HumanAdapter:
    kind = "human"

    async def validate_config(self, executor: ExecutorSpec) -> None:
        if executor.adapter != "human":
            raise ValueError("incorrect adapter")

    async def preview(self, call: CapabilityCall, cred: Credential) -> Preview:
        return Preview(summary=call.capability.description)

    async def invoke(self, call: CapabilityCall, cred: Credential) -> InvokeResult:
        return InvokeResult(ok=False, error_type="needs_human", message=call.capability.description)

    async def poll(self, handle: str, cred: Credential) -> InvokeResult:
        return InvokeResult(ok=False, error_type="needs_human", message="task pending")

    async def cancel(self, handle: str, cred: Credential) -> None:
        return None
