"""Neutral destination-store semantics shared by runtime and analysis."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StoreTransform:
    """One concrete conversion applied when a value enters destination storage."""

    kind: str = "identity"
    lower: int | None = None
    upper: int | None = None


IDENTITY_STORE = StoreTransform()

#: Largest finite magnitude a CLICK REAL holds (+/-3.4028235E+38).  pyrung
#: keeps a REAL at 64-bit precision but never outside this range: a result
#: past it is +/-Inf, as a CLICK's float32 Math stores it.
REAL_MAX = 3.4028235e38

#: The blank CHAR.  A CLICK TXT is one byte, so blank and NUL ($00) are the
#: same value: an unwritten TXT, ``fill("")`` and ``reset()`` all leave NUL.
BLANK_CHAR = "\x00"


def store_char(value: Any) -> Any:
    """The value a CHAR holds when *value* is stored: ``""`` becomes NUL.

    Every path that stores into a CHAR (instruction writes, reset, patch and
    force, tag defaults) goes through here, so the engine has one blank.
    """
    return BLANK_CHAR if value == "" else value


def real_in_range(value: Any) -> bool:
    """True when *value* is a finite REAL: within +/-``REAL_MAX`` (not Inf, not NaN)."""
    return -REAL_MAX <= value <= REAL_MAX


def check_real_value(value: Any, what: str, *, allow_non_finite: bool = False) -> None:
    """Raise ``ValueError`` when a user-supplied REAL value is out of range.

    A finite value past +/-``REAL_MAX`` is always refused: no float32 holds it.
    +/-Inf and NaN are refused too unless *allow_non_finite*: a REAL register
    holds them (a Math overflow stores +/-Inf), so a patch or force may write
    one, but a constant or default cannot be one.  Non-numbers are left to the
    caller's own checks.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if real_in_range(value):
            return
        if allow_non_finite and isinstance(value, float) and not math.isfinite(value):
            return
        raise ValueError(
            f"{what}: {value!r} is outside the REAL range -3.4028235E+38..3.4028235E+38"
        )


def store_real(value: Any) -> float:
    """The value a REAL holds when *value* is stored: past +/-``REAL_MAX`` it is +/-Inf.

    A CLICK REAL is a float32, so a result past its range is +/-Inf (not a
    saturated +/-``REAL_MAX``).  Inf and NaN are kept.  pyrung keeps a finite
    value at 64-bit precision: a compare, no float32 round trip.
    """
    try:
        real = float(value)
    except OverflowError:  # an int too large for a float
        return math.inf if value > 0 else -math.inf
    if real > REAL_MAX:
        return math.inf
    if real < -REAL_MAX:
        return -math.inf
    return real


def store_value(value: Any, storage: StoreTransform) -> Any:
    """Apply *storage* or raise when its descriptor/value is invalid."""

    kind = storage.kind
    if kind == "identity":
        return value
    if kind == "real":
        return store_real(value)
    if isinstance(value, float) and not math.isfinite(value):
        # A clamp pins +/-Inf at its bound; NaN, and anything that wraps, is 0.
        if kind == "clamp" and value == value and storage.lower is not None:
            return storage.upper if value > 0 else storage.lower
        return 0
    if kind == "bool":
        return bool(value)
    if kind == "char":
        return store_char(value)
    if kind not in {"clamp", "wrap"}:
        raise ValueError(f"unknown store transform: {kind!r}")
    if storage.lower is None or storage.upper is None:
        raise ValueError(f"{kind} store requires inclusive bounds")

    integer = int(value)
    if kind == "clamp":
        return max(storage.lower, min(storage.upper, integer))

    span = storage.upper - storage.lower + 1
    if span <= 0:
        raise ValueError("wrap store requires an ordered, non-empty range")
    return ((integer - storage.lower) % span) + storage.lower


def check_real_literal(value: Any, dest: Any, what: str) -> None:
    """Refuse a literal *value* stored into a REAL *dest* (a tag, an indirect
    reference or a range) outside CLICK's REAL range."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return
    dest_type = getattr(dest, "type", None)
    if dest_type is None:
        dest_type = getattr(getattr(dest, "block", None), "type", None)
    if getattr(dest_type, "name", None) == "REAL":
        check_real_value(value, what)


__all__ = [
    "BLANK_CHAR",
    "IDENTITY_STORE",
    "REAL_MAX",
    "StoreTransform",
    "check_real_literal",
    "check_real_value",
    "real_in_range",
    "store_char",
    "store_real",
    "store_value",
]
