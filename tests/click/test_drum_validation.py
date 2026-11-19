"""CLICK validation of event_drum(), time_drum() and time_event_drum(), which laddercodec's
Drum checks.

These tests cover how a refusal prints. A drum's outputs, events and presets are lists, so
the caret marks one item of a list. Its ``.jump(cond, step=...)`` holds the step inside a
chained call, so the caret marks the step.
"""

from __future__ import annotations

import dataclasses

import pytest
from laddercodec import Code, Drum

from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder
from pyrung.click.ladder import LadderExportError
from pyrung.click.validation import CLK_BANK_UNRESOLVED, families, validate_click_program
from pyrung.click.validation.fields import evaluate_family, refusals_of
from pyrung.click.validation.resolve import _ResolutionCache
from pyrung.core import (
    Block,
    Bool,
    Dint,
    Int,
    Or,
    TagType,
    event_drum,
    rise,
    shift,
    time_drum,
    time_event_drum,
)
from pyrung.core.program import Program, rung
from pyrung.core.validation.walker import ProgramLocation

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

_EVENT = "event_drum(outputs=[Y001, C2], events=[X003, C5], pattern=[[1, 0], [0, 1]], "
_EVENT_END = "current_step=DS1, completion_flag=C9)"


def _event(outputs=(y[1], c[2]), events=(x[3], c[5]), step=ds[1], flag=c[9]):
    return event_drum(
        outputs=list(outputs),
        events=list(events),
        pattern=[[1, 0], [0, 1]],
        current_step=step,
        completion_flag=flag,
    )


def _time(presets=(100, 200), accumulator=td[1], outputs=(y[1], c[2])):
    return time_drum(
        outputs=list(outputs),
        presets=list(presets),
        unit="s",
        pattern=[[1, 0], [0, 1]],
        current_step=ds[1],
        accumulator=accumulator,
        completion_flag=c[9],
    )


def _time_event(presets=(100, None, 300), events=(None, x[3], c[5])):
    return time_event_drum(
        outputs=[y[1], c[2]],
        presets=list(presets),
        events=list(events),
        unit="ms",
        pattern=[[1, 0], [0, 1], [1, 1]],
        current_step=ds[1],
        accumulator=td[1],
        completion_flag=c[9],
    )


def _program(write) -> Program:
    prog = Program(strict=False)
    with prog:
        with rung(x[1]):
            write()
    return prog


def _errors(write, mappings=()):
    tag_map = TagMap(list(mappings), include_system=False)
    return validate_click_program(_program(write), tag_map, mode="strict").errors


def _marked(finding) -> str:
    (frame,) = finding.display.frames
    _line, start, width = frame.caret
    return frame.lines[0][start : start + width]


def test_a_refused_output_marks_that_item_of_the_list():
    (finding,) = _errors(lambda: _event(outputs=(y[1], x[2])).reset(x[4]))
    assert finding.code == Code.BANK_NOT_ALLOWED
    (frame,) = finding.display.frames
    line = (
        "event_drum(outputs=[Y001, X002], events=[X003, C5], pattern=[[1, 0], [0, 1]], "
        "current_step=DS1, completion_flag=C9).reset(X004)"
    )
    assert frame.lines == (line,)
    assert frame.caret == (0, 26, 4)
    assert frame.caret_label == "X002 is not allowed"
    assert "This field takes Y, C." in finding.display.hint
    assert finding.location == "main.rung[1].instruction[0](EventDrumInstruction).instruction"


def test_the_label_gives_the_address_and_not_the_tags_name():
    out = Bool("Out1")
    (finding,) = _errors(lambda: _event(outputs=(y[1], out)).reset(x[4]), [out.map_to(x[2])])
    assert _marked(finding) == "Out1"
    label = finding.display.frames[0].caret_label
    assert "X002" in label
    assert "Out1" not in label


