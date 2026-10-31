"""A box reads its enable and every pin at one moment, before it executes.

pyrung's adopted model, pending hardware (conformance probes
``counters/pins_read_before_count`` and ``drum/pins_read_before_step_fix``):
CLICK gives a box with pins its own rung (C000A: all pins start from the same
branch), so a pin's wire either runs from the rail with all its own contacts
(rail form) or tees off the box's input wire after some shared contacts
(shared-parent form).  The two shapes must solve the same: the box takes its
enable and every pin in one reading, before any write of its own.

Each box kind runs twice here.  Program A writes the pins as ``And(parent, own)``
with one shared parent; program B copies the parent's contacts into every
pin.  Parents read the box's own state (counter Done/Acc, timer Done/Acc, shift
bits, drum step and completion flag) and carry rise()/fall().  A and B must
agree scan by scan, on every backend: the interpreter, the compiled kernel and
the generated CircuitPython.  The invalid-step cases pin the value a drum pin
sees when the drum corrects the step to 1 as it runs.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from pyrung.circuitpy import RunStopConfig, generate_circuitpy
from pyrung.core import (
    PLC,
    And,
    Block,
    Bool,
    CompiledPLC,
    Counter,
    Int,
    Or,
    Program,
    Rung,
    Tag,
    TagType,
    Timer,
    copy,
    count_down,
    count_up,
    event_drum,
    fall,
    on_delay,
    out,
    rise,
    shift,
)
from tests.circuitpy.test_codegen import _context_for_program, _run_single_scan_source
from tests.circuitpy.test_scan_time_and_stop import _BoardClock, _hw
from tests.circuitpy.test_system_points import _StubBase

DT = 0.01  # one 10 ms scan, as the board's loop takes
SCANS = 90
SEEDS = (1, 2, 3)

# Inputs every kind draws from; unused ones are simply not read.
En, A, P1, P2, P3 = (Bool(n) for n in ("En", "A", "P1", "P2", "P3"))
E1, E2, E3 = (Bool(n) for n in ("E1", "E2", "E3"))
DoStage = Bool("DoStage")
Stage = Int("Stage")
Out = Bool("Out")
Y1, Y2 = Bool("Y1"), Bool("Y2")
Step, Flag = Int("Step"), Bool("Flag")
S = Block("S", TagType.BOOL, 1, 8)
Ct = Counter.clone("Ct")
Tm = Timer.clone("Tm")

BOOL_INPUTS = (En, A, P1, P2, P3, E1, E2, E3, DoStage)
ALL_INPUTS = (*BOOL_INPUTS, Stage)


class Wire:
    """How a box's pins take the shared parent: one object, or copied contacts."""

    def __init__(self, shared: bool, parent: Callable[[], list]):
        self.shared = shared
        self._parent = parent
        self._p = And(*parent()) if shared else None

    def enable(self, *own) -> tuple:
        return (self._p, *own) if self.shared else (*self._parent(), *own)

    def pin(self, own):
        return And(self._p, own) if self.shared else And(*self._parent(), own)


@dataclass(frozen=True)
class Kind:
    name: str
    build: Callable[[Wire], None]
    parent: Callable[[], list]
    watch: tuple[Tag, ...]


def _count_up(w: Wire) -> None:
    with Rung(*w.enable(En)):
        count_up(Ct, 4).down(w.pin(P1)).reset(w.pin(P2))
    with Rung(Ct.Done):
        out(Out)


def _count_down(w: Wire) -> None:
    with Rung(*w.enable(En)):
        count_down(Ct, -3).reset(w.pin(P2))
    with Rung(Ct.Done):
        out(Out)


def _on_delay(w: Wire) -> None:
    with Rung(*w.enable(En)):
        on_delay(Tm, 30).reset(w.pin(P2))
    with Rung(Tm.Done):
        out(Out)


def _shift(w: Wire) -> None:
    with Rung(*w.enable(En)):
        shift(S.select(1, 4)).clock(w.pin(P1)).reset(w.pin(P2))


