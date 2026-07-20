"""Benchmark the stable-pattern comparison in ``RungFiringTimelines.append``.

The production path owns an immutable ``PMap`` but receives a small mutable
capture dict on every scan.  This probe compares the current per-key PMap
lookup with plausible alternatives before adding any storage to the timeline.

Run the focused microbenchmark:

    uv run python -m scratchpad.burner.benchmark_rung_firing_compare

Collect the write-count distribution from the real BurnerLoop workload:

    uv run python -m scratchpad.burner.benchmark_rung_firing_compare --burner
"""

from __future__ import annotations

import argparse
import gc
import importlib
import time
import timeit
import tracemalloc
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pyrsistent import PMap, pmap

import pyrung.core.rung_firings as rung_firings

_WRITE_COUNTS = (0, 1, 2, 4, 8, 16, 32)


@dataclass(frozen=True)
class Timing:
    """Best-of-repeat time for one comparator and write count."""

    comparator: str
    write_count: int
    match: bool
    nanoseconds_per_call: float


def _dict_loop(pattern: Mapping[str, Any], writes: Mapping[str, Any]) -> bool:
    if len(pattern) != len(writes):
        return False
    for name, value in writes.items():
        if pattern.get(name, rung_firings._FIRED_ONLY_SENTINEL) != value:
            return False
    return True


def _hybrid(pattern: PMap, writes: Mapping[str, Any]) -> bool:
    """Keep the cheap tiny-map path; use native equality for larger maps."""
    if len(pattern) != len(writes):
        return False
    if len(writes) <= 1:
        for name, value in writes.items():
            if pattern.get(name, rung_firings._FIRED_ONLY_SENTINEL) != value:
                return False
        return True
    return pattern == writes


def _time_case(
    name: str,
    comparator: Callable[[], bool],
    *,
    write_count: int,
    match: bool,
    calls: int,
    repeats: int,
) -> Timing:
    assert comparator() is match
    elapsed = min(timeit.repeat(comparator, number=calls, repeat=repeats))
    return Timing(
        comparator=name,
        write_count=write_count,
        match=match,
        nanoseconds_per_call=elapsed * 1e9 / calls,
    )


def _allocated_per_pattern(write_count: int, *, mirrored: bool, patterns: int) -> float:
    names = tuple(f"tag_{index}" for index in range(write_count))
    gc.collect()
    tracemalloc.start()
    before, _ = tracemalloc.get_traced_memory()
    retained: list[PMap | tuple[PMap, dict[str, int]]] = []
    for seed in range(patterns):
        writes = {name: seed * write_count + index for index, name in enumerate(names)}
        immutable = pmap(writes)
        retained.append((immutable, writes) if mirrored else immutable)
    after, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(retained) == patterns
    return (after - before) / patterns


def run_microbenchmark(calls: int, repeats: int, memory_patterns: int) -> None:
    print(f"best of {repeats}; {calls:,} calls per timing")
    print("case       writes  comparator          ns/call  vs-current")
    print("---------- ------  ------------------  -------  ----------")
    for match in (True, False):
        for write_count in _WRITE_COUNTS:
            writes = {f"tag_{index}": index for index in range(write_count)}
            immutable = pmap(writes)
            mirror = dict(writes)
            if not match and write_count:
                writes[f"tag_{write_count - 1}"] = -1
            elif not match:
                writes["unexpected"] = -1

            comparators = (
                (
                    "current-pmap-loop",
                    lambda immutable=immutable, writes=writes: rung_firings._same_writes(
                        immutable, writes
                    ),
                ),
                ("pmap-equality", lambda immutable=immutable, writes=writes: immutable == writes),
                (
                    "zero-storage-hybrid",
                    lambda immutable=immutable, writes=writes: _hybrid(immutable, writes),
                ),
                (
                    "cached-dict-loop",
                    lambda mirror=mirror, writes=writes: _dict_loop(mirror, writes),
                ),
                ("cached-dict-eq", lambda mirror=mirror, writes=writes: mirror == writes),
            )
            timings = [
                _time_case(
                    name,
                    comparator,
                    write_count=write_count,
                    match=match,
                    calls=calls,
                    repeats=repeats,
                )
                for name, comparator in comparators
            ]
            current = timings[0].nanoseconds_per_call
            label = "match" if match else "late-miss"
            for timing in timings:
                print(
                    f"{label:10} {write_count:6d}  {timing.comparator:18} "
                    f"{timing.nanoseconds_per_call:7.1f}  "
                    f"{timing.nanoseconds_per_call / current:9.2f}x"
                )

    print(f"\napproximate retained allocation; {memory_patterns:,} distinct patterns")
    print("writes  current B/pattern  mirrored B/pattern  added B/pattern")
    print("------  -----------------  ------------------  ---------------")
    for write_count in _WRITE_COUNTS:
        current = _allocated_per_pattern(
            write_count,
            mirrored=False,
            patterns=memory_patterns,
        )
        mirrored = _allocated_per_pattern(
            write_count,
            mirrored=True,
            patterns=memory_patterns,
        )
        print(f"{write_count:6d}  {current:17.1f}  {mirrored:18.1f}  {mirrored - current:15.1f}")


