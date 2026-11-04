"""Char against Char comparisons export to CLICK ladder and import back."""

from __future__ import annotations

import pytest

from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder, validate_click_program
from pyrung.click.ladder import LadderExportError
from pyrung.click.validation import CLK_TEXT_ORDER_COMPARE
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


def _program(op, right):
    a = Char("A")
    hit = Bool("Hit")
    with Program(strict=False) as prog:
        with rung(_OPS[op](a, right)):
            out(hit)
    return prog


@pytest.mark.parametrize("op", ["==", "!="])
@pytest.mark.parametrize("literal", [False, True])
def test_char_equality_exports_as_a_contact(op, literal):
    b = Char("B")
    prog = _program(op, "T" if literal else b)
    mapping = TagMap({Char("A"): txt[1], b: txt[2], Bool("Hit"): c[1]}, include_system=False)
    bundle = pyrung_to_ladder(prog, mapping)
    assert bundle.main_rows[1][1] == (f'TXT1{op}"T"' if literal else f"TXT1{op}TXT2")


@pytest.mark.parametrize("op", ["<", "<=", ">", ">="])
@pytest.mark.parametrize("literal", [False, True])
def test_char_order_compare_is_a_finding_and_refused_on_export(op, literal):
    a, b, hit = Char("A"), Char("B"), Bool("Hit")
    with Program(strict=False) as prog:
        with rung(_OPS[op](a, "T" if literal else b)):
            out(hit)
    mapping = TagMap({a: txt[1], b: txt[2], hit: c[1]}, include_system=False)

    report = validate_click_program(prog, mapping, mode="strict")
    found = [f for f in report.errors if f.code == CLK_TEXT_ORDER_COMPARE]
    assert len(found) == 1
    assert found[0].message.isascii() and found[0].suggestion.isascii()
    assert f"'{op}'" in found[0].message

    with pytest.raises(LadderExportError, match=CLK_TEXT_ORDER_COMPARE):
        pyrung_to_ladder(prog, mapping)
    assert pyrung_to_ladder(prog, mapping, allow=(CLK_TEXT_ORDER_COMPARE,)).main_rows


def test_numeric_order_compare_is_not_flagged():
    from pyrung.core import Int

    n, hit = Int("N"), Bool("Hit")
    with Program(strict=False) as prog:
        with rung(n > 3):
            out(hit)
    report = validate_click_program(
        prog, TagMap({n: ds[1], hit: c[1]}, include_system=False), mode="strict"
    )
    assert CLK_TEXT_ORDER_COMPARE not in {
        f.code for f in (*report.errors, *report.warnings, *report.hints)
    }
