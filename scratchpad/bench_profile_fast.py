"""Profile step_replay() to see remaining overhead breakdown.

Run with: uv run python scratchpad/bench_profile_fast.py
"""

from __future__ import annotations

import cProfile
import pstats
import time

from pyrung import Bool, Counter, Int, Real, Rung, Timer, branch, copy, count_up, on_delay, out, program, rise
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


def main():
    prog = _make_busy_program()
    compiled = compile_kernel(prog)
    plc = CompiledPLC(prog, dt=0.010, compiled=compiled)
    plc.patch({f"E{i}": True for i in range(20)})

    # Warm up
    for _ in range(20):
        plc.step_replay()

    print("=" * 60)
    print("  Profile: step_replay() x 500")
    print("=" * 60)

    prof = cProfile.Profile()
    prof.enable()
    for _ in range(500):
        plc.step_replay()
    prof.disable()

    stats = pstats.Stats(prof)
    stats.strip_dirs()

    stats.sort_stats("tottime")
    print("\n  Top 20 by self time:")
    stats.print_stats(20)

    # Also time it raw
    times = []
    for _ in range(10):
        t0 = time.perf_counter()
        for _ in range(500):
            plc.step_replay()
        times.append(time.perf_counter() - t0)

    import statistics
    med = statistics.median(times)
    print(f"\n  step_replay() x 500:  median {med*1000:.2f} ms")
    print(f"  per-step:             {med*1000/500:.4f} ms ({med*1e6/500:.1f} us)")

    # Compare with step()
    plc2 = CompiledPLC(prog, dt=0.010, compiled=compiled)
    plc2.patch({f"E{i}": True for i in range(20)})
    for _ in range(20):
        plc2.step()

    times2 = []
    for _ in range(10):
        t0 = time.perf_counter()
        for _ in range(500):
            plc2.step()
        times2.append(time.perf_counter() - t0)

    med2 = statistics.median(times2)
    print(f"\n  step() x 500:         median {med2*1000:.2f} ms")
    print(f"  per-step:             {med2*1000/500:.4f} ms ({med2*1e6/500:.1f} us)")
    print(f"\n  step_replay / step:   {med/med2:.2f}x  ({(1 - med/med2)*100:.0f}% faster)")


if __name__ == "__main__":
    main()
