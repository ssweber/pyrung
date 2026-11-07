"""The guarded scan loop against stepping, at fine granularity.

Every program here runs twice: through ``run_for`` / ``run_until`` with the loop
firing a few scans at a time, and interpreted, scan by scan.  Each state the loop
leaves between firings must equal the twin's state at that scan, with stimulus
changing mid-run, and each case says how much of it ran in the loop, so a decline
cannot pass as agreement.
"""

from __future__ import annotations

import ast
import math as pymath
import random
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from pyrung import (
    PLC,
    And,
    Block,
    Bool,
    Dint,
    Int,
    Or,
    Program,
    Real,
    Rung,
    TagType,
    Timer,
    blockcopy,
    call,
    copy,
    count_down,
    count_up,
    fall,
    fill,
    forloop,
    latch,
    math,
    off_delay,
    on_delay,
    out,
    reset,
    return_early,
    rise,
    run_function,
    shift,
    subroutine,
    system,
    time_drum,
)
from pyrung import Counter as CounterBlock
from pyrung.core import compiled_run, scan_trace
from pyrung.core.context import same_stored_value
from pyrung.core.expression import randint
from pyrung.core.harness import Harness
from pyrung.core.physical import Physical
from pyrung.core.scan_trace import (
    _CLASSIFIED_SYSTEM_TAGS,
    CONSTANT_SYSTEM_TAGS,
    DECLINING_SYSTEM_TAGS,
    MOVING_SYSTEM_TAGS,
    Decline,
    KernelSource,
    SelfCheckMismatch,
    build_trace,
)
from pyrung.core.system_points import (
    _DERIVED_TAG_NAMES,
    SYSTEM_TAGS_BY_NAME,
    WRITABLE_SYSTEM_TAG_NAMES,
)
from tests import fold_check

RTC_START = datetime(2026, 3, 14, 15, 9, 26, 500000)
BURSTS = (1, 1, 2, 1, 3, 7, 1, 1, 97, 1, 2, 1, 317, 1, 1, 5, 1)

T, T1, T2, T3 = (Timer.clone(name) for name in ("TrT", "TrT1", "TrT2", "TrT3"))
Cnt = CounterBlock.clone("TrC")
Hit, Run, Rst, Lamp, Lamp2, DrumDone = (
    Bool(name) for name in ("TrHit", "TrRun", "TrRst", "TrLamp", "TrLamp2", "TrDrumDone")
)
N, M, Step, Preset = (Int(name) for name in ("TrN", "TrM", "TrStep", "TrPreset"))
Big = Dint("TrBig")
Temp = Real("TrTemp")
Ds = Block("TrDs", TagType.INT, 1, 8)
Bits = Block("TrBits", TagType.BOOL, 1, 6)


