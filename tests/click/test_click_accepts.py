"""Forms that CLICK v4's editor accepts and that the export once refused.

Each test names the CLICK answer it rests on.  The answer files are:
- clicknick ``buildbot/jobs/editor-answers/RESULTS.md`` (rows A6 to A10)
- clicknick ``buildbot/jobs/lc-diff-click/RESULTS.md`` (rows A7-pairs and A-txt-yd)
- clicknick ``buildbot/jobs/copy-encodings/RESULTS.md``
- clicknick ``buildbot/jobs/lc-diff-click/RESULTS6.md`` (Copy rows 1 to 8)
- laddercodec ``tests/fixtures/editor_grids/*.answers.json``

A form CLICK refuses next to an accepted one stays refused.
"""

from __future__ import annotations

import dataclasses

import pytest
from laddercodec import Code

from pyrung.click import ClickBlocks, TagMap, ladder_to_pyrung, pyrung_to_ladder
from pyrung.click.ladder import LadderExportError
from pyrung.click.validation import (
    CLK_BANK_NOT_WRITABLE,
    CLK_COPY_WIDE_CONSTANT,
    CLK_MATH_MODE_MIXED,
    CLK_MATH_RANGE_NOT_CONTIGUOUS,
    CLK_MATH_WIDE_CONSTANT,
    CLK_RAND_BOUND_TYPE,
    validate_click_program,
)
from pyrung.core import Word, randint, to_ascii, to_value
from pyrung.core.program import Program, blockcopy, copy, fill, latch, math, out, reset, rung
from tests.click.helpers import exec_with_source, round_trip_logic

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()


def _program(write) -> Program:
    prog = Program(strict=False)
    with prog:
        with rung(x[1]):
            write()
    return prog


def _codes(write) -> set[str]:
    report = validate_click_program(_program(write), TagMap(), mode="strict")
    return {f.code for f in report.errors}


def _exports_as(write, token: str) -> Program:
    """Assert that *write* has no finding and exports as *token*."""
    prog = _program(write)
    assert _codes(write) == set()
    assert token in pyrung_to_ladder(prog, TagMap()).main_rows[1]
    return prog


def _exports_and_imports_as(write, token: str) -> None:
    """Assert that *write* exports as *token* and imports back to the same rung."""
    code, before, after = round_trip_logic(_exports_as(write, token), TagMap())
    assert before == after, code


def _refused(write, code: str) -> None:
    assert code in _codes(write)
    with pytest.raises(LadderExportError, match=code):
        pyrung_to_ladder(_program(write), TagMap())


# --- An Out coil on SC ----------------------------------------------------------------


@pytest.mark.parametrize("address", [162, 171, 303, 305, 326])
def test_an_out_coil_takes_the_sc_bits_clicks_out_picker_lists(address):
    # editor-answers A6 (SC162, SC171, SC303, SC326) and pickcheck.answers.json (SC305).
    _exports_and_imports_as(lambda: out(sc[address]), f"out(SC{address})")


@pytest.mark.parametrize("address", [1, 77, 307])
def test_an_out_coil_on_an_sc_click_refuses_is_still_refused(address):
    # editor-answers A6 (SC1) and pickcheck.answers.json (SC77, SC307): OBJ-003.
    _refused(lambda: out(sc[address]), CLK_BANK_NOT_WRITABLE)


def _not_writable_messages(write) -> list[str]:
    report = validate_click_program(_program(write), TagMap(), mode="strict")
    return [f.message for f in report.errors if f.code == CLK_BANK_NOT_WRITABLE]


@pytest.mark.parametrize(
    ("write", "call", "advice"),
    [
        (lambda: latch(sc[162]), "latch()", "Only out() can write it."),
        (lambda: reset(sc[162]), "reset()", "Only out() can write it."),
        (lambda: out(sc[1]), "out()", "Write to another address."),
    ],
    ids=["latch-sc", "reset-sc", "out-sc1"],
)
def test_a_refused_write_names_the_call_and_the_calls_that_can_write_there(write, call, advice):
    messages = _not_writable_messages(write)
    assert messages
    for message in messages:
        assert message.startswith(f"CLICK does not let {call} write ")
        assert message.endswith(f". {advice}")


