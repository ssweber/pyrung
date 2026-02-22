"""Automatically generated module split."""

from __future__ import annotations

import math
import re
import struct
from typing import Any

from pyrung.core.tag import Tag

_DINT_MIN = -2147483648


_DINT_MAX = 2147483647


_INT_MIN = -32768


_INT_MAX = 32767


def _clamp_dint(value: int) -> int:
    """Clamp integer to DINT (32-bit signed) range."""
    return max(_DINT_MIN, min(_DINT_MAX, value))


def _clamp_int(value: int) -> int:
    """Clamp integer to INT (16-bit signed) range."""
    return max(_INT_MIN, min(_INT_MAX, value))


def _int_to_float_bits(n: int) -> float:
    """Reinterpret a 32-bit unsigned integer bit pattern as IEEE 754 float."""
    return struct.unpack("<f", struct.pack("<I", int(n) & 0xFFFFFFFF))[0]


def _float_to_int_bits(f: float) -> int:
    """Reinterpret an IEEE 754 float bit pattern as 32-bit unsigned integer."""
    return struct.unpack("<I", struct.pack("<f", float(f)))[0]


def _ascii_char_from_code(code: int) -> str:
    if code < 0 or code > 127:
        raise ValueError("ASCII code out of range")
    return chr(code)


