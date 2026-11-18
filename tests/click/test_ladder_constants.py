"""Check that the export spells hex and REAL constants as CLICK does, and the import reads them.

When you type a rung, CLICK reads digits in a hex context as hex and appends ``h``. A hex
context is hex-mode Math, or a DH/XD/YD value or compare. A pasted rung gets no such fix-up.
pyrung literals stay decimal, so pyrung ``255`` exports as ``FFh``. The export is unpadded
because CLICK's Math normalizes ``000Ah`` to ``Ah``, and Copy and Compare keep either spelling.

REAL constants are float32 in CLICK and keep at most 7 significant digits. The export writes
them the way CLICK does (``3.402823E+38``).

A blank text constant is ``$00``, CLICK's ASCII-code constant for NUL (a TXT blank). A quoted
``""`` writes no TXT at all, and CLICK ends a quoted constant at NUL.
"""

from __future__ import annotations

import math as pymath

import pytest

from pyrung import PLC
from pyrung.click import ClickBlocks, TagMap, ladder_to_pyrung, pyrung_to_ladder
from pyrung.click.ladder.translator import click_real_literal
from pyrung.click.ladder.types import LadderBundle, LadderExportError
from pyrung.core import Program, rung
from pyrung.core.expression import lsh
from pyrung.core.program import copy, fill, math, out, search
from tests.click.helpers import exec_with_source, round_trip_logic, strip_pyrung_boilerplate

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

_WIDTH = 33


def _tokens(logic: Program) -> list[str]:
    bundle = pyrung_to_ladder(logic, TagMap())
    return [
        cell for row in bundle.main_rows[1:-1] for cell in row[1:] if cell not in ("", "-", "T")
    ]


def _bundle(*rows: tuple[str, str]) -> LadderBundle:
    """One rung per (contact, af) pair."""
    header = ("marker", *[f"c{i}" for i in range(_WIDTH - 2)], "AF")
    body = []
    for contact, af in rows:
        row = ["R", contact or "-", *["-"] * (_WIDTH - 3), af]
        body.append(tuple(row))
    return LadderBundle(main_rows=(header, *body), subroutine_rows=())


# ---------------------------------------------------------------------------
# Hex
# ---------------------------------------------------------------------------


def test_hex_contexts_export_hex_constants():
    with Program() as logic:
        with rung():
            math(dh[221] + 255, dh[222])
        with rung():
            math(dh[1] & 0x00FF, dh[2])
        with rung():
            math(lsh(dh[1], 4), dh[3])
        with rung():
            copy(10, dh[4])
        with rung():
            copy(-1, dh[5])
        with rung():
            fill(10, dh.select(7, 9))
        with rung(dh[1] == 0xABCD):
            out(c[1])
        with rung(dh[1] != 0):
            out(c[2])
        with rung():
            search(dh.select(1, 3) == 10, result=ds[1], found=c[3])
    assert _tokens(logic) == [
        "math(DH221 + FFh,DH222,mode=hex)",
        "math(DH1 AND FFh,DH2,mode=hex)",
        "math(LSH ( DH1 , 4h ),DH3,mode=hex)",
        "copy(Ah,DH4)",
        "copy(FFFFh,DH5)",
        "fill(Ah,DH7..DH9)",
        "DH1==ABCDh",
        "out(C1)",
        "DH1!=0h",
        "out(C2)",
        "search(DH1..DH3 == Ah,result=DS1,found=C3)",
    ]


def test_decimal_contexts_stay_decimal():
    with Program() as logic:
        with rung(ds[1] == 10):
            copy(10, ds[2])
        with rung():
            math(ds[1] + 10, ds[3])
    assert _tokens(logic) == [
        "DS1==10",
        "copy(10,DS2)",
        "math(DS1 + 10,DS3,mode=decimal)",
    ]


def test_hex_compare_outside_word_range_rejected():
    """Refuse DH1 == -1. It is never true in pyrung, but CLICK's FFFFh would be."""
    with Program() as logic:
        with rung(dh[1] == -1):
            out(c[1])
    with pytest.raises(LadderExportError, match="outside 0..65535"):
        pyrung_to_ladder(logic, TagMap())


def test_hex_constants_round_trip_with_pyrung_meaning():
    with Program() as logic:
        with rung():
            math(dh[221] + 10, dh[222])
        with rung(dh[1] == 0xC100):
            copy(0xDD01, dh[2])
        with rung():
            copy(-1, dh[3])
    code, before, after = round_trip_logic(logic, TagMap())
    assert after == before, code
    body = strip_pyrung_boilerplate(code)
    assert "math(DH221 + 0xA, DH222)" in body
    assert "copy(0xFFFF, DH3)" in body


