"""A family whose call does not fit its class's declarations overrides the hooks of its Family.

send() is the first example. Its target and remote start hold no tag, and a status bit that
the author leaves off holds a hidden tag. event_drum() is the second. Its outputs are a
list, and laddercodec refuses one item of it. The declarations below use only the hooks and
the public helpers, so a family branch could put them in its own module and edit no shared
code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from pyrung.click import ClickBlocks, TagMap
from pyrung.click.validation import CLK_BANK_UNRESOLVED, families
from pyrung.click.validation.fields import Family, Operand, Place, default_place, evaluate_family
from pyrung.click.validation.resolve import _ResolutionCache
from pyrung.core import Int, ModbusTcpTarget, send
from pyrung.core.instruction.send_receive import is_modbus_status_tag
from pyrung.core.program import Program, rung
from pyrung.core.validation.render import Call, call_of, operand_name
from pyrung.core.validation.walker import ProgramLocation

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

_STATUS_BITS = ("sending", "success", "error", "exception_response")


def _send_operands(instruction: Any) -> list[Operand]:
    """Return each argument of send() that holds a tag the author wrote."""
    operands = [Operand("source", "source", (("source", instruction.source),))]
    for name in _STATUS_BITS:
        bit = getattr(instruction, name)
        # A status bit that the author left off holds a hidden tag, which CLICK never sees.
        if not is_modbus_status_tag(bit.name):
            operands.append(Operand(name, f"{name.replace('_', ' ')} flag", ((name, bit),)))
    return operands


def _send_call(instruction: Any) -> Call:
    """Return send() as its author wrote it."""
    target = instruction.raw_target
    if isinstance(target, ModbusTcpTarget):
        target_text = f"ModbusTcpTarget({target.name!r}, {target.ip!r})"
    else:
        target_text = repr(instruction.target_name)
    arguments = [
        ("target", target_text, "keyword"),
        ("remote_start", repr(f"{instruction.bank}{instruction.start}"), "keyword"),
    ]
    for operand in _send_operands(instruction):
        ((_attribute, value),) = operand.values
        arguments.append((str(operand.argument), operand_name(value), "keyword"))
    return call_of("send", arguments)


@dataclass(frozen=True)
class _FakeRefusal:
    """A refusal shaped like laddercodec's, so that each argument of send() gets one."""

    field: str
    code: str = "bank-not-allowed"
    click_id: str | None = None

    def message(self, names: Any) -> str:
        return f"{self.field} is refused"

    def short(self, names: Any) -> str:
        return "refused"

    def detail(self, names: Any) -> str:
        return "The test refuses it."


_REFUSED = ("Send.target", "Send.remote_start", "Send.source", "Send.success")

SEND = Family(
    name="send",
    classes={
        "ModbusSendInstruction": {
            "source": "source",
            **{name: f"{name.replace('_', ' ')} flag" for name in _STATUS_BITS},
        }
    },
    build=lambda instruction, exporter: [_FakeRefusal(field) for field in _REFUSED],
    arguments={
        "Send.target": "target",
        "Send.remote_start": "remote_start",
        "Send.source": "source",
        "Send.quantity": "source",
        "Send.sending": "sending",
        "Send.success": "success",
        "Send.error": "error",
        "Send.exception_response": "exception_response",
        "Send.word_swap": "word_swap",
        "Send.function_code": "remote_start",
    },
    advice=lambda code, field, instruction, resolver: None,
    operands=_send_operands,
    call_text=_send_call,
)


@pytest.fixture
def send_family(monkeypatch):
    # This family stands in for any send() family that a branch adds.
    others = [f for f in families.FAMILIES if "ModbusSendInstruction" not in f.classes]
    monkeypatch.setattr(families, "FAMILIES", [*others, SEND])
    families._by_class.cache_clear()
    yield
    families._by_class.cache_clear()


def _send(source):
    prog = Program(strict=False)
    with prog:
        with rung(x[1]):
            send(
                target=ModbusTcpTarget("plc2", "192.168.1.2"),
                remote_start="DS1",
                source=source,
                success=c[2],
            )
    return prog.rungs[0]._instructions[0]


def _evaluate(instruction, mappings=()):
    location = ProgramLocation("main", None, 0, (), 0, "ModbusSendInstruction", "instruction")
    resolver = _ResolutionCache(TagMap(list(mappings), include_system=False))
    return evaluate_family(instruction, location, None, "strict", resolver, {}).findings()


