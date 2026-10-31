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
Never = Bool("Never")
Y1, Y2 = Bool("Y1"), Bool("Y2")
Step, Flag = Int("Step"), Bool("Flag")
S = Block("S", TagType.BOOL, 1, 8)
Ct = Counter.clone("Ct")
Tm = Timer.clone("Tm")

BOOL_INPUTS = (En, A, P1, P2, P3, E1, E2, E3, DoStage)
ALL_INPUTS = (*BOOL_INPUTS, Stage)


class Wire:
    """How a box's pins take the shared parent: one object, or copied contacts."""

    def __init__(self, shared: bool, parent: Callable[[], list], drop: str | None = None):
        self.shared = shared
        self._drop = drop
        self._parent = parent
        self._p = And(*parent()) if shared else None

    def enable(self, *own) -> tuple:
        return (self._p, *own) if self.shared else (*self._parent(), *own)

    def pin(self, own):
        if own.name == self._drop:  # the pin is never wired: an input nothing drives
            return Never
        return And(self._p, own) if self.shared else And(*self._parent(), own)


@dataclass(frozen=True)
class Kind:
    name: str
    build: Callable[[Wire], None]
    parent: Callable[[], list]
    watch: tuple[Tag, ...]
    pins: tuple[Tag, ...]  # each pin's own contact, which names the pin


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

_UP_PINS = (P1, P2)
_P2 = (P2,)
_DRUM_PINS = (P1, P2, P3)

KINDS = [
    Kind("up_done", _count_up, lambda: [~Ct.Done], _COUNTER_WATCH, _UP_PINS),
    Kind("up_acc", _count_up, lambda: [Or(Ct.Acc < 3, A)], _COUNTER_WATCH, _UP_PINS),
    Kind("up_rise", _count_up, lambda: [rise(A)], _COUNTER_WATCH, _UP_PINS),
    Kind("up_fall_done", _count_up, lambda: [fall(A), ~Ct.Done], _COUNTER_WATCH, _UP_PINS),
    Kind("down_done", _count_down, lambda: [~Ct.Done], _COUNTER_WATCH, _P2),
    Kind("down_acc", _count_down, lambda: [Ct.Acc > -2], _COUNTER_WATCH, _P2),
    Kind("down_rise", _count_down, lambda: [rise(A), ~Ct.Done], _COUNTER_WATCH, _P2),
    Kind("timer_done", _on_delay, lambda: [~Tm.Done], _TIMER_WATCH, _P2),
    Kind("timer_acc", _on_delay, lambda: [Or(Tm.Acc < 20, A)], _TIMER_WATCH, _P2),
    Kind("timer_rise", _on_delay, lambda: [rise(A)], _TIMER_WATCH, _P2),
    Kind("shift_bit", _shift, lambda: [~S[1]], _SHIFT_WATCH, _UP_PINS),
    Kind("shift_or", _shift, lambda: [Or(S[3], A)], _SHIFT_WATCH, _UP_PINS),
    Kind("shift_fall", _shift, lambda: [fall(A), ~S[4]], _SHIFT_WATCH, _UP_PINS),
    Kind("drum_step", _drum, lambda: [Step < 3], _DRUM_WATCH, _DRUM_PINS),
    Kind("drum_flag", _drum, lambda: [~Flag], _DRUM_WATCH, _DRUM_PINS),
    Kind("drum_step_or_a", _drum, lambda: [Or(Step == 2, A)], _DRUM_WATCH, _DRUM_PINS),
    Kind("drum_invalid", _drum, lambda: [Or(Step > 3, Step < 1, A)], _DRUM_WATCH, _DRUM_PINS),
    Kind("drum_rise", _drum, lambda: [rise(A), Step != 3], _DRUM_WATCH, _DRUM_PINS),
]