def _drum(w: Wire, *, jump_to: int = 2) -> None:
    with Rung(DoStage):
        copy(Stage, Step)
    with Rung(*w.enable(En)):
        event_drum(
            outputs=[Y1, Y2],
            events=[E1, E2, E3],
            pattern=[[1, 0], [0, 1], [1, 1]],
            current_step=Step,
            completion_flag=Flag,
        ).reset(w.pin(P1)).jump(w.pin(P2), step=jump_to).jog(w.pin(P3))


_COUNTER_WATCH = (Ct.Done, Ct.Acc, Out)
_TIMER_WATCH = (Tm.Done, Tm.Acc, Out)
_SHIFT_WATCH = tuple(S[i] for i in range(1, 5))
_DRUM_WATCH = (Step, Flag, Y1, Y2)

KINDS = [
    Kind("up_done", _count_up, lambda: [~Ct.Done], _COUNTER_WATCH),
    Kind("up_acc", _count_up, lambda: [Or(Ct.Acc < 3, A)], _COUNTER_WATCH),
    Kind("up_rise", _count_up, lambda: [rise(A)], _COUNTER_WATCH),
    Kind("up_fall_done", _count_up, lambda: [fall(A), ~Ct.Done], _COUNTER_WATCH),
    Kind("down_done", _count_down, lambda: [~Ct.Done], _COUNTER_WATCH),
    Kind("down_acc", _count_down, lambda: [Ct.Acc > -2], _COUNTER_WATCH),
    Kind("down_rise", _count_down, lambda: [rise(A), ~Ct.Done], _COUNTER_WATCH),
    Kind("timer_done", _on_delay, lambda: [~Tm.Done], _TIMER_WATCH),
    Kind("timer_acc", _on_delay, lambda: [Or(Tm.Acc < 20, A)], _TIMER_WATCH),
    Kind("timer_rise", _on_delay, lambda: [rise(A)], _TIMER_WATCH),
    Kind("shift_bit", _shift, lambda: [~S[1]], _SHIFT_WATCH),
    Kind("shift_or", _shift, lambda: [Or(S[3], A)], _SHIFT_WATCH),
    Kind("shift_fall", _shift, lambda: [fall(A), ~S[4]], _SHIFT_WATCH),
    Kind("drum_step", _drum, lambda: [Step < 3], _DRUM_WATCH),
    Kind("drum_flag", _drum, lambda: [~Flag], _DRUM_WATCH),
    Kind("drum_step_or_a", _drum, lambda: [Or(Step == 2, A)], _DRUM_WATCH),
    Kind("drum_invalid", _drum, lambda: [Or(Step > 3, Step < 1, A)], _DRUM_WATCH),
    Kind("drum_rise", _drum, lambda: [rise(A), Step != 3], _DRUM_WATCH),
]


def make_program(kind: Kind, *, shared: bool) -> Program:
    with Program(strict=False) as logic:
        kind.build(Wire(shared, kind.parent))
    return logic


def schedule(seed: int, scans: int = SCANS) -> list[dict[str, bool | int]]:
    rng = random.Random(seed)
    rows: list[dict[str, bool | int]] = []
    for _ in range(scans):
        row: dict[str, bool | int] = {t.name: rng.random() < 0.4 for t in BOOL_INPUTS}
        row["En"] = rng.random() < 0.75
        row["DoStage"] = rng.random() < 0.08
        row["Stage"] = rng.randint(0, 5)
        rows.append(row)
    return rows


def trace_core(
    program: Program, backend: str, rows: list[dict], watch: tuple[Tag, ...]
) -> list[tuple]:
    plc = (PLC if backend == "interpreted" else CompiledPLC)(program, dt=DT)
    seen = []
    for row in rows:
        plc.patch(row)
        plc.step()
        tags = plc.current_state.tags
        seen.append(tuple(tags.get(t.name, 0) for t in watch))
    return seen


