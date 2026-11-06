"""What a Copy, Block copy and Fill store, as a CLICK read it (conformance round 2026-11-05),
except where a test names a row as pyrung's own choice.

Each runs on every backend ``runner_factory`` gives: the interpreter, the compiled kernel, or
both in lockstep.  The kernel is the CircuitPython codegen's, so the generated P1AM-200 code
stores the same.
"""

from __future__ import annotations

import math

import pytest

from pyrung.click import ClickBlocks
from pyrung.core import (
    Block,
    Bool,
    Dint,
    Int,
    Program,
    Real,
    Rung,
    TagType,
    Word,
    blockcopy,
    calc,
    copy,
    fill,
    out,
    to_text,
    to_value,
    unpack_to_words,
)
from pyrung.core.condition import AllCondition, CompareEq

_blocks = ClickBlocks()
dd, df, dh, ds, txt = _blocks.dd, _blocks.df, _blocks.dh, _blocks.ds, _blocks.txt

SC43 = "fault.out_of_range"
SC44 = "fault.address_error"


def _one_scan(runner_factory, logic, patch=None):
    runner = runner_factory(logic)
    if patch:
        runner.patch(patch)
    runner.step()
    return runner.current_state.tags


@pytest.mark.parametrize(
    ("source", "value", "stored"),
    [
        (Dint, 70000, 65535),
        (Dint, 65535, 65535),
        (Dint, -1, 65535),
        (Dint, -32768, 32768),
        (Dint, -40000, 32768),
        (Real, 1.0e10, 65535),
        (Real, -1.5, 65535),
        (Int, -1, 65535),
        (Int, -32768, 32768),
    ],
)
def test_a_register_into_a_word_clamps_above_and_wraps_below(runner_factory, source, value, stored):
    """Read on a CLICK: past 65535 is 65535, -1 is 65535 and -Inf is 32768.  The floor at
    -32768 (-40000 stores 32768, as -32768 does) is pyrung's choice, not yet read."""
    src = source("Src", default=value)
    dest = Word("Dest", default=7)
    with Program() as logic:
        with Rung():
            copy(src, dest)
    tags = _one_scan(runner_factory, logic)
    assert (tags["Dest"], bool(tags.get(SC43, False))) == (stored, False)


@pytest.mark.parametrize(("value", "stored"), [(math.inf, 65535), (-math.inf, 32768)])
def test_an_infinity_into_a_word(runner_factory, value, stored):
    big = Real("Big", default=3.0e38)
    inf = Real("Inf")
    dest = Word("Dest", default=7)
    scale = 10 if value > 0 else -10
    with Program() as logic:
        with Rung():
            calc(big * scale, inf)
            copy(inf, dest)
    tags = _one_scan(runner_factory, logic)
    assert (tags["Dest"], bool(tags.get(SC43, False))) == (stored, False)


@pytest.mark.parametrize(("value", "stored"), [(70000, 4464), (-1, 65535)])
def test_a_constant_into_a_word_keeps_its_low_16_bits(runner_factory, value, stored):
    dest = Word("Dest", default=7)
    filled = Block("W", TagType.WORD, 1, 2)
    with Program() as logic:
        with Rung():
            copy(value, dest)
            fill(value, filled.select(1, 2))
    tags = _one_scan(runner_factory, logic)
    assert (tags["Dest"], tags["W1"], tags["W2"]) == (stored, stored, stored)
    assert not tags.get(SC43, False)


def test_a_word_into_an_int_keeps_its_bits(runner_factory):
    src = Word("Src", default=0xFFFF)
    low = Word("Low", default=0x8000)
    dest, dest2 = Int("Dest"), Int("Dest2")
    words = Block("W", TagType.WORD, 1, 2)
    ints = Block("I", TagType.INT, 1, 2)
    with Program() as logic:
        with Rung():
            copy(src, dest)
            copy(low, dest2)
            copy(0xFFFF, words[1])
            copy(1, words[2])
            blockcopy(words.select(1, 2), ints.select(1, 2))
    tags = _one_scan(runner_factory, logic)
    assert (tags["Dest"], tags["Dest2"], tags["I1"], tags["I2"]) == (-1, -32768, -1, 1)
    assert not tags.get(SC43, False)


