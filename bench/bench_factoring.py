"""Benchmark: free-input factoring on independent multi-zone programs.

Each zone has an Int mode selector (4 choices) and a Bool enable — fully
independent of the other zones.  Unfactored BFS evaluates the full
cross-product of all inputs per state; factored BFS evaluates per-group
combos independently and composes via delta merge.

Usage:
    uv run python bench/bench_factoring.py
"""

from __future__ import annotations

import time

from pyrung import Bool, Int, Timer, latch, on_delay, out, program, reset, rise, rung
from pyrung.core.analysis.prove import Intractable, reachable_states
from pyrung.core.analysis.prove.passes import _OptConfig


def build_program(n_zones: int):
    modes = {}
    enables = {}
    outputs = {}
    alarms = {}
    timers = {}

    for i in range(n_zones):
        z = f"Z{i}"
        modes[i] = Int(f"{z}_Mode", external=True, choices={0: "Off", 1: "Run", 2: "Hold", 3: "Manual"})
        enables[i] = Bool(f"{z}_Enable", external=True)
        outputs[i] = Bool(f"{z}_Active")
        alarms[i] = Bool(f"{z}_Alarm")
        timers[i] = Timer.clone(f"{z}_FaultTimer")

    @program(strict=False)
    def logic():
        for i in range(n_zones):
            with rung(enables[i], modes[i] == 1):
                latch(outputs[i])
            with rung(enables[i], modes[i] == 2):
                out(outputs[i])
            with rung(~enables[i]):
                reset(outputs[i])
            with rung(outputs[i], ~enables[i]):
                on_delay(timers[i], 1000)
            with rung(timers[i].Done):
                latch(alarms[i])
            with rung(rise(enables[i])):
                reset(alarms[i])

    return logic


def run_bench(n_zones: int, depth: int = 8):
    logic = build_program(n_zones)
    project = [f"Z{i}_Active" for i in range(n_zones)] + [f"Z{i}_Alarm" for i in range(n_zones)]
    combos = (4**n_zones) * (2**n_zones)

    print(f"Zones: {n_zones}  |  Free inputs: {2 * n_zones}  |  Combos/state: {combos}")

    results = {}
    for label, opt in [
        ("factoring ON ", _OptConfig(free_input_factoring=True)),
        ("factoring OFF", _OptConfig(free_input_factoring=False)),
    ]:
        t0 = time.perf_counter()
        result = reachable_states(logic, project=project, depth_budget=depth, _opt_config=opt)
        elapsed = time.perf_counter() - t0
        if isinstance(result, Intractable):
            print(f"  {label}:  INTRACTABLE  ({elapsed:.3f}s)")
        else:
            print(f"  {label}:  {len(result):>5} states  ({elapsed:.3f}s)")
            results[label.strip()] = elapsed

    if len(results) == 2:
        on_t = results["factoring ON"]
        off_t = results["factoring OFF"]
        print(f"  speedup:  {off_t / on_t:.1f}x")
    print()


if __name__ == "__main__":
    print("=" * 60)
    print("Free-input factoring benchmark")
    print("=" * 60)
    print()

    for zones in [2, 3, 4, 5]:
        run_bench(n_zones=zones)
