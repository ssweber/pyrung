"""CLICK validation of the Copy family, where laddercodec and pyrung's own checks meet.

laddercodec judges copy(), blockcopy(), fill() and the pack and unpack calls. These tests
cover the places where pyrung keeps a check of its own, or keeps one finding of two. They
also cover how a refusal prints: the call, the caret under the refused argument, and the hint.
"""

from __future__ import annotations

import dataclasses

import pytest
from laddercodec import BlockCopy, Code, Copy, Fill, Pack, Unpack

from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder
from pyrung.click.ladder import LadderExportError
from pyrung.click.validation import (
    CLK_BANK_NOT_WRITABLE,
    CLK_COPY_RANGE_NOT_CONSECUTIVE,
    CLK_PTR_CONTEXT_ONLY_COPY,
    validate_click_program,
)
from pyrung.core import Block, Bool, Dint, Int, TagType, udt
from pyrung.core.copy_converters import to_value
from pyrung.core.program import (
    Program,
    blockcopy,
    copy,
    fill,
    forloop,
    pack_bits,
    pack_text,
    pack_words,
    rung,
    unpack_to_bits,
    unpack_to_words,
)

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()


def _program(write) -> Program:
    prog = Program(strict=False)
    with prog:
        with rung(x[1]):
            write()
    return prog


def _at(kind: str) -> str:
    return f"main.rung[1].instruction[0]({kind}).instruction"


def _errors(write):
    return validate_click_program(_program(write), TagMap(), mode="strict").errors


# --- A Fill into a read-only SD ---------------------------------------------------------


@pytest.mark.parametrize(
    "write",
    [lambda: fill(5, sd.select(1, 3)), lambda: fill(5, sd.select(29, 30))],
    ids=["SD1..SD3", "SD29..SD30"],
)
def test_a_fill_onto_a_read_only_sd_is_refused(write):
    # SD29 is writable and SD30 is not. CLICK refuses the Fill if any SD in it is read-only.
    (finding,) = _errors(write)
    assert finding.code == Code.NOT_WRITABLE
    assert "OBJ-004" in finding.message
    with pytest.raises(LadderExportError, match=Code.NOT_WRITABLE):
        pyrung_to_ladder(_program(write), TagMap())


# --- A Fill of one register -------------------------------------------------------------


_BANK, _INCOMPATIBLE = Code.BANK_NOT_ALLOWED, Code.COPY_BANK_INCOMPATIBLE


@pytest.mark.parametrize(
    "write, codes",
    [
        (lambda: fill(c[1], y.select(1, 1)), [_BANK, _BANK]),
        (lambda: fill(c[1], c.select(2, 2)), [_BANK, _BANK]),
        (lambda: fill(x[1], y.select(1, 1)), [_BANK, _BANK]),
        (lambda: fill(ds[1], txt.select(5, 5)), [_INCOMPATIBLE]),
        (lambda: fill(df[1], txt.select(5, 5)), [_INCOMPATIBLE]),
    ],
    ids=["C->Y", "C->C", "X->Y", "DS->TXT", "DF->TXT"],
)
def test_a_fill_of_one_register_must_pass_as_a_fill(write, codes):
    # The export writes it as a Copy, and CLICK's Copy accepts these banks. CLICK's Fill does
    # not, so pyrung refuses the fill() as a Fill. A Fill refuses a bit as its value and as
    # its destination, and each refusal has its own finding.
    assert [f.code for f in _errors(write)] == codes


def test_a_fill_of_a_bit_suggests_a_copy_and_a_fill_of_a_constant_suggests_latch():
    value, dest = _errors(lambda: fill(c[1], y.select(1, 1)))
    assert (
        value.suggestion
        == dest.suggestion
        == ("Copy the bit into each destination bit with copy().")
    )
    (constant,) = _errors(lambda: fill(5, c.select(1, 4)))
    assert constant.suggestion == "Set or clear the bits with latch() or reset() on the range."


