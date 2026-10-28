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

#: Largest magnitude a CLICK REAL holds (+/-3.4028235E+38).  pyrung keeps a
#: REAL at 64-bit precision but never outside this range.
REAL_MAX = 3.4028235e38


def real_in_range(value: Any) -> bool:
    """True when *value* fits a REAL: finite and within +/-``REAL_MAX``."""
    return -REAL_MAX <= value <= REAL_MAX


def check_real_value(value: Any, what: str) -> None:
    """Raise ``ValueError`` when a user-supplied REAL value is out of range.

    Non-numbers are left to the caller's own checks.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not real_in_range(value):
            raise ValueError(
                f"{what}: {value!r} is outside the REAL range -3.4028235E+38..3.4028235E+38"
            )


def store_value(value: Any, storage: StoreTransform) -> Any:
    """Apply *storage* or raise when its descriptor/value is invalid."""

    kind = storage.kind
    if kind == "identity":
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    if kind == "real":
        # Saturate at CLICK's REAL range: a compare, no float32 round trip.
        try:
            real = float(value)
        except OverflowError:  # an int too large for a float
            return REAL_MAX if value > 0 else -REAL_MAX
        if real > REAL_MAX:
            return REAL_MAX
        if real < -REAL_MAX:
            return -REAL_MAX
        return real
    if kind == "bool":
        return bool(value)
    if kind == "char":
        return "\x00" if value == "" else value
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
    "IDENTITY_STORE",
    "REAL_MAX",
    "StoreTransform",
    "check_real_literal",
    "check_real_value",
    "real_in_range",
    "store_value",
]
