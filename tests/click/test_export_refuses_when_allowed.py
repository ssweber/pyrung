"""A fault that the export refuses even when its code is allowed is a warning in warn mode.

Most findings let the program through once the caller passes the code to ``allow=``. These
faults have no CLICK form to write, so the export refuses them either way. Warn mode must not
hide them as hints.
"""

from __future__ import annotations

import pytest

from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder
from pyrung.click.ladder import LadderExportError
from pyrung.click.validation import validate_click_program
from pyrung.core import Block, Int, Program, TagType, rung
from pyrung.core.expression import LiteralExpr
from pyrung.core.program import blockcopy, copy, fill, math, out

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()


def _fill_quote():
    with Program() as logic:
        with rung():
            fill('a"b', txt.select(1, 4))
    return logic, TagMap()


def _fill_one_register_quote():
    with Program() as logic:
        with rung():
            fill('a"b', txt.select(1, 1))
    return logic, TagMap()


def _fill_one_register_text():
    with Program() as logic:
        with rung():
            fill("ab", txt.select(1, 1))
    return logic, TagMap()


def _two_texts_read_two_ways():
    with Program(strict=False) as logic:
        with rung(LiteralExpr('A"=="B') == LiteralExpr("C")):
            out(c[1])
    return logic, TagMap()


def _copy_range_gap():
    with Program() as logic:
        with rung():
            blockcopy(y.select(1, 21), c.select(1, 17))
    return logic, TagMap()


def _math_range_gap():
    values = Block("V", TagType.INT, 1, 3)
    result = Int("Result")
    with Program() as logic:
        with rung():
            math(values.select(1, 3).max(), result)
    mapping = TagMap(
        {values[1]: ds[1], values[2]: ds[2], values[3]: ds[5], result: ds[10]},
        include_system=False,
    )
    return logic, mapping


def _wide_constant_into_a_hex_register():
    with Program() as logic:
        with rung():
            copy(70000, dh[1])
    return logic, TagMap()


def _text_past_the_bank():
    with Program() as logic:
        with rung():
            copy("a" * 10, txt[999])
    return logic, TagMap()


_FORMS = {
    "fill text with a quote": _fill_quote,
    "fill of one register with a quote": _fill_one_register_quote,
    "fill of one register with two characters": _fill_one_register_text,
    "compare of two texts that reads two ways": _two_texts_read_two_ways,
    "block copy range with a gap": _copy_range_gap,
    "math range with a gap": _math_range_gap,
    "wide constant into a hex register": _wide_constant_into_a_hex_register,
    "copy text past TXT1000": _text_past_the_bank,
}


def _codes(report):
    return {finding.code for finding in (*report.errors, *report.warnings, *report.hints)}


@pytest.mark.parametrize("form", _FORMS)
def test_the_export_refuses_the_fault_when_every_code_is_allowed(form):
    logic, mapping = _FORMS[form]()
    codes = _codes(validate_click_program(logic, mapping, mode="strict"))
    assert codes
    with pytest.raises(LadderExportError):
        pyrung_to_ladder(logic, mapping, allow=sorted(codes))


@pytest.mark.parametrize("form", _FORMS)
def test_warn_mode_reports_the_fault_as_a_warning(form):
    logic, mapping = _FORMS[form]()
    report = validate_click_program(logic, mapping, mode="warn")
    assert report.warnings
    # The inline expression of the two-text compare is a finding that allow= lets through.
    assert [h.code for h in report.hints if h.code != "CLK_EXPR_ONLY_IN_MATH"] == []


def test_a_compare_of_two_texts_with_one_reading_is_only_an_inline_expression():
    # pyrung builds a compare of two texts only from expressions. Validation and the export
    # refuse that as an expression in a condition, and allowing it writes the compare.
    with Program(strict=False) as logic:
        with rung(LiteralExpr("A") == LiteralExpr("B")):
            out(c[1])
    report = validate_click_program(logic, TagMap(), mode="strict")
    assert {finding.code for finding in report.errors} == {"CLK_EXPR_ONLY_IN_MATH"}
    with pytest.raises(LadderExportError, match="CLK_EXPR_ONLY_IN_MATH"):
        pyrung_to_ladder(logic, TagMap())
    bundle = pyrung_to_ladder(logic, TagMap(), allow=["CLK_EXPR_ONLY_IN_MATH"])
    assert '"A"=="B"' in [cell for row in bundle.main_rows for cell in row]


def test_a_compare_of_two_texts_that_reads_two_ways_has_a_coded_finding_and_refusal():
    logic, mapping = _two_texts_read_two_ways()
    report = validate_click_program(logic, mapping, mode="strict")
    assert "invalid-text-constant" in {finding.code for finding in report.errors}
    with pytest.raises(LadderExportError, match=r"\[invalid-text-constant\]"):
        pyrung_to_ladder(logic, mapping, allow=["CLK_EXPR_ONLY_IN_MATH"])
    with pytest.raises(LadderExportError, match="invalid-text-constant: "):
        pyrung_to_ladder(logic, mapping, allow=["CLK_EXPR_ONLY_IN_MATH", "invalid-text-constant"])
