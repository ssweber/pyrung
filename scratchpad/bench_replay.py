"""Benchmark: compiled vs classic replay for single lookup and range replay.

Run with: uv run python scratchpad/bench_replay.py
"""

from __future__ import annotations

import statistics
import time

from pyrung import (
    PLC,
    And,
    Bool,
    Counter,
    Int,
    Or,
    Real,
    Rung,
    Timer,
    branch,
    copy,
    count_up,
    latch,
    on_delay,
    out,
    program,
    reset,
    rise,
)


# ---------------------------------------------------------------------------
# Programs
# ---------------------------------------------------------------------------


def _make_conveyor() -> PLC:
    """click_conveyor.py logic with a simulated sort cycle."""
    StartBtn = Bool("StartBtn")
    StopBtn = Bool("StopBtn")
    EstopOK = Bool("EstopOK")
    Auto = Bool("Auto")
    Manual = Bool("Manual")
    EntrySensor = Bool("EntrySensor")
    DiverterBtn = Bool("DiverterBtn")
    BinASensor = Bool("BinASensor")
    BinBSensor = Bool("BinBSensor")
    ConveyorMotor = Bool("ConveyorMotor")
    DiverterCmd = Bool("DiverterCmd")
    StatusLight = Bool("StatusLight")
    Running = Bool("Running")
    IsLarge = Bool("IsLarge")
    CountReset = Bool("CountReset")
    State = Int("State")
    SizeReading = Int("SizeReading")
    SizeThreshold = Int("SizeThreshold")
    DetTimer = Timer.clone("DetTimer")
    HoldTimer = Timer.clone("HoldTimer")
    BinACounter = Counter.clone("BinACounter")
    BinBCounter = Counter.clone("BinBCounter")

    IDLE, DETECTING, SORTING, RESETTING = 0, 1, 2, 3

    @program
    def logic():
        with Rung(StartBtn, Or(Auto, Manual)):
            latch(Running)
        with Rung(~StopBtn):
            reset(Running)
        with Rung(~EstopOK):
            reset(Running)
        with Rung(EstopOK):
            with branch(Running):
                out(ConveyorMotor)
            with branch(Running):
                out(StatusLight)
        with Rung(State == IDLE, rise(EntrySensor)):
            copy(DETECTING, State)
        with Rung(State == DETECTING):
            on_delay(DetTimer, 500)
        with Rung(State == DETECTING, SizeReading > SizeThreshold):
            latch(IsLarge)
        with Rung(DetTimer.Done):
            copy(SORTING, State)
        with Rung(State == SORTING):
            on_delay(HoldTimer, 2000)
        with Rung(HoldTimer.Done):
            copy(RESETTING, State)
        with Rung(State == RESETTING):
            reset(IsLarge)
            copy(IDLE, State)
        with Rung(
            EstopOK,
            Or(And(State == SORTING, IsLarge, Auto), And(Manual, DiverterBtn)),
        ):
            out(DiverterCmd)
        with Rung(rise(BinASensor)):
            count_up(BinACounter, preset=9999).reset(CountReset)
        with Rung(rise(BinBSensor)):
            count_up(BinBCounter, preset=9999).reset(CountReset)

    runner = PLC(logic, dt=0.010)
    runner.patch({"StopBtn": True, "EstopOK": True, "Auto": True, "SizeThreshold": 100})
    runner.patch({"StartBtn": True})
    runner.step()
    runner.patch({"StartBtn": False})
    runner.force("EntrySensor", True)
    runner.force("SizeReading", 150)
    return runner


