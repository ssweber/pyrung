"""Copy / fill / block-copy crossings.

:class:`CopyCrossing` inverts the *data-flow half* of a single-value copy or
fill: ``copy(src, dest)`` makes ``dest == value`` hold iff ``src`` produced
``value``.  Copy is value-preserving, so the inverse is exact — **except at a
clamp rail**: INT/DINT destinations saturate (``_store_copy_value_to_tag_type``),
so when the target sits at the destination's type boundary and the source can
overflow it, every over-range source collapses there.  That case inverts to a
:class:`Cmp` range (``src >= max`` / ``src <= min``) — still exact, but not a
singleton.  Returning ``Eq(src, {rail})`` there would *under*-approximate (drop
the over-range preimages) — unsound.

It also inverts the two **bijective** conversions:

- ``to_ascii`` (Char->Int, ``ord``) — exact for codes 0..127.
- ``to_binary`` (Int->Char, ``chr(src & 0xFF)``) — exact only when the source
  range fits one byte (else ``& 0xFF`` aliasing -> fallthrough).

Affine expression sources are inverted according to the destination store:
REAL destinations are value-preserving, while INT/DINT rail targets produce
source-side :class:`Cmp` ranges with the inequality flipped for a negative
scale.  Non-divisible integer interiors and zero-scale expressions fall
through.

Lossy / variable-width forms (``to_value``, ``to_text``) and indirect sources
fall through. :class:`BlockCopyCrossing` is element-wise and resolves aligned
static slots; converting and indirect ranges fall through.
"""

from __future__ import annotations

import math
from typing import Any, TypeGuard

from pyrung.core.analysis.crossings import BaseCrossing, register
from pyrung.core.analysis.crossings._ranges import (
    clamps_on_store,
    range_subset,
    range_tags,
    type_bounds,
)
from pyrung.core.crossing import (
    REVERSE_FALLTHROUGH,
    UNKNOWN,
    Affine,
    Cmp,
    Constraint,
    CrossingContext,
    Eq,
    Literal,
    ReverseResult,
    satisfied,
    single,
    unsatisfiable,
)
from pyrung.core.expression import BinaryExpr, LiteralExpr, TagExpr, UnaryExpr
from pyrung.core.instruction.data_transfer import (
    BlockCopyInstruction,
    CopyInstruction,
    FillInstruction,
)
from pyrung.core.memory_block import IndirectExprRef, IndirectRef
from pyrung.core.tag import TagType

_ASCII_MAX = 127  # _ascii_char_from_code / to_ascii cap (instruction/conversions.py)
_INEQ_OPS = frozenset({"<", "<=", ">", ">="})  # ordering ops a copy passes through


def _named_source(src: Any) -> Any | None:
    """The source tag when *src* is a plain named tag (not indirect / literal)."""
    if isinstance(src, (IndirectRef, IndirectExprRef)):
        return None
    if hasattr(src, "name"):
        return src
    return None


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _affine_of(src: Any) -> tuple[Any, int | float, int | float] | None:
    """``(source_tag, scale, offset)`` when *src* is an affine map over one tag.

    The copy-source analogue of ``calc``'s affine forward: recognises ``+tag`` /
    ``-tag`` and ``tag ± k`` / ``k ± tag`` / ``tag * k`` / ``k * tag`` with a
    numeric literal *k* and a single source tag.  A non-affine, multi-tag, or
    non-numeric expression (or a plain tag / literal) yields ``None``.
    """
    if isinstance(src, UnaryExpr):
        if isinstance(src.operand, TagExpr):
            if src.symbol == "+":
                return src.operand.tag, 1, 0
            if src.symbol == "-":
                return src.operand.tag, -1, 0
        return None
    if not isinstance(src, BinaryExpr) or src.symbol not in ("+", "-", "*"):
        return None
    left, right = src.left, src.right
    left_tag = left.tag if isinstance(left, TagExpr) else None
    right_tag = right.tag if isinstance(right, TagExpr) else None
    left_lit = left.value if isinstance(left, LiteralExpr) else None
    right_lit = right.value if isinstance(right, LiteralExpr) else None

    if left_tag is not None and _is_number(right_lit):  # tag <op> k
        if src.symbol == "+":
            return left_tag, 1, right_lit
        if src.symbol == "-":
            return left_tag, 1, -right_lit
        return left_tag, right_lit, 0  # tag * k
    if right_tag is not None and _is_number(left_lit):  # k <op> tag
        if src.symbol == "+":
            return right_tag, 1, left_lit
        if src.symbol == "-":
            return right_tag, -1, left_lit  # k - tag == -tag + k
        return right_tag, left_lit, 0  # k * tag
    return None  # both tags / both literals / nested -> not single-tag affine


