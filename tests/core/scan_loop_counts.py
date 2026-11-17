"""Count the scans of long runs that ran in the guarded scan loop, and those that did not."""

from __future__ import annotations

from typing import Any

from pyrung import Bool
from pyrung.core import scan_trace

# A stop condition that never holds.  Pass it to run_until() to run a fixed number of scans.
# A callable would keep the run interpreted, but a condition goes through the loop.
NEVER = Bool("ScanLoopTestNever")


class ScansOutsideLoop:
    """Counts the scans of a runner that did not run in the loop.

    These are the interpreted scans and the real compiled scans.  The rate tests read the count
    as ``counter["n"]``.
    """

    def __init__(self, plc: Any) -> None:
        self._interpreted = 0
        self._real_before = scan_trace.TOTALS["real scans"]
        original = plc._run_single_scan

        def counted(**options: Any) -> Any:
            self._interpreted += 1
            return original(**options)

        plc._run_single_scan = counted

    @property
    def n(self) -> int:
        return self._interpreted + scan_trace.TOTALS["real scans"] - self._real_before

    def __getitem__(self, key: str) -> int:
        assert key == "n"
        return self.n