def test_a_block_copy_into_yd_suggests_copy_or_math():
    (finding,) = _errors(lambda: blockcopy(ds.select(1, 2), yd.select(1, 2)))
    assert finding.code == Code.BANK_NOT_ALLOWED
    assert finding.suggestion == "Write each YD with copy() or math()."


# --- Ranges that are not consecutive addresses ------------------------------------------


def _scattered():
    """Return bit and word blocks whose tags map onto addresses that are not one run.

    map_to names the CLICK tags after the block, so each test builds its own.
    """
    k = ClickBlocks()
    bits, words = Block("Bits", TagType.BOOL, 1, 4), Block("Words", TagType.INT, 1, 4)
    mapping = TagMap(
        [
            bits[1].map_to(k.c[1]),
            bits[2].map_to(k.c[5]),
            bits[3].map_to(k.c[3]),
            bits[4].map_to(k.c[9]),
            words[1].map_to(k.ds[1]),
            words[2].map_to(k.ds[5]),
            words[3].map_to(k.ds[13]),
            words[4].map_to(k.ds[9]),
        ],
        include_system=False,
    )
    return bits, words, k, mapping


def _scattered_program(write):
    bits, words, k, mapping = _scattered()
    prog = Program(strict=False)
    with prog:
        with rung(k.x[1]):
            write(bits, words, k)
    return prog, mapping


_SCATTERED_WRITES = {
    "blockcopy-source": lambda b, w, k: blockcopy(w.select(1, 4), k.ds.select(21, 24)),
    "blockcopy-dest": lambda b, w, k: blockcopy(k.ds.select(21, 24), w.select(1, 4)),
    "fill-dest": lambda b, w, k: fill(5, w.select(1, 4)),
    "pack-bits-source": lambda b, w, k: pack_bits(b.select(1, 4), k.ds[20]),
    "pack-words-source": lambda b, w, k: pack_words(w.select(1, 2), k.dd[20]),
    "unpack-bits-dest": lambda b, w, k: unpack_to_bits(k.ds[20], b.select(1, 4)),
    "unpack-words-dest": lambda b, w, k: unpack_to_words(k.dd[20], w.select(1, 2)),
}


@pytest.mark.parametrize("write", _SCATTERED_WRITES.values(), ids=_SCATTERED_WRITES.keys())
def test_a_range_of_scattered_tags_is_refused(write):
    # The export would write the range as a list, such as [DS1,DS5,DS13,DS9]. CLICK has no
    # list form.
    prog, mapping = _scattered_program(write)
    (finding,) = validate_click_program(prog, mapping, mode="strict").errors
    assert finding.code == CLK_COPY_RANGE_NOT_CONSECUTIVE
    assert finding.message.startswith("CLICK refuses ")
    assert finding.suggestion == (
        "Map the tags to consecutive addresses in one bank, or write one call for each run "
        "of consecutive addresses."
    )


@pytest.mark.parametrize("write", _SCATTERED_WRITES.values(), ids=_SCATTERED_WRITES.keys())
def test_the_export_refuses_a_range_of_scattered_tags_even_when_allowed(write):
    prog, mapping = _scattered_program(write)
    with pytest.raises(LadderExportError, match=CLK_COPY_RANGE_NOT_CONSECUTIVE):
        pyrung_to_ladder(prog, mapping, allow=[CLK_COPY_RANGE_NOT_CONSECUTIVE])


@pytest.mark.parametrize(
    "write",
    [
        lambda: fill(1, y.select(1, 21)),
        lambda: blockcopy(ds.select(1, 17), y.select(1, 21)),
        lambda: pack_bits(x.select(1, 21), dh[1]),
        lambda: unpack_to_bits(dh[1], y.select(1, 21)),
    ],
    ids=["fill", "blockcopy", "pack", "unpack"],
)
def test_an_x_or_y_range_across_a_slot_is_refused(write):
    # y.select(1, 21) holds Y001..Y016 and Y021, because Y017..Y020 do not exist.
    (finding,) = _errors(write)
    assert finding.code == CLK_COPY_RANGE_NOT_CONSECUTIVE


