"""The supported JSON Schema subset; no coercion, open objects or floating-point money."""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from copenhagen.core.canonical import canonical_json
from copenhagen.core.conditions import check_safe_pattern

FieldMap = dict[str, dict[str, Any]]
KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "items",
        "required",
        "additionalProperties",
        "enum",
        "const",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minItems",
        "maxItems",
        "description",
        "title",
        "default",
        "sensitive",
        "unit",
    }
)
TYPES = frozenset({"string", "integer", "boolean", "object", "array", "null"})


def is_sensitive(spec: dict[str, Any]) -> bool:
    return (
        bool(spec.get("sensitive"))
        or any(is_sensitive(child) for child in spec.get("properties", {}).values())
        or (spec.get("type") == "array" and is_sensitive(spec.get("items", {})))
    )


def check_fields(fields: FieldMap) -> None:
    for name, spec in fields.items():
        if not name or not name.isidentifier():
            raise ValueError(f"invalid field name: {name}")
        check_schema(spec, top=True)


def check_schema(spec: dict[str, Any], *, top: bool = False) -> None:
    extra = set(spec) - KEYWORDS
    if extra:
        raise ValueError(f"unsupported schema keywords: {sorted(extra)}")
    if spec.get("type") not in TYPES:
        raise ValueError("each field needs a supported type (numbers use integer units)")
    if "sensitive" in spec and type(spec["sensitive"]) is not bool:
        raise ValueError("sensitive must be boolean")
    if isinstance(spec.get("pattern"), str):
        check_safe_pattern(spec["pattern"])
    if top and "required" in spec and type(spec["required"]) is not bool:
        raise ValueError("field-level required must be boolean")
    if spec["type"] == "object":
        if spec.get("additionalProperties", False) is not False:
            raise ValueError("open object schemas are not supported")
        for child in spec.get("properties", {}).values():
            check_schema(child)
    if spec["type"] == "array":
        if "items" not in spec:
            raise ValueError("array requires items schema")
        check_schema(spec["items"])
    clean = {k: v for k, v in spec.items() if not (top and k == "required")}
    Draft202012Validator.check_schema(clean)
    canonical_json(spec)


def object_schema(fields: FieldMap) -> dict[str, Any]:
    def closed(spec: dict[str, Any]) -> dict[str, Any]:
        result = dict(spec)
        if result.get("type") == "object":
            result["additionalProperties"] = False
            result["properties"] = {k: closed(v) for k, v in result.get("properties", {}).items()}
        if result.get("type") == "array":
            result["items"] = closed(result["items"])
        return result

    return {
        "type": "object",
        "properties": {
            k: closed({a: b for a, b in v.items() if a != "required"}) for k, v in fields.items()
        },
        "required": [k for k, v in fields.items() if v.get("required", True)],
        "additionalProperties": False,
    }


def validate_values(fields: FieldMap, values: dict[str, Any]) -> None:
    canonical_json(values)
    errors = sorted(
        Draft202012Validator(object_schema(fields), format_checker=FormatChecker()).iter_errors(  # pyright: ignore[reportUnknownMemberType]
            values
        ),
        key=lambda e: str(e.path),
    )
    if errors:
        raise ValueError(
            "; ".join(
                f"{'.'.join(str(p) for p in e.path) or 'inputs'}: {e.message}" for e in errors
            )
        )
