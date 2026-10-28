"""Scan time in the generated scan loop, against core.

Each test runs the generated ``code.py`` for a few scans on a stand-in board
clock: ``time.monotonic_ns()`` reads an integer ns counter that moves only
when the loop sleeps out its scan, one fixed step per pass; ``time.monotonic()``
reads the same counter as CircuitPython does, a float with CircuitPython's
30-bit precision.  Expected values come from core's runner where they can.
"""

from __future__ import annotations

import struct
import time

import pytest

from pyrung.circuitpy import P1AM, ModbusServerConfig, RunStopConfig, generate_circuitpy
from pyrung.click import TagMap
from pyrung.core import PLC, Int, Program, Rung, Timer, copy, on_delay
from pyrung.core.system_points import system
from tests.circuitpy.test_codegen import _context_for_program, _run_single_scan_source
from tests.circuitpy.test_system_points import _StubBase

_SCAN_NS = 10_000_000  # every loop pass takes 10 ms


def _f32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def _circuitpython_float(value: float) -> float:
    """*value* as a CircuitPython float: float32 less its two low mantissa bits.

    CircuitPython builds for the SAMD51 store a float in an object word
    (MICROPY_OBJ_REPR_C), which drops the two low bits of the float32 mantissa.
    """
    (bits,) = struct.unpack("<I", struct.pack("<f", value))
    return struct.unpack("<f", struct.pack("<I", bits & ~3))[0]


class _BoardClock:
    def __init__(self, start_ns: int, step_ns: int = _SCAN_NS):
        self.now_ns = start_ns
        self.step_ns = step_ns

    def monotonic_ns(self) -> int:
        return self.now_ns

    def monotonic(self) -> float:
        # CircuitPython's time.monotonic(): float(ms) / 1000 in single precision.
        ms = self.now_ns // 1_000_000
        return _circuitpython_float(_f32(_f32(float(ms)) / 1000.0))

    def sleep(self, _seconds: float) -> None:
        self.now_ns += self.step_ns

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(time, "monotonic_ns", self.monotonic_ns)
        monkeypatch.setattr(time, "monotonic", self.monotonic)
        monkeypatch.setattr(time, "sleep", self.sleep)


def _hw() -> P1AM:
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    return hw


_SWITCH_SAMPLE = "_t_board_switch = bool(_board_switch_io.value)"


def _run(
    prog: Program,
    monkeypatch,
    *,
    scans: int,
    runstop: RunStopConfig | None = None,
    stop_scans: range | None = None,
    at_loop_top: str | None = None,
    modbus: bool = False,
    start_ns: int = 5_000_000_000,
):
    """Run *scans* loop passes; return the namespace and a tag reader.

    With *stop_scans*, the RUN/STOP switch reads STOP (high, for
    ``run_when_high=False``) on those passes.  *at_loop_top* runs at the top of
    each pass, before the scan reads the clock: where a Modbus request served
    after the previous scan lands.
    """
    hw = _hw()
    kwargs = {}
    if modbus:
        kwargs = {"modbus_server": ModbusServerConfig(ip="192.168.1.200"), "tag_map": TagMap()}
    result = generate_circuitpy(prog, hw, target_scan_ms=10.0, runstop=runstop, **kwargs)
    code = result.code
    if stop_scans is not None:
        assert _SWITCH_SAMPLE in code
        code = code.replace(_SWITCH_SAMPLE, "_t_board_switch = __scan_once in _STOP_SCANS", 1)
        code = code.replace("while True:", f"_STOP_SCANS = {stop_scans!r}\nwhile True:", 1)
    if at_loop_top is not None:
        code = code.replace("while True:\n", f"while True:\n    {at_loop_top}\n", 1)
    _BoardClock(start_ns).install(monkeypatch)
    namespace = _run_single_scan_source(
        code, monkeypatch, _StubBase(), runtime_source=result.runtime, scans=scans
    )
    ctx = _context_for_program(prog, hw)
    return namespace, (lambda tag: namespace[ctx.symbol_for_tag(tag)])


# -- Gap 1: scan time from integer ns -----------------------------------------

Ton = Timer.clone("Ton")


@pytest.mark.parametrize("uptime_s", [100_000, 1_000_000], ids=["28h", "11d"])
def test_timer_keeps_ms_at_long_uptime(monkeypatch, uptime_s):
    # 12 scans of 10 ms: the first times boot to scan (0 here), the other 11
    # add 10 ms each.  A float monotonic() at this uptime moves in steps of
    # 31 ms (28 h) or 250 ms (11 d), so the timer would lose time.
    with Program(strict=False) as prog:
        with Rung():
            on_delay(Ton, preset=1000, unit="Tms")

    hw = _hw()
    code = generate_circuitpy(prog, hw, target_scan_ms=10.0, runstop=None).code
    assert "time.monotonic()" not in code

    _namespace, read = _run(prog, monkeypatch, scans=12, start_ns=uptime_s * 1_000_000_000)

    assert read(Ton.Acc) == 110


def test_board_clock_float_loses_the_ms_it_should():
    # The stand-in's float clock is the one the old loop read: over the same
    # 110 ms it measures something else at either uptime.
    for uptime_s in (100_000, 1_000_000):
        clock = _BoardClock(uptime_s * 1_000_000_000)
        before = clock.monotonic()
        for _ in range(11):
            clock.sleep(0.0)
        assert round((clock.monotonic() - before) * 1000) != 110


# -- Gap 2: the first RUN scan after STOP gets its dt -------------------------

FirstAcc = Int("FirstAcc", retentive=False)


def _first_acc_program() -> Program:
    with Program(strict=False) as prog:
        with Rung():
            on_delay(Ton, preset=10000, unit="Tms")
        with Rung(system.sys.first_scan):
            copy(Ton.Acc, FirstAcc)
    return prog


def test_first_scan_after_stop_to_run_times_a_full_scan(monkeypatch):
    # Core, fixed step: the first scan after start() advances by a full dt.
    plc = PLC(logic=_first_acc_program(), dt=_SCAN_NS / 1e9)
    plc.run(cycles=3)
    plc.stop()
    plc.start()
    plc.step()
    core_first_acc = plc.current_state.tags[FirstAcc.name]
    assert core_first_acc == 10

    # Switch at STOP on passes 2..3 (debounced a pass later): STOP on pass 3,
    # back to RUN on pass 5, the first RUN scan.
    _namespace, read = _run(
        _first_acc_program(),
        monkeypatch,
        scans=6,
        runstop=RunStopConfig(debounce_ms=0, run_when_high=False),
        stop_scans=range(2, 4),
    )

    assert read(FirstAcc) == core_first_acc
    assert read(Ton.Acc) == core_first_acc