@pytest.mark.parametrize(
    "write",
    [
        lambda: fill(1, y.select(17, 20)),
        lambda: pack_bits(x.select(17, 17), dh[1]),
        lambda: unpack_to_bits(dh[1], y.select(17, 20)),
    ],
    ids=["fill", "pack", "unpack"],
)
def test_a_range_that_holds_no_address_is_refused(write):
    # X017..X020 and Y017..Y020 do not exist, so the range holds no tag.
    (finding,) = _errors(write)
    assert finding.code == CLK_COPY_RANGE_NOT_CONSECUTIVE
    assert finding.suggestion == "Select a range that holds CLICK addresses."


def test_a_finding_on_two_ranges_says_clicks_rule_once():
    (finding,) = _errors(lambda: blockcopy(x.select(1, 21), y.select(1, 21)))
    # X1..X21 is CLICK's own X bank, so the message says what it holds, not what it maps onto.
    assert " X1..X21 holds [X001," in finding.message
    assert " Y1..Y21 holds [Y001," in finding.message
    assert "maps onto" not in finding.message
    assert finding.message.count("CLICK takes a range only as consecutive addresses") == 1


def _in_a_loop(write):
    def body():
        with forloop(3):
            write()

    return body


_LISTS = {
    "pack-bits": lambda: pack_bits([c[1], c[2]], dh[1]),
    "pack-bits-tuple": lambda: pack_bits((c[1], c[2]), dh[1]),
    "pack-bits-one-tag": lambda: pack_bits(c[1], dh[1]),
    "unpack-bits": lambda: unpack_to_bits(dh[1], [c[1], c[2]]),
    "pack-words": lambda: pack_words([ds[1], ds[2]], dd[1]),
    "unpack-words": lambda: unpack_to_words(dd[1], [ds[1], ds[2]]),
    "pack-text": lambda: pack_text([txt[1], txt[2]], ds[1]),
    "pack-bits-in-a-loop": _in_a_loop(lambda: pack_bits([c[1], c[2]], dh[1])),
}


@pytest.mark.parametrize("write", _LISTS.values(), ids=_LISTS.keys())
def test_a_pack_or_unpack_of_a_list_is_refused_when_built(write):
    # These built, then raised TypeError on their first scan.
    with pytest.raises(TypeError, match=r"a range from \.select\(\)"):
        _program(write)


_REVERSED = {
    "blockcopy-source": (
        lambda: blockcopy(ds.select(1, 4).reverse(), ds.select(10, 13)),
        "blockcopy(DS1..DS4.reverse(), DS10..DS13)",
    ),
    "blockcopy-dest": (
        lambda: blockcopy(ds.select(1, 4), ds.select(10, 13).reverse()),
        "blockcopy(DS1..DS4, DS10..DS13.reverse())",
    ),
    "fill": (lambda: fill(5, ds.select(1, 4).reverse()), "fill(5, DS1..DS4.reverse())"),
    "pack-bits": (
        lambda: pack_bits(c.select(1, 4).reverse(), dh[1]),
        "pack_bits(C1..C4.reverse(), DH1)",
    ),
    "unpack-bits": (
        lambda: unpack_to_bits(dh[1], c.select(1, 4).reverse()),
        "unpack_to_bits(DH1, C1..C4.reverse())",
    ),
    "pack-text": (
        lambda: pack_text(txt.select(1, 3).reverse(), ds[1]),
        "pack_text(TXT1..TXT3.reverse(), DS1)",
    ),
}


@pytest.mark.parametrize("write, call", _REVERSED.values(), ids=_REVERSED.keys())
def test_a_reversed_range_is_refused_for_its_order(write, call):
    (finding,) = _errors(write)
    assert finding.code == CLK_COPY_RANGE_NOT_CONSECUTIVE
    assert finding.message.startswith(f"CLICK refuses {call} at ")
    assert "CLICK takes a range only in ascending order." in finding.message
    assert finding.suggestion == "Remove .reverse(), or reverse the data in another way."


# --- Pointers ---------------------------------------------------------------------------


