"""Char against Char comparisons export to CLICK ladder and import back."""

from __future__ import annotations

import pytest

from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder
from pyrung.core import Bool, Char, Program, rung
from pyrung.core.program import out

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

_OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


@pytest.mark.parametrize("op", list(_OPS))
def test_char_tags_compare_in_the_exported_ladder(op):
    a = Char("A")
    b = Char("B")
    hit = Bool("Hit")
    with Program(strict=False) as prog:
        with rung(_OPS[op](a, b)):
            out(hit)

    bundle = pyrung_to_ladder(prog, TagMap({a: txt[1], b: txt[2], hit: c[1]}, include_system=False))
    assert bundle.main_rows[1][1] == f"TXT1{op}TXT2"