def _as_single_ascii_char(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("CHAR value must be a string")
    if value == "":
        return value
    if len(value) != 1 or ord(value) > 127:
        raise ValueError("CHAR value must be blank or one ASCII character")
    return value


def _text_from_source_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    raise ValueError("text conversion source must resolve to str")


def _store_numeric_text_digits(text: str, targets: list[Tag], *, mode: str) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if len(text) != len(targets):
        raise ValueError("source/destination text length mismatch")

    for char, target in zip(text, targets, strict=True):
        if mode == "value":
            if char < "0" or char > "9":
                raise ValueError("Copy Character Value accepts only digits 0-9")
            numeric = ord(char) - ord("0")
        elif mode == "ascii":
            if ord(char) > 127:
                raise ValueError("Copy ASCII Code Value accepts ASCII only")
            numeric = ord(char)
        else:
            raise ValueError(f"Unsupported text->numeric mode: {mode}")
        updates[target.name] = _store_copy_value_to_tag_type(numeric, target)
    return updates


def _format_int_text(value: int, width: int, suppress_zero: bool, *, signed: bool = True) -> str:
    if suppress_zero:
        return str(value)
    if not signed:
        return f"{value:0{width}X}"
    if value < 0:
        return f"-{abs(value):0{width}d}"
    return f"{value:0{width}d}"


def _render_text_from_numeric(
    value: Any,
    *,
    source_tag: Tag | None,
    suppress_zero: bool,
    pad: int | None = None,
    exponential: bool,
) -> str:
    from pyrung.core.tag import TagType

    source_type = source_tag.type if source_tag is not None else None
    if source_type == TagType.REAL or isinstance(value, float):
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("REAL source is not finite")
        return f"{numeric:.7E}" if exponential else f"{numeric:.7f}"

    number = int(value)
    effective_suppress_zero = suppress_zero if pad is None else False
    signed_width = max(pad - 1, 0) if pad is not None and number < 0 else pad

    if source_type == TagType.WORD:
        width = 4 if pad is None else pad
        return _format_int_text(number & 0xFFFF, width, effective_suppress_zero, signed=False)
    if source_type == TagType.DINT:
        width = 10 if signed_width is None else signed_width
        return _format_int_text(number, width, effective_suppress_zero)
    if source_type == TagType.INT:
        width = 5 if signed_width is None else signed_width
        return _format_int_text(number, width, effective_suppress_zero)
    if pad is None:
        return str(number) if suppress_zero else f"{number:05d}"
    width = 5 if signed_width is None else signed_width
    return _format_int_text(number, width, suppress_zero=False)


def _parse_pack_text_value(text: str, dest_tag: Tag) -> Any:
    from pyrung.core.tag import TagType

    if text == "":
        raise ValueError("empty text cannot be parsed")

    if dest_tag.type in {TagType.INT, TagType.DINT}:
        if not re.fullmatch(r"[+-]?\d+", text):
            raise ValueError("integer parse failed")
        parsed = int(text, 10)
        if dest_tag.type == TagType.INT and (parsed < _INT_MIN or parsed > _INT_MAX):
            raise ValueError("integer out of INT range")
        if dest_tag.type == TagType.DINT and (parsed < _DINT_MIN or parsed > _DINT_MAX):
            raise ValueError("integer out of DINT range")
        return parsed

    if dest_tag.type == TagType.WORD:
        if not re.fullmatch(r"[0-9A-Fa-f]+", text):
            raise ValueError("hex parse failed")
        parsed = int(text, 16)
        if parsed < 0 or parsed > 0xFFFF:
            raise ValueError("hex out of WORD range")
        return parsed

    if dest_tag.type == TagType.REAL:
        parsed = float(text)
        if not math.isfinite(parsed):
            raise ValueError("REAL parse produced non-finite value")
        # Ensure value can round-trip to 32-bit float
        struct.pack("<f", parsed)
        return parsed

    raise TypeError(
        f"pack_text destination must be INT, DINT, WORD, or REAL; got {dest_tag.type.name}"
    )


def _store_copy_value_to_tag_type(value: Any, tag: Tag) -> Any:
    """Store value using copy/blockcopy/fill conversion semantics.

    - INT and DINT use saturating clamp.
    - Other destination types preserve current conversion behavior.
    """
    from pyrung.core.tag import TagType

    # Match existing conversion behavior for non-finite float sentinels.
    if isinstance(value, float) and (
        value != value or value == float("inf") or value == float("-inf")
    ):
        return 0

    if tag.type == TagType.INT:
        return _clamp_int(int(value))

    if tag.type == TagType.DINT:
        return _clamp_dint(int(value))

    return _truncate_to_tag_type(value, tag)


def _truncate_to_tag_type(value: Any, tag: Tag, mode: str = "decimal") -> Any:
    """Truncate a value to fit the destination tag's type.

    Implements hardware-verified modular wrapping used by math() result stores:
    - INT: 16-bit signed (-32768 to 32767)
    - DINT: 32-bit signed (-2147483648 to 2147483647)
    - WORD: 16-bit unsigned (0 to 65535)
    - REAL: 32-bit float (no truncation, just cast)
    - BOOL: truthiness
    - CHAR: no truncation

    In "hex" mode, all integer types wrap at 16-bit unsigned (0-65535).

    Args:
        value: The computed value to truncate.
        tag: The destination tag (used for type info).
        mode: "decimal" (default signed) or "hex" (unsigned 16-bit).

    Returns:
        Value truncated to the tag's type range.
    """
    from pyrung.core.tag import TagType

    # Handle division-by-zero sentinels (inf, nan)
    if isinstance(value, float) and (
        value != value or value == float("inf") or value == float("-inf")
    ):
        return 0

    if mode == "hex":
        # Hex mode: unsigned 16-bit wrap for all integer types
        return int(value) & 0xFFFF

    tag_type = tag.type

    if tag_type == TagType.BOOL:
        return bool(value)

    if tag_type == TagType.REAL:
        return float(value)

    if tag_type == TagType.CHAR:
        return value

    # Integer truncation with signed wrapping
    int_val = int(value)

    if tag_type == TagType.INT:
        # 16-bit signed: wrap to -32768..32767
        return ((int_val + 0x8000) & 0xFFFF) - 0x8000

    if tag_type == TagType.DINT:
        # 32-bit signed: wrap to -2147483648..2147483647
        return ((int_val + 0x80000000) & 0xFFFFFFFF) - 0x80000000

    if tag_type == TagType.WORD:
        # 16-bit unsigned: wrap to 0..65535
        return int_val & 0xFFFF

    # Fallback: no truncation
    return value


def _math_out_of_range_for_dest(value: Any, dest: Tag, mode: str) -> bool:
    """Return True if math result exceeds destination storage range."""
    from pyrung.core.tag import TagType

    if isinstance(value, float) and not math.isfinite(value):
        return False

    try:
        int_value = int(value)
    except (TypeError, ValueError, OverflowError):
        return False

    if mode == "hex":
        return int_value < 0 or int_value > 0xFFFF

    if dest.type == TagType.INT:
        return int_value < _INT_MIN or int_value > _INT_MAX
    if dest.type == TagType.DINT:
        return int_value < _DINT_MIN or int_value > _DINT_MAX
    if dest.type == TagType.WORD:
        return int_value < 0 or int_value > 0xFFFF
    return False
