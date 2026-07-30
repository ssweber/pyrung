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


def store_value(value: Any, storage: StoreTransform) -> Any:
    """Apply *storage* or raise when its descriptor/value is invalid."""

    kind = storage.kind
    if kind == "identity":
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    if kind == "real":
        return float(value)
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


__all__ = ["IDENTITY_STORE", "StoreTransform", "store_value"]
