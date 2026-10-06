"""HTTP adapter: exact host allow-list, no redirects, explicit request mapping."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from copenhagen.core.calls import CapabilityCall, Credential, InvokeResult, Preview
from copenhagen.core.capability import ExecutorSpec


class HTTPAdapter:
    kind = "http"

    def __init__(
        self,
        backends: dict[str, str],
        *,
        dev_override: bool = False,
        env: str = "prod",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if dev_override and env == "prod":
            raise ValueError("development backend override forbidden in production")
        self.backends = backends
        self.dev_override = dev_override
        self.env = env
        self.client = client

    def base(self, executor: ExecutorSpec) -> str:
        if executor.backend not in self.backends:
            raise ValueError("backend not configured")
        base = self.backends[executor.backend].rstrip("/")
        parsed = urlsplit(base)
        if parsed.username or parsed.password or parsed.fragment or parsed.query:
            raise ValueError("invalid backend URL")
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("backend must be an HTTP URL")
        override = self.dev_override and parsed.hostname in {"localhost", "127.0.0.1", "mockworld"}
        if parsed.hostname not in executor.allowed_hosts and not override:
            raise ValueError("host outside capability allowed_hosts")
        if self.env == "prod" and parsed.scheme != "https":
            raise ValueError("production backends require HTTPS")
        return base

    async def validate_config(self, executor: ExecutorSpec) -> None:
        self.base(executor)

    async def preview(self, call: CapabilityCall, cred: Credential) -> Preview:
        if call.capability.risk.class_ == "infrastructure":
            raise ValueError("generic HTTP cannot provide a saved infrastructure artifact")
        return Preview(
            summary=f"{call.capability.executor.operation} on {call.capability.executor.backend}"
        )

    async def invoke(self, call: CapabilityCall, cred: Credential) -> InvokeResult:
        executor = call.capability.executor
        base = self.base(executor)
        method, path = executor.operation.split(" ", 1)
        if method not in {"GET", "HEAD"} and call.capability.risk.class_ == "read":
            raise ValueError("read capability cannot write")
        payload = dict(call.inputs)
        names = re.findall(r"\{([a-z][a-z0-9_]*)\}", path)
        for name in names:
            if name not in payload:
                raise ValueError(f"missing path input {name}")
            path = path.replace("{" + name + "}", quote(str(payload.pop(name)), safe=""))
        if not path.startswith("/") or path.startswith("//") or "://" in path or ".." in path:
            raise ValueError("invalid operation path")
        query = {name: payload.pop(name) for name in executor.query_inputs if name in payload}
        if method in {"GET", "HEAD"}:
            query.update(payload)
        payload = {executor.input_mapping.get(k, k): v for k, v in payload.items()}
        query = {executor.input_mapping.get(k, k): v for k, v in query.items()}
        headers = {
            **executor.headers,
            "Idempotency-Key": call.idempotency_key,
            "X-Copenhagen-Run": call.run_id,
        }
        if cred.secret:
            headers[executor.credential_header] = executor.credential_prefix + cred.secret
        own_client = self.client is None
        client = self.client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
        try:
            response = await client.request(
                method,
                base + path,
                params=query,
                json=payload
                if method not in {"GET", "HEAD"} and executor.body_encoding == "json"
                else None,
                data=payload
                if method not in {"GET", "HEAD"} and executor.body_encoding == "form"
                else None,
                headers=headers,
                timeout=executor.timeout_seconds,
                follow_redirects=False,
            )
            result = map_response(response, callback=executor.mode == "webhook_callback")
            if result.ok and executor.output_mapping:
                try:
                    result = result.model_copy(
                        update={
                            "outputs": {
                                key: extract(result.outputs, path)
                                for key, path in executor.output_mapping.items()
                            }
                        }
                    )
                except (KeyError, IndexError, TypeError, ValueError):
                    return InvokeResult(
                        ok=False,
                        error_type="unknown_outcome",
                        message="response mapping did not match backend output",
                    )
            return result
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return InvokeResult(
                ok=False,
                error_type="retryable",
                message="connection could not be established",
                happened=False,
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            return InvokeResult(
                ok=False, error_type="unknown_outcome", message="connection lost; verify outcome"
            )
        finally:
            if own_client:
                await client.aclose()

    async def poll(self, handle: str, cred: Credential) -> InvokeResult:
        # Opaque handles encode a configured backend and a job id, never an arbitrary URL.
        backend, job = handle.split(":", 1)
        if backend not in self.backends or not re.fullmatch(r"[a-zA-Z0-9_-]+", job):
            raise ValueError("invalid job handle")
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            response = await client.get(
                self.backends[backend].rstrip("/") + "/jobs/" + job,
                headers={"Authorization": f"Bearer {cred.secret}"} if cred.secret else {},
                timeout=30,
            )
            return map_response(response, callback=True)

    async def cancel(self, handle: str, cred: Credential) -> None:
        raise ValueError("cancel requires an explicit registered capability")

    async def poll_call(self, call: CapabilityCall, handle: str, cred: Credential) -> InvokeResult:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,256}", handle):
            raise ValueError("invalid job handle")
        executor = call.capability.executor.model_copy(
            update={
                "operation": call.capability.executor.poll_operation.replace("{job_id}", handle),
                "query_inputs": (),
                "input_mapping": {},
            }
        )
        polling = call.model_copy(
            update={
                "inputs": {},
                "capability": call.capability.model_copy(update={"executor": executor}),
            }
        )
        return await self.invoke(polling, cred)


def extract(value: Any, path: str) -> Any:
    for part in path.split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def map_response(response: httpx.Response, *, callback: bool = False) -> InvokeResult:
    status = response.status_code
    if status == 202 and callback:
        try:
            return InvokeResult(
                ok=False,
                error_type="needs_human",
                handle=response.json()["job_id"],
                message="asynchronous job pending",
            )
        except (KeyError, ValueError):
            return InvokeResult(
                ok=False, error_type="unknown_outcome", message="malformed asynchronous response"
            )
    if 200 <= status < 300:
        try:
            outputs = response.json() if response.content else {}
            if not isinstance(outputs, dict):
                raise ValueError("outputs must be an object")
            return InvokeResult(ok=True, outputs=outputs, happened=True)
        except ValueError:
            return InvokeResult(
                ok=False, error_type="unknown_outcome", message="malformed success response"
            )
    if status == 404:
        return InvokeResult(
            ok=False, error_type="not_retryable", message="not found", happened=False
        )
    if status == 429:
        raw = response.headers.get("Retry-After", "1")
        return InvokeResult(
            ok=False,
            error_type="retryable",
            message="rate limited",
            retry_after=min(int(raw), 300) if raw.isdigit() else 1,
            happened=False,
        )
    if status in {408, 500, 502, 503, 504}:
        return InvokeResult(
            ok=False, error_type="unknown_outcome", message=f"HTTP {status}; verify before retry"
        )
    return InvokeResult(ok=False, error_type="not_retryable", message=f"HTTP {status}")
