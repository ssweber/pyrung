"""Drum crossing — intentional reverse frontier.

A drum output is selected by its *final* step after same-scan event, time,
jump, jog, and reset transitions.  ``current_step in emitting_steps OR held``
is therefore not a sound preimage over the occurrence-entry state: an event can
advance from a non-emitting entry step into an emitting final step.

Until the constraint algebra represents those transition controls, drum
reverse falls through.  The instruction's :meth:`advance_profile` remains the
operational path for current-step and completion channels.
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.crossings import BaseCrossing, register
from pyrung.core.crossing import (
    REVERSE_FALLTHROUGH,
    Constraint,
    CrossingContext,
    ReverseResult,
)
from pyrung.core.instruction.drums import EventDrumInstruction, TimeDrumInstruction


class DrumCrossing(BaseCrossing):
    """Defer drum output inversion to the instruction-owned advance path."""

    def reverse(
        self, instr: Any, rung: Any, target: Constraint, ctx: CrossingContext
    ) -> ReverseResult:
        return REVERSE_FALLTHROUGH


_DRUM = DrumCrossing()
register(EventDrumInstruction, _DRUM)
register(TimeDrumInstruction, _DRUM)
