"""Copy / fill / block-copy crossings (Phase 2).

:class:`CopyCrossing` inverts the *data-flow half* of a single-value copy or
fill: given ``dest == value``, the source must hold ``value`` (``copy(src,
dest)`` implies ``dest == value`` iff ``src == value``).  It also inverts the two
**bijective** text/numeric conversions:

- ``convert=to_ascii`` (Char->Int, ``dest == ord(src_char)``) — always exact; the
  producible codes are ASCII 0..127, so an int target outside that range is
  unsatisfiable.
- ``convert=to_binary`` (Int->Char, ``dest == chr(src & 0xFF)``) — exact only when
  the source's declared range fits one byte (otherwise the ``& 0xFF`` aliasing
  makes the preimage a superset -> fallthrough).

The lossy / variable-width forms (``to_value`` face-value, ``to_text`` rendered
digits) and indirect sources (idx-chase stays in the walker) fall through.
:class:`BlockCopyCrossing` is a registered fallthrough — per-slot range inversion
is deferred (``build_reverse_edge_map`` already serves the non-convert case for
prover seeding).
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.crossings import BaseCrossing, register
from pyrung.core.crossing import REVERSE_FALLTHROUGH, CrossingContext, ReverseResult
from pyrung.core.instruction.data_transfer import (
    BlockCopyInstruction,
    CopyInstruction,
    FillInstruction,
)
from pyrung.core.memory_block import IndirectExprRef, IndirectRef

_ASCII_MAX = 127  # _ascii_char_from_code / to_ascii cap (instruction/conversions.py)


def _unsatisfiable(dest: str) -> ReverseResult:
    """The structural-blocker encoding: no value of *dest* works."""
    return ReverseResult(constraints=[(dest, frozenset())])


def _named_source(src: Any) -> Any | None:
    """The source tag when *src* is a plain named tag (not indirect / literal)."""
    if isinstance(src, (IndirectRef, IndirectExprRef)):
        return None
    if hasattr(src, "name"):
        return src
    return None


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


class CopyCrossing(BaseCrossing):
    """Reverse for single-value copy / fill writers (and bijective conversions)."""

    def reverse(
        self, instr: Any, target_tag: str, target_value: Any, ctx: CrossingContext
    ) -> ReverseResult:
        conv = getattr(instr, "convert", None)
        if conv is not None:
            return self._reverse_convert(instr, conv, target_tag, target_value, ctx)
        src = instr.source if isinstance(instr, CopyInstruction) else instr.value
        named = _named_source(src)
        if named is not None:
            if getattr(named, "readonly", False):  # constant ref: dest is fixed
                return (
                    ReverseResult(exact=True)
                    if named.default == target_value
                    else _unsatisfiable(target_tag)
                )
            return ReverseResult(constraints=[(named.name, frozenset({target_value}))], exact=True)
        if isinstance(src, (bool, int, float, str)):  # literal copy: dest forced to src
            return ReverseResult(exact=True) if src == target_value else _unsatisfiable(target_tag)
        return REVERSE_FALLTHROUGH  # indirect source -> idx-chase stays in the walker

    def _reverse_convert(
        self, instr: Any, conv: Any, target_tag: str, target_value: Any, ctx: CrossingContext
    ) -> ReverseResult:
        named = _named_source(getattr(instr, "source", None))
        if named is None:  # literal / indirect convert source
            return REVERSE_FALLTHROUGH
        if conv.mode == "ascii":  # Char -> Int: dest == ord(src_char)
            if isinstance(target_value, int) and not isinstance(target_value, bool):
                if 0 <= target_value <= _ASCII_MAX:
                    return ReverseResult(
                        constraints=[(named.name, frozenset({chr(target_value)}))], exact=True
                    )
                return _unsatisfiable(target_tag)  # int outside producible 0..127
            return REVERSE_FALLTHROUGH  # non-int target -> unsure, defer
        if conv.mode == "binary":  # Int -> Char: dest == chr(src & 0xFF)
            if not (isinstance(target_value, str) and len(target_value) == 1):
                return REVERSE_FALLTHROUGH  # not a single CHAR target -> unsure, defer
            code = ord(target_value)
            if code > _ASCII_MAX:  # writer faults above ASCII -> never produced
                return _unsatisfiable(target_tag)
            if _source_fits_one_byte(named, ctx):
                return ReverseResult(constraints=[(named.name, frozenset({code}))], exact=True)
            return REVERSE_FALLTHROUGH  # & 0xFF aliasing -> superset
        return REVERSE_FALLTHROUGH  # "value" / "text" -> variable-width


class BlockCopyCrossing(BaseCrossing):
    """Block copy — registered fallthrough (per-slot range inversion deferred)."""


register(CopyInstruction, CopyCrossing())
register(FillInstruction, CopyCrossing())
register(BlockCopyInstruction, BlockCopyCrossing())
