from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

CopyMode = Literal["value", "ascii", "text", "binary"]


@dataclass(frozen=True)
class CopyModifier:
    """Typed wrapper for copy-family text conversion modes."""

    mode: CopyMode
    source: Any
    suppress_zero: bool = True
    pad: int | None = None
    exponential: bool = False
    termination_code: int | str | None = None


def _normalize_termination_code(value: int | str | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        if len(value) != 1:
            raise ValueError("termination_code string must be exactly one character")
        value = ord(value)
    if not isinstance(value, int):
        raise TypeError("termination_code must be int, str, or None")
    if value < 0 or value > 127:
        raise ValueError("termination_code must be in ASCII range 0..127")
    return value


def _normalize_pad(value: int | None) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int):
        raise TypeError("pad must be int or None")
    if value < 0:
        raise ValueError("pad must be >= 0")
    return value


def as_value(source: Any) -> CopyModifier:
    """Copy a Text source to a numeric destination using the character value.

    Corresponds to the Click PLC "Copy Character Value" option (Option 4b)
    for Text->Numeric copies.

    Example::

        # TXT1 = '5' -> DS1 = 5
        CopyInstruction(Source.as_value(), DS[1])
    """
    return CopyModifier(mode="value", source=source)


def as_ascii(source: Any) -> CopyModifier:
    """Copy a Text source to a numeric destination using the ASCII code.

    Corresponds to the Click PLC "Copy ASCII Code Value" option (Option 4b)
    for Text->Numeric copies.

    Example::

        # TXT1 = '5' -> DS1 = 53 (0x35)
        CopyInstruction(Source.as_ascii(), DS[1])
    """
    return CopyModifier(mode="ascii", source=source)


def as_text(
    source: Any,
    *,
    suppress_zero: bool = True,
    pad: int | None = None,
    exponential: bool = False,
    termination_code: int | str | None = None,
) -> CopyModifier:
    """Copy a numeric source to a Text destination as a string.

    Corresponds to the Click PLC Copy Option 4a (Numeric->Text) and
    Option 4c (Float->Text).

    Args:
        source: The numeric tag to convert.
        suppress_zero: When True (default), leading zeros are omitted
            (Click PLC "Suppress zero"). When False, leading zeros are
            written to fill the full digit width of the source data type
            (Click PLC "Do not Suppress zero").
        pad: Number of fixed digits in the output. Because Python
            strips leading zeros from integer literals (``00123`` is
            invalid), use ``pad`` to express the Click PLC "Fixed Digits"
            count with "Do not Suppress zero". For example,
            ``as_text(source, pad=5)`` produces ``"00123"`` for a value
            of 123. Implies ``suppress_zero=False``.
        exponential: When True, use exponential / scientific notation
            (Click PLC "Exponential Numbering", Option 4c). Only
            applicable to Float sources.
        termination_code: An ASCII code (int 0-127) or single character
            appended after the converted text. Corresponds to the Click
            PLC "Termination Code" option (supported by C0-1x and C2-x
            CPUs).

    Examples::

        # Suppress zero (default): DS1=123 -> TXT1-TXT3 = "123"
        CopyInstruction(Source.as_text(), TXT[1])

        # Do not suppress zero: DS1=123 -> TXT1-TXT5 = "00123"
        CopyInstruction(Source.as_text(suppress_zero=False), TXT[1])

        # Fixed digits with pad: DS1=123 -> TXT1-TXT5 = "00123"
        CopyInstruction(Source.as_text(pad=5), TXT[1])

        # Exponential: DF1=10000 -> "1.0000000E+04"
        CopyInstruction(Source.as_text(exponential=True), TXT[1])

        # Termination code: append a null character after the text
        CopyInstruction(Source.as_text(termination_code=0), TXT[1])
    """
    return CopyModifier(
        mode="text",
        source=source,
        suppress_zero=bool(suppress_zero),
        pad=_normalize_pad(pad),
        exponential=bool(exponential),
        termination_code=_normalize_termination_code(termination_code),
    )


def as_binary(source: Any) -> CopyModifier:
    """Copy a numeric source to a Text destination as its raw binary value.

    Corresponds to the Click PLC "Copy Binary" option (Option 4a) for
    Numeric->Text copies. The numeric value is stored directly as an
    ASCII character.

    Example::

        # DS1=123 (0x7B) -> TXT1 = '{' (ASCII 123)
        CopyInstruction(Source.as_binary(), TXT[1])
    """
    return CopyModifier(mode="binary", source=source)


__all__ = [
    "CopyModifier",
    "CopyMode",
    "as_value",
    "as_ascii",
    "as_text",
    "as_binary",
]
