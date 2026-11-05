"""Invocation work accounting, shared by disposable experiments and never restored."""

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# The ledger, in interpreted scans.  A loop scan costs about a hundredth of one, so what a search
# can spend does not depend on how much of it ran in the loop.  A trace and a kernel compile are
# not charged: a search has few of them, a small max_scans cannot pay for even one, and nothing
# measured them against an answer.  The wall clock is what bounds that time.
STRETCH_SCANS_PER_UNIT = 100

# how()'s backstop on a real clock, in seconds.  The ledger is the bound on scans; most of a
# search's time is its cause and replay work, which the ledger does not charge.
DEFAULT_WALL_CLOCK_S = 300.0

# A clock makes an answer depend on the machine, so a test that asserts answers turns it off.
WALL_CLOCK_ENABLED = True
# What one search may add to the heap, in the allocator's blocks: about 1 GB.  A search keeps
# every state it scans, on purpose (``cause()`` reads them without a replay), and how much one
# holds depends on how many tags change per scan, so scans would not bound it: 23 blocks and
# 1.6 KB a scan on a small program with no loop, much more when more changes.
MAX_SEARCH_BLOCKS = 16_000_000
# The count walks the allocator's pools: 14 us at 1M blocks, 322 us at 16M, on a scan of about
# 200 us at the least.  So it runs every few polls, sooner as what is held nears the cap.
_MOST_POLLS_PER_COUNT = 64


class SearchOutOfTime(BaseException):
    """A how() ran out of its wall clock.  The search ends with a terminal saying so.

    A BaseException, as ``asyncio.CancelledError`` is: the search reads its causes under
    many ``except Exception`` that mean "no evidence", and a clock that runs out while one
    is being read is not evidence of anything.
    """


class SearchOutOfMemory(BaseException):
    """A how() holds all a search may.  The search ends with a terminal saying so."""


@dataclass(frozen=True)
class WallClock:
    """When a how() has to stop, however far it got."""

    limit_s: float
    stop_at: float

    @classmethod
    def starting_now(cls, limit_s: float | None) -> "WallClock | None":
        if limit_s is None or not WALL_CLOCK_ENABLED:
            return None
        return cls(limit_s, time.monotonic() + limit_s)

    def check(self) -> None:
        if time.monotonic() >= self.stop_at:
            raise SearchOutOfTime

    def left(self) -> float:
        return self.stop_at - time.monotonic()


def search_limits(clock: WallClock | None) -> Callable[[], None]:
    """What a running search polls: its clock, and what it holds since this call."""
    start = sys.getallocatedblocks()
    held = 0
    polls = 0
    next_count = 1  # The first scan of a wide program may be most of the cap.

    def check() -> None:
        nonlocal held, polls, next_count
        if clock is not None:
            clock.check()
        polls += 1
        if polls < next_count:
            return
        now = sys.getallocatedblocks() - start
        if now > MAX_SEARCH_BLOCKS:
            raise SearchOutOfMemory
        # At this rate, half way to the cap; a rate that jumps has up to the most polls to show.
        grown = max(1, now - held) / polls
        next_count = max(
            1, min(_MOST_POLLS_PER_COUNT, int((MAX_SEARCH_BLOCKS - now) / (2 * grown)))
        )
        held = now
        polls = 0

    return check


@dataclass
class SearchBudget:
    """Spent search work and productive simulated dwell are separate quantities.

    Charge completed attempts before adoption. A checkpoint or a discarded
    experiment has no authority to refund this ledger. Research dispatches
    consume at least one unit even when they produce no executable scan.
    """

    spent: int = 0
    dwell: int = 0
    experiments: int = 0
    _charged_scans: set[tuple[Any, int]] = field(default_factory=set, repr=False)
    _charged_stretches: set[tuple[Any, int]] = field(default_factory=set, repr=False)
    _stretch_scans: int = 0
    _stretch_units: int = 0

    def charge(self, scans: int = 1, *, dwell_scans: int = 0) -> None:
        if scans < 0:
            raise ValueError("pilot: work charge cannot be negative")
        self.experiments += 1
        self.dwell += dwell_scans
        self.spent += max(1, scans)

    def remaining(self, limit: int) -> int:
        return max(0, limit - self.spent)

    def charge_execution(self, receipt: Any, *, dwell_scans: int = 0) -> None:
        """Charge each epoch-owned scan once across nested consumers.

        An interpreted scan is a unit, and the scans between two of them ran in the loop.
        """
        scans = {
            (span.epoch.reference, scan) for span in receipt.spans for scan in span.kernel_scan_ids
        }
        fresh = scans - self._charged_scans
        units = self._stretch_charge(receipt)
        if fresh:
            self._charged_scans.update(fresh)
            self.charge(len(fresh) + units, dwell_scans=dwell_scans)
        else:
            self.spent += units

    def _stretch_charge(self, receipt: Any) -> int:
        for span in receipt.spans:
            ids = span.kernel_scan_ids
            for before, after in zip(ids, ids[1:], strict=False):
                if after - before > 1:
                    key = (span.epoch.reference, before)
                    if key not in self._charged_stretches:
                        self._charged_stretches.add(key)
                        self._stretch_scans += after - before - 1
        units = self._stretch_scans // STRETCH_SCANS_PER_UNIT
        charged, self._stretch_units = self._stretch_units, units
        return units - charged