def make_program(kind: Kind, *, shared: bool, drop: Tag | None = None) -> Program:
    with Program(strict=False) as logic:
        kind.build(Wire(shared, kind.parent, drop.name if drop is not None else None))
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
    for seed in SEEDS:
        rows = schedule(seed)
        got = trace_core(shared, backend, rows, kind.watch)
        want = trace_core(rail, backend, rows, kind.watch)
        assert got == want, f"seed {seed}"
    # Every pin did something: the box differs with that pin never wired.
    for pin in kind.pins:
        bare = make_program(kind, shared=True, drop=pin)
        assert any(
            trace_core(bare, backend, schedule(seed), kind.watch)
            != trace_core(shared, backend, schedule(seed), kind.watch)
            for seed in SEEDS
        ), f"the schedule never made pin {pin.name} act"


@pytest.mark.parametrize("kind", KINDS, ids=lambda k: k.name)
def test_shared_parent_equals_rail_form_on_circuitpy(kind: Kind, monkeypatch) -> None:
    rows = schedule(SEEDS[0])
    got = trace_circuitpy(make_program(kind, shared=True), monkeypatch, rows, kind.watch)
    want = trace_circuitpy(make_program(kind, shared=False), monkeypatch, rows, kind.watch)
    assert got == want
    # And the board's code agrees with the core on the same schedule.
    core = trace_core(make_program(kind, shared=True), "interpreted", rows, kind.watch)
    assert got == core
    for pin in kind.pins:
        bare = trace_circuitpy(
            make_program(kind, shared=True, drop=pin), monkeypatch, rows, kind.watch
        )
        assert bare != got, f"the schedule never made pin {pin.name} act"


# -- invalid step: pins see the value the enable saw ---------------------------


def _drum_jump_to_3(w: Wire) -> None:
    _drum(w, jump_to=3)


_INVALID = Kind("invalid", _drum_jump_to_3, lambda: [Step > 3], _DRUM_WATCH, _DRUM_PINS)


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


# -- absolute traces: the box reads every pin before it writes ----------------
#
# The equivalence tests above compare two wirings of one runtime, so a runtime
# that reads every pin after the box wrote reads late in both and still passes.
# These cases pin the values a late read would change.  Each parent reads state
# the box itself writes that scan; "early" is pyrung's adopted model, "late"
# what a pin read after the box's write would leave.
#
# Not discriminating, so not covered: a shift register's clock (the shift is
# conditioned on the clock itself, so no write of the box precedes its read), a
# drum's completion flag (written only by the event that ends the drum, which a
# step parent already covers) and a counter's up input (the enable is read
# before anything).


def _up2(w: Wire) -> None:
    with Rung(*w.enable(En)):
        count_up(Ct, 2).down(w.pin(P1)).reset(w.pin(P2))


def _down2(w: Wire) -> None:
    with Rung(*w.enable(En)):
        count_down(Ct, -2).reset(w.pin(P2))


def _drum_events(w: Wire) -> None:
    _drum(w, jump_to=3)


def _idle(**on: bool | int) -> dict:
    return {t.name: False for t in BOOL_INPUTS} | {"Stage": 0} | on


_CT = (Ct.Done, Ct.Acc)
_not_done = lambda: [~Ct.Done]  # noqa: E731
_step_is_1 = lambda: [Step == 1]  # noqa: E731
# The drum case: stage step 1, run a scan, then the step's event rises together
# with the pin.  The event advances 1 -> 2 before the pin acts.
_DRUM_ROWS = [_idle(DoStage=True, Stage=1), _idle(En=True)]

