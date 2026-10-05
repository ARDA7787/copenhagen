"""Canonical JSON, content hashes and idempotency keys.

Everything Copenhagen hashes or signs goes through this module: approval hashes (I5),
idempotency keys (I4), the audit hash chain (I9) and published capability versions (I8).
Two values that mean the same thing must produce the same bytes, and two values that differ
must not.

Pure and workflow-safe: no clock, no randomness, no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "CanonicalError",
    "canonical_json",
    "idempotency_key",
    "inputs_hash",
    "sha256_hex",
]


class CanonicalError(ValueError):
    """The value cannot be canonicalised (float, naive datetime, non-string key, ...)."""


def canonical_json(value: Any) -> bytes:
    """RFC 8785-style canonical JSON as UTF-8 bytes."""
    raise NotImplementedError


def sha256_hex(value: Any) -> str:
    """Lowercase hex sha256 of ``canonical_json(value)``."""
    raise NotImplementedError


def inputs_hash(inputs: Mapping[str, Any], preview_artifact_hash: str | None = None) -> str:
    """The hash an approval is bound to (I5)."""
    raise NotImplementedError


def idempotency_key(run_id: str, step_id: str, inputs_hash_hex: str) -> str:
    """``run_id:step_id:inputs_hash[:12]`` (I4). No attempt number, so retries reuse it."""
    raise NotImplementedError