def run_burner_histogram(max_scans: int, wall_seconds: float) -> None:
    from pyrung import PLC
    from pyrung.core.analysis.pilot import pilot_events

    histogram: Counter[tuple[int, bool]] = Counter()
    original = rung_firings._same_writes

    def observed(pattern: PMap, writes: Mapping[str, Any]) -> bool:
        matched = original(pattern, writes)
        histogram[len(writes), matched] += 1
        return matched

    rung_firings._same_writes = observed
    started = time.perf_counter()
    finished = False
    try:
        logic = importlib.import_module("tests.fixtures.tumbler").logic
        plc = PLC(logic, dt=0.010)
        plc.step()
        target = plc._known_tags_by_name["y_BurnerLoop"]
        for event in pilot_events(plc, target, max_scans=max_scans):
            if event.kind == "finished":
                finished = True
                break
            if time.perf_counter() - started > wall_seconds:
                break
    finally:
        rung_firings._same_writes = original

    total = histogram.total()
    print(
        f"BurnerLoop {'finished' if finished else 'stopped'} in "
        f"{time.perf_counter() - started:.1f}s; {total:,} comparisons"
    )
    print("writes  matched  calls       share")
    print("------  -------  ----------  -------")
    for (write_count, matched), count in sorted(histogram.items()):
        print(f"{write_count:6d}  {str(matched):7}  {count:10,d}  {count / total:6.2%}")

    cases: list[tuple[PMap, dict[str, int], dict[str, int], int]] = []
    for (write_count, matched), count in histogram.items():
        mirror = {f"tag_{index}": index for index in range(write_count)}
        immutable = pmap(mirror)
        writes = dict(mirror)
        if not matched and write_count:
            writes[f"tag_{write_count - 1}"] = -1
        elif not matched:
            writes["unexpected"] = -1
        cases.append((immutable, writes, mirror, count))

    def weighted(comparator: Callable[[PMap, Mapping[str, Any], Mapping[str, Any]], bool]) -> int:
        matches = 0
        for immutable, writes, mirror, count in cases:
            for _ in range(count):
                matches += comparator(immutable, writes, mirror)
        return matches

    comparators = (
        (
            "current-pmap-loop",
            lambda pattern, writes, _mirror: rung_firings._same_writes(pattern, writes),
        ),
        ("zero-storage-hybrid", lambda pattern, writes, _mirror: _hybrid(pattern, writes)),
        ("cached-dict-eq", lambda _pattern, writes, mirror: mirror == writes),
    )
    print("\nweighted comparator loop; best of 5")
    for name, comparator in comparators:
        elapsed = min(
            timeit.repeat(
                lambda comparator=comparator: weighted(comparator),
                number=1,
                repeat=5,
            )
        )
        print(f"{name:20} {elapsed:.4f}s")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--burner", action="store_true")
    parser.add_argument("--calls", type=int, default=100_000)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--memory-patterns", type=int, default=5_000)
    parser.add_argument("--max-scans", type=int, default=40_000)
    parser.add_argument("--wall-seconds", type=float, default=300.0)
    args = parser.parse_args()
    if args.burner:
        run_burner_histogram(args.max_scans, args.wall_seconds)
    else:
        run_microbenchmark(args.calls, args.repeats, args.memory_patterns)


if __name__ == "__main__":
    main()
