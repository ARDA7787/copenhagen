"""The supported JSON Schema subset; no coercion, open objects or floating-point money."""

from __future__ import annotations

from typing import Any, cast

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
# Money is an integer count of the tenant currency's minor units. ``amount_cents`` without
# a unit is still read as money so older capabilities keep their ceiling.
MONEY_UNITS = frozenset({"cents"})
LEGACY_MONEY_FIELD = "amount_cents"


def is_money(name: str | None, spec: dict[str, Any]) -> bool:
    return spec.get("type") == "integer" and (
        spec.get("unit") in MONEY_UNITS or (name == LEGACY_MONEY_FIELD and "unit" not in spec)
    )


def money_paths(fields: FieldMap) -> list[str]:
    """Dotted paths of every declared money field (``[]`` marks array items)."""

    found: list[str] = []

    def walk(path: str, name: str | None, spec: dict[str, Any]) -> None:
        if is_money(name, spec):
            found.append(path)
        for key, child in spec.get("properties", {}).items():
            walk(f"{path}.{key}", key, child)
        if spec.get("type") == "array":
            walk(f"{path}[]", None, spec.get("items", {}))

    for name, spec in fields.items():
        walk(name, name, spec)
    return found


def money_values(fields: FieldMap, values: dict[str, Any]) -> list[Any]:
    """Every value supplied for a declared money field, including nested and array items."""

    found: list[Any] = []

    def walk(name: str | None, spec: dict[str, Any], value: Any) -> None:
        if value is None:
            return
        if is_money(name, spec):
            found.append(value)
        if isinstance(value, dict):
            mapping = cast(dict[str, Any], value)
            for key, child in spec.get("properties", {}).items():
                walk(key, child, mapping.get(key))
        if isinstance(value, list) and spec.get("type") == "array":
            for item in cast(list[Any], value):
                walk(None, spec.get("items", {}), item)

    for name, spec in fields.items():
        walk(name, spec, values.get(name))
    return found


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
    if "unit" in spec and not isinstance(spec["unit"], str):
        raise ValueError("unit must be a string")
    if spec.get("unit") in MONEY_UNITS and spec["type"] != "integer":
        raise ValueError("money fields must be integers in minor units")
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
