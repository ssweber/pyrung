"""Type-range helpers shared by the value-clamping crossings.

The integer types saturate-clamp on store (``_store_copy_value_to_tag_type`` /
``_clamp_int`` / ``_clamp_dint`` in ``instruction/conversions.py``), so a reverse
must know a type's rails to decide whether a target value sits at a clamp
boundary (where many source values collapse to one) or in the interior (where
the copy is value-preserving and the inverse is exact).
"""

from __future__ import annotations

import math
from typing import Any

from pyrung.core.instruction.conversions import copy_store_transform
from pyrung.core.memory_block import IndirectExprRef, IndirectRef
from pyrung.core.tag import TagType


def type_bounds(tag_type: TagType | None) -> tuple[int, int] | None:
    """The ``(min, max)`` saturating bounds of *tag_type*, or ``None``."""
    if tag_type not in (TagType.INT, TagType.DINT, TagType.WORD):
        return None
    transform = copy_store_transform(tag_type)
    if transform is None or transform.lower is None or transform.upper is None:
        return None
    return transform.lower, transform.upper


def stored_value_possible(tag_type: TagType | None, value: Any) -> bool:
    """Whether *value* belongs to the concrete stored domain of *tag_type*."""
    bounds = type_bounds(tag_type)
    if bounds is not None:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and float(value).is_integer()
            and bounds[0] <= value <= bounds[1]
        )
    if tag_type is TagType.REAL:
        return (
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        )
    if tag_type is TagType.BOOL:
        return isinstance(value, bool)
    if tag_type is TagType.CHAR:
        return isinstance(value, str) and len(value) == 1
    return False


def clamps_on_store(tag_type: TagType | None) -> bool:
    """Whether a copy/fill into *tag_type* saturate-clamps (INT/DINT)."""
    return tag_type in (TagType.INT, TagType.DINT)


def wraps_on_store(tag_type: TagType | None) -> bool:
    """Whether a calc into *tag_type* modular-wraps its integer range."""
    return tag_type in (TagType.INT, TagType.DINT, TagType.WORD)


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
