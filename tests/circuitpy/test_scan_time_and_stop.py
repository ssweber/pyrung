"""Scan time and STOP in the generated scan loop, against core.

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
from pyrung.core import PLC, Int, Program, Rung, Timer, calc, copy, on_delay, out
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


# -- Gaps 3 and 4: the STOP command holds -------------------------------------

RunScans = Int("RunScans", retentive=True)
ModeScans = Int("ModeScans", retentive=True)
_SC50_WRITE = "_mb_write_coil_plc('SC', 50, True)"


def _count_program() -> Program:
    with Program(strict=False) as prog:
        with Rung():
            calc(RunScans + 1, RunScans)
        with Rung(system.sys.mode_run):
            calc(ModeScans + 1, ModeScans)
    return prog


_SWITCH_IN_RUN = RunStopConfig(debounce_ms=0, run_when_high=False)


@pytest.mark.parametrize("runstop", [None, _SWITCH_IN_RUN], ids=["no-runstop", "switch-in-run"])
def test_sc50_over_modbus_holds_stop(monkeypatch, runstop):
    # Core's data provider stops the PLC on the write: no scan after it, SC50
    # not stored, SC11 off.  Written after scan 1, held for the rest.
    namespace, read = _run(
        _count_program(),
        monkeypatch,
        scans=6,
        runstop=runstop,
        modbus=True,
        at_loop_top=f"if __scan_once == 2: {_SC50_WRITE}",
    )

    assert namespace["_mode_run"] is False
    assert read(RunScans) == 2
    # The TagMap brings SC11 and SC50 in; the program alone does not.
    assert namespace["_t_sys_mode_run"] is False
    assert namespace["_t_sys_cmd_mode_stop"] is False


@pytest.mark.parametrize(
    ("scans", "run_scans"),
    [
        # Held through the switch's RUN; switch at STOP on 4..5, STOP on 5.
        (6, 2),
        # Back at RUN on 6, debounced on 7: STOP->RUN, and 7..9 run.
        (10, 5),
    ],
    ids=["held", "switch-stop-then-run"],
)
def test_sc50_over_modbus_releases_at_switch_stop_then_run(monkeypatch, scans, run_scans):
    namespace, read = _run(
        _count_program(),
        monkeypatch,
        scans=scans,
        runstop=_SWITCH_IN_RUN,
        stop_scans=range(4, 6),
        modbus=True,
        at_loop_top=f"if __scan_once == 2: {_SC50_WRITE}",
    )

    mode_run = scans == 10
    assert namespace["_mode_run"] is mode_run
    assert read(system.sys.mode_run) is mode_run
    assert read(RunScans) == run_scans


def _rung_stop_program() -> Program:
    # The second scan writes SC50; SC11 is read after it.
    with Program(strict=False) as prog:
        with Rung():
            calc(RunScans + 1, RunScans)
        with Rung(system.sys.mode_run):
            calc(ModeScans + 1, ModeScans)
        with Rung(RunScans == 2):
            out(system.sys.cmd_mode_stop)
    return prog


def _core_until_stop(prog: Program, max_scans: int) -> dict[str, object]:
    plc = PLC(logic=prog)
    for _ in range(max_scans):
        plc.step()
        if plc.mode == "stop":
            break
    assert plc.mode == "stop"
    tags = plc.current_state.tags
    return {
        "RunScans": tags[RunScans.name],
        "ModeScans": tags[ModeScans.name],
        "SC50": bool(tags.get(system.sys.cmd_mode_stop.name, False)),
    }


@pytest.mark.parametrize(
    "runstop",
    [None, RunStopConfig(debounce_ms=0, run_when_high=False, expose_mode_tags=False)],
    ids=["no-runstop", "switch-in-run"],
)
def test_sc50_from_a_rung_stops_like_core(monkeypatch, runstop):
    # Core: the scan after the write takes the command (SC50 clears, SC11
    # reads off for it), runs, and the PLC is in STOP at its end, held there.
    core = _core_until_stop(_rung_stop_program(), max_scans=10)
    assert core == {"RunScans": 3, "ModeScans": 2, "SC50": False}

    namespace, read = _run(_rung_stop_program(), monkeypatch, scans=6, runstop=runstop)

    assert namespace["_mode_run"] is False
    assert read(RunScans) == core["RunScans"]
    assert read(ModeScans) == core["ModeScans"]
    assert read(system.sys.cmd_mode_stop) is core["SC50"]
    assert read(system.sys.mode_run) is False


# -- Gap 5: SC11 follows the mode without expose_mode_tags --------------------


@pytest.mark.parametrize(
    ("scans", "mode_run"),
    [(3, True), (5, False)],
    ids=["run", "switch-at-stop"],
)
def test_referenced_sc11_follows_mode_without_expose_mode_tags(monkeypatch, scans, mode_run):
    # Switch at STOP from pass 3, debounced on 4.
    _namespace, read = _run(
        _count_program(),
        monkeypatch,
        scans=scans,
        runstop=RunStopConfig(debounce_ms=0, run_when_high=False, expose_mode_tags=False),
        stop_scans=range(3, 10),
    )

    assert read(system.sys.mode_run) is mode_run
    assert read(ModeScans) == read(RunScans) == min(scans, 4)
