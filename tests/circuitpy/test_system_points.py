"""System points in the generated scan loop: faults, scan counter, clocks, mode bits, RTC.

Each test runs the generated ``code.py`` for a few scans and checks what the
ladder read, against core's system-point runtime (``core/system_points.py``).
"""

from __future__ import annotations

import time

import pytest

from pyrung.circuitpy import P1AM, RunStopConfig, generate_circuitpy
from pyrung.core import Block, Bool, Int, Program, Real, Rung, TagType, calc, copy, out
from pyrung.core.expression import sqrt
from pyrung.core.system_points import system
from tests.circuitpy.test_codegen import _context_for_program, _run_single_scan_source


class _StubBase:
    def rollCall(self, modules):
        return None

    def readDiscrete(self, slot):
        return 0

    def writeDiscrete(self, value, slot):
        return None

    def readAnalog(self, slot, ch_num):
        return 0

    def writeAnalog(self, value, slot, ch_num):
        return None

    def readTemperature(self, slot, ch_num):
        return 0.0


def _hw() -> P1AM:
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    return hw


def _run(prog: Program, monkeypatch, *, scans: int, runstop: RunStopConfig | None = None):
    hw = _hw()
    result = generate_circuitpy(prog, hw, target_scan_ms=1.0, runstop=runstop)
    namespace = _run_single_scan_source(
        result.code, monkeypatch, _StubBase(), runtime_source=result.runtime, scans=scans
    )
    ctx = _context_for_program(prog, hw)
    return result.code, (lambda tag: namespace[ctx.symbol_for_tag(tag)])


Zero = Int("Zero", retentive=False)
Pointer = Int("Pointer", retentive=False)
Sink = Int("Sink", retentive=False)
Armed = Bool("Armed")
SeenAtTop = Int("SeenAtTop", retentive=False)
SeenAfter = Int("SeenAfter", retentive=False)
Table = Block("Table", TagType.INT, 1, 4, retentive=False)


def _divide_by_zero():
    calc(10 / Zero, Sink)


def _overflow():
    calc(Zero + 40000, Sink)


def _bad_pointer():
    copy(Table[Pointer], Sink)


@pytest.mark.parametrize(
    ("flag", "fault"),
    [
        (system.fault.division_error, _divide_by_zero),
        (system.fault.out_of_range, _overflow),
        (system.fault.address_error, _bad_pointer),
    ],
    ids=["SC40", "SC43", "SC44"],
)
def test_fault_flag_set_by_instruction_and_cleared_at_next_scan_start(monkeypatch, flag, fault):
    # Scan 0 faults; scan 1 does not.  The first rung counts the scans the flag
    # reads on before any instruction ran, the third the scans it reads on
    # after the faulting rung.
    with Program(strict=False) as prog:
        with Rung(flag):
            calc(SeenAtTop + 1, SeenAtTop)
        with Rung(~Armed):
            fault()
        with Rung(flag):
            calc(SeenAfter + 1, SeenAfter)
        with Rung():
            out(Armed)

    _code, read = _run(prog, monkeypatch, scans=3)

    assert read(SeenAfter) == 1
    assert read(SeenAtTop) == 0
    assert read(flag) is False


ScanCount = Int("ScanCount", retentive=False)
OddScans = Int("OddScans", retentive=False)
ClockMismatch = Int("ClockMismatch", retentive=False)
FirstScans = Int("FirstScans", retentive=False)


def test_scan_counter_steps_and_scan_clock_follows_its_parity(monkeypatch):
    counter = system.sys.scan_counter
    clock = system.sys.scan_clock_toggle
    with Program(strict=False) as prog:
        with Rung():
            copy(counter, ScanCount)
        with Rung(clock):
            calc(OddScans + 1, OddScans)
        with Rung(clock, counter % 2 == 0):
            calc(ClockMismatch + 1, ClockMismatch)
        with Rung(~clock, counter % 2 == 1):
            calc(ClockMismatch + 1, ClockMismatch)
        with Rung(system.sys.first_scan):
            calc(FirstScans + 1, FirstScans)

    _code, read = _run(prog, monkeypatch, scans=5)

    # Core: SD9 = scan_id (0 on the first scan), SC3 = SD9 is odd.
    assert read(ScanCount) == 4
    assert read(OddScans) == 2
    assert read(ClockMismatch) == 0
    assert read(FirstScans) == 1


