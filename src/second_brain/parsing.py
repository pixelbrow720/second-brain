"""Strict JSON, NDJSON, and deliberately-small YAML frontmatter parsing."""

from __future__ import annotations

import json
import re
from typing import Any

from .errors import DuplicateKeyError, ParseError
from .jsonio import loads_strict_json


_INTEGER = re.compile(r"^-?(?:0|[1-9][0-9]*)$")
_NUMBER = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$")
_MAPPING = re.compile(r"^([^:#][^:]*?):(?:[ \t]*(.*))?$")


def normalize_line_endings(text: str) -> str:
    """Normalize CRLF/CR input to LF and reject a Unicode BOM explicitly."""

    if not isinstance(text, str):
        raise ParseError("serialized input must be text")
    if text.startswith("\ufeff"):
        raise ParseError("UTF-8 BOM is not allowed")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_strict_json(text: str) -> Any:
    """Parse one JSON value without duplicate keys or non-JSON constants."""

    normalized = normalize_line_endings(text)
    try:
        return loads_strict_json(normalized)
    except (DuplicateKeyError, ValueError, json.JSONDecodeError) as error:
        raise ParseError("invalid strict JSON") from error


def parse_ndjson(text: str) -> list[dict[str, Any]]:
    """Parse strict, object-only NDJSON with no blank or partial records."""

    normalized = normalize_line_endings(text)
    if not normalized:
        return []
    if normalized.endswith("\n"):
        normalized = normalized[:-1]
    if not normalized:
        return []

    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(normalized.split("\n"), start=1):
        if not line.strip():
            raise ParseError(f"blank NDJSON record at line {line_number}")
        try:
            record = parse_strict_json(line)
        except ParseError as error:
            raise ParseError(f"invalid NDJSON record at line {line_number}") from error
        if not isinstance(record, dict):
            raise ParseError(f"NDJSON record at line {line_number} must be an object")
        records.append(record)
    return records


def parse_markdown_object(text: str) -> dict[str, Any]:
    """Parse a Markdown object with exactly one leading YAML-subset block.

    The returned mapping is the logical record: frontmatter fields plus the
    normalized Markdown body under ``body``.  The physical frontmatter never
    accepts a ``body`` field because that would make the authority ambiguous.
    """

    normalized = normalize_line_endings(text)
    if not normalized.startswith("---\n"):
        raise ParseError("Markdown object must begin with frontmatter")
    lines = normalized.split("\n")
    closing_index: int | None = None
    for index in range(1, len(lines)):
        if lines[index] == "---":
            closing_index = index
            break
        if lines[index] == "...":
            raise ParseError("multiple YAML documents are not allowed")
    if closing_index is None:
        raise ParseError("Markdown frontmatter is not closed")

    header = "\n".join(lines[1:closing_index])
    parsed = parse_yaml_frontmatter(header)
    if not isinstance(parsed, dict):
        raise ParseError("Markdown frontmatter must be a mapping")
    if "body" in parsed:
        raise ParseError("body belongs after frontmatter, not inside it")

    body = "\n".join(lines[closing_index + 1 :])
    # A second leading delimiter would be a second frontmatter document, not a
    # Markdown horizontal rule.  This narrow rule avoids an ambiguous authority
    # file while leaving ordinary delimiters later in the body untouched.
    if body.startswith("---\n") or body == "---":
        raise ParseError("multiple Markdown frontmatter blocks are not allowed")
    parsed["body"] = body
    return parsed


def parse_yaml_frontmatter(text: str) -> Any:
    """Parse the intentionally narrow, non-executable YAML subset for M1."""

    normalized = normalize_line_endings(text)
    if not normalized:
        return {}
    tokens: list[_YamlLine] = []
    for number, raw_line in enumerate(normalized.split("\n"), start=1):
        if "\t" in raw_line:
            raise ParseError(f"tabs are not allowed in YAML frontmatter (line {number})")
        if raw_line.strip() in {"", "#"} or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        content = raw_line[indent:]
        if content in {"---", "..."}:
            raise ParseError("multiple YAML documents are not allowed")
        _reject_yaml_feature(content, number)
        tokens.append(_YamlLine(indent=indent, content=content, number=number))
    if not tokens:
        return {}
    try:
        value, index = _parse_block(tokens, 0, tokens[0].indent)
    except DuplicateKeyError as error:
        raise ParseError("duplicate YAML key") from error
    if index != len(tokens):
        raise ParseError(f"unexpected YAML content at line {tokens[index].number}")
    return value


