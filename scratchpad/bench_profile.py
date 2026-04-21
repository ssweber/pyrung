"""Step 1 & 2: isolate compilation cost and profile CompiledPLC.step().

Run with: uv run python scratchpad/bench_profile.py
"""

from __future__ import annotations

import cProfile
import pstats
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
from pyrung.circuitpy.codegen import compile_kernel
from pyrung.core import CompiledPLC


def _make_busy_program():
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

    return logic


def step1_compilation_cost():
    print("=" * 60)
    print("  Step 1: Compilation cost")
    print("=" * 60)

    prog = _make_busy_program()

    times = []
    for _ in range(10):
        t0 = time.perf_counter()
        compile_kernel(prog)
        times.append(time.perf_counter() - t0)

    med = sorted(times)[len(times) // 2]
    print(f"  compile_kernel()      median {med*1000:8.2f} ms  (10 runs)")

    compiled = compile_kernel(prog)
    plc = CompiledPLC(prog, dt=0.010, compiled=compiled)
    plc.patch({f"E{i}": True for i in range(20)})

    times = []
    for _ in range(10):
        t0 = time.perf_counter()
        plc.run(cycles=500)
        times.append(time.perf_counter() - t0)

    med = sorted(times)[len(times) // 2]
    print(f"  run(500) compiled     median {med*1000:8.2f} ms  (10 runs)")
    print(f"  per-step compiled            {med*1000/500:8.4f} ms")

    plc_classic = PLC(prog, dt=0.010)
    plc_classic.patch({f"E{i}": True for i in range(20)})

    times = []
    for _ in range(10):
        fork = plc_classic.replay_to(plc_classic.current_state.scan_id)
        fork._replay_mode = False
        t0 = time.perf_counter()
        fork.run(cycles=500)
        times.append(time.perf_counter() - t0)

    med_classic = sorted(times)[len(times) // 2]
    print(f"  run(500) classic      median {med_classic*1000:8.2f} ms  (10 runs)")
    print(f"  per-step classic             {med_classic*1000/500:8.4f} ms")
    print(f"  steady-state ratio           {med_classic/med:5.1f}x")


def step2_profile():
    print("\n" + "=" * 60)
    print("  Step 2: cProfile of CompiledPLC.step() x 500")
    print("=" * 60)

    prog = _make_busy_program()
    compiled = compile_kernel(prog)
    plc = CompiledPLC(prog, dt=0.010, compiled=compiled)
    plc.patch({f"E{i}": True for i in range(20)})
    plc.run(cycles=10)

    prof = cProfile.Profile()
    prof.enable()
    plc.run(cycles=500)
    prof.disable()

    stats = pstats.Stats(prof)
    stats.strip_dirs()
    stats.sort_stats("cumulative")
    print("\n  Top 25 by cumulative time:")
    stats.print_stats(25)

    stats.sort_stats("tottime")
    print("\n  Top 25 by self time:")
    stats.print_stats(25)


def check_prev_usage():
    print("\n" + "=" * 60)
    print("  Step 2 supplement: does step_fn use memory['_prev:*']?")
    print("=" * 60)

    prog = _make_busy_program()
    compiled = compile_kernel(prog)

    has_prev_memory = "_prev:" in compiled.source
    has_memory_read = "memory[" in compiled.source
    print(f"  '_prev:' in source:     {has_prev_memory}")
    print(f"  'memory[' in source:    {has_memory_read}")

    if has_memory_read:
        for i, line in enumerate(compiled.source.splitlines(), 1):
            if "memory[" in line and "_prev:" in line:
                print(f"    Line {i}: {line.strip()}")


if __name__ == "__main__":
    step1_compilation_cost()
    step2_profile()
    check_prev_usage()
