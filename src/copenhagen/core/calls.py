"""Data exchanged between control and domain zones; credentials never cross Temporal."""

from typing import Any, Literal

from pydantic import Field

from copenhagen.core.capability import CapabilitySpec, Spec


class CapabilityCall(Spec, frozen=True):
    tenant_id: str
    run_id: str
    step_id: str
    capability: CapabilitySpec
    inputs: dict[str, Any]
    inputs_hash: str
    idempotency_key: str
    authorization: str = ""
    preview_artifact_hash: str | None = None
    attempt: int = 1


class InvokeResult(Spec, frozen=True):
    ok: bool
    outputs: dict[str, Any] = Field(default_factory=dict)
    error_type: Literal["retryable", "not_retryable", "unknown_outcome", "needs_human"] | None = (
        None
    )
    message: str = ""
    retry_after: int | None = None
    handle: str | None = None
    happened: bool | None = None


class Preview(Spec, frozen=True):
    summary: str
    artifact_hash: str | None = None


class Credential(Spec, frozen=True):
    name: str
    secret: str = Field(repr=False)