def test_a_status_bit_left_off_needs_no_address(send_family):
    source = Int("Src")
    findings = _evaluate(_send(source), [source.map_to(ds[10])])
    assert CLK_BANK_UNRESOLVED not in {finding.code for finding in findings}


def test_the_finding_shows_the_call_that_the_author_wrote(send_family):
    source = Int("Src")
    finding, *_ = _evaluate(_send(source), [source.map_to(ds[10])])
    call = "send(target=ModbusTcpTarget('plc2', '192.168.1.2'), remote_start='DS1', source=Src, success=C2)"
    assert finding.display.frames[0].lines == (call,)
    assert finding.message.startswith(f"CLICK refuses {call} at ")


def test_each_argument_gets_a_caret(send_family):
    source = Int("Src")
    findings = _evaluate(_send(source), [source.map_to(ds[10])])
    marked = []
    for finding in findings:
        frame = finding.display.frames[0]
        _line, start, width = frame.caret
        marked.append(frame.lines[0][start : start + width])
    assert marked == ["ModbusTcpTarget('plc2', '192.168.1.2')", "'DS1'", "Src", "C2"]


def test_an_unmapped_source_names_the_source(send_family):
    (finding,) = _evaluate(_send(Int("Src")))
    assert finding.code == CLK_BANK_UNRESOLVED
    assert finding.message.startswith("pyrung cannot find one CLICK address for the source")


def _declared_family(monkeypatch, cls, words):
    """Register a family of *cls* that reads its declarations, as a family branch would.

    Each class gets a name of its own, so that the test still runs once a family branch
    adds the real class to a family.
    """
    family = Family(
        name=cls.__name__,
        classes={cls.__name__: words},
        build=lambda instruction, exporter: [],
        arguments={},
        advice=lambda code, field, instruction, resolver: None,
    )
    monkeypatch.setattr(families, "FAMILIES", [*families.FAMILIES, family])
    families._by_class.cache_clear()


@pytest.fixture
def clear_families():
    yield
    families._by_class.cache_clear()


def test_an_unmapped_tag_in_a_chained_call_is_reported(monkeypatch, clear_families):
    from pyrung.core import Bool
    from pyrung.core.instruction import ShiftInstruction

    declared = type(
        "HookTestShift",
        (ShiftInstruction,),
        {
            "_call_operands": (("bit_range", "bit_range"),),
            "_call_chained": (("clock", "clock_condition"), ("reset", "reset_condition")),
        },
    )
    _declared_family(monkeypatch, declared, {"bit_range": "range"})
    clock = Bool("Clk")
    findings = _evaluate(declared(c.select(1, 8), None, clock, ~Bool("Rst")), [clock.map_to(x[2])])
    assert [f.message.split(" at ")[0] for f in findings] == [
        "pyrung cannot find one CLICK address for the reset"
    ]


def test_an_argument_that_holds_several_attributes_gets_one_finding(monkeypatch, clear_families):
    from pyrung.core import Timer
    from pyrung.core.instruction import OnDelayInstruction

    declared = type(
        "HookTestDelay",
        (OnDelayInstruction,),
        {
            "_call_operands": (("timer", ("done_bit", "accumulator")),),
            "_call_keyword_operands": (("preset", "preset"),),
        },
    )
    _declared_family(monkeypatch, declared, {"done_bit": "timer", "accumulator": "timer"})
    timer = Timer.clone("HookTest_Delay")
    (finding,) = _evaluate(declared(timer.Done, timer.Acc, 100, None))
    assert finding.code == CLK_BANK_UNRESOLVED
    assert finding.message.startswith("pyrung cannot find one CLICK address for the timer")
    assert finding.location.endswith("instruction.done_bit")


# A drum's call holds lists, and laddercodec names one item of a list field with its index,
# such as Drum.outputs[1]. The scratch family below declares event_drum() through the hooks.

_DRUM_FIELDS = {
    "Drum.outputs": "outputs",
    "Drum.completion_flag": "completion_flag",
}


def _drum_operands(instruction: Any) -> list[Operand]:
    """Return the outputs and the completion flag of event_drum(), each output a value."""
    outputs = tuple(("outputs", tag) for tag in instruction.outputs)
    flag = (("completion_flag", instruction.completion_flag),)
    return [Operand("outputs", "drum output", outputs), Operand("completion_flag", "flag", flag)]


