"""Immutable published capability contracts and cross-field safety rules."""

from __future__ import annotations

import re
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

from copenhagen.core.conditions import Condition
from copenhagen.core.schema import FieldMap, check_fields, validate_values

RiskClass = Literal[
    "read",
    "internal_write",
    "external_write",
    "financial",
    "identity",
    "infrastructure",
    "destructive",
    "legal",
]
Source = Literal["user", "trusted_output", "untrusted_output", "ai"]
HIGH_RISK = frozenset({"financial", "identity", "infrastructure", "destructive"})
NAME = r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$"
PIN = r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+@[1-9][0-9]*$"


class Spec(BaseModel, frozen=True, extra="forbid", populate_by_name=True):
    pass


class ExecutorSpec(Spec, frozen=True):
    adapter: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    backend: str
    operation: str
    queue: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    credential: str | None = None
    allowed_hosts: tuple[str, ...] = ()
    timeout_seconds: int = Field(default=30, ge=1, le=600, strict=True)
    mode: Literal["immediate", "webhook_callback"] = "immediate"
    path_inputs: tuple[str, ...] = ()
    query_inputs: tuple[str, ...] = ()
    body_encoding: Literal["json", "form"] = "json"
    input_mapping: dict[str, str] = Field(default_factory=dict)
    output_mapping: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    credential_header: str = "Authorization"
    credential_prefix: str = "Bearer "
    poll_operation: str = "GET /jobs/{job_id}"


class Risk(Spec, frozen=True):
    class_: RiskClass = Field(alias="class")
    reversible: bool = False
    external_effect: bool = False


class Approval(Spec, frozen=True):
    approver_role: str | None = None
    unattended_when: tuple[Condition, ...] | None = None
    expires_after: str = "48h"
    on_expiry: Literal["needs_attention", "fail_step"] = "needs_attention"

    @model_validator(mode="after")
    def valid(self) -> Self:
        duration_seconds(self.expires_after)
        if self.unattended_when == ():
            raise ValueError("unattended_when cannot be empty")
        return self


class Retry(Spec, frozen=True):
    mode: Literal["safe_only", "always", "never"] = "safe_only"
    max_attempts: int = Field(default=3, ge=1, le=10, strict=True)


class Verification(Spec, frozen=True):
    capability: str = Field(pattern=PIN)
    expect: Condition
    within: str = "10m"
    inputs: dict[str, Any] | None = None

    @model_validator(mode="after")
    def valid(self) -> Self:
        duration_seconds(self.within)
        return self


class Compensation(Spec, frozen=True):
    capability: str = Field(pattern=PIN)
    inputs: dict[str, Any] = Field(default_factory=dict)


class Data(Spec, frozen=True):
    classification: Literal["public", "internal", "confidential", "restricted"] = "internal"
    output_trust: Literal["trusted", "untrusted"] = "trusted"


class CapabilitySpec(Spec, frozen=True):
    api_version: Literal["copenhagen/v1"] = Field(default="copenhagen/v1", alias="apiVersion")
    kind: Literal["Capability"] = "Capability"
    name: str = Field(pattern=NAME)
    version: int = Field(ge=1, strict=True)
    status: Literal["draft", "active", "deprecated", "disabled"] = "active"
    owner: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    description: str = ""
    inputs: FieldMap = Field(default_factory=dict)
    outputs: FieldMap = Field(default_factory=dict)
    executor: ExecutorSpec
    risk: Risk
    approval: Approval = Field(default_factory=Approval)
    idempotency: dict[str, str] = Field(default_factory=dict)
    retry: Retry = Field(default_factory=Retry)
    verify: Verification | Literal["none"]
    verify_reason: str | None = None
    compensate: Compensation | str
    compensate_reason: str | None = None
    data: Data = Field(default_factory=Data)
    examples: tuple[dict[str, Any], ...] = ()

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"

    @model_validator(mode="after")
    def cross_fields(self) -> Self:
        check_fields(self.inputs)
        check_fields(self.outputs)
        if self.verify == "none" and not self.verify_reason:
            raise ValueError("verify=none requires verify_reason")
        if self.compensate == "none" and not self.compensate_reason:
            raise ValueError("compensate=none requires compensate_reason")
        if (
            isinstance(self.compensate, str)
            and self.compensate != "none"
            and re.fullmatch(PIN, self.compensate) is None
        ):
            raise ValueError("compensation capability must be pinned")
        if self.executor.adapter == "http":
            if not self.executor.allowed_hosts:
                raise ValueError("HTTP executor requires allowed_hosts")
            method, _, path = self.executor.operation.partition(" ")
            if method not in {
                "GET",
                "HEAD",
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
            } or not path.startswith("/"):
                raise ValueError("HTTP operation must be METHOD /path")
            if self.risk.class_ == "read" and method not in {"GET", "HEAD"}:
                raise ValueError("read capabilities cannot use write methods")
            if any("/" in h or ":" in h or "*" in h for h in self.executor.allowed_hosts):
                raise ValueError("allowed_hosts must contain exact hostnames")
            reserved = {"authorization", "host", "cookie", "idempotency-key", "x-copenhagen-run"}
            if any(
                k.lower() in reserved or "\n" in k + v or "\r" in k + v
                for k, v in self.executor.headers.items()
            ):
                raise ValueError("reserved or invalid static HTTP header")
            if (
                self.executor.credential_header.lower() in reserved - {"authorization"}
                or re.fullmatch(r"[A-Za-z0-9-]+", self.executor.credential_header) is None
                or any(c in self.executor.credential_prefix for c in "\r\n")
            ):
                raise ValueError("invalid credential header")
            if not set(self.executor.input_mapping) <= set(self.inputs):
                raise ValueError("input mapping names unknown fields")
            if not set(self.executor.output_mapping) <= set(self.outputs):
                raise ValueError("output mapping names unknown fields")
            if not self.executor.poll_operation.startswith("GET /"):
                raise ValueError("poll operation must be read-only GET /path")
        if self.approval.unattended_when:
            for condition in self.approval.unattended_when:
                if condition.parameter not in self.inputs:
                    raise ValueError(f"condition names missing input {condition.parameter}")
        for example in self.examples:
            validate_values(self.inputs, example["inputs"])
        return self


def duration_seconds(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)(s|m|h|d)", value)
    if match is None:
        raise ValueError("duration must be a positive integer followed by s, m, h or d")
    seconds = int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
    if seconds > 30 * 86400:
        raise ValueError("duration cannot exceed 30 days")
    return seconds
