"""CLICK validation of on_delay() and off_delay(), which laddercodec's Timer checks.

These tests cover how a refusal prints: the call, the caret under the refused argument, and
the hint. The timer's UDT is one argument that holds the Done bit and the Acc. The unit is an
option of the call, and `.reset()` is a chained call.
"""

from __future__ import annotations

import pytest
from laddercodec import Code

from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder
from pyrung.click.ladder import LadderExportError
from pyrung.click.validation import CLK_BANK_UNRESOLVED, validate_click_program
from pyrung.core import Bool, Int, Timer
from pyrung.core.program import Program, off_delay, on_delay, rung

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


def _timer(done=t[1], acc=td[1]):
    timer = Timer.clone("Tm")
    return timer, [timer.Done.map_to(done), timer.Acc.map_to(acc)]


def test_a_refused_done_bit_marks_the_timer():
    timer, mappings = _timer(done=c[1])
    (finding,) = _errors(lambda: on_delay(timer, preset=5), mappings)
    assert finding.code == Code.BANK_NOT_ALLOWED
    assert _frame(finding) == (("on_delay(Tm, preset=5)",), (0, 9, 2))
    assert finding.display.frames[0].caret_label == "C1 is not allowed"
    assert "This field takes T." in finding.display.hint
    assert finding.location == "main.rung[1].instruction[0](OnDelayInstruction).instruction"
    assert finding.message.isascii()


def test_a_refused_off_delay_accumulator_marks_the_timer():
    timer, mappings = _timer(acc=ds[1])
    (finding,) = _errors(lambda: off_delay(timer, preset=5), mappings)
    assert finding.code == Code.BANK_NOT_ALLOWED
    assert _frame(finding) == (("off_delay(Tm, preset=5)",), (0, 10, 2))
    assert finding.display.frames[0].caret_label == "DS1 is not allowed"


def test_the_label_gives_the_address_and_not_the_tag_name():
    preset = Int("Delay")
    timer, mappings = _timer()
    (finding,) = _errors(lambda: on_delay(timer, preset=preset), [*mappings, preset.map_to(td[9])])
    assert finding.code == Code.BANK_NOT_ALLOWED
    assert _frame(finding) == (("on_delay(Tm, preset=Delay)",), (0, 20, 5))
    label = finding.display.frames[0].caret_label
    assert "TD9" in label
    assert "Delay" not in label
    assert "OBJ-004" in finding.display.hint


@pytest.mark.parametrize("call", [on_delay, off_delay])
def test_a_preset_above_32767_marks_the_preset_and_advises_a_larger_unit(call):
    timer, mappings = _timer()
    (finding,) = _errors(lambda: call(timer, preset=40000), mappings)
    assert finding.code == Code.CONSTANT_OUT_OF_RANGE
    assert finding.suggestion == "Use a larger time unit to keep the preset within 32767."
    assert finding.suggestion in finding.display.hint
    # laddercodec gives no detail for this refusal, so its text is the problem line.
    assert "OBJ-005" in finding.display.problem
    lines, caret = _frame(finding)
    assert lines[0].endswith("preset=40000)")
    assert caret[2] == 5


def test_the_call_shows_a_unit_that_is_not_the_default():
    timer, mappings = _timer(done=c[1])
    (finding,) = _errors(lambda: on_delay(timer, preset=5, unit="s"), mappings)
    assert _frame(finding) == (("on_delay(Tm, preset=5, unit='seconds')",), (0, 9, 2))


def test_the_call_shows_the_reset_and_a_refusal_marks_the_timer():
    timer, mappings = _timer(done=c[1])
    (finding,) = _errors(lambda: on_delay(timer, preset=5).reset(x[2]), mappings)
    assert _frame(finding) == (("on_delay(Tm, preset=5).reset(X002)",), (0, 9, 2))


@pytest.mark.parametrize("call", [on_delay, off_delay])
def test_a_negative_constant_preset_is_refused(call):
    # CLICK has one Timer dialog for both delays. It takes digits only, and laddercodec's
    # text calls the preset an address.
    timer, mappings = _timer()
    (finding,) = _errors(lambda: call(timer, preset=-1), mappings)
    assert finding.code == Code.NOT_AN_ADDRESS
    lines, caret = _frame(finding)
    assert lines == (f"{call.__name__}(Tm, preset=-1)",)
    assert caret == (0, lines[0].index("preset=") + 7, 2)


