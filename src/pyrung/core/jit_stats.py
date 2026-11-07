"""Counters over the jit, for finding where a long run's scans go.

    from pyrung.core import jit_stats

    with jit_stats.collect() as stats:
        plc.run_for(60)
    print(stats.report())

They count where the scans ran and why a run stayed out of the guarded loop.  They also count
which guard sent the run out of the loop, and what compiling and tracing cost.  A stretch is a
run of scans in the loop with no other scan between them.  Nothing is counted until something
asks, and then only per run, per stretch and per trace, never per scan.

Set ``PYRUNG_JIT_STATS`` to a file name to count over a whole process, such as a script or a
pytest run.  The process appends the report to that file when it exits.  ``-`` writes it to
standard error instead.  Each pytest-xdist worker appends its own report.
"""

from __future__ import annotations

import atexit
import os
import sys
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

#: The collectors counting now.  The jit asks this before it counts anything.
ACTIVE: list[JitStats] = []


class JitStats:
    """What the jit did while this collector was on."""

    def __init__(self) -> None:
        # Calls of run_for()/run_until() and coast_until(), by kind.
        self.calls: Counter[str] = Counter()
        # Scans by where they ran.  "self check" counts the compiled scans that check a new
        # trace, which are work and not committed scans.
        self.scans: Counter[str] = Counter()
        # Each reason a run, an attempt or a trace stayed out of the loop.
        self.declines: Counter[str] = Counter()
        # What ended each stretch, as a guard's kernel source line or a reason.
        self.left_by: Counter[str] = Counter()
        # Guards that ended one path's loop in a coast, which then went on in another path.
        self.handed_over_by: Counter[str] = Counter()
        # Stretches, scans run and scans they could have run, by bit length of the last.
        self.by_length: dict[int, list[int]] = {}
        # Count and seconds of each costly step, such as a compile or a trace.
        self.costs: dict[str, list[float]] = {}
        # Other counts the loop keeps, such as traces built and misfires.
        self.events: Counter[str] = Counter()

    @property
    def stretches(self) -> int:
        return sum(entry[0] for entry in self.by_length.values())

    @property
    def stretched_scans(self) -> int:
        return sum(entry[1] for entry in self.by_length.values())

    @property
    def could_have_run(self) -> int:
        return sum(entry[2] for entry in self.by_length.values())

    def _stretch(self, used: int, could: int) -> None:
        entry = self.by_length.setdefault(max(could, 1).bit_length(), [0, 0, 0])
        entry[0] += 1
        entry[1] += used
        entry[2] += could

    def as_dict(self) -> dict[str, Any]:
        """The counts in plain types, to compare or to store."""
        return {
            "calls": dict(self.calls),
            "scans": dict(self.scans),
            "declines": dict(self.declines),
            "left_by": dict(self.left_by),
            "handed_over_by": dict(self.handed_over_by),
            "stretches": self.stretches,
            "stretched_scans": self.stretched_scans,
            "could_have_run": self.could_have_run,
            "costs": {name: (int(count), seconds) for name, (count, seconds) in self.costs.items()},
            "events": dict(self.events),
        }

    def report(self) -> str:
        """The counts as text, ASCII except for tag names in kernel source lines."""
        lines = ["jit counters"]
        if self.calls:
            lines.append(
                "  calls: " + ", ".join(f"{n} {kind}" for kind, n in sorted(self.calls.items()))
            )
        total = self.scans["loop"] + self.scans["compiled"] + self.scans["interpreted"]
        lines.append(
            f"  scans: {total}, {self.scans['loop']} in the loop ({_share(self.scans['loop'], total)}), "
            f"{self.scans['compiled']} compiled, {self.scans['interpreted']} interpreted, "
            f"{self.scans['self check']} more to check new traces"
        )
        lines.append(
            f"  stretches: {self.stretches}, which ran {self.stretched_scans} scans of the "
            f"{self.could_have_run} they could have run "
            f"({_share(self.stretched_scans, self.could_have_run)})"
        )
        if self.by_length:
            lines.append(
                "    (a stretch could have run on to the next guard that failed, or the run's end)"
            )
            lines.append(
                f"    {'could run':>15} {'stretches':>10} {'scans run':>10} {'could have':>11}"
            )
            for bits in sorted(self.by_length):
                count, used, could = self.by_length[bits]
                low, high = 1 << (bits - 1), (1 << bits) - 1
                span = str(low) if low == high else f"{low}..{high}"
                lines.append(f"    {span:>15} {count:>10} {used:>10} {could:>11}")
        lines += _counted("  stretches ended by:", self.left_by)
        lines += _counted(
            "  guards that sent a coast from one path to another:", self.handed_over_by
        )
        lines += _counted("  declines:", self.declines)
        if self.costs:
            lines.append("  costs:")
            for name, (count, seconds) in sorted(self.costs.items()):
                lines.append(f"    {name:<12} {int(count):>8} {seconds * 1000:>12.1f} ms")
        lines += _counted("  other counts:", self.events)
        return "\n".join(lines)


