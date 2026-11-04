"""A timer counts whole time exactly, whichever unit it counts in.

At a fixed dt, the scan a timer is Done on is ``ceil(delay / dt)`` for every unit, on
the interpreter, the kernel and the generated code, and a long run lands on what stepping
lands on.  The carry is whole nanoseconds (``time_mode.dt_to_ns``); these tables are
what its arithmetic has to give, written from the rule and not from the code.

Scans are numbered from 1, the first scan the rung is held (or, off-delay, the first it
is false).  The generated code's first scan has no time behind it (its dt is the clock
since boot, 0 here), so a count that starts on it lands one scan later.  A time drum's step
is a scan later everywhere: the scan a drum's rung comes back takes no time, and that is the
same first scan the generated code has none for.
"""

from __future__ import annotations

import math
import time
from fractions import Fraction
from typing import Any

import pytest

from pyrung.circuitpy import generate_circuitpy
from pyrung.core import (
    PLC,
    Bool,
    Dint,
    Int,
    Program,
    Rung,
    Timer,
    calc,
    compiled_run,
    copy,
    debounce,
    flasher,
    latch,
    off_delay,
    on_delay,
    scan_trace,
    time_drum,
    timed,
)
from pyrung.core.compiled_plc import CompiledPLC
from pyrung.core.site_memory import remap_site_memory
from pyrung.core.state import SystemState
from pyrung.core.time_mode import TimeMode, TimeUnit, dt_to_ns
from tests.circuitpy.test_codegen import _context_for_program, _run_single_scan_source
from tests.circuitpy.test_scan_time_and_stop import _BoardClock, _hw
from tests.circuitpy.test_system_points import _StubBase
from tests.core.scan_loop_counts import NEVER

DTS = ("0.001", "0.003", "0.01", "0.02", "0.05", "0.1", "0.2", "0.25", "0.5")
UNIT_SECONDS = {
    "Tms": Fraction(1, 1000),
    "Ts": Fraction(1),
    "Tm": Fraction(60),
    "Th": Fraction(3600),
    "Td": Fraction(86400),
}
PRESETS = (
    (1000, "Tms"),
    (5000, "Tms"),
    (30000, "Tms"),
    (1, "Ts"),
    (3, "Ts"),
    (33, "Ts"),
    (60, "Ts"),
    (1, "Tm"),
    (1, "Th"),
    (1, "Td"),
)
# Stepping past this many scans is what the long-run tests below are for.
MAX_SCANS = 6100


def want(preset: int, unit: str, dt: str) -> int:
    return math.ceil(preset * UNIT_SECONDS[unit] / Fraction(dt))


def want_of(kind: str, preset: int, unit: str, dt: str) -> int:
    return want(preset, unit, dt) + (1 if kind == "drum" else 0)


CROSS = [
    (preset, unit, dt)
    for preset, unit in PRESETS
    for dt in DTS
    if want(preset, unit, dt) <= MAX_SCANS
]


def _ids(row: tuple[int, str, str]) -> str:
    return f"{row[0]}{row[1]}-dt{row[2]}"


# --- The programs: each records, itself, the scan its timer ended on ----------------


def _on_delay_program(preset: int, unit: str) -> Program:
    Ton = Timer.clone("Ton")
    Scans = Dint("Scans", retentive=False)
    First = Dint("First", retentive=False)
    Seen = Bool("Seen", retentive=False)
    with Program(strict=False) as prog:
        with Rung():
            calc(Scans + 1, Scans)
        with Rung():
            on_delay(Ton, preset=preset, unit=unit)
        with Rung(Ton.Done, ~Seen):
            copy(Scans, First)
            latch(Seen)
    return prog


def _off_delay_program(preset: int, unit: str) -> Program:
    """The rung is true on scan 0, false from scan 1 on."""
    Tof = Timer.clone("Tof")
    Scans = Dint("Scans", retentive=False)
    First = Dint("First", retentive=False)
    Seen = Bool("Seen", retentive=False)
    Armed = Bool("Armed", retentive=False)
    with Program(strict=False) as prog:
        with Rung(~Armed):
            off_delay(Tof, preset=preset, unit=unit)
        with Rung(Armed):
            calc(Scans + 1, Scans)
        with Rung(Armed, ~Tof.Done, ~Seen):
            copy(Scans, First)
            latch(Seen)
        with Rung():
            latch(Armed)
    return prog


