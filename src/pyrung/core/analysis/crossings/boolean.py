"""Coil crossings (OUT / SET / RST) — condition-level attribution.

A coil's value is decided by its rung *condition*, so these crossings emit a
:class:`CondAttr` — "the written tag's value this scan equals (rung condition ==
expected)" — which the consumer resolves through the rung SP-tree
(``attribute()``).  ``reverse`` receives the ``rung`` precisely so this family
can.

- **OUT** is level-driven: ``coil == enabled``.  ``coil == value`` inverts to a
  single ``CondAttr(expected=value)`` (exact, unless one-shot — then the edge
  makes it necessary but not sufficient).  A non-Boolean target is unsatisfiable.
- **SET** only ever writes True (else it holds), so ``coil == True`` is *fired*
  (``CondAttr(True)``) **or** *held* (``Prior`` — chase the coil one scan back);
  ``coil == False`` can only be *held* (the latch never drives it false) — a
  single ``Prior``.  That "SET can't drive False" is the value-polarity oracle:
  a latch is never the writer that cleared a bit.
- **RST** is the mirror about the target's default value.

The held branch is a :class:`Prior` so the recorded resolver reads the prior
scan and the projected resolver recurses — one constraint, two resolvers.
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.crossings import BaseCrossing, register
from pyrung.core.crossing import (
    REVERSE_FALLTHROUGH,
    CondAttr,
    Constraint,
    CrossingContext,
    Eq,
    Prior,
    ReverseResult,
    disjoint,
    single,
    unsatisfiable,
)
from pyrung.core.instruction.coils import LatchInstruction, OutInstruction, ResetInstruction


def _eq_single(target: Constraint) -> tuple[str, Any] | None:
    if isinstance(target, Eq) and len(target.values) == 1:
        return target.tag, next(iter(target.values))
    return None


def _held(tag: str) -> Prior:
    """The coil kept its prior-scan value (chase the same target at N-1)."""
    return Prior(tag, tag, scale=1, offset=0)


class OutCrossing(BaseCrossing):
    """OUT: ``coil == value`` -> attribute the rung condition to ``value``."""

    def reverse(
        self, instr: Any, rung: Any, target: Constraint, ctx: CrossingContext
    ) -> ReverseResult:
        st = _eq_single(target)
        if st is None:
            return REVERSE_FALLTHROUGH
        tag, value = st
        if not isinstance(value, bool):
            return unsatisfiable(tag)  # OUT only ever drives True/False
        oneshot = bool(getattr(instr, "_oneshot", False))
        return single(CondAttr(expected=value), exact=not oneshot)


class LatchCrossing(BaseCrossing):
    """SET: True is fired-or-held; False can only be held (polarity oracle)."""

    def reverse(
        self, instr: Any, rung: Any, target: Constraint, ctx: CrossingContext
    ) -> ReverseResult:
        st = _eq_single(target)
        if st is None:
            return REVERSE_FALLTHROUGH
        tag, value = st
        if value is True:
            return disjoint((CondAttr(expected=True),), (_held(tag),), exact=True)
        if value is False:
            return single(_held(tag), exact=True)  # SET never drives False
        return unsatisfiable(tag)


class ResetCrossing(BaseCrossing):
    """RST: the default value is fired-or-held; any other value can only be held."""

    def reverse(
        self, instr: Any, rung: Any, target: Constraint, ctx: CrossingContext
    ) -> ReverseResult:
        st = _eq_single(target)
        if st is None:
            return REVERSE_FALLTHROUGH
        tag, value = st
        default = getattr(getattr(instr, "target", None), "default", None)
        if default is None:
            return REVERSE_FALLTHROUGH  # block / indirect target -> defer
        if value == default:
            return disjoint((CondAttr(expected=True),), (_held(tag),), exact=True)
        return single(_held(tag), exact=True)  # RST never drives a non-default value


register(OutInstruction, OutCrossing())
register(LatchInstruction, LatchCrossing())
register(ResetInstruction, ResetCrossing())
