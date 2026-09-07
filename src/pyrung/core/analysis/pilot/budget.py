"""Invocation work accounting, shared by disposable experiments and never restored."""

from dataclasses import dataclass, field
from typing import Any


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

    def charge(self, scans: int = 1, *, dwell_scans: int = 0) -> None:
        if scans < 0:
            raise ValueError("pilot: work charge cannot be negative")
        self.experiments += 1
        self.dwell += dwell_scans
        # Physical kernel evaluations consume work. Folded simulated time
        # does not, regardless of whether the attempt is later retained.
        self.spent += max(1, scans)

    def remaining(self, limit: int) -> int:
        return max(0, limit - self.spent)

    def charge_execution(self, receipt: Any, *, dwell_scans: int = 0) -> None:
        """Charge each epoch-owned kernel scan once across nested consumers."""
        scans = {
            (span.epoch.reference, scan) for span in receipt.spans for scan in span.kernel_scan_ids
        }
        fresh = scans - self._charged_scans
        if fresh:
            self._charged_scans.update(fresh)
            self.charge(len(fresh), dwell_scans=dwell_scans)
