"""Calc crossing (Phase 2) — affine inversion, equality and inequality.

:class:`CalcCrossing` inverts an affine calc writer in two shapes:

**Equality** (``dest == value``) reuses ``calc_reverse_edge`` (the codebase's
single affine inverter, also used by ``build_reverse_edge_map`` for prover
seeding): same-width add/subtract/negate are corrected through the destination
wrap and remain exact.  Wrapped multiplication and mismatched-width integer
stores fall through because a single arithmetic inverse would omit modular
aliases. Other singleton proposals also fall through until their full floating
preimage is represented.

**Inequality** (``dest op bound``, ``op`` in ``< <= > >=``) reverses the affine
forward relation onto its source(s) — the principled "reverse a constraint
through an instruction" that pilot's inequality levers consume:

- a storage-preserving REAL single-source affine with scale ±1 shifts the bound
  and flips the operator on a negative scale;
- wrapping integer destinations fall through because their true preimage is a
  split modular interval;
- two-tag forms fall through because freezing a partner at the current snapshot
  is a steering proposal, not a sound preimage when both operands may move.

Forms that fall through (add no constraint, defer to the caller):

- ``SumExpr`` (aggregate over a block range) — the Phase 3 sign-oracle seam;
  attributing ``sum != 0`` to "some operand nonzero" needs sign reasoning.
- non-affine / unrecognised expressions, multiply and two-tag inequalities,
  ``==``/``!=`` comparison targets, a tag-valued bound, wrapped multiplication
  aliases, and mismatched-width integer equality preimages.

Equality results are exact only when wrap-corrected over a bijective same-width
map. Other equality forms defer rather than returning an under-approximating
candidate. REAL inequality results remain conservative because the consumer
still verifies the proposed bound against the interpreted fork.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from pyrung.core.analysis.crossings import BaseCrossing, register
from pyrung.core.analysis.crossings._ranges import (
    stored_value_possible,
    wrap_to_type,
    wraps_on_store,
)
from pyrung.core.analysis.reverse_edges import calc_reverse_edge
from pyrung.core.crossing import (
    NO_CROSSING_PROPOSAL,
    REVERSE_FALLTHROUGH,
    UNKNOWN,
    Affine,
    Aggregate,
    Cmp,
    Constraint,
    CrossingContext,
    CrossingProposal,
    Eq,
    ReverseResult,
    single,
    unsatisfiable,
)
from pyrung.core.expression import BinaryExpr, LiteralExpr, SumExpr, TagExpr, UnaryExpr
from pyrung.core.instruction.calc import CalcInstruction
from pyrung.core.instruction.conversions import calc_store_transform
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
    """``(left, symbol, right)`` for a two-tag ``left ± right`` expression."""
    if not isinstance(expr, BinaryExpr) or expr.symbol not in ("+", "-"):
        return None
    left = _tag_name(expr.left)
    right = _tag_name(expr.right)
    return (left, expr.symbol, right) if left is not None and right is not None else None


class CalcCrossing(BaseCrossing):
    """Reverse for affine calc writers (equality targets)."""

    def forward(self, instr: Any, target_tag: str, ctx: CrossingContext) -> Any:
        claim = self._raw_forward(instr)
        dest_type = _type_of(target_tag, ctx)
        if dest_type is None:
            candidate_type = getattr(getattr(instr, "dest", None), "type", None)
            dest_type = candidate_type if isinstance(candidate_type, TagType) else None
        storage = calc_store_transform(dest_type)
        if storage is None:
            return UNKNOWN
        if isinstance(claim, (Affine, Aggregate)):
            return replace(claim, storage=storage)
        return claim

    def _raw_forward(self, instr: Any) -> Any:
        """Classify the pre-storage expression; callers apply storage checks."""
        expr = instr.expression
        if isinstance(expr, SumExpr):
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

    def propose(
        self, instr: Any, rung: Any, target: Constraint, ctx: CrossingContext
    ) -> CrossingProposal:
        """Snapshot-freeze one partner of ``A ± B op bound`` at a time.

        These branches are reactive steering candidates, not the complete
        preimage when both operands can move. The consumer must execute and
        verify whichever branch it chooses.
        """
        if (
            not isinstance(target, Cmp)
            or target.bound_is_tag
            or target.op not in _FLIP_OP
            or not _is_number(target.bound)
        ):
            return NO_CROSSING_PROPOSAL
        two = _two_tag_addsub(instr.expression)
        if two is None:
            return NO_CROSSING_PROPOSAL
        left, symbol, right = two
        left_now = ctx.snapshot.get(left)
        right_now = ctx.snapshot.get(right)
        if not _is_number(left_now) or not _is_number(right_now):
            return NO_CROSSING_PROPOSAL

        if symbol == "+":
            branches = (
                (Cmp(left, target.op, target.bound - right_now),),
                (Cmp(right, target.op, target.bound - left_now),),
            )
        else:
            branches = (
                (Cmp(left, target.op, target.bound + right_now),),
                (Cmp(right, _FLIP_OP[target.op], left_now - target.bound),),
            )
        return CrossingProposal(
            branches=branches,
            reason="snapshot-frozen partner in a two-source calc inequality",
            verify_required=True,
        )

    def _reverse_eq(self, instr: Any, target: Eq, ctx: CrossingContext) -> ReverseResult:
        target_value = next(iter(target.values))
        dest_type = _type_of(target.tag, ctx)
        if dest_type is None:
            candidate_type = getattr(getattr(instr, "dest", None), "type", None)
            dest_type = candidate_type if isinstance(candidate_type, TagType) else None
        if dest_type is not None and not stored_value_possible(dest_type, target_value):
            return unsatisfiable(target.tag)

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
            return REVERSE_FALLTHROUGH

        # Wrap-correction: an add/sub/negate is a bijection on the destination's
        # wrap ring, so when source and destination share a wrapping type the
        # naive preimage corrects to the unique true source value -> exact.
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

        # Any remaining singleton is only a try-and-verify proposal: wrapped
        # integers admit modular aliases and floating arithmetic may admit
        # rounding aliases. The sound reverse contract falls through.
        return REVERSE_FALLTHROUGH

    def _reverse_cmp(self, instr: Any, target: Cmp, ctx: CrossingContext) -> ReverseResult:
        """Reverse an inequality ``dest op bound`` onto the calc's source(s)."""
        op = target.op
        bound = target.bound
        if target.bound_is_tag or op not in _FLIP_OP or not _is_number(bound):
            return REVERSE_FALLTHROUGH  # tag-bound / ==,!= / non-numeric -> defer

        expr = instr.expression
        if isinstance(expr, SumExpr):
            return REVERSE_FALLTHROUGH  # Phase 3 sign-oracle seam

        # A linear inequality is not a sound preimage through modular storage:
        # e.g. INT(32767 + 1) < 0 although 32767 < -1 is false.
        if _type_of(target.tag, ctx) is not TagType.REAL:
            return REVERSE_FALLTHROUGH

        # Single-source affine: dest = scale*src + offset, scale in {1, -1}.
        fwd = self.forward(instr, target.tag, ctx)
        if isinstance(fwd, Affine):
            if fwd.scale == 1:
                return single(Cmp(fwd.source, op, bound - fwd.offset), exact=False)
            if fwd.scale == -1:
                return single(Cmp(fwd.source, _FLIP_OP[op], fwd.offset - bound), exact=False)
            return REVERSE_FALLTHROUGH  # multiply: non-bijective inequality -> defer

        # Freezing one partner at the snapshot produces a steering proposal,
        # not a preimage: both operands may move before the writer fires.
        return REVERSE_FALLTHROUGH


register(CalcInstruction, CalcCrossing())
