"""CLK_TEXT_PAST_BANK: a Copy by address whose text always runs past TXT1000.
CLK_TEXT_PAST_BLOCK: one whose text runs past the end of a block mapped inside the
bank, where pyrung stops and a CLICK writes on.

pyrung writes what fits and drops the rest; the findings warn, and export renders
the store and warns rather than refusing it.
"""

from __future__ import annotations

import warnings

import pytest

from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder, validate_click_program
from pyrung.click.validation import CLK_TEXT_PAST_BANK, CLK_TEXT_PAST_BLOCK
from pyrung.core import Block, Program, Real, TagType, rung, to_text, to_value
from pyrung.core.program import copy

_blocks = ClickBlocks()
df, ds, txt = _blocks.df, _blocks.ds, _blocks.txt


def _report(store, mode="warn"):
    level = Real("Level")
    with Program(strict=False) as prog:
        with rung():
            store(level)
    mapping = TagMap({level: df[1]}, include_system=False)
    return prog, mapping, validate_click_program(prog, mapping, mode=mode)


def _found(report):
    return [
        f for f in (*report.errors, *report.warnings, *report.hints) if f.code == CLK_TEXT_PAST_BANK
    ]


_AT = "at main.rung[0].instruction[0](CopyInstruction).instruction.target"


@pytest.mark.parametrize(
    ("store", "message", "suggestion"),
    [
        pytest.param(
            lambda level: copy(level, txt[995], convert=to_text()),
            f"copy() into TXT995 {_AT} writes 9 characters or more for a finite value, "
            "past TXT1000 at the end of the bank: all but the first 6 are dropped.",
            "Start it at TXT992 or lower to keep the first 9.",
            id="real",
        ),
        pytest.param(
            lambda level: copy(level, txt[999]),
            f"copy() into TXT999 {_AT} writes 9 characters or more for a finite value, "
            "past TXT1000 at the end of the bank: all but the first 2 are dropped.",
            "Start it at TXT992 or lower to keep the first 9.",
            id="plain_real",
        ),
        pytest.param(
            lambda level: copy("ABCDEFG", txt[997]),
            f"copy() into TXT997 {_AT} writes 7 characters, "
            "past TXT1000 at the end of the bank: the last 3 are dropped.",
            "Start it at TXT994 or lower to keep every one.",
            id="literal",
        ),
        pytest.param(
            lambda level: copy(ds[1], txt[997], convert=to_text(suppress_zero=False)),
            f"copy() into TXT997 {_AT} writes 5 characters or more, "
            "past TXT1000 at the end of the bank: all but the first 4 are dropped.",
            "Start it at TXT996 or lower to keep the first 5.",
            id="fixed_int",
        ),
        pytest.param(
            lambda level: copy(level, txt[990], convert=to_text(exponential=True)),
            f"copy() into TXT990 {_AT} writes 13 characters or more for a finite value, "
            "past TXT1000 at the end of the bank: all but the first 11 are dropped.",
            "Start it at TXT988 or lower to keep the first 13.",
            id="exponential",
        ),
        pytest.param(
            lambda level: copy(12345, txt[997], convert=to_text()),
            f"copy() into TXT997 {_AT} writes 5 characters, "
            "past TXT1000 at the end of the bank: the last one is dropped.",
            "Start it at TXT996 or lower to keep every one.",
            id="literal_number",
        ),
        pytest.param(
            lambda level: copy("1234", ds[4499], convert=to_value),
            f"copy() into DS4499 {_AT} writes 4 elements, "
            "past DS4500 at the end of the bank: the last 2 are dropped.",
            "Start it at DS4497 or lower to keep every one.",
            id="digits",
        ),
    ],
)
def test_a_text_that_always_runs_past_the_bank_warns(store, message, suggestion):
    _prog, _mapping, report = _report(store)
    (finding,) = _found(report)
    assert finding.severity == "warning"
    assert finding.message.isascii() and (finding.suggestion or "").isascii()
    assert finding.message == message
    assert finding.suggestion == suggestion


@pytest.mark.parametrize(
    "store",
    [
        pytest.param(lambda level: copy(level, txt[992], convert=to_text()), id="real_fits"),
        pytest.param(lambda level: copy(ds[1], txt[999], convert=to_text()), id="suppressed"),
        pytest.param(lambda level: copy("ABCD", txt[997]), id="literal_fits"),
        pytest.param(lambda level: copy(level, txt[ds[2]], convert=to_text()), id="pointer"),
        pytest.param(lambda level: copy("A", txt[1000]), id="one_char"),
    ],
)
def test_a_text_that_can_fit_is_no_finding(store):
    _prog, _mapping, report = _report(store)
    assert _found(report) == []


def test_export_warns_and_renders_the_store():
    prog, mapping, report = _report(lambda level: copy(level, txt[995], convert=to_text()))
    assert [f.severity for f in _found(validate_click_program(prog, mapping, mode="strict"))] == [
        "error"
    ]
    with pytest.warns(UserWarning, match=CLK_TEXT_PAST_BANK):
        bundle = pyrung_to_ladder(prog, mapping)
    assert any("TXT995" in cell for row in bundle.main_rows for cell in row)


def test_export_of_a_text_that_fits_does_not_warn():
    prog, mapping, _report_ = _report(lambda level: copy(level, txt[992], convert=to_text()))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        pyrung_to_ladder(prog, mapping)


def _mapped_report(store):
    """*store* of Msg, a block mapped onto TXT101..110 in the middle of the bank."""
    msg = Block("Msg", TagType.CHAR, 1, 10)
    level = Real("Level")
    with Program(strict=False) as prog:
        with rung():
            store(msg, level)
    mapping = TagMap([msg.map_to(txt.select(101, 110)), level.map_to(df[1])], include_system=False)
    return prog, mapping, validate_click_program(prog, mapping)


def test_a_text_past_a_mapped_block_warns_that_a_click_writes_on():
    _prog, _mapping, report = _mapped_report(lambda msg, level: copy("ABC", msg[9]))
    (finding,) = [f for f in report.warnings if f.code == CLK_TEXT_PAST_BLOCK]
    assert finding.message == (
        f"copy() into Msg9 (TXT109) {_AT} writes 3 characters, past Msg10 at the end of "
        "its block Msg: pyrung drops the last one, where a CLICK writes on into TXT111."
    )
    assert finding.suggestion == "Start it at Msg8 or lower to keep every one."
    assert _found(report) == []


def test_a_real_past_a_mapped_block_names_what_pyrung_keeps():
    _prog, _mapping, report = _mapped_report(
        lambda msg, level: copy(level, msg[5], convert=to_text())
    )
    (finding,) = [f for f in report.warnings if f.code == CLK_TEXT_PAST_BLOCK]
    assert finding.message == (
        f"copy() into Msg5 (TXT105) {_AT} writes 9 characters or more for a finite value, "
        "past Msg10 at the end of its block Msg: pyrung drops all but the first 6, where a "
        "CLICK writes on into TXT111."
    )
    assert finding.suggestion == "Start it at Msg2 or lower to keep the first 9."


def test_a_text_that_fits_its_mapped_block_is_no_finding():
    _prog, _mapping, report = _mapped_report(lambda msg, level: copy("AB", msg[9]))
    assert [f for f in report.warnings if f.code == CLK_TEXT_PAST_BLOCK] == []


def test_export_warns_of_a_text_past_a_mapped_block():
    prog, mapping, _report_ = _mapped_report(lambda msg, level: copy("ABC", msg[9]))
    with pytest.warns(UserWarning, match=CLK_TEXT_PAST_BLOCK):
        pyrung_to_ladder(prog, mapping)
