"""Counter crossings — the done-bit inverts to a predecessor accumulator bound.

A counter or timer sets its done bit by comparing its accumulator to a preset:

- ``count_up`` / ``on_delay``: ``done == (acc >= preset)``
- ``count_down``: ``done == (acc <= -preset)``

Counters change the accumulator before comparing it. A count-up can therefore
finish from ``acc == preset - 1`` and a count-down from
``acc == -preset + 1``. Their inexact reverse bounds include that one-scan
frontier, producing a sound predecessor superset. Dynamic presets use an
explicit :class:`~pyrung.core.crossing.AffineCmp`, preserving both the preset
dependency and the one-scan offset.

Timers can advance by a scan-duration- and unit-dependent amount, including
fractional hidden memory. Until those inputs are representable, both timer
families deliberately fall through. ``done == False`` and accumulator targets
also fall through because reset/hold paths do not have one useful sound bound.
"""

from __future__ import annotations

from typing import Any

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
from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction


def _preset_bound(preset: Any) -> tuple[Any, bool] | None:
    """``(bound, bound_is_tag)`` for a preset literal or tag, else ``None``."""
    if isinstance(preset, bool):
        return None
    if isinstance(preset, (int, float)):
        return preset, False
    name = getattr(preset, "name", None)
    return (name, True) if name is not None else None


class _CounterDoneCrossing(BaseCrossing):
    """Invert ``done == True`` to a sound one-scan predecessor bound."""

    _op = ">="  # comparison that makes the done bit true
    _negate_preset = False  # count_down compares against -preset
    _frontier_offset = -1

    def reverse(
        self, instr: Any, rung: Any, target: Constraint, ctx: CrossingContext
    ) -> ReverseResult:
        if not (isinstance(target, Eq) and len(target.values) == 1):
            return REVERSE_FALLTHROUGH
        tag = target.tag
        value = next(iter(target.values))
        if tag != instr.done_bit.name or value is not True:
            return REVERSE_FALLTHROUGH  # only the active-true done-bit direction
        bound = _preset_bound(instr.preset)
        if bound is None:
            return REVERSE_FALLTHROUGH
        bound_value, is_tag = bound
        if is_tag:
            return single(
                AffineCmp(
                    instr.accumulator.name,
                    self._op,
                    bound_value,
                    scale=-1 if self._negate_preset else 1,
                    offset=self._frontier_offset,
                )
            )
        if self._negate_preset:
            bound_value = -bound_value
        return single(
            Cmp(
                instr.accumulator.name,
                self._op,
                bound_value + self._frontier_offset,
            )
        )


class CountUpDoneCrossing(_CounterDoneCrossing):
    """Count-up: ``done`` can cross true from ``acc == preset - 1``."""

    _op = ">="
    _frontier_offset = -1


class CountDownDoneCrossing(_CounterDoneCrossing):
    """Count-down: ``done`` can cross true from ``acc == -preset + 1``."""

    _op = "<="
    _negate_preset = True
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
