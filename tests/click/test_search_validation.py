"""CLICK validation of search(), which laddercodec's Search checks.

These tests cover how a refusal prints: the call, the caret under the refused argument, and
the hint. search() takes its result and found flag as keywords, so the caret also marks a
keyword's value.
"""

from __future__ import annotations

import pytest
from laddercodec import Code

from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder
from pyrung.click.ladder import LadderExportError
from pyrung.click.validation import CLK_BANK_UNRESOLVED, validate_click_program
from pyrung.core import Block, Bool, Int, Tag, TagType, search
from pyrung.core.program import Program, rung

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()


def _program(write) -> Program:
    prog = Program(strict=False)
    with prog:
        with rung(x[1]):
            write()
    return prog


def _errors(write, mappings=()):
    tag_map = TagMap(list(mappings), include_system=False)
    return validate_click_program(_program(write), tag_map, mode="strict").errors


def _frame(finding):
    (frame,) = finding.display.frames
    return frame.lines, frame.caret


def test_a_refused_table_marks_the_comparison():
    (finding,) = _errors(lambda: search(sd.select(1, 10) == 5, result=ds[20], found=c[1]))
    assert finding.code == Code.BANK_NOT_ALLOWED
    line = "search(SD1..SD10 == 5, result=DS20, found=C1)"
    assert _frame(finding) == ((line,), (0, 7, 14))
    assert finding.display.frames[0].caret_label == "SD1..SD10 is not allowed"
    assert finding.suggestion == (
        "Copy the values into a range in one of those banks and search that."
    )
    assert "This field takes DS, DD, DH, DF, TD, CTD, TXT." in finding.display.hint
    assert finding.location == "main.rung[1].instruction[0](SearchInstruction).instruction"


def test_a_refused_result_marks_the_result_keyword():
    result = Int("Slot")
    (finding,) = _errors(
        lambda: search(ds.select(1, 10) == 5, result=result, found=c[1]),
        [result.map_to(td[3])],
    )
    assert finding.code == Code.BANK_NOT_ALLOWED
    line = "search(DS1..DS10 == 5, result=Slot, found=C1)"
    assert _frame(finding) == ((line,), (0, 30, 4))
    # The label gives the address, not the tag's name.
    label = finding.display.frames[0].caret_label
    assert "TD3" in label
    assert "Slot" not in label
    assert finding.suggestion == (
        "Map the result to a DS address, and copy it on from there if needed."
    )
    assert finding.suggestion in finding.display.hint


def test_a_refused_found_flag_marks_the_found_keyword():
    (finding,) = _errors(lambda: search(ds.select(1, 10) == 5, result=ds[20], found=y[2]))
    assert finding.code == Code.BANK_NOT_ALLOWED
    line = "search(DS1..DS10 == 5, result=DS20, found=Y002)"
    assert _frame(finding) == ((line,), (0, 42, 4))
    assert finding.suggestion == "Map found to a C bit, and copy it on from there if needed."


def test_the_hint_gives_clicks_error():
    (finding,) = _errors(lambda: search(ds.select(1, 10) == dh[30], result=ds[20], found=c[1]))
    assert finding.code == Code.SEARCH_VALUE_BANK
    assert "OBJ-004" in finding.message
    assert finding.message.isascii()


def test_a_text_search_takes_only_equal_and_not_equal():
    (finding,) = _errors(lambda: search(txt.select(1, 10) > "A", result=ds[20], found=c[1]))
    assert finding.code == Code.TEXT_ORDER_COMPARE


@pytest.mark.parametrize(
    "write",
    [
        lambda: search(ds.select(1, 10) == 5, result=ds[20], found=c[1]),
        lambda: search(dd.select(1, 10) > 70000, result=ds[20], found=c[1], oneshot=True),
        lambda: search(dh.select(1, 10) == 5, result=ds[20], found=c[1]),
        lambda: search(df.select(1, 10) >= 2.5, result=ds[20], found=c[1]),
        lambda: search(td.select(1, 10) < ds[30], result=ds[20], found=c[1], continuous=True),
        lambda: search(ctd.select(1, 10) != 5, result=ds[20], found=c[1]),
        lambda: search(txt.select(1, 10) == "AB", result=ds[20], found=c[1]),
        lambda: search(txt.select(1, 10) != txt[30], result=ds[20], found=c[1]),
    ],
    ids=["DS", "DD", "DH", "DF", "TD", "CTD", "TXT", "TXT tag"],
)
def test_a_search_that_click_takes_passes(write):
    assert _errors(write) == ()


def test_each_unmapped_operand_names_its_argument():
    values = Block("Values", TagType.INT, 1, 10)
    target, result, found = Int("Target"), Int("Result"), Bool("Found")
    findings = _errors(lambda: search(values.select(1, 10) >= target, result=result, found=found))
    # The comparison holds the range and the value, and it gets one finding.
    assert [f.code for f in findings] == [CLK_BANK_UNRESOLVED] * 3
    assert [f.message.split(" at ")[0] for f in findings] == [
        "pyrung cannot find one CLICK address for the comparison",
        "pyrung cannot find one CLICK address for the result",
        "pyrung cannot find one CLICK address for the found flag",
    ]
    assert [f.location.rpartition(".")[2] for f in findings] == ["search_range", "result", "found"]


def test_an_unmapped_value_is_the_comparisons_finding():
    target = Int("Target")
    (finding,) = _errors(lambda: search(ds.select(1, 10) >= target, result=ds[20], found=c[1]))
    assert finding.code == CLK_BANK_UNRESOLVED
    assert finding.location.endswith("instruction.value")


def test_a_constant_value_needs_no_address():
    # A number in the comparison holds no address, so it gets no CLK_BANK_UNRESOLVED.
    assert _errors(lambda: search(ds.select(1, 10) == 5, result=ds[20], found=c[1])) == ()


def test_a_pointer_value_has_one_finding_with_pyrungs_advice():
    pointer = Tag("P", TagType.INT)
    (finding,) = _errors(
        lambda: search(ds.select(1, 10) == ds[pointer], result=ds[20], found=c[1]),
        [pointer.map_to(ds[100])],
    )
    assert finding.code == Code.POINTER_OUTSIDE_COPY
    assert "can only be used inside copy()" in (finding.suggestion or "")


def test_a_table_of_scattered_addresses_is_refused():
    gap = Block("Gap", TagType.INT, 1, 3)
    (finding,) = _errors(
        lambda: search(gap.select(1, 3) >= 1, result=ds[20], found=c[1]),
        [gap[1].map_to(ds[1]), gap[2].map_to(ds[2]), gap[3].map_to(ds[5])],
    )
    assert finding.code == "CLK_COPY_RANGE_NOT_CONSECUTIVE"


def test_the_export_refuses_a_search_that_click_refuses():
    with pytest.raises(LadderExportError, match=Code.BANK_NOT_ALLOWED):
        pyrung_to_ladder(
            _program(lambda: search(ds.select(1, 10) == 5, result=dd[1], found=c[1])), TagMap()
        )


@pytest.mark.parametrize("code", ["CLK_SEARCH_RANGE_BANK", "CLK_SEARCH_RESULT_NOT_DS"])
def test_allow_names_what_replaced_the_removed_code(code):
    write = lambda: search(ds.select(1, 10) == 5, result=ds[20], found=c[1])  # noqa: E731
    with pytest.raises(ValueError, match=f"no longer reports {code}. Pass bank-not-allowed"):
        pyrung_to_ladder(_program(write), TagMap(), allow=(code,))