def test_native_padded_hex_compare_reads_back():
    """Import CLICK's own ``DH53==0000h`` (tumbler sm_IsCmdValid) and re-export it unpadded."""
    bundle = _bundle(("DH53==0000h", "out(C1051)"))
    ns: dict = {}
    exec_with_source(ladder_to_pyrung(bundle), ns)
    again = pyrung_to_ladder(ns["logic"], ns["mapping"])
    assert again.main_rows[1][1] == "DH53==0h"


@pytest.mark.parametrize("value", [0x0, 0xA, 0xC, 0xC1, 0xD1F, 0xDD01, 0xDF10, 0xCAFE, 0xFFFF])
def test_unpadded_hex_that_looks_like_an_address_round_trips(value: int):
    """Read ``C1h`` and ``DD01h`` as constants, not as the C1 or DD1 registers."""
    with Program() as logic:
        with rung(dh[1] == value):
            copy(value, dh[2])
            math(dh[1] + value, dh[3])
    code, before, after = round_trip_logic(logic, TagMap())
    assert after == before, code


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("hex", "math(DH1 + 0x255, DH2)"), ("decimal", "math(DH1 + 255, DH2)")],
)
def test_importer_reads_bare_digits_in_hex_math_as_hex(mode: str, expected: str):
    """CLICK reads ``DH1 + 255`` as 255h in hex-mode Math, and nowhere else."""
    bundle = _bundle(("", f"math(DH1 + 255,DH2,mode={mode})"))
    assert expected in strip_pyrung_boilerplate(ladder_to_pyrung(bundle))


# ---------------------------------------------------------------------------
# REAL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (3.14, "3.14"),
        (100.0, "100.0"),
        (0.1, "0.1"),
        (0.0, "0.0"),
        # CLICK keeps 7 significant digits. A pasted 3.40282347E+38 reads back as
        # 3.402823e+38, and the 8-digit 3.4028235E+38 fails v4's range check.
        (1 / 3, "0.3333333"),
        (3e38, "3.0E+38"),
        (3.4028234663852886e38, "3.402823E+38"),
        (-3.4028234663852886e38, "-3.402823E+38"),
        (16777215.0, "16777220.0"),
        (1e-10, "1.0E-10"),
        (-2.5e-7, "-2.5E-07"),
        (2.5e20, "2.5E+20"),
    ],
)
def test_click_real_literal(value: float, expected: str):
    assert click_real_literal(value) == expected


@pytest.mark.parametrize("value", [pymath.inf, -pymath.inf, pymath.nan, 1e39, -3.5e38])
def test_click_real_literal_rejects_unrepresentable(value: float):
    assert click_real_literal(value) is None


def test_real_constants_export_click_style():
    with Program() as logic:
        with rung():
            copy(3.14, df[1])
        with rung():
            copy(3e38, df[2])
        with rung(df[1] > 1e-10):
            math(df[1] * 2.5e20, df[3])
    assert _tokens(logic) == [
        "copy(3.14,DF1)",
        "copy(3.0E+38,DF2)",
        "DF1>1.0E-10",
        "math(DF1 * 2.5E+20,DF3,mode=decimal)",
    ]


@pytest.mark.parametrize("value", [pymath.inf, pymath.nan, 1e39])
def test_unrepresentable_real_rejected(value: float):
    # The DSL refuses a REAL literal past CLICK's range. As a backstop, the
    # exporter has no spelling for one either.
    with Program():
        with rung():
            with pytest.raises(ValueError, match="outside the REAL range|is NaN. A REAL constant"):
                copy(value, df[1])
    assert click_real_literal(value) is None


def test_negative_operands_export_click_style():
    """Check what CLICK v4 accepts when typed.

    It accepts ``DS1 + ( -5 )`` but not ``DS1 + -5``. It accepts a leading ``- DS1`` (its
    spelling of ``-DS1``) but not ``DS1 * ( - DS2 )``. So write a negation inside a formula
    as ``( 0 - DS2 )``.
    """
    with Program() as logic:
        with rung():
            math(ds[1] + -5, ds[2])
        with rung():
            math(df[1] * -2.5, df[2])
        with rung():
            math(ds[1] * -ds[2], ds[3])
        with rung():
            math(-5 + ds[1], ds[4])
        with rung():
            math(-ds[1], ds[5])
        with rung():
            math(-ds[1] * ds[2], ds[6])
        with rung():
            math(ds[1] - -(ds[2] + 1), ds[7])
        with rung():
            math(dh[1] + -dh[2], dh[3])
    assert _tokens(logic) == [
        "math(DS1 + ( -5 ),DS2,mode=decimal)",
        "math(DF1 * ( -2.5 ),DF2,mode=decimal)",
        "math(DS1 * ( 0 - DS2 ),DS3,mode=decimal)",
        "math(-5 + DS1,DS4,mode=decimal)",
        "math(- DS1,DS5,mode=decimal)",
        "math(( 0 - DS1 ) * DS2,DS6,mode=decimal)",
        "math(DS1 - ( 0 - ( DS2 + 1 ) ),DS7,mode=decimal)",
        "math(DH1 + ( 0h - DH2 ),DH3,mode=hex)",
    ]


