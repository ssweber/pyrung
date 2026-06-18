"""Pack / unpack crossings — bit/word rearrangements are bijective.

These are not lossy: packing and unpacking shuffle bits and words, so a target on
the packed register inverts exactly.

- **PackBits** — ``dest`` bit ``i`` came from source bit-tag ``i``; a target
  inverts to one :class:`Eq` per source bit (a fan-out).  A target whose pattern
  sets a bit past the source block is unsatisfiable (those bits are forced 0).
- **PackWords** — ``dest`` low/high 16 bits came from the two source words.
- **UnpackToBits** — a target on one destination bit constrains *one bit* of the
  wide source: an exact :class:`Mask` (the other bits stay free), where an
  :class:`Eq` set would be up to 2**31 wide.
- **UnpackToWords** — a target on one destination word constrains the matching
  16-bit slice of the source dword: an exact :class:`Mask`.

REAL bit-reinterpretation (``_int_to_float_bits``) and :class:`PackText` (a
number<->text *parse*, genuinely lossy) fall through.
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.crossings import BaseCrossing, register
from pyrung.core.analysis.crossings._ranges import range_tags
from pyrung.core.crossing import (
    REVERSE_FALLTHROUGH,
    Constraint,
    CrossingContext,
    Eq,
    Mask,
    ReverseResult,
    single,
    unsatisfiable,
)
from pyrung.core.instruction.packing import (
    PackBitsInstruction,
    PackTextInstruction,
    PackWordsInstruction,
    UnpackToBitsInstruction,
    UnpackToWordsInstruction,
)
from pyrung.core.tag import TagType

_WIDTH = {TagType.INT: 16, TagType.WORD: 16, TagType.DINT: 32}


def _width(tag_type: TagType | None) -> int | None:
    """The integer bit-width of *tag_type* (REAL/other -> ``None`` -> fallthrough)."""
    return _WIDTH.get(tag_type) if tag_type is not None else None


def _unsigned(value: Any, width: int) -> int | None:
    """The unsigned bit pattern of integer *value* in *width* bits, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value & ((1 << width) - 1)


def _word_value(pattern16: int, tag_type: TagType | None) -> int:
    """A 16-bit pattern as a value in *tag_type*'s domain (INT signed, WORD raw)."""
    if tag_type == TagType.INT and pattern16 >= 0x8000:
        return pattern16 - 0x10000
    return pattern16


def _single_value(target: Constraint) -> tuple[str, Any] | None:
    if isinstance(target, Eq) and len(target.values) == 1:
        return target.tag, next(iter(target.values))
    return None


def _tag_type(tag: Any) -> TagType | None:
    t = getattr(tag, "type", None)
    return t if isinstance(t, TagType) else None


class PackBitsCrossing(BaseCrossing):
    """Reverse ``pack_bits``: each destination bit fans out to its source bit-tag."""

    def reverse(
        self, instr: Any, rung: Any, target: Constraint, ctx: CrossingContext
    ) -> ReverseResult:
        st = _single_value(target)
        if st is None:
            return REVERSE_FALLTHROUGH
        dest_name, value = st
        width = _width(_tag_type(getattr(instr, "dest", None)))
        if width is None:
            return REVERSE_FALLTHROUGH  # REAL dest -> bit reinterpret, defer
        pattern = _unsigned(value, width)
        if pattern is None:
            return REVERSE_FALLTHROUGH
        bit_tags = range_tags(getattr(instr, "bit_block", None))
        if not bit_tags:
            return REVERSE_FALLTHROUGH
        n = len(bit_tags)
        if pattern >> n:  # a bit beyond the source block is set -> never produced
            return unsatisfiable(dest_name)
        constraints = tuple(
            Eq(bit.name, frozenset({bool((pattern >> i) & 1)})) for i, bit in enumerate(bit_tags)
        )
        return single(*constraints, exact=True)


class PackWordsCrossing(BaseCrossing):
    """Reverse ``pack_words``: low/high 16 bits fan out to the two source words."""

    def reverse(
        self, instr: Any, rung: Any, target: Constraint, ctx: CrossingContext
    ) -> ReverseResult:
        st = _single_value(target)
        if st is None:
            return REVERSE_FALLTHROUGH
        value = st[1]
        if _tag_type(getattr(instr, "dest", None)) != TagType.DINT:
            return REVERSE_FALLTHROUGH  # REAL dest -> bit reinterpret, defer
        pattern = _unsigned(value, 32)
        if pattern is None:
            return REVERSE_FALLTHROUGH
        word_tags = range_tags(getattr(instr, "word_block", None))
        if not word_tags or len(word_tags) != 2:
            return REVERSE_FALLTHROUGH
        lo, hi = word_tags
        return single(
            Eq(lo.name, frozenset({_word_value(pattern & 0xFFFF, _tag_type(lo))})),
            Eq(hi.name, frozenset({_word_value((pattern >> 16) & 0xFFFF, _tag_type(hi))})),
            exact=True,
        )


class UnpackToBitsCrossing(BaseCrossing):
    """Reverse ``unpack_to_bits``: a target bit pins one bit of the source word."""

    def reverse(
        self, instr: Any, rung: Any, target: Constraint, ctx: CrossingContext
    ) -> ReverseResult:
        st = _single_value(target)
        if st is None:
            return REVERSE_FALLTHROUGH
        bit_name, value = st
        if not isinstance(value, bool):
            return REVERSE_FALLTHROUGH  # bit destinations are BOOL
        source = getattr(instr, "source", None)
        src_name = getattr(source, "name", None)
        if src_name is None or _width(_tag_type(source)) is None:
            return REVERSE_FALLTHROUGH  # REAL / indirect source -> defer
        bit_tags = range_tags(getattr(instr, "bit_block", None))
        if not bit_tags:
            return REVERSE_FALLTHROUGH
        for k, bit in enumerate(bit_tags):
            if getattr(bit, "name", None) == bit_name:
                mask = 1 << k
                return single(Mask(src_name, mask, mask if value else 0), exact=True)
        return REVERSE_FALLTHROUGH


class UnpackToWordsCrossing(BaseCrossing):
    """Reverse ``unpack_to_words``: a target word pins one 16-bit slice of the dword."""

    def reverse(
        self, instr: Any, rung: Any, target: Constraint, ctx: CrossingContext
    ) -> ReverseResult:
        st = _single_value(target)
        if st is None:
            return REVERSE_FALLTHROUGH
        word_name, value = st
        source = getattr(instr, "source", None)
        src_name = getattr(source, "name", None)
        if src_name is None or _tag_type(source) != TagType.DINT:
            return REVERSE_FALLTHROUGH  # REAL / indirect source -> defer
        word_tags = range_tags(getattr(instr, "word_block", None))
        if not word_tags or len(word_tags) != 2:
            return REVERSE_FALLTHROUGH
        for k, word in enumerate(word_tags):
            if getattr(word, "name", None) == word_name:
                pattern = _unsigned(value, 16)
                if pattern is None:
                    return REVERSE_FALLTHROUGH
                shift = 16 * k
                return single(Mask(src_name, 0xFFFF << shift, pattern << shift), exact=True)
        return REVERSE_FALLTHROUGH


class PackTextCrossing(BaseCrossing):
    """Pack text (number<->text parse) — registered fallthrough (lossy spelling)."""


register(PackBitsInstruction, PackBitsCrossing())
register(PackWordsInstruction, PackWordsCrossing())
register(UnpackToBitsInstruction, UnpackToBitsCrossing())
register(UnpackToWordsInstruction, UnpackToWordsCrossing())
register(PackTextInstruction, PackTextCrossing())
