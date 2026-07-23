"""RFC 8785 (JCS) compatible JSON canonicalization helpers.

The storage ledger hashes logical JSON, so relying on ``json.dumps`` defaults is
not sufficient: Python's float spelling differs from ECMAScript at a few
important exponent boundaries.  This module deliberately accepts only JSON
values and emits the compact JCS representation used by M1.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any


_JSON_NUMBER = re.compile(r"^(-?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?)(\d+))?$")


def canonical_jcs_bytes(value: Any) -> bytes:
    """Return UTF-8 JCS bytes for a JSON-compatible value.

    JCS operates on the JSON data model, not arbitrary Python objects.  Being
    strict here prevents accidental hashing of e.g. dataclasses or Decimal
    instances through a surprising string conversion.
    """

    return _encode(value).encode("utf-8")


def sha256_hex(value: Any) -> str:
    """Return the SHA-256 digest of JCS bytes for ``value``."""

    return hashlib.sha256(canonical_jcs_bytes(value)).hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Return the SHA-256 digest for already-serialized bytes."""

    return hashlib.sha256(data).hexdigest()


def _encode(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _encode_string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _encode_float(value)
    if isinstance(value, list) or isinstance(value, tuple):
        return "[" + ",".join(_encode(item) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JCS object keys must be strings")
        # Python compares Unicode strings by code point, matching JCS's UTF-16
        # ordering for valid scalar strings except astral/non-BMP ties.  Those
        # ties have no practical JSON key collision, and explicitly sorting by
        # UTF-16 code units closes that remaining RFC 8785 edge case.
        keys = sorted(value, key=lambda key: key.encode("utf-16be", "surrogatepass"))
        return "{" + ",".join(
            _encode_string(key) + ":" + _encode(value[key]) for key in keys
        ) + "}"
    raise TypeError(f"value is not JSON-compatible: {type(value).__name__}")


def _encode_string(value: str) -> str:
    # RFC 8785 delegates string serialization to ECMAScript JSON.stringify.
    # Escaping only control characters, quote, and backslash gives its shortest
    # portable form.  Lone surrogates are rejected because they are not Unicode
    # scalar values and cannot safely survive UTF-8 round trips.
    chunks: list[str] = ['"']
    for character in value:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise ValueError("JCS strings cannot contain lone surrogate code points")
        if character == '"':
            chunks.append('\\"')
        elif character == "\\":
            chunks.append("\\\\")
        elif character == "\b":
            chunks.append("\\b")
        elif character == "\t":
            chunks.append("\\t")
        elif character == "\n":
            chunks.append("\\n")
        elif character == "\f":
            chunks.append("\\f")
        elif character == "\r":
            chunks.append("\\r")
        elif codepoint < 0x20:
            chunks.append(f"\\u{codepoint:04x}")
        else:
            chunks.append(character)
    chunks.append('"')
    return "".join(chunks)


def _encode_float(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("JCS does not permit non-finite numbers")
    if value == 0:
        return "0"

    # CPython's repr is the shortest correctly rounded decimal representation
    # of an IEEE-754 double.  Reformat that representation to the ECMAScript
    # threshold rules required by JCS (decimal for [1e-6, 1e21)).
    rendered = repr(value)
    match = _JSON_NUMBER.fullmatch(rendered)
    if match is None:  # Defensive; repr(float) is specified to be numeric.
        raise ValueError("cannot canonicalize float")
    sign, integer, fraction, exponent_sign, exponent_digits = match.groups()
    digits = integer + (fraction or "")
    decimal_index = len(integer)
    exponent = int((exponent_sign or "") + exponent_digits) if exponent_digits else 0
    decimal_index += exponent

    # Remove the trailing .0 that Python uses for whole floats.
    digits = digits.rstrip("0") or "0"
    if digits == "0":
        return "0"

    absolute = abs(value)
    if 1e-6 <= absolute < 1e21:
        if decimal_index <= 0:
            result = "0." + ("0" * -decimal_index) + digits
        elif decimal_index >= len(digits):
            result = digits + ("0" * (decimal_index - len(digits)))
        else:
            result = digits[:decimal_index] + "." + digits[decimal_index:]
        return sign + result

    # Scientific form has one leading digit and a minimally-spelled exponent.
    scientific_exponent = decimal_index - 1
    mantissa = digits[0]
    if len(digits) > 1:
        mantissa += "." + digits[1:]
    exponent_text = f"+{scientific_exponent}" if scientific_exponent >= 0 else str(scientific_exponent)
    return sign + mantissa + "e" + exponent_text
