"""CLICK validation of forloop(), which laddercodec's ForLoop checks.

These tests cover how a refusal prints: the call, the caret under the count, and the hint.
"""

from __future__ import annotations

import pytest
from laddercodec import Code

from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder
from pyrung.click.ladder import LadderExportError
from pyrung.click.validation import CLK_BANK_UNRESOLVED, validate_click_program
from pyrung.core import Tag, TagType, forloop
from pyrung.core.program import Program, out, rung

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()


def _program(count, oneshot=False) -> Program:
    prog = Program(strict=False)
    with prog:
        with rung(x[1]):
            with forloop(count, oneshot=oneshot):
                out(y[1])
    return prog


def _errors(count, mappings, oneshot=False):
    tag_map = TagMap(mappings, include_system=False)
    return validate_click_program(_program(count, oneshot), tag_map, mode="strict").errors


def _count_in_df():
    count = Tag("Count", TagType.REAL)
    return count, [count.map_to(df[1])]


@pytest.mark.parametrize(
    "oneshot, line", [(False, "forloop(Count)"), (True, "forloop(Count, oneshot=True)")]
)
def test_a_refusal_marks_the_count(oneshot, line):
    (finding,) = _errors(*_count_in_df(), oneshot=oneshot)
    assert finding.code == Code.FOR_COUNT_BANK
    (frame,) = finding.display.frames
    assert (frame.location, frame.lines, frame.caret) == ("Main:R1", (line,), (0, 8, 5))
    # laddercodec's short text labels the caret, so no problem line repeats it.
    assert finding.display.problem == ""
    assert finding.location == "main.rung[1].instruction[0](ForLoopInstruction).instruction"


def test_the_label_gives_the_address_and_the_hint_gives_the_fix():
    (finding,) = _errors(*_count_in_df())
    label = finding.display.frames[0].caret_label
    assert "DF1" in label
    assert "Count" not in label
    assert finding.suggestion == "Copy the count into a DS or DD tag and loop on that."
    assert finding.suggestion in finding.display.hint
    assert "OBJ-004" in finding.display.hint
    assert finding.message.isascii()


@pytest.mark.parametrize("address", [ds[1], dd[1], td[1], ctd[1], sd[1]], ids=lambda tag: tag.name)
def test_a_count_in_a_bank_that_click_takes_passes(address):
    assert _errors(address, []) == ()


def test_an_unmapped_count_names_the_count():
    (finding,) = _errors(Tag("Count", TagType.INT), [])
    assert finding.code == CLK_BANK_UNRESOLVED
    assert "for the count at" in finding.message


def test_a_pointer_count_has_one_finding_with_pyrungs_advice():
    # pyrung's CLK_PTR_CONTEXT_ONLY_COPY reports the same fault, so the refusal takes its
    # advice and pyrung's finding goes.
    pointer = Tag("P", TagType.INT)
    (finding,) = _errors(df[pointer], [pointer.map_to(ds[100])])
    assert finding.code == Code.POINTER_OUTSIDE_COPY
    assert "can only be used inside copy()" in (finding.suggestion or "")


def test_the_export_refuses_a_count_that_click_refuses():
    count, mappings = _count_in_df()
    with pytest.raises(LadderExportError, match=Code.FOR_COUNT_BANK):
        pyrung_to_ladder(_program(count), TagMap(mappings, include_system=False))


def test_allow_names_what_replaced_the_removed_code():
    with pytest.raises(
        ValueError, match="no longer reports CLK_FORLOOP_COUNT_BANK. Pass for-count"
    ):
        pyrung_to_ladder(_program(5), TagMap(), allow=("CLK_FORLOOP_COUNT_BANK",))
