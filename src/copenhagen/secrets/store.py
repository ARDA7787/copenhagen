"""Environment-only vendor credentials, available only to the matching worker."""

import os

from copenhagen.core.calls import Credential
from copenhagen.secrets.redaction import register


class EnvSecretStore:
    def get(self, name: str) -> Credential:
        key = "COPENHAGEN_SECRET_" + name.upper().replace("-", "_")
        value = os.environ.get(key)
        if not value:
            raise ValueError(f"credential {name} unavailable on this worker")
        register(value)
        return Credential(name=name, secret=value)


def redact(value: str) -> str:
    for key, secret in os.environ.items():
        if key.startswith("COPENHAGEN_SECRET_") and secret:
            value = value.replace(secret, "[REDACTED]")
    return value