ABSOLUTE = {
    # Up and down pulse together on the scan the second count turns Done on:
    # both see ~Done and cancel (early); a down read after the count sees Done.
    "up_down_cancel": (
        _up2,
        _not_done,
        _CT,
        (P1,),
        [_idle(En=True), _idle(), _idle(En=True, P1=True)],
        (False, 1),
    ),
    # Reset on that scan beats the count (early); read late it sees Done on.
    "up_reset_beats_count": (
        _up2,
        _not_done,
        _CT,
        (P2,),
        [_idle(En=True), _idle(), _idle(En=True, P2=True)],
        (False, 0),
    ),
    "down_reset_beats_count": (
        _down2,
        _not_done,
        _CT,
        (P2,),
        [_idle(En=True), _idle(), _idle(En=True, P2=True)],
        (False, 0),
    ),
    # Shift: reset reads ~S1 before the clock shifts a 1 into S1 on the same scan.
    "shift_reset_beats_clock": (
        _shift,
        lambda: [~S[1]],
        _SHIFT_WATCH,
        (P2,),
        [_idle(En=True, P1=True, P2=True)],
        (False, False, False, False),
    ),
    # Drum: the event steps 1 -> 2 on the scan the pin acts.  Reset reads
    # Step == 1 early and wins (step 1); a jog/jump reads it early too and
    # acts after the event (3).  Read after the event the step is 2, the parent
    # off, and the pin does nothing (2).
    "drum_reset_beats_event": (
        _drum_events,
        _step_is_1,
        _DRUM_WATCH,
        (P1,),
        [*_DRUM_ROWS, _idle(En=True, E1=True, P1=True)],
        (1, False, False, False),
    ),
    "drum_jog_after_event": (
        _drum_events,
        _step_is_1,
        _DRUM_WATCH,
        (P3,),
        [*_DRUM_ROWS, _idle(En=True, E1=True, P3=True)],
        (3, False, True, True),
    ),
    "drum_jump_after_event": (
        _drum_events,
        _step_is_1,
        _DRUM_WATCH,
        (P2,),
        [*_DRUM_ROWS, _idle(En=True, E1=True, P2=True)],
        (3, False, True, True),
    ),
}


def _absolute_trace(
    name: str, shared: bool, backend: str, monkeypatch, *, drop: bool = False
) -> tuple[list[tuple], tuple]:
    build, parent, watch, pins, rows, expect = ABSOLUTE[name]
    program = make_program(
        Kind(name, build, parent, watch, pins),
        shared=shared,
        drop=pins[0] if drop else None,
    )
    if backend == "circuitpy":
        seen = trace_circuitpy(program, monkeypatch, rows, watch)
    else:
        seen = trace_core(program, backend, rows, watch)
    return seen, expect


@pytest.mark.parametrize("name", list(ABSOLUTE))
@pytest.mark.parametrize("shared", [True, False], ids=["shared", "rail"])
@pytest.mark.parametrize("backend", ["interpreted", "compiled", "circuitpy"])
def test_pin_reads_precede_the_box_write(name: str, shared: bool, backend: str, monkeypatch):
    seen, expect = _absolute_trace(name, shared, backend, monkeypatch)
    assert seen[-1] == expect, seen
    # A pin read late sees its parent off and does nothing: the case's pin left
    # unwired.  The expectation must differ from that, or the case asserts nothing.
    bare, _ = _absolute_trace(name, shared, backend, monkeypatch, drop=True)
    assert bare[-1] != expect, f"{name} does not tell an early read from a late one"


def _timer_reset(w: Wire) -> None:
    with Rung(*w.enable(En)):
        on_delay(Tm, 30).reset(w.pin(P2))


@pytest.mark.parametrize("shared", [True, False], ids=["shared", "rail"])
@pytest.mark.parametrize("backend", ["interpreted", "compiled", "circuitpy"])
def test_timer_reset_reads_done_before_the_timer_writes_it(shared, backend, monkeypatch):
    """Reset on the scan the timer turns Done on beats it (parent ~Done read early)."""
    kind = Kind("timer", _timer_reset, _not_done_timer, _TIMER_WATCH[:2], _P2)
    # The scan Done first comes on with no reset, on the interpreter.
    free = trace_core(
        make_program(kind, shared=True), "interpreted", [_idle(En=True)] * 8, kind.watch
    )
    first = next(i for i, row in enumerate(free) if row[0])
    rows = [_idle(En=True)] * first + [_idle(En=True, P2=True)]
    program = make_program(kind, shared=shared)
    if backend == "circuitpy":
        seen = trace_circuitpy(program, monkeypatch, rows, kind.watch)
    else:
        seen = trace_core(program, backend, rows, kind.watch)
    assert seen[-1] == (False, 0), seen  # read late, Done is on and the reset is dropped


def _not_done_timer() -> list:
    return [~Tm.Done]