def test_a_block_copy_into_yd_is_refused():
    # rule-decisions B1: CLICK refuses a Block copy from DS into YD (OBJ-004).
    _refused(lambda: blockcopy(ds.select(1, 2), yd.select(1, 2)), "bank-not-allowed")


# --- Math into SD and YD ----------------------------------------------------------------


@pytest.mark.parametrize("dest", [sd[29], sd[61], sd[147]], ids=lambda dest: dest.name)
def test_a_decimal_math_into_a_writable_sd_exports(dest):
    # editor-answers A9 (SD29) and pickcheck.answers.json (SD61, SD147).
    _exports_and_imports_as(
        lambda: math(ds[1] + 1, dest), f"math(DS1 + 1,{dest.name},mode=decimal)"
    )


@pytest.mark.parametrize("dest", [sd[1], sd[62], sd[148]], ids=lambda dest: dest.name)
def test_a_decimal_math_into_an_sd_click_refuses_is_still_refused(dest):
    # editor-answers A9 (SD1) and pickcheck.answers.json (SD62, SD148): OBJ-003.
    _refused(lambda: math(ds[1] + 1, dest), CLK_BANK_NOT_WRITABLE)


def test_a_hex_math_into_yd_exports():
    # editor-answers A10 and math_hex.answers.json: a hex Math accepts YD0 and YD1 as its
    # Result.  The import reads no YD yet.
    _exports_as(lambda: math(dh[1] + dh[2], yd[1]), "math(DH1 + DH2,YD1,mode=hex)")


# --- Copy into YD -----------------------------------------------------------------------
# The import reads no XD or YD address yet, so these check the export alone.


def test_a_copy_into_yd_exports():
    # copy_single.answers.json: "single DS1 -> YD0" is accepted.
    _exports_as(lambda: copy(ds[1], yd[1]), "copy(DS1,YD1)")


@pytest.mark.parametrize(
    "convert, token",
    [(to_value, "copy(TXT1,YD1,convert=to_value)"), (to_ascii, "copy(TXT1,YD1,convert=to_ascii)")],
    ids=["value", "ascii"],
)
def test_a_copy_of_text_into_yd_exports(convert, token):
    # lc-diff-click A-txt-yd: CLICK offers Copy Character Value and Copy ASCII Code Value.
    _exports_as(lambda: copy(txt[1], yd[1], convert=convert), token)


def test_a_copy_of_a_bit_into_yd_is_still_refused():
    # copy_single.answers.json: "single C1 -> YD0" is refused (OBJ-024).
    _refused(lambda: copy(c[1], yd[1]), "copy-bank-incompatible")


# --- Block copy into SD and between TXT -------------------------------------------------


@pytest.mark.parametrize("bank", [ds, dd, dh, df, sd], ids=lambda bank: bank.name)
def test_a_block_copy_into_sd31_exports(bank):
    # editor-answers A7 (DS1..DS2) and lc-diff-click A7-pairs (DD, DH, DF, SD).  CLICK sets
    # the end to SD32 for each, the DD and DF pairs too.
    name = bank.name
    _exports_and_imports_as(
        lambda: blockcopy(bank.select(1, 2), sd.select(31, 32)),
        f"blockcopy({name}1..{name}2,SD31..SD32)",
    )


def test_a_block_copy_onto_a_read_only_sd_is_still_refused():
    # editor-answers A7: DS1..DS2 into SD29 is refused (OBJ-018), since SD30 is read-only.
    _refused(lambda: blockcopy(ds.select(1, 2), sd.select(29, 30)), "not-writable")


def test_a_block_copy_between_txt_exports():
    # editor-answers A8: TXT1..TXT2 into TXT11 is accepted.
    _exports_and_imports_as(
        lambda: blockcopy(txt.select(1, 2), txt.select(11, 12)),
        "blockcopy(TXT1..TXT2,TXT11..TXT12)",
    )


# --- Hex Math ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bank, token",
    [(xd, "SUM ( XD1 : XD4 )"), (yd, "SUM ( YD1 : YD4 )")],
    ids=["XD", "YD"],
)
def test_a_sum_over_xd_or_yd_exports(bank, token):
    # math_hex.answers.json: SUM(XD1:XD8) is accepted.  XD0u sits between XD0 and XD1, so
    # XD1..XD4 is a run of CLICK addresses with no gap.  The import reads no XD or YD yet.
    _exports_as(lambda: math(bank.select(1, 4).sum(), dh[100]), f"math({token},DH100,mode=hex)")


