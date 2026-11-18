"""Every instruction that writes a tag is seen by the readonly and final checks.

A program that writes a read-only system tag stays off the compiled kernel, so the write
raises as it does when stepping.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

import pyrung.click  # noqa: F401  (registers the CLICK instruction classes)
from pyrung.core import (
    Block,
    Bool,
    Char,
    Counter,
    Dint,
    Int,
    Program,
    Real,
    Tag,
    TagType,
    Timer,
    Word,
    blockcopy,
    call,
    copy,
    count_down,
    count_up,
    debounce,
    event_drum,
    fill,
    flasher,
    forloop,
    latch,
    math,
    off_delay,
    on_delay,
    out,
    pack_bits,
    pack_text,
    pack_words,
    reset,
    run_enabled_function,
    run_function,
    rung,
    search,
    shift,
    subroutine,
    system,
    time_drum,
    time_event_drum,
    timed,
    toggle,
    unpack_to_bits,
    unpack_to_words,
)
from pyrung.core.instruction.base import Instruction
from pyrung.core.instruction.send_receive import ModbusTcpTarget, receive, send
from pyrung.core.runner import _program_writes_read_only_system_tags
from pyrung.core.validation.duplicate_out import COIL_CONFLICTING_OUTPUT
from pyrung.core.validation.final_writers import TAG_FINAL_MULTIPLE_WRITERS
from pyrung.core.validation.readonly_write import TAG_READONLY_WRITE

_TAG_CLASSES = {
    TagType.BOOL: Bool,
    TagType.INT: Int,
    TagType.DINT: Dint,
    TagType.WORD: Word,
    TagType.REAL: Real,
    TagType.CHAR: Char,
}

#: A read-only system tag of each type that has one.
_SYSTEM_TAGS = {
    TagType.BOOL: system.sys.always_on,
    TagType.INT: system.sys.scan_counter,
    TagType.REAL: system.sys.dt,
}

#: pyrung names the loop index itself, so no user tag can be one.
_NOT_A_USER_TAG = {("ForLoopInstruction", "idx_tag")}


@dataclass(frozen=True)
class Case:
    """One write field of one instruction class, and a rung that writes *w* through it.

    A block field gets a block, and the check reports the first element of the range.
    """

    instruction: str
    field: str
    type: TagType
    build: Callable[[Any], None]
    block: bool = False

    @property
    def id(self) -> str:
        return f"{self.instruction}.{self.field}"

    @property
    def status(self) -> bool:
        """A timer's or counter's own status bit, which the final check leaves to its owner."""
        return self.field in {"en_bit", "tt_bit", "cu_bit", "cd_bit"}


def _en(name: str) -> Bool:
    return Bool(f"{name}_En")


def _timer(prefix: str, **fields: Tag) -> SimpleNamespace:
    tags = {
        "Done": Bool(f"{prefix}_Done"),
        "Acc": Int(f"{prefix}_Acc"),
        "EN": Bool(f"{prefix}_EN"),
        "TT": Bool(f"{prefix}_TT"),
    }
    return SimpleNamespace(**{**tags, **fields})


def _counter(prefix: str, **fields: Tag) -> SimpleNamespace:
    tags = {
        "Done": Bool(f"{prefix}_Done"),
        "Acc": Dint(f"{prefix}_Acc"),
        "CU": Bool(f"{prefix}_CU"),
        "CD": Bool(f"{prefix}_CD"),
    }
    return SimpleNamespace(**{**tags, **fields})


def _coil(dsl: Callable[[Tag], Any]) -> Callable[[Any], None]:
    def build(w: Any) -> None:
        with rung(_en("Coil")):
            dsl(w)

    return build


def _debounce_like(dsl: Callable[..., Any], field: str) -> Callable[[Any], None]:
    def build(w: Any) -> None:
        target = w if field == "target" else Bool("Deb_Target")
        on = _timer("DebOn", **({"Acc": w} if field == "on_acc" else {}))
        off = _timer("DebOff", **({"Acc": w} if field == "off_acc" else {}))
        with rung(_en("Deb")):
            dsl(target, on=(on, 10), off=(off, 10))

    return build


def _timed(field: str) -> Callable[[Any], None]:
    def build(w: Any) -> None:
        target = w if field == "target" else Bool("Timed_Target")
        timer = _timer("Timed", **({"Acc": w} if field == "timer_acc" else {}))
        with rung(_en("Timed")):
            timed(target, timer, 10)

    return build


_TIMER_FIELDS = {"done_bit": "Done", "accumulator": "Acc", "en_bit": "EN", "tt_bit": "TT"}
_COUNTER_FIELDS = {"done_bit": "Done", "accumulator": "Acc", "cu_bit": "CU", "cd_bit": "CD"}


def _timer_case(dsl: Callable[..., Any], field: str) -> Callable[[Any], None]:
    def build(w: Any) -> None:
        with rung(_en("Timer")):
            dsl(_timer("Tmr", **{_TIMER_FIELDS[field]: w}), 10)

    return build


def _counter_case(dsl: Callable[..., Any], field: str) -> Callable[[Any], None]:
    def build(w: Any) -> None:
        with rung(_en("Counter")):
            dsl(_counter("Ctr", **{_COUNTER_FIELDS[field]: w}), 5).reset(Bool("Ctr_Reset"))

    return build


def _drum(kind: str, field: str) -> Callable[[Any], None]:
    def build(w: Any) -> None:
        tags: dict[str, Any] = {
            "outputs": [Bool("Drum_Out1"), Bool("Drum_Out2")],
            "current_step": Int("Drum_Step"),
            "completion_flag": Bool("Drum_Done"),
            "accumulator": Int("Drum_Acc"),
        }
        tags[field] = [w, Bool("Drum_Out2")] if field == "outputs" else w
        pattern = [[1, 0], [0, 1]]
        with rung(_en("Drum")):
            if kind == "event":
                del tags["accumulator"]
                builder = event_drum(
                    events=[Bool("Drum_Ev1"), Bool("Drum_Ev2")], pattern=pattern, **tags
                )
            elif kind == "time":
                builder = time_drum(presets=[10, 10], pattern=pattern, **tags)
            else:
                builder = time_event_drum(
                    presets=[10, None], events=[None, Bool("Drum_Ev2")], pattern=pattern, **tags
                )
            builder.reset(Bool("Drum_Reset"))

    return build


def _search(field: str) -> Callable[[Any], None]:
    def build(w: Any) -> None:
        data = Block("SearchData", TagType.INT, 1, 4)
        result = w if field == "result" else Int("Search_Result")
        found = w if field == "found" else Bool("Search_Found")
        with rung(_en("Search")):
            search(data.select(1, 4) == 3, result=result, found=found)

    return build


def _callback(**kwargs: Any) -> dict[str, Any]:
    return {"result": 1}


def _enabled_callback(enabled: bool, **kwargs: Any) -> dict[str, Any]:
    return {"result": 1}


_PEER = ModbusTcpTarget(name="peer", ip="127.0.0.1", port=1)


def _modbus(kind: str, field: str) -> Callable[[Any], None]:
    def build(w: Any) -> None:
        status = {
            "success": Bool("Mb_Success"),
            "error": Bool("Mb_Error"),
            "exception_response": Int("Mb_Exception"),
        }
        busy = "sending" if kind == "send" else "receiving"
        status[busy] = Bool("Mb_Busy")
        with rung(_en("Modbus")):
            if kind == "send":
                status[field] = w
                send(target=_PEER, remote_start="DS1", source=Int("Mb_Source"), **status)
            else:
                dest = w if field == "dest" else Int("Mb_Dest")
                if field != "dest":
                    status[field] = w
                receive(target=_PEER, remote_start="DS1", dest=dest, **status)

    return build


def _rung(dsl: Callable[[Any], Any]) -> Callable[[Any], None]:
    def build(w: Any) -> None:
        with rung(_en("Data")):
            dsl(w)

    return build


def _shift(w: Any) -> None:
    with rung(_en("Shift")):
        shift(w.select(1, 2)).clock(Bool("Shift_Clock")).reset(Bool("Shift_Reset"))


def _math_in_a_loop(w: Any) -> None:
    with rung(_en("Loop")):
        with forloop(2):
            math(Int("Loop_A") + 1, w)


CASES = [
    Case("OutInstruction", "target", TagType.BOOL, _coil(out)),
    Case("LatchInstruction", "target", TagType.BOOL, _coil(latch)),
    Case("ResetInstruction", "target", TagType.BOOL, _coil(reset)),
    Case("ToggleInstruction", "target", TagType.BOOL, _coil(toggle)),
    *(
        Case(cls, field, kind, _debounce_like(dsl, field))
        for cls, dsl in (("DebounceInstruction", debounce), ("FlasherInstruction", flasher))
        for field, kind in (
            ("target", TagType.BOOL),
            ("on_acc", TagType.INT),
            ("off_acc", TagType.INT),
        )
    ),
    Case("TimedInstruction", "target", TagType.BOOL, _timed("target")),
    Case("TimedInstruction", "timer_acc", TagType.INT, _timed("timer_acc")),
    *(
        Case(
            cls,
            field,
            TagType.INT if field == "accumulator" else TagType.BOOL,
            _timer_case(dsl, field),
        )
        for cls, dsl in (("OnDelayInstruction", on_delay), ("OffDelayInstruction", off_delay))
        for field in _TIMER_FIELDS
    ),
    *(
        Case(
            cls,
            field,
            TagType.DINT if field == "accumulator" else TagType.BOOL,
            _counter_case(dsl, field),
        )
        for cls, dsl in (("CountUpInstruction", count_up), ("CountDownInstruction", count_down))
        for field in _COUNTER_FIELDS
    ),
    *(
        Case(
            cls,
            field,
            TagType.INT if field in {"current_step", "accumulator"} else TagType.BOOL,
            _drum(kind, field),
        )
        for cls, kind, fields in (
            ("EventDrumInstruction", "event", ("outputs", "current_step", "completion_flag")),
            (
                "TimeDrumInstruction",
                "time",
                ("outputs", "current_step", "accumulator", "completion_flag"),
            ),
            (
                "TimeEventDrumInstruction",
                "time_event",
                ("outputs", "current_step", "accumulator", "completion_flag"),
            ),
        )
        for field in fields
    ),
    Case("ShiftInstruction", "bit_range", TagType.BOOL, _shift, block=True),
    Case("CopyInstruction", "dest", TagType.INT, _rung(lambda w: copy(Int("Copy_Src"), w))),
    Case(
        "BlockCopyInstruction",
        "dest",
        TagType.INT,
        _rung(lambda w: blockcopy(Block("BC_Src", TagType.INT, 1, 2).select(1, 2), w.select(1, 2))),
        block=True,
    ),
    Case(
        "FillInstruction", "dest", TagType.INT, _rung(lambda w: fill(7, w.select(1, 2))), block=True
    ),
    Case("CalcInstruction", "dest", TagType.INT, _rung(lambda w: math(Int("Calc_A") + 1, w))),
    Case(
        "PackBitsInstruction",
        "dest",
        TagType.WORD,
        _rung(lambda w: pack_bits(Block("PB_Bits", TagType.BOOL, 1, 2).select(1, 2), w)),
    ),
    Case(
        "PackWordsInstruction",
        "dest",
        TagType.DINT,
        _rung(lambda w: pack_words(Block("PW_Words", TagType.INT, 1, 2).select(1, 2), w)),
    ),
    Case(
        "PackTextInstruction",
        "dest",
        TagType.INT,
        _rung(lambda w: pack_text(Block("PT_Chars", TagType.CHAR, 1, 2).select(1, 2), w)),
    ),
    Case(
        "UnpackToBitsInstruction",
        "bit_block",
        TagType.BOOL,
        _rung(lambda w: unpack_to_bits(Word("UB_Src"), w.select(1, 2))),
        block=True,
    ),
    Case(
        "UnpackToWordsInstruction",
        "word_block",
        TagType.INT,
        _rung(lambda w: unpack_to_words(Dint("UW_Src"), w.select(1, 2))),
        block=True,
    ),
    Case("SearchInstruction", "result", TagType.INT, _search("result")),
    Case("SearchInstruction", "found", TagType.BOOL, _search("found")),
    Case(
        "FunctionCallInstruction",
        "_outs",
        TagType.INT,
        _rung(lambda w: run_function(_callback, outs={"result": w})),
    ),
    Case(
        "EnabledFunctionCallInstruction",
        "_outs",
        TagType.INT,
        _rung(lambda w: run_enabled_function(_enabled_callback, outs={"result": w})),
    ),
    *(
        Case("ModbusSendInstruction", field, kind, _modbus("send", field))
        for field, kind in (
            ("sending", TagType.BOOL),
            ("success", TagType.BOOL),
            ("error", TagType.BOOL),
            ("exception_response", TagType.INT),
        )
    ),
    *(
        Case("ModbusReceiveInstruction", field, kind, _modbus("receive", field))
        for field, kind in (
            ("dest", TagType.INT),
            ("receiving", TagType.BOOL),
            ("success", TagType.BOOL),
            ("error", TagType.BOOL),
            ("exception_response", TagType.INT),
        )
    ),
]

#: Writes the declared fields do not show on their own: an instruction in a loop body.
EXTRA_CASES = [Case("ForLoopInstruction", "body", TagType.INT, _math_in_a_loop)]

ALL_CASES = CASES + EXTRA_CASES


def _flagged(case: Case, **flags: bool) -> tuple[Any, str]:
    """A tag (or block) carrying *flags*, and the name the check reports."""
    if case.block:
        block = Block("W", case.type, 1, 4)
        block.slot(1, **flags)
        return block, block[1].name
    tag = _TAG_CLASSES[case.type]("W", **flags)
    return tag, tag.name


def _program(build: Callable[[Any], None], w: Any, times: int = 1) -> Program:
    with Program(strict=False) as program:
        for _ in range(times):
            build(w)
    return program


def _concrete_instruction_classes() -> list[type]:
    found: list[type] = []
    pending = [Instruction]
    while pending:
        cls = pending.pop()
        for sub in cls.__subclasses__():
            pending.append(sub)
            if not inspect.isabstract(sub) and not sub.__name__.startswith("_"):
                found.append(sub)
    return found


def test_the_cases_cover_every_declared_write_field() -> None:
    declared = {
        (cls.__name__, field)
        for cls in _concrete_instruction_classes()
        for field in (*cls._writes, *getattr(cls, "_status_fields", ()))
    }
    assert {(case.instruction, case.field) for case in CASES} == declared - _NOT_A_USER_TAG


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda case: case.id)
def test_a_write_to_a_readonly_tag_is_reported(case: Case) -> None:
    w, name = _flagged(case, readonly=True)
    report = _program(case.build, w).check(select={TAG_READONLY_WRITE})
    assert name in {finding.target_name for finding in report.findings}


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda case: case.id)
def test_two_writes_to_a_final_tag_are_reported(case: Case) -> None:
    w, name = _flagged(case, final=True)
    if case.status:
        program = _program(case.build, w)
        with program:
            with rung(Bool("Status_Out_En")):
                out(w)
    else:
        program = _program(case.build, w, times=2)
    report = program.check(select={TAG_FINAL_MULTIPLE_WRITERS})
    assert name in {finding.target_name for finding in report.findings}


# The built-in Timer's EN and TT and Counter's CU and CD are final.  COIL_CONFLICTING_OUTPUT
# judges a timer or counter that is used twice.  It allows callers that exclude each other,
# so the final check does not count the status bits.
ReuseT = Timer.clone("ReuseT")
ReuseC = Counter.clone("ReuseC")
ReuseState = Int("ReuseState")


def _timer_and_counter() -> None:
    with rung(Bool("Reuse_Run")):
        on_delay(ReuseT, 100)
    with rung(Bool("Reuse_Up")):
        count_up(ReuseC, 5).reset(Bool("Reuse_Reset"))


def _findings(program: Program) -> list[tuple[str, str]]:
    return sorted((finding.code, finding.target_name) for finding in program.check().findings)


def test_a_timer_and_counter_in_subroutines_that_exclude_each_other_pass() -> None:
    with Program() as program:
        with rung(ReuseState == 1):
            call("reuse_a")
        with rung(ReuseState == 2):
            call("reuse_b")
        with subroutine("reuse_a"):
            _timer_and_counter()
        with subroutine("reuse_b"):
            _timer_and_counter()

    assert _findings(program) == []


def test_a_timer_and_counter_used_twice_in_main_conflict_on_done_and_acc_only() -> None:
    with Program() as program:
        _timer_and_counter()
        _timer_and_counter()

    assert _findings(program) == [
        (COIL_CONFLICTING_OUTPUT, ReuseC.Acc.name),
        (COIL_CONFLICTING_OUTPUT, ReuseC.Done.name),
        (COIL_CONFLICTING_OUTPUT, ReuseT.Acc.name),
        (COIL_CONFLICTING_OUTPUT, ReuseT.Done.name),
    ]


def test_an_out_to_a_status_bit_beside_its_timer_or_counter_is_a_second_writer() -> None:
    with Program() as program:
        _timer_and_counter()
        with rung(Bool("Reuse_Manual")):
            out(ReuseT.EN)
            out(ReuseC.CU)

    assert _findings(program) == [
        (TAG_FINAL_MULTIPLE_WRITERS, ReuseC.CU.name),
        (TAG_FINAL_MULTIPLE_WRITERS, ReuseT.EN.name),
    ]


@pytest.mark.parametrize(
    "case",
    [case for case in ALL_CASES if not case.block and case.type in _SYSTEM_TAGS],
    ids=lambda case: case.id,
)
def test_a_write_to_a_read_only_system_tag_keeps_the_kernel_off(case: Case) -> None:
    program = _program(case.build, _SYSTEM_TAGS[case.type])
    assert _program_writes_read_only_system_tags(program)
