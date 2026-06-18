"""Drum crossing — an output's value pins the current step to a static set.

A drum drives ``output[i] = pattern[step-1][i]`` from its compile-time pattern
matrix, so an output value inverts to *which steps emit it* — an exact
``Eq(current_step, {steps})`` — unioned with the held branch (a drum holds its
outputs when neither enabled nor reset):

    output[i] == v   <=>   (step in S where pattern[s][i] == v)  OR  (held)

with ``S`` read straight off ``instr.pattern``.  If no step emits ``v`` the value
can only be held.  The step transition itself (advance / jump / jog / reset) is
condition- and time-driven and stays the walker's sequencer domain — a target on
``current_step`` / the completion flag / the accumulator falls through.
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.crossings import BaseCrossing, register
from pyrung.core.crossing import (
    REVERSE_FALLTHROUGH,
    Constraint,
    CrossingContext,
    Eq,
    Prior,
    ReverseResult,
    disjoint,
    single,
)
from pyrung.core.instruction.drums import EventDrumInstruction, TimeDrumInstruction


class DrumCrossing(BaseCrossing):
    """Reverse a drum output back to the steps whose pattern emits its value."""

    def reverse(
        self, instr: Any, rung: Any, target: Constraint, ctx: CrossingContext
    ) -> ReverseResult:
        if not (isinstance(target, Eq) and len(target.values) == 1):
            return REVERSE_FALLTHROUGH
        tag = target.tag
        value = next(iter(target.values))
        outputs = getattr(instr, "outputs", ())
        pattern = getattr(instr, "pattern", ())
        index = next((i for i, out in enumerate(outputs) if out.name == tag), None)
        if index is None:
            return REVERSE_FALLTHROUGH  # current_step / completion / accumulator -> walker
        held = Prior(tag, tag, 1, 0)
        steps = frozenset(s + 1 for s, row in enumerate(pattern) if bool(row[index]) == value)
        if not steps:
            return single(held, exact=True)  # no step emits this value -> only held
        step_tag = instr.current_step.name
        return disjoint((Eq(step_tag, steps),), (held,), exact=True)


_DRUM = DrumCrossing()
register(EventDrumInstruction, _DRUM)
register(TimeDrumInstruction, _DRUM)