def test_a_sum_from_xd0_is_still_refused():
    # CLICK's XD0:XD1 range runs through XD0u (math_hex.answers.json accepts SUM(XD0:XD0u)
    # and SUM(XD0u:XD1)).  A pyrung block of XD never contains XD0u.
    _refused(lambda: math(xd.select(0, 1).sum(), dh[100]), CLK_MATH_RANGE_NOT_CONTIGUOUS)


def test_a_hex_math_of_minus_one_exports_as_ffffh():
    # math_hex.answers.json: "hex DH1 = DH1 + FFFFh" is accepted.  A hex Math reads -1 as
    # FFFFh.
    _exports_and_imports_as(lambda: math(dh[1] + -1, dh[100]), "math(DH1 + FFFFh,DH100,mode=hex)")


def test_a_hex_rand_of_two_dh_exports():
    # math_hex.answers.json: RAND(DH1,DH2) is accepted.
    _exports_and_imports_as(
        lambda: math(randint(dh[1], dh[2]), dh[100]), "math(RAND ( DH1 , DH2 ),DH100,mode=hex)"
    )


def test_a_hex_rand_of_xd_or_yd_is_still_refused():
    # math_hex.answers.json: RAND(XD1,Ah) and RAND(YD1,Ah) are refused (OBJ-1002).
    _refused(lambda: math(randint(xd[1], yd[1]), dh[3]), CLK_RAND_BOUND_TYPE)


def test_a_hex_rand_of_words_mapped_to_xd_is_still_refused():
    # math_hex.answers.json: a RAND bound in XD is refused (OBJ-1002), whatever the tag's type.
    lo, hi, dest = Word("Lo"), Word("Hi"), Word("Dest")

    def write():
        math(randint(lo, hi), dest)

    tag_map = TagMap([lo.map_to(xd[1]), hi.map_to(xd[2]), dest.map_to(dh[3])])
    report = validate_click_program(_program(write), tag_map, mode="strict")
    assert CLK_RAND_BOUND_TYPE in {f.code for f in report.errors}


@pytest.mark.parametrize(
    "write",
    [lambda: math(ds[1] + 1, yd[1]), lambda: math(dh[1] + 1, sd[29])],
    ids=["decimal-into-YD", "hex-into-SD"],
)
def test_a_math_result_in_the_other_modes_bank_is_still_refused(write):
    # editor-answers A10 and sc_sd_writable.answers.json: YD is a hex Result and SD a
    # decimal one.  pyrung makes either Math a mixed one.
    _refused(write, CLK_MATH_MODE_MIXED)


@pytest.mark.parametrize("constant", [65536, 70000, -32769])
def test_a_hex_math_constant_past_16_bits_is_refused_even_when_allowed(constant):
    # math_hex.answers.json: 10000h is refused (OBJ-1009).  Export once wrote the low 16
    # bits (70000 as 1170h), a different number from the one the program reads.
    write = lambda: math(dh[1] + constant, dh[100])  # noqa: E731
    assert CLK_MATH_WIDE_CONSTANT in _codes(write)
    with pytest.raises(LadderExportError, match=f"Constant {constant} does not fit in 16 bits"):
        pyrung_to_ladder(_program(write), TagMap(), allow=(CLK_MATH_WIDE_CONSTANT,))


def test_a_hex_math_constant_with_a_fraction_is_reported():
    # math_hex.answers.json: CLICK refuses a fraction in a hex Math (OBJ-1001).
    _refused(lambda: math(dh[1] + 1.5, dh[100]), CLK_MATH_WIDE_CONSTANT)


# --- A Fill of one register -------------------------------------------------------------


def test_a_fill_of_one_yd_exports_as_the_copy_click_accepts():
    # A Fill of one register exports as a Copy.  copy_single.answers.json accepts a Copy
    # into YD0.
    _exports_as(lambda: fill(5, yd.select(1, 1)), "copy(5h,YD1)")


