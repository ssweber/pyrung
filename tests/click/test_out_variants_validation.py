"""CLICK validation of toggle(), debounce(), flasher() and timed(), which laddercodec's OutVariant checks.

These tests cover how a refusal prints: the call, the caret under the refused argument, and
the hint. A debounce and a flasher write each side as ``on=(timer, preset)``, so the caret
marks the timer or the preset inside the tuple.
"""

from __future__ import annotations

import pytest
from laddercodec import Code

from pyrung import debounce, flasher, immediate, timed, toggle
from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder
from pyrung.click.ladder import LadderExportError
from pyrung.click.validation import CLK_BANK_UNRESOLVED, validate_click_program
from pyrung.core import Bool, Int, Timer
from pyrung.core.program import Program, rung

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

Timers = Timer.clone("Delays", count=4)


def _timer_mappings(acc_bank=td):
    return [
        Timers._blocks["Done"].map_to(t.select(1, 4)),
        Timers._blocks["Acc"].map_to(acc_bank.select(1, 4)),
    ]


def _program(write) -> Program:
    prog = Program(strict=False)
    with prog:
        with rung(x[1]):
            write()
    return prog


def _errors(write, mappings=None):
    tag_map = TagMap(_timer_mappings() if mappings is None else mappings, include_system=False)
    return validate_click_program(_program(write), tag_map, mode="strict").errors


def _marked(finding) -> str:
    """Return the text under the caret."""
    (frame,) = finding.display.frames
    _line, start, width = frame.caret
    return frame.lines[0][start : start + width]


def _line(finding) -> str:
    return finding.display.frames[0].lines[0]


def test_a_refused_target_marks_the_target():
    lamp = Bool("Lamp")
    (finding,) = _errors(
        lambda: flasher(lamp, on=(Timers[1], 5), off=(Timers[2], 5)),
        [*_timer_mappings(), lamp.map_to(x[3])],
    )
    assert finding.code == Code.BANK_NOT_ALLOWED
    assert _line(finding) == "flasher(Lamp, on=(Delays[1], 5), off=(Delays[2], 5))"
    assert _marked(finding) == "Lamp"
    assert "This field takes Y, C, SC." in finding.display.hint


def test_a_refused_accumulator_marks_its_timer_and_gives_the_address():
    mappings = [
        Timers._blocks["Done"].map_to(t.select(1, 4)),
        Timers._blocks["Acc"].map_to(ds.select(1, 4)),
    ]
    findings = _errors(lambda: debounce(y[1], on=(Timers[1], 500), off=(Timers[2], 200)), mappings)
    assert [f.code for f in findings] == [Code.BANK_NOT_ALLOWED] * 2
    on, off = findings
    assert _line(on) == "debounce(Y001, on=(Delays[1], 500), off=(Delays[2], 200))"
    assert _marked(on) == "Delays[1]"
    assert _marked(off) == "Delays[2]"
    label = on.display.frames[0].caret_label
    assert "DS1" in label
    assert "Delays" not in label


def test_a_refused_preset_marks_the_preset_in_its_tuple():
    preset = Int("OnTime")
    (finding,) = _errors(
        lambda: flasher(y[1], on=(Timers[1], preset), off=(Timers[2], 5)),
        [*_timer_mappings(), preset.map_to(sd[1])],
    )
    assert finding.code == Code.BANK_NOT_ALLOWED
    assert _line(finding) == "flasher(Y001, on=(Delays[1], OnTime), off=(Delays[2], 5))"
    assert _marked(finding) == "OnTime"


def test_a_constant_preset_that_the_dialog_refuses_marks_the_constant():
    (finding,) = _errors(lambda: flasher(y[1], on=(Timers[1], 5), off=(Timers[2], 0)))
    assert finding.code == Code.CONSTANT_OUT_OF_RANGE
    assert _line(finding) == "flasher(Y001, on=(Delays[1], 5), off=(Delays[2], 0))"
    assert _marked(finding) == "0"
    assert finding.suggestion == "Pass a DS tag as the preset. A register preset takes any value."
    assert "OBJ-005" in finding.message


def test_a_preset_above_the_range_marks_the_constant_without_register_advice():
    (finding,) = _errors(lambda: timed(y[1], Timers[1], 40_000))
    assert finding.code == Code.CONSTANT_OUT_OF_RANGE
    assert _line(finding) == "timed(Y001, Delays[1], 40000)"
    assert _marked(finding) == "40000"
    assert finding.suggestion is None