def _make_busy() -> PLC:
    """Synthetic busy program: 20 rungs, 8 timers, 4 counters, math, branches."""
    enables = [Bool(f"E{i}") for i in range(20)]
    outputs = [Bool(f"O{i}") for i in range(20)]
    timers = [Timer.clone(f"T{i}") for i in range(8)]
    counters = [Counter.clone(f"C{i}") for i in range(4)]
    accum = [Int(f"A{i}") for i in range(4)]
    temp = [Real(f"R{i}") for i in range(4)]

    @program(strict=False)
    def logic():
        for i in range(8):
            with Rung(enables[i]):
                on_delay(timers[i], preset=200 + i * 50)
        for i in range(4):
            with Rung(rise(enables[8 + i])):
                count_up(counters[i], preset=999).reset(enables[12 + i])
        for i in range(4):
            with Rung(enables[16 + i]):
                with branch(timers[i].Done):
                    out(outputs[i])
                with branch(timers[i + 4].Done):
                    out(outputs[i + 4])
                copy(accum[i] + 1, accum[i])
                copy(temp[i] + 0.1, temp[i])
        for i in range(8, 20):
            with Rung(enables[i % 8]):
                out(outputs[i])

    runner = PLC(logic, dt=0.010)
    patches = {f"E{i}": True for i in range(20)}
    runner.patch(patches)
    return runner


# ---------------------------------------------------------------------------
# Benchmark harness
# ---------------------------------------------------------------------------

WARMUP = 2
ITERATIONS = 20


def _flush_cache(plc: PLC) -> None:
    plc._recent_state_cache.clear()
    plc._recent_state_cache_bytes = 0
    plc._cache_state(plc.current_state)


def _bench(fn, label: str) -> float:
    for _ in range(WARMUP):
        fn()
    times = []
    for _ in range(ITERATIONS):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    med = statistics.median(times)
    p95 = sorted(times)[int(len(times) * 0.95)]
    print(f"  {label:40s}  median {med*1000:8.2f} ms   p95 {p95*1000:8.2f} ms")
    return med


def bench_program(name: str, make_fn, total_scans: int) -> dict:
    print(f"\n{'='*60}")
    print(f"  {name}  ({total_scans} scans)")
    print(f"{'='*60}")

    plc = make_fn()
    plc.run(cycles=total_scans)
    tip = plc.current_state.scan_id

    # Pick a target far enough back that it must replay (not cached)
    target_early = max(plc._initial_scan_id + 1, tip - total_scans + 10)
    target_mid = (plc._initial_scan_id + tip) // 2

    range_start = target_mid - 25
    range_end = target_mid + 25

    results = {}

    # --- Single lookup: early ---
    _flush_cache(plc)
    t_classic = _bench(lambda: plc._replay_to_classic(target_early), f"replay_to({target_early}) classic")
    _flush_cache(plc)
    t_compiled = _bench(lambda: plc.replay_to(target_early), f"replay_to({target_early}) compiled")
    results["single_early"] = (t_classic, t_compiled)

    # --- Single lookup: mid ---
    _flush_cache(plc)
    t_classic = _bench(lambda: plc._replay_to_classic(target_mid), f"replay_to({target_mid}) classic")
    _flush_cache(plc)
    t_compiled = _bench(lambda: plc.replay_to(target_mid), f"replay_to({target_mid}) compiled")
    results["single_mid"] = (t_classic, t_compiled)

    # --- Range replay ---
    _flush_cache(plc)
    t_classic = _bench(
        lambda: plc._replay_range_classic(range_start, range_end),
        f"range({range_start}..{range_end}) classic",
    )
    _flush_cache(plc)
    t_compiled = _bench(
        lambda: plc._replay_range(range_start, range_end),
        f"range({range_start}..{range_end}) compiled",
    )
    results["range"] = (t_classic, t_compiled)

    return results


def _print_summary(all_results: dict) -> None:
    print(f"\n{'='*60}")
    print("  Summary — speedup (classic / compiled)")
    print(f"{'='*60}")
    for prog_name, results in all_results.items():
        print(f"\n  {prog_name}:")
        for bench_name, (classic, compiled) in results.items():
            speedup = classic / compiled if compiled > 0 else float("inf")
            print(f"    {bench_name:20s}  {speedup:5.1f}x")


def main() -> None:
    all_results = {}
    all_results["click_conveyor"] = bench_program("click_conveyor", _make_conveyor, 1000)
    all_results["busy_synthetic"] = bench_program("busy_synthetic", _make_busy, 1000)

    # Larger history for the busy program
    all_results["busy_5k"] = bench_program("busy_synthetic (5k scans)", _make_busy, 5000)

    _print_summary(all_results)


if __name__ == "__main__":
    main()
