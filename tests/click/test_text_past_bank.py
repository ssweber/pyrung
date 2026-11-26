"""CLK_TEXT_PAST_BANK: a Copy by address whose text always runs past TXT1000.
CLK_TEXT_PAST_BLOCK: one whose text runs past the end of a block mapped inside the
bank, where pyrung stops and a CLICK writes on.

laddercodec's ``text-past-bank`` refuses a constant copied past TXT1000, and export
refuses it even when allowed. CLICK's Copy refuses it too (OBJ-003). CLICK's Copy accepts
a register source and saves no end address for it.  So any other store warns.

CLICK's answers are in clicknick ``buildbot/jobs/copy-encodings/RESULTS.md`` and
``buildbot/jobs/lc-diff-click/RESULTS8.md``.  CLICK counts a constant as it saves it,
not the text the Copy writes.  So 1.5 into TXT1 ends at TXT3.
"""

from __future__ import annotations

import warnings

import pytest
from laddercodec import Code

from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder
from pyrung.click.ladder import LadderExportError
from pyrung.click.validation import CLK_TEXT_PAST_BANK, CLK_TEXT_PAST_BLOCK, validate_click_program
from pyrung.core import Block, Program, Real, TagType, rung, to_text
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


def _refused(report):
    return [
        f
        for f in (*report.errors, *report.warnings, *report.hints)
        if f.code == Code.TEXT_PAST_BANK
    ]


_AT = "at main.rung[1].instruction[0](CopyInstruction).instruction.target"


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
    ("store", "call", "suggestion"),
    [
        pytest.param(
            lambda level: copy("ABCDEFG", txt[997]),
            "copy('ABCDEFG', TXT997)",
            "Start it at TXT994 or lower to keep every one.",
            id="literal",
        ),
        pytest.param(
            lambda level: copy(12345, txt[997], convert=to_text()),
            "copy(12345, TXT997, convert=to_text())",
            "Start it at TXT996 or lower to keep every one.",
            id="literal_number",
        ),
        pytest.param(
            lambda level: copy(1.5, txt[999], convert=to_text()),
            "copy(1.5, TXT999, convert=to_text())",
            "Start it at TXT992 or lower to keep every one.",
            id="literal_real",
        ),
    ],
)
def test_a_constant_that_runs_past_txt1000_is_refused_even_when_allowed(store, call, suggestion):
    prog, mapping, report = _report(store)
    assert _found(report) == []
    (finding,) = _refused(report)
    assert finding.message.startswith(f"CLICK refuses {call} at ")
    assert finding.severity == "warning"
    assert finding.suggestion == suggestion
    strict = validate_click_program(prog, mapping, mode="strict")
    assert [f.severity for f in _refused(strict)] == ["error"]
    # Validation refuses the constant first. Once its code is allowed, the render refuses it.
    refusals = (((), Code.TEXT_PAST_BANK), ((Code.TEXT_PAST_BANK,), r"runs past TXT1000\. CLICK"))
    for allow, refusal in refusals:
        with warnings.catch_warnings():
            # The refusal comes with no warning before it.
            warnings.simplefilter("error")
            with pytest.raises(LadderExportError, match=refusal):
                pyrung_to_ladder(prog, mapping, allow=allow)


@pytest.mark.parametrize(
    ("store", "start"),
    [
        pytest.param(lambda level: copy(1.5, txt[995], convert=to_text()), 995, id="real"),
        pytest.param(
            lambda level: copy(5, txt[997], convert=to_text(suppress_zero=False)),
            997,
            id="zeros",
        ),
        pytest.param(
            lambda level: copy(5, txt[1000], convert=to_text(termination_code=0)),
            1000,
            id="termination",
        ),
    ],
)
def test_a_constant_typed_within_txt1000_only_warns(store, start):
    # CLICK sets the end address from the constant as it saves it (1.5 into TXT1 ends at
    # TXT3), so these fit.  pyrung writes past TXT1000 and drops the rest.
    prog, mapping, report = _report(store)
    (finding,) = _found(report)
    assert finding.message.startswith(f"copy() into TXT{start} {_AT} writes ")
    assert "dropped." in finding.message
    with pytest.warns(UserWarning, match=CLK_TEXT_PAST_BANK):
        bundle = pyrung_to_ladder(prog, mapping)
    assert any(f"TXT{start}" in cell for row in bundle.main_rows for cell in row)


