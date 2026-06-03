"""Benchmark: split_at on nearly-independent zones sharing a coupling tag.

Each zone has a Bool enable and Int mode selector, but all zones share a
single stateful AutoMode Bool.  Without split_at, the shared tag prevents
automatic factoring (one partition group).  With split_at=["AutoMode"],
the prover promotes AutoMode to nondeterministic, the remaining inputs
become independent, and factored BFS splits them into per-zone groups.

Usage:
    uv run python bench/bench_split_at.py
"""

from __future__ import annotations

import time

from pyrung import Bool, Int, Timer, latch, on_delay, out, program, reset, rise, rung
from pyrung.core.analysis.prove import Intractable, reachable_states


def build_program(n_zones: int):
    auto_mode = Bool("AutoMode")
    set_auto = Bool("SetAuto", external=True)

    enables = {}
    modes = {}
    outputs = {}
    alarms = {}
    timers = {}

    for i in range(n_zones):
        z = f"Z{i}"
        enables[i] = Bool(f"{z}_Enable", external=True)
        modes[i] = Int(f"{z}_Mode", external=True, choices={0: "Off", 1: "Run", 2: "Hold", 3: "Manual"})
        outputs[i] = Bool(f"{z}_Active")
        alarms[i] = Bool(f"{z}_Alarm")
        timers[i] = Timer.clone(f"{z}_FaultTimer")

    @program(strict=False)
    def logic():
        with rung(set_auto):
            latch(auto_mode)

        for i in range(n_zones):
            with rung(enables[i], auto_mode, modes[i] == 1):
                latch(outputs[i])
            with rung(enables[i], auto_mode, modes[i] == 2):
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

    print(f"Zones: {n_zones}  |  Free inputs: {2 * n_zones + 1} (shared AutoMode)")

    results = {}
    for label, split_at in [
        ("split_at ON ", ["AutoMode"]),
        ("split_at OFF", None),
    ]:
        t0 = time.perf_counter()
        result = reachable_states(
            logic,
            project=project,
            depth_budget=depth,
            split_at=split_at,
        )
        elapsed = time.perf_counter() - t0
        if isinstance(result, Intractable):
            print(f"  {label}:  INTRACTABLE  ({elapsed:.3f}s)")
        else:
            print(f"  {label}:  {len(result):>5} states  ({elapsed:.3f}s)")
            results[label.strip()] = elapsed

    if len(results) == 2:
        on_t = results["split_at ON"]
        off_t = results["split_at OFF"]
        print(f"  speedup:  {off_t / on_t:.1f}x")
    print()


if __name__ == "__main__":
    print("=" * 60)
    print("split_at benchmark: coupled zones with shared AutoMode")
    print("=" * 60)
    print()

    for zones in [2, 3, 4, 5]:
        run_bench(n_zones=zones)
