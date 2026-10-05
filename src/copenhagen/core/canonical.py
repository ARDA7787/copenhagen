"""Canonical JSON, content hashes and idempotency keys.

Everything Copenhagen hashes or signs goes through this module: approval hashes (I5),
idempotency keys (I4), the audit hash chain (I9) and published capability versions (I8).
Two values that mean the same thing must produce the same bytes, and two values that differ
must not.

Pure and workflow-safe: no clock, no randomness, no I/O.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any, cast

__all__ = [
    "MAX_SAFE_INT",
    "CanonicalError",
    "canonical_json",
    "idempotency_key",
    "inputs_hash",
    "sha256_hex",
]

#: Largest integer every JSON reader agrees on (I-JSON, RFC 7493).
MAX_SAFE_INT = 2**53 - 1

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_KEY_HASH_CHARS = 12


class CanonicalError(ValueError):
    """The value cannot be canonicalised (float, naive datetime, non-string key, ...)."""


def canonical_json(value: Any) -> bytes:
    """RFC 8785-style canonical JSON as UTF-8 bytes.

    Objects are sorted by the UTF-16 code units of their keys, there is no whitespace, and
    strings escape only ``"``, ``\\`` and control characters. Supported values: ``None``,
    ``bool``, integers within +/-(2**53 - 1), ``str``, enums (as their value), aware
    ``datetime`` (ISO 8601 UTC with ``Z``), lists, tuples and string-keyed mappings.
    Floats and ``Decimal`` are rejected at any depth: money is integer cents (C8).
    """
    parts: list[str] = []
    try:
        _encode(value, parts)
    except RecursionError:
        raise CanonicalError("value is nested too deeply (or contains a cycle)") from None
    return "".join(parts).encode("utf-8")


def sha256_hex(value: Any) -> str:
    """Lowercase hex sha256 of ``canonical_json(value)``."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


def inputs_hash(inputs: Mapping[str, Any], preview_artifact_hash: str | None = None) -> str:
    """The hash an approval is bound to (I5).

    It covers the step inputs and, for infrastructure steps, the preview artifact (C1), so
    changing either after approval invalidates the approval.
    """
    # Runtime guard: inputs often come from untyped JSON, so the annotation is not enough.
    if not isinstance(inputs, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise CanonicalError(f"inputs must be an object, got {type(inputs).__name__}")
    if preview_artifact_hash is not None:
        _require_sha256_hex(preview_artifact_hash, "preview_artifact_hash")
    return sha256_hex({"inputs": inputs, "preview_artifact_hash": preview_artifact_hash})


def idempotency_key(run_id: str, step_id: str, inputs_hash_hex: str) -> str:
    """``run_id:step_id:inputs_hash[:12]`` (I4). No attempt number, so retries reuse it."""
    for name, part in (("run_id", run_id), ("step_id", step_id)):
        if not isinstance(part, str) or not part:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise CanonicalError(f"{name} must be a non-empty string")
        if ":" in part:
            raise CanonicalError(f"{name} must not contain ':' (the key would be ambiguous)")
    _require_sha256_hex(inputs_hash_hex, "inputs_hash")
    return f"{run_id}:{step_id}:{inputs_hash_hex[:_KEY_HASH_CHARS]}"


# ---------------------------------------------------------------------------- internals


def _require_sha256_hex(value: object, name: str) -> None:
    if not isinstance(value, str) or _SHA256_HEX.fullmatch(value) is None:
        raise CanonicalError(f"{name} must be 64 lowercase hex characters")


def _encode(value: Any, out: list[str]) -> None:
    # Order matters: bool is an int, datetime is a date, StrEnum is a str.
    if isinstance(value, enum.Enum):
        _encode(value.value, out)
    elif value is None:
        out.append("null")
    elif isinstance(value, bool):
        out.append("true" if value else "false")
    elif isinstance(value, int):
        if not -MAX_SAFE_INT <= value <= MAX_SAFE_INT:
            raise CanonicalError(f"integer {value} is outside +/-(2**53 - 1)")
        out.append(str(int(value)))
    elif isinstance(value, str):
        out.append(_string(value))
    elif isinstance(value, datetime):
        out.append(_string(_utc_iso(value)))
    elif isinstance(value, Mapping):
        _object(cast(Mapping[Any, Any], value), out)
    elif isinstance(value, list | tuple):
        out.append("[")
        for i, item in enumerate(cast("list[Any] | tuple[Any, ...]", value)):
            if i:
                out.append(",")
            _encode(item, out)
        out.append("]")
    elif isinstance(value, float):
        raise CanonicalError("floats are not allowed; use integer units (money is cents)")
    elif isinstance(value, date):
        raise CanonicalError("a bare date is ambiguous; use an aware datetime")
    else:
        raise CanonicalError(f"cannot canonicalise {type(value).__name__}")


def _object(value: Mapping[Any, Any], out: list[str]) -> None:
    items: dict[str, Any] = {}
    for key, item in value.items():
        plain = key.value if isinstance(key, enum.Enum) else key
        if not isinstance(plain, str):
            raise CanonicalError(f"object keys must be strings, got {type(key).__name__}")
        plain = str(plain)
        if plain in items:
            raise CanonicalError(f"duplicate key {plain!r}")
        items[plain] = item
    # RFC 8785 section 3.2.3: sort by UTF-16 code units, not code points.
    keys = sorted(items, key=_utf16_units)
    out.append("{")
    for i, key in enumerate(keys):
        if i:
            out.append(",")
        out.append(_string(key))
        out.append(":")
        _encode(items[key], out)
    out.append("}")


def _utf16_units(key: str) -> bytes:
    try:
        return key.encode("utf-16-be")
    except UnicodeEncodeError:
        raise CanonicalError("string is not valid Unicode (lone surrogate)") from None


def _string(value: str) -> str:
    # str(value) drops str subclasses; json.dumps with ensure_ascii=False escapes exactly
    # '"', '\\' and U+0000..U+001F (short forms where they exist, else lowercase \u00xx),
    # which is what RFC 8785 section 3.2.2.2 requires.
    plain = str.__str__(value)
    try:
        plain.encode("utf-8")
    except UnicodeEncodeError:
        raise CanonicalError("string is not valid Unicode (lone surrogate)") from None
    return json.dumps(plain, ensure_ascii=False)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CanonicalError("datetimes must be timezone-aware")
    return value.astimezone(UTC).replace(tzinfo=None).isoformat() + "Z"
