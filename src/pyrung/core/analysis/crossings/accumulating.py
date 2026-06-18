"""Counter / timer crossings — the done-bit inverts to an accumulator inequality.

A counter or timer sets its done bit by comparing its accumulator to a preset:

- ``count_up`` / ``on_delay``: ``done == (acc >= preset)``
- ``count_down``: ``done == (acc <= -preset)``

So ``done == True`` inverts to an exact-shaped :class:`Cmp` on the accumulator —
the answer to "why is it done?".  It is marked ``exact=False`` because the reset
path also forces ``done == False``: ``done == True`` *implies* ``acc >= preset``
(a sound necessary condition / superset) but is not equivalent to it.

Only the active-true direction is emitted.  ``done == False`` and the accumulator
itself fall through: ``done == False`` admits the reset/never-enabled states (no
clean accumulator bound), and the accumulator's predecessor is condition-driven
(reset makes any static predecessor constraint vacuous) — that chase is the
walker's value-stepping domain.  ``off_delay`` (TOF) inverts cleanly in neither
direction (enabled forces ``done == True`` regardless of ``acc``) and is a
registered fallthrough.
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.crossings import BaseCrossing, register
from pyrung.core.crossing import (
    REVERSE_FALLTHROUGH,
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


class _DoneBitCrossing(BaseCrossing):
    """Invert ``done == True`` to ``acc <op> preset``.  Subclasses set ``_op``."""

    _op = ">="  # comparison that makes the done bit true
    _negate_preset = False  # count_down compares against -preset

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
        if self._negate_preset:
            if is_tag:
                return REVERSE_FALLTHROUGH  # -preset of a tag is not a plain bound
            bound_value = -bound_value
        # exact=False: the reset path also forces done False, so acc <op> preset
        # is necessary for done==True but not sufficient.
        return single(Cmp(instr.accumulator.name, self._op, bound_value, bound_is_tag=is_tag))


class CountUpDoneCrossing(_DoneBitCrossing):
    """count_up / on_delay: ``done == True`` -> ``acc >= preset``."""

    _op = ">="


class CountDownDoneCrossing(_DoneBitCrossing):
    """count_down: ``done == True`` -> ``acc <= -preset`` (literal preset only)."""

    _op = "<="
    _negate_preset = True


class OffDelayCrossing(BaseCrossing):
    """off_delay (TOF) — registered fallthrough (no clean accumulator inversion)."""


register(CountUpInstruction, CountUpDoneCrossing())
register(CountDownInstruction, CountDownDoneCrossing())
register(OnDelayInstruction, CountUpDoneCrossing())
register(OffDelayInstruction, OffDelayCrossing())


__all__ = [
    "CountDownDoneCrossing",
    "CountUpDoneCrossing",
    "OffDelayCrossing",
]