def test_a_refused_event_marks_that_item_of_the_list():
    (finding,) = _errors(lambda: _event(events=(x[3], ds[5] > 3)).reset(x[4]))
    assert finding.code == Code.NOT_AN_ADDRESS
    assert _marked(finding) == "DS5 > 3"
    # Each event is an address. A contact such as ~C5 or rise(C5) is not one.
    (finding,) = _errors(lambda: _event(events=(x[3], ~c[5])).reset(x[4]))
    assert finding.code == Code.NOT_AN_ADDRESS
    assert _marked(finding) == "~C5"


def test_a_refused_event_gives_the_advice_to_copy_it_into_a_c_bit():
    (finding,) = _errors(lambda: _event(events=(x[3], ds[5] > 3)).reset(x[4]))
    assert finding.suggestion == "Copy the condition into a C bit and use that bit as the event."


def test_allow_with_the_old_event_code_no_longer_lets_the_finding_through():
    write = lambda: _event(events=(x[3], ds[5] > 3)).reset(x[4])  # noqa: E731
    with pytest.raises(LadderExportError, match=Code.NOT_AN_ADDRESS):
        pyrung_to_ladder(_program(write), TagMap(), allow=("CLK_BANK_WRONG_ROLE",))


def test_a_pin_with_several_conditions_prints_them_as_separate_arguments():
    write = lambda: _event(outputs=(y[1], x[2])).reset(x[4], x[5]).jump(x[7], x[8], step=ds[2])  # noqa: E731
    (finding,) = _errors(write)
    (frame,) = finding.display.frames
    assert frame.lines[0].endswith(".reset(X004, X005).jump(X007, X008, step=DS2)")


def test_a_refused_event_of_a_time_and_event_drum_marks_its_step():
    (finding,) = _errors(lambda: _time_event(events=(None, x[3], ds[5] > 3)).reset(x[4]))
    assert finding.code == Code.NOT_AN_ADDRESS
    assert _marked(finding) == "DS5 > 3"


def test_a_refused_preset_marks_that_item_of_the_list():
    preset = Dint("Preset")
    (finding,) = _errors(
        lambda: _time(presets=(100, preset)).reset(x[4]),
        [preset.map_to(dd[5])],
    )
    assert finding.code == Code.BANK_NOT_ALLOWED
    assert _marked(finding) == "Preset"
    assert "DD5" in finding.display.frames[0].caret_label
    assert "OBJ-004" in finding.display.hint


def test_a_refused_constant_preset_marks_that_item():
    (finding,) = _errors(lambda: _time(presets=(100, -1)).reset(x[4]))
    assert finding.code == Code.CONSTANT_OUT_OF_RANGE
    assert _marked(finding) == "-1"
    (finding,) = _errors(lambda: _time_event(presets=(100, None, 40000)).reset(x[4]))
    assert finding.code == Code.CONSTANT_OUT_OF_RANGE
    assert _marked(finding) == "40000"


def test_a_constant_preset_of_zero_passes():
    assert _errors(lambda: _time(presets=(0, 32767)).reset(x[4])) == ()


@pytest.mark.parametrize(
    "write, marked, code",
    [
        (lambda: _event(step=dd[1]).reset(x[4]), "DD1", Code.BANK_NOT_ALLOWED),
        (lambda: _event(flag=y[9]).reset(x[4]), "Y009", Code.BANK_NOT_ALLOWED),
        (lambda: _time(accumulator=ds[3]).reset(x[4]), "DS3", Code.BANK_NOT_ALLOWED),
    ],
    ids=["current_step", "completion_flag", "accumulator"],
)
def test_a_refused_keyword_marks_its_value(write, marked, code):
    (finding,) = _errors(write)
    assert finding.code == code
    assert _marked(finding) == marked


def test_a_jump_step_in_a_bank_that_click_refuses_marks_the_step():
    (finding,) = _errors(lambda: _event().reset(x[4]).jump(x[7], step=dd[4]))
    assert finding.code == Code.BANK_NOT_ALLOWED
    (frame,) = finding.display.frames
    assert frame.lines == (f"{_EVENT}{_EVENT_END}.reset(X004).jump(X007, step=DD4)",)
    assert _marked(finding) == "DD4"
    assert "DD4" in frame.caret_label