def test_a_word_into_a_dint_or_a_real_is_its_value(runner_factory):
    src = Word("Src", default=0xFFFF)
    dint, real = Dint("D"), Real("R")
    with Program() as logic:
        with Rung():
            copy(src, dint)
            copy(src, real)
    tags = _one_scan(runner_factory, logic)
    assert (tags["D"], tags["R"]) == (65535, 65535.0)


@pytest.mark.parametrize(("value", "on"), [(2, False), (3, True), (1, True), (0, False)])
def test_a_number_into_a_bit_takes_its_low_bit(runner_factory, value, on):
    src = Int("Src", default=value)
    bit, bit2 = Bool("Bit"), Bool("Bit2")
    bits = Block("B", TagType.BOOL, 1, 2)
    with Program() as logic:
        with Rung():
            copy(value, bit)
            copy(src, bit2)
            fill(value, bits.select(1, 2))
    tags = _one_scan(runner_factory, logic)
    assert (tags["Bit"], tags["Bit2"], tags["B1"], tags["B2"]) == (on, on, on, on)


def test_a_block_to_value_skips_a_bad_character(runner_factory):
    chars = Block("CH", TagType.CHAR, 1, 3)
    nums = Block("N", TagType.INT, 1, 3)
    with Program() as logic:
        with Rung():
            fill(99, nums.select(1, 3))
            blockcopy(chars.select(1, 3), nums.select(1, 3), convert=to_value)
    tags = _one_scan(runner_factory, logic, {"CH1": "1", "CH2": "A", "CH3": "2"})
    assert (tags["N1"], tags["N2"], tags["N3"]) == (1, 99, 2)
    assert tags[SC43]


def test_a_fill_that_clamps_raises_sc43(runner_factory):
    ints = Block("I", TagType.INT, 1, 2)
    with Program() as logic:
        with Rung():
            fill(40000, ints.select(1, 2))
    tags = _one_scan(runner_factory, logic)
    assert (tags["I1"], tags["I2"], tags[SC43]) == (32767, 32767, True)


def test_a_fill_that_fits_raises_no_sc43(runner_factory):
    ints = Block("I", TagType.INT, 1, 2)
    src = Dint("Src", default=5)
    with Program() as logic:
        with Rung():
            fill(src, ints.select(1, 2))
    tags = _one_scan(runner_factory, logic)
    assert (tags["I1"], tags.get(SC43, False)) == (5, False)


def test_a_pointer_past_ds_dh_df_or_txt_leaves_the_destination(runner_factory):
    ptr = Int("Ptr")
    ds_dest, dh_dest, df_dest, txt_dest = Int("DsDest"), Word("DhDest"), Real("DfDest"), txt[999]
    dd_dest = Dint("DdDest")
    with Program() as logic:
        with Rung():
            copy(4501, ptr)
            copy(ds[ptr], ds_dest)
            copy(501, ptr)
            copy(dh[ptr], dh_dest)
            copy(df[ptr], df_dest)
            copy(1001, ptr)
            copy(txt[ptr], txt_dest)
            copy(txt[ptr + 0], ds_dest)
            copy(1001, ptr)
            copy(dd[ptr], dd_dest)
    patch = {"DsDest": 99, "DhDest": 99, "DfDest": 99.0, "TXT999": "Z", "DdDest": 99}
    tags = _one_scan(runner_factory, logic, patch)
    assert (tags["DsDest"], tags["DhDest"], tags["DfDest"], tags["TXT999"]) == (99, 99, 99.0, "Z")
    # Past the DD bank the read is 0, and it is written.
    assert tags["DdDest"] == 0
    assert not tags.get(SC43, False) and not tags.get(SC44, False)


def test_a_pointer_below_a_kept_bank_still_raises_sc44(runner_factory):
    ptr = Int("Ptr")
    dest = Int("Dest", default=99)
    with Program() as logic:
        with Rung():
            copy(0, ptr)
            copy(ds[ptr], dest)
    tags = _one_scan(runner_factory, logic)
    assert (tags["Dest"], tags[SC44]) == (99, True)


def test_a_word_as_suppressed_text_is_hex(runner_factory):
    chars = Block("CH", TagType.CHAR, 1, 4)
    src = Word("Src", default=0x00AB)
    with Program() as logic:
        with Rung():
            copy(src, chars[1], convert=to_text())
    tags = _one_scan(runner_factory, logic)
    assert (tags["CH1"], tags["CH2"], tags.get("CH3", "\x00")) == ("A", "B", "\x00")