class _YamlLine:
    __slots__ = ("indent", "content", "number")

    def __init__(self, *, indent: int, content: str, number: int) -> None:
        self.indent = indent
        self.content = content
        self.number = number


def _parse_block(tokens: list[_YamlLine], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(tokens):
        raise ParseError("expected nested YAML value")
    if tokens[index].indent != indent:
        raise ParseError(f"invalid YAML indentation at line {tokens[index].number}")
    if _is_list_item(tokens[index].content):
        return _parse_list(tokens, index, indent)
    return _parse_mapping(tokens, index, indent)


def _parse_mapping(
    tokens: list[_YamlLine], index: int, indent: int
) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(tokens):
        token = tokens[index]
        if token.indent < indent:
            break
        if token.indent > indent:
            raise ParseError(f"unexpected YAML indentation at line {token.number}")
        if _is_list_item(token.content):
            raise ParseError(f"mixed list and mapping at line {token.number}")
        match = _MAPPING.fullmatch(token.content)
        if match is None:
            raise ParseError(f"invalid YAML mapping at line {token.number}")
        key = _parse_key(match.group(1), token.number)
        if key in result:
            raise DuplicateKeyError(f"duplicate YAML key: {key}")
        raw_value = match.group(2)
        index += 1
        if raw_value is None or raw_value == "":
            if index < len(tokens) and tokens[index].indent > indent:
                value, index = _parse_block(tokens, index, tokens[index].indent)
            else:
                value = None
        else:
            value = _parse_scalar(raw_value, token.number)
            if index < len(tokens) and tokens[index].indent > indent:
                raise ParseError(f"scalar has nested YAML content at line {tokens[index].number}")
        result[key] = value
    return result, index


def _parse_list(tokens: list[_YamlLine], index: int, indent: int) -> tuple[list[Any], int]:
    result: list[Any] = []
    while index < len(tokens):
        token = tokens[index]
        if token.indent < indent:
            break
        if token.indent > indent:
            raise ParseError(f"unexpected YAML indentation at line {token.number}")
        if not _is_list_item(token.content):
            raise ParseError(f"mixed list and mapping at line {token.number}")
        raw_value = token.content[1:].lstrip(" ")
        index += 1
        if not raw_value:
            if index < len(tokens) and tokens[index].indent > indent:
                value, index = _parse_block(tokens, index, tokens[index].indent)
            else:
                value = None
        elif _looks_like_inline_mapping(raw_value):
            value, index = _parse_inline_list_mapping(tokens, index, indent, raw_value, token.number)
        else:
            value = _parse_scalar(raw_value, token.number)
            if index < len(tokens) and tokens[index].indent > indent:
                raise ParseError(f"scalar has nested YAML content at line {tokens[index].number}")
        result.append(value)
    return result, index


def _parse_inline_list_mapping(
    tokens: list[_YamlLine], index: int, list_indent: int, raw_value: str, line_number: int
) -> tuple[dict[str, Any], int]:
    match = _MAPPING.fullmatch(raw_value)
    if match is None:
        raise ParseError(f"invalid YAML mapping at line {line_number}")
    key = _parse_key(match.group(1), line_number)
    raw_child = match.group(2)
    result: dict[str, Any] = {}
    if raw_child is None or raw_child == "":
        if index < len(tokens) and tokens[index].indent > list_indent:
            value, index = _parse_block(tokens, index, tokens[index].indent)
        else:
            value = None
    else:
        value = _parse_scalar(raw_child, line_number)
    result[key] = value

    if index < len(tokens) and tokens[index].indent > list_indent:
        nested_indent = tokens[index].indent
        extra, index = _parse_mapping(tokens, index, nested_indent)
        for extra_key, extra_value in extra.items():
            if extra_key in result:
                raise DuplicateKeyError(f"duplicate YAML key: {extra_key}")
            result[extra_key] = extra_value
    return result, index


def _is_list_item(content: str) -> bool:
    return content == "-" or content.startswith("- ")


def _looks_like_inline_mapping(value: str) -> bool:
    return value[:1] not in "[{\"'" and _MAPPING.fullmatch(value) is not None


def _parse_key(raw_key: str, line_number: int) -> str:
    key = raw_key.strip()
    if not key or key.startswith(("&", "*", "!")):
        raise ParseError(f"unsupported YAML key at line {line_number}")
    if key.startswith('"'):
        try:
            decoded = parse_strict_json(key)
        except ParseError as error:
            raise ParseError(f"invalid quoted YAML key at line {line_number}") from error
        if not isinstance(decoded, str):
            raise ParseError(f"YAML key must be text at line {line_number}")
        return decoded
    if key.startswith("'"):
        if not key.endswith("'") or len(key) < 2:
            raise ParseError(f"invalid quoted YAML key at line {line_number}")
        return key[1:-1].replace("''", "'")
    if any(character.isspace() for character in key):
        raise ParseError(f"YAML key cannot contain whitespace at line {line_number}")
    return key


def _parse_scalar(raw_value: str, line_number: int) -> Any:
    value = _strip_comment(raw_value).strip()
    if not value:
        return ""
    if value.startswith(("&", "*", "!")):
        raise ParseError(f"unsupported YAML feature at line {line_number}")
    if value.startswith(("|", ">")):
        raise ParseError(f"multiline YAML scalars are not supported at line {line_number}")
    if value.startswith('"'):
        try:
            decoded = parse_strict_json(value)
        except ParseError as error:
            raise ParseError(f"invalid double-quoted YAML scalar at line {line_number}") from error
        if not isinstance(decoded, str):
            raise ParseError(f"quoted YAML scalar must be text at line {line_number}")
        return decoded
    if value.startswith("'"):
        if not value.endswith("'") or len(value) < 2:
            raise ParseError(f"invalid single-quoted YAML scalar at line {line_number}")
        return value[1:-1].replace("''", "'")
    if value.startswith(("[", "{")):
        try:
            return parse_strict_json(value)
        except ParseError as error:
            raise ParseError(f"invalid JSON flow value at line {line_number}") from error
    if value in {"null", "~"}:
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    if value in {".nan", ".NaN", ".inf", ".Inf", "-.inf", "-.Inf"}:
        raise ParseError(f"non-finite YAML number at line {line_number}")
    if _INTEGER.fullmatch(value):
        return int(value)
    if _NUMBER.fullmatch(value):
        parsed = float(value)
        if not (parsed == parsed and abs(parsed) != float("inf")):
            raise ParseError(f"non-finite YAML number at line {line_number}")
        return parsed
    if ": " in value or value.endswith(":"):
        raise ParseError(f"ambiguous YAML scalar at line {line_number}")
    return value


def _strip_comment(value: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    for index, character in enumerate(value):
        if in_double and escaped:
            escaped = False
            continue
        if in_double and character == "\\":
            escaped = True
            continue
        if character == '"' and not in_single:
            in_double = not in_double
        elif character == "'" and not in_double:
            in_single = not in_single
        elif character == "#" and not in_single and not in_double:
            if index == 0 or value[index - 1].isspace():
                return value[:index]
    return value


def _reject_yaml_feature(content: str, line_number: int) -> None:
    # Anchors, aliases, and tags are rejected only in YAML token position, so a
    # normal title such as "C* parser" remains valid plain text.
    stripped = content.lstrip()
    if stripped.startswith(("&", "*", "!")):
        raise ParseError(f"unsupported YAML feature at line {line_number}")
    mapping = _MAPPING.fullmatch(content)
    if mapping is not None:
        scalar = (mapping.group(2) or "").lstrip()
        if scalar.startswith(("&", "*", "!")):
            raise ParseError(f"unsupported YAML feature at line {line_number}")