@pytest.mark.parametrize("step", [2, 7], ids=str)
def test_a_constant_jump_step_marks_the_step_and_gives_the_advice(step):
    (finding,) = _errors(lambda: _event().reset(x[4]).jump(x[7], step=step).jog(x[8]))
    assert finding.code == Code.NOT_AN_ADDRESS
    (frame,) = finding.display.frames
    assert frame.lines == (f"{_EVENT}{_EVENT_END}.reset(X004).jump(X007, step={step}).jog(X008)",)
    assert _marked(finding) == str(step)
    assert finding.suggestion == "Copy the step into a DS tag and jump to that tag."
    assert finding.suggestion in finding.display.hint


@pytest.mark.parametrize(
    "write",
    [
        lambda: _event().reset(x[4]),
        lambda: _event().reset(x[4]).jump(x[7], step=ds[2]).jog(x[8]),
        lambda: _time().reset(x[4]).jump(x[7], step=ds[2]),
        lambda: _time(presets=(ds[5], 200)).reset(x[4]),
        lambda: _time_event().reset(x[4]).jog(x[8]),
        lambda: _time_event(presets=(ds[5], None, 300)).reset(x[4]),
    ],
    ids=["event", "event pins", "time pins", "time register", "time event", "time event register"],
)
def test_a_drum_that_click_takes_passes(write):
    assert _errors(write) == ()


def test_each_unmapped_operand_names_its_argument():
    out, event, preset = Bool("Out1"), Bool("Event1"), Int("Preset")
    reset, step = Bool("Reset"), Int("Step")
    write = lambda: (
        time_event_drum(  # noqa: E731
            outputs=[out, c[2]],
            presets=[preset, None],
            events=[None, event],
            unit="s",
            pattern=[[1, 0], [0, 1]],
            current_step=ds[1],
            accumulator=td[1],
            completion_flag=c[9],
        )
        .reset(reset)
        .jump(x[7], step=step)
    )
    findings = _errors(write)
    assert [f.code for f in findings] == [CLK_BANK_UNRESOLVED] * 5
    assert [f.message.split(" at ")[0] for f in findings] == [
        "pyrung cannot find one CLICK address for the drum output",
        "pyrung cannot find one CLICK address for the drum event",
        "pyrung cannot find one CLICK address for the drum preset",
        "pyrung cannot find one CLICK address for the reset",
        "pyrung cannot find one CLICK address for the jump step",
    ]
    assert [f.location.rpartition(".")[2] for f in findings] == [
        "outputs",
        "events",
        "presets",
        "reset_condition",
        "jump_step",
    ]


@pytest.mark.parametrize("pin", ["reset", "jump", "jog"])
def test_an_unmapped_tag_in_a_chained_call_names_the_call(pin):
    pin_tag = Bool("Pin")

    def write():
        drum = _event()
        drum = drum.reset(pin_tag if pin == "reset" else x[4])
        if pin == "jump":
            drum = drum.jump(pin_tag, step=ds[2])
        if pin == "jog":
            drum = drum.jog(pin_tag)

    (finding,) = _errors(write)
    assert finding.code == CLK_BANK_UNRESOLVED
    assert finding.message.startswith(f"pyrung cannot find one CLICK address for the {pin}")


def test_the_export_refuses_a_drum_that_click_refuses():
    write = lambda: _event(outputs=(y[1], x[2])).reset(x[4])  # noqa: E731
    with pytest.raises(LadderExportError, match=Code.BANK_NOT_ALLOWED):
        pyrung_to_ladder(_program(write), TagMap())
    write = lambda: _event().reset(x[4]).jump(x[7], step=2)  # noqa: E731
    with pytest.raises(LadderExportError, match=Code.NOT_AN_ADDRESS):
        pyrung_to_ladder(_program(write), TagMap())


def test_allow_names_what_replaced_the_removed_code():
    write = lambda: _event().reset(x[4]).jump(x[7], step=2)  # noqa: E731
    code = "CLK_DRUM_JUMP_STEP_TAG_REQUIRED"
    with pytest.raises(ValueError, match=f"no longer reports {code}. Pass not-an-address"):
        pyrung_to_ladder(_program(write), TagMap(), allow=(code,))