@pytest.mark.parametrize("start, end", [(0, 1), (1, 2), (1, 8)])
def test_a_fill_of_several_yd_exports(start, end):
    # rule-decisions N6: CLICK accepts a Fill of 5 into every range from YD0..YD1 to
    # YD1..YD8. It refuses YD0..YD9 (OBJ-003), which pyrung's yd block cannot name.
    _exports_as(lambda: fill(5, yd.select(start, end)), f"fill(5h,YD{start}..YD{end})")


# --- Copy of a number into text ---------------------------------------------------------


@pytest.mark.parametrize("source, name", [(ds[1], "DS1"), (dd[1], "DD1"), (dh[1], "DH1")])
def test_a_plain_copy_of_an_integer_into_txt_exports_as_clicks_copy_value(source, name):
    # copy-encodings: CLICK's dialog opens a DS into TXT on Copy Value with Suppress zero,
    # which it saves as conversion 1, to_text(suppress_zero=1).  An integer source writes
    # no Exponential flag, so the check on exponential= never sees one.
    _exports_and_imports_as(
        lambda: copy(source, txt[5]),
        f"copy({name},TXT5,convert=to_text(suppress_zero=1,termination_code=none))",
    )


# --- A plain Copy across text exports the converter that pyrung runs -------------------
# CLICK's Copy dialog has no plain Copy between TXT and a number register or constant.
# CLICK's editor saves each one with its conversion, except a Copy of TXT into DF.  The
# dialog greys out every conversion for that one, and the editor saves it plain.


@pytest.mark.parametrize(
    "write, token",
    [
        (lambda: copy(txt[1], ds[1]), "copy(TXT1,DS1,convert=to_value)"),
        (lambda: copy(txt[1], dd[1]), "copy(TXT1,DD1,convert=to_value)"),
        (lambda: copy(txt[1], dh[1]), "copy(TXT1,DH1,convert=to_value)"),
        (lambda: copy(txt[1], td[1]), "copy(TXT1,TD1,convert=to_value)"),
        (lambda: copy(txt[1], ctd[1]), "copy(TXT1,CTD1,convert=to_value)"),
        (lambda: copy(txt[1], sd[29]), "copy(TXT1,SD29,convert=to_value)"),
        (lambda: copy(txt[ds[5]], ds[1]), "copy(TXT[DS5],DS1,convert=to_value)"),
        (
            lambda: copy(df[1], txt[1]),
            "copy(DF1,TXT1,convert=to_text(exponential=0,termination_code=none))",
        ),
        (
            lambda: copy(-5, txt[1]),
            "copy(-5,TXT1,convert=to_text(suppress_zero=1,termination_code=none))",
        ),
        (
            lambda: copy(2.5, txt[1]),
            "copy(2.5,TXT1,convert=to_text(exponential=0,termination_code=none))",
        ),
        (
            lambda: blockcopy(txt.select(1, 3), ds.select(1, 3)),
            "blockcopy(TXT1..TXT3,DS1..DS3,convert=to_value)",
        ),
        (
            lambda: blockcopy(txt.select(1, 3), dd.select(1, 3)),
            "blockcopy(TXT1..TXT3,DD1..DD3,convert=to_value)",
        ),
        (
            lambda: blockcopy(txt.select(1, 3), dh.select(1, 3)),
            "blockcopy(TXT1..TXT3,DH1..DH3,convert=to_value)",
        ),
        (lambda: copy(txt[1], df[1]), "copy(TXT1,DF1)"),
    ],
    ids=[
        "txt-ds",
        "txt-dd",
        "txt-dh",
        "txt-td",
        "txt-ctd",
        "txt-sd",
        "txt-pointer-ds",
        "df-txt",
        "int-constant-txt",
        "real-constant-txt",
        "block-txt-ds",
        "block-txt-dd",
        "block-txt-dh",
        "txt-df-stays-plain",
    ],
)
def test_a_plain_copy_across_text_exports_its_converter(write, token):
    _exports_and_imports_as(write, token)


def test_a_plain_copy_of_txt_into_yd_exports_to_value():
    # The importer refuses any Copy into YD1, so this test checks the export only.
    _exports_as(lambda: copy(txt[1], yd[1]), "copy(TXT1,YD1,convert=to_value)")


