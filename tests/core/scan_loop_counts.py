"""Measuring long runs: what ran in the guarded scan loop, and what did not."""

from __future__ import annotations

from typing import Any

from pyrung import Bool
from pyrung.core import scan_trace

# A stop condition that never holds, for "run this many scans" with run_until().  A
# callable would keep the run interpreted; a condition goes through the loop.
NEVER = Bool("ScanLoopTestNever")


class ScansOutsideLoop:
    """Counts the scans of a runner that did not run in the loop: interpreted or real
    compiled scans.  ``counter["n"]``, as the rate tests read it."""

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