def trace_circuitpy(
    program: Program, monkeypatch, rows: list[dict], watch: tuple[Tag, ...]
) -> list[tuple]:
    hw = _hw()
    result = generate_circuitpy(
        program,
        hw,
        target_scan_ms=10.0,
        runstop=RunStopConfig(debounce_ms=0, run_when_high=False),
    )
    ctx = _context_for_program(program, hw)
    in_syms = {t.name: ctx.symbol_for_tag(t) for t in ALL_INPUTS if t.name in ctx.referenced_tags}
    watch_syms = [ctx.symbol_for_tag(t) for t in watch]
    drive = [{in_syms[k]: v for k, v in row.items() if k in in_syms} for row in rows]
    hook = (
        f"_ROWS = {drive!r}\n_TRACE = []\nwhile True:\n"
        "    globals().update(_ROWS[__scan_once])\n"
        "    if __scan_once:\n"
        f"        _TRACE.append(tuple(eval(_s) for _s in {watch_syms!r}))\n"
    )
    code = result.code.replace("while True:\n", hook, 1)
    _BoardClock(5_000_000_000).install(monkeypatch)
    namespace = _run_single_scan_source(
        code, monkeypatch, _StubBase(), runtime_source=result.runtime, scans=len(rows)
    )
    trace = list(namespace["_TRACE"])
    trace.append(tuple(eval(s, namespace) for s in watch_syms))
    return trace


@pytest.mark.parametrize("kind", KINDS, ids=lambda k: k.name)
@pytest.mark.parametrize("backend", ["interpreted", "compiled"])
def test_shared_parent_equals_rail_form(kind: Kind, backend: str) -> None:
    shared = make_program(kind, shared=True)
    rail = make_program(kind, shared=False)
    moved = False
    for seed in SEEDS:
        rows = schedule(seed)
        got = trace_core(shared, backend, rows, kind.watch)
        want = trace_core(rail, backend, rows, kind.watch)
        assert got == want, f"seed {seed}"
        moved = moved or len(set(got)) > 1
    assert moved, "the schedule never moved the box: the comparison asserts nothing"


@pytest.mark.parametrize("kind", KINDS, ids=lambda k: k.name)
def test_shared_parent_equals_rail_form_on_circuitpy(kind: Kind, monkeypatch) -> None:
    rows = schedule(SEEDS[0])
    got = trace_circuitpy(make_program(kind, shared=True), monkeypatch, rows, kind.watch)
    want = trace_circuitpy(make_program(kind, shared=False), monkeypatch, rows, kind.watch)
    assert got == want
    # And the board's code agrees with the core on the same schedule.
    core = trace_core(make_program(kind, shared=True), "interpreted", rows, kind.watch)
    assert got == core


# -- invalid step: pins see the value the enable saw ---------------------------


def _drum_jump_to_3(w: Wire) -> None:
    _drum(w, jump_to=3)


_INVALID = Kind("invalid", _drum_jump_to_3, lambda: [Step > 3], _DRUM_WATCH)


def _invalid_rows(pin: str) -> list[dict]:
    idle = {t.name: False for t in BOOL_INPUTS} | {"Stage": 0}
    return [
        idle | {"DoStage": True, "Stage": 7},  # the step register holds 7 of 3
        idle | {"En": True, pin: True},  # the drum runs, correcting 7 to 1
        idle | {"En": True},
    ]


# The step after scan 2 when the pin saw the parent (Step > 3) on: a jog from
# the corrected step 1 reaches 2, a jump reaches 3.  A pin read after the
# correction sees step 1, no parent, and leaves the step at 1.
_PIN_RESULT = {"P2": 3, "P3": 2}


@pytest.mark.parametrize("pin", sorted(_PIN_RESULT))
@pytest.mark.parametrize("shared", [True, False], ids=["shared", "rail"])
@pytest.mark.parametrize("backend", ["interpreted", "compiled", "circuitpy"])
def test_pin_sees_the_step_the_enable_saw(pin: str, shared: bool, backend: str, monkeypatch):
    program = make_program(_INVALID, shared=shared)
    rows = _invalid_rows(pin)
    if backend == "circuitpy":
        seen = trace_circuitpy(program, monkeypatch, rows, _INVALID.watch)
    else:
        seen = trace_core(program, backend, rows, _INVALID.watch)
    assert seen[0][0] == 7  # staged, the drum not yet enabled
    assert seen[1][0] == _PIN_RESULT[pin], seen


def test_invalid_step_with_reset_pin_matches_in_both_forms() -> None:
    rows = _invalid_rows("P1")
    got = [
        trace_core(make_program(_INVALID, shared=s), "interpreted", rows, _DRUM_WATCH)
        for s in (True, False)
    ]
    assert got[0] == got[1]
    assert got[0][1][0] == 1