def test_an_imported_plain_copy_across_text_exports_its_converter():
    # A CSV that holds a plain Copy across text still imports.  Its program runs the
    # converter, so the export writes it.
    bundle = pyrung_to_ladder(_program(lambda: copy(txt[1], ds[1])), TagMap())
    rows = tuple(
        tuple(cell.replace(",convert=to_value", "") for cell in row) for row in bundle.main_rows
    )
    code = ladder_to_pyrung(dataclasses.replace(bundle, main_rows=rows))
    assert "copy(TXT1, DS1)" in code
    ns: dict[str, object] = {}
    exec_with_source(code, ns)
    reexported = pyrung_to_ladder(ns["logic"], ns["mapping"])
    assert "copy(TXT1,DS1,convert=to_value)" in reexported.main_rows[1]


def test_a_number_constant_past_txt1000_is_refused():
    # CLICK saves -5 as two characters, so its Copy into TXT1000 refuses it (OBJ-003).
    assert Code.TEXT_PAST_BANK in _codes(lambda: copy(-5, txt[1000]))
    with pytest.raises(LadderExportError, match=Code.TEXT_PAST_BANK):
        pyrung_to_ladder(_program(lambda: copy(-5, txt[1000])), TagMap())
    # With laddercodec's code allowed, the export's own refusal is reached.
    with pytest.raises(LadderExportError, match=r"2-character constant, which runs past TXT1000"):
        pyrung_to_ladder(
            _program(lambda: copy(-5, txt[1000])), TagMap(), allow=(Code.TEXT_PAST_BANK,)
        )


def test_a_fill_of_one_txt_with_a_number_is_refused():
    # The Fill exports as a Copy, and CLICK's Copy of a number into TXT stores its digits.
    with pytest.raises(LadderExportError, match="A Copy between text and a number"):
        pyrung_to_ladder(_program(lambda: fill(5, txt.select(1, 1))), TagMap())


# --- Destination addresses that CLICK saves -------------------------------------------


@pytest.mark.parametrize(
    "write, token",
    [
        (
            lambda: blockcopy(txt.select(1, 3), ds[10]),
            "blockcopy(TXT1..TXT3,DS10..DS12,convert=to_value)",
        ),
        (lambda: blockcopy(ds.select(1, 3), ds[10]), "blockcopy(DS1..DS3,DS10..DS12)"),
    ],
    ids=["txt-into-ds", "ds-into-ds"],
)
def test_a_block_copy_into_one_address_exports_the_range_click_saves(write, token):
    # CLICK's Block Copy dialog takes only the first destination address and saves the end.
    _exports_and_imports_as(write, token)


def test_a_text_copy_into_a_range_exports_its_first_txt():
    # A Copy has one destination field, and CLICK works out where the text ends.
    # CLICK's editor saves a Copy of "AB" into TXT999 with its end at TXT1000.
    _exports_and_imports_as(lambda: copy("AB", txt.select(5, 6)), 'copy("AB",TXT5)')


# --- A constant into a Word -------------------------------------------------------------


@pytest.mark.parametrize(
    "write, token",
    [
        (lambda: copy(-1, dh[1]), "copy(FFFFh,DH1)"),
        (lambda: copy(65535, yd[1]), "copy(FFFFh,YD1)"),
        (lambda: fill(-32768, dh.select(1, 4)), "fill(8000h,DH1..DH4)"),
    ],
    ids=["minus-one-into-DH", "65535-into-YD", "fill-minus-32768"],
)
def test_a_constant_that_fits_in_16_bits_exports_into_a_word(write, token):
    # Sam's ruling (2026-11-17): a Copy or Fill constant into DH, XD or YD follows the hex
    # Math's rule.  -32768..-1 is written as its 16 bits.
    _exports_as(write, token)


@pytest.mark.parametrize(
    "write, constant, allow",
    [
        (lambda: copy(70000, dh[1]), 70000, ()),
        (lambda: copy(65536, yd[1]), 65536, ()),
        (lambda: copy(-32769, dh[1]), -32769, ()),
        (lambda: copy(70000.5, dh[1]), 70000.5, ()),
        (lambda: fill(70000, dh.select(1, 1)), 70000, ()),
        (lambda: fill(70000, yd.select(1, 2)), 70000, ()),
    ],
    ids=[
        "copy-70000-into-DH",
        "copy-65536-into-YD",
        "copy-minus-32769-into-DH",
        "copy-70000.5-into-DH",
        "fill-one-DH",
        "fill-YD",
    ],
)
def test_a_constant_past_16_bits_into_a_word_is_refused_even_when_allowed(write, constant, allow):
    # Sam's ruling (2026-11-17), as for the hex Math: export once wrote 70000 as its low 16
    # bits (1170h), a different number from the one the program reads.
    assert CLK_COPY_WIDE_CONSTANT in _codes(write)
    with pytest.raises(LadderExportError, match=f"Constant {constant} does not fit in 16 bits"):
        pyrung_to_ladder(_program(write), TagMap(), allow=(CLK_COPY_WIDE_CONSTANT, *allow))