def _drum_program(preset: int, unit: str) -> Program:
    Scans = Dint("Scans", retentive=False)
    First = Dint("First", retentive=False)
    Seen = Bool("Seen", retentive=False)
    Step = Int("Step", retentive=False, default=1)
    Acc = Int("Acc", retentive=False)
    Flag = Bool("Flag", retentive=False)
    Reset = Bool("Reset", retentive=False)
    Out1 = Bool("Out1", retentive=False)
    Out2 = Bool("Out2", retentive=False)
    with Program(strict=False) as prog:
        with Rung():
            calc(Scans + 1, Scans)
        with Rung():
            time_drum(
                outputs=[Out1, Out2],
                presets=[preset, 32000],
                unit=unit,
                pattern=[[1, 0], [0, 1]],
                current_step=Step,
                accumulator=Acc,
                completion_flag=Flag,
            ).reset(Reset)
        with Rung(Step == 2, ~Seen):
            copy(Scans, First)
            latch(Seen)
    return prog


PROGRAMS = {"on": _on_delay_program, "off": _off_delay_program, "drum": _drum_program}


# --- The engines ----------------------------------------------------------------------


def _step_interpreter(prog: Program, dt: float, scans: int) -> int:
    plc = PLC(prog, dt=dt)
    for _ in range(scans):
        plc.step()
    return plc.current_state.tags["First"]


def _step_kernel(prog: Program, dt: float, scans: int) -> int:
    plc = CompiledPLC(prog, dt=dt)
    for _ in range(scans):
        plc.step()
    return plc.current_state.tags["First"]


def _run_generated(prog: Program, step_ns: int, scans: int) -> Any:
    hw = _hw()
    result = generate_circuitpy(prog, hw, target_scan_ms=step_ns / 1e6, runstop=None)
    monkeypatch = pytest.MonkeyPatch()
    try:
        _BoardClock(5_000_000_000, step_ns).install(monkeypatch)
        namespace = _run_single_scan_source(
            result.code,
            monkeypatch,
            _StubBase(),
            runtime_source=result.runtime,
            scans=scans,
        )
        return namespace, _context_for_program(prog, hw)
    finally:
        monkeypatch.undo()


def _generated_first(prog: Program, dt: str, scans: int) -> int:
    namespace, ctx = _run_generated(prog, int(Fraction(dt) * 10**9), scans)
    return namespace[ctx.symbol_for_tag(ctx.referenced_tags["First"])]


# --- Done lands on ceil(delay / dt), on every engine ----------------------------------


@pytest.mark.parametrize("kind", ["on", "off", "drum"])
@pytest.mark.parametrize("row", CROSS, ids=_ids)
def test_the_interpreter_ends_on_ceil(kind: str, row: tuple[int, str, str]) -> None:
    preset, unit, dt = row
    expect = want_of(kind, preset, unit, dt)
    assert _step_interpreter(PROGRAMS[kind](preset, unit), float(dt), expect + 3) == expect


@pytest.mark.parametrize("kind", ["on", "off", "drum"])
@pytest.mark.parametrize("row", CROSS, ids=_ids)
def test_the_kernel_ends_on_ceil(kind: str, row: tuple[int, str, str]) -> None:
    preset, unit, dt = row
    expect = want_of(kind, preset, unit, dt)
    assert _step_kernel(PROGRAMS[kind](preset, unit), float(dt), expect + 3) == expect


