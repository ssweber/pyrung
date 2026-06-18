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

Lossy / variable-width forms (``to_value``, ``to_text``) and indirect sources
fall through (those are filled in by the per-family work / stay in the walker).
:class:`BlockCopyCrossing` is element-wise — its per-slot fan-out is filled in by
the by-family work; here it is a registered fallthrough.
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.crossings import BaseCrossing, register
from pyrung.core.analysis.crossings._ranges import (
    clamps_on_store,
    range_subset,
    type_bounds,
)
from pyrung.core.crossing import (
    REVERSE_FALLTHROUGH,
    Cmp,
    Constraint,
    CrossingContext,
    Eq,
    ReverseResult,
    satisfied,
    single,
    unsatisfiable,
)
from pyrung.core.instruction.data_transfer import (
    BlockCopyInstruction,
    CopyInstruction,
    FillInstruction,
)
from pyrung.core.memory_block import IndirectExprRef, IndirectRef
from pyrung.core.tag import TagType

_ASCII_MAX = 127  # _ascii_char_from_code / to_ascii cap (instruction/conversions.py)


def _named_source(src: Any) -> Any | None:
    """The source tag when *src* is a plain named tag (not indirect / literal)."""
    if isinstance(src, (IndirectRef, IndirectExprRef)):
        return None
    if hasattr(src, "name"):
        return src
    return None


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


class CopyCrossing(BaseCrossing):
    """Reverse for single-value copy / fill writers (and bijective conversions)."""

    def reverse(
        self, instr: Any, rung: Any, target: Constraint, ctx: CrossingContext
    ) -> ReverseResult:
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
        if isinstance(src, (bool, int, float, str)):  # literal copy: dest forced to src
            return satisfied() if src == value else unsatisfiable(dest_name)
        return REVERSE_FALLTHROUGH  # indirect source -> idx-chase stays in the walker

    def _reverse_named(
        self, named: Any, dest_name: str, value: Any, instr: Any, ctx: CrossingContext
    ) -> ReverseResult:
        """Invert a value-preserving copy, honouring the destination clamp rails."""
        src_type = getattr(named, "type", None)
        dest_type = _dest_type(instr, dest_name, ctx)

        # Source can never overflow the destination -> the copy never clamps.
        if range_subset(src_type, dest_type):
            return single(Eq(named.name, frozenset({value})), exact=True)

        if clamps_on_store(dest_type):
            bounds = type_bounds(dest_type)
            assert bounds is not None  # clamps_on_store implies INT/DINT bounds
            lo, hi = bounds
            if value == hi:  # upper rail: any src >= hi clamps here
                return single(Cmp(named.name, ">=", hi), exact=True)
            if value == lo:  # lower rail: any src <= lo clamps here
                return single(Cmp(named.name, "<=", lo), exact=True)
            return single(Eq(named.name, frozenset({value})), exact=True)  # interior

        # Same-type non-clamping copy (e.g. CHAR->CHAR) is exact; otherwise we
        # cannot rule out a lossy store -> defer (the sound direction).
        if src_type is not None and src_type == dest_type:
            return single(Eq(named.name, frozenset({value})), exact=True)
        return REVERSE_FALLTHROUGH

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
    """Block copy — registered fallthrough (per-slot fan-out filled in by-family)."""


register(CopyInstruction, CopyCrossing())
register(FillInstruction, CopyCrossing())
register(BlockCopyInstruction, BlockCopyCrossing())