def test_a_constant_past_16_bits_into_a_word_mapped_to_dh_is_refused():
    setpoint = Word("Setpoint")
    prog = Program(strict=False)
    with prog:
        with rung(x[1]):
            copy(70000, setpoint)
    tag_map = TagMap([setpoint.map_to(dh[5])])
    report = validate_click_program(prog, tag_map, mode="strict")
    assert CLK_COPY_WIDE_CONSTANT in {f.code for f in report.errors}
    with pytest.raises(LadderExportError, match="Constant 70000 does not fit in 16 bits"):
        pyrung_to_ladder(prog, tag_map, allow=(CLK_COPY_WIDE_CONSTANT,))


@pytest.mark.parametrize("value", [1.5, -1.5])
@pytest.mark.parametrize("bank", [dh, yd], ids=lambda bank: bank.name)
def test_a_fraction_copied_into_a_word_exports_as_written(bank, value):
    # A CLICK accepts copy(1.5, DH1) and copy(-1.5, YD1), and stores 1 and FFFFh, as pyrung
    # does.  A single-register Fill exports as the same Copy.
    write = lambda: copy(value, bank[1])  # noqa: E731
    token = f"copy({value},{bank.name}1)"
    # The importer does not read a YD operand, so only the DH Copy makes the round trip.
    (_exports_and_imports_as if bank is dh else _exports_as)(write, token)
    _exports_as(lambda: fill(value, bank.select(1, 1)), token)


@pytest.mark.parametrize("value", [1.5, -1.5, 70000.5])
def test_a_fraction_filled_into_a_range_of_words_is_refused_even_when_allowed(value):
    write = lambda: fill(value, dh.select(1, 2))  # noqa: E731
    assert CLK_COPY_WIDE_CONSTANT in _codes(write)
    with pytest.raises(LadderExportError, match=f"Constant {value} is not an integer"):
        pyrung_to_ladder(_program(write), TagMap(), allow=(CLK_COPY_WIDE_CONSTANT,))


# --- CLICK's Copy answers ---------------------------------------------------------------


@pytest.mark.parametrize(
    "write, token",
    [
        (lambda: copy(ctd[1], df[1]), "copy(CTD1,DF1)"),
        (lambda: copy(df[1], ctd[1]), "copy(DF1,CTD1)"),
        (lambda: copy(1.5, dd[1]), "copy(1.5,DD1)"),
        (lambda: copy(1.5, ctd[1]), "copy(1.5,CTD1)"),
        (lambda: copy(32768, df[1]), "copy(32768,DF1)"),
        (lambda: copy(65536, df[1]), "copy(65536,DF1)"),
        (lambda: copy(df[ds[1]], dd[1]), "copy(DF[DS1],DD1)"),
        (lambda: copy(dd[ds[1]], df[1]), "copy(DD[DS1],DF1)"),
        (lambda: copy(ctd[ds[1]], df[1]), "copy(CTD[DS1],DF1)"),
        (lambda: copy("é", txt[1]), 'copy("é",TXT1)'),
        (lambda: copy("\t", txt[1]), 'copy("\t",TXT1)'),
    ],
    ids=[
        "CTD-to-DF",
        "DF-to-CTD",
        "real-into-DD",
        "real-into-CTD",
        "32768-into-DF",
        "65536-into-DF",
        "DF-pointer",
        "DD-pointer",
        "CTD-pointer",
        "e-acute-into-TXT",
        "tab-into-TXT",
    ],
)
def test_a_copy_click_accepts_exports_and_imports(write, token):
    # RESULTS6.md rows 1c, 2 and 7, and copy-encodings RESULTS.md: CLICK accepts each of
    # these Copies.
    _exports_and_imports_as(write, token)