def test_a_unit_that_the_out_dialog_lacks_marks_the_unit_keyword():
    (finding,) = _errors(lambda: debounce(y[1], on=(Timers[1], 5), off=(Timers[2], 5), unit="min"))
    assert finding.code == Code.COIL_TIME_UNIT
    assert _line(finding) == "debounce(Y001, on=(Delays[1], 5), off=(Delays[2], 5), unit='minutes')"
    assert _marked(finding) == "'minutes'"
    assert finding.suggestion == "Use unit= one of ms/sec and scale the preset."


def test_a_timed_coil_takes_minutes_but_not_hours():
    assert _errors(lambda: timed(y[1], Timers[1], 5, "min")) == ()
    (finding,) = _errors(lambda: timed(y[1], Timers[1], 5, "hour", maintained=True))
    assert finding.code == Code.COIL_TIME_UNIT
    assert _line(finding) == "timed(Y001, Delays[1], 5, unit='hours', maintained=True)"
    assert finding.suggestion == "Use unit= one of ms/sec/min and scale the preset."


def test_the_timed_caret_marks_the_timer_and_the_preset():
    mappings = [
        Timers._blocks["Done"].map_to(t.select(1, 4)),
        Timers._blocks["Acc"].map_to(ds.select(1, 4)),
    ]
    (finding,) = _errors(lambda: timed(y[1], Timers[1], 500), mappings)
    assert _line(finding) == "timed(Y001, Delays[1], 500)"
    assert _marked(finding) == "Delays[1]"


def test_a_toggle_on_a_bank_that_a_coil_cannot_write_is_refused():
    (finding,) = _errors(lambda: toggle(x[1]), [])
    assert finding.code == Code.BANK_NOT_ALLOWED
    assert _line(finding) == "toggle(X001)"
    assert _marked(finding) == "X001"


def test_an_immediate_toggle_outside_y_is_refused():
    (finding,) = _errors(lambda: toggle(immediate(c[1])), [])
    assert finding.code == Code.IMMEDIATE_COIL_NOT_Y
    assert _marked(finding) != ""


@pytest.mark.parametrize(
    "write",
    [
        lambda: toggle(y[1]),
        lambda: toggle(immediate(y[1])),
        lambda: debounce(y[1], on=(Timers[1], 0), off=(Timers[2], 0)),
        lambda: debounce(c[1], on=(Timers[1], 500), off=(Timers[2], 200), unit="sec"),
        lambda: flasher(y[1], on=(Timers[1], 1), off=(Timers[2], 32767)),
        lambda: timed(y[1], Timers[1], 3, "min", maintained=True),
    ],
)
def test_an_out_variant_that_click_takes_passes(write):
    assert _errors(write) == ()


def test_a_register_preset_takes_any_value():
    preset = Int("OnTime")
    assert (
        _errors(
            lambda: flasher(y[1], on=(Timers[1], preset), off=(Timers[2], preset)),
            [*_timer_mappings(), preset.map_to(ds[1])],
        )
        == ()
    )


def test_each_unmapped_operand_names_its_argument():
    preset = Int("OnTime")
    lamp = Bool("Lamp")
    findings = _errors(
        lambda: debounce(lamp, on=(Timers[1], preset), off=(Timers[2], 5)),
        [Timers._blocks["Done"].map_to(t.select(1, 4))],
    )
    assert [f.code for f in findings] == [CLK_BANK_UNRESOLVED] * 4
    assert [f.message.split(" at ")[0] for f in findings] == [
        "pyrung cannot find one CLICK address for the target",
        "pyrung cannot find one CLICK address for the on timer",
        "pyrung cannot find one CLICK address for the on preset",
        "pyrung cannot find one CLICK address for the off timer",
    ]


def test_a_constant_preset_needs_no_address():
    assert _errors(lambda: timed(y[1], Timers[1], 500)) == ()


def test_the_export_refuses_an_out_variant_that_click_refuses():
    write = lambda: flasher(y[1], on=(Timers[1], 0), off=(Timers[2], 5))  # noqa: E731
    tag_map = TagMap(_timer_mappings(), include_system=False)
    with pytest.raises(LadderExportError, match=Code.CONSTANT_OUT_OF_RANGE):
        pyrung_to_ladder(_program(write), tag_map)


