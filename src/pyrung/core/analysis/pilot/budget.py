"""Invocation work accounting, shared by disposable experiments and never restored."""

import time
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


class SearchOutOfTime(Exception):
    """A how() ran out of its wall clock.  The search ends with a terminal saying so."""


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