@pytest.mark.parametrize(
    ("store", "refused"),
    [
        # RESULTS9-10.md c10: CLICK saves a Copy's text as typed and counts each quote once,
        # so 'A"B' saves 3 characters and 'A""B' saves 4.
        pytest.param(lambda level: copy('A"B', txt[999]), True, id="quote-at-999"),
        pytest.param(lambda level: copy('A"B', txt[998]), False, id="quote-at-998"),
        pytest.param(lambda level: copy('A""B', txt[998]), True, id="two-quotes-at-998"),
        pytest.param(lambda level: copy('A""B', txt[997]), False, id="two-quotes-at-997"),
        pytest.param(
            lambda level: copy(123456789.0, txt[989], convert=to_text()), False, id="real-at-989"
        ),
        pytest.param(
            lambda level: copy(1e16, txt[996], convert=to_text()),
            False,
            id="e-form-at-996",
        ),
    ],
)
def test_a_constant_counts_as_click_saves_it(store, refused):
    # A constant that ends at TXT1000 fits (RESULTS8.md: "AB" into TXT999 ends there).
    prog, mapping, report = _report(store)
    assert bool(_refused(report)) is refused
    if refused:
        with pytest.raises(LadderExportError, match=r"runs past TXT1000\. CLICK's Copy"):
            pyrung_to_ladder(prog, mapping, allow=(Code.TEXT_PAST_BANK,))
    else:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            pyrung_to_ladder(prog, mapping)


def test_a_real_counts_as_click_saves_it():
    # RESULTS9-10.md: CLICK saves 123456789.0 as 1.234568E+08. Those 12 characters run past
    # TXT1000 from TXT990, and CLICK refuses the Copy. pyrung writes 123456800.0, 11
    # characters, so the refusal must count the saved form.
    prog, mapping, report = _report(
        lambda level: copy(123456789.0, txt[990], convert=to_text()), mode="strict"
    )
    assert [f.code for f in report.errors] == [Code.TEXT_PAST_BANK]
    with pytest.raises(LadderExportError, match=r"12-character constant, which runs past TXT1000"):
        pyrung_to_ladder(prog, mapping, allow=(Code.TEXT_PAST_BANK,))


def test_a_constant_past_txt1000_through_a_mapped_block_is_refused():
    msg = Block("M", TagType.CHAR, 1, 4)
    with Program(strict=False) as prog:
        with rung():
            copy("ABCDEFG", msg[1])
    mapping = TagMap([msg.map_to(txt.select(995, 998))], include_system=False)
    report = validate_click_program(prog, mapping)
    assert [f for f in report.warnings if f.code in (CLK_TEXT_PAST_BANK, CLK_TEXT_PAST_BLOCK)] == []
    (finding,) = _refused(report)
    assert finding.message.startswith("CLICK refuses copy('ABCDEFG', M1) at ")
    with pytest.raises(LadderExportError, match=r"runs past TXT1000\. CLICK's Copy"):
        pyrung_to_ladder(prog, mapping, allow=(Code.TEXT_PAST_BANK, CLK_TEXT_PAST_BLOCK))


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
    assert _refused(report) == []


def test_a_quote_on_its_own_fits_in_txt1000():
    # The export writes a quote on its own as $22, which is one character.
    prog, mapping, report = _report(lambda level: copy('"', txt[1000]), mode="strict")
    assert not report.errors
    bundle = pyrung_to_ladder(prog, mapping)
    assert any("copy($22,TXT1000)" in cell for row in bundle.main_rows for cell in row)


def test_export_warns_and_renders_the_store():
    prog, mapping, report = _report(lambda level: copy(level, txt[995], convert=to_text()))
    assert [f.severity for f in _found(validate_click_program(prog, mapping, mode="strict"))] == [
        "error"
    ]
    with pytest.warns(UserWarning, match=CLK_TEXT_PAST_BANK):
        bundle = pyrung_to_ladder(prog, mapping)
    assert any("TXT995" in cell for row in bundle.main_rows for cell in row)


def test_a_register_past_txt1000_warns_and_exports():
    # RESULTS8.md: CLICK's Copy accepts DS1 -> TXT997 with Do not suppress zero, and
    # saves no end address for it.
    prog, mapping, report = _report(
        lambda level: copy(ds[1], txt[997], convert=to_text(suppress_zero=False))
    )
    (finding,) = _found(report)
    assert finding.severity == "warning"
    with pytest.warns(UserWarning, match=CLK_TEXT_PAST_BANK):
        bundle = pyrung_to_ladder(prog, mapping)
    assert any("TXT997" in cell for row in bundle.main_rows for cell in row)


def test_export_of_a_text_that_fits_does_not_warn():
    prog, mapping, _report_ = _report(lambda level: copy(level, txt[992], convert=to_text()))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        pyrung_to_ladder(prog, mapping)


def _mapped_report(store):
    """Validate *store* of Msg, a block mapped onto TXT101..110 in the middle of the bank."""
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