def test_a_nan_reads_all_ones(runner_factory):
    zero = Real("Zero")
    nan = Real("NaN")
    words = Block("W", TagType.WORD, 1, 2)
    big = Real("Big", default=3.0e38)
    with Program() as logic:
        with Rung():
            calc(big * 10, zero)
            calc(zero - zero, nan)
            unpack_to_words(nan, words.select(1, 2))
    tags = _one_scan(runner_factory, logic)
    assert (tags["W1"], tags["W2"]) == (0xFFFF, 0xFFFF)


def test_a_plain_copy_across_text_converts(runner_factory):
    real = Real("R", default=66.7)
    digit = Int("Digit", default=99)
    with Program() as logic:
        with Rung():
            copy("Z", txt[901])
            copy(real, txt[901])
            copy(txt[901], digit)
    tags = _one_scan(runner_factory, logic)
    text = "".join(tags[f"TXT{n}"] for n in range(901, 911))
    assert text == "66.7000000"
    assert tags["Digit"] == 6
    assert not tags.get(SC43, False)


@pytest.mark.parametrize(
    ("first", "second", "equal"),
    [("A", "B", True), ("A", "C", False), ("B", "A", False), ("B", "B", False)],
)
def test_a_txt_against_a_two_character_literal_reads_the_next_txt(
    runner_factory, first, second, equal
):
    chars = Block("CH", TagType.CHAR, 1, 3)
    same, differs = Bool("Same"), Bool("Differs")
    with Program() as logic:
        with Rung(chars[1] == "AB"):
            out(same)
        with Rung(chars[1] != "AB"):
            out(differs)
    tags = _one_scan(runner_factory, logic, {"CH1": first, "CH2": second})
    assert (tags["Same"], tags["Differs"]) == (equal, not equal)


def test_a_literal_longer_than_the_block_compares_as_written():
    chars = Block("CH", TagType.CHAR, 1, 2)
    assert isinstance(chars[1] == "AB", AllCondition)
    assert isinstance(chars[2] == "AB", CompareEq)


def test_a_named_array_window_reads_the_next_slots_of_its_layout(runner_factory):
    """Instance-major, as ``map_to`` puts it on TXT: ``Pair[1].a == "XY"`` reads
    Pair1_a then Pair1_b, the two TXTs a CLICK compares."""
    from pyrung.core import Char, named_array

    @named_array(Char, count=2, stride=2)
    class Pair:
        a = ""
        b = ""

    @named_array(Char, count=2, stride=3)
    class Padded:
        a = ""
        b = ""

    window = Pair.a[1] == "XYZ"
    assert isinstance(window, AllCondition)
    assert [c.tag.name for c in window.conditions] == ["Pair1_a", "Pair1_b", "Pair2_a"]
    # Past the last instance, or onto a slot no field holds: compared as written.
    assert isinstance(Pair.b[2] == "XY", CompareEq)
    assert isinstance(Padded.b[1] == "XY", CompareEq)

    same, differs = Bool("Same"), Bool("Differs")
    with Program() as logic:
        with Rung(Pair.a[1] == "XY"):
            out(same)
        with Rung(Pair.a[1] != "XY"):
            out(differs)
    tags = _one_scan(runner_factory, logic, {"Pair1_a": "X", "Pair1_b": "Y", "Pair2_a": "Q"})
    assert (tags["Same"], tags["Differs"]) == (True, False)
    tags = _one_scan(runner_factory, logic, {"Pair1_a": "X", "Pair1_b": "Q", "Pair2_a": "Y"})
    assert (tags["Same"], tags["Differs"]) == (False, True)


def _window_shape(cond):
    if isinstance(cond, AllCondition):
        return [c.tag.name for c in cond.conditions]
    return ("as written", cond.tag.name)


def _mapped_banks():
    """The reviewer's four mappings onto a fresh TXT bank, and the compares over them."""
    from pyrung.core import Char, named_array

    bank = ClickBlocks().txt
    msg = Block("Msg", TagType.CHAR, 1, 2)  # out of order: Msg1 on TXT5, Msg2 on TXT3
    msg[1].map_to(bank[5])
    msg[2].map_to(bank[3])
    code = Char("Code")  # a lone Char on TXT21
    code.map_to(bank[21])
    inorder = Block("MsgC", TagType.CHAR, 1, 4)  # in order on TXT31..34
    inorder.map_to(bank.select(31, 34))

    @named_array(Char, count=2, stride=3)
    class Padded:  # TXT41..46, a padding slot after each instance
        a = ""
        b = ""

    Padded.map_to(bank.select(41, 46))
    return bank, msg, code


