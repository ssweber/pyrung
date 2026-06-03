"""Profile pyrung_to_ladder on the PackML benchmark program.

Usage:
    uv run python bench/bench_ladder.py              # top-30 cumulative
    uv run python bench/bench_ladder.py --callers     # callers/callees view
    uv run python bench/bench_ladder.py --save        # save .prof for snakeviz
    uv run python bench/bench_ladder.py --iters 100   # repeat for stable numbers
"""

from __future__ import annotations

import argparse
import cProfile
import pstats
import sys
import time

sys.path.insert(0, ".")


def main():
    parser = argparse.ArgumentParser(description="Profile pyrung_to_ladder")
    parser.add_argument("--iters", type=int, default=50, help="number of iterations")
    parser.add_argument("--callers", action="store_true", help="show callers/callees")
    parser.add_argument("--save", action="store_true", help="save .prof file")
    parser.add_argument(
        "--sort",
        default="cumulative",
        choices=["cumulative", "tottime", "calls"],
        help="sort key",
    )
    parser.add_argument("--lines", type=int, default=30, help="number of lines to show")
    parser.add_argument("--no-validate", action="store_true", help="skip validation")
    args = parser.parse_args()

    from tests.click.test_ladder_realistic import _build_program_and_mapping

    logic, mapping = _build_program_and_mapping()

    from pyrung.click.ladder import pyrung_to_ladder

    validate = not args.no_validate
    pyrung_to_ladder(logic, mapping, validate=validate)

    t0 = time.perf_counter()
    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(args.iters):
        pyrung_to_ladder(logic, mapping, validate=validate)
    profiler.disable()
    elapsed = time.perf_counter() - t0

    print(f"\n{'=' * 60}")
    print(f"pyrung_to_ladder × {args.iters}  =  {elapsed:.3f}s  ({elapsed / args.iters * 1000:.1f} ms/call)")
    print(f"{'=' * 60}\n")

    stats = pstats.Stats(profiler)
    stats.strip_dirs()

    if args.callers:
        stats.sort_stats(args.sort)
        stats.print_callers(args.lines)
    else:
        stats.sort_stats(args.sort)
        stats.print_stats(args.lines)

    if args.save:
        out = "bench/bench_ladder.prof"
        profiler.dump_stats(out)
        print(f"\nSaved to {out}  (open with: snakeviz {out})")


if __name__ == "__main__":
    main()
