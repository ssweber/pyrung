from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ConvertMode = Literal["value", "ascii", "text", "binary"]


@dataclass(frozen=True)
class CopyConverter:
    """Typed wrapper for copy-family text conversion modes.

    Passed as ``convert=`` to :func:`copy`. Not used elsewhere.

    Examples::

        copy(DS[1], Txt[1], convert=to_text())
        copy(ModeChar, DS[1], convert=to_value)
        copy(ModeChar, DS[1], convert=to_ascii)
        copy(DS[1], Txt[1], convert=to_binary)
    """

    mode: ConvertMode
    suppress_zero: bool = True
    exponential: bool = False
    termination_code: int | None = None

    def __call__(self, **kwargs: object) -> CopyConverter:
        """Allow ``to_binary()`` to work the same as ``to_binary``."""
        if not kwargs:
            return self
        raise TypeError(f"converter with mode {self.mode!r} does not accept arguments")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _normalize_termination_code(value: int | str | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        # $XX hex notation (Click PLC convention, e.g. "$0D" → 13)
        if value.startswith("$") and len(value) >= 2:
            hex_part = value[1:]
            try:
                value = int(hex_part, 16)
            except ValueError:
                raise ValueError(
                    f"termination_code hex string {value!r} contains invalid hex digits"
                ) from None
        elif len(value) == 1:
            value = ord(value)
        else:
            raise ValueError(
                "termination_code string must be a single character or $XX hex notation"
            )
    if not isinstance(value, int):
        raise TypeError("termination_code must be int, str, or None")
    if value < 0 or value > 127:
        raise ValueError("termination_code must be in ASCII range 0..127")
    return value


# ---------------------------------------------------------------------------
# Converters
# ---------------------------------------------------------------------------


def to_text(
    *,
    suppress_zero: bool = True,
    exponential: bool = False,
    termination_code: int | str | None = None,
) -> CopyConverter:
    """Numeric → Text conversion.

    Corresponds to the Click PLC Copy Option 4a (Numeric→Text) and
    Option 4c (Float→Text).

    Args:
        suppress_zero: When True (default), leading zeros are omitted
            (Click PLC "Suppress zero"). When False, leading zeros fill
            the full digit width of the source data type.
        exponential: When True, use scientific notation
            (Click PLC "Exponential Numbering", Option 4c). Only
            applicable to Float sources.
        termination_code: An ASCII code (int 0–127), single character,
            or ``$XX`` hex string appended after the converted text.
            Corresponds to the Click PLC "Termination Code" option
            (C0-1x and C2-x CPUs).

    Examples::

        copy(DS[1], Txt[1], convert=to_text())
        copy(DS[1], Txt[1], convert=to_text(suppress_zero=False))
        copy(DF[1], Txt[1], convert=to_text(exponential=True))
        copy(DS[1], Txt[1], convert=to_text(termination_code=0))
        copy(DS[1], Txt[1], convert=to_text(termination_code="$0D"))
    """
    return CopyConverter(
        mode="text",
        suppress_zero=bool(suppress_zero),
        exponential=bool(exponential),
        termination_code=_normalize_termination_code(termination_code),
    )


to_value: CopyConverter = CopyConverter(mode="value")
"""Text → Numeric conversion using the character's face value.

Corresponds to Click PLC "Copy Character Value" (Option 4b).

Example::

    # CHAR '5' → numeric 5
    copy(ModeChar, DS[1], convert=to_value)
"""

to_ascii: CopyConverter = CopyConverter(mode="ascii")
"""Text → Numeric conversion using the ASCII code.

Corresponds to Click PLC "Copy ASCII Code Value" (Option 4b).

Example::

    # CHAR '5' → ASCII 53
    copy(ModeChar, DS[1], convert=to_ascii)
"""

to_binary: CopyConverter = CopyConverter(mode="binary")
"""Numeric → Text conversion as raw binary.

Corresponds to Click PLC "Copy Binary" (Option 4a). The numeric value
is stored directly as an ASCII character.

Example::

    # DS1=123 → '{' (ASCII 123)
    copy(DS[1], Txt[1], convert=to_binary)
"""


__all__ = [
    "CopyConverter",
    "ConvertMode",
    "to_text",
    "to_value",
    "to_ascii",
    "to_binary",
]