@pytest.fixture
def fine_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every run through the kernel, the loop firing a few scans at a time, loudly."""
    monkeypatch.setattr(compiled_run, "INTERPRETED_PREFIX", 1)
    monkeypatch.setattr(scan_trace, "MIN_RUN_SCANS", 1)
    monkeypatch.setattr(scan_trace, "BURST_SIZES", BURSTS)
    monkeypatch.setattr(scan_trace, "STRICT_SELF_CHECK", True)
    monkeypatch.setattr(scan_trace, "SELF_CHECK_OBSERVERS", [fold_check._note_self_check_mismatch])


def _plc(program: Program, dt: float) -> PLC:
    plc = PLC(program, dt=dt)
    # From construction too: the log keeps the base a set_rtc() replaced, and two
    # runners built apart would differ there by the microseconds between them.
    plc._set_rtc_internal(RTC_START, 0.0)
    plc.set_rtc(RTC_START)
    return plc


def _assert_equal(ours: Any, stepped: Any) -> None:
    fold_check._compare("compiled", stepped.scan_id, ours, stepped)


class _Observed:
    """The states the loop leaves between firings."""

    def __init__(self, plc: PLC) -> None:
        self.plc = plc
        self.states: dict[int, Any] = {}

    def __call__(self, runner: Any, state: Any) -> None:
        if runner is self.plc:
            self.states[state.scan_id] = state

    def __enter__(self) -> _Observed:
        compiled_run.LOOP_OBSERVERS.append(self)
        return self

    def __exit__(self, *_exc: object) -> None:
        compiled_run.LOOP_OBSERVERS.remove(self)


def _step_to(
    twin: PLC,
    scan_id: int,
    seen: dict[int, Any],
    truth: dict[int, Any] | None = None,
) -> int:
    """Scan the twin on as one run does: a stopped runner resumes only at the next call.

    *truth* keeps a sample of the states stepped, for what history later says of them;
    a restart starts a new recording, so it forgets the old one's.
    """
    compared = 0
    twin._ensure_running()
    start = twin.state.scan_id
    if truth and start < max(truth):
        truth.clear()
    every = max(1, (scan_id - start) // fold_check.HISTORY_SAMPLES)
    while twin.state.scan_id < scan_id:
        twin._run_single_scan(consume_pause_request=False)
        sid = twin.state.scan_id
        if truth is not None and ((sid - start) % every == 0 or sid == scan_id):
            truth[sid] = twin.state
        ours = seen.get(twin.state.scan_id)
        if ours is not None:
            _assert_equal(ours, twin.state)
            compared += 1
    return compared


def _a_sample_of(truth: dict[int, Any]) -> dict[int, Any]:
    """Evenly spaced states of those kept, the last among them."""
    scans = sorted(truth)
    every = max(1, len(scans) // fold_check.HISTORY_SAMPLES)
    return {scan: truth[scan] for scan in scans[::-1][::every][: fold_check.HISTORY_SAMPLES]}


def _lockstep(
    program: Program,
    segments: list[tuple[float, dict[Any, Any]]],
    *,
    dt: float = 0.01,
    prepare: Callable[[PLC], None] | None = None,
) -> Counter[str]:
    """Run *program* through the segments, (seconds, patches first), against a twin."""
    plc = _plc(program, dt)
    twin = _plc(program, dt)
    if prepare is not None:
        prepare(plc)
        prepare(twin)
    before = Counter(scan_trace.TOTALS)
    compared = 0
    start = 0
    truth: dict[int, Any] = {}
    for seconds, patches in segments:
        if patches:
            plc.patch(patches)
            twin.patch(patches)
        start = plc.state.scan_id
        with _Observed(plc) as observed:
            plc.run_for(seconds)
        compared += _step_to(twin, plc.state.scan_id, observed.states, truth)
        _assert_equal(plc.state, twin.state)
        fold_check.compare_runners("compiled", plc, twin, start)
        fold_check.compare_continuation("compiled", plc, twin)
    # Once, at the end: the scans of every segment have to outlast the ones after them.
    fold_check.compare_history("compiled", plc, twin, start, _a_sample_of(truth))
    counts = Counter(scan_trace.TOTALS)
    counts.subtract(before)
    counts["states compared"] = compared
    return counts


# --- The corpus ------------------------------------------------------------------


def _corpus_timer_long_preset() -> None:
    with Rung(Run):
        on_delay(T, 400, "s")
    with Rung(T.Done):
        latch(Hit)
    with Rung(T.Acc >= 350):
        out(Lamp)


def _corpus_two_timers_past_300s() -> None:
    with Rung():
        on_delay(T1, 310, "s")
    with Rung():
        on_delay(T2, 650_000)
    with Rung(T1.Done):
        out(Lamp)
    with Rung(T2.Done):
        out(Lamp2)


def _corpus_off_delay() -> None:
    with Rung(Run):
        off_delay(T, 2_500)
    with Rung(T.Done):
        out(Lamp)


def _corpus_retentive_timer() -> None:
    with Rung(Run):
        on_delay(T, 5_000).reset(Rst)
    with Rung(T.Done):
        latch(Hit)


def _corpus_sequencer() -> None:
    with Rung(Step == 0):
        on_delay(T1, 300)
    with Rung(T1.Done):
        copy(1, Step)
    with Rung(Step == 1):
        on_delay(T2, 700)
    with Rung(T2.Done):
        copy(2, Step)
    with Rung(Step == 2):
        on_delay(T3, 500)
    with Rung(T3.Done):
        copy(0, Step)
    with Rung(T1.Acc >= 100):
        out(Lamp)


def _corpus_counter_on_clock() -> None:
    with Rung(system.sys.clock_100ms):
        count_up(Cnt, 25).reset(Rst)
    with Rung(Cnt.Done):
        latch(Hit)


def _corpus_counter_down() -> None:
    with Rung(system.sys.clock_500ms):
        count_down(Cnt, 0).reset(Rst)
    with Rung(Cnt.Acc <= -5):
        latch(Hit)


def _corpus_calc_wraps() -> None:
    with Rung():
        math(N + 7, N)
    with Rung(N < 0):
        latch(Hit)


def _corpus_calc_gated_by_clock() -> None:
    with Rung(system.sys.clock_1s):
        math(N + 1, N)
    with Rung(N >= 5):
        out(Lamp)


def _corpus_real_accumulates() -> None:
    with Rung(Run):
        math(Temp + 0.25, Temp)
    with Rung(Temp >= 10.0):
        latch(Hit)


def _median_of(*addresses: int) -> None:
    values = Block("TraceMedianValues", TagType.INT, 1, 3)
    level = Int("TrMedianLevel")
    with Rung(Run):
        copy(7, values[1])
    with Rung(Rst):
        copy(9, values[2])
    with Rung():
        math(values.select(addresses[0], addresses[-1]).median(), level)
    with Rung(level >= 8):
        latch(Hit)


def _corpus_median_of_an_even_count() -> None:
    _median_of(1, 2)


def _corpus_median_of_an_odd_count() -> None:
    _median_of(1, 3)


def _corpus_real_overflows() -> None:
    with Rung():
        copy(1.5e30, Temp, oneshot=True)
    with Rung():
        math(Temp * 1.5, Temp)
    with Rung(Temp > 1e37):
        latch(Hit)


def _corpus_division_by_moving() -> None:
    with Rung():
        on_delay(T, 2_000)
    with Rung():
        math(100 / (T.Acc - 1_000), N)


def _corpus_copy_out_of_range() -> None:
    with Rung():
        math(Big + 300, Big)
    with Rung():
        copy(Big, N)


def _corpus_clock_stored() -> None:
    with Rung(system.sys.clock_1s):
        out(Lamp)
    with Rung(system.sys.clock_100ms):
        out(Lamp2)


def _corpus_rtc_gate() -> None:
    with Rung(system.rtc.second < 30):
        math(N + 1, N)
    with Rung():
        on_delay(T, 60_000)


def _corpus_rtc_stored() -> None:
    with Rung():
        copy(system.rtc.second, N)
    with Rung(N == 59):
        latch(Hit)


def _corpus_edges() -> None:
    with Rung(rise(system.sys.clock_1s)):
        math(N + 1, N)
    with Rung(fall(Run)):
        math(M + 1, M)
    with Rung(rise(Run)):
        latch(Lamp)


def _corpus_oneshot() -> None:
    with Rung(T.Done):
        copy(1, N, oneshot=True)
    with Rung(N == 0):
        math(M + 1, M)
    with Rung():
        copy(0, N)
    with Rung():
        on_delay(T, 200)


def _corpus_double_acc_write() -> None:
    with Rung():
        on_delay(T, 300)
    with Rung():
        copy(0, T.Acc)


def _corpus_preset_around_reader() -> None:
    with Rung():
        copy(200, Preset)
    with Rung():
        on_delay(T, Preset)
    with Rung():
        copy(5_000, Preset)
    with Rung(T.Done):
        latch(Hit)


def _corpus_drum() -> None:
    with Rung(Run):
        time_drum(
            outputs=[Lamp, Lamp2],
            pattern=[[1, 0], [0, 1], [1, 1]],
            presets=[500, 100, 500],
            unit="ms",
            current_step=Step,
            accumulator=Big,
            completion_flag=DrumDone,
        ).reset(Rst)


def _corpus_block_fill_and_copy() -> None:
    with Rung(system.sys.clock_500ms):
        fill(N, Ds.select(1, 3))
    with Rung():
        blockcopy(Ds.select(1, 3), Ds.select(4, 6))
    with Rung():
        math(N + 1, N)


def _corpus_indirect() -> None:
    with Rung():
        copy(Step, Ds[N])
    with Rung(T.Done):
        math(N + 1, N)
    with Rung(~T.Done):
        on_delay(T, 250)
    with Rung(N >= 8):
        copy(1, N)
    with Rung(Ds[N] == 3):
        out(Lamp)


def _corpus_shift() -> None:
    with Rung(Run):
        shift(Bits.select(1, 6)).clock(system.sys.clock_100ms).reset(Rst)
    with Rung(Bits[6]):
        latch(Hit)


def _corpus_subroutine() -> None:
    @subroutine("TrSub")
    def _sub() -> None:
        with Rung(Rst):
            return_early()
        with Rung():
            math(N + 1, N)
        with Rung(N >= 100):
            copy(0, N)

    with Rung(Run):
        call(_sub)
    with Rung():
        on_delay(T, 1_000)


def _corpus_rtc_apply() -> None:
    with Rung(Run):
        on_delay(T, 2_000)
    with Rung(rise(T.Done)):
        copy(10, system.rtc.new_second)
        copy(5, system.rtc.new_minute)
        copy(1, system.rtc.new_hour)
        out(system.rtc.apply_time)
    with Rung(rise(T.Done)):
        copy(system.rtc.hour, N)
    with Rung():
        copy(system.rtc.hour, M)
    with Rung():
        copy(system.rtc.second, Step)


def _corpus_rtc_apply_date() -> None:
    with Rung(Run):
        on_delay(T, 1_000)
    with Rung(rise(T.Done)):
        copy(1, system.rtc.new_day)
        copy(2, system.rtc.new_month)
        copy(2027, system.rtc.new_year4)
        out(system.rtc.apply_date)
    with Rung(Run):
        on_delay(T1, 3_000)
    with Rung(rise(T1.Done)):
        copy(2, system.rtc.new_hour)
        copy(0, system.rtc.new_minute)
        copy(0, system.rtc.new_second)
        out(system.rtc.apply_time)
    with Rung():
        copy(system.rtc.month, M)
    with Rung():
        copy(system.rtc.hour, Step)


def _corpus_rtc_apply_held() -> None:
    """An apply on every scan: the hook is never quiet, so no scan reaches the loop."""
    with Rung(Run):
        on_delay(T, 1_000)
    with Rung(T.Done):
        copy(2, system.rtc.new_hour)
        copy(0, system.rtc.new_minute)
        copy(0, system.rtc.new_second)
        out(system.rtc.apply_time)
    with Rung():
        copy(system.rtc.second, Step)


def _corpus_mode_stop() -> None:
    with Rung(Run):
        on_delay(T, 2_000)
    with Rung(rise(T.Done)):
        out(system.sys.cmd_mode_stop)
    with Rung():
        math(N + 1, N)


def _corpus_mode_stop_at_once() -> None:
    with Rung(Run):
        out(system.sys.cmd_mode_stop)
    with Rung():
        math(N + 1, N)


def _corpus_forloop() -> None:
    with Rung(Run), forloop(3):
        math(N + 1, N)
    with Rung(N >= 900):
        latch(Hit)


CORPUS: dict[str, tuple[Callable[[], None], float, list[tuple[float, dict[Any, Any]]]]] = {
    "timer_long_preset": (
        _corpus_timer_long_preset,
        1.0,
        [(40.0, {Run: True}), (360.0, {}), (30.0, {Run: False}), (420.0, {Run: True})],
    ),
    "two_timers_past_300s": (_corpus_two_timers_past_300s, 0.5, [(700.0, {})]),
    "off_delay": (
        _corpus_off_delay,
        0.01,
        [(5.0, {Run: True}), (3.0, {Run: False}), (2.0, {Run: True})],
    ),
    "retentive_timer": (
        _corpus_retentive_timer,
        0.01,
        [(2.0, {Run: True}), (1.0, {Run: False}), (4.0, {Run: True}), (1.0, {Rst: True})],
    ),
    "sequencer": (_corpus_sequencer, 0.01, [(8.0, {})]),
    "counter_on_clock": (
        _corpus_counter_on_clock,
        0.01,
        [(3.0, {}), (0.5, {Rst: True}), (0.5, {Rst: False}), (4.0, {})],
    ),
    "counter_down": (_corpus_counter_down, 0.01, [(5.0, {})]),
    "calc_wraps": (_corpus_calc_wraps, 0.01, [(60.0, {})]),
    "median_of_an_even_count": (
        _corpus_median_of_an_even_count,
        0.01,
        [(2.0, {}), (2.0, {Run: True}), (2.0, {Rst: True})],
    ),
    "median_of_an_odd_count": (
        _corpus_median_of_an_odd_count,
        0.01,
        [(2.0, {}), (2.0, {Run: True}), (2.0, {Rst: True})],
    ),
    "calc_gated_by_clock": (_corpus_calc_gated_by_clock, 0.01, [(9.0, {})]),
    "real_accumulates": (
        _corpus_real_accumulates,
        0.01,
        [(1.0, {Run: True}), (0.2, {Run: False}), (1.0, {Run: True})],
    ),
    "real_overflows": (_corpus_real_overflows, 0.01, [(5.0, {})]),
    "division_by_moving": (_corpus_division_by_moving, 0.01, [(3.0, {})]),
    "copy_out_of_range": (_corpus_copy_out_of_range, 0.01, [(3.0, {})]),
    "clock_stored": (_corpus_clock_stored, 0.01, [(7.0, {})]),
    "ms_timer_fractional_dt": (_corpus_preset_around_reader, 0.3, [(30.0, {})]),
    "rtc_gate": (_corpus_rtc_gate, 0.01, [(70.0, {})]),
    "rtc_stored": (_corpus_rtc_stored, 0.02, [(70.0, {})]),
    "edges": (
        _corpus_edges,
        0.01,
        [(3.0, {Run: True}), (0.05, {Run: False}), (3.0, {Run: True})],
    ),
    "oneshot": (_corpus_oneshot, 0.01, [(4.0, {})]),
    "double_acc_write": (_corpus_double_acc_write, 0.01, [(3.0, {})]),
    "preset_around_reader": (_corpus_preset_around_reader, 0.01, [(8.0, {})]),
    "drum": (
        _corpus_drum,
        0.01,
        [(4.0, {Run: True}), (0.3, {Run: False}), (2.0, {Run: True}), (0.5, {Rst: True})],
    ),
    "block_fill_and_copy": (_corpus_block_fill_and_copy, 0.01, [(5.0, {})]),
    "indirect": (_corpus_indirect, 0.01, [(5.0, {N: 1, Step: 3})]),
    "shift": (
        _corpus_shift,
        0.01,
        [(3.0, {Run: True}), (0.5, {Rst: True}), (2.0, {Rst: False})],
    ),
    "subroutine": (
        _corpus_subroutine,
        0.01,
        [(1.0, {Run: True}), (0.2, {Rst: True}), (1.0, {Rst: False}), (0.5, {Run: False})],
    ),
    "forloop": (_corpus_forloop, 0.01, [(5.0, {Run: True})]),
    "rtc_apply": (_corpus_rtc_apply, 0.01, [(1.0, {Run: True}), (4.0, {}), (1.0, {Run: False})]),
    "rtc_apply_date": (_corpus_rtc_apply_date, 0.01, [(5.0, {Run: True}), (1.0, {})]),
    "rtc_apply_held": (_corpus_rtc_apply_held, 0.01, [(1.5, {Run: True}), (1.0, {})]),
    "mode_stop": (_corpus_mode_stop, 0.01, [(1.0, {Run: True}), (4.0, {}), (1.0, {})]),
    "mode_stop_at_once": (_corpus_mode_stop_at_once, 0.01, [(1.0, {Run: True}), (1.0, {})]),
}

# Cases where the loop has every right to run nothing: each scan is a decision.
_NO_COVERAGE_EXPECTED: frozenset[str] = frozenset({"rtc_apply_held"})


def _build(fill_program: Callable[[], None]) -> Program:
    with Program(strict=False) as program:
        fill_program()
    return program


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_corpus_equals_stepping_between_firings(name: str, fine_loop: None) -> None:
    fill_program, dt, segments = CORPUS[name]
    counts = _lockstep(_build(fill_program), segments, dt=dt)
    assert not counts["self check mismatches"]
    if name not in _NO_COVERAGE_EXPECTED:
        assert counts["loop scans"] > 0, dict(counts)
        assert counts["states compared"] > 0, dict(counts)


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("name", sorted(CORPUS))
def test_corpus_under_random_stimulus(name: str, seed: int, fine_loop: None) -> None:
    """The inputs flip at random scans, so a trace meets states no test author chose."""
    fill_program, dt, _segments = CORPUS[name]
    rng = random.Random(f"{name}/{seed}")
    segments: list[tuple[float, dict[Any, Any]]] = [(0.05 * dt / 0.01, {Run: True})]
    for _ in range(12):
        patches: dict[Any, Any] = {}
        if rng.random() < 0.6:
            patches[Run] = rng.random() < 0.7
        if rng.random() < 0.3:
            patches[Rst] = rng.random() < 0.3
        segments.append((rng.choice((1, 2, 7, 30, 120, 400)) * dt, patches))
    counts = _lockstep(_build(fill_program), segments, dt=dt)
    assert not counts["self check mismatches"]


def test_corpus_as_a_whole_mostly_runs_in_the_loop(fine_loop: None) -> None:
    """Coverage across the corpus, so a decline creeping in shows up here."""
    loop = real = 0
    for fill_program, dt, segments in CORPUS.values():
        counts = _lockstep(_build(fill_program), segments, dt=dt)
        loop += counts["loop scans"]
        real += counts["real scans"]
    assert loop > real


def test_long_preset_lands_on_the_stepped_scan_at_production_settings() -> None:
    """Long bursts, no observer: only the landing and the checkpoints to compare."""
    program = _build(_corpus_timer_long_preset)
    plc = _plc(program, 0.1)
    twin = _plc(program, 0.1)
    plc.patch({Run: True})
    twin.patch({Run: True})
    before = scan_trace.TOTALS["loop scans"]
    plc.run_for(450.0)
    twin.run(plc.state.scan_id)
    _assert_equal(plc.state, twin.state)
    assert plc.state.tags["TrHit"] is True
    assert scan_trace.TOTALS["loop scans"] - before > 0


def test_a_harness_plant_runs_in_the_loop_as_part_of_the_scan(fine_loop: None) -> None:
    """A plant is a second kernel function, before the input drain: it is traced too."""
    Plant = Real("TrPlant", physical=Physical("TrSensor", ramp_on=1.0, ramp_off=-0.5), link="TrRun")

    def fill_program() -> None:
        with Rung(Run):
            out(Lamp)
        with Rung(Plant >= 3.0):
            latch(Hit)
        with Rung():
            on_delay(T, 40_000)

    program = _build(fill_program)

    def make() -> PLC:
        plc = _plc(program, 0.01)
        Harness(plc).install()
        return plc

    plc = make()
    assert plc._compiled_replay_supported_kernel().pre_step_fn is not None
    counts = _lockstep(
        program,
        [(1.0, {Run: True}), (4.0, {}), (2.0, {Run: False}), (3.0, {})],
        prepare=lambda runner: Harness(runner).install(),
    )
    assert counts["loop scans"] > 0
    assert counts["states compared"] > 0


def test_a_bool_plant_latches_its_arming_key_as_stepping_does(fine_loop: None) -> None:
    """A plant's arming latch stays out of the state until it fires.

    The kernel holds it at its default until then, so this is where a key added on the
    wrong scan would show: the compare is by key as well as by value.
    """
    Feedback = Bool(
        "TrFeedback", physical=Physical("TrMotor", on_delay="2s", off_delay="500ms"), link="TrRun"
    )

    def fill_program() -> None:
        with Rung(Feedback):
            latch(Hit)
        with Rung():
            on_delay(T, 40_000)

    program = _build(fill_program)
    plc = _plc(program, 0.01)
    Harness(plc).install()
    assert plc._compiled_replay_supported_kernel() is not None
    counts = _lockstep(
        program,
        [(1.0, {Run: True}), (3.0, {}), (1.0, {Run: False}), (2.0, {}), (3.0, {Run: True})],
        prepare=lambda runner: Harness(runner).install(),
    )
    assert counts["loop scans"] > 0
    assert counts["states compared"] > 0


# --- Every system tag ------------------------------------------------------------------


def _reads_and_writes(tag: Any) -> Callable[[], None]:
    """A program that reads *tag*, and if it may, stores into it: a value, then the default."""

    def fill_program() -> None:
        if tag.type == TagType.BOOL:
            with Rung(tag):
                out(Lamp2)
        elif tag.type == TagType.REAL:
            with Rung():
                copy(tag, Temp)
        else:
            with Rung():
                copy(tag, N)
        with Rung():
            on_delay(T, 500)
        with Rung():
            on_delay(T1, 1_500)
        if tag.name in WRITABLE_SYSTEM_TAG_NAMES:
            with Rung(rise(T.Done)):
                if tag.type == TagType.BOOL:
                    out(tag)
                else:
                    copy(3, tag)
            with Rung(rise(T1.Done)):
                if tag.type == TagType.BOOL:
                    reset(tag)
                else:
                    copy(tag.default, tag)

    return fill_program


@pytest.mark.parametrize("name", sorted(SYSTEM_TAGS_BY_NAME))
def test_every_system_tag_read_and_written_equals_stepping(name: str, fine_loop: None) -> None:
    """Keys included: a stored point stays out of the state until its first write."""
    program = _build(_reads_and_writes(SYSTEM_TAGS_BY_NAME[name]))
    _lockstep(program, [(1.0, {Run: True}), (2.0, {})])


# --- A tag holds what was last written --------------------------------------------


Coiled = Int("TrCoiled")
CoiledReal = Real("TrCoiledReal")
Gained = Real("TrGained")
Source = Real("TrSource")


def _pulsed_coil_on_int() -> None:
    with Rung(Hit):
        out(Coiled)


def _pulsed_oneshot_on_int() -> None:
    with Rung(Hit):
        out(Coiled, oneshot=True)


def _pulsed_coil_on_real() -> None:
    with Rung(Hit):
        out(CoiledReal)


def _pulsed_coil_on_a_slot() -> None:
    with Rung(Hit):
        out(Ds[1])


def _pulsed_coil_on_a_range() -> None:
    with Rung(Hit):
        out(Ds.select(1, 3))


def _pulsed_latch_over_a_stored_one() -> None:
    with Rung(fall(system.sys.clock_1s)):
        copy(1, Coiled)
    with Rung(rise(system.sys.clock_1s)):
        reset(Coiled)
    with Rung(Hit):
        latch(Coiled)


def _pulsed_negative_gain() -> None:
    with Rung(Hit):
        copy(5.5, Source)
    with Rung(~Hit):
        copy(0.0, Source)
    with Rung():
        math(Source * -1.0, Gained)


LAST_WRITTEN = {
    "coil_on_int": _pulsed_coil_on_int,
    "oneshot_on_int": _pulsed_oneshot_on_int,
    "coil_on_real": _pulsed_coil_on_real,
    "coil_on_a_slot": _pulsed_coil_on_a_slot,
    "coil_on_a_range": _pulsed_coil_on_a_range,
    "latch_over_a_stored_one": _pulsed_latch_over_a_stored_one,
    "negative_gain": _pulsed_negative_gain,
}


@pytest.mark.parametrize("name", sorted(LAST_WRITTEN))
def test_a_stretch_between_published_states_leaves_what_stepping_does(
    name: str, fine_loop: None
) -> None:
    """A value that leaves and comes back to an equal one of another type or sign of zero,
    between two published states: 0 -> True -> False, 0.0 -> -5.5 -> -0.0."""

    def program() -> None:
        with Rung(Run):
            on_delay(T, 10_000_000)
        with Rung(rise(system.sys.clock_1s), T.Acc > 100):
            out(Hit)
        LAST_WRITTEN[name]()

    counts = _lockstep(_build(program), [(0.01, {Run: True}), (4.0, {})])
    assert counts["loop scans"] > 0


def test_stepping_publishes_the_write_that_equals_what_the_tag_held() -> None:
    def program() -> None:
        with Rung(Run):
            out(Coiled)
        with Rung():
            math(Source * -1.0, Gained)

    plc = PLC(_build(program), dt=0.01)
    plc.step()
    assert plc.state.tags[Coiled.name] is False
    assert pymath.copysign(1.0, plc.state.tags[Gained.name]) == -1.0


@pytest.mark.parametrize(
    ("old", "new", "same"),
    [
        (0, 0, True),
        (700, 350 * 2, True),
        (False, False, True),
        (1.5, 3.0 / 2, True),
        (0.0, 0.0 * 1, True),
        (-0.0, 0.0 * -1, True),
        ("a", "a", True),
        (0, False, False),
        (True, 1, False),
        (0, 0.0, False),
        (0.0, -0.0, False),
        (-0.0, 0.0, False),
        (1, 2, False),
        (float("nan"), float("nan"), False),
        (object(), 0, False),
    ],
)
def test_same_stored_value(old: Any, new: Any, same: bool) -> None:
    assert same_stored_value(old, new) is same


# --- Stop predicates --------------------------------------------------------------


STOPS: dict[str, Callable[[], Any]] = {
    "bit": lambda: (T1.Done,),
    "normally_closed": lambda: (~T1.TT,),
    "compare_ge": lambda: (T1.Acc >= 250,),
    "compare_eq": lambda: (T1.Acc == 290,),
    "compare_ne": lambda: (Step != 0,),
    "compare_lt": lambda: (T2.Acc > 0, T2.Acc < 11),
    "compare_tag": lambda: (T1.Acc > Preset,),
    "int_truthy": lambda: (Step,),
    "and": lambda: (And(Step == 1, T2.Acc >= 40),),
    "or": lambda: (Or(Step == 2, T1.Acc >= 299),),
    "rise": lambda: (rise(Lamp),),
    "fall": lambda: (fall(Lamp),),
    "block_cell_never_live": lambda: (Ds[7] == 1,),
    "block_cell": lambda: (Ds[2] >= 2,),
}


def _stop_program() -> None:
    with Rung(Step == 0):
        on_delay(T1, 300)
    with Rung(T1.Done):
        copy(1, Step)
    with Rung(Step == 1):
        on_delay(T2, 700)
    with Rung(T2.Done):
        copy(2, Step)
    with Rung(Step == 2):
        on_delay(T3, 500)
    with Rung(T3.Done):
        copy(0, Step)
    with Rung(T1.Acc >= 100):
        out(Lamp)
    with Rung(system.sys.clock_500ms):
        math(N + 1, N)
    with Rung():
        copy(N, Ds[2])
    with Rung():
        copy(Step, Ds[M])


@pytest.mark.parametrize("name", sorted(STOPS))
def test_stop_predicate_lands_on_the_stepped_scan(name: str, fine_loop: None) -> None:
    program = _build(_stop_program)
    conditions = STOPS[name]()
    plc = _plc(program, 0.01)
    twin = _plc(program, 0.01)
    plc.patch({Preset: 270, M: 5})
    twin.patch({Preset: 270, M: 5})
    before = scan_trace.TOTALS["loop scans"]
    plc.run_until(*conditions, max_cycles=400)
    twin.run_until(*conditions, max_cycles=400, jit=False)
    assert plc.state.scan_id == twin.state.scan_id
    _assert_equal(plc.state, twin.state)
    # The last judgement is the real one: on the scan the guard refused.
    assert scan_trace.TOTALS["loop scans"] > before or plc.state.scan_id <= 3


def test_stop_predicate_holds_on_every_scan_it_would_on_stepping(fine_loop: None) -> None:
    """Repeated stops: each landing, each stop judged from where the last one left."""
    program = _build(_stop_program)
    plc = _plc(program, 0.01)
    twin = _plc(program, 0.01)
    plc.patch({M: 5})
    twin.patch({M: 5})
    for _ in range(6):
        plc.run_until(rise(Lamp), max_cycles=300)
        twin.run_until(rise(Lamp), max_cycles=300, jit=False)
        assert plc.state.scan_id == twin.state.scan_id
        _assert_equal(plc.state, twin.state)


def test_stop_on_a_derived_system_tag_declines_to_plain_stepping(fine_loop: None) -> None:
    program = _build(_stop_program)
    plc = _plc(program, 0.01)
    twin = _plc(program, 0.01)
    before = Counter(scan_trace.TOTALS)
    plc.run_until(system.sys.clock_1s, max_cycles=200)
    twin.run_until(system.sys.clock_1s, max_cycles=200, jit=False)
    assert plc.state.scan_id == twin.state.scan_id
    assert scan_trace.TOTALS["loop scans"] == before["loop scans"]
    assert (
        scan_trace.TOTALS["run declined: stop predicate form"]
        > before["run declined: stop predicate form"]
    )


def test_callable_stop_predicate_keeps_stepping(fine_loop: None) -> None:
    program = _build(_stop_program)
    plc = _plc(program, 0.01)
    before = scan_trace.TOTALS["loop scans"]
    plc.run_until(lambda state: state.tags.get("TrStep") == 1, max_cycles=400)
    assert plc.state.tags["TrStep"] == 1
    assert scan_trace.TOTALS["loop scans"] == before


# --- Bounds -----------------------------------------------------------------------


def test_bounds_are_watched_on_every_scan_of_the_loop(fine_loop: None) -> None:
    Limited = Int("TrLimited", min=0, max=50)
    Choice = Int("TrChoice", choices={0: "a", 1: "b"})

    def fill_program() -> None:
        with Rung(system.sys.clock_100ms):
            math(Limited + 1, Limited)
        with Rung(T.Done):
            copy(5, Choice)
        with Rung():
            on_delay(T, 8_000)

    program = _build(fill_program)
    plc = _plc(program, 0.01)
    twin = _plc(program, 0.01)
    before = scan_trace.TOTALS["loop scans"]
    with _Observed(plc) as observed:
        plc.run_for(5.0)
    _step_to(twin, plc.state.scan_id, observed.states)
    _assert_equal(plc.state, twin.state)
    assert plc.bounds_violations.keys() == twin.bounds_violations.keys()
    assert scan_trace.TOTALS["loop scans"] > before
    # Limited passes its maximum at scan 1001 or so; the run steps from there.
    with _Observed(plc) as observed:
        plc.run_for(8.0)
    _step_to(twin, plc.state.scan_id, observed.states)
    _assert_equal(plc.state, twin.state)
    assert plc.bounds_violations.keys() == twin.bounds_violations.keys()
    assert plc.state.tags["TrLimited"] > 50
    assert plc.bounds_violations


# --- Self check -------------------------------------------------------------------


def _timer_program() -> Program:
    def fill_program() -> None:
        with Rung():
            on_delay(T, 2_000)
        with Rung():
            math(N + 3, N)

    return _build(fill_program)


def test_self_check_mismatch_fails_loudly_under_strict(
    fine_loop: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tracer disagreeing with Python about an operator is caught on its first scan."""
    wrong = dict(scan_trace._BINARY_OPERATORS)
    wrong[ast.Add] = ("-", wrong[ast.Add][1])
    monkeypatch.setattr(scan_trace, "_BINARY_OPERATORS", wrong)
    with fold_check.deliberate(), pytest.raises(SelfCheckMismatch):
        _plc(_timer_program(), 0.01).run_for(1.0)