@pytest.mark.parametrize("code", ["CLK_BANK_NOT_WRITABLE", "CLK_BANK_WRONG_ROLE"])
def test_allow_with_a_shared_old_code_no_longer_lets_the_finding_through(code):
    # Other checks still report these codes, so allow= takes them. A drum no longer does.
    write = lambda: _event(outputs=(y[1], x[2])).reset(x[4])  # noqa: E731
    with pytest.raises(LadderExportError, match=Code.BANK_NOT_ALLOWED):
        pyrung_to_ladder(_program(write), TagMap(), allow=(code,))
    pyrung_to_ladder(_program(write), TagMap(), allow=(Code.BANK_NOT_ALLOWED,))


def test_allow_with_the_old_preset_code_no_longer_lets_the_finding_through():
    # CLK_TIMER_PRESET_TOO_LOW is still a code of the timers, so allow= takes it. A drum no
    # longer reports it.
    write = lambda: _time(presets=(100, -1)).reset(x[4])  # noqa: E731
    with pytest.raises(LadderExportError, match=Code.CONSTANT_OUT_OF_RANGE):
        pyrung_to_ladder(_program(write), TagMap(), allow=("CLK_TIMER_PRESET_TOO_LOW",))
    pyrung_to_ladder(_program(write), TagMap(), allow=(Code.CONSTANT_OUT_OF_RANGE,))


def _bank_split_drum(out):
    return lambda: _event(outputs=(y[1], out)).reset(x[4])


_FORMS = {
    "event": lambda: _event().reset(x[4]),
    "event pins": lambda: _event().reset(x[4]).jump(x[7], step=ds[2]).jog(x[8]),
    "time": lambda: _time().reset(x[4]),
    "time register preset": lambda: _time(presets=(ds[5], 200)).reset(x[4]),
    "time other bank preset": lambda: _time(presets=(dd[5], 200)).reset(x[4]),
    "time large preset": lambda: _time(presets=(40000, 200)).reset(x[4]),
    "time negative preset": lambda: _time(presets=(-1, 200)).reset(x[4]),
    "time event": lambda: _time_event().reset(x[4]),
    "event output in X": _bank_split_drum(x[2]),
    "event output is the flag": lambda: _event(outputs=(y[1], c[9]), flag=c[9]).reset(x[4]),
    "event NC": lambda: _event(events=(x[3], ~c[5])).reset(x[4]),
    "event rise": lambda: _event(events=(x[3], rise(c[5]))).reset(x[4]),
    "event compare": lambda: _event(events=(x[3], ds[5] > 3)).reset(x[4]),
    "event Or": lambda: _event(events=(x[3], Or(c[5], c[6]))).reset(x[4]),
    "constant jump step": lambda: _event().reset(x[4]).jump(x[7], step=2),
    "step in DD": lambda: _event(step=dd[1]).reset(x[4]),
    "shift": lambda: shift(c.select(1, 8)).clock(x[2]).reset(x[3]),
    "shift reversed": lambda: shift(c.select(1, 8).reverse()).clock(x[2]).reset(x[3]),
    "shift in Y": lambda: shift(y.select(1, 8)).clock(x[2]).reset(x[3]),
    "shift one address": lambda: shift(c.select(2, 2)).clock(x[2]).reset(x[3]),
    "shift in X": lambda: shift(x.select(1, 8)).clock(x[2]).reset(x[3]),
}


_SCATTERED = Block("Scattered", TagType.BOOL, 1, 3)
_MIXED = Block("Mixed", TagType.BOOL, 1, 3)
_DESCENDING = Block("Descending", TagType.BOOL, 1, 3)
_MAPPINGS = {
    "scattered": [
        _SCATTERED[1].map_to(c[31]),
        _SCATTERED[2].map_to(c[35]),
        _SCATTERED[3].map_to(c[39]),
    ],
    "mixed": [_MIXED[1].map_to(c[21]), _MIXED[2].map_to(y[22]), _MIXED[3].map_to(c[23])],
    "descending": [
        _DESCENDING[1].map_to(c[43]),
        _DESCENDING[2].map_to(c[42]),
        _DESCENDING[3].map_to(c[41]),
    ],
}
_FORMS_WITH_MAPPINGS = {
    "shift scattered": (_SCATTERED.select(1, 3), "scattered"),
    "shift scattered reversed": (_SCATTERED.select(1, 3).reverse(), "scattered"),
    "shift mixed banks": (_MIXED.select(1, 3), "mixed"),
    "shift descending block": (_DESCENDING.select(1, 3), "descending"),
    "shift descending block reversed": (_DESCENDING.select(1, 3).reverse(), "descending"),
}
for _name, (_bits, _key) in _FORMS_WITH_MAPPINGS.items():
    _FORMS[_name] = lambda _bits=_bits: shift(_bits).clock(x[2]).reset(x[3])