_DS_POINTER = "Use a DS tag as the pointer source for copy() addressing."


@pytest.mark.parametrize(
    "write, code, call, suggestion",
    [
        (
            lambda: copy(ds[dd[1]], ds[2]),
            Code.POINTER_NOT_DS,
            "copy(DS[DD1], DS2)",
            _DS_POINTER,
        ),
        (
            lambda: copy(ds[2], ds[dd[1]]),
            Code.POINTER_NOT_DS,
            "copy(DS2, DS[DD1])",
            _DS_POINTER,
        ),
        (
            lambda: fill(ds[ds[1]], ds.select(5, 8)),
            Code.POINTER_OUTSIDE_COPY,
            "fill(DS[DS1], DS5..DS8)",
            "Pointer DS[DS1] can only be used inside copy(). Use a direct tag reference here "
            "instead.",
        ),
        (
            lambda: pack_bits(c.select(1, 16), ds[ds[1]]),
            Code.POINTER_OUTSIDE_COPY,
            "pack_bits(C1..C16, DS[DS1])",
            "Pointer DS[DS1] can only be used inside copy(). Use a direct tag reference here "
            "instead.",
        ),
    ],
    ids=["copy-source", "copy-dest", "fill", "pack_bits"],
)
def test_a_pointer_fault_in_the_copy_family_has_one_finding(write, code, call, suggestion):
    (finding,) = _errors(write)
    assert finding.code == code
    kind = {
        "copy": "CopyInstruction",
        "fill": "FillInstruction",
        "pack_bits": "PackBitsInstruction",
    }
    at = _at(kind[call.partition("(")[0]])
    assert finding.message.startswith(f"CLICK refuses {call} at {at}. ")
    # The advice is pyrung's, from the finding that laddercodec's finding replaces.
    assert finding.suggestion == suggestion


def test_a_copy_through_a_pointer_into_sd_is_accepted():
    # laddercodec editor_grids copy_single: CLICK accepts SD[DS1] -> DS1.
    assert _errors(lambda: copy(sd[ds[1]], ds[1])) == ()


def test_each_refused_pointer_has_its_own_finding_and_advice():
    ptr_a, ptr_b = Dint("PtrA"), Dint("PtrB")
    prog = _program(lambda: copy(ds[ptr_a], ds[ptr_b]))
    mapping = TagMap([ptr_a.map_to(dd[50]), ptr_b.map_to(dd[51])], include_system=False)
    source, dest = validate_click_program(prog, mapping, mode="strict").errors
    assert source.code == dest.code == Code.POINTER_NOT_DS
    # copy(DS[PtrA], DS[PtrB])
    assert source.display.frames[0].caret == (0, 5, 8)
    assert dest.display.frames[0].caret == (0, 15, 8)
    # The advice of pyrung's pointer finding for each operand stays with that operand.
    assert source.suggestion == (
        "Pointer 'PtrA' is mapped to DD memory. Remap it to a DS address so Click hardware can "
        "use it as a pointer."
    )
    assert dest.suggestion == (
        "Pointer 'PtrB' is mapped to DD memory. Remap it to a DS address so Click hardware can "
        "use it as a pointer."
    )
    assert source.suggestion in source.display.hint


def test_pointer_advice_for_an_operand_that_no_refusal_names_is_kept():
    from pyrung.click.validation.fields import CopyFamilyResult, _Refusal
    from pyrung.core.validation.display import Frame

    def refusal(attribute):
        return _Refusal(
            code=Code.POINTER_NOT_DS,
            severity="error",
            message="",
            location="",
            frame=Frame(location="Main:R1"),
            problem="",
            detail="",
            click_id=None,
            advice=None,
            attribute=attribute,
        )

    result = CopyFamilyResult([], (refusal("source"), refusal("dest")))
    on_dest = result.findings({(Code.POINTER_NOT_DS, "dest"): ["Remap the pointer."]})
    assert [f.suggestion for f in on_dest] == [None, "Remap the pointer."]
    # The first refusal of the code takes advice for an operand that no refusal names.
    elsewhere = result.findings({(Code.POINTER_NOT_DS, "value"): ["Remap the pointer."]})
    assert [f.suggestion for f in elsewhere] == ["Remap the pointer.", None]
    assert elsewhere[0].display.hint == "Remap the pointer."


