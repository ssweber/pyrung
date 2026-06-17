"""Pack / unpack crossings (Phase 2) — registered fallthroughs.

Packing (bits / words / text into a register) and unpacking are lossy or
variable-width transforms — "transform-chasing", which the corridor-walker plan
keeps conservative (ordering / advice only, no precise sub-goal).  Each of the
five classes is registered as a fallthrough so the gap is an explicit, asserted
cell in the coverage map rather than a silent omission.
"""

from __future__ import annotations

from pyrung.core.analysis.crossings import BaseCrossing, register
from pyrung.core.instruction.packing import (
    PackBitsInstruction,
    PackTextInstruction,
    PackWordsInstruction,
    UnpackToBitsInstruction,
    UnpackToWordsInstruction,
)


class PackCrossing(BaseCrossing):
    """Pack / unpack — registered fallthrough (lossy / variable-width)."""


_PACK = PackCrossing()
register(PackBitsInstruction, _PACK)
register(PackWordsInstruction, _PACK)
register(PackTextInstruction, _PACK)
register(UnpackToBitsInstruction, _PACK)
register(UnpackToWordsInstruction, _PACK)