SawMax = Int("SawMax", retentive=False)


def test_scan_counter_wraps_like_core(monkeypatch):
    # Core: SD9 = scan_id % 32768.
    counter = system.sys.scan_counter
    with Program(strict=False) as prog:
        with Rung(counter == 32767):
            copy(1, SawMax)
        with Rung():
            copy(counter, ScanCount)

    hw = _hw()
    result = generate_circuitpy(prog, hw, target_scan_ms=1.0, runstop=None)
    start = "\n_sys_scan_n = 0\n"
    assert start in result.code
    code = result.code.replace(start, "\n_sys_scan_n = 32766\n", 1)
    namespace = _run_single_scan_source(code, monkeypatch, _StubBase(), scans=3)
    ctx = _context_for_program(prog, hw)
    assert namespace[ctx.symbol_for_tag(SawMax)] == 1
    assert namespace[ctx.symbol_for_tag(ScanCount)] == 0


OnScans = Int("OnScans", retentive=False)


@pytest.mark.parametrize(
    ("clock", "step_ms", "scans", "on_scans"),
    [
        # Clock time = the time of the RUN scans before this one: 0, 0, 30, 60,
        # ..., 240 ms.  Half period 50 ms: on in [50, 100) and [150, 200).
        (system.sys.clock_100ms, 30, 10, 4),
        # 0, 0, 130, 260, 390, 520 ms.  Half period 250 ms: on in [250, 500).
        (system.sys.clock_500ms, 130, 6, 2),
    ],
    ids=["SC5", "SC6"],
)
def test_clock_bits_follow_wall_clock_with_named_full_period(
    monkeypatch, clock, step_ms, scans, on_scans
):
    # The board clock moves only while the loop sleeps out a scan.
    board = {"ns": 5_000_000_000, "sleeps": 0}

    def fake_sleep(_seconds):
        board["ns"] += step_ms * 1_000_000
        board["sleeps"] += 1

    monkeypatch.setattr(time, "monotonic_ns", lambda: board["ns"])
    monkeypatch.setattr(time, "sleep", fake_sleep)
    with Program(strict=False) as prog:
        with Rung(clock):
            calc(OnScans + 1, OnScans)

    _code, read = _run(prog, monkeypatch, scans=scans)

    assert board["sleeps"] == scans
    assert read(OnScans) == on_scans


FirstScanClockOff = Int("FirstScanClockOff", retentive=False)


def test_clock_bits_off_on_first_scan(monkeypatch):
    # Core's clocks read the time of the scans before this one: 0 on the first.
    clocks = (system.sys.clock_10ms, system.sys.clock_100ms, system.sys.clock_1s)
    with Program(strict=False) as prog:
        with Rung(system.sys.first_scan, *(~clock for clock in clocks)):
            copy(1, FirstScanClockOff)

    _code, read = _run(prog, monkeypatch, scans=1)

    assert read(FirstScanClockOff) == 1


SwitchOn = Int("SwitchOn", retentive=False)
ModeOn = Int("ModeOn", retentive=False)


@pytest.mark.parametrize(
    "runstop",
    [None, RunStopConfig(debounce_ms=0, run_when_high=False)],
    ids=["no-runstop", "switch-in-run"],
)
def test_mode_bits_on_in_run(monkeypatch, runstop):
    # The test board's switch reads low: RUN for run_when_high=False.
    with Program(strict=False) as prog:
        with Rung(system.sys.mode_switch_run):
            copy(1, SwitchOn)
        with Rung(system.sys.mode_run):
            copy(1, ModeOn)

    _code, read = _run(prog, monkeypatch, scans=2, runstop=runstop)

    assert read(SwitchOn) == 1
    assert read(ModeOn) == 1


