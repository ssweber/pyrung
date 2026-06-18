"""Shift-register crossing — a true bit's provenance is its neighbour one scan back.

On a clock rising edge a shift register sets bit[0] to the rung (data) condition
and copies each later bit from its lower neighbour; with no edge it holds.  So a
*True* cell inverts to an exact disjunction:

- ``bit[k] == True`` (k >= 1): came from ``bit[k-1]`` on a clock edge **or** was
  already True and held — ``Prior(bit_k, bit_{k-1})`` OR ``Prior(bit_k, bit_k)``.
- ``bit[0] == True``: the data/rung condition drove it on an edge **or** it held —
  ``CondAttr(True)`` OR ``Prior(bit_0, bit_0)``.

``== False`` falls through: the reset path drives every cell false, so a static
constraint there is vacuous (that branch is the consumer's to observe).
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.crossings import BaseCrossing, register
from pyrung.core.analysis.crossings._ranges import range_tags
from pyrung.core.crossing import (
    REVERSE_FALLTHROUGH,
    CondAttr,
    Constraint,
    CrossingContext,
    Eq,
    Prior,
    ReverseResult,
    disjoint,
)
from pyrung.core.instruction.advanced import ShiftInstruction


class ShiftCrossing(BaseCrossing):
    """Reverse a shift register: a True cell came from its neighbour or held."""

    def reverse(
        self, instr: Any, rung: Any, target: Constraint, ctx: CrossingContext
    ) -> ReverseResult:
        if not (isinstance(target, Eq) and len(target.values) == 1):
            return REVERSE_FALLTHROUGH
        tag = target.tag
        value = next(iter(target.values))
        if value is not True:
            return REVERSE_FALLTHROUGH  # reset drives False -> vacuous static constraint
        tags = range_tags(getattr(instr, "bit_range", None))
        if not tags:
            return REVERSE_FALLTHROUGH
        for k, bit in enumerate(tags):
            if getattr(bit, "name", None) == tag:
                held = Prior(tag, tag, 1, 0)
                if k == 0:  # data/rung condition drove it, or it held
                    return disjoint((CondAttr(expected=True),), (held,), exact=True)
                neighbour = Prior(tag, tags[k - 1].name, 1, 0)  # came from lower cell on edge
                return disjoint((neighbour,), (held,), exact=True)
        return REVERSE_FALLTHROUGH


register(ShiftInstruction, ShiftCrossing())
