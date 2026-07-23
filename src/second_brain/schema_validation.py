"""Small, dependency-free JSON Schema subset for frozen M0 contracts.

M0 intentionally validates only the keywords used by its checked-in schemas.
The M1 storage core will own the full safe-parser and canonicalization path.
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any

from .errors import SchemaValidationError
from .jsonio import canonical_json_bytes


_UTC_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$"
)


def validate_json_schema(instance: Any, schema: dict[str, Any]) -> None:
    """Validate an instance against the checked-in JSON Schema subset."""

    _validate(instance, schema, schema, "$")


def _validate(
    value: Any,
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    location: str,
) -> None:
    if "$ref" in schema:
        _validate(value, _resolve_ref(schema["$ref"], root_schema), root_schema, location)
        return

    if "const" in schema and not _json_equal(value, schema["const"]):
        _fail(location, f"must equal {schema['const']!r}")

    if "enum" in schema and not any(_json_equal(value, item) for item in schema["enum"]):
        _fail(location, f"must be one of {schema['enum']!r}")

    if "type" in schema:
        permitted = schema["type"]
        permitted_types = permitted if isinstance(permitted, list) else [permitted]
        if not any(_matches_type(value, item) for item in permitted_types):
            _fail(location, f"must have type {permitted_types!r}")

    if isinstance(value, dict):
        _validate_object(value, schema, root_schema, location)
    elif isinstance(value, list):
        _validate_array(value, schema, root_schema, location)
    elif isinstance(value, str):
        _validate_string(value, schema, location)
    elif _is_number(value):
        _validate_number(value, schema, location)


def _validate_object(
    value: dict[str, Any],
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    location: str,
) -> None:
    for key in schema.get("required", []):
        if key not in value:
            _fail(location, f"is missing required field {key!r}")

    properties = schema.get("properties", {})
    for key, child in value.items():
        child_location = f"{location}.{key}"
        if key in properties:
            _validate(child, properties[key], root_schema, child_location)
        elif schema.get("additionalProperties") is False:
            _fail(child_location, "is not an allowed field")
        elif isinstance(schema.get("additionalProperties"), dict):
            _validate(child, schema["additionalProperties"], root_schema, child_location)


def _validate_array(
    value: list[Any],
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    location: str,
) -> None:
    _check_size(value, schema, location)
    if schema.get("uniqueItems"):
        serialized = [canonical_json_bytes(item) for item in value]
        if len(serialized) != len(set(serialized)):
            _fail(location, "must not contain duplicate items")

    item_schema = schema.get("items")
    if isinstance(item_schema, dict):
        for index, item in enumerate(value):
            _validate(item, item_schema, root_schema, f"{location}[{index}]")


def _validate_string(value: str, schema: dict[str, Any], location: str) -> None:
    _check_size(value, schema, location)
    if "pattern" in schema and re.search(schema["pattern"], value) is None:
        _fail(location, f"must match pattern {schema['pattern']!r}")
    if schema.get("format") == "date-time":
        _validate_utc_timestamp(value, location)


def _validate_number(value: int | float, schema: dict[str, Any], location: str) -> None:
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if minimum is not None and value < minimum:
        _fail(location, f"must be at least {minimum}")
    if maximum is not None and value > maximum:
        _fail(location, f"must be at most {maximum}")


def _check_size(value: str | list[Any], schema: dict[str, Any], location: str) -> None:
    minimum = schema.get("minLength", schema.get("minItems"))
    maximum = schema.get("maxLength", schema.get("maxItems"))
    if minimum is not None and len(value) < minimum:
        _fail(location, f"must contain at least {minimum} item(s)")
    if maximum is not None and len(value) > maximum:
        _fail(location, f"must contain at most {maximum} item(s)")


def _matches_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": _is_number(value),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, False)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _json_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without Python's bool-is-int coercion."""

    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if left is None or right is None:
        return left is None and right is None
    if _is_number(left) and _is_number(right):
        return left == right
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if isinstance(left, list) or isinstance(right, list):
        return (
            isinstance(left, list)
            and isinstance(right, list)
            and len(left) == len(right)
            and all(_json_equal(item, other) for item, other in zip(left, right))
        )
    if isinstance(left, dict) or isinstance(right, dict):
        return (
            isinstance(left, dict)
            and isinstance(right, dict)
            and left.keys() == right.keys()
            and all(_json_equal(left[key], right[key]) for key in left)
        )
    return False


def _resolve_ref(reference: str, root_schema: dict[str, Any]) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise SchemaValidationError(f"unsupported external schema reference: {reference}")

    current: Any = root_schema
    for segment in reference[2:].split("/"):
        current = current[segment.replace("~1", "/").replace("~0", "~")]
    if not isinstance(current, dict):
        raise SchemaValidationError(f"schema reference is not an object: {reference}")
    return current


def parse_rfc3339_utc(value: str) -> datetime:
    """Parse the strict UTC timestamp form mandated by the blueprint."""

    if _UTC_TIMESTAMP.fullmatch(value) is None:
        raise SchemaValidationError(f"timestamp must be RFC3339 UTC with Z suffix: {value!r}")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SchemaValidationError(f"invalid RFC3339 timestamp: {value!r}") from error


def _validate_utc_timestamp(value: str, location: str) -> None:
    try:
        parse_rfc3339_utc(value)
    except SchemaValidationError as error:
        _fail(location, str(error))


def _fail(location: str, message: str) -> None:
    raise SchemaValidationError(f"{location} {message}")