def test_mode_switch_bit_reads_the_switch_in_stop(monkeypatch):
    # run_when_high=True with the switch low: STOP, and SC10 reads off.
    with Program(strict=False) as prog:
        with Rung(system.sys.mode_switch_run):
            copy(1, SwitchOn)

    _code, read = _run(
        prog, monkeypatch, scans=2, runstop=RunStopConfig(debounce_ms=0, run_when_high=True)
    )

    assert read(system.sys.mode_switch_run) is False


RTC_FIELDS = {
    "year4": 2026,
    "year2": 26,
    "month": 10,
    "day": 28,
    # Wednesday; CLICK counts Sunday = 1.
    "weekday": 4,
    "hour": 13,
    "minute": 45,
    "second": 7,
}


def test_rtc_points_read_localtime(monkeypatch):
    now = time.struct_time((2026, 10, 28, 13, 45, 7, 2, 301, -1))
    monkeypatch.setattr(time, "localtime", lambda *args: now)
    dests = {name: Int(f"Rtc_{name}", retentive=False) for name in RTC_FIELDS}
    with Program(strict=False) as prog:
        with Rung():
            for name, dest in dests.items():
                copy(getattr(system.rtc, name), dest)

    _code, read = _run(prog, monkeypatch, scans=1)

    assert {name: read(dest) for name, dest in dests.items()} == RTC_FIELDS


def test_unreferenced_system_points_emit_nothing(monkeypatch):
    with Program(strict=False) as prog:
        with Rung(Armed):
            copy(1, Sink)

    code, _read = _run(prog, monkeypatch, scans=1)

    for marker in ("_sys_t0_ns", "localtime", "_sys_scan_n", "_sys_first"):
        assert marker not in code


# A math error (SC46) does not stop the code, as it does not stop a CLICK or
# core: the Math writes 0, SC46 is on for the rest of that scan, the next scan
# starts with it off, and every scan runs (core/system_points.py).
Operand = Real("Operand", retentive=False)
Root = Real("Root", retentive=False)
SawMathError = Int("SawMathError", retentive=False)
Faulted = Int("Faulted", retentive=True)
RunScans = Int("RunScans", retentive=True)


def _math_error_program() -> Program:
    with Program(strict=False) as prog:
        with Rung():
            calc(RunScans + 1, RunScans)
        with Rung(Faulted == 0):
            copy(1, Faulted)
            copy(-4.0, Operand)
            copy(99.0, Root)
            calc(sqrt(Operand), Root)
        with Rung(system.fault.math_operation_error):
            calc(SawMathError + 1, SawMathError)
        # Reference SC50 and SC11, so the generated code carries them.
        with Rung(system.sys.cmd_mode_stop, system.sys.mode_run):
            copy(0, Sink)
    return prog


@pytest.mark.parametrize(
    "runstop",
    [None, RunStopConfig(debounce_ms=0, run_when_high=False)],
    ids=["no-runstop", "switch-in-run"],
)
def test_math_error_does_not_stop_the_code(monkeypatch, runstop):
    # The test board's switch reads low: RUN for run_when_high=False.
    _code, read = _run(_math_error_program(), monkeypatch, scans=5, runstop=runstop)

    assert read(Root) == 0.0
    # SC46 was on for the faulting scan only, and cleared at the next start.
    assert read(SawMathError) == 1
    assert read(system.fault.math_operation_error) is False
    # Every scan ran, in RUN, with SC50 off.
    assert read(RunScans) == 5
    assert read(system.sys.cmd_mode_stop) is False
    assert read(system.sys.mode_run) is True
    assert read(Faulted) == 1


@pytest.mark.parametrize(
    "runstop",
    # expose_mode_tags=True would bring in SC50, whose command holds STOP.
    [None, RunStopConfig(expose_mode_tags=False)],
    ids=["no-runstop", "runstop"],
)
def test_a_math_function_emits_no_stop_hold(monkeypatch, runstop):
    with Program(strict=False) as prog:
        with Rung():
            calc(sqrt(Operand), Root)

    hw = _hw()
    code = generate_circuitpy(prog, hw, target_scan_ms=1.0, runstop=runstop).code

    assert "_math_func" in code
    assert "_stop_hold" not in code
    if runstop is None:
        assert "_mode_run" not in code
        assert "_force_outputs_off" not in code
