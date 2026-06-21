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
from pyrung.core.analysis.crossings._ranges import wrap_to_type, wraps_on_store
from pyrung.core.analysis.reverse_edges import calc_reverse_edge
from pyrung.core.crossing import (
    REVERSE_FALLTHROUGH,
    UNKNOWN,
    Affine,
    Constraint,
    CrossingContext,
    Eq,
    ReverseResult,
    single,
)
from pyrung.core.expression import BinaryExpr, LiteralExpr, SumExpr, TagExpr, UnaryExpr
from pyrung.core.instruction.calc import CalcInstruction
from pyrung.core.tag import TagType


def _is_bijective_affine(expr: Any) -> bool:
    """Whether *expr* is an add/sub/negate (bijective under modular wrap)."""
    if isinstance(expr, UnaryExpr):
        return expr.symbol in ("+", "-")
    if isinstance(expr, BinaryExpr):
        return expr.symbol in ("+", "-")
    return False


def _type_of(name: str | None, ctx: CrossingContext) -> TagType | None:
    tag = ctx.tags_by_name.get(name) if name is not None else None
    t = getattr(tag, "type", None)
    return t if isinstance(t, TagType) else None


def _tag_name(node: Any) -> str | None:
    if isinstance(node, TagExpr):
        return getattr(node.tag, "name", None)
    return None


def _lit_value(node: Any) -> int | float | None:
    if isinstance(node, LiteralExpr) and isinstance(node.value, (int, float)):
        return node.value
    return None


class CalcCrossing(BaseCrossing):
    """Reverse for affine calc writers (equality targets)."""

    def forward(self, instr: Any, ctx: CrossingContext) -> Any:
        expr = instr.expression
        edge = calc_reverse_edge(expr)
        if edge is None:
            return UNKNOWN
        src, _ = edge
        if isinstance(expr, UnaryExpr):
            if expr.symbol == "+":
                return Affine(source=src, scale=1, offset=0)
            if expr.symbol == "-":
                return Affine(source=src, scale=-1, offset=0)
            return UNKNOWN
        if not isinstance(expr, BinaryExpr):
            return UNKNOWN
        left_tag = _tag_name(expr.left)
        right_tag = _tag_name(expr.right)
        left_lit = _lit_value(expr.left)
        right_lit = _lit_value(expr.right)
        if left_tag is not None and right_lit is not None:
            if expr.symbol == "+":
                return Affine(source=left_tag, scale=1, offset=right_lit)
            if expr.symbol == "-":
                return Affine(source=left_tag, scale=1, offset=-right_lit)
            if expr.symbol == "*":
                return Affine(source=left_tag, scale=right_lit, offset=0)
        if right_tag is not None and left_lit is not None:
            if expr.symbol == "+":
                return Affine(source=right_tag, scale=1, offset=left_lit)
            if expr.symbol == "-":
                return Affine(source=right_tag, scale=-1, offset=left_lit)
            if expr.symbol == "*":
                return Affine(source=right_tag, scale=left_lit, offset=0)
        return UNKNOWN

    def reverse(
        self, instr: Any, rung: Any, target: Constraint, ctx: CrossingContext
    ) -> ReverseResult:
        if not (isinstance(target, Eq) and len(target.values) == 1):
            return REVERSE_FALLTHROUGH  # multi-valued / non-Eq target -> defer
        target_value = next(iter(target.values))

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

        # Wrap-correction: an add/sub/negate is a bijection on the destination's
        # wrap ring, so when source and destination share a wrapping type the
        # naive preimage corrects to the unique true source value -> exact.
        dest_type = _type_of(getattr(getattr(instr, "dest", None), "name", None), ctx)
        src_type = _type_of(src, ctx)
        if (
            _is_bijective_affine(expr)
            and isinstance(pre, int)
            and src_type is not None
            and src_type == dest_type
            and wraps_on_store(dest_type)
        ):
            corrected = wrap_to_type(pre, dest_type)
            if corrected is not None:
                return single(Eq(src, frozenset({corrected})), exact=True)

        # Otherwise the wrap (mismatched widths, multiply, or unknown types) can
        # admit other preimages -> the naive value is a candidate the consumer
        # verifies (exact=False).
        return single(Eq(src, frozenset({pre})), exact=False)


register(CalcInstruction, CalcCrossing())
