"""Automatically generated module split."""

from __future__ import annotations

import math
import re
import struct
from typing import Any

from pyrung.core.storage import StoreTransform, real_in_range, store_char, store_value
from pyrung.core.tag import Tag

_DINT_MIN = -2147483648


_DINT_MAX = 2147483647


_INT_MIN = -32768


_INT_MAX = 32767


def copy_store_transform(tag_type: Any) -> StoreTransform | None:
    """Destination conversion used by copy, blockcopy, and fill."""
    from pyrung.core.tag import TagType

    if tag_type is TagType.INT:
        return StoreTransform("clamp", _INT_MIN, _INT_MAX)
    if tag_type is TagType.DINT:
        return StoreTransform("clamp", _DINT_MIN, _DINT_MAX)
    if tag_type is TagType.WORD:
        return StoreTransform("wrap", 0, 0xFFFF)
    if tag_type is TagType.REAL:
        return StoreTransform("real")
    if tag_type is TagType.BOOL:
        return StoreTransform("bool")
    if tag_type is TagType.CHAR:
        return StoreTransform("char")
    return None


def calc_store_transform(tag_type: Any, mode: str = "decimal") -> StoreTransform | None:
    """Destination conversion used by calc and packing result stores."""
    from pyrung.core.tag import TagType

    if mode == "hex" and tag_type in {TagType.INT, TagType.DINT, TagType.WORD}:
        return StoreTransform("wrap", 0, 0xFFFF)
    if tag_type is TagType.INT:
        return StoreTransform("wrap", _INT_MIN, _INT_MAX)
    if tag_type is TagType.DINT:
        return StoreTransform("wrap", _DINT_MIN, _DINT_MAX)
    if tag_type is TagType.WORD:
        return StoreTransform("wrap", 0, 0xFFFF)
    if tag_type is TagType.REAL:
        return StoreTransform("real")
    if tag_type is TagType.BOOL:
        return StoreTransform("bool")
    if tag_type is TagType.CHAR:
        return StoreTransform("char")
    return None


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
    if code < 0 or code > 255:
        raise ValueError("character code out of range")
    return chr(code)


def _as_single_ascii_char(value: Any) -> str:
    """The CHAR *value* stores as: one 8-bit character, a blank stored as NUL.

    A CLICK TXT is one byte: codes 128..255 are stored as they are.
    """
    if not isinstance(value, str):
        raise ValueError("CHAR value must be a string")
    if value == "":
        return store_char(value)
    if len(value) != 1 or ord(value) > 255:
        raise ValueError("CHAR value must be blank or one 8-bit character")
    return value


def _text_from_source_value(value: Any) -> str:
    """Source text for a text->numeric copy; a blank reads as one NUL."""
    if isinstance(value, str):
        return store_char(value)
    raise ValueError("text conversion source must resolve to str")


def _store_numeric_text_digits(text: str, targets: list[Tag], *, mode: str) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if len(text) != len(targets):
        raise ValueError("source/destination text length mismatch")

    for char, target in zip(text, targets, strict=True):
        if mode == "value":
            if char == "\x00":
                numeric = 0  # CLICK: Character Value of NUL writes 0, no fault
            elif char < "0" or char > "9":
                raise ValueError("Copy Character Value accepts only digits 0-9")
            else:
                numeric = ord(char) - ord("0")
        elif mode == "ascii":
            if ord(char) > 255:
                raise ValueError("Copy ASCII Code Value accepts 8-bit characters only")
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
        # CLICK: the parse stops at a decimal point ("12.5" is 12); any other
        # character that is not a digit refuses the text.  A value past the
        # destination clamps in the store, and the store raises the fault.
        match = re.fullmatch(r"([+-]?\d+)(?:\.\d*)?", text)
        if match is None:
            raise ValueError("integer parse failed")
        return int(match.group(1), 10)

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
    storage = copy_store_transform(tag.type)
    return value if storage is None else store_value(value, storage)


def _truncate_to_tag_type(value: Any, tag: Tag, mode: str = "decimal") -> Any:
    """Truncate a value to fit the destination tag's type.

    Implements hardware-verified modular wrapping used by calc() result stores:
    - INT: 16-bit signed (-32768 to 32767)
    - DINT: 32-bit signed (-2147483648 to 2147483647)
    - WORD: 16-bit unsigned (0 to 65535)
    - REAL: float, saturated at CLICK's range (+/-3.4028235E+38)
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
    storage = calc_store_transform(tag.type, mode)
    return value if storage is None else store_value(value, storage)


def _math_out_of_range_for_dest(value: Any, dest: Tag, mode: str) -> bool:
    """Return True if math result exceeds destination storage range."""
    from pyrung.core.tag import TagType

    if isinstance(value, float) and not math.isfinite(value):
        return False
    if dest.type == TagType.REAL and mode != "hex":
        # Past CLICK's REAL range: the store saturates, the flag says so.
        return isinstance(value, (int, float)) and not real_in_range(value)

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
