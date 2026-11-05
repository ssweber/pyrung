"""CLK_TEXT_PAST_BANK: a Copy by address whose text always runs past TXT1000.

pyrung writes what fits and drops the rest; the finding warns, and export renders
the store and warns rather than refusing it.
"""

from __future__ import annotations

import warnings

import pytest

from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder, validate_click_program
from pyrung.click.validation import CLK_TEXT_PAST_BANK
from pyrung.core import Program, Real, rung, to_text
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
        f
        for f in (*report.errors, *report.warnings, *report.hints)
        if f.code == CLK_TEXT_PAST_BANK
    ]


@pytest.mark.parametrize(
    ("store", "message"),
    [
        pytest.param(
            lambda level: copy(level, txt[995], convert=to_text()),
            "copy() into TXT995 at main.rung[0].instruction[0](CopyInstruction).instruction.target "
            "writes 9 or more characters, past TXT1000 at the end of the bank: "
            "the last 3 are dropped.",
            id="real",
        ),
        pytest.param(
            lambda level: copy(level, txt[999]),
            None,
            id="plain_real",
        ),
        pytest.param(
            lambda level: copy("ABCDEFG", txt[997]),
            None,
            id="literal",
        ),
        pytest.param(
            lambda level: copy(ds[1], txt[997], convert=to_text(suppress_zero=False)),
            None,
            id="fixed_int",
        ),
        pytest.param(
            lambda level: copy(level, txt[990], convert=to_text(exponential=True)),
            None,
            id="exponential",
        ),
        pytest.param(
            lambda level: copy(12345, txt[997], convert=to_text()),
            None,
            id="literal_number",
        ),
    ],
)
def test_a_text_that_always_runs_past_the_bank_warns(store, message):
    _prog, _mapping, report = _report(store)
    (finding,) = _found(report)
    assert finding.severity == "warning"
    assert finding.message.isascii() and (finding.suggestion or "").isascii()
    if message is not None:
        assert finding.message == message
        assert finding.suggestion == "Start it at TXT992 or lower to keep every one."


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