def test_a_mapped_tag_reads_the_txts_after_the_one_it_was_mapped_to(runner_factory):
    """``bank[n] == "AB"`` compares TXTn and TXTn+1, as a CLICK does, whichever
    tags hold them, whether or not the bank's slots were read before."""
    bank, _, _ = _mapped_banks()
    expected = [
        ["Msg1", "TXT6"],
        ["Code", "TXT22"],
        ["MsgC4", "TXT35"],
        ["Padded1_b", "TXT43"],
    ]

    def shapes():
        return [
            _window_shape(bank[5] == "AB"),
            _window_shape(bank[21] == "AB"),
            _window_shape(bank[34] == "XY"),
            _window_shape(bank[42] == "XY"),
        ]

    assert shapes() == expected
    for addr in (3, 5, 6, 21, 22, 34, 35, 42, 43):
        _ = bank[addr]
    assert shapes() == expected

    names = ("OutOfOrder", "Lone", "InOrder", "PaddedHit")
    out_of_order, lone, in_order, padded = (Bool(n) for n in names)
    with Program() as logic:
        with Rung(bank[5] == "AB"):
            out(out_of_order)
        with Rung(bank[21] == "AB"):
            out(lone)
        with Rung(bank[34] == "XY"):
            out(in_order)
        with Rung(bank[42] == "XY"):
            out(padded)
    hit = {"Msg1": "A", "TXT6": "B", "Code": "A", "TXT22": "B"}
    hit |= {"MsgC4": "X", "TXT35": "Y", "Padded1_b": "X", "TXT43": "Y"}
    tags = _one_scan(runner_factory, logic, {**hit, "Msg2": "Q"})
    assert [tags[n] for n in names] == [True] * 4
    # Msg2 (on TXT3) is not the character after Msg1: a CLICK reads TXT6.
    tags = _one_scan(runner_factory, logic, {**hit, "Msg2": "B", "TXT6": "Q"})
    assert tags["OutOfOrder"] is False


def test_a_logical_compare_built_after_mapping_reads_the_hardware(runner_factory):
    """``msg[1] == "AB"`` with Msg1 on TXT5 compares TXT5 and TXT6; built before
    the mapping it reads its block (and export refuses it, tests/click)."""
    from pyrung.core import Char

    before_msg = Block("Msg", TagType.CHAR, 1, 2)
    before_code = Char("Code")
    built_before = [_window_shape(before_msg[1] == "AB"), _window_shape(before_code == "AB")]
    assert built_before == [["Msg1", "Msg2"], ("as written", "Code")]

    bank, msg, code = _mapped_banks()
    assert _window_shape(msg[1] == "AB") == ["Msg1", "TXT6"]
    assert _window_shape(code == "AB") == ["Code", "TXT22"]
    for addr in (5, 6, 21, 22):
        _ = bank[addr]
    assert _window_shape(msg[1] == "AB") == ["Msg1", "TXT6"]

    same = Bool("Same")
    with Program() as logic:
        with Rung(msg[1] == "AB", code == "CD"):
            out(same)
    patch = {"Msg1": "A", "TXT6": "B", "Code": "C", "TXT22": "D"}
    assert _one_scan(runner_factory, logic, {**patch, "Msg2": "Q"})["Same"] is True
    assert _one_scan(runner_factory, logic, {**patch, "Msg2": "B", "TXT6": "Q"})["Same"] is False


def test_a_char_mapping_alone_does_not_hold_its_bank():
    """The slot a Char is mapped to is kept weakly: once nothing holds the bank, the
    tag is mapped nowhere, and compares as a Char of no block."""
    import gc
    import weakref

    from pyrung.core import Char

    code = Char("Code")
    bank = Block("LifeTxt", TagType.CHAR, 1, 4)
    code.map_to(bank[2])
    bank_ref = weakref.ref(bank)
    del bank
    gc.collect()
    assert bank_ref() is None
    assert _window_shape(code == "AB") == ("as written", "Code")