def test_a_done_and_acc_with_different_numbers_are_refused():
    timer, mappings = _timer(done=t[1], acc=td[2])
    (finding,) = _errors(lambda: on_delay(timer, preset=5), mappings)
    assert finding.code == Code.TIMER_PAIR_MISMATCH
    assert _frame(finding) == (("on_delay(Tm, preset=5)",), (0, 9, 2))
    assert finding.suggestion == "Map Done and Acc to the same number, e.g. T5 with TD5."


@pytest.mark.parametrize("call", [on_delay, off_delay])
@pytest.mark.parametrize("unit", ["ms", "s", "min", "hour", "day"])
def test_a_form_that_click_takes_has_no_findings(call, unit):
    timer, mappings = _timer(done=t[500], acc=td[500])
    preset = Int("Preset")
    assert (
        _errors(lambda: call(timer, preset=preset, unit=unit), [*mappings, preset.map_to(ds[3])])
        == ()
    )
    timer, mappings = _timer()
    assert _errors(lambda: call(timer, preset=32767, unit=unit), mappings) == ()


def test_a_retentive_on_delay_that_click_takes_has_no_findings():
    timer, mappings = _timer()
    assert _errors(lambda: on_delay(timer, preset=0).reset(x[2]), mappings) == ()


def test_an_unmapped_timer_names_the_timer():
    (finding,) = _errors(lambda: on_delay(Timer.clone("Tm"), preset=5))
    assert finding.code == CLK_BANK_UNRESOLVED
    assert "for the timer at" in finding.message


def test_an_unmapped_preset_names_the_preset():
    timer, mappings = _timer()
    (finding,) = _errors(lambda: on_delay(timer, preset=Int("Delay")), mappings)
    assert finding.code == CLK_BANK_UNRESOLVED
    assert "for the preset at" in finding.message


def test_an_unmapped_reset_names_the_reset_condition():
    timer, mappings = _timer()
    (finding,) = _errors(lambda: on_delay(timer, preset=5).reset(Bool("Clear")), mappings)
    assert finding.code == CLK_BANK_UNRESOLVED
    assert "for the reset condition at" in finding.message


def test_the_export_refuses_a_form_that_click_refuses():
    timer, mappings = _timer(done=c[1])
    with pytest.raises(LadderExportError, match=Code.BANK_NOT_ALLOWED):
        pyrung_to_ladder(_program(lambda: on_delay(timer, preset=5)), TagMap(mappings))


@pytest.mark.parametrize(
    ("old", "new", "mapping", "preset"),
    [
        ("CLK_BANK_WRONG_ROLE", Code.BANK_NOT_ALLOWED, {"done": c[1]}, 5),
        ("CLK_TIMER_PAIR_MISMATCH", Code.TIMER_PAIR_MISMATCH, {"acc": td[2]}, 5),
    ],
)
def test_the_old_code_no_longer_lets_the_finding_through(old, new, mapping, preset):
    # Other checks still report these codes, so allow= accepts them. The old code no longer
    # allows laddercodec's finding, and the new code does.
    timer, mappings = _timer(**mapping)
    program = _program(lambda: on_delay(timer, preset=preset))
    with pytest.raises(LadderExportError, match=new):
        pyrung_to_ladder(program, TagMap(mappings), allow=(old,))
    pyrung_to_ladder(program, TagMap(mappings), allow=(new,))


def test_allow_names_what_replaced_the_removed_preset_code():
    timer, mappings = _timer()
    program = _program(lambda: on_delay(timer, preset=40000))
    code = "CLK_TIMER_PRESET_OVERFLOW"
    with pytest.raises(ValueError, match=f"no longer reports {code}. Pass constant-out-of-range"):
        pyrung_to_ladder(program, TagMap(mappings), allow=(code,))
    pyrung_to_ladder(program, TagMap(mappings), allow=(Code.CONSTANT_OUT_OF_RANGE,))
