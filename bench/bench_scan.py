"""Benchmark and profile the history-bearing interpreted scan loop.

The default case reuses one PackML runner.  ``--pilot`` repeatedly forks a
warm runner and drives each child through ``run_until(..., fold=False)``, which
models pilot's short-lived trial forks while keeping the measurement focused on
real interpreted scans rather than fold detection.

Usage:
    uv run python bench/bench_scan.py
    uv run python bench/bench_scan.py --pilot --scans 200 --forks 40
    uv run python bench/bench_scan.py --profile --scans 500
"""

from __future__ import annotations

import argparse
import cProfile
import gc
import math
import pstats
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from examples.packml_bench import logic
from pyrung import PLC


def _new_runner() -> PLC:
    # Pilot forks deliberately retain every state so cause() never has to replay
    # an evicted trial history.  Benchmark the same cache policy here.
    return PLC(logic, history_budget=math.inf)


def _time_plain(scans: int) -> float:
    plc = _new_runner()
    plc.run(20)
    gc.collect()
    gc.disable()
    try:
        start = time.perf_counter()
        plc.run(scans)
        return (time.perf_counter() - start) * 1_000_000 / scans
    finally:
        gc.enable()


def _time_pilot(scans: int, forks: int) -> float:
    root = _new_runner()
    root.run(20)
    gc.collect()
    gc.disable()
    try:
        start = time.perf_counter()
        for _ in range(forks):
            child = root.fork(history_budget=math.inf, inherit_log=False)
            child.run_until(lambda _state: False, max_cycles=scans, fold=False)
        return (time.perf_counter() - start) * 1_000_000 / (scans * forks)
    finally:
        gc.enable()


def _profile(scans: int, lines: int) -> None:
    plc = _new_runner()
    plc.run(20)
    profiler = cProfile.Profile()
    profiler.enable()
    plc.run(scans)
    profiler.disable()
    pstats.Stats(profiler).strip_dirs().sort_stats("cumulative").print_stats(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scans", type=int, default=2_000, help="scans per measured runner")
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--pilot", action="store_true", help="benchmark fork + run_until trials")
    parser.add_argument("--forks", type=int, default=40, help="trial forks per pilot repeat")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--lines", type=int, default=40, help="profile rows to print")
    args = parser.parse_args()

    if args.scans < 1 or args.repeats < 1 or args.forks < 1:
        parser.error("--scans, --repeats, and --forks must be positive")

    if args.profile:
        _profile(args.scans, args.lines)
        return

    timer = _time_pilot if args.pilot else _time_plain
    samples = [
        timer(args.scans, args.forks) if args.pilot else timer(args.scans)
        for _ in range(args.repeats)
    ]
    label = "pilot fork + run_until" if args.pilot else "interpreted run"
    print(f"{label}: {[round(sample, 1) for sample in samples]} us/scan")
    print(f"median={statistics.median(samples):.1f} us/scan min={min(samples):.1f} us/scan")


if __name__ == "__main__":
    main()
