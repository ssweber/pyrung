"""Calc crossing (Phase 2) — affine (equality) inversion only.

:class:`CalcCrossing` inverts an affine calc writer for an equality target
``dest == value`` by reusing ``calc_reverse_edge`` (the codebase's single affine
inverter, also used by ``build_reverse_edge_map`` for prover seeding): ``dest =
src + k`` gives ``src == value - k``, ``dest = -src`` gives ``src == -value``,
``dest = src * k`` gives ``src == value // k`` when ``k`` divides ``value``.

Three forms fall through (add no constraint, defer to the caller):

- ``SumExpr`` (aggregate over a block range) — the Phase 3 sign-oracle seam;
  attributing ``sum != 0`` to "some operand nonzero" needs sign reasoning.
- non-affine / multi-tag expressions — ``calc_reverse_edge`` returns ``None``.
- a non-exact preimage (e.g. non-integer division ``value % k != 0``) — the
  invert function returns ``None``.

Inequality targets are **not** handled here: ``reverse`` is value-shaped (a
single equality ``target_value``), whereas inequality chasing
(``_chase_inequality_source`` / ``_extract_inequality_prereqs``) consumes
SP-expr atoms.  Those stay in their neutral home (``sp_values``), consumed by
walk / projected unchanged.

The result is marked ``exact=False``: calc *wraps* at the destination type's
boundary, so the integer preimage is a candidate the consumer must still verify
(the walker's interpreted fork is ground truth) rather than a hard necessary-and-
sufficient claim.  Upgrading to ``exact=True`` via source-type wrap-correction is
a follow-up for when CalcCrossing gains a production consumer.
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.crossings import BaseCrossing, register
from pyrung.core.analysis.reverse_edges import calc_reverse_edge
from pyrung.core.crossing import REVERSE_FALLTHROUGH, CrossingContext, ReverseResult
from pyrung.core.expression import SumExpr
from pyrung.core.instruction.calc import CalcInstruction


class CalcCrossing(BaseCrossing):
    """Reverse for affine calc writers (equality targets)."""

    def reverse(
        self, instr: Any, target_tag: str, target_value: Any, ctx: CrossingContext
    ) -> ReverseResult:
        expr = instr.expression
        if isinstance(expr, SumExpr):
            return REVERSE_FALLTHROUGH  # Phase 3 sign-oracle seam
        edge = calc_reverse_edge(expr)
        if edge is None:
            return REVERSE_FALLTHROUGH  # non-affine / multi-tag
        src, invert = edge
        try:
            pre = invert(target_value)
        except (TypeError, ValueError, ZeroDivisionError):
            return REVERSE_FALLTHROUGH  # non-numeric target -> defer
        if pre is None:
            return REVERSE_FALLTHROUGH  # non-exact preimage (e.g. value % k != 0)
        return ReverseResult(constraints=[(src, frozenset({pre}))], exact=False)


register(CalcInstruction, CalcCrossing())