def test_a_pointer_outside_the_copy_family_keeps_pyrungs_code():
    prog = Program(strict=False)
    with prog:
        with rung(ds[dd[1]] == 5):
            copy(1, ds[2])
    errors = validate_click_program(prog, TagMap(), mode="strict").errors
    assert [f.code for f in errors] == [CLK_PTR_CONTEXT_ONLY_COPY]


# --- Advice -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "write",
    [
        lambda: copy(txt[1], ds[1]),
        lambda: copy(df[1], txt[1]),
        lambda: blockcopy(txt.select(1, 3), ds.select(1, 3)),
    ],
    ids=["TXT->DS", "DF->TXT", "block TXT->DS"],
)
def test_a_plain_copy_across_text_is_checked_with_the_converter_it_exports(write):
    # CLICK saves these copies only with a converter. The export writes the one that pyrung
    # runs, so laddercodec checks the copy with that converter.
    assert _errors(write) == ()


def test_a_finding_names_the_call_and_clicks_error():
    (finding,) = _errors(lambda: pack_bits(x.select(1, 16), ds[1]))
    assert finding.code == Code.COPY_BANK_INCOMPATIBLE
    assert finding.message.startswith(
        f"CLICK refuses pack_bits(X1..X16, DS1) at {_at('PackBitsInstruction')}. "
    )
    assert "OBJ-024" in finding.message


@pytest.mark.parametrize(
    "write",
    [lambda: pack_bits(c.select(1, 1), ds[1]), lambda: unpack_to_bits(ds[1], c.select(1, 1))],
    ids=["pack_bits", "unpack_to_bits"],
)
def test_a_pack_of_one_bit_is_refused(write):
    (finding,) = _errors(write)
    assert finding.code == Code.RANGE_NOT_ASCENDING
    assert finding.suggestion is None


def test_a_block_copy_of_one_register_suggests_copy():
    (finding,) = _errors(lambda: blockcopy(ds.select(1, 1), ds.select(5, 5)))
    assert finding.code == Code.RANGE_NOT_ASCENDING
    assert finding.suggestion == "Use copy() for one register."


@pytest.mark.parametrize(
    "write, token",
    [
        (lambda: copy(14, dh[1]), "copy(14,DH1)"),
        (lambda: copy(30, yd[1]), "copy(30,YD1)"),
        (lambda: fill(14, dh.select(1, 1)), "copy(14,DH1)"),
    ],
    ids=["copy-DH", "copy-YD", "fill-DH"],
)
def test_a_constant_that_hex_would_spell_with_an_e_exports_in_decimal(write, token):
    # In hex, 14 is Eh and 30 is 1Eh. CLICK would read either as a real, so the export
    # writes the constant in decimal.
    prog = _program(write)
    assert validate_click_program(prog, TagMap(), mode="strict").errors == ()
    assert token in pyrung_to_ladder(prog, TagMap()).main_rows[1]


@pytest.mark.parametrize("dest", [(1, 2), (999, 1000)], ids=["TXT1", "TXT999"])
@pytest.mark.parametrize("text", ["AB", "HELLO"])
def test_a_fill_of_more_than_one_character_is_refused(text, dest):
    # laddercodec editor_grids copy_fill: CLICK refuses "AB" (OBJ-023). It accepts "HELLO",
    # but its save holds the Real 0.0 and not the text.
    (finding,) = _errors(lambda: fill(text, txt.select(*dest)))
    assert finding.code == Code.CONSTANT_OUT_OF_RANGE


# --- How a refusal prints ---------------------------------------------------------------