@pytest.mark.parametrize("kind", ["on", "off", "drum"])
@pytest.mark.parametrize("row", CROSS, ids=_ids)
def test_the_generated_code_ends_on_ceil(kind: str, row: tuple[int, str, str]) -> None:
    preset, unit, dt = row
    expect = want_of(kind, preset, unit, dt)
    # One later: its first scan has no time behind it (module docstring).  The off-delay's
    # first scan is the one its rung is true on, so it times all of them; a drum takes no time
    # on that scan in any engine.
    late = 1 if kind == "on" else 0
    assert _generated_first(PROGRAMS[kind](preset, unit), dt, expect + 4) == expect + late


# --- A long run lands where stepping lands, in the guarded loop or out of it ------------


def _in_the_long_run(plc: PLC, scans: int) -> int:
    """Run *scans* scans the way a long run does; return how many ran in the guarded loop."""
    before = scan_trace.TOTALS["loop scans"]
    plc.run_until(NEVER, max_cycles=scans)
    return scan_trace.TOTALS["loop scans"] - before


def _stepped(prog: Program, dt: float, scans: int) -> PLC:
    plc = PLC(prog, dt=dt)
    for _ in range(scans):
        plc.step()
    return plc


def _assert_same(ran: PLC, stepped: PLC) -> None:
    assert ran.current_state.scan_id == stepped.current_state.scan_id
    assert dict(ran.current_state.tags) == dict(stepped.current_state.tags)
    assert dict(ran.current_state.memory) == dict(stepped.current_state.memory)


@pytest.mark.usefixtures("loop_from_first_scan")
@pytest.mark.parametrize("kind", ["on", "off", "drum"])
@pytest.mark.parametrize("row", CROSS, ids=_ids)
def test_a_long_run_equals_stepping(kind: str, row: tuple[int, str, str]) -> None:
    """Whole state and scan count, in the loop, across the scan the timer ends on."""
    preset, unit, dt = row
    expect = want_of(kind, preset, unit, dt)
    scans = expect + 60
    prog = PROGRAMS[kind](preset, unit)
    ran = PLC(prog, dt=float(dt))
    assert _in_the_long_run(ran, scans) > 0
    _assert_same(ran, _stepped(prog, float(dt), scans))
    assert ran.current_state.tags["First"] == expect


@pytest.mark.parametrize("kind", ["on", "off", "drum"])
@pytest.mark.parametrize(
    ("preset", "unit", "dt"),
    [
        (500, "Ts", "0.1"),
        (1, "Tm", "0.01"),
        (20, "Tm", "0.25"),
        (30000, "Tms", "0.01"),
        (5000, "Tms", "0.001"),
        (15, "Ts", "0.003"),
    ],
)
def test_run_for_past_the_interpreted_prefix_equals_stepping(
    kind: str, preset: int, unit: str, dt: str
) -> None:
    """At the production settings, the timer ending well past the interpreted prefix."""
    expect = want_of(kind, preset, unit, dt)
    assert expect > compiled_run.INTERPRETED_PREFIX + scan_trace.MIN_RUN_SCANS
    prog = PROGRAMS[kind](preset, unit)
    ran = PLC(prog, dt=float(dt))
    before = scan_trace.TOTALS["loop scans"]
    ran.run_for((expect + 1200) * float(dt))
    assert scan_trace.TOTALS["loop scans"] - before > 0
    _assert_same(ran, _stepped(prog, float(dt), ran.current_state.scan_id))
    assert ran.current_state.tags["First"] == expect


# --- Acc: the whole units in the whole sum, on every scan ------------------------------


@pytest.mark.parametrize(
    ("unit", "dt"),
    [(u, d) for u in ("Tms", "Ts", "Tm") for d in ("0.003", "0.1", "0.25")],
)
def test_acc_is_the_whole_units_elapsed_on_every_scan(unit: str, dt: str) -> None:
    prog = _on_delay_program(32000, unit)
    interpreter, kernel = PLC(prog, dt=float(dt)), CompiledPLC(prog, dt=float(dt))
    per_unit = UNIT_SECONDS[unit]
    for scan in range(1, 3001):
        interpreter.step()
        kernel.step()
        expected = min(int(scan * Fraction(dt) / per_unit), 32767)
        assert interpreter.current_state.tags["Ton_Acc"] == expected, scan
        assert kernel.current_state.tags["Ton_Acc"] == expected, scan