def _drum_call(instruction: Any) -> Call:
    """Return event_drum() with the text of each output as an item of its list."""
    return call_of(
        "event_drum",
        [
            ("outputs", [operand_name(tag) for tag in instruction.outputs], "keyword"),
            ("completion_flag", operand_name(instruction.completion_flag), "keyword"),
        ],
    )


@dataclass(frozen=True)
class _FakePair(_FakeRefusal):
    """A refusal of two fields, shaped like laddercodec's refusal of a repeated output."""

    fields: tuple[str, ...] = ()


def _drum_family(monkeypatch, refusals, place=None):
    family = Family(
        name="HookTestDrum",
        classes={"EventDrumInstruction": {"outputs": "drum output", "completion_flag": "flag"}},
        build=lambda instruction, exporter: list(refusals),
        arguments=_DRUM_FIELDS,
        advice=lambda code, field, instruction, resolver: None,
        operands=_drum_operands,
        call_text=_drum_call,
        place=place,
    )
    others = [f for f in families.FAMILIES if "EventDrumInstruction" not in f.classes]
    monkeypatch.setattr(families, "FAMILIES", [*others, family])
    families._by_class.cache_clear()


def _drum(outputs):
    from pyrung.core import Bool, event_drum

    done = Bool("DrumDone")
    prog = Program(strict=False)
    with prog:
        with rung(x[1]):
            event_drum(
                outputs=outputs,
                events=[x[5], x[6]],
                pattern=[[1, 0], [0, 1]],
                current_step=ds[7],
                completion_flag=done,
            ).reset(x[3])
    location = ProgramLocation("main", None, 0, (), 0, "EventDrumInstruction", "instruction")
    mapped = [tag.map_to(y[1]) for tag in outputs if tag.name == "Out1"]
    resolver = _ResolutionCache(TagMap([*mapped, done.map_to(c[9])], include_system=False))
    instruction = prog.rungs[0]._instructions[0]
    return evaluate_family(instruction, location, None, "strict", resolver, {}).findings()


def _marked(finding) -> str:
    frame = finding.display.frames[0]
    _line, start, width = frame.caret
    return frame.lines[0][start : start + width]


def test_a_refused_item_marks_that_item_of_the_list(monkeypatch, clear_families):
    from pyrung.core import Bool

    _drum_family(monkeypatch, [_FakeRefusal("Drum.outputs[1]")])
    (finding,) = _drum([Bool("Out1"), x[9]])
    frame = finding.display.frames[0]
    assert frame.lines == ("event_drum(outputs=[Out1, X009], completion_flag=DrumDone)",)
    assert frame.caret == (0, 26, 4)
    assert frame.caret_label == "refused"


def test_a_refused_pair_marks_the_first_item_that_it_names(monkeypatch, clear_families):
    from pyrung.core import Bool

    pair = _FakePair("Drum.outputs[1]", fields=("Drum.outputs[1]", "Drum.outputs[0]"))
    _drum_family(monkeypatch, [pair])
    # Out1 maps to Y001, so both items are Y001. laddercodec names the repeat first.
    (finding,) = _drum([Bool("Out1"), y[1]])
    assert finding.display.frames[0].caret == (0, 26, 4)
    assert _marked(finding) == "Y001"


def test_a_family_can_place_a_refusal_itself(monkeypatch, clear_families):
    from pyrung.core import Bool

    pair = _FakePair("Drum.outputs[0]", fields=("Drum.outputs[0]", "Drum.completion_flag"))
    # This family marks the completion flag of such a pair, not the output.
    _drum_family(monkeypatch, [pair], place=lambda refusal: Place("completion_flag"))
    (finding,) = _drum([Bool("Out1"), x[9]])
    assert _marked(finding) == "DrumDone"


def test_the_default_place_reads_the_index_of_an_item():
    arguments = {"Drum.outputs": "outputs", "Copy.source": 0}
    assert default_place("Drum.outputs[12]", arguments) == Place("outputs", 12)
    assert default_place("Copy.source", arguments) == Place(0)
    assert default_place("Drum.events[1]", arguments) is None
    assert default_place(None, arguments) is None
