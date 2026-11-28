"""Invert a counter's done bit to a bound on the predecessor accumulator.

A counter or timer sets its done bit by comparing its accumulator to a preset:

- ``count_up`` / ``on_delay``: ``done == (acc >= preset)``
- ``count_down``: ``done == (acc <= preset)`` (as on a CLICK: it starts at 0,
  so a preset >= 0 is done from its first scan)

Counters change the accumulator before comparing it, by one count on a rising
edge of an input. So a count-up can finish from ``acc == preset - 1``, and a
count-down can finish from ``acc == preset + 1``. Their inexact reverse bounds
include that one-scan frontier. This gives a sound predecessor superset. Dynamic
presets use an explicit :class:`~pyrung.core.crossing.AffineCmp`, which keeps both
the preset dependency and the one-scan offset.

A timer can advance by an amount that depends on the scan duration and the unit.
That amount can include fractional hidden memory. Until those inputs can be
represented, both timer families deliberately fall through. ``done == False`` and
accumulator targets also fall through, because the reset and hold paths have no
single useful sound bound.
"""

from __future__ import annotations

from typing import Any, assert_never

from pyrung.core.analysis.crossings import BaseCrossing, register
from pyrung.core.crossing import (
    REVERSE_FALLTHROUGH,
    AffineCmp,
    Cmp,
    Constraint,
    CrossingContext,
    Eq,
    ReverseResult,
    single,
)
from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
from pyrung.core.instruction.operands import ConstArg, Preset, TagArg
from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction


class _CounterDoneCrossing(BaseCrossing):
    """Invert ``done == True`` to a sound one-scan predecessor bound."""

    _op = ">="  # comparison that makes the done bit true
    _frontier_offset = -1

    def reverse(
        self, instr: Any, rung: Any, target: Constraint, ctx: CrossingContext
    ) -> ReverseResult:
        if not (isinstance(target, Eq) and len(target.values) == 1):
            return REVERSE_FALLTHROUGH
        tag = target.tag
        value = next(iter(target.values))
        if tag != instr.done_bit.tag.name or value is not True:
            return REVERSE_FALLTHROUGH  # only the active-true done-bit direction
        acc = instr.accumulator.tag.name
        preset: Preset = instr.preset
        match preset:
            case TagArg(tag=preset_tag):
                return single(
                    AffineCmp(acc, self._op, preset_tag.name, offset=self._frontier_offset)
                )
            case ConstArg(value=bound):
                return single(Cmp(acc, self._op, bound + self._frontier_offset))
            case _:
                assert_never(preset)


class CountUpDoneCrossing(_CounterDoneCrossing):
    """Count-up: ``done`` can cross true from ``acc == preset - 1``."""

    _op = ">="
    _frontier_offset = -1


class CountDownDoneCrossing(_CounterDoneCrossing):
    """Count-down: ``done`` can cross true from ``acc == preset + 1``."""

    _op = "<="
    _frontier_offset = 1


class TimerDoneCrossing(BaseCrossing):
    """Timers fall through until dt, unit, and fractional state are constraints."""


register(CountUpInstruction, CountUpDoneCrossing())
register(CountDownInstruction, CountDownDoneCrossing())
register(OnDelayInstruction, TimerDoneCrossing())
register(OffDelayInstruction, TimerDoneCrossing())


__all__ = [
    "CountDownDoneCrossing",
    "CountUpDoneCrossing",
    "TimerDoneCrossing",
]