def _named_errors(write):
    """Return the strict errors of ``write(count, valve)``, with Count on DS1 and Valve on C1."""
    count, valve = Int("Count"), Bool("Valve")
    prog = _program(lambda: write(count, valve))
    mapping = TagMap([count.map_to(ds[1]), valve.map_to(c[1])], include_system=False)
    return validate_click_program(prog, mapping, mode="strict").errors


@pytest.mark.parametrize(
    "write, code, kind, line, caret",
    [
        (
            lambda count, valve: copy(count, valve),
            Code.COPY_BANK_INCOMPATIBLE,
            "CopyInstruction",
            "copy(Count, Valve)",
            (0, 12, 5),
        ),
        (
            lambda count, valve: fill(5, sd.select(1, 3)),
            Code.NOT_WRITABLE,
            "FillInstruction",
            "fill(5, SD1..SD3)",
            (0, 8, 8),
        ),
        (
            lambda count, valve: pack_bits(x.select(1, 16), count),
            Code.COPY_BANK_INCOMPATIBLE,
            "PackBitsInstruction",
            "pack_bits(X1..X16, Count)",
            (0, 19, 5),
        ),
        (
            lambda count, valve: blockcopy(ds.select(1, 1), ds.select(5, 5)),
            Code.RANGE_NOT_ASCENDING,
            "BlockCopyInstruction",
            "blockcopy(DS1..DS1, DS5..DS5)",
            (0, 10, 8),
        ),
    ],
    ids=["copy", "fill", "pack_bits", "blockcopy"],
)
def test_a_refusal_marks_the_refused_argument(write, code, kind, line, caret):
    (finding,) = _named_errors(write)
    assert finding.code == code
    (frame,) = finding.display.frames
    assert (frame.location, frame.lines, frame.caret) == ("Main:R1", (line,), caret)
    # laddercodec's short text labels the caret, so no problem line repeats it.
    assert frame.caret_label
    assert finding.display.problem == ""
    assert finding.display.hint
    assert finding.location == _at(kind)


@pytest.mark.parametrize(
    "write, line, caret",
    [
        (lambda: copy(sd[1], sd[1]), "copy(SD1, SD1)", (0, 10, 3)),
        (lambda: copy(sd[10], sd[1]), "copy(SD10, SD1)", (0, 11, 3)),
    ],
    ids=["same-name", "name-inside-a-name"],
)
def test_the_caret_marks_the_refused_argument_and_not_the_first_match(write, line, caret):
    # SD1 is read-only, so CLICK refuses it as the destination.
    (finding,) = _errors(write)
    assert finding.code == Code.NOT_WRITABLE
    (frame,) = finding.display.frames
    assert (frame.lines, frame.caret) == ((line,), caret)


def test_the_label_leaves_out_the_name_under_the_caret():
    (finding,) = _named_errors(lambda count, valve: copy(count, valve))
    label = finding.display.frames[0].caret_label
    assert "Count" in label
    assert "Valve" not in label
    # pyrung adds CLICK's id, which laddercodec's explanation leaves out.
    assert "OBJ-024" in finding.display.hint


def test_a_refusal_with_no_explanation_keeps_laddercodecs_message():
    (finding,) = _errors(lambda: copy(ds[1], txt[1], convert=to_value))
    assert finding.code == Code.COPY_CONVERTER_BANK
    (frame,) = finding.display.frames
    assert (frame.lines, frame.caret) == (("copy(DS1, TXT1, convert=to_value)",), (0, 24, 8))
    assert frame.caret_label == ""
    assert finding.display.problem
    assert finding.display.hint == finding.suggestion


def test_a_label_names_an_address_without_quotes_when_the_map_names_none():
    # laddercodec quotes each address when it gets no names at all.
    prog = _program(lambda: copy(ds[2], c[2]))
    (finding,) = validate_click_program(prog, TagMap(include_system=False), mode="strict").errors
    label = finding.display.frames[0].caret_label
    assert "C2" in label
    assert "'C2'" not in label
    assert "'DS2'" not in label