def _dest_type(instr: Any, dest_name: str, ctx: CrossingContext) -> TagType | None:
    """The destination element's type — from the instruction, else the context.

    A ``Tag`` dest exposes ``.type`` directly; a fill/block-copy range exposes it
    on its ``.block``; otherwise fall back to the context's tag table.
    """
    dest = getattr(instr, "dest", None)
    t = getattr(dest, "type", None)
    if isinstance(t, TagType):
        return t
    t = getattr(getattr(dest, "block", None), "type", None)  # BlockRange element type
    if isinstance(t, TagType):
        return t
    tag = ctx.tags_by_name.get(dest_name)
    t = getattr(tag, "type", None)
    return t if isinstance(t, TagType) else None


def _single_value(target: Constraint) -> tuple[str, Any] | None:
    """``(tag, value)`` when *target* is a single-valued ``Eq``; else ``None``."""
    if isinstance(target, Eq) and len(target.values) == 1:
        return target.tag, next(iter(target.values))
    return None


def _value_preserving(
    src_name: str, src_type: TagType | None, dest_type: TagType | None, value: Any
) -> ReverseResult:
    """Invert a value-preserving store (copy/fill/block slot), honouring clamp rails.

    Copy/fill/block-copy write ``dest == clamp(src)``.  Away from a rail the
    inverse is the exact singleton ``src == value``; at an INT/DINT rail every
    over-range source collapses there, so the inverse is the exact *range*
    ``src >= max`` / ``src <= min`` (a :class:`Cmp`).  Narrowing the rail to
    ``Eq(src, {rail})`` would drop the over-range preimages — unsound.
    """
    # Source can never overflow the destination -> the copy never clamps.
    if range_subset(src_type, dest_type):
        return single(Eq(src_name, frozenset({value})), exact=True)

    if clamps_on_store(dest_type):
        bounds = type_bounds(dest_type)
        assert bounds is not None  # clamps_on_store implies INT/DINT bounds
        lo, hi = bounds
        if value == hi:  # upper rail: any src >= hi clamps here
            return single(Cmp(src_name, ">=", hi), exact=True)
        if value == lo:  # lower rail: any src <= lo clamps here
            return single(Cmp(src_name, "<=", lo), exact=True)
        return single(Eq(src_name, frozenset({value})), exact=True)  # interior

    # Same-type non-clamping copy (e.g. CHAR->CHAR) is exact; otherwise we cannot
    # rule out a lossy store -> defer (the sound direction).
    if src_type is not None and src_type == dest_type:
        return single(Eq(src_name, frozenset({value})), exact=True)
    return REVERSE_FALLTHROUGH


