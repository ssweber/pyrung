"""CLICK spelling of hex and REAL constants on export, and reading them back.

CLICK reads digits in a hex context (hex-mode Math, a DH/XD/YD value or
compare) as hex and appends ``h`` when a rung is typed; a pasted rung gets no
such fix-up.  pyrung literals stay decimal, so pyrung ``255`` exports as
``FFh`` (unpadded: CLICK's Math normalizes ``000Ah`` to ``Ah``, and Copy
and Compare keep either spelling).  REAL constants are float32 in CLICK, keep at most 7
significant digits, and are written the way CLICK writes them (``3.402823E+38``).
"""

from __future__ import annotations

import math

import pytest

from pyrung.click import ClickBlocks, TagMap, ladder_to_pyrung, pyrung_to_ladder
from pyrung.click.ladder.translator import click_real_literal
from pyrung.click.ladder.types import LadderBundle, LadderExportError
from pyrung.core import Program, Rung
from pyrung.core.expression import lsh
from pyrung.core.program import calc, copy, fill, out, search
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
        with Rung():
            calc(dh[221] + 255, dh[222])
        with Rung():
            calc(dh[1] & 0x00FF, dh[2])
        with Rung():
            calc(lsh(dh[1], 4), dh[3])
        with Rung():
            copy(10, dh[4])
        with Rung():
            copy(-1, dh[5])
        with Rung():
            fill(10, dh.select(7, 9))
        with Rung(dh[1] == 0xABCD):
            out(c[1])
        with Rung(dh[1] != 0):
            out(c[2])
        with Rung():
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
        with Rung(ds[1] == 10):
            copy(10, ds[2])
        with Rung():
            calc(ds[1] + 10, ds[3])
    assert _tokens(logic) == [
        "DS1==10",
        "copy(10,DS2)",
        "math(DS1 + 10,DS3,mode=decimal)",
    ]


def test_hex_compare_outside_word_range_rejected():
    """pyrung's DH1 == -1 is never true; CLICK's FFFFh would be, so refuse."""
    with Program() as logic:
        with Rung(dh[1] == -1):
            out(c[1])
    with pytest.raises(LadderExportError, match="outside 0..65535"):
        pyrung_to_ladder(logic, TagMap())


def test_hex_constants_round_trip_with_pyrung_meaning():
    with Program() as logic:
        with Rung():
            calc(dh[221] + 10, dh[222])
        with Rung(dh[1] == 0xC100):
            copy(0xDD01, dh[2])
        with Rung():
            copy(-1, dh[3])
    code, before, after = round_trip_logic(logic, TagMap())
    assert after == before, code
    body = strip_pyrung_boilerplate(code)
    assert "calc(DH221 + 0xA, DH222)" in body
    assert "copy(0xFFFF, DH3)" in body


def test_native_padded_hex_compare_reads_back():
    """CLICK's own ``DH53==0000h`` (tumbler sm_IsCmdValid) imports and re-exports unpadded."""
    bundle = _bundle(("DH53==0000h", "out(C1051)"))
    ns: dict = {}
    exec_with_source(ladder_to_pyrung(bundle), ns)
    again = pyrung_to_ladder(ns["logic"], ns["mapping"])
    assert again.main_rows[1][1] == "DH53==0h"


@pytest.mark.parametrize("value", [0x0, 0xA, 0xC, 0xC1, 0xD1F, 0xDD01, 0xDF10, 0xCAFE, 0xFFFF])
def test_unpadded_hex_that_looks_like_an_address_round_trips(value: int):
    """``C1h`` / ``DD01h`` are constants, not the C1 or DD1 registers."""
    with Program() as logic:
        with Rung(dh[1] == value):
            copy(value, dh[2])
            calc(dh[1] + value, dh[3])
    code, before, after = round_trip_logic(logic, TagMap())
    assert after == before, code


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("hex", "calc(DH1 + 0x255, DH2)"), ("decimal", "calc(DH1 + 255, DH2)")],
)
def test_importer_reads_bare_digits_in_hex_math_as_hex(mode: str, expected: str):
    """CLICK reads ``DH1 + 255`` in hex-mode Math as 255h; only there."""
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
        # CLICK keeps 7 significant digits: a pasted 3.40282347E+38 reads back
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


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan, 1e39, -3.5e38])
def test_click_real_literal_rejects_unrepresentable(value: float):
    assert click_real_literal(value) is None


def test_real_constants_export_click_style():
    with Program() as logic:
        with Rung():
            copy(3.14, df[1])
        with Rung():
            copy(3e38, df[2])
        with Rung(df[1] > 1e-10):
            calc(df[1] * 2.5e20, df[3])
    assert _tokens(logic) == [
        "copy(3.14,DF1)",
        "copy(3.0E+38,DF2)",
        "DF1>1.0E-10",
        "math(DF1 * 2.5E+20,DF3,mode=decimal)",
    ]


@pytest.mark.parametrize("value", [math.inf, math.nan, 1e39])
def test_unrepresentable_real_rejected(value: float):
    # The DSL refuses a REAL literal past CLICK's range; the exporter's
    # spelling has no form for one either, as a backstop.
    with Program():
        with Rung():
            with pytest.raises(ValueError, match="outside the REAL range"):
                copy(value, df[1])
    assert click_real_literal(value) is None


def test_negative_operands_export_click_style():
    """What CLICK v4 takes, typed: ``DS1 + ( -5 )`` but not ``DS1 + -5``; a leading
    ``- DS1`` (its spelling of ``-DS1``) but not ``DS1 * ( - DS2 )``, so a negation
    inside a formula is written ``( 0 - DS2 )``."""
    with Program() as logic:
        with Rung():
            calc(ds[1] + -5, ds[2])
        with Rung():
            calc(df[1] * -2.5, df[2])
        with Rung():
            calc(ds[1] * -ds[2], ds[3])
        with Rung():
            calc(-5 + ds[1], ds[4])
        with Rung():
            calc(-ds[1], ds[5])
        with Rung():
            calc(-ds[1] * ds[2], ds[6])
        with Rung():
            calc(ds[1] - -(ds[2] + 1), ds[7])
        with Rung():
            calc(dh[1] + -dh[2], dh[3])
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
        with Rung():
            calc(ds[1] + -5, ds[2])
        with Rung():
            calc(df[1] * -2.5, df[2])
        with Rung():
            calc(ds[1] * -ds[2], ds[3])
        with Rung():
            calc(-ds[1], ds[5])
        with Rung():
            calc(-ds[1] * ds[2], ds[6])
        with Rung():
            calc(ds[1] - -(ds[2] + 1), ds[7])
    code, before, after = round_trip_logic(logic, TagMap())
    assert after == before, code


def test_real_exponent_round_trips():
    with Program() as logic:
        with Rung():
            copy(3e38, df[1])
        with Rung(df[1] > 1e-10):
            calc(df[1] * 2.5e20, df[3])
    code, before, after = round_trip_logic(logic, TagMap())
    assert after == before, code
    assert "copy(3.0E+38, DF1)" in strip_pyrung_boilerplate(code)