# --- Every unit at every dt, by way of the last few scans -------------------------------


@pytest.mark.parametrize("unit", ["Tms", "Ts", "Tm", "Th", "Td"])
@pytest.mark.parametrize("dt", DTS)
def test_a_unit_too_long_to_step_ends_where_the_carry_says(unit: str, dt: str) -> None:
    """A carry staged 40 scans short of the unit: both engines cross on the 40th."""
    dt_ns = int(Fraction(dt) * 10**9)
    unit_ns = int(UNIT_SECONDS[unit] * 10**9)
    if unit_ns <= 40 * dt_ns:
        pytest.skip("the unit is shorter than the staged run")
    prog = _on_delay_program(1, unit)
    state = SystemState().with_memory({"_frac:Ton_Acc": unit_ns - 40 * dt_ns})
    for engine in (PLC, CompiledPLC):
        plc: Any = engine(prog, initial_state=state, dt=float(dt))
        for _ in range(39):
            plc.step()
        assert not plc.current_state.tags["Ton_Done"]
        plc.step()
        assert plc.current_state.tags["Ton_Done"]
        assert plc.current_state.tags["Ton_Acc"] == 1
        assert plc.current_state.memory["_frac:Ton_Acc"] == 0


# --- The generated code: the clock's own integers, no float on the way ------------------


def test_generated_code_sums_uneven_scans_with_no_loss() -> None:
    """A board's dt is whatever the clock read: the units are the whole sum's."""
    prog = _on_delay_program(32000, "Tms")
    hw = _hw()
    result = generate_circuitpy(prog, hw, target_scan_ms=10.0, runstop=None)

    class Uneven(_BoardClock):
        """Whole ticks of 1/32768 s, a SAMD51's, in truncated ns; never the same twice."""

        ticks = 0

        def sleep(self, _seconds: float) -> None:
            self.ticks += 300 + (self.ticks % 17)
            self.now_ns = 5_000_000_000 + self.ticks * 1_000_000_000 // 32768

    clock = Uneven(5_000_000_000)
    monkeypatch = pytest.MonkeyPatch()
    try:
        clock.install(monkeypatch)
        namespace = _run_single_scan_source(
            result.code, monkeypatch, _StubBase(), runtime_source=result.runtime, scans=3000
        )
    finally:
        monkeypatch.undo()
    ctx = _context_for_program(prog, hw)
    acc = ctx.referenced_tags["Ton_Acc"]
    # The last scan timed up to the clock's reading before its own sleep.
    timed_ns = namespace["_last_scan_ns"] - 5_000_000_000
    assert namespace[ctx.symbol_for_tag(acc)] == timed_ns // 1_000_000
    assert namespace["_mem"]["_frac:Ton_Acc"] == timed_ns % 1_000_000


def test_generated_timer_ticks_are_integer_arithmetic() -> None:
    """A board's float cannot hold a day's fraction of 10 ms; no tick goes through one."""
    prog = _on_delay_program(1, "Td")
    code = generate_circuitpy(prog, _hw(), target_scan_ms=10.0, runstop=None).code
    assert "_dt_units" not in code
    assert "// 86400000000000" in code
    # A float is only what the board's scan-time points report.
    assert 'float(_mem.get("_dt"' not in code


# --- dt is whole ns when it can be -----------------------------------------------------


@pytest.mark.parametrize(
    ("dt", "ns"),
    [
        (0.0, 0),
        (0.1, 100_000_000),
        (0.003, 3_000_000),
        (0.0005, 500_000),
        (0.000123456, 123_456),
        (0.123456789, 123_456_789),
        (10, 10_000_000_000),
        (60.1, 60_100_000_000),
        (1 / 3, Fraction(10**9, 3)),
        (1 / 60, Fraction(10**9, 60)),
        (1 / 1024, Fraction(10**9, 1024)),
        (0.0123456789012345, 12_345_679),
        (2**-20, Fraction(10**9, 2**20)),
        (6e-10, Fraction(6e-10) * 10**9),
        (1.2345678e-6, Fraction(1.2345678e-6) * 10**9),
    ],
)
def test_dt_to_ns(dt: float, ns: int | Fraction) -> None:
    got = dt_to_ns(dt)
    assert got == ns
    assert type(got) is type(ns)


