"""Crossings — pack / unpack (bijective bit/word rearrangements)."""

from __future__ import annotations

from pyrung import Dint, Int, Real
from pyrung.core.analysis.crossings.pack import (
    PackBitsCrossing,
    PackWordsCrossing,
    UnpackToBitsCrossing,
    UnpackToWordsCrossing,
)
from pyrung.core.crossing import CrossingContext, Eq, Mask, eq_target
from pyrung.core.instruction.packing import (
    PackBitsInstruction,
    PackWordsInstruction,
    UnpackToBitsInstruction,
    UnpackToWordsInstruction,
)
from pyrung.core.memory_block import Block
from pyrung.core.tag import TagType


def _ctx() -> CrossingContext:
    return CrossingContext()


def _only(result):
    (branch,) = result.branches
    return branch


# --- pack_bits ----------------------------------------------------------------


def test_pack_bits_fans_out_to_each_source_bit() -> None:
    bits = Block("C", TagType.BOOL, 1, 16)
    dest = Int("Packed")
    instr = PackBitsInstruction(bits.select(1, 3), dest)
    r = PackBitsCrossing().reverse(instr, None, eq_target("Packed", 0b101), _ctx())  # C1,C3 set
    assert set(_only(r)) == {
        Eq("C1", frozenset({True})),
        Eq("C2", frozenset({False})),
        Eq("C3", frozenset({True})),
    }
    assert r.exact is True


def test_pack_bits_value_beyond_block_is_unsatisfiable() -> None:
    bits = Block("C", TagType.BOOL, 1, 16)
    dest = Int("Packed")
    instr = PackBitsInstruction(bits.select(1, 3), dest)  # only 3 bits
    r = PackBitsCrossing().reverse(instr, None, eq_target("Packed", 0b1000), _ctx())  # bit 3 set
    assert _only(r) == (Eq("Packed", frozenset()),)


def test_pack_bits_real_dest_falls_through() -> None:
    bits = Block("C", TagType.BOOL, 1, 16)
    dest = Real("Packed")
    instr = PackBitsInstruction(bits.select(1, 3), dest)
    assert PackBitsCrossing().reverse(instr, None, eq_target("Packed", 1.0), _ctx()).fallthrough


# --- pack_words ---------------------------------------------------------------


def test_pack_words_splits_into_lo_hi_with_signed_mapping() -> None:
    words = Block("DS", TagType.INT, 1, 2)
    dest = Dint("Packed")
    instr = PackWordsInstruction(words.select(1, 2), dest)
    # value 0x00008000: lo pattern 0x8000 -> INT -32768; hi 0.
    r = PackWordsCrossing().reverse(instr, None, eq_target("Packed", 0x8000), _ctx())
    assert _only(r) == (Eq("DS1", frozenset({-32768})), Eq("DS2", frozenset({0})))
    assert r.exact is True


# --- unpack_to_bits -----------------------------------------------------------


def test_unpack_to_bits_pins_one_source_bit_true() -> None:
    src = Int("Src")
    bits = Block("C", TagType.BOOL, 1, 16)
    instr = UnpackToBitsInstruction(src, bits.select(1, 16))
    r = UnpackToBitsCrossing().reverse(instr, None, eq_target("C6", True), _ctx())  # bit index 5
    assert _only(r) == (Mask("Src", 1 << 5, 1 << 5),)
    assert r.exact is True


def test_unpack_to_bits_pins_one_source_bit_false() -> None:
    src = Int("Src")
    bits = Block("C", TagType.BOOL, 1, 16)
    instr = UnpackToBitsInstruction(src, bits.select(1, 16))
    r = UnpackToBitsCrossing().reverse(instr, None, eq_target("C6", False), _ctx())
    assert _only(r) == (Mask("Src", 1 << 5, 0),)


def test_unpack_to_bits_real_source_falls_through() -> None:
    src = Real("Src")
    bits = Block("C", TagType.BOOL, 1, 16)
    instr = UnpackToBitsInstruction(src, bits.select(1, 16))
    assert UnpackToBitsCrossing().reverse(instr, None, eq_target("C6", True), _ctx()).fallthrough


# --- unpack_to_words ----------------------------------------------------------


def test_unpack_to_words_pins_low_word_slice() -> None:
    src = Dint("Src")
    words = Block("DS", TagType.INT, 1, 2)
    instr = UnpackToWordsInstruction(src, words.select(1, 2))
    r = UnpackToWordsCrossing().reverse(instr, None, eq_target("DS1", 7), _ctx())  # lo word
    assert _only(r) == (Mask("Src", 0xFFFF, 7),)


def test_unpack_to_words_pins_high_word_slice() -> None:
    src = Dint("Src")
    words = Block("DS", TagType.INT, 1, 2)
    instr = UnpackToWordsInstruction(src, words.select(1, 2))
    r = UnpackToWordsCrossing().reverse(instr, None, eq_target("DS2", 7), _ctx())  # hi word
    assert _only(r) == (Mask("Src", 0xFFFF << 16, 7 << 16),)