@pytest.mark.parametrize("name", list(_FORMS))
def test_validation_and_the_export_agree_on_a_shift_or_drum(name):
    # The check builds laddercodec's instruction from the fields, and the export writes a
    # call that laddercodec parses. Both must refuse the same forms.
    program = _program(_FORMS[name])
    key = _FORMS_WITH_MAPPINGS.get(name, (None, None))[1]
    tag_map = TagMap(_MAPPINGS.get(key, []), include_system=False)
    refused_by_check = bool(validate_click_program(program, tag_map, mode="strict").errors)
    try:
        pyrung_to_ladder(program, tag_map)
        refused_by_export = False
    except LadderExportError:
        refused_by_export = True
    assert refused_by_check == refused_by_export


# These tests hand the family's real build a Drum that laddercodec refuses in a way that no
# program of pyrung can reach, and check where the caret goes. The drum refuses a repeated
# output when it is built, and the export draws every rung within CLICK's height.


@pytest.fixture
def refused(monkeypatch):
    family = next(f for f in families.FAMILIES if f.name == "drums")

    def refuse(refusals):
        swapped = dataclasses.replace(family, build=lambda instruction, exporter: refusals)
        others = [f for f in families.FAMILIES if f is not family]
        monkeypatch.setattr(families, "FAMILIES", [*others, swapped])
        families._by_class.cache_clear()

    yield refuse
    families._by_class.cache_clear()


def _real_refusals(**changes):
    fields = {
        "kind": "event_drum",
        "outputs": ("Y1", "C2"),
        "events": ("X3", "C5"),
        "pattern": ((1, 0), (0, 1)),
        "current_step": "DS1",
        "completion_flag": "C9",
    }
    return refusals_of(lambda: Drum(**{**fields, **changes}))


def _evaluate(build):
    instruction = _program(lambda: build().reset(x[4]).jump(x[7], step=ds[2])).rungs[0]
    instruction = instruction._instructions[0]
    location = ProgramLocation("main", None, 0, (), 0, type(instruction).__name__, "instruction")
    resolver = _ResolutionCache(TagMap([], include_system=False))
    return evaluate_family(instruction, location, None, "strict", resolver, {}).findings()


def test_a_repeated_output_marks_the_repeat(refused):
    refusals = _real_refusals(outputs=("Y1", "Y1"))
    assert [r.code for r in refusals] == [Code.DUPLICATE_ADDRESS]
    refused(refusals)
    (finding,) = _evaluate(lambda: _event(outputs=(y[1], c[2])))
    assert _marked(finding) == "C2"


def test_an_output_that_is_the_completion_flag_marks_the_output():
    # The output C9 is also the completion flag, so laddercodec reports the repeat.
    (finding,) = _errors(lambda: _event(outputs=(y[1], c[9]), flag=c[9]).reset(x[4]))
    assert finding.code == Code.DUPLICATE_ADDRESS
    assert _marked(finding) == "C9"
    (frame,) = finding.display.frames
    assert frame.caret[1] < frame.lines[0].index("completion_flag")


def test_a_refusal_of_the_pin_rows_marks_the_reset(refused):
    refusals = _real_refusals(pin_rows=(3, 2, 1))
    assert [r.code for r in refusals] == [Code.INVALID_INPUT_ROWS]
    refused(refusals)
    (finding,) = _evaluate(_event)
    assert _marked(finding) == "X004"
