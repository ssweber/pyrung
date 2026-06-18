"""Type-range helpers shared by the value-clamping crossings.

The integer types saturate-clamp on store (``_store_copy_value_to_tag_type`` /
``_clamp_int`` / ``_clamp_dint`` in ``instruction/conversions.py``), so a reverse
must know a type's rails to decide whether a target value sits at a clamp
boundary (where many source values collapse to one) or in the interior (where
the copy is value-preserving and the inverse is exact).
"""

from __future__ import annotations

from typing import Any

from pyrung.core.memory_block import IndirectExprRef, IndirectRef
from pyrung.core.tag import TagType

# Saturating bounds, mirroring instruction/conversions.py (INT/DINT clamp; WORD
# is 16-bit unsigned).  REAL/CHAR/BOOL do not participate in integer clamping.
_INT = (-32768, 32767)
_DINT = (-2147483647, 2147483647)
_WORD = (0, 65535)

_BOUNDS: dict[TagType, tuple[int, int]] = {
    TagType.INT: _INT,
    TagType.DINT: _DINT,
    TagType.WORD: _WORD,
}


def type_bounds(tag_type: TagType | None) -> tuple[int, int] | None:
    """The ``(min, max)`` saturating bounds of *tag_type*, or ``None``."""
    if tag_type is None:
        return None
    return _BOUNDS.get(tag_type)


def clamps_on_store(tag_type: TagType | None) -> bool:
    """Whether a copy/fill into *tag_type* saturate-clamps (INT/DINT)."""
    return tag_type in (TagType.INT, TagType.DINT)


def wraps_on_store(tag_type: TagType | None) -> bool:
    """Whether a calc into *tag_type* modular-wraps the signed range (INT/DINT)."""
    return tag_type in (TagType.INT, TagType.DINT)


def wrap_to_type(value: int, tag_type: TagType | None) -> int | None:
    """Wrap *value* into *tag_type*'s signed two's-complement range (INT/DINT).

    Mirrors ``_truncate_to_tag_type``'s modular wrap, so the inverse of an affine
    calc lands on the true (wrapped) source value rather than an out-of-range
    candidate.  Returns ``None`` for non-wrapping types.
    """
    bounds = type_bounds(tag_type)
    if bounds is None or not wraps_on_store(tag_type):
        return None
    lo, hi = bounds
    span = hi - lo + 1
    return ((value - lo) % span) + lo


def range_subset(inner: TagType | None, outer: TagType | None) -> bool:
    """Whether every value of *inner* fits in *outer* — so a store never clamps."""
    inner_bounds = type_bounds(inner)
    outer_bounds = type_bounds(outer)
    if inner_bounds is None or outer_bounds is None:
        return False
    lo, hi = inner_bounds
    olo, ohi = outer_bounds
    return olo <= lo and hi <= ohi


def range_tags(block_range: Any) -> list[Any] | None:
    """The element tags of a *static* block range, or ``None`` if indirect.

    Used by the value-clamping and pack/unpack crossings to align a target slot
    with its source slot without a ``ScanContext``; an indirect / unresolvable
    range yields ``None`` (the caller falls through to the idx-chase).
    """
    if isinstance(block_range, (IndirectRef, IndirectExprRef)):
        return None
    tags_fn = getattr(block_range, "tags", None)
    if tags_fn is None:
        return None
    try:
        return list(tags_fn())
    except (TypeError, IndexError):
        return None