def test_self_check_mismatch_declines_the_rest_of_the_run(
    fine_loop: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not strict: the run steps on, equal to stepping, and the loop stays off."""
    monkeypatch.setattr(scan_trace, "STRICT_SELF_CHECK", False)
    wrong = dict(scan_trace._BINARY_OPERATORS)
    wrong[ast.Add] = ("-", wrong[ast.Add][1])
    monkeypatch.setattr(scan_trace, "_BINARY_OPERATORS", wrong)
    program = _timer_program()
    plc = _plc(program, 0.01)
    twin = _plc(program, 0.01)
    before = Counter(scan_trace.TOTALS)
    with fold_check.deliberate():
        plc.run_for(1.0)
        twin.run(plc.state.scan_id)
    _assert_equal(plc.state, twin.state)
    assert scan_trace.TOTALS["self check mismatches"] == before["self check mismatches"] + 1
    assert scan_trace.TOTALS["loop scans"] == before["loop scans"]


def test_lockstep_bites_when_guards_are_removed(
    fine_loop: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without guards the first iteration is still right, so the self check passes;
    the comparison between firings has to catch it.  If this stops failing, the
    corpus above proves nothing."""
    monkeypatch.setattr(scan_trace._Tracer, "_guard", lambda *_args, **_kwargs: None)
    with fold_check.deliberate(), pytest.raises(AssertionError, match="differs from stepping"):
        _lockstep(_build(_corpus_sequencer), [(8.0, {})])


def test_block_cell_rewritten_with_its_own_value_goes_live_in_the_loop(fine_loop: None) -> None:
    """The rewrite changes no value, but the cell is in the committed state from then."""

    def fill_program() -> None:
        with Rung():
            on_delay(T, 2_000)
        with Rung(T.Acc >= 500):
            fill(0, Ds.select(1, 3))
        with Rung():
            copy(Step, Ds[N])

    counts = _lockstep(_build(fill_program), [(3.0, {N: 5})])
    assert counts["loop scans"] > 0


# --- Trace cost -------------------------------------------------------------------


def _busy_toggle() -> Program:
    def fill_program() -> None:
        with Rung(~Lamp):
            out(Lamp)
        with Rung(Lamp):
            math(N + 1, N)
        with Rung(~Lamp):
            math(M + N, M)
        with Rung(M > 100):
            copy(0, M)

    return _build(fill_program)


def test_cap_on_traces_per_run_holds(fine_loop: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan_trace, "MAX_TRACES_PER_RUN", 2)
    counts = _lockstep(_busy_toggle(), [(3.0, {})])
    assert counts["traces"] <= 2


def test_a_program_that_keeps_exiting_backs_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attempts thin out: mostly plain real scans, with few attempts to pay for."""
    monkeypatch.setattr(compiled_run, "INTERPRETED_PREFIX", 1)
    monkeypatch.setattr(scan_trace, "MIN_RUN_SCANS", 1)
    program = _busy_toggle()
    plc = _plc(program, 0.01)
    twin = _plc(program, 0.01)
    before = Counter(scan_trace.TOTALS)
    plc.run_for(100.0)
    twin.run(plc.state.scan_id)
    _assert_equal(plc.state, twin.state)
    counts = Counter(scan_trace.TOTALS)
    counts.subtract(before)
    assert counts["traces"] < 20, dict(counts)


# --- Declines ---------------------------------------------------------------------


@pytest.mark.parametrize("tag", [system.sys.scan_counter, system.sys.scan_clock_toggle])
def test_scan_derived_tag_declines_the_run(tag: Any, fine_loop: None) -> None:
    def fill_program() -> None:
        with Rung(tag):
            math(N + 1, N)
        with Rung():
            copy(tag, M)

    counts = _lockstep(_build(fill_program), [(1.0, {})])
    assert counts["loop scans"] == 0
    assert counts["run declined: scan-derived system tag"] >= 1


def test_randint_declines_the_run(fine_loop: None) -> None:
    def fill_program() -> None:
        with Rung():
            copy(randint(1, 6), N)

    counts = _lockstep(_build(fill_program), [(0.5, {})])
    assert counts["loop scans"] == 0
    assert counts["run declined: randint"] >= 1


def test_force_declines_the_run(fine_loop: None) -> None:
    counts = _lockstep(
        _build(_corpus_sequencer),
        [(1.0, {})],
        prepare=lambda plc: plc.force(Lamp2, True),
    )
    assert counts["loop scans"] == 0
    assert counts["run declined: force"] >= 1


def test_a_run_function_body_is_not_traced(fine_loop: None) -> None:
    calls = {"count": 0}

    def impure(value: int) -> dict[str, int]:
        calls["count"] += 1
        return {"out": value + 1}

    def fill_program() -> None:
        with Rung():
            run_function(impure, ins={"value": N}, outs={"out": N})

    program = _build(fill_program)
    plc = _plc(program, 0.01)
    before = scan_trace.TOTALS["loop scans"]
    plc.run_for(0.5)
    assert scan_trace.TOTALS["loop scans"] == before
    assert plc.state.tags["TrN"] == plc.state.scan_id


# --- The system tag classification --------------------------------------------------


def test_every_system_tag_is_classified_exactly_once() -> None:
    """A new system tag must be decided on: calling a moving tag constant lands wrong."""
    assert _CLASSIFIED_SYSTEM_TAGS == frozenset(SYSTEM_TAGS_BY_NAME)
    assert not MOVING_SYSTEM_TAGS & DECLINING_SYSTEM_TAGS
    assert not MOVING_SYSTEM_TAGS & CONSTANT_SYSTEM_TAGS
    assert not DECLINING_SYSTEM_TAGS & CONSTANT_SYSTEM_TAGS


def test_constant_system_tags_do_not_depend_on_time_or_scan_id() -> None:
    """Resolve every derived tag at far apart times and scans: a tag that differs and
    is not moving or declining would be read as a constant, wrongly."""
    plc = _plc(_timer_program(), 0.01)
    runtime = plc._system_runtime
    plc.run(2)
    base = plc.state

    def at(scan_id: int, timestamp: float) -> dict[str, Any]:
        moved = base.__class__(
            scan_id=scan_id, timestamp=timestamp, tags=base.tags, memory=base.memory
        )
        return {name: runtime.resolve(name, moved)[1] for name in _DERIVED_TAG_NAMES}

    samples = [at(1 + i, 0.37 + 1234.567 * i) for i in range(1, 40)]
    moves = {name for name in _DERIVED_TAG_NAMES if len({repr(s[name]) for s in samples}) > 1}
    assert moves <= MOVING_SYSTEM_TAGS | DECLINING_SYSTEM_TAGS
    # And nothing listed as moving has been quietly made constant.
    assert moves >= {system.sys.clock_1s.name, system.rtc.second.name}


# --- The tracer on source it should refuse -------------------------------------------


def _replay_stub(source: str, **tags: Any) -> tuple[KernelSource, Any]:
    kernel_artifact: Any = SimpleNamespace(source=source)
    replay = SimpleNamespace(
        _kernel=SimpleNamespace(tags=tags, blocks={}, memory={}),
        _dt=0.01,
        _block_pos={},
        _live_block_tags=set(),
    )
    return KernelSource(kernel_artifact), replay


def _step_source(*body: str) -> str:
    lines = ["def _kernel_step(tags, blocks, memory, prev, dt):", *[f"    {b}" for b in body]]
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (["while tags['A']:", "    pass"], "statement While"),
        (["x = [abs][0](1)"], "call of an expression"),
        (["x = (lambda *v: 1)()"], "lambda with more than plain parameters"),
        (["x = (lambda v=1: v)()"], "lambda with more than plain parameters"),
        (["x = (lambda v: v)"], "expression Lambda"),
        (["x = open('f')"], "call open"),
        (["x = tags.get('A')"], "method get"),
        (["x = prev['A']"], "access to prev"),
        (["x = f\"{tags['A']}\""], "expression JoinedStr"),
        (["import os"], "statement Import"),
        (["x = tags['A']", "del x"], "statement Delete"),
        (["try:", "    pass", "finally:", "    pass"], "try-finally"),
        (["x = tags['A'][0:1]"], "slice read"),
        (["x = {1: 2}"], "expression Dict"),
        (["x = y"], "name y"),
        (["x = _user_function(1)"], "call _user_function"),
    ],
)
def test_unknown_forms_decline(body: list[str], reason: str) -> None:
    source = _step_source(*body) + "def _user_function(v):\n    return v\n"
    parsed, replay = _replay_stub(source, A=1)
    with pytest.raises(Decline, match=reason):
        build_trace(parsed, replay, set(), None, {})


@pytest.mark.parametrize(
    "tags", [{"B": 0}, {"A": 1}, {}], ids=["read key gone", "written key gone", "both gone"]
)
def test_a_loop_handed_containers_that_lost_a_key_runs_no_scan(tags: dict[str, int]) -> None:
    """The real scan raises or adds the key: the loop must leave that to it, untouched."""
    source = _step_source("x = tags['A']", "tags['A'] = x + 1", "tags['B'] = x")
    parsed, replay = _replay_stub(source, A=1, B=0)
    trace = build_trace(parsed, replay, {("tags", "A"), ("tags", "B")}, None, {})
    assert trace is not None
    before = dict(tags)
    assert trace.loop(tags, {}, {}, 0.0, 0.01, 5, None) == (0, 0.0)
    assert tags == before


def test_a_loop_with_every_key_runs_its_scans() -> None:
    """The control of the one above."""
    source = _step_source("x = tags['A']", "tags['A'] = x + 1", "tags['B'] = x")
    parsed, replay = _replay_stub(source, A=1, B=0)
    trace = build_trace(parsed, replay, {("tags", "A"), ("tags", "B")}, None, {})
    assert trace is not None
    tags = {"A": 1, "B": 0}
    used, _ts = trace.loop(tags, {}, {}, 0.0, 0.01, 5, None)
    assert used == 5
    assert tags == {"A": 6, "B": 5}


def test_a_moving_value_inside_a_container_is_not_lost() -> None:
    """``max`` over a list holding a moving value: stripping the mark would freeze it."""
    source = _step_source(
        "a = tags['A']",
        "tags['A'] = a + 1",
        "tags['B'] = max([a, 3])",
    )
    parsed, replay = _replay_stub(source, A=1, B=3)
    with pytest.raises(Decline, match="moving value"):
        build_trace(parsed, replay, {("tags", "A"), ("tags", "B")}, None, {})


def test_a_moving_iterable_declines() -> None:
    source = _step_source("a = tags['A']", "tags['A'] = a + 1", "for i in range(a):", "    pass")
    parsed, replay = _replay_stub(source, A=1)
    with pytest.raises(Decline, match="moving iterable"):
        build_trace(parsed, replay, {("tags", "A")}, None, {})


def test_a_kernel_global_rebound_elsewhere_is_no_constant() -> None:
    source = "SCALE = 3\ndef _sub_rescale(tags):\n    global SCALE\n    SCALE = 4\n" + _step_source(
        "tags['A'] = SCALE"
    )
    parsed, replay = _replay_stub(source, A=3)
    with pytest.raises(Decline, match="name SCALE"):
        build_trace(parsed, replay, set(), None, {})


def test_chains_membership_and_identity_trace_exactly() -> None:
    """Forms the prototype declined: run the loop against the plain Python scan."""
    source = _step_source(
        "a = tags['A']",
        "tags['A'] = a + 1",
        "tags['B'] = 0 <= a < 5",
        "tags['C'] = a in (1, 2, 3)",
        "tags['D'] = a is None",
        "tags['E'] = type(a) is int",
    )
    parsed, replay = _replay_stub(source, A=1, B=True, C=True, D=False, E=True)
    trace = build_trace(parsed, replay, {("tags", "A")}, None, {})
    tags = dict(replay._kernel.tags)
    used, timestamp = trace.loop(tags, {}, {}, 0.0, 0.01, 10, None)
    assert used == 3  # A reaches 4: that scan leaves the traced membership
    assert tags["A"] == 4
    assert timestamp == pytest.approx(0.03)


def test_a_comprehension_target_stays_in_the_comprehension() -> None:
    """Python scopes the target to the list: the local of that name reads as before."""
    source = _step_source(
        "x = tags['A']",
        "tags['A'] = x + 1",
        "y = [x for x in range(3)]",
        "tags['B'] = x",
    )
    parsed, replay = _replay_stub(source, A=1, B=0)
    trace = build_trace(parsed, replay, {("tags", "A"), ("tags", "B")}, None, {})
    tags = dict(replay._kernel.tags)
    used, _timestamp = trace.loop(tags, {}, {}, 0.0, 0.01, 5, None)
    assert used == 5
    assert tags == {"A": 6, "B": 5}


def test_a_comprehension_target_named_as_a_global_declines() -> None:
    source = "SCALE = 3\n" + _step_source(
        "global SCALE",
        "y = [SCALE for SCALE in range(3)]",
        "tags['A'] = y[0]",
    )
    parsed, replay = _replay_stub(source, A=0)
    with pytest.raises(Decline, match="comprehension target|name SCALE"):
        build_trace(parsed, replay, set(), None, {})


def test_a_load_that_raises_is_a_failed_guard_not_a_crash() -> None:
    """A key gone from the dicts: the firing runs no scans, and a real scan takes over."""
    source = _step_source("a = tags['A']", "tags['A'] = a + 1")
    parsed, replay = _replay_stub(source, A=1)
    trace = build_trace(parsed, replay, {("tags", "A")}, None, {})
    assert trace.loop({}, {}, {}, 0.0, 0.01, 5, None)[0] == 0


def test_values_compare_by_type_and_sign() -> None:
    same = scan_trace.same
    assert not same(1, True)
    assert not same(1, 1.0)
    assert not same(0.0, -0.0)
    assert same(float("nan"), float("nan"))
    assert same(3, 3)
    assert same(scan_trace.MISSING, scan_trace.MISSING)