def test_a_label_spells_a_field_as_the_call_does():
    @udt()
    class Motor:
        speed: Int

    valve = Bool("Valve")
    prog = _program(lambda: copy(Motor.speed, valve))
    mapping = TagMap([Motor.speed.map_to(ds[1]), valve.map_to(c[1])], include_system=False)
    (finding,) = validate_click_program(prog, mapping, mode="strict").errors
    assert finding.display.frames[0].lines == ("copy(Motor.speed, Valve)",)
    assert "Motor.speed (DS1)" in finding.display.frames[0].caret_label


def test_a_label_spells_a_range_as_the_call_does():
    (finding,) = _named_errors(lambda count, valve: pack_bits(x.select(1, 16), count))
    assert "X1..X16" in finding.display.frames[0].caret_label


def test_a_problem_line_names_the_tags():
    count = Int("Count")
    prog = _program(lambda: copy(count, txt[1], convert=to_value))
    mapping = TagMap([count.map_to(ds[1])], include_system=False)
    (finding,) = validate_click_program(prog, mapping, mode="strict").errors
    assert finding.display.frames[0].caret_label == ""
    assert "Count (DS1)" in finding.display.problem
    assert "Count (DS1)" in finding.message


def test_an_export_error_names_each_place_once():
    prog = _program(lambda: copy(sd[10], sd[1]))
    with pytest.raises(LadderExportError) as exc_info:
        pyrung_to_ladder(prog, TagMap())
    text = str(exc_info.value)
    assert " --> Main:R1" in text
    assert _at("CopyInstruction") not in text


def test_each_copy_family_field_has_an_argument():
    # A field that laddercodec renames or adds would lose its caret.
    from pyrung.click.validation.fields import _FIELD_ARGUMENTS

    fields = {
        f"{cls.__name__}.{field.name}"
        for cls in (Copy, BlockCopy, Fill, Pack, Unpack)
        for field in dataclasses.fields(cls)
        # The verb is the kind.
        if field.name != "kind"
    }
    assert set(_FIELD_ARGUMENTS) == fields


# --- allow= -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code, replacement",
    [
        ("CLK_COPY_BANK_INCOMPATIBLE", "copy-bank-incompatible or bank-not-allowed"),
        ("CLK_COPY_CONSTANT_INTO_BIT", "constant-into-bit or bank-not-allowed"),
        (
            "CLK_COPY_CONVERTER_INCOMPATIBLE",
            "copy-converter-bank, copy-txt-needs-convert or option-not-offered",
        ),
        ("CLK_PACK_TEXT_BANK_INCOMPATIBLE", "pack-kind-mismatch or copy-bank-incompatible"),
    ],
)
def test_allow_names_what_replaced_a_removed_code(code, replacement):
    with pytest.raises(ValueError, match=f"no longer reports {code}. Pass {replacement} to"):
        pyrung_to_ladder(_program(lambda: copy(ds[1], ds[2])), TagMap(), allow=(code,))


def test_allow_refuses_a_code_that_no_finding_has():
    with pytest.raises(ValueError, match="'copy-bank-incompatable'. It is neither"):
        pyrung_to_ladder(
            _program(lambda: copy(ds[1], ds[2])), TagMap(), allow=("copy-bank-incompatable",)
        )


def test_allow_takes_a_pyrung_code_and_a_laddercodec_code():
    write = lambda: fill(ds[1], txt.select(5, 5))  # noqa: E731
    with pytest.raises(LadderExportError, match="copy-bank-incompatible"):
        pyrung_to_ladder(_program(write), TagMap(), allow=(CLK_BANK_NOT_WRITABLE,))
    # With laddercodec's code allowed, the export's own refusal is reached.
    with pytest.raises(LadderExportError, match="Copy between text and a number converts"):
        pyrung_to_ladder(_program(write), TagMap(), allow=(Code.COPY_BANK_INCOMPATIBLE,))


def test_allow_refuses_a_bare_string():
    with pytest.raises(ValueError, match="Pass allow= a list of codes"):
        pyrung_to_ladder(
            _program(lambda: copy(ds[1], ds[2])), TagMap(), allow="copy-bank-incompatible"
        )