def _share(part: int, whole: int) -> str:
    return f"{part / whole:.1%}" if whole else "-"


def _counted(title: str, counts: Counter[str]) -> list[str]:
    if not counts:
        return []
    return [title] + [f"    {n:>8}  {key}" for key, n in counts.most_common()]


@contextmanager
def collect() -> Iterator[JitStats]:
    """Count what the jit does inside the block."""
    stats = JitStats()
    ACTIVE.append(stats)
    try:
        yield stats
    finally:
        ACTIVE.remove(stats)


# --- What the jit calls, each only while ACTIVE holds a collector ---------------------------


def add_scans(where: str, n: int) -> None:
    for stats in ACTIVE:
        stats.scans[where] += n


def add_decline(reason: str, n: int = 1) -> None:
    for stats in ACTIVE:
        stats.declines[reason] += n


def add_event(name: str, n: int = 1) -> None:
    for stats in ACTIVE:
        stats.events[name] += n


def add_handover(guard: str) -> None:
    for stats in ACTIVE:
        stats.handed_over_by[guard] += 1


def add_cost(name: str, seconds: float) -> None:
    for stats in ACTIVE:
        entry = stats.costs.setdefault(name, [0, 0.0])
        entry[0] += 1
        entry[1] += seconds


class Run:
    """One call's stretches.  Each waits for the run's next failed guard or its end, which
    says how long it could have run."""

    def __init__(self, kind: str) -> None:
        self.loop_scans = 0
        self._waiting: list[tuple[int, int]] = []  # (first scan, scans run)
        for stats in ACTIVE:
            stats.calls[kind] += 1

    def stretch(self, first: int, used: int, left_by: str, *, guard_failed: bool) -> None:
        """A stretch ran *used* scans from scan *first*.  *guard_failed* says the next scan
        took another path, so no stretch before it could have run past it."""
        self.loop_scans += used
        add_scans("loop", used)
        for stats in ACTIVE:
            stats.left_by[left_by] += 1
        self._waiting.append((first, used))
        if guard_failed:
            self._settle(first + used)

    def end(self, next_scan: int) -> None:
        """The run ended before scan *next_scan*."""
        self._settle(next_scan)

    def _settle(self, until: int) -> None:
        for first, used in self._waiting:
            for stats in ACTIVE:
                stats._stretch(used, max(until - first, used))
        self._waiting.clear()


def _dump(stats: JitStats, target: str) -> None:
    text = f"pyrung jit counters, process {os.getpid()}\n{stats.report()}\n"
    if target == "-":
        sys.stderr.write(text)
        return
    with open(target, "a", encoding="utf-8") as out:
        out.write(text)


def _from_the_environment() -> None:
    target = os.environ.get("PYRUNG_JIT_STATS")
    if not target:
        return
    stats = JitStats()
    ACTIVE.append(stats)
    atexit.register(_dump, stats, target)


_from_the_environment()
