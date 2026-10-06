"""Queue-scoped credential release requires a signed, input-bound authorization."""

import hashlib
import hmac

from copenhagen.core.calls import CapabilityCall, Credential
from copenhagen.core.canonical import canonical_json, idempotency_key, inputs_hash
from copenhagen.secrets.store import EnvSecretStore


def authorization(key: str, call: CapabilityCall) -> str:
    payload = {
        "tenant": call.tenant_id,
        "run": call.run_id,
        "step": call.step_id,
        "capability": call.capability.model_dump(mode="json", by_alias=True),
        "inputs_hash": call.inputs_hash,
    }
    return hmac.new(key.encode(), canonical_json(payload), hashlib.sha256).hexdigest()


class CredentialBroker:
    def __init__(self, queue: str, signing_key: str, credentials: frozenset[str]) -> None:
        self.queue = queue
        self.signing_key = signing_key
        self.credentials = credentials

    def get(self, call: CapabilityCall) -> Credential:
        if call.capability.executor.queue != self.queue:
            raise ValueError("worker queue does not own this capability")
        if inputs_hash(call.inputs, call.preview_artifact_hash) != call.inputs_hash:
            raise ValueError("resolved inputs hash mismatch")
        if call.idempotency_key != idempotency_key(call.run_id, call.step_id, call.inputs_hash):
            raise ValueError("invalid idempotency key")
        if not hmac.compare_digest(call.authorization, authorization(self.signing_key, call)):
            raise ValueError("control authorization missing or invalid")
        name = call.capability.executor.credential
        if name is None:
            return Credential(name="none", secret="")
        if name not in self.credentials:
            raise ValueError("credential is not assigned to this worker queue")
        return EnvSecretStore().get(name)
