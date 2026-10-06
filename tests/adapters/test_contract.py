import httpx
import pytest

from copenhagen.adapters.base import invoke_checked
from copenhagen.adapters.fake import FakeAdapter, World
from copenhagen.adapters.http import HTTPAdapter, map_response
from copenhagen.core.calls import CapabilityCall, Credential
from copenhagen.core.canonical import idempotency_key, inputs_hash
from copenhagen.db.store import transaction
from copenhagen.registry.publish import capability
from copenhagen.secrets.broker import CredentialBroker, authorization


def call(service):
    with transaction(service.engine) as session:
        cap = capability(session, service.tenant, "payments.refund@1")
    values = {"payment_id": "pi_123", "amount_cents": 30000, "reason": "duplicate"}
    h = inputs_hash(values)
    return CapabilityCall(
        tenant_id="demo",
        run_id="run",
        step_id="refund",
        capability=cap,
        inputs=values,
        inputs_hash=h,
        idempotency_key=idempotency_key("run", "refund", h),
    )


async def test_allowed_hosts_and_redirect_refusal(service):
    invocation = call(service)
    adapter = HTTPAdapter({"stripe": "https://attacker.example"})
    with pytest.raises(ValueError, match="allowed_hosts"):
        await adapter.invoke(invocation, Credential(name="none", secret=""))
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://attacker.example"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = HTTPAdapter({"stripe": "https://api.stripe.com"}, client=client)
        result = await adapter.invoke(invocation, Credential(name="none", secret=""))
    assert result.error_type == "not_retryable"
    assert len(requests) == 1
    assert requests[0].headers["Idempotency-Key"] == invocation.idempotency_key


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, "not_retryable"),
        (401, "not_retryable"),
        (404, "not_retryable"),
        (429, "retryable"),
        (500, "unknown_outcome"),
        (504, "unknown_outcome"),
    ],
)
def test_error_mapping(status, expected):
    result = map_response(httpx.Response(status))
    assert result.error_type == expected


@pytest.mark.invariant("I2")
@pytest.mark.invariant("I4")
async def test_credential_broker_requires_queue_and_hash(service):
    invocation = call(service)
    broker = CredentialBroker("finance", "test-key", frozenset())
    with pytest.raises(ValueError, match="authorization"):
        broker.get(invocation)
    signed = invocation.model_copy(update={"authorization": authorization("test-key", invocation)})
    assert broker.get(signed).name == "none"
    with pytest.raises(ValueError, match="queue"):
        CredentialBroker("people", "test-key", frozenset()).get(signed)
    with pytest.raises(ValueError, match="hash mismatch"):
        broker.get(signed.model_copy(update={"inputs": {**signed.inputs, "amount_cents": 750000}}))


async def test_commit_then_drop_survives_new_adapter(service, tmp_path):
    invocation = call(service)
    cap = invocation.capability.model_copy(
        update={
            "executor": invocation.capability.executor.model_copy(
                update={"adapter": "fake", "operation": "refund"}
            )
        }
    )
    invocation = invocation.model_copy(update={"capability": cap})
    world = World(str(tmp_path / "world.sqlite"))
    world.fault("refund", "commit_then_drop")
    result = await invoke_checked(
        FakeAdapter(world), invocation, Credential(name="none", secret="")
    )
    assert result.error_type == "unknown_outcome"
    restarted = World(str(tmp_path / "world.sqlite"))
    assert restarted.invoke(
        "get_effect", "verify", {"idempotency_key": invocation.idempotency_key}
    ).ok
    assert len(restarted.effects()) == 1


async def test_real_api_request_and_response_mapping(service):
    invocation = call(service)
    executor = invocation.capability.executor.model_copy(
        update={
            "body_encoding": "form",
            "input_mapping": {"payment_id": "payment_intent"},
            "output_mapping": {"refund_id": "data.id", "status": "data.status"},
            "credential_header": "X-Api-Key",
            "credential_prefix": "",
        }
    )
    invocation = invocation.model_copy(
        update={"capability": invocation.capability.model_copy(update={"executor": executor})}
    )
    requests = []

    def backend(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"data": {"id": "re_real", "status": "succeeded"}, "other_vendor_metadata": True},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as client:
        result = await invoke_checked(
            HTTPAdapter({"stripe": "https://api.stripe.com"}, client=client),
            invocation,
            Credential(name="vendor", secret="test-token"),
        )
    assert result.ok
    assert result.outputs == {"refund_id": "re_real", "status": "succeeded"}
    assert requests[0].headers["X-Api-Key"] == "test-token"
    assert requests[0].headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert b"payment_intent=pi_123" in requests[0].content


async def test_poll_uses_same_origin_mapping_and_credential(service):
    invocation = call(service)
    executor = invocation.capability.executor.model_copy(
        update={
            "mode": "webhook_callback",
            "poll_operation": "GET /operations/{job_id}",
            "output_mapping": {"refund_id": "id", "status": "status"},
        }
    )
    invocation = invocation.model_copy(
        update={"capability": invocation.capability.model_copy(update={"executor": executor})}
    )
    requests = []

    def backend(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "re_job", "status": "succeeded"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as client:
        adapter = HTTPAdapter({"stripe": "https://api.stripe.com"}, client=client)
        result = await adapter.poll_call(invocation, "job_1", Credential(name="none", secret=""))
        assert result.ok
        assert result.outputs["refund_id"] == "re_job"
        assert requests[0].url.path == "/operations/job_1"
        with pytest.raises(ValueError, match="job handle"):
            await adapter.poll_call(
                invocation, "https://evil.invalid", Credential(name="none", secret="")
            )


def test_worker_logs_redact_released_credentials_including_exceptions(monkeypatch):
    import io
    import logging

    from copenhagen.secrets.redaction import RedactingFormatter
    from copenhagen.secrets.store import EnvSecretStore

    monkeypatch.setenv("COPENHAGEN_SECRET_TEST", "private-backend-token")
    credential = EnvSecretStore().get("test")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter())
    logger = logging.Logger("isolated-test")
    logger.addHandler(handler)
    try:
        raise ValueError("provider included " + credential.secret)
    except ValueError:
        logger.exception("credential=%s", credential.secret)
    assert "private-backend-token" not in stream.getvalue()
    assert "[REDACTED]" in stream.getvalue()
    assert "private-backend-token" not in repr(credential)
