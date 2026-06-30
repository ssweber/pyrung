"""Calc crossing (Phase 2) — affine inversion, equality and inequality.

:class:`CalcCrossing` inverts an affine calc writer in two shapes:

**Equality** (``dest == value``) reuses ``calc_reverse_edge`` (the codebase's
single affine inverter, also used by ``build_reverse_edge_map`` for prover
seeding): ``dest = src + k`` gives ``src == value - k``, ``dest = -src`` gives
``src == -value``, ``dest = src * k`` gives ``src == value // k`` when ``k``
divides ``value``.

**Inequality** (``dest op bound``, ``op`` in ``< <= > >=``) reverses the affine
forward relation onto its source(s) — the principled "reverse a constraint
through an instruction" that pilot's inequality levers consume:

- single-source affine ``dest = scale*src + offset`` (scale ∈ {1, -1}): shift the
  bound (``src op bound - offset``) and flip the operator on a negative scale
  (``-src op b`` ⟺ ``src flip(op) -b``).  Multiply (``scale`` ∉ {1, -1}) is
  non-bijective under wrap, so it defers.
- two-tag ``A ± B`` (both operands tags): freeze the **partner** at its
  ``ctx.snapshot`` value and emit a DNF of one ``Cmp`` per operand —
  ``A op bound-B_now`` ∨ ``B op bound-A_now`` (the ``-B`` term flips the partner
  branch's operator).  This is the reactive joint-steering reverse: each branch
  re-points against the live partner each scan.  No snapshot for the partner ⇒
  fallthrough (nothing to freeze against).

Forms that fall through (add no constraint, defer to the caller):

- ``SumExpr`` (aggregate over a block range) — the Phase 3 sign-oracle seam;
  attributing ``sum != 0`` to "some operand nonzero" needs sign reasoning.
- non-affine / unrecognised expressions, multiply inequalities, ``==``/``!=``
  comparison targets, a tag-valued bound, and non-exact equality preimages.

Inequality results are marked ``exact=False``: the shifted bound is the *in-range
linear preimage*, but calc **wraps** at the destination type's rails, where the
true preimage can admit or exclude boundary values.  The single consumer (pilot)
verifies every lever against the interpreted fork (ground truth), so a candidate
region is the sound, useful shape here.  Equality results keep their existing
exactness (wrap-corrected when source and destination share a wrapping type).
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
    Cmp,
    Constraint,
    CrossingContext,
    Eq,
    ReverseResult,
    disjoint,
    single,
)
from pyrung.core.expression import BinaryExpr, LiteralExpr, SumExpr, TagExpr, UnaryExpr
from pyrung.core.instruction.calc import CalcInstruction
from pyrung.core.tag import TagType

#: Inequality operator under operand-side negation (``-src op b`` ⟺ ``src f(op) -b``).
_FLIP_OP = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}


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


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _two_tag_addsub(expr: Any) -> tuple[str, str, str] | None:
    """``(left, symbol, right)`` when *expr* is ``L ± R`` with both operands tags."""
    if not isinstance(expr, BinaryExpr) or expr.symbol not in ("+", "-"):
        return None
    left = _tag_name(expr.left)
    right = _tag_name(expr.right)
    if left is None or right is None:
        return None
    return (left, expr.symbol, right)


class CalcCrossing(BaseCrossing):
    """Reverse for affine calc writers (equality targets)."""

    def forward(self, instr: Any, ctx: CrossingContext) -> Any:
        expr = instr.expression
        if isinstance(expr, SumExpr):
            from pyrung.core.crossing import Aggregate

            return Aggregate(tags=tuple(tag.name for tag in expr.block_range))
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
        if isinstance(target, Cmp):
            return self._reverse_cmp(instr, target, ctx)
        if isinstance(target, Eq) and len(target.values) == 1:
            return self._reverse_eq(instr, target, ctx)
        return REVERSE_FALLTHROUGH  # multi-valued / unsupported target -> defer

    def _reverse_eq(self, instr: Any, target: Eq, ctx: CrossingContext) -> ReverseResult:
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

    def _reverse_cmp(self, instr: Any, target: Cmp, ctx: CrossingContext) -> ReverseResult:
        """Reverse an inequality ``dest op bound`` onto the calc's source(s)."""
        op = target.op
        bound = target.bound
        if target.bound_is_tag or op not in _FLIP_OP or not _is_number(bound):
            return REVERSE_FALLTHROUGH  # tag-bound / ==,!= / non-numeric -> defer

        expr = instr.expression
        if isinstance(expr, SumExpr):
            return REVERSE_FALLTHROUGH  # Phase 3 sign-oracle seam

        # Single-source affine: dest = scale*src + offset, scale in {1, -1}.
        fwd = self.forward(instr, ctx)
        if isinstance(fwd, Affine):
            if fwd.scale == 1:
                return single(Cmp(fwd.source, op, bound - fwd.offset), exact=False)
            if fwd.scale == -1:
                return single(Cmp(fwd.source, _FLIP_OP[op], fwd.offset - bound), exact=False)
            return REVERSE_FALLTHROUGH  # multiply: non-bijective inequality -> defer

        # Two-tag A ± B: freeze the partner at snapshot, DNF over both operands.
        two = _two_tag_addsub(expr)
        if two is not None:
            return self._reverse_two_tag_cmp(two, op, bound, ctx)
        return REVERSE_FALLTHROUGH

    def _reverse_two_tag_cmp(
        self, two: tuple[str, str, str], op: str, bound: Any, ctx: CrossingContext
    ) -> ReverseResult:
        """``A ± B op bound`` with the partner frozen at its ``ctx.snapshot`` value.

        Each branch steers one operand against the other's *current* value, so the
        two branches are the left/right reactive levers a consumer re-points each
        scan.  No snapshot for an operand ⇒ fallthrough (nothing to freeze).
        """
        left, sym, right = two
        l_now = ctx.snapshot.get(left)
        r_now = ctx.snapshot.get(right)
        if not _is_number(l_now) or not _is_number(r_now):
            return REVERSE_FALLTHROUGH

        if sym == "+":
            # A + B op bound  ⟹  A op bound - B_now  ∨  B op bound - A_now
            left_branch = (Cmp(left, op, bound - r_now),)
            right_branch = (Cmp(right, op, bound - l_now),)
        else:
            # A - B op bound  ⟹  A op bound + B_now  ∨  -B op bound - A_now
            #                                          ⟺ B flip(op) A_now - bound
            left_branch = (Cmp(left, op, bound + r_now),)
            right_branch = (Cmp(right, _FLIP_OP[op], l_now - bound),)
        return disjoint(left_branch, right_branch, exact=False)


register(CalcInstruction, CalcCrossing())
