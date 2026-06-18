"""Type-range helpers shared by the value-clamping crossings.

The integer types saturate-clamp on store (``_store_copy_value_to_tag_type`` /
``_clamp_int`` / ``_clamp_dint`` in ``instruction/conversions.py``), so a reverse
must know a type's rails to decide whether a target value sits at a clamp
boundary (where many source values collapse to one) or in the interior (where
the copy is value-preserving and the inverse is exact).
"""

from __future__ import annotations

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


def range_subset(inner: TagType | None, outer: TagType | None) -> bool:
    """Whether every value of *inner* fits in *outer* — so a store never clamps."""
    inner_bounds = type_bounds(inner)
    outer_bounds = type_bounds(outer)
    if inner_bounds is None or outer_bounds is None:
        return False
    lo, hi = inner_bounds
    olo, ohi = outer_bounds
    return olo <= lo and hi <= ohi