def test_a_dt_under_a_nanosecond_still_counts() -> None:
    ns = dt_to_ns(1e-10)
    assert 0 < ns < 1


@pytest.mark.parametrize("engine", [_step_interpreter, _step_kernel])
def test_a_dt_rounding_would_move_counts_exactly(engine: Any) -> None:
    """2**-20 s is 953.67 ns: 10 ms is scan 10486.  At 954 ns it would be 10483."""
    assert engine(_on_delay_program(10, "Tms"), 2**-20, 10_490) == 10_486


@pytest.mark.parametrize("engine", [PLC, CompiledPLC])
def test_a_dt_under_a_nanosecond_is_not_rounded_up_to_one(engine: Any) -> None:
    """6e-10 s is 0.6 ns: 5 ns short of a unit is 9 scans.  At 1 ns it would be 5."""
    state = SystemState().with_memory({"_frac:Ton_Acc": 1_000_000 - 5})
    plc: Any = engine(_on_delay_program(1, "Tms"), initial_state=state, dt=6e-10)
    for _ in range(8):
        plc.step()
    assert not plc.current_state.tags["Ton_Done"]
    plc.step()
    assert plc.current_state.tags["Ton_Done"]


def test_a_measured_dt_with_float_noise_is_whole_ns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clock read as a float, eleven days up: its noise is gone, and no time with it."""
    now = [1e6]

    def clock() -> float:
        now[0] += 0.01
        return now[0]

    monkeypatch.setattr(time, "perf_counter", clock)
    assert (1e6 + 0.01) - 1e6 != 0.01
    assert dt_to_ns((1e6 + 0.01) - 1e6) == 10_000_000
    plc = PLC(_on_delay_program(1, "Ts"), realtime=True)
    for _ in range(50):
        plc.step()
    carry = plc.current_state.memory["_frac:Ton_Acc"]
    assert type(carry) is int
    assert carry % 10_000_000 == 0


@pytest.mark.parametrize("unit", list(TimeUnit))
def test_each_unit_knows_its_length(unit: TimeUnit) -> None:
    assert unit.ns == round(1e9 / unit.dt_to_units(1.0))


# --- Non-decimal dt: every engine exact, and the loop declines it -----------------------


@pytest.mark.parametrize(
    ("dt", "scans_per_second"), [(1 / 3, 3), (1 / 60, 60), (1 / 1024, 1024), (1 / 7, 7)]
)
@pytest.mark.parametrize("engine", [_step_interpreter, _step_kernel])
def test_non_decimal_dt_is_exact(engine: Any, dt: float, scans_per_second: int) -> None:
    for preset, unit, seconds in ((1000, "Tms", 1), (3, "Ts", 3), (1, "Tm", 60)):
        expect = seconds * scans_per_second
        if expect > 10_000:
            continue
        assert engine(_on_delay_program(preset, unit), dt, expect + 2) == expect


@pytest.mark.parametrize("kind", ["on", "off", "drum"])
@pytest.mark.parametrize("dt", [1 / 3, 1 / 60, 1 / 1024, 2**-20])
def test_a_long_run_at_a_dt_of_no_whole_ns_declines_the_loop(kind: str, dt: float) -> None:
    """Past the prefix, at the production settings: not one scan in the loop, and exact."""
    preset, unit = (10, "Tms") if dt == 2**-20 else (3, "Ts")
    prog = PROGRAMS[kind](preset, unit)
    scans = compiled_run.INTERPRETED_PREFIX + 2500
    if dt == 2**-20:
        scans = 10_600
    ran = PLC(prog, dt=dt)
    assert _in_the_long_run(ran, scans) == 0
    _assert_same(ran, _stepped(prog, dt, scans))


@pytest.mark.usefixtures("loop_from_first_scan")
@pytest.mark.parametrize("dt", [1 / 3, 1 / 60])
def test_a_ratio_dt_stays_out_of_the_loop_however_short_the_run(dt: float) -> None:
    prog = _on_delay_program(3, "Ts")
    ran = PLC(prog, dt=dt)
    assert _in_the_long_run(ran, 400) == 0
    _assert_same(ran, _stepped(prog, dt, 400))


@pytest.mark.usefixtures("loop_from_first_scan")
@pytest.mark.parametrize("dt", [1 / 3, 1 / 60])
def test_a_ratio_dt_leaves_the_loop_when_a_timer_starts(dt: float) -> None:
    """Idle scans may run in the loop, with no carry yet to stop them.  A tick may not."""
    Ton = Timer.clone("Ton")
    Scans = Dint("Scans", retentive=False)
    with Program(strict=False) as prog:
        with Rung():
            calc(Scans + 1, Scans)
        with Rung(Scans > 100):
            on_delay(Ton, preset=3, unit="Ts")
    ran = PLC(prog, dt=dt)
    assert 0 < _in_the_long_run(ran, 400) <= 100
    _assert_same(ran, _stepped(prog, dt, 400))


def test_sub_millisecond_dt_keeps_the_fraction_of_a_millisecond() -> None:
    prog = _on_delay_program(10, "Tms")
    for dt, expect in ((0.0005, 20), (0.0001, 100), (0.00025, 40)):
        assert _step_interpreter(prog, dt, expect + 2) == expect
        assert _step_kernel(prog, dt, expect + 2) == expect


# --- The two engines hold the same memory ------------------------------------------------


@pytest.mark.parametrize("kind", ["on", "off", "drum"])
def test_interpreter_and_kernel_memory_agree_on_every_scan(kind: str) -> None:
    prog = PROGRAMS[kind](1, "Ts")
    interpreter, kernel = PLC(prog, dt=0.03), CompiledPLC(prog, dt=0.03)
    for _ in range(60):
        interpreter.step()
        kernel.step()
        for key, value in interpreter.current_state.memory.items():
            if key.startswith(("_frac:", "_dt")):
                assert kernel.current_state.memory[key] == value, key
                assert type(kernel.current_state.memory[key]) is type(value), key


# --- A carry survives whatever a state goes through ---------------------------------------


@pytest.mark.usefixtures("loop_from_first_scan")
def test_a_long_run_hands_on_the_carry_it_ends_with() -> None:
    prog = _on_delay_program(1, "Ts")
    plc = PLC(prog, dt=0.1)
    assert _in_the_long_run(plc, 5) > 0
    assert plc.current_state.memory["_frac:Ton_Acc"] == 500_000_000
    fork = plc.fork()
    assert _in_the_long_run(fork, 7) > 0
    assert fork.current_state.tags["Ton_Done"]
    assert fork.current_state.tags["First"] == 10
    _assert_same(fork, _stepped(prog, 0.1, 12))


def test_fork_and_history_keep_the_carry() -> None:
    prog = _on_delay_program(1, "Ts")
    plc = PLC(prog, dt=0.1)
    plc.run(5)
    assert plc.current_state.memory["_frac:Ton_Acc"] == 500_000_000
    fork = plc.fork()
    fork.run(5)
    assert fork.current_state.tags["Ton_Done"]
    assert fork.current_state.tags["First"] == 10
    plc.run(7)
    # An earlier scan reads back as it was, and a fork from it lands as stepping does.
    assert plc.history.at(3).memory["_frac:Ton_Acc"] == 300_000_000
    assert plc.current_state.tags["First"] == 10


@pytest.mark.parametrize(
    ("unit", "old", "ns"),
    [("Ts", 0.5, 500_000_000), ("Tm", 0.75, 45_000_000_000), ("Tms", 0.5, 500_000)],
)
@pytest.mark.parametrize("engine", ["interpreter", "kernel"])
def test_a_saved_float_fraction_still_counts(engine: str, unit: str, old: float, ns: int) -> None:
    """An old state's ``_frac`` is a float fraction of the unit: it reads as that much time."""
    preset = {"Ts": 1, "Tm": 1, "Tms": 1}[unit]
    prog = _on_delay_program(preset, unit)
    unit_ns = int(UNIT_SECONDS[unit] * 10**9)
    dt = (unit_ns // 2) / 1e9
    state = SystemState().with_memory({"_frac:Ton_Acc": old, "_dt": dt})
    plc: Any = (CompiledPLC if engine == "kernel" else PLC)(prog, initial_state=state, dt=dt)
    plc.step()
    assert plc.current_state.tags["Ton_Done"]
    assert plc.current_state.memory["_frac:Ton_Acc"] == (ns + unit_ns // 2) % unit_ns
    assert type(plc.current_state.memory["_frac:Ton_Acc"]) is int


def test_a_saved_fraction_of_no_timer_the_kernel_knows_reads_as_nothing() -> None:
    prog = _on_delay_program(1, "Ts")
    state = SystemState().with_memory({"_frac:Gone": 0.5})
    plc = CompiledPLC(prog, initial_state=state, dt=0.1)
    plc.step()
    assert plc.current_state.tags["Ton_Acc"] == 0
    assert plc.current_state.memory["_frac:Gone"] == 0


def test_changing_dt_mid_count_keeps_the_time() -> None:
    prog = _on_delay_program(1, "Ts")
    plc = PLC(prog, dt=0.1)
    plc.run(5)
    plc._set_time_mode(TimeMode.FIXED_STEP, dt=0.25)
    plc.run(1)
    assert plc.current_state.memory["_frac:Ton_Acc"] == 750_000_000
    plc.run(1)
    assert plc.current_state.tags["Ton_Acc"] == 1
    assert plc.current_state.memory["_frac:Ton_Acc"] == 0


@pytest.mark.usefixtures("loop_from_first_scan")
def test_a_long_run_after_a_dt_change_keeps_the_time() -> None:
    prog = _on_delay_program(1, "Ts")
    before = PLC(prog, dt=0.1)
    for _ in range(5):
        before.step()
    plc = PLC(prog, initial_state=before.current_state, dt=0.05)
    assert _in_the_long_run(plc, 9) > 0
    assert plc.current_state.tags["Ton_Acc"] == 0
    assert plc.current_state.memory["_frac:Ton_Acc"] == 950_000_000
    _in_the_long_run(plc, 1)
    assert plc.current_state.tags["Ton_Acc"] == 1
    assert plc.current_state.memory["_frac:Ton_Acc"] == 0


# --- A reload may change the unit under a carry ---------------------------------------------


@pytest.mark.parametrize(
    ("build", "keys"),
    [
        (lambda t, o, unit: on_delay(t, preset=5, unit=unit), ["_frac:Ton_Acc"]),
        (lambda t, o, unit: off_delay(t, preset=5, unit=unit), ["_frac:Ton_Acc"]),
        (
            lambda t, o, unit: debounce(Bool("B"), on=(t, 5), off=(o, 5), unit=unit),
            ["_frac:Ton_Acc", "_frac:Other_Acc"],
        ),
        (
            lambda t, o, unit: flasher(Bool("B"), on=(t, 5), off=(o, 5), unit=unit),
            ["_frac:Ton_Acc", "_frac:Other_Acc"],
        ),
        (lambda t, o, unit: timed(Bool("B"), t, 5, unit=unit), ["_frac:Ton_Acc"]),
        (
            lambda t, o, unit: time_drum(
                outputs=[Bool("B")],
                presets=[5],
                unit=unit,
                pattern=[[1]],
                current_step=Int("Step", default=1),
                accumulator=t.Acc,
                completion_flag=Bool("Flag"),
            ).reset(Bool("Reset")),
            ["_frac:Ton_Acc"],
        ),
    ],
    ids=["on_delay", "off_delay", "debounce", "flasher", "timed", "time_drum"],
)
def test_a_reload_cuts_each_carry_to_below_a_unit_of_its_timer(build: Any, keys: list[str]) -> None:
    def program(unit: str) -> Program:
        Ton, Other = Timer.clone("Ton"), Timer.clone("Other")
        with Program(strict=False) as prog:
            with Rung():
                build(Ton, Other, unit)
        return prog

    old, new = PLC(program("Tm"), dt=0.1), PLC(program("Tms"), dt=0.1)
    carries = {key: 59_000_000_000 for key in keys}
    carried, _ = remap_site_memory(carries, old._site_table, new._site_table)
    assert carried == dict.fromkeys(keys, 0)
    longer = dict.fromkeys(keys, 59_000_123_456)
    carried, _ = remap_site_memory(longer, old._site_table, new._site_table)
    assert carried == dict.fromkeys(keys, 123_456)
    kept, _ = remap_site_memory(longer, old._site_table, old._site_table)
    assert kept == longer


def test_a_reload_leaves_a_carry_it_finds_no_timer_for_and_an_old_float() -> None:
    prog = _on_delay_program(1, "Tms")
    table = PLC(prog, dt=0.1)._site_table
    memory = {"_frac:Gone": 59_000_000_000, "_frac:Ton_Acc": 0.75}
    assert remap_site_memory(memory, table, table)[0] == memory


# --- The v4 timed coils carry the same way -------------------------------------------------


def _coil_program(build: Any) -> Program:
    Scans = Dint("Scans", retentive=False)
    Target = Bool("Target", retentive=False)
    First = Dint("First", retentive=False)
    Seen = Bool("Seen", retentive=False)
    with Program(strict=False) as prog:
        with Rung():
            calc(Scans + 1, Scans)
        build(Target)
        with Rung(Target, ~Seen):
            copy(Scans, First)
            latch(Seen)
    return prog


@pytest.mark.parametrize("dt", DTS[2:])
@pytest.mark.parametrize("engine", [_step_interpreter, _step_kernel])
def test_debounce_turns_on_at_ceil_whichever_unit(engine: Any, dt: str) -> None:
    OnT, OffT = Timer.clone("OnT"), Timer.clone("OffT")

    def seconds(target: Any) -> None:
        with Rung():
            debounce(target, on=(OnT, 1), off=(OffT, 1), unit="Ts")

    def millis(target: Any) -> None:
        with Rung():
            debounce(target, on=(OnT, 1000), off=(OffT, 1000), unit="Tms")

    expect = want(1, "Ts", dt)
    assert engine(_coil_program(seconds), float(dt), expect + 2) == expect
    assert engine(_coil_program(millis), float(dt), expect + 2) == expect


@pytest.mark.parametrize("engine", [_step_interpreter, _step_kernel])
def test_timed_coil_drops_at_ceil_in_seconds(engine: Any) -> None:
    Pulse = Timer.clone("Pulse")
    Scans = Dint("Scans", retentive=False)
    Target = Bool("Target", retentive=False)
    First = Dint("First", retentive=False)
    Seen = Bool("Seen", retentive=False)
    with Program(strict=False) as prog:
        with Rung():
            calc(Scans + 1, Scans)
        with Rung():
            timed(Target, Pulse, 1, unit="Ts")
        with Rung(~Target, ~Seen):
            copy(Scans, First)
            latch(Seen)
    assert engine(prog, 0.1, 13) == 10


@pytest.mark.parametrize("engine", [_step_interpreter, _step_kernel])
def test_flasher_changes_phase_at_ceil_in_seconds(engine: Any) -> None:
    OnT, OffT = Timer.clone("OnT"), Timer.clone("OffT")
    Scans = Dint("Scans", retentive=False)
    Target = Bool("Target", retentive=False)
    First = Dint("First", retentive=False)
    Seen = Bool("Seen", retentive=False)
    with Program(strict=False) as prog:
        with Rung():
            calc(Scans + 1, Scans)
        with Rung():
            flasher(Target, on=(OnT, 1), off=(OffT, 1), unit="Ts")
        with Rung(~Target, ~Seen):
            copy(Scans, First)
            latch(Seen)
    assert engine(prog, 0.1, 13) == 10
