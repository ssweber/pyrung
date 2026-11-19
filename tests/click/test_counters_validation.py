"""CLICK validation of count_up() and count_down(), which laddercodec's Counter checks.

These tests cover how a refusal prints: the call, the caret under the refused argument, and
the hint. The counter's UDT is one argument that holds the Done bit and the Acc. `.down()` and
`.reset()` are chained calls.
"""

from __future__ import annotations

import pytest
from laddercodec import Code

from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder
from pyrung.click.ladder import LadderExportError
from pyrung.click.validation import CLK_BANK_UNRESOLVED, validate_click_program
from pyrung.core import Bool, Counter, Dint
from pyrung.core.program import Program, count_down, count_up, rung

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


def _counter(done=ct[1], acc=ctd[1]):
    counter = Counter.clone("Cn")
    return counter, [counter.Done.map_to(done), counter.Acc.map_to(acc)]


def test_a_refused_done_bit_marks_the_counter():
    counter, mappings = _counter(done=c[1])
    (finding,) = _errors(lambda: count_up(counter, preset=5).reset(x[2]), mappings)
    assert finding.code == Code.BANK_NOT_ALLOWED
    assert _frame(finding) == (("count_up(Cn, preset=5).reset(X002)",), (0, 9, 2))
    assert finding.display.frames[0].caret_label == "C1 is not allowed"
    assert "This field takes CT." in finding.display.hint
    assert finding.location == "main.rung[1].instruction[0](CountUpInstruction).instruction"
    assert finding.message.isascii()


def test_a_refused_count_down_accumulator_marks_the_counter():
    counter, mappings = _counter(acc=dd[1])
    (finding,) = _errors(lambda: count_down(counter, preset=-5).reset(x[2]), mappings)
    assert finding.code == Code.BANK_NOT_ALLOWED
    assert _frame(finding) == (("count_down(Cn, preset=-5).reset(X002)",), (0, 11, 2))
    assert finding.display.frames[0].caret_label == "DD1 is not allowed"


def test_the_call_shows_the_down_and_the_reset():
    counter, mappings = _counter(done=c[1])
    (finding,) = _errors(lambda: count_up(counter, preset=5).down(x[3]).reset(x[2]), mappings)
    assert _frame(finding) == (("count_up(Cn, preset=5).down(X003).reset(X002)",), (0, 9, 2))


def test_the_label_gives_the_address_and_not_the_tag_name():
    preset = Dint("Goal")
    counter, mappings = _counter()
    (finding,) = _errors(
        lambda: count_up(counter, preset=preset).reset(x[2]), [*mappings, preset.map_to(ctd[9])]
    )
    assert finding.code == Code.BANK_NOT_ALLOWED
    assert _frame(finding)[1] == (0, 20, 4)
    label = finding.display.frames[0].caret_label
    assert "CTD9" in label
    assert "Goal" not in label
    assert "OBJ-004" in finding.display.hint


@pytest.mark.parametrize("call", [count_up, count_down])
def test_a_preset_beyond_32_bits_marks_the_preset(call):
    counter, mappings = _counter()
    (finding,) = _errors(lambda: call(counter, preset=2**40).reset(x[2]), mappings)
    assert finding.code == Code.CONSTANT_OUT_OF_RANGE
    lines, caret = _frame(finding)
    assert lines[0].startswith(f"{call.__name__}(Cn, preset=1099511627776)")
    assert caret == (0, lines[0].index("preset=") + 7, 13)
    assert "OBJ-005" in finding.display.problem


def test_a_done_and_acc_with_different_numbers_are_refused():
    counter, mappings = _counter(done=ct[1], acc=ctd[2])
    (finding,) = _errors(lambda: count_up(counter, preset=5).reset(x[2]), mappings)
    assert finding.code == Code.TIMER_PAIR_MISMATCH
    assert finding.suggestion == "Map Done and Acc to the same number, e.g. CT5 with CTD5."
    assert _frame(finding) == (("count_up(Cn, preset=5).reset(X002)",), (0, 9, 2))


@pytest.mark.parametrize("call", [count_up, count_down])
@pytest.mark.parametrize("preset", [-(2**31), -1, 0, 2**31 - 1])
def test_a_form_that_click_takes_has_no_findings(call, preset):
    counter, mappings = _counter(done=ct[250], acc=ctd[250])
    assert _errors(lambda: call(counter, preset=preset).reset(x[2]), mappings) == ()


@pytest.mark.parametrize("bank", [ds, dd])
def test_a_preset_register_that_click_takes_has_no_findings(bank):
    preset = Dint("Goal") if bank is dd else None
    counter, mappings = _counter()
    if preset is None:
        assert _errors(lambda: count_up(counter, preset=ds[3]).reset(x[2]), mappings) == ()
    else:
        mappings = [*mappings, preset.map_to(dd[3])]
        assert _errors(lambda: count_up(counter, preset=preset).reset(x[2]), mappings) == ()


def test_a_bidirectional_counter_that_click_takes_has_no_findings():
    counter, mappings = _counter()
    assert _errors(lambda: count_up(counter, preset=5).down(x[3]).reset(x[2]), mappings) == ()


def test_an_unmapped_counter_names_the_counter():
    (finding,) = _errors(lambda: count_up(Counter.clone("Cn"), preset=5).reset(x[2]))
    assert finding.code == CLK_BANK_UNRESOLVED
    assert "for the counter at" in finding.message


def test_an_unmapped_preset_names_the_preset():
    counter, mappings = _counter()
    (finding,) = _errors(lambda: count_up(counter, preset=Dint("Goal")).reset(x[2]), mappings)
    assert finding.code == CLK_BANK_UNRESOLVED
    assert "for the preset at" in finding.message


def test_an_unmapped_reset_names_the_reset_condition():
    counter, mappings = _counter()
    (finding,) = _errors(lambda: count_down(counter, preset=-5).reset(Bool("Clear")), mappings)
    assert finding.code == CLK_BANK_UNRESOLVED
    assert "for the reset condition at" in finding.message


def test_an_unmapped_down_names_the_down_condition():
    counter, mappings = _counter()
    (finding,) = _errors(
        lambda: count_up(counter, preset=5).down(Bool("Back")).reset(x[2]), mappings
    )
    assert finding.code == CLK_BANK_UNRESOLVED
    assert "for the down condition at" in finding.message


def test_the_export_refuses_a_form_that_click_refuses():
    counter, mappings = _counter(done=c[1])
    program = _program(lambda: count_down(counter, preset=-5).reset(x[2]))
    with pytest.raises(LadderExportError, match=Code.BANK_NOT_ALLOWED):
        pyrung_to_ladder(program, TagMap(mappings))


@pytest.mark.parametrize(
    ("old", "new", "mapping"),
    [
        ("CLK_BANK_WRONG_ROLE", Code.BANK_NOT_ALLOWED, {"done": c[1]}),
        ("CLK_TIMER_PAIR_MISMATCH", Code.TIMER_PAIR_MISMATCH, {"acc": ctd[2]}),
    ],
)
def test_the_old_code_no_longer_lets_the_finding_through(old, new, mapping):
    # Other checks still report both old codes, so allow= accepts them. The old code no
    # longer allows laddercodec's finding, and the new code does.
    counter, mappings = _counter(**mapping)
    program = _program(lambda: count_up(counter, preset=5).reset(x[2]))
    with pytest.raises(LadderExportError, match=new):
        pyrung_to_ladder(program, TagMap(mappings), allow=(old,))
    pyrung_to_ladder(program, TagMap(mappings), allow=(new,))