class CopyCrossing(BaseCrossing):
    """Reverse for single-value copy / fill writers (and bijective conversions)."""

    def forward(self, instr: Any, ctx: CrossingContext) -> Any:
        if getattr(instr, "convert", None) is not None:
            return UNKNOWN
        src = instr.source if isinstance(instr, CopyInstruction) else instr.value
        named = _named_source(src)
        if named is not None:
            if getattr(named, "readonly", False):
                return Literal(named.default)
            dest_name = getattr(getattr(instr, "dest", None), "name", None)
            if range_subset(
                getattr(named, "type", None),
                _dest_type(instr, dest_name, ctx) if dest_name is not None else None,
            ):
                return Affine(source=named.name, scale=1, offset=0)
            return UNKNOWN
        if isinstance(src, (bool, int, float, str)):
            return Literal(src)
        affine = _affine_of(src)  # copy(scale*S + off, D) -> the affine relation
        if affine is not None:
            src_tag, scale, offset = affine
            if not getattr(src_tag, "readonly", False):
                return Affine(source=src_tag.name, scale=scale, offset=offset)
        return UNKNOWN

    def reverse(
        self, instr: Any, rung: Any, target: Constraint, ctx: CrossingContext
    ) -> ReverseResult:
        if isinstance(target, Cmp):
            return self._reverse_cmp(instr, target)
        single_target = _single_value(target)
        if single_target is None:
            return REVERSE_FALLTHROUGH  # multi-valued / non-Eq target -> defer
        dest_name, value = single_target

        conv = getattr(instr, "convert", None)
        if conv is not None:
            return self._reverse_convert(instr, conv, dest_name, value, ctx)

        src = instr.source if isinstance(instr, CopyInstruction) else instr.value
        named = _named_source(src)
        if named is not None:
            if getattr(named, "readonly", False):  # constant ref: dest is fixed
                return satisfied() if named.default == value else unsatisfiable(dest_name)
            return self._reverse_named(named, dest_name, value, instr, ctx)
        affine = self._reverse_affine_expr(src, dest_name, value, instr, ctx)
        if affine is not None:  # copy(scale*S + off, D): invert through the clamp
            return affine
        if isinstance(src, (bool, int, float, str)):  # literal copy: dest forced to src
            return satisfied() if src == value else unsatisfiable(dest_name)
        return REVERSE_FALLTHROUGH  # indirect source -> idx-chase stays in the walker

    def _reverse_affine_expr(
        self, src: Any, dest_name: str, value: Any, instr: Any, ctx: CrossingContext
    ) -> ReverseResult | None:
        """Invert an affine expression-source copy ``copy(scale*S + off, D)``.

        The copy-source twin of calc's affine reverse, but through copy's
        destination store semantics.  An INT/DINT interior has the exact
        integer preimage ``S == (value - off) / scale``.  At a clamp rail, all
        expression values beyond the rail collapse there, so the inverse is a
        source-side :class:`Cmp` range.  A REAL destination preserves the
        affine value and accepts the ordinary non-zero-scale preimage.

        Non-divisible integer interiors, zero scale, non-numeric targets, and
        unsupported destination types defer.
        """
        affine = _affine_of(src)
        if affine is None:
            return None  # non-affine / multi-tag / non-numeric literal -> defer
        src_tag, scale, offset = affine
        if getattr(src_tag, "readonly", False):
            return None  # constant source: not a steerable single-tag affine
        dest_type = _dest_type(instr, dest_name, ctx)
        if (
            not _is_number(value)
            or scale == 0
            or not math.isfinite(value)
            or not math.isfinite(scale)
            or not math.isfinite(offset)
        ):
            return None

        if dest_type is TagType.REAL:
            preimage = (value - offset) / scale
            src_type = getattr(src_tag, "type", None)
            if src_type is TagType.REAL:
                return single(Eq(src_tag.name, frozenset({preimage})), exact=False)
            if (
                src_type in (TagType.INT, TagType.DINT, TagType.WORD)
                and float(preimage).is_integer()
            ):
                return single(Eq(src_tag.name, frozenset({int(preimage)})), exact=True)
            return None

        if not clamps_on_store(dest_type) or not _is_int(value):
            return None
        bounds = type_bounds(dest_type)
        assert bounds is not None  # clamps_on_store implies INT/DINT bounds
        lo, hi = bounds
        if value == hi:
            op = ">=" if scale > 0 else "<="
            return single(Cmp(src_tag.name, op, (hi - offset) / scale), exact=True)
        if value == lo:
            op = "<=" if scale > 0 else ">="
            return single(Cmp(src_tag.name, op, (lo - offset) / scale), exact=True)

        # Interior clamp values have a singleton preimage only over integer
        # arithmetic.  Preserve fallthrough for fractional/non-divisible cases.
        if not (_is_int(scale) and _is_int(offset)):
            return None
        num = value - offset
        if num % scale != 0:
            return None
        return single(Eq(src_tag.name, frozenset({num // scale})), exact=True)

    def _reverse_named(
        self, named: Any, dest_name: str, value: Any, instr: Any, ctx: CrossingContext
    ) -> ReverseResult:
        """Invert a value-preserving copy, honouring the destination clamp rails."""
        return _value_preserving(
            named.name, getattr(named, "type", None), _dest_type(instr, dest_name, ctx), value
        )

    def _reverse_cmp(self, instr: Any, target: Cmp) -> ReverseResult:
        """Reverse an inequality through a value-preserving copy: ``dest op b`` ⟺
        ``src op b``.  Deferred for converting / literal / readonly / indirect
        sources (the sound direction).  ``exact=False``: a copy clamps at the
        destination's rails, where a boundary value can collapse — the consumer
        verifies, so the passed-through inequality is a candidate region."""
        if target.op not in _INEQ_OPS or getattr(instr, "convert", None) is not None:
            return REVERSE_FALLTHROUGH
        src = instr.source if isinstance(instr, CopyInstruction) else instr.value
        named = _named_source(src)
        if named is None or getattr(named, "readonly", False):
            return REVERSE_FALLTHROUGH  # literal / constant / indirect source -> defer
        return single(
            Cmp(named.name, target.op, target.bound, bound_is_tag=target.bound_is_tag),
            exact=False,
        )

    def _reverse_convert(
        self, instr: Any, conv: Any, dest_name: str, value: Any, ctx: CrossingContext
    ) -> ReverseResult:
        named = _named_source(getattr(instr, "source", None))
        if named is None:  # literal / indirect convert source
            return REVERSE_FALLTHROUGH
        if conv.mode == "ascii":  # Char -> Int: dest == ord(src_char)
            if isinstance(value, int) and not isinstance(value, bool):
                if 0 <= value <= _ASCII_MAX:
                    return single(Eq(named.name, frozenset({chr(value)})), exact=True)
                return unsatisfiable(dest_name)  # int outside producible 0..127
            return REVERSE_FALLTHROUGH  # non-int target -> unsure, defer
        if conv.mode == "binary":  # Int -> Char: dest == chr(src & 0xFF)
            if not (isinstance(value, str) and len(value) == 1):
                return REVERSE_FALLTHROUGH  # not a single CHAR target -> unsure, defer
            code = ord(value)
            if code > _ASCII_MAX:  # writer faults above ASCII -> never produced
                return unsatisfiable(dest_name)
            if _source_fits_one_byte(named, ctx):
                return single(Eq(named.name, frozenset({code})), exact=True)
            return REVERSE_FALLTHROUGH  # & 0xFF aliasing -> superset
        return REVERSE_FALLTHROUGH  # "value" / "text" -> variable-width


def _source_fits_one_byte(src: Any, ctx: CrossingContext) -> bool:
    """Whether *src*'s declared range fits 0..255, so ``v & 0xFF == v``."""
    name = getattr(src, "name", None)
    if name is None:
        return False
    tag = ctx.tags_by_name.get(name)
    if tag is None:
        return False
    lo, hi = getattr(tag, "min", None), getattr(tag, "max", None)
    return lo is not None and hi is not None and lo >= 0 and hi <= 0xFF


class BlockCopyCrossing(BaseCrossing):
    """Block copy — element-wise ``dst[i] == clamp(src[i])``.

    A target on one destination slot inverts to the value-preserving constraint
    on the *aligned* source slot (exact, with the same clamp-rail handling as a
    scalar copy).  Converting block copies (``value``/``ascii``) and indirect
    ranges fall through.
    """

    def reverse(
        self, instr: Any, rung: Any, target: Constraint, ctx: CrossingContext
    ) -> ReverseResult:
        single_target = _single_value(target)
        if single_target is None or getattr(instr, "convert", None) is not None:
            return REVERSE_FALLTHROUGH  # multi-valued / non-Eq / converting -> defer
        dest_name, value = single_target

        dst_tags = range_tags(getattr(instr, "dest", None))
        src_tags = range_tags(getattr(instr, "source", None))
        if dst_tags is None or src_tags is None or len(dst_tags) != len(src_tags):
            return REVERSE_FALLTHROUGH
        for i, dst in enumerate(dst_tags):
            if getattr(dst, "name", None) == dest_name:
                src = src_tags[i]
                return _value_preserving(
                    src.name,
                    getattr(src, "type", None),
                    getattr(dst, "type", None),
                    value,
                )
        return REVERSE_FALLTHROUGH  # target slot not in this copy's dest range


register(CopyInstruction, CopyCrossing())
register(FillInstruction, CopyCrossing())
register(BlockCopyInstruction, BlockCopyCrossing())