def test_negative_operands_round_trip():
    with Program() as logic:
        with rung():
            math(ds[1] + -5, ds[2])
        with rung():
            math(df[1] * -2.5, df[2])
        with rung():
            math(ds[1] * -ds[2], ds[3])
        with rung():
            math(-ds[1], ds[5])
        with rung():
            math(-ds[1] * ds[2], ds[6])
        with rung():
            math(ds[1] - -(ds[2] + 1), ds[7])
    code, before, after = round_trip_logic(logic, TagMap())
    assert after == before, code


def test_real_exponent_round_trips():
    with Program() as logic:
        with rung():
            copy(3e38, df[1])
        with rung(df[1] > 1e-10):
            math(df[1] * 2.5e20, df[3])
    code, before, after = round_trip_logic(logic, TagMap())
    assert after == before, code
    assert "copy(3.0E+38, DF1)" in strip_pyrung_boilerplate(code)


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def test_blank_text_constants_export_as_ascii_code():
    """Check that the export writes a blank text constant as ``$00``.

    On CLICK, ``""`` writes nothing, and a raw NUL in quotes ends the constant.
    """
    with Program() as logic:
        with rung():
            fill("\x00", txt.select(1, 2))
        with rung():
            copy("", txt[3])
        with rung():
            copy("\x00", txt[4])
        with rung(txt[1] == "\x00"):
            out(c[1])
        with rung(txt[2] != ""):
            out(c[2])
        with rung():
            search(txt.select(1, 4) == "\x00", result=ds[1], found=c[3])
        with rung():
            copy("AB", txt.select(5, 6))
    tokens = _tokens(logic)
    assert tokens == [
        "fill($00,TXT1..TXT2)",
        "copy($00,TXT3)",
        "copy($00,TXT4)",
        "TXT1==$00",
        "out(C1)",
        "TXT2!=$00",
        "out(C2)",
        "search(TXT1..TXT4 == $00,result=DS1,found=C3)",
        'copy("AB",TXT5..TXT6)',
    ]
    assert not any("\x00" in token for token in tokens)


def test_text_constant_with_embedded_nul_rejected():
    with Program() as logic:
        with rung():
            copy("A\x00", txt.select(1, 2))
    with pytest.raises(LadderExportError, match="holds a NUL"):
        pyrung_to_ladder(logic, TagMap())


def test_blank_text_constants_import_as_nul():
    with Program() as logic:
        with rung():
            copy("A", txt[1])
            copy("B", txt[2])
            copy("C", txt[3])
        with rung():
            fill("\x00", txt.select(1, 2))
        with rung():
            copy("", txt[3])
        with rung(txt[1] == ""):
            out(c[1])
    code, before, after = round_trip_logic(logic, TagMap())
    assert after == before, code
    body = strip_pyrung_boilerplate(code)
    assert 'fill("", txt.select(1, 2))' in body
    assert r"\u0000" not in body

    ns: dict = {}
    exec_with_source(code, ns)
    plc = PLC(ns["logic"])
    plc.step()
    tags = plc.current_state.tags
    assert [tags["TXT1"], tags["TXT2"], tags["TXT3"]] == ["\x00"] * 3
    assert tags["C1"] is True


@pytest.mark.parametrize("blank", ["$00", '""'])
def test_csv_blank_spellings_import_as_blank(blank: str):
    """Read both ``$00`` and ``""`` as the blank.

    ``$00`` is the export's spelling, and older CSVs use ``""``.
    """
    bundle = _bundle(
        ("", 'copy("Q",TXT1)'),
        ("", f"copy({blank},TXT1)"),
        (f"TXT1=={blank}", "out(C1)"),
    )
    code = ladder_to_pyrung(bundle)
    assert 'copy("", TXT1)' in strip_pyrung_boilerplate(code)
    ns: dict = {}
    exec_with_source(code, ns)
    plc = PLC(ns["logic"])
    plc.step()
    assert plc.current_state.tags["TXT1"] == "\x00"
    assert plc.current_state.tags["C1"] is True
    again = pyrung_to_ladder(ns["logic"], ns["mapping"])
    assert again.main_rows[2][-1] == "copy($00,TXT1)"
    assert again.main_rows[3][1] == "TXT1==$00"


def test_csv_ascii_code_constant_imports_as_its_character():
    """CLICK's ``$0D`` copies one CR into a TXT."""
    bundle = _bundle(("", "copy($0D,TXT1)"), ("TXT1==$41", "out(C1)"))
    body = strip_pyrung_boilerplate(ladder_to_pyrung(bundle))
    assert r'copy("\r", TXT1)' in body
    assert 'TXT1 == "A"' in body
