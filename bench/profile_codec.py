"""Profile pyrung_to_ladder and ladder_to_pyrung round-trip.

packml_bench uses indirect addressing (dh[idx]) which cannot pass
Click ladder export validation, so we use click_conveyor for the
pyrung_to_ladder direction and feed the resulting bundle back through
ladder_to_pyrung.

Usage:
    uv run python bench/profile_codec.py
"""

import cProfile
import pstats
import sys
import time

sys.path.insert(0, "src")
sys.path.insert(0, "examples")

from click_conveyor import logic, mapping  # noqa: E402
from pyrung.click.codegen import ladder_to_pyrung  # noqa: E402
from pyrung.click.ladder import pyrung_to_ladder  # noqa: E402


def bench_to_ladder(n: int = 200):
    start = time.perf_counter()
    result = None
    for _ in range(n):
        result = pyrung_to_ladder(logic, mapping)
    elapsed = time.perf_counter() - start
    print(f"pyrung_to_ladder: {n} iters in {elapsed:.3f}s  ({n / elapsed:.0f} iter/s, {elapsed / n * 1000:.1f}ms each)")
    return result


def bench_to_pyrung(bundle, n: int = 200):
    start = time.perf_counter()
    result = ""
    for _ in range(n):
        result = ladder_to_pyrung(bundle)
    elapsed = time.perf_counter() - start
    print(f"ladder_to_pyrung: {n} iters in {elapsed:.3f}s  ({n / elapsed:.0f} iter/s, {elapsed / n * 1000:.1f}ms each)")
    return result


def profile_section(name, fn, n, prof_path):
    profiler = cProfile.Profile()
    profiler.enable()
    result = fn(n)
    profiler.disable()

    stats = pstats.Stats(profiler)
    stats.strip_dirs()
    print(f"\nTOP 30 BY CUMULATIVE TIME ({name})")
    stats.sort_stats("cumulative")
    stats.print_stats(30)
    print(f"TOP 30 BY SELF TIME ({name})")
    stats.sort_stats("tottime")
    stats.print_stats(30)

    profiler.dump_stats(prof_path)
    return result


def main():
    n = 200

    # Warmup
    print("Warming up...")
    bundle = pyrung_to_ladder(logic, mapping)
    ladder_to_pyrung(bundle)

    print(f"\n{'=' * 80}")
    print(f"PROFILING pyrung_to_ladder ({n} iterations)")
    print(f"{'=' * 80}")
    bundle = profile_section("pyrung_to_ladder", bench_to_ladder, n, "bench/bench_to_ladder.prof")

    print(f"\n{'=' * 80}")
    print(f"PROFILING ladder_to_pyrung ({n} iterations)")
    print(f"{'=' * 80}")
    profile_section("ladder_to_pyrung", lambda n: bench_to_pyrung(bundle, n), n, "bench/bench_to_pyrung.prof")

    print("\nProfiles saved to bench/bench_to_ladder.prof and bench/bench_to_pyrung.prof")
    print("Visualize with: uv run snakeviz bench/bench_to_ladder.prof")


if __name__ == "__main__":
    main()
