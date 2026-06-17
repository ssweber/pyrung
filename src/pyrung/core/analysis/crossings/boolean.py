"""Boolean coil crossing (Phase 2) — registered placeholder.

A Boolean coil's energisation is decided by its rung *condition*, not by the
``OutInstruction`` itself.  Reversing ``coil == value`` to input contacts is the
job of ``attribute()`` (``sp_tree.py``), which walks the rung's SP-tree — and
``recorded_cause`` / ``why_cause`` already call it directly on the rung.  The
per-instruction ``reverse(instr, ...)`` signature has no access to that SP-tree,
so :class:`BoolCrossing` cannot wrap ``attribute()`` here; it is a registered
fallthrough.

It exists so ``OutInstruction`` is an explicit cell in the coverage map (a
deliberate "condition-level, see attribute()" decision, not an accidental gap),
and marks the seam where a future condition-aware projected consumer — one that
carries the rung's SP-tree — would wire Boolean reversal in.
"""

from __future__ import annotations

from pyrung.core.analysis.crossings import BaseCrossing, register
from pyrung.core.instruction.coils import OutInstruction


class BoolCrossing(BaseCrossing):
    """Boolean coil — registered fallthrough (attribution is condition-level)."""


register(OutInstruction, BoolCrossing())
