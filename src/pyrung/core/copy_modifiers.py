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
    return CopyModifier(mode="value", source=source)


def as_ascii(source: Any) -> CopyModifier:
    return CopyModifier(mode="ascii", source=source)


def as_text(
    source: Any,
    *,
    suppress_zero: bool = True,
    pad: int | None = None,
    exponential: bool = False,
    termination_code: int | str | None = None,
) -> CopyModifier:
    return CopyModifier(
        mode="text",
        source=source,
        suppress_zero=bool(suppress_zero),
        pad=_normalize_pad(pad),
        exponential=bool(exponential),
        termination_code=_normalize_termination_code(termination_code),
    )


def as_binary(source: Any) -> CopyModifier:
    return CopyModifier(mode="binary", source=source)


__all__ = [
    "CopyModifier",
    "CopyMode",
    "as_value",
    "as_ascii",
    "as_text",
    "as_binary",
]