def test_allow_names_what_replaced_the_removed_code():
    write = lambda: debounce(y[1], on=(Timers[1], 5), off=(Timers[2], 5), unit="min")  # noqa: E731
    tag_map = TagMap(_timer_mappings(), include_system=False)
    with pytest.raises(
        ValueError, match="no longer reports CLK_OUT_UNIT_UNSUPPORTED. Pass coil-time-unit"
    ):
        pyrung_to_ladder(_program(write), tag_map, allow=("CLK_OUT_UNIT_UNSUPPORTED",))
    assert pyrung_to_ladder(_program(write), tag_map, allow=(Code.COIL_TIME_UNIT,)).main_rows


@pytest.mark.parametrize("code", ["CLK_TIMER_PRESET_TOO_LOW", "CLK_TIMER_PRESET_OVERFLOW"])
def test_allow_with_a_code_that_other_checks_still_report_lets_nothing_through(code):
    write = lambda: flasher(y[1], on=(Timers[1], 0), off=(Timers[2], 40_000))  # noqa: E731
    tag_map = TagMap(_timer_mappings(), include_system=False)
    with pytest.raises(LadderExportError, match=Code.CONSTANT_OUT_OF_RANGE):
        pyrung_to_ladder(_program(write), tag_map, allow=(code,))
    assert pyrung_to_ladder(_program(write), tag_map, allow=(Code.CONSTANT_OUT_OF_RANGE,)).main_rows


@pytest.mark.parametrize(
    "write",
    [
        lambda: debounce(y[1], on=(Timers[1], 5), off=(Timers[2], 5)),
        lambda: flasher(y[1], on=(Timers[1], 5), off=(Timers[2], 5)),
        lambda: timed(y[1], Timers[1], 5),
    ],
    ids=["debounce", "flasher", "timed"],
)
def test_a_timer_whose_done_and_acc_are_on_different_numbers_is_refused(write):
    mappings = [
        Timers[1].Done.map_to(t[1]),
        Timers[1].Acc.map_to(td[7]),
        Timers[2].Done.map_to(t[2]),
        Timers[2].Acc.map_to(td[2]),
    ]
    tag_map = TagMap(mappings, include_system=False)
    report = validate_click_program(_program(write), tag_map, mode="strict")
    (finding,) = [f for f in report.errors if f.code == "CLK_TIMER_PAIR_MISMATCH"]
    assert "T1 and TD7" in finding.message
    assert finding.suggestion == "Map Done and Acc to the same number, e.g. T5 with TD5."
    with pytest.raises(LadderExportError, match="CLK_TIMER_PAIR_MISMATCH"):
        pyrung_to_ladder(_program(write), tag_map)


_VARIANTS = [
    lambda: debounce(y[1], on=(Timers[1], 5), off=(Timers[2], 5)),
    lambda: flasher(y[1], on=(Timers[1], 5), off=(Timers[2], 5)),
    lambda: timed(y[1], Timers[1], 5),
]


def _strict_report(write, mappings):
    tag_map = TagMap(mappings, include_system=False)
    return validate_click_program(_program(write), tag_map, mode="strict"), tag_map


@pytest.mark.parametrize("write", _VARIANTS, ids=["debounce", "flasher", "timed"])
@pytest.mark.parametrize("done_bank", [c, ct, x], ids=["c", "ct", "x"])
def test_a_done_bit_in_a_wrong_bank_is_refused(write, done_bank):
    mappings = [
        Timers._blocks["Done"].map_to(done_bank.select(1, 4)),
        Timers._blocks["Acc"].map_to(td.select(1, 4)),
    ]
    report, tag_map = _strict_report(write, mappings)
    found = [f for f in report.errors if f.code == Code.BANK_NOT_ALLOWED]
    assert len(found) == (1 if write is _VARIANTS[2] else 2)
    assert all("cannot be the timer done bit" in f.message for f in found)
    with pytest.raises(LadderExportError, match="timer done bit"):
        pyrung_to_ladder(_program(write), tag_map)


@pytest.mark.parametrize("write", _VARIANTS, ids=["debounce", "flasher", "timed"])
def test_an_unmapped_done_bit_is_refused(write):
    mappings = [Timers._blocks["Acc"].map_to(td.select(1, 4))]
    report, tag_map = _strict_report(write, mappings)
    assert [f.code for f in report.errors].count(CLK_BANK_UNRESOLVED) >= 1
    with pytest.raises(LadderExportError, match=CLK_BANK_UNRESOLVED):
        pyrung_to_ladder(_program(write), tag_map)


def test_an_immediate_toggle_prints_its_immediate_call():
    (finding,) = _errors(lambda: toggle(immediate(c[1])), [])
    assert _line(finding) == "toggle(immediate(C1))"
